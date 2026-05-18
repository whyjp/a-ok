# worker-control

로컬 Claude Code 워커 세션을 등록·실행·관찰하기 위한 최소 기능 도구.
SQLite 만으로 동작하며 외부 의존성은 표준 라이브러리 + (선택) tmux 뿐이다.

> **워크스페이스 정책 (v0.2)**
>
> | 경로 | role | 용도 |
> |------|------|------|
> | `D:/work-github` | `owned_work` | 본인 소유 GitHub 워크스페이스. **편집/커밋/PR/푸시/워커 기동의 유일한 기본 루트.** |
> | `D:/github` | `public_reference` | 다른 사람/공개 GitHub 저장소 참조용. **기본 read-only.** |
>
> - `workerctl sessions start` 는 `public_reference` 경로의 프로젝트에 대해
>   워커 기동을 **거부**한다.
> - 외부 오케스트레이터(Hermes 등)는 이 Claude Code 세션을 "지시"만 한다.
>   실제 파일/스캐닝/세션 관리/검증은 모두 이 도구가 직접 수행한다.
> - Claude 워커는 **항상 `claude --permission-mode auto` 일반 모드**로 기동한다.
>   `claude -p` / `--print` (print 모드) 는 **절대 사용하지 않는다.**
>   - 실제로 `worker_control.runtime.claude_argv()` 가 정책을 강제한다
>     (tmux/콘솔 두 경로 모두에서 `--permission-mode auto` 가 자동 부착됨).
>   - 외부 오케스트레이터가 워커 명령어를 직접 만들 때에도 동일한 패턴을 따른다.

## 빠른 시작 (Git Bash / PowerShell 공통)

```bash
# 1) 클론 + 설치 (한 번만)
git clone https://github.com/whyjp/a-ok.git D:/work-github/a-ok
cd D:/work-github/a-ok
pip install -e .

# 2) 한 줄로 호스트 전체 셋업 (idempotent — 매번 `git pull` 후 재실행해도 OK)
workerctl bootstrap
```

`workerctl bootstrap` 이 자동으로 처리하는 것:
1. canonical SQLite DB 생성 (`D:/work-github/.worker-control/worker-control.sqlite3`)
2. hermes ledger 테이블 (`hermes_sessions/runs/subprocs`) + `hermes_projects_v` view 적용
3. 워크스페이스 스캔 (`D:/work-github` = owned_work, `D:/github` = public_reference)
4. 디스크에서 발견한 모든 Hermes 프로파일의 `scripts/` 에 wrapper 자동 설치
   (SOUL.md / cron 이 참조하는 경로 호환성 유지). default 프로파일은 skip.
5. Windows 로그인 시 dashboard-daemon 자동 기동 (Startup 폴더 `.cmd`)
6. 즉시 BFF 기동 → `http://127.0.0.1:8765/`

### 수동 셋업 (bootstrap 분해)

```bash
# DB 초기화만
workerctl init

# 워크스페이스 두 곳 스캔
workerctl projects scan
workerctl projects list

# 프로파일 생성 (선택 — hermes 측이 이미 있으면 굳이 안 만들어도 됨)
workerctl profiles create default
workerctl profiles list                # workerctl DB + hermes disk 둘 다 표시

# 세션 시작 — owned_work 프로젝트에서만 허용된다.
workerctl sessions start --profile default --project worker-control
workerctl sessions list

# 세션 캡처/프롬프트/종료
workerctl sessions capture <session-id-or-name>
workerctl sessions prompt  <session-id-or-name> "ls -la"
workerctl sessions stop    <session-id-or-name>

# 대시보드
workerctl view serve             # http://127.0.0.1:8765/
workerctl dashboard              # 동일 명령의 짧은 별칭
workerctl view serve --open      # 띄운 뒤 브라우저로 열기

# Hermes 프로파일에 wrapper 만 재설치
workerctl install-hermes-profile

# Hermes cron (30 분) — 살아있는 세션이 있으면 Telegram 으로 snapshot
workerctl dashboard-snapshot
```

### Hermes 측 명령 (console scripts)

`a-ok` 가 hermes worker profile 의 SQLite ledger 도 함께 관리한다.
패키지 설치 시점에 다음 entry point 들이 PATH 에 노출된다:

| 명령 | 역할 |
|------|------|
| `workerctl-hermes-projects`  | claude-code 세션 ledger CRUD (`session start`/`run start`/`session list`/`sync-native` …) |
| `workerctl-hermes-heartbeat` | 30분 주기 활동 스냅샷 → Slack DM |
| `workerctl-hermes-report`    | Slack markdown + iShare HTML 번들 빌드 |
| `workerctl-hermes-subprocs`  | claude-code 워크로드 자식 프로세스 디스커버리 |
| `workerctl-hermes-migrate`   | 옛 `projects.db` → canonical DB 이관 (one-shot, idempotent) |

