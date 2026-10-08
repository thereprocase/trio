"""Agents attach images to send and dm.

What has to hold:

  * a `path` item becomes bytes on the agent's own machine: the Quartet
    frontend converts it before forwarding, the local stdio server reads it
    directly, and a hub refuses it with directions to send data_base64;
  * a path is read only when it names a regular file under the size cap,
    never through /proc, /dev or /sys, by name or by symlink;
  * the hub keeps only bytes that sniff as one of the four inline image types,
    applies the per-file cap and the per-member quota, and a refusal leaves
    no message, row or file behind;
  * a stored image is linked to its message, served by the dashboard exactly
    like a human upload, and delivered to another agent's poll as an image
    block; a DM's image reaches its participants only.

Usage: python tests/test-agent-attachments.py
"""
import asyncio
import base64
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.mkdtemp(prefix="nth_agent_attach_")
os.environ["NTH_HOME"] = _tmp
os.environ["NTH_QUIET"] = "1"
os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp(prefix="nth_agent_attach_cfg_"))

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_media as nmedia   # noqa: E402
import nth_server as srv     # noqa: E402
import nth_web as web        # noqa: E402
from nth_constants import AGENT_INBOX_CHANNEL  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"
srv.ATTACH_DIR = Path(_tmp) / "attachments"
web.ATTACH_DIR = srv.ATTACH_DIR

# The sniffer reads only the signature, so padding makes inert test images.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 2040            # 2048 bytes
GIF = b"GIF89a" + b"\x02" * 1018                        # 1024 bytes
FILES = Path(tempfile.mkdtemp(prefix="nth_agent_files_"))


def b64(data):
    return base64.b64encode(data).decode("ascii")


def db():
    conn = sqlite3.connect(str(srv.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def counts():
    with db() as conn:
        return (conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0])


def disk_files():
    root = srv.ATTACH_DIR
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


a = json.loads(srv.nth_connect(summary="sender", name="Ada", channel="pics"))
CH, ADA, ADA_TOKEN = a["channel"], a["member_id"], a["session_token"]
b = json.loads(srv.nth_connect(summary="reader", name="Bea", channel=CH))
BEA, BEA_TOKEN = b["member_id"], b["session_token"]
c = json.loads(srv.nth_connect(summary="bystander", name="Cy", channel=CH))
CY, CY_TOKEN = c["member_id"], c["session_token"]
# Clear the join notices so each poll below sees only what this test sends.
for member, token in ((BEA, BEA_TOKEN), (CY, CY_TOKEN)):
    srv.nth_poll(channel=CH, member_id=member, wait_seconds=0, session_token=token)
    body = json.loads(srv.nth_poll(channel=CH, member_id=member, wait_seconds=0,
                                   session_token=token))
    for m in body.get("messages", []):
        srv.nth_ack(channel=CH, member_id=member, through_id=m["id"], session_token=token)


def send(**kw):
    kw.setdefault("channel", CH)
    kw.setdefault("member_id", ADA)
    kw.setdefault("session_token", ADA_TOKEN)
    return json.loads(srv.nth_send(**kw))


# ── the frontend's conversion: path -> data_base64 ──────────────────────────
shot = FILES / "screen shot.png"
shot.write_bytes(PNG)
inlined = nmedia.inline_local_paths([{"path": str(shot)}])
check("frontend: a path item becomes data_base64 with the file's name",
      inlined == [{"data_base64": b64(PNG), "filename": "screen shot.png"}])
check("frontend: a list sent as JSON text is read like the list",
      nmedia.inline_local_paths(json.dumps([{"path": str(shot)}])) == inlined)
check("frontend: a data_base64 item passes through unchanged",
      nmedia.inline_local_paths([{"data_base64": b64(GIF), "filename": "g.gif"}])
      == [{"data_base64": b64(GIF), "filename": "g.gif"}])

# The hub never reads paths (this module was imported, not run as stdio).
check("hub: path reads are off unless the stdio entry point turns them on",
      srv._READS_CALLER_PATHS is False)
r = send(message="from the hub's view", attachments=[{"path": str(shot)}])
check("hub: a path item is refused with directions to send data_base64",
      "error" in r and "data_base64" in r["error"])

before = counts()
r = send(message="here is the screen", attachments=inlined)
check("hub: the converted item is accepted", r.get("ok") is True)
att = (r.get("attachments") or [{}])[0]
check("hub: the result names the stored image",
      att.get("mime") == "image/png" and att.get("filename") == "screen shot.png"
      and att.get("bytes") == len(PNG))
with db() as conn:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att.get("id"),)).fetchone()
check("store: the row is linked to the message and owned by the sender",
      row is not None and row["message_id"] == r.get("message_id")
      and row["member_id"] == ADA and row["channel"] == CH)
check("store: the file sits in the channel's directory with the sniffed extension",
      row is not None and Path(row["path"]).parent == srv.ATTACH_DIR / CH
      and Path(row["path"]).suffix == ".png" and Path(row["path"]).read_bytes() == PNG)
