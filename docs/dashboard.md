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

JSON 페이로드는 다음 키를 포함한다:

- `generated_at`, `version`, `db_path`, `runtime_root`
- `workspace_roots[]` — owned_work / public_reference 라벨과 정책
- `profiles[]` — `worker_profiles` 행
- `projects[]` — `projects` 행 + `policy` (work_capable / read_only)
- `hermes_sessions[]` — `worker_sessions` 행 + profile/project 조인
- `native_sessions[]` — `~/.claude/projects/` 디스커버리 결과
- `native_root`, `native_root_exists`, `native_note`
- `counters` — 9 종 요약치

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

### 탭

| 탭 | 출처 |
|----|------|
| 워커 프로파일 | `worker_profiles` 테이블 |
| Hermes 스폰 세션 | `worker_sessions` 테이블 (이 도구가 띄운 워커) |
| Native Claude 세션 | `~/.claude/projects/<encoded-path>/<uuid>.jsonl` (read-only) |
| 관리 대상 프로젝트 | `projects` 테이블 + 워크스페이스 정책 라벨 |

상태/역할 색상은 기존과 동일 (`running`/`working` → accent, `failed`/`killed`
→ danger 등).

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
