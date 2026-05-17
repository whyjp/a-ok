"""Workspace policy: sessions.start_session 가 public_reference 를 거부해야 한다."""
from __future__ import annotations

from pathlib import Path

import pytest

from worker_control import db, profiles, scanner, sessions
from worker_control.sessions import WriteProtectedRootError


def _setup_two_roots(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    work = tmp_path / "work"
    pub = tmp_path / "pub"
    work.mkdir()
    pub.mkdir()
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(pub))
    return work, pub


def test_start_session_refuses_public_reference(tmp_path: Path, monkeypatch):
    work, pub = _setup_two_roots(tmp_path, monkeypatch)
    (pub / "otherrepo").mkdir()

    db.init_db()
    scanner.scan_root(pub)            # otherrepo → public_reference
    profiles.create_profile("default")  # root defaults to owned_work

    with pytest.raises(WriteProtectedRootError):
        sessions.start_session("default", "otherrepo")


def test_start_session_refuses_other_role(tmp_path: Path, monkeypatch):
    """알려진 두 루트 어디에도 속하지 않는 경로는 거부."""
    work, pub = _setup_two_roots(tmp_path, monkeypatch)
    stray_root = tmp_path / "elsewhere"
    stray_root.mkdir()
    (stray_root / "thing").mkdir()

    db.init_db()
    # stray_root 를 강제로 스캔하면 role=other 로 기록됨.
    scanner.scan_root(stray_root)
    profiles.create_profile("default")

    with pytest.raises(WriteProtectedRootError):
        sessions.start_session("default", "thing")


def test_start_session_for_owned_work_passes_policy_guard(
    tmp_path: Path, monkeypatch
):
    """owned_work 프로젝트면 정책 가드는 통과한다 (실제 launch 는 mock)."""
    work, _pub = _setup_two_roots(tmp_path, monkeypatch)
    (work / "mineproj").mkdir()

    db.init_db()
    scanner.scan_root(work)
    profiles.create_profile("default")

    # runtime.launch 를 가짜로 대체해 실제 claude 프로세스 기동을 막는다.
    from worker_control import runtime as runtime_mod

    def fake_launch(name, cwd, prefer_tmux=True):
        return runtime_mod.LaunchResult(
            runtime="console",
            tmux_session=None,
            pid=12345,
            note="test-stub",
        )

    monkeypatch.setattr(runtime_mod, "launch", fake_launch)

    s = sessions.start_session("default", "mineproj")
    assert s.state == "running"
    assert s.runtime == "console"
    assert s.pid == 12345
