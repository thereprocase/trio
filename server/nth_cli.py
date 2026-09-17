#!/usr/bin/env python3
"""Trio's local runtime, Codex and Claude launchers, and event-service controls."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

from nth_codex_socket import CodexSocketClient
from nth_event_service import (home, state_dir, ensure_service, add_endpoint,
                               public_status, register, configure_listener)


def settings():
    path = home() / 'native.json'
    # utf-8-sig, as setup.py reads it: a hand edit on Windows can add a byte-order mark.
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


def background_options():
    return ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == 'nt' else {'start_new_session': True})


# Characters cmd.exe interprets even inside an argument list.
CMD_METACHARACTERS = '&|<>^%!"\r\n'


def guard_command_shim(executable, arguments, platform=None):
    """Refuse arguments that a Windows .cmd/.bat launcher would reinterpret.

    An npm-installed CLI resolves to a .cmd shim. Windows runs it through cmd.exe,
    which parses the command line again: an argument `a&b` runs `a` and then
    executes `b`. A list passed to subprocess does not protect against that.
    """
    if (platform or os.name) != 'nt' or not str(executable).lower().endswith(('.cmd', '.bat')):
        return
    for argument in arguments:
        if any(character in argument for character in CMD_METACHARACTERS):
            raise ValueError(
                str(executable) + ' is a .cmd launcher, and cmd.exe would reinterpret characters such as '
                '& | < > ^ % ! " in the argument ' + repr(argument[:60]) + '. Start without that argument '
                'and type it inside the session, or point Trio at a native executable '
                '(python setup.py install --claude-binary PATH / --codex-binary PATH).')


def run_foreground(command, **options):
    """Run the program the user asked for as if they had started it themselves.

    Ctrl+C belongs to that program: it shares this terminal and receives the
    interrupt itself. Left alone, Python would raise KeyboardInterrupt here and
    subprocess.call would then kill a session that only meant to cancel a prompt.
    A handler, not SIG_IGN: an ignored signal would be inherited by the program.
    """
    try:
        previous = signal.signal(signal.SIGINT, lambda *_: None)
    except ValueError:                      # not the main thread: nothing to guard
        return subprocess.call(command, **options)
    try:
        return subprocess.call(command, **options)
    finally:
        signal.signal(signal.SIGINT, previous)


def startable(saved):
    """True when a saved binary can still be started. A bare command name is for PATH
    to decide; a path is a file that exists, with or without a Windows extension."""
    saved = str(saved)
    if not os.path.dirname(saved):
        return bool(shutil.which(saved))
    extensions = [''] + (os.environ.get('PATHEXT', '').split(os.pathsep) if os.name == 'nt' else [])
    return any(Path(saved + extension).is_file() for extension in extensions)


def resolve_binary(name, saved_key, label, install_flag, override=None):
    saved = override or settings().get(saved_key)
    if saved:
        if startable(saved):
            return str(saved)
        # A saved path inside a versioned directory disappears with the next update.
        # With the command aliased to this launcher, that must not end every launch.
        found = shutil.which(name)
        if not found:
            raise RuntimeError(f'The saved {saved_key} {saved!r} does not exist. Re-run: '
                               f'python setup.py install {install_flag} /full/path/to/{name}')
        print(f'[trio] the saved {saved_key} {saved!r} no longer exists; using {found}, which may be a '
              f'different version. Save the new one: python setup.py install {install_flag} PATH',
              file=sys.stderr)
        return found
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'{label} was not found on PATH and no {saved_key} is saved. Install it, or '
                           f'point Trio at an existing one: python setup.py install {install_flag} '
                           f'/full/path/to/{name}')
    return found


def codex_binary(override=None):
    saved = override or settings().get('codex_binary')
    if saved and not startable(saved):
        # The Codex app keeps its CLI in a versioned directory that an update removes.
        # With `codex` aliased to this launcher, a stale path must not end every launch.
        found = shutil.which('codex')
        if found:
            print(f'[trio] the saved codex_binary {saved!r} no longer exists; using {found}, which may '
                  'be a different version. Save the new one: python setup.py install --codex-binary PATH',
                  file=sys.stderr)
        saved = found
    executable = saved or shutil.which('codex')
    if not executable:
        raise RuntimeError('Codex is not installed; install the stock Codex CLI first')
    return executable


CHANNEL_FLAG = '--dangerously-load-development-channels'
CHANNEL_SERVERS = (('nth-trio', 'nth_server.py'), ('nth-qweb', 'nth_quartet_proxy.py'))
# Claude Code invocations that open no interactive session (2.1.274). With `claude`
# aliased to `trio claude` these must reach the real binary exactly as typed: the
# channel flag means nothing to them, and its confirmation would stall a script.
# A subcommand added by a later release is not known here and gets the flag; if
# Claude refuses it, run the real binary by its path and add the name to this set.
CLAUDE_SUBCOMMANDS = frozenset((
    'agents', 'attach', 'auth', 'auto-mode', 'doctor', 'gateway', 'import', 'install', 'logs', 'mcp',
    'plugin', 'plugins', 'project', 'respawn', 'rm', 'setup-token', 'stop', 'kill', 'ultrareview',
    'update', 'upgrade'))
# --bg detaches at once: nobody is there to answer the flag's confirmation. Untested
# with channels, so it is left exactly as plain Claude Code runs it.
CLAUDE_ONE_SHOT_FLAGS = frozenset(('-p', '--print', '-h', '--help', '-v', '--version',
                                   '--bg', '--background'))
# Options may come before a subcommand (`claude --debug mcp list`), so a word that
# is an option's value must not be read as one. From `claude --help`, 2.1.274: an
# option written <value> or [value] takes the next word, one written <values...>
# takes every word up to the next option.
CLAUDE_VALUE_OPTIONS = frozenset((
    '--agent', '--agents', '--append-system-prompt', '--append-system-prompt-file', '--autocompact',
    '--cloud', '-d', '--debug', '--debug-file', '--effort', '--environment', '--fallback-model',
    '--from-pr', '--input-format', '--json-schema', '--max-budget-usd', '--model', '-n', '--name',
    '--output-format', '--permission-mode', '--permission-prompts', '--plugin-dir', '--plugin-url',
    '--prompt-suggestions', '--remote-control', '--remote-control-session-name-prefix', '-r', '--resume',
    '--session-id', '--setting-sources', '--settings', '--system-prompt', '--system-prompt-file',
    '--system-prompt-snapshot', '--teleport', '-w', '--worktree'))
CLAUDE_LIST_OPTIONS = frozenset((
    '--add-dir', '--allowedTools', '--allowed-tools', '--betas', '--disallowedTools', '--disallowed-tools',
    '--file', '--mcp-config', '--tools', CHANNEL_FLAG))
# What MSYS2 and Cygwin name the pipes they hand a native Windows program when
# their terminal runs without a pseudo console.
MSYS_TERMINAL_PIPE = re.compile(r'\\(msys|cygwin)-[0-9a-f]+-pty\d+-(from|to)-master', re.IGNORECASE)


# The same for Codex (codex-cli 0.154.0). Only the subcommands whose own --help lists
# --remote can use Trio's shared app-server; the rest must reach the real binary as
# typed, and must not start that server. Codex accepts options before a subcommand
# (`codex -C app resume`), so the options that take a value are listed: their
# values are never read as a subcommand. `-p` is --profile here, not --print.
CODEX_SUBCOMMANDS = frozenset((
    'agents', 'exec', 'review', 'login', 'logout', 'mcp', 'plugin', 'app-server', 'remote-control', 'app',
    'completion', 'update', 'doctor', 'sandbox', 'debug', 'apply', 'resume', 'queue', 'archive', 'delete',
    'migrate-rollouts', 'unarchive', 'fork', 'cloud', 'exec-server', 'features', 'help',
    'e', 'a'))                                  # documented aliases of exec and apply
CODEX_REMOTE_SUBCOMMANDS = frozenset(('agents', 'resume', 'queue', 'archive', 'delete', 'unarchive', 'fork'))
CODEX_VALUE_OPTIONS = frozenset((
    '-c', '--config', '--enable', '--disable', '--remote', '--remote-auth-token-env', '-i', '--image',
    '-m', '--model', '--local-provider', '-p', '--profile', '-s', '--sandbox', '-C', '--cd', '--add-dir',
    '-a', '--ask-for-approval'))
CODEX_ONE_SHOT_FLAGS = frozenset(('-h', '--help', '-V', '--version'))


def claude_binary(override=None):
    return resolve_binary('claude', 'claude_binary', 'Claude Code', '--claude-binary', override)


def claude_config_path():
    directory = os.environ.get('CLAUDE_CONFIG_DIR')
    candidates = ([Path(directory) / '.claude.json'] if directory else []) + [Path.home() / '.claude.json']
    return next((path for path in candidates if path.exists()), candidates[-1])


def frontend_root():
    """The directory this launcher's own frontends live in: the installed server directory."""
    return Path(__file__).resolve().parent


