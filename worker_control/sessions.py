"""Session lifecycle: start, capture, prompt, stop."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from worker_control.db import dump_json, load_json, session_scope, utcnow_iso
from worker_control.paths import (
    ROLE_OWNED_WORK,
    ROLE_PUBLIC_REFERENCE,
    classify_path,
)
from worker_control.profiles import require_profile
from worker_control.projects import require_project
from worker_control import runtime


class WriteProtectedRootError(RuntimeError):
    """워크스페이스 정책상 쓰기/세션 시작이 금지된 경로에서 기동 시도됨."""

# Coarse state vocabulary (see docs/operations.md)
VALID_STATES = {
    "starting", "running", "waiting_input", "working",
    "blocked", "completed", "failed", "killed",
}


@dataclass(slots=True)
class Session:
    id: int
    name: str
    profile_id: int
    project_id: int
    state: str
    runtime: str
    tmux_session: str | None
    pid: int | None
    started_at: str | None
    ended_at: str | None
    metadata: dict
    created_at: str
    updated_at: str


def _row_to_session(row) -> Session:
    return Session(
        id=row["id"],
        name=row["name"],
        profile_id=row["profile_id"],
        project_id=row["project_id"],
        state=row["state"],
        runtime=row["runtime"],
        tmux_session=row["tmux_session"],
        pid=row["pid"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        metadata=load_json(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _resolve_session(ident: str) -> Session:
    """Look up a session by numeric id or by name."""
    with session_scope() as conn:
        if ident.isdigit():
            row = conn.execute(
                "SELECT * FROM worker_sessions WHERE id = ?", (int(ident),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM worker_sessions WHERE name = ?", (ident,),
            ).fetchone()
    if row is None:
        raise LookupError(f"no such session: {ident}")
    return _row_to_session(row)


def _next_session_name(profile_name: str, project_name: str) -> str:
    """Generate a unique session name like 'default__worker-control__001'."""
    base = f"{profile_name}__{project_name}"
    with session_scope() as conn:
        rows = conn.execute(
            "SELECT name FROM worker_sessions WHERE name LIKE ?",
            (f"{base}__%",),
        ).fetchall()
    existing = {r["name"] for r in rows}
    for i in range(1, 1000):
        candidate = f"{base}__{i:03d}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not allocate session name (limit reached)")


def _record_event(conn, session_id: int, kind: str, payload: dict) -> None:
    conn.execute(
        """
        INSERT INTO session_events (session_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, kind, dump_json(payload), utcnow_iso()),
    )


def list_sessions() -> list[Session]:
    with session_scope() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_sessions ORDER BY id DESC"
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def start_session(profile_name: str, project_name: str,
                  prefer_tmux: bool = True) -> Session:
    """Insert a row in starting state, launch worker, transition to running.

    워크스페이스 정책: ``public_reference`` 루트(예: ``D:/github``) 의 프로젝트는
    워커 기동을 거부한다. 사용자는 작업할 프로젝트를 ``D:/work-github`` 아래로
    옮긴 후 다시 ``workerctl projects scan`` 을 돌려야 한다.
    """
    profile = require_profile(profile_name)
    project = require_project(project_name)
    cwd = Path(project.path)
    if not cwd.exists():
        raise FileNotFoundError(f"project path missing: {cwd}")

    # 정책 가드: project.root_role 우선, 누락 시 경로로 재분류.
    role = project.root_role or classify_path(project.path)
    if role == ROLE_PUBLIC_REFERENCE:
        raise WriteProtectedRootError(
            "이 프로젝트는 public_reference 워크스페이스(D:/github)에 있어 "
            "워커 기동이 금지되었습니다. 편집/PR 대상이라면 D:/work-github 으로 "
            "이동시킨 뒤 다시 `workerctl projects scan` 을 실행하세요. "
            f"(project={project.name}, path={project.path})"
        )
    if role != ROLE_OWNED_WORK:
        # 알려진 두 루트 어디에도 속하지 않는 경로(other) — 안전을 위해 거부.
        raise WriteProtectedRootError(
            "이 프로젝트는 알려진 owned_work 루트(D:/work-github) 외부에 있어 "
            "워커 기동이 금지되었습니다. WORKER_CONTROL_PROJECT_ROOT 로 루트를 "
            "명시하거나 프로젝트를 owned_work 루트 아래로 옮기세요. "
            f"(project={project.name}, path={project.path}, role={role})"
        )

    name = _next_session_name(profile.name, project.name)
    now = utcnow_iso()

    with session_scope() as conn:
        cur = conn.execute(
            """
            INSERT INTO worker_sessions
                (name, profile_id, project_id, state, runtime,
                 tmux_session, pid, started_at, ended_at,
                 metadata, created_at, updated_at)
            VALUES (?, ?, ?, 'starting', 'pending', NULL, NULL, ?, NULL, ?, ?, ?)
            """,
            (name, profile.id, project.id, now, dump_json({}), now, now),
        )
        new_id = cur.lastrowid
        _record_event(conn, new_id, "state", {"to": "starting"})

    # Launch outside the DB transaction (subprocess can be slow).
    try:
        result = runtime.launch(name, cwd, prefer_tmux=prefer_tmux)
        end_state = "running"
        note = result.note
    except Exception as exc:  # surface launch failure but still record it
        with session_scope() as conn:
            now2 = utcnow_iso()
            conn.execute(
                """
                UPDATE worker_sessions
                   SET state = 'failed', updated_at = ?, ended_at = ?
                 WHERE id = ?
                """,
                (now2, now2, new_id),
            )
            _record_event(conn, new_id, "error", {"message": str(exc)})
        raise

    with session_scope() as conn:
        now2 = utcnow_iso()
        conn.execute(
            """
            UPDATE worker_sessions
               SET state = ?, runtime = ?, tmux_session = ?, pid = ?, updated_at = ?
             WHERE id = ?
            """,
            (end_state, result.runtime, result.tmux_session, result.pid, now2, new_id),
        )
        _record_event(conn, new_id, "state", {"to": end_state, "note": note})

    return _resolve_session(str(new_id))


