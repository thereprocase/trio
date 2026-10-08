#!/usr/bin/env python3
"""Push delivery for a plainly launched Claude Code session, through its own hooks.

Claude Code runs a command hook with `asyncRewake: true` in the background and,
when it exits with code 2, wakes the model and shows it the hook's stderr as a
system reminder. This script is that hook. `setup.py` registers it in Claude's
user settings for four events:

    tool   PostToolUse on the Trio/Quartet connect, listen and ack tools of any
           nth-* server: note which membership this session holds, then wait
    stop   Stop, after every turn: wait again if this session holds memberships
    start  SessionStart on resume: a resumed session takes its memberships back
           and waits again, with no tool call needed
    end    SessionEnd: this session is over, its waiter leaves

Waiting means one process per session that long-polls the session's memberships
without acknowledging, filters per message, and exits 2 on the first message
that passes. It needs no launch flag and no Monitor, so it reaches a session
however it was started.

What it writes to stderr reaches the model framed as a system reminder, which
the model trusts more than a tool result. It is therefore a fixed sentence built
by nth_notice from integers and sanitized identifiers. It never carries message
text or a sender's name: the agent reads those through the poll tool, as
untrusted data. Polling and filtering are nth_listener's, shared with the
channel frontends.
Credentials are read from the identity file the frontend saved; nothing secret
is taken from the hook's input or kept in this script's state.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

try:
    from nth_listener import FILTERS, PUSH_BURST, PUSH_REFILL_SECONDS, Listener, quartet_poll_factory
    from nth_notice import ENDED_ADVICE, from_event, name  # noqa: F401 - ENDED_ADVICE re-exported
except Exception:  # noqa: BLE001
    # Run as a hook, a partial install must not disturb the session: whatever this
    # process writes to stderr reaches the model as a system reminder, a traceback
    # included. Leave quietly. Imported as a module, fail as usual.
    if __name__ != '__main__':
        raise
    sys.exit(0)

# Any nth-* server: setup.py registers nth-trio and nth-qweb, and users add more
# Quartet hubs under their own names (nth-team, ...). Server names never contain "__".
HOOK_TOOLS = re.compile(r'^mcp__(nth-[A-Za-z0-9-]+)__(trio|quartet)_(connect|listen|ack)$')
SESSION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{5,79}$')
IDENTITY_KEY = re.compile(r'^[0-9a-f]{24}$')
DEFAULT_FILTER = 'about'
# How often the waiter looks at its session's files and at the session itself.
TICK_SECONDS = 1.0
# Listeners that find something in the same moment share one wake.
SETTLE_SECONDS = .3
STATUS_EVERY_SECONDS = 20.0
# The frontend calls a waiter alive while its status is younger than this.
STATUS_FRESH_SECONDS = 60.0
# Only when the session's own process id is unknown: the waiter cannot tell that
# the session is gone, so it does not stay for ever. A turn's Stop hook re-arms.
UNSUPERVISED_LIFETIME_SECONDS = 24 * 3600.0


# ---- registration in Claude's user settings --------------------------------------

# Marks a hook group as Trio's own, so re-installing replaces it and an uninstall
# finds it, without disturbing hooks the user or another tool registered.
HOOK_TAG = 'nth-trio-delivery'
HOOK_EVENTS = (('PostToolUse', 'tool', r'mcp__nth-[A-Za-z0-9-]+__(trio|quartet)_(connect|listen|ack)'),
               ('Stop', 'stop', None), ('SessionStart', 'start', 'resume'), ('SessionEnd', 'end', None))
HOOK_SCRIPT = 'nth_claude_hook.py'
# Claude Code enforces a hook's `timeout` (600 s by default) even on asyncRewake hooks,
# and cancels the hook at expiry. Unset, an idle session would be deaf ten minutes
# after its last turn. A day matches the waiter's own unsupervised lifetime.
HOOK_TIMEOUT_SECONDS = 24 * 3600


def is_trio_group(group):
    """Whether a settings hook group is Trio's own. The tag is the primary mark, but a
    settings writer may drop keys it does not know, so a group whose command runs this
    script counts as Trio's too: detection, re-install and uninstall all rely on it."""
    if not isinstance(group, dict):
        return False
    if group.get(HOOK_TAG):
        return True
    for entry in group.get('hooks') or []:
        if not isinstance(entry, dict):
            continue
        parts = [entry.get('command')] + list(entry.get('args') or [])
        if any(isinstance(part, str) and part.replace('\\', '/').endswith('/' + HOOK_SCRIPT)
               or part == HOOK_SCRIPT for part in parts):
            return True
    return False


