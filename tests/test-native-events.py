"""Native local/remote delivery, identity, registry, and installer invariants."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import nth_event_service as service
from nth_event_access import native_connect_response
from nth_codex_relay import Spool
from nth_codex_runtime import CodexRuntimeManager


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'NTH_HOME': str(self.root), 'NTH_QUIET': '1'})
        self.env.start()
        self.binding = dict(endpoint='unix:///tmp/native-test.sock', thread_id='thread-1',
            source='quartet', url='http://localhost:8000/sse', channel='room',
            member_id='member-1', session_token='private-token', filter='about')
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
    def test_registry_idempotence_and_capability_scope(self):
        key = service.register(self.binding)
        self.assertEqual(service.register(self.binding), key)
        self.assertEqual(service.bindings()[0]['revision'], 1)
        self.assertEqual(service.public_status('room', 'member-1', 'wrong'), [])
        self.assertNotIn('private-token', json.dumps(service.public_status()))
        service.configure_listener('room', 'member-1', 'wrong', enabled=False)
        self.assertTrue(service.bindings()[0]['enabled'])
        service.configure_listener('room', 'member-1', 'private-token', filter_mode='at', enabled=False)
        row = service.bindings()[0]
        self.assertEqual(row['revision'], 2)
        self.assertFalse(row['enabled'])
        self.assertEqual(json.loads(row['config'])['filter'], 'at')
        service.register(self.binding)
        self.assertFalse(service.bindings()[0]['enabled'])
        self.assertEqual(json.loads(service.bindings()[0]['config'])['filter'], 'at')
    def test_filter_change_preserves_receipts(self):
        path = self.root / 'spool.sqlite'
        spool = Spool(path, self.binding)
        spool.stage([{'id': 1, 'mentioned': True}], 'room')
        class Client:
            def request(self, *args): return {'turn': {'id': 't'}}
        spool.deliver(Client(), 'thread-1')
        spool.close()
        spool = Spool(path, dict(self.binding, filter='at'))
        self.assertEqual(spool.deliver(Client(), 'thread-1'), [])
        spool.close()
    def test_observer_only_accepts_real_successful_connects(self):
        response = {'channel': 'room', 'member_id': 'member-1', 'session_token': 'private-token'}
        item = {'type': 'mcpToolCall', 'server': 'nth-qweb', 'tool': 'quartet_connect',
                'status': 'completed', 'result': {'content': [{'type': 'text', 'text': json.dumps(response)}]}}
        notification = {'method': 'item/completed', 'params': {'threadId': 'thread-1', 'item': item}}
        self.assertEqual(service.connected_identity(notification), ('quartet', 'thread-1', response))
        item['type'] = 'agentMessage'
        self.assertIsNone(service.connected_identity(notification))
        item['type'] = 'mcpToolCall'; item['server'] = 'peer-supplied'
        self.assertIsNone(service.connected_identity(notification))
        item['server'] = 'nth-qweb'; item['status'] = 'failed'
        self.assertIsNone(service.connected_identity(notification))
    def test_observer_uses_local_source_configuration(self):
        observer = service.Observer('unix:///tmp/native-test.sock', self.binding['url'], threading.Event())
        body = {'channel': 'room', 'member_id': 'member-1', 'session_token': 'private-token',
                'endpoint': 'ws://attacker:9000', 'url': 'http://attacker'}
        observer.notification({'method': 'item/completed', 'params': {'threadId': 'thread-1', 'item': {
            'type': 'mcpToolCall', 'server': 'nth-qweb', 'tool': 'quartet_connect', 'status': 'completed',
            'result': {'content': [{'type': 'text', 'text': json.dumps(body)}]}}}})
        stored = json.loads(service.bindings()[0]['config'])
        self.assertEqual(stored['url'], self.binding['url'])
        self.assertEqual(stored['endpoint'], self.binding['endpoint'])
    def test_history_recovery_uses_only_latest_membership_token(self):
        observer = service.Observer(self.binding['endpoint'], self.binding['url'], threading.Event())
        service.register(self.binding)
        service.configure_listener('room', 'member-1', self.binding['session_token'], enabled=False)
        items = []
        for token in ('old-token', self.binding['session_token']):
            body = dict(channel='room', member_id='member-1', session_token=token)
            items.append({'type': 'mcpToolCall', 'server': 'nth-qweb', 'tool': 'quartet_connect',
                'status': 'completed', 'result': {'content': [{'type': 'text', 'text': json.dumps(body)}]}})
        observer.recover_history('thread-1', [{'items': items}])
        self.assertFalse(service.bindings()[0]['enabled'])
        # A live reconnect can finish while the resume response is in flight.
        live = json.loads(json.dumps(items[-1]))
        live['result']['content'][0]['text'] = json.dumps(dict(channel='room', member_id='member-1', session_token='new-live-token'))
        observer.notification({'method': 'item/completed', 'params': {'threadId': 'thread-1', 'item': live}})
        observer.recover_history('thread-1', [{'items': items}])
        self.assertEqual(json.loads(service.bindings()[0]['config'])['session_token'], 'new-live-token')
    def test_launcher_preserves_codex_argument_order(self):
        import nth_cli
        with patch.object(nth_cli, 'ensure_codex', return_value=('unix:///tmp/test.sock', 'codex')), \
             patch.object(nth_cli.subprocess, 'call', return_value=0) as call:
            nth_cli.main(['codex', '--model', 'chosen-model', 'resume', 'thread-id'])
        self.assertEqual(call.call_args.args[0], ['codex', '--remote', 'unix:///tmp/test.sock',
                                                '--model', 'chosen-model', 'resume', 'thread-id'])
    def test_native_connect_keeps_tokens_out_of_launch_command(self):
        identity = {'channel': 'room', 'member_id': 'member-1', 'session_token': 'private-token', 'reclaim_secret': 'private-reclaim'}
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'claude'}):
            result = native_connect_response(dict(identity))
        self.assertNotIn('private-token', result['monitor_hint'])
        self.assertNotIn('private-reclaim', result['monitor_hint'])
        path = Path(result['identity_file'])
        self.assertEqual(json.loads(path.read_text())['session_token'], 'private-token')
        if os.name != 'nt': self.assertEqual(path.stat().st_mode & 0o077, 0)
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CODEX_ENDPOINT': 'unix:///tmp/native-test.sock'}):
            result = native_connect_response(dict(identity))
        self.assertEqual(result['event_delivery']['mode'], 'automatic')
        self.assertEqual(result['monitor_hint'], '')
    def test_install_preserves_unrelated_settings_and_installs_both_skills(self):
        spec = importlib.util.spec_from_file_location('native_setup', ROOT / 'setup.py')
        setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
        profile = self.root / 'profile'; profile.mkdir()
        (profile / '.claude.json').write_text(json.dumps({'custom': 7, 'mcpServers': {'other': {'command': 'keep'}}}))
        result = setup.install(profile, quartet_url='http://localhost:8000/sse',
                               skip_dependencies=True, register_codex=False)
        config = json.loads((profile / '.claude.json').read_text())
        self.assertEqual(config['custom'], 7)
        self.assertEqual(config['mcpServers']['other'], {'command': 'keep'})
        self.assertEqual(config['mcpServers']['nth-qweb']['type'], 'stdio')
        for client in ('.claude', '.codex'):
            for flavor in ('trio', 'quartet'):
                self.assertTrue((profile / client / 'skills' / flavor / 'SKILL.md').exists())
        self.assertTrue(Path(result['server'], 'nth_event_service.py').exists())
        self.assertTrue(list(profile.glob('.claude.json.bak-*')))
    def test_managed_event_steers_active_turn_and_preserves_private_routing(self):
        db_path = self.root / 'managed.sqlite'
        with contextlib.closing(sqlite3.connect(db_path)) as db:
            db.executescript("CREATE TABLE messages(id INTEGER); CREATE TABLE agents(id TEXT,effort TEXT); INSERT INTO agents VALUES ('agent','');")
        manager = object.__new__(CodexRuntimeManager)
        manager._lock = threading.RLock()
        manager._agent_lock = lambda _: contextlib.nullcontext()
        manager.is_running = lambda _: True
        manager._threads = {'agent': 'thread'}
        manager._active = {'agent': 'active-turn'}
        manager._starting = {}; manager._compacting = set(); manager._queued = {}
        manager._turn_context = {'active-turn': {'channel': 'private-first', 'baseline': 0}}
        def open_db():
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            return db
        manager._db = open_db
        manager._set_state = lambda *args, **kwargs: None
        calls = []
        class Client:
            def request(self, method, params):
                calls.append((method, params))
                return {'turn': {'id': 'active-turn'}}
        manager._client = Client()
        self.assertTrue(manager.feed('agent', 'public-other', 'new event', source_message_id=2))
        self.assertFalse(manager._queued)
        self.assertEqual(calls[0][1]['input'], [])
        self.assertEqual(calls[0][1]['toolOutput']['name'], 'trio_event')
        self.assertTrue(manager._turn_context['active-turn']['suppress_auto_bridge'])
        self.assertEqual(manager._turn_context['active-turn']['channel'], 'private-first')


if __name__ == '__main__':
    unittest.main()
