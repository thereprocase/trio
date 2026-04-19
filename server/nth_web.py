"""
Web dashboard for a trio channel — same functional surface as nth_dashboard
(chat tail, roster, @-autocomplete input) but rendered as a browser UI and
served over a local HTTP port.

The default binding is 127.0.0.1 (loopback only). Pass --tailnet to bind all
interfaces so peers on your tailnet can reach it over Tailscale. Tailscale's
ACL is the access control layer — this server has no auth of its own, so
never bind it to a public interface directly.

Usage:
    python3 nth_web.py MYCHAN                # loopback only, port 8765
    python3 nth_web.py MYCHAN --tailnet      # bind all interfaces
    python3 nth_web.py MYCHAN --port 9000
    python3 nth_web.py MYCHAN --host 100.x.y.z  # bind a specific interface

Architecture:
    - One EventHub polls the local SQLite DB every 0.5s and fans out
      events (new messages, roster snapshots) to every connected SSE client.
    - Each HTTP request runs on its own thread via ThreadingHTTPServer.
      SSE requests hold the thread for the life of the connection.
    - POSTs to /api/send open a short-lived sqlite3 connection and commit
      the message directly. No cross-thread Connection sharing.

Requires only the Python standard library.
"""
from __future__ import annotations

import argparse
import getpass
import html
import json
import os
import queue
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


# ───────── Config ─────────
DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"
DEFAULT_PORT = 8765
DB_POLL_INTERVAL = 0.5
HISTORY_LIMIT = 200          # messages sent to a client on /api/history
SSE_HEARTBEAT_SEC = 20       # keep-alive comment interval
STALE_SECONDS = 300          # fresh heartbeat threshold
DEAD_SECONDS = 900           # no heartbeat this long → dead
SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")
OPERATOR_MEMBER_ID_PREFIX = "_op_"
OPERATOR_NAME_FALLBACK = "Operator"


# ───────── Helpers ─────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def operator_identity() -> Tuple[str, str]:
    host = socket.gethostname().lower()
    host = re.sub(r'[^a-z0-9_-]', '-', host)[:20] or "host"
    try:
        name = getpass.getuser() or OPERATOR_NAME_FALLBACK
    except Exception:
        name = OPERATOR_NAME_FALLBACK
    return f"{OPERATOR_MEMBER_ID_PREFIX}{host}", name


def get_tailscale_ip() -> Optional[str]:
    """Best-effort: return the tailnet IPv4 address of this host, or None
    if Tailscale isn't installed/running. Used only for informational
    output — does NOT gate binding."""
    for cmd in ("tailscale", "tailscale.exe"):
        try:
            out = subprocess.check_output(
                [cmd, "ip", "-4"], timeout=2, stderr=subprocess.DEVNULL
            )
            ip = out.decode().strip().splitlines()[0]
            if ip and ip[0].isdigit():
                return ip
        except Exception:
            continue
    return None


