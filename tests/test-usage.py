"""Tests for the quota-burn series (server/nth_usage.py) and GET /api/usage.

Two tiers:

  1. Pure arithmetic, imported straight from nth_usage with no web server in
     sight. This is where the feature can be confidently WRONG rather than
     merely broken — a fabricated burn rate looks exactly like a real one — so
     most checks assert the guards: window capping at the quota's reset period,
     source isolation across a statusline<->CLI handoff, the 100% forecast
     clamp, honest reporting of the span a rate was really measured over, and
     rejection of NaN/Infinity before they can reach json.dumps.

  2. The endpoint end to end, with the statusline file, the CLI cache and the
     token-event log pointed at fixtures. No `claude -p "/usage"` subprocess is
     ever spawned.

Usage: python tests/test-usage.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_usage_"))
# NTH_HOME is read at import time by nth_usage and nth_supervisor, so it must
# be set before those load or this test would read and rewrite the real user's
# quota history.
os.environ["NTH_HOME"] = str(_tmp / "home")
(_tmp / "home").mkdir(parents=True, exist_ok=True)

import nth_server as srv          # noqa: E402
import nth_web as web             # noqa: E402
import nth_supervisor as nsup     # noqa: E402
import nth_usage as nu            # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


check("history path is redirected into the test home, not the real one",
      str(_tmp) in str(nu.HISTORY_PATH))

# ───────────────────────── tier 1: pure arithmetic ─────────────────────────

NOW = 1_700_000_000.0


def sample(age_s, fh=None, sd=None, fh_src="cli", sd_src="cli", cx=None):
    return {"t": NOW - age_s, "fh": fh, "sd": sd,
            "fh_src": fh_src, "sd_src": sd_src, "cx": dict(cx or {})}


# ── rate_over: pp/hour, plus the span it was ACTUALLY measured over ──
hist = [sample(7200, fh=10.0), sample(3600, fh=20.0)]
check("rate: 20% now vs 10% two hours ago = 5 pp/hr over 2.0h",
      nu.rate_over(hist, "fh", 86400, 20.0, NOW) == (5.0, 2.0))
check("rate: no current reading -> no rate",
      nu.rate_over(hist, "fh", 86400, None, NOW) == (None, None))
check("rate: no baseline sample -> no rate",
      nu.rate_over([], "fh", 86400, 20.0, NOW) == (None, None))
check("rate: baseline younger than 60s -> no rate (too short to mean anything)",
      nu.rate_over([sample(30, fh=19.0)], "fh", 86400, 20.0, NOW) == (None, None))
# Finite inputs, non-finite result: a huge percentage over a 60s baseline
# multiplies by 60. json.dumps would re-emit the Infinity and blank the panel.
check("rate: a rate that overflows to Infinity is refused, not emitted",
      nu.rate_over([sample(61, fh=0.0)], "fh", 86400, 1e308, NOW) == (None, None))

# ── the earliest-sample fallback must not mislabel the window ──
# With only a 20h-old sample on file, all three windows are the SAME 20h slope.
old_only = [sample(72000, fh=10.0)]
windows = nu.burn_windows(old_only, "fh", 20.0, NOW, "cli")
check("burn: an m15 window measured over 20h says so",
      windows["m15"]["pp_per_hr"] == 0.5
      and windows["m15"]["measured_hours"] == 20.0)
check("burn: all three windows agree when only one old sample exists",
      {w["pp_per_hr"] for w in windows.values()} == {0.5})

# ── usable_spans: strictly shorter than the quota's own reset period ──
check("spans: no cap -> all three windows",
      [n for n, _ in nu.usable_spans(None)] == ["m15", "h1", "h24"])
check("spans: a 5h quota drops the 24h window",
      [n for n, _ in nu.usable_spans(5 * 3600.0)] == ["m15", "h1"])
check("spans: a window exactly equal to the reset period is excluded",
      [n for n, _ in nu.usable_spans(3600.0)] == ["m15"])
capped = nu.burn_windows([sample(7200, fh=10.0)], "fh", 20.0, NOW, "cli",
                         max_span=5 * 3600.0)
check("burn: a capped window is present but null, so 'unmeasurable' is "
      "distinguishable from 'flat'",
      set(capped) == {"m15", "h1", "h24"}
      and capped["h24"] == {"pp_per_hr": None, "measured_hours": None}
      and capped["h1"]["pp_per_hr"] == 5.0)

# ── source isolation ──
mixed = [sample(7200, fh=10.0, fh_src="statusline")]
check("burn: samples from the other source are not rated against",
      nu.burn_windows(mixed, "fh", 20.0, NOW, "cli")["h24"]["pp_per_hr"] is None)
check("burn: samples from the SAME source are rated against",
      nu.burn_windows(mixed, "fh", 20.0, NOW, "statusline")["h24"]["pp_per_hr"] == 5.0)

# ── change_over: the same anti-sawtooth rule as the rate windows ──
chg = nu.change_over([sample(7200, fh=10.0)], "fh", 86400, 20.0, NOW, "cli")
check("change: reports both the delta and the elapsed hours",
      chg == {"percentage_points": 10.0, "elapsed_hours": 2.0})
check("change: baseline younger than 60s -> None",
      nu.change_over([sample(30, fh=19.0)], "fh", 86400, 20.0, NOW, "cli") is None)
# A 24h "daily change" on a 5-hour quota spans ~5 resets. Unshortened, a quota
# that read 95% just over five hours ago and has since reset to 10% reports a
# −85 pp "daily change" — the reset, presented as usage.
straddle = [sample(5 * 3600 + 10, fh=95.0, fh_src="cli"),
            sample(60, fh=10.0, fh_src="cli")]
check("change: WITHOUT the cap, the reset is reported as an -85pp daily change "
      "(this is the bug the cap exists for)",
      nu.change_over(straddle, "fh", 86400, 10.0, NOW, "cli"
                     )["percentage_points"] == -85.0)
check("change: WITH the quota's reset period as the cap, no number is reported "
      "rather than the reset",
      nu.change_over(straddle, "fh", 86400, 10.0, NOW, "cli",
                     max_span=5 * 3600.0) is None)
# Capping the requested SPAN is not sufficient on its own — the earliest-sample
# fallback can hand back a baseline older than the quota no matter how short
# the window asked for. The age of the baseline is what gets refused.
check("baseline: a baseline older than the quota is refused even for a short "
      "window",
      nu.rate_over(straddle, "fh", 900, 10.0, NOW,
                   max_age=5 * 3600.0) == (None, None))
check("baseline: the same short window is fine against a fresher baseline",
      nu.rate_over([sample(3600, fh=5.0, fh_src="cli")], "fh", 900, 10.0, NOW,
                   max_age=5 * 3600.0) == (5.0, 1.0))

# ── exhaust_projection ──
def burn_of(**rates):
    return {w: {"pp_per_hr": rates.get(w), "measured_hours": 2.0}
            for w in ("m15", "h1", "h24")}


proj = nu.exhaust_projection(burn_of(h24=5.0), 20.0, NOW + 3600.0, NOW)
check("projection: the longest positive window wins",
      proj["window"] == "h24" and proj["rate_per_hr"] == 5.0)
check("projection: reports the span the chosen window really covered",
      proj["window_hours"] == 2.0)
check("projection: 20% at 5pp/hr exhausts in 16h, after a 1h reset",
      proj["will_exhaust"] and abs(proj["exhaust_at"] - (NOW + 16 * 3600)) < 1
      and proj["before_reset"] is False)
check("projection: reports `current` explicitly, not back-derivable",
      proj["current"] == 20.0)
spike = nu.exhaust_projection(burn_of(m15=40.0), 40.0, NOW + 100 * 3600.0, NOW)
check("projection: forecast clamped to 100% and flagged",
      spike["projected_at_reset"] == 100.0 and spike["projection_clamped"] is True)
check("projection: clamping does not corrupt the reported current value",
      spike["current"] == 40.0)
neg = nu.exhaust_projection(burn_of(m15=6.0, h24=-82.0), 30.0, NOW + 3600.0, NOW)
check("projection: a negative long window does not mask a positive short one",
      neg["window"] == "m15" and neg["rate_per_hr"] == 6.0)
done = nu.exhaust_projection(burn_of(m15=5.0), 100.0, NOW + 3600.0, NOW)
check("projection: 100% used reads as exhausted, not as 'on track'",
      done["exhausted"] is True and done["will_exhaust"] is False)
flat = nu.exhaust_projection(burn_of(), 20.0, NOW + 3600.0, NOW)
check("projection: no positive rate -> no forecast, no exhaustion",
      flat["will_exhaust"] is False and flat["exhausted"] is False
      and flat["rate_per_hr"] is None)
# The statusline source is documented as stale, so a reset time in the past is
# the NORMAL case. Clamping it to zero produced a payload claiming a reset was
# 0.0 hours away next to a "forecast at reset" that was just `current`.
stale = nu.exhaust_projection(burn_of(h24=3.0), 40.0, NOW - 7200.0, NOW)
check("projection: a reset time in the past reads as absent, not as imminent",
      stale["hours_to_reset"] is None
      and stale["projected_at_reset"] is None
      and stale["before_reset"] is None)
check("projection: a stale reset time still leaves the burn rate usable",
      stale["will_exhaust"] is True and stale["rate_per_hr"] == 3.0)
zero = nu.exhaust_projection(burn_of(h24=3.0), 40.0, 0.0, NOW)
check("projection: resets_at == 0 is treated as absent, not as truthy-false",
      zero["hours_to_reset"] is None and zero["before_reset"] is None)

# ── history hygiene ──
nu.HISTORY_PATH.write_text(json.dumps({"v": nu.SCHEMA_VERSION, "samples": [
    {"t": 1, "fh": 10, "sd": None, "cx": {"a": 5}},
    {"t": 6, "fh": 3, "sd": 4, "cx": {"a": 7}},
]}))
check("history: a versioned file round-trips",
      [s["t"] for s in nu.load_history()] == [1, 6])
nu.HISTORY_PATH.write_text(json.dumps({"v": nu.SCHEMA_VERSION + 99, "samples": [
    {"t": 1, "fh": 10, "sd": None}]}))
check("history: an unrecognised schema version is discarded, not misread",
      nu.load_history() == [])
nu.HISTORY_PATH.write_text(
    '[{"t": 1, "fh": 10, "sd": null, "cx": {"a": 5, "bad": NaN}},'
    ' {"t": 2, "fh": NaN, "sd": 1},'
    ' {"t": Infinity, "fh": 1, "sd": 1},'
    ' {"t": 4, "fh": true, "sd": 1},'
    ' "not-a-dict",'
    ' {"t": 6, "fh": 3, "sd": 4, "cx": {"a": 7}}]')
loaded = nu.load_history()
check("history: a bare list (the pre-version format) is still read",
      [s["t"] for s in loaded] == [1, 6])
check("history: NaN/Infinity/bool/non-dict samples are dropped",
      all(isinstance(s["t"], (int, float)) for s in loaded))
check("history: a bad codex key is dropped without discarding the sample",
      loaded[0]["cx"] == {"a": 5})
nu.HISTORY_PATH.write_text("{not json")
check("history: an unparseable file reads as empty, not as a crash",
      nu.load_history() == [])

# ── record_sample ──
nu.HISTORY_PATH.unlink(missing_ok=True)
nu.record_sample(10.0, 5.0, "cli", "cli", {}, now=NOW)
nu.record_sample(11.0, 6.0, "cli", "cli", {}, now=NOW + 5)      # inside MIN_GAP
after = nu.record_sample(12.0, 7.0, "cli", "cli", {}, now=NOW + 100)
check("record: a sample inside MIN_GAP is suppressed, keeping the series sorted",
      [s["fh"] for s in after] == [10.0, 12.0])
check("record: nothing to record still returns the existing history",
      len(nu.record_sample(None, None, None, None, None, now=NOW + 200)) == 2)
check("record: samples older than the retention window are trimmed",
      len(nu.record_sample(13.0, 8.0, "cli", "cli", {},
                           now=NOW + nu.MAX_AGE + 1000)) == 1)
check("record: the file it wrote is valid JSON with a version stamp",
      json.loads(nu.HISTORY_PATH.read_text())["v"] == nu.SCHEMA_VERSION)
check("record: no temp file is left behind",
      not list(nu.HISTORY_PATH.parent.glob("usage-history.json.*.tmp")))

# A read-only home must be reported, not silently swallowed: without a signal
# every rate reads null forever, which looks exactly like "collecting a
# baseline" — and that is what the UI says.
_real_history = nu.HISTORY_PATH
nu.HISTORY_PATH = Path("/proc/definitely/not/writable/usage-history.json")
nu._write_error = None
nu.record_sample(1.0, 1.0, "cli", "cli", {}, now=NOW)
check("record: a persistent write failure is reported, not swallowed",
      nu.write_error() is not None)
nu.HISTORY_PATH = _real_history
nu._write_error = None

# ── codex quota flattening ──
multi = {"rate_limits": {"rateLimitsByLimitId": {
    "gpt5": {"limitName": "GPT-5", "primary": {
        "usedPercent": 42.5, "resetsAt": NOW + 3600, "windowDurationMins": 300},
        "secondary": {"usedPercent": 8}}}}}
rows = nu.quota_rows(multi, NOW)
check("codex rows: both buckets flattened with composite keys",
      [r["key"] for r in rows] == ["gpt5:primary", "gpt5:secondary"])
check("codex rows: label and window duration carried through",
      rows[0]["label"] == "GPT-5" and rows[0]["window_duration_mins"] == 300.0)
single = {"rate_limits": {"rateLimits": {
    "limitId": "plus", "primary": {"usedPercent": 12}}}}
check("codex rows: a single-bucket response is flattened too",
      [r["key"] for r in nu.quota_rows(single, NOW)] == ["plus:primary"])
check("codex rows: a non-dict account yields no rows",
      nu.quota_rows(None, NOW) == [] and nu.quota_rows("x", NOW) == [])
bad = json.loads('{"rate_limits": {"rateLimits": {"limitId": "z",'
                 ' "primary": {"usedPercent": NaN},'
                 ' "secondary": {"usedPercent": 150, "resetsAt": Infinity}}}}')
rows_bad = nu.quota_rows(bad, NOW)
check("codex rows: a NaN percentage drops that bucket entirely",
      [r["key"] for r in rows_bad] == ["z:secondary"])
check("codex rows: out-of-range percentage clamped, Infinity resets_at dropped",
      rows_bad[0]["used_percentage"] == 100.0 and rows_bad[0]["resets_at"] is None)
# A JS-epoch (milliseconds) resetsAt would otherwise read as a timestamp
# ~50,000 years out: every forecast permanently clamped and before_reset
# permanently true — a plausible-looking wrong answer rather than an error.
ms = {"rate_limits": {"rateLimits": {"limitId": "j", "primary": {
    "usedPercent": 10, "resetsAt": (NOW + 3600) * 1000}}}}
check("codex rows: a millisecond-epoch resetsAt is rejected, not believed",
      nu.quota_rows(ms, NOW)[0]["resets_at"] is None)

# ── json_safe: provider blobs are re-emitted whole ──
blob = json.loads('{"totalTokens": NaN, "nested": [1, Infinity, "x"], "ok": 3}')
safe = nu.json_safe(blob)
check("json_safe: NaN/Infinity scrubbed out of a pass-through blob",
      safe == {"totalTokens": None, "nested": [1, None, "x"], "ok": 3})
check("json_safe: the scrubbed blob survives a json.dumps round-trip",
      json.loads(json.dumps(safe)) == safe)

# ── token-rate aggregation ──
nsup.TOKEN_EVENTS_PATH = _tmp / "token-events.json"
# Written through the REAL writer, so a field rename on either side of the
# supervisor/usage seam fails this test instead of silently zeroing the panel.
nsup.record_token_event("a1", {"input_tokens": 200, "cache_read_input_tokens": 700,
                               "output_tokens": 100}, now=NOW - 60)
nsup.record_token_event("a2", {"total_tokens": 500, "output_tokens": 50},
                        now=NOW - 60, provider="codex")
nsup.record_token_event("a1", {"total_tokens": 99999, "output_tokens": 1},
                        now=NOW - 7200)
# One legacy event in the pre-reconciliation shape (tot+out only, no `v`) —
# these are what `unreconciled_codex` exists to flag.
with nsup.TOKEN_EVENTS_PATH.open("a") as fh:
    fh.write(json.dumps({"t": NOW - 60, "id": "a2", "provider": "codex",
                         "tot": 300, "out": 30}) + "\n")
rates = nu.token_rates(NOW, nu.token_events())
check("tokens: the 15m window excludes a 2h-old event",
      rates["m15"]["total"] == 1800 and rates["h24"]["total"] == 101799)
check("tokens: split by provider",
      rates["m15"]["providers"]["claude"]["total"] == 1000
      and rates["m15"]["providers"]["codex"]["total"] == 800)
check("tokens: categories preserved; unattributed remainder is `other`",
      rates["m15"]["providers"]["claude"]["cache_read"] == 700
      and rates["m15"]["providers"]["claude"]["other"] == 0
      and rates["m15"]["providers"]["codex"]["other"] == 720)
check("tokens: pre-fix codex events counted as unreconciled",
      rates["m15"]["unreconciled_codex"] == {"events": 1, "total": 300})
# Each event is accumulated into only the NARROWEST window it belongs to and
# cascaded outward, so the roll-up is what makes the wider windows correct at
# all. Assert it on every accumulator, not just the grand total.
check("tokens: provider buckets cascade from narrow windows into wide",
      rates["h24"]["providers"]["claude"]["total"] == 100999
      and rates["h1"]["providers"]["claude"]["total"] == 1000)
check("tokens: the unreconciled tally cascades too",
      rates["h1"]["unreconciled_codex"] == {"events": 1, "total": 300}
      and rates["h24"]["unreconciled_codex"] == {"events": 1, "total": 300})
check("tokens: an unknown provider is bucketed, not dropped",
      nu.token_rates(NOW, [{"t": NOW, "provider": "martian",
                            "tot": 7, "out": 1}]
                     )["m15"]["providers"]["unknown"]["total"] == 7)
# The parse is memoized against the file's (mtime, size). Proven by pulling the
# file out from under it: a cached read still answers, a changed file does not.
first = nu.token_events()
check("tokens: an unchanged file returns the memoized parse",
      nu.token_events() is first)
with nsup.TOKEN_EVENTS_PATH.open("a") as fh:
    fh.write(json.dumps({"t": NOW, "id": "a3", "provider": "claude",
                         "tot": 42, "out": 2, "v": 1}) + "\n")
check("tokens: an appended event invalidates the memo",
      nu.token_events() is not first and len(nu.token_events()) == len(first) + 1)

# ───────────────────────────── tier 2: HTTP ─────────────────────────────

srv.DB_DIR = _tmp
srv.DB_PATH = _tmp / "nth.db"
json.loads(srv.nth_connect(summary="t", name="Host", channel="chan-u"))

check("schema: the created_at index the message-rate query needs exists",
      any("idx_messages_created_at" in (r[0] or "") for r in
          sqlite3.connect(str(srv.DB_PATH)).execute(
              "SELECT name FROM sqlite_master WHERE type='index'").fetchall()))

web.NthWebHandler._default_channel = ""
web.NthWebHandler.db_path = srv.DB_PATH
web._DB_PATH_GLOBAL = srv.DB_PATH
web._SUPERVISOR = None

# Never let the handler shell out to `claude -p "/usage"` from a test.
_refresh_calls = []
nsup.maybe_refresh_usage_cli = lambda: _refresh_calls.append(1)

web.STATUSLINE_STATE_PATH = _tmp / "statusline-state.json"
web.STATUSLINE_STATE_PATH.write_text(json.dumps({"_cached_rate_limits": {
    "five_hour": {"used_percentage": 33.0, "resets_at": time.time() + 1800},
    "seven_day": {"used_percentage": 12.0, "resets_at": time.time() + 90000},
}}))
nu.HISTORY_PATH = _tmp / "usage-history-http.json"
nsup.USAGE_CLI_PATH = _tmp / "usage-cli.json"


def http(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode()
            return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode()
        except Exception:
            return e.code, ""


server = None
try:
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, body = http(port, "/api/usage")
    # Parse STRICTLY: the default json.loads accepts NaN/Infinity, which is
    # exactly what a browser will not do. Asserting through a strict parse is
    # the only way this test can catch a non-finite escaping into the body.
    strict = json.JSONDecoder(parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError(f"non-finite {c} in response")))
    d = strict.decode(body)
    check("usage: 200 with every top-level section present",
          st == 200 and d.get("ok") is True
          and set(d) >= {"claude", "codex", "burn", "messages", "tokens"})
    check("usage: statusline percentages surface, tagged per quota",
          d["claude"]["available"] is True
          and d["claude"]["five_hour"]["used_percentage"] == 33.0
          and d["claude"]["five_hour"]["source"] == "statusline")
    check("usage: the CLI refresh is kicked, not awaited", len(_refresh_calls) == 1)
    check("usage: burn windows report both a rate and the span measured",
          set(d["burn"]["five_hour"]) == {"m15", "h1", "h24"}
          and set(d["burn"]["five_hour"]["m15"]) == {"pp_per_hr", "measured_hours"})
    check("usage: the 24h window is capped away on the 5-hour quota",
          d["burn"]["five_hour"]["h24"]["pp_per_hr"] is None)
    check("usage: no managed codex agent is stated as a reason, not an error",
          d["codex"]["available"] is False
          and d["codex"].get("reason") == "no_managed_agent"
          and "error" not in d["codex"])
    check("usage: message rates split sent from received",
          set(d["messages"]) == {"m15", "h1", "h24"}
          and set(d["messages"]["m15"]) == {"total", "sent", "received"}
          and d["messages"]["h24"]["total"] >= d["messages"]["m15"]["total"])
    check("usage: a sample was persisted for the next request to rate against",
          len(nu.load_history()) == 1)

    # ── each quota keeps its OWN freshness ──
    # A stale statusline five-hour figure must not inherit a fresh CLI
    # seven-day figure's timestamp: that presents a 3-hour-old number as
    # 5 seconds old.
    nsup.USAGE_CLI_PATH.write_text(json.dumps({
        "t": time.time(), "week_pct": 44.0, "week_resets": time.time() + 90000}))
    st, body = http(port, "/api/usage")
    d = strict.decode(body)
    check("usage: a partial CLI parse overrides only the quota it parsed",
          d["claude"]["seven_day"]["used_percentage"] == 44.0
          and d["claude"]["seven_day"]["source"] == "cli"
          and d["claude"]["five_hour"]["used_percentage"] == 33.0
          and d["claude"]["five_hour"]["source"] == "statusline")
    check("usage: the stale quota keeps its own older timestamp",
          d["claude"]["five_hour"]["updated_at"]
          < d["claude"]["seven_day"]["updated_at"])

    # An oversized `t` in the CLI cache must not raise OverflowError out of
    # do_GET — that drops the connection with no response at all.
    nsup.USAGE_CLI_PATH.write_text('{"t": ' + "9" * 400 + ', "week_pct": 5}')
    st, body = http(port, "/api/usage")
    check("usage: an oversized CLI timestamp still answers",
          st == 200 and strict.decode(body)["ok"] is True)
    nsup.USAGE_CLI_PATH.unlink()

    # ── degradation, not 500s, and never a non-finite in the body ──
    web.STATUSLINE_STATE_PATH.write_text('{"_cached_rate_limits": {"five_hour": 7}}')
    st, body = http(port, "/api/usage")
    check("usage: a malformed statusline file yields available:false, not a 500",
          st == 200 and strict.decode(body)["claude"]["available"] is False)
    web.STATUSLINE_STATE_PATH.write_text(
        '{"_cached_rate_limits": {"five_hour": {"used_percentage": NaN},'
        ' "seven_day": {"used_percentage": 12.0}}}')
    st, body = http(port, "/api/usage")
    d = strict.decode(body)
    check("usage: a NaN percentage is dropped rather than re-emitted",
          st == 200 and d["claude"]["five_hour"]["used_percentage"] is None
          and d["claude"]["seven_day"]["used_percentage"] == 12.0)
    # An out-of-range percentage is the enabler for an Infinity RATE.
    web.STATUSLINE_STATE_PATH.write_text(
        '{"_cached_rate_limits": {"five_hour": {"used_percentage": 1e308},'
        ' "seven_day": {"used_percentage": 12.0}}}')
    st, body = http(port, "/api/usage")
    d = strict.decode(body)
    check("usage: an out-of-range percentage is clamped, so no Infinity rate "
          "can be derived from it",
          st == 200 and d["claude"]["five_hour"]["used_percentage"] == 100.0)

    # ── the operator gate, asserted THROUGH the predicate ──
    original = web.NthWebHandler._require_operator

    def _deny(self):
        self._error(403, "operator required")
        return None

    web.NthWebHandler._require_operator = _deny
    try:
        st, body = http(port, "/api/usage")
        check("usage: a caller the operator gate rejects gets no quota data",
              st != 200 and "five_hour" not in body)
    finally:
        web.NthWebHandler._require_operator = original
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — all usage/burn checks passed")
