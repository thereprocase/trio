"""nth_watch.py --once: pass the first event that needs the agent through, then stop."""
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_watch


class Stop(Exception):
    pass


class OnceStdoutTests(unittest.TestCase):
    def stream(self, wake=nth_watch.WAKE_EVENTS):
        real = io.StringIO()
        codes = []

        def fake_exit(code):
            codes.append(code)
            raise Stop

        return real, codes, nth_watch.OnceStdout(real, wake, exit=fake_exit)

    def test_keepalive_cadence_filter_mode_and_errors_keep_it_waiting(self):
        # cadence would re-fire on every relaunch: the monitors remember it only in memory.
        real, codes, out = self.stream()
        for event in ('filter_mode', 'keepalive', 'cadence', 'error'):
            print(json.dumps({'event': event}), file=out, flush=True)
        print('not json', file=out)
        self.assertEqual(real.getvalue(), '')
        self.assertEqual(codes, [])
        self.assertEqual(json.loads(out.last_error)['event'], 'error')

    def test_the_first_waking_event_is_printed_once_and_exits_zero(self):
        real, codes, out = self.stream()
        line = json.dumps({'event': 'new_messages', 'message_ids': [7], 'count': 1})
        with self.assertRaises(Stop):
            out.write(line[:10])
            out.write(line[10:] + '\n')
        self.assertEqual(real.getvalue(), line + '\n')
        self.assertEqual(codes, [0])

    def test_channel_end_and_revocation_wake(self):
        for event in ('channel_ended', 'channel_gone', 'culled', 'session_revoked', 'poll_refused'):
            real, codes, out = self.stream()
            with self.assertRaises(Stop):
                print(json.dumps({'event': event}), file=out)
            self.assertEqual(codes, [0], event)

    def test_keepalive_wakes_only_when_asked(self):
        real, codes, out = self.stream(nth_watch.WAKE_EVENTS | {'keepalive'})
        with self.assertRaises(Stop):
            print(json.dumps({'event': 'keepalive'}), file=out)
        self.assertEqual(codes, [0])


if __name__ == '__main__':
    unittest.main()
