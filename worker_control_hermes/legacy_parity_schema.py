"""Legacy-parity schema for per-session metadata (two tables, one shape).

Goal: bring the canonical worker-control SQLite store up to the field set
that the legacy `sites/1143` report (`C:/Users/cxx/Downloads/index.html`)
ships in its inline `DATA[]` JSON. Same fields, same depth, but split by
source kind so the dashboard's "hermes 세션" panel doesn't get polluted by
native claude jsonls (the original single-table design folded both into
``hermes_agent_sessions`` and broke that panel's semantics).

Two tables, identical column shape modulo the PK and the columns that
only apply to one side:

* ``hermes_agent_sessions`` — Hermes Agent turn-by-turn sessions sourced
  from ``~/AppData/Local/hermes/profiles/<name>/sessions/session_*.json``.
  PK is ``hermes_session_id`` (the hermes-side ``session_<host>_<ts>_<rand>``
  identifier). Populated by ``worker_control.hermes_session_sync`` and the
  Hermes branch of ``legacy_parity_ingest.ingest_all``.
* ``claude_session_parity`` — native Claude code transcripts sourced from
  ``~/.claude/projects/<encoded>/<uuid>.jsonl``. PK is ``session_uuid``
  (the 8-4-4-4-12 claude UUID). Populated by the Claude branch of
  ``legacy_parity_ingest.ingest_all``.

Heavy-cardinality fields (PR links, files, tools, recaps, queue) live in
shared child tables keyed by ``session_uuid`` — they don't care which
source produced the parent row, just the identifier. Each child table has
a stable ord/path UNIQUE so the parser can idempotently DELETE-then-INSERT
inside a single transaction.

Backward compat: the original design extended ``hermes_agent_sessions``
with ``_EXTRA_COLUMNS`` (kind / git_branch / msg_* / …). We keep those
ALTERs in place — the table itself still uses them for the Hermes-source
rows. The ``claude_session_parity`` table is purely additive.

Idempotent: re-running ``apply_legacy_parity_schema(conn)`` is safe.
ALTER TABLE ADD COLUMN is guarded by PRAGMA table_info() so we don't error
on re-apply. New tables use IF NOT EXISTS.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


# ---------------------------------------------------------------------------
# extra columns to add to hermes_agent_sessions
# ---------------------------------------------------------------------------

# (name, sql-type-with-default-suffix). The default is appended verbatim
# after the column type — keep it simple, no constraints.
_EXTRA_COLUMNS: list[tuple[str, str]] = [
    # Origin discriminator (claude jsonl vs hermes transcript).
    ("kind",                 "TEXT NOT NULL DEFAULT 'claude'"),
    # Git context the legacy report shows under the project cell.
    ("git_branch",           "TEXT"),
    ("claude_version",       "TEXT"),
    # Message counters (parsed from the transcript per turn).
    ("msg_user",             "INTEGER NOT NULL DEFAULT 0"),
    ("msg_assistant",        "INTEGER NOT NULL DEFAULT 0"),
    ("msg_tool",             "INTEGER NOT NULL DEFAULT 0"),
    # AI-derived natural-language labels.
    ("ai_title",             "TEXT"),
    ("summary",              "TEXT"),
    # Last user/assistant 200-char excerpts (recap cards).
    ("first_user_text",      "TEXT"),
    ("last_user_text",       "TEXT"),
    ("last_assistant_text",  "TEXT"),
    # Raw transcript size (legacy `size_bytes`).
    ("size_bytes",           "INTEGER"),
    # Spawn-related (parsed from a-ok: prefix conventions).
    ("spawn_slug",           "TEXT"),
    ("spawn_reason",         "TEXT"),
    ("is_spawned",           "INTEGER NOT NULL DEFAULT 0"),
    # Derived bucket: active(<2h) / inactive(<24h) / done.
    ("effective_status",     "TEXT"),
]


# ---------------------------------------------------------------------------
# child tables (heavy-cardinality session attributes)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# claude_session_parity — native ~/.claude/projects/<uuid>.jsonl parity rows
# ---------------------------------------------------------------------------
#
# Column shape mirrors hermes_agent_sessions + _EXTRA_COLUMNS so consumers
# can pull either source through the same loader code (only the PK name
# differs: session_uuid here, hermes_session_id there). ``profile_name``
# and ``profile_path`` are intentionally absent — they don't apply to
# native claude transcripts (those are hermes-profile concepts).

_CLAUDE_PARITY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS claude_session_parity (
    session_uuid      TEXT PRIMARY KEY,
    transcript_path   TEXT,
    transcript_size   INTEGER NOT NULL DEFAULT 0,
    transcript_mtime  TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    model             TEXT,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    first_message     TEXT,
    last_message      TEXT,
    cwd               TEXT,
    total_cost_usd    REAL,
    synced_at         TEXT NOT NULL DEFAULT '',
    -- legacy-parity extras (mirror _EXTRA_COLUMNS on hermes_agent_sessions)
    kind              TEXT NOT NULL DEFAULT 'claude',
    git_branch        TEXT,
    claude_version    TEXT,
    msg_user          INTEGER NOT NULL DEFAULT 0,
    msg_assistant     INTEGER NOT NULL DEFAULT 0,
    msg_tool          INTEGER NOT NULL DEFAULT 0,
    ai_title          TEXT,
    summary           TEXT,
    first_user_text   TEXT,
    last_user_text    TEXT,
    last_assistant_text TEXT,
    size_bytes        INTEGER,
    spawn_slug        TEXT,
    spawn_reason      TEXT,
    is_spawned        INTEGER NOT NULL DEFAULT 0,
    effective_status  TEXT,
    -- Short copy/lookup id. Every parity row is a native (`.claude/projects/`)
    -- transcript by definition, so the prefix is always 'nat#'.
    mention_id        TEXT GENERATED ALWAYS AS (
        'nat#' || substr(session_uuid, 1, 8)
    ) VIRTUAL
);
CREATE INDEX IF NOT EXISTS ix_claude_session_parity_mtime
    ON claude_session_parity(transcript_mtime DESC);
CREATE INDEX IF NOT EXISTS ix_claude_session_parity_mention_id
    ON claude_session_parity(mention_id);
"""


