#!/usr/bin/env python3
"""Regression test: each hub can carry its own installable-app identity.

Covers:
  1. With nothing set, the manifest, page head and icons are the built-in ones.
  2. NTH_APP_NAME / NTH_APP_SHORT_NAME / NTH_APP_THEME reach the manifest and
     the page head, HTML-escaped and with control characters dropped.
  3. A malformed theme colour falls back to the built-in one.
  4. NTH_APP_ICON_DIR replaces exactly the icons it holds as PNGs of the right
     size; a missing, non-PNG, oversized, wrong-size or non-regular file (a
     FIFO must not hang the import) keeps the built-in icon, and a value that
     names no directory is reported.
  4b. The 1024 icons are listed only when they come from the same place as
     their 512 siblings, so a hub customised before they existed keeps its
     own artwork on the Android splash.
  5. NTH_APP_BACKGROUND reaches the manifest; malformed colours are reported.
  6. NTH_APP_DEFAULT_THEME: unset keeps light-1; a known theme id is written on
     <html> as both data-theme (first paint) and data-default-theme (what the
     client resets to); an unknown one falls back to light-1 with a warning
     that lists the valid ids. The head script that applies a saved theme
     before first paint is still in place. The browser half (a saved choice
     wins, Reset returns to the hub default) is tests/test-hub-default-theme.js.

The settings are read at import, so each case imports nth_web in a fresh
interpreter with its own environment.

Run: python3 tests/test-app-identity.py
"""
import json
import os
import struct
import zlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
ICONS = SERVER / "web" / "icons"
PNG = b"\x89PNG\r\n\x1a\n"



def png(width, height, payload=b""):
    """A minimal valid PNG with the given IHDR size."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (PNG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 4))
            + chunk(b"tEXt", b"note\0" + payload) + chunk(b"IEND", b""))


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
    "html_tag": re.search(r"<html[^>]*>", head).group(0),
    "head_script": re.search(r"<script>(.*?)</script>", head, re.S).group(1),
    "known_themes": w.KNOWN_THEMES,
    "icons": {name: hashlib.sha256(data).hexdigest() for name, data in w.PWA_ICONS.items()},
    "route_icon": hashlib.sha256(w.PWA_ROUTES["/icons/icon-192.png"][0]).hexdigest(),
    "route_apple": hashlib.sha256(w.PWA_ROUTES["/apple-touch-icon.png"][0]).hexdigest(),
}))
"""


