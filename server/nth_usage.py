"""Quota-burn history and the arithmetic over it.

Provider APIs report a quota's CURRENT percentage but keep no history, so a
rate ("3 pp/hour"), a daily change and a forecast at reset all have to be
derived from samples we record ourselves. This module owns that: the on-disk
sample series under NTH_HOME, and every function that reads it.

It lives apart from nth_web.py deliberately. Every other file under NTH_HOME
has a dedicated owning module — token-events.json and usage-cli.json belong to
nth_supervisor, request-log.jsonl to nth_request_log — and none of the maths
below has any HTTP in it. Keeping it here also means the arithmetic can be
tested without standing up a web server.

The organising principle is: never report a number that is confidently wrong.
A rate that cannot be computed honestly is reported as None, and every derived
figure carries enough context to say what it was measured over. Concretely:

  * no slope is ever taken across a quota RESET, because that measures the
    reset rather than the usage. Two independent cuts enforce it: the window
    start derived from the provider's own `resets_at`, and a visible step down
    in the value, which covers the (normal) case of a stale or absent reset
    time. A window longer than the quota's own period is refused outright;
  * a rate is only ever computed across samples from the SAME source, because
    the two sources for a Claude percentage can disagree and a handoff between
    them would otherwise render as a spike;
  * every rate reports `measured_hours`, the real age of its baseline, because
    early on a "15 minute" window is necessarily measured over a longer span
    and labelling it `m15` would be a lie;
  * NaN and Infinity are rejected at every boundary. json.dumps re-emits them,
    browsers' JSON.parse then rejects the whole response, and the panel blanks
    entirely rather than losing one row — and a persisted one poisons the
    series for its full retention window.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_NTH_HOME = Path(os.environ.get("NTH_HOME", str(Path.home() / ".claude" / "nth")))
HISTORY_PATH = _NTH_HOME / "usage-history.json"

# Bumped when the sample shape changes incompatibly. Written into the file and
# checked on load: an unrecognised version is discarded rather than fed to
# arithmetic that assumes different field names. Token events already carry a
# `v` for the same reason.
SCHEMA_VERSION = 1

_LOCK = threading.Lock()
MAX_AGE = 24 * 3600     # keep 24h of samples
MIN_GAP = 25            # seconds — don't record samples closer than this

# The nominal rate windows. Nested by construction (m15 ⊂ h1 ⊂ h24).
BURN_SPANS: Tuple[Tuple[str, float], ...] = (
    ("m15", 900.0), ("h1", 3600.0), ("h24", 86400.0))

# resets_at is unix SECONDS. A JS-epoch value (milliseconds) would sail through
# as a timestamp ~50,000 years out, making every forecast permanently clamped
# and `before_reset` permanently true — a plausible-looking wrong answer rather
# than an error. Anything beyond this horizon is treated as absent.
MAX_RESET_HORIZON = 400 * 86400
# Timestamps below this are nonsense too — a negative or near-zero `resetsAt`
# re-emitted verbatim renders as a date tens of billions of years BC.
MIN_RESET_FLOOR = 1_000_000_000            # 2001-09-09; older than this repo

# A drop of more than this many percentage points between consecutive samples
# is a quota reset, not usage going backwards. Small enough to catch any real
# reset (they drop to near zero), large enough not to fire on provider
# rounding jitter.
RESET_DROP_PP = 0.5

# Set once if the series cannot be persisted. Without this the feature dies
# silently on a read-only NTH_HOME or a full disk: every rate reads None
# forever, which is indistinguishable from "still collecting a baseline".
_write_error: Optional[str] = None


def num_ok(value: Any, *, allow_none: bool) -> bool:
    """True if `value` is a JSON number safe to do arithmetic on and re-emit.

    `bool` is rejected because it subclasses `int`. NaN/Infinity are rejected
    because json.loads accepts those non-standard tokens, they satisfy
    isinstance, and json.dumps re-emits them.
    """
    if value is None:
        return allow_none
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    # math.isfinite() RAISES OverflowError on an int too large to convert to a
    # float, rather than returning False.
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def clamp_percentage(value: Any) -> Optional[float]:
    """A quota percentage coerced into [0, 100], or None if it is not a usable
    number. Callers must run every externally-sourced percentage through this:
    an unclamped huge value divided by a 60-second baseline produces an
    Infinity RATE out of finite inputs, which then reaches json.dumps."""
    if not num_ok(value, allow_none=False):
        return None
    return max(0.0, min(100.0, float(value)))


def sane_timestamp(value: Any, now: float) -> Optional[float]:
    """A reset timestamp in unix seconds, or None. See MAX_RESET_HORIZON."""
    if not num_ok(value, allow_none=False):
        return None
    ts = float(value)
    if ts > now + MAX_RESET_HORIZON or ts < MIN_RESET_FLOOR:
        return None
    return ts


def write_error() -> Optional[str]:
    """The reason the series could not be persisted, if it could not be."""
    return _write_error


def load_history() -> List[Dict[str, Any]]:
    """Well-formed samples only, oldest first.

    Consumers do arithmetic on `t` always and on `fh`/`sd` when non-None, so a
    hand-corrupted or partially-written entry must not reach them.
    """
    try:
        data = json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        if data.get("v") != SCHEMA_VERSION:
            return []
        data = data.get("samples")
    if not isinstance(data, list):
        return []

    def _codex_ok(cx: Any) -> bool:
        # Drop only the offending Codex key, never the whole sample — the
        # sample's Claude readings are still good, and discarding them would
        # punch a hole in the Claude burn series too.
        if isinstance(cx, dict):
            for key in [k for k, n in cx.items() if not num_ok(n, allow_none=False)]:
                cx.pop(key, None)
        return cx is None or isinstance(cx, dict)

    return [s for s in data if isinstance(s, dict)
            and num_ok(s.get("t"), allow_none=False)
            and num_ok(s.get("fh"), allow_none=True)
            and num_ok(s.get("sd"), allow_none=True)
            and _codex_ok(s.get("cx"))]


def record_sample(five_hour: Optional[float], seven_day: Optional[float],
                  fh_src: Optional[str] = None, sd_src: Optional[str] = None,
                  codex_limits: Optional[Dict[str, float]] = None,
                  now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Append one sample and return the trimmed history, oldest first.

    Each percentage carries its OWN source tag, so a rate is never computed
    across a source handoff even when only one of the two fields switched.

    A call with nothing to record still returns the current history, so a
    caller can derive rates from prior samples. The MIN_GAP suppression also
    guarantees the series stays sorted by `t`: a backwards clock or a slow
    writer cannot insert an out-of-order sample, it simply does not append.
    Every consumer's baseline search depends on that ordering.
    """
    global _write_error
    now = time.time() if now is None else now
    with _LOCK:
        hist = load_history()
        if five_hour is None and seven_day is None and not codex_limits:
            return hist
        if hist and now - hist[-1].get("t", 0) < MIN_GAP:
            return hist
        hist.append({"t": now, "fh": five_hour, "sd": seven_day,
                     "fh_src": fh_src, "sd_src": sd_src,
                     "cx": dict(codex_limits or {})})
        cutoff = now - MAX_AGE
        hist = [s for s in hist if s.get("t", 0) >= cutoff]
        payload = json.dumps({"v": SCHEMA_VERSION, "samples": hist})
        # tmp + replace, the same way nth_supervisor compacts token events and
        # nth_request_log prunes. A plain write_text truncates in place, and a
        # reader landing mid-write sees a torn file — which load_history
        # "recovers" by silently discarding 24h of series. The pid in the temp
        # name keeps two servers sharing one NTH_HOME from clobbering each
        # other's temp file (the replace itself is atomic).
        tmp = HISTORY_PATH.with_suffix(f".json.{os.getpid()}.tmp")
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload)
            os.replace(tmp, HISTORY_PATH)
            _write_error = None
        except OSError as exc:
            # Say so exactly once. Silence here means every rate reads null
            # forever while the UI reports "collecting a baseline".
            if _write_error is None:
                sys.stderr.write(
                    f"[nth_usage] cannot persist the quota-burn series to "
                    f"{HISTORY_PATH}: {exc}. Rates and forecasts will stay "
                    f"empty until this is fixed.\n")
            _write_error = str(exc)
            try:
                tmp.unlink()
            except OSError:
                pass
        return hist


