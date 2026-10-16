"""Role boundaries exercised through real MCP connect and HTTP handlers; no agent processes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_server as srv
import nth_web as web
import nth_authority as auth


class Access(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = []
        for name, value in [('DB_DIR', Path(self.tmp.name)), ('DB_PATH', Path(self.tmp.name)/'test.db'), ('_OWNER_APPROVED_CHANNELS', False)]:
            p = patch.object(srv, name, value); p.start(); self.addCleanup(p.stop)
        self.agent = json.loads(srv.nth_connect('shared', 'Helper', 'shared'))
        self.secret_agent = json.loads(srv.nth_connect('private', 'Hidden', 'private'))
        self.owner = web.OperatorIdentity('_op_owner', 'Owner', 'tailscale', 'owner@example.test')
        self.reader = web.OperatorIdentity('_op_reader', 'Reader', 'member', 'reader@example.test')
        db = srv.get_db()
        db.execute("INSERT INTO members (id,channel,name,kind,last_seen,joined_at) VALUES (?,?,?,?,?,?)", (self.reader.member_id,'shared','Reader','human',srv.now_iso(),srv.now_iso()))
        db.execute("UPDATE members SET status_text='secret-project' WHERE channel='private'")
        db.execute("UPDATE agents SET cwd='/private/path',runtime_ref='secret-session',pid=123 WHERE id=?", (self.secret_agent['member_id'],))
        db.commit(); db.close()
        self.handler = object.__new__(web.NthWebHandler)
        self.handler.db_path = srv.DB_PATH
        self.ident = self.reader
        self.handler._resolve_identity = lambda: ('test', self.ident, False)
        self.handler._json = lambda result, **kw: setattr(self, 'response', (kw.get('status', 200), result))
        self.handler._error = lambda status, message: setattr(self, 'response', (status, {'error':message}))
        self.handler._read_json_body = lambda **kw: self.body

    def connect(self, channel, **kw):
        return json.loads(srv.nth_connect('test', 'Operator', channel, **kw))

    def test_remote_channel_creation_requires_proven_delegation(self):
        srv._OWNER_APPROVED_CHANNELS = True
        with patch.object(srv.mcp, 'get_context', side_effect=ValueError('no HTTP context')):
            self.assertEqual(self.connect('new')['code'], 'owner_approval_required')
            args = dict(resume_member_id=self.agent['member_id'], reclaim_secret=self.agent['reclaim_secret'])
            self.assertEqual(self.connect('new', **args)['code'], 'owner_approval_required')
            db = srv.get_db()
            auth.set_member_admin(db, self.agent['member_id'], True, self.owner.member_id, srv.now_iso()); db.commit(); db.close()
            self.assertIn('error', self.connect('forged', resume_member_id=args['resume_member_id'], reclaim_secret='wrong'))
            self.assertEqual(self.connect('new', **args)['action'], 'created')
            db = srv.get_db(); auth.set_member_admin(db,args['resume_member_id'],False,self.owner.member_id,srv.now_iso());db.commit();db.close()
            self.assertEqual(self.connect('revoked', **args)['code'], 'owner_approval_required')
            self.assertIn('session_token', self.connect('shared'))
        db = srv.get_db()
        self.assertIsNone(db.execute("SELECT 1 FROM channels WHERE code='forged'").fetchone())
        self.assertIsNone(db.execute("SELECT 1 FROM channels WHERE code='revoked'").fetchone()); db.close()

    def test_only_owner_can_grant_and_revoke(self):
        self.body = {'member_id': self.agent['member_id'], 'member_admin':True}
        self.handler._handle_member_authority(urlparse('/api/member-authority'), True)
        self.assertEqual(self.response[0], 403)
        self.ident = self.owner
        self.handler._handle_member_authority(urlparse('/api/member-authority'), True)
        self.assertTrue(self.response[1]['member_admin'])
        self.body['member_admin']='true'
        self.handler._handle_member_authority(urlparse('/api/member-authority'), True)
        self.assertEqual(self.response[0], 400)
        self.body['member_admin']=False
        self.handler._handle_member_authority(urlparse('/api/member-authority'), True)
        self.assertFalse(self.response[1]['member_admin'])

    def test_web_creation_grant_does_not_grant_process_controls(self):
        self.body = {'code':'delegated-room','topic':'test'}
        self.handler._handle_channel_create()
        self.assertEqual(self.response[0],403)
        db=srv.get_db();auth.set_member_admin(db,self.reader.member_id,True,self.owner.member_id,srv.now_iso());db.commit();db.close()
        self.handler._handle_channel_create()
        self.assertEqual(self.response[0],201)
        self.assertIsNone(self.handler._require_operator())
        self.assertEqual(self.response[0],403)
        db=srv.get_db();auth.set_member_admin(db,self.reader.member_id,False,self.owner.member_id,srv.now_iso());db.commit();db.close()
        self.body['code']='after-revoke';self.handler._handle_channel_create();self.assertEqual(self.response[0],403)

    def test_roster_shared_and_verified_origin_only(self):
        self.handler._handle_agents_list(urlparse('/api/agents'))
        self.assertEqual({a['id'] for a in self.response[1]['agents']}, {self.agent['member_id']})
        db=srv.get_db();auth.remember_origin(db,self.secret_agent['member_id'],self.reader.login,srv.now_iso());db.commit();db.close()
        self.handler._handle_agents_list(urlparse('/api/agents'))
        agents={a['id']:a for a in self.response[1]['agents']}
        own=agents[self.secret_agent['member_id']]
        self.assertTrue(own['from_your_tailnet']); self.assertEqual(own['channels'],[])
        for forbidden in ('secret-project','/private/path','secret-session','reclaim_secret','"pid"','"private"'):
            self.assertNotIn(forbidden,json.dumps(agents))
        self.ident=web.OperatorIdentity('_op_fake','Reader','guest',self.reader.login)
        self.handler._handle_agents_list(urlparse('/api/agents'));self.assertEqual(self.response[1]['agents'],[])
        self.ident=web.OperatorIdentity('_op_verified','Reader','guest',self.reader.login,tailnet_verified=True)
        self.handler._handle_agents_list(urlparse('/api/agents'));self.assertEqual(len(self.response[1]['agents']),1)
        self.ident=web.OperatorIdentity('_op_pending','','pending')
        self.handler._handle_agents_list(urlparse('/api/agents'));self.assertEqual(self.response[0],403)

    def test_origin_uses_request_peer_never_node_host_and_is_immutable(self):
        srv._OWNER_APPROVED_CHANNELS=True
        context=SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(client=SimpleNamespace(host='100.64.1.2'))))
        with patch.object(srv.mcp,'get_context',return_value=context), patch.object(auth,'verified_peer_login',return_value=self.reader.login) as who:
            connected=json.loads(srv.nth_connect('test','Helper','shared',node_host='invented-owner'))
            who.assert_called_once_with('100.64.1.2')
        db=srv.get_db();self.assertIn(connected['member_id'],auth.owned_agent_ids(db,self.reader.login))
        auth.remember_origin(db,connected['member_id'],'other@example.test',srv.now_iso());db.commit()
        self.assertNotIn(connected['member_id'],auth.owned_agent_ids(db,'other@example.test'));db.close()

    def test_non_tailnet_peer_never_shells_out(self):
        with patch('subprocess.run') as run:
            for ip in ('127.0.0.1','192.0.2.1','--help',None):
                self.assertIsNone(auth.verified_peer_login(ip))
            run.assert_not_called()

if __name__ == '__main__':
    unittest.main()
