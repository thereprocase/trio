"""Opt-in wake contract: fake tmux, real store/runtime, isolated private state."""
import re
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime
from nth_interposer_wire import validate_request, WireError
from nth_interposer_tmux import TmuxSink, idle_footer, notice
from nth_notice import batch_notice_lines

RULE='─'*40
# Captured-style screens (ANSI removed, trailing spaces kept where clients draw them).
CLAUDE_IDLE='  ⎿  Done\n\n'+RULE+'\n❯ \n'+RULE+'\n  ⏵⏵ accept edits on (shift+tab to cycle)'
CLAUDE_WORKING='✻ Thinking… (12s · ↑ 1.2k tokens · esc to interrupt)\n\n'+RULE+'\n❯ \n'+RULE+'\n  ? for shortcuts'
CLAUDE_TODO_WORKING='✶ Running tests… (2m 5s · esc to interrupt)\n  ⎿  ☐ fix\n     ☐ test\n\n'+RULE+'\n❯ \n'+RULE+'\n  ? for shortcuts'
CLAUDE_COMPACTING='✻ Compacting conversation… (34s · esc to interrupt)\n\n'+RULE+'\n❯ \n'+RULE+'\n  ? for shortcuts'
CLAUDE_APPROVAL=(' Bash command\n   ls\n Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don\'t ask again\n'
 '   3. No, and tell Claude what to do differently (esc)')
CLAUDE_ANSWER_WORDS=('⏺ I was thinking about this change; compacting is not needed.\n'
 '  Quoted earlier: ✽ Working… (2m 5s · ↓ 2k tokens · esc to interrupt)\n  Allow once, esc to cancel.\n\n'+CLAUDE_IDLE)
CLAUDE_MULTILINE='\n'+RULE+'\n❯ \n  half-written second line\n'+RULE+'\n  ? for shortcuts'
CODEX_IDLE='• Done.\n\n› \n\n  100% context left · ? for shortcuts'
CODEX_WORKING='• Working (12s • esc to interrupt)\n\n› \n\n  100% context left · ? for shortcuts'
CODEX_COMPACTING='• Compacting context (3s • esc to interrupt)\n\n› \n\n  98% context left'
CODEX_APPROVAL=('  Would you like to run the following command?\n  $ ls\n› 1. Yes, proceed (y)\n'
 '  2. No, and tell Codex what to do differently (esc)\n\n  Press enter to confirm or esc to cancel')
CODEX_ANSWER_WORDS='• I was thinking: the old status read (12s) while working.\n\n› \n\n  100% context left · ? for shortcuts'
CODEX_MULTILINE='\n› \n  half-written second line\n\n  100% context left'
# Reduced from `tmux capture-pane -p -e` output of live clients (2026-11): escapes and
# layout kept, model name, directory and account details replaced.
REAL_CODEX_FOOT=('\n\n  \x1b[38;2;246;226;183mmodel-name high\x1b[39m · \x1b[38;2;171;223;167m~/project\x1b[39m\n')
REAL_CODEX_IDLE=('• Done.\n\n\x1b[1m\x1b[38;2;186;130;255m»\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m'+REAL_CODEX_FOOT+
 '  \x1b[1m←\x1b[0m for agents · \x1b[1m?\x1b[0m for shortcuts'+' '*40+'⚠ \x1b[38;2;196;167;103m1 warning\x1b[39m · \x1b[1mf2\x1b[0m to view')
REAL_CODEX_DRAFT=('• Done.\n\n\x1b[1m\x1b[38;2;186;130;255m»\x1b[0m draft text here'+REAL_CODEX_FOOT+
 ' '*60+'⚠ \x1b[38;2;196;167;103m1 warning\x1b[39m · \x1b[1mf2\x1b[0m to view')
REAL_CODEX_SECOND_LINE=('\x1b[1m\x1b[38;2;186;130;255m»\x1b[0m \n  second line of a draft'+REAL_CODEX_FOOT+
 '  \x1b[1m←\x1b[0m for agents · \x1b[1m?\x1b[0m for shortcuts')
