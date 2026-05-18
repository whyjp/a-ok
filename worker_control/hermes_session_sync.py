"""hermes_session_sync — persist Hermes Agent turn-by-turn sessions to SQLite.

The "hermes 세션" dashboard tab used to read
``~/AppData/Local/hermes/profiles/<name>/sessions/session_*.json`` live from
disk on every snapshot request. That mixed two responsibilities (BFF as data
collector vs presenter) and made the FE depend on the host's hermes home
being mounted, plus it forced full-tree filesystem walks per refresh.

This module makes the SQLite DB the single source of truth: it scans those
JSON files and UPSERTs one row per session into ``hermes_agent_sessions``,
extracting:

* started_at / ended_at — ``session_start`` / ``last_updated`` ISO strings
* model                 — top-level ``model`` field
* turn_count            — ``message_count``
* first_message,
  last_message         — the first/last ``user`` message text, truncated 240ch
* cwd                   — parsed from the ``Current working directory:`` line
                          inside the system prompt (Hermes stamps it there)
* total_cost_usd       — null today (Hermes JSON doesn't record cost yet)
* transcript_path /
  transcript_size /
  transcript_mtime     — file metadata so the FE's deep-link still works

It ALSO enriches ``hermes_sessions`` (the claude-code session table) by
back-filling ``first_message`` / ``last_message`` / ``cwd`` / ``model`` /
``turn_count`` / ``started_at`` / ``ended_at`` / ``total_cost_usd`` for each
claude session that links to a hermes-agent session via
``hermes_runs.hermes_session_id`` — so individual claude sessions carry the
parent agent's metadata as searchable columns.

Migration is backward-compatible: every column add is guarded by
``PRAGMA table_info``, every table create is ``CREATE TABLE IF NOT EXISTS``.

The dashboard BFF spawns one daemon thread that runs ``sync_once()`` every
60 seconds; the CLI exposes ``workerctl sessions sync-hermes`` for ad-hoc
manual refreshes (also used by tests).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker_control.db import connect, utcnow_iso
from worker_control.hermes_profiles import discover_hermes_profiles


# Columns we add to the existing hermes_sessions table to enrich each claude
# session with metadata inherited from its parent hermes-agent session. These
# are populated ONLY when hermes_runs.hermes_session_id links a claude row to
# a hermes-agent session — claude sessions without such a link keep these
# columns NULL.
_HERMES_SESSIONS_ENRICH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("started_at",     "TEXT"),
    ("ended_at_synced","TEXT"),    # parent hermes session's last_updated
    ("turn_count",     "INTEGER"),
    ("first_message",  "TEXT"),
    ("last_message",   "TEXT"),
    ("cwd",            "TEXT"),
    ("total_cost_usd", "REAL"),
    ("hermes_model",   "TEXT"),    # parent's model (claude session has its own already)
)

# Native hermes_sessions already has 'ended_at' (claude session lifecycle end)
# — DO NOT clobber it. We store the parent hermes-agent session's
# last_updated under a separate `ended_at_synced` column to avoid the name
# collision. The dashboard / sync code uses ended_at_synced for the agent
# session, ended_at for the claude session.

_HERMES_AGENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hermes_agent_sessions (
    hermes_session_id TEXT PRIMARY KEY,
    profile_name      TEXT,
    profile_path      TEXT,
    transcript_path   TEXT,
    transcript_size   INTEGER NOT NULL DEFAULT 0,
    transcript_mtime  TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    model             TEXT,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    first_message     TEXT,
    last_message      TEXT,
    cwd               TEXT,
    total_cost_usd    REAL,
    synced_at         TEXT NOT NULL
)
"""

_HERMES_AGENT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_hermes_agent_sessions_mtime "
    "ON hermes_agent_sessions(transcript_mtime DESC)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply the backward-compatible schema for hermes-session enrichment.

    * Adds enrichment columns onto ``hermes_sessions`` if missing
      (ALTER TABLE ADD COLUMN guarded by PRAGMA table_info).
    * Creates ``hermes_agent_sessions`` table if missing.

    Safe to call repeatedly. Tolerates missing ``hermes_sessions`` (returns
    without doing anything in that branch — the canonical DB always has it,
    but tests may bootstrap on a fresh DB).
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hermes_sessions'"
    ).fetchone():
        conn.execute(_HERMES_AGENT_TABLE_SQL)
        for sql in _HERMES_AGENT_INDEX_SQL:
            conn.execute(sql)
        return

    existing = {r["name"] for r in conn.execute("PRAGMA table_info(hermes_sessions)")}
    for col, type_ in _HERMES_SESSIONS_ENRICH_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE hermes_sessions ADD COLUMN {col} {type_}")

    conn.execute(_HERMES_AGENT_TABLE_SQL)
    for sql in _HERMES_AGENT_INDEX_SQL:
        conn.execute(sql)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HermesAgentRow:
    hermes_session_id: str
    profile_name: str | None
    profile_path: str | None
    transcript_path: str
    transcript_size: int
    transcript_mtime: str
    started_at: str | None
    ended_at: str | None
    model: str | None
    turn_count: int
    first_message: str | None
    last_message: str | None
    cwd: str | None
    total_cost_usd: float | None


