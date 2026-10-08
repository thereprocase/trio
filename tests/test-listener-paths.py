"""The Listener's loop, driven one scripted poll at a time: retries, terminal outcomes,
malformed messages and crashes.

The loop runs in the test's own thread with its stop event replaced by a recorder,
so nothing here sleeps and every delay the loop asked for is an assertion:

  * the retry delays (2 s per consecutive failure, capped at 30 s) and their reset;
  * the status and error text shown between retries;
  * how many hub calls each script cost, and the wait_seconds of each;
  * what each terminal reply reports, how often, and after what wait;
  * what a poll with a null or malformed message list does;
  * how a crash, a failed write and a stop racing a write end the listener.

Where the code does something a reader might not expect, the test pins the present
behaviour and says so in a comment; those are questions for the owner, not decisions.

Usage: python tests/test-listener-paths.py
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
os.environ.setdefault('NTH_QUIET', '1')
import nth_claude_hook as hook  # noqa: E402
import nth_listener as nl  # noqa: E402

CHANNEL_ENDED_ADVICE = 'The channel was ended. Stop work for it and tell the user.'


def message(mid, **flags):
    return dict({'id': mid, 'from': 'peer', 'content': 'text'}, **flags)


def new_messages(*messages):
    return {'event': 'new_messages', 'messages': list(messages)}


NO_NEW = {'event': 'no_new', 'messages': []}


class RecordingStop:
    """Stands in for the listener's stop Event: records each wait and never blocks."""

    def __init__(self, listener):
        self.listener, self.flag = listener, False
        self.waits, self.snapshots = [], []
        self.stop_on_wait = None

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds=None):
        self.waits.append(seconds)
        self.snapshots.append((self.listener.status, self.listener.error))
        if seconds == self.stop_on_wait:
            self.flag = True
        return self.flag


class Sink:
    """The sink a Listener reports to. Records every push; `raises` lists the push
    numbers (from 1) that raise, and `refuses` those that return False."""

    def __init__(self, prefix='quartet', raises=(), refuses=(), on_push=None):
        self.prefix, self.raises, self.refuses, self.on_push = prefix, set(raises), set(refuses), on_push
        self.attempts = []                  # (content, meta, cancelled, cancelled() at push time)

    def push(self, content, meta, cancelled=None):
        self.attempts.append((content, meta, cancelled, cancelled() if cancelled else None))
        if self.on_push:
            self.on_push(len(self.attempts))
        if len(self.attempts) in self.raises:
            raise RuntimeError('write failed')
        return len(self.attempts) not in self.refuses

    @property
    def metas(self):
        return [attempt[1] for attempt in self.attempts]


class Run:
    """One Listener loop over a script of poll replies (or exceptions to raise).

    When the script runs out the next poll stops the listener and returns no news, so
    the loop ends by itself. That last poll is not recorded in `calls`.
    """

    def __init__(self, script, filter_mode='about', sink=None, high_water=0, stop_on_wait=None, start=True):
        self.script, self.calls = list(script), []
        self.sink = sink or Sink()
        self.closed = 0
        self.listener = nl.Listener(
            self.sink, {'source': 'quartet', 'url': 'http://hub.example/sse', 'channel': 'room',
                        'member_id': 'me', 'session_token': 'secret-token', 'filter': filter_mode},
            self.poll, close=self.close, high_water=high_water)
        self.stop = self.listener._stop = RecordingStop(self.listener)
        self.stop.stop_on_wait = stop_on_wait
        self.sentinel = False
        if start:
            self.go()

    def go(self):
        self.listener._run()
        return self

    def close(self):
        self.closed += 1

    def poll(self, arguments):
        if not self.script:
            self.sentinel = True
            self.stop.set()
            return dict(NO_NEW)
        self.calls.append(dict(arguments))
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def waits(self):
        return self.stop.waits

    @property
    def snapshots(self):
        return self.stop.snapshots

    @property
    def wait_seconds(self):
        return [call['wait_seconds'] for call in self.calls]


