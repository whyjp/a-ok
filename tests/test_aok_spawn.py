"""Tests for worker_control_hermes.aok_spawn (PR: feat/aok-spawn-wrapper).

Coverage matrix:
  * pure helpers (PATH bootstrap, trap detection, trap wrap) — no subprocess
  * argparse contract (conflicting inputs reject)
  * subprocess plumbing (DEVNULL stdin on the child)
  * end-to-end ledger close (skipped when bash or workerctl-hermes-projects
    aren't on PATH so the suite stays green on minimal CI images)
  * trap body parity with projects._wrap_self_close — locks in that both
    helpers produce structurally identical EXIT traps so we can't drift
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from worker_control_hermes import aok_spawn
from worker_control_hermes.projects import _wrap_self_close


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_path_bootstrap_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    venv_bin = str(aok_spawn._venv_bin_dir())

    # 1) Fresh env without the bin dir → prepended.
    env: dict[str, str] = {"PATH": "/usr/bin:/usr/local/bin"}
    assert aok_spawn.ensure_path_bootstrap(env) is True
    assert env["PATH"].split(os.pathsep)[0] == venv_bin

    # 2) Re-running on the same env is a no-op.
    assert aok_spawn.ensure_path_bootstrap(env) is False
    assert env["PATH"].split(os.pathsep)[0] == venv_bin
    assert env["PATH"].count(venv_bin) == 1

    # 3) Empty PATH still works (no trailing separator).
    env2: dict[str, str] = {"PATH": ""}
    assert aok_spawn.ensure_path_bootstrap(env2) is True
    assert env2["PATH"] == venv_bin


def test_double_trap_detection() -> None:
    wrapped = "( trap 'echo hi' EXIT; cd /tmp && claude -p 'do it' )"
    assert aok_spawn.is_already_wrapped(wrapped) is True
    # Re-wrapping should NOT touch the string.
    assert aok_spawn.wrap_with_runend_trap(wrapped, run_id=42) == wrapped


def test_double_trap_detection_with_leading_whitespace() -> None:
    wrapped = "  (  trap 'x' EXIT; echo hi )"
    assert aok_spawn.is_already_wrapped(wrapped) is True


def test_wrap_when_missing() -> None:
    raw = "cd /tmp && claude -p 'hello'"
    out = aok_spawn.wrap_with_runend_trap(raw, run_id=99)
    assert out.startswith("( trap '")
    assert "workerctl-hermes-projects run end 99" in out
    assert raw in out


def test_trap_body_matches_projects() -> None:
    """Parity gate: our wrap_with_runend_trap must produce the SAME trap
    body as worker_control_hermes.projects._wrap_self_close — drifting
    would defeat the whole point of this CLI.
    """
    raw_cmd = "cd /tmp && claude -p 'parity'"
    run_id = 7
    ours = aok_spawn.wrap_with_runend_trap(raw_cmd, run_id)
    theirs = _wrap_self_close(raw_cmd, run_id)
    assert ours == theirs


# ---------------------------------------------------------------------------
# argparse contract
# ---------------------------------------------------------------------------


def test_conflicting_inputs_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "cmd.sh"
    f.write_text("echo hi", encoding="utf-8")
    with pytest.raises(SystemExit):
        aok_spawn.main([
            "--run-id", "1",
            "--cmd-file", str(f),
            "--inline-cmd", "echo other",
        ])


def test_conflicting_inputs_neither() -> None:
    with pytest.raises(SystemExit):
        aok_spawn.main(["--run-id", "1"])


def test_cmd_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        aok_spawn.main([
            "--run-id", "1",
            "--cmd-file", str(tmp_path / "does-not-exist.sh"),
        ])


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def test_devnull_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Foreground runs must close the child's stdin so claude --print
    doesn't deadlock waiting for stdin data.
    """
    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

    def fake_run(cmd, **kwargs):  # noqa: ANN001 — test shim
        captured["cmd"] = cmd
        captured["stdin"] = kwargs.get("stdin")
        captured["stdout_is_file"] = hasattr(kwargs.get("stdout"), "write")
        captured["stderr"] = kwargs.get("stderr")
        return _FakeProc()

    monkeypatch.setattr(aok_spawn.subprocess, "run", fake_run)
    log = tmp_path / "x.log"
    rc = aok_spawn.main([
        "--run-id", "5",
        "--inline-cmd", "true",
        "--log", str(log),
    ])
    assert rc == 0
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout_is_file"] is True
    # Stderr is folded into stdout so single-stream log is complete.
    assert captured["stderr"] is subprocess.STDOUT
    # The wrapped command actually went to bash -c.
    cmd = captured["cmd"]
    assert cmd[0] == "bash" and cmd[1] == "-c"
    assert "workerctl-hermes-projects run end 5" in cmd[2]


