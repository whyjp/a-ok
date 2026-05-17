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

## 미래 확장 (계획만)

- GitHub Issues / Linear 폴링 → 새 워커 자동 스폰
- cron / systemd-timer 기반 스케줄러
- 캡처 화면을 파싱해서 자동 state 전이
- 다중 호스트 워커 등록 (gRPC/SSH 기반)
- `public_reference` 루트에 대한 명시적 read-only 워커 모드
