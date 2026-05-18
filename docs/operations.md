# Operations

## 설치

```bash
cd D:/work-github/worker-control
pip install -e .
```

`workerctl` 명령이 PATH 에 잡혀야 한다. 잡히지 않으면:

```bash
python -m worker_control --help
```

## 초기화

```bash
workerctl init
```

기본 DB 경로는 `D:/work-github/.worker-control/worker-control.sqlite3` 이며
부모 디렉토리는 자동 생성된다. 환경 변수로 덮어쓸 수 있다:

```bash
# POSIX/Git Bash
export WORKER_CONTROL_HOME=/c/Users/cxx/.worker-control
# PowerShell
$env:WORKER_CONTROL_HOME = "C:/Users/cxx/.worker-control"
```

`/d/work-github` (MSYS 스타일) 와 `D:/work-github` 는 자동으로 같은
경로로 정규화된다.

## 워크스페이스 정책 (필수 이해)

| 경로 | role | 동작 |
|------|------|------|
| `D:/work-github` | `owned_work` | 스캔/세션 시작/캡처/프롬프트 모두 허용 |
| `D:/github` | `public_reference` | 스캔/조회만 허용. 세션 시작 거부(`WriteProtectedRootError`) |
| 그 외 | `other` | 세션 시작 거부. 환경변수로 새 owned_work 루트를 지정해야 함 |

CLI 종료 코드:
- `2` — 일반 사용자 오류(프로파일/프로젝트 없음 등)
- `3` — 정책 위반 (`WriteProtectedRootError`)

## 프로파일

```bash
workerctl profiles create default                  # root = D:/work-github (기본)
workerctl profiles create scratch --root D:/work-github/_scratch
workerctl profiles list
```

프로파일은 워커가 어떤 권한/옵션으로 기동될지를 묶는 라벨이다. 이 초기
버전에서는 `root` 만 사용한다 (Claude Code 워킹 디렉토리의 부모).

## 프로젝트 스캔

```bash
workerctl projects scan                            # configured_roots() 모두
workerctl projects scan --root D:/work-github      # 특정 루트만
workerctl projects scan --root D:/github           # public_reference 만 (read-only 모드 유지)
workerctl projects list
```

각 루트의 1단계 자식 디렉토리를 보고, `.git` 이 있는 것만 git 저장소로 취급한다.
branch/remote/dirty 와 함께 **root_role** 도 `projects` 테이블에 upsert 된다.
스캔 메타는 `project_scans` 에 적재된다.

`workerctl projects list` 출력은 다음과 같다:

```
ID   git/dirty  [role]              name                 branch      path
  1  git  *    [owned_work       ]  worker-control        main        D:/work-github/worker-control
  2  git       [public_reference ]  other-public-repo     main        D:/github/other-public-repo
```

## 세션 시작/관찰/종료

### 시작

```bash
workerctl sessions start --profile default --project worker-control
```

내부 동작:

1. profile, project 존재 확인.
2. **정책 가드** — `project.root_role` 이 `public_reference` 면 즉시 거부
   (`WriteProtectedRootError`, exit code 3). `other` 도 거부.
3. DB 에 `worker_sessions` 행을 `starting` 상태로 만든다.
4. tmux 가 있으면 별도 tmux 세션을 열고 `claude --permission-mode auto` 를 띄운다.
5. tmux 가 없으면 새 콘솔 창(Windows: `start "" cmd /k claude --permission-mode auto`)을 띄운다.
6. 결과 PID/tmux 세션명을 DB 에 기록하고 `running` 으로 전이.

> **워커 기동 인자 정책**
>
> - 항상 `claude --permission-mode auto` 일반 모드로 띄운다.
> - `claude -p` / `--print` (print 모드) 는 두 경로 모두에서 금지된다.
> - 실제 argv 는 `worker_control.runtime.claude_argv()` 가 만든다.
> - 외부에서 `WORKER_CONTROL_CLAUDE_BIN` 로 실행 파일 경로만 바꿀 수 있고,
>   기본 인자는 코드에서 강제된다.

거부 메시지 예:

```
error: 이 프로젝트는 public_reference 워크스페이스(D:/github)에 있어 워커
기동이 금지되었습니다. 편집/PR 대상이라면 D:/work-github 으로 이동시킨 뒤
다시 `workerctl projects scan` 을 실행하세요. (project=foo, path=D:/github/foo)
```

### 관찰

```bash
workerctl sessions capture <session-id-or-name>
```

