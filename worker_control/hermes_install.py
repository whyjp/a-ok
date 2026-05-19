"""Hermes profile integration — install / sync / bootstrap.

These commands let a fresh checkout become a fully wired worker-control
host in one line:

    workerctl bootstrap

What ``bootstrap`` does (idempotent, safe to re-run):

  1. ``workerctl init``                    — create canonical SQLite DB.
  2. apply schema extensions               — ``hermes_sessions / runs /
                                             subprocs`` tables + the
                                             ``hermes_projects_v`` legacy
                                             compat view (used by the
                                             worker_control_hermes scripts).
  3. ``workerctl projects scan``           — populate the projects table.
  4. ``workerctl install-hermes-profile``  — for every discovered hermes
                                             profile, install thin wrapper
                                             scripts at ``profiles/<name>/
                                             scripts/`` that delegate to the
                                             ``workerctl-hermes-*`` console
                                             scripts. This is the part that
                                             keeps existing SOUL.md /
                                             cron paths working AS-IS.
  5. autostart wire-up                     — drop a ``.cmd`` in the Windows
                                             Startup folder so the BFF comes
                                             up on next logon. On POSIX,
                                             prints the equivalent
                                             ``systemd --user`` snippet.
  6. ``dashboard-daemon --once``           — make sure the BFF is up *now*.

Each step prints what it did (or "already in place, skipped") so the user
can re-run safely after pulling new commits.

Wrapper-script contents:
The wrappers are tiny Python shebang stubs that exec the installed
``workerctl-hermes-*`` console script with the original argv. We embed
the absolute path to the venv's python (the one that runs workerctl) so
the wrappers keep working even if PATH changes later.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Iterable

from worker_control import __version__
from worker_control.db import connect, init_db
from worker_control.hermes_profiles import (
    HermesProfile,
    discover_hermes_profiles,
    hermes_home,
)
from worker_control.paths import configured_roots, runtime_root


# ── schema extension (kept in sync with worker_control_hermes/) ────────────

_EXTRA_SCHEMA_SQL = """
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
    ended_at        TEXT,
    mention_id      TEXT GENERATED ALWAYS AS (
        CASE origin WHEN 'spawned' THEN 'aok#' ELSE 'nat#' END
        || substr(uuid, 1, 8)
    ) VIRTUAL
);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_project    ON hermes_sessions(project_id);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_status     ON hermes_sessions(status);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_origin     ON hermes_sessions(origin);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_last_used  ON hermes_sessions(last_used_at DESC);
CREATE INDEX IF NOT EXISTS ix_hermes_sessions_mention_id ON hermes_sessions(mention_id);

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
    hermes_session_id TEXT,
    UNIQUE(session_id, run_index)
);
CREATE INDEX IF NOT EXISTS ix_hermes_runs_session ON hermes_runs(session_id);
CREATE INDEX IF NOT EXISTS ix_hermes_runs_started ON hermes_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_hermes_runs_hsess   ON hermes_runs(hermes_session_id);

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


def _apply_extra_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_EXTRA_SCHEMA_SQL)
        # Forward-only migration for DBs created before the GENERATED VIRTUAL
        # mention_id column was added. SQLite permits adding a VIRTUAL
        # generated column via ALTER TABLE (STORED would require a rebuild).
        # Use table_xinfo (not table_info) — the latter omits GENERATED
        # columns, so the check would always fire and re-ALTER would 1815.
        cols = {r[1] for r in conn.execute("PRAGMA table_xinfo(hermes_sessions)")}
        if "mention_id" not in cols:
            conn.execute(
                "ALTER TABLE hermes_sessions ADD COLUMN mention_id TEXT "
                "GENERATED ALWAYS AS ("
                "CASE origin WHEN 'spawned' THEN 'aok#' ELSE 'nat#' END "
                "|| substr(uuid, 1, 8)) VIRTUAL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_hermes_sessions_mention_id "
                "ON hermes_sessions(mention_id)"
            )
        # The hermes_projects_v view + INSTEAD-OF triggers (backward-compat
        # for the legacy projects.db column shape). The SQL file is shipped
        # as package data inside worker_control_hermes/.
        try:
            sql_text = (_pkg_files("worker_control_hermes")
                        .joinpath("hermes_projects_view.sql")
                        .read_text(encoding="utf-8"))
            conn.executescript(sql_text)
        except FileNotFoundError:
            # Package data missing — non-fatal; the view is only needed if
            # someone runs legacy scripts that read `projects` (old shape).
            pass
        conn.commit()
    finally:
        conn.close()


# ── wrapper installation ───────────────────────────────────────────────────

