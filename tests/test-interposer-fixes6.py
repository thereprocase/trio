"""Whole-service DNS isolation, ordered inbox work and crash-durable observation."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('round5',ROOT/'tests/test-interposer-fixes5.py')
round5=importlib.util.module_from_spec(spec);spec.loader.exec_module(round5)
cases=round5.cases
service,wire,shadow=cases.service,cases.wire,cases.shadow
Store,Runtime=cases.Store,cases.Runtime
KEY,KEY2,SESSION,SESSION2,URL=cases.KEY,cases.KEY2,cases.SESSION,cases.SESSION2,cases.URL
request,registration,message=cases.request,cases.registration,cases.message
import nth_interposer_hubs as hubs
import nth_interposer_runtime as runtime_module


class Fixes6Tests(unittest.TestCase):
    setUp=cases.FixTests.setUp
    tearDown=cases.FixTests.tearDown
    identity=cases.FixTests.identity
    op=cases.FixTests.op
    attach=cases.FixTests.attach
    wait_start=cases.FixTests.wait_start
    drain_wait=cases.FixTests.drain_wait
    eventually=cases.FixTests.eventually
    start_socket=cases.FixTests.start_socket
    stop_pollers=round5.Fixes5Tests.stop_pollers
    member=round5.Fixes5Tests.member
    pending=round5.Fixes5Tests.pending
    restart=round5.Fixes5Tests.restart

    def queue(self, name, frame):
        path=wire.private_dir(self.runtime.inbox_path)/name
        path.write_bytes(wire.encode_frame(frame))
        return path

    def responsive_path(self, path):
        self.attach(source='local',url='/fixture/nth.db',server='nth-trio')
        self.op('turn',session=SESSION,phase='started')
        listener=self.runtime.pollers[KEY]
        self.start_socket()
        entered,unblock=threading.Event(),threading.Event()
        checked=[]
        check=hubs.check_host
        def audited(url,**kwargs):
            self.assertFalse(self.store.lock._is_owned(), 'DNS validator owns a store lock')
            self.assertFalse(getattr(hubs._dns_context,'service',False), 'DNS validator on service thread')
            checked.append(url)
            return check(url,**kwargs)
        def slow(*args,**kwargs):
            entered.set()
            unblock.wait(2)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        errors=[]
        caller=None
        with patch.object(hubs,'check_host',side_effect=audited),patch.object(hubs.socket,'getaddrinfo',side_effect=slow):
            try:
                if path=='wire':
                    def announce():
                        try:
                            with wire.connect(timeout=1) as client:
                                client.call('hub.announce',server='nth-slow',url='https://slow.example/sse')
                        except wire.WireError as exc:
                            errors.append(exc)
                    caller=threading.Thread(target=announce);caller.start()
                elif path=='inbox':
                    for index in range(8):
                        self.queue('%03d.json'%index,request('hub.announce',server='nth-slow-'+str(index),
                                                           url='https://slow.example/sse'))
                    self.queue('009.json',registration(SESSION2))
                    self.queue('010.json',request('turn',session=SESSION2,phase='started'))
                    before=time.monotonic()
                    with hubs.service_thread():
                        self.runtime.drain()
                    self.assertLess(time.monotonic()-before,.1)
                else:
                    self.store.setup_hub('nth-slow','https://slow.example/sse')
                    for index in range(8):
                        key=format(index+200,'024x')
                        self.identity(key,url='https://slow.example/sse')
                        file=self.store.path.parent/'identities'/(key+'.json')
                        data=json.loads(file.read_text());data['member_id']='remote-'+str(index);file.write_text(json.dumps(data))
                        self.store.attach(request('membership.attach',session=SESSION,key=key,server='nth-slow',via='connect'))
                    before=time.monotonic()
                    with hubs.service_thread():
                        self.runtime.reconcile()
                    self.assertLess(time.monotonic()-before,.1)
                self.assertTrue(entered.wait(1))
                if path=='inbox':
                    self.assertTrue((self.runtime.inbox_path/'000.json').exists())
                    self.assertTrue((self.runtime.inbox_path/'009.json').exists())
                before=time.monotonic()
                with wire.connect(timeout=.25) as client:
                    self.assertIn('hubs',client.call('list'))
                self.assertLess(time.monotonic()-before,.4)
                before=time.monotonic()
                with hubs.service_thread():
                    self.runtime.tick()
                self.assertLess(time.monotonic()-before,.1)
                with self.store.lock:
                    self.assertEqual([m['id'] for m in listener._fresh([message(1191,mentioned=True)])],[1191])
                if path=='inbox':
                    unblock.set()
                    self.runtime.inbox_thread.join(3)
                    self.assertFalse(self.runtime.inbox_busy())
                    self.assertEqual(len(checked),8)
                    self.assertEqual(list(self.runtime.inbox_path.glob('*.json')),[])
                    self.assertEqual(self.store.snapshot(session=SESSION2)['sessions'][0]['state'],'in_turn')
                before=time.monotonic()
                with hubs.service_thread():
                    self.assertTrue(self.runtime.close())
                self.assertLess(time.monotonic()-before,.5)
            finally:
                unblock.set()
                if caller:
                    caller.join(2)
                if self.runtime.inbox_thread:
                    self.runtime.inbox_thread.join(2)
                for worker in list(self.runtime.startup_threads):
                    worker.join(2)
        self.assertTrue(checked)
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],1191)

    def test_service_main_loop_responsive_for_wire_resolution(self):
        self.responsive_path('wire')

    def test_service_main_loop_responsive_for_ordered_inbox_resolution(self):
        self.responsive_path('inbox')

    def test_service_main_loop_responsive_for_reconcile_resolution(self):
        self.responsive_path('reconcile')

    def test_startup_inbox_does_not_delay_real_server_readiness(self):
        entry=self.queue('000.json',request('hub.announce',server='nth-slow',url='https://slow.example/sse'))
        entered,unblock,stop=threading.Event(),threading.Event(),threading.Event()
        errors=[]
        def slow(*args,**kwargs):
            entered.set();unblock.wait(2)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
        def run():
            try:
                service.serve(stop=stop,idle_seconds=5)
            except Exception as exc:
                errors.append(exc)
        with patch.object(hubs.socket,'getaddrinfo',side_effect=slow):
            runner=threading.Thread(target=run);runner.start()
            try:
                self.assertTrue(entered.wait(1))
                with wire.connect(timeout=.25) as client:
                    self.assertIn('hubs',client.call('list'))
                self.assertTrue(entry.exists())
                stop.set()
                runner.join(.5)
                self.assertFalse(runner.is_alive(), 'service shutdown waited for inbox DNS')
            finally:
                stop.set();unblock.set();runner.join(2)
        self.assertEqual(errors,[])
        self.assertTrue(entry.exists(), 'unapplied shutdown work was discarded')

    def test_ordered_inbox_refusal_retention_and_next_instance_replay(self):
        first=self.queue('001.json',request('hub.announce',server='nth-slow',url='https://slow.example/sse'))
        second=self.queue('002.json',registration())
        third=self.queue('003.json',request('turn',session=SESSION,phase='started'))
        entered,unblock=threading.Event(),threading.Event()
        def dns(*args,**kwargs):
            entered.set();unblock.wait(2)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(hubs.socket,'getaddrinfo',side_effect=dns):
            self.runtime.drain()
            self.assertTrue(entered.wait(1))
            with self.store.lock:
                self.assertEqual(self.store.snapshot()['sessions'],[])
            self.assertTrue(all(p.exists() for p in (first,second,third)))
            self.runtime.close()
            unblock.set();self.runtime.inbox_thread.join(2)
        self.assertTrue(all(p.exists() for p in (first,second,third)))
        self.restart()
        with patch.object(hubs.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            self.drain_wait()
        self.assertTrue((self.runtime.inbox_path/'bad'/first.name).exists())
        self.assertFalse(second.exists());self.assertFalse(third.exists())
        self.assertEqual(self.store.snapshot(session=SESSION)['sessions'][0]['state'],'in_turn')

    def test_dns_context_guard_and_approval_snapshot_recheck(self):
        for guard in (self.store.lock,hubs.service_thread()):
            with guard,patch.object(hubs.socket,'getaddrinfo') as resolver:
                with self.assertRaises(wire.WireError):
                    hubs.check_host(URL)
                with self.assertRaises(wire.WireError):
                    hubs.resolve('hub.example',443)
                resolver.assert_not_called()
        self.store.announce('nth-approval',URL)
        changed='https://changed.example/sse'
        def change_during_check(*args,**kwargs):
            self.assertFalse(self.store.lock._is_owned())
            with self.store.lock,self.store.db:
                self.store.db.execute('UPDATE hubs SET pending_url=? WHERE server=?',(changed,'nth-approval'))
        with patch.object(hubs,'check_host',side_effect=change_during_check):
            with self.assertRaises(wire.WireError) as changed_error:
                self.store.approve('nth-approval',URL)
        self.assertIn('changed during approval',str(changed_error.exception))
        self.assertEqual(self.store.snapshot()['hubs'][0]['trust'],'announced')

    def test_subprocess_abrupt_exit_preserves_gaps_handoff_and_terminal_metadata(self):
        code="""import sys,os,logging,json
