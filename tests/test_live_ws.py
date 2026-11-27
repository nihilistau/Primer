"""primer.live.ws against a tiny stdlib WebSocket server on 127.0.0.1."""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time

import pytest

from primer.live.ws import WebSocket, WsError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ServerConn:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.request = ""

    def read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def handshake(self, bad_key=False, first=None):
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.request = head.decode()
        key = ""
        for ln in self.request.split("\r\n")[1:]:
            k, _, v = ln.partition(":")
            if k.strip().lower() == "sec-websocket-key":
                key = v.strip()
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if bad_key:
            acc = "AAAA"
        resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode()
        if first is not None:  # the first frame in the same segment as the 101
            resp += bytes([0x81, len(first)]) + first
        self.sock.sendall(resp)

    def read_frame(self):
        b1, b2 = self.read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.read(8))[0]
        assert b2 & 0x80, "client frames must be masked"
        key = self.read(4)
        data = bytes(b ^ key[i % 4] for i, b in enumerate(self.read(n)))
        return b1 & 0x0F, bool(b1 & 0x80), data

    def send_frame(self, op, payload=b"", fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        self.sock.sendall(head + payload)  # unmasked, like Chrome

    def send_text(self, s):
        self.send_frame(1, s.encode())


class WsServer:
    """Accepts one connection and runs handler(conn) on a thread."""

    def __init__(self, handler, bad_key=False, first=None):
        self.handler = handler
        self.first = first
        self.bad_key = bad_key
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.conn = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/devtools/browser/abc"

    def _run(self):
        try:
            s, _ = self.lsock.accept()
            s.settimeout(5)
            self.conn = ServerConn(s)
            self.conn.handshake(self.bad_key, self.first)
            self.handler(self.conn)
        except (EOFError, OSError):
            pass
        except Exception as e:  # surfaced by the test
            self.error = e
        finally:
            try:
                if self.conn:
                    self.conn.sock.close()
            except OSError:
                pass

    def stop(self):
        self.lsock.close()


@pytest.fixture
def serve():
    servers = []

    def make(handler, **kw):
        s = WsServer(handler, **kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()
    for s in servers:  # a handler that failed (an assert in it, say) fails the test
        s.thread.join(10)
        assert not s.thread.is_alive(), "the test server's handler never finished"
        assert s.error is None, f"the test server failed: {s.error!r}"


def _echo(conn):
    while True:
        op, _fin, data = conn.read_frame()
        if op == 8:
            conn.send_frame(8, data[:2])
            return
        conn.send_frame(op, data)


def test_handshake_and_text_round_trip(serve):
    s = serve(_echo)
    ws = WebSocket.connect(s.url, 2)
    try:
        ws.send_text("héllo")
        assert ws.recv() == "héllo"
    finally:
        ws.close()
    assert "GET /devtools/browser/abc HTTP/1.1" in s.conn.request
    assert "Host: 127.0.0.1:%d" % s.port in s.conn.request


def test_bad_accept_key_is_refused(serve):
    s = serve(_echo, bad_key=True)
    with pytest.raises(WsError):
        WebSocket.connect(s.url, 2)


def test_large_message_64bit_length(serve):
    s = serve(_echo)
    ws = WebSocket.connect(s.url, 2)
    try:
        big = "x" * 70000
        ws.send_text(big)
        assert ws.recv() == big
    finally:
        ws.close()


def test_fragmented_message_is_joined(serve):
    def handler(conn):
        conn.send_frame(1, b"hel", fin=False)
        conn.send_frame(0, b"lo ", fin=False)
        conn.send_frame(0, b"world", fin=True)
        conn.read_frame()

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2)
    try:
        assert ws.recv() == "hello world"
    finally:
        ws.close()


def test_ping_is_answered_with_pong(serve):
    got = {}

    def handler(conn):
        conn.send_frame(9, b"hi")
        op, _f, data = conn.read_frame()
        got["pong"] = (op, data)
        conn.send_text("after")
        conn.read_frame()

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2)
    try:
        assert ws.recv() == "after"
    finally:
        ws.close()
    assert got["pong"] == (10, b"hi")


def test_server_close_returns_none(serve):
    def handler(conn):
        conn.send_frame(8, struct.pack(">H", 1000))
        try:
            conn.read_frame()
        except (EOFError, OSError):
            pass

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2)
    assert ws.recv() is None
    assert ws.recv() is None


