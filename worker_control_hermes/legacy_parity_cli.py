"""CLI shims for the legacy-parity ingest pass.

The main entry path is heartbeat ticks (auto), but operators sometimes
want a manual `ingest now` button. This thin wrapper exposes one.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from .legacy_parity_ingest import ingest_all


def ingest_cmd(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="workerctl-hermes-parity-ingest")
    p.add_argument("--db", default=os.environ.get(
        "WORKER_PROJECTS_DB", r"D:/work-github/.worker-control/worker-control.sqlite3"))
    p.add_argument("--force", action="store_true",
                   help="ignore transcript_mtime watermarks and re-parse everything")
    args = p.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    stats = ingest_all(conn, force=args.force)
    conn.close()
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(ingest_cmd())
