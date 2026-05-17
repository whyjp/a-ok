"""Scan a project root for git repositories and persist results.

스캐너는 한 루트(또는 여러 루트) 의 1단계 자식 디렉토리를 본다. 각 프로젝트는
``root_role`` (owned_work / public_reference / other) 로 분류된다 — 이 라벨은
``workerctl sessions start`` 의 쓰기 정책 판단에 그대로 쓰인다.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from worker_control.db import dump_json, session_scope, utcnow_iso
from worker_control.paths import (
    ROLE_OTHER,
    classify_path,
    configured_roots,
    normalize_path,
    project_root_default,
)


@dataclass(slots=True)
class ProjectInfo:
    name: str
    path: Path
    is_git: bool
    branch: str | None = None
    remote_url: str | None = None
    is_dirty: bool = False
    root_role: str = ROLE_OTHER


def _git(cwd: Path, *args: str) -> str:
    """Run a git command, return stdout, or empty string on failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def _is_git_repo(path: Path) -> bool:
    """A repo can have either a .git directory or a .git file (worktrees)."""
    return (path / ".git").exists()


def inspect_project(path: Path, root_role: str = ROLE_OTHER) -> ProjectInfo:
    """Inspect a single directory and gather git info if present."""
    is_git = _is_git_repo(path)
    if not is_git:
        return ProjectInfo(name=path.name, path=path, is_git=False, root_role=root_role)
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or None
    remote = _git(path, "remote", "get-url", "origin") or None
    status = _git(path, "status", "--porcelain")
    return ProjectInfo(
        name=path.name,
        path=path,
        is_git=True,
        branch=branch,
        remote_url=remote,
        is_dirty=bool(status),
        root_role=root_role,
    )


def iter_candidates(root: Path) -> Iterable[Path]:
    """First-level child directories of `root`, sorted by name, hidden skipped."""
    if not root.exists():
        return ()
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def scan_root(root: Path | str | None = None) -> list[ProjectInfo]:
    """Scan first-level children of `root` and persist results.

    Returns the list of discovered ProjectInfo entries.
    """
    target = normalize_path(root) if root else project_root_default()
    role = classify_path(target)
    start = time.perf_counter()
    discovered: list[ProjectInfo] = []
    for child in iter_candidates(target):
        discovered.append(inspect_project(child, root_role=role))
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    git_repos = sum(1 for p in discovered if p.is_git)
    now = utcnow_iso()

    with session_scope() as conn:
        for proj in discovered:
            conn.execute(
                """
                INSERT INTO projects
                    (name, path, is_git, branch, remote_url, is_dirty,
                     root_role, last_scan_at, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    path        = excluded.path,
                    is_git      = excluded.is_git,
                    branch      = excluded.branch,
                    remote_url  = excluded.remote_url,
                    is_dirty    = excluded.is_dirty,
                    root_role   = excluded.root_role,
                    last_scan_at= excluded.last_scan_at,
                    updated_at  = excluded.updated_at
                """,
                (
                    proj.name,
                    str(proj.path),
                    1 if proj.is_git else 0,
                    proj.branch,
                    proj.remote_url,
                    1 if proj.is_dirty else 0,
                    proj.root_role,
                    now,
                    dump_json({}),
                    now,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO project_scans
                (root_path, root_role, discovered, git_repos, duration_ms,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(target),
                role,
                len(discovered),
                git_repos,
                elapsed_ms,
                dump_json({"items": [p.name for p in discovered]}),
                now,
            ),
        )
    return discovered


def scan_all_configured_roots() -> dict[str, list[ProjectInfo]]:
    """모든 ``configured_roots()`` 를 순회하며 각각 스캔.

    반환값: ``{role: [ProjectInfo, ...]}`` 형태.
    존재하지 않는 루트(예: D:/work-github 가 비어 있음) 도 안전하게 빈 리스트로
    처리된다.
    """
    out: dict[str, list[ProjectInfo]] = {}
    for root in configured_roots():
        out[root.role] = scan_root(root.path)
    return out
