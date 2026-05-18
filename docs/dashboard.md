# Dashboard (SQLite-backed BFF + 동적 FE)

worker-control 의 대시보드는 **SQLite 를 읽는 backend-for-frontend (BFF) +
동적 FE** 구조다. FE 는 패키지에 묶여 있는 정적 자산
(`worker_control/static/dashboard.html`) 한 장이고, BFF (`worker_control.server`)
가 그 FE 를 서빙하면서 `/api/snapshot` JSON 으로 데이터를 공급한다.

```
+--------------------------+                +---------------------------+
|  worker_control/static/  |   GET /        |  worker_control.server    |
|  dashboard.html (정적 FE) | <------------- |  (ThreadingHTTPServer)    |
+--------------------------+                +---------------------------+
                                                       |
                       GET /api/snapshot               v
                       (JSON)                  +-------------------+
                       <----------------------+  worker-control DB |
                                              |  worker-control.   |
                                              |  sqlite3           |
                                              +-------------------+
```

매번 정적 HTML 을 새로 만들지 않는다. 같은 FE 자산을 계속 서빙하고, DB 가
바뀌면 다음 polling 에서 자동 반영된다.

## 빠른 시작

```bash
# 1) DB 초기화 (한 번)
workerctl init

# 2) BFF 띄우기 — 둘 다 같은 명령
workerctl view serve
workerctl dashboard            # 짧은 별칭

# 옵션
workerctl view serve --open                              # 브라우저까지 자동
workerctl view serve --port 9000
workerctl view serve --db D:/work-github/.worker-control/worker-control.sqlite3
workerctl view serve --runtime-root D:/work-github/.worker-control
workerctl view serve --host 0.0.0.0 --allow-remote      # 비-loopback 노출
```

기본 바인딩은 `http://127.0.0.1:8765/` (loopback 전용). 비-loopback 주소를
쓰려면 `--allow-remote` 가 함께 필요하다. 대시보드는 DB 경로/프로젝트 경로
같은 환경 정보를 노출하므로 신뢰된 네트워크에서만 띄워야 한다.

## SQLite DB 경로

BFF 가 어떤 DB 를 읽는지 결정하는 우선순위:

1. `workerctl view serve --db <path>` (인자) — 최우선.
2. 환경변수 `WORKER_CONTROL_DB`.
3. `WORKER_CONTROL_HOME/worker-control.sqlite3` (런타임 루트 아래).
4. 기본값 `D:/work-github/.worker-control/worker-control.sqlite3`.

`--db` 와 `--runtime-root` 는 BFF 가 떠 있는 동안의 프로세스 환경변수를
세팅해서 동작한다 — 같은 프로세스 안의 모든 모듈이 즉시 그 값을 본다.

## 엔드포인트

| 메서드 / 경로 | 응답 |
|---------------|------|
| `GET /`               | 정적 FE HTML (= `dashboard.html` 자산) |
| `GET /dashboard.html` | `/` 의 alias |
| `GET /api/snapshot`   | `DashboardSnapshot` 의 JSON 직렬화 (요청마다 SQLite 재조회) |
| `GET /api/health`     | `{ ok, service, version, db_path, db_exists, runtime_root }` |

JSON 페이로드는 다음 키를 포함한다 (Phase 2 이후):

- `generated_at`, `version`, `db_path`, `runtime_root`
- `workspace_roots[]` — owned_work / public_reference 라벨과 정책
- `profiles[]` — `worker_profiles` 행
- `projects[]` — `projects` 행 + `policy` (work_capable / read_only)
- `hermes_sessions[]` — **단일 ledger `hermes_sessions` 행** 을
  `session_view.list_sessions()` 로 읽어 `a-ok:` prefix 가 붙은
  *spawn* 분류 결과만 추려낸 것. 각 행에 legacy-parity 자식 테이블
  (`pr_links`, `files_touched`, `tools_recent`, `recap_native`,
  `pending_queue`) + `git_branch` / `msg_count` / `ai_title` 가
  pre-join 된 채로 들어 있다.
- `native_sessions[]` — 같은 ledger 의 나머지 (native) 분류. 화면
  카드 구조는 동일.
- `hermes_agent_sessions[]` — Hermes 자체 agent-session 패널 (다른
  cardinality, `hermes_agent_sessions` 테이블에서 별도로 로드).
- `native_root`, `native_root_exists`, `native_note` — `~/.claude/projects/`
  디스커버리 메타 (행은 ledger 가 공급, 이 키는 root 경로만 노출).
- `counters` — 13 종 요약치 (`hermes_spawned` / `hermes_native` /
  `sessions_with_pr` / `sessions_with_pending` / `sessions_with_recap`
  / `sessions_with_files` 등 parity meta 칩의 분모).

## FE 페이지 구성

```
+-----------------------------------------------------------------+
|  worker-control · 컨트롤 패널                                    |
|  버전 · DB · 런타임 · 생성 시각 · 모드(라이브/오프라인)          |
+-----------------------------------------------------------------+
|  워크스페이스 카드 (owned_work / public_reference)               |
+-----------------------------------------------------------------+
|  요약 카운터 9장                                                 |
+-----------------------------------------------------------------+
|  [워커] [Hermes] [Native] [프로젝트]   ← 탭                       |
+-----------------------------------------------------------------+
|  탭별 테이블 (검색·필터·상태 badge)                              |
+-----------------------------------------------------------------+
```