def settings_files():
    """The Claude Code user settings file. CLAUDE_CONFIG_DIR overrides the default
    location entirely, as Claude Code itself resolves it."""
    directory = os.environ.get('CLAUDE_CONFIG_DIR')
    if directory:
        return [Path(directory) / 'settings.json']
    return [Path.home() / '.claude' / 'settings.json']


def delivery_hooks_installed():
    """True when Trio's delivery hooks are registered for this Claude, so a plainly
    launched session is woken by them and must not also run a Monitor."""
    for path in settings_files():
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        hooks = data.get('hooks') if isinstance(data, dict) else None
        if isinstance(hooks, dict) and any(
                is_trio_group(group)
                for groups in hooks.values() if isinstance(groups, list) for group in groups):
            return True
    return False


def install_hooks(settings, python, script, runtime):
    """Register the asyncRewake delivery hooks in a Claude settings dict, idempotently.

    A plainly launched Claude has no channel listener. These hooks give it one:
    after a Trio connect and after every turn, a background waiter polls this
    session's memberships and, on a message, exits 2 so Claude Code wakes the
    model. A session that never joins Trio spawns a short-lived process per turn
    that returns at once; a `trio claude` session ignores the hooks entirely.
    """
    hooks = settings.setdefault('hooks', {})
    for event, action, matcher in HOOK_EVENTS:
        groups = [group for group in hooks.get(event, []) if not is_trio_group(group)]
        entry = {'type': 'command', 'command': str(python),
                 'args': [str(script), '--home', str(runtime), action]}
        if action != 'end':                          # SessionEnd only records; it never wakes
            entry['asyncRewake'] = True
            entry['timeout'] = HOOK_TIMEOUT_SECONDS
        group = {HOOK_TAG: True, 'hooks': [entry]}
        if matcher:
            group['matcher'] = matcher
        hooks[event] = groups + [group]


def uninstall_hooks(settings):
    """Remove Trio's own delivery hooks; leave every other hook untouched. Returns
    the number removed."""
    removed = 0
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        kept = [group for group in hooks[event] if not is_trio_group(group)]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop('hooks', None)
    return removed


def hooks_dir():
    from nth_event_service import state_dir
    path = state_dir() / 'hooks'
    path.mkdir(mode=0o700, exist_ok=True)
    if os.name != 'nt':
        # Its files decide what this session listens to: refuse a directory someone else
        # could have planted (a symlink or another owner). Our own directory left open to
        # the group or world, e.g. by a default ACL, is closed rather than refused.
        import stat
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f'{path} must be a directory owned by this user, not a symlink')
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
    return path


def identities_dir():
    from nth_event_service import state_dir
    return state_dir() / 'identities'


def session_path(session_id, suffix='.json'):
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        raise ValueError('not a session id')
    return hooks_dir() / ('session-' + session_id + suffix)


def membership_path(key):
    if not isinstance(key, str) or not IDENTITY_KEY.match(key):
        raise ValueError('not an identity key')
    return hooks_dir() / ('membership-' + key + '.json')


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def write_json(path, value):
    """Readers see the previous complete file or the new one, never a part of it."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path, blocking):
    """An OS lock on `path`, released when the process dies. Yields whether it is held."""
    handle = open(path, 'a+b')
    held = False
    try:
        if os.name == 'nt':
            import msvcrt
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), mode, 1)
                    held = True
                    break
                except OSError:
                    if not blocking:
                        break                       # LK_LOCK itself gives up after ten tries
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                held = True
            except OSError:
                held = False
        yield held
    finally:
        if held and os.name == 'nt':
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def process_stamp(pid):
    """Something that stays the same for as long as process `pid` lives, or None when
    it is gone. Where the platform tells, it is the process's start time, so that a
    reused process id is not taken for the session it once belonged to."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)                 # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None                                               # not STILL_ACTIVE
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *[ctypes.byref(value) for value in times]):
                return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        # Field 22 of /proc/PID/stat, counted after the command name's closing bracket.
        return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return True


