# Architecture

## 목표

- 두 워크스페이스(`D:/work-github` = owned_work, `D:/github` = public_reference)
  의 프로젝트들과 Claude Code 워커 세션을 1:N 으로 묶어 로컬 SQLite 에
  등록·관찰한다.
- 외부 오케스트레이터에 의존하지 않고 단독 CLI(`workerctl`) 로 동작한다.
- **쓰기·세션 기동은 owned_work 루트에서만** 일어난다. public_reference
  루트는 스캔/조회만 허용한다.

## 워크스페이스 정책 (핵심)

```
+-------------------------+        +-----------------------------+
|  D:/work-github         |        |  D:/github                  |
|  role = owned_work      |        |  role = public_reference    |
|  편집/PR/푸시/워커 기동 |        |  read-only (참조용)         |
+-------------------------+        +-----------------------------+
            |                                   |
            +----------------+------------------+
                             v
                  +---------------------+
                  |  configured_roots() |
                  |  (paths.py)         |
                  +---------------------+
                             |
                             v
                  +---------------------+
                  |  scanner.scan_root  |  → projects.root_role
                  +---------------------+
                             |
                             v
                  +---------------------+
                  |  sessions.start     |  ← role=public_reference 면
                  |  (정책 가드)        |    WriteProtectedRootError
                  +---------------------+
```

## 컴포넌트

```
+--------------------+        +-------------------------+
|  workerctl CLI     |  -->   |  worker_control 패키지   |
+--------------------+        +-------------------------+
                                       |
                                       v
                          +----------------------------------+
                          |  SQLite (worker-control)         |
                          |   D:/work-github/.worker-control |
                          +----------------------------------+
                                       |
                                       v
                          +----------------------------+
                          |  runtime: tmux / 새 콘솔   |
                          |  (claude 일반 모드)        |
                          +----------------------------+
```

### CLI (`worker_control.cli`)
argparse 기반. 외부 의존성 없음. 서브커맨드 트리:

- `init`
- `profiles list | create`
- `projects scan | list`
- `sessions list | start | capture | prompt | stop`
- `view serve` (alias `dashboard`) — SQLite 기반 BFF + 정적 FE 서빙
- `view html --legacy` — [LEGACY] BFF 없이 단일 HTML 스냅샷 export

`projects scan` 은 `--root` 가 없으면 `configured_roots()` 의 모든 루트를
순회한다. `--root` 가 명시되면 그 루트만 스캔한다.

### DB (`worker_control.db`)
표준 라이브러리 `sqlite3` 만 사용. 부모 디렉토리 자동 생성.

테이블:
- `worker_profiles` — 워커 프로파일 (실행 옵션, 기본 root 등)
- `projects` — 스캔된 git 저장소 (`root_role` 컬럼 포함)
- `worker_sessions` — 워커 세션 인스턴스
- `session_events` — 이벤트 타임라인 (state 전이, capture, prompt, stop)
- `worker_commands` — 세션에 보낸 명령 (재현 가능성)
- `project_scans` — 스캔 실행 메타 (언제, 어디서, 몇 개, `root_role`)

각 행은 `created_at`/`updated_at` (UTC ISO8601) 와 자유 형식 JSON `metadata`
컬럼을 갖는다. 구 스키마(`root_role` 없음) 와는 자동 `ALTER TABLE` 마이그레이션
으로 호환된다.

### Paths / 정책 (`worker_control.paths`)
- `owned_work_root()` → 기본 `D:/work-github` (env: `WORKER_CONTROL_PROJECT_ROOT`)
- `public_reference_root()` → 기본 `D:/github` (env: `WORKER_CONTROL_PUBLIC_REFERENCE_ROOT`)
- `configured_roots()` → 두 루트를 role 과 함께 반환
- `classify_path(p)` → 어느 워크스페이스 자식인지 분류 → `owned_work` /
  `public_reference` / `other`
- `is_writable_project_path(p)` → 정책상 워커 기동 가능 여부