def probe(env_extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NTH_APP_")}
    env.update(NTH_QUIET="1", **env_extra)
    try:
        out = subprocess.run([sys.executable, "-c", PROBE, str(SERVER)], env=env,
                             capture_output=True, text=True, check=True, timeout=60)
    except subprocess.TimeoutExpired:
        check("importing nth_web finishes (an icon read blocked)", False)
        print(f"\n{PASS} passed, {FAIL} failed")
        sys.exit(1)
    return json.loads(out.stdout.strip().splitlines()[-1]), out.stderr


def sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


builtin_manifest = json.loads((SERVER / "web" / "manifest.webmanifest").read_text())


def unversioned(manifest):
    """The manifest with each icon URL's ?v= content version removed."""
    out = dict(manifest)
    out["icons"] = [dict(i, src=i["src"].split("?", 1)[0]) for i in manifest.get("icons", [])]
    return out


def icon_version(path):
    import hashlib as _h
    return _h.sha256(Path(path).read_bytes()).hexdigest()[:12]

# 1. Defaults reproduce the built-in identity.
got, _ = probe({})
check("default manifest equals the built-in file apart from icon versions",
      unversioned(got["manifest"]) == builtin_manifest,
      (got["manifest"].get("name"), got["manifest"].get("short_name")))
check("each manifest icon URL carries the version of the bytes it serves",
      all(i["src"].endswith("?v=" + icon_version(ICONS / i["src"].split("?")[0].rsplit("/", 1)[-1]))
          for i in got["manifest"]["icons"]))
check("default page title is unchanged", got["title"] == "nth — chat with your agents", got["title"])
check("default Home Screen title is unchanged", got["apple_title"] == "nth", got["apple_title"])
check("default theme colour is unchanged", got["theme_meta"] == "#3d7a63", got["theme_meta"])
check("default icons are the built-in files",
      all(got["icons"][n] == sha(ICONS / n) for n in got["icons"]))

# 1b. The size each icon name promises matches the built-in PNGs and the manifest.
sys.path.insert(0, str(SERVER))
os.environ.setdefault("NTH_QUIET", "1")
import nth_web  # noqa: E402  (the defaults are what this checks)
for name, want in nth_web.PWA_ICON_SIZES.items():
    check(f"built-in {name} is {want}x{want}",
          nth_web._png_size((ICONS / name).read_bytes()) == (want, want))
for entry in builtin_manifest["icons"]:
    name = entry["src"].rsplit("/", 1)[-1]
    want = nth_web.PWA_ICON_SIZES[name]
    check(f"manifest declares {name} as {want}x{want}", entry["sizes"] == f"{want}x{want}")

# 2. Overrides reach the manifest and the page head, escaped and cleaned.
got, _ = probe({"NTH_APP_NAME": "Field <b>Hub</b>\x07", "NTH_APP_SHORT_NAME": 'Fi"eld',
                "NTH_APP_THEME": "#C0392B"})
m = got["manifest"]
check("manifest name override, control characters dropped", m["name"] == "Field <b>Hub</b>", m["name"])
check("manifest short_name override", m["short_name"] == 'Fi"eld', m["short_name"])
check("manifest theme_color override", m["theme_color"] == "#C0392B", m["theme_color"])
check("manifest keeps every other field",
      {k: v for k, v in unversioned(m).items() if k not in ("name", "short_name", "theme_color")}
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
import hashlib
with tempfile.TemporaryDirectory() as tmp:
    custom = png(192, 192, b"custom-192")
    (Path(tmp) / "icon-192.png").write_bytes(custom)
    (Path(tmp) / "apple-touch-icon.png").write_bytes(b"GIF89a not a png")
    (Path(tmp) / "badge-96.PNG").write_bytes(png(96, 96))  # misspelt: stays built-in
    (Path(tmp) / "icon-512.png").write_bytes(png(512, 512, b"\0" * (1024 * 1024 + 1)))
    (Path(tmp) / "icon-maskable-192.png").write_bytes(png(64, 64))
    os.mkfifo(Path(tmp) / "icon-maskable-512.png")
    (Path(tmp) / "badge-96.png").mkdir()
    got, err = probe({"NTH_APP_ICON_DIR": tmp})
    check("a PNG of the right size replaces the built-in icon",
          got["icons"]["icon-192.png"] == hashlib.sha256(custom).hexdigest())
    check("a replaced icon's manifest URL carries its own version, so phones fetch the new art",
          any(i["src"] == "/icons/icon-192.png?v=" + hashlib.sha256(custom).hexdigest()[:12]
              for i in got["manifest"]["icons"]))
    check("the replaced icon is what /icons/ serves",
          got["route_icon"] == hashlib.sha256(custom).hexdigest())
    check("a non-PNG keeps the built-in icon, also on /apple-touch-icon.png",
          got["icons"]["apple-touch-icon.png"] == sha(ICONS / "apple-touch-icon.png")
          and got["route_apple"] == sha(ICONS / "apple-touch-icon.png"))
    check("an oversized file keeps the built-in icon",
          got["icons"]["icon-512.png"] == sha(ICONS / "icon-512.png"))
    check("a PNG of the wrong size keeps the built-in icon",
          got["icons"]["icon-maskable-192.png"] == sha(ICONS / "icon-maskable-192.png"))
    check("a FIFO keeps the built-in icon without hanging the import",
          got["icons"]["icon-maskable-512.png"] == sha(ICONS / "icon-maskable-512.png"))
    check("a missing file keeps the built-in icon",
          got["icons"]["badge-96.png"] == sha(ICONS / "badge-96.png"))
    check("the start-up summary names custom and built-in icons, so a misspelt file shows",
          "NTH_APP_ICON_DIR: custom icon-192.png; built-in icon-512.png, icon-1024.png, "
          "icon-maskable-192.png, icon-maskable-512.png, icon-maskable-1024.png, "
          "apple-touch-icon.png, badge-96.png" in err, err)
    check("each rejected file is reported on stderr",
          all(s in err for s in ("apple-touch-icon.png: not a PNG",
                                 "icon-512.png: larger than 1 MB",
                                 "icon-maskable-192.png: 64x64, expected 192x192",
                                 "icon-maskable-512.png: not a regular file")), err)

with tempfile.TemporaryDirectory() as tmp:
    apple = png(180, 180, b"apple")
    (Path(tmp) / "apple-touch-icon.png").write_bytes(apple)
    got, _ = probe({"NTH_APP_ICON_DIR": tmp})
    check("a 180x180 Apple icon is accepted and served on /apple-touch-icon.png",
          got["route_apple"] == hashlib.sha256(apple).hexdigest())

# 4b. The 1024 icons follow their 512 siblings.
def listed(manifest):
    return {i["src"].split("?", 1)[0].rsplit("/", 1)[-1] for i in manifest["icons"]}


with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "icon-512.png").write_bytes(png(512, 512, b"old-set"))
    (Path(tmp) / "icon-maskable-512.png").write_bytes(png(512, 512, b"old-set-m"))
    got, _ = probe({"NTH_APP_ICON_DIR": tmp})
    check("a custom set without 1024 files lists no built-in 1024 icon",
          listed(got["manifest"]) == {"icon-192.png", "icon-512.png",
                                      "icon-maskable-192.png", "icon-maskable-512.png"},
          listed(got["manifest"]))
    big = png(1024, 1024, b"new-set")
    (Path(tmp) / "icon-1024.png").write_bytes(big)
    got, _ = probe({"NTH_APP_ICON_DIR": tmp})
    check("a custom 1024 icon next to a custom 512 is listed under its own version",
          "/icons/icon-1024.png?v=" + hashlib.sha256(big).hexdigest()[:12]
          in [i["src"] for i in got["manifest"]["icons"]])
    check("each 1024 entry is decided on its own sibling",
          "icon-maskable-1024.png" not in listed(got["manifest"]), listed(got["manifest"]))

with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "icon-1024.png").write_bytes(png(1024, 1024, b"orphan"))
    (Path(tmp) / "icon-maskable-512.png").write_bytes(png(64, 64))  # refused: built-in
    got, err = probe({"NTH_APP_ICON_DIR": tmp})
    check("a custom 1024 without a custom 512 is left out of the manifest",
          "icon-1024.png" not in listed(got["manifest"]), listed(got["manifest"]))
    check("and the start-up log says why",
          "icon-1024.png: custom but icon-512.png is built-in; not listed in the manifest" in err, err)
    check("a refused 512 counts as built-in, so the built-in 1024 beside it stays listed",
          "icon-maskable-1024.png" in listed(got["manifest"]), listed(got["manifest"]))

