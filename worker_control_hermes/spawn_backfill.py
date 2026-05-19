"""spawn_backfill — close orphaned a-ok runs by scanning Hermes transcripts.

Heartbeat tick safety net for the ``hermes_runs`` ledger. Two failure
modes are covered:

1. **Stuck started**: a row exists with ``status='started'`` but the
   transcript shows the spawn already exited (or its parent transcript
   stopped moving 60+ minutes ago). Update to a terminal status so the
   dashboard "no-spawn" bucket doesn't keep counting it.
2. **Missing row**: the transcript signature shows a spawn happened
   (slug ``a-ok:<name>``, plus ``aok-spawn`` / ``claude -p`` / etc.) but
   ``hermes_runs`` has no matching row at all. Synthesize one tagged
   ``note='backfill: synthesized…'`` so the dashboard's spawn classifier
   recognizes the session as having spawned.

Scope:
- Only ``hermes_agent_sessions`` rows where ``profile_name IS NOT NULL``
  (i.e. Hermes profile sessions). Native claude jsonl rows are skipped.
- Only the ``hermes_runs`` table is mutated. ``hermes_sessions`` and
  ``hermes_agent_sessions`` are read-only here.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"a-ok:[a-z0-9._-]+")
RUN_END_RE = re.compile(r"workerctl-hermes-projects run end\s+(\d+)")
SPAWN_MARKER_RE = re.compile(
    r"\baok-spawn\b|\bclaude\s+(?:-p\b|--print\b|--resume\b)"
)
EXIT_NEAR_WINDOW = 32 * 1024  # 32 KB around each slug occurrence


def _parse_iso(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
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


def scan_transcript_for_spawns(path: str) -> list[dict]:
    """Scan a Hermes profile transcript for spawn signatures.

    Returns one entry per unique slug found, with closure/status
    inference based on neighboring ``exit=<n>`` markers. Returns ``[]``
    when the file can't be read or shows no spawn evidence at all.
    """
    p = Path(path)
    try:
        blob = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    has_spawn_evidence = bool(SPAWN_MARKER_RE.search(blob))
    # Slug presence alone is also evidence — sometimes the spawn was
    # done by a chain wrapper that doesn't include the literal
    # `aok-spawn` / `claude -p` token in this transcript.
    slug_iter = list(SLUG_RE.finditer(blob))
    if not slug_iter and not has_spawn_evidence:
        return []

    seen: dict[str, dict[str, Any]] = {}
    for m in slug_iter:
        slug = m.group(0)
        if slug in seen:
            continue
        offset = m.start()
        lo = max(0, offset - EXIT_NEAR_WINDOW)
        hi = min(len(blob), offset + EXIT_NEAR_WINDOW)
        window = blob[lo:hi]
        # Look for `exit=<n>` markers in the surrounding window. Use the
        # first one we find; in practice the trap appends a single
        # `exit=N` per run so collisions are rare.
        exit_match = re.search(r"\bexit=(-?\d+)\b", window)
        if exit_match:
            code = int(exit_match.group(1))
            closure_seen = True
            inferred_status = "done" if code == 0 else "failed"
        else:
            closure_seen = False
            inferred_status = "unknown"
        seen[slug] = {
            "slug": slug,
            "first_seen_byte_offset": offset,
            "closure_seen": closure_seen,
            "inferred_status": inferred_status,
        }
    return list(seen.values())


def _fetch_runs_for_session(
    conn: sqlite3.Connection, hermes_session_id: str
) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, session_id, run_index, name, status, started_at, "
        "       ended_at, note "
        "FROM hermes_runs "
        "WHERE hermes_session_id = ? AND name LIKE 'a-ok:%'",
        (hermes_session_id,),
    ))


def _resolve_session_id(
    conn: sqlite3.Connection, hermes_session_id: str
) -> int | None:
    """Best-effort lookup of the child hermes_sessions.id for an INSERT.

    The "spec" pairing is ``hermes_agent_sessions.hermes_session_id ==
    hermes_sessions.uuid``. In practice the join rarely lands (parent
    Hermes IDs are timestamp-shaped while ``hermes_sessions.uuid`` is
    UUID4), so callers must be prepared to skip when this returns
    ``None``.
    """
    row = conn.execute(
        "SELECT id FROM hermes_sessions WHERE uuid = ?",
        (hermes_session_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, sqlite3.Row) else row["id"]


def _next_run_index(conn: sqlite3.Connection, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(run_index), 0) FROM hermes_runs WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    cur = row[0] if row else 0
    return int(cur) + 1


def backfill_session(
    conn: sqlite3.Connection,
    hermes_session_uuid: str,
    transcript_path: str,
    *,
    now_iso: str,
    transcript_mtime: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Reconcile one Hermes session's ``hermes_runs`` rows.

    ``hermes_session_uuid`` is the parent Hermes session ID (the value
    stored in ``hermes_agent_sessions.hermes_session_id`` *and* in
    ``hermes_runs.hermes_session_id``). ``transcript_path`` is the
    Hermes JSON for that session.
    """
    stats: dict[str, Any] = {
        "updated": 0,
        "inserted": 0,
        "skipped": 0,
        "unmatched_slugs": [],
        "actions": [],  # per-action debug; small per-session
    }

    found = scan_transcript_for_spawns(transcript_path)
    if not found:
        return stats

    runs = _fetch_runs_for_session(conn, hermes_session_uuid)
    by_slug: dict[str, sqlite3.Row] = {}
    for r in runs:
        # If multiple runs share a slug (rare), prefer the most recent
        # (highest run_index).
        cur = by_slug.get(r["name"])
        if cur is None or r["run_index"] > cur["run_index"]:
            by_slug[r["name"]] = r

    mt = _parse_iso(transcript_mtime)
    now_dt = _parse_iso(now_iso) or _dt.datetime.now(_dt.timezone.utc)
    stale_cutoff = now_dt - _dt.timedelta(minutes=60)
    mtime_iso_for_close = transcript_mtime or now_iso

    for entry in found:
        slug = entry["slug"]
        inferred = entry["inferred_status"]
        closure_seen = entry["closure_seen"]

        # Slug-key match: prefer exact slug; the dispatcher appends a
        # `-r<N>` suffix when minting hermes_runs.name, so check both.
        run = by_slug.get(slug)
        if run is None:
            for k, v in by_slug.items():
                # `a-ok:foo` should match `a-ok:foo-r1`, `a-ok:foo-r2`, …
                if k.startswith(slug + "-r") or k == slug:
                    run = v
                    break

        if run is not None and run["status"] in ("done", "failed", "partial", "abandoned"):
            stats["skipped"] += 1
            continue

        if run is not None and run["status"] == "started":
            # Stale-or-closure update.
            transcript_stale = bool(mt and mt < stale_cutoff)
            if not (closure_seen or transcript_stale):
                stats["skipped"] += 1
                continue
            new_status = inferred if inferred in ("done", "failed") else "failed"
            new_note = "backfill: transcript scan"
            if closure_seen:
                new_note += f" closure_seen status={inferred}"
            else:
                new_note += " stale_mtime"
            existing_note = (run["note"] or "").strip()
            full_note = (
                f"{existing_note}\n{new_note}" if existing_note else new_note
            )
            stats["actions"].append({
                "kind": "update",
                "run_id": run["id"],
                "slug": slug,
                "new_status": new_status,
            })
            if not dry_run:
                conn.execute(
                    "UPDATE hermes_runs SET status=?, ended_at=COALESCE(ended_at, ?), "
                    "note=? WHERE id=?",
                    (new_status, mtime_iso_for_close, full_note, run["id"]),
                )
            stats["updated"] += 1
            continue

        # No existing run row — INSERT path.
        sid = _resolve_session_id(conn, hermes_session_uuid)
        if sid is None:
            stats["unmatched_slugs"].append(slug)
            stats["skipped"] += 1
            continue
        run_index = _next_run_index(conn, sid)
        new_status = (
            inferred if inferred in ("done", "failed") else "unknown"
        )
        started_at = transcript_mtime or now_iso
        ended_at = transcript_mtime or now_iso
        note = "backfill: synthesized from transcript signature"
        stats["actions"].append({
            "kind": "insert",
            "session_id": sid,
            "slug": slug,
            "run_index": run_index,
            "status": new_status,
        })
        if dry_run:
            stats["inserted"] += 1
            continue
        try:
            conn.execute(
                "INSERT INTO hermes_runs "
                "(session_id, run_index, name, mode, command, status, "
                " started_at, ended_at, note, hermes_session_id) "
                "VALUES (?, ?, ?, 'print', '', ?, ?, ?, ?, ?)",
                (
                    sid,
                    run_index,
                    slug,
                    new_status,
                    started_at,
                    ended_at,
                    note,
                    hermes_session_uuid,
                ),
            )
            stats["inserted"] += 1
        except sqlite3.IntegrityError:
            # Concurrent insert — another writer beat us. Safe to skip.
            stats["skipped"] += 1

    return stats


