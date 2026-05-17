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

## Future extensions (구현 안 함)

- GitHub Issues 폴링 → 자동 워커 스폰 (cron 스폰 포함)
- cron-like 스케줄러 / hermes 외 오케스트레이터 통합
- 멀티 호스트 / 원격 워커 등록
- 더 풍부한 상태 머신 / 토큰 사용량 추적
- TUI 대시보드
- `public_reference` 프로젝트에 대한 명시적 read-only 워커 모드(향후 정책 확장)