def since_last_reset(history: List[Dict[str, Any]], key: str,
                     window_start: Optional[float] = None
                     ) -> List[Dict[str, Any]]:
    """The tail of `history` that belongs to the CURRENT quota window.

    Everything downstream measures a slope, and a slope taken across a reset is
    not a burn rate — it is the reset, reported as usage. A quota that read 95%
    just before its reset and 8% just after yields "−87 pp" as a headline
    number if the two samples are compared.

    Two independent cuts, because neither is sufficient alone:

      * `window_start` (= resets_at − period) is EXACT when the provider told
        us when the quota resets. Samples before it belong to the last window.
      * A step DOWN in the value is a reset we can see for ourselves. This is
        what covers a stale or absent `resets_at` — which is the normal case
        for the statusline source, documented as lagging.

    Bounding the baseline's AGE instead (the obvious fix) is not enough: a
    reset three hours ago plus a sampling gap still leaves a four-hour-old
    pre-reset baseline inside a five-hour quota. The series only grows when
    someone polls, so multi-hour gaps are ordinary.
    """
    start = 0
    previous = None
    for index, sample in enumerate(history):
        value = sample.get(key)
        if value is None:
            continue
        if window_start is not None and sample["t"] < window_start:
            start = index + 1
        elif previous is not None and previous - value > RESET_DROP_PP:
            start = index
        previous = value
    return history[start:]


