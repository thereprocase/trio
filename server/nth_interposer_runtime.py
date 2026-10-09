"""Shadow-only listeners; observation and its high water share one locked update."""
import os
import json
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy

from nth_claude_hook import poll_factory, process_stamp
from nth_interposer_wire import home, private_dir, read_frame, WireError, MAX_FRAME, canonical_server
from nth_interposer_store import _json_file
from nth_listener import Listener, classify_poll, select_messages, OK
from nth_notice import message_notice, ended_notice, shown_reason, MAX_INTEGER
from nth_interposer_shadow import append, ranges_for, project_record

PENDING_PREFIX = 'shadow_pending:'
FINAL_FLUSH_ATTEMPTS = 3
MAX_STARTUPS = 4
START_RETRY_SECONDS = .5
START_RETRY_MAX = 30
START_FIELDS = ('source','url','channel','member_id','owner_session','filter','enabled','ended')


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
            if self.runtime.closing:
                return []
            row = self.runtime.member(self.row['key'])
            if not self.runtime.eligible(row):
                return []
            self.high_water = max(self.high_water,row['shadow_announced_through'])
            fresh = [m for m in super()._fresh(messages) if m['id']<MAX_INTEGER]
            selected = select_messages({'messages':fresh},row['filter'])
            if fresh:
                # Never commit seen IDs before they have an owner buffer. A handoff
                # after this lock moves that buffer, rather than discarding delivery.
                with self.runtime.buffer_transaction():
                    self.runtime.accumulate(row,selected)
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
            with self.runtime.buffer_transaction():
                self.runtime.accumulate(row,[],reason)
                self.runtime.store.db.execute("UPDATE memberships SET shadow_ended=?,poll_state='ended' WHERE key=?",(reason,row['key']))


