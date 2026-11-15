"""Durable interposer state.

A single connection and lock serialize worker requests. FULL synchronization keeps
committed high water durable. Legacy files remain authoritative until cutover.
"""
import json
import os
import stat
import math
from pathlib import Path
import sqlite3
import threading
import time

from nth_interposer_wire import home, private_dir, IDENTITY_KEY, SESSION_ID, validate_hub, WireError
from nth_interposer_hubs import StoreLock

SCHEMA_VERSION = 3
MAX_HUBS = 32
MAX_ANNOUNCED_HUBS = 8
ANNOUNCED_TTL = 86400
MAX_SESSIONS = 256
HOOKS_IMPORT_CUTOVER = 'hooks_import_cutover'  # Only the eventual cutover PR may set this.
HOOKS_IMPORT_INTERVAL = 60
# Terminal outbox rows are history only; bound them by age and count so queue
# lookups and status aggregation stay proportional to live work. Claimed rows
# ('sending', 'uncertain') are never pruned: an operator must inspect them.
DELIVERY_RETENTION = 7 * 86400
MAX_TERMINAL_DELIVERIES = 4096
TERMINAL_DELIVERY_STATES = ('written', 'cancelled')
DELIVERY_SINKS = ('tmux', 'opencode')
# meta key prefix + delivery id: the membership items behind an unclaimed
# notice, so an ownership change can move it. Kept in meta so the schema and
# a rollback to an older build are unaffected.
OUTBOX_MEMBERS = 'outbox_members:'
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
    descriptor = os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
    with os.fdopen(descriptor,'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024:
            raise ValueError('private state must be a bounded regular file')
        value = json.loads(stream.read(1024*1024+1))
    if not isinstance(value,dict):
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
        self.lock = StoreLock()
        self.import_skips = []
        self.import_cache = {}
        # Runtime uses this same mapping; eviction must retain metadata until
        # buffered evidence is successfully released (including failed writes).
        self.pending_buffers = {}
        self.pending_evidence_keys = {}
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValueError('unsupported interposer schema version')
            if version == 0:
                self.db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA +
                                      'PRAGMA user_version=2;\nCOMMIT;')
            elif version == 1:
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    columns = {r[1] for r in self.db.execute('PRAGMA table_info(hubs)')}
                    for column, definition in (('pending_url', "TEXT DEFAULT ''"), ('trust', "TEXT DEFAULT 'announced'")):
                        if column not in columns:
                            self.db.execute(f'ALTER TABLE hubs ADD COLUMN {column} {definition}')
                    self.db.execute("UPDATE hubs SET state='pending' WHERE trust='announced'")
                    self.db.execute('PRAGMA user_version=2')
            # One version chain: v1 -> main's v2 hub trust -> v3 shadow ownership.
            # Conditional additions also accept previously extended PR 3 v1 copies.
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                for table, additions in {
                    'hubs': {'approved': 'INT DEFAULT 0', 'config_url': "TEXT DEFAULT ''",
                             'config_pending_url': "TEXT DEFAULT ''"},
                    'memberships': {'shadow_filter': 'TEXT', 'shadow_enabled': 'INT', 'shadow_ended': 'TEXT',
                        'shadow_announced_through': 'INT DEFAULT 0', 'shadow_acked_through': 'INT DEFAULT 0',
                        'shadow_notices': 'INT DEFAULT 0', 'shadow_ids': 'INT DEFAULT 0'},
                    'holdings': {'attached': 'INT DEFAULT 0'},
                }.items():
                    columns = {r[1] for r in self.db.execute(f'PRAGMA table_info({table})')}
                    for column, definition in additions.items():
                        if column not in columns:
                            self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
                # Serves the sink/state/session queue lookups and status counts.
                self.db.execute('CREATE INDEX IF NOT EXISTS deliveries_queue ON deliveries(sink,state,session,id)')
                # Earlier builds evicted sessions without touching their outbox;
                # such queued rows can never be delivered and break the drain.
                self.db.execute("""UPDATE deliveries SET state='cancelled',done=? WHERE state='queued'
                    AND session NOT IN (SELECT session FROM sessions)""", (time.time(),))
                self._prune_deliveries()
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def _hub_room(self, *, setup=False):
        self.db.execute("DELETE FROM hubs WHERE trust='announced' AND announced_at<?", (time.time()-ANNOUNCED_TTL,))
        if not setup and self.db.execute("SELECT COUNT(*) FROM hubs WHERE trust='announced'").fetchone()[0] >= MAX_ANNOUNCED_HUBS:
            raise WireError('announced hub limit reached (maximum 8)', 'hub_limit')
        if self.db.execute('SELECT COUNT(*) FROM hubs').fetchone()[0] >= MAX_HUBS:
            oldest = self.db.execute("SELECT server FROM hubs WHERE trust='announced' ORDER BY announced_at,server LIMIT 1").fetchone()
            if setup and oldest:
                self.db.execute('DELETE FROM hubs WHERE server=?', (oldest[0],))
            else:
                raise WireError('hub table is full (maximum 32)', 'hub_limit')

    def announce(self, server, url, log=None, closing=None):
        from nth_interposer_hubs import check_host, restricted_host
        from nth_interposer_wire import canonical_server
        server = canonical_server(server)
        validate_hub(server, url, allow_restricted=True)
        def trusted_config():
            return [tuple(row) for row in self.db.execute(
                "SELECT server,url,trust,config_url FROM hubs WHERE url=? AND trust='setup' AND config_url=? ORDER BY server",
                (url,url))]
        with self.lock:
            if closing and closing():
                raise WireError('interposer is closing', 'service_closing')
            trusted = trusted_config()
        check_host(url, allow_restricted=bool(trusted) and restricted_host(url))
        with self.lock:
            if closing and closing():
                raise WireError('interposer is closing', 'service_closing')
            if trusted_config() != trusted:
                raise WireError('hub configuration changed during validation', 'hub_not_allowed')
            return self._announce_checked(server, url, log)

    def _announce_checked(self, server, url, log):
        with self.db:
            existing = self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone()
            now = time.time()
            if existing is None:
                self._hub_room()
                self.db.execute("INSERT INTO hubs(server,url,announced_at,state,since,error) VALUES (?,?,?,'pending',?,'')", (server,url,now,now))
            elif existing['url'] != url:
                self.db.execute('UPDATE hubs SET pending_url=?,announced_at=? WHERE server=?', (url,now,server))
                import logging
                (log or logging.getLogger('trio.interposer')).warning('hub change pending')
            else:
                self.db.execute('UPDATE hubs SET announced_at=? WHERE server=?', (now,server))
            return dict(self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone())

    def setup_hub(self, server, url):
        from nth_interposer_wire import canonical_server
        server = canonical_server(server)
        validate_hub(server, url, allow_restricted=True)
        with self.lock, self.db:
            known = self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone()
            if known is None:
                self._hub_room(setup=True)
                self.db.execute("INSERT INTO hubs(server,url,announced_at,state,since,error,trust,config_url) VALUES (?,?,?,'announced',?,'','setup',?)",
                                (server,url,time.time(),time.time(),url))
            elif known['approved'] and known['url'] != url:
                # User approval outranks stale config. Preserve it and expose the config candidate.
                self.db.execute('UPDATE hubs SET config_url=?,config_pending_url=? WHERE server=?', (url,url,server))
            else:
                self.db.execute("UPDATE hubs SET url=?,trust='setup',state='announced',config_url=?,config_pending_url='' WHERE server=?", (url,url,server))

    def import_hubs(self, root=None, log=None):
        from nth_interposer_hubs import config_hubs
        for server, url in config_hubs(root):
            try:
                self.setup_hub(server, url)
            except (OSError, ValueError, sqlite3.Error) as exc:
                if log:
                    log.warning('hub import skipped: %s', type(exc).__name__)

    def approve(self, server, url):
        from nth_interposer_hubs import check_host, restricted_host
        from nth_interposer_wire import canonical_server
        server = canonical_server(server)
        validate_hub(server, url, allow_restricted=True)
        with self.lock:
            row = self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone()
            if row is None:
                raise WireError('hub is unknown', 'unknown_hub')
            pending = row['pending_url'] or row['config_pending_url'] or row['url']
            if url != pending:
                raise WireError('approval URL does not match the pending URL', 'hub_not_allowed')
            snapshot = tuple(row)
            allow = row['trust']=='setup' and row['config_url']==url and restricted_host(url)
        check_host(url, allow_restricted=allow)
        with self.lock,self.db:
            current = self.db.execute('SELECT * FROM hubs WHERE server=?', (server,)).fetchone()
            if current is None or tuple(current)!=snapshot:
                raise WireError('hub configuration changed during approval', 'hub_not_allowed')
            self.db.execute("UPDATE hubs SET url=?,pending_url='',config_pending_url='',trust='setup',state='announced',approved=1 WHERE server=?", (url,server))
        return {'server': server, 'url': url, 'approved': True}

    def _prune_deliveries(self):
        states = TERMINAL_DELIVERY_STATES
        self.db.execute(f'DELETE FROM deliveries WHERE state IN ({",".join("?"*len(states))}) AND COALESCE(done,created)<?',
                        (*states, time.time()-DELIVERY_RETENTION))
        self.db.execute(f'''DELETE FROM deliveries WHERE state IN ({",".join("?"*len(states))}) AND id<=(
            SELECT id FROM deliveries WHERE state IN ({",".join("?"*len(states))}) ORDER BY id DESC LIMIT 1 OFFSET ?)''',
                        (*states, *states, MAX_TERMINAL_DELIVERIES))
        # Claimed rows are never moved, so their membership record is dead weight.
        self.db.execute(f'''DELETE FROM meta WHERE substr(key,1,{len(OUTBOX_MEMBERS)})=? AND NOT EXISTS(
            SELECT 1 FROM deliveries d WHERE d.state='queued' AND d.id=CAST(substr(meta.key,{len(OUTBOX_MEMBERS)+1}) AS INTEGER))''',
                        (OUTBOX_MEMBERS,))

    def prune_deliveries(self):
        with self.lock, self.db:
            self._prune_deliveries()

    def queue_notice(self, session, items, enqueue):
        """Caller holds the lock and transaction. Record which memberships a
        queued row carries so a later ownership change can move it."""
        if not enqueue(session, items):
            return False
        delivery = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                        (OUTBOX_MEMBERS+str(delivery), json.dumps([dict(item) for item in items], allow_nan=False)))
        return True

    def _sink_enqueue(self, sink):
        if sink == 'tmux':
            from nth_interposer_tmux import TmuxSink
            return TmuxSink(self).enqueue
        from nth_interposer_opencode import enqueue
        return lambda session, items: enqueue(self, session, items)

    def _cancel_queued(self, session):
        for row in self.db.execute("SELECT id FROM deliveries WHERE session=? AND state='queued'", (session,)).fetchall():
            self.db.execute('DELETE FROM meta WHERE key=?', (OUTBOX_MEMBERS+str(row[0]),))
        self.db.execute("UPDATE deliveries SET state='cancelled',done=? WHERE session=? AND state='queued'",
                        (time.time(), session))

    def _outbox_items(self, delivery):
        """The membership items a queued row announces; [] when unrecorded or damaged."""
        record = self.db.execute('SELECT value FROM meta WHERE key=?', (OUTBOX_MEMBERS+str(delivery),)).fetchone()
        try:
            items = json.loads(record[0]) if record else []
        except ValueError:
            items = []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _cancel_acknowledged(self, session):
        """Caller holds the lock and transaction. Cancel unclaimed notices that
        announce only messages the session has since acknowledged: it read them
        through a hook wake or its own poll while the notice waited, and typing
        the notice now would start a turn with nothing to read. A notice that
        reports ended delivery, or has no item record, is kept."""
        for row in self.db.execute("SELECT id FROM deliveries WHERE session=? AND state='queued'", (session,)).fetchall():
            items = self._outbox_items(row['id'])
            for item in items:
                acked = self.db.execute('SELECT shadow_acked_through FROM memberships WHERE key=?',
                                        (item.get('key'),)).fetchone()
                if (item.get('reason') or type(item.get('last_id')) is not int
                        or not acked or acked[0] is None or acked[0] < item['last_id']):
                    break
            else:
                if items:
                    self.db.execute("UPDATE deliveries SET state='cancelled',done=? WHERE id=?", (time.time(), row['id']))
                    self.db.execute('DELETE FROM meta WHERE key=?', (OUTBOX_MEMBERS+str(row['id']),))

    def _rehome_queued(self, session, ending=False):
        """Move unclaimed notices whose membership now belongs to another live
        session. Claimed rows ('sending'/'uncertain') are never touched: they
        may already be on screen, and a replay would duplicate them."""
        moved = []
        for row in self.db.execute("SELECT id FROM deliveries WHERE session=? AND state='queued' ORDER BY id",
                                   (session,)).fetchall():
            items = self._outbox_items(row['id'])
            stay, away = [], {}
            for item in items:
                owner = self.db.execute('SELECT owner_session FROM memberships WHERE key=?',
                                        (item.get('key'),)).fetchone()
                owner = owner[0] if owner else None
                if owner == session:
                    stay.append(item)
                elif owner:
                    away.setdefault(owner, []).append(item)
            if not away and not ending:
                continue
            self.db.execute("UPDATE deliveries SET state='cancelled',done=? WHERE id=?", (time.time(), row['id']))
            self.db.execute('DELETE FROM meta WHERE key=?', (OUTBOX_MEMBERS+str(row['id']),))
            if stay and not ending and not self.queue_notice(
                    session, stay, self._sink_enqueue(self.session(session)['sink'])):
                # The sink refused (its binding is gone): keep the items in the
                # runtime buffer like the ones that moved away.
                moved.append((session, stay))
            for owner, owned in away.items():
                target = self.session(owner)
                for item in owned:
                    item['owner_session'] = owner
                if target['sink'] in DELIVERY_SINKS and self.queue_notice(owner, owned, self._sink_enqueue(target['sink'])):
                    continue
                # A shadow or currently unbound owner takes the notice through the
                # runtime buffer, whose release path retries and journals it.
                moved.append((owner, owned))
        for owner, owned in moved:
            buffer = self.pending_buffers.setdefault(owner, {'at': time.monotonic(), 'members': {}})
            for item in owned:
                buffer['members'].setdefault(item['key'], item)
        if ending:
            self._cancel_queued(session)

    def _forget_sinks(self, session):
        self.db.execute('DELETE FROM meta WHERE key IN (?,?)', ('tmux:'+session, 'opencode_poll:'+session))

    def snapshot(self, key=None, session=None, ledger=True):
        with self.lock:
            def rows(table, column, value):
                query = f'SELECT * FROM {table}'
                arguments = ()
                if value is not None:
                    query += f' WHERE {column}=?'
                    arguments = (value,)
                return [dict(row) for row in self.db.execute(query + f' ORDER BY {column}', arguments)]
            sessions = rows('sessions', 'session', session)
            for item in sessions:
                if item.get('sink') == 'opencode':
                    heartbeat = self.db.execute('SELECT value FROM meta WHERE key=?',('opencode_poll:'+item['session'],)).fetchone()
                    try:
                        value = json.loads(heartbeat[0]) if heartbeat else 0
                    except ValueError:
                        value = 0
                    # A damaged heartbeat reads as never seen, not as a crash.
                    item['sink_heartbeat'] = value if type(value) in (int, float) and math.isfinite(value) else 0
            # Delivery bodies/payloads can contain peer text in later PRs; truth ops
            # expose control state only, never those content-bearing columns.
            holdings = rows('holdings', 'session', session)
            if key is not None:
                holdings = [row for row in holdings if row['key'] == key]
            result = {'hubs': rows('hubs', 'server', None),
                    'memberships': rows('memberships', 'key', key),
                    'sessions': sessions, 'holdings': holdings,
                    'legacy_import_skips': list(self.import_skips)}
            if ledger:
                # Callers inside the service (reconcile, death sweep) pass
                # ledger=False: they never read these counts.
                query = 'SELECT session,sink,state,COUNT(*) AS count FROM deliveries'
                arguments = ()
                if session is not None:
                    query += ' WHERE session=?'
                    arguments = (session,)
                result['delivery_states'] = [dict(r) for r in self.db.execute(
                    query + ' GROUP BY session,sink,state', arguments)]
            return result

    def live_sessions(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sessions WHERE state IN ('waiting','in_turn','idle')").fetchone()[0]

    def session(self, session):
        row = self.db.execute('SELECT * FROM sessions WHERE session=?', (session,)).fetchone()
        if row is None:
            raise WireError('session is not registered', 'unknown_session')
        return dict(row)

    def _session_room(self):
        if self.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] < MAX_SESSIONS:
            return
        victim = next((row for row in self.db.execute(
            "SELECT session FROM sessions WHERE state='ended' OR registered IS NULL ORDER BY registered,session")
            if row[0] not in self.pending_buffers and not self.db.execute(
                'SELECT 1 FROM meta WHERE substr(key,1,length(?))=?',
                ('shadow_pending:'+row[0]+':','shadow_pending:'+row[0]+':')).fetchone()), None)
        if victim is None:
            raise WireError('session limit reached', 'session_limit')
        # Queued work for a deleted session can never be admitted; claimed rows
        # stay as evidence. Sink bindings go with the session they describe.
        self._cancel_queued(victim[0])
        self._forget_sinks(victim[0])
        self.db.execute('DELETE FROM holdings WHERE session=?', (victim[0],))
        self.db.execute('DELETE FROM sessions WHERE session=?', (victim[0],))

    def register(self, request):
        from nth_claude_hook import process_stamp
        pid = request['host_pid']
        # Legacy hook observations must not replace an explicitly attached live sink.
        with self.lock:
            active=self.db.execute('SELECT * FROM sessions WHERE session=?',(request['session'],)).fetchone()
            if active and active['sink'] in ('tmux','opencode') and request['sink']!=active['sink'] and active['state']!='ended':
                return {'session':request['session'],'state':active['state']}
        if pid is None:
            with self.lock:
                known=self.db.execute('SELECT host_pid,host_stamp FROM sessions WHERE session=?',(request['session'],)).fetchone()
            if known and known['host_pid'] and known['host_stamp'] is not None and process_stamp(known['host_pid'])==known['host_stamp']:
                pid=known['host_pid']
        try:
            stamp = process_stamp(pid) if pid else None
        except (OverflowError, OSError):
            stamp = None
        if request['sink']=='opencode' and stamp is None:
            raise WireError('OpenCode host process is not live', 'dead_host')
        with self.lock, self.db:
            previous = self.db.execute('SELECT registered FROM sessions WHERE session=?',
                                       (request['session'],)).fetchone()
            registered = time.time()
            if previous and previous['registered'] is not None:
                # A strictly increasing registration stamp also identifies the
                # generation when a PID is unchanged or the wall clock stalls.
                registered = max(registered, math.nextafter(previous['registered'], math.inf))
            if not previous:
                self._session_room()
            self.db.execute('''INSERT INTO sessions(session,client,sink,host_pid,host_stamp,state,
                host_ok,problem,registered,wakes_hour) VALUES (?,?,?,?,?,'idle',?,?,?,0)
                ON CONFLICT(session) DO UPDATE SET client=excluded.client,sink=excluded.sink,
                host_pid=excluded.host_pid,host_stamp=excluded.host_stamp,
                state=CASE WHEN sessions.state='in_turn' AND sessions.host_pid IS excluded.host_pid
                    AND sessions.host_stamp IS excluded.host_stamp THEN 'in_turn'
                WHEN sessions.state='ended' AND ?=0 AND ? IS NULL THEN 'ended' ELSE 'idle' END,
                host_ok=excluded.host_ok,problem=excluded.problem,registered=excluded.registered''',
                (request['session'], request['client'], request['sink'], pid, stamp,
                 int(request['host_ok']), request['problem'], registered, int(request.get('resume',False)), stamp))
            if request['sink']=='tmux':
                # host_pid is the client. pane_pid, when given, is the pane's first
                # process (often a shell that outlives the client); both lifetimes
                # must still hold when a notice is typed.
                pane_pid = request.get('pane_pid') or pid
                try:
                    pane_stamp = stamp if pane_pid == pid else process_stamp(pane_pid)
                except (OverflowError, OSError):
                    pane_stamp = None
                if stamp is None or pane_stamp is None:
                    raise WireError('tmux host process is not live', 'dead_host')
                binding = dict(pane=request['pane'], pid=pane_pid, stamp=pane_stamp)
                if pane_pid != pid:
                    binding.update(client=pid, client_stamp=stamp)
                binding=json.dumps(binding)
                self.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',('tmux:'+request['session'],binding))
            else:
                self.db.execute('DELETE FROM meta WHERE key=?',('tmux:'+request['session'],))
            if request['sink']=='opencode':
                self.db.execute('DELETE FROM meta WHERE key=?',('opencode_poll:'+request['session'],))
            state = self.session(request['session'])['state']
            if state in ('idle','in_turn','waiting'):
                # Resume alone must restart retained memberships. Recover only
                # unowned seats; registration cannot steal from another owner.
                for row in self.db.execute('''SELECT m.key FROM memberships m
                    JOIN holdings h ON h.key=m.key WHERE h.session=? AND h.attached=1
                    AND m.owner_session IS NULL''', (request['session'],)).fetchall():
                    owner = self.db.execute('''SELECT h.session FROM holdings h JOIN sessions s
                        ON h.session=s.session WHERE h.key=? AND h.attached=1
                        AND s.registered IS NOT NULL AND s.state IN ('idle','in_turn','waiting')
                        ORDER BY h.joined DESC,h.session DESC LIMIT 1''', (row['key'],)).fetchone()
                    if owner:
                        self.db.execute('UPDATE memberships SET owner_session=? WHERE key=?',
                                        (owner[0],row['key']))
            return {'session': request['session'], 'state': state}

    def attach(self, request):
        key, session = request['key'], request['session']
        with self.lock, self.db:
            owner = self.session(session)
            if owner['registered'] is None or owner['state'] not in ('idle','in_turn','waiting'):
                raise WireError('session is not registered and live', 'unknown_session')
            try:
                identity = _json_file(self.path.parent / 'identities' / (key + '.json'))
                source, url, channel, member = [identity[field] for field in
                                               ('source', 'url', 'channel', 'member_id')]
                if source not in ('local', 'quartet') or not all(isinstance(v, str) and v for v in
                                                               (url, channel, member)):
                    raise ValueError
                if source != 'local':
                    validate_hub(request['server'], url, allow_restricted=True)
            except (OSError, ValueError, KeyError, TypeError):
                raise WireError('identity is missing or malformed', 'bad_identity') from None
            if source != 'local' and not self.db.execute("SELECT 1 FROM hubs WHERE url=? AND trust='setup'", (url,)).fetchone():
                raise WireError('identity hub is not announced', 'hub_not_allowed')
            previous = self.db.execute('SELECT owner_session FROM memberships WHERE key=?', (key,)).fetchone()
            self.db.execute("UPDATE memberships SET shadow_ended='membership replaced',owner_session=NULL,poll_state='ended' WHERE key!=? AND source=? AND url=? AND channel=? AND member_id=?",(key,source,url,channel,member))
            self.db.execute('''INSERT INTO memberships(key,source,url,channel,member_id,owner_session)
                VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET source=excluded.source,
                url=excluded.url,channel=excluded.channel,member_id=excluded.member_id,
                owner_session=excluded.owner_session''', (key, source, url, channel, member, session))
            self.db.execute('''INSERT INTO holdings(session,key,server,joined,attached) VALUES (?,?,?,?,1) ON CONFLICT(session,key)
                DO UPDATE SET server=excluded.server,joined=excluded.joined,attached=1''',
                (session, key, request['server'], time.time()))
            self.db.execute('UPDATE memberships SET shadow_filter=COALESCE(shadow_filter,filter),shadow_enabled=COALESCE(shadow_enabled,enabled),shadow_ended=COALESCE(shadow_ended,ended) WHERE key=?',(key,))
            if previous and previous[0] and previous[0] != session and self.db.execute(
                    'SELECT 1 FROM sessions WHERE session=?', (previous[0],)).fetchone():
                self._rehome_queued(previous[0])
            return {'key': key, 'owner': session}

    def configure(self, request):
        with self.lock, self.db:
            for field in ('filter', 'enabled'):
                if field in request:
                    self.db.execute(f'UPDATE memberships SET shadow_{field}=? WHERE key=?',
                                    (request[field], request['key']))
            row = self.db.execute('SELECT key,COALESCE(shadow_filter,filter) AS filter,COALESCE(shadow_enabled,enabled) AS enabled FROM memberships WHERE key=?',
                                  (request['key'],)).fetchone()
            if row is None:
                raise WireError('membership is unknown', 'unknown_membership')
            if request.get('enabled'):
                self.db.execute("UPDATE memberships SET shadow_ended='' WHERE key=? AND shadow_ended='listener failure'",(request['key'],))
            return dict(row, enabled=bool(row['enabled']))

    def ack(self, request):
        with self.lock, self.db:
            self.session(request['session'])
            self.db.execute('UPDATE memberships SET shadow_acked_through=MAX(shadow_acked_through,?) WHERE key=?',
                            (request['through_id'], request['key']))
            self._cancel_acknowledged(request['session'])
        return {}

    def turn(self, request):
        with self.lock, self.db:
            current = self.session(request['session'])
            state = 'in_turn' if request['phase'] == 'started' else 'idle'
            if current['state'] == 'ended':
                state = 'ended'
            self.db.execute('UPDATE sessions SET state=? WHERE session=?', (state, request['session']))
        return {'state': state}

    def end(self, session):
        with self.lock, self.db:
            self.session(session)
            self.db.execute("UPDATE sessions SET state='ended' WHERE session=?", (session,))
            for row in self.db.execute('SELECT key FROM memberships WHERE owner_session=?', (session,)).fetchall():
                owner = self.db.execute('''SELECT h.session FROM holdings h JOIN sessions s
                    ON h.session=s.session WHERE h.key=? AND h.session!=? AND h.attached=1 AND s.registered IS NOT NULL AND s.state IN ('idle','in_turn','waiting')
                    ORDER BY h.joined DESC,h.session DESC LIMIT 1''', (row['key'], session)).fetchone()
                self.db.execute('UPDATE memberships SET owner_session=? WHERE key=?',
                                (owner[0] if owner else None, row['key']))
            self._rehome_queued(session, ending=True)
            self._forget_sinks(session)
        return {}

    def skip_status(self):
        with self.lock:
            # Doctor needs only a bounded summary, not a potentially huge list of
            # memberships/sessions. Bound long basenames as well as row count.
            return {'skips': [{'basename': row['basename'][:160], 'reason': row['reason']}
                              for row in self.import_skips[:32]],
                    'skipped_total': len(self.import_skips)}

    def import_hooks(self, directory=None, log=None):
        directory = Path(directory) if directory is not None else self.path.parent/'hooks'
        with self.lock:
            if self.db.execute('SELECT 1 FROM meta WHERE key=? AND value=?',(HOOKS_IMPORT_CUTOVER,'1')).fetchone():
                return False
        with self.lock,self.db:
            if self.db.execute("SELECT 1 FROM meta WHERE key='hooks_imported'").fetchone():
                self.db.execute("DELETE FROM meta WHERE key='hooks_imported'")
        if not directory.is_dir():
            return False
        entries = []
        # File I/O/parsing is outside the writer lock. Stable files need no SQL;
        # a slow legacy filesystem cannot hold up a membership's poll callback.
        for prefix,validator,importer in (('membership-',IDENTITY_KEY,self._import_config),
                                          ('session-',SESSION_ID,self._import_session)):
            for path in sorted(directory.glob(prefix+'*.json')):
                identity = path.name[len(prefix):-5]
                if not validator.fullmatch(identity):
                    continue
                try:
                    info = path.lstat()
                    signature = (info.st_ino,info.st_size,info.st_mtime_ns)
                    if self.import_cache.get(path)==signature:
                        continue
                    entries.append((path,identity,importer,signature,_json_file(path)))
                except (OSError,ValueError,TypeError,RecursionError):
                    entries.append((path,identity,importer,None,None))
        skipped = []
        with self.lock,self.db:
            if self.db.execute("SELECT 1 FROM meta WHERE key='hooks_imported'").fetchone():
                self.db.execute("DELETE FROM meta WHERE key='hooks_imported'")
            previous = self.import_skips
            for path,identity,importer,signature,value in entries:
                self.db.execute('SAVEPOINT legacy_file')
                try:
                    if value is None:
                        raise ValueError('invalid legacy state')
                    importer(identity,value)
                    self.import_cache[path] = signature
                except (ValueError,TypeError,OSError,OverflowError,RecursionError):
                    self.db.execute('ROLLBACK TO legacy_file')
                    row = {'basename':path.name,'reason':'invalid legacy state'}
                    skipped.append(row)
                    if log is not None and row not in previous:
                        log.warning('legacy import skipped %r: %s',path.name,row['reason'])
                finally:
                    self.db.execute('RELEASE legacy_file')
            self.import_skips = skipped
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
        if not self.db.execute('SELECT 1 FROM sessions WHERE session=?',(session,)).fetchone():
            self._session_room()
        self.db.execute('''INSERT INTO sessions(session,client,sink,state,host_ok,problem,wakes_hour)
            VALUES (?,?,?, ?,0,'awaiting registration',0) ON CONFLICT(session) DO UPDATE SET
            client=COALESCE(NULLIF(excluded.client,''),sessions.client),
            state=CASE WHEN sessions.registered IS NOT NULL THEN sessions.state
                       WHEN excluded.state='ended' THEN 'ended' ELSE excluded.state END''',
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
                self.db.execute('''INSERT INTO holdings(session,key,server,joined) VALUES (?,?,?,?)
                    ON CONFLICT(session,key) DO UPDATE SET
                    server=CASE WHEN holdings.attached=1 THEN holdings.server ELSE COALESCE(NULLIF(excluded.server,''),holdings.server) END,
                    joined=CASE WHEN holdings.attached=1 THEN holdings.joined ELSE MAX(holdings.joined,excluded.joined) END''', (session, key, server, stamp))
