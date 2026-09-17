"""One local Trio event service, multiple durable channel/thread bindings.

The registry is local IPC: no unauthenticated HTTP control port. The service
observes only explicitly registered Codex endpoints, binds successful Trio and
Quartet connect tool results to their actual thread, and supervises listeners.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from nth_codex_relay import run, MembershipEnded, Spool, UncertainDelivery
from nth_codex_socket import CodexSocketClient


def home():
    return Path(os.environ.get('NTH_HOME', str(Path.home() / '.claude' / 'nth')))


def state_dir():
    path = home() / 'events'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _open_database():
    db = sqlite3.connect(state_dir() / 'registry.sqlite', timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=15000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS bindings (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'starting',
            error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, last_message INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS endpoints (
            endpoint TEXT PRIMARY KEY, quartet_url TEXT NOT NULL DEFAULT '', enabled INTEGER DEFAULT 1);
    ''')
    return db


@contextmanager
def database():
    db = _open_database()
    try:
        with db:
            yield db
    finally:
        db.close()


def set_status(binding_id, status, error='', last_message=None):
    with database() as db:
        db.execute('UPDATE bindings SET status=?,error=?,updated=? WHERE id=?',
                   (status, error, time.time(), binding_id))
        if last_message is not None:
            db.execute('UPDATE bindings SET last_message=? WHERE id=?', (last_message, binding_id))


def register(binding, *, replace=False):
    binding = dict(binding)
    binding.setdefault('source', 'quartet')
    binding.setdefault('filter', 'about')
    for field in ('endpoint', 'thread_id', 'url', 'channel', 'member_id', 'session_token'):
        if not isinstance(binding.get(field), str) or not binding[field]:
            raise ValueError('Binding requires ' + field)
    if binding['filter'] not in ('all', 'about', 'at'):
        raise ValueError('Invalid listening filter')
    if binding['source'] not in ('local', 'quartet'):
        raise ValueError('Invalid channel source')
    key = [binding[k] for k in ('endpoint', 'thread_id', 'source', 'url', 'channel', 'member_id')]
    binding_id = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
    encoded = json.dumps(binding, sort_keys=True)
    with database() as db:
        old = db.execute('SELECT config,enabled FROM bindings WHERE id=?', (binding_id,)).fetchone()
        if old:
            previous = json.loads(old['config'])
            if not replace and previous['session_token'] == binding['session_token']:
                # Recovered connect history must not re-enable a stopped
                # subscription or reset the user's current listening filter.
                return binding_id
            if old['config'] == encoded and old['enabled']:
                return binding_id
        db.execute('''INSERT INTO bindings(id,config,updated) VALUES (?,?,?)
            ON CONFLICT(id) DO UPDATE SET config=excluded.config,
            revision=bindings.revision+1,enabled=1,status='starting',error='',updated=excluded.updated''',
            (binding_id, encoded, time.time()))
    return binding_id


def bindings():
    with database() as db:
        return [dict(row) for row in db.execute('SELECT * FROM bindings ORDER BY id')]


def public_status(channel=None, member_id=None, session_token=None):
    result = []
    for row in bindings():
        b = json.loads(row['config'])
        if channel is not None and b['channel'] != channel:
            continue
        if member_id is not None and b['member_id'] != member_id:
            continue
        if session_token is not None and b['session_token'] != session_token:
            continue
        result.append({k: row[k] for k in ('id', 'enabled', 'status', 'error', 'last_message')}
                      | {k: b[k] for k in ('source', 'channel', 'member_id', 'thread_id', 'filter')})
    return result


def configure_listener(channel, member_id, session_token, *, filter_mode=None, enabled=None):
    matched = public_status(channel, member_id, session_token)
    for entry in matched:
        with database() as db:
            row = db.execute('SELECT config FROM bindings WHERE id=?', (entry['id'],)).fetchone()
            b = json.loads(row['config'])
            if filter_mode:
                if filter_mode not in ('all', 'about', 'at'):
                    raise ValueError('Invalid listening filter')
                b['filter'] = filter_mode
            db.execute('''UPDATE bindings SET config=?,enabled=?,revision=revision+1,
                status=?,updated=? WHERE id=?''', (json.dumps(b, sort_keys=True),
                entry['enabled'] if enabled is None else int(enabled),
                'stopping' if enabled is False else 'starting', time.time(), entry['id']))
    return public_status(channel, member_id, session_token)


def add_endpoint(endpoint, quartet_url=''):
    # Validate locally before persisting. Connecting happens in the observer.
    from urllib.parse import urlsplit
    url = urlsplit(endpoint)
    if not (endpoint.startswith('unix:///') or
            (url.scheme in ('ws', 'wss') and url.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ValueError('Codex endpoint must be a private local socket or loopback WebSocket')
    with database() as db:
        db.execute('''INSERT INTO endpoints(endpoint,quartet_url) VALUES (?,?)
            ON CONFLICT(endpoint) DO UPDATE SET quartet_url=excluded.quartet_url,enabled=1''',
            (endpoint, quartet_url))


def connected_identity(message):
    """Accept only real successful MCP connect completions, never peer text."""
    if message.get('method') != 'item/completed':
        return None
    params = message.get('params') or {}
    item = params.get('item') or {}
    if item.get('type') != 'mcpToolCall' or item.get('status') != 'completed':
        return None
    pair = (item.get('server'), item.get('tool'))
    source = {('nth-trio', 'trio_connect'): 'local',
              ('nth-qweb', 'quartet_connect'): 'quartet'}.get(pair)
    if not source or not params.get('threadId'):
        return None
    result = item.get('result') or {}
    if result.get('isError'):
        return None
    for block in result.get('content', []):
        if block.get('type') != 'text':
            continue
        try:
            body = json.loads(block['text'])
        except (ValueError, KeyError):
            continue
        if isinstance(body, dict) and not body.get('error') and all(body.get(k) for k in ('channel', 'member_id', 'session_token')):
            return source, params['threadId'], body
    return None


class Observer:
    def __init__(self, endpoint, quartet_url, stop):
        self.endpoint, self.quartet_url, self.stop = endpoint, quartet_url, stop
        self._history_lock = threading.RLock()
        self._live_joins = set()

    def notification(self, message, *, recovered=False):
        identity = connected_identity(message)
        if not identity:
            return
        source, thread_id, response = identity
        if source == 'quartet' and not self.quartet_url:
            return
        # Endpoints and source URLs come from local configuration. Peer/channel
        # content and even a remote connect response cannot choose a destination.
        key = (source, thread_id, response['channel'], response['member_id'])
        with self._history_lock:
            if recovered and key in self._live_joins:
                return  # A live join may have completed during thread/resume.
            if not recovered:
                self._live_joins.add(key)
            register({'endpoint': self.endpoint, 'thread_id': thread_id, 'source': source,
                'url': str((home() / 'nth.db').resolve()) if source == 'local' else self.quartet_url,
                'channel': response['channel'], 'member_id': response['member_id'],
                'session_token': response['session_token'], 'filter': 'about'})

    def recover_history(self, thread_id, turns):
        # A member can reconnect with several tokens in one thread. Recover
        # only its latest join, so stale joins cannot re-enable a listener
        # that was explicitly stopped under the current token.
        latest = {}
        for turn in turns:
            for item in turn.get('items', []):
                message = {'method': 'item/completed', 'params':
                           {'threadId': thread_id, 'item': item}}
                identity = connected_identity(message)
                if identity:
                    source, _, body = identity
                    latest[(source, body['channel'], body['member_id'])] = message
        for message in latest.values():
            self.notification(message, recovered=True)

    def run(self):
        while not self.stop.is_set():
            client = CodexSocketClient(self.endpoint, on_notification=self.notification)
            try:
                client.start()
                subscribed = set()
                while not self.stop.is_set():
                    page = client.request('thread/loaded/list', {})
                    loaded = set(page.get('data', []))
                    cursor = page.get('nextCursor')
                    while cursor:
                        page = client.request('thread/loaded/list', {'cursor': cursor})
                        loaded.update(page.get('data', []))
                        cursor = page.get('nextCursor')
                    for thread_id in loaded - subscribed:
                        resumed = client.request('thread/resume', {'threadId': thread_id})
                        if resumed['thread']['id'] == thread_id:
                            subscribed.add(thread_id)
                            # A quick connect may complete before subscription.
                            # Recover only successful MCP connect items from
                            # this explicitly watched server's loaded threads.
                            self.recover_history(thread_id, resumed['thread'].get('turns', []))
                    subscribed.intersection_update(loaded)
                    self.stop.wait(1)
            except Exception:
                self.stop.wait(3)
            finally:
                client.stop()


def service_alive(window=8):
    """True only for a fresh heartbeat. Reads one file; never starts the service.

    A registry row keeps saying 'listening' after the service that owned it has
    died, so readiness is decided here and not from the row.
    """
    try:
        age = time.time() - json.loads((state_dir() / 'service.json').read_text())['heartbeat']
    except (OSError, ValueError, KeyError, TypeError):
        return False
    # A NaN or infinite heartbeat fails both comparisons; a future one is not fresh.
    return -1 < age < window


def ensure_service():
    ready = state_dir() / 'service.json'
    try:
        status = json.loads(ready.read_text())
        if time.time() - status['heartbeat'] < 8:
            return status
    except (OSError, ValueError, KeyError):
        pass
    log = open(state_dir() / 'service.log', 'ab')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    log.close()
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            status = json.loads(ready.read_text())
            if time.time() - status['heartbeat'] < 8:
                return status
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise RuntimeError('Trio event service did not become ready; inspect events/service.log')


def serve(stop=None):
    os.umask(0o077)
    # Reuse the cross-platform lifetime lock; this DB is only a service lease.
    lease = Spool(state_dir() / 'service-lock.sqlite',
                  dict(endpoint='service', thread_id='service', url='local',
                       channel='service', member_id='service', filter='all'))
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    workers, observers = {}, {}

    def worker(row, worker_stop):
        binding_id, revision = row['id'], row['revision']
        b = json.loads(row['config'])
        def cancelled():
            with database() as db:
                current = db.execute('SELECT revision,enabled FROM bindings WHERE id=?', (binding_id,)).fetchone()
            return not current or not current['enabled'] or current['revision'] != revision
        backoff = 1
        while not worker_stop.is_set() and not cancelled():
            try:
                # Filter changes retain receipt history: fingerprint excludes the
                # selection policy in service mode, but the source/member stays fixed.
                run(b, state_dir() / (binding_id + '.sqlite'), stop_event=worker_stop,
                    cancelled=cancelled, on_status=lambda s: set_status(binding_id, s),
                    on_receipt=lambda r: set_status(binding_id, 'listening', last_message=r['message_id']))
                break
            except UncertainDelivery:
                set_status(binding_id, 'attention', 'unconfirmed_delivery')
                return
            except MembershipEnded:
                set_status(binding_id, 'ended', 'membership_ended')
                return
            except Exception as exc:
                set_status(binding_id, 'reconnecting', type(exc).__name__)
                worker_stop.wait(backoff)
                backoff = min(backoff * 2, 30)
        set_status(binding_id, 'stopped')

    try:
        while not stop.is_set():
            rows = {r['id']: r for r in bindings()}
            for binding_id, (revision, worker_stop, thread) in list(workers.items()):
                row = rows.get(binding_id)
                if not row or not row['enabled'] or row['revision'] != revision:
                    worker_stop.set()
                    if not thread.is_alive():
                        workers.pop(binding_id)
            for binding_id, row in rows.items():
                if row['enabled'] and binding_id not in workers and row['status'] not in ('attention', 'ended'):
                    worker_stop = threading.Event()
                    thread = threading.Thread(target=worker, args=(row, worker_stop), daemon=True)
                    workers[binding_id] = (row['revision'], worker_stop, thread)
                    thread.start()
            with database() as db:
                endpoints = list(db.execute('SELECT * FROM endpoints WHERE enabled=1'))
            configured = {e['endpoint']: e['quartet_url'] for e in endpoints}
            for address, (url, observer_stop, thread) in list(observers.items()):
                if configured.get(address) != url:
                    observer_stop.set()
                    if not thread.is_alive():
                        observers.pop(address)
            for endpoint in endpoints:
                if endpoint['endpoint'] not in observers:
                    observer_stop = threading.Event()
                    observer = Observer(endpoint['endpoint'], endpoint['quartet_url'], observer_stop)
                    thread = threading.Thread(target=observer.run, daemon=True)
                    observers[endpoint['endpoint']] = (endpoint['quartet_url'], observer_stop, thread)
                    thread.start()
            ready = state_dir() / 'service.json'
            temporary = ready.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': os.getpid(), 'heartbeat': time.time(),
                'bindings': len(workers), 'endpoints': len(observers)}))
            temporary.replace(ready)
            stop.wait(1)
    finally:
        for _, observer_stop, _ in observers.values():
            observer_stop.set()
        for _, worker_stop, _ in workers.values():
            worker_stop.set()
        lease.close()
        (state_dir() / 'service.json').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'status'])
    args = parser.parse_args()
    if args.command == 'serve':
        serve()
    else:
        print(json.dumps(public_status(), indent=2))
