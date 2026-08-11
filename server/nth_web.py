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
import ipaddress
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
import errno
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import ANIMAL_EMOJIS, animal_for, animal_for_channel, NTH_VERSION


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
IDENTITY_SOURCE_LOOPBACK = "loopback"
IDENTITY_SOURCE_GUEST = "guest"
IDENTITY_SOURCE_PENDING = "pending"
# Agents reading the roster can check the member's summary field:
#   "human — tailnet: knelsonb"       → identity-traceable via Tailscale
#   "human — local (user: repro)"     → connected via loopback; trust level is
#                                       "already has a shell on this box"
#   "human — GUEST (self-declared)"   → untrusted self-declared identity
# Neither replaces direct hub-console input.


def _is_loopback_ip(remote_ip: str) -> bool:
    """True iff remote_ip is a loopback address (127.0.0.0/8, ::1, or an
    IPv4-mapped-IPv6 loopback like ::ffff:127.0.0.1). Uses the stdlib's
    ipaddress parser so the check rejects impostors like "::1.2.3.4" that
    a naive string prefix would accept."""
    if not remote_ip:
        return False
    # Strip IPv6 zone identifier ("fe80::1%eth0") — ipaddress refuses it.
    ip_str = remote_ip.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    # IPv4-mapped IPv6: ipaddress flags the v6 address as is_loopback=False
    # but the embedded v4 may be loopback. Unwrap and recheck.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return False


# ───────── Helpers ─────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(s: str, maxlen: int = 20) -> str:
    """Slugify for ASCII-safe handles. Returns "" on empty/no-useful-chars
    so callers can pick their own fallback via `or "xxx"`. (Used to return
    "x" on empty, which defeated every `_slug(x) or 'guest'` call site.)"""
    s = re.sub(r"[^a-z0-9_-]", "-", (s or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen]


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
        # Guests get a kebab'd handle with a `-guest` suffix so the trust
        # tag lives inside a single whitespace-free token. Earlier designs
        # stored "Bob (Guest)" which parsed correctly but invited agents
        # (and humans) to treat "(Guest)" as a parenthetical annotation
        # they could strip — which silently broke mention routing when
        # they wrote @Bob instead of @Bob (Guest).
        if self.source == IDENTITY_SOURCE_GUEST:
            return f"{_slug(self.name) or 'guest'}-guest"
        return self.name

    @property
    def summary(self) -> str:
        if self.source == IDENTITY_SOURCE_TAILSCALE:
            return f"human — tailnet: {self.login or self.name}"
        if self.source == IDENTITY_SOURCE_LOOPBACK:
            return f"human — local (user: {self.login or self.name})"
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

    def resolve_from_loopback(self, token: str, remote_ip: str) -> Optional[OperatorIdentity]:
        """If the peer came in over loopback, trust the OS account the server
        is running under. Rationale: anyone who can open a TCP connection to
        127.0.0.1 already has a shell on this box — they could write directly
        to the SQLite DB or run any skill. Asking them to self-declare a
        Guest name would be theatre. The tradeoff is that every local user
        on a shared host would get the same identity; nth is single-user on
        a personal box, so that's fine.

        Returns None for non-loopback IPs so the caller can fall through to
        the self-declared Guest path.
        """
        if not _is_loopback_ip(remote_ip):
            return None
        # Cross-platform username discovery. getpass.getuser() checks the
        # usual environment variables then falls back to pwd on POSIX; we
        # wrap it in a broad except because on weird sandboxes it can raise
        # OSError/KeyError when neither env nor pwd resolves.
        try:
            user = getpass.getuser() or "local"
        except Exception:
            user = os.environ.get("USER") or os.environ.get("USERNAME") or "local"
        display = user
        slug = _slug(user) or "local"
        ident = OperatorIdentity(
            member_id=f"{OPERATOR_MEMBER_ID_PREFIX}l_{_hostname_slug()}_{slug}",
            name=display,
            source=IDENTITY_SOURCE_LOOPBACK,
            login=user,
            created_at=time.time(),
        )
        self.put(token, ident)
        return ident

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
        # Normalise Unicode + strip controls to blunt lookalike-impersonation.
        # NFKC folds full-width ＠ / ＃ / ！ etc. into their ASCII twins so we
        # can reject them consistently; the "Cc" category filter drops zero-
        # width joiners and the like.
        name = unicodedata.normalize("NFKC", raw_name or "")
        name = "".join(c for c in name if unicodedata.category(c)[0] != "C")
        name = name.strip()[:40] or "Guest"
        lower = name.lower()
        # Reserve sigil keywords so a guest can't name themselves "all" (and
        # poison every #all / @all / !all broadcast) or spoof the operator
        # member_id prefix.
        if lower in {"all", "everyone", "here", "channel"} or lower.startswith("_op_"):
            name = f"Guest-{token[:4]}"
        slug = _slug(name) or "guest"
        # Reuse the existing guest member_id when this token already has a
        # guest identity — a re-identify is a rename, not a new member.
        # Otherwise every typo-correction would orphan a members-table row
        # and spawn a ghost in the roster.
        with self._lock:
            prior = self._by_token.get(token)
        if prior is not None and prior.source == IDENTITY_SOURCE_GUEST:
            member_id = prior.member_id
        else:
            # Disambiguate multiple guests with the same chosen name by
            # suffixing a chunk of the token — keeps their rows distinct.
            member_id = f"{OPERATOR_MEMBER_ID_PREFIX}g_{slug}_{token[:6]}"
        ident = OperatorIdentity(
            member_id=member_id,
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


_GUEST_SUFFIX_RE = re.compile(r"\s*\(\s*guest\s*\)\s*$", re.IGNORECASE)
_GUEST_KEBAB_RE = re.compile(r"[-_]guest\s*$", re.IGNORECASE)
_GUEST_PREFIX_RE = re.compile(r"^\s*guest[:\-]\s*", re.IGNORECASE)


def _guest_stem(name: str) -> Optional[str]:
    """Return the human-friendly stem of a guest-tagged name, or None.

    The sigil parser is a strict literal match — an agent who writes
    `@Gabe` when the roster has `Gabe (Guest)` would otherwise silently
    fail to route. Treating the guest tag as a trust label (not part of
    the handle) and falling back to the stem lets mentions survive that
    common mistake without the server having to guess at arbitrary
    abbreviations. Recognised shapes: ``Alice (Guest)``, ``alice-guest``,
    ``Guest: Alice``, ``Guest-Alice``."""
    if not name:
        return None
    s = name.strip()
    m = _GUEST_SUFFIX_RE.search(s)
    if m:
        stem = s[: m.start()].rstrip(" -_").strip()
        return stem or None
    m = _GUEST_KEBAB_RE.search(s)
    if m:
        stem = s[: m.start()].rstrip(" -_").strip()
        return stem or None
    m = _GUEST_PREFIX_RE.match(s)
    if m:
        stem = s[m.end():].lstrip(" -_").strip()
        return stem or None
    return None


def _parse_sigils_against_roster(
    db: sqlite3.Connection, channel: str, content: str
) -> Tuple[List[str], List[str], List[str]]:
    """Resolve @name / #name / !name against channel members.

    Mirrors the parser in nth_server.nth_send so web-operator posts carry
    the same wake semantics as MCP-agent posts. @all + !all short-circuit
    to every-member; #all has no analogue (reference-to-everyone is just
    noise). Members named literally 'all' are skipped so they don't
    double-count against the keyword shortcuts.

    Belt-and-suspenders: after the literal-match pass, a second pass tries
    the "guest stem" of each guest-tagged member (so @Gabe still reaches
    @Gabe (Guest)). The fallback is skipped when the stem collides with
    another member's literal name (real identity wins) or when two guests
    share a stem (ambiguity — force the agent to type the literal).
    """
    members = db.execute(
        "SELECT id, name FROM members WHERE channel = ?",
        (channel,),
    ).fetchall()
    lowered = content.lower()
    all_ids = [m["id"] for m in members]
    at_all   = re.search(r"@all(?:\b|$)", lowered) is not None
    bang_all = re.search(r"!all(?:\b|$)", lowered) is not None
    mention_ids: List[str] = list(all_ids) if at_all   else []
    bang_ids:    List[str] = list(all_ids) if bang_all else []
    ref_ids:     List[str] = []
    # Track which members we hit literally and which names were already
    # claimed, so the guest-stem pass doesn't shadow a real identity.
    hit_at: set = set()
    hit_ref: set = set()
    hit_bang: set = set()
    literal_names_lower: set = set()
    for m in members:
        name = (m["name"] or "").strip()
        mid = m["id"]
        # Direct-id mention path: @<member_id> always routes, independent of
        # name. Agents that cache the id from trio_connect survive renames.
        id_esc = re.escape(mid)
        if not at_all:
            if re.search(r"@" + id_esc + r"(?:\b|$)", content, re.IGNORECASE):
                if mid not in hit_at:
                    mention_ids.append(mid)
                    hit_at.add(mid)
        if re.search(r"#" + id_esc + r"(?:\b|$)", content, re.IGNORECASE):
            if mid not in hit_ref:
                ref_ids.append(mid)
                hit_ref.add(mid)
        if not bang_all:
            if re.search(r"!" + id_esc + r"(?:\b|$)", content, re.IGNORECASE):
                if mid not in hit_bang:
                    bang_ids.append(mid)
                    hit_bang.add(mid)
        if name.lower() == "all" or not name:
            continue
        literal_names_lower.add(name.lower())
        name_esc = re.escape(name)
        if not at_all and mid not in hit_at:
            if re.search(r"@" + name_esc + r"(?:\b|$)", content, re.IGNORECASE):
                mention_ids.append(mid)
                hit_at.add(mid)
        if mid not in hit_ref:
            if re.search(r"#" + name_esc + r"(?:\b|$)", content, re.IGNORECASE):
                ref_ids.append(mid)
                hit_ref.add(mid)
        if not bang_all and mid not in hit_bang:
            if re.search(r"!" + name_esc + r"(?:\b|$)", content, re.IGNORECASE):
                bang_ids.append(mid)
                hit_bang.add(mid)

    # Guest-stem fallback. Group guest members by stem so we can detect
    # ambiguity (two guests named "Gabe (Guest)" / "gabe-guest" would both
    # want @Gabe — skip both rather than broadcast silently).
    guest_by_stem: Dict[str, List[sqlite3.Row]] = {}
    for m in members:
        stem = _guest_stem(m["name"] or "")
        if not stem:
            continue
        guest_by_stem.setdefault(stem.lower(), []).append(m)
    _RESERVED_STEMS = {"all", "everyone", "here", "channel"}
    for stem_lower, guests in guest_by_stem.items():
        if stem_lower in _RESERVED_STEMS:
            continue  # never let a stem fight the @all/!all broadcast shortcut
        if stem_lower in literal_names_lower:
            continue  # a real member already owns this name
        if len(guests) != 1:
            continue  # ambiguous — multiple guests share a stem
        g = guests[0]
        stem = _guest_stem(g["name"] or "") or ""
        if not stem:
            continue
        stem_esc = re.escape(stem)
        gid = g["id"]
        if not at_all and gid not in hit_at:
            if re.search(r"@" + stem_esc + r"(?:\b|$)", content, re.IGNORECASE):
                mention_ids.append(gid)
        if gid not in hit_ref:
            if re.search(r"#" + stem_esc + r"(?:\b|$)", content, re.IGNORECASE):
                ref_ids.append(gid)
        if not bang_all and gid not in hit_bang:
            if re.search(r"!" + stem_esc + r"(?:\b|$)", content, re.IGNORECASE):
                bang_ids.append(gid)
    return mention_ids, ref_ids, bang_ids


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
        # try/finally so queue.Full or a transient sqlite error doesn't leak
        # the connection. A leaked read connection holds a SHARED lock and,
        # worse, if Python's default isolation_level has auto-BEGUN any write,
        # holds the WAL writer lock until GC — which starved the monitor's
        # 0.5s polls below busy_timeout under contention.
        db = None
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=2000")
            members = self._fetch_roster(db)
            q.put_nowait(json.dumps({"type": "roster", "members": members}))
            q.put_nowait(json.dumps(
                {"type": "context", "sessions": _read_context_snapshots()}))
            rows = db.execute(
                "SELECT id, member_id, member_name, content, mentions, refs, bangs, created_at "
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
                    "bangs": parse_mentions_json(r["bangs"] if "bangs" in r.keys() else ""),
                    "created_at": r["created_at"],
                }))
        except (sqlite3.Error, queue.Full):
            pass
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
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
        # filter_mode (v7.2) is best-effort; older schemas fall back to 'all'.
        try:
            rows = db.execute(
                "SELECT m.id AS id, m.name AS name, m.status_text AS status_text, "
                "m.last_seen AS member_last_seen, m.last_read AS member_last_read, "
                "m.messenger_heartbeat AS messenger_heartbeat, "
                "m.watchdog_heartbeat AS watchdog_heartbeat, "
                "m.filter_mode AS filter_mode, "
                "m.context_json AS context_json, "
                "COALESCE(MAX(s.last_read), 0) AS session_last_read, "
                "MAX(s.last_seen) AS session_last_seen, "
                "GROUP_CONCAT(s.fingerprint) AS fingerprints "
                "FROM members m "
                "LEFT JOIN sessions s "
                "  ON s.channel = m.channel AND s.member_id = m.id "
                "  AND s.revoked_at IS NULL "
                "WHERE m.channel = ? "
                "GROUP BY m.id, m.channel "
                "ORDER BY m.joined_at",
                (self.channel,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute(
                "SELECT m.id AS id, m.name AS name, m.status_text AS status_text, "
                "m.last_seen AS member_last_seen, m.last_read AS member_last_read, "
                "m.messenger_heartbeat AS messenger_heartbeat, "
                "m.watchdog_heartbeat AS watchdog_heartbeat, "
                "COALESCE(MAX(s.last_read), 0) AS session_last_read, "
                "MAX(s.last_seen) AS session_last_seen, "
                "GROUP_CONCAT(s.fingerprint) AS fingerprints "
                "FROM members m "
                "LEFT JOIN sessions s "
                "  ON s.channel = m.channel AND s.member_id = m.id "
                "  AND s.revoked_at IS NULL "
                "WHERE m.channel = ? "
                "GROUP BY m.id, m.channel "
                "ORDER BY m.joined_at",
                (self.channel,),
            ).fetchall()
        # Collision-free avatars per channel. Sorted-id assignment in
        # animal_for_channel() makes the mapping stable across roster
        # refreshes as long as the member set is fixed; joins/leaves
        # may reshuffle affected members, which the client handles by
        # keying on the emoji/name fields we ship instead of hashing.
        avatars = animal_for_channel([r["id"] for r in rows])
        ctx_usage = _read_context_usage()
        out = []
        for r in rows:
            effective_last_read = max(
                r["member_last_read"] or 0,
                r["session_last_read"] or 0,
            )
            m_ls = r["member_last_seen"] or ""
            s_ls = r["session_last_seen"] or ""
            effective_last_seen = max(m_ls, s_ls) or None
            fm = r["filter_mode"] if "filter_mode" in r.keys() else "all"
            # Context %: match any of the member's session fingerprints
            # (CLAUDE_SESSION_IDs) against the statusline publisher files.
            context_pct = None
            context_full = None
            raw_ctx = r["context_json"] if "context_json" in r.keys() else None
            if raw_ctx:
                try:
                    cand = json.loads(raw_ctx)
                    relayed = cand.get("_relayed_at")
                    if relayed and (datetime.now(timezone.utc)
                                    - datetime.fromisoformat(relayed)
                                    ).total_seconds() < 120                             and isinstance(cand.get("used_pct"), (int, float)):
                        context_full = cand
                        context_pct = float(cand["used_pct"])
                except (ValueError, TypeError):
                    pass
            fps = r["fingerprints"] if "fingerprints" in r.keys() else None
            if context_full is None and fps and ctx_usage:
                for fp in str(fps).split(","):
                    if fp in ctx_usage:
                        context_full = ctx_usage[fp]
                        context_pct = float(context_full["used_pct"])
                        break
            aname, aemoji = avatars.get(r["id"], animal_for(r["id"]))
            out.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "status_text": r["status_text"] or "",
                "last_seen": effective_last_seen,
                "last_read": effective_last_read,
                "filter_mode": fm or "all",
                "status": member_status(effective_last_seen, r["status_text"] or ""),
                "context_pct": context_pct,
                "context": context_full,
                "animal_name": aname,
                "animal_emoji": aemoji,
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
        except sqlite3.Error as e:
            sys.stderr.write(f"[nth_web] DB open failed: {e}\n")
            return

        try:
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

            while not self._stop.is_set():
                try:
                    rows = db.execute(
                        "SELECT id, member_id, member_name, content, mentions, refs, bangs, created_at "
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
                            "bangs": parse_mentions_json(r["bangs"] if "bangs" in r.keys() else ""),
                            "created_at": r["created_at"],
                        })
                        self.last_msg_id = r["id"]

                    members = self._fetch_roster(db)
                    snapshot = json.dumps(members, sort_keys=True)
                    if snapshot != self._last_roster_snapshot:
                        self._last_roster_snapshot = snapshot
                        self._broadcast({"type": "roster", "members": members})

                    # Context rings: cheap (few tiny local files); broadcast
                    # only when the payload actually changed.
                    ctx_sessions = _read_context_snapshots()
                    ctx_snapshot = json.dumps(ctx_sessions, sort_keys=True)
                    if ctx_snapshot != getattr(self, "_last_context_snapshot", None):
                        self._last_context_snapshot = ctx_snapshot
                        self._broadcast({"type": "context", "sessions": ctx_sessions})

                except sqlite3.Error as e:
                    sys.stderr.write(f"[nth_web] poll error: {e}\n")

                self._stop.wait(DB_POLL_INTERVAL)
        finally:
            # Always close, even on unexpected thread exit. A leaked
            # connection would keep holding any in-flight read lock (and
            # under default isolation_level, any implicit BEGIN) for the
            # rest of the process lifetime.
            try:
                db.close()
            except sqlite3.Error:
                pass


# ───────── Per-session context usage (statusline publisher) ─────────
# The operator's statusline tee (claude-statusline repo) writes one JSON per
# live Claude session to this directory on every render. Sessions register
# their CLAUDE_SESSION_ID as sessions.fingerprint on connect, which is the
# join key. Only sessions on THIS machine appear — a hub-hosted nth_web
# cannot see spoke-side context files (the fleet answer is status_text
# publishing, not this).
CONTEXT_USAGE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
) / "claude-context"
CONTEXT_USAGE_STALE_S = 60


