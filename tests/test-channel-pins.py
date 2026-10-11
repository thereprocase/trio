"""Pin API: channel-scoped broadcasts, owner writes and stale-update rejection."""
import json,sys,tempfile,threading,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'server'))
import nth_server as srv
import nth_web as web
with tempfile.TemporaryDirectory() as tmp:
 srv.DB_DIR=Path(tmp);srv.DB_PATH=Path(tmp)/'nth.db'
 a=json.loads(srv.nth_connect(summary='test',name='Alpha',channel='alpha'))
 b=json.loads(srv.nth_connect(summary='test',name='Beta',channel='beta'))
 for row in [a,b]:srv.nth_send(channel=row['channel'],member_id=row['member_id'],message='reference')
 db=srv.get_db();web.ensure_ask_columns(db)
 first=db.execute("SELECT MAX(id) FROM messages WHERE channel='alpha'").fetchone()[0]
 other=db.execute("SELECT MAX(id) FROM messages WHERE channel='beta'").fetchone()[0]
 db.execute("INSERT INTO messages(channel,member_id,member_name,content,created_at,recipients) VALUES('alpha',?,'Alpha','private',?,?)",(a['member_id'],web.now_iso(),'["secret"]'))
 private=db.execute('SELECT last_insert_rowid()').fetchone()[0]
 db.execute("INSERT INTO attachments(channel,message_id,member_id,mime,filename,path,created_at) VALUES('alpha',?,?,'image/png','reference.png','/synthetic/reference.png',?)",(first,a['member_id'],web.now_iso()))
 db.execute("INSERT INTO pages(id,channel,message_id,member_id,title,html,bytes,created_at,expires_at) VALUES('abcdefghijklmnopqrstuv','alpha',?,?,'Reference page','<p>test</p>',11,?,?)",(first,a['member_id'],web.now_iso(),'2099-01-01T00:00:00Z'))
 db.commit();db.close()
 handler=type('PinHandler',(web.NthWebHandler,),{'db_path':srv.DB_PATH,'landing_mode':True})
 server=web.QuietThreadingHTTPServer(('127.0.0.1',0),handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 def call(body=None):
  req=urllib.request.Request('http://127.0.0.1:%s/api/channel-pin?channel=alpha'%server.server_port,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 try:
  assert call()[1]['pins']==[]
  assert call({'message_ids':[first],'expected_ids':[]})[0]==200
  assert call()[1]['pins'][0]['id']==first
  assert call()[1]['pins'][0]['attachments'][0]['filename']=='reference.png'
  assert call()[1]['pins'][0]['page']['path']=='/pages/abcdefghijklmnopqrstuv'
  assert call({'message_ids':[first,other],'expected_ids':[first]})[0]==404
  assert call({'message_ids':[private],'expected_ids':[first]})[0]==404
  assert call({'message_ids':[True],'expected_ids':[first]})[0]==400
  assert call({'message_ids':[first,first],'expected_ids':[first]})[0]==400
  assert call({'message_ids':[],'expected_ids':[]})[0]==409
  db=srv.get_db();db.execute("INSERT INTO messages(channel,member_id,member_name,content,created_at) VALUES('alpha',?,'Alpha','second reference',?)",(a['member_id'],web.now_iso()));second=db.execute('SELECT last_insert_rowid()').fetchone()[0];db.commit();db.close()
  assert call({'message_ids':[first,second],'expected_ids':[first]})[0]==200
  assert [p['id'] for p in call()[1]['pins']]==[first,second]
  db=srv.get_db();assert db.execute("SELECT pinned_message_id FROM channels WHERE code='alpha'").fetchone()[0] is None;db.close()
  real=handler._resolve_identity
  class Guest:source='guest';member_id='guest'
  handler._resolve_identity=lambda self:(None,Guest(),False)
  assert call()[0]==403
  handler.landing_mode=False;handler.channel='alpha'
  assert len(call()[1]['pins'])==2 and call()[1]['can_edit'] is False
  assert call({'message_ids':[],'expected_ids':[first,second]})[0]==403
  Guest.source=web.IDENTITY_SOURCE_PENDING
  assert call()[0]==403
  handler.landing_mode=True;handler._resolve_identity=real
  db=srv.get_db();db.execute('UPDATE messages SET retracted_at=? WHERE id=?',(web.now_iso(),first));db.commit();db.close()
  assert [p['id'] for p in call()[1]['pins']]==[second]
  assert call({'message_ids':[first],'expected_ids':[first,second]})[0]==404
  assert call({'message_ids':[second],'expected_ids':[first,second]})[0]==200
  assert call({'message_ids':[],'expected_ids':[second]})[0]==200
  assert call()[1]['pins']==[]
  db=srv.get_db();db.execute("UPDATE channels SET archived_at=? WHERE code='alpha'",(web.now_iso(),));db.commit();db.close()
  assert call()[1]['can_edit'] is False
  assert call({'message_ids':[],'expected_ids':[]})[0]==409
  print('PASS: two pins, objective unchanged, remove one/all, stale CAS, attachment, cross-channel/private/retracted denial, guest/pending admission')
 finally:server.shutdown();server.server_close()