def backfill_all(
    conn: sqlite3.Connection,
    *,
    window_hours: int = 168,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """Run :func:`backfill_session` across recent Hermes-profile sessions.

    Returns aggregate counters plus a per-session breakdown (capped at
    50 entries) for debugging. Skips ``profile_name IS NULL`` rows so
    native claude jsonls are never scanned.
    """
    aggregate: dict[str, Any] = {
        "updated": 0,
        "inserted": 0,
        "skipped": 0,
        "sessions_scanned": 0,
        "sessions_with_changes": 0,
        "unmatched_slugs": [],
        "per_session": [],
    }

    now_dt = _dt.datetime.now(_dt.timezone.utc)
    cutoff_iso = (
        now_dt - _dt.timedelta(hours=window_hours)
    ).isoformat(timespec="seconds")
    now_iso = now_dt.isoformat(timespec="seconds")

    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT hermes_session_id, transcript_path, transcript_mtime "
        "FROM hermes_agent_sessions "
        "WHERE profile_name IS NOT NULL "
        "  AND transcript_path IS NOT NULL "
        "  AND COALESCE(transcript_mtime, '') >= ? "
        "ORDER BY transcript_mtime DESC",
        (cutoff_iso,),
    ))
    if limit is not None:
        rows = rows[:limit]

    for r in rows:
        hsid = r["hermes_session_id"]
        tpath = r["transcript_path"]
        tmtime = r["transcript_mtime"]
        if not (hsid and tpath):
            continue
        if not Path(tpath).is_file():
            continue
        stats = backfill_session(
            conn,
            hsid,
            tpath,
            now_iso=now_iso,
            transcript_mtime=tmtime,
            dry_run=dry_run,
        )
        aggregate["sessions_scanned"] += 1
        aggregate["updated"] += stats["updated"]
        aggregate["inserted"] += stats["inserted"]
        aggregate["skipped"] += stats["skipped"]
        aggregate["unmatched_slugs"].extend(stats["unmatched_slugs"])
        if stats["updated"] or stats["inserted"]:
            aggregate["sessions_with_changes"] += 1
            if len(aggregate["per_session"]) < 50:
                aggregate["per_session"].append({
                    "hermes_session_id": hsid,
                    "updated": stats["updated"],
                    "inserted": stats["inserted"],
                    "actions": stats["actions"],
                })

    if not dry_run:
        conn.commit()
    return aggregate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_db_path() -> Path:
    env = os.environ.get("WORKER_CONTROL_DB")
    if env:
        return Path(env)
    # Fall back to the canonical location used elsewhere in this package.
    return Path("D:/work-github/.worker-control/worker-control.sqlite3")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="workerctl-hermes-backfill",
        description="Close orphaned a-ok runs by scanning Hermes transcripts.",
    )
    p.add_argument("--window-hours", type=int, default=168,
                   help="Lookback window for transcript_mtime (default 168h).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of sessions scanned (debug).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report changes without writing to the DB.")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable output.")
    args = p.parse_args(argv)

    db = _resolve_db_path()
    if not db.is_file():
        print(f"[backfill] DB not found: {db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db)
    try:
        stats = backfill_all(
            conn,
            window_hours=args.window_hours,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        mode = "dry-run" if args.dry_run else "applied"
        print(
            f"[backfill {mode}] scanned={stats['sessions_scanned']} "
            f"changed_sessions={stats['sessions_with_changes']} "
            f"updated={stats['updated']} inserted={stats['inserted']} "
            f"skipped={stats['skipped']} "
            f"unmatched_slug_count={len(stats['unmatched_slugs'])}"
        )
        for ps in stats["per_session"][:10]:
            print(
                f"  {ps['hermes_session_id']}: "
                f"updated={ps['updated']} inserted={ps['inserted']}"
            )
            for a in ps["actions"][:5]:
                print(f"    {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
