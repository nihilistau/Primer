"""Text engines: one protocol, an OpenAI-compatible and an sp-daemon implementation."""
from __future__ import annotations

import http.client
import socket
import threading
import urllib.error
import urllib.request
from typing import Callable, Iterator, Protocol
from urllib.parse import urlsplit

from primer.config import EngineSpec

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# a local model server is never reached through a system HTTP proxy
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EngineError(RuntimeError):
    """Connect, HTTP or mid-stream failure of a text engine."""


class StreamCancel:
    """Stops a text stream from another thread, even one stalled inside a read: the engine
    bind()s an abort for its in-flight response, and cancel() runs it, so the reading thread
    wakes at once with EngineError("stream cancelled"). Binding after cancel() aborts at once.
    Idempotent and thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abort: Callable[[], None] | None = None
        self.cancelled = False

    def bind(self, abort: Callable[[], None]) -> None:
        with self._lock:
            self._abort = abort
            now = self.cancelled
        if now:
            abort()

    def cancel(self) -> None:
        with self._lock:
            if self.cancelled:
                return
            self.cancelled = True
            abort = self._abort
        if abort is not None:
            abort()


def abort_response(resp) -> None:
    """Close the connection under an urllib HTTP response from any thread (see abort_socket)."""
    abort_socket(getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None))


def abort_socket(sock) -> None:
    """Close a connection from any thread: a read blocked on it (headers or body) fails at once,
    and the server sees the connection close (which stops generation). shutdown() wakes a
    blocked read on POSIX but not on Windows, where only closing the handle does; the handle is
    detached from its socket object first, so the reading thread's own close() later is a no-op
    and can never close a reused handle number."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed or reset
    try:
        fd = sock.detach()
        if fd >= 0:
            socket.close(fd)
    except OSError:
        pass


def open_stream(opener: urllib.request.OpenerDirector, url: str, data: bytes, headers: dict,
                timeout: float, cancel: StreamCancel | None):
    """POST `data` and return the response once its headers arrive; HTTP errors raise
    urllib.error.HTTPError as urllib does. With a cancel and a loopback server, the connection
    is opened here and the cancel bound to its socket before the request goes out, so it also
    stops a server that hasn't answered yet (llama-server sends no headers while it reads the
    prompt). Otherwise `opener` is used (proxies for remote hosts) and the cancel is bound once
    the headers are in."""
    parts = urlsplit(url)
    if cancel is None or (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        resp = opener.open(urllib.request.Request(url, data=data, headers=headers, method="POST"),
                           timeout=timeout)
        if cancel is not None:
            cancel.bind(lambda: abort_response(resp))
        return resp
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.connect()
        sock = conn.sock
        cancel.bind(lambda: abort_socket(sock))
        conn.request("POST", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                     body=data, headers=headers)
        resp = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    # Exactly as urllib does: drop the connection's own reference to the socket, which stays
    # open until the response is closed. Never conn.close(): on a keep-alive response (llama-
    # server: HTTP/1.1, chunked) that also closes the response being returned.
    if conn.sock:
        conn.sock.close()
        conn.sock = None
    if resp.status >= 400:
        resp.close()
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
    return resp


class TextEngine(Protocol):
    def stream(self, messages: list[dict], *, max_tokens: int = 1200,
               temperature: float = 0.7, cancel: StreamCancel | None = None) -> Iterator[str]: ...

    def complete(self, messages: list[dict], *, max_tokens: int = 400,
                 temperature: float = 0.2) -> str: ...

    def health(self) -> bool: ...


def opener_for(base_url: str) -> urllib.request.OpenerDirector:
    """No proxy for a loopback server; anything else (e.g. the hosted tier, which may sit
    behind a corporate proxy) uses the environment's proxies as usual."""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        host = ""
    return _DIRECT if host in LOOPBACK_HOSTS else urllib.request.build_opener()


def make_engine(spec: EngineSpec) -> TextEngine:
    if spec.kind == "openai":
        from primer.engines.openai_compat import OpenAICompatEngine
        return OpenAICompatEngine(spec)
    if spec.kind == "sp":
        from primer.engines.sp_daemon import SpDaemonEngine
        return SpDaemonEngine(spec)
    if spec.kind == "sdserver":
        raise EngineError("sdserver is not a text engine")
    raise EngineError(f"unknown engine kind {spec.kind!r}")
