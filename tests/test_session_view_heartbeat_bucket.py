"""Unit tests for ``session_view.heartbeat_bucket``.

The classifier is the single source of truth shared between heartbeat
and the dashboard for "which Slack bucket does a session belong to".
The cases below pin every return value plus the cross-validation guard
that closed the nat#f75febed regression (stale jsonl + live subproc must
not promote to ALIVE).
"""
from __future__ import annotations

import datetime as _dt

from worker_control.session_view import heartbeat_bucket


_NOW = _dt.datetime(2026, 5, 20, 12, 46, 57, tzinfo=_dt.timezone.utc)


def _ago(seconds: int) -> _dt.datetime:
    return _NOW - _dt.timedelta(seconds=seconds)


def test_active_within_5min_is_alive() -> None:
    assert heartbeat_bucket(
        display_status="active",
        last_activity=_ago(60),
        ended_at=None,
        now=_NOW,
    ) == "alive"


def test_active_5_to_30min_is_idle() -> None:
    assert heartbeat_bucket(
        display_status="active",
        last_activity=_ago(15 * 60),
        ended_at=None,
        now=_NOW,
    ) == "idle"


def test_inactive_beyond_window_drops() -> None:
    # 4h38m ≈ the nat#f75febed mtime delta — the parity layer has marked
    # the session 'inactive' and the heartbeat must NOT surface it even if
    # other live signals (subprocs, claude=busy) used to override.
    assert heartbeat_bucket(
        display_status="inactive",
        last_activity=_ago(int(4.6 * 3600)),
        ended_at=None,
        now=_NOW,
    ) is None


def test_inactive_within_window_is_idle() -> None:
    # Rare edge: jsonl is fresh (< window) but parity hasn't ingested yet
    # and the dashboard recency-fallback still puts it at 'inactive'.
    # We don't promote to ALIVE — IDLE is the conservative pick.
    assert heartbeat_bucket(
        display_status="inactive",
        last_activity=_ago(10 * 60),
        ended_at=None,
        now=_NOW,
    ) == "idle"


def test_done_within_window_with_recent_ended_at_is_just_ended() -> None:
    ended = (_NOW - _dt.timedelta(minutes=10)).isoformat()
    assert heartbeat_bucket(
        display_status="done",
        last_activity=_ago(10 * 60),
        ended_at=ended,
        now=_NOW,
    ) == "just_ended"


def test_done_without_ended_at_drops() -> None:
    # A 'done' display_status without a fresh ended_at — likely a stale
    # row past the just-ended grace. Drop, don't show.
    assert heartbeat_bucket(
        display_status="done",
        last_activity=_ago(10 * 60),
        ended_at=None,
        now=_NOW,
    ) is None


def test_done_with_old_ended_at_drops() -> None:
    # ended_at is 2 days old → outside window.
    old_ended = (_NOW - _dt.timedelta(days=2)).isoformat()
    assert heartbeat_bucket(
        display_status="done",
        last_activity=_ago(10 * 60),
        ended_at=old_ended,
        now=_NOW,
    ) is None


def test_no_last_activity_returns_none() -> None:
    assert heartbeat_bucket(
        display_status="active",
        last_activity=None,
        ended_at=None,
        now=_NOW,
    ) is None


def test_unknown_display_status_falls_through_to_idle() -> None:
    # NULL/empty display_status should not promote to ALIVE — IDLE is the
    # safe pick when we don't have a parity verdict.
    assert heartbeat_bucket(
        display_status=None,
        last_activity=_ago(60),
        ended_at=None,
        now=_NOW,
    ) == "idle"
