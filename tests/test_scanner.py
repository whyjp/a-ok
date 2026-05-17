import subprocess
from pathlib import Path

import pytest

from worker_control import db, projects, scanner
from worker_control.paths import ROLE_OWNED_WORK, ROLE_PUBLIC_REFERENCE


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=str(path), check=False)


def test_scan_root_persists_git_and_nongit(tmp_path: Path, monkeypatch):
    # 점프해 들어가는 루트가 owned_work 로 분류되도록 env 를 설정.
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "_unused_pub_"),
    )
    db.init_db()

    git_proj = tmp_path / "alpha"
    git_proj.mkdir()
    _git_init(git_proj)
    (git_proj / "file.txt").write_text("dirty")

    plain = tmp_path / "beta"
    plain.mkdir()

    hidden = tmp_path / ".skipme"
    hidden.mkdir()

    found = scanner.scan_root(tmp_path)
    names = {p.name for p in found}
    assert "alpha" in names and "beta" in names
    assert ".skipme" not in names

    listed = projects.list_projects()
    by_name = {p.name: p for p in listed}
    assert by_name["alpha"].is_git is True
    assert by_name["alpha"].is_dirty is True
    assert by_name["alpha"].root_role == ROLE_OWNED_WORK
    assert by_name["beta"].is_git is False
    assert by_name["beta"].root_role == ROLE_OWNED_WORK


def test_scan_writes_project_scans_row(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "_unused_pub_"),
    )
    db.init_db()
    (tmp_path / "solo").mkdir()
    scanner.scan_root(tmp_path)
    with db.session_scope() as conn:
        row = conn.execute(
            "SELECT discovered, git_repos, root_role "
            "FROM project_scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["discovered"] == 1
    assert row["git_repos"] == 0
    assert row["root_role"] == ROLE_OWNED_WORK


def test_scan_missing_root_returns_empty(tmp_path: Path):
    db.init_db()
    found = scanner.scan_root(tmp_path / "nope")
    assert found == []


def test_scan_classifies_public_reference(tmp_path: Path, monkeypatch):
    """public_reference 루트에 있는 프로젝트는 root_role 이 그렇게 찍혀야 한다."""
    pub = tmp_path / "pub"
    pub.mkdir()
    (pub / "otherrepo").mkdir()
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(pub))
    db.init_db()
    scanner.scan_root(pub)
    proj = projects.get_project("otherrepo")
    assert proj is not None
    assert proj.root_role == ROLE_PUBLIC_REFERENCE


def test_scan_all_configured_roots_handles_missing_paths(
    tmp_path: Path, monkeypatch
):
    """존재하지 않는 루트는 안전하게 빈 리스트를 돌려야 한다."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "proj").mkdir()
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "does-not-exist"),
    )
    db.init_db()
    out = scanner.scan_all_configured_roots()
    assert ROLE_OWNED_WORK in out and ROLE_PUBLIC_REFERENCE in out
    assert any(p.name == "proj" for p in out[ROLE_OWNED_WORK])
    assert out[ROLE_PUBLIC_REFERENCE] == []
