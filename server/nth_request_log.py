#!/usr/bin/env python3
"""Opt-in per-request token log for diagnosing unexpected token consumption.

WHY THIS EXISTS SEPARATELY FROM token-events.json
-------------------------------------------------
`nth_supervisor.record_token_event` keeps one event per *turn*. That answers
"how many tokens did we burn in the last hour", but not "why". A turn that makes
forty tool round-trips collapses into a single number, and the single most
common cause of a surprise bill — a long tool loop re-sending a large cached
prompt on every round-trip — looks identical to one expensive prompt.

This module records one entry per underlying API *request*, plus a rollup entry
per turn, so the two views can be compared directly: if a turn's rollup is 40x
its median request, the loop is the problem, not the prompt.

DEFAULT OFF
-----------
Enabled only when NTH_REQUEST_LOG is set to a truthy value. Unset means
every function here is a cheap no-op and nothing is written, so reverting to
stock behavior is a matter of unsetting one variable.

RETENTION
---------
Entries are pruned to the last 24h, with a floor and a ceiling: at least
MIN_ENTRIES are kept regardless of age (a quiet day should still leave
something to read), and never more than MAX_ENTRIES however fresh they are (a
busy multi-agent hub can produce far more inside 24h than is useful, and both
reading and pruning are O(n)).

FORMAT
------
Append-only JSONL, one JSON object per line. Appends are O(1); the file is only
read and rewritten during a prune, which is gated so it cannot run on every
request. (The per-turn ring buffer in nth_supervisor rewrites its entire file on
every write; at per-request granularity that would be markedly worse.)

Entry fields:
    t         float   unix seconds
    kind      str     "request" (one API call) or "turn" (rollup)
    agent     str     agent id
    provider  str     "claude" | "codex"
    model     str     model id, when the provider reports one
    turn      str     turn id, to join requests to their rollup
    seq       int     1-based request index within the turn (requests only)
    input/cache_read/cache_write/output/total   int
    detail    dict    optional provider-specific extras (stop reason, etc.)
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_NTH_HOME = Path(os.environ.get("NTH_HOME", str(Path.home() / ".claude" / "nth")))
REQUEST_LOG_PATH = _NTH_HOME / "request-log.jsonl"

ENV_FLAG = "NTH_REQUEST_LOG"
MAX_AGE = 24 * 3600          # prune entries older than this...
MIN_ENTRIES = 2000           # ...but never below this many, however old...
MAX_ENTRIES = 20_000         # ...and never above this many, however fresh.
# The ceiling is deliberately modest: a prune reads, parses, re-serializes and
# rewrites the whole file WHILE HOLDING _LOCK, on whichever agent's reader
# thread happens to hit the append that triggers it. A 200k-entry ceiling would
# stall every agent's logging for the length of a ~50MB rewrite.
# Rewriting the file is O(n). Only consider pruning every N appends, so the
# steady-state cost of logging stays a single append.
PRUNE_EVERY = 500
# No single request legitimately bills this many tokens of one category. A value
# above this is REJECTED to 0 (see _int) rather than clamped — clamping would
# invent tokens that were never spent, in a log whose only job is to be trusted.
MAX_PER_FIELD = 1_000_000_000
# Provider-supplied strings (agent, model, turn) are written verbatim. Bound
# them: they are unbounded in principle, and both the file and the by_model
# grouping in query() would carry whatever a buggy provider emitted.
MAX_STR = 200

_LOCK = threading.Lock()
_appends_since_prune = 0

_FIELDS = ("input", "cache_read", "cache_write", "output", "total")
# Both providers' spellings, normalized to this module's field names.
_ALIASES = {
    "input": ("input_tokens", "inputTokens", "input"),
    "cache_read": ("cache_read_input_tokens", "cachedInputTokens", "cache_read"),
    "cache_write": ("cache_creation_input_tokens", "cacheWriteInputTokens",
                    "cache_write"),
    "output": ("output_tokens", "outputTokens", "output"),
    "total": ("total_tokens", "totalTokens", "total"),
}


def enabled() -> bool:
    """True when the operator has opted in. Re-read every call so the flag can
    be flipped by restarting only the web server, not the whole session."""
    return str(os.environ.get(ENV_FLAG, "")).strip().lower() not in (
        "", "0", "false", "no", "off")


def _int(value: Any) -> int:
    """A sane, non-negative int, or 0.

    int(inf) raises OverflowError rather than ValueError, and an unbounded
    Python int written to disk would make later math.isfinite() calls raise.
    A value beyond MAX_PER_FIELD is REJECTED to 0 rather than clamped: clamping
    would invent a billion tokens that were never spent, and this log exists to
    be trusted while chasing an anomaly.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if number > MAX_PER_FIELD:
        return 0
    return max(0, number)