class RetryTests(unittest.TestCase):
    def test_invalid_replies_back_off_two_seconds_a_failure_and_a_good_poll_resets_it(self):
        run = Run([{'_raw': 'Error executing tool quartet_poll'}, None, ['not', 'a', 'poll'], NO_NEW,
                   {'_raw': 'again'}])
        self.assertEqual(len(run.calls), 5)
        self.assertEqual(run.waits, [2, 4, 6, nl.MIN_POLL_GAP_SECONDS, 2])
        self.assertEqual(run.snapshots, [('reconnecting', 'no poll result')] * 3
                         + [('listening', '')] + [('reconnecting', 'no poll result')])
        # The zero-wait first poll is spent only by a poll that succeeded.
        self.assertEqual(run.wait_seconds, [0, 0, 0, 0, nl.POLL_WAIT_SECONDS])
        self.assertEqual(run.sink.attempts, [])
        self.assertEqual(run.closed, 1)

    def test_the_delay_stops_growing_at_thirty_seconds(self):
        run = Run([{'_raw': 'down'}] * 17)
        self.assertEqual(len(run.calls), 17)
        self.assertEqual(run.waits, [min(2 * failure, 30) for failure in range(1, 18)])
        self.assertEqual(run.waits[-3:], [30, 30, 30])
        self.assertEqual(run.sink.attempts, [])

    def test_a_raising_poll_is_retried_on_the_same_schedule_and_never_shows_its_text(self):
        run = Run([RuntimeError('token=secret-token'), KeyError('x'), NO_NEW, ValueError('late')])
        self.assertEqual(run.waits, [2, 4, nl.MIN_POLL_GAP_SECONDS, 2])
        self.assertEqual(run.snapshots, [('reconnecting', 'RuntimeError'), ('reconnecting', 'KeyError'),
                                         ('listening', ''), ('reconnecting', 'ValueError')])
        self.assertNotIn('secret-token', repr(run.snapshots))
        self.assertEqual(run.sink.attempts, [])

    def test_invalid_replies_and_raises_share_one_failure_count(self):
        run = Run([RuntimeError('x'), {'_raw': 'y'}, RuntimeError('z'), NO_NEW])
        self.assertEqual(run.waits, [2, 4, 6, nl.MIN_POLL_GAP_SECONDS])

    def test_every_poll_asks_for_the_whole_backlog_and_never_acks(self):
        run = Run([NO_NEW, NO_NEW], filter_mode='at')
        for call in run.calls:
            self.assertEqual((call['auto_ack'], call['mentions_only'], call['monitor_heartbeat']),
                             (False, False, True))
            self.assertEqual((call['channel'], call['member_id'], call['monitor_filter']), ('room', 'me', 'at'))
        self.assertEqual(run.wait_seconds, [0, nl.POLL_WAIT_SECONDS])