# ---- session state ---------------------------------------------------------------

def empty_session():
    # `servers` names the MCP server each membership was joined through, so that a
    # wake on a machine with several Quartet hubs can say which one to poll; `joined`
    # says when, so a status check can tell the newest holder of a membership.
    return {'memberships': {}, 'ended': False, 'high_water': {}, 'acked': {},
            'bucket': None, 'wakes': 0, 'last_wake': None, 'servers': {}, 'joined': {},
            'client': ''}


def load_session(session_id):
    state = read_json(session_path(session_id))
    if state is None:
        return None
    for field, default in empty_session().items():
        if not isinstance(state.get(field), type(default)) and default is not None:
            state[field] = default
        state.setdefault(field, default)
    return state


@contextmanager
def session_update(session_id, create=False):
    """Read, change and write this session's state under a lock. Yields None when
    there is no state and none is to be created."""
    with file_lock(session_path(session_id, '.state.lock'), blocking=True):
        state = load_session(session_id)
        if state is None and create:
            state = empty_session()
        yield state
        if state is not None:
            write_json(session_path(session_id), state)


def tool_body(response):
    """The JSON body of an MCP tool result as a hook receives it, or None.

    Claude Code hands over the structured form as a string, `{"result": "<json>"}`;
    its documentation also allows a text block or a list of blocks. Codex hands
    over the whole CallToolResult as an object: `content`, `structuredContent`
    (itself `{"result": "<json>"}` for Trio's tools) and `isError`.
    """
    try:
        if isinstance(response, str):
            response = json.loads(response)
        if (isinstance(response, dict) and 'result' not in response and 'text' not in response
                and ('content' in response or 'structuredContent' in response)):
            if response.get('isError'):
                return None
            structured = response.get('structuredContent')
            response = structured if isinstance(structured, dict) else response.get('content')
        if isinstance(response, list):
            response = next((block for block in response
                             if isinstance(block, dict) and block.get('type') == 'text'), None)
        if isinstance(response, dict) and isinstance(response.get('text'), str):
            response = json.loads(response['text'])
        if isinstance(response, dict) and isinstance(response.get('result'), str):
            response = json.loads(response['result'])
        return response if isinstance(response, dict) else None
    except ValueError:
        return None


def identity_for(body):
    """(key, identity) for the membership a connect or listen result names, or None.

    The result names a file; what is trusted is the file. It must be one of this
    installation's identity files, and what it holds must match what the result
    says about the membership.
    """
    key = body.get('identity_key')
    if not isinstance(key, str) and isinstance(body.get('identity_file'), str):
        named = Path(body['identity_file'])
        key = named.stem if named.suffix == '.json' else None
    if not isinstance(key, str) or not IDENTITY_KEY.match(key):
        return None
    identity = read_json(identities_dir() / (key + '.json'))
    if not identity or not all(isinstance(identity.get(field), str) and identity[field]
                               for field in ('channel', 'member_id', 'session_token', 'source', 'url')):
        return None
    for field in ('channel', 'member_id'):
        if field in body and body[field] != identity[field]:
            return None
    return key, identity


def register(payload, tools=None, client='claude'):
    """Note what a successful connect, listen or ack says about this session.

    `tools` matches the tool names this client reports, with the server name as
    group 1, the flavor as group 2 and the operation as group 3."""
    match = (tools or HOOK_TOOLS).match(str(payload.get('tool_name') or ''))
    body = tool_body(payload.get('tool_response'))
    if not match or body is None or body.get('error'):
        return
    operation = match.group(3)
    session_id = payload.get('session_id')
    if operation == 'ack' and not session_path(session_id).exists():
        return
    if operation == 'ack':
        arguments = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
        through = arguments.get('through_id')
        if not body.get('ok') or not isinstance(through, int) or isinstance(through, bool):
            return
        with session_update(session_id) as state:
            for key, membership in (state or {}).get('memberships', {}).items():
                if (membership.get('channel') == arguments.get('channel')
                        and membership.get('member_id') == arguments.get('member_id')):
                    state['acked'][key] = max(int(state['acked'].get(key) or 0), through)
        return
    found = identity_for(body)
    if found is None:
        return
    key, identity = found
    prune()
    with session_update(session_id, create=True) as state:
        state['ended'] = False
        state['memberships'][key] = {'source': identity['source'], 'channel': identity['channel'],
                                     'member_id': identity['member_id']}
        state['servers'][key] = name(match.group(1))
        state['joined'][key] = time.time()
        state['client'] = client
        # A reconnect rotates the token, and with it the key: the old one is gone.
        for other, membership in list(state['memberships'].items()):
            if other != key and (membership.get('source'), membership.get('channel'),
                                 membership.get('member_id')) == (identity['source'], identity['channel'],
                                                                  identity['member_id']):
                del state['memberships'][other]
                state['servers'].pop(other, None)
                state['joined'].pop(other, None)
                for field in ('high_water', 'acked'):
                    if other in state[field]:
                        state[field][key] = max(int(state[field].get(key) or 0), int(state[field].pop(other)))


