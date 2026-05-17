# Dashboard (HTML view layer)

`workerctl view html` 은 worker-control 의 현재 상태를 **단일 정적 HTML** 로
직렬화한다. 외부 라이브러리/네트워크 요청 없이 동작하며 그대로 오프라인
브라우저에서 열린다.

## 생성 / 열기

```bash
workerctl view html                     # 기본 위치에 작성
workerctl view html --open              # 작성 후 기본 브라우저로 열기
workerctl view html -o /tmp/dash.html   # 임의 경로
workerctl view html --native-limit 0    # native 디스커버리 끄기
workerctl view html --native-limit 2000 # 더 많이 보기 (기본 500)
```

기본 출력 경로: `<runtime_root>/dashboard.html`
(= 기본값 `D:/work-github/.worker-control/dashboard.html`).
`.worker-control/` 디렉토리는 이미 `.gitignore` 로 차단되어 있으므로
런타임 산출물이 저장소에 섞일 위험이 없다.

CLI 종료 코드는 일반 CLI 와 동일하다 (정책 위반은 없으므로 dashboard 명령은
사실상 항상 0).

## 페이지 구성

```
+-----------------------------------------------------------------+
|  worker-control · 컨트롤 패널                                    |
|  버전 · DB · 런타임 · 생성 시각                                  |
+-----------------------------------------------------------------+
|  워크스페이스 카드 (owned_work / public_reference)               |
|   - 경로 · WRITE/READ-ONLY 정책 · exists 여부                    |
+-----------------------------------------------------------------+
|  요약 카운터 9장 — 프로파일/프로젝트/owned/public/git/dirty/      |
|  Hermes 세션/running/native 세션                                  |
+-----------------------------------------------------------------+
|  [워커] [Hermes] [Native] [프로젝트]   ← 탭                       |
+-----------------------------------------------------------------+
|  탭별 테이블 (검색·필터·상태 badge)                              |
+-----------------------------------------------------------------+
```

### 탭 1 — 워커 프로파일

`worker_profiles` 테이블의 행을 그대로 표시. 이름/루트로 검색.

### 탭 2 — Hermes 스폰 세션

`worker_sessions` 테이블 = **이 도구가 띄운** Claude 워커 세션. Hermes 등
외부 오케스트레이터가 `workerctl sessions start` 로 만든 모든 세션은 여기에
잡힌다. 상태(state) 드롭다운 + 텍스트 검색.

상태 색상:
- `running` / `working` → 강조 (accent)
- `starting` → info
- `waiting_input` / `blocked` → warn
- `failed` / `killed` → danger
- `completed` → muted

### 탭 3 — Native Claude 세션 (read-only 디스커버리)

`~/.claude/projects/<encoded-path>/<uuid>.jsonl` 파일들을 **읽기 전용** 으로
스캔. 디렉토리/파일에 손대지 않는다.

수집 항목:
- `session_id` (파일명에서 추출한 UUID)
- 디코딩된 프로젝트 경로 추정 + 원본 인코딩 디렉토리명
- `permissionMode` (JSONL 의 첫 ~50줄에서 발견되는 값)
- `leafUuid`
- 파일 크기 / 줄 수 / 마지막 수정 UTC 시각

#### 디렉토리명 디코딩 휴리스틱

Claude Code 의 인코딩은 공식 스펙이 없고 `:` / `/` / `\` 모두 `-` 로 치환되는
**lossy** 인코딩이라 단순 `-→/` 치환은 원본 경로의 하이픈
(예: `worker-control`) 을 망가뜨린다. 그래서 다음 순서로 시도한다:

1. `configured_roots()` 의 각 루트에 대해 **실제로 1단계 자식 디렉토리** 를
   열어 보고, `encode(root)/(child)` 가 `name` prefix 와 일치하면 원본 그대로
   복원 — 하이픈 보존됨.
2. 루트만 prefix 매칭되면 그 뒤를 `-→/` 로 변환 (deep paths 에서 부정확할
   수 있음 — "추정" 표기).
3. 알려진 루트 외부면 드라이브 prefix(`D--…`) 만 살리고 나머지를 `-→/` 치환.
4. 그것도 아니면 단순 `-→/` 치환.

#### 한계

- 파일 위치/포맷은 Claude Code 버전에 따라 변할 수 있다. 호환성을 위해 모든
  접근은 `try/except` 로 감싸고, 실패해도 대시보드는 정상 렌더된다.
- 본 도구는 이 JSONL 들을 **읽기만** 한다. 삭제/수정/병합/링크 생성 없음.
- 디렉토리 자체가 없으면 안내 메시지를 띄우고 빈 테이블을 보여준다.
- 환경변수 `WORKER_CONTROL_CLAUDE_PROJECTS_DIR` 로 위치를 명시 지정할 수 있다.

### 탭 4 — 관리 대상 프로젝트

`projects` 테이블 + 워크스페이스 정책 라벨. 다음 필터 제공:

- 워크스페이스 (owned_work / public_reference / other)
- git 상태 (전체 / git 만 / dirty 만 / clean git 만)
- 텍스트 (이름/브랜치/경로/원격)

각 행에는 정책 pill 이 함께 표시된다:

- **owned_work** → `WRITE/PR` (`workerctl sessions start` 가능)
- **public_reference** → `READ-ONLY` (`workerctl sessions start` 가 정책으로 거부됨)
- **other** → 표시만, 세션 시작 시 거부

## 데이터 흐름

```
            +------------------------+
            |  worker_control.db     |
            |  worker_profiles       |
            |  worker_sessions       |
            |  projects              |
            +------------------------+
                       |
                       v
            +------------------------+        +---------------------------+
            |  dashboard.collect_    |  +-----+  native_sessions          |
            |  snapshot()            |        |  .discover_native_        |
            |  (DashboardSnapshot)   |        |  sessions()               |
            +------------------------+        |  (~/.claude/projects/)    |
                       |                      +---------------------------+
                       v
            +------------------------+
            |  dashboard.render_html |
            |  (단일 HTML 문자열)    |
            +------------------------+
                       |
                       v
            +------------------------+
            |  dashboard.html        |
            |  (정적, 오프라인)      |
            +------------------------+
```

## 보안 / 안전

- HTML 페이로드는 인라인 JSON 으로 박힌다. `</script>` / `<!--` 가 데이터에
  들어가도 안전하게 escape 된다 (테스트로 회귀 방어).
- 모든 사용자 데이터(프로파일명, 프로젝트 경로 등) 는 클라이언트측
  `escapeHtml()` 을 거쳐 DOM 에 들어간다.
- 외부 네트워크 호출 없음. 폰트도 시스템 기본 + 모노스페이스 폴백만.
- native 디스커버리는 read-only — 파일 수정/생성/삭제하지 않는다.

## 환경 변수

`workerctl` 의 다른 변수와 동일하며 추가로:

| 변수 | 의미 | 기본값 |
|------|------|--------|
| `WORKER_CONTROL_CLAUDE_PROJECTS_DIR` | native 세션 JSONL 루트 | `~/.claude/projects` |
