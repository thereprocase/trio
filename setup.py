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
            claude_binary=None):
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
                             'env': {'TRIO_NATIVE_CLIENT': 'claude', 'NTH_HOME': str(runtime)}}
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
            subprocess.run([str(executable), 'mcp', 'add', name,
                            '--env', 'TRIO_NATIVE_CLIENT=codex', '--env', 'NTH_HOME=' + str(runtime),
                            '--', str(python), *arguments],
                           env=dict(os.environ, CODEX_HOME=str(codex_home)), check=True)
    bin_dir = target_home / '.local' / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / ('trio.cmd' if os.name == 'nt' else 'trio')
    backup(launcher)
    if os.name == 'nt':
        launcher.write_text('@echo off\n"' + str(python) + '" "' + str(server / 'nth_cli.py') + '" %*\n')
    else:
        launcher.write_text('#!/bin/sh\nexec ' + shlex.join([str(python), str(server / 'nth_cli.py')]) + ' "$@"\n')
        launcher.chmod(0o755)
    return {'launcher': str(launcher), 'python': str(python), 'server': str(server),
            'runtime': str(runtime), 'clients': list(clients)}


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
    return '\n'.join([
        '',
        'Next steps',
        '1. Restart Claude Code and Codex so that they load the installed servers.',
        '2. A session can receive pushed messages only if it was started through Trio. To make',
        '   plain `claude` and `codex` do that in every terminal, add two shell functions to your',
        '   profile (run once; re-run after moving or reinstalling Trio):',
        *profile,
        '   Then open a new terminal. Commands that open no session (claude mcp, claude -p,',
        '   codex exec, ...) behave exactly as before. Claude Code asks you to confirm a',
        '   development-channels flag at each launch: accept it. AGENT-RUNTIME.md explains what',
        '   that grants. To undo, delete the two functions from the profile.',
        '   Without this step, start sessions with `trio claude` and `trio codex`; a plainly',
        '   started Claude falls back to a Monitor that expires every 30 minutes.',
    ])


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
    args = parser.parse_args()
    clients = tuple(args.clients.split(','))
    if not clients or any(c not in ('claude', 'codex') for c in clients):
        parser.error('--clients must contain claude and/or codex')
    os.umask(0o077)
    result = install(args.home, quartet_url=args.quartet_url, clients=clients,
        skip_dependencies=args.skip_dependencies, register_codex=not args.no_register_codex,
        codex_binary=args.codex_binary, codex_app=args.codex_app,
        claude_binary=args.claude_binary)
    print(json.dumps(result, indent=2))
    # On stderr: stdout stays the machine-readable result.
    print(next_steps(result), file=sys.stderr)


if __name__ == '__main__':
    main()