- **tmux 모드**: `tmux capture-pane -p -t <session>` 로 현재 화면을 스냅샷.
- **콘솔 모드**: tmux 가 없으면 화면 직접 캡처는 안 된다. 대신 지금까지의
  state/event 로그를 출력한다. (제한사항 — README/architecture 참고)

### 프롬프트 주입

```bash
workerctl sessions prompt <session-id-or-name> "ls -la"
```

- **tmux 모드**: `tmux send-keys -t <session> "<text>" Enter`.
- **콘솔 모드**: 새 콘솔 창에는 안전하게 키 주입을 할 수 없으므로 명령이
  거부된다. DB 에는 `worker_commands` 에 'rejected_no_tmux' 로 기록만 남긴다.

### 종료

```bash
workerctl sessions stop <session-id-or-name>
```

- **tmux 모드**: `tmux kill-session -t <session>`.
- **콘솔 모드**: 저장된 PID 로 `taskkill /PID <pid> /T /F` (Windows) 또는
  `kill -TERM <pid>` (POSIX).

## 대시보드 (SQLite BFF + 동적 FE)

전체 상태를 한 페이지로 보고 싶다면 — **기본 운영은 SQLite 기반 BFF + 동적 FE**
(`workerctl view serve`) 다. 정적 단일 파일 export 는 BFF 없이 첨부/공유가
필요할 때만 쓰는 **레거시** 경로이며 `--legacy` 플래그를 명시해야 한다.

**SQLite DB 경로 우선순위**: `--db` > `WORKER_CONTROL_DB` >
`${WORKER_CONTROL_HOME}/worker-control.sqlite3` >
`D:/work-github/.worker-control/worker-control.sqlite3`.

```bash
# 기본: SQLite 기반 BFF + 동적 FE (요청마다 DB 재조회, 5 초 polling)
workerctl view serve              # http://127.0.0.1:8765/
workerctl dashboard               # 동일 명령의 짧은 별칭
workerctl view serve --open       # 띄운 뒤 브라우저로 열기
workerctl view serve --port 9000  # 다른 포트
workerctl view serve --db D:/work-github/.worker-control/worker-control.sqlite3
workerctl view serve --runtime-root D:/work-github/.worker-control

# [LEGACY] 단일 파일 스냅샷 — 오프라인 첨부/공유용
workerctl view html --legacy                  # <runtime_root>/dashboard.html 작성
workerctl view html --legacy --open
workerctl view html --legacy -o /tmp/x.html --native-limit 1000
```

탭은 워커 프로파일 / Hermes 스폰 세션 / Native Claude 세션 / 관리 대상
프로젝트 네 개로 구성된다. 자세한 내용은 `docs/dashboard.md` 참고.

`workerctl view serve` 는 stdlib `http.server` 만 사용하며 (외부 의존성 0),
기본적으로 loopback(`127.0.0.1`) 에만 바인딩한다. 비-loopback 주소로 열려면
`--allow-remote` 가 필요하다. 응답하는 엔드포인트는 `/`, `/api/snapshot`,
`/api/health` 세 개이고 모두 read-only 다.

## Hermes 시작 시 자동 기동 (상시 대시보드)

Hermes 가 떠 있는 동안 `http://127.0.0.1:8765/` 에 동적 대시보드가 항상
응답하도록 supervisor 를 띄운다.

```bash
# 기본 supervisor — 코드 변경 시 자식 BFF 자동 재시작
workerctl dashboard-daemon --log D:/work-github/.worker-control/dashboard.log

# 또는 한 번만 띄우고 빠지기 (Hermes 가 자체적으로 detach 한다면 충분)
workerctl dashboard-daemon --once --log D:/work-github/.worker-control/dashboard.log

# 얇은 wrapper (Hermes config 에 절대경로로 박기 좋음)
python D:/work-github/worker-control/scripts/worker_control_dashboard_service.py
```

핵심:

- **중복 실행 금지** — 시작 시 `/api/health` 를 먼저 찔러 이미 떠 있으면
  새 자식을 띄우지 않는다.
- **포트 8765 기본**, DB `D:/work-github/.worker-control/worker-control.sqlite3`.
- **자동 재시작** — `worker_control/*.py` 와 `worker_control/static/*.html`
  의 mtime 을 1 초 주기로 polling 해서 변경 감지 시 자식 BFF 를 graceful
  terminate → respawn 한다 (기본 `--watch` 켜짐, `--no-watch` 로 끔).
- **자식 사망 시 자동 재기동** — 자식 BFF 가 죽으면 supervisor 가 다시
  띄운다.

