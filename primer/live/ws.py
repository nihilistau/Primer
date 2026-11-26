"""A minimal stdlib WebSocket client, for Chrome's local DevTools endpoint only."""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import urllib.parse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 64 * 1024 * 1024
SEND_TIMEOUT = 10.0


class WsError(Exception):
    pass


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _set_send_timeout(sock: socket.socket, seconds: float) -> None:
    """Bound a blocking send (SO_SNDTIMEO), leaving recv fully blocking."""
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, int(seconds * 1000))
        else:
            sec = int(seconds)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack("ll", sec, int((seconds - sec) * 1_000_000)))
    except OSError:
        pass


class WebSocket:
    def __init__(self, sock: socket.socket, max_message: int = MAX_MESSAGE,
                 send_timeout: float = SEND_TIMEOUT):
        self._sock = sock
        self._max = max_message
        _set_send_timeout(sock, send_timeout)
        self._buf = b""
        self._send_lock = threading.Lock()
        self._closed = False
        self._sent_close = False
        self._failed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0, max_message: int = MAX_MESSAGE,
                send_timeout: float = SEND_TIMEOUT) -> "WebSocket":
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws" or u.hostname != "127.0.0.1":
            raise ValueError("only ws://127.0.0.1 is allowed")
        port = u.port or 80
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        except OSError as e:
            raise WsError(f"connect failed: {e}") from e
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            sock.sendall(req.encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WsError("handshake: connection closed")
                buf += chunk
                if len(buf) > 65536:
                    raise WsError("handshake: reply too large")
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
                raise WsError(f"handshake refused: {lines[0]}")
            hdr = {}
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                hdr[k.strip().lower()] = v.strip()
            want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            if hdr.get("sec-websocket-accept") != want:
                raise WsError("handshake: bad accept key")
            sock.settimeout(None)
        except OSError as e:
            sock.close()
            raise WsError(f"handshake: {e}") from e
        except BaseException:
            sock.close()
            raise
        ws = cls(sock, max_message, send_timeout)
        ws._buf = rest
        return ws

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(max(65536, n - len(self._buf)))
            except OSError as e:
                raise WsError(f"socket error: {e}") from e
            if not chunk:
                raise WsError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        with self._send_lock:
            if self._closed:
                raise WsError("socket closed")
            try:
                self._sock.sendall(head + key + _mask(payload, key))
            except OSError as e:
                self._shutdown()
                raise WsError(f"socket error: {e}") from e

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str | None:
        """The next text message, or None once the socket is closed."""
        parts: list[bytes] = []
        total = 0
        in_msg = False
        is_text = True
        try:
            while True:
                b1, b2 = self._read(2)
                fin = bool(b1 & 0x80)
                op = b1 & 0x0F
                n = b2 & 0x7F
                if b2 & 0x80:
                    self._fail(1002, "masked server frame")
                if n == 126:
                    n = struct.unpack(">H", self._read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._read(8))[0]
                if op >= 0x8 and (not fin or n > 125):
                    self._fail(1002, "bad control frame")
                if n > self._max or (in_msg and total + n > self._max):
                    self._fail(1009, "message too large")
                data = self._read(n) if n else b""
                if op == 0x8:
                    if not self._sent_close:
                        self._sent_close = True
                        try:
                            self._send_frame(0x8, data[:2])
                        except WsError:
                            pass
                    self._shutdown()
                    return None
                if op == 0x9:
                    try:
                        self._send_frame(0xA, data)
                    except WsError:
                        pass
                    continue
                if op == 0xA:
                    continue
                if op in (0x1, 0x2):
                    if in_msg:
                        self._fail(1002, "new message inside a fragmented one")
                    parts, total, in_msg, is_text = [data], n, True, op == 0x1
                elif op == 0x0:
                    if not in_msg:
                        self._fail(1002, "stray continuation frame")
                    parts.append(data)
                    total += n
                else:
                    self._fail(1002, f"bad opcode {op}")
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")
        except WsError:
            if self._closed and not self._failed:
                return None
            self._shutdown()
            raise

    def _fail(self, code: int, why: str) -> None:
        self._failed = True
        try:
            self._send_frame(0x8, struct.pack(">H", code))
        except WsError:
            pass
        self._shutdown()
        raise WsError(why)

    def _shutdown(self) -> None:
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def close(self) -> None:
        if not self._closed and not self._sent_close:
            self._sent_close = True
            if self._send_lock.acquire(timeout=1):
                self._send_lock.release()
                try:
                    self._send_frame(0x8, struct.pack(">H", 1000))
                except WsError:
                    pass
        self._shutdown()
