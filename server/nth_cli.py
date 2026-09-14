#!/usr/bin/env python3
"""Trio's local runtime, Codex launchers, and event-service controls."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

from nth_codex_socket import CodexSocketClient
from nth_event_service import (home, state_dir, ensure_service, add_endpoint,
                               public_status, register, configure_listener)


def settings():
    path = home() / 'native.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def background_options():
    return ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == 'nt' else {'start_new_session': True})


def codex_binary(override=None):
    executable = override or settings().get('codex_binary') or shutil.which('codex')
    if not executable:
        raise RuntimeError('Codex is not installed; install the stock Codex CLI first')
    return executable


def mcp_overrides(endpoint):
    result = []
    config = settings()
    server = Path(__file__).resolve().parent
    for name, script in [('nth-trio', 'nth_server.py'), ('nth-qweb', 'nth_quartet_proxy.py')]:
        if name == 'nth-qweb' and not config.get('quartet_url'):
            continue
        arguments = [str(server / script)]
        if name == 'nth-qweb':
            arguments += ['--url', config['quartet_url']]
        values = {'command': sys.executable, 'args': arguments,
                  'env': {'TRIO_NATIVE_CLIENT': 'codex', 'TRIO_CODEX_ENDPOINT': endpoint,
                          'NTH_HOME': str(home()), 'NTH_QUIET': '1'}}
        for key, value in values.items():
            if isinstance(value, dict):
                for env_name, env_value in value.items():
                    result += ['-c', f'mcp_servers.{name}.env.{env_name}={json.dumps(env_value)}']
            else:
                result += ['-c', f'mcp_servers.{name}.{key}={json.dumps(value)}']
    return result


def connectable(endpoint):
    client = CodexSocketClient(endpoint)
    try:
        client.start(timeout=2)
        return True
    except Exception:
        return False
    finally:
        client.stop()


def ensure_codex(binary=None):
    record = state_dir() / 'codex-server.json'
    try:
        existing = json.loads(record.read_text())
        if connectable(existing['endpoint']):
            add_endpoint(existing['endpoint'], settings().get('quartet_url', ''))
            ensure_service()
            return existing['endpoint'], existing['binary']
    except (OSError, ValueError, KeyError):
        pass
    binary = codex_binary(binary)
    if os.name == 'nt':
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        endpoint = f'ws://127.0.0.1:{port}'
    else:
        endpoint = 'unix://' + str(state_dir() / 'codex.sock')
    command = [binary, *mcp_overrides(endpoint), 'app-server', '--listen', endpoint]
    log = open(state_dir() / 'codex-server.log', 'ab')
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=log, **background_options())
    log.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Codex server exited; inspect events/codex-server.log')
        if connectable(endpoint):
            record.write_text(json.dumps({'endpoint': endpoint, 'binary': binary, 'pid': process.pid}))
            add_endpoint(endpoint, settings().get('quartet_url', ''))
            ensure_service()
            return endpoint, binary
        time.sleep(.3)
    raise RuntimeError('Codex server did not become ready; inspect events/codex-server.log')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Forward Codex's complete argv unchanged, including flags with values.
    if argv[:1] == ['codex']:
        os.umask(0o077)
        endpoint, binary = ensure_codex()
        arguments = argv[1:]
        if arguments[:1] == ['--']:
            arguments = arguments[1:]
        return subprocess.call([binary, '--remote', endpoint, *arguments])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('start', help='Start the local event service')
    sub.add_parser('status', help='Show delivery health without credentials')
    attach = sub.add_parser('attach', help='Observe an existing owning Codex endpoint')
    attach.add_argument('--endpoint', required=True)
    attach.add_argument('--quartet-url', default='')
    bind = sub.add_parser('bind', help='Explicit recovery binding from a private identity file')
    bind.add_argument('--identity', required=True)
    bind.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID', ''))
    bind.add_argument('--endpoint', required=True)
    bind.add_argument('--filter', choices=['all', 'about', 'at'], default='about')
    launch = sub.add_parser('codex', help='Launch stock Codex with native Trio/Quartet events')
    launch.add_argument('arguments', nargs=argparse.REMAINDER)
    desktop = sub.add_parser('desktop', help='Launch the Codex app against Trio\'s shared server')
    desktop.add_argument('--app', help='Installed app executable (or saved codex_app in native.json)')
    desktop.add_argument('--isolated', action='store_true', help='Use a separate app UI profile')
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != 'codex':
        parser.error('unrecognized arguments: ' + ' '.join(extra))
    os.umask(0o077)
    if args.command == 'status':
        print(json.dumps({'listeners': public_status()}, indent=2))
    elif args.command == 'start':
        print(json.dumps(ensure_service()))
    elif args.command == 'attach':
        if not connectable(args.endpoint):
            raise RuntimeError('The owning Codex endpoint is not reachable')
        add_endpoint(args.endpoint, args.quartet_url or settings().get('quartet_url', ''))
        print(json.dumps(ensure_service()))
    elif args.command == 'bind':
        from nth_codex_relay import load_binding
        identity_path = Path(args.identity)
        if os.name != 'nt' and identity_path.stat().st_mode & 0o077:
            raise ValueError('Identity file must be private (chmod 600)')
        identity = json.loads(identity_path.read_text(encoding='utf-8'))
        identity.update(endpoint=args.endpoint, thread_id=args.thread_id, filter=args.filter)
        print(json.dumps({'binding_id': register(identity, replace=True)}))
        ensure_service()
    elif args.command == 'codex':
        endpoint, binary = ensure_codex()
        arguments = extra + args.arguments
        if arguments[:1] == ['--']:
            arguments = arguments[1:]
        return subprocess.call([binary, '--remote', endpoint, *arguments])
    elif args.command == 'desktop':
        app = args.app or settings().get('codex_app')
        if not app:
            raise ValueError('Supply --app with the installed app executable or install with --codex-app')
        endpoint, _ = ensure_codex()
        env = dict(os.environ, CODEX_APP_SERVER_WS_URL=endpoint)
        env.pop('CODEX_APP_SERVER_FORCE_CLI', None)
        command = [app]
        if args.isolated:
            command += ['--user-data-dir=' + str(home() / 'codex-app-profile')]
        # A desktop window is explicitly requested here. Background helpers are
        # hidden; the interactive application itself is intentionally visible.
        subprocess.Popen(command, env=env)
        print('Codex app launch requested with Trio event delivery.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