환경변수로도 같은 동작:

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `WORKER_CONTROL_DASHBOARD_HOST` | bind host | `127.0.0.1` |
| `WORKER_CONTROL_DASHBOARD_PORT` | bind port | `8765` |
| `WORKER_CONTROL_DASHBOARD_LOG`  | 자식 BFF stdout/stderr 로그 | (DEVNULL) |
| `WORKER_CONTROL_DASHBOARD_ONCE` | `1` 이면 ensure-running 만 하고 종료 | `0` |

데이터는 SQLite 에 실시간으로 적재되며, FE 는 `/api/snapshot` 을 5 초마다
polling 하므로 페이지 새로고침 없이도 갱신된다 (자세한 흐름은
`docs/dashboard.md`).

## Telegram snapshot cron (30 분 주기)

레거시 정적 dashboard 는 **Telegram 으로 보내는 snapshot 전용** 으로만 쓴다.

```bash
# 살아있는 세션이 있을 때만 stdout 에 메시지 + MEDIA:<path> 출력
workerctl dashboard-snapshot

# Hermes cron 용 wrapper (절대경로)
python D:/work-github/worker-control/scripts/send_dashboard_snapshot.py
```

동작:

- **살아있는 세션 정의** — `worker_sessions.state` ∈
  `{starting, running, working, waiting_input, blocked}` 중 하나라도 있거나,
  최근 24 시간 내 mtime 을 가진 native Claude 세션이 있으면 alive.
- alive 면 `<runtime_root>/telegram-snapshot.html` 에 legacy 단일 파일
  HTML 을 생성하고 stdout 에:
  ```
  📊 worker-control snapshot
  · generated: ...
  ...
  MEDIA:D:/work-github/.worker-control/telegram-snapshot.html
  ```
- alive 가 아니면 stdout 은 비어있다 (Hermes cron 이 조용히 넘어가도록).

Hermes `no_agent` cron 예시 (config 위치/포맷은 Hermes 측 문서를 따른다):

```ini
# 30 min 주기
[cron.worker_control_snapshot]
schedule = "*/30 * * * *"
mode = "no_agent"
command = "python D:/work-github/worker-control/scripts/send_dashboard_snapshot.py"
# stdout 의 MEDIA:<path> 와 텍스트 본문이 Telegram 으로 그대로 전달된다
```

## 상태 어휘

```
starting    → 워커 프로세스 기동 시도 중
running     → 정상 가동, 입력 대기 아님
waiting_input → Claude 가 사용자 입력 대기 (가시성 한계)
working     → 작업 중
blocked     → 사용자 결정/승인 대기
completed   → 정상 종료
failed      → 비정상 종료
killed      → 사용자에 의한 명시적 종료
```

`waiting_input` / `working` / `blocked` 는 현재 버전에서 *자동 감지하지 않는다*.
사용자가 명시적 전이 명령으로 옮기거나, 향후 캡처 화면 파싱으로 확장한다.

## 한계 (현재 버전)

- Windows + tmux 없음 상태에서는 `capture`/`prompt` 가 제한적이다.
- 동일 호스트 단일 사용자 가정. 멀티 호스트는 다루지 않는다.
- GitHub Issues 폴링, cron 스폰 등은 향후 확장으로만 언급된다.
- 상태 자동 전이는 없다 — 외부에서 명시적으로 옮긴다.
- `public_reference` 정책은 *루트 기반* 이다. 심볼릭 링크/네트워크 드라이브로
  우회되지 않도록 정규화에 신경 쓰지만, 의도적 우회는 차단하지 않는다.

## Hermes 디스패처 run lifecycle (자동 종료)

`workerctl-hermes-projects session start` / `run start` 가 PM 에이전트에게
넘기는 `command` 는 더 이상 raw `claude ...` 한 줄이 아니라 bash subshell
래퍼다. 안쪽 claude 가 어떤 경로로 끝나든 (정상 종료, 비-0 exit, ctrl-c,
SIGTERM, SIGHUP) `EXIT` trap 이 `workerctl-hermes-projects run end <id>
--status done|failed --note "exit=<rc>"` 를 호출해 row 가 `started` 로
박혀버리는 누수를 차단한다. 원래 exit code 는 보존된다.

PM 에이전트는 JSON 출력의 `command` 필드를 **변형 없이 그대로 실행**한다.
별도로 `run end` 를 부를 필요가 없다. 레거시 동작 (raw 명령) 이 필요하면
`--no-auto-close` 플래그를 추가하면 된다.

JSON 출력 추가 필드:

- `command` — 래핑된 형태. PM 이 verbatim 실행.
- `command_raw` — 래핑 전 원본 (디버깅/로그용).
- `auto_close` — `true|false`.