sys.path.insert(0,sys.argv[1])
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime,ShadowListener
from nth_interposer_wire import private_dir
s=Store()
for session in ('session-alpha','session-bravo'):
 s.register(dict(session=session,client='claude',sink='rewake',host_pid=None,host_ok=True,problem=''))
s.setup_hub('nth-qweb','https://hub.example/sse')
k='0123456789abcdef01234567'
p=private_dir(s.path.parent/'identities')/(k+'.json')
p.write_text(json.dumps(dict(source='quartet',url='https://hub.example/sse',channel='room',member_id='member',session_token='synthetic-crash-secret')))
s.attach(dict(session='session-alpha',key=k,server='nth-qweb'))
s.turn(dict(session='session-alpha',phase='started'))
r=Runtime(s,logging.getLogger('test'),lambda i:(lambda a:dict(event='no_new',messages=[]),None))
if sys.argv[2]=='commit':
 raw=s.db
 class CommitCrash:
  def __getattr__(self,name): return getattr(raw,name)
  def __enter__(self): raw.__enter__(); return self
  def __exit__(self,*args):
   result=raw.__exit__(*args)
   if raw.execute('SELECT shadow_announced_through FROM memberships WHERE key=?',(k,)).fetchone()[0]==1203:
    os._exit(0)
   return result
 s.db=CommitCrash()
