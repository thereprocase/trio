"""Private JSON-lines IPC for the spoke interposer (no MCP dependencies)."""
from contextlib import contextmanager
import errno
import ipaddress
import secrets
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import io
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
MAX_FRAME = 64 * 1024  # Includes the newline, so readers never buffer an unbounded line.
FRAME_TIMEOUT = 10
LEASE_EXIT_STATUS = 75
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{5,79}\Z')
IDENTITY_KEY = re.compile(r'[0-9a-f]{24}\Z')
OPS = frozenset(('hello', 'hub.announce', 'session.register', 'membership.attach',
                 'membership.configure', 'ack.seen', 'turn', 'session.end', 'wait',
                 'subscribe', 'delivered', 'status', 'list'))


class WireError(ValueError):
    """A fixed, non-secret error suitable for sending to an IPC client."""
    def __init__(self, message, code='invalid_request'):
        super().__init__(message)
        self.code = code

    def payload(self):
        return {'code': self.code, 'message': str(self)}


class ServiceRefused(WireError):
    """A definitive response; retrying this operation could apply it twice."""


def home():
    return Path(os.environ.get('NTH_HOME', str(Path.home() / '.claude' / 'nth')))


def private_dir(path):
    """Close permissive directories; refuse planted symlinks or another owner."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        if not path.parent.exists():
            private_dir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('interposer directory must be owned by this user, not a symlink')
    path.chmod(0o700)
    return path


def run_dir():
    return private_dir(home() / 'run')


def _user_runtime_dir():
    return Path('/run/user') / str(os.getuid())


def _safe_runtime(path, *, exact_mode=False):
    if not path.is_absolute():
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and (stat.S_IMODE(info.st_mode) == 0o700 if exact_mode else not info.st_mode & 0o022))


def _test_runtime_root():
    value = os.environ.get('NTH_INTERPOSER_TEST_RUNTIME')
    if value is None:
        return None
    root = Path(value)
    if not _safe_runtime(root):
        raise WireError('test interposer runtime must remain an owned, private absolute directory')
    return root.resolve()


def _inside_test_runtime(path, root):
    if root is None:
        return True
    if not path.is_absolute() or not path.is_relative_to(root):
        return False  # Do not even resolve/stat a real user runtime in tests.
    return path.resolve().is_relative_to(root)


def _checked_socket_path(path, root):
    if not path.is_absolute():
        raise WireError('interposer socket path must be absolute')
    if not _inside_test_runtime(path, root):
        raise WireError('interposer socket is outside the guarded test runtime')
    if len(os.fsencode(path)) >= 104:
        raise WireError('interposer socket path must be under 104 bytes')
    return path


def socket_path(nth_home=None):
    root = _test_runtime_root()
    explicit = os.environ.get('NTH_INTERPOSER_SOCKET')
    if explicit:
        return _checked_socket_path(Path(explicit), root)
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    if runtime and _inside_test_runtime(Path(runtime), root) and _safe_runtime(Path(runtime)):
        directory = Path(runtime) / 'trio'
    elif (_inside_test_runtime(_user_runtime_dir(), root)
          and _safe_runtime(_user_runtime_dir(), exact_mode=True)):
        directory = _user_runtime_dir() / 'trio'
    else:
        directory = (Path(nth_home) if nth_home is not None else home()) / 'run'
    return _checked_socket_path(directory / 'interposer.sock', root)


@contextmanager
def file_lock(path, *, timeout=0):
    """flock is process-scoped here; never unlink the inode another contender holds."""
    import fcntl
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError('interposer lock must be a file owned by this user')
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('interposer lock is held') from None
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def encode_frame(value):
    if not isinstance(value, dict):
        raise WireError('frame must be a JSON object')
    try:
        frame = (json.dumps(value, separators=(',', ':'), ensure_ascii=True,
                            allow_nan=False) + '\n').encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise WireError('bad JSON') from None
    if len(frame) > MAX_FRAME:
        raise WireError('frame exceeds 64 KiB')
    return frame


def read_frame(reader):
    frame = reader.readline(MAX_FRAME + 1)
    if not frame:
        raise EOFError('interposer connection closed')
    if len(frame) > MAX_FRAME:
        raise WireError('frame exceeds 64 KiB')
    if not frame.endswith(b'\n'):
        raise WireError('frame must end with a newline')
    try:
        value = json.loads(frame, parse_constant=_bad_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise WireError('bad JSON') from None
    if not isinstance(value, dict):
        raise WireError('frame must be a JSON object')
    return value


class SocketFrameReader:
    """Bound a frame's wall time, including idle time before its first byte.

    A persistent connection therefore also expires after ten seconds between
    frames; clients reconnect for subsequent operations.
    """
    def __init__(self, sock, timeout=FRAME_TIMEOUT):
        self.socket, self.timeout = sock, timeout
        self.buffer = bytearray()

    def readline(self, limit):
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('frame read deadline exceeded')
            newline = self.buffer.find(b'\n', 0, limit)
            size = newline + 1 if newline >= 0 else min(len(self.buffer), limit)
            if newline >= 0 or size == limit:
                frame = bytes(self.buffer[:size])
                del self.buffer[:size]
                return frame
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(min(4096, limit - len(self.buffer)))
            if not chunk:
                frame = bytes(self.buffer)
                self.buffer.clear()
                return frame
            self.buffer.extend(chunk)


def _bad_constant(value):
    raise ValueError('nonfinite JSON number')


def valid_id(value):
    return type(value) is int and 0 <= value <= (1 << 63) - 1


def validate_request(value):
    if not valid_id(value.get('id')):
        raise WireError('id must be a nonnegative 63-bit integer')
    if type(value.get('v')) is not int or value['v'] != PROTOCOL_VERSION:
        raise WireError('protocol version mismatch; restart trio-interposer')
    op = value.get('op')
    if not isinstance(op, str) or op not in OPS:
        raise WireError('unknown op')
    # Secrets belong in identity files. Reject credentials even on unimplemented ops.
    if any(field in value for field in ('token', 'session_token', 'authkey')):
        raise WireError('credentials must not cross the interposer socket')
    if 'session' in value and value['session'] is not None:
        if not isinstance(value['session'], str) or not SESSION_ID.fullmatch(value['session']):
            raise WireError('bad session id')
    if 'key' in value:
        if not isinstance(value['key'], str) or not IDENTITY_KEY.fullmatch(value['key']):
            raise WireError('bad identity key')
    fields = {
        'hello': ({}, {'client', 'pid', 'version'}),
        'hub.announce': ({'server', 'url'}, set()),
        'list': (set(), set()), 'status': (set(), {'key', 'session', 'host_pid', 'skips'}),
        'session.register': ({'session', 'client', 'host_pid', 'sink', 'host_ok', 'problem'}, {'resume'}),
        'membership.attach': ({'session', 'key', 'server', 'via'}, set()),
        'membership.configure': ({'key'}, {'filter', 'enabled'}),
        'ack.seen': ({'session', 'key', 'through_id'}, set()),
        'turn': ({'session', 'phase'}, set()), 'session.end': ({'session'}, set()),
    }
    if op not in fields:
        raise WireError('not implemented in this version')
    required, optional = fields[op]
    if required - value.keys() or value.keys() - required - optional - {'v', 'id', 'op'}:
        raise WireError('missing or unknown fields')
    if 'session' in required and value.get('session') is None:
        raise WireError('bad session id')
    for field, choices in {'client': ('claude', 'codex'), 'sink': ('rewake', 'queue'),
                           'via': ('connect', 'listen', 'ack'), 'filter': ('all', 'about', 'at'),
                           'phase': ('started', 'ended')}.items():
        if field in value and op != 'hello' and value[field] not in choices:
            raise WireError('bad ' + field)
    for field in ('host_ok', 'enabled', 'resume'):
        if field in value and type(value[field]) is not bool:
            raise WireError('bad ' + field)
    if 'host_pid' in value and value['host_pid'] is not None:
        if not valid_id(value['host_pid']) or value['host_pid'] == 0:
            raise WireError('bad host_pid')
    if 'through_id' in value and not valid_id(value['through_id']):
        raise WireError('bad through_id')
    if 'problem' in value and (not isinstance(value['problem'], str)
            or not re.fullmatch(r'[ -~]{0,200}', value['problem'])):
        raise WireError('bad problem')
    if 'server' in value:
        value['server'] = canonical_server(value['server'])
    if op == 'hub.announce':
        validate_hub(value.get('server'), value.get('url'), allow_restricted=True)
    if 'skips' in value:
        if (op != 'status' or type(value['skips']) is not bool
                or (value['skips'] and any(field in value for field in ('key', 'session')))):
            raise WireError('skips must be a status boolean without key or session filters')
    return op


def canonical_server(server):
    if not isinstance(server, str) or not re.fullmatch(r'nth[-_][A-Za-z0-9_-]{1,40}', server):
        raise WireError('bad hub server name')
    return server.replace('_', '-')


def validate_server(server):
    return canonical_server(server)


def validate_hub(server, url, *, allow_restricted=False):
    validate_server(server)
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        if (not parts or len(url) > 512 or parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(ord(c) < 33 for c in url)):
            raise ValueError
        parts.port
    except ValueError:
        raise WireError('bad hub URL; credentials, query and fragment are forbidden') from None
    if not allow_restricted:
        from nth_interposer_hubs import restricted_host
        if restricted_host(url):
            raise WireError('loopback, link-local and metadata hub hosts are forbidden')


class Client:
    """One sequential connection; call returns the reply's ok payload or raises."""
    def __init__(self, sock):
        self.socket = sock
        self.reader = sock.makefile('rb')
        self.next_id = 0
        self.hello = None

    def call(self, op, **fields):
        request_id = self.next_id
        self.next_id += 1
        request = dict(fields, v=PROTOCOL_VERSION, id=request_id, op=op)
        validate_request(request)
        self.socket.sendall(encode_frame(request))
        reply = deadline_frame(self.socket, self.deadline) if hasattr(self, 'deadline') else read_frame(self.reader)
        if (reply.get('id') is None and 'error' in reply and 'ok' not in reply
                and type(reply.get('v')) is int and reply['v'] == PROTOCOL_VERSION):
            raise ServiceRefused(reply['error'].get('message', 'service error') if isinstance(reply['error'], dict) else str(reply['error']))
        if (reply.get('v') != PROTOCOL_VERSION or type(reply.get('v')) is not int
                or not valid_id(reply.get('id')) or reply['id'] != request_id):
            raise WireError('invalid reply id or protocol version; restart trio-interposer')
        if ('ok' in reply) == ('error' in reply):
            raise WireError('reply must contain exactly one of ok or error')
        if 'error' in reply:
            raise ServiceRefused(reply['error'].get('message', 'service error') if isinstance(reply['error'], dict)
                            else str(reply['error']))
        return reply['ok']

    def close(self):
        self.reader.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _connect(timeout, path=None):
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path if path is not None else socket_path()))
        sock.settimeout(max(.001, deadline - time.monotonic()))
        client = Client(sock)
        try:
            client.hello = client.call('hello', client='cli', pid=os.getpid())
            if (not isinstance(client.hello, dict)
                    or type(client.hello.get('protocol_min')) is not int
                    or type(client.hello.get('protocol_max')) is not int
                    or not client.hello.get('protocol_min', 2) <= PROTOCOL_VERSION
                    <= client.hello.get('protocol_max', 0)):
                raise WireError('hello protocol version mismatch; restart trio-interposer')
            if (not isinstance(client.hello.get('version'), str) or not client.hello['version']
                    or type(client.hello.get('schema_version')) is not int or client.hello['schema_version'] < 1
                    or type(client.hello.get('pid')) is not int or client.hello['pid'] <= 1):
                raise WireError('bad hello metadata; restart trio-interposer')
            return client
        except BaseException:
            client.close()
            raise
    except BaseException:
        sock.close()
        raise


