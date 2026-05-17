"""HTTP backend-for-frontend (BFF) for the worker-control dashboard.

worker-control 의 대시보드 FE 는 정적 자산
(``worker_control/static/dashboard.html``) 로 packaging 되고, 이 모듈이 그
자산을 그대로 서빙하면서 SQLite 를 읽어 ``/api/snapshot`` 으로 JSON 을
공급한다 — 즉 정형적인 **backend-for-frontend** 패턴이다.

매 요청마다 DB 를 새로 읽으므로 ``workerctl projects scan`` /
``workerctl sessions start`` 결과는 다음 polling 에서 자동으로 보인다.

엔드포인트:

* ``GET /``                  → FE HTML (정적 자산)
* ``GET /dashboard.html``    → ``/`` 의 alias
* ``GET /api/snapshot``      → ``snapshot_to_payload`` 의 JSON
* ``GET /api/health``        → 작은 헬스체크 JSON (DB 존재 여부 포함)

설계 원칙:

* **외부 의존성 0** — 표준 라이브러리(``http.server``, ``socketserver``,
  ``threading``) 만 사용한다.
* **기본 localhost 바인딩** — DB 경로/프로젝트 경로 같은 환경 정보를
  노출하므로 `0.0.0.0` 으로의 바인딩은 명시적이어야 한다.
* **읽기 전용** — 어떤 엔드포인트도 DB 를 변경하지 않는다.
* **CORS 단순화** — 같은 오리진에서 서빙되므로 CORS 헤더를 붙이지 않는다.
* **DB 경로 override** — ``DashboardServer(db_path_override=...)`` 로 임의의
  ``worker-control.sqlite3`` 파일을 가리킬 수 있다. 내부적으로
  ``WORKER_CONTROL_DB`` 환경변수를 세팅해서 모든 ``db_path()`` 사용처에
  반영된다.
"""
from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from worker_control import __version__
from worker_control.dashboard import (
    DEFAULT_OUTPUT_FILENAME,
    collect_snapshot,
    snapshot_to_payload,
    static_dashboard_html,
)
from worker_control.paths import db_path, runtime_root


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# ---------------------------------------------------------------------------

def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    """worker-control 대시보드 BFF 핸들러."""

    server_version = f"worker-control/{__version__}"

    # native 디스커버리 상한은 서버 인스턴스에서 가져온다.
    @property
    def native_limit(self) -> int | None:
        return getattr(self.server, "native_limit", 500)

    # 표준 로그를 stderr 그대로 흘리지 않고 짧게 정돈한다.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
        sink = getattr(self.server, "log_sink", None)
        msg = "%s - - [%s] %s\n" % (
            self.address_string(),
            self.log_date_time_string(),
            fmt % args,
        )
        if sink is None:
            return
        try:
            sink.write(msg)
            sink.flush()
        except Exception:
            pass

    # --- 공통 응답 헬퍼 ---------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send_html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    # --- 라우팅 -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: D401
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/" + DEFAULT_OUTPUT_FILENAME):
            self._handle_dashboard()
        elif path == "/api/snapshot":
            self._handle_snapshot()
        elif path == "/api/health":
            self._handle_health()
        else:
            self._send_text(HTTPStatus.NOT_FOUND, f"not found: {path}\n")

    # HEAD 는 GET 과 같은 라우팅을 거치되 본문은 비운다 (_send 가 처리).
    do_HEAD = do_GET

    # --- 핸들러 -----------------------------------------------------------
    def _handle_dashboard(self) -> None:
        """FE 자산을 그대로 돌려준다.

        과거에는 매 요청마다 스냅샷을 모아 HTML 에 박아 보냈지만, 이제는
        FE 가 ``/api/snapshot`` 을 직접 호출해서 데이터를 가져온다 —
        ``GET /`` 는 정적 FE asset 한 장만 보낸다.
        """
        try:
            html = static_dashboard_html()
        except Exception as exc:
            self._send_text(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"dashboard asset load failed: {exc}\n",
            )
            return
        self._send_html(HTTPStatus.OK, html)

    def _handle_snapshot(self) -> None:
        try:
            snap = collect_snapshot(native_limit=self.native_limit)
            payload = snapshot_to_payload(snap)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "snapshot_failed", "detail": str(exc)},
            )
            return
        self._send_json(HTTPStatus.OK, payload)

    def _handle_health(self) -> None:
        # 매 요청마다 현재 환경에서 다시 계산한다 — db_path() 가 env override
        # 로 바뀌어도 그대로 반영된다.
        db = db_path()
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "service": "worker-control",
            "version": __version__,
            "db_path": str(db),
            "db_exists": db.exists(),
            "runtime_root": str(runtime_root()),
        })


