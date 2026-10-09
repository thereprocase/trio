"""A–E review regressions. Synthetic DNS, guarded sockets and independent migrations."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('shadow_cases',ROOT/'tests/test-interposer-shadow.py')
cases=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
service,wire,shadow,claude,codex,cli=cases.service,cases.wire,cases.shadow,cases.claude,cases.codex,cases.cli
Store,Runtime=cases.Store,cases.Runtime
KEY,KEY2,SESSION,SESSION2,URL=cases.KEY,cases.KEY2,cases.SESSION,cases.SESSION2,cases.URL
registration,request,message=cases.registration,cases.request,cases.message
import nth_interposer_hubs as hubs
from nth_sse_client import MCPSSEClient


class FixTests(unittest.TestCase):
    setUp=cases.ShadowTests.setUp
    tearDown=cases.ShadowTests.tearDown
    identity=cases.ShadowTests.identity
    op=cases.ShadowTests.op
    attach=cases.ShadowTests.attach
    wait_start=cases.ShadowTests.wait_start
    eventually=cases.ShadowTests.eventually
    start_socket=cases.ShadowTests.start_socket

    def joined(self):
        with claude.session_update(SESSION,create=True) as state:
            state['memberships'][KEY]={'source':'quartet','channel':'room','member_id':'member'}

    def test_codex_underscore_tool_end_to_end_both_records(self):
        self.identity()
        self.store.setup_hub('nth-qweb',URL)
        payload=dict(session_id=SESSION,tool_name='mcp__nth_qweb__quartet_connect',
                     tool_response=json.dumps(dict(identity_key=KEY,channel='room',member_id='member')))
        claude.register(payload,tools=codex.HOOK_TOOLS,client='codex',shadow_host=(os.getpid(),''))
        self.hub.replies=[dict(event='new_messages',messages=[message(7,mentioned=True)])]
        self.runtime.drain()
        actual=cases.ScriptedHub([dict(event='new_messages',messages=[message(7,mentioned=True)])])
        with patch.object(claude,'poll_factory',actual.factory),patch.object(codex,'codex_binary',return_value='/fixture/codex'),\
             patch.object(codex,'queue_wake',return_value='queued'),patch.object(codex,'relay_owns',return_value=False),\
             patch.multiple(claude,TICK_SECONDS=.01,UNSUPERVISED_LIFETIME_SECONDS=.2),patch.object(codex,'SETTLE_SECONDS',.01):
            self.assertEqual(claude.wait(SESSION,codex.QueueSink(SESSION,os.getpid())),2)
        self.eventually(lambda:bool(self.runtime.buffers))
        self.runtime.release(SESSION,force=True)
        for side in ('actual','would'):
            rows=shadow.records(side)
            self.assertEqual(len(rows),1)
            self.assertEqual([(r['server'],r['first'],r['last']) for r in rows[0]['ranges']],[('nth-qweb',7,7)])

    def test_importer_does_not_undo_shadow_controls_or_marks(self):
        self.attach()
        self.op('membership.configure',key=KEY,filter='at',enabled=False)
        self.op('ack.seen',session=SESSION,key=KEY,through_id=12)
        with self.store.db:
            self.store.db.execute('UPDATE memberships SET shadow_announced_through=10,shadow_ended=? WHERE key=?',('channel ended',KEY))
        directory=wire.private_dir(wire.home()/'events/hooks')
        (directory/('membership-'+KEY+'.json')).write_text(json.dumps(dict(filter='all',enabled=True,ended='')))
        (directory/('session-'+SESSION+'.json')).write_text(json.dumps(dict(client='claude',memberships={KEY:dict(source='quartet',channel='room',member_id='member')},
                high_water={KEY:80},acked={KEY:70},ended=False,
                servers={KEY:'nth-obsolete'},joined={KEY:9999999999})))
        before=dict(self.store.db.execute('SELECT * FROM holdings WHERE session=? AND key=?',(SESSION,KEY)).fetchone())
        self.store.import_hooks(directory)
        row=self.store.snapshot()['memberships'][0]
        self.assertEqual((row['filter'],row['enabled'],row['ended'],row['announced_through'],row['acked_through']),('all',1,'',80,70))
        self.assertEqual((row['shadow_filter'],row['shadow_enabled'],row['shadow_ended'],row['shadow_announced_through'],row['shadow_acked_through']),('at',0,'channel ended',10,12))
        self.assertEqual(self.store.session(SESSION)['state'],'idle')
        holding=self.store.db.execute('SELECT * FROM holdings WHERE session=? AND key=?',(SESSION,KEY)).fetchone()
        self.assertEqual((holding['server'],holding['joined']),(before['server'],before['joined']))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM meta WHERE key='hooks_import_cutover'").fetchone())

    def test_registered_host_and_in_turn_survive_import_and_register(self):
        self.attach()
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            service.dispatch(self.store,dict(registration(),host_pid=os.getpid()))
            stamp=claude.process_stamp(os.getpid())
            self.assertEqual(self.store.session(SESSION)['state'],'idle')
            self.assertEqual(self.store.live_sessions(),1)
            self.op('turn',session=SESSION,phase='started')
            service.dispatch(self.store,registration())
            self.assertEqual(self.store.session(SESSION)['state'],'in_turn')
            service.dispatch(self.store,dict(registration(),host_pid=os.getpid()))
            path=wire.private_dir(wire.home()/'events/hooks')/('session-'+SESSION+'.json')
            path.write_text(json.dumps(dict(ended=True,client='claude',memberships={})))
            self.store.import_hooks()
            row=self.store.session(SESSION)
            self.assertEqual((row['state'],row['host_pid'],row['host_stamp']),('in_turn',os.getpid(),stamp))
            self.assertEqual(self.store.live_sessions(),1)

    def test_imported_holdings_cannot_become_owner(self):
        self.attach()
        path=wire.private_dir(wire.home()/'events/hooks')/('session-'+SESSION2+'.json')
        path.write_text(json.dumps(dict(client='claude',memberships={KEY:dict(source='quartet',channel='room',member_id='member')},
                                       servers={KEY:'nth-qweb'},joined={KEY:9999999999})))
        self.store.import_hooks()
        service.dispatch(self.store,registration(SESSION2))
        self.assertEqual(self.store.session(SESSION2)['state'],'idle')
        self.assertEqual(self.store.db.execute('SELECT attached FROM holdings WHERE session=? AND key=?',(SESSION2,KEY)).fetchone()[0],0)
        self.op('session.end',session=SESSION)
        self.assertIsNone(self.store.snapshot()['memberships'][0]['owner_session'])

    def test_unregistered_attached_holder_cannot_become_owner(self):
        self.attach()
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            with self.store.db:
                self.store.db.execute("INSERT INTO sessions(session,state) VALUES ('unregistered-holder','idle')")
                self.store.db.execute("INSERT INTO holdings(session,key,server,joined,attached) VALUES ('unregistered-holder',?,'nth-qweb',9999999999,1)",(KEY,))
            self.op('session.end',session=SESSION)
            self.assertIsNone(self.runtime.member(KEY)['owner_session'])

    def test_import_unchanged_files_is_cheap_and_skips_stay_reported(self):
        path=wire.private_dir(wire.home()/'events/hooks')/('membership-'+KEY+'.json')
        path.write_text(json.dumps(dict(filter='at',enabled=False)))
        self.store.import_hooks()
        changes=self.store.db.total_changes
        self.store.import_hooks()
        self.assertEqual(self.store.db.total_changes,changes)
        with self.store.db:
            self.store.db.execute('UPDATE memberships SET shadow_enabled=0,shadow_filter=? WHERE key=?',('about',KEY))
        # Same inode and size: only mtime distinguishes this valid rewrite.
        old=path.stat()
        payload=path.read_text().replace('false','true ')
        path.write_text(payload)
        os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns+1000000000))
        self.assertEqual((path.stat().st_ino,path.stat().st_size),(old.st_ino,old.st_size))
        self.store.import_hooks()
        row=self.store.snapshot()['memberships'][0]
        self.assertEqual((row['enabled'],row['shadow_enabled'],row['shadow_filter']),(1,0,'about'))
        bad=path.with_name('membership-'+KEY2+'.json')
        bad.write_text('not json')
        self.store.import_hooks()
        self.assertEqual(self.store.skip_status()['skipped_total'],1)
        self.store.import_hooks()
        self.assertEqual(self.store.skip_status()['skipped_total'],1)

    def test_seen_buffer_and_watermark_are_atomic_across_handoff(self):
        self.attach()
        self.op('turn',session=SESSION,phase='started')
        listener=self.runtime.pollers[KEY]
        listener._fresh([message(17,mentioned=True)])
        self.assertEqual(self.runtime.buffers[SESSION]['members'][KEY]['last_id'],17)
        self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'],17)
        service.dispatch(self.store,registration(SESSION2))
        self.op('membership.attach',session=SESSION2,key=KEY,server='nth-qweb',via='listen')
        listener._deliver([message(17,mentioned=True)],[message(17,mentioned=True)])
        self.assertNotIn(SESSION,self.runtime.buffers)
        self.runtime.release(SESSION2,force=True)
        row=shadow.records('would')[0]
        self.assertEqual((row['session'],row['ranges'][0]['last']),(SESSION2,17))

    def test_buffer_failure_keeps_watermark_and_allows_retry(self):
        self.attach()
        listener=self.runtime.pollers[KEY]
        listener.stop();listener.thread.join(1)
        self.op('turn',session=SESSION,phase='started')
        observed=[]
        def fail_buffer(*args):
            observed.append(self.runtime.member(KEY)['shadow_announced_through'])
            self.assertTrue(self.store.lock._is_owned())
            raise RuntimeError('synthetic buffer failure')
        with patch.object(self.runtime,'accumulate',side_effect=fail_buffer):
            with self.assertRaises(RuntimeError):listener._fresh([message(41,mentioned=True)])
        self.assertEqual(observed,[0])
        self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'],0)
        self.assertEqual(self.runtime.buffers,{})
        from nth_interposer_runtime import ShadowListener
        replacement=ShadowListener(self.runtime,self.runtime.member(KEY),dict(source='quartet',url=URL))
        self.assertEqual([m['id'] for m in replacement._fresh([message(41,mentioned=True)])],[41])
        self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'],41)
        self.runtime.release(SESSION,force=True,flush=True)
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],41)

    def test_handoff_waits_for_buffer_before_advancing_watermark(self):
        self.attach()
        listener=self.runtime.pollers[KEY]
        listener.stop();listener.thread.join(1)
        service.dispatch(self.store,registration(SESSION2))
        entered,attempted,done=threading.Event(),threading.Event(),threading.Event()
        errors=[]
        accumulate=self.runtime.accumulate
        def boundary(row,selected):
            self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'],0)
            entered.set()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(done.wait(.05))
            accumulate(row,selected)
        def handoff():
            entered.wait(1);attempted.set()
            try:self.op('membership.attach',session=SESSION2,key=KEY,server='nth-second',via='connect')
            except Exception as exc:errors.append(exc)
            finally:done.set()
        worker=threading.Thread(target=handoff);worker.start()
        try:
            with patch.object(self.runtime,'accumulate',side_effect=boundary):
                listener._fresh([message(43,mentioned=True)])
        finally:worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.runtime.release(SESSION2,force=True)
        row=shadow.records('would')[0]
        self.assertEqual((row['session'],row['ranges'][0]['last'],row['ranges'][0]['server']),(SESSION2,43,'nth-second'))

    def test_failed_evidence_retains_buffer_counters_and_retries(self):
        self.attach()
        self.runtime.accumulate(self.runtime.member(KEY),[message(47,mentioned=True)])
        before=json.loads(json.dumps(self.runtime.buffers))
        with patch('nth_interposer_runtime.append',return_value=False):
            self.runtime.release(SESSION,force=True)
        self.assertEqual(self.runtime.buffers,before)
        row=self.runtime.member(KEY)
        self.assertEqual((row['shadow_notices'],row['shadow_ids']),(0,0))
        self.runtime.release(SESSION,force=True)
        self.assertEqual(self.runtime.buffers,{})
        self.assertEqual([(r['ranges'][0]['first'],r['ranges'][0]['last']) for r in shadow.records('would')],[(47,47)])
        row=self.runtime.member(KEY)
        self.assertEqual((row['shadow_notices'],row['shadow_ids']),(1,1))

    def test_ended_buffer_protects_session_at_cap_until_release(self):
        import nth_interposer_store as storage
        self.attach()
        listener=self.runtime.pollers[KEY]
        listener.stop();listener.thread.join(1)
        self.op('turn',session=SESSION,phase='started')
        listener._fresh([message(31,mentioned=True)])
        with patch.object(storage,'MAX_SESSIONS',2):
            service.dispatch(self.store,registration(SESSION2))
            with patch('nth_interposer_runtime.append',return_value=False):
                self.op('session.end',session=SESSION)
                with self.assertRaises(wire.WireError) as error:
                    service.dispatch(self.store,registration('session-extra'),self.runtime)
                self.assertEqual(error.exception.code,'session_limit')
                self.assertEqual(self.store.session(SESSION)['state'],'ended')
                self.assertEqual(self.runtime.member(KEY)['shadow_announced_through'],31)
                self.runtime.reconcile()
                self.assertIn(SESSION,self.runtime.buffers)
            self.runtime.release(SESSION,force=True)
            service.dispatch(self.store,registration('session-extra'),self.runtime)
            self.assertNotIn(SESSION,[r['session'] for r in self.store.snapshot()['sessions']])
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],31)

    def test_release_after_ownership_mutation_uses_current_owner_and_sink(self):
        self.attach()
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            self.runtime.accumulate(self.runtime.member(KEY),[message(37,mentioned=True)])
            service.dispatch(self.store,registration(SESSION2,'codex'))
            # Direct mutation deliberately reproduces the interval before reconcile.
            self.store.attach(request('membership.attach',session=SESSION2,key=KEY,server='nth-second',via='connect'))
            self.op('turn',session=SESSION,phase='ended')
        self.assertEqual(shadow.records('would'),[])
        self.runtime.release(SESSION2,force=True)
        row=shadow.records('would')[0]
        self.assertEqual((row['session'],row['client'],row['sink']),(SESSION2,'codex','queue'))
        self.assertEqual(row['ranges'][0]['server'],'nth-second')

    def test_dispatch_serializes_ownership_change_with_reconcile(self):
        self.attach()
        service.dispatch(self.store,registration(SESSION2))
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            self.runtime.accumulate(self.runtime.member(KEY),[message(39,mentioned=True)])
        changed,attempted,finished=threading.Event(),threading.Event(),threading.Event()
        original=self.store.attach
        errors=[]
        def pause(req):
            result=original(req)
            changed.set()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(finished.wait(.05))
            return result
        def release():
            changed.wait(1);attempted.set()
            try:self.op('turn',session=SESSION,phase='ended')
            except Exception as exc:errors.append(exc)
            finally:finished.set()
        worker=threading.Thread(target=release);worker.start()
        try:
            with patch.object(self.store,'attach',side_effect=pause):
                self.op('membership.attach',session=SESSION2,key=KEY,server='nth-second',via='connect')
        finally:worker.join(2)
        self.assertEqual(errors,[])
        self.assertFalse(worker.is_alive())
        self.runtime.release(SESSION2,force=True)
        self.assertEqual(shadow.records('would')[0]['session'],SESSION2)

    def test_ended_last_owner_preserves_buffer(self):
        self.attach()
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            self.runtime.accumulate(self.runtime.member(KEY),[message(19,mentioned=True)])
            self.op('session.end',session=SESSION)
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],19)

    def test_close_flushes_in_turn_buffer(self):
        self.attach()
        self.op('turn',session=SESSION,phase='started')
        self.runtime.accumulate(self.runtime.member(KEY),[message(23,banged=True)])
        self.runtime.close()
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],23)
        self.assertEqual(self.runtime.buffers,{})

    def test_idle_exit_flushes_existing_buffer(self):
        self.drive_service_exit('idle')
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],29)

    def drive_service_exit(self, mode, registered_idle=False):
        self.identity()
        captured=[]
        stop=threading.Event()
        clock=[100.0]
        handled=[]
        def seeded(store,log):
            runtime=Runtime(store,log,self.hub.factory)
            store.setup_hub('nth-qweb',URL)
            store.register(registration())
            store.attach(request('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect'))
            if not registered_idle:
                store.turn(request('turn',session=SESSION,phase='started'))
                runtime.accumulate(runtime.member(KEY),[message(29,mentioned=True)])
                if mode=='idle':store.end(SESSION)
            runtime.tick=Mock()
            captured.append(runtime)
            return runtime
        def handle(_server):
            handled.append(clock[0]);clock[0]+=10
            if mode=='exception':raise RuntimeError('synthetic service failure')
            if mode=='stop' or len(handled)==3:stop.set()
        with patch('nth_interposer_runtime.Runtime',side_effect=seeded),\
             patch.object(service.Server,'handle_request',handle),\
             patch.object(service.time,'monotonic',side_effect=lambda:clock[0]):
            if mode=='exception':
                with self.assertRaises(RuntimeError):service.serve(idle_seconds=1,stop=stop)
            else:service.serve(idle_seconds=1,stop=stop)
        self.assertEqual(captured[0].buffers,{})
        self.assertEqual(len(handled),3 if registered_idle else 1)

    def test_service_stop_flushes_in_turn_buffer(self):
        self.drive_service_exit('stop')
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],29)

    def test_service_exception_flushes_in_turn_buffer(self):
        self.drive_service_exit('exception')
        self.assertEqual(shadow.records('would')[0]['ranges'][0]['last'],29)

    def test_registered_idle_session_survives_service_idle_deadline(self):
        self.drive_service_exit('idle',registered_idle=True)
        self.assertEqual(shadow.records('would'),[])

    def test_claude_settle_window_coalesces_staggered_hubs(self):
        self.attach()
        self.attach(KEY2,server='nth-second',url='https://other.example/sse')
        for listener in self.runtime.pollers.values():listener.stop();listener.thread.join(1)
        with patch('nth_interposer_runtime.time.monotonic',return_value=100):
            self.runtime.accumulate(self.runtime.member(KEY),[message(1,mentioned=True)])
        with patch('nth_interposer_runtime.time.monotonic',return_value=100.1):
            self.runtime.release(SESSION)
            self.assertEqual(shadow.records('would'),[])
            self.runtime.accumulate(self.runtime.member(KEY2),[message(2,mentioned=True)])
        with patch('nth_interposer_runtime.time.monotonic',return_value=100.299):
            self.runtime.release(SESSION)
            self.assertEqual(shadow.records('would'),[])
        with patch('nth_interposer_runtime.time.monotonic',return_value=100.301):self.runtime.release(SESSION)
        rows=shadow.records('would')
        self.assertEqual(len(rows),1)
        self.assertEqual({r['key'] for r in rows[0]['ranges']},{KEY,KEY2})

    def test_resume_or_live_stamp_revives_but_ended_attach_refused(self):
        self.attach()
        self.op('session.end',session=SESSION)
        with self.assertRaises(wire.WireError):
            self.op('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect')
        self.assertEqual(service.dispatch(self.store,dict(registration(),resume=True))['state'],'idle')
        self.store.end(SESSION)
        self.assertEqual(service.dispatch(self.store,dict(registration(),host_pid=os.getpid()))['state'],'idle')

    def test_reconnect_retires_duplicate_membership_key(self):
        self.attach()
        old=self.runtime.pollers[KEY]
        self.identity(KEY2)
        self.op('membership.attach',session=SESSION,key=KEY2,server='nth-qweb',via='connect')
        self.wait_start(KEY2)
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            self.assertTrue(old._stop.is_set())
            self.assertNotIn(KEY,self.runtime.pollers)
            self.assertIn(KEY2,self.runtime.pollers)
            self.assertEqual(self.runtime.member(KEY)['shadow_ended'],'membership replaced')

    def test_three_holders_choose_newest_registered_live(self):
        sessions=(SESSION,SESSION2,'session-charlie')
        self.identity()
        self.store.setup_hub('nth-qweb',URL)
        for index,session in enumerate(sessions,1):
            with patch('nth_interposer_store.time.time',return_value=index*10):
                service.dispatch(self.store,registration(session))
                self.op('membership.attach',session=session,key=KEY,server='nth-qweb',via='connect')
        with self.store.lock:
            self.assertTrue(self.store.lock._is_owned())
            with self.store.db:
                self.store.db.execute("INSERT INTO sessions(session,state,registered) VALUES ('ended-holder','ended',90)")
                self.store.db.execute("INSERT INTO holdings(session,key,server,joined,attached) VALUES ('ended-holder',?,'nth-qweb',90,1)",(KEY,))
                self.store.db.execute("INSERT INTO sessions(session,state) VALUES ('imported-holder','idle_unreachable')")
                self.store.db.execute("INSERT INTO holdings(session,key,server,joined) VALUES ('imported-holder',?,'nth-qweb',100)",(KEY,))
            self.op('session.end',session=sessions[-1])
            self.assertEqual(self.runtime.member(KEY)['owner_session'],SESSION2)

    def test_pending_cap_ttl_and_setup_room(self):
        for index in range(8):
            self.store.announce(f'nth-pending-{index}',URL)
        with self.assertRaises(wire.WireError):
            self.store.announce('nth-too-many',URL)
        with self.store.db:
            self.store.db.execute('UPDATE hubs SET announced_at=0 WHERE server=?',('nth-pending-0',))
        self.store.announce('nth-fresh',URL)
        self.assertEqual(len(self.store.snapshot()['hubs']),8)
        for index in range(25):
            self.store.setup_hub(f'nth-setup-{index}',URL)
        rows=self.store.snapshot()['hubs']
        self.assertEqual(len(rows),32)
        self.assertEqual(sum(r['trust']=='setup' for r in rows),25)

    def test_trusted_cap_and_import_error_is_per_hub(self):
        for index in range(32):
            self.store.setup_hub(f'nth-setup-{index}',URL)
        with self.assertRaises(wire.WireError):
            self.store.setup_hub('nth-overflow',URL)
        with patch.object(hubs,'config_hubs',return_value=iter([('nth-extra',URL),('nth-setup-0',URL)])):
            with patch.object(self.store,'setup_hub',wraps=self.store.setup_hub) as setup_hub:
                self.store.import_hubs(log=Mock())
                self.assertEqual(setup_hub.call_args_list[-1].args,('nth-setup-0',URL))
        self.assertEqual(len(self.store.snapshot()['hubs']),32)
        # Fresh second entry must actually acquire trust after an invalid first.
        with self.store.db:self.store.db.execute('DELETE FROM hubs')
        with patch.object(hubs,'config_hubs',return_value=iter([('invalid',URL),('nth-valid',URL)])):
            self.store.import_hubs(log=Mock())
        self.assertEqual([(r['server'],r['url'],r['trust']) for r in self.store.snapshot()['hubs']],[('nth-valid',URL,'setup')])

    def test_approval_exact_url_echo_and_restricted_recheck(self):
        self.store.announce('nth-qweb',URL)
        out=io.StringIO()
        with patch('sys.stdout',out):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb','https://other.example/sse']),1)
        self.assertEqual(self.store.snapshot()['hubs'][0]['trust'],'announced')
        with patch.object(hubs.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]):
            with self.assertRaises(wire.WireError):
                self.store.approve('nth-qweb',URL)
        with patch('sys.stdout',out):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb',URL]),0)
        self.assertEqual(json.loads(out.getvalue())['url'],URL)

    def test_approved_change_survives_config_import(self):
        new='https://approved.example/sse'
        self.store.setup_hub('nth-qweb',URL)
        self.store.announce('nth-qweb',new)
        self.store.approve('nth-qweb',new)
        self.store.setup_hub('nth-qweb',URL)
        row=self.store.snapshot()['hubs'][0]
        self.assertEqual((row['url'],row['trust'],row['config_pending_url']),(new,'setup',URL))

    def test_name_in_setup_retained_entries_and_codex_overrides(self):
        spec=importlib.util.spec_from_file_location('shadow_setup',ROOT/'setup.py')
        setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
        staged=self.root/'stage';staged.mkdir()
        config={'mcpServers':{'nth-second':{'command':'python','args':['/install/nth_quartet_proxy.py','--url','https://other.example/sse']}}}
        (staged/'.claude.json').write_text(json.dumps(config))
        directory=staged/'.codex';directory.mkdir()
        (directory/'config.toml').write_text('# retained comment\n[mcp_servers.nth-second]\ncommand="python"\nargs=["/install/nth_quartet_proxy.py","--url","https://other.example/sse"]\ncustom="keep"\nenv={KEEP="yes", NTH_SERVER_NAME="wrong"}\n')
        # The CLI is stubbed; only staged files are ever installed or changed.
        with patch.object(setup.subprocess,'run',return_value=Mock(returncode=0)):
            setup.install(staged,clients=('claude','codex'),codex_binary='/fixture/codex',quartet_url=URL,skip_dependencies=True,skip_systemd=True)
        store=Store(staged/'.claude/nth/events/interposer.sqlite')
        try:
            self.assertEqual({r['server'] for r in store.snapshot()['hubs']},{'nth-qweb','nth-second'})
        finally:
            store.close()
        data=json.loads((staged/'.claude.json').read_text())
        self.assertEqual(data['mcpServers']['nth-second']['env']['NTH_SERVER_NAME'],'nth-second')
        self.assertEqual(data['mcpServers']['nth-qweb']['env']['NTH_SERVER_NAME'],'nth-qweb')
        retained=tomllib.loads((directory/'config.toml').read_text())['mcp_servers']['nth-second']
        self.assertEqual(retained['env'],{'KEEP':'yes','NTH_SERVER_NAME':'nth-second'})
        self.assertEqual(retained['custom'],'keep')
        self.assertIn('# retained comment',(directory/'config.toml').read_text())
        import nth_quartet_proxy as proxy
        with patch.dict(os.environ,retained['env']),patch.object(wire,'tell') as tell:
            _,client,_=proxy.create_server(retained['args'][-1])
            tell.assert_called_once_with('hub.announce',server='nth-second',url=retained['args'][-1])
            client.close()
        with patch.object(cli,'settings',return_value={'quartet_url':URL}):
            self.assertTrue(any('NTH_SERVER_NAME="nth-qweb"' in arg for arg in cli.mcp_overrides('unix:///fixture')))

    def test_proxy_announces_registered_name_once(self):
        import nth_quartet_proxy as proxy
        with patch.dict(os.environ,NTH_SERVER_NAME='nth-second'),patch.object(wire,'tell') as tell:
            server,client,hub=proxy.create_server(URL)
            tell.assert_called_once_with('hub.announce',server='nth-second',url=URL)
            client.close()

    def test_retained_codex_hub_env_table_variants(self):
        previous_umask=os.umask(0o022)
        self.addCleanup(os.umask,previous_umask)
        spec=importlib.util.spec_from_file_location('env_setup',ROOT/'setup.py')
        setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
        path=self.root/'config.toml'
        for env in ('', 'env={KEEP="yes"}', '[mcp_servers."nth-second".env]\nKEEP="yes"',
                    '[mcp_servers."nth-second".env]\nKEEP="yes"\nNTH_SERVER_NAME="old"'):
            with self.subTest(env=env):
                path.write_text('# comment\n[mcp_servers."nth-second"]\ncommand="python"\nargs=["nth_quartet_proxy.py","--url","'+URL+'"]\n'+env+'\n[mcp_servers.unrelated]\ncommand="keep"\n')
                path.chmod(0o600)
                before=tomllib.loads(path.read_text())['mcp_servers']['unrelated']
                setup.name_codex_hubs(path)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                data=tomllib.loads(path.read_text())
                self.assertEqual(data['mcp_servers']['nth-second']['env']['NTH_SERVER_NAME'],'nth-second')
                if env:self.assertEqual(data['mcp_servers']['nth-second']['env']['KEEP'],'yes')
                self.assertEqual(data['mcp_servers']['unrelated'],before)
                self.assertIn('# comment',path.read_text())
                original=path.read_bytes();setup.name_codex_hubs(path)
                self.assertEqual(path.read_bytes(),original)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_installed_claude_startup_and_resume_matchers_register(self):
        spec=importlib.util.spec_from_file_location('hook_setup',ROOT/'setup.py')
        setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
        staged=self.root/'startup-install'
        setup.install(staged,clients=('claude',),skip_dependencies=True,skip_systemd=True)
        settings=json.loads((staged/'.claude/settings.json').read_text())
        hooks=settings['hooks']['SessionStart']
        for source in ('startup','resume'):
            selected=[entry for entry in hooks if re.search(entry.get('matcher',''),source)]
            self.assertEqual(len(selected),1)
            with patch.object(claude,'shadow_register') as register,patch('sys.stdin',io.StringIO(json.dumps(dict(session_id=SESSION,source=source)))):
                self.assertEqual(claude.main(['start']),0)
                register.assert_called_once_with(SESSION,resume=source=='resume')

    def evidence(self, side, mid, t, key=KEY, session=SESSION):
        with patch.object(shadow.time,'time',return_value=t):
            shadow.append(side,session,'claude','rewake',[dict(key=key,server='nth-qweb',first=mid,last=mid,count=1,addressed=True)],[],1)

    def anchor(self):
        for side in ('actual','would'):
            self.evidence(side,99,0,KEY2)
            self.evidence(side,100,100,KEY2)

    def test_diff_joins_membership_owner_and_keeps_gaps(self):
        self.anchor()
        self.evidence('actual',3,20,session=SESSION)
        self.evidence('actual',7,30,session=SESSION)
        self.evidence('would',3,22,session=SESSION2)
        self.evidence('would',6,32,session=SESSION2)
        result=shadow.compare()
        self.assertEqual(result['missing_in_would'],[dict(session=SESSION2,key=KEY,first=7,last=7)])
        self.assertEqual(result['missing_in_actual'],[dict(session=SESSION2,key=KEY,first=6,last=6)])
        self.assertFalse(any(r['first']==4 or r['first']==5 for v in (result['missing_in_would'],result['missing_in_actual']) for r in v))

    def test_diff_reverse_only_exit_since_and_exact_median(self):
        self.anchor()
        for index,(actual,would) in enumerate(((10,12),(20,27),(40,49)),1):
            self.evidence('actual',index,actual)
            self.evidence('would',index,would)
        self.assertEqual(shadow.compare()['median_release_delay'],7)
        self.evidence('would',7,70)
        with patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','shadow-diff','--json']),1)
        with patch.object(shadow.time,'time',return_value=110):
            self.assertEqual(shadow.compare(since=30)['missing_in_actual'],[])

    def test_diff_common_window_margin_and_key_separation(self):
        self.anchor()
        self.evidence('actual',1,1)
        self.evidence('would',2,99)
        self.evidence('actual',5,30,KEY)
        self.evidence('would',5,30,'f'*24)
        result=shadow.compare()
        self.assertEqual((result['window']['from'],result['window']['through']),(3,97))
        self.assertEqual([r['first'] for r in result['missing_in_would']],[5])
        self.assertEqual([r['first'] for r in result['missing_in_actual']],[5])

    def test_diff_matches_pairs_across_window_and_since_boundaries(self):
        self.anchor()
        for mid,actual,would in ((17,2,4),(23,96,98),(29,49,51)):
            self.evidence('actual',mid,actual)
            self.evidence('would',mid,would)
        for since in (None,50):
            with patch.object(shadow.time,'time',return_value=100):result=shadow.compare(since)
            self.assertTrue(result['comparable'])
            self.assertEqual(result['missing_in_actual'],[])
            self.assertEqual(result['missing_in_would'],[])
            self.assertEqual(result['median_release_delay'],2)

    def test_diff_malformed_rows_and_human_json_output(self):
        self.anchor()
        path=wire.home()/'events/shadow/actual.jsonl'
        with path.open('a') as stream:
            for row in ({},[],dict(t='wrong',side='actual'),dict(t=50,side='actual',session=SESSION,client='claude',sink='rewake',lines=1,ranges=[None],ended=[])):
                stream.write(json.dumps(row)+'\n')
        shadow.compare()
        with patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['interposer','shadow-diff']),0)
            self.assertIn('Shadow comparison:',out.getvalue())
        with patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['interposer','shadow-diff','--json']),0)
            json.loads(out.getvalue())

    def test_poll_state_no_repeated_writes_and_shadow_presence(self):
        self.attach()
        self.eventually(lambda:bool(self.hub.calls))
        arguments=self.hub.calls[0][1]
        self.assertIs(arguments['monitor_heartbeat'],False)
        self.assertNotIn('monitor_filter',arguments)
        self.assertIn('after_id',arguments)
        listener=self.runtime.pollers[KEY]
        listener.stop();listener.thread.join(1)
        with self.store.lock:
            self.runtime.poll_state(KEY,'listening','',55)
            before=self.store.db.total_changes
            for _ in range(10):
                self.runtime.poll_state(KEY,'listening','',55)
            self.assertEqual(self.store.db.total_changes,before)

    def test_enable_clears_only_retryable_shadow_end(self):
        self.attach()
        self.op('membership.configure',key=KEY,enabled=False)
        for reason,expected in (('listener failure',''),('channel ended','channel ended')):
            # Re-enabling starts a live listener: fixture SQL and assertions
            # must serialize with its callbacks just like production dispatch.
            with self.store.lock:
                self.assertTrue(self.store.lock._is_owned())
                with self.store.db:
                    self.store.db.execute('UPDATE memberships SET shadow_ended=? WHERE key=?',(reason,KEY))
                self.op('membership.configure',key=KEY,enabled=True)
                self.assertEqual(self.runtime.member(KEY)['shadow_ended'],expected)

    def test_kill_switch_stops_service_pollers(self):
        self.attach()
        listener=self.runtime.pollers[KEY]
        with patch.dict(os.environ,TRIO_INTERPOSER_SHADOW='0'):
            self.runtime.reconcile()
            self.assertEqual(self.runtime.pollers,{})
            self.assertTrue(listener._stop.is_set())

    def test_session_cap_and_registered_preservation(self):
        import nth_interposer_store as storage
        with patch.object(storage,'MAX_SESSIONS',2):
            service.dispatch(self.store,registration())
            service.dispatch(self.store,registration(SESSION2))
            with self.assertRaises(wire.WireError):
                service.dispatch(self.store,registration('session-extra'))
            self.store.end(SESSION)
            service.dispatch(self.store,registration('session-extra'))
            self.assertEqual(len(self.store.snapshot()['sessions']),2)

    def test_dns_restricted_answers_announce_approve_and_connect(self):
        public=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        private=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with patch.object(hubs.socket,'getaddrinfo',return_value=public):
            self.store.announce('nth-qweb',URL)
        with patch.object(hubs.socket,'getaddrinfo',return_value=public+private):
            with self.assertRaises(wire.WireError):self.store.announce('nth-other',URL)
            with self.assertRaises(wire.WireError):self.store.approve('nth-qweb',URL)
        self.store.approve('nth-qweb',URL)
        service.dispatch(self.store,registration())
        self.identity()
        with patch.object(hubs.socket,'getaddrinfo',return_value=private):
            self.op('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect')
            self.wait_start(KEY)
        self.assertEqual(self.runtime.pollers,{})
        self.assertEqual(self.hub.calls,[])
        self.assertEqual(self.runtime.member(KEY)['poll_state'],'reconnecting')

    def test_embedded_and_unicode_metadata_hosts(self):
        for host in ('[64:ff9b::7f00:1]','[2002:7f00:1::]','168.63.129.16','192.0.0.192','１２７.０.０.１','١٢٧.٠.٠.١'):
            with self.subTest(host=host),self.assertRaises(wire.WireError):
                self.store.announce('nth-bad','http://'+host+'/sse')

    def test_transition_prefix_ranges_refused_at_all_boundaries(self):
        for address in ('64:ff9b::cb00:710a','2002:cb00:710a::','64:ff9b::a9fe:a9fe','2002:a9fe:a9fe::'):
            url='https://['+address+']/sse'
            answer=[(socket.AF_INET6,socket.SOCK_STREAM,6,'',(address,443,0,0))]
            with self.subTest(address=address),patch.object(hubs.socket,'getaddrinfo',return_value=answer):
                self.assertTrue(hubs.restricted_address(address))
                with self.assertRaises(wire.WireError):self.store.announce('nth-second',url)
                with self.store.db:
                    self.store.db.execute('DELETE FROM hubs')
                    self.store.db.execute("INSERT INTO hubs(server,url,trust,state) VALUES (?,?,'announced','pending')",('nth-second',url))
                with self.assertRaises(wire.WireError):self.store.approve('nth-second',url)
                with patch.object(hubs.socket,'socket') as create,self.assertRaises(wire.WireError):
                    hubs.connection_guard(url)((address,443))
                create.assert_not_called()
                self.store.setup_hub('nth-second',url)
                self.assertEqual(self.store.announce('nth-second',url)['trust'],'setup')

    def test_connection_guard_pins_dns_answers(self):
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        sock=Mock()
        with patch.object(hubs.socket,'getaddrinfo',return_value=answer),patch.object(hubs.socket,'socket',return_value=sock):
            self.assertIs(hubs.connection_guard(URL)(('hub.example',443),timeout=2),sock)
            sock.connect.assert_called_once_with(('203.0.113.10',443))
        client=MCPSSEClient(URL)
        client.connection_guard=Mock()
        self.assertIs(client._make_conn(1)._create_connection,client.connection_guard)
        client.close()

    def test_connection_guard_refuses_rebinding_mixed_answers_and_reconnect(self):
        public=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('203.0.113.10',443))]
        restricted=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
        guard=hubs.connection_guard(URL)
        sock=Mock()
        for answers in ([public,restricted],[public,public+restricted],[restricted], [public+restricted]):
            with self.subTest(answers=answers),patch.object(hubs.socket,'getaddrinfo',side_effect=answers),\
                 patch.object(hubs.socket,'socket') as create:
                with self.assertRaises(wire.WireError):guard(('hub.example',443))
                create.assert_not_called()
        with patch.object(hubs.socket,'getaddrinfo',side_effect=[public,public,public,restricted]),\
             patch.object(hubs.socket,'socket',return_value=sock):
            self.assertIs(guard(('hub.example',443)),sock)
            with self.assertRaises(wire.WireError):guard(('hub.example',443))
            sock.connect.assert_called_once_with(('203.0.113.10',443))
        with patch.object(hubs.socket,'getaddrinfo') as dns,patch.object(hubs.socket,'socket') as create:
            with self.assertRaises(wire.WireError):guard(('other.example',443))
            dns.assert_not_called();create.assert_not_called()

    def test_shadow_factory_propagates_connection_guard(self):
        self.store.setup_hub('nth-qweb',URL)
        fake=Mock()
        fake.connection_guard=None
        with patch('nth_sse_client.MCPSSEClient',return_value=fake):
            poll,close=self.runtime.poll_factory(dict(source='quartet',url=URL))
        self.assertTrue(callable(fake.connection_guard))
        close()

    def test_config_public_host_rebinding_is_still_refused(self):
        self.store.setup_hub('nth-qweb',URL)
        self.identity();service.dispatch(self.store,registration())
        answer=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('169.254.169.254',443))]
        with patch.object(hubs.socket,'getaddrinfo',return_value=answer):
            with self.assertRaises(wire.WireError):self.store.announce('nth-qweb',URL)
            self.op('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect')
            self.wait_start(KEY)
        self.assertEqual(self.runtime.pollers,{})

    def test_sse_endpoint_requires_exact_origin(self):
        client=MCPSSEClient(URL)
        for endpoint in ('https://other.example/messages','http://hub.example/messages','https://hub.example:444/messages','//other.example/messages',
                         'https://user@hub.example/messages','https://user:password@hub.example/messages'):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):
                client._handle_event('endpoint',endpoint)
        self.assertIsNone(client.endpoint_url)
        self.assertFalse(client.endpoint_ready.is_set())
        client._handle_event('endpoint','https://HUB.EXAMPLE:443/messages')
        self.assertTrue(client.endpoint_ready.is_set())
        for endpoint in ('https://other.example/messages','https://user:password@hub.example/messages'):
            client.endpoint_url=endpoint
            with patch.object(client,'_make_conn') as connect,self.assertRaises(ValueError):
                client._post({'session_token':cases.SECRET})
            connect.assert_not_called()
        client.close()

    def test_fifo_symlink_oversize_quarantine_and_tmp_cleanup(self):
        inbox=wire.private_dir(wire.home()/'events/inbox')
        os.mkfifo(inbox/'fifo.json')
        target=self.root/'valid-request'
        data=wire.encode_frame(registration('session-linked'))
        target.write_bytes(data)
        (inbox/'link.json').symlink_to(target)
        (inbox/'large.json').write_bytes(b'x'*(wire.MAX_FRAME+1))
        tmp=inbox/'old.tmp';tmp.write_text('partial');os.utime(tmp,(0,0))
        wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
        before=time.monotonic();self.runtime.drain()
        self.assertLess(time.monotonic()-before,.5)
        self.assertEqual({p.name for p in (inbox/'bad').glob('*.json')},{'fifo.json','link.json','large.json'})
        self.assertFalse(tmp.exists())
        self.assertEqual(self.store.session(SESSION)['state'],'idle')
        self.assertNotIn('session-linked',[r['session'] for r in self.store.snapshot()['sessions']])
        self.assertEqual(target.read_bytes(),data)

    def test_readable_fifo_refused_before_reading_valid_frame(self):
        path=wire.private_dir(wire.home()/'events/inbox')/'readable.json'
        os.mkfifo(path)
        fd=os.open(path,os.O_RDWR|os.O_NONBLOCK)
        try:
            os.write(fd,wire.encode_frame(registration('session-fifo')))
            self.runtime.drain()
            self.assertTrue((path.parent/'bad'/path.name).exists())
            self.assertNotIn('session-fifo',[r['session'] for r in self.store.snapshot()['sessions']])
        finally:os.close(fd)

    def test_shadow_fifo_paths_never_delay_actual_delivery_or_would_release(self):
        directory=wire.private_dir(wire.home()/'events/shadow')
        for side in ('actual','would'):
            path=directory/(side+'.jsonl');os.mkfifo(path)
            script='''import sys,io,os,logging
sys.path.insert(0,sys.argv[1])
import nth_claude_hook as hook
import nth_interposer_shadow as shadow
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime
if sys.argv[2]=='actual':
    hook.SAY=sys.stderr
    sink=hook.StderrSink()
    sink.shadow_record=('session-alpha',[],[],1)
    assert sink.deliver(['synthetic live notice'])
else:
    store=Store()
    store.register(dict(session='session-alpha',client='claude',host_pid=None,sink='rewake',host_ok=True,problem=''))
    runtime=Runtime(store,logging.getLogger('test'))
    runtime.accumulate(dict(owner_session='session-alpha',key='0123456789abcdef01234567',source='local',channel='room',member_id='member'),[dict(id=1,mentioned=True)])
    runtime.release('session-alpha',force=True)
    assert runtime.buffers
    store.close()
'''
            try:
                completed=subprocess.run([sys.executable,'-c',script,str(ROOT/'server'),side],capture_output=True,text=True,timeout=2)
                self.assertEqual(completed.returncode,0,completed.stderr)
                if side=='actual':self.assertEqual(completed.stderr,'synthetic live notice\n')
            finally:path.unlink()

    def test_shadow_append_rejects_readable_fifo_descriptor(self):
        path=wire.private_dir(wire.home()/'events/shadow')/'actual.jsonl'
        os.mkfifo(path)
        fd=os.open(path,os.O_RDWR|os.O_NONBLOCK)
        try:
            self.assertFalse(shadow.append('actual',SESSION,'claude','rewake',[],[],1))
            with self.assertRaises(BlockingIOError):os.read(fd,4096)
        finally:os.close(fd)

    def test_drain_replace_race_keeps_processing(self):
        inbox=wire.private_dir(wire.home()/'events/inbox')
        (inbox/'000-bad.json').write_text('bad')
        wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
        with patch('nth_interposer_runtime.os.replace',side_effect=FileNotFoundError):
            self.runtime.drain()
        self.assertEqual(self.store.session(SESSION)['state'],'idle')

    def test_tick_five_second_drain_cadence(self):
        self.runtime.last_drain=100
        self.runtime.last_death=100
        with patch.object(self.runtime,'drain') as drain,patch.object(self.runtime,'reconcile'):
            for current,expected in ((104.9,0),(105,1),(109.9,1),(110,2)):
                with patch('nth_interposer_runtime.time.monotonic',return_value=current):self.runtime.tick()
                self.assertEqual(drain.call_count,expected)

    def test_inbox_after_service_readiness_is_applied(self):
        self.start_socket()
        # This is fallback work written after hello, not pre-start import data.
        with wire.connect() as client:self.assertIn('hubs',client.call('list'))
        inbox=wire.private_dir(wire.home()/'events/inbox')
        (inbox/'later.json').write_bytes(wire.encode_frame(registration()))
        with patch('nth_interposer_runtime.time.monotonic',return_value=105):
            self.runtime.last_drain=100;self.runtime.last_death=100;self.runtime.tick()
        self.assertEqual(self.store.session(SESSION)['state'],'idle')
        self.assertFalse((inbox/'later.json').exists())

    def test_real_service_drains_work_queued_after_readiness(self):
        process=subprocess.Popen([sys.executable,str(ROOT/'server/nth_interposer.py'),'serve','--idle-seconds','10'],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                try:
                    with wire.connect(timeout=.1) as client:
                        self.assertEqual(client.call('list')['sessions'],[])
                    break
                except (OSError,EOFError):time.sleep(.01)
            else:self.fail('service did not become ready')
            time.sleep(.2)  # Ensure startup drain has finished before creating fallback work.
            with patch.object(wire,'socket_path',return_value=self.root/'absent.sock'):
                wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
            files=list((wire.home()/'events/inbox').glob('*.json'))
            self.assertEqual(len(files),1)
            deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                with wire.connect(timeout=.3) as client:
                    rows=client.call('list')['sessions']
                if rows:
                    self.assertEqual(rows[0]['session'],SESSION)
                    if not files[0].exists():
                        break
                time.sleep(.03)
            else:self.fail('post-readiness fallback was never drained')
        finally:
            process.terminate();process.wait(timeout=3)

    def test_tell_no_spawn_both_paths_and_no_reply_replay(self):
        with patch.object(wire,'_spawn') as spawn,patch.object(wire.subprocess,'Popen') as popen:
            wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
            self.runtime.drain();self.start_socket()
            self.assertTrue(wire.tell('turn',session=SESSION,phase='started'))
            self.assertFalse(wire.tell('turn',session='unknown-holder',phase='ended'))
            self.assertFalse(list((wire.home()/'events/inbox').glob('*.json')))
            spawn.assert_not_called();popen.assert_not_called()

    def test_inbox_contention_longer_than_old_lock_budget_is_retained(self):
        inbox=wire.private_dir(wire.home()/'events/inbox')
        ready=threading.Event()
        def hold():
            with wire.file_lock(inbox/'write.lock'):
                ready.set();time.sleep(.08)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(ready.wait(1))
        try:
            wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')})
        finally:thread.join(1)
        self.assertEqual(len(list(inbox.glob('*.json'))),1)

    def test_contended_hung_batch_keeps_whole_hook_under_one_second(self):
        self.joined()
        inbox=wire.private_dir(wire.home()/'events/inbox')
        ready,unlock=threading.Event(),threading.Event()
        def hold():
            with wire.file_lock(inbox/'write.lock'):
                ready.set();unlock.wait(2)
        writer=threading.Thread(target=hold);writer.start();self.assertTrue(ready.wait(1))
        sock,reader,stop,_=self.hung_server(None)
        try:
            before=time.monotonic()
            with wire.observation():
                for phase in ('started','ended','started'):
                    self.assertFalse(wire.tell('turn',session=SESSION,phase=phase))
            self.assertLess(time.monotonic()-before,1)
        finally:
            unlock.set();writer.join(1);stop.set();reader.join(3);sock.close()

    def test_never_joined_turn_is_not_queued(self):
        self.assertFalse(wire.tell('turn',session=SESSION,phase='ended'))
        self.assertFalse((wire.home()/'events/inbox').exists())

    def hung_server(self, hello_delay):
        wire.private_dir(wire.socket_path().parent)
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        sock.bind(str(wire.socket_path()));sock.listen()
        stop=threading.Event();received=threading.Event()
        def serve():
            peer,_=sock.accept()
            try:
                with peer.makefile('rb') as reader:
                    wire.read_frame(reader)
                    if hello_delay is not None:
                        stop.wait(hello_delay)
                        peer.sendall(wire.encode_frame(dict(v=1,id=0,ok=dict(protocol_min=1,protocol_max=1))))
                        wire.read_frame(reader);received.set()
                    stop.wait(2)
            finally:peer.close()
        thread=threading.Thread(target=serve);thread.start()
        return sock,thread,stop,received

    def test_reply_half_second_limit_and_shared_handshake_deadline(self):
        for delay in (None,.35):
            sock,thread,stop,received=self.hung_server(delay)
            try:
                before=time.monotonic()
                self.assertFalse(wire.tell('session.register',**{k:v for k,v in registration().items() if k not in ('v','id','op')}))
                elapsed=time.monotonic()-before
                self.assertGreater(elapsed,.42)
                self.assertLess(elapsed,.67)
                if delay is not None:self.assertTrue(received.is_set())
            finally:
                stop.set();thread.join(3);sock.close();wire.socket_path().unlink()

    def test_connection_capacity_release_and_admission(self):
        wire.private_dir(wire.socket_path().parent)
        with patch.object(service,'MAX_CONNECTIONS',2):
            server=service.Server(wire.socket_path(),self.store,Mock())
        pairs=[socket.socketpair() for _ in range(3)]
        try:
            with patch.object(socketserver.ThreadingMixIn,'process_request') as parent,patch.object(server,'shutdown_request') as shutdown:
                for left,right in pairs:server.process_request(right,None)
                self.assertEqual(parent.call_count,2)
                shutdown.assert_called_once_with(pairs[2][1])
                with patch.object(socketserver.ThreadingMixIn,'process_request_thread'):
                    server.process_request_thread(pairs[0][1],None)
                server.process_request(pairs[2][1],None)
                self.assertEqual(parent.call_count,3)
        finally:
            server.server_close()
            for left,right in pairs:left.close();right.close()

    def test_tick_exception_does_not_stop_service(self):
        # serve() logs only the exception class and still reaches its idle exit.
        with patch.object(Runtime,'tick',side_effect=RuntimeError(cases.SECRET)):
            service.serve(idle_seconds=.15)
        text=(wire.home()/'logs/interposer.log').read_text()
        self.assertIn('shadow tick failed: RuntimeError',text)
        self.assertNotIn(cases.SECRET,text)
        self.assertIn('service idle exit',text)

    def test_approve_handles_sqlite_error(self):
        with patch('nth_interposer_store.Store',side_effect=sqlite3.OperationalError('synthetic')),patch('sys.stderr',io.StringIO()):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb',URL]),1)

    def test_live_host_survives_registration_without_pid(self):
        service.dispatch(self.store,dict(registration(),host_pid=os.getpid()))
        self.store.end(SESSION)
        self.assertEqual(service.dispatch(self.store,registration())['state'],'idle')
        self.assertEqual(self.store.session(SESSION)['host_pid'],os.getpid())

    def test_tool_observation_follows_legacy_hook_work(self):
        payload=dict(session_id=SESSION,tool_name='mcp__nth_qweb__quartet_connect',tool_response='{}')
        order=[]
        with patch('sys.stdin',io.StringIO(json.dumps(payload))),patch('sys.stderr',io.StringIO()),\
             patch.object(codex,'codex_host',return_value=(os.getpid(),'')),patch.object(codex,'trio_server_pid',return_value=None),\
             patch.object(claude,'register',side_effect=lambda *a,**k:order.append('register')) as register,\
             patch.object(codex,'arm',side_effect=lambda *a:order.append('arm')),\
             patch.object(claude,'shadow_tool',side_effect=lambda *a:order.append('observe')):
            codex.main(['tool'])
        self.assertEqual(order,['register','arm','observe'])
        self.assertIs(register.call_args.kwargs['observe'],False)

    def test_actual_range_snapshot_is_copied_under_wake_lock(self):
        self.joined();self.identity(source='local',url='/fixture/nth.db')
        real_wake=claude.Wake
        class GuardLock:
            def __init__(self):self.lock=threading.Lock();self.held=False
            def __enter__(self):self.lock.acquire();self.held=True;return self
            def __exit__(self,*args):self.held=False;self.lock.release()
        class LockedList(list):
            def __init__(self,guard):super().__init__();self.guard=guard
            def __iter__(self):
                if not self.guard.held:raise AssertionError('snapshot outside wake.lock')
                return super().__iter__()
        def wake(bucket):
            result=real_wake(bucket);result.lock=GuardLock()
            result.shadow_ranges=LockedList(result.lock);result.shadow_ended=LockedList(result.lock)
            return result
        actual=cases.ScriptedHub([dict(event='new_messages',messages=[message(7,mentioned=True)])])
        with patch.object(claude,'Wake',side_effect=wake),patch.object(claude,'poll_factory',actual.factory),\
             patch.object(claude,'SAY',io.StringIO()),patch.multiple(claude,TICK_SECONDS=.01,SETTLE_SECONDS=.01,UNSUPERVISED_LIFETIME_SECONDS=.2):
            self.assertEqual(claude.wait(SESSION),2)
        self.assertEqual(len(shadow.records('actual')),1)

    def test_old_schema_v1_v2_migrate_without_automatic_trust(self):
        schema='''CREATE TABLE hubs(server TEXT PRIMARY KEY,url TEXT,announced_at REAL,state TEXT,since REAL,error TEXT);
        CREATE TABLE memberships(key TEXT PRIMARY KEY,source TEXT,url TEXT,channel TEXT,member_id TEXT,filter TEXT DEFAULT 'about',enabled INT DEFAULT 1,ended TEXT DEFAULT '',owner_session TEXT,announced_through INT DEFAULT 0,acked_through INT DEFAULT 0,poll_state TEXT,poll_error TEXT,last_ok REAL);
        CREATE TABLE sessions(session TEXT PRIMARY KEY,client TEXT,sink TEXT,host_pid INT,host_stamp INT,state TEXT,host_ok INT,problem TEXT,registered REAL,last_wake REAL,wakes_hour INT);
        CREATE TABLE holdings(session TEXT,key TEXT,server TEXT,joined REAL,PRIMARY KEY(session,key));
        CREATE TABLE deliveries(id INTEGER PRIMARY KEY,session TEXT,sink TEXT,body TEXT,ranges TEXT,state TEXT,created REAL,done REAL);
        CREATE TABLE appserver_spool(key TEXT,message_id INT,payload TEXT,state TEXT,turn_id TEXT,PRIMARY KEY(key,message_id));
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'''
        for version in (1,2):
            directory=wire.private_dir(self.root/('upgrade-'+str(version)))
            path=directory/'interposer.sqlite'
            db=sqlite3.connect(path);db.executescript(schema)
            db.execute("INSERT INTO hubs VALUES ('nth-qweb',?,1,'announced',1,'')",(URL,))
            db.execute('INSERT INTO memberships(key,source,url,channel,member_id,owner_session,announced_through,acked_through) VALUES (?,?,?,?,?,?,9,4)',(KEY,'quartet',URL,'room','member',SESSION))
            db.execute("INSERT INTO sessions(session,client,state) VALUES (?,'claude','idle_unreachable')",(SESSION,))
            db.execute("INSERT INTO holdings VALUES (?,?,'nth-qweb',10)",(SESSION,KEY))
            if version==2:db.executescript("ALTER TABLE hubs ADD COLUMN pending_url TEXT DEFAULT ''; ALTER TABLE hubs ADD COLUMN trust TEXT DEFAULT 'announced'; UPDATE hubs SET state='pending';")
            db.execute('PRAGMA user_version='+str(version));db.commit();db.close()
            upgraded=Store(path)
            probe=Runtime(upgraded,Mock(),self.hub.factory)
            try:
                row=upgraded.snapshot()['hubs'][0]
                self.assertEqual((row['trust'],row['state']),('announced','pending'))
                self.assertEqual(upgraded.db.execute('PRAGMA user_version').fetchone()[0],3)
                upgraded.register(registration())
                probe.reconcile()
                self.assertEqual(probe.pollers,{})
                member=upgraded.snapshot()['memberships'][0]
                self.assertEqual((member['announced_through'],member['acked_through']),(9,4))
                self.assertEqual((member['shadow_announced_through'],member['shadow_acked_through']),(0,0))
                identity=wire.private_dir(directory/'identities')/(KEY+'.json')
                identity.write_text(json.dumps(dict(source='quartet',url=URL,channel='room',member_id='member',session_token=cases.SECRET)))
                with self.assertRaises(wire.WireError):upgraded.attach(request('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect'))
                upgraded.approve('nth-qweb',URL)
                self.assertEqual(upgraded.attach(request('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect'))['owner'],SESSION)
                probe.reconcile()
                self.wait_start(KEY,probe)
                self.assertIn(KEY,probe.pollers)
            finally:
                probe.close();upgraded.close()
            again=Store(path)
            try:self.assertEqual(again.snapshot()['hubs'][0]['trust'],'setup')
            finally:again.close()


if __name__=='__main__':unittest.main()
