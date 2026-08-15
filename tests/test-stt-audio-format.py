#!/usr/bin/env python3
"""Does the whisper.cpp worker hand its engine audio the engine can actually read?

whisper.cpp decodes 16 kHz mono WAV and nothing else -- it links dr_wav, it has
no demuxer for WebM, Opus, MP4 or MP3. The browser records WebM/Opus
(`new Blob(mediaChunks, {type: rec.mimeType || 'audio/webm'})`), and nth_web.py
writes that body to a temp file whose extension comes from the request's
Content-Type, defaulting to `.webm`.

So the audio arriving at the worker is WebM, and something has to resample it to
16 kHz mono WAV before the engine sees it. If nothing does, the engine fails on
every real clip while the health check still reports the engine as ready -- a
green light in front of a broken tool, which is the failure this release exists
to eliminate.

WHY THIS TEST WORKS WITHOUT WHISPER.CPP INSTALLED: it substitutes a stub binary
that answers `--help` the way whisper.cpp does and, when asked to transcribe,
records the file it was handed. We then inspect that file. This tests OUR half
of the contract -- what we hand the engine -- which is the half we control and
the half that was wrong. It needs no model, no GPU and no network.

Run:  python3 tests/test-stt-audio-format.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")

failures = []
skips = []


def check(name, ok):
    print(("PASS: " if ok else "FAIL: ") + name)
    if not ok:
        failures.append(name)


def skip(name, why):
    print(f"SKIP: {name} — {why}")
    skips.append(name)


# A stand-in for whisper-cli. `--help` mimics whisper.cpp closely enough for a
# run-based probe to accept it; a transcribe call records the -f argument and
# writes the -of output file the worker expects.
STUB = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if "--help" in args or "-h" in args:
    sys.stdout.write(
        "usage: whisper-cli [options] file0.wav file1.wav ...\n"
        "  -m FNAME, --model FNAME   model path\n"
        "  -f FNAME, --file FNAME    input WAV file path\n")
    sys.exit(0)
audio = None
out = None
for i, a in enumerate(args):
    if a in ("-f", "--file") and i + 1 < len(args):
        audio = args[i + 1]
    if a in ("-of", "--output-file") and i + 1 < len(args):
        out = args[i + 1]
# Record the file's CONTENT signature now, not just its path: a worker that
# converts correctly writes a temp WAV and deletes it as soon as the engine
# returns, so by the time the test looks, a correct implementation has
# (rightly) cleaned up. Inspecting at call time is the only honest moment.
info = {"path": audio or "", "magic": "", "channels": None, "rate": None}
if audio and os.path.exists(audio):
    with open(audio, "rb") as fh:
        head = fh.read(12)
    info["magic"] = head.hex()
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        import wave
        try:
            with wave.open(audio, "rb") as w:
                info["channels"], info["rate"] = w.getnchannels(), w.getframerate()
        except Exception:
            pass
import json as _json
with open(os.environ["STUB_RECORD"], "w") as fh:
    fh.write(_json.dumps(info))
if out:
    with open(out + ".txt", "w") as fh:
        fh.write("stub transcript\n")
sys.stdout.write("stub transcript\n")
sys.exit(0)
"""


def find_worker():
    """Whichever whisper.cpp-backed worker this tree ships. Both names are in
    play across the 8.1.1 branches; test the one that exists rather than
    hard-coding a branch's choice."""
    for name in ("nth_stt_worker_cli.py", "nth_whisper_cpp_worker.py"):
        p = os.path.join(SERVER, name)
        if os.path.isfile(p):
            return p
    return None


def main():
    worker = find_worker()
    if worker is None:
        skip("stt audio format", "no whisper.cpp-backed worker in this tree")
        return
    if shutil.which("ffmpeg") is None:
        skip("stt audio format", "ffmpeg absent — cannot synthesise browser audio")
        return

    tmp = tempfile.mkdtemp(prefix="nth_stt_fmt_")
    try:
        # Exactly what the browser sends: WebM/Opus.
        webm = os.path.join(tmp, "clip.webm")
        rc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=1",
             "-c:a", "libopus", "-f", "webm", webm],
            capture_output=True).returncode
        if rc != 0 or not os.path.exists(webm):
            skip("stt audio format", "ffmpeg could not produce a WebM/Opus clip")
            return
        with open(webm, "rb") as fh:
            check("fixture really is WebM (EBML magic)", fh.read(4) == b"\x1aE\xdf\xa3")

        stub = os.path.join(tmp, "whisper-cli")
        with open(stub, "w") as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)
        record = os.path.join(tmp, "record.txt")
        model = os.path.join(tmp, "ggml-base.en.bin")
        with open(model, "wb") as fh:
            fh.write(b"\0" * 64)          # existence is all any worker checks

        env = dict(os.environ,
                   STUB_RECORD=record,
                   NTH_STT_CLI_BIN=stub, NTH_STT_CLI_MODEL=model,
                   NTH_WHISPER_CPP_BIN=stub, NTH_WHISPER_CPP_MODEL=model)
        proc = subprocess.Popen(
            [sys.executable, worker, stub, model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env)
        try:
            ready = json.loads(proc.stdout.readline() or "{}")
            if not ready.get("ready"):
                skip("stt audio format",
                     f"worker did not start against the stub: {ready.get('error')!r}")
                return
            proc.stdin.write(json.dumps({"audio": webm, "language": "en"}) + "\n")
            proc.stdin.flush()
            json.loads(proc.stdout.readline() or "{}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not os.path.exists(record):
            check("engine was invoked with an audio file", False)
            return
        with open(record) as fh:
            info = json.loads(fh.read() or "{}")
        check("engine was invoked with an audio file", bool(info.get("path")))
        if not info.get("path"):
            return

        # THE ASSERTION. whisper.cpp reads WAV only; anything else fails at the
        # first real clip while health() still says the engine is ready.
        magic = bytes.fromhex(info.get("magic") or "")
        is_wav = magic[:4] == b"RIFF" and magic[8:12] == b"WAVE"
        check("engine is handed a RIFF/WAVE file, not the browser's WebM "
              "(whisper.cpp cannot decode WebM/Opus)", is_wav)
        if not is_wav:
            print(f"      handed: {info['path']}\n      magic : {magic[:4]!r} "
                  f"(WebM is b'\\x1aE\\xdf\\xa3')")
            return
        ch, rate = info.get("channels"), info.get("rate")
        check(f"engine is handed MONO audio (got {ch}ch)", ch == 1)
        check(f"engine is handed 16 kHz audio (got {rate} Hz)", rate == 16000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


main()
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s), "
      f"{len(skips)} skip(s)")
sys.exit(1 if failures else 0)
