"""Hook delivery for a plainly launched Claude: isolated NTH_HOME, scripted polls, no host."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SERVER_DIR = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER_DIR))
import nth_claude_channel as channel_module
import nth_claude_hook as hook

SESSION = 'c4244eb6-d069-4d02-9932-1ff0185e13c4'
KEY = '0123456789abcdef01234567'
TOKEN = 'test-capability'
# Stands in for arbitrary peer text: distinctive, so a test can assert it never
# reaches the model-facing stream. Not an instruction; the point is that content
# of any kind stays out of stderr.
PEER_MARK = 'PEER-CONTENT-MARKER-9c1f'


def message(mid, **flags):
    return dict({'id': mid, 'from': 'sender ' + PEER_MARK, 'content': PEER_MARK}, **flags)


class ScriptedPolls:
    """Each scripted response once, then the idle answer. Records every call."""
    def __init__(self, responses):
        self.responses, self.calls, self.closed = list(responses), [], 0
        self.idle = {'event': 'no_new', 'messages': []}

    def factory(self, identity):
        def poll(arguments):
            self.calls.append(dict(arguments))
            if self.responses:
                return self.responses.pop(0)
            time.sleep(.02)
            return self.idle

        def close():
            self.closed += 1
        return poll, close

def clean_env():
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(('TRIO_', 'NTH_', 'CLAUDE'))}
    return keep


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, dict(clean_env(), NTH_HOME=str(self.home), NTH_QUIET='1'),
                              clear=True)
        self.env.start()
        (self.home / 'events' / 'identities').mkdir(parents=True)
        # A faster, patchable clock everywhere the waiter looks at the world.
        self.fast = patch.multiple(hook, TICK_SECONDS=.02, SETTLE_SECONDS=.02,
                                   STATUS_EVERY_SECONDS=.1, UNSUPERVISED_LIFETIME_SECONDS=.4)
        self.fast.start()

    def tearDown(self):
        self.fast.stop()
        self.env.stop()
        self.temp.cleanup()

    def identity(self, key=KEY, channel='room', member_id='member', token=TOKEN, source='local'):
        record = {'channel': channel, 'member_id': member_id, 'session_token': token,
                  'reclaim_secret': '', 'source': source, 'url': 'http://hub.example/sse'
                  if source != 'local' else str((self.home / 'nth.db').resolve())}
        path = self.home / 'events' / 'identities' / (key + '.json')
        path.write_text(json.dumps(record), encoding='utf-8')
        return record

    def connect_payload(self, key=KEY, channel='room', member_id='member', tool='mcp__nth-trio__trio_connect'):
        body = {'channel': channel, 'member_id': member_id, 'session_token': TOKEN,
                'identity_file': str(self.home / 'events' / 'identities' / (key + '.json'))}
        return {'session_id': SESSION, 'tool_name': tool,
                'tool_response': json.dumps({'result': json.dumps(body)})}

    def run_wait(self, responses, session_id=SESSION):
        polls = ScriptedPolls(responses)
        say = io.StringIO()
        with patch.object(hook, 'poll_factory', polls.factory), \
             patch.object(hook, 'claude_pid', return_value=None), \
             patch.object(hook, 'SAY', say):
            code = hook.wait(session_id)
        return code, say.getvalue(), polls

    # ---- register (the tool hook) ------------------------------------------------

    def test_a_connect_records_the_membership_from_its_identity_file(self):
        self.identity()
        hook.register(self.connect_payload())
        state = hook.load_session(SESSION)
        self.assertEqual(list(state['memberships']), [KEY])
        self.assertEqual(state['memberships'][KEY],
                         {'source': 'local', 'channel': 'room', 'member_id': 'member'})
        self.assertFalse(state['ended'])

    def test_a_result_naming_an_unknown_or_mismatched_identity_is_ignored(self):
        # No identity file on disk for the named key.
        hook.register(self.connect_payload())
        self.assertIsNone(hook.load_session(SESSION))
        # A file that disagrees with the result about the membership is not trusted.
        self.identity(channel='other-room')
        hook.register(self.connect_payload(channel='room'))
        self.assertIsNone(hook.load_session(SESSION))

    def test_a_reconnect_replaces_the_old_key_and_keeps_the_mark(self):
        self.identity(key=KEY)
        hook.register(self.connect_payload(key=KEY))
        with hook.session_update(SESSION) as state:
            state['high_water'][KEY] = 7
        new_key = 'abcdef0123456789abcdef01'
        self.identity(key=new_key)                       # same channel+member, rotated token/key
        hook.register(self.connect_payload(key=new_key))
        state = hook.load_session(SESSION)
        self.assertEqual(list(state['memberships']), [new_key])
        self.assertEqual(state['high_water'].get(new_key), 7)

    def test_an_ack_advances_only_a_membership_this_session_holds(self):
        self.identity()
        hook.register(self.connect_payload())
        ack = {'session_id': SESSION, 'tool_name': 'mcp__nth-trio__trio_ack',
               'tool_input': {'channel': 'room', 'member_id': 'member', 'through_id': 5},
               'tool_response': json.dumps({'result': json.dumps({'ok': True})})}
        hook.register(ack)
        self.assertEqual(hook.load_session(SESSION)['acked'][KEY], 5)

    # ---- wait (the stop/tool hook body) ------------------------------------------

    def test_a_message_that_passes_the_filter_wakes_and_names_no_peer_text(self):
        self.identity()
        hook.register(self.connect_payload())
        code, said, polls = self.run_wait([{'event': 'new_messages', 'messages': [message(2, mentioned=True)]}])
        self.assertEqual(code, 2)
        self.assertIn('1 new trio message', said)
        self.assertIn('id 2', said)
        self.assertIn('room', said)
        self.assertIn('trio_poll', said)
        self.assertIn('trio_ack', said)
        # The whole point: no peer content and no sender name in the model-facing line.
        self.assertNotIn(PEER_MARK, said)
        self.assertNotIn(TOKEN, said)
        # The mark advanced, so a later waiter does not repeat this wake.
        self.assertEqual(hook.load_session(SESSION)['high_water'][KEY], 2)

    def test_a_look_only_session_with_no_membership_starts_nothing(self):
        # Session state exists (a status check happened) but holds no membership.
        with hook.session_update(SESSION, create=True):
            pass
        code, said, polls = self.run_wait([{'event': 'new_messages', 'messages': [message(2, mentioned=True)]}])
        self.assertEqual((code, said, polls.calls), (0, '', []))

    def test_a_message_the_filter_declines_does_not_wake(self):
        self.identity()
        hook.register(self.connect_payload())
        # about-filter default: an unaddressed message is not for us. With no end
        # event, the waiter idles until its (shortened) unsupervised lifetime, then
        # leaves without a wake. The mark still advances past what it declined.
        code, said, polls = self.run_wait([{'event': 'new_messages', 'messages': [message(2)]}])
        self.assertEqual(code, 0)
        self.assertEqual(said, '')
        self.assertEqual(hook.load_session(SESSION)['high_water'][KEY], 2)

    def test_a_second_waiter_for_the_same_session_declines(self):
        self.identity()
        hook.register(self.connect_payload())
        import threading
        held = threading.Event()
        release = threading.Event()

        def hold():
            with hook.file_lock(hook.session_path(SESSION, '.lock'), blocking=False) as ok:
                if ok:
                    held.set()
                    release.wait(5)

        keeper = threading.Thread(target=hold, daemon=True)
        keeper.start()
        self.assertTrue(held.wait(5))
        try:
            self.assertEqual(self.run_wait([{'event': 'new_messages', 'messages': [message(2, mentioned=True)]}])[0], 0)
        finally:
            release.set()
            keeper.join(5)

    def test_an_ended_membership_wakes_once_then_is_silent(self):
        self.identity()
        hook.register(self.connect_payload())
        code, said, polls = self.run_wait([{'event': 'ended', 'unread_count': 0}])
        self.assertEqual(code, 2)
        self.assertIn('delivery has stopped', said.lower())
        self.assertIn('The channel was ended', said)
        # Recorded as ended, so the next arming skips it and says nothing more.
        self.assertEqual(hook.membership_config(KEY)['ended'], 'channel ended')
        code2, said2, _ = self.run_wait([{'event': 'new_messages', 'messages': [message(3, mentioned=True)]}])
        self.assertEqual((code2, said2), (0, ''))

    # ---- main (dispatch + guards) ------------------------------------------------

    def run_main(self, event, payload, home=None):
        with patch('sys.stdin', io.StringIO(json.dumps(payload))), \
             patch.object(hook, 'wait', return_value=0) as waited, \
             patch.object(sys, 'stderr', io.StringIO()):
            argv = (['--home', home] if home else []) + [event]
            code = hook.main(argv)
        return code, waited

    def test_main_stands_down_inside_a_channel_session(self):
        with patch.dict(os.environ, {'TRIO_CLAUDE_CHANNEL': '1'}):
            code, waited = self.run_main('stop', {'session_id': SESSION})
        self.assertEqual(code, 0)
        waited.assert_not_called()

    def test_main_ignores_a_missing_or_malformed_session_id(self):
        for payload in ({'session_id': ''}, {'session_id': 'has spaces'}, {'no': 'session'}):
            code, waited = self.run_main('stop', payload)
            self.assertEqual(code, 0)
            waited.assert_not_called()

    def test_main_end_marks_the_session_over(self):
        self.identity()
        hook.register(self.connect_payload())
        with patch('sys.stdin', io.StringIO(json.dumps({'session_id': SESSION}))), \
             patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(hook.main(['end']), 0)
        self.assertTrue(hook.load_session(SESSION)['ended'])


if __name__ == '__main__':
    unittest.main()