def _text(value: Any) -> str:
    """A bounded string, truncated from the LEFT.

    Provider-supplied labels are unbounded in principle. Keeping the tail
    matters: a turn key is "<session id>#<n>", so cutting the end would strip
    the discriminator and merge every turn of a long-session agent into one key
    — the exact collision the per-turn key exists to prevent.
    """
    text = str(value or "")
    return text if len(text) <= MAX_STR else text[-MAX_STR:]


def _detail(value: Any, depth: int = 0) -> Any:
    """A bounded, JSON-safe copy of a caller-supplied detail payload.

    `detail` carries provider strings too (stop_reason, subtype, session_id),
    and a non-serializable value in it would make json.dumps raise and discard
    the whole entry.
    """
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if isinstance(value, int) or math.isfinite(value) else None
    if isinstance(value, dict) and depth < 2:
        return {str(k)[:MAX_STR]: _detail(v, depth + 1)
                for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)) and depth < 2:
        return [_detail(v, depth + 1) for v in list(value)[:20]]
    return _text(value)


def normalize(usage: Optional[Dict[str, Any]], provider: str,
              disjoint: bool = False) -> Dict[str, int]:
    """Provider usage → this module's disjoint categories.

    Codex reports `cachedInputTokens` as a SUBSET of `inputTokens`, while
    Claude's `input_tokens` excludes its cache fields. Subtract for Codex so a
    request's categories mean the same thing regardless of provider — the whole
    point of this log is comparing them.

    Pass ``disjoint=True`` when the caller has ALREADY converted to the disjoint
    convention (nth_codex_runtime normalizes at the wire boundary before
    aggregating). Subtracting a second time would undercount uncached input by
    the full cached amount.
    """
    out = {field: 0 for field in _FIELDS}
    if not isinstance(usage, dict):
        return out
    for field, names in _ALIASES.items():
        for name in names:
            if name in usage:
                out[field] = _int(usage[name])
                break
    if provider == "codex" and not disjoint:
        out["input"] = max(0, out["input"] - out["cache_read"])
    categories = out["input"] + out["cache_read"] + out["cache_write"] + out["output"]
    out["total"] = max(categories, out["total"])
    return out


def record(kind: str, agent_id: str, provider: str,
           usage: Optional[Dict[str, Any]], *,
           model: str = "", turn: str = "", seq: int = 0,
           detail: Optional[Dict[str, Any]] = None, disjoint: bool = False,
           now: Optional[float] = None) -> None:
    """Append one entry. Best-effort and never raises: this is diagnostics, and
    must not be able to break a turn it is only observing."""
    if not enabled():
        return
    try:
        counts = normalize(usage, provider, disjoint)
        if counts["total"] <= 0:
            return
        entry: Dict[str, Any] = {
            "t": now if now is not None else time.time(),
            "kind": kind,
            "agent": _text(agent_id),
            "provider": (provider or "unknown").lower()[:MAX_STR],
            **counts,
        }
        if model:
            entry["model"] = _text(model)
        if turn:
            entry["turn"] = _text(turn)
        if seq:
            entry["seq"] = int(seq)
        if detail:
            entry["detail"] = _detail(detail)
        _append(entry)
    except Exception:
        pass


def record_request(agent_id: str, provider: str, usage: Optional[Dict[str, Any]],
                   **kw: Any) -> None:
    """One underlying API request."""
    record("request", agent_id, provider, usage, **kw)


def record_turn(agent_id: str, provider: str, usage: Optional[Dict[str, Any]],
                **kw: Any) -> None:
    """A completed turn's rollup, for comparison against its requests."""
    record("turn", agent_id, provider, usage, **kw)


def _append(entry: Dict[str, Any]) -> None:
    global _appends_since_prune
    line = json.dumps(entry, allow_nan=False) + "\n"
    with _LOCK:
        try:
            REQUEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with REQUEST_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            return
        _appends_since_prune += 1
        if _appends_since_prune >= PRUNE_EVERY:
            _appends_since_prune = 0
            _prune_locked()


