#!/usr/bin/env python3
"""Push delivery for a plainly launched Codex CLI, through its own hooks.

The Codex counterpart of nth_claude_hook.py, and built on it: the session state,
the membership records, the per-message filter, the rate limit and the wake text
are that module's. What differs is how a wake reaches the session. Codex hooks
run synchronously inside the Codex app-server daemon and cannot wake an idle
thread, so this hook starts a detached waiter and returns at once; on the first
message that passes the filter the waiter runs

    codex queue --thread <session id> --message <wake text>

which starts a turn in an idle thread, or queues one behind a running turn.
`setup.py` registers the hook in Codex's hooks.json for five events:

    tool    PostToolUse on the Trio/Quartet connect, listen and ack tools of any
            nth-* server: note which membership this session holds, then arm
    stop    Stop, after every turn: arm again if this session holds memberships
    start   SessionStart (startup, resume): a resumed session takes its
            memberships back and arms
    prompt  UserPromptSubmit: a person typed, so the unattended-wake budget
            starts again (a queued wake also arrives as a prompt; it does not count)
    end     SessionEnd: this session is over, its waiter leaves

The hook arms only inside the shared Codex app-server daemon, the server
`codex queue` reaches. In any other host (a TUI without the daemon, the desktop
app, an IDE's app-server) a queued wake could run the thread in a second server,
so it stands down and says why in the session's status file.

A closed window does not end a session while wakes keep it busy: the daemon
unloads a thread only after it has been idle for about a minute, and every wake
is activity. The waiter therefore stops after TRIO_CODEX_UNATTENDED_WAKES wakes
(default 10; 0 means no limit) with nobody typing, and stays paused until a
person types in the session or resumes it.

Arming spawns `nth_codex_hook.py wait` in a new session with stdin, stdout and
stderr on the null device: Codex waits for the hook's pipes to close, and kills
the hook's process group when its timeout expires. The waiter queues one wake and
exits; the Stop hook of the turn that wake starts arms the next waiter.

The wake is the same fixed sentence a Claude session gets, built from integers
and sanitized identifiers. It reaches the thread as a user message, so it never
carries message text or a sender's name: the agent reads those through the poll
tool, as untrusted data.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nth_claude_hook as core  # noqa: E402

# Codex reports an MCP tool as mcp__<server>__<tool>, with every character of the
# server name outside [A-Za-z0-9_] replaced by "_": nth-qweb becomes nth_qweb.
# Hyphens are accepted too, in case that rule changes; "__" never occurs inside.
HOOK_TOOLS = re.compile(r'^mcp__(nth[-_][A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)__(trio|quartet)_(connect|listen|ack)$')

# ---- registration in Codex's hooks.json --------------------------------------------

HOOK_SCRIPT = 'nth_codex_hook.py'
# Codex matchers are regular expressions searched anywhere in the name, so this
# one is anchored. "startup|resume" is matched exactly (only letters and "|").
TOOL_MATCHER = r'^mcp__nth[-_][A-Za-z0-9_-]*__(trio|quartet)_(connect|listen|ack)$'
HOOK_EVENTS = (('PostToolUse', 'tool', TOOL_MATCHER), ('Stop', 'stop', None),
               ('SessionStart', 'start', 'startup|resume'), ('UserPromptSubmit', 'prompt', None),
               ('SessionEnd', 'end', None))
# The hook only records and spawns, so it returns in well under a second. A sync
# hook holds up the turn while it runs: keep the ceiling short.
HOOK_TIMEOUT_SECONDS = 30
# Codex clamps a SessionEnd hook to three seconds.
END_TIMEOUT_SECONDS = 3

# ---- the waiter ----------------------------------------------------------------------

# Listeners poll at most once a second, so a gather window of a few polls turns a
# burst into one wake. The wake itself takes about six seconds to start a turn.
SETTLE_SECONDS = 2.5
# Codex's daemon outlives any one session, so the waiter leaves after a day even
# while the daemon runs. Each turn's Stop arms a fresh one.
LIFETIME_SECONDS = 24 * 3600.0
# How long a new waiter waits for a predecessor that is just delivering its wake.
LOCK_PATIENCE_SECONDS = 10.0
QUEUE_TIMEOUT_SECONDS = 30
QUEUE_ATTEMPTS = 3
QUEUE_RETRY_SECONDS = 2.0
# How often a waiter checks whether Trio's own event service has taken the thread.
RELAY_CHECK_SECONDS = 30.0
# Wakes allowed with nobody typing in the session, before delivery pauses.
DEFAULT_UNATTENDED_WAKES = 10
# Every queued notice starts with this, and a prompt made only of such lines is a
# wake, not a person: see is_wake_notice.
NOTICE_PREFIX = 'Trio delivery'
# Processes that may stand between the daemon and this hook: Codex runs a hook as
# `$SHELL -lc <command>` (cmd.exe /C on Windows), and the shell may or may not exec.
SHELLS = frozenset(('sh', 'bash', 'zsh', 'dash', 'ash', 'ksh', 'mksh', 'fish', 'tcsh', 'csh',
                    'busybox', 'nu', 'env', 'cmd', 'cmd.exe', 'powershell', 'powershell.exe',
                    'pwsh', 'pwsh.exe'))


def codex_home():
    """Codex's home directory. Codex passes only a few variables to an MCP server,
    CODEX_HOME not among them, so setup.py states it as TRIO_CODEX_HOME."""
    for variable in ('TRIO_CODEX_HOME', 'CODEX_HOME'):
        if os.environ.get(variable):
            return Path(os.environ[variable])
    return Path.home() / '.codex'


def hooks_file(home=None):
    return Path(home or codex_home()) / 'hooks.json'


def _is_trio_command(command):
    return (isinstance(command, str)
            and re.search(r'(^|[\\/"\'\s])' + re.escape(HOOK_SCRIPT) + r'(["\'\s]|$)', command) is not None)


def is_trio_group(group):
    """Whether a hooks.json matcher group is Trio's own: every handler in it runs this
    script. hooks.json has no room for a tag (Codex rejects unknown top-level keys and
    keeps nothing else it does not know), so the command is the mark."""
    if not isinstance(group, dict):
        return False
    handlers = group.get('hooks')
    return (isinstance(handlers, list) and bool(handlers)
            and all(isinstance(h, dict) and _is_trio_command(h.get('command')) for h in handlers))


def hook_command(python, script, runtime, action):
    """The command line Codex runs: `$SHELL -lc` on POSIX, `cmd.exe /C` on Windows."""
    argv = [str(python), str(script), '--home', str(runtime), action]
    return subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv)


def install_hooks(data, python, script, runtime):
    """Register Trio's delivery hooks in a parsed hooks.json, idempotently.

    Codex keys a hook's trust by its file, event, group index and handler index. A
    re-install therefore rewrites Trio's group where it already stands, so neither
    it nor the user's own hooks after it move and need trusting again; a new group
    goes at the end of its event."""
    if not isinstance(data.get('hooks'), dict):
        data['hooks'] = {}
    hooks = data['hooks']
    for event, action, matcher in HOOK_EVENTS:
        entry = {'type': 'command', 'command': hook_command(python, script, runtime, action),
                 'timeout': END_TIMEOUT_SECONDS if action == 'end' else HOOK_TIMEOUT_SECONDS}
        group = {'hooks': [entry]}
        if matcher:
            group = {'matcher': matcher, 'hooks': [entry]}
        groups = hooks.get(event) if isinstance(hooks.get(event), list) else []
        ours = [index for index, existing in enumerate(groups) if is_trio_group(existing)]
        if ours:
            groups[ours[0]] = group
            groups = [existing for index, existing in enumerate(groups) if index not in ours[1:]]
        else:
            groups.append(group)
        hooks[event] = groups
    return data


def uninstall_hooks(data):
    """Remove Trio's own groups; leave every other hook untouched. Returns the number removed."""
    hooks = data.get('hooks') if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        kept = [group for group in hooks[event] if not is_trio_group(group)]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


