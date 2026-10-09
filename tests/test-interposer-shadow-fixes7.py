"""Round-seven regressions: bounded evidence and running-listener journal recovery."""
import importlib.util
import io
import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shadow_cases7',ROOT/'tests/test-interposer-shadow.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)
shadow, wire, cli = cases.shadow, cases.wire, cases.cli
KEY, SESSION, SESSION2 = cases.KEY, cases.SESSION, cases.SESSION2
import nth_interposer_runtime as runtime_module


class JournalOutage:
    """Fail only the actual journal INSERT, preserving SQLite rollback semantics."""
    def __init__(self, db, failures=0, blocked=None):
        self.db, self.failures, self.blocked = db, failures, blocked
        self.failed = threading.Event()
        self.attempts = 0
        self.observe_status = None
        self.statuses = []

    def execute(self, sql, parameters=()):
        if 'INSERT INTO meta(key,value)' in sql:
            self.attempts += 1
            if self.observe_status:
                self.statuses.append(self.observe_status())
            if self.failures or (self.blocked and not self.blocked.is_set()):
                self.failures = max(0,self.failures-1)
                self.failed.set()
                raise sqlite3.OperationalError('synthetic journal outage')
        return self.db.execute(sql,parameters)

    def __getattr__(self, name):
        return getattr(self.db,name)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, *args):
        return self.db.__exit__(*args)


