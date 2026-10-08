"""Burner pages from agents: trio_page and the dashboard's /pages/<id>.

What has to hold:

  * trio_page stores the HTML with an unguessable id and an expiry and posts
    the message that announces it, in one transaction; a refused page posts
    nothing;
  * /pages/<id> answers only a viewer who can see that message, with the exact
    sandbox CSP (scripts, no same-origin), nosniff and no-referrer;
  * an unnamed visitor is refused, a DM page opens only for its participants
    (and the all-seeing owner, as DMs do), a retracted announcement takes the
    page down, and a single-channel dashboard serves only its own channel;
  * an expired page answers 410 and the sweep removes it; ending a channel
    removes its pages; the 512 KB cap and the TTL bounds are enforced.

Usage: python tests/test-agent-pages.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="nth_agent_pages_")
os.environ["NTH_HOME"] = _tmp
os.environ["NTH_QUIET"] = "1"
os.environ.pop("NTH_DASHBOARD_URL", None)

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_media as nmedia   # noqa: E402
import nth_server as srv     # noqa: E402
import nth_web as web        # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
srv.ATTACH_DIR = Path(_tmp) / "attachments"
web.ATTACH_DIR = srv.ATTACH_DIR

# Written out in full on purpose: a change to the policy must fail this test,
# not silently follow a shared constant.
EXPECTED_CSP = ("sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; img-src data:; connect-src 'none'; "
                "frame-ancestors 'self'")
HTML = "<!doctype html><title>Chart</title><style>b{color:red}</style><b>42</b><script>1</script>"


def db():
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def message_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def page_rows():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]


a = json.loads(srv.nth_connect(summary="author", name="Ada", channel="charts"))
CH, ADA, ADA_TOKEN = a["channel"], a["member_id"], a["session_token"]
b = json.loads(srv.nth_connect(summary="reader", name="Bea", channel=CH))
BEA, BEA_TOKEN = b["member_id"], b["session_token"]
other = json.loads(srv.nth_connect(summary="elsewhere", name="Oz", channel="other-room"))
OTHER_CH = other["channel"]

# Two named web guests. The participant has a members row, so a DM can name it.
GUEST_IN = web.OperatorIdentity("_op_g_participant", "Pat", web.IDENTITY_SOURCE_GUEST)
GUEST_OUT = web.OperatorIdentity("_op_g_outsider", "Quin", web.IDENTITY_SOURCE_GUEST)
PENDING = web.OperatorIdentity("_op_p_visitor", "", web.IDENTITY_SOURCE_PENDING)
with db() as conn:
    web.ensure_operator_row(conn, CH, GUEST_IN)


def page(**kw):
    kw.setdefault("channel", CH)
    kw.setdefault("member_id", ADA)
    kw.setdefault("session_token", ADA_TOKEN)
    kw.setdefault("title", "Latency chart")
    kw.setdefault("html", HTML)
    return json.loads(srv.nth_page(**kw))


# ── create ──────────────────────────────────────────────────────────────────
r = page(message="@Bea the numbers you asked for")
p = r.get("page") or {}
check("create: ok, with a message id and a page", r.get("ok") is True and r.get("message_id") and p)
check("create: the id is long and unguessable", bool(nmedia.PAGE_ID_RE.match(p.get("id", "")))
      and len(p.get("id", "")) >= 32)
check("create: url is the dashboard path when no address is configured",
      r.get("url") == p.get("path") == "/pages/" + p.get("id", ""))
expires = datetime.fromisoformat(p.get("expires_at", "1970-01-01T00:00:00+00:00"))
check("create: expires 24 hours out by default",
      abs((expires - datetime.now(timezone.utc)) - timedelta(hours=24)) < timedelta(minutes=1))
with db() as conn:
    msg = conn.execute("SELECT content, mentions FROM messages WHERE id = ?",
                       (r.get("message_id"),)).fetchone()
check("create: the announcement carries the title and caption",
      msg is not None and msg["content"] == "[page] Latency chart\n\n@Bea the numbers you asked for")
check("create: the caption's sigils parse as in send", msg is not None and BEA in msg["mentions"])
PAGE_ID, PAGE_MSG = p.get("id"), r.get("message_id")
check("event: the dashboard's message event carries the page",
      (web._message_event(db(), db().execute("SELECT * FROM messages WHERE id = ?",
                                              (PAGE_MSG,)).fetchone(), CH)["page"] or {})
      .get("id") == PAGE_ID)

body = json.loads(srv.nth_poll(channel=CH, member_id=BEA, wait_seconds=0, session_token=BEA_TOKEN))
seen = [m for m in body.get("messages", []) if m["id"] == PAGE_MSG]
check("poll: another agent learns the page's title and path",
      seen and seen[0].get("page", {}).get("path") == "/pages/" + PAGE_ID)

srv.DASHBOARD_URL = "https://hub.example:8765"
try:
    r2 = page(title="With address")
    check("create: NTH_DASHBOARD_URL turns the path into a full address",
          r2.get("url") == "https://hub.example:8765/pages/" + r2["page"]["id"])
finally:
    srv.DASHBOARD_URL = ""

# ── refusals post nothing ───────────────────────────────────────────────────
before = (message_count(), page_rows())
for label, kw in (
        ("html over 512 KB", {"html": "x" * (nmedia.MAX_PAGE_BYTES + 1)}),
        ("multi-byte html over 512 KB", {"html": "é" * (nmedia.MAX_PAGE_BYTES // 2 + 1)}),
        ("empty html", {"html": "  "}),
        ("empty title", {"title": ""}),
        ("a title over 120 characters", {"title": "t" * 121}),
        ("ttl of zero", {"ttl_hours": 0}),
        ("ttl past a week", {"ttl_hours": 169}),
        ("an unknown member", {"member_id": "nobody", "session_token": ""}),
        ("someone else's token", {"session_token": BEA_TOKEN}),
        ("an unknown DM recipient", {"to": "nobody-at-all"})):
    out = page(**kw)
    check(f"refused: {label}", "error" in out and "ok" not in out)
check("refused: no message or page was stored", (message_count(), page_rows()) == before)
check("cap: html of exactly 512 KB is accepted",
      page(title="Full size", html="x" * nmedia.MAX_PAGE_BYTES).get("ok") is True)

live = page_rows()
real_limit = nmedia.MAX_LIVE_PAGES_PER_MEMBER
try:
    with db() as conn:
        mine = conn.execute("SELECT COUNT(*) FROM pages WHERE channel = ? AND member_id = ?",
                            (CH, ADA)).fetchone()[0]
    nmedia.MAX_LIVE_PAGES_PER_MEMBER = mine
    out = page(title="One too many")
    check("limit: live pages per member are capped", "error" in out and page_rows() == live)
finally:
    nmedia.MAX_LIVE_PAGES_PER_MEMBER = real_limit

# A DM page, to the participating guest.
r = page(title="Private notes", to=GUEST_IN.member_id)
check("dm: a page can be posted as a DM", r.get("ok") is True and r.get("private") is True)
DM_PAGE = (r.get("page") or {}).get("id")

# ── serving ─────────────────────────────────────────────────────────────────
_real_resolve = web.NthWebHandler._resolve_identity


def as_identity(ident):
    if ident is None:
        web.NthWebHandler._resolve_identity = _real_resolve
    else:
        web.NthWebHandler._resolve_identity = lambda self: (None, ident, False)


def get(port, page_id):
    url = f"http://127.0.0.1:{port}/pages/{page_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


server = None
try:
    web.NthWebHandler.hub = None
    web.NthWebHandler.channel = None
    web.NthWebHandler.landing_mode = True
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # The loopback operator is the owner.
    st, headers, text = get(port, PAGE_ID)
    check("serve: the owner gets the page", st == 200 and text == HTML)
    check("serve: the CSP is exactly the sandbox policy",
          headers.get("Content-Security-Policy") == EXPECTED_CSP)
    check("serve: the sandbox never grants same-origin",
          "allow-same-origin" not in (headers.get("Content-Security-Policy") or ""))
    check("serve: nosniff", headers.get("X-Content-Type-Options") == "nosniff")
    check("serve: no referrer", headers.get("Referrer-Policy") == "no-referrer")
    check("serve: served as HTML, never cached",
          headers.get("Content-Type") == "text/html; charset=utf-8"
          and "no-store" in (headers.get("Cache-Control") or ""))

    as_identity(GUEST_OUT)
    st, _h, _t = get(port, PAGE_ID)
    check("serve: a named guest who can read the channel can open its page", st == 200)

    as_identity(PENDING)
    st, _h, _t = get(port, PAGE_ID)
    check("serve: an unnamed visitor is refused (403)", st == 403)

    as_identity(GUEST_IN)
    st, _h, text = get(port, DM_PAGE)
    check("dm: the participating guest opens the DM page", st == 200 and text == HTML)
    as_identity(GUEST_OUT)
    st, _h, text = get(port, DM_PAGE)
    check("dm: a guest outside the DM gets 404, the same as no page at all",
          st == 404 and HTML not in text)
    as_identity(None)
    st, _h, _t = get(port, DM_PAGE)
    check("dm: the all-seeing owner opens it, as with DMs", st == 200)

    st, _h, _t = get(port, "A" * 32)
    check("serve: an unknown id is 404", st == 404)
    st, _h, _t = get(port, "..%2Fnth.db")
    check("serve: a malformed id is 404", st == 404)

    # A single-channel dashboard serves its own channel only.
    web.NthWebHandler.landing_mode = False
    web.NthWebHandler.channel = OTHER_CH
    st, _h, _t = get(port, PAGE_ID)
    check("serve: a dashboard bound to another channel gets 404", st == 404)
    web.NthWebHandler.channel = CH
    st, _h, _t = get(port, PAGE_ID)
    check("serve: a dashboard bound to the page's channel serves it", st == 200)
    web.NthWebHandler.landing_mode = True
    web.NthWebHandler.channel = None

    # Retracting the announcement takes the page down.
    r = page(title="Withdrawn")
    srv.nth_retract(channel=CH, member_id=ADA, message_id=r["message_id"],
                    reason="wrong data", session_token=ADA_TOKEN)
    st, _h, _t = get(port, r["page"]["id"])
    check("retract: a retracted announcement's page is 404", st == 404)

    # ── expiry and the sweep ───────────────────────────────────────────────
    r = page(title="Short lived", ttl_hours=0.5)
    short = r["page"]["id"]
    st, _h, _t = get(port, short)
    check("expiry: a fresh page opens", st == 200)
    past = nmedia.utc_stamp(datetime.now(timezone.utc) - timedelta(minutes=1))
    with db() as conn:
        conn.execute("UPDATE pages SET expires_at = ? WHERE id = ?", (past, short))
    st, _h, text = get(port, short)
    check("expiry: an expired page answers 410", st == 410 and HTML not in text)
    stats = web.sweep_attachments(srv.DB_PATH, force=True)
    check("expiry: the sweep removes it", stats.get("expired_pages") == 1)
    st, _h, _t = get(port, short)
    check("expiry: a swept page is 404", st == 404)

    # Creating a page also sweeps expired ones.
    r = page(title="Another short one")
    with db() as conn:
        conn.execute("UPDATE pages SET expires_at = ? WHERE id = ?", (past, r["page"]["id"]))
    page(title="Trigger")
    with db() as conn:
        gone = conn.execute("SELECT 1 FROM pages WHERE id = ?", (r["page"]["id"],)).fetchone()
    check("expiry: a new page sweeps expired ones", gone is None)

    # ── ending the channel ─────────────────────────────────────────────────
    with db() as conn:
        live_here = conn.execute("SELECT COUNT(*) FROM pages WHERE channel = ?", (CH,)).fetchone()[0]
    check("end: the channel has live pages before it ends", live_here > 0)
    srv.nth_end(channel=CH, member_id=ADA)
    with db() as conn:
        left = conn.execute("SELECT COUNT(*) FROM pages WHERE channel = ?", (CH,)).fetchone()[0]
    check("end: ending the channel removes its pages", left == 0)
    st, _h, _t = get(port, PAGE_ID)
    check("end: its page link is 404", st == 404)
    st, _h, _t = get(port, DM_PAGE)
    check("end: a DM page lives on the DM transport, outside the ended channel", st == 200)
finally:
    web.NthWebHandler._resolve_identity = _real_resolve
    if server is not None:
        server.shutdown()
        server.server_close()

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