def registration_problem(name, script, registered):
    """Why this registered MCP server must not be named as a channel, or ''.

    The development-channels flag lets the named server insert text into the
    session unasked. That grant is only acceptable for Trio's own local stdio
    frontends. `setup.sh` registers nth-qweb as a remote SSE server and nth-trio
    without the client marker: naming those would hand the grant to a network
    server, or to a frontend that never pushes.
    """
    if not isinstance(registered, dict):
        return 'is not registered for Claude'
    if registered.get('type', 'stdio') != 'stdio' or 'command' not in registered:
        return 'is registered as a remote (' + str(registered.get('type')) + ') server, not a local stdio one'
    # What is checked is what the registration executes: a Python interpreter whose
    # first argument is THIS installation's frontend. A file that merely shares the
    # script's name, anywhere else on disk, is not Trio.
    expected = frontend_root() / script
    arguments = [str(argument) for argument in (registered.get('args') or [])]
    try:
        runs_ours = (bool(arguments) and expected.exists() and
                     os.path.normcase(str(Path(arguments[0]).resolve())) == os.path.normcase(str(expected.resolve())))
    except OSError:
        runs_ours = False
    if not runs_ours:
        return 'does not run this installation\'s ' + script + ' (' + str(expected) + ')'
    if not Path(str(registered.get('command') or '')).stem.lower().startswith('python'):
        return 'is not started by a Python interpreter'
    if (registered.get('env') or {}).get('TRIO_NATIVE_CLIENT') != 'claude':
        return 'lacks TRIO_NATIVE_CLIENT=claude, so it would never push'
    return ''


