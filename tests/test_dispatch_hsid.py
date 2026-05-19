"""Tests for the dispatch-time HERMES_SESSION_ID validator.

The fix is forward-only: every dispatcher INSERT into ``hermes_runs`` must
go through ``_resolve_active_hsid()`` so stale ghost env values get
auto-corrected to whatever ``~/AppData/Local/hermes/sessions/`` says is the
most-recently-touched session. Backfill is now opt-in via env gate so the
heartbeat can't relink an orphan run to the wrong PM session.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# _resolve_active_hsid — unit
# ---------------------------------------------------------------------------

def _touch(p: Path, mtime: float) -> None:
    p.write_text("{}", encoding="utf-8")
    import os
    os.utime(p, (mtime, mtime))


def _make_sessions_dir(tmp_path: Path, files: dict[str, float]) -> Path:
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    for hsid, age_seconds in files.items():
        _touch(sdir / f"session_{hsid}.json", time.time() - age_seconds)
    return sdir


def test_env_validated_when_file_fresh(tmp_path: Path) -> None:
    from worker_control_hermes.projects import _resolve_active_hsid

    sdir = _make_sessions_dir(tmp_path, {"abc123": 60})
    hsid, reason = _resolve_active_hsid(sessions_dir=sdir, env_value="abc123")

    assert hsid == "abc123"
    assert reason == "env_validated_fresh"


def test_env_stale_falls_back_to_latest(tmp_path: Path, capsys) -> None:
    from worker_control_hermes.projects import _resolve_active_hsid

    sdir = _make_sessions_dir(
        tmp_path,
        {
            "stale": 12 * 24 * 3600,   # 12 days old
            "fresh": 30,               # 30 s old → latest by mtime
        },
    )
    hsid, reason = _resolve_active_hsid(sessions_dir=sdir, env_value="stale")

    assert hsid == "fresh"
    assert reason == "fallback_latest_mtime"
    err = capsys.readouterr().err
    assert "stale HERMES_SESSION_ID" in err
    assert "'stale'" in err
    assert "'fresh'" in err


def test_env_unset_uses_latest(tmp_path: Path, capsys) -> None:
    from worker_control_hermes.projects import _resolve_active_hsid

    sdir = _make_sessions_dir(tmp_path, {"zonly": 5})
    hsid, reason = _resolve_active_hsid(sessions_dir=sdir, env_value="")

    assert hsid == "zonly"
    assert reason == "fallback_env_empty"
    # No WARN when env was empty to begin with.
    assert "stale HERMES_SESSION_ID" not in capsys.readouterr().err


def test_env_set_but_no_file_exists_uses_latest(tmp_path: Path) -> None:
    from worker_control_hermes.projects import _resolve_active_hsid

    sdir = _make_sessions_dir(tmp_path, {"newer": 10})
    hsid, reason = _resolve_active_hsid(sessions_dir=sdir, env_value="ghost-not-on-disk")

    assert hsid == "newer"
    assert reason == "fallback_latest_mtime"


def test_no_sessions_dir_falls_back_to_env(tmp_path: Path) -> None:
    from worker_control_hermes.projects import _resolve_active_hsid

    missing = tmp_path / "does-not-exist"
    hsid, reason = _resolve_active_hsid(sessions_dir=missing, env_value="env123")

    assert hsid == "env123"
    assert reason == "env_used_no_sessions_dir"


def test_empty_sessions_dir_returns_env_unchanged(tmp_path: Path) -> None:
    """Edge case: sessions dir exists but has no session_*.json — keep env."""
    from worker_control_hermes.projects import _resolve_active_hsid

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    hsid, reason = _resolve_active_hsid(sessions_dir=sdir, env_value="env123")

    assert hsid == "env123"
    assert reason == "no_session_files_fallback_env"


# ---------------------------------------------------------------------------
# Integration — cmd_session_start + cmd_run_start stamp the resolved hsid
# ---------------------------------------------------------------------------

_CANONICAL_SCHEMA = """
-- worker_profiles marker tells projects._connect() to skip the legacy
-- SCHEMA bootstrap, which would otherwise try to ALTER hermes_projects_v.
CREATE TABLE worker_profiles (id INTEGER PRIMARY KEY);

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
);

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
    claude_name     TEXT,
    claude_status   TEXT,
    claude_status_at TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL,
    ended_at        TEXT
);

