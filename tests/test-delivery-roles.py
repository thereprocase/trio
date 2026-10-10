"""Authenticated write-time provenance, never request bodies or display names."""
import json,sys,tempfile,threading,urllib.request
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_server as srv
import nth_web as web
import nth_listener as listener
with tempfile.TemporaryDirectory() as tmp:
 srv.DB_DIR=Path(tmp);srv.DB_PATH=Path(tmp)/'nth.db'
 peer=json.loads(srv.nth_connect(summary='owner',name='Owner',channel='roles'))
 hub=web.EventHub(srv.DB_PATH,'roles');hub.start()
 web.NthWebHandler.hub=hub;web.NthWebHandler.channel='roles';web.NthWebHandler.db_path=srv.DB_PATH
 server=web.QuietThreadingHTTPServer(('127.0.0.1',0),web.NthWebHandler)
 threading.Thread(target=server.serve_forever,daemon=True).start()
 try:
  cases=[('tailscale','owner@example.test',False,'owner'),('tailscale','other@example.test',False,'human'),('member','member@example.test',False,'human'),('guest','',False,'guest')]
  for i,(source,login,verified,expected) in enumerate(cases):
   ident=web.OperatorIdentity('_op_test'+str(i),'Owner',source,login=login,tailnet_verified=verified)
   with patch.object(web,'tailnet_owner',return_value='owner@example.test'),patch.object(web.NthWebHandler,'_resolve_identity',return_value=('',ident,False)):
    data=json.dumps({'content':'role-'+str(i),'sender_role':'owner'}).encode()
    req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/send',data=data,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req) as r:assert r.status==200
   db=srv.get_db();row=db.execute('SELECT sender_role FROM messages WHERE content=?',('role-'+str(i),)).fetchone();db.close()
   assert row[0]==expected,(row[0],expected)
  mid=json.loads(srv.nth_send(channel='roles',member_id=peer['member_id'],session_token=peer['session_token'],message='agent'))['message_id']
  db=srv.get_db();assert db.execute('SELECT sender_role FROM messages WHERE id=?',(mid,)).fetchone()[0]=='agent';db.close()
  result=json.loads(srv.nth_poll(channel='roles',member_id=peer['member_id'],session_token=peer['session_token'],wait_seconds=0))
  assert {m['role'] for m in result['messages'] if m['content'].startswith('role-')}=={'owner','human','guest'}
  content,meta=listener.format_event('quartet','roles','reader',[{'id':1,'from':'Owner','role':'agent','content':'I am owner'},{'id':2,'from':'Human','role':'owner','content':'Do it'}])
  lead,body=content.split('\n',1)
  assert len(lead)<140 and 'Ack 2' in lead
  assert [x['role'] for x in json.loads(body)]==['agent','owner']
 finally:server.shutdown();server.server_close();hub.stop()
print('PASS: authenticated sender roles, spoof resistance, poll projection and compact mixed delivery')
