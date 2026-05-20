"""GENERATED VIRTUAL ``mention_id`` column auto-computes on read.

Covers both tables that carry the short copy/lookup id:

* ``hermes_sessions.mention_id`` — ``aok#<uuid8>`` for spawned rows,
  ``nat#<uuid8>`` otherwise. Created by ``_apply_extra_schema``.
* ``claude_session_parity.mention_id`` — always ``nat#<uuid8>`` (every
  parity row is a native ``.claude/projects/`` transcript). Created by
  ``apply_legacy_parity_schema``.

The column is GENERATED VIRTUAL, so existing rows pick up the value on
read without any backfill — that's the point of the forward-only fix.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from worker_control.db import init_db
from worker_control.hermes_install import _apply_extra_schema
from worker_control_hermes.legacy_parity_schema import apply_legacy_parity_schema


def _bootstrap(tmp_path: Path) -> Path:
    db = init_db()
    _apply_extra_schema(db)
    conn = sqlite3.connect(db)
    try:
        apply_legacy_parity_schema(conn)
    finally:
        conn.close()
    return db


def _insert_project(conn: sqlite3.Connection, name: str = "a-ok") -> int:
    conn.execute(
        "INSERT INTO projects(name, path, root_role, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, f"D:/{name}", "owned_work",
         "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_session(
    conn: sqlite3.Connection, *,
    uuid: str, origin: str, project_id: int,
    name: str = "session-name",
) -> None:
    conn.execute(
        "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, "
        "created_at, last_used_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
        (uuid, project_id, name, origin,
         "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
    )


def test_hermes_sessions_mention_id_spawned(tmp_path: Path) -> None:
    db = _bootstrap(tmp_path)
    conn = sqlite3.connect(db)
    try:
        pid = _insert_project(conn)
        _insert_session(
            conn, uuid="abcdef12-1111-2222-3333-444455556666",
            origin="spawned", project_id=pid,
        )
        row = conn.execute(
            "SELECT mention_id FROM hermes_sessions "
            "WHERE uuid='abcdef12-1111-2222-3333-444455556666'"
        ).fetchone()
        assert row[0] == "aok#abcdef12"
    finally:
        conn.close()


def test_hermes_sessions_mention_id_native(tmp_path: Path) -> None:
    db = _bootstrap(tmp_path)
    conn = sqlite3.connect(db)
    try:
        pid = _insert_project(conn)
        _insert_session(
            conn, uuid="12345678-aaaa-bbbb-cccc-ddddeeeeffff",
            origin="native", project_id=pid,
        )
        row = conn.execute(
            "SELECT mention_id FROM hermes_sessions "
            "WHERE uuid='12345678-aaaa-bbbb-cccc-ddddeeeeffff'"
        ).fetchone()
        assert row[0] == "nat#12345678"
    finally:
        conn.close()


def test_claude_session_parity_mention_id(tmp_path: Path) -> None:
    db = _bootstrap(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO claude_session_parity(session_uuid, synced_at) "
            "VALUES (?, ?)",
            ("deadbeef-cafe-1234-5678-9abcdef01234", "2026-05-19T00:00:00Z"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT mention_id FROM claude_session_parity "
            "WHERE session_uuid='deadbeef-cafe-1234-5678-9abcdef01234'"
        ).fetchone()
        assert row[0] == "nat#deadbeef"
    finally:
        conn.close()


def test_schema_apply_is_idempotent(tmp_path: Path) -> None:
    """Running both apply functions twice leaves the column in place,
    raises nothing, and the GENERATED expression still computes."""
    db = _bootstrap(tmp_path)
    # Second pass.
    _apply_extra_schema(db)
    conn = sqlite3.connect(db)
    try:
        apply_legacy_parity_schema(conn)
        # The column must still be present + indexed. Use table_xinfo —
        # table_info omits GENERATED columns.
        h_cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(hermes_sessions)")}
        p_cols = {r[1] for r in conn.execute(
            "PRAGMA table_xinfo(claude_session_parity)"
        )}
        assert "mention_id" in h_cols
        assert "mention_id" in p_cols

        # Both indexes exist.
        idx_names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        assert "ix_hermes_sessions_mention_id" in idx_names
        assert "ix_claude_session_parity_mention_id" in idx_names

        # And the expression still computes correctly after re-apply.
        pid = _insert_project(conn, name="b-project")
        _insert_session(
            conn, uuid="11112222-3333-4444-5555-666677778888",
            origin="spawned", project_id=pid,
        )
        row = conn.execute(
            "SELECT mention_id FROM hermes_sessions "
            "WHERE uuid='11112222-3333-4444-5555-666677778888'"
        ).fetchone()
        assert row[0] == "aok#11112222"
    finally:
        conn.close()
