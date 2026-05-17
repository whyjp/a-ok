"""Runtime policy: 워커는 항상 ``claude --permission-mode auto`` 로 기동된다.

``claude -p`` / ``--print`` (print 모드) 가 끼어 있으면 정책 위반으로 거부한다.
"""
from __future__ import annotations

import pytest

from worker_control import runtime


def test_claude_argv_starts_with_bin():
    argv = runtime.claude_argv()
    assert argv[0] == runtime.CLAUDE_BIN


def test_claude_argv_includes_permission_mode_auto():
    argv = runtime.claude_argv()
    assert "--permission-mode" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "auto"


def test_claude_argv_never_includes_print_mode():
    argv = runtime.claude_argv()
    assert "-p" not in argv
    assert "--print" not in argv


def test_claude_default_args_constant_does_not_contain_print_flags():
    assert "-p" not in runtime.CLAUDE_DEFAULT_ARGS
    assert "--print" not in runtime.CLAUDE_DEFAULT_ARGS


def test_claude_argv_rejects_forbidden_args(monkeypatch):
    bad = ("--permission-mode", "auto", "-p")
    monkeypatch.setattr(runtime, "CLAUDE_DEFAULT_ARGS", bad)
    with pytest.raises(RuntimeError, match="policy violation"):
        runtime.claude_argv()


def test_launch_tmux_command_includes_auto_mode(monkeypatch, tmp_path):
    """tmux 분기에서 실제로 ``--permission-mode auto`` 가 명령에 들어가는지 검증."""
    captured: dict = {}

    def fake_run(cmd, check):
        captured["cmd"] = list(cmd)
        captured["check"] = check
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    result = runtime.launch_tmux("sess-test", tmp_path)
    assert result.runtime == "tmux"
    assert "--permission-mode" in captured["cmd"]
    assert "auto" in captured["cmd"]
    assert "-p" not in captured["cmd"]
    # tmux 헤더 옵션은 보존되어야 한다.
    assert captured["cmd"][:6] == [
        "tmux", "new-session", "-d", "-s", "sess-test", "-c",
    ]
