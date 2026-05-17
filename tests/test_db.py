from worker_control import db, profiles


def test_init_creates_all_tables():
    target = db.init_db()
    assert target.exists()
    with db.session_scope() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert {
        "worker_profiles", "projects", "worker_sessions",
        "session_events", "worker_commands", "project_scans",
    }.issubset(names)


def test_init_is_idempotent():
    db.init_db()
    db.init_db()  # should not raise


def test_profile_create_and_list():
    db.init_db()
    p = profiles.create_profile("default", root="D:/github")
    assert p.id > 0
    listed = profiles.list_profiles()
    assert any(x.name == "default" for x in listed)


def test_profile_create_duplicate_raises():
    db.init_db()
    profiles.create_profile("dup")
    try:
        profiles.create_profile("dup")
    except ValueError:
        return
    raise AssertionError("expected ValueError on duplicate profile")
