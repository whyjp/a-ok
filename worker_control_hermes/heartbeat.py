#!/usr/bin/env python
"""
heartbeat.py — 30-minute activity snapshot of all claude-code work on this host.

Looks at two sources, classifies sessions into buckets relative to "now", and
posts a plaintext-friendly summary to Slack so the user always knows what's
running, what just ended, and what's idle.

Data sources:
    1. worker projects.db
        - sessions (uuid, origin, status, name, brief, model, project, …)
        - runs     (per-invocation timeline of spawned sessions)
    2. ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
        - file mtime  → most reliable "last touched" signal for native sessions
        - subagents/agent-*.jsonl  → sub-agent invocations

Classification (relative to NOW, with window=30 min):
    🟢 ALIVE        last event ≤ 5 min ago                           (still working)
    ✅ JUST ENDED   sessions explicitly closed in this window         (hermes only — status='done'/'failed' AND ended_at within window)
    💤 IDLE         5 min < last event ≤ 30 min ago, no explicit end (might be done, might be paused — claude-code doesn't emit a "closed" marker)
    (everything older is omitted)

The whole script is read-only against both data sources except for the Slack POST.

Run modes:
    python scripts/heartbeat.py              # build + print to stdout
    python scripts/heartbeat.py --post       # also post to Slack DM (uses SLACK_BOT_TOKEN env)
    python scripts/heartbeat.py --window 60  # widen the window from default 30 min

Designed to be called by a Hermes cron job every 30 minutes:
    hermes cronjob create … --schedule "*/30 * * * *" \\
        --script "C:\\Users\\cxx\\AppData\\Local\\hermes\\profiles\\worker\\scripts\\heartbeat.py --post" \\
        --no-agent
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

# subprocs is a sibling module in our package. The legacy bare ``import
# subprocs`` form is kept as a last-resort fallback for old hand-installed
# copies that lived next to the script, but the package import takes
# priority so wrappers don't accidentally recurse into themselves.
try:
    from worker_control_hermes import subprocs as _subprocs_mod  # type: ignore
except ImportError:
    try:
        import subprocs as _subprocs_mod  # type: ignore  # legacy fallback
    except ImportError:
        _subprocs_mod = None  # type: ignore[assignment]

PROFILE_HOME = Path(__file__).resolve().parent.parent
DB_PATH      = Path(os.environ.get("WORKER_PROJECTS_DB", r"D:/work-github/.worker-control/worker-control.sqlite3"))
CLAUDE_PROJECTS_DIR = Path(os.environ.get(
    "CLAUDE_PROJECTS_DIR",
    Path.home() / ".claude" / "projects",
))
SLACK_USER_ID = os.environ.get("WORKER_SLACK_USER_ID", "U05DT4P6LN8")

NOW = _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    s = ts.strip()
    # Normalize trailing Z and naive-utc.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _fmt_age(d: _dt.datetime | None) -> str:
    if not d:
        return "?"
    delta = NOW - d
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in future?"
    if secs < 90:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 36:
        return f"{hrs}h ago"
    days = hrs // 24
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# pull data
# ---------------------------------------------------------------------------

def _load_db_sessions() -> list[dict]:
    """Return one dict per session from the unified ``session_view`` reader.

    PR #5 (Phase 2): heartbeat used to query ``hermes_sessions`` directly,
    then synthesize in-memory rows for jsonl files the ledger hadn't
    picked up (the `_synthetic=True` block — deleted in this PR).
    ``session_sync`` now persists those rows, so we just read what's in
    the DB. The returned dict shape is preserved (``name`` / ``origin`` /
    ``proj_name`` / ``proj_path`` / ``runs``) so the rest of the
    classification + render pipeline doesn't have to change.
    """
    if not DB_PATH.is_file():
        return []
    # Local import: keeps the heartbeat script importable from a checkout
    # where worker_control isn't on sys.path yet (it normally is — the
    # package install path adds it — but a manual `python heartbeat.py`
    # from inside the worker_control_hermes dir benefits from the lazy bind).
    from worker_control.session_view import list_sessions as _list_sessions

    views = _list_sessions()
    if not views:
        return []

    # Per-session runs are still needed for `_line()` / the slack render
    # (it shows last_run mode/status/started_at). Pull them in one query
    # keyed by session_id to avoid N+1.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    runs_by_sid: dict[int, list[dict]] = {}
    try:
        for r in conn.execute(
            "SELECT session_id, id, run_index, mode, status, "
            "       started_at, ended_at, name FROM hermes_runs "
            "ORDER BY started_at"
        ):
            runs_by_sid.setdefault(r["session_id"], []).append({
                k: r[k] for k in
                ("id", "run_index", "mode", "status", "started_at", "ended_at", "name")
            })
    finally:
        conn.close()

    out: list[dict] = []
    for v in views:
        out.append({
            "id":                v.id,
            "uuid":              v.uuid,
            "name":              v.name,
            "origin":            v.origin,
            "status":            v.status,
            "display_status":    v.display_status,
            "brief":             v.brief,
            "model":             v.model,
            "last_used_at":      v.last_used_at,
            "ended_at":          v.ended_at,
            "created_at":        v.created_at,
            "claude_name":       v.claude_name,
            "claude_status":     v.claude_status,
            "claude_status_at":  v.claude_status_at,
            "proj_name":         v.project_name,
            "proj_path":         v.project_path,
            # Tool-invocation total — surfaced in Slack as a compact
            # `🔧 tools×N` chip. The actual command/snippet text is NOT
            # propagated; only the count survives this hop.
            "msg_tool":          v.msg_tool,
            "runs":              runs_by_sid.get(v.id, []),
        })
    return out


def _jsonl_lookup() -> dict[str, dict]:
    """Map session UUID → {jsonl_path, mtime, subagent_count, subagent_mtime}.

    File mtime is a more accurate "last touched" signal than the
    last_used_at column for native sessions (sync-native may be stale).
    """
    out: dict[str, dict] = {}
    if not CLAUDE_PROJECTS_DIR.is_dir():
        return out
    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            uid = jl.stem.lower()
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", uid):
                continue
            try:
                mtime = _dt.datetime.fromtimestamp(jl.stat().st_mtime, tz=_dt.timezone.utc)
            except OSError:
                continue
            info = {"jsonl": jl, "mtime": mtime, "subs": []}
            sub_dir = jl.parent / jl.stem / "subagents"
            if sub_dir.is_dir():
                for sub in sub_dir.glob("agent-*.jsonl"):
                    try:
                        sm = _dt.datetime.fromtimestamp(sub.stat().st_mtime, tz=_dt.timezone.utc)
                    except OSError:
                        continue
                    info["subs"].append({"path": sub, "mtime": sm})
            out[uid] = info
    return out


def _extract_current_prompt(jsonl_path: Path, max_chars: int = 200) -> tuple[str, bool]:
    """Return (latest_user_prompt, is_pending) for an ALIVE session.

    Scans the jsonl tail to find:
      - the most recent meaningful `user` event (non-slash, non-meta), and
      - whether any `assistant` event timestamp follows it.

    A user event newer than the last assistant event ⇒ claude-code is
    currently processing that prompt. Useful for the heartbeat's ALIVE
    bucket so the user can see *what* the session is chewing on right now,
    not just the first thing they asked.

    Reads at most the last ~256 KB of the file (jsonls grow line-by-line,
    so the latest turn is always at the end). Falls back gracefully on
    parse errors or empty files.
    """
    try:
        st = jsonl_path.stat()
        size = st.st_size
        with jsonl_path.open("rb") as fh:
            if size > 262144:
                fh.seek(size - 262144)
                fh.readline()  # discard partial line at start of the chunk
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return "", False

    last_user_text = ""
    last_user_ts = ""
    last_assistant_ts = ""
    for ln in blob.splitlines():
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        t = obj.get("type")
        ts = obj.get("timestamp") or ""
        if t == "assistant":
            if ts > last_assistant_ts:
                last_assistant_ts = ts
        elif t == "user":
            msg = obj.get("message") or {}
            content = msg.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict):
                        # Skip tool_result events — they're encoded as user
                        # messages but they're not human prompts.
                        if c.get("type") == "tool_result":
                            text = ""
                            parts = []
                            break
                        if c.get("type") == "text":
                            parts.append(c.get("text") or "")
                    elif isinstance(c, str):
                        parts.append(c)
                text = " ".join(p for p in parts if p)
            text = re.sub(r"\s+", " ", text).strip()
            # Skip slash commands and meta-resume / system-injected markers.
            if not text or text.startswith("<") or text.startswith("/"):
                continue
            if ts >= last_user_ts:
                last_user_ts = ts
                last_user_text = text

    if not last_user_text:
        return "", False
    if len(last_user_text) > max_chars:
        last_user_text = last_user_text[: max_chars - 1].rstrip() + "…"
    is_pending = bool(last_user_ts) and last_user_ts > last_assistant_ts
    return last_user_text, is_pending


def _extract_first_user_text(jsonl_path: Path, max_chars: int = 140) -> str:
    """Return a one-line natural-language label for a session.

    Reads the first `user` message from the jsonl and flattens its content
    into plain text. Falls back to "" if nothing usable is found in the
    first ~50 lines (claude-code sometimes emits system / tool-result
    events before the human's first turn). The 50-line cap keeps this
    cheap when called per-session on every heartbeat tick.
    """
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh):
                if i > 50:
                    break
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text") or "")
                        elif isinstance(c, str):
                            parts.append(c)
                    text = " ".join(p for p in parts if p)
                text = re.sub(r"\s+", " ", text).strip()
                # Skip slash-command echoes and meta-resume markers.
                if not text or text.startswith("<") or text.startswith("/"):
                    continue
                if len(text) > max_chars:
                    text = text[: max_chars - 1].rstrip() + "…"
                return text
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def classify_sessions(window_min: int) -> dict:
    # Heartbeat tick — also a good moment to refresh the legacy-parity
    # tables (hermes_agent_sessions + child rows). Mtime-keyed skip keeps
    # this cheap when transcripts haven't moved. Failures are non-fatal:
    # the heartbeat must keep producing its Slack summary even if the
    # parity ingestion hits an unexpected jsonl shape.
    try:
        from worker_control_hermes.legacy_parity_ingest import ingest_all as _parity_ingest
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _parity_stats = _parity_ingest(_conn)
        _conn.close()
        print(f"[parity-ingest] {_parity_stats}", file=sys.stderr)
    except Exception as e:
        print(f"[parity-ingest] failed: {e!r}", file=sys.stderr)

    # Ledger gap backfill — scan transcripts for spawn signatures and
    # close/synthesize ``hermes_runs`` rows the dispatcher trap missed.
    # Independent of the parity ingest above (different tables, different
    # failure modes); errors are swallowed so a bad transcript can't
    # break the heartbeat tick.
    try:
        from worker_control_hermes.spawn_backfill import backfill_all as _spawn_backfill
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _bf_stats = _spawn_backfill(_conn, window_hours=168, dry_run=False)
        _conn.close()
        # Trim the noisy per-session breakdown out of the heartbeat log
        # — leave just the headline counters.
        _bf_brief = {
            k: _bf_stats.get(k, 0) for k in
            ("sessions_scanned", "sessions_with_changes",
             "updated", "inserted", "relinked", "ambiguous", "skipped")
        }
        print(f"[spawn-backfill] {_bf_brief}", file=sys.stderr)
    except Exception as e:
        print(f"[spawn-backfill] failed: {e!r}", file=sys.stderr)

    sessions = _load_db_sessions()
    jsonl = _jsonl_lookup()
    db_uuids = {s["uuid"].lower() for s in sessions}
    # PR #5 (Phase 2): the previous in-memory `_synthetic=True` block that
    # synthesized native rows for jsonl files missing from the ledger has
    # been deleted. `session_sync` (PR #4) now persists those rows on the
    # heartbeat tick (via the dashboard's auto-sync thread or `workerctl
    # session sync-all`), so `_load_db_sessions()` already returns them.
    # Any UUID still missing from `db_uuids` after this point is a genuine
    # gap to investigate (e.g. its cwd doesn't decode), not a transient
    # one to paper over.

    # Subprocess scan — discover every workload (Go binary, test runner,
    # long-lived server) owned by a claude-code session, persist it to the
    # `subprocs` table, then bucket each session's subprocs into alive/ended.
    # This is the "양측 처리" the user asked for: discovery is live (so the
    # heartbeat reflects this very second), but the snapshot is also written
    # to the DB so it survives across heartbeat ticks and we can detect the
    # ended-since-last-scan transition.
    subprocs_by_uuid: dict[str, dict] = {}
    subprocs_stats = {"alive": 0, "ended_now": 0, "kept": 0, "available": False}
    if _subprocs_mod is not None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            _records, subprocs_stats = _subprocs_mod.scan_and_persist(conn)
            subprocs_stats["available"] = True
            # Re-fetch from DB so we get the just-ended rows too.
            window_cutoff_iso = (NOW - _dt.timedelta(minutes=window_min)).isoformat(timespec="seconds")
            for row in conn.execute(
                "SELECT * FROM hermes_subprocs WHERE status='alive' OR "
                "(status='ended' AND ended_at >= ?)",
                (window_cutoff_iso,),
            ):
                uid = (row["session_uuid"] or "").lower()
                bucket = subprocs_by_uuid.setdefault(uid, {"alive": [], "just_ended": []})
                target = "alive" if row["status"] == "alive" else "just_ended"
                bucket[target].append(dict(row))
            conn.close()
        except Exception as e:
            # Surface to stderr but never fail the heartbeat over a psutil hiccup.
            print(f"[subprocs] scan failed: {e!r}", file=sys.stderr)

    alive_cutoff   = NOW - _dt.timedelta(minutes=5)
    window_cutoff  = NOW - _dt.timedelta(minutes=window_min)

    alive, just_ended, idle = [], [], []
    for s in sessions:
        # Best-known "last activity".
        # 1. jsonl mtime (most accurate for native) if available
        # 2. runs[-1].started_at if we recorded a run
        # 3. last_used_at column
        last = None
        jl = jsonl.get(s["uuid"].lower())
        if jl:
            last = jl["mtime"]
            if jl["subs"]:
                last = max(last, max(sub["mtime"] for sub in jl["subs"]))
        for r in s["runs"]:
            for k in ("ended_at", "started_at"):
                t = _parse_iso(r.get(k))
                if t and (last is None or t > last):
                    last = t
        t = _parse_iso(s.get("last_used_at"))
        if t and (last is None or t > last):
            last = t

        s["_last"] = last
        s["_subagents"] = len(jl["subs"]) if jl else 0
        s["_subagents_active"] = sum(1 for sub in (jl or {}).get("subs", [])
                                     if sub["mtime"] >= window_cutoff) if jl else 0

        # Subprocess attribution (workload children: Go pipelines, test runners
        # …). A session with live workload subprocs should NOT be classified
        # as idle even if the jsonl is silent — that's exactly the
        # native-1bd94511 / ie-e2e-test case that prompted this work.
        sp_bucket = subprocs_by_uuid.get(s["uuid"].lower()) or {"alive": [], "just_ended": []}
        # Keep tail.exe out of the user-visible subprocess list — it's
        # infrastructure noise, not workload. We still persist it in the DB
        # for forensics, but we don't render it.
        s["_subprocs_alive"] = [r for r in sp_bucket["alive"] if r.get("kind") != "tail"]
        s["_subprocs_just_ended"] = [r for r in sp_bucket["just_ended"] if r.get("kind") != "tail"]

        # Claude-Code official registry & per-session tasks. These give us a
        # busy/idle signal straight from claude-code itself (more accurate
        # than guessing from jsonl mtime) and the user-facing TODO list
        # the session is currently chewing on.
        #
        # Mutable claude-side label is kept under `_claude_name` and rendered
        # SEPARATELY from `s["name"]` (the hermes-assigned stable slug).
        # Mixing them would break the spawned-vs-native classifier which keys
        # off the `scv-` prefix on `s["name"]`.
        s["_claude_status"] = s.get("claude_status")  # DB-cached fallback
        s["_claude_name"]   = s.get("claude_name")
        s["_active_tasks"]  = []
        if _subprocs_mod is not None:
            try:
                reg = _subprocs_mod.claude_session_status(s["uuid"])
                if reg:
                    # Live registry beats DB cache when both exist.
                    s["_claude_status"] = reg.get("status")
                    s["_claude_name"]   = reg.get("name") or s["_claude_name"]
                tasks = _subprocs_mod.claude_session_tasks(s["uuid"])
                in_prog = [t for t in tasks if t.get("status") == "in_progress"]
                pending = [t for t in tasks if t.get("status") == "pending"]
                s["_active_tasks"] = in_prog[:3] + pending[:2]
            except Exception as e:
                print(f"[subprocs] claude lookup failed for {s['uuid'][:8]}: {e!r}",
                      file=sys.stderr)

        # If any workload subproc is alive OR claude reports 'busy', treat
        # the session as having very recent activity even if its jsonl
        # mtime hasn't moved.
        if s["_subprocs_alive"] or s["_claude_status"] == "busy":
            last = NOW  # forces ALIVE bucket below
            s["_last"] = NOW
        # Natural-language label for the Slack card. Prefer the worker-DB
        # brief (curated one-liner), fall back to the jsonl's first user
        # message. Empty string means "no caption available".
        nl = (s.get("brief") or "").strip()
        if not nl and jl:
            nl = _extract_first_user_text(jl["jsonl"])
        s["_summary"] = nl

        # For sessions still active in this window, also extract the latest
        # user prompt so the heartbeat can show what the session is working
        # on *right now* — not just its opening question.
        s["_current_prompt"] = ""
        s["_pending"] = False
        if last is not None and jl:
            cp, pending = _extract_current_prompt(jl["jsonl"])
            # Only attach if it adds info beyond the first-user summary.
            if cp and cp != s["_summary"]:
                s["_current_prompt"] = cp
                s["_pending"] = pending
            elif cp and pending:
                # Same as the opening prompt but no assistant reply yet —
                # still useful to flag as pending.
                s["_pending"] = pending

        if last is None:
            continue
        if last < window_cutoff:
            continue   # outside window — skip

        # Explicit "just ended" path for hermes-tracked sessions.
        ended_at = _parse_iso(s.get("ended_at"))
        if s.get("status") in ("done", "failed", "abandoned") and ended_at and ended_at >= window_cutoff:
            just_ended.append(s)
            continue

        if last >= alive_cutoff:
            alive.append(s)
        else:
            idle.append(s)

    # Sort each bucket by recency desc.
    for bucket in (alive, just_ended, idle):
        bucket.sort(key=lambda s: s.get("_last") or NOW, reverse=True)

    # Sweep candidates — a-ok: runs stuck in 'started' beyond the default
    # safety-net window (24h). These would be cleaned by
    # `workerctl-hermes-projects runs sweep`. Surfaced here so an operator
    # scrolling the heartbeat snapshot sees the leak before the user does.
    sweep_candidates: list[dict] = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cutoff_iso = (NOW - _dt.timedelta(hours=24)).isoformat(timespec="seconds")
        sweep_candidates = [dict(r) for r in conn.execute(
            "SELECT r.id, r.name, r.started_at, s.uuid AS session_uuid "
            "FROM hermes_runs r JOIN hermes_sessions s ON s.id = r.session_id "
            "WHERE r.status='started' AND r.name LIKE 'a-ok:%' "
            "AND r.started_at < ? "
            "ORDER BY r.started_at ASC",
            (cutoff_iso,),
        ).fetchall()]
        conn.close()
    except Exception as e:
        print(f"[sweep-probe] failed: {e!r}", file=sys.stderr)

    return {
        "alive": alive,
        "just_ended": just_ended,
        "idle": idle,
        "window_min": window_min,
        "total_db_sessions": len(db_uuids),
        "total_jsonl_sessions": len(jsonl),
        "subprocs_stats": subprocs_stats,
        "sweep_candidates": sweep_candidates,
    }


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _mention_id(s: dict) -> str:
    """Return a copy/mention-friendly identifier for a session.

    spawned: the hermes-managed slug (already `a-ok:<slug>`), with a
        derived `a-ok:<uuid-short>` fallback for legacy rows that
        predate the slug naming convention.
    native: `native:<claude_name>` if claude-code's own session name is
        known, else `native:<name>` (user-set via `/name`), else
        `native:<uuid-short>` so the user can always copy *something*
        stable to refer to the session.
    """
    uuid_short = (s.get("uuid") or "")[:8]
    if s.get("origin") == "spawned":
        nm = (s.get("name") or "").strip()
        if nm.startswith("a-ok:"):
            return nm
        return f"a-ok:{uuid_short}"
    cn = (s.get("_claude_name") or "").strip()
    nm = (s.get("name") or "").strip()
    label = cn or nm or uuid_short
    return label if label.startswith("native:") else f"native:{label}"


def _line(s: dict) -> str:
    """One-line description of a session for the Slack snapshot."""
    origin_tag = "spawned" if s["origin"] == "spawned" else "native"
    name = (s["name"] or "(unnamed)")[:36]
    proj = (s["proj_name"] or Path(s["proj_path"] or "").name or "(unknown)")[:24]
    uuid_short = s["uuid"][:8]
    age = _fmt_age(s["_last"])
    mid = _mention_id(s)

    if mid == (s.get("name") or "").strip():
        # name == mention_id: no point repeating it — just keep the
        # uuid-short tail so the line still carries a stable identifier.
        title = f"[{origin_tag}] {proj} / {name}  ({uuid_short})"
    else:
        title = f"[{origin_tag}] {proj} / {name}  [{mid}]"
    cn = (s.get("_claude_name") or "").strip()
    if cn and cn != (s["name"] or ""):
        title += f"  (claude: {cn[:36]})"
    bits = [title, f"        last event {age}"]
    if s["runs"]:
        last_run = s["runs"][-1]
        run_age = _fmt_age(_parse_iso(last_run.get("started_at")))
        bits[-1] += f"  ·  run #{last_run['run_index']} {last_run['mode']} ({last_run['status']}, {run_age})"
    if s.get("_subagents_active"):
        bits[-1] += f"  ·  {s['_subagents_active']} subagents 🔵"
    elif s.get("_subagents"):
        bits[-1] += f"  ·  {s['_subagents']} subagents"
    if s.get("brief"):
        b = re.sub(r"\s+", " ", s["brief"]).strip()
        if b:
            bits.append(f"        “{b[:90]}{'…' if len(b) > 90 else ''}”")
    elif s.get("_summary"):
        # No curated brief — show the jsonl's first-user-message as the
        # session caption so the user can tell at a glance what the human
        # actually asked.
        b = s["_summary"]
        bits.append(f"        ▸ {b[:110]}{'…' if len(b) > 110 else ''}")
    # Latest in-flight prompt (ALIVE sessions): show what claude is
    # chewing on right now, separately from the opening question.
    if s.get("_current_prompt"):
        marker = "🟡 now" if s.get("_pending") else "↳ now"
        cp = s["_current_prompt"]
        bits.append(f"        {marker}: {cp[:140]}{'…' if len(cp) > 140 else ''}")
    elif s.get("_pending"):
        bits.append("        🟡 processing (no assistant reply yet)")
    # Claude-Code TODOs for this session — the authoritative "what is the
    # session working on" signal, written by claude itself.
    for t in (s.get("_active_tasks") or [])[:2]:
        st = t.get("status", "?")
        subj = (t.get("subject") or t.get("activeForm") or "").strip()
        if not subj:
            continue
        icon = "🔧" if st == "in_progress" else "⏳"
        bits.append(f"        {icon} task #{t.get('id','?')}: {subj[:140]}")
    # Workload subprocesses owned by this session — surfaced as a name + pid
    # only. The cmdline / arg vector was historically rendered inline, but
    # it leaks command snippets the user explicitly asked not to see in
    # Slack; the DB still has the full cmdline for forensics.
    for sp in (s.get("_subprocs_alive") or [])[:3]:
        age = _fmt_age(_parse_iso(sp.get("started_at")))
        bits.append(f"        ⚙ {sp.get('name','?')}  pid={sp.get('pid')}  (started {age})")
    extra_alive = len(s.get("_subprocs_alive") or []) - 3
    if extra_alive > 0:
        bits.append(f"        ⚙ +{extra_alive} more alive subproc(s)")
    for sp in (s.get("_subprocs_just_ended") or [])[:2]:
        age = _fmt_age(_parse_iso(sp.get("ended_at")))
        bits.append(f"        ☑ ended {sp.get('name','?')} pid={sp.get('pid')} ({age})")
    # Compact tool-call counter — only the number survives, not the
    # individual Bash/Edit/Write invocations or their argument strings.
    tools_n = int(s.get("msg_tool") or 0)
    if tools_n:
        bits.append(f"        🔧 tools×{tools_n}")
    return "\n".join(bits)


def render_text(snap: dict) -> str:
    hdr_local = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    n_alive = len(snap["alive"])
    n_ended = len(snap["just_ended"])
    n_idle  = len(snap["idle"])
    sp = snap.get("subprocs_stats") or {}
    sp_suffix = ""
    if sp.get("available"):
        sp_suffix = (f"  ·  subprocs alive={sp.get('alive',0)} "
                     f"ended={sp.get('ended_now',0)}")
    lines = [
        f"🫀 worker heartbeat — {hdr_local}  (window: last {snap['window_min']} min)",
        f"   alive={n_alive}  just-ended={n_ended}  idle={n_idle}  "
        f"·  db={snap['total_db_sessions']}  jsonl={snap['total_jsonl_sessions']}"
        f"{sp_suffix}",
        "",
    ]
    if n_alive:
        lines.append(f"🟢 ALIVE — {n_alive}  (last event ≤ 5 min)")
        for s in snap["alive"]:
            lines.append(_line(s))
        lines.append("")
    if n_ended:
        lines.append(f"✅ JUST ENDED — {n_ended}  (closed within window, hermes-tracked)")
        for s in snap["just_ended"]:
            lines.append(_line(s))
        lines.append("")
    if n_idle:
        lines.append(f"💤 IDLE — {n_idle}  (5–{snap['window_min']} min, no explicit close)")
        for s in snap["idle"]:
            lines.append(_line(s))
        lines.append("")
    if not (n_alive or n_ended or n_idle):
        lines.append("(아무 활동 없음 — 마지막 30분 동안 어떤 claude 세션도 움직이지 않음)")
    sweep = snap.get("sweep_candidates") or []
    if sweep:
        lines.append("")
        lines.append(
            f"🧹 SWEEP CANDIDATES — {len(sweep)}  "
            f"(a-ok: runs stuck `started` >24h — run "
            f"`workerctl-hermes-projects runs sweep` to clean)"
        )
        for r in sweep[:5]:
            lines.append(
                f"  run #{r['id']:>5}  started_at={r['started_at']}  "
                f"name={r['name']}"
            )
        if len(sweep) > 5:
            lines.append(f"  … +{len(sweep) - 5} more")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Block Kit render (Slack-rich)
# ---------------------------------------------------------------------------

def _slack_esc(s: str) -> str:
    """Escape the three characters Slack mrkdwn treats specially."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_text_for(s: dict, bucket: str) -> str:
    """Markdown for one session block in a Slack card.

    Layout (Slack mrkdwn). The bucket emoji is intentionally absent from
    each session — the bucket header above already carries it, so repeating
    it per row is visual noise. Origin (native/spawned) is dropped to the
    meta line for the same reason; the dominant signal on the title row is
    *what* and *when*, not *how it was started*.

        *project* / `session-name`   ·   3m ago
        > <natural-language summary, rendered as a quote bar>
        _native · run #2 print (done, 5m) · 2 subagents 🔵 · sonnet_
    """
    proj = (s.get("proj_name") or Path(s.get("proj_path") or "").name or "(unknown)")
    name = s["name"] or "(unnamed)"
    age = _fmt_age(s["_last"])
    origin_tag = "spawned" if s["origin"] == "spawned" else "native"
    mid = _mention_id(s)

    if mid == (s.get("name") or "").strip():
        # mention_id and the hermes-assigned slug are identical — render
        # one combined chip rather than `<mid>` `<name>` side by side.
        head = (
            f"*{_slack_esc(proj)}* / `{_slack_esc(name)}`"
            f"  ·  {_slack_esc(age)}"
        )
    else:
        head = (
            f"`{_slack_esc(mid)}`  *{_slack_esc(proj)}* / `{_slack_esc(name)}`"
            f"  ·  {_slack_esc(age)}"
        )
    # Mutable claude-side label — shown ONLY when it diverges from the
    # hermes-managed slug, so the user can see "I /renamed this session in
    # claude to '3day_sampler'" without us ever clobbering the stable name.
    cn = (s.get("_claude_name") or "").strip()
    if cn and cn != name:
        head += f"  _(claude: `{_slack_esc(cn)}`)_"

    summary = (s.get("brief") or "").strip() or (s.get("_summary") or "").strip()

    tail_bits = [origin_tag]
    if s.get("_claude_status"):
        # claude-code's own busy/idle flag — far more reliable than mtime guessing.
        tail_bits.append(f"claude:{s['_claude_status']}")
    if s["runs"]:
        last_run = s["runs"][-1]
        run_age = _fmt_age(_parse_iso(last_run.get("started_at")))
        tail_bits.append(
            f"run #{last_run['run_index']} {last_run['mode']} "
            f"({last_run['status']}, {run_age})"
        )
    if s.get("_subagents_active"):
        tail_bits.append(f"{s['_subagents_active']} subagents 🔵")
    elif s.get("_subagents"):
        tail_bits.append(f"{s['_subagents']} subagents")
    if s.get("model"):
        tail_bits.append(str(s["model"]))

    lines = [head]
    if summary:
        # Slack blockquote — gives the summary a left bar so the content
        # area visually separates from the title/meta rows.
        sm = summary[:200] + ("…" if len(summary) > 200 else "")
        lines.append("> " + _slack_esc(sm))
    # Latest in-flight prompt (only when distinct from the opening summary).
    # When `_pending` is set the session is mid-turn — flag it so the user
    # can tell at a glance whether claude is currently working on something
    # or just waiting on its next instruction.
    cp = (s.get("_current_prompt") or "").strip()
    if cp:
        marker = "🟡 *now*" if s.get("_pending") else "↳ *now*"
        cp_text = cp[:220] + ("…" if len(cp) > 220 else "")
        lines.append(f"{marker}: {_slack_esc(cp_text)}")
    elif s.get("_pending"):
        lines.append("🟡 _processing (no assistant reply yet)_")
    # Claude-Code's own TODO list for this session — surface the
    # in_progress task as a "task: …" line, plus the next pending so
    # the user sees the queue. Much more accurate than guessing from
    # the latest user prompt.
    for t in (s.get("_active_tasks") or [])[:3]:
        st = t.get("status", "?")
        subj = (t.get("subject") or t.get("activeForm") or "").strip()
        if not subj:
            continue
        sm = subj[:160] + ("…" if len(subj) > 160 else "")
        icon = "🔧" if st == "in_progress" else "⏳"
        lines.append(f"{icon} *task* `#{t.get('id','?')}`: {_slack_esc(sm)}")
    # Workload subprocesses — name + pid only, no cmdline. The argv
    # contains tool snippets the user asked us not to surface in Slack;
    # the DB still has it for forensics.
    for sp in (s.get("_subprocs_alive") or [])[:3]:
        age = _fmt_age(_parse_iso(sp.get("started_at")))
        head = (
            f"⚙ `{_slack_esc(sp.get('name','?'))}` pid `{sp.get('pid')}` "
            f"_started {_slack_esc(age)}_"
        )
        lines.append(head)
    extra_alive = len(s.get("_subprocs_alive") or []) - 3
    if extra_alive > 0:
        lines.append(f"⚙ _+{extra_alive} more alive subproc(s)_")
    for sp in (s.get("_subprocs_just_ended") or [])[:2]:
        age = _fmt_age(_parse_iso(sp.get("ended_at")))
        lines.append(
            f"☑ _ended_ `{_slack_esc(sp.get('name','?'))}` pid `{sp.get('pid')}` "
            f"_({_slack_esc(age)})_"
        )
    # Compact tool-call counter — surfaced as a single chip on the tail
    # row alongside other metadata.
    tools_n = int(s.get("msg_tool") or 0)
    if tools_n:
        tail_bits.append(f"🔧 tools×{tools_n}")
    if tail_bits:
        lines.append("_" + "  ·  ".join(_slack_esc(b) for b in tail_bits) + "_")

    return "\n".join(lines)


def render_blocks(snap: dict) -> list[dict]:
    """Build a Slack Block Kit payload for the heartbeat.

    Slack doesn't have real tables; this approximates one using a header,
    a context strip with totals, and one section per session grouped by
    bucket with dividers between buckets. Sections cap text at 3000 chars,
    so we batch long bucket lists into multiple sections of up to 5
    sessions each (keeps individual blocks comfortably under the limit).
    """
    hdr_local = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    n_alive = len(snap["alive"])
    n_ended = len(snap["just_ended"])
    n_idle  = len(snap["idle"])
    window_min = snap["window_min"]

    blocks: list[dict] = [
        {"type": "header", "text": {
            "type": "plain_text",
            "text": f"🫀 worker heartbeat · {hdr_local}",
        }},
        {"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": (
                f"window: last *{window_min}* min  ·  "
                f"🟢 alive *{n_alive}*  ·  ✅ just-ended *{n_ended}*  ·  "
                f"💤 idle *{n_idle}*  ·  "
                f"db={snap['total_db_sessions']} · jsonl={snap['total_jsonl_sessions']}"
                + (
                    f"  ·  ⚙ subprocs alive=*{(snap.get('subprocs_stats') or {}).get('alive',0)}* "
                    f"ended=*{(snap.get('subprocs_stats') or {}).get('ended_now',0)}*"
                    if (snap.get("subprocs_stats") or {}).get("available") else ""
                )
            ),
        }]},
    ]

    def _add_bucket(label: str, emoji: str, hint: str, items: list[dict], key: str) -> None:
        if not items:
            return
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"*{emoji} {label} — {len(items)}*  _({hint})_",
        }})
        # Batch sessions into sections of up to 5 to stay well under
        # Slack's 3000-char-per-text-object cap.
        chunk = 5
        for i in range(0, len(items), chunk):
            piece = "\n\n".join(_section_text_for(s, key) for s in items[i:i + chunk])
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": piece[:2900],
            }})

    _add_bucket("ALIVE",      "🟢", "last event ≤ 5 min",          snap["alive"],      "alive")
    _add_bucket("JUST ENDED", "✅", "closed within window",         snap["just_ended"], "just_ended")
    _add_bucket("IDLE",       "💤", f"5–{window_min} min, no close", snap["idle"],      "idle")

    if not (n_alive or n_ended or n_idle):
        blocks.append({"type": "section", "text": {
            "type": "mrkdwn",
            "text": "(아무 활동 없음 — 마지막 30분 동안 어떤 claude 세션도 움직이지 않음)",
        }})

    # Slack hard-limits a message to 50 blocks. If we ever exceed it
    # (huge fleet), drop the tail and add a note rather than 400-ing.
    if len(blocks) > 50:
        blocks = blocks[:49]
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": f"_…truncated to 50 blocks (Slack limit). See `heartbeat.py` stdout for the full list._",
        }]})
    return blocks


