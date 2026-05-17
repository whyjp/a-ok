"""Dashboard data layer — SQLite snapshot for a dynamic backend-for-frontend.

이 모듈은 두 가지 책임만 진다:

1. **스냅샷 수집** — ``collect_snapshot()`` 이 ``worker_profiles`` /
   ``projects`` / ``worker_sessions`` 테이블과 ``~/.claude/projects/`` 의
   native 세션을 한 묶음 ``DashboardSnapshot`` 으로 모은다.
2. **FE 자산 로딩** — ``static_dashboard_html()`` 이 패키지에 포함된
   ``static/dashboard.html`` 을 반환한다. 이 파일은 BFF (``server.py``) 가
   ``GET /`` 응답으로 그대로 서빙한다. FE 는 ``/api/snapshot`` 을 호출해
   동적으로 데이터를 그린다.

레거시:
    ``render_html()`` / ``write_dashboard()`` 는 BFF 없이 동작하는 단일 파일
    오프라인 스냅샷을 위해서만 남겨둔다. 새 사용자는 BFF
    (``workerctl view serve``) 를 써야 한다.
"""
from __future__ import annotations

import json
import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from worker_control import __version__
from worker_control.db import utcnow_iso
from worker_control.native_sessions import (
    NativeSnapshot,
    discover_native_sessions,
)
from worker_control.paths import (
    ROLE_OWNED_WORK,
    ROLE_PUBLIC_REFERENCE,
    configured_roots,
    db_path,
    runtime_root,
)
from worker_control.profiles import list_profiles
from worker_control.projects import list_projects
from worker_control.sessions import list_sessions


DEFAULT_OUTPUT_FILENAME = "dashboard.html"

# 패키지에 포함된 FE 자산. BFF 가 그대로 서빙하고, legacy 경로에서는
# placeholder 를 인라인 데이터로 치환해서 단일 HTML 로 내보낸다.
STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML_PATH = STATIC_DIR / "dashboard.html"

# 정적 FE asset 안에 들어 있는 placeholder. 이 문자열이 등장하는 위치는
# ``<script id="dashboard-data" type="application/json">"__INLINE_DATA__"</script>``
# 이며, JS 는 파싱 결과가 객체가 아니면 ``/api/snapshot`` 으로 폴백한다.
_INLINE_PLACEHOLDER = '"__INLINE_DATA__"'


# ----- 스냅샷 -----------------------------------------------------------------

@dataclass(slots=True)
class WorkspaceRootView:
    role: str
    path: str
    exists: bool
    writable: bool


@dataclass(slots=True)
class DashboardSnapshot:
    """대시보드 렌더에 필요한 모든 데이터 (직렬화 가능)."""

    generated_at: str
    version: str
    db_path: str
    runtime_root: str
    workspace_roots: list[WorkspaceRootView] = field(default_factory=list)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    hermes_sessions: list[dict[str, Any]] = field(default_factory=list)
    native_sessions: list[dict[str, Any]] = field(default_factory=list)
    native_root: str = ""
    native_root_exists: bool = False
    native_note: str | None = None
    counters: dict[str, int] = field(default_factory=dict)


