"""Unit tests for the spawn_backfill module + heartbeat integration smoke."""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

import pytest

from worker_control_hermes import spawn_backfill
from worker_control_hermes.spawn_backfill import (
    backfill_all,
    backfill_session,
    scan_transcript_for_spawns,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL
);

CREATE TABLE hermes_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    origin          TEXT NOT NULL DEFAULT 'native',
    created_at      TEXT NOT NULL,
    last_used_at    TEXT NOT NULL,
    ended_at        TEXT
);

CREATE TABLE hermes_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES hermes_sessions(id) ON DELETE CASCADE,
    run_index    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    mode         TEXT NOT NULL,
    command      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'started',
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    note         TEXT,
    hermes_session_id TEXT,
    UNIQUE(session_id, run_index)
);

CREATE TABLE hermes_agent_sessions (
    hermes_session_id TEXT PRIMARY KEY,
    profile_name      TEXT,
    profile_path      TEXT,
    transcript_path   TEXT,
    transcript_size   INTEGER NOT NULL DEFAULT 0,
    transcript_mtime  TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    synced_at         TEXT NOT NULL,
    ai_title          TEXT,
    first_user_text   TEXT,
    turn_count        INTEGER,
    msg_user          INTEGER
);
"""


def _insert_agent_session(
    conn: sqlite3.Connection,
    hsid: str,
    *,
    ghost: bool,
    transcript_path: str = "",
) -> None:
    if ghost:
        conn.execute(
            "INSERT INTO hermes_agent_sessions "
            "(hermes_session_id, profile_name, transcript_path, transcript_mtime, "
            " synced_at, ai_title, first_user_text, turn_count, msg_user) "
            "VALUES (?, 'default', ?, ?, ?, NULL, NULL, 1, 0)",
            (hsid, transcript_path, _old_iso(1), _now_iso()),
        )
    else:
        conn.execute(
            "INSERT INTO hermes_agent_sessions "
            "(hermes_session_id, profile_name, transcript_path, transcript_mtime, "
            " synced_at, ai_title, first_user_text, turn_count, msg_user) "
            "VALUES (?, 'default', ?, ?, ?, 'real PM', 'do the thing', 7, 4)",
            (hsid, transcript_path, _old_iso(1), _now_iso()),
        )


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (1, 'demo', '/tmp/demo')"
    )
    conn.commit()
    return conn


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _old_iso(hours: float) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    ).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def test_scan_finds_slug_and_closure(tmp_path: Path) -> None:
    txt = (
        "Starting up...\n"
        "$ aok-spawn --run-id 99 --inline-cmd ...\n"
        "running a-ok:fix-the-thing-r1 ...\n"
        "claude --print started\n"
        "[run-end] exit=0\n"
        "done.\n"
    )
    p = tmp_path / "transcript.json"
    p.write_text(txt, encoding="utf-8")

    found = scan_transcript_for_spawns(str(p))
    assert len(found) == 1
    e = found[0]
    assert e["slug"] == "a-ok:fix-the-thing-r1"
    assert e["closure_seen"] is True
    assert e["inferred_status"] == "done"


def test_scan_finds_slug_only(tmp_path: Path) -> None:
    txt = (
        "$ claude -p ...\n"
        "spawning a-ok:half-done-r1 ...\n"
        "(no exit marker — looks like the trap silently failed)\n"
    )
    p = tmp_path / "transcript.json"
    p.write_text(txt, encoding="utf-8")

    found = scan_transcript_for_spawns(str(p))
    assert len(found) == 1
    assert found[0]["closure_seen"] is False
    assert found[0]["inferred_status"] == "unknown"


def test_scan_handles_non_zero_exit(tmp_path: Path) -> None:
    txt = (
        "spawn a-ok:boom-r1\n"
        "aok-spawn fired\n"
        "...\n"
        "exit=137\n"
    )
    p = tmp_path / "transcript.json"
    p.write_text(txt, encoding="utf-8")

    found = scan_transcript_for_spawns(str(p))
    assert found[0]["inferred_status"] == "failed"
    assert found[0]["closure_seen"] is True


def test_scan_empty_when_no_signature(tmp_path: Path) -> None:
    p = tmp_path / "transcript.json"
    p.write_text("nothing interesting at all here\n", encoding="utf-8")
    assert scan_transcript_for_spawns(str(p)) == []


# ---------------------------------------------------------------------------
# backfill_session — UPDATE path
# ---------------------------------------------------------------------------

def test_backfill_updates_started_row(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_aaaaaa"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (10, "uuid-child-10", "a-ok:stale", _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, hermes_session_id, note) "
        "VALUES (10, 1, 'a-ok:stale-r1', 'print', '', 'started', ?, ?, "
        "'pre-existing context')",
        (_old_iso(3), parent_id),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    # exit=0 close marker triggers the 'done' inference.
    transcript.write_text(
        "spawn a-ok:stale-r1 ...\nlots of work ...\nexit=0\n",
        encoding="utf-8",
    )
    transcript_mtime = _old_iso(2)

    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=transcript_mtime,
    )

    assert stats["updated"] == 1
    assert stats["inserted"] == 0
    row = conn.execute(
        "SELECT status, ended_at, note FROM hermes_runs WHERE name='a-ok:stale-r1'"
    ).fetchone()
    assert row["status"] == "done"
    assert row["ended_at"] == transcript_mtime
    assert "backfill" in (row["note"] or "")
    assert "pre-existing context" in (row["note"] or "")


def test_backfill_started_stale_no_closure_marks_failed(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_bbbbbb"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (20, "uuid-child-20", "a-ok:abandoned", _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, hermes_session_id) "
        "VALUES (20, 1, 'a-ok:abandoned-r1', 'print', '', 'started', ?, ?)",
        (_old_iso(10), parent_id),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    # No exit marker; transcript_mtime > 60 min old → falls into stale branch.
    transcript.write_text("spawn a-ok:abandoned-r1 ...\n", encoding="utf-8")

    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=_old_iso(5),
    )
    assert stats["updated"] == 1
    row = conn.execute(
        "SELECT status FROM hermes_runs WHERE name='a-ok:abandoned-r1'"
    ).fetchone()
    assert row["status"] == "failed"


def test_backfill_skips_closed_run(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_cccccc"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (30, "uuid-child-30", "a-ok:already-done", _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, ended_at, hermes_session_id) "
        "VALUES (30, 1, 'a-ok:already-done-r1', 'print', '', 'done', ?, ?, ?)",
        (_old_iso(2), _old_iso(1), parent_id),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    transcript.write_text(
        "spawn a-ok:already-done-r1 ...\nexit=0\n", encoding="utf-8"
    )

    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=_old_iso(1),
    )
    assert stats["updated"] == 0
    assert stats["inserted"] == 0
    assert stats["skipped"] == 1


# ---------------------------------------------------------------------------
# backfill_session — INSERT path
# ---------------------------------------------------------------------------

def test_backfill_inserts_missing_run(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    # The INSERT path depends on the uuid join landing — set hermes_sessions.uuid
    # to equal the parent hermes_session_id so the resolver succeeds.
    parent_id = "20260519_120000_dddddd"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'native', ?, ?)",
        (40, parent_id, "parent-hermes", _now_iso(), _now_iso()),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    transcript.write_text(
        "aok-spawn ...\nspawned a-ok:brand-new ...\nexit=0\n",
        encoding="utf-8",
    )
    transcript_mtime = _old_iso(2)

    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=transcript_mtime,
    )
    assert stats["inserted"] == 1

    row = conn.execute(
        "SELECT name, status, started_at, ended_at, note, hermes_session_id, mode "
        "FROM hermes_runs WHERE name='a-ok:brand-new'"
    ).fetchone()
    assert row["name"] == "a-ok:brand-new"
    assert row["status"] == "done"
    assert row["mode"] == "print"
    assert row["hermes_session_id"] == parent_id
    assert "backfill" in (row["note"] or "")
    assert row["started_at"] == transcript_mtime


def test_backfill_skips_when_session_id_unresolvable(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_eeeeee"  # no matching hermes_sessions row
    transcript = tmp_path / "session.json"
    transcript.write_text(
        "aok-spawn ...\na-ok:orphan-slug ...\nexit=0\n", encoding="utf-8"
    )
    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=_old_iso(2),
    )
    assert stats["inserted"] == 0
    assert stats["unmatched_slugs"] == ["a-ok:orphan-slug"]
    assert conn.execute(
        "SELECT COUNT(*) FROM hermes_runs"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# dry-run & backfill_all
# ---------------------------------------------------------------------------

def test_dry_run_no_mutation(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_ffffff"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (50, "uuid-child-50", "a-ok:dryrun", _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, hermes_session_id) "
        "VALUES (50, 1, 'a-ok:dryrun-r1', 'print', '', 'started', ?, ?)",
        (_old_iso(3), parent_id),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    transcript.write_text(
        "spawn a-ok:dryrun-r1 ...\nexit=0\n", encoding="utf-8"
    )

    stats = backfill_session(
        conn,
        parent_id,
        str(transcript),
        now_iso=_now_iso(),
        transcript_mtime=_old_iso(2),
        dry_run=True,
    )
    assert stats["updated"] == 1
    row = conn.execute(
        "SELECT status, ended_at FROM hermes_runs WHERE name='a-ok:dryrun-r1'"
    ).fetchone()
    # Row should be untouched in dry-run mode.
    assert row["status"] == "started"
    assert row["ended_at"] is None


def test_backfill_skips_native_rows(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    # A native row (profile_name IS NULL) with a transcript that, were it
    # scanned, would synthesize an a-ok run. backfill_all must not touch it.
    transcript = tmp_path / "native.jsonl"
    transcript.write_text(
        "aok-spawn ...\na-ok:should-not-be-touched ...\nexit=0\n",
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO hermes_agent_sessions (hermes_session_id, profile_name, "
        "transcript_path, transcript_mtime, synced_at) "
        "VALUES (?, NULL, ?, ?, ?)",
        ("native-uuid", str(transcript), _old_iso(1), _now_iso()),
    )
    conn.commit()

    stats = backfill_all(conn, window_hours=24, dry_run=False)
    assert stats["sessions_scanned"] == 0
    assert stats["inserted"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM hermes_runs"
    ).fetchone()[0] == 0


def test_backfill_all_aggregates_per_session(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    parent_id = "20260519_120000_aggregate"
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (60, "uuid-child-60", "a-ok:agg", _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, hermes_session_id) "
        "VALUES (60, 1, 'a-ok:agg-r1', 'print', '', 'started', ?, ?)",
        (_old_iso(3), parent_id),
    )
    conn.commit()

    transcript = tmp_path / "session.json"
    transcript.write_text(
        "spawn a-ok:agg-r1 ...\nexit=0\n", encoding="utf-8"
    )
    conn.execute(
        "INSERT INTO hermes_agent_sessions (hermes_session_id, profile_name, "
        "transcript_path, transcript_mtime, synced_at) "
        "VALUES (?, 'default', ?, ?, ?)",
        (parent_id, str(transcript), _old_iso(1), _now_iso()),
    )
    conn.commit()

    stats = backfill_all(conn, window_hours=24, dry_run=False)
    assert stats["sessions_scanned"] == 1
    assert stats["sessions_with_changes"] == 1
    assert stats["updated"] == 1
    assert stats["per_session"][0]["hermes_session_id"] == parent_id


# ---------------------------------------------------------------------------
# heartbeat integration smoke
# ---------------------------------------------------------------------------

def test_classify_sessions_calls_backfill(monkeypatch, tmp_path: Path) -> None:
    """classify_sessions() must invoke backfill_all once per tick when the
    env gate is on, and a failure in backfill must not break the heartbeat
    flow. The gate itself defaults OFF — see ``test_dispatch_hsid``."""
    from worker_control_hermes import heartbeat

    monkeypatch.setenv("WORKER_CONTROL_BACKFILL_ENABLED", "1")

    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(heartbeat, "DB_PATH", db)

    # Stub out everything the rest of classify_sessions touches so we
    # only exercise the backfill hook.
    monkeypatch.setattr(heartbeat, "_load_db_sessions", lambda: [])
    monkeypatch.setattr(heartbeat, "_jsonl_lookup", lambda: {})

    # parity_ingest call should still happen but is fine to no-op.
    import worker_control_hermes.legacy_parity_ingest as parity_mod
    monkeypatch.setattr(parity_mod, "ingest_all", lambda conn, **_kw: {"ok": True})

    calls = {"n": 0}

    def fake_backfill(conn, **kw):
        calls["n"] += 1
        assert kw.get("dry_run") is False
        assert kw.get("window_hours") == 168
        return {
            "sessions_scanned": 0, "sessions_with_changes": 0,
            "updated": 0, "inserted": 0, "skipped": 0,
            "relinked": 0, "ambiguous": 0,
            "unmatched_slugs": [], "per_session": [],
        }

    monkeypatch.setattr(spawn_backfill, "backfill_all", fake_backfill)

    snap = heartbeat.classify_sessions(window_min=30)
    assert calls["n"] == 1
    # Sanity — pipeline still returned a snapshot dict.
    assert "alive" in snap and "idle" in snap


# ---------------------------------------------------------------------------
# relink — ghost hsid → real hsid
# ---------------------------------------------------------------------------

def _seed_run_on_hsid(
    conn: sqlite3.Connection,
    *,
    session_pk: int,
    uuid: str,
    slug_name: str,
    hsid: str,
    status: str = "started",
) -> None:
    conn.execute(
        "INSERT INTO hermes_sessions (id, uuid, project_id, name, origin, "
        "created_at, last_used_at) VALUES (?, ?, 1, ?, 'spawned', ?, ?)",
        (session_pk, uuid, slug_name, _now_iso(), _now_iso()),
    )
    conn.execute(
        "INSERT INTO hermes_runs (session_id, run_index, name, mode, command, "
        "status, started_at, hermes_session_id) "
        "VALUES (?, 1, ?, 'print', '', ?, ?, ?)",
        (session_pk, slug_name, status, _old_iso(3), hsid),
    )


def test_relink_orphan_run_from_ghost_hsid(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    ghost_hsid = "20260519_100000_ghosty"
    real_hsid = "20260519_102213_cc5ea1"

    _insert_agent_session(conn, ghost_hsid, ghost=True)
    transcript = tmp_path / "real.json"
    transcript.write_text(
        "aok-spawn ...\nspawn a-ok:foo-r1 ...\nexit=0\n", encoding="utf-8"
    )
    _insert_agent_session(
        conn, real_hsid, ghost=False, transcript_path=str(transcript)
    )
    _seed_run_on_hsid(
        conn, session_pk=100, uuid="uuid-child-100",
        slug_name="a-ok:foo-r1", hsid=ghost_hsid, status="started",
    )
    conn.commit()

    stats = backfill_session(
        conn, real_hsid, str(transcript),
        now_iso=_now_iso(), transcript_mtime=_old_iso(1),
    )

    assert stats["relinked"] == 1
    assert stats["updated"] == 1
    assert stats["ambiguous"] == 0
    row = conn.execute(
        "SELECT hermes_session_id, status FROM hermes_runs "
        "WHERE name='a-ok:foo-r1'"
    ).fetchone()
    assert row["hermes_session_id"] == real_hsid
    assert row["status"] == "done"


def test_relink_skipped_when_both_real(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    other_real_hsid = "20260519_101010_otherr"
    real_hsid = "20260519_102213_cc5ea1"

    _insert_agent_session(conn, other_real_hsid, ghost=False)
    transcript = tmp_path / "real.json"
    transcript.write_text(
        "aok-spawn ...\nspawn a-ok:foo-r1 ...\nexit=0\n", encoding="utf-8"
    )
    _insert_agent_session(
        conn, real_hsid, ghost=False, transcript_path=str(transcript)
    )
    _seed_run_on_hsid(
        conn, session_pk=101, uuid="uuid-child-101",
        slug_name="a-ok:foo-r1", hsid=other_real_hsid, status="started",
    )
    conn.commit()

    stats = backfill_session(
        conn, real_hsid, str(transcript),
        now_iso=_now_iso(), transcript_mtime=_old_iso(1),
    )

    assert stats["relinked"] == 0
    assert stats["ambiguous"] == 1
    row = conn.execute(
        "SELECT hermes_session_id, status FROM hermes_runs "
        "WHERE name='a-ok:foo-r1'"
    ).fetchone()
    assert row["hermes_session_id"] == other_real_hsid
    assert row["status"] == "started"


def test_relink_skipped_when_transcript_hsid_ghost(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    real_other_hsid = "20260519_101010_realer"
    ghost_current_hsid = "20260519_102213_ghost1"

    _insert_agent_session(conn, real_other_hsid, ghost=False)
    transcript = tmp_path / "ghost.json"
    transcript.write_text(
        "aok-spawn ...\nspawn a-ok:foo-r1 ...\nexit=0\n", encoding="utf-8"
    )
    _insert_agent_session(
        conn, ghost_current_hsid, ghost=True, transcript_path=str(transcript)
    )
    _seed_run_on_hsid(
        conn, session_pk=102, uuid="uuid-child-102",
        slug_name="a-ok:foo-r1", hsid=real_other_hsid, status="started",
    )
    conn.commit()

    stats = backfill_session(
        conn, ghost_current_hsid, str(transcript),
        now_iso=_now_iso(), transcript_mtime=_old_iso(1),
    )

    assert stats["relinked"] == 0
    assert stats["ambiguous"] == 1
    row = conn.execute(
        "SELECT hermes_session_id, status FROM hermes_runs "
        "WHERE name='a-ok:foo-r1'"
    ).fetchone()
    assert row["hermes_session_id"] == real_other_hsid
    assert row["status"] == "started"


def test_dry_run_no_relink_mutation(tmp_path: Path) -> None:
    conn = _fresh_db(tmp_path)
    ghost_hsid = "20260519_100000_ghosty"
    real_hsid = "20260519_102213_cc5ea1"

    _insert_agent_session(conn, ghost_hsid, ghost=True)
    transcript = tmp_path / "real.json"
    transcript.write_text(
        "aok-spawn ...\nspawn a-ok:foo-r1 ...\nexit=0\n", encoding="utf-8"
    )
    _insert_agent_session(
        conn, real_hsid, ghost=False, transcript_path=str(transcript)
    )
    _seed_run_on_hsid(
        conn, session_pk=103, uuid="uuid-child-103",
        slug_name="a-ok:foo-r1", hsid=ghost_hsid, status="started",
    )
    conn.commit()

    stats = backfill_session(
        conn, real_hsid, str(transcript),
        now_iso=_now_iso(), transcript_mtime=_old_iso(1),
        dry_run=True,
    )

    assert stats["relinked"] == 1
    row = conn.execute(
        "SELECT hermes_session_id, status, ended_at FROM hermes_runs "
        "WHERE name='a-ok:foo-r1'"
    ).fetchone()
    # DB must be untouched in dry-run mode — neither hsid nor status moves.
    assert row["hermes_session_id"] == ghost_hsid
    assert row["status"] == "started"
    assert row["ended_at"] is None


def test_cli_dry_run_emits_json(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "wc.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db))

    rc = spawn_backfill.main(["--dry-run", "--json", "--window-hours", "24"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"sessions_scanned"' in out