REAL_CLAUDE_BOX=('\x1b[38;5;244m'+RULE+'\n\x1b[39m❯\xa0{}\n\x1b[38;5;244m'+RULE+'\n\x1b[39m  \x1b[1m\x1b[38;5;157muser\x1b[0m\x1b[38;5;246m │ model │ 0s\n'
 '  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m')
REAL_CLAUDE_IDLE='  reply text\n\n\x1b[38;5;246m✻ Baked for 2s · done 8:37 AM\x1b[39m\n\n'+REAL_CLAUDE_BOX.format('')
REAL_CLAUDE_DRAFT=REAL_CLAUDE_BOX.format('draft text here')
REAL_CLAUDE_HINT=REAL_CLAUDE_BOX.format('\x1b[2mTry "fix the failing test"\x1b[0m')
REAL_CLAUDE_WORKING=('  ⎿  $ sleep 25\n\n\x1b[38;5;215m✢\x1b[39m \x1b[38;5;215mProofing\x1b[38;5;222m…\x1b[38;5;215m \x1b[38;5;246m(4s · ↓\x1b[39m '
 '\x1b[38;5;246m82 tokens · \x1b[38;5;249mthinking\x1b[38;5;246m)\x1b[39m\n        a tip line under the spinner\n\n'+REAL_CLAUDE_BOX.format(''))
ITEM=dict(source='quartet',channel='room',member_id='member',first_id=4,last_id=5,count=2,addressed=True,
 reason='',server='quartet-a',content='DO NOT COPY ME')

class SinkCase(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name);self.home.chmod(0o700)
  self.env=patch.dict(os.environ,{'NTH_HOME':str(self.home),'NTH_INTERPOSER_TEST_RUNTIME':str(self.home)})
  self.env.start();self.store=Store();self.calls=[];self.footer=CLAUDE_IDLE
  self.req=dict(v=1,id=1,op='session.register',session='session-test',client='claude',host_pid=os.getpid(),sink='tmux',host_ok=True,problem='',pane='%123')
  validate_request(self.req);self.store.register(self.req)
  self.sink=TmuxSink(self.store,run=self.run_tmux)
 def tearDown(self):self.store.close();self.env.stop();self.tmp.cleanup()
 def run_tmux(self,*args):
  self.calls.append(args)
  if args[0]=='display-message':return f'{os.getpid()} claude'
  if args[0]=='capture-pane':return self.footer
  return ''
 def enqueue(self,session='session-test'):
  with self.store.lock,self.store.db:self.sink.enqueue(session,[ITEM])
 def state(self):return self.store.db.execute('SELECT state FROM deliveries').fetchone()[0]

class SinkTests(SinkCase):
 def test_busy_then_idle_after_ten_minutes(self):
  self.enqueue();self.store.db.execute('UPDATE deliveries SET created=?',(time.time()-600,));self.store.db.commit()
  self.footer=CLAUDE_WORKING;self.sink.drain();self.assertEqual(self.state(),'queued')
  self.footer=CLAUDE_IDLE;self.sink.drain();self.assertEqual(self.state(),'written')
  sends=[c for c in self.calls if c[0]=='send-keys'];self.assertEqual(len(sends),2)
  self.assertEqual(sends[0][:5],('send-keys','-t','%123','-l','--'));self.assertNotIn('DO NOT COPY ME',sends[0][-1]);self.assertEqual(sends[1][-1],'Enter')
  self.sink.drain();self.assertEqual(len([c for c in self.calls if c[0]=='send-keys']),2)
 def test_current_claude_spinner_above_six_line_prompt_defers(self):
  self.enqueue()
  tail='\n'.join([RULE,'❯',RULE,'  route','  folder','  status'])
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
  self.assertTrue(idle_footer(CODEX_IDLE,'codex'))
  self.assertTrue(idle_footer(CLAUDE_IDLE,'claude'))
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
  text=notice([dict(ITEM,channel='x\nEVIL',count=1)])
  self.assertNotIn('\n',text)

