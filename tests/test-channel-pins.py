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
 db.commit();db.close()
 handler=type('PinHandler',(web.NthWebHandler,),{'db_path':srv.DB_PATH,'landing_mode':True})
 server=web.QuietThreadingHTTPServer(('127.0.0.1',0),handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 def call(body=None):
  req=urllib.request.Request('http://127.0.0.1:%s/api/channel-pin?channel=alpha'%server.server_port,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 try:
  assert call()[1]['pin'] is None
  assert call({'message_id':first,'expected_id':None})[0]==200
  assert call()[1]['pin']['id']==first
  assert call()[1]['pin']['attachments'][0]['filename']=='reference.png'
  assert call({'message_id':other,'expected_id':first})[0]==404
  assert call({'message_id':private,'expected_id':first})[0]==404
  assert call({'message_id':True,'expected_id':first})[0]==400
  assert call({'message_id':None,'expected_id':None})[0]==409
  real=handler._resolve_identity
  class Guest:source='guest';member_id='guest'
  handler._resolve_identity=lambda self:(None,Guest(),False)
  assert call()[0]==403  # arbitrary landing-channel reads are forbidden
  handler.landing_mode=False;handler.channel='alpha'
  assert call()[1]['pin']['id']==first and call()[1]['can_edit'] is False
  assert call({'message_id':None,'expected_id':first})[0]==403
  Guest.source=web.IDENTITY_SOURCE_PENDING
  assert call()[0]==403
  handler.landing_mode=True
  handler._resolve_identity=real
  assert call({'message_id':None,'expected_id':first})[0]==200
  assert call()[1]['pin'] is None
  db=srv.get_db();db.execute('UPDATE messages SET retracted_at=? WHERE id=?',(web.now_iso(),first));db.commit();db.close()
  assert call({'message_id':first,'expected_id':None})[0]==404
  db=srv.get_db();db.execute("UPDATE channels SET archived_at=? WHERE code='alpha'",(web.now_iso(),));db.commit();db.close()
  assert call()[1]['can_edit'] is False
  assert call({'message_id':None,'expected_id':None})[0]==409
  print('PASS: pin/read/unpin, cross-channel/private denial, types, stale CAS, guest read-only')
 finally:server.shutdown();server.server_close()
