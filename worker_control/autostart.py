"""대시보드 BFF 의 상시 실행 supervisor.

Hermes 시작 시 ``workerctl dashboard daemon`` (또는 직접
``python -m worker_control.autostart``) 으로 한 번 호출하면:

* 이미 떠 있으면 health-check 만 통과시키고 즉시 종료.
* 없으면 subprocess 로 ``workerctl view serve`` 를 띄우고 backgrounding.
* ``--watch`` (기본 켜짐) 이면 worker_control/* 와 static 파일의
  mtime 을 polling 해서 코드가 바뀌면 subprocess 를 안전하게 재시작.

stdlib 전용. Windows + Git-Bash 환경에서 동작한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from worker_control.server import DEFAULT_HOST, DEFAULT_PORT


_PKG_DIR = Path(__file__).resolve().parent
# mtime 을 감시할 파일 목록 — 백엔드 코드 + 정적 FE.
DEFAULT_WATCH_PATHS: tuple[Path, ...] = tuple(
    sorted(
        list(_PKG_DIR.glob("*.py"))
        + list((_PKG_DIR / "static").glob("*.html"))
    )
)


def health_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    h = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{h}:{port}/api/health"


def probe_health(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    timeout: float = 1.5,
) -> dict | None:
    """``/api/health`` 를 한 번 찔러본다. 성공 시 payload, 실패 시 ``None``."""
    url = health_url(host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data
    return None


def compute_signature(paths: tuple[Path, ...] = DEFAULT_WATCH_PATHS) -> tuple:
    """감시 대상 파일 집합의 mtime/size 시그니처."""
    sig: list[tuple[str, float, int]] = []
    for p in paths:
        try:
            st = p.stat()
        except (FileNotFoundError, OSError):
            sig.append((str(p), 0.0, -1))
            continue
        sig.append((str(p), st.st_mtime, st.st_size))
    return tuple(sig)


def _serve_argv(
    host: str,
    port: int,
    *,
    allow_remote: bool,
    db: str | None,
    runtime_root: str | None,
    native_limit: int,
) -> list[str]:
    argv = [sys.executable, "-m", "worker_control",
            "view", "serve",
            "--host", host, "--port", str(port),
            "--native-limit", str(native_limit)]
    if allow_remote:
        argv.append("--allow-remote")
    if db:
        argv.extend(["--db", db])
    if runtime_root:
        argv.extend(["--runtime-root", runtime_root])
    return argv


def _spawn(
    host: str,
    port: int,
    *,
    allow_remote: bool,
    db: str | None,
    runtime_root: str | None,
    native_limit: int,
    log_path: Path | None,
) -> subprocess.Popen:
    argv = _serve_argv(
        host, port,
        allow_remote=allow_remote,
        db=db, runtime_root=runtime_root,
        native_limit=native_limit,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "ab", buffering=0)
        stdout = log_fp
        stderr = log_fp
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
    # 자식이 부모 console 의 Ctrl-C 시그널을 받지 않도록 새 그룹/세션에.
    kwargs: dict = {"stdout": stdout, "stderr": stderr,
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _terminate(proc: subprocess.Popen, *, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def wait_for_health(
    host: str,
    port: int,
    *,
    timeout: float = 10.0,
    interval: float = 0.2,
) -> dict | None:
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        last = probe_health(host, port, timeout=1.0)
        if last is not None:
            return last
        time.sleep(interval)
    return last


@dataclass
class DaemonResult:
    spawned: bool
    already_running: bool
    health: dict | None
    note: str


def ensure_running(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
    db: str | None = None,
    runtime_root: str | None = None,
    native_limit: int = 500,
    log_path: Path | None = None,
    startup_timeout: float = 10.0,
) -> DaemonResult:
    """이미 떠 있으면 그대로 두고, 없으면 detached subprocess 로 띄운다.

    이 함수는 supervising 을 하지 않는다 — 떠 있는지 확인하고 (없으면 띄우고)
    health 통과까지만 기다린 뒤 바로 반환한다. 코드 변경 감시까지 원하면
    ``run_supervisor`` 를 쓴다.
    """
    existing = probe_health(host, port)
    if existing is not None:
        return DaemonResult(
            spawned=False, already_running=True,
            health=existing, note=f"already running on {host}:{port}",
        )

    _spawn(
        host, port,
        allow_remote=allow_remote,
        db=db, runtime_root=runtime_root,
        native_limit=native_limit, log_path=log_path,
    )
    h = wait_for_health(host, port, timeout=startup_timeout)
    if h is None:
        return DaemonResult(
            spawned=True, already_running=False, health=None,
            note=(
                f"spawned but health check did not pass within "
                f"{startup_timeout:.0f}s — check logs at {log_path}"
            ),
        )
    return DaemonResult(
        spawned=True, already_running=False, health=h,
        note=f"spawned and healthy on {host}:{port}",
    )


def run_supervisor(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
    db: str | None = None,
    runtime_root: str | None = None,
    native_limit: int = 500,
    log_path: Path | None = None,
    watch_paths: tuple[Path, ...] = DEFAULT_WATCH_PATHS,
    poll_interval: float = 1.0,
    on_event=None,
) -> int:
    """포그라운드 supervisor — 자식 BFF 를 띄우고 코드 변경 시 재시작.

    이미 다른 supervisor 가 돌고 있으면 0 으로 즉시 종료 (중복 실행 금지).
    """
    if probe_health(host, port) is not None:
        _emit(on_event, "already-running",
              f"dashboard already healthy on {host}:{port}; supervisor exiting")
        return 0

    proc = _spawn(
        host, port,
        allow_remote=allow_remote, db=db, runtime_root=runtime_root,
        native_limit=native_limit, log_path=log_path,
    )
    _emit(on_event, "spawned", f"spawned dashboard pid={proc.pid}")
    sig = compute_signature(watch_paths)

    try:
        while True:
            try:
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                _emit(on_event, "interrupt", "supervisor interrupted")
                break

            # 자식이 죽었으면 재기동.
            rc = proc.poll()
            if rc is not None:
                _emit(on_event, "child-exit",
                      f"dashboard exited rc={rc}; respawning")
                proc = _spawn(
                    host, port,
                    allow_remote=allow_remote, db=db,
                    runtime_root=runtime_root,
                    native_limit=native_limit, log_path=log_path,
                )
                sig = compute_signature(watch_paths)
                continue

            new_sig = compute_signature(watch_paths)
            if new_sig != sig:
                _emit(on_event, "reload",
                      "watched files changed; restarting dashboard")
                _terminate(proc)
                proc = _spawn(
                    host, port,
                    allow_remote=allow_remote, db=db,
                    runtime_root=runtime_root,
                    native_limit=native_limit, log_path=log_path,
                )
                sig = new_sig
    finally:
        _terminate(proc)
    return 0


def _emit(on_event, kind: str, message: str) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, message)
    except Exception:
        pass


if __name__ == "__main__":
    # `python -m worker_control.autostart` 짧은 진입점.
    import argparse
    p = argparse.ArgumentParser(prog="worker_control.autostart")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-watch", action="store_true",
                   help="health-check 후 백그라운드로 띄우기만 하고 종료")
    p.add_argument("--allow-remote", action="store_true")
    p.add_argument("--db", default=None)
    p.add_argument("--runtime-root", default=None)
    p.add_argument("--native-limit", type=int, default=500)
    p.add_argument("--log", default=None,
                   help="자식 stdout/stderr 를 쌓을 로그 파일 경로")
    args = p.parse_args()
    log_path = Path(args.log) if args.log else None
    if args.no_watch:
        res = ensure_running(
            host=args.host, port=args.port,
            allow_remote=args.allow_remote,
            db=args.db, runtime_root=args.runtime_root,
            native_limit=args.native_limit,
            log_path=log_path,
        )
        print(res.note)
        sys.exit(0 if res.health is not None else 1)
    else:
        sys.exit(run_supervisor(
            host=args.host, port=args.port,
            allow_remote=args.allow_remote,
            db=args.db, runtime_root=args.runtime_root,
            native_limit=args.native_limit,
            log_path=log_path,
            on_event=lambda k, m: print(f"[autostart:{k}] {m}", flush=True),
        ))
