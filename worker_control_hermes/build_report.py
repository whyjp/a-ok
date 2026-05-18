#!/usr/bin/env python
"""
build_report.py — render a worker-task summary as a self-contained HTML page
and (optionally) ishare-deploy it.

Usage (from a worker turn):
    python scripts/build_report.py \
        --session <session-uuid-or-name> \
        --status done \
        --title "토큰 만료 버그 픽스" \
        --body-md /tmp/report.md \
        --deploy \
        --json

Outputs (always to stdout when --json):
    {"local_path": "...", "url": "https://...", "bundle_id": "...", "session": {...}}

Design choices:
- HTML is one file, no external CSS/JS, dark theme tuned for at-a-glance review.
- Body input is markdown (--body-md path or --body inline); rendered with the
  stdlib only — no extra deps — using a small markdown-subset renderer
  (headings, lists, code blocks, inline code, bold, italic, links).
- Session metadata (project, uuid, brief, timestamps) is pulled from the
  worker projects.db, so summaries are auditable.
- ishare deploy is invoked via the bundled CLI; per the user's preference
  every report is a NEW bundle so URLs stay independent.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR   = Path(__file__).resolve().parent
PROFILE_HOME = SCRIPT_DIR.parent                                  # ../  → worker profile root
DB_PATH      = Path(os.environ.get("WORKER_PROJECTS_DB", r"D:/work-github/.worker-control/worker-control.sqlite3"))
REPORTS_DIR  = PROFILE_HOME / "reports"
def _resolve_ishare_cli() -> Path:
    """Locate the ishare CLI shipped with the ishare-hosting Claude plugin.

    Layout: ~/.claude/plugins/cache/isharepub/ishare-hosting/<version>/cli/index.js
    We pick the highest semver-looking version directory.
    """
    override = os.environ.get("ISHARE_CLI", "")
    if override:
        p = Path(override)
        if p.exists():
            return p
    base = Path.home() / ".claude" / "plugins" / "cache" / "isharepub" / "ishare-hosting"
    if not base.is_dir():
        return Path("__missing__")
    candidates = sorted(
        (d for d in base.iterdir() if d.is_dir() and (d / "cli" / "index.js").exists()),
        key=lambda d: tuple(int(x) for x in re.findall(r"\d+", d.name) or [0]),
        reverse=True,
    )
    if not candidates:
        return Path("__missing__")
    return candidates[0] / "cli" / "index.js"


ISHARE_CLI = _resolve_ishare_cli()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_session(key: str):
    """Resolve ``key`` to a (SessionView, project_row) tuple.

    PR #5 (Phase 2): session lookup now goes through
    ``worker_control.session_view.get_session`` so all consumers share
    the same fuzzy-match rules and the report stays in sync with the
    dashboard / heartbeat view. Project info is still loaded directly
    from ``hermes_projects_v`` because the report needs ``git_repo`` —
    a column the session-side view doesn't carry.
    """
    from worker_control.session_view import get_session
    try:
        sess = get_session(key)
    except LookupError as exc:
        raise SystemExit(str(exc))
    if sess is None:
        raise SystemExit(f"Session not found: {key}")
    with _connect() as conn:
        proj = conn.execute(
            "SELECT * FROM hermes_projects_v WHERE id=?",
            (sess.project_id,),
        ).fetchone()
    return sess, proj


# ---------------------------------------------------------------------------
# tiny markdown → HTML (stdlib only)
# ---------------------------------------------------------------------------

def _inline(text: str) -> str:
    # Escape first, then re-introduce supported inline tokens.
    s = html.escape(text)
    # links: [label](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
               s)
    # inline code
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # bold / italic (order matters)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*_])\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"(?<![_])_([^_]+)_(?!_)", r"<em>\1</em>", s)
    return s


def _md_to_html(md: str) -> str:
    out: list[str] = []
    in_code = False
    code_buf: list[str] = []
    in_list: str | None = None    # 'ul' | 'ol' | None
    para: list[str] = []

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + _inline(" ".join(para)).replace("\n", " ") + "</p>")
            para = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for raw in md.splitlines():
        if raw.startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                flush_para(); close_list()
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue

        line = raw.rstrip()
        if not line.strip():
            flush_para(); close_list(); continue

        # headings
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); close_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue

        # unordered list
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            flush_para()
            if in_list != "ul":
                close_list(); out.append("<ul>"); in_list = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # ordered list
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            flush_para()
            if in_list != "ol":
                close_list(); out.append("<ol>"); in_list = "ol"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # paragraph
        close_list()
        para.append(line.strip())

    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
    flush_para(); close_list()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_CSS = r"""
