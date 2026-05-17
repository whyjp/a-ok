"""Read-side helpers for projects (writes happen in scanner.py)."""
from __future__ import annotations

from dataclasses import dataclass

from worker_control.db import load_json, session_scope
from worker_control.paths import ROLE_OTHER


@dataclass(slots=True)
class Project:
    id: int
    name: str
    path: str
    is_git: bool
    branch: str | None
    remote_url: str | None
    is_dirty: bool
    root_role: str
    last_scan_at: str | None
    metadata: dict
    created_at: str
    updated_at: str


def _row_to_project(row) -> Project:
    try:
        role = row["root_role"] or ROLE_OTHER
    except (IndexError, KeyError):
        role = ROLE_OTHER
    return Project(
        id=row["id"],
        name=row["name"],
        path=row["path"],
        is_git=bool(row["is_git"]),
        branch=row["branch"],
        remote_url=row["remote_url"],
        is_dirty=bool(row["is_dirty"]),
        root_role=role,
        last_scan_at=row["last_scan_at"],
        metadata=load_json(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_projects(git_only: bool = False) -> list[Project]:
    sql = "SELECT * FROM projects"
    if git_only:
        sql += " WHERE is_git = 1"
    sql += " ORDER BY root_role, name"
    with session_scope() as conn:
        rows = conn.execute(sql).fetchall()
    return [_row_to_project(r) for r in rows]


def get_project(name: str) -> Project | None:
    with session_scope() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_project(row) if row else None


def require_project(name: str) -> Project:
    p = get_project(name)
    if p is None:
        raise LookupError(f"no such project (scan first?): {name}")
    return p