def parse_mentions_json(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def member_status(last_seen_iso: Optional[str], status_text: str) -> str:
    """Match the dashboard's status classification."""
    if not last_seen_iso:
        return "dead"
    try:
        ts = datetime.fromisoformat(last_seen_iso).timestamp()
    except (ValueError, TypeError):
        return "dead"
    age = datetime.now(timezone.utc).timestamp() - ts
    if age > DEAD_SECONDS:
        return "dead"
    if age > STALE_SECONDS:
        return "stale"
    if status_text and any(kw in status_text.lower() for kw in SLEEPING_KEYWORDS):
        return "idle"
    return "active"


def ensure_operator_row(db: sqlite3.Connection, channel: str) -> Tuple[str, str]:
    op_id, op_name = operator_identity()
    now = now_iso()
    db.execute(
        "INSERT OR IGNORE INTO members "
        "(id, channel, name, summary, skills, last_seen, last_read, joined_at, "
        " active, status_text, status_changed_at, messenger_heartbeat, watchdog_heartbeat) "
        "VALUES (?, ?, ?, 'human operator (via nth_web)', '', ?, 0, ?, 1, "
        " 'operator — watching via web', ?, '', '')",
        (op_id, channel, op_name, now, now, now),
    )
    return op_id, op_name


# ───────── EventHub: polls DB, fans out SSE events ─────────
class EventHub:
    """Single background thread watches the DB and pushes JSON events to any
    subscribed SSE client. Each client owns a queue.Queue of pending payloads."""

    def __init__(self, db_path: Path, channel: str):
        self.db_path = db_path
        self.channel = channel
        self.last_msg_id = 0
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_roster_snapshot: Optional[str] = None

    # ── subscription ──
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.append(q)
        # Immediately send a current snapshot so the client renders right away.
        self._prime_subscriber(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _prime_subscriber(self, q: queue.Queue) -> None:
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=2000")
            # Snapshot roster
            members = self._fetch_roster(db)
            q.put_nowait(json.dumps({"type": "roster", "members": members}))
            # Last-N history
            rows = db.execute(
                "SELECT id, member_id, member_name, content, mentions, created_at "
                "FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?",
                (self.channel, HISTORY_LIMIT),
            ).fetchall()
            for r in reversed(rows):
                q.put_nowait(json.dumps({
                    "type": "message",
                    "id": r["id"],
                    "member_id": r["member_id"],
                    "member_name": r["member_name"] or r["member_id"],
                    "content": r["content"] or "",
                    "mentions": parse_mentions_json(r["mentions"]),
                    "created_at": r["created_at"],
                }))
            db.close()
        except (sqlite3.Error, queue.Full):
            pass

    # ── broadcast ──
    def _broadcast(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event)
        with self._lock:
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for d in dead:
                self._subs.remove(d)

    # ── DB poll ──
    def _fetch_roster(self, db: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = db.execute(
            "SELECT id, name, status_text, last_seen, last_read, "
            "messenger_heartbeat, watchdog_heartbeat "
            "FROM members WHERE channel = ? ORDER BY joined_at",
            (self.channel,),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "status_text": r["status_text"] or "",
                "last_seen": r["last_seen"],
                "last_read": r["last_read"] or 0,
                "status": member_status(r["last_seen"], r["status_text"] or ""),
            })
        return out

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=2000")
            # Prime last_msg_id so we don't re-fire history on startup —
            # primed subscribers already got the history through _prime_subscriber.
            try:
                row = db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM messages WHERE channel = ?",
                    (self.channel,),
                ).fetchone()
                self.last_msg_id = int(row[0] or 0)
            except sqlite3.Error:
                self.last_msg_id = 0
        except sqlite3.Error as e:
            sys.stderr.write(f"[nth_web] DB open failed: {e}\n")
            return

        while not self._stop.is_set():
            try:
                # New messages
                rows = db.execute(
                    "SELECT id, member_id, member_name, content, mentions, created_at "
                    "FROM messages WHERE channel = ? AND id > ? ORDER BY id",
                    (self.channel, self.last_msg_id),
                ).fetchall()
                for r in rows:
                    self._broadcast({
                        "type": "message",
                        "id": r["id"],
                        "member_id": r["member_id"],
                        "member_name": r["member_name"] or r["member_id"],
                        "content": r["content"] or "",
                        "mentions": parse_mentions_json(r["mentions"]),
                        "created_at": r["created_at"],
                    })
                    self.last_msg_id = r["id"]

                # Roster snapshot (diffed against previous to avoid pointless fan-out)
                members = self._fetch_roster(db)
                snapshot = json.dumps(members, sort_keys=True)
                if snapshot != self._last_roster_snapshot:
                    self._last_roster_snapshot = snapshot
                    self._broadcast({"type": "roster", "members": members})

            except sqlite3.Error as e:
                sys.stderr.write(f"[nth_web] poll error: {e}\n")

            self._stop.wait(DB_POLL_INTERVAL)

        try:
            db.close()
        except Exception:
            pass


# ───────── HTTP handler ─────────
class NthWebHandler(BaseHTTPRequestHandler):
    # Populated in main()
    hub: Optional[EventHub] = None
    channel: str = ""
    db_path: Path = DB_PATH

    # Suppress default noisy logging
    def log_message(self, fmt: str, *args) -> None:
        # Comment out if you want request logs.
        pass

    # ── routing ──
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._serve_html(INDEX_HTML)
        elif path == "/api/meta":
            self._json({
                "channel": self.channel,
                "operator": {
                    "id": operator_identity()[0],
                    "name": operator_identity()[1],
                },
                "server_host": socket.gethostname(),
            })
        elif path == "/api/events":
            self._serve_sse()
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/send":
            self._handle_send()
        else:
            self._error(404, "not found")

    # ── handlers ──
    def _serve_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj: Any, status: int = 200) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status=status)

    def _serve_sse(self) -> None:
        assert self.hub is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.hub.subscribe()
        try:
            last_heartbeat = time.monotonic()
            while True:
                try:
                    payload = q.get(timeout=1.0)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_heartbeat >= SSE_HEARTBEAT_SEC:
                        # SSE "comment" line — keeps the connection alive
                        # through intermediate proxies without polluting data.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def _handle_send(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 16384:
            self._error(400, "missing or oversized body")
            return
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return

        content = (body.get("content") or "").strip()
        if not content:
            self._error(400, "empty content")
            return
        mentions = body.get("mentions") or []
        if not isinstance(mentions, list) or not all(isinstance(m, str) for m in mentions):
            self._error(400, "mentions must be a list of strings")
            return
        if len(content) > 4000:
            self._error(400, "content too long (max 4000 chars)")
            return

        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            op_id, op_name = ensure_operator_row(db, self.channel)
            now = now_iso()
            cursor = db.execute(
                "INSERT INTO messages "
                "(channel, member_id, member_name, content, created_at, mentions) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.channel, op_id, op_name, content, now,
                 json.dumps(mentions) if mentions else ""),
            )
            msg_id = cursor.lastrowid
            db.execute(
                "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                (now, self.channel, op_id),
            )
            db.commit()
            db.close()
        except sqlite3.Error as e:
            self._error(500, f"db error: {e}")
            return

        self._json({"ok": True, "id": msg_id})


