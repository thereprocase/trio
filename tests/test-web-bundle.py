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
  * SCOPE — the client is a single IIFE whose functions all share one closure.
    Split across two <script> tags it stops working, and it stops working at
    runtime in the browser, where this suite would never see it.

Usage: python tests/test-web-bundle.py
"""
import os
import re
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

# ── every marker was substituted ───────────────────────────────────────
# A surviving marker means a substitution silently missed its file — the
# per-file rendering's one new failure mode over the old single pass.
leftover = [m for m in ("<!--__TRIO_STYLES__-->", "<!--__TRIO_SCRIPTS__-->",
                        "/*__ANIMAL_EMOJIS__*/", "/*__ANIMAL_NAMES__*/",
                        "/*__STT_LANG__*/") if m in served]
check("no template marker survives into the served page"
      + (f" — LEFT: {', '.join(leftover)}" if leftover else ""), not leftover)
check("the emoji list was injected as real JSON",
      re.search(r"const ANIMAL_EMOJIS = \[", served) is not None)
check("the dictation language was injected as a quoted tag",
      re.search(r'const STT_WEB_LANG = "[a-z]{2}-[A-Z]{2}"', served) is not None)

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
check(f"WEB_JS_FILES declares every script on disk, in prefix order "
      f"({len(disk_js_files)} found)",
      list(web.WEB_JS_FILES) == disk_js_files)

# ── content: nothing is dropped ────────────────────────────────────────
page_css = "".join(re.findall(r"<style[^>]*>\n(.*?)</style>", served, re.DOTALL))
page_js = "".join(re.findall(r"<script[^>]*>\n(.*?)</script>", served, re.DOTALL))

# Per-file and driven off the directory, so dropping an entry from the tuple
# leaves its file on disk and unfound in the page.
for name in disk_css_files:
    body = web._render_web_source(name)
    check(f"{name} reaches the page ({len(body)} chars)", body in page_css)
for name in disk_js_files:
    body = web._strip_test_hook(web._render_web_source(name))
    check(f"{name} reaches the page ({len(body)} chars)", body in page_js)

check("the page carries no CSS beyond the declared layers",
      len(page_css) == sum(len(web._render_web_source(n)) for n in disk_css_files))

# ── order: the cascade is the contract ─────────────────────────────────
seen = [m.group(1) for m in
        re.finditer(r'<(?:style|script) data-trio-source="([^"]+)"', served)]
check("every layer is inlined exactly once, in cascade order",
      seen == disk_css_files + disk_js_files)

# Order asserted against real content too, not only the tags: the responsive
# overrides must land after the tokens they override.
check("CSS layers are inlined in cascade order",
      served.index("@media (max-width: 768px)") > served.index(":root {"))

# ── scope: one closure, one script tag ─────────────────────────────────
check("the client ships as a single <script> — splitting it breaks the closure",
      len(re.findall(r"<script[^>]*>", served)) == len(web.WEB_JS_FILES) == 1)
check("the whole IIFE is contiguous in one block",
      page_js.count("})();") >= 1 and "(() => {" in page_js)

# ── the test hook never ships ──────────────────────────────────────────
check("the test hook is stripped from the served bundle",
      "__TRIO_TEST_HOOK_START__" not in served
      and any("__TRIO_TEST_HOOK_START__" in web._read_web_source(n)
              for n in disk_js_files))

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
