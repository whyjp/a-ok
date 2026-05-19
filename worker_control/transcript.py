"""Transcript loaders: hermes-profile JSON + native-claude JSONL.

Both transcript formats are normalised into a single shape that the FE
modal renders without per-format branching:

    {
      "path": str, "size": int, "mtime": iso-string,
      "format": "hermes-profile" | "claude-jsonl",
      "turns": [
        { "role", "ts", "text", "tool_calls", "thinking", "raw" }, ...
      ],
    }

Hermes profile sessions live at
``~/AppData/Local/hermes/profiles/<name>/sessions/session_*.json`` with a
top-level ``messages`` array. Native Claude transcripts are line-delimited
JSON under ``~/.claude/projects/<encoded>/*.jsonl``.

A 50 MiB hard cap protects the BFF / browser from accidentally serving
giant files into a single modal payload.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_BYTES = 50 * 1024 * 1024


def load_transcript(path: Path) -> dict[str, Any]:
    st = path.stat()
    if st.st_size > MAX_BYTES:
        raise ValueError(f"too_large: {st.st_size} > {MAX_BYTES}")
    mtime = datetime.fromtimestamp(st.st_mtime).isoformat()
    if path.suffix.lower() == ".jsonl":
        fmt, turns = _load_claude_jsonl(path)
    else:
        fmt, turns = _load_hermes_profile(path)
    return {
        "path": str(path),
        "size": st.st_size,
        "mtime": mtime,
        "format": fmt,
        "turns": turns,
    }


# ---------------------------------------------------------------------------
# hermes-profile

def _load_hermes_profile(path: Path) -> tuple[str, list[dict[str, Any]]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    msgs = d.get("messages") or []
    turns: list[dict[str, Any]] = []
    for m in msgs:
        role = m.get("role") or "system"
        text = m.get("content") or ""
        if not isinstance(text, str):
            # Defensive — some hermes variants may stuff arrays into content
            text = json.dumps(text, ensure_ascii=False)
        tool_calls = None
        if m.get("tool_calls"):
            tool_calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                # arguments may be a JSON string OR an object — pretty-print
                # either way so the FE can drop it into a <pre> tag.
                if isinstance(args, str):
                    try:
                        args = json.dumps(
                            json.loads(args), ensure_ascii=False, indent=2,
                        )
                    except Exception:
                        pass
                elif args is not None:
                    args = json.dumps(args, ensure_ascii=False, indent=2)
                tool_calls.append({
                    "name": fn.get("name") or tc.get("type") or "?",
                    "args": args or "",
                })
        turns.append({
            "role": role,
            "ts": None,
            "text": text,
            "tool_calls": tool_calls,
            "thinking": m.get("reasoning"),
            "raw": m,
        })
    return "hermes-profile", turns


# ---------------------------------------------------------------------------
# claude-jsonl

def _load_claude_jsonl(path: Path) -> tuple[str, list[dict[str, Any]]]:
    turns: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if t == "user":
                m = d.get("message") or {}
                c = m.get("content")
                text = c if isinstance(c, str) else _join_blocks(c)
                turns.append({
                    "role": "user", "ts": ts, "text": text,
                    "tool_calls": None, "thinking": None, "raw": d,
                })
            elif t == "assistant":
                m = d.get("message") or {}
                c = m.get("content")
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_calls: list[dict[str, str]] = []
                if isinstance(c, list):
                    for blk in c:
                        if not isinstance(blk, dict):
                            continue
                        bt = blk.get("type")
                        if bt == "text":
                            text_parts.append(blk.get("text", ""))
                        elif bt == "thinking":
                            thinking_parts.append(blk.get("thinking", ""))
                        elif bt == "tool_use":
                            args = blk.get("input")
                            args_str = (
                                json.dumps(args, ensure_ascii=False, indent=2)
                                if args is not None else ""
                            )
                            tool_calls.append({
                                "name": blk.get("name") or "?",
                                "args": args_str,
                            })
                elif isinstance(c, str):
                    text_parts.append(c)
                turns.append({
                    "role": "assistant",
                    "ts": ts,
                    "text": "\n".join(text_parts),
                    "tool_calls": tool_calls or None,
                    "thinking": "\n".join(thinking_parts) or None,
                    "raw": d,
                })
            elif t in ("system", "attachment", "queue-operation",
                       "last-prompt", "file-history-snapshot"):
                label_map = {
                    "system": "system",
                    "attachment": "attachment",
                    "queue-operation": "queue",
                    "last-prompt": "last-prompt",
                    "file-history-snapshot": "snapshot",
                }
                label = label_map.get(t, t)
                text = d.get("content") or d.get("lastPrompt") or ""
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
                turns.append({
                    "role": "system",
                    "ts": ts,
                    "text": f"[{label}] {text[:2000]}",
                    "tool_calls": None,
                    "thinking": None,
                    "raw": d,
                })
            else:
                turns.append({
                    "role": "system",
                    "ts": ts,
                    "text": f"[{t}] (skipped)",
                    "tool_calls": None,
                    "thinking": None,
                    "raw": d,
                })
    return "claude-jsonl", turns


def _join_blocks(c: Any) -> str:
    if isinstance(c, list):
        out: list[str] = []
        for blk in c:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    out.append(blk.get("text", ""))
                elif blk.get("type") == "tool_result":
                    r = blk.get("content")
                    out.append(
                        r if isinstance(r, str)
                        else json.dumps(r, ensure_ascii=False)
                    )
        return "\n".join(out)
    if c is None:
        return ""
    return str(c)