_CWD_RE = re.compile(r"^Current working directory:\s*(.+?)\s*$", re.MULTILINE)
_MSG_MAX = 240


def _iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return s[:_MSG_MAX]


def _extract_message_text(msg: Any) -> str | None:
    """Pull a plain-text body out of a Hermes message record.

    Hermes message ``content`` is sometimes a string, sometimes a list of
    parts (``[{"type": "text", "text": "…"}, …]``). We accept either.
    """
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    out.append(t)
            elif isinstance(part, str):
                out.append(part)
        if out:
            return " ".join(out)
    return None


def parse_session_file(path: Path, profile_name: str | None,
                       profile_path: str | None) -> HermesAgentRow | None:
    """Parse one session_*.json into a HermesAgentRow (best-effort).

    Returns ``None`` if the file is unreadable / not JSON / missing the
    minimum ``session_id`` field. Never raises.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    sid = data.get("session_id") or path.stem[len("session_"):] or None
    if not sid:
        return None

    messages = data.get("messages")
    if not isinstance(messages, list):
        messages = []

    first_user: str | None = None
    last_user: str | None  = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        text_body = _extract_message_text(m)
        if not text_body:
            continue
        if first_user is None:
            first_user = text_body
        last_user = text_body

    system_prompt = data.get("system_prompt") or ""
    cwd: str | None = None
    if isinstance(system_prompt, str):
        m = _CWD_RE.search(system_prompt)
        if m:
            cwd = m.group(1).strip()

    turn_count = data.get("message_count")
    if not isinstance(turn_count, int):
        turn_count = len(messages)

    # total_cost_usd: not stored by Hermes today; keep schema column in case
    # a future Hermes version starts emitting one. Accept either top-level
    # ``total_cost_usd`` or ``usage.total_cost_usd``.
    cost: float | None = None
    raw_cost = data.get("total_cost_usd")
    if isinstance(raw_cost, (int, float)):
        cost = float(raw_cost)
    else:
        usage = data.get("usage")
        if isinstance(usage, dict):
            v = usage.get("total_cost_usd")
            if isinstance(v, (int, float)):
                cost = float(v)

    return HermesAgentRow(
        hermes_session_id=str(sid),
        profile_name=profile_name,
        profile_path=profile_path,
        transcript_path=str(path),
        transcript_size=st.st_size,
        transcript_mtime=_iso_from_mtime(st.st_mtime),
        started_at=data.get("session_start") if isinstance(data.get("session_start"), str) else None,
        ended_at=data.get("last_updated") if isinstance(data.get("last_updated"), str) else None,
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        turn_count=int(turn_count),
        first_message=_truncate(first_user),
        last_message=_truncate(last_user),
        cwd=cwd,
        total_cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

_UPSERT_SQL = """
INSERT INTO hermes_agent_sessions(
    hermes_session_id, profile_name, profile_path,
    transcript_path, transcript_size, transcript_mtime,
    started_at, ended_at, model, turn_count,
    first_message, last_message, cwd, total_cost_usd, synced_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(hermes_session_id) DO UPDATE SET
    profile_name      = excluded.profile_name,
    profile_path      = excluded.profile_path,
    transcript_path   = excluded.transcript_path,
    transcript_size   = excluded.transcript_size,
    transcript_mtime  = excluded.transcript_mtime,
    started_at        = COALESCE(excluded.started_at,    hermes_agent_sessions.started_at),
    ended_at          = COALESCE(excluded.ended_at,      hermes_agent_sessions.ended_at),
    model             = COALESCE(excluded.model,         hermes_agent_sessions.model),
    turn_count        = excluded.turn_count,
    first_message     = COALESCE(excluded.first_message, hermes_agent_sessions.first_message),
    last_message      = excluded.last_message,
    cwd               = COALESCE(excluded.cwd,           hermes_agent_sessions.cwd),
    total_cost_usd    = COALESCE(excluded.total_cost_usd, hermes_agent_sessions.total_cost_usd),
    synced_at         = excluded.synced_at
"""


def upsert_agent_session(conn: sqlite3.Connection, row: HermesAgentRow) -> None:
    conn.execute(_UPSERT_SQL, (
        row.hermes_session_id, row.profile_name, row.profile_path,
        row.transcript_path, row.transcript_size, row.transcript_mtime,
        row.started_at, row.ended_at, row.model, row.turn_count,
        row.first_message, row.last_message, row.cwd, row.total_cost_usd,
        utcnow_iso(),
    ))


def _propagate_to_claude_sessions(conn: sqlite3.Connection) -> int:
    """Back-fill enrichment columns on ``hermes_sessions`` from the parent
    hermes-agent session, joined via ``hermes_runs.hermes_session_id``.

    For each claude session, we pick the FIRST distinct
    ``hermes_runs.hermes_session_id`` we see (sessions normally have one
    parent anyway). Idempotent — re-running just refreshes the columns.
    Returns the number of claude sessions touched.
    """
    sql = """
    UPDATE hermes_sessions
       SET started_at      = COALESCE(agg.started_at,     hermes_sessions.started_at),
           ended_at_synced = COALESCE(agg.ended_at,       hermes_sessions.ended_at_synced),
           turn_count      = COALESCE(agg.turn_count,     hermes_sessions.turn_count),
           first_message   = COALESCE(agg.first_message,  hermes_sessions.first_message),
           last_message    = COALESCE(agg.last_message,   hermes_sessions.last_message),
           cwd             = COALESCE(agg.cwd,            hermes_sessions.cwd),
           total_cost_usd  = COALESCE(agg.total_cost_usd, hermes_sessions.total_cost_usd),
           hermes_model    = COALESCE(agg.model,          hermes_sessions.hermes_model)
      FROM (
          SELECT r.session_id AS sess_id,
                 a.started_at, a.ended_at, a.turn_count,
                 a.first_message, a.last_message,
                 a.cwd, a.total_cost_usd, a.model
            FROM hermes_runs r
            JOIN hermes_agent_sessions a
              ON a.hermes_session_id = r.hermes_session_id
           WHERE r.hermes_session_id IS NOT NULL
           GROUP BY r.session_id
      ) AS agg
     WHERE hermes_sessions.id = agg.sess_id
    """
    cur = conn.execute(sql)
    return cur.rowcount if cur.rowcount is not None else 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SyncResult:
    profiles_scanned:   int
    files_seen:         int
    upserted:           int
    enrichment_updated: int
    skipped:            int
    duration_ms:        int


def sync_once(*, db: Path | None = None,
              profiles_filter: tuple[str, ...] | None = None) -> SyncResult:
    """Scan every Hermes profile's sessions/*.json and upsert into the DB.

    Parameters
    ----------
    db:
        Optional DB path override (mostly for tests).
    profiles_filter:
        Optional profile-name allow-list. Empty/None = all profiles.
    """
    t0 = time.monotonic()
    profiles_scanned = 0
    files_seen = 0
    upserted = 0
    skipped = 0

    profs = discover_hermes_profiles()
    if profiles_filter:
        wanted = set(profiles_filter)
        profs = [p for p in profs if p.name in wanted]

    with connect(db) as conn:
        ensure_schema(conn)
        for prof in profs:
            sessions_dir = Path(prof.path) / "sessions"
            if not sessions_dir.is_dir():
                continue
            profiles_scanned += 1
            for jf in sessions_dir.glob("session_*.json"):
                files_seen += 1
                row = parse_session_file(jf, prof.name, prof.path)
                if row is None:
                    skipped += 1
                    continue
                upsert_agent_session(conn, row)
                upserted += 1
        enrichment_updated = _propagate_to_claude_sessions(conn)

    return SyncResult(
        profiles_scanned=profiles_scanned,
        files_seen=files_seen,
        upserted=upserted,
        enrichment_updated=enrichment_updated,
        skipped=skipped,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Background refresher (used by the BFF server)
# ---------------------------------------------------------------------------

class PeriodicSyncWorker:
    """Daemon thread that calls ``sync_once`` every ``interval_s`` seconds.

    Use ``start()`` once on dashboard server boot; ``stop()`` on shutdown
    (the thread is a daemon so process exit reaps it regardless).

    The first sync runs immediately on start so the FE sees populated data
    on the first ``/api/snapshot`` after boot rather than after one cycle's
    wait. Errors are swallowed and logged via ``on_error`` (default: silent)
    — we never want a hiccup here to take down the BFF.
    """

    def __init__(self, *, interval_s: float = 60.0,
                 on_event=None, on_error=None,
                 db: Path | None = None) -> None:
        self.interval_s = float(interval_s)
        self.on_event = on_event
        self.on_error = on_error
        self._db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hermes-session-sync", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                res = sync_once(db=self._db)
                if self.on_event:
                    self.on_event(res)
            except Exception as exc:
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass
            # Wait with early-exit on stop signal
            self._stop.wait(self.interval_s)
