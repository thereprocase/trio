"""PR 3/C8 contracts: isolated homes, scripted pollers and real Unix IPC."""
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import nth_interposer as service
import nth_interposer_wire as wire
from nth_interposer_store import Store
from nth_interposer_runtime import Runtime
import nth_interposer_shadow as shadow
import nth_listener as nl
import nth_claude_hook as claude
import nth_codex_hook as codex
import nth_cli as cli

KEY = '0123456789abcdef01234567'
KEY2 = 'abcdef0123456789abcdef01'
SESSION = 'session-alpha'
SESSION2 = 'session-bravo'
SECRET = 'synthetic-token-private'
CONTENT = 'synthetic-peer-text-private'
SENDER = 'synthetic-peer-name-private'
URL = 'https://hub.example/sse'


def request(op, **fields):
    return dict(v=1, id=7, op=op, **fields)


def registration(session=SESSION, client='claude'):
    return request('session.register', session=session, client=client, host_pid=None,
                   sink='rewake' if client == 'claude' else 'queue', host_ok=True, problem='')


def message(mid, **flags):
    return dict(id=mid, content=CONTENT, **{'from': SENDER}, **flags)


class ScriptedHub:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls, self.closed = [], 0
        self.lock = threading.Lock()

    def factory(self, identity):
        def poll(arguments):
            with self.lock:
                self.calls.append((identity['url'], dict(arguments)))
                reply = self.replies.pop(0) if self.replies else {'event': 'no_new', 'messages': []}
            if isinstance(reply, Exception):
                raise reply
            return reply
        def close():
            self.closed += 1
        return poll, close


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ip3-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, dict(os.environ, HOME=str(self.root),
            NTH_HOME=str(self.root / 'nth'), XDG_RUNTIME_DIR=str(self.root / 'rt'),
            TRIO_INTERPOSER_SHADOW='1', NTH_QUIET='1', NTH_INTERPOSER_TEST_RUNTIME=str(self.root),
            CODEX_HOME=str(self.root/'codex'),TRIO_CODEX_HOME=str(self.root/'codex')), clear=True)
        self.environment.start()
        os.environ.pop("NTH_INTERPOSER_SOCKET",None)
        from interposer_test_dns import fixture_dns
        self.dns = patch.object(socket,"getaddrinfo",side_effect=fixture_dns)
        self.dns.start()
        self.store = Store()
        self.hub = ScriptedHub()
        self.runtime = Runtime(self.store, logging.getLogger('test.shadow'), self.hub.factory)
        self.fast = patch.multiple(nl, MIN_POLL_GAP_SECONDS=.01, REFUSAL_GRACE_SECONDS=.02,
                                  RETRY_STEP_SECONDS=.02, RETRY_MAX_SECONDS=.05)
        self.fast.start()
        self.stop, self.server, self.thread = threading.Event(), None, None

    def tearDown(self):
        if self.server:
            self.stop.set()
            self.thread.join(2)
            self.server.server_close()
        self.runtime.close()
        for worker in list(self.runtime.startup_threads):
            worker.join(2)
        self.store.close()
        self.fast.stop()
        self.dns.stop()
        self.environment.stop()
        self.temp.cleanup()

    def identity(self, key=KEY, url=URL, source='quartet'):
        path = wire.private_dir(self.store.path.parent / 'identities') / (key + '.json')
        path.write_text(json.dumps(dict(source=source, url=url, channel='room', member_id='member',
                                      session_token=SECRET)))
        path.chmod(0o600)

    def op(self, op, **fields):
        return service.dispatch(self.store, request(op, **fields), self.runtime)

    def attach(self, key=KEY, session=SESSION, url=URL, source='quartet', server='nth-qweb'):
        service.dispatch(self.store, registration(session))
        self.identity(key, url, source)
        if source != 'local':
            self.store.setup_hub(server, url)
        result = self.op('membership.attach', session=session, key=key, server=server, via='connect')
        self.wait_start(key)
        return result

    def wait_start(self, key, runtime=None):
        runtime = runtime or self.runtime
        def settled():
            if runtime is not self.runtime:
                runtime.reconcile()
            with runtime.store.lock:
                row = runtime.member(key)
                return key not in runtime.startups and (key in runtime.pollers or key in runtime.start_retry
                                                        or not runtime.eligible(row))
        self.eventually(settled)

    def eventually(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.runtime.reconcile()
            if predicate():
                return
            time.sleep(.01)
        self.fail('shadow condition did not become true')

    def start_socket(self):
        wire.private_dir(wire.socket_path().parent)
        self.server = service.Server(wire.socket_path(), self.store, logging.getLogger('test.shadow'))
        self.server.runtime = self.runtime
        def run():
            while not self.stop.is_set():
                self.server.handle_request()
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def test_c1_golden_request_and_reply_frames(self):
        self.identity(source='local', url='/temporary/nth.db')
        cases = json.loads((ROOT/'tests/fixtures/interposer-shadow-wire.json').read_text())
        self.start_socket()
        with wire.connect() as client:
            for case in cases:
                req = json.loads(case['request'])
                expected = json.loads(case['reply'])['ok']
                with self.subTest(op=req['op']):
                    golden = case['request'].encode()
                    self.assertEqual(wire.encode_frame(req), golden)
                    reply = client.call(req['op'], **{k: v for k, v in req.items() if k not in ('v','id','op')})
                    self.assertEqual(reply, expected)
                    self.assertEqual(wire.encode_frame(dict(v=1, id=7, ok=reply)),
                                     case['reply'].encode())
        self.assertEqual(self.store.snapshot()['memberships'][0]['shadow_acked_through'], 12)

    def test_every_field_validation(self):
        cases = [registration(), request('membership.attach', session=SESSION, key=KEY, server='nth-qweb', via='ack'),
            request('membership.configure', key=KEY, filter='at', enabled=True),
            request('ack.seen', session=SESSION, key=KEY, through_id=3),
            request('turn', session=SESSION, phase='started'), request('session.end', session=SESSION)]
        invalid = dict(session=[None, '', '../bad', 3], key=[None, 'x', 'A'*24, 3],
            server=[ 'nth-'+ 'x'*41, '../server', 3], via=['bad', None],
            filter=['bad', None], enabled=[1, None], through_id=[-1, True, 1.5, 2**63],
            client=['frontend', None], host_pid=[0, -1, True, 1.5], sink=['channel', None],
            host_ok=[1, None], problem=['\n', 'é', 'x'*201, 1], phase=['idle', None])
        for req in cases:
            wire.validate_request(req)
            with self.assertRaises(wire.WireError):
                wire.validate_request(dict(req, surprise=True))
            for field in req.keys() - {'v', 'id', 'op'}:
                if not (req['op'] == 'membership.configure' and field in ('filter','enabled')):
                    with self.assertRaises(wire.WireError):
                        wire.validate_request({k: v for k,v in req.items() if k != field})
                for value in invalid.get(field, []):
                    with self.subTest(op=req['op'], field=field, value=value):
                        with self.assertRaises(wire.WireError):
                            wire.validate_request(dict(req, **{field:value}))
        wire.validate_request(request('membership.configure', key=KEY))

    def test_error_frame_and_unknown_session(self):
        self.start_socket()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(wire.socket_path()))
            with wire.Client(sock) as client:
                client.call('hello')
                sock.sendall(wire.encode_frame(request('turn', session=SESSION, phase='ended')))
                self.assertEqual(wire.read_frame(client.reader), dict(v=1, id=7,
                    error=dict(code='unknown_session', message='session is not registered')))
        with self.assertRaisesRegex(wire.WireError, 'not registered'):
            self.op('membership.attach', session=SESSION, key=KEY, server='nth-qweb', via='connect')

    def test_owner_latest_attach_and_end_handoff(self):
        self.attach()
        service.dispatch(self.store, registration(SESSION2))
        self.op('membership.attach', session=SESSION2, key=KEY, server='nth-qweb', via='listen')
        self.assertEqual(self.store.snapshot()['memberships'][0]['owner_session'], SESSION2)
        self.op('session.end', session=SESSION2)
        self.assertEqual(self.store.snapshot()['memberships'][0]['owner_session'], SESSION)
        self.op('session.end', session=SESSION)
        self.assertIsNone(self.store.snapshot()['memberships'][0]['owner_session'])
        self.assertEqual(service.dispatch(self.store,dict(registration(),resume=True))['state'], 'idle')

    def test_attach_preserves_config_and_ack_monotonic(self):
        self.attach()
        self.op('membership.configure', key=KEY, filter='at', enabled=False)
        self.op('membership.attach', session=SESSION, key=KEY, server='nth-qweb', via='ack')
        self.op('ack.seen', session=SESSION, key=KEY, through_id=9)
        self.op('ack.seen', session=SESSION, key=KEY, through_id=2)
        row = self.store.snapshot()['memberships'][0]
        self.assertEqual((row['shadow_filter'], row['shadow_enabled'], row['shadow_acked_through']), ('at', 0, 9))

    def test_configure_stops_poller(self):
        self.attach()
        listener = self.runtime.pollers[KEY]
        self.op('membership.configure', key=KEY, enabled=False)
        self.assertTrue(listener._stop.is_set())
        self.assertNotIn(KEY, self.runtime.pollers)
        self.assertEqual(self.store.snapshot()['memberships'][0]['poll_state'], 'stopped')

    def test_tell_socket_and_absent_inbox(self):
        self.assertFalse(wire.tell('session.register', **{k:v for k,v in registration().items() if k not in ('v','id','op')}))
        self.assertEqual(len(list((wire.home() / 'events/inbox').glob('*.json'))), 1)
        self.runtime.drain()
        self.assertEqual(self.store.session(SESSION)['state'], 'idle')
        self.start_socket()
        self.assertTrue(wire.tell('turn', session=SESSION, phase='started'))
        self.assertEqual(self.store.session(SESSION)['state'], 'in_turn')
        self.assertFalse(list((wire.home() / 'events/inbox').glob('*.json')))

    def test_tell_kill_switch_and_never_raises(self):
        with patch.dict(os.environ, TRIO_INTERPOSER_SHADOW='0'):
            self.assertFalse(wire.tell('turn', session=SESSION, phase='ended'))
        self.assertFalse((wire.home() / 'events/inbox').exists())
        for fields in ({'session': []}, {'session': SESSION, 'phase': object()}):
            self.assertFalse(wire.tell('turn', **fields))
        with patch.object(wire, 'private_dir', side_effect=OSError):
            self.assertFalse(wire.tell('turn', session=SESSION, phase='ended'))

    def test_tell_inbox_cap_permissions_and_atomic_file(self):
        with claude.session_update(SESSION,create=True) as state:
            state['memberships'][KEY]={'source':'local','channel':'room','member_id':'member'}
        inbox = wire.private_dir(wire.home() / 'events/inbox')
        for i in range(1000):
            (inbox / f'{i:020d}.json').write_text('{}\n')
        wire.tell('turn', session=SESSION, phase='ended')
        files = list(inbox.glob('*.json'))
        self.assertEqual(len(files), 1000)
        self.assertFalse((inbox / '00000000000000000000.json').exists())
        new = max(files, key=lambda p:p.name)
        self.assertEqual(new.stat().st_mode & 0o777, 0o600)
        self.assertEqual(inbox.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(inbox.glob('*.tmp')))

    def test_drain_applies_quarantines_and_caps(self):
        wire.tell('session.register', **{k:v for k,v in registration().items() if k not in ('v','id','op')})
        inbox = wire.home() / 'events/inbox'
        for i in range(101):
            (inbox / f'bad-{i:03d}.json').write_text('not-json')
        self.runtime.drain()
        self.assertEqual(self.store.session(SESSION)['state'], 'idle')
        self.assertEqual(len(list((inbox/'bad').glob('*.json'))), 100)
        self.assertFalse(list(inbox.glob('*.json')))

    def test_hung_socket_budget_and_batch_budget(self):
        with claude.session_update(SESSION,create=True) as state:
            state['memberships'][KEY]={'source':'local','channel':'room','member_id':'member'}
        wire.private_dir(wire.socket_path().parent)
        stop = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(wire.socket_path()))
            sock.listen()
            peers = []
            def accept():
                sock.settimeout(.05)
                while not stop.is_set():
                    try:
                        peer, _ = sock.accept()
                        peers.append(peer)
                    except socket.timeout:
                        continue
            thread = threading.Thread(target=accept)
            thread.start()
            try:
                before = time.monotonic()
                with wire.observation():
                    for phase in ('started', 'ended', 'started'):
                        self.assertFalse(wire.tell('turn', session=SESSION, phase=phase))
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(len(list((wire.home()/'events/inbox').glob('*.json'))), 3)
            finally:
                stop.set()
                thread.join(1)
                for peer in peers:
                    peer.close()

    def test_500_addressed_all_ids_and_private_logs(self):
        self.hub.replies = [{'event':'new_messages','messages':[message(i, mentioned=True) for i in range(1,501)]}]
        self.attach()
        self.eventually(lambda: bool(shadow.records('would')))
        row = shadow.records('would')[0]
        self.assertEqual(row['ranges'], [dict(key=KEY,server='nth-qweb',first=1,last=500,count=500,addressed=True)])
        member = self.store.snapshot()['memberships'][0]
        self.assertEqual((member['shadow_announced_through'], member['shadow_ids'], member['shadow_notices']), (500,500,1))
        text = json.dumps(row)
        for marker in (SECRET, CONTENT, SENDER):
            self.assertNotIn(marker, text)
        self.assertEqual(row['lines'], 1)
        args = self.hub.calls[0][1]
        self.assertEqual(args, dict(channel='room',member_id='member',session_token=SECRET, auto_ack=False,
            wait_seconds=0,mentions_only=False,monitor_heartbeat=False,after_id=0))
        self.eventually(lambda: len(self.hub.calls) > 1)
        self.assertEqual(self.hub.calls[1][1]['wait_seconds'], 30)
        self.assertFalse(any(args['auto_ack'] for _,args in self.hub.calls))

    def test_500_unaddressed_no_notice_watermark_advances(self):
        self.hub.replies = [{'event':'new_messages','messages':[message(i) for i in range(1,501)]}]
        self.attach()
        self.eventually(lambda:self.store.snapshot()['memberships'][0]['shadow_announced_through']==500)
        self.runtime.release(SESSION, force=True)
        self.assertEqual(shadow.records('would'), [])

    def test_bangs_filter_holes_dedup_and_no_cap(self):
        batch = [message(1),message(2,banged=True),message(3),message(4,referenced=True),message(5,mentioned=True)]
        self.hub.replies = [{'event':'new_messages','messages':batch}]*2
        self.attach()
        self.eventually(lambda: bool(shadow.records('would')))
        spans=shadow.records('would')[0]['ranges']
        self.assertEqual([(r['first'],r['last']) for r in spans], [(2,2),(4,5)])
        self.assertEqual(sum(r['count'] for r in spans),3)

    def test_two_hubs_coalesce_one_notice(self):
        barrier=threading.Barrier(2)
        replies={URL:dict(event='new_messages',messages=[message(1,mentioned=True)]),
                 'https://second.example/sse':dict(event='new_messages',messages=[message(7,banged=True)])}
        def factory(identity):
            first=True
            def poll(arguments):
                nonlocal first
                if first:
                    first=False
                    barrier.wait(timeout=2)
                    return replies[identity['url']]
                return dict(event='no_new',messages=[])
            return poll,lambda:None
        self.runtime.factory=factory
        self.attach()
        time.sleep(.05)  # The first poller must not steal the second hub's reply.
        self.attach(KEY2,url='https://second.example/sse',server='nth-second')
        self.eventually(lambda:bool(shadow.records('would')))
        self.assertEqual(len(shadow.records('would')),1)
        row=shadow.records('would')[0]
        self.assertEqual(row['lines'],2)
        self.assertEqual({r['key']:(r['first'],r['last'],r['count']) for r in row['ranges']},
                         {KEY:(1,1,1),KEY2:(7,7,1)})

    def test_each_terminal_once(self):
        cases = [({'error':'Invalid or revoked session_token.'},'membership refused'),
                 ({'error':nl.NOT_A_MEMBER},'member removed'),
                 ({'event':'ended'},'channel ended'),({'event':'channel_gone'},'channel ended')]
        for i,(reply,reason) in enumerate(cases):
            key=f'{i+10:024x}'
            self.hub.replies=[reply]
            self.attach(key)
            self.eventually(lambda: self.store.snapshot(key=key)['memberships'][0]['shadow_ended']==reason)
            self.runtime.release(SESSION, force=True)
            self.runtime.reconcile()
            records=[e for r in shadow.records('would') for e in r['ended'] if e['key']==key]
            self.assertEqual(records,[dict(key=key,reason=reason)])

    def test_turn_holds_ended_releases(self):
        self.attach()
        self.op('turn',session=SESSION,phase='started')
        self.hub.replies=[{'event':'new_messages','messages':[message(1,mentioned=True)]}]
        self.eventually(lambda: bool(self.runtime.buffers))
        self.runtime.release(SESSION,force=True)
        self.assertEqual(shadow.records('would'),[])
        self.op('turn',session=SESSION,phase='ended')
        self.assertEqual(len(shadow.records('would')),1)

    def test_codex_settle_window(self):
        self.attach()
        service.dispatch(self.store, registration(client='codex'))
        row=self.store.snapshot()['memberships'][0]
        self.runtime.accumulate(row,[message(8,mentioned=True)])
        self.runtime.buffers[SESSION]['at']=time.monotonic()-1
        self.runtime.release(SESSION)
        self.assertEqual(shadow.records('would'),[])
        self.runtime.buffers[SESSION]['at']=time.monotonic()-3
        self.runtime.release(SESSION)
        self.assertEqual(len(shadow.records('would')),1)

    def test_reconnecting_no_notice_and_host_death(self):
        self.hub.replies=[OSError(SECRET)]*100
        self.attach()
        self.eventually(lambda:self.store.snapshot()['memberships'][0]['poll_state']=='reconnecting')
        self.assertEqual(shadow.records('would'),[])
        row=dict(registration(),host_pid=os.getpid())
        service.dispatch(self.store,row)
        self.assertEqual(self.store.session(SESSION)['host_stamp'],claude.process_stamp(os.getpid()))
        with patch('nth_interposer_runtime.process_stamp',return_value=None):
            self.runtime.tick()
        self.assertEqual(self.store.session(SESSION)['state'],'ended')

    def test_rotation_permissions_projection_both_sides(self):
        item=dict(key=KEY,server='nth-qweb',first=1,last=3,count=3,addressed=True,content=CONTENT,sender=SENDER,token=SECRET)
        for side in ('actual','would'):
            with patch.object(shadow,'ROTATE_BYTES',1):
                for i in range(2):
                    self.assertTrue(shadow.append(side,SESSION,'claude','rewake',[item],[dict(key=KEY,reason=CONTENT,content=CONTENT,sender=SENDER,token=SECRET)],1))
            self.assertEqual(len(shadow.records(side)),2)
            self.assertTrue((wire.home()/'events/shadow'/(side+'.jsonl.1')).exists())
            self.assertEqual(len((wire.home()/'events/shadow'/(side+'.jsonl')).read_text().splitlines()),1)
            for path in (wire.home()/'events/shadow').glob(side+'.jsonl*'):
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                for marker in (CONTENT,SENDER,SECRET):
                    self.assertNotIn(marker,path.read_text())
        self.assertEqual((wire.home()/'events/shadow').stat().st_mode&0o777,0o700)

    def test_shadow_diff_match_missing_and_delay(self):
        def write(side,mid,t,key=KEY,session=SESSION):
            with patch.object(shadow.time,'time',return_value=t):
                shadow.append(side,session,'claude','rewake',[dict(key=key,server='nth-qweb',first=mid,last=mid,count=1,addressed=True)],[],1)
        for side in ('actual','would'):
            write(side,99,0,key=KEY2)
            write(side,100,100,key=KEY2)
        write('actual',1,20)
        write('actual',2,30)
        write('would',1,22)
        write('would',2,38)
        with patch.object(shadow.time,'time',return_value=110),patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['interposer','shadow-diff','--json']),0)
            result=json.loads(out.getvalue())
        self.assertEqual(result['sessions'][SESSION],dict(actual_notices=2,would_notices=2))
        self.assertEqual(result['median_release_delay'],5)
        write('actual',3,50)
        write('would',4,55)
        with patch.object(shadow.time,'time',return_value=110),patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','shadow-diff','--json']),1)
        result=shadow.compare()
        self.assertEqual(result['missing_in_would'],[dict(session=SESSION,key=KEY,first=3,last=3)])
        self.assertEqual(result['missing_in_actual'],[dict(session=SESSION,key=KEY,first=4,last=4)])
        with patch.object(shadow.time,'time',return_value=110),patch('sys.stdout',io.StringIO()) as out:
            self.assertEqual(cli.main(['interposer','shadow-diff','--since','40','--json']),0)
            self.assertEqual(json.loads(out.getvalue())['sessions'],{})

    def test_c8_pending_attach_waits_for_approval(self):
        service.dispatch(self.store,registration())
        self.identity()
        self.store.announce('nth-qweb',URL)
        with self.assertRaises(wire.WireError) as error:
            self.op('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect')
        self.assertEqual(error.exception.code,'hub_not_allowed')
        self.assertEqual(self.hub.calls,[])
        with patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb',URL]),0)
        self.op('membership.attach',session=SESSION,key=KEY,server='nth-qweb',via='connect')
        self.eventually(lambda:bool(self.hub.calls))

    def test_c8_repoint_preserves_trusted_url(self):
        self.attach()
        self.eventually(lambda:len(self.hub.calls)>=2)
        first=self.runtime.pollers[KEY]
        self.op('membership.configure',key=KEY,enabled=False)
        first.thread.join(1)
        self.assertFalse(first.thread.is_alive())
        baseline=len(self.hub.calls)
        new='https://untrusted.example/sse'
        with self.assertLogs('trio.interposer',level='WARNING') as logs:
            row=self.store.announce('nth-qweb',new)
        self.assertEqual((row['url'],row['pending_url'],row['trust']),(URL,new,'setup'))
        self.assertEqual(logs.records[0].getMessage(),'hub change pending')
        self.op('membership.configure',key=KEY,enabled=True)
        self.eventually(lambda:len(self.hub.calls)>baseline)
        self.assertIsNot(self.runtime.pollers[KEY],first)
        self.assertEqual({url for url,_ in self.hub.calls[baseline:]},{URL})
        self.assertEqual(self.store.snapshot()['hubs'][0]['pending_url'],new)
        self.start_socket()
        with wire.connect() as client:
            for op in ('status','list'):
                self.assertEqual(client.call(op)['hubs'][0]['pending_url'],new)
        for line in logs.output:
            self.assertNotIn(new,line)
            self.assertNotIn(SECRET,line)

    def test_c8_approve_cli_promotes_pending_change(self):
        self.store.announce('nth-qweb',URL)
        with patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb',URL]),0)
        self.assertEqual(self.store.snapshot()['hubs'][0]['trust'],'setup')
        new='https://second.example/sse'
        self.store.announce('nth-qweb',new)
        with patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['interposer','approve','nth-qweb',new]),0)
        self.assertEqual(self.store.snapshot()['hubs'][0]['url'],new)
        with self.assertRaises(wire.WireError):
            wire.validate_request(request('hub.approve',server='nth-qweb'))

    def test_c8_restricted_hosts_cap_and_url_limit(self):
        for host in ('localhost','127.0.0.1','127.1','2130706433','[::1]', '[::ffff:127.0.0.1]',
                     '169.254.169.254','[fe80::1]','100.100.100.200','[fd00:ec2::254]',
                     '0.0.0.0','metadata.google.internal'):
            with self.subTest(host=host),self.assertRaises(wire.WireError):
                self.store.announce('nth-bad','http://'+host+'/sse')
        loop='http://127.0.0.1/sse'
        self.store.setup_hub('nth-localhub',loop)
        self.assertEqual(self.store.announce('nth-localhub',loop)['trust'],'setup')
        for i in range(8):
            self.store.announce(f'nth-hub-{i}',URL)
        with self.assertRaises(wire.WireError):
            self.store.announce('nth-overflow',URL)
        with self.assertRaises(wire.WireError):
            wire.validate_hub('nth-qweb','https://hub.example/'+'x'*512)

    def test_c8_config_fixtures_import_without_writing(self):
        data={'mcpServers':{'nth-qweb':{'command':'python','args':['/install/nth_quartet_proxy.py','--url',URL]},
                            'unrelated':{'command':'other','args':['--url','https://other.example/sse']}}}
        path=self.root/'.claude.json'
        path.write_text(json.dumps(data))
        directory=self.root/'.codex'
        directory.mkdir()
        config=directory/'config.toml'
        config.write_text('[mcp_servers.nth-second]\ncommand="python"\nargs=["/install/nth_quartet_proxy.py","--url","http://127.0.0.1/sse"]\n')
        before=(path.read_bytes(),config.read_bytes())
        self.store.import_hubs()
        self.assertEqual({r['server']:r['trust'] for r in self.store.snapshot()['hubs']},
                         {'nth-qweb':'setup','nth-second':'setup'})
        self.assertEqual((path.read_bytes(),config.read_bytes()),before)

    def test_c8_setup_and_service_start_import_config_trust(self):
        spec=importlib.util.spec_from_file_location('shadow_setup',ROOT/'setup.py')
        setup=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        staged=self.root/'staged'
        setup.install(staged,quartet_url=URL,clients=('claude',),skip_dependencies=True,skip_systemd=True)
        installed=Store(staged/'.claude/nth/events/interposer.sqlite')
        try:
            self.assertEqual(installed.snapshot()['hubs'][0]['trust'],'setup')
        finally:
            installed.close()
        path=self.root/'.claude.json'
        path.write_text(json.dumps({'mcpServers':{'nth-config':{'command':'python',
            'args':['/install/nth_quartet_proxy.py','--url',URL]}}}))
        process=subprocess.Popen([sys.executable,str(ROOT/'server/nth_interposer.py'),'serve','--idle-seconds','1'],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                try:
                    with wire.connect(timeout=.1) as client:
                        state=client.call('list')
                        self.assertEqual(state['hubs'][0]['trust'],'setup')
                        self.assertEqual(state['hubs'][0]['server'],'nth-config')
                        break
                except (OSError,EOFError):
                    time.sleep(.01)
            else:
                self.fail('service startup did not import trusted configuration')
        finally:
            process.terminate()
            process.wait(timeout=3)

    def test_identity_replacement_cannot_redirect_token(self):
        self.attach()
        self.op('membership.configure',key=KEY,enabled=False)
        self.identity(url='https://untrusted.example/sse')
        before=len(self.hub.calls)
        self.op('membership.configure',key=KEY,enabled=True)
        self.wait_start(KEY)
        self.assertEqual(len(self.hub.calls),before)
        self.assertEqual(self.store.snapshot()['memberships'][0]['poll_state'],'reconnecting')

    def test_pending_membership_not_polled_even_if_present_in_database(self):
        self.store.announce('nth-qweb',URL)
        self.identity()
        service.dispatch(self.store,registration())
        with self.store.db:
            self.store.db.execute('INSERT INTO memberships(key,source,url,channel,member_id,owner_session) VALUES (?,?,?,?,?,?)',
                                  (KEY,'quartet',URL,'room','member',SESSION))
            self.store.db.execute('INSERT INTO holdings(session,key,server,joined,attached) VALUES (?,?,?,1,1)',(SESSION,KEY,'nth-qweb'))
        self.assertFalse(self.runtime.allowed(self.runtime.member(KEY)))
        self.runtime.reconcile()
        self.assertEqual(self.runtime.pollers,{})
        self.assertEqual(self.hub.calls,[])

    def test_hook_absent_service_inbox_and_unchanged_delivery_actual(self):
        self.identity(source='local',url='/temporary/nth.db')
        payload=dict(session_id=SESSION,tool_name='mcp__nth-trio__trio_connect',
                     tool_response=json.dumps(dict(identity_key=KEY,channel='room',member_id='member')))
        claude.register(payload)
        self.assertEqual([json.loads(p.read_text())['op'] for p in sorted((wire.home()/'events/inbox').glob('*.json'))],
                         ['session.register','membership.attach'])
        hub=ScriptedHub([{'event':'new_messages','messages':[message(7,mentioned=True)]}])
        output=io.StringIO()
        with patch.object(claude,'poll_factory',hub.factory),patch.object(claude,'SAY',output),\
             patch.multiple(claude,TICK_SECONDS=.01,SETTLE_SECONDS=.01,UNSUPERVISED_LIFETIME_SECONDS=.2):
            self.assertEqual(claude.wait(SESSION),2)
        expected=__import__('nth_notice').message_notice('trio','room','member',7,7,1,True)+'\n'
        self.assertEqual(output.getvalue(),expected)
        self.assertEqual(len(shadow.records('actual')),1)
        for marker in (SECRET,CONTENT,SENDER):
            self.assertNotIn(marker,json.dumps(shadow.records('actual')))

    def test_hook_kill_switch_nothing_written(self):
        self.identity(source='local',url='/temporary/nth.db')
        payload=dict(session_id=SESSION,tool_name='mcp__nth-trio__trio_connect',
                     tool_response=json.dumps(dict(identity_key=KEY,channel='room',member_id='member')))
        with patch.dict(os.environ,TRIO_INTERPOSER_SHADOW='0'):
            claude.register(payload)
            self.assertFalse((wire.home()/'events/inbox').exists())
            self.assertFalse(shadow.append('actual',SESSION,'claude','rewake',[],[],1))
        self.assertFalse((wire.home()/'events/shadow').exists())

    def test_queue_actual_queued_unknown_only(self):
        sink=codex.QueueSink(SESSION,None)
        sink.shadow_record=(SESSION,[],[],1)
        for result in ('queued','unknown','failed'):
            with patch.object(codex,'queue_wake',return_value=result):
                self.assertEqual(sink.deliver(['fixed notice']),result!='failed')
        self.assertEqual(len(shadow.records('actual')),2)

    def test_hook_listen_ack_order_and_saved_configuration(self):
        self.identity(source='local',url='/temporary/nth.db')
        payload=dict(session_id=SESSION,tool_name='mcp__nth-trio__trio_connect',
                     tool_response=json.dumps(dict(identity_key=KEY,channel='room',member_id='member')))
        claude.register(payload)
        inbox=wire.home()/'events/inbox'
        for file in inbox.glob('*.json'):
            file.unlink()
        claude.configure_membership(KEY,filter_mode='at',enabled=False)
        payload['tool_name']='mcp__nth-trio__trio_listen'
        claude.register(payload)
        rows=[json.loads(p.read_text()) for p in sorted(inbox.glob('*.json'))]
        self.assertEqual([r['op'] for r in rows],['session.register','membership.attach','membership.configure'])
        self.assertEqual((rows[-1]['filter'],rows[-1]['enabled']),('at',False))
        for file in inbox.glob('*.json'):
            file.unlink()
        payload.update(tool_name='mcp__nth-trio__trio_ack',tool_response=json.dumps(dict(ok=True,watermark=9)),
                       tool_input=dict(channel='room',member_id='member',through_id=9))
        claude.register(payload)
        rows=[json.loads(p.read_text()) for p in sorted(inbox.glob('*.json'))]
        self.assertEqual([r['op'] for r in rows],['session.register','membership.attach','ack.seen'])
        self.assertEqual(rows[-1]['through_id'],9)

    def test_session_hook_lifecycle_and_codex_host_fields(self):
        # Real Claude hook processes have no service and must exit silently.
        for event,payload in (('start',dict(session_id=SESSION,source='startup')),
                              ('stop',dict(session_id=SESSION)),('end',dict(session_id=SESSION))):
            run=subprocess.run([sys.executable,str(ROOT/'server/nth_claude_hook.py'),event],
                               input=json.dumps(payload),text=True,capture_output=True,timeout=2)
            self.assertEqual((run.returncode,run.stdout,run.stderr),(0,'',''))
        inbox=wire.home()/'events/inbox'
        self.assertEqual([json.loads(p.read_text())['op'] for p in sorted(inbox.glob('*.json'))],
                         ['session.register','session.end'])
        for file in inbox.glob('*.json'):
            file.unlink()
        with claude.session_update(SESSION,create=True) as state:
            state['memberships'][KEY]={'source':'local','channel':'room','member_id':'member'}
        for event in ('start','stop','end'):
            with patch('sys.stdin',io.StringIO(json.dumps(dict(session_id=SESSION,source='startup')))),\
                 patch('sys.stderr',io.StringIO()),patch.object(codex,'codex_host',return_value=(42,'synthetic unsafe host')),\
                 patch.object(codex,'trio_server_pid',return_value=None),patch.object(codex,'arm') as arm:
                self.assertEqual(codex.main([event]),0)
                if event=='stop':
                    def check(*args):
                        ops=[json.loads(p.read_text())['op'] for p in sorted(inbox.glob('*.json'))]
                        self.assertEqual(ops[-1],'turn')
                    # Repeat Stop with an arm observer to prove the ordering.
                    arm.side_effect=check
                    with patch('sys.stdin',io.StringIO(json.dumps(dict(session_id=SESSION)))):
                        codex.main(['stop'])
        rows=[json.loads(p.read_text()) for p in sorted(inbox.glob('*.json'))]
        self.assertEqual([r['op'] for r in rows],['session.register','turn','turn','session.end'])
        self.assertEqual((rows[0]['host_ok'],rows[0]['problem'],rows[0]['sink']),
                         (False,'synthetic unsafe host','queue'))

    def test_actual_flood_and_shadow_id_sets_match_with_holes(self):
        batch=[message(i,mentioned=True) for i in range(1,1001,2)]
        self.identity(source='local',url='/temporary/nth.db')
        claude.register(dict(session_id=SESSION,tool_name='mcp__nth-trio__trio_connect',
            tool_response=json.dumps(dict(identity_key=KEY,channel='room',member_id='member'))))
        self.hub.replies=[dict(event='new_messages',messages=batch)]
        self.runtime.drain()
        actual=ScriptedHub([dict(event='new_messages',messages=batch)])
        with patch.object(claude,'poll_factory',actual.factory),patch.object(claude,'SAY',io.StringIO()),\
             patch.multiple(claude,TICK_SECONDS=.01,SETTLE_SECONDS=.01,UNSUPERVISED_LIFETIME_SECONDS=.2):
            self.assertEqual(claude.wait(SESSION),2)
        self.eventually(lambda:bool(shadow.records('would')))
        result=shadow.compare()
        self.assertEqual(result['missing_in_actual'],[])
        self.assertEqual(result['missing_in_would'],[])
        self.assertEqual(sum(r['count'] for r in shadow.records('actual')[0]['ranges']),500)
        self.assertEqual(sum(r['count'] for r in shadow.records('would')[0]['ranges']),500)

    def test_shadow_notice_uses_shared_builders(self):
        self.attach()
        row=self.store.snapshot()['memberships'][0]
        self.runtime.accumulate(row,[message(3,banged=True),message(4,mentioned=True)])
        with patch('nth_interposer_runtime.message_notice',wraps=__import__('nth_notice').message_notice) as builder:
            self.runtime.release(SESSION,force=True)
            builder.assert_called_once_with('quartet','room','member',3,4,2,True,None)

    def test_frame_deadline_semaphore_and_service_log_rotation(self):
        left,right=socket.socketpair()
        try:
            left.sendall(b'{')
            before=time.monotonic()
            with self.assertRaises(TimeoutError):
                service.deadline_frame(right,seconds=.03)
            self.assertLess(time.monotonic()-before,.1)
        finally:
            left.close();right.close()
        self.start_socket()
        with patch.object(self.server,'connection_slots') as slots:
            slots.acquire.return_value=False
            import socketserver
            left,right=socket.socketpair()
            try:
                with patch.object(socketserver.ThreadingMixIn,'process_request') as parent,patch.object(self.server,'shutdown_request') as shutdown:
                    self.server.process_request(right,None)
                    slots.acquire.assert_called_once_with(blocking=False)
                    parent.assert_not_called()
                    shutdown.assert_called_once_with(right)
            finally:
                left.close();right.close()
        with service.service_log() as log:
            handler=log.handlers[-1]
            handler.maxBytes=100
            for _ in range(20):
                log.info('fixed synthetic event label')
        directory=wire.home()/'logs'
        self.assertTrue((directory/'interposer.log.1').exists())
        self.assertEqual((directory/'interposer.log').stat().st_mode&0o777,0o600)


if __name__=='__main__':
    unittest.main()
