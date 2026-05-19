"""Preflight + dup-guard + orphan-sweep tests for cmd_session_start (PRD P0).

Spec: docs/intent-and-prd-orphan-dedup.md §1 §2 §3 §4.

These cover the INSERT-axis of the orphan-dedup fix:

* ``§1`` — bash -n preflight runs inside the same transaction as the
  ``hermes_sessions`` / ``hermes_runs`` INSERT, so a syntax error in
  the emitted command rolls back BOTH rows.
* ``§2`` — ``--extra-flags`` get spliced INSIDE the trap subshell, not
  appended after the closing ``)`` — the wrapped command must still
  pass ``bash -n``.
* ``§3`` — ``cmd_session_start`` with a colliding ``(project, name)``
  inside the 60-s window aborts with exit-2 + JSON body carrying
  ``existing_uuid``. Beyond the window, the old row gets transitioned
  to ``abandoned`` and the new INSERT proceeds.
* ``§4`` — ``runs sweep --orphan-stale --max-age N`` transitions only
  the rows older than N seconds.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


_CANONICAL_SCHEMA = """
CREATE TABLE worker_profiles (id INTEGER PRIMARY KEY);

CREATE TABLE hermes_projects_v (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path   TEXT NOT NULL UNIQUE,
    project_type  TEXT,
    git_repo      TEXT,
    display_name  TEXT,
    description   TEXT,
    learned_notes TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    last_used_at  TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE hermes_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    project_id      INTEGER NOT NULL,
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
    ended_at        TEXT
);

CREATE TABLE hermes_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL,
    run_index         INTEGER NOT NULL,
    name              TEXT NOT NULL,
    mode              TEXT NOT NULL,
    command           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'started',
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    note              TEXT,
    hermes_session_id TEXT
);
"""


def _setup_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, int]:
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_CANONICAL_SCHEMA)
    proj_path = str((tmp_path / "proj").resolve())
    (tmp_path / "proj").mkdir()
    conn.execute(
        "INSERT INTO hermes_projects_v(folder_path, display_name, created_at, last_used_at) "
        "VALUES (?, 'proj', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (proj_path,),
    )
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    from worker_control_hermes import projects as projects_mod
    monkeypatch.setattr(projects_mod, "DB_PATH", db)
    # Disable hsid stamping — _stamp_hsid() reads the real ~/AppData dir.
    monkeypatch.setattr(projects_mod, "_stamp_hsid", lambda: None)
    return db, proj_path, int(pid)


def _start_args(
    project: str,
    *,
    uuid: str,
    name: str | None = None,
    brief: str = "hi",
    extra_flags: str = "",
    no_auto_close: bool = False,
    json_out: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        project=project,
        uuid=uuid,
        name=name,
        brief=brief,
        model=None,
        permission_mode=None,
        print=True,
        prompt="do a thing",
        max_turns=None,
        allowed_tools=None,
        no_auto_close=no_auto_close,
        extra_flags=extra_flags,
        json=json_out,
    )


# ---------------------------------------------------------------------------
# §1 dry-allocation preflight
# ---------------------------------------------------------------------------


def test_preflight_failure_rolls_back_all_inserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken raw_cmd → bash -n fails → no rows land in either table."""
    if shutil.which("bash") is None:
        pytest.skip("bash unavailable; preflight test requires bash on PATH")

    from worker_control_hermes import projects as projects_mod

    db, proj_path, _pid = _setup_ledger(tmp_path, monkeypatch)

    # Inject a deliberately broken raw_cmd by monkey-patching
    # _build_claude_command to return shell-syntax-garbage. The preflight
    # call must surface this and roll back the surrounding transaction.
    def busted(**_kw):
        return "claude -p 'unterminated"  # missing closing quote → bash -n fails

    monkeypatch.setattr(projects_mod, "_build_claude_command", busted)

    uid = "deadbeef-0000-0000-0000-000000000001"
    with pytest.raises(SystemExit) as excinfo:
        projects_mod.cmd_session_start(_start_args(proj_path, uuid=uid))
    assert "preflight failed" in str(excinfo.value)

    conn = sqlite3.connect(db)
    sess_n = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE uuid=?", (uid,)
    ).fetchone()[0]
    runs_n = conn.execute("SELECT COUNT(*) FROM hermes_runs").fetchone()[0]
    conn.close()
    assert sess_n == 0, "hermes_sessions row must be rolled back"
    assert runs_n == 0, "hermes_runs row must be rolled back"