class RoundSevenTests(unittest.TestCase):
    setUp = cases.ShadowTests.setUp
    tearDown = cases.ShadowTests.tearDown
    identity = cases.ShadowTests.identity
    op = cases.ShadowTests.op
    attach = cases.ShadowTests.attach
    wait_start = cases.ShadowTests.wait_start
    eventually = cases.ShadowTests.eventually

    def member(self):
        with self.store.lock:
            return self.runtime.member(KEY)

    def idle_listener(self):
        self.attach()
        listener = self.runtime.pollers[KEY]
        listener.stop()
        listener.thread.join(2)
        # Direct observation fixtures retain an explicitly stopped listener.
        return listener

    def anchor(self, side, at):
        with patch.object(shadow.time,'time',return_value=at):
            self.assertTrue(shadow.append(side,SESSION2,'claude','rewake',[],[],1))

    def test_gapped_flood_over_five_mb_rotation_restart_and_diff(self):
        self.attach()
        self.op('membership.configure',key=KEY,filter='at')
        self.wait_start(KEY)
        self.op('turn',session=SESSION,phase='started')
        listener = self.runtime.pollers[KEY]
        listener.stop()
        listener.thread.join(2)
        messages = [cases.message(i,mentioned=bool(i%2)) for i in range(1,100001)]
        listener._fresh(messages)
        self.assertEqual(self.member()['shadow_announced_through'],100000)
        self.assertEqual(self.runtime.buffers[SESSION]['members'][KEY]['count'],50000)
        record = self.runtime.buffer_record(SESSION)
        self.assertGreater(len(shadow.encode_record(record)),5*1024*1024)
        self.anchor('would',0)
        with patch.object(shadow.time,'time',return_value=50):
            self.assertTrue(self.runtime.release(SESSION,force=True,flush=True))
        # Rotate the entire split release into .1; each bounded line must survive.
        self.anchor('would',100)
        spans = shadow.ranges_for(KEY,'nth-qweb',[m for m in messages if m['mentioned']])
        with patch.object(shadow,'ROTATE_BYTES',50*1024*1024):
            self.anchor('actual',0)
            with patch.object(shadow.time,'time',return_value=70):
                for start in range(0,len(spans),1000):
                    self.assertTrue(shadow.append('actual',SESSION,'claude','rewake',spans[start:start+1000],[],1))
            self.anchor('actual',100)
        self.assertEqual((self.member()['shadow_notices'],self.member()['shadow_ids']),(1,50000))
        self.assertFalse(self.runtime.buffers)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM meta WHERE key LIKE 'shadow_pending:%'").fetchone())
        log = self.runtime.log
        self.runtime.close()
        self.store.close()
        self.store = cases.Store()
        self.runtime = cases.Runtime(self.store,log,self.hub.factory)
        rows = shadow.records('would')
        self.assertEqual(rows.errors,[])
        chunks = [r for r in rows if r['session']==SESSION]
        self.assertGreater(len(chunks),1)
        self.assertEqual(len({r['notice_id'] for r in chunks}),1)
        self.assertEqual([r['part'] for r in chunks],list(range(len(chunks))))
        self.assertTrue(all(r['parts']==len(chunks) for r in chunks))
        self.assertTrue(all(len(shadow.encode_record(r))<=shadow.MAX_RECORD_BYTES for r in chunks))
        self.assertEqual([r['first'] for row in chunks for r in row['ranges']],list(range(1,100000,2)))
        self.assertEqual(sum(r['count'] for row in chunks for r in row['ranges']),50000)
        result = shadow.compare()
        self.assertTrue(result['comparable'])
        self.assertEqual(result['missing_in_would'],[])
        self.assertEqual(result['missing_in_actual'],[])
        with patch.object(shadow,'COMPARE_MARGIN',0):
            result = shadow.compare()
            self.assertEqual(result['sessions'][SESSION],dict(actual_notices=50,would_notices=1))
        with patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['interposer','shadow-diff','--json']),0)
            self.assertEqual(json.loads(out.getvalue())['errors'],[])

    def test_reader_reports_overlong_bad_and_truncated_lines_without_input(self):
        self.anchor('actual',0)
        path = wire.home()/'events/shadow/actual.jsonl'
        with path.open('ab') as out:
            out.write(b'x'*(shadow.MAX_RECORD_BYTES+100)+b'\n')
            out.write(b'{"secret":"synthetic-private-data"}\n')
            out.write(b'{"truncated":')
        rows = shadow.records('actual')
        self.assertEqual(len(rows),1)
        self.assertEqual([e['line'] for e in rows.errors],[2,3,4])
        result = shadow.compare()
        self.assertFalse(result['comparable'])
        self.assertEqual(len(result['errors']),3)
        for flag in ([],['--json']):
            with patch('sys.stdout',io.StringIO()) as out:
                self.assertEqual(cli.main(['interposer','shadow-diff',*flag]),1)
                self.assertNotIn('synthetic-private-data',out.getvalue())
                self.assertIn('actual.jsonl',out.getvalue())

    def test_chunks_terminal_counts_and_missing_duplicate_metadata(self):
        spans = shadow.ranges_for(KEY,'nth-qweb',[cases.message(i) for i in range(1,80,2)])
        with patch.object(shadow,'MAX_RECORD_BYTES',750):
            self.assertTrue(shadow.append('would',SESSION,'claude','rewake',spans,[dict(key=KEY,reason='channel ended')],2))
            rows = shadow.records('would')
        self.assertEqual(rows.errors,[])
        self.assertEqual(sum(r['count'] for row in rows for r in row['ranges']),40)
        self.assertEqual([r for row in rows for r in row['ended']],[dict(key=KEY,reason='channel ended')])
        path = wire.home()/'events/shadow/would.jsonl'
        for variant,reason in ((rows[:-1],'incomplete logical notice'),(rows+[rows[0]],'duplicate'),
                              (rows[:-1]+[dict(rows[-1],lines=99)],'inconsistent')):
            path.write_bytes(b''.join(shadow.encode_record(r) for r in variant))
            self.assertTrue(any(reason in e['reason'] for e in shadow.records('would').errors))
        self.assertFalse(shadow.valid_record(dict(rows[0],parts=True),'would'))

    def test_running_listener_retries_two_journal_inserts_and_observes(self):
        with patch.multiple(runtime_module,START_RETRY_SECONDS=.01,START_RETRY_MAX=.02):
            self.attach()
            original = self.store.db
            outage = JournalOutage(original,failures=2)
            self.store.db = outage
            self.hub.replies.append(dict(event='new_messages',messages=[cases.message(1701,mentioned=True)]))
            try:
                self.eventually(lambda:self.member()['shadow_announced_through']==1701)
                listener = self.runtime.pollers[KEY]
                self.assertEqual(outage.failures,0)
                self.assertGreaterEqual(outage.attempts,3)
                self.assertTrue(listener.thread.is_alive())
                self.assertEqual(self.member()['shadow_ended'],'')
                self.assertEqual(self.runtime.buffers[SESSION]['members'][KEY]['count'],1)
                self.assertTrue(self.runtime.release(SESSION,force=True))
                self.assertEqual([r['first'] for row in shadow.records('would') for r in row['ranges']],[1701])
            finally:
                self.store.db = original

    def test_terminal_status_waits_for_commit_after_outage(self):
        with patch.multiple(runtime_module,START_RETRY_SECONDS=.01,START_RETRY_MAX=.02):
            self.attach()
            listener = self.runtime.pollers[KEY]
            original, recovered = self.store.db, threading.Event()
            outage = JournalOutage(original,blocked=recovered)
            outage.observe_status = lambda:listener.state
            self.store.db = outage
            self.hub.replies.append(dict(event='ended',messages=[]))
            try:
                self.assertTrue(outage.failed.wait(2))
                with self.store.lock:
                    self.assertNotEqual(listener.state,'ended')
                    self.assertEqual(self.member()['shadow_ended'],'')
                    self.assertFalse(self.runtime.buffers)
                self.assertTrue(listener.thread.is_alive())
                recovered.set()
                listener.thread.join(2)
                self.assertFalse(listener.thread.is_alive())
                self.assertTrue(outage.statuses)
                self.assertNotIn('ended',outage.statuses)
                self.assertEqual(listener.state,'ended')
                self.assertEqual(self.member()['shadow_ended'],'channel ended')
                self.assertTrue(self.runtime.release(SESSION,force=True))
                self.assertEqual([r['reason'] for row in shadow.records('would') for r in row['ended']],['channel ended'])
            finally:
                recovered.set()
                self.store.db = original

    def test_retry_backoff_is_bounded_and_stop_interrupts(self):
        listener = self.idle_listener()
        delays = []
        def fail():
            raise sqlite3.OperationalError('synthetic')
        def wait(delay):
            delays.append(delay)
            return len(delays)==10
        with patch.object(listener._stop,'wait',side_effect=wait):
            listener.retry_storage(fail)
        self.assertEqual(delays[:3],[.5,1,2])
        self.assertEqual(delays[-1],30)
        self.assertLessEqual(max(delays),30)

    def test_reconcile_and_reenable_restart_dead_uncommitted_poller(self):
        self.attach()
        dead = self.runtime.pollers[KEY]
        # Force the real thread's exception path to fail before terminal persistence.
        original_poll, original_end = dead.poll, dead._end
        def failed_end(*args):
            raise SystemExit('synthetic uncommitted terminal failure')
        dead._end = failed_end
        dead.poll = lambda arguments: dict(event='ended',messages=[])
        dead.thread.join(2)
        self.assertFalse(dead.thread.is_alive())
        self.assertEqual(self.member()['shadow_ended'],'')
        try:
            self.runtime.reconcile()
            self.assertNotIn(KEY,self.runtime.pollers)
            due = self.runtime.start_retry[KEY][1]
            self.assertGreater(due,time.monotonic())
            self.op('membership.configure',key=KEY,enabled=True)
            self.assertNotIn(KEY,self.runtime.pollers)
            self.hub.replies.append(dict(event='new_messages',messages=[cases.message(1701,mentioned=True)]))
            self.eventually(lambda:self.member()['shadow_announced_through']==1701)
            self.assertIsNot(self.runtime.pollers[KEY],dead)
            self.assertTrue(self.runtime.pollers[KEY].thread.is_alive())
            self.assertEqual(self.member()['shadow_ended'],'')
        finally:
            dead.poll, dead._end = original_poll, original_end


if __name__=='__main__':
    unittest.main(verbosity=2)
