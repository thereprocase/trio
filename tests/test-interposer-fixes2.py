"""Round-two regressions; never load units into a manager or bind a real runtime."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('round2_cases', ROOT / 'tests/test-interposer-skeleton.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
wire, storage, service, setup, cli, doctor = cases.wire, cases.storage, cases.service, cases.setup, cases.cli, cases.doctor
KEY, SESSION, FIXTURES = cases.KEY, cases.SESSION, cases.FIXTURES


class Round2Tests(cases.InterposerCase):
    def setUp(self):
        # Offline verify expands inherited units' %t socket paths as well as
        # ours. A long TMPDIR would exceed Unix's 108-byte sockaddr_un limit.
        with patch.object(tempfile, 'tempdir', '/tmp'):
            super().setUp()

    def units(self):
        self.fake_systemctl(running=False)
        self.assertIs(setup.install_interposer_units(self.root,sys.executable,ROOT / 'server',wire.home()),True)
        return self.root / '.config/systemd/user'

    def test_unquoted_listen_stream_and_percent_escaping(self):
        directory = self.units()
        text = (directory / 'trio-interposer.socket').read_text()
        self.assertIn('ListenStream='+str(wire.socket_path())+'\n',text)
        self.assertNotIn('ListenStream="',text)
        path = self.root / 'per%cent' / 'interposer.sock'
        self.assertEqual(setup._listen_stream(path,self.root / 'manager'),str(path).replace('%','%%'))
        for value in ('white space','tab\tname','new\nline','single\'quote','double"quote',
                      'back\\slash','control\x01','no-break\xa0space','line\u2028break'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'unsafe ListenStream'):
                setup._listen_stream(self.root / value / 'socket',self.root / 'manager')

    def test_manager_runtime_prefers_percent_t(self):
        with patch.object(wire,'_user_runtime_dir',return_value=self.runtime):
            directory = self.units()
        text = (directory / 'trio-interposer.socket').read_text()
        self.assertIn('ListenStream=%t/trio/interposer.sock\n',text)
        self.assertNotIn('ListenStream="',text)

    def test_generated_units_verify_offline(self):
        analyze = shutil.which('systemd-analyze')
        if not analyze:
            self.skipTest('systemd-analyze is not installed')
        directory = self.units()
        env = dict(os.environ,HOME=str(self.root),XDG_CONFIG_HOME=str(self.root / '.config'),
                   XDG_RUNTIME_DIR=str(self.runtime),SYSTEMD_UNIT_PATH=str(directory)+':')
        # verify parses the supplied files offline. No systemctl call loads these
        # units into the real manager; its only invocations above are our fake.
        completed = subprocess.run([analyze,'--user','verify',str(directory / 'trio-interposer.socket'),
                                    str(directory / 'trio-interposer.service')],
                                   env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertNotIn('ListenStream',completed.stderr)
        self.assertEqual(self.calls(),[['--user','show-environment'],['--user','daemon-reload'],
            ['--user','enable','--now','trio-interposer.socket'],
            ['--user','is-active','--quiet','trio-interposer.service']])

    def test_waiter_status_files_are_ignored_before_savepoints(self):
        hooks = wire.private_dir(wire.home() / 'events') / 'hooks'
        shutil.copytree(FIXTURES,hooks)
        status = hooks / ('session-'+SESSION+'.status.json')
        status.write_text('{invalid telemetry; not state')
        stray = hooks / 'session-not-a-session.extra.json'
        stray.write_text('{invalid telemetry')
        originals = {p.name:p.read_bytes() for p in hooks.iterdir()}
        store = storage.Store()
        statements = []
        store.db.set_trace_callback(statements.append)
        logger = Mock()
        try:
            self.assertTrue(store.import_hooks(log=logger))
            self.assertEqual(store.snapshot()['legacy_import_skips'],[])
            self.assertEqual(len(store.snapshot()['sessions']),2)
            self.assertEqual(sum(s.startswith('SAVEPOINT') for s in statements),3)
            logger.warning.assert_not_called()
        finally:
            store.close()
        self.start()
        self.assertEqual(doctor.interposer_check()[1],doctor.OK)
        self.assertEqual({p.name:p.read_bytes() for p in hooks.iterdir()},originals)

    def fake_ps(self):
        directory = self.root / 'bin'
        directory.mkdir(exist_ok=True)
        self.ps_body = self.root / 'ps-body.txt'
        self.ps_log = self.root / 'ps-argv.jsonl'
        program = directory / 'ps'
        program.write_text('#!'+sys.executable+'\n'
            'import json,os,sys\n'
            'with open(os.environ["FAKE_PS_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
            'print(open(os.environ["FAKE_PS_BODY"]).read(),end="")\n'
            'sys.exit(int(os.environ.get("FAKE_PS_EXIT","0")))\n')
        program.chmod(0o755)
        os.environ.update(PATH=str(directory),FAKE_PS_LOG=str(self.ps_log),FAKE_PS_BODY=str(self.ps_body))

    def test_non_linux_pid_verification_uses_fake_ps(self):
        self.fake_ps()
        for body,expected in (('python3 /install/server/nth_interposer.py serve',True),
                              ('python3 "/install/with space/nth_interposer.py" serve',True),
                              ('python3 /install/server/nth_interposer.py stop',False),
                              ('python3 /install/server/unrelated.py serve',False),('',False)):
            self.ps_body.write_text(body)
            with self.subTest(body=body):
                self.assertEqual(wire.service_process(4242,platform='darwin'),expected)
        self.assertTrue(all(json.loads(s)==['-o','command=','-p','4242'] for s in self.ps_log.read_text().splitlines()))
        self.ps_body.write_text('python3 /install/server/nth_interposer.py serve')
        os.environ['FAKE_PS_EXIT'] = '1'
        self.assertFalse(wire.service_process(4242,platform='darwin'))
        with patch.object(wire.subprocess,'run',side_effect=subprocess.TimeoutExpired('ps',1)):
            self.assertFalse(wire.service_process(4242,platform='darwin'))
        os.environ.pop('NTH_INTERPOSER_TEST_RUNTIME')
        self.assertEqual(wire._ps_command(),'/bin/ps')

    def test_non_linux_restart_verifies_before_signalling(self):
        self.fake_ps()
        self.ps_body.write_text('python3 /install/server/unrelated.py serve')
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.hello = {'pid':4242,'activation':'fallback'}
        with patch.object(wire.sys,'platform','darwin'),patch.object(wire,'connect',return_value=client), \
             patch.object(wire.os,'kill') as kill,patch('sys.stderr',io.StringIO()):
            self.assertEqual(cli.main(['interposer','restart']),1)
            kill.assert_not_called()
            self.ps_body.write_text('python3 /install/server/nth_interposer.py serve')
            kill.side_effect = lambda *_:self.ps_body.write_text('')
            with patch('sys.stdout',io.StringIO()):
                self.assertEqual(cli.main(['interposer','restart']),0)
            kill.assert_called_once_with(4242,wire.signal.SIGTERM)

    def test_no_user_manager_is_quiet_and_creates_no_units(self):
        self.fake_systemctl(available=False)
        stderr = io.StringIO()
        with patch('sys.stderr',stderr):
            self.assertFalse(setup.systemd_available(self.root))
            result = setup.install(self.root,clients=(),skip_dependencies=True)
        self.assertIs(result['interposer_systemd'],False)
        self.assertEqual(stderr.getvalue(),'')
        self.assertFalse((self.root / '.config').exists())
        self.assertTrue(Path(result['launcher']).exists())
        for error in (OSError('synthetic'),subprocess.TimeoutExpired('systemctl',10)):
            with patch.object(setup.subprocess,'run',side_effect=error),patch('sys.stderr',stderr):
                self.assertFalse(setup.systemd_available(self.root))
        self.assertEqual(stderr.getvalue(),'')

    def test_stop_fallback_ipc_failure_does_not_stop_install(self):
        self.fake_systemctl(running=False)
        for error in (EOFError('synthetic'),OSError('synthetic')):
            with self.subTest(error=type(error).__name__),patch.object(wire,'stop_fallback',side_effect=error), \
                 patch('sys.stderr',io.StringIO()) as stderr:
                self.assertIs(setup.install_interposer_units(self.root,sys.executable,ROOT / 'server',wire.home()),True)
                self.assertIn('continuing socket setup',stderr.getvalue())
        self.assertEqual(sum('enable' in s for s in self.calls()),2)

    def test_nth_home_fallback_unit_pins_exact_socket(self):
        os.environ.pop('XDG_RUNTIME_DIR')
        directory = self.units()
        chosen = wire.home() / 'run/interposer.sock'
        socket_unit = (directory / 'trio-interposer.socket').read_text()
        unit = (directory / 'trio-interposer.service').read_text()
        self.assertIn('ListenStream='+str(chosen)+'\n',socket_unit)
        self.assertIn('Environment="NTH_INTERPOSER_SOCKET='+str(chosen)+'"\n',unit)
        self.assertNotIn('Environment="XDG_RUNTIME_DIR=',unit)
        # Simulate a later environment with a different valid runtime. The
        # pinned service still adopts exactly the pathname systemd supplied.
        os.environ['XDG_RUNTIME_DIR'] = str(self.runtime)
        os.environ['NTH_INTERPOSER_SOCKET'] = str(chosen)
        self.start(activated=True)
        self.assertTrue(chosen.is_socket())
        self.assertFalse((self.runtime / 'trio/interposer.sock').exists())

    def test_explicit_socket_override_and_activation_path_mismatch(self):
        chosen = self.root / 'pinned/interposer.sock'
        with patch.dict(os.environ,{'NTH_INTERPOSER_SOCKET':str(chosen)}):
            self.assertEqual(wire.socket_path(),chosen)
            sock = Mock(family=socket.AF_UNIX,type=socket.SOCK_STREAM)
            sock.getsockname.return_value = str(self.runtime / 'trio/interposer.sock')
            sock.getsockopt.return_value = 1
            sock.fileno.return_value = 3
            with patch.dict(os.environ,{'LISTEN_PID':str(os.getpid()),'LISTEN_FDS':'1'}), \
                 patch.object(service.socket,'socket',return_value=sock),patch.object(service.os,'set_inheritable'):
                with self.assertRaisesRegex(wire.WireError,'invalid systemd interposer socket'):
                    service.activated_socket()
                sock.close.assert_called_once()

    def test_spawned_child_keeps_guard_and_never_discovers_real_runtime(self):
        process = Mock()
        with patch.object(wire.subprocess,'Popen',return_value=process) as popen, \
             patch.object(wire.threading,'Thread'):
            wire._spawn()
        self.assertEqual(popen.call_args.kwargs['env']['NTH_INTERPOSER_TEST_RUNTIME'],str(self.root))
        self.assertEqual(popen.call_args.kwargs['env']['NTH_INTERPOSER_SOCKET'],str(wire.socket_path()))
        # Deleting only XDG_RUNTIME_DIR must select temporary NTH_HOME, without
        # inspecting a canonical runtime outside the test guard.
        self.runtime.rmdir()
        real_safe = wire._safe_runtime
        def safe(path,**kwargs):
            self.assertTrue(path.is_relative_to(self.root),'real runtime was inspected')
            return real_safe(path,**kwargs)
        with patch.object(wire,'_safe_runtime',side_effect=safe),patch.object(wire,'_user_runtime_dir',return_value=Path('/run/user')/'synthetic-user'):
            self.assertEqual(wire.socket_path(),wire.home() / 'run/interposer.sock')
        # Start an actual isolated child with the vanished XDG child directory.
        child = wire._spawn()
        self.processes.append(child)
        deadline = time.monotonic()+10
        while True:
            try:
                with wire.connect(timeout=.5) as client:
                    self.assertEqual(client.hello['activation'],'fallback')
                    break
            except (FileNotFoundError,ConnectionRefusedError):
                self.assertLess(time.monotonic(),deadline)
                threading.Event().wait(.01)
        self.assertTrue((wire.home() / 'run/interposer.sock').is_socket())

    def test_missing_guard_fails_before_spawn_or_runtime_creation(self):
        missing = self.root / 'missing-guard'
        with patch.dict(os.environ,{'NTH_INTERPOSER_TEST_RUNTIME':str(missing)}), \
             patch.object(wire.subprocess,'Popen') as popen:
            with self.assertRaisesRegex(wire.WireError,'test interposer runtime'):
                wire._spawn()
            popen.assert_not_called()
            with self.assertRaisesRegex(wire.WireError,'test interposer runtime'):
                service.serve()
            self.assertFalse(missing.exists())
            self.assertFalse((wire.home() / 'run').exists())

    def test_child_with_missing_guard_exits_without_binding(self):
        missing = self.root / 'deleted-runtime'
        env = dict(os.environ,NTH_INTERPOSER_TEST_RUNTIME=str(missing))
        completed = subprocess.run([sys.executable,'-I','-c',
            'import sys,runpy; sys.path.insert(0,sys.argv[1]); p=sys.argv.pop(2); sys.argv.pop(1); runpy.run_path(p,run_name="__main__")',
            str(ROOT / 'server'),str(ROOT / 'server/nth_interposer.py'),'serve'],
            env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(completed.returncode,1)
        self.assertIn('WireError',completed.stderr)
        self.assertFalse((wire.home() / 'run/interposer.sock').exists())

    def test_guard_blocks_outside_and_symlink_socket_overrides(self):
        for value in ('relative.sock','/run/user/synthetic-user/interposer.sock',
                      str(self.root / '../escaped.sock')):
            with self.subTest(value=value),patch.dict(os.environ,{'NTH_INTERPOSER_SOCKET':value}):
                with self.assertRaises(wire.WireError):
                    wire.socket_path()
        with tempfile.TemporaryDirectory(prefix='ip2-guard-outside-') as name:
            outside = Path(name)
            link = self.root / 'link'
            link.symlink_to(outside,target_is_directory=True)
            with patch.dict(os.environ,{'NTH_INTERPOSER_SOCKET':str(link / 'interposer.sock')}):
                with self.assertRaisesRegex(wire.WireError,'outside the guarded'):
                    wire.socket_path()

    def test_doctor_uses_bounded_skip_status_for_large_state(self):
        store = storage.Store()
        with store.db:
            store.db.execute('INSERT INTO sessions(session,problem) VALUES (?,?)',(SESSION,'x'*70000))
        store.close()
        self.start()
        with wire.connect() as client:
            with self.assertRaisesRegex(wire.WireError,'response exceeds 64 KiB'):
                client.call('list')
            self.assertEqual(client.call('status',skips=True),{'skips':[],'skipped_total':0})
        self.assertEqual(doctor.interposer_check()[1],doctor.OK)

    def test_skip_summary_is_bounded_and_validates_filters(self):
        store = storage.Store()
        try:
            store.import_skips = [{'basename':'session-'+('x'*250)+'.json','reason':'invalid legacy state'} for _ in range(1000)]
            result = service.dispatch(store,{'v':1,'id':7,'op':'status','skips':True})
            self.assertEqual(len(result['skips']),32)
            self.assertEqual(result['skipped_total'],1000)
            self.assertLess(len(wire.encode_frame({'v':1,'id':7,'ok':result})),65536)
            for fields in ({'skips':1},{'skips':True,'key':KEY},{'skips':True,'session':None}):
                with self.assertRaisesRegex(wire.WireError,'skips must be'):
                    wire.validate_request(dict(v=1,id=7,op='status',**fields))
        finally:
            store.close()

    def test_frame_deadline_includes_idle_between_frames(self):
        now = [0]
        sock = Mock()
        sock.recv.return_value = b'{}\n'
        with patch.object(wire.time,'monotonic',side_effect=lambda:now[0]):
            reader = wire.SocketFrameReader(sock)
            self.assertEqual(wire.read_frame(reader),{})
            def late_first_byte(limit):
                now[0] += 11
                return b'{}\n'
            sock.recv.side_effect = late_first_byte
            with self.assertRaisesRegex(TimeoutError,'deadline'):
                wire.read_frame(reader)
        self.assertEqual([call.args[0] for call in sock.settimeout.call_args_list],[10,10])


if __name__ == '__main__':
    unittest.main()