def test_preflight_pass_keeps_inserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: normal (well-formed) command passes preflight and rows land."""
    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")
    from worker_control_hermes import projects as projects_mod

    db, proj_path, _pid = _setup_ledger(tmp_path, monkeypatch)

    uid = "deadbeef-0000-0000-0000-000000000002"
    projects_mod.cmd_session_start(_start_args(proj_path, uuid=uid))

    conn = sqlite3.connect(db)
    sess_n = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE uuid=?", (uid,)
    ).fetchone()[0]
    runs_n = conn.execute("SELECT COUNT(*) FROM hermes_runs").fetchone()[0]
    conn.close()
    assert sess_n == 1
    assert runs_n == 1


# ---------------------------------------------------------------------------
# §2 _wrap_self_close extra_flags
# ---------------------------------------------------------------------------


def test_wrap_self_close_extra_flags_default_empty() -> None:
    """Default behaviour: no extra_flags → output matches the legacy wrap."""
    from worker_control_hermes.projects import _wrap_self_close

    wrapped = _wrap_self_close("claude -p 'hi'", 42)
    legacy = _wrap_self_close("claude -p 'hi'", 42, extra_flags="")
    assert wrapped == legacy


def test_wrap_self_close_extra_flags_inside_subshell() -> None:
    """The extra_flags string must land BEFORE the closing ``)``."""
    from worker_control_hermes.projects import _wrap_self_close

    wrapped = _wrap_self_close(
        "claude -p 'hi' --session-id 11111111-1111-1111-1111-111111111111",
        7,
        extra_flags="--output-format json",
    )
    assert wrapped.startswith("(")
    assert wrapped.endswith(")")
    inside = wrapped[1:-1]
    # both the raw command and the extra flag live in the same subshell
    assert "claude -p 'hi'" in inside
    assert "--output-format json" in inside
    # the extra flag is NOT appended outside the closing paren
    assert ") --output-format json" not in wrapped


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for bash -n preflight")
def test_wrap_self_close_extra_flags_passes_bash_n() -> None:
    """The emitted command with extra_flags must parse under ``bash -n``."""
    from worker_control_hermes.projects import _wrap_self_close

    raw = ("cd '/tmp/proj' && claude -p 'do a thing' "
           "--session-id 11111111-1111-1111-1111-111111111111 "
           "--name 'a-ok:session-r1'")
    cmd = _wrap_self_close(raw, 99, extra_flags="--output-format json --verbose")
    proc = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"bash -n rejected the wrapped command:\nstderr={proc.stderr!r}\ncmd={cmd!r}"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for bash -n preflight")
def test_session_start_with_extra_flags_passes_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """End-to-end: ``cmd_session_start --extra-flags '...'`` lands rows."""
    from worker_control_hermes import projects as projects_mod

    db, proj_path, _pid = _setup_ledger(tmp_path, monkeypatch)

    uid = "deadbeef-0000-0000-0000-000000000003"
    projects_mod.cmd_session_start(
        _start_args(proj_path, uuid=uid, extra_flags="--output-format json")
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["uuid"] == uid
    # the emitted command must contain the extra flag INSIDE the wrap
    assert "--output-format json )" in payload["command"] or \
        "--output-format json " in payload["command"].split(")")[0]


# ---------------------------------------------------------------------------
# §3 duplicate-name guard
# ---------------------------------------------------------------------------


def test_dup_within_window_returns_exit2_and_existing_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")
    from worker_control_hermes import projects as projects_mod

    db, proj_path, _pid = _setup_ledger(tmp_path, monkeypatch)

    uid1 = "deadbeef-0000-0000-0000-aaaaaaaaaaaa"
    uid2 = "deadbeef-0000-0000-0000-bbbbbbbbbbbb"
    name = "a-ok:dup-test"

    # First call: succeeds.
    projects_mod.cmd_session_start(
        _start_args(proj_path, uuid=uid1, name=name, json_out=False)
    )
    # Drain first-call output so capsys only carries the second call.
    capsys.readouterr()
    # Second call moments later: must be refused with exit 2.
    with pytest.raises(SystemExit) as excinfo:
        projects_mod.cmd_session_start(
            _start_args(proj_path, uuid=uid2, name=name, json_out=True)
        )
    assert excinfo.value.code == 2

    captured = capsys.readouterr()
    assert "duplicate within" in captured.err
    body = json.loads(captured.out)
    assert body["error"] == "duplicate"
    assert body["existing_uuid"] == uid1
    assert isinstance(body["existing_run_id"], int)

    # The second uid must NOT have landed.
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM hermes_sessions WHERE uuid=?", (uid2,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_dup_outside_window_transitions_old_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash unavailable")
    from worker_control_hermes import projects as projects_mod

    db, proj_path, pid = _setup_ledger(tmp_path, monkeypatch)

    uid1 = "deadbeef-0000-0000-0000-cccccccccccc"
    uid2 = "deadbeef-0000-0000-0000-dddddddddddd"
    name = "a-ok:windowed-test"

    projects_mod.cmd_session_start(
        _start_args(proj_path, uuid=uid1, name=name, json_out=False)
    )

    # Age the first row + its run beyond 60s.
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120))\
        .isoformat(timespec="seconds")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE hermes_sessions SET last_used_at=?, created_at=? WHERE uuid=?",
        (old, old, uid1),
    )
    conn.execute("UPDATE hermes_runs SET started_at=? WHERE name LIKE 'a-ok:windowed-test%'", (old,))
    conn.commit()
    conn.close()

    # Now the second call must proceed AND the old row must be abandoned.
    projects_mod.cmd_session_start(
        _start_args(proj_path, uuid=uid2, name=name, json_out=False)
    )

    conn = sqlite3.connect(db)
    row1 = conn.execute(
        "SELECT status FROM hermes_sessions WHERE uuid=?", (uid1,)
    ).fetchone()
    row2 = conn.execute(
        "SELECT status FROM hermes_sessions WHERE uuid=?", (uid2,)
    ).fetchone()
    run1_status = conn.execute(
        "SELECT status FROM hermes_runs WHERE session_id=("
        "SELECT id FROM hermes_sessions WHERE uuid=?)",
        (uid1,),
    ).fetchone()
    conn.close()
    assert row1[0] == "abandoned"
    assert row2[0] == "active"
    assert run1_status[0] == "abandoned"


# ---------------------------------------------------------------------------
# §4 runs sweep --orphan-stale
# ---------------------------------------------------------------------------


def test_runs_sweep_orphan_stale_only_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """2 fresh + 2 old started rows → only the 2 old ones transition."""
    from worker_control_hermes import projects as projects_mod

    db, proj_path, pid = _setup_ledger(tmp_path, monkeypatch)

    # Insert a single session and 4 runs against it with mixed ages.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO hermes_sessions(uuid, project_id, name, created_at, last_used_at) "
        "VALUES ('11111111-1111-1111-1111-111111111111', ?, 'a-ok:sweepy', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (pid,),
    )
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    old_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=300))\
        .isoformat(timespec="seconds")
    for idx, (st, label) in enumerate(
        [(now_iso, "fresh-A"), (now_iso, "fresh-B"), (old_iso, "old-A"), (old_iso, "old-B")],
        start=1,
    ):
        conn.execute(
            "INSERT INTO hermes_runs(session_id, run_index, name, mode, command, "
            "status, started_at) VALUES (?, ?, ?, 'print', 'claude -p hi', 'started', ?)",
            (sid, idx, f"a-ok:sweepy-{label}", st),
        )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        max_age_hours=24.0,  # default — not used in orphan mode
        orphan_stale=True,
        max_age=60,
        dry_run=False,
        json=False,
    )
    projects_mod.cmd_runs_sweep(args)
    out = capsys.readouterr().out
    assert "swept 2 run(s) → abandoned" in out

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, status, note FROM hermes_runs ORDER BY id"
    ).fetchall()
    conn.close()
    by_name = {r["name"]: dict(r) for r in rows}
    assert by_name["a-ok:sweepy-fresh-A"]["status"] == "started"
    assert by_name["a-ok:sweepy-fresh-B"]["status"] == "started"
    assert by_name["a-ok:sweepy-old-A"]["status"] == "abandoned"
    assert by_name["a-ok:sweepy-old-B"]["status"] == "abandoned"
    # The note must record the age cutoff so audit trails can tell which
    # sweep invocation closed it.
    assert by_name["a-ok:sweepy-old-A"]["note"] == "orphan-sweep age>60s"


def test_runs_sweep_orphan_stale_dry_run_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """--dry-run lists the candidates without writing."""
    from worker_control_hermes import projects as projects_mod

    db, proj_path, pid = _setup_ledger(tmp_path, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO hermes_sessions(uuid, project_id, name, created_at, last_used_at) "
        "VALUES ('22222222-2222-2222-2222-222222222222', ?, 'a-ok:dry-sweep', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (pid,),
    )
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    old_iso = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=900))\
        .isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO hermes_runs(session_id, run_index, name, mode, command, "
        "status, started_at) VALUES (?, 1, 'a-ok:dry-sweep-r1', 'print', "
        "'claude -p hi', 'started', ?)",
        (sid, old_iso),
    )
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        max_age_hours=24.0,
        orphan_stale=True,
        max_age=60,
        dry_run=True,
        json=False,
    )
    projects_mod.cmd_runs_sweep(args)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out

    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM hermes_runs WHERE name='a-ok:dry-sweep-r1'"
    ).fetchone()[0]
    conn.close()
    assert status == "started", "dry-run must not write"
