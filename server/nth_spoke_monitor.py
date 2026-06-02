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
#   {"event": "error",          "msg": "..."}
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
# Pure stdlib. Works on Windows (py launcher) and Linux/macOS.
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
import http.client
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

# --- Tunables (match nth_monitor.py where applicable) ---------------------
DEFAULT_URL          = "http://localhost:8000/sse"
DEFAULT_POLL_WAIT    = 15            # quartet_poll long-poll window (server cap 30)
DEFAULT_STATUS_EVERY = 30            # how often to recompute cadence/keepalive
CADENCE_THRESHOLD    = 600           # 10 min
KEEPALIVE_THRESHOLD  = 55 * 60       # 55 min
KEEPALIVE_GIVEUP    = 7 * 3600      # 7 h
RECONNECT_BACKOFF    = [1, 2, 5, 10, 30, 60]

# Sentinel keywords for "sleeping" mode (idle/standing-by). Mirror
# nth_constants.SLEEPING_KEYWORDS - kept inline so this script remains
# importable without the rest of the skill.
SLEEPING_KEYWORDS = (
    "idle", "sleeping", "asleep", "afk", "away", "out",
    "standing by", "stand by", "done", "task done",
    "back later", "brb", "off", "offline",
)

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


# --- Minimal MCP-over-SSE client (pure stdlib) ---------------------------
#
# MCP SSE transport (per the FastMCP server in nth_server.py + the
# 2024-11-05 spec): GET <sse-url> opens an event-stream; the server
# emits an `endpoint` event whose `data:` is the URL the client POSTs
# JSON-RPC requests to. Responses arrive back on the SSE stream as
# `message` events. POST returns 202 on accept; the real payload lands
# in the stream.
class _SSEDisconnect(Exception):
    pass