# ───────── HTML / JS / CSS (served as /) ─────────
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nth_web — trio dashboard</title>
<style>
  :root {
    --bg: #0b0f14; --bg2: #121821; --panel: #161d27; --border: #273040;
    --fg: #d8dde6; --dim: #7a8596; --dimmer: #4a5262;
    --accent: #3ba0e6; --accent2: #59cb79; --warn: #e3c34c; --err: #e56a4a;
    --mention: #e3c34c;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%;
    background: var(--bg); color: var(--fg);
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", ui-monospace, Menlo, monospace;
    font-size: 13px; line-height: 1.45;
  }
  #app { display: grid; grid-template-columns: 1fr 280px; grid-template-rows: 42px 1fr auto;
         height: 100vh; }
  header { grid-column: 1 / 3; background: var(--bg2); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; padding: 0 14px; gap: 16px;
           font-weight: 600; }
  header .title { color: var(--accent); }
  header .meta { color: var(--dim); font-weight: 400; }
  header .conn {
    margin-left: auto; font-size: 11px; padding: 3px 8px; border-radius: 3px;
    background: var(--panel); border: 1px solid var(--border);
  }
  header .conn.ok { color: var(--accent2); }
  header .conn.bad { color: var(--err); }

  #chat { grid-row: 2 / 3; grid-column: 1 / 2; overflow-y: auto;
          padding: 12px 16px; scroll-behavior: smooth; }
  .msg { margin-bottom: 10px; word-wrap: break-word; }
  .msg .head { font-size: 11px; color: var(--dim); margin-bottom: 2px; }
  .msg .author { font-weight: 600; }
  .msg .mentions { color: var(--mention); margin-left: 6px; }
  .msg .body { white-space: pre-wrap; }
  .msg.system .body { color: var(--dim); font-style: italic; }
  .msg.mine .author { color: var(--accent2); }
  .msg.targeted { background: #1a2030; padding: 6px 10px; border-left: 3px solid var(--mention);
                  margin-left: -10px; border-radius: 0 3px 3px 0; }

  #roster { grid-row: 2 / 3; grid-column: 2 / 3;
            background: var(--panel); border-left: 1px solid var(--border);
            overflow-y: auto; padding: 10px 12px; }
  #roster h2 { font-size: 11px; text-transform: uppercase; color: var(--dim);
               letter-spacing: 0.08em; margin: 4px 0 8px; }
  .member { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; }
  .member:hover { background: var(--bg2); }
  .member .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .member .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .member .id { color: var(--dimmer); font-size: 10px; }
  .dot.active { background: var(--accent2); }
  .dot.idle { background: var(--dimmer); }
  .dot.stale { background: var(--warn); }
  .dot.dead { background: var(--err); }
  .member .stext { font-size: 10px; color: var(--dim); margin-top: 1px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  #composer { grid-row: 3 / 4; grid-column: 1 / 3;
              background: var(--bg2); border-top: 1px solid var(--border);
              padding: 8px 14px; display: flex; flex-direction: column; gap: 4px; }
  #preview { font-size: 11px; color: var(--dim); min-height: 14px; }
  #preview .tgt { color: var(--mention); font-weight: 600; }
  #input-row { display: flex; gap: 8px; align-items: flex-end; position: relative; }
  #input { flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
           padding: 8px 10px; border-radius: 4px; font-family: inherit; font-size: 13px;
           resize: none; min-height: 36px; max-height: 160px; }
  #input:focus { outline: none; border-color: var(--accent); }
  #send-btn { background: var(--accent); color: var(--bg); border: none;
              padding: 0 18px; height: 36px; border-radius: 4px; cursor: pointer;
              font-weight: 600; font-family: inherit; font-size: 13px; }
  #send-btn:hover { background: #50b0f0; }
  #send-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #hint { font-size: 10px; color: var(--dimmer); margin-top: 2px; }
  #hint kbd { background: var(--panel); border: 1px solid var(--border); padding: 1px 5px;
              border-radius: 2px; font-size: 10px; color: var(--dim); }

  #completions { position: absolute; left: 0; bottom: 42px;
                 background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
                 max-height: 200px; overflow-y: auto; display: none; z-index: 10;
                 min-width: 280px; box-shadow: 0 -4px 12px rgba(0,0,0,0.4); }
  #completions.active { display: block; }
  .completion { padding: 6px 10px; cursor: pointer; display: flex; gap: 8px; align-items: center; }
  .completion:hover, .completion.selected { background: var(--bg); }
  .completion .cname { color: var(--fg); }
  .completion .cid { color: var(--dimmer); font-size: 10px; }
  .completion .cdot { width: 6px; height: 6px; border-radius: 50%; }
