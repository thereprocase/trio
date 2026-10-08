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
from datetime import datetime, timedelta, timezone
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
        proxy_server, _client, _hub = proxy.create_server("http://hub.example/sse")

    def call(name, **arguments):
        request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
            name=name, arguments=arguments))
        with patch.dict(os.environ, env, clear=True):
            return asyncio.run(proxy_server.request_handlers[types.CallToolRequest](request)).root

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

# ═══ Review fixes ═══════════════════════════════════════════════════════════
import struct  # noqa: E402
import nth_codex_runtime as ncodex  # noqa: E402
import nth_supervisor as nsup  # noqa: E402


def png(width, height, size=2048):
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    return head + b"\x00" * (size - len(head))


def send_items(items, **kw):
    return send(message=kw.pop("message", "m"), attachments=items, **kw)


# ── W-A: folders a path may come from ───────────────────────────────────────
allowed = FILES / "allowed"
outside = FILES / "outside"
allowed.mkdir()
outside.mkdir()
(allowed / "in.png").write_bytes(PNG)
(outside / "out.png").write_bytes(PNG)
os.symlink(outside / "out.png", allowed / "escape.png")
roots = [os.path.realpath(allowed)]

check("roots: by default the working directory and the temp directory",
      nmedia.attach_roots({}) == [os.path.realpath(os.getcwd()),
                                  os.path.realpath(tempfile.gettempdir())])
check("roots: NTH_ATTACH_ROOTS replaces the default; relative entries are dropped",
      nmedia.attach_roots({"NTH_ATTACH_ROOTS": os.pathsep.join([str(allowed), "rel/dir"])})
      == roots)
check("roots: a managed agent with no roots gets none",
      nmedia.attach_roots({"NTH_MANAGED_AGENT": "1"}) == []
      and nmedia.attach_roots({"NTH_MANAGED_AGENT": "1", "NTH_ATTACH_ROOTS": ""}) == [])
check("roots: a file inside a root is read",
      nmedia.read_local_file(str(allowed / "in.png"), 1 << 20, roots)[0] == PNG)
check("roots: a file outside every root is refused",
      raises(lambda: nmedia.read_local_file(str(outside / "out.png"), 1 << 20, roots)))
check("roots: a symlink inside a root that leads outside is refused",
      raises(lambda: nmedia.read_local_file(str(allowed / "escape.png"), 1 << 20, roots)))
check("roots: with no roots no path is read",
      raises(lambda: nmedia.read_local_file(str(allowed / "in.png"), 1 << 20, [])))

# O_NOFOLLOW: a link that is still a link at open time (here: resolution is
# skipped, as in a swap after the checks) is refused by the open itself.
os.symlink(allowed / "in.png", allowed / "late-link.png")
_realpath = nmedia.os.path.realpath
try:
    nmedia.os.path.realpath = lambda p: os.path.abspath(p)
    check("nofollow: a symlink at open time is refused",
          raises(lambda: nmedia.read_local_file(str(allowed / "late-link.png"), 1 << 20, roots)))
finally:
    nmedia.os.path.realpath = _realpath

# The hub's managed agents: their server is started with the marker and the
# agent's own folder, or no folder at all.
cfg = json.loads(nsup.build_mcp_config("/srv/nth_server.py", attach_roots=["/work/agent"]))
check("managed: the MCP config marks the server and names the agent's folder",
      cfg["mcpServers"]["nth-trio"]["env"] == {"NTH_MANAGED_AGENT": "1",
                                               "NTH_ATTACH_ROOTS": "/work/agent"})
check("managed: the hub passes the agent's cwd, or no root without one",
      json.loads(web.build_mcp_config_for_hub("/work/a"))["mcpServers"]["nth-trio"]["env"]
      ["NTH_ATTACH_ROOTS"] == "/work/a"
      and json.loads(web.build_mcp_config_for_hub(""))["mcpServers"]["nth-trio"]["env"]
      == {"NTH_MANAGED_AGENT": "1", "NTH_ATTACH_ROOTS": ""})
_saved_cmd = os.environ.pop("TRIO_CODEX_CMD", None)
try:
    codex_argv = ncodex.build_app_server_argv("/srv/nth_server.py", "py3")
finally:
    if _saved_cmd is not None:
        os.environ["TRIO_CODEX_CMD"] = _saved_cmd
check("managed: the shared Codex server is marked and gets no root",
      'mcp_servers.nth-trio.env.NTH_MANAGED_AGENT="1"' in codex_argv
      and 'mcp_servers.nth-trio.env.NTH_ATTACH_ROOTS=""' in codex_argv)
srv._READS_CALLER_PATHS = True
try:
    with patch.dict(os.environ, {"NTH_MANAGED_AGENT": "1", "NTH_ATTACH_ROOTS": ""}):
        r = send_items([{"path": str(allowed / "in.png")}])
    check("managed: a managed agent's stdio server with no roots reads no path",
          "error" in r and "data_base64" in r["error"])
    with patch.dict(os.environ, {"NTH_MANAGED_AGENT": "1", "NTH_ATTACH_ROOTS": str(allowed)}):
        r_in = send_items([{"path": str(allowed / "in.png")}])
        r_out = send_items([{"path": str(outside / "out.png")}])
    check("managed: it reads inside its own folder and nowhere else",
          r_in.get("ok") is True and "error" in r_out and "outside" in r_out["error"])
