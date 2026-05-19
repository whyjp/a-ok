# Intent

운영자가 worker-control 대시보드에서 같은 *worker 이름*(`a-ok:test-multi-spawn-a-readme-summary`)이 서로 다른 두 개의 hermes-session 그룹(`20260519_154513_9925891f`, `20260519_154429_cd14c910`) 안에 각각 한 번씩, 사실상 "복제된 카드처럼" 노출되는 현상을 발견했음.

조사 결과 ── 단일 표시 버그가 아니라 INSERT/SELECT 양쪽에 걸친 **2-축 결함**이다.

1. **INSERT 축 — orphan 누적**:
   `workerctl-hermes-projects session start` 가 호출되는 순간 `hermes_sessions` 에 row 가 들어가고
   `hermes_runs` 에 `status='started'` 행이 들어감. 그러나 그 직후 emit 된 command 가
   bash syntax error 등으로 실패해 trap 이 점화되지 않으면 row 들은 영구히 `active`/`started`
   로 남는다. 같은 prompt 를 재시도할 때마다 신규 UUID 가 추가 INSERT 되므로
   동일 이름의 stale-active row 가 N개 누적된다 (실측 4 row: id 204/205/209/210).

2. **SELECT 축 — 그룹화 정책 부재**:
   `session_view.list_sessions()` 는 hermes_sessions 를 1:1 로 노출만 한다.
   대시보드 FE 는 그것을 `hermes_session_id` (=  parent hsid) 별로 묶어 보여주는데,
   같은 worker name 의 stale-active row 가 서로 다른 hsid 에 stamp 돼 있으면
   각 hsid 그룹마다 "같은 이름의 워커" 카드가 출현 → "중복" 으로 인지된다.

운영자 입장의 진짜 의도: **이 dispatcher 가 같은 이름의 워커를 한 hsid 그룹 안에 두 번 띄우지 않으면서, 그룹 간에 stale 잔재가 흘러넘치지도 않게** 만들어야 한다는 것.

# PRD — worker-control dispatcher orphan-prevention & dashboard dedup

## P0 — INSERT 축 fix (`worker_control_hermes/projects.py`)

### 1. dry-allocation 도입
`cmd_session_start` 가 현재는 (a) DB row INSERT → (b) command 문자열 emit → (c) caller 가 그 command 를 bash 로 실행, 순서다. (b) 에서 syntax error 가 잠재되면 (a) 의 row 가 영구히 `active` 로 남는다.

→ `--allocate-only` (default OFF, opt-in) 또는 더 보수적으로 default 동작 자체를 변경: emitted command 의 **shell-syntax preflight** 를 INSERT 와 같은 트랜잭션 안에서 수행하고, 실패 시 `ROLLBACK`. preflight 는 `bash -n <(echo "$emitted")` 한 줄.

### 2. trap robustness — `--output-format json` 같은 사용자 후행 옵션 흡수
현재 `_wrap_self_close` 는 `( trap '...' EXIT; <raw_cmd> )` 으로 닫혀 있어서 caller 가 `)` 뒤에 옵션을 붙이는 자연스러운 패턴이 syntax error 가 된다. 두 가지 보완:

- (a) `cmd_session_start` 에 `--extra-flags` 옵션 추가: 받은 문자열을 `raw_cmd` 내부에 안전하게 합성한 뒤 wrap. emitted command 가 항상 자기-완결적이 되도록.
- (b) JSON 출력에 `command_raw_inner`(현재 `command_raw` 와 동일) 외에 `command_tail_safe` 키 추가: 끝에 임의 옵션을 append 해도 wrap 이 깨지지 않는 형태(예: `bash -c '...'` 으로 한 줄 압축).

(a) 가 본질적, (b) 는 호환성용. (a) 먼저.

### 3. duplicate-name guard
같은 `(project_id, name, status='active')` 가 이미 존재하면 신규 INSERT 전에 직전 row 의 `(uuid, age)` 를 출력하고 다음 분기:
- age ≤ 60s → **422 Conflict** 와 함께 기존 uuid 반환 (UI 가 그걸 재사용하도록)
- age > 60s → 기존 row 의 status 를 `abandoned` 로 자동 전이 후 새 INSERT 진행 (단, `hermes_runs` 의 stale `started` 도 같이 `abandoned` 로)

