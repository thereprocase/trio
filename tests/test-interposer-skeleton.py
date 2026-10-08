"""Interposer skeleton contracts; every test isolates home, runtime and processes."""
import concurrent.futures
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import nth_interposer as service
import nth_interposer_store as storage
import nth_interposer_wire as wire
import nth_cli as cli
import nth_doctor as doctor

spec = importlib.util.spec_from_file_location('interposer_setup', ROOT / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
KEY = '0123456789abcdef01234567'
SESSION = '11111111-2222-4333-8444-555555555555'
FIXTURES = ROOT / 'tests' / 'fixtures' / 'interposer-hooks'
EXPECTED_OPS = frozenset(('hello', 'hub.announce', 'session.register', 'membership.attach',
    'membership.configure', 'ack.seen', 'turn', 'session.end', 'wait', 'subscribe',
    'delivered', 'status', 'list'))


@unittest.skipUnless(hasattr(socket, 'AF_UNIX') and os.name == 'posix', 'Unix service skeleton')
class InterposerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ip2-')
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'rt'
        self.runtime.mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(self.root), NTH_HOME=str(self.root / 'nth'),
                   XDG_RUNTIME_DIR=str(self.runtime), CODEX_HOME=str(self.root / 'codex'),
                   TRIO_CODEX_HOME=str(self.root / 'codex'), PYTHONDONTWRITEBYTECODE='1')
        for field in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
            env.pop(field, None)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.processes = []
        self.user_runtime = patch.object(wire, '_user_runtime_dir', return_value=self.root / 'no-user-runtime')
        self.user_runtime.start()

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.env.stop()
        self.user_runtime.stop()
        self.tmp.cleanup()

    def start(self, idle=60, *, activated=False):
        command = [sys.executable, str(ROOT / 'server' / 'nth_interposer.py'), 'serve',
                   '--idle-seconds', str(idle)]
        if activated:
            wire.private_dir(wire.socket_path().parent)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(wire.socket_path()))
            sock.listen()
            code = ('import os,sys; os.dup2(int(sys.argv[1]),3); '
                    'os.set_inheritable(3,True); os.environ["LISTEN_PID"]=str(os.getpid()); '
                    'os.environ["LISTEN_FDS"]="1"; os.execv(sys.executable,sys.argv[2:])')
            process = subprocess.Popen([sys.executable, '-c', code, str(sock.fileno()), *command],
                                       pass_fds=(sock.fileno(),), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            sock.close()
        else:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with wire.connect(timeout=.2) as client:
                    self.assertEqual(client.hello['pid'], process.pid)
                    return process
            except (OSError, EOFError):
                if process.poll() is not None:
                    self.fail('service exited before hello')
                time.sleep(.01)
        self.fail('service did not answer hello')

    def raw(self, frame):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(str(wire.socket_path()))
            sock.sendall(frame)
            with sock.makefile('rb') as reader:
                return wire.read_frame(reader)

    def fake_systemctl(self, running=True, available=True):
        directory = self.root / 'bin'
        directory.mkdir(exist_ok=True)
        program = directory / 'systemctl'
        program.write_text('#!' + sys.executable + '\n' +
            'import json,os,sys\n'
            'with open(os.environ["SYSTEMCTL_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'sys.exit(0 if "is-active" not in sys.argv and "show-environment" not in sys.argv else '
            '(int(os.environ["SYSTEMCTL_ACTIVE"]) if "is-active" in sys.argv else '
            'int(os.environ["SYSTEMCTL_AVAILABLE"])))\n')
        program.chmod(0o755)
        self.command_log = self.root / 'systemctl.jsonl'
        os.environ.update(PATH=str(directory), SYSTEMCTL_LOG=str(self.command_log),
                          SYSTEMCTL_ACTIVE='0' if running else '3',
                          SYSTEMCTL_AVAILABLE='0' if available else '1')

    def calls(self):
        return [json.loads(line) for line in self.command_log.read_text().splitlines()]

class SkeletonTests(InterposerCase):
    def test_golden_frames(self):
        self.assertEqual(wire.OPS, EXPECTED_OPS)
        for op in sorted(EXPECTED_OPS):
            expected = ('{"v":1,"id":7,"op":"' + op + '"}\n').encode()
            value = {'v': 1, 'id': 7, 'op': op}
            self.assertEqual(wire.encode_frame(value), expected)
            self.assertEqual(wire.read_frame(io.BytesIO(expected)), value)
        self.assertEqual(wire.encode_frame({'v': 1, 'id': 7, 'ok': {}}), b'{"v":1,"id":7,"ok":{}}\n')
        self.assertEqual(wire.encode_frame({'v': 1, 'id': 7, 'error': 'unknown op'}),
                         b'{"v":1,"id":7,"error":"unknown op"}\n')

    def test_frame_size_boundary_and_oversize(self):
        frame = wire.encode_frame({'x': ''})
        limit = {'x': 'a' * (wire.MAX_FRAME - len(frame))}
        self.assertEqual(len(wire.encode_frame(limit)), 65536)
        self.assertEqual(wire.read_frame(io.BytesIO(wire.encode_frame(limit))), limit)
        with self.assertRaisesRegex(wire.WireError, '64 KiB'):
            wire.encode_frame({'x': limit['x'] + 'a'})
        self.start()
        self.assertIn('64 KiB', self.raw(b'a' * 65537 + b'\n')['error'])

    def test_bad_json(self):
        self.start()
        for frame in (b'{oops}\n', b'[]\n', b'{"v":1,"id":7,"op":"hello","pid":NaN}\n',
                      b'{"v":1,"id":7,"op":"hello","pid":Infinity}\n', b'\xff\n'):
            with self.subTest(frame=frame):
                self.assertIn('error', self.raw(frame))
        with self.assertRaisesRegex(wire.WireError, 'newline'):
            wire.read_frame(io.BytesIO(b'{}'))

    def test_bad_request_id(self):
        self.start()
        for value in (None, True, -1, 1.5, '7', 1 << 63):
            reply = self.raw(wire.encode_frame(dict(v=1, id=value, op='hello')))
            self.assertIn('id must', reply['error'])
            self.assertIsNone(reply['id'])

    def test_unknown_op(self):
        self.start()
        reply = self.raw(b'{"v":1,"id":7,"op":"unknown"}\n')
        self.assertEqual(reply, {'v': 1, 'id': 7, 'error': 'unknown op'})

    def test_hello_version_mismatch(self):
        self.start()
        for version in (2, True, '1', None):
            reply = self.raw(wire.encode_frame(dict(v=version, id=7, op='hello')))
            self.assertIn('protocol version mismatch', reply['error'])
            self.assertIn('restart trio-interposer', reply['error'])

    def test_hello_must_be_first(self):
        self.start()
        reply = self.raw(b'{"v":1,"id":7,"op":"list"}\n')
        self.assertEqual(reply['error'], 'first frame must be hello')

    def test_client_checks_reply_id(self):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(b'{"v":1,"id":99,"ok":{}}\n')
        with wire.Client(sock) as client:
            with self.assertRaisesRegex(wire.WireError, 'reply id'):
                client.call('hello')

    def test_client_checks_hello_range(self):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(
            b'{"v":1,"id":0,"ok":{"protocol_min":2,"protocol_max":2}}\n')
        with patch.object(wire.socket, 'socket', return_value=sock):
            with self.assertRaisesRegex(wire.WireError, 'hello protocol version mismatch'):
                wire.connect()
        sock.close.assert_called()

    def test_unimplemented_ops_and_identity_validation(self):
        self.start()
        for op in sorted(EXPECTED_OPS - {'hello', 'hub.announce', 'list', 'status'}):
            with wire.connect() as client:
                with self.assertRaisesRegex(wire.WireError, '^not implemented in this version$'):
                    client.call(op)
                self.assertEqual(client.call('list')['hubs'], [])
        for fields, error in (({'session': '../session'}, 'bad session'),
                              ({'key': 'not-a-key'}, 'bad identity'),
                              ({'session_token': 'synthetic-secret'}, 'credentials')):
            reply = self.raw(wire.encode_frame(dict(v=1, id=1, op='hello', **fields)))
            self.assertIn(error, reply['error'])

    def test_private_socket_and_directories(self):
        self.start()
        for path in (wire.socket_path().parent, wire.home() / 'run', wire.home() / 'events', wire.home() / 'logs'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(wire.socket_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual((wire.home() / 'events' / 'interposer.sqlite').stat().st_mode & 0o777, 0o600)
        self.assertEqual((wire.home() / 'logs' / 'interposer.log').stat().st_mode & 0o777, 0o600)

    def test_runtime_fallback_and_symlink_refusal(self):
        os.environ.pop('XDG_RUNTIME_DIR')
        self.assertEqual(wire.socket_path(), wire.home() / 'run' / 'interposer.sock')
        planted = self.root / 'planted'
        planted.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(PermissionError):
            wire.private_dir(planted)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux peer credentials')
    def test_foreign_uid_refused(self):
        peer = Mock()
        peer.getsockopt.return_value = struct.pack('iII', 42, os.getuid() + 1, 42)
        self.assertFalse(service.peer_allowed(peer))
        peer.getsockopt.return_value = struct.pack('iII', 42, os.getuid(), 42)
        self.assertTrue(service.peer_allowed(peer))
        peer.getsockopt.side_effect = OSError('synthetic')
        self.assertFalse(service.peer_allowed(peer))
        peer.getsockopt.side_effect = None
        high_uid = (1 << 31) + 7
        with patch.object(service.os,'getuid',return_value=high_uid):
            peer.getsockopt.return_value = struct.pack('iII',42,high_uid,high_uid)
            self.assertTrue(service.peer_allowed(peer))
            peer.getsockopt.return_value = struct.pack('iII',42,high_uid+1,high_uid)
            self.assertFalse(service.peer_allowed(peer))
        store = storage.Store()
        wire.private_dir(wire.socket_path().parent)
        server = service.Server(wire.socket_path(), store, logging.getLogger('test'))
        try:
            with patch.object(service, 'peer_allowed', return_value=False) as admission, \
                    patch.object(service.Handler, 'handle') as handler:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    sock.connect(str(wire.socket_path()))
                    sock.sendall(b'{"v":1,"id":7,"op":"hello"}\n')
                    server.handle_request()
                    try:
                        self.assertEqual(sock.recv(1), b'')
                    except ConnectionResetError:
                        pass
                admission.assert_called_once()
                handler.assert_not_called()
        finally:
            server.server_close()
            store.close()

    def test_single_instance_lease(self):
        first = self.start()
        second = subprocess.run([sys.executable, str(ROOT / 'server' / 'nth_interposer.py'), 'serve'],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(second.returncode, 75)
        self.assertIn('lease already held', second.stderr)
        with wire.connect() as client:
            self.assertEqual(client.hello['pid'], first.pid)

    def test_idle_exit(self):
        # Process smoke checks use a generous interval; fake-clock tests below
        # prove idle arithmetic without racing a briefly descheduled parent.
        process = self.start()
        process.terminate()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(wire.socket_path().exists())
        self.assertIn('service stopped', (wire.home() / 'logs' / 'interposer.log').read_text())

    def test_live_session_defers_idle_exit(self):
        store = storage.Store()
        with store.db:
            store.db.execute("INSERT INTO sessions(session,state) VALUES (?,'waiting')", (SESSION,))
        store.close()
        process = self.start()
        self.assertIsNone(process.poll())
        with wire.connect() as client:
            self.assertEqual(client.call('list')['sessions'][0]['state'], 'waiting')

    def test_spawn_fallback(self):
        original = wire._spawn
        def spawn():
            process = original()
            self.processes.append(process)
            return process
        with patch.object(wire, '_spawn', side_effect=spawn):
            with wire.connect(spawn=True) as client:
                pid = client.hello['pid']
            with wire.connect(spawn=True) as client:
                self.assertEqual(client.hello['pid'], pid)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue((wire.home() / 'run' / 'spawn.lock').exists())

    def test_concurrent_spawn_serialized(self):
        processes = []
        original = wire._spawn
        def spawn():
            process = original()
            processes.append(process)
            self.processes.append(process)
            return process
        def hello(_):
            with wire.connect(spawn=True) as client:
                return client.hello['pid']
        with patch.object(wire, '_spawn', side_effect=spawn):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                pids = list(pool.map(hello, range(4)))
        self.assertEqual(len(set(pids)), 1)
        self.assertEqual(len(processes), 1)

    def test_spawn_timeout_and_non_spawn_errors(self):
        with self.assertRaises(FileNotFoundError):
            wire.connect()
        with patch.object(wire, '_connect', side_effect=FileNotFoundError(2, 'synthetic')):
            process = Mock()
            process.poll.return_value = None
            clock = [0.0]
            def advance(seconds):
                clock[0] += seconds
            with patch.object(wire, '_spawn', return_value=process), \
                 patch.object(wire.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(wire.time,'sleep',side_effect=advance):
                began = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'connect timeout'):
                    wire.connect(spawn=True, timeout=.1)
                self.assertAlmostEqual(time.monotonic() - began, .1)
        with patch.object(wire, '_connect', side_effect=PermissionError(13, 'synthetic')):
            with patch.object(wire, '_spawn') as spawn:
                with self.assertRaises(PermissionError):
                    wire.connect(spawn=True)
                spawn.assert_not_called()

    def test_stale_socket_replaced_but_live_socket_preserved(self):
        wire.private_dir(wire.socket_path().parent)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(wire.socket_path()))
        self.start()
        with self.assertRaisesRegex(wire.WireError, 'already listening'):
            service.remove_stale_socket(wire.socket_path())
        path = self.root / 'regular'
        path.write_text('keep')
        with self.assertRaises(PermissionError):
            service.remove_stale_socket(path)
        self.assertEqual(path.read_text(), 'keep')

    def test_systemd_socket_activation(self):
        process = self.start(activated=True)
        self.assertEqual(wire.socket_path().stat().st_mode & 0o777, 0o600)
        process.terminate()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        # Systemd owns the pathname, including when the idle service exits.
        self.assertTrue(wire.socket_path().exists())

    def test_activation_environment_validation(self):
        sock = Mock(family=socket.AF_UNIX,type=socket.SOCK_STREAM)
        sock.getsockname.return_value = str(wire.socket_path())
        sock.getsockopt.return_value = 1
        sock.fileno.return_value = 3
        with patch.dict(os.environ, {'LISTEN_PID': str(os.getpid()), 'LISTEN_FDS': '2'}), \
             patch.object(service.socket,'socket',return_value=sock),patch.object(service.os,'set_inheritable'):
            with self.assertRaisesRegex(wire.WireError, 'one systemd'):
                service.activated_socket()
        with patch.dict(os.environ, {'LISTEN_PID': str(os.getpid() + 1), 'LISTEN_FDS': '1'}):
            self.assertIsNone(service.activated_socket())

    def test_store_schema_durability_and_version(self):
        store = storage.Store()
        try:
            tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'hubs', 'memberships', 'sessions', 'holdings', 'deliveries', 'appserver_spool', 'meta'})
            self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(store.db.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 2)
            for table, columns in {
                'memberships': {'key','source','url','channel','member_id','filter','enabled','ended',
                    'owner_session','announced_through','acked_through','poll_state','poll_error','last_ok'},
                'sessions': {'session','client','sink','host_pid','host_stamp','state','host_ok','problem',
                    'registered','last_wake','wakes_hour'},
                'hubs': {'server','url','announced_at','state','since','error','pending_url','trust'},
                'holdings': {'session','key','server','joined'},
                'deliveries': {'id','session','sink','body','ranges','state','created','done'},
                'appserver_spool': {'key','message_id','payload','state','turn_id'},
            }.items():
                self.assertEqual({row[1] for row in store.db.execute('PRAGMA table_info(' + table + ')')}, columns)
            store.db.execute('PRAGMA user_version=99')
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'schema version'):
            storage.Store()

    def test_importer_preserves_watermarks_stops_and_ended(self):
        store = storage.Store()
        try:
            self.assertTrue(store.import_hooks(FIXTURES))
            state = store.snapshot()
            member = state['memberships'][0]
            self.assertEqual((member['announced_through'], member['acked_through']), (43, 39))
            self.assertEqual((member['filter'], member['enabled'], member['ended']), ('at', 0, 'member removed'))
            self.assertEqual(len(state['holdings']), 2)
            self.assertEqual([row['state'] for row in state['sessions']], ['idle_unreachable', 'ended'])
            self.assertEqual(store.live_sessions(), 0)
            self.assertNotIn('synthetic-secret-never-import', json.dumps(state))
            self.assertNotIn(b'synthetic-secret-never-import', store.path.read_bytes())
        finally:
            store.close()

    def test_importer_idempotent_and_atomic(self):
        hooks = self.root / 'hooks'
        shutil.copytree(FIXTURES, hooks)
        store = storage.Store()
        try:
            self.assertTrue(store.import_hooks(hooks))
            before = store.snapshot()
            (hooks / ('membership-' + KEY + '.json')).write_text('{"filter":"all","enabled":true}')
            self.assertTrue(store.import_hooks(hooks))
            after = store.snapshot()
            self.assertEqual((after['memberships'][0]['filter'], after['memberships'][0]['enabled']), ('all', 1))
            self.assertEqual(after['memberships'][0]['ended'], '')
            self.assertEqual(after['holdings'], before['holdings'])
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(), [])
        finally:
            store.close()
        other = storage.Store(self.root / 'other' / 'interposer.sqlite')
        try:
            (hooks / ('session-' + SESSION + '.json')).write_text('{"high_water":{"' + KEY + '":-1}}')
            self.assertTrue(other.import_hooks(hooks))
            state = other.snapshot()
            self.assertEqual(state['legacy_import_skips'], [{'basename': 'session-' + SESSION + '.json',
                                                          'reason': 'invalid legacy state'}])
            self.assertEqual(len(state['sessions']), 1)
            self.assertNotEqual(state['sessions'][0]['session'], SESSION)
            self.assertFalse(any(row['session'] == SESSION for row in state['holdings']))
            shutil.copy(FIXTURES / ('session-' + SESSION + '.json'), hooks)
            self.assertTrue(other.import_hooks(hooks))
            self.assertEqual(other.snapshot()['legacy_import_skips'], [])
        finally:
            other.close()

    def test_hub_announce_list_status_and_concurrent_writes(self):
        self.start()
        with wire.connect() as client:
            self.assertEqual(client.call('list'), {'hubs': [], 'memberships': [], 'sessions': [],
                                                 'holdings': [], 'legacy_import_skips': []})
        def announce(number):
            with wire.connect() as client:
                return client.call('hub.announce', server='nth-demo', url='https://hub.example/sse')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(announce, range(8)))
        with wire.connect() as client:
            state = client.call('status', key=KEY, session=SESSION)
            self.assertEqual(len(state['hubs']), 1)
            self.assertEqual(state['hubs'][0]['state'], 'pending')
            self.assertEqual(state['hubs'][0]['url'], 'https://hub.example/sse')
            self.assertEqual(state['memberships'], [])
            self.assertEqual(state['sessions'], [])
        self.processes[0].terminate()
        self.processes[0].wait(timeout=3)
        self.start()
        with wire.connect() as client:
            self.assertEqual(len(client.call('list')['hubs']), 1)

    def test_status_projects_and_filters_stored_state(self):
        store = storage.Store()
        try:
            store.import_hooks(FIXTURES)
            with store.db:
                store.db.execute("INSERT INTO deliveries(session,body) VALUES (?,'synthetic-peer')", (SESSION,))
            state = store.snapshot(key=KEY, session=SESSION)
            self.assertEqual(len(state['sessions']), 1)
            self.assertEqual(len(state['holdings']), 1)
            self.assertEqual(state['memberships'][0]['key'], KEY)
            self.assertEqual(store.snapshot(key='f' * 24)['memberships'], [])
            self.assertNotIn('synthetic-peer', json.dumps(state))
        finally:
            store.close()

    def test_credentials_and_bad_hubs_never_logged(self):
        self.start()
        marker = 'synthetic-secret-marker'
        for fields in ({'session_token': marker}, {'token': marker}, {'authkey': marker},
                       {'op': 'hub.announce', 'server': 'nth-demo', 'url': 'https://u:' + marker + '@hub.example/sse'},
                       {'op': 'hub.announce', 'server': 'nth-demo', 'url': 'https://hub.example/sse?token=' + marker},
                       {'op': 'hub.announce', 'server': '../bad', 'url': 'https://hub.example/sse'}):
            request = dict(v=1, id=7, op='hello')
            request.update(fields)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(str(wire.socket_path()))
                sock.sendall(b'{"v":1,"id":0,"op":"hello"}\n')
                with sock.makefile('rb') as reader:
                    self.assertIn('ok', wire.read_frame(reader))
                    sock.sendall(wire.encode_frame(request))
                    reply = wire.read_frame(reader)
                    expected = ('credentials must not cross' if any(k in fields for k in ('token','session_token','authkey'))
                                else 'bad hub server name' if fields.get('server') == '../bad' else 'bad hub URL')
                    self.assertIn(expected, reply['error'])
                    self.assertEqual(reply['id'], 7)
                    sock.sendall(b'{"v":1,"id":8,"op":"list"}\n')
                    self.assertEqual(wire.read_frame(reader)['ok']['hubs'], [])
        log = (wire.home() / 'logs' / 'interposer.log').read_text()
        self.assertNotIn(marker, log)
        self.assertNotIn(marker, (wire.home() / 'events' / 'interposer.sqlite').read_bytes().decode(errors='replace'))

    def test_setup_units_and_running_restart(self):
        self.fake_systemctl()
        self.assertTrue(setup.install_interposer_units(self.root, '/venv/bin/python', '/install/server', '/runtime'))
        directory = self.root / '.config' / 'systemd' / 'user'
        socket_unit = (directory / 'trio-interposer.socket').read_text()
        for line in ('ListenStream="' + str(wire.socket_path()) + '"', 'SocketMode=0600', 'DirectoryMode=0700',
                     'WantedBy=sockets.target', 'RemoveOnStop=yes'):
            self.assertIn(line, socket_unit)
        unit = (directory / 'trio-interposer.service').read_text()
        for line in ('ExecStart="/venv/bin/python" "/install/server/nth_interposer.py" serve',
                     'Environment="NTH_HOME=/runtime"', 'Restart=on-failure', 'RestartSec=2',
                     'NoNewPrivileges=yes', 'UMask=0077', 'LockPersonality=yes', 'RestrictRealtime=yes',
                     'RestartPreventExitStatus=75'):
            self.assertIn(line, unit)
        self.assertNotIn('PrivateTmp=', unit)
        self.assertEqual(self.calls(), [['--user', 'show-environment'], ['--user', 'daemon-reload'],
            ['--user', 'enable', '--now', 'trio-interposer.socket'],
            ['--user', 'is-active', '--quiet', 'trio-interposer.service'],
            ['--user', 'restart', 'trio-interposer.service']])
        setup.install_interposer_units(self.root, '/new python', '/install/server', '/runtime')
        self.assertTrue(list(directory.glob('trio-interposer.service.bak-*')))
        self.assertEqual(setup._unit_quote('a%"b\\c'), '"a%%\\"b\\\\c"')

    def test_setup_gates_systemd_and_does_not_start_inactive_service(self):
        self.fake_systemctl(running=False)
        self.assertFalse(setup.install_interposer_units(self.root, '/python', '/server', '/runtime', platform='darwin'))
        self.assertFalse(self.command_log.exists())
        self.assertFalse(setup.install_interposer_units(self.root / 'staged', '/python', '/server', '/runtime'))
        self.assertFalse(self.command_log.exists())
        self.assertTrue(setup.install_interposer_units(self.root, '/python', '/server', '/runtime'))
        self.assertNotIn(['--user', 'restart', 'trio-interposer.service'], self.calls())
        self.command_log.unlink()
        os.environ['SYSTEMCTL_AVAILABLE'] = '1'
        self.assertEqual(setup.install_interposer_units(self.root, '/python', '/server', '/runtime'),
                         'failed: CalledProcessError')
        self.assertEqual(self.calls(), [['--user', 'show-environment']])

    def test_setup_install_skip_flag(self):
        self.fake_systemctl()
        result = setup.install(self.root, clients=(), skip_dependencies=True, skip_systemd=True)
        self.assertFalse(self.command_log.exists())
        self.assertTrue((Path(result['server']) / 'nth_interposer.py').exists())
        # Exercise argparse too, with only a temporary HOME and fake systemctl.
        completed = subprocess.run([sys.executable, str(ROOT / 'setup.py'), 'install', '--clients', 'claude',
                                   '--skip-dependencies', '--skip-systemd'], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(self.command_log.exists())

    def test_cli_status_and_absent_service(self):
        result = subprocess.run([sys.executable, str(ROOT / 'server' / 'nth_cli.py'), 'interposer', 'status'],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Interposer unavailable', result.stderr)
        process = self.start()
        result = subprocess.run([sys.executable, str(ROOT / 'server' / 'nth_cli.py'), 'interposer', 'status'],
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['hello']['pid'], process.pid)
        self.assertEqual(state['hubs'], [])

    def test_cli_restart_and_logs(self):
        first = self.start()
        # Capture the new fallback child to reap it within this test.
        original = wire._spawn
        def spawn():
            process = original()
            self.processes.append(process)
            return process
        output = io.StringIO()
        with patch.object(wire, '_spawn', side_effect=spawn), patch('sys.stdout', output):
            self.assertEqual(cli.main(['interposer', 'restart']), 0)
        self.assertTrue(json.loads(output.getvalue())['restarted'])
        self.assertNotEqual(json.loads(output.getvalue())['hello']['pid'], first.pid)
        first.wait(timeout=3)
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(cli.main(['interposer', 'logs']), 0)
        self.assertIn('service started', output.getvalue())

    def test_doctor_checks_socket_and_hello(self):
        self.assertEqual(doctor.interposer_check()[1], doctor.WARN)
        self.start()
        row = doctor.interposer_check()
        self.assertEqual(row[1], doctor.OK)
        self.assertIn('hello answered', row[2])
        with patch.object(wire, 'connect', side_effect=wire.WireError('synthetic')):
            self.assertEqual(doctor.interposer_check()[1], doctor.FAIL)
        self.assertIn('interposer_check()', (ROOT / 'server' / 'nth_doctor.py').read_text().split('def run_checks')[1])


if __name__ == '__main__':
    unittest.main()
