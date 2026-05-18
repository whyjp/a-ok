"""Tests for the HTML dashboard view layer."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from worker_control import cli, dashboard, db, profiles, projects, scanner
from worker_control.paths import ROLE_OWNED_WORK, ROLE_PUBLIC_REFERENCE


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "main"], cwd=str(path), check=False,
    )


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch):
    """owned_work 와 public_reference 양쪽에 프로젝트를 만들어두고 스캔."""
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
    # native 디스커버리는 격리된 빈 디렉토리로
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR",
        str(tmp_path / ".claude-projects"),
    )

    db.init_db()
    profiles.create_profile("default", root=str(work))
    scanner.scan_root(work)
    scanner.scan_root(pub)
    return tmp_path


def test_snapshot_includes_all_layers(populated_db):
    snap = dashboard.collect_snapshot()
    assert snap.version
    assert snap.db_path
    assert snap.counters["profiles"] == 1
    assert snap.counters["projects"] >= 2
    assert snap.counters["projects_owned"] >= 1
    assert snap.counters["projects_public"] >= 1
    assert snap.counters["hermes_sessions"] == 0
    # workspace roots include both owned + public
    roles = {r.role for r in snap.workspace_roots}
    assert ROLE_OWNED_WORK in roles
    assert ROLE_PUBLIC_REFERENCE in roles


def test_snapshot_marks_policy_per_project(populated_db):
    snap = dashboard.collect_snapshot()
    by_role: dict[str, list[dict]] = {}
    for p in snap.projects:
        by_role.setdefault(p["root_role"], []).append(p)
    assert any(p["policy"] == "work_capable"
               for p in by_role[ROLE_OWNED_WORK])
    assert any(p["policy"] == "read_only"
               for p in by_role[ROLE_PUBLIC_REFERENCE])


def test_render_html_contains_korean_labels_and_data(populated_db):
    snap = dashboard.collect_snapshot()
    html_text = dashboard.render_html(snap)
    assert html_text.startswith("<!DOCTYPE html>")
    assert "worker-control" in html_text
    assert "워커 프로파일" in html_text
    assert "hermes 스폰 claude 세션" in html_text
    assert "Native Claude 세션" in html_text
    assert "관리대상 프로젝트" in html_text
    # the JSON payload is embedded and parseable
    marker = '<script id="dashboard-data" type="application/json">'
    start = html_text.index(marker) + len(marker)
    end = html_text.index("</script>", start)
    payload = html_text[start:end]
    parsed = json.loads(payload)
    assert parsed["counters"]["projects"] == snap.counters["projects"]
    assert any(p["root_role"] == ROLE_OWNED_WORK for p in parsed["projects"])


def test_write_dashboard_default_path(populated_db):
    out = dashboard.write_dashboard()
    assert out.exists()
    assert out.name == "dashboard.html"
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


# --- legacy-parity payload shape ---------------------------------------------

_PARITY_KEYS = (
    "kind", "git_branch", "claude_version",
    "msg_user", "msg_assistant", "msg_tool",
    "ai_title", "summary",
    "first_user_text", "last_user_text", "last_assistant_text",
    "size_bytes", "spawn_slug", "is_spawned", "effective_status",
    "pr_links", "files_touched", "tools_recent",
    "recap_native", "pending_queue",
)


def test_merge_parity_extras_fills_defaults_when_uuid_unknown():
    """No matching extras row → every parity key gets a safe default.

    The dashboard FE assumes a stable row shape (every session row carries
    the legacy-parity keys), so the merge helper MUST fill defaults even
    when the parity ingest hasn't produced a row for this session.
    """
    row = {"uuid": "no-such-uuid", "name": "X"}
    dashboard._merge_parity_extras(row, {}, "no-such-uuid")
    for key in _PARITY_KEYS:
        assert key in row, f"missing default for {key!r}"
    assert row["pr_links"] == []
    assert row["files_touched"] == []
    assert row["tools_recent"] == []
    assert row["recap_native"] == []
    assert row["pending_queue"] == []
    assert row["msg_user"] == 0
    assert row["is_spawned"] is False


def test_merge_parity_extras_applies_child_arrays_when_present():
    """Matching extras row → its child arrays + scalars land on the payload row."""
    extras_map = {
        "abc-uuid": {
            **dashboard._EMPTY_PARITY_EXTRAS,
            "git_branch": "main",
            "claude_version": "2.1.141",
            "msg_user": 21, "msg_assistant": 35, "msg_tool": 4,
            "ai_title": "rich title",
            "pr_links": [{"url": "https://x/p/1", "num": 1,
                          "repo": "x/p", "kind": "github"}],
            "files_touched": ["a.py", "b.py"],
            "tools_recent": [{"name": "Bash", "snippet": "ls", "ts": None}],
            "pending_queue": [{"text": "do it", "queued_at": None}],
            "recap_native": [{"content": "first line\nbody", "ts": None}],
        },
    }
    row = {"uuid": "ABC-UUID", "name": "Y"}
    dashboard._merge_parity_extras(row, extras_map, row["uuid"])
    assert row["git_branch"] == "main"
    assert row["claude_version"] == "2.1.141"
    assert row["msg_user"] == 21
    assert row["msg_assistant"] == 35
    assert row["msg_tool"] == 4
    assert row["ai_title"] == "rich title"
    assert row["pr_links"][0]["num"] == 1
    assert "a.py" in row["files_touched"]
    assert row["tools_recent"][0]["name"] == "Bash"
    assert row["pending_queue"][0]["text"] == "do it"
    assert row["recap_native"][0]["content"].startswith("first line")


def test_write_dashboard_custom_path(populated_db, tmp_path: Path):
    target = tmp_path / "custom" / "view.html"
    out = dashboard.write_dashboard(output=target)
    assert out == target
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert "worker-control" in body


def test_render_json_payload_escapes_script_tag(tmp_path: Path, monkeypatch):
    """``</script>`` 가 페이로드에 들어가도 인라인 스크립트가 깨지지 않아야."""
    monkeypatch.setenv("WORKER_CONTROL_PROJECT_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv(
        "WORKER_CONTROL_PUBLIC_REFERENCE_ROOT", str(tmp_path / "pub"),
    )
    monkeypatch.setenv(
        "WORKER_CONTROL_CLAUDE_PROJECTS_DIR", str(tmp_path / "claude"),
    )
    (tmp_path / "work").mkdir()
    db.init_db()
    profiles.create_profile("evil</script>", root=str(tmp_path / "work"))
    snap = dashboard.collect_snapshot()
    html_text = dashboard.render_html(snap)
    # 원본 </script> 가 페이로드 내에 그대로 등장하면 안 된다.
    marker = '<script id="dashboard-data" type="application/json">'
    payload_start = html_text.index(marker) + len(marker)
    payload_end = html_text.index("</script>", payload_start)
    payload = html_text[payload_start:payload_end]
    assert "</script>" not in payload
    # 그러나 parser 가 복원했을 때(JSON.parse 는 `<\/` 를 정상 처리) 데이터는 살아 있음.
    parsed = json.loads(payload)
    assert any(p["name"] == "evil</script>" for p in parsed["profiles"])


def test_cli_view_html_creates_file(populated_db, tmp_path: Path):
    target = tmp_path / "out" / "dash.html"
    rc = cli.main(["view", "html", "--legacy", "--output", str(target)])
    assert rc == 0
    assert target.exists()


def test_cli_view_html_requires_legacy_flag(populated_db, tmp_path: Path, capsys):
    target = tmp_path / "out" / "dash.html"
    rc = cli.main(["view", "html", "--output", str(target)])
    assert rc == 2
    assert not target.exists()
    err = capsys.readouterr().err
    assert "--legacy" in err
    assert "view serve" in err


def test_cli_view_html_default_under_runtime_root(populated_db, tmp_path: Path):
    rc = cli.main(["view", "html", "--legacy"])
    assert rc == 0
    # runtime_root 가 conftest 에서 tmp_path/.worker-control 로 잡혀 있음
    expected = tmp_path / ".worker-control" / "dashboard.html"
    assert expected.exists()


def test_cli_view_html_native_limit_zero_disables(
    populated_db, tmp_path: Path,
):
    target = tmp_path / "no-native.html"
    rc = cli.main(["view", "html", "--legacy", "--output", str(target),
                   "--native-limit", "0"])
    assert rc == 0
    body = target.read_text(encoding="utf-8")
    assert "비활성화" in body


# --- FE 자산 / BFF 통합 -------------------------------------------------------

def test_static_dashboard_html_is_packaged_and_parseable():
    """정적 FE 자산이 패키지에 함께 깔리고, placeholder 가 그대로 들어있다."""
    text = dashboard.static_dashboard_html()
    assert text.startswith("<!DOCTYPE html>")
    # placeholder 가 정적 자산에 있어야 한다 (render_html 이 이 자리를 치환)
    assert '"__INLINE_DATA__"' in text
    # FE 가 BFF 와 통신하는 엔드포인트
    assert "api/snapshot" in text
    # 라벨이 살아 있는지
    assert "워커 프로파일" in text
    assert "hermes 스폰 claude 세션" in text


def test_render_html_replaces_inline_placeholder(populated_db):
    """render_html 은 placeholder 위치에 스냅샷 JSON 을 박는다 (레거시 경로)."""
    snap = dashboard.collect_snapshot()
    html_text = dashboard.render_html(snap)
    # placeholder 가 더 이상 보이면 안 됨 (인라인 데이터로 치환됨)
    assert '"__INLINE_DATA__"' not in html_text
    # 같은 자산 기반이므로 동일한 한국어 라벨이 그대로 살아 있어야 한다
    assert "워커 프로파일" in html_text
