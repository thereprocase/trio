#!/usr/bin/env python3
# nth_spoke_monitor.py - spoke-side event monitor for nth / quartet channels.
#
# Companion to nth_monitor.py. nth_monitor.py reads ~/.claude/nth/nth.db
# directly (hub-only). This script runs on a SPOKE session, where the
# channel DB is somewhere else, and reaches the hub via MCP-over-SSE
# (the same URL the spoke's MCP client uses, e.g. http://hub:8000/sse).
#
# Emits the SAME JSON event shapes as nth_monitor.py so the parent Claude
# treats hub and spoke monitors interchangeably:
#
#   {"event": "new_messages",   ...}
#   {"event": "cadence",        "gap_seconds": N, "claimed_tasks": K}
#   {"event": "keepalive",      "gap_seconds": N, ...}
#   {"event": "channel_ended",  "ended_by": "..."}
#   {"event": "channel_gone"}
#   {"event": "culled",         "member_id": "...", "channel": "..."}
#   {"event": "session_revoked", "member_id": "...", "channel": "...",
#    "reason": "refused", "msg": "..."}
#   {"event": "error",          "msg": "..."}
#
# channel_ended, channel_gone, culled and session_revoked are terminal: the
# monitor exits after emitting one. nth_listener.classify_poll decides which.
#
# Filters: --filter all|about|at (same semantics as nth_monitor; bangs
# always wake regardless of filter). Legacy --mention-filter == --filter about.
#
# Long-polls quartet_poll(wait_seconds=POLL_WAIT_SEC) so it is nearly
# idle network-wise: one open SSE connection + one HTTP POST every 15 s
# when nothing arrives, instant on arrival.
#
# IMPORTANT: this monitor does NOT advance the parent's read watermark.
# It passes auto_ack=false and tracks dedup by local high-water mark.
# Parent Claude calls quartet_ack on its own session.
#
# Pure stdlib. Works on Windows (py launcher) and Linux/macOS. Run it from the
# installed server directory: it imports nth_sse_client and nth_listener.
"""Spoke-side nth/quartet monitor. Run via Claude Code's Monitor tool:

    Monitor(
        command="py -3 .../nth_spoke_monitor.py <channel> <member_id> --filter about",
        description="<channel> events (spoke)",
        persistent=True,
        timeout_ms=3600000,
    )

Useful env / flags:
    --url               default http://localhost:8000/sse
                        or set NTH_QWEB_URL
    --filter MODE       all | about | at  (default all)
    --debug             stderr trace of SSE + JSON-RPC traffic
    --poll-wait SEC     long-poll seconds passed to quartet_poll (default 15)
    --status-interval S compute cadence/keepalive every N seconds (default 30)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# The hub client lives in nth_sse_client; it is re-exported here because services
# and tests imported it from this script before it moved.
from nth_sse_client import MCPSSEClient, RECONNECT_BACKOFF, SSE_READ_TIMEOUT  # noqa: E402,F401
from nth_listener import classify_poll  # noqa: E402

# --- Tunables (match nth_monitor.py where applicable) ---------------------
DEFAULT_URL          = "http://localhost:8000/sse"
DEFAULT_POLL_WAIT    = 15            # quartet_poll long-poll window (server cap 30)
DEFAULT_STATUS_EVERY = 30            # how often to recompute cadence/keepalive
CADENCE_THRESHOLD    = 600           # 10 min
KEEPALIVE_THRESHOLD  = 55 * 60       # 55 min
KEEPALIVE_GIVEUP    = 7 * 3600      # 7 h
REFUSED_MSG = ("The hub refused this membership: its session token was revoked "
               "(a cull, a reclaim and your own reconnect all revoke it). This "
               "monitor has stopped. If you did not just reconnect, tell the user; "
               "never reconnect or reclaim on your own.")

# Sleeping-mode keywords: import the canonical set when the script runs from
# the installed server dir (its normal home), fall back to a verbatim copy when
# nth_constants cannot be imported. The script itself needs its siblings
# nth_sse_client and nth_listener. The original inline set here had drifted much
# broader than canon ("done", "out", "off"...) — broad matching wrongly idles
# the monitor for statuses like "rollout done, watching logs".
try:
    from nth_constants import SLEEPING_KEYWORDS, project_context
except ImportError:
    SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")
    project_context = None

# Own-session context file, written by the operator's statusline publisher.
if sys.platform == "win32":
    _CTX_DIR = os.path.join(os.environ.get("LOCALAPPDATA",
                                           os.path.expanduser("~")), "claude-context")
else:
    _CTX_DIR = os.path.join(os.environ.get("XDG_STATE_HOME",
                                           os.path.expanduser("~/.local/state")),
                            "claude-context")


def _discover_session_id():
    """Find our Claude Code session ID without requiring an env var.
    Walk the process tree to find the Claude Code parent, then read its
    session file (~/.claude/sessions/<pid>.json) for the sessionId."""
    explicit = (os.environ.get("CLAUDE_CODE_SESSION_ID")
                or os.environ.get("CLAUDE_SESSION_ID", ""))
    if explicit:
        return explicit
    sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    try:
        pid = os.getpid()
        for _ in range(10):
            pid = _get_ppid(pid)
            if pid <= 1:
                break
            sf = os.path.join(sessions_dir, f"{pid}.json")
            if os.path.isfile(sf):
                with open(sf, encoding="utf-8") as f:
                    return json.loads(f.read()).get("sessionId", "")
    except Exception:
        pass
    return ""


def _get_ppid(pid):
    """Cross-platform parent PID lookup (Linux, macOS, Windows)."""
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as w
        TH32CS_SNAPPROCESS = 0x2
        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_char * 260)]
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        pe = PE32(); pe.dwSize = ctypes.sizeof(PE32)
        try:
            if k32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    if pe.th32ProcessID == pid:
                        return pe.th32ParentProcessID
                    if not k32.Process32Next(snap, ctypes.byref(pe)):
                        break
        finally:
            k32.CloseHandle(snap)
        return 0
    # Linux: fast /proc read. macOS: falls through to ps.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            # Format: pid (comm) state ppid ...
            # comm is unsanitised and may itself contain ')' or spaces, so
            # split on the LAST ')' — splitting on the first one misparses
            # any process whose name contains a paren.
            return int(f.read().rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    # macOS (and any POSIX without /proc): ps -o ppid= -p PID
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            timeout=2, stderr=subprocess.DEVNULL,
        )
        return int(out.decode().strip())
    except Exception:
        return 0


_OWN_SESSION_ID = _discover_session_id()


def read_own_context():
    """This session's statusline snapshot as a JSON string, or None.
    Stale (>120s) or unreadable files are skipped silently."""
    if not _OWN_SESSION_ID:
        return None
    path = os.path.join(_CTX_DIR, _OWN_SESSION_ID + ".json")
    try:
        if time.time() - os.stat(path).st_mtime > 120:
            return None
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        parsed = json.loads(raw)  # validate before shipping
        # Project here as well as server-side: the raw statusline snapshot
        # carries transcript paths, cwds and cumulative spend, and there is
        # no reason for any of that to cross the wire in the first place.
        if project_context is not None:
            projected = project_context(parsed)
            if projected is None:
                return None
            return json.dumps(projected)
        return raw
    except (OSError, ValueError, TypeError):
        return None


FILTER_MODES = ("all", "about", "at")
LEGACY_FILTER_MAP = {
    "at+broadcast":       "about",
    "at+pound":           "about",
    "at+pound+broadcast": "about",
    "pound":              "about",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def seconds_since(iso_ts):
    if not iso_ts:
        return float("inf")
    try:
        s = iso_ts.replace("Z", "+00:00") if isinstance(iso_ts, str) else iso_ts
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def gap_for_emit(gap):
    """JSON-safe gap: None when unknown (inf). round(inf) raises OverflowError
    — the exact crash class fixed in nth_monitor.py (2026-08-11)."""
    return None if gap == float("inf") else round(gap)


def is_sleeping(status_text):
    if not status_text:
        return False
    lower = status_text.lower()
    return any(kw in lower for kw in SLEEPING_KEYWORDS)


def emit(event_dict):
    print(json.dumps(event_dict, separators=(",", ":")), flush=True)


def parse_id_list(raw):
    """Sigil columns come back as JSON strings, native lists, or None."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, str)]
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []
    return []