class Runtime:
    def __init__(self, store, log, factory=None):
        self.store,self.log = store,log
        self.factory = factory or self.poll_factory
        self.pollers,self.buffers = {},store.pending_buffers
        self.last_drain = self.last_death = 0.0
        self.closing = False
        self.pending_keys = store.pending_evidence_keys
        self.startups, self.start_retry = {}, {}
        self.startup_threads = set()
        self.startup_slots = threading.BoundedSemaphore(MAX_STARTUPS)
        self.inbox_path = home()/'events'/'inbox'
        self.inbox_thread = None
        self.recover()

    def recover(self):
        """Replay only projected evidence, without requiring a live owner or hub."""
        with self.store.lock:
            for row in self.store.db.execute(
                    'SELECT key,value FROM meta WHERE key LIKE ?', (PENDING_PREFIX+'%',)).fetchall():
                if row['key'] in self.pending_keys.values():
                    continue  # This process still owns the corresponding live buffer.
                try:
                    record = json.loads(row['value'])
                    fields = ('session','client','sink','ranges','ended','lines')
                    buffered = record.get('buffered',False)
                    extra = {'buffered','buffered_at_ns'} if buffered else set()
                    if (set(record)!=set(fields)|extra or not row['key'].startswith(PENDING_PREFIX+record['session']+':')
                            or (buffered and (buffered is not True or type(record['buffered_at_ns']) is not int
                                              or record['buffered_at_ns']<0))):
                        raise ValueError
                    projected = {k:record[k] for k in fields}
                    clean = project_record('would', **projected)
                    if not clean:
                        continue
                    if buffered:
                        self.restore_buffer(row['key'],clean,record['buffered_at_ns'])
                    elif append('would', **projected):
                        with self.store.db:
                            self.count_record(clean)
                            self.store.db.execute('DELETE FROM meta WHERE key=?', (row['key'],))
                except (ValueError,TypeError,KeyError):
                    self.log.warning('shadow recovery refused: invalid record')

    def restore_buffer(self, journal_key, record, at_ns):
        session = record['session']
        self.store.session(session)  # Durable pending rows protect this metadata.
        grouped = {}
        for span in record['ranges']:
            grouped.setdefault(span['key'],[]).append(dict(span))
        ended = {item['key']:item['reason'] for item in record['ended']}
        now = time.monotonic()
        with self.buffer_transaction():
            buffer = self.buffers.setdefault(session,dict(at=min(now,at_ns/1e9),members={}))
            buffer['at'] = min(buffer['at'],now,at_ns/1e9)
            for key in grouped.keys()|ended.keys():
                member = self.member(key)
                if member is None:
                    raise ValueError('pending membership missing')
                spans = grouped.get(key,[])
                if key in buffer['members']:
                    raise ValueError('duplicate buffered snapshot')
                holding = self.store.db.execute('SELECT server FROM holdings WHERE session=? AND key=?',
                                               (session,key)).fetchone()
                buffer['members'][key] = dict(member,owner_session=session,
                    server=spans[0]['server'] if spans else canonical_server(holding[0] if holding else 'nth-trio'),
                    ranges=spans,first_id=min((r['first'] for r in spans),default=0),
                    last_id=max((r['last'] for r in spans),default=0),count=sum(r['count'] for r in spans),
                    addressed=any(r['addressed'] for r in spans),banged=False,reason=ended.get(key,''))
            self.pending_keys[session] = journal_key

    @contextmanager
    def buffer_transaction(self):
        """Memory rollback and projected journal/cursor writes commit together."""
        with self.store.lock:
            previous, keys = deepcopy(self.buffers),dict(self.pending_keys)
            try:
                with self.store.db:
                    yield
                    self.persist_buffers()
            except BaseException:
                self.buffers.clear()
                self.buffers.update(previous)
                self.pending_keys.clear()
                self.pending_keys.update(keys)
                raise

    def persist_buffer(self, session, record, buffered):
        pending = {k:record[k] for k in ('session','client','sink','ranges','ended','lines')}
        if buffered:
            pending.update(buffered=True,buffered_at_ns=max(0,int(self.buffers[session]['at']*1e9)))
        key = self.pending_keys.setdefault(session,PENDING_PREFIX+session+':'+uuid.uuid4().hex)
        self.store.db.execute('''INSERT INTO meta(key,value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE meta.value!=excluded.value''',
            (key,json.dumps(pending,allow_nan=False)))

    def persist_buffers(self):
        for session in self.buffers:
            record = self.buffer_record(session)
            if not record:
                raise ValueError('invalid buffered evidence')
            self.persist_buffer(session,record,buffered=not self.closing)
        # Write the new owner's snapshot before deleting an old owner's entry.
        for session in list(self.pending_keys):
            if session not in self.buffers:
                self.store.db.execute('DELETE FROM meta WHERE key=?',(self.pending_keys.pop(session),))

    def count_record(self, record):
        counts = {}
        for span in record['ranges']:
            counts[span['key']] = counts.get(span['key'],0) + span['count']
        for ended in record['ended']:
            counts.setdefault(ended['key'],0)
        for key,count in counts.items():
            self.store.db.execute('UPDATE memberships SET shadow_notices=shadow_notices+1,shadow_ids=shadow_ids+? WHERE key=?',
                                  (count,key))

    def poll_factory(self, identity):
        if identity['source']=='local':
            return poll_factory(identity)
        from nth_listener import quartet_poll_factory
        from nth_interposer_hubs import connection_guard, restricted_host
        with self.store.lock:
            if self.closing:
                raise WireError('interposer is closing', 'service_closing')
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
        if self.closing or os.environ.get('TRIO_INTERPOSER_SHADOW')=='0' or not row:
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
        with self.buffer_transaction():
            self._transfer_buffers()

    def _transfer_buffers(self):
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

    def buffer_record(self, session, render=False):
        buffer, state = self.buffers[session],self.store.session(session)
        ranges,ended,lines = [],[],0
        for key,item in sorted(buffer['members'].items()):
            prefix = 'trio' if item['source']=='local' else 'quartet'
            server = None if state['client']=='claude' else item['server']
            if item['count'] and not item['reason']:
                lines += 1
                if render:
                    message_notice(prefix,item['channel'],item['member_id'],item['first_id'],
                                   item['last_id'],item['count'],item['addressed'],server)
            ranges.extend(item['ranges'])
            if item['reason']:
                lines += 1
                if render:
                    ended_notice(prefix,item['channel'],item['member_id'],item['reason'],server)
                ended.append({'key':key,'reason':item['reason']})
        return project_record('would',session,state['client'],state['sink'],ranges,ended,lines)

    def release(self, session, force=False, flush=False):
        with self.store.lock:
            self.transfer_buffers()
            buffer = self.buffers.get(session)
            if not buffer:
                return True
            state = self.store.session(session)
            settle = .3 if state['client']=='claude' else 2.5
            if (state['state']=='in_turn' and not flush) or (not force and time.monotonic()-buffer['at']<settle):
                return False
            record = self.buffer_record(session,render=True)
            if not record:
                return False
            with self.store.db:
                self.persist_buffer(session,record,buffered=False)
            projected = {k:record[k] for k in ('session','client','sink','ranges','ended','lines')}
            if append('would', **projected):
                with self.store.db:
                    self.count_record(record)
                    if session in self.pending_keys:
                        self.store.db.execute('DELETE FROM meta WHERE key=?',(self.pending_keys[session],))
                self.buffers.pop(session,None)
                self.pending_keys.pop(session,None)
                return True
            # On a logging failure retain the buffer for another attempt/close.
            return False

    def poll_state(self, key, state, error='', last_ok=None):
        row = self.store.db.execute('SELECT poll_state,poll_error,last_ok FROM memberships WHERE key=?',(key,)).fetchone()
        ok = last_ok if last_ok is not None else row['last_ok']
        if (row['poll_state'],row['poll_error'],row['last_ok'])!=(state,error,ok):
            with self.store.db:
                self.store.db.execute('UPDATE memberships SET poll_state=?,poll_error=?,last_ok=? WHERE key=?',(state,error,ok,key))

    def start_signature(self, row):
        """Called under the lock; includes owner registration and attached holding."""
        state = self.store.session(row['owner_session'])
        holding = self.store.db.execute('SELECT server,joined,attached FROM holdings WHERE session=? AND key=?',
                                       (row['owner_session'],row['key'])).fetchone()
        trust = tuple(tuple(r) for r in self.store.db.execute(
            "SELECT server,url,trust,config_url,approved FROM hubs WHERE url=? AND trust='setup' ORDER BY server",
            (row['url'],))) if row['source']!='local' else ()
        return (tuple(row[k] for k in START_FIELDS),
                tuple(state[k] for k in ('registered','host_pid','host_stamp','client','sink')),
                tuple(holding), trust)

    def start_failed(self, job, exc):
        with self.store.lock:
            if self.closing or job['cancelled'].is_set() or self.startups.get(job['row']['key']) is not job:
                return
            row = self.member(job['row']['key'])
            if not self.eligible(row) or self.start_signature(row)!=job['signature']:
                return
            previous = self.start_retry.get(row['key'])
            failures = (previous[2] if previous and previous[0]==job['signature'] else 0)+1
            delay = min(START_RETRY_MAX, START_RETRY_SECONDS * 2**min(failures-1,16))
            self.start_retry[row['key']] = (job['signature'],time.monotonic()+delay,failures)
            self.poll_state(row['key'],'reconnecting',type(exc).__name__)

    def start_current(self, job, path, stamp):
        """Under store.lock, refuse stale work before construction and before start."""
        key = job['row']['key']
        if self.closing or job['cancelled'].is_set() or self.startups.get(key) is not job:
            return None
        current = self.member(key)
        if (not self.eligible(current) or self.start_signature(current)!=job['signature']
                or self.identity_stamp(path)!=stamp or key in self.pollers):
            return None
        return current

    def start_member(self, job):
        """DNS, identity reads and factory construction run outside the service loop/lock."""
        row, listener, accepted = job['row'], None, False
        try:
            if job['cancelled'].is_set():
                return
            path = self.store.path.parent/'identities'/(row['key']+'.json')
            stamp = self.identity_stamp(path)
            identity = _json_file(path)
            if self.identity_stamp(path)!=stamp:
                raise ValueError('identity changed during read')
            if any(identity.get(k)!=row[k] for k in ('url','source','channel','member_id')):
                raise ValueError('identity changed')
            if not isinstance(identity.get('session_token'),str) or not identity['session_token']:
                raise ValueError('identity token absent')
            if job['cancelled'].is_set():
                return
            if row['source']!='local':
                from nth_interposer_hubs import check_host, restricted_host
                allow = any(trusted[3]==row['url'] for trusted in job['signature'][3])
                check_host(row['url'],allow_restricted=allow and restricted_host(row['url']))
            if job['cancelled'].is_set():
                return
            with self.store.lock:
                current = self.start_current(job,path,stamp)
                if current is None:
                    return
            listener = ShadowListener(self,current,identity)
            if self.identity_stamp(path)!=stamp or _json_file(path)!=identity or self.identity_stamp(path)!=stamp:
                raise ValueError('identity changed during validation')
            with self.store.lock:
                # A late worker must not read a closed Store or resurrect a stop.
                current = self.start_current(job,path,stamp)
                if current is None:
                    return
                listener.row = current
                listener.high_water = max(listener.high_water,current['shadow_announced_through'])
                self.pollers[row['key']] = listener
                self.poll_state(row['key'],'starting')
                listener.start()
                accepted = True
                self.start_retry.pop(row['key'],None)
        except Exception as exc:
            with self.store.lock:
                if listener and self.pollers.get(row['key']) is listener:
                    self.pollers.pop(row['key'],None)
            self.start_failed(job,exc)
        finally:
            if listener and not accepted:
                listener.stop()
            with self.store.lock:
                if self.startups.get(row['key']) is job:
                    self.startups.pop(row['key'],None)
                self.startup_threads.discard(job['thread'])
            job['slots'].release()

    @staticmethod
    def identity_stamp(path):
        info = path.lstat()
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)

    def queue_start(self, row, signature):
        if not self.startup_slots.acquire(blocking=False):
            return False
        job = dict(row=dict(row),signature=signature,cancelled=threading.Event(),slots=self.startup_slots)
        self.startups[row['key']] = job
        try:
            self.poll_state(row['key'],'starting')
            thread = threading.Thread(target=self.start_member,args=(job,),name='shadow-startup',daemon=True)
            job['thread'] = thread
            self.startup_threads.add(thread)
            thread.start()
        except Exception as exc:
            self.start_failed(job,exc)
            self.startups.pop(row['key'],None)
            if 'thread' in job:
                self.startup_threads.discard(job['thread'])
            self.startup_slots.release()
        return True

    def reconcile(self):
        with self.store.lock:
            if self.closing:
                return
            self.recover()
            self.transfer_buffers()
            wanted = {r['key']:r for raw in self.store.snapshot()['memberships']
                      if self.eligible(r:=shadow_row(raw))}
            for key,job in list(self.startups.items()):
                row = wanted.get(key)
                if not row or self.start_signature(row)!=job['signature']:
                    job['cancelled'].set()
                    self.startups.pop(key,None)
            for key in list(self.start_retry):
                if key not in wanted:
                    self.start_retry.pop(key,None)
            for key,listener in list(self.pollers.items()):
                row = wanted.get(key)
                if not row or any(row[k]!=listener.row[k] for k in ('owner_session','filter','url')):
                    listener.stop()
                    self.pollers.pop(key)
                    if not self.member(key)['ended']:
                        self.poll_state(key,'stopped')
            queued = 0
            for key,row in wanted.items():
                if key in self.pollers:
                    listener = self.pollers[key]
                    self.poll_state(key,listener.state,listener.error,listener.last_ok)
                    continue
                if key in self.startups:
                    continue
                signature = self.start_signature(row)
                retry = self.start_retry.get(key)
                if retry and retry[0]==signature and time.monotonic()<retry[1]:
                    continue
                if queued>=MAX_STARTUPS or not self.queue_start(row,signature):
                    break
                queued += 1
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
        """Kick one ordered worker; service startup/tick must never wait on DNS."""
        if not self.inbox_path.is_dir():
            return
        with self.store.lock:
            if self.closing or self.inbox_busy():
                return
            worker = threading.Thread(target=self._drain,name='shadow-inbox',daemon=True)
            self.inbox_thread = worker
            try:
                worker.start()
            except Exception as exc:
                self.inbox_thread = None
                self.log.info('inbox worker failed: %s',type(exc).__name__)

    def inbox_busy(self):
        return self.inbox_thread is not None and self.inbox_thread.is_alive()

    def _drain(self):
        from nth_interposer import dispatch
        with self.store.lock:
            if self.closing:
                return
        inbox = private_dir(self.inbox_path)
        for path in inbox.glob('*.tmp'):
            try:
                if time.time()-path.lstat().st_mtime>60:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        for path in sorted(inbox.glob('*.json')):
            with self.store.lock:
                if self.closing:
                    return  # Unapplied files belong to the next service instance.
            try:
                request = self.inbox_request(path)
                if request['op'] not in ('hub.announce','session.register','membership.attach','membership.configure','ack.seen','turn','session.end'):
                    raise WireError('invalid inbox operation')
                dispatch(self.store,request,self,log=self.log)
            except Exception as exc:
                with self.store.lock:
                    if self.closing:
                        return
                    bad = private_dir(inbox/'bad')
                    try:
                        os.replace(path,bad/path.name)
                    except OSError:
                        pass  # Another drainer/removal can win; continue with remaining work.
                    for old in sorted(bad.glob('*.json'))[:-100]:
                        old.unlink(missing_ok=True)
                    self.log.info('inbox refused: %s',type(exc).__name__)
            else:
                with self.store.lock:
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
                    with self.store.lock:
                        current = self.store.session(session['session'])
                        # Registration may replace even the same PID/stamp while
                        # the slow OS check runs outside the writer lock.
                        if not self.closing and all(current[k]==session[k] for k in
                                ('host_pid','host_stamp','registered')):
                            self.store.end(session['session'])
        self.reconcile()

    def close(self):
        with self.store.lock:
            self.closing = True
            for job in self.startups.values():
                job['cancelled'].set()
            self.startups.clear()
            self.start_retry.clear()
            listeners = list(self.pollers.values())
            self.pollers.clear()
            for listener in listeners:
                listener.stop()
        for listener in listeners:
            listener.thread.join(timeout=1)
        with self.store.lock:
            self.transfer_buffers()
            for attempt in range(FINAL_FLUSH_ATTEMPTS):
                self.recover()
                for session in list(self.buffers):
                    self.release(session,force=True,flush=True)
                if not self.buffers:
                    break
                if attempt+1 < FINAL_FLUSH_ATTEMPTS:
                    time.sleep(.05)
            return not self.buffers and not self.store.db.execute(
                'SELECT 1 FROM meta WHERE key LIKE ?', (PENDING_PREFIX+'%',)).fetchone()