def _baseline(history: List[Dict[str, Any]], key: str, span: float,
              now: float) -> Optional[Dict[str, Any]]:
    """The sample to measure against: the newest one at or before the window
    start, falling back to the earliest available when none is that old.

    The fallback is why every rate reports `measured_hours` — early on, or
    after a reset, the only baseline available is younger than the window
    asked for, and the honest answer is the real span rather than the label.

    Shared by every rate and change function so a guard added here cannot
    silently fail to apply elsewhere. Assumes `history` is sorted by `t` and
    already trimmed by since_last_reset().
    """
    target = now - span
    prior = None
    for sample in history:
        if sample.get(key) is None:
            continue
        if sample["t"] <= target:
            prior = sample
        else:
            if prior is None:
                prior = sample
            break
    return prior


def rate_over(history: List[Dict[str, Any]], key: str, span: float,
              current: Optional[float], now: float
              ) -> Tuple[Optional[float], Optional[float]]:
    """(percentage points per hour, hours actually measured over).

    The second element exists because of the earliest-sample fallback in
    _baseline: with only a 20-hour-old sample on file, all three windows return
    the same 20-hour slope, and reporting one of them as "m15" without saying
    so would be exactly the confidently-wrong number this module refuses to
    produce.

    Returns (None, None) when there is no current reading, no baseline, or the
    baseline is younger than 60s — too short a span to mean anything.
    """
    if current is None:
        return None, None
    prior = _baseline(history, key, span, now)
    if prior is None:
        return None, None
    elapsed = now - prior["t"]
    if elapsed < 60:
        return None, None
    rate = (current - prior[key]) / elapsed * 3600.0
    # Finite inputs can still produce a non-finite rate: a huge percentage over
    # a 60-second baseline multiplies by 60. Callers clamp percentages on read,
    # but this is the last gate before the number reaches a response body.
    if not num_ok(rate, allow_none=False):
        return None, None
    return round(rate, 2), round(elapsed / 3600.0, 2)