class TerminalOutcomeTests(unittest.TestCase):
    CASES = [
        ({'error': 'Invalid or revoked session_token.'}, 'membership refused', [nl.REFUSAL_GRACE_SECONDS]),
        ({'error': 'session_token does not match member_id.'}, 'membership refused',
         [nl.REFUSAL_GRACE_SECONDS]),
        ({'error': 'You are not a member of this channel.'}, 'member removed', []),
        ({'event': 'ended', 'unread_count': 0}, 'channel ended', []),
        ({'ended': True}, 'channel ended', []),
        ({'event': 'channel_gone'}, 'channel ended', []),
        ({'event': 'channel_not_found'}, 'channel ended', []),
        ({'error': 'channel_not_found'}, 'channel ended', []),
    ]

    def test_each_terminal_reply_reports_once_after_its_own_wait_and_costs_one_call(self):
        for reply, reason, waits in self.CASES:
            with self.subTest(reply=reply):
                run = Run([reply, NO_NEW, NO_NEW])
                self.assertEqual(len(run.calls), 1)
                self.assertFalse(run.sentinel)
                self.assertEqual(run.waits, waits)
                self.assertEqual(len(run.sink.attempts), 1)
                content, meta, _, _ = run.sink.attempts[0]
                self.assertEqual((meta['event'], meta['reason'], meta['channel'], meta['member_id']),
                                 ('delivery_ended', reason, 'room', 'me'))
                self.assertIn('"event":"delivery_ended"', content)
                self.assertEqual((run.listener.status, run.listener.error, run.listener.state),
                                 ('ended', reason, 'ended'))
                self.assertEqual(run.closed, 1)

    def test_a_refusal_replaced_during_its_grace_says_nothing(self):
        run = Run([{'error': 'Invalid or revoked session_token.'}], stop_on_wait=nl.REFUSAL_GRACE_SECONDS)
        self.assertEqual(run.sink.attempts, [])
        self.assertEqual(run.snapshots, [('reconnecting', 'membership refused')])
        self.assertEqual(run.listener.state, 'stopped')

    def test_a_refusal_after_good_polls_is_still_a_refusal(self):
        run = Run([NO_NEW, {'error': 'Invalid or revoked session_token.'}])
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(run.sink.metas[0]['reason'], 'membership refused')

    def test_the_old_hub_end_reports_unread_messages_only_when_there_are_some(self):
        history = 'read them with quartet_history.'
        cases = [({'event': 'ended', 'unread_count': 3},
                  'The channel was ended. It closed with 3 message(s) you had not read: ' + history
                  + ' Stop work for it and tell the user.'),
                 ({'event': 'ended', 'unread_count': 1}, None),
                 ({'event': 'ended', 'unread_count': 0}, CHANNEL_ENDED_ADVICE),
                 ({'event': 'ended'}, CHANNEL_ENDED_ADVICE),
                 ({'event': 'ended', 'unread_count': -1}, CHANNEL_ENDED_ADVICE),
                 ({'event': 'ended', 'unread_count': '3'}, CHANNEL_ENDED_ADVICE),
                 ({'event': 'ended', 'unread_count': None}, CHANNEL_ENDED_ADVICE),
                 ({'event': 'ended', 'unread_count': 2.0}, CHANNEL_ENDED_ADVICE),
                 ({'channel_gone': True, 'event': 'channel_gone', 'unread_count': 4}, None),
                 ({'error': 'channel_not_found'}, CHANNEL_ENDED_ADVICE)]
        for reply, advice in cases:
            with self.subTest(reply=reply):
                content = Run([reply]).sink.attempts[0][0]
                lead = content.split('\n', 1)[0]
                if advice is not None:
                    self.assertTrue(lead.endswith(advice), lead)
                if advice is None or 'closed with' in advice:
                    self.assertIn('It closed with', lead)
                else:
                    self.assertNotIn('It closed with', lead)

    def test_unread_count_true_is_counted_as_an_integer(self):
        # PIN, behaviour question: True is an int in Python, so a JSON true in unread_count
        # reads "It closed with True message(s)". The hub only ever sends a count.
        lead = Run([{'event': 'ended', 'unread_count': True}]).sink.attempts[0][0].split('\n', 1)[0]
        self.assertIn('It closed with True message(s)', lead)

    def test_the_notice_names_the_sinks_prefix(self):
        lead = Run([{'event': 'ended', 'unread_count': 2}], sink=Sink('trio')).sink.attempts[0][0]
        self.assertIn('trio_history', lead.split('\n', 1)[0])


