"""hermes_ledger — read-side adapter over the canonical SQLite DB.

The hermes worker-profile stores its claude-code session ledger in three
tables that coexist with worker_control's own ones in the same DB:

  * hermes_sessions   — one row per claude session UUID
  * hermes_runs       — one row per `claude` invocation against that UUID
  * hermes_subprocs   — workload sub-processes psutil-discovered under a
                        live claude TUI (not surfaced on the dashboard yet)

Classification — what decides the dashboard tab:

a-ok (worker-control) is now the canonical dispatcher for sessions spawned
under this Hermes worker profile, so the AUTHORITATIVE spawn signal is the
``a-ok:`` prefix on ``hermes_runs.name`` (or on the session-side base name).
The dispatcher (``scripts/projects.py::_run_name()``) stamps every run it
emits with this prefix, and the prefix is unforgeable by a claude
``/rename`` (the prefix lives on the run name in our DB, not on the
user-facing claude-side label).

``hermes_runs.mode='print'`` (i.e. ``claude -p``) is NOT a spawn signal,
because a human can invoke ``claude -p`` manually too. We still surface
the print/interactive counts as an independent column for visibility,
but the spawn/native split is driven by the prefix alone.

A user with additional dispatchers (other workers, other tooling) can add
their own prefixes via the ``WORKER_CONTROL_EXTRA_SPAWN_PREFIXES`` env
var (comma-separated). The ``a-ok:`` prefix is always honoured regardless.

History: pre-a-ok dispatch data has run names like ``<base>__r<idx>-<ts>``
without the ``a-ok:`` prefix. Those land in the Native tab by design — no
migration needed; the classifier's prefix check is forward-only.

This module is **read-only** — it never writes back.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from worker_control.db import connect


# Reserved namespace prefix for sessions spawned by the a-ok (worker-control)
# dispatcher running inside a Hermes worker profile. Stamped by
# ``scripts/projects.py::_run_name()``. If you change this, also change the
# corresponding constant in that file in lockstep.
A_OK_SPAWN_PREFIX = "a-ok:"


def _load_extra_prefixes() -> tuple[str, ...]:
    """Additional run-name prefixes that should count as a spawn signal.

    Read fresh on every call so users can flip the env var without
    restarting the BFF — the next ``/api/snapshot`` request picks it up.
    Format: comma-separated, whitespace trimmed, empty entries dropped.

    The ``A_OK_SPAWN_PREFIX`` is ALWAYS honoured regardless of env state;
    this env only adds extra prefixes on top.
    """
    raw = os.environ.get("WORKER_CONTROL_EXTRA_SPAWN_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _all_spawn_prefixes() -> tuple[str, ...]:
    """The full set of prefixes that mark a session as a-ok-spawned."""
    return (A_OK_SPAWN_PREFIX,) + _load_extra_prefixes()


@dataclass(slots=True)
class HermesSessionView:
    """One row in the dashboard's hermes-session table."""
    id: int
    uuid: str
    name: str
    status: str
    origin: str                 # raw 'spawned'/'native' from runs (legacy column)
    classification: str         # see _classify()
    spawn_reason: str | None    # 'prefix:<matched>' or None
    dispatch_mode: str          # 'print' | 'interactive' | '—'  (info only)
    run_count: int
    print_run_count: int
    last_run_index: int | None
    last_run_name: str | None
    last_run_mode: str | None
    last_run_status: str | None
    last_run_started_at: str | None
    last_run_ended_at: str | None
    model: str | None
    permission_mode: str | None
    brief: str | None
    claude_name: str | None
    claude_status: str | None
    claude_status_at: str | None
    project_id: int | None
    project_name: str | None
    project_path: str | None
    project_role: str | None
    created_at: str
    last_used_at: str
    ended_at: str | None


def _matches_prefix(name: str | None, prefixes: tuple[str, ...]) -> str | None:
    """Return the matched prefix if ``name`` starts with any of them, else None."""
    if not name:
        return None
    for p in prefixes:
        if name.startswith(p):
            return p
    return None