with s.lock:
 l=ShadowListener(r,r.member(k),json.loads(p.read_text()))
 r.pollers[k]=l
 l._fresh([dict(id=1201,mentioned=True),dict(id=1203,banged=True)])
 if sys.argv[2]=='handoff':
  s.attach(dict(session='session-bravo',key=k,server='nth-qweb'))
  r.transfer_buffers()
  s.turn(dict(session='session-bravo',phase='started'))
 if sys.argv[2]=='terminal':
  l._end('channel ended','')
os._exit(0)
"""
        # Each child uses an independent private home; no close handler can run.
        original_env=dict(os.environ)
        for variant in ('commit','buffer','handoff','terminal'):
            with self.subTest(variant=variant):
                child=self.root/variant
                env=dict(original_env,NTH_HOME=str(child),XDG_RUNTIME_DIR=str(self.root/'child-rt'))
                result=subprocess.run([sys.executable,'-c',code,str(ROOT/'server'),variant],
                                      env=env,capture_output=True,text=True,timeout=3)
                self.assertEqual(result.returncode,0,result.stderr)
                with patch.dict(os.environ,NTH_HOME=str(child)):
                    store=Store()
                    runtime=Runtime(store,logging.getLogger('test.crash'),self.hub.factory)
                    try:
                        with store.lock:
                            self.assertEqual(runtime.member(KEY)['shadow_announced_through'],1203)
                            owner=SESSION2 if variant=='handoff' else SESSION
                            self.assertIn(owner,runtime.buffers)
                            self.assertEqual(runtime.buffers[owner]['members'][KEY]['count'],2)
                            self.assertEqual(shadow.records('would'),[], 'in-turn restoration released early')
                        service.dispatch(store,request('turn',session=owner,phase='ended'),runtime)
                        rows=shadow.records('would')
                        self.assertEqual([(r['first'],r['last']) for r in rows[0]['ranges']],[(1201,1201),(1203,1203)])
                        self.assertEqual(rows[0]['session'],owner)
                        self.assertEqual(bool(rows[0]['ended']),variant=='terminal')
                        with store.lock:
                            self.assertEqual(runtime.member(KEY)['shadow_ids'],2)
                        self.assertNotIn('synthetic-crash-secret',store.path.read_bytes().decode('latin1'))
                    finally:
                        runtime.close()
                        for worker in list(runtime.startup_threads):worker.join(1)
                        store.close()

    def test_journal_write_failure_rolls_back_cursor_and_memory(self):
        self.attach()
        self.stop_pollers()
        listener=self.runtime.pollers[KEY]
        with patch.object(self.runtime,'persist_buffers',side_effect=OSError('synthetic-journal-failure')):
            with self.assertRaises(OSError):
                listener._fresh([message(1211,mentioned=True)])
        self.assertEqual(self.member(KEY)['shadow_announced_through'],0)
        self.assertEqual(self.runtime.buffers,{})
        self.assertEqual(self.pending(),[])

    def prepare_one(self):
        self.store.setup_hub('nth-qweb',URL)
        self.store.register(registration())
        self.identity()
        self.store.attach(request('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect'))

    def finish_startups(self):
        with self.store.lock:
            workers=list(self.runtime.startup_threads)
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def require_fresh_delivery(self, mid):
        with self.hub.lock:
            self.hub.replies.append(dict(event='new_messages',messages=[message(mid,mentioned=True)]))
        self.runtime.reconcile()
        self.finish_startups()
        self.assertIn(KEY,self.runtime.pollers)
        for _ in range(100):
            if self.member(KEY)['shadow_announced_through']==mid:
                break
            threading.Event().wait(.01)
        else:
            self.fail('recovered startup did not observe a fresh ID')
        self.assertTrue(self.runtime.release(SESSION,force=True,flush=True))
        self.assertEqual(shadow.records('would')[-1]['ranges'][-1]['last'],mid)

    def test_unchanged_startup_retries_production_delay_sequence_and_recovers(self):
        self.prepare_one()
        clock=[1000.0]
        with patch.object(runtime_module.time,'monotonic',side_effect=lambda:clock[0]):
            with patch.object(hubs,'check_host',side_effect=wire.WireError('synthetic-refusal')) as resolver:
                for index,delay in enumerate((.5,1,2,4,8,16,30,30),1):
                    before=clock[0]
                    self.runtime.reconcile()
                    self.finish_startups()
                    with self.store.lock:
                        state=self.runtime.start_retry[KEY]
                    self.assertEqual(state[2],index)
                    self.assertAlmostEqual(state[1]-before,delay,places=5)
                    self.assertEqual(resolver.call_count,index)
                    clock[0]=state[1]-.001
                    self.runtime.reconcile()
                    self.assertNotIn(KEY,self.runtime.startups)
                    self.assertEqual(resolver.call_count,index)
                    clock[0]=state[1]+.001
            with patch.object(hubs,'check_host'):
                self.require_fresh_delivery(1221)
            self.assertNotIn(KEY,self.runtime.start_retry)

    def test_missing_empty_and_nonstring_tokens_fail_before_factory_then_repair(self):
        self.prepare_one()
        path=self.store.path.parent/'identities'/(KEY+'.json')
        original=json.loads(path.read_text())
        clock=[1000.0]
        with patch.object(runtime_module.time,'monotonic',side_effect=lambda:clock[0]):
            for index,token in enumerate(('missing','',None,False,17,{}),1):
                with self.subTest(token=token):
                    data=dict(original)
                    if token=='missing':
                        data.pop('session_token')
                    else:
                        data['session_token']=token
                    path.write_text(json.dumps(data))
                    self.store.configure(request('membership.configure',key=KEY,enabled=True))
                    before=self.member(KEY)['shadow_announced_through']
                    calls=len(self.hub.calls)
                    with patch.object(self.runtime,'factory',wraps=self.hub.factory) as factory:
                        self.runtime.reconcile()
                        self.finish_startups()
                        factory.assert_not_called()
                    self.assertEqual(len(self.hub.calls),calls)
                    self.assertEqual(self.member(KEY)['shadow_announced_through'],before)
                    self.assertEqual(self.member(KEY)['ended'],'')
                    self.assertEqual(self.member(KEY)['poll_state'],'reconnecting')
                    self.assertNotIn(KEY,self.runtime.pollers)
                    due=self.runtime.start_retry[KEY][1]
                    path.write_text(json.dumps(original))
                    clock[0]=due+.001
                    self.require_fresh_delivery(1225+index)
                    self.op('membership.configure',key=KEY,enabled=False)
                    self.assertNotIn(KEY,self.runtime.pollers)

    def test_worker_launch_failure_clears_job_and_same_signature_recovers(self):
        self.prepare_one()
        clock=[1000.0]
        with patch.object(runtime_module.time,'monotonic',side_effect=lambda:clock[0]):
            with patch.object(runtime_module.threading.Thread,'start',side_effect=RuntimeError('synthetic-worker-failure')):
                self.runtime.reconcile()
            self.assertEqual(self.runtime.startups,{})
            self.assertEqual(self.runtime.pollers,{})
            self.assertEqual(self.runtime.startup_threads,set())
            self.assertEqual(self.runtime.startup_slots._value,4)
            due=self.runtime.start_retry[KEY][1]
            clock[0]=due+.001
            self.require_fresh_delivery(1233)

    def test_listener_launch_failure_closes_candidate_and_same_signature_recovers(self):
        self.prepare_one()
        clock=[1000.0]
        with patch.object(runtime_module.time,'monotonic',side_effect=lambda:clock[0]):
            with patch.object(runtime_module.ShadowListener,'start',side_effect=RuntimeError('synthetic-listener-failure')):
                self.runtime.reconcile()
                self.finish_startups()
            self.assertEqual(self.runtime.startups,{})
            self.assertEqual(self.runtime.pollers,{})
            self.assertEqual(self.runtime.startup_slots._value,4)
            self.assertEqual(self.hub.closed,1)
            due=self.runtime.start_retry[KEY][1]
            clock[0]=due+.001
            self.require_fresh_delivery(1237)


if __name__=='__main__':
    unittest.main()
