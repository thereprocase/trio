"""Tests for GET /api/usage/requests — the reader over nth_request_log.

The log's WRITER shipped with the supervisor; until this endpoint there was no
way to read it back short of `jq` on the raw JSONL. These tests cover the query
surface (since / agent / provider / kind / limit) and, more importantly, the
argument handling: this handler runs inside do_GET, which has NO wrapping
exception handler, so a bad query parameter that raises drops the TCP
connection with no response at all — not even a 500. Several checks below exist
only to prove that cannot happen.

Usage: python tests/test-request-log-api.py
"""
import json
import os
import shutil
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

_tmp = Path(tempfile.mkdtemp(prefix="nth_reqlog_"))
# NTH_HOME is read at IMPORT time by nth_request_log, so it must be set before
# the module loads — otherwise this test reads (and prunes) the real user's log.
os.environ["NTH_HOME"] = str(_tmp / "home")
(_tmp / "home").mkdir(parents=True, exist_ok=True)

import nth_server as srv          # noqa: E402
import nth_web as web             # noqa: E402
import nth_request_log as nrl     # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


check("log path is redirected into the test home, not the real one",
      str(_tmp) in str(nrl.REQUEST_LOG_PATH))

srv.DB_DIR = _tmp
srv.DB_PATH = _tmp / "nth.db"
json.loads(srv.nth_connect(summary="t", name="Host", channel="chan-r"))

web.NthWebHandler._default_channel = ""
web.NthWebHandler.db_path = srv.DB_PATH
web._DB_PATH_GLOBAL = srv.DB_PATH
web._SUPERVISOR = None


def http(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


server = None
try:
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    # ── disabled: informative, not a 404 ──
    os.environ.pop(nrl.ENV_FLAG, None)
    st, d = http(port, "/api/usage/requests")
    check("disabled: 200 with enabled:false and how to switch it on",
          st == 200 and d.get("ok") is True and d.get("enabled") is False
          and nrl.ENV_FLAG in d.get("hint", ""))

    # ── enabled: entries come back ──
    os.environ[nrl.ENV_FLAG] = "1"
    nrl.record_request("agent-a", "claude",
                       {"input_tokens": 100, "output_tokens": 10}, model="sonnet")
    nrl.record_request("agent-b", "codex",
                       {"input_tokens": 20, "output_tokens": 5}, model="gpt")
    nrl.record_turn("agent-a", "claude",
                    {"input_tokens": 100, "output_tokens": 10}, model="sonnet")
    st, d = http(port, "/api/usage/requests")
    check("enabled: every entry returned, no hint",
          st == 200 and d.get("enabled") is True
          and len(d.get("entries", [])) == 3 and "hint" not in d)
    check("aggregates by agent are present — they are the point of the endpoint",
          isinstance(d.get("by_agent"), (dict, list)) and d.get("by_agent"))

    st, d = http(port, "/api/usage/requests?kind=request")
    check("kind filter", st == 200 and len(d["entries"]) == 2
          and all(e["kind"] == "request" for e in d["entries"]))
    st, d = http(port, "/api/usage/requests?agent=agent-b")
    check("agent filter", st == 200 and len(d["entries"]) == 1
          and d["entries"][0]["agent"] == "agent-b")
    st, d = http(port, "/api/usage/requests?provider=CODEX")
    check("provider filter is case-insensitive",
          st == 200 and len(d["entries"]) == 1)
    st, d = http(port, "/api/usage/requests?agent=nobody")
    check("a filter matching nothing returns an empty list, not an error",
          st == 200 and d["entries"] == [])
    st, d = http(port, "/api/usage/requests?limit=2")
    check("limit honoured", st == 200 and len(d["entries"]) == 2)

    # ── `since` parsing ──
    st, d = http(port, "/api/usage/requests?since=15m")
    check("`15m` shorthand keeps just-written entries",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?since=1s")
    check("`1s` shorthand excludes nothing newer than a second ago",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, f"/api/usage/requests?since={time.time() + 3600}")
    check("a future absolute `since` excludes everything",
          st == 200 and d["entries"] == [])

    # ── arguments that must not drop the connection ──
    # do_GET has no wrapping handler: an OverflowError here means the client
    # sees RemoteDisconnected, with no status line at all.
    st, d = http(port, f"/api/usage/requests?since={'9' * 400}d")
    check("a 400-digit shorthand answers instead of dropping the connection",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, f"/api/usage/requests?since=1e{'9' * 20}")
    check("an absolute `since` too large for a float still answers",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?since=NaN")
    check("a NaN `since` is ignored rather than silently matching nothing",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?since=Infinity")
    check("an Infinity `since` is ignored rather than excluding everything",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?since=yesterday")
    check("an unparseable `since` is ignored", st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?limit=notanumber")
    check("an unparseable limit falls back to the default",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, f"/api/usage/requests?limit={'9' * 400}")
    check("an absurd limit is capped rather than raising",
          st == 200 and len(d["entries"]) == 3)
    st, d = http(port, "/api/usage/requests?limit=-5")
    check("a negative limit clamps to at least one",
          st == 200 and len(d["entries"]) == 1)

    # ── the operator gate ──
    # A loopback probe resolves to an all-seeing operator, so a request from
    # 127.0.0.1 cannot distinguish "the gate works" from "this caller happens
    # to be an operator". Assert THROUGH the predicate instead.
    # The stub must ALSO send the refusal, because that is what the real
    # predicate does before returning None — a stub that merely returns None
    # leaves the handler falling off the end having written nothing, and the
    # client sees RemoteDisconnected. (Which is itself a useful demonstration
    # that do_GET has no wrapping handler.)
    original = web.NthWebHandler._require_operator

    def _deny(self):
        self._error(403, "operator required")
        return None

    web.NthWebHandler._require_operator = _deny
    try:
        st, d = http(port, "/api/usage/requests")
        check("a caller the operator gate rejects gets no log data",
              st != 200 and not d.get("entries"))
    finally:
        web.NthWebHandler._require_operator = original
    st, d = http(port, "/api/usage/requests")
    check("the gate restored: operator reads the log again",
          st == 200 and len(d["entries"]) == 3)
finally:
    if server is not None:
        server.shutdown()
        server.server_close()
    os.environ.pop(nrl.ENV_FLAG, None)
    shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("OK — all request-log endpoint checks passed")
