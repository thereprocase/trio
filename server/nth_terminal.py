"""Owner-paired tmux control. No agent-facing tool, shell commands, or retries.

The hub stores a single bounded snapshot per binding. Commands are consumed
before returning to a bridge; a lost response is uncertain, never replayed.
Run `python nth_terminal.py --help` for private pairing and the host bridge.
"""
import argparse
import hashlib
import hmac
import json
import math
from datetime import datetime
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

MAX_SCREEN = 24000
MAX_AGE = 12
ID = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


def private_json(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError('terminal configuration must be a private regular file')
    return json.loads(path.read_text())


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)


def host_config():
    return Path(os.environ.get('NTH_TERMINAL_HOSTS', str(Path(os.environ.get(
        'NTH_HOME', str(Path.home() / '.claude/nth'))) / 'terminal-hosts.json')))


def authenticate(header, config=None):
    if not isinstance(header, str) or not header.startswith('Bearer '):
        return None
    token = header[7:]
    if len(token) != 64:
        return None
    try:
        rows = private_json(config or host_config()).get('hosts', [])
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    for row in rows:
        if hmac.compare_digest(digest, str(row.get('token_hash', ''))) and ID.fullmatch(row.get('host', '')):
            return row['host']
    return None


def connect(path):
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS terminal_sessions (
          id TEXT PRIMARY KEY, host TEXT NOT NULL, name TEXT NOT NULL,
          generation TEXT NOT NULL, snapshot TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS terminal_commands (
          id TEXT PRIMARY KEY, session TEXT NOT NULL, host TEXT NOT NULL,
          generation TEXT NOT NULL, screen_hash TEXT NOT NULL, action TEXT NOT NULL,
          params TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
          updated REAL NOT NULL, actor TEXT NOT NULL, result TEXT NOT NULL DEFAULT '');
    ''')
    return db


def validate_action(params):
    if not isinstance(params, dict):
        raise ValueError('action must be an object')
    action = params.get('action')
    if action not in ('compact', 'interrupt', 'reply'):
        raise ValueError('unsupported terminal action')
    text = params.get('text', '')
    if not isinstance(text, str) or len(text) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('terminal input must be one line, up to 2000 characters')
    pause = params.get('pause_ms', 200)
    enters = params.get('enter_count', 2 if action == 'compact' else 1)
    if type(pause) is not int or not 100 <= pause <= 1000 or type(enters) is not int or not 0 <= enters <= 2:
        raise ValueError('pause must be 100–1000 ms; Enter count must be 0–2')
    if action == 'compact':
        text = '/compact'
    return {'action': action, 'text': text, 'pause_ms': pause, 'enter_count': enters}


def queue(path, params, actor, now=None):
    now = time.time() if now is None else now
    command = validate_action(params)
    if any(not isinstance(params.get(k), str) for k in ('session','generation','screen_hash')):
        raise ValueError('session, generation and screen hash required')
    db = connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM terminal_sessions WHERE id=?', (params.get('session'),)).fetchone()
            if not row or now - row['updated'] > MAX_AGE:
                raise ValueError('terminal is offline; refresh before acting')
            snapshot = json.loads(row['snapshot'])
            # Output from a busy agent must not prevent stopping that same process.
            if (not snapshot.get('available') or params.get('generation') != row['generation']
                    or (command['action'] != 'interrupt' and params.get('screen_hash') != snapshot.get('screen_hash'))):
                raise ValueError('terminal changed; refresh before acting')
            pending = db.execute("SELECT 1 FROM terminal_commands WHERE session=? AND status IN ('queued','dispatched') AND updated>?", (row['id'], now-30)).fetchone()
            if pending:
                raise ValueError('a terminal action is already pending')
            cid = secrets.token_hex(16)
            db.execute('INSERT INTO terminal_commands (id,session,host,generation,screen_hash,action,params,status,created,updated,actor) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (cid,row['id'],row['host'],row['generation'],snapshot['screen_hash'],command['action'],json.dumps(command),'queued',now,now,actor))
        return {'id':cid,'status':'queued'}
    finally:
        db.close()


def list_sessions(path, now=None):
    now = time.time() if now is None else now
    db = connect(path)
    try:
        sessions=[]
        for row in db.execute('SELECT * FROM terminal_sessions ORDER BY name'):
            snap=json.loads(row['snapshot'])
            online=now-row['updated']<=MAX_AGE
            if not online:
                snap['screen']=''
            recent=[dict(r) for r in db.execute('SELECT id,action,status,created,updated,result,actor FROM terminal_commands WHERE session=? ORDER BY created DESC LIMIT 8',(row['id'],))]
            for cmd in recent:
                if cmd['status'] in ('queued','dispatched') and now-cmd['updated']>30:
                    cmd['status']='expired' if cmd['status']=='queued' else 'uncertain'
            sessions.append({**snap,'id':row['id'],'host':row['host'],'name':row['name'],'generation':row['generation'],'updated':row['updated'],'online':online,'commands':recent})
        return sessions
    finally:
        db.close()


def exchange(path, host, payload, now=None):
    now=time.time() if now is None else now
    if not isinstance(payload,dict):
        raise ValueError('bridge envelope must be an object')
    snapshots=payload.get('sessions',[]); results=payload.get('results',[])
    if not isinstance(snapshots,list) or len(snapshots)>32 or not isinstance(results,list) or len(results)>32:
        raise ValueError('invalid bridge envelope')
    clean=[]
    for snap in snapshots:
        if not isinstance(snap,dict) or not ID.fullmatch(str(snap.get('binding',''))):
            raise ValueError('invalid terminal binding')
        screen=snap.get('screen','')
        if not isinstance(screen,str) or len(screen)>MAX_SCREEN:
            raise ValueError('terminal snapshot too large')
        generation=snap.get('generation','')
        if not isinstance(generation,str) or len(generation)>128:
            raise ValueError('invalid session generation')
        clean.append({'binding':snap['binding'],'generation':generation,
            'name':str(snap.get('name',snap['binding']))[:100], 'member_id':str(snap.get('member_id',''))[:80], 'provider':str(snap.get('provider',''))[:20],
            'pane':str(snap.get('pane',''))[:20], 'available':snap.get('available') is True,
            'screen':screen,'screen_hash':hashlib.sha256(screen.encode()).hexdigest(),
            'usage':clean_usage(snap.get('usage')), 'problem':str(snap.get('problem',''))[:160]})
    db=connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            for snap in clean:
                sid=host+':'+snap['binding']
                db.execute('INSERT OR REPLACE INTO terminal_sessions VALUES (?,?,?,?,?,?)',
                           (sid,host,snap['name'],snap['generation'],json.dumps(snap),now))
            # Mark missing bindings offline immediately, not just at the TTL.
            present={host+':'+s['binding'] for s in clean}
            for row in db.execute('SELECT id FROM terminal_sessions WHERE host=?',(host,)).fetchall():
                if row['id'] not in present:
                    db.execute('UPDATE terminal_sessions SET updated=0 WHERE id=?',(row['id'],))
            for result in results:
                if not isinstance(result,dict) or result.get('status') not in ('sent','refused','uncertain'):
                    raise ValueError('invalid command result')
                db.execute("UPDATE terminal_commands SET status=?,result=?,params='{}',updated=? WHERE id=? AND host=? AND status='dispatched'",
                    (result['status'],str(result.get('detail',''))[:160],now,result.get('id'),host))
            db.execute("UPDATE terminal_commands SET status='expired',params='{}' WHERE status='queued' AND created<?",(now-MAX_AGE,))
            db.execute("UPDATE terminal_commands SET status='uncertain',params='{}' WHERE status='dispatched' AND updated<?",(now-30,))
            jobs=[]
            for row in db.execute("SELECT * FROM terminal_commands WHERE host=? AND status='queued' ORDER BY created LIMIT 1",(host,)):
                jobs.append({**json.loads(row['params']),'id':row['id'],'binding':row['session'].split(':',1)[1],'generation':row['generation'],'screen_hash':row['screen_hash']})
                db.execute("UPDATE terminal_commands SET status='dispatched',updated=? WHERE id=?",(now,row['id']))
        return {'commands':jobs}
    finally:
        db.close()


def clean_usage(value):
    """Only quota scalars cross the bridge; no account IDs or source file paths."""
    if not isinstance(value,dict):return {'windows':[]}
    windows=[]
    for row in value.get('windows',[])[:12] if isinstance(value.get('windows'),list) else []:
        if not isinstance(row,dict):continue
        pct=row.get('used_percentage')
        if type(pct) not in (int,float) or not 0<=pct<=100 or not math.isfinite(pct):continue
        reset=row.get('resets_at');updated=row.get('updated_at')
        windows.append({'label':str(row.get('label','Usage'))[:60], 'used_percentage':pct,
          'resets_at':reset if type(reset) in (int,float) and 0<reset<1e11 and math.isfinite(reset) else None,
          'updated_at':updated if type(updated) in (int,float) and 0<updated<1e11 and math.isfinite(updated) else None})
    return {'windows':windows,'source':str(value.get('source',''))[:40]}


def read_usage(binding):
    """Read only an explicitly selected local cache/transcript; never refresh via a model."""
    path=binding.get('usage_file');format=binding.get('usage_format')
    if not path:return {'windows':[]}
    def timestamp(value):
        if isinstance(value,str):
            try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
            except ValueError:return None
        return value
    try:
        source=Path(path)
        if not source.is_file():return {'windows':[]}
        with source.open('rb') as f:
            f.seek(0,2);size=f.tell()
            if format=='claude-statusline':
                if size>1024*1024:return {'windows':[]}
                f.seek(0);raw=json.load(f);limits=raw.get('_cached_rate_limits',{})
                rows=[]
                for key,quota in limits.items() if isinstance(limits,dict) else []:
                    if not isinstance(quota,dict) or key not in ('five_hour','seven_day','seven_day_sonnet','seven_day_opus'):continue
                    rows.append({'label':{'five_hour':'5 hour','seven_day':'Weekly','seven_day_sonnet':'Weekly · Sonnet','seven_day_opus':'Weekly · Opus'}[key],
                      'used_percentage':quota.get('used_percentage',quota.get('utilization')),
                      'resets_at':timestamp(quota.get('resets_at')),'updated_at':timestamp(quota.get('updated_at',raw.get('_cached_rate_limits_at')))})
                return clean_usage({'windows':rows,'source':'Claude statusline cache'})
            if format=='codex-session':
                start=max(0,size-512*1024);f.seek(start)
                if start:f.readline()
                lines=f.read().decode('utf-8',errors='replace').splitlines()
                for line in reversed(lines):
                    try:record=json.loads(line)
                    except ValueError:continue
                    payload=record.get('payload',{})
                    limits=payload.get('rate_limits') if isinstance(payload,dict) else None
                    if not isinstance(limits,dict):continue
                    rows=[]
                    for key in ('primary','secondary'):
                        quota=limits.get(key)
                        if not isinstance(quota,dict):continue
                        minutes=quota.get('window_minutes')
                        label='5 hour' if minutes==300 else 'Weekly' if minutes==10080 else (str(minutes)+' minute' if type(minutes) in (int,float) else key.title())
                        rows.append({'label':label,'used_percentage':quota.get('used_percent'),
                          'resets_at':timestamp(quota.get('resets_at')),'updated_at':timestamp(record.get('timestamp'))})
                    return clean_usage({'windows':rows,'source':'Codex session telemetry'})
    except (OSError,ValueError,TypeError,AttributeError):pass
    return {'windows':[]}


class Tmux:
    def __init__(self, socket=None):
        self.command=['tmux']+(['-S',socket] if socket else [])

    def run(self,*args):
        return subprocess.run([*self.command,*args],check=True,capture_output=True,text=True,timeout=3).stdout

    def identity(self,binding):
        pane=binding['pane']
        if not re.fullmatch(r'%[0-9]+',pane) or binding['provider'] not in ('claude','codex'):
            raise ValueError('invalid pane binding')
        fields=self.run('display-message','-p','-t',pane,'#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_tty}').strip().split('\t')
        if len(fields)!=4 or fields[0]!=pane or fields[2]!=binding['provider']:
            raise ValueError('pane is not running the bound agent')
        pane_stat=Path('/proc/'+fields[1]+'/stat').read_text()
        foreground=int(pane_stat[pane_stat.rfind(')')+2:].split()[5])
        if foreground <= 0:
            raise ValueError('pane has no foreground process group')
        stat=Path('/proc/'+str(foreground)+'/stat').read_text()
        # PID + kernel start time survive neither replacement nor PID reuse.
        start=stat[stat.rfind(')')+2:].split()[19]
        return hashlib.sha256(('\0'.join(fields[:3])+str(foreground)+':'+start).encode()).hexdigest()

    def snapshot(self,binding):
        result={k:binding[k] for k in ('binding','name','pane','provider')}
        result['member_id']=binding.get('member_id','')
        result['usage']=read_usage(binding)
        try:
            generation=self.identity(binding)
            if generation != binding.get('generation'):
                raise ValueError('bound agent process was replaced; pair again')
            screen=self.run('capture-pane','-p','-t',binding['pane'])[-MAX_SCREEN:]
            if self.identity(binding)!=generation:
                raise ValueError('pane changed while reading')
            result.update(available=True,generation=generation,screen=screen,screen_hash=hashlib.sha256(screen.encode()).hexdigest())
        except (OSError,ValueError,subprocess.SubprocessError):
            result.update(available=False,generation='',screen='',problem='Pane unavailable or agent process changed')
        return result

    def execute(self,binding,job):
        sent=False
        try:
            params=validate_action(job)
            snap=self.snapshot(binding)
            if (not snap.get('available') or snap['generation']!=job['generation']
                    or (params['action']!='interrupt' and snap['screen_hash']!=job['screen_hash'])):
                return {'id':job['id'],'status':'refused','detail':'Pane or screen changed; refresh and try again'}
            def keys(*args):
                nonlocal sent
                if self.identity(binding)!=job['generation']:
                    raise ValueError('agent process changed')
                sent=True  # a subprocess timeout can mean keys reached tmux
                self.run('send-keys','-t',binding['pane'],*args)
            if params['action']=='interrupt':
                keys('C-c')
            else:
                if params['text']:keys('-l','--',params['text'])
                for _ in range(params['enter_count']):
                    time.sleep(params['pause_ms']/1000)
                    keys('Enter')
            return {'id':job['id'],'status':'sent','detail':'Keys sent; inspect the terminal for the outcome'}
        except (OSError,ValueError,KeyError,subprocess.SubprocessError):
            return {'id':job.get('id',''),'status':'uncertain' if sent else 'refused','detail':'Action interrupted; not retried'}


def bridge(config):
    cfg=private_json(config)
    u=urlsplit(cfg['url'])
    if u.scheme!='https' or u.username or u.password or u.query or u.fragment:
        raise ValueError('bridge requires a trusted HTTPS hub URL')
    adapter=Tmux(cfg.get('socket'))
    bindings={b['binding']:b for b in cfg['bindings']}
    if len(bindings)!=len(cfg['bindings']) or not 1 <= len(bindings) <= 32:
        raise ValueError('unique explicit bindings required')
    results=[]
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(NoRedirect)
    while True:
        payload={'sessions':[adapter.snapshot(b) for b in bindings.values()],'results':results}
        req=urllib.request.Request(cfg['url'].rstrip('/')+'/api/terminals/exchange',json.dumps(payload).encode(),
             headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json','User-Agent':'Quartet-Terminal-Bridge'})
        try:
            with opener.open(req,timeout=10) as r:data=json.load(r)
            results=[]
            for job in data.get('commands',[]):
                b=bindings.get(job.get('binding'))
                results.append(adapter.execute(b,job) if b else {'id':job.get('id',''),'status':'refused','detail':'Unknown binding'})
        except (OSError,ValueError):
            # Keep outcomes for acknowledgement, but never retry key execution.
            pass
        time.sleep(1)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--config',required=True)
    bind=sub.add_parser('bind');bind.add_argument('--config',required=True)
    bind.add_argument('--member-id',required=True);bind.add_argument('--binding',required=True);bind.add_argument('--name',required=True)
    bind.add_argument('--pane',required=True);bind.add_argument('--provider',choices=['claude','codex'],required=True)
    pair=sub.add_parser('pair');pair.add_argument('--host',required=True);pair.add_argument('--url',required=True)
    pair.add_argument('--hub-file',required=True);pair.add_argument('--spoke-file',required=True)
    pair.add_argument('--member-id',required=True);pair.add_argument('--binding',required=True);pair.add_argument('--name',required=True);pair.add_argument('--pane',required=True)
    pair.add_argument('--provider',choices=['claude','codex'],required=True);pair.add_argument('--socket')
    for command in (pair,bind):
        command.add_argument('--usage-file');command.add_argument('--usage-format',choices=['claude-statusline','codex-session'])
    args=parser.parse_args(argv)
    if args.command=='run':return bridge(args.config)
    if bool(args.usage_file)!=bool(args.usage_format):parser.error('usage file and format must be supplied together')
    if not ID.fullmatch(args.member_id):parser.error('invalid Quartet member ID')
    if args.command=='bind':
        cfg=private_json(args.config)
        if not ID.fullmatch(args.binding):parser.error('invalid binding')
        binding={'binding':args.binding,'name':args.name,'pane':args.pane,'provider':args.provider,'member_id':args.member_id,'usage_file':args.usage_file,'usage_format':args.usage_format}
        binding['generation']=Tmux(cfg.get('socket')).identity(binding)
        cfg['bindings']=[b for b in cfg['bindings'] if b['binding']!=args.binding]+[binding]
        tmp=Path(args.config).with_name(Path(args.config).name+'.'+secrets.token_hex(4))
        save_private(tmp,cfg);os.replace(tmp,args.config)
        print('Binding saved. Restart the bridge to load the explicit binding.')
        return
    if not ID.fullmatch(args.host) or not ID.fullmatch(args.binding) or not re.fullmatch(r'%[0-9]+',args.pane):
        parser.error('invalid host, binding or pane')
    binding={'binding':args.binding,'name':args.name,'pane':args.pane,'provider':args.provider,'member_id':args.member_id,'usage_file':args.usage_file,'usage_format':args.usage_format}
    binding['generation']=Tmux(args.socket).identity(binding)
    token=secrets.token_hex(32)
    save_private(args.hub_file,{'hosts':[{'host':args.host,'token_hash':hashlib.sha256(token.encode()).hexdigest()}]})
    save_private(args.spoke_file,{'url':args.url,'token':token,'socket':args.socket,'bindings':[binding]})
    print('Private pairing files written. Transfer each only to its matching host; no credentials printed.')


if __name__=='__main__':main()
