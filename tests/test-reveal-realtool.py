#!/usr/bin/env python3
"""Real-tool smoke test for the #22 reveal argv, per platform.

WHY THIS EXISTS: tests/test-file-reveal.py mocks subprocess.run and asserts the
argv nth *builds*. It cannot fail, because what is wrong is what the OS does
with that argv. Three shipped bugs (xdg-open's "--", the split "/select,"
token, explorer's exit-1-on-success) all sat behind a passing mocked test.

This test invokes the REAL tool and checks it accepts our argument shape. It
never opens a window: the Linux check runs with the display environment
stripped, so xdg-open parses arguments and then fails to find a launch method —
which is exactly the distinction we care about (parse failure vs. launch
failure).

It SKIPS LOUDLY. A silent skip is how the STT tests hid the same class of gap.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REVEAL_TIMEOUT = 10

# Substrings that mean "the tool rejected our ARGUMENTS", as opposed to
# "the tool understood us but couldn't reach a GUI".
PARSE_FAILURE_MARKERS = (
    "unexpected option",
    "syntax error",
    "invalid option",
    "unrecognized option",
)


def _loud_skip(reason):
    sys.stderr.write(f"\n*** SKIPPED (coverage gap, not a pass): {reason}\n")
    raise unittest.SkipTest(reason)


class RevealArgvAcceptedByRealTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nth_reveal_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = os.path.join(self.tmp, "sample.txt")
        with open(self.target, "w") as fh:
            fh.write("x")

    @unittest.skipUnless(sys.platform.startswith("linux"), "linux only")
    def test_linux_xdg_open_accepts_our_argv(self):
        if shutil.which("xdg-open") is None:
            _loud_skip("xdg-open not installed — the Linux reveal path is UNVERIFIED here")
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE")}
        env["DE"] = "generic"
        env["BROWSER"] = ""
        cp = subprocess.run(["xdg-open", self.tmp], env=env,
                            capture_output=True, text=True, timeout=REVEAL_TIMEOUT)
        blob = (cp.stderr + cp.stdout).lower()
        for marker in PARSE_FAILURE_MARKERS:
            self.assertNotIn(
                marker, blob,
                f"xdg-open rejected our arguments ({marker!r}). This is the "
                f"'--' class of bug: the reveal endpoint will 502 on every "
                f"click. Full output:\n{cp.stderr}{cp.stdout}")

    @unittest.skipUnless(sys.platform.startswith("linux"), "linux only")
    def test_linux_double_dash_is_rejected_regression(self):
        """Pins the ORIGINAL defect so it cannot come back: xdg-open must be
        shown to reject '--'. If a future xdg-utils starts accepting it, this
        fails and tells us the workaround is no longer needed — an honest
        signal either way."""
        if shutil.which("xdg-open") is None:
            _loud_skip("xdg-open not installed — regression pin UNVERIFIED here")
        cp = subprocess.run(["xdg-open", "--", self.tmp],
                            capture_output=True, text=True, timeout=REVEAL_TIMEOUT)
        blob = (cp.stderr + cp.stdout).lower()
        self.assertTrue(
            any(m in blob for m in PARSE_FAILURE_MARKERS),
            "xdg-open now ACCEPTS '--'. The reveal fix's comment is stale; "
            "re-check whether the sentinel should be restored.")

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_macos_open_R_accepts_our_argv(self):
        if shutil.which("open") is None:
            _loud_skip("/usr/bin/open missing — the macOS reveal path is UNVERIFIED here")
        # -R on a real path is the shape we ship. This DOES bring Finder
        # forward; acceptable in a test run, and there is no dry-run mode.
        cp = subprocess.run(["open", "-R", "--", self.target],
                            capture_output=True, text=True, timeout=REVEAL_TIMEOUT)
        self.assertEqual(cp.returncode, 0,
                         f"open -R rejected our argv: {cp.stderr}")

    @unittest.skipUnless(sys.platform.startswith("win"), "windows only")
    def test_windows_select_token_is_single_argv(self):
        """explorer.exe returns nonzero even on success, so its exit code
        proves nothing. What we CAN assert without a GUI oracle is the shape
        of the argument we build: '/select,<path>' must be one token."""
        argv = ["explorer", f"/select,{self.target}"]
        self.assertEqual(len(argv), 2,
                         "'/select,' and the path must be a single argv token; "
                         "splitting them makes explorer ignore the selector.")
        self.assertTrue(argv[1].startswith("/select,"))
        self.assertNotIn(", ", argv[1],
                         "a space after the comma makes explorer open Documents")


if __name__ == "__main__":
    unittest.main(verbosity=2)
