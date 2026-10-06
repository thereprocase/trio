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
RESOLVE_INTERVAL_S = 60  # how often to re-glob the sessions tree for our rollout
HAS_OPEN_CACHE_S = 30    # how long a "codex holds this open" answer stays good


def newest_rollout(codex_home, session_id=""):
    """Newest rollout file, optionally restricted to one codex session.
    With multiple codex TUIs sharing a CODEX_HOME, the bare newest-file
    heuristic follows whichever session spoke last — pass session_id to pin
    a publisher to its own session's rollout."""
    pattern = os.path.join(codex_home, "sessions", "*", "*", "*", "rollout-*.jsonl")
    files = glob.glob(pattern)
    if session_id:
        # Exact tail match, not a substring: rollout names end in
        # -<session_id>.jsonl, and two sessions started in the same second
        # share a long filename prefix. A substring test matched both and
        # then max(mtime) picked one arbitrarily — reintroducing the very
        # cross-session mixup that pinning was added to prevent.
        suffix = "-{}.jsonl".format(session_id)
        exact = [p for p in files if os.path.basename(p).endswith(suffix)]
        # Fall back to the prefix match so a deliberately shortened id
        # still resolves, but only when nothing matched exactly.
        files = exact or [p for p in files if session_id in os.path.basename(p)]
    if not files:
        return None
    try:
        return max(files, key=lambda p: os.path.getmtime(p))
    except OSError:
        # A rollout removed between glob() and getmtime() must not kill
        # a long-lived daemon.
        return None


_has_open_cache = {"path": None, "at": 0.0, "val": False}


def codex_has_open(path, _cache_s=None):
    """True if a running codex process holds `path` open. /proc-based; on
    platforms without /proc fall back to rollout mtime recency.

    Answers are cached briefly: this walks every PID on the box reading
    /proc/<pid>/comm, which is ~3 syscalls per process (roughly 1,400 on a
    normal desktop) — far too much to repeat every few seconds to answer a
    question that changes when a TUI starts or stops. A recently-written
    rollout is treated as live without scanning at all.
    """
    cache_s = HAS_OPEN_CACHE_S if _cache_s is None else _cache_s
    now = time.monotonic()
    c = _has_open_cache
    if c["path"] == path and (now - c["at"]) < cache_s:
        return c["val"]

    def _remember(val):
        c["path"], c["at"], c["val"] = path, now, val
        return val

    # Fast path: a file written in the last few seconds is definitionally
    # held by a live codex — no need to enumerate anything.
    try:
        if (time.time() - os.path.getmtime(path)) < 15:
            return _remember(True)
    except OSError:
        return _remember(False)

    proc = "/proc"
    if not os.path.isdir(proc):
        try:
            return _remember((time.time() - os.path.getmtime(path)) < STALE_ROLLOUT_S)
        except OSError:
            return _remember(False)
    target = os.path.realpath(path)
    try:
        pids = os.listdir(proc)
    except OSError:
        # /proc vanished or became unreadable (containers, odd sandboxes).
        try:
            return _remember((time.time() - os.path.getmtime(path)) < STALE_ROLLOUT_S)
        except OSError:
            return _remember(False)
    for pid in pids:
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
                        return _remember(True)
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    return _remember(False)


class RolloutTail:
    """Incrementally parse one rollout file, keeping the latest context facts."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.partial = ""
        self.session_id = ""
        self.cwd = ""
        self.model = ""
        self.effort = ""
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
                if payload.get("effort"):
                    self.effort = payload["effort"]
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
        # `ts` is refreshed every tick while codex holds the rollout open, so
        # it measures liveness, not recency — a session idle for hours still
        # publishes a fresh ts carrying an hours-old token count. Ship the
        # real data age alongside it so consumers can dim rather than lie.
        data_age_s = None
        if self.last_event_ts:
            try:
                evt = datetime.fromisoformat(
                    self.last_event_ts.replace("Z", "+00:00"))
                if evt.tzinfo is None:
                    evt = evt.replace(tzinfo=timezone.utc)
                data_age_s = max(0, int(
                    (datetime.now(timezone.utc) - evt).total_seconds()))
            except (ValueError, TypeError):
                data_age_s = None
        return {
            "session_id": slug,
            "session_name": name,
            "used_pct": pct,
            "cw_size": self.context_window,
            "model": self.model,
            "effort": self.effort,
            "cwd": self.cwd,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "codex-rollout",
            "codex_session_id": self.session_id,
            "rollout": os.path.basename(self.path),
            "total_tokens": used,
            "last_event_ts": self.last_event_ts,
            "data_age_s": data_age_s,
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
    path = None
    missing_ticks = 0
    last_resolve = 0.0
    while True:
        # Re-resolving means globbing the whole sessions tree and stat-ing
        # every rollout — hundreds of files, several hundred MB, on a box
        # with any history. Once we're following a file that is still
        # growing, that answer cannot change, so only re-resolve when we
        # have no file or ours has gone quiet.
        need_resolve = (
            path is None
            or (time.time() - last_resolve) > RESOLVE_INTERVAL_S
        )
        if need_resolve:
            found = newest_rollout(args.codex_home, args.session_id)
            last_resolve = time.time()
            if found and found != path:
                path = found
                tail = RolloutTail(path)
                print(f"[codex-ctx] following {os.path.basename(path)}", flush=True)
            elif not found:
                # Pinned rollout not present (publisher started before codex,
                # or the session resumed into a new file with a new id).
                # Say so once instead of spinning silently forever.
                missing_ticks += 1
                if missing_ticks in (1, 60):
                    what = args.session_id or "any session"
                    print(f"[codex-ctx] no rollout found for {what} under "
                          f"{args.codex_home}/sessions — waiting", flush=True)
        if path:
            missing_ticks = 0
            tail.scan()
            snap = tail.snapshot(args.name, slug)
            # Keep writing while codex holds the file open: that publishes
            # liveness. snapshot() carries data_age_s so consumers can tell
            # a live-but-idle session from a genuinely current reading.
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
