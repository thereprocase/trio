#!/usr/bin/env python3
"""Claude's native Monitor entry point for a saved Trio/Quartet identity.

--once turns it into a one-shot waiter for a Claude session with neither channel
mode nor the delivery hooks: run it with the Bash tool's run_in_background. It
blocks, costing no model turns, until an event that needs the agent, prints that
one event line and exits 0; the finished background task wakes the session.
Acknowledge the messages first, then start it again: a waiter started before the
ack would wake at once for the same messages.
"""
import argparse
import io
import json
import os
from pathlib import Path
import sys

# Events that need the agent. keepalive only taps the prompt cache and
# filter_mode only reports the filter, so a one-shot waiter keeps waiting
# through them; error lines are transient while the monitor keeps running.
# cadence is left out too: the monitors remember that they fired it only in
# memory, so every relaunch would fire it again at once and loop on wakes.
WAKE_EVENTS = {'new_messages', 'channel_ended', 'channel_gone', 'culled', 'session_revoked',
               'poll_refused'}


class OnceStdout(io.TextIOBase):
    """Stand-in for stdout under --once: pass the first waking event through, then exit."""

    def __init__(self, real, wake_events, exit=os._exit):
        self.real, self.wake_events, self.exit = real, wake_events, exit
        self.pending, self.last_error = '', None

    def writable(self):
        return True

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            self.handle(line)
        return len(text)

    def handle(self, line):
        try:
            event = json.loads(line).get('event')
        except (ValueError, AttributeError):
            return
        if event == 'error':
            self.last_error = line
        if event in self.wake_events:
            self.real.write(line + '\n')
            self.real.flush()
            self.exit(0)         # os._exit: leave the monitor's poll loop and threads at once


def run(identity, filter_mode):
    if identity['source'] == 'local':
        import nth_monitor
        nth_monitor.monitor(identity['channel'], identity['member_id'],
                            filter_mode=filter_mode, _db_path=Path(identity['url']),
                            session_token=identity['session_token'])
    else:
        from nth_spoke_monitor import monitor
        from nth_sse_client import MCPSSEClient
        client = MCPSSEClient(identity['url'])
        try:
            client.connect()
            monitor(client, identity['channel'], identity['member_id'], filter_mode,
                    identity['session_token'], 15, 30)
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--identity', required=True)
    parser.add_argument('--filter', choices=['all', 'about', 'at'], default='about')
    parser.add_argument('--once', action='store_true',
                        help='exit after the first event that needs the agent (for run_in_background)')
    parser.add_argument('--wake-on-keepalive', action='store_true',
                        help='with --once, also wake for the 55-minute keepalive')
    args = parser.parse_args()
    path = Path(args.identity)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Identity file must be private (chmod 600)')
    identity = json.loads(path.read_text(encoding='utf-8'))
    if not args.once:
        run(identity, args.filter)
        return
    real = sys.stdout
    wake = WAKE_EVENTS | ({'keepalive'} if args.wake_on_keepalive else set())
    sys.stdout = once = OnceStdout(real, wake)
    try:
        run(identity, args.filter)
    except SystemExit:
        pass
    finally:
        sys.stdout = real
    # The monitor gave up, as it does after repeated failures: say why and wake the
    # agent so it can probe and relaunch rather than sit unreachable.
    real.write((once.last_error or json.dumps({'event': 'error', 'msg': 'monitor exited'})) + '\n')
    real.flush()
    sys.exit(1)


if __name__ == '__main__':
    main()