class ChannelRefused(RuntimeError):
    """The channel grant was refused. The session itself can still start, without it."""


def launch_directory():
    return Path.cwd()


def _same_directory(left, right):
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def shadowing_registrations(name, config, directory):
    """Registrations of `name` that outrank the user-scoped one for a session started
    in `directory`, as (where, registration) pairs.

    Claude Code resolves a server name by scope: local, then project, then user.
    Local scope is this same file's `projects[DIRECTORY].mcpServers`; project scope
    is a `.mcp.json`. The channel flag names a server by name only, so whichever
    of them Claude picks receives the grant. The directory and every ancestor are
    searched rather than modelling which one Claude treats as the project.
    A registration of None stands for one that could not be read: what cannot be
    validated is not trusted.
    """
    directories = [directory, *directory.parents]
    try:
        resolved = directory.resolve()
        directories += [d for d in (resolved, *resolved.parents)
                        if not any(_same_directory(d, known) for known in directories)]
    except OSError:
        pass
    found = []
    projects = config.get('projects') if isinstance(config, dict) else None
    for key, project in (projects.items() if isinstance(projects, dict) else ()):
        servers = project.get('mcpServers') if isinstance(project, dict) else None
        if isinstance(servers, dict) and name in servers and any(_same_directory(key, d) for d in directories):
            found.append((f'local scope for {key}', servers[name]))
    for candidate in directories:
        project_file = candidate / '.mcp.json'
        try:
            raw = project_file.read_text(encoding='utf-8-sig')
        except OSError:
            continue
        try:
            servers = json.loads(raw).get('mcpServers')
        except (ValueError, AttributeError):
            servers = None
            if name in raw:
                found.append((str(project_file), None))
        if isinstance(servers, dict) and name in servers:
            found.append((str(project_file), servers[name]))
    return found


def shadow_problem(name, script, config, directory):
    """Why a higher-precedence registration of `name` forbids naming it, or ''."""
    for where, registered in shadowing_registrations(name, config, directory):
        problem = (registration_problem(name, script, registered) if registered is not None
                   else 'could not be read, so it cannot be checked')
        if problem:
            return (f'is also registered in {where}, which outranks the installed one, and that '
                    f'registration {problem}. Remove it (claude mcp remove --scope local {name}, or delete '
                    f'the entry from .mcp.json) or start the session from another directory')
    return ''


