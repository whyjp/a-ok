"""Tests for native Claude session discovery (read-only)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from worker_control import native_sessions


def test_decode_project_dirname_drive_prefix() -> None:
    got = native_sessions.decode_project_dirname("D--work-github-worker-control")
    assert got == "D:/work-github/worker-control"


def test_decode_project_dirname_lowercase_drive() -> None:
    got = native_sessions.decode_project_dirname("c--users-cxx")
    assert got == "C:/users/cxx"


def test_decode_project_dirname_no_drive_prefix() -> None:
    got = native_sessions.decode_project_dirname("some-arbitrary-name")
    assert got == "some/arbitrary/name"


def test_decode_project_dirname_empty() -> None:
    assert native_sessions.decode_project_dirname("") == ""


def test_discover_missing_root_returns_note(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR",
        str(tmp_path / "does-not-exist"),
    )
    snap = native_sessions.discover_native_sessions()
    assert snap.root_exists is False
    assert snap.sessions == []
    assert snap.note and "발견되지 않았습니다" in snap.note


def test_discover_picks_up_jsonl_with_metadata(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "projects"
    proj = root / "D--work-github-worker-control"
    proj.mkdir(parents=True)
    sid = "02cd57cf-4377-455e-84c1-1a067fc952e4"
    path = proj / f"{sid}.jsonl"
    lines = [
        json.dumps({
            "type": "last-prompt", "leafUuid": "abc-leaf",
            "sessionId": sid,
        }),
        json.dumps({
            "type": "permission-mode", "permissionMode": "auto",
            "sessionId": sid,
        }),
        json.dumps({"type": "user", "text": "hi"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("WORKER_CONTROL_CLAUDE_PROJECTS_DIR", str(root))
    snap = native_sessions.discover_native_sessions()

    assert snap.root_exists is True
    assert len(snap.sessions) == 1
    sess = snap.sessions[0]
    assert sess.session_id == sid
    assert sess.permission_mode == "auto"
    assert sess.leaf_uuid == "abc-leaf"
    assert sess.line_count == 3
    assert sess.project_dir_name == "D--work-github-worker-control"
    assert sess.project_path_guess == "D:/work-github/worker-control"
    assert sess.size_bytes > 0


def test_discover_skips_non_jsonl(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "projects"
    proj = root / "fake"
    proj.mkdir(parents=True)
    (proj / "README.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setenv("WORKER_CONTROL_CLAUDE_PROJECTS_DIR", str(root))
    snap = native_sessions.discover_native_sessions()
    assert snap.sessions == []


def test_discover_limit_zero_skips_discovery(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "projects"
    proj = root / "D--foo"
    proj.mkdir(parents=True)
    (proj / "x.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("WORKER_CONTROL_CLAUDE_PROJECTS_DIR", str(root))
    snap = native_sessions.discover_native_sessions(limit=0)
    assert snap.sessions == []
    assert snap.root_exists is True
    assert snap.note and "비활성화" in snap.note


def test_discover_limit_truncates(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "projects"
    proj = root / "D--bar"
    proj.mkdir(parents=True)
    for i in range(5):
        (proj / f"sess-{i}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("WORKER_CONTROL_CLAUDE_PROJECTS_DIR", str(root))
    snap = native_sessions.discover_native_sessions(limit=2)
    assert len(snap.sessions) == 2
    assert snap.note and "초과" in snap.note
