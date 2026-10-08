"""Durable interposer state, written only by the leased service.

A single connection and lock serialize worker requests. FULL synchronization keeps
committed high water durable; schema and legacy import markers commit atomically.
"""
import json
from pathlib import Path
import sqlite3
import threading
import time

from nth_interposer_wire import home, private_dir, IDENTITY_KEY, SESSION_ID, validate_hub

SCHEMA_VERSION = 1
SCHEMA = '''
CREATE TABLE hubs(server TEXT PRIMARY KEY, url TEXT, announced_at REAL,
                  state TEXT, since REAL, error TEXT);
CREATE TABLE memberships(key TEXT PRIMARY KEY, source TEXT, url TEXT, channel TEXT, member_id TEXT,
    filter TEXT DEFAULT 'about', enabled INT DEFAULT 1, ended TEXT DEFAULT '',
    owner_session TEXT, announced_through INT DEFAULT 0, acked_through INT DEFAULT 0,
    poll_state TEXT, poll_error TEXT, last_ok REAL);
CREATE TABLE sessions(session TEXT PRIMARY KEY, client TEXT, sink TEXT, host_pid INT, host_stamp INT,
    state TEXT, host_ok INT, problem TEXT, registered REAL, last_wake REAL, wakes_hour INT);
CREATE TABLE holdings(session TEXT, key TEXT, server TEXT, joined REAL, PRIMARY KEY(session,key));
CREATE TABLE deliveries(id INTEGER PRIMARY KEY, session TEXT, sink TEXT, body TEXT, ranges TEXT,
    state TEXT, created REAL, done REAL);
CREATE TABLE appserver_spool(key TEXT, message_id INT, payload TEXT, state TEXT, turn_id TEXT,
    PRIMARY KEY(key,message_id));
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def _json_file(path):
    if path.is_symlink():
        raise ValueError('legacy state must not be a symlink')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('legacy state must be an object')
    return value


def _mapping(value, field):
    result = value.get(field, {})
    if not isinstance(result, dict):
        raise ValueError('legacy state map is malformed')
    return result


def _mark(value):
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise ValueError('legacy high water must be a nonnegative integer')
    return value


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else home() / 'events' / 'interposer.sqlite'
        private_dir(self.path.parent)
        if self.path.is_symlink():
            raise PermissionError('interposer database must not be a symlink')
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('unsupported interposer schema version')
            if version == 0:
                self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                                      f'PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def announce(self, server, url):
        validate_hub(server, url)
        now = time.time()
        with self.lock, self.db:
            self.db.execute('''INSERT INTO hubs VALUES (?,?,?,'announced',?,'')
                ON CONFLICT(server) DO UPDATE SET url=excluded.url,
                announced_at=excluded.announced_at, state='announced', since=excluded.since, error='' ''',
                            (server, url, now, now))
            return dict(self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone())

    def snapshot(self, key=None, session=None):
        with self.lock:
            def rows(table, column, value):
                query = f'SELECT * FROM {table}'
                arguments = ()
                if value is not None:
                    query += f' WHERE {column}=?'
                    arguments = (value,)
                return [dict(row) for row in self.db.execute(query + f' ORDER BY {column}', arguments)]
            # Delivery bodies/payloads can contain peer text in later PRs; truth ops
            # expose control state only, never those content-bearing columns.
            holdings = rows('holdings', 'session', session)
            if key is not None:
                holdings = [row for row in holdings if row['key'] == key]
            return {'hubs': rows('hubs', 'server', None),
                    'memberships': rows('memberships', 'key', key),
                    'sessions': rows('sessions', 'session', session), 'holdings': holdings}

    def live_sessions(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sessions WHERE state IN ('waiting','in_turn')").fetchone()[0]

    def import_hooks(self, directory=None):
        directory = Path(directory) if directory is not None else self.path.parent / 'hooks'
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM meta WHERE key='hooks_imported'").fetchone():
                return False
            # Read all files before any writes. A corrupt file must not mark a partial
            # migration complete and quietly reset a membership's stopped filter.
            configs = []
            sessions = []
            for path in sorted(directory.glob('membership-*.json')):
                key = path.name[len('membership-'):-len('.json')]
                if IDENTITY_KEY.fullmatch(key):
                    configs.append((key, _json_file(path)))
            for path in sorted(directory.glob('session-*.json')):
                session = path.name[len('session-'):-len('.json')]
                if SESSION_ID.fullmatch(session):
                    sessions.append((session, _json_file(path)))
            for key, config in configs:
                mode = config.get('filter', 'about')
                enabled = config.get('enabled', True)
                ended = config.get('ended', '')
                if mode not in ('all', 'about', 'at') or type(enabled) is not bool or not isinstance(ended, str):
                    raise ValueError('legacy membership configuration is malformed')
                self.db.execute('''INSERT INTO memberships(key,filter,enabled,ended) VALUES (?,?,?,?)
                    ON CONFLICT(key) DO NOTHING''', (key, mode, int(enabled), ended))
            for session, state in sessions:
                self._import_session(session, state)
            self.db.execute("INSERT INTO meta VALUES ('hooks_imported','1')")
            return True

    def _import_session(self, session, state):
        client = state.get('client', '')
        if client not in ('', 'claude', 'codex') or type(state.get('ended', False)) is not bool:
            raise ValueError('legacy session is malformed')
        # A disk record is not proof a host is alive. PR 3 registration will supply
        # pid/start-stamp and reactivate it; importing must not strand the idle lease.
        self.db.execute('''INSERT INTO sessions(session,client,sink,state,host_ok,problem,wakes_hour)
            VALUES (?,?,?, ?,0,'awaiting registration',0) ON CONFLICT(session) DO NOTHING''',
                        (session, client, 'queue' if client == 'codex' else 'rewake',
                         'ended' if state.get('ended') else 'idle_unreachable'))
        memberships = _mapping(state, 'memberships')
        marks, acked = _mapping(state, 'high_water'), _mapping(state, 'acked')
        servers, joined = _mapping(state, 'servers'), _mapping(state, 'joined')
        for key in sorted(set(memberships) | set(marks) | set(acked)):
            if not isinstance(key, str) or not IDENTITY_KEY.fullmatch(key):
                raise ValueError('legacy identity key is malformed')
            member = memberships.get(key, {})
            if not isinstance(member, dict):
                raise ValueError('legacy membership is malformed')
            # Explicit projection: identity tokens and arbitrary legacy fields are
            # never copied into SQLite, even if old state files contain them.
            fields = [member.get(field, '') for field in ('source', 'channel', 'member_id')]
            if not all(isinstance(field, str) for field in fields):
                raise ValueError('legacy membership metadata is malformed')
            self.db.execute('''INSERT INTO memberships(key,source,channel,member_id,announced_through,acked_through)
                VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                source=excluded.source,channel=excluded.channel,member_id=excluded.member_id,
                announced_through=MAX(announced_through,excluded.announced_through),
                acked_through=MAX(acked_through,excluded.acked_through)''',
                            (key, *fields, _mark(marks.get(key, 0)), _mark(acked.get(key, 0))))
            if key in memberships:
                server, stamp = servers.get(key, ''), joined.get(key, 0)
                if not isinstance(server, str) or type(stamp) not in (int, float):
                    raise ValueError('legacy holding is malformed')
                self.db.execute('''INSERT INTO holdings VALUES (?,?,?,?)
                    ON CONFLICT(session,key) DO NOTHING''', (session, key, server, stamp))