# Each wrapper is a tiny self-contained Python launcher that execs the
# installed console-script entry point. Embedding the absolute python path
# means the wrappers keep working even if PATH later loses the venv.
_WRAPPER_TEMPLATE = '''\
#!{python}
# Auto-generated by `workerctl install-hermes-profile`. Do not hand-edit —
# changes will be overwritten on the next bootstrap/install. The wrapper
# only exists so legacy SOUL.md / cron entries referencing this path keep
# working; new automation should call `workerctl-hermes-{name}` directly.
import os, sys
from worker_control_hermes import {module} as _mod
sys.exit(_mod.main() or 0)
'''


# (filename-stem, module-name-in-package, console-script-name)
_WRAPPERS: tuple[tuple[str, str, str], ...] = (
    ("projects",                "projects",               "projects"),
    ("heartbeat",               "heartbeat",              "heartbeat"),
    ("build_report",            "build_report",           "report"),
    ("subprocs",                "subprocs",               "subprocs"),
    ("migrate_to_canonical_db", "migrate_to_canonical_db", "migrate"),
)


def _write_wrapper(target: Path, module: str, name: str,
                   python_exe: str) -> str:
    """Write a wrapper script. Returns one of 'created' / 'updated' / 'skipped'."""
    content = _WRAPPER_TEMPLATE.format(
        python=python_exe.replace("\\", "/"),
        module=module,
        name=name,
    )
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if existing == content:
            return "skipped"
        action = "updated"
    else:
        action = "created"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    try:
        # POSIX exec bit — no-op on NTFS, harmless.
        target.chmod(target.stat().st_mode | 0o755)
    except OSError:
        pass
    return action


def install_wrappers(profile: HermesProfile,
                     python_exe: str | None = None) -> dict[str, str]:
    """Drop wrapper *.py files in ``<profile>/scripts/``.

    Returns ``{filename: 'created'|'updated'|'skipped'}`` for reporting.
    """
    scripts_dir = Path(profile.path) / "scripts"
    python_exe = python_exe or sys.executable
    out: dict[str, str] = {}
    for stem, module, name in _WRAPPERS:
        target = scripts_dir / f"{stem}.py"
        out[target.name] = _write_wrapper(target, module, name, python_exe)
    # Also ship hermes_projects_view.sql alongside (it was historically next
    # to the scripts; some tooling may still glob for it there).
    try:
        sql = (_pkg_files("worker_control_hermes")
               .joinpath("hermes_projects_view.sql"))
        sql_target = scripts_dir / "hermes_projects_view.sql"
        new = sql.read_text(encoding="utf-8")
        if not sql_target.is_file() or sql_target.read_text(encoding="utf-8") != new:
            sql_target.write_text(new, encoding="utf-8")
            out["hermes_projects_view.sql"] = "updated" if sql_target.exists() else "created"
        else:
            out["hermes_projects_view.sql"] = "skipped"
    except FileNotFoundError:
        pass
    return out


def install_into_all_profiles(python_exe: str | None = None
                              ) -> dict[str, dict[str, str]]:
    """Install wrappers in every discovered Hermes profile.

    We skip the implicit ``default`` profile (= the hermes home root itself).
    Its ``scripts/`` directory belongs to hermes proper (heartbeat shell
    scripts, ndjson compactors, etc.), and dropping our wrappers next to
    those files would mix two unrelated tooling layers in one folder. If a
    user really wants the wrappers in the root home, they can pass
    ``--include-default`` (handled by the CLI wrapper).

    Returns ``{profile_name: per-file-result-dict}``.
    """
    profiles = discover_hermes_profiles()
    out: dict[str, dict[str, str]] = {}
    for prof in profiles:
        if prof.is_default:
            continue
        out[prof.name] = install_wrappers(prof, python_exe=python_exe)
    return out


# ── autostart wire-up ──────────────────────────────────────────────────────

