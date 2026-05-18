"""Split native-claude parity rows out of ``hermes_agent_sessions``.

Before this migration both Hermes-profile sessions and native Claude
``~/.claude/projects/<uuid>.jsonl`` sessions were UPSERTed into
``hermes_agent_sessions`` by ``legacy_parity_ingest.ingest_all``. That
polluted the dashboard's "hermes 세션" panel with native claude rows
(seen in production as ``transcript_only`` entries with an empty
``profile_name``) and broke the table's per-docstring invariant
("Hermes Agent turn-by-turn sessions").

This migration moves every row whose ``transcript_path`` lives under
``~/.claude/projects/`` into the new ``claude_session_parity`` table and
deletes it from ``hermes_agent_sessions``. The schema for both tables is
created upfront by ``apply_legacy_parity_schema``; this module only
shuffles rows.

Idempotent: rows already absent from ``hermes_agent_sessions`` are
silently skipped, and the UPSERT into ``claude_session_parity`` is safe
on re-run.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from worker_control_hermes.legacy_parity_schema import (
    _CLAUDE_PARITY_COLS,
    apply_legacy_parity_schema,
    upsert_claude_parity_row,
)


# Match transcript_path values that live under ``~/.claude/projects/`` —
# we accept both POSIX and Windows separators. Anchoring on the literal
# ``.claude`` directory plus ``projects`` avoids false positives on paths
# that happen to contain ``claude`` in a project name.
_CLAUDE_PATH_RE = re.compile(r"[\\/]\.claude[\\/]+projects[\\/]+", re.IGNORECASE)


def _row_is_native_claude(transcript_path: str | None) -> bool:
    if not transcript_path:
        return False
    return _CLAUDE_PATH_RE.search(transcript_path) is not None


def migrate(conn: sqlite3.Connection) -> dict[str, int]:
    """Move native-claude rows from hermes_agent_sessions to claude_session_parity.

    Returns a counter dict: ``{"scanned", "moved", "deleted", "skipped"}``.
    """
    apply_legacy_parity_schema(conn)
    stats = {"scanned": 0, "moved": 0, "deleted": 0, "skipped": 0}

    # Pull the candidate set in one query so we don't re-read on every loop.
    # We deliberately read all columns so we can re-emit them via
    # upsert_claude_parity_row without losing any field.
    cur = conn.execute(
        "SELECT * FROM hermes_agent_sessions WHERE transcript_path IS NOT NULL"
    )
    candidates: list[dict[str, Any]] = []
    cols = [d[0] for d in cur.description]
    for raw in cur.fetchall():
        d = dict(zip(cols, raw))
        if _row_is_native_claude(d.get("transcript_path")):
            candidates.append(d)
        else:
            stats["skipped"] += 1
    stats["scanned"] = len(candidates) + stats["skipped"]

    for row in candidates:
        # Filter to columns claude_session_parity actually has, renaming PK.
        payload: dict[str, Any] = {}
        for k, v in row.items():
            if k == "hermes_session_id":
                payload["session_uuid"] = v
            elif k in _CLAUDE_PARITY_COLS:
                payload[k] = v
        if not payload.get("session_uuid"):
            stats["skipped"] += 1
            continue
        upsert_claude_parity_row(conn, payload)
        conn.execute(
            "DELETE FROM hermes_agent_sessions WHERE hermes_session_id=?",
            (row["hermes_session_id"],),
        )
        stats["moved"] += 1
        stats["deleted"] += 1

    conn.commit()
    return stats


def main() -> int:
    """CLI entry — run the split against ``$WORKER_CONTROL_DB``.

    Usage: ``python -m worker_control_hermes.migrations._2026_split_claude_parity``
    """
    import os
    from pathlib import Path

    db = Path(os.environ.get(
        "WORKER_CONTROL_DB",
        "D:/work-github/.worker-control/worker-control.sqlite3",
    ))
    if not db.is_file():
        print(f"canonical DB not found: {db}")
        return 1
    conn = sqlite3.connect(str(db))
    stats = migrate(conn)
    print(f"split-claude-parity {db}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
