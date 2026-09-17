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
                              delivery_status, listen, uses_monitor)
from nth_claude_channel import (ChannelHub, call_succeeded, channel_mode,
                                quartet_poll_factory, run_stdio)

LOCAL_TOOLS = ('quartet_delivery_status', 'quartet_listen')
# A hub that never stops paging must not hang the tool listing.
MAX_TOOL_PAGES = 50


def _joined(body, url, hub):
    """A successful connect, shaped for this client, with its listener started."""
    body = native_connect_response(body, source='quartet', url=url, channel=hub is not None)
    if hub is not None and not body.get('error') and all(
            body.get(k) for k in ('channel', 'member_id', 'session_token')):
        # The same successful connect result Trio binds a Codex thread from.
        # A listener failure must not fail the join.
        try:
            body['event_delivery']['listener'] = hub.start(
                body['channel'], body['member_id'], body['session_token'])
        except Exception as exc:  # noqa: BLE001
            body['event_delivery']['listener'] = {'status': 'failed', 'error': type(exc).__name__}
    adapt_response_guidance(body, 'quartet')
    return True


def _guided(body):
    # The hub writes its footers for Claude's Monitor and cannot know who asks.
    return adapt_response_guidance(body, 'quartet')


def _adapt(response, transform):
    """Apply `transform(body) -> changed` to the response's JSON body, in every form
    the hub returned it. Never raises: adapting is a courtesy, and whatever the hub
    sent must still reach the agent if this code does not understand it."""
    try:
        content = response.get('content')
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block.get('text')
            if not isinstance(text, str):
                continue
            try:
                body = json.loads(text)
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            if not transform(body):
                continue                    # nothing to adapt here: the hub's own bytes stay
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
    except Exception:  # noqa: BLE001
        return


def create_server(url):
    server = Server('nth-qweb')
    client = MCPSSEClient(url)
    # Claude channel mode: this process owns the only path into the session, so
    # its listeners live here. Codex keeps using the central event service.
    hub = ChannelHub('quartet', 'quartet', url, quartet_poll_factory) if channel_mode() else None
    # All mutable state lives in this one dict. Bare flags in this scope were
    # once shadowed by a helper of the same name, which disabled the connection.
    state = {'started': False, 'listed': False, 'accepts_token': set()}
    lock = asyncio.Lock()

    async def remote(method, params=None):
        async with lock:
            if not state['started']:
                # Once, ever: connect() starts a reader thread on each call, and
                # the client reconnects by itself after a failure.
                state['started'] = True
                await asyncio.to_thread(client.connect)
        return await asyncio.to_thread(client.call, method, params, 60)

    local_schema = {'type': 'object', 'properties': {
        'channel': {'type': 'string'}, 'member_id': {'type': 'string'},
        'session_token': {'type': 'string'}}, 'required': ['channel', 'member_id', 'session_token']}

    @server.list_tools()
    async def list_tools():
        listed, cursor = [], None
        for _ in range(MAX_TOOL_PAGES):
            response = await remote('tools/list', {'cursor': cursor} if cursor else {})
            tools = [t for t in (response.get('tools') or []) if isinstance(t, dict) and t.get('name')]
            state['accepts_token'].update(
                t['name'] for t in tools
                if 'session_token' in ((t.get('inputSchema') or {}).get('properties') or {}))
            listed.extend(types.Tool.model_validate(t) for t in tools if t['name'] not in LOCAL_TOOLS)
            cursor = response.get('nextCursor')
            if not cursor:
                break
        state['listed'] = True
        listed.append(types.Tool(name='quartet_delivery_status', description='Check this session\'s event delivery: the Codex listener, or the Claude channel listener. Only ready=true means reachable.', inputSchema=local_schema))
        schema = json.loads(json.dumps(local_schema))
        schema['properties'].update(filter_mode={'type': 'string', 'enum': ['all', 'about', 'at']}, enabled={'type': 'boolean'})
        listed.append(types.Tool(name='quartet_listen', description='Start, change or stop this session\'s event listener. An omitted filter_mode or enabled leaves that setting as it is. enabled=true restarts it from these credentials after a session restart.', inputSchema=schema))
        return listed

    def host_info():
        try:
            info = server.request_context.session.client_params.clientInfo
            return {'name': info.name, 'version': info.version}
        except Exception:  # noqa: BLE001 - optional detail; status must not depend on it
            return None

    @server.call_tool()
    async def call_tool(name, arguments):
        if name in LOCAL_TOOLS:
            options = {'hub': hub}
            if name == 'quartet_delivery_status' and hub is not None:
                options['host'] = host_info()
            callback = delivery_status if name == 'quartet_delivery_status' else listen
            result = await asyncio.to_thread(callback, **arguments, **options)
            return [types.TextContent(type='text', text=json.dumps(result))]
        if not name.startswith('quartet_'):
            raise ValueError('Expected a Quartet tool')
        is_ack = name.endswith('_ack')
        if hub is not None and is_ack:
            # Which remote tools take a session token is learned from the hub's
            # tool list. The mcp library happens to refresh that list before each
            # call, but that is its private behaviour: ask for it explicitly.
            if not state['listed']:
                await list_tools()
            # See ChannelHub.complete: a woken model omits the token this frontend holds.
            arguments = hub.complete(name, arguments, name in state['accepts_token'])
        response = await remote('tools/call', {'name': name, 'arguments': arguments})
        if hub is not None and is_ack:
            hub.observe(name, arguments, call_succeeded(response))
        if isinstance(response, dict) and not response.get('isError'):
            if name == 'quartet_connect':
                _adapt(response, lambda body: _joined(body, url, hub))
            elif not uses_monitor():
                # Only sessions without a Monitor need the hub's footers adapted;
                # for the rest the response is not even parsed.
                _adapt(response, _guided)
        return types.CallToolResult.model_validate(response)

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