def usable_spans(max_span: Optional[float]) -> List[Tuple[str, float]]:
    """Rate windows strictly shorter than the quota's own reset period.

    A window that outlives its quota straddles resets: a 5-hour quota that read
    88% yesterday and has since reset to 6% would report −82 pp over 24h.
    Windows that long are reported as None rather than as a fabricated number.
    Equality is excluded too — a window exactly the length of the reset period
    always straddles the very reset it was meant to avoid.
    """
    if max_span is None:
        return list(BURN_SPANS)
    return [(name, span) for name, span in BURN_SPANS if span < max_span]


def burn_windows(history: List[Dict[str, Any]], key: str,
                 current: Optional[float], now: float,
                 source: Optional[str] = None,
                 max_span: Optional[float] = None,
                 window_start: Optional[float] = None
                 ) -> Dict[str, Dict[str, Optional[float]]]:
    """{window: {pp_per_hr, measured_hours}} for every window in BURN_SPANS.

    Capped windows are present with None values rather than absent, so a client
    can tell "not measurable" from "flat". Only samples whose THIS field came
    from the same source as the current reading are rated against.
    """
    src_key = f"{key}_src"
    same_source = [s for s in history if s.get(src_key) == source]
    same_source = since_last_reset(same_source, key, window_start)
    usable = dict(usable_spans(max_span))
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for name, _ in BURN_SPANS:
        if name in usable:
            rate, hours = rate_over(same_source, key, usable[name], current, now)
        else:
            rate, hours = None, None
        out[name] = {"pp_per_hr": rate, "measured_hours": hours}
    return out


