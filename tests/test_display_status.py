"""Unit tests for ``session_view._compute_display_status``.

The display_status field is the dashboard's single source of truth for
the active / inactive / done pills. Five cases pin the rule so a future
edit to the helper can't silently shift one of the buckets.
"""
from __future__ import annotations

import datetime as _dt

from worker_control.session_view import _compute_display_status


# Fix "now" so the recency thresholds are deterministic.
_NOW = _dt.datetime(2026, 5, 18, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _iso(minutes_ago: int) -> str:
    return (_NOW - _dt.timedelta(minutes=minutes_ago)).isoformat()


def test_ended_at_forces_done() -> None:
    # Even if last_used_at is within the 2h active window, an ended_at
    # always wins. The session is terminal.
    assert _compute_display_status(
        status="active", ended_at="2026-05-18T11:30:00+00:00",
        effective_status=None, last_used_at=_iso(5), now=_NOW,
    ) == "done"


def test_terminal_status_forces_done() -> None:
    # Ledger status in the terminal set wins over any recency signal.
    for st in ("done", "failed", "abandoned"):
        assert _compute_display_status(
            status=st, ended_at=None,
            effective_status="active", last_used_at=_iso(1), now=_NOW,
        ) == "done", f"status={st} should force done"


def test_effective_status_active_trusted_when_recency_agrees() -> None:
    # Parity layer says 'active' AND last_used_at is fresh — believed.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status="active", last_used_at=_iso(10), now=_NOW,
    ) == "active"


def test_effective_status_active_downgraded_when_stale() -> None:
    # Defense-in-depth: a stale 'active' parity flag (write-path watermark
    # never re-ran the bucket recompute) gets cross-validated against
    # last_used_at and downgraded to inactive/done. This guards the
    # dashboard against a future regression in the ingest layer.
    # 5h since last activity → inactive even though parity says active.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status="active", last_used_at=_iso(60 * 5), now=_NOW,
    ) == "inactive"
    # 30h since last activity → done.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status="active", last_used_at=_iso(60 * 30), now=_NOW,
    ) == "done"


def test_effective_status_active_trusted_when_no_recency_signal() -> None:
    # No last_used_at to cross-validate with — trust the parity flag rather
    # than silently demote a session that may genuinely be active.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status="active", last_used_at=None, now=_NOW,
    ) == "active"


def test_recent_last_used_within_2h_is_active() -> None:
    # 30 minutes old — well inside the 2h active window.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status=None, last_used_at=_iso(30), now=_NOW,
    ) == "active"


def test_between_2h_and_24h_is_inactive() -> None:
    # 5 hours old — outside active, inside inactive.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status=None, last_used_at=_iso(60 * 5), now=_NOW,
    ) == "inactive"


def test_over_24h_is_done() -> None:
    # 30 hours old — outside both windows, treated as done.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status=None, last_used_at=_iso(60 * 30), now=_NOW,
    ) == "done"


def test_missing_last_used_falls_back_inactive() -> None:
    # A row with no timestamp shouldn't be greener than rows that have
    # one — we anchor unknowns at "inactive" so the dashboard doesn't
    # mislead the operator.
    assert _compute_display_status(
        status="active", ended_at=None,
        effective_status=None, last_used_at=None, now=_NOW,
    ) == "inactive"
