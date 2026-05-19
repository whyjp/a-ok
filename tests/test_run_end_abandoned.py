"""`run end --status abandoned` parity with `session end`.

`cmd_run_end` already writes `args.status` unconditionally to
`hermes_runs.status`. The only missing piece was the argparse `choices=`
list rejecting `'abandoned'`, which forced operators to drop to raw SQL
when marking a single dry-allocated dispatcher run as abandoned (e.g. a
verification artifact from the dup-guard preflight that was never
executed). This test pins both the happy path and the argparse rejection.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


_MIN_SCHEMA = """
CREATE TABLE worker_profiles (id INTEGER PRIMARY KEY);
CREATE TABLE hermes_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    ended_at TEXT,
    last_used_at TEXT NOT NULL
);
CREATE TABLE hermes_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    note TEXT
);
INSERT INTO hermes_sessions(uuid, name, status, last_used_at)
    VALUES ('aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'a-ok:abandoned-test', 'active',
            '2026-01-01T00:00:00+00:00');
INSERT INTO hermes_runs(session_id, name, status, started_at)
    VALUES (1, 'a-ok:abandoned-test-r1', 'started',
            '2026-01-01T00:00:00+00:00');
"""


def _build_ledger(tmp_path: Path) -> tuple[Path, int]:
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_MIN_SCHEMA)
    conn.commit()
    run_id = conn.execute("SELECT id FROM hermes_runs LIMIT 1").fetchone()[0]
    conn.close()
    return db, int(run_id)


def test_cmd_run_end_abandoned_writes_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct call to cmd_run_end with status='abandoned' flips the row
    and stamps ended_at; the spawn auto-promote (done-only) is skipped."""
    from worker_control_hermes import projects as projects_mod

    db, run_id = _build_ledger(tmp_path)
    monkeypatch.setattr(projects_mod, "DB_PATH", db)

    projects_mod.cmd_run_end(argparse.Namespace(
        run_id=str(run_id),
        status="abandoned",
        note="manual abandon (test)",
    ))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM hermes_runs WHERE id=?", (run_id,)
    ).fetchone()
    sess = conn.execute(
        "SELECT status FROM hermes_sessions WHERE id=?", (row["session_id"],)
    ).fetchone()
    conn.close()

    assert row["status"] == "abandoned"
    assert row["ended_at"] is not None
    assert row["note"] == "manual abandon (test)"
    # Session must remain 'active' — auto-promote only fires on done.
    assert sess["status"] == "active"


def test_run_end_rejects_unknown_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """argparse choices= must still reject statuses outside the allowed set."""
    db, run_id = _build_ledger(tmp_path)
    env = {
        **__import__("os").environ,
        "WORKER_PROJECTS_DB": str(db),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "worker_control_hermes.projects",
         "run", "end", str(run_id), "--status", "nonsense"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "invalid choice" in (proc.stderr + proc.stdout).lower()
