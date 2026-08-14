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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from nth_constants import (ANIMAL_EMOJIS, animal_for, animal_for_channel,
                           NTH_VERSION, project_context)


# ───────── Config ─────────
DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"
DEFAULT_PORT = 8765
DB_POLL_INTERVAL = 0.5
HISTORY_LIMIT = 200          # messages sent to a client on /api/history
HUB_IDLE_REAP_S = 300        # retire a channel's EventHub after this long unwatched
SSE_HEARTBEAT_SEC = 20       # keep-alive comment interval

# ── Image attachments (Phase-1 prototype) ──
# Attachments live beside the database they belong to, NOT at a fixed path.
# A hardcoded location means --db does not isolate anything: pointing the server
# at a scratch DB still reads and DELETES files belonging to the real one, which
# is a live footgun for anyone testing the GC below.
ATTACH_DIR = Path.home() / ".claude" / "nth" / "attachments"


def attach_dir_for(db_path: Path) -> Path:
    """Attachment root for a given database file."""
    return Path(db_path).resolve().parent / "attachments"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024     # 10 MB hard cap per image
# Total attachment bytes one member may hold in one channel. The per-image cap
# bounds a single request; nothing bounded the SUM, so any identity allowed to
# upload could fill the disk one legal 10 MB image at a time. sweep_attachments
# only reclaims UNLINKED rows, so anything linked to a message is permanent --
# this quota is the only bound on an upload right.
MAX_MEMBER_ATTACH_BYTES = int(os.environ.get("NTH_ATTACH_QUOTA_BYTES", 200 * 1024 * 1024))
# Attachment GC. An upload creates its row UNLINKED and /api/send links it, so
# anything still unlinked long afterwards was abandoned — a paste thought better
# of, a closed tab, a failed send. Nothing ever collected those, so they
# accumulated on disk for the life of the install.
ATTACH_GC_GRACE_S = 24 * 3600      # an unlinked upload is abandoned after this
ATTACH_GC_MIN_INTERVAL_S = 600     # at most one sweep per process per 10 min
ATTACH_GC_MAX_DELETES = 500        # deletions per sweep
ATTACH_GC_MAX_SCAN = 2000          # files stat'd per sweep, resumed round-robin
ALLOWED_IMAGE_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}
STALE_SECONDS = 300          # fresh heartbeat threshold
DEAD_SECONDS = 900           # no heartbeat this long → dead
SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")
OPERATOR_MEMBER_ID_PREFIX = "_op_"
OPERATOR_NAME_FALLBACK = "Operator"
OP_COOKIE = "nth_op"
OP_COOKIE_MAX_AGE = 60 * 60 * 24 * 30   # 30 days
OP_PENDING_TTL_S = 60 * 60              # drop un-resolved 'pending' identities
OP_REGISTRY_MAX = 5000                  # hard cap, oldest evicted first
IDENTITY_SOURCE_TAILSCALE = "tailscale"
IDENTITY_SOURCE_LOOPBACK = "loopback"
IDENTITY_SOURCE_GUEST = "guest"
IDENTITY_SOURCE_PENDING = "pending"
# Agents reading the roster can check the member's summary field:
#   "human — tailnet: alice"          → identity-traceable via Tailscale
#   "human — local (user: alice)"     → connected via loopback; trust level is
#                                       "already has a shell on this box"
#   "human — GUEST (self-declared)"   → untrusted self-declared identity
# Neither replaces direct hub-console input.

# Identity tiers allowed to perform destructive, roster-wide actions (cull).
# A self-declared guest is deliberately excluded — see _handle_cull.
CULL_ALLOWED_SOURCES = (IDENTITY_SOURCE_LOOPBACK, IDENTITY_SOURCE_TAILSCALE)
# Identity tiers allowed to inspect or reveal paths on the operator's own
# filesystem. A self-declared guest is excluded: these endpoints answer
# questions about local disk, and the server can bind 0.0.0.0 under --tailnet.
LOCAL_PATH_ALLOWED_SOURCES = (IDENTITY_SOURCE_LOOPBACK, IDENTITY_SOURCE_TAILSCALE)

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
            return "human — GUEST (self-declared)"
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
            self._evict_locked()

    def _evict_locked(self) -> None:
        """Bound the registry. Every cookie-less request mints a token and
        stores a 'pending' identity, so without eviction an unauthenticated
        client (or a scanner) grows this dict until the process dies.
        Pending entries expire on a timer; resolved ones only when the hard
        cap is hit, oldest first, since losing one just re-prompts a human.
        """
        now = time.time()
        for tok, ident in list(self._by_token.items()):
            created = getattr(ident, "created_at", None)
            if created is None:
                continue
            if (ident.source == IDENTITY_SOURCE_PENDING
                    and now - created > OP_PENDING_TTL_S):
                del self._by_token[tok]
        if len(self._by_token) > OP_REGISTRY_MAX:
            oldest = sorted(
                self._by_token.items(),
                key=lambda kv: getattr(kv[1], "created_at", 0) or 0,
            )
            for tok, _ in oldest[: len(self._by_token) - OP_REGISTRY_MAX]:
                del self._by_token[tok]

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


def _iso_secs(iso: Optional[str]) -> Optional[float]:
    """Parse an ISO 8601 timestamp to epoch seconds, or None if unusable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def member_status(last_seen_iso: Optional[str], status_text: str,
                  session_activity_iso: Optional[str] = None,
                  last_turn_end_iso: Optional[str] = None) -> str:
    """Classify a member for the roster dot.

    States: working / active / idle / stale / dead.
      dead    — no heartbeat for DEAD_SECONDS (process gone).
      stale   — heartbeat aging (> STALE_SECONDS).
      idle    — alive, but its last turn has ended (nothing since) or it set a
                sleeping status_text: "done / waiting on you".
      working — alive AND it has acted since its last turn end (mid-turn). This
                is the pulsing "keep chilling, it's on it" dot; it needs the
                nth_turn_hook to have recorded a turn end. "Acted" means its
                sessions.last_seen advanced past that turn end. With the
                nth_activity_hook installed (PreToolUse + UserPromptSubmit),
                *any* tool call or prompt bumps last_seen, so this holds for the
                whole active turn — reasoning, a long Bash, a sub-agent — not
                just from the agent's first trio call. Without the activity hook
                only trio RPCs bump last_seen, so a turn that makes zero trio
                calls would read idle until its Stop hook fires.
      active  — alive but we have no turn data (hook not installed): the legacy
                green dot, so hook-less deployments are unchanged.
    """
    ls = _iso_secs(last_seen_iso)
    if ls is None:
        return "dead"
    age = datetime.now(timezone.utc).timestamp() - ls
    if age > DEAD_SECONDS:
        return "dead"
    if age > STALE_SECONDS:
        return "stale"
    if status_text and any(kw in status_text.lower() for kw in SLEEPING_KEYWORDS):
        return "idle"
    # Turn-state split — only when the turn hook has recorded an end for this
    # member. Acted since that end -> mid-turn -> working; otherwise finished.
    end = _iso_secs(last_turn_end_iso)
    if end is not None:
        # A backward wall-clock step (NTP correction, host sleep/wake) can leave
        # a Stop stamp in the future. No later activity can then exceed it, so
        # the member would read idle while genuinely working. Treat a turn end
        # that is ahead of now as no turn data at all.
        if end > datetime.now(timezone.utc).timestamp() + 1:
            return "active"
        act = _iso_secs(session_activity_iso)
        return "working" if (act is not None and act > end) else "idle"
    return "active"  # no turn data (hook not installed) — legacy behavior


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


def cull_member(db: sqlite3.Connection, channel: str, caller_id: str,
                caller_name: str, target_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Remove a member from a channel — mirrors nth_server.nth_cull so the web
    dashboard can offer it directly. Deletes the target's row, releases their
    claimed tasks back to open, drops their locks, and posts a [culled] system
    message. Returns (result, error) with exactly one non-None. Must run inside
    the caller's transaction."""
    target = db.execute(
        "SELECT id, name FROM members WHERE id = ? AND channel = ?",
        (target_id, channel),
    ).fetchone()
    if not target:
        return None, "member not found in this channel"
    if target_id == caller_id:
        return None, "you can't remove yourself"
    now = now_iso()
    target_name = target["name"]

    released = db.execute(
        "SELECT id FROM tasks WHERE channel = ? AND claimed_by = ? AND status = 'claimed'",
        (channel, target_id),
    ).fetchall()
    db.execute(
        "UPDATE tasks SET claimed_by = NULL, status = 'open', updated_at = ? "
        "WHERE channel = ? AND claimed_by = ? AND status = 'claimed'",
        (now, channel, target_id),
    )
    # Read the held locks before dropping them so the notice can name them —
    # otherwise the operator gets no record of what was released.
    released_locks = [r["resource"] for r in db.execute(
        "SELECT resource FROM locks WHERE channel = ? AND held_by = ?",
        (channel, target_id)).fetchall()]
    db.execute("DELETE FROM locks WHERE channel = ? AND held_by = ?", (channel, target_id))
    db.execute("DELETE FROM members WHERE id = ? AND channel = ?", (target_id, channel))
    # Revoke their sessions so a lingering token can't be reused if the same
    # member_id ever re-joins (defence-in-depth; also stops row build-up).
    db.execute(
        "UPDATE sessions SET revoked_at = ? WHERE channel = ? AND member_id = ? "
        "AND revoked_at IS NULL",
        (now, channel, target_id),
    )

    released_ids = [r["id"] for r in released]
    # Name the operator: this renders as an author-less system line, so without
    # it someone returning to the channel can see a member was removed but not
    # by whom — for an irreversible action that is the first thing they ask.
    msg = f"[culled] {target_name} ({target_id}) removed from channel by {caller_name}"
    if released_ids:
        msg += " — released tasks: " + ", ".join(f"#{t}" for t in released_ids)
    if released_locks:
        msg += " — released locks: " + ", ".join(released_locks)
    db.execute(
        "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (channel, caller_id, caller_name, msg, now),
    )
    return {"culled": target_name, "culled_id": target_id,
            "released_tasks": released_ids,
            "released_locks": released_locks}, None


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Real image MIME from magic bytes, or None if not a supported image.
    We trust the sniffed type over the client-declared Content-Type."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_last_attach_gc = 0.0
_attach_gc_cursor = 0        # resume point for the bounded orphan walk
_attach_gc_lock = threading.Lock()


def _unlink_quietly(path: Path) -> bool:
    """Delete a file, but only if it lives under the CURRENT attachment root.

    attachments.path stores an absolute path, so a database pointed at by --db
    can name files belonging to a different install. Without this check, running
    the server against a scratch copy of a DB deletes the REAL files its rows
    happen to reference — which is exactly how this check came to be written.
    """
    try:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(ATTACH_DIR.resolve()):
            return False
        resolved.unlink()
        return True
    except (OSError, ValueError):
        return False


