"""Discovery of *native* Claude Code sessions on this host (read-only).

Claude Code (CLI) 가 호스트 머신에 남기는 세션 로그는 보통 다음 경로에 있다:

    ~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl

`<encoded-project-path>` 는 절대 경로의 ``:`` / ``/`` / ``\\`` 가 모두 ``-`` 로
치환된 이름이다. 예) ``D:/work-github/worker-control`` →
``D--work-github-worker-control``.

이 모듈은 그 디렉토리를 **읽기 전용** 으로만 들여다본다. 파일에 손대지 않고,
세션 단위 메타데이터(파일명에서 추출한 UUID, 크기, mtime, 첫 줄에서 추론한
``permissionMode`` / ``leafUuid``) 만 모은다.

위치/형식이 호스트마다 다를 수 있으므로 모든 단계는 ``try/except`` 로 감싸고,
존재하지 않거나 권한이 없으면 빈 결과를 반환한다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_NATIVE_ROOT_ENV = "WORKER_CONTROL_CLAUDE_PROJECTS_DIR"


@dataclass(slots=True)
class NativeSession:
    """단일 native Claude 세션 로그(JSONL) 의 요약."""

    session_id: str            # 파일명에서 뽑은 UUID (보통 36자)
    project_dir_name: str      # ~/.claude/projects 아래 1단계 디렉토리명
    project_path_guess: str    # 디렉토리명을 OS 경로로 역추정한 결과
    jsonl_path: str            # 실제 JSONL 파일 절대경로
    size_bytes: int            # 파일 크기
    modified_at: str           # 마지막 수정 UTC ISO 시간
    permission_mode: str | None = None  # 'auto' / 'manual' / None
    leaf_uuid: str | None = None
    line_count: int | None = None


@dataclass(slots=True)
class NativeSnapshot:
    """``~/.claude/projects`` 전체에 대한 디스커버리 결과."""

    root: str
    root_exists: bool
    sessions: list[NativeSession] = field(default_factory=list)
    note: str | None = None  # 사용자에게 보여줄 경고/안내 (디렉토리 없음 등)


# ----- 경로 / 인코딩 ---------------------------------------------------------

def native_projects_root() -> Path:
    """Claude Code 의 native 세션 로그 루트 경로를 결정.

    우선순위:
    1. ``WORKER_CONTROL_CLAUDE_PROJECTS_DIR`` 환경변수
    2. ``~/.claude/projects`` (HOME 또는 USERPROFILE 기준)
    """
    env = os.environ.get(_NATIVE_ROOT_ENV)
    if env:
        return Path(env)
    home = Path(os.path.expanduser("~"))
    return home / ".claude" / "projects"


def _encode_path_like_claude(path: str) -> str:
    """Claude Code 가 쓰는 인코딩 규칙(추정): ``/`` 와 ``:`` 를 ``-`` 로 치환."""
    return path.replace(":", "-").replace("/", "-").replace("\\", "-")


def _known_root_paths() -> list[Path]:
    """디코딩 휴리스틱이 매칭에 쓸 알려진 워크스페이스 루트 후보들.

    paths 모듈을 지연 import 해서 순환 의존을 피한다.
    """
    try:
        from worker_control.paths import configured_roots
    except Exception:
        return []
    return [r.path for r in configured_roots()]


def _candidate_subpaths(root: Path) -> list[str]:
    """루트의 1단계 자식 디렉토리 이름들 (POSIX-style 슬래시)."""
    try:
        return [
            child.name for child in root.iterdir() if child.is_dir()
        ]
    except OSError:
        return []


def decode_project_dirname(name: str) -> str:
    """``D--work-github-worker-control`` → ``D:/work-github/worker-control`` 추정.

    Claude Code 의 인코딩은 공식 스펙이 없고 ``:`` / ``/`` / ``\\`` 가 모두
    ``-`` 로 치환되는 lossy 인코딩이라 단순 ``-→/`` 치환은 원본 경로의 하이픈
    (예: ``worker-control``) 을 망가뜨린다.

    매칭 전략:

    1. ``configured_roots()`` 의 각 루트에 대해 그 루트의 1단계 자식
       디렉토리를 실제로 열어 보고, ``encode(root)/(child)`` 가 ``name`` 의
       prefix 와 일치하면 정확한 절대 경로를 복원한다. → 원본 하이픈을
       살릴 수 있다.
    2. 1단계 매칭이 안 되더라도 루트만 prefix 로 일치하면, 루트는 원본 그대로
       두고 그 뒤만 ``-→/`` 로 치환 — 깊은 경로에서 정확하지 않을 수 있다.
    3. 알려진 루트 어디에도 안 걸리면 드라이브 prefix(``D--…``) 만 살리고
       나머지는 ``-→/`` 로 단순 치환.
    4. 그것도 아니면 ``-→/`` 로 단순 치환.
    """
    if not name:
        return name

    for root in _known_root_paths():
        root_str = str(root).replace("\\", "/")
        prefix = _encode_path_like_claude(root_str)

        # 1.a) 루트 자체 (자식 없음)
        if name == prefix:
            return root_str

        # 1.b) 루트 + 실제 1단계 자식 디렉토리명 매칭
        if name.startswith(prefix + "-"):
            tail = name[len(prefix) + 1:]
            for child_name in _candidate_subpaths(root):
                enc_child = _encode_path_like_claude(child_name)
                if tail == enc_child:
                    return f"{root_str}/{child_name}"
                if tail.startswith(enc_child + "-"):
                    deeper = tail[len(enc_child) + 1:]
                    return (
                        f"{root_str}/{child_name}/"
                        f"{deeper.replace('-', '/')}"
                    )
            # 2) 루트는 맞았지만 1단계 자식 매칭 실패 — best-effort
            return f"{root_str}/{tail.replace('-', '/')}"

    # 3) 드라이브 prefix 만 인식하는 단순 치환
    m = re.match(r"^([A-Za-z])--(.*)$", name)
    if m:
        drive, rest = m.group(1).upper(), m.group(2)
        return f"{drive}:/{rest.replace('-', '/')}"

    # 4) 폴백
    return name.replace("-", "/")


# ----- 파일 파싱 -------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _iso_from_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _peek_jsonl(path: Path, max_lines: int = 50) -> tuple[str | None, str | None, int]:
    """JSONL 의 앞부분에서 permission_mode / leaf_uuid / 라인 수 추정.

    파일 전체를 다 읽지 않고 ``max_lines`` 줄만 본다.
    파싱 실패는 조용히 무시한다.
    """
    permission_mode: str | None = None
    leaf_uuid: str | None = None
    line_count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line_count += 1
                if line_count > max_lines:
                    # 나머지 줄 수도 빠르게 셈
                    for _extra in fh:
                        line_count += 1
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if permission_mode is None and "permissionMode" in obj:
                    val = obj.get("permissionMode")
                    if isinstance(val, str):
                        permission_mode = val
                if leaf_uuid is None and "leafUuid" in obj:
                    val = obj.get("leafUuid")
                    if isinstance(val, str):
                        leaf_uuid = val
                if permission_mode is not None and leaf_uuid is not None:
                    # 우리가 필요한 메타는 다 얻음. 라인 수만 마저 세기.
                    for _extra in fh:
                        line_count += 1
                    break
    except OSError:
        return None, None, 0
    return permission_mode, leaf_uuid, line_count


def _iter_session_files(project_dir: Path) -> Iterable[Path]:
    try:
        for child in project_dir.iterdir():
            if child.is_file() and child.suffix.lower() == ".jsonl":
                yield child
    except OSError:
        return


def inspect_native_session(jsonl_path: Path, project_dir_name: str) -> NativeSession:
    """단일 .jsonl 파일 → NativeSession 요약."""
    stem = jsonl_path.stem
    sid = stem if _UUID_RE.match(stem) else stem
    try:
        stat = jsonl_path.stat()
        size = stat.st_size
        mtime = _iso_from_mtime(stat.st_mtime)
    except OSError:
        size = 0
        mtime = ""
    pmode, leaf, line_count = _peek_jsonl(jsonl_path)
    return NativeSession(
        session_id=sid,
        project_dir_name=project_dir_name,
        project_path_guess=decode_project_dirname(project_dir_name),
        jsonl_path=str(jsonl_path),
        size_bytes=size,
        modified_at=mtime,
        permission_mode=pmode,
        leaf_uuid=leaf,
        line_count=line_count,
    )


def discover_native_sessions(limit: int | None = None) -> NativeSnapshot:
    """``~/.claude/projects`` 전체를 읽기 전용으로 스캔.

    Parameters
    ----------
    limit:
        총 세션 수 상한. ``None`` 이면 전부 수집. 대시보드에서 너무 많은
        세션이 잡힐 때 잘라낼 수 있게 둠.
    """
    root = native_projects_root()

    # limit == 0 → "디스커버리 자체를 스킵" 으로 해석. root 존재 여부와 무관.
    if limit is not None and limit <= 0:
        return NativeSnapshot(
            root=str(root),
            root_exists=root.exists(),
            note="native 세션 디스커버리가 --native-limit=0 으로 비활성화되었습니다.",
        )

    if not root.exists():
        return NativeSnapshot(
            root=str(root),
            root_exists=False,
            note=(
                "Claude Code native 세션 디렉토리가 발견되지 않았습니다. "
                f"기본 위치 '{root}' 가 없거나 접근할 수 없습니다. "
                f"환경변수 {_NATIVE_ROOT_ENV} 로 명시 지정할 수 있습니다."
            ),
        )

    sessions: list[NativeSession] = []
    note: str | None = None

    try:
        project_dirs = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError as exc:
        return NativeSnapshot(
            root=str(root),
            root_exists=True,
            note=f"native 세션 디렉토리 읽기 실패: {exc}",
        )

    truncated = False
    for pdir in project_dirs:
        for jsonl in _iter_session_files(pdir):
            if limit is not None and len(sessions) >= limit:
                truncated = True
                break
            sessions.append(inspect_native_session(jsonl, pdir.name))
        if truncated:
            break
    if truncated:
        note = (
            f"native 세션이 {limit}개를 초과해 일부만 표시합니다. "
            "더 보려면 --native-limit 을 늘리세요."
        )

    # 최근 수정순으로 정렬 (mtime 가 빈 항목은 맨 뒤로)
    sessions.sort(key=lambda s: s.modified_at or "", reverse=True)
    return NativeSnapshot(
        root=str(root), root_exists=True, sessions=sessions, note=note,
    )