def _spawn():
    # An inherited systemd activation fd belongs only to the service systemd starts.
    env = dict(os.environ)
    # Pin selection before launch. In tests this validates the guard before cwd
    # creation, and the child can never rediscover a real user runtime directory.
    env['NTH_INTERPOSER_SOCKET'] = str(socket_path())
    for field in ('LISTEN_FDS', 'LISTEN_PID', 'LISTEN_FDNAMES', 'PYTHONPATH', 'PYTHONHOME'):
        env.pop(field, None)
    # -I excludes cwd/PYTHONPATH; bootstrap only the installed sibling directory.
    script = Path(__file__).resolve().with_name('nth_interposer.py')
    bootstrap = ('import runpy,sys; from pathlib import Path; '
                 'sys.path.insert(0,str(Path(sys.argv[1]).parent)); '
                 'p=sys.argv.pop(1); runpy.run_path(p,run_name="__main__")')
    process = subprocess.Popen([sys.executable, '-I', '-c', bootstrap, str(script), 'serve'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                            cwd=run_dir(), env=env)
    # The starter can be long-lived. Reap this child when it exits, even when no
    # subsequent connect occurs; do not leave zombie cleanup to Popen's GC.
    threading.Thread(target=process.wait, daemon=True, name='interposer-reaper').start()
    return process


def _ps_command():
    # Production uses the OS utility, never a caller-controlled PATH command.
    # Tests may substitute a fake ps only within their explicit temporary guard.
    root = _test_runtime_root()
    fake = shutil.which('ps') if root is not None else None
    if fake and _inside_test_runtime(Path(fake), root):
        return fake
    return '/bin/ps'


def service_process(pid, proc_root=Path('/proc'), *, platform=None):
    if type(pid) is not int or pid <= 1 or pid == os.getpid():
        return False
    try:
        if (platform or sys.platform).startswith('linux'):
            with (proc_root / str(pid) / 'cmdline').open('rb') as stream:
                arguments = [os.fsdecode(arg) for arg in stream.read(8192).split(b'\0')]
        else:
            result = subprocess.run([_ps_command(), '-o', 'command=', '-p', str(pid)],
                                    capture_output=True, text=True, timeout=1)
            if result.returncode != 0:
                return False
            arguments = shlex.split(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return any(Path(arg).name == 'nth_interposer.py' and arguments[index + 1] == 'serve'
               for index, arg in enumerate(arguments[:-1]))


def stop_fallback(*, timeout=5, path=None):
    try:
        with connect(timeout=min(1, timeout), path=path) as client:
            if client.hello.get('activation') == 'systemd':
                return False
            pid = client.hello.get('pid')
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    if not service_process(pid):
        raise WireError('refusing to signal an unverified interposer pid')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while service_process(pid):
        if time.monotonic() >= deadline:
            raise TimeoutError('interposer did not stop within 5 seconds')
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    return True


def connect(spawn=False, *, timeout=10, path=None):
    """Handshake without activating a fallback unless explicitly requested."""
    deadline = time.monotonic() + timeout
    try:
        return _connect(timeout, path)
    except OSError as exc:
        if not spawn or exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise
    with file_lock(run_dir() / 'spawn.lock', timeout=max(0, deadline - time.monotonic())):
        try:
            return _connect(max(.001, deadline - time.monotonic()), path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
        process = _spawn()
        while time.monotonic() < deadline:
            try:
                return _connect(max(.001, deadline - time.monotonic()), path)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
            if process.poll() is not None:
                raise WireError('interposer failed to start; see trio interposer logs')
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
    raise TimeoutError('interposer did not start within the connect timeout')


_observation = threading.local()


@contextmanager
def observation():
    """One hook's registration/attach/configure sequence shares a sub-second budget."""
    previous = getattr(_observation, 'deadline', None)
    _observation.deadline = time.monotonic() + .8
    try:
        yield
    finally:
        _observation.deadline = previous


def deadline_frame(sock, deadline):
    data = bytearray()
    while len(data) <= MAX_FRAME:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('frame deadline')
        sock.settimeout(remaining)
        byte = sock.recv(1)
        if not byte:
            raise EOFError
        data.extend(byte)
        if byte == b'\n':
            return read_frame(io.BytesIO(data))
    raise WireError('frame exceeds 64 KiB')


def tell(op, **fields):
    """Observe a hook without spawning or letting a failed observer block delivery."""
    if os.environ.get('TRIO_INTERPOSER_SHADOW') == '0':
        return False
    request = dict(fields, v=PROTOCOL_VERSION, id=1, op=op)
    try:
        budget = getattr(_observation, 'deadline', None) or time.monotonic() + .8
        validate_request(request)
        encode_frame(request)
        # One shared reply deadline bounds BOTH hello and the op, including a
        # service that answers hello but hangs on the next request.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            remaining = budget - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(min(.3, remaining))
            sock.connect(str(socket_path()))
            deadline = min(budget, time.monotonic() + .5)
            with Client(sock) as client:
                client.deadline = deadline
                sock.settimeout(max(.001, deadline - time.monotonic()))
                hello = client.call('hello', client='frontend', pid=os.getpid())
                if not hello['protocol_min'] <= PROTOCOL_VERSION <= hello['protocol_max']:
                    raise WireError('hello protocol version mismatch')
                sock.settimeout(max(.001, deadline - time.monotonic()))
                client.call(op, **fields)
        return True
    except ServiceRefused:
        return False
    except (OSError, EOFError, TimeoutError):
        if op=='turn':
            try:
                from nth_interposer_store import _json_file
                state = _json_file(home()/'events'/'hooks'/('session-'+fields['session']+'.json'))
                if not state.get('memberships'):
                    return False
            except Exception:
                return False
        try:
            # Invalid input is not durable work; never save arbitrary hook fields.
            validate_request(request)
            frame = encode_frame(request)
            inbox = private_dir(home() / 'events' / 'inbox')
            # Share fallback time across a hook batch; contention must not turn
            # three short observations into three new lock deadlines.
            with file_lock(inbox / 'write.lock', timeout=min(.15,max(0,budget+.15-time.monotonic()))):
                files = sorted(inbox.glob('*.json'))
                for path in files[:max(0, len(files) - 999)]:
                    path.unlink(missing_ok=True)
                target = inbox / f'{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.json'
                temp = target.with_suffix('.tmp')
                try:
                    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(frame)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        except Exception:
            pass  # Observation must never make a successful hook fail.
        return False
    except Exception:
        return False