# ---------------------------------------------------------------------------

def post_to_slack(text: str, blocks: list[dict] | None = None) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        # Try to pick it up from the user's .env (gateway-level config).
        env_path = Path.home() / "AppData" / "Local" / "hermes" / ".env"
        if env_path.is_file():
            for ln in env_path.read_text(encoding="utf-8").splitlines():
                if ln.startswith("SLACK_BOT_TOKEN="):
                    token = ln.split("=", 1)[1].strip()
                    break
    if not token:
        raise SystemExit("SLACK_BOT_TOKEN not set (env or ~/AppData/Local/hermes/.env)")

    # `text` is sent as the notification/preview fallback. When blocks are
    # present Slack renders the blocks in the channel and uses `text` for
    # notifications and screen readers. mrkdwn=False on the fallback so the
    # plain-text bucket layout isn't reinterpreted.
    payload: dict = {
        "channel": SLACK_USER_ID,
        "text": text,
        "mrkdwn": False,
    }
    if blocks:
        payload["blocks"] = blocks
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "ignore")}


def _sync_ledger_before_classify() -> None:
    """Phase 2 PR #6 — reconcile ``hermes_sessions`` from disk before classify.

    The heartbeat reads from the unified ``session_view`` (PR #5); that
    reader trusts ``hermes_sessions`` to be current. The Phase 2 design
    keeps the disk → DB writer in one module (``session_sync``); calling
    ``sync_all`` here is the only thing the heartbeat needs to do to keep
    the ledger fresh on every tick. mtime-keyed skip inside ``sync_all``
    makes this < 1 s on a populated host; failures are non-fatal — a stale
    classify is still better than no heartbeat post.
    """
    try:
        import time as _t
        from worker_control.db import connect as _connect
        from worker_control.session_sync import sync_all as _sync_all
        t0 = _t.monotonic()
        with _connect(DB_PATH) as conn:
            res = _sync_all(conn, quiet=True)
        ms = int((_t.monotonic() - t0) * 1000)
        print(
            f"[session-sync] sync_all: jsonl={res.synced_jsonl} "
            f"profile={res.synced_profile} "
            f"skipped_mtime={res.skipped_mtime_unchanged} "
            f"errors={res.errors} ({ms} ms)",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[session-sync] sync_all failed: {e!r}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=30, help="lookback window in minutes (default 30)")
    p.add_argument("--post", action="store_true", help="post to Slack DM (otherwise print only)")
    p.add_argument("--plain", action="store_true",
                   help="post the legacy plain-text layout instead of the Block Kit card")
    p.add_argument("--json", action="store_true", help="dump raw classification as JSON")
    args = p.parse_args()

    # Phase 2 PR #6 — reconcile ledger from disk before classify reads it.
    _sync_ledger_before_classify()

    snap = classify_sessions(args.window)
    if args.json:
        def default(o):
            if isinstance(o, _dt.datetime):
                return o.isoformat()
            if isinstance(o, Path):
                return str(o)
            return str(o)
        print(json.dumps(snap, default=default, ensure_ascii=False, indent=2))
        return

    text = render_text(snap)
    print(text)
    if args.post:
        # Noise reduction: if all three buckets are empty, this heartbeat tick
        # has nothing to report — skip the Slack post entirely. The stdout
        # still prints the "(아무 활동 없음 …)" line for the cron log so we
        # can verify the job is running even on quiet ticks.
        n_total = len(snap["alive"]) + len(snap["just_ended"]) + len(snap["idle"])
        if n_total == 0:
            print("--- no activity in window — skipping slack post", file=sys.stderr)
            return
        print("---", file=sys.stderr)
        blocks = None if args.plain else render_blocks(snap)
        result = post_to_slack(text, blocks=blocks)
        print(f"slack: {result.get('ok')} ({result.get('error', 'sent')})", file=sys.stderr)


if __name__ == "__main__":
    main()
