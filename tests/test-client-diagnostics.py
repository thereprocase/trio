"""Diagnostic projection, retention and HTTP access boundaries; synthetic data only."""
import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import nth_web as web

class Handler(web.NthWebHandler):
    def _resolve_identity(self):
        source = self.headers.get('X-Test-Role', 'member')
        return '', web.OperatorIdentity('_op_t_owner' if source == 'owner' else '_op_m_reader', 'Fixture', source), False

server = web.QuietThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()

def call(method='GET', body=None, role='member', origin=None):
    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/client-diagnostics',
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'X-Test-Role': role, **({'Origin': origin} if origin else {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())

try:
    web.CLIENT_DIAGNOSTICS.clear()
    payload = {'session': 'a' * 16, 'reason': 'manual', 'message': 'PRIVATE_SENTINEL',
        'token': 'PRIVATE_SENTINEL', 'feeds': {'channel': {'count': 12, 'age_ms': 45001, 'body': 'PRIVATE_SENTINEL'}},
        'events': [{'kind': 'request', 'method': 'GET', 'path': '/api/channels', 'status': 403,
            'duration_ms': 7, 'body': 'PRIVATE_SENTINEL', 'error': 'TypeError'},
            {'kind': 'request', 'path': '/api/history?token=PRIVATE_SENTINEL'},
            {'kind': 'error', 'error': 'PRIVATE_SENTINEL', 'stack': 'PRIVATE_SENTINEL'}]}
    assert call('POST', payload)[0] == 200
    assert call()[0] == 403  # named member may submit, never inspect others
    status, output = call(role='owner')
    assert status == 200 and len(output['reports']) == 1
    report = output['reports'][0]
    assert report['events'][0]['status'] == 403 and report['feeds']['channel']['age_ms'] == 45001
    assert 'PRIVATE_SENTINEL' not in json.dumps(output)
    assert call('POST', payload)[0] == 429
    assert call('POST', payload, role='pending')[0] == 403
    assert call('POST', payload, origin='https://unrelated.invalid')[0] == 403
    assert call('POST', ['not', 'object'])[0] == 400
    assert call('POST', {'session': 'x' * 20000})[0] == 400
    for i in range(110):
        web.CLIENT_DIAGNOSTICS.append({'member_id': str(i), 'received_at': 0})
    assert len(web.CLIENT_DIAGNOSTICS) == 100
    payload['events'] = payload['events'] * 40
    assert len(web._project_client_diagnostics(payload)['events']) == 60
    print('PASS: projection/privacy, member submit, owner-only read, rate/size/origin bounds, ring retention')
finally:
    server.shutdown()
    server.server_close()
