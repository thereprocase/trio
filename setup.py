#!/usr/bin/env python3
"""Install Trio natively for Claude and Codex, preserving unrelated settings."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
STAMP = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')


def backup(path):
    path = Path(path)
    if path.exists():
        copy = path.with_name(path.name + '.bak-' + STAMP)
        if not copy.exists():
            shutil.copy2(path, copy)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    temporary = path.with_name(path.name + '.tmp-' + STAMP)
    temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    if os.name != 'nt':
        temporary.chmod(0o600)
    temporary.replace(path)


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() == source.read_bytes():
        return
    backup(destination)
    shutil.copy2(source, destination)


def install(target_home, *, quartet_url='', clients=('claude', 'codex'),
            skip_dependencies=False, register_codex=True, codex_binary=None, codex_app=None,
            claude_binary=None, skip_systemd=False):
    target_home = Path(target_home).resolve()
    claude_home = target_home / '.claude'
    codex_home = Path(os.environ.get('CODEX_HOME', str(target_home / '.codex')))
    # --home staging must never write into the caller's real CODEX_HOME.
    if target_home != Path.home().resolve():
        codex_home = target_home / '.codex'
    runtime = claude_home / 'nth'
    server = claude_home / 'skills' / 'nth' / 'server'
    venv = runtime / 'venv'
    python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    runtime.mkdir(parents=True, exist_ok=True)
    if not skip_dependencies:
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--only-binary=:all:',
                        'mcp>=1.26,<2', 'websockets>=15,<16'], check=True)
    else:
        python = Path(sys.executable)
    for source in (ROOT / 'server').rglob('*'):
        if source.is_file() and '__pycache__' not in source.parts and source.suffix != '.pyc':
            copy_file(source, server / source.relative_to(ROOT / 'server'))
    for client in clients:
        base = claude_home if client == 'claude' else codex_home
        for flavor in ('trio', 'quartet'):
            destination = base / 'skills' / flavor
            for source_name, name in ((f'SKILL-{flavor}.md', 'SKILL.md'),
                                      (f'REFERENCE-{flavor}.md', 'REFERENCE.md'),
                                      (f'PROTOCOLS-{flavor}.md', 'PROTOCOLS.md'),
                                      ('DESIGN.md', 'DESIGN.md'),
                                      ('AGENT-RUNTIME.md', 'AGENT-RUNTIME.md')):
                if (ROOT / source_name).exists():
                    copy_file(ROOT / source_name, destination / name)
    config_path = runtime / 'native.json'
    config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
    if quartet_url:
        config['quartet_url'] = quartet_url
    if codex_binary:
        config['codex_binary'] = str(codex_binary)
    if codex_app:
        config['codex_app'] = str(codex_app)
    if claude_binary:
        config['claude_binary'] = str(claude_binary)
    write_json(config_path, config)
    quartet_url = config.get('quartet_url', '')
    if 'claude' in clients:
        path = target_home / '.claude.json'
        config = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        servers = config.setdefault('mcpServers', {})
        for name, script in [('nth-trio', 'nth_server.py'), ('nth-qweb', 'nth_quartet_proxy.py')]:
            if name == 'nth-qweb' and not quartet_url:
                continue
            arguments = [str(server / script)]
            if name == 'nth-qweb':
                arguments += ['--url', quartet_url]
            servers[name] = {'type': 'stdio', 'command': str(python), 'args': arguments,
                             'env': {'TRIO_NATIVE_CLIENT': 'claude', 'NTH_HOME': str(runtime), 'NTH_SERVER_NAME': name}}
        for registered, entry in servers.items():
            if isinstance(entry,dict) and registered.startswith('nth-') and any('nth_quartet_proxy' in str(a) for a in entry.get('args',[])):
                entry.setdefault('env',{})['NTH_SERVER_NAME']=registered
        write_json(path, config)
        settings_path = claude_home / 'settings.json'
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
        allow = settings.setdefault('permissions', {}).setdefault('allow', [])
        for name, prefix in [('nth-trio', 'trio'), ('nth-qweb', 'quartet')]:
            for operation in ('delivery_status', 'listen'):
                permission = f'mcp__{name}__{prefix}_{operation}'
                if permission not in allow:
                    allow.append(permission)
        sys.path.insert(0, str(ROOT / 'server'))
        import nth_claude_hook
        nth_claude_hook.install_hooks(settings, python, server / 'nth_claude_hook.py', runtime)
        write_json(settings_path, settings)
    if 'codex' in clients and register_codex:
        executable = codex_binary or shutil.which('codex')
        if not executable:
            raise RuntimeError('Codex CLI missing: supply --codex-binary or use --no-register-codex for staging')
        backup(codex_home / 'config.toml')
        for name, script in [('nth-trio', 'nth_server.py'), ('nth-qweb', 'nth_quartet_proxy.py')]:
            if name == 'nth-qweb' and not quartet_url:
                continue
            arguments = [str(server / script)]
            if name == 'nth-qweb':
                arguments += ['--url', quartet_url]
            # Codex passes an MCP server only a few variables of its own, CODEX_HOME not
            # among them: TRIO_CODEX_HOME tells the server where the delivery hooks live.
            subprocess.run([str(executable), 'mcp', 'add', name,
                            '--env', 'TRIO_NATIVE_CLIENT=codex', '--env', 'NTH_SERVER_NAME=' + name, '--env', 'NTH_HOME=' + str(runtime),
                            '--env', 'TRIO_CODEX_HOME=' + str(codex_home),
                            '--', str(python), *arguments],
                           env=dict(os.environ, CODEX_HOME=str(codex_home)), check=True)
        codex_hooks = install_codex_hooks(codex_home, python, server / 'nth_codex_hook.py', runtime)
    else:
        codex_hooks = None
    # Trust only operator MCP configuration. Never promote a frontend announcement.
    sys.path.insert(0, str(ROOT / 'server'))
    from nth_interposer_store import Store
    trust_store = Store(runtime / 'events' / 'interposer.sqlite')
    try:
        trust_store.import_hubs(target_home)
        if quartet_url and ('claude' in clients or ('codex' in clients and register_codex)):
            trust_store.setup_hub('nth-qweb', quartet_url)
    finally:
        trust_store.close()
    bin_dir = target_home / '.local' / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / ('trio.cmd' if os.name == 'nt' else 'trio')
    backup(launcher)
    if os.name == 'nt':
        launcher.write_text('@echo off\n"' + str(python) + '" "' + str(server / 'nth_cli.py') + '" %*\n')
    else:
        launcher.write_text('#!/bin/sh\nexec ' + shlex.join([str(python), str(server / 'nth_cli.py')]) + ' "$@"\n')
        launcher.chmod(0o755)
    result = {'launcher': str(launcher), 'python': str(python), 'server': str(server),
              'runtime': str(runtime), 'clients': list(clients)}
    # A staged --home is never permission to control the caller's user manager.
    if not skip_systemd and target_home == Path.home().resolve():
        result['interposer_systemd'] = install_interposer_units(target_home, python, server, runtime)
    if codex_hooks:
        result.update(codex_hooks)
    return result


def systemd_available(target_home, platform=None):
    if not (platform or sys.platform).startswith('linux') or not shutil.which('systemctl'):
        return False
    # A systemctl binary alone says nothing about a working user manager (WSL,
    # containers and ssh sessions can have the binary but no user systemd).
    try:
        result = subprocess.run(['systemctl', '--user', 'show-environment'],
                                env=dict(os.environ, HOME=str(target_home)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _unit_quote(value, *, expand_dollar=True):
    # ExecStart and Environment use systemd's quoting, not shell quoting.
    value = str(value)
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError('control characters are forbidden in unit paths')
    if expand_dollar:
        value = value.replace('$', '$$')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _listen_stream(path, manager_runtime):
    # ListenStream does not use ExecStart/Environment's quoted-string syntax.
    value = str(path)
    if any(c.isspace() or not c.isprintable() or c in '\'"\\' for c in value):
        raise ValueError('unsafe ListenStream path: whitespace, quotes, backslashes or controls')
    if path == manager_runtime / 'trio' / 'interposer.sock':
        return '%t/trio/interposer.sock'
    return value.replace('%', '%%')


def install_interposer_units(target_home, python, server, runtime, platform=None):
    try:
        return _install_interposer_units(target_home, python, server, runtime, platform)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # A service-manager outage must not throw away a completed native install.
        failure = 'failed: ' + type(exc).__name__
        print('Warning: interposer systemd setup ' + failure, file=sys.stderr)
        return failure


def _install_interposer_units(target_home, python, server, runtime, platform=None):
    target_home = Path(target_home).resolve()
    if target_home != Path.home().resolve() or not systemd_available(target_home, platform):
        return False
    sys.path.insert(0, str(ROOT / 'server'))
    from nth_interposer_wire import socket_path, stop_fallback, _user_runtime_dir
    selected_socket = socket_path(runtime)
    listen_stream = _listen_stream(selected_socket, _user_runtime_dir())
    directory = target_home / '.config' / 'systemd' / 'user'
    directory.mkdir(parents=True, exist_ok=True)
    socket_unit = '''[Unit]
Description=Trio spoke interposer socket

[Socket]
ListenStream={socket_path}
SocketMode=0600
DirectoryMode=0700
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
'''.format(socket_path=listen_stream)
    service_unit = ('[Unit]\nDescription=Trio spoke interposer\n\n[Service]\n' +
                    'ExecStart=' + _unit_quote(python) + ' ' +
                    _unit_quote(Path(server) / 'nth_interposer.py') + ' serve\n' +
                    'Environment=' + _unit_quote('NTH_HOME=' + str(runtime), expand_dollar=False) + '\n' +
                    'Environment=' + _unit_quote('NTH_INTERPOSER_SOCKET=' + str(selected_socket), expand_dollar=False) + '\n' +
                    'Restart=on-failure\nRestartSec=2\nRestartPreventExitStatus=75\n' +
                    'NoNewPrivileges=yes\nUMask=0077\nLockPersonality=yes\nRestrictRealtime=yes\n')
    # PrivateTmp requires filesystem namespaces for an unprivileged user unit.
    # Omit it so installations work when user namespaces are disabled.
    for name, body in (('trio-interposer.socket', socket_unit), ('trio-interposer.service', service_unit)):
        path = directory / name
        if path.exists() and path.read_bytes() == body.encode('utf-8'):
            continue
        backup(path)
        temporary = path.with_name(name + '.tmp-' + STAMP)
        temporary.write_text(body, encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(path)
    env = dict(os.environ, HOME=str(target_home))
    subprocess.run(['systemctl', '--user', 'daemon-reload'], env=env, check=True, timeout=15)
    # The fallback owns a different socket inode. End it before systemd binds;
    # hello alone is not sufficient authority to signal an arbitrary process.
    try:
        stop_fallback(timeout=5, path=selected_socket)
    except (EOFError, OSError) as exc:
        # An IPC outage must not prevent enabling the replacement socket unit.
        print('Warning: interposer fallback stop unavailable: ' + type(exc).__name__ +
              '; continuing socket setup', file=sys.stderr)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'trio-interposer.socket'],
                   env=env, check=True, timeout=15)
    running = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'trio-interposer.service'],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if running.returncode == 0:
        subprocess.run(['systemctl', '--user', 'restart', 'trio-interposer.service'],
                       env=env, check=True, timeout=15)
    elif running.returncode != 3:
        raise subprocess.CalledProcessError(running.returncode, running.args)
    return True


def install_codex_hooks(codex_home, python, script, runtime):
    """Register the Codex delivery hooks in CODEX_HOME/hooks.json, keeping every other hook.

    hooks.json rather than config.toml: Python has no TOML writer in its standard
    library, and rewriting config.toml would drop the user's comments. Codex loads
    both forms; it only warns when one layer uses both."""
    sys.path.insert(0, str(ROOT / 'server'))
    import nth_codex_hook
    path = nth_codex_hook.hooks_file(codex_home)
    try:
        data = nth_codex_hook.load_hooks_file(path)
    except ValueError as exc:
        raise RuntimeError(f'{path} is not valid hooks JSON ({exc}); fix or move it, then install again')
    before = json.dumps(data, sort_keys=True)
    nth_codex_hook.install_hooks(data, python, script, runtime)
    if json.dumps(data, sort_keys=True) != before:
        nth_codex_hook.save_hooks_file(path, data, STAMP)
    return {'codex_hooks': str(path), 'codex_toml_hooks': nth_codex_hook.toml_hooks_present(codex_home)}


def next_steps(result, platform=None):
    """What the user still has to do by hand. The installer never edits a shell profile."""
    launcher = result['launcher']
    if (platform or os.name) == 'nt':
        # Add-Content keeps an existing profile's encoding; `>>` in Windows
        # PowerShell 5.1 would append UTF-16 to a UTF-8 file.
        profile = ['     New-Item -ItemType Directory -Force (Split-Path $PROFILE) | Out-Null',
                   f'     & "{launcher}" shell-init powershell | Add-Content -Path $PROFILE']
    else:
        profile = [f'     {shlex.quote(launcher)} shell-init bash >> ~/.bashrc     (zsh: shell-init zsh >> ~/.zshrc)']
    hooks = 'claude' in result.get('clients', ())
    lines = [
        '',
        'Next steps',
        '1. Restart Claude Code and Codex so that they load the installed servers.',
    ]
    if hooks:
        lines += [
            '2. Delivery hooks were registered in Claude\'s settings.json: a plainly launched Claude,',
            '   however it was started, is now woken by a filtered message with no launch flag and no',
            '   Monitor. A session that never joins Trio just spawns a short-lived hook per turn. Remove',
            '   the hooks any time with `trio hooks-uninstall`.',
            '3. For the faster channel path (4-8 s, no per-turn process) and for Codex, make plain',
            '   `claude` and `codex` start through Trio in every terminal by adding two shell functions',
            '   to your profile (run once; re-run after moving or reinstalling Trio):',
        ]
    else:
        lines += [
            '2. To make plain `claude` and `codex` start through Trio in every terminal, add two shell',
            '   functions to your profile (run once; re-run after moving or reinstalling Trio):',
        ]
    lines += [
        *profile,
        '   Then open a new terminal. Commands that open no session (claude mcp, claude -p,',
        '   codex exec, ...) behave exactly as before. Claude Code asks you to confirm a',
        '   development-channels flag at each launch: accept it. AGENT-RUNTIME.md explains what',
        '   that grants. To undo, delete the two functions from the profile.',
    ]
    if result.get('codex_hooks'):
        lines += [
            '',
            'Codex delivery hooks were registered in ' + result['codex_hooks'] + '.',
            'ONE-TIME STEP: Codex runs new or changed hooks only after you trust them. Start `codex`;',
            'at "Hooks need review" choose "Trust all and continue" (or review them in /hooks).',
            'Until then a plainly launched Codex is not woken. Re-run this step after any reinstall',
            'that changes the hook commands. Only the shared Codex daemon is woken; a session is woken',
            'for every message that passes its filter, also after its window closes, until it ends.',
            'Remove the hooks with `trio hooks-uninstall`.',
        ]
        if result.get('codex_toml_hooks'):
            lines.append('Note: config.toml also declares hooks. Codex loads both and warns at startup '
                         'that one layer uses two forms.')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install'])
    parser.add_argument('--home', default=str(Path.home()))
    parser.add_argument('--quartet-url', default='')
    parser.add_argument('--clients', default='claude,codex')
    parser.add_argument('--codex-binary')
    parser.add_argument('--codex-app', help='Save the installed desktop executable for trio desktop')
    parser.add_argument('--claude-binary', help='Save a Claude Code executable for trio claude (default: claude on PATH)')
    parser.add_argument('--skip-dependencies', action='store_true', help='Use current Python for an isolated staging test')
    parser.add_argument('--no-register-codex', action='store_true', help='Stage files without changing Codex MCP settings')
    parser.add_argument('--skip-systemd', action='store_true', help='Skip user interposer socket/service installation')
    args = parser.parse_args()
    clients = tuple(args.clients.split(','))
    if not clients or any(c not in ('claude', 'codex') for c in clients):
        parser.error('--clients must contain claude and/or codex')
    os.umask(0o077)
    result = install(args.home, quartet_url=args.quartet_url, clients=clients,
        skip_dependencies=args.skip_dependencies, register_codex=not args.no_register_codex,
        codex_binary=args.codex_binary, codex_app=args.codex_app,
        claude_binary=args.claude_binary, skip_systemd=args.skip_systemd)
    print(json.dumps(result, indent=2))
    # On stderr: stdout stays the machine-readable result.
    print(next_steps(result), file=sys.stderr)


if __name__ == '__main__':
    main()
