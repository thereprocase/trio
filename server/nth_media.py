"""Shared rules for rich content: attachment types and storage, file reads on an
agent's machine, and burner pages.

Standard library only. Three processes import it:

  * nth_web (the dashboard) sniffs and stores human uploads, serves attachments
    and pages, and sweeps what has expired;
  * nth_server (the channel tools, local stdio or the Quartet hub) stores the
    attachments and pages agents send;
  * nth_quartet_proxy (the Quartet frontend on the agent's machine) reads the
    files an agent names and forwards their bytes, because the hub cannot see
    that machine's disk.

Every path read, in the local server and in the frontend alike, goes through
read_local_file: the same allowed folders, file checks and size caps, and the
same image-type check before any byte leaves the machine. The hub then sniffs
the bytes again, as it does for every attachment, and applies the quota.
"""
import base64
import binascii
import os
import re
import secrets
import sqlite3
import stat
import struct
import tempfile
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
# kept until its channel goes (agents' DM images: DM_AGENT_ATTACH_RETENTION_DAYS),
# so this quota is the only bound on that right.
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


# ── Image dimensions ──────────────────────────────────────────────────────

_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from a PNG, GIF, WebP or JPEG header, or None when the
    header cannot be read. Reads the header only; the image is never decoded."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                if len(data) < 30:
                    return None
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
            elif chunk == b"VP8L" and len(data) >= 25 and data[20:21] == b"\x2f":
                bits = int.from_bytes(data[21:25], "little")
                w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                w, h = struct.unpack("<HH", data[26:30])
                w, h = w & 0x3FFF, h & 0x3FFF
            else:
                return None
        elif data[:3] == b"\xff\xd8\xff":
            return _jpeg_dimensions(data)
        else:
            return None
    except struct.error:
        return None
    return (w, h) if w > 0 and h > 0 else None


# Bounds on the header walk: real files have a few dozen segments and a few
# fill bytes, and this runs inside a send's write transaction.
_JPEG_MAX_SEGMENTS = 10000
_JPEG_MAX_FILL = 64 * 1024


def _jpeg_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    i, size, fill = 2, len(data), 0
    for _ in range(_JPEG_MAX_SEGMENTS):
        while i < size and data[i] == 0xFF and i + 1 < size and data[i + 1] == 0xFF:
            i += 1                  # fill bytes
            fill += 1
            if fill > _JPEG_MAX_FILL:
                return None
        if i + 4 > size or data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in _JPEG_SOF:
            if i + 9 > size:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h) if w > 0 and h > 0 else None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            return None
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


# ── Agent attachments ─────────────────────────────────────────────────────

# Per-file cap for an image an agent sends. Lower than the dashboard's upload
# cap because every byte is meant for a model's context on poll.
MAX_AGENT_ATTACH_BYTES = 10 * 1024 * 1024
# All attachments of one message together.
MAX_MESSAGE_ATTACH_BYTES = 25 * 1024 * 1024

# Folders a `path` attachment may come from, as an os.pathsep list. Unset, the
# reading process's working directory (the agent's project) and the system
# temp directory. A hub-managed agent's server is started with this set to the
# agent's own working directory, and with NTH_MANAGED_AGENT=1, so an unset or
# empty list there allows no path at all.
ATTACH_ROOTS_ENV = "NTH_ATTACH_ROOTS"
MANAGED_AGENT_ENV = "NTH_MANAGED_AGENT"


def agent_file_limit() -> int:
    """Largest single image an agent may send."""
    return min(MAX_UPLOAD_BYTES, MAX_AGENT_ATTACH_BYTES)


def attach_roots(environ=None) -> List[str]:
    """Resolved folders a path attachment may come from (see ATTACH_ROOTS_ENV)."""
    env = os.environ if environ is None else environ
    raw = (env.get(ATTACH_ROOTS_ENV) or "").strip()
    if raw:
        entries = raw.split(os.pathsep)
    elif env.get(MANAGED_AGENT_ENV) == "1":
        return []
    else:
        entries = [os.getcwd(), tempfile.gettempdir()]
    roots = []
    for entry in entries:
        entry = os.path.expanduser(entry.strip())
        # A relative root would mean whatever the working directory happens to be.
        if entry and os.path.isabs(entry):
            roots.append(os.path.realpath(entry))
    return roots


class RichContentError(ValueError):
    """An attachment or page the caller must fix. The message is shown to the
    agent as the tool's error, so it says what to change."""


class PreparedAttachment(NamedTuple):
    data: bytes
    mime: str
    filename: str


# Kernel and device trees hold live and endless data (a process's memory map,
# a terminal, /dev/zero). A root such as / must still never reach them.
_REFUSED_ROOTS = ("/proc", "/dev", "/sys")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _under_refused_root(path: str) -> bool:
    norm = os.path.normpath(path)
    return any(_under(norm, root) for root in _REFUSED_ROOTS)


