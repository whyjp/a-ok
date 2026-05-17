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
# 1) 설치 (개발 모드)
cd D:/work-github/worker-control
pip install -e .

# 2) DB 초기화 (D:/work-github/.worker-control/worker-control.sqlite3 생성)
workerctl init

# 3) 워크스페이스 두 곳을 한 번에 스캔
#    (D:/work-github → owned_work, D:/github → public_reference)
workerctl projects scan
workerctl projects list

# 4) 프로파일 생성 (기본 root = D:/work-github)
workerctl profiles create default
workerctl profiles list

# 5) 세션 시작 — owned_work 프로젝트에서만 허용된다.
workerctl sessions start --profile default --project worker-control
workerctl sessions list

# 6) 세션 캡처/프롬프트/종료
workerctl sessions capture <session-id-or-name>
workerctl sessions prompt  <session-id-or-name> "ls -la"
workerctl sessions stop    <session-id-or-name>

# 7) 상태를 한 페이지로 보고 싶다면 — HTML 대시보드 생성
workerctl view html              # D:/work-github/.worker-control/dashboard.html 작성
workerctl view html --open       # 작성 후 기본 브라우저로 열기
workerctl view html -o out.html  # 임의 위치
```

## HTML 대시보드

`workerctl view html` 은 현재 DB 상태를 단일 정적 HTML 로 직렬화한다.
오프라인에서 그대로 열리며 외부 의존성이 없다.

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
worker-control/
├── README.md
├── pyproject.toml
├── .gitignore
├── docs/
│   ├── architecture.md
│   ├── dashboard.md
│   └── operations.md
├── worker_control/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── dashboard.py
│   ├── db.py
│   ├── native_sessions.py
│   ├── paths.py
│   ├── profiles.py
│   ├── projects.py
│   ├── runtime.py
│   ├── scanner.py
│   └── sessions.py
└── tests/
    ├── test_dashboard.py
    ├── test_db.py
    ├── test_native_sessions.py
    ├── test_paths.py
    ├── test_runtime_argv.py
    ├── test_scanner.py
    └── test_sessions_policy.py
```

## 미래 확장 (현재 미구현, 문서로만 명시)

- GitHub Issues 폴링 → 워커 자동 스폰
- cron/스케줄러 기반 정기 워커 기동
- 캡처 스트림에서 자동 state 전이
- 멀티 호스트 워커 등록

## 라이선스

MIT
