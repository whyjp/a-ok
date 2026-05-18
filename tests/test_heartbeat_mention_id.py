"""Tests for the mention-friendly ID surfaced in heartbeat output.

`_mention_id` derives a copy/paste-able identifier for every session and
the two render functions (`_line` for plaintext, `_section_text_for` for
Slack) embed it next to the human-readable session label. The four
cases below exercise each derivation branch and verify the rendered
output carries the ID.
"""
from __future__ import annotations

import datetime as _dt

from worker_control_hermes import heartbeat


def _base_session(**overrides) -> dict:
    """Minimum dict shape needed by `_line` / `_section_text_for`."""
    s: dict = {
        "id": 1,
        "uuid": "deadbeef-1111-2222-3333-444455556666",
        "name": None,
        "origin": "native",
        "status": "active",
        "display_status": "active",
        "brief": None,
        "model": "claude-sonnet-4-5",
        "last_used_at": "2026-05-18T11:55:00Z",
        "ended_at": None,
        "created_at": "2026-05-18T10:00:00Z",
        "claude_name": None,
        "claude_status": None,
        "claude_status_at": None,
        "proj_name": "a-ok",
        "proj_path": "D:/work-github/a-ok",
        "msg_tool": 0,
        "runs": [],
        "_last": _dt.datetime(2026, 5, 18, 11, 55, 0, tzinfo=_dt.timezone.utc),
        "_subagents": 0,
        "_subagents_active": 0,
        "_subprocs_alive": [],
        "_subprocs_just_ended": [],
        "_claude_status": None,
        "_claude_name": None,
        "_active_tasks": [],
        "_summary": "",
        "_current_prompt": "",
        "_pending": False,
    }
    s.update(overrides)
    return s


# ---------------------------------------------------------------------------
# Case 1 — spawned with the hermes `a-ok:<slug>` prefix already in place.
# ---------------------------------------------------------------------------

def test_mention_id_spawned_with_prefix() -> None:
    s = _base_session(origin="spawned", name="a-ok:scv-foo")
    assert heartbeat._mention_id(s) == "a-ok:scv-foo"


def test_renderers_carry_spawned_prefix_id() -> None:
    s = _base_session(origin="spawned", name="a-ok:scv-foo")
    line = heartbeat._line(s)
    section = heartbeat._section_text_for(s, "alive")
    assert "a-ok:scv-foo" in line
    assert "a-ok:scv-foo" in section


# ---------------------------------------------------------------------------
# Case 2 — spawned with a legacy non-prefixed name → uuid-short fallback.
# ---------------------------------------------------------------------------

def test_mention_id_spawned_legacy() -> None:
    s = _base_session(
        origin="spawned",
        name="legacy-name",
        uuid="cafebabe-aaaa-bbbb-cccc-ddddeeeeffff",
    )
    assert heartbeat._mention_id(s) == "a-ok:cafebabe"


def test_renderers_carry_spawned_legacy_id() -> None:
    s = _base_session(
        origin="spawned",
        name="legacy-name",
        uuid="cafebabe-aaaa-bbbb-cccc-ddddeeeeffff",
    )
    line = heartbeat._line(s)
    section = heartbeat._section_text_for(s, "alive")
    assert "a-ok:cafebabe" in line
    assert "a-ok:cafebabe" in section


# ---------------------------------------------------------------------------
# Case 3 — native session, claude-code provided a session name.
# ---------------------------------------------------------------------------

def test_mention_id_native_with_claude_name() -> None:
    s = _base_session(
        origin="native",
        name=None,
        _claude_name="3day_sampler",
    )
    assert heartbeat._mention_id(s) == "native:3day_sampler"


def test_renderers_carry_native_claude_name_id() -> None:
    s = _base_session(
        origin="native",
        name=None,
        _claude_name="3day_sampler",
    )
    line = heartbeat._line(s)
    section = heartbeat._section_text_for(s, "alive")
    assert "native:3day_sampler" in line
    assert "native:3day_sampler" in section


# ---------------------------------------------------------------------------
# Case 4 — native session, no claude_name and no user-set name → uuid-short.
# ---------------------------------------------------------------------------

def test_mention_id_native_empty_falls_back_to_uuid() -> None:
    s = _base_session(
        origin="native",
        name=None,
        _claude_name=None,
        uuid="abcd1234-1111-2222-3333-444455556666",
    )
    assert heartbeat._mention_id(s) == "native:abcd1234"


def test_renderers_carry_native_uuid_fallback_id() -> None:
    s = _base_session(
        origin="native",
        name=None,
        _claude_name=None,
        uuid="abcd1234-1111-2222-3333-444455556666",
    )
    line = heartbeat._line(s)
    section = heartbeat._section_text_for(s, "alive")
    assert "native:abcd1234" in line
    assert "native:abcd1234" in section


# ---------------------------------------------------------------------------
# Deduplication — when the mention_id matches `name` exactly (e.g. a
# native session whose user-set name is already `native:something`), the
# title row must not render the ID twice.
# ---------------------------------------------------------------------------

def test_renderers_dedupe_when_name_equals_mention_id() -> None:
    s = _base_session(
        origin="native",
        name="native:explicit",
        _claude_name=None,
    )
    assert heartbeat._mention_id(s) == "native:explicit"
    line = heartbeat._line(s)
    section = heartbeat._section_text_for(s, "alive")
    assert line.count("native:explicit") == 1, line
    assert section.count("native:explicit") == 1, section