def load_hooks_file(path):
    """The parsed hooks.json, {} when there is none. A file that is not a JSON object
    raises: it is the user's, and rewriting it would destroy what it holds."""
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'{path} does not hold a JSON object')
    return data


def save_hooks_file(path, data, stamp=None):
    """Back up the current file, then replace it atomically, private to this user."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime('%Y%m%d-%H%M%S')
    if path.exists():
        backup = path.with_name(path.name + '.bak-' + stamp)
        if not backup.exists():
            shutil.copy2(path, backup)
    temporary = path.with_name(path.name + '.tmp-' + stamp)
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    if os.name != 'nt':
        temporary.chmod(0o600)
    temporary.replace(path)


def delivery_hooks_installed(home=None):
    """True when Trio's delivery hooks are registered in this Codex home's hooks.json.
    Whether the user has trusted them is recorded by Codex itself and not checked here."""
    try:
        hooks = load_hooks_file(hooks_file(home)).get('hooks')
    except (OSError, ValueError):
        return False
    return isinstance(hooks, dict) and any(
        is_trio_group(group) for groups in hooks.values() if isinstance(groups, list) for group in groups)


def toml_hooks_present(home=None):
    """Whether config.toml also declares hooks. Codex loads both forms but warns at
    startup when one layer uses both. None when it cannot tell."""
    path = Path(home or codex_home()) / 'config.toml'
    try:
        import tomllib
        config = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - no tomllib, or a file Codex itself would reject
        return None
    hooks = config.get('hooks')
    return isinstance(hooks, dict) and any(key != 'state' and value for key, value in hooks.items())


# ---- finding the processes around the hook --------------------------------------------

def _ps(pid, field):
    """One `ps` column for a process where /proc is absent (macOS), or None."""
    ps = '/bin/ps' if Path('/bin/ps').exists() else 'ps'
    try:
        output = subprocess.run([ps, '-o', field + '=', '-p', str(pid)], capture_output=True,
                                text=True, timeout=5, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return output or None


def process_info(pid):
    """(name, parent pid) of a process, or (None, None) when it cannot be read."""
    if not isinstance(pid, int) or pid <= 0 or os.name == 'nt':
        return None, None
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        head, tail = text.split(' (', 1)[1].rsplit(') ', 1)
        return head, int(tail.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    command, parent = _ps(pid, 'comm'), _ps(pid, 'ppid')
    try:
        return os.path.basename(command), int(parent)
    except (TypeError, ValueError):
        return None, None


def process_argv(pid):
    """A process's command line as a list, or None when it cannot be read. From `ps` the
    arguments are split on spaces, which is enough for the flags looked at here."""
    if not isinstance(pid, int) or pid <= 0 or os.name == 'nt':
        return None
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        return [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part]
    except OSError:
        pass
    command = _ps(pid, 'command')
    return command.split() if command else None


def is_managed_daemon(argv):
    """Whether a command line is the shared app-server daemon Codex starts for itself:
    `codex app-server [--remote-control] --listen unix:// [--managed-daemon]`. Its socket is
    the default one under CODEX_HOME, which is the server `codex queue` discovers. An
    app-server on any other endpoint (stdio for an IDE, an explicit socket, a websocket)
    is a different server."""
    if not argv or 'app-server' not in argv:
        return False
    if '--managed-daemon' in argv:
        return True
    for index, part in enumerate(argv):
        value = part[len('--listen='):] if part.startswith('--listen=') else (
            argv[index + 1] if part == '--listen' and index + 1 < len(argv) else None)
        if value is not None:
            return value == 'unix://'
    return False


def codex_ancestor():
    """The nearest ancestor that is not a shell, if it is a Codex process, else None."""
    pid = os.getppid()
    for _ in range(4):
        process, parent = process_info(pid)
        if process is None:
            return None
        if process.lower() not in SHELLS:
            return pid if 'codex' in process.lower() else None
        pid = parent
    return None


def codex_host():
    """(pid, problem): the Codex process that ran this hook, looking past the shell Codex
    wraps the command in, and why a wake must not be queued from it ('' when it may).

    Only the shared daemon is safe: `codex queue` reaches that server, and a thread that
    lives in another one would be run a second time. The pid is the waiter's supervisor."""
    if os.name == 'nt':
        return None, 'unsupported on Windows: the hook cannot identify the Codex daemon there'
    pid = codex_ancestor()
    if pid is None:
        return None, 'the hook was not run by a Codex process'
    argv = process_argv(pid)
    if argv is None:
        return pid, 'the command line of the Codex process that ran the hook cannot be read'
    if not is_managed_daemon(argv):
        return pid, ('the hook ran in a Codex process that is not the shared app-server daemon (a TUI '
                     'without the daemon, the desktop app or an IDE), so codex queue would reach a '
                     'different server')
    return pid, ''


