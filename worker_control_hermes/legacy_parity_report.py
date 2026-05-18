"""Build a single self-contained HTML page identical in shape to the
legacy ``sites/1143`` claude-sessions report.

The page reads its `DATA[]` from the canonical SQLite DB (hermes_agent_sessions
+ child tables). Rendering is vanilla JS — no frameworks, no external
resources — so the artifact is portable (drag-and-drop into a browser,
iShare, or email attachment).

Usage:
    workerctl-hermes-report-legacy [--out PATH] [--db PATH] [--ingest]

The same `load_session_payload(conn)` function is exposed for the live
BFF (`worker_control.dashboard`) to share the SQL.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


# Shared SQL: same row shape for static export & live BFF.
_SESSIONS_SQL = """
SELECT
  s.hermes_session_id        AS session_id,
  COALESCE(s.kind, 'claude') AS kind,
  s.profile_name,
  s.profile_path,
  s.transcript_path          AS jsonl_path,
  s.transcript_size,
  s.transcript_mtime,
  s.started_at,
  s.ended_at,
  s.model                    AS worker_model,
  s.turn_count,
  s.first_message,
  s.last_message,
  s.cwd,
  s.total_cost_usd,
  s.synced_at,
  s.git_branch,
  s.claude_version           AS version,
  s.msg_user,
  s.msg_assistant,
  s.msg_tool,
  s.ai_title,
  s.summary,
  s.first_user_text,
  s.last_user_text,
  s.last_assistant_text,
  s.size_bytes,
  s.spawn_slug,
  s.spawn_reason,
  s.is_spawned,
  s.effective_status,
  COALESCE(hs.name, '')      AS worker_name,
  COALESCE(hs.brief, '')     AS worker_brief,
  COALESCE(hs.notes, '')     AS worker_notes,
  COALESCE(hs.permission_mode, '') AS worker_perm,
  COALESCE(hs.status, '')    AS worker_status,
  p.path                     AS project_folder,
  p.name                     AS project_name,
  COALESCE(json_extract(p.metadata, '$.hermes.project_type'), '') AS project_type,
  COALESCE(json_extract(p.metadata, '$.hermes.git_repo'), p.remote_url) AS git_repo
FROM hermes_agent_sessions s
LEFT JOIN hermes_sessions hs ON LOWER(hs.uuid) = LOWER(s.hermes_session_id)
LEFT JOIN projects p ON p.id = hs.project_id
ORDER BY COALESCE(s.transcript_mtime, s.started_at, '') DESC
"""


_SESSIONS_SQL_STANDALONE = """
SELECT
  s.hermes_session_id        AS session_id,
  COALESCE(s.kind, 'claude') AS kind,
  s.profile_name,
  s.profile_path,
  s.transcript_path          AS jsonl_path,
  s.transcript_size,
  s.transcript_mtime,
  s.started_at,
  s.ended_at,
  s.model                    AS worker_model,
  s.turn_count,
  s.first_message,
  s.last_message,
  s.cwd,
  s.total_cost_usd,
  s.synced_at,
  s.git_branch,
  s.claude_version           AS version,
  s.msg_user,
  s.msg_assistant,
  s.msg_tool,
  s.ai_title,
  s.summary,
  s.first_user_text,
  s.last_user_text,
  s.last_assistant_text,
  s.size_bytes,
  s.spawn_slug,
  s.spawn_reason,
  s.is_spawned,
  s.effective_status,
  '' AS worker_name, '' AS worker_brief, '' AS worker_notes,
  '' AS worker_perm, '' AS worker_status,
  NULL AS project_folder, NULL AS project_name,
  '' AS project_type, NULL AS git_repo
