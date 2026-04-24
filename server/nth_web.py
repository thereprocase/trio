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
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import ANIMAL_EMOJIS, animal_for, animal_for_channel


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
        except sqlite3.OperationalError:
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
        # Collision-free avatars per channel. Sorted-id assignment in
        # animal_for_channel() makes the mapping stable across roster
        # refreshes as long as the member set is fixed; joins/leaves
        # may reshuffle affected members, which the client handles by
        # keying on the emoji/name fields we ship instead of hashing.
        avatars = animal_for_channel([r["id"] for r in rows])
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
            aname, aemoji = avatars.get(r["id"], animal_for(r["id"]))
            out.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "status_text": r["status_text"] or "",
                "last_seen": effective_last_seen,
                "last_read": effective_last_read,
                "filter_mode": fm or "all",
                "status": member_status(effective_last_seen, r["status_text"] or ""),
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
                op_id, op_name = ensure_operator_row(db, self.channel, ident)
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
                        (self.channel, op_id, task_body, now, now),
                    )
                    task_id = tcur.lastrowid
                    posted_content = f"[task #{task_id}] {task_body}"

                # Server-side parse the three sigils against the current roster,
                # matching nth_send's behavior so web-operator posts carry the
                # same wake semantics as MCP-agent posts.
                mention_ids, ref_ids, bang_ids = _parse_sigils_against_roster(
                    db, self.channel, posted_content
                )
                cursor = db.execute(
                    "INSERT INTO messages "
                    "(channel, member_id, member_name, content, created_at, "
                    " mentions, refs, bangs) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.channel, op_id, op_name, posted_content, now,
                     json.dumps(mention_ids) if mention_ids else "",
                     json.dumps(ref_ids)     if ref_ids     else "",
                     json.dumps(bang_ids)    if bang_ids    else ""),
                )
                msg_id = cursor.lastrowid
                db.execute(
                    "UPDATE members SET last_seen = ? WHERE channel = ? AND id = ?",
                    (now, self.channel, op_id),
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
    --bg: #0b0f14; --bg2: #121821; --panel: #161d27; --border: #273040;
    --fg: #d8dde6; --dim: #7a8596; --dimmer: #4a5262;
    --accent: #3ba0e6; --accent2: #59cb79; --warn: #e3c34c; --err: #e56a4a;
    --mention: #e3c34c;
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
  header #font-picker { background: var(--panel); color: var(--fg); border: 1px solid var(--border);
                        padding: 3px 6px; border-radius: 3px; font-family: inherit; font-size: 11px;
                        cursor: pointer; }
  header #font-picker:focus { outline: none; border-color: var(--accent); }

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
  #chat .msg .body code.mdic { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.1);
                         border-radius: 3px; padding: 0 4px; font-family: ui-monospace, Menlo, Monaco, monospace;
                         font-size: 0.92em; }
  #chat .msg .body pre.mdcode { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
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
  .msg .body h1 { font-size: 1.35em; border-bottom: 1px solid rgba(255,255,255,0.15); padding-bottom: 2px; }
  .msg .body h2 { font-size: 1.2em; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 2px; }
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
                          background: rgba(255,255,255,0.03); color: rgba(255,255,255,0.85); }
  .msg .body hr { border: 0; border-top: 1px solid rgba(255,255,255,0.18); margin: 8px 0; }
  .msg .body table { border-collapse: collapse; margin: 4px 0; font-size: 0.95em; }
  .msg .body th, .msg .body td { border: 1px solid rgba(255,255,255,0.15); padding: 3px 8px; }
  .msg .body th { background: rgba(255,255,255,0.06); font-weight: 700; text-align: left; }
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
  body.dm-mode #target-bar { display: none; }
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
      <option value='ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'>System mono</option>
    </select>
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
    <div id="target-bar"></div>
    <div id="input-row">
      <div id="completions"></div>
      <textarea id="input" rows="1" placeholder="Type a message. @ to mention, $task <desc> to post a claimable task. Enter to send, Shift+Enter for newline."></textarea>
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
    // Persistent target selection: set of member_ids that every send is
    // addressed to (prepended as @name mentions). Empty = broadcast.
    selectedTargets: new Set(),
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
          body: humanizeIdSigils(m.content || '').slice(0, 140),
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
  function loadPersistedTargets() {
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
      clearPill.textContent = 'clear';
      clearPill.title = 'clear selection (broadcast) — Alt+0';
      clearPill.addEventListener('click', () => {
        state.selectedTargets.clear();
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
    } else if (state.selectedTargets.size > 0) {
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
    if (e.altKey && !e.ctrlKey && !e.metaKey && !state.dmTargetId) {
      if (e.key >= '1' && e.key <= '9') {
        const idx = parseInt(e.key, 10) - 1;
        const id = state.targetOrder[idx];
        if (id) { toggleTarget(id); e.preventDefault(); return; }
      }
      if (e.key === '0') {
        if (state.selectedTargets.size > 0) {
          state.selectedTargets.clear();
          savePersistedTargets();
          renderComposerTargets();
          updatePreview();
        }
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
                   op.source === 'loopback'  ? '[local]'   :
                   op.source === 'guest'     ? '[GUEST]'   : '';
    hMeta.textContent = `posting as ${opAnimal.emoji} ${op.name} (${op.id}) — the ${opAnimal.name} ${srcTag}  ·  ${state.server_host}`;
  }

  // ── Bootstrap ──
  async function boot() {
    try {
      const r = await fetch('/api/meta');
      const meta = await r.json();
      state.channel = meta.channel;
      state.server_host = meta.server_host;
      loadPersistedTargets();
      renderComposerTargets();
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
