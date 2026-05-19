"""Tests for the /api/transcript BFF endpoint and transcript normalisation.

Covers:

* Allow-list guard — paths outside ``WORKER_CONTROL_TRANSCRIPT_EXTRA_ROOTS``
  / ``~/.claude/projects`` / ``~/AppData/Local/hermes/profiles`` are rejected
  with 400 ``path_not_allowed`` so the BFF never serves arbitrary files.
* 404 when the path lives inside an allowed root but doesn't exist.
* 50 MiB cap — anything larger is rejected with 413 ``too_large``.
* hermes-profile session JSON → ``format=='hermes-profile'`` with normalised
  ``tool_calls``.
* native Claude JSONL → ``format=='claude-jsonl'`` with assistant thinking
  extracted into its own field.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from worker_control import db, server
from worker_control.transcript import MAX_BYTES


@contextmanager
def _running_server() -> Iterator[server.DashboardServer]:
    srv, thread = server.serve_in_thread(
        host="127.0.0.1", port=0, native_limit=0, log_sink=None,
    )
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _get(url: str, *, timeout: float = 5.0):
    return urllib.request.urlopen(url, timeout=timeout)


def _read_err(exc: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:
        return {}


@pytest.fixture
def transcript_env(tmp_path: Path, monkeypatch):
    """Common env wiring: isolated DB + transcript allow-root = tmp_path.

    We override ``WORKER_CONTROL_TRANSCRIPT_EXTRA_ROOTS`` so the suite never
    has to touch ``~/.claude`` or hermes profile dirs.
    """
    db_file = tmp_path / "wc.sqlite3"
    monkeypatch.setenv("WORKER_CONTROL_DB", str(db_file))
    monkeypatch.setenv("WORKER_CONTROL_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv(
        "WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "work"),
    )
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "public"),
    )
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR",
        str(tmp_path / ".claude-projects"),
    )
    monkeypatch.setenv(
        "WORKER_CONTROL_TRANSCRIPT_EXTRA_ROOTS", str(tmp_path),
    )
    db.init_db(db_file)
    return tmp_path


def _q(path: Path) -> str:
    return urllib.parse.quote(str(path), safe="")


# urllib.parse is needed but the line above is the only access — import here
# so the test module stays explicit.
import urllib.parse  # noqa: E402


def test_path_outside_allowlist_returns_400(transcript_env, tmp_path, monkeypatch):
    # Strip the EXTRA roots so tmp_path is no longer allow-listed, then point
    # at a file inside tmp_path — it should be rejected.
    monkeypatch.delenv("WORKER_CONTROL_TRANSCRIPT_EXTRA_ROOTS", raising=False)
    evil = tmp_path / "evil.json"
    evil.write_text("{}", encoding="utf-8")
    with _running_server() as srv:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(srv.url + "api/transcript?path=" + _q(evil))
    assert exc.value.code == 400
    body = _read_err(exc.value)
    assert body.get("error") == "path_not_allowed"


def test_missing_file_inside_allowlist_returns_404(transcript_env):
    target = transcript_env / "missing.json"
    with _running_server() as srv:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(srv.url + "api/transcript?path=" + _q(target))
    assert exc.value.code == 404
    assert _read_err(exc.value).get("error") == "not_found"


def test_hermes_profile_format(transcript_env):
    target = transcript_env / "session_abc.json"
    target.write_text(json.dumps({
        "session_id": "abc",
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello",
             "tool_calls": [
                 {"function": {"name": "x", "arguments": "{\"a\":1}"}},
             ],
             "reasoning": "think"},
        ],
    }), encoding="utf-8")
    with _running_server() as srv:
        with _get(srv.url + "api/transcript?path=" + _q(target)) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
    assert data["format"] == "hermes-profile"
    assert len(data["turns"]) == 2
    assert data["turns"][0]["role"] == "user"
    assert data["turns"][0]["text"] == "hi"
    assert data["turns"][1]["role"] == "assistant"
    assert data["turns"][1]["tool_calls"][0]["name"] == "x"
    # JSON-string arguments are pretty-printed.
    assert "\"a\": 1" in data["turns"][1]["tool_calls"][0]["args"]
    assert data["turns"][1]["thinking"] == "think"


def test_hermes_profile_object_arguments(transcript_env):
    """tool_calls.arguments may be a dict (not a JSON string)."""
    target = transcript_env / "session_obj.json"
    target.write_text(json.dumps({
        "messages": [
            {"role": "assistant", "content": None,
             "tool_calls": [
                 {"function": {"name": "search",
                               "arguments": {"q": "hello", "n": 3}}},
             ]},
        ],
    }), encoding="utf-8")
    with _running_server() as srv:
        with _get(srv.url + "api/transcript?path=" + _q(target)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    args = data["turns"][0]["tool_calls"][0]["args"]
    assert "\"q\": \"hello\"" in args
    assert "\"n\": 3" in args


def test_claude_jsonl_format(transcript_env):
    target = transcript_env / "session_xyz.jsonl"
    lines = [
        {"type": "user",
         "timestamp": "2026-05-19T00:00:00Z",
         "message": {"content": "what's up?"}},
        {"type": "assistant",
         "timestamp": "2026-05-19T00:00:01Z",
         "message": {"content": [
             {"type": "thinking", "thinking": "let me consider"},
             {"type": "text", "text": "all good"},
             {"type": "tool_use", "name": "bash",
              "input": {"command": "ls"}},
         ]}},
        {"type": "system",
         "timestamp": "2026-05-19T00:00:02Z",
         "content": "tool result blob"},
    ]
    target.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    with _running_server() as srv:
        with _get(srv.url + "api/transcript?path=" + _q(target)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    assert data["format"] == "claude-jsonl"
    assert len(data["turns"]) == 3
    assert data["turns"][0]["role"] == "user"
    assert data["turns"][0]["text"] == "what's up?"
    assert data["turns"][1]["role"] == "assistant"
    assert data["turns"][1]["text"] == "all good"
    assert data["turns"][1]["thinking"] == "let me consider"
    assert data["turns"][1]["tool_calls"][0]["name"] == "bash"
    assert "\"command\": \"ls\"" in data["turns"][1]["tool_calls"][0]["args"]
    assert data["turns"][2]["role"] == "system"
    assert "[system]" in data["turns"][2]["text"]


def test_oversize_file_returns_413(transcript_env):
    target = transcript_env / "huge.json"
    # Build a file just over the cap without actually filling 50 MiB of JSON.
    # The cap is checked via os.stat before parsing, so writing zero bytes
    # then truncating to MAX_BYTES+1 is enough.
    with target.open("wb") as fh:
        fh.truncate(MAX_BYTES + 1)
    with _running_server() as srv:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(srv.url + "api/transcript?path=" + _q(target))
    assert exc.value.code == 413
    assert _read_err(exc.value).get("error") == "too_large"
