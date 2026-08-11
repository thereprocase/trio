#!/usr/bin/env python3
"""Publish a Codex TUI session's context usage as a claude-context snapshot.

Codex records token_count events (last_token_usage + model_context_window) in
its rollout JSONL under CODEX_HOME/sessions/. This daemon tails the newest
rollout and mirrors the latest usage into the same atomic snapshot files the
Claude Code statusline publishes (~/.local/state/claude-context/<slug>.json),
so an nth spoke/local monitor started with --claude-session <slug> relays
codex context to the hub exactly like a Claude session's.

Snapshots are only refreshed while a live codex process holds the rollout
open; when codex exits, the snapshot ages out naturally (consumers treat
>120s as stale), matching how an idle Claude statusline goes dark.

Stdlib only. One publisher per codex TUI per machine.

Usage:
    python3 codex_context_publisher.py --name Codex-Terra [--slug codex-terra]
        [--codex-home ~/.codex] [--interval 5] [--once]
"""

import argparse
import glob
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

if sys.platform == "win32":
    _CTX_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "claude-context")
else:
    _CTX_DIR = os.path.join(os.environ.get("XDG_STATE_HOME",
                                           os.path.expanduser("~/.local/state")),
                            "claude-context")

STALE_ROLLOUT_S = 600  # fallback liveness window when /proc is unavailable


def newest_rollout(codex_home, session_id=""):
    """Newest rollout file, optionally restricted to one codex session.
    With multiple codex TUIs sharing a CODEX_HOME, the bare newest-file
    heuristic follows whichever session spoke last — pass session_id to pin
    a publisher to its own session's rollout."""
    pattern = os.path.join(codex_home, "sessions", "*", "*", "*", "rollout-*.jsonl")
    files = glob.glob(pattern)
    if session_id:
        files = [p for p in files if session_id in os.path.basename(p)]
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def codex_has_open(path):
    """True if a running codex process holds `path` open. /proc-based; on
    platforms without /proc fall back to rollout mtime recency."""
    proc = "/proc"
    if not os.path.isdir(proc):
        try:
            return (time.time() - os.path.getmtime(path)) < STALE_ROLLOUT_S
        except OSError:
            return False
    target = os.path.realpath(path)
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() != "codex":
                    continue
            fd_dir = os.path.join(proc, pid, "fd")
            for fd in os.listdir(fd_dir):
                try:
                    if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                        return True
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return False


class RolloutTail:
    """Incrementally parse one rollout file, keeping the latest context facts."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.session_id = ""
        self.cwd = ""
        self.model = ""
        self.context_window = None
        self.last_usage = None
        self.last_event_ts = ""

    def scan(self):
        """Read newly appended bytes; return True if context facts changed."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self.offset:  # truncated/rotated underneath us
            self.offset = 0
            self.partial = ""
        if size == self.offset:
            return False
        changed = False
        with open(self.path, encoding="utf-8", errors="replace") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        buf = self.partial + chunk
        lines = buf.split("\n")
        self.partial = lines.pop()  # last element is partial (or empty)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line, strict=False)
            except ValueError:
                continue
            payload = obj.get("payload") or {}
            # token_count nests as {"type":"event_msg","payload":{"type":"token_count"}};
            # turn_context is a top-level type with the context dict as payload.
            ptype = payload.get("type") or obj.get("type") or ""
            if payload.get("session_id") and not self.session_id:
                self.session_id = payload["session_id"]
                self.cwd = payload.get("cwd", self.cwd)
                changed = True
            if ptype == "turn_context":
                if payload.get("model"):
                    self.model = payload["model"]
                if payload.get("cwd"):
                    self.cwd = payload["cwd"]
                changed = True
            elif ptype == "token_count":
                info = payload.get("info") or {}
                mcw = info.get("model_context_window")
                if mcw:
                    self.context_window = mcw
                last = info.get("last_token_usage")
                if last and last.get("total_tokens"):
                    self.last_usage = last
                    self.last_event_ts = obj.get("timestamp", "")
                    changed = True
        return changed

    def snapshot(self, name, slug):
        if not self.last_usage or not self.context_window:
            return None
        used = self.last_usage.get("total_tokens", 0)
        pct = round(min(100.0, used / self.context_window * 100.0), 1)
        return {
            "session_id": slug,
            "session_name": name,
            "used_pct": pct,
            "cw_size": self.context_window,
            "model": self.model,
            "cwd": self.cwd,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "codex-rollout",
            "codex_session_id": self.session_id,
            "rollout": os.path.basename(self.path),
            "total_tokens": used,
            "last_event_ts": self.last_event_ts,
        }


def write_snapshot(payload, slug):
    os.makedirs(_CTX_DIR, exist_ok=True)
    path = os.path.join(_CTX_DIR, slug + ".json")
    fd, tmp = tempfile.mkstemp(dir=_CTX_DIR, suffix=".tmp", prefix="ctx_")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", required=True,
                    help="session_name shown in rings/dumps (e.g. Codex-Terra)")
    ap.add_argument("--slug", default="",
                    help="snapshot file id; default codex-<name slugified>")
    ap.add_argument("--codex-home",
                    default=os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")))
    ap.add_argument("--session-id", default="",
                    help="pin to the rollout of this codex session id "
                         "(required when several codex TUIs share a CODEX_HOME)")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true",
                    help="single scan+publish, then exit (for testing)")
    args = ap.parse_args()

    slug = args.slug or re.sub(r"[^a-z0-9]+", "-", args.name.lower()).strip("-")
    if not slug.startswith("codex"):
        slug = "codex-" + slug
    tail = None
    announced = False
    while True:
        path = newest_rollout(args.codex_home, args.session_id)
        if path:
            if tail is None or tail.path != path:
                tail = RolloutTail(path)
                print(f"[codex-ctx] following {os.path.basename(path)}", flush=True)
            tail.scan()
            snap = tail.snapshot(args.name, slug)
            # Refresh ts every tick while codex is alive so consumers see it
            # as fresh; stop refreshing once codex exits and let it age out.
            if snap and (args.once or codex_has_open(path)):
                write_snapshot(snap, slug)
                if not announced:
                    print(f"[codex-ctx] publishing {slug}.json "
                          f"({snap['used_pct']}% of {snap['cw_size']})", flush=True)
                    announced = True
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