def should_emit_summary(poll_response, filter_mode):
    """Spoke variant of nth_monitor.should_wake. The SSE poll response
    surfaces TOP-LEVEL boolean flags rather than per-message sigil arrays,
    so we evaluate at the response level instead of per-message:
      * has_mentions == "any new message in this batch @-pings me"
      * has_refs     == "any new message in this batch #-references me"
      * has_bangs    == "any new message in this batch bangs me / broadcasts !all"
    For "at" mode we ALSO pass mentions_only=True to the server so the
    response only contains messages we'd actually wake on.
    """
    has_at   = bool(poll_response.get("has_mentions"))
    has_pd   = bool(poll_response.get("has_refs"))
    has_bang = bool(poll_response.get("has_bangs"))
    if has_bang:
        return True, "bang"
    if filter_mode == "all":
        # Any new_messages wake on `all`; flag the most specific kind.
        kind = "at" if has_at else ("pound" if has_pd else "ambient")
        return True, kind
    if filter_mode == "about":
        if has_at:   return True, "at"
        if has_pd:   return True, "pound"
        return False, None
    if filter_mode == "at":
        if has_at:   return True, "at"
        return False, None
    return True, "ambient"


# --- Monitor loop ---------------------------------------------------------
def monitor(client, channel, member_id, filter_mode, session_token,
            poll_wait_seconds, status_interval):
    local_hwm = 0
    last_status_mono = 0.0
    cached_mode = "active"          # for new_messages.mode
    cadence_fired = False
    keepalive_fired = False
    consecutive_poll_errors = 0
    consecutive_status_errors = 0

    while True:
        # ---------------- LONG-POLL FOR NEW MESSAGES ----------------
        poll_started = time.monotonic()
        prev_hwm = local_hwm
        try:
            args = {
                "channel": channel,
                "member_id": member_id,
                "wait_seconds": poll_wait_seconds,
                "auto_ack": False,        # never touch parent's watermark
                # For "at" filter, let the server drop pure-cross-talk before
                # it hits the wire (returns @me + broadcasts only). For other
                # modes we want every message so client-side filter has data.
                "mentions_only": (filter_mode == "at"),
            }
            args["monitor_heartbeat"] = True
            args["monitor_filter"] = filter_mode
            own_ctx = read_own_context()
            if own_ctx:
                args["monitor_context"] = own_ctx
            if session_token:
                args["session_token"] = session_token
            poll = client.call_tool("quartet_poll", args,
                                    timeout=poll_wait_seconds + 30)
            consecutive_poll_errors = 0
        except Exception as e:
            consecutive_poll_errors += 1
            # First failure stays silent: the SSE client self-heals on retry
            # (hub restarts, transient EOFs) and every error event wakes the
            # subscribing agent. Only sustained failure is worth a turn.
            if consecutive_poll_errors >= 2:
                emit({"event": "error",
                      "msg": f"poll failed ({consecutive_poll_errors}): {e}"})
            if "Not connected" in str(e) and consecutive_poll_errors >= 2:
                # The reader thread is wedged (dead socket, no EOF). Kick it:
                # closing the socket forces its reconnect path to run.
                client.force_reconnect()
            time.sleep(min(2 * consecutive_poll_errors, 30))
            continue

        # Every terminal outcome ends this monitor with one event that wakes the
        # agent (nth_watch --once passes exactly these through). An INVALID reply
        # is transient and retried silently, as before.
        outcome = classify_poll(poll)
        if outcome == "ended":
            emit({"event": "channel_ended",
                  "ended_by": poll.get("ended_by")})
            return
        if outcome == "gone":
            emit({"event": "channel_gone"})
            return
        if outcome == "culled":
            emit({"event": "culled", "member_id": member_id, "channel": channel})
            return
        if outcome == "refused":
            # A cull revokes the member's sessions, so a poll WITH a token sees a
            # cull as a refused token; so do a reclaim and the agent's own reconnect.
            emit({"event": "session_revoked", "member_id": member_id,
                  "channel": channel, "reason": "refused", "msg": REFUSED_MSG})
            return
        if outcome == "ok":
            ev = poll.get("event")
            if ev == "new_messages":
                messages = poll.get("messages", []) or []
                # Dedup against local_hwm — server has no spoke-side watermark
                # without a session_token, so it tends to re-return the same
                # backlog on every poll. We're the source of truth on what we
                # already emitted.
                new_msgs = [m for m in messages
                            if (m.get("id") or 0) > local_hwm]
                if messages:
                    local_hwm = max(local_hwm,
                                    max((m.get("id") or 0) for m in messages))
                if new_msgs:
                    wake, _kind = should_emit_summary(poll, filter_mode)
                    if wake:
                        from_names = []
                        seen = set()
                        for m in new_msgs:
                            n = m.get("from") or m.get("member_name") or ""
                            if n and n not in seen:
                                seen.add(n)
                                from_names.append(n)
                        latest = new_msgs[-1].get("content") or ""
                        preview = latest[:80] + ("…" if len(latest) > 80 else "")
                        emit({
                            "event": "new_messages",
                            "mode": cached_mode,
                            "message_ids": [m.get("id") for m in new_msgs],
                            "count": len(new_msgs),
                            "has_bangs": bool(poll.get("has_bangs")),
                            "has_mentions": bool(poll.get("has_mentions")),
                            "has_refs": bool(poll.get("has_refs")),
                            "from_names": from_names,
                            "preview": preview,
                            "filter": filter_mode,
                        })
            # ev == "no_new" or anything else: no emit

        # Hot-spin guard. Without a session_token the server has no spoke-
        # side watermark to advance, so it will return the same backlog
        # the instant we ask again — long-poll never actually long-polls.
        # If the poll came back fast AND we didn't see anything new
        # locally, sleep before hitting it again.
        poll_elapsed = time.monotonic() - poll_started
        if poll_elapsed < 1.0 and local_hwm == prev_hwm:
            time.sleep(min(poll_wait_seconds, 2.0))

        # ---------------- STATUS CHECK (cadence / keepalive) ----------------
        now = time.monotonic()
        if now - last_status_mono < status_interval:
            continue
        last_status_mono = now
        try:
            status = client.call_tool("quartet_status", {"channel": channel},
                                      timeout=20)
            consecutive_status_errors = 0
        except Exception as e:
            consecutive_status_errors += 1
            if consecutive_status_errors >= 2:
                emit({"event": "error",
                      "msg": f"status failed ({consecutive_status_errors}): {e}"})
            continue

        if not isinstance(status, dict):
            continue
        if status.get("error") == "channel_not_found":
            emit({"event": "channel_gone"})
            return
        if status.get("status") == "ended":
            emit({"event": "channel_ended", "ended_by": status.get("ended_by")})
            return

        members = status.get("members") or []
        me = None
        for m in members:
            if m.get("id") == member_id or m.get("member_id") == member_id:
                me = m
                break
        if me is None:
            emit({"event": "error", "msg": "Member not found in channel."})
            time.sleep(5)
            continue

        sleeping = is_sleeping(me.get("status_text"))
        cached_mode = "idle" if sleeping else "active"

        # --- own_gap: time since this member last posted ---
        own_last = (me.get("last_post")
                    or me.get("last_message_at")
                    or me.get("last_seen"))
        own_gap = seconds_since(own_last)

        # Fallback: scan recent_messages for our own most recent
        recent = status.get("recent_messages") or status.get("messages") or []
        if own_gap == float("inf"):
            for m in reversed(recent):
                if (m.get("member_id") == member_id
                        or m.get("from_id") == member_id):
                    own_gap = seconds_since(m.get("at") or m.get("created_at"))
                    break

        # --- claimed tasks ---
        claimed = 0
        for t in (status.get("tasks") or []):
            if (t.get("claimed_by") == member_id
                    and t.get("status") == "claimed"):
                claimed += 1

        # Cadence
        if not sleeping and claimed > 0:
            if own_gap > CADENCE_THRESHOLD and not cadence_fired:
                emit({"event": "cadence",
                      "gap_seconds": gap_for_emit(own_gap),
                      "claimed_tasks": claimed})
                cadence_fired = True
            elif own_gap < CADENCE_THRESHOLD:
                cadence_fired = False
        else:
            cadence_fired = False

        # --- engaged_gap: last @me / #me / !me from a peer ---
        engaged_gap = float("inf")
        for m in recent:
            origin = m.get("member_id") or m.get("from_id")
            if origin == member_id:
                continue
            if (member_id in parse_id_list(m.get("mentions"))
                    or member_id in parse_id_list(m.get("refs"))
                    or member_id in parse_id_list(m.get("bangs"))):
                g = seconds_since(m.get("at") or m.get("created_at"))
                if g < engaged_gap:
                    engaged_gap = g

        needed_gap = min(own_gap, engaged_gap)
        stale_in_channel = needed_gap > KEEPALIVE_GIVEUP

        if (own_gap > KEEPALIVE_THRESHOLD
                and not stale_in_channel
                and not keepalive_fired):
            emit({
                "event": "keepalive",
                "gap_seconds": gap_for_emit(own_gap),
                "threshold_seconds": KEEPALIVE_THRESHOLD,
                "engaged_gap_seconds": gap_for_emit(engaged_gap),
            })
            keepalive_fired = True
        elif own_gap < KEEPALIVE_THRESHOLD:
            keepalive_fired = False


