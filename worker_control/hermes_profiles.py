"""hermes_profiles — read-side discovery of Hermes Agent profiles on disk.

Hermes profiles live as directories under
``~/AppData/Local/hermes/profiles/<name>/`` (and the root ``~/AppData/Local/
hermes/`` itself acts as the implicit ``default`` profile). worker-control's
own ``worker_profiles`` SQLite table is empty by default — users don't have
to ``workerctl profiles create`` to get a usable dashboard; we just scan
disk and present whatever's there.

This module is **read-only**. It never writes to the hermes home — only
reads ``config.yaml``, counts ``skills/``, checks for ``SOUL.md`` / ``.env``,
and surfaces a few fields the dashboard cares about.

Override the hermes home location with the ``HERMES_HOME`` env var
(useful for tests / non-standard installs).
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def hermes_home() -> Path:
    """Return the host's Hermes Agent home directory.

    Resolution order:
      1. ``HERMES_HOME`` env var (absolute path)
      2. Windows: ``%LOCALAPPDATA%\\hermes``
      3. POSIX:  ``~/.local/share/hermes`` if it exists, else ``~/.hermes``
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()

    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "hermes"
        return Path.home() / "AppData" / "Local" / "hermes"

    posix_candidates = [
        Path.home() / ".local" / "share" / "hermes",
        Path.home() / ".hermes",
    ]
    for c in posix_candidates:
        if c.is_dir():
            return c
    return posix_candidates[-1]


@dataclass(slots=True)
class HermesProfile:
    """One Hermes profile, sufficient to render a dashboard row."""
    name: str
    path: str
    is_default: bool        # True for the root hermes home (the implicit default)
    has_config: bool
    has_soul: bool
    has_env: bool
    has_auth: bool
    model: str | None
    provider: str | None
    skills_count: int
    sessions_count: int
    last_session_at: str | None
    config_size: int
    soul_size: int
    last_modified: str | None
    # Some profile homes link to a local wrapper script (e.g. ~/.local/bin/<name>);
    # not authoritative, just useful for the human reading the dashboard.
    alias_hint: str | None


def _isoformat(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    import datetime as _dt
    return _dt.datetime.fromtimestamp(
        st.st_mtime, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_model(config_path: Path) -> tuple[str | None, str | None]:
    """Pull ``model.default`` and ``model.provider`` out of a hermes config.yaml.

    Uses a tiny line-based parser (no PyYAML dep — hermes config has a stable
    flat-ish layout for this section). Returns ``(model, provider)`` and
    silently falls back to ``(None, None)`` on any parse glitch.
    """
    if not config_path.is_file():
        return None, None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    model: str | None = None
    provider: str | None = None
    in_model = False
    for raw in text.splitlines():
        # leave on dedent
        if raw.strip() and not raw.startswith((" ", "\t")):
            if raw.startswith("model:"):
                in_model = True
                continue
            in_model = False
            continue
        if not in_model:
            continue
        line = raw.strip()
        if line.startswith("default:") and model is None:
            model = line.split(":", 1)[1].strip().strip("'\"") or None
        elif line.startswith("provider:") and provider is None:
            provider = line.split(":", 1)[1].strip().strip("'\"") or None
    return model, provider


def _count_skills(profile_dir: Path) -> int:
    """Count SKILL.md files under <profile>/skills/ (recursive)."""
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return 0
    return sum(1 for _ in skills_dir.rglob("SKILL.md"))


def _count_sessions(profile_dir: Path) -> tuple[int, str | None]:
    """Count session_*.json files and return (count, latest_mtime_iso)."""
    sessions_dir = profile_dir / "sessions"
    if not sessions_dir.is_dir():
        return 0, None
    files = list(sessions_dir.glob("session_*.json"))
    if not files:
        return 0, None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return len(files), _isoformat(latest)


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _alias_hint(name: str) -> str | None:
    """Return a wrapper-script path likely to launch this profile, if found."""
    for candidate in [
        Path.home() / ".local" / "bin" / name,
        Path.home() / ".local" / "bin" / f"{name}.cmd",
        Path.home() / ".local" / "bin" / f"{name}.exe",
    ]:
        if candidate.exists():
            return str(candidate)
    return None


def _scan_profile(profile_dir: Path, name: str, is_default: bool) -> HermesProfile:
    config_path = profile_dir / "config.yaml"
    soul_path = profile_dir / "SOUL.md"
    env_path = profile_dir / ".env"
    auth_path = profile_dir / "auth.json"

    model, provider = _extract_model(config_path)
    skills_count = _count_skills(profile_dir)
    sessions_count, last_session_at = _count_sessions(profile_dir)

    return HermesProfile(
        name=name,
        path=str(profile_dir),
        is_default=is_default,
        has_config=config_path.is_file(),
        has_soul=soul_path.is_file(),
        has_env=env_path.is_file(),
        has_auth=auth_path.is_file(),
        model=model,
        provider=provider,
        skills_count=skills_count,
        sessions_count=sessions_count,
        last_session_at=last_session_at,
        config_size=_file_size(config_path),
        soul_size=_file_size(soul_path),
        last_modified=_isoformat(config_path) or _isoformat(profile_dir),
        alias_hint=_alias_hint(name),
    )


def discover_hermes_profiles() -> list[HermesProfile]:
    """Walk the Hermes home and return every profile we can see.

    Order: ``default`` first (the root home), then each ``profiles/<name>/``
    sorted alphabetically. Returns ``[]`` if the hermes home doesn't exist —
    callers should treat that as "no hermes installed, just show worker
    profiles from the DB".
    """
    home = hermes_home()
    out: list[HermesProfile] = []

    # The hermes root home itself is the implicit `default` profile when
    # `hermes profile list` shows it; mirror that behaviour. Only count it
    # as a profile if it has the tell-tale files (config.yaml or auth.json).
    if home.is_dir() and ((home / "config.yaml").is_file()
                          or (home / "auth.json").is_file()):
        out.append(_scan_profile(home, "default", is_default=True))

    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            out.append(_scan_profile(child, child.name, is_default=False))

    return out


def hermes_profile_to_dict(p: HermesProfile) -> dict[str, Any]:
    return asdict(p)