# ---------------------------------------------------------------------------

class DashboardServer(ThreadingHTTPServer):
    """``DashboardHandler`` 를 동작시키는 ThreadingHTTPServer 래퍼.

    인스턴스 속성으로 ``native_limit`` 과 ``log_sink`` 를 들고 다닌다.

    ``db_path_override`` / ``runtime_root_override`` 가 주어지면 인스턴스
    초기화 시점에 ``WORKER_CONTROL_DB`` / ``WORKER_CONTROL_HOME`` 환경변수를
    덮어쓴다. 같은 프로세스 안의 모든 ``paths.db_path()`` /
    ``paths.runtime_root()`` 호출이 이 값을 본다.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        native_limit: int | None = 500,
        log_sink=None,
        db_path_override: str | os.PathLike[str] | None = None,
        runtime_root_override: str | os.PathLike[str] | None = None,
    ) -> None:
        if db_path_override is not None:
            os.environ["WORKER_CONTROL_DB"] = str(db_path_override)
        if runtime_root_override is not None:
            os.environ["WORKER_CONTROL_HOME"] = str(runtime_root_override)
        super().__init__((host, port), DashboardHandler)
        self.native_limit = native_limit
        self.log_sink = log_sink
        self.db_path: Path = db_path()
        self.runtime_root: Path = runtime_root()

    @property
    def host(self) -> str:
        return self.server_address[0]

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::"):  # 사용자에게 보여주는 URL 은 loopback 으로
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/"


# ---------------------------------------------------------------------------

def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    native_limit: int | None = 500,
    log_sink=None,
    db_path_override: str | os.PathLike[str] | None = None,
    runtime_root_override: str | os.PathLike[str] | None = None,
) -> DashboardServer:
    """``DashboardServer`` 인스턴스를 만든다 (서빙 시작은 호출자 책임)."""
    return DashboardServer(
        host=host, port=port,
        native_limit=native_limit, log_sink=log_sink,
        db_path_override=db_path_override,
        runtime_root_override=runtime_root_override,
    )


def serve_forever(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    native_limit: int | None = 500,
    on_start: Callable[[DashboardServer], None] | None = None,
    log_sink=None,
    db_path_override: str | os.PathLike[str] | None = None,
    runtime_root_override: str | os.PathLike[str] | None = None,
) -> None:
    """블로킹 호출. ``Ctrl-C`` 로만 빠져나온다."""
    server = make_server(
        host=host, port=port,
        native_limit=native_limit, log_sink=log_sink,
        db_path_override=db_path_override,
        runtime_root_override=runtime_root_override,
    )
    if on_start is not None:
        on_start(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def serve_in_thread(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    native_limit: int | None = 500,
    log_sink=None,
    db_path_override: str | os.PathLike[str] | None = None,
    runtime_root_override: str | os.PathLike[str] | None = None,
) -> tuple[DashboardServer, threading.Thread]:
    """테스트/임베드용. 데몬 스레드에서 서버를 실행하고 핸들을 돌려준다.

    호출자는 ``server.shutdown()`` 으로 정지시키고 ``thread.join()`` 으로
    회수한다.
    """
    server = make_server(
        host=host, port=port,
        native_limit=native_limit, log_sink=log_sink,
        db_path_override=db_path_override,
        runtime_root_override=runtime_root_override,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="worker-control-dashboard",
        daemon=True,
    )
    thread.start()
    return server, thread
