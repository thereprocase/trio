"""Quartet stdio frontend against a fake hub; isolated NTH_HOME, no network, no credentials."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from mcp import types
import nth_quartet_proxy as proxy

TOKEN = 'test-capability'
HUB_GUIDANCE = 'Launch a Monitor with nth_spoke_monitor.py'
HUB_FOOTER = ('[server] Stay connected. RESTART YOUR BACKGROUND MONITOR NOW if it is not running. '
              '[server] Monitor heartbeat stale. Spokes: launch nth_spoke_monitor.py.')
PROPERTIES = {key: {'type': 'string'} for key in ('channel', 'member_id', 'session_token')}


class FakeHub:
    """Stands in for MCPSSEClient. Like the real hub, it returns a string body both as
    a text block and as structuredContent.result."""
    instances = []

    def __init__(self, url):
        self.calls = []
        self.connects = 0
        FakeHub.instances.append(self)

    def connect(self):
        self.connects += 1

    def close(self):
        pass

    def force_reconnect(self):
        pass

    def call_tool(self, name, arguments, timeout=60):
        if not self.connects:
            raise RuntimeError('Not connected (no SSE endpoint)')
        return {'event': 'no_new', 'messages': []}

    def call(self, method, params=None, timeout=60):
        # Strict like the real client: a frontend that forgets to connect stalls there.
        if not self.connects:
            raise RuntimeError('Not connected (no SSE endpoint)')
        self.calls.append((method, params))
        if method == 'tools/list':
            return {'tools': [
                {'name': 'quartet_connect', 'inputSchema': {'type': 'object', 'properties': {
                    'summary': {'type': 'string'}, 'name': {'type': 'string'}, 'channel': {'type': 'string'}}}},
                {'name': 'quartet_ack', 'inputSchema': {'type': 'object', 'properties': dict(
                    PROPERTIES, through_id={'type': 'integer'})}},
                {'name': 'quartet_roster', 'inputSchema': {'type': 'object', 'properties': {
                    'channel': {'type': 'string'}, 'member_id': {'type': 'string'}}}}]}
        name = params['name']
        if name == 'quartet_connect':
            body = {'ok': True, 'channel': 'room', 'member_id': 'member-1', 'session_token': TOKEN,
                    'monitor_hint': 'python3 nth_spoke_monitor.py room member-1',
                    'instructions': HUB_GUIDANCE}
        elif name == 'quartet_roster':
            body = {'ok': True, 'members': []}                       # nothing for a frontend to adapt
        else:
            body = {'ok': True, 'echo': params['arguments'], 'footer': HUB_FOOTER,
                    'messages': [{'id': 1, 'from': 'peer', 'content': HUB_FOOTER}]}
        text = json.dumps(body)
        return {'content': [{'type': 'text', 'text': text}], 'structuredContent': {'result': text},
                'isError': False}


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FakeHub.instances.clear()
        self.hubs = []

    def tearDown(self):
        for hub in self.hubs:
            hub.stop_all()
        try:
            self.temp.cleanup()
        except OSError:
            pass

    def serve(self, **environment):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('TRIO_', 'NTH_'))}
        env.update(NTH_HOME=self.temp.name, **environment)
        with patch.dict(os.environ, env, clear=True), \
             patch.object(proxy, 'MCPSSEClient', FakeHub), \
             patch('nth_spoke_monitor.MCPSSEClient', FakeHub):
            server, _, hub = proxy.create_server('http://hub.example/sse')
        if hub is not None:
            self.hubs.append(hub)
        self.environment = env
        return server

    def call(self, server, tool_name, /, **arguments):   # positional-only: quartet_connect takes name=
        request = types.CallToolRequest(method='tools/call',
                                        params=types.CallToolRequestParams(name=tool_name, arguments=arguments))
        with patch.dict(os.environ, self.environment, clear=True), \
             patch('nth_spoke_monitor.MCPSSEClient', FakeHub):
            return asyncio.run(server.request_handlers[types.CallToolRequest](request)).root

    def test_connect_rewrites_the_structured_result_as_well_as_the_text(self):
        for environment, mode in (({'TRIO_NATIVE_CLIENT': 'claude', 'TRIO_CLAUDE_CHANNEL': '1'}, 'channel'),
                                  ({'TRIO_NATIVE_CLIENT': 'codex'}, 'manual_attach')):
            with self.subTest(mode=mode):
                result = self.call(self.serve(**environment), 'quartet_connect',
                                   summary='test', name='member', channel='room')
                text = result.content[0].text
                # Whichever form a host shows the model, it is the same rewritten body.
                self.assertEqual(result.structuredContent, {'result': text})
                body = json.loads(text)
                self.assertEqual((body['event_delivery']['mode'], body['event_delivery']['readiness']),
                                 (mode, 'unverified'))
                self.assertEqual(body['monitor_hint'], '')
                self.assertNotIn(HUB_GUIDANCE, json.dumps(result.structuredContent))
                self.assertNotIn(TOKEN, json.dumps(body['event_delivery']))

    def test_channel_mode_supplies_the_held_token_only_where_the_hub_tool_takes_one(self):
        server = self.serve(TRIO_NATIVE_CLIENT='claude', TRIO_CLAUDE_CHANNEL='1')
        self.call(server, 'quartet_connect', summary='test', name='member', channel='room')
        remote = FakeHub.instances[0]
        self.call(server, 'quartet_ack', channel='room', member_id='member-1', through_id=4)
        self.assertEqual(remote.calls[-1][1]['arguments']['session_token'], TOKEN)
        self.call(server, 'quartet_roster', channel='room', member_id='member-1')
        self.assertNotIn('session_token', remote.calls[-1][1]['arguments'])
        self.call(server, 'quartet_ack', channel='room', member_id='someone-else', through_id=4)
        self.assertNotIn('session_token', remote.calls[-1][1]['arguments'])
        self.assertEqual(remote.connects, 1)      # the tool-call connection is opened once, and is opened

    def test_hub_monitor_footers_are_adapted_in_both_forms_and_peer_content_is_not(self):
        for environment in ({'TRIO_NATIVE_CLIENT': 'claude', 'TRIO_CLAUDE_CHANNEL': '1'},
                            {'TRIO_NATIVE_CLIENT': 'codex'}):
            with self.subTest(environment=environment):
                FakeHub.instances.clear()
                result = self.call(self.serve(**environment), 'quartet_ack',
                                   channel='room', member_id='member-1', through_id=1)
                self.assertEqual(result.structuredContent, {'result': result.content[0].text})
                body = json.loads(result.content[0].text)
                self.assertNotIn('MONITOR NOW', body['footer'])
                self.assertNotIn('heartbeat stale', body['footer'])
                self.assertIn('quartet_delivery_status', body['footer'])
                self.assertEqual(body['messages'][0]['content'], HUB_FOOTER)
        # Nothing to adapt: the hub's own bytes pass through untouched.
        result = self.call(self.serve(TRIO_NATIVE_CLIENT='codex'), 'quartet_roster', channel='room', member_id='m')
        self.assertEqual(result.content[0].text, json.dumps({'ok': True, 'members': []}))

    def test_without_channel_mode_calls_pass_through_unchanged(self):
        server = self.serve(TRIO_NATIVE_CLIENT='claude')
        self.call(server, 'quartet_connect', summary='test', name='member', channel='room')
        result = self.call(server, 'quartet_ack', channel='room', member_id='member-1', through_id=4)
        self.assertNotIn('session_token', FakeHub.instances[0].calls[-1][1]['arguments'])
        # A Claude session that does run a Monitor keeps the hub's guidance.
        self.assertEqual(json.loads(result.content[0].text)['footer'], HUB_FOOTER)


if __name__ == '__main__':
    unittest.main()