class MalformedMessageTests(unittest.TestCase):
    def test_a_null_or_malformed_message_list_selects_nothing_and_keeps_listening(self):
        for poll in (new_messages(), {'event': 'new_messages', 'messages': None}, {'event': 'new_messages'},
                     {'event': 'no_new', 'messages': None}):
            with self.subTest(poll=poll):
                run = Run([poll, poll])
                self.assertEqual(run.sink.attempts, [])
                self.assertEqual(run.listener.status, 'listening')
                self.assertEqual(run.waits, [nl.MIN_POLL_GAP_SECONDS] * 2)
                self.assertEqual(run.snapshots, [('listening', '')] * 2)

    def test_a_message_list_that_is_not_a_list_is_treated_as_a_stuck_backlog(self):
        # PIN, behaviour question: a truthy non-list ('x', {'id': 1}) is not a null list, so
        # the loop reads it as an unread backlog it cannot use and backs off like one
        # (2, 4, 8, 10 s) instead of the 1 s floor. Nothing ends and nothing is written.
        for bad in ('x', {'id': 1}, 7):
            with self.subTest(messages=bad):
                poll = {'event': 'new_messages', 'messages': bad}
                run = Run([poll] * 5)
                self.assertEqual(run.sink.attempts, [])
                self.assertEqual(run.listener.status, 'listening')
                self.assertEqual(run.waits, [2.0, 4.0, 8.0, 10.0, 10.0])

    def test_a_list_of_junk_is_a_stuck_backlog_and_a_poll_with_no_messages_resets_it(self):
        junk = new_messages(None, 'x', 3)
        run = Run([junk, junk, NO_NEW, junk])
        self.assertEqual(run.waits, [2.0, 4.0, nl.MIN_POLL_GAP_SECONDS, 2.0])
        self.assertEqual(run.sink.attempts, [])

    def test_junk_beside_a_good_message_does_not_hide_it(self):
        run = Run([new_messages(None, 'x', message(True, mentioned=True), message(7, mentioned=True))])
        self.assertEqual(len(run.sink.attempts), 1)
        self.assertEqual((run.sink.metas[0]['message_id'], run.sink.metas[0]['count']), ('7', '1'))
        self.assertEqual(run.listener.high_water, 7)

    def test_out_of_order_ids_are_delivered_in_order_and_the_mark_is_the_highest(self):
        run = Run([new_messages(message(5, mentioned=True), message(3, mentioned=True))])
        meta = run.sink.metas[0]
        self.assertEqual((meta['first_message_id'], meta['message_id'], meta['count']), ('3', '5', '2'))
        self.assertEqual(run.listener.high_water, 5)

    def test_a_repeated_id_is_delivered_once(self):
        run = Run([new_messages(message(3, mentioned=True), message(3, mentioned=True))])
        self.assertEqual(run.sink.metas[0]['count'], '1')

    def test_ids_at_or_below_the_mark_are_not_delivered_again(self):
        run = Run([new_messages(*[message(i, mentioned=True) for i in (4, 5, 6)])], high_water=5)
        self.assertEqual((run.sink.metas[0]['message_id'], run.sink.metas[0]['count']), ('6', '1'))
        self.assertEqual(run.listener.high_water, 6)

    def test_what_the_filter_declines_is_marked_seen(self):
        run = Run([new_messages(message(4), message(2))], filter_mode='at')
        self.assertEqual(run.sink.attempts, [])
        self.assertEqual(run.listener.high_water, 4)
        self.assertEqual(run.waits, [nl.MIN_POLL_GAP_SECONDS])

    def test_ids_that_are_not_plain_integers_are_dropped_and_leave_the_member_deaf(self):
        # PIN, behaviour question: a message whose id is a string, a float, a bool, zero or
        # negative is dropped by _fresh. Because the list is truthy and nothing in it is
        # fresh, the loop reads it as a stuck backlog: nothing is delivered, the status
        # stays `listening`, and the poll backs off. A hub that sent string ids would make
        # every membership deaf without saying so.
        for bad in ('5', 5.0, True, 0, -2, None):
            with self.subTest(id=bad):
                run = Run([new_messages(dict(message(1, mentioned=True), id=bad))] * 3)
                self.assertEqual(run.sink.attempts, [])
                self.assertEqual(run.listener.high_water, 0)
                self.assertEqual(run.listener.status, 'listening')
                self.assertEqual(run.waits, [2.0, 4.0, 8.0])


class RateLimitTests(unittest.TestCase):
    def test_a_rate_limited_poll_waits_and_marks_nothing_seen(self):
        with patch.object(nl, 'PUSH_BURST', 1), patch.object(nl, 'PUSH_REFILL_SECONDS', 100.0):
            run = Run([new_messages(message(2, mentioned=True)), new_messages(message(2, mentioned=True),
                                                                              message(3, mentioned=True))])
        self.assertEqual(len(run.sink.attempts), 1)
        self.assertEqual(run.listener.high_water, 2)
        self.assertEqual(len(run.calls), 2)
        # The second poll is held for the rest of the refill period, not for the poll floor.
        self.assertGreater(run.waits[1], 90)
        self.assertEqual(run.listener.high_water, 2)


