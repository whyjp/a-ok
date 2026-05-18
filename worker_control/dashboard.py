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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker_control import __version__
from worker_control.db import connect as connect_canonical, utcnow_iso
from worker_control.hermes_ledger import (
    HermesSessionView,
    hermes_session_counters,
    is_spawn,
    list_hermes_sessions,
)
from worker_control.hermes_profiles import (
    HermesProfile,
    discover_hermes_profiles,
    hermes_profile_to_dict,
)
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
    # Hermes profiles auto-discovered from disk (~/AppData/Local/hermes/
    # profiles/<name>/). Independent of `profiles` above (which is
    # worker_control's own DB-backed worker_profiles table) so the dashboard
    # can show both sources side by side.
    hermes_profiles: list[dict[str, Any]] = field(default_factory=list)
    hermes_home: str = ""
    hermes_home_exists: bool = False
    projects: list[dict[str, Any]] = field(default_factory=list)
    hermes_sessions: list[dict[str, Any]] = field(default_factory=list)
    native_sessions: list[dict[str, Any]] = field(default_factory=list)
    # Hermes turn-by-turn sessions (one row per ~/AppData/Local/hermes/
    # profiles/<name>/sessions/session_*.json), joined with the claude runs
    # they spawned via hermes_runs.hermes_session_id.
    hermes_agent_sessions: list[dict[str, Any]] = field(default_factory=list)
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


def _hermes_ledger_to_dict(v: HermesSessionView) -> dict[str, Any]:
    """HermesSessionView → JSON-serializable dict for the BFF response."""
    return {
        "id": v.id,
        "uuid": v.uuid,
        "name": v.name,
        "status": v.status,
        "origin": v.origin,
        "classification": v.classification,
        "spawn_reason": v.spawn_reason,
        "dispatch_mode": v.dispatch_mode,
        "run_count": v.run_count,
        "print_run_count": v.print_run_count,
        "last_run_index": v.last_run_index,
        "last_run_name": v.last_run_name,
        "last_run_mode": v.last_run_mode,
        "last_run_status": v.last_run_status,
        "last_run_started_at": v.last_run_started_at,
        "last_run_ended_at": v.last_run_ended_at,
        "model": v.model,
        "permission_mode": v.permission_mode,
        "brief": v.brief,
        "claude_name": v.claude_name,
        "claude_status": v.claude_status,
        "claude_status_at": v.claude_status_at,
        "project_id": v.project_id,
        "project_name": v.project_name,
        "project_path": v.project_path,
        "project_role": v.project_role,
        "created_at": v.created_at,
        "last_used_at": v.last_used_at,
        "ended_at": v.ended_at,
    }