def channel_servers():
    """The `server:NAME` entries that may be named, after checking each registration."""
    path = claude_config_path()
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        registered = config.get('mcpServers') or {}
    except (OSError, ValueError, AttributeError):
        raise ChannelRefused(f'Could not read Claude\'s MCP configuration at {path}. '
                             'Run: python setup.py install') from None
    wanted = [entry for entry in CHANNEL_SERVERS if entry[0] == 'nth-trio' or settings().get('quartet_url')]
    named = []
    directory = launch_directory()
    for name, script in wanted:
        # The installed registration, and every same-name one that outranks it from
        # here: the flag grants by name, and Claude decides which registration that is.
        problem = registration_problem(name, script, registered.get(name))
        remedy = ('Re-run: python setup.py install' + (' --quartet-url URL' if name == 'nth-qweb' else '')
                  + '   (setup.sh does not register it for channel delivery).')
        if not problem:
            # Reinstalling does not remove a registration in another scope.
            problem, remedy = shadow_problem(name, script, config, directory), ''
        if not problem:
            named.append('server:' + name)
        elif name == 'nth-trio':
            raise ChannelRefused(f'{name} {problem}. It will not be named as a channel. {remedy}'.strip())
        else:
            print(f'[trio] {name} {problem}: Quartet messages will NOT be pushed into this session. '
                  + remedy, file=sys.stderr)
    return named


def claude_command(arguments, binary=None):
    """Claude Code's argv for a session that accepts Trio/Quartet channel events.

    Channels are a research preview and these servers are not on Anthropic's
    allowlist, so the host only registers them when the development flag names
    them. The names are the ones setup.py registers in Claude's own MCP config;
    a server supplied through --mcp-config is not visible to channel registration.
    """
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    servers = channel_servers()
    executable = claude_binary(binary)
    guard_command_shim(executable, arguments)
    if CHANNEL_FLAG in arguments:
        # The user named development channels of their own. The flag takes a list,
        # so Trio's servers join that list rather than repeating the flag.
        at = arguments.index(CHANNEL_FLAG) + 1
        return [executable, *arguments[:at], *servers, *arguments[at:]]
    if '--' in arguments:
        # Everything after a bare `--` is the prompt: a flag placed there would be
        # read as text. The separator also ends the flag's list.
        at = arguments.index('--')
        return [executable, *arguments[:at], CHANNEL_FLAG, *servers, *arguments[at:]]
    # The flag goes last so that its list cannot swallow the user's own prompt.
    return [executable, *arguments, CHANNEL_FLAG, *servers]


def claude_environment():
    # The host declares nothing about channels to an MCP server, so the launcher
    # tells the frontends. Claude Code passes its environment on to the stdio
    # servers it spawns. Child processes inherit it too: a nested session must be
    # started with `trio claude` as well, or it would expect events it cannot get.
    return dict(os.environ, TRIO_CLAUDE_CHANNEL='1')


