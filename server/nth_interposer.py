#!/usr/bin/env python3
"""Per-user spoke interposer skeleton: storage and IPC, no polling or delivery."""
import argparse
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time

from nth_constants import NTH_VERSION
from nth_interposer_store import Store, SCHEMA_VERSION
from nth_interposer_wire import (PROTOCOL_VERSION, WireError, encode_frame, read_frame,
                                validate_request, valid_id, home, private_dir, run_dir,
                                socket_path, file_lock)


def peer_allowed(sock):
    if not sys.platform.startswith('linux'):
        return True  # Other Unix platforms still enforce the private socket/directory.
    try:
        _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == os.getuid()
    except OSError:
        return False  # Fail closed if the kernel cannot identify a Linux peer.


def activated_socket():
    """Consume the single systemd descriptor only when addressed to this process."""
    try:
        pid, count = int(os.environ.get('LISTEN_PID', '0')), int(os.environ.get('LISTEN_FDS', '0'))
    except ValueError:
        raise WireError('invalid systemd socket activation environment') from None
    if pid != os.getpid() or count == 0:
        return None
    if count != 1:
        raise WireError('expected one systemd socket activation descriptor')
    sock = socket.socket(fileno=3)
    if (sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM
            or sock.getsockname() != str(socket_path()) or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)):
        sock.close()
        raise WireError('invalid systemd interposer socket')
    for field in ('LISTEN_FDS', 'LISTEN_PID', 'LISTEN_FDNAMES'):
        os.environ.pop(field, None)
    os.set_inheritable(sock.fileno(), False)
    return sock


@contextmanager
def service_log():
    path = private_dir(home() / 'logs') / 'interposer.log'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    stream = os.fdopen(descriptor, 'a', encoding='utf-8')
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger('trio.interposer')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        handler.close()
        stream.close()


def dispatch(store, request):
    op = validate_request(request)
    if op == 'hello':
        return {'version': NTH_VERSION, 'protocol_min': PROTOCOL_VERSION,
                'protocol_max': PROTOCOL_VERSION, 'schema_version': SCHEMA_VERSION, 'pid': os.getpid()}
    if op == 'hub.announce':
        return store.announce(request['server'], request['url'])
    if op in ('list', 'status'):
        return store.snapshot(request.get('key') if op == 'status' else None,
                              request.get('session') if op == 'status' else None)
    raise WireError('not implemented in this version')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        greeted = False
        self.request.settimeout(10)
        while True:
            request_id = None
            try:
                request = read_frame(self.rfile)
                request_id = request.get('id') if valid_id(request.get('id')) else None
                op = validate_request(request)
                if not greeted and op != 'hello':
                    raise WireError('first frame must be hello')
                payload = dispatch(self.server.store, request)
                reply = {'v': PROTOCOL_VERSION, 'id': request_id, 'ok': payload}
                greeted = True
            except EOFError:
                return
            except WireError as exc:
                reply = {'v': PROTOCOL_VERSION, 'id': request_id, 'error': str(exc)}
                self.server.log.info('request refused: WireError')
                # A framing error leaves the stream ambiguous. Send one bounded
                # error and close, rather than treating its tail as a fresh op.
                self.wfile.write(encode_frame(reply))
                return
            except (OSError, ValueError, TypeError) as exc:
                self.server.log.warning('request failed: %s', type(exc).__name__)
                return
            try:
                self.wfile.write(encode_frame(reply))
            except WireError:
                self.wfile.write(encode_frame({'v': PROTOCOL_VERSION, 'id': request_id,
                                              'error': 'response exceeds 64 KiB; use status with a key or session'}))
            except OSError:
                return


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, store, log, inherited=None):
        self.store, self.log = store, log
        super().__init__(str(path), Handler, bind_and_activate=False)
        if inherited is not None:
            self.socket.close()
            self.socket = inherited
            self.server_address = inherited.getsockname()
        else:
            try:
                self.server_bind()
                os.chmod(path, 0o600)
                self.server_activate()
            except BaseException:
                self.server_close()
                raise
        self.timeout = .1

    def verify_request(self, request, client_address):
        allowed = peer_allowed(request)
        if not allowed:
            self.log.warning('peer refused: foreign uid or unavailable credentials')
        return allowed

    def handle_error(self, request, client_address):
        # socketserver's default prints traceback/peer data on stderr. The service
        # records only exception classes, never request fields or credentials.
        self.log.warning('connection failed: %s', sys.exc_info()[0].__name__)


def remove_stale_socket(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('refusing to replace a non-socket or foreign socket')
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(.5)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            pass
        else:
            raise WireError('interposer socket is already listening')
    finally:
        probe.close()
    # The lease serializes fallback service starts; never unlink systemd's socket.
    path.unlink()


def serve(*, idle_seconds=1800, stop=None):
    if idle_seconds <= 0:
        raise ValueError('idle timeout must be positive')
    stop = stop if stop is not None else threading.Event()
    with file_lock(run_dir() / 'lease.lock'):
        path = socket_path()
        private_dir(path.parent)
        inherited = activated_socket()
        if inherited is None:
            remove_stale_socket(path)
        else:
            path.chmod(0o600)
        with service_log() as log:
            store = Store()
            server = None
            try:
                store.import_hooks()
                server = Server(path, store, log, inherited)
                log.info('service started: protocol=%d schema=%d', PROTOCOL_VERSION, SCHEMA_VERSION)
                idle_since = time.monotonic()
                while not stop.is_set():
                    server.handle_request()
                    if store.live_sessions():
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= idle_seconds:
                        log.info('service idle exit')
                        break
            finally:
                if server is not None:
                    server.server_close()
                    if inherited is None:
                        path.unlink(missing_ok=True)
                elif inherited is not None:
                    inherited.close()
                store.close()
                log.info('service stopped')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve'])
    parser.add_argument('--idle-seconds', type=float, default=1800,
                        help='Exit after this many seconds with zero live sessions (default: 1800)')
    args = parser.parse_args(argv)
    os.umask(0o077)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        serve(idle_seconds=args.idle_seconds, stop=stop)
    except Exception as exc:
        # Fixed error classes only: a damaged legacy file may contain a token.
        with service_log() as log:
            log.error('service startup failed: %s', type(exc).__name__)
        print('interposer failed: ' + type(exc).__name__ + '; see trio interposer logs', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
