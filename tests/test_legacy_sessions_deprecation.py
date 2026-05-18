"""`workerctl sessions list` 가 Phase 2 deprecation 경고를 stderr 로 찍는지."""
from __future__ import annotations

import pytest

from worker_control import cli, db


def test_sessions_list_emits_deprecation_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    db.init_db()

    rc = cli.main(["sessions", "list"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "deprecated" in captured.err.lower(), (
        f"expected 'deprecated' in stderr, got: {captured.err!r}"
    )
    assert "workerctl session" in captured.err, (
        f"expected pointer to `workerctl session ...`, got: {captured.err!r}"
    )


def test_sessions_list_returns_zero_on_empty_db(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deprecation warning must not change the exit code."""
    db.init_db()

    rc = cli.main(["sessions", "list"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "(no sessions)" in captured.out