이 정책은 `worker_control/session_sync.upsert_session` 의 set-once invariant 와 충돌하지 않게 별도 헬퍼 `_sweep_stale_name_dupes()` 로 분리한다.

### 4. preflight orphan sweep
`workerctl-hermes-projects runs sweep` (이미 존재) 의 보완: `--orphan-stale --max-age 60s` 추가하여 60초 넘게 `started` 인 dispatcher-allocated row 를 `abandoned` 로 일괄 닫는 verb. cron 으로 5분마다 도는 게 안전.

## P0 — SELECT 축 fix (`worker_control/session_view.py`, `dashboard.py`)

### 5. `list_sessions()` 그룹화 정책 명문화
함수 시그니처에 `group_dupes: Literal["off","by_name","by_name_within_hsid"] = "by_name_within_hsid"` 추가.
- `off` ── 현행 동작 (1 row = 1 SessionView)
- `by_name_within_hsid` ── 같은 `(name, hermes_session_id)` 조합에서는 `last_used_at` 최신 1건만 노출, 나머지는 `superseded_by` 키로 sibling uuid 리스트만 들고 다님.
- `by_name` ── hsid 무시하고 name 단위로 최신 1건.

기본값 `by_name_within_hsid` ── 현재 사용자가 본 "두 hsid 에 한 번씩" 가시 중복 해소.

### 6. snapshot payload schema bump
`/api/snapshot` 에 `dedup_policy` 필드를 한 줄 추가 (값 = list_sessions 가 사용한 정책). FE 는 그대로 둬도 동작, 대신 빈 SessionView 의 `superseded_by` 가 1 개 이상이면 카드 우측에 작은 `+N` 뱃지를 띄울 수 있게 hook 만 열어둔다 (FE 작업은 후속 PR).

### 7. 마이그레이션 backfill — **gate OFF default**
사용자 정책상 "forward-only fix" 가 원칙이므로, 기존 stale row 의 일괄 `abandoned` 전환은 `WORKER_CONTROL_STALE_BACKFILL_ENABLED=1` env gate 뒤에서만 수행한다. 코드는 추가하되 기본 OFF.

## P1 — Tests

- `tests/test_session_start_dupe_guard.py` ── 같은 (project,name) 으로 두 번 호출 시 422 + 기존 uuid 재사용.
- `tests/test_wrap_self_close_extra_flags.py` ── `--extra-flags '--output-format json'` 흡수 검증, emitted command 의 `bash -n` preflight 통과.
- `tests/test_list_sessions_dedup.py` ── 4-row fixture 에서 `by_name_within_hsid` 가 정확히 2건(=hsid 개수) 반환.

## 성공 기준

운영자가 같은 prompt 로 의도적/사고로 4번 spawn 호출하더라도:
- DB 에는 살아있는 `active` row 가 항상 0 또는 1개 (`by_name`, 동일 project) 로 유지된다.
- 대시보드는 hsid 당 1개 카드만 노출하며 `+N` 뱃지로 sibling 의 존재를 알린다.
- 시스템 어디에도 silent failure 가 없다 ── preflight 실패는 명시적 error, dupe 감지는 명시적 422.

## Non-goals

- 기존 history row (id 204/205/209 등) 의 retroactive mass-migrate. 사용자 정책상 OFF.
- FE 시각 디자인 변경. hook 만 열어두고 후속 PR.
- workerctl-venv 패키지 재설치 자동화 (이번 세션에서 수동 처리됨).

## 작업 분배 (worker 스폰 단위)

- **W1 (a-ok-fix-orphan)**: P0 §1, §2, §3, §4 — `projects.py` 와 신규 헬퍼. PR 단위 하나로.
- **W2 (a-ok-fix-dedup)**: P0 §5, §6 — `session_view.py` + `dashboard.py`. PR 단위 하나로.
- **W3 (a-ok-tests)**: P1 — pytest 3종. W1/W2 머지 이후 follow-up PR.

W1/W2 는 서로 독립적이므로 **병렬 spawn 가능**. W3 는 직렬.