# Column names for claude_session_parity (sans PK) — exposed so the
# upsert helper can build INSERT statements without hard-coding the list.
_CLAUDE_PARITY_COLS: tuple[str, ...] = (
    "session_uuid",
    "transcript_path", "transcript_size", "transcript_mtime",
    "started_at", "ended_at", "model", "turn_count",
    "first_message", "last_message", "cwd", "total_cost_usd",
    "synced_at",
    "kind", "git_branch", "claude_version",
    "msg_user", "msg_assistant", "msg_tool",
    "ai_title", "summary",
    "first_user_text", "last_user_text", "last_assistant_text",
    "size_bytes",
    "spawn_slug", "spawn_reason", "is_spawned",
    "effective_status",
)


_CHILD_TABLES_SQL = """
-- PR / MR URLs detected in transcript text.
CREATE TABLE IF NOT EXISTS session_pr_links (
    session_uuid TEXT NOT NULL,
    url          TEXT NOT NULL,
    num          INTEGER,
    repo         TEXT,
    kind         TEXT,   -- 'github' | 'gitlab' | null
    PRIMARY KEY (session_uuid, url)
);
CREATE INDEX IF NOT EXISTS ix_session_pr_links_uuid ON session_pr_links(session_uuid);

-- Files mentioned in tool_use Edit/Write/Read arguments.
CREATE TABLE IF NOT EXISTS session_files_touched (
    session_uuid TEXT NOT NULL,
    path         TEXT NOT NULL,
    last_seen_at TEXT,
    op           TEXT,   -- 'edit' | 'write' | 'read' | null
    PRIMARY KEY (session_uuid, path)
);
CREATE INDEX IF NOT EXISTS ix_session_files_touched_uuid ON session_files_touched(session_uuid);

-- Recent tool invocations (Bash, Edit, etc.) with a small snippet.
-- `ord` is the parser-assigned ordering (oldest=0, newest=N-1).
CREATE TABLE IF NOT EXISTS session_tools_recent (
    session_uuid TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    name         TEXT NOT NULL,
    snippet      TEXT,
    ts           TEXT,
    PRIMARY KEY (session_uuid, ord)
);
CREATE INDEX IF NOT EXISTS ix_session_tools_recent_uuid ON session_tools_recent(session_uuid);

-- Native /recap responses (claude-written checkpoint summaries).
CREATE TABLE IF NOT EXISTS session_recaps (
    session_uuid TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    content      TEXT NOT NULL,
    ts           TEXT,
    PRIMARY KEY (session_uuid, ord)
);
CREATE INDEX IF NOT EXISTS ix_session_recaps_uuid ON session_recaps(session_uuid);

-- Queued user prompts that have no assistant reply yet.
CREATE TABLE IF NOT EXISTS session_pending_queue (
    session_uuid TEXT NOT NULL,
    ord          INTEGER NOT NULL,
    text         TEXT NOT NULL,
    queued_at    TEXT,
    PRIMARY KEY (session_uuid, ord)
);
CREATE INDEX IF NOT EXISTS ix_session_pending_queue_uuid ON session_pending_queue(session_uuid);
"""


# Child-table names — exposed so callers (parser, tests) can iterate.
CHILD_TABLES: tuple[str, ...] = (
    "session_pr_links",
    "session_files_touched",
    "session_tools_recent",
    "session_recaps",
    "session_pending_queue",
)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}   # r[1] = name