def _windows_startup_dir() -> Path:
    return (Path.home()
            / "AppData" / "Roaming" / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def _wctl_executable() -> str:
    """Resolve the absolute path to the workerctl entry point."""
    # On Windows the venv installs `workerctl.exe` next to python.exe.
    exe = shutil.which("workerctl")
    if exe:
        return exe
    # Fall back to the venv layout: <python_dir>/workerctl[.exe]
    py_dir = Path(sys.executable).parent
    for cand in (py_dir / "workerctl.exe", py_dir / "workerctl"):
        if cand.exists():
            return str(cand)
    return "workerctl"  # last resort; relies on PATH


def install_autostart(*, log_path: Path | None = None) -> tuple[Path | None, str]:
    """Install the dashboard-daemon autostart entry. Returns (path, status).

    Status is one of:
      * 'created'  — newly written
      * 'updated'  — content changed
      * 'skipped'  — already correct
      * 'unsupported' — non-Windows host (we still print POSIX instructions)
    """
    if os.name != "nt":
        return None, "unsupported"
    startup = _windows_startup_dir()
    target = startup / "workerctl-dashboard.cmd"
    wctl = _wctl_executable()
    log = str(log_path) if log_path else str(runtime_root() / "dashboard.log")
    content = (
        "@echo off\r\n"
        "REM Auto-generated by `workerctl bootstrap` / install-hermes-profile.\r\n"
        "REM Re-run bootstrap to refresh paths.\r\n"
        f'set "WCTL={wctl}"\r\n'
        f'set "LOG={log}"\r\n'
        'start "" /B "%WCTL%" dashboard-daemon --once --log "%LOG%"\r\n'
    )
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return target, "skipped"
    action = "updated" if target.is_file() else "created"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target, action


# ── bootstrap orchestrator (the user-facing one-liner) ─────────────────────

def cmd_install_hermes_profile(args) -> int:
    python_exe = args.python or sys.executable
    out = install_into_all_profiles(python_exe=python_exe)
    if not out:
        print("(no hermes profile found — nothing to install)")
        return 0
    for name, per_file in out.items():
        print(f"profile: {name}")
        for fname, status in per_file.items():
            print(f"  {status:8s}  {fname}")
    return 0


def cmd_bootstrap(args) -> int:
    """End-to-end host setup. Idempotent — safe to re-run."""
    print(f"=== worker-control bootstrap (v{__version__}) ===")

    # 1. DB init
    db_path = init_db()
    print(f"[1/6] db init      → {db_path}")

    # 2. Schema extensions
    _apply_extra_schema(db_path)
    print(f"[2/6] schema ext   → hermes_sessions / runs / subprocs / view applied")

    # 3. Projects scan
    if args.skip_scan:
        print("[3/6] projects scan → skipped (--skip-scan)")
    else:
        from worker_control import scanner
        total_proj, total_git = 0, 0
        for root in configured_roots():
            if not root.path.exists():
                print(f"      (skip {root.role}: {root.path} not found)")
                continue
            results = scanner.scan_root(str(root.path))
            n = len(results)
            g = sum(1 for r in results if r.is_git)
            total_proj += n
            total_git += g
            print(f"      scanned {root.path} [{root.role}]: "
                  f"{n} entries, {g} git repos")
        print(f"[3/6] projects scan → total {total_proj} entries, {total_git} git")

    # 4. Install wrappers into discovered hermes profiles
    wrap_out = install_into_all_profiles()
    if wrap_out:
        for name, per_file in wrap_out.items():
            counts: dict[str, int] = {}
            for status in per_file.values():
                counts[status] = counts.get(status, 0) + 1
            summary = " · ".join(f"{k}:{v}" for k, v in counts.items())
            print(f"[4/6] hermes wrappers ({name}) → {summary}")
    else:
        print("[4/6] hermes wrappers → no hermes profile detected (skipped)")

    # 5. Autostart
    auto_path, auto_status = install_autostart()
    if auto_status == "unsupported":
        print("[5/6] autostart    → not on Windows; manual setup:")
        print("       systemd --user: place a `workerctl-dashboard.service` unit")
        print(f"       calling: {_wctl_executable()} dashboard-daemon")
    else:
        print(f"[5/6] autostart    → {auto_status}  {auto_path}")

    # 6. Start the BFF right now so the user can open the dashboard
    #    without waiting for next logon.
    if args.skip_dashboard:
        print("[6/6] dashboard    → skipped (--skip-dashboard)")
    else:
        try:
            wctl = _wctl_executable()
            subprocess.run([wctl, "dashboard-daemon", "--once"], check=False)
            print(f"[6/6] dashboard    → http://127.0.0.1:8765/ (use --skip-dashboard to skip)")
        except Exception as exc:
            print(f"[6/6] dashboard    → failed: {exc}")

    print()
    print("✅ bootstrap complete. Open http://127.0.0.1:8765/ to view the dashboard.")
    print("   Re-run `workerctl bootstrap` after `git pull` to refresh wrappers/schema.")
    return 0


def add_subcommands(sub) -> None:
    """Register install-hermes-profile + bootstrap on the workerctl CLI parser."""
    ihp = sub.add_parser(
        "install-hermes-profile",
        help="install thin wrapper scripts inside every discovered "
             "Hermes profile's scripts/ dir (legacy path compat for "
             "SOUL.md / cron entries that still reference scripts/*.py)",
    )
    ihp.add_argument("--python",
                     help="python executable to embed in the wrapper shebang "
                          "(default: current sys.executable)")
    ihp.set_defaults(func=cmd_install_hermes_profile)

    bs = sub.add_parser(
        "bootstrap",
        help="one-shot host setup: init DB + apply schema + projects scan + "
             "install hermes wrappers + autostart + start BFF. Idempotent.",
    )
    bs.add_argument("--skip-scan", action="store_true",
                    help="skip `projects scan` step")
    bs.add_argument("--skip-dashboard", action="store_true",
                    help="skip starting dashboard-daemon at the end")
    bs.set_defaults(func=cmd_bootstrap)