def trio_server_pid():
    """The app-server `trio codex` started, from its record. Its threads are delivered
    to by Trio's event service; hooks registered for plain Codex stand down there."""
    from nth_event_service import state_dir
    record = core.read_json(state_dir() / 'codex-server.json') or {}
    pid = record.get('pid')
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


def relay_owns(session_id):
    """Whether Trio's event service is delivering to this thread already, as it does
    for a thread started through `trio codex`. Then the waiter must not wake it too."""
    try:
        from nth_event_service import bindings, service_alive, state_dir
        if not (state_dir() / 'registry.sqlite').exists():
            return False
        owned = any(json.loads(row['config']).get('thread_id') == session_id and row['enabled']
                    and row['status'] == 'listening' for row in bindings())
        return owned and service_alive()
    except Exception:  # noqa: BLE001 - the registry is optional here
        return False


def codex_binary():
    """The codex executable for `codex queue`: TRIO_CODEX_BINARY, then codex on PATH,
    then the codex_binary saved by `setup.py --codex-binary`. Never the process that
    ran the hook: that is the daemon, whose path an update may already have replaced."""
    override = os.environ.get('TRIO_CODEX_BINARY')
    if override:
        return override if Path(override).is_file() else None
    found = shutil.which('codex')
    if found:
        return found
    from nth_event_service import home
    saved = (core.read_json(home() / 'native.json') or {}).get('codex_binary')
    return saved if isinstance(saved, str) and Path(saved).is_file() else None