def _ensure_hermes_agent_sessions_exists(conn: sqlite3.Connection) -> None:
    """Create hermes_agent_sessions if it isn't there yet.

    The hermes sync worker normally creates this table on first run; we
    need it before we can ALTER it. Schema mirrors the one in
    ``worker_control.hermes_session_sync`` so a sync worker run after this
    migration is a no-op.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hermes_agent_sessions (
            hermes_session_id TEXT PRIMARY KEY,
            profile_name      TEXT,
            profile_path      TEXT,
            transcript_path   TEXT,
            transcript_size   INTEGER NOT NULL DEFAULT 0,
            transcript_mtime  TEXT,
            started_at        TEXT,
            ended_at          TEXT,
            model             TEXT,
            turn_count        INTEGER NOT NULL DEFAULT 0,
            first_message     TEXT,
            last_message      TEXT,
            cwd               TEXT,
            total_cost_usd    REAL,
            synced_at         TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_hermes_agent_sessions_mtime "
        "ON hermes_agent_sessions(transcript_mtime DESC)"
    )


def apply_legacy_parity_schema(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Idempotently extend the canonical DB with the legacy-parity columns/tables.

    Returns a small audit dict listing what was added — empty list means
    the column/table was already present. Useful for the migration command
    to report what changed in this run.
    """
    audit: dict[str, list[str]] = {"columns_added": [], "tables_added": []}

    _ensure_hermes_agent_sessions_exists(conn)

    existing = _existing_columns(conn, "hermes_agent_sessions")
    for col, decl in _EXTRA_COLUMNS:
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE hermes_agent_sessions ADD COLUMN {col} {decl}")
        audit["columns_added"].append(col)

    # New child tables + claude_session_parity — track which ones the
    # migration actually created by snapshotting the master table before/after.
    before = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.executescript(_CHILD_TABLES_SQL)
    conn.executescript(_CLAUDE_PARITY_TABLE_SQL)
    after = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    audit["tables_added"] = sorted(after - before)

    # Forward-only migration: add the GENERATED VIRTUAL mention_id column to
    # claude_session_parity rows that pre-date this PR. VIRTUAL columns can
    # be added via ALTER TABLE without rebuilding the table.
    # NOTE: ``PRAGMA table_info`` omits GENERATED columns; use ``table_xinfo``
    # so the existence check correctly skips after the column is present.
    parity_cols = {
        r[1] for r in conn.execute("PRAGMA table_xinfo(claude_session_parity)")
    }
    if "mention_id" not in parity_cols:
        conn.execute(
            "ALTER TABLE claude_session_parity ADD COLUMN mention_id TEXT "
            "GENERATED ALWAYS AS ('nat#' || substr(session_uuid, 1, 8)) VIRTUAL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_claude_session_parity_mention_id "
            "ON claude_session_parity(mention_id)"
        )
        audit["columns_added"].append("mention_id")

    conn.commit()
    return audit


# ---------------------------------------------------------------------------
# helpers used by the parser / dashboard
# ---------------------------------------------------------------------------

def upsert_session_row(conn: sqlite3.Connection, row: dict) -> None:
    """UPSERT one hermes_agent_sessions row with all legacy-parity fields.

    Required keys: hermes_session_id (PK), synced_at.
    All other keys are optional and default to NULL / 0 per column defaults.
    """
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "hermes_session_id")
    sql = (
        f"INSERT INTO hermes_agent_sessions({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT(hermes_session_id) DO UPDATE SET {update_clause}"
    )
    conn.execute(sql, [row[c] for c in cols])


def upsert_claude_parity_row(conn: sqlite3.Connection, row: dict) -> None:
    """UPSERT one claude_session_parity row.

    Accepts the same dict shape the parser produces for hermes_agent_sessions
    (PK column ``hermes_session_id`` is renamed to ``session_uuid``;
    ``profile_name``/``profile_path`` are silently dropped — they don't apply
    to native claude transcripts).

    Required keys: hermes_session_id OR session_uuid; synced_at.
    """
    payload = dict(row)
    # Rename PK if caller handed us the hermes-style key.
    if "session_uuid" not in payload and "hermes_session_id" in payload:
        payload["session_uuid"] = payload.pop("hermes_session_id")
    # Drop columns that don't exist on claude_session_parity.
    payload.pop("profile_name", None)
    payload.pop("profile_path", None)
    # Filter to known columns so a stray key doesn't crash the INSERT.
    cols = [c for c in payload.keys() if c in _CLAUDE_PARITY_COLS]
    if "session_uuid" not in cols:
        raise ValueError("upsert_claude_parity_row requires session_uuid")
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    update_clause = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c != "session_uuid"
    )
    sql = (
        f"INSERT INTO claude_session_parity({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT(session_uuid) DO UPDATE SET {update_clause}"
    )
    conn.execute(sql, [payload[c] for c in cols])


def replace_child_rows(
    conn: sqlite3.Connection,
    table: str,
    session_uuid: str,
    rows: Iterable[dict],
) -> None:
    """Replace all rows for this session_uuid in one child table.

    DELETE-then-INSERT inside a single transaction. Each `rows` item is a
    dict whose keys MUST include all NOT-NULL columns for the table.
    """
    if table not in CHILD_TABLES:
        raise ValueError(f"not a legacy-parity child table: {table}")
    conn.execute(f"DELETE FROM {table} WHERE session_uuid=?", (session_uuid,))
    rows_list = list(rows)
    if not rows_list:
        return
    cols = list(rows_list[0].keys())
    if "session_uuid" not in cols:
        cols = ["session_uuid"] + cols
        for r in rows_list:
            r["session_uuid"] = session_uuid
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    conn.executemany(
        f"INSERT OR REPLACE INTO {table}({col_list}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows_list],
    )
