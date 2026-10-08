#!/usr/bin/env python3
"""Regression test: each hub can carry its own installable-app identity.

Covers:
  1. With nothing set, the manifest, page head and icons are the built-in ones.
  2. NTH_APP_NAME / NTH_APP_SHORT_NAME / NTH_APP_THEME reach the manifest and
     the page head, HTML-escaped and with control characters dropped.
  3. A malformed theme colour falls back to the built-in one.
  4. NTH_APP_ICON_DIR replaces exactly the icons it holds as PNGs; a missing,
     non-PNG or oversized file keeps the built-in icon.

The settings are read at import, so each case imports nth_web in a fresh
interpreter with its own environment.

Run: python3 tests/test-app-identity.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
ICONS = SERVER / "web" / "icons"
PNG = b"\x89PNG\r\n\x1a\n"

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
import hashlib, json, re, sys
sys.path.insert(0, sys.argv[1])
import nth_web as w
head = w.INDEX_HTML.split("</head>", 1)[0]
print(json.dumps({
    "manifest": json.loads(w.PWA_MANIFEST),
    "title": re.search(r"<title>(.*?)</title>", head).group(1),
    "apple_title": re.search(r'apple-mobile-web-app-title" content="([^"]*)"', head).group(1),
    "theme_meta": re.search(r'name="theme-color" content="([^"]*)"', head).group(1),
    "icons": {name: hashlib.sha256(data).hexdigest() for name, data in w.PWA_ICONS.items()},
    "route_icon": hashlib.sha256(w.PWA_ROUTES["/icons/icon-192.png"][0]).hexdigest(),
    "route_apple": hashlib.sha256(w.PWA_ROUTES["/apple-touch-icon.png"][0]).hexdigest(),
}))
"""


def probe(env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NTH_APP_")}
    env.update(NTH_QUIET="1", **env_extra)
    out = subprocess.run([sys.executable, "-c", PROBE, str(SERVER)], env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


def sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


builtin_manifest = json.loads((SERVER / "web" / "manifest.webmanifest").read_text())

# 1. Defaults reproduce the built-in identity.
got, _ = probe({})
check("default manifest equals the built-in file", got["manifest"] == builtin_manifest,
      (got["manifest"].get("name"), got["manifest"].get("short_name")))
check("default page title is unchanged", got["title"] == "nth — chat with your agents", got["title"])
check("default Home Screen title is unchanged", got["apple_title"] == "nth", got["apple_title"])
check("default theme colour is unchanged", got["theme_meta"] == "#3d7a63", got["theme_meta"])
check("default icons are the built-in files",
      all(got["icons"][n] == sha(ICONS / n) for n in got["icons"]))

# 2. Overrides reach the manifest and the page head, escaped and cleaned.
got, _ = probe({"NTH_APP_NAME": "Field <b>Hub</b>\x07", "NTH_APP_SHORT_NAME": 'Fi"eld',
                "NTH_APP_THEME": "#C0392B"})
m = got["manifest"]
check("manifest name override, control characters dropped", m["name"] == "Field <b>Hub</b>", m["name"])
check("manifest short_name override", m["short_name"] == 'Fi"eld', m["short_name"])
check("manifest theme_color override", m["theme_color"] == "#C0392B", m["theme_color"])
check("manifest keeps every other field",
      {k: v for k, v in m.items() if k not in ("name", "short_name", "theme_color")}
      == {k: v for k, v in builtin_manifest.items() if k not in ("name", "short_name", "theme_color")})
check("page title escaped", got["title"] == "Field &lt;b&gt;Hub&lt;/b&gt;", got["title"])
check("Home Screen title escaped", got["apple_title"] == "Fi&quot;eld", got["apple_title"])
check("theme meta override", got["theme_meta"] == "#C0392B", got["theme_meta"])

# 3. Malformed colour and blank names fall back.
got, _ = probe({"NTH_APP_THEME": "red; background:url(x)", "NTH_APP_NAME": "   ",
                "NTH_APP_SHORT_NAME": "x" * 80})
check("malformed theme colour falls back", got["theme_meta"] == "#3d7a63", got["theme_meta"])
check("blank name falls back", got["manifest"]["name"] == builtin_manifest["name"])
check("short name clipped to 24 characters", got["manifest"]["short_name"] == "x" * 24)

# 4. Icon directory: replaces what it holds, falls back otherwise.
with tempfile.TemporaryDirectory() as tmp:
    custom = PNG + b"custom-192"
    (Path(tmp) / "icon-192.png").write_bytes(custom)
    (Path(tmp) / "apple-touch-icon.png").write_bytes(b"GIF89a not a png")
    (Path(tmp) / "icon-512.png").write_bytes(PNG + b"\0" * (1024 * 1024 + 1))
    import hashlib
    got, err = probe({"NTH_APP_ICON_DIR": tmp})
    check("a PNG in the icon directory replaces the built-in icon",
          got["icons"]["icon-192.png"] == hashlib.sha256(custom).hexdigest())
    check("the replaced icon is what /icons/ serves",
          got["route_icon"] == hashlib.sha256(custom).hexdigest())
    check("a non-PNG keeps the built-in icon, also on /apple-touch-icon.png",
          got["icons"]["apple-touch-icon.png"] == sha(ICONS / "apple-touch-icon.png")
          and got["route_apple"] == sha(ICONS / "apple-touch-icon.png"))
    check("an oversized file keeps the built-in icon",
          got["icons"]["icon-512.png"] == sha(ICONS / "icon-512.png"))
    check("a missing file keeps the built-in icon",
          got["icons"]["badge-96.png"] == sha(ICONS / "badge-96.png"))
    check("each rejected file is reported once on stderr",
          "apple-touch-icon.png: not a PNG" in err and "icon-512.png: larger than 1 MB" in err, err)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
