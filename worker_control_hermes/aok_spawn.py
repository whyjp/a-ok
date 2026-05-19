#!/usr/bin/env python
"""
aok-spawn — PM dispatcher helper that runs the bash-wrapped claude command
emitted by ``workerctl-hermes-projects {session,run} start --json`` and
guarantees the ledger ``run end`` trap fires.

Why this exists
---------------
PM agents used to drop the ``command`` string returned by the JSON dispatcher
into a temporary ``cmd-*.sh`` and launch it via background bash. That bash
started as a fresh login shell that did NOT have the workerctl venv on PATH,
so ``workerctl-hermes-projects run end ...`` in the EXIT trap silently failed
with ``command not found``. The row in ``hermes_runs`` stayed
``status='started'`` and the dashboard's spawn classifier (which only counts
rows with a closed status) under-reported actual spawns.

aok-spawn moves the PATH bootstrap, the trap wrapping, and the
foreground/detach plumbing into one CLI so the PM agent has exactly one
contract to follow:

    aok-spawn --run-id <id> --inline-cmd "<dispatcher command>"

Behaviour
---------
* Prepends the workerctl venv bin dir to ``PATH`` (idempotent).
* Detects whether the input command is already trap-wrapped (matches
  ``( trap '...' EXIT; ...``) and wraps it only when missing. Double-wrap
  would still work but would call ``run end`` twice; the explicit
  detection keeps audit logs clean.
* Runs the command under bash with stdin closed (``< /dev/null`` semantics)
  so ``claude --print`` doesn't sit waiting for stdin data.
* Foreground mode propagates the real exit code; detach mode launches the
  command in the background and prints the PID.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# An already-wrapped command starts with "( trap '...' EXIT;". We match
# liberally on whitespace so commands round-tripped through a here-doc or
# extra padding still detect.
_TRAP_PREFIX_RE = re.compile(r"^\s*\(\s*trap\s+'", re.DOTALL)


def _venv_bin_dir() -> Path:
    """Return the platform-correct workerctl venv bin/Scripts directory."""
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "hermes" / "workerctl-venv" / "Scripts"
    return Path.home() / ".local" / "share" / "hermes" / "workerctl-venv" / "bin"


def ensure_path_bootstrap(env: dict[str, str] | None = None) -> bool:
    """Prepend the workerctl venv bin dir to ``PATH`` if not already there.

    Mutates ``env`` (or ``os.environ`` when ``env`` is None) in place.
    Returns True when PATH was modified, False when the bin dir was
    already first-or-present.
    """
    target = env if env is not None else os.environ
    bin_dir = str(_venv_bin_dir())
    current = target.get("PATH", "")
    sep = os.pathsep
    parts = [p for p in current.split(sep) if p]
    if any(os.path.normcase(p) == os.path.normcase(bin_dir) for p in parts):
        return False
    target["PATH"] = bin_dir + (sep + current if current else "")
    return True


def is_already_wrapped(cmd: str) -> bool:
    """True if ``cmd`` already starts with our ``( trap '...' EXIT;`` form."""
    return bool(_TRAP_PREFIX_RE.match(cmd))


def _trap_body(run_id: int) -> str:
    """Build the single-quoted EXIT trap body.

    Kept structurally identical to ``worker_control_hermes.projects.
    _wrap_self_close``'s trap body. The trap is best-effort
    (``|| true``) so a missing entry point on PATH never masks the real
    exit code. See ``tests/test_aok_spawn.py::test_trap_body_matches_projects``
    for the lock-in.
    """
    return (
        "__rc=$?; "
        f"workerctl-hermes-projects run end {run_id} "
        "--status $( [ \"$__rc\" = \"0\" ] && echo done || echo failed ) "
        "--note \"exit=$__rc\" >/dev/null 2>&1 || true"
    )


def wrap_with_runend_trap(cmd: str, run_id: int) -> str:
    """Wrap ``cmd`` in ``( trap '...' EXIT; <cmd> )``. Idempotent."""
    if is_already_wrapped(cmd):
        return cmd
    return f"( trap '{_trap_body(run_id)}' EXIT; {cmd} )"


def _resolve_command(args: argparse.Namespace) -> str:
    if bool(args.cmd_file) == bool(args.inline_cmd):
        raise SystemExit(
            "aok-spawn: exactly one of --cmd-file or --inline-cmd must be given"
        )
    if args.cmd_file:
        path = Path(args.cmd_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"aok-spawn: --cmd-file not found: {path}")
        return path.read_text(encoding="utf-8")
    return args.inline_cmd


def _default_log_path(run_id: int) -> Path:
    tmp = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(tmp) / f"aok-spawn-run-{run_id}.log"


def _run_foreground(wrapped: str, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # stdin=DEVNULL is non-negotiable: ``claude --print`` keeps the FD open
    # waiting for stdin data otherwise, which deadlocks the worker.
    with log_path.open("ab") as log_fp:
        proc = subprocess.run(  # noqa: S603 — bash invocation is the contract
            ["bash", "-c", wrapped],
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
        )
    return proc.returncode


def _spawn_detached(wrapped: str, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — keeps the child alive
        # after the parent exits and disconnects it from this console.
        creationflags = 0x00000008 | 0x00000200
    proc = subprocess.Popen(  # noqa: S603 — bash invocation is the contract
        ["bash", "-c", wrapped],
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        close_fds=True,
    )
    return proc.pid


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aok-spawn",
        description=(
            "Run a dispatcher-emitted claude command with PATH bootstrap "
            "and EXIT-trap ledger close. See worker_control_hermes.aok_spawn "
            "module docstring for context."
        ),
    )
    p.add_argument("--run-id", type=int, required=True,
                   help="hermes_runs.id allocated by `workerctl-hermes-projects "
                        "{session,run} start` before this call.")
    p.add_argument("--cmd-file", help="path to a .sh containing the command "
                                       "to run (mutually exclusive with --inline-cmd)")
    p.add_argument("--inline-cmd", help="command string to run "
                                         "(mutually exclusive with --cmd-file)")
    p.add_argument("--log", help="log file path (default: $TEMP/aok-spawn-run-<id>.log)")
    p.add_argument("--no-trap", action="store_true",
                   help="debug: skip the EXIT trap wrap (the input command "
                        "runs verbatim — only use when you're going to call "
                        "`run end` yourself).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--foreground", dest="detach", action="store_false",
                      help="run synchronously and propagate exit code (default)")
    mode.add_argument("--detach", dest="detach", action="store_true",
                      help="launch in background, print PID, exit immediately")
    p.set_defaults(detach=False)
    p.add_argument("--json", action="store_true",
                   help="print a single-line JSON summary on stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    path_prepended = ensure_path_bootstrap()

    cmd = _resolve_command(args)
    trap_already_present = is_already_wrapped(cmd)
    if args.no_trap:
        wrapped = cmd
    else:
        wrapped = wrap_with_runend_trap(cmd, args.run_id)

    log_path = Path(args.log).expanduser() if args.log else _default_log_path(args.run_id)

    if args.detach:
        pid = _spawn_detached(wrapped, log_path)
        if args.json:
            print(json.dumps({
                "run_id": args.run_id,
                "log": str(log_path),
                "pid": pid,
                "exit_code": None,
                "trap_already_present": trap_already_present,
                "path_prepended": path_prepended,
                "detached": True,
            }, ensure_ascii=False))
        else:
            print(f"aok-spawn: detached pid={pid} log={log_path}")
        return 0

    rc = _run_foreground(wrapped, log_path)
    if args.json:
        print(json.dumps({
            "run_id": args.run_id,
            "log": str(log_path),
            "pid": None,
            "exit_code": rc,
            "trap_already_present": trap_already_present,
            "path_prepended": path_prepended,
            "detached": False,
        }, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
