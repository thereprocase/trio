"""Opt-in wake contract: fake tmux, real store/runtime, isolated private state."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime
from nth_interposer_wire import validate_request, WireError
from nth_interposer_tmux import TmuxSink, idle_footer, notice
from nth_claude_hook import process_stamp

class SinkTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.home.chmod(0o700)
  self.env=patch.dict(os.environ,{'NTH_HOME':str(self.home),'NTH_INTERPOSER_TEST_RUNTIME':str(self.home)})
  self.env.start();self.store=Store();self.calls=[];self.footer='❯\n\nS2 20%'
  self.req=dict(v=1,id=1,op='session.register',session='session-test',client='claude',host_pid=os.getpid(),sink='tmux',host_ok=True,problem='',pane='%123')
  validate_request(self.req);self.store.register(self.req)
  self.sink=TmuxSink(self.store,run=self.run_tmux)
 def tearDown(self):self.store.close();self.env.stop();self.tmp.cleanup()
 def run_tmux(self,*args):
  self.calls.append(args)
  if args[0]=='display-message':return f'{os.getpid()} claude'
  if args[0]=='capture-pane':return self.footer
  return ''
 def enqueue(self):
  with self.store.lock,self.store.db:self.sink.enqueue('session-test',[dict(source='quartet',channel='room',member_id='member',count=2,addressed=True,content='DO NOT COPY ME')])
 def state(self):return self.store.db.execute('SELECT state FROM deliveries').fetchone()[0]
 def test_busy_then_idle_after_ten_minutes(self):
  self.enqueue();self.store.db.execute('UPDATE deliveries SET created=?',(time.time()-600,));self.store.db.commit()
  self.footer='❯\nThinking… (esc to interrupt)';self.sink.drain();self.assertEqual(self.state(),'queued')
  self.footer='❯\n';self.sink.drain();self.assertEqual(self.state(),'written')
  sends=[c for c in self.calls if c[0]=='send-keys'];self.assertEqual(len(sends),2)
  self.assertEqual(sends[0][:5],('send-keys','-t','%123','-l','--'));self.assertNotIn('DO NOT COPY ME',sends[0][-1]);self.assertEqual(sends[1][-1],'Enter')
  self.sink.drain();self.assertEqual(len([c for c in self.calls if c[0]=='send-keys']),2)
 def test_current_claude_spinner_above_six_line_prompt_defers(self):
  self.enqueue()
  tail='\n'.join(['separator','❯','separator','route','folder','status'])
  self.footer='✽ Working… (2m 5s · ↓ 2k tokens)\n'+tail
  self.sink.drain();self.assertEqual(self.state(),'queued')
  self.assertFalse(any(c[0]=='send-keys' for c in self.calls))
  self.footer=tail
  self.sink.drain();self.assertEqual(self.state(),'written')
 def test_nonempty_input_and_shell_never_receive(self):
  self.enqueue();self.footer='❯ do not submit';self.sink.drain();self.assertEqual(self.state(),'queued')
  self.sink.run=lambda *a:f'{os.getpid()} bash';self.sink.drain();self.assertEqual(self.state(),'queued')
 def test_partial_send_is_not_replayed(self):
  self.enqueue();old=self.sink.run
  def fail(*args):
   if args[0]=='send-keys' and args[-1]=='Enter':raise subprocess.TimeoutExpired('tmux',2)
   return old(*args)
  self.sink.run=fail;self.sink.drain();self.assertEqual(self.state(),'uncertain')
  self.sink.run=old;self.sink.drain();self.assertEqual(self.state(),'uncertain')
 def test_end_cancels_pending(self):
  self.enqueue();self.store.end('session-test');self.sink.drain();self.assertEqual(self.state(),'cancelled')
 def test_legacy_registration_cannot_remove_explicit_sink(self):
  req=dict(self.req,sink='rewake');req.pop('pane');self.store.register(req)
  self.assertEqual(self.store.session('session-test')['sink'],'tmux')
 def test_wrong_pid_does_not_send(self):
  self.enqueue();self.sink.run=lambda *args:'99999 claude';self.sink.drain();self.assertEqual(self.state(),'queued')
 def test_protocol_rejects_pane_injection(self):
  for pane in ['%1;evil','abc','-t','%1\n']:
   with self.assertRaises(WireError):validate_request(dict(self.req,pane=pane))
 def test_protocol_requires_live_binding(self):
  with self.assertRaises(WireError):validate_request(dict(self.req,host_pid=None))
 def test_fifo_order_one_notice_per_session_per_poll(self):
  self.enqueue();self.enqueue();self.sink.drain()
  self.assertEqual([r[0] for r in self.store.db.execute('SELECT state FROM deliveries ORDER BY id')],['written','queued'])
 def test_restart_keeps_queued_notice(self):
  self.enqueue();other=TmuxSink(self.store,run=self.run_tmux);other.drain();self.assertEqual(self.state(),'written')
 def test_footer_contract(self):
  self.assertTrue(idle_footer('›\nctx 10%','codex'))
  self.assertTrue(idle_footer('❯\naccept edits on','claude'))
  for text in ['old ❯\nhello','❯ editing','❯\nAllow once','❯\nesc to cancel']:
   self.assertFalse(idle_footer(text,'claude'))
 def test_runtime_moves_committed_buffer_to_durable_outbox(self):
  key='0123456789abcdef01234567'
  with self.store.db:
   self.store.db.execute("INSERT INTO memberships(key,source,url,channel,member_id,owner_session) VALUES (?,?,?,?,?,?)",(key,'local','local','room','member','session-test'))
   self.store.db.execute("INSERT INTO holdings(session,key,server,joined,attached) VALUES (?,?,?,?,1)",('session-test',key,'nth-trio',time.time()))
  runtime=Runtime(self.store,logging.getLogger('test'))
  with self.store.lock,runtime.buffer_transaction():runtime.accumulate(runtime.member(key),[{'id':1,'mentioned':True}])
  self.assertIn('session-test',runtime.buffers)
  runtime.release('session-test',force=True)
  self.assertEqual(self.state(),'queued');self.assertNotIn('session-test',runtime.buffers)
  self.assertFalse(self.store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'shadow_pending:%'").fetchone())
  runtime.close();self.sink.drain();self.assertEqual(self.state(),'written')
 def test_projection_is_single_line(self):
  text=notice([dict(source='quartet',channel='x\nEVIL',count=1,addressed=False)])
  self.assertNotIn('\n',text)

if __name__=='__main__':unittest.main()
