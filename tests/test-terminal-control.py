"""Paired terminal command authorization, stale checks and at-most-once dispatch."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_terminal as t

def rejects(fn):
    try:fn()
    except ValueError:return
    raise AssertionError('expected refusal')

with tempfile.TemporaryDirectory() as tmp:
    db=Path(tmp)/'state.db'; config=Path(tmp)/'hosts.json'; token='a'*64
    t.save_private(config,{'hosts':[{'host':'local','token_hash':hashlib.sha256(token.encode()).hexdigest()}]})
    assert t.authenticate('Bearer '+token,config)=='local'
    assert t.authenticate('Bearer '+'b'*64,config) is None
    config.chmod(0o644);assert t.authenticate('Bearer '+token,config) is None;config.chmod(0o600)
    screen='Agent prompt'
    snap={'binding':'worker','name':'Worker','provider':'claude','pane':'%1','available':True,'generation':'g','screen':screen}
    assert t.exchange(db,'local',{'sessions':[snap]},now=100)=={'commands':[]}
    params={'session':'local:worker','generation':'g','screen_hash':hashlib.sha256(screen.encode()).hexdigest(),'action':'compact'}
    rejects(lambda:t.queue(db,{**params,'screen_hash':'stale'},'owner',now=101))
    rejects(lambda:t.queue(db,{**params,'generation':'replacement'},'owner',now=101))
    rejects(lambda:t.queue(db,params,'owner',now=120))
    job=t.queue(db,params,'owner',now=101)
    rejects(lambda:t.queue(db,params,'owner',now=102))
    assert not t.exchange(db,'different',{'sessions':[]},now=102)['commands']
    jobs=t.exchange(db,'local',{'sessions':[snap]},now=102)['commands'];assert len(jobs)==1
    assert jobs[0]['text']=='/compact' and jobs[0]['pause_ms']==200 and jobs[0]['enter_count']==2
    assert not t.exchange(db,'local',{'sessions':[snap]},now=103)['commands'],'no replay after ambiguous response'
    t.exchange(db,'local',{'sessions':[snap],'results':[{'id':job['id'],'status':'sent','detail':'Keys sent'}]},now=104)
    assert t.list_sessions(db,now=104)[0]['commands'][0]['status']=='sent'
    assert t.list_sessions(db,now=120)[0]['screen']==''
    t.queue(db,params,'owner',now=105)
    assert not t.exchange(db,'local',{'sessions':[snap]},now=130)['commands'],'expired command not sent'
    t.exchange(db,'local',{'sessions':[]},now=131)
    rejects(lambda:t.queue(db,params,'owner',now=132))
    for bad in [[],{'action':'exec'},{'action':'reply','text':'line\ncommand'},{'action':'reply','pause_ms':True},{'action':'reply','enter_count':99}]:
        rejects(lambda:t.validate_action(bad))
    rejects(lambda:t.exchange(db,'local',[],now=132))

class Fake(t.Tmux):
    def __init__(self):self.calls=[];self.n=0
    def snapshot(self,b):return {'available':True,'generation':'g','screen_hash':'h'}
    def identity(self,b):self.n+=1;return 'g'
    def run(self,*args):self.calls.append(args);return ''
f=Fake();job={'id':'j','generation':'g','screen_hash':'h','action':'compact'}
with patch.object(t.time,'sleep') as sleep:
    result=f.execute({'pane':'%1'},job)
assert result['status']=='sent'
assert f.calls==[('send-keys','-t','%1','-l','--','/compact'),('send-keys','-t','%1','Enter'),('send-keys','-t','%1','Enter')]
assert [c.args for c in sleep.call_args_list]==[(.2,),(.2,)]
f=Fake()
f.identity=lambda b:'other'
assert f.execute({'pane':'%1'},job)['status']=='refused' and not f.calls
f=Fake()
def identity(b):
    f.n+=1
    return 'g' if f.n==1 else 'changed'
f.identity=identity
with patch.object(t.time,'sleep'):
    assert f.execute({'pane':'%1'},job)['status']=='uncertain'
assert len(f.calls)==1,'no Enter after process replacement'
print('PASS: authentication, host scope, stale screens, expiry, no replay, exact timed keys, process replacement')
