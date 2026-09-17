#!/usr/bin/env python3
"""Local MCP frontend for Quartet: native stdio clients, existing remote hub.

Preserves remote tool schemas, results, and image blocks. Only local listener
controls and provider-specific connect guidance are added. No hub deployment.
"""
import argparse
import asyncio
import json
import os

from mcp.server import Server
from mcp import types
from nth_spoke_monitor import MCPSSEClient
from nth_event_access import (adapt_response_guidance, native_connect_response,
                              delivery_status, listen)
from nth_claude_channel import (ChannelHub, call_succeeded, channel_mode,
                                quartet_poll_factory, run_stdio)


def create_server(url):
    server = Server('nth-qweb')
    client = MCPSSEClient(url)
    # Claude channel mode: this process owns the only path into the session, so
    # its listeners live here. Codex keeps using the central event service.
    hub = ChannelHub('quartet', 'quartet', url, quartet_poll_factory) if channel_mode() else None
    connected = False
    lock = asyncio.Lock()

    async def remote(method, params=None):
        nonlocal connected
        async with lock:
            if not connected:
                await asyncio.to_thread(client.connect)
                connected = True
        return await asyncio.to_thread(client.call, method, params, 60)

    accepts_token = set()      # remote tools whose schema has a session_token

    local_schema = {'type': 'object', 'properties': {
        'channel': {'type': 'string'}, 'member_id': {'type': 'string'},
        'session_token': {'type': 'string'}}, 'required': ['channel', 'member_id', 'session_token']}

    @server.list_tools()
    async def list_tools():
        listed, cursor = [], None
        while True:
            response = await remote('tools/list', {'cursor': cursor} if cursor else {})
            accepts_token.update(t['name'] for t in response.get('tools', [])
                                 if 'session_token' in (t.get('inputSchema') or {}).get('properties', {}))
            listed.extend(types.Tool.model_validate(t) for t in response.get('tools', [])
                          if t['name'] not in ('quartet_delivery_status', 'quartet_listen'))
            cursor = response.get('nextCursor')
            if not cursor:
                break
        listed.append(types.Tool(name='quartet_delivery_status', description='Check this session\'s event delivery: the Codex listener, or the Claude channel listener.', inputSchema=local_schema))
        schema = json.loads(json.dumps(local_schema))
        schema['properties'].update(filter_mode={'type': 'string', 'enum': ['all', 'about', 'at']}, enabled={'type': 'boolean'})
        listed.append(types.Tool(name='quartet_listen', description='Start, change or stop this session\'s event listener. An omitted filter_mode or enabled leaves that setting as it is. enabled=true restarts it from these credentials after a session restart.', inputSchema=schema))
        return listed

    @server.call_tool()
    async def call_tool(name, arguments):
        if name in ('quartet_delivery_status', 'quartet_listen'):
            callback = delivery_status if name.endswith('delivery_status') else listen
            result = await asyncio.to_thread(callback, **arguments, hub=hub)
            return [types.TextContent(type='text', text=json.dumps(result))]
        if not name.startswith('quartet_'):
            raise ValueError('Expected a Quartet tool')
        if hub is not None:
            # See ChannelHub.complete: a woken model omits the token this frontend holds.
            arguments = hub.complete(name, arguments, name in accepts_token)
        response = await remote('tools/call', {'name': name, 'arguments': arguments})
        if hub is not None:
            hub.observe(name, arguments, call_succeeded(response))
        if not response.get('isError'):
            adapt(response, joined if name == 'quartet_connect' else guided)
        return types.CallToolResult.model_validate(response)

    def joined(body):
        body = native_connect_response(body, source='quartet', url=url)
        if hub is not None and not body.get('error') and all(
                body.get(k) for k in ('channel', 'member_id', 'session_token')):
            # The same successful connect result Trio binds a Codex thread from.
            # A listener failure must not fail the join.
            try:
                body['event_delivery']['listener'] = hub.start(
                    body['channel'], body['member_id'], body['session_token'])
            except Exception as exc:  # noqa: BLE001
                body['event_delivery']['listener'] = {'status': 'failed', 'error': type(exc).__name__}
        return guided(body)

    def guided(body):
        # The hub writes its footers for Claude's Monitor and cannot know who asks.
        return adapt_response_guidance(body, 'quartet')

    def adapt(response, transform):
        """Transform the response's JSON body, in every form the hub returned it."""
        for block in response.get('content', []):
            if block.get('type') != 'text':
                continue
            try:
                body = json.loads(block['text'])
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            before = json.dumps(body)
            body = transform(body)
            if json.dumps(body) == before:
                return                      # nothing to adapt: pass the hub's bytes through
            block['text'] = json.dumps(body)
            # The hub also returns the body as structured content, and a host may
            # show the model that form. Left as it was, it would carry the hub's
            # own monitor guidance past this rewrite.
            structured = response.get('structuredContent')
            if isinstance(structured, dict):
                if isinstance(structured.get('result'), str):
                    structured['result'] = block['text']
                else:
                    response['structuredContent'] = body
            return

    return server, client, hub


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('NTH_QUARTET_URL', ''))
    args = parser.parse_args()
    if not args.url:
        parser.error('--url is required')
    os.umask(0o077)
    server, client, hub = create_server(args.url)
    try:
        await run_stdio(server, hub)
    finally:
        client.close()


if __name__ == '__main__':
    asyncio.run(main())
