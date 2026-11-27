"""The guarding proxy: the live browser's one way out to the network.

Chrome is started with --proxy-server pointing here (and --proxy-bypass-list=<-loopback>, so
even loopback names come here, and --disable-quic, so nothing goes around it over UDP). Chrome
then never resolves a name for page traffic: this proxy does, once, checks every address it got
(primer.grounding.web._check_host's rules, plus guard.py's local and single-label name rules),
and connects to that checked address by IP. A name that answers public to a check and local to
a second lookup (DNS rebinding) can't reach anything local, because there is no second lookup.

It speaks two forms only:
- `CONNECT host:port` (https and wss): after the checks, 200, then bytes are spliced both ways;
- absolute-form http (`GET http://host/path HTTP/1.1`, for http and ws): the request is
  rewritten to origin-form with Host taken from the URL (a client Host header is dropped), and
  sent to the checked address. The connection carries that one request (or one WebSocket).
Anything else gets 400; a refused host or port 403; a full proxy 503; an unreachable host 502.
Each is a plain HTTP response, then the connection is closed.

The probe: a request for `probe_host` (a random name under .invalid, unique to this proxy) is
never resolved or connected; it is recorded (wait_probe) and answered 403. The live service
has the browser ask for it at start-up and refuses to run unless it arrives, so a browser that
isn't sending its traffic here (a proxy policy overriding --proxy-server, say) never runs.
Chrome applies a proxy policy change at once, so the service asks again (new_probe gives a
fresh name) before each new live window and about once a minute while windows are open."""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import select
import socket
import threading
import time

from primer.grounding.web import WebError, _check_host
from primer.live.guard import ALLOWED_PORTS, _local_name

log = logging.getLogger("primer.live.proxy")

MAX_CONNS = 256
CONNECT_TIMEOUT_S = 10.0
LOOKUP_TIMEOUT_S = 3.0
IDLE_TIMEOUT_S = 120.0
HEAD_MAX = 16 * 1024
HEAD_TIMEOUT_S = 10.0  # the whole request head, however slowly it trickles in
MAX_LOOKUPS = 64  # lookups in flight (a lookup that hangs can't be stopped, only abandoned)
RESPONSE_HEAD_MAX = 64 * 1024
STOP_JOIN_S = 3.0
_ACCEPT_POLL_S = 0.25
_CHUNK = 65536

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_VERSION = re.compile(r"HTTP/1\.[01]")
_NAME = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,252})")
_PORT = re.compile(r"[0-9]{1,5}")
_ABSOLUTE = re.compile(r"(?i)http://([^/?#]*)([^#]*)(?:#.*)?")
_STATUS = re.compile(rb"HTTP/1\.[01] ([0-9]{3})")
_REASONS = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway",
            503: "Service Unavailable"}
_DROP = {"host", "proxy-connection", "proxy-authorization", "proxy-authenticate", "keep-alive"}
PROBE_SUFFIX = ".primer-proxy-probe.invalid"
_new_socket = socket.socket  # the upstream socket (a seam for the tests)


