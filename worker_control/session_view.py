"""session_view — single reader for the unified session ledger.

Phase 2 PR #5. Before this module, three independent consumers
(``dashboard.collect_snapshot``, ``worker_control_hermes.heartbeat``,
``worker_control_hermes.build_report``) each read ``hermes_sessions``
their own way — and ``heartbeat`` even synthesised in-memory rows for
jsonl files the ledger hadn't picked up yet (lines 339-364 of
heartbeat.py before this PR). That made the dashboard / report / Slack
heartbeat disagree about what the host's "current" session set actually
was, and any new consumer would have to re-implement the join.

With PR #4's ``session_sync`` populating ``hermes_sessions`` from every
disk source, this reader can be the sole join point. It returns one
``SessionView`` per ledger row, each pre-joined with:

* the most-recent ``hermes_runs`` row (for ``last_run_*`` + counts),
* the linked ``projects`` row (name / path / role),
* the matching ``hermes_agent_sessions`` parity payload (kind, git_branch,
  msg counts, ai_title, summary, recap texts, …) — keyed by
  ``hermes_agent_sessions.hermes_session_id == hermes_sessions.uuid``
  for the claude-jsonl rows that share UUIDs with the agent-side
  identifier (the common case on this host),
* the five legacy-parity child tables (``session_pr_links``,
  ``session_files_touched``, ``session_tools_recent``, ``session_recaps``,
  ``session_pending_queue``) keyed by ``session_uuid``.

The reader does NO writes and NO disk reads — ``session_sync`` owns
those. Classification (``a_ok_spawned`` / ``interactive_multi`` /
``native``) is the same prefix rule used by ``hermes_ledger`` so the
spawn vs native tab split doesn't drift.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from worker_control.db import connect


# Same constants/rules as worker_control.hermes_ledger so the spawn rule
# is shared verbatim. We don't import from hermes_ledger to keep the
# direction one-way (consumers will migrate off hermes_ledger; we don't
# want a cycle while that happens).
A_OK_SPAWN_PREFIX = "a-ok:"


def _load_extra_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("WORKER_CONTROL_EXTRA_SPAWN_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _all_spawn_prefixes() -> tuple[str, ...]:
    return (A_OK_SPAWN_PREFIX,) + _load_extra_prefixes()


SPAWN_CLASSIFICATIONS = frozenset({"a_ok_spawned"})


# ---------------------------------------------------------------------------
# View dataclass — every row carries the full join in one shape so
# consumers never have to LEFT JOIN anything extra.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionView:
    """One unified session row — ledger + agent-parity + child arrays."""

    # ── hermes_sessions ──────────────────────────────────────────────────
    id: int
    uuid: str
    name: str
    status: str
    origin: str
    model: str | None
    permission_mode: str | None
    brief: str | None
    notes: str | None
    claude_name: str | None
    claude_status: str | None
    claude_status_at: str | None
    created_at: str
    last_used_at: str
    ended_at: str | None
    # Derived (computed in reader, not stored):
    classification: str
    spawn_reason: str | None
    dispatch_mode: str

    # ── runs aggregates / latest run ─────────────────────────────────────
    run_count: int
    print_run_count: int
    last_run_index: int | None
    last_run_name: str | None
    last_run_mode: str | None
    last_run_status: str | None
    last_run_started_at: str | None
    last_run_ended_at: str | None

    # ── projects (via project_id) ────────────────────────────────────────
    project_id: int | None
    project_name: str | None
    project_path: str | None
    project_role: str | None

    # ── hermes_agent_sessions (parity + agent metadata) ──────────────────
    # All optional — these are NULL for sessions that the agent-sync
    # worker hasn't seen yet (e.g. brand-new spawned rows whose transcript
    # the sync loop is about to pick up).
    agent_kind: str | None = None
    git_branch: str | None = None
    claude_version: str | None = None
    msg_user: int = 0
    msg_assistant: int = 0
    msg_tool: int = 0
    ai_title: str | None = None
    summary: str | None = None
    first_user_text: str | None = None
    last_user_text: str | None = None
    last_assistant_text: str | None = None
    transcript_size_bytes: int | None = None
    spawn_slug: str | None = None
    # NOTE: kept distinct from the ledger-side spawn_reason (which comes
    # from the prefix rule) so consumers can prefer the ledger value and
    # fall back to the agent parser's reason only when explicit.
    spawn_reason_agent: str | None = None
    is_spawned_agent: bool = False
    effective_status: str | None = None
    agent_profile_name: str | None = None
    agent_profile_path: str | None = None
    transcript_path: str | None = None
    transcript_mtime: str | None = None
    agent_started_at: str | None = None
    agent_ended_at: str | None = None
    agent_model: str | None = None
    turn_count: int = 0
    agent_first_message: str | None = None
    agent_last_message: str | None = None
    cwd: str | None = None
    total_cost_usd: float | None = None
    synced_at: str | None = None

    # ── parity child tables ──────────────────────────────────────────────
    pr_links: list[dict[str, Any]] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    tools_recent: list[dict[str, Any]] = field(default_factory=list)
    recaps: list[dict[str, Any]] = field(default_factory=list)
    pending_queue: list[dict[str, Any]] = field(default_factory=list)

    # ── derived display state (computed, not stored) ─────────────────────
    # Three buckets the dashboard renders as colored pills:
    #   active   = session is or was recently touched (≤ 2h)
    #   inactive = touched within 2h–24h ago
    #   done     = ended explicitly, or status indicates terminal, or ≥ 24h
    # Precedence: ended_at / terminal status > effective_status > recency.
    display_status: str = "inactive"

    # Sibling UUIDs collapsed into this row by ``list_sessions(group_dupes=…)``.
    # Empty under ``group_dupes="off"``; populated when the SELECT-axis dedup
    # policy folds same-name rows into a single visible card.
    superseded_by: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL (split out so tests can introspect column expectations)
# ---------------------------------------------------------------------------

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
    s.model, s.permission_mode, s.brief, s.notes,
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
"""


