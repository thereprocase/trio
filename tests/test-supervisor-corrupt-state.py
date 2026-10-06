"""The supervisor's on-disk caches must survive corruption without raising.

Both files here are written by append or by an external subprocess, so both can
be found half-written after a crash — and both are READ from inside a web
request handler that has no wrapping exception handler. A raise there does not
produce a 500; it closes the socket with no status line at all, on every
request, until someone repairs the file by hand.

The two failure modes below are specifically the ones that slip past a plausible
guard:

  * `float()` on an oversized JSON int raises OverflowError, not ValueError, and
    `isinstance(x, (int, float))` happily admits the value first.
  * a file truncated mid-UTF-8 makes `read_text()` raise UnicodeDecodeError,
    which is a ValueError — so `except OSError` around the read does nothing.

Usage: python tests/test-supervisor-corrupt-state.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server"
sys.path.insert(0, str(SERVER))

_tmp = Path(tempfile.mkdtemp(prefix="nth_corrupt_"))
os.environ["NTH_HOME"] = str(_tmp)

import nth_supervisor as nsup    # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def survives(fn, *args, **kwargs):
    """(did_not_raise, result_or_exception)."""
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:               # noqa: BLE001 — the point of the test
        return False, f"{type(exc).__name__}: {exc}"


try:
    nsup.USAGE_CLI_PATH = _tmp / "usage-cli.json"
    nsup.TOKEN_EVENTS_PATH = _tmp / "token-events.json"

    # ── usage-cli cache ──
    nsup.USAGE_CLI_PATH.write_text('{"t": ' + "9" * 400 + ', "session_pct": 10}')
    cached = nsup.load_usage_cli()
    check("an oversized `t` is admitted by load_usage_cli's isinstance check "
          "(so the guard downstream is the one that matters)",
          isinstance(cached, dict))
    ok, detail = survives(nsup.maybe_refresh_usage_cli)
    check(f"maybe_refresh_usage_cli survives an oversized `t` ({detail})", ok)

    nsup.USAGE_CLI_PATH.write_text('{"t": "not a number", "session_pct": 10}')
    ok, _ = survives(nsup.maybe_refresh_usage_cli)
    check("maybe_refresh_usage_cli survives a non-numeric `t`", ok)
    nsup.USAGE_CLI_PATH.write_text("{ truncated")
    ok, _ = survives(nsup.maybe_refresh_usage_cli)
    check("maybe_refresh_usage_cli survives an unparseable cache", ok)
    nsup.USAGE_CLI_PATH.unlink()
    ok, _ = survives(nsup.maybe_refresh_usage_cli)
    check("maybe_refresh_usage_cli survives a missing cache", ok)

    # ── token event log ──
    good = [json.dumps({"t": 1000.0, "id": "a", "provider": "claude",
                        "tot": 100, "out": 10, "v": 1}),
            json.dumps({"t": 1001.0, "id": "a", "provider": "claude",
                        "tot": 200, "out": 20, "v": 1})]
    nsup.TOKEN_EVENTS_PATH.write_text("\n".join(good) + "\n")
    check("a clean log reads back completely", len(nsup.load_token_events()) == 2)

    # A crash mid-append leaves a partial multi-byte sequence.
    nsup.TOKEN_EVENTS_PATH.write_bytes(
        ("\n".join(good) + "\n").encode() + b'{"t": 3, "id": "\xf0\x9f\x92')
    ok, events = survives(nsup.load_token_events)
    check("load_token_events survives a truncated multi-byte tail"
          + ("" if ok else f" — {events}"), ok)
    check("and the intact events before the tear are still returned",
          ok and len(events) == 2)

    # A torn line that is merely invalid JSON must not lose the whole file.
    nsup.TOKEN_EVENTS_PATH.write_text("\n".join(good) + '\n{"t": 3, "id"')
    events = nsup.load_token_events()
    check("a torn JSON line is skipped, not fatal to the rest",
          len(events) == 2)

    # Whole-file garbage degrades to empty rather than raising.
    nsup.TOKEN_EVENTS_PATH.write_bytes(b"\xff\xfe\x00\x01 not text at all")
    ok, events = survives(nsup.load_token_events)
    check("load_token_events survives a binary-garbage file", ok and events == [])

    nsup.TOKEN_EVENTS_PATH.unlink()
    ok, events = survives(nsup.load_token_events)
    check("load_token_events survives a missing file", ok and events == [])
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — the supervisor's caches degrade instead of raising")