CONTEXT_SNAPSHOT_STALE_S = 120


def _read_context_snapshots() -> List[Dict[str, Any]]:
    """All fresh publisher files as dicts (plus _age_s), newest first.
    Stale >120s ignored; the UI additionally dims entries older than 30s."""
    out: List[Dict[str, Any]] = []
    try:
        now = time.time()
        for p in CONTEXT_USAGE_DIR.glob("*.json"):
            try:
                age = now - p.stat().st_mtime
                if age > CONTEXT_SNAPSHOT_STALE_S:
                    continue
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data.get("session_id"), str):
                    continue
                data["_age_s"] = int(age)
                out.append(data)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    out.sort(key=lambda d: d["_age_s"])
    return out


def _read_context_usage() -> Dict[str, Dict[str, Any]]:
    """{claude_session_id: full snapshot dict} for fresh (<60s) files."""
    return {
        d["session_id"]: d
        for d in _read_context_snapshots()
        if d["_age_s"] <= CONTEXT_USAGE_STALE_S
        and isinstance(d.get("used_pct"), (int, float))
    }


# ───────── Landing snapshot ─────────
def _landing_snapshot(db_path: Path) -> Dict[str, Any]:
    """Everything the landing page needs in one JSON read: DB health, node
    check-ins, per-channel liveness. Counts, names, and ages only — the
    landing page never ships message content."""
    now = datetime.now(timezone.utc)

    def age_s(iso: Optional[str]) -> Optional[int]:
        if not iso:
            return None
        try:
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0, int((now - ts).total_seconds()))
        except ValueError:
            return None

    out: Dict[str, Any] = {
        "version": NTH_VERSION,
        "host": socket.gethostname(),
        "db": str(db_path),
        "db_ok": False,
        "time": now.isoformat(),
        "nodes": [],
        "channels": [],
    }
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        out["error"] = type(e).__name__
        return out
    try:
        try:
            for r in db.execute(
                    "SELECT hostname, transport, nth_version, python, last_seen "
                    "FROM nodes ORDER BY last_seen DESC"):
                a = age_s(r["last_seen"])
                out["nodes"].append({
                    "hostname": r["hostname"], "transport": r["transport"],
                    "nth_version": r["nth_version"], "python": r["python"],
                    "age_s": a, "live": a is not None and a < STALE_SECONDS,
                })
        except sqlite3.OperationalError:
            pass  # pre-v7.3 DB: no nodes table yet

        for ch in db.execute(
                "SELECT code, status FROM channels ORDER BY code").fetchall():
            hbs = [m["messenger_heartbeat"] for m in db.execute(
                "SELECT messenger_heartbeat FROM members WHERE channel = ?",
                (ch["code"],)).fetchall()]
            live = sum(1 for hb in hbs
                       if (a := age_s(hb)) is not None and a < STALE_SECONDS)
            msgs, last_msg = db.execute(
                "SELECT COUNT(*), MAX(created_at) FROM messages WHERE channel = ?",
                (ch["code"],)).fetchone()
            out["channels"].append({
                "code": ch["code"], "status": ch["status"],
                "members": len(hbs), "live": live, "msgs": msgs,
                "last_msg_age_s": age_s(last_msg),
            })
        out["context_sessions"] = _read_context_snapshots()
        out["channels"].sort(
            key=lambda c: (c["status"] != "active",
                           c["last_msg_age_s"] if c["last_msg_age_s"] is not None
                           else float("inf")))
        out["db_ok"] = True
    except sqlite3.Error as e:
        out["error"] = type(e).__name__
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    return out


# ───────── HTTP handler ─────────
CHANNEL_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,31}$")


