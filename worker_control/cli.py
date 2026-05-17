"""workerctl CLI — argparse-based, stdlib-only."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from worker_control import (
    __version__, dashboard, db, profiles, projects, scanner, sessions,
)
from worker_control.paths import (
    classify_path,
    configured_roots,
    db_path,
    project_root_default,
    runtime_root,
)
from worker_control.sessions import WriteProtectedRootError


# --- subcommand handlers -----------------------------------------------------

def cmd_init(_args: argparse.Namespace) -> int:
    target = db.init_db()
    print(f"runtime root : {runtime_root()}")
    print(f"db path      : {target}")
    print("configured workspace roots:")
    for root in configured_roots():
        print(f"  - {root.role:18s} {root.path}")
    print("OK")
    return 0


def cmd_profiles_list(_args: argparse.Namespace) -> int:
    rows = profiles.list_profiles()
    if not rows:
        print("(no profiles — create one with `workerctl profiles create <name>`)")
        return 0
    for p in rows:
        print(f"{p.id:>3}  {p.name:20s}  root={p.root_path}")
    return 0


def cmd_profiles_create(args: argparse.Namespace) -> int:
    try:
        p = profiles.create_profile(args.name, root=args.root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"created profile #{p.id}: {p.name} (root={p.root_path})")
    return 0


def cmd_projects_scan(args: argparse.Namespace) -> int:
    # --root 가 명시되면 그 루트만 스캔, 아니면 모든 configured_roots() 스캔.
    if args.root:
        found = scanner.scan_root(args.root)
        role = classify_path(args.root)
        git = sum(1 for f in found if f.is_git)
        print(f"scanned {args.root} [{role}]: {len(found)} entries, {git} git repos")
        return 0

    total_found = 0
    total_git = 0
    for root in configured_roots():
        found = scanner.scan_root(root.path)
        git = sum(1 for f in found if f.is_git)
        total_found += len(found)
        total_git += git
        marker = "" if root.path.exists() else "  (path missing — 0 entries)"
        print(
            f"scanned {root.path} [{root.role}]: "
            f"{len(found)} entries, {git} git repos{marker}"
        )
    print(f"total: {total_found} entries, {total_git} git repos")
    return 0


def cmd_projects_list(args: argparse.Namespace) -> int:
    rows = projects.list_projects(git_only=args.git_only)
    if not rows:
        print("(no projects — run `workerctl projects scan` first)")
        return 0
    for p in rows:
        git = "git" if p.is_git else "   "
        dirty = "*" if p.is_dirty else " "
        branch = p.branch or "-"
        print(
            f"{p.id:>3}  {git} {dirty}  [{p.root_role:16s}] "
            f"{p.name:32s}  {branch:20s}  {p.path}"
        )
    return 0


def cmd_sessions_list(_args: argparse.Namespace) -> int:
    rows = sessions.list_sessions()
    if not rows:
        print("(no sessions)")
        return 0
    for s in rows:
        print(f"{s.id:>3}  [{s.state:13s}] {s.runtime:7s}  "
              f"{s.name}  tmux={s.tmux_session or '-'} pid={s.pid or '-'}")
    return 0


def cmd_sessions_start(args: argparse.Namespace) -> int:
    try:
        s = sessions.start_session(args.profile, args.project,
                                   prefer_tmux=not args.no_tmux)
    except WriteProtectedRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (LookupError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"started session #{s.id}: {s.name}  state={s.state}  runtime={s.runtime}")
    if s.runtime == "console":
        print("note: no tmux — opened a console window; capture/prompt are limited.")
    return 0


def cmd_sessions_capture(args: argparse.Namespace) -> int:
    try:
        s, body = sessions.capture(args.session)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"--- capture from session #{s.id} ({s.name}) via {s.runtime} ---")
    print(body)
    return 0


def cmd_sessions_prompt(args: argparse.Namespace) -> int:
    try:
        s, note = sessions.prompt(args.session, args.text)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"prompt → #{s.id} ({s.name}): {note}")
    return 0 if note == "ok" else 1


def cmd_view_html(args: argparse.Namespace) -> int:
    """HTML 대시보드를 생성한다. ``--open`` 으로 브라우저까지 열 수 있다."""
    output_path = dashboard.write_dashboard(
        output=args.output,
        native_limit=args.native_limit,
    )
    print(f"dashboard written: {output_path}")
    if args.open:
        opened = dashboard.open_in_browser(output_path)
        print("opened in browser" if opened else "(browser open failed)")
    return 0


def cmd_sessions_stop(args: argparse.Namespace) -> int:
    try:
        s = sessions.stop_session(args.session)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"stopped session #{s.id}: {s.name}  state={s.state}")
    return 0


# --- argparse wiring ---------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workerctl",
        description="Local Claude Code worker-control (SQLite-backed).",
    )
    p.add_argument("--version", action="version",
                   version=f"worker-control {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    sp = sub.add_parser("init", help="initialize the SQLite DB")
    sp.set_defaults(func=cmd_init)

    # profiles
    pp = sub.add_parser("profiles", help="manage worker profiles")
    pp_sub = pp.add_subparsers(dest="action", required=True)
    pl = pp_sub.add_parser("list", help="list profiles")
    pl.set_defaults(func=cmd_profiles_list)
    pc = pp_sub.add_parser("create", help="create a new profile")
    pc.add_argument("name")
    pc.add_argument("--root", default=None,
                    help="default project root (default: D:/work-github)")
    pc.set_defaults(func=cmd_profiles_create)

    # projects
    pj = sub.add_parser("projects", help="scan/list discovered projects")
    pj_sub = pj.add_subparsers(dest="action", required=True)
    ps = pj_sub.add_parser(
        "scan",
        help="scan a project root; without --root scans all configured roots",
    )
    ps.add_argument("--root", default=None)
    ps.set_defaults(func=cmd_projects_scan)
    pl2 = pj_sub.add_parser("list", help="list projects")
    pl2.add_argument("--git-only", action="store_true")
    pl2.set_defaults(func=cmd_projects_list)

    # sessions
    ss = sub.add_parser("sessions", help="manage worker sessions")
    ss_sub = ss.add_subparsers(dest="action", required=True)

    sl = ss_sub.add_parser("list", help="list sessions")
    sl.set_defaults(func=cmd_sessions_list)

    sst = ss_sub.add_parser("start", help="start a new session")
    sst.add_argument("--profile", required=True)
    sst.add_argument("--project", required=True)
    sst.add_argument("--no-tmux", action="store_true",
                     help="force console fallback even if tmux is available")
    sst.set_defaults(func=cmd_sessions_start)

    sc = ss_sub.add_parser("capture", help="capture current screen / recent events")
    sc.add_argument("session", help="session id or name")
    sc.set_defaults(func=cmd_sessions_capture)

    spt = ss_sub.add_parser("prompt", help="send a prompt to a session")
    spt.add_argument("session", help="session id or name")
    spt.add_argument("text", help="text to send")
    spt.set_defaults(func=cmd_sessions_prompt)

    sx = ss_sub.add_parser("stop", help="stop a session")
    sx.add_argument("session", help="session id or name")
    sx.set_defaults(func=cmd_sessions_stop)

    # view (HTML dashboard)
    vv = sub.add_parser(
        "view",
        help="render the worker-control state as a static dashboard",
    )
    vv_sub = vv.add_subparsers(dest="action", required=True)
    vh = vv_sub.add_parser(
        "html",
        help="write a single-file HTML dashboard "
             "(workers / hermes sessions / native sessions / projects)",
    )
    vh.add_argument(
        "--output", "-o", default=None,
        help="output HTML path "
             "(default: <runtime_root>/dashboard.html, "
             "i.e. D:/work-github/.worker-control/dashboard.html)",
    )
    vh.add_argument(
        "--open", action="store_true",
        help="also open the generated file in the default browser",
    )
    vh.add_argument(
        "--native-limit", type=int, default=500,
        help="maximum number of native Claude sessions to discover "
             "(default: 500; use 0 to skip native discovery)",
    )
    vh.set_defaults(func=cmd_view_html)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
