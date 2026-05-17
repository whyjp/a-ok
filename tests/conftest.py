"""Test setup: route the runtime root into a tmp directory per test session."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force WORKER_CONTROL_HOME into a temp directory for every test."""
    monkeypatch.setenv("WORKER_CONTROL_HOME", str(tmp_path / ".worker-control"))
    monkeypatch.delenv("WORKER_CONTROL_DB", raising=False)
    monkeypatch.delenv("WORKER_CONTROL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", raising=False)
    return tmp_path