class _Refuse(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class _Drop(Exception):
    """The upstream's response can't be made safe to pass on: close the connection."""


class _Count:
    """The lookups in flight. Lookup threads hold this, never the proxy, so an abandoned
    lookup (one that never returns) keeps nothing of the proxy alive."""

    def __init__(self):
        self.lock = threading.Lock()
        self.n = 0


def _lookup(resolve, count: _Count, box: dict, done: threading.Event, args, kwargs) -> None:
    try:
        box["r"] = list(resolve(*args, **kwargs))
    except BaseException as e:  # noqa: BLE001  (handed back to the caller)
        box["e"] = e
    finally:
        with count.lock:
            count.n -= 1
        done.set()


def _authority(text: str) -> tuple[str, int]:
    """(host, port) from `host:port` or `[v6]:port`. A port is required; userinfo, zone ids,
    paths and anything unusual are refused (400)."""
    if not text or len(text) > 300 or not text.isascii() or any(
            c in text for c in "@/%?#\\ \t\r\n"):
        raise _Refuse(400)
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise _Refuse(400)
        host, rest = text[1:end], text[end + 1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise _Refuse(400) from None
    else:
        host, sep, port = text.rpartition(":")
        if not sep or not host or ":" in host or not _NAME.fullmatch(host):
            raise _Refuse(400)
        rest = ":" + port
    if not rest.startswith(":") or not _PORT.fullmatch(rest[1:]):
        raise _Refuse(400)
    port_n = int(rest[1:])
    if not 0 < port_n < 65536:
        raise _Refuse(400)
    return host.lower(), port_n


class _Plan:
    def __init__(self, host: str, port: int, head: bytes | None):
        self.host, self.port, self.head = host, port, head  # head None: a CONNECT tunnel


def _plan(head: bytes) -> _Plan:
    """What the request head asks for, or _Refuse(400)."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # latin-1 decodes every byte; kept for clarity
        raise _Refuse(400) from None
    lines = text.split("\r\n")
    if any("\r" in ln or "\n" in ln or "\0" in ln for ln in lines):
        raise _Refuse(400)
    parts = lines[0].split(" ")
    if len(parts) != 3 or not _TOKEN.fullmatch(parts[0]) or not _VERSION.fullmatch(parts[2]):
        raise _Refuse(400)
    method, target, version = parts
    headers: list[tuple[str, str]] = []
    for ln in lines[1:]:
        name, sep, value = ln.partition(":")
        if not sep or not _TOKEN.fullmatch(name):  # also refuses obs-fold (a leading space)
            raise _Refuse(400)
        headers.append((name, value.strip(" \t")))
    if method == "CONNECT":
        host, port = _authority(target)
        return _Plan(host, port, None)
    m = _ABSOLUTE.fullmatch(target)
    if not m:  # origin-form, https://, authority-form with another method, ...
        raise _Refuse(400)
    netloc, path = m.group(1), m.group(2) or "/"
    if path.startswith("?"):
        path = "/" + path
    if not path.startswith("/"):
        raise _Refuse(400)
    explicit = netloc.startswith("[") and "]:" in netloc or (
        not netloc.startswith("[") and ":" in netloc)
    host, port = _authority(netloc if explicit else netloc + ":80")
    shown = f"[{host}]" if ":" in host else host
    if explicit:
        shown += f":{port}"
    lower = {n.lower(): v for n, v in headers}
    upgrade = "upgrade" in lower and "upgrade" in [
        t.strip().lower() for t in lower.get("connection", "").split(",")]
    out = [f"{method} {path} {version}", f"Host: {shown}"]
    for name, value in headers:
        low = name.lower()
        if low in _DROP or (low == "connection" and not upgrade):
            continue
        out.append(f"{name}: {value}")
    if not upgrade:
        out.append("Connection: close")
    return _Plan(host, port, ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))


def _close_heads(buf: bytes) -> tuple[bytes, bytes, bool]:
    """Rewrite the response head(s) at the start of `buf` so the browser won't reuse the
    connection: (bytes to send, bytes still held, still rewriting). 1xx heads pass as they are
    and the next is looked at; a 101 (a WebSocket) passes and ends the rewriting."""
    out = b""
    while True:
        i = buf.find(b"\r\n\r\n")
        if i < 0:
            if len(buf) > RESPONSE_HEAD_MAX:
                raise _Drop("response head too long")
            return out, buf, True
        head, buf = buf[:i], buf[i + 4:]
        m = _STATUS.match(head)
        if not m:
            raise _Drop("not an HTTP/1.x response")
        code = int(m.group(1))
        if code == 101:
            return out + head + b"\r\n\r\n" + buf, b"", False
        if 100 <= code < 200:
            out += head + b"\r\n\r\n"
            continue
        lines = [ln for ln in head.split(b"\r\n")[1:]
                 if ln.split(b":", 1)[0].strip().lower()
                 not in (b"connection", b"keep-alive", b"proxy-connection")]
        head = b"\r\n".join([head.split(b"\r\n", 1)[0], *lines, b"Connection: close"])
        return out + head + b"\r\n\r\n" + buf, b"", False


class GuardProxy:
    """A loopback proxy that only connects to public addresses. start() -> port; stop()."""

    def __init__(self, resolve=socket.getaddrinfo, allowed_ports=ALLOWED_PORTS, *,
                 connect=None, max_conns: int = MAX_CONNS,
                 connect_timeout: float = CONNECT_TIMEOUT_S,
                 lookup_timeout: float = LOOKUP_TIMEOUT_S,
                 idle_timeout: float = IDLE_TIMEOUT_S, head_max: int = HEAD_MAX,
                 head_timeout: float = HEAD_TIMEOUT_S, max_lookups: int = MAX_LOOKUPS):
        self._resolve = resolve
        self.allowed_ports = frozenset(allowed_ports)
        self._connect = connect or self._tracked_connect
        self.max_conns = max_conns
        self.connect_timeout = connect_timeout
        self.lookup_timeout = lookup_timeout
        self.idle_timeout = idle_timeout
        self.head_max = head_max
        self.head_timeout = head_timeout
        self.max_lookups = max_lookups
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener: socket.socket | None = None
        self._accepter: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._socks: set[socket.socket] = set()
        self._threads: set[threading.Thread] = set()
        self._lookups = _Count()
        self._started = False
        self.probe_host = secrets.token_hex(8) + PROBE_SUFFIX
        self._probed = threading.Event()
        self._refused = 0
        self.port: int | None = None

    # --- lifecycle -------------------------------------------------------------------------

    @property
    def probe_url(self) -> str:
        return f"http://{self.probe_host}/"

    def wait_probe(self, timeout: float) -> bool:
        """True once a request for probe_host has come through this proxy."""
        with self._lock:
            probed = self._probed
        return probed.wait(timeout)

    def new_probe(self) -> str:
        """A fresh random probe name (returned), not yet seen: the next wait_probe waits for a
        request for it. A late request for an older name is then just a name that can't be
        resolved."""
        with self._lock:
            self.probe_host = secrets.token_hex(8) + PROBE_SUFFIX
            self._probed = threading.Event()
            return self.probe_host

    def start(self) -> int:
        """Listen and return the port. A proxy starts once: after stop(), make a new one."""
        if self._started:
            if self._stopping.is_set():
                raise RuntimeError("this proxy was stopped; make a new one")
            return self.port
        if self._stopping.is_set():
            raise RuntimeError("this proxy was stopped; make a new one")
        self._started = True
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            ls.bind(("127.0.0.1", 0))
            ls.listen(128)
            ls.settimeout(_ACCEPT_POLL_S)
        except BaseException:
            ls.close()
            raise
        self._listener, self.port = ls, ls.getsockname()[1]
        self._accepter = threading.Thread(target=self._accept_loop, name="live-proxy",
                                          daemon=True)
        self._accepter.start()
        return self.port

    def stop(self) -> None:
        """Close the listener and every open connection; wait a bounded time for threads."""
        self._stopping.set()
        ls, self._listener = self._listener, None
        if ls is not None:
            try:
                ls.close()
            except OSError:
                pass
        with self._lock:
            socks = list(self._socks)
            threads = list(self._threads)
        for s in socks:
            self._kill(s)
        deadline = time.monotonic() + STOP_JOIN_S
        for t in [self._accepter, *threads]:
            if t is not None and t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))

    def stats(self) -> dict:
        with self._lock:
            return {"open": len(self._clients), "refused": self._refused}

    @staticmethod
    def _kill(s: socket.socket) -> None:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    # --- connections -----------------------------------------------------------------------

    def _accept_loop(self) -> None:
        ls = self._listener
        while not self._stopping.is_set() and ls is not None:
            try:
                c, _addr = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                time.sleep(0.05)
                continue
            with self._lock:
                full = len(self._clients) >= self.max_conns or self._stopping.is_set()
                if full:
                    self._refused += 1
                else:
                    self._clients.add(c)
                    self._socks.add(c)
            if full:
                self._answer(c, 503)
                self._kill(c)
                continue
            t = threading.Thread(target=self._handle, args=(c,), name="live-proxy-conn",
                                 daemon=True)
            with self._lock:
                self._threads.add(t)
            t.start()

    def _handle(self, c: socket.socket) -> None:
        up = None
        try:
            got = self._read_head(c)
            if got is None:
                return
            head, rest = got
            plan = _plan(head)
            with self._lock:
                probed = self._probed if plan.host == self.probe_host else None
            if probed is not None:
                probed.set()
                self._answer(c, 403)
                return
            if plan.port not in self.allowed_ports:
                raise _Refuse(403)
            targets = self._check(plan.host, plan.port)
            up = self._open(targets)
            with self._lock:
                self._socks.add(up)  # already there when _tracked_connect made it
                if self._stopping.is_set():
                    raise OSError("stopping")
            if plan.head is None:
                c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                first = rest
            else:
                first = plan.head + rest
            if first:
                up.settimeout(self.idle_timeout)
                up.sendall(first)
            self._splice(c, up, rewrite=plan.head is not None)
        except _Drop as e:
            log.debug("live proxy dropped a connection: %s", e)
        except _Refuse as r:
            if r.code != 502:
                with self._lock:
                    self._refused += 1
            self._answer(c, r.code)
        except Exception:  # resets, timeouts, a stop() closing the sockets
            log.debug("live proxy connection ended", exc_info=True)
        finally:
            for s in (c, up):
                if s is not None:
                    self._kill(s)
            with self._lock:
                self._clients.discard(c)
                self._socks.discard(c)
                if up is not None:
                    self._socks.discard(up)
                self._threads.discard(threading.current_thread())

    @staticmethod
    def _answer(c: socket.socket, code: int) -> None:
        reason = _REASONS.get(code, "Error")
        body = f"{code} {reason}\n".encode("ascii")
        try:
            c.settimeout(1.0)
            c.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                      .encode("ascii") + body)
        except OSError:
            pass

    def _read_head(self, c: socket.socket) -> tuple[bytes, bytes] | None:
        """(head, bytes after it), None to close quietly (EOF, or past the total deadline),
        or _Refuse(400) for a head over head_max."""
        deadline = time.monotonic() + self.head_timeout
        buf = b""
        while True:
            i = buf.find(b"\r\n\r\n")
            if i >= 0:
                if i + 4 > self.head_max:
                    raise _Refuse(400)
                return buf[:i], buf[i + 4:]
            if len(buf) > self.head_max:
                raise _Refuse(400)
            left = deadline - time.monotonic()
            if left <= 0 or self._stopping.is_set():
                return None
            c.settimeout(left)
            try:
                data = c.recv(8192)
            except socket.timeout:
                return None
            if not data:
                return None
            buf += data

    def _timed_resolve(self, host, port, *a, **k):
        """The injected resolver, on a thread of the proxy's own, given up after
        lookup_timeout seconds (or at once when the proxy stops); refused at once when
        max_lookups are already in flight. A lookup given up on is abandoned: its daemon
        thread holds only the resolver, a counter and its own result box."""
        count = self._lookups
        with count.lock:
            if count.n >= self.max_lookups:
                raise OSError("too many lookups in flight")
            count.n += 1
        box: dict = {}
        done = threading.Event()
        threading.Thread(target=_lookup, args=(self._resolve, count, box, done, (host, port, *a),
                                               k), name="live-proxy-dns", daemon=True).start()
        deadline = time.monotonic() + self.lookup_timeout
        while not done.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            if self._stopping.is_set():
                raise OSError("stopping")
            if time.monotonic() >= deadline:
                raise OSError(f"looking up {host} took too long")
        if "e" in box:
            e = box["e"]
            raise e if isinstance(e, (OSError, ValueError)) else OSError(str(e))
        return box["r"]

    def _check(self, host: str, port: int) -> list[tuple[int, tuple]]:
        """Resolve once and check every address; the (family, sockaddr) list to connect to,
        built from that same answer. _Refuse(403) for anything not plainly public."""
        if _local_name(host):
            raise _Refuse(403)
        infos: list = []

        def capture(h, p, *a, **k):
            got = self._timed_resolve(h, p, *a, **k)
            infos.extend(got)
            return got
        try:
            _check_host(host, capture)
        except WebError:
            raise _Refuse(403) from None
        except Exception:
            raise _Refuse(403) from None
        try:
            ips = [ipaddress.ip_address(host)] if not infos else [
                ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]
        except (ValueError, IndexError, TypeError):
            raise _Refuse(403) from None
        out: list[tuple[int, tuple]] = []
        for ip in ips:
            if not ip.is_global:  # _check_host said so already; kept as a second look
                raise _Refuse(403)
            t = ((socket.AF_INET6, (str(ip), port, 0, 0)) if ip.version == 6
                 else (socket.AF_INET, (str(ip), port)))
            if t not in out:
                out.append(t)
        if not out:
            raise _Refuse(403)
        return out

    def _tracked_connect(self, family: int, sockaddr: tuple, timeout: float) -> socket.socket:
        """Connect a socket that stop() can see (and so abort) while it is connecting."""
        s = _new_socket(family, socket.SOCK_STREAM)
        with self._lock:
            if self._stopping.is_set():
                s.close()
                raise OSError("stopping")
            self._socks.add(s)
        try:
            s.settimeout(timeout)
            s.connect(sockaddr)
        except BaseException:
            with self._lock:
                self._socks.discard(s)
            s.close()
            raise
        return s

    def _open(self, targets: list[tuple[int, tuple]]) -> socket.socket:
        """Connect to the first checked address that answers within connect_timeout."""
        deadline = time.monotonic() + self.connect_timeout
        for family, sockaddr in targets:
            left = deadline - time.monotonic()
            if left <= 0 or self._stopping.is_set():
                break
            try:
                return self._connect(family, sockaddr, left)
            except OSError:
                continue
        raise _Refuse(502)

    def _splice(self, c: socket.socket, up: socket.socket, rewrite: bool) -> None:
        """Copy bytes both ways until both sides end, or nothing moves for idle_timeout."""
        other = {c: up, up: c}
        reading = [c, up]
        held = b""
        last = time.monotonic()
        for s in reading:
            s.settimeout(self.idle_timeout)  # a peer that never reads can't hold sendall
        while reading and not self._stopping.is_set():
            left = self.idle_timeout - (time.monotonic() - last)
            if left <= 0:
                return
            r, _w, x = select.select(reading, [], reading, min(left, 1.0))
            if x:
                return
            for s in r:
                data = s.recv(_CHUNK)
                if not data:
                    reading.remove(s)
                    if s is up and held:
                        c.sendall(held)
                        held = b""
                    try:
                        other[s].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                last = time.monotonic()
                if s is up and rewrite:
                    data, held, rewrite = _close_heads(held + data)
                    if not data:
                        continue
                other[s].sendall(data)