finally:
    srv._READS_CALLER_PATHS = False

# ── W-D and W-A in the frontend ─────────────────────────────────────────────
notes = FILES / "notes.png"
notes.write_bytes(b"SECRET_TOKEN=do-not-send\n")
check("frontend: a non-image file is refused before it is encoded",
      raises(lambda: nmedia.inline_local_paths([{"path": str(notes)}])))
if "call" in globals():
    def hub_calls():
        return [p for m, p in remote.calls if m == "tools/call"]

    before_calls = len(hub_calls())
    result = call("quartet_send", channel=CH, member_id=ADA, message="m",
                  attachments=[{"path": str(notes)}])
    check("proxy: a non-image file never reaches the hub",
          "error" in json.loads(result.content[0].text) and len(hub_calls()) == before_calls)
    env["NTH_ATTACH_ROOTS"] = str(allowed)
    try:
        result = call("quartet_send", channel=CH, member_id=ADA, message="m",
                      attachments=[{"path": str(outside / "out.png")}])
        check("proxy: a file outside NTH_ATTACH_ROOTS never reaches the hub",
              "outside" in json.loads(result.content[0].text).get("error", "")
              and len(hub_calls()) == before_calls)
        call("quartet_send", channel=CH, member_id=ADA, message="m",
             attachments=[{"path": str(allowed / "in.png")}])
        check("proxy: a file inside NTH_ATTACH_ROOTS is forwarded",
              len(hub_calls()) == before_calls + 1)
    finally:
        env.pop("NTH_ATTACH_ROOTS", None)

# ── W-B: sizes an agent may send, and what a model receives ─────────────────
gif_hdr = b"GIF89a" + struct.pack("<HH", 640, 480) + b"\x00" * 64
webp_x = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x0a\x00\x00\x00\x00\x00\x00\x00" \
    + (1919).to_bytes(3, "little") + (1079).to_bytes(3, "little")
webp_l = b"RIFF\x00\x00\x00\x00WEBPVP8L\x00\x00\x00\x00\x2f" \
    + ((99) | (49 << 14)).to_bytes(4, "little")
webp_lossy = b"RIFF\x00\x00\x00\x00WEBPVP8 \x00\x00\x00\x00" + b"\x00\x00\x00\x9d\x01\x2a" \
    + struct.pack("<HH", 320, 200)
jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 600, 800) + b"\x00" * 16)
check("dimensions: PNG, GIF, WebP (VP8X, VP8L, VP8) and JPEG headers",
      nmedia.image_dimensions(png(1200, 900)) == (1200, 900)
      and nmedia.image_dimensions(gif_hdr) == (640, 480)
      and nmedia.image_dimensions(webp_x) == (1920, 1080)
      and nmedia.image_dimensions(webp_l) == (100, 50)
      and nmedia.image_dimensions(webp_lossy) == (320, 200)
      and nmedia.image_dimensions(jpeg) == (800, 600))
check("dimensions: an unreadable header gives None", nmedia.image_dimensions(b"\xff\xd8\xff") is None)

r = send_items([{"data_base64": b64(png(10, 10, 10 * 1024 * 1024 + 1))}])
check("cap: an agent image over 10 MB is refused under a 25 MB upload cap",
      "error" in r and str(10 * 1024 * 1024) in r["error"] and nmedia.MAX_UPLOAD_BYTES > 10 * 1024 * 1024)
nine = b64(png(10, 10, 9 * 1024 * 1024))
before = counts()
r = send_items([{"data_base64": nine}] * 3)
check("cap: one message's attachments may total 25 MB at most",
      "error" in r and "total" in r["error"] and counts() == before)
big_a = (FILES / "big-a.png"); big_a.write_bytes(png(10, 10, 9 * 1024 * 1024))
check("cap: paths count against the per-message total too",
      raises(lambda: nmedia.inline_local_paths([{"path": str(big_a)}] * 3)))

# Fresh reader so the poll below holds only these messages.
d = json.loads(srv.nth_connect(summary="model reader", name="Dee", channel=CH))
DEE, DEE_TOKEN = d["member_id"], d["session_token"]
srv.nth_poll(channel=CH, member_id=DEE, wait_seconds=0, session_token=DEE_TOKEN)
body = srv.nth_poll(channel=CH, member_id=DEE, wait_seconds=0, session_token=DEE_TOKEN)
body = json.loads(body[0] if isinstance(body, list) else body)
for m in body.get("messages", []):
    srv.nth_ack(channel=CH, member_id=DEE, through_id=m["id"], session_token=DEE_TOKEN)

