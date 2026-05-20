"""Regression test for the ``mention_id`` GENERATED-column migration.

The earlier shape of ``_apply_extra_schema`` / ``apply_legacy_parity_schema``
emitted ``CREATE INDEX ... ON <tbl>(mention_id)`` inline in the same
``executescript`` block that declared the ``CREATE TABLE``. On pre-PR DBs
where the table already existed *without* the GENERATED ``mention_id``
column, the CREATE TABLE was a no-op but the CREATE INDEX aborted the
whole executescript with ``OperationalError: no such column: mention_id``.
The forward-only ALTER fallback farther down the function never ran, so
heartbeat-driven parity ingestion crashed on every existing host.

This file pins the fix:

  A) Fresh DB → column + index both present, no error.
  B) Legacy DB (tables exist *without* mention_id) → migration adds the
     column AND the index without raising. This is the bug scenario.
  C) Migration is idempotent on a legacy DB (calling it twice is a no-op
     after the first run, both runs succeed).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from worker_control.db import init_db
from worker_control.hermes_install import _apply_extra_schema
from worker_control_hermes.legacy_parity_schema import apply_legacy_parity_schema


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Pre-PR shape of hermes_sessions — identical to the live schema modulo
# the missing ``mention_id`` GENERATED column + its index.
_LEGACY_HERMES_SESSIONS_SQL = """
CREATE TABLE hermes_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    origin          TEXT NOT NULL DEFAULT 'native',
    model           TEXT,
    permission_mode TEXT,
    brief           TEXT,
    notes           TEXT DEFAULT '',
    claude_name     TEXT,
    claude_status   TEXT,
    claude_status_at TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL,
    ended_at        TEXT
);
CREATE INDEX ix_hermes_sessions_project    ON hermes_sessions(project_id);
CREATE INDEX ix_hermes_sessions_status     ON hermes_sessions(status);
CREATE INDEX ix_hermes_sessions_origin     ON hermes_sessions(origin);
CREATE INDEX ix_hermes_sessions_last_used  ON hermes_sessions(last_used_at DESC);
"""

# Pre-PR shape of claude_session_parity — same shape as the live one but
# with the GENERATED ``mention_id`` column and its index removed.
_LEGACY_CLAUDE_PARITY_SQL = """
CREATE TABLE claude_session_parity (
    session_uuid      TEXT PRIMARY KEY,
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
    synced_at         TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'claude',
    git_branch        TEXT,
    claude_version    TEXT,
    msg_user          INTEGER NOT NULL DEFAULT 0,
    msg_assistant     INTEGER NOT NULL DEFAULT 0,
    msg_tool          INTEGER NOT NULL DEFAULT 0,
    ai_title          TEXT,
    summary           TEXT,
    first_user_text   TEXT,
    last_user_text    TEXT,
    last_assistant_text TEXT,
    size_bytes        INTEGER,
    spawn_slug        TEXT,
    spawn_reason      TEXT,
    is_spawned        INTEGER NOT NULL DEFAULT 0,
    effective_status  TEXT
);
CREATE INDEX ix_claude_session_parity_mtime
    ON claude_session_parity(transcript_mtime DESC);
