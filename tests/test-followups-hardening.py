"""Malformed events and spoke metadata cannot produce notices or unsafe JSON."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'server'))
import nth_notice as notice
import nth_listener as listener
import nth_spoke_monitor as spoke
import nth_claude_hook as hook


class HardeningTests(unittest.TestCase):
    META = {'channel': 'room', 'member_id': 'member-1', 'message_id': '5',
            'first_message_id': '5', 'count': '1'}

    def test_only_known_events_produce_notices(self):
        for event in (None, '', 'cadence', 'no_new', 'unknown', [], {}):
            with self.subTest(event=event):
                self.assertIsNone(notice.from_event('trio', dict(self.META, event=event)))
        self.assertIsNone(notice.from_event('trio', self.META))
        self.assertIsNotNone(notice.from_event('trio', dict(self.META, event='new_messages')))
        self.assertIsNotNone(notice.from_event('trio', dict(self.META, event='delivery_ended')))

    def test_listener_messages_are_tagged_before_the_hook_adapter(self):
        _, meta = listener.format_event('trio', 'room', 'member-1', [{'id': 5, 'content': 'synthetic'}])
        self.assertEqual(meta['event'], 'new_messages')
        self.assertIsNotNone(notice.from_event('trio', meta))

    def test_hook_keeps_legacy_metadata_but_rejects_unknown_events(self):
        wake = hook.Wake(None)
        sink = hook.WakeFor(wake, '0' * 24, 'trio')
        self.assertTrue(sink.push('', self.META))
        self.assertEqual(len(wake.lines), 1)
        self.assertFalse(sink.push('', dict(self.META, event='cadence')))
        self.assertEqual(len(wake.lines), 1)

    def test_all_nonfinite_gaps_become_json_null(self):
        for gap in (float('inf'), float('-inf'), float('nan')):
            with self.subTest(gap=gap):
                self.assertIsNone(spoke.gap_for_emit(gap))
                self.assertEqual(json.dumps({'gap': spoke.gap_for_emit(gap)}, allow_nan=False), '{"gap": null}')
        self.assertEqual(spoke.gap_for_emit(1.7), 2)
        self.assertEqual(spoke.gap_for_emit(-1.7), -2)

    def test_json_and_native_id_lists_discard_nonstrings(self):
        values = ['member-1', 1, None, True, {}, [], 'member-2']
        expected = ['member-1', 'member-2']
        self.assertEqual(spoke.parse_id_list(values), expected)
        self.assertEqual(spoke.parse_id_list(json.dumps(values)), expected)
        for bad in ('{}', 'null', 'broken', 1, None):
            self.assertEqual(spoke.parse_id_list(bad), [])


if __name__ == '__main__':
    unittest.main()
