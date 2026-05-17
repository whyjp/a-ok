"""Telegram 용 정적 dashboard snapshot.

Hermes cron 의 ``no_agent`` 모드에서 30 분에 한 번 호출된다. 사용자
정책:

* **세션이 살아있을 때만** 레거시 단일 파일 HTML 을 생성하고 stdout 에
  Telegram 으로 보낼 message + ``MEDIA:<path>`` 를 출력.
* 세션이 죽어 있으면 stdout 을 비워서 cron 이 조용히 지나가게.

"세션이 살아있다" 의 정의 (DB schema 기반):

* ``worker_sessions.state`` 가 다음 중 하나 — ``starting``, ``running``,
  ``working``, ``waiting_input``, ``blocked``.
* 또는 24 시간 이내에 ``native`` 세션이 발견됨 (heartbeat 대용).

native 디스커버리는 비싸므로 ``--include-native`` 로만 켠다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from worker_control import dashboard
from worker_control.db import session_scope, utcnow_iso
from worker_control.paths import runtime_root
from worker_control.native_sessions import discover_native_sessions


# state 어휘 (sessions.VALID_STATES) 중 "살아있다" 로 간주할 집합.
LIVE_STATES: frozenset[str] = frozenset({
    "starting", "running", "working", "waiting_input", "blocked",
})

# native 세션 mtime 이 이보다 최근이면 "살아있음" 으로 판단 (초).
NATIVE_HEARTBEAT_WINDOW_SEC: int = 24 * 60 * 60


@dataclass(slots=True)
class LiveCheck:
    alive: bool
    hermes_live: int
    native_recent: int
    detail: str


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # 'Z' 접미사를 timezone-aware 로 받아준다.
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1]).replace(
                tzinfo=timezone.utc,
            )
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def check_live(
    *,
    include_native: bool = False,
    native_limit: int | None = 200,
    now: datetime | None = None,
) -> LiveCheck:
    """세션이 살아있는지 판단."""
    now = now or datetime.now(timezone.utc)

    hermes_live = 0
    placeholders = ",".join(["?"] * len(LIVE_STATES))
    with session_scope() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM worker_sessions "
            f"WHERE state IN ({placeholders})",
            tuple(LIVE_STATES),
        ).fetchone()
        hermes_live = int(row["c"] if row else 0)

    native_recent = 0
    note_extra = ""
    if include_native:
        try:
            snap = discover_native_sessions(limit=native_limit)
        except Exception as exc:  # pragma: no cover - discovery best-effort
            snap = None
            note_extra = f" (native discovery error: {exc})"
        if snap is not None:
            for sess in snap.sessions:
                # NativeSession 은 mtime_utc 를 ISO 로 갖고 있음 (best-effort).
                mtime = _iso_to_dt(getattr(sess, "modified_at", None))
                if mtime is None:
                    continue
                age = (now - mtime).total_seconds()
                if 0 <= age <= NATIVE_HEARTBEAT_WINDOW_SEC:
                    native_recent += 1

    alive = hermes_live > 0 or native_recent > 0
    detail = (
        f"hermes_live={hermes_live} native_recent={native_recent}"
        f"{note_extra}"
    )
    return LiveCheck(
        alive=alive,
        hermes_live=hermes_live,
        native_recent=native_recent,
        detail=detail,
    )


def default_snapshot_path() -> Path:
    """레거시 snapshot HTML 의 기본 출력 경로."""
    return runtime_root() / "telegram-snapshot.html"


def build_snapshot(
    *,
    output: Path | None = None,
    native_limit: int | None = 200,
) -> Path:
    """현재 DB 로 legacy 단일 파일 HTML 을 작성하고 그 경로를 반환."""
    target = output or default_snapshot_path()
    return dashboard.write_dashboard(output=target, native_limit=native_limit)


def render_telegram_text(
    snap: dashboard.DashboardSnapshot,
    check: LiveCheck,
) -> str:
    c = snap.counters
    lines = [
        "📊 worker-control snapshot",
        f"· generated: {snap.generated_at}",
        f"· version:   {snap.version}",
        f"· db:        {snap.db_path}",
        "",
        "Hermes 세션:",
        f"  total={c.get('hermes_sessions', 0)} "
        f"running={c.get('hermes_running', 0)} "
        f"live={check.hermes_live}",
        "프로젝트:",
        f"  total={c.get('projects', 0)} "
        f"owned={c.get('projects_owned', 0)} "
        f"public={c.get('projects_public', 0)} "
        f"git={c.get('projects_git', 0)} "
        f"dirty={c.get('projects_dirty', 0)}",
        "Native:",
        f"  sessions={c.get('native_sessions', 0)} "
        f"recent_24h={check.native_recent}",
    ]
    return "\n".join(lines)


def emit_snapshot(
    *,
    output: Path | None = None,
    native_limit: int | None = 200,
    include_native_for_liveness: bool = True,
    stream: IO[str] | None = None,
) -> int:
    """cron 진입점.

    살아있는 세션이 있으면 stdout 에 Telegram 메시지 + ``MEDIA:<path>`` 출력.
    없으면 stdout 비어있고 종료 코드 0.

    Returns
    -------
    int
        cron 종료 코드. 항상 0 (정상). 에러는 stderr 로만 보낸다.
    """
    out = stream or sys.stdout
    check = check_live(
        include_native=include_native_for_liveness,
        native_limit=native_limit,
    )
    if not check.alive:
        # silent — Hermes cron no_agent 가 빈 stdout 을 무시하도록.
        print(
            f"[snapshot] no live session ({check.detail}); skipping",
            file=sys.stderr,
        )
        return 0

    try:
        snap = dashboard.collect_snapshot(native_limit=native_limit)
        html_path = build_snapshot(output=output, native_limit=native_limit)
    except Exception as exc:
        print(f"[snapshot] failed to build: {exc}", file=sys.stderr)
        return 0  # cron 은 조용히 — 사용자에게 spam 하지 않는다

    text = render_telegram_text(snap, check)
    print(text, file=out)
    print(f"MEDIA:{html_path}", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import argparse
    p = argparse.ArgumentParser(prog="worker_control.snapshot")
    p.add_argument("--output", default=None,
                   help=f"output HTML path (default: {default_snapshot_path()})")
    p.add_argument("--native-limit", type=int, default=200)
    p.add_argument(
        "--no-native-liveness", action="store_true",
        help="native 세션을 liveness 판단에서 제외 (Hermes 세션만으로 판단)",
    )
    args = p.parse_args()
    sys.exit(emit_snapshot(
        output=Path(args.output) if args.output else None,
        native_limit=args.native_limit,
        include_native_for_liveness=not args.no_native_liveness,
    ))
