"""SQLite schema and connection helper.

Uses only the standard library. Connection has Row factory and foreign keys on.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from worker_control.paths import db_path, ensure_runtime_dirs

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS worker_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    root_path       TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    path            TEXT NOT NULL,
    is_git          INTEGER NOT NULL DEFAULT 0,
    branch          TEXT,
    remote_url      TEXT,
    is_dirty        INTEGER NOT NULL DEFAULT 0,
    -- 워크스페이스 역할: owned_work | public_reference | other
    root_role       TEXT NOT NULL DEFAULT 'other',
    last_scan_at    TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    profile_id      INTEGER NOT NULL REFERENCES worker_profiles(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL REFERENCES projects(id)        ON DELETE CASCADE,
    state           TEXT NOT NULL,
    runtime         TEXT NOT NULL,           -- 'tmux' | 'console' | 'pending'
    tmux_session    TEXT,
    pid             INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES worker_sessions(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,           -- 'state' | 'capture' | 'note' | 'error'
    payload         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_session_events_session_id
    ON session_events(session_id);

CREATE TABLE IF NOT EXISTS worker_commands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES worker_sessions(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    delivery        TEXT NOT NULL,           -- 'tmux' | 'rejected_no_tmux' | 'queued'
    result          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_worker_commands_session_id
    ON worker_commands(session_id);

CREATE TABLE IF NOT EXISTS project_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path       TEXT NOT NULL,
    -- 스캔한 루트의 역할 라벨 (owned_work / public_reference / other).
    root_role       TEXT NOT NULL DEFAULT 'other',
    discovered      INTEGER NOT NULL DEFAULT 0,
    git_repos       INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
"""

# 기존 DB(이전 스키마) 와의 호환을 위한 가벼운 마이그레이션.
# - projects.root_role / project_scans.root_role 컬럼이 없으면 추가한다.
_MIGRATIONS_SQL: tuple[tuple[str, str], ...] = (
    (
        "projects",
        "ALTER TABLE projects ADD COLUMN root_role TEXT NOT NULL DEFAULT 'other'",
    ),
    (
        "project_scans",
        "ALTER TABLE project_scans ADD COLUMN root_role TEXT NOT NULL DEFAULT 'other'",
    ),
)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _apply_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """ALTER TABLE 으로 누락된 root_role 컬럼만 보강. 기존 데이터는 그대로 유지."""
    for table, sql in _MIGRATIONS_SQL:
        col = sql.split("ADD COLUMN", 1)[1].strip().split()[0]
        cols = _column_names(conn, table)
        if col not in cols:
            conn.execute(sql)


def utcnow_iso() -> str:
    """UTC ISO-8601 timestamp with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults; create parent dirs."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: Path | None = None) -> Path:
    """Create schema if missing. Returns the DB path used."""
    ensure_runtime_dirs()
    target = path or db_path()
    with connect(target) as conn:
        conn.executescript(SCHEMA_SQL)
        _apply_lightweight_migrations(conn)
    return target


@contextmanager
def session_scope(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that opens a connection and closes it on exit."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def dump_json(obj: Any) -> str:
    """Stable JSON dump for metadata columns."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return loaded if isinstance(loaded, dict) else {"_value": loaded}
