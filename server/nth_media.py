"""Shared rules for rich content: attachment types and storage, file reads on an
agent's machine, and burner pages.

Standard library only. Three processes import it, and each needs the same answer:

  * nth_web (the dashboard) sniffs and stores human uploads, serves attachments
    and pages, and sweeps what has expired;
  * nth_server (the channel tools, local stdio or the Quartet hub) stores the
    attachments and pages agents send;
  * nth_quartet_proxy (the Quartet frontend on the agent's machine) reads the
    files an agent names and forwards their bytes, because the hub cannot see
    that machine's disk.

Keeping the rules here means a file the web upload refuses is refused for an
agent too, and the frontend reads a local path under exactly the checks the
local server applies.
"""
import base64
import binascii
import json
import os
import re
import secrets
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


def env_bytes(name: str, default: int) -> int:
    """A positive byte count from the environment; the default when unset or malformed."""
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# ── Attachment types and limits ───────────────────────────────────────────

MAX_UPLOAD_BYTES = env_bytes("NTH_UPLOAD_MAX_BYTES", 25 * 1024 * 1024)  # hard cap per file
# Total attachment bytes one member may hold in one channel. The per-file cap
# bounds a single request; this bounds the SUM, so an identity allowed to attach
# cannot fill the disk one legal file at a time. Anything linked to a message is
# kept until its channel goes, so this quota is the only bound on that right.
MAX_MEMBER_ATTACH_BYTES = env_bytes("NTH_ATTACH_QUOTA_BYTES", 200 * 1024 * 1024)

