"""Hook delivery for a plainly launched Codex: isolated NTH_HOME and Codex home, a fake
`codex` on PATH that records its queue calls, scripted hubs, no real Codex."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / 'server'
sys.path.insert(0, str(SERVER_DIR))
import nth_listener as listener_module
import nth_claude_hook as core
import nth_codex_hook as hook
import nth_codex_socket as sockets
from nth_codex_runtime import CodexProtocolError

SESSION = '019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b'
KEY = '0123456789abcdef01234567'
KEY2 = 'abcdef0123456789abcdef01'
TOKEN = 'test-capability'
# Stands in for arbitrary peer text: distinctive, so a test can assert it never
# reaches the queued notice. Not an instruction.
PEER_MARK = 'PEER-CONTENT-MARKER-5e2a'
FAKE_CODEX = '''import json, os, sys, time
with open(os.environ['FAKE_CODEX_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
time.sleep(float(os.environ.get('FAKE_CODEX_SLEEP', '0')))
sys.exit(int(os.environ.get('FAKE_CODEX_EXIT', '0')))
'''
# A stand-in for the Codex daemon: started through a link named `codex`, with the
# daemon's flags on its command line (or a TUI's), it runs each hook event it is sent
# as Codex does: `/bin/sh -c`, a new process session, piped stdio, and the hook's
# process group killed afterwards, as Codex does when a hook times out.
FAKE_DAEMON = r'''import json, os, shlex, signal, subprocess, sys, time
python, script, home = sys.argv[1:4]
for line in sys.stdin:
    event, payload = json.loads(line)
    command = ' '.join(shlex.quote(part) for part in (python, script, '--home', home, event))
    started = time.monotonic()
    hook = subprocess.Popen(['/bin/sh', '-c', command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    out, err = hook.communicate(json.dumps(payload).encode(), timeout=20)
    took = time.monotonic() - started
    try:
        os.killpg(hook.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print(json.dumps([hook.returncode, out.decode(), err.decode(), took]), flush=True)
'''
MANAGED_DAEMON_ARGS = ['app-server', '--listen', 'unix://', '--managed-daemon']


def message(mid, **flags):
    return dict({'id': mid, 'from': 'sender ' + PEER_MARK, 'content': PEER_MARK}, **flags)


def codex_result(body, error=False):
    """A CallToolResult as Codex hands it to a PostToolUse hook."""
    text = json.dumps(body)
    return {'content': [{'type': 'text', 'text': text}], 'structuredContent': {'result': text},
            'isError': error}


class Hubs:
    """Scripted hubs by URL. Each poll returns the hub's next scripted batch, or its last
    one again (a backlog nobody acked), or nothing new. Records which hubs were polled."""

    def __init__(self, **scripts):
        self.scripts = {url: list(batches) for url, batches in scripts.items()}
        self.last, self.polled, self.on_poll = {}, [], None

    def factory(self, identity):
        url = identity['url']

        def poll(arguments):
            self.polled.append(url)
            script = self.scripts.get(url) or []
            if script:
                self.last[url] = script.pop(0)
            if self.on_poll:
                self.on_poll(url)
            if url in self.last:
                return {'event': 'new_messages', 'messages': list(self.last[url])}
            time.sleep(.02)
            return {'event': 'no_new', 'messages': []}
        return poll, None


class FakeSocket:
    """Loaded owning daemon; record inputs without creating any model turn."""
    def __init__(self, endpoint):
        self.endpoint = endpoint
    def start(self, **kwargs):
        pass
    def stop(self):
        pass
    def request(self, method, params, **kwargs):
        if method == 'thread/read':
            return {'thread': {'status': {'type': 'active'}}}
        assert method == 'turn/start'
        with open(os.environ['FAKE_CODEX_LOG'], 'a') as log:
            log.write(json.dumps(['turn/start', '--thread', params['threadId'], '--message',
                                 params['input'][0]['text']]) + '\n')
        if os.environ.get('FAKE_CODEX_SLEEP'):
            raise CodexProtocolError('Codex App Server timed out: turn/start')
        if os.environ.get('FAKE_CODEX_EXIT') == '1':
            raise CodexProtocolError('turn/start: refused')
        return {'turn': {'id': 'active-turn'}}


def clean_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(('TRIO_', 'NTH_', 'CLAUDE', 'CODEX_', 'FAKE_CODEX'))}


@unittest.skipIf(os.name == 'nt', 'the fake codex is a POSIX script')
class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home, self.codex_home, self.bin = root / 'nth', root / 'codex', root / 'bin'
        for path in (self.home / 'events' / 'identities', self.codex_home, self.bin):
            path.mkdir(parents=True)
        self.log = root / 'codex-calls.jsonl'
        fake = self.bin / 'codex'
        fake.write_text('#!' + sys.executable + '\n' + FAKE_CODEX, encoding='utf-8')
        fake.chmod(0o755)
        self.env = patch.dict(os.environ, dict(
            clean_env(), NTH_HOME=str(self.home), NTH_QUIET='1', TRIO_CODEX_HOME=str(self.codex_home),
            FAKE_CODEX_LOG=str(self.log), PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', '')),
            clear=True)
        self.env.start()
        self.fast = [patch.object(sockets, 'CodexSocketClient', FakeSocket), patch.multiple(core, TICK_SECONDS=.02, STATUS_EVERY_SECONDS=.1, LOCK_RETRY_SECONDS=.02),
                     patch.multiple(hook, SETTLE_SECONDS=.4, LIFETIME_SECONDS=.6, LOCK_PATIENCE_SECONDS=.1,
                                    QUEUE_RETRY_SECONDS=.01),
                     patch.object(listener_module, 'MIN_POLL_GAP_SECONDS', .02)]
        for fast in self.fast:
            fast.start()

    def tearDown(self):
        for fast in reversed(self.fast):
            fast.stop()
        self.env.stop()
        self.temp.cleanup()

    # ---- helpers -----------------------------------------------------------------------

    def identity(self, key=KEY, channel='room', member_id='member', url='http://hub-a.example/sse',
                 source='quartet'):
        record = {'channel': channel, 'member_id': member_id, 'session_token': TOKEN,
                  'reclaim_secret': '', 'source': source, 'url': url}
        (self.home / 'events' / 'identities' / (key + '.json')).write_text(json.dumps(record), encoding='utf-8')
        return record

    def payload(self, key=KEY, channel='room', member_id='member', tool='mcp__nth_qweb__quartet_connect',
                error=False):
        body = {'channel': channel, 'member_id': member_id, 'session_token': TOKEN,
                'identity_file': str(self.home / 'events' / 'identities' / (key + '.json'))}
        return {'session_id': SESSION, 'hook_event_name': 'PostToolUse', 'tool_name': tool,
                'tool_input': {}, 'tool_response': codex_result(body, error)}

    def join(self, key=KEY, tool='mcp__nth_qweb__quartet_connect', **identity):
        record = self.identity(key=key, **identity)
        core.register(self.payload(key=key, channel=record['channel'], member_id=record['member_id'],
                                   tool=tool), tools=hook.HOOK_TOOLS, client='codex')
        return record

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def run_waiter(self, hubs, supervisor=None):
        with patch.object(core, 'poll_factory', hubs.factory):
            return core.wait(SESSION, hook.QueueSink(SESSION, supervisor))

    def run_main(self, event, payload, **patches):
        spawned = []
        patches.setdefault('codex_host', lambda: (4321, ''))   # as if run by the managed daemon
        with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
             patch.object(sys, 'stderr', io.StringIO()), \
             patch.multiple(hook, spawn_waiter=lambda *args: spawned.append(args), **patches):
            code = hook.main(['--home', str(self.home), event])
        return code, spawned

    # ---- registration from PostToolUse ---------------------------------------------------

    def test_a_codex_connect_records_the_membership_and_its_server(self):
        self.join()
        state = core.load_session(SESSION)
        self.assertEqual(state['memberships'][KEY],
                         {'source': 'quartet', 'channel': 'room', 'member_id': 'member'})
        self.assertEqual(state['servers'][KEY], 'nth_qweb')
        self.assertEqual(state['client'], 'codex')

    def test_a_failed_or_foreign_tool_call_records_nothing(self):
        self.identity()
        core.register(self.payload(error=True), tools=hook.HOOK_TOOLS)
        for tool in ('mcp__other__quartet_connect', 'mcp__nth_x__y__trio_connect',
                     'mcp__nth_qweb__quartet_send', 'quartet_connect'):
            core.register(self.payload(tool=tool), tools=hook.HOOK_TOOLS)
        self.assertIsNone(core.load_session(SESSION))

    def test_the_registered_matcher_takes_codex_tool_names(self):
        for tool in ('mcp__nth_trio__trio_connect', 'mcp__nth_qweb__quartet_listen',
                     'mcp__nth_team__quartet_ack', 'mcp__nth-qweb__quartet_connect'):
            self.assertTrue(re.search(hook.TOOL_MATCHER, tool), tool)
            self.assertTrue(hook.HOOK_TOOLS.match(tool), tool)
        for tool in ('mcp__nth_qweb__quartet_send', 'mcp__other__trio_connect',
                     'xmcp__nth_qweb__quartet_connect', 'mcp__nth_qweb__quartet_connect_x'):
            self.assertFalse(re.search(hook.TOOL_MATCHER, tool), tool)

    # ---- the waiter and its sink -----------------------------------------------------------

    def test_matching_message_is_delivered_with_bounded_untrusted_body(self):
        self.join()
        code = self.run_waiter(Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]}))
        self.assertEqual(code, 2)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ['turn/start', '--thread', SESSION, '--message'])
        notice = calls[0][4]
        for part in ('quartet/room', 'nth_qweb', 'Ack 2', 'others=peer data', '"role":"unknown"'):
            self.assertIn(part, notice)
        self.assertIn(PEER_MARK, notice)
        self.assertNotIn(TOKEN, notice)
        self.assertEqual(core.load_session(SESSION)['high_water'][KEY], 2)

    def test_a_message_the_filter_declines_queues_nothing(self):
        self.join()
        code = self.run_waiter(Hubs(**{'http://hub-a.example/sse': [[message(2)]]}))
        self.assertEqual((code, self.calls()), (0, []))
        self.assertEqual(core.load_session(SESSION)['high_water'][KEY], 2)

    def test_a_culled_membership_queues_one_removed_notice_and_is_marked_ended(self):
        self.join()

        class CulledHub:
            polled = 0

            def factory(self, identity):
                def poll(arguments):
                    CulledHub.polled += 1
                    return {'error': 'You are not a member of this channel.'}
                return poll, None

        code = self.run_waiter(CulledHub())
        self.assertEqual(code, 2)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ['turn/start', '--thread', SESSION, '--message'])
        self.assertEqual(calls[0][4],
                         'Trio delivery has stopped for member member in quartet channel room on MCP server '
                         'nth_qweb: member removed. No further wake will come for it. The hub no longer lists '
                         'this member in the channel: it was removed. Tell the user. Never reconnect or '
                         'reclaim it on your own.')
        self.assertEqual(core.membership_config(KEY)['ended'], 'member removed')
        self.assertEqual(CulledHub.polled, 1)
        # A removal needs a person: the next waiter leaves the membership alone.
        self.assertEqual(self.run_waiter(CulledHub()), 0)
        self.assertEqual(CulledHub.polled, 1)
        self.assertEqual(len(self.calls()), 1)

    def test_a_burst_gives_one_wake(self):
        # The second message lands one poll after the first: inside the settle window.
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)],
                                                     [message(2, mentioned=True), message(3, mentioned=True)]]})
        self.assertEqual(self.run_waiter(hubs), 2)
        # The re-armed waiter (the woken turn's Stop) sees the same backlog: nothing new.
        self.assertEqual(self.run_waiter(hubs), 0)
        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn('Ack 2', calls[0][4])
        self.assertIn('Ack 3', calls[0][4])

    def test_memberships_on_several_servers_are_all_watched(self):
        self.join()
        self.join(key=KEY2, tool='mcp__nth_team__quartet_connect', channel='ops', member_id='helper',
                  url='http://hub-b.example/sse')
        hubs = Hubs(**{'http://hub-b.example/sse': [[message(5, mentioned=True)]]})
        self.assertEqual(self.run_waiter(hubs), 2)
        self.assertEqual(set(hubs.polled), {'http://hub-a.example/sse', 'http://hub-b.example/sse'})
        notice = self.calls()[0][4]
        self.assertIn('quartet/ops', notice)
        self.assertIn('nth_team', notice)
        self.assertNotIn('quartet/room', notice)

    def test_session_end_stops_a_waiting_waiter_without_a_wake(self):
        self.join()
        hubs = Hubs()
        result = []
        with patch.object(hook, 'LIFETIME_SECONDS', 30.0):
            waiter = threading.Thread(target=lambda: result.append(self.run_waiter(hubs)))
            waiter.start()
            deadline = time.monotonic() + 5
            while not hubs.polled and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(hubs.polled, 'the waiter never polled')
            self.run_main('end', {'session_id': SESSION, 'hook_event_name': 'SessionEnd', 'reason': 'other'})
            waiter.join(5)
        self.assertFalse(waiter.is_alive(), 'SessionEnd did not stop the waiter')
        self.assertEqual((result, self.calls()), ([0], []))
        # An ended session is not armed again by a later Stop.
        self.assertEqual(self.run_main('stop', {'session_id': SESSION})[1], [])

    def test_a_session_that_ends_while_the_wake_settles_is_not_woken(self):
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})

        def end_now(url):
            with core.session_update(SESSION) as state:
                state['ended'] = True
        hubs.on_poll = end_now
        self.assertEqual(self.run_waiter(hubs), 0)
        self.assertEqual(self.calls(), [])
        # Nothing was marked seen: a resumed session hears it from its next waiter.
        self.assertEqual(core.load_session(SESSION)['high_water'].get(KEY, 0), 0)

    def test_refused_submission_is_left_for_next_waiter(self):
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
        with patch.dict(os.environ, {'FAKE_CODEX_EXIT': '1'}):
            self.assertEqual(self.run_waiter(hubs), 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(core.load_session(SESSION)['high_water'].get(KEY, 0), 0)
        status = core.read_json(core.session_path(SESSION, '.status.json'))
        self.assertEqual(status['error'], 'wake not delivered')
        self.assertEqual(self.run_waiter(hubs), 2)       # the next waiter announces it again
        self.assertEqual(core.load_session(SESSION)['high_water'][KEY], 2)

    def test_a_thread_trios_event_service_delivers_to_is_left_alone(self):
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
        with patch.object(hook, 'relay_owns', return_value=True):
            self.assertEqual(self.run_waiter(hubs), 0)
        self.assertEqual((hubs.polled, self.calls()), ([], []))

    def test_the_waiter_leaves_when_its_daemon_exits(self):
        self.join()
        daemon = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        result = []
        with patch.object(hook, 'LIFETIME_SECONDS', 30.0):
            waiter = threading.Thread(target=lambda: result.append(self.run_waiter(Hubs(), supervisor=daemon.pid)))
            waiter.start()
            time.sleep(.3)
            daemon.kill()
            daemon.wait()
            waiter.join(5)
        self.assertFalse(waiter.is_alive())
        self.assertEqual((result, self.calls()), ([0], []))

    def test_one_waiter_per_session(self):
        self.join()
        held, release = threading.Event(), threading.Event()

        def hold():
            with core.file_lock(core.session_path(SESSION, '.lock'), blocking=False) as ok:
                if ok:
                    held.set()
                    release.wait(5)
        keeper = threading.Thread(target=hold, daemon=True)
        keeper.start()
        self.assertTrue(held.wait(5))
        try:
            hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
            self.assertEqual(self.run_waiter(hubs), 0)
            self.assertEqual((hubs.polled, self.calls()), ([], []))
        finally:
            release.set()
            keeper.join(5)

    def test_the_codex_binary_comes_from_the_override_then_path(self):
        self.assertEqual(hook.codex_binary(), str(self.bin / 'codex'))
        other = self.bin / 'codex-pinned'
        other.write_text('')
        with patch.dict(os.environ, {'TRIO_CODEX_BINARY': str(other)}):
            self.assertEqual(hook.codex_binary(), str(other))
        with patch.dict(os.environ, {'TRIO_CODEX_BINARY': str(self.bin / 'missing')}):
            self.assertIsNone(hook.codex_binary())

    # ---- main: which events arm --------------------------------------------------------------

    def test_stop_and_tool_arm_only_a_session_with_memberships(self):
        self.assertEqual(self.run_main('stop', {'session_id': SESSION})[1], [])
        self.identity()
        code, spawned = self.run_main('tool', self.payload())
        self.assertEqual(code, 0)
        self.assertEqual([args[0] for args in spawned], [SESSION])
        self.assertEqual([args[0] for args in self.run_main('stop', {'session_id': SESSION})[1]], [SESSION])

    def test_resume_takes_an_ended_session_back_and_other_starts_do_not(self):
        self.join()
        self.run_main('end', {'session_id': SESSION})
        for source in ('clear', 'compact', 'fork'):
            self.assertEqual(self.run_main('start', {'session_id': SESSION, 'source': source})[1], [])
        self.assertTrue(core.load_session(SESSION)['ended'])
        spawned = self.run_main('start', {'session_id': SESSION, 'source': 'resume'})[1]
        self.assertFalse(core.load_session(SESSION)['ended'])
        self.assertEqual([args[0] for args in spawned], [SESSION])

    def test_hooks_stand_down_inside_the_trio_codex_server(self):
        (self.home / 'events' / 'codex-server.json').write_text(json.dumps({'pid': 4242}))
        self.identity()
        code, spawned = self.run_main('tool', self.payload(), codex_host=lambda: (4242, ''))
        self.assertEqual((code, spawned), (0, []))
        self.assertIsNone(core.load_session(SESSION))

    def test_a_malformed_session_id_is_ignored(self):
        for payload in ({'session_id': ''}, {'session_id': 'has spaces'}, {'other': 1}):
            self.assertEqual(self.run_main('stop', payload), (0, []))

    def test_a_host_other_than_the_managed_daemon_records_why_and_arms_nothing(self):
        import nth_event_access as access
        self.install()
        self.join()
        problem = 'the hook ran in a Codex process that is not the shared app-server daemon'
        code, spawned = self.run_main('stop', {'session_id': SESSION}, codex_host=lambda: (4321, problem))
        self.assertEqual((code, spawned), (0, []))
        self.assertEqual(core.read_status(SESSION)['problem'], problem)
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
        self.assertEqual((status['state'], status['ready']), ('unavailable', False))
        self.assertIn('not the shared app-server daemon', status['hint'])

    def test_only_the_shared_daemon_command_line_counts(self):
        managed = [['/usr/bin/codex', 'app-server', '--listen', 'unix://', '--managed-daemon'],
                   ['codex', 'app-server', '--remote-control', '--listen', 'unix://'],
                   ['codex', 'app-server', '--listen=unix://']]
        others = [['codex'], ['codex', 'resume', 'x'], ['codex', 'app-server', '--listen', 'stdio://'],
                  ['codex', 'app-server'], ['codex', 'app-server', '--listen', 'unix:///run/trio/codex.sock'],
                  ['codex', 'app-server', '--listen', 'ws://127.0.0.1:4500'], None, []]
        for argv in managed:
            self.assertTrue(hook.is_managed_daemon(argv), argv)
        for argv in others:
            self.assertFalse(hook.is_managed_daemon(argv), argv)

    def test_the_host_is_the_nearest_codex_ancestor_past_the_shell(self):
        def tree(processes):
            return patch.multiple(hook, process_info=lambda pid: processes.get(pid, (None, None)))
        parent = os.getppid()
        with tree({parent: ('bash', 50), 50: ('codex', 1)}):
            self.assertEqual(hook.codex_ancestor(), 50)
        with tree({parent: ('codex', 1)}):
            self.assertEqual(hook.codex_ancestor(), parent)
        with tree({parent: ('zsh', 50), 50: ('sh', 60), 60: ('codex-x86_64-unknown-linux-musl', 1)}):
            self.assertEqual(hook.codex_ancestor(), 60)
        # A shell Trio does not know, or no Codex at all: never the wrong process.
        with tree({parent: ('xonsh', 50), 50: ('codex', 1)}):
            self.assertIsNone(hook.codex_ancestor())
            self.assertEqual(hook.codex_host(), (None, 'the hook was not run by a Codex process'))
        with tree({}):
            self.assertIsNone(hook.codex_ancestor())

    # ---- detachment: the real hook as Codex runs it ------------------------------------------

    def start_daemon(self, managed=True):
        host = self.bin.parent / 'host'
        host.mkdir(exist_ok=True)
        if not (host / 'codex').exists():
            (host / 'codex').symlink_to(sys.executable)
        argv = [str(host / 'codex'), '-c', FAKE_DAEMON, sys.executable, str(SERVER_DIR / 'nth_codex_hook.py'),
                str(self.home)] + (MANAGED_DAEMON_ARGS if managed else ['--no-alt-screen'])
        daemon = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(self.stop_daemon, daemon)
        return daemon

    @staticmethod
    def stop_daemon(daemon):
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait()
        daemon.stdin.close()
        daemon.stdout.close()

    @staticmethod
    def send(daemon, event, payload):
        daemon.stdin.write((json.dumps([event, payload]) + '\n').encode())
        daemon.stdin.flush()
        line = daemon.stdout.readline()
        if not line:
            raise AssertionError('the stand-in daemon died: a hook did not return in time')
        return json.loads(line)

    def wait_for_waiter(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = core.read_status(SESSION) or {}
            if status.get('pid'):
                pid = status['pid']
                self.addCleanup(lambda: core.process_stamp(pid) is not None and os.kill(pid, signal.SIGKILL))
                return pid, status
            time.sleep(.1)
        self.fail('no waiter reported itself')

    def wait_gone(self, pid):
        deadline = time.monotonic() + 10
        while core.process_stamp(pid) is not None and time.monotonic() < deadline:
            time.sleep(.1)
        return core.process_stamp(pid) is None

    def test_the_hook_returns_at_once_while_its_waiter_lives_on(self):
        # Codex reads a hook's stdout and stderr until they close, and kills the hook's
        # process group on a timeout. A waiter that kept the pipes would hold the hook
        # open; one left in the group would die with it.
        self.join(url='http://127.0.0.1:9/sse')          # unreachable: the waiter keeps retrying
        daemon = self.start_daemon()
        code, out, err, took = self.send(daemon, 'stop', {'session_id': SESSION, 'hook_event_name': 'Stop'})
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertLess(took, 5)
        waiter, status = self.wait_for_waiter()
        self.assertEqual((status['client'], status['session']), ('codex', SESSION))
        self.assertEqual(status['supervisor_pid'], daemon.pid)          # the daemon that ran the hook
        with core.file_lock(core.session_path(SESSION, '.lock'), blocking=False) as free:
            self.assertFalse(free, 'the waiter does not hold the session lock')
        code, out, err, _ = self.send(daemon, 'end', {'session_id': SESSION, 'hook_event_name': 'SessionEnd'})
        self.assertEqual((code, out, err), (0, '', ''))
        self.assertTrue(self.wait_gone(waiter), 'SessionEnd did not stop the waiter')
        self.assertEqual(self.calls(), [])

    def test_the_waiter_follows_the_daemon_that_ran_its_hook(self):
        self.join(url='http://127.0.0.1:9/sse')
        daemon = self.start_daemon()
        self.assertEqual(self.send(daemon, 'stop', {'session_id': SESSION})[:3], [0, '', ''])
        waiter, _ = self.wait_for_waiter()
        daemon.kill()
        daemon.wait()
        self.assertTrue(self.wait_gone(waiter), 'the waiter outlived its daemon')

    def test_a_tui_without_the_daemon_gets_no_waiter(self):
        # codex queue reaches the shared daemon; a thread living in another server would
        # be run a second time there.
        self.join(url='http://127.0.0.1:9/sse')
        daemon = self.start_daemon(managed=False)
        self.assertEqual(self.send(daemon, 'stop', {'session_id': SESSION})[:3], [0, '', ''])
        status = core.read_status(SESSION)
        self.assertEqual(status['pid'], 0)
        self.assertIn('not the shared app-server daemon', status['problem'])
        time.sleep(.5)
        self.assertEqual(core.read_status(SESSION)['pid'], 0)

    # ---- no cap: the filter decides ----------------------------------------------------------

    def wake_once(self, hubs, mid):
        # A full rate-limit bucket each time: the rate limit only spaces wakes out.
        with core.session_update(SESSION) as state:
            state['bucket'] = None
        hubs.last['http://hub-a.example/sse'] = [message(mid, mentioned=True)]
        return self.run_waiter(hubs)

    def test_every_message_that_passes_the_filter_wakes_with_no_cap(self):
        import nth_event_access as access
        self.install()
        self.join()
        hubs = Hubs()
        self.assertEqual([self.wake_once(hubs, mid) for mid in range(2, 27)], [2] * 25)
        self.assertEqual(len(self.calls()), 25)
        state = core.load_session(SESSION)
        self.assertNotIn('paused', state)
        self.assertNotIn('unattended_wakes', state)
        # A Stop after all that still arms the next waiter.
        self.assertEqual([args[0] for args in self.run_main('stop', {'session_id': SESSION})[1]], [SESSION])
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
        self.assertNotEqual(status['state'], 'paused')

    def test_the_retired_prompt_event_does_nothing(self):
        # An install from an earlier release still runs `... prompt` until it is reinstalled.
        self.join()
        before = core.load_session(SESSION)
        self.assertEqual(self.run_main('prompt', {'session_id': SESSION, 'prompt': 'hello'}), (0, []))
        self.assertEqual(core.load_session(SESSION), before)

    def test_a_resume_rearms(self):
        self.join()
        spawned = self.run_main('start', {'session_id': SESSION, 'source': 'resume'})[1]
        self.assertEqual([args[0] for args in spawned], [SESSION])

    # ---- queue outcomes ----------------------------------------------------------------------

    def test_a_queue_that_times_out_counts_as_announced(self):
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
        with patch.object(hook, 'QUEUE_TIMEOUT_SECONDS', .3), patch.dict(os.environ, {'FAKE_CODEX_SLEEP': '2'}):
            self.assertEqual(self.run_waiter(hubs), 2)
        self.assertEqual(len(self.calls()), 1)                       # not retried: it may have queued
        self.assertEqual(core.load_session(SESSION)['high_water'][KEY], 2)
        self.assertIn('timed out', core.read_status(SESSION)['note'])
        self.assertEqual(self.run_waiter(hubs), 0)                   # the same ids are not announced again
        self.assertEqual(len(self.calls()), 1)

    def test_no_codex_executable_means_no_waiter_and_says_so(self):
        import nth_event_access as access
        self.install()
        self.join()
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
        with patch.dict(os.environ, {'TRIO_CODEX_BINARY': str(self.bin / 'missing')}):
            self.assertEqual(self.run_waiter(hubs), 0)
        self.assertEqual(hubs.polled, [])
        self.assertIn('no codex executable', core.read_status(SESSION)['problem'])
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
        self.assertEqual((status['state'], status['ready']), ('unavailable', False))

    def test_status_says_delivering_while_the_wake_is_queued(self):
        import nth_event_access as access
        self.install()
        self.join()
        seen = []

        def deliver(lines):
            with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
                seen.append(access.delivery_status('room', 'member', TOKEN, session=SESSION))
            return True
        hubs = Hubs(**{'http://hub-a.example/sse': [[message(2, mentioned=True)]]})
        sink = hook.QueueSink(SESSION, None)
        sink.deliver = deliver
        with patch.object(core, 'poll_factory', hubs.factory):
            self.assertEqual(core.wait(SESSION, sink), 2)
        self.assertEqual((seen[0]['state'], seen[0]['ready']), ('delivering', False))

    # ---- what the MCP server reports ----------------------------------------------------------

    def install(self):
        data = hook.install_hooks({}, sys.executable, SERVER_DIR / 'nth_codex_hook.py', self.home)
        hook.save_hooks_file(hook.hooks_file(), data)

    def live_status(self, session, key, client='codex', state='listening'):
        core.write_status(session, client, os.getpid(), {key: {'status': state, 'error': '', 'filter': 'about'}})

    def test_a_plain_codex_with_the_hooks_is_told_hooks_and_status_tells_the_truth(self):
        import nth_event_access as access
        self.install()
        record = self.identity()
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            joined = access.native_connect_response({'channel': 'room', 'member_id': 'member',
                                                     'session_token': TOKEN}, source='quartet',
                                                    url=record['url'])
            self.assertEqual(joined['event_delivery']['mode'], 'hooks')
            self.assertEqual(joined['monitor_hint'], '')
            self.assertIn('/hooks', joined['instructions'])
            # No session has picked the membership up (hooks untrusted): not ready, and it says why.
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
            self.assertEqual((status['state'], status['ready'], status['waiter']), ('hooks', False, 'none'))
            self.assertIn('Trust all and continue', status['hint'])
            self.join()
            # This session's own live waiter reports the membership listening: ready.
            self.live_status(SESSION, KEY)
            for session in (SESSION, None):                       # None: the newest holder
                status = access.delivery_status('room', 'member', TOKEN, session=session)
                self.assertEqual((status['state'], status['ready'], status['waiter'], status['session']),
                                 ('listening', True, 'running', SESSION))
            # A stale heartbeat is not a waiter.
            core.write_json(core.session_path(SESSION, '.status.json'), dict(
                core.read_status(SESSION), heartbeat=time.time() - 600))
            self.assertFalse(access.delivery_status('room', 'member', TOKEN, session=SESSION)['ready'])
            # listen saves the filter for the waiter and names the identity for the tool hook.
            reply = access.listen('room', 'member', TOKEN, filter_mode='at', session=SESSION)
            self.assertEqual((reply['state'], reply['identity_key'], reply['filter_mode']), ('hooks', KEY, 'at'))
            self.assertEqual(core.membership_config(KEY)['filter'], 'at')
            stopped = access.listen('room', 'member', TOKEN, enabled=False)
            self.assertEqual((stopped['delivery_state'], stopped['ready']), ('stopped', False))
        restarted = '0d1e2f30-4050-4607-8809-0a0b0c0d0e0f'
        core.register({'session_id': restarted, 'tool_name': 'mcp__nth_qweb__quartet_listen',
                       'tool_response': codex_result(reply)}, tools=hook.HOOK_TOOLS, client='codex')
        self.assertEqual(list(core.load_session(restarted)['memberships']), [KEY])
        # Under `trio codex` the event service delivers: the hooks are not claimed.
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CODEX_ENDPOINT': 'unix:///x.sock'}):
            self.assertEqual(access.delivery_status('room', 'member', TOKEN)['state'], 'not_attached')

    def test_status_never_borrows_another_sessions_or_clients_waiter(self):
        import nth_event_access as access
        self.install()
        self.join()
        other = '0d1e2f30-4050-4607-8809-0a0b0c0d0e0f'
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            # A Claude waiter listing the same key is not a Codex waiter.
            self.live_status(SESSION, KEY, client='claude')
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
            self.assertEqual((status['ready'], status['waiter']), (False, 'none'))
            # Another Codex session's waiter: said so, never a bare ready.
            core.register(dict(self.payload(), session_id=other), tools=hook.HOOK_TOOLS, client='codex')
            core.write_status(SESSION, 'codex', 0, {})
            self.live_status(other, KEY)
            status = access.delivery_status('room', 'member', TOKEN, session=SESSION)
            self.assertEqual((status['ready'], status['waiter'], status['waiter_session']),
                             (False, 'other_session', other))
            self.assertIn('another Codex session', status['hint'])
            # Asked from that other session, it is that session's own waiter: ready.
            self.assertTrue(access.delivery_status('room', 'member', TOKEN, session=other)['ready'])

    def test_a_hub_added_under_trio_codex_is_told_the_hooks_stand_down(self):
        import nth_event_access as access
        self.install()
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}), \
             patch.object(access, '_under_trio_codex_server', return_value=True):
            joined = access.native_connect_response({'channel': 'room', 'member_id': 'member',
                                                     'session_token': TOKEN})
            status = access.delivery_status('room', 'member', TOKEN)
        self.assertEqual(joined['event_delivery']['mode'], 'manual_attach')
        self.assertIn('stand down', joined['instructions'])
        self.assertNotIn('/hooks', joined['instructions'])
        self.assertEqual(status['state'], 'not_attached')
        self.assertIn('stand down', status['hint'])

    def test_the_caller_session_comes_from_the_request_meta(self):
        import nth_event_access as access
        self.assertEqual(access.caller_session({'sessionId': SESSION, 'threadId': SESSION}), SESSION)
        for meta in (None, {}, {'sessionId': 'has spaces'}, {'sessionId': 7}):
            self.assertIsNone(access.caller_session(meta))

    def test_without_the_hooks_a_plain_codex_is_still_manual_attach(self):
        import nth_event_access as access
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'codex'}):
            joined = access.native_connect_response({'channel': 'room', 'member_id': 'member',
                                                     'session_token': TOKEN})
        self.assertEqual(joined['event_delivery']['mode'], 'manual_attach')


class RegistrationTests(unittest.TestCase):
    FOREIGN = {'command': 'notify.sh', 'type': 'command'}

    def install(self, data, python='/venv/bin/python', runtime='/nth'):
        return hook.install_hooks(data, python, '/nth/server/nth_codex_hook.py', runtime)

    def test_install_is_idempotent_and_keeps_everyone_elses_hooks_in_place(self):
        data = {'description': 'mine', 'hooks': {
            'Stop': [{'hooks': [self.FOREIGN]}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [self.FOREIGN]}]}}
        self.install(data)
        data['hooks']['Stop'].append({'hooks': [dict(self.FOREIGN, command='later.sh')]})
        first = json.loads(json.dumps(data))
        self.install(data)
        self.assertEqual(data, first)                          # twice: nothing moves or doubles
        stop = data['hooks']['Stop']
        # Codex keys trust by group index: ours stays where it was, the user's hooks too.
        self.assertEqual([hook.is_trio_group(g) for g in stop], [False, True, False])
        self.assertEqual(data['hooks']['PreToolUse'], [{'matcher': 'Bash', 'hooks': [self.FOREIGN]}])
        self.assertEqual(data['description'], 'mine')
        # A changed command (a moved install) is rewritten in the same slot.
        self.install(data, python='/other/python')
        self.assertEqual([hook.is_trio_group(g) for g in data['hooks']['Stop']], [False, True, False])
        self.assertIn('/other/python', data['hooks']['Stop'][1]['hooks'][0]['command'])

    def test_the_written_entries(self):
        data = self.install({})
        self.assertEqual(set(data), {'hooks'})                 # Codex rejects unknown top-level keys
        tool = data['hooks']['PostToolUse'][0]
        self.assertEqual(tool['matcher'], hook.TOOL_MATCHER)
        self.assertEqual(tool['hooks'][0], {'type': 'command', 'timeout': hook.HOOK_TIMEOUT_SECONDS,
                                            'command': '/venv/bin/python /nth/server/nth_codex_hook.py '
                                                       '--home /nth tool'})
        self.assertEqual(data['hooks']['SessionStart'][0]['matcher'], 'startup|resume')
        self.assertNotIn('matcher', data['hooks']['Stop'][0])
        self.assertEqual(data['hooks']['SessionEnd'][0]['hooks'][0]['timeout'], 3)
        for event, action, _ in hook.HOOK_EVENTS:
            entry = data['hooks'][event][0]['hooks'][0]
            self.assertTrue(entry['command'].endswith(' ' + action))
            self.assertNotIn('async', entry)                   # sync: the hook only spawns and returns

    def test_uninstall_removes_only_trios_groups(self):
        data = self.install({'hooks': {'Stop': [{'hooks': [self.FOREIGN]}]}})
        self.assertEqual(hook.uninstall_hooks(data), len(hook.HOOK_EVENTS))
        self.assertEqual(data, {'hooks': {'Stop': [{'hooks': [self.FOREIGN]}]}})
        self.assertEqual(hook.uninstall_hooks(data), 0)
        self.assertEqual(hook.uninstall_hooks({}), 0)
        # A group the user shares with our handler is theirs: left alone.
        mixed = {'hooks': {'Stop': [{'hooks': [self.FOREIGN, self.install({})['hooks']['Stop'][0]['hooks'][0]]}]}}
        self.assertEqual(hook.uninstall_hooks(mixed), 0)

    def test_a_reinstall_removes_the_old_prompt_group_in_place(self):
        old = {'type': 'command', 'command': '/venv/bin/python /nth/server/nth_codex_hook.py --home /nth prompt',
               'timeout': 30}
        mine, later = {'hooks': [self.FOREIGN]}, {'hooks': [dict(self.FOREIGN, command='later.sh')]}
        data = self.install({'hooks': {'UserPromptSubmit': [mine, {'hooks': [old]}, later]}})
        self.assertEqual(data['hooks']['UserPromptSubmit'], [mine, later])
        self.assertTrue(all(event != 'UserPromptSubmit' for event, _, _ in hook.HOOK_EVENTS))
        # Trio's group alone: the event goes with it.
        data = self.install({'hooks': {'UserPromptSubmit': [{'hooks': [old]}]}})
        self.assertNotIn('UserPromptSubmit', data['hooks'])
        # And uninstall still finds it in a file nobody reinstalled.
        stale = {'hooks': {'UserPromptSubmit': [mine, {'hooks': [old]}]}}
        self.assertEqual(hook.uninstall_hooks(stale), 1)
        self.assertEqual(stale, {'hooks': {'UserPromptSubmit': [mine]}})

    def test_a_quoted_path_with_spaces_is_still_recognised(self):
        data = hook.install_hooks({}, '/opt/my venv/python', '/my nth/server/nth_codex_hook.py', '/my nth')
        command = data['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertIn("'/my nth/server/nth_codex_hook.py'", command)
        self.assertTrue(hook.is_trio_group(data['hooks']['Stop'][0]))
        self.assertFalse(hook.is_trio_group({'hooks': [{'type': 'command', 'command': 'x_nth_codex_hook.py.bak'}]}))


@unittest.skipIf(os.name == 'nt', 'the fake codex is a POSIX script')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.profile = Path(self.temp.name) / 'profile'
        (self.profile / '.codex').mkdir(parents=True)
        self.log = Path(self.temp.name) / 'codex-calls.jsonl'
        self.fake = Path(self.temp.name) / 'codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_CODEX, encoding='utf-8')
        self.fake.chmod(0o755)
        self.env = patch.dict(os.environ, dict(clean_env(), FAKE_CODEX_LOG=str(self.log), NTH_QUIET='1'),
                              clear=True)
        self.env.start()
        spec = importlib.util.spec_from_file_location('native_setup_codex', ROOT / 'setup.py')
        self.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.setup)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def install(self):
        return self.setup.install(self.profile, clients=('codex',), skip_dependencies=True,
                                  codex_binary=str(self.fake))

    def test_install_writes_hooks_json_keeps_the_rest_and_is_idempotent(self):
        codex = self.profile / '.codex'
        config = '# my settings\nmodel = "x"  # keep this comment\n'
        (codex / 'config.toml').write_text(config, encoding='utf-8')
        user = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'notify.sh'}]}]}}
        (codex / 'hooks.json').write_text(json.dumps(user), encoding='utf-8')
        result = self.install()
        self.assertEqual(result['codex_hooks'], str(codex / 'hooks.json'))
        self.assertFalse(result['codex_toml_hooks'])
        self.assertEqual((codex / 'config.toml').read_text(encoding='utf-8'), config)
        written = json.loads((codex / 'hooks.json').read_text(encoding='utf-8'))
        self.assertEqual(written['hooks']['Stop'][0], user['hooks']['Stop'][0])
        self.assertTrue(hook.is_trio_group(written['hooks']['Stop'][1]))
        self.assertIn(str(Path(result['server']) / 'nth_codex_hook.py'),
                      written['hooks']['Stop'][1]['hooks'][0]['command'])
        backups = list(codex.glob('hooks.json.bak-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8')), user)
        if os.name != 'nt':
            self.assertEqual((codex / 'hooks.json').stat().st_mode & 0o077, 0)
        before = (codex / 'hooks.json').read_bytes()
        self.install()
        self.assertEqual((codex / 'hooks.json').read_bytes(), before)
        # The MCP servers learn where the hooks are: Codex does not pass CODEX_HOME to them.
        adds = [call for call in (json.loads(line) for line in self.log.read_text().splitlines())
                if call[:2] == ['mcp', 'add']]
        self.assertTrue(adds)
        for call in adds:
            self.assertIn('TRIO_CODEX_HOME=' + str(codex), call)
        steps = self.setup.next_steps(result)
        self.assertIn('Trust all and continue', steps)
        self.assertIn('trio hooks-uninstall', steps)

    def test_a_hooks_file_that_is_not_json_is_never_overwritten(self):
        codex = self.profile / '.codex'
        (codex / 'hooks.json').write_text('{ not json', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual((codex / 'hooks.json').read_text(encoding='utf-8'), '{ not json')

    def test_toml_hooks_are_noted(self):
        (self.profile / '.codex' / 'config.toml').write_text(
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "x"\n', encoding='utf-8')
        self.assertTrue(self.install()['codex_toml_hooks'])

    def test_hooks_uninstall_removes_the_codex_hooks(self):
        self.install()
        import nth_cli
        out = io.StringIO()
        with patch.dict(os.environ, {'TRIO_CODEX_HOME': str(self.profile / '.codex')}), \
             patch('sys.stdout', out):
            self.assertEqual(nth_cli.main(['hooks-uninstall', '--clients', 'codex']), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['codex_removed'], len(hook.HOOK_EVENTS))
        self.assertNotIn('removed', report)                  # Claude's settings were not touched
        self.assertFalse(hook.delivery_hooks_installed(self.profile / '.codex'))

    def test_hooks_uninstall_reports_an_unreadable_hooks_file_and_leaves_it(self):
        import nth_cli
        codex = self.profile / '.codex'
        (codex / 'hooks.json').write_text('{ not json', encoding='utf-8')
        out = io.StringIO()
        with patch.dict(os.environ, {'TRIO_CODEX_HOME': str(codex)}), patch('sys.stdout', out):
            self.assertEqual(nth_cli.main(['hooks-uninstall', '--clients', 'codex']), 1)
        report = json.loads(out.getvalue())
        self.assertEqual(report['codex_removed'], 0)
        self.assertIn('left unchanged', report['codex_error'])
        self.assertEqual((codex / 'hooks.json').read_text(encoding='utf-8'), '{ not json')


if __name__ == '__main__':
    unittest.main()
