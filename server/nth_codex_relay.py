#!/usr/bin/env python3
"""Durable Quartet -> existing stock Codex thread event relay.

One explicit binding per process. Neither starts Codex nor creates a thread.
Reattaches to the bound thread with no configuration overrides. Quartet read
watermarks remain owned by the agent, as with the Monitor.
Accepted means app-server accepted the event, not that the model processed it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading

from nth_codex_socket import CodexSocketClient
from nth_spoke_monitor import MCPSSEClient
from nth_event_sources import create_source


class UncertainDelivery(RuntimeError):
    pass


class MembershipEnded(RuntimeError):
    pass


class Spool:
    def __init__(self, path, binding):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(path) + '.lock', 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock.seek(0)
                self.lock.write(b'0')
                self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('Another relay owns this spool') from None
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS binding (fingerprint TEXT PRIMARY KEY)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS events (
            message_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', turn_id TEXT)''')
        identity = {k: binding[k] for k in ('endpoint', 'thread_id', 'url', 'channel', 'member_id')}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        legacy = hashlib.sha256(json.dumps(dict(identity, filter=binding['filter']), sort_keys=True).encode()).hexdigest()
        previous = self.db.execute('SELECT fingerprint FROM binding').fetchone()
        if previous and previous[0] not in (fingerprint, legacy):
            self.close()
            raise ValueError('Spool belongs to a different binding; use a new spool')
        if previous and previous[0] == legacy:
            self.db.execute('UPDATE binding SET fingerprint=?', (fingerprint,))
        self.db.execute('INSERT OR IGNORE INTO binding VALUES (?)', (fingerprint,))
        self.db.commit()

    def stage(self, messages, channel):
        with self.db:
            for message in messages:
                mid = message['id']
                if not isinstance(mid, int) or mid <= 0:
                    raise ValueError('Quartet message requires a positive integer id')
                payload = json.dumps({'event': 'new_messages', 'channel': channel,
                    'event_id': f'{channel}:{mid}', 'messages': [message]}, separators=(',', ':'))
                self.db.execute('INSERT OR IGNORE INTO events(message_id,payload) VALUES (?,?)', (mid, payload))

    def deliver(self, client, thread_id, *, tool_name='quartet_event', cancelled=None):
        if self.db.execute("SELECT 1 FROM events WHERE state='sending' LIMIT 1").fetchone():
            raise UncertainDelivery('Previous delivery has no receipt; reconcile the thread before retrying')
        delivered = []
        for mid, payload in self.db.execute("SELECT message_id,payload FROM events WHERE state='pending' ORDER BY message_id").fetchall():
            if cancelled and cancelled():
                break
            # Commit before touching the wire: an interrupted send is ambiguous,
            # never silently replayed and never reported as successfully read.
            with self.db:
                self.db.execute("UPDATE events SET state='sending' WHERE message_id=?", (mid,))
            try:
                response = client.request('turn/start', {'threadId': thread_id, 'input': [],
                    'toolOutput': {'name': tool_name, 'namespace': None, 'output': payload}})
                turn_id = response['turn']['id']
            except Exception as exc:
                raise UncertainDelivery('Event acceptance is unconfirmed; retained for reconciliation') from exc
            with self.db:
                self.db.execute("UPDATE events SET state='accepted',turn_id=? WHERE message_id=?", (turn_id, mid))
            delivered.append({'message_id': mid, 'turn_id': turn_id, 'state': 'accepted'})
        return delivered

    def close(self):
        self.db.close()
        self.lock.close()


def load_binding(path):
    path = Path(path)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Binding contains a session token; chmod 600 the binding file')
    binding = json.loads(path.read_text(encoding='utf-8'))
    for key in ('endpoint', 'thread_id', 'url', 'channel', 'member_id', 'session_token'):
        if not isinstance(binding.get(key), str) or not binding[key]:
            raise ValueError('Binding requires ' + key)
    binding.setdefault('filter', 'at')
    if binding['filter'] not in ('all', 'about', 'at'):
        raise ValueError('Relay filter must be all, about or at')
    return binding


def select_messages(poll, filter_mode):
    # Filter the newly returned message itself, not stale batch-level flags.
    # Fetch all visible messages so @someone-else plus !me cannot be filtered
    # out by the hub's mentions_only shortcut before its bang reaches us.
    return [message for message in poll.get('messages', [])
            if filter_mode == 'all' or message.get('banged')
            or message.get('mentioned')
            or (filter_mode == 'about' and message.get('referenced'))]


def run(binding, spool_path, *, once=False, stop_event=None, on_status=None,
        on_receipt=None, cancelled=None):
    stop_event = stop_event or threading.Event()
    is_cancelled = lambda: stop_event.is_set() or bool(cancelled and cancelled())
    spool = Spool(spool_path, binding)
    codex = CodexSocketClient(binding['endpoint'])
    quartet = (create_source(binding) if binding.get('source') == 'local'
               else MCPSSEClient(binding['url']))
    try:
        codex.start()
        # Verify existence before subscribing. A missing thread must fail rather
        # than create a fork. Resume on this same server attaches to a loaded
        # thread, or loads this exact thread after its last subscriber left.
        codex.request('thread/read', {'threadId': binding['thread_id'], 'includeTurns': False})
        resumed = codex.request('thread/resume', {'threadId': binding['thread_id']})
        if resumed['thread']['id'] != binding['thread_id']:
            raise RuntimeError('Codex returned a different thread identity')
        quartet.connect()
        if on_status:
            on_status('listening')
        while not is_cancelled():
            poll = quartet.call_tool('quartet_poll', {
                'channel': binding['channel'], 'member_id': binding['member_id'],
                'session_token': binding['session_token'], 'auto_ack': False,
                'wait_seconds': 0 if once else 15,
                'mentions_only': False,
                'monitor_heartbeat': True, 'monitor_filter': binding['filter'],
            }, timeout=45)
            # Check membership on every pass, including before draining a spool.
            # Never auto-reclaim a revoked session or leak tokens through errors.
            if not isinstance(poll, dict) or poll.get('error'):
                raise MembershipEnded('Channel refused the poll; check membership and session binding')
            if poll.get('ended') or poll.get('event') in ('ended', 'channel_not_found', 'channel_gone'):
                raise MembershipEnded('Channel ended or disappeared')
            if is_cancelled():
                return
            messages = select_messages(poll, binding['filter'])
            spool.stage(messages, binding['channel'])
            for receipt in spool.deliver(codex, binding['thread_id'],
                    tool_name='trio_event' if binding.get('source') == 'local' else 'quartet_event',
                    cancelled=is_cancelled):
                if on_receipt:
                    on_receipt(receipt)
                else:
                    print(json.dumps(receipt), flush=True)
            if once:
                return
            # A non-acking poll can return old data immediately. Avoid a hot loop
            # without altering the agent's server-side read watermark.
            stop_event.wait(1)
    finally:
        quartet.close()
        codex.stop()
        spool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True, help='Private JSON binding file')
    parser.add_argument('--spool', required=True, help='Durable SQLite delivery ledger')
    parser.add_argument('--once', action='store_true', help='Poll and drain once')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(load_binding(args.binding), args.spool, once=args.once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Do not dump upstream exception details: they can contain credentials.
        print('Relay stopped: ' + type(exc).__name__ + '. Check binding and delivery ledger.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