def _profile_to_dict(p) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "root_path": p.root_path,
        "metadata": p.metadata,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _project_to_dict(p) -> dict[str, Any]:
    is_owned = p.root_role == ROLE_OWNED_WORK
    return {
        "id": p.id,
        "name": p.name,
        "path": p.path,
        "is_git": p.is_git,
        "branch": p.branch,
        "remote_url": p.remote_url,
        "is_dirty": p.is_dirty,
        "root_role": p.root_role,
        "policy": "work_capable" if is_owned else "read_only",
        "last_scan_at": p.last_scan_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _session_to_dict(s, profile_by_id, project_by_id) -> dict[str, Any]:
    prof = profile_by_id.get(s.profile_id)
    proj = project_by_id.get(s.project_id)
    return {
        "id": s.id,
        "name": s.name,
        "state": s.state,
        "runtime": s.runtime,
        "tmux_session": s.tmux_session,
        "pid": s.pid,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "profile_id": s.profile_id,
        "profile_name": prof.name if prof else None,
        "project_id": s.project_id,
        "project_name": proj.name if proj else None,
        "project_path": proj.path if proj else None,
        "project_role": proj.root_role if proj else None,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def collect_snapshot(native_limit: int | None = 500) -> DashboardSnapshot:
    """현재 DB 상태 + native 세션 디스커버리 결과를 한 묶음으로 모은다."""
    # 워크스페이스 루트
    roots: list[WorkspaceRootView] = []
    for r in configured_roots():
        roots.append(WorkspaceRootView(
            role=r.role,
            path=str(r.path),
            exists=r.path.exists(),
            writable=r.is_writable_default,
        ))

    # 프로파일 / 프로젝트 / Hermes 세션
    profiles = list_profiles()
    projs = list_projects()
    sess = list_sessions()
    profile_by_id = {p.id: p for p in profiles}
    project_by_id = {p.id: p for p in projs}

    # native 세션 (read-only)
    try:
        native: NativeSnapshot = discover_native_sessions(limit=native_limit)
    except Exception as exc:  # 안전망 — discovery 실패해도 대시보드는 떠야 함
        native = NativeSnapshot(
            root="(error)", root_exists=False,
            note=f"native 디스커버리 중 예외: {exc}",
        )

    snap = DashboardSnapshot(
        generated_at=utcnow_iso(),
        version=__version__,
        db_path=str(db_path()),
        runtime_root=str(runtime_root()),
        workspace_roots=roots,
        profiles=[_profile_to_dict(p) for p in profiles],
        projects=[_project_to_dict(p) for p in projs],
        hermes_sessions=[
            _session_to_dict(s, profile_by_id, project_by_id) for s in sess
        ],
        native_sessions=[asdict(n) for n in native.sessions],
        native_root=native.root,
        native_root_exists=native.root_exists,
        native_note=native.note,
        counters={
            "profiles": len(profiles),
            "projects": len(projs),
            "projects_owned": sum(
                1 for p in projs if p.root_role == ROLE_OWNED_WORK
            ),
            "projects_public": sum(
                1 for p in projs if p.root_role == ROLE_PUBLIC_REFERENCE
            ),
            "projects_git": sum(1 for p in projs if p.is_git),
            "projects_dirty": sum(1 for p in projs if p.is_dirty),
            "hermes_sessions": len(sess),
            "hermes_running": sum(
                1 for s in sess if s.state in ("running", "working")
            ),
            "native_sessions": len(native.sessions),
        },
    )
    return snap


# ----- 직렬화 헬퍼 ------------------------------------------------------------

def snapshot_to_payload(snap: DashboardSnapshot) -> dict[str, Any]:
    """``DashboardSnapshot`` → JSON 직렬화 가능한 dict.

    BFF ``/api/snapshot`` 과 legacy 인라인 export 둘 다 같은 모양을 본다.
    """
    return {
        "generated_at": snap.generated_at,
        "version": snap.version,
        "db_path": snap.db_path,
        "runtime_root": snap.runtime_root,
        "workspace_roots": [asdict(r) for r in snap.workspace_roots],
        "profiles": snap.profiles,
        "projects": snap.projects,
        "hermes_sessions": snap.hermes_sessions,
        "native_sessions": snap.native_sessions,
        "native_root": snap.native_root,
        "native_root_exists": snap.native_root_exists,
        "native_note": snap.native_note,
        "counters": snap.counters,
    }


def _snapshot_to_inline_json(snap: DashboardSnapshot) -> str:
    """``<script type="application/json">`` 안에 안전하게 들어갈 JSON 문자열.

    ``</script>`` / ``<!--`` 끼어 들어 인라인 스크립트가 깨지지 않도록 sentinel
    치환을 거친다.
    """
    raw = json.dumps(snapshot_to_payload(snap), ensure_ascii=False)
    return (
        raw.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


# ----- FE 자산 로딩 -----------------------------------------------------------

def static_dashboard_html() -> str:
    """패키지에 포함된 정적 FE 자산을 그대로 반환한다.

    BFF (``server.py``) 의 ``GET /`` 가 이 결과를 그대로 응답한다. 안에 들어
    있는 placeholder ``"__INLINE_DATA__"`` 는 그대로 두며, FE 의 JS 는 파싱
    결과가 객체가 아니면 ``/api/snapshot`` 을 호출해 데이터를 가져온다.
    """
    return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


# ----- 레거시: 단일 파일 인라인 export ----------------------------------------

def render_html(snap: DashboardSnapshot) -> str:
    """**LEGACY**: 정적 FE 자산에 스냅샷 JSON 을 박아 단일 HTML 로 반환한다.

    오프라인/이메일 첨부용으로만 사용한다. 일반 사용자는
    ``workerctl view serve`` 의 BFF + FE 조합을 쓴다.
    """
    template = static_dashboard_html()
    if _INLINE_PLACEHOLDER not in template:
        raise RuntimeError(
            "dashboard 정적 자산이 손상되었습니다 "
            f"(placeholder {_INLINE_PLACEHOLDER!r} 누락)"
        )
    return template.replace(_INLINE_PLACEHOLDER, _snapshot_to_inline_json(snap))


def default_output_path() -> Path:
    """레거시 export 의 기본 출력 경로."""
    return runtime_root() / DEFAULT_OUTPUT_FILENAME


def write_dashboard(
    output: Path | str | None = None,
    *,
    native_limit: int | None = 500,
) -> Path:
    """**LEGACY**: 단일 파일 인라인 스냅샷 HTML 을 작성한다.

    Parameters
    ----------
    output:
        출력 파일 경로. ``None`` 이면 ``default_output_path()`` 사용.
        디렉토리가 없으면 자동 생성된다.
    native_limit:
        native 세션 디스커버리 상한.
    """
    snap = collect_snapshot(native_limit=native_limit)
    target = Path(output) if output else default_output_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_html(snap)
    target.write_text(html_text, encoding="utf-8")
    return target


def open_in_browser(path: Path) -> bool:
    """기본 브라우저로 파일을 연다. 실패해도 예외를 던지지 않음."""
    try:
        url = path.resolve().as_uri()
    except (OSError, ValueError):
        url = "file:///" + str(path).replace("\\", "/")
    return open_in_browser_url(url)


def open_in_browser_url(url: str) -> bool:
    """기본 브라우저로 임의 URL 을 연다. 실패해도 예외를 던지지 않음."""
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False
