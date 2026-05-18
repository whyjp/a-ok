"""Tests for ``worker_control.session_sync.sync_all`` (Phase 2 PR #6).

PR #4 / #5 covered the single-row writer and the reader. PR #6 introduces
``sync_all`` — the disk-walking entry point used by both
``workerctl session sync-all`` and the heartbeat tick. The behaviours
that matter to the scheduler / heartbeat caller:

* both disk sources (claude jsonl + hermes profile session_*.json) are
  walked in one call,
* an unchanged jsonl is skipped without re-parsing the file (mtime keyed
  against ``hermes_sessions.last_used_at``), so the heartbeat stays under
  1 s on a populated host,
* calling twice in a row never inserts duplicates and never thrashes
  ``last_used_at`` backward,
* the post-walk ``_reclassify_origins`` is invoked exactly once.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from textwrap import dedent

import pytest

from worker_control import session_sync
from worker_control.session_sync import sync_all


_PROJECTS_TABLE_SQL = dedent(
    """
    CREATE TABLE hermes_projects_v (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        folder_path   TEXT NOT NULL UNIQUE,
        project_type  TEXT,
        git_repo      TEXT,
        display_name  TEXT,
        description   TEXT,
        learned_notes TEXT DEFAULT '',
        created_at    TEXT NOT NULL,
        last_used_at  TEXT NOT NULL,
        use_count     INTEGER NOT NULL DEFAULT 0
    )
    """
).strip()

_SESSIONS_TABLE_SQL = dedent(
    """
    CREATE TABLE hermes_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid            TEXT NOT NULL UNIQUE,
        project_id      INTEGER NOT NULL,
        name            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active',
        origin          TEXT NOT NULL DEFAULT 'native',
        model           TEXT,
        permission_mode TEXT,
        brief           TEXT,
        notes           TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        last_used_at    TEXT NOT NULL,
        ended_at        TEXT,
        claude_name     TEXT
    )
    """
).strip()

_RUNS_TABLE_SQL = dedent(
    """
    CREATE TABLE hermes_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        INTEGER NOT NULL,
        run_index         INTEGER NOT NULL,
        hermes_session_id TEXT,
        mode              TEXT NOT NULL,
        started_at        TEXT NOT NULL,
        ended_at          TEXT
    )
    """
).strip()


def _make_db(tmp_path: Path, *project_paths: str) -> sqlite3.Connection:
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_PROJECTS_TABLE_SQL)
    conn.execute(_SESSIONS_TABLE_SQL)
    conn.execute(_RUNS_TABLE_SQL)
    now = "2026-05-18T00:00:00+00:00"
    for p in project_paths:
        conn.execute(
            "INSERT INTO hermes_projects_v(folder_path, created_at, last_used_at) "
            "VALUES (?, ?, ?)",
            (str(Path(p).resolve()), now, now),
        )
    conn.commit()
    return conn


def _write_jsonl(path: Path, cwd: str, uuid: str, *,
                 user_text: str = "hello",
                 model: str = "claude-sonnet-4-6",
                 last_ts: str = "2026-05-17T10:00:02Z") -> None:
    lines = [
        {"type": "summary", "cwd": cwd, "version": "1.2.3",
         "timestamp": "2026-05-17T10:00:00Z"},
        {"type": "user", "timestamp": "2026-05-17T10:00:01Z",
         "message": {"content": user_text}},
        {"type": "assistant", "timestamp": last_ts,
         "message": {"model": model, "content": "ok"}},
    ]
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")


def _write_profile_session(path: Path, *, claude_uid: str, cwd: str,
                           last_updated: str = "2026-05-17T11:00:00Z") -> None:
    payload = {
        "session_id": "hermes-abc",
        "claude_session_id": claude_uid,
        "cwd": cwd,
        "model": "claude-sonnet-4-6",
        "session_start": "2026-05-17T09:00:00Z",
        "last_updated": last_updated,
        "name": "from-hermes",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# sync_all — both sources
# ---------------------------------------------------------------------------


def test_sync_all_walks_jsonl_and_profile_sources(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    # 1. jsonl source
    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    uid_jsonl = "11111111-1111-1111-1111-111111111111"
    _write_jsonl(proj_dir / f"{uid_jsonl}.jsonl", str(project), uid_jsonl,
                 user_text="from-jsonl")

    # 2. hermes profile session_*.json source
    hermes_home = tmp_path / "hermes"
    profile_sessions = hermes_home / "profiles" / "worker" / "sessions"
    profile_sessions.mkdir(parents=True)
    uid_profile = "22222222-2222-2222-2222-222222222222"
    _write_profile_session(
        profile_sessions / "session_hermes-abc.json",
        claude_uid=uid_profile, cwd=str(project),
    )

    res = sync_all(
        conn,
        claude_projects_dir=claude_root,
        hermes_home=hermes_home,
    )

    assert res.synced_jsonl == 1
    assert res.synced_profile == 1
    assert res.errors == 0

    rows = conn.execute(
        "SELECT uuid, origin FROM hermes_sessions ORDER BY uuid"
    ).fetchall()
    assert [r["uuid"] for r in rows] == sorted([uid_jsonl, uid_profile])
    # No runs → both reclassified to native.
    assert {r["origin"] for r in rows} == {"native"}
    assert res.reclassify_native == 2
    assert res.reclassify_spawned == 0


# ---------------------------------------------------------------------------
# mtime-keyed skip
# ---------------------------------------------------------------------------


def test_sync_all_skips_unchanged_jsonl_on_second_call(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    uid = "33333333-3333-3333-3333-333333333333"
    jpath = proj_dir / f"{uid}.jsonl"
    _write_jsonl(jpath, str(project), uid)

    # Force the file's mtime to be older than its in-jsonl last_event so
    # the cached last_used_at (from the transcript) always wins on the
    # second pass regardless of clock skew between fs and "now".
    past = time.time() - 7 * 24 * 3600
    os.utime(jpath, (past, past))

    first = sync_all(conn, claude_projects_dir=claude_root,
                     hermes_home=tmp_path / "no-hermes-here")
    assert first.synced_jsonl == 1
    assert first.skipped_mtime_unchanged == 0

    second = sync_all(conn, claude_projects_dir=claude_root,
                      hermes_home=tmp_path / "no-hermes-here")
    # Second pass: file mtime <= cached last_used_at, so we skip.
    assert second.synced_jsonl == 0
    assert second.skipped_mtime_unchanged == 1


def test_sync_all_re_syncs_after_mtime_bump(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    uid = "44444444-4444-4444-4444-444444444444"
    jpath = proj_dir / f"{uid}.jsonl"
    _write_jsonl(jpath, str(project), uid,
                 last_ts="2026-05-17T10:00:02Z")
    os.utime(jpath, (time.time() - 86400, time.time() - 86400))

    sync_all(conn, claude_projects_dir=claude_root,
             hermes_home=tmp_path / "no-hermes-here")

    # Rewrite jsonl with a later last_ts so the row's last_used_at goes
    # forward and mtime bumps past the cache.
    _write_jsonl(jpath, str(project), uid,
                 last_ts="2026-05-18T18:00:00Z")
    # Make sure mtime is newer than the cached last_used_at (the cache
    # stores the ISO timestamp inside the jsonl, which we just advanced).
    future = time.time() + 60
    os.utime(jpath, (future, future))

    res = sync_all(conn, claude_projects_dir=claude_root,
                   hermes_home=tmp_path / "no-hermes-here")
    assert res.synced_jsonl == 1
    assert res.skipped_mtime_unchanged == 0
    row = conn.execute(
        "SELECT last_used_at FROM hermes_sessions WHERE uuid=?", (uid,)
    ).fetchone()
    assert row["last_used_at"] == "2026-05-18T18:00:00Z"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_sync_all_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    for i, uid_tail in enumerate(("a", "b", "c"), start=1):
        uid = f"55555555-5555-5555-5555-55555555555{uid_tail}"
        _write_jsonl(proj_dir / f"{uid}.jsonl", str(project), uid,
                     user_text=f"task-{i}")

    first = sync_all(conn, claude_projects_dir=claude_root,
                     hermes_home=tmp_path / "no-hermes-here")
    second = sync_all(conn, claude_projects_dir=claude_root,
                      hermes_home=tmp_path / "no-hermes-here")

    assert first.synced_jsonl == 3
    # Second pass: either all skipped (mtime caught up) or all re-upserted
    # (mtime > last_used_at because the jsonl was just written) — but in
    # any case the row count must stay at 3 and no INSERTs.
    n_rows = conn.execute("SELECT COUNT(*) FROM hermes_sessions").fetchone()[0]
    assert n_rows == 3
    assert second.errors == 0


# ---------------------------------------------------------------------------
# Reclassify is invoked exactly once
# ---------------------------------------------------------------------------


def test_sync_all_reclassifies_spawned_sessions(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    uid = "66666666-6666-6666-6666-666666666666"
    _write_jsonl(proj_dir / f"{uid}.jsonl", str(project), uid)

    sync_all(conn, claude_projects_dir=claude_root,
             hermes_home=tmp_path / "no-hermes-here")

    # Now insert a "print"-mode run; the next sync_all must reclassify
    # this session to spawned.
    sess_id = conn.execute(
        "SELECT id FROM hermes_sessions WHERE uuid=?", (uid,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO hermes_runs(session_id, run_index, mode, started_at) "
        "VALUES (?, 1, 'print', '2026-05-18T00:00:00Z')",
        (sess_id,),
    )
    conn.commit()

    res = sync_all(conn, claude_projects_dir=claude_root,
                   hermes_home=tmp_path / "no-hermes-here")
    assert res.reclassify_spawned == 1
    assert res.reclassify_native == 0
    row = conn.execute(
        "SELECT origin FROM hermes_sessions WHERE uuid=?", (uid,)
    ).fetchone()
    assert row["origin"] == "spawned"


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def test_sync_all_dry_run_does_not_write(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "anything"
    proj_dir.mkdir(parents=True)
    uid = "77777777-7777-7777-7777-777777777777"
    _write_jsonl(proj_dir / f"{uid}.jsonl", str(project), uid)

    res = sync_all(conn, claude_projects_dir=claude_root,
                   hermes_home=tmp_path / "no-hermes-here", dry_run=True)
    assert res.synced_jsonl == 1
    n_rows = conn.execute("SELECT COUNT(*) FROM hermes_sessions").fetchone()[0]
    assert n_rows == 0


# ---------------------------------------------------------------------------
# Missing roots are not errors
# ---------------------------------------------------------------------------


def test_sync_all_handles_missing_roots(tmp_path: Path) -> None:
    conn = _make_db(tmp_path)
    res = sync_all(conn,
                   claude_projects_dir=tmp_path / "no-claude",
                   hermes_home=tmp_path / "no-hermes")
    assert res.synced_jsonl == 0
    assert res.synced_profile == 0
    assert res.errors == 0


# ---------------------------------------------------------------------------
# Unregistered projects are counted, not raised
# ---------------------------------------------------------------------------


def test_sync_all_counts_unregistered_projects(tmp_path: Path) -> None:
    known = tmp_path / "known"
    known.mkdir()
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    conn = _make_db(tmp_path, str(known))

    claude_root = tmp_path / "claude" / "projects"
    proj_dir = claude_root / "x"
    proj_dir.mkdir(parents=True)
    _write_jsonl(proj_dir / "88888888-8888-8888-8888-888888888881.jsonl",
                 str(known), "88888888-8888-8888-8888-888888888881")
    _write_jsonl(proj_dir / "88888888-8888-8888-8888-888888888882.jsonl",
                 str(unknown), "88888888-8888-8888-8888-888888888882")

    res = sync_all(conn, claude_projects_dir=claude_root,
                   hermes_home=tmp_path / "no-hermes-here")
    assert res.synced_jsonl == 1
    assert res.skipped_no_project == 1


# ---------------------------------------------------------------------------
# Heartbeat wiring (mock)
# ---------------------------------------------------------------------------


def test_heartbeat_main_calls_sync_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat must invoke ``_sync_ledger_before_classify`` before
    ``classify_sessions`` so the unified view that classify reads from is
    backed by an up-to-date ``hermes_sessions``."""
    from worker_control_hermes import heartbeat

    calls: list[str] = []

    def fake_sync() -> None:
        calls.append("sync")

    def fake_classify(_window: int) -> dict:
        calls.append("classify")
        return {"alive": [], "just_ended": [], "idle": []}

    def fake_render(_snap: dict) -> str:
        return ""

    monkeypatch.setattr(heartbeat, "_sync_ledger_before_classify", fake_sync)
    monkeypatch.setattr(heartbeat, "classify_sessions", fake_classify)
    monkeypatch.setattr(heartbeat, "render_text", fake_render)
    monkeypatch.setattr("sys.argv", ["heartbeat"])

    heartbeat.main()

    assert calls == ["sync", "classify"]
