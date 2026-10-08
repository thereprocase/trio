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
import time
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
        self.clock.start()

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
        started = time.monotonic()
        self.assertEqual(self.poll(after_id=last, wait_seconds=1)['event'], 'no_new')
        self.assertGreaterEqual(time.monotonic() - started, 0.8)

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
        srv.nth_end(channel='room', member_id=self.writer['member_id'])
        result = self.poll(after_id=mids[1])
        self.assertEqual(result['event'], 'ended')
        self.assertTrue(all(m['id'] > mids[1] for m in result['unread']))
        self.assertEqual(result['unread_count'], len(result['unread']))

    def test_invalid_cursors_are_refused(self):
        for value in (-1, 1.5, True, '4'):
            with self.subTest(value=value):
                self.assertEqual(self.poll(after_id=value), {'error': 'after_id must be an integer at least 0.'})

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

    def test_presence_stales_after_two_minutes_despite_other_heartbeats(self):
        for state in ('waiting', 'in_turn', 'unreachable'):
            self.poll(delivery_state=state)
            with patch.object(web, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = NOW + timedelta(seconds=120)
                clock.fromtimestamp.side_effect = datetime.fromtimestamp
                self.assertNotIn('silent since', self.roster()['delivery_label'])
                clock.now.return_value = NOW + timedelta(seconds=121)
                row = self.roster()
            self.assertEqual((row['status'], row['delivery_label']), ('stale', 'silent since 12:30'))

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
        srv.nth_end(channel='room', member_id=self.writer['member_id'])
        self.assertTrue(json.loads(srv.nth_cleanup(channel='room'))['ok'])
        for token in (False, True):
            self.assertEqual(self.poll(token=token), {'event': 'channel_gone'})

    def test_cleanup_during_a_long_poll_is_gone(self):
        def cleanup(*args):
            srv.nth_end(channel='room', member_id=self.writer['member_id'])
            srv.nth_cleanup(channel='room')
        with patch.object(srv, '_wait_for_change', side_effect=cleanup):
            self.assertEqual(self.poll(wait_seconds=1), {'event': 'channel_gone'})

    def test_registered_tool_schema_and_calls_expose_optional_arguments(self):
        async def check():
            tool = next(t for t in await srv.mcp.list_tools() if t.name == f'{srv.TOOL_PREFIX}_poll')
            self.assertIn('after_id', tool.inputSchema['properties'])
            self.assertIn('delivery_state', tool.inputSchema['properties'])
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