def read_local_file(path_text: Any, max_bytes: int,
                    roots: Optional[List[str]] = None) -> Tuple[bytes, str]:
    """Read one file an agent named on its own machine: (bytes, basename).

    Only a regular file, by absolute path (a leading ~ is expanded), inside one
    of `roots` (attach_roots() when None) once symlinks resolve, at most
    `max_bytes`. A path into /proc, /dev or /sys is refused as written and as
    resolved. The open uses O_NOFOLLOW where the platform has it, and the type
    is checked again on the open handle, so a file swapped for a link, FIFO or
    device after the checks is still refused."""
    if not isinstance(path_text, str) or not path_text.strip():
        raise RichContentError("attachment path must be a non-empty string")
    expanded = os.path.expanduser(path_text.strip())
    if not os.path.isabs(expanded):
        raise RichContentError(f"attachment path must be absolute: {path_text!r}")
    real = os.path.realpath(expanded)
    if _under_refused_root(expanded) or _under_refused_root(real):
        raise RichContentError(f"attachment path is under /proc, /dev or /sys: {path_text!r}")
    allowed = attach_roots() if roots is None else roots
    if not allowed:
        raise RichContentError(
            "this server attaches no files by path; send the image as `data_base64` "
            "with a `filename`")
    if not any(_under(real, root) for root in allowed):
        raise RichContentError(
            f"attachment path {path_text!r} is outside the folders files may be attached "
            f"from ({os.pathsep.join(allowed)}); copy it into one, or set {ATTACH_ROOTS_ENV}")
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
    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
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


_DATA_URL_PREFIX = re.compile(r"^data:[\w.+/-]*;base64,", re.IGNORECASE)


def _compact_base64(text: Any) -> str:
    if not isinstance(text, str):
        raise RichContentError("attachment `data_base64` must be a string")
    return "".join(_DATA_URL_PREFIX.sub("", text.strip(), count=1).split())


def _decoded_size(compact: str) -> int:
    """Bytes `compact` decodes to, computed without decoding."""
    return len(compact) * 3 // 4 - (len(compact) - len(compact.rstrip("=")))


# Agents name what they attach so a reader can decide from the name alone
# whether to fetch it. These words say nothing about what an image shows.
_GENERIC_NAME_WORDS = {
    "image", "images", "img", "screenshot", "screenshots", "screen", "shot", "snapshot",
    "capture", "screencap", "untitled", "file", "photo", "pic", "picture", "download",
    "clipboard", "paste", "pasted", "attachment", "upload", "output", "out", "temp", "tmp",
    "test", "new", "copy", "scan", "figure", "fig", "graph", "chart", "plot", "diagram",
    "export", "frame", "render", "result", "at", "am", "pm",
}
_HEXISH = re.compile(r"[0-9a-f]{8}(-?[0-9a-f]{4}){3}-?[0-9a-f]{12}|[0-9a-f]{8,}")
NAME_EXAMPLE = "headlights-option-A-segmented.png"


def require_descriptive_name(name: str) -> str:
    """The name, when it says what the image shows; else RichContentError.
    Generic words (image, screenshot, untitled, file, ...), dates, numbers, a
    bare hash or a UUID leave nothing descriptive, so such a name is refused."""
    stem = Path(name.strip()).stem.lower() if name and name.strip() else ""
    words = [w for w in re.findall(r"[a-z]+", stem)
             if len(w) >= 3 and w not in _GENERIC_NAME_WORDS]
    if not stem or _HEXISH.fullmatch(stem) or not words:
        raise RichContentError(
            f"attachment name {name!r} does not describe the image; pass a `filename` "
            f"that says what it shows, such as {NAME_EXAMPLE!r}")
    return name.strip()