CREATE TABLE hermes_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL,
    run_index         INTEGER NOT NULL,
    name              TEXT NOT NULL,
    mode              TEXT NOT NULL,
    command           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'started',
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    note              TEXT,
    hermes_session_id TEXT
);
"""


def _setup_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a tmp canonical-mode ledger DB + register one project."""
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_CANONICAL_SCHEMA)
    proj_path = str((tmp_path / "proj").resolve())
    (tmp_path / "proj").mkdir()
    conn.execute(
        "INSERT INTO hermes_projects_v(folder_path, display_name, created_at, last_used_at) "
        "VALUES (?, 'proj', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (proj_path,),
    )
    conn.commit()
    conn.close()

    from worker_control_hermes import projects as projects_mod
    monkeypatch.setattr(projects_mod, "DB_PATH", db)
    return db


def _session_start_args(project: str, *, uuid: str, brief: str = "hi") -> argparse.Namespace:
    return argparse.Namespace(
        project=project,
        uuid=uuid,
        name=None,
        brief=brief,
        model=None,
        permission_mode=None,
        print=True,
        prompt="do a thing",
        max_turns=None,
        allowed_tools=None,
        no_auto_close=True,
        json=True,
    )


def _run_start_args(session: str) -> argparse.Namespace:
    return argparse.Namespace(
        session=session,
        model=None,
        permission_mode=None,
        print=True,
        prompt="another",
        max_turns=None,
        allowed_tools=None,
        no_auto_close=True,
        json=True,
    )


def test_session_start_stamps_resolved_hsid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """A fresh env value points at a real, recent session_*.json — that
    exact hsid lands in hermes_runs.hermes_session_id."""
    from worker_control_hermes import projects as projects_mod

    db = _setup_ledger(tmp_path, monkeypatch)

    sdir = _make_sessions_dir(tmp_path, {"hsid-fresh": 30})
    monkeypatch.setattr(projects_mod, "_HERMES_SESSIONS_DIR", sdir)
    monkeypatch.setenv("HERMES_SESSION_ID", "hsid-fresh")

    uid = "11111111-2222-3333-4444-555555555555"
    proj_path = str((tmp_path / "proj").resolve())
    projects_mod.cmd_session_start(_session_start_args(proj_path, uuid=uid))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT hermes_session_id, run_index FROM hermes_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row["hermes_session_id"] == "hsid-fresh"
    assert row["run_index"] == 1


def test_run_start_resume_stamps_same_resolved_hsid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs on the same UUID under the same fresh env — both rows
    stamp the same resolved hsid."""
    from worker_control_hermes import projects as projects_mod

    db = _setup_ledger(tmp_path, monkeypatch)
    sdir = _make_sessions_dir(tmp_path, {"hsid-stable": 30})
    monkeypatch.setattr(projects_mod, "_HERMES_SESSIONS_DIR", sdir)
    monkeypatch.setenv("HERMES_SESSION_ID", "hsid-stable")

    uid = "22222222-2222-3333-4444-555555555555"
    proj_path = str((tmp_path / "proj").resolve())
    projects_mod.cmd_session_start(_session_start_args(proj_path, uuid=uid))
    projects_mod.cmd_run_start(_run_start_args(uid))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT run_index, hermes_session_id FROM hermes_runs ORDER BY run_index"
    ).fetchall()
    conn.close()
    assert [r["run_index"] for r in rows] == [1, 2]
    assert {r["hermes_session_id"] for r in rows} == {"hsid-stable"}


def test_session_start_corrects_stale_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: env says ``ghost`` but the on-disk session_*.json with
    that name is 12 days old and a fresher one exists — the row must
    stamp the fresh one, not the ghost."""
    from worker_control_hermes import projects as projects_mod

    db = _setup_ledger(tmp_path, monkeypatch)
    sdir = _make_sessions_dir(
        tmp_path,
        {"ghost": 12 * 24 * 3600, "fresh-pm": 30},
    )
    monkeypatch.setattr(projects_mod, "_HERMES_SESSIONS_DIR", sdir)
    monkeypatch.setenv("HERMES_SESSION_ID", "ghost")

    uid = "33333333-2222-3333-4444-555555555555"
    proj_path = str((tmp_path / "proj").resolve())
    projects_mod.cmd_session_start(_session_start_args(proj_path, uuid=uid))

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT hermes_session_id FROM hermes_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] == "fresh-pm"