def msys_terminal(stream):
    """True when `stream` is the pipe of an MSYS2 or Cygwin terminal (Git Bash's
    mintty) that runs without a pseudo console. A person is typing there, but a
    native Windows program is handed pipes and isatty() says no."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                          ctypes.c_void_p, wintypes.DWORD]
        handle = msvcrt.get_osfhandle(stream.fileno())
        if kernel32.GetFileType(handle) != 3:                       # FILE_TYPE_PIPE
            return False
        buffer = ctypes.create_string_buffer(4 + 2 * 512)
        if not kernel32.GetFileInformationByHandleEx(handle, 2, buffer, len(buffer)):   # FileNameInfo
            return False
        length = int.from_bytes(buffer.raw[:4], 'little')
        return bool(MSYS_TERMINAL_PIPE.match(buffer.raw[4:4 + length].decode('utf-16-le', 'replace')))
    except Exception:  # noqa: BLE001 - a probe: any failure means "not known to be a terminal"
        return False


def terminal_attached():
    """True when a person is at both ends. An interactive session needs a terminal."""
    try:
        return all(stream.isatty() or msys_terminal(stream) for stream in (sys.stdin, sys.stdout))
    except (AttributeError, ValueError):
        return False


def claude_subcommand(arguments):
    """The subcommand a Claude Code argv names, or '' for a session."""
    skip = listing = False
    for argument in arguments:
        if argument == '--':
            break
        if argument.startswith('-'):
            name = argument.split('=', 1)[0]
            skip = name in CLAUDE_VALUE_OPTIONS and '=' not in argument
            listing = name in CLAUDE_LIST_OPTIONS
        elif skip:
            skip = False
        elif not listing:
            # The first word that is no option's value: a subcommand, or the prompt.
            return argument if argument in CLAUDE_SUBCOMMANDS else ''
    return ''


def claude_session_wanted(arguments, terminal=True):
    """False for an invocation that opens no interactive session: a subcommand, a
    one-shot flag, or no terminal to hold one."""
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    if not terminal or claude_subcommand(arguments):
        return False
    # Options end at a bare `--`: what follows is the prompt, whatever it looks like.
    options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    # Short flags combine: `-pc` is --print --continue. A cluster that starts with a
    # short option taking a value is that option with its value attached: `-dapi`
    # is --debug api, not a print run.
    return not any(argument in CLAUDE_ONE_SHOT_FLAGS
                   or (re.fullmatch(r'-[A-Za-z]*[phv][A-Za-z]*', argument) and argument[1] not in 'dnrw')
                   for argument in options)


def claude_passthrough(arguments, binary=None):
    """Claude Code's argv exactly as typed, for an invocation that is not a session.

    Claude's MCP configuration is not checked here: `claude mcp ...` is how a
    broken registration gets repaired.
    """
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    executable = claude_binary(binary)
    guard_command_shim(executable, arguments)
    return [executable, *arguments]


def codex_subcommand(arguments):
    """The subcommand a Codex argv names, or '' for the interactive form."""
    skip = False
    for argument in arguments:
        if skip:
            skip = False
        elif argument == '--':
            break
        elif argument in CODEX_VALUE_OPTIONS:
            skip = True
        elif not argument.startswith('-'):
            # The first word that is no option's value: a subcommand, or the prompt.
            return argument if argument in CODEX_SUBCOMMANDS else ''
    return ''


def codex_session_wanted(arguments, terminal=True):
    """True when this argv should run against Trio's shared app-server."""
    arguments = list(arguments)
    options = arguments[:arguments.index('--')] if '--' in arguments else arguments
    if any(argument in CODEX_ONE_SHOT_FLAGS for argument in options):
        return False
    if any(argument == '--remote' or argument.startswith('--remote=') for argument in options):
        # The user chose an app-server of their own. Codex rejects a second --remote.
        return False
    subcommand = codex_subcommand(arguments)
    if subcommand:
        return subcommand in CODEX_REMOTE_SUBCOMMANDS
    # The interactive form needs a person at a terminal.
    return terminal


def launch_codex(arguments):
    arguments = list(arguments)
    if arguments[:1] == ['--']:
        arguments = arguments[1:]
    if not codex_session_wanted(arguments, terminal_attached()):
        if codex_session_wanted(arguments):
            print('[trio] no terminal on stdin and stdout: starting Codex without Trio\'s shared server',
                  file=sys.stderr)
        binary = codex_binary()
        guard_command_shim(binary, arguments)
        return run_foreground([binary, *arguments])
    try:
        endpoint, binary = ensure_codex()
    except RuntimeError as problem:
        # Trio may fail to add delivery; it must never fail to start the tool. With
        # `codex` aliased to this launcher, a server that did not come up would
        # otherwise leave the user with an error and no Codex.
        print(f'[trio] {problem}\n[trio] Starting Codex WITHOUT Trio\'s shared server: this session '
              'cannot receive pushed messages.', file=sys.stderr)
        binary = codex_binary()
        guard_command_shim(binary, arguments)
        return run_foreground([binary, *arguments])
    guard_command_shim(binary, arguments)
    return run_foreground([binary, '--remote', endpoint, *arguments])


def plain_environment():
    # Inside a `trio claude` session the variable is inherited. A one-shot child
    # gets no channel from its host, and its frontends must not believe otherwise.
    return {name: value for name, value in os.environ.items() if name != 'TRIO_CLAUDE_CHANNEL'}


