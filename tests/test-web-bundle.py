"""The served page must still be the page, composed from server/web/.

The browser bundle used to be one 5,220-line string literal inside
nth_web.py. It is now an HTML skeleton plus ordered CSS/JS layers on disk,
inlined at import time. That swap is invisible at runtime only for as long as
composition preserves three things, so each gets a test here:

  * CONTENT — every byte of every layer reaches the page. A silently dropped
    layer is a dashboard that renders unstyled or half-dead, with nothing in
    any log to say why.
  * ORDER — CSS cascades in list order and scripts execute in list order.
    Reordering WEB_CSS_FILES is a real visual change with no diff in any .css
    file, so the order is asserted against the page, not against the tuple.
  * SCOPE — each JS module is its own IIFE communicating only through the
    window.Trio namespace. A module that leaks a bare global, or one placed
    before something it reads at definition time, breaks at runtime in the
    browser, where this suite would never see it.

Usage: python tests/test-web-bundle.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
os.environ.setdefault("NTH_HOME", tempfile.mkdtemp(prefix="nth_bundle_"))

import nth_web as web    # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


served = web.INDEX_HTML

# ── it is still a page ─────────────────────────────────────────────────
check("the composed bundle is a complete document",
      served.lstrip().startswith("<!doctype html>")
      and served.rstrip().endswith("</html>"))
check("the soft keyboard resizes the layout viewport",
      re.search(r'<meta\s+name="viewport"\s+content="[^"]*'
                r'interactive-widget=resizes-content[^"]*">', served) is not None)

# ── every marker was substituted ───────────────────────────────────────
# A surviving marker means a substitution silently missed its file — the
# per-file rendering's one new failure mode over the old single pass.
leftover = [m for m in ("<!--__TRIO_STYLES__-->", "<!--__TRIO_SCRIPTS__-->",
                        "/*__STT_LANG__*/", "/*__ASK_HELPERS__*/")
            if m in served]
check("no template marker survives into the served page"
      + (f" — LEFT: {', '.join(leftover)}" if leftover else ""), not leftover)
check("the dictation language was injected as a quoted tag",
      re.search(r'recognition\.lang = "[a-z]{2}-[A-Z]{2}"', served) is not None)
check("the Character icon attribution reaches the Settings drawer",
      "SVG Repo" in served
      and "creativecommons.org/licenses/by/4.0/" in served)
# The ask helpers are a separate file so Node can require() them; if the
# injection silently no-ops, every interactive question renders as plain text
# with a ReferenceError in the console and nothing server-side to notice.
check("the ask helpers were injected, not left as a comment",
      "function isAskChoices(" in served and "function composeAnswer(" in served)
check("the Atrium brand uses the message-square-chat icon",
      'M18 9V7.2C18 6.0799' in served
      and 'M 0.049804 0.049804' not in served)
check("the Preferences page carries icon attribution",
      "Character and brand icons from" in served
      and "creativecommons.org/licenses/by/4.0/" in served)

# ── the declaration must match the directory ───────────────────────────
# GROUND TRUTH IS THE DIRECTORY, NOT THE TUPLE. Deriving the expectation from
# WEB_CSS_FILES would make this unfalsifiable: reorder or delete an entry and
# both sides of the comparison move together. (Both mutations survived an
# earlier version of this file that did exactly that.) The numeric filename
# prefixes encode the intended cascade, so sorted() is an independent
# statement of the same contract, and a layer added to disk but never declared
# — invisible in the browser — fails here too.
WEB = SERVER / "web"
disk_css_files = [f"css/{p.name}" for p in sorted((WEB / "css").iterdir())
                  if p.suffix == ".css"]
disk_js_files = [f"js/{p.name}" for p in sorted((WEB / "js").iterdir())
                 if p.suffix == ".js"]

check(f"WEB_CSS_FILES declares every layer on disk, in prefix order "
      f"({len(disk_css_files)} found)",
      list(web.WEB_CSS_FILES) == disk_css_files)

# Inspired presets must be full component skins. A token-only preset would
# technically appear in the picker while still rendering the same modern
# rounded cards, which is precisely the failure this feature is meant to avoid.
tokens_css = (WEB / "css" / "00-tokens.css").read_text(encoding="utf-8")
historic_css = (WEB / "css" / "35-historic.css").read_text(encoding="utf-8")
for preset in ("historic-win98", "historic-gameboy", "historic-geocities",
               "inspired-ipod", "inspired-messenger", "inspired-slack"):
    check(f"{preset} declares design tokens and a component skin",
          f'[data-theme="{preset}"]' in tokens_css
          and historic_css.count(f'[data-theme="{preset}"]') >= 12)
check("Inspired skins include platform-native control vocabularies",
      "::-webkit-scrollbar-button" in historic_css
      and "border-left:8px solid #20251a" in historic_css
      and "border:6px ridge #0ff" in historic_css
      and ".gb-dpad" in historic_css
      and ".gb-round" in historic_css
      and ".ipod-wheel" in historic_css
      and "repeating-linear-gradient(0deg,#d9d9d9" in historic_css
      and "background:#4a154b" in historic_css
      and "flex-direction:row; width:100%; max-width:none" in historic_css
      and "font-family:\"Lato\"" in historic_css
      and ".messages { padding:18px 0 10px; }" in historic_css
      and "-webkit-font-smoothing:antialiased" in historic_css
      and ".conversation-header { padding-left:14px; }" in historic_css
      and "background:#69386b" in historic_css
      and ".dm-item > .av {" in historic_css
      and "border-radius:5px" in historic_css)
check("retired Windows 3.1 preset is absent from tokens and component skins",
      "historic-win31" not in tokens_css and "historic-win31" not in historic_css)

# JS gets the SAME independent oracle as CSS. It did not used to: load order
# was a dependency order that CONTRADICTED the filename prefixes (02 before 00,
# because core installed a fallback Trio.api that won if it loaded first), so
# the directory could only prove the set and the order was asserted as six
# hand-picked claims. Four of those six were satisfied by a plain sort anyway,
# so they asserted nothing the naming already gave — and the tuple still had
# unasserted edges, which is what made a reviewer call it scar tissue rather
# than a contract.
#
# Core now requires the store and the api instead of shadowing them, and the
# files were renumbered so the prefixes tell the truth. That buys back a real
# oracle: reorder the tuple ANY way and this fails, without anyone having to
# have predicted which pair would matter.
check(f"WEB_JS_FILES declares every module on disk, in prefix order "
      f"({len(disk_js_files)} found)",
      list(web.WEB_JS_FILES) == disk_js_files)

# The prefixes only mean something if the dependencies actually run that way.
# These are the definition-time reads that the numbering has to keep satisfied
# — stated so that renumbering a file to a "tidier" slot cannot quietly break
# it, and so the sorted-order check above is grounded in why rather than in
# convention alone.
order = {name: i for i, name in enumerate(web.WEB_JS_FILES)}
for earlier, later, why in (
    ("js/01-store.js", "js/06-core.js",
     "core requires the store to exist and throws if it does not"),
    ("js/02-api.js", "js/06-core.js",
     "core requires the api to exist and throws if it does not"),
    ("js/06-core.js", "js/11-conversation.js",
     "the conversation resolves avatar tones through Trio.avatarTone"),
    ("js/06-core.js", "js/12-composer.js",
     "the composer publishes onto Trio.actions"),
    ("js/06-core.js", "js/20-workspace.js",
     "the workspace resolves avatar tones through Trio.avatarTone"),
    ("js/09-ui.js", "js/46-data.js",
     "the data page confirms through Trio.ui"),
    ("js/10-markdown.js", "js/11-conversation.js",
     "the conversation renders message bodies through the markdown module"),
    ("js/06-core.js", "js/90-boot.js", "boot calls Trio.boot()"),
    ("js/07-lifecycle.js", "js/90-boot.js",
     "boot mounts every feature through Trio.lifecycle"),
    ("js/20-workspace.js", "js/90-boot.js",
     "boot mounts the workspace feature by name"),
):
    check(f"{earlier} is declared before {later} — {why}",
          earlier in order and later in order
          and order.get(earlier, -1) < order.get(later, -1))

check("the test hook is declared last, after everything it inspects",
      web.WEB_JS_FILES[-1] == "js/99-test-hook.js")

# ── content: nothing is dropped ────────────────────────────────────────
page_css = "".join(re.findall(r"<style[^>]*>\n(.*?)</style>", served, re.DOTALL))
page_js = "".join(re.findall(r"<script[^>]*>\n(.*?)</script>", served, re.DOTALL))

# Per-file and driven off the directory, so dropping an entry from the tuple
# leaves its file on disk and unfound in the page.
for name in disk_css_files:
    body = web._render_web_source(name)
    check(f"{name} reaches the page ({len(body)} chars)", body in page_css)
for name in disk_js_files:
    # 99-test-hook.js is deliberately gutted on the way out (see below), so it
    # is the one module whose disk content must NOT appear verbatim.
    if name == "js/99-test-hook.js":
        continue
    body = web._render_web_source(name)
    check(f"{name} reaches the page ({len(body)} chars)", body in page_js)

check("the page carries no CSS beyond the declared layers",
      len(page_css) == sum(len(web._render_web_source(n)) for n in disk_css_files))

# ── order: the cascade is the contract ─────────────────────────────────
seen = [m.group(1) for m in
        re.finditer(r'<(?:style|script) data-trio-source="([^"]+)"', served)]
# CSS is checked against the directory (an independent oracle); JS against the
# declaration, whose order the constraints above already pin. What this adds
# for JS is that the composer emits each module exactly once and in the order
# it was told to — a duplicate <script> would re-run a module's IIFE and
# re-register its listeners.
check("every layer is inlined exactly once, in declared order",
      seen == disk_css_files + list(web.WEB_JS_FILES))

# Order asserted against real content too, not only the tags: a correct list of
# data-trio-source attributes with the bodies emitted in some other order would
# pass everything above and still cascade wrong. Probes are the layer bodies
# themselves rather than a hardcoded selector, so changing a breakpoint or
# renaming a token does not fake a failure here.
first_css = web._render_web_source(disk_css_files[0])
last_css = web._render_web_source(disk_css_files[-1])
check(f"{disk_css_files[-1]} is inlined after {disk_css_files[0]} — the last "
      f"layer overrides the first, so their order in the page is the cascade",
      served.index(last_css) > served.index(first_css))

# ── scope: one <script> per module, each parsing on its own ────────────
# Counted by data-trio-source: index.html carries its own inline script (the
# pre-paint theme restore, which must run before any layer loads), and a bare
# count would fold that in and drift the moment another is added.
check(f"every declared module gets its own <script> tag "
      f"({len(web.WEB_JS_FILES)} expected)",
      len(re.findall(r'<script data-trio-source="js/', served))
      == len(web.WEB_JS_FILES))

# The modules share nothing but the window.Trio namespace, so each must be a
# complete program by itself. A module that opened a brace or a closure and
# never closed it would still concatenate into a page that *looks* fine here
# while swallowing the next module's source into its own scope — the browser
# would be the first thing to notice. Parsing each rendered body alone is the
# direct test of that, and it checks what ships (post-substitution), not what
# is on disk.
node = shutil.which("node")
if not node:
    print("SKIP: per-module parse — node not installed")
else:
    tmpdir = Path(tempfile.mkdtemp(prefix="nth_bundle_js_"))
    for name in web.WEB_JS_FILES:
        probe = tmpdir / name.replace("/", "_")
        probe.write_text(web._render_web_source(name), encoding="utf-8")
        result = subprocess.run([node, "--check", str(probe)],
                                capture_output=True, text=True, timeout=60)
        check(f"{name} parses as a standalone program"
              + ("" if result.returncode == 0
                 else f" — {result.stderr.strip().splitlines()[-1:]}"),
              result.returncode == 0)

# ── the test hook never ships ──────────────────────────────────────────
check("the test hook is stripped from the served bundle",
      "__TRIO_TEST_HOOK_START__" not in served
      and any("__TRIO_TEST_HOOK_START__" in web._read_web_source(n)
              for n in disk_js_files))

# ── the sources must stay TEXT ─────────────────────────────────────────
# A raw control byte in a source file makes git classify the whole file as
# binary: `git diff` prints "Binary files differ", `git diff --check` skips it,
# and it silently drops out of every line-based review. One module sat in that
# state for an entire branch — reviewed by nobody, including its author — and a
# second one was introduced while FIXING the first, by typing a separator that
# came out as a literal NUL rather than an escape. The intent (a delimiter no
# id can contain) is fine; '\\u0000' expresses it and keeps the file reviewable.
binary = []
for name in list(disk_css_files) + list(disk_js_files) + ["index.html"]:
    raw = (WEB / name).read_bytes()
    if b"\x00" in raw:
        binary.append(f"{name} (NUL at byte {raw.index(chr(0).encode())})")
check("no web source contains a raw control byte — git would treat it as "
      "binary and drop it out of every diff"
      + (f" — {', '.join(binary)}" if binary else ""),
      not binary)

# ── the reader refuses to leave server/web/ ────────────────────────────
escaped = None
try:
    web._read_web_source("../nth_web.py")
except ValueError:
    escaped = True
except Exception as exc:                       # noqa: BLE001
    escaped = f"wrong error: {type(exc).__name__}"
check(f"the source reader rejects a path escaping server/web/ ({escaped})",
      escaped is True)

missing = None
try:
    web._read_web_source("css/not-a-real-layer.css")
except RuntimeError as exc:
    missing = "required web source missing" in str(exc)
check("a missing layer raises rather than serving a broken page", missing is True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print(f"OK — {len(web.WEB_CSS_FILES)} CSS layers + {len(web.WEB_JS_FILES)} JS "
      f"composed into {len(served)} chars, in order, with nothing dropped")
