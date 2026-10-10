"""Terminal routes are owner-only; bridge credentials cannot act as an owner."""
import hashlib,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
with tempfile.TemporaryDirectory() as tmp:
 os.environ['NTH_HOME']=tmp
 import nth_web as web
 import nth_terminal as terminal
 h=web.NthWebHandler.__new__(web.NthWebHandler);h.db_path=Path(tmp)/'state.db';h.headers={}
 outputs=[];h._json=lambda obj,**kw:outputs.append(obj);h._error=lambda code,text:outputs.append({'error':code})
 h._resolve_identity=lambda:('',SimpleNamespace(source=web.IDENTITY_SOURCE_GUEST),False)
 h._read_json_body=lambda **kw:(_ for _ in ()).throw(AssertionError('unauthorized body was read'))
 h._handle_terminals();h._handle_terminal_action();h._handle_terminal_exchange()
 assert outputs==[{'error':403}]*3,outputs
 outputs.clear();h._resolve_identity=lambda:('',SimpleNamespace(source=web.IDENTITY_SOURCE_LOOPBACK,member_id='_op_owner'),False)
 h._handle_terminals();assert outputs==[{'sessions':[]}]
 h._read_json_body=lambda **kw:[]
 h._handle_terminal_action();assert outputs[-1]=={'error':409}
 config=Path(tmp)/'hosts.json';token='c'*64
 terminal.save_private(config,{'hosts':[{'host':'host','token_hash':hashlib.sha256(token.encode()).hexdigest()}]})
 with patch.dict(os.environ,{'NTH_TERMINAL_HOSTS':str(config)}):
  h.headers={'Authorization':'Bearer '+token};h._read_json_body=lambda **kw:{'sessions':[]}
  h._handle_terminal_exchange();assert outputs[-1]=={'commands':[]}
  # Pairing grants only exchange, not terminal viewing or owner actions.
  h._resolve_identity=lambda:('',SimpleNamespace(source=web.IDENTITY_SOURCE_GUEST),False)
  h._handle_terminals();h._handle_terminal_action();assert outputs[-2:]==[{'error':403}]*2
print('PASS: owner viewing/actions; scoped bridge authentication; malformed action rejection')
