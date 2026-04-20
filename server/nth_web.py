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
import http.cookies
import json
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import ANIMAL_EMOJIS, animal_for


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
OP_COOKIE = "nth_op"
OP_COOKIE_MAX_AGE = 60 * 60 * 24 * 30   # 30 days
IDENTITY_SOURCE_TAILSCALE = "tailscale"
IDENTITY_SOURCE_GUEST = "guest"
IDENTITY_SOURCE_PENDING = "pending"
# Agents reading the roster can check the member's summary field:
#   "human — tailnet: knelsonb"   → identity-traceable via Tailscale
#   "human — GUEST (self-declared)" → untrusted self-declared identity
# Neither replaces direct hub-console input.


# ───────── Helpers ─────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(s: str, maxlen: int = 20) -> str:
    s = re.sub(r"[^a-z0-9_-]", "-", (s or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "x"


def _hostname_slug() -> str:
    return _slug(socket.gethostname()) or "host"


@dataclass
class OperatorIdentity:
    member_id: str
    name: str
    source: str             # "tailscale" | "guest" | "pending"
    login: str = ""         # Tailscale login or raw self-declared name
    created_at: float = 0.0

    @property
    def display_name(self) -> str:
        if self.source == IDENTITY_SOURCE_GUEST:
            return f"{self.name} (Guest)"
        return self.name

    @property
    def summary(self) -> str:
        if self.source == IDENTITY_SOURCE_TAILSCALE:
            return f"human — tailnet: {self.login or self.name}"
        if self.source == IDENTITY_SOURCE_GUEST:
            return f"human — GUEST (self-declared)"
        return "human — pending identity"


def tailscale_whois(remote_ip: str) -> Optional[Dict[str, str]]:
    """Ask the local Tailscale daemon who owns a tailnet IP. Returns
    {login, display, node} or None if Tailscale isn't available or the
    caller isn't on the tailnet."""
    if not remote_ip:
        return None
    for cmd in ("tailscale", "tailscale.exe"):
        try:
            out = subprocess.check_output(
                [cmd, "whois", "--json", remote_ip],
                timeout=3, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        try:
            data = json.loads(out.decode("utf-8", errors="replace"))
        except (ValueError, TypeError):
            return None
        up = (data.get("UserProfile") or {})
        login = up.get("LoginName") or ""
        display = up.get("DisplayName") or ""
        node = ((data.get("Node") or {}).get("Name") or "").split(".", 1)[0]
        if not login and not display:
            return None
        return {"login": login, "display": display, "node": node}
    return None


class OperatorRegistry:
    """Per-cookie-token identity store. In-memory — resets on process
    restart. Threadsafe because HTTP handlers share the process via
    ThreadingHTTPServer."""

    def __init__(self) -> None:
        self._by_token: Dict[str, OperatorIdentity] = {}
        self._lock = threading.Lock()

    def new_token(self) -> str:
        return secrets.token_urlsafe(24)

    def get(self, token: str) -> Optional[OperatorIdentity]:
        with self._lock:
            return self._by_token.get(token)

    def put(self, token: str, ident: OperatorIdentity) -> None:
        with self._lock:
            self._by_token[token] = ident

    def resolve_from_tailscale(self, token: str, remote_ip: str) -> Optional[OperatorIdentity]:
        info = tailscale_whois(remote_ip)
        if not info:
            return None
        login = info.get("login") or ""
        # Use the username half of the login (strip @domain).
        login_user = login.split("@", 1)[0] if login else ""
        display = info.get("display") or login_user or "tailnet-user"
        slug = _slug(login_user or display) or "tailnet"
        ident = OperatorIdentity(
            member_id=f"{OPERATOR_MEMBER_ID_PREFIX}t_{_hostname_slug()}_{slug}",
            name=display,
            source=IDENTITY_SOURCE_TAILSCALE,
            login=login,
            created_at=time.time(),
        )
        self.put(token, ident)
        return ident

    def register_guest(self, token: str, raw_name: str) -> OperatorIdentity:
        name = (raw_name or "").strip()[:40] or "Guest"
        slug = _slug(name) or "guest"
        # Disambiguate multiple guests with the same chosen name by
        # suffixing a chunk of the token — keeps their rows distinct.
        ident = OperatorIdentity(
            member_id=f"{OPERATOR_MEMBER_ID_PREFIX}g_{slug}_{token[:6]}",
            name=name,
            source=IDENTITY_SOURCE_GUEST,
            login=name,
            created_at=time.time(),
        )
        self.put(token, ident)
        return ident


OPERATOR_REGISTRY = OperatorRegistry()


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


def ensure_operator_row(db: sqlite3.Connection, channel: str, ident: OperatorIdentity) -> Tuple[str, str]:
    """Insert-or-update this operator's members row. On every send we
    refresh the summary so trust source is fresh if a guest later upgrades
    to a Tailscale identity (or vice versa)."""
    now = now_iso()
    db.execute(
        "INSERT OR IGNORE INTO members "
        "(id, channel, name, summary, skills, last_seen, last_read, joined_at, "
        " active, status_text, status_changed_at, messenger_heartbeat, watchdog_heartbeat) "
        "VALUES (?, ?, ?, ?, '', ?, 0, ?, 1, "
        " 'operator — watching via web', ?, '', '')",
        (ident.member_id, channel, ident.display_name, ident.summary, now, now, now),
    )
    db.execute(
        "UPDATE members SET name = ?, summary = ? "
        "WHERE channel = ? AND id = ?",
        (ident.display_name, ident.summary, channel, ident.member_id),
    )
    return ident.member_id, ident.display_name


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
                "SELECT id, member_id, member_name, content, mentions, refs, created_at "
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
                    "refs": parse_mentions_json(r["refs"] if "refs" in r.keys() else ""),
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
        # v6.2+ session-mode clients write sessions.last_read / last_seen
        # and never touch members.*. Reconcile like nth_monitor.py:171-183
        # so the web console sees real watermark + liveness movement.
        rows = db.execute(
            "SELECT m.id AS id, m.name AS name, m.status_text AS status_text, "
            "m.last_seen AS member_last_seen, m.last_read AS member_last_read, "
            "m.messenger_heartbeat AS messenger_heartbeat, "
            "m.watchdog_heartbeat AS watchdog_heartbeat, "
            "COALESCE(MAX(s.last_read), 0) AS session_last_read, "
            "MAX(s.last_seen) AS session_last_seen "
            "FROM members m "
            "LEFT JOIN sessions s "
            "  ON s.channel = m.channel AND s.member_id = m.id "
            "  AND s.revoked_at IS NULL "
            "WHERE m.channel = ? "
            "GROUP BY m.id, m.channel "
            "ORDER BY m.joined_at",
            (self.channel,),
        ).fetchall()
        out = []
        for r in rows:
            effective_last_read = max(
                r["member_last_read"] or 0,
                r["session_last_read"] or 0,
            )
            # ISO-8601 strings compare lexicographically in UTC
            m_ls = r["member_last_seen"] or ""
            s_ls = r["session_last_seen"] or ""
            effective_last_seen = max(m_ls, s_ls) or None
            out.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "status_text": r["status_text"] or "",
                "last_seen": effective_last_seen,
                "last_read": effective_last_read,
                "status": member_status(effective_last_seen, r["status_text"] or ""),
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
                    "SELECT id, member_id, member_name, content, mentions, refs, created_at "
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
                        "refs": parse_mentions_json(r["refs"] if "refs" in r.keys() else ""),
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

    # ── identity ──
    def _client_ip(self) -> str:
        """Remote IP — trust XFF only when it's set (reverse proxies)."""
        xff = self.headers.get("X-Forwarded-For") or ""
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _get_or_mint_cookie(self) -> Tuple[str, bool]:
        """Return (token, is_new). Parses the incoming cookie; if none, mints one."""
        raw = self.headers.get("Cookie") or ""
        try:
            jar = http.cookies.SimpleCookie(raw)
            tok = jar.get(OP_COOKIE)
            if tok and tok.value:
                return tok.value, False
        except http.cookies.CookieError:
            pass
        return OPERATOR_REGISTRY.new_token(), True

    def _resolve_identity(self) -> Tuple[str, OperatorIdentity, bool]:
        """Resolve (token, identity, is_new_cookie). Tailscale first,
        pending-guest fallback. The pending identity has no name yet;
        the browser must POST /api/identify to set one."""
        token, is_new = self._get_or_mint_cookie()
        ident = OPERATOR_REGISTRY.get(token)
        if ident is not None:
            return token, ident, is_new
        # Try Tailscale whois on the remote address
        ident = OPERATOR_REGISTRY.resolve_from_tailscale(token, self._client_ip())
        if ident is not None:
            return token, ident, is_new
        # Park as pending until the browser supplies a name
        ident = OperatorIdentity(
            member_id=f"{OPERATOR_MEMBER_ID_PREFIX}p_{token[:8]}",
            name="",
            source=IDENTITY_SOURCE_PENDING,
            login="",
            created_at=time.time(),
        )
        OPERATOR_REGISTRY.put(token, ident)
        return token, ident, is_new

    def _set_cookie(self, token: str) -> None:
        c = http.cookies.SimpleCookie()
        c[OP_COOKIE] = token
        c[OP_COOKIE]["path"] = "/"
        c[OP_COOKIE]["max-age"] = OP_COOKIE_MAX_AGE
        c[OP_COOKIE]["httponly"] = True
        c[OP_COOKIE]["samesite"] = "Lax"
        # morsel.OutputString() returns a header value without "Set-Cookie: "
        self.send_header("Set-Cookie", c[OP_COOKIE].OutputString())

    # ── routing ──
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            # Mint a cookie on first visit so /api/meta + /api/events carry it.
            token, _ident, is_new = self._resolve_identity()
            self._serve_html(INDEX_HTML, set_cookie_token=token if is_new else None)
        elif path == "/api/meta":
            token, ident, is_new = self._resolve_identity()
            self._json({
                "channel": self.channel,
                "operator": {
                    "id": ident.member_id,
                    "name": ident.display_name,
                    "source": ident.source,
                    "pending": ident.source == IDENTITY_SOURCE_PENDING,
                },
                "server_host": socket.gethostname(),
            }, set_cookie_token=token if is_new else None)
        elif path == "/api/events":
            self._serve_sse()
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/send":
            self._handle_send()
        elif parsed.path == "/api/identify":
            self._handle_identify()
        else:
            self._error(404, "not found")

    # ── handlers ──
    def _serve_html(self, body: str, set_cookie_token: Optional[str] = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie_token:
            self._set_cookie(set_cookie_token)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj: Any, status: int = 200, set_cookie_token: Optional[str] = None) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie_token:
            self._set_cookie(set_cookie_token)
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

    def _read_json_body(self, max_bytes: int = 16384) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > max_bytes:
            self._error(400, "missing or oversized body")
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "invalid JSON")
            return None

    def _handle_identify(self) -> None:
        body = self._read_json_body(max_bytes=2048)
        if body is None:
            return
        raw_name = (body.get("name") or "").strip()
        if not raw_name:
            self._error(400, "name required")
            return
        if len(raw_name) > 40:
            self._error(400, "name too long (max 40 chars)")
            return
        token, _is_new = self._get_or_mint_cookie()
        existing = OPERATOR_REGISTRY.get(token)
        if existing and existing.source == IDENTITY_SOURCE_TAILSCALE:
            # Already identity-traceable — refuse to downgrade to Guest.
            self._json({
                "ok": True, "upgraded": False,
                "operator": {"id": existing.member_id, "name": existing.display_name,
                             "source": existing.source, "pending": False},
            })
            return
        ident = OPERATOR_REGISTRY.register_guest(token, raw_name)
        self._json({
            "ok": True,
            "operator": {"id": ident.member_id, "name": ident.display_name,
                         "source": ident.source, "pending": False},
        }, set_cookie_token=token)

    def _handle_send(self) -> None:
        body = self._read_json_body()
        if body is None:
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

        token, ident, _is_new = self._resolve_identity()
        if ident.source == IDENTITY_SOURCE_PENDING:
            self._error(403, "identity required — POST /api/identify first")
            return

        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            op_id, op_name = ensure_operator_row(db, self.channel, ident)
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
<title>nth_web</title>
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
  button { font-family: inherit; }

  #app { display: grid; grid-template-columns: 1fr 300px; grid-template-rows: 42px 1fr auto;
         height: 100vh; }

  /* ── Header ── */
  header { grid-column: 1 / 3; background: var(--bg2); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; padding: 0 14px; gap: 12px;
           font-weight: 600; }
  header .title { color: var(--accent); }
  header .meta { color: var(--dim); font-weight: 400; font-size: 11px; }
  header .spacer { flex: 1; }
  header .pill {
    font-size: 11px; padding: 3px 8px; border-radius: 3px; cursor: pointer;
    background: var(--panel); border: 1px solid var(--border); user-select: none;
    color: var(--dim); font-weight: 500;
  }
  header .pill:hover { border-color: var(--accent); color: var(--fg); }
  header .pill.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  header .pill.conn.ok { color: var(--accent2); }
  header .pill.conn.bad { color: var(--err); }
  header #filter { background: var(--panel); color: var(--fg); border: 1px solid var(--border);
                   padding: 3px 8px; border-radius: 3px; font-family: inherit; font-size: 11px;
                   width: 160px; }
  header #filter:focus { outline: none; border-color: var(--accent); }

  /* ── Chat ── */
  #chat-wrap { grid-row: 2 / 3; grid-column: 1 / 2; position: relative; overflow: hidden; }
  #chat { height: 100%; overflow-y: auto; padding: 12px 16px; scroll-behavior: smooth; }
  .msg { margin-bottom: 10px; word-wrap: break-word; cursor: pointer; padding: 4px 8px 6px;
         border-radius: 3px; border-left: 3px solid transparent; margin-left: -8px; }
  .msg:hover { background: #0f1420; }
  .msg .head { font-size: 11px; color: var(--dim); margin-bottom: 2px;
               display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .msg .head .time { cursor: help; }
  .msg .author { font-weight: 600; }
  .msg .mentions-bar { font-size: 11px; margin: 2px 0 4px;
                       display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  .msg .mentions-bar .to-label { color: var(--dim); font-size: 10px;
                                  text-transform: uppercase; letter-spacing: 0.5px;
                                  margin-right: 2px; }
  .msg .mentions-bar .mchip { display: inline-flex; align-items: center; gap: 3px;
                               padding: 1px 7px 1px 5px; border-radius: 10px;
                               background: rgba(255, 196, 116, 0.15);
                               color: var(--mention);
                               border: 1px solid rgba(255, 196, 116, 0.3);
                               font-weight: 600; }
  .msg .mentions-bar .mchip .manimal { font-size: 13px; line-height: 1; }
  /* #pound references bar — "about" someone, not "to" them. Muted vs. @ pings. */
  .msg .refs-bar { font-size: 11px; margin: 2px 0 4px;
                   display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  .msg .refs-bar .to-label { color: var(--dim); font-size: 10px;
                              text-transform: uppercase; letter-spacing: 0.5px;
                              margin-right: 2px; }
  .msg .refs-bar .mchip { display: inline-flex; align-items: center; gap: 3px;
                          padding: 1px 7px 1px 5px; border-radius: 10px;
                          background: rgba(126, 222, 126, 0.08);
                          color: #9ccf9c;
                          border: 1px solid rgba(126, 222, 126, 0.25);
                          font-weight: 500; }
  .msg .refs-bar .mchip .manimal { font-size: 13px; line-height: 1; }
  .msg .body { white-space: pre-wrap; }
  .msg.compact .body {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .msg.compact .body::after { content: ""; }
  .msg.system .body { color: var(--dim); font-style: italic; }
  .msg.mine .author { color: var(--accent2); }
  .msg.targeted { background: #1a2030; border-left-color: var(--mention); }
  .msg.filtered-out { display: none; }
  .msg.dm-hidden { display: none; }
  body.dm-mode .acks { display: none; }  /* two participants; ack badges are noise */

  /* Ack badges — one per member. Emoji is the identity; colored ring
     is a secondary signal. Read = full opacity, pending = dim + desaturated. */
  .acks { display: inline-flex; gap: 3px; margin-left: auto; align-items: center; }
  .ack-badge { display: inline-flex; align-items: center; justify-content: center;
               width: 20px; height: 20px; border-radius: 50%;
               font-size: 13px; line-height: 1;
               background: transparent;
               border: 1.5px solid transparent;
               cursor: pointer;
               user-select: none; }
  .ack-badge.read    { opacity: 1; }
  .ack-badge.pending { opacity: 0.35; filter: grayscale(0.7); }
  .ack-badge.self    { display: none; }

  /* Watermark pins — animal emoji parked at the highest message a given
     member has read. One pin per member, migrates forward as they ack. */
  .msg { position: relative; }
  .watermark-pins { position: absolute; right: 6px; bottom: 2px;
                    display: flex; gap: 3px; pointer-events: none;
                    opacity: 0.9; }
  .watermark-pin { font-size: 16px; line-height: 1;
                   transition: transform 0.35s ease;
                   text-shadow: 0 0 2px var(--bg), 0 0 2px var(--bg); }
  .watermark-pin.self { filter: drop-shadow(0 0 3px var(--accent)); }
  .watermark-pin.here { animation: here-pulse 1.8s ease-in-out infinite; }
  @keyframes here-pulse {
    0%, 100% { transform: translateX(0); opacity: 0.95; }
    50%      { transform: translateX(-3px); opacity: 0.55; }
  }

  /* Jump-to-latest */
  #jump-btn { position: absolute; right: 18px; bottom: 14px;
              background: var(--accent); color: var(--bg); border: none; padding: 6px 12px;
              border-radius: 18px; cursor: pointer; font-weight: 600; font-size: 11px;
              box-shadow: 0 4px 14px rgba(0,0,0,0.5); display: none; z-index: 5; }
  #jump-btn.show { display: block; }
  #jump-btn:hover { background: #50b0f0; }
  #jump-btn .count { background: var(--err); color: white;
                     border-radius: 10px; padding: 1px 6px; margin-left: 4px; font-size: 10px; }

  /* ── Roster sidebar ── */
  #side { grid-row: 2 / 3; grid-column: 2 / 3;
          background: var(--panel); border-left: 1px solid var(--border);
          overflow-y: auto; display: flex; flex-direction: column; }
  #side section { padding: 10px 12px; border-bottom: 1px solid var(--border); }
  #side section:last-child { border-bottom: none; }
  #side h2 { font-size: 10px; text-transform: uppercase; color: var(--dim);
             letter-spacing: 0.08em; margin: 0 0 8px; font-weight: 600; }

  .member { padding: 5px 0; cursor: pointer; }
  .member + .member { border-top: 1px solid #1d2533; }
  .member .row { display: flex; align-items: center; gap: 8px; }
  .member .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .member .roster-animal { font-size: 16px; line-height: 1; flex-shrink: 0;
                           user-select: none; }
  .member .dm-btn { font-size: 9px; padding: 2px 5px; border-radius: 3px;
                    background: #1c2432; color: var(--dim); border: 1px solid #283242;
                    cursor: pointer; flex-shrink: 0; user-select: none;
                    text-transform: uppercase; letter-spacing: 0.5px; }
  .member .dm-btn:hover { background: var(--accent); color: var(--bg);
                          border-color: var(--accent); }
  .member .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                  font-weight: 500; }
  .member .caret { color: var(--dimmer); font-size: 9px; transition: transform 0.1s; }
  .member.expanded .caret { transform: rotate(90deg); }
  .member .id { color: var(--dimmer); font-size: 10px; margin-left: 2px; }
  .dot.active { background: var(--accent2); }
  .dot.idle { background: var(--dimmer); }
  .dot.stale { background: var(--warn); }
  .dot.dead { background: var(--err); }
  .member .stext { font-size: 10px; color: var(--dim); margin-top: 2px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                   padding-left: 16px; }

  .member .stats { display: none; padding: 8px 0 2px 16px;
                   font-size: 10px; color: var(--dim); }
  .member.expanded .stats { display: block; }
  .stats .stat-row { display: flex; justify-content: space-between; padding: 2px 0; gap: 10px; }
  .stats .stat-label { color: var(--dim); }
  .stats .stat-val { color: var(--fg); font-weight: 600; text-align: right;
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                     max-width: 180px; }
  .stats .stat-val.good { color: var(--accent2); }
  .stats .stat-val.warn { color: var(--warn); }
  .stats .stat-val.bad { color: var(--err); }
  .stats .snippet { color: var(--fg); font-style: italic;
                    white-space: normal; padding-top: 4px; line-height: 1.3;
                    max-height: 54px; overflow: hidden; }

  /* Channel stats block */
  #chanstats .stat-row { display: flex; justify-content: space-between; padding: 3px 0;
                         font-size: 11px; }
  #chanstats .stat-label { color: var(--dim); }
  #chanstats .stat-val { color: var(--fg); font-weight: 600; }
  #sparkline { font-family: inherit; font-size: 14px; color: var(--accent);
               letter-spacing: -1px; padding-top: 4px; }
  #filter-banner { padding: 4px 8px; background: #1a2030; color: var(--mention);
                   font-size: 10px; border-radius: 3px; margin-bottom: 6px;
                   display: none; cursor: pointer; }
  #filter-banner.active { display: block; }

  /* ── Composer (unchanged from v1) ── */
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

  /* Guest identify modal */
  #guest-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.75);
                 display: flex; align-items: center; justify-content: center;
                 z-index: 1000; }
  #guest-modal .guest-card { background: var(--panel); border: 1px solid #2a3342;
                             border-radius: 8px; padding: 22px 26px; width: min(460px, 90vw);
                             box-shadow: 0 10px 40px rgba(0,0,0,0.6); }
  #guest-modal h2 { margin: 0 0 10px 0; font-size: 16px; }
  #guest-modal p { margin: 8px 0; font-size: 13px; line-height: 1.4; color: var(--fg); }
  #guest-modal p.dim { color: var(--dim); font-size: 12px; }
  #guest-modal label { display: block; margin: 14px 0 4px; font-size: 12px;
                       color: var(--dim); }
  #guest-modal input { width: 100%; padding: 8px 10px; background: var(--bg);
                       color: var(--fg); border: 1px solid #2a3342; border-radius: 4px;
                       font-size: 14px; box-sizing: border-box; }
  #guest-modal input:focus { outline: none; border-color: var(--accent); }
  #guest-modal .guest-err { color: #ff8470; font-size: 12px; min-height: 16px;
                             margin-top: 6px; }
  #guest-modal button { margin-top: 10px; padding: 8px 16px; background: var(--accent);
                        color: var(--bg); border: none; border-radius: 4px;
                        font-weight: 600; cursor: pointer; }
  #guest-modal button:hover { background: #50b0f0; }
