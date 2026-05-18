"""worker_control_hermes — Hermes-profile-side companions to worker_control.

The companions are split out from the core ``worker_control`` package
because they encode Hermes Agent integration choices that don't belong in
the workerctl base (claude-code dispatch, Slack heartbeat, iShare-bundled
report, etc.). They share the same canonical SQLite DB (managed by
``worker_control``) — see ``worker_control.hermes_ledger`` for the
read-side adapter the dashboard uses.

A user who clones this repo and points a Hermes worker profile at it gets
all of: project registry, run dispatcher, heartbeat, report builder, and
subprocess tracker — installed as ``workerctl-hermes-*`` console scripts
plus a thin wrapper layer under ``~/AppData/Local/hermes/profiles/<name>/
scripts/`` for backward compatibility with hardcoded SOUL.md paths.

Public entry points (console scripts in pyproject.toml):
    workerctl-hermes-projects   = worker_control_hermes.projects:main
    workerctl-hermes-heartbeat  = worker_control_hermes.heartbeat:main
    workerctl-hermes-report     = worker_control_hermes.build_report:main
    workerctl-hermes-subprocs   = worker_control_hermes.subprocs:main
    workerctl-hermes-migrate    = worker_control_hermes.migrate_to_canonical_db:main
"""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SQL_DIR = PACKAGE_DIR
