"""Round-three lifecycle, TOML preservation and evidence reader regressions."""
import importlib.util
import io
import json
import os
from pathlib import Path
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fix_cases', ROOT/'tests/test-interposer-shadow-fixes.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
service, wire, shadow, claude, codex = cases.service, cases.wire, cases.shadow, cases.claude, cases.codex
KEY, KEY2, SESSION, SESSION2, URL = cases.KEY, cases.KEY2, cases.SESSION, cases.SESSION2, cases.URL
registration, request, message = cases.registration, cases.request, cases.message


class Fixes3Tests(unittest.TestCase):
    setUp = cases.FixTests.setUp
    tearDown = cases.FixTests.tearDown
    identity = cases.FixTests.identity
    op = cases.FixTests.op
    attach = cases.FixTests.attach
    wait_start = cases.FixTests.wait_start
    eventually = cases.FixTests.eventually

    def member(self, key):
        with self.store.lock:
            return self.runtime.member(key)

    def session(self, session):
        with self.store.lock:
            return self.store.session(session)

    def stop_pollers(self):
        for listener in list(self.runtime.pollers.values()):
            listener.stop()
            listener.thread.join(1)
            self.assertFalse(listener.thread.is_alive())

    def setup_module(self):
        spec = importlib.util.spec_from_file_location('fixture_setup', ROOT/'setup.py')
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        return setup

    def test_shutdown_quiesces_requests_before_final_buffer_flush(self):
        self.attach()
        self.stop_pollers()
        self.op('turn', session=SESSION, phase='started')
        self.runtime.accumulate(self.member(KEY), [message(81, mentioned=True)])
        accepted, resume, replied = threading.Event(), threading.Event(), threading.Event()
        stop = threading.Event()
        results, errors = [], []
        real_dispatch = service.dispatch
        real_close = service.Server.server_close

        def delayed(store, req, runtime=None, **kwargs):
            if req['op'] == 'membership.configure':
                accepted.set()
                if not resume.wait(3):
                    raise RuntimeError('request barrier timed out')
            return real_dispatch(store, req, runtime, **kwargs)

        def closing(server):
            # serve has completed the final flush, but the accepted handler is
            # still alive and its connection can carry a refusal reply.
            self.assertTrue(self.runtime.closing)
            self.assertEqual(self.runtime.buffers, {})
            with patch.object(service, 'peer_allowed', return_value=True):
                self.assertFalse(server.verify_request(None, None))
            resume.set()
            self.assertTrue(replied.wait(2))
            self.runtime.reconcile()
            self.assertEqual(self.runtime.pollers, {})
            self.assertEqual(self.runtime.buffers, {})
            self.assertEqual([r['ranges'][0]['last'] for r in shadow.records('would')], [81])
            return real_close(server)

        def serve():
            try:
                service.serve(stop=stop)
            except Exception as exc:
                errors.append(exc)

        def client():
            try:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        connection = wire.connect(timeout=.5)
                        break
                    except (OSError, EOFError):
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.01)
                with connection:
                    connection.socket.sendall(wire.encode_frame(request('membership.configure', key=KEY, enabled=True)))
                    results.append(wire.read_frame(connection.reader))
            except Exception as exc:
                errors.append(exc)
            finally:
                replied.set()

        with patch.object(service, 'dispatch', side_effect=delayed), \
             patch.object(service, 'Store', return_value=self.store), \
             patch('nth_interposer_runtime.Runtime', return_value=self.runtime), \
             patch.object(service.Server, 'server_close', closing):
            runner = threading.Thread(target=serve)
            caller = threading.Thread(target=client)
            runner.start()
            caller.start()
            try:
                self.assertTrue(accepted.wait(2))
                stop.set()
                runner.join(4)
                caller.join(2)
                self.assertFalse(runner.is_alive())
                self.assertFalse(caller.is_alive())
            finally:
                stop.set()
                resume.set()
                runner.join(4)
                caller.join(2)
        self.assertEqual(errors, [])
        self.assertIn('error', results[0])
        self.assertEqual(results[0]['error']['code'], 'service_closing')
        # serve closes its Store; tearDown still needs an open fixture connection.
        self.store = cases.Store()
        self.runtime.store = self.store

    def test_closed_runtime_cannot_advance_cursor_or_restart_polling(self):
        self.attach()
        listener = self.runtime.pollers[KEY]
        self.runtime.close()
        with self.store.lock:
            # A slow poll can finish after the Store closes. Check shutdown
            # before even attempting to read membership state.
            with patch.object(self.runtime, 'member', side_effect=AssertionError('closed store accessed')):
                self.assertEqual(listener._fresh([message(83, mentioned=True)]), [])
            self.runtime.reconcile()
            self.assertEqual(self.member(KEY)['shadow_announced_through'], 0)
            self.assertEqual(self.runtime.pollers, {})
            self.assertEqual(self.runtime.buffers, {})

    def test_resume_hook_restarts_unowned_retained_membership(self):
        for client in ('claude', 'codex'):
            with self.subTest(client=client):
                session = SESSION if client == 'claude' else SESSION2
                key = KEY if client == 'claude' else KEY2
                self.attach(key, session=session, url=URL if client == 'claude' else 'https://second.example/sse',
                            server='nth-qweb' if client == 'claude' else 'nth-second')
                self.op('session.end', session=session)
                self.assertIsNone(self.member(key)['owner_session'])
                with claude.session_update(session, create=True) as state:
                    state['client'] = client
                    state['ended'] = True
                    state['memberships'][key] = dict(source='quartet', channel='room', member_id='member')
                self.stop_pollers()
                baseline = len(self.hub.calls)
                hook = claude if client == 'claude' else codex
                payload = dict(session_id=session, source='resume')
                with patch.object(wire, 'socket_path', return_value=self.root/'absent.sock'), \
                     patch.object(codex, 'codex_host', return_value=(os.getpid(), '')), \
                     patch.object(codex, 'spawn_waiter'), \
                     patch.object(os, 'umask'), patch.object(claude, 'SAY', io.StringIO()), \
                     patch('sys.stderr', io.StringIO()), \
                     patch('sys.stdin', io.StringIO(json.dumps(payload))):
                    self.assertEqual(hook.main(['start']), 0)
                    os.sys.stderr.close()
                self.hub.replies = [dict(event='new_messages', messages=[message(87, mentioned=True)])]
                self.runtime.drain()
                self.wait_start(key)
                with self.store.lock:
                    self.assertEqual(self.session(session)['state'], 'idle')
                    self.assertEqual(self.member(key)['owner_session'], session)
                    self.assertIn(key, self.runtime.pollers)
                self.eventually(lambda: self.member(key)['shadow_announced_through'] == 87)
                self.assertGreater(len(self.hub.calls), baseline)
                self.runtime.release(session, force=True)
                self.assertTrue(any(r['session'] == session and r['ranges'][0]['last'] == 87
                                    for r in shadow.records('would')))
                self.op('session.end', session=session)

    def test_resume_preserves_other_owner_and_unattached_holdings(self):
        self.attach(session=SESSION2)
        self.attach()
        with self.store.lock, self.store.db:
            # Make retained holder order deterministic: the reviving session
            # was the latest holder before it ended and yielded to SESSION2.
            self.store.db.execute('UPDATE holdings SET joined=? WHERE session=?', (1, SESSION2))
            self.store.db.execute('UPDATE holdings SET joined=? WHERE session=?', (2, SESSION))
        self.op('session.end', session=SESSION)
        self.op('session.register', **{k:v for k,v in dict(registration(), resume=True).items()
                                      if k not in ('v','id','op')})
        self.assertEqual(self.member(KEY)['owner_session'], SESSION2)
        with self.store.lock, self.store.db:
            self.store.db.execute('UPDATE holdings SET attached=0 WHERE session=?', (SESSION,))
        self.op('session.end', session=SESSION2)
        self.op('session.register', **{k:v for k,v in dict(registration(), resume=True).items()
                                      if k not in ('v','id','op')})
        self.assertIsNone(self.member(KEY)['owner_session'])

    def test_death_check_does_not_end_concurrently_registered_host(self):
        self.attach()
        for new_pid in (102, 101):
            with self.subTest(new_pid=new_pid):
                with patch.object(claude, 'process_stamp', return_value=1), \
                     patch('nth_interposer_store.time.time', return_value=100):
                    service.dispatch(self.store, dict(registration(), host_pid=101, resume=True), self.runtime)
                previous_generation = self.session(SESSION)['registered']
                checked, continue_check = threading.Event(), threading.Event()
                errors = []
                def old_host(pid):
                    self.assertEqual(pid, 101)
                    checked.set()
                    self.assertTrue(continue_check.wait(2))
                    return None
                def tick():
                    try:
                        self.runtime.tick()
                    except Exception as exc:
                        errors.append(exc)
                self.runtime.last_death = 0
                with patch('nth_interposer_runtime.process_stamp', side_effect=old_host):
                    worker = threading.Thread(target=tick)
                    worker.start()
                    try:
                        self.assertTrue(checked.wait(2))
                        with patch.object(claude, 'process_stamp', return_value=1), \
                             patch('nth_interposer_store.time.time', return_value=100):
                            service.dispatch(self.store, dict(registration(), host_pid=new_pid, resume=True), self.runtime)
                        self.assertGreater(self.session(SESSION)['registered'], previous_generation)
                    finally:
                        continue_check.set()
                        worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                with self.store.lock:
                    self.assertEqual(self.session(SESSION)['state'], 'idle')
                    self.assertEqual(self.session(SESSION)['host_pid'], new_pid)
                    self.assertEqual(self.member(KEY)['owner_session'], SESSION)
                    self.assertIn(KEY, self.runtime.pollers)
        # The unchanged dead-host observation still ends its session.
        self.runtime.last_death = 0
        with patch('nth_interposer_runtime.process_stamp', return_value=None):
            self.runtime.tick()
        self.assertEqual(self.session(SESSION)['state'], 'ended')
        self.assertIsNone(self.member(KEY)['owner_session'])

    def test_handoff_keeps_original_settle_deadline_with_younger_destination_buffer(self):
        self.attach()
        self.attach(KEY2, session=SESSION2, url='https://second.example/sse', server='nth-second')
        self.stop_pollers()
        with self.store.lock:
            with patch('nth_interposer_runtime.time.monotonic', return_value=100):
                self.runtime.accumulate(self.member(KEY), [message(91, mentioned=True)])
            with patch('nth_interposer_runtime.time.monotonic', return_value=100.15):
                self.runtime.accumulate(self.member(KEY2), [message(93, mentioned=True)])
            with patch('nth_interposer_runtime.time.monotonic', return_value=100.2):
                self.op('membership.attach', session=SESSION2, key=KEY, server='nth-second', via='connect')
                self.assertNotIn(SESSION, self.runtime.buffers)
                self.runtime.release(SESSION2)
                self.assertEqual(shadow.records('would'), [])
            with patch('nth_interposer_runtime.time.monotonic', return_value=100.299):
                self.runtime.release(SESSION2)
                self.assertEqual(shadow.records('would'), [])
            with patch('nth_interposer_runtime.time.monotonic', return_value=100.301):
                self.runtime.release(SESSION2)
        rows = shadow.records('would')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session'], SESSION2)
        self.assertEqual({(r['key'], r['last']) for r in rows[0]['ranges']}, {(KEY,91),(KEY2,93)})
        self.assertEqual(self.runtime.buffers, {})

    def test_retained_toml_preserves_strings_quoted_keys_and_multiline_values(self):
        setup = self.setup_module()
        path = self.root/'config.toml'
        envs = [
            'env={KEEP=\'prefix NTH_SERVER_NAME="old" suffix\'}',
            'env={KEEP=\'prefix } suffix\', "NTH_SERVER_NAME"="old"}',
            'env={KEEP="escaped \\" NTH_SERVER_NAME=\\"old\\" } suffix", \'NTH_SERVER_NAME\'="old"}',
            '"env"={KEEP={nested="} NTH_SERVER_NAME=old"}, NTH_SERVER_NAME="old"}',
            '[mcp_servers."nth-second".env]\nKEEP="yes"\n"NTH_SERVER_NAME"="old" # retain comment',
            '[mcp_servers."nth-second".env]\nKEEP="yes"\n\'NTH_SERVER_NAME\'="old"',
            '[mcp_servers."nth-second".env]\nKEEP="""text\n[mcp_servers.fake.env]\nNTH_SERVER_NAME=old\n}"""\nNTH_SERVER_NAME="""old\nname"""',
            "[mcp_servers.'nth-second'.env]\nKEEP='''literal\n} NTH_SERVER_NAME=\"old\"\n'''",
            'env={KEEP=["}", "NTH_SERVER_NAME=old"], NTH_SERVER_NAME=7}',
        ]
        for env in envs:
            with self.subTest(env=env):
                text = '# retained comment\n[mcp_servers."nth-second"]\nargs=["nth_quartet_proxy.py"]\n' + env + '\n[mcp_servers.other]\ncommand="keep"\n'
                path.write_text(text)
                path.chmod(0o600)
                expected = tomllib.loads(text)
                expected['mcp_servers']['nth-second']['env']['NTH_SERVER_NAME'] = 'nth-second'
                setup.name_codex_hubs(path)
                self.assertEqual(tomllib.loads(path.read_text()), expected)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn('# retained comment', path.read_text())
                original = path.read_bytes()
                setup.name_codex_hubs(path)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsupported_toml_layout_is_unchanged_and_reported(self):
        setup = self.setup_module()
        path = self.root/'config.toml'
        for text in ('mcp_servers = {nth-second = {args=["nth_quartet_proxy.py"], env={KEEP="yes"}}}\n',
                     '[mcp_servers.nth-second]\nargs=["nth_quartet_proxy.py"]\nenv.KEEP="yes"\n',
                     '[mcp_servers.nth-second]\nargs=["nth_quartet_proxy.py"]\nenv="keep"\n'):
            with self.subTest(text=text):
                path.write_text(text)
                original = path.read_bytes()
                with patch('sys.stderr', io.StringIO()) as out:
                    setup.name_codex_hubs(path)
                self.assertEqual(path.read_bytes(), original)
                self.assertIn('skipped NTH_SERVER_NAME edit', out.getvalue())

    def test_toml_preservation_guard_refuses_an_unsafe_edit(self):
        setup = self.setup_module()
        path = self.root/'config.toml'
        path.write_text('[mcp_servers.nth-second]\nargs=["nth_quartet_proxy.py"]\n'
                        '[mcp_servers.nth-second.env]\nKEEP="synthetic-private-value"\n')
        original = path.read_bytes()
        decode = setup._toml_assignment
        def misidentify(text, tokens):
            key, rhs = decode(text, tokens)
            return ({'NTH_SERVER_NAME': 0} if key == {'KEEP': 0} else key), rhs
        with patch.object(setup, '_toml_assignment', side_effect=misidentify), \
             patch('sys.stderr', io.StringIO()) as out:
            setup.name_codex_hubs(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn('preservation check failed', out.getvalue())
        self.assertFalse(list(path.parent.glob('config.toml.bak-*')))

    def test_retained_config_install_preserves_mode_and_unrelated_values(self):
        previous_umask = os.umask(0o022)
        self.addCleanup(os.umask, previous_umask)
        setup = self.setup_module()
        staged = self.root/'install'
        path = staged/'.codex/config.toml'
        path.parent.mkdir(parents=True)
        path.write_text('[mcp_servers.nth-second]\nargs=["nth_quartet_proxy.py","--url","'+URL+'"]\nenv={KEEP=\'prefix } NTH_SERVER_NAME="old" suffix\'}\n')
        path.chmod(0o600)
        with patch.object(setup.subprocess, 'run'):
            setup.install(staged, clients=('codex',), skip_dependencies=True,
                          skip_systemd=True, codex_binary='/fixture/codex')
        env = tomllib.loads(path.read_text())['mcp_servers']['nth-second']['env']
        self.assertEqual(env, dict(KEEP='prefix } NTH_SERVER_NAME="old" suffix', NTH_SERVER_NAME='nth-second'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(list(path.parent.glob('config.toml.bak-*')))

    def test_records_refuses_fifo_and_existing_symlink_on_each_evidence_path(self):
        directory = wire.private_dir(wire.home()/'events/shadow')
        for side in ('actual', 'would'):
            row = dict(t=100, side=side, session=SESSION, client='claude', sink='rewake',
                       ranges=[dict(key=KEY, server='nth-qweb', first=7, last=7, count=1, addressed=True)],
                       ended=[], lines=1)
            data = (json.dumps(row)+'\n').encode()
            target = self.root/(side+'-target.jsonl')
            target.write_bytes(data)
            for suffix in ('.jsonl', '.jsonl.1'):
                path = directory/(side+suffix)
                for kind in ('empty-fifo', 'readable-fifo', 'symlink', 'regular'):
                    with self.subTest(side=side, suffix=suffix, kind=kind):
                        fd = None
                        try:
                            if kind.endswith('fifo'):
                                os.mkfifo(path)
                                if kind == 'readable-fifo':
                                    fd = os.open(path, os.O_RDWR|os.O_NONBLOCK)
                                    os.write(fd, data)
                            elif kind == 'symlink':
                                path.symlink_to(target)
                            else:
                                path.write_bytes(data)
                            # A child timeout makes blocking-read mutations fail
                            # promptly, without leaving the suite stuck.
                            import subprocess
                            code = 'import sys,json; sys.path.insert(0,sys.argv[1]); from nth_interposer_shadow import records; print(json.dumps(records(sys.argv[2])))'
                            result = subprocess.run([os.sys.executable, '-c', code, str(ROOT/'server'), side],
                                                    capture_output=True, text=True, timeout=2)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(json.loads(result.stdout), [row] if kind == 'regular' else [])
                            self.assertEqual(target.read_bytes(), data)
                        finally:
                            if fd is not None:
                                os.close(fd)
                            path.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