class FixesTests(SinkCase):
 """Regressions for the 2026-11 review of the tmux sink; each names its finding."""
 def register(self,session,pane):
  req=dict(self.req,session=session,pane=pane);validate_request(req);self.store.register(req)
 def states(self,session):
  return [r[0] for r in self.store.db.execute('SELECT state FROM deliveries WHERE session=? ORDER BY id',(session,))]
 def panes(self,footers):
  def run(*args):
   self.calls.append(args)
   if args[0]=='display-message':return f'{os.getpid()} claude'
   if args[0]=='capture-pane':return footers[args[args.index('-t')+1]]
   return ''
  return run
 def test_busy_session_backlog_does_not_starve_others(self):
  # Finding 1: 40 rows for one busy pane used to fill the 32-row batch.
  self.register('session-a','%1');self.register('session-b','%2')
  for _ in range(40):self.enqueue('session-a')
  self.enqueue('session-b')
  self.sink.run=self.panes({'%1':CLAUDE_WORKING,'%2':CLAUDE_IDLE});self.sink.drain()
  self.assertEqual(self.states('session-b'),['written']);self.assertEqual(set(self.states('session-a')),{'queued'})
 def test_sessions_rotate_when_more_pending_than_one_drain(self):
  # Finding 1: with more pending sessions than a drain handles, each is reached in turn.
  self.register('session-a','%1');self.register('session-b','%2')
  self.enqueue('session-a');self.enqueue('session-b')
  self.sink.run=self.panes({'%1':CLAUDE_WORKING,'%2':CLAUDE_IDLE})
  with patch('nth_interposer_tmux.SESSIONS_PER_DRAIN',1):
   self.sink.drain();self.sink.drain()
  self.assertEqual(self.states('session-b'),['written'])
 def test_fifo_within_session(self):
  # Finding 1: the oldest row of a session goes first.
  self.enqueue();self.enqueue()
  self.store.db.execute("UPDATE deliveries SET body='first' WHERE id=(SELECT MIN(id) FROM deliveries)");self.store.db.commit()
  self.sink.drain();self.assertEqual([c for c in self.calls if c[0]=='send-keys'][0][-1],'first')
 def test_answer_text_with_status_words_is_idle(self):
  # Finding 2: finished answer text naming thinking/compacting/a spinner is history.
  self.assertTrue(idle_footer(CLAUDE_ANSWER_WORDS,'claude'))
  self.assertTrue(idle_footer(CODEX_ANSWER_WORDS,'codex'))
  self.enqueue();self.footer=CLAUDE_ANSWER_WORDS;self.sink.drain();self.assertEqual(self.state(),'written')
 def test_live_footer_fixtures(self):
  # Finding 2: the live footer states of both clients.
  self.assertTrue(idle_footer(CLAUDE_IDLE,'claude'));self.assertTrue(idle_footer(CODEX_IDLE,'codex'))
  for text in [CLAUDE_WORKING,CLAUDE_TODO_WORKING,CLAUDE_COMPACTING,CLAUDE_APPROVAL,'\x1b[2m'+CLAUDE_WORKING]:
   self.assertFalse(idle_footer(text,'claude'),text)
  for text in [CODEX_WORKING,CODEX_COMPACTING,CODEX_APPROVAL]:
   self.assertFalse(idle_footer(text,'codex'),text)
 def test_captured_client_screens(self):
  # Screens reduced from live captures: the current Codex marker and faint placeholder,
  # truecolour escapes (a "2" inside 38;2;r;g;b is not the faint attribute), a finished
  # Claude turn line and a live Claude spinner with a tip line under it.
  self.assertTrue(idle_footer(REAL_CODEX_IDLE,'codex'))
  self.assertFalse(idle_footer(REAL_CODEX_DRAFT,'codex'))
  self.assertFalse(idle_footer(REAL_CODEX_SECOND_LINE,'codex'))
  self.assertTrue(idle_footer(REAL_CLAUDE_IDLE,'claude'))
  self.assertFalse(idle_footer(REAL_CLAUDE_DRAFT,'claude'))
  self.assertTrue(idle_footer(REAL_CLAUDE_HINT,'claude'))
  self.assertFalse(idle_footer(REAL_CLAUDE_WORKING,'claude'))
  # A placeholder captured without escapes cannot be told from a draft: defer.
  self.assertFalse(idle_footer(re.sub(r'\x1b\[[0-9;]*m','',REAL_CODEX_IDLE),'codex'))
 def test_capture_requests_escapes(self):
  self.enqueue();self.sink.drain()
  self.assertIn('-e',[c for c in self.calls if c[0]=='capture-pane'][0])
 def test_cli_binds_the_pane_it_inspected(self):
  # The CLI reads the pane id and pane process from tmux and registers exactly those.
  import contextlib, io, nth_cli, nth_interposer_wire
  sent=[]
  class Service:
   def call(self,op,**fields):
    request=dict(fields,v=1,id=1,op=op);validate_request(request);sent.append(request)
  @contextlib.contextmanager
  def connect():yield Service()
  out,err=io.StringIO(),io.StringIO()
  with patch('nth_interposer_tmux.command',return_value='%7 4242 bash\n') as tmux,patch.object(nth_interposer_wire,'connect',connect),\
    patch.object(nth_cli,'foreground_pid',return_value=4343) as foreground,\
    contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
   self.assertEqual(nth_cli.bind_tmux('session-test','codex','work:1.0'),0)
   self.assertEqual(nth_cli.bind_tmux('session-test','opencode','%7'),1)
   self.assertEqual(nth_cli.bind_tmux('','claude','%7'),1)
  self.assertEqual(tmux.call_args[0][3],'work:1.0')
  self.assertEqual(len(sent),1)
  # The client is the pane's foreground process; the pane's own process is recorded beside it.
  self.assertEqual({k:sent[0][k] for k in ('session','client','sink','pane','host_pid','pane_pid')},
   dict(session='session-test',client='codex',sink='tmux',pane='%7',host_pid=4343,pane_pid=4242))
  foreground.assert_called_with(4242)
  self.assertIn("running 'bash'",err.getvalue())
 def test_cli_status_names_one_session(self):
  # A store with hundreds of sessions overflows one reply frame; the CLI asks for one session.
  import contextlib, io, nth_cli, nth_interposer_wire
  asked=[]
  class Service:
   hello={'version':'test'}
   def call(self,op,**fields):
    asked.append((op,fields))
    if op=='list':raise nth_interposer_wire.refusal({'code':'frame_too_large','message':'response exceeds 64 KiB'})
    return {'sessions':[{'session':fields['session'],'sink':'tmux'}]}
  @contextlib.contextmanager
  def connect(**_):yield Service()
  out,err=io.StringIO(),io.StringIO()
  with patch.object(nth_interposer_wire,'connect',connect),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
   self.assertEqual(nth_cli.interposer_control('status','session-test'),0)
   self.assertEqual(nth_cli.interposer_control('status'),1)
  self.assertEqual(asked,[('status',{'session':'session-test'}),('list',{})])
  self.assertIn('"sink": "tmux"',out.getvalue())
  self.assertIn('trio interposer status --session ID',err.getvalue())
 def test_multiline_unfinished_prompt_defers(self):
  # Finding 4: an empty first input line with continuation lines is not empty input.
  self.assertFalse(idle_footer(CLAUDE_MULTILINE,'claude'));self.assertFalse(idle_footer(CODEX_MULTILINE,'codex'))
  self.enqueue();self.footer=CLAUDE_MULTILINE;self.sink.drain();self.assertEqual(self.state(),'queued')
  self.assertFalse(any(c[0]=='send-keys' for c in self.calls))
 def test_send_is_fenced_by_store_lock(self):
  # Finding 3: register/end take the store lock; it must be held across both sends.
  free=[]
  def run(*args):
   if args[0]=='send-keys':
    t=threading.Thread(target=lambda:free.append(self.store.lock.acquire(timeout=0) and (self.store.lock.release() or True)))
    t.start();t.join()
   return self.run_tmux(*args)
  self.enqueue();self.sink.run=run;self.sink.drain()
  self.assertEqual(self.state(),'written');self.assertEqual(free,[False,False])
 def test_identity_rechecked_inside_fence(self):
  # Finding 3: a pane whose process changes after inspection receives nothing.
  seen=[]
  def run(*args):
   self.calls.append(args)
   if args[0]=='display-message':
    seen.append(1);return f'{os.getpid()} claude' if len(seen)==1 else '99999 bash'
   return self.footer if args[0]=='capture-pane' else ''
  self.enqueue();self.sink.run=run;self.sink.drain()
  self.assertEqual(self.state(),'queued');self.assertFalse(any(c[0]=='send-keys' for c in self.calls))
 def test_rebind_during_inspection_follows_new_pane(self):
  # A rebind before the fence never types into the old pane; the notice moves to the new one.
  def run(*args):
   if args[0]=='capture-pane' and not self.rebound:self.rebound=True;self.register('session-test','%456')
   return self.run_tmux(*args)
  self.rebound=False
  self.enqueue();self.sink.run=run;self.sink.drain()
  self.assertEqual(self.state(),'queued');self.assertFalse(any(c[0]=='send-keys' for c in self.calls))
  self.sink.drain()
  self.assertEqual(self.state(),'written')
  self.assertEqual({c[2] for c in self.calls if c[0]=='send-keys'},{'%456'})
 def test_dead_client_in_live_pane_gets_nothing(self):
  # The pane's shell is alive but the bound client is gone: a new client there must not be typed into.
  child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
  try:
   request=dict(self.req,host_pid=child.pid,pane_pid=os.getpid());validate_request(request);self.store.register(request)
   binding=self.store.db.execute("SELECT value FROM meta WHERE key='tmux:session-test'").fetchone()[0]
   self.assertIn('"client"',binding)
   self.enqueue();self.sink.drain();self.assertEqual(self.state(),'written')
   self.enqueue()
  finally:
   child.kill();child.wait()
  self.sink.drain()
  self.assertEqual(self.states('session-test'),['written','queued'])
  self.assertEqual(len([c for c in self.calls if c[0]=='send-keys']),2)
 def test_pane_pid_needs_tmux_sink(self):
  with self.assertRaises(WireError):validate_request(dict(self.req,sink='rewake',pane_pid=5))
  with self.assertRaises(WireError):validate_request(dict(self.req,pane_pid=0))
 def test_orphan_row_does_not_abort_drain(self):
  # Finding 5: a row whose session was evicted is cancelled; other sessions still deliver.
  with self.store.db:
   self.store.db.execute("INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES (?,?,?,?,?,?)",
    ('session-gone','tmux','x','{"pane":"%9","pid":1,"stamp":1}','queued',time.time()))
  self.enqueue();self.sink.drain()
  self.assertEqual(self.states('session-gone'),['cancelled']);self.assertEqual(self.states('session-test'),['written'])
 def test_mixed_source_notice_keeps_each_membership(self):
  # Finding 6: per-membership prefix, member id and server.
  text=notice([dict(ITEM,source='local',member_id='alpha',server='nth-trio'),dict(ITEM,member_id='beta'),
               dict(ITEM,source='local',channel='done',member_id='gamma',count=0,reason='channel ended',server='nth-trio')])
  self.assertIn('trio_poll on MCP server nth-trio',text);self.assertIn('quartet_poll on MCP server quartet-a',text)
  for member in ('alpha','beta','gamma'):self.assertIn(f'member {member}',text)
  self.assertIn('trio channel done',text);self.assertNotIn('\n',text)
  self.assertEqual(len(batch_notice_lines([ITEM,dict(ITEM,reason='channel ended')])),2)
 def test_runtime_tick_schedules_sink(self):
  # Mutation guard: Runtime.tick() must start the tmux drain.
  self.enqueue();runtime=Runtime(self.store,logging.getLogger('test'));runtime.tmux_sink.run=self.run_tmux
  try:
   runtime.tick();runtime.tmux_sink.thread.join(timeout=10)
   self.assertEqual(self.state(),'written')
  finally:runtime.close()
 def test_sends_the_stored_body(self):
  # Mutation guard: the literal send carries the queued row body.
  self.enqueue();body=self.store.db.execute('SELECT body FROM deliveries').fetchone()[0]
  self.sink.drain();self.assertTrue(body)
  self.assertEqual([c for c in self.calls if c[0]=='send-keys'][0][-1],body)

if __name__=='__main__':unittest.main()