def transition(session_ident: str, new_state: str) -> Session:
    """Explicitly move a session to a new coarse state."""
    if new_state not in VALID_STATES:
        raise ValueError(f"invalid state: {new_state}")
    sess = _resolve_session(session_ident)
    with session_scope() as conn:
        now = utcnow_iso()
        conn.execute(
            "UPDATE worker_sessions SET state = ?, updated_at = ? WHERE id = ?",
            (new_state, now, sess.id),
        )
        _record_event(conn, sess.id, "state", {"from": sess.state, "to": new_state})
    return _resolve_session(str(sess.id))


def capture(session_ident: str) -> tuple[Session, str]:
    """Capture current screen (tmux) or recent events (console fallback)."""
    sess = _resolve_session(session_ident)
    if sess.runtime == "tmux" and sess.tmux_session:
        body = runtime.tmux_capture(sess.tmux_session)
        with session_scope() as conn:
            _record_event(conn, sess.id, "capture",
                          {"bytes": len(body), "via": "tmux"})
        return sess, body
    # Console fallback: dump last N events.
    with session_scope() as conn:
        rows = conn.execute(
            """
            SELECT created_at, kind, payload
              FROM session_events
             WHERE session_id = ?
             ORDER BY id DESC LIMIT 20
            """,
            (sess.id,),
        ).fetchall()
        _record_event(conn, sess.id, "capture",
                      {"bytes": 0, "via": "console-stub"})
    lines = [
        "[no tmux — screen capture unavailable; last events follow]",
        *[
            f"{r['created_at']}  {r['kind']:8s}  {r['payload']}"
            for r in reversed(rows)
        ],
    ]
    return sess, "\n".join(lines)


def prompt(session_ident: str, text: str) -> tuple[Session, str]:
    """Send a prompt to the worker. Only tmux runtime can deliver it."""
    sess = _resolve_session(session_ident)
    delivery: str
    result_note: str
    if sess.runtime == "tmux" and sess.tmux_session:
        runtime.tmux_send(sess.tmux_session, text)
        delivery = "tmux"
        result_note = "ok"
    else:
        delivery = "rejected_no_tmux"
        result_note = "no tmux runtime; manual input required in the spawned console"

    with session_scope() as conn:
        conn.execute(
            """
            INSERT INTO worker_commands
                (session_id, text, delivery, result, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sess.id, text, delivery, result_note, utcnow_iso()),
        )
    return sess, result_note


def stop_session(session_ident: str) -> Session:
    """Stop a session. tmux: kill-session. console: taskkill the spawner pid."""
    sess = _resolve_session(session_ident)
    if sess.runtime == "tmux" and sess.tmux_session:
        runtime.tmux_kill(sess.tmux_session)
    elif sess.pid:
        runtime.kill_pid(sess.pid)
    with session_scope() as conn:
        now = utcnow_iso()
        conn.execute(
            """
            UPDATE worker_sessions
               SET state = 'killed', ended_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (now, now, sess.id),
        )
        _record_event(conn, sess.id, "state",
                      {"from": sess.state, "to": "killed"})
    return _resolve_session(str(sess.id))
