"""Claim/ack delivery for the OpenCode SDK adapter; ambiguous claims never replay."""
import json
import time
from nth_claude_hook import process_stamp
from nth_interposer_tmux import notice
from nth_interposer_wire import WireError


def enqueue(store, session, items):
    store.db.execute('INSERT INTO deliveries(session,sink,body,ranges,state,created) VALUES (?,?,?,?,?,?)',
                     (session,'opencode',notice(items),'{}','queued',time.time()))
    return True


def dispatch(store, request):
    session = store.session(request['session'])
    if session['sink'] != 'opencode' or session['state'] == 'ended':
        raise WireError('OpenCode session is not live')
    pid = request['host_pid']
    if pid != session['host_pid'] or session['host_stamp'] != process_stamp(pid):
        raise WireError('OpenCode host lifetime does not match')
    binding = json.dumps(dict(pid=pid,stamp=session['host_stamp']))
    with store.db:
        if request['op'] == 'wait':
            store.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',('opencode_poll:'+request['session'],json.dumps(time.time())))
            if session['state'] == 'in_turn': return {'delivery':None}
            row = store.db.execute("SELECT id,body FROM deliveries WHERE session=? AND sink='opencode' AND state='queued' ORDER BY id LIMIT 1", (request['session'],)).fetchone()
            if row is None: return {'delivery':None}
            store.db.execute("UPDATE deliveries SET state='sending',ranges=? WHERE id=?",(binding,row['id']))
            return {'delivery':dict(id=row['id'],notice=row['body'])}
        row = store.db.execute("SELECT state,ranges FROM deliveries WHERE session=? AND sink='opencode' AND id=?",(request['session'],request['delivery_id'])).fetchone()
        if row is None or row['state'] not in ('sending','written','uncertain') or row['ranges'] != binding:
            raise WireError('delivery claim does not match')
        if row['state'] == 'sending':
            outcome = request['outcome']
            store.db.execute('UPDATE deliveries SET state=?,done=? WHERE id=?',(outcome,time.time(),request['delivery_id']))
            store.db.execute('UPDATE sessions SET last_wake=?,problem=? WHERE session=?',
                (time.time(),'' if outcome=='written' else 'OpenCode prompt admission uncertain; inspect session',request['session']))
        return {'recorded':True}
