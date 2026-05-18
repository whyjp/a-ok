"""
One-shot migration: fold hermes worker-profile's projects.db
into the canonical worker-control SQLite DB at D:/work-github/.worker-control/.

Rules:
  * Canonical `projects` table (worker_control schema) is the single source of
    truth for project rows. hermes' `projects.folder_path` → canonical `projects.path`.
    hermes-specific fields (project_type, git_repo, display_name, description,
    learned_notes, use_count, last_used_at) are stashed in
    canonical.projects.metadata['hermes'].
  * The hermes session ledger lives in three NEW tables in the canonical DB,
    prefixed `hermes_` to avoid colliding with worker_control's `worker_sessions`:
        hermes_sessions  (was: sessions)
        hermes_runs      (was: runs)
        hermes_subprocs  (was: subprocs)
    Foreign key `project_id` is REMAPPED from the old projects.id to the new
    canonical projects.id by path equality.
  * Old projects.db is preserved (a timestamped .bak.* already exists), and
    the original file is left in place so a rollback is one cp away. After
    migration, hermes scripts will be repointed to read the canonical DB.

This script is idempotent: re-running it will skip already-migrated rows
(matched by uuid for sessions, by (session_uuid, pid, started_at) for
subprocs, by composite (session_id, run_index) for runs).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

CANONICAL = Path(os.environ.get(
    "WORKER_CONTROL_DB",
    "D:/work-github/.worker-control/worker-control.sqlite3",
))
OLD = Path(os.environ.get(
    "WORKER_PROJECTS_DB",
    Path.home() / "AppData/Local/hermes/profiles/worker/projects.db",
))

# ---- canonical-side schema extension --------------------------------------
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    origin          TEXT NOT NULL DEFAULT 'native',
    model           TEXT,
    permission_mode TEXT,
    brief           TEXT,
    notes           TEXT DEFAULT '',
    claude_name     TEXT,
    claude_status   TEXT,
    claude_status_at TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL,
    ended_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_project   ON hermes_sessions(project_id);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_status    ON hermes_sessions(status);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_origin    ON hermes_sessions(origin);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_last_used ON hermes_sessions(last_used_at DESC);

CREATE TABLE IF NOT EXISTS hermes_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES hermes_sessions(id) ON DELETE CASCADE,
    run_index    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    command      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'started',
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    note         TEXT,
    UNIQUE(session_id, run_index)
);
CREATE INDEX IF NOT EXISTS ix_hermes_runs_session ON hermes_runs(session_id);
CREATE INDEX IF NOT EXISTS ix_hermes_runs_started ON hermes_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS hermes_subprocs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid  TEXT NOT NULL,
    pid           INTEGER NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'workload',
    name          TEXT NOT NULL,
    cmdline       TEXT,
    cwd           TEXT,
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL DEFAULT 'alive',
    task_id       TEXT,
    UNIQUE(session_uuid, pid, started_at)
);
CREATE INDEX IF NOT EXISTS ix_hermes_subprocs_uuid   ON hermes_subprocs(session_uuid);
CREATE INDEX IF NOT EXISTS ix_hermes_subprocs_status ON hermes_subprocs(status);
CREATE INDEX IF NOT EXISTS ix_hermes_subprocs_last   ON hermes_subprocs(last_seen_at DESC);
"""


def _path_key(p: str) -> str:
    """Normalize a path for cross-DB equality (case-insensitive, forward slashes)."""
    return str(Path(p)).replace("\\", "/").lower()


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_root_role(path: str) -> str:
    p = _path_key(path)
    if p.startswith("d:/work-github"):
        return "owned_work"
    if p.startswith("d:/github"):
        return "public_reference"
    return "other"