heavy = png(100, 100, 3_800_000)
wide = png(9000, 10)
three_mb = png(50, 50, 3_000_000)
r1 = send_items([{"data_base64": b64(heavy), "filename": "heavy.png"},
                 {"data_base64": b64(wide), "filename": "wide.png"}])
r2 = send_items([{"data_base64": b64(three_mb), "filename": "a.png"},
                 {"data_base64": b64(three_mb), "filename": "b.png"}])
check("poll setup: the large images are accepted for the dashboard",
      r1.get("ok") is True and r2.get("ok") is True)
out = srv.nth_poll(channel=CH, member_id=DEE, wait_seconds=0, session_token=DEE_TOKEN)
payload = json.loads(out[0] if isinstance(out, list) else out)
items = {a["filename"]: a for m in payload.get("messages", []) for a in m.get("attachments", [])}
blocks = out[1:] if isinstance(out, list) else []
check("poll: an image over 3.75 MB is withheld from the model",
      items.get("heavy.png", {}).get("delivered") is False
      and items["heavy.png"].get("reason") == "too_large_for_model")
check("poll: an image over 8000 px on a side is withheld from the model",
      items.get("wide.png", {}).get("delivered") is False
      and items["wide.png"].get("reason") == "too_large_for_model")
check("poll: about 5 MB of images per poll; the rest wait with a reason",
      items.get("a.png", {}).get("delivered") is True
      and items.get("b.png", {}).get("delivered") is False
      and items["b.png"].get("reason") == "poll_image_budget_spent"
      and sum(len(b.data) for b in blocks) <= 5_000_000)
check("poll: a poll carrying images says they are members' content",
      bool(blocks) and "content from channel members" in payload.get("images_note", ""))
srv.nth_ack(channel=CH, member_id=DEE, session_token=DEE_TOKEN,
            through_id=max(m["id"] for m in payload["messages"]))
send(message="words only")
quiet = srv.nth_poll(channel=CH, member_id=DEE, wait_seconds=0, session_token=DEE_TOKEN)
check("poll: a poll with no images carries no image note",
      "images_note" not in json.loads(quiet[0] if isinstance(quiet, list) else quiet))

# ── Note 4: a failed write after the files are written leaves no file ──────
t = json.loads(srv.nth_connect(summary="trigger", name="Tri", channel="trig-room"))
with db() as conn:
    conn.execute("CREATE TRIGGER fail_send BEFORE UPDATE OF updated_at ON channels "
                 "WHEN NEW.code = 'trig-room' BEGIN SELECT RAISE(ABORT, 'forced'); END")
files_before, rows_before = disk_files(), counts()
r = json.loads(srv.nth_send(channel="trig-room", member_id=t["member_id"], message="x",
                            session_token=t["session_token"],
                            attachments=[{"data_base64": b64(PNG)}]))
with db() as conn:
    conn.execute("DROP TRIGGER fail_send")
check("transaction: a write failing after the files are written is reported",
      "error" in r and "ok" not in r)
check("transaction: and leaves no message, row or file", counts() == rows_before
      and disk_files() == files_before)

# ── Note 7: agents' DM images are kept 30 days ─────────────────────────────
old_dm = json.loads(srv.nth_dm(member_id=ADA, to="Bea", message="old",
                               session_token=ADA_TOKEN, attachments=[{"data_base64": b64(GIF)}]))
new_dm = json.loads(srv.nth_dm(member_id=ADA, to="Bea", message="new",
                               session_token=ADA_TOKEN, attachments=[{"data_base64": b64(GIF)}]))
long_ago = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
with db() as conn:
    conn.execute("UPDATE attachments SET created_at = ? WHERE id = ?",
                 (long_ago, old_dm["attachments"][0]["id"]))
    old_path = conn.execute("SELECT path FROM attachments WHERE id = ?",
                            (old_dm["attachments"][0]["id"],)).fetchone()[0]
    # A person's DM upload of the same age keeps its current lifetime.
    conn.execute("INSERT INTO attachments (channel, message_id, member_id, mime, filename, "
                 "bytes, path, created_at) VALUES (?, ?, '_op_l_person', 'image/gif', 'p.gif', "
                 "1, '', ?)", (AGENT_INBOX_CHANNEL, old_dm["message_id"], long_ago))
stats = web.sweep_attachments(srv.DB_PATH, force=True)
with db() as conn:
    left = {r[0] for r in conn.execute("SELECT member_id || ':' || created_at FROM attachments "
                                       "WHERE channel = ?", (AGENT_INBOX_CHANNEL,))}
    ids = {r[0] for r in conn.execute("SELECT id FROM attachments WHERE channel = ?",
                                      (AGENT_INBOX_CHANNEL,))}
check("retention: an agent's DM image older than 30 days is swept, file and row",
      stats.get("dm_retention") == 1 and old_dm["attachments"][0]["id"] not in ids
      and not Path(old_path).exists())
check("retention: a recent agent DM image stays", new_dm["attachments"][0]["id"] in ids)
check("retention: a person's DM upload stays", f"_op_l_person:{long_ago}" in left)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
