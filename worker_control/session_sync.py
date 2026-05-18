"""session_sync — single writer for the ``hermes_sessions`` table.

Phase 2 PR #4. Before this module existed, ``hermes_sessions`` had three
independent INSERT/UPDATE paths (``worker_control_hermes.projects``
``cmd_session_start`` + ``cmd_session_sync_native`` + the heartbeat
back-fill in ``worker_control.hermes_session_sync``), each with its own
view of which columns to touch and which to leave alone. The result was a
3-day stale ``last_used_at`` for ``origin='native'`` sessions because
``cmd_session_sync_native`` was never invoked by any scheduler — and even
when it was, the spawn/native dispatcher and the native scanner could
race and clobber each other's metadata.

This module centralises every write into ``upsert_session``. The dataclass
``SessionUpsert`` is the only shape callers construct; the helper
``from_*`` factories adapt the three known sources (claude-code ``.jsonl``
transcripts, hermes-profile ``session_*.json`` files, and the dispatcher
argv from ``cmd_session_start``) into that shape.

Locked-in rules — see PR #4 review:

* ``origin`` is written EXACTLY ONCE, on the INSERT that creates the row.
  No UPDATE path may change it. (A native session that the dispatcher
  later spawns is reclassified via the ``runs`` table by
  ``_reclassify_origins`` — never by overwriting ``hermes_sessions.origin``
  directly.)
* ``last_used_at`` always advances forward; we MAX() it with whatever
  the caller supplied so a slow native sync never rewinds a session that
  the heartbeat already touched.
* Enrichment columns populated by ``hermes_session_sync`` (``cwd``,
  ``first_message``, ``last_message``, ``turn_count``,
  ``total_cost_usd``, ``hermes_model``, ``started_at``,
  ``ended_at_synced``) are NEVER touched here. ``upsert_session`` only
  writes the columns owned by the dispatcher / native-import surface.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _norm_path(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def _decode_claude_project_dir(name: str) -> str | None:
    if not name or not name[0].isalpha():
        return None
    m = re.match(r"^([A-Za-z])--(.+)$", name)
    if not m:
        return None
    drive, rest = m.group(1), m.group(2)
    candidate = f"{drive}:\\" + rest.replace("-", "\\")
    if Path(candidate).is_dir():
        return candidate
    parts = rest.split("-")
    for n_merge in range(1, len(parts)):
        head = parts[:-n_merge]
        tail = "-".join(parts[-n_merge:])
        candidate2 = (
            f"{drive}:\\" + "\\".join(head + [tail]) if head else f"{drive}:\\{tail}"
        )
        if Path(candidate2).is_dir():
            return candidate2
    return None


@dataclass(slots=True)
class SessionUpsert:
    """One row's worth of intent for ``hermes_sessions``.

    ``project_path`` is resolved to ``project_id`` inside ``upsert_session``
    so callers don't have to thread project lookups through every factory.
    """

    uuid: str
    name: str
    origin: str  # 'spawned' | 'native' — applied only on INSERT
    project_path: str
    brief: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    last_used_at: str | None = None
    ended_at: str | None = None
    status: str | None = None
    claude_name: str | None = None
    notes: str | None = None
    created_at: str | None = None
    # Raw counts kept around for the auto-register "[sync] native session
    # from …" notes string — they're not columns on hermes_sessions, just
    # context for the notes blob.
    user_count: int = 0
    assistant_count: int = 0
    subagent_count: int = 0
    claude_version: str | None = None
    source_path: str | None = None

    def __post_init__(self) -> None:
        self.uuid = (self.uuid or "").lower()
        if not _UUID_RE.match(self.uuid):
            raise ValueError(f"SessionUpsert: invalid uuid {self.uuid!r}")
        if self.origin not in ("spawned", "native"):
            raise ValueError(f"SessionUpsert: invalid origin {self.origin!r}")


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _scan_jsonl(jsonl_path: Path) -> dict[str, Any]:
    """Read a claude-code ``.jsonl`` transcript and pull our summary fields.

    Mirrors ``worker_control_hermes.projects._scan_native_session`` so the
    two stay byte-for-byte compatible (when ``projects.py`` finally
    delegates to ``upsert_session``, the on-disk semantics don't shift).
    """
    info: dict[str, Any] = {
        "uuid": jsonl_path.stem,
        "cwd": None,
        "first_user_at": None,
        "last_event_at": None,
        "custom_title": None,
        "first_user_text": "",
        "user_count": 0,
        "assistant_count": 0,
        "subagent_count": 0,
        "model": None,
        "version": None,
    }
    if not jsonl_path.is_file():
        return info
    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                t = d.get("type")
                if d.get("timestamp"):
                    info["last_event_at"] = d["timestamp"]
                if d.get("cwd") and not info["cwd"]:
                    info["cwd"] = d["cwd"]
                if d.get("version") and not info["version"]:
                    info["version"] = d["version"]
                if t == "custom-title":
                    info["custom_title"] = d.get("customTitle")
                elif t == "agent-name" and not info["custom_title"]:
                    info["custom_title"] = d.get("agentName")
                elif t == "user":
                    info["user_count"] += 1
                    if not info["first_user_text"]:
                        m = d.get("message")
                        text = ""
                        if isinstance(m, dict):
                            c = m.get("content", "")
                            if isinstance(c, list):
                                for x in c:
                                    if isinstance(x, dict) and x.get("text"):
                                        text = x["text"]
                                        break
                            else:
                                text = c or ""
                        else:
                            text = str(m)
                        text = (text or "").strip()
                        if text.startswith("<") and ">" in text[:40]:
                            continue
                        if not text:
                            continue
                        text = re.sub(r"\s+", " ", text)
                        info["first_user_text"] = text[:300]
                        if d.get("timestamp"):
                            info["first_user_at"] = d["timestamp"]
                elif t == "assistant":
                    info["assistant_count"] += 1
                    msg = d.get("message")
                    if isinstance(msg, dict) and msg.get("model"):
                        info["model"] = msg["model"]
    except Exception:
        return info

    sub_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
    if sub_dir.is_dir():
        info["subagent_count"] = sum(1 for _ in sub_dir.glob("agent-*.jsonl"))
    return info


def from_jsonl(jsonl_path: str | Path) -> SessionUpsert | None:
    """Build a SessionUpsert from a claude-code ``.jsonl`` transcript.

    Returns ``None`` if the file is unreadable, has no UUID-shaped name,
    or carries no recoverable ``cwd`` (either inside the transcript or
    decodable from its parent directory). Never raises.
    """
    jsonl_path = Path(jsonl_path)
    uid = jsonl_path.stem
    if not _UUID_RE.match(uid):
        return None
    info = _scan_jsonl(jsonl_path)
    cwd = info["cwd"] or _decode_claude_project_dir(jsonl_path.parent.name)
    if not cwd:
        return None
    now = _now()
    name = (
        info["custom_title"]
        or (info["first_user_text"][:48] if info["first_user_text"] else "")
        or f"native-{uid[:8]}"
    )
    last_used = info["last_event_at"] or info["first_user_at"] or now
    first_seen = info["first_user_at"] or last_used
    brief = info["first_user_text"][:200] if info["first_user_text"] else None
    return SessionUpsert(
        uuid=uid,
        name=name,
        origin="native",
        project_path=_norm_path(cwd),
        brief=brief,
        model=info["model"],
        last_used_at=last_used,
        created_at=first_seen,
        status="active",
        user_count=info["user_count"],
        assistant_count=info["assistant_count"],
        subagent_count=info["subagent_count"],
        claude_version=info["version"],
        source_path=str(jsonl_path),
    )


def from_profile_session_json(path: str | Path) -> SessionUpsert | None:
    """Build a SessionUpsert from a hermes-profile ``session_*.json`` file.

    Hermes profile session files describe **agent** sessions, not claude
    sessions, so most of the time there is no claude UUID to upsert and
    we return ``None``. We still look for a ``claude_session_id`` /
    ``session_uuid`` field (some hermes profiles started writing it for
    debug purposes) so future profile shapes can drive a write here
    without another writer popping into existence.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    uid = (
        data.get("claude_session_id")
        or data.get("session_uuid")
        or data.get("uuid")
    )
    if not isinstance(uid, str) or not _UUID_RE.match(uid):
        return None
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return SessionUpsert(
        uuid=uid,
        name=str(data.get("name") or f"profile-{uid[:8]}"),
        origin="native",
        project_path=_norm_path(cwd),
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        last_used_at=data.get("last_updated")
        if isinstance(data.get("last_updated"), str)
        else None,
        created_at=data.get("session_start")
        if isinstance(data.get("session_start"), str)
        else None,
        status="active",
        source_path=str(path),
    )


def from_dispatcher_argv(
    *,
    name: str,
    uuid: str,
    project_path: str,
    brief: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    created_at: str | None = None,
) -> SessionUpsert:
    """Build a SessionUpsert for a dispatcher-spawned session.

    Origin is fixed to ``spawned``. This is the only factory that can
    introduce a spawned row — ``from_jsonl`` and
    ``from_profile_session_json`` always emit ``native`` (the worker's
    ``_reclassify_origins`` will promote them later if a ``-p`` run
    materialises against them).
    """
    now = _now()
    return SessionUpsert(
        uuid=uuid,
        name=name,
        origin="spawned",
        project_path=project_path,
        brief=brief,
        model=model,
        permission_mode=permission_mode,
        created_at=created_at or now,
        last_used_at=now,
        status="active",
    )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class ProjectNotRegistered(LookupError):
    """Raised when ``upsert_session`` can't resolve ``project_path``.

    Callers that want auto-registration (``cmd_session_sync_native
    --auto-register-projects``) must register the project BEFORE invoking
    ``upsert_session``. This writer never creates projects.
    """


def _resolve_project_id(conn: sqlite3.Connection, project_path: str) -> int | None:
    norm = _norm_path(project_path)
    row = conn.execute(
        "SELECT id FROM hermes_projects_v WHERE folder_path=?", (norm,)
    ).fetchone()
    if row is None:
        return None
    # Support both sqlite3.Row and plain tuple for test ergonomics.
    try:
        return int(row["id"])
    except (IndexError, TypeError):
        return int(row[0])


def upsert_session(conn: sqlite3.Connection, upsert: SessionUpsert) -> int:
    """Insert or update one ``hermes_sessions`` row. Returns the row id.

    UPDATE path NEVER touches ``origin`` or columns owned by the
    hermes-agent-session enrichment back-fill. ``last_used_at`` advances
    monotonically (we MAX() with whatever's already on the row).
    """
    project_id = _resolve_project_id(conn, upsert.project_path)
    if project_id is None:
        raise ProjectNotRegistered(upsert.project_path)

    now = _now()
    last_used = upsert.last_used_at or now
    created = upsert.created_at or last_used or now

    existing = conn.execute(
        "SELECT id, last_used_at, brief, model, claude_name "
        "FROM hermes_sessions WHERE uuid=?",
        (upsert.uuid,),
    ).fetchone()

    if existing is None:
        notes = upsert.notes or ""
        if upsert.source_path and not notes:
            notes = (
                f"[sync] native session from {upsert.source_path}\n"
                f"        user/asst/sub = {upsert.user_count}/"
                f"{upsert.assistant_count}/{upsert.subagent_count}\n"
                f"        claude version = {upsert.claude_version or '?'}"
            )
        conn.execute(
            "INSERT INTO hermes_sessions("
            "uuid, project_id, name, status, origin, model, permission_mode, "
            "brief, notes, created_at, last_used_at, ended_at, claude_name"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                upsert.uuid,
                project_id,
                upsert.name,
                upsert.status or "active",
                upsert.origin,
                upsert.model,
                upsert.permission_mode,
                upsert.brief or "",
                notes,
                created,
                last_used,
                upsert.ended_at,
                upsert.claude_name,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    # UPDATE path — origin stays untouched, last_used_at monotonic, all
    # other fields COALESCE so a partial caller doesn't blank populated
    # columns.
    try:
        existing_id = int(existing["id"])
        existing_last = existing["last_used_at"]
    except (IndexError, TypeError):
        existing_id = int(existing[0])
        existing_last = existing[1]
    new_last = max(existing_last or "", last_used or "")

    sets: list[str] = ["last_used_at=?"]
    vals: list[Any] = [new_last]
    if upsert.brief is not None:
        sets.append("brief=?")
        vals.append(upsert.brief)
    if upsert.model is not None:
        sets.append("model=COALESCE(?, model)")
        vals.append(upsert.model)
    if upsert.name:
        sets.append("name=?")
        vals.append(upsert.name)
    if upsert.permission_mode is not None:
        sets.append("permission_mode=?")
        vals.append(upsert.permission_mode)
    if upsert.ended_at is not None:
        sets.append("ended_at=?")
        vals.append(upsert.ended_at)
    if upsert.status is not None:
        sets.append("status=?")
        vals.append(upsert.status)
    if upsert.claude_name is not None:
        sets.append("claude_name=?")
        vals.append(upsert.claude_name)
    vals.append(existing_id)
    conn.execute(
        f"UPDATE hermes_sessions SET {', '.join(sets)} WHERE id=?", vals
    )
    return existing_id


# ---------------------------------------------------------------------------
# Sync entry-points
# ---------------------------------------------------------------------------


def sync_jsonl_dir(
    conn: sqlite3.Connection, root: str | Path
) -> dict[str, int]:
    """Discover claude-code ``.jsonl`` transcripts under ``root`` and upsert each.

    ``root`` is the directory that contains one sub-directory per claude
    project (the structure ``~/.claude/projects`` uses). Files that have
    no UUID stem, no recoverable cwd, or whose project isn't registered
    are counted under ``skipped`` rather than raised.

    Returns a dict with ``{"created", "updated", "skipped"}`` counts.
    Idempotent — re-running against the same tree only bumps
    ``updated`` (and ``last_used_at`` to whatever the transcript now
    shows).
    """
    root = Path(root)
    counts = {"created": 0, "updated": 0, "skipped": 0}
    if not root.is_dir():
        return counts

    existing_ids = {
        row[0]
        for row in conn.execute("SELECT uuid FROM hermes_sessions").fetchall()
    }

    for proj_dir in root.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            upsert = from_jsonl(jsonl)
            if upsert is None:
                counts["skipped"] += 1
                continue
            try:
                upsert_session(conn, upsert)
            except ProjectNotRegistered:
                counts["skipped"] += 1
                continue
            if upsert.uuid in existing_ids:
                counts["updated"] += 1
            else:
                counts["created"] += 1
                existing_ids.add(upsert.uuid)
    return counts


# ---------------------------------------------------------------------------
# sync_all — disk-walking entry point (Phase 2 PR #6)
# ---------------------------------------------------------------------------
#
# PR #4 introduced the writer and PR #5 the reader. PR #6 wires both ends to a
# scheduler-friendly entry point that walks every known on-disk source and
# materialises the rows. ``workerctl session sync-all`` + the heartbeat tick
# call this; nothing else writes to ``hermes_sessions`` outside the
# already-delegating per-row paths.
#
# Walk targets:
#   1. ``~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`` — every claude-code
#      transcript that ships UUID-shaped names. ``from_jsonl`` decides
#      whether the file is upsertable.
#   2. ``<hermes_home>/profiles/<name>/sessions/session_*.json`` and
#      ``<hermes_home>/sessions/session_*.json`` — hermes-profile session
#      files. ``from_profile_session_json`` only emits a write when the
#      JSON carries an explicit ``claude_session_id`` (rare today, but
#      future-proof — see PR #4 review).
#
# Skip behaviour:
#   * mtime-keyed skip — we pre-fetch ``(uuid, last_used_at)`` from
#     ``hermes_sessions`` once and, for every file whose mtime is already
#     covered, do not even open it. This is the entire reason a 50-file
#     heartbeat call lands under 1 s.
#   * ``--since <iso>`` — files older than the cutoff are ignored outright.
#
# Reclassify:
#   After all upserts, we call the same SQL ``_reclassify_origins`` ships
#   (inline-copied here to keep ``session_sync`` import-free of the hermes
#   sub-package). Idempotent.

_HERMES_PROJECTS_SUBDIR = "projects"  # not used, kept for symmetry with docs


def _default_claude_projects_dir() -> Path:
    return Path(
        os.environ.get("CLAUDE_PROJECTS_DIR")
        or (Path.home() / ".claude" / "projects")
    )


def _default_hermes_home() -> Path | None:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "hermes"
        return Path.home() / "AppData" / "Local" / "hermes"
    # POSIX preferred → fallback
    for c in (
        Path.home() / ".local" / "share" / "hermes",
        Path.home() / ".hermes",
    ):
        if c.is_dir():
            return c
    return None


def _iter_hermes_session_jsons(hermes_home: Path) -> Iterable[Path]:
    """Yield every ``session_*.json`` reachable under a hermes home.

    Walks ``profiles/<name>/sessions/`` (the normal case) and the
    top-level ``sessions/`` directory (the implicit default profile).
    """
    if not hermes_home.is_dir():
        return
    root_sessions = hermes_home / "sessions"
    if root_sessions.is_dir():
        for j in root_sessions.glob("session_*.json"):
            yield j
    profiles_dir = hermes_home / "profiles"
    if profiles_dir.is_dir():
        for prof in profiles_dir.iterdir():
            sd = prof / "sessions"
            if sd.is_dir():
                for j in sd.glob("session_*.json"):
                    yield j


def _reclassify_origins(conn: sqlite3.Connection) -> tuple[int, int]:
    """Inline copy of ``worker_control_hermes.projects._reclassify_origins``.

    Duplicated to keep ``session_sync`` from importing the hermes
    sub-package (which itself imports ``session_sync`` — circular). The
    SQL is locked-in (see the docstring in ``projects.py``); if it ever
    changes there, change it here too.
    """
    conn.execute(
        """
        UPDATE hermes_sessions
           SET origin = CASE
               WHEN EXISTS (
                   SELECT 1 FROM hermes_runs r
                   WHERE r.session_id = hermes_sessions.id AND r.mode = 'print'
               ) THEN 'spawned'
               ELSE 'native'
           END
        """
    )
    sp = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE origin='spawned'"
    ).fetchone()[0]
    nv = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE origin='native'"
    ).fetchone()[0]
    return int(sp), int(nv)


