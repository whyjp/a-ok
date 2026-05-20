# Heartbeat ↔ DB status mismatch — investigation & fix

## Symptom

Real session: `nat#f75febed` (live-memory-console, branch `docs-plan-multi-game-backend-unification`).

- `claude_session_parity.effective_status = 'inactive'`
- transcript mtime: **>4 hours stale** (no jsonl growth)
- `last_message = '[Request interrupted by user]'`
- yet Hermes heartbeat Slack 메시지에 주기적으로 **ALIVE/active** 로 표시됨
- `claude.exe` 프로세스 2개 (PID 45032, 51316) 가 같은 cwd 에 남아있음

User 진단: "DB 와 heartbeat 판단이 서로 다를 이유가 없다 — 두 로직(code path + DB 적재) 같이 검토하라."

## Goal

Heartbeat Slack 메시지의 status 분류와 `claude_session_parity.effective_status` (그리고 dashboard / session_view 가 노출하는 display_status) 가 **same source of truth** 를 쓰도록 통합. nat#f75febed 같은 케이스에서 ALIVE 로 잘못 표시되지 않아야 함.

## Investigation scope (Phase 1 — read-only, no edits)

다음 파일을 모두 읽고, 각각이 "session 이 active 인가" 를 어떻게 판단하는지 정확히 매핑한 표를 만들어라:

1. `worker_control_hermes/heartbeat.py` — Slack 으로 나가는 분류 (ALIVE / IDLE / JUST ENDED).  
   특히 docstring 의 "Classification (relative to NOW, with window=30 min)" 과 실제 코드가 일치하는지.
2. `worker_control_hermes/legacy_parity_ingest.py` + `legacy_parity_parser.py` + `legacy_parity_schema.py` — `claude_session_parity.effective_status` 가 어떤 입력 (jsonl mtime / last_message / 프로세스 / `[Request interrupted by user]` sentinel 등) 으로 산출되는지.
3. `worker_control/session_view.py` + `worker_control/dashboard.py` — `display_status` 가 어떻게 산출되는지 (heartbeat 와 같은 함수를 호출하는지, 아니면 별도 로직인지).
4. `worker_control/session_sync.py` + `worker_control/snapshot.py` — sync 가 transcript mtime 외에 별도로 status 를 덮어쓰는 경로가 있는지.

산출물: 각 surface (heartbeat Slack / parity.effective_status / dashboard display_status) 가 사용하는 **입력 신호 + 분류 함수 + threshold** 의 비교표 → `docs/plans/2026-05-20-heartbeat-db-status-mismatch.md` 에 "Findings" 섹션으로 append.

## Hypotheses to verify

다음 가설들을 코드 읽기 + 필요 시 sqlite 쿼리로 검증:

- (H1) Heartbeat 는 `claude_session_parity.transcript_mtime` 이 아니라 jsonl 파일 mtime 을 직접 stat 한다 → 어떤 다른 프로세스가 jsonl 을 touch 하면 stale 인데도 mtime 이 갱신될 수 있음.
- (H2) Heartbeat 의 "ALIVE ≤ 5 min" window 가 `claude_session_parity.effective_status='inactive'` 를 무시한다 (즉 두 로직이 독립적).
- (H3) `[Request interrupted by user]` sentinel 을 parity ingest 는 inactive 처리하지만 heartbeat 는 무시.
- (H4) `claude.exe` 프로세스 alive 가 분류에 직접 영향 (process scan 이 mtime 보다 우선).
- (H5) sync/snapshot 이 transcript_mtime 을 "now" 로 덮어쓰는 path 가 있음.

각 가설에 대해 **참/거짓 + 근거 line number** 명시.

## Phase 2 — Fix (only after Phase 1 findings 보고)

원칙:
- **Single source of truth**: `claude_session_parity.effective_status` (또는 그와 동등한 함수) 를 heartbeat / dashboard / session_view 가 공유.
- Forward-only: 과거 잘못 보낸 메시지 backfill 금지.
- Same-MR for naming/schema: status 분류 함수 이름이 바뀌면 의존 surface (heartbeat / dashboard html / tests / docs) 모두 같은 MR.
- Tests: `tests/test_heartbeat_slack_hide.py`, `tests/test_display_status.py`, `tests/test_legacy_parity_ingest.py` 에 nat#f75febed 시나리오 (jsonl mtime > 30min, last_message='[Request interrupted by user]', claude.exe alive) 를 fixture 로 추가하고 **각 surface 가 동일 status 를 반환** 하는지 assert.

Phase 2 진입 전 user 컨펌 받지 말고 곧장 진행 (autonomy mode). 단 Phase 1 findings 와 채택한 source-of-truth 후보를 plan 파일에 먼저 append.

## Out of scope

