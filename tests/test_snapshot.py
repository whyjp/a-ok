"""Tests for the Telegram-bound dashboard snapshot emitter."""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from worker_control import cli, db, profiles, scanner, sessions, snapshot
from worker_control.db import session_scope, utcnow_iso, dump_json


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "main"], cwd=str(path), check=False,
    )


@pytest.fixture
def populated(tmp_path: Path, monkeypatch):
    work = tmp_path / "work-github"
    pub = tmp_path / "github"
    work.mkdir()
    pub.mkdir()
    (work / "worker-control").mkdir()
    _git_init(work / "worker-control")
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(pub))
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR",
        str(tmp_path / ".claude-projects"),
    )
    db.init_db()
    profiles.create_profile("default", root=str(work))
    scanner.scan_root(work)
    return tmp_path


def _insert_session(state: str) -> int:
    """프로파일/프로젝트 첫 행에 붙여 임의 상태로 worker_session 직접 삽입."""
    now = utcnow_iso()
    with session_scope() as conn:
        prof = conn.execute(
            "SELECT id FROM worker_profiles LIMIT 1"
        ).fetchone()
        proj = conn.execute(
            "SELECT id FROM projects LIMIT 1"
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO worker_sessions
              (name, profile_id, project_id, state, runtime,
               tmux_session, pid, started_at, ended_at,
               metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL, ?, ?, ?)
            """,
            (f"test-{state}", prof["id"], proj["id"], state, now,
             dump_json({}), now, now),
        )
        return cur.lastrowid


def test_check_live_false_when_no_sessions(populated):
    res = snapshot.check_live(include_native=False)
    assert res.alive is False
    assert res.hermes_live == 0
    assert res.native_recent == 0


def test_check_live_true_for_running_state(populated):
    _insert_session("running")
    res = snapshot.check_live(include_native=False)
    assert res.alive is True
    assert res.hermes_live == 1


@pytest.mark.parametrize("state", sorted(snapshot.LIVE_STATES))
def test_each_live_state_counts(populated, state):
    _insert_session(state)
    res = snapshot.check_live(include_native=False)
    assert res.hermes_live == 1
    assert res.alive is True


@pytest.mark.parametrize("state", ["completed", "failed", "killed"])
def test_dead_states_do_not_count(populated, state):
    _insert_session(state)
    res = snapshot.check_live(include_native=False)
    assert res.hermes_live == 0
    assert res.alive is False


def test_emit_snapshot_silent_when_no_live_session(populated, capsys):
    rc = snapshot.emit_snapshot(include_native_for_liveness=False)
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "no live session" in out.err


def test_emit_snapshot_outputs_message_and_media_when_alive(populated, capsys):
    _insert_session("running")
    rc = snapshot.emit_snapshot(include_native_for_liveness=False)
    assert rc == 0
    captured = capsys.readouterr()
    text = captured.out
    assert "worker-control snapshot" in text
    assert "MEDIA:" in text
    media_lines = [ln for ln in text.splitlines() if ln.startswith("MEDIA:")]
    assert len(media_lines) == 1
    media_path = Path(media_lines[0][len("MEDIA:"):])
    assert media_path.exists()
    body = media_path.read_text(encoding="utf-8")
    assert body.startswith("<!DOCTYPE html>")
    # legacy export 는 인라인 데이터를 박아넣는다.
    assert "__INLINE_DATA__" not in body


def test_cli_dashboard_snapshot_silent_when_no_live(populated, capsys):
    rc = cli.main(["dashboard-snapshot", "--no-native-liveness"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.out == ""


def test_cli_dashboard_snapshot_emits_when_live(populated, capsys):
    _insert_session("working")
    rc = cli.main(["dashboard-snapshot", "--no-native-liveness"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "MEDIA:" in out
    assert "worker-control snapshot" in out


def test_cli_dashboard_daemon_once_against_existing_server(populated, capsys):
    from worker_control import server
    db.init_db()
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0, log_sink=None,
    )
    try:
        rc = cli.main([
            "dashboard-daemon", "--once",
            "--host", "127.0.0.1", "--port", str(srv.port),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "already running" in out
    finally:
        srv.shutdown(); srv.server_close(); thread.join(timeout=2)
