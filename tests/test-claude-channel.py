"""Offline invariants for Claude channel delivery; no credentials, model calls or live hub."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_claude_channel as channel_module
from nth_claude_channel import ChannelHub, METHOD, UNCONFIRMED, channel_mode, format_event

TOKEN = 'test-capability'
MESSAGES = [
    {'id': 1, 'from': 'peer', 'content': 'ambient'},
    {'id': 2, 'from': 'peer', 'content': '@receiver hello', 'mentioned': True},
    {'id': 3, 'from': 'peer', 'content': '#receiver context', 'referenced': True},
    {'id': 4, 'from': 'peer', 'content': '@other !receiver', 'banged': True}]


class RecordingWriter:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.on_send = lambda: None

    async def send(self, session_message):
        if self.closed:
            raise RuntimeError('transport closed')
        self.sent.append(session_message.message.root)
        self.on_send()


class ScriptedSource:
    """Returns each scripted response once, then `idle` on every later poll.

    A hub returns the same unread backlog until the agent acks, so a test that
    replaces a listener sets `idle` to that backlog.
    """
    def __init__(self, responses):
        self.responses = list(responses)
        self.idle = {'event': 'no_new', 'messages': []}
        self.calls = []
        self.closed = False

    def factory(self, binding):
        def poll(arguments):
            self.calls.append(dict(arguments))
            if self.responses:
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            time.sleep(.05)
            return self.idle

        def close():
            self.closed = True
        return poll, close


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.02)
    return False


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.writer = RecordingWriter()
        self.hubs = []

    def tearDown(self):
        for hub in self.hubs:
            hub.stop_all()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def hub(self, responses, prefix='quartet', source='quartet'):
        self.source = ScriptedSource(responses)
        hub = ChannelHub(prefix, source, 'http://hub.example/sse', self.source.factory)
        hub.attach(self.loop, self.writer)
        self.hubs.append(hub)
        return hub

    def pushed_ids(self):
        return [int(note.params['meta']['message_id']) for note in self.writer.sent]

    def test_mode_requires_claude_and_the_launcher_flag(self):
        for env, expected in (({'TRIO_NATIVE_CLIENT': 'claude', 'TRIO_CLAUDE_CHANNEL': '1'}, True),
                              ({'TRIO_NATIVE_CLIENT': 'claude'}, False),
                              ({'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CLAUDE_CHANNEL': '1'}, False),
                              ({'TRIO_CLAUDE_CHANNEL': '1'}, False)):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(channel_mode(), expected, env)

    def test_event_matches_relay_payload_and_host_meta_contract(self):
        content, meta = format_event('quartet', 'test', 'receiver', MESSAGES[1])
        lead, body = content.split('\n', 1)
        self.assertIn('quartet_ack', lead)
        self.assertIn('untrusted', lead)
        self.assertEqual(json.loads(body), {'event': 'new_messages', 'channel': 'test',
                                            'event_id': 'test:2', 'messages': [MESSAGES[1]]})
        self.assertEqual(meta['message_id'], '2')
        self.assertEqual((meta['mentioned'], meta['banged'], meta['referenced']), ('true', 'false', 'false'))
        for key, value in meta.items():
            self.assertTrue(key.isidentifier(), key)
            self.assertIsInstance(value, str)

    def test_filters_are_per_message_and_bangs_survive_every_filter(self):
        for mode, expected in (('at', [2, 4]), ('about', [2, 3, 4]), ('all', [1, 2, 3, 4])):
            self.writer.sent.clear()
            hub = self.hub([{'event': 'new_messages', 'has_mentions': True, 'messages': MESSAGES}])
            hub.start('test', 'receiver', TOKEN, mode)
            self.assertTrue(wait_until(lambda: len(self.writer.sent) == len(expected)), mode)
            self.assertEqual(self.pushed_ids(), expected)
            hub.stop_all()
        poll = self.source.calls[0]
        self.assertFalse(poll['mentions_only'])
        self.assertFalse(poll['auto_ack'])
        self.assertTrue(poll['monitor_heartbeat'])

    def test_notification_shape_and_no_credentials_on_the_wire(self):
        hub = self.hub([{'event': 'new_messages', 'messages': [MESSAGES[1]]}])
        hub.start('test', 'receiver', TOKEN, 'about')
        self.assertTrue(wait_until(lambda: self.writer.sent))
        note = self.writer.sent[0]
        self.assertEqual(note.method, METHOD)
        self.assertFalse(hasattr(note, 'id'))
        self.assertNotIn(TOKEN, json.dumps(note.params))

    def test_unacknowledged_backlog_is_not_pushed_twice(self):
        backlog = {'event': 'new_messages', 'messages': [MESSAGES[1]]}
        newer = {'event': 'new_messages', 'messages': [MESSAGES[1], MESSAGES[3]]}
        hub = self.hub([backlog, backlog, newer])
        with patch.object(channel_module.threading.Event, 'wait', lambda self, timeout=None: self.is_set()):
            hub.start('test', 'receiver', TOKEN, 'about')
            self.assertTrue(wait_until(lambda: len(self.source.calls) >= 4))
        self.assertEqual(self.pushed_ids(), [2, 4])

    def test_refused_membership_ends_without_reclaiming(self):
        hub = self.hub([{'error': 'session revoked'}])
        hub.start('test', 'receiver', TOKEN)
        self.assertTrue(wait_until(lambda: hub.status('test', 'receiver', TOKEN)[0]['status'] == 'ended'))
        time.sleep(.2)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(self.writer.sent, [])
        self.assertTrue(wait_until(lambda: self.source.closed))

    def test_channel_end_stops_the_listener(self):
        hub = self.hub([{'event': 'ended', 'ended_by': 'peer'}])
        hub.start('test', 'receiver', TOKEN)
        self.assertTrue(wait_until(lambda: hub.status('test', 'receiver', TOKEN)[0]['error'] == 'channel ended'))

    def test_transport_errors_reconnect_without_leaking_details(self):
        hub = self.hub([RuntimeError('token=' + TOKEN), {'event': 'new_messages', 'messages': [MESSAGES[1]]}])
        with patch.object(channel_module.threading.Event, 'wait', lambda self, timeout=None: self.is_set()):
            hub.start('test', 'receiver', TOKEN, 'about')
            self.assertTrue(wait_until(lambda: self.pushed_ids() == [2]))
        self.assertNotIn(TOKEN, json.dumps(hub.status('test', 'receiver', TOKEN)))

    def test_closed_transport_ends_delivery_and_keeps_the_message_unseen(self):
        self.writer.closed = True
        hub = self.hub([{'event': 'new_messages', 'messages': [MESSAGES[1]]}])
        hub.start('test', 'receiver', TOKEN, 'about')
        self.assertTrue(wait_until(lambda: hub.status('test', 'receiver', TOKEN)[0]['error'] == 'transport closed'))
        state = hub.status('test', 'receiver', TOKEN)[0]
        self.assertEqual((state['status'], state['written'], state['last_written_message_id']), ('ended', 0, 0))

    def test_a_busy_event_loop_delays_a_write_but_never_drops_it(self):
        hub = self.hub([])
        self.loop.call_soon_threadsafe(time.sleep, 1.0)   # a synchronous tool holding the loop
        self.assertTrue(hub.push('late but delivered', {'message_id': '9'}))
        self.assertEqual(self.pushed_ids(), [9])

    def test_status_is_unconfirmed_and_scoped_to_the_token(self):
        hub = self.hub([{'event': 'new_messages', 'messages': [MESSAGES[1]]}])
        hub.start('test', 'receiver', TOKEN, 'about')
        self.assertTrue(wait_until(lambda: self.writer.sent))
        state = hub.status('test', 'receiver', TOKEN)[0]
        self.assertEqual(state['delivery'], UNCONFIRMED)
        self.assertEqual((state['provider'], state['transport'], state['written']), ('claude', 'channel', 1))
        self.assertNotIn('accepted', json.dumps(state))
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertEqual(hub.status('test', 'receiver', 'someone-elses-token'), [])
        self.assertEqual(hub.configure('test', 'receiver', 'someone-elses-token', enabled=False), [])
        self.assertTrue(hub.status('test', 'receiver', TOKEN)[0]['enabled'])

    def test_stop_stays_stopped_and_listen_restarts_from_credentials(self):
        hub = self.hub([])
        self.assertEqual(hub.status('test', 'receiver', TOKEN), [])
        started = hub.configure('test', 'receiver', TOKEN, enabled=True)   # fresh process: no prior connect
        self.assertEqual((started[0]['filter'], started[0]['enabled']), ('about', True))
        stopped = hub.configure('test', 'receiver', TOKEN, enabled=False)
        self.assertEqual((stopped[0]['status'], stopped[0]['enabled']), ('stopped', False))
        again = hub.configure('test', 'receiver', TOKEN, filter_mode='at', enabled=True)
        self.assertEqual((again[0]['filter'], again[0]['enabled']), ('at', True))
        with self.assertRaises(ValueError):
            hub.configure('test', 'receiver', TOKEN, filter_mode='everything')

    def test_filter_change_replaces_the_listener_instead_of_stacking(self):
        hub = self.hub([])
        hub.start('test', 'receiver', TOKEN, 'about')
        first = hub.listeners[('test', 'receiver')]
        hub.configure('test', 'receiver', TOKEN, filter_mode='all')
        self.assertEqual(len(hub.listeners), 1)
        self.assertIsNot(hub.listeners[('test', 'receiver')], first)
        self.assertTrue(wait_until(lambda: not first.thread.is_alive()))

    def test_filter_change_does_not_replay_what_was_already_written(self):
        hub = self.hub([])
        self.source.idle = {'event': 'new_messages', 'messages': MESSAGES}   # never acked
        hub.start('test', 'receiver', TOKEN, 'at')
        self.assertTrue(wait_until(lambda: self.pushed_ids() == [2, 4]))
        hub.configure('test', 'receiver', TOKEN, filter_mode='all')
        later = {'id': 5, 'from': 'peer', 'content': 'ambient, after the change'}
        self.source.idle = {'event': 'new_messages', 'messages': MESSAGES + [later]}
        self.assertTrue(wait_until(lambda: 5 in self.pushed_ids()))
        self.assertEqual(self.pushed_ids(), [2, 4, 5])

    def test_filter_change_alone_never_reenables_an_explicit_stop(self):
        hub = self.hub([])
        hub.start('test', 'receiver', TOKEN, 'about')
        hub.configure('test', 'receiver', TOKEN, enabled=False)
        stopped = hub.listeners[('test', 'receiver')]
        changed = hub.configure('test', 'receiver', TOKEN, filter_mode='all')
        self.assertEqual((changed[0]['status'], changed[0]['enabled'], changed[0]['filter']),
                         ('stopped', False, 'all'))
        self.assertIs(hub.listeners[('test', 'receiver')], stopped)
        resumed = hub.configure('test', 'receiver', TOKEN, enabled=True)
        self.assertEqual((resumed[0]['filter'], resumed[0]['enabled']), ('all', True))
        # Without a listener there is nothing to change, and nothing is started.
        self.assertEqual(hub.configure('test', 'stranger', TOKEN, filter_mode='all'), [])
        self.assertNotIn(('test', 'stranger'), hub.listeners)

    def test_credentials_that_do_not_own_a_listener_never_replace_it(self):
        hub = self.hub([])
        hub.start('test', 'receiver', TOKEN, 'about')
        first = hub.listeners[('test', 'receiver')]
        for attempt in ({'enabled': True}, {'filter_mode': 'all'}, {'filter_mode': 'at', 'enabled': True}):
            self.assertEqual(hub.configure('test', 'receiver', 'someone-elses-token', **attempt), [], attempt)
            self.assertIs(hub.listeners[('test', 'receiver')], first)
            self.assertTrue(first.thread.is_alive())
            self.assertEqual(first.binding['filter'], 'about')
        hub.configure('test', 'receiver', TOKEN, enabled=False)           # a stopped one is still owned
        self.assertEqual(hub.configure('test', 'receiver', 'someone-elses-token', enabled=True), [])
        self.assertIs(hub.listeners[('test', 'receiver')], first)

    def test_an_ended_listener_can_be_restarted_with_fresh_credentials(self):
        hub = self.hub([{'error': 'session revoked'}])
        hub.start('test', 'receiver', TOKEN)
        self.assertTrue(wait_until(lambda: hub.status('test', 'receiver', TOKEN)[0]['status'] == 'ended'))
        fresh = hub.configure('test', 'receiver', 'token-after-reconnect', enabled=True)
        self.assertEqual(fresh[0]['enabled'], True)
        self.assertEqual(hub.status('test', 'receiver', TOKEN), [])

    def test_stop_withdraws_a_write_still_queued_behind_a_busy_loop(self):
        hub = self.hub([])
        release, stopped, result = threading.Event(), threading.Event(), []
        self.loop.call_soon_threadsafe(release.wait, 10)   # a synchronous tool holding the loop
        worker = threading.Thread(target=lambda: result.append(
            hub.push('queued', {'message_id': '9'}, cancelled=stopped.is_set)))
        worker.start()
        time.sleep(.4)
        self.assertEqual(result, [])                       # still waiting: never dropped on its own
        stopped.set()
        worker.join(timeout=3)
        self.assertEqual(result, [False])
        release.set()
        time.sleep(.4)
        self.assertEqual(self.writer.sent, [])             # nothing escaped after the stop
        self.assertFalse(hub.push('after stop', {'message_id': '10'}, cancelled=stopped.is_set))

    def test_stop_mid_batch_loses_nothing_and_a_reenable_repeats_nothing(self):
        hub = self.hub([])
        self.source.idle = {'event': 'new_messages', 'messages': MESSAGES}
        self.writer.on_send = hub.stop_all                 # stop lands right after the first write
        hub.start('test', 'receiver', TOKEN, 'all')
        first = hub.listeners[('test', 'receiver')]
        self.assertTrue(wait_until(lambda: not first.thread.is_alive()))
        self.assertEqual((self.pushed_ids(), first.high_water), ([1], 1))
        self.writer.on_send = lambda: None
        hub.configure('test', 'receiver', TOKEN, enabled=True)
        self.assertTrue(wait_until(lambda: len(self.writer.sent) == 4))
        self.assertEqual(self.pushed_ids(), [1, 2, 3, 4])


if __name__ == '__main__':
    unittest.main()
