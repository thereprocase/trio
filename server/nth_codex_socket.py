"""Attach to an existing Codex WebSocket endpoint without owning its process.

Codex's Unix control sockets carry WebSocket frames, not newline JSON. Its
`app-server proxy` copies raw bytes and does not perform that conversion.
The optional websockets dependency is imported only when connecting.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import urllib.parse

from nth_codex_runtime import CodexAppServerClient, CodexProtocolError


class CodexSocketClient(CodexAppServerClient):
    def __init__(self, endpoint, *, on_notification=None, on_server_request=None):
        super().__init__(command=[], on_notification=on_notification,
                         on_server_request=on_server_request)
        self.endpoint = endpoint
        self.socket = None

    def start(self, timeout=15):
        if self.alive():
            return dict(self.initialize_result)
        from websockets.sync.client import connect, unix_connect
        self._closed.clear()
        if self._request_executor_closed:
            self._request_executor = ThreadPoolExecutor(max_workers=8)
            self._request_executor_closed = False
        if self._notify_executor_closed:
            self._notify_executor = ThreadPoolExecutor(max_workers=1)
            self._notify_executor_closed = False
        if self.endpoint.startswith('unix://'):
            path = self.endpoint[len('unix://'):]
            if not os.path.isabs(path):
                raise ValueError('Explicit absolute Unix socket path required')
            self.socket = unix_connect(path, open_timeout=timeout, proxy=None,
                                       compression=None, max_size=16 * 1024 * 1024)
        else:
            url = urllib.parse.urlsplit(self.endpoint)
            if url.scheme not in ('ws', 'wss') or url.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('Use a local Codex socket or loopback WebSocket endpoint')
            self.socket = connect(self.endpoint, open_timeout=timeout, proxy=None,
                                  max_size=16 * 1024 * 1024)
        self._reader = threading.Thread(target=self._read_socket, daemon=True)
        self._reader.start()
        try:
            self.initialize_result = self.request('initialize', {
                'clientInfo': {'name': 'trio_relay', 'title': 'Trio event relay', 'version': '0.1.0'},
            }, timeout=timeout)
            self.notify('initialized', {})
        except Exception:
            self.stop()
            raise
        return dict(self.initialize_result)

    def _read_socket(self):
        try:
            self._read_messages(iter(self.socket))
        except Exception as exc:
            self._stderr.append(f'control connection closed: {type(exc).__name__}')
            self._closed.set()
            self._fail_pending('Codex control connection closed')

    def alive(self):
        return self.socket is not None and not self._closed.is_set()

    @property
    def pid(self):
        return None  # Borrowed connection: never claim ownership of Codex.

    def _send(self, message):
        if not self.alive():
            raise CodexProtocolError('Codex control connection is unavailable')
        try:
            with self._write_lock:
                self.socket.send(json.dumps(message, separators=(',', ':')))
        except Exception as exc:
            raise CodexProtocolError('Codex control connection write failed') from exc

    def _handle_server_request(self, message):
        # Requests may be broadcast to every subscriber. An observer must not
        # race the interactive client by answering (or rejecting) its approvals.
        if self.on_server_request is not None:
            super()._handle_server_request(message)
        else:
            self._stderr.append('UI request left to owner: ' + str(message.get('method')))

    def stop(self, grace=3):
        self._closed.set()
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=grace)
        super().stop(grace=grace)