class NthWebHandler(BaseHTTPRequestHandler):
    # Populated in main()
    hub: Optional[EventHub] = None
    channel: str = ""
    db_path: Path = DB_PATH
    # Landing mode (no channel argument): / serves the fleet/channel index,
    # /c/<code> serves the per-channel app, and API requests carry their
    # channel in a ?channel= query param. EventHubs are created lazily, one
    # per channel viewed, and poll for the life of the process.
    landing_mode: bool = False
    hubs: Dict[str, EventHub] = {}
    hubs_lock = threading.Lock()

    def _channel_for_request(self, parsed) -> Optional[str]:
        """Channel an API request addresses. None = missing/invalid."""
        if not self.landing_mode:
            return self.channel
        code = (parse_qs(parsed.query).get("channel") or [""])[0]
        if not CHANNEL_CODE_RE.match(code or ""):
            return None
        return code

    def _hub_for_channel(self, code: str) -> EventHub:
        if not self.landing_mode:
            assert self.hub is not None
            return self.hub
        cls = NthWebHandler
        with cls.hubs_lock:
            hub = cls.hubs.get(code)
            if hub is None:
                hub = EventHub(self.db_path, code)
                hub.start()
                cls.hubs[code] = hub
            return hub

    def _channel_exists(self, code: str) -> bool:
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
            try:
                return db.execute(
                    "SELECT 1 FROM channels WHERE code = ?", (code,)
                ).fetchone() is not None
            finally:
                db.close()
        except sqlite3.Error:
            return False

    # Suppress default noisy logging
    def log_message(self, fmt: str, *args) -> None:
        # Comment out if you want request logs.
        pass

    # ── identity ──
    def _client_ip(self) -> str:
        """Remote IP of the direct TCP peer.

        We DO NOT honour X-Forwarded-For here. nth_web is designed to be
        served directly over Tailscale — no reverse proxy sits in front —
        so any XFF header we see was attacker-controlled. Trusting it would
        let a direct client send `X-Forwarded-For: 100.x.y.z` to have
        `tailscale_whois()` resolve them as that tailnet peer, spoofing a
        trusted `source=tailscale` identity. If a reverse-proxied deployment
        ever becomes a real use case, add an explicit TRUSTED_PROXY_CIDRS
        allowlist gated on `self.client_address[0]` before re-enabling XFF.
        """
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
        """Resolve (token, identity, is_new_cookie). Trust ladder:
        Tailscale whois → loopback-OS-user → pending (browser must POST
        /api/identify to self-declare a Guest name).
        """
        token, is_new = self._get_or_mint_cookie()
        ident = OPERATOR_REGISTRY.get(token)
        if ident is not None:
            return token, ident, is_new
        remote_ip = self._client_ip()
        # Try Tailscale whois on the remote address
        ident = OPERATOR_REGISTRY.resolve_from_tailscale(token, remote_ip)
        if ident is not None:
            return token, ident, is_new
        # Loopback: peer is already on the machine, trust the OS user
        ident = OPERATOR_REGISTRY.resolve_from_loopback(token, remote_ip)
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
            body = LANDING_HTML if self.landing_mode else INDEX_HTML
            self._serve_html(body, set_cookie_token=token if is_new else None)
        elif self.landing_mode and path.startswith("/c/"):
            code = path[3:].rstrip("/")
            if not CHANNEL_CODE_RE.match(code):
                self._error(404, "bad channel code")
                return
            if not self._channel_exists(code):
                self._error(404, f"no such channel: {code}")
                return
            token, _ident, is_new = self._resolve_identity()
            # The channel code passed CHANNEL_CODE_RE, so this substitution
            # cannot inject into the script context.
            body = INDEX_HTML.replace(
                "/*__API_QS__*/''", json.dumps(f"?channel={code}"))
            self._serve_html(body, set_cookie_token=token if is_new else None)
        elif self.landing_mode and path == "/api/landing":
            self._json(_landing_snapshot(self.db_path))
        elif path == "/api/meta":
            ch = self._channel_for_request(parsed)
            if ch is None:
                self._error(400, "channel query param required")
                return
            token, ident, is_new = self._resolve_identity()
            self._json({
                "channel": ch,
                "operator": {
                    "id": ident.member_id,
                    "name": ident.display_name,
                    "source": ident.source,
                    "pending": ident.source == IDENTITY_SOURCE_PENDING,
                },
                "server_host": socket.gethostname(),
            }, set_cookie_token=token if is_new else None)
        elif path == "/api/events":
            ch = self._channel_for_request(parsed)
            if ch is None:
                self._error(400, "channel query param required")
                return
            self._serve_sse(self._hub_for_channel(ch))
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

    def _serve_sse(self, hub: EventHub) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = hub.subscribe()
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
            hub.unsubscribe(q)

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
        if existing and existing.source in (IDENTITY_SOURCE_TAILSCALE, IDENTITY_SOURCE_LOOPBACK):
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
        send_channel = self._channel_for_request(urlparse(self.path))
        if send_channel is None:
            self._error(400, "channel query param required")
            return
        if self.landing_mode and not self._channel_exists(send_channel):
            self._error(404, f"no such channel: {send_channel}")
            return
        body = self._read_json_body()
        if body is None:
            return

        content = (body.get("content") or "").strip()
        if not content:
            self._error(400, "empty content")
            return
        if len(content) > 4000:
            self._error(400, "content too long (max 4000 chars)")
            return

        token, ident, _is_new = self._resolve_identity()
        if ident.source == IDENTITY_SOURCE_PENDING:
            self._error(403, "identity required — POST /api/identify first")
            return

        db = None
        try:
            # isolation_level=None puts the connection in autocommit mode —
            # we wrap the send in an explicit BEGIN/COMMIT transaction below.
            # With the default isolation_level, any sqlite3.Error between the
            # first DML and commit() leaves the connection holding the WAL
            # writer lock until close(); the finally clause below is the only
            # thing that reliably returned us to a releasable state.
            db = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            try:
                op_id, op_name = ensure_operator_row(db, send_channel, ident)
                now = now_iso()

                # Leading "$task " marks this as a claimable task — same
                # table + status flow as trio_send(task=True). The prefix
                # is stripped from the task description, and the posted
                # message is rewritten to "[task #N] …" so readers see the
                # same shape as MCP-originated tasks. blocked_by is not
                # supported from the web UI for now.
                is_task = False
                task_body = content
                if content.startswith("$task "):
                    is_task = True
                    task_body = content[len("$task "):].strip()
                    if not task_body:
                        self._error(400, "empty task body")
                        db.execute("ROLLBACK")
                        return

                posted_content = content
                if is_task:
                    tcur = db.execute(
                        "INSERT INTO tasks (channel, posted_by, status, description, "
                        " blocked_by, created_at, updated_at) "
                        "VALUES (?, ?, 'open', ?, '[]', ?, ?)",
                        (send_channel, op_id, task_body, now, now),
                    )
                    task_id = tcur.lastrowid
                    posted_content = f"[task #{task_id}] {task_body}"

                # Server-side parse the three sigils against the current roster,
                # matching nth_send's behavior so web-operator posts carry the
                # same wake semantics as MCP-agent posts.
                mention_ids, ref_ids, bang_ids = _parse_sigils_against_roster(
                    db, send_channel, posted_content
                )
                cursor = db.execute(
                    "INSERT INTO messages "
                    "(channel, member_id, member_name, content, created_at, "
                    " mentions, refs, bangs) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (send_channel, op_id, op_name, posted_content, now,
                     json.dumps(mention_ids) if mention_ids else "",
                     json.dumps(ref_ids)     if ref_ids     else "",
                     json.dumps(bang_ids)    if bang_ids    else ""),
                )
                msg_id = cursor.lastrowid
                db.execute(
                    "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                    (now, send_channel, op_id),
                )
                db.execute("COMMIT")
            except sqlite3.Error:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            self._error(500, f"db error: {e}")
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass

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
    /* ── Midnight (default dark) ── */
    --bg: #0b0f14; --bg2: #121821; --panel: #161d27; --border: #273040;
    --fg: #d8dde6; --dim: #7a8596; --dimmer: #4a5262;
    --accent: #3ba0e6; --accent-hi: #50b0f0; --accent2: #59cb79;
    --warn: #e3c34c; --err: #e56a4a; --mention: #e3c34c;
    --hover: #0f1420; --ov: 255,255,255;
    --card-radius: 3px; --card-shadow: none;
    --pill-radius: 3px; --input-radius: 4px;
  }
  :root[data-theme="light"] {
    /* ── Daylight (light) ── */
    --bg: #f6f7f9; --bg2: #eceef2; --panel: #e2e6ec; --border: #c8cfd8;
    --fg: #1c2430; --dim: #5a6675; --dimmer: #9aa4b2;
    --accent: #1f7fd0; --accent-hi: #2b93e6; --accent2: #2e9e52;
    --warn: #b8860b; --err: #cc4a2c; --mention: #b8860b;
    --hover: #dce1e8; --ov: 0,0,0;
  }
  :root[data-theme="nord"] {
    /* ── Nord (dark) ── */
    --bg: #2e3440; --bg2: #2b303b; --panel: #3b4252; --border: #434c5e;
    --fg: #e5e9f0; --dim: #8f9bb3; --dimmer: #616e88;
    --accent: #88c0d0; --accent-hi: #8fbcbb; --accent2: #a3be8c;
    --warn: #ebcb8b; --err: #bf616a; --mention: #ebcb8b;
    --hover: #353c4a; --ov: 255,255,255;
  }
  :root[data-theme="dracula"] {
    /* ── Dracula (dark) ── */
    --bg: #282a36; --bg2: #21222c; --panel: #343746; --border: #44475a;
    --fg: #f8f8f2; --dim: #a0a3b1; --dimmer: #6272a4;
    --accent: #bd93f9; --accent-hi: #caa9fa; --accent2: #50fa7b;
    --warn: #f1fa8c; --err: #ff5555; --mention: #ffb86c;
    --hover: #313442; --ov: 255,255,255;
  }
  :root[data-theme="pve-dark"] {
    /* ── Proxmox VE Dark (from theme-proxmox-dark.css) ── */
    --bg: #1a1a1a; --bg2: #262626; --panel: #333; --border: #404040;
    --fg: #f2f2f2; --dim: #999; --dimmer: #666;
    --accent: #4db5ff; --accent-hi: #99d5ff; --accent2: #0060a4;
    --warn: #ffae0b; --err: #ce3c3c; --mention: #ffae0b;
    --hover: #595959; --ov: 255,255,255;
    --card-radius: 2px; --card-shadow: 0 1px 5px rgba(0,0,0,0.5);
    --pill-radius: 2px; --input-radius: 2px;
  }
  :root[data-theme="pve-light"] {
    /* ── Proxmox VE Light (from ext6-pve.css + gauge defaults) ── */
    --bg: #f5f5f5; --bg2: #e2eff9; --panel: #fff; --border: #cfcfcf;
    --fg: #000; --dim: #555; --dimmer: #a8a8a8;
    --accent: #3892d4; --accent-hi: #4db5ff; --accent2: #21bf4b;
    --warn: #cc8e00; --err: #cc1800; --mention: #cc8e00;
    --hover: #e2eff9; --ov: 0,0,0;
    --card-radius: 2px; --card-shadow: 0 1px 8px rgba(136,136,136,0.3);
    --pill-radius: 2px; --input-radius: 2px;
  }
  :root[data-theme="solarized"] {
    /* ── Solarized Dark (PVE Dashboard) ── */
    --bg: #002b36; --bg2: #00212b; --panel: #073642; --border: rgba(147,161,161,.2);
    --fg: #eee8d5; --dim: #93a1a1; --dimmer: #6c7c7c;
    --accent: #268bd2; --accent-hi: #3a9bde; --accent2: #859900;
    --warn: #b58900; --err: #dc322f; --mention: #b58900;
    --hover: #0a4453; --ov: 255,255,255;
    --card-radius: 6px; --card-shadow: 0 1px 4px rgba(0,0,0,.4); --pill-radius: 4px;
  }
  :root[data-theme="bluebubble"] {
    /* ── Walled Garden (light) ── */
    --bg: #fff; --bg2: #f2f2f7; --panel: #fff; --border: #c6c6c8;
    --fg: #1c1c1e; --dim: #8e8e93; --dimmer: #c7c7cc;
    --accent: #007aff; --accent-hi: #409cff; --accent2: #34c759;
    --warn: #ff9500; --err: #ff3b30; --mention: #ff9500;
    --hover: #e5e5ea; --ov: 0,0,0;
    --card-radius: 18px; --card-shadow: none;
    --pill-radius: 999px; --input-radius: 18px;
    --bubble-mine: #007aff; --bubble-mine-ink: #fff;
    --bubble-theirs: #e5e5ea; --bubble-theirs-ink: #000;
    --bubble-system: transparent;
  }
  /* ── Walled Garden: pixel-faithful recreation ── */
  :root[data-theme="bluebubble"] .msg {
    max-width: 70%; border-left: none; margin-left: 0; margin-bottom: 2px;
    padding: 6px 12px 8px; border-radius: 18px; position: relative;
    background: var(--bubble-theirs) !important; color: var(--bubble-theirs-ink);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
      "Helvetica", "Arial", sans-serif;
    font-size: 17px; line-height: 1.28; letter-spacing: -0.01em;
  }
  :root[data-theme="bluebubble"] .msg:hover { filter: brightness(0.97); }
  :root[data-theme="bluebubble"] .msg:not(.mine) { border-bottom-left-radius: 4px; }
  /* Tail — CSS triangle on the last bubble in a run */
  :root[data-theme="bluebubble"] .msg:not(.mine)::before {
    content: ""; position: absolute; bottom: 0; left: -6px;
    width: 12px; height: 16px;
    background: radial-gradient(ellipse at top right, var(--bubble-theirs) 55%, transparent 56%);
  }
  :root[data-theme="bluebubble"] .msg.mine::before {
    content: ""; position: absolute; bottom: 0; right: -6px; left: auto;
    width: 12px; height: 16px;
    background: radial-gradient(ellipse at top left, var(--bubble-mine) 55%, transparent 56%);
  }
  :root[data-theme="bluebubble"] .msg .head {
    font-size: 11px; color: #86868b; margin-bottom: 1px;
    font-weight: 400;
  }
  :root[data-theme="bluebubble"] .msg .head .time { color: #86868b; }
  :root[data-theme="bluebubble"] .msg .author { font-weight: 600; color: #1c1c1e; font-size: 13px; }
  :root[data-theme="bluebubble"] .msg .body { color: inherit; }
  :root[data-theme="bluebubble"] .msg .body.plain { white-space: pre-wrap; }
  :root[data-theme="bluebubble"] .msg.mine {
    margin-left: auto; background: var(--bubble-mine) !important;
    color: var(--bubble-mine-ink); border-bottom-right-radius: 4px;
    border-bottom-left-radius: 18px;
  }
  :root[data-theme="bluebubble"] .msg.mine .head { color: rgba(255,255,255,0.65); }
  :root[data-theme="bluebubble"] .msg.mine .head .time { color: rgba(255,255,255,0.5); }
  :root[data-theme="bluebubble"] .msg.mine .author { color: rgba(255,255,255,0.85); }
  :root[data-theme="bluebubble"] .msg.system {
    max-width: 100%; text-align: center; border-radius: 10px;
    background: transparent !important; color: #86868b; font-style: normal;
    font-size: 13px; padding: 4px 14px; font-weight: 400;
  }
  :root[data-theme="bluebubble"] .msg.system::before { display: none; }
  :root[data-theme="bluebubble"] .msg .mentions-bar .mchip,
  :root[data-theme="bluebubble"] .msg .refs-bar .mchip {
    background: rgba(0,0,0,0.07); border: none; color: #007aff;
    font-weight: 500; border-radius: 10px; font-size: 13px;
  }
  :root[data-theme="bluebubble"] .msg.mine .mentions-bar .mchip,
  :root[data-theme="bluebubble"] .msg.mine .refs-bar .mchip {
    background: rgba(255,255,255,0.2); border: none; color: #fff;
  }
  :root[data-theme="bluebubble"] .msg .bangs-bar .mchip {
    background: rgba(255,59,48,0.12); border: none; color: #ff3b30;
  }
  :root[data-theme="bluebubble"] .msg .body code.mdic {
    background: rgba(0,0,0,0.06); border: none; border-radius: 4px;
    font-size: 0.9em;
  }
  :root[data-theme="bluebubble"] .msg.mine .body code.mdic {
    background: rgba(255,255,255,0.18); border: none;
  }
  :root[data-theme="bluebubble"] .msg .body pre.mdcode {
    background: rgba(0,0,0,0.04); border: none; border-radius: 10px;
    padding: 8px 12px;
  }
  :root[data-theme="bluebubble"] .msg.mine .body pre.mdcode {
    background: rgba(255,255,255,0.12); border: none;
  }
  :root[data-theme="bluebubble"] .msg .body a { color: #007aff; text-decoration: none; }
  :root[data-theme="bluebubble"] .msg.mine .body a { color: #fff; text-decoration: underline; }
  :root[data-theme="bluebubble"] .msg.targeted {
    border-left: none; box-shadow: 0 0 0 2px rgba(255,149,0,0.4);
    border-radius: 18px;
  }
  /* Header — frosted glass nav bar */
  :root[data-theme="bluebubble"] header {
    background: rgba(249,249,249,0.94); border-bottom: 0.5px solid rgba(0,0,0,0.12);
    backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px);
  }
  :root[data-theme="bluebubble"] header .title { color: #007aff; font-size: 17px; }
  :root[data-theme="bluebubble"] header .meta { color: #86868b; }
  :root[data-theme="bluebubble"] header .pill { border: none;
    background: rgba(0,122,255,0.12); color: #007aff; font-weight: 500; }
  :root[data-theme="bluebubble"] header .pill:hover { background: rgba(0,122,255,0.2); }
  :root[data-theme="bluebubble"] header .pill.on { background: #007aff; color: #fff; }
  :root[data-theme="bluebubble"] header .pill.conn.ok { color: #34c759; background: rgba(52,199,89,0.12); }
  :root[data-theme="bluebubble"] header .pill.conn.bad { color: #ff3b30; background: rgba(255,59,48,0.12); }
  /* Sidebar */
  :root[data-theme="bluebubble"] #side {
    background: #f2f2f7; border-left: 0.5px solid rgba(0,0,0,0.12);
  }
  :root[data-theme="bluebubble"] #side h2 { color: #86868b; font-size: 13px;
    text-transform: uppercase; letter-spacing: 0.02em; }
  :root[data-theme="bluebubble"] .member .name { font-size: 15px; }
  :root[data-theme="bluebubble"] .member .stext { font-size: 13px; color: #86868b; }
  :root[data-theme="bluebubble"] .member .dot { width: 10px; height: 10px; }
  :root[data-theme="bluebubble"] .member + .member { border-top: 0.5px solid rgba(0,0,0,0.1); }
  :root[data-theme="bluebubble"] .member .dm-btn {
    background: rgba(0,122,255,0.12); color: #007aff; border: none; border-radius: 14px;
  }
  /* Composer — iOS keyboard area feel */
  :root[data-theme="bluebubble"] #composer {
    background: #f2f2f7; border-top: 0.5px solid rgba(0,0,0,0.12); padding: 8px 10px;
  }
  :root[data-theme="bluebubble"] #input {
    background: #fff; border: 0.5px solid #c6c6c8; border-radius: 18px;
    padding: 8px 14px; font-size: 17px; line-height: 1.28;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  }
  :root[data-theme="bluebubble"] #input:focus { border-color: #007aff; }
  :root[data-theme="bluebubble"] #send-btn {
    border-radius: 50%; width: 34px; height: 34px; padding: 0;
    font-size: 0; background: #007aff; position: relative;
  }
  :root[data-theme="bluebubble"] #send-btn::after {
    content: "\2191"; font-size: 20px; font-weight: 700; color: #fff;
  }
  :root[data-theme="bluebubble"] #send-btn:disabled { background: #c7c7cc; }
  :root[data-theme="bluebubble"] #hint { display: none; }
  :root[data-theme="bluebubble"] #preview { font-size: 13px; color: #86868b; }
  :root[data-theme="bluebubble"] #target-bar .tb-pill {
    border: none; background: rgba(0,122,255,0.1); color: #007aff;
    border-radius: 14px; font-weight: 500;
  }
  :root[data-theme="bluebubble"] #target-bar .tb-pill.on {
    background: #007aff; color: #fff;
  }
  /* Completions dropdown */
  :root[data-theme="bluebubble"] #completions {
    border-radius: 14px; border: none; box-shadow: 0 4px 24px rgba(0,0,0,0.15);
    background: rgba(255,255,255,0.98); backdrop-filter: blur(20px);
  }
  :root[data-theme="bluebubble"] .completion:hover,
  :root[data-theme="bluebubble"] .completion.selected { background: #e5e5ea; }
  /* Settings panel */
  :root[data-theme="bluebubble"] #settings-panel {
    border-radius: 14px; border: none; box-shadow: 0 4px 24px rgba(0,0,0,0.15);
    background: rgba(255,255,255,0.98); backdrop-filter: blur(20px);
  }
  /* Guest modal */
  :root[data-theme="bluebubble"] #guest-modal .guest-card {
    border-radius: 14px; border: none; box-shadow: 0 4px 30px rgba(0,0,0,0.2);
    background: #fff;
  }
  :root[data-theme="bluebubble"] #guest-modal button {
    border-radius: 14px; background: #007aff; font-weight: 600;
  }
  /* Hide noise — clean like the garden */
  :root[data-theme="bluebubble"] .acks { display: none; }
  :root[data-theme="bluebubble"] .watermark-pins { display: none; }
  :root[data-theme="bluebubble"] #jump-btn {
    border-radius: 999px; background: #007aff; box-shadow: 0 2px 12px rgba(0,122,255,0.3);
  }
  :root[data-theme="bluebubble"] #chat {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
      "Helvetica Neue", "Helvetica", "Arial", sans-serif;
    background: #fff;
  }

  :root[data-theme="win31"] {
    /* ── Windows 3.1 (PVE Dashboard) ── */
    --bg: #008080; --bg2: #008080; --panel: #c0c0c0; --border: #808080;
    --fg: #000; --dim: #404040; --dimmer: #808080;
    --accent: #E57000; --accent-hi: #ff8c3a; --accent2: #008000;
    --warn: #808000; --err: #800000; --mention: #808000;
    --hover: #d0d0d0; --ov: 0,0,0;
    --card-radius: 0; --card-shadow: none; --pill-radius: 0;
  }
  :root[data-theme="crt"] {
    /* ── CRT Green (PVE Dashboard) ── */
    --bg: #020a02; --bg2: #031003; --panel: #031603; --border: rgba(51,255,102,.28);
    --fg: #33ff66; --dim: #1f9941; --dimmer: #145a28;
    --accent: #7dff9c; --accent-hi: #a0ffb8; --accent2: #33ff66;
    --warn: #c6ff00; --err: #ff5544; --mention: #c6ff00;
    --hover: #041d04; --ov: 255,255,255;
    --card-radius: 2px; --card-shadow: 0 0 10px rgba(51,255,102,.12); --pill-radius: 2px;
  }
  :root[data-theme="amber"] {
    /* ── Amber Mono (PVE Dashboard) ── */
    --bg: #0d0700; --bg2: #140a00; --panel: #1a0e00; --border: rgba(255,176,0,.25);
    --fg: #ffb000; --dim: #b87900; --dimmer: #7a5200;
    --accent: #ffcb52; --accent-hi: #ffe080; --accent2: #ffb000;
    --warn: #ffd700; --err: #ff5e2e; --mention: #ffd700;
    --hover: #1f1100; --ov: 255,255,255;
    --card-radius: 2px; --card-shadow: 0 0 10px rgba(255,176,0,.1); --pill-radius: 2px;
  }
  :root[data-theme="paper"] {
    /* ── Paper Print (PVE Dashboard) ── */
    --bg: #f4f1ea; --bg2: #efeae0; --panel: #fffdf8; --border: #d8d2c4;
    --fg: #1c1b18; --dim: #6b675e; --dimmer: #9a968a;
    --accent: #9a3b2e; --accent-hi: #b8503e; --accent2: #3a6b2e;
    --warn: #9a7b1a; --err: #a32a22; --mention: #9a7b1a;
    --hover: #f5f0e6; --ov: 0,0,0;
    --card-radius: 2px; --card-shadow: 0 1px 0 #d8d2c4; --pill-radius: 2px;
  }
  :root[data-theme="vaporwave"] {
    /* ── Vaporwave (PVE Dashboard) ── */
    --bg: #2b0f54; --bg2: #1b1145; --panel: #3a1f6e; --border: rgba(255,134,200,.3);
    --fg: #ffe6ff; --dim: #c7a6ff; --dimmer: #8a6ac0;
    --accent: #7af9ff; --accent-hi: #a0fcff; --accent2: #9bffb0;
    --warn: #ffe66d; --err: #ff6b8b; --mention: #ffe66d;
    --hover: #4a2f80; --ov: 255,255,255;
    --card-radius: 16px; --card-shadow: 0 8px 24px rgba(255,134,200,.25); --pill-radius: 999px;
  }
  :root[data-theme="synthwave"] {
    /* ── Synthwave (PVE Dashboard) ── */
    --bg: #120024; --bg2: #06000f; --panel: #1c0636; --border: rgba(5,217,232,.3);
    --fg: #ffd9ff; --dim: #b07adb; --dimmer: #7a50a0;
    --accent: #05d9e8; --accent-hi: #40e8f0; --accent2: #39ff14;
    --warn: #f9c80e; --err: #ff2a6d; --mention: #f9c80e;
    --hover: #2a1048; --ov: 255,255,255;
    --card-radius: 4px; --card-shadow: 0 0 18px rgba(255,42,109,.3); --pill-radius: 3px;
  }
  :root[data-theme="gameboy"] {
    /* ── Game Boy (PVE Dashboard) ── */
    --bg: #9bbc0f; --bg2: #9bbc0f; --panel: #8bac0f; --border: #306230;
    --fg: #0f380f; --dim: #306230; --dimmer: #5a8a5a;
    --accent: #0f380f; --accent-hi: #1a4a1a; --accent2: #0f380f;
    --warn: #306230; --err: #0f380f; --mention: #306230;
    --hover: #98b80e; --ov: 0,0,0;
    --card-radius: 0; --card-shadow: 3px 3px 0 #0f380f; --pill-radius: 0;
  }
  :root[data-theme="dosblue"] {
    /* ── DOS Blue (PVE Dashboard) ── */
    --bg: #0000aa; --bg2: #0000aa; --panel: #0000aa; --border: #5555ff;
    --fg: #fff; --dim: #55ffff; --dimmer: #3a9a9a;
    --accent: #ffff55; --accent-hi: #ffffaa; --accent2: #55ff55;
    --warn: #ffff55; --err: #ff5555; --mention: #ffff55;
    --hover: #000080; --ov: 255,255,255;
    --card-radius: 0; --card-shadow: none; --pill-radius: 0;
  }
  :root[data-theme="popart"] {
    /* ── Pop Art (PVE Dashboard) ── */
    --bg: #0a0014; --bg2: #1a0033; --panel: #15041f; --border: #3a0d5e;
    --fg: #fff5e1; --dim: #b89cff; --dimmer: #7a60c0;
    --accent: #00f5ff; --accent-hi: #60faff; --accent2: #39ff14;
    --warn: #ffbe0b; --err: #ff206e; --mention: #ffbe0b;
    --hover: #200840; --ov: 255,255,255;
    --card-radius: 0; --card-shadow: 5px 5px 0 #ff006e; --pill-radius: 0;
  }
  :root[data-theme="lcars"] {
    /* ── LCARS (PVE Dashboard) ── */
    --bg: #000; --bg2: #000; --panel: #140d06; --border: #3a2a14;
    --fg: #FFCC99; --dim: #C9A98C; --dimmer: #8a6a50;
    --accent: #FF9900; --accent-hi: #FFCC66; --accent2: #66CC66;
    --warn: #FFCC66; --err: #CC6666; --mention: #FFCC66;
    --hover: #1f1508; --ov: 255,255,255;
    --card-radius: 14px; --card-shadow: none; --pill-radius: 999px;
  }
  * { box-sizing: border-box; }
  :root {
    --msg-font: "JetBrains Mono", "Fira Code", "Cascadia Code", ui-monospace, Menlo, monospace;
  }
  html, body { margin: 0; padding: 0; height: 100%;
    background: var(--bg); color: var(--fg);
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", ui-monospace, Menlo, monospace;
    font-size: 13px; line-height: 1.45;
  }
  #chat, #chat .msg, #chat .msg * { font-family: var(--msg-font); }
  button { font-family: inherit; }

  #app { display: grid; grid-template-columns: 1fr 300px; grid-template-rows: 42px 1fr auto;
         height: 100vh; }
  #app.side-collapsed { grid-template-columns: 1fr 0; }
  #app.side-collapsed #side { display: none; }

  /* ── Header ── */
  header { grid-column: 1 / 3; background: var(--bg2); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; padding: 0 16px; gap: 14px;
           font-weight: 600; }
  header .title { color: var(--accent); flex-shrink: 0; }
  header .meta { color: var(--dim); font-weight: 400; font-size: 11px; }
  header .spacer { flex: 1; }
  /* Participant chips — the "who is in this chat" label for a scoped
     conversation view. Sized off the target-bar pills so every theme's
     border/radius tokens apply without per-theme work. */
  header .participants { display: flex; align-items: center; gap: 5px;
                         overflow: hidden; flex-shrink: 1; min-width: 0; }
  header .participants:empty { display: none; }
  header .participants .pchip {
    display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0;
    font-size: 11px; font-weight: 600; padding: 2px 8px;
    border-radius: var(--pill-radius); background: var(--panel);
    border: 1px solid var(--border); max-width: 160px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  header .participants .pchip.unknown { color: var(--dimmer); font-weight: 400;
                                        font-style: italic; }
  header .participants .pchip .pc-emoji { font-size: 13px; line-height: 1; }
  .pill {
    font-size: 11px; padding: 3px 8px; border-radius: var(--pill-radius); cursor: pointer;
    background: var(--panel); border: 1px solid var(--border); user-select: none;
    color: var(--dim); font-weight: 500;
  }
  .pill:hover { border-color: var(--accent); color: var(--fg); }
  a.pill { text-decoration: none; }
  .pill.on { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  header .pill.conn.ok { color: var(--accent2); }
  header .pill.conn.bad { color: var(--err); }
  header #filter { background: var(--panel); color: var(--fg); border: 1px solid var(--border);
                   padding: 3px 8px; border-radius: 3px; font-family: inherit; font-size: 11px;
                   width: 160px; }
  header #filter:focus { outline: none; border-color: var(--accent); }
  #font-picker, #theme-picker {
                        background: var(--panel); color: var(--fg); border: 1px solid var(--border);
                        padding: 3px 6px; border-radius: 3px; font-family: inherit; font-size: 11px;
                        cursor: pointer; }
  #font-picker:focus, #theme-picker:focus { outline: none; border-color: var(--accent); }

  /* ── Settings panel (drawer) ── */
  #settings-panel {
    position: fixed; top: 46px; right: 10px; z-index: 30;
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--card-radius);
    padding: 12px 14px; min-width: 250px; max-width: 320px;
    box-shadow: var(--card-shadow, 0 8px 30px rgba(0,0,0,0.4));
    display: flex; flex-direction: column; gap: 10px;
  }
  #settings-panel[hidden] { display: none; }
  #settings-panel h3 { margin: 0; font-size: 10px; text-transform: uppercase;
                       letter-spacing: 0.6px; color: var(--dim); font-weight: 700; }
  #settings-panel .set-row { display: flex; align-items: center;
                             justify-content: space-between; gap: 12px;
                             font-size: 12px; color: var(--fg); }
  #settings-panel .set-row[hidden] { display: none; }
  #settings-panel .set-row > span:first-child { color: var(--dim); white-space: nowrap; }
  #settings-panel select {
    background: var(--panel); color: var(--fg); border: 1px solid var(--border);
    padding: 3px 6px; border-radius: 3px; font-family: inherit; font-size: 11px; cursor: pointer; }
  #settings-panel select:focus { outline: none; border-color: var(--accent); }
  #settings-panel input[type="range"] { width: 130px; cursor: pointer; accent-color: var(--accent); }

  /* ── Chat ── */
  #chat-wrap { grid-row: 2 / 3; grid-column: 1 / 2; position: relative; overflow: hidden; }
  #chat { height: 100%; overflow-y: auto; padding: 14px 18px; scroll-behavior: smooth; }
  .msg { margin-bottom: 12px; word-wrap: break-word; cursor: pointer; padding: 6px 10px 8px;
         border-radius: var(--card-radius); border-left: 3px solid transparent; margin-left: -10px; }
  .msg:hover { background: var(--hover); }
  .msg .head { font-size: 11px; color: var(--dim); margin-bottom: 4px;
               display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
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
  /* !bangs bar — UNFILTERABLE. Loudest visual; rendered above @mentions. */
  .msg .bangs-bar { font-size: 12px; margin: 2px 0 4px;
                    display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  .msg .bangs-bar .to-label { color: #ff8470; font-size: 10px; font-weight: 700;
                               text-transform: uppercase; letter-spacing: 1px;
                               margin-right: 2px;
                               padding: 1px 5px; border-radius: 3px;
                               background: rgba(255, 132, 112, 0.15); }
  .msg .bangs-bar .mchip { display: inline-flex; align-items: center; gap: 3px;
                           padding: 1px 7px 1px 5px; border-radius: 10px;
                           background: rgba(255, 132, 112, 0.2);
                           color: #ff8470;
                           border: 1px solid rgba(255, 132, 112, 0.5);
                           font-weight: 700; }
  .msg .bangs-bar .mchip .manimal { font-size: 13px; line-height: 1; }
  .msg .body { word-wrap: break-word; overflow-wrap: break-word; }
  .msg .body.plain { white-space: pre-wrap; }
  .msg .body > *:first-child { margin-top: 0; }
  .msg .body > *:last-child { margin-bottom: 0; }
  .msg .body p { margin: 4px 0; white-space: pre-wrap; }
  #chat .msg .body code.mdic { background: rgba(var(--ov),0.08); border: 1px solid rgba(var(--ov),0.1);
                         border-radius: 3px; padding: 0 4px; font-family: ui-monospace, Menlo, Monaco, monospace;
                         font-size: 0.92em; }
  #chat .msg .body pre.mdcode { background: rgba(var(--ov),0.05); border: 1px solid rgba(var(--ov),0.1);
                          border-radius: 4px; padding: 6px 8px; margin: 4px 0;
                          font-family: ui-monospace, Menlo, Monaco, monospace; font-size: 0.9em;
                          white-space: pre-wrap; overflow-x: auto; }
  .msg .body strong { font-weight: 700; }
  .msg .body em { font-style: italic; }
  .msg .body del { opacity: 0.7; }
  .msg .body a { color: var(--accent2); text-decoration: underline; }
  .msg .body h1, .msg .body h2, .msg .body h3,
  .msg .body h4, .msg .body h5, .msg .body h6 {
    margin: 8px 0 4px; font-weight: 700; line-height: 1.25; }
  .msg .body h1 { font-size: 1.35em; border-bottom: 1px solid rgba(var(--ov),0.15); padding-bottom: 2px; }
  .msg .body h2 { font-size: 1.2em; border-bottom: 1px solid rgba(var(--ov),0.1); padding-bottom: 2px; }
  .msg .body h3 { font-size: 1.1em; }
  .msg .body h4 { font-size: 1.0em; }
  .msg .body h5 { font-size: 0.95em; opacity: 0.9; }
  .msg .body h6 { font-size: 0.9em; opacity: 0.8; }
  .msg .body ul, .msg .body ol { margin: 4px 0; padding-left: 22px; }
  .msg .body ul ul, .msg .body ol ol,
  .msg .body ul ol, .msg .body ol ul { margin: 0; }
  .msg .body li { margin: 1px 0; }
  .msg .body li.task { list-style: none; margin-left: -18px; }
  .msg .body li.task input { margin-right: 6px; vertical-align: -1px; }
  .msg .body blockquote { margin: 4px 0; padding: 2px 10px; border-left: 3px solid var(--accent2);
                          background: rgba(var(--ov),0.03); color: rgba(var(--ov),0.85); }
  .msg .body hr { border: 0; border-top: 1px solid rgba(var(--ov),0.18); margin: 8px 0; }
  .msg .body table { border-collapse: collapse; margin: 4px 0; font-size: 0.95em; }
  .msg .body th, .msg .body td { border: 1px solid rgba(var(--ov),0.15); padding: 3px 8px; }
  .msg .body th { background: rgba(var(--ov),0.06); font-weight: 700; text-align: left; }
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
  /* Two-party view: "who has read this" is just the one other person, so the
     ack badges are noise. With three or more participants they carry real
     information again, so they stay. */
  body.conv-pair .acks { display: none; }

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
  .watermark-pin.ctx-ringed { border-radius: 50%; padding: 2px; }
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
  #jump-btn:hover { background: var(--accent-hi); }
  #jump-btn .count { background: var(--err); color: white;
                     border-radius: 10px; padding: 1px 6px; margin-left: 4px; font-size: 10px; }

  /* ── Roster sidebar ── */
  #side { grid-row: 2 / 3; grid-column: 2 / 3;
          background: var(--panel); border-left: 1px solid var(--border);
          overflow-y: auto; display: flex; flex-direction: column; }
  #side section { padding: 14px 14px; border-bottom: 1px solid var(--border); }
  #side section:last-child { border-bottom: none; }
  #side h2 { font-size: 10px; text-transform: uppercase; color: var(--dim);
             letter-spacing: 0.08em; margin: 0 0 10px; font-weight: 600; }

  .member { padding: 8px 0; cursor: pointer; }
  .member + .member { border-top: 1px solid var(--border); }
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
  .member .conv-pick { font-size: 12px; line-height: 1; cursor: pointer;
                       color: var(--dimmer); flex-shrink: 0; user-select: none; }
  .member .conv-pick:hover { color: var(--accent); }
  .member .conv-pick.on { color: var(--accent); }
  #conv-pickbar { display: flex; flex-wrap: wrap; gap: 5px; margin: 0 0 8px; }
  .ctx-card { display: flex; align-items: center; gap: 8px; padding: 4px 2px; }
  .ctx-ring { position: relative; width: 36px; height: 36px; flex: none; }
  .ctx-ring svg { width: 36px; height: 36px; transform: rotate(-90deg); }
  .ctx-ring .track { fill: none; stroke: var(--border); stroke-width: 4; }
  .ctx-ring .fill { fill: none; stroke-width: 4; stroke-linecap: round;
                    transition: stroke-dashoffset 0.6s ease; }
  .ctx-ring .pct-text { position: absolute; inset: 0; display: flex;
                        align-items: center; justify-content: center;
                        font-size: 10px; color: var(--fg); }
  .ctx-info { min-width: 0; }
  .ctx-name { font-size: 11px; color: var(--fg); white-space: nowrap;
              overflow: hidden; text-overflow: ellipsis; }
  .ctx-meta { font-size: 10px; color: var(--dim); }
  .ctx-empty { font-size: 11px; color: var(--dim); padding: 2px; }
  .member .ctx-pct { font-size: 9px; padding: 1px 5px; border-radius: 7px;
                     background: #2a3340; color: #8fa5c0; margin-left: 4px; }
  .member .ctx-pct.warm { background: #4a3a20; color: #e5d35e; }
  .member .ctx-pct.hot  { background: #4a2420; color: #ff8470; }
  .member .fmode { font-size: 9px; padding: 1px 5px; border-radius: 3px;
                   flex-shrink: 0; user-select: none;
                   text-transform: uppercase; letter-spacing: 0.5px;
                   border: 1px solid transparent; }
  .member .fmode.all   { color: var(--dim); background: #1c2432; border-color: #283242; }
  .member .fmode.about { color: #9ccf9c; background: rgba(126, 222, 126, 0.08);
                         border-color: rgba(126, 222, 126, 0.25); }
  .member .fmode.at    { color: #f0c060; background: rgba(240, 192, 96, 0.1);
                         border-color: rgba(240, 192, 96, 0.3); }
  .member .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                  font-weight: 500; }
  .member .caret { color: var(--dimmer); font-size: 9px; transition: transform 0.1s; }
  .member.expanded .caret { transform: rotate(90deg); }
  .member .id { color: var(--dimmer); font-size: 10px; margin-left: 2px; }
  .dot.active { background: var(--accent2); }
  .dot.idle { background: var(--dimmer); }
  .dot.stale { background: var(--warn); }
  .dot.dead { background: var(--err); }
  .member .stext { font-size: 10px; color: var(--dim); margin-top: 4px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                   padding-left: 16px; line-height: 1.4; }

  .member .stats { display: none; padding: 10px 0 4px 16px;
                   font-size: 10px; color: var(--dim); }
  .member.expanded .stats { display: block; }
  .stats .stat-row { display: flex; justify-content: space-between; padding: 3px 0; gap: 12px; }
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
  #chanstats .stat-row { display: flex; justify-content: space-between; padding: 4px 0;
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
              padding: 10px 16px; display: flex; flex-direction: column; gap: 6px; }
  #preview { font-size: 11px; color: var(--dim); min-height: 14px; }
  #preview .tgt { color: var(--mention); font-weight: 600; }
  /* Horizontal persistent-target selector — pick 1..N claudes (or All) and
     every send is addressed to them until toggled off. */
  #target-bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
                font-size: 11px; min-height: 24px; }
  #target-bar .tb-label { color: var(--dim); margin-right: 2px; }
  #target-bar .tb-pill { background: var(--panel); color: var(--dim);
                         border: 1px solid var(--border); border-radius: 12px;
                         padding: 2px 9px; cursor: pointer; user-select: none;
                         font-family: inherit; font-size: 11px;
                         display: inline-flex; align-items: center; gap: 4px;
                         transition: background 0.08s, color 0.08s, border-color 0.08s; }
  #target-bar .tb-pill:hover { border-color: var(--accent); color: var(--fg); }
  #target-bar .tb-pill.on { background: var(--accent); color: var(--bg);
                            border-color: var(--accent); font-weight: 600; }
  #target-bar .tb-pill .tb-num { opacity: 0.6; font-size: 10px; }
  #target-bar .tb-pill.on .tb-num { opacity: 0.9; }
  #target-bar .tb-pill.tb-all { border-style: dashed; }
  #target-bar .tb-pill.tb-all.on { border-style: solid; }
  /* The target bar stays visible in a conversation — restricted to that
     conversation's members — so you can still reply to just one of them
     without leaving the view. */
  #input-row { display: flex; gap: 8px; align-items: flex-end; position: relative; }
  #input { flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
           padding: 8px 10px; border-radius: var(--input-radius); font-family: inherit; font-size: 13px;
           resize: none; min-height: 36px; max-height: 160px; }
  #input:focus { outline: none; border-color: var(--accent); }
  #send-btn { background: var(--accent); color: var(--bg); border: none;
              padding: 0 18px; height: 36px; border-radius: 4px; cursor: pointer;
              font-weight: 600; font-family: inherit; font-size: 13px; }
  #send-btn:hover { background: var(--accent-hi); }
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

  /* Mobile roster toggle — hidden on desktop, sole sidebar opener on mobile */
  #btn-mobile-roster { display: none; font-size: 16px; padding: 3px 10px; }

  /* ── Mobile responsive ── */
  @media (max-width: 768px) {
    #app { grid-template-columns: 1fr !important; grid-template-rows: auto 1fr auto; }
    header { flex-wrap: nowrap; gap: 6px; padding: 6px 10px; height: 42px; overflow: hidden; }
    header .spacer { flex: 1; }
    header .meta { display: none; }
    /* Mobile header: channel name + spacer + hamburger + settings + conn dot */
    header > #filter, header > #font-picker, header > #theme-picker,
    header > #btn-side, header > #btn-compact, header > #btn-notify,
    header > #btn-sound { display: none !important; }
    #btn-mobile-roster { display: inline-block !important; order: 9; }
    #btn-settings { order: 10; font-size: 14px; padding: 3px 8px; }
    #h-conn { order: 11; font-size: 10px; padding: 2px 6px; }

    /* Sidebar: hidden by default, full-overlay when toggled open */
    #side { display: none !important; position: fixed; inset: 0; z-index: 20;
            grid-column: 1; grid-row: 2; border-left: none;
            overflow-y: auto; padding-top: 48px; }
    #app.mobile-side-open #side { display: flex !important; }
    /* Scrim behind sidebar overlay */
    #mobile-scrim { display: none; position: fixed; inset: 0; z-index: 19;
                    background: rgba(0,0,0,0.5); }
    #app.mobile-side-open #mobile-scrim { display: block; }

    /* Settings panel: full-width on mobile */
    #settings-panel { right: 0; left: 0; max-width: 100%; border-radius: 0;
                      top: auto; position: fixed; }

    /* Composer: touch-friendly */
    #composer { padding: 6px 8px; }
    #input { font-size: 16px; min-height: 40px; }  /* ≥16px prevents iOS zoom */
    #send-btn { height: 40px; padding: 0 14px; }
    #hint { display: none; }
    #target-bar { gap: 4px; }
    #target-bar .tb-pill { padding: 4px 10px; font-size: 12px; }

    /* Chat: tighter padding */
    #chat { padding: 8px 10px; }
    .msg { margin-left: -4px; padding: 4px 4px 6px; }

    /* Completions: full-width */
    #completions { left: 0; right: 0; min-width: auto; }

    /* Jump button: centered */
    #jump-btn { right: 50%; transform: translateX(50%); }
  }

  @media (max-width: 480px) {
    header .meta { display: none; }
    .msg .head { font-size: 10px; }
    .msg .mentions-bar .mchip, .msg .refs-bar .mchip,
    .msg .bangs-bar .mchip { font-size: 10px; padding: 1px 5px; }
    #target-bar .tb-pill { padding: 3px 7px; font-size: 11px; }
  }

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
  #guest-modal button:hover { background: var(--accent-hi); }
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
    <a class="pill" id="btn-home" href="/" title="back to the hub landing page">⌂</a>
    <span class="title" id="h-channel">trio#…</span>
    <span class="participants" id="h-participants"></span>
    <span class="meta" id="h-meta">connecting…</span>
    <span class="spacer"></span>
    <select id="theme-picker" title="color theme">
      <optgroup label="Dark">
        <option value="midnight">Midnight</option>
        <option value="nord">Nord</option>
        <option value="dracula">Dracula</option>
        <option value="pve-dark">Proxmox</option>
        <option value="solarized">Solarized</option>
        <option value="synthwave">Synthwave</option>
        <option value="vaporwave">Vaporwave</option>
        <option value="lcars">LCARS</option>
      </optgroup>
      <optgroup label="Light">
        <option value="light">Daylight</option>
        <option value="pve-light">Clean</option>
        <option value="paper">Paper</option>
        <option value="popart">Pop Art</option>
        <option value="bluebubble">Walled Garden</option>
      </optgroup>
      <optgroup label="Retro">
        <option value="crt">CRT Green</option>
        <option value="amber">Amber Mono</option>
        <option value="dosblue">DOS Blue</option>
        <option value="gameboy">Game Boy</option>
        <option value="win31">Windows 3.1</option>
      </optgroup>
    </select>
    <select id="font-picker" title="message font">
      <option value='"JetBrains Mono", "Fira Code", "Cascadia Code", ui-monospace, Menlo, monospace'>JetBrains Mono (default)</option>
      <option value='"Fira Code", ui-monospace, Menlo, monospace'>Fira Code</option>
      <option value='"Cascadia Code", "Cascadia Mono", ui-monospace, Consolas, monospace'>Cascadia Code</option>
      <option value='"Hack", ui-monospace, Menlo, monospace'>Hack</option>
      <option value='"IBM Plex Mono", ui-monospace, Menlo, monospace'>IBM Plex Mono</option>
      <option value='"Source Code Pro", ui-monospace, Menlo, monospace'>Source Code Pro</option>
      <option value='Menlo, Monaco, ui-monospace, monospace'>Menlo</option>
      <option value='Monaco, Menlo, ui-monospace, monospace'>Monaco</option>
      <option value='Consolas, "Cascadia Mono", ui-monospace, monospace'>Consolas</option>
      <option value='"SF Mono", "SFMono-Regular", ui-monospace, Menlo, monospace'>SF Mono</option>
    </select>
    <input id="filter" type="text" placeholder="filter messages…" spellcheck="false">
    <span class="pill on" id="btn-side" title="show/hide the roster sidebar">roster</span>
    <span class="pill" id="btn-compact" title="clamp every message body to 3 lines">compact</span>
    <span class="pill" id="btn-notify" title="desktop notifications on @you">🔔 off</span>
    <span class="pill" id="btn-sound" title="play a chime on any new message">🔊 off</span>
    <span class="pill" id="btn-settings" title="settings">⚙ settings</span>
    <span class="pill" id="btn-mobile-roster" title="show roster &amp; context">☰</span>
    <span class="pill conn bad" id="h-conn">● disconnected</span>
  </header>
  <div id="settings-panel" hidden>
    <h3>Settings</h3>
  </div>

  <div id="mobile-scrim"></div>
  <div id="chat-wrap">
    <div id="chat"></div>
    <button id="jump-btn">↓ latest<span class="count" id="jump-count" style="display:none">0</span></button>
  </div>

  <aside id="side">
    <section>
      <div id="filter-banner">filter active — showing matching messages only. click to clear.</div>
      <h2 id="r-heading">Members</h2>
      <div id="conv-pickbar" hidden></div>
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
    <div id="target-bar"></div>
    <div id="input-row">
      <div id="completions"></div>
      <textarea id="input" rows="1" placeholder="Message — @ to mention, Enter to send"></textarea>
      <button id="send-btn">Send</button>
    </div>
    <div id="hint">
      <kbd>Enter</kbd> send
      <kbd>Shift+Enter</kbd> newline
      <kbd>@</kbd> mention
      <kbd>Tab</kbd> accept completion
      <kbd>Esc</kbd> dismiss
      <kbd>↑/↓</kbd> navigate
      <kbd>Alt+1..9</kbd> toggle target
      <kbd>Alt+A</kbd> all
      <kbd>Alt+0</kbd> clear
      <kbd>Ctrl+B</kbd> roster
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
  const convPickBar = document.getElementById('conv-pickbar');
  const chanStatsEl = document.getElementById('chanstats');
  const sparkEl = document.getElementById('sparkline');
  const hChannel = document.getElementById('h-channel');
  const partsEl = document.getElementById('h-participants');
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
  const btnSound = document.getElementById('btn-sound');
  const fontPicker = document.getElementById('font-picker');
  const jumpBtn = document.getElementById('jump-btn');
  const jumpCount = document.getElementById('jump-count');
  const targetBar = document.getElementById('target-bar');

  // Message-font picker — persists per-origin via localStorage.
  try {
    const saved = localStorage.getItem('trio.msgFont');
    if (saved) {
      let found = false;
      for (const opt of fontPicker.options) {
        if (opt.value === saved) { fontPicker.value = saved; found = true; break; }
      }
      if (found) document.documentElement.style.setProperty('--msg-font', saved);
    }
  } catch (_) { /* private-mode: ignore */ }
  fontPicker.addEventListener('change', () => {
    const v = fontPicker.value;
    document.documentElement.style.setProperty('--msg-font', v);
    try { localStorage.setItem('trio.msgFont', v); } catch (_) {}
  });

  // Theme picker — persists per-origin via localStorage. Unknown/missing
  // theme falls back to 'midnight' (the base :root palette).
  const themePicker = document.getElementById('theme-picker');
  function applyTheme(v) {
    document.documentElement.setAttribute('data-theme', v || 'midnight');
  }
  try {
    const savedTheme = localStorage.getItem('trio.theme');
    if (savedTheme) {
      for (const opt of themePicker.options) {
        if (opt.value === savedTheme) { themePicker.value = savedTheme; break; }
      }
      applyTheme(savedTheme);
    } else {
      applyTheme('midnight');
    }
  } catch (_) { applyTheme('midnight'); }
  themePicker.addEventListener('change', () => {
    applyTheme(themePicker.value);
    try { localStorage.setItem('trio.theme', themePicker.value); } catch (_) {}
  });

  // ── URL params ──
  const URL_PARAMS = new URLSearchParams(location.search);
  // ?dm=<id> historically scoped the view to one agent. It now accepts a
  // comma-separated list (?dm=idA,idB) describing a *conversation*: the
  // operator plus N agents. One id keeps the old two-party behaviour, so
  // existing bookmarked DM tabs keep working unchanged.
  // Ids are validated and capped: they arrive from a URL, are used as Map
  // keys and textContent (never innerHTML), and an unbounded list would make
  // refreshDmVisibility() walk the set once per message per render.
  const CONV_MAX = 8;
  const CONV_IDS = [...new Set(
    (URL_PARAMS.get('dm') || '')
      .split(',').map(s => s.trim())
      .filter(s => s && /^[A-Za-z0-9_.-]+$/.test(s))
  )].slice(0, CONV_MAX);
  const CONV_MODE = CONV_IDS.length > 0;
  // Kept as aliases so the pre-existing DM call sites keep reading naturally.
  const DM_TARGET_ID = CONV_IDS.length === 1 ? CONV_IDS[0] : '';
  const DM_MODE = CONV_MODE;
  // ?pane=1 marks this document as embedded in the /workspace shell. Panes
  // must not write shared localStorage keys — iframes share the top-level
  // origin's storage, so a pane persisting its view would silently redefine
  // the main tab's preferences.
  const PANE_MODE = URL_PARAMS.get('pane') === '1';
  // ?roster=0 forces the sidebar hidden on load regardless of the
  // localStorage preference. Split-screen panes use this so an embedded
  // pane never inherits the standalone window's roster choice.
  const ROSTER_PARAM = URL_PARAMS.get('roster');
  // Landing-mode multiplexing: when this page is served at /c/<code>, the
  // server substitutes a "?channel=<code>" query string here so every API
  // call names its channel. Single-channel mode leaves it '' (the server
  // already knows its one channel) — the token below is valid JS as-is.
  const API_QS = /*__API_QS__*/'';

  // ── State ──
  const state = {
    channel: '',
    operator: { id: '', name: '' },
    server_host: '',
    dmTargetId: DM_TARGET_ID,      // empty string → main channel view
    // Conversation scope: member ids this view is limited to (excluding the
    // operator, who is always implicitly present). Empty → whole channel.
    convIds: new Set(CONV_IDS),
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
    initialLoad: true,              // pin to newest until the history burst settles
    soundEnabled: false,
    chimeVolume: 0.33,
    notifyScope: 'mention',   // 'mention' | 'all'
    notifyWhen: 'hidden',     // 'hidden' | 'always'
    unreadCount: 0,                 // for tab title while hidden
    jumpUnread: 0,                  // messages arrived while user was scrolled up
    rateBins: new Map(),            // bin_epoch_10s → count
    startedAt: Date.now(),
    originalTitle: 'nth_web',
    // Persistent target selection: set of member_ids that every send is
    // addressed to (prepended as @name mentions). Empty = broadcast.
    selectedTargets: new Set(),
    // Roster checkboxes staging a new conversation ("Start a conversation").
    // View-local and deliberately unpersisted — it's a transient selection.
    convPicks: new Set(),
    // Ordered list of target ids as rendered in the bar — index → id,
    // so Alt+1..9 maps to the Nth pill.
    targetOrder: [],
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
    // Prefer the server-assigned avatar when present — the server runs
    // a per-channel collision-free assignment (animal_for_channel) so
    // no two current members share an emoji. Fall back to a local hash
    // pick for historical message authors no longer in the roster.
    if (member && member.animal_emoji) {
      return { name: member.animal_name || '', emoji: member.animal_emoji };
    }
    const id = (member && (member.id || member.member_id)) || '';
    const i = hash32(id) % ANIMAL_EMOJIS.length;
    return { name: ANIMAL_NAMES[i], emoji: ANIMAL_EMOJIS[i] };
  }
  // Lookup table: member_id → {name, emoji} from the most recent roster.
  // Used to resolve avatars on messages whose author is still in the
  // channel — the message object itself doesn't carry the avatar.
  const AVATAR_BY_ID = new Map();
  function rememberAvatars(members) {
    AVATAR_BY_ID.clear();
    for (const m of (members || [])) {
      if (m && m.id && m.animal_emoji) {
        AVATAR_BY_ID.set(m.id, { name: m.animal_name || '', emoji: m.animal_emoji });
      }
    }
  }
  function animalForId(id) {
    const cached = AVATAR_BY_ID.get(id);
    if (cached) return cached;
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

  // Markdown → HTML. Server is stdlib-only; render on the client.
  // Block-level: ATX headings (# … ######), fenced code (```lang), lists
  // (ul/ol, nested by indent), GFM task lists (- [ ] / - [x]), blockquotes
  // (nested with renderMarkdown recursion), thematic breaks (---, ***, ___),
  // GFM pipe tables (with :---: alignment), paragraphs.
  // Inline: **bold**, *italic*/_italic_, ~~strike~~, `inline code`,
  // [text](url), autolinked http(s). Soft line breaks inside a paragraph
  // become <br>.
  function renderMarkdown(text) {
    if (!text) return '';
    text = text.replace(/\u0000/g, '');
    // Stash fenced and inline code FIRST so their contents survive every
    // subsequent transform (including line splitting for block parsing).
    const fences = [];
    let src = text.replace(/```(?:([A-Za-z0-9_+-]+))?\n?([\s\S]*?)```/g, (_m, lang, code) => {
      fences.push(code.replace(/\n$/, ''));
      return '\u0000F' + (fences.length - 1) + '\u0000';
    });
    const inlines = [];
    src = src.replace(/`([^`\n]+)`/g, (_m, code) => {
      inlines.push(code);
      return '\u0000I' + (inlines.length - 1) + '\u0000';
    });

    function inlineFmt(t) {
      t = escapeHtml(t);
      t = humanizeIdSigils(t);
      t = t.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
      t = t.replace(/(^|[\s(\[])\*([^*\n]+?)\*(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/(^|[\s(\[])_([^_\n]+?)_(?=[\s.,!?;:)\]]|$)/g, '$1<em>$2</em>');
      t = t.replace(/~~([^~\n]+?)~~/g, '<del>$1</del>');
      t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, txt, url) => {
        const safeUrl = url.replace(/&(?:quot|#39);/g, '');
        return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>';
      });
      t = t.replace(/(^|[\s(])(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, (_m, pre, url) => {
        const safeUrl = url.replace(/&(?:quot|#39);/g, '');
        return pre + '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
      });
      return t;
    }

    function splitRow(row) {
      let r = row.trim();
      if (r.startsWith('|')) r = r.slice(1);
      if (r.endsWith('|')) r = r.slice(0, -1);
      return r.split('|').map(c => c.trim());
    }
    function isTableSep(line) {
      return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    }
    function parseAlign(sep) {
      return splitRow(sep).map(c => {
        const left = c.startsWith(':'), right = c.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        if (left) return 'left';
        return '';
      });
    }

    // A list marker at the start (after stripping leading indent).
    function listMarker(line) {
      const m = line.match(/^(\s*)(-|\*|\+|\d+\.)\s+(.*)$/);
      if (!m) return null;
      const indent = m[1].replace(/\t/g, '    ').length;
      const ordered = /^\d+\./.test(m[2]);
      let content = m[3];
      let task = null;
      const tm = content.match(/^\[( |x|X)\]\s+(.*)$/);
      if (tm) { task = tm[1].toLowerCase() === 'x'; content = tm[2]; }
      return { indent, ordered, content, task };
    }

    // Consume a list beginning at lines[start] with baseline indent.
    // Returns [html, nextIndex]. Nested lists handled by recursion: a line
    // whose indent is > baseline and is itself a list marker becomes a
    // child list attached to the previous <li>.
    function parseList(lines, start) {
      const first = listMarker(lines[start]);
      if (!first) return null;
      const baseIndent = first.indent;
      const ordered = first.ordered;
      const items = [];  // { html, task }
      let i = start;
      while (i < lines.length) {
        const line = lines[i];
        if (!line.trim()) {
          // Blank line: list continues if the next non-blank is still a
          // list item at the same indent. Otherwise break.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j >= lines.length) { i = j; break; }
          const nxt = listMarker(lines[j]);
          if (!nxt || nxt.indent < baseIndent) { i = j; break; }
          i = j; continue;
        }
        const mk = listMarker(line);
        if (mk && mk.indent === baseIndent && mk.ordered === ordered) {
          // Collect continuation lines (indented more, non-list) and
          // child lists (indented more, list marker).
          let body = inlineFmt(mk.content);
          let task = mk.task;
          i++;
          let childHtml = '';
          while (i < lines.length) {
            const ln = lines[i];
            if (!ln.trim()) break;
            const sub = listMarker(ln);
            if (sub && sub.indent > baseIndent) {
              const [h, ni] = parseList(lines, i);
              childHtml += h;
              i = ni;
              continue;
            }
            if (sub && sub.indent <= baseIndent) break;
            // Lazy continuation — appended as soft-wrapped text.
            body += '\n' + inlineFmt(ln.trim());
            i++;
          }
          items.push({ body: body.replace(/\n/g, '<br>') + childHtml, task });
        } else if (mk && mk.indent < baseIndent) {
          break;
        } else if (!mk) {
          break;
        } else {
          // Different list type (ordered vs unordered) or deeper start —
          // terminate this list so the caller can start a new one.
          break;
        }
      }
      const tag = ordered ? 'ol' : 'ul';
      let html = '<' + tag + '>';
      for (const it of items) {
        if (it.task === null || it.task === undefined) {
          html += '<li>' + it.body + '</li>';
        } else {
          const checked = it.task ? ' checked' : '';
          html += '<li class="task"><input type="checkbox" disabled' + checked + '>' +
                  it.body + '</li>';
        }
      }
      html += '</' + tag + '>';
      return [html, i];
    }

    const lines = src.split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      // Skip blank lines between blocks.
      if (!line.trim()) { i++; continue; }

      // Thematic break.
      if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
        out.push('<hr>'); i++; continue;
      }

      // ATX heading.
      const h = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) {
        const lvl = h[1].length;
        out.push('<h' + lvl + '>' + inlineFmt(h[2]) + '</h' + lvl + '>');
        i++; continue;
      }

      // Blockquote — collect consecutive `>` lines, recurse on dequoted body.
      if (/^\s{0,3}>\s?/.test(line)) {
        const block = [];
        while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
          block.push(lines[i].replace(/^\s{0,3}>\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + renderMarkdown(block.join('\n')) + '</blockquote>');
        continue;
      }

      // GFM table — require a pipe in the first line AND a separator on the next.
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const header = splitRow(line);
        const align = parseAlign(lines[i + 1]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        let t = '<table><thead><tr>';
        header.forEach((cell, j) => {
          const a = align[j] ? ' style="text-align:' + align[j] + '"' : '';
          t += '<th' + a + '>' + inlineFmt(cell) + '</th>';
        });
        t += '</tr></thead><tbody>';
        rows.forEach(r => {
          t += '<tr>';
          for (let j = 0; j < header.length; j++) {
            const a = align[j] ? ' style="text-align:' + align[j] + '"' : '';
            t += '<td' + a + '>' + inlineFmt(r[j] || '') + '</td>';
          }
          t += '</tr>';
        });
        t += '</tbody></table>';
        out.push(t);
        continue;
      }

      // List (ul / ol).
      if (listMarker(line)) {
        const parsed = parseList(lines, i);
        if (parsed) { out.push(parsed[0]); i = parsed[1]; continue; }
      }

      // Fenced-code sentinel — emit directly to prevent <p><pre> nesting.
      if (/^\u0000F\d+\u0000$/.test(line.trim())) {
        out.push(line.trim()); i++; continue;
      }

      // Paragraph — consume until a block boundary.
      const p = [];
      while (i < lines.length) {
        const ln = lines[i];
        if (!ln.trim()) break;
        if (/^\u0000F\d+\u0000$/.test(ln)) break;
        if (/^\s{0,3}(#{1,6})\s+/.test(ln)) break;
        if (/^\s{0,3}>\s?/.test(ln)) break;
        if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(ln)) break;
        if (listMarker(ln)) break;
        if (ln.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) break;
        p.push(ln);
        i++;
      }
      out.push('<p>' + p.map(inlineFmt).join('<br>') + '</p>');
    }

    let html = out.join('');
    html = html.replace(/\u0000I(\d+)\u0000/g, (_m, k) =>
      '<code class="mdic">' + escapeHtml(inlines[+k]) + '</code>');
    html = html.replace(/\u0000F(\d+)\u0000/g, (_m, k) =>
      '<pre class="mdcode">' + escapeHtml(fences[+k]) + '</pre>');
    return html;
  }

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

  // Rewrite @<member_id> / #<member_id> / !<member_id> to @<friendly-name>
  // in message bodies before rendering. The raw id-sigil form is valid
  // input (the server-side parser routes it correctly) but ugly to read;
  // agents can address-by-id for rename resilience and the UI translates
  // back to the current display name on the fly. Unknown ids are left
  // alone so stale history isn't mangled.
  function humanizeIdSigils(text) {
    if (!text) return text;
    if (!state.members || !state.members.size) return text;
    // Build a single alternation across all known ids, longest first so
    // "_op_g_bob_abcdef" beats a hypothetical prefix "_op_g_bob".
    const ids = Array.from(state.members.keys())
      .filter(Boolean)
      .sort((a, b) => b.length - a.length)
      .map(id => id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!ids.length) return text;
    const re = new RegExp('([@#!])(' + ids.join('|') + ')(?=\\b|$)', 'g');
    return text.replace(re, (match, sigil, id) => {
      const mem = state.members.get(id);
      const name = mem && mem.name ? escapeHtml(mem.name) : id;
      return sigil + name;
    });
  }

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

  // After the initial history burst goes quiet, snap once more to the bottom
  // (markdown/fonts reflow taller after the synchronous appends) and switch to
  // normal "follow only if near bottom" behavior for live messages.
  let _initialSettleTimer = null;
  function scheduleInitialSettle() {
    if (_initialSettleTimer) clearTimeout(_initialSettleTimer);
    _initialSettleTimer = setTimeout(() => {
      _initialSettleTimer = null;
      state.initialLoad = false;
      requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
    }, 250);
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
                       + humanizeIdSigils(m.content || '').toLowerCase() + ' '
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

    // !bangs bar FIRST — unfilterable, loudest visual signal.
    if (!isSystem && m.bangs && m.bangs.length) {
      div.appendChild(renderTargetBar(m.bangs, 'bangs-bar', '!', 'BANG'));
    }
    // @mentions bar (pings) — always rendered above body so auto-@ isn't missed.
    if (!isSystem && m.mentions && m.mentions.length) {
      div.appendChild(renderTargetBar(m.mentions, 'mentions-bar', '@', '→'));
    }
    // #pound refs bar (talked about, not pinged). Softer visual.
    if (!isSystem && m.refs && m.refs.length) {
      div.appendChild(renderTargetBar(m.refs, 'refs-bar', '#', 'about'));
    }

    const body = document.createElement('div');
    body.className = 'body';
    if (isSystem) {
      body.classList.add('plain');
      body.textContent = humanizeIdSigils(m.content || '');
    } else {
      body.innerHTML = renderMarkdown(m.content || '');
    }
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

    if (state.initialLoad) {
      // Fresh page load: keep pinned to the newest message through the whole
      // history burst, then do one final settle after layout reflows.
      chat.scrollTop = chat.scrollHeight;
      scheduleInitialSettle();
    } else if (nearBottom) {
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

    // Desktop notification on @you while hidden (opt-in). In a scoped
    // conversation, only fire for messages that view actually shows — reuse
    // the view predicate rather than restating the rule, so the two can't
    // drift apart.
    const dmOk = isRelevantInDm(m);
    const scopeOk = state.notifyScope === 'all'
      ? (!isMine && !isSystem)
      : (!isMine && mentionsOperator);
    const whenOk = state.notifyWhen === 'always' ? true : document.hidden;
    if (state.notifyEnabled && whenOk && scopeOk && dmOk &&
        'Notification' in window && Notification.permission === 'granted') {
      try {
        const n = new Notification(`@${state.operator.name} — ${m.member_name}`, {
          body: humanizeIdSigils(m.content || '').slice(0, 140),
          tag: 'trio-' + m.id,
          silent: false,
        });
        n.onclick = () => { window.focus(); n.close(); };
      } catch (e) { /* ignore */ }
    }

    // In-page chime on any new message from someone else (opt-in, focus-agnostic).
    if (state.soundEnabled && !isMine && !isSystem) playChime();
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
      // Re-humanize id-sigils in the body: a rename changes the display
      // form, and any unknown ids that have since joined the roster
      // should now resolve.
      const body = dom.querySelector('.body');
      if (body) {
        if (isSystemContent(m.content || '')) {
          body.classList.add('plain');
          body.textContent = humanizeIdSigils(m.content || '');
        } else {
          body.classList.remove('plain');
          body.innerHTML = renderMarkdown(m.content || '');
        }
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
      rebuildBar(dom.querySelector('.bangs-bar'),    m.bangs,    '!');
      rebuildBar(dom.querySelector('.mentions-bar'), m.mentions, '@');
      rebuildBar(dom.querySelector('.refs-bar'),     m.refs,     '#');
    }
  }

  // ── Roster rendering ──
  // ── Persistent target selector (horizontal bar above the chat box) ──
  // Treat any roster row that isn't this operator and isn't another web
  // operator (_op_*) as a "claude" eligible for targeting.
  function isTargetable(m) {
    if (!m || !m.id) return false;
    if (m.id === state.operator.id) return false;
    if (m.id.startsWith('_op_')) return false;
    return true;
  }
  function targetStorageKey() {
    return 'trio_targets_' + (state.channel || '_');
  }
  // A conversation view derives its targets from the URL, and localStorage is
  // shared across every same-origin document — including split-screen panes.
  // Letting a scoped view read or write the shared key would make panes race
  // each other and silently redefine the main tab's sticky selection.
  function loadPersistedTargets() {
    if (CONV_MODE) {
      state.selectedTargets = new Set(CONV_IDS);
      return;
    }
    try {
      const raw = localStorage.getItem(targetStorageKey());
      if (!raw) return;
      const ids = JSON.parse(raw);
      if (Array.isArray(ids)) {
        state.selectedTargets = new Set(ids.filter(x => typeof x === 'string'));
      }
    } catch (_) { /* ignore */ }
  }
  function savePersistedTargets() {
    if (CONV_MODE) return;
    try {
      localStorage.setItem(targetStorageKey(),
        JSON.stringify([...state.selectedTargets]));
    } catch (_) { /* ignore */ }
  }
  function toggleTarget(id) {
    if (state.selectedTargets.has(id)) state.selectedTargets.delete(id);
    else state.selectedTargets.add(id);
    savePersistedTargets();
    renderComposerTargets();
    updatePreview();
  }
  function toggleAllTargets() {
    const all = state.targetOrder;
    if (all.length === 0) return;
    const allSelected = all.every(id => state.selectedTargets.has(id));
    if (allSelected) state.selectedTargets.clear();
    else for (const id of all) state.selectedTargets.add(id);
    savePersistedTargets();
    renderComposerTargets();
    updatePreview();
  }
  function renderComposerTargets() {
    if (!targetBar) return;
    targetBar.innerHTML = '';
    // Build the ordered list of targetable members. Sort by active-first
    // then name so the numbering is stable-ish across renders.
    const order = { active: 0, idle: 1, stale: 2, dead: 3 };
    const targetables = [...state.members.values()]
      .filter(isTargetable)
      // In a conversation view the bar is restricted to that conversation's
      // participants — offering the rest of the channel would let a send
      // silently escape the scope the view promises.
      .filter(m => !CONV_MODE || state.convIds.has(m.id))
      .sort((a, b) => {
        const oa = order[a.status] ?? 4;
        const ob = order[b.status] ?? 4;
        if (oa !== ob) return oa - ob;
        return (a.name || '').localeCompare(b.name || '');
      });
    state.targetOrder = targetables.map(m => m.id);
    // Drop stale selections for members who left the channel. Skip pruning
    // before the first roster snapshot arrives — the Map is empty then and
    // we'd clobber a restored-from-localStorage selection.
    if (state.members.size > 0) {
      let mutated = false;
      for (const id of [...state.selectedTargets]) {
        if (!state.members.has(id) || !isTargetable(state.members.get(id))) {
          state.selectedTargets.delete(id);
          mutated = true;
        }
      }
      if (mutated) savePersistedTargets();
    }

    if (targetables.length === 0) {
      const lbl = document.createElement('span');
      lbl.className = 'tb-label';
      lbl.textContent = 'no agents in channel yet';
      targetBar.appendChild(lbl);
      return;
    }
    const lbl = document.createElement('span');
    lbl.className = 'tb-label';
    lbl.textContent = 'send to:';
    targetBar.appendChild(lbl);

    targetables.forEach((m, idx) => {
      const pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'tb-pill' + (state.selectedTargets.has(m.id) ? ' on' : '');
      const a = animalFor(m);
      pill.innerHTML = '<span class="tb-num">' + (idx + 1) + '</span>' +
                       '<span>' + (a.emoji || '') + '</span>' +
                       '<span>' + escapeHtml(m.name || m.id) + '</span>';
      pill.title = 'click to toggle — Alt+' + (idx + 1) + ' keyboard shortcut';
      pill.addEventListener('click', () => toggleTarget(m.id));
      targetBar.appendChild(pill);
    });

    const allSelected = targetables.length > 0 &&
      targetables.every(m => state.selectedTargets.has(m.id));
    const allPill = document.createElement('button');
    allPill.type = 'button';
    allPill.className = 'tb-pill tb-all' + (allSelected ? ' on' : '');
    allPill.innerHTML = '<span class="tb-num">A</span><span>All</span>';
    allPill.title = 'toggle all targets — Alt+A';
    allPill.addEventListener('click', toggleAllTargets);
    targetBar.appendChild(allPill);

    if (state.selectedTargets.size > 0) {
      const clearPill = document.createElement('button');
      clearPill.type = 'button';
      clearPill.className = 'tb-pill';
      clearPill.textContent = CONV_MODE ? 'all in chat' : 'clear';
      clearPill.title = CONV_MODE
        ? 'reset to every participant — Alt+0'
        : 'clear selection (broadcast) — Alt+0';
      clearPill.addEventListener('click', () => {
        state.selectedTargets = CONV_MODE ? new Set(CONV_IDS) : new Set();
        savePersistedTargets();
        renderComposerTargets();
        updatePreview();
      });
      targetBar.appendChild(clearPill);
    }
  }

  function renderRoster(members) {
    applyRosterWatermarkDeltas(members);
    // Refresh the id→avatar cache so animalForId() resolves message
    // authors to the server-assigned collision-free emoji. Must run
    // before any render path that looks up avatars by id.
    rememberAvatars(members);

    // Per-member context (fingerprint-joined server-side): drives the
    // ring on each member's watermark pin.
    state.contextByMember = new Map(
      members.filter(m => m.context_pct != null).map(m => [m.id, m.context_pct]));
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

    renderComposerTargets();
    renderParticipants();
    renderRosterPickBar();
    updateAllAckBadges();
    renderWatermarkPins();
    scheduleHereUpdate();
    updateChanStats();
  }

  // ── "Start a conversation": act on the roster checkboxes ──
  // Opens a scoped view over the picked members. Ephemeral by design — the
  // conversation lives entirely in the URL, so it's shareable and
  // reload-survivable without any server-side saved-view state.
  function convUrlFor(ids, opts) {
    const qs = 'dm=' + ids.map(encodeURIComponent).join(',');
    return '/?' + qs + ((opts && opts.pane) ? '&roster=0&pane=1' : '');
  }
  function renderRosterPickBar() {
    if (!convPickBar) return;
    const ids = [...state.convPicks];
    convPickBar.hidden = ids.length === 0;
    convPickBar.innerHTML = '';
    if (!ids.length) return;

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'tb-pill on';
    open.textContent = `Start conversation (${ids.length})`;
    open.title = 'Open a view scoped to the checked members';
    open.addEventListener('click', () => {
      window.open(convUrlFor(ids), '_blank');
    });
    convPickBar.appendChild(open);

    const split = document.createElement('button');
    split.type = 'button';
    split.className = 'tb-pill';
    split.textContent = 'Split screen';
    split.title = 'Open the split-screen workspace with this conversation as the first pane';
    split.addEventListener('click', () => {
      window.open('/workspace?p=' + encodeURIComponent(ids.join(',')), '_blank');
    });
    convPickBar.appendChild(split);

    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'tb-pill';
    clear.textContent = 'clear';
    clear.addEventListener('click', () => {
      state.convPicks.clear();
      renderRosterPickBar();
      renderRoster([...state.members.values()]);
    });
    convPickBar.appendChild(clear);
  }

  // ── Participant chips: who is in this scoped conversation ──
  // The header chips are the pane's identity. In a split-screen grid the
  // composer's target bar is off at the bottom of a short pane, so the
  // header is the only place a glance can answer "which chat is this".
  function renderParticipants() {
    if (!partsEl) return;
    partsEl.innerHTML = '';
    if (!CONV_MODE) return;
    const emojis = [];
    for (const id of CONV_IDS) {
      const m = state.members.get(id);
      const chip = document.createElement('span');
      chip.className = 'pchip' + (m ? '' : ' unknown');
      if (m) {
        const a = animalFor(m);
        emojis.push(a.emoji);
        const em = document.createElement('span');
        em.className = 'pc-emoji';
        em.textContent = a.emoji;
        chip.appendChild(em);
        const nm = document.createElement('span');
        nm.textContent = m.name;
        nm.style.color = colorFor(m.id);
        chip.appendChild(nm);
        chip.title = `${m.name} (${m.id}) — the ${a.name}`;
      } else {
        // Roster hasn't arrived yet, or this member left the channel.
        chip.textContent = id;
        chip.title = `${id} — not in the current roster`;
      }
      partsEl.appendChild(chip);
    }
    // Tab title carries the emoji run so a row of pinned tabs stays legible.
    const label = `${emojis.join('')} trio#${state.channel}`;
    state.originalTitle = label;
    updateTitle();
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
      const cpct = state.contextByMember && state.contextByMember.get(mid);
      if (cpct != null) {
        const cc = cpct >= 80 ? 'var(--err)' : cpct >= 60 ? 'var(--warn)' : 'var(--accent2)';
        pin.classList.add('ctx-ringed');
        pin.style.background =
          `conic-gradient(${cc} ${Math.round(cpct)}%, var(--border) 0)`;
        pin.title += ` — context ${Math.round(cpct)}%`;
      }
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
    // Filter mode pill — "all" shown dim, "about" green, "at" amber. Helps
    // humans see at a glance who will actually hear an ambient message.
    const fm = m.filter_mode || 'all';
    if (fm && fm !== 'all') {
      const fmPill = document.createElement('span');
      fmPill.className = 'fmode ' + fm;
      fmPill.textContent = fm;
      fmPill.title = fm === 'at'
        ? 'Listening mode: at — only wakes on @pings. Ambient messages silent.'
        : 'Listening mode: about — wakes on @pings and #pounds. Ambient silent.';
      topRow.appendChild(fmPill);
    }
    // Context-window usage badge — present only for sessions on the same
    // machine as this nth_web (fed by the statusline publisher).
    if (m.context_pct != null) {
      const ctxPill = document.createElement('span');
      const pct = Math.round(m.context_pct);
      ctxPill.className = 'ctx-pct' + (pct >= 80 ? ' hot' : pct >= 60 ? ' warm' : '');
      ctxPill.textContent = pct + '%';
      ctxPill.title = 'Context window used (from this machine\'s statusline publisher)';
      topRow.appendChild(ctxPill);
    }
    // DM button — opens a filtered-view tab for this agent.
    // Hide for self, for human operator rows, and inside an existing DM tab.
    if (!CONV_MODE && m.id !== state.operator.id && !m.id.startsWith('_op_')) {
      const pick = document.createElement('span');
      pick.className = 'conv-pick' + (state.convPicks.has(m.id) ? ' on' : '');
      pick.textContent = state.convPicks.has(m.id) ? '☑' : '☐';
      pick.title = `Include ${m.name} in a new conversation`;
      pick.addEventListener('click', (e) => {
        e.stopPropagation();
        if (state.convPicks.has(m.id)) state.convPicks.delete(m.id);
        else state.convPicks.add(m.id);
        renderRosterPickBar();
        pick.classList.toggle('on', state.convPicks.has(m.id));
        pick.textContent = state.convPicks.has(m.id) ? '☑' : '☐';
      });
      topRow.appendChild(pick);

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
    if (m.context) {
      const c = m.context;
      const h = c.harness || {};
      const cw = h.context_window || {};
      const rl = h.rate_limits || {};
      // Claude snapshots nest sizes under harness; codex publisher snapshots
      // carry cw_size (and effort) at the top level.
      const cwSize = (cw.context_window_size || c.cw_size || 0);
      const cwLabel = cwSize >= 1e6 ? (cwSize/1e6)+'M' : cwSize >= 1e3 ? Math.round(cwSize/1e3)+'k' : '';
      const pct = c.used_pct != null ? Math.round(c.used_pct) + '%' : '—';
      const pctClass = (c.used_pct || 0) >= 80 ? 'bad' : (c.used_pct || 0) >= 60 ? 'warn' : 'good';
      const model = ((c.model || '').startsWith('claude-')
        ? c.model.replace(/^claude-/, '').split('-').slice(0, 2).join(' ')
        : (c.model || '')) || '—';
      const fiveH = rl.five_hour || {};
      const sevenD = rl.seven_day || {};
      const fhPct = fiveH.used_percentage != null ? Math.round(fiveH.used_percentage) + '%' : '';
      const sdPct = sevenD.used_percentage != null ? Math.round(sevenD.used_percentage) + '%' : '';
      const ctxRows = [
        ['context', `${pct} of ${cwLabel}`, pctClass],
        ['model', model, ''],
      ];
      if (c.effort) ctxRows.push(['effort', escapeHtml(c.effort), '']);
      if (fhPct) ctxRows.push(['5h limit', fhPct, (fiveH.used_percentage||0) >= 80 ? 'bad' : '']);
      if (sdPct) ctxRows.push(['7d limit', sdPct, (sevenD.used_percentage||0) >= 80 ? 'bad' : '']);
      if (c.session_name) ctxRows.push(['session', escapeHtml(c.session_name), '']);
      for (const [k2, v2, cl] of ctxRows) {
        html += `<div class="stat-row"><span class="stat-label">${k2}</span>`
             +  `<span class="stat-val ${cl}">${v2}</span></div>`;
      }
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
  // @ (ping), # (pound-reference), or ! (bang / unfilterable) trigger the popup.
  // Sigil is carried through so acceptance preserves the user's intent.
  function currentSigilToken() {
    const pos = input.selectionStart;
    const text = input.value.slice(0, pos);
    const atPos   = text.lastIndexOf('@');
    const hashPos = text.lastIndexOf('#');
    const bangPos = text.lastIndexOf('!');
    const sigilPos = Math.max(atPos, hashPos, bangPos);
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
  function resolveBangs(text)    { return resolveSigilTokens(text, '!'); }
  function updatePreview() {
    const pings = resolveMentions(input.value);
    const refs  = resolveRefs(input.value);
    const bangs = resolveBangs(input.value);
    const txtL  = (input.value || '').toLowerCase();
    const parts = [];
    if (!state.dmTargetId && state.selectedTargets.size > 0) {
      const tgts = [...state.selectedTargets]
        .map(id => state.members.get(id))
        .filter(Boolean)
        .map(m => `<span class="tgt">@${escapeHtml(m.name)}</span>`)
        .join(', ');
      parts.push(`locked targets: ${tgts}`);
    }
    if (pings.length) {
      const names = pings.map(m => `<span class="tgt">@${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`pings: ${names}`);
    }
    if (refs.length) {
      const n = refs.map(m => `<span class="tgt" style="color:#9ccf9c">#${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`refs: ${n}`);
    }
    if (bangs.length || /(^|\s)!all(\b|$)/.test(txtL)) {
      const n = bangs.map(m => `<span class="tgt" style="color:#ff8470">!${escapeHtml(m.name)}</span>`).join(', ');
      const allTag = /(^|\s)!all(\b|$)/.test(txtL) ? '<span class="tgt" style="color:#ff8470">!all</span>' : '';
      parts.push(`<b style="color:#ff8470">BANGS (unfilterable)</b>: ${[allTag, n].filter(Boolean).join(', ')}`);
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
    // A conversation view seeds its own targets from the participant set, so
    // both the old DM case and the target-bar case are the same rule at
    // different arities: force every selected id into mentions (the agent
    // must be woken even if the operator forgot the @mention) and prepend the
    // visible @name so the message reads unambiguously in main-tab backscroll.
    if (state.selectedTargets.size > 0) {
      // Persistent target bar: prepend @name for each selected agent that
      // the typed content doesn't already mention, and make sure all
      // selected ids end up in mentionIds so the server-side wake logic
      // fires. Selection is not cleared after send — it's sticky.
      const tags = [];
      for (const id of state.targetOrder) {
        if (!state.selectedTargets.has(id)) continue;
        if (!mentionIds.includes(id)) mentionIds.push(id);
        const m = state.members.get(id);
        if (!m) continue;
        const atTag = '@' + m.name;
        if (text.toLowerCase().includes(atTag.toLowerCase())) continue;
        tags.push(atTag);
      }
      if (tags.length > 0) text = tags.join(' ') + ' ' + text;
    }
    sendBtn.disabled = true;
    try {
      const r = await fetch('/api/send' + API_QS, {
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
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      if (e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key, 10) - 1;
        const id = state.targetOrder[idx];
        if (id) { toggleTarget(id); e.preventDefault(); return; }
      }
      if (e.key === '0') {
        // In a conversation, "clear" means every participant again — an
        // empty selection would broadcast outside the view's scope.
        const reset = CONV_MODE ? new Set(CONV_IDS) : new Set();
        state.selectedTargets = reset;
        savePersistedTargets();
        renderComposerTargets();
        updatePreview();
        e.preventDefault(); return;
      }
      if (e.key === 'a' || e.key === 'A') {
        toggleAllTargets(); e.preventDefault(); return;
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
    // A message belongs to this conversation when either end of it is a
    // participant: the author is in the set, or the message is addressed to
    // someone in the set. The operator counts as a participant, so their own
    // posts and anything aimed at them stay visible.
    //
    // Deliberately looser than a strict two-party DM — with three or more
    // participants, side remarks that don't @mention everyone are still part
    // of the same thread, and dropping them would silently hide context.
    if (!state.convIds.size) return true;
    const inScope = (id) => !!id && (state.convIds.has(id) || id === state.operator.id);
    if (inScope(m.member_id)) return true;
    return (m.mentions || []).some(inScope);
  }
  function applyDmFilterToNode(node, m) {
    if (!state.convIds.size) { node.classList.remove('dm-hidden'); return; }
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
    if (typeof syncSettingVisibility === 'function') syncSettingVisibility();
  });

  // ── Chime (WebAudio, no audio asset — synthesized on the fly) ──
  let _audioCtx = null;
  function ensureAudio() {
    if (_audioCtx) return _audioCtx;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      _audioCtx = AC ? new AC() : null;
    } catch (_) { _audioCtx = null; }
    return _audioCtx;
  }
  function playChime() {
    const ctx = ensureAudio();
    if (!ctx) return;
    if (ctx.state === 'suspended') { try { ctx.resume(); } catch (_) {} }
    const vol = Math.max(0, Math.min(1, state.chimeVolume));
    if (vol <= 0) return;
    try {
      const now = ctx.currentTime;
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(vol, now + 0.012);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.40);
      gain.connect(ctx.destination);
      // two-note ping: E6 -> A6
      [[1318.51, 0], [1760.0, 0.09]].forEach(([freq, t]) => {
        const osc = ctx.createOscillator();
        osc.type = 'sine';
        osc.frequency.value = freq;
        osc.connect(gain);
        osc.start(now + t);
        osc.stop(now + t + 0.28);
      });
    } catch (_) { /* ignore */ }
  }

  // ── Sound (chime) toggle — off by default; chimes on any new peer message ──
  btnSound.addEventListener('click', () => {
    state.soundEnabled = !state.soundEnabled;
    btnSound.textContent = state.soundEnabled ? '🔊 on' : '🔊 off';
    btnSound.classList.toggle('on', state.soundEnabled);
    try { localStorage.setItem('trio.sound', state.soundEnabled ? '1' : '0'); } catch (_) {}
    // The click is a user gesture — unlock the AudioContext and preview the chime.
    if (state.soundEnabled) { ensureAudio(); playChime(); }
    if (typeof syncSettingVisibility === 'function') syncSettingVisibility();
  });
  // Restore persisted preference (audio stays suspended until the first gesture).
  try {
    if (localStorage.getItem('trio.sound') === '1') {
      state.soundEnabled = true;
      btnSound.textContent = '🔊 on';
      btnSound.classList.add('on');
    }
  } catch (_) {}

  // ── Sidebar collapse toggle — persisted; 'on' pill state == roster visible ──
  const btnSide = document.getElementById('btn-side');
  const appEl = document.getElementById('app');
  function applySidebar(collapsed) {
    appEl.classList.toggle('side-collapsed', collapsed);
    btnSide.classList.toggle('on', !collapsed);
  }
  // ?roster=0/1 is a per-document view instruction and wins over the stored
  // preference; without the param we fall back to what the user last chose.
  let _sideCollapsed = false;
  if (ROSTER_PARAM !== null) {
    _sideCollapsed = ROSTER_PARAM === '0';
  } else {
    try { _sideCollapsed = localStorage.getItem('trio.sideCollapsed') === '1'; } catch (_) {}
  }
  applySidebar(_sideCollapsed);
  function toggleSidebar() {
    _sideCollapsed = !_sideCollapsed;
    applySidebar(_sideCollapsed);
    // A pane must not persist this: localStorage is shared with the main tab,
    // so a pane hiding its roster would make the main tab open collapsed too.
    if (PANE_MODE) return;
    try { localStorage.setItem('trio.sideCollapsed', _sideCollapsed ? '1' : '0'); } catch (_) {}
  }
  btnSide.addEventListener('click', () => {
    if (window.innerWidth <= 768) { toggleMobileSidebar(); } else { toggleSidebar(); }
  });
  // Keyboard shortcut: Ctrl+B toggles the roster sidebar (editor convention).
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey &&
        (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      if (window.innerWidth <= 768) { toggleMobileSidebar(); } else { toggleSidebar(); }
    }
  });

  // ── Mobile sidebar: overlay with scrim ──
  const mobileScrim = document.getElementById('mobile-scrim');
  const btnMobileRoster = document.getElementById('btn-mobile-roster');
  function toggleMobileSidebar() {
    const open = appEl.classList.toggle('mobile-side-open');
    btnSide.classList.toggle('on', open);
    if (btnMobileRoster) btnMobileRoster.classList.toggle('on', open);
  }
  if (btnMobileRoster) btnMobileRoster.addEventListener('click', toggleMobileSidebar);
  if (mobileScrim) {
    mobileScrim.addEventListener('click', () => {
      appEl.classList.remove('mobile-side-open');
      btnSide.classList.toggle('on', false);
      if (btnMobileRoster) btnMobileRoster.classList.toggle('on', false);
    });
  }
  // Auto-collapse sidebar on narrow viewports at load. An explicit ?roster=
  // wins — otherwise a pane in a split-screen grid (which is narrow by
  // construction) could never be asked to show its roster.
  if (window.innerWidth <= 768 && ROSTER_PARAM === null) {
    applySidebar(true);
  }

  // ── Settings panel: relocate controls out of the header into a ⚙ drawer ──
  // appendChild MOVES the live elements, so every existing handler/state stays
  // intact — no rewiring, no reproducing the font list.
  const btnSettings = document.getElementById('btn-settings');
  const settingsPanel = document.getElementById('settings-panel');
  [
    ['Theme', 'theme-picker'],
    ['Message font', 'font-picker'],
    ['Roster sidebar', 'btn-side'],
    ['Compact messages', 'btn-compact'],
    ['Desktop notifications', 'btn-notify'],
    ['Chime on new message', 'btn-sound'],
  ].forEach(([labelText, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const row = document.createElement('div');
    row.className = 'set-row';
    const lab = document.createElement('span');
    lab.textContent = labelText;
    row.appendChild(lab);
    row.appendChild(el);
    settingsPanel.appendChild(row);
  });

  // Extra settings built here (not relocated): chime volume + notify prefs.
  function addSettingRow(labelText, controlEl) {
    const row = document.createElement('div');
    row.className = 'set-row';
    const lab = document.createElement('span');
    lab.textContent = labelText;
    row.appendChild(lab);
    row.appendChild(controlEl);
    settingsPanel.appendChild(row);
    return row;
  }

  // Chime volume slider — drives state.chimeVolume; previews on release.
  try {
    const sv = parseFloat(localStorage.getItem('trio.chimeVolume'));
    if (!isNaN(sv)) state.chimeVolume = Math.max(0, Math.min(1, sv));
  } catch (_) {}
  const volSlider = document.createElement('input');
  volSlider.type = 'range';
  volSlider.min = '0'; volSlider.max = '1'; volSlider.step = '0.01';
  volSlider.value = String(state.chimeVolume);
  volSlider.addEventListener('input', () => {
    state.chimeVolume = parseFloat(volSlider.value) || 0;
    try { localStorage.setItem('trio.chimeVolume', String(state.chimeVolume)); } catch (_) {}
  });
  volSlider.addEventListener('change', () => { ensureAudio(); playChime(); });
  const chimeVolRow = addSettingRow('Chime volume', volSlider);

  // Notification preference dropdowns.
  function prefSelect(options, current) {
    const sel = document.createElement('select');
    options.forEach(([val, label]) => {
      const o = document.createElement('option');
      o.value = val; o.textContent = label;
      if (val === current) o.selected = true;
      sel.appendChild(o);
    });
    return sel;
  }
  try {
    const ns = localStorage.getItem('trio.notifyScope'); if (ns) state.notifyScope = ns;
    const nw = localStorage.getItem('trio.notifyWhen'); if (nw) state.notifyWhen = nw;
  } catch (_) {}
  const notifyScopeSel = prefSelect(
    [['mention', '@mentions only'], ['all', 'all messages']], state.notifyScope);
  notifyScopeSel.addEventListener('change', () => {
    state.notifyScope = notifyScopeSel.value;
    try { localStorage.setItem('trio.notifyScope', state.notifyScope); } catch (_) {}
  });
  const notifyScopeRow = addSettingRow('Notify for', notifyScopeSel);
  const notifyWhenSel = prefSelect(
    [['hidden', 'tab in background'], ['always', 'always']], state.notifyWhen);
  notifyWhenSel.addEventListener('change', () => {
    state.notifyWhen = notifyWhenSel.value;
    try { localStorage.setItem('trio.notifyWhen', state.notifyWhen); } catch (_) {}
  });
  const notifyWhenRow = addSettingRow('Notify when', notifyWhenSel);

  // Sub-settings only show when their parent feature is enabled.
  function syncSettingVisibility() {
    if (chimeVolRow) chimeVolRow.hidden = !state.soundEnabled;
    if (notifyScopeRow) notifyScopeRow.hidden = !state.notifyEnabled;
    if (notifyWhenRow) notifyWhenRow.hidden = !state.notifyEnabled;
  }
  syncSettingVisibility();

  function toggleSettings(force) {
    const show = (force !== undefined) ? force : settingsPanel.hasAttribute('hidden');
    if (show) { settingsPanel.removeAttribute('hidden'); btnSettings.classList.add('on'); }
    else { settingsPanel.setAttribute('hidden', ''); btnSettings.classList.remove('on'); }
  }
  btnSettings.addEventListener('click', (e) => { e.stopPropagation(); toggleSettings(); });
  document.addEventListener('click', (e) => {
    if (settingsPanel.hasAttribute('hidden')) return;
    if (settingsPanel.contains(e.target) || btnSettings.contains(e.target)) return;
    toggleSettings(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !settingsPanel.hasAttribute('hidden')) toggleSettings(false);
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
    es = new EventSource('/api/events' + API_QS);
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
        else if (payload.type === 'context') renderContext(payload.sessions);
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
      const r = await fetch('/api/identify' + API_QS, {
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
                   op.source === 'loopback'  ? '[local]'   :
                   op.source === 'guest'     ? '[GUEST]'   : '';
    hMeta.textContent = `posting as ${opAnimal.emoji} ${op.name} (${op.id}) — the ${opAnimal.name} ${srcTag}  ·  ${state.server_host}`;
  }

  // ── Bootstrap ──
  async function boot() {
    try {
      const r = await fetch('/api/meta' + API_QS);
      const meta = await r.json();
      state.channel = meta.channel;
      state.server_host = meta.server_host;
      loadPersistedTargets();
      renderComposerTargets();
      // The channel label stays plain — participant chips (rendered once the
      // roster lands) carry the "who is in this chat" information.
      hChannel.textContent = (CONV_MODE ? '⇄ trio#' : 'trio#') + meta.channel;
      state.originalTitle = (CONV_MODE ? '⇄ trio#' : 'trio#') + meta.channel;
      if (CONV_MODE) document.body.classList.add('dm-mode');
      if (CONV_IDS.length === 1) document.body.classList.add('conv-pair');
      renderParticipants();
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

  // ── Context rings ──
  const ctxListEl = document.getElementById('ctx-list');
  const CTX_CIRC = 2 * Math.PI * 14; // r=14 for 36px ring
  function ctxColor(pct) {
    if (pct >= 80) return 'var(--err)';
    if (pct >= 60) return 'var(--warn)';
    return 'var(--accent2)';
  }
  function ctxModelShort(m) {
    if (!m) return '';
    const p = m.replace(/^claude-/, '').split('-');
    return p[0] || m;
  }
  function renderContext(sessions) {
    if (!ctxListEl) return;
    if (!sessions || !sessions.length) {
      ctxListEl.innerHTML = '<div class="ctx-empty">no active sessions</div>';
      return;
    }
    ctxListEl.innerHTML = '';
    for (const s of sessions) {
      const pct = s.used_pct || 0;
      const color = ctxColor(pct);
      const offset = CTX_CIRC * (1 - pct / 100);
      const name = s.session_name || s.session_id || '?';
      const model = ctxModelShort(s.model);
      const cwLabel = s.cw_size >= 1000000
        ? (s.cw_size / 1000000) + 'M'
        : s.cw_size >= 1000 ? Math.round(s.cw_size / 1000) + 'k' : '';
      const age = s._age_s || 0;
      const fresh = age < 30;

      const card = document.createElement('div');
      card.className = 'ctx-card';
      card.title = `${Math.round(pct)}% of ${cwLabel} context · ${model} · ${age}s ago`;
      card.style.opacity = fresh ? '1' : '0.5';
      card.innerHTML = `
        <div class="ctx-ring">
          <svg viewBox="0 0 36 36">
            <circle class="track" cx="18" cy="18" r="14"/>
            <circle class="fill" cx="18" cy="18" r="14"
              stroke="${color}"
              stroke-dasharray="${CTX_CIRC}"
              stroke-dashoffset="${offset}"/>
          </svg>
          <div class="pct-text">${Math.round(pct)}</div>
        </div>
        <div class="ctx-info">
          <div class="ctx-name">${escapeHtml(name)}</div>
          <div class="ctx-meta">
            <span class="ctx-model">${escapeHtml(model)}</span>
            ${cwLabel ? ' · ' + cwLabel : ''}
          </div>
        </div>`;
      ctxListEl.appendChild(card);
    }
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


# ───────── Landing page (served as / in landing mode) ─────────
# Fleet strip + node check-ins + channel index. Renders exclusively through
# DOM APIs (textContent) — channel codes and hostnames are DB strings and
# must never hit innerHTML.
LANDING_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nth — fleet</title>
<style>
  :root {
    --bg: #101318; --panel: #171b22; --border: #262c37;
    --fg: #d7dde6; --dim: #79839a; --accent: #62d7ef;
    --ok: #7ede7e; --warn: #e5d35e; --bad: #ff8470;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.45 ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace;
    padding: 1.2rem; max-width: 900px; margin-inline: auto;
  }
  h1 { font-size: 1.05rem; margin: 0; letter-spacing: .04em; }
  h1 .v { color: var(--dim); font-weight: normal; font-size: .85rem; }
  h2 { font-size: .8rem; color: var(--dim); text-transform: uppercase;
       letter-spacing: .12em; margin: 1.6rem 0 .5rem; }
  header { display: flex; align-items: baseline; gap: .8rem; flex-wrap: wrap; }
  #strip { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .9rem; }
  .pill { background: var(--panel); border: 1px solid var(--border);
          border-radius: 999px; padding: .15rem .7rem; font-size: .8rem; }
  .pill b { font-weight: 600; }
  .ok   { color: var(--ok); }
  .warn { color: var(--warn); }
  .bad  { color: var(--bad); }
  .dim  { color: var(--dim); }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: .3rem .6rem; font-size: .85rem;
           border-bottom: 1px solid var(--border); }
  th { color: var(--dim); font-weight: normal; font-size: .72rem;
       text-transform: uppercase; letter-spacing: .1em; }
  td.num, th.num { text-align: right; }
  .dot { display: inline-block; width: .55em; height: .55em;
         border-radius: 50%; margin-right: .45em; vertical-align: baseline; }
  .dot.live  { background: var(--ok); }
  .dot.idle  { background: var(--dim); }
  .dot.ended { background: transparent; border: 1px solid var(--dim); }
  a.chan { color: var(--accent); text-decoration: none; }
  a.chan:hover { text-decoration: underline; }
  tr.ended td { color: var(--dim); }
  #err { color: var(--bad); margin-top: 1rem; display: none; }
  footer { color: var(--dim); font-size: .72rem; margin-top: 2rem; }
  #ctx-strip { display: flex; flex-wrap: wrap; gap: .9rem; }
  .ctxs { display: flex; align-items: center; gap: .5rem;
          background: var(--panel); border: 1px solid var(--border);
          border-radius: 8px; padding: .35rem .6rem; }
  .ctxs svg { width: 34px; height: 34px; transform: rotate(-90deg); flex: none; }
  .ctxs .track { fill: none; stroke: var(--border); stroke-width: 4; }
  .ctxs .arc { fill: none; stroke-width: 4; stroke-linecap: round; }
  .ctxs .who { font-size: .8rem; }
  .ctxs .sub { font-size: .68rem; color: var(--dim); }
  .ctxs.stale { opacity: .45; }
  #ctx-strip .none { color: var(--dim); font-size: .8rem; }
</style>
</head>
<body>
<header>
  <h1>nth <span class="dim">//</span> fleet <span class="v" id="hdr-host"></span></h1>
</header>
<div id="strip"></div>
<div id="err"></div>
<h2>Sessions <span class="dim" style="font-size:.65rem">(this host)</span></h2>
<div id="ctx-strip"></div>
<h2>Nodes</h2>
<table id="nodes"><thead><tr>
  <th>host</th><th>transport</th><th>version</th><th>python</th><th class="num">seen</th>
</tr></thead><tbody></tbody></table>
<h2>Channels</h2>
<table id="channels"><thead><tr>
  <th>channel</th><th class="num">members</th><th class="num">live</th>
  <th class="num">msgs</th><th class="num">activity</th>
</tr></thead><tbody></tbody></table>
<footer id="foot"></footer>
<script>
  function ageStr(s) {
    if (s === null || s === undefined) return 'never';
    if (s < 90) return s + 's';
    if (s < 5400) return Math.floor(s / 60) + 'm';
    if (s < 172800) return (s / 3600).toFixed(1) + 'h';
    return (s / 86400).toFixed(1) + 'd';
  }
  function pill(html_free_text, cls) {
    const el = document.createElement('span');
    el.className = 'pill' + (cls ? ' ' + cls : '');
    el.textContent = html_free_text;
    return el;
  }
  function td(text, cls) {
    const el = document.createElement('td');
    if (cls) el.className = cls;
    el.textContent = text;
    return el;
  }
  async function refresh() {
    let d;
    try {
      const r = await fetch('/api/landing');
      d = await r.json();
    } catch (e) {
      document.getElementById('err').style.display = 'block';
      document.getElementById('err').textContent = 'landing fetch failed: ' + e;
      return;
    }
    document.getElementById('err').style.display = 'none';
    document.getElementById('hdr-host').textContent =
      d.host + ' · v' + d.version;

    const liveMembers = d.channels.reduce((a, c) => a + c.live, 0);
    const liveNodes = d.nodes.filter(n => n.live).length;
    const activeCh = d.channels.filter(c => c.status === 'active').length;
    const strip = document.getElementById('strip');
    strip.replaceChildren(
      pill(d.db_ok ? 'db ok' : 'DB DOWN', d.db_ok ? 'ok' : 'bad'),
      pill(activeCh + ' active channels'),
      pill(liveMembers + ' live members', liveMembers ? 'ok' : ''),
      pill('nodes ' + liveNodes + '/' + d.nodes.length + ' live',
           liveNodes ? 'ok' : 'warn'),
    );

    const ctxStrip = document.getElementById('ctx-strip');
    const CIRC = 2 * Math.PI * 14;
    const sessions = d.context_sessions || [];
    if (!sessions.length) {
      const none = document.createElement('span');
      none.className = 'none';
      none.textContent = 'no publishing sessions on this host';
      ctxStrip.replaceChildren(none);
    } else {
      ctxStrip.replaceChildren(...sessions.map(s => {
        const pct = Math.round(s.used_pct || 0);
        const card = document.createElement('div');
        card.className = 'ctxs' + ((s._age_s || 0) > 30 ? ' stale' : '');
        const svgNS = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(svgNS, 'svg');
        svg.setAttribute('viewBox', '0 0 36 36');
        const track = document.createElementNS(svgNS, 'circle');
        track.setAttribute('class', 'track');
        ['cx','cy','r'].forEach((a,i)=>track.setAttribute(a,[18,18,14][i]));
        const arc = document.createElementNS(svgNS, 'circle');
        arc.setAttribute('class', 'arc');
        ['cx','cy','r'].forEach((a,i)=>arc.setAttribute(a,[18,18,14][i]));
        arc.setAttribute('stroke', pct >= 80 ? 'var(--bad)' : pct >= 60 ? 'var(--warn)' : 'var(--ok)');
        arc.setAttribute('stroke-dasharray', String(CIRC));
        arc.setAttribute('stroke-dashoffset', String(CIRC * (1 - pct / 100)));
        svg.append(track, arc);
        const info = document.createElement('div');
        const who = document.createElement('div');
        who.className = 'who';
        who.textContent = (s.session_name || s.session_id || '?') + ' · ' + pct + '%';
        const sub = document.createElement('div');
        sub.className = 'sub';
        const cw = s.cw_size >= 1e6 ? (s.cw_size/1e6) + 'M' : Math.round((s.cw_size||0)/1e3) + 'k';
        sub.textContent = (s.model || '').replace(/^claude-/, '') + ' · ' + cw;
        info.append(who, sub);
        card.append(svg, info);
        return card;
      }));
    }

    const ntb = document.querySelector('#nodes tbody');
    ntb.replaceChildren(...d.nodes.map(n => {
      const tr = document.createElement('tr');
      const hostCell = td('');
      const dot = document.createElement('span');
      dot.className = 'dot ' + (n.live ? 'live' : 'idle');
      hostCell.append(dot, document.createTextNode(n.hostname));
      tr.append(hostCell, td(n.transport),
                td(n.nth_version ? 'v' + n.nth_version : '?'),
                td(n.python || '?'),
                td(ageStr(n.age_s), 'num ' + (n.live ? 'ok' : 'dim')));
      return tr;
    }));

    const ctb = document.querySelector('#channels tbody');
    ctb.replaceChildren(...d.channels.map(c => {
      const tr = document.createElement('tr');
      if (c.status === 'ended') tr.className = 'ended';
      const cCell = td('');
      const dot = document.createElement('span');
      dot.className = 'dot ' +
        (c.status === 'ended' ? 'ended' : (c.live > 0 ? 'live' : 'idle'));
      const a = document.createElement('a');
      a.className = 'chan';
      a.href = '/c/' + encodeURIComponent(c.code);
      a.textContent = c.code;
      cCell.append(dot, a);
      if (c.status === 'ended') {
        cCell.append(document.createTextNode(' (ended)'));
      }
      tr.append(cCell, td(String(c.members), 'num'),
                td(String(c.live), 'num ' + (c.live ? 'ok' : 'dim')),
                td(String(c.msgs), 'num'),
                td(ageStr(c.last_msg_age_s), 'num'));
      return tr;
    }));

    document.getElementById('foot').textContent =
      'db: ' + d.db + ' · refreshed ' + new Date().toLocaleTimeString();
  }
  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ───────── Entry ─────────
class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Ignore expected disconnects from tab closes, refreshes, and SSE retries."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main() -> int:
    ap = argparse.ArgumentParser(description="Web dashboard for a trio channel.")
    ap.add_argument("channel", nargs="?", default=None,
                    help="Channel code to observe. Omit to serve the landing "
                         "page instead: fleet health + channel index at /, "
                         "with every channel's dashboard at /c/<code>.")
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

    # Single-channel mode spins up its one event hub before serving.
    # Landing mode creates hubs lazily, one per channel actually viewed.
    hub = None
    if args.channel:
        hub = EventHub(db_path, args.channel)
        hub.start()
        NthWebHandler.hub = hub
        NthWebHandler.channel = args.channel
    else:
        NthWebHandler.landing_mode = True
    NthWebHandler.db_path = db_path

    # Let multiple channel dashboards start without manual port coordination.
    requested_port = args.port
    port = requested_port
    server = None
    for _ in range(50):
        try:
            server = QuietThreadingHTTPServer((host, port), NthWebHandler)
            break
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                port += 1
                continue
            raise
    if server is None:
        sys.stderr.write(
            f"No free port found in {requested_port}..{requested_port + 49}\n")
        return 1
    # Threaded server handles one SSE connection per thread; don't let them
    # keep the process alive on Ctrl-C.
    server.daemon_threads = True

    def stop_hubs():
        if hub is not None:
            hub.stop()
        with NthWebHandler.hubs_lock:
            for h in NthWebHandler.hubs.values():
                h.stop()

    def shutdown(_sig=None, _frm=None):
        stop_hubs()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)

    # Banner
    ts_ip = get_tailscale_ip()
    print("nth_web serving:")
    print(f"  channel:     {args.channel or '(landing page — all channels at /c/<code>)'}")
    print(f"  db:          {db_path}")
    if port != requested_port:
        print(f"  note:        port {requested_port} was busy — using {port} instead")
    print(f"  bound on:    http://{host}:{port}/")
    print(f"  localhost:   http://127.0.0.1:{port}/")
    if ts_ip and host in ("0.0.0.0",):
        print(f"  tailnet:     http://{ts_ip}:{port}/   (visible to tailnet peers)")
    elif ts_ip:
        print(f"  tailnet IP:  {ts_ip}   (pass --tailnet to bind)")
    print("  Ctrl-C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_hubs()

    return 0


if __name__ == "__main__":
    sys.exit(main())