# A platform without O_NONBLOCK / O_NOCTTY (Windows) still loads custom icons.
with tempfile.TemporaryDirectory() as tmp:
    plain = png(192, 192, b"plain")
    (Path(tmp) / "icon-192.png").write_bytes(plain)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NTH_APP_")}
    env.update(NTH_QUIET="1", NTH_APP_ICON_DIR=tmp)
    snippet = ("import os, sys, hashlib\n"
               "for flag in ('O_NONBLOCK', 'O_NOCTTY'):\n"
               "    if hasattr(os, flag): delattr(os, flag)\n"
               "sys.path.insert(0, sys.argv[1])\n"
               "import nth_web\n"
               "print(hashlib.sha256(nth_web.PWA_ICONS['icon-192.png']).hexdigest())\n")
    out = subprocess.run([sys.executable, "-c", snippet, str(SERVER)], env=env,
                         capture_output=True, text=True, timeout=60)
    check("custom icons load where O_NONBLOCK and O_NOCTTY do not exist",
          out.returncode == 0 and out.stdout.strip().endswith(hashlib.sha256(plain).hexdigest()),
          (out.returncode, out.stderr[-300:]))

got, err = probe({"NTH_APP_ICON_DIR": "/nonexistent/app-icons"})
check("an icon directory that does not exist is reported and changes nothing",
      "is not a directory" in err
      and all(got["icons"][n] == sha(ICONS / n) for n in got["icons"]), err)

# 5. Splash background and colour warnings.
got, err = probe({"NTH_APP_BACKGROUND": "#0b0405", "NTH_APP_THEME": "c0392b"})
check("background colour override reaches the manifest",
      got["manifest"]["background_color"] == "#0b0405", got["manifest"]["background_color"])
check("a malformed theme colour is reported", "NTH_APP_THEME='c0392b' is not #rrggbb" in err, err)

# 6. Per-hub default theme.
TOKENS_CSS = (SERVER / "web" / "css" / "00-tokens.css").read_text(encoding="utf-8")
got, err = probe({})
check("unset default theme keeps today's light-1 on <html>",
      got["html_tag"] == '<html lang="en" data-theme="light-1" data-default-theme="light-1">',
      got["html_tag"])
check("no theme warning when the default theme is unset", "NTH_APP_DEFAULT_THEME" not in err, err)
known = got["known_themes"]
check("the theme ids come from the client's list and include Rescue",
      known.get("inspired-rescue") == "Rescue" and known.get("light-1") == "Sagebrush"
      and len(known) == 21, known)
check("every known theme id has design tokens",
      all(f'[data-theme="{tid}"]' in TOKENS_CSS for tid in known),
      [tid for tid in known if f'[data-theme="{tid}"]' not in TOKENS_CSS])
check("the head script still applies a saved theme before first paint",
      "localStorage.getItem('trio.preferences.v1')" in got["head_script"]
      and "document.documentElement.dataset.theme = __t" in got["head_script"])

got, err = probe({"NTH_APP_DEFAULT_THEME": "inspired-rescue"})
check("a valid default theme is written for first paint and for Reset",
      got["html_tag"] == '<html lang="en" data-theme="inspired-rescue" '
                         'data-default-theme="inspired-rescue">', got["html_tag"])
check("a valid default theme is not reported", "NTH_APP_DEFAULT_THEME" not in err, err)

got, _ = probe({"NTH_APP_DEFAULT_THEME": "  Inspired-Rescue "})
check("the theme id is matched without regard to case or surrounding space",
      'data-default-theme="inspired-rescue"' in got["html_tag"], got["html_tag"])

got, err = probe({"NTH_APP_DEFAULT_THEME": 'rescue"><script>'})
check("an unknown default theme falls back to light-1",
      got["html_tag"] == '<html lang="en" data-theme="light-1" data-default-theme="light-1">',
      got["html_tag"])
check("an unknown default theme is reported with the ids that would work",
      "NTH_APP_DEFAULT_THEME='rescue\"><script>' is not a theme id; using light-1" in err
      and "inspired-rescue (Rescue)" in err, err)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