:root {
  color-scheme: dark;
  --bg: #0e1117;
  --panel: #161b22;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #7d8590;
  --accent: #58a6ff;
  --ok: #3fb950;
  --warn: #d29922;
  --err: #f85149;
  --code-bg: #1f242c;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px 80px;
  background: var(--bg); color: var(--text);
  font: 15px/1.6 -apple-system, "Segoe UI", "Pretendard", "Apple SD Gothic Neo", sans-serif;
}
main { max-width: 880px; margin: 0 auto; }
header.report-head {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 22px 26px; margin-bottom: 24px;
}
header.report-head h1 { margin: 0 0 6px; font-size: 22px; }
header.report-head .meta { color: var(--muted); font-size: 13px; }
header.report-head .meta span { margin-right: 14px; }
.status-badge { display: inline-block; padding: 3px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600; letter-spacing: .02em; }
.status-done    { background: rgba(63,185,80,.18);  color: var(--ok); }
.status-partial { background: rgba(210,153,34,.18); color: var(--warn); }
.status-failed  { background: rgba(248,81,73,.18); color: var(--err); }
.status-active  { background: rgba(88,166,255,.18); color: var(--accent); }
section.body { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 24px 28px; }
section.body h2 { margin-top: 28px; font-size: 18px; border-bottom: 1px solid var(--border);
  padding-bottom: 6px; }
section.body h3 { margin-top: 22px; font-size: 16px; }
section.body p { margin: 12px 0; }
section.body a { color: var(--accent); }
section.body code { background: var(--code-bg); padding: 1px 6px;
  border-radius: 4px; font-size: 13px; font-family: "JetBrains Mono", Menlo, Consolas, monospace; }
section.body pre { background: var(--code-bg); padding: 14px 16px;
  border-radius: 8px; overflow: auto; font-size: 13px; line-height: 1.5; }
section.body pre code { background: transparent; padding: 0; border-radius: 0; }
section.body ul, section.body ol { padding-left: 24px; }
section.body li { margin: 4px 0; }
.footer { margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center; }
.kv { display: grid; grid-template-columns: 110px 1fr; gap: 6px 14px; margin-top: 12px; }
.kv .k { color: var(--muted); font-size: 12px; }
.kv .v { font-family: "JetBrains Mono", Menlo, Consolas, monospace; font-size: 12px;
  word-break: break-all; }
"""

_HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_esc}</title>
<style>{css}</style>
</head>
<body>
<main>
  <header class="report-head">
    <h1>{title_esc}</h1>
    <div class="meta">
      <span class="status-badge status-{status_class}">{status_label}</span>
      <span>Project: <code>{project_esc}</code></span>
      <span>{generated_at}</span>
    </div>
    <div class="kv">
      <div class="k">session</div><div class="v">{session_name_esc} <span style="color:var(--muted)">({session_uuid})</span></div>
      <div class="k">project</div><div class="v">{project_path_esc}</div>
      {repo_row}{brief_row}
    </div>
  </header>
  <section class="body">
{body_html}
  </section>
  <p class="footer">Generated by Hermes worker · {generated_at}</p>
</main>
</body>
</html>
"""


_STATUS_LABELS = {
    "done":     ("완료",      "done"),
    "partial":  ("부분 완료", "partial"),
    "failed":   ("실패",      "failed"),
    "active":   ("진행 중",   "active"),
}


