#!/usr/bin/env python
"""
subprocs.py — discover and persist subprocess activity owned by claude-code sessions.

Why this exists
---------------
A claude-code session can spawn long-running background work (Go pipelines, test
harnesses, build watchers) via the Bash tool's `run_in_background=true`. Those
workloads outlive the human's prompt cycle and are completely invisible to the
existing heartbeat, which only watches the session jsonl mtime. The user has to
guess whether "session idle" means "all done" or "still chewing through 7 days
of data in the background". This module closes that gap.

How it discovers them
---------------------
1. Walk every process via psutil.
2. claude-code streams background-bash stdout to:
       %TEMP%\\claude\\<encoded-cwd>\\<SESSION_UUID>\\tasks\\<TASK_ID>.output
   and keeps a `tail.exe -F <path>` alive while the task runs. That tail.exe is
   the canonical bridge: its cmdline gives us (session_uuid, task_id) directly.
3. For each claude.exe parent we see, build a map of {claude_pid -> session_uuid}
   from its child tail.exe processes.
4. For every other "interesting" process (Go binary, test runner, etc.), walk
   the ppid chain upward. The first claude.exe we hit gives us the session UUID
   via the map from (3). Generic shells, ssh, the runtime python, etc. are
   filtered out.

What it does NOT try to discover
--------------------------------
- Hermes-spawned `delegate_task` subagents — those run inside the parent agent's
  Python process, not as separate OS processes. They show up in heartbeat via
  `subagents/agent-*.jsonl` already.
- Non-Bash MCP subprocesses — out of scope; they're managed by their respective
  server processes, not by a single claude session.

The data sink
-------------
We persist into `projects.db.subprocs`:
    (session_uuid, pid, kind, name, cmdline, cwd, started_at, last_seen_at,
     ended_at, status, task_id)
Each scan() upserts rows by (session_uuid, pid). The next scan that no longer
sees PID X marks it ended ("status='ended'", ended_at=now). This gives the user
the same "alive / just-ended / idle" view they have for sessions, but at the
subprocess level.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
from pathlib import Path

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover — surfaced at call site
    psutil = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Claude Code's official session registry — preferred mapping source.
#
# claude-code itself writes ~/.claude/sessions/<pid>.json for every running
# instance, containing {pid, sessionId, cwd, status, kind, name, updatedAt}.
# This is the authoritative claude-PID → session-UUID mapping; we don't have
# to walk ppid chains or sniff tail.exe cmdlines for it. We still keep the
# tail-based + cwd fallback paths for two reasons:
#   1. older claude-code versions don't write this file
#   2. crash-exit leaves a stale file with a pid the OS reused → can't trust
#      a sessions/<pid>.json blindly unless the actual process exists
# So the priority is:  claude-sessions → tail.exe → cwd-newest-jsonl
# ---------------------------------------------------------------------------

_CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
_CLAUDE_TASKS_DIR    = Path.home() / ".claude" / "tasks"


def _load_claude_session_registry() -> dict[int, dict]:
    """Return {pid: {sessionId, cwd, status, kind, name, updatedAt}}.

    Reads every ~/.claude/sessions/*.json and validates that the PID is still
    alive (psutil) before keeping the entry. Crash-survivors get dropped so
    we never attribute a workload to a recycled-PID session.
    """
    out: dict[int, dict] = {}
    if not _CLAUDE_SESSIONS_DIR.is_dir():
        return out
    alive_pids = set()
    if psutil is not None:
        try:
            alive_pids = set(psutil.pids())
        except Exception:
            alive_pids = set()
    import json as _json
    for f in _CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        if alive_pids and pid not in alive_pids:
            continue  # stale registry entry (crash exit)
        sid = (data.get("sessionId") or "").lower()
        if not sid:
            continue
        out[pid] = {
            "session_uuid": sid,
            "cwd":          data.get("cwd") or "",
            "status":       data.get("status"),       # 'busy' | 'idle'
            "kind":         data.get("kind"),         # 'interactive' | 'print'
            "name":         data.get("name"),         # claude /name label
            "updated_at":   data.get("updatedAt"),
            "started_at":   data.get("startedAt"),
            "version":      data.get("version"),
        }
    return out


def claude_session_status(pid_or_uuid: str | int) -> dict | None:
    """Public lookup: by PID (int) or session UUID (str). Returns the same dict
    structure as _load_claude_session_registry() values, or None if unknown."""
    reg = _load_claude_session_registry()
    if isinstance(pid_or_uuid, int):
        return reg.get(pid_or_uuid)
    target = pid_or_uuid.lower()
    for v in reg.values():
        if v["session_uuid"] == target:
            return v
    return None


def claude_session_tasks(session_uuid: str) -> list[dict]:
    """Return the in-progress / pending tasks for a session.

    claude-code writes per-task JSON to ~/.claude/tasks/<sid>/<n>.json with
    schema {id, subject, description, activeForm, status, blocks, blockedBy}.
    We return them sorted by id desc (most recent first) so the heartbeat
    can show "what's the session actually doing right now" instead of having
    to guess from the latest user prompt.
    """
    task_dir = _CLAUDE_TASKS_DIR / session_uuid.lower()
    if not task_dir.is_dir():
        return []
    import json as _json
    rows: list[tuple[int, dict]] = []
    for f in task_dir.glob("*.json"):
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            num = int(f.stem)
        except ValueError:
            num = 0
        rows.append((num, data))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in rows]


# ---------------------------------------------------------------------------
# constants / filters
# ---------------------------------------------------------------------------

# Names we never attribute as "workload subprocesses" — they're either shells,
# claude-code itself, or boring infrastructure. Lowercase, exact match.
_BORING_NAMES = {
    "bash.exe", "sh.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "claude.exe", "node.exe", "tail.exe", "head.exe", "cat.exe",
    "tee.exe", "grep.exe", "rg.exe", "find.exe", "ls.exe",
    "git.exe", "wsl.exe", "less.exe", "more.exe", "tput.exe",
    "stty.exe", "ssh.exe", "scp.exe", "rsync.exe",
    "code.exe", "code-insiders.exe", "cursor.exe",
    # Console infrastructure / LSPs we never want to call "workload".
    "conhost.exe", "openconsole.exe", "winpty-agent.exe", "winpty.exe",
    "gopls.exe", "rust-analyzer.exe", "pyright.exe", "pylsp.exe",
    "typescript-language-server.exe", "vscode-json-language-server.exe",
    # python here is debatable; we keep it boring because every hermes/cron
    # tick spawns short-lived python. If a workload is a Python long-runner
    # we'll still pick it up via cmdline keyword fallback below.
    "python.exe", "python3.exe", "py.exe",
}

# Cmdline keywords that promote a python.exe back from "boring" to "workload"
# (e.g. uvicorn, gunicorn, jupyter, manage.py runserver). Substring match,
# case-insensitive.
_PYTHON_PROMOTE_KEYWORDS = (
    "uvicorn", "gunicorn", "celery worker", "manage.py runserver",
    "jupyter", "fastapi", "flask run", "streamlit", "scrapy",
)

# Pull the session UUID out of a tail.exe cmdline pointing into
# Temp/claude/<enc-cwd>/<UUID>/tasks/<taskid>.output. We accept both forward
# and back slashes because the path is sometimes msys-style.
_TAIL_UUID_RE = re.compile(
    r"[\\/]claude[\\/][^\\/]+[\\/]"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"[\\/]tasks[\\/]([a-z0-9]+)\.output",
    re.IGNORECASE,
)

# Where claude-code stores per-session jsonl files. The directory name is the
# project cwd with path separators replaced by `-` (drive letter included).
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _encode_cwd(cwd: str) -> str:
    """Mirror claude-code's cwd → projects-dir encoding.

    claude-code replaces both the drive colon and every path separator with
    a single `-`, so `D:\\work\\foo` → `D--work-foo` (drive colon AND the
    leading `\\` each contribute a dash). We use the same rule.
    """
    # Drive colon and both slash flavours each become `-`.
    s = cwd.replace(":", "-").replace("\\", "-").replace("/", "-")
    return s


def _fallback_uuid_for_cwd(cwd: str) -> str | None:
    """When tail-based attribution fails (e.g. the background task was launched
    before claude-code started using the tail.exe streaming pattern, or the tail
    already exited), fall back to "newest jsonl in this cwd's projects dir".

    This is imperfect when one cwd has multiple concurrent sessions, but it's
    the best we can do without an explicit signal — and in practice users
    rarely run two claude sessions against the same project at once.
    """
    if not cwd:
        return None
    enc = _encode_cwd(cwd)
    candidates = [
        _CLAUDE_PROJECTS_DIR / enc,
        # Also try a leading-dash variant — claude-code occasionally prefixes
        # the encoded cwd with `-` (observed on Windows drive paths).
        _CLAUDE_PROJECTS_DIR / ("-" + enc.lstrip("-")),
    ]
    newest_mtime = -1.0
    newest_uuid: str | None = None
    for d in candidates:
        if not d.is_dir():
            continue
        for jl in d.glob("*.jsonl"):
            try:
                if jl.stat().st_mtime > newest_mtime:
                    newest_mtime = jl.stat().st_mtime
                    newest_uuid = jl.stem.lower()
            except OSError:
                continue
    return newest_uuid


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

SCHEMA_SUBPROCS = """
CREATE TABLE IF NOT EXISTS hermes_subprocs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid  TEXT NOT NULL,                  -- attributed claude session
    pid           INTEGER NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'workload',  -- 'workload' | 'tail' | 'shell'
    name          TEXT NOT NULL,                  -- process image name
    cmdline       TEXT,                           -- full cmdline (truncated)
    cwd           TEXT,
    started_at    TEXT NOT NULL,                  -- ISO, from psutil.create_time
    last_seen_at  TEXT NOT NULL,                  -- ISO, updated each scan
    ended_at      TEXT,
    status        TEXT NOT NULL DEFAULT 'alive',  -- 'alive' | 'ended'
    task_id       TEXT,                           -- for kind='tail', the bash task id
    UNIQUE(session_uuid, pid, started_at)
);
CREATE INDEX IF NOT EXISTS idx_subprocs_session ON subprocs(session_uuid);
CREATE INDEX IF NOT EXISTS idx_subprocs_status  ON subprocs(status);
CREATE INDEX IF NOT EXISTS idx_subprocs_last    ON subprocs(last_seen_at DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the subprocs table if missing. Safe to call every tick.

    On the canonical worker-control DB the hermes_subprocs table is already
    owned by the migration; we detect that case and skip the legacy
    `subprocs` CREATE TABLE/INDEX, which would refer to a non-existent
    table name.
    """
    is_canonical = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_profiles'"
    ).fetchone() is not None
    if not is_canonical:
        conn.executescript(SCHEMA_SUBPROCS)
        conn.commit()


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def _safe(p, attr):
    try:
        return p.info.get(attr)
    except Exception:
        return None


def _walk_to_claude(pid_map: dict[int, dict], pid: int, max_depth: int = 12) -> int | None:
    """Walk ppid chain from `pid` until we hit a claude.exe; return that PID."""
    cur = pid
    seen = set()
    for _ in range(max_depth):
        if cur in seen or cur <= 0:
            return None
        seen.add(cur)
        proc = pid_map.get(cur)
        if not proc:
            return None
        if (proc.get("name") or "").lower() == "claude.exe" and "--type=" not in (proc.get("cmdline_str") or ""):
            # Skip claude.exe child renderer/gpu/utility processes — they have
            # --type=renderer/gpu-process/utility. The "real" CLI claude.exe
            # has no --type= flag.
            return cur
        cur = proc.get("ppid") or 0
    return None


def discover(now: _dt.datetime | None = None) -> list[dict]:
    """Return a list of subprocess records, attributed to claude sessions.

    Each record:
        {
            "session_uuid": str,
            "pid": int,
            "kind": "workload" | "tail",
            "name": str,
            "cmdline": str,
            "cwd": str,
            "started_at": iso str,
            "task_id": str | None,   # only for tail
        }
    """
    if psutil is None:
        return []
    now = now or _dt.datetime.now(_dt.timezone.utc)

    # Pass 1 — snapshot every process.
    pid_map: dict[int, dict] = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd", "create_time"]):
        try:
            info = dict(p.info)
        except Exception:
            continue
        cl = info.get("cmdline") or []
        if isinstance(cl, list):
            info["cmdline_str"] = " ".join(cl)
        else:
            info["cmdline_str"] = str(cl or "")
        pid_map[info["pid"]] = info

    # Pass 1.5 — load claude-code's official session registry. This gives us
    # the authoritative {claude_pid -> session_uuid} mapping for every
    # currently-running claude.exe, without any cmdline sniffing. We layer
    # the tail-based + cwd-fallback signals on top of this for older
    # claude-code versions that don't write the registry.
    claude_registry = _load_claude_session_registry()
    claude_to_session: dict[int, tuple[str, float]] = {
        pid: (info["session_uuid"], (info.get("started_at") or 0) / 1000.0)
        for pid, info in claude_registry.items()
    }

    # Pass 2 — augment with tail.exe children → session UUID. This still wins
    # for old claude-code versions and is also our only source for the
    # tail.exe records themselves (which we persist for forensics).
    tail_records: list[dict] = []
    for info in pid_map.values():
        if (info.get("name") or "").lower() != "tail.exe":
            continue
        m = _TAIL_UUID_RE.search(info["cmdline_str"])
        if not m:
            continue
        session_uuid = m.group(1).lower()
        task_id = m.group(2)
        parent_pid = info.get("ppid") or 0
        claude_pid = _walk_to_claude(pid_map, parent_pid)
        if claude_pid is not None and claude_pid not in claude_to_session:
            ct = info.get("create_time") or 0.0
            claude_to_session[claude_pid] = (session_uuid, ct)
        # Record the tail itself as a (kind=tail) subprocess. Users typically
        # don't care about tail.exe per se, but having it in the DB makes the
        # 1bd94511-style "where did the data come from" forensics trivial.
        tail_records.append({
            "session_uuid": session_uuid,
            "pid": info["pid"],
            "kind": "tail",
            "name": info["name"],
            "cmdline": info["cmdline_str"][:500],
            "cwd": info.get("cwd") or "",
            "started_at": _dt.datetime.fromtimestamp(
                info.get("create_time") or 0.0, tz=_dt.timezone.utc
            ).isoformat(timespec="seconds"),
            "task_id": task_id,
        })

    # Pass 3 — for every non-boring process, walk up to claude.exe and attribute.
    workload_records: list[dict] = []
    for info in pid_map.values():
        name = (info.get("name") or "").lower()
        if not name:
            continue
        cmdline_str = info["cmdline_str"]
        # Filter boring infrastructure with one exception: pythons running
        # long-lived servers (uvicorn/gunicorn/etc) are real workloads.
        if name in _BORING_NAMES:
            if name in ("python.exe", "python3.exe", "py.exe"):
                low = cmdline_str.lower()
                if not any(kw in low for kw in _PYTHON_PROMOTE_KEYWORDS):
                    continue
            else:
                continue
        # Don't double-count tails — they're already captured above.
        if name == "tail.exe":
            continue
        parent_pid = info.get("ppid") or 0
        claude_pid = _walk_to_claude(pid_map, parent_pid)
        if claude_pid is None:
            continue
        session = claude_to_session.get(claude_pid)
        session_uuid: str | None = None
        if session:
            session_uuid = session[0]
        else:
            # Fall back to "newest jsonl in this claude.exe's cwd-encoded dir".
            claude_info = pid_map.get(claude_pid) or {}
            session_uuid = _fallback_uuid_for_cwd(claude_info.get("cwd") or "")
        if not session_uuid:
            continue
        workload_records.append({
            "session_uuid": session_uuid,
            "pid": info["pid"],
            "kind": "workload",
            "name": info["name"],
            "cmdline": cmdline_str[:500],
            "cwd": info.get("cwd") or "",
            "started_at": _dt.datetime.fromtimestamp(
                info.get("create_time") or 0.0, tz=_dt.timezone.utc
            ).isoformat(timespec="seconds"),
            "task_id": None,
        })

    return tail_records + workload_records


# ---------------------------------------------------------------------------
# DB sync
# ---------------------------------------------------------------------------

def sync(conn: sqlite3.Connection, records: list[dict],
         now: _dt.datetime | None = None) -> dict:
    """Upsert records, mark missing-but-alive rows as ended.

    Returns a small stats dict: {"alive": N, "ended_now": M, "kept": K}.
    """
    ensure_schema(conn)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    seen_keys: set[tuple[str, int, str]] = set()
    for r in records:
        key = (r["session_uuid"], r["pid"], r["started_at"])
        seen_keys.add(key)
        conn.execute("""
            INSERT INTO hermes_subprocs (session_uuid, pid, kind, name, cmdline,
                                  cwd, started_at, last_seen_at, ended_at,
                                  status, task_id)
            VALUES (?,?,?,?,?,?,?,?,NULL,'alive',?)
            ON CONFLICT(session_uuid, pid, started_at) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                status       = 'alive',
                ended_at     = NULL,
                cmdline      = excluded.cmdline,
                cwd          = excluded.cwd
        """, (
            r["session_uuid"], r["pid"], r["kind"], r["name"], r["cmdline"],
            r["cwd"], r["started_at"], now_iso, r.get("task_id"),
        ))

    # Mark any row that was alive on last scan but didn't show up this scan.
    ended_now = 0
    rows = conn.execute(
        "SELECT id, session_uuid, pid, started_at FROM hermes_subprocs WHERE status='alive'"
    ).fetchall()
    for row in rows:
        key = (row["session_uuid"], row["pid"], row["started_at"])
        if key in seen_keys:
            continue
        conn.execute(
            "UPDATE hermes_subprocs SET status='ended', ended_at=? WHERE id=?",
            (now_iso, row["id"]),
        )
        ended_now += 1
    conn.commit()

    alive = conn.execute(
        "SELECT COUNT(*) FROM hermes_subprocs WHERE status='alive'"
    ).fetchone()[0]
    return {"alive": alive, "ended_now": ended_now, "kept": len(seen_keys)}


def sync_claude_registry(conn: sqlite3.Connection,
                         now: _dt.datetime | None = None) -> int:
    """Push the claude-code-side mutable metadata (claude_name, claude_status)
    into the sessions table. Strictly separate from hermes_sessions.name, which is the
    hermes-assigned stable slug.

    Returns the number of rows updated. Silently skips rows whose UUID we
    don't track yet — sync-native picks those up on its own schedule.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    reg = _load_claude_session_registry()
    # Confirm the sessions table has the columns we need. If older worker
    # installs haven't migrated yet we silently no-op rather than throwing
    # — the schema migration runs on the next `projects.py` invocation.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "claude_name" not in cols or "claude_status" not in cols:
        return 0
    updated = 0
    for entry in reg.values():
        cur = conn.execute(
            "UPDATE hermes_sessions SET claude_name=?, claude_status=?, claude_status_at=? "
            "WHERE lower(uuid)=?",
            (entry.get("name"), entry.get("status"), now_iso,
             entry["session_uuid"]),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def scan_and_persist(conn: sqlite3.Connection,
                     now: _dt.datetime | None = None) -> tuple[list[dict], dict]:
    """One-call helper: discover subprocs + sync to DB + push claude
    registry metadata onto matching session rows."""
    records = discover(now=now)
    stats = sync(conn, records, now=now)
    try:
        stats["registry_updated"] = sync_claude_registry(conn, now=now)
    except Exception:
        # Schema may not be migrated on this DB yet. Don't fail the heartbeat.
        stats["registry_updated"] = -1
    return records, stats


# ---------------------------------------------------------------------------
# read-side helpers (for heartbeat / CLI)
# ---------------------------------------------------------------------------

def for_session(conn: sqlite3.Connection, session_uuid: str,
                window_min: int = 30) -> dict:
    """Return active + recently-ended subprocs for one session.

    `recently-ended` = ended_at within the heartbeat window. The same
    bucket semantics the heartbeat already uses for sessions.
    """
    ensure_schema(conn)
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(minutes=window_min)).isoformat(timespec="seconds")
    alive = [dict(r) for r in conn.execute(
        "SELECT * FROM hermes_subprocs WHERE session_uuid=? AND status='alive' "
        "ORDER BY started_at",
        (session_uuid.lower(),),
    )]
    just_ended = [dict(r) for r in conn.execute(
        "SELECT * FROM hermes_subprocs WHERE session_uuid=? AND status='ended' "
        "AND ended_at >= ? ORDER BY ended_at DESC",
        (session_uuid.lower(), cutoff),
    )]
    return {"alive": alive, "just_ended": just_ended}


def main() -> int:
    """CLI: discover claude-code subprocs + sync to canonical DB.

    Usage:
        workerctl-hermes-subprocs              # one-shot scan + sync
        workerctl-hermes-subprocs --json       # also dump current state as JSON
    """
    import argparse
    import json as _json
    import os
    import sqlite3

    parser = argparse.ArgumentParser(
        description="Discover and sync claude-code workload subprocesses"
    )
    parser.add_argument("--db", help="override DB path (else WORKER_PROJECTS_DB / canonical)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON summary of scan + DB state")
    args = parser.parse_args()

    db_path = (
        args.db
        or os.environ.get("WORKER_PROJECTS_DB")
        or "D:/work-github/.worker-control/worker-control.sqlite3"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    records, stats = scan_and_persist(conn)
    if args.json:
        print(_json.dumps({"scan_stats": stats, "records": records},
                          ensure_ascii=False, indent=2, default=str))
    else:
        print(f"scanned: {len(records)} processes")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
