"""Real still previews, bounded decoding/cache, and HTTP visibility parity."""
import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

tmp = tempfile.TemporaryDirectory(prefix="nth-preview-test-")
os.environ["NTH_HOME"] = tmp.name
os.environ["NTH_QUIET"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from PIL import Image
import nth_media as media
import nth_server as srv
import nth_web as web

root = Path(tmp.name)
srv.DB_DIR = root
srv.DB_PATH = root / "nth.db"
srv.ATTACH_DIR = root / "attachments"
web.ATTACH_DIR = srv.ATTACH_DIR
sender = json.loads(srv.nth_connect(channel="previews", name="Ada", summary="test"))
pat = web.OperatorIdentity("_op_g_pat", "Pat", web.IDENTITY_SOURCE_GUEST)
outsider = web.OperatorIdentity("_op_g_quin", "Quin", web.IDENTITY_SOURCE_GUEST)
pending = web.OperatorIdentity("_op_p_pending", "", web.IDENTITY_SOURCE_PENDING)
with sqlite3.connect(srv.DB_PATH) as db:
    web.ensure_operator_row(db, "previews", pat)

raw = io.BytesIO()
Image.new("RGB", (1200, 800), "blue").save(raw, format="GIF", save_all=True,
    append_images=[Image.new("RGB", (1200, 800), "red")], duration=100, loop=0)
original = raw.getvalue()
sent = json.loads(srv.nth_send(channel="previews", member_id=sender["member_id"],
    session_token=sender["session_token"], message="private animation",
    attachments=[{"data_base64": base64.b64encode(original).decode(), "filename":"suspension-travel.gif"}]))
aid = sent["attachments"][0]["id"]
with sqlite3.connect(srv.DB_PATH) as db:
    db.execute("UPDATE messages SET recipients=? WHERE id=?", (json.dumps([pat.member_id]), sent["message_id"]))
    path = Path(db.execute("SELECT path FROM attachments WHERE id=?", (aid,)).fetchone()[0])

with path.open("rb") as f:
    preview = media.chat_image_preview(f)
with Image.open(io.BytesIO(preview)) as im:
    assert im.format == "JPEG" and max(im.size) <= 960
    assert getattr(im, "n_frames", 1) == 1
    r,g,b = im.getpixel((100,100)); assert b > 200 and r < 30
assert path.read_bytes() == original
with patch("PIL.Image.open", side_effect=AssertionError("cache decoded twice")):
    with path.open("rb") as f: assert media.chat_image_preview(f) == preview
print("PASS first GIF frame, fit, still encoding, original preservation, cache")

exif = Image.Exif(); exif[274] = 6
oriented = root / "oriented.jpg"
Image.new("RGB", (40,20), "green").save(oriented, exif=exif)
with oriented.open("rb") as f: payload = media.chat_image_preview(f)
with Image.open(io.BytesIO(payload)) as im: assert im.size == (20,40)
print("PASS EXIF orientation")

huge = root / "too-many-pixels.png"
Image.new("RGB", (101,100), "white").save(huge)
with patch.object(media, "CHAT_PREVIEW_MAX_PIXELS", 10000):
    try:
        with huge.open("rb") as f: media.chat_image_preview(f)
    except ValueError as e: assert str(e) == "preview_pixel_limit"
    else: raise AssertionError("pixel limit bypassed")
print("PASS pixel limit before decode")

class Handler(web.NthWebHandler):
    channel = None
    landing_mode = True
    db_path = srv.DB_PATH
    def _resolve_identity(self):
        ident = {"pat":pat, "quin":outsider, "pending":pending}[self.headers.get("X-Viewer","quin")]
        return None,ident,False

server = web.QuietThreadingHTTPServer(("127.0.0.1",0), Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
def get(viewer, query="preview=1", channel="previews"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/attachment/{aid}?channel={channel}&{query}", headers={"X-Viewer":viewer})
    try:
        with urllib.request.urlopen(req,timeout=5) as r: return r.status,r.headers,r.read()
    except urllib.error.HTTPError as e: return e.code,e.headers,e.read()
try:
    status,headers,body = get("pat")
    assert status == 200 and body == preview and headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"].startswith("private,")
    status,headers,body = get("pat", query="")
    assert status == 200 and body == original and headers["Content-Type"] == "image/gif"
    assert get("quin")[0] == 404
    assert get("pending")[0] == 403
    assert get("pat",channel="wrong-room")[0] == 404
    print("PASS real HTTP preview/original + private, pending, wrong-channel denial")
finally:
    server.shutdown(); server.server_close()
tmp.cleanup()
