"""setup.sh must install every module the installed modules import.

setup.sh copies server files by NAME, in two separate lists (the hub-service
deploy near the top, and the hub/spoke install near the bottom). Adding a new
server module means remembering both. Nobody does, and the failure is total but
invisible from the repo: `python3 server/nth_web.py` works perfectly from a
checkout — where every sibling is present — and the installed copy dies on
`ModuleNotFoundError` at import, before it can log anything.

So this does not hand-maintain a third list to drift alongside the other two.
It reads setup.sh, then walks the import graph of what setup.sh says it
installs, and requires the closure to be installed too.

Usage: python tests/test-install-manifest.py
"""
import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "server"
SETUP = ROOT / "setup.sh"

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


text = SETUP.read_text()

# List A — hub-service deploy: a `for f in a.py b.py ...; do` loop.
loop = re.search(r"for f in ((?:[\w./\\$]+\.py\s*\\?\s*)+);\s*do", text)
hub_service = set(re.findall(r"(\w+\.py)", loop.group(1))) if loop else set()

# List B — hub/spoke install: individual `cp "$SCRIPT_DIR/server/x.py" ...`.
hub_spoke = set(re.findall(r'cp "\$SCRIPT_DIR/server/(\w+\.py)"', text))

check("setup.sh: the hub-service copy loop was found and is non-empty",
      len(hub_service) > 3)
check("setup.sh: the hub/spoke copy list was found and is non-empty",
      len(hub_spoke) > 3)


def imported_by(module_file: Path) -> set:
    """Every sibling `nth_*` / `codex_*` module this file imports, at any depth
    of nesting — a deferred import inside a function still needs the file to be
    on disk when that function runs."""
    try:
        tree = ast.parse(module_file.read_text())
    except (OSError, SyntaxError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {name for name in found
            if (SERVER / f"{name}.py").exists()}


def closure(seed_files: set, label: str) -> set:
    """Everything reachable by import from `seed_files`."""
    seen, queue = set(), [f for f in seed_files]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        for dep in imported_by(SERVER / name):
            queue.append(f"{dep}.py")
    return seen


for label, installed in (("hub-service", hub_service), ("hub/spoke", hub_spoke)):
    present = {f for f in installed if (SERVER / f).exists()}
    check(f"{label}: every file the list names actually exists in server/",
          present == installed)
    required = closure(present, label)
    missing = sorted(required - installed)
    check(f"{label}: installs every module its own files import"
          + (f" — MISSING: {', '.join(missing)}" if missing else ""),
          not missing)

# ── Data files, not just modules ───────────────────────────────────────
# nth_web.py composes its page from server/web/ at IMPORT time. That is a
# dependency the import-closure walk above cannot see: it is data, not an
# `import`. Missing it is the same total-but-invisible failure — the repo runs
# fine, the installed copy raises before serving anything.

web_copied = re.findall(r'cp -R "\$SCRIPT_DIR/server/web"', text)
check("setup.sh: both install paths copy server/web/ recursively "
      f"(found {len(web_copied)})", len(web_copied) == 2)


def declared_assets() -> list:
    """Read WEB_CSS_FILES / WEB_JS_FILES out of nth_web.py without importing."""
    tree = ast.parse((SERVER / "nth_web.py").read_text())
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", None) in ("WEB_CSS_FILES", "WEB_JS_FILES"):
                names += [el.value for el in node.value.elts]
    return names


assets = declared_assets()
check(f"nth_web.py declares its web assets ({len(assets)} found)", len(assets) > 1)
absent = [a for a in assets + ["index.html"] if not (SERVER / "web" / a).exists()]
check("every declared web asset exists on disk"
      + (f" — MISSING: {', '.join(absent)}" if absent else ""), not absent)

# ── The real guard: build what setup.sh installs, and import it ────────
# Everything above reads setup.sh and believes it. This does not: it copies
# ONLY the files setup.sh names into an empty tree and imports nth_web there.
# Delete either `cp -R server/web` line and this goes red, which is exactly
# what the named-module lists failed to do for three releases.
import os
import shutil
import subprocess
import tempfile

staging = Path(tempfile.mkdtemp(prefix="nth_install_"))
try:
    dest = staging / "server"
    dest.mkdir()
    for f in sorted(hub_spoke):
        if (SERVER / f).exists():
            shutil.copy(SERVER / f, dest / f)
    if web_copied:                      # only if setup.sh actually says to
        shutil.copytree(SERVER / "web", dest / "web")

    env = dict(os.environ, NTH_HOME=str(staging / "home"),
               PYTHONPATH=str(dest))
    proc = subprocess.run([sys.executable, "-c", "import nth_web"],
                          capture_output=True, text=True, timeout=120, env=env)
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    check("a tree containing only what setup.sh installs can import nth_web"
          + (f" — {detail[-1][:160]}" if proc.returncode and detail else ""),
          proc.returncode == 0)
finally:
    shutil.rmtree(staging, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    print("\nAdd the missing module(s) to BOTH copy lists in setup.sh. An "
          "installed nth_web.py that cannot import its own dependency fails "
          "at startup, and only on an installed copy — never in the repo.")
    sys.exit(1)
print("OK — setup.sh installs the full import closure of what it ships")
