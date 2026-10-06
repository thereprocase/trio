#!/usr/bin/env python3
"""nth_supervisor — the deterministic agent process supervisor.

Part of the unified-interface build (see proposals/unified-interface.md). This
is PLAIN SOFTWARE, not an agent: no LLM, no tokens in its control loop. It owns
the OS handles of headless `claude -p` agent sessions so the hub can spawn,
stop, hibernate, resume, and place them authoritatively — the thing today's
architecture can't do (the server can't see or kill a member's OS process,
the root of bugs B1/B2).

Design points realised here:
  * Agents are headless `claude -p` in stream-json mode on the user's Claude
    Code SUBSCRIPTION (not the Agent SDK — that needs a per-token API key).
  * The agent binary is configurable via $TRIO_AGENT_CMD so this module is
    testable against a fake stream-json agent WITHOUT spawning real, billed
    Claude sessions. In production it defaults to `claude`.
  * Durable identity lives in the `agents` table (nth_server schema); the
    supervisor keeps the DB row's state/pid/session_id in sync with the OS
    process. A hibernated agent keeps its session_id and is revived with
    `--resume`, memory intact.

This module intentionally does NOT wire the HTTP endpoints or channel message
routing yet — those land in follow-up increments on nth_web. It provides the
process-lifecycle core + DB state machine, unit-tested in isolation.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None  # type: ignore

import nth_request_log as nrl
from nth_constants import AGENT_INBOX_CHANNEL

# Distinguishes turn keys minted by THIS hub process from those of any earlier
# one whose entries are still in the 24h request log. See _turn_key_locked.
_RUN_NONCE = f"{os.getpid():x}-"

_NTH_HOME = Path(os.environ.get("NTH_HOME", str(Path.home() / ".claude" / "nth")))
DB_PATH = Path(os.environ.get("NTH_DB_PATH", str(_NTH_HOME / "nth.db")))

# Attachment root, derived the same way nth_web.attach_dir_for() derives it —
# beside the database — so a scratch DB genuinely isolates its files and the
# per-agent --add-dir grant points at the directory the web upload path actually
# writes to. Deliberately derived rather than imported: nth_web rebinds its own
# ATTACH_DIR from the resolved db_path at startup, so a shared module constant
# would be wrong for any non-default database.
ATTACH_DIR = DB_PATH.resolve().parent / "attachments"

# ── Token-consumption history ──
# Real token usage over time, harvested for free from the agents' own traffic:
# each turn's stream-json `result` event carries an accumulated `usage` (the
# turn's total billed tokens — input + cache + output). Ring-buffer those
# per-turn totals so the web layer can aggregate tokens/15m/1h/24h across all
# agents. No extra API calls, no rate-limit-header scraping.
TOKEN_EVENTS_PATH = _NTH_HOME / "token-events.json"
_TOKEN_LOCK = threading.Lock()
_TOKEN_MAX_AGE = 24 * 3600     # keep 24h of turn events
# Retention is time-based, but compaction re-reads and rewrites the whole file,
# so a hard count cap keeps one runaway agent (or a provider flooding synthetic
# events) from making each compaction progressively slower.
_TOKEN_MAX_EVENTS = 50_000
# Compact this often, not every append. See record_token_event.
_TOKEN_PRUNE_EVERY = 500
_token_appends = 0
# No single turn can legitimately bill this many tokens of one category. A cap
# keeps one absurd provider value from dominating a 24h aggregate.
_TOKEN_MAX_PER_EVENT = 1_000_000_000
# Schema generation for a written event. Bumped when the arithmetic behind the
# stored numbers changes, so aggregates can tell old records from new ones.
#   (absent) — pre-provider events; only tot/out are meaningful.
#   2        — Codex categories are disjoint (cached subtracted from input).
#              Codex events WITHOUT this marker inflate cached tokens ~2x.
_TOKEN_SCHEMA = 2


def _num_ok(v: Any) -> bool:
    """A real, finite number (not bool, which subclasses int).

    NaN and infinity must be rejected here: json.loads accepts the non-standard
    NaN/Infinity tokens, they pass an isinstance check, and json.dumps re-emits
    them — which browsers' JSON.parse rejects, blanking the whole usage panel.
    """
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    # math.isfinite() RAISES OverflowError on an int too large to convert to
    # float — it does not return False. Left unguarded, one oversized integer
    # in the file would make every subsequent load raise, including the load
    # inside record_token_event, so the file could never be trimmed or rewritten
    # again and /api/usage would 500 permanently.
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def load_token_events() -> List[Dict[str, Any]]:
    """Well-formed token events only.

    The file is JSONL (one event per line) so writes can be O(1) appends. A
    file written by an older build is a single JSON array; it is still read, so
    an upgrade does not throw away the last 24h of history.

    `t`, `tot` and `out` must all be real numbers — consumers do arithmetic on
    every one, so a hand-corrupted entry must not reach the aggregator.
    """
    try:
        # errors="replace", not strict: a crash mid-append leaves a truncated
        # multi-byte sequence, and a strict read raises UnicodeDecodeError —
        # which is a ValueError, NOT an OSError, so it would escape this
        # handler entirely. Callers include a web request handler with no
        # wrapping try, where that means a dropped connection on every poll
        # until someone repairs the file by hand. A mangled line is dropped by
        # the per-line JSON parse below instead.
        raw = TOKEN_EVENTS_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: List[Any] = []
    stripped = raw.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else []
        except (ValueError, json.JSONDecodeError):
            items = []
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue  # skip a torn line rather than losing the whole file
    return [e for e in items if isinstance(e, dict)
            and _num_ok(e.get("t")) and _num_ok(e.get("tot")) and _num_ok(e.get("out"))]


def record_token_event(agent_id: str, usage: Optional[Dict[str, Any]],
                       now: Optional[float] = None,
                       provider: str = "claude") -> None:
    """Append a provider-attributed, per-turn token event (best-effort, 24h).

    ``usage`` may use Claude's snake_case stream-json fields or the normalized
    shape produced from Codex App Server token notifications.  Keep ``tot`` and
    ``out`` for compatibility with the original event format, but also retain
    every available input/cache/output category so the home screen can explain
    what the total contains instead of collapsing it into one opaque number.
    """
    if not isinstance(usage, dict):
        return

    def _int(key: str) -> int:
        value = usage.get(key)
        # int(nan) raises ValueError but int(inf) raises OverflowError, and a
        # non-finite value that slipped through would poison a 24h aggregate.
        if isinstance(value, float) and not math.isfinite(value):
            return 0
        try:
            # Cap the magnitude too. Python ints are unbounded, so a buggy
            # provider value would otherwise be written verbatim and skew the
            # 24h aggregate for its whole retention window — and an int beyond
            # float range makes math.isfinite() raise on every later load.
            return max(0, min(_TOKEN_MAX_PER_EVENT, int(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    input_tokens = _int("input_tokens")
    cache_write = _int("cache_creation_input_tokens")
    cache_read = _int("cache_read_input_tokens")
    output_tokens = _int("output_tokens")
    category_total = input_tokens + cache_write + cache_read + output_tokens
    # Codex supplies an authoritative totalTokens alongside its categories.
    # Preserve any provider-only remainder (for example a future token class)
    # as ``other`` rather than silently losing it.
    total = max(category_total, _int("total_tokens"))
    if total <= 0:
        return
    provider = provider.lower().strip()
    if provider not in ("claude", "codex"):
        provider = "unknown"
    ts = now if now is not None else datetime.now(timezone.utc).timestamp()
    event = {
        "t": ts, "id": agent_id, "provider": provider, "v": _TOKEN_SCHEMA,
        "tot": total, "out": output_tokens,
        "input": input_tokens, "cache_write": cache_write,
        "cache_read": cache_read, "output": output_tokens,
        "other": max(0, total - category_total),
    }
    # Append, don't rewrite. This runs on EVERY turn of EVERY agent, and the
    # obvious implementation — load the whole file, append, filter, dump —
    # costs O(n) CPU under one process-global lock on every one of those turns.
    # Measured at the 50k cap that is ~100ms of JSON work per turn, serialised
    # fleet-wide, so with a room full of agents each turn completion queues
    # behind whichever agent is currently rewriting. An append is O(1); the
    # expensive compaction happens once every _TOKEN_PRUNE_EVERY appends, which
    # is the same shape nth_request_log.py already uses.
    global _token_appends
    with _TOKEN_LOCK:
        try:
            TOKEN_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with TOKEN_EVENTS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
        except OSError:
            return
        _token_appends += 1
        if _token_appends < _TOKEN_PRUNE_EVERY:
            return
        _token_appends = 0
        _prune_token_events(ts)


def _prune_token_events(now_ts: float) -> None:
    """Compact the log to the retention window. Caller holds _TOKEN_LOCK."""
    events = load_token_events()
    cutoff = now_ts - _TOKEN_MAX_AGE
    events = [e for e in events if e["t"] >= cutoff]
    if len(events) > _TOKEN_MAX_EVENTS:
        events = events[-_TOKEN_MAX_EVENTS:]
    try:
        # Atomic replace so the unlocked web-side reader never observes a
        # torn or empty file mid-write.
        tmp = TOKEN_EVENTS_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
        os.replace(tmp, TOKEN_EVENTS_PATH)
    except OSError:
        pass


# ── Account usage via `claude -p "/usage"` ──
# `statusline-state.json` only refreshes when an interactive Claude Code session
# renders its status bar, so it goes stale (and can even show last week's numbers
# after a reset). The `/usage` slash command runs headless — `claude -p "/usage"`
# — and prints the CURRENT session/weekly percentages + reset times as plain
# text, at ZERO token cost (it's a client command, no model call). We run it on
# a short gate (see _USAGE_CLI_MIN_GAP) whenever an agent turn completes and cache the parsed result so
# /api/usage can prefer this fresh, accurate source over the statusline file.
USAGE_CLI_PATH = _NTH_HOME / "usage-cli.json"
_USAGE_CLI_LOCK = threading.Lock()
_USAGE_CLI_MIN_GAP = 60           # seconds between refresh ATTEMPTS (per activity)
_usage_cli_inflight = False       # guard: at most one refresh subprocess at a time
_usage_cli_last_attempt = 0.0     # gate on attempts too, so failures back off
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_reset_text(text: str, now: float) -> Optional[float]:
    """"Aug 6 at 2:30pm (America/Los_Angeles)" / "Aug 13 at 11am (...)" → unix
    seconds. A reset is always in the future, so a computed past time rolls to
    next year (handles a Dec→Jan boundary). None on any parse failure."""
    m = re.match(r"([A-Za-z]{3})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)"
                 r"\s*(?:\(([^)]+)\))?", (text or "").strip(), re.I)
    if not m:
        return None
    mon, day, hr, minute, ampm, tzname = m.groups()
    month = _MONTHS.get(mon.capitalize())
    if not month:
        return None
    # A named zone we can't resolve (no zoneinfo/tzdata, or an unknown key) must
    # yield None, not a confidently-wrong UTC time skewed by the zone offset.
    tz: Any = timezone.utc
    if tzname:
        if ZoneInfo is None:
            return None
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            return None
    hour = int(hr) % 12
    if ampm.lower() == "pm":
        hour += 12
    mins = int(minute) if minute else 0
    year = datetime.fromtimestamp(now, tz).year

    def _mk(y: int) -> Optional[float]:
        try:
            return datetime(y, month, int(day), hour, mins, tzinfo=tz).timestamp()
        except (ValueError, OverflowError):
            return None

    ts = _mk(year)
    if ts is not None and ts < now - 60:
        # Roll to next year (Dec→Jan). If that date is itself invalid (e.g.
        # Feb 29 → non-leap next year), return None rather than the past `ts`.
        ts = _mk(year + 1)
    return ts


def _parse_usage_output(text: str, now: float) -> Optional[Dict[str, Any]]:
    """Parse `/usage` stdout into {session_pct, session_resets, week_pct,
    week_resets, t}. Returns None if neither percentage is found."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")   # strip ANSI

    def _one(pattern: str) -> Optional[Dict[str, Any]]:
        m = re.search(pattern, text, re.M)
        if not m:
            return None
        return {"pct": int(m.group(1)),
                "resets": _parse_reset_text(m.group(2) or "", now)}

    session = _one(r"Current session:\s*(\d+)%\s*used(?:\s*·\s*resets\s*(.+?)\s*$)?")
    week = _one(r"Current week \(all models\):\s*(\d+)%\s*used(?:\s*·\s*resets\s*(.+?)\s*$)?")
    if session is None and week is None:
        return None
    return {
        "t": now,
        "session_pct": session["pct"] if session else None,
        "session_resets": session["resets"] if session else None,
        "week_pct": week["pct"] if week else None,
        "week_resets": week["resets"] if week else None,
    }


