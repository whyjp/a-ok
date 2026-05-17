"""Process runtime: tmux preferred, OS console fallback.

정책 (workspace-policy v0.2):
- Claude Code 워커는 항상 ``claude --permission-mode auto`` 일반 모드로 띄운다.
- ``claude -p`` / ``--print`` (print 모드) 는 **절대 사용하지 않는다.**
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


CLAUDE_BIN = os.environ.get("WORKER_CONTROL_CLAUDE_BIN", "claude")
# 정책상 기본 인자. ``claude --permission-mode auto`` 를 강제한다.
CLAUDE_DEFAULT_ARGS: tuple[str, ...] = ("--permission-mode", "auto")
# 정책상 절대 포함되어서는 안 되는 인자.
FORBIDDEN_CLAUDE_ARGS: frozenset[str] = frozenset({"-p", "--print"})


def claude_argv() -> list[str]:
    """워커 기동에 사용할 argv 를 생성한다.

    - 반드시 ``--permission-mode auto`` 를 포함한다.
    - ``-p`` / ``--print`` 가 끼어들면 즉시 예외를 던진다 (정책 위반).
    """
    argv = [CLAUDE_BIN, *CLAUDE_DEFAULT_ARGS]
    bad = FORBIDDEN_CLAUDE_ARGS.intersection(argv)
    if bad:
        raise RuntimeError(
            f"policy violation: forbidden claude args present: {sorted(bad)} "
            "(claude -p / --print 모드는 워커에서 금지됩니다)"
        )
    return argv


@dataclass(slots=True)
class LaunchResult:
    runtime: str          # 'tmux' or 'console'
    tmux_session: str | None
    pid: int | None
    note: str


def have_tmux() -> bool:
    return shutil.which("tmux") is not None


def _quote_for_tmux(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def launch_tmux(session_name: str, cwd: Path) -> LaunchResult:
    """Start a detached tmux session running `claude --permission-mode auto` in `cwd`."""
    argv = claude_argv()
    cmd = ["tmux", "new-session", "-d", "-s", session_name, "-c", str(cwd), *argv]
    subprocess.run(cmd, check=True)
    return LaunchResult(
        runtime="tmux", tmux_session=session_name, pid=None,
        note=f"tmux session '{session_name}' started with {' '.join(argv)}",
    )


def launch_console(cwd: Path) -> LaunchResult:
    """Open a new OS console window running `claude --permission-mode auto` (no tmux).

    Windows: `start "" cmd /k claude --permission-mode auto` via cmd.exe (so /k keeps the window).
    POSIX:   prefer x-terminal-emulator, fall back to xterm.
    """
    argv = claude_argv()
    if sys.platform.startswith("win"):
        # `start` is a cmd.exe builtin, so we must invoke through cmd /c.
        # Quote args carefully — first quoted token after `start` is the window title.
        cmd = ["cmd", "/c", "start", "", "cmd", "/k", *argv]
        proc = subprocess.Popen(cmd, cwd=str(cwd))
        return LaunchResult(
            runtime="console", tmux_session=None, pid=proc.pid,
            note="Windows console window opened; pid is the spawner, not claude itself",
        )
    # POSIX without tmux
    term = shutil.which("x-terminal-emulator") or shutil.which("xterm")
    if not term:
        raise RuntimeError(
            "No terminal emulator found and tmux unavailable; "
            "install tmux to enable session control."
        )
    proc = subprocess.Popen([term, "-e", *argv], cwd=str(cwd))
    return LaunchResult(
        runtime="console", tmux_session=None, pid=proc.pid,
        note=f"opened via {term} (no tmux)",
    )


def launch(session_name: str, cwd: Path, prefer_tmux: bool = True) -> LaunchResult:
    """Start a Claude Code worker. Never uses `claude -p`."""
    if prefer_tmux and have_tmux():
        return launch_tmux(session_name, cwd)
    return launch_console(cwd)


def tmux_capture(session_name: str) -> str:
    """Return the current screen content of a tmux session."""
    out = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session_name],
        capture_output=True, text=True, check=False,
    )
    return out.stdout


def tmux_send(session_name: str, text: str) -> None:
    """Send text + Enter to a tmux session."""
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, text, "Enter"],
        check=True,
    )


def tmux_kill(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        check=False,
    )


def kill_pid(pid: int) -> None:
    """Best-effort kill for the console fallback."""
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
        except ProcessLookupError:
            pass