# Images every browser renders, shown inline in the conversation.
ALLOWED_IMAGE_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}
# Everything a person may attach from the dashboard. The type always comes from
# the bytes (sniff_attachment_mime), never from the client. Anything outside
# ALLOWED_IMAGE_MIME is served as a download and never rendered by the page.
# Office documents are ZIP containers and arrive as application/zip under their
# own filename; phones send photos as HEIC/HEIF, which only some browsers show.
ALLOWED_ATTACH_MIME = {
    **ALLOWED_IMAGE_MIME,
    "image/heic": ".heic", "image/heif": ".heif",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/plain": ".txt",
}
# Extensions a download may keep, by sniffed type. The type comes from the bytes and
# the name from the client, so without this a text file could be saved as run.bat or
# Invoice.hta and run on a double-click. Any other extension gets the type's own added.
ATTACH_NAME_EXTENSIONS = {
    "image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}, "image/gif": {".gif"},
    "image/webp": {".webp"}, "image/heic": {".heic"}, "image/heif": {".heif", ".heic"},
    "application/pdf": {".pdf"},
    "application/zip": {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"},
    "text/plain": {".txt", ".csv", ".log", ".md"},
}

# What an agent may attach: the four inline image types. A `path` item is read
# by a process acting outside the agent client's own file permissions (the local
# server or the Quartet frontend), so the set it can pull into a channel is kept
# to formats identified by an image header. A key, an .env file or a config
# file is plain text and never matches one. Images are also the only kind that
# reaches other agents as content (poll returns them as image blocks).
AGENT_ATTACH_MIME = ALLOWED_IMAGE_MIME
# Matches the dashboard's limit on one message.
MAX_ATTACHMENTS_PER_MESSAGE = 8

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1"}


def attachment_filename(raw_name: str, mime: str) -> str:
    """The stored name for an upload: sanitised, and ending in an extension the sniffed
    type allows, so what the operator saves is the kind of file its bytes are."""
    ext = ALLOWED_ATTACH_MIME[mime]
    name = re.sub(r"[^\w.\- ]", "_", raw_name)[:120].strip(" .") or ("file" + ext)
    if Path(name).suffix.lower() not in ATTACH_NAME_EXTENSIONS.get(mime, {ext}):
        name = name[:120 - len(ext)] + ext
    return name


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


def sniff_attachment_mime(data: bytes) -> Optional[str]:
    """Attachment MIME from the content, or None if the type is not accepted.
    Images first (sniff_image_mime), then PDF, ZIP, HEIC/HEIF, and plain text:
    UTF-8 with no NUL byte, the only type recognised by the absence of a header."""
    mime = sniff_image_mime(data)
    if mime:
        return mime
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "application/zip"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
    sample = data[:65536]
    if sample and b"\x00" not in sample:
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            # Only a sample that cut a longer file mid-character may end in a partial one.
            cut = len(data) > len(sample) and exc.reason == "unexpected end of data"
            if not cut:
                return None
        return "text/plain"
    return None


def channel_dir_name(channel: str) -> str:
    """The on-disk directory name for a channel's attachments."""
    return re.sub(r"[^\w.\-]", "_", channel or "")


def ensure_attachments_table(db: sqlite3.Connection) -> None:
    """Create the attachments table if missing. Both nth_server.get_db() and the
    dashboard call this, so either can run first against a fresh database."""
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


# ── Agent attachments ─────────────────────────────────────────────────────

class RichContentError(ValueError):
    """An attachment or page the caller must fix. The message is shown to the
    agent as the tool's error, so it says what to change."""


class PreparedAttachment(NamedTuple):
    data: bytes
    mime: str
    filename: str


# Kernel and device trees. Reading from them yields endless or live data (a
# process's memory map, a terminal, /dev/zero) rather than a file someone made.
_REFUSED_ROOTS = ("/proc", "/dev", "/sys")


def _under_refused_root(path: str) -> bool:
    norm = os.path.normpath(path)
    return any(norm == root or norm.startswith(root + os.sep) for root in _REFUSED_ROOTS)


def read_local_file(path_text: Any, max_bytes: int) -> Tuple[bytes, str]:
    """Read one file an agent named on its own machine: (bytes, basename).

    Only a regular file, by absolute path (a leading ~ is expanded), at most
    `max_bytes`. A path into /proc, /dev or /sys is refused both as written and
    after symlinks resolve, and the type is checked again on the open handle so
    a file swapped for a FIFO or device between the check and the read is still
    refused."""
    if not isinstance(path_text, str) or not path_text.strip():
        raise RichContentError("attachment path must be a non-empty string")
    expanded = os.path.expanduser(path_text.strip())
    if not os.path.isabs(expanded):
        raise RichContentError(f"attachment path must be absolute: {path_text!r}")
    real = os.path.realpath(expanded)
    if _under_refused_root(expanded) or _under_refused_root(real):
        raise RichContentError(f"attachment path is under /proc, /dev or /sys: {path_text!r}")
    try:
        st = os.stat(real)
    except OSError:
        raise RichContentError(f"attachment file not found: {path_text!r}")
    if not stat.S_ISREG(st.st_mode):
        raise RichContentError(f"attachment path is not a regular file: {path_text!r}")
    if st.st_size > max_bytes:
        raise RichContentError(
            f"attachment {os.path.basename(real)!r} is {st.st_size} bytes; "
            f"the limit is {max_bytes}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        fd = os.open(real, flags)
    except OSError as exc:
        raise RichContentError(f"attachment file could not be opened: {path_text!r} ({exc.strerror})")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RichContentError(f"attachment path is not a regular file: {path_text!r}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise RichContentError(
            f"attachment {os.path.basename(real)!r} is larger than the {max_bytes}-byte limit")
    return data, os.path.basename(real)


def _items(items: Any) -> List[Dict[str, Any]]:
    """The attachment list, shape-checked. None or [] is no attachments.

    Some clients send a list argument as its JSON text. The hub's tool layer
    parses that form itself, so the frontend accepts it too."""
    if items is None:
        return []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            raise RichContentError("attachments must be a list of objects")
    if not isinstance(items, list):
        raise RichContentError("attachments must be a list of objects")
    if len(items) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise RichContentError(
            f"too many attachments ({len(items)}); the limit is {MAX_ATTACHMENTS_PER_MESSAGE}")
    for item in items:
        if not isinstance(item, dict):
            raise RichContentError("each attachment must be an object with `path` or `data_base64`")
        has_path = bool(item.get("path"))
        has_data = bool(item.get("data_base64"))
        if has_path == has_data:
            raise RichContentError("each attachment needs exactly one of `path` or `data_base64`")
        if "filename" in item and not isinstance(item["filename"], str):
            raise RichContentError("attachment `filename` must be a string")
    return items


def inline_local_paths(items: Any, max_bytes: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
    """Turn every `path` item into `data_base64` + `filename`, for a frontend
    that forwards to a hub which cannot read this machine. `data_base64` items
    pass through unchanged. Raises RichContentError on the first bad item, so
    nothing is sent when any file cannot be read."""
    if items is None:
        return None
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    out = []
    for item in _items(items):
        if item.get("path"):
            data, base = read_local_file(item["path"], limit)
            out.append({"data_base64": base64.b64encode(data).decode("ascii"),
                        "filename": item.get("filename") or base})
        else:
            out.append(dict(item))
    return out


_DATA_URL_PREFIX = re.compile(r"^data:[\w.+/-]*;base64,", re.IGNORECASE)


def _decode_base64(text: Any, max_bytes: int) -> bytes:
    if not isinstance(text, str):
        raise RichContentError("attachment `data_base64` must be a string")
    compact = "".join(_DATA_URL_PREFIX.sub("", text.strip(), count=1).split())
    # Refuse before decoding: base64 is 4 characters per 3 bytes.
    if len(compact) > (max_bytes + 2) // 3 * 4:
        raise RichContentError(f"attachment is larger than the {max_bytes}-byte limit")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise RichContentError("attachment `data_base64` is not valid base64")
    if not data:
        raise RichContentError("attachment is empty")
    if len(data) > max_bytes:
        raise RichContentError(f"attachment is larger than the {max_bytes}-byte limit")
    return data


def prepare_attachments(items: Any, *, read_paths: bool,
                        max_bytes: Optional[int] = None) -> List[PreparedAttachment]:
    """Validate an agent's attachment list into sniffed, named byte blobs.

    `read_paths` is True only where the caller and this process share a machine
    and a user (the local stdio server). A hub sets it False: a path there would
    name a file on the hub, so it is refused with directions to send bytes."""
    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    prepared = []
    for item in _items(items):
        if item.get("path"):
            if not read_paths:
                raise RichContentError(
                    "this server cannot read files on your machine; send the bytes as "
                    "`data_base64` with a `filename`. The Quartet frontend installed by "
                    "setup.py converts `path` items for you.")
            data, base = read_local_file(item["path"], limit)
            raw_name = item.get("filename") or base
        else:
            data = _decode_base64(item["data_base64"], limit)
            raw_name = item.get("filename") or ""
        mime = sniff_image_mime(data)
        if mime not in AGENT_ATTACH_MIME:
            shown = raw_name or "attachment"
            raise RichContentError(
                f"{shown!r} is not a PNG, JPEG, GIF or WebP image; agents may attach "
                "those four types. Put text or HTML in a message or a page instead.")
        prepared.append(PreparedAttachment(data, mime, attachment_filename(raw_name, mime)))
    return prepared


def store_attachments(db: sqlite3.Connection, attach_root: Path, channel: str,
                      member_id: str, message_id: int,
                      prepared: List[PreparedAttachment], quota: int,
                      now: str) -> Tuple[List[Dict[str, Any]], List[Path]]:
    """Insert rows linked to `message_id` and write their files.

    Runs inside the caller's open write transaction, after its message INSERT,
    so the quota read below is serialized with every other writer. Returns
    (metadata, written file paths). The caller commits, or on any later failure
    rolls back and unlinks the returned paths. On failure here this unlinks its
    own files and raises RichContentError; the caller still rolls back."""
    if not prepared:
        return [], []
    ensure_attachments_table(db)
    used = db.execute(
        "SELECT COALESCE(SUM(bytes), 0) FROM attachments WHERE channel = ? AND member_id = ?",
        (channel, member_id)).fetchone()[0]
    adding = sum(len(p.data) for p in prepared)
    if used + adding > quota:
        raise RichContentError(
            f"attachment quota exceeded: you hold {used} bytes in this channel and "
            f"the limit is {quota}")
    chan_dir = Path(attach_root) / channel_dir_name(channel)
    written: List[Path] = []
    meta: List[Dict[str, Any]] = []
    try:
        chan_dir.mkdir(parents=True, exist_ok=True)
        for p in prepared:
            cur = db.execute(
                "INSERT INTO attachments "
                "(channel, message_id, member_id, mime, filename, bytes, path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                (channel, message_id, member_id, p.mime, p.filename, len(p.data), now))
            att_id = cur.lastrowid
            fpath = chan_dir / f"{att_id}{ALLOWED_ATTACH_MIME[p.mime]}"
            fpath.write_bytes(p.data)
            written.append(fpath)
            db.execute("UPDATE attachments SET path = ? WHERE id = ?", (str(fpath), att_id))
            meta.append({"id": att_id, "mime": p.mime, "filename": p.filename,
                         "bytes": len(p.data)})
    except (OSError, sqlite3.Error) as exc:
        unlink_quietly(written)
        raise RichContentError(f"attachment could not be stored ({type(exc).__name__})")
    return meta, written


def unlink_quietly(paths) -> None:
    for p in paths:
        try:
            Path(p).unlink()
        except OSError:
            pass


# ── Burner pages ──────────────────────────────────────────────────────────
# An agent posts a self-contained HTML page; the dashboard serves it at
# /pages/<id> to whoever can see the message that announced it, in a sandbox
# with an opaque origin, until it expires.

MAX_PAGE_BYTES = 512 * 1024
MAX_PAGE_TITLE = 120
DEFAULT_PAGE_TTL_HOURS = 24.0
MAX_PAGE_TTL_HOURS = 7 * 24.0
# Live pages one member may hold in one channel: 50 x 512 KB bounds the rows.
MAX_LIVE_PAGES_PER_MEMBER = 50
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
PAGE_PATH_PREFIX = "/pages/"

# `sandbox allow-scripts` without allow-same-origin gives the page an opaque
# origin: its scripts run, and it can reach neither the dashboard's cookies,
# storage nor API. connect-src 'none' stops it calling out; forms, popups and
# top navigation stay blocked because the sandbox does not allow them.
PAGE_CSP = ("sandbox allow-scripts; default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'none'; "
            "frame-ancestors 'self'")
PAGE_HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    ("Content-Security-Policy", PAGE_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "private, no-store"),
)


def utc_stamp(moment: datetime) -> str:
    """Second-precision UTC ISO time; these compare correctly as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def page_path(page_id: str) -> str:
    return PAGE_PATH_PREFIX + page_id


def ensure_pages_table(db: sqlite3.Connection) -> None:
    """Create the pages table if missing. The HTML lives in the row: pages are
    small, capped and short-lived, so deleting a row is the whole cleanup."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS pages ("
        " id TEXT PRIMARY KEY,"
        " channel TEXT NOT NULL,"
        " message_id INTEGER,"
        " member_id TEXT NOT NULL,"
        " title TEXT NOT NULL,"
        " html TEXT NOT NULL,"
        " bytes INTEGER NOT NULL,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_channel ON pages(channel, member_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_message ON pages(message_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_expires ON pages(expires_at)")


class PageDraft(NamedTuple):
    title: str
    html: str
    ttl_hours: float


def validate_page(title: Any, html: Any, ttl_hours: Any) -> PageDraft:
    """A page ready to store, or RichContentError naming what to fix."""
    if not isinstance(title, str) or not title.strip():
        raise RichContentError("a page needs a title")
    clean_title = " ".join(title.split())
    if len(clean_title) > MAX_PAGE_TITLE:
        raise RichContentError(f"page title is longer than {MAX_PAGE_TITLE} characters")
    if not isinstance(html, str) or not html.strip():
        raise RichContentError("a page needs html")
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise RichContentError(
            f"page html is {size} bytes; the limit is {MAX_PAGE_BYTES}. Inline images "
            "as small data: URLs or attach them to a message instead.")
    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError):
        raise RichContentError("ttl_hours must be a number")
    if not (ttl > 0) or ttl > MAX_PAGE_TTL_HOURS:
        raise RichContentError(
            f"ttl_hours must be more than 0 and at most {MAX_PAGE_TTL_HOURS:g}")
    return PageDraft(clean_title, html, ttl)


def insert_page(db: sqlite3.Connection, channel: str, member_id: str,
                message_id: int, draft: PageDraft,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Store a page linked to its announcing message. Runs inside the caller's
    write transaction, after the message INSERT. Sweeps expired pages first so
    the live-page count below counts only pages that can still be opened."""
    moment = now or datetime.now(timezone.utc)
    ensure_pages_table(db)
    sweep_pages(db, moment)
    live = db.execute(
        "SELECT COUNT(*) FROM pages WHERE channel = ? AND member_id = ?",
        (channel, member_id)).fetchone()[0]
    if live >= MAX_LIVE_PAGES_PER_MEMBER:
        raise RichContentError(
            f"you already have {live} live pages in this channel; the limit is "
            f"{MAX_LIVE_PAGES_PER_MEMBER}. Let some expire first.")
    page_id = secrets.token_urlsafe(24)
    expires_at = utc_stamp(moment + timedelta(hours=draft.ttl_hours))
    db.execute(
        "INSERT INTO pages (id, channel, message_id, member_id, title, html, bytes, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (page_id, channel, message_id, member_id, draft.title, draft.html,
         len(draft.html.encode("utf-8")), utc_stamp(moment), expires_at))
    return {"id": page_id, "title": draft.title, "path": page_path(page_id),
            "expires_at": expires_at}


def sweep_pages(db: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Delete expired pages and pages whose channel no longer exists. Returns
    the number deleted; a database without the table has nothing to sweep."""
    stamp = utc_stamp(now or datetime.now(timezone.utc))
    try:
        cur = db.execute(
            "DELETE FROM pages WHERE expires_at <= ? "
            "OR channel NOT IN (SELECT code FROM channels)", (stamp,))
    except sqlite3.OperationalError:
        return 0
    return cur.rowcount or 0


def purge_channel_pages(db: sqlite3.Connection, channel: str) -> int:
    """Delete every page of one channel (the channel ended or was removed)."""
    try:
        cur = db.execute("DELETE FROM pages WHERE channel = ?", (channel,))
    except sqlite3.OperationalError:
        return 0
    return cur.rowcount or 0


def page_for_message(db: sqlite3.Connection, msg_id: int) -> Optional[Dict[str, Any]]:
    """{id, title, path, expires_at} for the page a message announced, or None.
    Defensive: a database without the table answers None."""
    try:
        row = db.execute(
            "SELECT id, title, expires_at FROM pages WHERE message_id = ? LIMIT 1",
            (msg_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"id": row[0], "title": row[1], "path": page_path(row[0]), "expires_at": row[2]}


def page_expired(expires_at: str, now: Optional[datetime] = None) -> bool:
    return (expires_at or "") <= utc_stamp(now or datetime.now(timezone.utc))
