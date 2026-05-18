"""Tests for ``worker_control.session_view`` — the single reader path.

Each test bootstraps a minimal canonical schema (the same tables the
production migrate_to_canonical_db.py builds, but only the slices the
reader touches) so we exercise ``list_sessions`` against real SQL
semantics — JOIN behavior, the prefix-driven classification, the
parity-column merge, and the five child-table aggregations.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from worker_control import session_view
from worker_control.session_view import (
    SessionView,
    is_spawn,
    list_sessions,
    session_counters,
)


# ---------------------------------------------------------------------------
# Schema bootstrap (a tiny canonical DB, no migrations)
# ---------------------------------------------------------------------------


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
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL,
        run_index   INTEGER NOT NULL,
        name        TEXT,
        mode        TEXT,
        status      TEXT,
        started_at  TEXT,
        ended_at    TEXT
    )
""")

_AGENT_SQL = dedent("""
    CREATE TABLE hermes_agent_sessions (
        hermes_session_id TEXT PRIMARY KEY,
        profile_name      TEXT,
        profile_path      TEXT,
        transcript_path   TEXT,
        transcript_mtime  TEXT,
        started_at        TEXT,
        ended_at          TEXT,
        model             TEXT,
        turn_count        INTEGER NOT NULL DEFAULT 0,
        first_message     TEXT,
        last_message      TEXT,
        cwd               TEXT,
        total_cost_usd    REAL,
        synced_at         TEXT NOT NULL DEFAULT '',
        kind              TEXT NOT NULL DEFAULT 'claude',
        git_branch        TEXT,
        claude_version    TEXT,
        msg_user          INTEGER NOT NULL DEFAULT 0,
        msg_assistant     INTEGER NOT NULL DEFAULT 0,
        msg_tool          INTEGER NOT NULL DEFAULT 0,
        ai_title          TEXT,
        summary           TEXT,
        first_user_text   TEXT,
        last_user_text    TEXT,
        last_assistant_text TEXT,
        size_bytes        INTEGER,
        spawn_slug        TEXT,
        spawn_reason      TEXT,
        is_spawned        INTEGER NOT NULL DEFAULT 0,
        effective_status  TEXT
    )
""")

_PR_LINKS_SQL = dedent("""
    CREATE TABLE session_pr_links (
        session_uuid TEXT NOT NULL,
        url          TEXT NOT NULL,
        num          INTEGER,
        repo         TEXT,
        kind         TEXT,
        PRIMARY KEY (session_uuid, url)
    )
""")

_FILES_SQL = dedent("""
    CREATE TABLE session_files_touched (
        session_uuid TEXT NOT NULL,
        path         TEXT NOT NULL,
        last_seen_at TEXT,
        op           TEXT,
        PRIMARY KEY (session_uuid, path)
    )
""")

_TOOLS_SQL = dedent("""
    CREATE TABLE session_tools_recent (
        session_uuid TEXT NOT NULL,
        ord          INTEGER NOT NULL,
        name         TEXT NOT NULL,
        snippet      TEXT,
        ts           TEXT,
        PRIMARY KEY (session_uuid, ord)
    )
""")

_RECAPS_SQL = dedent("""
    CREATE TABLE session_recaps (
        session_uuid TEXT NOT NULL,
        ord          INTEGER NOT NULL,
        content      TEXT NOT NULL,
        ts           TEXT,
        PRIMARY KEY (session_uuid, ord)
    )
""")

_PENDING_SQL = dedent("""
    CREATE TABLE session_pending_queue (
        session_uuid TEXT NOT NULL,
        ord          INTEGER NOT NULL,
        text         TEXT NOT NULL,
        queued_at    TEXT,
        PRIMARY KEY (session_uuid, ord)
    )
""")


def _bootstrap_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for sql in (
        _PROJECTS_SQL, _SESSIONS_SQL, _RUNS_SQL, _AGENT_SQL,
        _PR_LINKS_SQL, _FILES_SQL, _TOOLS_SQL, _RECAPS_SQL, _PENDING_SQL,
    ):
        conn.execute(sql)
    return conn