def _collect_hermes_session_panel(
    ledger: list[HermesSessionView],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the "hermes 세션" dashboard panel from the DB (read-only).

    Joins ``hermes_agent_sessions`` (populated by
    ``worker_control.hermes_session_sync``) with the claude ledger rows that
    point at each hermes session via ``hermes_runs.hermes_session_id``. No
    disk reads — the sync worker is the only path that touches
    ``~/AppData/Local/hermes/profiles/*/sessions/*.json``.

    A row appears for every key present in EITHER source:
      * ``transcript+runs`` — in both: agent session synced, claude runs link to it.
      * ``transcript_only`` — synced agent session, no claude run links yet.
      * ``runs_only``       — orphaned ``hermes_runs.hermes_session_id`` that
                              we don't have a synced agent row for (may
                              indicate the sync worker is behind, or the
                              transcript file was deleted).
    """
    rows_by_hsess: dict[str, list[dict[str, Any]]] = {}
    agent_rows: dict[str, dict[str, Any]] = {}

    try:
        with connect_canonical() as conn:
            # Claude sessions per hermes session (via hermes_runs link).
            try:
                cur = conn.execute(
                    "SELECT s.id AS sess_db_id, s.uuid, s.name, s.project_id, "
                    "       r.hermes_session_id "
                    "FROM hermes_runs r "
                    "JOIN hermes_sessions s ON s.id = r.session_id "
                    "WHERE r.hermes_session_id IS NOT NULL "
                    "GROUP BY s.id, r.hermes_session_id"
                )
                for r in cur.fetchall():
                    rows_by_hsess.setdefault(r["hermes_session_id"], []).append({
                        "claude_session_db_id": r["sess_db_id"],
                        "claude_uuid":          r["uuid"],
                        "claude_name":          r["name"],
                    })
            except Exception:
                pass

            # Agent sessions from the synced table. If the table is missing
            # (sync never ran), we get [] and fall through with whatever
            # runs_only data we have.
            try:
                cur = conn.execute(
                    "SELECT hermes_session_id, profile_name, profile_path, "
                    "       transcript_path, transcript_size, transcript_mtime, "
                    "       started_at, ended_at, model, turn_count, "
                    "       first_message, last_message, cwd, total_cost_usd, "
                    "       synced_at "
                    "FROM hermes_agent_sessions"
                )
                for r in cur.fetchall():
                    agent_rows[r["hermes_session_id"]] = dict(r)
            except Exception:
                pass
    except Exception:
        pass

    all_keys = set(agent_rows) | set(rows_by_hsess)
    rows_out: list[dict[str, Any]] = []
    for sid in all_keys:
        a = agent_rows.get(sid)
        spawned = rows_by_hsess.get(sid, [])
        rows_out.append({
            "hermes_session_id":  sid,
            "profile_name":       a["profile_name"]      if a else None,
            "profile_path":       a["profile_path"]      if a else None,
            "transcript":         a["transcript_path"]   if a else None,
            "transcript_size":    a["transcript_size"]   if a else 0,
            "transcript_mtime":   a["transcript_mtime"]  if a else None,
            "started_at":         a["started_at"]        if a else None,
            "ended_at":           a["ended_at"]          if a else None,
            "model":              a["model"]             if a else None,
            "turn_count":         a["turn_count"]        if a else 0,
            "first_message":      a["first_message"]     if a else None,
            "last_message":       a["last_message"]      if a else None,
            "cwd":                a["cwd"]               if a else None,
            "total_cost_usd":     a["total_cost_usd"]    if a else None,
            "synced_at":          a["synced_at"]         if a else None,
            "discovered_via":     "transcript+runs" if (a and spawned)
                                  else ("transcript_only" if a else "runs_only"),
            "spawned_claudes":    spawned,
            "spawned_count":      len(spawned),
        })
    rows_out.sort(
        key=lambda r: (r["transcript_mtime"] or r["started_at"] or "",
                       r["hermes_session_id"]),
        reverse=True,
    )
    counters = {
        "hermes_agent_sessions":      len(rows_out),
        "hermes_agent_with_spawn":    sum(1 for r in rows_out if r["spawned_count"]),
        "hermes_agent_orphaned_runs": sum(
            1 for r in rows_out if r["discovered_via"] == "runs_only"
        ),
    }
    return rows_out, counters


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

    # native 세션 jsonl 디스커버리는 이제 **보조정보** 로만 본다 (root 경로/
    # 디스커버리 가능 여부/디렉토리 메타). 화면 표시용 행은 모두 단일 ledger
    # (`hermes_sessions` 테이블) 에서 나온다 — 사용자가 호스트의 단일 ledger
    # 를 `scv-` prefix 유무로만 두 탭에 분할하길 원함.
    try:
        native: NativeSnapshot = discover_native_sessions(limit=native_limit)
    except Exception as exc:
        native = NativeSnapshot(
            root="(error)", root_exists=False,
            note=f"native 디스커버리 중 예외: {exc}",
        )

    # ── 단일 ledger (hermes_sessions JOIN hermes_runs JOIN projects) ──────
    # 159 개 행 전체를 한 번 가져와서 `scv-` prefix 유무로 두 분할:
    #   * scv_spawned         → "Hermes 스폰 세션" 탭 (= hermes_sessions 페이로드)
    #   * scv- 가 없는 나머지 → "Native 세션" 탭 (= native_sessions 페이로드)
    # `hermes_ledger_sessions` 라는 별도 카운터/배열은 더 이상 만들지 않는다.
    try:
        ledger: list[HermesSessionView] = list_hermes_sessions()
    except Exception:
        ledger = []
    scv_rows: list[HermesSessionView]    = []
    native_rows: list[HermesSessionView] = []
    for v in ledger:
        if is_spawn(v):
            scv_rows.append(v)
        else:
            native_rows.append(v)

    # Hermes profiles auto-discovered from disk (read-only).
    from worker_control.hermes_profiles import hermes_home as _hh
    home_path = _hh()
    try:
        hermes_profs: list[HermesProfile] = discover_hermes_profiles()
    except Exception:
        hermes_profs = []
    hermes_profs_dicts = [hermes_profile_to_dict(p) for p in hermes_profs]

    # Hermes turn-by-turn sessions + their claude spawn relationships.
    hermes_agent_rows, hermes_agent_counters = _collect_hermes_session_panel(ledger)

    snap = DashboardSnapshot(
        generated_at=utcnow_iso(),
        version=__version__,
        db_path=str(db_path()),
        runtime_root=str(runtime_root()),
        workspace_roots=roots,
        profiles=[_profile_to_dict(p) for p in profiles],
        hermes_profiles=hermes_profs_dicts,
        hermes_home=str(home_path),
        hermes_home_exists=home_path.is_dir(),
        projects=[_project_to_dict(p) for p in projs],
        # 두 탭의 페이로드 — 같은 ledger 테이블의 행을 prefix 로 분할:
        hermes_sessions=[_hermes_ledger_to_dict(v) for v in scv_rows],
        native_sessions=[_hermes_ledger_to_dict(v) for v in native_rows],
        hermes_agent_sessions=hermes_agent_rows,
        native_root=native.root,
        native_root_exists=native.root_exists,
        native_note=native.note,
        counters={
            "profiles": len(profiles),
            "hermes_profiles": len(hermes_profs),
            "projects": len(projs),
            "projects_owned": sum(
                1 for p in projs if p.root_role == ROLE_OWNED_WORK
            ),
            "projects_public": sum(
                1 for p in projs if p.root_role == ROLE_PUBLIC_REFERENCE
            ),
            "projects_git": sum(1 for p in projs if p.is_git),
            "projects_dirty": sum(1 for p in projs if p.is_dirty),
            # 세션 사용 형태 카운터 (전체 ledger 159 기준).
            # `hermes_session_counters` 가 hermes_spawned/print_spawned/
            # prefix_spawned/interactive_multi/native + ledger_total 을 한꺼번에
            # 채워준다. 탭 분배는 그 결과의 `hermes_spawned` 가 곧 scv_rows 길이.
            **hermes_session_counters(ledger),
            # 탭 분배 결과 (`hermes_spawned` 와 같지만 시맨틱 명확화 위해 별도 키)
            "hermes_sessions": len(scv_rows),
            "native_sessions": len(native_rows),
            # 보조: 호스트 디스크 jsonl 파일 카운트 (디버그용, ledger 와 무관)
            "native_jsonl_files": len(native.sessions),
            **hermes_agent_counters,
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
        "hermes_profiles": snap.hermes_profiles,
        "hermes_home": snap.hermes_home,
        "hermes_home_exists": snap.hermes_home_exists,
        "projects": snap.projects,
        "hermes_sessions": snap.hermes_sessions,
        "native_sessions": snap.native_sessions,
        "hermes_agent_sessions": snap.hermes_agent_sessions,
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
