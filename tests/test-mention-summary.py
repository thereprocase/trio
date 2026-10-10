"""Summary counts match details, without returning bodies or resolving names."""
import json, sqlite3, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_web as web
with tempfile.TemporaryDirectory() as tmp:
    dbpath=Path(tmp)/'test.db'
    db=sqlite3.connect(dbpath)
    db.executescript('''CREATE TABLE channels(code TEXT,archived_at TEXT);
    CREATE TABLE messages(id INTEGER,channel TEXT,member_id TEXT,member_name TEXT,content TEXT,created_at TEXT,mentions TEXT);
    CREATE TABLE message_reads(message_id INTEGER,member_id TEXT);
    INSERT INTO channels VALUES('sample',NULL);''')
    for i,mentions in enumerate(['["owner"]','["owner"]','["owner-other"]'],1):
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',(i,'sample','peer','Peer','body '*1000,'2026-01-01',mentions))
    db.execute('INSERT INTO message_reads VALUES(1,"owner")');db.commit();db.close()
    h=object.__new__(web.NthWebHandler);h.db_path=dbpath;h._require_operator=lambda:SimpleNamespace(member_id='owner')
    out=[];h._json=lambda data:out.append(data)
    original=web.resolve_display_name
    web.resolve_display_name=lambda *args: (_ for _ in ()).throw(AssertionError('summary must not resolve names'))
    h._handle_mentions(urlparse('/api/mentions?summary=1'));summary=out.pop()
    assert summary=={'ok':True,'count':2,'unread_count':1}
    web.resolve_display_name=lambda *args:'Peer'
    h._handle_mentions();details=out.pop()
    assert details['count']==summary['count'] and details['unread_count']==summary['unread_count']
    assert len(json.dumps(summary)) < len(json.dumps(details))/100
    web.resolve_display_name=original
print('PASS: exact summary counts, no bodies or name lookups, >99% fixture payload reduction')
