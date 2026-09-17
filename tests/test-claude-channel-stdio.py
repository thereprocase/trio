"""A fake Claude host drives the real local frontend over stdio; isolated NTH_HOME,
no credentials, model calls or live hub."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SERVER = Path(__file__).resolve().parents[1] / 'server' / 'nth_server.py'
CHANNEL_METHOD = 'notifications/claude/channel'


class Host:
    """Minimal MCP stdio client: enough of Claude Code to initialize and call tools."""

    def __init__(self, home, channel_flag):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('TRIO_', 'NTH_'))}
        env.update(NTH_HOME=str(home), NTH_QUIET='1', TRIO_NATIVE_CLIENT='claude')
        if channel_flag:
            env['TRIO_CLAUDE_CHANNEL'] = '1'
        self.process = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', bufsize=1, env=env)
        self.frames = queue.Queue()
        self.events = []
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.capabilities = self.request('initialize', {
            'protocolVersion': '2025-06-18', 'capabilities': {},
            'clientInfo': {'name': 'fake-claude-host', 'version': '0'}})['capabilities']
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def _read(self):
        for line in self.process.stdout:
            line = line.strip()
            if line:
                self.frames.put(json.loads(line))

    def _send(self, frame):
        self.process.stdin.write(json.dumps(frame) + '\n')
        self.process.stdin.flush()

    def request(self, method, params, timeout=30):
        self.next_id += 1
        self._send({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.frames.get(timeout=max(.1, deadline - time.monotonic()))
            if frame.get('id') == self.next_id:
                if 'error' in frame:
                    raise RuntimeError(frame['error'])
                return frame['result']
            self._keep(frame)
        raise TimeoutError(method)

    def _keep(self, frame):
        if frame.get('method') == CHANNEL_METHOD:
            self.events.append(frame['params'])

    def tool(self, tool_name, /, **arguments):   # positional-only: trio_connect itself takes name=
        result = self.request('tools/call', {'name': tool_name, 'arguments': arguments})
        return json.loads(result['content'][0]['text'])

    def drain(self, seconds):
        """Collect channel events for a while; returns everything seen so far."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._keep(self.frames.get(timeout=.1))
            except queue.Empty:
                pass
        return self.events

    def wait_events(self, count, timeout=20):
        deadline = time.monotonic() + timeout
        while len(self.events) < count and time.monotonic() < deadline:
            self.drain(.2)
        return self.events

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=10)
        except Exception:
            self.process.kill()
            self.process.wait(timeout=10)
        finally:
            self.process.stdout.close()


class StdioChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        try:
            self.temp.cleanup()
        except OSError:
            pass   # Windows can hold the SQLite WAL a moment longer

    def host(self, channel_flag=True):
        host = Host(self.temp.name, channel_flag)
        self.hosts.append(host)
        return host

    def join(self, host, name):
        body = host.tool('trio_connect', summary='offline test member', name=name, channel='channel-test')
        self.assertTrue(body.get('ok'), body)
        return body

    def test_channel_mode_delivers_filtered_messages_as_push_events(self):
        host = self.host()
        self.assertIn('claude/channel', host.capabilities.get('experimental', {}))
        receiver = self.join(host, 'receiver')
        sender = self.join(host, 'sender')
        delivery = receiver['event_delivery']
        self.assertEqual((delivery['provider'], delivery['mode']), ('claude', 'channel'))
        self.assertEqual(delivery['readiness'], 'unverified')
        self.assertIn('A successful join is not readiness', receiver['instructions'])
        self.assertEqual(receiver['monitor_hint'], '')
        self.assertNotIn(receiver['session_token'], json.dumps(delivery))
        self.assertIn(delivery['listener']['status'], ('starting', 'listening'))

        def say(text):
            return host.tool('trio_send', channel='channel-test', member_id=sender['member_id'],
                             message=text, session_token=sender['session_token'])['message_id']

        def receiver_events():
            return [e for e in host.events if e['meta']['member_id'] == receiver['member_id']]

        ambient = say('ambient chatter, no sigil')
        mention = say('@receiver please look')
        host.wait_events(1)
        host.drain(2.5)                       # time for a wrongly pushed ambient message to show up
        ids = [int(e['meta']['message_id']) for e in receiver_events()]
        self.assertEqual(ids, [mention])
        self.assertNotIn(ambient, ids)
        event = receiver_events()[0]
        lead, body = event['content'].split('\n', 1)
        self.assertIn('trio_ack', lead)
        payload = json.loads(body)
        self.assertEqual((payload['event'], payload['event_id']), ('new_messages', f'channel-test:{mention}'))
        self.assertEqual(payload['messages'][0]['id'], mention)
        self.assertEqual((event['meta']['mentioned'], event['meta']['sender']), ('true', 'sender'))
        self.assertNotIn(receiver['session_token'], json.dumps(event))

        status = host.tool('trio_delivery_status', channel='channel-test',
                           member_id=receiver['member_id'], session_token=receiver['session_token'])
        self.assertEqual((status['state'], status['ready'], status['hint']), ('listening', True, ''))
        stranger = host.tool('trio_delivery_status', channel='channel-test',
                             member_id=receiver['member_id'], session_token='not-the-token')
        self.assertEqual((stranger['state'], stranger['ready'], stranger['listeners']), ('not_attached', False, []))
        self.assertTrue(stranger['hint'].startswith('Setup is incomplete:'))
        self.assertIn('tell your peers', stranger['hint'])
        listener = status['listeners'][0]
        self.assertEqual((listener['transport'], listener['last_written_message_id']), ('channel', mention))
        self.assertIn('no receipt', listener['delivery'])
        self.assertNotIn('accepted', json.dumps(status))
        self.assertEqual(listener['confirmed_through'], 0)

        # A woken model leaves its token out. The frontend supplies the one it holds,
        # so the ack moves the session's own watermark (a tokenless ack would move only
        # the legacy per-member one, and the backlog would be written again after a
        # restart) and counts as delivery evidence.
        host.tool('trio_ack', channel='channel-test', member_id=receiver['member_id'], through_id=mention)
        unread = host.tool('trio_poll', channel='channel-test', member_id=receiver['member_id'],
                           session_token=receiver['session_token'], wait_seconds=0, auto_ack=False)
        self.assertEqual(unread.get('messages', []), [])
        # No server footer may tell a channel session to start a Monitor.
        say('@receiver one more, to read by poll')
        polled = host.tool('trio_poll', channel='channel-test', member_id=receiver['member_id'],
                           session_token=receiver['session_token'], wait_seconds=0, auto_ack=False)
        self.assertIn('3-call cadence', polled['footer'])
        self.assertNotIn('MONITOR', polled['footer'].upper().replace('NOT USE A MONITOR', ''))
        self.assertIn('trio_delivery_status', polled['footer'])
        host.tool('trio_ack', channel='channel-test', member_id=receiver['member_id'],
                  through_id=polled['messages'][-1]['id'])
        status = host.tool('trio_delivery_status', channel='channel-test',
                           member_id=receiver['member_id'], session_token=receiver['session_token'])
        # At least the pushed mention is confirmed. Whether the polled message was
        # also pushed before its ack is a race, and either outcome is correct.
        self.assertGreaterEqual(status['listeners'][0]['confirmed_through'], mention)
        self.assertNotIn(receiver['session_token'], json.dumps(status))

        # Stop stays stopped.
        stopped = host.tool('trio_listen', channel='channel-test', member_id=receiver['member_id'],
                            session_token=receiver['session_token'], enabled=False)
        self.assertEqual(stopped['listeners'][0]['status'], 'stopped')
        # A filter change with enabled omitted keeps the stop and remembers the filter.
        changed = host.tool('trio_listen', channel='channel-test', member_id=receiver['member_id'],
                            session_token=receiver['session_token'], filter_mode='all')
        self.assertEqual((changed['listeners'][0]['status'], changed['listeners'][0]['filter']), ('stopped', 'all'))
        status = host.tool('trio_delivery_status', channel='channel-test',
                           member_id=receiver['member_id'], session_token=receiver['session_token'])
        self.assertEqual((status['state'], status['ready']), ('stopped', False))
        # A stop the user asked for is not a fault to repair.
        self.assertTrue(status['hint'].startswith('Delivery is off:'))
        self.assertIn('stays stopped', status['hint'])
        self.assertIn('tell your peers', status['hint'])
        self.assertIn('no receipt', status['delivery'])
        silent = say('@receiver while you were stopped')
        host.drain(3)
        self.assertNotIn(silent, [int(e['meta']['message_id']) for e in receiver_events()])

        # Restart from credentials on the 'at' filter; a bang survives it, a #ref does not.
        host.tool('trio_ack', channel='channel-test', member_id=receiver['member_id'],
                  through_id=silent, session_token=receiver['session_token'])
        restarted = host.tool('trio_listen', channel='channel-test', member_id=receiver['member_id'],
                              session_token=receiver['session_token'], filter_mode='at', enabled=True)
        self.assertEqual(restarted['listeners'][0]['filter'], 'at')
        before = len(receiver_events())
        reference = say('#receiver was mentioned in passing')
        bang = say('@sender !receiver urgent')
        host.wait_events(len(host.events) + 1)
        host.drain(2.5)
        fresh = [int(e['meta']['message_id']) for e in receiver_events()[before:]]
        self.assertEqual(fresh, [bang])
        self.assertNotIn(reference, fresh)
        self.assertEqual(receiver_events()[-1]['meta']['banged'], 'true')

    def test_without_the_launcher_flag_nothing_is_pushed(self):
        host = self.host(channel_flag=False)
        self.assertNotIn('claude/channel', host.capabilities.get('experimental') or {})
        receiver = self.join(host, 'receiver')
        sender = self.join(host, 'sender')
        self.assertEqual(receiver['event_delivery']['mode'], 'monitor')
        self.assertTrue(receiver['monitor_hint'])
        self.assertEqual(receiver['event_delivery']['readiness'], 'unverified')
        self.assertIn('A successful join is not readiness', receiver['instructions'])
        self.assertIn('30-minute lease', receiver['instructions'])
        self.assertNotIn('listener', receiver['event_delivery'])
        host.tool('trio_send', channel='channel-test', member_id=sender['member_id'],
                  message='@receiver hello', session_token=sender['session_token'])
        self.assertEqual(host.drain(3), [])
        # A Claude session that does run a Monitor keeps the Monitor guidance.
        polled = host.tool('trio_poll', channel='channel-test', member_id=receiver['member_id'],
                           session_token=receiver['session_token'], wait_seconds=0, auto_ack=False)
        self.assertIn('RESTART YOUR BACKGROUND MONITOR NOW', polled['footer'])


if __name__ == '__main__':
    unittest.main()