def _classify(*, name: str, last_run_name: str | None,
              has_any_run: bool,
              spawn_prefixes: tuple[str, ...]) -> tuple[str, str | None]:
    """Decide which dashboard bucket this session belongs to.

    Returns (label, reason). Order of precedence:

      1. ``a_ok_spawned`` — name or last_run_name starts with a configured
         spawn prefix (``a-ok:`` is always in the set; extras come from
         ``WORKER_CONTROL_EXTRA_SPAWN_PREFIXES``). Reason = ``"prefix:<p>"``.

      2. ``interactive_multi`` — at least one run recorded, no prefix match.
         Could be a human ``--resume`` chain or a manual ``claude -p``.

      3. ``native`` — no runs (sync-native imported UUID, never invoked).
    """
    matched = _matches_prefix(name, spawn_prefixes) \
        or _matches_prefix(last_run_name, spawn_prefixes)
    if matched is not None:
        return "a_ok_spawned", f"prefix:{matched}"
    if has_any_run:
        return "interactive_multi", None
    return "native", None


SPAWN_CLASSIFICATIONS = frozenset({"a_ok_spawned"})


_LIST_SQL = """
WITH last_run AS (
    SELECT r.session_id,
           r.run_index, r.name, r.mode, r.status,
           r.started_at, r.ended_at
    FROM hermes_runs r
    JOIN (
        SELECT session_id, MAX(run_index) AS mx
        FROM hermes_runs
        GROUP BY session_id
    ) m ON m.session_id = r.session_id AND m.mx = r.run_index
),
run_stats AS (
    SELECT session_id,
           COUNT(*)                                            AS run_count,
           SUM(CASE WHEN mode = 'print' THEN 1 ELSE 0 END)     AS print_run_count
    FROM hermes_runs
    GROUP BY session_id
)
SELECT
    s.id, s.uuid, s.name, s.status, s.origin,
    s.model, s.permission_mode, s.brief,
    s.claude_name, s.claude_status, s.claude_status_at,
    s.created_at, s.last_used_at, s.ended_at,
    s.project_id,
    p.name        AS project_name,
    p.path        AS project_path,
    p.root_role   AS project_role,
    COALESCE(rs.run_count, 0)        AS run_count,
    COALESCE(rs.print_run_count, 0)  AS print_run_count,
    lr.run_index   AS last_run_index,
    lr.name        AS last_run_name,
    lr.mode        AS last_run_mode,
    lr.status      AS last_run_status,
    lr.started_at  AS last_run_started_at,
    lr.ended_at    AS last_run_ended_at
FROM hermes_sessions s
LEFT JOIN projects   p  ON p.id  = s.project_id
LEFT JOIN run_stats  rs ON rs.session_id = s.id
LEFT JOIN last_run   lr ON lr.session_id = s.id
ORDER BY s.last_used_at DESC, s.id DESC
"""


def _has_hermes_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name IN ('hermes_sessions','hermes_runs') "
        "LIMIT 1"
    ).fetchone()
    return row is not None


def auto_reclassify_origins(conn: sqlite3.Connection) -> int:
    """Recompute ``hermes_sessions.origin`` from the prefix rule.

    Single UPDATE statement, idempotent. Sets ``origin='spawned'`` iff the
    session row's own name OR ANY associated ``hermes_runs.name`` starts with
    a configured spawn prefix; ``'native'`` otherwise.

    This deliberately diverges from ``worker_control_hermes/projects.py::
    _reclassify_origins`` which uses the legacy ``mode='print'`` signal —
    the dashboard classifier is prefix-driven (see ``_classify`` above), so
    we keep the ``origin`` SQL column aligned with the prefix rule. Called
    on every snapshot read so newly-allocated sessions appear in the spawn
    tab without a manual ``workerctl session reclassify``.

    Returns the number of sessions now classified as spawned.
    """
    prefixes = _all_spawn_prefixes()
    if not prefixes:
        return 0
    # Build OR-chains for both the session name and the runs name. We use
    # ``GLOB`` with a trailing ``*`` so SQLite avoids the ``LIKE`` planner
    # cost of escaping ``%`` / ``_`` in user prefixes (a-ok: contains neither
    # today, but be conservative for forward-compat).
    sess_pred = " OR ".join(["name GLOB ?"] * len(prefixes))
    run_pred  = " OR ".join(["r.name GLOB ?"] * len(prefixes))
    args = tuple(f"{p}*" for p in prefixes) * 2
    sql = (
        "UPDATE hermes_sessions SET origin = CASE "
        f" WHEN ({sess_pred}) "
        f" OR EXISTS (SELECT 1 FROM hermes_runs r "
        f"            WHERE r.session_id = hermes_sessions.id AND ({run_pred})) "
        " THEN 'spawned' ELSE 'native' END"
    )
    try:
        conn.execute(sql, args)
    except sqlite3.OperationalError:
        # hermes_runs missing — nothing to reclassify against.
        return 0
    sp = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE origin='spawned'"
    ).fetchone()[0]
    return sp


