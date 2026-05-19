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


def test_session_view_to_dict_fills_parity_keys_when_agent_row_missing():
    """No agent_session row → every parity key still appears on the FE dict.

    The dashboard FE assumes a stable row shape (every session row carries
    the legacy-parity keys), so `_session_view_to_dict` MUST emit them all
    even for a SessionView whose agent-side parity columns are NULL.

    PR #5: the explicit ``_merge_parity_extras`` helper is gone — the
    SessionView dataclass owns the defaults (msg_* = 0, is_spawned_agent =
    False, child arrays = []), and ``_session_view_to_dict`` re-keys to
    the FE's expected names (``size_bytes``, ``recap_native``).
    """
    from worker_control.session_view import SessionView
    v = SessionView(
        id=1, uuid="no-such-uuid", name="X", status="active",
        origin="native", model=None, permission_mode=None, brief=None,
        notes=None, claude_name=None, claude_status=None,
        claude_status_at=None, created_at="2026-05-18T00:00:00Z",
        last_used_at="2026-05-18T00:00:00Z", ended_at=None,
        classification="native", spawn_reason=None, dispatch_mode="—",
        run_count=0, print_run_count=0, last_run_index=None,
        last_run_name=None, last_run_mode=None, last_run_status=None,
        last_run_started_at=None, last_run_ended_at=None,
        project_id=None, project_name=None, project_path=None,
        project_role=None,
    )
    row = dashboard._session_view_to_dict(v)
    for key in _PARITY_KEYS:
        assert key in row, f"missing default for {key!r}"
    assert row["pr_links"] == []
    assert row["files_touched"] == []
    assert row["tools_recent"] == []
    assert row["recap_native"] == []
    assert row["pending_queue"] == []
    assert row["msg_user"] == 0
    assert row["is_spawned"] is False


def test_session_view_to_dict_emits_agent_payload_when_present():
    """Populated SessionView → parity scalars + child arrays land on the dict."""
    from worker_control.session_view import SessionView
    v = SessionView(
        id=2, uuid="ABC-UUID", name="Y", status="active", origin="spawned",
        model=None, permission_mode=None, brief=None, notes=None,
        claude_name=None, claude_status=None, claude_status_at=None,
        created_at="2026-05-18T00:00:00Z",
        last_used_at="2026-05-18T00:00:00Z", ended_at=None,
        classification="a_ok_spawned", spawn_reason="prefix:a-ok:",
        dispatch_mode="print", run_count=1, print_run_count=1,
        last_run_index=1, last_run_name="a-ok:hello", last_run_mode="print",
        last_run_status="done", last_run_started_at=None,
        last_run_ended_at=None,
        project_id=None, project_name=None, project_path=None,
        project_role=None,
        agent_kind="claude", git_branch="main", claude_version="2.1.141",
        msg_user=21, msg_assistant=35, msg_tool=4, ai_title="rich title",
        pr_links=[{"url": "https://x/p/1", "num": 1,
                   "repo": "x/p", "kind": "github"}],
        files_touched=["a.py", "b.py"],
        tools_recent=[{"name": "Bash", "snippet": "ls", "ts": None}],
        pending_queue=[{"text": "do it", "queued_at": None}],
        recaps=[{"content": "first line\nbody", "ts": None}],
    )
    row = dashboard._session_view_to_dict(v)
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


def test_hermes_session_panel_excludes_native_claude_rows(
    populated_db, tmp_path: Path,
):
    """Post-split, ``_collect_hermes_session_panel`` reads only Hermes-profile
    sessions (``hermes_agent_sessions`` table). Native claude rows live in
    ``claude_session_parity`` and must not appear in the panel.
    """
    import sqlite3

    from worker_control_hermes.legacy_parity_schema import (
        apply_legacy_parity_schema, upsert_claude_parity_row,
        upsert_session_row,
    )

    db_path = db.db_path()
    conn = sqlite3.connect(str(db_path))
    apply_legacy_parity_schema(conn)
    # A native claude row — must NOT appear in the panel.
    upsert_claude_parity_row(conn, {
        "session_uuid": "abcdef12-3456-7890-abcd-ef0123456789",
        "kind": "claude",
        "transcript_path": str(Path.home() / ".claude" / "projects"
                               / "fake" / "abcdef12-3456-7890-abcd-ef0123456789.jsonl"),
        "synced_at": "2026-05-18T00:00:00Z",
    })
    # A real hermes-profile row — must appear with profile_name set.
    upsert_session_row(conn, {
        "hermes_session_id": "session_host_111_aaa",
        "kind": "hermes",
        "profile_name": "worker",
        "profile_path": "/fake/profile",
        "transcript_path": "/fake/profile/sessions/session_host_111_aaa.json",
        "synced_at": "2026-05-18T00:00:00Z",
    })
    conn.commit()
    conn.close()

    rows, _ = dashboard._collect_hermes_session_panel([])
    sids = {r["hermes_session_id"] for r in rows}
    assert "abcdef12-3456-7890-abcd-ef0123456789" not in sids
    assert "session_host_111_aaa" in sids
    panel_row = next(
        r for r in rows
        if r["hermes_session_id"] == "session_host_111_aaa"
    )
    assert panel_row["profile_name"] == "worker"
    # Every panel row must have a non-empty profile_name (Hermes-only invariant).
    assert all(r["profile_name"] for r in rows)


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


# --- spawn-claude chip status (claude_status_compact) ------------------------

def test_compute_chip_status_ended_at_forces_done():
    assert dashboard._compute_chip_status(
        status="active", ended_at="2026-05-18T11:30:00+00:00",
        last_used_at="2026-05-18T11:59:00+00:00", claude_status=None,
    ) == "done"


