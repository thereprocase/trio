"""MCPSSEClient.close() must unblock its reader thread and return, for both kinds of
SSE stream http.client distinguishes. Loopback only; no hub, credentials or model calls."""
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from nth_spoke_monitor import MCPSSEClient

EVENT = b'event: endpoint\ndata: /messages/?session_id=test\n\n'


class HoldingServer:
    """Answers one SSE request with the endpoint event, then holds the stream open
    and silent: the state a reader thread is in for nearly all of its life."""

    def __init__(self, chunked):
        self.chunked = chunked
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.release = threading.Event()
        self.peer_closed = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        connection, _ = self.listener.accept()
        with connection:
            connection.recv(65536)
            head = b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n'
            if self.chunked:
                # What a real hub sends. http.client keeps the socket on the connection.
                body = head + b'Transfer-Encoding: chunked\r\n\r\n' + b'%x\r\n%s\r\n' % (len(EVENT), EVENT)
            else:
                # Close-delimited. http.client hands the socket over to the response.
                body = head + b'\r\n' + EVENT
            connection.sendall(body)
            connection.settimeout(10)
            try:
                if connection.recv(1) == b'':
                    self.peer_closed.set()
            except OSError:
                pass
            self.release.wait(10)

    def close(self):
        self.release.set()
        self.listener.close()


class CloseTests(unittest.TestCase):
    def check(self, chunked):
        try:
            server = HoldingServer(chunked)
        except OSError as exc:
            self.skipTest(f'loopback sockets unavailable here: {exc}')
        self.addCleanup(server.close)
        client = MCPSSEClient(f'http://127.0.0.1:{server.port}/sse')
        client._sse_thread = threading.Thread(target=client._sse_loop, daemon=True)
        client._sse_thread.start()
        self.assertTrue(client.endpoint_ready.wait(5))
        time.sleep(.3)                       # let the reader settle into its blocking read
        finished = threading.Event()

        def close():
            client.close()
            finished.set()

        threading.Thread(target=close, daemon=True).start()
        self.assertTrue(finished.wait(5), 'close() blocked behind the reader thread')
        client._sse_thread.join(5)
        self.assertFalse(client._sse_thread.is_alive(), 'the reader thread was never unblocked')
        self.assertTrue(server.peer_closed.wait(5))

    def test_chunked_stream_close_does_not_deadlock_behind_the_reader(self):
        self.check(chunked=True)

    def test_close_delimited_stream_close_still_unblocks_the_reader(self):
        self.check(chunked=False)


if __name__ == '__main__':
    unittest.main()