def test_dropped_socket_raises(serve):
    s = serve(lambda conn: None)
    ws = WebSocket.connect(s.url, 2)
    with pytest.raises(WsError):
        ws.recv()


def test_message_cap_on_one_frame(serve):
    got = {}

    def handler(conn):
        # a header claiming 5000 bytes, without sending them
        conn.sock.sendall(bytes([0x81, 126]) + struct.pack(">H", 5000))
        op, _f, data = conn.read_frame()
        got["close"] = (op, struct.unpack(">H", data[:2])[0])

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2, max_message=1000)
    t0 = time.monotonic()
    with pytest.raises(WsError, match="too large"):
        ws.recv()
    assert time.monotonic() - t0 < 1
    s.thread.join(2)
    assert got["close"] == (8, 1009)


def test_message_cap_on_the_sum_of_fragments(serve):
    def handler(conn):
        conn.send_frame(1, b"a" * 600, fin=False)
        conn.send_frame(0, b"b" * 600, fin=True)
        try:
            conn.read_frame()
        except (EOFError, OSError):
            pass

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2, max_message=1000)
    with pytest.raises(WsError, match="too large"):
        ws.recv()


def test_16_bit_length(serve):
    s = serve(_echo)
    ws = WebSocket.connect(s.url, 2)
    try:
        ws.send_text("y" * 1000)
        assert ws.recv() == "y" * 1000
    finally:
        ws.close()


def test_ping_between_fragments(serve):
    got = {}

    def handler(conn):
        conn.send_frame(1, b"one ", fin=False)
        conn.send_frame(9, b"p")
        op, _f, data = conn.read_frame()
        got["pong"] = (op, data)
        conn.send_frame(0, b"two", fin=True)
        conn.read_frame()

    s = serve(handler)
    ws = WebSocket.connect(s.url, 2)
    try:
        assert ws.recv() == "one two"
    finally:
        ws.close()
    assert got["pong"] == (10, b"p")


def test_first_frame_in_the_handshake_segment(serve):
    s = serve(lambda conn: conn.read_frame(), first=b"early")
    ws = WebSocket.connect(s.url, 2)
    try:
        assert ws.recv() == "early"
    finally:
        ws.close()


def _expect_failure(serve, handler):
    s = serve(handler)
    ws = WebSocket.connect(s.url, 2)
    with pytest.raises(WsError):
        ws.recv()


def test_masked_server_frame_is_refused(serve):
    def handler(conn):
        conn.sock.sendall(bytes([0x81, 0x80 | 2]) + b"\0\0\0\0" + b"hi")
        try:
            conn.read_frame()
        except (EOFError, OSError):
            pass

    _expect_failure(serve, handler)


def test_stray_continuation_is_refused(serve):
    _expect_failure(serve, lambda conn: conn.send_frame(0, b"x"))


def test_new_message_inside_fragmented_one_is_refused(serve):
    def handler(conn):
        conn.send_frame(1, b"a", fin=False)
        conn.send_frame(1, b"b")

    _expect_failure(serve, handler)


def test_fragmented_control_frame_is_refused(serve):
    _expect_failure(serve, lambda conn: conn.send_frame(9, b"x", fin=False))


def test_handshake_timeout_is_a_ws_error():
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    try:
        t0 = time.monotonic()
        with pytest.raises(WsError, match="handshake"):
            WebSocket.connect("ws://127.0.0.1:%d/x" % ls.getsockname()[1], 0.3)
        assert time.monotonic() - t0 < 2
    finally:
        ls.close()


def test_send_timeout_closes_the_socket(serve):
    release = threading.Event()
    s = serve(lambda conn: release.wait(5))
    ws = WebSocket.connect(s.url, 2, send_timeout=0.3)
    t0 = time.monotonic()
    try:
        with pytest.raises(WsError):
            for _ in range(40):
                ws.send_text("z" * 8_000_000)
        assert time.monotonic() - t0 < 5
        with pytest.raises(WsError):
            ws.send_text("again")
    finally:
        release.set()
        ws.close()


@pytest.mark.parametrize("url", [
    "ws://localhost:9222/x", "ws://127.0.0.2:9222/x", "ws://172.31.0.5:9222/x",
    "ws://169.254.169.254/x", "wss://127.0.0.1:9222/x", "http://127.0.0.1:9222/x",
    "ws://[::1]:9222/x", "ws://127.0.0.1.evil.test:9222/x",
])
def test_non_loopback_url_is_refused(url):
    with pytest.raises(ValueError):
        WebSocket.connect(url, 1)