def parse_filter_arg(value):
    if not value:
        return "all"
    if value in FILTER_MODES:
        return value
    if value in LEGACY_FILTER_MAP:
        return LEGACY_FILTER_MAP[value]
    raise ValueError(f"unknown filter mode '{value}'. valid: {', '.join(FILTER_MODES)}")


def main():
    ap = argparse.ArgumentParser(add_help=True,
        description="Spoke-side nth/quartet event monitor (MCP-over-SSE).")
    ap.add_argument("channel")
    ap.add_argument("member_id")
    ap.add_argument("--filter", default="all",
                    help="all | about | at (default all)")
    ap.add_argument("--mention-filter", action="store_true",
                    help="legacy alias for --filter about")
    ap.add_argument("--session-token", default=os.environ.get("NTH_SESSION_TOKEN", ""),
                    help="optional bearer token for the spoke's session "
                         "(passed straight through to quartet_poll)")
    ap.add_argument("--url", default=os.environ.get("NTH_QWEB_URL", DEFAULT_URL),
                    help=f"hub SSE URL (default {DEFAULT_URL})")
    ap.add_argument("--poll-wait", type=int, default=DEFAULT_POLL_WAIT,
                    help=f"long-poll seconds (default {DEFAULT_POLL_WAIT})")
    ap.add_argument("--status-interval", type=int, default=DEFAULT_STATUS_EVERY,
                    help=f"cadence/keepalive cadence (default {DEFAULT_STATUS_EVERY}s)")
    ap.add_argument("--claude-session", default="",
                    help="Claude session id override for the statusline relay "
                         "(autodetected from CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--debug", action="store_true",
                    help="stderr trace of SSE + JSON-RPC frames")
    args = ap.parse_args()

    if args.mention_filter:
        mode = "about"
    else:
        try:
            mode = parse_filter_arg(args.filter)
        except ValueError as e:
            emit({"event": "error", "msg": str(e)})
            sys.exit(1)

    sys.stderr.write(
        f"[nth-spoke-monitor] channel={args.channel} member={args.member_id} "
        f"filter={mode} url={args.url}\n"
    )
    sys.stderr.flush()

    if args.claude_session:
        global _OWN_SESSION_ID
        _OWN_SESSION_ID = args.claude_session
    client = MCPSSEClient(args.url, debug=args.debug)
    try:
        client.connect()
    except Exception as e:
        emit({"event": "error", "msg": f"connect failed: {e}"})
        sys.exit(2)

    try:
        monitor(client, args.channel, args.member_id, mode,
                args.session_token, args.poll_wait, args.status_interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