# ---------------------------------------------------------------------------
# Heartbeat backfill gate
# ---------------------------------------------------------------------------

_HEARTBEAT_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL
);
CREATE TABLE hermes_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'native',
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE hermes_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    run_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    note TEXT,
    hermes_session_id TEXT
);
CREATE TABLE hermes_agent_sessions (
    hermes_session_id TEXT PRIMARY KEY,
    profile_name TEXT,
    profile_path TEXT,
    transcript_path TEXT,
    transcript_size INTEGER NOT NULL DEFAULT 0,
    transcript_mtime TEXT,
    started_at TEXT,
    ended_at TEXT,
    synced_at TEXT NOT NULL,
    ai_title TEXT,
    first_user_text TEXT,
    turn_count INTEGER,
    msg_user INTEGER
);
"""


def _stub_heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Common heartbeat setup — tmp DB + stub everything except the gate."""
    from worker_control_hermes import heartbeat
    import worker_control_hermes.legacy_parity_ingest as parity_mod

    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_HEARTBEAT_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(heartbeat, "DB_PATH", db)
    monkeypatch.setattr(heartbeat, "_load_db_sessions", lambda: [])
    monkeypatch.setattr(heartbeat, "_jsonl_lookup", lambda: {})
    monkeypatch.setattr(parity_mod, "ingest_all", lambda conn, **_kw: {"ok": True})
    return heartbeat


def test_heartbeat_backfill_gated_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    """With no env flag, classify_sessions must not invoke backfill_all and
    must surface the disabled message on stderr."""
    from worker_control_hermes import spawn_backfill

    heartbeat = _stub_heartbeat(monkeypatch, tmp_path)
    monkeypatch.delenv("WORKER_CONTROL_BACKFILL_ENABLED", raising=False)

    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("backfill_all must not be called when gate is off")

    monkeypatch.setattr(spawn_backfill, "backfill_all", boom)

    heartbeat.classify_sessions(window_min=30)

    assert calls["n"] == 0
    err = capsys.readouterr().err
    assert "[spawn-backfill] disabled" in err
    assert "WORKER_CONTROL_BACKFILL_ENABLED" in err


def test_heartbeat_backfill_runs_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With WORKER_CONTROL_BACKFILL_ENABLED=1, backfill_all gets one call."""
    from worker_control_hermes import spawn_backfill

    heartbeat = _stub_heartbeat(monkeypatch, tmp_path)
    monkeypatch.setenv("WORKER_CONTROL_BACKFILL_ENABLED", "1")

    calls = {"n": 0}

    def fake_backfill(conn, **kw):
        calls["n"] += 1
        return {
            "sessions_scanned": 0, "sessions_with_changes": 0,
            "updated": 0, "inserted": 0, "skipped": 0,
            "relinked": 0, "ambiguous": 0,
            "unmatched_slugs": [], "per_session": [],
        }

    monkeypatch.setattr(spawn_backfill, "backfill_all", fake_backfill)
    heartbeat.classify_sessions(window_min=30)
    assert calls["n"] == 1
