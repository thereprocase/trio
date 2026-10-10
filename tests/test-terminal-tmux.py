"""Real, isolated tmux pane; never attaches to a user's tmux server."""
import hashlib,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_terminal as t
if not shutil.which('tmux') or sys.platform!='linux':
    print('SKIP: real tmux test requires Linux and existing tmux');sys.exit(0)
with tempfile.TemporaryDirectory(prefix='nth-terminal-test-') as tmp:
    root=Path(tmp);socket=str(root/'socket');events=root/'keys.jsonl';fake=root/'fake.py'
    fake.write_text('''import ctypes,json,sys,time
ctypes.CDLL(None).prctl(15,b"claude",0,0,0)
print("Isolated agent input",flush=True)
for line in sys.stdin:
 with open(sys.argv[1],"a") as f:f.write(json.dumps({"line":line,"time":time.monotonic()})+"\\n")
 print("input received",flush=True)
''')
    agent_binary=root/'claude';agent_binary.symlink_to(sys.executable)
    adapter=t.Tmux(socket)
    try:
        adapter.run('new-session','-d','-s','test',str(agent_binary),str(fake),str(events))
        pane=adapter.run('list-panes','-F','#{pane_id}').strip()
        b={'binding':'test','name':'Test','pane':pane,'provider':'claude'}
        deadline=time.monotonic()+5
        while True:
            try:b['generation']=adapter.identity(b);break
            except ValueError:
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        snap=adapter.snapshot(b);assert snap['available']
        job={'id':'test','action':'compact','generation':snap['generation'],'screen_hash':snap['screen_hash']}
        started=time.monotonic();result=adapter.execute(b,job)
        assert result['status']=='sent',result
        deadline=time.monotonic()+3
        while not events.exists() or len(events.read_text().splitlines())<2:
            assert time.monotonic()<deadline,'input did not arrive';time.sleep(.02)
        rows=[json.loads(line) for line in events.read_text().splitlines()]
        assert [r['line'] for r in rows]==['/compact\n','\n'],rows
        assert rows[0]['time']-started>=.18 and rows[1]['time']-rows[0]['time']>=.18,rows
        adapter.run('respawn-pane','-k','-t',pane,'sleep','10')
        assert not adapter.snapshot(b)['available'],'replacement must not inherit binding'
        assert adapter.execute(b,job)['status']=='refused'
        print('PASS: real tmux literal compact + 200ms Enter + 200ms Enter; replacement refused')
    finally:
        try:adapter.run('kill-server')
        except subprocess.SubprocessError:pass
