"""Transcription uses the upload identity gate before consuming audio or a slot."""
import io
import os
import subprocess
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import nth_web as web


class TranscribeIdentityTests(unittest.TestCase):
    def drive(self, source):
        h = web.NthWebHandler.__new__(web.NthWebHandler)
        h._resolve_identity = lambda: ('synthetic-token', types.SimpleNamespace(source=source), False)
        h._json = Mock()
        h._error = Mock()
        h.headers = {'Content-Length': '5', 'Content-Type': 'audio/webm'}
        h.rfile = Mock(wraps=io.BytesIO(b'audio'))
        h.connection = Mock()
        h.connection.gettimeout.return_value = None
        backend = Mock(ENGINE='remote')
        backend.transcribe.return_value = {'text': 'hello', 'seconds': 1}
        slots = Mock()
        slots.acquire.return_value = True
        with patch.object(web, 'STT_REMOTE', backend), patch.object(web, 'STT_SLOTS', slots):
            h._handle_transcribe()
        return h, backend, slots

    def test_allowed_sources_transcribe(self):
        for source in web.UPLOAD_ALLOWED_SOURCES:
            with self.subTest(source=source):
                h, backend, slots = self.drive(source)
                h._error.assert_not_called()
                self.assertTrue(h._json.call_args.args[0]['ok'])
                backend.transcribe.assert_called_once_with(b'audio', 'audio/webm')
                slots.release.assert_called_once()

    def test_unidentified_is_rejected_before_reading(self):
        self.assert_rejected(web.IDENTITY_SOURCE_PENDING)

    def test_guest_is_rejected_before_reading(self):
        self.assert_rejected(web.IDENTITY_SOURCE_GUEST)

    def assert_rejected(self, source):
        h, backend, slots = self.drive(source)
        h._error.assert_called_once()
        self.assertEqual(h._error.call_args.args[0], 403)
        h.rfile.read.assert_not_called()
        backend.transcribe.assert_not_called()
        slots.acquire.assert_not_called()

    def health(self, source):
        h = web.NthWebHandler.__new__(web.NthWebHandler)
        h.path, h.landing_mode = '/api/stt/health', False
        h._resolve_identity = lambda: ('synthetic-token', types.SimpleNamespace(source=source), False)
        h._json, h._error = Mock(), Mock()
        h.do_GET()
        h._error.assert_not_called()
        return h._json.call_args.args[0]

    def test_health_reports_available_only_for_allowed_sources(self):
        health = {'available': True, 'detail': 'ready', 'warm': True, 'model': 'synthetic',
                  'engine': 'remote', 'deadline_s': 300}
        for source in web.UPLOAD_ALLOWED_SOURCES:
            with self.subTest(source=source), patch.object(web, 'stt_health', return_value=health), \
                 patch.object(web, 'SECURE_URL_HINT', ''):
                self.assertEqual(self.health(source), {**health, 'secure_url': ''})

    def test_health_is_open_but_denied_callers_cannot_choose_hub(self):
        for source in (web.IDENTITY_SOURCE_PENDING, web.IDENTITY_SOURCE_GUEST):
            with self.subTest(source=source), patch.object(web, 'stt_health') as probe:
                self.assertEqual(self.health(source), {'available': False,
                    'detail': 'dictation on this hub is limited to its members'})
                probe.assert_not_called()

    def test_health_deadline_covers_cold_start_and_queue(self):
        backend = Mock()
        with patch.object(web, 'STT_REMOTE', None), patch.object(web, 'STT', backend), \
             patch.object(web, 'STT_WORKER_START_TIMEOUT', 180), \
             patch.object(web, 'STT_TRANSCRIBE_TIMEOUT', 120), \
             patch.object(web, 'STT_BODY_READ_TIMEOUT', 30), patch.object(web, 'STT_MAX_CONCURRENT', 2):
            backend.health.return_value = {'available': True, 'warm': False}
            cold = web.stt_health()['deadline_s']
            self.assertGreaterEqual(cold, 30 + 2 * (180 + 120))
            backend.health.return_value = {'available': True, 'warm': True}
            warm = web.stt_health()['deadline_s']
            self.assertGreaterEqual(warm, 30 + 2 * 120 + 180)
            self.assertEqual(warm, cold, "warm snapshots can become cold before upload")

    def test_operator_timeout_applies_to_local_inference(self):
        with tempfile.TemporaryDirectory(prefix='nth-stt-deadline-') as directory:
            env = dict(os.environ, NTH_HOME=directory, NTH_STT_TIMEOUT='123', NTH_STT_URL='',
                       PYTHONDONTWRITEBYTECODE='1')
            code = "import sys; sys.path.insert(0, sys.argv[1]); import nth_web; assert nth_web.STT_TRANSCRIBE_TIMEOUT == 123"
            result = subprocess.run([sys.executable, '-c', code,
                str(Path(web.__file__).parent)], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_queue_has_a_finite_wait_and_does_not_release_anothers_lock(self):
        worker = web.SttWorker('synthetic', 'en')
        lock = Mock()
        lock.acquire.return_value = False
        worker._lock = lock
        with patch.object(web, 'STT_MAX_CONCURRENT', 2), \
             patch.object(web, 'STT_WORKER_START_TIMEOUT', 180), \
             patch.object(web, 'STT_TRANSCRIBE_TIMEOUT', 120):
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                worker.transcribe('synthetic.webm')
        lock.acquire.assert_called_once_with(timeout=302)
        lock.release.assert_not_called()

    def test_remote_health_deadline_uses_configured_request_ceiling(self):
        backend = Mock(timeout=420)
        backend.health.return_value = {'available': True, 'warm': False, 'remote': True}
        with patch.object(web, 'STT_REMOTE', backend), patch.object(web, 'STT_BODY_READ_TIMEOUT', 30):
            self.assertEqual(web.stt_health()['deadline_s'], 450)


if __name__ == '__main__':
    unittest.main()