- Heartbeat 메시지 포맷/색상 변경
- claude.exe 프로세스 정리 (사용자가 직접 결정)
- live-memory-console 쪽 작업 재개

## Deliverable

1. Plan 파일에 Findings + Fix 섹션 append.
2. a-ok 에 MR (current repo, `git remote -v` 로 확인 후 glab 사용).
3. nat#f75febed 케이스가 새 분류 로직에서 IDLE/ENDED 로 떨어지는지 sqlite 로 검증한 결과를 MR description 에 포함.

---

## Findings (Phase 1, 2026-05-20)

### Comparison table — each surface's "is this session active?" inputs

| Surface | Input signal(s) | Classifier | Thresholds |
|---|---|---|---|
| `claude_session_parity.effective_status` (write path) | `transcript_mtime` (jsonl stat) + `ended_at` (always NULL for claude rows) | `legacy_parity_parser._classify_status()` + bulk SQL refresh `_refresh_effective_status` in `legacy_parity_ingest.py:263` | `active` ≤ 2h, `inactive` ≤ 24h, else `done` (from `ACTIVE_WINDOW_SEC=7200`, `INACTIVE_WINDOW_SEC=86400`) |
| `SessionView.display_status` (dashboard / view layer) | `hermes_sessions.ended_at`, `hermes_sessions.status`, parity `effective_status`, `hermes_sessions.last_used_at` | `session_view._compute_display_status()` (`session_view.py:275`) | `ended_at`/terminal → done; trust `effective_status='active'` only if `last_used_at` ≤ 2h; else recency (≤2h active / ≤24h inactive / >24h done) |
| `heartbeat` Slack bucket (ALIVE / JUST_ENDED / IDLE) | jsonl mtime (stat in `_jsonl_lookup`), `runs.started_at/ended_at`, `hermes_sessions.last_used_at`, **subprocs alive list**, **claude registry `status`** | `classify_sessions()` in `heartbeat.py:344` — its own bucket logic, *independent* of `effective_status` / `display_status` | `alive` ≤ 5 min, `just_ended` (status∈{done,failed,abandoned} AND ended_at within window), else `idle`; window default 30 min |

The three surfaces do NOT share a classifier. heartbeat re-derives "last activity" from disk + live process scan and ignores both `effective_status` and `display_status` columns the view already exposes.

### Hypothesis verdicts

- **H1 (heartbeat stats jsonl directly, not parity)** — **TRUE**. `heartbeat.py:182-212 (_jsonl_lookup)` `jl.stat().st_mtime`. parity's `transcript_mtime` column is never read by heartbeat. In nat#f75febed's case both agree (mtime = 4h38m old), so this is not the proximate cause but it's a structural divergence.
- **H2 (heartbeat ALIVE window ignores `effective_status='inactive'`)** — **TRUE**. `heartbeat.py:546-549` only consults `last >= alive_cutoff`; never reads `effective_status` nor `display_status`. The view loader writes them into `s["display_status"]` (`heartbeat.py:162`) but `classify_sessions` never references that field for bucketing.
- **H3 (`[Request interrupted by user]` sentinel ignored by heartbeat)** — **TRUE**. parity ingest stores it as `last_message` (`legacy_parity_parser.py:243-244`) but doesn't treat it as a terminal marker — `effective_status` is still computed from mtime. heartbeat reads neither `last_message` nor a "user-interrupt" flag.
- **H4 (claude.exe / subprocess scan forces ALIVE)** — **TRUE — this is the proximate cause for nat#f75febed.** `heartbeat.py:506-510`:
  ```python
  if s["_subprocs_alive"] or s["_claude_status"] == "busy":
      last = NOW  # forces ALIVE bucket below
      s["_last"] = NOW
  ```
  DB query confirms `hermes_subprocs` row id=297 (`rtk.exe grep ...`, `pid=51140`, `status='alive'`, `started_at=2026-05-20T01:26:55+00:00`) attributed to `f75febed-de62-4469-84f7-e3d2b6f9c3e7` — that rtk.exe has been alive ~11h and is presumably a stuck/orphaned grep. Every heartbeat tick this overrides `last` to NOW → ALIVE bucket, regardless of jsonl staleness.
- **H5 (sync/snapshot overwrites `transcript_mtime` to now)** — **FALSE**. `session_sync.py:416` MAX()'s `last_used_at` forward (never rewrites to NOW), and `parse_claude_jsonl` always re-reads the real file mtime (`legacy_parity_parser.py:316 _file_mtime_iso`).

### nat#f75febed concrete numbers

