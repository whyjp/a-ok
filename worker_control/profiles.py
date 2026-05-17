"""Worker profile CRUD."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from worker_control.db import dump_json, load_json, session_scope, utcnow_iso
from worker_control.paths import normalize_path, project_root_default


@dataclass(slots=True)
class Profile:
    id: int
    name: str
    root_path: str
    metadata: dict
    created_at: str
    updated_at: str


def _row_to_profile(row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        root_path=row["root_path"],
        metadata=load_json(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_profiles() -> list[Profile]:
    with session_scope() as conn:
        rows = conn.execute(
            "SELECT * FROM worker_profiles ORDER BY name"
        ).fetchall()
    return [_row_to_profile(r) for r in rows]


def get_profile(name: str) -> Profile | None:
    with session_scope() as conn:
        row = conn.execute(
            "SELECT * FROM worker_profiles WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_profile(row) if row else None


def create_profile(name: str, root: str | None = None,
                   metadata: dict | None = None) -> Profile:
    """Create a profile. Raises ValueError on duplicate name."""
    root_path = str(normalize_path(root)) if root else str(project_root_default())
    now = utcnow_iso()
    meta_json = dump_json(metadata or {})
    with session_scope() as conn:
        existing = conn.execute(
            "SELECT id FROM worker_profiles WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"profile already exists: {name}")
        cur = conn.execute(
            """
            INSERT INTO worker_profiles
                (name, root_path, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, root_path, meta_json, now, now),
        )
        new_id = cur.lastrowid
    fetched = get_profile(name)
    assert fetched is not None and fetched.id == new_id  # invariant
    return fetched


def require_profile(name: str) -> Profile:
    p = get_profile(name)
    if p is None:
        raise LookupError(f"no such profile: {name}")
    return p