def _insert_project(conn: sqlite3.Connection, *, name: str, path: str,
                    role: str = "owned_work") -> int:
    conn.execute(
        "INSERT INTO projects(name, path, root_role, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, path, role, "2026-05-18T00:00:00Z", "2026-05-18T00:00:00Z"),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_session(
    conn: sqlite3.Connection, *,
    uuid: str, name: str, project_id: int,
    origin: str = "native", status: str = "active",
    last_used_at: str = "2026-05-18T00:00:00Z",
    brief: str | None = None, model: str | None = None,
    notes: str = "",
) -> int:
    conn.execute(
        "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, "
        " model, brief, notes, created_at, last_used_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid, project_id, name, status, origin, model, brief, notes,
         last_used_at, last_used_at),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_run(conn: sqlite3.Connection, *,
                session_id: int, run_index: int,
                name: str, mode: str = "print", status: str = "done",
                started_at: str = "2026-05-18T00:00:00Z") -> None:
    conn.execute(
        "INSERT INTO hermes_runs(session_id, run_index, name, mode, status, "
        "started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, run_index, name, mode, status, started_at, started_at),
    )


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a fresh canonical DB on disk + route session_view at it."""
    db_path = tmp_path / "wc.sqlite3"
    conn = _bootstrap_db(db_path)
    # session_view.connect() looks at WORKER_CONTROL_DB; the autouse
    # fixture in conftest already pointed WORKER_CONTROL_HOME at tmp_path,
    # so place the bootstrap DB where db_path() expects to find it.
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_path))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Empty / missing-table behavior
# ---------------------------------------------------------------------------


def test_list_sessions_empty_returns_empty(db: sqlite3.Connection) -> None:
    """Empty DB → no rows, no exceptions."""
    out = list_sessions()
    assert out == []


def test_list_sessions_missing_hermes_tables_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the canonical DB doesn't have hermes_sessions yet → [], no crash."""
    db_path = tmp_path / "wc.sqlite3"
    sqlite3.connect(db_path).close()  # empty file, no tables
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_path))
    assert list_sessions() == []


# ---------------------------------------------------------------------------
# Join + classification
# ---------------------------------------------------------------------------


def test_list_sessions_joins_project_and_runs(db: sqlite3.Connection) -> None:
    """Every row carries its project name/path and last-run aggregates."""
    pid = _insert_project(db, name="proj-x", path="/tmp/x")
    sid = _insert_session(
        db, uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        name="hello", project_id=pid, brief="why",
    )
    _insert_run(db, session_id=sid, run_index=1, name="hello-r1",
                mode="print", status="done")
    _insert_run(db, session_id=sid, run_index=2, name="hello-r2",
                mode="interactive", status="started",
                started_at="2026-05-19T00:00:00Z")
    db.commit()

    rows = list_sessions()
    assert len(rows) == 1
    v = rows[0]
    assert v.uuid == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert v.name == "hello"
    assert v.project_name == "proj-x"
    assert v.project_path == "/tmp/x"
    assert v.run_count == 2
    assert v.print_run_count == 1
    assert v.last_run_index == 2
    assert v.last_run_mode == "interactive"
    assert v.dispatch_mode == "interactive"
    assert v.brief == "why"


def test_classification_spawn_vs_native(db: sqlite3.Connection) -> None:
    """The ``a-ok:`` name prefix promotes a session to a_ok_spawned; a
    session with no runs and no prefix is ``native``; one with a run but
    no prefix is ``interactive_multi``.
    """
    pid = _insert_project(db, name="p", path="/tmp/p")
    s_spawn = _insert_session(
        db, uuid="11111111-1111-1111-1111-111111111111",
        name="a-ok:work", project_id=pid, origin="spawned",
    )
    s_native = _insert_session(
        db, uuid="22222222-2222-2222-2222-222222222222",
        name="manual", project_id=pid, origin="native",
    )
    s_interactive = _insert_session(
        db, uuid="33333333-3333-3333-3333-333333333333",
        name="resumed", project_id=pid, origin="native",
    )
    _insert_run(db, session_id=s_interactive, run_index=1,
                name="resumed-r1", mode="print", status="done")
    db.commit()

    rows = {v.uuid: v for v in list_sessions()}
    assert rows["11111111-1111-1111-1111-111111111111"].classification == "a_ok_spawned"
    assert rows["11111111-1111-1111-1111-111111111111"].spawn_reason == "prefix:a-ok:"
    assert rows["22222222-2222-2222-2222-222222222222"].classification == "native"
    assert rows["33333333-3333-3333-3333-333333333333"].classification == "interactive_multi"

    assert is_spawn(rows["11111111-1111-1111-1111-111111111111"])
    assert not is_spawn(rows["22222222-2222-2222-2222-222222222222"])
    assert not is_spawn(rows["33333333-3333-3333-3333-333333333333"])