`bootstrap` 이 hermes profile 의 `scripts/<name>.py` 위치에 wrapper 도
함께 깔아주므로, 기존 SOUL.md / hermes cron 의 절대 경로
(`python <profile>/scripts/projects.py …`) 도 그대로 동작한다.

## 대시보드 (BFF + 동적 FE)

대시보드는 **SQLite 를 읽는 backend-for-frontend (BFF) + 동적 FE** 구조다.
매번 정적 HTML 을 생성하지 않는다 — 정적 FE 한 장
(`worker_control/static/dashboard.html`) 을 `workerctl view serve` 가 그대로
서빙하면서, FE 가 `/api/snapshot` JSON 을 5 초마다 polling 해서 자동으로
다시 그린다.

- `workerctl view serve` — stdlib `http.server` 기반 로컬 BFF. 기본 바인딩은
  `127.0.0.1:8765` 로 loopback 전용이며, 비-loopback 주소로 띄우려면
  `--allow-remote` 가 필요하다. `--db <path>` / `--runtime-root <path>` 로
  임의의 SQLite DB / 런타임 루트를 가리킬 수 있다.
- `workerctl dashboard` — 같은 동작의 짧은 별칭.
- `workerctl view html --legacy` — [레거시] 단일 파일 오프라인 스냅샷.
  BFF 없이 첨부/공유가 필요할 때만 쓴다. `--legacy` 플래그를 명시하지
  않으면 거부된다.
- `workerctl dashboard-daemon` — Hermes 시작 시 자동 기동용 supervisor.
  중복 실행 가드(`/api/health`) + 코드/정적자산 변경 시 자식 BFF 자동 재시작.
  자세한 환경변수/`scripts/worker_control_dashboard_service.py` wrapper 는
  `docs/operations.md` 참고.
- `workerctl dashboard-snapshot` — 살아있는 세션이 있을 때만 legacy 정적
  HTML 을 만들어 stdout 에 Telegram 페이로드(`MEDIA:<path>` 포함)를 발사
  하는 Hermes cron 진입점. wrapper: `scripts/send_dashboard_snapshot.py`.

탭 구성:

- **워커 프로파일** — `worker_profiles` 테이블.
- **Hermes 스폰 세션** — `worker_sessions` 테이블 (이 도구가 띄운 워커).
- **Native Claude 세션** — `~/.claude/projects/` 의 JSONL 을 **읽기 전용** 으로
  디스커버리한 결과 (세션 ID, 추정된 프로젝트 경로, `permissionMode`,
  파일 크기/줄 수/수정 시각).
- **관리 대상 프로젝트** — `projects` 테이블. 워크스페이스 역할(badge)·정책
  (`WRITE/PR` vs `READ-ONLY`)·git 상태·브랜치·경로를 한눈에.

검색·상태/역할 필터·요약 카운터·생성 시각이 함께 표시된다. 기본 출력 위치는
런타임 루트 아래 `dashboard.html` 이며 `.worker-control/` 자체가 이미
`.gitignore` 로 차단되어 있다.

native 디스커버리는 호스트마다 위치/포맷이 다를 수 있으므로 휴리스틱이다.
- 기본 경로: `~/.claude/projects/`
- 덮어쓰기: 환경변수 `WORKER_CONTROL_CLAUDE_PROJECTS_DIR`
- 디렉토리명 디코딩은 `configured_roots()` 의 실제 자식 디렉토리와 매칭해
  원본 하이픈(예: `worker-control`) 을 살리고, 매칭 실패 시 단순 `-→/` 치환
  으로 폴백한다.
- 끄려면 `--native-limit 0`.

### BFF 엔드포인트 (`workerctl view serve`)

| 엔드포인트 | 응답 |
|------------|------|
| `GET /` (또는 `/dashboard.html`) | 정적 FE HTML (= `worker_control/static/dashboard.html`) |
| `GET /api/snapshot` | `DashboardSnapshot` 의 JSON 직렬화 (요청마다 SQLite 재조회) |
| `GET /api/health` | `{ "ok": true, "service": "worker-control", "version": "…", "db_path": "…", "db_exists": true, "runtime_root": "…" }` |

- 기본적으로 `D:/work-github/.worker-control/worker-control.sqlite3` 를 읽는다.
  `--db <path>` / 환경변수 `WORKER_CONTROL_DB` 로 임의의 SQLite DB 를 가리킬 수
  있다 (`--runtime-root` / `WORKER_CONTROL_HOME` 도 동일하게 동작).
- 매 요청마다 `collect_snapshot()` 을 새로 호출하므로 `projects scan` /
  `sessions start` 결과가 다음 polling 에서 자동으로 보인다.
- 브라우저에서는 상단 메타에 `모드 라이브 · 5s` 가 표시된다. fetch 실패 시
  `라이브 · http <code>` / `라이브 · 오류` 로 표시되며 다음 주기에 재시도한다.

### 레거시 단일 파일 export (`workerctl view html --legacy`)