def _codex_series(history: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """One normalized Codex quota key (``limit:primary``) reshaped into the
    {t, value} form the shared functions read. Samples that never carried this
    limit id are omitted rather than zero-filled — a missing reading is not a
    reading of zero."""
    return [{"t": s["t"], "value": s.get("cx", {}).get(key)}
            for s in history
            if isinstance(s.get("cx"), dict) and key in s["cx"]]


def codex_burn_windows(history: List[Dict[str, Any]], key: str,
                       current: Optional[float], now: float,
                       max_span: Optional[float] = None,
                       window_start: Optional[float] = None
                       ) -> Dict[str, Dict[str, Optional[float]]]:
    shaped = since_last_reset(_codex_series(history, key), "value", window_start)
    usable = dict(usable_spans(max_span))
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for name, _ in BURN_SPANS:
        if name in usable:
            rate, hours = rate_over(shaped, "value", usable[name], current, now)
        else:
            rate, hours = None, None
        out[name] = {"pp_per_hr": rate, "measured_hours": hours}
    return out


def change_over(history: List[Dict[str, Any]], key: str, span: float,
                current: Optional[float], now: float,
                source: Optional[str] = None,
                window_start: Optional[float] = None) -> Optional[Dict[str, float]]:
    """Actual percentage-point change and elapsed hours for a lookback window.

    `window_start` applies the same anti-sawtooth rule as the rate windows.
    Without it a "daily change" on a 5-hour quota spans five resets and reports
    the resets rather than the usage: a quota that read 95% just before a reset
    and 8% just after would report −87 pp of "daily change".

    Note this deliberately keeps the full 24h nominal span and lets the
    baseline fall back to the oldest sample in the current window. Refusing to
    answer instead would make `daily_change` permanently null on any quota
    shorter than a day, which is every Claude session quota.
    """
    if current is None:
        return None
    if source is not None:
        history = [s for s in history if s.get(f"{key}_src") == source]
    history = since_last_reset(history, key, window_start)
    prior = _baseline(history, key, span, now)
    if prior is None or now - prior["t"] < 60:
        return None
    change = current - prior[key]
    if not num_ok(change, allow_none=False):
        return None
    return {"percentage_points": round(change, 2),
            "elapsed_hours": round((now - prior["t"]) / 3600.0, 2)}


def codex_change_over(history: List[Dict[str, Any]], key: str,
                      current: Optional[float], now: float,
                      window_start: Optional[float] = None
                      ) -> Optional[Dict[str, float]]:
    return change_over(_codex_series(history, key), "value", 86400.0,
                       current, now, window_start=window_start)


def exhaust_projection(burn: Dict[str, Dict[str, Optional[float]]],
                       current: Optional[float], resets_at: Optional[float],
                       now: float) -> Dict[str, Any]:
    """Project when a quota reaches 100% at the current burn rate.

    Picks the steadiest window whose rate is genuinely POSITIVE, longest first:
    a brief spike does not dominate, and a negative rate (a 24h window
    straddling a weekly reset, where the old high still anchors the baseline)
    never suppresses a real short-term burn. Reports which window it chose AND
    how long that window was really measured over, so the trend can be labelled
    from the same basis rather than from the window's nominal name.

    A `resets_at` in the past is treated as ABSENT rather than as "0 hours
    away". The statusline source is documented as stale, so a stale reset time
    is the normal case, and clamping it to zero produced a self-contradictory
    payload: a forecast "at reset" that was just `current` wearing a forecast's
    clothes, next to a reset claimed to be 0.0 hours out.
    """
    def _rate(window: str) -> Optional[float]:
        return (burn.get(window) or {}).get("pp_per_hr")

    window = next((w for w in ("h24", "h1", "m15")
                   if (_rate(w) or 0) > 0), None)
    rate = _rate(window) if window else None
    window_hours = ((burn.get(window) or {}).get("measured_hours")
                    if window else None)
    exhausted = current is not None and current >= 100
    hours_to_reset: Optional[float] = None
    if resets_at is not None and resets_at > now:
        hours_to_reset = (resets_at - now) / 3600.0
    # A quota cannot exceed 100% used. Without the clamp a bursty short-window
    # rate extrapolated across a week renders absurdities like "3040% expected
    # at reset"; the flag says the clamp engaged so the UI can hedge.
    raw_projection = (current + rate * hours_to_reset
                      if current is not None and rate is not None
                      and hours_to_reset is not None else None)
    if raw_projection is not None and not num_ok(raw_projection, allow_none=False):
        raw_projection = None
    # Truncate rather than round: a raw 99.9998 rounding UP to 100.0 publishes
    # "expected at reset: 100.0%" beside `before_reset: false`, and a client
    # reading the first as "exhausts before the reset" would contradict the
    # field that actually answers that question.
    projected_at_reset = (
        100.0 if raw_projection is not None and raw_projection >= 100.0
        else (math.floor(raw_projection * 10) / 10
              if raw_projection is not None else None))
    # `current` is reported explicitly. A UI showing the arithmetic behind the
    # forecast ("40% now + 3.0 pp/hr × 20h") cannot back-derive it as
    # `projected − rate × hours` once the clamp engages: a clamped 3040%→100%
    # would render "0% now" for a quota sitting at 40%.
    shared = {
        "rate_per_hr": rate,
        "window": window,
        "window_hours": window_hours,
        "current": current,
        "projection_clamped": (raw_projection is not None
                               and raw_projection > 100.0),
        "projected_at_reset": projected_at_reset,
        "hours_to_reset": (round(hours_to_reset, 2)
                           if hours_to_reset is not None else None),
    }
    if exhausted or rate is None or current is None:
        return {"will_exhaust": False, "exhaust_at": None, "before_reset": None,
                "exhausted": exhausted, **shared}
    exhaust_at = now + (100 - current) / rate * 3600.0
    if not num_ok(exhaust_at, allow_none=False):
        return {"will_exhaust": False, "exhaust_at": None, "before_reset": None,
                "exhausted": False, **shared}
    before = (exhaust_at < resets_at
              if resets_at is not None and hours_to_reset is not None else None)
    return {"will_exhaust": True, "exhaust_at": exhaust_at,
            "before_reset": before, "exhausted": False, **shared}


def json_safe(value: Any, depth: int = 0) -> Any:
    """Strip NaN/Infinity out of a provider blob we re-emit verbatim.

    json.loads accepts the non-standard NaN/Infinity tokens by default, so a
    provider that emits one lands it straight in our response body — where
    browsers' JSON.parse rejects the WHOLE payload, blanking the panel. The
    quota rows are parsed field by field and guarded individually; these blobs
    (token summaries, daily buckets) are passed through whole, so they need a
    recursive scrub instead.
    """
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, depth + 1) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return value if num_ok(value, allow_none=False) else None
    return str(value)


