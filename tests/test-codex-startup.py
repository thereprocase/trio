"""Concurrent launcher processes must share one server. No models or live state."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / 'server'
sys.path.insert(0, str(SERVER))
import nth_cli as cli

FAKE_SERVER = r'''
import json, sys, time
from urllib.parse import urlsplit
from websockets.sync.server import serve, unix_serve
endpoint = sys.argv[sys.argv.index('--listen') + 1]
def handle(ws):
    for line in ws:
        message = json.loads(line)
        if 'id' in message:
            ws.send(json.dumps({'id': message['id'], 'result': {'userAgent': 'synthetic-codex'}}))
time.sleep(.7)  # Both independent callers reach cold startup before it is ready.
if endpoint.startswith('unix://'):
    server = unix_serve(handle, path=endpoint[len('unix://'):])
else:
    address = urlsplit(endpoint)
    server = serve(handle, address.hostname, address.port)
with server:
    server.serve_forever()
'''

CALLER = r'''
import json, os, pathlib, subprocess, sys, time
sys.path.insert(0, os.environ['TRIO_TEST_SERVER'])
import nth_cli as cli
root = pathlib.Path(os.environ['NTH_HOME'])
real_popen = subprocess.Popen
def spawn(command, **kwargs):
    process = real_popen([sys.executable, str(root / 'fake_server.py'), *command[1:]], **kwargs)
    (root / ('spawn-' + str(process.pid))).write_text(str(process.pid))
    return process
cli.subprocess.Popen = spawn
cli.add_endpoint = lambda *args: None
cli.ensure_service = lambda: None
(root / ('ready-' + str(os.getpid()))).touch()
deadline = time.monotonic() + 15
while not (root / 'go').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('test start barrier timed out')
    time.sleep(.02)
try:
    endpoint, binary = cli.ensure_codex()
    print(json.dumps({'endpoint': endpoint, 'binary': binary}), flush=True)
except Exception as error:
    print(json.dumps({'error': str(error)}), flush=True)
    sys.exit(1)
'''


class StartupTests(unittest.TestCase):
    def test_a_live_server_is_not_replaced_when_local_registration_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'NTH_HOME': directory}):
            record = cli.state_dir() / 'codex-server.json'
            record.write_text(json.dumps({'endpoint': 'unix:///tmp/live.sock', 'binary': sys.executable}))
            with patch.object(cli, 'connectable', return_value=True), \
                 patch.object(cli, 'add_endpoint', side_effect=PermissionError('synthetic registry failure')), \
                 patch.object(cli.subprocess, 'Popen') as spawn:
                with self.assertRaises(PermissionError):
                    cli.ensure_codex()
                spawn.assert_not_called()

    def test_startup_lease_is_exclusive_and_released_after_process_death(self):
        with tempfile.TemporaryDirectory(prefix='trio-lease-') as directory:
            root = Path(directory)
            env = dict(os.environ, NTH_HOME=directory, PYTHONPATH=str(SERVER))
            code = ('import pathlib, time, nth_cli; '
                    'lease=nth_cli.codex_startup_lock(); lease.__enter__(); '
                    'pathlib.Path(nth_cli.home(), "locked").touch(); time.sleep(30)')
            holder = subprocess.Popen([sys.executable, '-c', code], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not (root / 'locked').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / 'locked').exists(), 'holder did not acquire the startup lease')
                with patch.dict(os.environ, {'NTH_HOME': directory}):
                    with self.assertRaises(sqlite3.OperationalError):
                        with cli.codex_startup_lock(timeout=.1):
                            self.fail('a second launcher acquired the held lease')
                    holder.kill()
                    holder.wait(timeout=5)
                    with cli.codex_startup_lock(timeout=.5):
                        pass  # No stale file/PID cleanup is needed after a crash.
            finally:
                if holder.poll() is None:
                    holder.kill()
                holder.communicate(timeout=5)

    def test_failed_record_replacement_preserves_the_previous_complete_record(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'codex-server.json'
            previous = {'endpoint': 'unix:///tmp/previous.sock', 'pid': 1}
            replacement = {'endpoint': 'unix:///tmp/current.sock', 'pid': 2}
            record.write_text(json.dumps(previous), encoding='utf-8')
            with patch.object(cli.os, 'replace', side_effect=PermissionError('synthetic write failure')):
                with self.assertRaises(PermissionError):
                    cli.write_codex_record(record, replacement)
            self.assertEqual(json.loads(record.read_text()), previous)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            cli.write_codex_record(record, replacement)
            self.assertEqual(json.loads(record.read_text()), replacement)

    def test_two_cold_launchers_start_exactly_one_server(self):
        try:
            import websockets.sync.server  # noqa: F401
        except ImportError:
            self.skipTest('requires the installed websockets dependency')
        with tempfile.TemporaryDirectory(prefix='trio-startup-') as directory:
            root = Path(directory)
            (root / 'fake_server.py').write_text(FAKE_SERVER, encoding='utf-8')
            (root / 'caller.py').write_text(CALLER, encoding='utf-8')
            (root / 'native.json').write_text(json.dumps({'codex_binary': sys.executable}), encoding='utf-8')
            env = dict(os.environ, NTH_HOME=directory, TRIO_TEST_SERVER=str(SERVER))
            callers = []
            try:
                callers = [subprocess.Popen([sys.executable, str(root / 'caller.py')], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
                deadline = time.monotonic() + 15
                while len(list(root.glob('ready-*'))) != 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(len(list(root.glob('ready-*'))), 2, 'both callers must reach the start barrier')
                (root / 'go').touch()
                outputs = [process.communicate(timeout=45) for process in callers]
                self.assertEqual([process.returncode for process in callers], [0, 0], outputs)
                replies = [json.loads(output) for output, _ in outputs]
                self.assertEqual(replies[0]['endpoint'], replies[1]['endpoint'])
                self.assertEqual(len(list(root.glob('spawn-*'))), 1, 'cold launches created competing servers')
                record = json.loads((root / 'events' / 'codex-server.json').read_text(encoding='utf-8'))
                self.assertEqual(record['endpoint'], replies[0]['endpoint'])
                self.assertTrue((root / ('spawn-' + str(record['pid']))).exists())
            finally:
                for process in callers:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
                # Only the fixture-owned server PIDs, never a saved/live runtime.
                for marker in root.glob('spawn-*'):
                    try:
                        os.kill(int(marker.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                # Windows closes the fixture's redirected log asynchronously.
                time.sleep(.2)


if __name__ == '__main__':
    unittest.main()
