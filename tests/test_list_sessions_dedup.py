"""Tests for ``list_sessions(group_dupes=…)`` — SELECT-axis dedup.

Spec: docs/intent-and-prd-orphan-dedup.md §5, §6.

Fixture shape: four ``hermes_sessions`` rows that share the worker name
``a-ok:dup-test``. Two link to parent hsid_A via ``hermes_runs``; two link
to parent hsid_B. This is the same "two hsid groups, one duplicated card
each" pathology the user actually observed in prod.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from worker_control.session_view import list_sessions


_PROJECTS_SQL = dedent("""
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        path        TEXT NOT NULL,
        root_role   TEXT NOT NULL DEFAULT 'owned_work',
        created_at  TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL DEFAULT ''
    )
""")

_SESSIONS_SQL = dedent("""
    CREATE TABLE hermes_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid            TEXT NOT NULL UNIQUE,
        project_id      INTEGER,
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
    )
""")

_RUNS_SQL = dedent("""
    CREATE TABLE hermes_runs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        INTEGER NOT NULL,
        run_index         INTEGER NOT NULL,
        name              TEXT,
        mode              TEXT,
        status            TEXT,
        started_at        TEXT,
        ended_at          TEXT,
        hermes_session_id TEXT
    )
""")


def _bootstrap(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for sql in (_PROJECTS_SQL, _SESSIONS_SQL, _RUNS_SQL):
        conn.execute(sql)
    return conn


def _insert_project(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO projects(name, path, root_role, created_at, updated_at) "
        "VALUES ('p', '/tmp/p', 'owned_work', "
        "        '2026-05-18T00:00:00Z', '2026-05-18T00:00:00Z')"
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_session(conn: sqlite3.Connection, *, uuid: str, name: str,
                    project_id: int, last_used_at: str) -> int:
    conn.execute(
        "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, "
        " created_at, last_used_at) "
        "VALUES (?, ?, ?, 'active', 'spawned', ?, ?)",
        (uuid, project_id, name, last_used_at, last_used_at),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _link_run(conn: sqlite3.Connection, *, session_id: int, run_index: int,
              hermes_session_id: str) -> None:
    conn.execute(
        "INSERT INTO hermes_runs(session_id, run_index, name, mode, status, "
        "started_at, ended_at, hermes_session_id) "
        "VALUES (?, ?, 'a-ok:dup-test-r', 'print', 'done', "
        "        '2026-05-18T00:00:00Z', '2026-05-18T00:00:00Z', ?)",
        (session_id, run_index, hermes_session_id),
    )


# Two parent hermes_session_ids, two sessions stamped to each.
HSID_A = "session_host_20260519_154513_9925891f"
HSID_B = "session_host_20260519_154429_cd14c910"

UUIDS = {
    "A1": "aaaaaaa1-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "A2": "aaaaaaa2-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "B1": "bbbbbbb1-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    "B2": "bbbbbbb2-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
}

# A2 / B2 are the "newest" rows in each hsid group → should win their groups.
TIMESTAMPS = {
    "A1": "2026-05-19T15:40:00Z",
    "A2": "2026-05-19T15:50:00Z",
    "B1": "2026-05-19T15:41:00Z",
    "B2": "2026-05-19T15:51:00Z",
}


@pytest.fixture
def dup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "wc.sqlite3"
    conn = _bootstrap(db_path)
    pid = _insert_project(conn)
    sid_A1 = _insert_session(conn, uuid=UUIDS["A1"], name="a-ok:dup-test",
                              project_id=pid, last_used_at=TIMESTAMPS["A1"])
    sid_A2 = _insert_session(conn, uuid=UUIDS["A2"], name="a-ok:dup-test",
                              project_id=pid, last_used_at=TIMESTAMPS["A2"])
    sid_B1 = _insert_session(conn, uuid=UUIDS["B1"], name="a-ok:dup-test",
                              project_id=pid, last_used_at=TIMESTAMPS["B1"])
    sid_B2 = _insert_session(conn, uuid=UUIDS["B2"], name="a-ok:dup-test",
                              project_id=pid, last_used_at=TIMESTAMPS["B2"])
    _link_run(conn, session_id=sid_A1, run_index=1, hermes_session_id=HSID_A)
    _link_run(conn, session_id=sid_A2, run_index=1, hermes_session_id=HSID_A)
    _link_run(conn, session_id=sid_B1, run_index=1, hermes_session_id=HSID_B)
    _link_run(conn, session_id=sid_B2, run_index=1, hermes_session_id=HSID_B)
    conn.commit()
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_path))
    yield conn
    conn.close()


def test_group_dupes_off_returns_all_four(dup_db: sqlite3.Connection) -> None:
    rows = list_sessions(group_dupes="off")
    assert len(rows) == 4
    assert all(v.superseded_by == [] for v in rows)


def test_group_dupes_by_name_within_hsid_collapses_per_hsid(
    dup_db: sqlite3.Connection,
) -> None:
    rows = list_sessions(group_dupes="by_name_within_hsid")
    assert len(rows) == 2
    # Winners are the newer row in each hsid group (A2, B2).
    winner_uuids = {v.uuid for v in rows}
    assert winner_uuids == {UUIDS["A2"], UUIDS["B2"]}
    for v in rows:
        assert len(v.superseded_by) == 1
    # Each winner's superseded_by points at its sibling in the same hsid group.
    by_uuid = {v.uuid: v for v in rows}
    assert by_uuid[UUIDS["A2"]].superseded_by == [UUIDS["A1"]]
    assert by_uuid[UUIDS["B2"]].superseded_by == [UUIDS["B1"]]


def test_group_dupes_by_name_collapses_across_hsids(
    dup_db: sqlite3.Connection,
) -> None:
    rows = list_sessions(group_dupes="by_name")
    assert len(rows) == 1
    v = rows[0]
    # Newest of the four wins (B2 at 15:51 vs A2 at 15:50).
    assert v.uuid == UUIDS["B2"]
    assert len(v.superseded_by) == 3
    # The other three uuids are recorded — order is newest-superseded-first
    # because list_sessions sorts last_used_at DESC before grouping.
    assert set(v.superseded_by) == {UUIDS["A2"], UUIDS["B1"], UUIDS["A1"]}


def test_default_policy_matches_by_name_within_hsid(
    dup_db: sqlite3.Connection,
) -> None:
    """No kwarg → new default (by_name_within_hsid).

    Documents the backwards-compat behavior change introduced by §5: callers
    that want the legacy 1:1 view must now opt in via ``group_dupes='off'``.
    """
    default_rows = list_sessions()
    explicit_rows = list_sessions(group_dupes="by_name_within_hsid")
    assert {v.uuid for v in default_rows} == {v.uuid for v in explicit_rows}
    assert len(default_rows) == 2
