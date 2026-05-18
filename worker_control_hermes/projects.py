#!/usr/bin/env python
"""
worker projects registry — small SQLite-backed catalog of project folders
the Hermes `worker` profile is allowed to operate on.

DB lives next to this script (or at $WORKER_PROJECTS_DB), and is kept
deliberately schema-light so it can grow with the agent.

Columns:
    id              INTEGER PK
    folder_path     TEXT UNIQUE   absolute, normalized
    project_type    TEXT          free-form category (e.g. 'gitlab-nexon', 'github-official', 'local')
    git_repo        TEXT          owner/repo or remote URL (auto-detected if empty)
    display_name    TEXT          user-given short name (optional, indexed)
    description     TEXT          one-line user description
    learned_notes   TEXT          accumulated notes (timestamped, append-only)
    created_at      TEXT (ISO8601 UTC)
    last_used_at    TEXT
    use_count       INTEGER DEFAULT 0

Subcommands (run `python projects.py -h` for the full list):
    add       register a folder
    list      list projects (sort: --recent default, --frequent, --name)
    show      show one project (key = id, path, or display_name)
    update    edit fields, or append a learned note
    touch     bump last_used_at + use_count (call before/after each run)
    remove    drop a project from the registry
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import uuid as _uuid
from pathlib import Path

DB_PATH = Path(os.environ.get("WORKER_PROJECTS_DB", r"D:/work-github/.worker-control/worker-control.sqlite3"))

# hermes-agent stamps this on every tool-call child process; we keep it on
# every hermes_runs insert so the dashboard can show "this claude session
# was spawned from hermes turn X" and offer a click-through to that turn.
# Empty (None) when invoked from a regular shell.
_HERMES_SESSION_ID = os.environ.get("HERMES_SESSION_ID") or None

SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_projects_v (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path   TEXT NOT NULL UNIQUE,
    project_type  TEXT,
    git_repo      TEXT,
    display_name  TEXT,
    description   TEXT,
    learned_notes TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_projects_last_used ON projects(last_used_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_use_count ON projects(use_count DESC);
CREATE INDEX IF NOT EXISTS idx_projects_name       ON projects(display_name);

CREATE TABLE IF NOT EXISTS hermes_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid          TEXT NOT NULL UNIQUE,
    project_id    INTEGER NOT NULL REFERENCES hermes_projects_v(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | done | failed | abandoned
    origin        TEXT NOT NULL DEFAULT 'native',   -- spawned | native (see _reclassify_origins)
    model         TEXT,
    permission_mode TEXT,
    brief         TEXT,                              -- short task description
    notes         TEXT DEFAULT '',                   -- append-only outcome log
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project    ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status     ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_used  ON sessions(last_used_at DESC);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Apply lightweight in-place migrations for older DBs."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "project_type" not in cols:
        conn.execute("ALTER TABLE hermes_projects_v ADD COLUMN project_type TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_type ON projects(project_type)")

    # sessions.origin (was added after the initial schema shipped)
    sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if sess_cols and "origin" not in sess_cols:
        conn.execute("ALTER TABLE hermes_sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'native'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_origin ON sessions(origin)")

    # sessions.claude_name — the user-facing label claude-code keeps for the
    # session (set via /name, surfaced in ~/.claude/sessions/<pid>.json.name).
    # KEEP THIS STRICTLY SEPARATE FROM hermes_sessions.name. `sessions.name` is the
    # hermes-assigned slug that drives spawned-vs-native classification (the
    # `scv-` prefix is reserved); a user `/rename` must never overwrite it.
    # `claude_name` is purely display metadata, refreshed from the claude-side
    # registry on every heartbeat tick.
    if sess_cols and "claude_name" not in sess_cols:
        conn.execute("ALTER TABLE hermes_sessions ADD COLUMN claude_name TEXT")
    # claude_status / claude_status_at — last observed busy|idle plus the wall
    # time of the observation, also from the claude-side registry. Useful as a
    # tie-breaker when the heartbeat needs to know "is the human's claude
    # window still attached to this session" without having to re-scan psutil.
    if sess_cols and "claude_status" not in sess_cols:
        conn.execute("ALTER TABLE hermes_sessions ADD COLUMN claude_status TEXT")
    if sess_cols and "claude_status_at" not in sess_cols:
        conn.execute("ALTER TABLE hermes_sessions ADD COLUMN claude_status_at TEXT")

    # runs table — every claude-code invocation against a session is logged here
    # so we can rebuild the timeline of a long-lived session (each run gets its
    # own --name suffix; the parent session UUID stays stable so --resume works).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hermes_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL REFERENCES hermes_sessions(id) ON DELETE CASCADE,
            run_index     INTEGER NOT NULL,        -- 1-based, per session
            name          TEXT NOT NULL,           -- the --name actually passed to claude
            mode          TEXT NOT NULL,           -- 'print' | 'interactive'
            command       TEXT NOT NULL,           -- the full claude command we emitted
            status        TEXT NOT NULL DEFAULT 'started',  -- started | done | failed
            started_at    TEXT NOT NULL,
            ended_at      TEXT,
            note          TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_session   ON runs(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at DESC)")


def _reclassify_origins(conn: sqlite3.Connection) -> tuple[int, int]:
    """Recompute every session's origin from authoritative signals.

    Rule (locked-in after the 145/0 classification bug):
      - origin='spawned' IFF there exists at least one row in `runs` with
        mode='print' for this session_id. That's the only signal that
        proves the worker autonomously dispatched a non-interactive
        invocation. UUID allocation alone (a row in `sessions`) does NOT
        qualify — users routinely take an allocated UUID and drive it
        interactively, which is native usage with hermes metadata.
      - origin='native' otherwise. Worker-side metadata (brief, model,
        permission_mode, name) stays on the row as useful annotation.

    Returns (spawned_count, native_count) after the update.
    """
    conn.execute("""
        UPDATE hermes_sessions
           SET origin = CASE
               WHEN EXISTS (
                   SELECT 1 FROM hermes_runs r
                   WHERE r.session_id = hermes_sessions.id AND r.mode = 'print'
               ) THEN 'spawned'
               ELSE 'native'
           END
    """)
    sp = conn.execute("SELECT COUNT(*) FROM hermes_sessions WHERE origin='spawned'").fetchone()[0]
    nv = conn.execute("SELECT COUNT(*) FROM hermes_sessions WHERE origin='native'").fetchone()[0]
    return sp, nv


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _norm_path(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def _detect_git_repo(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        url = out.stdout.strip()
        return url or None
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # When pointed at the canonical worker-control DB, all hermes_* tables and
    # the hermes_projects_v view are already created/maintained by the
    # workerctl + migrate_to_canonical_db.py pipeline; running the legacy
    # SCHEMA/`_ensure_columns()` against a VIEW would error
    # ("ALTER TABLE hermes_projects_v ..." / "CREATE INDEX ON projects(...)").
    # Detect canonical by the presence of the `worker_profiles` table, which
    # only worker_control owns.
    is_canonical = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_profiles'"
    ).fetchone() is not None
    if not is_canonical:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
    return conn


def _resolve_key(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    # numeric id?
    if key.isdigit():
        row = conn.execute("SELECT * FROM hermes_projects_v WHERE id=?", (int(key),)).fetchone()
        if row:
            return row
    # exact path match (normalized)
    norm = _norm_path(key)
    row = conn.execute("SELECT * FROM hermes_projects_v WHERE folder_path=?", (norm,)).fetchone()
    if row:
        return row
    # display_name exact, then LIKE
    row = conn.execute("SELECT * FROM hermes_projects_v WHERE display_name=?", (key,)).fetchone()
    if row:
        return row
    rows = conn.execute(
        "SELECT * FROM hermes_projects_v WHERE display_name LIKE ? OR folder_path LIKE ?",
        (f"%{key}%", f"%{key}%"),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        names = ", ".join(f"#{r['id']} {r['display_name'] or r['folder_path']}" for r in rows)
        raise SystemExit(f"Ambiguous key '{key}': matches {names}")
    return None


def cmd_add(args: argparse.Namespace) -> None:
    path = _norm_path(args.path)
    if not Path(path).is_dir():
        raise SystemExit(f"Not a directory: {path}")
    repo = args.repo or _detect_git_repo(path)
    now = _now()
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO hermes_projects_v(folder_path, project_type, git_repo, display_name, "
                "description, learned_notes, created_at, last_used_at, use_count) "
                "VALUES (?, ?, ?, ?, ?, '', ?, ?, 0)",
                (path, args.type, repo, args.name, args.desc or "", now, now),
            )
        except sqlite3.IntegrityError:
            raise SystemExit(f"Already registered: {path} (use 'update')")
    print(f"Added: {path}"
          + (f" [{args.type}]" if args.type else "")
          + (f" [{repo}]" if repo else ""))


def cmd_list(args: argparse.Namespace) -> None:
    order = {
        "recent":   "last_used_at DESC, id DESC",
        "frequent": "use_count DESC, last_used_at DESC",
        "name":     "COALESCE(display_name, folder_path) ASC",
        "created":  "created_at DESC",
    }[args.sort]
    where, params = "", []
    if args.type:
        where = "WHERE project_type = ?"
        params.append(args.type)
    params.append(args.limit)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM hermes_projects_v {where} ORDER BY {order} LIMIT ?",
            params,
        ).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(no projects registered)")
        return
    print(f"{'ID':>3}  {'NAME':<24}  {'TYPE':<16}  {'USES':>4}  {'LAST USED':<20}  PATH")
    for r in rows:
        name = (r["display_name"] or Path(r["folder_path"]).name)[:24]
        tp   = (r["project_type"] or "")[:16]
        last = (r["last_used_at"] or "")[:19].replace("T", " ")
        print(f"{r['id']:>3}  {name:<24}  {tp:<16}  {r['use_count']:>4}  {last:<20}  {r['folder_path']}")


def cmd_show(args: argparse.Namespace) -> None:
    with _connect() as conn:
        row = _resolve_key(conn, args.key)
    if not row:
        raise SystemExit(f"Not found: {args.key}")
    d = dict(row)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return
    for k in ("id", "display_name", "folder_path", "git_repo",
              "description", "use_count", "created_at", "last_used_at"):
        print(f"{k:>14}: {d.get(k) or ''}")
    notes = d.get("learned_notes") or ""
    if notes.strip():
        print("\n--- learned notes ---")
        print(notes.rstrip())


def cmd_update(args: argparse.Namespace) -> None:
    with _connect() as conn:
        row = _resolve_key(conn, args.key)
        if not row:
            raise SystemExit(f"Not found: {args.key}")
        sets, vals = [], []
        if args.name is not None:
            sets.append("display_name=?");  vals.append(args.name)
        if args.type is not None:
            sets.append("project_type=?");  vals.append(args.type)
        if args.repo is not None:
            sets.append("git_repo=?");      vals.append(args.repo)
        if args.desc is not None:
            sets.append("description=?");   vals.append(args.desc)
        if args.append_note:
            stamp = _now()
            existing = row["learned_notes"] or ""
            new = (existing + ("\n" if existing else "")
                   + f"[{stamp}] {args.append_note}")
            sets.append("learned_notes=?"); vals.append(new)
        if not sets:
            raise SystemExit("Nothing to update. Pass --name/--repo/--desc/--append-note.")
        vals.append(row["id"])
        conn.execute(f"UPDATE hermes_projects_v SET {', '.join(sets)} WHERE id=?", vals)
    print(f"Updated #{row['id']}")


def cmd_touch(args: argparse.Namespace) -> None:
    with _connect() as conn:
        row = _resolve_key(conn, args.key)
        if not row:
            raise SystemExit(f"Not found: {args.key}")
        conn.execute(
            "UPDATE hermes_projects_v SET last_used_at=?, use_count=use_count+1 WHERE id=?",
            (_now(), row["id"]),
        )
    print(f"Touched #{row['id']} ({row['folder_path']})")


def cmd_remove(args: argparse.Namespace) -> None:
    with _connect() as conn:
        row = _resolve_key(conn, args.key)
        if not row:
            raise SystemExit(f"Not found: {args.key}")
        conn.execute("DELETE FROM hermes_projects_v WHERE id=?", (row["id"],))
    print(f"Removed #{row['id']} ({row['folder_path']})")


# ---------------------------------------------------------------------------
# sessions — track Claude Code session UUIDs attached to a project
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                      re.IGNORECASE)


def _slugify(text: str, fallback: str = "task") -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return (s or fallback)[:48]


def _resolve_session(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    if key.isdigit():
        row = conn.execute("SELECT * FROM hermes_sessions WHERE id=?", (int(key),)).fetchone()
        if row:
            return row
    if _UUID_RE.match(key):
        return conn.execute("SELECT * FROM hermes_sessions WHERE uuid=?", (key.lower(),)).fetchone()
    rows = conn.execute(
        "SELECT * FROM hermes_sessions WHERE name=? OR uuid LIKE ? OR name LIKE ? "
        "ORDER BY last_used_at DESC",
        (key, f"{key}%", f"%{key}%"),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        names = ", ".join(f"#{r['id']} {r['name']} ({r['uuid'][:8]})" for r in rows)
        raise SystemExit(f"Ambiguous session key '{key}': {names}")
    return None


def cmd_session_start(args: argparse.Namespace) -> None:
    """Allocate a new session UUID for a project and print the claude command.

    This also records run #1 in the runs table so the timeline is unified.
    Subsequent invocations should use `run start <session>` (which uses
    --resume instead of --session-id).
    """
    with _connect() as conn:
        proj = _resolve_key(conn, args.project)
        if not proj:
            raise SystemExit(f"Project not found: {args.project}")
        uid = (args.uuid or str(_uuid.uuid4())).lower()
        if not _UUID_RE.match(uid):
            raise SystemExit(f"Invalid UUID: {uid}")
        base_name = args.name or _slugify(args.brief or "", "session")
        if not base_name:
            raise SystemExit("Session name is empty after slugify; pass --name")
        now = _now()
        try:
            conn.execute(
                "INSERT INTO hermes_sessions(uuid, project_id, name, status, model, "
                "permission_mode, brief, notes, created_at, last_used_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?, '', ?, ?)",
                (uid, proj["id"], base_name, args.model, args.permission_mode,
                 args.brief or "", now, now),
            )
        except sqlite3.IntegrityError:
            raise SystemExit(f"Session UUID collision: {uid}")
        sess_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "UPDATE hermes_projects_v SET last_used_at=?, use_count=use_count+1 WHERE id=?",
            (now, proj["id"]),
        )

        # Record the inaugural run.
        run_name = _run_name(base_name, 1)
        mode = "print" if args.print else "interactive"
        cmd = _build_claude_command(
            cwd=proj["folder_path"],
            uid=uid,
            name=run_name,
            model=args.model,
            permission_mode=args.permission_mode,
            print_mode=args.print,
            prompt=args.prompt,
            max_turns=args.max_turns,
            allowed_tools=args.allowed_tools,
            resume=False,
        )
        conn.execute(
            "INSERT INTO hermes_runs(session_id, run_index, name, mode, command, status, started_at, hermes_session_id) "
            "VALUES (?, ?, ?, ?, ?, 'started', ?, ?)",
            (sess_id, 1, run_name, mode, cmd, now, _HERMES_SESSION_ID),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # The brand-new session is now eligible for `spawned` classification
        # if this first run is print-mode. Reclassify once.
        _reclassify_origins(conn)

    payload = {
        "uuid": uid,
        "name": base_name,
        "project": proj["folder_path"],
        "run_id": run_id,
        "run_index": 1,
        "run_name": run_name,
        "mode": mode,
        "command": cmd,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"session uuid : {uid}")
    print(f"session name : {base_name}")
    print(f"project      : {proj['folder_path']}")
    print(f"run #{run_id} (idx 1, mode={mode})")
    print(f"  --name     : {run_name}")
    print(f"  command    : {cmd}")


def _build_claude_command(*, cwd: str, uid: str, name: str,
                          model: str | None, permission_mode: str | None,
                          print_mode: bool, prompt: str | None,
                          max_turns: int | None,
                          allowed_tools: str | None,
                          resume: bool = False) -> str:
    parts = ["claude"]
    if print_mode:
        parts.append("-p")
        if prompt:
            parts.append(shlex.quote(prompt))
        if max_turns:
            parts.extend(["--max-turns", str(max_turns)])
    if resume:
        # Second-and-later runs against an existing session must use --resume.
        # Passing --session-id on an already-existing UUID is rejected by
        # claude-code ("session already exists"). --name is still set so the
        # /resume picker shows the new rename.
        parts.extend(["--resume", uid, "--name", shlex.quote(name)])
    else:
        parts.extend(["--session-id", uid, "--name", shlex.quote(name)])
    if model:
        parts.extend(["--model", model])
    if permission_mode:
        parts.extend(["--permission-mode", permission_mode])
    if allowed_tools:
        parts.extend(["--allowedTools", shlex.quote(allowed_tools)])
    return f"cd {shlex.quote(cwd)} && {' '.join(parts)}"


def cmd_session_resume_cmd(args: argparse.Namespace) -> None:
    """Print the exact command to resume a session interactively (head-ful)."""
    with _connect() as conn:
        sess = _resolve_session(conn, args.key)
        if not sess:
            raise SystemExit(f"Session not found: {args.key}")
        proj = conn.execute("SELECT * FROM hermes_projects_v WHERE id=?",
                            (sess["project_id"],)).fetchone()
    # User-facing resume = interactive (no -p), with --resume so Claude rejoins history.
    parts = ["claude", "--resume", sess["uuid"], "--name", shlex.quote(sess["name"])]
    if sess["model"]:
        parts.extend(["--model", sess["model"]])
    if sess["permission_mode"]:
        parts.extend(["--permission-mode", sess["permission_mode"]])
    cmd = f"cd {shlex.quote(proj['folder_path'])} && {' '.join(parts)}"
    if args.json:
        print(json.dumps({
            "uuid": sess["uuid"],
            "name": sess["name"],
            "project": proj["folder_path"],
            "command": cmd,
        }, ensure_ascii=False, indent=2))
        return
    print(cmd)


def cmd_session_list(args: argparse.Namespace) -> None:
    order = {
        "recent":  "s.last_used_at DESC, s.id DESC",
        "created": "s.created_at DESC",
        "name":    "s.name ASC",
    }[args.sort]
    where, params = [], []
    if args.project:
        with _connect() as conn:
            proj = _resolve_key(conn, args.project)
            if not proj:
                raise SystemExit(f"Project not found: {args.project}")
            where.append("s.project_id=?"); params.append(proj["id"])
    if args.status:
        where.append("s.status=?"); params.append(args.status)
    if args.origin:
        where.append("s.origin=?"); params.append(args.origin)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(args.limit)
    sql = (f"SELECT s.*, p.display_name AS proj_name, p.folder_path AS proj_path "
           f"FROM hermes_sessions s JOIN hermes_projects_v p ON p.id = s.project_id "
           f"{where_sql} ORDER BY {order} LIMIT ?")
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(no sessions)")
        return
    print(f"{'ID':>3}  {'ORG':<6}  {'STATUS':<9}  {'NAME':<26}  {'PROJECT':<22}  {'UUID':<10}  LAST USED")
    for r in rows:
        last = (r["last_used_at"] or "")[:19].replace("T", " ")
        origin = (r["origin"] if "origin" in r.keys() else "hermes") or "hermes"
        print(f"{r['id']:>3}  {origin:<6}  {r['status']:<9}  {(r['name'] or '')[:26]:<26}  "
              f"{(r['proj_name'] or Path(r['proj_path']).name)[:22]:<22}  "
              f"{r['uuid'][:8]:<10}  {last}")


def cmd_session_show(args: argparse.Namespace) -> None:
    with _connect() as conn:
        sess = _resolve_session(conn, args.key)
        if not sess:
            raise SystemExit(f"Session not found: {args.key}")
        proj = conn.execute("SELECT * FROM hermes_projects_v WHERE id=?",
                            (sess["project_id"],)).fetchone()
    d = dict(sess)
    d["project_path"] = proj["folder_path"]
    d["project_name"] = proj["display_name"]
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return
    for k in ("id", "uuid", "name", "status", "model", "permission_mode",
              "project_name", "project_path", "brief",
              "created_at", "last_used_at", "ended_at"):
        v = d.get(k)
        if v:
            print(f"{k:>14}: {v}")
    notes = d.get("notes") or ""
    if notes.strip():
        print("\n--- notes ---")
        print(notes.rstrip())


def cmd_session_end(args: argparse.Namespace) -> None:
    with _connect() as conn:
        sess = _resolve_session(conn, args.key)
        if not sess:
            raise SystemExit(f"Session not found: {args.key}")
        now = _now()
        sets = ["status=?", "ended_at=?", "last_used_at=?"]
        vals = [args.status, now, now]
        if args.append_note:
            existing = sess["notes"] or ""
            new = (existing + ("\n" if existing else "")
                   + f"[{now}] {args.append_note}")
            sets.append("notes=?"); vals.append(new)
        vals.append(sess["id"])
        conn.execute(f"UPDATE hermes_sessions SET {', '.join(sets)} WHERE id=?", vals)
    print(f"Session #{sess['id']} ({sess['uuid']}) → {args.status}")


def cmd_session_reclassify(args: argparse.Namespace) -> None:
    """User-facing wrapper around `_reclassify_origins`."""
    with _connect() as conn:
        sp, nv = _reclassify_origins(conn)
    print(f"reclassify: {sp} spawned, {nv} native")


def cmd_session_note(args: argparse.Namespace) -> None:
    with _connect() as conn:
        sess = _resolve_session(conn, args.key)
        if not sess:
            raise SystemExit(f"Session not found: {args.key}")
        now = _now()
        existing = sess["notes"] or ""
        new = existing + ("\n" if existing else "") + f"[{now}] {args.text}"
        conn.execute(
            "UPDATE hermes_sessions SET notes=?, last_used_at=? WHERE id=?",
            (new, now, sess["id"]),
        )
    print(f"Note appended to session #{sess['id']}")


# ---------------------------------------------------------------------------
# Native claude-code session import — scan ~/.claude/projects and ingest any
# session UUID that we don't already track. `origin='native'` marks them so
# the registry visibly distinguishes them from hermes-spawned sessions.
# ---------------------------------------------------------------------------

_CLAUDE_PROJECTS_DIR = Path(os.environ.get(
    "CLAUDE_PROJECTS_DIR",
    Path.home() / ".claude" / "projects",
))


def _decode_claude_project_dir(name: str) -> str | None:
    """Convert claude-code's project folder name back to a filesystem path.

    Claude encodes cwd by replacing path separators with '-' and the drive
    colon with nothing — e.g. ``D--work-gitlab-live-memory-console`` →
    ``D:\\work-gitlab\\live-memory-console``. We try common reverse rules and
    pick whichever directory exists.
    """
    if not name or not name[0].isalpha() or not name.startswith(name[0]):
        return None
    # Pattern: "<DRIVE>--<rest-with-dashes>"
    m = re.match(r"^([A-Za-z])--(.+)$", name)
    if not m:
        return None
    drive, rest = m.group(1), m.group(2)
    candidate = f"{drive}:\\" + rest.replace("-", "\\")
    if Path(candidate).is_dir():
        return candidate
    # Some segments may legitimately contain '-' (e.g. live-memory-console).
    # Try iteratively merging trailing segments back with hyphens.
    parts = rest.split("-")
    for n_merge in range(1, len(parts)):
        head = parts[:-n_merge]
        tail = "-".join(parts[-n_merge:])
        candidate2 = f"{drive}:\\" + "\\".join(head + [tail]) if head else f"{drive}:\\{tail}"
        if Path(candidate2).is_dir():
            return candidate2
    return None


def _scan_native_session(jsonl_path: Path) -> dict:
    """Read a claude-code session .jsonl and extract our summary fields."""
    info = {
        "uuid": jsonl_path.stem,
        "cwd": None,
        "first_user_at": None,
        "last_event_at": None,
        "custom_title": None,
        "first_user_text": "",
        "user_count": 0,
        "assistant_count": 0,
        "subagent_count": 0,
        "model": None,
        "version": None,
    }
    if not jsonl_path.is_file():
        return info
    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                t = d.get("type")
                if d.get("timestamp"):
                    info["last_event_at"] = d["timestamp"]
                if d.get("cwd") and not info["cwd"]:
                    info["cwd"] = d["cwd"]
                if d.get("version") and not info["version"]:
                    info["version"] = d["version"]
                if t == "custom-title":
                    info["custom_title"] = d.get("customTitle")
                elif t == "agent-name" and not info["custom_title"]:
                    info["custom_title"] = d.get("agentName")
                elif t == "user":
                    info["user_count"] += 1
                    if not info["first_user_text"]:
                        m = d.get("message")
                        text = ""
                        if isinstance(m, dict):
                            c = m.get("content", "")
                            if isinstance(c, list):
                                for x in c:
                                    if isinstance(x, dict) and x.get("text"):
                                        text = x["text"]; break
                            else:
                                text = c or ""
                        else:
                            text = str(m)
                        text = (text or "").strip()
                        # Skip claude-code's internal system frames (caveats,
                        # tool_result echoes wrapped as "user", etc.) — they
                        # show up as the FIRST message and make every native
                        # session look like "<local-command-caveat>...".
                        if text.startswith("<") and ">" in text[:40]:
                            continue
                        if not text:
                            continue
                        # Collapse whitespace so the value renders cleanly on a
                        # single line (display columns / Slack pings).
                        text = re.sub(r"\s+", " ", text)
                        info["first_user_text"] = text[:300]
                        if d.get("timestamp"):
                            info["first_user_at"] = d["timestamp"]
                elif t == "assistant":
                    info["assistant_count"] += 1
                    if d.get("message", {}).get("model"):
                        info["model"] = d["message"]["model"]
    except Exception:
        return info

    # Count subagent sidefiles, too — claude stores them under
    # <project>/<uuid>/subagents/agent-*.jsonl
    sub_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
    if sub_dir.is_dir():
        info["subagent_count"] = sum(1 for p in sub_dir.glob("agent-*.jsonl"))
    return info


def cmd_session_sync_native(args: argparse.Namespace) -> None:
    """Import claude-code native sessions from ~/.claude/projects into the registry."""
    if not _CLAUDE_PROJECTS_DIR.is_dir():
        raise SystemExit(f"claude projects dir not found: {_CLAUDE_PROJECTS_DIR}")

    inserted, updated, skipped = 0, 0, 0
    now = _now()
    with _connect() as conn:
        # Build a lookup of already-tracked UUIDs.
        existing = {row["uuid"]: dict(row) for row in
                    conn.execute("SELECT * FROM hermes_sessions").fetchall()}

        for proj_dir in _CLAUDE_PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                uid = jsonl.stem
                if not _UUID_RE.match(uid):
                    continue

                info = _scan_native_session(jsonl)
                cwd = info["cwd"] or _decode_claude_project_dir(proj_dir.name)
                if not cwd:
                    skipped += 1
                    continue

                # Find/create the matching project row.
                norm = _norm_path(cwd)
                proj_row = conn.execute(
                    "SELECT * FROM hermes_projects_v WHERE folder_path=?", (norm,),
                ).fetchone()
                if not proj_row:
                    if args.auto_register_projects:
                        repo = _detect_git_repo(norm)
                        conn.execute(
                            "INSERT INTO hermes_projects_v(folder_path, project_type, git_repo, "
                            "display_name, description, learned_notes, created_at, "
                            "last_used_at, use_count) VALUES (?, 'native', ?, ?, "
                            "'(auto-registered from claude-code session sync)', '', ?, ?, 0)",
                            (norm, repo, Path(norm).name, now, now),
                        )
                        proj_row = conn.execute(
                            "SELECT * FROM hermes_projects_v WHERE folder_path=?", (norm,)
                        ).fetchone()
                    else:
                        skipped += 1
                        continue

                name = info["custom_title"] or info["first_user_text"][:48] or f"native-{uid[:8]}"
                last_used = info["last_event_at"] or info["first_user_at"] or now
                first_seen = info["first_user_at"] or last_used

                if uid in existing:
                    # Don't downgrade a hermes-origin session — just refresh metadata.
                    sets = ["last_used_at=?"]
                    vals = [max(last_used, existing[uid]["last_used_at"])]
                    if not existing[uid].get("model") and info["model"]:
                        sets.append("model=?"); vals.append(info["model"])
                    if existing[uid].get("origin") == "native":
                        # Refresh native name; user may have set a custom-title
                        # mid-session, or our parser previously latched onto a
                        # system caveat row.
                        sets.append("name=?"); vals.append(name)
                        if info["first_user_text"]:
                            sets.append("brief=?"); vals.append(info["first_user_text"][:200])
                    vals.append(existing[uid]["id"])
                    conn.execute(
                        f"UPDATE hermes_sessions SET {', '.join(sets)} WHERE id=?", vals,
                    )
                    updated += 1
                    continue

                brief = info["first_user_text"][:200]
                notes = (
                    f"[sync] native session from {jsonl}\n"
                    f"        user/asst/sub = {info['user_count']}/"
                    f"{info['assistant_count']}/{info['subagent_count']}\n"
                    f"        claude version = {info['version'] or '?'}"
                )
                conn.execute(
                    "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, model, "
                    "brief, notes, created_at, last_used_at) "
                    "VALUES (?, ?, ?, 'active', 'native', ?, ?, ?, ?, ?)",
                    (uid, proj_row["id"], name, info["model"], brief, notes,
                     first_seen, last_used),
                )
                inserted += 1

        # Always reclassify at the end of a sync — the runs table is the
        # only authoritative signal for `origin`.
        sp, nv = _reclassify_origins(conn)

    print(f"sync: {inserted} new, {updated} refreshed, {skipped} skipped")
    print(f"reclassify: {sp} spawned, {nv} native")


# ---------------------------------------------------------------------------
# runs — each claude-code invocation against a session is a `run`. The session
# UUID stays stable (so `claude --resume <uuid>` always works); each run gets
# its own --name with a timestamp suffix so the user can identify *which*
# invocation they're picking up on. Useful when a worker spawn dies / a
# shutdown happens mid-task and a human needs to inspect or revive it.
# ---------------------------------------------------------------------------

def _next_run_index(conn: sqlite3.Connection, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(run_index), 0) AS m FROM hermes_runs WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return (row["m"] or 0) + 1


def _run_name(base: str, idx: int) -> str:
    # Format: "a-ok:<base>-r<idx>"
    # The `a-ok:` prefix is RESERVED for sessions spawned by the a-ok
    # (worker-control) dispatcher running inside this Hermes worker profile.
    # The dashboard's classifier (worker_control.hermes_ledger) treats this
    # prefix as the AUTHORITATIVE spawn signal — `claude -p` invocation
    # mode is NOT used for the spawn/native split anymore, because a human
    # can invoke `claude -p` manually too. Only the prefix is unforgeable
    # by a `/rename` (the prefix lives on hermes_runs.name, not on the
    # user-facing claude-side label).
    #
    # The colon (a-ok:) — not a dash — is intentional: it visibly marks
    # the run name as carrying a namespace, distinguishes a-ok-owned runs
    # from any historical `a-ok-...` slug a human might have chosen for
    # their own task names, and reads as "a-ok namespace, session id X".
    #
    # If you change this, also update:
    #   * D:/work-github/a-ok worker_control/hermes_ledger.py
    #     (A_OK_SPAWN_PREFIX constant)
    #   * hermes-pm-dispatcher-profile skill (classifier docs)
    prefix = "a-ok:"
    suffix = f"-r{idx}"
    max_base = max(8, 80 - len(prefix) - len(suffix))
    return prefix + base[:max_base] + suffix


def cmd_run_start(args: argparse.Namespace) -> None:
    """Allocate a new run on an existing session and emit the claude command."""
    with _connect() as conn:
        sess = _resolve_session(conn, args.session)
        if not sess:
            raise SystemExit(f"Session not found: {args.session}")
        proj = conn.execute("SELECT * FROM hermes_projects_v WHERE id=?",
                            (sess["project_id"],)).fetchone()

        idx = _next_run_index(conn, sess["id"])
        run_name = _run_name(sess["name"], idx)
        mode = "print" if args.print else "interactive"

        cmd = _build_claude_command(
            cwd=proj["folder_path"],
            uid=sess["uuid"],
            name=run_name,
            model=args.model or sess["model"],
            permission_mode=args.permission_mode or sess["permission_mode"],
            print_mode=args.print,
            prompt=args.prompt,
            max_turns=args.max_turns,
            allowed_tools=args.allowed_tools,
            resume=(idx > 1),    # runs >=2 must use --resume, not --session-id (already exists)
        )

        now = _now()
        conn.execute(
            "INSERT INTO hermes_runs(session_id, run_index, name, mode, command, status, started_at, hermes_session_id) "
            "VALUES (?, ?, ?, ?, ?, 'started', ?, ?)",
            (sess["id"], idx, run_name, mode, cmd, now, _HERMES_SESSION_ID),
        )
        # New run may promote this session to 'spawned' (e.g. first print run
        # after a chain of interactive ones). Idempotent.
        _reclassify_origins(conn)
        conn.execute(
            "UPDATE hermes_sessions SET last_used_at=? WHERE id=?", (now, sess["id"]),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    payload = {
        "run_id": run_id,
        "run_index": idx,
        "session_uuid": sess["uuid"],
        "name": run_name,
        "project": proj["folder_path"],
        "command": cmd,
        "mode": mode,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"run #{run_id} (idx {idx}, mode={mode})")
    print(f"  session  : {sess['uuid']}  ({sess['name']})")
    print(f"  --name   : {run_name}")
    print(f"  project  : {proj['folder_path']}")
    print(f"  command  : {cmd}")


def cmd_run_end(args: argparse.Namespace) -> None:
    promoted = False
    with _connect() as conn:
        row = conn.execute("SELECT * FROM hermes_runs WHERE id=?", (int(args.run_id),)).fetchone()
        if not row:
            raise SystemExit(f"Run not found: id={args.run_id}")
        now = _now()
        conn.execute(
            "UPDATE hermes_runs SET status=?, ended_at=?, note=? WHERE id=?",
            (args.status, now, args.note, row["id"]),
        )
        # Auto-promote spawn session to `done` when:
        #   - this run ended with status='done'
        #   - the run name carries the reserved `a-ok:` spawn prefix
        #   - no other run in the same session is still 'started'
        #   - the session is currently 'active' (don't overwrite failed/abandoned)
        # See hermes-pm-dispatcher-profile/references/session-status-lifecycle.md
        if args.status == "done" and (row["name"] or "").startswith("a-ok:"):
            sess = conn.execute(
                "SELECT id, uuid, name, status FROM hermes_sessions WHERE id=?",
                (row["session_id"],),
            ).fetchone()
            if sess and sess["status"] == "active":
                in_flight = conn.execute(
                    "SELECT COUNT(*) AS c FROM hermes_runs WHERE session_id=? AND status='started'",
                    (sess["id"],),
                ).fetchone()["c"]
                if in_flight == 0:
                    conn.execute(
                        "UPDATE hermes_sessions SET status='done', ended_at=?, last_used_at=? WHERE id=?",
                        (now, now, sess["id"]),
                    )
                    promoted = (sess["uuid"], sess["name"])
    print(f"run #{row['id']} → {args.status}")
    if promoted:
        print(f"session {promoted[0]} ({promoted[1]}) → done  [spawn auto-promote]")


def cmd_runs_list(args: argparse.Namespace) -> None:
    where, params = [], []
    if args.session:
        with _connect() as conn:
            sess = _resolve_session(conn, args.session)
            if not sess:
                raise SystemExit(f"Session not found: {args.session}")
            where.append("r.session_id=?"); params.append(sess["id"])
    if args.status:
        where.append("r.status=?"); params.append(args.status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(args.limit)
    sql = (
        "SELECT r.*, s.name AS session_name, s.uuid AS session_uuid, "
        "       p.display_name AS proj_name, p.folder_path AS proj_path "
        "FROM hermes_runs r "
        "JOIN hermes_sessions s ON s.id = r.session_id "
        "JOIN hermes_projects_v p ON p.id = s.project_id "
        f"{where_sql} ORDER BY r.started_at DESC LIMIT ?"
    )
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(no runs)")
        return
    print(f"{'ID':>4}  {'STATUS':<8}  {'MODE':<11}  {'SESSION':<22}  {'IDX':>3}  STARTED              NAME")
    for r in rows:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        sess_lbl = (r["session_name"] or "")[:22]
        print(f"{r['id']:>4}  {r['status']:<8}  {r['mode']:<11}  {sess_lbl:<22}  "
              f"{r['run_index']:>3}  {started:<20}  {r['name']}")


def main() -> None:
    p = argparse.ArgumentParser(description="worker project registry")
    p.add_argument("--db", help="override DB path (also WORKER_PROJECTS_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a folder")
    a.add_argument("path")
    a.add_argument("--name", help="display name (short)")
    a.add_argument("--type", help="project type/category (e.g. gitlab-nexon, github-official)")
    a.add_argument("--repo", help="git repo (auto-detected if omitted)")
    a.add_argument("--desc", help="one-line description")
    a.set_defaults(fn=cmd_add)

    l = sub.add_parser("list", help="list projects")
    l.add_argument("--sort", choices=["recent", "frequent", "name", "created"],
                   default="recent")
    l.add_argument("--type", help="filter by project_type")
    l.add_argument("--limit", type=int, default=50)
    l.add_argument("--json", action="store_true")
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="show one project")
    s.add_argument("key", help="id, path, or display_name (substring ok)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    u = sub.add_parser("update", help="edit fields / append note")
    u.add_argument("key")
    u.add_argument("--name")
    u.add_argument("--type", help="project type/category")
    u.add_argument("--repo")
    u.add_argument("--desc")
    u.add_argument("--append-note", help="append a learned note (timestamped)")
    u.set_defaults(fn=cmd_update)

    t = sub.add_parser("touch", help="bump last_used_at + use_count")
    t.add_argument("key")
    t.set_defaults(fn=cmd_touch)

    r = sub.add_parser("remove", help="drop from registry")
    r.add_argument("key")
    r.set_defaults(fn=cmd_remove)

    # --- sessions ----------------------------------------------------------
    ss = sub.add_parser("session", help="claude-code session tracking")
    ss_sub = ss.add_subparsers(dest="session_cmd", required=True)

    ss_start = ss_sub.add_parser("start", help="allocate a UUID and emit the claude command")
    ss_start.add_argument("project", help="project key (id/path/name)")
    ss_start.add_argument("--name", help="session display name (autoslug from --brief if omitted)")
    ss_start.add_argument("--brief", help="short task description (1 line)")
    ss_start.add_argument("--uuid", help="reuse a specific UUID instead of generating one")
    ss_start.add_argument("--model", help="claude model alias (sonnet/opus/haiku or full name)")
    ss_start.add_argument("--permission-mode", help="acceptEdits|auto|plan|bypassPermissions|...")
    ss_start.add_argument("--print", action="store_true",
                          help="emit a `claude -p` (one-shot) command; default is interactive")
    ss_start.add_argument("--prompt", help="prompt to embed when --print is set")
    ss_start.add_argument("--max-turns", type=int, help="--max-turns for print mode")
    ss_start.add_argument("--allowed-tools",
                          help='value passed verbatim to claude --allowedTools '
                               '(e.g. "Read,Edit,Bash")')
    ss_start.add_argument("--json", action="store_true")
    ss_start.set_defaults(fn=cmd_session_start)

    ss_resume = ss_sub.add_parser("resume-cmd",
                                  help="print the head-ful resume command for a session")
    ss_resume.add_argument("key", help="session id/uuid/name (substring ok)")
    ss_resume.add_argument("--json", action="store_true")
    ss_resume.set_defaults(fn=cmd_session_resume_cmd)

    ss_list = ss_sub.add_parser("list", help="list sessions")
    ss_list.add_argument("--project", help="filter by project key")
    ss_list.add_argument("--status", choices=["active", "done", "failed", "abandoned"],
                         help="filter by status")
    ss_list.add_argument("--origin", choices=["spawned", "native"],
                         help="filter by origin (spawned = worker dispatched a "
                              "non-interactive `claude -p` run; native = "
                              "everything else, even if worker allocated the UUID)")
    ss_list.add_argument("--sort", choices=["recent", "created", "name"], default="recent")
    ss_list.add_argument("--limit", type=int, default=50)
    ss_list.add_argument("--json", action="store_true")
    ss_list.set_defaults(fn=cmd_session_list)

    ss_show = ss_sub.add_parser("show", help="show session details")
    ss_show.add_argument("key")
    ss_show.add_argument("--json", action="store_true")
    ss_show.set_defaults(fn=cmd_session_show)

    ss_end = ss_sub.add_parser("end", help="mark a session done/failed/abandoned")
    ss_end.add_argument("key")
    ss_end.add_argument("--status", choices=["done", "failed", "abandoned"], default="done")
    ss_end.add_argument("--append-note", help="add a final note describing the outcome")
    ss_end.set_defaults(fn=cmd_session_end)

    ss_note = ss_sub.add_parser("note", help="append a timestamped note to a session")
    ss_note.add_argument("key")
    ss_note.add_argument("text")
    ss_note.set_defaults(fn=cmd_session_note)

    ss_sync = ss_sub.add_parser("sync-native",
                                help="scan ~/.claude/projects for native claude-code "
                                     "sessions and import them with origin='native'")
    ss_sync.add_argument("--auto-register-projects", action="store_true",
                         help="auto-create projects for folders we don't know about yet "
                              "(otherwise sessions for unknown folders are skipped)")
    ss_sync.set_defaults(fn=cmd_session_sync_native)

    ss_recl = ss_sub.add_parser("reclassify",
                                help="recompute origin (spawned vs native) for every "
                                     "session based on the runs table. Idempotent.")
    ss_recl.set_defaults(fn=cmd_session_reclassify)

    # --- runs (per-invocation timeline of a session) ----------------------
    rr = sub.add_parser("run", help="claude-code run tracking inside a session")
    rr_sub = rr.add_subparsers(dest="run_cmd", required=True)

    rr_start = rr_sub.add_parser("start", help="record a new run on an existing session "
                                                "and emit the claude command (uses --resume)")
    rr_start.add_argument("session", help="session uuid/name/id")
    rr_start.add_argument("--model")
    rr_start.add_argument("--permission-mode")
    rr_start.add_argument("--print", action="store_true",
                          help="emit `claude -p` instead of interactive")
    rr_start.add_argument("--prompt")
    rr_start.add_argument("--max-turns", type=int)
    rr_start.add_argument("--allowed-tools")
    rr_start.add_argument("--json", action="store_true")
    rr_start.set_defaults(fn=cmd_run_start)

    rr_end = rr_sub.add_parser("end", help="mark a run finished")
    rr_end.add_argument("run_id", help="run id (from `runs list` or `run start` output)")
    rr_end.add_argument("--status", choices=["done", "failed"], default="done")
    rr_end.add_argument("--note", help="freeform outcome note")
    rr_end.set_defaults(fn=cmd_run_end)

    runs = sub.add_parser("runs", help="list runs (timeline view)")
    runs.add_argument("--session", help="filter by session key")
    runs.add_argument("--status", choices=["started", "done", "failed"])
    runs.add_argument("--limit", type=int, default=50)
    runs.add_argument("--json", action="store_true")
    runs.set_defaults(fn=cmd_runs_list)

    args = p.parse_args()
    if args.db:
        global DB_PATH
        DB_PATH = Path(args.db).expanduser().resolve()
    args.fn(args)


if __name__ == "__main__":
    main()
