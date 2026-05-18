"""Tests for ``worker_control.session_sync`` — the single writer for the
``hermes_sessions`` table.

These tests bootstrap a minimal canonical schema in a fresh sqlite file
per test so we exercise ``upsert_session`` against real SQL semantics
rather than a stub. The full canonical DB has many more tables (managed
by ``migrate_to_canonical_db.py``); we only create the two the writer
touches.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from worker_control import session_sync
from worker_control.session_sync import (
    ProjectNotRegistered,
    SessionUpsert,
    from_dispatcher_argv,
    from_jsonl,
    sync_jsonl_dir,
    upsert_session,
)


_PROJECTS_TABLE_SQL = dedent(
    """
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
    )
    """
).strip()

_SESSIONS_TABLE_SQL = dedent(
    """
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
        created_at      TEXT NOT NULL,
        last_used_at    TEXT NOT NULL,
        ended_at        TEXT,
        claude_name     TEXT,
        -- enrichment columns owned by hermes_session_sync; the upsert
        -- writer must never touch these.
        cwd             TEXT,
        first_message   TEXT,
        last_message    TEXT,
        turn_count      INTEGER,
        total_cost_usd  REAL
    )
    """
).strip()


def _make_db(tmp_path: Path, *project_paths: str) -> sqlite3.Connection:
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_PROJECTS_TABLE_SQL)
    conn.execute(_SESSIONS_TABLE_SQL)
    now = "2026-05-18T00:00:00+00:00"
    for p in project_paths:
        conn.execute(
            "INSERT INTO hermes_projects_v(folder_path, created_at, last_used_at) "
            "VALUES (?, ?, ?)",
            (str(Path(p).resolve()), now, now),
        )
    conn.commit()
    return conn


def _jsonl_lines(cwd: str, uuid: str, *, user_text: str = "hello world",
                 model: str = "claude-sonnet-4-6") -> str:
    return "\n".join(
        json.dumps(d)
        for d in [
            {"type": "summary", "cwd": cwd, "version": "1.2.3",
             "timestamp": "2026-05-17T10:00:00Z"},
            {"type": "user", "timestamp": "2026-05-17T10:00:01Z",
             "message": {"content": user_text}},
            {"type": "assistant", "timestamp": "2026-05-17T10:00:02Z",
             "message": {"model": model, "content": "hi"}},
        ]
    ) + "\n"


# ---------------------------------------------------------------------------
# from_jsonl
# ---------------------------------------------------------------------------


def test_from_jsonl_happy_path(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    proj_dir = tmp_path / "projects" / "fake"
    proj_dir.mkdir(parents=True)
    sid = "02cd57cf-4377-455e-84c1-1a067fc952e4"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_text(_jsonl_lines(str(project), sid, user_text="port the writer"),
                     encoding="utf-8")

    up = from_jsonl(jsonl)
    assert up is not None
    assert up.uuid == sid
    assert up.origin == "native"
    assert up.project_path == str(project.resolve())
    assert up.brief == "port the writer"
    assert up.model == "claude-sonnet-4-6"
    assert up.last_used_at == "2026-05-17T10:00:02Z"
    assert up.name == "port the writer"
    assert up.source_path == str(jsonl)


def test_from_jsonl_empty_file(tmp_path: Path) -> None:
    sid = "02cd57cf-4377-455e-84c1-1a067fc952e4"
    proj_dir = tmp_path / "projects" / "D--missing-path-cannot-decode"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_text("", encoding="utf-8")
    # No cwd recoverable → None (don't raise).
    assert from_jsonl(jsonl) is None


def test_from_jsonl_corrupt_lines_dont_raise(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    proj_dir = tmp_path / "projects" / "fake"
    proj_dir.mkdir(parents=True)
    sid = "02cd57cf-4377-455e-84c1-1a067fc952e4"
    jsonl = proj_dir / f"{sid}.jsonl"
    jsonl.write_text(
        '{"type": "summary", "cwd": "' + str(project).replace("\\", "/") + '"}\n'
        "this-is-not-json\n"
        '{"type": "user", "timestamp": "2026-05-17T10:00:00Z", '
        '"message": {"content": "ok"}}\n',
        encoding="utf-8",
    )
    up = from_jsonl(jsonl)
    assert up is not None
    assert up.brief == "ok"


def test_from_jsonl_non_uuid_stem_returns_none(tmp_path: Path) -> None:
    proj_dir = tmp_path / "projects" / "fake"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / "not-a-uuid.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")
    assert from_jsonl(jsonl) is None


# ---------------------------------------------------------------------------
# upsert_session
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_row_with_origin(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    sid = "11111111-1111-1111-1111-111111111111"
    rowid = upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="spawn-x", origin="spawned",
            project_path=str(project), brief="task brief",
            model="claude-opus-4-7", last_used_at="2026-05-18T01:00:00+00:00",
            created_at="2026-05-18T01:00:00+00:00",
        ),
    )
    row = conn.execute(
        "SELECT * FROM hermes_sessions WHERE id=?", (rowid,)
    ).fetchone()
    assert row["uuid"] == sid
    assert row["origin"] == "spawned"
    assert row["name"] == "spawn-x"
    assert row["brief"] == "task brief"
    assert row["model"] == "claude-opus-4-7"
    assert row["status"] == "active"
    assert row["last_used_at"] == "2026-05-18T01:00:00+00:00"


def test_upsert_preserves_origin_on_update(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    sid = "22222222-2222-2222-2222-222222222222"
    # Seed with origin=spawned.
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="spawn-a", origin="spawned",
            project_path=str(project),
            last_used_at="2026-05-18T01:00:00+00:00",
            created_at="2026-05-18T01:00:00+00:00",
        ),
    )
    # Native scanner sees the same UUID later; must NOT downgrade.
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="rename-from-native", origin="native",
            project_path=str(project), brief="fresher brief",
            last_used_at="2026-05-18T02:00:00+00:00",
        ),
    )
    row = conn.execute(
        "SELECT * FROM hermes_sessions WHERE uuid=?", (sid,)
    ).fetchone()
    assert row["origin"] == "spawned", "origin must never change on UPDATE"
    assert row["brief"] == "fresher brief"
    assert row["name"] == "rename-from-native"
    assert row["last_used_at"] == "2026-05-18T02:00:00+00:00"


def test_upsert_last_used_at_is_monotonic(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    sid = "33333333-3333-3333-3333-333333333333"
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="x", origin="native",
            project_path=str(project),
            last_used_at="2026-05-18T05:00:00+00:00",
        ),
    )
    # Older timestamp must not rewind last_used_at.
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="x", origin="native",
            project_path=str(project),
            last_used_at="2026-05-18T01:00:00+00:00",
        ),
    )
    row = conn.execute(
        "SELECT last_used_at FROM hermes_sessions WHERE uuid=?", (sid,)
    ).fetchone()
    assert row["last_used_at"] == "2026-05-18T05:00:00+00:00"


def test_upsert_does_not_clobber_enrichment_columns(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    sid = "44444444-4444-4444-4444-444444444444"
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="x", origin="native",
            project_path=str(project),
            last_used_at="2026-05-18T01:00:00+00:00",
        ),
    )
    # Simulate hermes_session_sync back-fill writing enrichment.
    conn.execute(
        "UPDATE hermes_sessions SET cwd=?, first_message=?, turn_count=?, "
        "total_cost_usd=? WHERE uuid=?",
        ("D:/work", "first msg from hermes", 42, 1.23, sid),
    )
    # Now another native sync hits the row.
    upsert_session(
        conn,
        SessionUpsert(
            uuid=sid, name="x", origin="native",
            project_path=str(project), brief="updated brief",
            last_used_at="2026-05-18T02:00:00+00:00",
        ),
    )
    row = conn.execute(
        "SELECT cwd, first_message, turn_count, total_cost_usd "
        "FROM hermes_sessions WHERE uuid=?",
        (sid,),
    ).fetchone()
    assert row["cwd"] == "D:/work"
    assert row["first_message"] == "first msg from hermes"
    assert row["turn_count"] == 42
    assert row["total_cost_usd"] == pytest.approx(1.23)


def test_upsert_raises_when_project_not_registered(tmp_path: Path) -> None:
    conn = _make_db(tmp_path)  # no projects seeded
    sid = "55555555-5555-5555-5555-555555555555"
    with pytest.raises(ProjectNotRegistered):
        upsert_session(
            conn,
            SessionUpsert(
                uuid=sid, name="x", origin="native",
                project_path=str(tmp_path / "unregistered"),
            ),
        )


def test_upsert_rejects_invalid_uuid(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    _make_db(tmp_path, str(project))
    with pytest.raises(ValueError):
        SessionUpsert(
            uuid="not-a-uuid", name="x", origin="native",
            project_path=str(project),
        )


def test_upsert_rejects_invalid_origin(tmp_path: Path) -> None:
    project = tmp_path / "p1"
    project.mkdir()
    with pytest.raises(ValueError):
        SessionUpsert(
            uuid="66666666-6666-6666-6666-666666666666",
            name="x", origin="bogus",
            project_path=str(project),
        )


# ---------------------------------------------------------------------------
# from_dispatcher_argv
# ---------------------------------------------------------------------------


def test_from_dispatcher_argv_marks_origin_spawned(tmp_path: Path) -> None:
    up = from_dispatcher_argv(
        name="a-ok:hello",
        uuid="77777777-7777-7777-7777-777777777777",
        project_path=str(tmp_path),
        brief="dispatcher brief",
        model="claude-opus-4-7",
    )
    assert up.origin == "spawned"
    assert up.name == "a-ok:hello"
    assert up.model == "claude-opus-4-7"
    assert up.brief == "dispatcher brief"


# ---------------------------------------------------------------------------
# sync_jsonl_dir
# ---------------------------------------------------------------------------


def test_sync_jsonl_dir_discovers_and_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    conn = _make_db(tmp_path, str(project))

    root = tmp_path / "projects"
    proj_dir = root / "anything"
    proj_dir.mkdir(parents=True)
    uuids = [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3",
    ]
    for u in uuids:
        (proj_dir / f"{u}.jsonl").write_text(
            _jsonl_lines(str(project), u, user_text=f"task-{u[-1]}"),
            encoding="utf-8",
        )

    first = sync_jsonl_dir(conn, root)
    assert first == {"created": 3, "updated": 0, "skipped": 0}

    second = sync_jsonl_dir(conn, root)
    assert second == {"created": 0, "updated": 3, "skipped": 0}

    rows = conn.execute(
        "SELECT uuid, origin FROM hermes_sessions ORDER BY uuid"
    ).fetchall()
    assert [r["uuid"] for r in rows] == uuids
    assert {r["origin"] for r in rows} == {"native"}


def test_sync_jsonl_dir_skips_unregistered_projects(tmp_path: Path) -> None:
    project_registered = tmp_path / "known"
    project_registered.mkdir()
    project_unknown = tmp_path / "unknown"
    project_unknown.mkdir()
    conn = _make_db(tmp_path, str(project_registered))

    root = tmp_path / "projects"
    proj_dir = root / "x"
    proj_dir.mkdir(parents=True)
    (proj_dir / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1.jsonl").write_text(
        _jsonl_lines(str(project_registered),
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"),
        encoding="utf-8",
    )
    (proj_dir / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2.jsonl").write_text(
        _jsonl_lines(str(project_unknown),
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"),
        encoding="utf-8",
    )

    result = sync_jsonl_dir(conn, root)
    assert result == {"created": 1, "updated": 0, "skipped": 1}


def test_sync_jsonl_dir_missing_root_returns_zero_counts(tmp_path: Path) -> None:
    conn = _make_db(tmp_path)
    assert sync_jsonl_dir(conn, tmp_path / "nope") == {
        "created": 0, "updated": 0, "skipped": 0,
    }