### 탭 (Phase 2 이후)

| 탭 | 출처 |
|----|------|
| 워커 프로파일 | `worker_profiles` 테이블 |
| Hermes 스폰 세션 | `hermes_sessions` (단일 ledger) WHERE 분류 = `a_ok_spawned` |
| Native Claude 세션 | `hermes_sessions` (단일 ledger) WHERE 분류 ≠ `a_ok_spawned` |
| 관리 대상 프로젝트 | `projects` 테이블 + 워크스페이스 정책 라벨 |

두 세션 탭은 **같은 SQLite 행** 을 `a-ok:` prefix 로 분할한 결과다. 디스크
재스캔이 아니라 `session_view.list_sessions()` 한 번의 호출이 모든 카드를
공급한다.

상태/역할 색상은 기존과 동일 (`running`/`working` → accent, `failed`/`killed`
→ danger 등).

#### 카드 구성 (Phase 2 parity meta 포함)

각 세션 카드의 데이터 출처 (FE 가 `hermes_sessions[i]` 의 키를 그대로 본다):

| 화면 요소 | JSON 키 | 공급 위치 |
|-----------|---------|-----------|
| 좌상단 origin tag (`spawn` / `native`) | `classification` | `session_view._classify_origin` (a-ok: prefix 매칭) |
| 헤더 제목 | `ai_title` → fallback `first_message`  | `hermes_agent_sessions` (spawn) / `claude_session_parity` (native) |
| git branch chip | `git_branch` | 같은 parity 테이블 (출처별 분기) |
| recap card 본문 | `recap_native` | `session_recaps` 테이블 |
| PR chips | `pr_links[]` | `session_pr_links` 테이블 |
| 파일 chips + insertions/deletions | `files_touched[]` | `session_files_touched` |
| 최근 tool chips | `tools_recent[]` | `session_tools_recent` |
| pending follow-up | `pending_queue[]` | `session_pending_queue` |
| turn / cost | `turn_count`, `total_cost_usd` | `hermes_sessions` (writer 가 sync 시 백필) |
| 마지막 활동 | `last_used_at` | `hermes_sessions.last_used_at` (writer 가 MAX 로 전진) |

위 자식 테이블이 비어있어도 카드 자체는 그려진다 — FE 는 빈 배열을 hide
처리한다.

> 참고: 레거시 `worker_sessions` 테이블은 Phase 2 에서 더 이상 FE 어디에도
> 노출하지 않는다 (현 호스트 0 rows). 자세한 절차는
> `docs/operations.md` "Legacy `workerctl sessions list` (deprecated)" 참고.

### Native 디스커버리

- 기본 경로: `~/.claude/projects/`
- 덮어쓰기: 환경변수 `WORKER_CONTROL_CLAUDE_PROJECTS_DIR`
- 디렉토리명 디코딩(`D--work-github-worker-control` →
  `D:/work-github/worker-control`)은 `configured_roots()` 의 실제 자식
  디렉토리와 매칭해 원본 하이픈을 살린다. 매칭 실패 시 단순 `-→/` 폴백.
- 끄려면 `--native-limit 0`.

## FE 의 부팅 흐름

`worker_control/static/dashboard.html` 의 JS 는 세 가지 시나리오를 자동
구분한다:

1. **`http(s)://` 으로 열림 (BFF 모드)**
   - 페이지 부팅 직후 `fetch('/api/snapshot')` 으로 데이터 1회 로드.
   - 이후 5 초 주기로 polling 해서 자동 재렌더.
   - 상단 메타에 `모드 라이브 · 5s` 표시.
2. **`file://` 으로 열림 + 인라인 데이터 있음 (legacy export 모드)**
   - `<script id="dashboard-data">` 의 placeholder `"__INLINE_DATA__"` 가
     실제 JSON 으로 치환되어 있으면 그 값으로 한 번만 렌더.
   - polling 없음. 상단 메타에 `모드 오프라인 스냅샷` 표시.
3. **`file://` 으로 열림 + 인라인 데이터 없음**
   - "BFF 가 필요합니다" 배너만 보여주고 멈춘다. `workerctl view serve` 를
     쓰라고 안내한다.

## 보안 / 안전

- 모든 사용자 데이터(프로파일명, 프로젝트 경로 등)는 클라이언트측
  `escapeHtml()` 을 거쳐 DOM 에 들어간다.
- legacy export 의 인라인 JSON 은 `</script>` / `<!--` 를 escape 한다
  (XSS 방지, 회귀 테스트 있음).
- 외부 네트워크/폰트 호출 없음. 시스템 폰트만.
- BFF 는 read-only — 어떤 엔드포인트도 DB 를 변경하지 않는다.
- native 디스커버리는 read-only — 파일을 수정/생성/삭제하지 않는다.
- 기본 바인딩은 `127.0.0.1`. 비-loopback 은 명시적 `--allow-remote` 필요.