def _items(items: Any) -> List[Dict[str, Any]]:
    """The attachment list, shape-checked and normalised: path items keep
    `path`, data items carry their base64 without whitespace or a data: URL
    prefix as `data`, and every item carries its descriptive `filename` (the
    given one, else the path's basename). Names and the base64 items' decoded
    total are checked here, before anything is read or decoded; path items
    count against the per-message cap as they are read."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise RichContentError("attachments must be a list of objects")
    if len(items) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise RichContentError(
            f"too many attachments ({len(items)}); the limit is {MAX_ATTACHMENTS_PER_MESSAGE}")
    out, total = [], 0
    for item in items:
        if not isinstance(item, dict):
            raise RichContentError("each attachment must be an object with `path` or `data_base64`")
        has_path = bool(item.get("path"))
        has_data = bool(item.get("data_base64"))
        if has_path == has_data:
            raise RichContentError("each attachment needs exactly one of `path` or `data_base64`")
        name = item.get("filename", "")
        if not isinstance(name, str):
            raise RichContentError("attachment `filename` must be a string")
        if has_path:
            given = item["path"] if isinstance(item["path"], str) else ""
            name = require_descriptive_name(name or os.path.basename(given.strip()))
            out.append({"path": item["path"], "filename": name})
            continue
        if not name.strip():
            raise RichContentError(
                "each `data_base64` attachment needs a `filename` that says what the "
                f"image shows, such as {NAME_EXAMPLE!r}")
        name = require_descriptive_name(name)
        compact = _compact_base64(item["data_base64"])
        total += _decoded_size(compact)
        if total > MAX_MESSAGE_ATTACH_BYTES:
            raise RichContentError(
                f"the attachments in one message total more than {MAX_MESSAGE_ATTACH_BYTES} bytes")
        out.append({"data": compact, "filename": name})
    return out


def _require_image(data: bytes, shown_name: str) -> str:
    mime = sniff_image_mime(data)
    if mime not in AGENT_ATTACH_MIME:
        raise RichContentError(
            f"{(shown_name or 'attachment')!r} is not a PNG, JPEG, GIF or WebP image; "
            "agents may attach those four types. Put text or HTML in a message or a page instead.")
    return mime


class _Budget:
    """Bytes left under the per-message cap, spent as files are read."""

    def __init__(self, items):
        # Base64 items were totalled by _items; paths spend what is left.
        self.left = MAX_MESSAGE_ATTACH_BYTES - sum(
            _decoded_size(i["data"]) for i in items if "data" in i)

    def spend(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise RichContentError(
                f"the attachments in one message total more than {MAX_MESSAGE_ATTACH_BYTES} bytes")


def inline_local_paths(items: Any, roots: Optional[List[str]] = None) -> Optional[List[Dict[str, Any]]]:
    """Turn every `path` item into `data_base64` + `filename`, for a frontend
    that forwards to a hub which cannot read this machine. Each file passes
    read_local_file and must be an image before it is encoded, so a refused
    file never leaves the machine. `data_base64` items pass through for the
    hub to check. Raises RichContentError on the first bad item, so nothing is
    sent when any file is refused."""
    if items is None:
        return None
    normal = _items(items)
    budget = _Budget(normal)
    out = []
    for item in normal:
        if "path" in item:
            data, _base = read_local_file(item["path"], agent_file_limit(), roots)
            budget.spend(len(data))
            _require_image(data, item["filename"])
            out.append({"data_base64": base64.b64encode(data).decode("ascii"),
                        "filename": item["filename"]})
        else:
            out.append({"data_base64": item["data"], "filename": item["filename"]})
    return out


def _decode_base64(compact: str, max_bytes: int) -> bytes:
    if _decoded_size(compact) > max_bytes:
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
                        roots: Optional[List[str]] = None) -> List[PreparedAttachment]:
    """Validate an agent's attachment list into sniffed, named byte blobs.

    `read_paths` is True only where the caller and this process share a machine
    and a user (the local stdio server). A hub sets it False: a path there would
    name a file on the hub, so it is refused with directions to send bytes."""
    normal = _items(items)
    budget = _Budget(normal)
    limit = agent_file_limit()
    prepared = []
    for item in normal:
        if "path" in item:
            if not read_paths:
                raise RichContentError(
                    "this server cannot read files on your machine; send the bytes as "
                    "`data_base64` with a `filename`. The Quartet frontend installed by "
                    "setup.py converts `path` items for you.")
            data, _base = read_local_file(item["path"], limit, roots)
            budget.spend(len(data))
            raw_name = item["filename"]
        else:
            data = _decode_base64(item["data"], limit)
            raw_name = item["filename"]
        mime = _require_image(data, raw_name)
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
            dims = image_dimensions(p.data) or (None, None)
            cur = db.execute(
                "INSERT INTO attachments "
                "(channel, message_id, member_id, mime, filename, width, height, bytes, "
                " path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
                (channel, message_id, member_id, p.mime, p.filename, dims[0], dims[1],
                 len(p.data), now))
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


# ── Images fetched by a model ─────────────────────────────────────────────
# Agents fetch images one at a time with the image tool; poll only lists them.
# An image block the model API refuses stays in the agent's history and fails
# every later request, so the tool returns only images the API accepts:
#   * at most 3.75 MB raw, which is 5 MB once base64-encoded, the API's
#     per-image limit;
#   * at most 2000 pixels on a side, the API's limit for a request carrying
#     more than 20 images, which a long session with images reaches;
#   * with dimensions read from the header: an image whose size cannot be
#     read is one the API may refuse.
# The API also caps a whole request at 32 MB, which a long session that
# fetches many images can still reach; that total is the receiving client's
# to manage, and the hub cannot see it.
MAX_MODEL_IMAGE_BYTES = 3_750_000
MAX_MODEL_IMAGE_SIDE = 2000
TOO_LARGE_FOR_MODEL = "too_large_for_model"
UNREADABLE_IMAGE = "unreadable_image"


def model_image_refusal(data: bytes) -> Optional[str]:
    """None when an image may go to a model as a block, else the reason."""
    if len(data) > MAX_MODEL_IMAGE_BYTES:
        return TOO_LARGE_FOR_MODEL
    dims = image_dimensions(data)
    if dims is None or min(dims) <= 0:
        return UNREADABLE_IMAGE
    if max(dims) > MAX_MODEL_IMAGE_SIDE:
        return TOO_LARGE_FOR_MODEL
    return None


# ── Retention of DM images from agents ───────────────────────────────────
# Every DM shares one transport channel, so an agent's per-channel quota there
# would be a lifetime allowance. Images agents send in DMs are therefore kept
# this many days, then swept, which returns their bytes to the quota.
DM_AGENT_ATTACH_RETENTION_DAYS = 30


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
    """Create the pages table if missing. The HTML lives in the row, as the
    LAST column: a page runs to 512 KB and spills into overflow pages, and
    SQLite reads a row's columns in order, so every column a lookup, count or
    sweep needs sits before it and is read without touching the HTML."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS pages ("
        " id TEXT PRIMARY KEY,"
        " channel TEXT NOT NULL,"
        " message_id INTEGER,"
        " member_id TEXT NOT NULL,"
        " title TEXT NOT NULL,"
        " bytes INTEGER NOT NULL,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL,"
        " html TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_channel ON pages(channel, member_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_message ON pages(message_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_pages_expires ON pages(expires_at)")


