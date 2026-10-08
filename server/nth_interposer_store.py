"""Durable interposer state, written only by the leased service.

A single connection and lock serialize worker requests. FULL synchronization keeps
committed high water durable. Legacy files remain authoritative until cutover.
"""
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from nth_interposer_wire import home, private_dir, IDENTITY_KEY, SESSION_ID, validate_hub

SCHEMA_VERSION = 2
HOOKS_IMPORT_CUTOVER = 'hooks_import_cutover'  # Only the eventual cutover PR may set this.
HOOKS_IMPORT_INTERVAL = 60
SCHEMA = '''
CREATE TABLE hubs(server TEXT PRIMARY KEY, url TEXT, announced_at REAL,
                  state TEXT, since REAL, error TEXT, pending_url TEXT DEFAULT '',
                  trust TEXT DEFAULT 'announced');
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
        self.import_skips = []
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError('unsupported interposer schema version')
            if version == 0:
                self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                                      f'PRAGMA user_version={SCHEMA_VERSION};\nCOMMIT;')
            elif version == 1:
                self.db.executescript('''BEGIN IMMEDIATE;
                    ALTER TABLE hubs ADD COLUMN pending_url TEXT DEFAULT '';
                    ALTER TABLE hubs ADD COLUMN trust TEXT DEFAULT 'announced';
                    UPDATE hubs SET state='pending';
                    PRAGMA user_version=2;
                    COMMIT;''')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def announce(self, server, url, log=None):
        validate_hub(server, url)
        now = time.time()
        with self.lock, self.db:
            existing = self.db.execute('SELECT url FROM hubs WHERE server=?', (server,)).fetchone()
            if existing is None:
                if self.db.execute('SELECT COUNT(*) FROM hubs').fetchone()[0] >= 32:
                    from nth_interposer_wire import WireError
                    raise WireError('hub table is full (maximum 32)')
                self.db.execute('''INSERT INTO hubs(server,url,announced_at,state,since,error)
                    VALUES (?,?,?,'pending',?,'')''', (server, url, now, now))
            elif existing['url'] != url:
                self.db.execute('UPDATE hubs SET pending_url=?,announced_at=? WHERE server=?', (url, now, server))
                if log is not None:
                    log.info('hub URL change pending: %s', server)
            else:
                self.db.execute('UPDATE hubs SET announced_at=? WHERE server=?', (now, server))
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
                    'sessions': rows('sessions', 'session', session), 'holdings': holdings,
                    'legacy_import_skips': list(self.import_skips)}

    def live_sessions(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sessions WHERE state IN ('waiting','in_turn')").fetchone()[0]

    def import_hooks(self, directory=None, log=None):
        directory = Path(directory) if directory is not None else self.path.parent / 'hooks'
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM meta WHERE key=? AND value=?',
                               (HOOKS_IMPORT_CUTOVER, '1')).fetchone():
                return False
            # Discard PR 2's premature marker even on an upgraded database. Nothing
            # here sets the future cutover marker while legacy waiters own state.
            self.db.execute("DELETE FROM meta WHERE key='hooks_imported'")
            previous = self.import_skips
            self.import_skips = []
            if not directory.is_dir():
                return False
            for prefix, validator, importer in (
                    ('membership-', IDENTITY_KEY, self._import_config),
                    ('session-', SESSION_ID, self._import_session)):
                for path in sorted(directory.glob(prefix + '*.json')):
                    identity = path.name[len(prefix):-len('.json')]
                    reason = 'invalid legacy state'
                    self.db.execute('SAVEPOINT legacy_file')
                    try:
                        if not validator.fullmatch(identity):
                            raise ValueError('bad legacy filename')
                        importer(identity, _json_file(path))
                    except (ValueError, TypeError, OSError, OverflowError, RecursionError):
                        self.db.execute('ROLLBACK TO legacy_file')
                        skipped = {'basename': path.name, 'reason': reason}
                        self.import_skips.append(skipped)
                        if log is not None and skipped not in previous:
                            log.warning('legacy import skipped %r: %s', path.name, reason)
                    finally:
                        self.db.execute('RELEASE legacy_file')
            return True

    def _import_config(self, key, config):
        mode = config.get('filter', 'about')
        enabled = config.get('enabled', True)
        ended = config.get('ended', '')
        if mode not in ('all', 'about', 'at') or type(enabled) is not bool or not isinstance(ended, str):
            raise ValueError('legacy membership configuration is malformed')
        self.db.execute('''INSERT INTO memberships(key,filter,enabled,ended) VALUES (?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET filter=excluded.filter,
            enabled=excluded.enabled, ended=excluded.ended''', (key, mode, int(enabled), ended))

    def _import_session(self, session, state):
        client = state.get('client', '')
        if client not in ('', 'claude', 'codex') or type(state.get('ended', False)) is not bool:
            raise ValueError('legacy session is malformed')
        # A disk record is not proof a host is alive. PR 3 registration will supply
        # pid/start-stamp and reactivate it; importing must not strand the idle lease.
        self.db.execute('''INSERT INTO sessions(session,client,sink,state,host_ok,problem,wakes_hour)
            VALUES (?,?,?, ?,0,'awaiting registration',0) ON CONFLICT(session) DO UPDATE SET
            client=COALESCE(NULLIF(excluded.client,''),sessions.client),
            state=CASE WHEN excluded.state='ended' THEN 'ended'
                       WHEN sessions.host_pid IS NULL THEN excluded.state ELSE sessions.state END''',
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
                source=COALESCE(NULLIF(excluded.source,''),memberships.source),
                channel=COALESCE(NULLIF(excluded.channel,''),memberships.channel),
                member_id=COALESCE(NULLIF(excluded.member_id,''),memberships.member_id),
                announced_through=MAX(announced_through,excluded.announced_through),
                acked_through=MAX(acked_through,excluded.acked_through)''',
                            (key, *fields, _mark(marks.get(key, 0)), _mark(acked.get(key, 0))))
            if key in memberships:
                server, stamp = servers.get(key, ''), joined.get(key, 0)
                if not isinstance(server, str) or type(stamp) not in (int, float) or not math.isfinite(stamp):
                    raise ValueError('legacy holding is malformed')
                self.db.execute('''INSERT INTO holdings VALUES (?,?,?,?)
                    ON CONFLICT(session,key) DO UPDATE SET
                    server=COALESCE(NULLIF(excluded.server,''),holdings.server),
                    joined=MAX(holdings.joined,excluded.joined)''', (session, key, server, stamp))
