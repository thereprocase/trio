"""Optional hub poll cursors/presence and schema-negotiated listener cursors.

All database and runtime state is temporary. No network services are started.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='nth-poll-args-')
os.environ['NTH_HOME'] = TEMP.name
os.environ['XDG_STATE_HOME'] = str(Path(TEMP.name) / 'state')
os.environ['NTH_QUIET'] = '1'
for key in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID', 'CODEX_THREAD_ID'):
    os.environ.pop(key, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_server as srv
import nth_listener as listener
import nth_web as web

NOW = datetime(2030, 1, 2, 12, 30, tzinfo=timezone.utc)


class PollArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.saved = srv.DB_DIR, srv.DB_PATH
        srv.DB_DIR = Path(self.temp.name)
        srv.DB_PATH = srv.DB_DIR / 'nth.db'
        self.reader = json.loads(srv.nth_connect('test', name='Reader', channel='room'))
        self.writer = json.loads(srv.nth_connect('test', name='Writer', channel='room'))
        db = srv.get_db()
        through = db.execute('SELECT MAX(id) FROM messages').fetchone()[0]
        db.close()
        self.ack(through)
        srv.nth_ack(channel='room', member_id=self.reader['member_id'], through_id=through)
        self.hub = web.EventHub(srv.DB_PATH, 'room')
        self.clock = patch.object(srv, 'now_iso', return_value=NOW.isoformat())
        self.server_clock = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        srv.DB_DIR, srv.DB_PATH = self.saved
        self.temp.cleanup()

    def send(self, text='hello'):
        reply = json.loads(srv.nth_send(channel='room', member_id=self.writer['member_id'], message=text))
        return reply['message_id']

    def raw_poll(self, token=True, **kwargs):
        arguments = dict(channel='room', member_id=self.reader['member_id'], wait_seconds=0)
        if token:
            arguments['session_token'] = self.reader['session_token']
        arguments.update(kwargs)
        return srv.nth_poll(**arguments)

    def poll(self, **kwargs):
        return json.loads(self.raw_poll(**kwargs))

    def rows(self):
        with srv.get_db() as db:
            member = dict(db.execute('SELECT * FROM members WHERE id = ? AND channel = ?',
                                    (self.reader['member_id'], 'room')).fetchone())
            session = dict(db.execute('SELECT * FROM sessions WHERE session_token = ?',
                                     (self.reader['session_token'],)).fetchone())
        db.close()
        return member, session

    def roster(self):
        with srv.get_db() as db:
            result = self.hub._fetch_roster(db)
        db.close()
        return next(row for row in result if row['id'] == self.reader['member_id'])

    def ack(self, mid):
        srv.nth_ack(channel='room', member_id=self.reader['member_id'], through_id=mid,
                    session_token=self.reader['session_token'])

    def test_after_id_skips_backlog_without_acknowledging_it(self):
        mids = [self.send(str(i)) for i in range(4)]
        before = self.rows()
        result = self.poll(after_id=mids[1])
        self.assertEqual([m['id'] for m in result['messages']], mids[2:])
        after = self.rows()
        self.assertEqual([r['last_read'] for r in before], [r['last_read'] for r in after])
        self.assertEqual([m['id'] for m in self.poll()['messages']], mids)

    def test_after_id_zero_and_none_are_byte_identical_to_omission(self):
        self.send()
        self.assertEqual(self.raw_poll(), self.raw_poll(after_id=0))
        self.assertEqual(self.raw_poll(), self.raw_poll(after_id=None, delivery_state=None))

    def test_cursor_uses_max_of_session_ack_and_after_id(self):
        mids = [self.send(str(i)) for i in range(5)]
        self.ack(mids[2])
        self.assertEqual([m['id'] for m in self.poll(after_id=mids[0])['messages']], mids[3:])
        self.assertEqual([m['id'] for m in self.poll(after_id=mids[3])['messages']], mids[4:])
        self.assertEqual(self.poll(after_id=mids[-1])['event'], 'no_new')

    def test_cursor_uses_member_ack_without_a_token(self):
        mids = [self.send(str(i)) for i in range(4)]
        srv.nth_ack(channel='room', member_id=self.reader['member_id'], through_id=mids[1])
        result = self.poll(token=False, auto_ack=False, after_id=mids[0])
        self.assertEqual([m['id'] for m in result['messages']], mids[2:])
        self.assertEqual(self.rows()[0]['last_read'], mids[1])

    def test_backlog_does_not_short_circuit_a_long_poll(self):
        last = self.send()
        ticks = [0.0]
        def deadline(*args):
            ticks[0] = 1.0
        with patch.object(srv.time, 'monotonic', side_effect=lambda: ticks[0]), \
                patch.object(srv, '_wait_for_change', side_effect=deadline) as wait:
            self.assertEqual(self.poll(after_id=last, wait_seconds=1)['event'], 'no_new')
        wait.assert_called_once()
        self.assertEqual(ticks[0], 1.0)

    def test_new_message_arrives_while_waiting_beyond_backlog(self):
        last = self.send('backlog')
        def arrival(*args):
            self.send('new arrival')
        with patch.object(srv, '_wait_for_change', side_effect=arrival) as wait:
            result = self.poll(after_id=last, wait_seconds=1)
        wait.assert_called_once()
        self.assertEqual([m['content'] for m in result['messages']], ['new arrival'])

    def test_ended_channel_unread_also_respects_cursor(self):
        mids = [self.send(str(i)) for i in range(3)]
        self.ack(mids[0])
        srv.nth_ack(channel='room', member_id=self.reader['member_id'], through_id=mids[0])
        ended = json.loads(srv.nth_end(channel='room', member_id=self.writer['member_id']))
        self.assertEqual(ended['total_messages'], 5)  # two joins + three posts; end posts nothing
        for token in (False, True):
            for cursor, remaining in ((0, mids[1:]), (mids[1], mids[2:]), (mids[-1] + 10, [])):
                with self.subTest(token=token, cursor=cursor):
                    result = self.poll(token=token, after_id=cursor)
                    self.assertEqual(result['event'], 'ended')
                    self.assertEqual(result['unread_count'], 2)  # all visible unacked posts
                    self.assertEqual(result['unread'], [
                        {'id': mid, 'from': 'Writer', 'content': str(mids.index(mid)), 'at': NOW.isoformat()}
                        for mid in remaining])

    def test_invalid_cursors_are_refused(self):
        for value in (-1, 1.5, True, '4', 2**53, 2**80):
            with self.subTest(value=value):
                self.assertEqual(self.poll(after_id=value), {'error': 'after_id must be an integer with 0 <= after_id < 2**53.'})

    def test_delivery_state_is_recorded_with_a_timestamp(self):
        for state in ('waiting', 'in_turn', 'unreachable'):
            with self.subTest(state=state):
                self.poll(delivery_state=state)
                member, _ = self.rows()
                self.assertEqual((member['delivery_state'], member['delivery_state_at']),
                                 (state, NOW.isoformat()))
        self.poll()
        self.assertEqual(self.rows()[0]['delivery_state'], 'unreachable')

    def test_bad_delivery_state_is_refused_without_presence_writes(self):
        for state in ('idle', 'WAITING', '', 3, ['waiting']):
            with self.subTest(state=state):
                self.assertEqual(self.poll(delivery_state=state),
                                 {'error': 'delivery_state must be waiting, in_turn or unreachable.'})
                self.assertIsNone(self.rows()[0]['delivery_state'])
                self.assertIsNone(self.rows()[0]['delivery_state_at'])

    def test_roster_renders_each_fresh_delivery_state(self):
        for state, status, label in (('waiting', 'idle', 'listening (hooks)'),
                                      ('in_turn', 'working', 'working'),
                                      ('unreachable', 'stale', 'unreachable')):
            with self.subTest(state=state):
                self.poll(delivery_state=state)
                with patch.object(web, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = NOW
                    row = self.roster()
                self.assertEqual((row['status'], row['delivery_label']), (status, label))
                self.assertEqual(row['delivery_state'], state)
                self.assertEqual(row['delivery_state_at'], NOW.isoformat())

    def test_presence_stales_after_two_minutes_without_newer_heartbeats(self):
        for state in ('waiting', 'in_turn', 'unreachable'):
            self.poll(delivery_state=state)
            with patch.object(web, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = NOW + timedelta(seconds=120)
                clock.fromtimestamp.side_effect = datetime.fromtimestamp
                self.assertNotIn('silent since', self.roster()['delivery_label'])
                clock.now.return_value = NOW + timedelta(seconds=121)
                row = self.roster()
            self.assertEqual((row['status'], row['delivery_label']), ('stale', 'silent since'))
            self.assertEqual(row['delivery_state_at'], NOW.isoformat())

    def test_presence_refresh_does_not_churn_but_staleness_pushes(self):
        self.poll(delivery_state='waiting')
        with patch.object(web, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = NOW
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            first = self.roster()
            with patch.object(srv, 'now_iso', return_value=(NOW + timedelta(seconds=10)).isoformat()):
                self.poll(delivery_state='waiting')
            second = self.roster()
            self.assertEqual(web._roster_change_key([first]), web._roster_change_key([second]))
            clock.now.return_value = NOW + timedelta(seconds=131)
            stale = self.roster()
            self.assertNotEqual(web._roster_change_key([second]), web._roster_change_key([stale]))

    def test_legacy_roster_has_no_delivery_fields(self):
        row = self.roster()
        self.assertNotIn('delivery_label', row)
        self.assertNotIn('delivery_state', row)
        self.assertEqual(row['status'], 'active')

    def test_old_database_roster_and_idempotent_migration(self):
        db = srv.get_db()
        db.execute('ALTER TABLE members DROP COLUMN delivery_state')
        db.execute('ALTER TABLE members DROP COLUMN delivery_state_at')
        db.commit()
        row = next(r for r in self.hub._fetch_roster(db) if r['id'] == self.reader['member_id'])
        self.assertNotIn('delivery_label', row)
        db.close()
        srv._schema_ready_key = None
        self.assertIsNone(self.rows()[0]['delivery_state'])
        self.assertIsNone(self.rows()[0]['delivery_state_at'])
        self.poll(delivery_state='waiting')
        self.assertEqual(self.rows()[0]['delivery_state'], 'waiting')

    def test_old_client_default_response_and_auto_ack_are_unchanged(self):
        mid = self.send()
        with patch.object(srv, '_sentinel_nag', return_value=''), patch.object(srv, '_guidance', return_value='guidance'):
            expected = json.dumps({'event': 'new_messages', 'unread_count': 1,
                                   'messages': [{'id': mid, 'from': 'Writer', 'content': 'hello', 'at': NOW.isoformat()}],
                                   'footer': 'guidance'})
            self.assertEqual(self.raw_poll(token=False), expected)
        self.assertEqual(self.rows()[0]['last_read'], mid)
        self.assertEqual(self.poll(token=False)['event'], 'no_new')

    def test_old_token_client_keeps_unread_until_explicit_ack(self):
        mid = self.send()
        self.assertEqual(self.raw_poll(), self.raw_poll())
        self.assertLess(self.rows()[1]['last_read'], mid)
        self.ack(mid)
        self.assertEqual(self.poll()['event'], 'no_new')

    def test_channel_is_checked_before_membership_or_token(self):
        other = self.writer['session_token']
        db = srv.get_db()
        db.execute('UPDATE sessions SET revoked_at = ? WHERE session_token = ?', (NOW.isoformat(), other))
        db.commit()
        db.close()
        srv.nth_end(channel='room', member_id=self.writer['member_id'])
        self.assertTrue(json.loads(srv.nth_cleanup(channel='room'))['ok'])
        for token in ('', self.reader['session_token'], 'invalid-token', other):
            with self.subTest(token_type='original' if token == self.reader['session_token'] else 'other'):
                self.assertEqual(self.poll(token=False, session_token=token), {'event': 'channel_gone'})
        self.assertEqual(self.poll(member_id='different-member'), {'event': 'channel_gone'})

    def test_cleanup_during_a_long_poll_is_gone(self):
        for token in (False, True):
            with self.subTest(token=token):
                with patch.object(srv, '_wait_for_change') as wait:
                    def cleanup(*args):
                        srv.nth_end(channel='room', member_id=self.writer['member_id'])
                        srv.nth_cleanup(channel='room')
                    if token:
                        # Re-create after the preceding tokenless deletion.
                        self.reader = json.loads(srv.nth_connect('test', name='Reader', channel='room'))
                        self.writer = json.loads(srv.nth_connect('test', name='Writer', channel='room'))
                        db = srv.get_db()
                        latest = db.execute('SELECT MAX(id) FROM messages WHERE channel = ?', ('room',)).fetchone()[0]
                        db.close()
                        self.ack(latest)
                    wait.side_effect = cleanup
                    self.assertEqual(self.poll(token=token, wait_seconds=1), {'event': 'channel_gone'})
                    wait.assert_called_once()

    def test_registered_tool_schema_and_calls_expose_optional_arguments(self):
        async def check():
            tool = next(t for t in await srv.mcp.list_tools() if t.name == f'{srv.TOOL_PREFIX}_poll')
            self.assertIn('after_id', tool.inputSchema['properties'])
            self.assertIn('delivery_state', tool.inputSchema['properties'])
            cursor_schema = next(v for v in tool.inputSchema['properties']['after_id']['anyOf'] if v.get('type') == 'integer')
            self.assertEqual(cursor_schema['exclusiveMaximum'], 2**53)
            enums = [v['enum'] for v in tool.inputSchema['properties']['delivery_state']['anyOf'] if 'enum' in v]
            self.assertEqual(enums, [['waiting', 'in_turn', 'unreachable']])
            self.assertNotIn('after_id', tool.inputSchema.get('required', []))
            self.assertNotIn('delivery_state', tool.inputSchema.get('required', []))
            mid = self.send()
            response = await srv.mcp.call_tool(tool.name, dict(channel='room', member_id=self.reader['member_id'],
                                        session_token=self.reader['session_token'], wait_seconds=0,
                                        after_id=mid, delivery_state='waiting'))
            # FastMCP returns (content, structuredContent) with current SDK.
            content = response[0] if isinstance(response, tuple) else response
            self.assertEqual(json.loads(content[0].text)['event'], 'no_new')
            self.assertEqual(self.rows()[0]['delivery_state'], 'waiting')
        asyncio.run(check())


    def test_presence_cannot_be_spoofed_without_the_members_token(self):
        before = self.rows()
        attempts = ('', 'wrong-token', self.writer['session_token'])
        for token in attempts:
            with self.subTest(token_kind='empty' if not token else 'other'):
                reply = self.poll(token=False, session_token=token, delivery_state='waiting')
                self.assertIn('error', reply)
                self.assertIn('session_token', reply['error'])
                self.assertIsNone(self.rows()[0]['delivery_state'])
                self.assertIsNone(self.rows()[0]['delivery_state_at'])
        db = srv.get_db()
        db.execute('UPDATE sessions SET revoked_at = ? WHERE session_token = ?',
                   (NOW.isoformat(), self.reader['session_token']))
        db.commit()
        db.close()
        self.assertIn('error', self.poll(delivery_state='in_turn'))
        self.assertIsNone(self.rows()[0]['delivery_state'])
        self.assertEqual([r['last_read'] for r in before], [r['last_read'] for r in self.rows()])

    def test_legacy_cursor_default_never_acks_skipped_or_returned_ids(self):
        mids = [self.send(str(i)) for i in range(3)]
        before = [r['last_read'] for r in self.rows()]
        self.assertEqual([m['id'] for m in self.poll(token=False, after_id=0)['messages']], mids)
        self.assertEqual([r['last_read'] for r in self.rows()], before)
        result = self.poll(token=False, after_id=mids[0])
        self.assertEqual([m['id'] for m in result['messages']], mids[1:])
        self.assertEqual([r['last_read'] for r in self.rows()], before)
        self.assertEqual([m['id'] for m in self.poll(token=False, auto_ack=False)['messages']], mids)

    def test_empty_legacy_cursor_poll_preserves_both_watermarks(self):
        last = self.send()
        before = [r['last_read'] for r in self.rows()]
        for cursor in (last, last + 10):
            self.assertEqual(self.poll(token=False, after_id=cursor)['event'], 'no_new')
            self.assertEqual([r['last_read'] for r in self.rows()], before)

    def test_ack_during_wait_is_reread_before_delivering_arrivals(self):
        backlog = self.send('backlog')
        arrived = []
        def arrival(*args):
            arrived.append(self.send('acked during wait'))
            self.ack(arrived[0])
            arrived.append(self.send('still unread'))
        with patch.object(srv, '_wait_for_change', side_effect=arrival) as wait:
            result = self.poll(after_id=backlog, wait_seconds=1)
        wait.assert_called_once()
        self.assertEqual([m['id'] for m in result['messages']], arrived[1:])
        self.assertEqual([m['content'] for m in result['messages']], ['still unread'])
        self.assertEqual(self.rows()[1]['last_read'], arrived[0])

    def test_future_cursor_is_not_clamped_to_latest_message(self):
        last = self.send('backlog')
        cursor = last + 100
        ticks = [0.0]
        def arrival(*args):
            self.send('below future cursor')
            ticks[0] = 1.0
        with patch.object(srv.time, 'monotonic', side_effect=lambda: ticks[0]), \
                patch.object(srv, '_wait_for_change', side_effect=arrival) as wait:
            self.assertEqual(self.poll(after_id=cursor, wait_seconds=1)['event'], 'no_new')
        wait.assert_called_once()
        self.assertLess(self.rows()[1]['last_read'], last)
        self.assertEqual([m['content'] for m in self.poll()['messages']], ['backlog', 'below future cursor'])

    def test_presence_omission_preserves_report_and_newer_heartbeat_supersedes_stale_hint(self):
        for state in ('waiting', 'in_turn', 'unreachable'):
            self.server_clock.return_value = NOW.isoformat()
            self.poll(delivery_state=state)
            later = NOW + timedelta(seconds=121)
            self.server_clock.return_value = later.isoformat()
            self.poll(monitor_heartbeat=True)  # a real, unrelated heartbeat without presence
            member, _ = self.rows()
            self.assertEqual(member['last_seen'], later.isoformat())
            self.assertEqual(member['delivery_state_at'], NOW.isoformat())
            self.assertEqual(member['delivery_state'], state)
            with patch.object(web, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = later
                row = self.roster()
            self.assertEqual(row['status'], 'active')
            self.assertNotIn('delivery_label', row)

    def test_presence_never_masks_stronger_roster_states(self):
        for state in ('blocked', 'errored', 'archived', 'sleeping', 'compacting'):
            with self.subTest(state=state):
                self.poll(delivery_state='waiting')
                db = srv.get_db()
                db.execute('UPDATE sessions SET blocked_since = ? WHERE session_token = ?',
                           (NOW.isoformat() if state == 'blocked' else None, self.reader['session_token']))
                db.execute('UPDATE agents SET state = ?, archived_at = ? WHERE id = ?',
                           (state if state != 'blocked' else 'running',
                            NOW.isoformat() if state == 'archived' else None, self.reader['member_id']))
                db.execute('UPDATE members SET status_text = ? WHERE id = ?', ('my own words', self.reader['member_id']))
                db.commit()
                db.close()
                with patch.object(web, 'datetime', wraps=datetime) as clock:
                    clock.now.return_value = NOW
                    row = self.roster()
                self.assertEqual(row['status'], state)
                self.assertNotIn('delivery_label', row)
                self.assertEqual(row['status_text'], 'my own words')

    def test_connect_and_reclaim_clear_old_delivery_reports(self):
        self.poll(delivery_state='waiting')
        secret = self.reader['reclaim_secret']
        resumed = json.loads(srv.nth_connect('test', name='Reader', channel='room',
                            resume_member_id=self.reader['member_id'], reclaim_secret=self.reader['reclaim_secret']))
        self.reader = resumed
        self.assertIsNone(self.rows()[0]['delivery_state'])
        self.assertIsNone(self.rows()[0]['delivery_state_at'])
        # A first attachment to another channel is clear too, without disturbing this room.
        self.poll(delivery_state='in_turn')
        other = json.loads(srv.nth_connect('test', name='Reader', channel='another',
                            resume_member_id=resumed['member_id'], reclaim_secret=secret))
        db = srv.get_db()
        row = db.execute('SELECT delivery_state, delivery_state_at FROM members WHERE channel = ? AND id = ?',
                         ('another', other['member_id'])).fetchone()
        db.close()
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(self.rows()[0]['delivery_state'], 'in_turn')

    def test_presence_is_written_once_per_long_poll_and_renewed_by_next_request(self):
        writes = []
        original_get_db = srv.get_db
        def traced_db():
            db = original_get_db()
            db.set_trace_callback(lambda sql: writes.append(sql) if sql.startswith('UPDATE members SET delivery_state =') else None)
            return db
        def arrival(*args):
            self.server_clock.return_value = (NOW + timedelta(seconds=1)).isoformat()
            self.send('arrival')
        with patch.object(srv, 'get_db', side_effect=traced_db), \
                patch.object(srv, '_wait_for_change', side_effect=arrival):
            self.poll(delivery_state='waiting', wait_seconds=1)
            self.assertEqual(self.rows()[0]['delivery_state_at'], NOW.isoformat())
            self.assertEqual(len(writes), 1)
            self.poll(delivery_state='waiting')
            self.assertEqual(self.rows()[0]['delivery_state_at'], (NOW + timedelta(seconds=1)).isoformat())
            self.assertEqual(len(writes), 2)

    def test_registered_tool_rejects_invalid_arguments_without_writes(self):
        async def check():
            before = self.rows()
            invalid = [('after_id', v) for v in (True, '4', -1, 1.5, 2**53, 2**80)]
            invalid += [('delivery_state', v) for v in ('idle', '', 'WAITING', True, 3, ['waiting'])]
            for name, value in invalid:
                with self.subTest(argument=name, value=value):
                    args = dict(channel='room', member_id=self.reader['member_id'],
                                session_token=self.reader['session_token'], wait_seconds=0, **{name: value})
                    try:
                        response = await srv.mcp.call_tool(f'{srv.TOOL_PREFIX}_poll', args)
                    except Exception as exc:
                        self.assertIn(name, str(exc))  # SDK validation errors are refusals too
                    else:
                        content = response[0] if isinstance(response, tuple) else response
                        self.assertIn('error', json.loads(content[0].text))
                    after = self.rows()
                    self.assertEqual(after, before)
        asyncio.run(check())

    def test_cursor_largest_safe_integer_is_accepted(self):
        self.assertEqual(self.poll(after_id=2**53 - 1)['event'], 'no_new')

    def test_legacy_golden_bytes_and_ack_effects_for_both_client_modes(self):
        # Independent JSON goldens, never built from the returned response.
        goldens = {
            'empty': '{"event": "no_new", "unread_count": 0, "reminder": "No new messages, but stay connected."}',
            'unread': '{"event": "new_messages", "unread_count": 1, "messages": [{"id": 3, "from": "Writer", "content": "hello", "at": "2030-01-02T12:30:00+00:00"}], "footer": "guidance"}',
            'filtered': '{"event": "new_messages", "unread_count": 1, "messages": [{"id": 3, "from": "Writer", "content": "hello", "at": "2030-01-02T12:30:00+00:00"}], "footer": "guidance", "filtered_by": "Writer"}',
            'unmatched': '{"event": "no_new", "unread_count": 1, "reminder": "No matching messages yet, but stay connected. Other members may need you. Keep polling until the channel ends or your user tells you to stop."}',
            'enriched': '{"event": "new_messages", "unread_count": 1, "messages": [{"id": 3, "from": "Writer", "content": "hello", "at": "2030-01-02T12:30:00+00:00", "mentioned": true, "referenced": true, "banged": true}], "footer": "guidance", "has_mentions": true}',
            'ended': '{"event": "ended", "ended_by": "Writer", "unread_count": 1, "unread": [{"id": 3, "from": "Writer", "content": "hello", "at": "2030-01-02T12:30:00+00:00"}]}',
            'ended_empty': '{"event": "ended", "ended_by": "Writer", "unread_count": 0, "unread": []}',
        }
        for token in (False, True):
            for kind, expected in goldens.items():
                with self.subTest(token=token, kind=kind):
                    db = srv.get_db()
                    db.execute('DELETE FROM messages WHERE channel = ?', ('room',))
                    db.execute('UPDATE members SET last_read = 0 WHERE channel = ?', ('room',))
                    db.execute('UPDATE sessions SET last_read = 0 WHERE channel = ?', ('room',))
                    db.execute('UPDATE channels SET status = ?, ended_by = ? WHERE code = ?',
                               ('ended' if kind.startswith('ended') else 'active', self.writer['member_id'], 'room'))
                    if kind not in ('empty', 'ended_empty'):
                        flags = json.dumps([self.reader['member_id']]) if kind == 'enriched' else ''
                        db.execute('INSERT INTO messages (id, channel, member_id, member_name, content, created_at, mentions, refs, bangs) VALUES (3, ?, ?, ?, ?, ?, ?, ?, ?)',
                                   ('room', self.writer['member_id'], 'Writer', 'hello', NOW.isoformat(), flags, flags, flags))
                    db.commit()
                    db.close()
                    kwargs = {'from_name': 'Writer'} if kind == 'filtered' else {'from_name': 'Nobody'} if kind == 'unmatched' else {}
                    with patch.object(srv, '_sentinel_nag', return_value=''), patch.object(srv, '_guidance', return_value='guidance'):
                        self.assertEqual(self.raw_poll(token=token, **kwargs), expected)
                    member, session = self.rows()
                    self.assertEqual(session['last_read'], 0)
                    self.assertEqual(member['last_read'], 3 if not token and kind in ('unread', 'enriched') else 0)


class FakeClient:
    def __init__(self, url):
        self.endpoint_url = 'http://hub.example/messages/session-a'
        self.supports = True
        self.calls = []
        self.schemas = 0
        self.connects = 0
        self.closed = False
        self.paginated = False

    def connect(self):
        self.connects += 1

    def call(self, method, params):
        assert method == 'tools/list'
        self.schemas += 1
        if self.paginated and not params.get('cursor'):
            return {'tools': [], 'nextCursor': 'next'}
        properties = {'after_id': {'type': 'integer'}} if self.supports else {}
        return {'tools': [{'name': 'quartet_poll', 'inputSchema': {'properties': properties}}]}

    def call_tool(self, name, arguments, timeout):
        assert name == 'quartet_poll'
        if not self.supports and 'after_id' in arguments:
            raise AssertionError('old hub refuses unsupported cursor')
        self.calls.append(dict(arguments))
        return {'event': 'no_new'}

    def close(self):
        self.closed = True


class QuartetCursorTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient('http://hub.example/sse')
        with patch('nth_sse_client.MCPSSEClient', return_value=self.client):
            self.poll, self.close = listener.quartet_poll_factory({'url': 'http://hub.example/sse'})

    def test_schema_support_is_checked_once_and_cursor_tracks_arguments(self):
        for high_water in (0, 15, 28):
            arguments = {'wait_seconds': 0, 'after_id': high_water}
            self.poll(arguments)
            self.assertEqual(self.client.calls[-1]['after_id'], high_water)
            self.assertNotIn('delivery_state', self.client.calls[-1])
            self.assertEqual(arguments, {'wait_seconds': 0, 'after_id': high_water})
        self.assertEqual(self.client.schemas, 1)
        self.assertEqual(self.client.connects, 1)
        self.close()
        self.assertTrue(self.client.closed)

    def test_old_hub_omits_cursor_without_mutating_arguments(self):
        self.client.supports = False
        arguments = {'wait_seconds': 0, 'after_id': 28}
        for _ in range(2):
            self.poll(arguments)
        self.assertEqual(self.client.calls, [{'wait_seconds': 0}] * 2)
        self.assertEqual(arguments['after_id'], 28)
        self.assertEqual(self.client.schemas, 1)

    def test_schema_is_rechecked_after_reconnect_and_upgrade(self):
        self.client.supports = False
        self.poll({'wait_seconds': 0, 'after_id': 7})
        self.client.endpoint_url = 'http://hub.example/messages/session-b'
        self.client.supports = True
        self.poll({'wait_seconds': 0, 'after_id': 9})
        self.assertEqual(self.client.schemas, 2)
        self.assertEqual(self.client.calls, [{'wait_seconds': 0}, {'wait_seconds': 0, 'after_id': 9}])

    def test_paginated_schema_is_supported(self):
        self.client.paginated = True
        self.poll({'wait_seconds': 0, 'after_id': 9})
        self.assertEqual(self.client.schemas, 2)
        self.assertEqual(self.client.calls[-1]['after_id'], 9)

    def test_listener_supplies_current_high_water(self):
        class Hub:
            prefix = 'quartet'
            source = 'quartet'
            url = 'http://hub.example/sse'
        seen = []
        def poll(arguments):
            seen.append(arguments)
            if len(seen) == 1:
                return {'event': 'new_messages', 'messages': [{'id': 12, 'content': 'ambient'}]}
            active._stop.set()
            return {'event': 'no_new'}
        active = listener.Listener(Hub(), {'channel': 'room', 'member_id': 'reader',
                                          'session_token': 'synthetic-token', 'filter': 'at'}, poll, high_water=8)
        active._loop()
        self.assertEqual([args['after_id'] for args in seen], [8, 12])
        self.assertTrue(all('delivery_state' not in args for args in seen))


    def test_only_the_poll_tool_can_advertise_cursor_support(self):
        for tools in (
            [{'name': 'quartet_history', 'inputSchema': {'properties': {'after_id': {}}}},
             {'name': 'quartet_poll', 'inputSchema': {'properties': {}}}],
            [{'name': 'quartet_poll', 'inputSchema': {'properties': {}}},
             {'name': 'quartet_history', 'inputSchema': {'properties': {'after_id': {}}}}],
            [{'name': 'quartet_history', 'inputSchema': {'properties': {'after_id': {}}}}],
        ):
            self.client.supports = False
            self.client.endpoint_url += '-next'
            with patch.object(self.client, 'call', return_value={'tools': tools}):
                self.poll({'wait_seconds': 0, 'after_id': 9})
            self.assertEqual(self.client.calls[-1], {'wait_seconds': 0})

    def test_reconnect_downgrade_discards_cached_cursor_support(self):
        self.poll({'wait_seconds': 0, 'after_id': 7})
        self.client.endpoint_url += '-old'
        self.client.supports = False
        self.poll({'wait_seconds': 0, 'after_id': 9})
        self.assertEqual(self.client.schemas, 2)
        self.assertEqual(self.client.calls[-1], {'wait_seconds': 0})

    def test_discovery_exception_falls_back_then_retries_successfully(self):
        for exception in (TimeoutError('schema timeout'), ValueError('bad schema')):
            self.client.endpoint_url += '-retry'
            with patch.object(self.client, 'call', side_effect=exception):
                self.assertEqual(self.poll({'wait_seconds': 0, 'after_id': 9})['event'], 'no_new')
            self.assertEqual(self.client.calls[-1], {'wait_seconds': 0})
            self.poll({'wait_seconds': 0, 'after_id': 10})
            self.assertEqual(self.client.calls[-1], {'wait_seconds': 0, 'after_id': 10})
        self.assertEqual(self.client.schemas, 2)

    def test_discovery_is_bounded_and_incomplete_schema_falls_back(self):
        pages = []
        def endless(method, params):
            pages.append(params)
            return {'tools': [{'name': 'quartet_poll', 'inputSchema': {'properties': {'after_id': {}}}}],
                    'nextCursor': str(len(pages))}
        with patch.object(self.client, 'call', side_effect=endless):
            self.poll({'wait_seconds': 0, 'after_id': 9})
        self.assertEqual(len(pages), 10)
        self.assertEqual(self.client.calls[-1], {'wait_seconds': 0})
        self.poll({'wait_seconds': 0, 'after_id': 10})
        self.assertEqual(self.client.calls[-1], {'wait_seconds': 0, 'after_id': 10})

    def test_malformed_schema_falls_back_without_poisoning_cache(self):
        for bad in (None, {'tools': [None]}, {'tools': [{'name': 'quartet_poll', 'inputSchema': None}]}):
            self.client.endpoint_url += '-malformed'
            with patch.object(self.client, 'call', return_value=bad):
                self.poll({'wait_seconds': 0, 'after_id': 9})
            self.assertEqual(self.client.calls[-1], {'wait_seconds': 0})
            self.poll({'wait_seconds': 0, 'after_id': 10})
            self.assertEqual(self.client.calls[-1], {'wait_seconds': 0, 'after_id': 10})


if __name__ == '__main__':
    unittest.main(verbosity=2)