def main() -> int:
    if not OLD.is_file():
        print(f"old projects.db not found: {OLD}", file=sys.stderr)
        return 1
    if not CANONICAL.is_file():
        print(f"canonical DB not found (run `workerctl init` first): {CANONICAL}",
              file=sys.stderr)
        return 1

    canon = sqlite3.connect(str(CANONICAL))
    canon.row_factory = sqlite3.Row
    canon.execute("PRAGMA foreign_keys = ON")
    canon.executescript(EXTRA_SCHEMA)

    # Legacy-parity schema (idempotent) — adds the columns/tables the
    # dashboard needs to render claude/hermes sessions with the same
    # depth as the legacy sites/1143 report.
    from worker_control_hermes.legacy_parity_schema import apply_legacy_parity_schema
    parity_audit = apply_legacy_parity_schema(canon)
    if parity_audit["columns_added"] or parity_audit["tables_added"]:
        print(f"legacy-parity: +cols={parity_audit['columns_added']} "
              f"+tables={parity_audit['tables_added']}")

    # Data migration — split native-claude parity rows out of the
    # Hermes-only table. Idempotent: re-running is a no-op once the
    # native rows have been moved.
    from worker_control_hermes.migrations._2026_split_claude_parity import (
        migrate as _split_claude_parity,
    )
    split_stats = _split_claude_parity(canon)
    if split_stats["moved"]:
        print(f"split-claude-parity: moved={split_stats['moved']} "
              f"deleted={split_stats['deleted']} "
              f"skipped={split_stats['skipped']}")

    old = sqlite3.connect(str(OLD))
    old.row_factory = sqlite3.Row

    # ---- 1. projects: hermes → canonical (upsert by path) ----------------
    old_projects = old.execute("SELECT * FROM projects").fetchall()
    print(f"hermes projects: {len(old_projects)}")

    canon_existing = {
        _path_key(r["path"]): r["id"]
        for r in canon.execute("SELECT id, path FROM projects").fetchall()
    }

    proj_id_map: dict[int, int] = {}   # old_id → canonical_id
    inserted = 0
    refreshed = 0
    for r in old_projects:
        path = r["folder_path"]
        norm_path = str(Path(path))     # native separators
        key = _path_key(path)
        hermes_meta = {
            "project_type":  r["project_type"],
            "git_repo":      r["git_repo"],
            "display_name":  r["display_name"],
            "description":   r["description"],
            "learned_notes": r["learned_notes"],
            "created_at":    r["created_at"],
            "last_used_at":  r["last_used_at"],
            "use_count":     r["use_count"],
        }
        if key in canon_existing:
            new_id = canon_existing[key]
            # Merge hermes metadata into existing canonical row (don't clobber).
            row = canon.execute(
                "SELECT metadata FROM projects WHERE id=?", (new_id,)
            ).fetchone()
            try:
                meta = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            meta["hermes"] = hermes_meta
            canon.execute(
                "UPDATE projects SET metadata=?, updated_at=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), _now_iso(), new_id),
            )
            refreshed += 1
        else:
            name = (r["display_name"] or Path(path).name).strip()
            # uniqueness fallback
            base_name = name
            n = 2
            while canon.execute(
                "SELECT 1 FROM projects WHERE name=?", (name,),
            ).fetchone():
                name = f"{base_name}-{n}"
                n += 1
            meta = {"hermes": hermes_meta}
            now = _now_iso()
            cur = canon.execute(
                "INSERT INTO projects(name, path, is_git, branch, remote_url, "
                "is_dirty, root_role, last_scan_at, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, 0, ?, NULL, ?, ?, ?)",
                (
                    name,
                    norm_path,
                    1 if r["git_repo"] else 0,
                    r["git_repo"],
                    _classify_root_role(path),
                    json.dumps(meta, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            new_id = cur.lastrowid
            canon_existing[key] = new_id
            inserted += 1
        proj_id_map[r["id"]] = new_id

    print(f"  projects → canonical: inserted={inserted} refreshed={refreshed}")

    # ---- 2. sessions → hermes_sessions ------------------------------------
    old_sessions = old.execute("SELECT * FROM sessions").fetchall()
    print(f"hermes sessions: {len(old_sessions)}")

    existing_session_uuids = {
        r["uuid"]: r["id"]
        for r in canon.execute("SELECT id, uuid FROM hermes_sessions").fetchall()
    }
    sess_id_map: dict[int, int] = {}   # old session_id → new hermes_sessions.id
    s_ins = 0; s_skip = 0
    for r in old_sessions:
        if r["uuid"] in existing_session_uuids:
            sess_id_map[r["id"]] = existing_session_uuids[r["uuid"]]
            s_skip += 1
            continue
        new_proj_id = proj_id_map.get(r["project_id"])
        if not new_proj_id:
            print(f"  [warn] session {r['uuid']} has no project_id mapping; skipped")
            continue
        cur = canon.execute(
            "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, "
            "model, permission_mode, brief, notes, claude_name, claude_status, "
            "claude_status_at, created_at, last_used_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["uuid"], new_proj_id, r["name"], r["status"], r["origin"],
                r["model"], r["permission_mode"], r["brief"], r["notes"],
                r["claude_name"], r["claude_status"], r["claude_status_at"],
                r["created_at"], r["last_used_at"], r["ended_at"],
            ),
        )
        sess_id_map[r["id"]] = cur.lastrowid
        s_ins += 1
    print(f"  hermes_sessions: inserted={s_ins} skipped={s_skip}")

    # ---- 3. runs → hermes_runs --------------------------------------------
    old_runs = old.execute("SELECT * FROM runs").fetchall()
    print(f"hermes runs: {len(old_runs)}")
    r_ins = 0; r_skip = 0
    for r in old_runs:
        new_sess_id = sess_id_map.get(r["session_id"])
        if not new_sess_id:
            print(f"  [warn] run #{r['id']} has no session mapping; skipped")
            r_skip += 1
            continue
        exists = canon.execute(
            "SELECT 1 FROM hermes_runs WHERE session_id=? AND run_index=?",
            (new_sess_id, r["run_index"]),
        ).fetchone()
        if exists:
            r_skip += 1
            continue
        canon.execute(
            "INSERT INTO hermes_runs(session_id, run_index, name, mode, command, "
            "status, started_at, ended_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_sess_id, r["run_index"], r["name"], r["mode"], r["command"],
                r["status"], r["started_at"], r["ended_at"], r["note"],
            ),
        )
        r_ins += 1
    print(f"  hermes_runs: inserted={r_ins} skipped={r_skip}")

    # ---- 4. subprocs → hermes_subprocs ------------------------------------
    old_subprocs = old.execute("SELECT * FROM subprocs").fetchall()
    print(f"hermes subprocs: {len(old_subprocs)}")
    sp_ins = 0; sp_skip = 0
    for r in old_subprocs:
        exists = canon.execute(
            "SELECT 1 FROM hermes_subprocs WHERE session_uuid=? AND pid=? AND started_at=?",
            (r["session_uuid"], r["pid"], r["started_at"]),
        ).fetchone()
        if exists:
            sp_skip += 1
            continue
        canon.execute(
            "INSERT INTO hermes_subprocs(session_uuid, pid, kind, name, cmdline, "
            "cwd, started_at, last_seen_at, ended_at, status, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["session_uuid"], r["pid"], r["kind"], r["name"], r["cmdline"],
                r["cwd"], r["started_at"], r["last_seen_at"], r["ended_at"],
                r["status"], r["task_id"],
            ),
        )
        sp_ins += 1
    print(f"  hermes_subprocs: inserted={sp_ins} skipped={sp_skip}")

    canon.commit()

    # Final counts on canonical side
    print("\ncanonical DB counts after migration:")
    for t in ("projects", "worker_profiles", "worker_sessions",
              "hermes_sessions", "hermes_runs", "hermes_subprocs"):
        n = canon.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
