#!/usr/bin/env python3
"""Persistent local speech-to-text worker for nth_web.py.

Loads an mlx-whisper model ONCE and keeps it warm, so each transcription costs
only inference (~0.8s) instead of a ~3s cold model load per subprocess. Speaks a
line-delimited JSON protocol over stdin/stdout:

  stdin   {"audio": "/path/to/file", "language": "en"}\\n
  stdout  {"ok": true, "text": "...", "seconds": 0.79}\\n
      or  {"ok": false, "error": "..."}\\n

On startup it prints exactly one line once the model is resident:
  {"ready": true, "model": "..."}      model loaded, requests may follow
  {"ready": false, "error": "..."}     import/load failed — parent should give up

The worker exits on stdin EOF, so when the parent web server dies the pipe
closes and this process cleans itself up (no orphan).

NOT stdlib-only: this imports mlx_whisper. It is an OPTIONAL sidecar spawned by
nth_web.py only when local transcription is used; the core web server stays
dependency-free and merely pipes audio paths to this process.
"""
import json
import os
import sys
import tempfile
import time
import wave

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
# Whisper hallucinates words from silence/near-silence, and its own
# no_speech_prob is unreliable (mlx returns ~0 even for pure silence), so we
# gate on actual audio energy (RMS) and skip transcription below this floor.
#
# The floor was originally 0.02, calibrated against a reference of speech≈0.156.
# That reference came from macOS `say` output — synthetic, loud, close-miked and
# normalised. Real microphone input at conversational distance measured 0.015 to
# 0.034 on the first machine this met, i.e. the OLD THRESHOLD SAT INSIDE THE
# RANGE OF NORMAL SPEECH: the same person saying the same sentence would be
# transcribed or rejected as silence depending on how loudly they happened to
# say it. The unit tests never caught it because they generate their audio with
# `say` too, reproducing the very bias that set the number.
#
# 0.005 clears digital silence (0.000) and sits well under quiet real speech,
# leaving Whisper's own empty-transcript result as the backstop for room noise.
# Raise it via the env var on a noisy machine that hallucinates from ambience.
RMS_SILENCE_THRESHOLD = float(os.environ.get("NTH_STT_SILENCE_RMS", "0.005"))


def _load_and_rms(path):
    """Decode the audio ONCE and return (samples, rms). `samples` is the decoded
    float32 array (reused for transcription so ffmpeg isn't run twice); rms is the
    0..1 amplitude used for the silence gate. Returns (None, None) if it can't be
    measured — in which case we do NOT gate and fall back to path-based decode."""
    try:
        import numpy as np
        from mlx_whisper.audio import load_audio
        a = np.asarray(load_audio(path), dtype=np.float32)
        if a.size == 0:
            return a, 0.0
        rms = float(np.sqrt(np.mean(np.square(a.astype(np.float64)))))
        return a, rms
    except Exception:  # noqa: BLE001
        return None, None


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _silent_wav() -> str:
    """A 0.1s silent 16kHz mono WAV used to force the model to load before we
    report ready — so 'ready' means genuinely warm, not merely importable."""
    fd, path = tempfile.mkstemp(prefix="nth_stt_warm_", suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return path


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL

    try:
        import mlx_whisper
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"mlx_whisper import failed: {e}"})
        return 1

    # Warm the model (downloads on first ever use, then cached on disk).
    warm = None
    try:
        warm = _silent_wav()
        mlx_whisper.transcribe(warm, path_or_hf_repo=model)
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"model load failed: {e}"})
        return 1
    finally:
        if warm:
            try:
                os.unlink(warm)
            except OSError:
                pass

    _emit({"ready": True, "model": model})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _emit({"ok": False, "error": "bad request json"})
            continue
        audio = req.get("audio")
        lang = req.get("language") or None
        if not audio:
            _emit({"ok": False, "error": "no audio path"})
            continue
        t0 = time.time()
        try:
            samples, rms = _load_and_rms(audio)
            if rms is not None and rms < RMS_SILENCE_THRESHOLD:
                # Silence / quiet noise — skip Whisper so it can't hallucinate.
                _emit({"ok": True, "text": "", "seconds": round(time.time() - t0, 2),
                       "no_speech": True, "rms": round(rms, 5)})
                continue
            # Reuse the decoded samples when available (avoids a 2nd ffmpeg decode);
            # fall back to the path if decoding failed above.
            src = samples if samples is not None else audio
            if lang:
                result = mlx_whisper.transcribe(src, path_or_hf_repo=model, language=lang)
            else:
                result = mlx_whisper.transcribe(src, path_or_hf_repo=model)
            text = str(result.get("text") or "").strip()
            _emit({"ok": True, "text": text, "seconds": round(time.time() - t0, 2),
                   "no_speech": (not text),
                   "rms": (round(rms, 5) if rms is not None else None)})
        except Exception as e:  # noqa: BLE001
            _emit({"ok": False, "error": str(e)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