"""


def _xinfo_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    """table_xinfo includes GENERATED columns (table_info does not)."""
    return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    )}


def _insert_project(conn: sqlite3.Connection, name: str = "a-ok") -> int:
    conn.execute(
        "INSERT INTO projects(name, path, root_role, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, f"D:/{name}", "owned_work",
         "2026-05-20T00:00:00Z", "2026-05-20T00:00:00Z"),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


# ---------------------------------------------------------------------------
# A) fresh DB
# ---------------------------------------------------------------------------

def test_fresh_db_gets_column_and_index(tmp_path: Path) -> None:
    """Bootstrapping from scratch lands both the GENERATED column AND the
    index. (Index used to live inline in the CREATE TABLE executescript,
    which silently dropped it on the legacy path — keep coverage of the
    fresh path too so we don't regress in the other direction.)"""
    db = init_db()
    _apply_extra_schema(db)
    conn = sqlite3.connect(db)
    try:
        apply_legacy_parity_schema(conn)

        assert "mention_id" in _xinfo_cols(conn, "hermes_sessions")
        assert "mention_id" in _xinfo_cols(conn, "claude_session_parity")

        idx = _index_names(conn)
        assert "ix_hermes_sessions_mention_id" in idx
        assert "ix_claude_session_parity_mention_id" in idx

        # GENERATED expressions still compute end-to-end on a fresh DB.
        pid = _insert_project(conn)
        conn.execute(
            "INSERT INTO hermes_sessions(uuid, project_id, name, status, "
            "origin, created_at, last_used_at) "
            "VALUES (?, ?, 'fresh', 'active', 'spawned', ?, ?)",
            ("aaaabbbb-1111-2222-3333-444455556666", pid,
             "2026-05-20T00:00:00Z", "2026-05-20T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO claude_session_parity(session_uuid, synced_at) "
            "VALUES ('ccccdddd-1111-2222-3333-444455556666', '2026-05-20T00:00:00Z')"
        )
        conn.commit()

        h = conn.execute(
            "SELECT mention_id FROM hermes_sessions WHERE uuid = ?",
            ("aaaabbbb-1111-2222-3333-444455556666",),
        ).fetchone()
        p = conn.execute(
            "SELECT mention_id FROM claude_session_parity "
            "WHERE session_uuid = 'ccccdddd-1111-2222-3333-444455556666'"
        ).fetchone()
        assert h[0] == "aok#aaaabbbb"
        assert p[0] == "nat#ccccdddd"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# B) legacy DB — the original bug scenario
# ---------------------------------------------------------------------------

def _seed_legacy_hermes_sessions(db: Path) -> None:
    """Simulate a DB whose hermes_sessions table predates the GENERATED
    column. ``init_db`` has already created ``projects``; we manually drop
    the future-state hermes_sessions (in case _apply_extra_schema or other
    bootstrap touched it) and re-create the legacy shape."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE IF EXISTS hermes_sessions")
        conn.executescript(_LEGACY_HERMES_SESSIONS_SQL)
        conn.commit()
    finally:
        conn.close()


def _seed_legacy_claude_parity(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS claude_session_parity")
    conn.executescript(_LEGACY_CLAUDE_PARITY_SQL)
    conn.commit()


def test_legacy_db_migration_adds_mention_id_without_error(tmp_path: Path) -> None:
    """The bug: pre-PR DB has the tables but not the GENERATED column.
    The old executescript would crash on ``CREATE INDEX ... mention_id``
    before the ALTER fallback could run. After the fix, both
    ``_apply_extra_schema`` and ``apply_legacy_parity_schema`` quietly
    backfill the column AND the index."""
    db = init_db()

    # 1. Build the legacy hermes_sessions table (no mention_id).
    _seed_legacy_hermes_sessions(db)
    # 2. Build the legacy claude_session_parity table (no mention_id).
    #    We do this on a short-lived connection so the migration runs on
    #    a fresh one, mirroring how the heartbeat actually invokes it.
    conn = sqlite3.connect(str(db))
    try:
        _seed_legacy_claude_parity(conn)
    finally:
        conn.close()

    # Sanity: the legacy DB really is missing the column on both tables.
    conn = sqlite3.connect(str(db))
    try:
        assert "mention_id" not in _xinfo_cols(conn, "hermes_sessions")
        assert "mention_id" not in _xinfo_cols(conn, "claude_session_parity")
    finally:
        conn.close()

    # 3. Run the actual migration — must NOT raise OperationalError.
    _apply_extra_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        apply_legacy_parity_schema(conn)

        # Column present on both tables now.
        assert "mention_id" in _xinfo_cols(conn, "hermes_sessions")
        assert "mention_id" in _xinfo_cols(conn, "claude_session_parity")

        # Both indexes present.
        idx = _index_names(conn)
        assert "ix_hermes_sessions_mention_id" in idx
        assert "ix_claude_session_parity_mention_id" in idx

        # And the GENERATED expression computes on rows inserted via the
        # legacy-shape table (proving the ALTER actually wired the
        # generation expression, not just an empty column).
        pid = _insert_project(conn, name="legacy-proj")
        conn.execute(
            "INSERT INTO hermes_sessions(uuid, project_id, name, status, "
            "origin, created_at, last_used_at) "
            "VALUES (?, ?, 'leg', 'active', 'native', ?, ?)",
            ("12345678-aaaa-bbbb-cccc-ddddeeeeffff", pid,
             "2026-05-20T00:00:00Z", "2026-05-20T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO claude_session_parity(session_uuid, synced_at) "
            "VALUES ('deadbeef-cafe-1234-5678-9abcdef01234', '2026-05-20T00:00:00Z')"
        )
        conn.commit()

        h = conn.execute(
            "SELECT mention_id FROM hermes_sessions "
            "WHERE uuid = '12345678-aaaa-bbbb-cccc-ddddeeeeffff'"
        ).fetchone()
        p = conn.execute(
            "SELECT mention_id FROM claude_session_parity "
            "WHERE session_uuid = 'deadbeef-cafe-1234-5678-9abcdef01234'"
        ).fetchone()
        assert h[0] == "nat#12345678"
        assert p[0] == "nat#deadbeef"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# C) idempotency over the legacy path
# ---------------------------------------------------------------------------

def test_legacy_db_migration_is_idempotent(tmp_path: Path) -> None:
    """Calling the migration twice on a legacy DB must not raise. The
    first pass should backfill the column + index; the second is a no-op
    guarded by table_xinfo / CREATE INDEX IF NOT EXISTS."""
    db = init_db()
    _seed_legacy_hermes_sessions(db)
    conn = sqlite3.connect(str(db))
    try:
        _seed_legacy_claude_parity(conn)
    finally:
        conn.close()

    # First pass — does the backfill.
    _apply_extra_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        apply_legacy_parity_schema(conn)
    finally:
        conn.close()

    # Second pass — must be a no-op. (No try/except; if either call
    # raises, pytest will flag it.)
    _apply_extra_schema(db)
    conn = sqlite3.connect(str(db))
    try:
        apply_legacy_parity_schema(conn)
        # Still in good shape.
        assert "mention_id" in _xinfo_cols(conn, "hermes_sessions")
        assert "mention_id" in _xinfo_cols(conn, "claude_session_parity")
        idx = _index_names(conn)
        assert "ix_hermes_sessions_mention_id" in idx
        assert "ix_claude_session_parity_mention_id" in idx
    finally:
        conn.close()