오프라인 첨부/공유용으로 남겨둔 폴백 경로. BFF 없이 단독으로 동작하는 HTML
한 장을 만들며, 일반 운영에서는 쓰지 않는다 (DB 가 갱신되어도 자동 반영되지
않는다). 자세한 옵션은 `docs/dashboard.md` 참고.

## 데이터 경로

| 항목 | 경로 |
|------|------|
| 정식 코드 저장소 | `D:/work-github/worker-control` |
| 이전(참조) 사본 | `D:/github/worker-control` *(read-only, 이동 알림용)* |
| 런타임 DB | `D:/work-github/.worker-control/worker-control.sqlite3` |
| 세션 로그/캡처 | `D:/work-github/.worker-control/sessions/<session-id>/` |
| Hermes 설정 정책 저장소 | `D:/work-github/hermes-settings` |

런타임 산출물은 저장소 밖(`D:/work-github/.worker-control/`)에 둔다.
`.gitignore` 가 `*.sqlite3`, `logs/`, `data/`, `.worker-control/` 도 차단한다.

## 환경 변수

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `WORKER_CONTROL_HOME` | 런타임 루트(SQLite/로그) | `D:/work-github/.worker-control` |
| `WORKER_CONTROL_DB` | DB 파일 경로 | `${WORKER_CONTROL_HOME}/worker-control.sqlite3` |
| `WORKER_CONTROL_PROJECT_ROOT` | owned_work 루트 | `D:/work-github` |
| `WORKER_CONTROL_PUBLIC_REFERENCE_ROOT` | public_reference 루트 | `D:/github` |
| `WORKER_CONTROL_CLAUDE_BIN` | claude 실행 파일 | `claude` |
| `WORKER_CONTROL_SPAWN_SLUG_PREFIXES` | 워커 dispatch 식별용 run-name prefix (콤마 구분). 빈 값이면 `claude -p` 만으로 spawn 판정. 예: `scv-,worker-` | (empty) |

## tmux 가용성

- **tmux 가 있을 때**: 세션마다 별도 tmux 세션을 열고 `claude` 를 띄운다.
  `capture`/`prompt` 가 안정적으로 동작한다.
- **tmux 가 없을 때 (Windows 기본)**: 새 콘솔 창을 열어 `claude` 를 직접 띄운다.
  - 시작/종료는 동작한다.
  - `capture`/`prompt` 는 제한된다 — DB 에 남는 마지막 이벤트 로그를 기준으로 동작.
  - 즉, 콘솔 창에 사람이 직접 타이핑하는 운영을 가정한다.
  - 자세한 한계는 `docs/operations.md` 참고.

> **중요**: tmux 가 없을 때도 절대 `claude -p` 로 폴백하지 않는다 (정책).
> 두 모드 모두 `claude --permission-mode auto` 로 기동된다.

## 상태(state) 어휘

세션 상태는 거친 단위만 추적한다:

```
starting → running → waiting_input → working → blocked
                                            ↘ completed / failed / killed
```

## 디렉토리 구조

```
a-ok/
├── README.md
├── pyproject.toml
├── .gitignore
├── docs/
│   ├── architecture.md
│   ├── dashboard.md
│   └── operations.md
├── worker_control/                    # core (BFF, CLI, dashboard, schema)
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── dashboard.py
│   ├── db.py
│   ├── hermes_install.py              # bootstrap + install-hermes-profile
│   ├── hermes_ledger.py               # read-side of hermes_sessions/runs
│   ├── hermes_profiles.py             # ~/AppData/Local/hermes 디스커버리
│   ├── native_sessions.py
│   ├── paths.py
│   ├── profiles.py
│   ├── projects.py
│   ├── runtime.py
│   ├── scanner.py
│   ├── sessions.py
│   ├── server.py
│   └── static/dashboard.html
├── worker_control_hermes/             # hermes-profile-side companions
│   ├── __init__.py                    # (entry points 등록 대상)
│   ├── projects.py                    # claude-code 세션 ledger CLI
│   ├── heartbeat.py                   # Slack DM 30분 스냅샷
│   ├── build_report.py                # Slack + iShare 리포트 빌더
│   ├── subprocs.py                    # claude 워크로드 자식 프로세스 트래커
│   ├── migrate_to_canonical_db.py     # 옛 projects.db → canonical 이관
│   └── hermes_projects_view.sql       # 옛 컬럼 layout 호환 view
└── tests/
    ├── test_dashboard.py
    ├── test_db.py
    ├── test_native_sessions.py
    ├── test_paths.py
    ├── test_runtime_argv.py
    ├── test_scanner.py
    ├── test_server.py
    └── test_sessions_policy.py
```

## 미래 확장 (현재 미구현, 문서로만 명시)

- GitHub Issues 폴링 → 워커 자동 스폰
- cron/스케줄러 기반 정기 워커 기동
- 캡처 스트림에서 자동 state 전이
- 멀티 호스트 워커 등록

## 라이선스

MIT