def test_no_trap_skips_wrap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(aok_spawn.subprocess, "run", fake_run)
    aok_spawn.main([
        "--run-id", "8",
        "--inline-cmd", "echo bare",
        "--no-trap",
        "--log", str(tmp_path / "n.log"),
    ])
    cmd = captured["cmd"]
    assert cmd[2] == "echo bare"


# ---------------------------------------------------------------------------
# End-to-end ledger close (integration)
# ---------------------------------------------------------------------------


def _have_e2e_tools() -> tuple[bool, str]:
    if shutil.which("bash") is None:
        return False, "bash not on PATH"
    if shutil.which("workerctl-hermes-projects") is None:
        return False, "workerctl-hermes-projects not installed (run pip install -e .)"
    return True, ""


@pytest.mark.parametrize("inline_cmd,expected_status", [
    ("exit 0", "done"),
    ("exit 1", "failed"),
])
def test_subprocess_run_records_exit_in_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inline_cmd: str,
    expected_status: str,
) -> None:
    """Spawn aok-spawn against a tmp ledger DB and verify the EXIT trap
    actually closes the run row.

    Skips automatically on machines that don't have bash or the
    workerctl-hermes-projects entry point on PATH (e.g. CI without the
    package installed). The hermes_runs row is set up with the minimum
    columns ``cmd_run_end`` needs to UPDATE.
    """
    ok, reason = _have_e2e_tools()
    if not ok:
        pytest.skip(reason)

    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    # ``worker_profiles`` marker tells projects._connect() to skip the
    # legacy SCHEMA bootstrap (which would try to ALTER a view).
    conn.executescript("""
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
            VALUES ('11111111-2222-3333-4444-555555555555',
                    'a-ok:test-session', 'active', '2026-01-01T00:00:00+00:00');
        INSERT INTO hermes_runs(session_id, name, status, started_at)
            VALUES (1, 'a-ok:test-session-r1', 'started', '2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    run_id = conn.execute("SELECT id FROM hermes_runs LIMIT 1").fetchone()[0]
    conn.close()

    env = os.environ.copy()
    env["WORKER_PROJECTS_DB"] = str(db)
    # The PATH from the test process already has workerctl-hermes-projects
    # (we checked above), so the trap call will resolve. No need to inject
    # the venv bin — aok-spawn does that anyway.
    log = tmp_path / "spawn.log"
    proc = subprocess.run(
        [sys.executable, "-m", "worker_control_hermes.aok_spawn",
         "--run-id", str(run_id),
         "--inline-cmd", inline_cmd,
         "--log", str(log)],
        env=env,
        capture_output=True,
        text=True,
    )
    # Foreground propagates exit code: 0 for "exit 0", 1 for "exit 1".
    assert proc.returncode == (0 if expected_status == "done" else 1), (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r} log={log.read_text() if log.exists() else '(no log)'}"
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM hermes_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    assert row["status"] == expected_status, (
        f"row={dict(row)} log={log.read_text() if log.exists() else '(no log)'}"
    )
    assert row["ended_at"] is not None
    # cmd_run_end stores the --note verbatim. trap emits exit=<rc>.
    assert (row["note"] or "").startswith(
        "exit=0" if expected_status == "done" else "exit=1"
    )


def test_module_entry_point_help() -> None:
    """`python -m worker_control_hermes.aok_spawn --help` must print
    usage (smoke test that the entry point doesn't import-error).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "worker_control_hermes.aok_spawn", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "aok-spawn" in proc.stdout
    assert "--run-id" in proc.stdout