def test_compute_chip_status_terminal_status_forces_done():
    for st in ("done", "abandoned"):
        assert dashboard._compute_chip_status(
            status=st, ended_at=None,
            last_used_at="2026-05-18T11:59:00+00:00", claude_status=None,
        ) == "done", f"status={st} should force done"


def test_compute_chip_status_failed_or_claude_error_is_error():
    # ledger status == "failed" → error
    assert dashboard._compute_chip_status(
        status="failed", ended_at=None,
        last_used_at=None, claude_status=None,
    ) == "error"
    # claude_status == "error" → error (even with status="active")
    assert dashboard._compute_chip_status(
        status="active", ended_at=None,
        last_used_at=None, claude_status="error",
    ) == "error"


def test_compute_chip_status_recency_buckets():
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def _iso(minutes_ago: int) -> str:
        return (now - _dt.timedelta(minutes=minutes_ago)).isoformat()

    # ≤ 2h → active
    assert dashboard._compute_chip_status(
        status="active", ended_at=None, last_used_at=_iso(30),
        claude_status=None,
    ) == "active"
    # 2h–24h → inactive
    assert dashboard._compute_chip_status(
        status="active", ended_at=None, last_used_at=_iso(60 * 5),
        claude_status=None,
    ) == "inactive"
    # > 24h → done
    assert dashboard._compute_chip_status(
        status="active", ended_at=None, last_used_at=_iso(60 * 30),
        claude_status=None,
    ) == "done"


def test_compute_chip_status_missing_last_used_falls_back_inactive():
    assert dashboard._compute_chip_status(
        status="active", ended_at=None, last_used_at=None, claude_status=None,
    ) == "inactive"


def test_hermes_session_panel_emits_chip_status_for_each_spawn(populated_db):
    """Every ``spawned_claudes[*]`` row carries a valid ``claude_status_compact``.

    Inserts four hermes_sessions rows linked to one hermes_agent_session via
    hermes_runs, each in a distinct state (active/inactive/done/error). The
    BFF panel must expose the bucket value on every chip — no key missing,
    no value outside the four allowed buckets.
    """
    import datetime as _dt
    import sqlite3

    from worker_control import hermes_install
    from worker_control_hermes.legacy_parity_schema import (
        apply_legacy_parity_schema, upsert_session_row,
    )

    db_path = db.db_path()
    hermes_install._apply_extra_schema(db_path)

    now = _dt.datetime.now(_dt.timezone.utc)
    fresh   = (now - _dt.timedelta(minutes=10)).isoformat()
    stale   = (now - _dt.timedelta(hours=5)).isoformat()
    ancient = (now - _dt.timedelta(hours=30)).isoformat()
    hsid = "session_host_chip_status"

    conn = sqlite3.connect(str(db_path))
    apply_legacy_parity_schema(conn)
    # An agent_sessions row so the panel surfaces this hsid at all.
    upsert_session_row(conn, {
        "hermes_session_id": hsid,
        "kind": "hermes",
        "profile_name": "worker",
        "profile_path": "/fake/profile",
        "transcript_path": f"/fake/profile/sessions/{hsid}.json",
        "synced_at": now.isoformat(),
    })
    # Project for the FK constraint on hermes_sessions.
    conn.execute(
        "INSERT INTO projects(name, path, root_role, created_at, updated_at) "
        "VALUES ('p', '/tmp/p', 'owned_work', ?, ?)", (fresh, fresh),
    )
    proj_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    # Four claude sessions in distinct states.
    spec = [
        # (uuid, status, ended_at, last_used_at, claude_status, expected)
        ("11111111-aaaa-aaaa-aaaa-111111111111",
         "active", None, fresh,   None,    "active"),
        ("22222222-aaaa-aaaa-aaaa-222222222222",
         "active", None, stale,   None,    "inactive"),
        ("33333333-aaaa-aaaa-aaaa-333333333333",
         "done",   ancient, ancient, None, "done"),
        ("44444444-aaaa-aaaa-aaaa-444444444444",
         "failed", None, fresh,   None,    "error"),
    ]
    for i, (uuid, st, ended, last_used, cstat, _expected) in enumerate(spec):
        conn.execute(
            "INSERT INTO hermes_sessions(uuid, project_id, name, status, origin, "
            " created_at, last_used_at, ended_at, claude_status) "
            "VALUES (?, ?, ?, ?, 'spawned', ?, ?, ?, ?)",
            (uuid, proj_id, f"a-ok:chip-{i}", st, last_used, last_used, ended, cstat),
        )
        sid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO hermes_runs(session_id, run_index, name, mode, "
            "command, started_at, hermes_session_id) "
            "VALUES (?, 1, ?, 'print', '', ?, ?)",
            (sid, f"a-ok:chip-{i}-r", last_used, hsid),
        )
    conn.commit()
    conn.close()

    rows, _ = dashboard._collect_hermes_session_panel([])
    panel_row = next(r for r in rows if r["hermes_session_id"] == hsid)
    by_uuid = {c["claude_uuid"]: c for c in panel_row["spawned_claudes"]}
    valid = {"active", "inactive", "done", "error"}
    for uuid, _st, _ended, _last, _cstat, expected in spec:
        chip = by_uuid[uuid]
        assert "claude_status_compact" in chip, f"missing on {uuid}"
        assert chip["claude_status_compact"] in valid
        assert chip["claude_status_compact"] == expected, (
            f"uuid={uuid} expected={expected} "
            f"got={chip['claude_status_compact']}"
        )
