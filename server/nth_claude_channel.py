#!/usr/bin/env python3
"""Claude Code channel delivery for the Trio and Quartet stdio frontends.

Codex has a socket Trio can dial, so its relay lives in the central event
service. Claude's only injection path is the stdio pipe of the MCP server
process Claude itself spawned, so Claude's listener runs inside that frontend
and "deliver" means writing `notifications/claude/channel` to this process's own
write stream. The host injects the event into the open session: it wakes an idle
session, and during a turn it lands at the next model-step boundary.

A channel notification has no receipt. Status therefore says written, never
accepted; the agent's own ack is the only confirmation that it was read.

No model turn happens on a timer. The listener long-polls in the background,
which costs no tokens, and nothing reaches the session unless a message passes
the membership's filter: there is no idle wake-up to lease or re-arm.

Every notification makes the host run a model turn over the session's whole
context, and any channel peer can cause one. The listener therefore writes at
most one notification per poll, caps what one carries, and rate-limits them.
"""
import asyncio
import concurrent.futures
import json
import os
import secrets
import threading

from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

# The listener, its event format and its poll factory moved to nth_listener so the
# hook processes can use them without the mcp SDK. They are re-exported here for the
# frontends that construct a ChannelHub with channel.quartet_poll_factory.
from nth_listener import (FILTERS, UNCONFIRMED, Listener, attribute, format_event,  # noqa: F401
                          format_notice, quartet_poll_factory, select_messages)

CAPABILITY = 'claude/channel'
METHOD = 'notifications/claude/channel'
# Claude Code releases this path was run against end to end. Channels are a
# research preview: status names a host outside this list so that a change in
# behaviour after an update points at the right component.
CONFIRMED_HOST_VERSIONS = ('2.1.274',)
# How often a push waiting on a busy event loop looks for a stop.
PUSH_WAIT_SLICE_SECONDS = .2
# How long a replaced or stopped listener gets to finish a write and close its connection.
REPLACE_JOIN_SECONDS = 1.0


def channel_mode():
    """True when this frontend serves Claude AND the session was launched for channels.

    The host declares nothing channel-related in its initialize request, so the
    launcher (`trio claude`) states it through the environment, as `trio codex`
    does with TRIO_CODEX_ENDPOINT.
    """
    return (os.environ.get('TRIO_NATIVE_CLIENT') == 'claude'
            and os.environ.get('TRIO_CLAUDE_CHANNEL') == '1')


def _same(held, offered):
    """Constant-time comparison of two session tokens."""
    return (isinstance(held, str) and isinstance(offered, str)
            and secrets.compare_digest(held.encode(), offered.encode()))