def sweep_attachments(db_path: Path, force: bool = False) -> Dict[str, int]:
    """Collect attachments nothing can reach any more.

    Three kinds, all of which leaked before this existed:
      * abandoned uploads — still unlinked ATTACH_GC_GRACE_S after creation.
      * attachments of a channel that no longer exists — nth_cleanup deletes a
        channel's messages and members but never its attachments.
      * orphan files — a crash between writing the file and inserting its row
        leaves bytes on disk that nothing references.

    Rows are deleted BEFORE their files: a crash in between leaves an orphan
    file, which the third sweep reclaims. The other order would leave a row
    pointing at nothing, which is a visibly broken image instead.

    Opportunistic — called from the upload path and at startup, rate-limited so
    a burst of uploads does not sweep repeatedly. Mirrors the idle-hub reaper
    rather than adding a thread. Returns counts for logging/tests.
    """
    global _last_attach_gc
    now = time.time()
    with _attach_gc_lock:
        if not force and (now - _last_attach_gc) < ATTACH_GC_MIN_INTERVAL_S:
            return {"skipped": 1}
        _last_attach_gc = now

    stats = {"abandoned": 0, "dead_channel": 0, "orphan_files": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ATTACH_GC_GRACE_S)
    cutoff_iso = cutoff.isoformat()
    db = None
    try:
        db = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=3000")
        ensure_attachments_table(db)

        # Two independent queries with their own budgets. A single UNION ALL
        # with one LIMIT let the first branch consume the whole budget, so a
        # dead channel's disk was never freed while any abandoned backlog
        # existed — and a row matching BOTH predicates came back twice.
        half = max(1, ATTACH_GC_MAX_DELETES // 2)
        doomed = [
            (r["id"], r["path"], "abandoned") for r in db.execute(
                "SELECT id, path FROM attachments "
                " WHERE message_id IS NULL AND created_at < ? LIMIT ?",
                (cutoff_iso, half)).fetchall()
        ]
        seen = {d[0] for d in doomed}
        for r in db.execute(
                "SELECT a.id AS id, a.path AS path FROM attachments a "
                " LEFT JOIN channels c ON c.code = a.channel "
                " WHERE c.code IS NULL LIMIT ?",
                (ATTACH_GC_MAX_DELETES - len(doomed),)).fetchall():
            if r["id"] not in seen:
                doomed.append((r["id"], r["path"], "dead_channel"))

        # One transaction for the batch. Autocommitting each delete took the WAL
        # writer lock up to 500 times per sweep, interleaved with unlink()
        # syscalls — measured as ~770ms tail latency on unrelated concurrent
        # writes (message sends, task claims) for as long as the sweep ran.
        # Rows still go before files: the commit lands first, then the unlinks,
        # so a crash in between leaves an orphan file the walk reclaims.
        if doomed:
            # Compare-and-swap on the state that made each row doomed. The
            # select and the delete are separate statements, so a row can be
            # LINKED by a concurrent /api/send in between — and _handle_send
            # holds BEGIN IMMEDIATE, so an unconditional delete does not race
            # it, it queues behind it and then destroys the attachment the user
            # just successfully posted. Only rows still in the observed state
            # are deleted, and only rows we actually deleted get their file
            # unlinked.
            confirmed = []
            db.execute("BEGIN IMMEDIATE")
            try:
                for att_id, att_path, why in doomed:
                    if why == "abandoned":
                        cur = db.execute(
                            "DELETE FROM attachments "
                            " WHERE id = ? AND message_id IS NULL", (att_id,))
                    else:
                        cur = db.execute(
                            "DELETE FROM attachments WHERE id = ? AND NOT EXISTS "
                            " (SELECT 1 FROM channels c WHERE c.code = "
                            "  (SELECT channel FROM attachments WHERE id = ?))",
                            (att_id, att_id))
                    if cur.rowcount:
                        confirmed.append((att_path, why))
                db.execute("COMMIT")
            except sqlite3.Error:
                db.execute("ROLLBACK")
                raise
            # Files only after the rows are durably gone.
            for att_path, why in confirmed:
                _unlink_quietly(Path(att_path))
                stats[why] += 1

        # Orphan files, only ones older than the grace period. NB the upload
        # path inserts its row FIRST (with an empty path), then writes the file,
        # then fills the path in — so the window this guards is not "file
        # written before its row exists" but the gap between the `known`
        # snapshot below and the walk that follows it. That is seconds; the
        # grace covers it by orders of magnitude.
        #
        # Walk a BOUNDED slice of the tree per sweep, resuming where the last
        # one stopped. Loading every path and stat'ing every file made the cost
        # scale with total historical attachments rather than with garbage —
        # measured at ~1.2s on a 150k-attachment install that had nothing to
        # collect. Coverage is still complete, just spread over several sweeps.
        global _attach_gc_cursor
        try:
            chan_dirs = sorted(d for d in ATTACH_DIR.iterdir() if d.is_dir())
        except OSError:
            chan_dirs = []
        if chan_dirs:
            start = _attach_gc_cursor % len(chan_dirs)
            order = chan_dirs[start:] + chan_dirs[:start]
            scanned = 0
            deletes = ATTACH_GC_MAX_DELETES
            visited = 0
            for chan_dir in order:
                if scanned >= ATTACH_GC_MAX_SCAN or deletes <= 0:
                    break
                visited += 1
                # Only this channel's paths, so the set stays proportional to
                # the slice being walked (indexed by channel).
                known = {r["path"] for r in db.execute(
                    "SELECT path FROM attachments WHERE channel = ?",
                    (chan_dir.name,))}
                try:
                    entries = list(chan_dir.iterdir())
                except OSError:
                    continue
                for f in entries:
                    if scanned >= ATTACH_GC_MAX_SCAN or deletes <= 0:
                        break
                    scanned += 1
                    if str(f) in known:
                        continue
                    try:
                        if not f.is_file():
                            continue
                        if (now - f.stat().st_mtime) < ATTACH_GC_GRACE_S:
                            continue
                    except OSError:
                        continue
                    if _unlink_quietly(f):
                        stats["orphan_files"] += 1
                        deletes -= 1
            _attach_gc_cursor = (start + visited) % len(chan_dirs)
    except sqlite3.Error:
        return stats
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
    return stats


def ensure_attachments_table(db: sqlite3.Connection) -> None:
    """Create the attachments table on demand. The web side owns this for the
    prototype so it works before the MCP server ships the canonical CREATE —
    both use IF NOT EXISTS, so it stays safe once the server half lands."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS attachments ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " channel TEXT NOT NULL,"
        " message_id INTEGER,"
        " member_id TEXT NOT NULL,"
        " mime TEXT NOT NULL,"
        " filename TEXT,"
        " width INTEGER, height INTEGER, bytes INTEGER,"
        " path TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_channel "
        "ON attachments(channel)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_unlinked "
        "ON attachments(created_at) WHERE message_id IS NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachments_message "
        "ON attachments(message_id)"
    )


def attachments_for_message(db: sqlite3.Connection, msg_id: int) -> List[Dict[str, Any]]:
    """[{id, mime, filename}] for a message. Defensive: returns [] if the
    attachments table doesn't exist yet (no uploads have happened)."""
    try:
        rows = db.execute(
            "SELECT id, mime, filename FROM attachments "
            "WHERE message_id = ? ORDER BY id", (msg_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "mime": r["mime"], "filename": r["filename"] or ""}
            for r in rows]


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
        self.idle_since: Optional[float] = None

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
            if not self._subs:
                # Stamp the moment we went quiet; the reaper uses this to
                # retire hubs for channels nobody is watching any more.
                self.idle_since = time.time()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

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
                    "attachments": attachments_for_message(db, r["id"]),
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
        # Two independently-optional columns, so they get independent tiers:
        # filter_mode/context_json (v7.2) and last_turn_end (this feature). The
        # turn column is added by nth_server at MCP startup, but the dashboard
        # can be launched standalone against a DB whose server has not restarted
        # — folding both into one try/except would drop filter_mode and the
        # context % for every member just because the turn column is missing.
        def _roster_sql(turn: bool, v72: bool) -> str:
            cols = [
                "m.id AS id", "m.name AS name", "m.status_text AS status_text",
                "m.last_seen AS member_last_seen", "m.last_read AS member_last_read",
                "m.messenger_heartbeat AS messenger_heartbeat",
                "m.watchdog_heartbeat AS watchdog_heartbeat",
            ]
            if v72:
                cols += ["m.filter_mode AS filter_mode", "m.context_json AS context_json"]
            cols += [
                "COALESCE(MAX(s.last_read), 0) AS session_last_read",
                "MAX(s.last_seen) AS session_last_seen",
                "GROUP_CONCAT(s.fingerprint) AS fingerprints",
            ]
            if turn:
                cols.append("MAX(s.last_turn_end) AS session_last_turn_end")
            return ("SELECT " + ", ".join(cols) + " FROM members m "
                    "LEFT JOIN sessions s "
                    "  ON s.channel = m.channel AND s.member_id = m.id "
                    "  AND s.revoked_at IS NULL "
                    "WHERE m.channel = ? "
                    "GROUP BY m.id, m.channel "
                    "ORDER BY m.joined_at")

        rows = None
        for _turn, _v72 in ((True, True), (False, True), (False, False)):
            try:
                rows = db.execute(_roster_sql(_turn, _v72), (self.channel,)).fetchall()
                break
            except sqlite3.OperationalError:
                continue
        if rows is None:
            rows = []

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
            keys = r.keys()
            s_turn_end = r["session_last_turn_end"] if "session_last_turn_end" in keys else None
            aname, aemoji = avatars.get(r["id"], animal_for(r["id"]))
            out.append({
                "id": r["id"],
                "name": r["name"] or r["id"],
                "status_text": r["status_text"] or "",
                "last_seen": effective_last_seen,
                "last_read": effective_last_read,
                "filter_mode": fm or "all",
                "context_pct": context_pct,
                "context": context_full,
                # working/idle split uses the session's OWN activity (not the
                # monitor-inflated effective_last_seen) vs. its last turn end.
                # MAX(last_seen) and MAX(last_turn_end) are taken independently,
                # which is correct under trio's one-primary-session-per-member
                # invariant (nth_connect mints a single session per member id).
                # If multi-session members are reintroduced, pair both values
                # from the newest-last_seen session instead.
                "status": member_status(
                    effective_last_seen, r["status_text"] or "",
                    session_activity_iso=(r["session_last_seen"] or None),
                    last_turn_end_iso=s_turn_end),
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
                            "attachments": attachments_for_message(db, r["id"]),
                        })
                        self.last_msg_id = r["id"]

                    members = self._fetch_roster(db)
                    snapshot = json.dumps(members, sort_keys=True)
                    if snapshot != self._last_roster_snapshot:
                        self._last_roster_snapshot = snapshot
                        self._broadcast({"type": "roster", "members": members})

                    # Context rings: cheap (few tiny local files); broadcast
                    # only when the payload actually changed. The age fields
                    # move every tick, so they are excluded from the
                    # comparison — hashing them made this fire ~1/s forever
                    # to every connected browser.
                    ctx_sessions = _read_context_snapshots()
                    ctx_snapshot = _ctx_change_key(ctx_sessions)
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


_CTX_CACHE_TTL_S = 1.0
_ctx_cache: Dict[str, Any] = {"at": 0.0, "val": []}
_ctx_cache_lock = threading.Lock()


def _read_context_snapshots() -> List[Dict[str, Any]]:
    """All fresh publisher files as dicts (plus _age_s), newest first.
    Stale >120s ignored; the UI additionally dims entries older than 30s.

    Memoised for _CTX_CACHE_TTL_S: one EventHub tick calls this from both
    the roster build and the ring broadcast, one thread runs per viewed
    channel, and /api/landing calls it per request — all re-globbing and
    re-parsing the same handful of files. The TTL is below the poll
    interval's practical resolution, so freshness is unaffected.
    """
    now_c = time.monotonic()
    with _ctx_cache_lock:
        if now_c - _ctx_cache["at"] < _CTX_CACHE_TTL_S:
            return list(_ctx_cache["val"])
    out: List[Dict[str, Any]] = []
    try:
        now = time.time()
        for p in CONTEXT_USAGE_DIR.glob("*.json"):
            try:
                age = now - p.stat().st_mtime
                if age > CONTEXT_SNAPSHOT_STALE_S:
                    continue
                raw = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                if not isinstance(raw.get("session_id"), str):
                    continue
                # Project before it leaves this function: these snapshots
                # go to /api/landing and every SSE subscriber, neither of
                # which requires an identity. The raw statusline file
                # carries transcript paths, cwds, project dirs and spend.
                data = project_context(raw)
                data["_age_s"] = int(age)
                out.append(data)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    out.sort(key=lambda d: d["_age_s"])
    with _ctx_cache_lock:
        _ctx_cache["at"] = now_c
        _ctx_cache["val"] = out
    return list(out)


_CTX_VOLATILE = ("_age_s", "data_age_s", "ts", "_relayed_at")


def _ctx_change_key(sessions: List[Dict[str, Any]]) -> str:
    """Stable digest of a context payload, ignoring fields that tick on
    their own. Used to decide whether an SSE broadcast is warranted."""
    return json.dumps(
        [{k: v for k, v in s.items() if k not in _CTX_VOLATILE} for s in sessions],
        sort_keys=True,
    )


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
            cls._reap_idle_hubs_locked()
            hub = cls.hubs.get(code)
            if hub is None:
                hub = EventHub(self.db_path, code)
                hub.start()
                cls.hubs[code] = hub
            return hub

    @classmethod
    def _reap_idle_hubs_locked(cls) -> None:
        """Retire hubs nobody has watched for HUB_IDLE_REAP_S.

        Caller must hold hubs_lock. Each live hub is a thread plus a
        SQLite connection polling twice a second, so without this a
        browsed-once channel costs 2 queries/second for the life of the
        process.
        """
        now = time.time()
        for code, hub in list(cls.hubs.items()):
            if hub.subscriber_count() > 0:
                continue
            idle_since = hub.idle_since
            if idle_since is not None and (now - idle_since) > HUB_IDLE_REAP_S:
                hub.stop()
                del cls.hubs[code]

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
            # Verify before spinning up a hub: each one is a permanent
            # thread polling SQLite twice a second, so accepting any
            # well-formed code would let an unauthenticated caller mint
            # unbounded threads with a loop of random codes.
            if self.landing_mode and not self._channel_exists(ch):
                self._error(404, f"no such channel: {ch}")
                return
            self._serve_sse(self._hub_for_channel(ch))
        elif path == "/api/search":
            self._handle_search(parsed)
        elif path.startswith("/api/attachment/"):
            self._serve_attachment(path)
        else:
            self._error(404, "not found")

    def _reject_cross_site(self) -> bool:
        """True (and an error already sent) when this POST looks cross-site.

        _resolve_identity() derives trust from the source IP, not from the
        session cookie: a cookie-less request from a browser still resolves as
        the loopback/tailnet operator. SameSite is therefore not a CSRF control
        here, because the cookie is not the credential. A cross-origin fetch
        with a CORS-safelisted Content-Type skips preflight, so the write lands
        even though the response is opaque to the attacker.

        Origin is the load-bearing half: browsers set it on every cross-origin
        request and page script cannot forge it. Sec-Fetch-Site is defence in
        depth and is absent on older/non-Chromium clients, so its absence must
        be allowed. Compare Origin against the request's own Host rather than a
        configured value -- the same hub is reached by tailnet name and by
        tailnet IP, and those are different origins.
        """
        origin = self.headers.get("Origin")
        if origin:
            if urlparse(origin).netloc != self.headers.get("Host", ""):
                self._error(403, "cross-origin POST rejected")
                return True
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            self._error(403, "cross-site POST rejected")
            return True
        return False

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self._reject_cross_site():
            return
        if parsed.path == "/api/send":
            self._handle_send()
        elif parsed.path == "/api/identify":
            self._handle_identify()
        elif parsed.path == "/api/cull":
            self._handle_cull()
        elif parsed.path == "/api/path/validate":
            self._handle_path_validate()
        elif parsed.path == "/api/reveal":
            self._handle_reveal()
        elif parsed.path == "/api/upload":
            self._handle_upload()
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
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            # A non-numeric Content-Length is a malformed request, not a
            # reason to dump a traceback into the hub's journal.
            self._error(400, "invalid Content-Length")
            return None
        if length <= 0 or length > max_bytes:
            self._error(400, "missing or oversized body")
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError guards against a deeply-nested-JSON DoS (json.loads
            # recurses); it is not a ValueError subclass, so name it explicitly.
            self._error(400, "invalid JSON")
            return None

    def _handle_search(self, parsed) -> None:
        """Full-history search: substring match over this channel's stored
        messages (beyond the ~200 the dashboard keeps in memory)."""
        # Landing mode serves many channels from one process, so the channel
        # comes from the request, not from a process-wide attribute. Mirrors
        # every other handler here; binding self.channel would match "" and
        # silently return nothing.
        ch = self._channel_for_request(parsed)
        if ch is None:
            self._error(400, "channel query param required")
            return
        if self.landing_mode and not self._channel_exists(ch):
            self._error(404, f"no such channel: {ch}")
            return
        qs = parse_qs(parsed.query)
        q = (qs.get("q", [""])[0] or "").strip()
        if len(q) < 2:
            self._error(400, "query too short (min 2 chars)")
            return
        q = q[:200]
        _token, ident, _is_new = self._resolve_identity()
        if ident.source == IDENTITY_SOURCE_PENDING:
            self._error(403, "identity required — POST /api/identify first")
            return
        # Escape LIKE wildcards so a query like "50%" is a literal substring.
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        db = None
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=3000")
            rows = db.execute(
                "SELECT id, member_id, member_name, content, created_at FROM messages "
                "WHERE channel = ? AND content LIKE ? ESCAPE '\\' "
                "ORDER BY id DESC LIMIT 200",
                (ch, like),
            ).fetchall()
            results = [{"id": r["id"], "member_id": r["member_id"],
                        "member_name": r["member_name"] or r["member_id"],
                        "content": r["content"] or "", "created_at": r["created_at"]}
                       for r in rows]
        except sqlite3.Error as e:
            # sqlite3's message can carry table/column names and the db file
            # path — internal shape the browser has no business seeing. Log
            # the detail to the operator's journal, hand the client a short
            # generic reason.
            sys.stderr.write(f"[nth_web] search db error: {e}\n")
            self._error(500, "search failed")
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        self._json({"ok": True, "query": q, "count": len(results), "results": results})

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
        raw_ids = body.get("attachment_ids") or []
        if not isinstance(raw_ids, list):
            self._error(400, "invalid attachment_ids")
            return
        # Strict integer contract: reject floats, bools, and numeric strings
        # (type(True) is bool, so booleans are rejected here too).
        if not all(type(a) is int and a > 0 for a in raw_ids):
            self._error(400, "invalid attachment_ids")
            return
        attachment_ids = list(raw_ids)
        if len(attachment_ids) > 8:
            self._error(400, "too many attachments (max 8)")
            return
        if len(set(attachment_ids)) != len(attachment_ids):
            self._error(400, "duplicate attachment id")
            return
        if not content and not attachment_ids:
            self._error(400, "empty content")
            return
        if not content and attachment_ids:
            content = "[image]"
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

                # Validate attachments up front: every requested id must be
                # this operator's own, unlinked, in-channel row — else abort,
                # so an image-only send can't post a false "[image]" with no
                # image actually attached.
                if attachment_ids:
                    ensure_attachments_table(db)
                    placeholders = ",".join("?" * len(attachment_ids))
                    owned = db.execute(
                        f"SELECT id FROM attachments WHERE id IN ({placeholders}) "
                        "AND channel = ? AND member_id = ? AND message_id IS NULL",
                        (*attachment_ids, send_channel, op_id),
                    ).fetchall()
                    if {r["id"] for r in owned} != set(attachment_ids):
                        db.execute("ROLLBACK")
                        self._error(400, "invalid or already-linked attachment id")
                        return

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
                # Link any uploaded attachments to this message (own, unlinked).
                if attachment_ids:
                    db.executemany(
                        "UPDATE attachments SET message_id = ? "
                        "WHERE id = ? AND channel = ? AND member_id = ? "
                        "AND message_id IS NULL",
                        [(msg_id, aid, send_channel, op_id) for aid in attachment_ids],
                    )
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
            # Same reasoning as the search handler above: sqlite's text names
            # tables and columns, and a client learns the schema one failed
            # request at a time. It is also useless to the person who hit it —
            # "no such column: bangs" after a missed migration tells them
            # nothing they can act on, while the operator's log is exactly
            # where that belongs.
            sys.stderr.write(f"[nth_web] send db error: {e}\n")
            self._error(500, "send failed")
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        self._json({"ok": True, "id": msg_id})

    def _handle_cull(self) -> None:
        """Remove a member from the channel at the operator's request — the
        dashboard's roster remove (×) button. Mirrors trio_cull: releases the
        target's tasks/locks and posts a [culled] system message."""
        body = self._read_json_body(max_bytes=2048)
        if body is None:
            return
        # _read_json_body only guarantees valid JSON, not a dict of strings —
        # guard both before .get()/.strip() so bad input is a clean 400, not an
        # AttributeError that drops the connection.
        if not isinstance(body, dict):
            self._error(400, "invalid body")
            return
        target_id = body.get("target_member_id")
        if not isinstance(target_id, str) or not target_id.strip():
            self._error(400, "target_member_id required")
            return
        target_id = target_id.strip()
        cull_channel = self._channel_for_request(urlparse(self.path))
        if cull_channel is None:
            self._error(400, "channel query param required")
            return
        if self.landing_mode and not self._channel_exists(cull_channel):
            self._error(404, f"no such channel: {cull_channel}")
            return
        _token, ident, _is_new = self._resolve_identity()
        if ident.source == IDENTITY_SOURCE_PENDING:
            self._error(403, "identity required — POST /api/identify first")
            return
        # Removing a member is destructive and roster-wide — restrict it to
        # trusted identities (a local shell or a Tailscale-verified peer). A
        # self-declared guest, the weakest tier, must not be able to rip out
        # agents or other participants (esp. under --tailnet's 0.0.0.0 bind).
        if ident.source not in CULL_ALLOWED_SOURCES:
            self._error(403, "only a trusted operator (local or tailnet) can remove members")
            return
        db = None
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE")
            try:
                op_id, op_name = ensure_operator_row(db, cull_channel, ident)
                result, err = cull_member(db, cull_channel, op_id, op_name, target_id)
                if err:
                    db.execute("ROLLBACK")
                    self._error(400, err)
                    return
                db.execute("COMMIT")
            except sqlite3.Error:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as e:
            # This handler is new in this branch, so shipping the pattern
            # would INTRODUCE the leak rather than inherit it. sqlite's text
            # names tables and columns, and cull is reachable by anyone the
            # server will accept a POST from.
            sys.stderr.write(f"[nth_web] cull db error: {e}\n")
            self._error(500, "remove failed")
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        self._json({"ok": True, **(result or {})})

    # ── file-path validate / reveal ──
    # The client detects path-LIKE tokens in message bodies broadly, then asks
    # the server which ones actually exist on disk; only real files get linked
    # (validation, not pattern-matching, gates linkification). A linked path can
    # then be revealed in Finder. There is NO access gating on these endpoints
    # (operator's explicit choice), so injection-safety is enforced structurally:
    # reveal never runs a shell and never plain-`open`s a file (which would
    # launch its default app) — it only `open -R` (reveal/select in Finder).
    _PATH_VALIDATE_CAP = 200          # max candidates per validate request
    _PATH_MAX_LEN = 4096              # ignore absurdly long candidates

    @staticmethod
    def _expand_path(candidate: str) -> str:
        """Expand a leading ~ (and ~user), then require the result to be
        ABSOLUTE. Returns "" for anything relative.

        A relative token has no agreed meaning here. It would resolve against
        the SERVER's working directory, which is wherever the dashboard happened
        to be launched — not the cwd of the agent that wrote the message. In a
        fleet whose agents work in different checkouts (the normal case) that
        means "see server/nth_web.py" links to whichever copy the dashboard was
        started next to, reveals it with a success flash, and renders
        differently for two operators reading the same message. A confident link
        to the wrong file is worse than no link, so relative tokens stay plain
        text and the reader keeps the literal string the agent wrote."""
        expanded = os.path.expanduser(candidate)
        return expanded if os.path.isabs(expanded) else ""

    @staticmethod
    def _is_trivial_root(expanded: str) -> bool:
        """True for a filesystem root or pure-separator token ('/', '//', '/..',
        a bare Windows/volume drive root). These EXIST on disk yet are never a
        meaningful file link — treating a lone '/' as one is exactly what made a
        slash used as prose punctuation ('reload / incognito', '#' / '!') pick
        up a folder icon. Rejected in both validate and reveal (defense in depth
        alongside the client's filename-segment filter). Real paths UNDER a root
        ('/Users/…') contain more than separators, so they're unaffected."""
        if not expanded or not expanded.strip("/\\ \t"):
            return True                       # empty or only slashes/whitespace
        try:
            norm = os.path.normpath(expanded)
        except (ValueError, TypeError):
            return False
        if norm in (os.sep, "/", "//"):       # POSIX root (normpath preserves '//')
            return True
        drive, tail = os.path.splitdrive(norm)
        if drive and tail in ("", os.sep, "/", "\\"):   # bare drive root 'C:\'
            return True
        return False

    def _resolve_existing(self, raw: str) -> Optional[str]:
        """Return the expanded on-disk target for `raw`, or None if it doesn't
        exist (or is a trivial root — see _is_trivial_root). Tries the candidate
        as-is first, then with a trailing :line[:col] (editor/grep/Claude-Code
        form) stripped — so both validate and reveal agree on what a `path:line`
        token resolves to. Uses lexists so broken symlinks (still revealable)
        count. Never raises (a NUL/bad path is just 'not found')."""
        for cand in (raw, re.sub(r":\d+(?::\d+)?$", "", raw)):
            expanded = self._expand_path(cand)
            if self._is_trivial_root(expanded):
                continue                      # '/' & bare roots are not linkable
            try:
                if expanded and os.path.lexists(expanded):
                    return expanded
            except (ValueError, OSError):
                continue
        return None

    def _handle_path_validate(self) -> None:
        """POST /api/path/validate — body {"paths": [...]}. Returns
        {"exists": {candidate: bool}} keyed by the ORIGINAL candidate string
        (so client cache keys line up). A `path:line[:col]` token counts as
        existing when the bare file exists. Capped at _PATH_VALIDATE_CAP."""
        # These two endpoints read and act on the OPERATOR'S OWN filesystem, so
        # they are restricted to the same trusted tiers as other destructive
        # controls: a local shell, or a Tailscale-verified peer. The fork left
        # them ungated, which was defensible when the server bound loopback and
        # served one channel; upstream can bind 0.0.0.0 (--tailnet) and serves a
        # channel-less landing surface, where ungated meant any reachable peer
        # could enumerate the operator's filesystem and pop Finder windows on
        # their screen without knowing any channel code.
        _token, ident, _is_new = self._resolve_identity()
        if ident.source not in LOCAL_PATH_ALLOWED_SOURCES:
            self._error(403, "only a trusted operator (local or tailnet) can inspect local paths")
            return
        # Bodies can carry up to 200 paths; allow a generous cap over the default.
        body = self._read_json_body(max_bytes=256 * 1024)
        if body is None:
            return
        if not isinstance(body, dict):
            self._error(400, "invalid body")
            return
        paths = body.get("paths")
        if not isinstance(paths, list):
            self._error(400, "paths must be a list")
            return
        exists: Dict[str, bool] = {}
        for cand in paths[: self._PATH_VALIDATE_CAP]:
            if not isinstance(cand, str) or not cand or len(cand) > self._PATH_MAX_LEN:
                continue
            if cand in exists:
                continue
            exists[cand] = self._resolve_existing(cand) is not None
        self._json({"exists": exists})

    def _handle_reveal(self) -> None:
        """POST /api/reveal — body {"path": "..."}. Reveal (select) the file in
        Finder. SECURITY: no shell, arg-list only, `open -R` (reveal) never plain
        `open` (which would launch the default app), and a leading `--` so a
        path beginning with `-` can't be read as a flag. Existence is verified
        first (404 otherwise), so a bogus/injection-style value never reaches a
        launch. A `path:line[:col]` suffix (Claude-Code form) is stripped so the
        file itself is revealed."""
        # These two endpoints read and act on the OPERATOR'S OWN filesystem, so
        # they are restricted to the same trusted tiers as other destructive
        # controls: a local shell, or a Tailscale-verified peer. The fork left
        # them ungated, which was defensible when the server bound loopback and
        # served one channel; upstream can bind 0.0.0.0 (--tailnet) and serves a
        # channel-less landing surface, where ungated meant any reachable peer
        # could enumerate the operator's filesystem and pop Finder windows on
        # their screen without knowing any channel code.
        _token, ident, _is_new = self._resolve_identity()
        if ident.source not in LOCAL_PATH_ALLOWED_SOURCES:
            self._error(403, "only a trusted operator (local or tailnet) can inspect local paths")
            return
        body = self._read_json_body(max_bytes=8192)
        if body is None:
            return
        if not isinstance(body, dict):
            self._error(400, "invalid body")
            return
        raw = body.get("path")
        if not isinstance(raw, str) or not raw.strip():
            self._error(400, "path required")
            return
        raw = raw.strip()
        if len(raw) > self._PATH_MAX_LEN:
            self._error(400, "path too long")
            return

        # Resolve to an existing target (as-is, else with a :line[:col] suffix
        # stripped). Same resolver validate uses, so the UI and the reveal agree.
        target = self._resolve_existing(raw)
        if target is None:
            self._error(404, "path not found on disk")
            return

        abspath = os.path.abspath(target)
        plat = sys.platform
        # Whether the child's exit status is a trustworthy success signal.
        # It is on macOS and Linux; it is NOT on Windows -- see below.
        check_rc = True
        try:
            if plat == "darwin":
                # Reveal (select) in Finder. ARG LIST + `--`: no shell, no flag
                # injection. `-R` reveals; it never launches the file's app.
                # `--` IS correct here: /usr/bin/open documents and accepts it.
                cp = subprocess.run(
                    ["open", "-R", "--", abspath],
                    capture_output=True, text=True, timeout=10,
                )
            elif plat.startswith("linux"):
                # Best-effort: open the containing folder (no reliable "select").
                folder = abspath if os.path.isdir(abspath) else os.path.dirname(abspath)
                # NO `--`. xdg-open's main argument loop matches `-*` before any
                # sentinel handling and calls exit_failure_syntax, so a `--` makes
                # EVERY call fail with "unexpected option '--'". Measured against
                # xdg-utils 1.2.1. abspath is absolute, so there is no
                # leading-dash case for a sentinel to guard against anyway.
                cp = subprocess.run(
                    ["xdg-open", folder],
                    capture_output=True, text=True, timeout=10,
                )
            elif plat.startswith("win"):
                # ONE argv token: explorer parses "/select,<path>" as a unit, and
                # a space after the comma makes it ignore the selector and open
                # Documents instead.
                cp = subprocess.run(
                    ["explorer", f"/select,{abspath}"],
                    capture_output=True, text=True, timeout=10,
                )
                # explorer.exe returns nonzero on SUCCESS as a matter of course,
                # so treating its exit status as failure turns every working
                # reveal into a 502.
                check_rc = False
            else:
                self._json({"ok": False, "error": f"unsupported platform: {plat}"},
                           status=501)
                return
        except FileNotFoundError:
            self._json({"ok": False, "error": "reveal tool not available"}, status=501)
            return
        except subprocess.TimeoutExpired:
            self._error(504, "reveal timed out")
            return
        if check_rc and cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or "").strip() or f"exit {cp.returncode}"
            self._error(502, f"reveal failed: {msg}")
            return
        self._json({"ok": True, "path": abspath})

    def _handle_upload(self) -> None:
        """Accept a raw image body (Content-Type = mime, X-Filename header),
        validate by magic bytes, store on disk, and create an unlinked
        attachments row. The subsequent /api/send links it to a message."""
        token, ident, _is_new = self._resolve_identity()
        # Writing files into the operator's home directory is the same class of
        # action as revealing a path there, so it takes the same tier as
        # /api/reveal and /api/path/validate. A self-declared guest is the
        # weakest identity this server mints -- under --tailnet (the deployed
        # mode) that is anyone who can reach the port and type a name. Gating
        # only on PENDING let them write 10 MB per request, unmetered.
        if ident.source not in LOCAL_PATH_ALLOWED_SOURCES:
            self._error(403, "only a trusted operator (local or tailnet) can upload")
            return
        # Same reason as _serve_attachment: the channel comes from the request,
        # not from a process-wide attribute that landing mode never sets.
        ch = self._channel_for_request(urlparse(self.path))
        if ch is None:
            self._error(400, "channel query param required")
            return
        if self.landing_mode and not self._channel_exists(ch):
            self._error(404, f"no such channel: {ch}")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except (TypeError, ValueError):
            self._error(400, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._error(400, "image is missing or larger than the 10 MB limit")
            return
        try:
            data = self.rfile.read(length)
        except OSError:
            self._error(400, "read failed")
            return
        if len(data) != length:
            self._error(400, "incomplete upload")
            return
        mime = sniff_image_mime(data)
        if mime not in ALLOWED_IMAGE_MIME:
            self._error(400, "unsupported image type (png/jpeg/gif/webp only)")
            return
        ext = ALLOWED_IMAGE_MIME[mime]
        # X-Filename is percent-encoded by the client (HTTP headers must be
        # ISO-8859-1, but filenames — e.g. macOS screenshots — carry Unicode).
        raw_name = unquote(self.headers.get("X-Filename", "") or "")
        filename = re.sub(r"[^\w.\- ]", "_", raw_name)[:120] or ("image" + ext)

        db = None
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            ensure_attachments_table(db)
            op_id, _op_name = ensure_operator_row(db, ch, ident)
            # The gate above says WHO may upload; this says HOW MUCH. Both are
            # needed: the gate does not stop a cross-site POST, which executes
            # as the trusted local operator and therefore passes it.
            used = db.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS b FROM attachments "
                " WHERE channel = ? AND member_id = ?", (ch, op_id),
            ).fetchone()["b"]
            if used + len(data) > MAX_MEMBER_ATTACH_BYTES:
                self._error(413, "attachment quota exceeded")
                return
            now = now_iso()
            cur = db.execute(
                "INSERT INTO attachments "
                "(channel, message_id, member_id, mime, filename, bytes, path, created_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, '', ?)",
                (ch, op_id, mime, filename, len(data), now),
            )
            att_id = cur.lastrowid
            fpath = None
            try:
                chan_dir = ATTACH_DIR / re.sub(r"[^\w.\-]", "_", ch)
                chan_dir.mkdir(parents=True, exist_ok=True)
                fpath = chan_dir / f"{att_id}{ext}"
                fpath.write_bytes(data)
                db.execute("UPDATE attachments SET path = ? WHERE id = ?",
                           (str(fpath), att_id))
            except (OSError, sqlite3.Error):
                # Roll back BOTH sides so no orphan row or file survives.
                try:
                    db.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
                except sqlite3.Error:
                    pass
                if fpath is not None:
                    try:
                        fpath.unlink()
                    except OSError:
                        pass
                raise
        except (sqlite3.Error, OSError) as e:
            self._error(500, f"upload error: {e}")
            return
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        self._json({"ok": True, "id": att_id, "mime": mime,
                    "filename": filename})   # client builds the URL with its channel
        # Opportunistic GC: uploading is exactly when abandoned uploads accrue,
        # and it is already a slow path. Rate-limited internally, and after the
        # response so it can never delay the client.
        sweep_attachments(self.db_path)

    def _serve_attachment(self, path: str) -> None:
        tail = path.rsplit("/", 1)[-1]
        if not tail.isdigit():
            self._error(404, "not found")
            return
        att_id = int(tail)
        # Attachment bytes are channel content, so they get the same bar as
        # every other read: a resolved identity and the channel taken from the
        # REQUEST. Binding the process-wide self.channel would serve nothing in
        # landing mode (it is "" there) and, worse, would ignore which channel
        # the caller actually asked for. The upstream original had no gate at
        # all; the fork's gate keyed on a DM visibility engine that does not
        # exist here, so this is re-derived rather than ported.
        parsed = urlparse(self.path)
        ch = self._channel_for_request(parsed)
        if ch is None:
            self._error(400, "channel query param required")
            return
        if self.landing_mode and not self._channel_exists(ch):
            self._error(404, f"no such channel: {ch}")
            return
        _token, ident, _is_new = self._resolve_identity()
        if ident.source == IDENTITY_SOURCE_PENDING:
            self._error(403, "identity required — POST /api/identify first")
            return
        row = None
        db = None
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=2000")
            op_id, _op_name = ensure_operator_row(db, ch, ident)
            row = db.execute(
                # An attachment is readable once it is PUBLISHED (linked to a
                # message everyone in the channel can see). Before that it is
                # still in someone's composer, so only its uploader may fetch
                # it. Ids are small sequential integers, so without this an
                # image pasted and then thought better of stays readable by
                # anyone who guesses its id. The fork's gate keyed this on a DM
                # visibility engine that does not exist upstream; the
                # uploader-only half is not DM-specific and is re-derived here.
                "SELECT mime, path FROM attachments "
                " WHERE id = ? AND channel = ? "
                "   AND (message_id IS NOT NULL OR member_id = ?)",
                (att_id, ch, op_id),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
        if not row:
            self._error(404, "not found")
            return
        try:
            chan_root = (ATTACH_DIR / re.sub(r"[^\w.\-]", "_", ch)).resolve()
            resolved = Path(row["path"]).resolve()
            # Defense in depth: only serve files under THIS channel's dir.
            if not resolved.is_relative_to(chan_root):
                self._error(404, "not found")
                return
            data = resolved.read_bytes()
        except OSError:
            self._error(404, "file missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", row["mime"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


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
    --fg: #d8dde6; --dim: #7a8596; --dimmer: #59606f;
    --accent: #3ba0e6; --accent-hi: #50b0f0; --accent2: #59cb79;
    --warn: #e3c34c; --err: #e56a4a; --mention: #e3c34c;
    --hover: #0f1420; --ov: 255,255,255;
    --card-radius: 3px; --card-shadow: none;
    --pill-radius: 3px; --input-radius: 4px;
    --ref-chip: #9ccf9c; --ref-chip-bg: rgba(126, 222, 126, 0.08);
    --ref-chip-border: rgba(126, 222, 126, 0.25);
    --bang-chip: #ff8470; --bang-chip-bg: rgba(255, 132, 112, 0.2);
    --bang-chip-border: rgba(255, 132, 112, 0.5);
    --bang-label-bg: rgba(255, 132, 112, 0.15);
  }
  :root[data-theme="light"] {
    /* ── Daylight (light) ── */
    --bg: #f6f7f9; --bg2: #eceef2; --panel: #e2e6ec; --border: #c8cfd8;
    --fg: #1c2430; --dim: #5a6675; --dimmer: #88909d;
    --accent: #1f7fd0; --accent-hi: #2b93e6; --accent2: #2e9e52;
    --warn: #b8860b; --err: #cc4a2c; --mention: #b8860b;
    --hover: #dce1e8; --ov: 0,0,0;
    --ref-chip: #2d8a2d; --ref-chip-bg: rgba(45, 138, 45, 0.1);
    --ref-chip-border: rgba(45, 138, 45, 0.3);
    --bang-chip: #cc3320; --bang-chip-bg: rgba(204, 51, 32, 0.1);
    --bang-chip-border: rgba(204, 51, 32, 0.35);
    --bang-label-bg: rgba(204, 51, 32, 0.1);
  }
  :root[data-theme="nord"] {
    /* ── Nord (dark) ── */
    --bg: #2e3440; --bg2: #2b303b; --panel: #3b4252; --border: #434c5e;
    --fg: #e5e9f0; --dim: #8f9bb3; --dimmer: #717d94;
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
    --fg: #000; --dim: #555; --dimmer: #8e8e8e;
    --accent: #3892d4; --accent-hi: #4db5ff; --accent2: #21bf4b;
    --warn: #bd8300; --err: #cc1800; --mention: #bd8300;
    --hover: #e2eff9; --ov: 0,0,0;
    --card-radius: 2px; --card-shadow: 0 1px 8px rgba(136,136,136,0.3);
    --pill-radius: 2px; --input-radius: 2px;
    --ref-chip: #2d8a2d; --ref-chip-bg: rgba(45, 138, 45, 0.1);
    --ref-chip-border: rgba(45, 138, 45, 0.3);
    --bang-chip: #cc3320; --bang-chip-bg: rgba(204, 51, 32, 0.1);
    --bang-chip-border: rgba(204, 51, 32, 0.35);
    --bang-label-bg: rgba(204, 51, 32, 0.1);
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
    /* ── Walled Garden (dark, from macOS dark-mode Messages) ── */
    --bg: #1c1c1e; --bg2: #2c2c2e; --panel: #2c2c2e; --border: #3a3a3c;
    --fg: #fff; --dim: #98989f; --dimmer: #6b6b6e;
    --accent: #0a84ff; --accent-hi: #409cff; --accent2: #30d158;
    --warn: #ff9f0a; --err: #ff453a; --mention: #ff9f0a;
    --hover: #3a3a3c; --ov: 255,255,255;
    --card-radius: 18px; --card-shadow: none;
    --pill-radius: 999px; --input-radius: 18px;
    --bubble-mine: #0b84ff; --bubble-mine-ink: #fff;
    --bubble-theirs: #26252a; --bubble-theirs-ink: #fff;
    --bubble-system: transparent;
  }
  /* ── Walled Garden: dark-mode pixel-faithful recreation ── */
  :root[data-theme="bluebubble"] .msg {
    max-width: 70%; border-left: none; margin-left: 0; margin-bottom: 2px;
    padding: 8px 14px; border-radius: 18px; position: relative;
    background: var(--bubble-theirs) !important; color: var(--bubble-theirs-ink);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
      "Helvetica Neue", "Helvetica", "Arial", sans-serif;
    font-size: 17px; line-height: 1.28; letter-spacing: -0.01em;
  }
  :root[data-theme="bluebubble"] .msg.sender-break { margin-top: 8px; }
  :root[data-theme="bluebubble"] .msg:hover { filter: brightness(1.08); }
  :root[data-theme="bluebubble"] .msg:not(.mine) { border-bottom-left-radius: 4px; }
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
    font-size: 11px; color: #98989f; margin-bottom: 1px;
    font-weight: 400;
  }
  :root[data-theme="bluebubble"] .msg .head .time { color: #98989f; }
  :root[data-theme="bluebubble"] .msg .author { font-weight: 600; color: #fff; font-size: 13px; }
  :root[data-theme="bluebubble"] .msg .body { color: inherit; }
  :root[data-theme="bluebubble"] .msg .body.plain { white-space: pre-wrap; }
  :root[data-theme="bluebubble"] .msg.mine {
    margin-left: auto; background: var(--bubble-mine) !important;
    color: var(--bubble-mine-ink); border-bottom-right-radius: 4px;
    border-bottom-left-radius: 18px;
  }
  :root[data-theme="bluebubble"] .msg.mine .head { color: rgba(255,255,255,0.6); }
  :root[data-theme="bluebubble"] .msg.mine .head .time { color: rgba(255,255,255,0.45); }
  :root[data-theme="bluebubble"] .msg.mine .author { color: rgba(255,255,255,0.8); }
  :root[data-theme="bluebubble"] .msg.system {
    max-width: 100%; text-align: center; border-radius: 10px;
    background: transparent !important; color: #98989f; font-style: normal;
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
  /* Header — frosted dark glass */
  :root[data-theme="bluebubble"] header {
    background: rgba(28,28,30,0.92); border-bottom: 0.5px solid rgba(255,255,255,0.08);
    backdrop-filter: saturate(180%) blur(20px); -webkit-backdrop-filter: saturate(180%) blur(20px);
  }
  :root[data-theme="bluebubble"] header .title { color: #0a84ff; font-size: 17px; }
  :root[data-theme="bluebubble"] header .meta { color: #98989f; }
  :root[data-theme="bluebubble"] header .pill { border: none;
    background: rgba(10,132,255,0.15); color: #0a84ff; font-weight: 500; }
  :root[data-theme="bluebubble"] header .pill:hover { background: rgba(10,132,255,0.25); }
  :root[data-theme="bluebubble"] header .pill.on { background: #0a84ff; color: #fff; }
  :root[data-theme="bluebubble"] header .pill.conn.ok { color: #30d158; background: rgba(48,209,88,0.15); }
  :root[data-theme="bluebubble"] header .pill.conn.bad { color: #ff453a; background: rgba(255,69,58,0.15); }
  /* Sidebar */
  :root[data-theme="bluebubble"] #side {
    background: #2c2c2e; border-left: 0.5px solid rgba(255,255,255,0.08);
  }
  :root[data-theme="bluebubble"] #side h2 { color: #98989f; font-size: 13px;
    text-transform: uppercase; letter-spacing: 0.02em; }
  :root[data-theme="bluebubble"] .member .name { font-size: 15px; color: #fff; }
  :root[data-theme="bluebubble"] .member .stext { font-size: 13px; color: #98989f; }
  :root[data-theme="bluebubble"] .member .dot { width: 10px; height: 10px; }
  :root[data-theme="bluebubble"] .member + .member { border-top: 0.5px solid rgba(255,255,255,0.08); }
  :root[data-theme="bluebubble"] .member .dm-btn {
    background: rgba(10,132,255,0.15); color: #0a84ff; border: none; border-radius: 14px;
  }
  /* Match the theme's pill shape so the remove control isn't a sharp grey box
     beside a rounded blue one — destructive, so it keeps the red family. */
  :root[data-theme="bluebubble"] .member .rm-btn {
    background: rgba(255,69,58,0.15); color: #ff453a; border: none; border-radius: 14px;
  }
  :root[data-theme="bluebubble"] .member .rm-btn:hover:not(:disabled) {
    background: #ff453a; color: #fff;
  }
  /* Composer — dark keyboard area */
  :root[data-theme="bluebubble"] #composer {
    background: #1c1c1e; border-top: 0.5px solid rgba(255,255,255,0.08); padding: 8px 10px;
  }
  /* The textarea is transparent here, so the stack carries the bubble fill. */
  :root[data-theme="bluebubble"] #input-stack {
    background: #2c2c2e; border-radius: 18px;
  }
  /* The mirror must follow every metric this theme sets on #input, and #input's
     glyphs must stay transparent — this selector out-specifies the base rule,
     so without re-asserting it the real text is drawn opaque ON TOP of the
     mirror in a different font and size. */
  :root[data-theme="bluebubble"] #input-highlight {
    border: 0.5px solid transparent; border-radius: 18px;
    padding: 8px 14px; font-size: 17px; line-height: 1.28;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
      "Helvetica Neue", "Helvetica", "Arial", sans-serif;
  }
  :root[data-theme="bluebubble"] #input {
    background: transparent; color: transparent; caret-color: #fff; border: 0.5px solid #48484a; border-radius: 18px;
    padding: 8px 14px; font-size: 17px; line-height: 1.28;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
      "Helvetica Neue", "Helvetica", "Arial", sans-serif;
  }
  :root[data-theme="bluebubble"] #input:focus { border-color: #0a84ff; }
  :root[data-theme="bluebubble"] #send-btn {
    border-radius: 50%; width: 34px; height: 34px; padding: 0;
    font-size: 0; background: #0a84ff; position: relative;
  }
  :root[data-theme="bluebubble"] #send-btn::after {
    content: "\2191"; font-size: 20px; font-weight: 700; color: #fff;
  }
  :root[data-theme="bluebubble"] #send-btn:disabled { background: #48484a; }
  :root[data-theme="bluebubble"] #hint { display: none; }
  :root[data-theme="bluebubble"] #preview { font-size: 13px; color: #98989f; }
  :root[data-theme="bluebubble"] #target-bar .tb-pill {
    border: none; background: rgba(10,132,255,0.15); color: #0a84ff;
    border-radius: 14px; font-weight: 500;
  }
  :root[data-theme="bluebubble"] #target-bar .tb-pill.on {
    background: #0a84ff; color: #fff;
  }
  /* Completions dropdown */
  :root[data-theme="bluebubble"] #completions {
    border-radius: 14px; border: none; box-shadow: 0 4px 24px rgba(0,0,0,0.5);
    background: rgba(44,44,46,0.96); backdrop-filter: blur(20px);
  }
  :root[data-theme="bluebubble"] .completion:hover,
  :root[data-theme="bluebubble"] .completion.selected { background: #3a3a3c; }
  /* Settings panel */
  :root[data-theme="bluebubble"] #settings-panel {
    border-radius: 14px; border: none; box-shadow: 0 4px 24px rgba(0,0,0,0.5);
    background: rgba(44,44,46,0.96); backdrop-filter: blur(20px);
  }
  /* Guest modal */
  :root[data-theme="bluebubble"] #guest-modal .guest-card {
    border-radius: 14px; border: none; box-shadow: 0 4px 30px rgba(0,0,0,0.5);
    background: #2c2c2e;
  }
  :root[data-theme="bluebubble"] #guest-modal button {
    border-radius: 14px; background: #0a84ff; font-weight: 600;
  }
  /* Hide noise — clean like the garden */
  :root[data-theme="bluebubble"] .acks { display: none; }
  :root[data-theme="bluebubble"] .watermark-pins { display: none; }
  :root[data-theme="bluebubble"] #jump-btn {
    border-radius: 999px; background: #0a84ff; box-shadow: 0 2px 12px rgba(10,132,255,0.4);
  }
  :root[data-theme="bluebubble"] #chat {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
      "Helvetica Neue", "Helvetica", "Arial", sans-serif;
    background: #000;
  }

  :root[data-theme="win31"] {
    /* ── Windows 3.1 (PVE Dashboard) ── */
    --bg: #c0c0c0; --bg2: #008080; --panel: #c0c0c0; --border: #808080;
    --fg: #000; --dim: #404040; --dimmer: #606060;
    --accent: #a85000; --accent-hi: #904500; --accent2: #006400;
    --warn: #5c5c00; --err: #800000; --mention: #5c5c00;
    --hover: #d4d4d4; --ov: 0,0,0;
    --card-radius: 0; --card-shadow: none; --pill-radius: 0;
    --ref-chip: #3d6b3d; --ref-chip-bg: rgba(61, 107, 61, 0.12);
    --ref-chip-border: rgba(61, 107, 61, 0.3);
    --bang-chip: #b82e1f; --bang-chip-bg: rgba(184, 46, 31, 0.12);
    --bang-chip-border: rgba(184, 46, 31, 0.35);
    --bang-label-bg: rgba(184, 46, 31, 0.1);
  }
  :root[data-theme="crt"] {
    /* ── CRT Green (PVE Dashboard) ── */
    --bg: #020a02; --bg2: #031003; --panel: #031603; --border: rgba(51,255,102,.28);
    --fg: #33ff66; --dim: #1f9941; --dimmer: #29693c;
    --accent: #7dff9c; --accent-hi: #a0ffb8; --accent2: #33ff66;
    --warn: #c6ff00; --err: #ff5544; --mention: #c6ff00;
    --hover: #041d04; --ov: 255,255,255;
    --card-radius: 2px; --card-shadow: 0 0 10px rgba(51,255,102,.12); --pill-radius: 2px;
  }
  :root[data-theme="amber"] {
    /* ── Amber Mono (PVE Dashboard) ── */
    --bg: #0d0700; --bg2: #140a00; --panel: #1a0e00; --border: rgba(255,176,0,.25);
    --fg: #ffb000; --dim: #b87900; --dimmer: #845a0a;
    --accent: #ffcb52; --accent-hi: #ffe080; --accent2: #ffb000;
    --warn: #ffd700; --err: #ff5e2e; --mention: #ffd700;
    --hover: #1f1100; --ov: 255,255,255;
    --card-radius: 2px; --card-shadow: 0 0 10px rgba(255,176,0,.1); --pill-radius: 2px;
  }
  :root[data-theme="paper"] {
    /* ── Paper Print (PVE Dashboard) ── */
    --bg: #f4f1ea; --bg2: #efeae0; --panel: #fffdf8; --border: #d8d2c4;
    --fg: #1c1b18; --dim: #6b675e; --dimmer: #8f8b80;
    --accent: #9a3b2e; --accent-hi: #b8503e; --accent2: #3a6b2e;
    --warn: #9a7b1a; --err: #a32a22; --mention: #9a7b1a;
    --hover: #f5f0e6; --ov: 0,0,0;
    --card-radius: 2px; --card-shadow: 0 1px 0 #d8d2c4; --pill-radius: 2px;
    --ref-chip: #2d8a2d; --ref-chip-bg: rgba(45, 138, 45, 0.1);
    --ref-chip-border: rgba(45, 138, 45, 0.3);
    --bang-chip: #cc3320; --bang-chip-bg: rgba(204, 51, 32, 0.1);
    --bang-chip-border: rgba(204, 51, 32, 0.35);
    --bang-label-bg: rgba(204, 51, 32, 0.1);
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
    --fg: #0f380f; --dim: #285528; --dimmer: #426542;
    --accent: #0f380f; --accent-hi: #1a4a1a; --accent2: #0f380f;
    --warn: #306230; --err: #0f380f; --mention: #306230;
    --hover: #98b80e; --ov: 0,0,0;
    --card-radius: 0; --card-shadow: 3px 3px 0 #0f380f; --pill-radius: 0;
    --ref-chip: #1e5a1e; --ref-chip-bg: rgba(30, 90, 30, 0.15);
    --ref-chip-border: rgba(30, 90, 30, 0.35);
    --bang-chip: #a52a1a; --bang-chip-bg: rgba(165, 42, 26, 0.15);
    --bang-chip-border: rgba(165, 42, 26, 0.35);
    --bang-label-bg: rgba(165, 42, 26, 0.12);
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

  /* Full-history search panel */
  #search-panel { position: fixed; top: 8%; left: 50%; transform: translateX(-50%);
    width: min(680px, 92vw); max-height: 80vh; z-index: 70; display: flex; flex-direction: column;
    background: var(--bg2); border: 1px solid var(--border); border-radius: 10px;
    box-shadow: 0 12px 48px rgba(0,0,0,0.55); overflow: hidden; }
  #search-panel[hidden] { display: none; }
  .search-head { display: flex; gap: 8px; padding: 10px; border-bottom: 1px solid var(--border); }
  #search-input { flex: 1; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg); color: var(--fg); font: inherit; }
  #search-input:focus { outline: none; border-color: var(--accent); }
  #search-close { background: none; border: none; color: var(--dim); font-size: 22px;
    line-height: 1; cursor: pointer; padding: 0 8px; }
  #search-close:hover { color: var(--fg); }
  #search-status { padding: 6px 12px; font-size: 11px; color: var(--dim); }
  #search-results { overflow-y: auto; padding: 4px 8px 10px; }
  .search-hit { padding: 8px 10px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; }
  .search-hit:hover { background: rgba(var(--ov),0.06); border-color: rgba(var(--ov),0.15); }
  .search-hit .sh-meta { font-size: 10px; color: var(--dim); margin-bottom: 2px; }
  .search-hit .sh-author { font-weight: 600; }
  .search-hit .sh-body { font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .msg.flash { animation: flashmsg 1.4s ease-out; }
  @keyframes flashmsg { 0% { background: var(--hover); } 100% { background: transparent; } }

  /* ── Header ── */
  header { grid-column: 1 / 3; background: var(--bg2); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; padding: 0 16px; gap: 12px;
           font-weight: 600; }
  header .title { color: var(--accent); }
  header .meta { color: var(--dim); font-weight: 400; font-size: 11px; }
  header .spacer { flex: 1; }
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
  #font-picker option:disabled { color: var(--dimmer); }
  #font-picker.wg-locked { opacity: 0.7; }

  /* ── Settings panel (drawer) ── */
  #settings-panel {
    position: fixed; top: 46px; right: 10px; z-index: 30;
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--card-radius);
    padding: 14px; min-width: 250px; max-width: 320px;
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
  #chat { height: 100%; overflow-y: auto; padding: 14px 16px; scroll-behavior: smooth; }
  /* Message numbers (#N): a per-message left-margin tag, hidden unless #chat
     carries .show-msg-nums. The number rests at the message's vertical centre;
     via position:sticky it pins just inside the viewport edge once that centre
     would scroll out of view, so it stays visible beside its message and then
     leaves with the message once it's fully off-screen. Pure CSS — the gutter
     spans the full message height and flex-centres the sticky number. */
  .msg-num-gutter { display: none; }
  /* position:relative is also set on .msg below for hover actions/pins; repeated
     here so this gutter's absolute/full-height centring can't silently break if
     that unrelated rule is ever changed. */
  #chat.show-msg-nums .msg { padding-left: 52px; position: relative; }
  #chat.show-msg-nums .msg-num-gutter {
    display: flex; align-items: center; justify-content: flex-end;
    position: absolute; left: 0; top: 0; height: 100%; width: 46px;
    pointer-events: none; }
  /* NB: no overflow:auto/hidden/scroll on .msg-num-gutter (or any ancestor up to
     #chat) — that would make the gutter the sticky scroll-container and break the
     number's position:sticky. The large-id overflow guard lives on the sticky
     span itself (own-overflow is safe), not on an ancestor. */
  #chat.show-msg-nums .msg-num {
    position: sticky; top: 10px; bottom: 10px;
    max-width: 100%; overflow: hidden; text-overflow: ellipsis;
    font-size: 10px; line-height: 1.2; color: var(--dim);
    font-variant-numeric: tabular-nums; white-space: nowrap;
    pointer-events: auto; user-select: text; cursor: text; }
  #chat.show-msg-nums .msg.targeted .msg-num { color: var(--accent); }
  /* Walled Garden draws each message as a chat bubble (its own padding, an 18px
     radius and a ::before tail), so padding the bubble would print the number
     inside it. Indent the column instead and hang the gutter in that margin,
     leaving the bubble geometry untouched. */
  :root[data-theme="bluebubble"] #chat.show-msg-nums { padding-left: 46px; }
  :root[data-theme="bluebubble"] #chat.show-msg-nums .msg { padding-left: 14px; }
  :root[data-theme="bluebubble"] #chat.show-msg-nums .msg-num-gutter {
    left: -42px; width: 36px; }
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
                               background: color-mix(in srgb, var(--mention) 15%, transparent);
                               color: var(--mention);
                               border: 1px solid color-mix(in srgb, var(--mention) 30%, transparent);
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
                          background: var(--ref-chip-bg);
                          color: var(--ref-chip);
                          border: 1px solid var(--ref-chip-border);
                          font-weight: 500; }
  .msg .refs-bar .mchip .manimal { font-size: 13px; line-height: 1; }
  /* !bangs bar — UNFILTERABLE. Loudest visual; rendered above @mentions. */
  .msg .bangs-bar { font-size: 12px; margin: 2px 0 4px;
                    display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  .msg .bangs-bar .to-label { color: var(--bang-chip); font-size: 10px; font-weight: 700;
                               text-transform: uppercase; letter-spacing: 1px;
                               margin-right: 2px;
                               padding: 1px 5px; border-radius: 3px;
                               background: var(--bang-label-bg); }
  .msg .bangs-bar .mchip { display: inline-flex; align-items: center; gap: 3px;
                           padding: 1px 7px 1px 5px; border-radius: 10px;
                           background: var(--bang-chip-bg);
                           color: var(--bang-chip);
                           border: 1px solid var(--bang-chip-border);
                           font-weight: 700; }
  .msg .bangs-bar .mchip .manimal { font-size: 13px; line-height: 1; }
  .msg .body { word-wrap: break-word; overflow-wrap: break-word; }
  .msg .body.plain { white-space: pre-wrap; }
  /* Valid @mentions stay visible in the prose itself, not only in the
     routing bar above the message. The member-colored inset makes adjacent
     mentions distinguishable while the shared mention color preserves the
     meaning across themes. */
  /* Text-forward mention: a member-colored dot + member-tinted text, faint
     tint behind. The dot carries the "who" color pop; text stays legible on
     every theme via color-mix toward the theme foreground. @all falls back to
     the theme --mention color (no per-member color is set for it). */
  .msg .body .inline-mention {
    display: inline-block; padding: 0 5px; margin: 0 1px; border-radius: 5px;
    background: color-mix(in srgb, var(--mention-member-color, var(--mention)) 11%, transparent);
    /* Mix mostly toward --fg so contrast tracks the theme's own body text.
       Mixing mostly toward the pastel palette colour reads fine on dark
       themes and measured as low as 1.30:1 on the light ones — the words
       carrying the routing meaning were the only unreadable thing on screen. */
    color: color-mix(in srgb, var(--mention-member-color, var(--mention)) 30%, var(--fg));
    font-weight: 700; overflow-wrap: anywhere;
  }
  .msg .body .inline-mention::before {
    content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--mention-member-color, var(--mention));
    margin-right: 4px; vertical-align: 1px;
  }
  /* @all broadcast — a celebratory rainbow shimmer so "ping everyone" reads
     louder than a single-member @mention. The gradient is clipped to the glyphs
     and slowly panned; the dot is a static rainbow bead. Targets the pseudo-
     member id "all" that decorateInlineSigil sets on the span. Motion is
     disabled under prefers-reduced-motion (the static rainbow still reads). */
  .msg .body .inline-mention[data-member-id="all"] {
    background: linear-gradient(90deg,
      #ff5f5f, #ffb347, #ffe66d, #7ede7e, #62d7ef, #8eb9ff, #d070d7, #ff5f5f);
    background-size: 200% 100%;
    /* A BACKING PLATE, not the glyphs. Clipping the gradient to the text made
       @all unreadable on every light theme (measured 1.02-1.17:1 at the pale
       stops) — the loudest signal in the product was the least visible thing on
       screen. Text stays --fg so the word is legible in all 18 themes. */
    color: var(--fg);
    text-shadow: 0 0 3px var(--bg);
    animation: at-all-shimmer 3s linear infinite;
    font-weight: 800;
  }
  .msg .body .inline-mention[data-member-id="all"]::before {
    background: conic-gradient(#ff5f5f, #ffb347, #ffe66d, #7ede7e,
      #62d7ef, #8eb9ff, #d070d7, #ff5f5f);
  }
  @keyframes at-all-shimmer {
    0%   { background-position:   0% 50%; }
    100% { background-position: 200% 50%; }
  }
  @media (prefers-reduced-motion: reduce) {
    .msg .body .inline-mention[data-member-id="all"] { animation: none; }
  }
  /* #pound reference inline — same chip+dot mechanism as @, but tinted from the
     muted "about" green (matches .refs-bar) and lighter weight so it reads
     quieter than an @ping. Dot stays member-colored to keep the "who". */
  .msg .body .inline-ref {
    display: inline-block; padding: 0 5px; margin: 0 1px; border-radius: 5px;
    background: rgba(126, 222, 126, 0.08);
    color: color-mix(in srgb, #9ccf9c, var(--fg) 32%);
    font-weight: 500; white-space: nowrap;
  }
  .msg .body .inline-ref::before {
    content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--mention-member-color, #9ccf9c);
    margin-right: 4px; vertical-align: 1px;
  }
  /* !bang alert inline — same mechanism, tinted from the loud coral (matches
     .bangs-bar) with heavier weight so it reads louder than an @ping. Dot stays
     member-colored to keep the "who". */
  .msg .body .inline-bang {
    display: inline-block; padding: 0 5px; margin: 0 1px; border-radius: 5px;
    background: rgba(255, 132, 112, 0.16);
    color: color-mix(in srgb, #ff8470, var(--fg) 18%);
    font-weight: 800; white-space: nowrap;
  }
  .msg .body .inline-bang::before {
    content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--mention-member-color, #ff8470);
    margin-right: 4px; vertical-align: 1px;
  }
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
  /* Validated file paths — clickable "reveal in Finder" links. Distinct from
     plain links: code-tinted chip + a subtle 📁 affordance, dotted underline. */
  #chat .msg .body a.file-link {
    color: var(--accent); text-decoration: underline; text-decoration-style: dotted;
    text-underline-offset: 2px; cursor: pointer;
    background: rgba(var(--ov),0.06); border-radius: 3px; padding: 0 3px;
    transition: background 0.12s ease, color 0.12s ease;
  }
  .file-links-unavailable { padding: 6px 12px; font-size: 11px; color: var(--dim);
                            background: var(--bg2); border-bottom: 1px solid var(--border); }
  .file-link-note { font-size: 0.85em; color: var(--err); white-space: normal; }
  /* Game Boy's --err is identical to --accent and to the body text colour, so
     colour alone cannot signal failure. The strike-through is a shape cue that
     survives any palette. */
  #chat .msg .body a.file-link.file-link-err { text-decoration-line: line-through underline; }
  #chat .msg .body a.file-link::after { content: " 📁"; font-size: 0.82em; opacity: 0.65; }
  #chat .msg .body a.file-link:hover { background: rgba(var(--ov),0.12); }
  #chat .msg .body a.file-link:focus-visible { outline: 1px solid var(--accent); outline-offset: 1px; }
  #chat .msg .body a.file-link.file-link-ok  { background: rgba(var(--ok-rgb, 80,200,120),0.22); }
  #chat .msg .body a.file-link.file-link-err {
    color: var(--err); background: rgba(var(--ov),0.10); text-decoration-style: wavy;
  }
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
  .msg .body th, .msg .body td { border: 1px solid rgba(var(--ov),0.15); padding: 4px 8px; }
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
  .msg.targeted { background: var(--hover); border-left-color: var(--mention); }
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
  /* top "N new messages" bar — jump to the first unread */
  #new-bar { position: absolute; left: 50%; top: 10px; transform: translateX(-50%);
             background: var(--mention); color: var(--bg); border: none; z-index: 6;
             padding: 5px 14px; border-radius: 16px; font-size: 11px; font-weight: 600;
             cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,0.5); display: none;
             user-select: none; }
  #new-bar.show { display: block; }
  #new-bar:hover { filter: brightness(1.1); }
  /* "new messages" divider before the first unread message */
  .unread-divider { display: flex; align-items: center; gap: 8px; margin: 10px 4px;
                    color: var(--mention); font-size: 10px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: 0.6px; }
  .unread-divider::before, .unread-divider::after { content: ""; flex: 1; height: 1px;
                    background: var(--mention); }

  /* ── Roster sidebar ── */
  #side { grid-row: 2 / 3; grid-column: 2 / 3;
          background: var(--panel); border-left: 1px solid var(--border);
          overflow-y: auto; display: flex; flex-direction: column; }
  #side section { padding: 14px; border-bottom: 1px solid var(--border); }
  #side section:last-child { border-bottom: none; }
  #side h2 { font-size: 10px; text-transform: uppercase; color: var(--dim);
             letter-spacing: 0.08em; margin: 0 0 10px; font-weight: 600; }
  /* Heading row: section title on the left, close control in the corner. */
  #side .side-head { display: flex; align-items: center; justify-content: space-between;
                     gap: 8px; margin: 0 0 10px; }
  #side .side-head h2 { margin: 0; }
  #side-close { flex: 0 0 auto; width: 22px; height: 22px; padding: 0;
                display: flex; align-items: center; justify-content: center;
                background: var(--bg2); color: var(--dim);
                border: 1px solid var(--border); border-radius: var(--pill-radius);
                font-family: inherit; font-size: 12px; line-height: 1; cursor: pointer; }
  #side-close:hover { background: var(--accent); color: var(--bg);
                      border-color: var(--accent); }
  #side-close:focus-visible { outline: none; border-color: var(--accent);
                              color: var(--fg); }

  .member { padding: 8px 0; cursor: pointer; }
  .member + .member { border-top: 1px solid var(--border); }
  .member .row { display: flex; align-items: center; gap: 8px; }
  .member .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .member .roster-animal { font-size: 16px; line-height: 1; flex-shrink: 0;
                           user-select: none; }
  .member .dm-btn { font-size: 9px; padding: 2px 6px; border-radius: 3px;
                    background: var(--bg2); color: var(--dim); border: 1px solid var(--border);
                    cursor: pointer; flex-shrink: 0; user-select: none;
                    text-transform: uppercase; letter-spacing: 0.5px; }
  .member .dm-btn:hover { background: var(--accent); color: var(--bg);
                          border-color: var(--accent); }
  .member .ctx-pct { font-size: 9px; padding: 1px 5px; border-radius: 7px;
                     background: var(--bg2); color: var(--dim); margin-left: 4px; }
  .member .ctx-pct.warm { background: #4a3a20; color: #e5d35e; }
  .member .ctx-pct.hot  { background: #4a2420; color: var(--bang-chip); }
  .member .member-actions { display: none; padding: 6px 0 2px 16px; }
  .member.expanded .member-actions { display: flex; }
  /* Destructive, so it carries --err rather than the amber --mention hue that
     means "someone said your name" everywhere else in this UI. */
  /* Sized off .dm-btn on purpose: the routine control and the destructive one
     sit in the same expanded row, and the destructive one should not be the
     bigger target. */
  .member .rm-btn { font: inherit; font-size: 9px; line-height: 1.2;
                    padding: 2px 6px; border-radius: 3px;
                    background: var(--bg2); color: var(--err); border: 1px solid var(--border);
                    cursor: pointer; flex-shrink: 0; user-select: none;
                    text-transform: uppercase; letter-spacing: 0.5px; }
  .member .rm-btn:hover:not(:disabled) { background: var(--err); color: var(--bg);
                          border-color: var(--err); }
  .member .rm-btn:disabled { opacity: 0.6; cursor: default; }
  .member .fmode { font-size: 9px; padding: 1px 5px; border-radius: 3px;
                   flex-shrink: 0; user-select: none;
                   text-transform: uppercase; letter-spacing: 0.5px;
                   border: 1px solid transparent; }
  .member .fmode.all   { color: var(--dim); background: var(--bg2); border-color: var(--border); }
  .member .fmode.about { color: var(--ref-chip); background: var(--ref-chip-bg);
                         border-color: var(--ref-chip-border); }
  .member .fmode.at    { color: #f0c060; background: rgba(240, 192, 96, 0.1);
                         border-color: rgba(240, 192, 96, 0.3); }
  .member .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                  font-weight: 500; }
  .member .caret { color: var(--dimmer); font-size: 9px; transition: transform 0.1s; }
  .member.expanded .caret { transform: rotate(90deg); }
  .member .id { color: var(--dimmer); font-size: 10px; margin-left: 2px; }
  .dot.active { background: var(--accent2); }
  /* working = alive AND mid-turn: a breathing green dot, the "it's on it,
     keep chilling" cue. Distinct from the solid green "active" (legacy /
     hook-not-installed) and the grey "idle" (turn ended, waiting on you). */
  .dot.working { background: var(--accent2); animation: workpulse 1.3s ease-in-out infinite; }
  @keyframes workpulse {
    0%   { opacity: 1;    transform: scale(1); }
    50%  { opacity: 0.4;  transform: scale(0.72); }
    100% { opacity: 1;    transform: scale(1); }
  }
  @media (prefers-reduced-motion: reduce) { .dot.working { animation: none; } }
  .dot.idle { background: var(--dimmer); }
  .dot.stale { background: var(--warn); }
  .dot.dead { background: var(--err); }
  .member .stext { font-size: 10px; color: var(--dim); margin-top: 4px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                   padding-left: 16px; line-height: 1.4; }

  .member .stats { display: none; padding: 10px 0 4px 16px;
                   font-size: 10px; color: var(--dim); }
  .member.expanded .stats { display: block; }
  .stats .stat-row { display: flex; justify-content: space-between; padding: 4px 0; gap: 12px; }
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
  #filter-banner { padding: 4px 8px; background: var(--hover); color: var(--mention);
                   font-size: 10px; border-radius: 3px; margin-bottom: 6px;
                   display: none; cursor: pointer; }
  #filter-banner.active { display: block; }
  /* Fatal/bootstrap errors. Deliberately outside header .meta, which the
     mobile breakpoint hides — a failed boot has to be legible on a phone. */
  #fatal-banner { display: none; padding: 10px 14px; background: var(--err);
                  color: #fff; font-size: 13px; font-weight: 600;
                  text-align: center; position: sticky; top: 0; z-index: 999; }

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
                         padding: 2px 8px; cursor: pointer; user-select: none;
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
  #input-stack { flex: 1; position: relative; min-width: 0; background: var(--bg);
                 border-radius: var(--input-radius); }
  #input-highlight {
    position: absolute; inset: 0; z-index: 0; pointer-events: none;
    padding: 8px 10px; border: 1px solid transparent; border-radius: var(--input-radius);
    font-family: inherit; font-size: 13px; line-height: 1.45;
    white-space: pre-wrap; overflow-wrap: break-word; overflow: hidden;
    /* Reserve the gutter the textarea's scrollbar takes once the draft
       exceeds max-height, or the two wrap at different columns wherever
       scrollbars are classic rather than overlay. */
    scrollbar-gutter: stable;
    color: var(--fg);
  }
  /* The highlight mirrors the textarea 1:1, so it must NOT change text metrics —
     background/colour only. Anything that adds width or shifts the baseline
     (padding, border, underline, a leading dot) makes the overlay drift from
     the typed glyphs. Member colour is wired in per-token by
     renderComposerMentionHighlights(). */
  #input-highlight.composing { visibility: hidden; }
  #input-highlight .composer-mention {
    color: var(--mention-member-color, var(--mention));
    box-shadow: inset 0 -2px 0 var(--mention-member-color, var(--mention));
  }
  /* @all in the composer gets the same rainbow shimmer as the rendered chip,
     so typing "@all" previews the broadcast. Reuses the at-all-shimmer keyframe. */
  #input-highlight .composer-mention-all {
    background: linear-gradient(90deg,
      #ff5f5f, #ffb347, #ffe66d, #7ede7e, #62d7ef, #8eb9ff, #d070d7, #ff5f5f);
    background-size: 200% 100%;
    color: var(--fg); text-shadow: 0 0 3px var(--bg);
    box-shadow: none;
    animation: at-all-shimmer 3s linear infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    #input-highlight .composer-mention-all { animation: none; }
  }
  /* The textarea's own glyphs are hidden (color: transparent) so the colored
     #input-highlight mirror behind it is what the user reads; caret-color keeps
     the caret visible. Placeholder + selection are restored explicitly since
     they'd otherwise inherit the transparent text color. */
  /* The textarea's own glyphs are transparent so the coloured mirror behind it
     is what the user reads; caret-color keeps the caret visible. line-height is
     pinned because the mirror must match it exactly. */
  #input { scrollbar-gutter: stable; position: relative; z-index: 1; width: 100%; display: block;
           background: transparent; color: transparent; caret-color: var(--fg);
           border: 1px solid var(--border);
           padding: 8px 10px; border-radius: var(--input-radius);
           font-family: inherit; font-size: 13px; line-height: 1.45;
           resize: none; min-height: 36px; max-height: 160px; }
  #input:focus { outline: none; border-color: var(--accent); }
  #input::placeholder { color: var(--dim); opacity: 1; }
  /* Translucent selection so the colored mirror text stays readable through it. */
  #input::selection { background: color-mix(in srgb, var(--accent) 32%, transparent); }
  #send-btn { background: var(--accent); color: var(--bg); border: none;
              padding: 0 18px; height: 36px; border-radius: 4px; cursor: pointer;
              font-weight: 600; font-family: inherit; font-size: 13px; }
  #send-btn:hover { background: var(--accent-hi); }
  #send-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  #attach-btn { background: var(--panel); color: var(--fg); border: 1px solid var(--border);
                height: 36px; min-width: 38px; border-radius: 4px; cursor: pointer;
                font-size: 16px; line-height: 1; }
  #attach-btn:hover { border-color: var(--accent); }
  #attach-strip { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
  #attach-strip:empty { display: none; }
  .attach-thumb { position: relative; width: 60px; height: 60px; border-radius: 4px;
                  overflow: hidden; border: 1px solid var(--border); background: var(--bg); }
  .attach-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .attach-thumb .rm { position: absolute; top: 1px; right: 1px; width: 16px; height: 16px;
                      border-radius: 50%; background: rgba(0,0,0,0.65); color: #fff;
                      border: none; cursor: pointer; font-size: 11px; line-height: 16px;
                      padding: 0; text-align: center; }
  .attach-thumb.uploading { opacity: 0.5; }
  #composer.dragover { outline: 2px dashed var(--accent); outline-offset: -4px; }
  /* Compact clamps .body, but attachments are a SIBLING of it — without
     this an image message stayed 250-380px tall while plain ones clamped
     to ~57px, so the setting did almost nothing on a channel with images. */
  .msg.compact .msg-attachments { max-height: 64px; overflow: hidden; }
  .msg.compact .msg-img { max-height: 64px; }
  .msg .msg-attachments { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px;
                          min-width: 0; max-width: 100%; }
  .msg .msg-attachments > a { min-width: 0; max-width: 100%; }
  /* min() not a bare 320px: as a flex item with min-width:auto the link
     refused to shrink, so an image tore through the bubble's rounded corner
     and off the viewport at every phone width (measured 75px past the edge
     at 390px in Walled Garden, and 23px at 320px in the default themes). */
  .msg-img-missing { display: inline-block; font-size: 12px; color: var(--dim);
                     padding: 6px 10px; border: 1px dashed var(--border);
                     border-radius: 6px; }
  .msg .msg-img { max-width: min(320px, 100%); max-height: 320px;
                  height: auto; border-radius: 6px;
                  border: 1px solid var(--border); cursor: pointer; display: block; }
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

    /* Sidebar: hidden by default, slide-in overlay leaving 60px scrim tap zone */
    #side { display: none !important; position: fixed; top: 0; bottom: 0; right: 0;
            width: calc(100vw - 60px); max-width: 320px; z-index: 20;
            grid-column: 1; grid-row: 2; border-left: 1px solid var(--border);
            overflow-y: auto; padding-top: 12px; }
    #app.mobile-side-open #side { display: flex !important; }
    /* Same corner control, sized for a fingertip. */
    #side-close { width: 30px; height: 30px; font-size: 15px; }
    /* Scrim behind sidebar overlay */
    #mobile-scrim { display: none; position: fixed; inset: 0; z-index: 19;
                    background: rgba(0,0,0,0.5); }
    #app.mobile-side-open #mobile-scrim { display: block; }

    /* Settings panel: full-width on mobile */
    #settings-panel { right: 0; left: 0; max-width: 100%; border-radius: 0;
                      top: auto; position: fixed; }

    /* Composer: touch-friendly */
    #composer { padding: 6px 8px; }
    /* The mirror must take EVERY metric override the textarea takes, or the
       coloured text drifts off the caret. >=16px also prevents iOS zoom. */
    #input, #input-highlight { font-size: 16px; min-height: 40px; }
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
    /* Shrink the message-number gutter on phones so it doesn't eat the body. */
    #chat.show-msg-nums .msg { padding-left: 44px; }
    #chat.show-msg-nums .msg-num-gutter { width: 38px; }
    header .meta { display: none; }
    .msg .head { font-size: 10px; }
    .msg .mentions-bar .mchip, .msg .refs-bar .mchip,
    .msg .bangs-bar .mchip { font-size: 10px; padding: 1px 5px; }
    #target-bar .tb-pill { padding: 3px 8px; font-size: 11px; }
  }

  /* Guest identify modal */
  #guest-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.75);
                 display: flex; align-items: center; justify-content: center;
                 z-index: 1000; }
  #guest-modal .guest-card { background: var(--panel); border: 1px solid #2a3342;
                             border-radius: 8px; padding: 24px; width: min(460px, 90vw);
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
  #guest-modal .guest-err { color: var(--err); font-size: 12px; min-height: 16px;
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
    <a class="pill" id="btn-home" href="/" title="back to the hub landing page">⌂ fleet</a>
    <span class="title" id="h-channel">trio#…</span>
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
        <option value="popart">Pop Art</option>
        <option value="lcars">LCARS</option>
        <option value="bluebubble">Walled Garden</option>
      </optgroup>
      <optgroup label="Light">
        <option value="light">Daylight</option>
        <option value="pve-light">Clean</option>
        <option value="paper">Paper</option>
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
      <option value='"Iosevka", "Iosevka Term", "Iosevka Fixed", ui-monospace, Menlo, monospace'>Iosevka</option>
      <option value='Menlo, Monaco, ui-monospace, monospace'>Menlo</option>
      <option value='Monaco, Menlo, ui-monospace, monospace'>Monaco</option>
      <option value='Consolas, "Cascadia Mono", ui-monospace, monospace'>Consolas</option>
      <option value='"SF Mono", "SFMono-Regular", ui-monospace, Menlo, monospace'>SF Mono</option>
      <option value='"Atkinson Hyperlegible Next", "Atkinson Hyperlegible", -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif'>Atkinson Hyperlegible</option>
      <option value='-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "Helvetica", "Arial", sans-serif' disabled>Walled Garden</option>
    </select>
    <input id="filter" type="text" placeholder="filter messages…" spellcheck="false">
    <span class="pill" id="btn-search" title="search the full channel history">🔍 search</span>
    <span class="pill on" id="btn-side" title="show/hide the roster sidebar">roster</span>
    <span class="pill on" id="btn-msgnum" title="show each message's #number in the left margin">#nums</span>
    <span class="pill" id="btn-compact" title="clamp every message body to 3 lines">compact</span>
    <span class="pill" id="btn-notify" title="desktop notifications on @you">🔔 off</span>
    <span class="pill" id="btn-sound" title="play a chime on new messages (scope in settings when on)">🔊 off</span>
    <span class="pill" id="btn-settings" title="settings">⚙ settings</span>
    <span class="pill" id="btn-mobile-roster" title="show roster &amp; context">☰</span>
    <span class="pill conn bad" id="h-conn">● disconnected</span>
  </header>
  <div id="settings-panel" hidden>
    <h3>Settings</h3>
  </div>

  <div id="mobile-scrim"></div>
  <div id="chat-wrap">
    <div id="new-bar" title="jump to the first unread message"></div>
    <div id="chat"></div>
    <button id="jump-btn">↓ latest<span class="count" id="jump-count" style="display:none">0</span></button>
  </div>

  <aside id="side">
    <section>
      <div id="filter-banner">filter active — showing matching messages only. click to clear.</div>
      <div class="side-head">
        <h2 id="r-heading">Members</h2>
        <button id="side-close" aria-label="Close sidebar" title="Close sidebar">✕</button>
      </div>
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
    <div id="attach-strip"></div>
    <input type="file" id="file-input" accept="image/png,image/jpeg,image/gif,image/webp" multiple style="display:none">
    <div id="input-row">
      <div id="completions"></div>
      <button id="attach-btn" title="attach image (or paste / drop into the box)">🖼</button>
      <div id="input-stack">
        <div id="input-highlight" aria-hidden="true"></div>
        <textarea id="input" rows="1" placeholder="Message — @ to mention, Enter to send"></textarea>
      </div>
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
      <kbd>paste / drop</kbd> image
      <span style="margin-left:14px;color:var(--dim)">click a message to expand/collapse in compact mode</span>
    </div>
  </div>