class CrashAndWriteTests(unittest.TestCase):
    class Exploding(dict):
        """A reply that classifies as a poll and then fails when its messages are read."""

        def get(self, key, default=None):
            if key == 'messages':
                raise KeyError(key)
            return super().get(key, default)

    def test_a_crash_ends_delivery_once_naming_the_exception_class_only(self):
        run = Run([self.Exploding(event='new_messages')])
        self.assertEqual(len(run.sink.attempts), 1)
        content, meta, _, _ = run.sink.attempts[0]
        self.assertEqual((meta['event'], meta['reason']), ('delivery_ended', 'KeyError'))
        self.assertEqual((run.listener.status, run.listener.error), ('ended', 'KeyError'))
        self.assertIn('The listener failed unexpectedly. Tell the user, and check quartet_delivery_status.',
                      content.split('\n', 1)[0])
        self.assertEqual(run.closed, 1)

    def test_a_notice_that_cannot_be_written_still_leaves_the_listener_ended(self):
        run = Run([self.Exploding(event='new_messages')], sink=Sink(raises={1}))
        self.assertEqual(len(run.sink.attempts), 1)
        self.assertEqual((run.listener.status, run.listener.error), ('ended', 'KeyError'))
        self.assertEqual(run.closed, 1)

    def test_a_failed_message_write_ends_with_a_notice_that_is_written(self):
        run = Run([new_messages(message(2, mentioned=True))], sink=Sink(raises={1}))
        self.assertEqual(len(run.sink.attempts), 2)
        self.assertEqual(run.sink.metas[1]['reason'], 'RuntimeError')
        self.assertEqual(run.listener.high_water, 0)                 # a successor still gets message 2

    def test_a_stop_racing_a_failing_write_says_nothing(self):
        sink = Sink(raises={1}, on_push=lambda number: run.listener.stop())
        run = Run([new_messages(message(2, mentioned=True))], sink=sink, start=False)
        run.go()
        self.assertEqual(len(run.sink.attempts), 1)
        self.assertNotEqual(run.listener.status, 'ended')
        self.assertEqual(run.listener.state, 'stopped')

    def test_a_refused_write_ends_as_a_closed_transport_without_a_notice(self):
        run = Run([new_messages(message(2, mentioned=True)), NO_NEW], sink=Sink(refuses={1}))
        self.assertEqual(len(run.sink.attempts), 1)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual((run.listener.status, run.listener.error), ('ended', 'transport closed'))
        self.assertEqual(run.listener.high_water, 0)

    def test_a_refused_write_during_a_stop_is_a_stop_not_an_end(self):
        sink = Sink(refuses={1}, on_push=lambda number: run.listener.stop())
        run = Run([new_messages(message(2, mentioned=True))], sink=sink, start=False)
        run.go()
        self.assertNotEqual(run.listener.status, 'ended')
        self.assertEqual(run.listener.state, 'stopped')

    def test_a_stop_while_a_poll_is_in_flight_writes_nothing(self):
        box = []

        class StopsDuringPoll(Run):
            def poll(self, arguments):
                reply = super().poll(arguments)
                if not self.sentinel and not box:
                    box.append(True)
                    self.listener.stop()
                return reply

        for reply in (new_messages(message(2, mentioned=True)), {'error': 'Invalid or revoked session_token.'},
                      {'error': 'You are not a member of this channel.'}, {'event': 'ended'}):
            with self.subTest(reply=reply):
                del box[:]
                run = StopsDuringPoll([reply])
                self.assertEqual(run.sink.attempts, [])
                self.assertEqual(run.listener.high_water, 0)
                self.assertEqual(run.listener.state, 'stopped')

    def test_the_sink_is_told_how_to_tell_that_the_listener_stopped(self):
        run = Run([new_messages(message(2, mentioned=True)), {'event': 'ended'}])
        self.assertEqual(len(run.sink.attempts), 2)
        for content, meta, cancelled, at_push in run.sink.attempts:
            self.assertTrue(callable(cancelled))
            self.assertIs(at_push, False)
        run.listener.stop()
        self.assertTrue(all(attempt[2]() for attempt in run.sink.attempts))