# Same column list `_load_parity_extras` reads, ordered to match the
# SessionView field mapping below. Kept in one place so adding a parity
# column means touching this string + the dataclass + the mapping.
_AGENT_COLS = (
    "kind", "git_branch", "claude_version",
    "msg_user", "msg_assistant", "msg_tool",
    "ai_title", "summary",
    "first_user_text", "last_user_text", "last_assistant_text",
    "size_bytes", "spawn_slug", "spawn_reason", "is_spawned",
    "effective_status",
    "profile_name", "profile_path",
    "transcript_path", "transcript_mtime",
    "started_at", "ended_at",
    "model", "turn_count",
    "first_message", "last_message",
    "cwd", "total_cost_usd", "synced_at",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _matches_prefix(name: str | None, prefixes: tuple[str, ...]) -> str | None:
    if not name:
        return None
    for p in prefixes:
        if name.startswith(p):
            return p
    return None


_TERMINAL_STATUSES = frozenset({"done", "failed", "abandoned"})

# Recency thresholds used by ``_compute_display_status``.
# Kept as module-level constants so tests can pin them.
ACTIVE_WINDOW_SECONDS = 2 * 60 * 60      # ≤ 2h     → active
INACTIVE_WINDOW_SECONDS = 24 * 60 * 60   # 2h–24h   → inactive; ≥ 24h → done


def _parse_iso_dt(ts: str | None):
    """Best-effort ISO-8601 parse — returns aware UTC datetime or None.

    Mirrors ``heartbeat._parse_iso`` minus its dependency on a module-level
    NOW, so the session_view reader can be tested deterministically.
    """
    if not ts:
        return None
    import datetime as _dt
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _compute_display_status(
    *,
    status: str | None,
    ended_at: str | None,
    effective_status: str | None,
    last_used_at: str | None,
    now=None,
) -> str:
    """Return ``"active" | "inactive" | "done"`` for a session row.

    Precedence (highest first):

    1. ``ended_at`` set OR ledger ``status`` is terminal       → ``done``
    2. agent-parity ``effective_status == 'active'`` AND
       ``last_used_at`` ≤ 2h ago                               → ``active``
       (cross-validated — a stale ``effective_status`` from a stuck
       watermark gets downgraded by recency, defense-in-depth against the
       write-path bug fixed in ``legacy_parity_ingest._refresh_effective_status``.)
    3. recency of ``last_used_at`` against fixed thresholds:
       ≤ 2h → ``active``;  ≤ 24h → ``inactive``;  > 24h → ``done``

    Missing/unparseable ``last_used_at`` falls back to ``inactive`` so a
    row without a timestamp doesn't look greener than rows that have one.
    """
    if ended_at:
        return "done"
    if (status or "").lower() in _TERMINAL_STATUSES:
        return "done"

    import datetime as _dt
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    last = _parse_iso_dt(last_used_at)
    age_s: float | None
    if last is None:
        age_s = None
    else:
        age_s = (now - last).total_seconds()

    if (effective_status or "").lower() == "active":
        # Trust the parity flag only if recency agrees. When last_used_at
        # is missing we still trust the flag (no signal to override with).
        if age_s is None or age_s < 0 or age_s <= ACTIVE_WINDOW_SECONDS:
            return "active"
        if age_s <= INACTIVE_WINDOW_SECONDS:
            return "inactive"
        return "done"

    if age_s is None:
        return "inactive"
    if age_s < 0:
        # Future timestamp — treat as just-touched.
        return "active"
    if age_s <= ACTIVE_WINDOW_SECONDS:
        return "active"
    if age_s <= INACTIVE_WINDOW_SECONDS:
        return "inactive"
    return "done"


def _classify(
    *,
    name: str,
    last_run_name: str | None,
    has_any_run: bool,
    spawn_prefixes: tuple[str, ...],
) -> tuple[str, str | None]:
    """Spawn / interactive / native bucket — same rule as hermes_ledger."""
    matched = _matches_prefix(name, spawn_prefixes) \
        or _matches_prefix(last_run_name, spawn_prefixes)
    if matched is not None:
        return "a_ok_spawned", f"prefix:{matched}"
    if has_any_run:
        return "interactive_multi", None
    return "native", None


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _has_hermes_tables(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "hermes_sessions") and _has_table(conn, "hermes_runs")


def _safe_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _auto_reclassify_origins(conn: sqlite3.Connection,
                              spawn_prefixes: tuple[str, ...]) -> None:
    """Mirror ``hermes_ledger.auto_reclassify_origins`` — keep origin in
    sync with the prefix rule so newly-allocated sessions appear in the
    spawn tab without a manual ``workerctl session reclassify``. Failures
    are non-fatal (e.g. ``hermes_runs`` missing).
    """
    if not spawn_prefixes:
        return
    sess_pred = " OR ".join(["name GLOB ?"] * len(spawn_prefixes))
    run_pred  = " OR ".join(["r.name GLOB ?"] * len(spawn_prefixes))
    args = tuple(f"{p}*" for p in spawn_prefixes) * 2
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
        return


# ---------------------------------------------------------------------------
# Parity extras loaders (formerly inside dashboard._load_parity_extras —
# moved here so every consumer gets the same shape from one place).
# ---------------------------------------------------------------------------


def _load_agent_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Map ``lower(hermes_session_id)`` → agent_session column dict.

    Defensive against a DB where the legacy-parity migration hasn't run —
    columns are picked per-table from PRAGMA so missing ones become NULL
    rather than raising.

    Post-split this table holds ONLY Hermes-profile session rows (PK is the
    hermes session_<host>_<ts>_<rand> id). Native claude transcripts live
    in ``claude_session_parity`` and are loaded via
    :func:`_load_claude_parity_rows`.
    """
    out: dict[str, dict[str, Any]] = {}
    if not _has_table(conn, "hermes_agent_sessions"):
        return out
    have = _safe_columns(conn, "hermes_agent_sessions")
    cols = ["hermes_session_id"] + [c for c in _AGENT_COLS if c in have]
    sql = "SELECT " + ", ".join(cols) + " FROM hermes_agent_sessions"
    try:
        cur = conn.execute(sql)
    except sqlite3.OperationalError:
        return out
    for r in cur.fetchall():
        sid = (r["hermes_session_id"] or "").lower()
        if not sid:
            continue
        # Re-key with `None` for missing parity columns so downstream
        # consumers don't have to ``hasattr``-check.
        row: dict[str, Any] = {c: (r[c] if c in have else None) for c in _AGENT_COLS}
        out[sid] = row
    return out


def _load_claude_parity_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Map ``lower(session_uuid)`` → claude_session_parity column dict.

    Same shape as :func:`_load_agent_rows` so the ledger assembly code can
    treat either source identically (profile_name/profile_path don't apply
    to native claude rows and are returned as ``None``).
    """
    out: dict[str, dict[str, Any]] = {}
    if not _has_table(conn, "claude_session_parity"):
        return out
    have = _safe_columns(conn, "claude_session_parity")
    cols = ["session_uuid"] + [c for c in _AGENT_COLS if c in have]
    sql = "SELECT " + ", ".join(cols) + " FROM claude_session_parity"
    try:
        cur = conn.execute(sql)
    except sqlite3.OperationalError:
        return out
    for r in cur.fetchall():
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        row: dict[str, Any] = {c: (r[c] if c in have else None) for c in _AGENT_COLS}
        out[sid] = row
    return out


def _load_parent_hermes_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Map ``lower(hermes_sessions.uuid)`` → parent ``hermes_runs.hermes_session_id``.

    Used by the ledger reader to decide which parity table holds the
    session's agent payload — if a parent hermes session_id is set, the
    metadata comes from ``hermes_agent_sessions``; otherwise the row is a
    native claude session and its metadata lives in
    ``claude_session_parity``.
    """
    out: dict[str, str] = {}
    if not _has_table(conn, "hermes_runs") or not _has_table(conn, "hermes_sessions"):
        return out
    try:
        cur = conn.execute(
            "SELECT s.uuid AS uuid, r.hermes_session_id AS hsid "
            "FROM hermes_runs r JOIN hermes_sessions s ON s.id = r.session_id "
            "WHERE r.hermes_session_id IS NOT NULL"
        )
    except sqlite3.OperationalError:
        return out
    for r in cur.fetchall():
        sid = (r["uuid"] or "").lower()
        hsid = (r["hsid"] or "")
        if not sid or not hsid:
            continue
        # Keep the most recently inserted hermes_session_id when multiple
        # runs link to different parents (rare; the row gets overwritten).
        out[sid] = hsid
    return out


def _load_child_arrays(conn: sqlite3.Connection) -> dict[str, dict[str, list]]:
    """Map ``lower(session_uuid)`` → ``{pr_links, files_touched, tools_recent,
    recaps, pending_queue}`` arrays.

    Caps mirror the legacy report (top-N most-recent) so a session with
    thousands of tool invocations doesn't drag the dashboard payload.
    """
    out: dict[str, dict[str, list]] = {}

    def _bucket(sid: str) -> dict[str, list]:
        b = out.get(sid)
        if b is None:
            b = {
                "pr_links": [],
                "files_touched": [],
                "tools_recent": [],
                "recaps": [],
                "pending_queue": [],
            }
            out[sid] = b
        return b

    def _safe(sql: str, params: Iterable[Any] = ()) -> Iterable[sqlite3.Row]:
        try:
            return list(conn.execute(sql, tuple(params)))
        except sqlite3.OperationalError:
            return []

    for r in _safe(
        "SELECT session_uuid, url, num, repo, kind FROM session_pr_links"
    ):
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        _bucket(sid)["pr_links"].append({
            "url": r["url"], "num": r["num"],
            "repo": r["repo"], "kind": r["kind"],
        })

    for r in _safe(
        "SELECT session_uuid, path FROM session_files_touched "
        "ORDER BY last_seen_at DESC NULLS LAST"
    ):
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        b = _bucket(sid)
        if len(b["files_touched"]) < 20:
            b["files_touched"].append(r["path"])

    for r in _safe(
        "SELECT session_uuid, name, snippet, ts FROM session_tools_recent "
        "ORDER BY ord DESC"
    ):
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        b = _bucket(sid)
        if len(b["tools_recent"]) < 8:
            b["tools_recent"].append({
                "name": r["name"], "snippet": r["snippet"], "ts": r["ts"],
            })

    for r in _safe(
        "SELECT session_uuid, content, ts FROM session_recaps "
        "ORDER BY ord DESC"
    ):
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        b = _bucket(sid)
        if len(b["recaps"]) < 5:
            b["recaps"].append({"content": r["content"], "ts": r["ts"]})

    for r in _safe(
        "SELECT session_uuid, text, queued_at FROM session_pending_queue "
        "ORDER BY ord ASC"
    ):
        sid = (r["session_uuid"] or "").lower()
        if not sid:
            continue
        _bucket(sid)["pending_queue"].append({
            "text": r["text"], "queued_at": r["queued_at"],
        })

    return out


# ---------------------------------------------------------------------------
# Public reader
# ---------------------------------------------------------------------------


DedupPolicy = Literal["off", "by_name", "by_name_within_hsid"]


def _dedup_views(
    views: list[SessionView],
    *,
    policy: DedupPolicy,
    parent_hsid: dict[str, str],
) -> list[SessionView]:
    """Collapse same-name rows per the SELECT-axis dedup policy.

    Input is already ordered newest-first by ``last_used_at``, so the first
    occurrence per group key is the winner; later occurrences are recorded
    on the winner's ``superseded_by`` list (newest superseded first) and
    dropped from the returned list.

    ``policy="by_name_within_hsid"`` keys by ``(name, parent_hsid)``. A row
    with no parent hsid (native row) keys by ``(name, uuid)`` so distinct
    native rows of the same name never collapse — the policy exists to fix
    the spawn-side duplicate-card artifact, not to merge native sessions.
    """
    if policy == "off" or not views:
        return views
    grouped: dict[tuple[str, str], SessionView] = {}
    order: list[tuple[str, str]] = []
    for v in views:
        if policy == "by_name":
            key = (v.name or "", "*")
        else:  # by_name_within_hsid
            hsid = parent_hsid.get((v.uuid or "").lower(), "")
            # Fall back to uuid when no parent hsid — keeps native rows distinct.
            key = (v.name or "", hsid or f"__noparent__:{v.uuid}")
        winner = grouped.get(key)
        if winner is None:
            grouped[key] = v
            order.append(key)
        else:
            winner.superseded_by.append(v.uuid)
    return [grouped[k] for k in order]


def list_sessions(
    *,
    classification: str | Iterable[str] | None = None,
    freshness: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    group_dupes: DedupPolicy = "by_name_within_hsid",
) -> list[SessionView]:
    """Return one ``SessionView`` per ``hermes_sessions`` row.

    Parameters
    ----------
    classification:
        Restrict to one or more classification buckets. ``"spawned"`` is
        accepted as a shorthand for ``a_ok_spawned``; ``"native"`` means
        any non-spawn (i.e. ``interactive_multi`` ∪ ``native``) to match
        the dashboard's "Native 세션" tab semantics. ``None`` returns all.
    freshness:
        Reserved for future windowed filters (e.g. ``"5min"``, ``"30min"``).
        Today only ``None`` and ``"any"`` are honoured — callers needing
        strict windowing should pass ``since`` directly.
    since:
        ISO-8601 string; only rows with ``last_used_at >= since`` are kept.
    limit:
        Cap the number of rows returned.

    Returns rows ordered newest-first by ``last_used_at``.
    """
    spawn_prefixes = _all_spawn_prefixes()
    with connect() as conn:
        if not _has_hermes_tables(conn):
            return []
        _auto_reclassify_origins(conn, spawn_prefixes)
        agent_rows = _load_agent_rows(conn)
        claude_parity_rows = _load_claude_parity_rows(conn)
        parent_hsid = _load_parent_hermes_map(conn)
        child_arrays = _load_child_arrays(conn)
        sql = _LIST_SQL + " ORDER BY s.last_used_at DESC, s.id DESC"
        rows = conn.execute(sql).fetchall()

    # Normalize classification filter into a set.
    if isinstance(classification, str):
        wanted = {classification}
    elif classification is None:
        wanted = None
    else:
        wanted = set(classification)
    if wanted is not None:
        wanted = {("a_ok_spawned" if c == "spawned" else c) for c in wanted}

    # Resolve the schema once outside the loop so the (rare) case where
    # `notes` isn't yet present in an older DB doesn't crash the join.
    out: list[SessionView] = []
    for r in rows:
        cls, reason = _classify(
            name=r["name"],
            last_run_name=r["last_run_name"],
            has_any_run=(r["run_count"] or 0) > 0,
            spawn_prefixes=spawn_prefixes,
        )

        # Classification filter — accept shorthand: "native" matches BOTH
        # native and interactive_multi (the dashboard's Native tab shows both).
        if wanted is not None:
            keep = cls in wanted
            if not keep and "native" in wanted and cls == "interactive_multi":
                keep = True
            if not keep:
                continue

        if since is not None and (r["last_used_at"] or "") < since:
            continue
        # `freshness` is a forward-compat hook; "any" / None pass through.
        if freshness not in (None, "any"):
            # Conservative no-op rather than silently dropping rows on an
            # unknown token — callers using non-default windows should pass
            # `since` explicitly until we implement named windows.
            pass

        # `notes` is older DBs may not have — guard with a try.
        try:
            notes = r["notes"]
        except (IndexError, KeyError):
            notes = None

        uuid_lower = (r["uuid"] or "").lower()
        # Pick the parity payload source: when the session has a parent
        # hermes_session_id (spawned-by-hermes case), prefer the
        # hermes_agent_sessions row keyed by that parent id; fall back to
        # the claude parity row keyed by the session UUID. Native claude
        # rows have no parent and resolve straight to claude_session_parity.
        parent_id = parent_hsid.get(uuid_lower)
        agent: dict[str, Any] = {}
        if parent_id:
            agent = agent_rows.get(parent_id.lower()) or {}
        if not agent:
            agent = claude_parity_rows.get(uuid_lower) or {}
        if not agent:
            # Last-resort fallback for legacy DBs where claude rows still
            # live in hermes_agent_sessions keyed by claude UUID (pre-split).
            agent = agent_rows.get(uuid_lower) or {}
        kids = child_arrays.get(uuid_lower) or {}

        out.append(SessionView(
            id=r["id"],
            uuid=r["uuid"],
            name=r["name"],
            status=r["status"],
            origin=r["origin"],
            model=r["model"],
            permission_mode=r["permission_mode"],
            brief=r["brief"],
            notes=notes,
            claude_name=r["claude_name"],
            claude_status=r["claude_status"],
            claude_status_at=r["claude_status_at"],
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
            ended_at=r["ended_at"],
            classification=cls,
            spawn_reason=reason,
            dispatch_mode=r["last_run_mode"] or "—",
            run_count=r["run_count"] or 0,
            print_run_count=r["print_run_count"] or 0,
            last_run_index=r["last_run_index"],
            last_run_name=r["last_run_name"],
            last_run_mode=r["last_run_mode"],
            last_run_status=r["last_run_status"],
            last_run_started_at=r["last_run_started_at"],
            last_run_ended_at=r["last_run_ended_at"],
            project_id=r["project_id"],
            project_name=r["project_name"],
            project_path=r["project_path"],
            project_role=r["project_role"],
            # parity columns
            agent_kind=agent.get("kind"),
            git_branch=agent.get("git_branch"),
            claude_version=agent.get("claude_version"),
            msg_user=int(agent.get("msg_user") or 0),
            msg_assistant=int(agent.get("msg_assistant") or 0),
            msg_tool=int(agent.get("msg_tool") or 0),
            ai_title=agent.get("ai_title"),
            summary=agent.get("summary"),
            first_user_text=agent.get("first_user_text"),
            last_user_text=agent.get("last_user_text"),
            last_assistant_text=agent.get("last_assistant_text"),
            transcript_size_bytes=agent.get("size_bytes"),
            spawn_slug=agent.get("spawn_slug"),
            spawn_reason_agent=agent.get("spawn_reason"),
            is_spawned_agent=bool(agent.get("is_spawned") or 0),
            effective_status=agent.get("effective_status"),
            agent_profile_name=agent.get("profile_name"),
            agent_profile_path=agent.get("profile_path"),
            transcript_path=agent.get("transcript_path"),
            transcript_mtime=agent.get("transcript_mtime"),
            agent_started_at=agent.get("started_at"),
            agent_ended_at=agent.get("ended_at"),
            agent_model=agent.get("model"),
            turn_count=int(agent.get("turn_count") or 0),
            agent_first_message=agent.get("first_message"),
            agent_last_message=agent.get("last_message"),
            cwd=agent.get("cwd"),
            total_cost_usd=agent.get("total_cost_usd"),
            synced_at=agent.get("synced_at"),
            pr_links=kids.get("pr_links", []),
            files_touched=kids.get("files_touched", []),
            tools_recent=kids.get("tools_recent", []),
            recaps=kids.get("recaps", []),
            pending_queue=kids.get("pending_queue", []),
            display_status=_compute_display_status(
                status=r["status"],
                ended_at=r["ended_at"],
                effective_status=agent.get("effective_status"),
                last_used_at=r["last_used_at"],
            ),
        ))

    # SELECT-axis dedup runs before `limit` so the cap counts visible rows,
    # not pre-collapse duplicates.
    out = _dedup_views(out, policy=group_dupes, parent_hsid=parent_hsid)
    if limit is not None and len(out) > limit:
        out = out[:limit]
    return out


def get_session(key: str) -> SessionView | None:
    """Lookup a single session by UUID, integer id, or name fragment.

    Used by ``build_report.py`` which historically did its own fuzzy
    matching against ``hermes_sessions``. Centralised here so the
    matching rules stay consistent across consumers.

    Returns ``None`` if nothing matches. Raises ``LookupError`` if the
    key is ambiguous (multiple matches by name fragment).
    """
    rows = list_sessions()
    if not rows:
        return None
    k = key.strip()
    klower = k.lower()
    # 1) Exact UUID
    for v in rows:
        if (v.uuid or "").lower() == klower:
            return v
    # 2) Numeric id
    if k.isdigit():
        target = int(k)
        for v in rows:
            if v.id == target:
                return v
    # 3) Exact name
    for v in rows:
        if v.name == k:
            return v
    # 4) UUID prefix or name fragment
    matches = [
        v for v in rows
        if (v.uuid or "").lower().startswith(klower) or k in (v.name or "")
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"#{v.id} {v.name}" for v in matches)
        raise LookupError(f"Ambiguous session key {key!r}: {names}")
    return None


def is_spawn(view: SessionView) -> bool:
    """True iff this row belongs in the 'Hermes 스폰' dashboard tab."""
    return view.classification in SPAWN_CLASSIFICATIONS


def session_counters(views: list[SessionView]) -> dict[str, int]:
    """Roll-up counters used by the dashboard summary cards.

    Same key set as ``hermes_ledger.hermes_session_counters`` so the FE
    JSON shape doesn't shift when we swap consumers over.
    """
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
        "hermes_ledger_total":      len(views),
        "hermes_active":            by_status.get("active", 0),
        "hermes_done":              by_status.get("done", 0),
        "hermes_failed":            by_status.get("failed", 0),
        "hermes_spawned":           spawn_total,
        "hermes_a_ok_spawned":      spawn_total,
        "hermes_interactive_multi": by_classification.get("interactive_multi", 0),
        "hermes_native":            by_classification.get("native", 0),
        "hermes_print_runs_total":  print_runs,
        "hermes_interactive_runs_total": interactive_runs,
        "hermes_sessions_with_print_run":
            sum(1 for v in views if v.print_run_count > 0),
    }


__all__ = [
    "A_OK_SPAWN_PREFIX",
    "SPAWN_CLASSIFICATIONS",
    "SessionView",
    "get_session",
    "is_spawn",
    "list_sessions",
    "session_counters",
]