def _render_html(*, title: str, status: str, body_md: str,
                 sess, proj: sqlite3.Row) -> str:
    label, css_class = _STATUS_LABELS.get(status, (status, "active"))
    repo_row = (
        f'<div class="k">repo</div><div class="v">{html.escape(proj["git_repo"])}</div>'
        if proj["git_repo"] else ""
    )
    brief_row = (
        f'<div class="k">brief</div><div class="v">{html.escape(sess.brief or "")}</div>'
        if sess.brief else ""
    )
    return _HTML_TEMPLATE.format(
        title_esc=html.escape(title),
        css=_CSS,
        status_class=css_class,
        status_label=label,
        project_esc=html.escape(proj["display_name"] or Path(proj["folder_path"]).name),
        generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        session_name_esc=html.escape(sess.name),
        session_uuid=sess.uuid,
        project_path_esc=html.escape(proj["folder_path"]),
        repo_row=repo_row,
        brief_row=brief_row,
        body_html=_md_to_html(body_md),
    )


# ---------------------------------------------------------------------------
# ishare deploy
# ---------------------------------------------------------------------------

def _deploy_to_ishare(folder: Path, bundle_name: str) -> dict:
    """Create a brand-new ishare bundle from `folder` and return its info.

    CLI signature (v0.2.0):
        ishare create <bundle_name> <paths...> [--site-url --version --entry-file --private]
    Emits a multi-line JSON envelope:
        {"ok": true, "command":"create", "result":{"bundle_id":..., "version":"1.0.0", ...}}
    No `url` in create's response — use `ishare status <id>` to read `hosting_url`.
    """
    if not ISHARE_CLI.exists():
        raise SystemExit(
            f"ishare CLI not found at {ISHARE_CLI}. "
            "Set ISHARE_CLI env var or install the ishare-hosting plugin "
            "(`claude plugin install ishare-hosting@isharepub`)."
        )
    if not os.environ.get("ISHARE_TOKEN"):
        raise SystemExit(
            "ISHARE_TOKEN env var is not set. Set it as a user OS env var "
            "(see ishare-hosting skill docs)."
        )

    def _ishare_json(args: list[str]) -> dict:
        r = subprocess.run(["node", str(ISHARE_CLI), *args],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            raise SystemExit(f"ishare {args[0]} failed (exit={r.returncode}):\n{err or out}")
        try:
            return json.loads(out)
        except Exception:
            # Fall back: scan for the JSON envelope.
            start = out.find("{")
            end   = out.rfind("}")
            if start >= 0 and end > start:
                return json.loads(out[start:end + 1])
            raise SystemExit(f"could not parse ishare CLI output:\nSTDOUT:\n{out}\nSTDERR:\n{err}")

    created = _ishare_json(["create", bundle_name, str(folder)])
    if not created.get("ok"):
        raise SystemExit(f"ishare create returned ok=false: {created}")
    result = created.get("result", {})
    bundle_id = result.get("bundle_id")
    if bundle_id is None:
        raise SystemExit(f"ishare create did not return bundle_id: {created}")

    # Fetch hosting_url via status (create doesn't include it).
    status = _ishare_json(["status", str(bundle_id)])
    sresult = status.get("result", {})
    url = sresult.get("hosting_url")
    return {
        "bundle_id": bundle_id,
        "bundle_name": bundle_name,
        "version": result.get("version"),
        "version_id": result.get("version_id"),
        "url": url,
        "entry_file": sresult.get("entry_file"),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True, help="session uuid/name/id (from projects.py)")
    p.add_argument("--title",   required=True, help="page title / report title")
    p.add_argument("--status",  default="done",
                   choices=["done", "partial", "failed", "active"])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--body-md",   type=Path, help="path to markdown body file (also bundled into the report as report.md)")
    g.add_argument("--body",      help="markdown body inline (also bundled into the report as report.md)")
    p.add_argument("--html",      type=Path,
                   help="path to a pre-rendered index.html (e.g. produced by the "
                        "huashu-design skill). When set, the fallback markdown→HTML "
                        "renderer is skipped and this file ships as index.html. "
                        "The markdown source still ships alongside as report.md, "
                        "so consumers can choose between the rich HTML view and the "
                        "lossless markdown source.")
    p.add_argument("--out-dir", type=Path, default=None,
                   help=f"output dir (default: {REPORTS_DIR}/<session-slug>). "
                        "When --html is set and points elsewhere, the file is copied here. "
                        "Use this when the huashu-design output folder has additional "
                        "assets (images, CSS, fonts) you want bundled into ishare.")
    p.add_argument("--deploy", action="store_true",
                   help="also deploy to ishare and return URL")
    p.add_argument("--bundle-name", default=None,
                   help="ishare bundle name (default: worker-<session-slug>-<ts>)")
    p.add_argument("--json", action="store_true", help="emit a JSON result blob")
    args = p.parse_args()

    sess, proj = _fetch_session(args.session)

    # Markdown body is always required (either --body or --body-md) and is always
    # shipped alongside the HTML as report.md. This is a deliberate design choice:
    # converting markdown to HTML is lossy (code highlighting, custom block syntax,
    # tables in some renderers), so the markdown source is the canonical lossless
    # form. The HTML is the rich, visual presentation. Consumers pick whichever
    # representation fits their use case.
    body_md = args.body if args.body is not None else args.body_md.read_text(encoding="utf-8")

    out_dir = args.out_dir or (REPORTS_DIR / f"{sess.name}-{sess.uuid[:8]}")
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "index.html"
    md_path   = out_dir / "report.md"

    # 1. Always ship the markdown source (lossless).
    md_path.write_text(body_md, encoding="utf-8")

    # 2. HTML: either pre-rendered (huashu-design preferred) or fallback renderer.
    if args.html is not None:
        src = args.html.resolve()
        if not src.is_file():
            raise SystemExit(f"--html target not found: {src}")
        if src.resolve() != html_path.resolve():
            html_path.write_bytes(src.read_bytes())
        renderer_used = "pre-rendered (huashu-design or equivalent)"
    else:
        html_path.write_text(
            _render_html(title=args.title, status=args.status, body_md=body_md,
                         sess=sess, proj=proj),
            encoding="utf-8",
        )
        renderer_used = "fallback-markdown"

    result: dict = {
        "local_path": str(html_path),
        "markdown_path": str(md_path),
        "renderer": renderer_used,
        "session": {"uuid": sess.uuid, "name": sess.name, "id": sess.id},
        "project": {"name": proj["display_name"], "path": proj["folder_path"]},
    }

    if args.deploy:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bname = args.bundle_name or f"worker-{sess.name}-{ts}"[:80]
        # Per spec: iShare ships ONLY the HTML view. The markdown source is
        # for Slack (message body / attachment) and local audit, not for
        # publishing. Stage a temp dir with just index.html (+ any sibling
        # assets that are NOT report.md) to keep the bundle clean.
        import shutil, tempfile
        with tempfile.TemporaryDirectory(prefix="ishare-stage-") as stage:
            stage_p = Path(stage)
            for item in out_dir.iterdir():
                if item.name == "report.md":
                    continue                       # markdown stays out of ishare
                target = stage_p / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            info = _deploy_to_ishare(stage_p, bname)
        result["bundle"] = info
        result["url"] = info.get("url")

        # Persist the URL back into the session notes for audit.
        if result["url"]:
            now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            with _connect() as conn:
                existing_row = conn.execute(
                    "SELECT notes FROM hermes_sessions WHERE id=?", (sess.id,)
                ).fetchone()
                existing = (existing_row["notes"] if existing_row else "") or ""
                new = (existing + ("\n" if existing else "")
                       + f"[{now}] report deployed: {result['url']}")
                conn.execute("UPDATE hermes_sessions SET notes=?, last_used_at=? WHERE id=?",
                             (new, now, sess.id))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"HTML     : {result['local_path']}")
        print(f"Markdown : {result['markdown_path']}")
        print(f"Renderer : {renderer_used}")
        if result.get("url"):
            print(f"URL      : {result['url']}")


if __name__ == "__main__":
    main()
