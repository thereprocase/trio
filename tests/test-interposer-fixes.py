"""Review regressions with isolated processes, explicit contracts and fake clocks."""
from contextlib import contextmanager
import errno
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('interposer_cases', ROOT / 'tests' / 'test-interposer-skeleton.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
wire, storage, service, setup, cli, doctor = cases.wire, cases.storage, cases.service, cases.setup, cases.cli, cases.doctor
KEY, SESSION, FIXTURES = cases.KEY, cases.SESSION, cases.FIXTURES
OTHER_KEY = 'abcdef0123456789abcdef01'
OTHER_SESSION = '22222222-3333-4444-8555-666666666666'


class FixTests(cases.InterposerCase):
    @contextmanager
    def server(self, log=None):
        store = storage.Store()
        wire.private_dir(wire.socket_path().parent)
        server = service.Server(wire.socket_path(), store, log or Mock())
        try:
            yield server, store
        finally:
            server.server_close()
            store.close()

    @contextmanager
    def raw_client(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(str(wire.socket_path()))
            with sock.makefile('rb') as reader:
                sock.sendall(b'{"v":1,"id":0,"op":"hello"}\n')
                self.assertIn('ok', wire.read_frame(reader))
                yield sock, reader

    def hooks(self):
        import shutil
        directory = wire.private_dir(wire.home() / 'events') / 'hooks'
        shutil.copytree(FIXTURES, directory)
        return directory

    def test_reimport_legacy_wins_and_maximum_marks(self):
        hooks = self.hooks()
        store = storage.Store()
        try:
            with store.db:
                store.db.execute('INSERT INTO memberships(key,announced_through,acked_through) VALUES (?,60,55)', (KEY,))
                store.db.execute("INSERT INTO meta VALUES ('hooks_imported','1')")
            self.assertTrue(store.import_hooks())
            self.assertEqual((store.snapshot()['memberships'][0]['announced_through'],
                              store.snapshot()['memberships'][0]['acked_through']), (60,55))
            config = hooks / ('membership-' + KEY + '.json')
            for mode, enabled, ended in (('all',True,''), ('about',False,'channel ended'), ('at',True,'refused')):
                config.write_text(json.dumps(dict(filter=mode, enabled=enabled, ended=ended)))
                store.import_hooks()
                member = store.snapshot()['memberships'][0]
                self.assertEqual((member['filter'],bool(member['enabled']),member['ended']), (mode,enabled,ended))
                self.assertEqual((member['announced_through'],member['acked_through']), (60,55))
            (hooks / ('session-' + OTHER_SESSION + '.json')).write_text(json.dumps({
                'memberships': {}, 'high_water': {KEY: 1, OTHER_KEY: 7}, 'acked': {KEY: 2}, 'ended': False}))
            store.import_hooks()
            member = store.snapshot(key=KEY)['memberships'][0]
            self.assertEqual((member['announced_through'],member['acked_through']), (60,55))
            mark_only = store.snapshot(key=OTHER_KEY)['memberships'][0]
            self.assertEqual((mark_only['announced_through'],mark_only['acked_through']), (7,0))
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(), [])
            store.close()
            store = storage.Store()
            store.import_hooks()
            self.assertEqual(store.snapshot(key=KEY)['memberships'][0]['announced_through'], 60)
            self.assertEqual(len(store.snapshot()['holdings']), 2)
        finally:
            store.close()

    def test_missing_hooks_and_future_cutover_marker(self):
        store = storage.Store()
        try:
            self.assertFalse(store.import_hooks())
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(), [])
            self.hooks()
            with store.db:
                store.db.execute('INSERT INTO meta VALUES (?,?)', ('hooks_import_cutover','1'))
            self.assertFalse(store.import_hooks())
            self.assertEqual(store.snapshot()['memberships'], [])
        finally:
            store.close()

    def test_malformed_legacy_files_isolated_reported_and_retryable(self):
        hooks = self.hooks()
        bad_session = hooks / ('session-' + OTHER_SESSION + '.json')
        bad_config = hooks / ('membership-' + OTHER_KEY + '.json')
        invalid_states = [{'memberships': []}, {'high_water': []}, {'acked': None}, {'servers': []},
            {'joined': []}, {'client': []}, {'ended': 'yes'},
            {'memberships': {OTHER_KEY: []}}, {'memberships': {OTHER_KEY: {'source': 1}}},
            {'memberships': {OTHER_KEY: {}}, 'joined': {OTHER_KEY: 'later'}},
            {'memberships': {OTHER_KEY: {}}, 'servers': {OTHER_KEY: False}},
            *[{'high_water': {OTHER_KEY: value}} for value in (-1,True,1.5,'7',1 << 63)],
            {'high_water': {'short': 1}}, [], None]
        store = storage.Store()
        logger = Mock()
        try:
            store.import_hooks(log=logger)
            def database_rows():
                return {row[0]:[tuple(value) for value in store.db.execute('SELECT * FROM '+row[0]+' ORDER BY rowid')]
                        for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
            baseline = database_rows()
            for state in invalid_states:
                with self.subTest(state=state):
                    bad_session.write_text(json.dumps(state))
                    before = bad_session.read_bytes()
                    store.import_hooks(log=logger)
                    skipped = store.snapshot()['legacy_import_skips']
                    self.assertEqual(skipped, [{'basename': bad_session.name, 'reason': 'invalid legacy state'}])
                    self.assertEqual(bad_session.read_bytes(), before)
                    self.assertFalse(any(row['session'] == OTHER_SESSION for row in store.snapshot()['sessions']))
                    self.assertFalse(any(row['session'] == OTHER_SESSION for row in store.snapshot()['holdings']))
                    self.assertEqual(database_rows(),baseline)
            for config in ({'filter': []}, {'filter': 'loud'}, {'enabled': 1}, {'ended': False}, []):
                bad_config.write_text(json.dumps(config))
                store.import_hooks(log=logger)
                self.assertEqual(len(store.snapshot()['legacy_import_skips']), 2)
                self.assertEqual(store.snapshot(key=OTHER_KEY)['memberships'], [])
            bad_session.write_text('{"synthetic-secret": broken')
            store.import_hooks(log=logger)
            self.assertNotIn('synthetic-secret', str(logger.mock_calls))
            bad_session.write_text('{}')
            bad_config.write_text('{"filter":"at","enabled":false,"ended":""}')
            store.import_hooks(log=logger)
            self.assertEqual(store.snapshot()['legacy_import_skips'], [])
            self.assertEqual(store.snapshot(key=OTHER_KEY)['memberships'][0]['enabled'], 0)
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(), [])
        finally:
            store.close()

    def test_startup_import_skips_bad_file_and_doctor_reports_it(self):
        hooks = self.hooks()
        bad = hooks / ('session-' + OTHER_SESSION + '.json')
        bad.write_text('{"memberships":[]}')
        before = {p.name:p.read_bytes() for p in hooks.iterdir()}
        first = self.start()
        with wire.connect() as client:
            state = client.call('list')
            self.assertEqual(state['memberships'][0]['filter'], 'at')
            self.assertEqual(state['memberships'][0]['enabled'], 0)
            self.assertEqual(state['legacy_import_skips'], [{'basename':bad.name,'reason':'invalid legacy state'}])
        self.assertIn(bad.name, doctor.interposer_check()[2])
        log = (wire.home() / 'logs' / 'interposer.log').read_text()
        self.assertIn(bad.name, log)
        self.assertNotIn('memberships', log)
        first.terminate(); first.wait(timeout=5)
        self.start()
        with wire.connect() as client:
            self.assertEqual(client.call('list')['memberships'][0]['announced_through'], 43)
        self.assertEqual({p.name:p.read_bytes() for p in hooks.iterdir()}, before)

    def test_periodic_import_at_sixty_seconds(self):
        now = [0]
        stop = threading.Event()
        store = Mock()
        store.live_sessions.return_value = 1
        server = Mock()
        def tick():
            now[0] += 30
            if now[0] >= 150:
                stop.set()
        server.handle_request.side_effect = tick
        with patch.object(service,'Store',return_value=store), patch.object(service,'Server',return_value=server), \
             patch.object(service.time,'monotonic',side_effect=lambda:now[0]):
            service.serve(stop=stop)
        self.assertEqual(storage.HOOKS_IMPORT_INTERVAL, 60)
        self.assertEqual(store.import_hooks.call_count, 3)  # start, 60, 120

    def test_empty_import_metadata_does_not_erase_existing_values(self):
        hooks = self.hooks()
        store = storage.Store()
        try:
            store.import_hooks()
            path = hooks / 'session-zzzzzz-later.json'
            path.write_text(json.dumps({'high_water':{KEY:1}, 'acked':{OTHER_KEY:9}}))
            store.import_hooks()
            row = store.snapshot(key=KEY)['memberships'][0]
            self.assertEqual((row['source'],row['channel'],row['member_id']), ('quartet','room','member-1'))
            row = store.snapshot(key=OTHER_KEY)['memberships'][0]
            self.assertEqual((row['announced_through'],row['acked_through']), (0,9))
        finally:
            store.close()

    def test_all_database_tables_and_wal_are_token_free(self):
        hooks = self.hooks()
        markers = ['synthetic-secret-never-import','synthetic-secret-final-session','synthetic-nested-secret']
        for path in hooks.glob('session-*.json'):
            state = json.loads(path.read_text())
            state['session_token'] = markers[2]
            state['extra'] = {'authkey':markers[2]}
            state['memberships'][KEY]['extra'] = {'token':markers[2]}
            path.write_text(json.dumps(state))
        before = {p.name:p.read_bytes() for p in hooks.iterdir()}
        store = storage.Store()
        try:
            store.import_hooks()
            for (table,) in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                values = repr([tuple(row) for row in store.db.execute('SELECT * FROM ' + table)])
                for marker in markers:
                    self.assertNotIn(marker, values, table)
            for suffix in ('','-wal','-shm'):
                path = Path(str(store.path) + suffix)
                if path.exists():
                    for marker in markers:
                        self.assertNotIn(marker.encode(), path.read_bytes(), suffix)
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            for marker in markers:
                self.assertNotIn(marker.encode(), store.path.read_bytes())
        finally:
            store.close()
        self.assertEqual({p.name:p.read_bytes() for p in hooks.iterdir()}, before)

    def test_replacement_socket_survives_fallback_shutdown(self):
        process = self.start()
        old = wire.socket_path().lstat()
        wire.socket_path().unlink()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as replacement:
            replacement.bind(str(wire.socket_path()))
            replacement.listen()
            info = wire.socket_path().lstat()
            self.assertNotEqual((old.st_dev,old.st_ino),(info.st_dev,info.st_ino))
            process.terminate(); process.wait(timeout=5)
            self.assertEqual(wire.socket_path().lstat().st_ino, info.st_ino)

    def test_setup_stops_verified_fallback_before_enabling_socket(self):
        process = self.start()
        self.fake_systemctl(running=False)
        real_run = setup.subprocess.run
        def run(arguments, **kwargs):
            if 'enable' in arguments:
                self.assertFalse(wire.service_process(process.pid))
                self.assertFalse(wire.socket_path().exists())
            return real_run(arguments,**kwargs)
        with patch.object(setup.subprocess,'run',side_effect=run):
            self.assertTrue(setup.install_interposer_units(self.root,sys.executable,ROOT / 'server',wire.home()))
        process.wait(timeout=5)

    def test_restart_rejects_unverified_pid(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.hello = {'pid':4242,'activation':'fallback'}
        with patch.object(wire,'connect',return_value=client), patch.object(wire,'service_process',return_value=False), \
             patch.object(wire.os,'kill') as kill, patch('sys.stderr',io.StringIO()):
            self.assertEqual(cli.main(['interposer','restart']),1)
            kill.assert_not_called()

    def test_runtime_selection_branches_and_length(self):
        canonical = self.root / 'canonical'
        canonical.mkdir(mode=0o700)
        with patch.object(wire,'_user_runtime_dir',return_value=canonical):
            self.assertEqual(wire.socket_path(),self.runtime / 'trio' / 'interposer.sock')
            for value in ('relative',str(self.root / 'absent')):
                os.environ['XDG_RUNTIME_DIR'] = value
                self.assertEqual(wire.socket_path(),canonical / 'trio' / 'interposer.sock')
            os.environ['XDG_RUNTIME_DIR'] = str(self.runtime)
            self.runtime.chmod(0o777)
            self.assertEqual(wire.socket_path(),canonical / 'trio' / 'interposer.sock')
            self.runtime.chmod(0o755)
            self.assertEqual(wire.socket_path(),self.runtime / 'trio' / 'interposer.sock')
            os.environ.pop('XDG_RUNTIME_DIR')
            for mode in (0o755,0o770):
                canonical.chmod(mode)
                self.assertEqual(wire.socket_path(),wire.home() / 'run' / 'interposer.sock')
            metadata = Mock(st_mode=stat.S_IFDIR | 0o700,st_uid=os.getuid()+1)
            with patch.object(Path,'lstat',return_value=metadata):
                self.assertEqual(wire.socket_path(),wire.home() / 'run' / 'interposer.sock')
        os.environ['NTH_HOME'] = str(self.root / ('x'*100))
        with self.assertRaisesRegex(wire.WireError,'under 104 bytes'):
            wire.socket_path()

    def test_doctor_stale_socket_never_spawns_and_missing_unit_wording(self):
        wire.private_dir(wire.socket_path().parent)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.bind(str(wire.socket_path()))
        with patch.object(wire,'_spawn') as spawn:
            self.assertEqual(doctor.interposer_check()[1],doctor.FAIL)
            spawn.assert_not_called()
        wire.socket_path().unlink()
        unit = self.root / '.config/systemd/user/trio-interposer.socket'
        unit.parent.mkdir(parents=True)
        unit.write_text('[Socket]\n')
        self.assertEqual(doctor.interposer_check()[2],
                         'socket unit stopped or failed; see trio interposer logs')

    def test_connection_limit_and_release(self):
        self.assertEqual(service.MAX_CONNECTIONS,32)
        sockets = []
        with self.server() as (server,store):
            try:
                for _ in range(32):
                    sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                    sock.settimeout(1)
                    sockets.append(sock)
                    sock.connect(str(wire.socket_path()))
                    server.handle_request()
                extra = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                extra.settimeout(1)
                sockets.append(extra)
                extra.connect(str(wire.socket_path()))
                server.handle_request()
                self.assertEqual(extra.recv(1),b'')
                sockets[0].close()
                deadline = time.monotonic()+3
                while len(server.connections) == 32 and time.monotonic()<deadline:
                    threading.Event().wait(.01)
                self.assertLess(len(server.connections),32)
                replacement = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
                replacement.settimeout(1)
                sockets.append(replacement)
                replacement.connect(str(wire.socket_path()))
                replacement.sendall(b'{"v":1,"id":7,"op":"hello"}\n')
                server.handle_request()
                with replacement.makefile('rb') as reader:
                    self.assertIn('ok',wire.read_frame(reader))
            finally:
                for sock in sockets:
                    sock.close()

    def test_frame_deadline_cannot_be_extended_by_dripping_bytes(self):
        sock = Mock()
        now = [0]
        def receive(limit):
            now[0] += 6
            return b'a'
        sock.recv.side_effect = receive
        self.assertEqual(service.FRAME_TIMEOUT,10)
        with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]):
            reader = wire.SocketFrameReader(sock)
            with self.assertRaisesRegex(TimeoutError,'deadline'):
                wire.read_frame(reader)
        self.assertEqual(sock.recv.call_count,2)
        self.assertEqual([call.args[0] for call in sock.settimeout.call_args_list],[10,4])
        # Even the terminating newline cannot make a late frame acceptable.
        now[0] = 0
        def late_complete(limit):
            now[0] = 11
            return b'{}\n'
        sock.recv.side_effect = late_complete
        with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]):
            with self.assertRaisesRegex(TimeoutError,'deadline'):
                wire.read_frame(wire.SocketFrameReader(sock))

    def test_timeout_logs_are_rate_limited_and_recover(self):
        logger = Mock()
        with self.server(log=logger) as (server,store):
            with patch.object(service.time,'monotonic',side_effect=[0,0,1,59,60]):
                for _ in range(5):
                    server.timeout_notice()
            self.assertEqual(logger.warning.call_count,2)
            logger.warning.assert_called_with('frame read timed out')

    def test_log_rotation_keeps_one_private_backup(self):
        with service.service_log() as log:
            for _ in range(2300):
                log.info('synthetic padding %s','x'*1000)
        directory = wire.home() / 'logs'
        self.assertEqual({p.name for p in directory.iterdir()},{'interposer.log','interposer.log.1'})
        for path in directory.iterdir():
            self.assertLessEqual(path.stat().st_size,1024*1024)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)

    def test_hub_repoint_pending_and_table_url_caps(self):
        store = storage.Store()
        try:
            first = store.announce('nth-demo','https://hub.example/sse')
            self.assertEqual((first['url'],first['trust'],first['state']),
                             ('https://hub.example/sse','announced','pending'))
            changed = store.announce('nth-demo','https://other.example/sse')
            self.assertEqual(changed['url'],first['url'])
            self.assertEqual(changed['pending_url'],'https://other.example/sse')
            self.assertEqual(store.snapshot()['hubs'][0],changed)
            for number in range(31):
                store.announce('nth-demo-' + str(number),'https://hub.example/sse')
            with self.assertRaisesRegex(wire.WireError,'maximum 32'):
                store.announce('nth-overflow','https://hub.example/sse')
            self.assertEqual(store.announce('nth-demo',first['url'])['url'],first['url'])
            self.assertEqual(len(store.snapshot()['hubs']),32)
            accepted = 'https://hub.example/' + 'x'*(512-len('https://hub.example/'))
            wire.validate_hub('nth-demo',accepted)
            with self.assertRaisesRegex(wire.WireError,'bad hub URL'):
                store.announce('nth-demo',accepted+'x')
        finally:
            store.close()

    def test_bad_hubs_rejected_after_hello_for_specific_reasons(self):
        self.start()
        urls = ['https://u:p@hub.example/sse','https://hub.example/sse?a=b','https://hub.example/sse#part',
                'ftp://hub.example/sse','https:///sse','https://hub.example:bad/sse','https://hub.example:70000/sse',
                'https://hub.example/\npath','http://localhost/sse','http://x.localhost/sse',
                'http://127.0.0.1/sse','http://127.1/sse','http://2130706433/sse','http://[::1]/sse',
                'http://[::ffff:127.0.0.1]/sse','http://169.254.169.254/sse','http://[fe80::1]/sse',
                'http://metadata.google.internal/sse','http://instance-data.ec2.internal/sse',
                'http://100.100.100.200/sse','http://[fd00:ec2::254]/sse',
                'http://[::ffff:100.100.100.200]/sse']
        with self.raw_client() as (sock,reader):
            for request_id,url in enumerate(urls,1):
                with self.subTest(url=url):
                    sock.sendall(wire.encode_frame(dict(v=1,id=request_id,op='hub.announce',server='nth-demo',url=url)))
                    reply = wire.read_frame(reader)
                    self.assertEqual(reply['id'],request_id)
                    self.assertTrue(reply['error'].startswith(('bad hub URL','loopback, link-local')))
            sock.sendall(wire.encode_frame(dict(v=1,id=100,op='hub.announce',server='nth-demo',url='https://hub.example/sse')))
            self.assertEqual(wire.read_frame(reader)['ok']['url'],'https://hub.example/sse')
            sock.sendall(b'{"v":1,"id":101,"op":"list"}\n')
            self.assertEqual(len(wire.read_frame(reader)['ok']['hubs']),1)

    def test_systemd_install_failures_warn_and_finish_json_result(self):
        self.fake_systemctl()
        real_run = setup.subprocess.run
        for failure in (subprocess.CalledProcessError(1,['systemctl']),subprocess.TimeoutExpired(['systemctl'],15)):
            def run(arguments,**kwargs):
                if 'daemon-reload' in arguments:
                    raise failure
                return real_run(arguments,**kwargs)
            output,error = io.StringIO(),io.StringIO()
            with patch.object(setup.subprocess,'run',side_effect=run), patch('sys.stdout',output), \
                 patch('sys.stderr',error), patch.object(sys,'argv',['setup.py','install','--clients','claude','--skip-dependencies']):
                setup.main()
            result = json.loads(output.getvalue())
            self.assertEqual(result['interposer_systemd'],'failed: '+type(failure).__name__)
            self.assertTrue(Path(result['launcher']).exists())
            self.assertIn('Warning: interposer systemd setup failed',error.getvalue())

    def test_units_unchanged_skip_backups_quote_and_hardening(self):
        self.fake_systemctl()
        self.assertTrue(setup.install_interposer_units(self.root,sys.executable,ROOT / 'server',wire.home()))
        directory = self.root / '.config/systemd/user'
        before = {p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in directory.iterdir()}
        self.assertTrue(setup.install_interposer_units(self.root,sys.executable,ROOT / 'server',wire.home()))
        self.assertEqual({p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in directory.iterdir()},before)
        unit = (directory / 'trio-interposer.service').read_text()
        for option in ('UMask=0077','LockPersonality=yes','RestrictRealtime=yes','RestartPreventExitStatus=75'):
            self.assertIn(option,unit)
        self.assertNotIn('PrivateTmp=',unit)
        self.assertEqual(setup._unit_quote('a$b%"c\\d'),'"a$$b%%\\"c\\\\d"')
        for value in ('bad\npath','bad\rpath','bad\x00path','bad\x7fpath'):
            with self.assertRaisesRegex(ValueError,'control'):
                setup._unit_quote(value)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)

    def test_normal_install_and_staged_home_gating(self):
        self.fake_systemctl()
        result = setup.install(self.root,clients=(),skip_dependencies=True)
        self.assertIs(result['interposer_systemd'],True)
        self.assertTrue((self.root / '.config/systemd/user/trio-interposer.socket').exists())
        self.assertIn(['--user','enable','--now','trio-interposer.socket'],self.calls())
        self.command_log.unlink()
        setup.install(self.root / 'staged',clients=(),skip_dependencies=True)
        self.assertFalse(self.command_log.exists())

    def test_cli_systemd_restart_and_log_tail(self):
        self.fake_systemctl()
        unit = self.root / '.config/systemd/user/trio-interposer.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('[Service]\n')
        self.start()
        with patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','restart']),0)
        self.assertEqual(self.calls(),[['--user','restart','trio-interposer.service']])
        log = wire.home() / 'logs/interposer.log'
        log.unlink()
        out = io.StringIO()
        with patch('sys.stdout',out):
            self.assertEqual(cli.main(['interposer','logs']),0)
        self.assertEqual(out.getvalue(),'No interposer log yet.\n')
        log.write_text(''.join(f'line {n}\n' for n in range(130)))
        out = io.StringIO()
        with patch('sys.stdout',out):
            self.assertEqual(cli.main(['interposer','logs']),0)
        self.assertEqual(out.getvalue(),''.join(f'line {n}\n' for n in range(30,130)))

    def test_doctor_run_checks_and_render_integration(self):
        self.start()
        with patch.object(doctor,'_read_registration',return_value=(None,None)), \
             patch.object(doctor,'_installed_version',return_value=(None,None)), \
             patch.object(doctor,'_freshness_check',return_value=None), \
             patch.object(doctor,'_http_json',return_value=(None,None,'synthetic')), \
             patch.object(doctor,'INSTALL_DIR',self.root / 'install'), \
             patch.object(doctor,'HUB_INSTALL_DIR',self.root / 'hub'), \
             patch.object(doctor,'DB_PATH',self.root / 'empty.db'), \
             patch.object(doctor.socket,'gethostname',return_value='node-example'):
            checks,fleet = doctor.run_checks()
            row = next(row for row in checks if row[0]=='interposer')
            self.assertEqual(row[1],doctor.OK)
            rendered = doctor.render(checks,fleet,color=False)
            self.assertIn('hello answered',str(rendered))

    def test_spawn_isolation_and_reaper(self):
        for field in ('LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES','PYTHONPATH','PYTHONHOME'):
            os.environ[field] = 'synthetic-untrusted'
        process = Mock()
        with patch.object(wire.subprocess,'Popen',return_value=process) as popen, \
             patch.object(wire.threading,'Thread') as thread:
            self.assertIs(wire._spawn(),process)
        arguments,options = popen.call_args.args[0],popen.call_args.kwargs
        self.assertEqual(arguments[1],'-I')
        self.assertEqual(arguments[-1],'serve')
        self.assertEqual(options['cwd'],wire.home() / 'run')
        self.assertTrue(options['close_fds'])
        self.assertTrue(options['start_new_session'])
        for field in ('LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES','PYTHONPATH','PYTHONHOME'):
            self.assertNotIn(field,options['env'])
        self.assertEqual(thread.call_args.kwargs['target'],process.wait)
        thread.return_value.start.assert_called_once()

    def test_spawn_child_is_actually_reaped(self):
        process = wire._spawn()
        self.processes.append(process)
        # Wait for its hello before termination; the reaper owns waitpid.
        deadline = time.monotonic()+10
        while True:
            try:
                with wire.connect(timeout=.5):
                    break
            except (FileNotFoundError,ConnectionRefusedError):
                self.assertLess(time.monotonic(),deadline)
                threading.Event().wait(.01)
        process.terminate()
        deadline = time.monotonic()+5
        while process.returncode is None and time.monotonic()<deadline:
            threading.Event().wait(.01)
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid,os.WNOHANG)

    def test_spawn_failure_branches_and_lock_timeout(self):
        with patch.object(wire,'_connect',side_effect=FileNotFoundError(2,'synthetic')), \
             patch.object(wire,'_spawn',return_value=Mock(poll=Mock(return_value=1))):
            with self.assertRaisesRegex(wire.WireError,'failed to start'):
                wire.connect(spawn=True,timeout=.2)
        with patch.object(wire,'_connect',side_effect=wire.WireError('bad hello')), \
             patch.object(wire,'_spawn') as spawn:
            with self.assertRaisesRegex(wire.WireError,'bad hello'):
                wire.connect(spawn=True)
            spawn.assert_not_called()
        with wire.file_lock(wire.run_dir() / 'spawn.lock'):
            clock = [0.0]
            def advance(seconds):
                clock[0] += seconds
            with patch.object(wire.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(wire.time,'sleep',side_effect=advance):
                began = time.monotonic()
                with self.assertRaisesRegex(TimeoutError,'lock is held'):
                    wire.connect(spawn=True,timeout=.1)
                self.assertAlmostEqual(time.monotonic()-began,.1)

    def test_stale_socket_spawn_fallback(self):
        wire.private_dir(wire.socket_path().parent)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.bind(str(wire.socket_path()))
        original = wire._spawn
        def spawn():
            process = original()
            self.processes.append(process)
            return process
        with patch.object(wire,'_spawn',side_effect=spawn):
            with wire.connect(spawn=True) as client:
                self.assertEqual(client.hello['activation'],'fallback')

    def test_frame_buffer_bound_fragmentation_eof_and_nonfinite_json(self):
        reader = Mock()
        reader.readline.return_value = b'{"v":1,"id":7,"op":"hello"}\n'
        wire.read_frame(reader)
        reader.readline.assert_called_once_with(65537)
        sock = Mock()
        sock.recv.side_effect = [b'{"v":',b'1,"id":7,"op":"hello"}\n{"x":2}\n',b'']
        buffered = wire.SocketFrameReader(sock)
        self.assertEqual(wire.read_frame(buffered)['op'],'hello')
        self.assertEqual(wire.read_frame(buffered),{'x':2})
        with self.assertRaises(EOFError):
            wire.read_frame(buffered)
        for number in ('NaN','Infinity','-Infinity'):
            with self.assertRaisesRegex(wire.WireError,'bad JSON'):
                wire.read_frame(io.BytesIO(('{"v":1,"id":7,"op":"hello","pid":'+number+'}\n').encode()))
        with self.assertRaisesRegex(wire.WireError,'newline'):
            wire.read_frame(io.BytesIO(b'{"v":1'))

    def test_unterminated_oversize_refuses_and_framing_errors_close(self):
        self.start()
        for frame in (b'x'*65537,b'{broken}\n'):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(str(wire.socket_path()))
                sock.sendall(frame+b'\n'+b'{"v":1,"id":7,"op":"hello"}\n'+wire.encode_frame({
                    'v':1,'id':8,'op':'hub.announce','server':'nth-should-not-execute','url':'https://hub.example/sse'}))
                with sock.makefile('rb') as reader:
                    reply = wire.read_frame(reader)
                    self.assertEqual(reply,{'v':1,'id':None,'error':
                        'frame exceeds 64 KiB' if len(frame)>65536 else 'bad JSON'})
                    try:
                        with self.assertRaises(EOFError):
                            wire.read_frame(reader)
                    except ConnectionResetError:
                        pass
        with wire.connect() as client:
            self.assertEqual(client.call('list')['hubs'],[])

    def test_exact_frame_and_response_boundaries(self):
        self.start()
        request = {'v':1,'id':7,'op':'hello','padding':''}
        size = len(wire.encode_frame(request))
        request['padding'] = 'x'*(65536-size)
        accepted = wire.encode_frame(request)
        self.assertEqual(len(accepted.rstrip(b'\n')),65535)
        self.assertIn('ok',self.raw(accepted))
        # Build independently of the capped encoder for the over-limit case.
        request['padding'] += 'x'
        refused = (json.dumps(request,separators=(',',':'))+'\n').encode()
        self.assertEqual(len(refused.rstrip(b'\n')),65536)
        self.assertEqual(self.raw(refused),{'v':1,'id':None,'error':'frame exceeds 64 KiB'})
        reply = {'v':1,'id':7,'ok':''}
        reply['ok'] = 'x'*(65536-len(wire.encode_frame(reply)))
        self.assertEqual(len(wire.encode_frame(reply)),65536)
        reply['ok'] += 'x'
        with self.assertRaisesRegex(wire.WireError,'64 KiB'):
            wire.encode_frame(reply)

    def test_oversize_snapshot_has_exact_bounded_error_reply(self):
        store = storage.Store()
        with store.db:
            store.db.execute('INSERT INTO memberships(key,ended) VALUES (?,?)',(KEY,'x'*70000))
        store.close()
        self.start()
        with self.raw_client() as (sock,reader):
            sock.sendall(b'{"v":1,"id":7,"op":"list"}\n')
            reply = wire.read_frame(reader)
            self.assertEqual(reply,{'v':1,'id':7,'error':
                'response exceeds 64 KiB; use status with a key or session'})
            self.assertLess(len(wire.encode_frame(reply)),65536)
            sock.sendall(wire.encode_frame(dict(v=1,id=8,op='status',key=OTHER_KEY)))
            self.assertEqual(wire.read_frame(reader)['ok']['memberships'],[])

    def test_client_reply_validation_matrix_and_null_error(self):
        good = {'v':1,'id':0,'ok':{}}
        bad = [dict(good,v=v) for v in (None,True,'1',2)]
        bad += [dict(good,id=i) for i in (True,-1,None,1,'0',1.5)]
        bad += [{'id':0,'ok':{}},{'v':1,'id':0,'ok':{},'error':'bad'},{'v':1,'id':0}]
        for reply in bad:
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(wire.encode_frame(reply))
            with self.subTest(reply=reply),wire.Client(sock) as client:
                expected = 'exactly one' if ('ok' in reply)==('error' in reply) else 'invalid reply id'
                with self.assertRaisesRegex(wire.WireError,expected):
                    client.call('hello')
        for reply,expected in (({'v':1,'id':None,'error':'frame exceeds 64 KiB'},'frame exceeds 64 KiB'),
                               ({'v':1,'id':0,'error':{}},'service error')):
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(wire.encode_frame(reply))
            with wire.Client(sock) as client:
                with self.assertRaisesRegex(wire.WireError,'^'+expected+'$'):
                    client.call('hello')

    def test_hello_contract_and_malformed_metadata(self):
        process = self.start()
        with wire.connect() as client:
            self.assertEqual(client.hello,{'version':'8.3.0-beta.4','protocol_min':1,'protocol_max':1,
                                         'schema_version':2,'pid':process.pid,'activation':'fallback'})
        good = {'version':'test-service','protocol_min':1,'protocol_max':1,'schema_version':2,'pid':4242}
        payloads = [[],None,{'protocol_min':2,'protocol_max':2},{'protocol_min':0,'protocol_max':0}]
        payloads += [dict(good,**{field:value}) for field,values in (
            ('protocol_min',[None,True,'1']),('protocol_max',[None,True,'1']),
            ('version',[None,'']),('schema_version',[True,0,'2']),('pid',[True,0,'4242'])) for value in values]
        for payload in payloads:
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(wire.encode_frame({'v':1,'id':0,'ok':payload}))
            with self.subTest(payload=payload),patch.object(wire.socket,'socket',return_value=sock):
                with self.assertRaises(wire.WireError):
                    wire.connect()
            sock.close.assert_called()

    def test_identifier_boundaries_local_and_server_validation(self):
        self.start()
        bad_sessions = ['',None,False,123,[],{},'a'*5,'a'*81,'../path','a.bbbb']
        bad_keys = [None,False,123,[],{},'a'*23,'a'*25,'A'*24,'z'*24]
        with self.raw_client() as (sock,reader):
            for field,values,error in (('session',bad_sessions,'bad session id'),('key',bad_keys,'bad identity key')):
                for value in values:
                    # null session is explicitly allowed on status, not other ops.
                    if field=='session' and value is None:
                        continue
                    request = dict(v=1,id=7,op='hello',**{field:value})
                    with self.subTest(field=field,value=value):
                        with self.assertRaisesRegex(wire.WireError,error):
                            wire.validate_request(request)
                        sock.sendall(wire.encode_frame(request))
                        self.assertEqual(wire.read_frame(reader),{'v':1,'id':7,'error':error})
            for sid in ('a'*6,'a'*80):
                wire.validate_request(dict(v=1,id=0,op='status',session=sid))
            for request_id in (0,(1 << 63)-1):
                sock.sendall(wire.encode_frame(dict(v=1,id=request_id,op='status',session=None,key=KEY)))
                self.assertEqual(wire.read_frame(reader)['id'],request_id)
            sock.sendall(b'{"v":1,"id":9,"op":"unknown"}\n')
            self.assertEqual(wire.read_frame(reader),{'v':1,'id':9,'error':'unknown op'})
            sock.sendall(b'{"v":1,"id":10,"op":"list"}\n')
            self.assertIn('ok',wire.read_frame(reader))

    def test_existing_modes_repaired_and_auxiliary_files_private(self):
        previous_umask = os.umask(0)
        try:
            store = storage.Store()
            store.close()
            for directory in (wire.home() / 'run',wire.home() / 'events',wire.home() / 'logs',wire.socket_path().parent):
                directory.mkdir(exist_ok=True)
                directory.chmod(0o775)
            for name in ('spawn.lock','lease.lock'):
                path = wire.home() / 'run' / name
                path.write_text('')
                path.chmod(0o666)
            (wire.home() / 'logs/interposer.log').write_text('')
            (wire.home() / 'logs/interposer.log').chmod(0o666)
            (wire.home() / 'events/interposer.sqlite').chmod(0o666)
            with wire.file_lock(wire.run_dir() / 'spawn.lock'):
                pass
            self.start()
            for directory in (wire.home() / 'run',wire.home() / 'events',wire.home() / 'logs',wire.socket_path().parent):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
            for path in (wire.home() / 'events').iterdir():
                if path.name.startswith('interposer.sqlite'):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            for name in ('spawn.lock','lease.lock'):
                self.assertEqual(stat.S_IMODE((wire.home() / 'run' / name).stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE((wire.home() / 'logs/interposer.log').stat().st_mode),0o600)
        finally:
            os.umask(previous_umask)

    def test_planted_paths_refused_without_modifying_targets(self):
        target = self.root / 'target'
        target.write_text('unchanged')
        private = self.root / 'private'
        private.mkdir()
        metadata = Mock(st_mode=stat.S_IFDIR | 0o700,st_uid=os.getuid()+1)
        with patch.object(Path,'lstat',return_value=metadata):
            with self.assertRaisesRegex(PermissionError,'owned by this user'):
                wire.private_dir(private)
        for name in ('spawn.lock','lease.lock'):
            path = wire.run_dir() / name
            path.symlink_to(target)
            with self.assertRaises(OSError) as refusal:
                with wire.file_lock(path):
                    self.fail('planted lock was followed')
            self.assertEqual(refusal.exception.errno,errno.ELOOP)
        owned_lock = private / 'owned.lock'
        foreign_info = Mock(st_mode=stat.S_IFREG | 0o600,st_uid=os.getuid()+1)
        with patch.object(wire.os,'fstat',return_value=foreign_info):
            with self.assertRaisesRegex(PermissionError,'owned by this user'):
                with wire.file_lock(owned_lock):
                    self.fail('foreign lock was admitted')
        log_path = wire.private_dir(wire.home() / 'logs') / 'interposer.log'
        log_path.symlink_to(target)
        with self.assertRaises(OSError) as refusal:
            with service.service_log():
                self.fail('planted log was followed')
        self.assertEqual(refusal.exception.errno,errno.ELOOP)
        wire.private_dir(wire.socket_path().parent)
        wire.socket_path().symlink_to(target)
        with self.assertRaises(PermissionError):
            service.remove_stale_socket(wire.socket_path())
        database_target = self.root / 'valid-target.sqlite'
        with sqlite3.connect(database_target) as db:
            db.execute('CREATE TABLE unrelated(value TEXT)')
        db.close()
        before = database_target.read_bytes()
        db_path = wire.private_dir(wire.home() / 'events') / 'interposer.sqlite'
        db_path.symlink_to(database_target)
        with self.assertRaisesRegex(PermissionError,'database must not be a symlink'):
            storage.Store()
        self.assertEqual(database_target.read_bytes(),before)
        db_path.unlink()
        legacy = wire.private_dir(wire.home() / 'events/hooks') / ('session-'+SESSION+'.json')
        legacy_target = self.root / 'valid-legacy.json'
        legacy_target.write_text('{}')
        legacy.symlink_to(legacy_target)
        store = storage.Store()
        try:
            store.import_hooks()
            self.assertEqual(store.snapshot()['legacy_import_skips'],
                             [{'basename':legacy.name,'reason':'invalid legacy state'}])
            self.assertEqual(store.snapshot()['sessions'],[])
        finally:
            store.close()
        self.assertEqual(legacy_target.read_text(),'{}')
        self.assertEqual(target.read_text(),'unchanged')

    def test_schema_types_primary_keys_defaults_and_behavior(self):
        declarations = {
            'hubs':'server:TEXT:1 url:TEXT announced_at:REAL state:TEXT since:REAL error:TEXT pending_url:TEXT trust:TEXT',
            'memberships':'key:TEXT:1 source:TEXT url:TEXT channel:TEXT member_id:TEXT filter:TEXT enabled:INT ended:TEXT '
                'owner_session:TEXT announced_through:INT acked_through:INT poll_state:TEXT poll_error:TEXT last_ok:REAL',
            'sessions':'session:TEXT:1 client:TEXT sink:TEXT host_pid:INT host_stamp:INT state:TEXT host_ok:INT problem:TEXT '
                'registered:REAL last_wake:REAL wakes_hour:INT',
            'holdings':'session:TEXT:1 key:TEXT:2 server:TEXT joined:REAL',
            'deliveries':'id:INTEGER:1 session:TEXT sink:TEXT body:TEXT ranges:TEXT state:TEXT created:REAL done:REAL',
            'appserver_spool':'key:TEXT:1 message_id:INT:2 payload:TEXT state:TEXT turn_id:TEXT',
            'meta':'key:TEXT:1 value:TEXT'}
        defaults = {'memberships':{'filter':"'about'",'enabled':'1','ended':"''",'announced_through':'0','acked_through':'0'},
                    'hubs':{'pending_url':"''",'trust':"'announced'"}}
        store = storage.Store()
        try:
            for table,declaration in declarations.items():
                columns = list(store.db.execute('PRAGMA table_info('+table+')'))
                expected = [part.split(':') for part in declaration.split()]
                self.assertEqual([(row['name'],row['type'],row['pk'],row['dflt_value']) for row in columns],
                    [(part[0],part[1],int(part[2]) if len(part)>2 else 0,defaults.get(table,{}).get(part[0])) for part in expected])
            self.assertEqual(store.db.execute('PRAGMA table_info(meta)').fetchall()[1]['notnull'],1)
            with store.db:
                store.db.execute('INSERT INTO memberships(key) VALUES (?)',(KEY,))
                row = store.snapshot()['memberships'][0]
                self.assertEqual((row['filter'],row['enabled'],row['ended'],row['announced_through'],row['acked_through']),
                                 ('about',1,'',0,0))
                for table,columns,values in (('holdings','session,key',(SESSION,KEY)),
                                            ('appserver_spool','key,message_id',(KEY,7))):
                    store.db.execute('INSERT INTO '+table+'('+columns+') VALUES (?,?)',values)
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.db.execute('INSERT INTO '+table+'('+columns+') VALUES (?,?)',values)
                store.db.execute('INSERT INTO deliveries(session) VALUES (?)',(SESSION,))
                receipt = store.db.execute('SELECT id FROM deliveries').fetchone()[0]
                self.assertIs(type(receipt),int)
                self.assertGreater(receipt,0)
        finally:
            store.close()

    def test_live_states_default_and_idle_clock_transition(self):
        store = storage.Store()
        try:
            for state,count in (('waiting',1),('in_turn',1),('ended',0),('idle_unreachable',0)):
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO sessions(session,state) VALUES (?,?)',(SESSION,state))
                self.assertEqual(store.live_sessions(),count,state)
        finally:
            store.close()
        self.assertEqual(inspect.signature(service.serve).parameters['idle_seconds'].default,1800)
        with patch.object(service,'serve') as serve,patch.object(service.signal,'signal'):
            self.assertEqual(service.main(['serve']),0)
            self.assertEqual(serve.call_args.kwargs['idle_seconds'],1800)
        now = [0]
        fake_store,fake_server = Mock(),Mock()
        fake_store.live_sessions.side_effect = [1,1,0,0]
        fake_server.handle_request.side_effect = lambda:now.__setitem__(0,now[0]+10)
        with patch.object(service,'Store',return_value=fake_store),patch.object(service,'Server',return_value=fake_server), \
             patch.object(service.time,'monotonic',side_effect=lambda:now[0]):
            service.serve(idle_seconds=20)
        self.assertEqual(fake_server.handle_request.call_count,4)

    def test_invalid_activation_descriptors_and_environment(self):
        good = {'family':socket.AF_UNIX,'type':socket.SOCK_STREAM,
                'getsockname.return_value':str(wire.socket_path()),'getsockopt.return_value':1,
                'fileno.return_value':3}
        invalid = [{'family':socket.AF_INET},{'type':socket.SOCK_DGRAM},
                   {'getsockname.return_value':str(self.root / 'wrong.sock')},{'getsockopt.return_value':0}]
        for changes in invalid:
            sock = Mock(**dict(good,**changes))
            with self.subTest(changes=changes),patch.dict(os.environ,{'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'1'}), \
                 patch.object(service.socket,'socket',return_value=sock),patch.object(service.os,'set_inheritable'):
                with self.assertRaisesRegex(wire.WireError,'invalid systemd interposer socket'):
                    service.activated_socket()
                sock.close.assert_called_once()
        for pid,count in (('bad','1'),(str(os.getpid()),'bad')):
            with patch.dict(os.environ,{'LISTEN_PID':pid,'LISTEN_FDS':count}):
                with self.assertRaisesRegex(wire.WireError,'activation environment'):
                    service.activated_socket()
        sock = Mock(**good)
        with patch.dict(os.environ,{'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'1','LISTEN_FDNAMES':'interposer'}), \
             patch.object(service.socket,'socket',return_value=sock),patch.object(service.os,'set_inheritable') as inherit:
            self.assertIs(service.activated_socket(),sock)
            for field in ('LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES'):
                self.assertNotIn(field,os.environ)
            inherit.assert_called_once_with(sock.fileno(),False)

    def test_status_ipc_filters_multiple_memberships_and_sessions(self):
        store = storage.Store()
        with store.db:
            for key,sid in ((KEY,SESSION),(OTHER_KEY,OTHER_SESSION)):
                store.db.execute('INSERT INTO memberships(key,source,channel,member_id) VALUES (?,?,?,?)',
                                 (key,'quartet','room','member-'+key[:1]))
                store.db.execute("INSERT INTO sessions(session,state) VALUES (?,'idle_unreachable')",(sid,))
                store.db.execute('INSERT INTO holdings(session,key,server) VALUES (?,?,?)',(sid,key,'nth-demo'))
        store.close()
        self.start()
        with wire.connect() as client:
            result = client.call('status',key=KEY,session=SESSION)
            self.assertEqual([row['key'] for row in result['memberships']],[KEY])
            self.assertEqual([row['session'] for row in result['sessions']],[SESSION])
            self.assertEqual([(row['session'],row['key']) for row in result['holdings']],[(SESSION,KEY)])
            result = client.call('status',key=OTHER_KEY)
            self.assertEqual([row['key'] for row in result['holdings']],[OTHER_KEY])
        completed = subprocess.run([sys.executable,str(ROOT / 'server/nth_cli.py'),'interposer','status'],
                                   capture_output=True,text=True,timeout=10)
        self.assertEqual(completed.returncode,0,completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual({row['key'] for row in result['memberships']},{KEY,OTHER_KEY})
        self.assertEqual({row['session'] for row in result['sessions']},{SESSION,OTHER_SESSION})
        self.assertNotIn('body',result)

    @unittest.skipUnless(getattr(os,'geteuid',lambda:-1)()==0 and sys.platform.startswith('linux'),
                         'foreign-uid subprocess requires root')
    def test_foreign_uid_subprocess(self):
        process = self.start()
        for path in (self.root,self.runtime,wire.socket_path().parent):
            path.chmod(0o711)
        wire.socket_path().chmod(0o666)  # Allow reaching SO_PEERCRED, rather than DAC rejection.
        code = ('import socket,sys; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(1); '
                's.connect(sys.argv[1]); s.sendall(b\'{"v":1,"id":7,"op":"hello"}\\n\'); '
                '\ntry: data=s.recv(1)\nexcept ConnectionResetError: data=b""\n'
                'assert data==b"",data')
        # Resolve a venv symlink before dropping uid: a private interpreter path
        # must not turn a peer-admission assertion into an exec permission error.
        probe = subprocess.run([str(Path(sys.executable).resolve()),'-I','-c',code,str(wire.socket_path())],
                               user=65534,group=65534,extra_groups=(),capture_output=True,text=True,timeout=5)
        self.assertEqual(probe.returncode,0,probe.stderr)

    def test_parent_startup_delay_does_not_race_idle_smoke(self):
        real_popen = subprocess.Popen
        def delayed(*args,**kwargs):
            process = real_popen(*args,**kwargs)
            threading.Event().wait(.7)  # Deterministic scheduler-delay reproduction.
            return process
        with patch.object(subprocess,'Popen',side_effect=delayed):
            process = self.start()
        self.assertIsNone(process.poll())

    def test_literal_golden_requests_and_replies_for_implemented_ops(self):
        fixtures = json.loads((ROOT / 'tests/fixtures/interposer-wire-golden.json').read_text())
        self.assertEqual([row['request']['op'] for row in fixtures],['hello','hub.announce','list','status'])
        store = storage.Store()
        try:
            with patch.object(service.os,'getpid',return_value=4242),patch.object(storage.time,'time',return_value=123.0):
                for row in fixtures:
                    request,expected = row['request'],row['reply']
                    frame = (json.dumps(request,separators=(',',':'))+'\n').encode()
                    self.assertEqual(wire.encode_frame(request),frame)
                    payload = service.dispatch(store,wire.read_frame(io.BytesIO(frame)))
                    self.assertEqual({'v':1,'id':request['id'],'ok':payload},expected)
                    self.assertEqual(wire.encode_frame(expected),
                                     (json.dumps(expected,separators=(',',':'))+'\n').encode())
        finally:
            store.close()

    def test_legacy_writer_format_and_close_reopen(self):
        import nth_claude_hook as hook
        with hook.session_update(SESSION,create=True) as state:
            state['client'] = 'claude'
            state['memberships'][KEY] = {'source':'quartet','channel':'room','member_id':'member-1'}
            state['high_water'][KEY] = 17
            state['acked'][KEY] = 12
            state['servers'][KEY] = 'nth-demo'
            state['joined'][KEY] = 1234
        hook.configure_membership(KEY,filter_mode='at',enabled=False)
        store = storage.Store()
        store.import_hooks()
        expected = store.snapshot()
        store.close()
        store = storage.Store()
        try:
            self.assertEqual(store.snapshot(),expected)
            self.assertEqual(store.snapshot()['memberships'][0]['announced_through'],17)
            self.assertEqual(store.snapshot()['memberships'][0]['enabled'],0)
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(),[])
        finally:
            store.close()

    def test_v1_schema_upgrade_preserves_rows_and_removes_old_marker(self):
        path = wire.private_dir(wire.home() / 'events') / 'interposer.sqlite'
        db = sqlite3.connect(path)
        # The previous release's schema is an independent fixture, not generated
        # by the migration under test. Remaining tables exercise normal snapshots.
        db.executescript('''
            CREATE TABLE hubs(server TEXT PRIMARY KEY,url TEXT,announced_at REAL,state TEXT,since REAL,error TEXT);
            CREATE TABLE memberships(key TEXT PRIMARY KEY,source TEXT,url TEXT,channel TEXT,member_id TEXT,
                filter TEXT DEFAULT 'about',enabled INT DEFAULT 1,ended TEXT DEFAULT '',owner_session TEXT,
                announced_through INT DEFAULT 0,acked_through INT DEFAULT 0,poll_state TEXT,poll_error TEXT,last_ok REAL);
            CREATE TABLE sessions(session TEXT PRIMARY KEY,client TEXT,sink TEXT,host_pid INT,host_stamp INT,
                state TEXT,host_ok INT,problem TEXT,registered REAL,last_wake REAL,wakes_hour INT);
            CREATE TABLE holdings(session TEXT,key TEXT,server TEXT,joined REAL,PRIMARY KEY(session,key));
            CREATE TABLE deliveries(id INTEGER PRIMARY KEY,session TEXT,sink TEXT,body TEXT,ranges TEXT,state TEXT,created REAL,done REAL);
            CREATE TABLE appserver_spool(key TEXT,message_id INT,payload TEXT,state TEXT,turn_id TEXT,PRIMARY KEY(key,message_id));
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO hubs VALUES ('nth-demo','https://hub.example/sse',1,'announced',1,'');
            INSERT INTO meta VALUES ('hooks_imported','1');
            PRAGMA user_version=1;
        ''')
        db.close()
        store = storage.Store()
        try:
            hub = store.snapshot()['hubs'][0]
            self.assertEqual((hub['url'],hub['state'],hub['trust'],hub['pending_url']),
                             ('https://hub.example/sse','pending','announced',''))
            store.import_hooks()
            self.assertEqual(store.db.execute('SELECT * FROM meta').fetchall(),[])
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0],2)
        finally:
            store.close()

    def test_handler_uses_frame_deadline_and_rate_limited_timeout_notice(self):
        handler = service.Handler.__new__(service.Handler)
        handler.request,handler.server = Mock(),Mock()
        handler.rfile = io.BytesIO()
        reader = Mock()
        reader.readline.side_effect = TimeoutError('synthetic deadline')
        with patch.object(service,'SocketFrameReader',return_value=reader) as factory:
            handler.handle()
        factory.assert_called_once_with(handler.request,timeout=10)
        handler.server.timeout_notice.assert_called_once()


if __name__ == '__main__':
    unittest.main()