class ChannelHub:
    """Owns this frontend's write stream and its per-membership listeners."""

    def __init__(self, prefix, source, url, poll_factory):
        self.prefix = prefix                  # 'trio' | 'quartet'
        self.source = source                  # 'local' | 'quartet'
        self.url = url
        self.poll_factory = poll_factory      # callable(binding) -> (poll, close or None)
        self.loop = None
        self.writer = None
        self.listeners = {}
        self.lock = threading.Lock()

    def attach(self, loop, writer):
        self.loop, self.writer = loop, writer

    def push(self, content, meta, cancelled=None):
        """Write one notification. True once it is on the transport.

        The local frontend's tools are synchronous, so an agent's own long poll can
        hold the event loop for many seconds. The send stays scheduled regardless,
        so a slow loop is waited out, never treated as a failure: giving up early
        would drop a message the listener is about to mark as seen.
        """
        if self.writer is None or self.loop is None or (cancelled and cancelled()):
            return False
        note = types.JSONRPCNotification(jsonrpc='2.0', method=METHOD,
                                         params={'content': content, 'meta': meta})

        async def write():
            # Checked here, on the loop thread, at the moment of writing. Cancelling
            # the future from the waiting thread is not enough: asyncio runs a new
            # task's first step before a cancellation scheduled after it, and a
            # stream write completes in that first step.
            if cancelled and cancelled():
                return False
            await self.writer.send(SessionMessage(message=types.JSONRPCMessage(note)))
            return True

        future = asyncio.run_coroutine_threadsafe(write(), self.loop)
        while True:
            try:
                return future.result(timeout=PUSH_WAIT_SLICE_SECONDS)
            except (TimeoutError, concurrent.futures.TimeoutError):
                if self.loop.is_closed():
                    return False
                # Stopped while still queued: stop waiting. write() refuses to send
                # whenever the loop reaches it. A stop landing in the instant a send
                # completes can report an unwritten message that was written; the
                # successor then repeats that one notification. Never a loss.
                if cancelled and cancelled() and future.cancel():
                    return False
            except concurrent.futures.CancelledError:
                return False
            except Exception:  # noqa: BLE001 - closed transport: delivery is over, the process is not
                return False

    def start(self, channel, member_id, session_token, filter_mode='about'):
        """Start or replace this membership's listener.

        Called directly only for a verified successful connect, which may carry a
        rotated token. Agent-supplied credentials go through configure().
        """
        if filter_mode not in FILTERS:
            raise ValueError('Invalid listening filter')
        binding = {'source': self.source, 'url': self.url, 'channel': channel,
                   'member_id': member_id, 'session_token': session_token, 'filter': filter_mode}
        # Built before anything is stopped: if this raises, the membership keeps
        # the listener it had.
        poll, close = self.poll_factory(binding)
        with self.lock:
            listener = Listener(self, binding, poll, close)
            previous = self.listeners.pop((channel, member_id), None)
            if previous:
                previous.stop()
                # A write in flight when the stop landed finishes or is withdrawn
                # within a push slice; waiting for it keeps the marks exact. A local
                # long poll cannot be interrupted, and needs no wait: the loop
                # checks the stop before it writes anything.
                if previous.thread.is_alive():
                    previous.thread.join(REPLACE_JOIN_SECONDS)
                with previous._evidence:
                    # Same membership, same process: what it saw, what it wrote,
                    # its evidence and its rate-limit budget carry over.
                    for field in ('high_water', 'written', 'notifications', 'last_written',
                                  'first_written', 'acked_through', 'confirmed_through',
                                  'tokens', 'refilled'):
                        setattr(listener, field, getattr(previous, field))
            self.listeners[listener.key] = listener
            listener.start()
        return listener.public()

    def _find(self, arguments):
        channel, member_id = arguments.get('channel'), arguments.get('member_id')
        if not isinstance(channel, str) or not isinstance(member_id, str):
            return None
        with self.lock:
            return self.listeners.get((channel, member_id))

    def _owned(self, channel, member_id, session_token):
        with self.lock:
            listener = self.listeners.get((channel, member_id))
        # The token is the capability: a caller without it sees and changes nothing.
        return listener if listener and _same(listener.binding['session_token'], session_token) else None

    def status(self, channel, member_id, session_token):
        listener = self._owned(channel, member_id, session_token)
        return [listener.public()] if listener else []

    def configure(self, channel, member_id, session_token, *, filter_mode=None, enabled=None):
        if filter_mode is not None and filter_mode not in FILTERS:
            raise ValueError('Invalid listening filter')
        with self.lock:
            existing = self.listeners.get((channel, member_id))
        current = existing if existing and _same(existing.binding['session_token'], session_token) else None
        if existing and not current and existing.state != 'ended':
            # Credentials that do not own the listener change nothing: they must
            # never evict or replace a working subscription.
            return []
        if enabled is False:
            if current:
                if filter_mode:
                    current.binding['filter'] = filter_mode
                current.stop()
            return self.status(channel, member_id, session_token)
        live = current is not None and current.state in ('starting', 'listening', 'reconnecting')
        if enabled is None:
            # An omitted `enabled` never starts anything. It changes the filter of
            # a live listener, and for a stopped or an ended one it only remembers
            # the filter: a stop stays a stop, and an ended membership is not
            # revived by a call that may have been made just to look.
            if current is None:
                return []
            if filter_mode and filter_mode != current.binding['filter']:
                if live:
                    return [self.start(channel, member_id, session_token, filter_mode)]
                current.binding['filter'] = filter_mode
            return [current.public()]
        # enabled=True: (re)start from credentials alone. After a session restart
        # this frontend is a new process, and the rule is probe, then listen.
        wanted = filter_mode or (current.binding['filter'] if current else 'about')
        if live and current.binding['filter'] == wanted:
            return [current.public()]
        return [self.start(channel, member_id, session_token, wanted)]

    def complete(self, name, arguments, accepts_token):
        """Supply the held session token for an ack that omits it. Never raises.

        A model woken by an event leaves the token out. A tokenless ack moves only
        the legacy per-member watermark, never the session's, so the acknowledged
        backlog was written again after every restart. Only acks are completed:
        that is the one call whose omission breaks delivery, and completing a poll
        would silently turn off the auto-advance the poll tool documents. Whoever
        can place a call on this pipe can therefore advance this membership's read
        mark without presenting the token; they cannot post or act as the member.
        The token never reaches the model.
        """
        try:
            if (not accepts_token or not isinstance(name, str) or not name.endswith('_ack')
                    or not isinstance(arguments, dict) or arguments.get('session_token')):
                return {} if arguments is None else arguments
            listener = self._find(arguments)
            if listener is None or listener.state == 'ended':
                return arguments
            return dict(arguments, session_token=listener.binding['session_token'])
        except Exception:  # noqa: BLE001 - completion is a courtesy; the call itself must go ahead
            return {} if arguments is None else arguments

    def observe(self, name, arguments, succeeded):
        """Record an ack that passed through this frontend as delivery evidence. Never raises."""
        try:
            if (not succeeded or not isinstance(name, str) or not name.endswith('_ack')
                    or not isinstance(arguments, dict)):
                return
            listener = self._find(arguments)
            through_id = arguments.get('through_id')
            # Another valid session of the same member moves its own watermark, not
            # this one's: only an ack made with this listener's token counts here.
            if (listener is not None and isinstance(through_id, int) and not isinstance(through_id, bool)
                    and _same(listener.binding['session_token'], arguments.get('session_token'))):
                listener.acknowledged(through_id)
        except Exception:  # noqa: BLE001 - evidence must never fail the call it watched
            pass

    def stop_all(self):
        with self.lock:
            listeners = list(self.listeners.values())
        for listener in listeners:
            listener.stop()
        # Let each close its hub connection instead of dying mid-poll with the process.
        for listener in listeners:
            if listener.thread.is_alive():
                listener.thread.join(REPLACE_JOIN_SECONDS)

    # The Quartet frontend loads this module only when a channel was asked for, and
    # then holds nothing of it but this hub: what it needs is reachable from here.
    def call_succeeded(self, result):
        return call_succeeded(result)

    async def run_stdio(self, server):
        await run_stdio(server, self)


