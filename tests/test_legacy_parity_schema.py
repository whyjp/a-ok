"""Schema migration is idempotent + adds all expected columns/tables."""
from __future__ import annotations

import sqlite3

from worker_control_hermes.legacy_parity_schema import (
    CHILD_TABLES,
    apply_legacy_parity_schema,
    replace_child_rows,
    upsert_claude_parity_row,
    upsert_session_row,
)
from worker_control_hermes.migrations._2026_split_claude_parity import migrate


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


def test_claude_session_parity_table_created() -> None:
    """apply_legacy_parity_schema must idempotently create claude_session_parity."""
    conn = sqlite3.connect(":memory:")
    audit = apply_legacy_parity_schema(conn)
    assert "claude_session_parity" in audit["tables_added"]
    cols = _table_columns(conn, "claude_session_parity")
    # Sanity: PK + a few of the extra columns
    assert "session_uuid" in cols
    for c in ("kind", "git_branch", "msg_user", "ai_title",
              "spawn_slug", "is_spawned", "effective_status"):
        assert c in cols


def test_upsert_claude_parity_row_renames_pk() -> None:
    """upsert_claude_parity_row accepts the parser's hermes_session_id dict
    shape and stores it under session_uuid."""
    conn = sqlite3.connect(":memory:")
    apply_legacy_parity_schema(conn)
    upsert_claude_parity_row(conn, {
        "hermes_session_id": "abcdef12-3456-7890-abcd-ef0123456789",
        "kind": "claude",
        "profile_name": "ignored",   # gets dropped silently
        "profile_path": "ignored",
        "synced_at": "2026-05-18T00:00:00Z",
        "msg_user": 3,
        "ai_title": "row",
    })
    row = conn.execute(
        "SELECT session_uuid, kind, msg_user, ai_title "
        "FROM claude_session_parity"
    ).fetchone()
    assert row == (
        "abcdef12-3456-7890-abcd-ef0123456789", "claude", 3, "row",
    )


def test_split_claude_parity_migration_moves_rows() -> None:
    """The split migration moves ~/.claude/projects rows into the new table
    and deletes them from hermes_agent_sessions; hermes-profile rows stay."""
    conn = sqlite3.connect(":memory:")
    apply_legacy_parity_schema(conn)
    # A native claude row (should move).
    upsert_session_row(conn, {
        "hermes_session_id": "11111111-1111-1111-1111-111111111111",
        "kind": "claude",
        "transcript_path": r"C:\Users\me\.claude\projects\D--proj\11111111-1111-1111-1111-111111111111.jsonl",
        "msg_user": 9,
        "synced_at": "2026-05-18T00:00:00Z",
    })
    # A hermes-profile row (should NOT move).
    upsert_session_row(conn, {
        "hermes_session_id": "session_hostA_123_abc",
        "kind": "hermes",
        "profile_name": "worker",
        "transcript_path": r"C:\Users\me\AppData\Local\hermes\profiles\worker\sessions\session_hostA_123_abc.json",
        "msg_user": 2,
        "synced_at": "2026-05-18T00:00:00Z",
    })

    stats = migrate(conn)
    assert stats["moved"] == 1
    assert stats["deleted"] == 1

    # Native row moved.
    assert conn.execute(
        "SELECT COUNT(*) FROM hermes_agent_sessions WHERE hermes_session_id=?",
        ("11111111-1111-1111-1111-111111111111",),
    ).fetchone()[0] == 0
    moved = conn.execute(
        "SELECT msg_user FROM claude_session_parity WHERE session_uuid=?",
        ("11111111-1111-1111-1111-111111111111",),
    ).fetchone()
    assert moved == (9,)

    # Hermes-profile row stayed put.
    stayed = conn.execute(
        "SELECT msg_user FROM hermes_agent_sessions WHERE hermes_session_id=?",
        ("session_hostA_123_abc",),
    ).fetchone()
    assert stayed == (2,)
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_session_parity WHERE session_uuid=?",
        ("session_hostA_123_abc",),
    ).fetchone()[0] == 0

    # Idempotent: re-running is a no-op.
    stats2 = migrate(conn)
    assert stats2["moved"] == 0


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