def quota_rows(account: Optional[Dict[str, Any]],
               now: float) -> List[Dict[str, Any]]:
    """Flatten App Server's single/multi-bucket quota response for the UI."""
    if not isinstance(account, dict):
        return []
    limits = account.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    by_id = limits.get("rateLimitsByLimitId")
    snapshots: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(by_id, dict) and by_id:
        snapshots = [(str(key), value) for key, value in by_id.items()
                     if isinstance(value, dict)]
    elif isinstance(limits.get("rateLimits"), dict):
        snap = limits["rateLimits"]
        snapshots = [(str(snap.get("limitId") or "codex"), snap)]
    rows: List[Dict[str, Any]] = []
    for fallback_id, snap in snapshots:
        limit_id = str(snap.get("limitId") or fallback_id)
        label = str(snap.get("limitName") or limit_id.replace("_", " "))
        for kind in ("primary", "secondary"):
            window = snap.get(kind)
            if not isinstance(window, dict):
                continue
            used = clamp_percentage(window.get("usedPercent"))
            if used is None:
                continue
            raw_duration = window.get("windowDurationMins")
            duration = (float(raw_duration)
                        if num_ok(raw_duration, allow_none=False) else None)
            rows.append({
                "key": f"{limit_id}:{kind}",
                "limit_id": limit_id,
                "label": label,
                "kind": kind,
                "used_percentage": used,
                "resets_at": sane_timestamp(window.get("resetsAt"), now),
                # MINUTES on the wire; every consumer converts to seconds.
                "window_duration_mins": (duration if duration and duration > 0
                                         else None),
                "reached_type": json_safe(snap.get("rateLimitReachedType")),
                "plan_type": json_safe(snap.get("planType")),
            })
    return rows


# ── token consumption ──
# The event log is append-only and can reach 50k entries, where a full parse
# costs ~170ms. /api/usage is polled, so re-parsing an unchanged file on every
# request would spend that on the request thread holding the GIL. Memoize the
# parse against the file's (mtime, size): an append always changes the size and
# a compaction changes both, so this is exact invalidation rather than a TTL
# that can serve stale numbers.
_token_memo: Dict[str, Any] = {"key": None, "events": []}
_token_memo_lock = threading.Lock()


def token_events() -> List[Dict[str, Any]]:
    import nth_supervisor as nsup     # local: only the token log needs it
    path = nsup.TOKEN_EVENTS_PATH
    try:
        stat = path.stat()
        # Inode and ctime as well as mtime and size: a same-size rewrite inside
        # one mtime tick is indistinguishable otherwise, which is unreachable
        # on APFS/ext4 but not on a coarse-granularity or network filesystem.
        key = (str(path), stat.st_mtime_ns, stat.st_size,
               stat.st_ino, stat.st_ctime_ns)
    except OSError:
        key = None
    # A copy of the LIST (not of the events): the memo is shared by every
    # caller on every request thread, so handing out the backing list means one
    # caller appending to it corrupts every later aggregate for the life of the
    # process. The events themselves are treated as read-only.
    with _token_memo_lock:
        if key is not None and _token_memo["key"] == key:
            return list(_token_memo["events"])
    events = nsup.load_token_events()
    with _token_memo_lock:
        _token_memo["key"] = key
        _token_memo["events"] = events
    return list(events)


_TOKEN_WINDOWS: Tuple[Tuple[str, float], ...] = (
    ("m15", 900.0), ("h1", 3600.0), ("h24", 86400.0))
_TOKEN_PROVIDERS = ("claude", "codex", "unknown")
_TOKEN_FIELDS = ("total", "input", "cache_write", "cache_read", "output", "other")


