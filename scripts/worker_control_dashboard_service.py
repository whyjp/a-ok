#!/usr/bin/env python
"""Hermes 시작 시 자동 실행되는 worker-control 동적 대시보드 wrapper.

이 스크립트는 ``workerctl dashboard-daemon`` 의 얇은 launcher 다. Hermes
config 에서 직접 부르기 쉽도록 절대경로 import 만 사용하고, 옵션은 환경
변수로 받는다.

환경변수 (선택):

* ``WORKER_CONTROL_DASHBOARD_HOST``  (기본 ``127.0.0.1``)
* ``WORKER_CONTROL_DASHBOARD_PORT``  (기본 ``8765``)
* ``WORKER_CONTROL_DB``              — BFF 가 읽을 SQLite 경로
* ``WORKER_CONTROL_HOME``            — runtime root (기본 ``D:/work-github/.worker-control``)
* ``WORKER_CONTROL_DASHBOARD_LOG``   — 자식 BFF stdout/stderr 로그 파일
* ``WORKER_CONTROL_DASHBOARD_ONCE``  — ``1`` 이면 supervisor 없이 ensure-running 후 종료

사용 예 (Git-Bash):

    python D:/work-github/worker-control/scripts/worker_control_dashboard_service.py

또는 ``--once`` 만 켜고 백그라운드로 detach 하고 싶다면:

    WORKER_CONTROL_DASHBOARD_ONCE=1 python .../worker_control_dashboard_service.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# repo 내부 실행도 가능하도록 패키지 경로 보강.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from worker_control import autostart  # noqa: E402


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def main() -> int:
    host = _env("WORKER_CONTROL_DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(_env("WORKER_CONTROL_DASHBOARD_PORT", "8765") or 8765)
    log_env = _env("WORKER_CONTROL_DASHBOARD_LOG")
    log_path = Path(log_env) if log_env else None
    once = (_env("WORKER_CONTROL_DASHBOARD_ONCE", "0") or "0") not in (
        "0", "false", "False", "",
    )

    if once:
        res = autostart.ensure_running(
            host=host, port=port, log_path=log_path,
        )
        print(res.note, flush=True)
        return 0 if res.health is not None else 1

    return autostart.run_supervisor(
        host=host, port=port, log_path=log_path,
        on_event=lambda k, m: print(f"[autostart:{k}] {m}", flush=True),
    )


if __name__ == "__main__":
    raise SystemExit(main())