### Scanner (`worker_control.scanner`)
- `--root` 의 1단계 자식 디렉토리만 본다.
- `.git` 존재 여부를 자체 판별 (디렉토리 또는 파일 형태 모두 지원 — worktree 대응).
- git 저장소면 `git rev-parse --abbrev-ref HEAD`, `git remote get-url origin`,
  `git status --porcelain` 으로 branch/remote/dirty 를 직접 수집.
- `classify_path()` 결과(`root_role`) 를 함께 영속화.
- 결과를 `projects` 에 upsert 하고 스캔 메타를 `project_scans` 에 적재.
- `scan_all_configured_roots()` 헬퍼는 두 루트를 순회한다.

### Runtime (`worker_control.runtime`)
- `shutil.which("tmux")` 로 tmux 가용성 판별.
- 워커 argv 는 `claude_argv()` 한 곳에서 만든다 — 기본 `[CLAUDE_BIN, "--permission-mode", "auto"]`.
- tmux 가 있으면 `tmux new-session -d -s <session-name> -c <cwd> claude --permission-mode auto`.
- tmux 가 없으면 OS 별 새 콘솔 창에서 `claude --permission-mode auto` 기동
  (Windows: `start "" cmd /k claude --permission-mode auto`,
  POSIX: `xterm -e claude --permission-mode auto`).
- **절대 `claude -p` / `--print` 사용 금지.** `claude_argv()` 가
  `FORBIDDEN_CLAUDE_ARGS` 에 걸리면 `RuntimeError` 로 정책 위반을 알린다.
- 시작 PID/세션명을 DB 에 저장.

### Dashboard data layer / Native discovery (`worker_control.dashboard`, `worker_control.native_sessions`)
- `dashboard.collect_snapshot()` 가 DB 의 모든 레이어(프로파일/프로젝트/
  Hermes 세션) 를 모으고, `native_sessions.discover_native_sessions()` 가
  `~/.claude/projects/` 의 JSONL 파일을 **read-only** 로 디스커버리한다.
- `dashboard.snapshot_to_payload()` 는 스냅샷을 JSON 직렬화 가능한 dict 로
  바꾼다. BFF 의 `/api/snapshot` 응답과 레거시 인라인 export 가 같은 함수를
  쓴다.
- `dashboard.static_dashboard_html()` 는 패키지에 묶여 있는 정적 FE
  자산(`worker_control/static/dashboard.html`) 을 그대로 반환한다. BFF 가
  `GET /` 응답으로 이 파일을 보낸다.
- `dashboard.render_html()` (**LEGACY**) 는 정적 자산의 placeholder
  `"__INLINE_DATA__"` 위치에 스냅샷 JSON 을 박아 단일 HTML 을 반환한다 —
  오프라인 첨부용으로만 사용한다.
- 디렉토리명 디코딩(`D--work-github-worker-control` →
  `D:/work-github/worker-control`) 은 `configured_roots()` 의 실제 자식
  디렉토리와 매칭해서 원본 하이픈을 살린다. 매칭 실패 시 단순 `-→/` 폴백.

### FE 자산 (`worker_control/static/dashboard.html`)
- CSS/JS 가 모두 인라인된 단일 HTML. 외부 네트워크/폰트/스크립트 의존성 없음.
- 부팅 시 `<script id="dashboard-data">` 의 내용을 파싱한다:
  - 객체면 = 레거시 인라인 export → 그 값으로 한 번 그린다 (polling 없음).
  - placeholder 그대로면 = BFF 모드 → `fetch('/api/snapshot')` 으로 데이터를
    가져오고 5 초 주기로 polling 한다.
  - `file://` + 인라인 없음이면 안내 배너만 보여주고 멈춘다.

### BFF (`worker_control.server`)
- stdlib `http.server.ThreadingHTTPServer` 기반 백엔드. 외부 의존성 0.
- `GET /` — 정적 FE 자산을 그대로 응답 (요청마다 디스크에서 다시 읽음).
- `GET /api/snapshot` — 요청마다 `collect_snapshot()` 을 재호출해 JSON 응답.
  즉 `projects scan` / `sessions start` 결과가 다음 polling 에서 자동 반영.