def load_usage_cli() -> Optional[Dict[str, Any]]:
    """The last cached `/usage` result, or None."""
    try:
        data = json.loads(USAGE_CLI_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("t"), (int, float)) else None


def _refresh_usage_cli() -> None:
    """Run `claude -p "/usage"`, parse it, and cache the result. Best-effort:
    any failure leaves the previous cache (and the statusline fallback) intact."""
    global _usage_cli_inflight
    binary = shutil.which("claude") or "claude"
    try:
        proc = subprocess.run(
            [binary, "-p", "/usage", "--strict-mcp-config",
             "--mcp-config", '{"mcpServers":{}}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=45, text=True)
        parsed = _parse_usage_output(proc.stdout, datetime.now(timezone.utc).timestamp())
        if parsed is not None:
            try:
                USAGE_CLI_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = USAGE_CLI_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(parsed))
                os.replace(tmp, USAGE_CLI_PATH)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        with _USAGE_CLI_LOCK:
            _usage_cli_inflight = False


def maybe_refresh_usage_cli() -> None:
    """If enough time has passed since the last refresh ATTEMPT (see
    _USAGE_CLI_MIN_GAP) and none is in flight, kick one off on a background thread. Non-blocking — never stalls
    the caller (the agent turn handler)."""
    global _usage_cli_inflight, _usage_cli_last_attempt
    now = datetime.now(timezone.utc).timestamp()
    with _USAGE_CLI_LOCK:
        if _usage_cli_inflight:
            return
        cached = load_usage_cli()
        # load_usage_cli only checks isinstance(t, (int, float)), which admits
        # an oversized JSON int — and float() on one raises OverflowError, not
        # ValueError. This is called from a web request handler with no
        # wrapping exception handler, so an unguarded raise costs the client
        # the entire response: no status line, just a dropped connection.
        try:
            last_success = float(cached.get("t") or 0) if cached else 0.0
        except (TypeError, ValueError, OverflowError):
            last_success = 0.0
        # Gate on the most recent ATTEMPT (success or not) so a persistently
        # failing /usage doesn't spawn a subprocess on every trigger.
        if now - max(last_success, _usage_cli_last_attempt) < _USAGE_CLI_MIN_GAP:
            return
        _usage_cli_inflight = True
        _usage_cli_last_attempt = now
    threading.Thread(target=_refresh_usage_cli, daemon=True).start()

# How many stderr lines to retain per agent for post-mortem diagnostics.
STDERR_TAIL_LINES = 200

TRIO_TOOL_NAMES = (
    "connect", "send", "dm", "poll", "ack", "pounds", "ask",
    "claim", "complete", "cancel", "release", "lock", "unlock",
    "set_status", "rename", "status", "roster", "history", "end",
    "list", "cull", "cleanup", "retract", "avatar_choices", "set_avatar",
)
MANAGED_ALLOWED_TOOLS = ",".join(
    f"mcp__nth-trio__trio_{name}" for name in TRIO_TOOL_NAMES)

# The MCP tool Claude Code calls itself (never the model) to resolve a gated
# tool call — see nth_server.nth_permission_prompt. Only meaningful when the
# agent actually has the nth-trio MCP server wired in (mcp_config) and its
# permission mode can actually produce a prompt (not bypassPermissions).
PERMISSION_PROMPT_TOOL = "mcp__nth-trio__trio_permission_prompt"

# Map the web dashboard permission profile to a `claude --permission-mode`.
# Models the hub offers when creating an agent, with the effort levels each
# accepts. Surfaced by /api/agent-models so the UI never hardcodes a model list.
# ORDER IS THE PICKER'S ORDER, most capable first. Codex's list arrives from
# its App Server already ranked; this one is ours to state, so state it.
# `efforts` is per model and is not decoration — Haiku genuinely has no `max`,
# and the picker reads this rather than offering a fixed low/medium/high.
CLAUDE_MODELS = [
    {"id": "fable", "name": "Fable", "efforts": ["low", "medium", "high", "max"]},
    {"id": "opus", "name": "Opus", "efforts": ["low", "medium", "high", "max"]},
    {"id": "sonnet", "name": "Sonnet", "efforts": ["low", "medium", "high", "max"],
     "default": True},
    {"id": "haiku", "name": "Haiku", "efforts": ["low", "medium", "high"]},
]

PERMISSION_MODES = {
    "observe": "manual",
    "balanced": "auto",
    "autonomous": "bypassPermissions",
}

# Valid agent lifecycle states (mirror the supervisor state machine in the
# design doc). Kept as plain strings in agents.state. ST_IDLE is set by the hub
# (idle-timer), not by this core — it's here so the enum is complete.
ST_SPAWNING = "spawning"
ST_RUNNING = "running"
ST_IDLE = "idle"
ST_COMPACTING = "compacting"
ST_SLEEPING = "sleeping"
ST_STOPPED = "stopped"
ST_ERRORED = "errored"

# Context window used to turn a turn's token usage into a fullness
# percentage. As of the 4.6/5 model generation, 1M tokens is the DEFAULT
# context window for Sonnet/Opus (no beta header needed) — only Haiku stays
# at the older 200k. Matched by substring against the model string Claude
# Code was spawned with (tier alias like "sonnet" or a full versioned model
# id), case-insensitive. An unrecognized/empty model string conservatively
# assumes the SMALLER window — under-reporting fullness (a full context
# read as merely high) is worse than over-reporting it, since it would hide
# a genuinely-imminent compaction.
DEFAULT_CONTEXT_WINDOW = 200_000
_MODEL_CONTEXT_WINDOWS = (
    ("haiku", 200_000),
)
_LARGE_CONTEXT_WINDOW = 1_000_000


def context_window_for(model: str) -> int:
    m = (model or "").lower()
    for needle, window in _MODEL_CONTEXT_WINDOWS:
        if needle in m:
            return window
    return _LARGE_CONTEXT_WINDOW if m else DEFAULT_CONTEXT_WINDOW

_warned_override = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_value(row, column: str, default: str = "") -> str:
    """Read an OPTIONAL agents-row column. Older/partial schemas (and the
    minimal tables some tests build) may not have every column, so a missing
    one reads as the default rather than raising."""
    if row is None or column not in row.keys():
        return default
    return row[column] or default


class ClaudeRuntime:
    """Claude Code process adapter.

    The supervisor owns lifecycle policy; this adapter owns CLI-specific argv,
    capability checks, and stream semantics. A future Codex adapter can satisfy
    the same small surface without branching the hub's lifecycle code.
    """

    name = "claude"

    def binary(self) -> List[str]:
        return agent_binary()

    def build_spawn_argv(self, **kwargs) -> List[str]:
        return build_spawn_argv(_runtime=self, **kwargs)

    def diagnostics(self, timeout: float = 5.0) -> Dict[str, Any]:
        argv = self.binary()
        override = bool(os.environ.get("TRIO_AGENT_CMD", "").strip())
        executable = shutil.which(argv[0]) if argv else None
        result: Dict[str, Any] = {
            "provider": self.name,
            "command": argv,
            "executable": executable or "",
            "available": bool(executable),
            "authenticated": None,
            "auth_method": "",
            "version": "",
            "ready": False,
            "detail": "",
            "override": override,
        }
        if not executable:
            result["detail"] = f"{argv[0] if argv else 'claude'} was not found on PATH"
            return result
        if override:
            result.update(ready=True, detail="custom agent command configured")
            return result
        try:
            version = subprocess.run(
                [argv[0], "--version"], check=False, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            result["version"] = (version.stdout or version.stderr).strip()
            if version.returncode != 0:
                result["detail"] = "Claude Code version check failed"
                return result
            auth = subprocess.run(
                [argv[0], "auth", "status", "--json"], check=False, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            payload = json.loads(auth.stdout or "{}") if auth.returncode == 0 else {}
            result["authenticated"] = bool(payload.get("loggedIn"))
            result["auth_method"] = str(payload.get("authMethod") or "")
            result["ready"] = bool(result["authenticated"])
            result["detail"] = ("ready" if result["ready"] else
                                "Claude Code is not authenticated; run `claude login`")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            result["detail"] = f"Claude Code health check failed: {exc}"
        return result


# ── cross-process agent ownership ───────────────────────────────────────────
#
# Every liveness check on the spawn path reads AgentSupervisor._procs, which is
# memory belonging to ONE hub process. That is correct within a process and
# useless between them: a second nth_web against the same database starts with
# an empty registry, concludes the agent is dead, and spawns it again. The
# result is two live processes sharing one member_id and one channel identity,
# a reclaim_secret rotation apart — observed in the field as two Frost and two
# Atlas processes, each pair split across two hubs.
#
# The database already records the owning pid (`agents.pid`), so ownership was
# always recoverable across processes; nothing consulted it. These helpers do.


OWNER_CACHE_SECONDS = 2.0

# The phrase nth_web's build_agent_preamble puts in every managed agent's
# --append-system-prompt. Matching on this rather than a bare id means another
# agent's prompt merely NAMING this agent cannot be mistaken for being it.
# Keep in sync with nth_web.build_agent_preamble.
AGENT_ID_MARKER = "Your Trio member_id is {agent_id}"


class ForeignAgentError(RuntimeError):
    """A live process for this agent exists under a supervisor that isn't us."""

    def __init__(self, agent_id: str, pid: int):
        super().__init__(
            f"agent {agent_id} already runs as pid {pid} under another "
            f"supervisor; refusing to spawn a duplicate")
        self.agent_id = agent_id
        self.pid = pid


def pid_alive(pid: int) -> bool:
    """Does this pid name a live process?

    The pid <= 0 guard is not defensive padding: os.kill(0, 0) SUCCEEDS — it
    signals the caller's own process group — so an absent pid read from the
    database as NULL and coerced to 0 would otherwise report "alive" forever,
    and whatever it guards could never be reclaimed.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # CPython maps os.kill on Windows to TerminateProcess, which IGNORES
        # the signal argument. os.kill(pid, 0) there does not probe the
        # process, it KILLS it — so the liveness check would destroy the very
        # agent it was asked about. setup.sh advertises Windows Git Bash, so
        # this path is reachable. tasklist is the probe that doesn't shoot.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists and belongs to another user. Existence is the question.
        return True
    except OSError:
        return False
    return True


# Retained so nth_web's lease can ask the same question through the name it
# already imports; the underscore version was reaching across a module
# boundary for what is plainly shared ownership vocabulary.
_pid_alive = pid_alive


def _pid_ps(pid: int) -> tuple:
    """(process state, command line) for this pid, or ("", "") if unreadable.

    Both in one ps because the state is only needed alongside the command
    line, and forking twice to learn two fields about the same process is
    waste on a path the router walks.
    """
    try:
        out = subprocess.run(["ps", "-o", "state=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ("", "")
    if out.returncode != 0:
        return ("", "")
    line = out.stdout.strip()
    if not line:
        return ("", "")
    state, _, command = line.partition(" ")
    return (state.strip(), command.strip())


def _pid_cmdline(pid: int) -> str:
    """This pid's command line, or "" when it can't be read."""
    return _pid_ps(pid)[1]


def _is_zombie(state: str) -> bool:
    """A reaped-but-not-waited process is NOT running.

    os.kill(pid, 0) succeeds on a zombie — the pid stays in the process table
    until its parent waits — so liveness alone calls a dead agent alive. That
    is not academic: an agent that exits while its hub is busy sits as a
    zombie, and treating it as the live owner of its identity would refuse
    every respawn for as long as nobody reaps it. It also made reclaim report
    failure immediately after successfully killing something.
    """
    return state.upper().startswith("Z")


def _really_running(pid: int) -> bool:
    """Alive AND not a zombie. See _is_zombie for why the distinction matters."""
    if not pid_alive(pid):
        return False
    return not _is_zombie(_pid_ps(pid)[0])


def pid_owns_agent(pid: Optional[int], agent_id: str) -> bool:
    """True when `pid` is a live process that IS this agent.

    Liveness alone is the wrong test in both directions. Pids get recycled, so
    a stale row can name a pid that now belongs to something unrelated —
    treating that as the agent would strand it permanently, unspawnable. Every
    agent carries its own id in the preamble baked into its argv, so the
    command line is a positive identity check that survives recycling.

    When the command line can't be read (unsupported platform, hardened
    process), fall back to bare liveness. Spawning a duplicate is the failure
    this path exists to prevent, so ambiguity has to resolve to "owned" — a
    wrongly-refused spawn is visible and recoverable, a duplicate identity is
    neither.
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if not pid_alive(pid):
        return False
    state, cmd = _pid_ps(pid)
    if _is_zombie(state):
        return False
    if not cmd:
        return True
    # Anchored on the preamble's own wording, not a bare substring search.
    # Agent ids appear inside --append-system-prompt, which begins with the
    # operator's base_prompt — so an operator who writes "coordinate with
    # ag_abc123" puts agent A's id into agent B's argv. If A then dies and its
    # recorded pid is recycled onto B's process, a bare `agent_id in cmd`
    # reports A as alive and strands it unspawnable forever. Only the hub
    # writes this phrase, and it writes it once per process.
    return AGENT_ID_MARKER.format(agent_id=agent_id) in cmd


def agent_binary() -> List[str]:
    """The base argv for launching an agent. Overridable via $TRIO_AGENT_CMD
    (shell-split) so tests can point at a fake stream-json agent. Defaults to
    the real headless Claude Code CLI.

    Because this swaps the launched executable, a non-empty override is logged
    once to stderr — an unexpected value in production is an arbitrary-command
    vector the operator should see."""
    global _warned_override
    raw = os.environ.get("TRIO_AGENT_CMD", "").strip()
    if raw:
        if not _warned_override:
            sys.stderr.write(
                f"[nth_supervisor] NOTE: TRIO_AGENT_CMD override active — "
                f"launching agents via: {raw!r}\n")
            _warned_override = True
        return shlex.split(raw)
    return ["claude"]


def build_spawn_argv(
    *,
    model: str = "",
    system_prompt: str = "",
    mcp_config: str = "",
    resume_session_id: str = "",
    permission_mode: str = "auto",
    extra_dirs: Optional[List[str]] = None,
    disallowed_tools: str = f"AskUserQuestion,{PERMISSION_PROMPT_TOOL}",
    allowed_tools: str = MANAGED_ALLOWED_TOOLS,
    effort: str = "",
    _runtime: Optional[ClaudeRuntime] = None,
) -> List[str]:
    """Assemble the headless `claude -p` command for one agent.

    Streaming JSON both ways keeps the session conversational across turns and
    lets us capture the session_id (for --resume) from the init event. We drive
    the JSON stream, NOT a pseudo-terminal — no TTY scraping.

    `effort` is the reasoning/thinking level (low|medium|high|xhigh|max); more
    effort = more planning before acting, which helps weaker models drive tools.
    """
    argv = list(_runtime.binary() if _runtime is not None else agent_binary())
    argv += [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode,
    ]
    if effort:
        argv += ["--effort", effort]
    if disallowed_tools:
        argv += ["--disallowedTools", disallowed_tools]
    if allowed_tools:
        argv += ["--allowedTools", allowed_tools]
    if model:
        argv += ["--model", model]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
        # Only wire the approval gate when there's an MCP server to resolve it
        # against, and only when the mode can actually produce a prompt —
        # bypassPermissions never asks, so the flag would be dead weight.
        if permission_mode != "bypassPermissions":
            argv += ["--permission-prompt-tool", PERMISSION_PROMPT_TOOL]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    # Attachments live under one shared ATTACH_DIR root, but --add-dir grants
    # the agent's raw Read tool filesystem access with NO trio-level
    # visibility check — trio's can_see/DM-withholding model doesn't apply to
    # it. Adding the WHOLE root here (as this used to do) let any agent read
    # every OTHER channel's uploaded attachments too, not just its own
    # (LOTC/Aragorn). Callers must pass the specific channel-scoped
    # subdirectories this agent is actually allowed to see; ATTACH_DIR itself
    # is still ensured so uploads have somewhere to land.
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    add_dirs = {d for d in (extra_dirs or []) if d}
    for d in add_dirs:
        argv += ["--add-dir", d]
    return argv


def build_mcp_config(nth_server_path: str, python_cmd: str = "") -> str:
    """Inline JSON for `claude --mcp-config` that gives a spawned agent the Trio
    MCP tools (stdio), pointed at this repo's nth_server.py. Returned as a
    compact JSON string (claude accepts inline config or a file path).

    NOTE: enabling this makes the agent call trio_connect itself, which mints a
    NEW member_id — the identity-reclaim path (agents connect AS their agent_id)
    must land alongside wiring this in, or it reproduces bug B1 (duplicate
    member on connect). See proposals/unified-interface.md § Agent identity.
    """
    py = python_cmd or sys.executable
    return json.dumps({
        "mcpServers": {
            "nth-trio": {
                "type": "stdio",
                "command": py,
                "args": [nth_server_path],
            }
        }
    }, separators=(",", ":"))


class AgentProc:
    """A live agent OS process + its reader threads. One thread parses the
    stream-json stdout (capturing session_id from the init event, forwarding
    events); a second DRAINS stderr into a bounded ring buffer so a chatty
    agent can't deadlock on a full stderr pipe."""

    def __init__(self, agent_id: str, argv: List[str],
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 on_session: Optional[Callable[[str, str], None]] = None,
                 cwd: str = ""):
        self.agent_id = agent_id
        self.argv = argv
        self.on_event = on_event
        self.on_session = on_session
        self.cwd = cwd
        self.session_id: str = ""
        self._session_evt = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self._readers: List[threading.Thread] = []
        self._stderr: Deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            cwd=(self.cwd or None),
        )
        self._readers = [
            threading.Thread(target=self._read_loop, daemon=True),
            threading.Thread(target=self._stderr_loop, daemon=True),
        ]
        for t in self._readers:
            t.start()

    def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            # Capture session_id from the init system event (first line).
            if not self.session_id:
                sid = evt.get("session_id") or evt.get("sessionId") or ""
                if sid:
                    self.session_id = sid
                    # Persist immediately — do NOT rely on the spawn() return
                    # path, which loses a late-arriving id if wait_session timed
                    # out (Sauron: else --resume is skipped and memory is lost).
                    if self.on_session is not None:
                        try:
                            self.on_session(self.agent_id, sid)
                        except Exception:
                            pass
                    # A waiter may treat initialization as fully durable. Release
                    # it only after the persistence callback has completed.
                    self._session_evt.set()
            if self.on_event is not None:
                try:
                    self.on_event(self.agent_id, evt)
                except Exception:
                    pass

    def _stderr_loop(self) -> None:
        """Drain stderr so the OS pipe buffer can't fill and block the child.
        Kept as a bounded tail for ST_ERRORED diagnostics."""
        if self.proc is None or self.proc.stderr is None:
            return
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip("\n"))

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def wait_session(self, timeout: float = 10.0) -> str:
        """Block until the init event yields a session_id (or timeout)."""
        self._session_evt.wait(timeout)
        return self.session_id

    def send_user(self, text: str) -> bool:
        """Feed a user message into the agent's stream-json stdin."""
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            return False
        msg = {"type": "user",
               "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    @property
    def pid(self) -> Optional[int]:
        # Valid only while the process is alive — Popen retains a stale/recycled
        # pid after exit, so don't hand that to callers (Sauron).
        if self.proc and self.proc.poll() is None:
            return self.proc.pid
        return None

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self, grace: float = 3.0) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=grace)
        except Exception:
            pass


