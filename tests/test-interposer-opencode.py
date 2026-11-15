"""Real durable store with synthetic SDK admission acknowledgements."""
import importlib.util, os, sys, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
spec=importlib.util.spec_from_file_location('tmux_tests',ROOT/'tests/test-interposer-tmux.py')
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
from nth_interposer_opencode import enqueue, dispatch
from nth_interposer_wire import validate_request, WireError

class SDKTests(base.SinkTests):
 def setUp(self):
  super().setUp()
  self.store.end('session-test')
  self.req.update(client='opencode',sink='opencode',resume=True);self.req.pop('pane')
  validate_request(self.req);self.store.register(self.req)
 def claim(self, **extra):
  req=dict(v=1,id=1,op='wait',session='session-test',host_pid=os.getpid(),**extra)
  validate_request(req)
  with self.store.lock:return dispatch(self.store,req)
 def enqueue(self):
  with self.store.lock,self.store.db:enqueue(self.store,'session-test',[dict(source='quartet',channel='room',member_id='m',first_id=4,last_id=5,count=2,addressed=True,content='PRIVATE BODY')])
 def test_sdk_busy_idle_admission_is_at_most_once(self):
  self.enqueue();self.store.turn(dict(session='session-test',phase='started'))
  self.assertIsNone(self.claim()['delivery'])
  self.store.turn(dict(session='session-test',phase='ended'))
  delivery=self.claim()['delivery'];self.assertNotIn('PRIVATE',delivery['notice'])
  self.assertIsNone(self.claim()['delivery'])
  req=dict(v=1,id=2,op='delivered',session='session-test',host_pid=os.getpid(),delivery_id=delivery['id'],outcome='written')
  validate_request(req)
  with self.store.lock:dispatch(self.store,req);dispatch(self.store,req)
  self.assertEqual(self.state(),'written')
 def test_lost_sdk_reply_is_never_replayed(self):
  self.enqueue();self.claim();self.assertIsNone(self.claim()['delivery'])
  self.assertEqual(self.state(),'sending')
 def test_runtime_routes_membership_notice_to_sdk_outbox(self):
  import logging,time
  from nth_interposer_runtime import Runtime
  key='0123456789abcdef01234567'
  with self.store.db:
   self.store.db.execute("INSERT INTO memberships(key,source,url,channel,member_id,owner_session) VALUES (?,?,?,?,?,?)",(key,'local','local','room','member','session-test'))
   self.store.db.execute("INSERT INTO holdings(session,key,server,joined,attached) VALUES (?,?,?,?,1)",('session-test',key,'nth-trio',time.time()))
  runtime=Runtime(self.store,logging.getLogger('test'))
  with self.store.lock,runtime.buffer_transaction():runtime.accumulate(runtime.member(key),[{'id':1,'mentioned':True}])
  runtime.release('session-test',force=True)
  self.assertEqual(self.state(),'queued');self.assertNotIn('session-test',runtime.buffers)
  runtime.close()
  self.assertRegex(self.claim()['delivery']['notice'],r'1 new trio message .* in channel room\.')
 def test_ready_requires_live_sdk_consumer_and_listener(self):
  import nth_event_access as access
  import time
  from unittest.mock import patch,MagicMock
  key='0123456789abcdef01234567'
  with self.store.db:self.store.db.execute("INSERT INTO memberships(key,source,url,channel,member_id,owner_session,poll_state,last_ok) VALUES (?,?,?,?,?,?,?,?)",(key,'local','local','room','member','session-test','listening',time.time()))
  client=MagicMock();client.__enter__.return_value=client
  client.call.side_effect=lambda *a,**k:self.store.snapshot(key=key)
  with patch.dict(os.environ,{'TRIO_NATIVE_CLIENT':'opencode'}),patch('nth_claude_hook.identity_key_for',return_value=key),patch('nth_interposer_wire.connect',return_value=client):
   self.assertFalse(access.delivery_status('room','member','synthetic')['ready'])
   self.claim()
   self.assertTrue(access.delivery_status('room','member','synthetic')['ready'])
   with self.store.db:self.store.db.execute("UPDATE meta SET value='0' WHERE key='opencode_poll:session-test'")
   self.assertFalse(access.delivery_status('room','member','synthetic')['ready'])
 def test_wrong_host_cannot_claim(self):
  self.enqueue()
  with self.store.lock,self.assertRaises(WireError):dispatch(self.store,dict(op='wait',session='session-test',host_pid=os.getpid()+100000))
 def test_legacy_registration_cannot_remove_explicit_sdk_sink(self):
  self.store.register(dict(self.req,client='claude',sink='rewake'))
  self.assertEqual(self.store.session('session-test')['sink'],'opencode')

# Reuse setup/helpers, not the tmux-specific test cases.
for name in dir(base.SinkTests):
 if name.startswith('test_') and name not in SDKTests.__dict__:setattr(SDKTests,name,None)
if __name__=='__main__':unittest.main()