- `GET /api/health` — 서비스/DB 상태 (`{ok, version, db_path, db_exists,
  runtime_root}`).
- `DashboardServer(db_path_override=..., runtime_root_override=...)` —
  생성 시점에 `WORKER_CONTROL_DB` / `WORKER_CONTROL_HOME` 환경변수를
  덮어쓴다. CLI 의 `--db` / `--runtime-root` 가 이 경로로 들어온다.
- 기본 바인딩은 `127.0.0.1:8765`. `--allow-remote` 없이 다른 호스트로 띄우면
  CLI 가 종료 코드 2 로 거부한다.
- 어떤 엔드포인트도 DB 를 변경하지 않는다 (read-only 백엔드).
- CLI 진입점: `workerctl view serve` (alias `workerctl dashboard`)
  `[--host H] [--port P] [--open] [--allow-remote] [--native-limit N]
  [--db PATH] [--runtime-root PATH]`.

### Sessions (`worker_control.sessions`)
- 세션 라이프사이클(starting → running → ... → killed) 을 코어 모듈에서
  처리하고, runtime 모듈은 실제 프로세스/tmux 조작만 담당한다.
- **정책 가드**: `start_session` 진입 시 `project.root_role` 을 확인하여
  `public_reference` 또는 알려진 owned_work 외부면 `WriteProtectedRootError`
  를 발생시키고 CLI 는 종료 코드 3 으로 알린다.
- `capture`/`prompt`/`stop` 은 tmux 가 있으면 tmux 명령으로, 없으면 OS 별
  대안(있으면 사용, 없으면 명확히 거절) 으로 처리.

## Hermes 설정 저장소

`D:/work-github/hermes-settings/` 가 별도 git 저장소로 운영된다. 워커
오케스트레이션 정책, 환경 변수 가이드, 본 worker-control 과 함께 쓰이는
정책 문서를 모은다. 런타임 DB/로그/시크릿은 포함하지 않는다.

## Phase 2 — 단일 SessionRepository (PR #4–#7)

Phase 2 는 ``hermes_sessions`` 가 **한 곳의 writer** 와 **한 곳의 reader** 만
가지도록 정리한 작업이다. 사실상 모든 dashboard / heartbeat / build_report
화면은 같은 SQLite 뷰를 통과한다.

```
+-----------------------------+   upsert    +----------------------------+
|  Disk sources               | ----------> |  session_sync (writer)     |
|  · ~/.claude/projects/*.jsonl              |  worker_control/           |
|  · <hermes_home>/profiles/  |              |   session_sync.py          |
|    sessions/session_*.json  |              |  · from_jsonl              |
|  · 디스패처 argv             |              |  · from_profile_session…   |
+-----------------------------+              |  · from_dispatcher_argv    |
                                             |  · upsert_session          |
                                             |  · sync_all() (cron 진입)  |
                                             +-------------+--------------+
                                                           |
                                                           v
                                          +----------------------------------+
                                          |  SQLite (single canonical DB)    |
                                          |   hermes_sessions  (authoritative)|
                                          |   + hermes_runs / hermes_subprocs |
                                          |   + hermes_agent_sessions         |
                                          |   + 5 legacy-parity child tables  |
                                          +----------------+-----------------+
                                                           |
                                                           v
                                          +----------------------------------+
                                          |  session_view (reader, 단일)     |
                                          |  worker_control/session_view.py  |
                                          |  · list_sessions(...)             |
                                          |  · 조인만 — 디스크 접근 없음     |
                                          |  · 분류(spawn/native) 규칙 공유  |
                                          +----------------+-----------------+
                                                           |
                              +----------------------------+----------------------------+
                              v                            v                            v
              +-------------------------+    +-------------------------+    +-------------------------+
              | dashboard.collect_      |    | heartbeat.classify_     |    | build_report (Slack/    |
              | snapshot (HTTP BFF)     |    | sessions (30분 cron)    |    |  iShare 빌드)           |
              | http://127.0.0.1:8765/  |    |                         |    |                         |
              +-------------------------+    +-------------------------+    +-------------------------+
```