</style>
</head>
<body>
<div id="app">
  <header>
    <span class="title" id="h-channel">trio#…</span>
    <span class="meta" id="h-meta">connecting…</span>
    <span class="conn bad" id="h-conn">● disconnected</span>
  </header>

  <div id="chat"></div>

  <aside id="roster">
    <h2 id="r-heading">Members</h2>
    <div id="r-list"></div>
  </aside>

  <div id="composer">
    <div id="preview">(broadcast — all connected members receive this)</div>
    <div id="input-row">
      <div id="completions"></div>
      <textarea id="input" rows="1" placeholder="Type a message. @ to mention. Enter to send. Shift+Enter for newline."></textarea>
      <button id="send-btn">Send</button>
    </div>
    <div id="hint">
      <kbd>Enter</kbd> send
      <kbd>Shift+Enter</kbd> newline
      <kbd>@</kbd> mention
      <kbd>Tab</kbd> accept completion
      <kbd>Esc</kbd> dismiss
      <kbd>↑/↓</kbd> navigate completions
    </div>
  </div>
</div>

<script>
(() => {
  const chat = document.getElementById('chat');
  const rosterEl = document.getElementById('r-list');
  const rosterHeading = document.getElementById('r-heading');
  const hChannel = document.getElementById('h-channel');
  const hMeta = document.getElementById('h-meta');
  const hConn = document.getElementById('h-conn');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send-btn');
  const preview = document.getElementById('preview');
  const compEl = document.getElementById('completions');

  const state = {
    channel: '',
    operator: { id: '', name: '' },
    server_host: '',
    members: new Map(),            // id → member
    seenMsgIds: new Set(),
    completion: { visible: false, index: 0, items: [], atPos: -1 },
  };

  // ── Palette (mirrors the terminal dashboard) ──
  const PALETTE = ['#62d7ef','#d070d7','#7ede7e','#e5d35e',
                   '#8eb9ff','#ff8470','#9ef0f0','#f79fea'];
  function colorFor(id) {
    // Deterministic assignment via hash.
    let h = 0;
    for (const c of id) h = (h * 31 + c.charCodeAt(0)) | 0;
    return PALETTE[Math.abs(h) % PALETTE.length];
  }

  // ── Rendering ──
  function formatTime(iso) {
    if (!iso) return '--:--';
    try {
      const d = new Date(iso);
      return d.toTimeString().slice(0, 8);
    } catch (e) { return '--:--'; }
  }

  const SYSTEM_PREFIXES = ['[claimed ', '[done ', '[cancelled ', '[released ',
                           '[retracted ', '[joined ', '[left ', '[ended ',
                           '[locked ', '[unlocked ', '[status ', '[pinned '];

  function appendMessage(m) {
    if (state.seenMsgIds.has(m.id)) return;
    state.seenMsgIds.add(m.id);

    const isMine = m.member_id === state.operator.id;
    const isSystem = SYSTEM_PREFIXES.some(p => m.content.startsWith(p));
    const mentionsOperator = (m.mentions || []).includes(state.operator.id);

    const div = document.createElement('div');
    div.className = 'msg' + (isMine ? ' mine' : '') + (isSystem ? ' system' : '')
                  + (mentionsOperator ? ' targeted' : '');

    const head = document.createElement('div');
    head.className = 'head';
    const time = document.createElement('span');
    time.textContent = formatTime(m.created_at) + '  ';
    head.appendChild(time);
    if (!isSystem) {
      const author = document.createElement('span');
      author.className = 'author';
      author.textContent = m.member_name;
      author.style.color = colorFor(m.member_id);
      head.appendChild(author);
      if (m.mentions && m.mentions.length) {
        const mtag = document.createElement('span');
        mtag.className = 'mentions';
        const names = m.mentions.map(id => {
          const mem = state.members.get(id);
          return '@' + (mem ? mem.name : id);
        });
        mtag.textContent = ' → ' + names.join(', ');
        head.appendChild(mtag);
      }
    }
    div.appendChild(head);

    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = m.content;
    div.appendChild(body);

    // Only auto-scroll if we were near the bottom already.
    const nearBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    chat.appendChild(div);
    if (nearBottom) chat.scrollTop = chat.scrollHeight;
  }

  function renderRoster(members) {
    // Update map
    state.members.clear();
    for (const m of members) state.members.set(m.id, m);

    // Render
    rosterEl.innerHTML = '';
    const sorted = members.slice().sort((a, b) => {
      // Operator last, then active → idle → stale → dead, then by name.
      const order = { active: 0, idle: 1, stale: 2, dead: 3 };
      if (a.id === state.operator.id) return 1;
      if (b.id === state.operator.id) return -1;
      const oa = order[a.status] ?? 4;
      const ob = order[b.status] ?? 4;
      if (oa !== ob) return oa - ob;
      return a.name.localeCompare(b.name);
    });
    for (const m of sorted) {
      const row = document.createElement('div');
      row.className = 'member';
      row.title = `${m.name} (${m.id})\n${m.status_text || ''}`;
      row.onclick = () => insertMention(m);
      const dot = document.createElement('div');
      dot.className = 'dot ' + m.status;
      row.appendChild(dot);
      const box = document.createElement('div');
      box.style.flex = '1';
      box.style.overflow = 'hidden';
      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = m.name;
      name.style.color = colorFor(m.id);
      box.appendChild(name);
      if (m.status_text) {
        const st = document.createElement('div');
        st.className = 'stext';
        st.textContent = m.status_text;
        box.appendChild(st);
      }
      row.appendChild(box);
      const id = document.createElement('div');
      id.className = 'id';
      id.textContent = m.id.slice(0, 8);
      row.appendChild(id);
      rosterEl.appendChild(row);
    }
    rosterHeading.textContent = `Members (${members.length})`;
  }

  // ── Autocomplete ──
  function currentAtToken() {
    const pos = input.selectionStart;
    const text = input.value.slice(0, pos);
    const atPos = text.lastIndexOf('@');
    if (atPos < 0) return null;
    if (atPos > 0 && !' \t,;([\n'.includes(text[atPos - 1])) return null;
    const frag = text.slice(atPos + 1);
    if (frag && !/^[A-Za-z0-9_\-]*$/.test(frag)) return null;
    return { atPos, fragment: frag };
  }

  function computeCompletions() {
    const tok = currentAtToken();
    if (!tok) return { items: [], atPos: -1 };
    const frag = tok.fragment.toLowerCase();
    const matches = [];
    for (const m of state.members.values()) {
      if (m.id === state.operator.id) continue;
      const nameL = (m.name || '').toLowerCase();
      if (!frag || nameL.includes(frag) || m.id.toLowerCase().startsWith(frag)) {
        matches.push(m);
      }
    }
    matches.sort((a, b) => {
      const an = (a.name || '').toLowerCase(), bn = (b.name || '').toLowerCase();
      const as = an.startsWith(frag) ? 0 : (frag && an.includes(frag) ? 1 : 2);
      const bs = bn.startsWith(frag) ? 0 : (frag && bn.includes(frag) ? 1 : 2);
      if (as !== bs) return as - bs;
      return an.localeCompare(bn);
    });
    return { items: matches.slice(0, 8), atPos: tok.atPos };
  }

  function renderCompletions() {
    const { items, atPos } = state.completion;
    compEl.innerHTML = '';
    if (!state.completion.visible || items.length === 0) {
      compEl.classList.remove('active');
      return;
    }
    items.forEach((m, i) => {
      const row = document.createElement('div');
      row.className = 'completion' + (i === state.completion.index ? ' selected' : '');
      const dot = document.createElement('div');
      dot.className = 'cdot dot ' + m.status;
      row.appendChild(dot);
      const name = document.createElement('span');
      name.className = 'cname';
      name.textContent = '@' + m.name;
      name.style.color = colorFor(m.id);
      row.appendChild(name);
      const id = document.createElement('span');
      id.className = 'cid';
      id.textContent = m.id;
      row.appendChild(id);
      row.onmousedown = (e) => { e.preventDefault(); acceptCompletion(i); };
      compEl.appendChild(row);
    });
    compEl.classList.add('active');
  }

  function refreshCompletions() {
    const { items, atPos } = computeCompletions();
    state.completion.items = items;
    state.completion.atPos = atPos;
    state.completion.visible = items.length > 0 && atPos >= 0;
    if (state.completion.index >= items.length) state.completion.index = 0;
    renderCompletions();
  }

  function acceptCompletion(i) {
    const { items, atPos } = state.completion;
    if (atPos < 0 || !items.length) return;
    const idx = i ?? state.completion.index;
    const m = items[idx];
    if (!m) return;
    const before = input.value.slice(0, atPos);
    const tok = currentAtToken();
    const endPos = input.selectionStart;
    const after = input.value.slice(endPos);
    const repl = '@' + (m.name || m.id) + ' ';
    input.value = before + repl + after;
    const newPos = (before + repl).length;
    input.setSelectionRange(newPos, newPos);
    state.completion.visible = false;
    renderCompletions();
    updatePreview();
  }

  function insertMention(m) {
    // Append @name at cursor position with surrounding spaces.
    const pos = input.selectionStart;
    const before = input.value.slice(0, pos);
    const after = input.value.slice(pos);
    const needSpaceBefore = before && !before.endsWith(' ') && !before.endsWith('\n');
    const tag = (needSpaceBefore ? ' ' : '') + '@' + (m.name || m.id) + ' ';
    input.value = before + tag + after;
    input.focus();
    const p = (before + tag).length;
    input.setSelectionRange(p, p);
    updatePreview();
  }

  // ── Mention resolution (client side, for preview only) ──
  function resolveMentions(text) {
    const out = [];
    const seen = new Set();
    const re = /(?<![A-Za-z0-9_])@([A-Za-z0-9_\-]+)/g;
    let m;
    while ((m = re.exec(text))) {
      const tok = m[1];
      let picked = null;
      for (const mem of state.members.values()) {
        if (mem.id === state.operator.id) continue;
        if (mem.id === tok || (mem.name && mem.name.toLowerCase() === tok.toLowerCase())) {
          picked = mem; break;
        }
      }
      if (!picked) {
        // unique id prefix
        const prefix = [...state.members.values()]
          .filter(mem => mem.id !== state.operator.id
                        && mem.id.toLowerCase().startsWith(tok.toLowerCase()));
        if (prefix.length === 1) picked = prefix[0];
      }
      if (picked && !seen.has(picked.id)) {
        seen.add(picked.id);
        out.push(picked);
      }
    }
    return out;
  }

  function updatePreview() {
    const resolved = resolveMentions(input.value);
    if (!resolved.length) {
      preview.innerHTML = '(broadcast — all connected members receive this)';
    } else {
      const names = resolved.map(m => `<span class="tgt">${m.name}</span>`).join(', ');
      preview.innerHTML = `to: ${names} <span style="color:var(--dimmer)">(other members won't get a targeted notification)</span>`;
    }
  }

  function autoResizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(160, Math.max(36, input.scrollHeight)) + 'px';
  }

  // ── Send ──
  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    const resolved = resolveMentions(input.value);
    const mentionIds = resolved.map(m => m.id);
    sendBtn.disabled = true;
    try {
      const r = await fetch('/api/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text, mentions: mentionIds }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'unknown' }));
        alert('send failed: ' + (err.error || r.status));
        return;
      }
      input.value = '';
      autoResizeInput();
      state.completion.visible = false;
      renderCompletions();
      updatePreview();
    } catch (e) {
      alert('send failed: ' + e.message);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  // ── Key handling ──
  input.addEventListener('keydown', (e) => {
    if (state.completion.visible) {
      if (e.key === 'ArrowDown') {
        state.completion.index = (state.completion.index + 1) % state.completion.items.length;
        renderCompletions(); e.preventDefault(); return;
      }
      if (e.key === 'ArrowUp') {
        state.completion.index = (state.completion.index - 1 + state.completion.items.length)
                                 % state.completion.items.length;
        renderCompletions(); e.preventDefault(); return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && state.completion.items.length > 0)) {
        acceptCompletion(); e.preventDefault(); return;
      }
      if (e.key === 'Escape') {
        state.completion.visible = false; renderCompletions();
        e.preventDefault(); return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  input.addEventListener('input', () => {
    autoResizeInput();
    refreshCompletions();
    updatePreview();
  });
  sendBtn.addEventListener('click', sendMessage);

  // ── SSE connection + auto-reconnect ──
  let es = null;
  let reconnectTimer = null;
  function connect() {
    if (es) try { es.close(); } catch (e) {}
    es = new EventSource('/api/events');
    es.onopen = () => {
      hConn.textContent = '● connected';
      hConn.classList.remove('bad');
      hConn.classList.add('ok');
    };
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === 'message') appendMessage(payload);
        else if (payload.type === 'roster') renderRoster(payload.members);
      } catch (e) {
        console.error('bad event', e);
      }
    };
    es.onerror = () => {
      hConn.textContent = '● reconnecting…';
      hConn.classList.remove('ok');
      hConn.classList.add('bad');
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, 2000);
      }
    };
  }

  // ── Bootstrap ──
  async function boot() {
    try {
      const r = await fetch('/api/meta');
      const meta = await r.json();
      state.channel = meta.channel;
      state.operator = meta.operator;
      state.server_host = meta.server_host;
      hChannel.textContent = 'trio#' + meta.channel;
      hMeta.textContent = `posting as ${meta.operator.name} (${meta.operator.id})  ·  ${meta.server_host}`;
    } catch (e) {
      hMeta.textContent = 'bootstrap failed: ' + e.message;
    }
    connect();
    input.focus();
    updatePreview();
  }

  boot();
})();
</script>
</body>
</html>
"""


# ───────── Entry ─────────
def main() -> int:
    ap = argparse.ArgumentParser(description="Web dashboard for a trio channel.")
    ap.add_argument("channel", help="Channel code to observe.")
    ap.add_argument("--host", default=None,
                    help="Interface to bind. Default 127.0.0.1. "
                         "Use --tailnet to bind 0.0.0.0 instead.")
    ap.add_argument("--tailnet", action="store_true",
                    help="Shortcut for --host 0.0.0.0 (reachable from tailnet peers). "
                         "Only safe if your Tailscale ACL / host firewall gates the port.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"Port to bind (default {DEFAULT_PORT}).")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"Path to nth.db (default {DB_PATH}).")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.stderr.write(f"nth.db not found at {db_path}\n")
        return 1

    host = args.host
    if host is None:
        host = "0.0.0.0" if args.tailnet else "127.0.0.1"

    # Spin up the event hub before serving.
    hub = EventHub(db_path, args.channel)
    hub.start()

    NthWebHandler.hub = hub
    NthWebHandler.channel = args.channel
    NthWebHandler.db_path = db_path

    server = ThreadingHTTPServer((host, args.port), NthWebHandler)
    # Threaded server handles one SSE connection per thread; don't let them
    # keep the process alive on Ctrl-C.
    server.daemon_threads = True

    def shutdown(_sig=None, _frm=None):
        hub.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)

    # Banner
    ts_ip = get_tailscale_ip()
    print("nth_web serving:")
    print(f"  channel:     {args.channel}")
    print(f"  db:          {db_path}")
    print(f"  bound on:    http://{host}:{args.port}/")
    print(f"  localhost:   http://127.0.0.1:{args.port}/")
    if ts_ip and host in ("0.0.0.0",):
        print(f"  tailnet:     http://{ts_ip}:{args.port}/   (visible to tailnet peers)")
    elif ts_ip:
        print(f"  tailnet IP:  {ts_ip}   (pass --tailnet to bind)")
    print("  Ctrl-C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
