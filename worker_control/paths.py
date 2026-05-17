"""Path normalization, workspace policy, and runtime location.

워크스페이스 정책 (두 개의 루트, 역할 분리):

- ``D:/work-github`` — **owned_work**. 본인 소유의 GitHub 워크스페이스.
  편집/커밋/PR/푸시 대상. 워커가 ``claude`` 를 띄울 수 있는 유일한 기본 루트.
- ``D:/github`` — **public_reference**. 다른 사람/공개 저장소 참조용.
  기본 read-only. ``workerctl sessions start`` 는 이 루트의 프로젝트에 대해
  워커 기동을 거부한다.

런타임 데이터(SQLite DB, 세션 로그/캡처) 는 코드 저장소 밖에 둔다:
기본 위치는 ``D:/work-github/.worker-control`` (구버전에서 ``D:/github/.worker-control``
를 쓰던 호환성은 ``WORKER_CONTROL_HOME`` 으로 명시 지정).

환경 변수:
- ``WORKER_CONTROL_HOME``                  : 런타임 루트(SQLite 등) 위치 덮어쓰기.
- ``WORKER_CONTROL_DB``                    : DB 파일 경로 덮어쓰기.
- ``WORKER_CONTROL_PROJECT_ROOT``          : 기본 owned_work 루트 덮어쓰기.
- ``WORKER_CONTROL_PUBLIC_REFERENCE_ROOT`` : 기본 public_reference 루트 덮어쓰기.

MSYS 스타일 ``/d/github`` 와 ``D:/github`` 는 같은 경로로 정규화된다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 두 워크스페이스의 기본 루트
DEFAULT_OWNED_WORK_ROOT: Final[Path] = Path("D:/work-github")
DEFAULT_PUBLIC_REFERENCE_ROOT: Final[Path] = Path("D:/github")

# 런타임 데이터는 owned_work 워크스페이스 아래의 숨김 폴더에 둔다.
DEFAULT_RUNTIME_ROOT: Final[Path] = DEFAULT_OWNED_WORK_ROOT / ".worker-control"
DEFAULT_DB_FILENAME: Final[str] = "worker-control.sqlite3"

# 역할 라벨 (DB 에 그대로 저장된다)
ROLE_OWNED_WORK: Final[str] = "owned_work"
ROLE_PUBLIC_REFERENCE: Final[str] = "public_reference"
ROLE_OTHER: Final[str] = "other"

_MSYS_DRIVE_RE = re.compile(r"^/([a-zA-Z])(/|$)")


@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    """루트 경로와 역할(role) 메타데이터."""

    path: Path
    role: str  # ROLE_OWNED_WORK | ROLE_PUBLIC_REFERENCE | ROLE_OTHER

    @property
    def is_writable_default(self) -> bool:
        """기본적으로 워커가 쓰기/세션 시작 가능한 루트인지."""
        return self.role == ROLE_OWNED_WORK


def normalize_path(value: str | os.PathLike[str]) -> Path:
    """Normalize MSYS-style (/d/github) and mixed-slash paths to a Path.

    Examples
    --------
    >>> str(normalize_path("/d/github")).replace("\\\\", "/")
    'D:/github'
    >>> str(normalize_path("D:/work-github/worker-control")).replace("\\\\", "/")
    'D:/work-github/worker-control'
    """
    s = os.fspath(value)
    m = _MSYS_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).upper()
        rest = s[m.end() - (1 if m.group(2) else 0):]
        s = f"{drive}:{rest}" if not rest.startswith("/") else f"{drive}:{rest}"
    return Path(s)


def _resolve_for_compare(p: Path) -> Path:
    """비교용 경로 정규화. 존재 여부와 무관하게 동작."""
    try:
        return Path(os.path.normpath(os.path.abspath(str(p))))
    except OSError:
        return p


# ---------- 루트 해석 ---------------------------------------------------------

def owned_work_root() -> Path:
    """기본 owned_work 루트. WORKER_CONTROL_PROJECT_ROOT 로 덮어쓸 수 있다."""
    env = os.environ.get("WORKER_CONTROL_PROJECT_ROOT")
    if env:
        return normalize_path(env)
    return DEFAULT_OWNED_WORK_ROOT


def public_reference_root() -> Path:
    """기본 public_reference 루트. WORKER_CONTROL_PUBLIC_REFERENCE_ROOT 로 덮어쓸 수 있다."""
    env = os.environ.get("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT")
    if env:
        return normalize_path(env)
    return DEFAULT_PUBLIC_REFERENCE_ROOT


def project_root_default() -> Path:
    """기본 프로젝트 스캔 루트 — 이제 owned_work 루트로 통일."""
    return owned_work_root()


def configured_roots() -> list[WorkspaceRoot]:
    """현재 설정된 모든 워크스페이스 루트(역할 포함)."""
    return [
        WorkspaceRoot(owned_work_root(), ROLE_OWNED_WORK),
        WorkspaceRoot(public_reference_root(), ROLE_PUBLIC_REFERENCE),
    ]


def classify_path(path: str | os.PathLike[str]) -> str:
    """주어진 경로가 어느 워크스페이스에 속하는지 역할로 분류.

    어떤 알려진 루트의 자식도 아니면 ``ROLE_OTHER`` 를 반환한다.
    """
    p = _resolve_for_compare(normalize_path(path))
    for root in configured_roots():
        rp = _resolve_for_compare(root.path)
        try:
            p.relative_to(rp)
        except ValueError:
            continue
        return root.role
    return ROLE_OTHER


def is_writable_project_path(path: str | os.PathLike[str]) -> bool:
    """워커가 이 경로에서 세션을 시작해도 되는지 (기본 정책)."""
    return classify_path(path) == ROLE_OWNED_WORK


# ---------- 런타임 위치 -------------------------------------------------------

def runtime_root() -> Path:
    """Resolve the runtime root, honoring WORKER_CONTROL_HOME if set."""
    env = os.environ.get("WORKER_CONTROL_HOME")
    if env:
        return normalize_path(env)
    return DEFAULT_RUNTIME_ROOT


def db_path() -> Path:
    """Resolve the default SQLite DB path."""
    env = os.environ.get("WORKER_CONTROL_DB")
    if env:
        return normalize_path(env)
    return runtime_root() / DEFAULT_DB_FILENAME


def sessions_dir() -> Path:
    """Directory holding per-session capture/log files."""
    return runtime_root() / "sessions"


def ensure_runtime_dirs() -> None:
    """Create runtime root and sessions dir if missing."""
    runtime_root().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