</style>
</head>
<body>
<div id="guest-modal" style="display:none">
  <div class="guest-card">
    <h2>Identify yourself</h2>
    <p>Tailscale didn't recognise your connection, so you're joining as a <b>Guest</b>.
       Agents will see you as untrusted and self-declared — they should not treat your
       messages as authoritative.</p>
    <p class="dim">If you should be identified via Tailscale, connect via your tailnet
       IP and reload.</p>
    <label>Display name
      <input id="guest-name" type="text" maxlength="40" placeholder="e.g. Bob" autocomplete="off">
    </label>
    <div class="guest-err" id="guest-err"></div>
    <button id="guest-submit">Join as Guest</button>
  </div>
</div>
<div id="app">
  <header>
    <span class="title" id="h-channel">trio#…</span>
    <span class="meta" id="h-meta">connecting…</span>
    <span class="spacer"></span>
    <input id="filter" type="text" placeholder="filter messages…" spellcheck="false">
    <span class="pill" id="btn-compact" title="clamp every message body to 3 lines">compact</span>
    <span class="pill" id="btn-notify" title="desktop notifications on @you">🔔 off</span>
    <span class="pill conn bad" id="h-conn">● disconnected</span>
  </header>

  <div id="chat-wrap">
    <div id="chat"></div>
    <button id="jump-btn">↓ latest<span class="count" id="jump-count" style="display:none">0</span></button>
  </div>

  <aside id="side">
    <section>
      <div id="filter-banner">filter active — showing matching messages only. click to clear.</div>
      <h2 id="r-heading">Members</h2>
      <div id="r-list"></div>
    </section>
    <section id="chanstats-wrap">
      <h2>Channel stats</h2>
      <div id="chanstats"></div>
      <div id="sparkline"></div>
    </section>
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
      <kbd>↑/↓</kbd> navigate
      <span style="margin-left:14px;color:var(--dim)">click a message to expand/collapse in compact mode</span>
    </div>
  </div>
