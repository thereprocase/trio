"""Tests for full-history search (GET /api/search).

Live loopback: seed messages, then query the endpoint — substring match,
case-insensitivity, LIKE-wildcard escaping (so "50%" is literal), and the
min-length guard. Usage: python tests/test-search.py
"""
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote
import shutil
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_server as srv    # noqa: E402
import nth_web as web       # noqa: E402

failures = []
skips = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def skip(name, why):
    print(f"SKIP: {name} ({why})")
    skips.append(name)


_tmp = tempfile.mkdtemp(prefix="nth_search_")
srv.DB_DIR = Path(_tmp)
srv.DB_PATH = Path(_tmp) / "nth.db"


def http_get(port, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


r = json.loads(srv.nth_connect(summary="t", name="Asker", channel="searchtest"))
CH, asker = r["channel"], r["member_id"]
for msg in ["the quick brown fox", "PLAIN text here", "50% discount today",
            "another brown message", "unrelated content"]:
    srv.nth_send(channel=CH, member_id=asker, message=msg)


def contents(results):
    return [x["content"] for x in results]


hub = web.EventHub(srv.DB_PATH, CH)
server = None
try:
    hub.start()
    web.NthWebHandler.hub = hub
    web.NthWebHandler.channel = CH
    web.NthWebHandler.db_path = srv.DB_PATH
    server = web.QuietThreadingHTTPServer(("127.0.0.1", 0), web.NthWebHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.2)

    st, d = http_get(port, "/api/search?q=brown")
    if st != 200:
        skip("search", f"endpoint not reachable ({st})")
    else:
        check("search: substring match", d.get("ok") and d.get("count") == 2
              and all("brown" in c for c in contents(d["results"])))
        # case-insensitive
        st, d = http_get(port, "/api/search?q=plain")
        check("search: case-insensitive", d.get("count") == 1
              and "PLAIN text here" in contents(d["results"]))
        # newest-first ordering
        st, d = http_get(port, "/api/search?q=brown")
        ids = [x["id"] for x in d["results"]]
        check("search: newest-first", ids == sorted(ids, reverse=True))
        # LIKE-wildcard escaping — "0%" must be a literal substring, not a
        # match-everything wildcard (would otherwise match all 6 messages).
        st, d = http_get(port, "/api/search?q=" + quote("0%"))
        check("search: wildcard % escaped (literal)",
              d.get("count") == 1 and "50% discount today" in contents(d["results"]))
        # min length (a single char is rejected)
        st, d = http_get(port, "/api/search?q=a")
        check("search: min length -> 400", st == 400)
        # no matches
        st, d = http_get(port, "/api/search?q=zzzznomatch")
        check("search: no matches -> empty", d.get("ok") and d.get("count") == 0)
except OSError as e:
    skip("search", f"could not start server: {e}")
finally:
    if server is not None:
        server.shutdown()
    hub.stop()

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
