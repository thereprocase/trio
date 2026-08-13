"""Tests for local speech-to-text (Idea 1) and the LOTC review hardening.

Three tiers, so the file is safe on CI / non-Mac boxes:

  1. Pure helpers (nth_web) — always run.
  2. SttWorker manager error/respawn branches driven by STUB worker scripts over
     the real stdin/stdout protocol — always run (no model, no mic, deterministic).
  3. Real transcription + RMS silence gate (nth_stt_worker + mlx_whisper) — SKIP
     when mlx_whisper or macOS `say` is unavailable.

Drives the REAL modules, not duplicated logic.

Usage: python tests/test-stt.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_web as web          # noqa: E402
import nth_stt_worker as wk    # noqa: E402

failures = []
skips = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def skip(name, why):
    print(f"SKIP: {name} ({why})")
    skips.append(name)


def raises_runtime(fn, needle=None):
    try:
        fn()
        return False
    except RuntimeError as e:
        return (needle is None) or (needle.lower() in str(e).lower())
    except Exception:
        return False


# ── 1. Pure helpers ──────────────────────────────────────────────────────────
check("ext: webm", web._stt_ext_for("audio/webm") == ".webm")
check("ext: webm+codecs", web._stt_ext_for("audio/webm;codecs=opus") == ".webm")
check("ext: mixed-case + whitespace", web._stt_ext_for("  AUDIO/WEBM ; codecs=OPUS ") == ".webm")
check("ext: wav", web._stt_ext_for("audio/wav") == ".wav")
check("ext: aiff", web._stt_ext_for("audio/x-aiff") == ".aiff")
check("ext: unknown -> webm", web._stt_ext_for("application/octet-stream") == ".webm")
check("ext: empty -> webm", web._stt_ext_for("") == ".webm")

# _stt_model_cached against a fabricated HF cache (validates real True/False logic).
_cachedir = tempfile.mkdtemp(prefix="nth_hf_")
_snap = Path(_cachedir) / "hub" / "models--foo--bar" / "snapshots" / "abc"
_snap.mkdir(parents=True)
(_snap / "weights.bin").write_text("x")
_env_saved = {k: os.environ.get(k) for k in ("HF_HOME", "HUGGINGFACE_HUB_CACHE")}
os.environ.pop("HUGGINGFACE_HUB_CACHE", None)
os.environ["HF_HOME"] = _cachedir
try:
    check("model_cached True when weights present", web._stt_model_cached("foo/bar") is True)
    check("model_cached False when absent", web._stt_model_cached("no/thing") is False)
finally:
    for k, v in _env_saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_cachedir, ignore_errors=True)

h = web.STT.health()
check("health has required keys", {"available", "engine", "model", "warm", "detail"} <= set(h))
check("health engine is mlx_whisper", h["engine"] == "mlx_whisper")

# The sidecar is a separate file that the installer has to copy. If it is
# missing, say so plainly instead of failing later with a path-bearing OSError.
_saved_worker = web.STT_WORKER
web.STT_WORKER = Path(tempfile.gettempdir()) / "definitely_not_here_nth_stt_worker.py"
try:
    _hm = web.SttWorker("stub-model", "en").health()
    check("health: missing sidecar reports unavailable", _hm.get("available") is False)
    check("health: missing sidecar says 'not installed'",
          "not installed" in _hm.get("detail", ""))
    check("spawn: missing sidecar -> clean RuntimeError, no path echoed",
          raises_runtime(lambda: web.SttWorker("stub-model", "en").transcribe("/x"),
                         "not installed"))
finally:
    web.STT_WORKER = _saved_worker

# setup.sh must actually ship the sidecar, or dictation installs half-built.
_setup = (Path(__file__).resolve().parent.parent / "setup.sh").read_text()
check("setup.sh installs nth_stt_worker.py", "nth_stt_worker.py" in _setup)


# ── 2. SttWorker manager branches via stub workers (no model needed) ──────────
STUBS = {
    "echo": (
        "import sys,json\n"
        "sys.stdout.write(json.dumps({'ready':True,'model':'stub'})+chr(10));sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(json.dumps({'ok':True,'text':'stub-heard','seconds':0.0,'no_speech':False})+chr(10));sys.stdout.flush()\n"
    ),
    "notready": (
        "import sys,json\n"
        "sys.stdout.write(json.dumps({'ready':False,'error':'stub load fail'})+chr(10));sys.stdout.flush()\n"
    ),
    "badready": "import sys\nsys.stdout.write('not json'+chr(10));sys.stdout.flush()\n",
    "exitstartup": "import sys\nsys.exit(0)\n",
    "garbage": (
        "import sys,json\n"
        "sys.stdout.write(json.dumps({'ready':True})+chr(10));sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('garbage'+chr(10));sys.stdout.flush()\n"
    ),
    "notok": (
        "import sys,json\n"
        "sys.stdout.write(json.dumps({'ready':True})+chr(10));sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(json.dumps({'ok':False,'error':'stub error'})+chr(10));sys.stdout.flush()\n"
    ),
    "hang": (
        "import sys,json,time\n"
        "sys.stdout.write(json.dumps({'ready':True})+chr(10));sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    time.sleep(30)\n"
    ),
}
_stubdir = tempfile.mkdtemp(prefix="nth_stub_")
_stubpaths = {}
for name, body in STUBS.items():
    p = Path(_stubdir) / f"stub_{name}.py"
    p.write_text(body)
    _stubpaths[name] = p

_saved = (web.STT_WORKER, web.STT_WORKER_START_TIMEOUT, web.STT_TRANSCRIBE_TIMEOUT)
web.STT_WORKER_START_TIMEOUT = 10
web.STT_TRANSCRIBE_TIMEOUT = 3
try:
    def mkworker(stub):
        web.STT_WORKER = _stubpaths[stub]
        return web.SttWorker("stub-model", "en")

    # startup-failure branches
    w = mkworker("notready")
    check("startup: not-ready -> RuntimeError(load fail)",
          raises_runtime(lambda: w.transcribe("/x"), "stub load fail")); w._reset()

    w = mkworker("badready")
    check("startup: malformed ready line -> RuntimeError",
          raises_runtime(lambda: w.transcribe("/x"), "malformed startup")); w._reset()

    w = mkworker("exitstartup")
    check("startup: worker exits during startup -> RuntimeError",
          raises_runtime(lambda: w.transcribe("/x"), "exited during startup")); w._reset()

    # per-request error branches
    w = mkworker("notok")
    check("request: ok:false -> RuntimeError(stub error)",
          raises_runtime(lambda: w.transcribe("/x"), "stub error")); w._reset()

    # An engine failure must be distinguishable from a protocol failure: only
    # the former carries verbatim ffmpeg/mlx text that must not reach a client.
    w = mkworker("notok")
    try:
        w.transcribe("/x")
        check("engine failure raises SttEngineError", False)
    except web.SttEngineError:
        check("engine failure raises SttEngineError", True)
    except RuntimeError:
        check("engine failure raises SttEngineError", False)
    w._reset()

    w = mkworker("hang")
    try:
        w.transcribe("/x")
        check("protocol failure is NOT an SttEngineError", False)
    except web.SttEngineError:
        check("protocol failure is NOT an SttEngineError", False)
    except RuntimeError:
        check("protocol failure is NOT an SttEngineError", True)
    w._reset()

    w = mkworker("garbage")
    check("request: malformed response -> RuntimeError",
          raises_runtime(lambda: w.transcribe("/x"), "malformed response"))
    check("request: malformed response RESETS worker (LOTC fix)", not w._alive()); w._reset()

    w = mkworker("hang")
    t0 = time.time()
    check("request: hang -> RuntimeError(timed out)",
          raises_runtime(lambda: w.transcribe("/x"), "timed out"))
    check("request: timeout respected (< 3x window)", time.time() - t0 < 9)
    check("request: timeout RESETS hung worker", not w._alive()); w._reset()

    # happy path + transparent respawn after crash
    w = mkworker("echo")
    r = w.transcribe("/x")
    check("echo: returns text", r.get("text") == "stub-heard")
    check("echo: worker alive after success", w._alive())
    check("echo: health warm when worker alive",
          w.health().get("available") is True and w.health().get("warm") is True)
    w._proc.kill(); w._proc.wait(timeout=5)   # simulate crash between requests
    r2 = w.transcribe("/x")
    check("respawn: transcribe works again after worker death", r2.get("text") == "stub-heard")
    w._reset()
finally:
    web.STT_WORKER, web.STT_WORKER_START_TIMEOUT, web.STT_TRANSCRIBE_TIMEOUT = _saved
    shutil.rmtree(_stubdir, ignore_errors=True)


# ── 2b. The HTTP boundary must not echo engine text ──────────────────────────
# mlx_whisper relays ffmpeg's stderr verbatim — kilobytes of it, carrying
# absolute local paths including this request's own temp file. That belongs in
# the server log and never in a response body. Regression test for a real leak
# caught by POSTing two bytes of garbage to a running server.
class _FakeRfile:
    def __init__(self, data):
        self._d = data

    def read(self, n):
        return self._d[:n]


def _drive_transcribe(exc):
    """Run _handle_transcribe with STT.transcribe raising `exc`; return what it
    would have sent as {'body': ..., 'status': ...}."""
    h = web.NthWebHandler.__new__(web.NthWebHandler)   # bypass socket setup
    sent = {}
    h._json = lambda obj, status=200, **kw: sent.update(body=obj, status=status)
    h._error = lambda status, msg: sent.update(body={"error": msg}, status=status)
    ident = types.SimpleNamespace(source=web.IDENTITY_SOURCE_TAILSCALE,
                                  member_id="_op_test", display_name="tester")
    h._resolve_identity = lambda: ("tok", ident, False)
    h.headers = {"Content-Length": "4", "Content-Type": "audio/webm"}
    h.rfile = _FakeRfile(b"abcd")
    saved = web.STT.transcribe
    web.STT.transcribe = lambda _p: (_ for _ in ()).throw(exc)
    try:
        h._handle_transcribe()
    finally:
        web.STT.transcribe = saved
    return sent


LEAKY = ("Failed to load audio: ffmpeg version 7.1.1 "
         "--prefix=/opt/homebrew/Cellar/ffmpeg/7.1.1_3 "
         "Error opening input file /var/folders/ss/b1lq0000gn/T/nth_stt_aks2.webm.")
_r = _drive_transcribe(web.SttEngineError(LEAKY))
_body = json.dumps(_r.get("body"))
check("engine error: reports ok:false", _r.get("body", {}).get("ok") is False)
check("engine error: HTTP 200 so the client can fall back", _r.get("status") == 200)
check("engine error: temp path not leaked", "/var/folders" not in _body)
check("engine error: install path not leaked", "/opt/homebrew" not in _body)
check("engine error: verbatim engine text not leaked", "ffmpeg" not in _body.lower())

# Protocol errors are short and path-free, and the client's fallback banner
# reads them to explain itself — those must survive intact.
_r2 = _drive_transcribe(RuntimeError("transcription timed out"))
check("protocol error text preserved for the client",
      "timed out" in json.dumps(_r2.get("body")))

# Oversized bodies are refused before any engine work.
_h = web.NthWebHandler.__new__(web.NthWebHandler)
_sent = {}
_h._json = lambda obj, status=200, **kw: _sent.update(body=obj, status=status)
_h._error = lambda status, msg: _sent.update(body={"error": msg}, status=status)
_h._resolve_identity = lambda: ("tok", types.SimpleNamespace(
    source=web.IDENTITY_SOURCE_TAILSCALE, member_id="_op_t", display_name="t"), False)
_h.headers = {"Content-Length": str(web.MAX_STT_BYTES + 1), "Content-Type": "audio/webm"}
_h.rfile = _FakeRfile(b"")
_h._handle_transcribe()
check("oversized audio rejected with 400", _sent.get("status") == 400)


# ── 3. Real transcription + RMS gate (skips if deps missing) ─────────────────
def mlx_available():
    try:
        r = subprocess.run([sys.executable, "-c", "import mlx_whisper"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


if not mlx_available():
    skip("real transcription", "mlx_whisper not importable")
else:
    tmpdir = tempfile.mkdtemp(prefix="nth_stt_real_")
    try:
        # _load_and_rms: silence ~0, corrupt file -> (None, None) fail-open.
        silent = str(Path(tmpdir) / "silence.wav")
        with wave.open(silent, "wb") as w2:
            w2.setnchannels(1); w2.setsampwidth(2); w2.setframerate(16000)
            w2.writeframes(b"\x00\x00" * 32000)
        _s, rms_silence = wk._load_and_rms(silent)
        check("rms: silence near zero", rms_silence is not None and rms_silence < 0.005)
        bad = str(Path(tmpdir) / "not-audio.wav")
        Path(bad).write_text("this is not audio")
        _b, rms_bad = wk._load_and_rms(bad)
        check("rms: unreadable -> None (fail-open)", rms_bad is None)

        # Worker-manager RMS gate: silence flagged, speech transcribed.
        wworker = web.SttWorker(web.STT_MODEL, "en")
        try:
            rs = wworker.transcribe(silent)
            check("silence flagged no_speech (Whisper skipped)", rs.get("no_speech") is True)
            check("silence yields no text", not (rs.get("text") or "").strip())
            if shutil.which("say"):
                speech = str(Path(tmpdir) / "speech.aiff")
                subprocess.run(["say", "-o", speech,
                                "The quick brown fox jumps over the lazy dog."], check=True)
                rp = wworker.transcribe(speech)
                txt = (rp.get("text") or "").lower()
                check("speech transcribes the phrase",
                      "quick brown fox" in txt and "lazy dog" in txt)
                check("speech not flagged no_speech", rp.get("no_speech") is False)
            else:
                skip("real speech round-trip", "macOS `say` not available")
        finally:
            wworker._reset()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), {len(skips)} skip(s)")
sys.exit(1 if failures else 0)
