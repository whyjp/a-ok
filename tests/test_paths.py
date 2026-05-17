from pathlib import Path

from worker_control.paths import (
    DEFAULT_OWNED_WORK_ROOT,
    DEFAULT_PUBLIC_REFERENCE_ROOT,
    ROLE_OTHER,
    ROLE_OWNED_WORK,
    ROLE_PUBLIC_REFERENCE,
    classify_path,
    configured_roots,
    db_path,
    ensure_runtime_dirs,
    is_writable_project_path,
    normalize_path,
    owned_work_root,
    project_root_default,
    public_reference_root,
    runtime_root,
    sessions_dir,
)


def test_normalize_msys_drive():
    assert str(normalize_path("/d/github")).replace("\\", "/") == "D:/github"
    assert (
        str(normalize_path("/d/work-github/worker-control")).replace("\\", "/")
        == "D:/work-github/worker-control"
    )


def test_normalize_passthrough():
    assert str(normalize_path("D:/github")).replace("\\", "/") == "D:/github"
    assert str(normalize_path("C:/Users/cxx")).replace("\\", "/") == "C:/Users/cxx"


def test_runtime_root_uses_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKER_CONTROL_HOME", str(tmp_path))
    assert runtime_root() == tmp_path


def test_db_path_under_runtime_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKER_CONTROL_HOME", str(tmp_path))
    assert db_path() == tmp_path / "worker-control.sqlite3"


def test_ensure_runtime_dirs_creates(tmp_path: Path, monkeypatch):
    target = tmp_path / "rt"
    monkeypatch.setenv("WORKER_CONTROL_HOME", str(target))
    ensure_runtime_dirs()
    assert target.exists() and target.is_dir()
    assert sessions_dir().exists()


# ---- 워크스페이스 정책 ------------------------------------------------------

def test_default_owned_work_root_is_work_github():
    assert str(DEFAULT_OWNED_WORK_ROOT).replace("\\", "/") == "D:/work-github"


def test_default_public_reference_root_is_github():
    assert str(DEFAULT_PUBLIC_REFERENCE_ROOT).replace("\\", "/") == "D:/github"


def test_project_root_default_is_owned_work():
    assert project_root_default() == owned_work_root() == DEFAULT_OWNED_WORK_ROOT


def test_root_env_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "wg"))
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "gh")
    )
    assert owned_work_root() == tmp_path / "wg"
    assert public_reference_root() == tmp_path / "gh"


def test_configured_roots_returns_two_roles():
    roots = configured_roots()
    roles = {r.role for r in roots}
    assert roles == {ROLE_OWNED_WORK, ROLE_PUBLIC_REFERENCE}


def test_classify_path_owned_work(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "pub"))
    assert classify_path(work / "myproj") == ROLE_OWNED_WORK
    assert is_writable_project_path(work / "myproj") is True


def test_classify_path_public_reference(tmp_path: Path, monkeypatch):
    pub = tmp_path / "pub"
    pub.mkdir()
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(pub))
    assert classify_path(pub / "otherrepo") == ROLE_PUBLIC_REFERENCE
    assert is_writable_project_path(pub / "otherrepo") is False


def test_classify_path_other(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "pub"))
    stray = tmp_path / "elsewhere" / "thing"
    assert classify_path(stray) == ROLE_OTHER
    assert is_writable_project_path(stray) is False
