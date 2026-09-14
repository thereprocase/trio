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
from mcp.server.stdio import stdio_server
from mcp import types
from nth_spoke_monitor import MCPSSEClient
from nth_event_access import native_connect_response, delivery_status, listen


def create_server(url):
    server = Server('nth-qweb')
    client = MCPSSEClient(url)
    connected = False
    lock = asyncio.Lock()

    async def remote(method, params=None):
        nonlocal connected
        async with lock:
            if not connected:
                await asyncio.to_thread(client.connect)
                connected = True
        return await asyncio.to_thread(client.call, method, params, 60)

    local_schema = {'type': 'object', 'properties': {
        'channel': {'type': 'string'}, 'member_id': {'type': 'string'},
        'session_token': {'type': 'string'}}, 'required': ['channel', 'member_id', 'session_token']}

    @server.list_tools()
    async def list_tools():
        listed, cursor = [], None
        while True:
            response = await remote('tools/list', {'cursor': cursor} if cursor else {})
            listed.extend(types.Tool.model_validate(t) for t in response.get('tools', [])
                          if t['name'] not in ('quartet_delivery_status', 'quartet_listen'))
            cursor = response.get('nextCursor')
            if not cursor:
                break
        listed.append(types.Tool(name='quartet_delivery_status', description='Check this session\'s local Codex event delivery.', inputSchema=local_schema))
        schema = json.loads(json.dumps(local_schema))
        schema['properties'].update(filter_mode={'type': 'string', 'enum': ['all', 'about', 'at']}, enabled={'type': 'boolean'})
        listed.append(types.Tool(name='quartet_listen', description='Change or stop this session\'s local Codex listener.', inputSchema=schema))
        return listed

    @server.call_tool()
    async def call_tool(name, arguments):
        if name in ('quartet_delivery_status', 'quartet_listen'):
            callback = delivery_status if name.endswith('delivery_status') else listen
            result = await asyncio.to_thread(callback, **arguments)
            return [types.TextContent(type='text', text=json.dumps(result))]
        if not name.startswith('quartet_'):
            raise ValueError('Expected a Quartet tool')
        response = await remote('tools/call', {'name': name, 'arguments': arguments})
        if name == 'quartet_connect' and not response.get('isError'):
            for block in response.get('content', []):
                if block.get('type') == 'text':
                    try:
                        body = json.loads(block['text'])
                    except ValueError:
                        continue
                    if isinstance(body, dict):
                        block['text'] = json.dumps(native_connect_response(body, source='quartet', url=url))
        return types.CallToolResult.model_validate(response)

    return server, client


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('NTH_QUARTET_URL', ''))
    args = parser.parse_args()
    if not args.url:
        parser.error('--url is required')
    os.umask(0o077)
    server, client = create_server(args.url)
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        client.close()


if __name__ == '__main__':
    asyncio.run(main())
