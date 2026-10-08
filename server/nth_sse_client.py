#!/usr/bin/env python3
"""Minimal MCP-over-SSE client, pure stdlib.

Every spoke-side process that talks to a Quartet hub uses this one client: the
spoke monitor, the Quartet stdio frontend, the Claude channel and hook listeners
and the Codex relay. It lived inside nth_spoke_monitor.py, so services imported
a monitor script to reach a hub; it stands alone here, and nth_spoke_monitor
re-exports it for older imports.

It identifies itself to the hub as nth-spoke-monitor, as it always has: hubs
and their logs already know that name.
"""
import http.client
import json
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.parse

RECONNECT_BACKOFF    = [1, 2, 5, 10, 30, 60]
SSE_READ_TIMEOUT     = 90            # server pings ~15s; 90s silent = dead socket


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
        self._active_conn = None
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
        self.force_reconnect()

    def force_reconnect(self):
        """Close the live SSE socket so the reader thread unblocks and runs
        its normal reconnect path. Safe to call from any thread."""
        conn = self._active_conn
        if conn is not None:
            # Shut the socket down first; close() alone does not unblock the
            # reader. For a chunked stream it needs the buffered reader's lock,
            # which the reader thread holds while blocked in recv: on Windows
            # close() then never returns, and a frontend that closes its client on
            # exit is left behind as an orphan process. For a close-delimited
            # stream http.client has already handed the socket to the response,
            # so close() touches nothing and the reader stays blocked.
            # Neither step may depend on the hub answering: a wedged hub is the
            # case this method exists for. shutdown() wakes a blocked recv on
            # Linux but not on Windows; closing the handle does on Windows.
            # detach() first, so the reader's socket object can never touch a
            # handle number the OS has since given to someone else.
            sock = getattr(conn, '_sse_sock', None) or conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    socket.close(sock.detach())
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

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
        # A read timeout is the ONLY reliable dead-hub detector: a restart
        # that skips the FIN leaves readline() blocking forever otherwise.
        conn = self._make_conn(timeout=SSE_READ_TIMEOUT)
        try:
            conn.request("GET", self.sse_path, headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "User-Agent": "nth-spoke-monitor/1.0",
            })
            # Kept for force_reconnect(): getresponse() drops conn.sock when the
            # stream is close-delimited.
            conn._sse_sock = conn.sock
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
        self._active_conn = conn
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