### 부속 테이블 (parity payload)

`session_view` 가 각 ledger row 와 함께 조인해 반환하는 자식 테이블:

| 테이블 | 역할 |
|--------|------|
| `session_pr_links`     | 세션이 만들거나 갱신한 PR 번호/URL/제목/병합 시각 |
| `session_files_touched`| 세션이 만진 파일 경로 + insertions/deletions/last_modified |
| `session_tools_recent` | 최근 사용한 tool name + 횟수 + 마지막 시각 (디버그 단서) |
| `session_recaps`       | 세션이 자체적으로 적은 recap 텍스트 (사용자 표시용) |
| `session_pending_queue`| 아직 처리하지 못한 follow-up 액션 큐 |

각 테이블 PK 는 `session_uuid` 로 `hermes_sessions.uuid` 와 1:N 으로 묶인다.
parity 컬럼 자체 (`kind`, `git_branch`, `msg_count`, `ai_title`, `summary`, …)
는 PR #8 에서 둘로 쪼개졌다:

* `hermes_agent_sessions` — hermes-only 세션 (`session_*.json` 출처).
* `claude_session_parity` — native `~/.claude/projects/<uuid>.jsonl` 출처.

`session_view` 가 spawned 행이면 `hermes_agent_sessions` 우선, native 행이면
`claude_session_parity` 우선으로 조인하고, 둘 다 비어있으면 옛 스키마(통합
테이블) 와 호환되도록 `hermes_agent_sessions` 를 claude UUID 로 한 번 더
조회한다. 5종 자식 테이블은 source-agnostic 이라 그대로 공유된다.

### 무엇이 사라졌나 — heartbeat in-memory synthesis

PR #5 이전의 `worker_control_hermes.heartbeat` 는 ledger 가 알지 못하는
jsonl 파일을 발견하면 **메모리 안에서만** `_synthetic=True` row 를 만들어
slack DM 에 끼워 넣었다 (line 339–364). dashboard / build_report 는 그 row 를
못 봤기 때문에 화면마다 세션 집합이 달랐다.

PR #4 의 `session_sync` 가 동일한 데이터를 **실제 row 로 persist** 하면서
PR #5 에서 그 in-memory 합성 블록은 **삭제**됐다. 이제 heartbeat 는
`session_view.list_sessions()` 결과를 그대로 분류만 한다.

### 트리거

| 시점 | 무엇이 도는가 |
|------|---------------|
| 매 heartbeat 틱 (30분) | `workerctl-hermes-heartbeat` 가 `_sync_ledger_before_classify()` 로 `sync_all` 을 먼저 호출 → 그 다음 `classify_sessions()`. 평균 latency ≈ 720 ms (PR #6 측정치). |
| (선택) `*/5 min` cron / Task Scheduler / systemd-timer | `workerctl session sync-all --quiet`. 30 분 freshness 가 부족한 경우만 추가. recipe 는 `docs/operations.md` "세션 레저 sync-all (Phase 2 PR #6)" 절. |
| 디스패처 emit-time | `workerctl-hermes-projects session start` 가 `session_sync.upsert_session(from_dispatcher_argv)` 를 호출해 즉시 row 를 깐다. |

레거시 `worker_sessions` 테이블은 Phase 2 에서 **deprecate** 되었다. 현 호스트
기준 0 rows 이고 dashboard / report 어디에도 노출되지 않는다. 자세한 절차는
`docs/operations.md` "Legacy `workerctl sessions list` (deprecated)" 참고.

## Future extensions (구현 안 함)

- GitHub Issues 폴링 → 자동 워커 스폰 (cron 스폰 포함)
- cron-like 스케줄러 / hermes 외 오케스트레이터 통합
- 멀티 호스트 / 원격 워커 등록
- 더 풍부한 상태 머신 / 토큰 사용량 추적
- TUI 대시보드
- `public_reference` 프로젝트에 대한 명시적 read-only 워커 모드(향후 정책 확장)