def identity_key_for(channel, member_id, session_token):
    """The key of this installation's saved identity for these credentials, or None."""
    try:
        paths = sorted(identities_dir().glob('*.json'))
    except OSError:
        return None
    for path in paths:
        if not IDENTITY_KEY.match(path.stem):
            continue
        identity = read_json(path) or {}
        if (identity.get('channel'), identity.get('member_id'), identity.get('session_token')) \
                == (channel, member_id, session_token):
            return path.stem
    return None


@contextmanager
def membership_update(key):
    """Read, change and write a membership's saved config under a lock. listen() and the
    waiter's ended mark both write it; unlocked, one could revert the other and undo a stop."""
    path = membership_path(key)
    with file_lock(path.with_suffix('.lock'), blocking=True):
        config = dict(read_json(path) or {})
        yield config
        write_json(path, config)


# Why a waiter stopped serving a membership. Only a listener failure is worth retrying
# on request; an ended channel or a refused membership needs a person (or a reconnect).
RETRYABLE_ENDED = ('listener failure',)


def configure_membership(key, filter_mode=None, enabled=None):
    """Save a listen call's filter and on/off switch where the waiter reads them; an
    omitted value keeps what is saved. enabled=true also clears a retryable ended mark.
    Returns the resulting membership_config, whose `ended` says if the waiter still skips it."""
    if filter_mode is not None and filter_mode not in FILTERS:
        raise ValueError(f'filter_mode must be one of {sorted(FILTERS)}')
    with membership_update(key) as config:
        if filter_mode is not None:
            config['filter'] = filter_mode
        if enabled is not None:
            config['enabled'] = bool(enabled)
        if enabled and config.get('ended') in RETRYABLE_ENDED:
            config.pop('ended')
    return membership_config(key)


def membership_config(key):
    config = read_json(membership_path(key)) or {}
    chosen = config.get('filter')
    return {'filter': chosen if chosen in FILTERS else DEFAULT_FILTER,
            'enabled': config.get('enabled') is not False,
            'ended': config.get('ended') if isinstance(config.get('ended'), str) else ''}


# ---- the waiter ------------------------------------------------------------------

class Wake:
    """What the listeners report to. It stands where a ChannelHub stands, and keeps
    only integers and the two identifiers: a listener's rendered content, which is
    peer text, is dropped here and never reaches stderr."""

    def __init__(self, bucket):
        self.burst, self.refill = float(PUSH_BURST), float(PUSH_REFILL_SECONDS)
        now = time.time()
        saved = bucket if isinstance(bucket, dict) else {}
        tokens, at = saved.get('tokens'), saved.get('at')
        if not isinstance(tokens, (int, float)) or not isinstance(at, (int, float)) or at > now:
            tokens, at = self.burst, now
        self.tokens = min(self.burst, float(tokens) + (now - at) / self.refill)
        self.at = now
        self.lock = threading.Lock()
        self.fired = threading.Event()
        self.lines = []
        self.ended = {}

    def delay(self):
        """Seconds until a wake may be written; 0 when one may go now."""
        with self.lock:
            now = time.time()
            self.tokens = min(self.burst, self.tokens + (now - self.at) / self.refill)
            self.at = now
            return 0.0 if self.tokens >= 1 else (1 - self.tokens) * self.refill

    def bucket(self):
        with self.lock:
            return {'tokens': self.tokens, 'at': self.at}


