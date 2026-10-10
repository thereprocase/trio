"""Owning-daemon submission; no model calls, socket or process launches."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import nth_codex_hook as hook
from nth_codex_runtime import CodexProtocolError

class Client:
    status='active'
    failure=None
    instances=[]
    def __init__(self, endpoint):
        self.endpoint=endpoint; self.calls=[]; self.closed=False
        self.instances.append(self)
    def start(self, **kw): pass
    def stop(self): self.closed=True
    def request(self, method, params, **kw):
        self.calls.append((method,params))
        if method=='thread/read': return {'thread':{'status':{'type':self.status}}}
        if self.failure: raise CodexProtocolError(self.failure)
        return {'turn':{'id':'t'}}

class SteerTests(unittest.TestCase):
    def setUp(self):
        Client.instances=[]; Client.status='active'; Client.failure=None
        self.p=patch('nth_codex_socket.CodexSocketClient',Client); self.p.start()
        self.addCleanup(self.p.stop)
    def test_busy_and_idle_use_atomic_start_without_policy_overrides(self):
        for state in ('active','idle'):
            Client.status=state
            self.assertEqual(hook.steer_wake('exact-thread','untrusted payload'),'delivered')
            c=Client.instances[-1]
            self.assertTrue(c.closed)
            self.assertEqual(c.calls,[('thread/read',{'threadId':'exact-thread','includeTurns':False}),
                ('turn/start',{'threadId':'exact-thread','input':[{'type':'text','text':'untrusted payload','text_elements':[]}]})])
            self.assertTrue(c.endpoint.endswith('/app-server-control/app-server-control.sock'))
    def test_unloaded_never_resumed_or_queued(self):
        Client.status='notLoaded'
        self.assertEqual(hook.steer_wake('id','text'),'failed')
        self.assertEqual(len(Client.instances[-1].calls),1)
    def test_refusal_is_not_retried(self):
        Client.failure='turn/start: refused'
        self.assertEqual(hook.steer_wake('id','text'),'failed')
        self.assertEqual(len(Client.instances[-1].calls),2)
    def test_ambiguous_submission_not_retried(self):
        Client.failure='Codex App Server timed out: turn/start'
        self.assertEqual(hook.steer_wake('id','text'),'unknown')
        self.assertEqual(len(Client.instances[-1].calls),2)

if __name__=='__main__': unittest.main()