def test_classification_filter(db: sqlite3.Connection) -> None:
    """``classification="spawned"`` filters to a_ok_spawned only; ``"native"``
    includes both ``native`` and ``interactive_multi`` (FE Native tab semantics)."""
    pid = _insert_project(db, name="p", path="/tmp/p")
    _insert_session(db, uuid="11111111-1111-1111-1111-111111111111",
                    name="a-ok:s1", project_id=pid)
    sid_inter = _insert_session(
        db, uuid="22222222-2222-2222-2222-222222222222",
        name="inter", project_id=pid,
    )
    _insert_run(db, session_id=sid_inter, run_index=1, name="inter-r")
    _insert_session(db, uuid="33333333-3333-3333-3333-333333333333",
                    name="bare", project_id=pid)
    db.commit()

    spawn_only = list_sessions(classification="spawned")
    assert {v.uuid for v in spawn_only} == {"11111111-1111-1111-1111-111111111111"}

    native_view = list_sessions(classification="native")
    assert {v.uuid for v in native_view} == {
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    }


def test_since_filter_drops_old_rows(db: sqlite3.Connection) -> None:
    pid = _insert_project(db, name="p", path="/tmp/p")
    _insert_session(db, uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    name="old", project_id=pid,
                    last_used_at="2026-05-01T00:00:00Z")
    _insert_session(db, uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    name="new", project_id=pid,
                    last_used_at="2026-05-18T00:00:00Z")
    db.commit()
    rows = list_sessions(since="2026-05-10T00:00:00Z")
    assert {v.uuid for v in rows} == {"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}


def test_limit_applied(db: sqlite3.Connection) -> None:
    pid = _insert_project(db, name="p", path="/tmp/p")
    for i, ts in enumerate(["2026-05-18T00:00:00Z",
                            "2026-05-17T00:00:00Z",
                            "2026-05-16T00:00:00Z"]):
        _insert_session(
            db, uuid=f"{i:08x}-{i:04x}-{i:04x}-{i:04x}-{i:012x}",
            name=f"s{i}", project_id=pid, last_used_at=ts,
        )
    db.commit()
    rows = list_sessions(limit=2)
    assert len(rows) == 2
    # newest-first ordering
    assert rows[0].name == "s0"
    assert rows[1].name == "s1"


# ---------------------------------------------------------------------------
# Parity columns + child tables
# ---------------------------------------------------------------------------


def test_agent_parity_columns_merge_into_view(db: sqlite3.Connection) -> None:
    """When ``hermes_agent_sessions`` has a row keyed by the same uuid,
    its parity columns appear on the SessionView (kind, git_branch,
    msg counts, summary, …) without a separate query.
    """
    pid = _insert_project(db, name="p", path="/tmp/p")
    uid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    _insert_session(db, uuid=uid, name="paired", project_id=pid)
    db.execute(
        "INSERT INTO hermes_agent_sessions("
        "hermes_session_id, kind, git_branch, claude_version, "
        "msg_user, msg_assistant, msg_tool, ai_title, summary, "
        "first_user_text, last_user_text, last_assistant_text, "
        "size_bytes, spawn_slug, spawn_reason, is_spawned, "
        "effective_status, transcript_path, transcript_mtime, "
        "started_at, ended_at, model, turn_count, first_message, "
        "last_message, cwd, total_cost_usd, synced_at) "
        "VALUES (?, 'claude', 'main', '2.1.141', 21, 35, 4, 'a title', "
        "'a summary', 'first user', 'last user', 'last asst', 12345, "
        "'a-ok:work', 'prefix:a-ok:', 1, 'active', '/tmp/t.jsonl', "
        "'2026-05-18T00:00:00Z', '2026-05-18T00:00:00Z', NULL, "
        "'claude-sonnet-4-6', 56, 'first message', 'last message', "
        "'/tmp/p', 0.42, '2026-05-18T00:01:00Z')",
        (uid,),
    )
    db.commit()

    rows = list_sessions()
    assert len(rows) == 1
    v = rows[0]
    assert v.agent_kind == "claude"
    assert v.git_branch == "main"
    assert v.claude_version == "2.1.141"
    assert v.msg_user == 21
    assert v.msg_assistant == 35
    assert v.msg_tool == 4
    assert v.ai_title == "a title"
    assert v.summary == "a summary"
    assert v.first_user_text == "first user"
    assert v.last_assistant_text == "last asst"
    assert v.transcript_size_bytes == 12345
    assert v.spawn_slug == "a-ok:work"
    assert v.is_spawned_agent is True
    assert v.effective_status == "active"
    assert v.turn_count == 56
    assert v.total_cost_usd == 0.42


def test_child_tables_arrays_attached(db: sqlite3.Connection) -> None:
    """The five parity child tables land on the matching SessionView as arrays."""
    pid = _insert_project(db, name="p", path="/tmp/p")
    uid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    _insert_session(db, uuid=uid, name="rich", project_id=pid)

    db.execute(
        "INSERT INTO session_pr_links(session_uuid, url, num, repo, kind) "
        "VALUES (?, 'https://x/p/1', 1, 'x/p', 'github')",
        (uid,),
    )
    db.execute(
        "INSERT INTO session_files_touched(session_uuid, path, last_seen_at, op) "
        "VALUES (?, 'a.py', '2026-05-18T00:00:00Z', 'edit'), "
        "       (?, 'b.py', '2026-05-18T00:00:01Z', 'write')",
        (uid, uid),
    )
    db.execute(
        "INSERT INTO session_tools_recent(session_uuid, ord, name, snippet, ts) "
        "VALUES (?, 0, 'Bash', 'ls', '2026-05-18T00:00:00Z'), "
        "       (?, 1, 'Read', 'a.py', '2026-05-18T00:00:01Z')",
        (uid, uid),
    )
    db.execute(
        "INSERT INTO session_recaps(session_uuid, ord, content, ts) "
        "VALUES (?, 0, 'recap one', '2026-05-18T00:00:00Z')",
        (uid,),
    )
    db.execute(
        "INSERT INTO session_pending_queue(session_uuid, ord, text, queued_at) "
        "VALUES (?, 0, 'queue first', '2026-05-18T00:00:00Z'), "
        "       (?, 1, 'queue second', '2026-05-18T00:00:01Z')",
        (uid, uid),
    )
    db.commit()

    v = list_sessions()[0]
    assert [p["num"] for p in v.pr_links] == [1]
    assert set(v.files_touched) == {"a.py", "b.py"}
    # tools_recent is ordered DESC by ord
    assert [t["name"] for t in v.tools_recent] == ["Read", "Bash"]
    assert v.recaps[0]["content"] == "recap one"
    # pending_queue is ordered ASC by ord
    assert [q["text"] for q in v.pending_queue] == ["queue first", "queue second"]


def test_get_session_lookup_modes(db: sqlite3.Connection) -> None:
    """get_session resolves by exact UUID, integer id, exact name."""
    pid = _insert_project(db, name="p", path="/tmp/p")
    uid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    sid = _insert_session(db, uuid=uid, name="findme", project_id=pid)
    db.commit()

    assert session_view.get_session(uid).id == sid
    assert session_view.get_session(str(sid)).uuid == uid
    assert session_view.get_session("findme").uuid == uid
    assert session_view.get_session("no-such-thing") is None


def test_session_counters_basic_shape(db: sqlite3.Connection) -> None:
    pid = _insert_project(db, name="p", path="/tmp/p")
    _insert_session(db, uuid="11111111-1111-1111-1111-111111111111",
                    name="a-ok:s1", project_id=pid, status="active")
    sid_done = _insert_session(
        db, uuid="22222222-2222-2222-2222-222222222222",
        name="a-ok:s2", project_id=pid, status="done",
    )
    _insert_run(db, session_id=sid_done, run_index=1, name="a-ok:s2-r",
                mode="print", status="done")
    _insert_session(db, uuid="33333333-3333-3333-3333-333333333333",
                    name="native-x", project_id=pid)
    db.commit()

    counters = session_counters(list_sessions())
    assert counters["hermes_ledger_total"] == 3
    assert counters["hermes_spawned"] == 2
    assert counters["hermes_native"] == 1
    assert counters["hermes_active"] == 2
    assert counters["hermes_done"] == 1
    assert counters["hermes_print_runs_total"] == 1
    assert counters["hermes_sessions_with_print_run"] == 1
