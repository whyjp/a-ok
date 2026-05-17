"""HTML 대시보드 — worker-control 상태를 한 페이지로 보여준다.

레이어:

1. **워커 프로파일** — ``worker_profiles`` 테이블.
2. **Hermes 스폰 세션** — ``worker_sessions`` 테이블 (이 도구가 띄운 워커).
3. **Native Claude 세션** — ``~/.claude/projects/`` 의 JSONL 파일 (read-only).
4. **관리 대상 프로젝트** — ``projects`` 테이블 (스캔 결과).

대시보드는 단일 정적 HTML 로 만들어지며 오프라인 브라우저에서 동작한다.
스타일/JS 는 모두 인라인. 의존성 없음.
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


# ----- HTML 렌더 -------------------------------------------------------------

def _snapshot_to_json(snap: DashboardSnapshot) -> str:
    """대시보드 JS 에 그대로 박을 JSON 문자열.

    ``</script>`` 끼어 들지 않도록 처리한다 (XSS 방지).
    """
    payload = {
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
    raw = json.dumps(payload, ensure_ascii=False)
    # 인라인 <script> 안에 안전하게 들어가도록 sentinel 치환
    return (
        raw.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>worker-control · 대시보드</title>
<style>
:root {
  --bg-0: #07080c;
  --bg-1: #0d111a;
  --bg-2: #131826;
  --bg-3: #1a2030;
  --line: rgba(255,255,255,0.07);
  --line-strong: rgba(255,255,255,0.14);
  --text-0: #f4f6fb;
  --text-1: #c4cbdb;
  --text-2: #828aa0;
  --text-3: #565d72;
  --accent: #7cf2c3;
  --accent-dim: rgba(124,242,195,0.15);
  --warn: #ffb066;
  --warn-dim: rgba(255,176,102,0.13);
  --danger: #ff6b80;
  --danger-dim: rgba(255,107,128,0.13);
  --info: #7eb6ff;
  --info-dim: rgba(126,182,255,0.13);
  --muted: #4f5468;
  --shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 24px 60px -32px rgba(0,0,0,0.6);
  --mono: "JetBrains Mono", "SFMono-Regular", ui-monospace, Consolas, "Liberation Mono", monospace;
  --sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans KR",
          "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.55;
  color: var(--text-1);
  background:
    radial-gradient(1200px 700px at 100% -10%, rgba(124,242,195,0.07), transparent 60%),
    radial-gradient(900px 500px at -10% 110%, rgba(126,182,255,0.07), transparent 60%),
    var(--bg-0);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--info); text-decoration: none; }
a:hover { text-decoration: underline; }
code, .mono { font-family: var(--mono); }

.shell {
  max-width: 1320px;
  margin: 0 auto;
  padding: 32px 28px 96px;
}

/* --- header --- */
.header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  padding-bottom: 22px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 24px;
}
.brand { display: flex; align-items: center; gap: 14px; }
.brand-mark {
  width: 44px; height: 44px; border-radius: 12px;
  background: linear-gradient(135deg, #1a2030, #07080c);
  border: 1px solid var(--line-strong);
  display: grid; place-items: center;
  box-shadow: var(--shadow);
}
.brand-mark::before {
  content: "";
  width: 18px; height: 18px;
  border: 1.5px solid var(--accent);
  border-right-color: transparent;
  border-bottom-color: transparent;
  transform: rotate(-45deg);
  border-radius: 4px;
}
.brand-text h1 {
  margin: 0; font-size: 18px; letter-spacing: 0.02em;
  color: var(--text-0); font-weight: 600;
}
.brand-text .sub {
  margin-top: 2px; font-size: 12px; color: var(--text-2);
  font-family: var(--mono);
}
.meta {
  display: flex; gap: 18px; flex-wrap: wrap; justify-content: flex-end;
  font-size: 12px; color: var(--text-2);
  font-family: var(--mono);
}
.meta div span { color: var(--text-0); }
.meta .refresh { color: var(--accent); }

/* --- summary cards --- */
.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 14px;
  margin-bottom: 22px;
}
.card {
  background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 16px 18px;
  position: relative;
  overflow: hidden;
  box-shadow: var(--shadow);
}
.card .label {
  font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--text-3);
}
.card .value {
  font-family: var(--mono); font-size: 26px; color: var(--text-0);
  margin-top: 8px; letter-spacing: -0.01em;
}
.card .hint { font-size: 11px; color: var(--text-2); margin-top: 4px; }
.card.accent .value { color: var(--accent); }
.card.warn .value { color: var(--warn); }
.card.info .value { color: var(--info); }
.card.danger .value { color: var(--danger); }

/* --- tabs --- */
.tabs {
  display: flex; gap: 4px; padding: 4px;
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: 12px;
  margin-bottom: 18px;
  width: max-content;
  max-width: 100%;
  overflow-x: auto;
}
.tab {
  background: transparent; color: var(--text-2);
  border: 0; padding: 9px 14px; border-radius: 8px;
  font: inherit; cursor: pointer;
  display: inline-flex; align-items: center; gap: 8px;
  white-space: nowrap;
}
.tab:hover { color: var(--text-0); background: var(--bg-2); }
.tab[aria-selected="true"] {
  color: var(--text-0);
  background: linear-gradient(180deg, var(--bg-3), var(--bg-2));
  box-shadow: inset 0 0 0 1px var(--line-strong);
}
.tab .pill {
  font-family: var(--mono); font-size: 11px; color: var(--text-2);
  padding: 1px 7px; border-radius: 999px;
  background: var(--bg-3); border: 1px solid var(--line);
}

/* --- panels --- */
.panel { display: none; }
.panel[aria-current="true"] { display: block; }
.panel-head {
  display: flex; gap: 12px; flex-wrap: wrap;
  align-items: center; justify-content: space-between;
  margin-bottom: 12px;
}
.panel-title { font-size: 16px; color: var(--text-0); font-weight: 600; }
.panel-desc { color: var(--text-2); font-size: 12px; }
.panel-tools {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
}
.search {
  background: var(--bg-1);
  border: 1px solid var(--line);
  color: var(--text-0);
  padding: 8px 12px; border-radius: 9px;
  font: inherit; min-width: 220px;
}
.search::placeholder { color: var(--text-3); }
.search:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}
.select {
  background: var(--bg-1);
  border: 1px solid var(--line);
  color: var(--text-0);
  padding: 8px 10px; border-radius: 9px;
  font: inherit;
}
.select:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}

/* --- tables / lists --- */
.table-wrap {
  background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
  border: 1px solid var(--line);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: var(--shadow);
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
thead th {
  text-align: left;
  font-weight: 500;
  color: var(--text-2);
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: rgba(255,255,255,0.015);
  white-space: nowrap;
}
tbody td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
  color: var(--text-1);
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: rgba(255,255,255,0.025); }
tbody td.mono, thead th.mono { font-family: var(--mono); font-size: 12px; }
.empty {
  padding: 36px 18px;
  text-align: center;
  color: var(--text-2);
  font-size: 13px;
}

/* --- badges --- */
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  background: var(--bg-3);
  color: var(--text-1);
  font-family: var(--mono);
  white-space: nowrap;
}
.badge::before {
  content: ""; width: 6px; height: 6px; border-radius: 999px;
  background: currentColor; opacity: 0.85;
}
.badge.accent { color: var(--accent); background: var(--accent-dim); border-color: transparent; }
.badge.warn   { color: var(--warn);   background: var(--warn-dim);   border-color: transparent; }
.badge.info   { color: var(--info);   background: var(--info-dim);   border-color: transparent; }
.badge.danger { color: var(--danger); background: var(--danger-dim); border-color: transparent; }
.badge.muted  { color: var(--text-3); }
.badge.plain::before { display: none; }

.policy-pill {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; padding: 3px 9px; border-radius: 7px;
  border: 1px solid var(--line-strong); font-family: var(--mono);
}
.policy-pill.work { color: var(--accent); border-color: transparent; background: var(--accent-dim); }
.policy-pill.ro   { color: var(--warn);   border-color: transparent; background: var(--warn-dim); }
.policy-pill.other{ color: var(--text-2); }

.path { color: var(--text-2); font-family: var(--mono); font-size: 12px; word-break: break-all; }

.footnote {
  margin-top: 28px;
  color: var(--text-3);
  font-size: 11px;
  font-family: var(--mono);
}
.notice {
  background: var(--warn-dim);
  border: 1px solid transparent;
  color: var(--warn);
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 12px;
  margin-bottom: 14px;
}
.kbd {
  font-family: var(--mono); font-size: 11px;
  padding: 1px 6px; border-radius: 5px;
  background: var(--bg-3); border: 1px solid var(--line-strong);
  color: var(--text-0);
}
.roots {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 10px; margin-bottom: 18px;
}
.root-card {
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px 14px;
}
.root-card .role {
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--text-3);
}
.root-card .root-path {
  font-family: var(--mono); color: var(--text-0); margin-top: 4px;
  word-break: break-all;
}
.root-card .row {
  margin-top: 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
}
</style>
</head>
<body>
<main class="shell">

  <header class="header">
    <div class="brand">
      <div class="brand-mark"></div>
      <div class="brand-text">
        <h1>worker-control · 컨트롤 패널</h1>
        <div class="sub">로컬 Claude Code 워커 관제 — 정적 HTML 스냅샷</div>
      </div>
    </div>
    <div class="meta">
      <div>버전 <span class="mono" id="m-version">—</span></div>
      <div>DB <span class="mono" id="m-db">—</span></div>
      <div>런타임 <span class="mono" id="m-runtime">—</span></div>
      <div class="refresh">생성 <span class="mono" id="m-generated">—</span></div>
    </div>
  </header>

  <section class="roots" id="roots"></section>

  <section class="summary" id="summary"></section>

  <div class="tabs" role="tablist" id="tabs">
    <button class="tab" role="tab" data-target="workers" aria-selected="true">
      워커 프로파일 <span class="pill" id="c-workers">0</span>
    </button>
    <button class="tab" role="tab" data-target="hermes">
      Hermes 스폰 세션 <span class="pill" id="c-hermes">0</span>
    </button>
    <button class="tab" role="tab" data-target="native">
      Native 세션 <span class="pill" id="c-native">0</span>
    </button>
    <button class="tab" role="tab" data-target="projects">
      관리 대상 프로젝트 <span class="pill" id="c-projects">0</span>
    </button>
  </div>

  <!-- 워커 프로파일 패널 -->
  <section class="panel" id="p-workers" role="tabpanel" aria-current="true">
    <div class="panel-head">
      <div>
        <div class="panel-title">워커 프로파일</div>
        <div class="panel-desc">
          <code>workerctl profiles</code> 가 관리하는 워커 실행 프로파일 목록.
          각 프로파일은 워커가 어떤 루트에서 기동될지를 묶는 라벨이다.
        </div>
      </div>
      <div class="panel-tools">
        <input class="search" type="search" placeholder="이름/루트로 검색"
               data-filter="workers" />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>이름</th><th>기본 루트</th>
            <th>생성</th><th>업데이트</th>
          </tr>
        </thead>
        <tbody id="t-workers"></tbody>
      </table>
    </div>
  </section>

  <!-- Hermes 세션 패널 -->
  <section class="panel" id="p-hermes" role="tabpanel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Hermes 스폰 세션</div>
        <div class="panel-desc">
          worker-control 이 직접 띄운 (= Hermes 오케스트레이션 대상) Claude 워커
          세션. <code>worker_sessions</code> 테이블 기준.
          모두 <code>claude --permission-mode auto</code> 일반 모드로 기동된다.
        </div>
      </div>
      <div class="panel-tools">
        <select class="select" data-filter-state="hermes">
          <option value="">모든 상태</option>
          <option>starting</option>
          <option>running</option>
          <option>waiting_input</option>
          <option>working</option>
          <option>blocked</option>
          <option>completed</option>
          <option>failed</option>
          <option>killed</option>
        </select>
        <input class="search" type="search"
               placeholder="이름/프로젝트/프로파일 검색"
               data-filter="hermes" />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>이름</th><th>상태</th><th>runtime</th>
            <th>프로파일</th><th>프로젝트</th>
            <th>tmux / pid</th><th>started</th>
          </tr>
        </thead>
        <tbody id="t-hermes"></tbody>
      </table>
    </div>
  </section>

  <!-- Native 세션 패널 -->
  <section class="panel" id="p-native" role="tabpanel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Native Claude 세션</div>
        <div class="panel-desc">
          이 호스트의 <code>~/.claude/projects/</code> 아래에 남아있는 Claude
          CLI 세션 로그. <strong>읽기 전용</strong> 으로 디스커버리한다 — 파일
          위치/포맷이 호스트마다 다를 수 있으므로 휴리스틱으로 인코딩을 푼다.
        </div>
      </div>
      <div class="panel-tools">
        <input class="search" type="search"
               placeholder="세션 ID / 경로 / permissionMode 검색"
               data-filter="native" />
      </div>
    </div>
    <div id="native-notice"></div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>세션 ID</th><th>프로젝트 (추정)</th><th>permissionMode</th>
            <th>줄 수</th><th>크기</th><th>수정</th>
          </tr>
        </thead>
        <tbody id="t-native"></tbody>
      </table>
    </div>
    <div class="footnote">
      native 세션 디렉토리:
      <code id="native-root">—</code> ·
      덮어쓰기: <span class="kbd">WORKER_CONTROL_CLAUDE_PROJECTS_DIR</span>
    </div>
  </section>

  <!-- 프로젝트 패널 -->
  <section class="panel" id="p-projects" role="tabpanel">
    <div class="panel-head">
      <div>
        <div class="panel-title">관리 대상 프로젝트</div>
        <div class="panel-desc">
          <code>workerctl projects scan</code> 결과. owned_work
          (<code>D:/work-github</code>) 는 워커 기동/PR 가능, public_reference
          (<code>D:/github</code>) 는 <strong>read-only</strong> — 세션 시작이
          정책으로 차단된다.
        </div>
      </div>
      <div class="panel-tools">
        <select class="select" data-filter-role="projects">
          <option value="">모든 워크스페이스</option>
          <option value="owned_work">owned_work (편집/PR 가능)</option>
          <option value="public_reference">public_reference (read-only)</option>
          <option value="other">other</option>
        </select>
        <select class="select" data-filter-git="projects">
          <option value="">git/비-git 모두</option>
          <option value="git">git 저장소만</option>
          <option value="dirty">dirty 만</option>
          <option value="clean">clean git 만</option>
        </select>
        <input class="search" type="search"
               placeholder="이름/브랜치/경로 검색"
               data-filter="projects" />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th><th>이름</th><th>워크스페이스</th><th>정책</th>
            <th>git</th><th>브랜치</th><th>경로</th><th>마지막 스캔</th>
          </tr>
        </thead>
        <tbody id="t-projects"></tbody>
      </table>
    </div>
  </section>

  <div class="footnote" id="footnote">
    정책: D:/work-github (owned_work, 편집/PR 가능) ·
    D:/github (public_reference, read-only · 워커 기동 금지).
    워커는 항상 <span class="mono">claude --permission-mode auto</span> 로 기동되며
    <span class="mono">claude -p</span> 는 정책상 사용되지 않는다.
  </div>

</main>

<script id="dashboard-data" type="application/json">__DATA__</script>
<script>
(function() {
  const raw = document.getElementById("dashboard-data").textContent;
  let data;
  try { data = JSON.parse(raw); }
  catch (e) {
    document.body.innerHTML =
      "<pre style='color:#ff6b80;padding:24px'>dashboard data parse failed: "
      + (e && e.message) + "</pre>";
    return;
  }

  // --- header meta ---
  setText("m-version", "v" + (data.version || "?"));
  setText("m-db", data.db_path || "—");
  setText("m-runtime", data.runtime_root || "—");
  setText("m-generated", data.generated_at || "—");

  // --- workspace roots ---
  const rootsEl = document.getElementById("roots");
  rootsEl.innerHTML = (data.workspace_roots || []).map(function(r) {
    const policy = r.writable
      ? "<span class='policy-pill work'>WRITE · 워커 기동 가능</span>"
      : "<span class='policy-pill ro'>READ-ONLY · 세션 시작 금지</span>";
    const exists = r.exists
      ? "<span class='badge accent plain'>exists</span>"
      : "<span class='badge danger plain'>missing</span>";
    return ""
      + "<div class='root-card'>"
      +   "<div class='role'>" + escapeHtml(r.role) + "</div>"
      +   "<div class='root-path'>" + escapeHtml(r.path) + "</div>"
      +   "<div class='row'>" + policy + exists + "</div>"
      + "</div>";
  }).join("");

  // --- summary cards ---
  const c = data.counters || {};
  const cards = [
    { label: "프로파일",       value: c.profiles,        cls: "" },
    { label: "프로젝트 (전체)", value: c.projects,        cls: "info" },
    { label: "owned_work",    value: c.projects_owned,  cls: "accent",
      hint: "편집/PR 가능" },
    { label: "public_reference", value: c.projects_public, cls: "warn",
      hint: "read-only" },
    { label: "git 저장소",     value: c.projects_git,    cls: "" },
    { label: "dirty",         value: c.projects_dirty,  cls: c.projects_dirty ? "danger" : "" },
    { label: "Hermes 세션",   value: c.hermes_sessions, cls: "info" },
    { label: "running",       value: c.hermes_running,  cls: c.hermes_running ? "accent" : "" },
    { label: "Native 세션",   value: c.native_sessions, cls: "" }
  ];
  document.getElementById("summary").innerHTML = cards.map(function(card) {
    return ""
      + "<div class='card " + card.cls + "'>"
      +   "<div class='label'>" + escapeHtml(card.label) + "</div>"
      +   "<div class='value'>" + (card.value != null ? card.value : "—") + "</div>"
      +   (card.hint ? "<div class='hint'>" + escapeHtml(card.hint) + "</div>" : "")
      + "</div>";
  }).join("");

  // --- tab counts ---
  setText("c-workers",  c.profiles);
  setText("c-hermes",   c.hermes_sessions);
  setText("c-native",   c.native_sessions);
  setText("c-projects", c.projects);

  // --- tab switching ---
  const tabs = Array.from(document.querySelectorAll(".tab"));
  tabs.forEach(function(t) {
    t.addEventListener("click", function() {
      tabs.forEach(function(x) { x.setAttribute("aria-selected", "false"); });
      t.setAttribute("aria-selected", "true");
      document.querySelectorAll(".panel").forEach(function(p) {
        p.setAttribute("aria-current", "false");
      });
      document.getElementById("p-" + t.dataset.target)
        .setAttribute("aria-current", "true");
    });
  });

  // --- renderers ---
  function renderWorkers(filterText) {
    const ft = (filterText || "").toLowerCase();
    const rows = (data.profiles || []).filter(function(p) {
      if (!ft) return true;
      return (p.name + " " + p.root_path).toLowerCase().indexOf(ft) >= 0;
    });
    const tbody = document.getElementById("t-workers");
    if (!rows.length) {
      tbody.innerHTML = emptyRow(5,
        "프로파일이 없습니다. `workerctl profiles create &lt;name&gt;` 로 만드세요.");
      return;
    }
    tbody.innerHTML = rows.map(function(p) {
      return ""
        + "<tr>"
        +   "<td class='mono'>" + p.id + "</td>"
        +   "<td><strong>" + escapeHtml(p.name) + "</strong></td>"
        +   "<td><span class='path'>" + escapeHtml(p.root_path || "") + "</span></td>"
        +   "<td class='mono'>" + escapeHtml(p.created_at || "—") + "</td>"
        +   "<td class='mono'>" + escapeHtml(p.updated_at || "—") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderHermes(filterText, stateFilter) {
    const ft = (filterText || "").toLowerCase();
    const sf = (stateFilter || "").trim();
    const rows = (data.hermes_sessions || []).filter(function(s) {
      if (sf && s.state !== sf) return false;
      if (!ft) return true;
      const blob = [s.name, s.profile_name, s.project_name, s.project_path,
                    s.tmux_session, s.runtime, s.state].join(" ").toLowerCase();
      return blob.indexOf(ft) >= 0;
    });
    const tbody = document.getElementById("t-hermes");
    if (!rows.length) {
      tbody.innerHTML = emptyRow(8,
        "Hermes 가 띄운 세션이 없습니다. `workerctl sessions start --profile X --project Y`");
      return;
    }
    tbody.innerHTML = rows.map(function(s) {
      const stateBadge = badgeForState(s.state);
      const runtimeBadge = "<span class='badge " + (s.runtime === "tmux" ? "info" : "muted")
        + " plain'>" + escapeHtml(s.runtime || "—") + "</span>";
      return ""
        + "<tr>"
        +   "<td class='mono'>" + s.id + "</td>"
        +   "<td><strong>" + escapeHtml(s.name) + "</strong></td>"
        +   "<td>" + stateBadge + "</td>"
        +   "<td>" + runtimeBadge + "</td>"
        +   "<td>" + escapeHtml(s.profile_name || "—") + "</td>"
        +   "<td>"
        +     "<div>" + escapeHtml(s.project_name || "—") + "</div>"
        +     (s.project_path ? "<div class='path'>" + escapeHtml(s.project_path) + "</div>" : "")
        +   "</td>"
        +   "<td class='mono'>"
        +     (s.tmux_session ? escapeHtml(s.tmux_session) : "<span class='path'>(no tmux)</span>")
        +     (s.pid ? "<div class='path'>pid " + s.pid + "</div>" : "")
        +   "</td>"
        +   "<td class='mono'>" + escapeHtml(s.started_at || "—") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderNative(filterText) {
    const ft = (filterText || "").toLowerCase();
    const noticeEl = document.getElementById("native-notice");
    if (data.native_note) {
      noticeEl.innerHTML = "<div class='notice'>" + escapeHtml(data.native_note) + "</div>";
    } else {
      noticeEl.innerHTML = "";
    }
    setText("native-root", data.native_root || "—");
    const rows = (data.native_sessions || []).filter(function(s) {
      if (!ft) return true;
      const blob = [s.session_id, s.project_path_guess, s.project_dir_name,
                    s.permission_mode, s.jsonl_path].join(" ").toLowerCase();
      return blob.indexOf(ft) >= 0;
    });
    const tbody = document.getElementById("t-native");
    if (!rows.length) {
      tbody.innerHTML = emptyRow(6,
        data.native_root_exists
          ? "해당하는 native 세션이 없습니다."
          : "native 세션 디렉토리가 없거나 비어 있습니다.");
      return;
    }
    tbody.innerHTML = rows.map(function(s) {
      const pmodeBadge = s.permission_mode
        ? "<span class='badge " + (s.permission_mode === "auto" ? "accent" : "warn")
            + " plain'>" + escapeHtml(s.permission_mode) + "</span>"
        : "<span class='badge muted plain'>—</span>";
      return ""
        + "<tr>"
        +   "<td class='mono'>" + escapeHtml(s.session_id) + "</td>"
        +   "<td>"
        +     "<div>" + escapeHtml(s.project_path_guess) + "</div>"
        +     "<div class='path'>" + escapeHtml(s.project_dir_name) + "</div>"
        +   "</td>"
        +   "<td>" + pmodeBadge + "</td>"
        +   "<td class='mono'>" + (s.line_count != null ? s.line_count : "—") + "</td>"
        +   "<td class='mono'>" + humanBytes(s.size_bytes) + "</td>"
        +   "<td class='mono'>" + escapeHtml(s.modified_at || "—") + "</td>"
        + "</tr>";
    }).join("");
  }

  function renderProjects(filterText, roleFilter, gitFilter) {
    const ft = (filterText || "").toLowerCase();
    const rf = (roleFilter || "").trim();
    const gf = (gitFilter || "").trim();
    const rows = (data.projects || []).filter(function(p) {
      if (rf && p.root_role !== rf) return false;
      if (gf === "git" && !p.is_git) return false;
      if (gf === "dirty" && !(p.is_git && p.is_dirty)) return false;
      if (gf === "clean" && !(p.is_git && !p.is_dirty)) return false;
      if (!ft) return true;
      const blob = [p.name, p.branch || "", p.path, p.remote_url || ""].join(" ").toLowerCase();
      return blob.indexOf(ft) >= 0;
    });
    const tbody = document.getElementById("t-projects");
    if (!rows.length) {
      tbody.innerHTML = emptyRow(8,
        "해당하는 프로젝트가 없습니다. `workerctl projects scan` 후 다시 시도하세요.");
      return;
    }
    tbody.innerHTML = rows.map(function(p) {
      const roleBadge = badgeForRole(p.root_role);
      const policyPill = p.policy === "work_capable"
        ? "<span class='policy-pill work'>WRITE/PR</span>"
        : "<span class='policy-pill ro'>READ-ONLY</span>";
      const gitBadge = p.is_git
        ? (p.is_dirty
            ? "<span class='badge danger plain'>git · dirty</span>"
            : "<span class='badge accent plain'>git · clean</span>")
        : "<span class='badge muted plain'>—</span>";
      return ""
        + "<tr>"
        +   "<td class='mono'>" + p.id + "</td>"
        +   "<td><strong>" + escapeHtml(p.name) + "</strong></td>"
        +   "<td>" + roleBadge + "</td>"
        +   "<td>" + policyPill + "</td>"
        +   "<td>" + gitBadge + "</td>"
        +   "<td class='mono'>" + escapeHtml(p.branch || "—") + "</td>"
        +   "<td><span class='path'>" + escapeHtml(p.path || "") + "</span>"
        +     (p.remote_url ? "<div class='path'>" + escapeHtml(p.remote_url) + "</div>" : "") + "</td>"
        +   "<td class='mono'>" + escapeHtml(p.last_scan_at || "—") + "</td>"
        + "</tr>";
    }).join("");
  }

  // --- filter wiring ---
  const workersSearch = document.querySelector("[data-filter='workers']");
  workersSearch && workersSearch.addEventListener("input", function() {
    renderWorkers(workersSearch.value);
  });

  const hermesSearch = document.querySelector("[data-filter='hermes']");
  const hermesState  = document.querySelector("[data-filter-state='hermes']");
  function refreshHermes() { renderHermes(hermesSearch.value, hermesState.value); }
  hermesSearch && hermesSearch.addEventListener("input", refreshHermes);
  hermesState  && hermesState.addEventListener("change", refreshHermes);

  const nativeSearch = document.querySelector("[data-filter='native']");
  nativeSearch && nativeSearch.addEventListener("input", function() {
    renderNative(nativeSearch.value);
  });

  const projSearch = document.querySelector("[data-filter='projects']");
  const projRole   = document.querySelector("[data-filter-role='projects']");
  const projGit    = document.querySelector("[data-filter-git='projects']");
  function refreshProjects() {
    renderProjects(projSearch.value, projRole.value, projGit.value);
  }
  projSearch && projSearch.addEventListener("input", refreshProjects);
  projRole   && projRole.addEventListener("change", refreshProjects);
  projGit    && projGit.addEventListener("change", refreshProjects);

  // --- initial paint ---
  renderWorkers("");
  renderHermes("", "");
  renderNative("");
  renderProjects("", "", "");

  // --- helpers ---
  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value == null ? "—" : String(value);
  }
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function emptyRow(colspan, msg) {
    return "<tr><td class='empty' colspan='" + colspan + "'>"
      + escapeHtml(msg) + "</td></tr>";
  }
  function humanBytes(n) {
    if (n == null) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024*1024) return (n/1024).toFixed(1) + " KiB";
    return (n/1024/1024).toFixed(2) + " MiB";
  }
  function badgeForState(state) {
    const cls = ({
      "running":       "accent",
      "working":       "accent",
      "starting":      "info",
      "waiting_input": "warn",
      "blocked":       "warn",
      "failed":        "danger",
      "killed":        "danger",
      "completed":     "muted"
    })[state] || "muted";
    return "<span class='badge " + cls + " plain'>" + escapeHtml(state || "—") + "</span>";
  }
  function badgeForRole(role) {
    const cls = role === "owned_work" ? "accent"
              : role === "public_reference" ? "warn" : "muted";
    return "<span class='badge " + cls + " plain'>" + escapeHtml(role || "other") + "</span>";
  }
})();
</script>
</body>
</html>
"""


def render_html(snap: DashboardSnapshot) -> str:
    """``DashboardSnapshot`` → 단일 HTML 문자열."""
    data_json = _snapshot_to_json(snap)
    return _HTML_TEMPLATE.replace("__DATA__", data_json)


def default_output_path() -> Path:
    """기본 출력 경로 — 런타임 루트 아래 ``dashboard.html``."""
    return runtime_root() / DEFAULT_OUTPUT_FILENAME


def write_dashboard(
    output: Path | str | None = None,
    *,
    native_limit: int | None = 500,
) -> Path:
    """HTML 대시보드 파일을 작성하고 그 경로를 돌려준다.

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
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False
