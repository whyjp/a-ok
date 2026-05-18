"""workerctl CLI — argparse-based, stdlib-only."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from worker_control import (
    __version__, autostart, dashboard, db, profiles, projects, scanner,
    server, session_sync, sessions, snapshot,
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
    # Always also list Hermes profiles discovered on disk — even when the
    # workerctl DB is empty, the dashboard / CLI should show what's actually
    # on the host.
    from worker_control.hermes_profiles import (
        discover_hermes_profiles,
        hermes_home,
    )
    hermes_rows = discover_hermes_profiles()

    if not rows and not hermes_rows:
        print("(no profiles — create one with `workerctl profiles create <name>`)")
        return 0

    print(f"-- workerctl DB profiles ({len(rows)}) --")
    if not rows:
        print("  (none — DB table `worker_profiles` is empty)")
    else:
        for p in rows:
            print(f"  {p.id:>3}  {p.name:20s}  root={p.root_path}")

    print(f"\n-- hermes disk profiles ({len(hermes_rows)}) "
          f"@ {hermes_home()} --")
    if not hermes_rows:
        print("  (no hermes home found)")
    else:
        for hp in hermes_rows:
            tag = " [default]" if hp.is_default else ""
            model = hp.model or "—"
            print(f"  {hp.name:20s}{tag}  model={model}  "
                  f"skills={hp.skills_count}  sessions={hp.sessions_count}")
            print(f"                          path={hp.path}")
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
    """**LEGACY**: 단일 파일 인라인 스냅샷 HTML 을 작성한다.

    기본 운영 흐름은 ``workerctl view serve`` (SQLite 기반 BFF) 이다. 이
    명령은 오프라인 첨부/공유용으로만 남겨두었고, ``--legacy`` 플래그를
    명시해야 동작한다.
    """
    if not args.legacy:
        print(
            "error: `workerctl view html` 은 레거시 export 입니다. 기본 운영은 "
            "`workerctl view serve` (SQLite 기반 BFF + 동적 FE) 를 쓰세요.\n"
            "       단일 파일 스냅샷이 정말 필요하면 `--legacy` 플래그를 함께 "
            "지정하세요.",
            file=sys.stderr,
        )
        return 2

    output_path = dashboard.write_dashboard(
        output=args.output,
        native_limit=args.native_limit,
    )
    print(f"[legacy] dashboard written: {output_path}")
    print(
        "  note: 이 파일은 BFF 없이 단독으로 동작하는 오프라인 스냅샷입니다. "
        "DB 가 갱신되면 다시 생성해야 합니다."
    )
    if args.open:
        opened = dashboard.open_in_browser(output_path)
        print("opened in browser" if opened else "(browser open failed)")
    return 0


def cmd_view_serve(args: argparse.Namespace) -> int:
    """대시보드 BFF + 정적 FE 를 띄운다.

    ``GET /``               → 정적 FE HTML (``worker_control/static/dashboard.html``)
    ``GET /api/snapshot``   → 현재 스냅샷 JSON (요청마다 SQLite 재조회)
    ``GET /api/health``     → 헬스체크 JSON (DB 존재 여부 포함)
    """
    host = args.host
    port = args.port

    if host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        print(
            f"error: refusing to bind {host} — 대시보드는 DB/프로젝트 경로 등 "
            "환경 정보를 노출합니다. loopback(127.0.0.1) 외 주소로 띄우려면 "
            "`--allow-remote` 를 함께 지정하세요.",
            file=sys.stderr,
        )
        return 2

    try:
        srv = server.make_server(
            host=host, port=port,
            native_limit=args.native_limit,
            log_sink=sys.stderr,
            db_path_override=args.db,
            runtime_root_override=args.runtime_root,
        )
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2

    url = srv.url
    print(f"worker-control dashboard BFF listening on {url}")
    print(f"  GET {url}                → 정적 FE (dashboard.html)")
    print(f"  GET {url}api/snapshot    → 동적 스냅샷 JSON")
    print(f"  GET {url}api/health      → 헬스체크 JSON")
    print(f"  db:           {srv.db_path}")
    print(f"  runtime root: {srv.runtime_root}")
    if not srv.db_path.exists():
        print(
            f"  warning: DB 파일이 아직 없습니다 → `workerctl init` 으로 만드세요.",
            file=sys.stderr,
        )
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "warning: 비-loopback 주소로 바인딩되었습니다. 신뢰할 수 없는 "
            "네트워크에는 노출하지 마세요.",
            file=sys.stderr,
        )

    if args.open:
        opened = dashboard.open_in_browser_url(url)
        print("opened in browser" if opened else "(browser open failed)")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        srv.server_close()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """``workerctl dashboard`` 는 ``workerctl view serve`` 의 짧은 별칭."""
    return cmd_view_serve(args)


def cmd_dashboard_daemon(args: argparse.Namespace) -> int:
    """동적 dashboard BFF 를 상시 실행 (Hermes 시작 시 자동 기동용).

    이미 같은 host/port 로 떠 있으면 health-check 만 통과시키고 즉시 종료.
    ``--watch`` (기본 켜짐) 이면 ``worker_control/*.py`` / ``static/*.html``
    mtime 을 감시해서 코드가 바뀌면 자식 BFF 를 안전하게 재시작한다.
    """
    log_path = Path(args.log) if args.log else None

    if args.once or not args.watch:
        res = autostart.ensure_running(
            host=args.host, port=args.port,
            allow_remote=args.allow_remote,
            db=args.db, runtime_root=args.runtime_root,
            native_limit=args.native_limit,
            log_path=log_path,
        )
        print(res.note)
        if res.health is None:
            return 1
        return 0

    return autostart.run_supervisor(
        host=args.host, port=args.port,
        allow_remote=args.allow_remote,
        db=args.db, runtime_root=args.runtime_root,
        native_limit=args.native_limit,
        log_path=log_path,
        on_event=lambda k, m: print(f"[autostart:{k}] {m}", flush=True),
    )


def cmd_dashboard_snapshot(args: argparse.Namespace) -> int:
    """legacy 정적 dashboard 를 Telegram 으로 보낼 stdout 페이로드로 발사."""
    return snapshot.emit_snapshot(
        output=Path(args.output) if args.output else None,
        native_limit=args.native_limit,
        include_native_for_liveness=not args.no_native_liveness,
    )


def cmd_sessions_stop(args: argparse.Namespace) -> int:
    try:
        s = sessions.stop_session(args.session)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"stopped session #{s.id}: {s.name}  state={s.state}")
    return 0


def cmd_sessions_sync_hermes(args: argparse.Namespace) -> int:
    """Scan every Hermes profile's sessions/*.json and persist to SQLite."""
    from worker_control.hermes_session_sync import sync_once
    profiles_filter = tuple(args.profile) if args.profile else None
    res = sync_once(profiles_filter=profiles_filter)
    print(
        f"hermes-session-sync: profiles={res.profiles_scanned} "
        f"files={res.files_seen} upserted={res.upserted} "
        f"enriched={res.enrichment_updated} skipped={res.skipped} "
        f"({res.duration_ms} ms)"
    )
    return 0


def cmd_session_sync_all(args: argparse.Namespace) -> int:
    """Phase 2 PR #6 — walk every disk source once and reconcile the ledger.

    Calls ``session_sync.sync_all`` against the canonical DB. The function
    itself is idempotent; the CLI is a thin wrapper that adds progress
    printing and a one-line summary so cron / heartbeat can both reach the
    same code path without diverging on logging behaviour.
    """
    with db.connect() as conn:
        result = session_sync.sync_all(
            conn,
            since=args.since,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
    if not args.quiet:
        print(
            f"sync-all: synced(jsonl)={result.synced_jsonl} "
            f"synced(profile)={result.synced_profile} "
            f"skipped(mtime_unchanged)={result.skipped_mtime_unchanged} "
            f"skipped(no_project)={result.skipped_no_project} "
            f"skipped(no_uuid)={result.skipped_no_uuid} "
            f"errors={result.errors} "
            f"reclassify=(spawned={result.reclassify_spawned},"
            f"native={result.reclassify_native}) "
            f"({result.duration_ms} ms)"
        )
    return 0 if result.errors == 0 else 1


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

    sh = ss_sub.add_parser(
        "sync-hermes",
        help="scan ~/AppData/Local/hermes/profiles/*/sessions/*.json and "
             "UPSERT into the hermes_agent_sessions table (single source of "
             "truth for the dashboard's 'hermes 세션' tab)",
    )
    sh.add_argument(
        "--profile", action="append", default=[],
        help="restrict to a profile name (repeatable; default = all profiles)",
    )
    sh.set_defaults(func=cmd_sessions_sync_hermes)

    # session — Phase 2 PR #6 singular subcommand for unified writer entry
    # points. Today it only carries `sync-all`; future PRs will migrate the
    # other session lifecycle commands here.
    sess = sub.add_parser(
        "session",
        help="unified session-ledger ops (Phase 2 PR #6+)",
    )
    sess_sub = sess.add_subparsers(dest="action", required=True)

    ssa = sess_sub.add_parser(
        "sync-all",
        help="walk ~/.claude/projects/*.jsonl + hermes profiles' session_*.json "
             "and upsert every row into hermes_sessions via the unified "
             "session_sync writer; finishes by calling reclassify_origins. "
             "Idempotent; safe to run on a 5-min cron.",
    )
    ssa.add_argument(
        "--since", default=None,
        help="ISO-8601 cutoff: ignore files older than this "
             "(applies to file mtime — a fast pre-filter, not a "
             "session-touched-since filter)",
    )
    ssa.add_argument(
        "--dry-run", action="store_true",
        help="walk and count but do not write to the DB",
    )
    ssa.add_argument(
        "--quiet", action="store_true",
        help="suppress progress and the final summary line",
    )
    ssa.set_defaults(func=cmd_session_sync_all)

    # view (dashboard FE + BFF; legacy export 도 여기 아래)
    vv = sub.add_parser(
        "view",
        help="dashboard FE/BFF — `serve` (권장, SQLite 기반 BFF), "
             "`html` (legacy 단일 파일 export)",
    )
    vv_sub = vv.add_subparsers(dest="action", required=True)

    def _add_serve_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--host", default=server.DEFAULT_HOST,
            help=f"bind host (default: {server.DEFAULT_HOST}; "
                 "loopback 외 주소는 --allow-remote 필요)",
        )
        parser.add_argument(
            "--port", type=int, default=server.DEFAULT_PORT,
            help=f"bind port (default: {server.DEFAULT_PORT})",
        )
        parser.add_argument(
            "--allow-remote", action="store_true",
            help="allow binding to non-loopback addresses "
                 "(데이터/경로가 노출될 수 있으므로 신뢰된 네트워크에서만 사용)",
        )
        parser.add_argument(
            "--open", action="store_true",
            help="also open the served URL in the default browser",
        )
        parser.add_argument(
            "--native-limit", type=int, default=500,
            help="maximum number of native Claude sessions to discover per "
                 "request (default: 500; use 0 to skip native discovery)",
        )
        parser.add_argument(
            "--db", default=None,
            help="SQLite DB 경로 override "
                 "(default: D:/work-github/.worker-control/worker-control.sqlite3, "
                 "환경변수 WORKER_CONTROL_DB 와 동일)",
        )
        parser.add_argument(
            "--runtime-root", default=None,
            help="런타임 루트 디렉토리 override "
                 "(default: D:/work-github/.worker-control, "
                 "환경변수 WORKER_CONTROL_HOME 과 동일)",
        )

    # view serve — 기본 운영 경로 (SQLite BFF + 정적 FE)
    vs = vv_sub.add_parser(
        "serve",
        help="run the SQLite-backed dashboard BFF "
             "(serves the static FE at / and JSON at /api/snapshot)",
    )
    _add_serve_args(vs)
    vs.set_defaults(func=cmd_view_serve)

    # view html — 레거시 단일 파일 export
    vh = vv_sub.add_parser(
        "html",
        help="[legacy] write a single-file HTML snapshot "
             "(use `view serve` for the dynamic dashboard)",
    )
    vh.add_argument(
        "--legacy", action="store_true",
        help="레거시 export 라는 점을 명시적으로 인정한다 "
             "(이 플래그 없이는 거부됨)",
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

    # dashboard — view serve 의 짧은 별칭
    dsh = sub.add_parser(
        "dashboard",
        help="alias for `view serve` — SQLite-backed dashboard BFF",
    )
    _add_serve_args(dsh)
    dsh.set_defaults(func=cmd_dashboard)

    # dashboard-daemon — 상시 실행 + 자동 재시작 supervisor
    dd = sub.add_parser(
        "dashboard-daemon",
        help="run the dashboard BFF as a long-lived supervisor with "
             "auto-restart on code changes (Hermes autostart entry point)",
    )
    _add_serve_args(dd)
    dd.add_argument(
        "--watch", dest="watch", action="store_true", default=True,
        help="watch worker_control/*.py and static/*.html for changes and "
             "restart the BFF (default)",
    )
    dd.add_argument(
        "--no-watch", dest="watch", action="store_false",
        help="don't watch files — just ensure BFF is up and exit",
    )
    dd.add_argument(
        "--once", action="store_true",
        help="ensure-running probe only: if dashboard already healthy, "
             "exit 0; else spawn BFF in background and exit",
    )
    dd.add_argument(
        "--log", default=None,
        help="path for child BFF stdout/stderr "
             "(default: discard); recommended for cron/Hermes use",
    )
    dd.set_defaults(func=cmd_dashboard_daemon)

    # dashboard-snapshot — Telegram 으로 보낼 legacy snapshot 페이로드
    dsn = sub.add_parser(
        "dashboard-snapshot",
        help="emit a legacy static dashboard snapshot for Telegram "
             "(stdout = message + MEDIA:<path>; empty if no live session)",
    )
    dsn.add_argument(
        "--output", default=None,
        help="output HTML path "
             "(default: <runtime_root>/telegram-snapshot.html)",
    )
    dsn.add_argument(
        "--native-limit", type=int, default=200,
        help="native session discovery cap (default: 200)",
    )
    dsn.add_argument(
        "--no-native-liveness", action="store_true",
        help="ignore native sessions when deciding 'alive' — use only "
             "Hermes-spawned worker_sessions state",
    )
    dsn.set_defaults(func=cmd_dashboard_snapshot)

    # bootstrap + install-hermes-profile (one-shot host setup commands).
    # Kept in a sibling module so the schema-extension SQL + wrapper template
    # don't bloat this file.
    from worker_control import hermes_install
    hermes_install.add_subcommands(sub)

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