class WakeFor:
    """One membership's view of the Wake: the sink a Listener reports to."""

    def __init__(self, wake, key, prefix, server=None):
        # Several hubs share the tool names (quartet_poll on each), so a sink that
        # names servers says which one this membership belongs to.
        self.wake, self.key, self.prefix, self.server = wake, key, prefix, server

    def push(self, content, meta, cancelled=None):
        del content                                  # peer text: never used here
        if cancelled and cancelled():
            return False
        # The Listener tags its metadata. Retain compatibility for older
        # callers that supplied message metadata without an event tag.
        if isinstance(meta, dict) and 'event' not in meta:
            meta = {**meta, 'event': 'new_messages'}
        notice = from_event(self.prefix, meta, self.server)
        if notice is None:
            return False
        with self.wake.lock:
            if notice.ended:
                self.wake.ended[self.key] = notice.ended
            self.wake.tokens -= 1
            self.wake.lines.append(notice.line)
        self.wake.fired.set()
        return True


def poll_factory(identity):
    """(poll, close) against the hub or the local database this identity belongs to."""
    if identity['source'] == 'local':
        from nth_event_sources import create_source
        source = create_source({'source': 'local', 'url': identity['url']})
        started = []

        def poll(arguments):
            if not started:
                started.append(True)
                source.connect()
            return source.call_tool('trio_poll', arguments)
        return poll, None
    return quartet_poll_factory({'url': identity['url']})


def make_listener(wake, key, identity, config, high_water, server=None):
    class WaitingListener(Listener):
        # One bucket for the session: every wake is a model turn, whichever
        # membership it is for.
        def _push_delay(self):
            return wake.delay()

    prefix = 'trio' if identity['source'] == 'local' else 'quartet'
    binding = {'source': identity['source'], 'url': identity['url'], 'channel': identity['channel'],
               'member_id': identity['member_id'], 'session_token': identity['session_token'],
               'filter': config['filter']}
    poll, close = poll_factory(identity)
    listener = WaitingListener(WakeFor(wake, key, prefix, server), binding, poll, close, high_water=high_water)
    listener.start()
    return listener


def claude_pid():
    try:
        pid = int(os.environ.get('CLAUDE_PID') or 0)
    except ValueError:
        pid = 0
    return pid if pid > 0 else None


class StderrSink:
    """How a waiter reaches a Claude Code session: it writes the wake to stderr and the
    asyncRewake hook exits 2. The session's own process supervises the waiter; with no
    process id to watch, the waiter leaves after UNSUPERVISED_LIFETIME_SECONDS.

    A sink supplies `client`, `supervisor` (a process id or None), `lifetime` (seconds
    or None), `settle` (seconds to gather listeners that fire together), `patience`
    (seconds to wait for a predecessor still holding the session's lock), `name_servers`,
    `status` and
    `outcome` (extra fields for the status file), `standing_down()`, `preflight()` and
    `deliver(lines)`, which returns whether the wake counts as delivered.
    """
    client = 'claude'
    name_servers = False
    patience = 0.0
    outcome = {}

    def __init__(self):
        self.supervisor = claude_pid()
        self.lifetime = None if self.supervisor else UNSUPERVISED_LIFETIME_SECONDS
        self.settle = SETTLE_SECONDS
        self.status = {'claude_pid': self.supervisor}

    def standing_down(self):
        return False

    def preflight(self):
        """A reason this sink cannot deliver at all, or ''."""
        return ''

    def deliver(self, lines):
        SAY.write('\n'.join(lines) + '\n')
        SAY.flush()
        return True


LOCK_RETRY_SECONDS = .5


def wait(session_id, sink=None):
    """Be this session's waiter, unless it has one. 0: nothing was delivered. 2: a wake was
    delivered (for Claude, written to stderr for the hook to exit 2 with)."""
    sink = sink or StderrSink()
    deadline = time.monotonic() + sink.patience
    while True:
        with file_lock(session_path(session_id, '.lock'), blocking=False) as held:
            if held:
                return _wait_locked(session_id, sink)
        # A predecessor that is just leaving releases the lock in a moment; one that
        # is still waiting keeps it, and this process is not needed.
        if time.monotonic() >= deadline:
            return 0
        time.sleep(LOCK_RETRY_SECONDS)