FROM hermes_agent_sessions s
ORDER BY COALESCE(s.transcript_mtime, s.started_at, '') DESC
"""


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def load_session_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return one dict per session matching legacy DATA[] shape.

    When `hermes_sessions` / `projects` tables are absent (test envs that
    only initialise the parity tables), falls back to a join-less query so
    the parity report can still render.
    """
    conn.row_factory = sqlite3.Row
    sql = _SESSIONS_SQL if (
        _has_table(conn, "hermes_sessions") and _has_table(conn, "projects")
    ) else _SESSIONS_SQL_STANDALONE

    pr_by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM session_pr_links"):
        pr_by_sid.setdefault(r["session_uuid"], []).append(
            {"url": r["url"], "num": r["num"], "repo": r["repo"], "kind": r["kind"]}
        )
    files_by_sid: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT * FROM session_files_touched ORDER BY last_seen_at DESC"
    ):
        files_by_sid.setdefault(r["session_uuid"], []).append(r["path"])
    tools_by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM session_tools_recent ORDER BY ord ASC"):
        tools_by_sid.setdefault(r["session_uuid"], []).append(
            {"name": r["name"], "snippet": r["snippet"], "ts": r["ts"]}
        )
    recaps_by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM session_recaps ORDER BY ord ASC"):
        recaps_by_sid.setdefault(r["session_uuid"], []).append(
            {"content": r["content"], "ts": r["ts"]}
        )
    pending_by_sid: dict[str, list[str]] = {}
    for r in conn.execute("SELECT * FROM session_pending_queue ORDER BY ord ASC"):
        pending_by_sid.setdefault(r["session_uuid"], []).append(r["text"])

    runs_by_uuid: dict[str, list[dict[str, Any]]] = {}
    try:
        for r in conn.execute(
            """
            SELECT s.uuid AS uuid, r.run_index, r.mode, r.status, r.started_at,
                   r.ended_at, r.name
            FROM hermes_runs r JOIN hermes_sessions s ON s.id = r.session_id
            ORDER BY r.started_at ASC
            """
        ):
            runs_by_uuid.setdefault(r["uuid"].lower(), []).append(
                {"run_index": r["run_index"], "mode": r["mode"],
                 "status": r["status"], "name": r["name"]}
            )
    except sqlite3.OperationalError:
        pass

    out: list[dict[str, Any]] = []
    for r in conn.execute(sql):
        sid = r["session_id"]
        origin = "spawned" if (r["is_spawned"] or 0) else (
            "hermes" if r["kind"] == "hermes" else "native"
        )
        d = {
            "session_id":          sid,
            "kind":                r["kind"],
            "origin":              origin,
            "cwd":                 r["cwd"],
            "project_dir":         (Path(r["jsonl_path"]).parent.name
                                    if r["jsonl_path"] else None),
            "project_folder":      r["project_folder"],
            "project_name":        r["project_name"],
            "project_type":        r["project_type"],
            "git_repo":            r["git_repo"],
            "git_branch":          r["git_branch"],
            "version":             r["version"],
            "first_ts":            r["started_at"],
            "last_ts":             r["transcript_mtime"] or r["started_at"],
            "msg_user":            r["msg_user"] or 0,
            "msg_assistant":       r["msg_assistant"] or 0,
            "msg_tool":            r["msg_tool"] or 0,
            "ai_title":            r["ai_title"],
            "summary":             r["summary"],
            "first_user_text":     r["first_user_text"],
            "last_user_text":      r["last_user_text"],
            "last_assistant_text": r["last_assistant_text"],
            "pr_links":            pr_by_sid.get(sid, []),
            "files_touched":       files_by_sid.get(sid, []),
            "tools_recent":        tools_by_sid.get(sid, []),
            "recap_native":        recaps_by_sid.get(sid, []),
            "pending_queue":       pending_by_sid.get(sid, []),
            "jsonl_path":          r["jsonl_path"],
            "size_bytes":          r["size_bytes"],
            "spawn_slug":          r["spawn_slug"] or "",
            "spawn_reason":        r["spawn_reason"] or "",
            "is_spawned":          bool(r["is_spawned"]),
            "effective_status":    r["effective_status"],
            "worker_name":         r["worker_name"] or "",
            "worker_brief":        r["worker_brief"] or "",
            "worker_notes":        r["worker_notes"] or "",
            "worker_model":        r["worker_model"] or "",
            "worker_perm":         r["worker_perm"] or "",
            "worker_status":       r["worker_status"] or "",
            "profile_name":        r["profile_name"],
            "profile_path":        r["profile_path"],
            "runs":                runs_by_uuid.get(sid.lower(), []),
        }
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# HTML (vanilla JS, single file) — structurally mirrors the legacy report
# ---------------------------------------------------------------------------

