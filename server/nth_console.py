"""
Live console view of nth channel traffic — reads the SQLite DB directly,
no MCP, no Claude session.

Usage:
    python3 nth_console.py                      # follow all channels
    python3 nth_console.py -c MYCHAN            # follow one channel
    python3 nth_console.py -c MYCHAN -s 600     # show last 600s, then follow
    python3 nth_console.py --snapshot           # print current log and exit
    python3 nth_console.py --no-color           # disable ANSI colour

Windows: use `py` instead of `python3`. Output uses ANSI escapes; on
Windows 10+ cmd/PowerShell/Terminal VT processing is enabled
automatically, and colour is disabled when stdout is not a TTY.

Messages include user posts and server-generated task lifecycle events
(`[claimed #N] …`, `[done #N] …`, etc.), so the feed is a full trace of
channel activity.
"""
import argparse
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "nth" / "nth.db"
POLL_INTERVAL = 0.5
SYSTEM_PREFIXES = ("[claimed ", "[done ", "[cancelled ", "[released ",
                   "[retracted ", "[joined ", "[left ", "[ended ",
                   "[locked ", "[unlocked ", "[status ", "[pinned ")


class Colour:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    TIME = "\x1b[90m"            # bright black / grey
    CHANNEL = "\x1b[36m"         # cyan
    AUTHOR = "\x1b[32m"          # green
    MENTION = "\x1b[33m"         # yellow
    SYSTEM = "\x1b[90m"          # grey for server lifecycle messages
    RETRACTED = "\x1b[31m"       # red for retractions

    @classmethod
    def disable(cls):
        for name in list(vars(cls)):
            if name.isupper():
                setattr(cls, name, "")


def enable_windows_vt():
    """On Windows 10+, flip the console into VT-processing mode so ANSI
    escapes render. No-op elsewhere, and no-op if unavailable."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)      # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(handle, mode.value | 0x0001 | 0x0004)
    except Exception:
        pass


def force_utf8_stdout():
    """Windows default stdout encoding (cp1252) crashes on characters
    like ≤, ≥, em-dashes, emoji — anything outside the codepage. Peer
    messages routinely contain those. Reconfigure once at startup so
    the output stream is UTF-8 with replacement for anything truly
    unencodable. No-op on Python < 3.7 or if stdout was overridden."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def fmt_time(iso_ts):
    try:
        return datetime.fromisoformat(iso_ts).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"


def parse_mentions(raw):
    if not raw:
        return []
    try:
        import json
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def render(row, show_channel):
    """Turn a messages row into a printable line."""
    ts = fmt_time(row["created_at"])
    author = row["member_name"] or row["member_id"] or "?"
    content = row["content"] or ""
    mentions = parse_mentions(row["mentions"] if "mentions" in row.keys() else "")

    is_system = content.startswith(SYSTEM_PREFIXES)
    is_retracted = content.startswith("[RETRACTED:")

    parts = [f"{Colour.TIME}{ts}{Colour.RESET}"]
    if show_channel:
        parts.append(f"{Colour.CHANNEL}{row['channel']:>8}{Colour.RESET}")

    if is_retracted:
        parts.append(f"{Colour.AUTHOR}{author}{Colour.RESET}")
        parts.append(f"{Colour.RETRACTED}{content}{Colour.RESET}")
    elif is_system:
        parts.append(f"{Colour.SYSTEM}{content}{Colour.RESET}")
    else:
        parts.append(f"{Colour.AUTHOR}{author}{Colour.RESET}")
        if mentions:
            tag = "@" + ",@".join(mentions)
            parts.append(f"{Colour.MENTION}{tag}{Colour.RESET}")
        parts.append(content)

    return "  ".join(parts)


def open_db(db_path):
    db = sqlite3.connect(str(db_path), timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=2000")
    return db


def fetch(db, channel, after_id, since_ts):
    sql = ("SELECT id, channel, member_id, member_name, content, mentions, "
           "created_at FROM messages WHERE id > ?")
    args = [after_id]
    if channel:
        sql += " AND channel = ?"
        args.append(channel)
    if since_ts:
        sql += " AND created_at >= ?"
        args.append(since_ts)
    sql += " ORDER BY id"
    return db.execute(sql, args).fetchall()


def main():
    ap = argparse.ArgumentParser(description="Live console view of nth traffic.")
    ap.add_argument("-c", "--channel", help="Filter to one channel code.")
    ap.add_argument("-s", "--since", type=int, default=0,
                    help="Seconds of backlog to print before tailing (default 0).")
    ap.add_argument("--snapshot", action="store_true",
                    help="Print current log and exit (no tail).")
    ap.add_argument("--no-color", action="store_true",
                    help="Disable ANSI colour.")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"Path to nth.db (default {DB_PATH}).")
    ap.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                    help=f"Tail poll interval in seconds (default {POLL_INTERVAL}).")
    args = ap.parse_args()

    force_utf8_stdout()

    if args.no_color or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        Colour.disable()
    else:
        enable_windows_vt()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"nth.db not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    db = open_db(db_path)
    show_channel = args.channel is None

    # Starting watermark: if --since, pull history from that far back.
    since_ts = None
    if args.since > 0:
        since_ts = datetime.fromtimestamp(time.time() - args.since).astimezone().isoformat()
    elif args.snapshot:
        since_ts = "1970-01-01T00:00:00+00:00"

    rows = fetch(db, args.channel, 0, since_ts)
    for r in rows:
        print(render(r, show_channel))
    last_id = rows[-1]["id"] if rows else 0

    if args.snapshot:
        return

    # SIGINT handler for clean exit.
    stop = {"flag": False}

    def _sigint(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sigint)

    header = f"(following {'all channels' if show_channel else args.channel}; Ctrl-C to exit)"
    print(f"{Colour.DIM}{header}{Colour.RESET}", file=sys.stderr)

    try:
        while not stop["flag"]:
            new = fetch(db, args.channel, last_id, None)
            for r in new:
                print(render(r, show_channel))
                last_id = r["id"]
            sys.stdout.flush()
            time.sleep(args.poll_interval)
    finally:
        db.close()


if __name__ == "__main__":
    main()