def _status(session_id, sink, pid, listeners, **extra):
    write_status(session_id, sink.client, pid, listeners, **sink.status, **extra)


def write_status(session_id, client, pid, listeners, **extra):
    """The waiter's own report, read by delivery status. `pid` is 0 when no waiter runs."""
    write_json(session_path(session_id, '.status.json'),
               dict(extra, client=client, session=session_id, pid=pid, heartbeat=time.time(),
                    listeners=listeners))


def _wait_locked(session_id, sink):
    state = load_session(session_id)
    if not state or state['ended'] or not state['memberships']:
        return 0
    supervisor, born = sink.supervisor, time.monotonic()
    stamp = process_stamp(supervisor) if supervisor else None
    if supervisor and stamp is None:
        return 0
    if sink.standing_down():
        return 0
    problem = sink.preflight()
    if problem:
        _status(session_id, sink, 0, {}, problem=problem)
        return 0
    wake = Wake(state['bucket'])
    listeners, filters, marks, written, reported = {}, {}, {}, 0.0, None
    fired = False
    try:
        while True:
            if supervisor and process_stamp(supervisor) != stamp:
                return 0                             # the session is gone: leave no orphan
            if sink.lifetime is not None and time.monotonic() - born > sink.lifetime:
                return 0
            state = load_session(session_id)
            if not state or state['ended'] or sink.standing_down():
                return 0
            wanted = {}
            for key in state['memberships']:
                config = membership_config(key)
                identity = read_json(identities_dir() / (key + '.json'))
                if identity and config['enabled'] and not config['ended'] and key not in wake.ended:
                    wanted[key] = (identity, config)
            for key in [key for key in listeners if key not in wanted or filters[key] != wanted[key][1]['filter']]:
                previous = listeners.pop(key)
                previous.stop()
                marks[key] = max(marks.get(key, 0), previous.high_water)
            for key, (identity, config) in wanted.items():
                if key not in listeners:
                    filters[key] = config['filter']
                    server = state['servers'].get(key) if sink.name_servers else None
                    listeners[key] = make_listener(wake, key, identity, config,
                                                   max(int(state['high_water'].get(key) or 0), marks.get(key, 0)),
                                                   server)
            if not listeners:
                return 0                             # nothing is enabled: a later hook re-arms
            report = {key: (listener.state, listener.error, filters[key]) for key, listener in listeners.items()}
            if report != reported or time.time() - written > STATUS_EVERY_SECONDS:
                reported, written = report, time.time()
                _status(session_id, sink, os.getpid(),
                        {key: {'status': s, 'error': e, 'filter': f} for key, (s, e, f) in report.items()})
            if wake.fired.wait(TICK_SECONDS):
                time.sleep(sink.settle)
                fired = True
                break
    finally:
        for key, listener in listeners.items():
            listener.stop()
            marks[key] = max(marks.get(key, 0), listener.high_water)
        if not fired:
            # What was seen stays seen, however this waiter leaves: a successor neither
            # repeats a wake nor goes back for what the filter declined.
            _remember(session_id, marks, wake, woke=False)
            _status(session_id, sink, 0, {})
    with wake.lock:
        lines, ended = list(wake.lines), dict(wake.ended)
    state = load_session(session_id)
    # A session that ended while the listeners settled is not woken, and nothing is
    # marked seen: a resumed session hears it from its next waiter.
    gone = (not state or state['ended']
            or (supervisor and process_stamp(supervisor) != stamp) or sink.standing_down())
    if not gone:
        _status(session_id, sink, os.getpid(), {}, delivering=True)
    delivered = not gone and sink.deliver(lines)
    if delivered:
        _remember(session_id, marks, wake, woke=True)
        for key, reason in ended.items():
            # Said once. A membership that is over is not announced again at every re-arm.
            with membership_update(key) as config:
                config['ended'] = reason
        _status(session_id, sink, 0, {}, **sink.outcome)
        return 2
    # Undelivered: the marks stay where they were, so the next waiter announces it again.
    _remember(session_id, {}, wake, woke=False)
    _status(session_id, sink, 0, {}, error='' if gone else 'wake not delivered')
    return 0