bash 전용. cmd.exe / PowerShell 사용자는 `--no-auto-close` 로 raw 형태를
받고 직접 `run end` 를 호출한다.

### Sweeper (안전망)

SIGKILL / 정전 등으로 trap 자체가 실행되지 못한 row 를 청소한다:

```bash
workerctl-hermes-projects runs sweep [--max-age-hours 24] [--dry-run] [--json]
```

조건:
- `status = 'started'`
- `name LIKE 'a-ok:%'` (디스패처 소유 run 만 — 다른 prefix 는 절대 안 건드림)
- `started_at` 가 `--max-age-hours` 보다 오래됨

청소된 row 는 `status='failed'` + sweep 사유 note 가 박힌다. 실패 run 은
세션을 자동 종료하지 않는다 (사람이 확인하라는 의도).

heartbeat (`workerctl-hermes-heartbeat`) 가 sweep 후보를 자체 snapshot 의
🧹 SWEEP CANDIDATES 섹션에 노출한다. 24h 임계 누수가 있으면 같은 화면에서
즉시 보인다.

상세 패턴 / PM-side SOUL.md 권고 사항: `hermes-pm-dispatcher-profile` 스킬의
`references/run-lifecycle-hooks.md`.

## 세션 레저 sync-all (Phase 2 PR #6)

`hermes_sessions` 는 단일 writer (`worker_control.session_sync`) 가
관리한다. 디스크의 두 소스 — `~/.claude/projects/<encoded>/<uuid>.jsonl`
과 `<hermes_home>/profiles/*/sessions/session_*.json` — 를 한 번에
훑어서 upsert 하는 진입점이 `workerctl session sync-all` 이다.

```bash
# 기본 — 디스크 전체 walk, mtime 변하지 않은 파일은 파싱 자체를 건너뜀
workerctl session sync-all

# --since: 그 시각 이후 mtime 인 파일만
workerctl session sync-all --since 2026-05-18T00:00:00Z

# --dry-run: 카운터만 계산, DB write 없음
workerctl session sync-all --dry-run

# --quiet: 50개마다 찍히는 진행상황과 끝 요약 한 줄 모두 끔
workerctl session sync-all --quiet
```

종료 직전 한 줄 요약:

```
sync-all: synced(jsonl)=31 synced(profile)=0 skipped(mtime_unchanged)=119
          skipped(no_project)=0 skipped(no_uuid)=94 errors=0
          reclassify=(spawned=17,native=158) (726 ms)
```

* `skipped_mtime_unchanged` — 캐시된 `last_used_at` 이상으로 mtime 이
  움직이지 않은 파일. 파싱조차 하지 않으므로 hot path.
* `synced_jsonl` / `synced_profile` — `upsert_session` 으로 실제 row 가
  들어가거나 갱신된 수.
* `reclassify=(spawned=N,native=M)` — `_reclassify_origins` 호출 결과
  (`hermes_runs.mode='print'` 이 존재하는 세션만 spawned).

기본 운영에서는 **heartbeat 가 매 틱마다 `sync_all` 을 호출**하므로
별도 cron 은 필수가 아니다 (30 분 freshness 가 허용 가능하다는 전제).
5 분 freshness 가 필요하면 아래 둘 중 하나로 등록한다.

### Windows Task Scheduler

```powershell
# 5분 주기. powershell 창은 -WindowStyle Hidden 으로 숨김
schtasks /Create /SC MINUTE /MO 5 /TN "workerctl-session-sync-all" `
  /TR "powershell -WindowStyle Hidden -Command `"workerctl session sync-all --quiet`""
```

### POSIX cron

```cron
*/5 * * * *   /usr/local/bin/workerctl session sync-all --quiet >> /var/log/workerctl-sync.log 2>&1
```

### systemd-timer

```ini
# /etc/systemd/system/workerctl-sync-all.service
[Unit]
Description=workerctl session sync-all

[Service]
Type=oneshot
ExecStart=/usr/local/bin/workerctl session sync-all --quiet
```

```ini
# /etc/systemd/system/workerctl-sync-all.timer
[Unit]
Description=Run workerctl session sync-all every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=workerctl-sync-all.service

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now workerctl-sync-all.timer
```

## 미래 확장 (계획만)

- GitHub Issues / Linear 폴링 → 새 워커 자동 스폰
- cron / systemd-timer 기반 스케줄러
- 캡처 화면을 파싱해서 자동 state 전이
- 다중 호스트 워커 등록 (gRPC/SSH 기반)
- `public_reference` 루트에 대한 명시적 read-only 워커 모드
