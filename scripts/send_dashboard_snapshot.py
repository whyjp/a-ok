#!/usr/bin/env python
"""Hermes cron 용 wrapper — 30 분 주기 Telegram snapshot.

``workerctl dashboard-snapshot`` 의 얇은 launcher. 살아있는 세션이 있으면
stdout 에 메시지 + ``MEDIA:<path>`` 한 줄을 출력해 Hermes ``no_agent`` cron
이 그대로 Telegram 으로 전달하게 한다. 죽어 있으면 stdout 을 비워서 cron
이 조용히 넘어간다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from worker_control import snapshot  # noqa: E402


def main() -> int:
    output_env = os.environ.get("WORKER_CONTROL_SNAPSHOT_OUTPUT")
    output = Path(output_env) if output_env else None
    try:
        native_limit = int(
            os.environ.get("WORKER_CONTROL_SNAPSHOT_NATIVE_LIMIT", "200")
        )
    except ValueError:
        native_limit = 200
    skip_native = os.environ.get(
        "WORKER_CONTROL_SNAPSHOT_NO_NATIVE_LIVENESS", "0",
    ) not in ("0", "false", "False", "")

    return snapshot.emit_snapshot(
        output=output,
        native_limit=native_limit,
        include_native_for_liveness=not skip_native,
    )


if __name__ == "__main__":
    raise SystemExit(main())