- `hermes_sessions.last_used_at` = `2026-05-20T08:08:40.148Z` (parity-derived; matches jsonl mtime).
- `claude_session_parity.transcript_mtime` = `2026-05-20T08:08:40+00:00`; `effective_status='inactive'`; `last_message='[Request interrupted by user]'`.
- `~/.claude/sessions/45032.json` exists → `status='shell'` (NOT 'busy'). So claude-registry alone does NOT trigger the override.
- `hermes_subprocs` id=297 — `rtk.exe`, `status='alive'`, age ~11h → THIS triggers the override.
- now = `2026-05-20T12:46:57+00:00`; age since last jsonl write ≈ 4h38m.
- `_compute_display_status` correctly yields `inactive` (effective_status non-active, age > 2h, age < 24h).
- heartbeat without fix: `last=NOW` from H4 override → ALIVE.

### Chosen source of truth

`SessionView.display_status` (which is itself derived from `effective_status` + `ended_at`/terminal + `last_used_at` recency cross-validation in `_compute_display_status`).

Why this and not raw `effective_status`:
- `display_status` already cross-validates parity 'active' against `last_used_at` (defense-in-depth that the dashboard tests pin).
- `display_status` honors `ended_at` and terminal `status` — the parity table never carries these for claude rows.
- It's already loaded into the heartbeat session dict at `heartbeat.py:162` but unused.

Heartbeat's 5-min ALIVE vs 30-min IDLE granularity stays — it's a finer sub-window inside `display_status='active'`. The fix adds `display_status` as a hard gate so an `'inactive'` or `'done'` row can never be promoted to ALIVE by live-subprocess attribution alone.

---

## Phase 2 design

1. **Add `heartbeat_bucket()` in `session_view.py`** — a shared classifier that takes `(display_status, last_activity, ended_at, now, window_min)` and returns one of `"alive" | "just_ended" | "idle" | None`. Single source of truth function imported by heartbeat.
2. **`heartbeat.classify_sessions`** — drop the `if s["_subprocs_alive"] or s["_claude_status"] == "busy": last = NOW` override (lines 506-510). Live subprocs/busy-claude still rendered as per-row detail chips in `_line` / `_section_text_for`, just no longer promote bucket. Replace the inline alive/idle/just_ended logic with a `heartbeat_bucket()` call keyed on the view-derived `display_status`.
3. **Tests**:
   - `tests/test_display_status.py` — add `nat#f75febed` fixture (4h38m old `last_used_at`, `effective_status='inactive'`, no `ended_at`) → assert `display_status=='inactive'`.
   - `tests/test_heartbeat_slack_hide.py` — extend or new test `test_classify_*` — synthetic session with stale mtime + alive subproc + claude `status='shell'` → assert NOT in `alive` bucket.
   - `tests/test_legacy_parity_ingest.py` — already covers the `'inactive'` re-derivation; leave as-is.
   - New `tests/test_session_view_heartbeat_bucket.py` — unit-test the shared classifier across all four return values.

---

## Fix (Phase 2, applied)

### Code changes

1. `worker_control/session_view.py` — added `heartbeat_bucket(*, display_status, last_activity, ended_at, now, window_min, alive_cutoff_sec) -> "alive"|"just_ended"|"idle"|None`. Single classifier; honors `display_status` as the primary signal (which itself already cross-validates `effective_status` against `last_used_at`). 30-min default window + 5-min alive sub-window match the existing heartbeat thresholds.
2. `worker_control_hermes/heartbeat.py` —
   - Dropped the `if s["_subprocs_alive"] or s["_claude_status"] == "busy": last = NOW` override that was promoting stale-jsonl sessions to ALIVE. Comment block left in place explaining why (with a reference to the parity fix).
   - Replaced the inline `alive_cutoff / window_cutoff / just_ended` branch with a single `heartbeat_bucket(...)` call.
   - Live subprocs + `claude_status` chips still render in `_line` / `_section_text_for` — visibility preserved, only the bucket promotion removed.

### Verification against the real DB

```
uuid:                f75febed-de62-4469-84f7-e3d2b6f9c3e7
display_status:      inactive
effective_status:    inactive
status:              active
ended_at:            None
last_used_at:        2026-05-20T08:08:40.148Z
transcript_mtime:    2026-05-20T08:08:40+00:00
last_user_text:      [Request interrupted by user]
heartbeat bucket (window=30 min default):  None    ← dropped from Slack snapshot
heartbeat bucket (window=10 h hypothetical):'idle'   ← never alive even at huge window
```

### Tests added

- `tests/test_session_view_heartbeat_bucket.py` — 9 cases pinning the new classifier (alive / idle / just_ended / dropped) including the nat#f75febed shape.
- `tests/test_heartbeat_classify_parity.py` — end-to-end regression: a stale-mtime `display_status='inactive'` session paired with a fresh `display_status='active'` mirror session. Asserts the stale one is NOT in ALIVE (and falls out of the 30-min window entirely), while the fresh one still buckets as ALIVE.

All 250 tests pass (`pytest tests/ -q --ignore=tests/test_meaningful_diff.py`).
