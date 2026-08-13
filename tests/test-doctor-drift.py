"""Tests for nth_doctor's stale-install detection.

setup.sh COPIES the repo into the install directory, so pulling new code
changes nothing that runs until it is re-run. The failure mode is silent: a
feature is present in the checkout and simply absent at runtime.

Two things this file deliberately pins beyond the happy path, both because a
review found them unpinned:

  * The CHECK, not just the helper. An earlier version of these tests passed
    with the check deleted from run_checks entirely — the helper was covered
    and its wiring was not. The label, the level and the remediation wording
    are the parts an operator acts on.
  * SILENCE on correct installs. A freshness warning that fires on a good
    deploy is worse than no warning, because it is the same warning people
    would need to trust on the day it is real.

Usage: python tests/test-doctor-drift.py
"""
import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
DOCTOR = SERVER / "nth_doctor.py"
spec = importlib.util.spec_from_file_location("nth_doctor", DOCTOR)
doc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc)

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth_doctor_"))


def copy_install(name, mutate=None, skip=None):
    """A copy-deploy install: every server/*.py copied to its own directory."""
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    for src in SERVER.glob("*.py"):
        if skip and src.name == skip:
            continue
        (d / src.name).write_bytes(src.read_bytes())
    if mutate:
        (d / mutate).write_text("# drifted\n")
    return d


try:
    # --- the case the check exists for -------------------------------------
    stale = copy_install("stale", mutate="nth_web.py")
    row = doc._freshness_check(stale)
    check("a drifted file is reported", row is not None)
    if row:
        label, level, detail = row
        check("level is WARN, not FAIL (stale is not broken)", level == doc.WARN)
        check("label fits doctor's 12-char column", len(label) <= 12)
        check("detail names the drifted file", "nth_web.py" in detail)
        check("detail names the install directory", str(stale) in detail)
        check("detail gives the spoke remedy", "setup.sh" in detail)
        check("spoke remedy mentions restarting Claude Code",
              "restart Claude Code" in detail)
        # Direction-neutral: an install can legitimately be NEWER than the
        # checkout being run from (a second clone, a bisect, an old tag), and
        # "run setup.sh" would then clobber a correct install with older code.
        # Word boundaries, not substrings: the detail carries absolute paths,
        # and "/var/folders/" contains "older". A plain `in` test failed here
        # on a message that was perfectly correct.
        check("detail does not claim the checkout is newer",
              not re.search(r"\b(newer|older)\b", detail, re.I))
        check("detail does not issue a bare directional order",
              "run `bash setup.sh`" not in detail)

    missing = copy_install("missing", skip="nth_web.py")
    check("a file missing from the install is reported",
          doc._freshness_check(missing) is not None)

    # --- everything below must stay silent ---------------------------------
    fresh = copy_install("fresh")
    check("an identical copy install is silent", doc._freshness_check(fresh) is None)

    check("doctor's own directory is silent", doc._freshness_check(SERVER) is None)

    # link.sh symlinks individual FILES into a real directory, so the install
    # dir never compares equal to the checkout — it is the CONTENT read through
    # those links that matches. A directory-equality check would have missed
    # this and warned on every symlinked dev install.
    linked = tmp / "linked"
    linked.mkdir(exist_ok=True)
    for src in SERVER.glob("*.py"):
        (linked / src.name).symlink_to(src)
    check("a link.sh-shaped symlink install is silent",
          doc._freshness_check(linked) is None)

    check("a nonexistent install directory is silent",
          doc._freshness_check(tmp / "nope") is None)
    check("None install directory is silent", doc._freshness_check(None) is None)

    # --- hub-service installs need a different remedy ----------------------
    hub = copy_install("hub", mutate="nth_web.py")
    real_hub = doc.HUB_INSTALL_DIR
    try:
        doc.HUB_INSTALL_DIR = hub
        hub_row = doc._freshness_check(hub)
        check("hub install still reports drift", hub_row is not None)
        if hub_row:
            detail = hub_row[2]
            check("hub remedy is the hub-service command",
                  "setup.sh hub-service" in detail)
            check("hub remedy does not say to restart Claude Code",
                  "restart Claude Code" not in detail)
    finally:
        doc.HUB_INSTALL_DIR = real_hub

    # --- the check is actually wired into run_checks -----------------------
    # Pinned structurally: standing up run_checks needs network, systemd and a
    # database, but a check nothing calls is worth nothing, and that exact
    # regression survived an earlier version of this file.
    source = DOCTOR.read_text()
    check("run_checks calls the freshness check",
          "_freshness_check(" in source.split("def run_checks")[1])
    check("failures in the freshness check cannot abort the run",
          "could not compare install" in source)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
