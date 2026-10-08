"""The build id the installed app compares to decide "Update available".

nth_web hashes the composed page, the manifest and the service worker into
APP_BUILD, stamps it into <meta name="nth-build"> and serves it at
/api/version. Two ways that goes wrong for a phone:
  * the id does NOT change when something a reload would bring changes (a
    JS or CSS file, the app identity), so the pill never appears;
  * the id changes when nothing did (a restart of the same checkout), so the
    pill nags every installed app after every restart.
The page side (the pill, the menu item) is tests/test-app-refresh.js; the
served page agreeing with the live route is tests/test-served-page-boots.js.

Each case imports nth_web in a fresh interpreter, because the build is
computed at import. Run: python3 tests/test-app-version.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


PROBE = r"""
import json, re, sys
sys.path.insert(0, sys.argv[1])
import nth_web as w
payload, ctype, cache, extra = w.PWA_ROUTES["/api/version"]
print(json.dumps({
    "build": w.APP_BUILD,
    "meta": re.search(r'<meta name="nth-build" content="([^"]*)">', w.INDEX_HTML).group(1),
    "payload": json.loads(payload),
    "cache": cache,
    "ctype": ctype,
}))
"""


def probe(server_dir, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NTH_APP_")}
    env.update(NTH_QUIET="1", **(env_extra or {}))
    out = subprocess.run([sys.executable, "-c", PROBE, str(server_dir)], env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        if "No module named 'mcp'" in out.stderr:
            print(out.stderr)   # run-all.sh reports this as a skip
            sys.exit(1)
        raise RuntimeError(out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def copy_server(tmp, name):
    dest = Path(tmp) / name
    shutil.copytree(SERVER, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


base = probe(SERVER)
check("the build id is 12 hex characters", bool(re.fullmatch(r"[0-9a-f]{12}", base["build"])), base["build"])
check("the page carries the same id in <meta name=\"nth-build\">", base["meta"] == base["build"])
check("/api/version serves only the build id", base["payload"] == {"build": base["build"]}, base["payload"])
check("/api/version is never cached", base["cache"] == "no-store", base["cache"])
check("/api/version is JSON", base["ctype"].startswith("application/json"), base["ctype"])
check("a second import of the same tree gives the same id", probe(SERVER)["build"] == base["build"])

with tempfile.TemporaryDirectory() as tmp:
    same = copy_server(tmp, "same")
    check("an unchanged copy of the tree gives the same id (no paths or times in it)",
          probe(same)["build"] == base["build"])

    js = copy_server(tmp, "js")
    with (js / "web" / "js" / "08-sidebar.js").open("a", encoding="utf-8") as f:
        f.write("\n// changed\n")
    check("a changed JS module changes the id", probe(js)["build"] != base["build"])

    css = copy_server(tmp, "css")
    with (css / "web" / "css" / "20-conversation.css").open("a", encoding="utf-8") as f:
        f.write("\n/* changed */\n")
    check("a changed stylesheet changes the id", probe(css)["build"] != base["build"])

    sw = copy_server(tmp, "sw")
    with (sw / "web" / "sw.js").open("a", encoding="utf-8") as f:
        f.write("\n// changed\n")
    check("a changed service worker changes the id", probe(sw)["build"] != base["build"])

renamed = probe(SERVER, {"NTH_APP_NAME": "Shared hub"})
check("a different app name (NTH_APP_NAME) changes the id", renamed["build"] != base["build"])
recoloured = probe(SERVER, {"NTH_APP_THEME": "#224466"})
check("a different app colour (NTH_APP_THEME) changes the id", recoloured["build"] != base["build"])
check("  and the two identities differ from each other", renamed["build"] != recoloured["build"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