_HEAD_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>Claude Sessions — Native vs Hermes-Spawned</title>
<style>
  :root {
    --bg:#0b0f17; --panel:#121826; --panel2:#1a2233; --border:#26314a;
    --text:#e4ebf5; --muted:#8a98b3; --accent:#7aa2ff; --accent2:#a78bfa;
    --good:#34d399; --warn:#fbbf24; --bad:#f87171;
    --native:#60a5fa; --spawned:#a78bfa;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:13px}
  header{padding:20px 24px;border-bottom:1px solid var(--border);
    display:flex;align-items:baseline;gap:24px;flex-wrap:wrap;background:var(--panel)}
  header h1{margin:0;font-size:20px;font-weight:600}
  header .meta{color:var(--muted);font-size:12px}
  .stats{display:flex;gap:14px;flex-wrap:wrap}
  .stat{background:var(--panel2);padding:8px 14px;border-radius:8px;
    border:1px solid var(--border);font-size:12px}
  .stat b{color:var(--accent);font-size:14px;margin-right:6px}
  .controls{padding:14px 24px;display:flex;gap:10px;flex-wrap:wrap;
    align-items:center;background:var(--panel);border-bottom:1px solid var(--border)}
  .controls input,.controls select{
    background:var(--panel2);color:var(--text);border:1px solid var(--border);
    padding:7px 10px;border-radius:6px;font-size:13px;font-family:inherit}
  .controls input[type="search"]{min-width:280px}
  .controls label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px}
  .controls .pill{padding:6px 12px;border-radius:14px;cursor:pointer;
    border:1px solid var(--border);background:var(--panel2);font-size:12px;user-select:none}
  .controls .pill.active{background:var(--accent);color:#0b0f17;border-color:var(--accent);font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  thead th{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--border);
    text-align:left;padding:10px 12px;font-weight:600;color:var(--muted);
    cursor:pointer;user-select:none;white-space:nowrap}
  thead th:hover{color:var(--text)}
  thead th.sorted::after{content:" ▾";color:var(--accent)}
  thead th.sorted.asc::after{content:" ▴"}
  tbody tr{border-bottom:1px solid var(--border)}
  tbody tr:hover{background:var(--panel2)}
  tbody td{padding:10px 12px;vertical-align:top}
  td.session-id{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
    font-size:11px;color:var(--muted)}
  td.project{max-width:320px;word-break:break-all}
  td.project .name{color:var(--text);font-weight:500}
  td.project .path{color:var(--muted);font-size:11px;font-family:ui-monospace,monospace}
  td.brief{max-width:380px;color:var(--muted);font-size:11.5px;line-height:1.45}
  td.brief .first{color:var(--text)}
  .recap{margin-top:10px;padding:10px 12px 10px 14px;
    background:linear-gradient(90deg,rgba(167,139,250,.12),rgba(167,139,250,.03));
    border-left:3px solid var(--accent2);border-radius:0 8px 8px 0;position:relative}
  .recap-header{display:flex;align-items:center;gap:8px;margin-bottom:8px;
    padding-bottom:6px;border-bottom:1px dashed rgba(167,139,250,.2)}
  .recap-header .glyph{font-size:14px;color:var(--accent2);font-weight:700}
  .recap-header .title{color:#ddd6fe;font-size:11px;font-weight:700;
    text-transform:uppercase;letter-spacing:1px}
  .recap-header .oneline{flex:1;color:var(--text);font-size:11.5px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
  .recap .native-recap{color:#e4ebf5;font-size:12px;line-height:1.55;
    padding:6px 0 10px;border-bottom:1px dashed rgba(167,139,250,.2);margin-bottom:8px;
    white-space:pre-wrap;word-break:break-word}
  .recap .native-recap .more{color:var(--muted);font-size:10.5px;display:block;margin-top:2px}
  .recap-grid{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;align-items:start}
  .recap .lbl{justify-self:start;padding:2px 8px;border-radius:10px;
    background:rgba(167,139,250,.25);color:#ddd6fe;font-size:9.5px;font-weight:700;
    text-transform:uppercase;letter-spacing:.5px;line-height:1.4;white-space:nowrap}
  .recap .lbl.warn{background:rgba(251,191,36,.25);color:#fde68a}
  .recap .lbl.pr{background:rgba(122,162,255,.25);color:#bfdbfe}
  .recap .val{color:#e4ebf5;font-size:11.5px;line-height:1.5;word-break:break-word;min-width:0}
  .recap .val.muted{color:var(--muted)}
  .recap .val.pending{color:#fbbf24}
  .recap .val.files{font-family:ui-monospace,monospace;font-size:10.5px;color:#c7d2fe}
  .recap .val a{color:#93c5fd;text-decoration:none;font-family:ui-monospace,monospace;font-size:11px}
  .recap .val a:hover{text-decoration:underline}
  .recap .val code{background:rgba(255,255,255,.06);padding:1px 6px;border-radius:4px;
    font-size:10px;color:#a78bfa;margin-right:3px}
  .recap .val .more{color:var(--muted);font-size:10.5px;margin-left:4px}
  .tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10.5px;
    font-weight:600;letter-spacing:.3px;text-transform:uppercase}
  .tag.native{background:rgba(96,165,250,.18);color:#93c5fd;border:1px solid rgba(96,165,250,.35)}
  .tag.spawned{background:rgba(167,139,250,.18);color:#c4b5fd;border:1px solid rgba(167,139,250,.35)}
  .tag.hermes{background:rgba(52,211,153,.18);color:#6ee7b7;border:1px solid rgba(52,211,153,.35)}
  .tag.status-active{background:rgba(52,211,153,.18);color:#6ee7b7}
  .tag.status-inactive{background:rgba(148,163,184,.18);color:#cbd5e1}
  .tag.status-done{background:rgba(96,165,250,.18);color:#93c5fd}
  .tag.status-failed,.tag.status-abandoned{background:rgba(248,113,113,.18);color:#fca5a5}
  .runs{margin-top:6px;font-size:11px;color:var(--muted)}
  .runs code{background:var(--panel2);padding:1px 5px;border-radius:3px;font-size:10.5px}
  .ts{white-space:nowrap;font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
  .resume{font-family:ui-monospace,monospace;font-size:10.5px;color:var(--accent2);
    background:var(--panel2);padding:3px 7px;border-radius:4px;border:1px solid var(--border);
    cursor:pointer;display:inline-block;margin-top:4px;user-select:all}
  .empty{padding:40px;text-align:center;color:var(--muted)}
  .hidden{display:none !important}
  details summary{cursor:pointer;color:var(--accent);font-size:11px}
  details pre{background:var(--panel2);padding:8px 10px;border-radius:5px;
    border:1px solid var(--border);font-size:10.5px;white-space:pre-wrap;
    word-break:break-all;color:var(--muted);max-height:200px;overflow:auto;margin:6px 0 0 0}
  footer{padding:14px 24px;color:var(--muted);font-size:11px;text-align:center;
    border-top:1px solid var(--border)}
</style>
</head>
<body>
<header>
  <h1>Claude Sessions Report</h1>
  <div class="meta">생성: __GENERATED__ · 호스트 ~/.claude/projects + hermes worker DB</div>
  <div class="stats">
    <div class="stat"><b id="stat-total">0</b>총 세션</div>
    <div class="stat"><b id="stat-native">0</b>네이티브</div>
    <div class="stat"><b id="stat-spawned">0</b>Hermes-스폰</div>
    <div class="stat"><b id="stat-projects">0</b>프로젝트</div>
    <div class="stat"><b id="stat-pending">0</b>큐 대기</div>
    <div class="stat"><b id="stat-pr">0</b>PR/MR</div>
    <div class="stat"><b id="stat-native-recap">0</b>claude recap</div>
    <div class="stat"><b id="stat-visible">0</b>표시 중</div>
  </div>
</header>

<div class="controls">
  <input type="search" id="q" placeholder="검색: cwd / brief / session-id / project / branch…">
  <span class="pill active" data-origin="all">전체</span>
  <span class="pill" data-origin="native">네이티브만</span>
  <span class="pill" data-origin="spawned">스폰만</span>
  <span class="pill" id="pill-recap" data-recap="0">📌 큐 대기/PR 있음</span>
  <label>프로젝트:
    <select id="filter-project"><option value="">(전체)</option></select>
  </label>
  <label>상태:
    <select id="filter-status"><option value="">(전체)</option></select>
  </label>
  <label>최근:
    <select id="filter-time">
      <option value="">(전체)</option>
      <option value="1">24시간</option>
      <option value="7">7일</option>
      <option value="30">30일</option>
    </select>
  </label>
  <button id="reset" class="pill">초기화</button>
</div>

<table id="tbl">
<thead>
<tr>
  <th data-sort="origin">유형</th>
  <th data-sort="project">프로젝트 / cwd</th>
  <th data-sort="worker_name">세션 이름 / brief</th>
  <th data-sort="last_ts" class="sorted">최근 활동</th>
  <th data-sort="first_ts">시작</th>
  <th data-sort="duration">길이</th>
  <th data-sort="msgs">메시지</th>
  <th data-sort="status">상태</th>
  <th data-sort="version">정보</th>
</tr>
</thead>
<tbody id="rows"></tbody>
</table>
<div class="empty hidden" id="empty">필터 조건과 일치하는 세션이 없습니다.</div>
<footer>worker-control legacy-parity report · canonical DB single source of truth</footer>
<script>
"""

_JS_TAIL = r"""
function fmtTs(s) {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d)) return s;
  const pad = n => String(n).padStart(2,"0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDuration(a, b) {
  if (!a || !b) return "";
  const ms = new Date(b) - new Date(a);
  if (isNaN(ms) || ms < 0) return "";
  const s = Math.floor(ms/1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s/60);
  if (m < 60) return m + "m";
  const h = Math.floor(m/60);
  const rm = m % 60;
  return h + "h" + (rm ? " " + rm + "m" : "");
}
function durationMs(a, b) {
  if (!a || !b) return 0;
  const ms = new Date(b) - new Date(a);
  return isNaN(ms) ? 0 : Math.max(0, ms);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

const projectsSet = new Set(), statusSet = new Set();
for (const s of DATA) {
  const p = s.project_name || s.project_folder || s.cwd || s.project_dir || "(unknown)";
  projectsSet.add(p);
  if (s.effective_status) statusSet.add(s.effective_status);
}
const pSel = document.getElementById("filter-project");
[...projectsSet].sort().forEach(p => {
  const o = document.createElement("option"); o.value = p; o.textContent = p; pSel.appendChild(o);
});
const sSel = document.getElementById("filter-status");
[...statusSet].sort().forEach(s => {
  const o = document.createElement("option"); o.value = s; o.textContent = s; sSel.appendChild(o);
});

let sortKey = "last_ts", sortAsc = false;
let originFilter = "all";
let recapFilter = false;

function hasRecapSignal(s) {
  return (s.pending_queue && s.pending_queue.length) || (s.pr_links && s.pr_links.length);
}

function getCell(s, key) {
  switch(key) {
    case "origin": return s.origin;
    case "project": return (s.project_name || s.project_folder || s.cwd || "").toLowerCase();
    case "worker_name": return (s.worker_name || s.first_user_text || "").toLowerCase();
    case "last_ts": return s.last_ts || "";
    case "first_ts": return s.first_ts || "";
    case "duration": return durationMs(s.first_ts, s.last_ts);
    case "msgs": return (s.msg_user||0) + (s.msg_assistant||0);
    case "status": return s.effective_status || "";
    case "version": return s.version || "";
  }
  return "";
}

function render() {
  const q = document.getElementById("q").value.trim().toLowerCase();
  const pf = pSel.value;
  const sf = sSel.value;
  const tf = document.getElementById("filter-time").value;
  const now = Date.now();
  const cutoff = tf ? now - parseInt(tf,10)*86400000 : 0;

  let rows = DATA.filter(s => {
    if (originFilter !== "all" && s.origin !== originFilter) return false;
    if (recapFilter && !hasRecapSignal(s)) return false;
    if (pf && (s.project_name || s.project_folder || s.cwd) !== pf) return false;
    if (sf && s.effective_status !== sf) return false;
    if (cutoff && new Date(s.last_ts).getTime() < cutoff) return false;
    if (q) {
      const hay = [s.session_id, s.cwd, s.project_folder, s.project_name, s.worker_name,
        s.worker_brief, s.first_user_text, s.summary, s.git_branch, s.git_repo,
        s.project_type, s.version, s.ai_title, s.last_user_text, s.last_assistant_text,
        ...(s.pending_queue||[]), ...(s.files_touched||[]),
        ...((s.pr_links||[]).map(p=>p.url))].filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  rows.sort((a,b) => {
    const va = getCell(a, sortKey), vb = getCell(b, sortKey);
    if (va < vb) return sortAsc ? -1 : 1;
    if (va > vb) return sortAsc ? 1 : -1;
    return 0;
  });

  const tbody = document.getElementById("rows");
  tbody.innerHTML = rows.map(s => {
    const projLabel = s.project_name || s.project_folder || s.cwd || s.project_dir;
    const projPath = s.project_folder || s.cwd || "";
    const briefRaw = s.worker_brief || s.summary || s.first_user_text || "";
    const briefText = briefRaw;
    const runsHtml = (s.runs && s.runs.length)
      ? `<div class="runs">runs: ${s.runs.map(r =>
          `<code>r${r.run_index}·${r.mode}·${r.status}</code>`).join(" ")}</div>` : "";
    const resumeCmd = `claude --resume ${s.session_id}`;
    const pending = s.pending_queue || [];
    const prs = s.pr_links || [];
    const files = s.files_touched || [];
    const tools = s.tools_recent || [];
    const lastUser = s.last_user_text || "";
    const lastAsst = s.last_assistant_text || "";
    const nativeRecaps = s.recap_native || [];
    let recapHtml = "";
    const cells = [];
    function push(lblText, lblCls, valHtml, valCls) {
      const lc = lblCls ? ` ${lblCls}` : "";
      const vc = valCls ? ` ${valCls}` : "";
      cells.push(`<span class="lbl${lc}">${esc(lblText)}</span><div class="val${vc}">${valHtml}</div>`);
    }
    if (s.ai_title) push("title", "", esc(s.ai_title));
    if (pending.length) {
      const head = esc(pending[0]);
      const more = pending.length>1 ? `<span class="more">+${pending.length-1}</span>` : "";
      push("queued", "warn", head + more, "pending");
    }
    if (prs.length) {
      const prHtml = prs.slice(-3).map(p => {
        const label = p.num ? `#${esc(p.num)}` : esc((p.url||"").split("/").pop());
        return `<a href="${esc(p.url)}" target="_blank" rel="noopener">${label}</a>`;
      }).join(" · ");
      push("pr/mr", "pr", prHtml);
    }
    if (lastUser && lastUser !== s.first_user_text) push("last ask", "", esc(lastUser));
    if (lastAsst) push("last say", "", esc(lastAsst), "muted");
    if (files.length) {
      const fHtml = files.slice(0,5).map(f => esc(f.split(/[\\/]/).pop())).join(" · ");
      push("files", "", fHtml, "files");
    }
    if (tools.length) {
      const tHtml = tools.slice(-5).map(t => `<code>${esc(t.name)}</code>`).join("");
      push("tools", "", tHtml);
    }
    if (cells.length || nativeRecaps.length) {
      let oneliner = "";
      let nativeBlock = "";
      if (nativeRecaps.length) {
        const latest = nativeRecaps[nativeRecaps.length-1].content || "";
        const firstLine = latest.split(/\r?\n/)[0] || latest;
        oneliner = firstLine.slice(0,140) + (firstLine.length>140?"…":"");
        const olderCount = nativeRecaps.length - 1;
        nativeBlock = `<div class="native-recap">${esc(latest)}` +
          (olderCount > 0 ? `<span class="more">+${olderCount} earlier recap${olderCount>1?"s":""}</span>` : "") +
          `</div>`;
      } else if (pending.length) {
        oneliner = `🟡 queued: ${pending[0].slice(0,70)}${pending[0].length>70?"…":""}`;
      } else if (prs.length) {
        const p = prs[prs.length-1];
        oneliner = `🔀 ${p.num?"#"+p.num:""} open${prs.length>1?` (+${prs.length-1})`:""}`;
      } else if (lastUser && lastUser !== s.first_user_text) {
        oneliner = `↳ ${lastUser.slice(0,80)}${lastUser.length>80?"…":""}`;
      } else if (s.ai_title) {
        oneliner = s.ai_title;
      } else if (lastAsst) {
        oneliner = `✓ ${lastAsst.slice(0,80)}${lastAsst.length>80?"…":""}`;
      }
      const sourceTag = nativeRecaps.length
        ? `<span class="lbl pr" style="margin-left:auto">claude</span>`
        : `<span class="lbl" style="margin-left:auto;background:rgba(148,163,184,.18);color:#cbd5e1">derived</span>`;
      recapHtml = `<div class="recap">
        <div class="recap-header">
          <span class="glyph">※</span>
          <span class="title">recap</span>
          <span class="oneline">${esc(oneliner)}</span>
          ${sourceTag}
        </div>
        ${nativeBlock}
        ${cells.length ? `<div class="recap-grid">${cells.join("")}</div>` : ""}
      </div>`;
    }
    return `<tr>
      <td><span class="tag ${s.origin}">${s.origin === "spawned" ? "스폰" : (s.origin === "hermes" ? "Hermes" : "native")}</span></td>
      <td class="project">
        <div class="name">${esc(projLabel)}</div>
        ${projPath && projPath !== projLabel ? `<div class="path">${esc(projPath)}</div>` : ""}
        ${s.git_branch ? `<div class="path">⎇ ${esc(s.git_branch)}</div>` : ""}
        ${s.project_type ? `<div class="path">type: ${esc(s.project_type)}</div>` : ""}
      </td>
      <td class="brief">
        ${s.worker_name ? `<div class="first">${esc(s.worker_name)}</div>` : ""}
        ${briefText ? `<div>${esc(briefText.slice(0,240))}${briefText.length>240?"…":""}</div>` : ""}
        ${recapHtml}
        ${runsHtml}
        <div class="resume" title="클릭 후 복사" onclick="navigator.clipboard.writeText(this.textContent)">${esc(resumeCmd)}</div>
      </td>
      <td class="ts">${esc(fmtTs(s.last_ts))}</td>
      <td class="ts">${esc(fmtTs(s.first_ts))}</td>
      <td class="ts">${esc(fmtDuration(s.first_ts, s.last_ts))}</td>
      <td>${(s.msg_user||0)+(s.msg_assistant||0)} <span style="color:var(--muted);font-size:10.5px">(u${s.msg_user||0}/a${s.msg_assistant||0}/t${s.msg_tool||0})</span></td>
      <td>${s.effective_status ? `<span class="tag status-${esc(s.effective_status)}">${esc(s.effective_status)}</span>` : ""}</td>
      <td class="ts">
        ${s.version ? "v"+esc(s.version) : ""}
        ${s.worker_model ? `<br>${esc(s.worker_model)}` : ""}
        <details><summary>session-id</summary><pre>${esc(s.session_id)}\n${esc(s.jsonl_path)}</pre></details>
      </td>
    </tr>`;
  }).join("");

  document.getElementById("empty").classList.toggle("hidden", rows.length > 0);
  document.getElementById("stat-visible").textContent = rows.length;

  document.querySelectorAll("thead th").forEach(th => {
    th.classList.remove("sorted","asc");
    if (th.dataset.sort === sortKey) {
      th.classList.add("sorted");
      if (sortAsc) th.classList.add("asc");
    }
  });
}

document.getElementById("stat-total").textContent = DATA.length;
document.getElementById("stat-native").textContent = DATA.filter(s=>s.origin==="native").length;
document.getElementById("stat-spawned").textContent = DATA.filter(s=>s.origin==="spawned").length;
document.getElementById("stat-projects").textContent = projectsSet.size;
document.getElementById("stat-pending").textContent = DATA.filter(s => s.pending_queue && s.pending_queue.length).length;
document.getElementById("stat-pr").textContent = DATA.filter(s => s.pr_links && s.pr_links.length).length;
document.getElementById("stat-native-recap").textContent = DATA.filter(s => s.recap_native && s.recap_native.length).length;

document.getElementById("q").addEventListener("input", render);
pSel.addEventListener("change", render);
sSel.addEventListener("change", render);
document.getElementById("filter-time").addEventListener("change", render);
document.querySelectorAll(".pill[data-origin]").forEach(el => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".pill[data-origin]").forEach(e=>e.classList.remove("active"));
    el.classList.add("active");
    originFilter = el.dataset.origin;
    render();
  });
});
document.getElementById("pill-recap").addEventListener("click", (e) => {
  recapFilter = !recapFilter;
  e.target.classList.toggle("active", recapFilter);
  render();
});
document.getElementById("reset").addEventListener("click", () => {
  document.getElementById("q").value = "";
  pSel.value = ""; sSel.value = "";
  document.getElementById("filter-time").value = "";
  originFilter = "all";
  recapFilter = false;
  document.getElementById("pill-recap").classList.remove("active");
  document.querySelectorAll(".pill[data-origin]").forEach(e=>e.classList.remove("active"));
  document.querySelector('.pill[data-origin="all"]').classList.add("active");
  sortKey = "last_ts"; sortAsc = false;
  render();
});
document.querySelectorAll("thead th").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (k === sortKey) sortAsc = !sortAsc;
    else { sortKey = k; sortAsc = false; }
    render();
  });
});

render();
</script>
</body>
</html>
"""


def render_html(payload: list[dict[str, Any]], *, generated_at: str | None = None) -> str:
    gen = generated_at or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = _HEAD_HTML.replace("__GENERATED__", html.escape(gen))
    # </script> & <!-- safe escape
    raw = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    return head + f"const DATA = {raw};\n" + _JS_TAIL


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.environ.get(
        "WORKER_PROJECTS_DB", r"D:/work-github/.worker-control/worker-control.sqlite3"))
    p.add_argument("--out", type=Path,
                   default=Path("D:/work-github/a-ok/artifacts/report.html"))
    p.add_argument("--ingest", action="store_true",
                   help="run a parity-ingest pass first (default: skip — trust heartbeat ticks)")
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    if args.ingest:
        from .legacy_parity_ingest import ingest_all
        stats = ingest_all(conn)
        print(f"ingest: {stats}", file=sys.stderr)

    payload = load_session_payload(conn)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(payload), encoding="utf-8")
    print(f"wrote {args.out}  ({len(payload)} sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