def _blank_bucket() -> Dict[str, int]:
    return {field: 0 for field in _TOKEN_FIELDS}


def token_rates(now: float,
                events: Optional[List[Dict[str, Any]]] = None
                ) -> Dict[str, Dict[str, Any]]:
    """Token consumption across all agents over 15m / 1h / 24h.

    `total` is all billed tokens (input + cache + output); `output` is what was
    generated. Aggregated from the per-turn events the supervisor harvests from
    stream-json usage — no extra API calls.

    ONE pass over the events, and each event is added to exactly ONE bucket.

    The obvious shape — filter the list per window, then again per provider,
    then sum each field — walks the same 50k events twenty-one times for a
    result one walk produces. That mattered: it was ~290ms of GIL-holding CPU
    on an endpoint the home screen polls.

    The windows are nested (m15 ⊂ h1 ⊂ h24), so an event is accumulated only
    into the NARROWEST window it belongs to and the totals are cascaded
    outward at the end. Writing every event into all three costs three times
    the inner work for the same answer.
    """
    if events is None:
        events = token_events()

    order = [name for name, _ in _TOKEN_WINDOWS]          # narrowest first
    by_provider = {name: {p: _blank_bucket() for p in _TOKEN_PROVIDERS}
                   for name in order}
    # Codex events written before the cached-token fix double-counted cached
    # input, inflating their totals by up to ~2x. They age out within 24h, but
    # until then a window silently blends corrected and inflated numbers — so
    # report how much of it is suspect rather than one confidently-wrong figure.
    unreconciled = {name: {"events": 0, "total": 0} for name in order}
    cutoffs = [(name, now - span) for name, span in _TOKEN_WINDOWS]

    def _int(event: Dict[str, Any], key: str, fallback: str = "") -> int:
        value = event.get(key)
        if value is None and fallback:
            value = event.get(fallback)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    for event in events:
        stamp = event.get("t", 0)
        if not isinstance(stamp, (int, float)):
            continue
        home = None
        for name, cutoff in cutoffs:      # narrowest first: first hit wins
            if stamp >= cutoff:
                home = name
                break
        if home is None:
            continue
        provider = str(event.get("provider") or "unknown").lower()
        if provider not in _TOKEN_PROVIDERS:
            provider = "unknown"
        total = _int(event, "tot")
        output = _int(event, "output", "out")
        input_tokens = _int(event, "input")
        cache_write = _int(event, "cache_write")
        cache_read = _int(event, "cache_read")
        # Old events retained only total+output. Keep their lost input/cache
        # share visible as "other" instead of falsely attributing it to input.
        known = input_tokens + cache_write + cache_read + output
        other = max(_int(event, "other"), total - known)
        bucket = by_provider[home][provider]
        bucket["total"] += total
        bucket["input"] += input_tokens
        bucket["cache_write"] += cache_write
        bucket["cache_read"] += cache_read
        bucket["output"] += output
        bucket["other"] += other
        if provider == "codex" and not event.get("v"):
            unreconciled[home]["events"] += 1
            unreconciled[home]["total"] += total

    # Cascade narrow into wide, then derive each window's grand total.
    for inner, outer in zip(order, order[1:]):
        for provider in _TOKEN_PROVIDERS:
            src = by_provider[inner][provider]
            dst = by_provider[outer][provider]
            for field in _TOKEN_FIELDS:
                dst[field] += src[field]
        unreconciled[outer]["events"] += unreconciled[inner]["events"]
        unreconciled[outer]["total"] += unreconciled[inner]["total"]

    out: Dict[str, Dict[str, Any]] = {}
    for name in order:
        totals = _blank_bucket()
        for provider in _TOKEN_PROVIDERS:
            for field in _TOKEN_FIELDS:
                totals[field] += by_provider[name][provider][field]
        out[name] = {**totals,
                     "providers": by_provider[name],
                     "unreconciled_codex": unreconciled[name]}
    return out
