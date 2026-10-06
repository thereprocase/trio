"""Tests for gzip transfer encoding on the app shell (NthWebHandler._serve_html).

Why this exists: the shell is one inlined page — every stylesheet and every
client module in a single response — so it is both the largest thing this
server sends and the most compressible. Compressing it is worth doing and
easy to get subtly wrong in ways that only show up on somebody else's client.

The three failure modes this guards, in order of how quietly they break:

  1. `gzip;q=0` means "gzip is NOT acceptable" and still contains the word
     gzip. A substring test answers that client with a gzipped body it just
     said it cannot decode, and the page is blank with no server-side error.
  2. Content-Length describing the DECODED length while the compressed bytes
     are written. The client waits forever for bytes that will never come, or
     the connection desyncs.
  3. Compression widening past the static shells. That is a security boundary
     (BREACH needs a secret and attacker-controlled input in one compressed
     body), not just a scoping preference — so it is asserted structurally
     below, not just sampled route by route.

Usage: python tests/test-web-gzip.py
"""
import gzip
import http.client
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


# ── unit: the Accept-Encoding parser ──────────────────────────────────────
# Driven directly rather than over HTTP because the interesting cases are
# header shapes a browser will not easily produce on demand.
ACCEPT_CASES = [
    (None,                       False, "absent header"),
    ("",                         False, "empty header"),
    ("gzip",                     True,  "plain gzip"),
    ("GZIP",                     True,  "gzip is case-insensitive"),
    (" gzip ",                   True,  "surrounding whitespace"),
    ("gzip, deflate, br",        True,  "gzip among others"),
    ("gzip;q=1.0",               True,  "explicit q=1"),
    ("gzip;q=0.5",               True,  "any positive q"),
    ("gzip;q=0",                 False, "q=0 is a refusal, not a mention"),
    ("gzip;q=0.0",               False, "q=0.0 is a refusal"),
    ("gzip ; q=0",               False, "q=0 with whitespace around the ;"),
    ("deflate, gzip;q=0",        False, "q=0 refusal alongside another coding"),
    ("identity",                 False, "identity only"),
    ("deflate, br",              False, "no gzip and no wildcard"),
    ("*",                        True,  "wildcard accepts gzip"),
    ("*;q=0",                    False, "wildcard refusal"),
    ("*;q=0, gzip",              True,  "explicit gzip overrides wildcard refusal"),
    ("*, gzip;q=0",              False, "explicit refusal overrides wildcard accept"),
    ("gzip;q=bogus",             False, "malformed q falls back to uncompressed"),
    ("gzip;q=2",                 False, "q above one is malformed"),
    ("gzip;q=1.1",               False, "q above one with decimal is malformed"),
    ("gzip;q=0.0001",            False, "q with excess precision is malformed"),
    ("gzip;q=1e-3",              False, "exponent q form is malformed"),
    ("gzip;q=+0.5",              False, "signed q form is malformed"),
    ("gzip;q=inf",               False, "infinite q form is malformed"),
    ("gzip;q=0, gzip",           False, "first duplicate refusal is honored"),
    ("gzip, gzip;q=0",           False, "last duplicate refusal is honored"),
    ("gzip;q=1;q=0",             False, "repeated q parameter is malformed"),
    ("gzip;foo",                 False, "bare extension parameter is malformed"),
    ("gzip;foo=bar",             False, "extension parameter is malformed"),
    ("gzip;q=.5;q=bogus",        False, "short and repeated q forms are malformed"),
]
for header, expected, label in ACCEPT_CASES:
    check(f"accept-encoding: {label}", web._accepts_gzip(header) is expected)


# ── structural: _serve_html is the ONLY place that can set Content-Encoding ──
# This is the claim that actually covers attachments, avatars, the request log
# and every route added after today. Enumerating routes could only ever sample.
source = (SERVER / "nth_web.py").read_text()
enc_lines = [n for n, line in enumerate(source.splitlines(), 1)
             if re.search(r'send_header\(\s*["\']Content-Encoding["\']', line)]
check("exactly one Content-Encoding send_header in the module", len(enc_lines) == 1)
if len(enc_lines) == 1:
    # Walk back to the enclosing def to prove it is the HTML path specifically.
    lines = source.splitlines()
    enclosing = next((lines[i].strip() for i in range(enc_lines[0] - 1, -1, -1)
                      if lines[i].lstrip().startswith("def ")), "")
    check("that Content-Encoding lives in _serve_html",
          enclosing.startswith("def _serve_html"))

check("_HTML_GZIP holds exactly the two static shells", len(web._HTML_GZIP) == 2)
check("_HTML_GZIP covers INDEX_HTML", web.INDEX_HTML in web._HTML_GZIP)
check("_HTML_GZIP covers LANDING_HTML", web.LANDING_HTML in web._HTML_GZIP)

# Determinism: a rebuilt archive must equal the stored one, or "identical build,
# identical bytes" stops holding and no cross-run comparison means anything.
check("gzip output is deterministic",
      web._gzip_bytes(b"nth" * 500) == web._gzip_bytes(b"nth" * 500))
check("gzip header carries no timestamp",
      web._gzip_bytes(b"nth" * 500)[4:8] == b"\x00\x00\x00\x00")


