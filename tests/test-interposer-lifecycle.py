"""Outbox lifecycle across ownership changes, session end/eviction, host
replacement, retention and restart. Real store and runtime, synthetic ids."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))
import nth_interposer_store as storage
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime
from nth_interposer_wire import validate_request, WireError
from nth_interposer_tmux import TmuxSink
from nth_interposer import dispatch as service

KEY = '0123456789abcdef01234567'
OTHER_KEY = '89abcdef0123456789abcdef'


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.home.chmod(0o700)
        self.env = patch.dict(os.environ, {'NTH_HOME': str(self.home), 'NTH_INTERPOSER_TEST_RUNTIME': str(self.home)})
        self.env.start()
        self.store = Store()
        self.children = []

    def tearDown(self):
        for child in self.children:
            child.kill()
            child.wait()
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def register(self, session, sink='opencode', pid=None, resume=False):
        request = dict(v=1, id=1, op='session.register', session=session, client='opencode' if sink == 'opencode' else 'claude',
                       host_pid=pid or os.getpid(), sink=sink, host_ok=True, problem='')
        if sink == 'tmux':
            request['pane'] = '%7'
        if resume:
            request['resume'] = True
        validate_request(request)
        return self.store.register(request)

    def identity(self, key, channel='room'):
        folder = self.store.path.parent/'identities'
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder/(key+'.json')).write_text(json.dumps(dict(source='local', url='local', channel=channel, member_id='member')))

    def attach(self, key, session):
        return self.store.attach(dict(key=key, session=session, server='nth-trio'))

    def wait(self, session, pid=None):
        request = dict(v=1, id=3, op='wait', session=session, host_pid=pid or os.getpid())
        return service(self.store, request)['delivery']

    def delivered(self, session, delivery, outcome='written'):
        request = dict(v=1, id=4, op='delivered', session=session, host_pid=os.getpid(),
                       delivery_id=delivery, outcome=outcome)
        return service(self.store, request)

    def queue_from_runtime(self, key, session, ids=(1,)):
        runtime = Runtime(self.store, logging.getLogger('test'))
        with self.store.lock, runtime.buffer_transaction():
            runtime.accumulate(runtime.member(key), [{'id': i, 'mentioned': False} for i in ids])
        self.assertTrue(runtime.release(session, force=True))
        runtime.close()

    def states(self, session=None):
        query = 'SELECT state FROM deliveries' + (' WHERE session=?' if session else '') + ' ORDER BY id'
        return [r[0] for r in self.store.db.execute(query, (session,) if session else ())]

    # Finding 1: an unclaimed notice follows its membership to the new owner.
    def test_queued_notice_follows_membership_to_new_session(self):
        self.register('session-old')
        self.register('session-new')
        self.identity(KEY)
        self.attach(KEY, 'session-old')
        self.queue_from_runtime(KEY, 'session-old', ids=(1, 2))
        self.assertEqual(self.states('session-old'), ['queued'])
        self.attach(KEY, 'session-new')
        # The old session lost ownership: it must not receive the stale notice.
        self.assertIsNone(self.wait('session-old'))
        self.store.end('session-old')
        delivery = self.wait('session-new')
        self.assertIsNotNone(delivery)
        self.assertRegex(delivery['notice'], r'2 new trio messages .* in channel room\.')
        self.assertEqual(self.states('session-old'), ['cancelled'])

    # Finding 1: a claimed notice stays with its claim and is never replayed.
    def test_handoff_never_replays_claimed_notice(self):
        self.register('session-old')
        self.register('session-new')
        self.identity(KEY)
        self.attach(KEY, 'session-old')
        self.queue_from_runtime(KEY, 'session-old', ids=(1,))
        claimed = self.wait('session-old')
        self.queue_from_runtime(KEY, 'session-old', ids=(2,))
        self.attach(KEY, 'session-new')
        self.store.end('session-old')
        self.assertEqual(self.states('session-old'), ['sending', 'cancelled'])
        moved = self.wait('session-new')
        self.assertNotEqual(moved['id'], claimed['id'])
        self.assertRegex(moved['notice'], r'1 new trio message .* in channel room\.')
        self.assertIsNone(self.wait('session-new'))

    # Finding 1: only the departing membership moves out of a shared notice.
    def test_shared_notice_splits_by_owner(self):
        self.register('session-old')
        self.register('session-new')
        self.identity(KEY, 'room')
        self.identity(OTHER_KEY, 'hall')
        self.attach(KEY, 'session-old')
        self.attach(OTHER_KEY, 'session-old')
        runtime = Runtime(self.store, logging.getLogger('test'))
        with self.store.lock, runtime.buffer_transaction():
            runtime.accumulate(runtime.member(KEY), [{'id': 1}])
            runtime.accumulate(runtime.member(OTHER_KEY), [{'id': 1}, {'id': 2}])
        runtime.release('session-old', force=True)
        runtime.close()
        self.attach(KEY, 'session-new')
        old = self.wait('session-old')['notice']
        self.assertRegex(old, r'2 new trio messages .* in channel hall\.')
        self.assertNotIn('room', old)
        new = self.wait('session-new')['notice']
        self.assertRegex(new, r'1 new trio message .* in channel room\.')
        self.assertNotIn('hall', new)

    # Finding 2: end cancels queued work and removes sink bindings.
    def test_end_cancels_queue_and_forgets_sink_bindings(self):
        self.register('session-tmux', sink='tmux')
        self.register('session-sdk')
        self.wait('session-sdk')
        sink = TmuxSink(self.store, run=lambda *a: '')
        from nth_interposer_opencode import enqueue
        with self.store.lock, self.store.db:
            sink.enqueue('session-tmux', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=1, count=1)])
            enqueue(self.store, 'session-sdk', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=1, count=1)])
        self.store.end('session-tmux')
        self.store.end('session-sdk')
        self.assertEqual(self.states(), ['cancelled', 'cancelled'])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM meta WHERE key IN ('tmux:session-tmux','opencode_poll:session-sdk')").fetchone())

    # Finding 2: eviction at the session cap cancels the victim's queue, so the
    # tmux drain keeps working instead of failing on an unknown session.
    def test_eviction_cancels_queue_and_drain_survives(self):
        with patch.object(storage, 'MAX_SESSIONS', 3):
            self.register('session-a', sink='tmux')
            sink = TmuxSink(self.store, run=lambda *a: '')
            with self.store.lock, self.store.db:
                sink.enqueue('session-a', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=1, count=1)])
                # Simulate a row claimed before the end: kept for inspection.
                sink.enqueue('session-a', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=1, count=1)])
                self.store.db.execute("UPDATE deliveries SET state='uncertain' WHERE id=(SELECT MAX(id) FROM deliveries)")
                self.store.db.execute("UPDATE sessions SET state='ended' WHERE session='session-a'")
            self.register('session-b')
            self.register('session-c')
            self.register('session-d')
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM sessions WHERE session='session-a'").fetchone())
        self.assertEqual(self.states('session-a'), ['cancelled', 'uncertain'])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM meta WHERE key='tmux:session-a'").fetchone())
        sink.drain()

    # Finding 2: databases written by earlier builds already hold orphan rows.
    def test_open_cancels_orphan_queue_rows(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES ('session-gone','tmux','x','{}','queued',?)", (time.time(),))
        self.store.close()
        self.store = Store()
        self.assertEqual(self.states(), ['cancelled'])

    # Finding 3: a replacement host does not inherit the dead host's turn.
    def test_new_host_lifetime_starts_idle(self):
        self.register('session-sdk')
        from nth_interposer_opencode import enqueue
        with self.store.lock, self.store.db:
            enqueue(self.store, 'session-sdk', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=1, count=1)])
        self.store.turn(dict(session='session-sdk', phase='started'))
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.children.append(child)
        self.register('session-sdk', pid=child.pid, resume=True)
        self.assertEqual(self.store.session('session-sdk')['state'], 'idle')
        self.assertIsNotNone(self.wait('session-sdk', pid=child.pid))

    def test_same_host_reregistration_keeps_turn(self):
        self.register('session-sdk')
        self.store.turn(dict(session='session-sdk', phase='started'))
        self.register('session-sdk', resume=True)
        self.assertEqual(self.store.session('session-sdk')['state'], 'in_turn')

    # Finding 4: queue index, in-place upgrade, retention and no ledger scan in reconcile.
    def test_schema_three_database_gains_index_without_losing_rows(self):
        with self.store.db:
            self.store.db.execute('DROP INDEX deliveries_queue')
            self.store.db.execute("INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES ('s','opencode','x','{}','uncertain',?)", (time.time(),))
        self.store.close()
        self.store = Store()
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='deliveries_queue'").fetchone())
        self.assertEqual(self.states(), ['uncertain'])
        plan = ' '.join(r[-1] for r in self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT id,body FROM deliveries WHERE session=? AND sink='opencode' AND state='queued' ORDER BY id LIMIT 1", ('s',)))
        self.assertIn('deliveries_queue', plan)

    def test_terminal_rows_are_pruned_and_claims_kept(self):
        old = time.time() - storage.DELIVERY_RETENTION - 60
        with self.store.db:
            for state in ('written', 'cancelled', 'sending', 'uncertain', 'queued'):
                self.store.db.execute('INSERT INTO deliveries(session,sink,body,ranges,state,created,done) VALUES (?,?,?,?,?,?,?)',
                                      ('s', 'opencode', 'x', '{}', state, old, old))
        with patch.object(storage, 'MAX_TERMINAL_DELIVERIES', 2):
            self.store.prune_deliveries()
            self.assertEqual(sorted(self.states()), ['queued', 'sending', 'uncertain'])
            with self.store.db:
                for _ in range(5):
                    self.store.db.execute("INSERT INTO deliveries(session,sink,body,ranges,state,created,done) VALUES ('s','tmux','x','{}','written',?,?)",
                                          (time.time(), time.time()))
            self.store.prune_deliveries()
        self.assertEqual(self.states().count('written'), 2)

    def test_reconcile_does_not_aggregate_delivery_ledger(self):
        self.register('session-sdk')
        runtime = Runtime(self.store, logging.getLogger('test'))
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            runtime.reconcile()
            runtime.last_drain = runtime.last_death = 0
            with patch.object(runtime.tmux_sink, 'schedule'), patch.object(runtime, 'drain'):
                runtime.tick()
        finally:
            self.store.db.set_trace_callback(None)
            runtime.close()
        self.assertFalse([s for s in statements if 'GROUP BY' in s and 'deliveries' in s])
        self.assertIn('delivery_states', self.store.snapshot())

    # Finding 5: damaged or future heartbeats.
    def test_truncated_heartbeat_does_not_break_status(self):
        self.register('session-sdk')
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('opencode_poll:session-sdk','17000')")
            self.store.db.execute("UPDATE meta SET value='1.5e' WHERE key='opencode_poll:session-sdk'")
        self.assertEqual(self.store.snapshot()['sessions'][0]['sink_heartbeat'], 0)

    def test_future_heartbeat_is_not_ready(self):
        import nth_event_access as access
        self.register('session-sdk')
        with self.store.db:
            self.store.db.execute("INSERT INTO memberships(key,source,url,channel,member_id,owner_session,poll_state,last_ok) VALUES (?,?,?,?,?,?,?,?)",
                                  (KEY, 'local', 'local', 'room', 'member', 'session-sdk', 'listening', time.time()))
        self.wait('session-sdk')
        client = MagicMock()
        client.__enter__.return_value = client
        client.call.side_effect = lambda *a, **k: self.store.snapshot(key=KEY)
        with patch.dict(os.environ, {'TRIO_NATIVE_CLIENT': 'opencode'}), patch('nth_claude_hook.identity_key_for', return_value=KEY), \
                patch('nth_interposer_wire.connect', return_value=client):
            self.assertTrue(access.delivery_status('room', 'member', 'synthetic')['ready'])
            with self.store.db:
                self.store.db.execute("UPDATE meta SET value=? WHERE key='opencode_poll:session-sdk'", (json.dumps(time.time()+3600),))
            self.assertFalse(access.delivery_status('room', 'member', 'synthetic')['ready'])

    # Restart: queued notices survive and can still move; claims are not replayed.
    def test_restart_keeps_queue_and_handoff_record(self):
        self.register('session-old')
        self.register('session-new')
        self.identity(KEY)
        self.attach(KEY, 'session-old')
        self.queue_from_runtime(KEY, 'session-old', ids=(1,))
        claimed = self.wait('session-old')
        self.queue_from_runtime(KEY, 'session-old', ids=(2, 3))
        self.store.close()
        self.store = Store()
        Runtime(self.store, logging.getLogger('test')).close()
        self.attach(KEY, 'session-new')
        self.store.end('session-old')
        moved = self.wait('session-new')
        self.assertRegex(moved['notice'], r'2 new trio messages .* in channel room\.')
        self.assertIsNone(self.wait('session-new'))
        self.assertEqual(self.store.db.execute('SELECT state FROM deliveries WHERE id=?', (claimed['id'],)).fetchone()[0], 'sending')

    # Service route (Ent): wait/delivered pass request validation and dispatch.
    def test_service_route_wait_and_delivered(self):
        self.register('session-sdk')
        from nth_interposer_opencode import enqueue
        with self.store.lock, self.store.db:
            enqueue(self.store, 'session-sdk', [dict(source='local', channel='room', member_id='m', first_id=1, last_id=4, count=4)])
        delivery = self.wait('session-sdk')
        self.assertRegex(delivery['notice'], r'4 new trio messages .* in channel room\.')
        self.assertEqual(self.delivered('session-sdk', delivery['id']), {'recorded': True})
        self.assertEqual(self.states(), ['written'])
        for bad in (dict(op='delivered', session='session-sdk', host_pid=os.getpid(), delivery_id=delivery['id'], outcome='maybe'),
                    dict(op='wait', session='session-sdk'),
                    dict(op='delivered', session='session-sdk', host_pid=os.getpid(), delivery_id=delivery['id']+1, outcome='written')):
            with self.assertRaises(WireError):
                service(self.store, dict(v=1, id=5, **bad))
        self.assertEqual(self.states(), ['written'])


if __name__ == '__main__':
    unittest.main()