</div>

<div id="search-panel" hidden>
  <div class="search-head">
    <input id="search-input" type="text" placeholder="search all history…" autocomplete="off" spellcheck="false">
    <button id="search-close" title="close (Esc)" aria-label="close search">×</button>
  </div>
  <div id="search-status"></div>
  <div id="search-results"></div>
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
  const inputHighlight = document.getElementById('input-highlight');
  const sendBtn = document.getElementById('send-btn');
  const preview = document.getElementById('preview');
  const compEl = document.getElementById('completions');
  const btnMsgNum = document.getElementById('btn-msgnum');
  const filterEl = document.getElementById('filter');
  const filterBanner = document.getElementById('filter-banner');
  const btnCompact = document.getElementById('btn-compact');
  const btnNotify = document.getElementById('btn-notify');
  const btnSound = document.getElementById('btn-sound');
  const fontPicker = document.getElementById('font-picker');
  const jumpBtn = document.getElementById('jump-btn');
  const jumpCount = document.getElementById('jump-count');
  const newBar = document.getElementById('new-bar');
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
  const WG_FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Helvetica Neue", "Helvetica", "Arial", sans-serif';
  let _savedFontBeforeWG = null;   // stash the user's font choice while WG is active

  function setFontPickerLocked(locked) {
    if (locked) {
      fontPicker.classList.add('wg-locked');
      for (const opt of fontPicker.options) {
        if (opt.value === WG_FONT) { opt.disabled = false; }
        else { opt.disabled = true; }
      }
      fontPicker.value = WG_FONT;
      document.documentElement.style.setProperty('--msg-font', WG_FONT);
    } else {
      fontPicker.classList.remove('wg-locked');
      for (const opt of fontPicker.options) {
        if (opt.value === WG_FONT) { opt.disabled = true; }
        else { opt.disabled = false; }
      }
      // Restore the user's previous font choice
      if (_savedFontBeforeWG) {
        let found = false;
        for (const opt of fontPicker.options) {
          if (opt.value === _savedFontBeforeWG && !opt.disabled) {
            fontPicker.value = _savedFontBeforeWG; found = true; break;
          }
        }
        if (found) document.documentElement.style.setProperty('--msg-font', _savedFontBeforeWG);
        _savedFontBeforeWG = null;
      } else {
        // Fall back to saved font or default
        try {
          const s = localStorage.getItem('trio.msgFont');
          if (s) {
            for (const opt of fontPicker.options) {
              if (opt.value === s && !opt.disabled) {
                fontPicker.value = s;
                document.documentElement.style.setProperty('--msg-font', s);
                break;
              }
            }
          }
        } catch (_) {}
      }
    }
  }

  function applyTheme(v) {
    const prev = document.documentElement.getAttribute('data-theme') || 'midnight';
    const next = v || 'midnight';
    document.documentElement.setAttribute('data-theme', next);
    // Walled Garden font lock: entering or leaving bluebubble
    if (next === 'bluebubble' && prev !== 'bluebubble') {
      _savedFontBeforeWG = fontPicker.value;
      setFontPickerLocked(true);
    } else if (next !== 'bluebubble' && prev === 'bluebubble') {
      setFontPickerLocked(false);
    }
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
  const DM_TARGET_ID = URL_PARAMS.get('dm') || '';
  const DM_MODE = !!DM_TARGET_ID;
  // Landing-mode multiplexing: when this page is served at /c/<code>, the
  // server substitutes a "?channel=<code>" query string here so every API
  // call names its channel. Single-channel mode leaves it '' (the server
  // already knows its one channel) — the token below is valid JS as-is.
  const API_QS = /*__API_QS__*/'';

  // ── State ──
  // How recently a real gesture must have happened for a scroll to count as
  // the user's. Covers a smooth-scroll animation started by a real drag.
  const USER_INTENT_MS = 1500;
  let CAN_CULL = false;
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
    initialLoad: true,              // pin to newest until the history burst settles
    soundEnabled: false,
    chimeVolume: 0.33,
    soundScope: 'all',        // 'mention' | 'all' — chime scope, INDEPENDENT of
                              // notifyScope. Defaults to 'all' to preserve the
                              // historical "chime on any new message" behavior
                              // for operators who already had the chime on.
    notifyScope: 'mention',   // 'mention' | 'all'
    notifyWhen: 'hidden',     // 'hidden' | 'always'
    pendingAttachments: [],   // images uploaded but not yet attached to a send
    unreadCount: 0,                 // for tab title while hidden
    jumpUnread: 0,                  // messages arrived while user was scrolled up
    lastSeenId: 0,                  // highest msg id the user has caught up to
    userIntentAt: 0,                // timestamp of the last real scroll gesture
                                    // (session-based; drives the unread divider)
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

  // A single character-class run + optional :line[:col] — a flat quantifier
  // (no nested `(…+…)+`), so it scans in LINEAR time and can't be driven into
  // catastrophic/quadratic backtracking (ReDoS) by a long slash-free blob.
  // Candidates are then post-filtered: a real path must contain a '/'.
  const FILE_PATH_RUN_RE = /[A-Za-z0-9_.~/-]+(?::\d+(?::\d+)?)?/g;
  const FILE_PATH_MAX_LEN = 4096;
  // Per-path validation cache (path token → exists bool). Shared across every
  // message so re-renders and repeated paths never re-hit the endpoint.
  // Bounded: keys are every distinct path-like token ever seen, including inert
  // look-alikes, on a tab that may live for days. Oldest-out at the cap.
  const FILE_PATH_CACHE_MAX = 5000;
  const filePathCache = new Map();
  function cacheFilePath(token, ok) {
    if (filePathCache.size >= FILE_PATH_CACHE_MAX) {
      const oldest = filePathCache.keys().next();
      if (!oldest.done) filePathCache.delete(oldest.value);
    }
    filePathCache.set(token, ok);
  }
  // Said once per page: without it the feature simply is not there for a viewer
  // the server will not trust, which is indistinguishable from "none of those
  // files exist".
  let _fileLinksNoticeShown = false;
  function noteFileLinksUnavailable() {
    if (_fileLinksNoticeShown) return;
    _fileLinksNoticeShown = true;
    const bar = document.createElement('div');
    bar.className = 'file-links-unavailable';
    bar.setAttribute('role', 'status');
    bar.textContent = 'File paths are not clickable here — reveal-in-Finder is '
                    + 'limited to the machine running the dashboard.';
    if (chat && chat.parentNode) chat.parentNode.insertBefore(bar, chat);
  }

  function detectFilePathCandidates(text) {
    const out = [];
    if (!text) return out;
    FILE_PATH_RUN_RE.lastIndex = 0;
    let m;
    while ((m = FILE_PATH_RUN_RE.exec(text)) !== null) {
      let tok = m[0];
      const start = m.index;
      if (tok.indexOf('/') === -1) continue;               // not path-like (no separator)
      // Require a real FILENAME SEGMENT, not just separators: a candidate must
      // carry at least one name character ([A-Za-z0-9_]). This rejects a BARE
      // '/' (and pure-punctuation runs like '//', './', '-/-') that a slash used
      // as prose punctuation produces — "reload / incognito", "high / low",
      // "#" / "!". Those would otherwise validate against on-disk roots ('/'
      // exists!) and wrongly pick up a folder link. Slash-joined WORDS ('and/or',
      // 'high/medium/low') still pass here but are gated by real existence, so
      // they only link if they genuinely resolve. (Server rejects roots too —
      // defense in depth.)
      if (!/[A-Za-z0-9_]/.test(tok)) continue;
      // Drop a single trailing sentence period ("…/c.py." → "…/c.py"); never a
      // ".." tail. Trailing trim only, so the start offset stays valid.
      tok = tok.replace(/([^.\/])\.$/, '$1');
      if (!tok || tok.length > FILE_PATH_MAX_LEN) continue;
      out.push({ start, end: start + tok.length, token: tok });
    }
    return out;
  }

  // Wrap candidate tokens the caller marks valid (isValid(token) === true) in a
  // .file-link. Skips code/pre/existing links, the @/#/! sigil spans, and
  // already-linkified paths, so we never double-wrap or touch literal code.
  // onClick (optional) is attached to each created link.
  function linkifyValidatedPaths(root, isValid, onClick) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest(
        'code, pre, a, .inline-mention, .inline-ref, .inline-bang, .file-link')) continue;
      if (detectFilePathCandidates(node.nodeValue || '').some(c => isValid(c.token)))
        nodes.push(node);
    }
    for (const node of nodes) {
      const text = node.nodeValue || '';
      const cands = detectFilePathCandidates(text).filter(c => isValid(c.token));
      if (!cands.length) continue;
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const c of cands) {
        if (c.start < cursor) continue;   // defensive: skip any overlap
        frag.appendChild(document.createTextNode(text.slice(cursor, c.start)));
        const link = document.createElement('a');
        link.className = 'file-link';
        link.textContent = c.token;
        link.dataset.path = c.token;
        link.setAttribute('role', 'button');
        link.setAttribute('tabindex', '0');
        link.title = 'Reveal in Finder';
        if (typeof onClick === 'function') {
          link.addEventListener('click', (e) => { e.preventDefault(); onClick(c.token, link); });
          link.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(c.token, link); }
          });
        }
        frag.appendChild(link);
        cursor = c.end;
      }
      frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.replaceWith(frag);
    }
  }

  // Brief inline state on a file link after a reveal attempt (no navigation,
  // no modal). Success/failure both auto-revert; failures surface the reason
  // in the tooltip.
  function flashFileLink(link, ok, msg) {
    if (!link || !link.classList) return;
    const cls = ok ? 'file-link-ok' : 'file-link-err';
    link.classList.add(cls);
    // A failure reason written to link.title is unreadable: the pointer is
    // already over the link when you click, so the native tooltip does not
    // re-fire, and touch has no tooltip at all. Show it inline instead, and
    // announce it, so the reason survives long enough to be read.
    if (!ok && msg) {
      const prev = link.parentNode && link.parentNode.querySelector('.file-link-note');
      if (prev) prev.remove();
      const note = document.createElement('span');
      note.className = 'file-link-note';
      note.setAttribute('role', 'status');
      note.textContent = ' — ' + msg;
      if (link.parentNode) link.parentNode.insertBefore(note, link.nextSibling);
      setTimeout(() => { note.remove(); }, 6000);
    }
    setTimeout(() => { link.classList.remove(cls); }, 1500);
  }

  async function revealPath(path, link) {
    if (typeof fetch !== 'function') return;
    try {
      const r = await fetch('/api/reveal', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data && data.ok) flashFileLink(link, true);
      else flashFileLink(link, false, (data && data.error) || ('reveal failed (' + r.status + ')'));
    } catch (e) {
      flashFileLink(link, false, 'reveal failed: ' + e.message);
    }
  }

  // Detect candidate paths in a rendered message body, validate the uncached
  // ones against the server (batched into one request per message), then
  // linkify only those confirmed to exist. Fire-and-forget from paintBody.
  // Relative candidates are resolved by the server against ITS cwd (best
  // effort); if they don't resolve there, they simply stay unlinked.
  // Validation is batched across every body painted in the same tick. A
  // 200-message history burst otherwise fired ~130 separate POSTs (measured
  // 317ms of pure per-request overhead against 1.8ms for the same candidates
  // sent once); the filesystem work was never the cost. Each caller registers
  // its root, one flush resolves every outstanding token, then each root is
  // linkified from the shared cache.
  let _pendingRoots = [];
  let _pendingTokens = new Set();
  let _flushTimer = null;

  async function _flushFilePathValidation() {
    _flushTimer = null;
    const roots = _pendingRoots; _pendingRoots = [];
    const tokens = _pendingTokens; _pendingTokens = new Set();
    const need = [...tokens].filter(t => !filePathCache.has(t));
    for (let i = 0; i < need.length; i += 200) {   // server caps at 200/req
      const chunk = need.slice(i, i + 200);
      try {
        const r = await fetch('/api/path/validate', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: chunk }),
        });
        if (r.ok) {
          const data = await r.json().catch(() => ({}));
          const ex = (data && data.exists) || {};
          for (const t of chunk) cacheFilePath(t, ex[t] === true);
        } else if (r.status === 403) {
          noteFileLinksUnavailable();
          for (const t of chunk) cacheFilePath(t, false);
        }
      } catch (e) { /* leave uncached — just won't linkify this pass */ }
    }
    for (const root of roots) {
      if (!root.isConnected) continue;     // message re-rendered or removed
      linkifyValidatedPaths(root, (t) => filePathCache.get(t) === true, revealPath);
    }
  }

  function decorateFilePaths(root) {
    if (!root || typeof fetch !== 'function') return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let found = false;
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest(
        'code, pre, a, .inline-mention, .inline-ref, .inline-bang, .file-link')) continue;
      for (const c of detectFilePathCandidates(node.nodeValue || '')) {
        _pendingTokens.add(c.token); found = true;
      }
    }
    if (!found) return;
    _pendingRoots.push(root);
    if (_flushTimer === null) _flushTimer = setTimeout(_flushFilePathValidation, 0);
  }

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

  const SYSTEM_WORDS = new Set(['claimed', 'done', 'cancelled', 'released',
    'retracted', 'joined', 'left', 'ended', 'locked', 'unlocked', 'status',
    'pinned', 'renamed', 'culled']);
  // System notices come in two shapes: "[word #id] ..." (the task family) and
  // "[word] ..." (join/pin/lock/unlock/rename). A plain startsWith('[word ')
  // only ever matched the first, so the second rendered as ordinary markdown.
  // Requiring a space-or-end after the "]" keeps a markdown link such as
  // [done](url) from being muted as a system notice.
  function isSystemContent(s) {
    const m = /^\[([a-z]+)(?:\s|\](?:\s|$))/.exec(s || '');
    return !!m && SYSTEM_WORDS.has(m[1]);
  }

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

  function mentionMemberForToken(token, allowedIds) {
    const lower = (token || '').toLowerCase();
    if (lower === 'all') return { id: 'all', name: 'all' };
    for (const mem of state.members.values()) {
      if (allowedIds && !allowedIds.has(mem.id)) continue;
      if ((mem.id || '').toLowerCase() === lower ||
          (mem.name || '').toLowerCase() === lower) return mem;
    }
    return null;
  }

  // Find only syntactically complete, roster-resolved @mentions. Unknown
  // @words stay unadorned, which doubles as feedback that they will not ping
  // a participant.
  function collectMentionMatches(text, allowedIds) {
    const matches = [];
    const re = /(^|[^A-Za-z0-9_])@([A-Za-z0-9_.-]+)/g;
    let hit;
    while ((hit = re.exec(text || ''))) {
      // The token class greedily swallows trailing sentence punctuation
      // (".", "-") — e.g. "thanks @Claude." captures "Claude.". Resolve the
      // full token first (so names that legitimately contain "."/"-" like
      // jen.chen / gabe-guest still match), then trim trailing "."/"-" and
      // retry so the mention still highlights, matching the server's routing.
      let token = hit[2];
      let member = mentionMemberForToken(token, allowedIds);
      while (!member && (token.endsWith('.') || token.endsWith('-'))) {
        token = token.slice(0, -1);
        member = mentionMemberForToken(token, allowedIds);
      }
      if (!member) continue;
      const start = hit.index + hit[1].length;
      matches.push({ start, end: start + token.length + 1, member });
    }
    return matches;
  }

  function decorateInlineMentions(root, mentionIds) {
    if (!root || !mentionIds || !mentionIds.length) return;
    const allowed = new Set(mentionIds);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest('code, pre, a, .inline-mention')) continue;
      if (collectMentionMatches(node.nodeValue || '', allowed).length) nodes.push(node);
    }
    for (const node of nodes) {
      const text = node.nodeValue || '';
      const matches = collectMentionMatches(text, allowed);
      if (!matches.length) continue;
      const frag = document.createDocumentFragment();
      let cursor = 0;
      for (const match of matches) {
        frag.appendChild(document.createTextNode(text.slice(cursor, match.start)));
        const span = document.createElement('span');
        span.className = 'inline-mention';
        span.textContent = text.slice(match.start, match.end);
        span.dataset.memberId = match.member.id;
        span.title = match.member.id === 'all'
          ? 'Mentions every participant'
          : 'Mentions ' + (match.member.name || match.member.id);
        if (match.member.id !== 'all') {
          span.style.setProperty('--mention-member-color', colorFor(match.member.id));
        }
        frag.appendChild(span);
        cursor = match.end;
      }
      frag.appendChild(document.createTextNode(text.slice(cursor)));
      node.replaceWith(frag);
    }
  }

  // Pure: draft text -> mirror HTML. Split out from the DOM write so the
  // escaping can actually be tested — this is the one path that builds markup
  // from raw user input, so a missed escape here is exploitable by typing.
  function composerMentionHtml(text) {
    text = text || '';
    const matches = collectMentionMatches(text, null);
    let html = '';
    let cursor = 0;
    for (const match of matches) {
      html += escapeHtml(text.slice(cursor, match.start));
      // colorFor returns a fixed palette hex (injection-safe); @all has no
      // per-member color and falls back to the rainbow shimmer via its own class.
      const isAll = match.member.id === 'all';
      const mc = isAll ? '' : colorFor(match.member.id);
      const styleAttr = mc ? ' style="--mention-member-color:' + mc + '"' : '';
      const cls = isAll ? 'composer-mention composer-mention-all' : 'composer-mention';
      html += '<span class="' + cls + '"' + styleAttr + '>' +
              escapeHtml(text.slice(match.start, match.end)) + '</span>';
      cursor = match.end;
    }
    html += escapeHtml(text.slice(cursor));
    // Preserve a final blank line so the mirror stays aligned with textarea
    // scrollHeight and wrapping behavior.
    return html + (text.endsWith('\n') ? '\n ' : '');
  }

  function renderComposerMentionHighlights() {
    if (!inputHighlight) return;
    inputHighlight.innerHTML = composerMentionHtml(input.value || '');
    inputHighlight.scrollTop = input.scrollTop;
    inputHighlight.scrollLeft = input.scrollLeft;
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
  let _initialSettleDeadline = 0;
  function settleInitialLoad() {
    _initialSettleTimer = null;
    _initialSettleDeadline = 0;
    state.initialLoad = false;
    // seedBaseline + disownScroll come from the unread-divider work: the
    // baseline must be taken once the history burst has settled, and the
    // programmatic scroll below must NOT count as user intent — otherwise
    // opening a channel marks everything read before the reader has seen it.
    seedBaseline();
    requestAnimationFrame(() => { disownScroll(); chat.scrollTop = chat.scrollHeight; });
  }
  function scheduleInitialSettle() {
    // The quiet gap is rescheduled on each append, so a burst spaced under
    // 250ms would hold initialLoad open for its whole duration — and the chime
    // is gated on that flag, so it would be muted exactly during an agent
    // flurry. Cap the total wait so a dense burst still settles.
    const now = Date.now();
    if (!_initialSettleDeadline) _initialSettleDeadline = now + 3000;
    if (_initialSettleTimer) clearTimeout(_initialSettleTimer);
    // Both sides changed this scheduler. Kept: the renderer's CAPPED wait (a
    // dense burst must still settle, or the chime stays muted through an agent
    // flurry) driving the unread work's settle body, which now lives in
    // settleInitialLoad() above. Taking either side alone would have silently
    // dropped the other's fix.
    const wait = Math.max(0, Math.min(250, _initialSettleDeadline - now));
    _initialSettleTimer = setTimeout(settleInitialLoad, wait);
  }

  function appendMessage(m) {
    if (state.seenMsgIds.has(m.id)) return;
    state.seenMsgIds.add(m.id);
    state.messages.set(m.id, m);
    ingestMessageForStats(m);

    const isMine = m.member_id === state.operator.id;
    const isSystem = isSystemContent(m.content || '');
    const mentionsOperator = (m.mentions || []).includes(state.operator.id);
    // '!' sigils land in a separate `bangs` column, never in `mentions`.
    // A bang is the last-resort signal an agent cannot be opted out of, so
    // it must reach a mention-scoped chime too — otherwise the one message
    // that paints a red BANG bar is the one message that makes no sound.
    const bangsOperator = (m.bangs || []).includes(state.operator.id);

    const div = document.createElement('div');
    div.className = 'msg' + (isMine ? ' mine' : '') + (isSystem ? ' system' : '')
                  + (mentionsOperator ? ' targeted' : '');
    div.dataset.msgId = String(m.id);
    div.dataset.sender = m.member_id || '';
    div.dataset.search = (m.content || '').toLowerCase() + ' '
                       + humanizeIdSigils(m.content || '').toLowerCase() + ' '
                       + (m.member_name || '').toLowerCase();

    // Message-number gutter (#N) — visible only when #chat.show-msg-nums.
    // Absolute + full-height so it centres on the whole message; the inner
    // span is position:sticky (see CSS) so the number rides the visible slice.
    const numGutter = document.createElement('div');
    numGutter.className = 'msg-num-gutter';
    // No ARIA here on purpose. The visible "#N" is real text inside the
    // message's own subtree, ahead of the timestamp in DOM order, so a screen
    // reader already reads the number then the message — the same order a
    // sighted reader gets. A role/aria-label would duplicate that text and add
    // one region boundary per message; aria-hidden would take it away entirely.
    const numEl = document.createElement('span');
    numEl.className = 'msg-num';
    numEl.textContent = '#' + m.id;
    numEl.title = 'message ' + m.id;
    // The number is selectable/copyable; don't let a click on it also toggle
    // the message's compact/expand state.
    numEl.addEventListener('click', (e) => e.stopPropagation());
    numGutter.appendChild(numEl);
    div.appendChild(numGutter);

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
      decorateInlineMentions(body, m.mentions || []);
      // Async: validate path-like tokens with the server and linkify the real
      // ones (reveal-in-Finder). Fire-and-forget so paint stays synchronous.
      decorateFilePaths(body);
    }
    div.appendChild(body);

    // Image attachments — inline thumbnails, click opens full size in a new tab.
    if (m.attachments && m.attachments.length) {
      const wrap = document.createElement('div');
      wrap.className = 'msg-attachments';
      for (const att of m.attachments) {
        // API_QS carries ?channel=<code> in landing mode; without it the
        // server cannot tell which channel's attachment is being asked for.
        const url = '/api/attachment/' + att.id + API_QS;
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener';
        const img = document.createElement('img');
        img.className = 'msg-img';
        img.src = url;
        img.alt = att.filename || 'image';
        img.loading = 'lazy';
        // Late-loading images reflow taller; keep us pinned if near bottom.
        img.addEventListener('load', () => {
          const nb = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 120;
          if (state.initialLoad || nb) chat.scrollTop = chat.scrollHeight;
        });
        // A failing image otherwise collapses to a bare broken-image glyph:
        // the viewer cannot tell whether it was deleted, whether they are not
        // allowed to see it (the read endpoint requires a resolved identity),
        // or whether the network hiccupped.
        img.addEventListener('error', () => {
          const note = document.createElement('span');
          note.className = 'msg-img-missing';
          note.textContent = '🖼 image unavailable — ' + (att.filename || 'attachment');
          note.title = 'It may have been removed, or you may not have access '
                     + 'to attachments on this machine.';
          if (a.parentNode) a.parentNode.replaceChild(note, a);
        });
        // Opening the image should not also toggle the message's compact state.
        a.addEventListener('click', (e) => { e.stopPropagation(); });
        a.appendChild(img);
        wrap.appendChild(a);
      }
      div.appendChild(wrap);
    }

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

    // Mark sender-change boundaries for bluebubble inter-bubble spacing
    const prevMsg = chat.lastElementChild;
    if (prevMsg && prevMsg.dataset.sender !== div.dataset.sender) {
      div.classList.add('sender-break');
    }
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
    } else if (nearBottom && !document.hidden) {
      // Only auto-pin to the bottom when the tab is VISIBLE. Pinning while
      // hidden would leave us at the bottom on return, so the "new messages"
      // divider for what arrived while away would be marked caught-up and lost.
      chat.scrollTop = chat.scrollHeight;
    } else {
      // Same rule as the divider: your own message is not something you have
      // yet to read. Without this, sending while scrolled up raises the
      // jump-to-latest badge as well as the divider — two separate claims that
      // there is something new, both of them about you.
      if (!isMine) state.jumpUnread++;
      updateJumpButton();
    }

    // Unread divider: if the user is keeping up (tab visible + at/near bottom),
    // they've seen this message; otherwise it's unread since they looked away or
    // scrolled up, and a "new messages" divider is drawn before the first such.
    if (state.initialLoad) {
      // History burst. The baseline is set once in seedBaseline() when the
      // burst settles; advancing per-message here would race a hidden tab.
    } else if (!document.hidden && nearBottom && !isHiddenMsg(div)) {
      // Only messages the user can actually see count as read on arrival, and
      // the advance has to be the same ascending walk markCaughtUp does — a
      // bare Math.max would jump the watermark over earlier messages a filter
      // is hiding, which is the very thing that walk exists to prevent. One
      // function owns the invariant.
      markCaughtUp();
    } else {
      refreshUnreadDivider();
    }

    // Tab-title badge when hidden
    if (document.hidden && !isMine) {
      state.unreadCount++;
      updateTitle();
    }

    // Desktop notification on @you while hidden (opt-in). In DM mode,
    // only fire for the DM target — don't pull focus for other channel chatter.
    const dmOk = (!state.dmTargetId || m.member_id === state.dmTargetId);
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

    // In-page chime for a new peer message (opt-in, focus-agnostic). The scope
    // (soundScope) is kept independent of the desktop-notify scope, so a quiet
    // chime on all messages can coexist with a popup only on @mentions, or vice
    // versa. Reuses the same mentionsOperator predicate the notify block uses.
    // Skip the primed-history burst on load/reconnect — chime only for LIVE
    // messages once state.initialLoad has settled. Without this, a refresh plays
    // every historical chime at once (overlapping waveforms = loud + phasey).
    // In a DM view every channel message is still appended and merely
    // CSS-hidden, so without this the operator hears a chime for a message
    // they cannot see — an audible event with no visible cause.
    if (shouldChime({
          initialLoad: state.initialLoad, soundEnabled: state.soundEnabled,
          isMine, isSystem,
          dmVisible: (!state.dmTargetId || isRelevantInDm(m)),
          scope: state.soundScope,
          addressed: mentionsOperator || bangsOperator,
        })) playChime();
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
          decorateInlineMentions(body, m.mentions || []);
          decorateFilePaths(body);
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
    const order = { working: 0, active: 1, idle: 2, stale: 3, dead: 4 };
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
    // Drop members the roster no longer lists. state.members backs the composer
    // target chips (and their Alt+N hotkeys), @-autocomplete, ack badges and
    // watermark pins — without this a culled member stays selectable until reload.
    const liveIds = new Set(members.map(m => m.id));
    for (const id of [...state.members.keys()]) {
      if (!liveIds.has(id)) state.members.delete(id);
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
      const order = { working: 0, active: 1, idle: 2, stale: 3, dead: 4 };
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
    // A roster arrival/rename can turn an existing @token from unresolved to
    // valid without another keystroke, so refresh the composer mirror too.
    updatePreview();
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

  // Remove a member from the channel (roster × button). Confirms first — it
  // releases their claimed tasks + locks and posts a [culled] message. The SSE
  // roster refresh drops them from the sidebar; it does not stop a live agent's
  // process (it would just start erroring and could reconnect).
  async function cullMember(id, name, btn) {
    // Single backslash-n. This script is embedded in a Python raw string, so a
    // doubled backslash survives to the browser verbatim and the dialog would
    // display the escape sequence as literal text.
    if (!confirm('Remove ' + name + ' from the channel?\n\n'
        + 'This cannot be undone. Their claimed tasks and held locks are '
        + 'released, their sessions are revoked, and a [culled] notice is '
        + 'posted to the channel.\n\n'
        + 'It does not stop a running process — it only removes them here.')) return;
    // Disable while in flight and bound the wait. Without this the button gives
    // no signal at all after you have confirmed an irreversible action — and a
    // request CAN hang indefinitely: several dashboard tabs consume the
    // browser's per-origin connection cap with their SSE streams, and the DM
    // button opens tabs, so reaching the cap is a normal thing to do.
    const label = btn ? btn.textContent : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Removing…'; }
    try {
      const r = await fetch('/api/cull' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_member_id: id }),
        signal: (AbortSignal && AbortSignal.timeout) ? AbortSignal.timeout(15000) : undefined,
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'unknown' }));
        alert('remove failed: ' + (err.error || r.status));
      }
    } catch (e) {
      alert(e.name === 'TimeoutError'
        ? 'remove timed out — the dashboard did not get a reply, so ' + name
          + ' may or may not have been removed. Reload to check.'
        : 'remove failed: ' + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = label; }
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

    // Remove control — revealed only when the row is expanded, so it can't be
    // mis-clicked from the collapsed roster (on a phone the old always-visible
    // × sat 53px from the drawer's own close ×, same glyph, at a sub-44px
    // target). Hidden entirely for identities the server would refuse, rather
    // than walking them through two dialogs into a 403.
    if (!DM_MODE && m.id !== state.operator.id && CAN_CULL) {
      const actions = document.createElement('div');
      actions.className = 'member-actions';
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'rm-btn';
      rm.textContent = 'Remove';
      rm.title = `Remove ${m.name} from this channel — releases their tasks and locks, and cannot be undone`;
      rm.addEventListener('click', (e) => { e.stopPropagation(); cullMember(m.id, m.name, rm); });
      actions.appendChild(rm);
      row.appendChild(actions);
    }

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
      // Codex publishers refresh their snapshot while the TUI is alive even
      // when no new token count arrived, so a fresh file can carry an old
      // number. data_age_s is the age of the reading itself — say so rather
      // than presenting an hours-old figure as current.
      const dAge = c.data_age_s;
      const staleNote = (typeof dAge === 'number' && dAge > 300)
        ? ` (as of ${dAge >= 3600 ? Math.round(dAge/3600)+'h' : Math.round(dAge/60)+'m'} ago)`
        : '';
      const ctxRows = [
        // cwLabel is '' when the window size is unknown — don't render "45% of ".
        ['context', (cwLabel ? `${pct} of ${cwLabel}` : pct) + escapeHtml(staleNote),
         staleNote ? '' : pctClass],
        ['model', escapeHtml(model), ''],
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
    renderComposerMentionHighlights();
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
      const n = refs.map(m => `<span class="tgt" style="color:var(--ref-chip)">#${escapeHtml(m.name)}</span>`).join(', ');
      parts.push(`refs: ${n}`);
    }
    if (bangs.length || /(^|\s)!all(\b|$)/.test(txtL)) {
      const n = bangs.map(m => `<span class="tgt" style="color:var(--bang-chip)">!${escapeHtml(m.name)}</span>`).join(', ');
      const allTag = /(^|\s)!all(\b|$)/.test(txtL) ? '<span class="tgt" style="color:var(--bang-chip)">!all</span>' : '';
      parts.push(`<b style="color:var(--bang-chip)">BANGS (unfilterable)</b>: ${[allTag, n].filter(Boolean).join(', ')}`);
    }
    preview.innerHTML = parts.join('  ·  ');
  }
  function autoResizeInput() {
    input.style.height = 'auto';
    input.style.height = Math.min(160, Math.max(36, input.scrollHeight)) + 'px';
    if (inputHighlight) {
      inputHighlight.style.height = input.style.height;
      inputHighlight.scrollTop = input.scrollTop;
      inputHighlight.scrollLeft = input.scrollLeft;
    }
  }

  // ── Send ──
  // ── Image attachments (composer upload) ──
  const attachBtn = document.getElementById('attach-btn');
  const fileInput = document.getElementById('file-input');
  const attachStrip = document.getElementById('attach-strip');
  const composerEl = document.getElementById('composer');

  function renderAttachStrip() {
    attachStrip.innerHTML = '';
    state.pendingAttachments.forEach((att, i) => {
      const t = document.createElement('div');
      t.className = 'attach-thumb' + (att.uploading ? ' uploading' : '');
      if (att.url) {
        const img = document.createElement('img');
        img.src = att.url;
        t.appendChild(img);
      }
      if (!att.uploading) {
        const rm = document.createElement('button');
        rm.className = 'rm'; rm.textContent = '×'; rm.title = 'remove';
        rm.addEventListener('click', () => {
          dropSlot(att);
          renderAttachStrip();
        });
        t.appendChild(rm);
      }
      attachStrip.appendChild(t);
    });
  }

  function revokeBlob(att) {
    if (att && att.url && att.url.indexOf('blob:') === 0) URL.revokeObjectURL(att.url);
  }
  function dropSlot(slot) {
    revokeBlob(slot);
    const idx = state.pendingAttachments.indexOf(slot);
    if (idx >= 0) state.pendingAttachments.splice(idx, 1);
  }

  // Mirrors MAX_UPLOAD_BYTES in this file's Python half, so a huge file is
  // refused before it is pushed over the wire. A literal rather than a
  // substitution, so the served bundle carries no placeholder the test
  // harness would need to know about; the server still enforces the real
  // limit, so drift can only make the client stricter, never unsafe.
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

  async function uploadImage(file) {
    if (!file) return;
    if (!file.type || !/^image\//.test(file.type)) {
      // The composer flashes an accepting outline on dragover, so returning
      // silently here tells the user the drop landed and then does nothing.
      // Drag-and-drop also bypasses the file picker's accept= filter entirely.
      alert('"' + (file.name || 'that file') + '" is not an image. '
            + 'PNG, JPEG, GIF and WebP can be attached.');
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      // Checked here as well as server-side so a 40MB photo is not pushed over
      // the wire before being refused, and so the number is human-sized.
      const mb = (n) => (n / (1024 * 1024)).toFixed(1).replace(/\.0$/, '');
      alert('"' + (file.name || 'that image') + '" is ' + mb(file.size)
            + ' MB — the limit is ' + mb(MAX_UPLOAD_BYTES) + ' MB.');
      return;
    }
    if (state.pendingAttachments.length >= 8) { alert('max 8 images per message'); return; }
    const slot = { uploading: true, url: URL.createObjectURL(file) };
    state.pendingAttachments.push(slot);
    renderAttachStrip();
    try {
      const r = await fetch('/api/upload' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': file.type, 'X-Filename': encodeURIComponent(file.name || 'image') },
        body: file,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        alert('upload failed: ' + (data.error || r.status));
        dropSlot(slot);
      } else {
        revokeBlob(slot);                       // free the local preview blob
        slot.id = data.id;
        slot.uploading = false;
        slot.url = '/api/attachment/' + data.id + API_QS;
      }
    } catch (e) {
      alert('upload failed: ' + e.message);
      dropSlot(slot);
    }
    renderAttachStrip();
  }

  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    for (const f of fileInput.files) uploadImage(f);
    fileInput.value = '';
  });
  input.addEventListener('paste', (e) => {
    const items = (e.clipboardData || {}).items || [];
    for (const it of items) {
      if (it.kind === 'file' && /^image\//.test(it.type)) {
        const f = it.getAsFile();
        if (f) { e.preventDefault(); uploadImage(f); }
      }
    }
  });
  ['dragover', 'dragenter'].forEach(ev => composerEl.addEventListener(ev, (e) => {
    e.preventDefault(); composerEl.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => composerEl.addEventListener(ev, (e) => {
    e.preventDefault(); composerEl.classList.remove('dragover');
  }));
  composerEl.addEventListener('drop', (e) => {
    const files = (e.dataTransfer || {}).files || [];
    for (const f of files) uploadImage(f);
  });

  // Remove specific slots, preserving anything added since.
  function dropAttachments(slots) {
    const gone = new Set(slots);
    state.pendingAttachments = state.pendingAttachments.filter(a => !gone.has(a));
  }

  async function sendMessage() {
    let text = input.value.trim();
    const readyAtt = state.pendingAttachments.filter(a => a.id && !a.uploading);
    if (state.pendingAttachments.some(a => a.uploading)) {
      alert('wait for image upload to finish'); return;
    }
    if (!text && readyAtt.length === 0) return;
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
      const r = await fetch('/api/send' + API_QS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text, mentions: mentionIds,
                               attachment_ids: readyAtt.map(a => a.id) }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ error: 'unknown' }));
        // A rejected relink means the server already consumed these ids on an
        // earlier attempt whose response we lost — the images DID post. Drop
        // them rather than leaving a composer that can never send again and
        // that invites the user to delete images they actually published.
        if (/already-linked/.test(err.error || '')) {
          dropAttachments(readyAtt);
          renderAttachStrip();
          alert('Those images were already posted — the earlier send did go '
                + 'through even though it reported an error. Removed them from '
                + 'the composer; your text is still here.');
        } else {
          alert('send failed: ' + (err.error || r.status));
        }
        return;
      }
      input.value = '';
      // Splice out exactly what we sent. Reassigning to [] would also destroy
      // an image pasted DURING the in-flight send: its upload completes into a
      // slot no longer in the array, so it vanishes from the strip with no
      // error and is orphaned server-side.
      dropAttachments(readyAtt);
      renderAttachStrip();
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
  input.addEventListener('scroll', () => {
    if (!inputHighlight) return;
    inputHighlight.scrollTop = input.scrollTop;
    inputHighlight.scrollLeft = input.scrollLeft;
  });
  // IME / dead-key / emoji composition: the provisional (pre-commit) glyphs are
  // drawn by the browser in the textarea itself, which is normally transparent
  // (the colored mirror is what shows). Reveal the textarea and hide the mirror
  // for the duration of composition so the preview is visible; on commit, revert
  // and re-render the mirror from the now-updated value.
  input.addEventListener('compositionstart', () => {
    input.style.color = 'var(--fg)';
    // visibility, not colour: a mention chip sets its own colour, so it would
    // stay painted over the revealed textarea and double the token.
    if (inputHighlight) inputHighlight.classList.add('composing');
  });
  input.addEventListener('compositionend', () => {
    input.style.color = '';
    if (inputHighlight) inputHighlight.classList.remove('composing');
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
    // Re-anchor the unread divider to the first still-visible unread message
    // (a filter may have hidden the one it was sitting before).
    refreshUnreadDivider();
  }
  function applyFilterToNode(node) {
    // Skip non-message children (e.g. the unread divider) — they have no msgId.
    if (!node.dataset || node.dataset.msgId === undefined) return;
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

  // ── Message-number toggle (#N in the left gutter) ──
  // Persists per-origin via localStorage, default ON. Toggling just flips a
  // class on #chat; pure-CSS sticky positioning handles the rest (see .msg-num).
  let msgNumsOn = true;
  try { msgNumsOn = localStorage.getItem('trio.msgNumbers') !== '0'; } catch (_) {}
  function applyMsgNums() {
    chat.classList.toggle('show-msg-nums', msgNumsOn);
    btnMsgNum.classList.toggle('on', msgNumsOn);
  }
  applyMsgNums();
  btnMsgNum.addEventListener('click', () => {
    msgNumsOn = !msgNumsOn;
    try { localStorage.setItem('trio.msgNumbers', msgNumsOn ? '1' : '0'); } catch (_) {}
    applyMsgNums();
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
  // Does a peer message qualify for the chime under the current scope?
  //   'all'     → every peer message chimes.
  //   'mention' → only messages that @mention the operator chime.
  // Pure (no DOM/state) so it can be unit-tested via the harness hook. The
  // on/off master is state.soundEnabled + the btn-sound pill; this only refines
  // an already-enabled chime, and stays independent of notifyScope.
  function chimeScopeAllows(scope, mentionsOperator) {
    return scope === 'all' ? true : !!mentionsOperator;
  }
  // The whole chime decision, pure and testable. The gate that actually
  // matters is not the scope predicate but the conditions around it: the
  // history burst, your own messages, system notices, and a DM view where the
  // message is appended but hidden.
  function shouldChime(o) {
    if (!o || o.initialLoad) return false;      // primed history, not live
    if (!o.soundEnabled) return false;
    if (o.isMine || o.isSystem) return false;
    if (!o.dmVisible) return false;             // appended but CSS-hidden
    return chimeScopeAllows(o.scope, o.addressed);
  }
  let _lastChimeAt = 0;
  function playChime() {
    const ctx = ensureAudio();
    if (!ctx) return;
    // Coalesce. A reconnect drains the whole offline backlog through one
    // synchronous handler, and each call ramps a fresh gain to full volume at
    // essentially the same currentTime — forty of those sum into clipping
    // rather than forty chimes. One sound per burst is the useful signal.
    const nowMs = Date.now();
    if (nowMs - _lastChimeAt < 400) return;
    _lastChimeAt = nowMs;
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

  // ── Sound (chime) toggle — off by default; the pill is the on/off master and
  //    state.soundScope (settings drawer) refines which peer messages chime. ──
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
  let _sideCollapsed = false;
  try { _sideCollapsed = localStorage.getItem('trio.sideCollapsed') === '1'; } catch (_) {}
  applySidebar(_sideCollapsed);
  function toggleSidebar() {
    _sideCollapsed = !_sideCollapsed;
    applySidebar(_sideCollapsed);
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
  const btnSideClose = document.getElementById('side-close');
  function closeMobileSidebar() {
    appEl.classList.remove('mobile-side-open');
    btnSide.classList.toggle('on', false);
    if (btnMobileRoster) btnMobileRoster.classList.toggle('on', false);
  }
  function toggleMobileSidebar() {
    const open = appEl.classList.toggle('mobile-side-open');
    btnSide.classList.toggle('on', open);
    if (btnMobileRoster) btnMobileRoster.classList.toggle('on', open);
  }
  // The in-sidebar close control picks the same path as the header pill.
  function closeSidebar() {
    if (window.innerWidth <= 768) { closeMobileSidebar(); }
    else if (!_sideCollapsed) { toggleSidebar(); }
  }
  if (btnMobileRoster) btnMobileRoster.addEventListener('click', toggleMobileSidebar);
  if (btnSideClose) btnSideClose.addEventListener('click', closeSidebar);
  if (mobileScrim) mobileScrim.addEventListener('click', closeMobileSidebar);
  // Auto-collapse sidebar on narrow viewports at load
  if (window.innerWidth <= 768) {
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
    ['Message numbers', 'btn-msgnum'],
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

  // Build a <select> preloaded with `options` ([value, label] pairs) and the
  // `current` value pre-selected. Shared by the chime + notification prefs.
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

  // Chime scope — off is the btn-sound pill; this refines an enabled chime to
  // fire on every message or only @mentions. Independent of the notify scope.
  try {
    const ss = localStorage.getItem('trio.soundScope'); if (ss) state.soundScope = ss;
  } catch (_) {}
  // Wording ('all messages' / '@mentions only') and the mention-first vs
  // all-first default are matched to the notify-scope select so the two read as
  // siblings; the title spells out that they're independent controls.
  const soundScopeSel = prefSelect(
    [['all', 'all messages'], ['mention', '@mentions only']], state.soundScope);
  soundScopeSel.title = 'Chime scope — independent of desktop notifications';
  soundScopeSel.addEventListener('change', () => {
    state.soundScope = soundScopeSel.value;
    try { localStorage.setItem('trio.soundScope', state.soundScope); } catch (_) {}
  });
  const soundScopeRow = addSettingRow('Chime for', soundScopeSel);

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

  // Notification preference dropdowns (reuse prefSelect defined above).
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
    if (soundScopeRow) soundScopeRow.hidden = !state.soundEnabled;
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
  // ── Unread divider ──
  // Count / locate unread (id > lastSeenId), skipping filtered/DM-hidden nodes
  // so the divider + "new" bar stay in sync with what's actually shown.
  function isHiddenMsg(dom) {
    return dom.classList.contains('filtered-out') || dom.classList.contains('dm-hidden');
  }
  // You cannot have unread your own message. Sending while scrolled up used to
  // raise a "new messages" divider above your own post and add it to the
  // counter, because unread was decided purely by id > lastSeenId.
  //
  // Skipped at the point of COUNTING rather than by advancing lastSeenId past
  // it. The watermark is a single high-water mark: moving it over your own
  // message would also mark every earlier message read, so a peer's message
  // that arrived while you were scrolled up would vanish from the divider
  // merely because you replied to something else.
  function isOwnMsg(dom) {
    return !!state.operator.id && dom.dataset.sender === state.operator.id;
  }
  function firstVisibleUnreadDom() {
    for (const id of [...state.messageDomById.keys()].sort((a, b) => a - b)) {
      if (id <= state.lastSeenId) continue;
      const dom = state.messageDomById.get(id);
      if (dom && !isHiddenMsg(dom) && !isOwnMsg(dom)) return dom;
    }
    return null;
  }
  function unreadCountVisible() {
    let n = 0;
    for (const [id, dom] of state.messageDomById) {
      if (id > state.lastSeenId && !isHiddenMsg(dom) && !isOwnMsg(dom)) n++;
    }
    return n;
  }
  // Draw a "new messages" line before the first *visible* unread message.
  function refreshUnreadDivider() {
    const old = document.getElementById('unread-divider');
    if (old) old.remove();
    if (state.lastSeenId) {
      const dom = firstVisibleUnreadDom();
      if (dom) {
        const bar = document.createElement('div');
        bar.id = 'unread-divider';
        bar.className = 'unread-divider';
        bar.textContent = 'new messages';
        chat.insertBefore(bar, dom);
      }
    }
    updateNewBar();
  }
  // Establish the read watermark once, when the history burst settles — the
  // only place that works when the channel was opened in a background tab,
  // which landing mode makes the normal way in.
  //
  // Everything already on screen counts as seen, so you arrive caught up
  // rather than staring at a divider above the entire history. If the server
  // has a last_read for this operator it wins, but note that nth_web.py does
  // not currently write members.last_read for web operators (ensure_operator_row
  // inserts 0 and only last_seen is updated), so in practice this resolves to
  // "newest" today. The branch is here so that persisting a web operator's
  // read position starts working without touching this function.
  function seedBaseline() {
    if (state.lastSeenId) return;
    // reduce(), not Math.max(...spread) — a long channel would exceed the
    // argument limit and throw RangeError.
    const newest = [...state.messageDomById.keys()]
      .reduce((a, b) => (b > a ? b : a), 0);
    const me = state.members.get(state.operator.id);
    const serverLastRead = me ? (me.last_read || 0) : 0;
    state.lastSeenId = serverLastRead > 0 ? Math.min(serverLastRead, newest) : newest;
    refreshUnreadDivider();
  }

  // The user caught up — advance the watermark over the messages they could
  // actually have read, and clear the divider.
  //
  // lastSeenId is a single high-water mark, so it must never jump OVER an
  // unread message the user has not seen. Two kinds of hidden message need
  // opposite treatment:
  //   • filtered-out — the user's own filter is hiding it temporarily. Stop
  //     here. Advancing past it would mark it read because they searched for
  //     something else, and clearing the filter would silently lose it.
  //   • dm-hidden — structurally not part of this view at all. Skip it; if it
  //     blocked the walk the watermark could never advance past it again.
  function markCaughtUp() {
    let mark = state.lastSeenId;
    for (const id of [...state.messageDomById.keys()].sort((a, b) => a - b)) {
      if (id <= state.lastSeenId) continue;
      const dom = state.messageDomById.get(id);
      if (dom.classList.contains('filtered-out')) break;
      mark = id;
    }
    state.lastSeenId = mark;
    if (!unreadCountVisible()) {
      const bar = document.getElementById('unread-divider');
      if (bar) bar.remove();
    }
    updateNewBar();
  }
  // Top "N new messages" bar — the conventional jump-to-first-unread affordance.
  // Shown whenever an unread divider exists; clicking scrolls up to it.
  function updateNewBar() {
    if (!newBar) return;
    if (!document.getElementById('unread-divider')) { newBar.classList.remove('show'); return; }
    // "N new messages below" is meaningless when you are already at the bottom
    // looking at them. This happens two ways: the jump-to-unread clamps here
    // when the unread block is shorter than the viewport (and then there is no
    // scroll left to attribute, so nothing marks), and a filter can leave the
    // walk unable to advance past a hidden message beneath a visible one.
    // Hiding the claim is honest in both; the watermark is deliberately
    // untouched, so nothing is marked read on the user's behalf.
    if (chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80) {
      newBar.classList.remove('show');
      return;
    }
    const n = unreadCountVisible();
    newBar.textContent = '↓ ' + n + ' new message' + (n === 1 ? '' : 's');
    newBar.classList.add('show');
  }

  function updateJumpButton() {
    const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
    if (atBottom) {
      state.jumpUnread = 0;
      jumpBtn.classList.remove('show');
      jumpCount.style.display = 'none';
      // Only a scroll the USER performed means "I have read to here". A
      // scroll event alone does not say who caused it, and the page issues
      // several of its own (the post-burst settle, the jump-to-unread), so
      // attribute the scroll to a recent real gesture instead of racing it
      // against a timer.
      if (!document.hidden && scrollIsUsers()) markCaughtUp();
      return;
    }
    jumpBtn.classList.add('show');
    if (state.jumpUnread > 0) {
      jumpCount.style.display = '';
      jumpCount.textContent = state.jumpUnread;
    } else {
      jumpCount.style.display = 'none';
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
  // A scroll is "the user's" when a real input gesture on the scroller
  // preceded it. Programmatic scrolls (settle, jump-to-unread) have none, so
  // they can never mark messages read. Bound to the scroller, not the
  // document, so clicking the "new messages" bar is not mistaken for intent.
  function noteIntent() { state.userIntentAt = Date.now(); }
  // True when a scroll happening right now is attributable to the user.
  function scrollIsUsers() { return Date.now() - state.userIntentAt < USER_INTENT_MS; }
  // A scroll the PAGE issues is never the user's, however recently they moved.
  // Called immediately before every programmatic scrollTop assignment: without
  // it a wheel in the preceding USER_INTENT_MS donates its attribution to the
  // animation, and sustainIntent then carries that donation to the bottom.
  function disownScroll() { state.userIntentAt = 0; }
  // Keep an already-attributed scroll attributed while it is still moving.
  // Cannot bootstrap: an unattributed scroll starts stale and stays stale.
  function sustainIntent() { if (scrollIsUsers()) noteIntent(); }
  for (const ev of ['wheel', 'touchstart', 'touchmove', 'pointerdown', 'mousedown']) {
    chat.addEventListener(ev, noteIntent, { passive: true });
  }
  document.addEventListener('keydown', (e) => {
    // Only keys that could plausibly have scrolled the chat. Typing in the
    // composer must not count: boot focuses #input, so a space typed while a
    // programmatic scroll is still gliding would hand it the user's
    // attribution and let it mark the unread read.
    if (e.target && e.target.closest &&
        e.target.closest('input, textarea, select, [contenteditable]')) return;
    if (['PageDown', 'PageUp', 'End', 'Home', 'ArrowDown', 'ArrowUp', ' '].includes(e.key)) noteIntent();
  }, { passive: true });
  chat.addEventListener('scroll', () => {
    // A scroll that is ALREADY the user's keeps its attribution for as long as
    // it keeps moving — iOS momentum routinely runs 1-3s past touchend, and a
    // long smooth scroll can outlast USER_INTENT_MS on its own. This cannot
    // bootstrap a programmatic scroll into attribution: that one starts stale,
    // so the condition is false on its very first frame and stays false.
    sustainIntent();
    updateJumpButton();
    scheduleHereUpdate();
  });
  jumpBtn.addEventListener('click', () => {
    chat.scrollTop = chat.scrollHeight;
    state.jumpUnread = 0;
    if (!document.hidden) markCaughtUp();
    updateJumpButton();
  });
  // Top bar: scroll UP to the first unread message (the divider). Does not mark
  // caught-up — you're going TO the unread, not past it.
  newBar.addEventListener('click', () => {
    const dom = firstVisibleUnreadDom();
    if (!dom) return;
    // #chat is scroll-behavior: smooth, so this starts an animation lasting
    // well over a second on a long channel, and the browser clamps it to the
    // bottom whenever the unread block is shorter than one viewport. Neither
    // is a scroll the user performed, so neither may count as catching up —
    // see USER_INTENT_MS.
    disownScroll();
    chat.scrollTop = Math.max(0, dom.offsetTop - 8);
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
      // Returning to the tab: if already at the bottom, they've caught up;
      // otherwise surface the "new messages" divider for what arrived while away.
      const atBottom = chat.scrollHeight - chat.clientHeight - chat.scrollTop < 80;
      if (atBottom) markCaughtUp();
      else refreshUnreadDivider();
      updateJumpButton();
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
      // A channel with no history primes zero messages, so appendMessage never
      // fires and nothing would ever clear initialLoad — the first live message
      // would arrive un-chimed. Arm the settle from the connection itself.
      if (state.initialLoad) scheduleInitialSettle();
      hConn.textContent = '● connected';
      hConn.classList.remove('bad');
      hConn.classList.add('ok');
    };
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.type === 'message') appendMessage(payload);
        else if (payload.type === 'roster') renderRoster(payload.members);
        // 'context' frames carry the per-host session list. The channel page
        // renders context per-member (roster badge + stats drill-down); the
        // standalone ring sidebar was removed in b771656, so nothing here
        // consumes them. The landing page still renders rings from its own
        // /api/landing poll.
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
    // The server refuses a cull from anything but a local shell or a
    // Tailscale-verified peer. Mirror that here so an identity the server
    // would reject never sees the control at all, rather than being walked
    // through a confirm dialog into a 403.
    CAN_CULL = (op && (op.source === 'loopback' || op.source === 'tailscale'));
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
      hChannel.textContent = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      state.originalTitle = (DM_MODE ? 'DM — trio#' : 'trio#') + meta.channel;
      if (DM_MODE) document.body.classList.add('dm-mode');
      updateTitle();
      if (meta.operator && meta.operator.pending) {
        // Untrusted connection — need a name before anything else
        showGuestModal();
        return;
      }
      bootAttempts = 0;
      clearFatal();
      applyOperator(meta.operator);
      afterBoot();
    } catch (e) {
      // Retry like the SSE path does. Without this a single blip while the
      // hub restarts left a permanently dead page — and the message went
      // into header .meta, which mobile CSS hides, so on a phone the whole
      // app was simply blank with no explanation.
      bootAttempts++;
      showFatal('Could not reach the hub (' + e.message + '). Retrying…');
      if (bootAttempts < 20) setTimeout(boot, Math.min(2000 * bootAttempts, 15000));
      else showFatal('Could not reach the hub: ' + e.message +
                     '. Check it is running, then reload.');
    }
  }
  let bootAttempts = 0;
  function showFatal(msg) {
    let el = document.getElementById('fatal-banner');
    if (!el) {
      el = document.createElement('div');
      el.id = 'fatal-banner';
      document.body.prepend(el);
    }
    el.textContent = msg;
    el.style.display = 'block';
  }
  function clearFatal() {
    const el = document.getElementById('fatal-banner');
    if (el) el.style.display = 'none';
  }
  // ── Full-history search (queries the server DB, not just loaded messages) ──
  const btnSearch = document.getElementById('btn-search');
  const searchPanel = document.getElementById('search-panel');
  const searchInput = document.getElementById('search-input');
  const searchClose = document.getElementById('search-close');
  const searchStatus = document.getElementById('search-status');
  const searchResults = document.getElementById('search-results');
  let searchTimer = 0, searchSeq = 0;

  function openSearch() {
    searchPanel.hidden = false;
    if (state.filter && !searchInput.value) searchInput.value = state.filter;
    searchInput.focus(); searchInput.select();
    if (searchInput.value.trim().length >= 2) runSearch();
  }
  function closeSearch() { searchPanel.hidden = true; }
  async function runSearch() {
    const q = searchInput.value.trim();
    searchResults.innerHTML = '';
    if (q.length < 2) { searchStatus.textContent = 'type at least 2 characters'; return; }
    searchStatus.textContent = 'searching…';
    const seq = ++searchSeq;
    try {
      // API_QS carries ?channel=<code> in landing mode and is empty in
      // single-channel mode, so pick the right query-string joiner.
      const r = await fetch('/api/search' + (API_QS ? API_QS + '&' : '?')
                            + 'q=' + encodeURIComponent(q));
      const d = await r.json().catch(() => ({}));
      if (seq !== searchSeq) return;   // a newer query superseded this one
      if (!r.ok || !d.ok) { searchStatus.textContent = 'search failed: ' + (d.error || r.status); return; }
      renderSearchResults(d.results || []);
    } catch (e) {
      if (seq === searchSeq) searchStatus.textContent = 'search failed: ' + e.message;
    }
  }
  function renderSearchResults(results) {
    const capped = results.length >= 200;
    searchStatus.textContent = results.length
      ? (results.length + (capped ? '+' : '') + ' match' + (results.length === 1 ? '' : 'es')
         + ' — newest first')
      : 'no matches';
    const frag = document.createDocumentFragment();
    for (const m of results) {
      const hit = document.createElement('div');
      hit.className = 'search-hit';
      const meta = document.createElement('div');
      meta.className = 'sh-meta';
      const author = document.createElement('span');
      author.className = 'sh-author';
      author.textContent = m.member_name;
      author.style.color = colorFor(m.member_id);
      meta.appendChild(author);
      meta.appendChild(document.createTextNode('  ·  ' + formatTime(m.created_at)));
      const body = document.createElement('div');
      body.className = 'sh-body';
      body.textContent = humanizeIdSigils(m.content || '');
      hit.appendChild(meta);
      hit.appendChild(body);
      // If the match is in the loaded timeline, jump + flash it; otherwise the
      // panel row is the result (it's outside the in-memory window).
      hit.addEventListener('click', () => {
        const dom = state.messageDomById.get(m.id);
        if (dom) {
          closeSearch();
          dom.scrollIntoView({ block: 'center' });
          dom.classList.add('flash');
          setTimeout(() => dom.classList.remove('flash'), 1500);
        }
      });
      frag.appendChild(hit);
    }
    searchResults.appendChild(frag);
  }
  btnSearch.addEventListener('click', openSearch);
  searchClose.addEventListener('click', closeSearch);
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 250);
  });
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeSearch(); }
    else if (e.key === 'Enter') { clearTimeout(searchTimer); runSearch(); }
  });

  function afterBoot() {
    // API_QS is only set in landing mode. In single-channel mode "/" IS this
    // page, so the home link would just reload and its tooltip would be a lie.
    if (!API_QS) {
      const bh = document.getElementById('btn-home');
      if (bh) bh.style.display = 'none';
    }
    connect();
    input.focus();
    updatePreview();
    updateChanStats();
  }

  // __TRIO_TEST_HOOK_START__
  // Test hook: when this script is loaded under the Node DOM harness
  // (tests/dom-harness.js), expose the internal render/parse helpers for unit
  // testing. This whole block (marker to marker) is STRIPPED from the served
  // browser bundle at render time (see _strip_test_hook in the INDEX_HTML
  // substitution below), so the internal state reference never ships to a
  // browser at all. The runtime guard is a second line of defense in case the
  // strip ever fails: the test global is only pre-seeded by the harness
  // sandbox, never in production. Placed before boot() so the hooks are
  // available even if boot() throws against the harness's minimal DOM.
  if (typeof globalThis !== 'undefined' && globalThis.__TRIO_TEST__) {
    globalThis.__TRIO_TEST__ = {
      state,
      renderMarkdown, escapeHtml, isSystemContent, humanizeIdSigils,
      formatTime,
      collectMentionMatches, mentionMemberForToken,
      decorateInlineMentions, composerMentionHtml,
      chimeScopeAllows, shouldChime,
      detectFilePathCandidates, linkifyValidatedPaths, decorateFilePaths,
    };
  }
  // __TRIO_TEST_HOOK_END__

  boot();
})();
</script>
</body>
</html>
"""

# Strip the test-only hook block (between the sentinel markers) from the served
# browser bundle so the internal `state` reference is never exposed on a global
# in production. The Node DOM harness reads the raw source file directly, so it
# still sees the block. If the markers are ever renamed the block simply stays
# in — no worse than the runtime __TRIO_TEST__ guard that also protects it.
def _strip_test_hook(html: str) -> str:
    return re.sub(
        r"\n\s*// __TRIO_TEST_HOOK_START__.*?// __TRIO_TEST_HOOK_END__",
        "", html, flags=re.DOTALL)


# One-shot substitution at import time — inject the emoji list into the JS
# so server-side animal_for() and client-side animalFor() stay in sync, and
# drop the test hook from the shipped bundle.
INDEX_HTML = _strip_test_hook(
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
    global ATTACH_DIR
    ATTACH_DIR = attach_dir_for(db_path)
    if not db_path.exists():
        sys.stderr.write(
            f"nth.db not found at {db_path}\n"
            f"It's created the first time a session runs /trio. Start a Claude\n"
            f"Code session, run /trio, then retry — or pass --db PATH.\n")
        return 1

    # Typo'd channel codes used to start a normal-looking server that stayed
    # empty forever. Landing mode already validates; single-channel didn't.
    if args.channel:
        try:
            _probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = _probe.execute(
                    "SELECT 1 FROM channels WHERE code = ?", (args.channel,)
                ).fetchone()
                if row is None:
                    known = [r[0] for r in _probe.execute(
                        "SELECT code FROM channels ORDER BY code LIMIT 20")]
                    sys.stderr.write(f"no such channel: {args.channel}\n")
                    if known:
                        sys.stderr.write("channels in this db: "
                                         + ", ".join(known) + "\n")
                    else:
                        sys.stderr.write("this db has no channels yet\n")
                    return 1
            finally:
                _probe.close()
        except sqlite3.Error as e:
            sys.stderr.write(f"could not read {db_path}: {e}\n")
            return 1

    host = args.host
    if host is None:
        host = "0.0.0.0" if args.tailnet else "127.0.0.1"

    # Single-channel mode spins up its one event hub before serving.
    # One sweep at startup so a long-running install reclaims whatever leaked
    # while it was down, without waiting for someone to upload.
    def _startup_sweep() -> None:
        try:
            _gc = sweep_attachments(db_path, force=True)
            if any(_gc.get(k) for k in ("abandoned", "dead_channel", "orphan_files")):
                print(f"attachments: reclaimed {_gc}", flush=True)
        except Exception:
            pass

    # On a daemon thread: this ran inline before the socket was bound, so on a
    # large install the dashboard, every channel and every API route were
    # unreachable for the duration (measured ~1.2s at 150k attachments, and it
    # grows with the install). Nothing downstream depends on its result.
    threading.Thread(target=_startup_sweep, name="attach-gc", daemon=True).start()

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