check("store: one message and one attachment were added", counts() == (before[0] + 1, before[1] + 1))
PNG_MSG, PNG_ATT = r.get("message_id"), att.get("id")

# The full frontend: quartet_send with a path reaches the hub as bytes only.
try:
    from mcp import types
    import nth_quartet_proxy as proxy

    class FakeHub:
        instances = []

        def __init__(self, url):
            self.calls = []
            FakeHub.instances.append(self)

        def connect(self):
            pass

        def close(self):
            pass

        def call(self, method, params=None, timeout=60):
            self.calls.append((method, params))
            if method == "tools/list":
                return {"tools": []}
            text = json.dumps({"ok": True})
            return {"content": [{"type": "text", "text": text}], "isError": False}

    env = {k: v for k, v in os.environ.items() if not k.startswith(("TRIO_", "NTH_"))}
    env.update(NTH_HOME=_tmp, TRIO_NATIVE_CLIENT="codex")
    with patch.dict(os.environ, env, clear=True), patch.object(proxy, "MCPSSEClient", FakeHub):
        server, _client, _hub = proxy.create_server("http://hub.example/sse")

    def call(name, **arguments):
        request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name=name, arguments=arguments))
        with patch.dict(os.environ, env, clear=True):
            return asyncio.run(server.request_handlers[types.CallToolRequest](request)).root

    remote = FakeHub.instances[0]
    call("quartet_send", channel=CH, member_id=ADA, message="m",
         attachments=[{"path": str(shot)}, {"data_base64": b64(GIF), "filename": "g.gif"}])
    sent = [p for m, p in remote.calls if m == "tools/call"][-1]["arguments"]["attachments"]
    check("proxy: quartet_send forwards file bytes and no path",
          sent == [{"data_base64": b64(PNG), "filename": "screen shot.png"},
                   {"data_base64": b64(GIF), "filename": "g.gif"}])
    call("quartet_dm", member_id=ADA, message="m", to="Bea", attachments=[{"path": str(shot)}])
    check("proxy: quartet_dm converts paths too",
          [p for m, p in remote.calls if m == "tools/call"][-1]["arguments"]["attachments"][0]
          .get("data_base64") == b64(PNG))
    def tool_calls():
        # The library may list tools before a call; only tools/call reaches the hub's tools.
        return [params for method, params in remote.calls if method == "tools/call"]

    calls_before = len(tool_calls())
    result = call("quartet_send", channel=CH, member_id=ADA, message="m",
                  attachments=[{"path": "/proc/self/environ"}])
    body = json.loads(result.content[0].text)
    check("proxy: an unreadable path is answered locally and never reaches the hub",
          "error" in body and "/proc" in body["error"] and len(tool_calls()) == calls_before)
except ImportError as exc:   # the mcp SDK is required by nth_server above anyway
    check(f"proxy: importable ({exc})", False)

# ── base64 straight from a direct-SSE client ────────────────────────────────
r = send(message="", attachments=[{"data_base64": "data:image/gif;base64," + b64(GIF),
                                   "filename": "anim.gif"}])
check("base64: an image-only send is accepted (data: URL prefix allowed)", r.get("ok") is True)
with db() as conn:
    content = conn.execute("SELECT content FROM messages WHERE id = ?",
                           (r.get("message_id"),)).fetchone()[0]
check("base64: an image-only message reads [image], like the dashboard's",
      content == "[image]")

# ── refusals leave nothing behind ───────────────────────────────────────────
files_before, rows_before = disk_files(), counts()
for label, items in (
        ("plain text", [{"data_base64": b64(b"API_KEY=not-an-image\n"), "filename": "x.png"}]),
        ("a PDF", [{"data_base64": b64(b"%PDF-1.7\n" + b"x" * 64), "filename": "r.pdf"}]),
        ("HEIC", [{"data_base64": b64(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)}]),
        ("invalid base64", [{"data_base64": "not base64 !!"}]),
        ("both path and data", [{"path": str(shot), "data_base64": b64(PNG)}]),
        ("neither path nor data", [{"filename": "x.png"}]),
        ("nine images", [{"data_base64": b64(PNG)}] * 9),
        ("a good image after a bad one", [{"data_base64": b64(PNG)},
                                          {"data_base64": b64(b"#!/bin/sh\n")}])):
    r = send(message="should not post", attachments=items)
    check(f"refused: {label}", "error" in r and "ok" not in r)
check("refused: no message, row or file was left behind",
      counts() == rows_before and disk_files() == files_before)

def raises(fn):
    try:
        fn()
    except nmedia.RichContentError:
        return True
    return False


real_cap = nmedia.MAX_UPLOAD_BYTES
try:
    nmedia.MAX_UPLOAD_BYTES = 1024
    before = counts()
    r = send(message="big", attachments=[{"data_base64": b64(PNG)}])
    check("cap: an image over the per-file cap is refused",
          "error" in r and "limit" in r["error"] and counts() == before)
    check("cap: the frontend refuses a file over the per-file cap",
          raises(lambda: nmedia.inline_local_paths([{"path": str(shot)}])))
finally:
    nmedia.MAX_UPLOAD_BYTES = real_cap