</div>

<script>
(() => {
  // ── DOM handles ──
  const chatWrap = document.getElementById('chat-wrap');
  const chat = document.getElementById('chat');
  const rosterEl = document.getElementById('r-list');
  const rosterHeading = document.getElementById('r-heading');
  const chanStatsEl = document.getElementById('chanstats');
  const sparkEl = document.getElementById('sparkline');
  const hChannel = document.getElementById('h-channel');
  const hMeta = document.getElementById('h-meta');
  const hConn = document.getElementById('h-conn');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send-btn');
  const preview = document.getElementById('preview');
  const compEl = document.getElementById('completions');
  const filterEl = document.getElementById('filter');
  const filterBanner = document.getElementById('filter-banner');
  const btnCompact = document.getElementById('btn-compact');
  const btnNotify = document.getElementById('btn-notify');
  const jumpBtn = document.getElementById('jump-btn');
  const jumpCount = document.getElementById('jump-count');

  // ── URL params ──
  const URL_PARAMS = new URLSearchParams(location.search);
  const DM_TARGET_ID = URL_PARAMS.get('dm') || '';
  const DM_MODE = !!DM_TARGET_ID;

  // ── State ──
  const state = {
    channel: '',
    operator: { id: '', name: '' },
    server_host: '',
    dmTargetId: DM_TARGET_ID,      // empty string → main channel view
    members: new Map(),            // id → member (roster row)
    messages: new Map(),            // id → message
    messageDomById: new Map(),      // id → DOM node (for ack badge updates)
    seenMsgIds: new Set(),
    completion: { visible: false, index: 0, items: [], atPos: -1, sigil: '@' },
    agentStats: new Map(),          // id → {sent, sent_times[], lengths[], lastSnippet,
                                    //        read_latencies[], queue_depth,
                                    //        directed_received, directed_replied, pending_directed[]}
    filter: '',
    compact: false,                 // global compact mode
    expandedMsgs: new Set(),        // ids with per-msg override (toggle-specific)
    expandedMembers: new Set(),     // member ids with expanded stats
    notifyEnabled: false,
    unreadCount: 0,                 // for tab title while hidden
    jumpUnread: 0,                  // messages arrived while user was scrolled up
    rateBins: new Map(),            // bin_epoch_10s → count
    startedAt: Date.now(),
    originalTitle: 'nth_web',
  };
  const PALETTE = ['#62d7ef','#d070d7','#7ede7e','#e5d35e',
                   '#8eb9ff','#ff8470','#9ef0f0','#f79fea'];
  // Must match Python animal_for() in nth_constants.py — don't reorder.
  const ANIMAL_EMOJIS = /*__ANIMAL_EMOJIS__*/;
  const ANIMAL_NAMES  = /*__ANIMAL_NAMES__*/;
  function hash32(id) {
    let h = 0;
    for (const c of (id || '')) h = ((h * 31 + c.charCodeAt(0)) >>> 0);
    return h;
  }
  function colorFor(id) {
    return PALETTE[hash32(id) % PALETTE.length];
  }
  function animalFor(member) {
    const id = (member && (member.id || member.member_id)) || '';
    const i = hash32(id) % ANIMAL_EMOJIS.length;
    return { name: ANIMAL_NAMES[i], emoji: ANIMAL_EMOJIS[i] };
  }
  function initialOf(member) {
    // Kept as a fallback only; UI uses animalFor().
    const n = (member && (member.name || member.id)) || '?';
    return n.trim().charAt(0).toUpperCase() || '?';
  }
  function escapeHtml(s) { return s.replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }

  // ── Time ──
  function formatTime(iso) {
    if (!iso) return '--:--';
    try {
      const d = new Date(iso);
      return d.toTimeString().slice(0, 8);
    } catch (e) { return '--:--'; }
  }
  function fmtRel(seconds) {
    if (seconds == null || !isFinite(seconds)) return '—';
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }

  const SYSTEM_PREFIXES = ['[claimed ', '[done ', '[cancelled ', '[released ',
                           '[retracted ', '[joined ', '[left ', '[ended ',
                           '[locked ', '[unlocked ', '[status ', '[pinned ',
                           '[renamed '];
  function isSystemContent(s) { return SYSTEM_PREFIXES.some(p => s.startsWith(p)); }

  // ── Per-member agent stats (client-side aggregate, derived from event stream) ──
  function agentState(id) {
    if (!state.agentStats.has(id)) {
      state.agentStats.set(id, {
        sent: 0, sent_times: [], lengths: [], lastSnippet: '',
        read_latencies: [], queue_depth: 0,
        directed_received: 0, directed_replied: 0, pending_directed: [],
        last_read_seen: 0,    // last snapshot of this member's DB last_read value
      });
    }
    return state.agentStats.get(id);
  }

  function ingestMessageForStats(msg) {
    const s = agentState(msg.member_id);
    s.sent++;
    s.sent_times.push(new Date(msg.created_at).getTime() || Date.now());
    if (s.sent_times.length > 500) s.sent_times.shift();
    s.lengths.push((msg.content || '').length);
    if (s.lengths.length > 20) s.lengths.shift();
    s.lastSnippet = (msg.content || '').slice(0, 100);

    // @-reply accounting: if sender had pending directed messages to reply to,
    // count this send as a reply to all of them (first-response-counts).
    while (s.pending_directed.length > 0) {
      s.pending_directed.shift();
      s.directed_replied++;
    }

    // For every other member, this new message either bumps their queue
    // (if their last_read < msg.id) or is for a mentioned recipient.
    for (const [mid, mem] of state.members) {
      if (mid === msg.member_id) continue;
      if ((mem.last_read || 0) < msg.id) {
        const ms = agentState(mid);
        ms.queue_depth++;
      }
      if ((msg.mentions || []).includes(mid)) {
        const ms = agentState(mid);
        ms.directed_received++;
        ms.pending_directed.push(msg.id);
      }
    }

    // Global activity rate bins (10-second granularity)
    const bin = Math.floor((new Date(msg.created_at).getTime() || Date.now()) / 10000) * 10000;
    state.rateBins.set(bin, (state.rateBins.get(bin) || 0) + 1);
  }

  function applyRosterWatermarkDeltas(newMembers) {
    const now = Date.now();
    for (const m of newMembers) {
      const prev = state.members.get(m.id);
      const prevLR = prev ? (prev.last_read || 0) : 0;
      const newLR = m.last_read || 0;
      if (newLR > prevLR) {
        const s = agentState(m.id);
        // Credit read-latencies for messages in (prevLR, newLR]
        for (const [msgId, msg] of state.messages) {
          if (msgId > prevLR && msgId <= newLR && msg.member_id !== m.id) {
            const sent = new Date(msg.created_at).getTime();
            if (sent) {
              s.read_latencies.push((now - sent) / 1000);
              if (s.read_latencies.length > 20) s.read_latencies.shift();
            }
            // Decrement their queue — they've now read this one.
            s.queue_depth = Math.max(0, s.queue_depth - 1);
          }
        }
        s.last_read_seen = newLR;
      }
    }
  }

  function agentSendRatePerHour(id) {
    const s = state.agentStats.get(id);
    if (!s) return 0;
    const cutoff = Date.now() - 3600 * 1000;
    return s.sent_times.filter(t => t >= cutoff).length;
  }
  function agentAvgReadLatency(id) {
    const s = state.agentStats.get(id);
    if (!s || s.read_latencies.length === 0) return null;
    return s.read_latencies.reduce((a, b) => a + b, 0) / s.read_latencies.length;
  }
  function agentAvgLen(id) {
    const s = state.agentStats.get(id);
    if (!s || s.lengths.length === 0) return null;
    return s.lengths.reduce((a, b) => a + b, 0) / s.lengths.length;
  }
  function agentReplyRate(id) {
    const s = state.agentStats.get(id);
    if (!s || s.directed_received === 0) return null;
    return s.directed_replied / s.directed_received;
  }

  // ── Ack badges per message ──
  function updateAckBadges(msgId) {
    const dom = state.messageDomById.get(msgId);
    if (!dom) return;
    const box = dom.querySelector('.acks');
    if (!box) return;
    box.innerHTML = '';
    const msg = state.messages.get(msgId);
    if (!msg) return;
    // One badge per NON-operator, NON-sender member. Sender doesn't need to
    // ack their own message; operator is already us.
    for (const [mid, mem] of state.members) {
      if (mid === state.operator.id) continue;
      if (mid === msg.member_id) continue;
      const read = (mem.last_read || 0) >= msgId;
      const { name: animalName, emoji } = animalFor(mem);
      const badge = document.createElement('span');
      badge.className = 'ack-badge ' + (read ? 'read' : 'pending');
      badge.textContent = emoji;
      badge.style.borderColor = colorFor(mid);
      badge.title = `${mem.name} (${mid}) — the ${animalName} — ${read ? 'read ✓' : 'pending…'}  · last_read: ${mem.last_read}  (click to open DM tab)`;
      badge.onclick = (e) => {
        e.stopPropagation();
        if (!DM_MODE) window.open('/?dm=' + encodeURIComponent(mid), '_blank');
      };
      box.appendChild(badge);
    }
  }

  function updateAllAckBadges() {
    for (const id of state.messageDomById.keys()) updateAckBadges(id);
  }

  // Build a sigil-bar (@mentions or #refs) for a message — factored so
  // both visual styles use identical markup and differ only in class +
  // label + sigil.
  function renderTargetBar(ids, className, sigil, label) {
    const bar = document.createElement('div');
    bar.className = className;
    const lab = document.createElement('span');
    lab.className = 'to-label';
    lab.textContent = label;
    bar.appendChild(lab);
    for (const id of ids) {
      const mem = state.members.get(id);
      const nm = mem ? mem.name : id;
      const anim = animalFor(mem || { id });
      const chip = document.createElement('span');
      chip.className = 'mchip';
      const a = document.createElement('span');
      a.className = 'manimal';
      a.textContent = anim.emoji;
      chip.appendChild(a);
      chip.appendChild(document.createTextNode(sigil + nm));
      bar.appendChild(chip);
    }
    return bar;
  }

  // ── Message rendering ──
  function applyCompactClass(node, id) {
    const override = state.expandedMsgs.has(id);
    if (state.compact && !override) node.classList.add('compact');
    else node.classList.remove('compact');
  }

  function appendMessage(m) {
    if (state.seenMsgIds.has(m.id)) return;
    state.seenMsgIds.add(m.id);
    state.messages.set(m.id, m);
    ingestMessageForStats(m);

    const isMine = m.member_id === state.operator.id;
    const isSystem = isSystemContent(m.content || '');
    const mentionsOperator = (m.mentions || []).includes(state.operator.id);

    const div = document.createElement('div');
    div.className = 'msg' + (isMine ? ' mine' : '') + (isSystem ? ' system' : '')
                  + (mentionsOperator ? ' targeted' : '');
    div.dataset.msgId = String(m.id);
    div.dataset.search = (m.content || '').toLowerCase() + ' '
                       + (m.member_name || '').toLowerCase();

    const head = document.createElement('div');
    head.className = 'head';
    const timeSpan = document.createElement('span');
    timeSpan.className = 'time';
    timeSpan.textContent = formatTime(m.created_at);
    timeSpan.title = m.created_at || '';
    head.appendChild(timeSpan);
    if (!isSystem) {
      const author = document.createElement('span');
      author.className = 'author';
      author.textContent = m.member_name;
      author.style.color = colorFor(m.member_id);
      head.appendChild(author);
    }
    const acks = document.createElement('span');
    acks.className = 'acks';
    head.appendChild(acks);
    div.appendChild(head);

    // Prominent @mentions bar (pings) — always rendered when there are pings,
    // above the body so auto-@ recipients can't be missed even when the composer
    // didn't write @name inline.
    if (!isSystem && m.mentions && m.mentions.length) {
      div.appendChild(renderTargetBar(m.mentions, 'mentions-bar', '@', '→'));
    }
    // #pound refs bar (talked about, not pinged). Softer visual.
    if (!isSystem && m.refs && m.refs.length) {
      div.appendChild(renderTargetBar(m.refs, 'refs-bar', '#', 'about'));
    }

    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = m.content;
    div.appendChild(body);

    // Watermark pins — animals of agents whose last_read == this message id.
    const pins = document.createElement('div');
    pins.className = 'watermark-pins';
    div.appendChild(pins);

    // Toggle expand/compact on click
    div.addEventListener('click', (e) => {
      if (e.target.closest('.ack-badge')) return;
      if (state.expandedMsgs.has(m.id)) state.expandedMsgs.delete(m.id);
      else state.expandedMsgs.add(m.id);
      applyCompactClass(div, m.id);
    });

    applyCompactClass(div, m.id);
    applyFilterToNode(div);
    applyDmFilterToNode(div, m);

    const nearBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    chat.appendChild(div);
    state.messageDomById.set(m.id, div);
    updateAckBadges(m.id);
    renderWatermarkPins();
    scheduleHereUpdate();

    if (nearBottom) {
      chat.scrollTop = chat.scrollHeight;
    } else {
      state.jumpUnread++;
      updateJumpButton();
    }

    // Tab-title badge when hidden
    if (document.hidden) {
      state.unreadCount++;
      updateTitle();
    }

    // Desktop notification on @you while hidden (opt-in). In DM mode,
    // only fire for the DM target — don't pull focus for other channel chatter.
    const notifyEligible = !isMine && mentionsOperator &&
                           (!state.dmTargetId || m.member_id === state.dmTargetId);
    if (document.hidden && notifyEligible && state.notifyEnabled &&
        'Notification' in window && Notification.permission === 'granted') {
      try {
        const n = new Notification(`@${state.operator.name} — ${m.member_name}`, {
          body: (m.content || '').slice(0, 140),
          tag: 'trio-' + m.id,
          silent: false,
        });
        n.onclick = () => { window.focus(); n.close(); };
      } catch (e) { /* ignore */ }
    }
  }

  // Existing message names may change (rename) — update author labels + mention
  // resolutions in-place so backscroll stays readable.
  function refreshMessageAuthors() {
    for (const [id, m] of state.messages) {
      const dom = state.messageDomById.get(id);
      if (!dom) continue;
      const author = dom.querySelector('.author');
      if (author && !isSystemContent(m.content || '')) {
        author.textContent = m.member_name;
        author.style.color = colorFor(m.member_id);
      }
      function rebuildBar(bar, ids, sigil) {
        if (!bar || !ids || !ids.length) return;
        while (bar.childNodes.length > 1) bar.removeChild(bar.lastChild);
        for (const mid of ids) {
          const mem = state.members.get(mid);
          const nm = mem ? mem.name : mid;
          const anim = animalFor(mem || { id: mid });
          const chip = document.createElement('span');
          chip.className = 'mchip';
          const a = document.createElement('span');
          a.className = 'manimal';
          a.textContent = anim.emoji;
          chip.appendChild(a);
          chip.appendChild(document.createTextNode(sigil + nm));
          bar.appendChild(chip);
        }
      }
      rebuildBar(dom.querySelector('.mentions-bar'), m.mentions, '@');
      rebuildBar(dom.querySelector('.refs-bar'), m.refs, '#');
    }
  }

  // ── Roster rendering ──
  function renderRoster(members) {
    applyRosterWatermarkDeltas(members);

    // Reconcile state.members — and detect name changes so the chat can
    // retroactively re-label past messages from the renamed member.
    const rename_from = new Map();  // id → old member_name for messages
    for (const m of members) {
      const old = state.members.get(m.id);
      state.members.set(m.id, m);
      if (old && old.name !== m.name) rename_from.set(m.id, { from: old.name, to: m.name });
    }

    if (rename_from.size > 0) {
      // Patch cached message records so author label follows the current alias.
      for (const [id, msg] of state.messages) {
        const rename = rename_from.get(msg.member_id);
        if (rename) {
          msg.member_name = rename.to;
        }
      }
      refreshMessageAuthors();
    }

    rosterEl.innerHTML = '';
    const sorted = members.slice().sort((a, b) => {
      const order = { active: 0, idle: 1, stale: 2, dead: 3 };
      if (a.id === state.operator.id) return 1;
      if (b.id === state.operator.id) return -1;
      const oa = order[a.status] ?? 4;
      const ob = order[b.status] ?? 4;
      if (oa !== ob) return oa - ob;
      return (a.name || '').localeCompare(b.name || '');
    });
    for (const m of sorted) rosterEl.appendChild(renderMemberRow(m));
    rosterHeading.textContent = `Members (${members.length})`;

    updateAllAckBadges();
    renderWatermarkPins();
    scheduleHereUpdate();
    updateChanStats();

    // DM mode: update tab title with target's current name/animal now
    // that we have the roster.
    if (DM_MODE) {
      const tgt = state.members.get(DM_TARGET_ID);
      if (tgt) {
        const a = animalFor(tgt);
        const label = `DM ${a.emoji} ${tgt.name} — trio#${state.channel}`;
        state.originalTitle = label;
        hChannel.textContent = label;
        updateTitle();
      }
    }
  }

  // ── Watermark pins: one animal per member, parked at their last-read msg ──
  function renderWatermarkPins() {
    // Clear existing pins first
    for (const dom of state.messageDomById.values()) {
      const c = dom.querySelector('.watermark-pins');
      if (c) c.innerHTML = '';
    }
    // Sorted message ids (ascending). state.messageDomById preserves
    // insertion order, but be explicit because history prefixing
    // might out-of-order future paths.
    const sortedIds = [...state.messageDomById.keys()].sort((a, b) => a - b);
    if (sortedIds.length === 0) return;
    for (const [mid, mem] of state.members) {
      const lr = mem.last_read || 0;
      if (lr <= 0) continue;
      // Binary search: highest id <= lr in sortedIds
      let lo = 0, hi = sortedIds.length - 1, pinId = -1;
      while (lo <= hi) {
        const k = (lo + hi) >> 1;
        if (sortedIds[k] <= lr) { pinId = sortedIds[k]; lo = k + 1; }
        else hi = k - 1;
      }
      if (pinId < 0) continue;
      const dom = state.messageDomById.get(pinId);
      if (!dom) continue;
      const c = dom.querySelector('.watermark-pins');
      if (!c) continue;
      const a = animalFor(mem);
      const pin = document.createElement('span');
      pin.className = 'watermark-pin' + (mid === state.operator.id ? ' self' : '');
      pin.textContent = a.emoji;
      pin.title = `${mem.name} — the ${a.name} — read through #${lr}`;
      c.appendChild(pin);
    }
  }

  function renderMemberRow(m) {
    const { name: animalName, emoji } = animalFor(m);
    const row = document.createElement('div');
    row.className = 'member' + (state.expandedMembers.has(m.id) ? ' expanded' : '');
    row.title = `${m.name} (${m.id}) — the ${animalName}\n${m.status_text || ''}\nlast_read: ${m.last_read}`;

    const topRow = document.createElement('div');
    topRow.className = 'row';
    const dot = document.createElement('div');
    dot.className = 'dot ' + m.status;
    topRow.appendChild(dot);
    const animalSpan = document.createElement('span');
    animalSpan.className = 'roster-animal';
    animalSpan.textContent = emoji;
    animalSpan.title = `the ${animalName}`;
    topRow.appendChild(animalSpan);
    const nameBox = document.createElement('div');
    nameBox.className = 'name';
    nameBox.textContent = m.name;
    nameBox.style.color = colorFor(m.id);
    topRow.appendChild(nameBox);
    const idSpan = document.createElement('div');
    idSpan.className = 'id';
    idSpan.textContent = m.id.slice(0, 8);
    topRow.appendChild(idSpan);
    // DM button — opens a filtered-view tab for this agent.
    // Hide for self, for human operator rows, and inside an existing DM tab.
    if (!DM_MODE && m.id !== state.operator.id && !m.id.startsWith('_op_')) {
      const dmBtn = document.createElement('span');
      dmBtn.className = 'dm-btn';
      dmBtn.textContent = 'DM';
      dmBtn.title = `Open DM tab with ${m.name}`;
      dmBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        window.open('/?dm=' + encodeURIComponent(m.id), '_blank');
      });
      topRow.appendChild(dmBtn);
    }
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = '▶';
    topRow.appendChild(caret);
    row.appendChild(topRow);

    if (m.status_text) {
      const st = document.createElement('div');
      st.className = 'stext';
      st.textContent = m.status_text;
      row.appendChild(st);
    }

    const stats = document.createElement('div');
    stats.className = 'stats';
    stats.innerHTML = renderMemberStatsHTML(m);
    row.appendChild(stats);

    row.addEventListener('click', (e) => {
      // Clicking the name on a mention-capable row? On shift-click → filter.
      if (e.shiftKey) {
        setFilter(m.name);
        return;
      }
      if (state.expandedMembers.has(m.id)) state.expandedMembers.delete(m.id);
      else state.expandedMembers.add(m.id);
      row.classList.toggle('expanded');
      stats.innerHTML = renderMemberStatsHTML(m);
    });
    return row;
  }

  function renderMemberStatsHTML(m) {
    const maxId = Math.max(0, ...state.messages.keys());
    const behind = Math.max(0, maxId - (m.last_read || 0));
    const lat = agentAvgReadLatency(m.id);
    const latClass = lat == null ? '' : (lat >= 20 ? 'bad' : (lat >= 5 ? 'warn' : 'good'));
    const q = (state.agentStats.get(m.id) || {}).queue_depth || 0;
    const qClass = q >= 10 ? 'bad' : (q >= 3 ? 'warn' : 'good');
    const sent = (state.agentStats.get(m.id) || {}).sent || 0;
    const rate = agentSendRatePerHour(m.id);
    const rr = agentReplyRate(m.id);
    const alen = agentAvgLen(m.id);
    const snippet = (state.agentStats.get(m.id) || {}).lastSnippet || '';
    const lastSeenAge = m.last_seen ? fmtRel((Date.now() - new Date(m.last_seen).getTime()) / 1000) : '—';

    const rows = [
      ['seen',          escapeHtml(lastSeenAge), ''],
      ['last_read',     `${m.last_read} <span style="color:var(--dimmer)">(${behind} behind)</span>`, behind > 5 ? 'warn' : ''],
      ['read-lat',      lat == null ? '—' : lat.toFixed(1) + 's', latClass],
      ['sent',          `${sent} <span style="color:var(--dimmer)">(${rate}/h)</span>`, ''],
      ['queue',         String(q), qClass],
      ['@reply %',      rr == null ? '—' : Math.round(rr * 100) + '%', ''],
      ['avg len',       alen == null ? '—' : Math.round(alen), ''],
    ];
    let html = '';
    for (const [k, v, cls] of rows) {
      html += `<div class="stat-row"><span class="stat-label">${k}</span>`
           +  `<span class="stat-val ${cls}">${v}</span></div>`;
    }
    if (snippet) {
      html += `<div class="snippet" title="${escapeHtml(snippet)}">${escapeHtml(snippet)}</div>`;
    }
    return html;
  }

  // ── Channel stats ──
  function updateChanStats() {
    const totalMsgs = state.messages.size;
    const runtime = (Date.now() - state.startedAt) / 1000;
    const now = Date.now();
    const cutoff = now - 5 * 60 * 1000;
    let recent = 0;
    for (const [bin, count] of state.rateBins) if (bin >= cutoff) recent += count;
    const ratePerMin = recent / 5;   // msgs/min over last 5 min

    const stats = [
      ['total messages', totalMsgs],
      ['rate (5m avg)', ratePerMin.toFixed(1) + '/min'],
      ['session uptime', fmtRel(runtime)],
    ];
    let html = '';
    for (const [k, v] of stats) {
      html += `<div class="stat-row"><span class="stat-label">${k}</span>`
           +  `<span class="stat-val">${v}</span></div>`;
    }
    chanStatsEl.innerHTML = html;
    renderSparkline();
  }
  function renderSparkline() {
    const BARS = '▁▂▃▄▅▆▇█';
    const WIN_MIN = 5;
    const WIN_SEC = WIN_MIN * 60;
    const binSize = 10;
    const now = Date.now();
    const nowBin = Math.floor(now / (binSize * 1000)) * (binSize * 1000);
    const wantBins = WIN_SEC / binSize;
    const vals = [];
    for (let i = wantBins - 1; i >= 0; i--) {
      const k = nowBin - i * (binSize * 1000);
      vals.push(state.rateBins.get(k) || 0);
    }
    const hi = Math.max(1, ...vals);
    sparkEl.textContent = vals.map(v =>
      BARS[Math.min(BARS.length - 1, Math.floor(v / hi * (BARS.length - 1)))]).join('');
    sparkEl.title = `5-min activity · max ${hi} msg / 10s bin`;
  }

  // ── Autocomplete ──
  // Either @ (ping) or # (pound-reference) triggers the popup. Sigil is
  // carried through so acceptance preserves the user's intent.
  function currentSigilToken() {
    const pos = input.selectionStart;
    const text = input.value.slice(0, pos);
    const atPos = text.lastIndexOf('@');
    const hashPos = text.lastIndexOf('#');
    const sigilPos = Math.max(atPos, hashPos);
    if (sigilPos < 0) return null;
    const sigil = text[sigilPos];
    if (sigilPos > 0 && !' \t,;([\n'.includes(text[sigilPos - 1])) return null;
    const frag = text.slice(sigilPos + 1);
    if (frag && !/^[A-Za-z0-9_\-]*$/.test(frag)) return null;
    return { sigilPos, sigil, fragment: frag };
  }
  function computeCompletions() {
    const tok = currentSigilToken();
    if (!tok) return { items: [], atPos: -1, sigil: '@' };
    const frag = tok.fragment.toLowerCase();
    const matches = [];
    for (const m of state.members.values()) {
      if (m.id === state.operator.id) continue;
      const nameL = (m.name || '').toLowerCase();
      if (!frag || nameL.includes(frag) || m.id.toLowerCase().startsWith(frag)) matches.push(m);
    }
    matches.sort((a, b) => {
      const an = (a.name || '').toLowerCase(), bn = (b.name || '').toLowerCase();
      const as = an.startsWith(frag) ? 0 : (frag && an.includes(frag) ? 1 : 2);
      const bs = bn.startsWith(frag) ? 0 : (frag && bn.includes(frag) ? 1 : 2);
      if (as !== bs) return as - bs;
      return an.localeCompare(bn);
    });
    return { items: matches.slice(0, 8), atPos: tok.sigilPos, sigil: tok.sigil };
  }
  function renderCompletions() {
    const { items } = state.completion;
    compEl.innerHTML = '';
    if (!state.completion.visible || items.length === 0) { compEl.classList.remove('active'); return; }
    items.forEach((m, i) => {
      const row = document.createElement('div');
      row.className = 'completion' + (i === state.completion.index ? ' selected' : '');
      const dot = document.createElement('div');
      dot.className = 'cdot dot ' + m.status;
      row.appendChild(dot);
      const anim = animalFor(m);
      const emoji = document.createElement('span');
      emoji.textContent = anim.emoji;
      emoji.style.fontSize = '14px';
      row.appendChild(emoji);
      const name = document.createElement('span');
      name.className = 'cname';
      name.textContent = (state.completion.sigil || '@') + m.name;
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
    const { items, atPos, sigil } = computeCompletions();
    state.completion.items = items;
    state.completion.atPos = atPos;
    state.completion.sigil = sigil;
    state.completion.visible = items.length > 0 && atPos >= 0;
    if (state.completion.index >= items.length) state.completion.index = 0;
    renderCompletions();
  }
  function acceptCompletion(i) {
    const { items, atPos, sigil } = state.completion;
    if (atPos < 0 || !items.length) return;
    const idx = i ?? state.completion.index;
    const m = items[idx];
    if (!m) return;
    const before = input.value.slice(0, atPos);
    const endPos = input.selectionStart;
    const after = input.value.slice(endPos);
    const repl = (sigil || '@') + (m.name || m.id) + ' ';
    input.value = before + repl + after;
    const newPos = (before + repl).length;
    input.setSelectionRange(newPos, newPos);
    state.completion.visible = false;
    renderCompletions();
    updatePreview();
  }
  function insertMention(m) {
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
  function resolveSigilTokens(text, sigil) {
    const out = [];
    const seen = new Set();
    const esc = sigil === '@' ? '@' : '#';
    const re = new RegExp(`(?<![A-Za-z0-9_])${esc}([A-Za-z0-9_\\-]+)`, 'g');
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
  function resolveMentions(text) { return resolveSigilTokens(text, '@'); }
  function resolveRefs(text)     { return resolveSigilTokens(text, '#'); }
  function updatePreview() {
    const resolved = resolveMentions(input.value);
    const refs = resolveRefs(input.value);
    const parts = [];
    if (!resolved.length) {
      parts.push('(broadcast — all connected members receive this)');
    } else {
      const names = resolved.map(m => `<span class="tgt">@${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`pings: ${names}`);
    }
    if (refs.length) {
      const rnames = refs.map(m => `<span class="tgt" style="color:#9ccf9c">#${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`refs: ${rnames}`);
    }
    preview.innerHTML = parts.join('  ·  ');
  }
  function autoResizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(160, Math.max(36, input.scrollHeight)) + 'px';
  }

  // ── Send ──
  async function sendMessage() {
    let text = input.value.trim();
    if (!text) return;
    const resolved = resolveMentions(input.value);
    const mentionIds = resolved.map(m => m.id);
    // DM mode: always include the DM target so the agent sees the message
    // (even if the operator forgot the @mention). Also prepend the visible
    // @name to the content so it's unambiguous in main-tab backscroll — the
    // composer doesn't need to show it; it's added at send time.
    if (state.dmTargetId) {
      if (!mentionIds.includes(state.dmTargetId)) mentionIds.push(state.dmTargetId);
      const tgt = state.members.get(state.dmTargetId);
      const tgtName = tgt ? tgt.name : state.dmTargetId;
      const atTag = '@' + tgtName;
      if (!text.toLowerCase().startsWith(atTag.toLowerCase())) {
        text = atTag + ' ' + text;
      }
    }
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

  // ── Filter ──
  function setFilter(q) {
    state.filter = (q || '').toLowerCase();
    filterEl.value = q || '';
    filterBanner.classList.toggle('active', !!state.filter);
    if (state.filter) filterBanner.textContent = `filter: “${q}” — click to clear`;
    applyFilterToAll();
  }
  function applyFilterToAll() {
    for (const node of chat.children) applyFilterToNode(node);
  }
  function applyFilterToNode(node) {
    if (!state.filter) { node.classList.remove('filtered-out'); return; }
    const hit = (node.dataset.search || '').includes(state.filter);
    node.classList.toggle('filtered-out', !hit);
  }
  function isRelevantInDm(m) {
    // Conversation between operator and DM target:
    //  • authored by target → must @mention operator
    //  • authored by operator → must @mention target
    //  • system notices about this target (e.g. task claims) stay visible
    if (!state.dmTargetId) return true;
    const ms = m.mentions || [];
    if (m.member_id === state.dmTargetId && ms.includes(state.operator.id)) return true;
    if (m.member_id === state.operator.id && ms.includes(state.dmTargetId)) return true;
    return false;
  }
  function applyDmFilterToNode(node, m) {
    if (!state.dmTargetId) { node.classList.remove('dm-hidden'); return; }
    node.classList.toggle('dm-hidden', !isRelevantInDm(m));
  }
  function refreshDmVisibility() {
    for (const [id, dom] of state.messageDomById) {
      const m = state.messages.get(id);
      if (m) applyDmFilterToNode(dom, m);
    }
  }
  filterEl.addEventListener('input', () => setFilter(filterEl.value));
  filterBanner.addEventListener('click', () => setFilter(''));

  // ── Compact toggle ──
  btnCompact.addEventListener('click', () => {
    state.compact = !state.compact;
    btnCompact.classList.toggle('on', state.compact);
    for (const [id, dom] of state.messageDomById) applyCompactClass(dom, id);
  });

  // ── Notify toggle ──
  btnNotify.addEventListener('click', async () => {
    if (!('Notification' in window)) {
      alert('This browser does not support desktop notifications.');
      return;
    }
    if (!state.notifyEnabled) {
      if (Notification.permission === 'default') {
        const r = await Notification.requestPermission();
        if (r !== 'granted') return;
      } else if (Notification.permission === 'denied') {
        alert('Notifications are blocked by the browser. Enable them in site settings.');
        return;
      }
      state.notifyEnabled = true;
      btnNotify.textContent = '🔔 on';
      btnNotify.classList.add('on');
    } else {
      state.notifyEnabled = false;
      btnNotify.textContent = '🔔 off';
      btnNotify.classList.remove('on');
    }
  });

  // ── Jump-to-latest + unread counter ──
  function updateJumpButton() {
    const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    if (atBottom) {
      state.jumpUnread = 0;
      jumpBtn.classList.remove('show');
      jumpCount.style.display = 'none';
    } else {
      jumpBtn.classList.add('show');
      if (state.jumpUnread > 0) {
        jumpCount.style.display = '';
        jumpCount.textContent = state.jumpUnread;
      } else {
        jumpCount.style.display = 'none';
      }
    }
  }
  // ── "You are here" indicator — operator's emoji on topmost visible
  //    message when scrolled up. Cleared when scrolled back to bottom. ──
  let hereRaf = 0;
  function scheduleHereUpdate() {
    if (hereRaf) return;
    hereRaf = requestAnimationFrame(() => {
      hereRaf = 0;
      updateHereIndicator();
    });
  }
  function updateHereIndicator() {
    // Remove any stale 'here' pins first
    for (const dom of state.messageDomById.values()) {
      const here = dom.querySelector('.watermark-pin.here');
      if (here) here.remove();
    }
    // Only show when user is scrolled up.
    const scrolledUp = chat.scrollHeight - chat.clientHeight - chat.scrollTop >= 80;
    if (!scrolledUp) return;
    if (!state.operator.id) return;

    // Find topmost message whose bottom is below the viewport top.
    const scrollTop = chat.scrollTop;
    let topDom = null;
    for (const dom of state.messageDomById.values()) {
      if (dom.classList.contains('dm-hidden') || dom.classList.contains('filtered-out')) continue;
      if (dom.offsetTop + dom.offsetHeight > scrollTop) { topDom = dom; break; }
    }
    if (!topDom) return;
    const container = topDom.querySelector('.watermark-pins');
    if (!container) return;
    const a = animalFor(state.operator);
    const pin = document.createElement('span');
    pin.className = 'watermark-pin here self';
    pin.textContent = a.emoji;
    pin.title = `you are here — the ${a.name}`;
    container.appendChild(pin);
  }
  chat.addEventListener('scroll', () => { updateJumpButton(); scheduleHereUpdate(); });
  jumpBtn.addEventListener('click', () => {
    chat.scrollTop = chat.scrollHeight;
    state.jumpUnread = 0;
    updateJumpButton();
  });

  // ── Title / tab badge ──
  function updateTitle() {
    const base = state.channel ? `trio#${state.channel}` : state.originalTitle;
    document.title = state.unreadCount > 0 ? `(${state.unreadCount}) ${base}` : base;
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      state.unreadCount = 0;
      updateTitle();
    }
  });
  window.addEventListener('focus', () => {
    state.unreadCount = 0;
    updateTitle();
  });

  // ── SSE ──
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
      } catch (e) { console.error('bad event', e); }
    };
    es.onerror = () => {
      hConn.textContent = '● reconnecting…';
      hConn.classList.remove('ok');
      hConn.classList.add('bad');
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 2000);
      }
    };
  }

  // Periodically refresh stats (queue-depth decay, rate window rolls, sparkline).
  setInterval(() => {
    updateChanStats();
    // Re-render stats for any expanded member.
    for (const id of state.expandedMembers) {
      const m = state.members.get(id);
      if (!m) continue;
      const row = [...rosterEl.querySelectorAll('.member')].find(el =>
        el.querySelector('.id')?.textContent === id.slice(0, 8));
      if (row) {
        const stats = row.querySelector('.stats');
        if (stats) stats.innerHTML = renderMemberStatsHTML(m);
      }
    }
  }, 2000);

  // ── Guest identify modal ──
  function showGuestModal(errMsg) {
    const modal = document.getElementById('guest-modal');
    const err = document.getElementById('guest-err');
    err.textContent = errMsg || '';
    modal.style.display = 'flex';
    const field = document.getElementById('guest-name');
    field.focus();
  }
  function hideGuestModal() {
    document.getElementById('guest-modal').style.display = 'none';
  }
  async function submitGuestName() {
    const field = document.getElementById('guest-name');
    const err = document.getElementById('guest-err');
    const name = (field.value || '').trim();
    if (!name) { err.textContent = 'Name is required.'; return null; }
    try {
      const r = await fetch('/api/identify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        err.textContent = data.error || 'Failed to register.';
        return null;
      }
      return data.operator;
    } catch (e) {
      err.textContent = 'Request failed: ' + e.message;
      return null;
    }
  }
  document.getElementById('guest-submit').addEventListener('click', async () => {
    const op = await submitGuestName();
    if (op) { hideGuestModal(); applyOperator(op); afterBoot(); }
  });
  document.getElementById('guest-name').addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const op = await submitGuestName();
      if (op) { hideGuestModal(); applyOperator(op); afterBoot(); }
    }
  });

  function applyOperator(op) {
    state.operator = op;
    const opAnimal = animalFor(op);
    const srcTag = op.source === 'tailscale' ? '[tailnet]' :
                   op.source === 'guest'     ? '[GUEST]'    : '';
    hMeta.textContent = `posting as ${opAnimal.emoji} ${op.name} (${op.id}) — the ${opAnimal.name} ${srcTag}  ·  ${state.server_host}`;
  }

  // ── Bootstrap ──
  async function boot() {
    try {
      const r = await fetch('/api/meta');
      const meta = await r.json();
      state.channel = meta.channel;
      state.server_host = meta.server_host;
      hChannel.textContent = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      state.originalTitle = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      if (DM_MODE) document.body.classList.add('dm-mode');
      updateTitle();
      if (meta.operator.pending) {
        // Untrusted connection — need a name before anything else
        showGuestModal();
        return;
      }
      applyOperator(meta.operator);
      afterBoot();
    } catch (e) {
      hMeta.textContent = 'bootstrap failed: ' + e.message;
    }
  }
  function afterBoot() {
    connect();
    input.focus();
    updatePreview();
    updateChanStats();
  }

  boot();
})();
</script>
</body>
</html>
"""

# One-shot substitution at import time — inject the emoji list into the JS
# so server-side animal_for() and client-side animalFor() stay in sync.
INDEX_HTML = (
    INDEX_HTML
    .replace("/*__ANIMAL_EMOJIS__*/", json.dumps([e for _, e in ANIMAL_EMOJIS]))
    .replace("/*__ANIMAL_NAMES__*/",  json.dumps([n for n, _ in ANIMAL_EMOJIS]))
)


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
