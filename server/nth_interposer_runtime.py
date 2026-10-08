"""Shadow-only listeners; observation and its high water share one locked update."""
import os
import stat
import time

from nth_claude_hook import poll_factory, process_stamp
from nth_interposer_wire import home, private_dir, read_frame, WireError, MAX_FRAME, canonical_server
from nth_interposer_store import _json_file
from nth_listener import Listener, classify_poll, select_messages, OK
from nth_notice import message_notice, ended_notice, shown_reason, MAX_INTEGER
from nth_interposer_shadow import append, ranges_for


def shadow_row(row):
    row = dict(row)
    for field in ('filter', 'enabled', 'ended'):
        value = row['shadow_'+field]
        row[field] = row[field] if value is None else value
    row['announced_through'] = row['shadow_announced_through']
    return row


class ShadowListener(Listener):
    def __init__(self, runtime, row, identity):
        self.runtime, self.row = runtime, row
        self.prefix = 'trio' if row['source']=='local' else 'quartet'
        self.last_ok = None
        poll, close = runtime.factory(identity)
        def observe_poll(arguments):
            arguments = dict(arguments, monitor_heartbeat=False)
            arguments.pop('monitor_filter', None)
            result = poll(arguments)
            if classify_poll(result)==OK:
                self.last_ok = time.time()
            return result
        super().__init__(self,dict(identity,filter=row['filter']),observe_poll,close,
                         high_water=row['shadow_announced_through'])

    def _push_delay(self):
        return 0.0

    def _fresh(self, messages):
        with self.runtime.store.lock:
            row = self.runtime.member(self.row['key'])
            if not self.runtime.eligible(row):
                return []
            self.high_water = max(self.high_water,row['shadow_announced_through'])
            fresh = [m for m in super()._fresh(messages) if m['id']<MAX_INTEGER]
            selected = select_messages({'messages':fresh},row['filter'])
            if fresh:
                # Never commit seen IDs before they have an owner buffer. A handoff
                # after this lock moves that buffer, rather than discarding delivery.
                self.runtime.accumulate(row,selected)
                with self.runtime.store.db:
                    self.runtime.store.db.execute('UPDATE memberships SET shadow_announced_through=? WHERE key=?',
                                                  (fresh[-1]['id'],row['key']))
                self.high_water = fresh[-1]['id']
            return fresh

    def _deliver(self, fresh, selected):
        return True  # _fresh already atomically buffered the integer-only observation.

    def _end(self, reason, advice):
        del advice
        with self.runtime.store.lock:
            if self.runtime.pollers.get(self.row['key']) is not self or self._stop.is_set():
                return
            row = self.runtime.member(self.row['key'])
            if not self.runtime.eligible(row):
                return
            reason = shown_reason(reason)
            self.status,self.error = 'ended',reason
            self.runtime.accumulate(row,[],reason)
            with self.runtime.store.db:
                self.runtime.store.db.execute("UPDATE memberships SET shadow_ended=?,poll_state='ended' WHERE key=?",(reason,row['key']))