def _prune_locked() -> None:
    """Drop entries older than MAX_AGE, keeping at least MIN_ENTRIES. Caller
    holds _LOCK."""
    entries = _read_all()
    if len(entries) <= MIN_ENTRIES:
        return
    # Sort by time before applying either bound. File order is append order,
    # which is NOT time order: record(now=...) lets a caller write out of
    # sequence, and a backward clock step does the same. Slicing "the newest"
    # off an unsorted list would discard fresh entries and keep stale ones.
    entries.sort(key=lambda e: e.get("t", 0))
    cutoff = time.time() - MAX_AGE
    fresh = [e for e in entries if e.get("t", 0) >= cutoff]
    # The age cutoff has a floor and a ceiling. Floor: if the cutoff would
    # leave fewer than MIN_ENTRIES, keep the newest MIN_ENTRIES instead, so a
    # quiet day still leaves something to read. Ceiling: never keep more than
    # MAX_ENTRIES however fresh they are — a busy multi-agent hub can produce
    # far more than that inside 24h, and every read and every prune is O(n).
    keep = fresh if len(fresh) >= MIN_ENTRIES else entries[-MIN_ENTRIES:]
    if len(keep) > MAX_ENTRIES:
        keep = keep[-MAX_ENTRIES:]
    if len(keep) == len(entries):
        return
    # A process-unique tmp name. _LOCK is a threading.Lock, so it does not
    # serialize across processes, and this user runs more than one hub — a
    # shared tmp path would let two prunes interleave and corrupt the file.
    tmp = REQUEST_LOG_PATH.with_suffix(f".jsonl.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in keep:
                fh.write(json.dumps(entry, allow_nan=False) + "\n")
        os.replace(tmp, REQUEST_LOG_PATH)
    except (OSError, ValueError):
        # Never leave a partial tmp behind to accumulate on a full disk.
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_all() -> List[Dict[str, Any]]:
    """Every well-formed entry, oldest first. A torn or hand-edited line is
    skipped rather than aborting the read — a diagnostic log that refuses to
    open because of one bad line is useless exactly when it is needed."""
    try:
        # errors="replace", not a bare decode: UnicodeDecodeError is a
        # ValueError, NOT an OSError, so a file with any invalid byte (a torn
        # write, or anything that appended binary) would otherwise raise past
        # this guard and take the endpoint down with it. Undecodable bytes
        # become replacement chars, and the line then fails JSON parsing and is
        # skipped like any other corrupt line.
        raw = REQUEST_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        t = entry.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            continue
        try:
            if not math.isfinite(t):
                continue
        except OverflowError:
            continue
        entries.append(entry)
    return entries


def query(since: Optional[float] = None, agent: str = "", provider: str = "",
          kind: str = "", limit: int = 500) -> Dict[str, Any]:
    """Filtered entries plus aggregates, newest first.

    The aggregates are the point of the endpoint: totals by agent and by model,
    and the biggest single requests, are what identify a runaway consumer.
    """
    entries = _read_all()
    if since is not None:
        entries = [e for e in entries if e.get("t", 0) >= since]
    if agent:
        entries = [e for e in entries if e.get("agent") == agent]
    if provider:
        entries = [e for e in entries
                   if str(e.get("provider", "")).lower() == provider.lower()]
    if kind:
        entries = [e for e in entries if e.get("kind") == kind]

    requests = [e for e in entries if e.get("kind") == "request"]
    turns = [e for e in entries if e.get("kind") == "turn"]

    def _totals(rows: List[Dict[str, Any]]) -> Dict[str, int]:
        return {field: sum(_int(r.get(field)) for r in rows) for field in _FIELDS}

    def _group(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
        buckets: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row.get(key) or "unknown")
            bucket = buckets.setdefault(name, {key: name, "requests": 0,
                                               **{f: 0 for f in _FIELDS}})
            bucket["requests"] += 1
            for field in _FIELDS:
                bucket[field] += _int(row.get(field))
        return sorted(buckets.values(), key=lambda b: b["total"], reverse=True)

    # Turn rollups are the authoritative billed figure; per-request entries are
    # the breakdown. Summing both would double-count, so report them separately.
    ordered = sorted(entries, key=lambda e: e.get("t", 0), reverse=True)
    return {
        "enabled": enabled(),
        "path": str(REQUEST_LOG_PATH),
        "counts": {"requests": len(requests), "turns": len(turns)},
        "totals": {"requests": _totals(requests), "turns": _totals(turns)},
        "by_agent": _group(requests, "agent"),
        "by_model": _group(requests, "model"),
        "top_requests": sorted(
            requests, key=lambda r: _int(r.get("total")), reverse=True)[:20],
        "entries": ordered[:max(0, limit)],
        "truncated": len(ordered) > max(0, limit),
    }