def list_hermes_sessions(limit: int | None = None) -> list[HermesSessionView]:
    """Return one HermesSessionView per row in `hermes_sessions`, joined with
    the most-recent run (mode + status + name) and run-count aggregates.

    Safe on a DB that does NOT have the hermes ledger tables yet — returns []
    instead of raising, so the worker_control side never depends on the
    hermes worker profile being installed.
    """
    spawn_prefixes = _all_spawn_prefixes()
    with connect() as conn:
        if not _has_hermes_tables(conn):
            return []
        # Cheap idempotent UPDATE — keeps `origin` in sync with the prefix
        # rule so the spawn tab counter reflects sessions allocated since
        # the last refresh, without the user running `session reclassify`.
        auto_reclassify_origins(conn)
        rows = conn.execute(_LIST_SQL).fetchall()

    out: list[HermesSessionView] = []
    for r in rows:
        cls, reason = _classify(
            name=r["name"],
            last_run_name=r["last_run_name"],
            has_any_run=(r["run_count"] or 0) > 0,
            spawn_prefixes=spawn_prefixes,
        )
        dispatch_mode = r["last_run_mode"] or "—"
        out.append(HermesSessionView(
            id=r["id"],
            uuid=r["uuid"],
            name=r["name"],
            status=r["status"],
            origin=r["origin"],
            classification=cls,
            spawn_reason=reason,
            dispatch_mode=dispatch_mode,
            run_count=r["run_count"] or 0,
            print_run_count=r["print_run_count"] or 0,
            last_run_index=r["last_run_index"],
            last_run_name=r["last_run_name"],
            last_run_mode=r["last_run_mode"],
            last_run_status=r["last_run_status"],
            last_run_started_at=r["last_run_started_at"],
            last_run_ended_at=r["last_run_ended_at"],
            model=r["model"],
            permission_mode=r["permission_mode"],
            brief=r["brief"],
            claude_name=r["claude_name"],
            claude_status=r["claude_status"],
            claude_status_at=r["claude_status_at"],
            project_id=r["project_id"],
            project_name=r["project_name"],
            project_path=r["project_path"],
            project_role=r["project_role"],
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
            ended_at=r["ended_at"],
        ))
        if limit is not None and len(out) >= limit:
            break
    return out


def is_spawn(view: HermesSessionView) -> bool:
    """True iff this session belongs in the 'Hermes 스폰' dashboard tab."""
    return view.classification in SPAWN_CLASSIFICATIONS


def hermes_session_counters(views: list[HermesSessionView]) -> dict[str, int]:
    """Roll-up counters used by the dashboard summary cards."""
    n = len(views)
    by_status: dict[str, int] = {}
    by_classification: dict[str, int] = {}
    print_runs = 0
    interactive_runs = 0
    for v in views:
        by_status[v.status] = by_status.get(v.status, 0) + 1
        by_classification[v.classification] = (
            by_classification.get(v.classification, 0) + 1
        )
        print_runs += v.print_run_count
        interactive_runs += max(v.run_count - v.print_run_count, 0)
    spawn_total = by_classification.get("a_ok_spawned", 0)
    return {
        "hermes_ledger_total":      n,
        "hermes_active":            by_status.get("active", 0),
        "hermes_done":              by_status.get("done", 0),
        "hermes_failed":            by_status.get("failed", 0),
        # Spawn tab = a_ok_spawned (= name/last_run_name matches an a-ok
        # spawn prefix). print/interactive run counts below are info only.
        "hermes_spawned":           spawn_total,
        "hermes_a_ok_spawned":      spawn_total,
        "hermes_interactive_multi": by_classification.get("interactive_multi", 0),
        "hermes_native":            by_classification.get("native", 0),
        # Independent of the spawn/native split — useful for "how many of
        # our sessions ever ran print mode" auditing.
        "hermes_print_runs_total":  print_runs,
        "hermes_interactive_runs_total": interactive_runs,
        "hermes_sessions_with_print_run":
            sum(1 for v in views if v.print_run_count > 0),
    }