def shell_init(shell, clients=('claude', 'codex')):
    """Shell functions that make the plain commands start Trio's launchers.

    Printed, never installed: a shell profile is the user's to edit. The functions
    call this interpreter and this file by path. On Windows that bypasses the
    `trio.cmd` launcher, through which cmd.exe would reinterpret the arguments.
    """
    python, cli = sys.executable, str(Path(__file__).resolve())
    lines = []
    for name in clients:
        if shell == 'powershell':
            call = '& ' + ' '.join("'" + part.replace("'", "''") + "'" for part in (python, cli, name)) + ' @args'
            # A function receives piped input in $input; a native command does not
            # see it unless it is passed on.
            lines.append(f'function {name} {{ if ($MyInvocation.ExpectingInput) {{ $input | {call} }} '
                         f'else {{ {call} }} }}')
        else:
            lines.append(f'{name}() {{ {shlex.join([python, cli, name])} "$@"; }}')
    return '\n'.join(lines)


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
            # The server still runs, but an update may have removed the file it was
            # started from: the client is resolved afresh, with the same fallback.
            return existing['endpoint'], codex_binary(existing['binary'])
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
        return launch_codex(argv[1:])
    # Likewise for Claude Code. Channel delivery runs inside Claude's own MCP
    # frontends, so no Codex server or event service is started here.
    if argv[:1] == ['claude']:
        os.umask(0o077)
        if not claude_session_wanted(argv[1:], terminal_attached()):
            if claude_session_wanted(argv[1:]):
                print('[trio] no terminal on stdin and stdout: starting Claude Code without channel '
                      'delivery', file=sys.stderr)
            return run_foreground(claude_passthrough(argv[1:]), env=plain_environment())
        try:
            command = claude_command(argv[1:])
        except ChannelRefused as refusal:
            # The grant is refused, not the session. With `claude` aliased to this
            # launcher, refusing to start would let a repository's .mcp.json turn
            # `claude` into a dead command inside it.
            print(f'[trio] {refusal}\n[trio] Starting Claude Code WITHOUT channel delivery. A channel joined '
                  'from this session uses the Monitor.', file=sys.stderr)
            return run_foreground(claude_passthrough(argv[1:]), env=plain_environment())
        return run_foreground(command, env=claude_environment())
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('start', help='Start the local event service')
    sub.add_parser('status', help='Show Codex delivery health without credentials (Claude channel '
                                  'listeners live inside the Claude session: ask the agent to call '
                                  '*_delivery_status)')
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
    # Listed for --help only: the argv check above handles every `trio claude`.
    sub.add_parser('claude', help='Launch Claude Code with Trio/Quartet events pushed into the session. '
                                  'Claude Code asks you to confirm a development-channels flag at every '
                                  'launch: see AGENT-RUNTIME.md')
    init = sub.add_parser('shell-init', help='Print shell functions that make plain `claude` and `codex` '
                                             'start through Trio. Add the output to your shell profile')
    init.add_argument('shell', choices=['powershell', 'bash', 'zsh'])
    init.add_argument('--clients', default='claude,codex',
                      help='Which commands to wrap: claude, codex or claude,codex (default)')
    desktop = sub.add_parser('desktop', help='Launch the Codex app against Trio\'s shared server')
    desktop.add_argument('--app', help='Installed app executable (or saved codex_app in native.json)')
    desktop.add_argument('--isolated', action='store_true', help='Use a separate app UI profile')
    args, extra = parser.parse_known_args(argv)
    if extra and args.command != 'codex':
        parser.error('unrecognized arguments: ' + ' '.join(extra))
    os.umask(0o077)
    if args.command == 'status':
        print(json.dumps({'listeners': public_status(),
                          'claude_channels': 'not shown here: a channel listener lives inside the Claude '
                                             'session, so ask the agent to call *_delivery_status'}, indent=2))
    elif args.command == 'shell-init':
        clients = tuple(args.clients.split(','))
        if not clients or any(client not in ('claude', 'codex') for client in clients):
            parser.error('--clients must contain claude and/or codex')
        print(shell_init(args.shell, clients))
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
        return launch_codex(extra + args.arguments)
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
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