class MCPSSEClient:
    def __init__(self, base_url, debug=False):
        u = urllib.parse.urlparse(base_url)
        if u.scheme not in ("http", "https"):
            raise ValueError("URL scheme must be http or https")
        self.scheme  = u.scheme
        self.host    = u.hostname
        self.port    = u.port or (443 if u.scheme == "https" else 80)
        self.sse_path = u.path or "/sse"
        if u.query:
            self.sse_path += "?" + u.query
        self.base_origin = f"{u.scheme}://{u.netloc}"
        self.debug = debug

        self.endpoint_url = None
        self.endpoint_ready = threading.Event()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._stop = threading.Event()
        self._sse_thread = None
        self._initialized = threading.Event()
        self._init_lock = threading.Lock()

    def _dbg(self, *a):
        if self.debug:
            sys.stderr.write("[mcp-sse] " + " ".join(str(x) for x in a) + "\n")
            sys.stderr.flush()

    def _abs(self, url):
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = "/" + url
        return self.base_origin + url

    def _next_request_id(self):
        with self._id_lock:
            i = self._next_id
            self._next_id += 1
            return i

    def connect(self):
        self._sse_thread = threading.Thread(
            target=self._sse_loop, name="mcp-sse-reader", daemon=True
        )
        self._sse_thread.start()
        if not self.endpoint_ready.wait(timeout=20):
            raise RuntimeError("Timed out waiting for SSE endpoint event")
        # Initial handshake. Reruns automatically on every SSE reconnect
        # via _ensure_initialized() inside call() — survives hub restarts.
        return self._ensure_initialized(timeout=15)

    def close(self):
        self._stop.set()

    # ---- SSE reader thread ---------------------------------------------
    def _sse_loop(self):
        backoff_idx = 0
        while not self._stop.is_set():
            conn = None
            try:
                conn = self._open_sse()
                self._read_sse(conn)
                # Normal end of stream
                raise _SSEDisconnect("server closed stream")
            except Exception as e:
                self._dbg(f"sse loop error: {type(e).__name__}: {e}")
                # Wipe endpoint AND init state so the next call() re-runs the
                # MCP initialize handshake against the fresh transport session.
                self.endpoint_url = None
                self.endpoint_ready.clear()
                self._initialized.clear()
                # Fail any pending requests with disconnect error
                with self._pending_lock:
                    pending = list(self._pending.items())
                    self._pending.clear()
                for rid, q in pending:
                    try:
                        q.put_nowait({"jsonrpc": "2.0", "id": rid,
                                      "error": {"code": -32099,
                                                "message": f"SSE disconnected: {e}"}})
                    except queue.Full:
                        pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                return
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            backoff_idx = min(backoff_idx + 1, len(RECONNECT_BACKOFF) - 1)
            self._dbg(f"reconnecting in {delay}s")
            for _ in range(delay * 2):
                if self._stop.is_set():
                    return
                time.sleep(0.5)

    def _open_sse(self):
        conn = self._make_conn(timeout=None)
        try:
            conn.request("GET", self.sse_path, headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "User-Agent": "nth-spoke-monitor/1.0",
            })
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status != 200:
            body = resp.read()[:200]
            conn.close()
            raise RuntimeError(f"SSE GET {self.sse_path}: HTTP {resp.status} {body!r}")
        # Store response on conn so caller can close later
        conn._sse_resp = resp
        return conn

    def _read_sse(self, conn):
        resp = conn._sse_resp
        event_name = "message"
        data_lines = []
        while not self._stop.is_set():
            line_bytes = resp.fp.readline()
            if not line_bytes:
                raise _SSEDisconnect("EOF on SSE stream")
            try:
                line = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                line = line_bytes.decode("utf-8", errors="replace")
            line = line.rstrip("\r\n")
            if line == "":
                if data_lines:
                    self._handle_event(event_name, "\n".join(data_lines))
                event_name = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE comment / heartbeat
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
            # id: and retry: ignored

    def _handle_event(self, event_name, data):
        if event_name == "endpoint":
            self.endpoint_url = self._abs(data.strip())
            self.endpoint_ready.set()
            self._dbg(f"endpoint -> {self.endpoint_url}")
            return
        if event_name == "message":
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                self._dbg(f"bad sse data (first 200): {data[:200]}")
                return
            self._dbg(f"sse msg id={obj.get('id')} method={obj.get('method')}")
            rid = obj.get("id")
            if rid is not None:
                with self._pending_lock:
                    q = self._pending.pop(rid, None)
                if q is not None:
                    try:
                        q.put_nowait(obj)
                    except queue.Full:
                        pass
            # Server-initiated notifications: ignored for now (no params we use)
            return

    # ---- HTTP helpers ---------------------------------------------------
    def _make_conn(self, timeout):
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _post(self, body):
        if self.endpoint_url is None:
            raise RuntimeError("Not connected — no endpoint URL")
        u = urllib.parse.urlparse(self.endpoint_url)
        host = u.hostname or self.host
        port = u.port or self.port
        path = u.path + ("?" + u.query if u.query else "")
        scheme = u.scheme or self.scheme
        if scheme == "https":
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, timeout=20, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=20)
        try:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            conn.request("POST", path, body=data, headers={
                "Content-Type":  "application/json",
                "Accept":        "application/json, text/event-stream",
                "User-Agent":    "nth-spoke-monitor/1.0",
                "Content-Length": str(len(data)),
            })
            resp = conn.getresponse()
            if resp.status not in (200, 202):
                err = resp.read()[:300]
                raise RuntimeError(f"POST {path} -> HTTP {resp.status}: {err!r}")
            resp.read()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _post_and_wait(self, body, timeout):
        # Bypasses the initialized gate; used by both _do_initialize and call.
        rid = body["id"]
        q = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = q
        self._post(body)
        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{body.get('method','?')} id={rid} timed out after {timeout}s")
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"{body.get('method','?')} error {err.get('code')}: {err.get('message')}"
            )
        return resp.get("result")

    def _do_initialize(self, timeout=15):
        rid = self._next_request_id()
        result = self._post_and_wait({
            "jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nth-spoke-monitor", "version": "1.0"},
            },
        }, timeout=timeout)
        self._post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        return result

    def _ensure_initialized(self, timeout=15):
        # Idempotent under lock — exactly one handshake per SSE session.
        if self._initialized.is_set():
            return None
        with self._init_lock:
            if self._initialized.is_set():
                return None
            result = self._do_initialize(timeout=timeout)
            self._initialized.set()
            return result

    def call(self, method, params=None, timeout=60):
        if not self.endpoint_ready.wait(timeout=30):
            raise RuntimeError("Not connected (no SSE endpoint)")
        self._ensure_initialized(timeout=15)
        rid = self._next_request_id()
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        return self._post_and_wait(body, timeout)

    def call_tool(self, name, arguments=None, timeout=60):
        # Retry once on -32602 / "before initialization was complete": that
        # means the server-side transport session was reset under us (hub
        # restart). Force a re-handshake on the next attempt.
        try:
            result = self.call("tools/call",
                               {"name": name, "arguments": arguments or {}},
                               timeout=timeout)
        except RuntimeError as e:
            msg = str(e).lower()
            if "-32602" in msg or "before initialization" in msg:
                self._initialized.clear()
                result = self.call("tools/call",
                                   {"name": name, "arguments": arguments or {}},
                                   timeout=timeout)
            else:
                raise
        # MCP tools return {content: [{type:'text', text: '<JSON-as-string>'}], ...}
        # quartet tools wrap their dict response as text inside content[0].text.
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    txt = first.get("text", "")
                    try:
                        return json.loads(txt)
                    except json.JSONDecodeError:
                        return {"_raw": txt}
        return result


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
            if session_token:
                args["session_token"] = session_token
            poll = client.call_tool("quartet_poll", args,
                                    timeout=poll_wait_seconds + 30)
            consecutive_poll_errors = 0
        except Exception as e:
            consecutive_poll_errors += 1
            emit({"event": "error",
                  "msg": f"poll failed ({consecutive_poll_errors}): {e}"})
            time.sleep(min(2 * consecutive_poll_errors, 30))
            continue

        if isinstance(poll, dict):
            ev = poll.get("event")
            if ev == "ended" or poll.get("ended"):
                emit({"event": "channel_ended",
                      "ended_by": poll.get("ended_by")})
                return
            if ev == "channel_not_found" or poll.get("error") == "channel_not_found":
                emit({"event": "channel_gone"})
                return
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
                      "gap_seconds": round(own_gap),
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
                "gap_seconds": round(own_gap),
                "threshold_seconds": KEEPALIVE_THRESHOLD,
                "engaged_gap_seconds": round(engaged_gap),
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