def _remember(session_id, marks, wake, woke):
    with session_update(session_id) as state:
        if state is not None:
            for key, mark in marks.items():
                if key in state['memberships']:
                    state['high_water'][key] = max(int(state['high_water'].get(key) or 0), mark)
            state['bucket'] = wake.bucket()
            if woke:
                state['wakes'] = int(state.get('wakes') or 0) + 1
                state['last_wake'] = {'at': time.time()}


def read_status(session_id):
    """A session's waiter status file, or None."""
    try:
        return read_json(session_path(session_id, '.status.json'))
    except (OSError, ValueError):
        return None


def status_live(status, now=None):
    """Whether a status file was written by a waiter that is still running and recent."""
    now = now or time.time()
    if not isinstance(status, dict):
        return False
    heartbeat, pid = status.get('heartbeat'), status.get('pid')
    return (isinstance(heartbeat, (int, float)) and now - heartbeat <= STATUS_FRESH_SECONDS
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and process_stamp(pid) is not None)


def holders(key, client):
    """The sessions of `client` that hold membership `key` and have not ended, the most
    recent joiner first."""
    try:
        paths = list(hooks_dir().glob('session-*.json'))
    except OSError:
        return []
    found = []
    for path in paths:
        if path.name.endswith('.status.json'):
            continue
        state = read_json(path) or {}
        memberships = state.get('memberships')
        if (state.get('client') != client or not isinstance(memberships, dict) or key not in memberships
                or state.get('ended')):
            continue
        joined = (state.get('joined') or {}).get(key)
        found.append((joined if isinstance(joined, (int, float)) else 0, path.name[len('session-'):-len('.json')]))
    return [session for _, session in sorted(found, reverse=True)]


def waiter_for(key, client, sessions=None, now=None):
    """The first live waiter of `client` that serves membership `key`, searched in
    `sessions` (default: every session of that client), as its listener status plus
    the session and the age of its heartbeat; None when none does. A waiter of another
    client, or a dead or silent waiter, never counts."""
    now = now or time.time()
    for session in (holders(key, client) if sessions is None else sessions):
        status = read_status(session)
        if not status_live(status, now) or status.get('client') != client:
            continue
        listener = (status.get('listeners') or {}).get(key)
        if not isinstance(listener, dict):
            continue
        return {'status': str(listener.get('status') or ''), 'filter': listener.get('filter'),
                'heartbeat_age': round(max(0.0, now - status['heartbeat']), 1), 'session': session}
    return None


SAY = sys.stderr


def prune(now=None):
    """Forget sessions and memberships nobody has touched for weeks."""
    now = now or time.time()
    for pattern, days in (('session-*', 14), ('membership-*.json', 30), ('membership-*.lock', 30)):
        for path in hooks_dir().glob(pattern):
            try:
                if now - path.stat().st_mtime > days * 86400:
                    path.unlink()
            except OSError:
                pass


def main(argv=None):
    global SAY
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--home', help='Trio runtime directory (NTH_HOME) of this installation')
    parser.add_argument('event', choices=['tool', 'stop', 'start', 'end'])
    args = parser.parse_args(argv)
    # A session launched with `trio claude` has listeners inside its frontends.
    if os.environ.get('TRIO_CLAUDE_CHANNEL') == '1':
        return 0
    if args.home:
        os.environ['NTH_HOME'] = args.home
    os.environ.setdefault('NTH_QUIET', '1')          # no console banner from the server module
    SAY, sys.stderr = sys.stderr, open(os.devnull, 'w', encoding='utf-8')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    session_id = payload.get('session_id') if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        return 0
    os.umask(0o077)
    if args.event == 'end':
        with session_update(session_id) as state:
            if state is not None:
                state['ended'] = True
        return 0
    if args.event == 'tool':
        register(payload)
    if args.event == 'start':
        # Only a resume continues a session that held memberships; a fresh start,
        # /clear and compaction need nothing here (compaction never ended it). It
        # does not wait here: Claude's first response may wait for SessionStart
        # hooks, so the Stop hook after that turn starts the waiter.
        if payload.get('source') == 'resume':
            with session_update(session_id) as state:
                if state is not None and state['memberships']:
                    state['ended'] = False
        return 0
    if not session_path(session_id).exists():
        return 0                                     # a session that never joined: nothing to do
    return wait(session_id)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a hook must never disturb the session it serves
        sys.exit(0)
