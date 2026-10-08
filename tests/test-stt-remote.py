"""Tests for the remote speech-service backend of /api/stt/* (NTH_STT_URL).

A fake speech service runs on localhost and speaks the service's API: GET
/health and POST /transcribe behind a bearer token. The tests drive the real
RemoteStt class and the real _handle_transcribe handler against it, covering:

  * configuration: URL validation, token file missing / open to other users /
    malformed, a bad NTH_STT_TIMEOUT, none of which may stop nth_web importing;
  * /api/stt/health: the response keys the client depends on, available /
    unavailable / timeout, the probe cache, and redirects left unfollowed;
  * /api/stt/transcribe: bytes and Content-Type forwarded, the service's
    errors mapped to messages the client already understands, its error text
    kept out of responses, the size cap and the concurrency slots;
  * the token never appearing in a log line or a response;
  * NTH_STT_URL unset keeping the mlx sidecar path.

No model, microphone or network beyond 127.0.0.1 is needed.

Usage: python tests/test-stt-remote.py
"""
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_web as web          # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


TOKEN = "test-token-6f1d0c2a9b"
MODEL = "fake-parakeet-int8"
PRIVATE_PATH = "/srv/speech/tmp/upload-81723.webm"
DEEP_JSON = b"[" * 200000           # json.loads raises RecursionError on this


# ── A fake speech service ────────────────────────────────────────────────────
class FakeService:
    """Records every request; `health` and `transcribe` pick its behaviour."""

    def __init__(self):
        self.health = "ok"
        self.transcribe = "ok"
        self.hits = {"health": 0, "transcribe": 0}
        self.last = {}
        self.redirect_to = ""
        self.delay = 0.0


def make_handler(svc):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # keep test output readable
            pass

        def _send(self, status, obj=None, raw=None, headers=None):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            return self.headers.get("Authorization") == f"Bearer {TOKEN}"

        def do_GET(self):
            if self.path.endswith("/health"):
                svc.hits["health"] += 1
            else:
                svc.hits.setdefault("other", 0)
                svc.hits["other"] += 1
                self._send(200, {"ok": True})
                return
            mode = svc.health
            if mode == "slow":
                time.sleep(svc.delay)
            if not self._authorized():
                self._send(401, {"ok": False, "error": "bad token"})
            elif mode == "redirect":
                self._send(302, {"ok": False}, headers={"Location": svc.redirect_to + "/health"})
            elif mode == "notready":
                self._send(200, {"ok": False, "model": MODEL, "warm": False})
            elif mode == "cold":
                self._send(200, {"ok": True, "model": MODEL, "quantization": "int8", "warm": False})
            elif mode == "500":
                self._send(500, {"ok": False, "error": "boom"})
            elif mode == "deep":
                self._send(200, raw=DEEP_JSON)
            else:
                self._send(200, {"ok": True, "model": MODEL, "quantization": "int8", "warm": True})

        def do_POST(self):
            svc.hits["transcribe"] += 1
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            svc.last = {"path": self.path, "body": body,
                        "content_type": self.headers.get("Content-Type"),
                        "authorization": self.headers.get("Authorization")}
            mode = svc.transcribe
            if mode == "slow":
                time.sleep(svc.delay)
            if not self._authorized():
                self._send(401, {"ok": False, "error": "bad token"})
            elif mode == "ok":
                self._send(200, {"ok": True, "text": "hello from the service", "seconds": 0.9})
            elif mode == "empty":
                self._send(200, {"ok": True, "text": "  ", "seconds": 0.2})
            elif mode == "422":
                self._send(422, {"ok": False,
                                 "error": f"ffmpeg could not decode {PRIVATE_PATH}: invalid data"})
            elif mode == "500":
                self._send(500, {"ok": False, "error": f"inference failed at {PRIVATE_PATH}"})
            elif mode == "413":
                self._send(413, {"ok": False, "error": "too large"})
            elif mode == "503":
                self._send(503, {"ok": False, "error": "overloaded"})
            elif mode == "garbage":
                self._send(200, raw=b"<html>not json</html>")
            elif mode == "deep":
                self._send(200, raw=DEEP_JSON)
            elif mode == "huge-number":
                # Valid JSON whose number does not fit in a float.
                self._send(200, raw=b'{"ok": true, "text": "hi", "seconds": 1' + b"0" * 400 + b"}")
            elif mode == "echo-token":
                # A careless service that puts request headers in its errors.
                self._send(422, {"ok": False,
                                 "error": f"rejected: {self.headers.get('Authorization')}"})
            elif mode == "redirect":
                self._send(307, {"ok": False},
                           headers={"Location": svc.redirect_to + "/transcribe"})
            else:
                self._send(200, {"ok": True, "text": "?", "seconds": 0})

    return Handler


