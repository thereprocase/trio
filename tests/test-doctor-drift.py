"""Tests for nth_doctor's stale-install detection.

setup.sh COPIES the repo into the install directory, so pulling new code
changes nothing that runs until it is re-run. The failure mode is silent: a
feature is present in the checkout and simply absent at runtime.

The interesting cases here are the ones that must stay QUIET. A freshness
check that cries wolf on a correct symlink deploy, or on doctor running from
the install it is describing, is worse than no check at all — people learn to
ignore it, and it is the same warning they would need to trust on the day it
is real.

Usage: python tests/test-doctor-drift.py
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

DOCTOR = Path(__file__).resolve().parent.parent / "server" / "nth_doctor.py"
spec = importlib.util.spec_from_file_location("nth_doctor", DOCTOR)
doc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc)

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="nth_doctor_"))
_real_installed = doc._installed_version


def install_at(version, name):
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "nth_constants.py").write_text(f'NTH_VERSION = "{version}"\n')
    return d


try:
    running, run_dir = doc._running_version()
    check("running version parses from the checkout", bool(running))
    check("running dir is where nth_doctor lives", run_dir == DOCTOR.parent)

    # Stale install — the case the check exists for.
    doc._installed_version = lambda: ("0.0.1", install_at("0.0.1", "old"))
    drift = doc._install_drift()
    check("older install is reported", drift is not None)
    if drift:
        check("reports both versions", drift[0] == running and drift[1] == "0.0.1")

    # Everything below must stay silent.
    doc._installed_version = lambda: (running, install_at(running, "same"))
    check("matching version is silent", doc._install_drift() is None)

    doc._installed_version = lambda: (running, run_dir)
    check("doctor running from the install it describes is silent",
          doc._install_drift() is None)

    link = tmp / "linked"
    if not link.exists():
        link.symlink_to(run_dir)
    # A link.sh deploy: the install path IS the checkout, so a version string
    # read through it can never be stale no matter what it says.
    doc._installed_version = lambda: ("0.0.1", link)
    check("symlinked install is silent", doc._install_drift() is None)

    doc._installed_version = lambda: (None, tmp)
    check("unversioned install is silent (its own check covers that)",
          doc._install_drift() is None)

    doc._installed_version = lambda: ("0.0.1", None)
    check("no install directory is silent", doc._install_drift() is None)
finally:
    doc._installed_version = _real_installed
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
