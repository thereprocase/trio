"""Offline relay delivery invariants; no credentials, model calls or live hub."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from nth_codex_relay import Spool, UncertainDelivery, run, select_messages
from nth_codex_socket import CodexSocketClient

BINDING = dict(endpoint='unix:///tmp/test-codex.sock', thread_id='test-thread',
               url='http://localhost:8000/sse', channel='test', member_id='receiver',
               session_token='test-capability', filter='at')


class FakeCodex:
    def __init__(self, *args):
        self.calls = []
    def start(self): pass
    def stop(self): pass
    def request(self, method, params):
        self.calls.append((method, params))
        return {'turn': {'id': 'same-active-turn'}, 'thread': {'id': 'test-thread'}}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'events.sqlite'
        self.spool = Spool(self.path, BINDING)
        self.client = FakeCodex()
    def tearDown(self):
        if self.spool:
            self.spool.close()
        self.temp.cleanup()
    def stage(self):
        self.spool.stage([{'id': 2, 'content': 'second'}, {'id': 1, 'content': 'first'}], 'test')
    def test_order_identity_and_typed_payload(self):
        self.stage()
        receipts = self.spool.deliver(self.client, 'test-thread')
        self.assertEqual([x['message_id'] for x in receipts], [1, 2])
        for method, params in self.client.calls:
            self.assertEqual(method, 'turn/start')
            self.assertEqual(params['threadId'], 'test-thread')
            self.assertEqual(params['input'], [])
            self.assertEqual(params['toolOutput']['name'], 'quartet_event')
            self.assertNotIn('test-capability', json.dumps(params))
    def test_filters_preserve_bangs_without_stale_batch_wakes(self):
        poll = {'has_mentions': True, 'messages': [
            {'id': 1, 'content': 'ambient'},
            {'id': 2, 'mentioned': True},
            {'id': 3, 'referenced': True},
            {'id': 4, 'content': '@other !me', 'banged': True}]}
        self.assertEqual([m['id'] for m in select_messages(poll, 'at')], [2, 4])
        self.assertEqual([m['id'] for m in select_messages(poll, 'about')], [2, 3, 4])
        self.assertEqual([m['id'] for m in select_messages(poll, 'all')], [1, 2, 3, 4])
    def test_restart_deduplicates(self):
        self.stage()
        self.spool.deliver(self.client, 'test-thread')
        self.spool.close()
        self.spool = Spool(self.path, BINDING)
        self.stage()
        self.assertEqual(self.spool.deliver(self.client, 'test-thread'), [])
        self.assertEqual(len(self.client.calls), 2)
    def test_timeout_preserves_ambiguous_send_and_blocks_replay(self):
        self.stage()
        with patch.object(self.client, 'request', side_effect=TimeoutError):
            with self.assertRaises(UncertainDelivery):
                self.spool.deliver(self.client, 'test-thread')
        self.spool.close()
        self.spool = Spool(self.path, BINDING)
        with self.assertRaises(UncertainDelivery):
            self.spool.deliver(self.client, 'test-thread')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.spool.db.execute('SELECT state FROM events ORDER BY message_id').fetchall(), [('sending',), ('pending',)])
    def test_wrong_binding_fails(self):
        self.spool.close()
        self.spool = None
        with self.assertRaises(ValueError):
            Spool(self.path, dict(BINDING, thread_id='wrong-thread'))
    def test_one_owner(self):
        with self.assertRaises(RuntimeError):
            Spool(self.path, BINDING)
    def test_observer_never_answers_approval(self):
        client = CodexSocketClient(BINDING['endpoint'])
        try:
            with patch.object(client, '_send') as send:
                client._handle_server_request({'id': 3, 'method': 'item/commandExecution/requestApproval'})
                send.assert_not_called()
        finally:
            client.stop()
    def test_revocation_blocks_pending_delivery(self):
        self.stage()
        self.spool.close()
        self.spool = None
        class RevokedQuartet:
            def __init__(self, *args): pass
            def connect(self): pass
            def close(self): pass
            def call_tool(inner, method, params, **kwargs):
                self.assertEqual(method, 'quartet_poll')
                self.assertFalse(params['auto_ack'])
                return {'error': 'Invalid or revoked session_token.'}
        with patch('nth_codex_relay.CodexSocketClient', return_value=self.client), patch('nth_codex_relay.MCPSSEClient', RevokedQuartet):
            with self.assertRaises(RuntimeError):
                run(BINDING, self.path, once=True)
        self.assertFalse(any(method == 'turn/start' for method, _ in self.client.calls))


if __name__ == '__main__':
    unittest.main()