class PageDraft(NamedTuple):
    title: str
    html: str
    ttl_hours: float


def _utf8_len(text: str, what: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise RichContentError(f"page {what} is not valid Unicode text (it holds a lone surrogate)")


def validate_page(title: Any, html: Any, ttl_hours: Any) -> PageDraft:
    """A page ready to store, or RichContentError naming what to fix."""
    if not isinstance(title, str) or not title.strip():
        raise RichContentError("a page needs a title")
    clean_title = " ".join(title.split())
    _utf8_len(clean_title, "title")
    if len(clean_title) > MAX_PAGE_TITLE:
        raise RichContentError(f"page title is longer than {MAX_PAGE_TITLE} characters")
    if not isinstance(html, str) or not html.strip():
        raise RichContentError("a page needs html")
    size = _utf8_len(html, "html")
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
    write transaction, after the message INSERT. Deletes expired pages first
    (one indexed DELETE) so the live-page count counts only pages that can
    still be opened."""
    moment = now or datetime.now(timezone.utc)
    ensure_pages_table(db)
    sweep_expired_pages(db, moment)
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
        "INSERT INTO pages (id, channel, message_id, member_id, title, bytes, "
        "created_at, expires_at, html) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (page_id, channel, message_id, member_id, draft.title,
         _utf8_len(draft.html, "html"), utc_stamp(moment), expires_at, draft.html))
    return {"id": page_id, "title": draft.title, "path": page_path(page_id),
            "expires_at": expires_at}


def sweep_expired_pages(db: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Delete pages past their expiry: one DELETE on the expires_at index."""
    stamp = utc_stamp(now or datetime.now(timezone.utc))
    try:
        cur = db.execute("DELETE FROM pages WHERE expires_at <= ?", (stamp,))
    except sqlite3.OperationalError:
        return 0
    return cur.rowcount or 0


def sweep_orphan_pages(db: sqlite3.Connection) -> int:
    """Delete pages whose channel no longer exists. The distinct channels are
    read from the (channel, member_id) index, which never touches the page
    rows or their HTML; then each missing channel's pages are deleted."""
    try:
        gone = [r[0] for r in db.execute(
            "SELECT DISTINCT p.channel FROM pages p "
            "WHERE NOT EXISTS (SELECT 1 FROM channels c WHERE c.code = p.channel)")]
    except sqlite3.OperationalError:
        return 0
    return sum(purge_channel_pages(db, ch) for ch in gone)


def sweep_pages(db: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Both page sweeps; the number of pages deleted."""
    return sweep_expired_pages(db, now) + sweep_orphan_pages(db)


def purge_channel_pages(db: sqlite3.Connection, channel: str) -> int:
    """Delete every page of one channel (the channel ended or was removed)."""
    try:
        cur = db.execute("DELETE FROM pages WHERE channel = ?", (channel,))
    except sqlite3.OperationalError:
        return 0
    return cur.rowcount or 0


def purge_message_page(db: sqlite3.Connection, message_id: int) -> int:
    """Delete the page a message announced (the message was retracted)."""
    try:
        cur = db.execute("DELETE FROM pages WHERE message_id = ?", (message_id,))
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