# ── live HTTP ─────────────────────────────────────────────────────────────
_tmp = tempfile.mkdtemp(prefix="nth_gzip_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def get(port, path, accept_encoding="__omit__", read_body=True):
    """Raw request with total control over Accept-Encoding.

    http.client rather than urllib because the header under test must be
    exactly what this function says it is — including absent — and a helper
    that quietly supplies its own default would make the "absent" case
    untestable while still printing PASS.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        if isinstance(accept_encoding, list):
            conn.putrequest("GET", path)
            for value in accept_encoding:
                conn.putheader("Accept-Encoding", value)
            conn.endheaders()
        else:
            headers = {}
            if accept_encoding != "__omit__":
                headers["Accept-Encoding"] = accept_encoding
            conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read() if read_body else b""
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


r = json.loads(srv.nth_connect(summary="t", name="R", channel="gziptest"))
CH = r["channel"]

hub = web.EventHub(srv.DB_PATH, CH)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    raw_shell = web.INDEX_HTML.encode("utf-8")

    # 1 — the happy path. Decoded equivalence is the assertion that matters:
    # "it said gzip" proves nothing about whether the page still works.
    st, h, body = get(port, "/", "gzip")
    check("gzip requested: 200", st == 200)
    check("gzip requested: Content-Encoding is gzip", h.get("Content-Encoding") == "gzip")
    check("gzip requested: decodes to the exact uncompressed shell",
          gzip.decompress(body) == raw_shell)
    check("gzip requested: Content-Length matches the bytes on the wire",
          h.get("Content-Length") == str(len(body)))
    check("gzip requested: Content-Length is the COMPRESSED length",
          int(h["Content-Length"]) < len(raw_shell))
    check("gzip requested: Vary advertises Accept-Encoding",
          "accept-encoding" in h.get("Vary", "").lower())
    check("gzip requested: Content-Type unchanged",
          h.get("Content-Type") == "text/html; charset=utf-8")

    # 2 — no Accept-Encoding at all. The plain path must still be intact.
    st, h, body = get(port, "/")
    check("no accept-encoding: 200", st == 200)
    check("no accept-encoding: no Content-Encoding header", "Content-Encoding" not in h)
    check("no accept-encoding: body is the plain shell", body == raw_shell)
    check("no accept-encoding: Content-Length matches", h.get("Content-Length") == str(len(body)))
    check("no accept-encoding: Vary still advertised",
          "accept-encoding" in h.get("Vary", "").lower())

    # 3 — the q=0 refusal, end to end. This is failure mode 1 from the header.
    st, h, body = get(port, "/", "gzip;q=0")
    check("gzip;q=0: not compressed", "Content-Encoding" not in h)
    check("gzip;q=0: body is the plain shell", body == raw_shell)

    st, h, body = get(port, "/", "identity")
    check("identity: not compressed", "Content-Encoding" not in h)
    check("identity: body is the plain shell", body == raw_shell)

    # Separate physical field-lines have the same semantics as a comma-joined
    # field value. A refusal in either line keeps the response readable.
    st, h, body = get(port, "/", ["gzip", "gzip;q=0"])
    check("duplicate accept-encoding lines honor refusal",
          "Content-Encoding" not in h and body == raw_shell)

    # 4 — the other shell. /fleet serves LANDING_HTML through the same method.
    raw_landing = web.LANDING_HTML.encode("utf-8")
    st, h, body = get(port, "/fleet", "gzip")
    check("/fleet: compressed", h.get("Content-Encoding") == "gzip")
    check("/fleet: decodes to the exact landing page",
          gzip.decompress(body) == raw_landing)

    # A pushState URL is the same shell; it must not lose compression just for
    # being a different path in UI_PATHS.
    st, h, body = get(port, "/tasks", "gzip")
    check("/tasks (pushState URL): compressed", h.get("Content-Encoding") == "gzip")
    check("/tasks: decodes to the shell", gzip.decompress(body) == raw_shell)

    # 5 — JSON is never compressed, even when gzip is on offer.
    st, h, body = get(port, "/api/health", "gzip")
    check("/api/health: 200", st == 200)
    check("/api/health: not compressed", "Content-Encoding" not in h)
    check("/api/health: still parses as JSON", isinstance(json.loads(body), dict))

    st, h, body = get(port, "/api/meta", "gzip")
    check("/api/meta: not compressed", "Content-Encoding" not in h)

    # 6 — SSE is never compressed. Headers only: the stream never ends.
    st, h, _ = get(port, "/api/events", "gzip", read_body=False)
    check("/api/events: 200", st == 200)
    check("/api/events: not compressed", "Content-Encoding" not in h)
    check("/api/events: still an event stream",
          h.get("Content-Type") == "text/event-stream")

    # 7 — the point of the exercise. Reported rather than threshold-asserted:
    # the ratio is a property of today's CSS and JS, and a test that fails when
    # the page legitimately changes shape is a test people delete.
    st, h, body = get(port, "/", "gzip")
    ratio = len(body) / len(raw_shell)
    print(f"\n  shell: {len(raw_shell):,} B raw -> {len(body):,} B gzip "
          f"({ratio:.1%}, {(1 - ratio):.0%} saved)")
    check("compression actually reduces the shell", len(body) < len(raw_shell))

finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all gzip checks passed")