def queue_wake(session_id, text):
    """Queue the wake into the thread: 'queued', 'failed' or 'unknown'.

    A refusal is retried. A timeout is not: the message may have been queued after
    all, so it is 'unknown' and counts as delivered, and the next waiter does not
    announce the same ids again. A failure leaves the marks where they were."""
    binary = codex_binary()
    if not binary:
        return 'failed'
    command = [binary, 'queue', '--thread', session_id, '--message', text]
    for attempt in range(QUEUE_ATTEMPTS):
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=QUEUE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return 'unknown'
        except OSError:
            return 'failed'
        if result.returncode == 0:
            return 'queued'
        if attempt + 1 < QUEUE_ATTEMPTS:
            time.sleep(QUEUE_RETRY_SECONDS * (attempt + 1))
    return 'failed'


def unattended_budget():
    """Wakes allowed with nobody typing: TRIO_CODEX_UNATTENDED_WAKES, default 10; 0 or
    less means no limit (None)."""
    try:
        value = int(os.environ.get('TRIO_CODEX_UNATTENDED_WAKES', DEFAULT_UNATTENDED_WAKES))
    except ValueError:
        value = DEFAULT_UNATTENDED_WAKES
    return value if value > 0 else None


def is_wake_notice(prompt):
    """Whether a submitted prompt is a queued Trio notice rather than a person typing.
    Codex runs UserPromptSubmit for queued messages too."""
    if not isinstance(prompt, str):
        return False
    lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith(NOTICE_PREFIX) for line in lines)


class QueueSink:
    """How a waiter reaches a Codex thread: `codex queue`. See core.StderrSink."""
    client = 'codex'
    name_servers = True

    def __init__(self, session_id, supervisor):
        self.session_id = session_id
        self.supervisor = supervisor
        self.lifetime = LIFETIME_SECONDS
        self.settle = SETTLE_SECONDS
        self.patience = LOCK_PATIENCE_SECONDS
        self.budget = unattended_budget()
        self.status = {'supervisor_pid': supervisor}
        self.outcome = {}
        self._checked = None
        self._owned = False

    def standing_down(self):
        now = time.monotonic()
        if self._checked is None or now - self._checked >= RELAY_CHECK_SECONDS:
            self._checked, self._owned = now, relay_owns(self.session_id)
        return self._owned

    def preflight(self):
        if not codex_binary():
            return 'no codex executable found: put codex on PATH or set TRIO_CODEX_BINARY'
        return ''

    def deliver(self, lines):
        result = queue_wake(self.session_id, '\n'.join(lines))
        if result == 'unknown':
            self.outcome = {'note': 'codex queue timed out: the wake may not have been queued, and its '
                                    'messages count as announced'}
        return result in ('queued', 'unknown')