def start(svc):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class DripServer:
    """Answers every request one byte per DRIP_INTERVAL_S, either inside the
    status line and headers ("headers") or inside a declared body ("body").

    Each byte arrives well inside any socket timeout, so only an overall
    deadline can stop the client waiting. The drip stops after DRIP_LIMIT_S
    so a client without one fails its elapsed-time check instead of hanging
    the suite.
    """

    DRIP_INTERVAL_S = 0.2
    DRIP_LIMIT_S = 6.0

    def __init__(self, where):
        self.where = where
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.base = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_request(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return
            body += chunk

    def _serve(self, conn):
        try:
            self._read_request(conn)
            if self.where == "headers":
                prefix, drip = b"", b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 4096
            else:
                prefix = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" \
                         b"Content-Length: 4096\r\n\r\n"
                drip = b" " * 4096
            conn.sendall(prefix)
            stop = time.monotonic() + self.DRIP_LIMIT_S
            for i in range(len(drip)):
                if time.monotonic() > stop:
                    break
                conn.sendall(drip[i:i + 1])
                time.sleep(self.DRIP_INTERVAL_S)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


svc = FakeService()
service, BASE = start(svc)
other = FakeService()            # the host a redirect points at
other_server, OTHER_BASE = start(other)
svc.redirect_to = OTHER_BASE

# A port with nothing listening: bind, read the number, close.
_probe_sock = socket.socket()
_probe_sock.bind(("127.0.0.1", 0))
DEAD_BASE = f"http://127.0.0.1:{_probe_sock.getsockname()[1]}"
_probe_sock.close()

tmpdir = tempfile.mkdtemp(prefix="nth_stt_remote_")


def token_file(content=TOKEN + "\n", mode=0o600, name="token"):
    path = Path(tmpdir) / name
    path.write_text(content)
    os.chmod(path, mode)
    return str(path)


GOOD_TOKEN_FILE = token_file()

# Everything the module writes to stderr is captured, so the token can be
# searched for in it at the end. Responses are collected for the same reason.
_real_stderr = sys.stderr
captured = io.StringIO()
sys.stderr = captured
responses = []


def remote(url=BASE, tf=GOOD_TOKEN_FILE, timeout=5):
    return web.RemoteStt(url, tf, timeout)


def reset(health="ok", transcribe="ok"):
    svc.health, svc.transcribe = health, transcribe
    svc.hits.update(health=0, transcribe=0)
    other.hits.update(health=0, transcribe=0)


CONTRACT_KEYS = {"engine", "model", "available", "warm", "detail"}

try:
    # ── 1. Configuration ─────────────────────────────────────────────────────
    t = web._stt_remote_target("http://stt.example:10400")
    check("url: http with port", t == ("http", "stt.example", 10400, ""))
    t = web._stt_remote_target("https://stt.example/speech/")
    check("url: https default port, base path kept without trailing slash",
          t == ("https", "stt.example", 443, "/speech"))
    for bad in ("ftp://stt.example", "file:///etc/passwd", "stt.example:10400",
                "http://", "http://user:pw@stt.example", "http://stt.example/?x=1",
                "http://stt.example:notaport"):
        try:
            web._stt_remote_target(bad)
            check(f"url: rejects {bad!r}", False)
        except ValueError:
            check(f"url: rejects {bad!r}", True)

    b = remote(url="ftp://stt.example")
    check("bad url: held as config_error, not raised", bool(b.config_error))
    h = b.health()
    check("bad url: health unavailable with the contract keys",
          h["available"] is False and CONTRACT_KEYS <= set(h))

    missing = remote(tf=str(Path(tmpdir) / "no-such-token"))
    check("missing token file: config_error says missing",
          "missing" in (missing.config_error or ""))
    h = missing.health()
    check("missing token file: health unavailable", h["available"] is False)
    check("missing token file: detail names no path", tmpdir not in h["detail"])
    check("missing token file: still remote:true with a strict boolean",
          h.get("remote") is True and h["available"] is False)

    reset()
    wr = remote(tf=token_file(mode=0o644, name="world-readable"))
    check("world-readable token file: refused",
          "outside its group" in (wr.config_error or ""))
    check("world-readable token file: health unavailable", wr.health()["available"] is False)
    try:
        wr.transcribe(b"abcd", "audio/webm")
        check("world-readable token file: transcribe refuses", False)
    except RuntimeError as e:
        check("world-readable token file: transcribe refuses", "outside its group" in str(e))
    check("world-readable token file: the service is never contacted",
          svc.hits["health"] == 0 and svc.hits["transcribe"] == 0)
    check("world-readable token file: token not kept", wr._token == "")

    check("group-readable token file: accepted (only users outside the group are refused)",
          remote(tf=token_file(mode=0o640, name="group-readable")).config_error is None)
    check("token file unset: refused", "not set" in (remote(tf="").config_error or ""))
    check("empty token file: refused",
          "single-line" in (remote(tf=token_file("", name="empty")).config_error or ""))
    check("two-line token file: refused",
          "single-line" in (remote(tf=token_file("a\nb\n", name="two")).config_error or ""))
    check("directory as token file: refused",
          bool(remote(tf=tmpdir).config_error))
    check("good token file: accepted, trailing newline stripped",
          remote().config_error is None and remote()._token == TOKEN)

    check("forwarded type: opus webm kept",
          web._stt_forward_type("audio/webm;codecs=opus") == "audio/webm;codecs=opus")
    check("forwarded type: junk replaced",
          web._stt_forward_type("not a type") == "application/octet-stream")

    # ── 2. Health ────────────────────────────────────────────────────────────
    reset()
    r = remote()
    h = r.health()
    check("health: contract keys present", CONTRACT_KEYS <= set(h))
    check("health: engine is remote", h["engine"] == "remote")
    check("health: model is what the service reports", h["model"] == MODEL)
    check("health: available and warm", h["available"] is True and h["warm"] is True)
    check("health: cached:true so no 'downloading' label shows", h.get("cached") is True)
    check("health: remote:true marks where the audio goes", h.get("remote") is True)
    check("health: available/warm are strict booleans, detail/model strings",
          type(h["available"]) is bool and type(h["warm"]) is bool
          and isinstance(h["detail"], str) and isinstance(h["model"], str))

    # Cache: a second call within the TTL does not reach the service.
    r.health()
    r.health()
    check("health: probe cached (one request for three calls)", svc.hits["health"] == 1)
    r._probe = (time.monotonic() - 1, r._probe[1])   # expire it
    r.health()
    check("health: re-probed after the TTL", svc.hits["health"] == 2)

    reset(health="cold")
    h = remote().health()
    check("health: cold service is available but not warm",
          h["available"] is True and h["warm"] is False)

    reset(health="notready")
    h = remote().health()
    check("health: ok:false -> unavailable", h["available"] is False and "not ready" in h["detail"])

    reset(health="500")
    h = remote().health()
    check("health: HTTP 500 -> unavailable", h["available"] is False and "500" in h["detail"])

    reset()
    wrong = remote(tf=token_file("wrong-token\n", name="wrong"))
    h = wrong.health()
    check("health: rejected token -> unavailable, says so",
          h["available"] is False and "token" in h["detail"])

    reset()
    down = remote(url=DEAD_BASE)
    h = down.health()
    check("health: nothing listening -> unavailable", h["available"] is False
          and "unreachable" in h["detail"])
    down.health()
    check("health: a failure is cached too", down._probe is not None)
    check("health: failure TTL is shorter than the success TTL",
          web.STT_REMOTE_DOWN_TTL_S < web.STT_PROBE_TTL_S)

    reset(health="slow")
    svc.delay = 2.0
    saved_timeout = web.STT_REMOTE_HEALTH_TIMEOUT
    web.STT_REMOTE_HEALTH_TIMEOUT = 0.4
    try:
        t0 = time.monotonic()
        h = remote().health()
        elapsed = time.monotonic() - t0
    finally:
        web.STT_REMOTE_HEALTH_TIMEOUT = saved_timeout
    check("health: slow service -> unavailable", h["available"] is False
          and "in time" in h["detail"])
    check("health: bounded by the health timeout", elapsed < 1.5)
    time.sleep(svc.delay)        # let the slow handler finish before reuse
    svc.delay = 0.0

    reset(health="redirect")
    h = remote().health()
    check("health: redirect not followed", other.hits["health"] == 0)
    check("health: redirect reported", h["available"] is False and "redirect" in h["detail"])

    # The route's own answer: same keys whichever backend serves it.
    reset()
    saved_remote = web.STT_REMOTE
    try:
        web.STT_REMOTE = remote()
        hr = web.stt_health()
        web.STT_REMOTE = None
        hm = web.stt_health()
    finally:
        web.STT_REMOTE = saved_remote
    check("stt_health: remote backend selected when configured", hr["engine"] == "remote")
    check("stt_health: mlx backend when not configured", hm["engine"] == "mlx_whisper")
    check("stt_health: mlx response carries no remote flag", not hm.get("remote"))
    check("stt_health: both backends answer with the contract keys",
          CONTRACT_KEYS <= set(hr) and CONTRACT_KEYS <= set(hm))
    responses.extend([hr, hm])

    # ── 3. Transcribe through the real handler ───────────────────────────────
    class _FakeRfile:
        def __init__(self, data):
            self._d = data

        def read(self, n):
            return self._d[:n]

    class _FakeConn:
        def __init__(self):
            self.timeout = None

        def gettimeout(self):
            return self.timeout

        def settimeout(self, value):
            self.timeout = value

    def drive(backend, data=b"\x1aE\xdf\xa3webm-bytes", ctype="audio/webm;codecs=opus",
              length=None):
        h = web.NthWebHandler.__new__(web.NthWebHandler)   # bypass socket setup
        sent = {}
        h._json = lambda obj, status=200, **kw: sent.update(body=obj, status=status)
        h._error = lambda status, msg: sent.update(body={"error": msg}, status=status)
        ident = types.SimpleNamespace(source=web.IDENTITY_SOURCE_TAILSCALE,
                                      member_id="_op_test", display_name="tester")
        h._resolve_identity = lambda: ("tok", ident, False)
        h.headers = {"Content-Length": str(len(data) if length is None else length),
                     "Content-Type": ctype}
        h.rfile = _FakeRfile(data)
        h.connection = _FakeConn()
        saved = web.STT_REMOTE
        web.STT_REMOTE = backend
        try:
            h._handle_transcribe()
        finally:
            web.STT_REMOTE = saved
        responses.append(sent.get("body"))
        return sent

    # Success: the bytes and type arrive unchanged, with the bearer token, and
    # no temp file is written for the remote path.
    reset()
    audio = b"\x1aE\xdf\xa3" + bytes(range(256)) * 4
    real_mkstemp = web.tempfile.mkstemp

    def _no_tempfile(*a, **k):
        raise AssertionError("remote path must not write a temp file")

    cold = drive(remote(), data=b"x")["body"]
    check("transcribe: model is '' (not invented) before any health probe",
          cold.get("ok") is True and cold.get("model") == "")
    reset()
    warmed = remote()
    warmed.health()          # the client checks health before it dictates
    web.tempfile.mkstemp = _no_tempfile
    try:
        s = drive(warmed, data=audio)
    except AssertionError as e:
        s = {"body": {"error": str(e)}}
    finally:
        web.tempfile.mkstemp = real_mkstemp
    check("transcribe: the speech service is called, not the sidecar",
          svc.hits["transcribe"] == 1)
    body = s.get("body") or {}
    check("transcribe: ok with the service's text",
          s.get("status") == 200 and body.get("ok") is True
          and body.get("text") == "hello from the service")
    check("transcribe: engine remote, model from the service",
          body.get("engine") == "remote" and body.get("model") == MODEL)
    check("transcribe: seconds relayed", body.get("seconds") == 0.9)
    check("transcribe: response keeps the client's fields",
          {"ok", "text", "seconds", "no_speech", "rms", "engine", "model"} <= set(body))
    check("transcribe: bytes forwarded unchanged", svc.last.get("body") == audio)
    check("transcribe: Content-Type forwarded",
          svc.last.get("content_type") == "audio/webm;codecs=opus")
    check("transcribe: posted to /transcribe with the bearer token",
          svc.last.get("path") == "/transcribe"
          and svc.last.get("authorization") == f"Bearer {TOKEN}")

    reset(transcribe="empty")
    body = drive(remote())["body"]
    check("transcribe: empty text reads as no_speech", body.get("ok") is True
          and body.get("no_speech") is True)

    # Error mapping. Engine failures keep their HTTP 200 + ok:false shape and
    # the generic message; the service's text (with its paths) stays out.
    for mode in ("422", "500"):
        reset(transcribe=mode)
        s = drive(remote())
        dumped = json.dumps(s.get("body"))
        check(f"transcribe {mode}: 200 ok:false",
              s.get("status") == 200 and s["body"].get("ok") is False)
        check(f"transcribe {mode}: the existing engine-error message",
              s["body"].get("error") == "the audio could not be transcribed")
        check(f"transcribe {mode}: service text and paths not relayed",
              PRIVATE_PATH not in dumped and "ffmpeg" not in dumped
              and "inference" not in dumped)
    check("transcribe 500: service text logged for the operator",
          "inference failed" in captured.getvalue())

    reset(transcribe="413")
    s = drive(remote())
    check("transcribe 413: ok:false, says too large",
          s["body"].get("ok") is False and "too large" in s["body"].get("error", ""))

    reset(transcribe="503")
    s = drive(remote())
    check("transcribe 503: reads as busy to the client (busy|try again)",
          "busy" in s["body"].get("error", "") and "try again" in s["body"].get("error", ""))

    reset()
    s = drive(remote(tf=token_file("wrong-token\n", name="wrong2")))
    check("transcribe 401: says the token was rejected",
          s.get("status") == 200 and "token" in s["body"].get("error", ""))

    reset(transcribe="garbage")
    s = drive(remote())
    check("transcribe: unreadable reply -> ok:false",
          s["body"].get("ok") is False and "unreadable" in s["body"].get("error", ""))

    reset()
    s = drive(remote(url=DEAD_BASE))
    check("transcribe: unreachable -> 200 ok:false (not the local-buffer 500)",
          s.get("status") == 200 and "unreachable" in s["body"].get("error", ""))

    reset(transcribe="slow")
    svc.delay = 2.0
    t0 = time.monotonic()
    s = drive(remote(timeout=0.5))
    elapsed = time.monotonic() - t0
    check("transcribe: slow service -> the existing 'timed out' message",
          "timed out" in s["body"].get("error", ""))
    check("transcribe: bounded by NTH_STT_TIMEOUT", elapsed < 1.5)
    time.sleep(svc.delay)
    svc.delay = 0.0

    reset(transcribe="redirect")
    s = drive(remote())
    check("transcribe: redirect not followed", other.hits["transcribe"] == 0)
    check("transcribe: redirect reported", "redirect" in s["body"].get("error", ""))

    reset(transcribe="ok")
    r = remote()
    r.health()
    check("transcribe: a failure clears a cached 'available'",
          r._probe is not None)
    r._host = "127.0.0.1"
    r._port = int(DEAD_BASE.rsplit(":", 1)[1])
    drive(r)
    check("transcribe: ... so the next health call re-probes", r._probe is None)

    # Size cap and concurrency slots are enforced before the service is called.
    reset()
    s = drive(remote(), data=b"", length=web.MAX_STT_BYTES + 1)
    check("size cap: oversized audio rejected with 400", s.get("status") == 400)
    check("size cap: refused as oversized, before reading the body",
          "oversized" in (s.get("body") or {}).get("error", ""))
    check("size cap: the service is never contacted", svc.hits["transcribe"] == 0)

    reset()
    held = 0
    while web.STT_SLOTS.acquire(blocking=False):
        held += 1
    try:
        s = drive(remote())
    finally:
        for _ in range(held):
            web.STT_SLOTS.release()
    check("slots: a full house answers 503 busy", s.get("status") == 503)
    check("slots: the service is never contacted", svc.hits["transcribe"] == 0)

    reset(transcribe="echo-token")
    s = drive(remote())
    check("echoed token: request still fails cleanly", s["body"].get("ok") is False)

    # NTH_STT_URL unset: the handler keeps writing a temp file for the sidecar.
    seen = {}

    def _fake_mlx(path):
        seen["path"] = path
        seen["exists"] = os.path.exists(path)
        return {"text": "from the sidecar", "seconds": 0.5, "no_speech": False, "rms": 0.1}

    saved_t = web.STT.transcribe
    web.STT.transcribe = _fake_mlx
    try:
        reset()
        s = drive(None, ctype="audio/webm")
    finally:
        web.STT.transcribe = saved_t
    check("mlx path: used when no speech service is configured",
          s["body"].get("engine") == "mlx_whisper" and s["body"].get("text") == "from the sidecar")
    check("mlx path: audio handed over as a temp file",
          seen.get("exists") is True and seen.get("path", "").endswith(".webm"))
    check("mlx path: temp file removed afterwards", not os.path.exists(seen.get("path", "/")))
    check("mlx path: the service is never contacted", svc.hits["transcribe"] == 0)

    # ── 3b. A service that drips its reply ───────────────────────────────────
    # One byte every 0.2 s never trips a 1 s socket timeout; only an overall
    # deadline ends the wait. Without one, a slot (transcribe) or the probe
    # lock (health) is held for as long as the service cares to drip.
    for where in ("headers", "body"):
        drip = DripServer(where)
        try:
            t0 = time.monotonic()
            s = drive(remote(url=drip.base, timeout=1))
            elapsed = time.monotonic() - t0
            check(f"drip in {where}: transcribe ends at its deadline ({elapsed:.1f}s)",
                  elapsed < 2.5)
            check(f"drip in {where}: transcribe reports the existing 'timed out'",
                  "timed out" in (s.get("body") or {}).get("error", ""))
            saved_timeout = web.STT_REMOTE_HEALTH_TIMEOUT
            web.STT_REMOTE_HEALTH_TIMEOUT = 1
            try:
                t0 = time.monotonic()
                h = remote(url=drip.base).health()
                elapsed = time.monotonic() - t0
            finally:
                web.STT_REMOTE_HEALTH_TIMEOUT = saved_timeout
            check(f"drip in {where}: health ends at its deadline ({elapsed:.1f}s)",
                  elapsed < 2.5)
            check(f"drip in {where}: health unavailable, 'did not answer in time'",
                  h["available"] is False and "in time" in h["detail"])
        finally:
            drip.close()

    check("health timeout fits inside the client's 3 s health wait",
          web.STT_REMOTE_HEALTH_TIMEOUT <= 2)

    # ── 3c. Malformed answers never escape as exceptions ─────────────────────
    reset(health="deep")
    try:
        h = remote().health()
        check("deep JSON: health answers unavailable", h["available"] is False)
        check("deep JSON: read as an unreadable reply, not an unplanned failure",
              "unreadable" in h["detail"])
    except RecursionError:
        check("deep JSON: health answers unavailable", False)

    reset(transcribe="deep")
    try:
        s = drive(remote())
        err = (s.get("body") or {}).get("error", "")
        check("deep JSON: transcribe answers 200 ok:false",
              s.get("status") == 200 and s["body"].get("ok") is False)
        check("deep JSON: no interpreter text reaches the client",
              "recursion" not in err.lower() and "stack" not in err.lower())
        check("deep JSON: transcribe says the reply was unreadable", "unreadable" in err)
    except RecursionError:
        check("deep JSON: transcribe answers 200 ok:false", False)

    reset(transcribe="huge-number")
    try:
        s = drive(remote())
        check("huge number: transcript kept, seconds dropped",
              s["body"].get("ok") is True and s["body"].get("seconds") is None)
    except OverflowError:
        check("huge number: transcript kept, seconds dropped", False)
    check("_stt_number: overflowing int is None", web._stt_number(10 ** 400) is None)

    broken = remote()

    def _explode(*a, **k):
        raise KeyError("something nobody planned for")

    broken._request = _explode
    try:
        h = broken.health()
        check("unexpected probe failure: health still answers unavailable",
              h["available"] is False and CONTRACT_KEYS <= set(h))
    except KeyError:
        check("unexpected probe failure: health still answers unavailable", False)
    try:
        broken.transcribe(b"abcd", "audio/webm")
        check("unexpected transcribe failure: RuntimeError for the client", False)
    except RuntimeError:
        check("unexpected transcribe failure: RuntimeError for the client", True)
    except KeyError:
        check("unexpected transcribe failure: RuntimeError for the client", False)

    # ── 3d. The token is removed before text is clipped ──────────────────────
    straddle = "x" * 295 + TOKEN + " tail"
    scrubbed = remote()._scrub(straddle)
    check("scrub: a token straddling the clip boundary leaves no prefix",
          TOKEN[:5] not in scrubbed)
    check("scrub: still clipped", len(scrubbed) <= 300)

    # ── 3e. Token file read failures stay configuration errors ───────────────
    real_fstat = web.os.fstat

    def _fstat_fails(fd):
        raise OSError("simulated fstat failure")

    web.os.fstat = _fstat_fails
    try:
        b = remote()
        check("fstat failure: held as config_error, not raised", bool(b.config_error))
    except OSError:
        check("fstat failure: held as config_error, not raised", False)
    finally:
        web.os.fstat = real_fstat

    # ── 3f. URL forms http.client would trip over later ──────────────────────
    for bad, why in (("http://stt.example/a;b", "';'"), ("http://stt.example/a b", "space"),
                     ("http://stt.example/a\tb", "tab"), ("http://stt.example/\u00e9", "non-ASCII")):
        try:
            web._stt_remote_target(bad)
            check(f"url: rejects {why} up front", False)
        except ValueError as e:
            check(f"url: rejects {why} up front", "NTH_STT_URL" in str(e))

    # ── 3g. HTTPS: the hand-built TLS connection verifies and works ──────────
    if not shutil.which("openssl"):
        print("SKIP: https path (openssl CLI not available)")
    else:
        key, cert = Path(tmpdir) / "tls.key", Path(tmpdir) / "tls.crt"
        made = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            capture_output=True, timeout=60)
        if made.returncode != 0:
            print("SKIP: https path (openssl could not make a test certificate)")
        else:
            tls_svc = FakeService()
            tls_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tls_svc))
            tls_server.daemon_threads = True
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(str(cert), str(key))
            tls_server.socket = server_ctx.wrap_socket(tls_server.socket, server_side=True)
            threading.Thread(target=tls_server.serve_forever, daemon=True).start()
            tls_base = f"https://127.0.0.1:{tls_server.server_address[1]}"
            saved_cafile = os.environ.get("SSL_CERT_FILE")
            try:
                h = remote(url=tls_base).health()
                check("https: an untrusted certificate is refused",
                      h["available"] is False and "certificate" in h["detail"])
                os.environ["SSL_CERT_FILE"] = str(cert)   # trust the test certificate
                h = remote(url=tls_base).health()
                check("https: health over verified TLS", h["available"] is True
                      and h["model"] == MODEL)
                s = drive(remote(url=tls_base), data=b"tls-audio")
                check("https: transcribe over verified TLS",
                      s["body"].get("text") == "hello from the service"
                      and tls_svc.last.get("body") == b"tls-audio")
            finally:
                if saved_cafile is None:
                    os.environ.pop("SSL_CERT_FILE", None)
                else:
                    os.environ["SSL_CERT_FILE"] = saved_cafile
                tls_server.shutdown()

