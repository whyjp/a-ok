"""Tests for the dashboard supervisor (worker_control.autostart)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from worker_control import autostart, db, server


def test_probe_health_returns_none_when_nothing_listening():
    # port 1 is virtually never bound; probe must fail fast and quietly.
    assert autostart.probe_health("127.0.0.1", 1, timeout=0.5) is None


def test_probe_health_against_running_server(tmp_path: Path):
    db.init_db()
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0, log_sink=None,
    )
    try:
        h = autostart.probe_health("127.0.0.1", srv.port, timeout=2.0)
        assert h is not None
        assert h["ok"] is True
        assert h["service"] == "worker-control"
    finally:
        srv.shutdown(); srv.server_close(); thread.join(timeout=2)


def test_compute_signature_detects_changes(tmp_path: Path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.html"
    a.write_text("x = 1\n", encoding="utf-8")
    b.write_text("<html></html>", encoding="utf-8")
    paths = (a, b)
    sig0 = autostart.compute_signature(paths)
    # 같은 내용에서 다시 잡으면 동일.
    assert autostart.compute_signature(paths) == sig0
    # 파일을 수정하면 시그니처가 바뀐다.
    time.sleep(0.02)
    a.write_text("x = 2\n", encoding="utf-8")
    # mtime resolution 보정용으로 명시 set.
    new_t = a.stat().st_mtime + 1.0
    import os as _os
    _os.utime(a, (new_t, new_t))
    sig1 = autostart.compute_signature(paths)
    assert sig1 != sig0


def test_compute_signature_handles_missing_files(tmp_path: Path):
    missing = tmp_path / "does-not-exist.py"
    sig = autostart.compute_signature((missing,))
    assert sig[0][0] == str(missing)
    assert sig[0][2] == -1  # placeholder size


def test_default_watch_paths_include_server_and_static_html():
    paths = {p.name for p in autostart.DEFAULT_WATCH_PATHS}
    assert "server.py" in paths
    assert "dashboard.py" in paths
    assert "dashboard.html" in paths


def test_ensure_running_detects_existing_server(tmp_path: Path):
    """이미 떠 있으면 spawn=False 로 빠르게 반환해야 한다."""
    db.init_db()
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0, log_sink=None,
    )
    try:
        res = autostart.ensure_running(host="127.0.0.1", port=srv.port)
        assert res.spawned is False
        assert res.already_running is True
        assert res.health is not None
        assert res.health["ok"] is True
    finally:
        srv.shutdown(); srv.server_close(); thread.join(timeout=2)


def test_health_url_normalizes_wildcard_host():
    assert autostart.health_url("0.0.0.0", 8765).startswith(
        "http://127.0.0.1:8765/"
    )
    assert autostart.health_url("127.0.0.1", 9000).startswith(
        "http://127.0.0.1:9000/"
    )