def spawn_waiter(session_id, supervisor):
    """Start this session's waiter fully detached and return at once.

    Codex reads the hook's stdout and stderr until they close, so a waiter that kept
    them would hold the hook (and the turn) open until the hook timed out, and then be
    killed with the hook's process group. A new session and the null device for all
    three streams let the hook exit while the waiter lives on."""
    command = [sys.executable, str(Path(__file__).resolve()), 'wait', '--session', session_id]
    if os.environ.get('NTH_HOME'):
        command[2:2] = ['--home', os.environ['NTH_HOME']]
    if supervisor:
        command += ['--supervisor', str(supervisor)]
    options = {}
    if os.name == 'nt':
        options['creationflags'] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        options['start_new_session'] = True
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, cwd=str(core.hooks_dir()),
                            **options)


def arm(session_id, supervisor, problem=''):
    """Start a waiter when this session holds memberships, has not ended and is not
    paused. From a host a wake must not be queued from, record why instead."""
    state = core.load_session(session_id)
    if not state or state['ended'] or not state['memberships'] or state.get('paused'):
        return None
    if problem:
        core.write_status(session_id, 'codex', 0, {}, supervisor_pid=supervisor, problem=problem)
        return None
    return spawn_waiter(session_id, supervisor)


def _attended(session_id):
    """A person is here: the unattended-wake budget starts again. True when it was paused."""
    if not core.session_path(session_id).exists():
        return False                                 # a session that never joined: no lock file either
    with core.session_update(session_id) as state:
        if state is None:
            return False
        paused = bool(state.get('paused'))
        state.update(unattended_wakes=0, paused=False, last_prompt=time.time())
        return paused


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--home', help='Trio runtime directory (NTH_HOME) of this installation')
    parser.add_argument('event', choices=['tool', 'stop', 'start', 'prompt', 'end', 'wait'])
    parser.add_argument('--session', help='wait: the session (thread) to serve')
    parser.add_argument('--supervisor', type=int, help='wait: the process whose exit ends the waiter')
    args = parser.parse_args(argv)
    if args.home:
        os.environ['NTH_HOME'] = args.home
    os.environ.setdefault('NTH_QUIET', '1')          # no console banner from the server module
    os.umask(0o077)
    # Codex may show a hook's output to the model or the user: say nothing at all.
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    if args.event == 'wait':
        if not isinstance(args.session, str) or not core.SESSION_ID.match(args.session):
            return 0
        core.wait(args.session, QueueSink(args.session, args.supervisor))
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    session_id = payload.get('session_id') if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not core.SESSION_ID.match(session_id):
        return 0
    if args.event == 'end':
        with core.session_update(session_id) as state:
            if state is not None:
                state['ended'] = True
        return 0
    host, problem = codex_host()
    if host and host == trio_server_pid():
        return 0                                     # `trio codex`: the event service delivers
    if args.event == 'prompt':
        if is_wake_notice(payload.get('prompt')):
            return 0
        if _attended(session_id):
            arm(session_id, host, problem)           # paused until now; otherwise Stop arms
        return 0
    if args.event == 'tool':
        core.register(payload, tools=HOOK_TOOLS, client='codex')
    if args.event == 'start':
        if payload.get('source') not in ('startup', 'resume'):
            return 0
        with core.session_update(session_id) as state:
            if state is not None and state['memberships']:
                state['ended'] = False
        if payload.get('source') == 'resume':
            _attended(session_id)
    arm(session_id, host, problem)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a hook must never disturb the session it serves
        sys.exit(0)
