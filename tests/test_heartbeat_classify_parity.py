"""Regression test for the heartbeat ↔ DB parity fix.

nat#f75febed scenario: jsonl is 4h+ stale, parity says ``effective_status='inactive'``,
``last_message='[Request interrupted by user]'``, but a workload subproc
(``rtk.exe grep …``) is still alive and the claude registry has a
session.json with ``status='shell'``. Before the fix, ``classify_sessions``
overrode ``last`` to NOW because ``_subprocs_alive`` was non-empty and
posted the session as ALIVE every 30 minutes. After the fix the bucket
classifier consults the view-derived ``display_status`` and the session
is dropped (out of the heartbeat window) — same verdict the dashboard
already showed.
"""
from __future__ import annotations

import datetime as _dt

from worker_control_hermes import heartbeat


def _aged(hours: float) -> _dt.datetime:
    return heartbeat.NOW - _dt.timedelta(hours=hours)


def _stale_inactive_session() -> dict:
    """Shape ``_load_db_sessions`` returns for nat#f75febed-like rows."""
    return {
        "id": 1,
        "uuid": "f75febed-de62-4469-84f7-e3d2b6f9c3e7",
        "name": "366 MR docs/plans/2026-05-19-",
        "origin": "native",
        "status": "active",
        "display_status": "inactive",
        "brief": None,
        "model": "claude-sonnet-4-5",
        "last_used_at": (heartbeat.NOW - _dt.timedelta(hours=4, minutes=38)).isoformat(),
        "ended_at": None,
        "created_at": (heartbeat.NOW - _dt.timedelta(days=1)).isoformat(),
        "claude_name": None,
        "claude_status": None,
        "claude_status_at": None,
        "proj_name": "live-memory-console",
        "proj_path": "D:/work-gitlab/live-memory-console",
        "msg_tool": 12,
        "runs": [],
    }


def _fresh_active_session() -> dict:
    """Mirror session that should still classify as ALIVE — sanity baseline."""
    s = _stale_inactive_session()
    s["uuid"] = "11111111-2222-3333-4444-555555555555"
    s["display_status"] = "active"
    s["last_used_at"] = (heartbeat.NOW - _dt.timedelta(minutes=2)).isoformat()
    return s


def test_stale_inactive_with_live_subproc_is_not_alive(
    monkeypatch, tmp_path,
) -> None:
    """The regression: live subproc must not promote a stale 'inactive'
    session to the ALIVE bucket. With the fix the session is dropped
    entirely (mtime is far outside the 30-min heartbeat window)."""
    sess_stale = _stale_inactive_session()
    sess_stale["brief"] = "doing live-memory-console work"  # skip jsonl fallback
    sess_fresh = _fresh_active_session()
    sess_fresh["brief"] = "fresh active session"

    # Fake the parity ingest + ledger sync to no-ops so we exercise the
    # bucket logic in isolation. Real jsonl paths are created so
    # ``_extract_current_prompt`` / ``_extract_first_user_text`` have
    # something to stat / open if invoked.
    stale_jl = tmp_path / "stale.jsonl"
    fresh_jl = tmp_path / "fresh.jsonl"
    stale_jl.write_text("", encoding="utf-8")
    fresh_jl.write_text("", encoding="utf-8")
    monkeypatch.setattr(heartbeat, "_load_db_sessions",
                        lambda: [sess_stale, sess_fresh])
    monkeypatch.setattr(heartbeat, "_jsonl_lookup", lambda: {
        sess_stale["uuid"]: {
            "jsonl": stale_jl,
            "mtime": _aged(4.6),
            "subs": [],
        },
        sess_fresh["uuid"]: {
            "jsonl": fresh_jl,
            "mtime": heartbeat.NOW - _dt.timedelta(minutes=2),
            "subs": [],
        },
    })
    # Skip the parity-ingest sqlite calls.
    import worker_control_hermes.legacy_parity_ingest as _li
    monkeypatch.setattr(_li, "ingest_all", lambda *a, **k: {})
    # Fake the subprocs scan: stale session has an "alive" workload subproc
    # that previously overrode last → ALIVE. Fresh session has none.
    fake_subprocs = type("M", (), {})()
    def _scan_and_persist(conn, now=None):
        return ([], {"alive": 1, "ended_now": 0, "kept": 0})
    def _claude_session_status(_uuid):
        return None
    def _claude_session_tasks(_uuid):
        return []
    fake_subprocs.scan_and_persist     = _scan_and_persist
    fake_subprocs.claude_session_status = _claude_session_status
    fake_subprocs.claude_session_tasks  = _claude_session_tasks
    monkeypatch.setattr(heartbeat, "_subprocs_mod", fake_subprocs)

    # Skip the embedded sqlite re-fetch loops by pointing DB_PATH at a
    # non-existent file — the code tolerates the missing DB for the
    # subprocs_by_uuid map and the sweep-candidates query (both wrapped
    # in try/except).
    from pathlib import Path
    monkeypatch.setattr(heartbeat, "DB_PATH", Path("/nonexistent.sqlite3"))

    snap = heartbeat.classify_sessions(window_min=30)

    alive_uuids = {s["uuid"] for s in snap["alive"]}
    idle_uuids  = {s["uuid"] for s in snap["idle"]}
    just_ended_uuids = {s["uuid"] for s in snap["just_ended"]}

    # The stale session must NOT be in ALIVE.
    assert sess_stale["uuid"] not in alive_uuids, (
        "regression: nat#f75febed-shaped session promoted to ALIVE"
    )
    # And because its mtime is 4h+ old (well past the 30-min window) it
    # should not appear anywhere in the snapshot.
    assert sess_stale["uuid"] not in idle_uuids
    assert sess_stale["uuid"] not in just_ended_uuids

    # Sanity: the fresh active mirror session IS still in ALIVE.
    assert sess_fresh["uuid"] in alive_uuids, (
        "fresh 'active' session should still bucket as ALIVE"
    )
