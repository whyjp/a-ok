"""Schema migration is idempotent + adds all expected columns/tables."""
from __future__ import annotations

import sqlite3

from worker_control_hermes.legacy_parity_schema import (
    CHILD_TABLES,
    apply_legacy_parity_schema,
    replace_child_rows,
    upsert_session_row,
)


_EXPECTED_COLUMNS = {
    "kind", "git_branch", "claude_version",
    "msg_user", "msg_assistant", "msg_tool",
    "ai_title", "summary",
    "first_user_text", "last_user_text", "last_assistant_text",
    "size_bytes",
    "spawn_slug", "spawn_reason", "is_spawned",
    "effective_status",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_first_apply_adds_columns_and_tables() -> None:
    conn = sqlite3.connect(":memory:")
    audit = apply_legacy_parity_schema(conn)

    cols = _table_columns(conn, "hermes_agent_sessions")
    assert _EXPECTED_COLUMNS <= cols

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for ct in CHILD_TABLES:
        assert ct in tables

    # audit reports what was added
    assert _EXPECTED_COLUMNS <= set(audit["columns_added"])
    assert set(audit["tables_added"]) >= set(CHILD_TABLES)


def test_reapply_is_noop() -> None:
    conn = sqlite3.connect(":memory:")
    apply_legacy_parity_schema(conn)
    audit = apply_legacy_parity_schema(conn)
    assert audit == {"columns_added": [], "tables_added": []}


def test_upsert_session_row_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    apply_legacy_parity_schema(conn)
    upsert_session_row(conn, {
        "hermes_session_id": "abc-123",
        "kind": "claude",
        "synced_at": "2026-05-18T00:00:00Z",
        "msg_user": 5, "msg_assistant": 7, "msg_tool": 2,
        "ai_title": "First call",
        "is_spawned": 0,
        "effective_status": "active",
    })
    upsert_session_row(conn, {
        "hermes_session_id": "abc-123",
        "kind": "claude",
        "synced_at": "2026-05-18T01:00:00Z",
        "msg_user": 6,
        "ai_title": "Second call",
    })
    row = conn.execute(
        "SELECT msg_user, msg_assistant, ai_title FROM hermes_agent_sessions "
        "WHERE hermes_session_id='abc-123'"
    ).fetchone()
    assert row == (6, 7, "Second call")


def test_replace_child_rows_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_legacy_parity_schema(conn)
    rows1 = [{"url": "https://github.com/x/y/pull/1", "num": 1, "repo": "x/y", "kind": "github"}]
    replace_child_rows(conn, "session_pr_links", "u1", rows1)
    replace_child_rows(conn, "session_pr_links", "u1", rows1)   # same set
    assert conn.execute(
        "SELECT COUNT(*) FROM session_pr_links WHERE session_uuid='u1'"
    ).fetchone()[0] == 1

    # Replacement to empty set deletes everything.
    replace_child_rows(conn, "session_pr_links", "u1", [])
    assert conn.execute(
        "SELECT COUNT(*) FROM session_pr_links WHERE session_uuid='u1'"
    ).fetchone()[0] == 0
