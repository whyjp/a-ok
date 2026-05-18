# Phase 2 Integration — Single SessionRepository

## Why

PRs #1–#3 unified the DB file and brought `worker_control_hermes` into the
package, but the **writers** that populate `hermes_sessions` still live in
three places with no shared trigger:

| Source (disk)                                              | Writer                                       | Target                                                                            | Trigger          |
|------------------------------------------------------------|----------------------------------------------|-----------------------------------------------------------------------------------|------------------|
| `~/.claude/projects/<enc>/<uuid>.jsonl`                    | `worker_control_hermes.projects.cmd_session_sync_native` | INSERT `hermes_sessions` (origin='native')                            | **none** (manual only) |
| same jsonl                                                 | `worker_control.hermes_session_sync` (PR #3) | UPSERT `hermes_agent_sessions`; back-fill columns on `hermes_sessions`            | **none**         |
| same jsonl mtime                                           | `worker_control_hermes.heartbeat` (line 339–364) | in-memory `_synthetic=True` row (NOT persisted)                                | every 30 min     |
| dispatcher argv                                            | `worker_control_hermes.projects.cmd_session_start` | INSERT `hermes_sessions` + `hermes_runs`                                       | emit-time        |
| `~/AppData/Local/hermes/profiles/*/sessions/*.json`        | `worker_control.hermes_session_sync`         | UPSERT `hermes_agent_sessions`                                                    | **none**         |
| legacy `worker_sessions` table                             | legacy `workerctl sessions` CLI              | `worker_sessions` (NOT joined into the unified ledger)                            | live             |

Evidence: `hermes_sessions WHERE origin='native'` last `last_used_at` is
3 days stale (2026-05-15) even though `~/.claude/projects/` is being
written minute-by-minute. `heartbeat.py` looks correct only because it
re-synthesizes the missing rows in memory every tick — that hides the
underlying ledger drift from the user but the dashboard / `build_report` /
any downstream consumer still sees a stale picture.

## Goal

One **writer** (`session_sync`), one **reader** (`session_view`), three
consumers (dashboard, heartbeat, build_report) all reading the same view.
Heartbeat's in-memory synthesis block is **deleted** — `session_sync` now
makes that persistent.

```
worker_control/session_sync.py        ← NEW. all upserters in one module
  ├─ from_jsonl(jsonl_path)           ← logic from projects.py::_scan_native_session
  ├─ from_profile_session_json(path)  ← logic from hermes_session_sync.py
  ├─ from_dispatcher_argv(...)        ← INSERT extracted from cmd_session_start
  └─ upsert_session(row)              ← THE ONLY writer for hermes_sessions

worker_control/session_view.py        ← NEW. single reader
  └─ list_sessions(*, classification, freshness, ...) → list[SessionView]
     · hermes_sessions is authoritative
     · jsonl mtime → last_used_at "overtake" happens in sync, not here
     · reader just joins; classification rule unchanged
```

| Consumer                                | Before                                            | After                                  |
|-----------------------------------------|---------------------------------------------------|----------------------------------------|
| `dashboard.collect_snapshot()`          | `hermes_ledger.list_hermes_sessions()` (DB only, stale) | `session_view.list_sessions()`    |
| `heartbeat.classify_sessions()`         | DB + in-memory synthesis (lines 339-364)          | `session_view.list_sessions()` + classify only |
| `build_report.py`                       | DB only                                           | `session_view.list_sessions()`         |

The in-memory synthesis disappears because `session_sync` makes the same
rows real and persistent.

## Triggers

```
*/5 min   cron    workerctl session sync-all      # OPTIONAL — only if 5-min freshness is needed
                   ├─ jsonl walk → upsert_session(from_jsonl)
                   ├─ profile_session_json walk → upsert + back-fill
                   └─ reclassify_origins (idempotent, already exists)

emit-time         workerctl session start         # unchanged externally, internally delegates to upsert
                   └─ upsert_session(from_dispatcher_argv) + insert hermes_runs

every heartbeat   workerctl-hermes-heartbeat
                   └─ calls sync-all FIRST (< 1s), then classifies
```

If heartbeat calls `sync-all` itself, a separate cron is not strictly
required — 30-min staleness is acceptable for the dashboard if the user is
OK with that. The dedicated `*/5` cron is the second-step polish.

## Legacy `worker_sessions` decision

PR #2's README claims "unify ... as a single product" but the
`worker_sessions` table and `workerctl sessions list` weren't touched.

**Decision: deprecate, don't migrate.**

- 0 rows on this host (tmux/console workers aren't being used).
- An adapter `from_worker_session_row()` is dead code today.
- Keep the table read-only, hide from the dashboard, mark in docs.

If a future host actually uses tmux workers, revisit and add the adapter.
Cheap to add later, expensive to maintain now.

## PR plan

Each PR is independent and ≤ ~400 lines diff. Workers MUST use git
worktrees to avoid colliding with other in-flight `a-ok:` sessions on
this repo.

### PR #4 — `worker_control/session_sync.py`  (1–2h)

NEW module. Three `from_*` adapters + one `upsert_session`. Re-point the
three existing writers (`cmd_session_sync_native`, `hermes_session_sync`,
`cmd_session_start` INSERT path) to delegate to it. Existing public
function names stay (compat wrappers). Add unit tests covering each
adapter and the upsert deduplication.

**Files touched:**
- NEW `worker_control/session_sync.py`
- EDIT `worker_control_hermes/projects.py` (delegate `_scan_native_session` + `cmd_session_sync_native` INSERT path)
- EDIT `worker_control/hermes_session_sync.py` (delegate `_upsert_hermes_session` body)
- NEW `tests/test_session_sync.py`

**Out of scope:** schema changes, dashboard.py, heartbeat.py, the reader
side. Those are PR #5.

### PR #5 — `worker_control/session_view.py` + consumer migration  (1–2h)

NEW reader module. Migrate dashboard, heartbeat, build_report. Delete
heartbeat's in-memory synthesis block (line 339–364). **Hard depends on
PR #4 being merged first** because the reader assumes sync has populated
the rows.

**Out of scope:** sync-all CLI, scheduling.

### PR #6 — `workerctl session sync-all` + heartbeat auto-sync  (≤1h)

Add the CLI subcommand. Have `workerctl-hermes-heartbeat` call
`session_sync.sync_all()` before classification. Optional cron job
registration documented in `docs/operations.md`.

### PR #7 — docs + deprecate `worker_sessions`  (≤30min)

- `docs/architecture.md`: replace the writers-table with the new single-writer flow.
- `docs/dashboard.md`: note that the synthesis path is gone.
- `docs/operations.md`: deprecation notice for `workerctl sessions list`,
  pointer to `workerctl session list --include-legacy` if anyone asks.
- README badge / FAQ entry.

## Coexistence with in-flight `a-ok:` worker sessions

When dispatching PR #4–#7 to claude-code workers via
`workerctl-hermes-projects session start`, the worker **must** isolate
itself with a git worktree so file-edit collisions with other in-flight
sessions on the same repo are physically impossible:

```bash
# preferred: claude's --worktree flag (handles cleanup, tmux pairing)
claude -p '<brief>' -w phase2-pr4 ...

# OR explicit git worktree (when --worktree flag misbehaves on Windows):
git -C D:/work-github/a-ok worktree add ../a-ok-phase2-pr4 -b feat/phase2-pr4-session-sync
cd ../a-ok-phase2-pr4 && claude -p '<brief>' ...
```

PR brief must call this out explicitly and tell the worker to:

1. Create the worktree under `<repo>/.claude/worktrees/<slug>` or `../a-ok-<slug>`.
2. Branch off `origin/main` (NOT off any unmerged feat branch from a sibling worker).
3. Push the branch and open PR with `gh pr create --base main`.
4. Note the parent SHA so the reviewer can rebase the conflicting sibling.

## Conflict map (known in-flight: `a-ok:worker-control-sites-1143-parity-retry2`)

That session is extending `hermes_sessions` columns + dashboard rendering
for legacy parity. Overlap risk per PR in this plan:

| PR  | Files                                                                | Overlap with parity-retry2 | Strategy |
|-----|----------------------------------------------------------------------|---------------------------|----------|
| #4  | NEW `session_sync.py`, EDIT `projects.py` + `hermes_session_sync.py` | medium (both touch upsert SQL) | **Worker rebases on parity-retry2 if it lands first.** PR #4 can land before parity-retry2 too — the upsert delegation is column-agnostic. |
| #5  | `dashboard.py`, `heartbeat.py`, NEW `session_view.py`                | **HIGH** — same files     | **Wait for parity-retry2 to merge** before starting PR #5. |
| #6  | `cli.py`, `heartbeat.py`                                             | low-medium                | After PR #5. |
| #7  | docs only                                                            | none                      | Any time. |

Order: parity-retry2 merges first → PR #4 → PR #5 → PR #6 → PR #7.
PR #4 can be started immediately in a worktree without waiting.