class WakeSinkTests(unittest.TestCase):
    META = {'channel': 'room', 'member_id': 'me', 'message_id': '5', 'first_message_id': '5',
            'count': '1', 'more_unread': '0', 'mentioned': 'true', 'banged': 'false'}

    def sink(self):
        wake = hook.Wake(None)
        return wake, hook.WakeFor(wake, '0' * 24, 'trio')

    def test_a_cancelled_push_leaves_the_wake_untouched(self):
        wake, sink = self.sink()
        tokens = wake.tokens
        for meta in (self.META, {'event': 'delivery_ended', 'channel': 'room', 'member_id': 'me',
                                 'reason': 'channel ended'}):
            self.assertFalse(sink.push('peer text', meta, cancelled=lambda: True))
        self.assertEqual((wake.lines, wake.ended, wake.tokens, wake.fired.is_set()), ([], {}, tokens, False))

    def test_a_push_that_is_not_cancelled_is_written_and_costs_a_token(self):
        wake, sink = self.sink()
        tokens = wake.tokens
        self.assertTrue(sink.push('peer text', self.META, cancelled=lambda: False))
        self.assertTrue(sink.push('peer text', self.META))
        self.assertEqual(len(wake.lines), 2)
        self.assertEqual(wake.tokens, tokens - 2)
        self.assertTrue(wake.fired.is_set())

    def test_a_malformed_event_is_refused_without_a_trace(self):
        wake, sink = self.sink()
        tokens = wake.tokens
        for meta in (None, {}, dict(self.META, message_id='x'), dict(self.META, count=None)):
            self.assertFalse(sink.push('', meta))
        self.assertEqual((wake.lines, wake.ended, wake.tokens, wake.fired.is_set()), ([], {}, tokens, False))

    def test_a_delivery_ended_event_marks_the_membership_ended(self):
        wake, sink = self.sink()
        meta = {'event': 'delivery_ended', 'channel': 'room', 'member_id': 'me', 'reason': 'member removed'}
        self.assertTrue(sink.push('', meta))
        self.assertEqual(wake.ended, {'0' * 24: 'member removed'})
        self.assertIn('member removed', wake.lines[0])


class WakeBucketTests(unittest.TestCase):
    def test_the_bucket_follows_the_hooks_own_constants(self):
        with patch.multiple(hook, PUSH_BURST=2, PUSH_REFILL_SECONDS=100):
            fresh = hook.Wake(None)
            self.assertEqual((fresh.burst, fresh.refill, fresh.tokens), (2.0, 100.0, 2.0))
            empty = hook.Wake({'tokens': 0, 'at': hook.time.time() - 50})
            self.assertAlmostEqual(empty.tokens, .5, places=1)
            self.assertAlmostEqual(empty.delay(), 50, delta=1)
            self.assertEqual(hook.Wake({'tokens': 99, 'at': hook.time.time()}).tokens, 2.0)
            self.assertEqual(hook.Wake({'tokens': 0, 'at': hook.time.time() + 1000}).tokens, 2.0)
        with patch.multiple(hook, PUSH_BURST=5, PUSH_REFILL_SECONDS=10):
            self.assertEqual(hook.Wake(None).tokens, 5.0)

    def test_a_listener_built_for_the_hook_asks_the_wake_not_its_own_bucket(self):
        wake = hook.Wake(None)
        wake.tokens = 0.0
        wake.at = hook.time.time()
        seen = {}

        def fake_factory(identity):
            return (lambda arguments: dict(NO_NEW)), None

        identity = {'source': 'quartet', 'url': 'http://hub.example/sse', 'channel': 'room',
                    'member_id': 'me', 'session_token': 't'}
        with patch.object(hook, 'poll_factory', fake_factory), \
                patch.object(nl.Listener, 'start', lambda self: None):
            listener = hook.make_listener(wake, '0' * 24, identity, {'filter': 'about'}, 0)
        seen['delay'] = listener._push_delay()
        self.assertGreater(seen['delay'], 0)
        self.assertEqual(listener.tokens, float(nl.PUSH_BURST))      # its own bucket is never read


if __name__ == '__main__':
    unittest.main()