finally:
    sys.stderr = _real_stderr

# ── 4. The token never leaves the process ───────────────────────────────────
log = captured.getvalue()
check("token: absent from everything logged", TOKEN not in log)
check("token: an echoed token is scrubbed before logging", "[token]" in log)
check("token: absent from every response",
      all(TOKEN not in json.dumps(r) for r in responses))


# ── 5. Import-time configuration (a fresh interpreter per case) ──────────────
def import_with(env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NTH_STT_")}
    env.update(env_extra)
    env["PYTHONPATH"] = str(SERVER)
    code = ("import json, nth_web as w\n"
            "print(json.dumps({'remote': w.STT_REMOTE is not None,"
            " 'timeout': w.STT_REMOTE_TIMEOUT, 'health': w.stt_health()}))\n")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=60, env=env)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = None
    return p.returncode, out, p.stderr


rc, out, err = import_with({})
check("unset NTH_STT_URL: imports, no remote backend, mlx health",
      rc == 0 and out and out["remote"] is False and out["health"]["engine"] == "mlx_whisper")

rc, out, err = import_with({"NTH_STT_URL": BASE, "NTH_STT_TOKEN_FILE": GOOD_TOKEN_FILE,
                            "NTH_STT_TIMEOUT": "12"})
check("configured: remote backend reports the service as available",
      rc == 0 and out and out["remote"] is True and out["health"]["available"] is True
      and out["health"]["model"] == MODEL)
check("configured: NTH_STT_TIMEOUT read", out and out["timeout"] == 12)

ww = token_file(mode=0o644, name="ww-import")
rc, out, err = import_with({"NTH_STT_URL": BASE, "NTH_STT_TOKEN_FILE": ww})
check("world-readable token file at startup: dashboard still imports", rc == 0 and out)
check("world-readable token file at startup: reported unavailable",
      out and out["health"]["available"] is False and out["health"]["engine"] == "remote")
check("world-readable token file at startup: a warning is logged", "outside its group" in err)
check("world-readable token file at startup: token not logged", TOKEN not in err)

rc, out, err = import_with({"NTH_STT_URL": "stt.example:10400",
                            "NTH_STT_TOKEN_FILE": GOOD_TOKEN_FILE, "NTH_STT_TIMEOUT": "soon"})
check("typo'd URL and timeout: dashboard still imports", rc == 0 and out)
check("typo'd timeout: default used", out and out["timeout"] == 60)
check("typo'd URL: reported unavailable",
      out and out["health"]["available"] is False and "http" in out["health"]["detail"])

service.shutdown()
other_server.shutdown()
shutil.rmtree(tmpdir, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
