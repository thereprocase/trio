"""Shutdown recovery, bounded DNS, private logging and resume eligibility."""
import importlib.util
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('round3', ROOT/'tests/test-interposer-fixes3.py')
round3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(round3)
cases = round3.cases
service, wire, shadow, claude = cases.service, cases.wire, cases.shadow, cases.claude
Store, Runtime = cases.Store, cases.Runtime
KEY, KEY2, SESSION, SESSION2, URL = cases.KEY, cases.KEY2, cases.SESSION, cases.SESSION2, cases.URL
request, registration, message = cases.request, cases.registration, cases.message
import nth_interposer_hubs as hubs
import nth_interposer_runtime as runtime_module


class Fixes4Tests(unittest.TestCase):
    setUp = cases.FixTests.setUp
    tearDown = cases.FixTests.tearDown
    identity = cases.FixTests.identity
    op = cases.FixTests.op
    attach = cases.FixTests.attach
    wait_start = cases.FixTests.wait_start
    eventually = cases.FixTests.eventually
    start_socket = cases.FixTests.start_socket
    stop_pollers = round3.Fixes3Tests.stop_pollers
    member = round3.Fixes3Tests.member

    def buffer(self, mid=903):
        self.attach()
        self.stop_pollers()
        self.op('turn', session=SESSION, phase='started')
        with self.store.lock:
            listener = self.runtime.pollers[KEY]
            listener._fresh([message(mid, mentioned=True)])
            self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'], mid)
        return listener

    def pending(self):
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute(
                "SELECT key,value FROM meta WHERE key LIKE 'shadow_pending:%'")]

    def restart(self):
        self.store.close()
        self.store = Store()
        self.runtime = Runtime(self.store, logging.getLogger('test.recovery'), self.hub.factory)

    def test_final_flush_lock_contention_preserves_ids_across_restart(self):
        listener = self.buffer()
        path = wire.private_dir(wire.home()/'events/shadow')/'would.lock'
        with wire.file_lock(path):
            real_append = runtime_module.append
            def committed_before_append(*args, **kwargs):
                with closing(sqlite3.connect(self.store.path)) as reader:
                    saved = [json.loads(row[0]) for row in reader.execute(
                        "SELECT value FROM meta WHERE key LIKE 'shadow_pending:%'")]
                self.assertTrue(saved, 'pending evidence was not committed before append')
                self.assertEqual(saved[0]['ranges'][0]['last'], 903)
                return real_append(*args, **kwargs)
            with patch.object(runtime_module, 'append', side_effect=committed_before_append):
                self.assertFalse(self.runtime.close())
            self.assertTrue(self.runtime.buffers)
            self.assertEqual(shadow.records('would'), [])
            pending = self.pending()
            self.assertEqual(len(pending), 1)
            record = json.loads(pending[0]['value'])
            self.assertEqual(set(record), {'session','client','sink','ranges','ended','lines'})
            self.assertEqual(record['ranges'][0]['last'], 903)
            self.assertNotIn('synthetic-token-private', pending[0]['value'])
            self.assertNotIn('synthetic-peer-text-private', pending[0]['value'])
        self.restart()
        self.assertEqual(self.member(KEY)['shadow_announced_through'], 903)
        rows = shadow.records('would')
        self.assertEqual([(r['ranges'][0]['first'], r['ranges'][0]['last']) for r in rows], [(903,903)])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.member(KEY)['shadow_ids'], 1)
        from nth_interposer_runtime import ShadowListener
        replay = ShadowListener(self.runtime, self.member(KEY), dict(source='quartet',url=URL))
        with self.store.lock:
            self.assertEqual(replay._fresh([message(903, mentioned=True)]), [])
        self.assertTrue(listener._stop.is_set())
        self.assertTrue(self.runtime.close())
        self.restart()
        self.assertEqual(len(shadow.records('would')), 1)
        # Interrupt append before it can return, then discard the old process state.
        self.buffer(941)
        class Interrupted(BaseException):
            pass
        with patch.object(runtime_module, 'append', side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                self.runtime.close()
        self.restart()
        self.assertEqual(self.member(KEY)['shadow_announced_through'], 941)
        self.assertEqual({r['ranges'][0]['last'] for r in shadow.records('would')}, {903,941})
        self.assertEqual(self.pending(), [])
        listener = self.buffer(947)
        with self.store.lock:
            listener._fresh([message(949, mentioned=True)])
        self.assertTrue(self.runtime.release(SESSION, force=True, flush=True))
        self.assertEqual((self.member(KEY)['shadow_ids'],self.member(KEY)['shadow_notices']), (4,3))
        with self.store.lock:
            self.runtime.accumulate(self.runtime.member(KEY), [], 'channel ended')
        self.assertTrue(self.runtime.release(SESSION, force=True, flush=True))
        self.assertEqual((self.member(KEY)['shadow_ids'],self.member(KEY)['shadow_notices']), (4,4))

    def test_final_flush_retries_transient_failure_and_reports_success(self):
        self.buffer(907)
        real = runtime_module.append
        attempts = []
        def transient(*args, **kwargs):
            attempts.append(1)
            return False if len(attempts) == 1 else real(*args, **kwargs)
        with patch.object(runtime_module, 'append', side_effect=transient):
            self.assertTrue(self.runtime.close())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.runtime.buffers, {})
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'], 907)

    def test_pending_recovery_is_private_survives_ended_owner_and_kill_switch(self):
        self.buffer(911)
        with self.store.lock:
            item = self.runtime.buffers[SESSION]['members'][KEY]
            item['content'] = 'synthetic-secret-body'
            item['ranges'][0]['token'] = 'synthetic-secret-token'
            self.store.end(SESSION)
        with patch.dict(os.environ, TRIO_INTERPOSER_SHADOW='0'):
            self.assertFalse(self.runtime.close())
        payload = self.pending()[0]['value']
        self.assertNotIn('synthetic-secret', payload)
        self.assertNotIn('channel', payload)
        with patch.dict(os.environ, TRIO_INTERPOSER_SHADOW='0'):
            self.restart()
            self.assertEqual(len(self.pending()), 1)
            self.assertEqual(self.runtime.buffers, {})
            import nth_interposer_store as storage
            with patch.object(storage, 'MAX_SESSIONS', 2):
                service.dispatch(self.store, registration(SESSION2), self.runtime)
                with self.assertRaises(wire.WireError) as refused:
                    service.dispatch(self.store, registration('session-extra'), self.runtime)
                self.assertEqual(refused.exception.code, 'session_limit')
                self.assertEqual(self.store.snapshot(session=SESSION)['sessions'][0]['state'], 'ended')
                self.assertEqual(self.store.snapshot(session=SESSION)['holdings'][0]['attached'], 1)
        self.runtime.reconcile()
        self.assertEqual(self.pending(), [])
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'], 911)
        service.dispatch(self.store, dict(registration(), resume=True), self.runtime)
        self.wait_start(KEY)
        self.assertEqual(self.member(KEY)['owner_session'], SESSION)
        self.assertIn(KEY, self.runtime.pollers)

    def test_older_pending_record_is_not_overwritten_by_another_failed_close(self):
        self.buffer(913)
        with patch.object(runtime_module, 'append', return_value=False):
            self.assertFalse(self.runtime.close())
            self.restart()
            self.assertEqual(len(self.pending()), 1)
            self.assertEqual(self.runtime.buffers, {})
            self.assertFalse(self.runtime.close(), 'old pending evidence must make close incomplete')
            self.restart()
            with self.store.lock:
                self.runtime.accumulate(self.runtime.member(KEY), [message(917, mentioned=True)])
            self.assertFalse(self.runtime.close())
            self.assertEqual(len(self.pending()), 2)
        self.restart()
        self.assertEqual({r['ranges'][0]['last'] for r in shadow.records('would')}, {913,917})
        self.assertEqual(self.pending(), [])

    def test_serve_checks_final_flush_failure_and_closes_resources(self):
        self.buffer(919)
        closed = Mock()
        real_server_close = service.Server.server_close
        def close_server(server):
            closed()
            real_server_close(server)
        stop = threading.Event()
        stop.set()
        with patch.object(service, 'Store', return_value=self.store), \
             patch.object(runtime_module, 'Runtime', return_value=self.runtime), \
             patch.object(runtime_module, 'append', return_value=False), \
             patch.object(service.Server, 'server_close', close_server):
            service.serve(stop=stop)
        closed.assert_called_once()
        self.assertIn('shadow evidence pending durable recovery', (wire.home()/'logs/interposer.log').read_text())
        self.store = Store()
        self.runtime = Runtime(self.store, logging.getLogger('test.recovery'), self.hub.factory)
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'], 919)

    def test_delayed_announcement_keeps_socket_admission_shadow_and_close_responsive(self):
        listener = self.buffer(921)
        self.start_socket()
        entered, unblock, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.10',443))]
        def slow(host, port, **kwargs):
            entered.set()
            unblock.wait(3)
            return answer
        def announce():
            try:
                with wire.connect(timeout=2) as client:
                    client.call('hub.announce', server='nth-slow', url='https://slow.example/sse')
            except wire.WireError as exc:
                errors.append(exc)
            finally:
                done.set()
        with patch.object(hubs.socket, 'getaddrinfo', side_effect=slow), \
             patch.object(hubs, 'DNS_TIMEOUT', 2):
            caller = threading.Thread(target=announce)
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                with wire.connect(timeout=.3) as client:
                    self.assertIn('hubs', client.call('list'))
                self.assertLess(time.monotonic()-before, .5)
                with self.store.lock:
                    self.assertEqual([m['id'] for m in listener._fresh([message(923, mentioned=True)])], [923])
                before = time.monotonic()
                self.assertTrue(self.runtime.close())
                self.assertLess(time.monotonic()-before, .5)
                unblock.set()
                self.assertTrue(done.wait(1))
            finally:
                unblock.set()
                caller.join(3)
        self.assertFalse(caller.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('closing', str(errors[0]))
        self.assertFalse(any(r['server']=='nth-slow' for r in self.store.snapshot()['hubs']))
        self.assertEqual(max(r['last'] for r in shadow.records('would')[0]['ranges']), 923)

    def test_resolver_deadline_and_worker_cap_are_independent(self):
        entered, unblock, finished = threading.Event(), threading.Event(), threading.Event()
        answers = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        count, guard, errors = [0], threading.Lock(), []
        def slow(*args, **kwargs):
            with guard:
                count[0] += 1
            entered.set()
            unblock.wait(3)
            return answers
        def request_resolution():
            try:
                hubs.resolve('slow.example',443)
            except wire.WireError as exc:
                errors.append(exc)
            finally:
                finished.set()
        slots = threading.BoundedSemaphore(2)
        with patch.object(hubs, '_dns_slots', slots), patch.object(hubs, 'DNS_TIMEOUT', .05), \
             patch.object(hubs.socket, 'getaddrinfo', side_effect=slow):
            caller = threading.Thread(target=request_resolution)
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(finished.wait(.5), 'resolver deadline did not expire')
                caller.join(1)
                # The first OS worker is still blocked; only one more can start.
                for _ in range(12):
                    with self.assertRaises(wire.WireError):
                        hubs.resolve('slow.example',443)
                self.assertEqual(count[0], 2)
            finally:
                unblock.set()
                caller.join(3)
                deadline = time.monotonic()+1
                while slots._value != 2 and time.monotonic()<deadline:
                    time.sleep(.005)
        self.assertEqual(len(errors), 1)
        self.assertEqual(slots._value, 2)
        # Use actual production bounds: neither deadline nor semaphore is replaced.
        blocked, completed = threading.Event(), threading.Event()
        counts, errors, mutex = [0,0], [], threading.Lock()
        def production_slow(*args, **kwargs):
            with mutex:
                counts[0] += 1
            blocked.wait(3)
            return answers
        def production_call():
            try:
                hubs.resolve('slow.example',443)
            except wire.WireError as exc:
                with mutex:
                    errors.append(exc)
            finally:
                with mutex:
                    counts[1] += 1
                    if counts[1]==12:
                        completed.set()
        production_slots = hubs._dns_slots
        available = production_slots._value
        with patch.object(hubs.socket,'getaddrinfo',side_effect=production_slow):
            callers = [threading.Thread(target=production_call) for _ in range(12)]
            for caller in callers:
                caller.start()
            try:
                self.assertTrue(completed.wait(.8), 'production DNS deadline exceeded')
                self.assertEqual(counts[0],4, 'production resolver worker limit changed')
                self.assertEqual(len(errors),12)
            finally:
                blocked.set()
                for caller in callers:
                    caller.join(2)
                deadline = time.monotonic()+1
                while production_slots._value!=available and time.monotonic()<deadline:
                    time.sleep(.005)
        self.assertEqual(production_slots._value,available)
        # An exception from Thread.start must release the same acquired slot.
        startup_slots = threading.BoundedSemaphore(1)
        with patch.object(hubs,'_dns_slots',startup_slots):
            with patch.object(hubs.threading.Thread,'start',side_effect=RuntimeError('synthetic-start-failure')):
                with self.assertRaises(RuntimeError):
                    hubs.resolve('slow.example',443)
            self.assertTrue(startup_slots.acquire(blocking=False), 'failed thread startup leaked capacity')
            startup_slots.release()
            with patch.object(hubs.socket,'getaddrinfo',return_value=answers):
                self.assertEqual(hubs.resolve('slow.example',443),answers)

    def test_announcement_rechecks_trusted_config_after_dns(self):
        self.store.setup_hub('nth-local','http://127.0.0.1/sse')
        entered, unblock = threading.Event(), threading.Event()
        errors = []
        def check(url, **kwargs):
            self.assertTrue(kwargs['allow_restricted'])
            entered.set()
            unblock.wait(2)
        def announce():
            try:
                self.op('hub.announce', server='nth-candidate', url='http://127.0.0.1/sse')
            except wire.WireError as exc:
                errors.append(exc)
        with patch.object(hubs, 'check_host', side_effect=check):
            caller = threading.Thread(target=announce)
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                self.store.setup_hub('nth-local',URL)
            finally:
                unblock.set()
                caller.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIn('configuration changed', str(errors[0]))
        self.assertFalse(any(r['server']=='nth-candidate' for r in self.store.snapshot()['hubs']))

    def test_service_log_fifo_startup_and_reopen_rotation_fail_promptly(self):
        child_home = self.root/'c'
        directory = wire.private_dir(child_home/'logs')
        path = directory/'interposer.log'
        env = dict(os.environ, NTH_HOME=str(child_home), XDG_RUNTIME_DIR=str(self.root/'cr'))
        for kind in ('regular','empty-fifo','readable-fifo'):
            with self.subTest(startup=kind):
                fd = None
                if kind=='regular':
                    path.write_text('')
                else:
                    os.mkfifo(path)
                    if kind=='readable-fifo':
                        fd = os.open(path, os.O_RDWR|os.O_NONBLOCK)
                try:
                    result = subprocess.run([sys.executable, str(ROOT/'server/nth_interposer.py'),
                        'serve','--idle-seconds','.1'], env=env, capture_output=True, text=True, timeout=2)
                    self.assertEqual(result.returncode, 0 if kind=='regular' else 1, result.stderr)
                    if kind=='regular':
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    else:
                        self.assertIn('interposer failed: OSError', result.stderr)
                        self.assertNotIn(str(child_home), result.stderr)
                        if fd is not None:
                            with self.assertRaises(BlockingIOError):
                                os.read(fd,4096)
                finally:
                    if fd is not None:
                        os.close(fd)
                    path.unlink()
        code = """import sys,os
sys.path.insert(0,sys.argv[1])
from nth_interposer import PrivateRotatingHandler
p=sys.argv[2]; mode=sys.argv[3]
h=PrivateRotatingHandler(p,maxBytes=1,backupCount=1)
fds=[]
if mode=='reopen':
    h.stream.close(); h.stream=None; os.unlink(p); os.mkfifo(p)
    if sys.argv[4]=='readable': fds.append(os.open(p,os.O_RDWR|os.O_NONBLOCK))
    operation=h._open
else:
    rotate=h.rotate
    def plant(a,b):
        rotate(a,b); os.mkfifo(a)
        if sys.argv[4]=='readable': fds.append(os.open(a,os.O_RDWR|os.O_NONBLOCK))
    h.rotate=plant
    operation=h.doRollover
try:
    operation()
except OSError:
    pass
else:
    raise AssertionError('FIFO service log accepted')
finally:
    h.close()
    for fd in fds: os.close(fd)
"""
        for mode in ('reopen','rotate'):
            for readable in ('empty','readable'):
                with self.subTest(mode=mode, readable=readable):
                    result = subprocess.run([sys.executable,'-c',code,str(ROOT/'server'),str(path),mode,readable],
                                            env=env,capture_output=True,text=True,timeout=2)
                    self.assertEqual(result.returncode,0,result.stderr)
                    path.unlink()
                    path.with_suffix('.log.1').unlink(missing_ok=True)
        # A child exit must not hide leaked rejected descriptors.
        handler = service.PrivateRotatingHandler(path,maxBytes=1,backupCount=1)
        handler.stream.close()
        handler.stream = None
        path.unlink()
        os.mkfifo(path)
        reader = os.open(path,os.O_RDWR|os.O_NONBLOCK)
        opened, real_open = [], os.open
        def capture(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        try:
            with patch.object(service.os,'open',side_effect=capture):
                for _ in range(20):
                    with self.assertRaises(OSError):
                        handler._open()
                    with self.assertRaises(OSError) as unusable:
                        os.fstat(opened[-1])
                    import errno
                    self.assertEqual(unusable.exception.errno,errno.EBADF)
            self.assertEqual(len(opened),20)
        finally:
            handler.close()
            for fd in opened:
                try:
                    os.close(fd)
                except OSError:
                    pass
            os.close(reader)
            path.unlink()

    def test_log_initialization_failure_does_not_reopen_failed_sink(self):
        with patch.object(service,'serve',side_effect=OSError('synthetic-secret')), \
             patch.object(service,'service_log') as log, patch('sys.stderr') as stderr, \
             patch.object(service.signal,'signal'):
            self.assertEqual(service.main(['serve']),1)
        log.assert_not_called()
        self.assertNotIn('synthetic-secret',''.join(str(call) for call in stderr.write.call_args_list))

    def test_resume_ignores_later_ended_and_unregistered_attached_holders(self):
        for ineligible in ('ended','unregistered'):
            with self.subTest(ineligible=ineligible):
                for session in (SESSION,SESSION2):
                    self.op('session.register',**{k:v for k,v in dict(registration(session),resume=True).items()
                                                  if k not in ('v','id','op')})
                self.attach()
                self.attach(session=SESSION2)
                self.op('session.end',session=SESSION)
                self.op('session.end',session=SESSION2)
                with self.store.lock, self.store.db:
                    self.assertIsNone(self.runtime.member(KEY)['owner_session'])
                    self.store.db.execute('UPDATE holdings SET joined=10 WHERE session=?',(SESSION,))
                    self.store.db.execute('UPDATE holdings SET joined=20 WHERE session=?',(SESSION2,))
                    if ineligible=='unregistered':
                        self.store.db.execute("UPDATE sessions SET registered=NULL,state='idle' WHERE session=?",(SESSION2,))
                baseline = len(self.hub.calls)
                self.hub.replies = [dict(event='new_messages',messages=[message(931 if ineligible=='ended' else 937,mentioned=True)])]
                self.op('session.register',**{k:v for k,v in dict(registration(),resume=True).items() if k not in ('v','id','op')})
                self.wait_start(KEY)
                with self.store.lock:
                    self.assertEqual(self.runtime.member(KEY)['owner_session'],SESSION)
                    self.assertIn(KEY,self.runtime.pollers)
                expected = 931 if ineligible=='ended' else 937
                self.eventually(lambda:self.member(KEY)['shadow_announced_through']==expected)
                self.assertGreater(len(self.hub.calls),baseline)
                self.assertTrue(self.runtime.release(SESSION,force=True))
                self.assertTrue(any(r['ranges'][0]['last']==expected for r in shadow.records('would')))
                self.op('session.end',session=SESSION)


if __name__ == '__main__':
    unittest.main()
