"""End-to-end: ingest_all takes synthetic transcripts, writes to DB,
then load_session_payload returns the same shape the legacy DATA[] uses.

We monkeypatch the source directories so we don't touch the host's real
~/.claude/projects or hermes profile dirs.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from worker_control_hermes import legacy_parity_ingest as ingest_mod
from worker_control_hermes.legacy_parity_ingest import ingest_all
from worker_control_hermes.legacy_parity_report import load_session_payload
from worker_control_hermes.legacy_parity_schema import apply_legacy_parity_schema


def _fake_jsonl(path: Path) -> None:
    lines = [
        {"type": "user", "timestamp": "2026-05-18T10:00:00Z",
         "message": {"content": "hi"}, "cwd": "D:/some/proj",
         "gitBranch": "feat/x", "version": "2.1.140"},
        {"type": "assistant", "timestamp": "2026-05-18T10:00:01Z",
         "message": {"content": [
             {"type": "text", "text": "ok https://github.com/a/b/pull/3"},
             {"type": "tool_use", "name": "Read",
              "input": {"file_path": "D:/some/proj/main.py"}},
         ]}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for o in lines:
            fh.write(json.dumps(o) + "\n")


def test_ingest_all_populates_canonical_db(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)

    # Build a fake claude projects dir with one jsonl.
    fake_claude = tmp_path / "claude_projects"
    proj = fake_claude / "D--some-proj"
    proj.mkdir(parents=True)
    jl = proj / "abcdef12-3456-7890-abcd-ef0123456789.jsonl"
    _fake_jsonl(jl)

    fake_hermes = tmp_path / "hermes_profiles"
    fake_hermes.mkdir(parents=True)

    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR", fake_claude)
    monkeypatch.setattr(ingest_mod, "HERMES_HOME_DIR", fake_hermes)

    stats = ingest_all(conn, force=True)
    assert stats["claude_scanned"] == 1
    assert stats["claude_updated"] == 1

    # Claude rows live in claude_session_parity post-split — NOT in
    # hermes_agent_sessions (which is reserved for Hermes-profile sources).
    row = conn.execute(
        "SELECT msg_user, msg_assistant, msg_tool, ai_title, git_branch, "
        "claude_version, is_spawned, effective_status "
        "FROM claude_session_parity WHERE session_uuid=?",
        (jl.stem,)
    ).fetchone()
    assert row == (1, 1, 1, None, "feat/x", "2.1.140", 0, "active")

    # And the Hermes-only table must be empty for this native claude row.
    assert conn.execute(
        "SELECT COUNT(*) FROM hermes_agent_sessions WHERE hermes_session_id=?",
        (jl.stem,)
    ).fetchone()[0] == 0

    # Child tables populated (shared between both sources).
    assert conn.execute(
        "SELECT COUNT(*) FROM session_pr_links WHERE session_uuid=?",
        (jl.stem,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM session_files_touched WHERE session_uuid=?",
        (jl.stem,)
    ).fetchone()[0] == 1

    # Watermark skip: second run should report 0 updated.
    stats2 = ingest_all(conn)
    assert stats2["claude_updated"] == 0
    assert stats2["skipped"] >= 1


def test_ingest_all_auto_runs_split_migration(tmp_path: Path, monkeypatch) -> None:
    """A heartbeat tick on a legacy DB (claude rows in hermes_agent_sessions)
    moves them into claude_session_parity without an explicit migrate call.

    Regression guard for the auto-apply hook added to ``ingest_all``.
    """
    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)
    apply_legacy_parity_schema(conn)

    # Seed a legacy-shaped row that lived in hermes_agent_sessions before the
    # split: transcript_path under ~/.claude/projects/...
    uid = "11111111-2222-3333-4444-555555555555"
    conn.execute(
        "INSERT INTO hermes_agent_sessions"
        "(hermes_session_id, transcript_path, transcript_mtime, synced_at, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, "C:/Users/x/.claude/projects/Foo/" + uid + ".jsonl",
         "2026-05-18T00:00:00+00:00", "2026-05-18T00:00:00+00:00", "native"),
    )
    conn.commit()

    # Point ingest at empty source dirs so it doesn't try to scan real disk.
    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR", tmp_path / "no_claude")
    monkeypatch.setattr(ingest_mod, "HERMES_HOME_DIR",   tmp_path / "no_hermes")

    ingest_all(conn)

    # The legacy row must now live in claude_session_parity, not hermes_agent_sessions.
    assert conn.execute(
        "SELECT COUNT(*) FROM hermes_agent_sessions WHERE hermes_session_id=?", (uid,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM claude_session_parity WHERE session_uuid=?", (uid,)
    ).fetchone()[0] == 1


def test_load_session_payload_matches_legacy_keys(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)

    fake_claude = tmp_path / "claude_projects"
    proj = fake_claude / "D--some-proj"
    proj.mkdir(parents=True)
    jl = proj / "deadbeef-cafe-babe-c0de-1234567890ab.jsonl"
    _fake_jsonl(jl)

    monkeypatch.setattr(ingest_mod, "CLAUDE_PROJECTS_DIR", fake_claude)
    monkeypatch.setattr(ingest_mod, "HERMES_HOME_DIR", tmp_path / "no_hermes")

    ingest_all(conn, force=True)

    payload = load_session_payload(conn)
    assert len(payload) == 1
    s = payload[0]
    # Legacy DATA[] required keys
    for k in ("session_id", "origin", "cwd", "git_branch", "version",
              "msg_user", "msg_assistant", "msg_tool",
              "pr_links", "files_touched", "tools_recent",
              "recap_native", "pending_queue", "effective_status",
              "spawn_slug", "is_spawned"):
        assert k in s, f"missing payload key: {k}"