class Runtime:
    def __init__(self, store, log, factory=None):
        self.store,self.log = store,log
        self.factory = factory or self.poll_factory
        self.pollers,self.buffers = {},store.pending_buffers
        self.last_drain = self.last_death = 0.0

    def poll_factory(self, identity):
        if identity['source']=='local':
            return poll_factory(identity)
        from nth_listener import quartet_poll_factory
        from nth_interposer_hubs import connection_guard, restricted_host
        row = self.store.db.execute("SELECT config_url FROM hubs WHERE url=? AND trust='setup'",(identity['url'],)).fetchone()
        allow = bool(row and row['config_url']==identity['url'] and restricted_host(identity['url']))
        return quartet_poll_factory({'url':identity['url'],
            'connection_guard':connection_guard(identity['url'],allow_restricted=allow)})

    def member(self, key):
        row = self.store.db.execute('SELECT * FROM memberships WHERE key=?',(key,)).fetchone()
        return shadow_row(row) if row else None

    def allowed(self, row):
        return row['source']=='local' or bool(self.store.db.execute(
            "SELECT 1 FROM hubs WHERE url=? AND trust='setup'",(row['url'],)).fetchone())

    def eligible(self, row):
        if os.environ.get('TRIO_INTERPOSER_SHADOW')=='0' or not row:
            return False
        if not row['owner_session'] or not row['enabled'] or row['ended'] or not self.allowed(row):
            return False
        if not self.store.db.execute('SELECT 1 FROM holdings WHERE session=? AND key=? AND attached=1',(row['owner_session'],row['key'])).fetchone():
            return False
        session = self.store.session(row['owner_session'])
        return session['registered'] is not None and session['state'] in ('idle','in_turn','waiting')

    def current(self, listener):
        row = self.member(listener.row['key'])
        return (not listener._stop.is_set() and self.pollers.get(listener.row['key']) is listener
                and self.eligible(row) and all(row[k]==listener.row[k] for k in ('owner_session','filter','url')))

    def accumulate(self, row, selected, reason=''):
        if not selected and not reason:
            return
        session,key = row['owner_session'],row['key']
        buffer = self.buffers.setdefault(session,{'at':time.monotonic(),'members':{}})
        item = buffer['members'].setdefault(key,dict(row,first_id=0,last_id=0,count=0,
            addressed=False,banged=False,ranges=[],reason=''))
        holding = self.store.db.execute('SELECT server FROM holdings WHERE session=? AND key=? AND attached=1',(session,key)).fetchone()
        item['server'] = canonical_server(holding[0]) if holding else 'nth-trio'
        if selected:
            item['first_id'] = item['first_id'] or selected[0]['id']
            item['last_id'] = selected[-1]['id']
            item['count'] += len(selected)
            item['addressed'] |= any(bool(m.get('mentioned') or m.get('banged')) for m in selected)
            item['banged'] |= any(bool(m.get('banged')) for m in selected)
            self.merge_ranges(item['ranges'],ranges_for(key,item['server'],selected))
        if reason:
            item['reason'] = reason

    @staticmethod
    def merge_ranges(target, ranges):
        for span in ranges:
            if target and target[-1]['last']+1==span['first']:
                target[-1]['last'] = span['last']
                target[-1]['count'] += span['count']
                target[-1]['addressed'] |= span['addressed']
            else:
                target.append(dict(span))

    def transfer_buffers(self):
        for session,buffer in list(self.buffers.items()):
            for key,item in list(buffer['members'].items()):
                row = self.member(key)
                owner = row['owner_session'] if row else None
                if owner and owner!=session:
                    target = self.buffers.setdefault(owner,{'at':buffer['at'],'members':{}})
                    target['at'] = min(target['at'],buffer['at'])
                    if key in target['members']:
                        other = target['members'][key]
                        other['first_id'] = min(i for i in (other['first_id'],item['first_id']) if i) if other['count']+item['count'] else 0
                        other['last_id'] = max(other['last_id'],item['last_id'])
                        other['count'] += item['count']
                        other['addressed'] |= item['addressed']
                        other['banged'] |= item['banged']
                        other['ranges'] = sorted(other['ranges']+item['ranges'],key=lambda r:r['first'])
                        other['reason'] = other['reason'] or item['reason']
                    else:
                        target['members'][key] = item
                    item['owner_session'] = owner
                    holding = self.store.db.execute('SELECT server FROM holdings WHERE session=? AND key=? AND attached=1',(owner,key)).fetchone()
                    server = canonical_server(holding[0]) if holding else 'nth-trio'
                    moved = target['members'][key]
                    moved['server'] = server
                    for span in moved['ranges']:
                        span['server'] = server
                    del buffer['members'][key]
            if not buffer['members']:
                self.buffers.pop(session,None)

    def release(self, session, force=False, flush=False):
        with self.store.lock:
            self.transfer_buffers()
            buffer = self.buffers.get(session)
            if not buffer:
                return
            state = self.store.session(session)
            settle = .3 if state['client']=='claude' else 2.5
            if (state['state']=='in_turn' and not flush) or (not force and time.monotonic()-buffer['at']<settle):
                return
            ranges,ended,lines = [],[],[]
            for key,item in sorted(buffer['members'].items()):
                prefix = 'trio' if item['source']=='local' else 'quartet'
                server = None if state['client']=='claude' else item['server']
                if item['count'] and not item['reason']:
                    lines.append(message_notice(prefix,item['channel'],item['member_id'],item['first_id'],
                                               item['last_id'],item['count'],item['addressed'],server))
                ranges.extend(item['ranges'])
                if item['reason']:
                    lines.append(ended_notice(prefix,item['channel'],item['member_id'],item['reason'],server))
                    ended.append({'key':key,'reason':item['reason']})
            if append('would',session,state['client'],state['sink'],ranges,ended,len(lines)):
                with self.store.db:
                    for key,item in buffer['members'].items():
                        self.store.db.execute('UPDATE memberships SET shadow_notices=shadow_notices+1,shadow_ids=shadow_ids+? WHERE key=?',(item['count'],key))
                self.buffers.pop(session,None)
            # On a logging failure retain the buffer for another attempt/close.

    def poll_state(self, key, state, error='', last_ok=None):
        row = self.store.db.execute('SELECT poll_state,poll_error,last_ok FROM memberships WHERE key=?',(key,)).fetchone()
        ok = last_ok if last_ok is not None else row['last_ok']
        if (row['poll_state'],row['poll_error'],row['last_ok'])!=(state,error,ok):
            with self.store.db:
                self.store.db.execute('UPDATE memberships SET poll_state=?,poll_error=?,last_ok=? WHERE key=?',(state,error,ok,key))

    def reconcile(self):
        with self.store.lock:
            self.transfer_buffers()
            wanted = {r['key']:r for raw in self.store.snapshot()['memberships']
                      if self.eligible(r:=shadow_row(raw))}
            for key,listener in list(self.pollers.items()):
                row = wanted.get(key)
                if not row or any(row[k]!=listener.row[k] for k in ('owner_session','filter','url')):
                    listener.stop()
                    self.pollers.pop(key)
                    if not self.member(key)['ended']:
                        self.poll_state(key,'stopped')
            for key,row in wanted.items():
                if key in self.pollers:
                    listener = self.pollers[key]
                    self.poll_state(key,listener.state,listener.error,listener.last_ok)
                    continue
                try:
                    identity = _json_file(self.store.path.parent/'identities'/(key+'.json'))
                    if any(identity.get(k)!=row[k] for k in ('url','source','channel','member_id')):
                        raise ValueError('identity changed')
                    if not isinstance(identity.get('session_token'),str) or not identity['session_token']:
                        raise ValueError('identity token absent')
                    if row['source']!='local':
                        from nth_interposer_hubs import check_host, restricted_host
                        trusted = self.store.db.execute("SELECT config_url FROM hubs WHERE url=? AND trust='setup'",(row['url'],)).fetchone()
                        check_host(row['url'],allow_restricted=trusted['config_url']==row['url'] and restricted_host(row['url']))
                    listener = ShadowListener(self,row,identity)
                    self.pollers[key] = listener
                    self.poll_state(key,'starting')
                    listener.start()
                except Exception as exc:
                    self.poll_state(key,'reconnecting',type(exc).__name__)
            for session in list(self.buffers):
                self.release(session,force=self.store.session(session)['state']=='ended')

    @staticmethod
    def inbox_request(path):
        import io
        fd = os.open(path,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_FRAME:
                raise WireError('inbox entry must be a bounded regular file')
            data = stream.read(MAX_FRAME+1)
        reader = io.BytesIO(data)
        result = read_frame(reader)
        if reader.read(1):
            raise WireError('multiple inbox frames')
        return result

    def drain(self):
        from nth_interposer import dispatch
        inbox = private_dir(home()/'events'/'inbox')
        for path in inbox.glob('*.tmp'):
            try:
                if time.time()-path.lstat().st_mtime>60:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in sorted(inbox.glob('*.json')):
            try:
                request = self.inbox_request(path)
                if request['op'] not in ('hub.announce','session.register','membership.attach','membership.configure','ack.seen','turn','session.end'):
                    raise WireError('invalid inbox operation')
                dispatch(self.store,request,self,log=self.log)
            except Exception as exc:
                bad = private_dir(inbox/'bad')
                try:
                    os.replace(path,bad/path.name)
                except OSError:
                    pass  # Another drainer/removal can win; continue with remaining work.
                for old in sorted(bad.glob('*.json'))[:-100]:
                    old.unlink(missing_ok=True)
                self.log.info('inbox refused: %s',type(exc).__name__)
            else:
                path.unlink(missing_ok=True)

    def tick(self):
        now = time.monotonic()
        if now-self.last_drain>=5:
            self.last_drain = now
            self.drain()
        if now-self.last_death>=60:
            self.last_death = now
            for session in self.store.snapshot()['sessions']:
                if session['state']=='ended' or not session['host_pid']:
                    continue
                try:
                    stamp = process_stamp(session['host_pid'])
                except (OverflowError,OSError):
                    stamp = None
                if stamp is None or stamp!=session['host_stamp']:
                    self.store.end(session['session'])
        self.reconcile()

    def close(self):
        with self.store.lock:
            listeners = list(self.pollers.values())
            self.pollers.clear()
            for listener in listeners:
                listener.stop()
        for listener in listeners:
            listener.thread.join(timeout=1)
        with self.store.lock:
            self.transfer_buffers()
            for session in list(self.buffers):
                self.release(session,force=True,flush=True)
