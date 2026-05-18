"""Transcript parser that produces legacy-parity fields for one session.

Same field shape as the legacy `sites/1143` report (`index.html`). Works
on either:
  * a claude-code jsonl transcript (per-line JSON event stream)
  * a hermes-profile session_*.json document (single nested JSON)

The parser is intentionally tolerant: malformed lines are skipped, unknown
event types are ignored. Output is a dict with at most:
    {
        "session_uuid": str,
        "kind":         "claude" | "hermes",
        "row":          dict (columns for hermes_agent_sessions),
        "pr_links":     list[dict],
        "files_touched":list[dict],
        "tools_recent": list[dict],
        "recaps":       list[dict],
        "pending":      list[dict],
    }

`row` always contains synced_at + hermes_session_id + kind so the caller
can hand it directly to upsert_session_row().
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# regexes & constants
# ---------------------------------------------------------------------------

# GitHub PR or GitLab MR — we capture (host, repo, kind, num).
_PR_RE = re.compile(
    r"https://(?P<host>github\.com|gitlab\.[a-zA-Z0-9\-.]+)/"
    r"(?P<repo>[A-Za-z0-9_./\-]+?)/"
    r"(?P<kind>pull|merge_requests|-/merge_requests)/(?P<num>\d+)",
)
# A-OK spawn prefix per `worker_control_hermes.projects` / `hermes_ledger`.
_A_OK_PREFIX = "a-ok:"

_RECAP_HINTS = (
    "(disable recaps in /config)",
    "/recap ",
    "[recap]",
)

# Buckets that gate effective_status.
_ACTIVE_WINDOW_SEC   = 2 * 3600    # < 2h since last activity → active
_INACTIVE_WINDOW_SEC = 24 * 3600   # < 24h → inactive, else done


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(ts: Any) -> _dt.datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _clip(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    s = re.sub(r"\s+", " ", str(text)).strip()
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _message_text(content: Any) -> str:
    """Flatten claude `message.content` (str or list of blocks) → str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("type")
                if t == "text":
                    out.append(c.get("text") or "")
                elif t == "tool_result":
                    # tool_result is technically a user-typed event but
                    # never a human message — leave it out.
                    return ""
            elif isinstance(c, str):
                out.append(c)
        return " ".join(p for p in out if p)
    return ""


def _spawn_from_name(name: str) -> tuple[str, str, bool]:
    """Return (spawn_slug, spawn_reason, is_spawned) per a-ok: convention."""
    if not name or not isinstance(name, str):
        return "", "", False
    if not name.startswith(_A_OK_PREFIX):
        return "", "", False
    rest = name[len(_A_OK_PREFIX):]
    # a-ok:<slug>__r<run_idx>-<timestamp> or a-ok:<slug>
    m = re.match(r"(?P<slug>[A-Za-z0-9_\-]+?)(?:__r\d+(?:-[\dT:Z+\-]+)?)?$", rest)
    slug = m.group("slug") if m else rest
    return slug, "a-ok-prefix", True


def _classify_status(
    *, last_activity: _dt.datetime | None, ended_at: _dt.datetime | None,
) -> str:
    """active(<2h since last) / inactive(<24h) / done."""
    if ended_at is not None:
        return "done"
    if last_activity is None:
        return "done"
    age = (_now() - last_activity).total_seconds()
    if age < _ACTIVE_WINDOW_SEC:
        return "active"
    if age < _INACTIVE_WINDOW_SEC:
        return "inactive"
    return "done"


# ---------------------------------------------------------------------------
# main entry — claude jsonl
# ---------------------------------------------------------------------------