# ── which paths may be read ─────────────────────────────────────────────────
link_to_proc = FILES / "innocent.png"
os.symlink("/proc/self/status", link_to_proc)
fifo = FILES / "pipe.png"
os.mkfifo(fifo)
for label, path in (("a relative path", "screen shot.png"),
                    ("a directory", str(FILES)),
                    ("a file under /proc", "/proc/self/status"),
                    ("a device", "/dev/zero"),
                    ("a symlink into /proc", str(link_to_proc)),
                    ("a FIFO", str(fifo)),
                    ("a missing file", str(FILES / "missing.png"))):
    started = time.monotonic()
    check(f"path: {label} is refused", raises(lambda: nmedia.read_local_file(path, 1 << 20)))
    check(f"path: refusing {label} does not block", time.monotonic() - started < 2)

# The local stdio server reads paths itself, under the same checks.
srv._READS_CALLER_PATHS = True
try:
    r = send(message="local read", attachments=[{"path": str(shot), "filename": "renamed.png"}])
    check("local: the stdio server reads a path directly", r.get("ok") is True
          and r["attachments"][0]["filename"] == "renamed.png")
    r = send(message="local proc", attachments=[{"path": str(link_to_proc)}])
    check("local: the stdio server refuses a symlink into /proc", "error" in r)
finally:
    srv._READS_CALLER_PATHS = False

# ── per-member quota ────────────────────────────────────────────────────────
with db() as conn:
    held = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM attachments "
                        "WHERE channel = ? AND member_id = ?", (CH, ADA)).fetchone()[0]
real_quota = nmedia.MAX_MEMBER_ATTACH_BYTES
try:
    nmedia.MAX_MEMBER_ATTACH_BYTES = held + len(PNG) - 1
    before = counts()
    r = send(message="over quota", attachments=[{"data_base64": b64(PNG)}])
    check("quota: an image past the member's quota is refused",
          "error" in r and "quota" in r["error"])
    check("quota: the refused send posted nothing", counts() == before)
    nmedia.MAX_MEMBER_ATTACH_BYTES = held + len(PNG)
    r = send(message="exactly at quota", attachments=[{"data_base64": b64(PNG)}])
    check("quota: the same image fits once the quota allows it", r.get("ok") is True)
    # Another member's holdings are their own.
    r = json.loads(srv.nth_send(channel=CH, member_id=BEA, message="mine",
                                session_token=BEA_TOKEN,
                                attachments=[{"data_base64": b64(GIF)}]))
    check("quota: it is counted per member", r.get("ok") is True)
finally:
    nmedia.MAX_MEMBER_ATTACH_BYTES = real_quota

# ── another agent's poll returns the image ──────────────────────────────────
result = srv.nth_poll(channel=CH, member_id=CY, wait_seconds=0, session_token=CY_TOKEN)
check("poll: a message with images returns [payload, *image blocks]",
      isinstance(result, list) and len(result) >= 2)
payload = json.loads(result[0]) if isinstance(result, list) else {}
first = next((m for m in payload.get("messages", []) if m["id"] == PNG_MSG), {})
check("poll: the message lists its attachment as delivered",
      (first.get("attachments") or [{}])[0].get("delivered") is True)
images = [blk for blk in result[1:]] if isinstance(result, list) else []
check("poll: the first image block carries the sent bytes",
      bool(images) and images[0].data == PNG and images[0]._format == "png")

# ── a DM's image is for its participants ────────────────────────────────────
r = json.loads(srv.nth_dm(member_id=ADA, to="Bea", message="just for you",
                          session_token=ADA_TOKEN,
                          attachments=[{"data_base64": b64(PNG), "filename": "dm.png"}]))
check("dm: an image DM is accepted", r.get("ok") is True and r.get("attachments"))
DM_MSG = r.get("message_id")


def inbox_poll(member):
    out = srv.nth_poll(channel=AGENT_INBOX_CHANNEL, member_id=member, wait_seconds=0)
    body = json.loads(out[0] if isinstance(out, list) else out)
    return body, (out[1:] if isinstance(out, list) else [])


body, blocks = inbox_poll(BEA)
check("dm: the recipient receives the image",
      any(m["id"] == DM_MSG for m in body.get("messages", [])) and blocks
      and blocks[0].data == PNG)
body, blocks = inbox_poll(CY)
check("dm: a non-recipient receives neither the message nor the image",
      not any(m["id"] == DM_MSG for m in body.get("messages", [])) and not blocks)

# ── the dashboard serves it like a human upload ─────────────────────────────
check("web: the message event lists the attachment",
      web.attachments_for_message(db(), PNG_MSG)
      == [{"id": PNG_ATT, "mime": "image/png", "filename": "screen shot.png"}])
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
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/attachment/{PNG_ATT}",
                                timeout=5) as resp:
        check("web: the image is served inline with its sniffed type",
              resp.status == 200 and resp.headers.get("Content-Type") == "image/png"
              and "Content-Disposition" not in resp.headers and resp.read() == PNG)
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    hub.stop()

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