def call_succeeded(result):
    """True when a tool result's first JSON text block reports ok. Never raises.

    The local frontend yields content blocks (or a tuple led by them); the Quartet
    frontend yields the remote response dict.
    """
    try:
        if isinstance(result, tuple):
            result = result[0] if result else []
        if isinstance(result, dict):
            if result.get('isError'):
                return False
            result = result.get('content') or []
        for block in result if isinstance(result, (list, tuple)) else [result]:
            text = block.get('text') if isinstance(block, dict) else getattr(block, 'text', None)
            if not isinstance(text, str) or not text:
                continue
            body = json.loads(text)
            if isinstance(body, dict) and isinstance(body.get('result'), str):
                body = json.loads(body['result'])
            return isinstance(body, dict) and bool(body.get('ok'))
    except Exception:  # noqa: BLE001
        pass
    return False


def host_note(host):
    """A sentence when the host is not one this path was confirmed on, else ''."""
    version = str((host or {}).get('version') or '')
    if not version or version in CONFIRMED_HOST_VERSIONS:
        return ''
    return ('Channel delivery was last confirmed on Claude Code ' + ', '.join(CONFIRMED_HOST_VERSIONS)
            + '; this host reports ' + attribute((host or {}).get('name') or 'an unnamed client', 40)
            + ' ' + attribute(version, 40) + '.')


def complete_local_calls(mcp, hub):
    """Route the local frontend's tool calls through the hub (see ChannelHub.complete)."""
    schemas = {}

    async def call_tool(name, arguments):
        if not schemas:
            schemas.update({tool.name: tool.inputSchema for tool in await mcp.list_tools()})
        accepts = 'session_token' in ((schemas.get(name) or {}).get('properties') or {})
        arguments = hub.complete(name, arguments, accepts)
        result = await mcp.call_tool(name, arguments)
        if isinstance(name, str) and name.endswith('_ack'):
            # Only an ack is evidence, so only an ack's result is parsed.
            hub.observe(name, arguments, call_succeeded(result))
        return result

    # Replaces the handler FastMCP registered for itself; same options as FastMCP uses.
    mcp._mcp_server.call_tool(validate_input=False)(call_tool)


async def run_stdio(server, hub=None):
    """FastMCP.run() hides the write stream; both frontends run through here instead."""
    async with stdio_server() as (reader, writer):
        if hub is not None:
            hub.attach(asyncio.get_running_loop(), writer)
        options = server.create_initialization_options(
            experimental_capabilities={CAPABILITY: {}} if hub is not None else {})
        try:
            await server.run(reader, writer, options)
        finally:
            if hub is not None:
                hub.stop_all()