## 상시 실행 / 자동 재시작 (`dashboard-daemon`)

Hermes 가 떠 있는 동안 대시보드가 항상 응답하도록 supervisor 를 띄울 수
있다. supervisor 는

1. 시작 시 `/api/health` 를 찔러 이미 떠 있으면 새 자식을 띄우지 않고,
2. 없으면 자식 BFF 를 detached subprocess 로 spawn 하고,
3. `worker_control/*.py` 와 `static/*.html` 의 mtime 을 1 초 주기로
   감시해서 코드가 바뀌면 자식을 안전하게 재시작하며,
4. 자식이 죽으면 자동으로 다시 띄운다.

```bash
workerctl dashboard-daemon --log D:/work-github/.worker-control/dashboard.log
workerctl dashboard-daemon --once   # ensure-running 한 번 만 하고 종료
workerctl dashboard-daemon --no-watch  # supervisor 없이 그대로 detach
```

stdlib 만 사용하므로 Windows/Git-Bash 양쪽에서 동일하게 동작한다. 자세한
Hermes 통합 예시는 `docs/operations.md` 참고.

## Phase 2 — heartbeat in-memory synthesis 제거

PR #5 이전의 heartbeat (`worker_control_hermes/heartbeat.py` line 339–364)
는 ledger 가 모르는 jsonl 파일을 발견하면 메모리 안에서 `_synthetic=True`
row 를 만들어 Slack DM 에 끼워 넣었다. dashboard / build_report 는 그
synthesized row 를 못 봤기 때문에 화면마다 보는 세션이 달랐다.

PR #4 의 `worker_control.session_sync` 가 jsonl / profile JSON / 디스패처
argv 세 소스를 모두 **실제 row 로** persist 시키면서 그 메모리 합성 블록은
**완전히 삭제**됐다. 이제 모든 consumer 가 동일한 `session_view` 를 본다 —
dashboard 도 예외 없다.

PR #6 의 `workerctl session sync-all` 은 heartbeat 가 매 30 분 틱마다 직접
호출한다 (`_sync_ledger_before_classify()`). 화면 freshness 가 30 분으로
부족하면 `*/5 min` cron / Task Scheduler / systemd-timer 로 같은 명령을
추가 등록한다 (recipe 는 `docs/operations.md`).

## Telegram snapshot (`dashboard-snapshot`)

레거시 단일 파일 HTML 은 더 이상 일상 운영에서 쓰지 않는다. Hermes cron
에서 30 분 주기로 Telegram 으로 보내는 snapshot 용으로만 남겼다.

```bash
workerctl dashboard-snapshot
# 살아있는 세션이 있을 때만 stdout 에:
#   📊 worker-control snapshot
#   · generated: ...
#   ...
#   MEDIA:D:/work-github/.worker-control/telegram-snapshot.html
```

살아있다 = `worker_sessions.state ∈ {starting, running, working,
waiting_input, blocked}` 또는 최근 24 시간 내 mtime 의 native 세션이
존재할 때. 살아있지 않으면 stdout 을 비워서 cron 이 조용히 지나간다.

> **참고**: `worker_sessions` 테이블은 Phase 2 에서 deprecate 되었고
> 현 호스트 기준 0 rows 라 사실상 `native` 24h mtime 만으로 alive 판정이
> 굴러간다. 향후 tmux/console 워커가 실제로 쓰일 때 부활시킨다.

## 레거시: 단일 파일 HTML export

오프라인 첨부/공유 등 BFF 없이 단독 HTML 이 필요한 경우에만 사용한다.
일반 운영에서는 쓰지 말 것.

```bash
workerctl view html --legacy                       # default <runtime_root>/dashboard.html
workerctl view html --legacy --open
workerctl view html --legacy -o /tmp/dash.html --native-limit 1000
workerctl view html --legacy --native-limit 0      # native 디스커버리 끄기
```

- `--legacy` 플래그를 명시하지 않으면 거부되며 BFF 사용을 안내한다.
- 출력 HTML 은 `worker_control/static/dashboard.html` 자산을 그대로 쓰되,
  placeholder `"__INLINE_DATA__"` 위치에 현재 스냅샷 JSON 을 박는다.
- 한 번 생성된 파일은 그 시점의 스냅샷이다 — DB 가 바뀌면 다시 생성해야 한다.

## 환경 변수

`workerctl` 의 다른 변수와 동일하며 BFF 관련해서:

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `WORKER_CONTROL_DB` | BFF/CLI 가 읽는 SQLite 경로 | `${WORKER_CONTROL_HOME}/worker-control.sqlite3` |
| `WORKER_CONTROL_HOME` | 런타임 루트 (= 기본 DB 부모) | `D:/work-github/.worker-control` |
| `WORKER_CONTROL_CLAUDE_PROJECTS_DIR` | native 세션 JSONL 루트 | `~/.claude/projects` |

`workerctl view serve --db` / `--runtime-root` 는 위 환경변수를 그대로
세팅하는 단축 옵션이다.