@dataclass(slots=True)
class SyncAllResult:
    synced_jsonl: int = 0
    synced_profile: int = 0
    skipped_mtime_unchanged: int = 0
    skipped_no_project: int = 0
    skipped_no_uuid: int = 0
    errors: int = 0
    reclassify_spawned: int = 0
    reclassify_native: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "synced_jsonl": self.synced_jsonl,
            "synced_profile": self.synced_profile,
            "skipped_mtime_unchanged": self.skipped_mtime_unchanged,
            "skipped_no_project": self.skipped_no_project,
            "skipped_no_uuid": self.skipped_no_uuid,
            "errors": self.errors,
            "reclassify_spawned": self.reclassify_spawned,
            "reclassify_native": self.reclassify_native,
            "duration_ms": self.duration_ms,
        }


def _mtime_iso(p: Path) -> str | None:
    try:
        ts = p.stat().st_mtime
    except OSError:
        return None
    return _dt.datetime.fromtimestamp(
        ts, tz=_dt.timezone.utc
    ).isoformat(timespec="seconds")


def sync_all(
    conn: sqlite3.Connection,
    *,
    claude_projects_dir: Path | str | None = None,
    hermes_home: Path | str | None = None,
    since: str | None = None,
    dry_run: bool = False,
    quiet: bool = True,
    progress_every: int = 50,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> SyncAllResult:
    """Walk every disk source and upsert into ``hermes_sessions``.

    Parameters
    ----------
    conn:
        Open SQLite connection.  Caller owns the lifecycle; we do not
        commit (sqlite3 autocommit + ``isolation_level=None`` is how
        ``worker_control.db.connect`` already opens it).
    claude_projects_dir:
        Override for ``~/.claude/projects``.  Falls back to env
        ``CLAUDE_PROJECTS_DIR`` then to the home-relative default.
    hermes_home:
        Override for the hermes home.  ``None`` → ``HERMES_HOME`` env,
        then platform default.  If the path doesn't exist the profile
        walk is silently skipped (a host without hermes is fine).
    since:
        ISO-8601 cutoff.  Files older than this are not opened.
    dry_run:
        Build the result counters without touching the DB.
    quiet:
        Suppress per-50-file progress prints.
    """
    t0 = time.monotonic()
    result = SyncAllResult()

    claude_dir = Path(claude_projects_dir) if claude_projects_dir else _default_claude_projects_dir()
    home = Path(hermes_home) if hermes_home else _default_hermes_home()

    # Pre-fetch the (uuid → last_used_at) cache for mtime-skip.  One scan
    # of an already-synced ledger is cheap; one row per touched session is
    # the difference between "heartbeat takes 30 s" and "heartbeat takes
    # 0.3 s".
    cache: dict[str, str] = {}
    try:
        for row in conn.execute(
            "SELECT uuid, last_used_at FROM hermes_sessions"
        ).fetchall():
            try:
                cache[row["uuid"]] = row["last_used_at"] or ""
            except (IndexError, TypeError):
                cache[row[0]] = row[1] or ""
    except sqlite3.OperationalError:
        # Fresh DB with no schema yet — nothing to skip against.
        pass

    def _emit(label: str, processed: int, total: int) -> None:
        if on_progress is not None:
            on_progress(label, processed, total)
        elif not quiet and progress_every and processed % progress_every == 0:
            print(f"[sync-all] {label} … {processed}/{total}")

    # ----- 1. claude-code jsonl walk ----------------------------------------
    jsonl_paths: list[Path] = []
    if claude_dir.is_dir():
        for proj_dir in claude_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for j in proj_dir.glob("*.jsonl"):
                jsonl_paths.append(j)

    total_jsonl = len(jsonl_paths)
    for idx, jpath in enumerate(jsonl_paths, start=1):
        try:
            mtime_iso = _mtime_iso(jpath)
            if since and mtime_iso and mtime_iso < since:
                result.skipped_mtime_unchanged += 1
                _emit("jsonl", idx, total_jsonl)
                continue
            uid = jpath.stem.lower()
            cached_last = cache.get(uid)
            if (
                cached_last
                and mtime_iso
                and mtime_iso <= cached_last
            ):
                # File hasn't moved since the row's last_used_at — nothing
                # to do.  This is the hot path on a heartbeat tick.
                result.skipped_mtime_unchanged += 1
                _emit("jsonl", idx, total_jsonl)
                continue

            upsert = from_jsonl(jpath)
            if upsert is None:
                result.skipped_no_uuid += 1
                _emit("jsonl", idx, total_jsonl)
                continue
            if dry_run:
                result.synced_jsonl += 1
                _emit("jsonl", idx, total_jsonl)
                continue
            try:
                upsert_session(conn, upsert)
            except ProjectNotRegistered:
                result.skipped_no_project += 1
                _emit("jsonl", idx, total_jsonl)
                continue
            cache[upsert.uuid] = upsert.last_used_at or cached_last or ""
            result.synced_jsonl += 1
            _emit("jsonl", idx, total_jsonl)
        except Exception:
            result.errors += 1
            _emit("jsonl", idx, total_jsonl)

    # ----- 2. hermes profile session_*.json walk ----------------------------
    profile_paths: list[Path] = []
    if home is not None:
        profile_paths = list(_iter_hermes_session_jsons(home))
    total_prof = len(profile_paths)
    for idx, ppath in enumerate(profile_paths, start=1):
        try:
            mtime_iso = _mtime_iso(ppath)
            if since and mtime_iso and mtime_iso < since:
                result.skipped_mtime_unchanged += 1
                _emit("profile", idx, total_prof)
                continue

            upsert = from_profile_session_json(ppath)
            if upsert is None:
                # No claude_session_id in this file — that's the normal
                # case for hermes-only sessions; not an error.
                result.skipped_no_uuid += 1
                _emit("profile", idx, total_prof)
                continue
            cached_last = cache.get(upsert.uuid)
            if (
                cached_last
                and mtime_iso
                and mtime_iso <= cached_last
                and (not upsert.last_used_at or upsert.last_used_at <= cached_last)
            ):
                result.skipped_mtime_unchanged += 1
                _emit("profile", idx, total_prof)
                continue
            if dry_run:
                result.synced_profile += 1
                _emit("profile", idx, total_prof)
                continue
            try:
                upsert_session(conn, upsert)
            except ProjectNotRegistered:
                result.skipped_no_project += 1
                _emit("profile", idx, total_prof)
                continue
            cache[upsert.uuid] = upsert.last_used_at or cached_last or ""
            result.synced_profile += 1
            _emit("profile", idx, total_prof)
        except Exception:
            result.errors += 1
            _emit("profile", idx, total_prof)

    # ----- 3. reclassify origins -------------------------------------------
    if not dry_run:
        try:
            sp, nv = _reclassify_origins(conn)
            result.reclassify_spawned = sp
            result.reclassify_native = nv
        except sqlite3.OperationalError:
            # Schema missing — leave 0/0.
            pass

    result.duration_ms = int((time.monotonic() - t0) * 1000)
    return result


__all__ = [
    "ProjectNotRegistered",
    "SessionUpsert",
    "SyncAllResult",
    "from_dispatcher_argv",
    "from_jsonl",
    "from_profile_session_json",
    "sync_all",
    "sync_jsonl_dir",
    "upsert_session",
]