def parse_claude_jsonl(
    jsonl_path: Path | str,
    *,
    session_uuid: str | None = None,
    worker_name: str | None = None,
    tools_keep: int = 8,
    files_keep: int = 20,
    text_max: int = 200,
) -> dict[str, Any]:
    """Parse a claude-code jsonl transcript into legacy-parity fields.

    `session_uuid` defaults to the jsonl stem (filename without `.jsonl`).
    `worker_name` is the hermes-side stable slug — used for spawn detection
    when not derivable from the transcript itself.
    """
    p = Path(jsonl_path)
    sess_uuid = (session_uuid or p.stem).lower()

    msg_user = 0
    msg_assistant = 0
    msg_tool = 0
    first_ts: _dt.datetime | None = None
    last_ts: _dt.datetime | None = None
    first_user_text: str | None = None
    last_user_text: str | None = None
    last_user_ts: str = ""
    last_assistant_text: str | None = None
    last_assistant_ts: str = ""
    ai_title: str | None = None
    summary: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    claude_version: str | None = None

    pr_links: dict[str, dict[str, Any]] = {}     # url → record
    files_touched: dict[str, dict[str, Any]] = {}
    tools_recent: list[dict[str, Any]] = []
    recaps: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    size_bytes = 0
    try:
        size_bytes = p.stat().st_size
    except OSError:
        pass

    if not p.is_file():
        return _empty_result(
            sess_uuid, "claude", worker_name=worker_name, size_bytes=size_bytes,
        )

    with p.open(encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or not ln.startswith("{"):
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue

            ts = obj.get("timestamp") or ""
            tdt = _parse_iso(ts)
            if tdt is not None:
                if first_ts is None or tdt < first_ts:
                    first_ts = tdt
                if last_ts is None or tdt > last_ts:
                    last_ts = tdt

            # Meta fields appear once near the top, but stick around.
            if obj.get("cwd") and not cwd:
                cwd = obj["cwd"]
            if obj.get("gitBranch") and not git_branch:
                git_branch = obj["gitBranch"]
            if obj.get("version") and not claude_version:
                claude_version = obj["version"]

            t = obj.get("type")
            msg = obj.get("message") or {}
            content = msg.get("content")
            text = _message_text(content)
            text_clean = re.sub(r"\s+", " ", text).strip() if text else ""

            if t == "summary":
                # claude system-generated session summary lives under
                # obj["summary"] (the title) — fits the legacy `ai_title`.
                if obj.get("summary"):
                    ai_title = _clip(obj["summary"], 200)

            elif t == "user":
                # Skip slash-commands and tool_result-as-user wrappers.
                if not text_clean or text_clean.startswith("<") or text_clean.startswith("/"):
                    continue
                msg_user += 1
                if first_user_text is None:
                    first_user_text = _clip(text_clean, text_max)
                if ts >= last_user_ts:
                    last_user_ts = ts
                    last_user_text = _clip(text_clean, text_max)
                _harvest_pr_links(text_clean, pr_links)

            elif t == "assistant":
                msg_assistant += 1
                if ts >= last_assistant_ts:
                    last_assistant_ts = ts
                    last_assistant_text = _clip(text_clean, text_max)
                # native /recap responses leave a fingerprint in the
                # assistant text — collect those as recap_native rows.
                if any(hint in (text or "") for hint in _RECAP_HINTS):
                    recaps.append({
                        "ord": len(recaps),
                        "content": _clip(text_clean, 1200) or "",
                        "ts": ts,
                    })
                _harvest_pr_links(text_clean, pr_links)
                # tool_use blocks live INSIDE the assistant content list.
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        msg_tool += 1
                        _record_tool(c, ts, tools_recent, files_touched)

            elif t == "tool_use":
                # Some recorders also emit top-level tool_use events.
                msg_tool += 1
                _record_tool(obj, ts, tools_recent, files_touched)

            # Pending-queue: jsonl events with type='queued_input' or a
            # boolean flag the user adds via /queue. Not always present;
            # tolerated as an optional channel.
            if obj.get("queued_input") or t == "queued_input":
                qtext = obj.get("text") or text_clean
                if qtext:
                    pending.append({
                        "ord": len(pending),
                        "text": _clip(qtext, 400) or qtext[:400],
                        "queued_at": ts or None,
                    })

    # Last-user without a newer assistant ⇒ pending-by-implicit-queue.
    if last_user_text and last_user_ts > last_assistant_ts:
        if not any(q["text"] == last_user_text for q in pending):
            pending.append({
                "ord": len(pending),
                "text": _clip(last_user_text, 400) or last_user_text[:400],
                "queued_at": last_user_ts or None,
            })

    # Trim tools/files to caps; tools keeps the newest N (ord renumbered),
    # files dedupes and keeps the newest N by last_seen_at.
    tools_recent_trimmed = tools_recent[-tools_keep:]
    for i, t in enumerate(tools_recent_trimmed):
        t["ord"] = i
    files_list = sorted(
        files_touched.values(),
        key=lambda r: r.get("last_seen_at") or "",
        reverse=True,
    )[:files_keep]

    spawn_slug, spawn_reason, is_spawned = _spawn_from_name(worker_name or "")
    effective = _classify_status(last_activity=last_ts, ended_at=None)

    row = {
        "hermes_session_id":   sess_uuid,
        "kind":                "claude",
        "profile_name":        None,
        "profile_path":        None,
        "transcript_path":     str(p),
        "transcript_size":     size_bytes,
        "transcript_mtime":    _file_mtime_iso(p),
        "started_at":          first_ts.isoformat() if first_ts else None,
        "ended_at":            None,
        "model":               None,
        "turn_count":          msg_user + msg_assistant,
        "first_message":       first_user_text,
        "last_message":        last_user_text,
        "cwd":                 cwd,
        "total_cost_usd":      None,
        "synced_at":           _now().isoformat(timespec="seconds"),
        "git_branch":          git_branch,
        "claude_version":      claude_version,
        "msg_user":            msg_user,
        "msg_assistant":       msg_assistant,
        "msg_tool":            msg_tool,
        "ai_title":            ai_title,
        "summary":             summary,
        "first_user_text":     first_user_text,
        "last_user_text":      last_user_text,
        "last_assistant_text": last_assistant_text,
        "size_bytes":          size_bytes,
        "spawn_slug":          spawn_slug,
        "spawn_reason":        spawn_reason,
        "is_spawned":          1 if is_spawned else 0,
        "effective_status":    effective,
    }
    return {
        "session_uuid": sess_uuid,
        "kind":         "claude",
        "row":          row,
        "pr_links":     list(pr_links.values()),
        "files_touched":files_list,
        "tools_recent": tools_recent_trimmed,
        "recaps":       recaps,
        "pending":      pending,
    }


# ---------------------------------------------------------------------------
# main entry — hermes session_*.json
# ---------------------------------------------------------------------------

def parse_hermes_session_json(
    json_path: Path | str,
    *,
    profile_name: str | None = None,
    profile_path: str | None = None,
) -> dict[str, Any]:
    """Parse a hermes-profile session_*.json into legacy-parity fields.

    Hermes JSON is a single nested object whose top-level keys include
    `session_id`, `session_start`, `last_updated`, `message_count`,
    `messages[]`, and (when present) `system_prompt`. We re-use the same
    PR / file / tool / recap extraction across both formats.
    """
    p = Path(json_path)
    size_bytes = 0
    try:
        size_bytes = p.stat().st_size
    except OSError:
        pass
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        doc = {}

    sess_uuid = (
        doc.get("session_id")
        or p.stem.replace("session_", "")
    ).strip().lower()

    started = doc.get("session_start") or doc.get("started_at") or None
    ended = doc.get("last_updated") or doc.get("ended_at") or None

    msgs = doc.get("messages") or []
    msg_user = 0
    msg_assistant = 0
    msg_tool = 0
    first_user_text: str | None = None
    last_user_text: str | None = None
    last_assistant_text: str | None = None
    pr_links: dict[str, dict[str, Any]] = {}
    files_touched: dict[str, dict[str, Any]] = {}
    tools_recent: list[dict[str, Any]] = []
    recaps: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for i, m in enumerate(msgs):
        role = (m.get("role") or m.get("type") or "").lower()
        ts = m.get("timestamp") or m.get("ts") or ""
        text = _message_text(m.get("content"))
        text_clean = re.sub(r"\s+", " ", text).strip() if text else ""
        if role == "user":
            if not text_clean or text_clean.startswith("/"):
                continue
            msg_user += 1
            if first_user_text is None:
                first_user_text = _clip(text_clean, 200)
            last_user_text = _clip(text_clean, 200)
            _harvest_pr_links(text_clean, pr_links)
        elif role == "assistant":
            msg_assistant += 1
            last_assistant_text = _clip(text_clean, 200)
            if any(hint in text for hint in _RECAP_HINTS):
                recaps.append({
                    "ord": len(recaps),
                    "content": _clip(text_clean, 1200) or "",
                    "ts": ts or None,
                })
            _harvest_pr_links(text_clean, pr_links)
        elif role in ("tool", "tool_use"):
            msg_tool += 1
            _record_tool(m, ts, tools_recent, files_touched)

    cwd = None
    sysp = doc.get("system_prompt") or ""
    if isinstance(sysp, str):
        m = re.search(r"Current working directory:\s*(\S+)", sysp)
        if m:
            cwd = m.group(1)

    last_dt = _parse_iso(ended)
    effective = _classify_status(last_activity=last_dt, ended_at=None)

    row = {
        "hermes_session_id":   sess_uuid,
        "kind":                "hermes",
        "profile_name":        profile_name,
        "profile_path":        profile_path,
        "transcript_path":     str(p),
        "transcript_size":     size_bytes,
        "transcript_mtime":    _file_mtime_iso(p),
        "started_at":          started,
        "ended_at":            ended,
        "model":               doc.get("model"),
        "turn_count":          int(doc.get("message_count") or (msg_user + msg_assistant)),
        "first_message":       first_user_text,
        "last_message":        last_user_text,
        "cwd":                 cwd,
        "total_cost_usd":      doc.get("total_cost_usd"),
        "synced_at":           _now().isoformat(timespec="seconds"),
        "git_branch":          None,
        "claude_version":      None,
        "msg_user":            msg_user,
        "msg_assistant":       msg_assistant,
        "msg_tool":            msg_tool,
        "ai_title":            _clip(doc.get("title"), 200),
        "summary":             _clip(doc.get("summary"), 400),
        "first_user_text":     first_user_text,
        "last_user_text":      last_user_text,
        "last_assistant_text": last_assistant_text,
        "size_bytes":          size_bytes,
        "spawn_slug":          "",
        "spawn_reason":        "",
        "is_spawned":          0,
        "effective_status":    effective,
    }
    return {
        "session_uuid": sess_uuid,
        "kind":         "hermes",
        "row":          row,
        "pr_links":     list(pr_links.values()),
        "files_touched":list(files_touched.values()),
        "tools_recent": tools_recent[-8:],
        "recaps":       recaps,
        "pending":      pending,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _file_mtime_iso(p: Path) -> str | None:
    try:
        return _dt.datetime.fromtimestamp(
            p.stat().st_mtime, tz=_dt.timezone.utc,
        ).isoformat(timespec="seconds")
    except OSError:
        return None


def _empty_result(
    sess_uuid: str, kind: str, *, worker_name: str | None = None,
    size_bytes: int = 0,
) -> dict[str, Any]:
    spawn_slug, spawn_reason, is_spawned = _spawn_from_name(worker_name or "")
    return {
        "session_uuid": sess_uuid,
        "kind":         kind,
        "row": {
            "hermes_session_id":   sess_uuid,
            "kind":                kind,
            "synced_at":           _now().isoformat(timespec="seconds"),
            "transcript_size":     size_bytes,
            "turn_count":          0,
            "msg_user":            0,
            "msg_assistant":       0,
            "msg_tool":            0,
            "is_spawned":          1 if is_spawned else 0,
            "spawn_slug":          spawn_slug,
            "spawn_reason":        spawn_reason,
            "effective_status":    "done",
            "size_bytes":          size_bytes,
        },
        "pr_links": [], "files_touched": [],
        "tools_recent": [], "recaps": [], "pending": [],
    }


def _harvest_pr_links(text: str, sink: dict[str, dict[str, Any]]) -> None:
    if not text:
        return
    for m in _PR_RE.finditer(text):
        url = m.group(0)
        if url in sink:
            continue
        host = m.group("host")
        kind = "github" if "github.com" in host else "gitlab"
        sink[url] = {
            "url":  url,
            "num":  int(m.group("num")),
            "repo": m.group("repo"),
            "kind": kind,
        }


def _record_tool(
    tool_obj: dict[str, Any],
    ts: str,
    tools_recent: list[dict[str, Any]],
    files_touched: dict[str, dict[str, Any]],
) -> None:
    """Append one tool record + file-touch side effect to the running lists."""
    name = tool_obj.get("name") or tool_obj.get("tool") or "?"
    inp = tool_obj.get("input") or tool_obj.get("tool_input") or {}
    snippet = ""
    if isinstance(inp, dict):
        first_val = ""
        for k in ("command", "file_path", "path", "pattern", "url", "code"):
            if k in inp and inp[k]:
                first_val = str(inp[k])
                break
        if not first_val:
            for v in inp.values():
                if isinstance(v, str) and v:
                    first_val = v
                    break
        snippet = first_val[:80]
    elif isinstance(inp, str):
        snippet = inp[:80]

    tools_recent.append({
        "ord":     len(tools_recent),
        "name":    name,
        "snippet": snippet,
        "ts":      ts or None,
    })

    # File touches — Edit/Write/Read mostly. Update last_seen_at.
    op_map = {"Edit": "edit", "Write": "write", "Read": "read", "MultiEdit": "edit"}
    op = op_map.get(name)
    if op and isinstance(inp, dict):
        fp = inp.get("file_path") or inp.get("path")
        if fp:
            existing = files_touched.get(fp) or {"path": fp}
            existing["last_seen_at"] = ts or existing.get("last_seen_at")
            existing["op"] = op
            files_touched[fp] = existing
