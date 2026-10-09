"""Registered attachment admission, asynchronous startup and round-four guard gaps."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('round4',ROOT/'tests/test-interposer-fixes4.py')
round4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(round4)
cases = round4.cases
service, wire, shadow, claude = cases.service,cases.wire,cases.shadow,cases.claude
Store, Runtime = cases.Store,cases.Runtime
KEY, KEY2, SESSION, SESSION2, URL = cases.KEY,cases.KEY2,cases.SESSION,cases.SESSION2,cases.URL
request, registration, message = cases.request,cases.registration,cases.message
import nth_interposer_hubs as hubs
import nth_interposer_runtime as runtime_module


class Fixes5Tests(unittest.TestCase):
    setUp = cases.FixTests.setUp
    tearDown = cases.FixTests.tearDown
    identity = cases.FixTests.identity
    op = cases.FixTests.op
    attach = cases.FixTests.attach
    wait_start = cases.FixTests.wait_start
    eventually = cases.FixTests.eventually
    start_socket = cases.FixTests.start_socket
    stop_pollers = round4.Fixes4Tests.stop_pollers
    member = round4.Fixes4Tests.member
    buffer = round4.Fixes4Tests.buffer
    pending = round4.Fixes4Tests.pending
    restart = round4.Fixes4Tests.restart

    def import_holder(self):
        path = wire.private_dir(wire.home()/'events/hooks')/('session-'+SESSION2+'.json')
        path.write_text(json.dumps(dict(client='claude',ended=False,
            memberships={KEY:dict(source='quartet',channel='room',member_id='member')},
            servers={KEY:'nth-qweb'},joined={KEY:2})))
        self.store.import_hooks()
        with self.store.lock:
            state = self.store.session(SESSION2)
            self.assertIsNone(state['registered'])
            self.assertEqual(state['state'],'idle_unreachable')

    def test_imported_unregistered_attach_refused_on_socket_and_inbox_with_live_looking_states(self):
        self.attach()
        self.op('turn',session=SESSION,phase='started')
        self.import_holder()
        self.start_socket()
        original = self.runtime.pollers[KEY]
        with self.store.lock:
            holding = tuple(self.store.db.execute('SELECT * FROM holdings WHERE session=? AND key=?',(SESSION2,KEY)).fetchone())
        for index,state in enumerate(('idle_unreachable','idle','in_turn','waiting'),1):
            with self.subTest(state=state):
                if state!='idle_unreachable':
                    with self.store.lock,self.store.db:
                        self.store.db.execute('UPDATE sessions SET state=? WHERE session=?',(state,SESSION2))
                frame = request('membership.attach',session=SESSION2,key=KEY,server='nth-qweb',via='connect')
                with wire.connect(timeout=.4) as client:
                    client.socket.sendall(wire.encode_frame(frame))
                    reply = wire.read_frame(client.reader)
                self.assertIn('error',reply)
                self.assertEqual(reply['error']['code'],'unknown_session')
                inbox = wire.private_dir(wire.home()/'events/inbox')
                entry = inbox/('attach-'+str(index)+'.json')
                entry.write_bytes(wire.encode_frame(frame))
                self.runtime.drain()
                self.assertTrue((inbox/'bad'/entry.name).exists())
                with self.store.lock:
                    self.assertEqual(self.runtime.member(KEY)['owner_session'],SESSION)
                    self.assertIs(self.runtime.pollers[KEY],original)
                    self.assertFalse(original._stop.is_set())
                    self.assertEqual(tuple(self.store.db.execute('SELECT * FROM holdings WHERE session=? AND key=?',
                                                               (SESSION2,KEY)).fetchone()),holding)
                mid = 1000+index
                with self.hub.lock:
                    self.hub.replies.append(dict(event='new_messages',messages=[message(mid,mentioned=True)]))
                self.eventually(lambda:self.member(KEY)['shadow_announced_through']==mid)
        self.op('turn',session=SESSION,phase='ended')
        self.assertEqual({r['last'] for r in shadow.records('would')[0]['ranges']},{1004})
        # Explicit registration is the prerequisite for a successful takeover.
        self.op('session.register',**{k:v for k,v in registration(SESSION2).items() if k not in ('v','id','op')})
        self.op('membership.attach',session=SESSION2,key=KEY,server='nth-qweb',via='connect')
        self.wait_start(KEY)
        self.assertEqual(self.member(KEY)['owner_session'],SESSION2)
        self.assertIsNot(self.runtime.pollers[KEY],original)

    def test_registered_nonlive_attach_cannot_take_ownership(self):
        self.attach()
        self.op('session.register',**{k:v for k,v in registration(SESSION2).items() if k not in ('v','id','op')})
        for state in ('ended','idle_unreachable','unavailable'):
            with self.subTest(state=state),self.store.lock:
                with self.store.db:
                    self.store.db.execute('UPDATE sessions SET state=? WHERE session=?',(state,SESSION2))
                with self.assertRaises(wire.WireError) as denied:
                    self.op('membership.attach',session=SESSION2,key=KEY,server='nth-qweb',via='connect')
                self.assertEqual(denied.exception.code,'unknown_session')
                self.assertEqual(self.runtime.member(KEY)['owner_session'],SESSION)

    def prepare_remote(self, count=8):
        self.store.setup_hub('nth-slow','https://slow.example/sse')
        self.store.register(registration())
        keys = [format(index+100,'024x') for index in range(count)]
        for index,key in enumerate(keys):
            self.identity(key,url='https://slow.example/sse')
            path = self.store.path.parent/'identities'/(key+'.json')
            identity = json.loads(path.read_text())
            identity['member_id'] = 'member-'+str(index)
            path.write_text(json.dumps(identity))
            self.store.attach(request('membership.attach',session=SESSION,key=key,server='nth-slow',via='connect'))
        return keys

    def join_startups(self, workers):
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_eight_slow_failing_startups_leave_tick_socket_existing_shadow_and_close_responsive(self):
        self.attach(source='local',url='/fixture/nth.db',server='nth-trio')
        self.op('turn',session=SESSION,phase='started')
        existing = self.runtime.pollers[KEY]
        keys = self.prepare_remote()
        entered = threading.Event()
        calls, mutex = [],threading.Lock()
        restricted = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
        def slow(*args, **kwargs):
            with mutex:
                calls.append(1)
            entered.set()
            time.sleep(.25)
            return restricted
        with patch.object(hubs.socket,'getaddrinfo',side_effect=slow):
            before = time.monotonic()
            self.runtime.reconcile()
            self.assertLess(time.monotonic()-before,.15)
            self.assertTrue(entered.wait(1))
            self.start_socket()
            before = time.monotonic()
            with wire.connect(timeout=.3) as client:
                self.assertIn('hubs',client.call('list'))
                client.call('status',key=KEY)
            self.assertLess(time.monotonic()-before,.5)
            before = time.monotonic()
            self.runtime.tick()
            self.assertLess(time.monotonic()-before,.15)
            with self.store.lock:
                self.assertEqual([m['id'] for m in existing._fresh([message(1011,mentioned=True)])],[1011])
            # Drain several denied cohorts; each reconcile itself must stay short.
            deadline = time.monotonic()+2
            while time.monotonic()<deadline:
                before = time.monotonic()
                self.runtime.reconcile()
                self.assertLess(time.monotonic()-before,.15)
                with self.store.lock:
                    self.assertLessEqual(len(self.runtime.startup_threads),4)
                    failed = all(key in self.runtime.start_retry for key in keys)
                if failed:
                    break
                time.sleep(.01)
            else:
                self.fail('startup checks did not finish their denied cohorts')
            self.assertGreaterEqual(len(calls),8)
            self.assertEqual(set(self.runtime.pollers),{KEY})
            # Begin another blocked cohort and close without waiting for its DNS.
            with self.store.lock:
                self.runtime.start_retry.clear()
            self.runtime.reconcile()
            workers = list(self.runtime.startup_threads)
            before = time.monotonic()
            self.assertTrue(self.runtime.close())
            self.assertLess(time.monotonic()-before,.5)
            self.join_startups(workers)
        self.assertEqual(self.runtime.pollers,{})
        self.assertEqual(self.runtime.startups,{})
        self.assertFalse(any(url=='https://slow.example/sse' for url,_ in self.hub.calls))
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],1011)

    def test_startup_failure_backoff_and_state_change_retry(self):
        keys = self.prepare_remote(1)
        key = keys[0]
        bad = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(hubs.socket,'getaddrinfo',return_value=bad) as resolver:
            self.runtime.reconcile()
            self.wait_start(key)
            with self.store.lock:
                first = self.runtime.start_retry[key]
            calls = resolver.call_count
            with patch.object(runtime_module.time,'monotonic',return_value=first[1]-.01):
                for _ in range(100):
                    self.runtime.reconcile()
                self.assertEqual(resolver.call_count,calls)
                self.assertNotIn(key,self.runtime.startups)
            self.op('membership.configure',key=key,filter='at')
            self.wait_start(key)
            self.assertGreater(resolver.call_count,calls)
        public = [(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        with patch.object(hubs.socket,'getaddrinfo',return_value=public):
            # A fresh owner registration invalidates the failed startup generation.
            self.op('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
            self.wait_start(key)
        self.assertIn(key,self.runtime.pollers)
        self.assertNotIn(key,self.runtime.start_retry)

    def test_startup_checks_discard_state_and_identity_changes_before_factory(self):
        keys = self.prepare_remote(7)
        self.store.register(registration(SESSION2))
        for key,action in zip(keys,('stop','owner','filter','trust','config','identity','registration')):
            with self.subTest(action=action):
                # Keep unrelated prepared rows out of this controlled validation.
                with self.store.lock,self.store.db:
                    self.store.db.execute('UPDATE memberships SET shadow_enabled=0')
                    self.store.db.execute('UPDATE memberships SET shadow_enabled=1 WHERE key=?',(key,))
                    if action=='config':
                        self.store.db.execute("UPDATE hubs SET approved=1 WHERE server='nth-slow'")
                entered,unblock = threading.Event(),threading.Event()
                def dns(*args, **kwargs):
                    entered.set()
                    unblock.wait(2)
                with patch.object(hubs,'check_host',side_effect=dns),patch.object(self.runtime,'factory',wraps=self.hub.factory) as factory:
                    self.runtime.reconcile()
                    self.assertTrue(entered.wait(1))
                    workers = list(self.runtime.startup_threads)
                    with self.store.lock:
                        if action=='stop':
                            self.store.configure(request('membership.configure',key=key,enabled=False))
                        elif action=='owner':
                            self.store.attach(request('membership.attach',session=SESSION2,key=key,server='nth-slow',via='connect'))
                        elif action=='filter':
                            self.store.configure(request('membership.configure',key=key,filter='at'))
                        elif action=='trust':
                            self.store.setup_hub('nth-slow','https://changed.example/sse')
                        elif action=='config':
                            self.store.setup_hub('nth-slow','https://changed.example/sse')
                        elif action=='identity':
                            path = self.store.path.parent/'identities'/(key+'.json')
                            value = json.loads(path.read_text())
                            value['session_token'] = 'synthetic-replaced-token'
                            path.write_text(json.dumps(value))
                        else:
                            self.store.register(registration())
                    unblock.set()
                    self.join_startups(workers)
                    self.assertNotIn(key,self.runtime.pollers)
                    factory.assert_not_called()
                self.store.setup_hub('nth-slow','https://slow.example/sse')
        self.assertEqual(self.hub.calls,[])

    def test_factory_completion_rechecks_before_start_and_closes_stale_candidate(self):
        key = self.prepare_remote(1)[0]
        entered,unblock = threading.Event(),threading.Event()
        def factory(identity):
            entered.set()
            unblock.wait(2)
            return self.hub.factory(identity)
        with patch.object(hubs,'check_host'),patch.object(self.runtime,'factory',side_effect=factory):
            self.runtime.reconcile()
            self.assertTrue(entered.wait(1))
            workers = list(self.runtime.startup_threads)
            with self.store.lock:
                self.store.configure(request('membership.configure',key=key,enabled=False))
            unblock.set()
            self.join_startups(workers)
        self.assertNotIn(key,self.runtime.pollers)
        self.assertEqual(self.hub.calls,[])
        self.assertEqual(self.hub.closed,1)

    def test_startup_worker_capacity_and_per_reconcile_work_are_bounded(self):
        keys = self.prepare_remote()
        entered,unblock = threading.Event(),threading.Event()
        calls,mutex = [],threading.Lock()
        def factory(identity):
            with mutex:
                calls.append(1)
            entered.set()
            unblock.wait(2)
            return self.hub.factory(identity)
        with patch.object(hubs,'check_host'),patch.object(self.runtime,'factory',side_effect=factory):
            self.runtime.reconcile()
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                self.runtime.reconcile()
            with self.store.lock:
                workers = list(self.runtime.startup_threads)
                self.assertEqual(len(workers),4)
                self.assertEqual(len(self.runtime.startups),4)
            self.assertLessEqual(len(calls),4)
            self.runtime.close()
            unblock.set()
            self.join_startups(workers)
        self.assertEqual(self.runtime.pollers,{})
        self.assertEqual(self.hub.calls,[])

    def test_thread_start_failure_still_bounds_one_reconcile(self):
        self.prepare_remote()
        with patch.object(runtime_module.threading.Thread,'start',side_effect=RuntimeError('synthetic-startup-failure')) as start:
            self.runtime.reconcile()
        self.assertEqual(start.call_count,4)
        self.assertEqual(len(self.runtime.start_retry),4)
        self.assertEqual(self.runtime.startup_slots._value,4)
        self.assertEqual(self.runtime.startup_threads,set())

    def counters(self, key):
        row = self.member(key)
        return row['shadow_ids'],row['shadow_notices']

    def check_mixed_counters(self, recovery):
        self.attach()
        self.attach(KEY2,source='local',url='/fixture/other.db',server='nth-trio')
        self.stop_pollers()
        self.op('turn',session=SESSION,phase='started')
        with self.store.lock:
            self.runtime.pollers[KEY]._fresh([message(1041,mentioned=True),message(1043,mentioned=True)])
            self.runtime.pollers[KEY2]._fresh([message(1047,mentioned=True),message(1051,mentioned=True)])
        def flush():
            if recovery:
                with patch.object(runtime_module,'append',return_value=False):
                    self.assertFalse(self.runtime.close())
                self.restart()
            else:
                self.assertTrue(self.runtime.release(SESSION,force=True,flush=True))
        flush()
        self.assertEqual(self.counters(KEY),(2,1))
        self.assertEqual(self.counters(KEY2),(2,1))
        with self.store.lock:
            self.runtime.accumulate(self.runtime.member(KEY),[],'channel ended')
            self.runtime.accumulate(self.runtime.member(KEY2),[],'channel ended')
        flush()
        self.assertEqual(self.counters(KEY),(2,2))
        self.assertEqual(self.counters(KEY2),(2,2))
        with self.store.lock:
            self.runtime.accumulate(self.runtime.member(KEY),[message(1053,mentioned=True),message(1057,mentioned=True)])
            self.runtime.accumulate(self.runtime.member(KEY2),[],'channel ended')
        flush()
        self.assertEqual(self.counters(KEY),(4,3))
        self.assertEqual(self.counters(KEY2),(2,3))

    def test_exact_gapped_terminal_and_mixed_counters_on_ordinary_release(self):
        self.check_mixed_counters(False)

    def test_exact_gapped_terminal_and_mixed_counters_on_restart_recovery(self):
        self.check_mixed_counters(True)

    def test_service_reports_old_pending_without_a_live_buffer(self):
        self.buffer(1061)
        stop = threading.Event()
        stop.set()
        with patch.object(runtime_module,'append',return_value=False):
            self.assertFalse(self.runtime.close())
            self.restart()
            self.assertEqual(self.runtime.buffers,{})
            self.assertEqual(len(self.pending()),1)
            self.assertFalse(self.runtime.close())
            self.restart()
            with patch.object(service,'Store',return_value=self.store),patch.object(runtime_module,'Runtime',return_value=self.runtime):
                service.serve(stop=stop)
        self.assertIn('shadow evidence pending durable recovery',(wire.home()/'logs/interposer.log').read_text())
        self.store = Store()
        self.runtime = Runtime(self.store,logging.getLogger('test.pending'),self.hub.factory)
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],1061)


if __name__ == '__main__':
    unittest.main()
