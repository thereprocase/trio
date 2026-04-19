"""
Live multi-agent dashboard for a trio channel.

Watches a 3-8 agent Claude Code group chat and surfaces engagement signals
(read latency, queue depth, send cadence) so a human operator can spot
agents that are slow, stuck, lurking, or dropped.

Pure consumer of the shared SQLite event stream — we observe the messages
and members tables and update per-agent rolling state incrementally as
events land. We never scrape agent internals.

Usage:
    python3 nth_dashboard.py CHANNEL        # watch one channel
    python3 nth_dashboard.py --help

Keybinds:
    s    cycle sort: last-seen → read-latency → sent → queue-depth → last-seen
    p    pause / resume polling
    q    quit (also Ctrl-C)

Requires `rich` (pip install rich).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

try:
    from rich.box import SIMPLE_HEAD
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.stderr.write("nth_dashboard requires 'rich'. Install with: pip install rich\n")
    sys.exit(1)


# ───────── Thresholds (edit these, not the logic) ─────────
STALE_SECONDS        = 300.0     # heartbeat missing this long → stale status
DEAD_SECONDS         = 900.0     # heartbeat missing this long → dead status
LATENCY_LAGGING_S    = 5.0       # avg read latency above → yellow
LATENCY_BAD_S        = 20.0      # avg read latency above → red
QUEUE_LAGGING        = 3         # queue depth above → yellow
QUEUE_BAD            = 10        # queue depth above → red
RECENT_WINDOW_N      = 20        # rolling window size for latency + send-length
RATE_WINDOW_SEC      = 3600.0    # /hr rate window
POLL_INTERVAL        = 0.5       # seconds between DB polls
REFRESH_HZ           = 4         # Live refresh rate
SPARKLINE_MINS       = 5         # minutes of global msg-rate to show
SPARKLINE_BIN_SEC    = 10        # one bar = 10s
TAIL_MIN_LINES       = 8         # min display rows reserved for chat tail
TAIL_FETCH           = 60        # how many messages to pull for the tail
SNIPPET_CHARS        = 60        # table row "Last snippet" truncation
TABLE_OVERHEAD_LINES = 4         # header + separator + padding for the table
SLEEPING_KEYWORDS    = ("idle", "standing by", "tier 3", "agent-monitor")

AGENT_PALETTE = [
    "cyan", "magenta", "green", "yellow",
    "bright_blue", "bright_red", "bright_cyan", "bright_magenta",
]

SPARK_BARS = "▁▂▃▄▅▆▇█"
STATUS_GLYPH = {
    "active":  ("●", "green"),
    "working": ("◐", "cyan"),
    "idle":    ("◯", "bright_black"),
    "stale":   ("○", "yellow"),
    "dead":    ("✕", "red"),
}

SORT_MODES = [
    ("last-seen",    lambda a: -(a.last_seen or 0)),
    ("read-latency", lambda a: -(a.avg_read_latency() or 0)),
    ("sent",         lambda a: -a.sent_count),
    ("queue-depth",  lambda a: -a.queue_depth),
]


# ───────── Helpers ─────────
def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def parse_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def fmt_rel(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 0:       return "0s"
    if s < 60:      return f"{s}s"
    if s < 3600:    return f"{s // 60}m"
    if s < 86400:   return f"{s // 3600}h"
    return f"{s // 86400}d"


def parse_mentions(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def sparkline(values: List[int]) -> str:
    if not values:
        return ""
    hi = max(values) or 1
    return "".join(SPARK_BARS[min(len(SPARK_BARS) - 1,
                                  int(v / hi * (len(SPARK_BARS) - 1)))]
                   for v in values)


def is_sleeping(status_text: str) -> bool:
    if not status_text:
        return False
    lo = status_text.lower()
    return any(kw in lo for kw in SLEEPING_KEYWORDS)


def force_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ───────── Per-agent state ─────────
@dataclass
class AgentState:
    id: str
    name: str
    color: str
    status_text: str = ""
    last_seen_iso: Optional[str] = None
    last_seen: Optional[float] = None        # heartbeat epoch
    last_read: int = 0                       # watermark
    queue_depth: int = 0
    sent_count: int = 0
    send_times: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=500))
    send_lengths: Deque[int] = field(default_factory=lambda: collections.deque(maxlen=RECENT_WINDOW_N))
    last_snippet: str = ""
    read_latencies: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=RECENT_WINDOW_N))
    pending_directed: Deque[int] = field(default_factory=lambda: collections.deque(maxlen=50))
    directed_received: int = 0
    directed_replied: int = 0

    # ── metrics ──
    def avg_read_latency(self) -> Optional[float]:
        if not self.read_latencies:
            return None
        return sum(self.read_latencies) / len(self.read_latencies)

    def avg_send_len(self) -> Optional[float]:
        if not self.send_lengths:
            return None
        return sum(self.send_lengths) / len(self.send_lengths)

    def send_rate_per_hour(self) -> int:
        cutoff = now_ts() - RATE_WINDOW_SEC
        return sum(1 for t in self.send_times if t >= cutoff)

    def reply_rate(self) -> Optional[float]:
        if self.directed_received == 0:
            return None
        return self.directed_replied / self.directed_received

    def status(self) -> str:
        age = (now_ts() - self.last_seen) if self.last_seen else float("inf")
        if age > DEAD_SECONDS:
            return "dead"
        if age > STALE_SECONDS:
            return "stale"
        if is_sleeping(self.status_text):
            return "idle"
        # "working" = monitor is running but queue is growing, implying agent
        # is off-Monitor handling a tool call and not yielding.
        if self.queue_depth >= QUEUE_LAGGING:
            return "working"
        return "active"


# ───────── Dashboard core ─────────
class Dashboard:
    def __init__(self, channel: str, db_path: Path, console: Optional[Console] = None):
        self.channel = channel
        self.db_path = db_path
        self.console = console or Console()
        self.db: Optional[sqlite3.Connection] = None
        self.agents: Dict[str, AgentState] = {}
        self.last_msg_id = 0
        self.total_msgs = 0
        self.started_at = now_ts()
        self.sort_idx = 0
        self.paused = False
        self.rate_bins: Deque[Tuple[int, int]] = collections.deque(
            maxlen=(SPARKLINE_MINS * 60) // SPARKLINE_BIN_SEC
        )   # (bin_epoch, count)
        self.error: Optional[str] = None
        self._palette_cursor = 0

    def open(self) -> None:
        self.db = sqlite3.connect(str(self.db_path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=2000")

    def close(self) -> None:
        if self.db:
            self.db.close()
            self.db = None

    # ── State intake ──
    def _ensure_agent(self, mid: str, name: str) -> AgentState:
        a = self.agents.get(mid)
        if a is None:
            color = AGENT_PALETTE[self._palette_cursor % len(AGENT_PALETTE)]
            self._palette_cursor += 1
            a = AgentState(id=mid, name=name or mid, color=color)
            self.agents[mid] = a
        elif name and not a.name:
            a.name = name
        return a

    def _fetch_members(self) -> None:
        assert self.db
        rows = self.db.execute(
            "SELECT id, name, status_text, last_seen, last_read "
            "FROM members WHERE channel = ?",
            (self.channel,),
        ).fetchall()
        for r in rows:
            agent = self._ensure_agent(r["id"], r["name"])
            agent.status_text = r["status_text"] or ""
            if r["last_seen"]:
                agent.last_seen_iso = r["last_seen"]
                agent.last_seen = parse_ts(r["last_seen"])

            new_wm = r["last_read"] or 0
            if new_wm > agent.last_read:
                self._credit_read(agent, agent.last_read, new_wm)
                agent.last_read = new_wm

    def _credit_read(self, agent: AgentState, from_id: int, to_id: int) -> None:
        """Watermark advanced from from_id to to_id for this agent.
        Compute observed read latencies for all messages in that range not
        authored by the agent."""
        assert self.db
        rows = self.db.execute(
            "SELECT id, member_id, mentions, created_at FROM messages "
            "WHERE channel = ? AND id > ? AND id <= ?",
            (self.channel, from_id, to_id),
        ).fetchall()
        obs_t = now_ts()
        for row in rows:
            if row["member_id"] == agent.id:
                continue
            sent = parse_ts(row["created_at"])
            if sent is not None:
                agent.read_latencies.append(max(0.0, obs_t - sent))
            if agent.queue_depth > 0:
                agent.queue_depth -= 1
            # Check if this was a directed @mention awaiting reply.
            if row["id"] in agent.pending_directed:
                # Receipt alone isn't a reply; reply is counted when agent
                # next sends something. But receipt caps the window.
                pass

    def _fetch_new_messages(self) -> None:
        assert self.db
        rows = self.db.execute(
            "SELECT id, member_id, member_name, content, mentions, created_at "
            "FROM messages WHERE channel = ? AND id > ? ORDER BY id",
            (self.channel, self.last_msg_id),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            self._ingest_message(row)
            self.last_msg_id = row["id"]
            self.total_msgs += 1
            self._record_rate(parse_ts(row["created_at"]) or now_ts())

    def _ingest_message(self, row) -> None:
        sender_id = row["member_id"]
        sender = self._ensure_agent(sender_id, row["member_name"])
        content = row["content"] or ""
        sent = parse_ts(row["created_at"]) or now_ts()

        sender.sent_count += 1
        sender.send_times.append(sent)
        sender.send_lengths.append(len(content))
        sender.last_snippet = content[:SNIPPET_CHARS] + ("…" if len(content) > SNIPPET_CHARS else "")

        # If sender had pending directed-to-them messages, count this send as a reply
        # to all of them (simple first-response-counts model).
        while sender.pending_directed:
            sender.pending_directed.popleft()
            sender.directed_replied += 1

        mentions = parse_mentions(row["mentions"])
        for other in self.agents.values():
            if other.id == sender_id:
                continue
            # Only bump queue for agents who haven't already read this id.
            # Without this check, backfilled history at startup inflates the
            # queue of agents whose last_read watermark is already past these
            # messages.
            if other.last_read < row["id"]:
                other.queue_depth += 1
            if other.id in mentions:
                other.directed_received += 1
                other.pending_directed.append(row["id"])

    def _record_rate(self, ts: float) -> None:
        bin_key = int(ts // SPARKLINE_BIN_SEC) * SPARKLINE_BIN_SEC
        if self.rate_bins and self.rate_bins[-1][0] == bin_key:
            prev_k, prev_v = self.rate_bins[-1]
            self.rate_bins[-1] = (prev_k, prev_v + 1)
        else:
            self.rate_bins.append((bin_key, 1))

    def _sparkline_values(self) -> List[int]:
        # Pad the rate_bins to fixed width so the sparkline doesn't jitter.
        want = (SPARKLINE_MINS * 60) // SPARKLINE_BIN_SEC
        now_bin = int(now_ts() // SPARKLINE_BIN_SEC) * SPARKLINE_BIN_SEC
        by_bin = {k: v for k, v in self.rate_bins}
        out = []
        for i in range(want, 0, -1):
            k = now_bin - (i - 1) * SPARKLINE_BIN_SEC
            out.append(by_bin.get(k, 0))
        return out

    # ── Tick ──
    def tick(self) -> None:
        if self.paused:
            return
        try:
            self._fetch_members()
            self._fetch_new_messages()
            self.error = None
        except sqlite3.Error as e:
            self.error = f"db error: {e}"

    # ── Render ──
    def _latency_colour(self, lat: Optional[float]) -> str:
        if lat is None:                 return "bright_black"
        if lat >= LATENCY_BAD_S:        return "red"
        if lat >= LATENCY_LAGGING_S:    return "yellow"
        return "green"

    def _queue_colour(self, q: int) -> str:
        if q >= QUEUE_BAD:       return "red"
        if q >= QUEUE_LAGGING:   return "yellow"
        return "green"

    def render(self) -> Group:
        # ── Header ──
        runtime = fmt_rel(now_ts() - self.started_at)
        elapsed = max(1, now_ts() - self.started_at)
        global_rate = self.total_msgs * 60.0 / elapsed
        spark = sparkline(self._sparkline_values())
        sort_name = SORT_MODES[self.sort_idx][0]
        pause_tag = "[PAUSED] " if self.paused else ""

        header = Text()
        header.append(f"{pause_tag}", style="bold red" if self.paused else "")
        header.append(f"trio#{self.channel}", style="bold white")
        header.append(f"  runtime={runtime}", style="bright_black")
        header.append(f"  msgs={self.total_msgs}", style="bright_black")
        header.append(f"  {global_rate:.1f}/min", style="bright_black")
        header.append(f"  {spark}", style="cyan")
        header.append(f"  sort={sort_name}", style="bright_black")
        header.append("   [s]ort [p]ause [q]uit", style="dim")

        # ── Table ──
        table = Table(
            box=SIMPLE_HEAD,
            expand=True,
            padding=(0, 1),
            show_edge=False,
            header_style="bold bright_black",
        )
        table.add_column(" ", width=2)                       # status glyph
        table.add_column("Agent", no_wrap=True, min_width=8)
        table.add_column("Model", no_wrap=True, style="dim", width=6)
        table.add_column("Seen", no_wrap=True, justify="right", width=5)
        table.add_column("Read-lat", no_wrap=True, justify="right", width=8)
        table.add_column("Sent", no_wrap=True, justify="right", width=10)
        table.add_column("Q", no_wrap=True, justify="right", width=4)
        table.add_column("@%", no_wrap=True, justify="right", width=5)
        table.add_column("Avg-len", no_wrap=True, justify="right", width=7)
        table.add_column("Last snippet", no_wrap=True, overflow="ellipsis", ratio=1)

        agents_sorted = sorted(self.agents.values(), key=SORT_MODES[self.sort_idx][1])

        for a in agents_sorted:
            status = a.status()
            glyph, sty = STATUS_GLYPH[status]
            age = (now_ts() - a.last_seen) if a.last_seen else None

            lat = a.avg_read_latency()
            lat_txt = f"{lat:.1f}s" if lat is not None else "—"
            lat_sty = self._latency_colour(lat)

            qd = a.queue_depth
            qd_sty = self._queue_colour(qd)

            rate = a.send_rate_per_hour()
            sent_txt = f"{a.sent_count} ({rate}/h)"

            avg_len = a.avg_send_len()
            len_txt = f"{int(avg_len)}" if avg_len is not None else "—"

            rr = a.reply_rate()
            rr_txt = f"{rr * 100:.0f}%" if rr is not None else "—"

            model = "-"        # we don't have this; placeholder kept for future

            table.add_row(
                Text(glyph, style=sty),
                Text(a.name, style=a.color if status not in ("stale", "dead") else "dim"),
                model,
                fmt_rel(age),
                Text(lat_txt, style=lat_sty),
                sent_txt,
                Text(str(qd), style=qd_sty),
                rr_txt,
                len_txt,
                a.last_snippet or "",
            )

        if not agents_sorted:
            table.add_row("", Text("(no members yet)", style="dim"), "", "", "", "", "", "", "", "")

        # ── Tail ──
        tail_rows = self._render_tail()

        sections: List = [header, table]
        if self.error:
            sections.append(Text(f"  ⚠ {self.error}", style="red"))
        sections.append(Text(""))
        sections.append(tail_rows)
        return Group(*sections)

    def _tail_viewport_lines(self) -> int:
        """How many terminal rows the tail may use. Header, table, and
        blank separators eat from the top; everything else is ours."""
        total = self.console.size.height
        used_above = 2                                      # header + blank
        used_above += TABLE_OVERHEAD_LINES + max(1, len(self.agents))
        if self.error:
            used_above += 1
        used_above += 1                                     # blank between table and tail
        return max(TAIL_MIN_LINES, total - used_above - 1)

    def _render_tail(self) -> Group:
        assert self.db is not None
        rows = self.db.execute(
            "SELECT id, member_id, member_name, content, mentions, created_at "
            "FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?",
            (self.channel, TAIL_FETCH),
        ).fetchall()
        rows = list(reversed(rows))                         # oldest first

        # Build a chronological stream of wrapped display lines.
        wrap_width = max(20, self.console.size.width - 2)
        all_lines: List[Text] = []
        for i, r in enumerate(rows):
            t = parse_ts(r["created_at"])
            hhmmss = datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "--:--:--"
            sender = self._ensure_agent(r["member_id"], r["member_name"])
            mentions = parse_mentions(r["mentions"])
            content = r["content"] or ""

            header = Text()
            header.append(f"{hhmmss}  ", style="bright_black")
            header.append(sender.name, style=f"{sender.color} bold")
            if mentions:
                header.append(" → @" + ",@".join(mentions), style="yellow")
            all_lines.append(header)

            body = Text("  " + content)
            for wrapped in body.wrap(self.console, wrap_width):
                all_lines.append(wrapped)

            if i != len(rows) - 1:                          # blank separator between messages
                all_lines.append(Text(""))

        # Bottom-align into the viewport: keep only the most recent lines
        # that fit; pad the top with blanks when we have less content than
        # screen real-estate (e.g. a fresh channel).
        height = self._tail_viewport_lines()
        if len(all_lines) > height:
            all_lines = all_lines[-height:]
        else:
            pad = height - len(all_lines)
            all_lines = [Text("")] * pad + all_lines

        return Group(*all_lines)

    # ── Keybinds ──
    def cycle_sort(self) -> None:
        self.sort_idx = (self.sort_idx + 1) % len(SORT_MODES)

    def toggle_pause(self) -> None:
        self.paused = not self.paused


# ───────── Key reader thread ─────────
class KeyReader(threading.Thread):
    def __init__(self, dash: Dashboard, stop_flag: Dict[str, bool]):
        super().__init__(daemon=True)
        self.dash = dash
        self.stop = stop_flag

    def run(self) -> None:
        try:
            if sys.platform == "win32":
                self._run_windows()
            else:
                self._run_unix()
        except Exception:
            # Never let keyboard handling kill the app.
            pass

    def _run_windows(self) -> None:
        import msvcrt
        while not self.stop["flag"]:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                self._handle(ch)
            time.sleep(0.05)

    def _run_unix(self) -> None:
        import select
        import termios
        import tty

        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self.stop["flag"]:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    self._handle(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _handle(self, ch: str) -> None:
        if ch in ("q", "Q", "\x03"):
            self.stop["flag"] = True
        elif ch in ("s", "S"):
            self.dash.cycle_sort()
        elif ch in ("p", "P"):
            self.dash.toggle_pause()


# ───────── Main ─────────
def main() -> int:
    force_utf8_stdout()

    ap = argparse.ArgumentParser(description="Live multi-agent dashboard for a trio channel.")
    ap.add_argument("channel", help="Channel code to observe.")
    ap.add_argument("--db", default=str(Path.home() / ".claude" / "nth" / "nth.db"),
                    help="Path to nth.db (default: ~/.claude/nth/nth.db).")
    ap.add_argument("--poll-interval", type=float, default=POLL_INTERVAL,
                    help=f"DB poll interval in seconds (default {POLL_INTERVAL}).")
    ap.add_argument("--refresh-hz", type=int, default=REFRESH_HZ,
                    help=f"Live render refresh Hz (default {REFRESH_HZ}).")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.stderr.write(f"nth.db not found at {db_path}\n")
        return 1

    console = Console()
    dash = Dashboard(args.channel, db_path, console=console)
    dash.open()

    stop_flag = {"flag": False}

    def on_sigint(_sig, _frm):
        stop_flag["flag"] = True

    signal.signal(signal.SIGINT, on_sigint)

    keys = KeyReader(dash, stop_flag)
    keys.start()

    try:
        with Live(dash.render(), console=console, refresh_per_second=args.refresh_hz,
                  screen=True, transient=False) as live:
            next_poll = 0.0
            while not stop_flag["flag"]:
                t = time.monotonic()
                if t >= next_poll:
                    dash.tick()
                    next_poll = t + args.poll_interval
                live.update(dash.render())
                time.sleep(1.0 / max(1, args.refresh_hz))
    finally:
        stop_flag["flag"] = True
        dash.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
