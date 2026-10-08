"""Private JSON-lines IPC for the spoke interposer (no MCP dependencies)."""
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
MAX_FRAME = 64 * 1024  # Includes the newline, so readers never buffer an unbounded line.
SESSION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{5,79}\Z')
IDENTITY_KEY = re.compile(r'[0-9a-f]{24}\Z')
OPS = frozenset(('hello', 'hub.announce', 'session.register', 'membership.attach',
                 'membership.configure', 'ack.seen', 'turn', 'session.end', 'wait',
                 'subscribe', 'delivered', 'status', 'list'))


class WireError(ValueError):
    """A fixed, non-secret error suitable for sending to an IPC client."""


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


def socket_path():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return (Path(runtime) / 'trio' if runtime else home() / 'run') / 'interposer.sock'


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
    if op == 'hub.announce':
        validate_hub(value.get('server'), value.get('url'))
    return op


def validate_hub(server, url):
    if not isinstance(server, str) or not re.fullmatch(r'nth-[A-Za-z0-9_.-]{1,100}', server):
        raise WireError('bad hub server name')
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        if (not parts or parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or any(ord(c) < 33 for c in url)):
            raise ValueError
        parts.port  # Reject malformed ports before saving an allowlist entry.
    except ValueError:
        raise WireError('bad hub URL; credentials, query and fragment are forbidden') from None


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
        reply = read_frame(self.reader)
        if (reply.get('v') != PROTOCOL_VERSION or type(reply.get('v')) is not int
                or not valid_id(reply.get('id')) or reply['id'] != request_id):
            raise WireError('invalid reply id or protocol version; restart trio-interposer')
        if ('ok' in reply) == ('error' in reply):
            raise WireError('reply must contain exactly one of ok or error')
        if 'error' in reply:
            raise WireError(reply['error'] if isinstance(reply['error'], str) else 'service error')
        return reply['ok']

    def close(self):
        self.reader.close()
        self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _connect(timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path()))
        client = Client(sock)
        try:
            client.hello = client.call('hello', client='cli', pid=os.getpid())
            if (not isinstance(client.hello, dict)
                    or not client.hello.get('protocol_min', 2) <= PROTOCOL_VERSION
                    <= client.hello.get('protocol_max', 0)):
                raise WireError('hello protocol version mismatch; restart trio-interposer')
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
    for field in ('LISTEN_FDS', 'LISTEN_PID', 'LISTEN_FDNAMES'):
        env.pop(field, None)
    return subprocess.Popen([sys.executable, str(Path(__file__).with_name('nth_interposer.py')), 'serve'],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=env)


def connect(spawn=False, *, timeout=10):
    """Handshake without activating a fallback unless explicitly requested."""
    deadline = time.monotonic() + timeout
    try:
        return _connect(timeout)
    except OSError as exc:
        if not spawn or exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise
    with file_lock(run_dir() / 'spawn.lock', timeout=max(0, deadline - time.monotonic())):
        try:
            return _connect(max(.01, deadline - time.monotonic()))
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
        process = _spawn()
        while time.monotonic() < deadline:
            try:
                return _connect(max(.01, deadline - time.monotonic()))
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
            if process.poll() is not None:
                raise WireError('interposer failed to start; see trio interposer logs')
            time.sleep(.05)
    raise TimeoutError('interposer did not start within the connect timeout')
