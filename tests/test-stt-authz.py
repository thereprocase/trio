"""Transcription uses the upload identity gate before consuming audio or a slot."""
import io
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

    def test_health_keeps_existing_response_and_guest_access(self):
        health = {'available': True, 'detail': 'ready', 'warm': True, 'model': 'synthetic', 'engine': 'remote'}
        for source in (*web.UPLOAD_ALLOWED_SOURCES, web.IDENTITY_SOURCE_GUEST):
            with self.subTest(source=source):
                h = web.NthWebHandler.__new__(web.NthWebHandler)
                h.path = '/api/stt/health'
                h.landing_mode = False
                h._resolve_identity = lambda: ('synthetic-token', types.SimpleNamespace(source=source), False)
                h._json, h._error = Mock(), Mock()
                with patch.object(web, 'stt_health', return_value=health), patch.object(web, 'SECURE_URL_HINT', ''):
                    h.do_GET()
                h._error.assert_not_called()
                h._json.assert_called_once_with({**health, 'secure_url': ''})

    def test_health_keeps_existing_pending_rejection(self):
        h = web.NthWebHandler.__new__(web.NthWebHandler)
        h.path, h.landing_mode = '/api/stt/health', False
        h._resolve_identity = lambda: ('synthetic-token', types.SimpleNamespace(source=web.IDENTITY_SOURCE_PENDING), False)
        h._error = Mock()
        with patch.object(web, 'stt_health') as health:
            h.do_GET()
        h._error.assert_called_once_with(403, 'pick a name to join this channel first')
        health.assert_not_called()


if __name__ == '__main__':
    unittest.main()
