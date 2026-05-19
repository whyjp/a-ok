"""``heartbeat._mention_id`` renders the short copy/lookup id.

Same formula as the DB's GENERATED VIRTUAL ``mention_id`` column, so a
Slack mention can be pasted straight into ``WHERE mention_id = ?``.

Four shapes:
  * spawned + name              → ``aok#<uuid8>``
  * spawned, no name            → ``aok#<uuid8>``
  * native + claude_name        → ``nat#<uuid8>``
  * native, bare                → ``nat#<uuid8>``

The ``name`` / ``_claude_name`` fields are ignored by the new formula —
only ``origin`` and ``uuid`` matter. They're kept in the fixtures to
guard against accidental regressions that would resurrect the old
name-driven derivation.
"""
from __future__ import annotations

import re

from worker_control_hermes import heartbeat


_SHAPE_RE = re.compile(r"^(aok|nat)#[0-9a-f]{8}$")


def _session(**overrides) -> dict:
    s: dict = {
        "uuid": "abcdef12-aaaa-bbbb-cccc-ddddeeeeffff",
        "origin": "native",
        "name": None,
        "_claude_name": None,
    }
    s.update(overrides)
    return s


def test_spawned_with_name() -> None:
    mid = heartbeat._mention_id(_session(
        origin="spawned",
        uuid="abcdef12-aaaa-bbbb-cccc-ddddeeeeffff",
        name="a-ok:long-slug-name",
    ))
    assert mid == "aok#abcdef12"
    assert _SHAPE_RE.match(mid)


def test_spawned_without_name() -> None:
    mid = heartbeat._mention_id(_session(
        origin="spawned",
        uuid="cafebabe-1111-2222-3333-444455556666",
        name=None,
    ))
    assert mid == "aok#cafebabe"
    assert _SHAPE_RE.match(mid)


def test_native_with_claude_name() -> None:
    mid = heartbeat._mention_id(_session(
        origin="native",
        uuid="12345678-aaaa-bbbb-cccc-ddddeeeeffff",
        _claude_name="3day_sampler",
    ))
    assert mid == "nat#12345678"
    assert _SHAPE_RE.match(mid)


def test_native_bare() -> None:
    mid = heartbeat._mention_id(_session(
        origin="native",
        uuid="deadbeef-1111-2222-3333-444455556666",
        name=None,
        _claude_name=None,
    ))
    assert mid == "nat#deadbeef"
    assert _SHAPE_RE.match(mid)


def test_unknown_origin_falls_back_to_native_prefix() -> None:
    """``origin`` other than 'spawned' takes the native prefix — matches
    the SQL ``CASE origin WHEN 'spawned' THEN 'aok#' ELSE 'nat#' END``
    so DB and renderer stay in lockstep."""
    mid = heartbeat._mention_id(_session(
        origin="???",
        uuid="aabbccdd-1111-2222-3333-444455556666",
    ))
    assert mid == "nat#aabbccdd"
    assert _SHAPE_RE.match(mid)