class AgentSupervisor:
    """Owns all live AgentProc handles and keeps the `agents` DB row in sync.

    Deterministic and process-local: the hub holds one of these. A per-agent
    lock serializes lifecycle ops (spawn/hibernate/wake/stop) on the SAME agent
    so a stop() can't interleave a slow spawn() and leave the DB row claiming
    'running' with a dead pid (Sauron). A short global lock guards only the
    shared dicts; blocking process I/O happens outside it.
    """

    def __init__(self, db_path: Path = DB_PATH,
                 on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 runtime: Optional[ClaudeRuntime] = None):
        self.db_path = db_path
        self.on_event = on_event
        self.runtime = runtime or ClaudeRuntime()
        self._procs: Dict[str, AgentProc] = {}
        # Agents whose spawn is in flight. reconcile() must not reap these:
        # their handle is registered before the process exists.
        self._starting: set = set()
        # External callers' "spawn about to happen" reservations — see
        # reserve_starting(). Deliberately separate from _starting, which is
        # spawn()'s own internal bookkeeping and unconditionally discarded in
        # its finally; refcounted (id -> count) so nested/overlapping
        # reservations for the same id don't clobber each other.
        self._reserved: Dict[str, int] = {}
        self._pending: Dict[str, Deque[Dict[str, Any]]] = {}
        self._compacting: set[str] = set()
        self._models: Dict[str, str] = {}
        # Per-turn state for the opt-in request log, all keyed by agent_id and
        # all cleared together by _forget_turn_log().
        #   _req_seq  1-based index of the current turn's API requests
        #   _turn_key a per-TURN join id. Deliberately not session_id: Claude
        #             stamps one session_id on every event of every turn (it is
        #             the --resume handle), so using it would give every turn in
        #             an agent's life the same key and make requests
        #             unjoinable to their rollup.
        #   _last_msg the last API response id seen, to collapse the several
        #             `assistant` events one response can produce into one entry
        self._req_seq: Dict[str, int] = {}
        self._turn_key: Dict[str, str] = {}
        self._turn_count: Dict[str, int] = {}
        self._last_msg: Dict[str, str] = {}
        self._agent_locks: Dict[str, threading.RLock] = {}
        # agent_id -> (monotonic_expiry, owner_pid_or_None). See
        # foreign_owner_pid: the answer costs a fork and is asked per message.
        self._owner_cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._accepting = True

    # ── db helpers ──
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _plock(self, agent_id: str) -> threading.RLock:
        with self._lock:
            lk = self._agent_locks.get(agent_id)
            if lk is None:
                lk = self._agent_locks[agent_id] = threading.RLock()
            return lk

    def plock(self, agent_id: str) -> threading.RLock:
        """Public accessor for the per-agent lifecycle lock. spawn/hibernate/
        wake/stop all serialize on this internally; external callers that read
        is_running() and then act on the result (e.g. nth_web.py's wake_agent,
        which rotates reclaim_secret only when the agent looks dead) must hold
        it across the whole check-then-act, or a spawn() in flight on another
        thread can finish — and hand the process a secret — after the read but
        before the rotation, leaving the live process holding a secret the DB
        no longer has (B1: a router wake racing a fresh /api/agents create)."""
        return self._plock(agent_id)

    def _turn_key_locked(self, agent_id: str, session_id: Any) -> str:
        """The current turn's join id, minting one if a turn just started.

        NOT session_id: Claude stamps a single session_id on every event of
        every turn (it is the --resume handle), so it identifies the agent's
        whole life, not one turn. Requests and their rollup need a key that
        changes per turn or they cannot be joined. Caller must hold _lock.
        """
        key = self._turn_key.get(agent_id)
        if not key:
            count = self._turn_count.get(agent_id, 0) + 1
            self._turn_count[agent_id] = count
            # _RUN_NONCE is essential, not decoration: _turn_count is in-memory
            # and restarts at 0, while session_id is the PERSISTED --resume
            # handle and the log survives across restarts with 24h retention.
            # Without it, the first turn after a hub restart would mint the same
            # key the first turn before it used, in the same file — reviving the
            # very collision the per-turn key exists to prevent.
            key = f"{str(session_id or agent_id)}#{_RUN_NONCE}{count}"
            self._turn_key[agent_id] = key
        return key

    def _forget_turn_log_locked(self, agent_id: str) -> None:
        """Drop per-turn request-log state. Caller must hold _lock.

        Only the `result` event ends a turn normally. Every path that abandons
        one without a result — hibernate, stop, clear, reconcile after a crash,
        shutdown — must call this, or the stale request counter leaks into the
        NEXT turn and inflates exactly the tool-loop signal this log exists to
        report. (_turn_count is deliberately NOT reset: it only has to keep
        minting distinct keys for this agent.)
        """
        self._req_seq.pop(agent_id, None)
        self._turn_key.pop(agent_id, None)
        self._last_msg.pop(agent_id, None)

    def _forget_pending(self, agent_id: str) -> None:
        with self._lock:
            self._pending.pop(agent_id, None)
            self._compacting.discard(agent_id)
            # Every turn-abandonment path already funnels through here.
            self._forget_turn_log_locked(agent_id)

    def _handle_event(self, agent_id: str, evt: Dict[str, Any],
                      source: Optional[AgentProc] = None) -> None:
        """Keep activity/state current, then forward the event to the hub.

        Claude emits a terminal ``result`` event after a turn.  Treat that as
        idle (eligible for hibernation); all other output is active work.
        """
        with self._lock:
            if source is not None and self._procs.get(agent_id) is not source:
                return
            compacting = agent_id in self._compacting
            if evt.get("type") == "result":
                self._compacting.discard(agent_id)
        state = ST_IDLE if evt.get("type") == "result" else (
            ST_COMPACTING if compacting else ST_RUNNING)
        # A reader thread can deliver its final buffered event while stop() /
        # shutdown() is tearing the process down. Never let that late event
        # resurrect a deliberately stopped DB row.
        if self.is_running(agent_id):
            try:
                self._set_state(agent_id, state)
            except Exception:
                pass
        if evt.get("type") == "assistant":
            # Context occupancy must come from a single API response's own
            # usage, not the turn-level `result` event: `result.usage` is
            # ACCUMULATED across every internal API call the turn made (tool
            # round-trips each add another request's cache_read_input_tokens
            # on top), so it overcounts by roughly the number of tool calls
            # and can peg near 100% on a mostly-empty context window. Each
            # `assistant` event's message.usage is that one request's actual
            # prompt size — the freshest one before the turn ends is the
            # turn's real end-of-turn context size.
            message = evt.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                try:
                    tokens = max(0, int(usage.get("input_tokens") or 0)
                                 + int(usage.get("cache_creation_input_tokens") or 0)
                                 + int(usage.get("cache_read_input_tokens") or 0))
                    with self._lock:
                        window = context_window_for(self._models.get(agent_id, ""))
                    pct = max(0.0, min(100.0, round(
                        100.0 * tokens / window, 1)))
                    self._set_context(agent_id, pct, tokens)
                except Exception:
                    pass
                # Same event, different purpose: message.usage is ONE request's
                # billed tokens, which is the granularity that shows a tool loop
                # re-sending a large cached prompt on every round-trip. No-op
                # unless the operator opted in.
                if nrl.enabled():
                    msg_id = str(message.get("id") or "") if isinstance(
                        message, dict) else ""
                    seq, turn_key, spawn_model = 0, "", ""
                    with self._lock:
                        # One API response can arrive as SEVERAL `assistant`
                        # events — one per content block (text, then each
                        # tool_use) — all carrying the same message.id AND the
                        # same cumulative usage. Counting each as its own
                        # request would multiply a turn's apparent request count
                        # and token total by the number of blocks. Collapse them
                        # on the response id; only a new id is a new request.
                        duplicate = bool(msg_id) and (
                            self._last_msg.get(agent_id) == msg_id)
                        if not duplicate:
                            if msg_id:
                                self._last_msg[agent_id] = msg_id
                            seq = self._req_seq.get(agent_id, 0) + 1
                            self._req_seq[agent_id] = seq
                            turn_key = self._turn_key_locked(
                                agent_id, evt.get("session_id"))
                            # `model` must mean ONE thing in every entry or
                            # by_model groups two namespaces together. The
                            # configured spawn string ("opus") is the shared
                            # vocabulary — Codex only has that — so it is the
                            # column, and the resolved API id goes in detail.
                            spawn_model = self._models.get(agent_id, "")
                    if not duplicate:
                        api_model = (message.get("model") or "") if isinstance(
                            message, dict) else ""
                        stop_reason = message.get("stop_reason") if isinstance(
                            message, dict) else None
                        detail = {k: v for k, v in
                                  (("stop_reason", stop_reason),
                                   ("api_model", api_model)) if v}
                        nrl.record_request(
                            agent_id, "claude", usage, seq=seq, turn=turn_key,
                            model=spawn_model, detail=detail or None)
        if evt.get("type") == "result":
            # result.usage is the turn's ACCUMULATED billed tokens — the right
            # figure for consumption-over-time (unlike per-request context size).
            record_token_event(agent_id, evt.get("usage"))
            if nrl.enabled():
                with self._lock:
                    requests = self._req_seq.get(agent_id, 0)
                    model = self._models.get(agent_id, "")
                    turn_key = self._turn_key_locked(
                        agent_id, evt.get("session_id"))
                    # The turn is over: the next request starts a new one.
                    self._forget_turn_log_locked(agent_id)
                nrl.record_turn(
                    agent_id, "claude", evt.get("usage"), model=model,
                    turn=turn_key,
                    detail={"requests": requests,
                            "session_id": str(evt.get("session_id") or ""),
                            "duration_ms": evt.get("duration_ms"),
                            "num_turns": evt.get("num_turns"),
                            "subtype": evt.get("subtype")})
            # Opportunistically refresh the account usage % (rate-gated, runs
            # on a background thread — never blocks turn handling).
            maybe_refresh_usage_cli()
            self._bridge_result(agent_id, evt)
        if self.on_event is not None:
            self.on_event(agent_id, evt)

    def _bridge_result(self, agent_id: str, evt: Dict[str, Any]) -> None:
        """Publish a plain headless result when the model skipped Trio tools.

        MCP-authored replies win: if the agent posted in the source channel
        after this turn was fed, the result is only lifecycle metadata and is
        not duplicated. Otherwise the successful result becomes the reply.
        """
        with self._lock:
            pending = self._pending.get(agent_id)
            context = pending.popleft() if pending else None
            if pending is not None and not pending:
                self._pending.pop(agent_id, None)
        if context is None or evt.get("is_error"):
            return
        content = evt.get("result")
        if not isinstance(content, str) or not content.strip():
            return
        channel = context["channel"]
        baseline = context["baseline"]
        db = self._db()
        try:
            already_posted = db.execute(
                "SELECT 1 FROM messages WHERE channel=? AND member_id=? AND id>? LIMIT 1",
                (channel, agent_id, baseline)).fetchone()
            if already_posted:
                return
            agent = db.execute("SELECT name FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                return
            recipients: List[str] = []
            if channel == AGENT_INBOX_CHANNEL:
                # Use the specific message THIS turn was fed to answer — never
                # infer the recipient by scanning current inbox history, which
                # can pick up a different, later sender's DM (see bug link
                # above).
                source_sender = context.get("source_sender")
                if source_sender:
                    recipients = [source_sender]
                else:
                    recipients = [r["id"] for r in db.execute(
                        "SELECT id FROM members WHERE channel=? AND kind='human' "
                        "AND active=1 ORDER BY joined_at", (channel,)).fetchall()]
            now = now_iso()
            db.execute(
                "INSERT INTO messages (channel,member_id,member_name,content,mentions,"
                "recipients,created_at) VALUES (?,?,?,?,?,?,?)",
                (channel, agent_id, agent["name"], content.strip(),
                 json.dumps(recipients) if recipients else "",
                 json.dumps(recipients) if recipients else "[]", now))
            db.execute(
                "UPDATE members SET last_seen=? WHERE channel=? AND id=?",
                (now, channel, agent_id))
            db.commit()
        except sqlite3.Error:
            # Supervisor-only unit schemas and pre-migration databases may not
            # have the messaging tables yet. Lifecycle must remain unaffected.
            pass
        finally:
            db.close()

    def _persist_session(self, agent_id: str, session_id: str) -> None:
        """Called from the reader thread the instant a session_id is captured,
        so --resume continuity survives even a slow init (Sauron crit)."""
        if not session_id:
            return
        db = self._db()
        try:
            columns = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
            if "runtime_ref" in columns:
                db.execute(
                    "UPDATE agents SET session_id = ?, runtime_ref = ?, "
                    "last_active_at = ? WHERE id = ?",
                    (session_id, session_id, now_iso(), agent_id))
            else:
                db.execute(
                    "UPDATE agents SET session_id = ?, last_active_at = ? WHERE id = ?",
                    (session_id, now_iso(), agent_id))
            self._register_sessions(db, agent_id, session_id)
            db.commit()
        finally:
            db.close()

    def _register_sessions(self, db, agent_id: str, fingerprint: str) -> None:
        """Anchor the activity hooks to this agent's channels.

        The hooks find a session by FINGERPRINT — the raw Claude Code session
        id — and stamp last_seen/last_tool_* on it. Until this ran, the only
        thing that ever created a sessions row was the agent choosing to call
        trio_connect. An agent that posts with trio_send and never connects
        (perfectly legal — it is handed its member id at spawn) therefore had
        no row for the hooks to write to, so it sat at whatever its member row
        last said and never showed working or a tool. Observed live: of two
        agents created ninety seconds apart, the one that connected reported
        status correctly and the one that did not read idle forever.

        Status is infrastructure, so it cannot depend on the agent's goodwill.
        We know the fingerprint here (this is the instant we capture it) and
        the placements are just the agent's members rows, so we register the
        row ourselves. If the agent does connect later it mints its own, newer
        row and the hooks follow that one — the scope always takes the newest
        live session per channel.
        """
        if not fingerprint:
            return
        try:
            placements = [r[0] for r in db.execute(
                "SELECT channel FROM members WHERE id = ? AND active = 1",
                (agent_id,))]
            for channel in placements:
                exists = db.execute(
                    "SELECT 1 FROM sessions WHERE member_id = ? AND channel = ? "
                    "AND fingerprint = ? AND revoked_at IS NULL LIMIT 1",
                    (agent_id, channel, fingerprint)).fetchone()
                if exists:
                    continue
                now = now_iso()
                # last_read starts at the member's watermark, not 0: this row is
                # a telemetry anchor, and seeding it at 0 would advertise every
                # message in the channel as unread for a session that has in
                # fact read up to the member watermark.
                # role='anchor', not 'primary'. Nothing is ever handed this
                # token — it exists so the activity/turn hooks have somewhere to
                # stamp status. Calling it 'primary' put it in the blast radius
                # of a reclaim's displacement sweep, which revokes live primary
                # sessions for the identity; that matters when connect's own
                # fingerprint is empty and this anchor is the only row the hooks
                # can match, because the agent then reads idle forever after its
                # first reclaim. As a bonus, a leaked anchor token can no longer
                # send: every capability check rejects role != 'primary'.
                db.execute(
                    "INSERT INTO sessions (session_token, member_id, channel, "
                    "role, pid, fingerprint, connected_at, last_seen, last_read) "
                    "VALUES (?, ?, ?, 'anchor', NULL, ?, ?, ?, "
                    " COALESCE((SELECT last_read FROM members WHERE id = ? "
                    "           AND channel = ?), 0))",
                    ("s_" + secrets.token_hex(16), agent_id, channel,
                     fingerprint, now, now, agent_id, channel))
        except sqlite3.Error:
            # Telemetry must never take down a spawn. Without the row the agent
            # simply reports as it did before this change.
            pass

    def _set_state(self, agent_id: str, state: str, *,
                   pid: Optional[int] = None, session_id: Optional[str] = None,
                   clear_pid: bool = False, clear_session: bool = False) -> int:
        """Update an agent's row. Returns rows affected (0 = unknown agent)."""
        db = self._db()
        try:
            columns = {r[1] for r in db.execute("PRAGMA table_info(agents)")}
            sets = ["state = ?", "last_active_at = ?"]
            vals: List[Any] = [state, now_iso()]
            if clear_pid:
                sets.append("pid = NULL")
            elif pid is not None:
                sets.append("pid = ?")
                vals.append(pid)
            if session_id:
                sets.append("session_id = ?")
                vals.append(session_id)
                if "runtime_ref" in columns:
                    sets.append("runtime_ref = ?")
                    vals.append(session_id)
            elif clear_session:
                sets.append("session_id = NULL")
                if "runtime_ref" in columns:
                    sets.append("runtime_ref = NULL")
            vals.append(agent_id)
            cur = db.execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals)
            db.commit()
            if clear_pid or pid is not None:
                # We just changed who owns this process, so any memoised answer
                # about that predates our own action.
                self._forget_owner(agent_id)
            return cur.rowcount
        finally:
            db.close()

    def _set_context(self, agent_id: str, pct: float, tokens: int) -> None:
        """Persist a context-fullness reading from a turn's result event.

        Best-effort: swallows DB errors rather than risking the reader
        thread that's mid-delivery of the actual turn result.
        """
        db = self._db()
        try:
            db.execute(
                "UPDATE agents SET context_pct = ?, context_tokens = ? WHERE id = ?",
                (pct, tokens, agent_id))
            db.commit()
        except sqlite3.Error:
            pass
        finally:
            db.close()

    # ── lifecycle ──
    def spawn(self, agent_id: str, *, model: str = "", system_prompt: str = "",
              mcp_config: str = "", resume_session_id: str = "", effort: str = "",
              cwd: str = "", permission_profile: str = "balanced",
              extra_dirs: Optional[List[str]] = None,
              provider: str = "",
              session_timeout: float = 10.0) -> AgentProc:
        """Launch (or resume) an agent process and sync its DB row. Serialized
        per-agent. Blocks briefly to capture the session_id from the init
        event; the reader thread also persists it directly, so a slow init
        doesn't lose --resume continuity.

        cwd, when non-empty, becomes the spawned process's working directory
        (Popen cwd=). Empty falls back to the supervisor's inherited cwd, the
        pre-cwd-threading behavior.

        provider names the runtime to launch under. This supervisor backs
        exactly one, so anything else is refused rather than silently launched
        as a Claude agent — an agent row created for another provider must not
        come up as the wrong kind of process."""
        if provider and provider.lower() != self.runtime.name:
            raise ValueError(f"unsupported runtime provider: {provider}")
        with self._plock(agent_id):
            if not self._accepting:
                raise RuntimeError("agent supervisor is shutting down")
            with self._lock:
                existing = self._procs.get(agent_id)
                if existing and existing.alive():
                    return existing
            # _procs answers "is it running HERE". The database answers "is it
            # running AT ALL" — and a second hub on the same db has its own
            # empty _procs, so without this check both hubs pass the guard
            # above and both spawn. This is the single point where a duplicate
            # identity can be created, so it is the single point that has to
            # refuse.
            foreign = self.foreign_owner_pid(agent_id)
            if foreign is not None:
                raise ForeignAgentError(agent_id, foreign)
            permission_mode = PERMISSION_MODES.get(permission_profile, "auto")
            argv = self.runtime.build_spawn_argv(
                model=model, system_prompt=system_prompt, mcp_config=mcp_config,
                resume_session_id=resume_session_id, effort=effort,
                permission_mode=permission_mode, extra_dirs=extra_dirs)
            proc = AgentProc(
                agent_id, argv,
                on_event=lambda aid, evt: self._handle_event(aid, evt, source=proc),
                on_session=self._persist_session,
                cwd=cwd)
            with self._lock:
                self._models[agent_id] = model
            self._set_state(agent_id, ST_SPAWNING)
            # Register the proc BEFORE start() so the reader thread's early
            # events find themselves in _procs and pass the stale-source guard
            # in _handle_event. Registering after start() dropped any event
            # emitted between start() and the assignment (the guard saw
            # _procs.get(agent_id) as None/old != source and returned). The
            # spawn-died branch below still removes the handle on failure.
            with self._lock:
                self._procs[agent_id] = proc
                self._starting.add(agent_id)
            try:
                proc.start()
                sid = proc.wait_session(session_timeout)
            except Exception:
                # The row already says 'spawning'. Leaving it there would show
                # a permanently-starting agent, and the stale handle would make
                # is_running() lie — so unwind both before the caller sees the
                # error (a missing binary raises straight out of Popen).
                with self._lock:
                    if self._procs.get(agent_id) is proc:
                        del self._procs[agent_id]
                self._set_state(agent_id, ST_ERRORED, clear_pid=True)
                raise
            finally:
                with self._lock:
                    self._starting.discard(agent_id)
            if not proc.alive():
                # Spawn died before/around init — drop the dead handle so the
                # registry doesn't hold a zombie entry (Sauron).
                with self._lock:
                    if self._procs.get(agent_id) is proc:
                        del self._procs[agent_id]
                self._set_state(agent_id, ST_ERRORED, clear_pid=True)
            else:
                self._set_state(agent_id, ST_RUNNING, pid=proc.pid,
                                session_id=sid or None)
            return proc

    def hibernate(self, agent_id: str) -> bool:
        """Stop the process but keep session_id → state=sleeping. Revived later
        with --resume, memory intact. Returns False if the agent was neither
        running nor a known row (no-op)."""
        with self._plock(agent_id):
            self._refuse_if_foreign(agent_id)
            with self._lock:
                proc = self._procs.pop(agent_id, None)
            if proc:
                proc.stop()
            self._forget_pending(agent_id)
            rows = self._set_state(agent_id, ST_SLEEPING, clear_pid=True)
            return bool(proc) or rows > 0

    def wake(self, agent_id: str, **spawn_kw) -> Optional[AgentProc]:
        """Resume a sleeping agent from its persisted session_id. If the agent
        has no session_id yet (never spawned), this is a cold first start."""
        db = self._db()
        try:
            # SELECT * (not a column list) so a partial/older agents table —
            # e.g. one without permission_profile — still wakes instead of
            # raising "no such column"; optional fields read via _row_value.
            row = db.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        # Any turn context queued against a prior (possibly crashed) process
        # for this agent_id no longer corresponds to a real in-flight turn —
        # the fresh process starts with no pending results to bridge.
        self._forget_pending(agent_id)
        return self.spawn(
            agent_id,
            model=spawn_kw.get("model", row["model"] or ""),
            system_prompt=spawn_kw.get("system_prompt", row["base_prompt"] or ""),
            mcp_config=spawn_kw.get("mcp_config", ""),
            effort=spawn_kw.get("effort", _row_value(row, "effort")),
            cwd=spawn_kw.get("cwd", _row_value(row, "cwd")),
            # Read the profile back from the row like model/effort/cwd already
            # are — otherwise every wake silently reverted the agent to
            # "balanced" and an edited permission profile never took effect.
            # (The Codex runtime already persisted it this way.)
            permission_profile=spawn_kw.get(
                "permission_profile", _row_value(row, "permission_profile", "balanced")),
            extra_dirs=spawn_kw.get("extra_dirs"),
            resume_session_id=row["session_id"] or "")

    def _refuse_if_foreign(self, agent_id: str) -> None:
        """Raise ForeignAgentError if a process we do not own is alive here.

        Guarding spawn() alone is half an invariant. `agents.pid` is the ONLY
        cross-process ownership record, so a hub that nulls it for an agent it
        does not own destroys the evidence — and every later foreign_owner_pid
        returns None, so the next spawn creates exactly the duplicate this
        module exists to prevent.

        The immediate damage is as bad as the eventual damage. _procs.pop
        returns None for an agent we never spawned, so no signal is ever sent:
        the row says stopped while the process runs on, reachable by nobody and
        killable through no interface. stop() would even return True, because
        it reports success on `rows > 0`.

        Every writer of clear_pid=True calls this first.
        """
        foreign = self.foreign_owner_pid(agent_id)
        if foreign is not None:
            raise ForeignAgentError(agent_id, foreign)

    def stop(self, agent_id: str) -> bool:
        """Deliberately halt an agent (state=stopped). Returns False on no-op
        (unknown agent, not running). Raises ForeignAgentError if the live
        process belongs to another hub."""
        with self._plock(agent_id):
            self._refuse_if_foreign(agent_id)
            with self._lock:
                proc = self._procs.pop(agent_id, None)
                self._models.pop(agent_id, None)
            if proc:
                proc.stop()
            self._forget_pending(agent_id)
            rows = self._set_state(agent_id, ST_STOPPED, clear_pid=True)
            return bool(proc) or rows > 0

    def interrupt(self, agent_id: str) -> bool:
        """Cut a turn short without ending the session.

        Claude Code has no in-band cancel on its stdin stream, so the only way
        to stop a turn is to end the process. The session_id is deliberately
        KEPT, so the next wake resumes the same transcript with --resume: the
        agent loses the turn it was mid-way through, not its memory. That is
        the difference between interrupt and stop."""
        with self._plock(agent_id):
            self._refuse_if_foreign(agent_id)
            with self._lock:
                proc = self._procs.pop(agent_id, None)
                self._models.pop(agent_id, None)
            if not proc:
                return False
            proc.stop()
            self._forget_pending(agent_id)
            # sleeping, not stopped: an interrupted agent is still deployed and
            # a wake should bring it back where it was.
            self._set_state(agent_id, ST_SLEEPING, clear_pid=True)
            return True

    def clear(self, agent_id: str, **spawn_kw) -> Optional[AgentProc]:
        """Discard transcript continuity and launch a fresh session.

        Durable agent identity and placements remain unchanged; only the Claude
        session id/context is cleared.  The caller supplies the rebuilt Trio
        preamble/MCP config just as it does for wake.
        """
        with self._plock(agent_id):
            # BEFORE the _set_state below, not after spawn() gets to check.
            # clear() destroys session_id, and that is irreversible: against a
            # live process owned by another hub, the spawn() refusal arrives
            # too late — the transcript is already unresumable and the row is
            # parked in ST_STOPPED where the router will not route to it. One
            # click, permanent damage, to an agent this hub never owned.
            self._refuse_if_foreign(agent_id)
            db = self._db()
            try:
                row = db.execute(
                    "SELECT * FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
            finally:
                db.close()
            if row is None:
                return None
            with self._lock:
                proc = self._procs.pop(agent_id, None)
            if proc:
                proc.stop()
            self._forget_pending(agent_id)
            self._set_state(agent_id, ST_STOPPED, clear_pid=True, clear_session=True)
            return self.spawn(
                agent_id,
                model=spawn_kw.get("model", row["model"] or ""),
                effort=spawn_kw.get("effort", _row_value(row, "effort")),
                system_prompt=spawn_kw.get("system_prompt", ""),
                mcp_config=spawn_kw.get("mcp_config", ""),
                cwd=spawn_kw.get("cwd", _row_value(row, "cwd")),
                permission_profile=spawn_kw.get(
                    "permission_profile", _row_value(row, "permission_profile", "balanced")),
                extra_dirs=spawn_kw.get("extra_dirs"),
                resume_session_id="",
            )

    def reclaim(self, agent_id: str, grace: float = 3.0) -> Dict[str, Any]:
        """End the process recorded for this agent and free the identity.

        The escape hatch for an ORPHAN, and the reason the ownership guard is
        safe to have at all. A hub takes SIGTERM, its agents survive as
        reparented processes (measured: they outlive SIGTERM and need
        SIGKILL), and every guard here then correctly refuses to duplicate
        them — which leaves them alive, unfed, and reachable by nobody.
        Detection without reconciliation is a worse operator experience than
        the duplicates it replaced, because at least a duplicate was visible
        and killable from the dashboard.

        Adoption is not on the table: an agent talks over stdin/stdout pipes
        held by the process that spawned it, and those died with the parent.
        No surviving process can speak to an orphan, so the only honest
        recovery is to end it and let the agent be spawned fresh — which the
        caller can then do, because the row no longer names a live pid.

        Deliberately NOT automatic. A live process under a healthy peer hub
        looks identical from here to an orphan under a dead one, so choosing
        to kill it is the operator's call to make, not a side effect of
        routing. Returns what it actually did rather than a bool, because
        "there was nothing to kill" and "killed pid 123" are different
        answers and the dashboard should be able to say which.
        """
        with self._plock(agent_id):
            with self._lock:
                proc = self._procs.pop(agent_id, None)
                self._models.pop(agent_id, None)
            if proc is not None:
                # Ours after all — this is just a stop with a louder name.
                proc.stop(grace=grace)
            db = self._db()
            try:
                row = db.execute(
                    "SELECT pid FROM agents WHERE id = ?", (agent_id,)).fetchone()
            except sqlite3.Error:
                row = None
            finally:
                db.close()
            pid = int(row["pid"]) if row is not None and row["pid"] else 0
            killed = None
            if pid and pid_owns_agent(pid, agent_id):
                killed = pid
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                deadline = time.monotonic() + grace
                while time.monotonic() < deadline and _really_running(pid):
                    time.sleep(0.1)
                if _really_running(pid):
                    # Agents ignore SIGTERM often enough that treating it as
                    # sufficient would leave the identity still taken and the
                    # operator none the wiser.
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    time.sleep(0.2)
            self._forget_pending(agent_id)
            self._forget_owner(agent_id)
            self._set_state(agent_id, ST_STOPPED, clear_pid=True)
            still = bool(killed and _really_running(killed))
            return {"agent_id": agent_id, "killed_pid": killed,
                    "was_local": proc is not None, "still_alive": still}

    def compact(self, agent_id: str, message: str = "") -> bool:
        """Compact a live Claude session, optionally guiding its summary."""
        with self._plock(agent_id):
            with self._lock:
                proc = self._procs.get(agent_id)
                if proc and proc.alive():
                    self._compacting.add(agent_id)
            if not proc or not proc.alive():
                return False
            self._set_state(agent_id, ST_COMPACTING)
            command = "/compact" + (" " + message.strip() if message.strip() else "")
            if proc.send_user(command):
                return True
            with self._lock:
                self._compacting.discard(agent_id)
            self._set_state(agent_id, ST_RUNNING)
            return False

    def feed(self, agent_id: str, channel: str, text: str,
             attachments: Optional[List[str]] = None,
             source_message_id: int = 0, source_sender: str = "") -> bool:
        """Route an inbound channel message into the agent, channel-tagged
        (hybrid context). The agent replies to a specific channel via its
        injected Trio MCP. Returns False if the agent isn't live (the hub is
        responsible for waking a sleeping agent first — see design doc).

        source_message_id/source_sender identify the specific inbound message
        this turn is answering, so a plain (non-Trio-tool) result can be
        bridged to the correct private recipient even if a second inbox
        message from someone else arrives before this turn's result — see
        bugs/2026-08-01-private-fallback-reply-wrong-recipient.md."""
        with self._plock(agent_id):
            if attachments:
                text += "\n\nAttached local files:\n" + "\n".join(attachments)
            with self._lock:
                proc = self._procs.get(agent_id)
            if not proc:
                return False
            baseline = 0
            try:
                db = self._db()
                try:
                    baseline = db.execute(
                        "SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
                finally:
                    db.close()
            except sqlite3.Error:
                pass
            context = {"channel": channel, "baseline": baseline,
                      "source_message_id": source_message_id,
                      "source_sender": source_sender}
            with self._lock:
                self._pending.setdefault(agent_id, collections.deque()).append(context)
            ok = proc.send_user(f"[#{channel}] {text}")
            if ok:
                self._set_state(agent_id, ST_RUNNING)
            else:
                with self._lock:
                    pending = self._pending.get(agent_id)
                    if pending:
                        try:
                            pending.remove(context)
                        except ValueError:
                            pass
                        if not pending:
                            self._pending.pop(agent_id, None)
            return ok

    # ── approvals ──
    # DB-backed, unlike Codex's in-memory approval inbox (nth_codex_runtime.py)
    # — trio_permission_prompt runs inside the spawned `claude` subprocess's
    # own nth_server.py MCP child, a different OS process from whatever hub
    # holds this AgentSupervisor, so the `approvals` table (nth_server.get_db)
    # is the only thing both sides can see.
    def pending_approvals(self) -> List[Dict[str, Any]]:
        db = self._db()
        try:
            columns = {r[1] for r in db.execute("PRAGMA table_info(approvals)")}
            if not columns:
                return []
            rows = db.execute(
                "SELECT * FROM approvals WHERE provider='claude' AND status='pending' "
                "ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    # Decisions the approval inbox accepts. The dashboard offers four; only two
    # are distinct outcomes for THIS gate, so the session-scoped and cancelled
    # forms are normalised rather than rejected. Rejecting them silently left
    # the approval pending and the agent blocked for the full timeout while the
    # operator saw a 404.
    _DECISION_ALIASES = {
        "accept": "accept",
        "acceptForSession": "accept",
        "decline": "decline",
        "cancel": "decline",
    }

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        decision = self._DECISION_ALIASES.get(decision, "")
        if not decision:
            return False
        db = self._db()
        try:
            columns = {r[1] for r in db.execute("PRAGMA table_info(approvals)")}
            if not columns:
                # No such table yet (a hub-only DB nth_server.py hasn't
                # migrated) — nothing to resolve, not an error (LOTC/Ents).
                return False
            cur = db.execute(
                "UPDATE approvals SET status='resolved', decision=?, resolved_at=? "
                "WHERE id=? AND provider='claude' AND status='pending'",
                (decision, now_iso(), approval_id))
            db.commit()
            return cur.rowcount > 0
        finally:
            db.close()

    def is_running(self, agent_id: str) -> bool:
        """Does THIS supervisor own a live process for the agent?

        Deliberately in-memory only, and deliberately not the whole truth: it
        is called per routed message, so it stays cheap. Callers that are about
        to act on a False — spawn it, rotate its secret — must additionally ask
        foreign_owner_pid(), which is the expensive, authoritative check.
        """
        with self._lock:
            proc = self._procs.get(agent_id)
        return bool(proc and proc.alive())

    def foreign_owner_pid(self, agent_id: str) -> Optional[int]:
        """Pid of a live process for this agent that this supervisor does NOT
        own, or None.

        A process in our own _procs is ours to feed, stop and reap, so it is
        never "foreign". Anything else alive belongs to another hub, and the
        only safe move is to leave it be: we cannot feed it (no handle), and
        spawning alongside it is the duplicate-identity bug.
        """
        with self._lock:
            mine = self._procs.get(agent_id)
        if mine is not None and mine.alive():
            return None
        # Memoised briefly. The router calls this per routed message for any
        # agent not running locally, and each miss costs a sqlite connect plus
        # a fork/exec of ps — a subprocess per message, forever, to re-learn a
        # fact that changes at most once per agent lifetime. Short enough that
        # a genuinely departed owner is noticed within a couple of seconds.
        now = time.monotonic()
        with self._lock:
            cached = self._owner_cache.get(agent_id)
            if cached is not None and cached[0] > now:
                return cached[1]
        # connect() is inside the guard, not before it: this runs on the spawn
        # path and from the router loop, so a database that is briefly
        # unreadable must degrade to the pre-existing behaviour rather than
        # raise and wedge every spawn in the process.
        try:
            db = self._db()
        except sqlite3.Error:
            return None
        try:
            row = db.execute(
                "SELECT pid FROM agents WHERE id = ?", (agent_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            db.close()
        if row is None:
            return None
        # No special case for "our own dead handle": reaching here means our
        # handle is not alive, and a row naming a dead pid fails pid_owns_agent
        # on liveness — so a stale row correctly reads as unowned and we keep
        # the right to restart it.
        pid = row["pid"]
        owner = int(pid) if pid_owns_agent(pid, agent_id) else None
        with self._lock:
            self._owner_cache[agent_id] = (
                time.monotonic() + OWNER_CACHE_SECONDS, owner)
        return owner

    def _forget_owner(self, agent_id: str) -> None:
        """Drop the memoised ownership answer for one agent.

        Called wherever this hub changes who owns the process, so the next
        question is answered from the world rather than from a cache that
        predates our own action.
        """
        with self._lock:
            self._owner_cache.pop(agent_id, None)

    def reserve_starting(self, agent_id: str) -> None:
        """Mark agent_id as having a create in flight, BEFORE its `agents` row
        is even committed. Closes the window between that commit (which
        ensure_agent_inboxes' INSERT OR IGNORE can use to make ANY
        non-archived agent routable, not just the one being created) and
        spawn() actually starting: without it, wake_agent() can see
        is_running() == False for an agent seconds away from being spawned
        and rotate its reclaim_secret out from under the preamble the real
        spawn() is about to hand the process (LOTC Sauron/Gandalf, B1
        recurrence). Deliberately a SEPARATE set from `_starting` — spawn()
        adds to and unconditionally discards from `_starting` itself as
        internal bookkeeping (see spawn()), so sharing one set means spawn's
        own cleanup would silently clear a caller's still-active reservation
        the moment spawn() returns, reopening the exact window this exists
        to close. Refcounted so overlapping reserve calls for the same id
        (e.g. a retry) can't have one's release evict the other's guard.
        Always pair with release_starting in a finally, even on error, so a
        failed create doesn't wedge the id."""
        with self._lock:
            self._reserved[agent_id] = self._reserved.get(agent_id, 0) + 1

    def release_starting(self, agent_id: str) -> None:
        with self._lock:
            n = self._reserved.get(agent_id, 0) - 1
            if n <= 0:
                self._reserved.pop(agent_id, None)
            else:
                self._reserved[agent_id] = n

    def is_running_or_starting(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self._starting or agent_id in self._reserved:
                return True
            proc = self._procs.get(agent_id)
        if proc is not None and proc.alive():
            return True
        # A live process under another hub counts as running. Callers use this
        # to decide whether rotating the agent's reclaim_secret is safe, and
        # rotating one out from under a process we don't own is B1 again with a
        # second hub cast as the racing thread — the live agent keeps a secret
        # the database no longer has, and can never reclaim its identity.
        return self.foreign_owner_pid(agent_id) is not None

    def is_busy(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._compacting

    def live_ids(self) -> List[str]:
        with self._lock:
            return [a for a, p in self._procs.items() if p.alive()]

    # ── provider surface ──
    # The hub calls these without caring which runtime backs an agent. They are
    # trivial here because Claude Code is the only provider: it streams results
    # back rather than queueing them, and it exposes no activity log of its own.
    # A second provider is added by giving the hub a dispatcher that implements
    # this same surface and forwards to the right runtime — which is why these
    # exist at all rather than being special-cased at the call sites.

    def provider_for(self, agent_id: str) -> str:
        return self.runtime.name

    def queued_count(self, agent_id: str) -> int:
        """Prompts accepted but not yet started. Always 0: send_user writes
        straight to the process stdin, so there is no queue to be behind."""
        return 0

    def activity(self, agent_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Provider-side activity log. Empty: Claude Code reports through the
        stream this supervisor already turns into channel messages, so there is
        no separate log to surface."""
        return []

    def diagnostics(self, provider: str = "", *, deep: bool = False) -> Dict[str, Any]:
        """Runtime readiness, for the spawn preflight and the UI."""
        if provider and provider.lower() != self.runtime.name:
            return {"provider": provider, "ready": False,
                    "detail": f"unsupported runtime provider: {provider}"}
        return self.runtime.diagnostics()

    def list_models(self, provider: str = "") -> List[Dict[str, Any]]:
        if provider and provider.lower() != self.runtime.name:
            raise ValueError(f"unsupported runtime provider: {provider}")
        return [dict(model) for model in CLAUDE_MODELS]

    def reconcile(self) -> List[str]:
        """Reap agents whose process died out-of-band (crash/kill) without a
        lifecycle call: drop the dead handle and flip the DB row off 'running'
        so it doesn't lie. Returns the reaped agent ids. Intended to be called
        periodically by the hub (also covers Legolas' zombie note)."""
        reaped = []
        with self._lock:
            # A handle is registered BEFORE its process starts, so that an
            # early init event can find it. In that window alive() is False
            # while the agent is about to be perfectly healthy — reaping it
            # there deletes the handle, stamps the row errored, and then spawn
            # stamps 'running' again with no handle registered: a live claude
            # session the supervisor can no longer see or stop. Skip anything a
            # spawn is currently holding.
            dead = [(a, pr) for a, pr in self._procs.items()
                    if not pr.alive() and a not in self._starting]
            for a, _ in dead:
                del self._procs[a]
        for a, _ in dead:
            self._set_state(a, ST_ERRORED, clear_pid=True)
            # A crash reaped here (as opposed to a deliberate stop/hibernate/
            # clear) left _pending untouched. Without this, _bridge_result()
            # would pop a turn context belonging to the DEAD process against
            # a plain result from whatever wakes next, routing it to the
            # wrong channel (bugs/2026-08-01-claude-crash-retains-pending-context.md).
            self._forget_pending(a)
            reaped.append(a)
        return reaped

    def shutdown(self, preserve_sessions: bool = False) -> None:
        """Stop every live agent (process shutdown). Marks rows stopped so a
        later daemon start can decide whether to auto-resume."""
        self._accepting = False
        with self._lock:
            items = list(self._procs.items())
            self._procs.clear()
            self._pending.clear()
            # Every turn still open is being abandoned. The supervisor object
            # survives shutdown(preserve_sessions=True) (the SIGINT path in
            # nth_web calls it), so leaving these behind would leak a stale
            # request counter into the next turn each agent runs.
            for agent_id, _proc in items:
                self._forget_turn_log_locked(agent_id)
        for agent_id, proc in items:
            proc.stop()
            self._set_state(agent_id,
                            ST_SLEEPING if preserve_sessions else ST_STOPPED,
                            clear_pid=True)


if __name__ == "__main__":
    # Minimal manual smoke: `TRIO_AGENT_CMD='python3 tests/fake_agent.py' \
    #   python3 server/nth_supervisor.py` — but real use is via the hub.
    print("nth_supervisor is a library; import AgentSupervisor. "
          f"agent binary = {agent_binary()}")
