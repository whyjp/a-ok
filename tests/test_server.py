"""Tests for the worker-control dashboard HTTP backend."""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from worker_control import cli, db, profiles, scanner, server
from worker_control.paths import ROLE_OWNED_WORK, ROLE_PUBLIC_REFERENCE


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "main"], cwd=str(path), check=False,
    )


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch):
    """owned_work / public_reference 양쪽에 프로젝트를 만들고 스캔."""
    work = tmp_path / "work-github"
    pub = tmp_path / "github"
    work.mkdir()
    pub.mkdir()
    (work / "worker-control").mkdir()
    _git_init(work / "worker-control")
    (pub / "some-public-repo").mkdir()
    _git_init(pub / "some-public-repo")

    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    monkeypatch.setenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(pub))
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR",
        str(tmp_path / ".claude-projects"),
    )

    db.init_db()
    profiles.create_profile("default", root=str(work))
    scanner.scan_root(work)
    scanner.scan_root(pub)
    return tmp_path


@contextmanager
def _running_server(native_limit: int | None = 0) -> Iterator[server.DashboardServer]:
    """포트 0 으로 띄워서 ephemeral port 를 받고, 정리까지 책임진다."""
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=native_limit, log_sink=None,
    )
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _get(url: str, *, accept: str | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url)
    if accept:
        req.add_header("Accept", accept)
    return urllib.request.urlopen(req, timeout=timeout)


def test_health_endpoint(populated_db):
    with _running_server() as srv:
        with _get(srv.url + "api/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
    assert data["ok"] is True
    assert data["service"] == "worker-control"
    assert data["version"]


def test_snapshot_endpoint_returns_live_data(populated_db):
    with _running_server() as srv:
        with _get(srv.url + "api/snapshot", accept="application/json") as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            payload = json.loads(resp.read().decode("utf-8"))
    assert payload["counters"]["profiles"] == 1
    assert payload["counters"]["projects"] >= 2
    roles = {p["root_role"] for p in payload["projects"]}
    assert ROLE_OWNED_WORK in roles
    assert ROLE_PUBLIC_REFERENCE in roles


def test_snapshot_reflects_db_changes_between_requests(populated_db):
    """DB 가 갱신되면 다음 요청에서 자동으로 반영되어야 한다 (per-request 스냅샷)."""
    with _running_server() as srv:
        with _get(srv.url + "api/snapshot") as resp:
            before = json.loads(resp.read().decode("utf-8"))
        profiles.create_profile(
            "second", root=str(populated_db / "work-github"),
        )
        with _get(srv.url + "api/snapshot") as resp:
            after = json.loads(resp.read().decode("utf-8"))
    assert after["counters"]["profiles"] == before["counters"]["profiles"] + 1
    names = {p["name"] for p in after["profiles"]}
    assert "second" in names


def test_root_serves_html_dashboard(populated_db):
    with _running_server() as srv:
        with _get(srv.url) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
    assert body.startswith("<!DOCTYPE html>")
    assert "worker-control" in body
    # 라이브 모드용 코드/마커가 포함돼 있어야 한다
    assert "/api/snapshot" in body or "api/snapshot" in body
    assert "라이브" in body  # 라이브 모드 라벨


def test_dashboard_html_alias(populated_db):
    with _running_server() as srv:
        with _get(srv.url + "dashboard.html") as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
    assert body.startswith("<!DOCTYPE html>")


def test_unknown_path_returns_404(populated_db):
    with _running_server() as srv:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(srv.url + "no/such/path")
    assert exc_info.value.code == 404


def test_responses_have_no_store(populated_db):
    with _running_server() as srv:
        for path in ("", "api/snapshot", "api/health"):
            with _get(srv.url + path) as resp:
                assert resp.headers.get("Cache-Control") == "no-store"


def test_cli_refuses_non_loopback_without_allow_remote(populated_db, capsys):
    rc = cli.main(["view", "serve", "--host", "0.0.0.0", "--port", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--allow-remote" in err


def test_serve_in_thread_helper_clean_shutdown(populated_db):
    """serve_in_thread 가 만든 데몬 스레드는 shutdown 후 회수 가능해야 한다."""
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0, log_sink=None,
    )
    try:
        assert thread.is_alive()
        with _get(srv.url + "api/health") as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_root_serves_static_asset_unchanged(populated_db):
    """`GET /` 응답은 패키지의 정적 FE 자산과 동일해야 한다.

    매 요청마다 새 HTML 을 생성하던 옛 동작과 달리, BFF 는 정적 FE 한 장을
    그대로 서빙하고 데이터는 `/api/snapshot` 으로만 흘린다.
    """
    from worker_control.dashboard import static_dashboard_html
    expected = static_dashboard_html()
    with _running_server() as srv:
        with _get(srv.url) as resp:
            body = resp.read().decode("utf-8")
    assert body == expected
    # placeholder 가 그대로 살아 있으니, 인라인 데이터를 박지 않았다는 증거
    assert '"__INLINE_DATA__"' in body


def test_health_endpoint_exposes_db_path(populated_db):
    with _running_server() as srv:
        with _get(srv.url + "api/health") as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert data["db_exists"] is True
    assert data["db_path"].endswith("worker-control.sqlite3")
    assert data["runtime_root"]


def test_db_path_override_steers_bff(tmp_path: Path, monkeypatch):
    """--db 가 들어오면 그 DB 를 읽어야 한다 (다른 DB → 다른 결과)."""
    # 두 개의 격리된 DB 를 만든다.
    db_a = tmp_path / "a" / "wc.sqlite3"
    db_b = tmp_path / "b" / "wc.sqlite3"
    db_a.parent.mkdir(parents=True)
    db_b.parent.mkdir(parents=True)

    work = tmp_path / "work"
    work.mkdir()

    # DB-A 는 비워두고, DB-B 에만 profile 을 하나 만든다.
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_a))
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(work))
    db.init_db(db_a)

    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_b))
    db.init_db(db_b)
    profiles.create_profile("only-in-b", root=str(work))

    # 이제 BFF 를 DB-A 로 띄운다 → snapshot 의 profiles 수는 0 이어야 한다.
    srv_a, t_a = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0,
        db_path_override=str(db_a),
    )
    try:
        with _get(srv_a.url + "api/snapshot") as resp:
            payload_a = json.loads(resp.read().decode("utf-8"))
    finally:
        srv_a.shutdown(); srv_a.server_close(); t_a.join(timeout=2)
    assert payload_a["counters"]["profiles"] == 0

    # 같은 프로세스에서 DB-B 로 다시 띄우면 only-in-b 가 보여야 한다.
    srv_b, t_b = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0,
        db_path_override=str(db_b),
    )
    try:
        with _get(srv_b.url + "api/snapshot") as resp:
            payload_b = json.loads(resp.read().decode("utf-8"))
        with _get(srv_b.url + "api/health") as resp:
            health_b = json.loads(resp.read().decode("utf-8"))
    finally:
        srv_b.shutdown(); srv_b.server_close(); t_b.join(timeout=2)
    assert payload_b["counters"]["profiles"] == 1
    assert {p["name"] for p in payload_b["profiles"]} == {"only-in-b"}
    assert Path(health_b["db_path"]).resolve() == db_b.resolve()


def test_cli_dashboard_alias_is_wired(populated_db, capsys):
    """`workerctl dashboard --host 0.0.0.0` 도 --allow-remote 가드를 탄다."""
    rc = cli.main(["dashboard", "--host", "0.0.0.0", "--port", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--allow-remote" in err
