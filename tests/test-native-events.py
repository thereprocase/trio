"""Native local/remote delivery, identity, registry, and installer invariants."""
import contextlib
import importlib.util
import json
import os
import shlex
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import nth_event_service as service
from nth_event_access import delivery_status, listen, native_connect_response
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
    def test_codex_connect_requires_verification_even_with_a_configured_endpoint(self):
        identity = dict(channel='room', member_id='member-1', session_token='private-token')
        for source in ('local', 'quartet'):
            for endpoint in ('', 'unix:///tmp/native-test.sock'):
                with self.subTest(source=source, endpoint=endpoint), patch.dict(os.environ, {
                    'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CODEX_ENDPOINT': endpoint,
                }):
                    result = native_connect_response(dict(identity), source=source,
                                                     url='http://hub.example/sse')
                    self.assertEqual(result['event_delivery']['readiness'], 'unverified')
                    self.assertIn('A successful join is not readiness', result['instructions'])
                    prefix = 'trio' if source == 'local' else 'quartet'
                    self.assertIn(prefix + '_delivery_status', result['instructions'])
                    self.assertEqual(result['monitor_hint'], '')
    def test_codex_missing_listener_is_incomplete_setup_not_background_availability(self):
        result = delivery_status('room', 'member-1', 'private-token')
        self.assertEqual(result['state'], 'not_attached')
        self.assertFalse(result['ready'])
        self.assertTrue(result['hint'].startswith('Setup is incomplete:'))
        self.assertIn('tell your peers', result['hint'])
        self.assertFalse(service.bindings())  # Readiness checks must not connect or bind.
    def test_codex_status_ignores_an_inherited_claude_channel_flag(self):
        key = service.register(self.binding)
        service.set_status(key, 'listening')
        (service.state_dir() / 'service.json').write_text(json.dumps({'heartbeat': time.time()}))
        for inherited in ('1', 'unavailable'):
            with self.subTest(inherited=inherited), patch.dict(os.environ, {
                'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CLAUDE_CHANNEL': inherited,
            }):
                result = delivery_status('room', 'member-1', 'private-token')
                self.assertEqual((result['state'], result['ready']), ('listening', True))
                missing = delivery_status('room', 'member-1', 'wrong-token')
                self.assertEqual(missing['state'], 'not_attached')
                self.assertIn('Launch Codex', missing['hint'])
                self.assertNotIn('Monitor', missing['hint'])
    def test_codex_can_stop_and_change_filter_with_an_inherited_claude_flag(self):
        key = service.register(self.binding)
        service.set_status(key, 'listening')
        (service.state_dir() / 'service.json').write_text(json.dumps({'heartbeat': time.time()}))
        for inherited in ('1', 'unavailable'):
            with self.subTest(inherited=inherited), patch.dict(os.environ, {
                'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CLAUDE_CHANNEL': inherited,
            }):
                service.configure_listener('room', 'member-1', 'private-token', enabled=True)
                stopped = listen('room', 'member-1', 'private-token', enabled=False)
                self.assertFalse(service.bindings()[0]['enabled'])
                self.assertFalse(stopped['ready'])
                listen('room', 'member-1', 'private-token', filter_mode='at')
                row = service.bindings()[0]
                self.assertFalse(row['enabled'])
                self.assertEqual(json.loads(row['config'])['filter'], 'at')
    def test_codex_readiness_needs_an_enabled_listening_subscription(self):
        key = service.register(self.binding)
        service_file = service.state_dir() / 'service.json'
        service_file.write_text(json.dumps({'heartbeat': time.time()}))
        for state in ('starting', 'reconnecting', 'attention', 'ended', 'stopped', 'listening'):
            with self.subTest(state=state):
                service.set_status(key, state)
                result = delivery_status('room', 'member-1', 'private-token')
                self.assertEqual(result['ready'], state == 'listening')
        service.configure_listener('room', 'member-1', 'private-token', enabled=False)
        # A delayed worker status update must not overrule the user's stop.
        service.set_status(key, 'listening')
        late = delivery_status('room', 'member-1', 'private-token')
        self.assertFalse(late['ready'])
        # Nor may the top-level state or the hint suggest it is, or should be, running.
        self.assertEqual(late['state'], 'stopping')
        self.assertIn('stays stopped', late['hint'])
        self.assertNotIn('recovers on its own', late['hint'])
        self.assertFalse(delivery_status('room', 'member-1', 'wrong-token')['ready'])
    def test_codex_stop_and_terminal_states_come_before_service_health(self):
        key = service.register(self.binding)
        (service.state_dir() / 'service.json').unlink(missing_ok=True)   # no live service at all
        service.configure_listener('room', 'member-1', 'private-token', enabled=False)
        stopped = delivery_status('room', 'member-1', 'private-token')
        # A listener stopped on purpose is not a service fault to repair.
        self.assertEqual((stopped['state'], stopped['ready']), ('stopped', False))
        self.assertIn('stays stopped', stopped['hint'])
        self.assertNotIn('service', stopped['hint'].lower())
        service.configure_listener('room', 'member-1', 'private-token', enabled=True)
        for state, error, phrases in (
                ('attention', 'unconfirmed_delivery', ('owning thread', 'durable delivery ledger', 'does not settle it')),
                ('ended', 'membership_ended', ('not revived automatically', 'Never reconnect or reclaim'))):
            with self.subTest(state=state):
                service.set_status(key, state, error)
                result = delivery_status('room', 'member-1', 'private-token')
                self.assertEqual((result['state'], result['ready']), (state, False))
                self.assertIn(error, result['hint'])
                for phrase in phrases:
                    self.assertIn(phrase, result['hint'])
                # Restarting the service or the listener resolves neither.
                self.assertNotIn('trio start', result['hint'])
                self.assertNotIn('enabled=true', result['hint'])
    def test_codex_saved_listening_state_is_not_ready_without_a_fresh_service(self):
        key = service.register(self.binding)
        service.set_status(key, 'listening')
        service_file = service.state_dir() / 'service.json'
        now = time.time()
        cases = (None, '{invalid', '{}', '[]', json.dumps({'heartbeat': 'bad'}),
                 json.dumps({'heartbeat': now - 60}), json.dumps({'heartbeat': now + 60}),
                 json.dumps({'heartbeat': float('nan')}),
                 json.dumps({'heartbeat': float('inf')}))
        for heartbeat in cases:
            with self.subTest(heartbeat=heartbeat):
                if heartbeat is None:
                    service_file.unlink(missing_ok=True)
                else:
                    service_file.write_text(heartbeat)
                result = delivery_status('room', 'member-1', 'private-token')
                self.assertFalse(result['ready'])
                self.assertNotEqual(result['state'], 'listening')
                self.assertIn('service', result['hint'].lower())
        # Status inspection must neither reconnect nor mutate the saved binding.
        row = service.bindings()[0]
        self.assertEqual((row['status'], row['enabled'], row['revision']), ('listening', 1, 1))
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
    def test_launcher_leaves_codex_invocations_that_open_no_session_alone(self):
        import nth_cli

        def launched(arguments, terminal=True):
            with patch.object(nth_cli, 'ensure_codex', return_value=('unix:///tmp/test.sock', 'codex')) as server, \
                 patch.object(nth_cli, 'codex_binary', return_value='codex'), \
                 patch.object(nth_cli, 'terminal_attached', return_value=terminal), \
                 patch.object(nth_cli.subprocess, 'call', return_value=0) as call:
                nth_cli.main(['codex', *arguments])
            return call.call_args.args[0], server.called

        # With `codex` aliased to `trio codex`, these must behave as they always did:
        # typed argv, and no shared app-server started on their account.
        for arguments in (['exec', 'a prompt'], ['login'], ['mcp', 'list'], ['--version'], ['resume', '--help'],
                          ['-C', 'app', 'exec', 'a prompt'], ['-m', 'chosen-model', 'review'], ['help']):
            with self.subTest(plain=arguments):
                self.assertEqual(launched(arguments), (['codex', *arguments], False))
        # A session, or session management the shared server owns. `-p` is --profile
        # and `-C` is --cd: their values are not subcommands.
        for arguments in ([], ['a prompt'], ['-C', 'app'], ['-p', 'exec'], ['fork', 'thread-id'],
                          ['-m', 'chosen-model', '--', 'exec']):
            with self.subTest(session=arguments):
                self.assertEqual(launched(arguments),
                                 (['codex', '--remote', 'unix:///tmp/test.sock', *arguments], True))
        # No terminal: the interactive form is left alone, session management is not.
        self.assertEqual(launched(['a prompt'], terminal=False), (['codex', 'a prompt'], False))
        self.assertEqual(launched(['archive', 'thread-id'], terminal=False),
                         (['codex', '--remote', 'unix:///tmp/test.sock', 'archive', 'thread-id'], True))
        self.assertEqual(launched(['--', 'exec', 'a prompt']), (['codex', 'exec', 'a prompt'], False))

    def test_native_connect_keeps_tokens_out_of_launch_command(self):
        identity = {'channel': 'room', 'member_id': 'member-1', 'session_token': 'private-token', 'reclaim_secret': 'private-reclaim'}
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'claude'}):
            result = native_connect_response(dict(identity))
        self.assertNotIn('private-token', result['monitor_hint'])
        self.assertNotIn('private-reclaim', result['monitor_hint'])
        argv = shlex.split(result['monitor_hint'])
        self.assertEqual(argv[0], sys.executable.replace('\\', '/') if os.name == 'nt' else sys.executable)
        self.assertEqual(Path(argv[argv.index('--identity') + 1]), Path(result['identity_file']))
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
                installed_skill = profile / client / 'skills' / flavor
                for installed_name, source_name in (
                    ('SKILL.md', f'SKILL-{flavor}.md'), ('AGENT-RUNTIME.md', 'AGENT-RUNTIME.md'),
                    ('REFERENCE.md', f'REFERENCE-{flavor}.md'),
                    ('PROTOCOLS.md', f'PROTOCOLS-{flavor}.md'),
                ):
                    self.assertEqual((installed_skill / installed_name).read_bytes(),
                                     (ROOT / source_name).read_bytes())
        self.assertTrue(Path(result['server'], 'nth_event_service.py').exists())
        self.assertTrue(list(profile.glob('.claude.json.bak-*')))
        # The installer tells the user how to make plain launches go through Trio,
        # and leaves the profile to them.
        windows, posix = setup.next_steps(result, platform='nt'), setup.next_steps(result, platform='posix')
        self.assertIn(f'& "{result["launcher"]}" shell-init powershell | Add-Content -Path $PROFILE', windows)
        self.assertIn('shell-init bash >> ~/.bashrc', posix)
        for text in (windows, posix):
            self.assertIn('Restart Claude Code and Codex', text)
            self.assertIn('To undo, delete the two functions', text)
        self.assertFalse(list(profile.glob('**/*profile*.ps1')) + list(profile.glob('.bashrc')))
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
