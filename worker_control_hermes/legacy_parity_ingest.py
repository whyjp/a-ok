"""Ingestion driver — scan claude+hermes transcripts and write parity rows.

Called every heartbeat tick. Cheap: skips transcripts whose mtime hasn't
moved since the last sync (the ``transcript_mtime`` column on the target
parity table is the watermark).

Two scan passes, two destination tables:
1. Claude jsonl under ``~/.claude/projects/<encoded>/<uuid>.jsonl``
   → ``claude_session_parity`` (PK ``session_uuid``).
2. Hermes session_*.json under ``~/AppData/Local/hermes/profiles/*/sessions``
   → ``hermes_agent_sessions`` (PK ``hermes_session_id``).

The parser is shared (``legacy_parity_parser``); only the row-upsert
target differs by source. Child tables (PR links, files, tools, recaps,
queue) are shared — they're keyed by ``session_uuid`` and the parser
produces identical-shaped child rows for both sources.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path
from typing import Any

from .legacy_parity_parser import (
    parse_claude_jsonl,
    parse_hermes_session_json,
)
from .legacy_parity_schema import (
    CHILD_TABLES,
    apply_legacy_parity_schema,
    replace_child_rows,
    upsert_claude_parity_row,
    upsert_session_row,
)


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
HERMES_HOME_DIR = Path.home() / "AppData" / "Local" / "hermes" / "profiles"


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)


def _mtime_iso(p: Path) -> str | None:
    try:
        return _dt.datetime.fromtimestamp(
            p.stat().st_mtime, tz=_dt.timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        return None


def _worker_name_lookup(conn: sqlite3.Connection) -> dict[str, str]:
    """uuid → hermes_sessions.name (so spawn detection can use the stable slug)."""
    try:
        return {
            r[0].lower(): r[1]
            for r in conn.execute("SELECT uuid, name FROM hermes_sessions")
        }
    except sqlite3.OperationalError:
        return {}


def _existing_hermes_watermarks(conn: sqlite3.Connection) -> dict[str, str]:
    """hermes_session_id → previously-stored transcript_mtime (Hermes side).

    Works regardless of whether the caller set conn.row_factory — we read
    by positional index so we never depend on Row column access.
    """
    try:
        return {
            r[0]: (r[1] or "")
            for r in conn.execute(
                "SELECT hermes_session_id, transcript_mtime FROM hermes_agent_sessions"
            )
        }
    except sqlite3.OperationalError:
        return {}


def _existing_claude_watermarks(conn: sqlite3.Connection) -> dict[str, str]:
    """session_uuid → previously-stored transcript_mtime (Claude side)."""
    try:
        return {
            r[0]: (r[1] or "")
            for r in conn.execute(
                "SELECT session_uuid, transcript_mtime FROM claude_session_parity"
            )
        }
    except sqlite3.OperationalError:
        return {}


def _commit_parsed(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    *,
    target: str = "hermes",
) -> None:
    """Apply one parser result to the DB (row UPSERT + child DELETE+INSERT).

    ``target='hermes'`` writes the parent row to ``hermes_agent_sessions``;
    ``target='claude'`` writes it to ``claude_session_parity``. Child tables
    are shared and use the same DELETE+INSERT path either way.
    """
    if target == "claude":
        upsert_claude_parity_row(conn, parsed["row"])
    else:
        upsert_session_row(conn, parsed["row"])
    sess_uuid = parsed["session_uuid"]
    replace_child_rows(
        conn, "session_pr_links", sess_uuid,
        [{"url": r["url"], "num": r.get("num"), "repo": r.get("repo"),
          "kind": r.get("kind")} for r in parsed["pr_links"]],
    )
    replace_child_rows(
        conn, "session_files_touched", sess_uuid,
        [{"path": r["path"], "last_seen_at": r.get("last_seen_at"),
          "op": r.get("op")} for r in parsed["files_touched"]],
    )
    replace_child_rows(
        conn, "session_tools_recent", sess_uuid,
        [{"ord": r["ord"], "name": r["name"], "snippet": r.get("snippet"),
          "ts": r.get("ts")} for r in parsed["tools_recent"]],
    )
    replace_child_rows(
        conn, "session_recaps", sess_uuid,
        [{"ord": r["ord"], "content": r["content"], "ts": r.get("ts")}
         for r in parsed["recaps"]],
    )
    replace_child_rows(
        conn, "session_pending_queue", sess_uuid,
        [{"ord": r["ord"], "text": r["text"], "queued_at": r.get("queued_at")}
         for r in parsed["pending"]],
    )


def ingest_all(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, int]:
    """Scan both sources, parse what's changed, write to DB.

    Returns counters: {"claude_scanned", "claude_updated",
    "hermes_scanned", "hermes_updated", "skipped"}.

    Also opportunistically applies the split-claude-parity data migration —
    new hosts get the row-shuffle automatically on the first heartbeat tick
    instead of requiring an out-of-band ``workerctl-hermes-migrate`` run.
    The migration is idempotent + cheap when there's nothing to move
    (only reads ``hermes_agent_sessions.transcript_path``), so calling it
    every tick is safe.
    """
    apply_legacy_parity_schema(conn)
    # Auto-apply pending data migrations. Today there's exactly one
    # (split-claude-parity); register additional ones here as they land.
    # Each migration's ``migrate()`` must be idempotent + safe to call on
    # every heartbeat tick.
    try:
        from worker_control_hermes.migrations._2026_split_claude_parity import (
            migrate as _split_claude_parity,
        )
        _split_claude_parity(conn)
    except Exception:
        # Never let a migration hiccup take down the heartbeat — the
        # out-of-band ``workerctl-hermes-migrate`` CLI remains the
        # authoritative recovery path.
        pass
    worker_names = _worker_name_lookup(conn)
    claude_watermarks = _existing_claude_watermarks(conn)
    hermes_watermarks = _existing_hermes_watermarks(conn)
    stats = {"claude_scanned": 0, "claude_updated": 0,
             "hermes_scanned": 0, "hermes_updated": 0, "skipped": 0}

    # --- Claude jsonls ----------------------------------------------------
    if CLAUDE_PROJECTS_DIR.is_dir():
        for proj in CLAUDE_PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            for jl in proj.glob("*.jsonl"):
                uid = jl.stem.lower()
                if not _UUID_RE.fullmatch(uid):
                    continue
                stats["claude_scanned"] += 1
                mt = _mtime_iso(jl)
                if (
                    not force
                    and claude_watermarks.get(uid) == mt
                    and mt is not None
                ):
                    stats["skipped"] += 1
                    continue
                parsed = parse_claude_jsonl(
                    jl,
                    session_uuid=uid,
                    worker_name=worker_names.get(uid),
                )
                # Override mtime with disk value so re-skip logic works.
                parsed["row"]["transcript_mtime"] = mt
                _commit_parsed(conn, parsed, target="claude")
                stats["claude_updated"] += 1

    # --- Hermes session_*.json -------------------------------------------
    if HERMES_HOME_DIR.is_dir():
        for prof in HERMES_HOME_DIR.iterdir():
            sess_dir = prof / "sessions"
            if not sess_dir.is_dir():
                continue
            for js in sess_dir.glob("session_*.json"):
                stats["hermes_scanned"] += 1
                # The hermes session id IS the file stem.
                hsid = js.stem
                mt = _mtime_iso(js)
                if (
                    not force
                    and hermes_watermarks.get(hsid) == mt
                    and mt is not None
                ):
                    stats["skipped"] += 1
                    continue
                parsed = parse_hermes_session_json(
                    js, profile_name=prof.name, profile_path=str(prof),
                )
                parsed["row"]["transcript_mtime"] = mt
                _commit_parsed(conn, parsed, target="hermes")
                stats["hermes_updated"] += 1

    conn.commit()
    return stats
