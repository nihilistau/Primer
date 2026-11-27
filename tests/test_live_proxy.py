"""primer.live.proxy: the guarding proxy, against a local upstream reached through an injected
connect hook while an injected resolver answers public addresses (no internet needed)."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from primer.live import proxy as proxy_mod
from primer.live.proxy import GuardProxy

PUBLIC = "93.184.215.14"
ADDRS = {"public.example": [PUBLIC], "other.example": ["93.184.215.15"],
         "local.example": ["127.0.0.1"], "mixed.example": [PUBLIC, "172.31.0.5"]}


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class Upstream:
    """A local server standing in for the public host. mode "echo" sends back what it gets;
    mode "http" records one request head and answers it with a keep-alive response."""

    def __init__(self, mode="echo"):
        self.mode = mode
        self.got: list[bytes] = []
        self.ls = socket.socket()
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(16)
        self.port = self.ls.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        buf = b""
        try:
            while True:
                data = c.recv(65536)
                if not data:
                    break
                if self.mode == "echo":
                    c.sendall(data)
                    continue
                buf += data
                if b"\r\n\r\n" in buf:
                    self.got.append(buf)
                    c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                              b"Connection: keep-alive\r\nKeep-Alive: timeout=5\r\n\r\nok")
                    break
        except OSError:
            pass
        finally:
            c.close()

    def close(self):
        self.ls.close()


class Net:
    """The injected resolver and connect hook: every connect goes to the local upstream, and
    the address the proxy asked for is recorded."""

    def __init__(self, upstream, addrs=ADDRS):
        self.upstream = upstream
        self.addrs = addrs
        self.lookups: list[str] = []
        self.connects: list[tuple] = []

    def resolve(self, host, port, *a, **k):
        self.lookups.append(host)
        if host not in self.addrs:
            raise socket.gaierror(f"no such host {host}")
        out = []
        for ip in self.addrs[host]:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return out

    def connect(self, family, sockaddr, timeout):
        self.connects.append((family, sockaddr))
        return socket.create_connection(("127.0.0.1", self.upstream.port), timeout)


@pytest.fixture
def rig():
    made = []

    def _rig(mode="echo", addrs=ADDRS, **kw):
        up = Upstream(mode)
        net = Net(up, addrs)
        kw.setdefault("resolve", net.resolve)
        p = GuardProxy(connect=net.connect, **kw)
        port = p.start()
        made.append((p, up))
        return p, port, net, up

    yield _rig
    for p, up in made:
        p.stop()
        up.close()


def dial(port, data=b"", timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout)
    s.settimeout(timeout)
    if data:
        s.sendall(data)
    return s


def read_head(s):
    buf = b""
    while b"\r\n\r\n" not in buf:
        data = s.recv(4096)
        if not data:
            break
        buf += data
    return buf


def read_all(s):
    buf = b""
    while True:
        try:
            data = s.recv(65536)
        except OSError:
            break
        if not data:
            break
        buf += data
    return buf


def status(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1]) if raw.startswith(b"HTTP/1.1 ") else 0


# --- the defaults ----------------------------------------------------------------------------

def test_the_limits_are_the_specified_ones():
    p = GuardProxy()
    assert p.allowed_ports == frozenset({80, 443, *range(8080, 8444)})
    assert (p.max_conns, p.connect_timeout, p.lookup_timeout, p.idle_timeout) == (
        256, 10.0, 3.0, 120.0)
    assert (p.head_max, p.head_timeout) == (16 * 1024, 10.0)


def test_it_listens_on_loopback_at_a_port_the_system_picks():
    p = GuardProxy()
    try:
        port = p.start()
        assert port > 0 and p._listener.getsockname() == ("127.0.0.1", port)
    finally:
        p.stop()


# --- tunnels ---------------------------------------------------------------------------------

def test_connect_to_a_public_address_is_tunnelled_both_ways(rig):
    p, port, net, _up = rig()
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n")
    head = read_head(s)
    assert status(head) == 200
    s.sendall(b"hello through")
    got = b""
    while len(got) < 13:
        got += s.recv(100)
    assert got == b"hello through"
    assert net.connects == [(socket.AF_INET, (PUBLIC, 443))]
    assert p.stats()["open"] == 1
    s.close()
    assert wait_for(lambda: p.stats()["open"] == 0)


def test_bytes_sent_with_the_connect_head_are_forwarded(rig):
    _p, port, _net, _up = rig()
    s = dial(port, b"CONNECT public.example:8443 HTTP/1.1\r\n\r\nearly")
    raw = b""
    while not raw.endswith(b"early"):
        raw += s.recv(100)
    assert raw == b"HTTP/1.1 200 Connection Established\r\n\r\nearly"
    s.close()


def test_a_public_ipv6_literal_is_connected_by_address(rig):
    _p, port, net, _up = rig()
    s = dial(port, b"CONNECT [2606:4700::1111]:443 HTTP/1.1\r\n\r\n")
    assert status(read_head(s)) == 200
    assert net.connects == [(socket.AF_INET6, ("2606:4700::1111", 443, 0, 0))]
    assert net.lookups == []  # a literal is never looked up
    s.close()


@pytest.mark.parametrize("target", [
    "127.0.0.1:443", "[::1]:443", "[::ffff:127.0.0.1]:443", "172.31.0.5:443",
    "169.254.169.254:80", "localhost:443", "x.localhost:443", "router:443",
    "public.example:22", "local.example:443", "mixed.example:443", "[64:ff9b::7f00:1]:443",
    "2130706433:443", "printer.local:443", "nowhere.example:443",
])
def test_these_are_refused_with_403(rig, target):
    p, port, net, _up = rig()
    s = dial(port, f"CONNECT {target} HTTP/1.1\r\nHost: public.example\r\n\r\n".encode())
    raw = read_all(s)
    assert status(raw) == 403 and b"Connection: close" in raw
    assert net.connects == []
    assert p.stats()["refused"] == 1
    if target.split(":")[0] in ("localhost", "x.localhost", "router", "printer.local") \
            or target.endswith(":22"):
        assert net.lookups == []  # refused before any lookup


@pytest.mark.parametrize("head", [
    b"CONNECT public.example HTTP/1.1\r\n\r\n",  # no port
    b"CONNECT user@public.example:443 HTTP/1.1\r\n\r\n",
    b"CONNECT [fe80::1%25eth0]:443 HTTP/1.1\r\n\r\n",
    b"CONNECT [fe80::1%eth0]:443 HTTP/1.1\r\n\r\n",
    b"CONNECT public.example:0 HTTP/1.1\r\n\r\n",
    b"CONNECT public.example:99999 HTTP/1.1\r\n\r\n",
    b"CONNECT public.example:443/x HTTP/1.1\r\n\r\n",
    b"CONNECT http://public.example:443 HTTP/1.1\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: public.example\r\n\r\n",  # origin-form
    b"GET https://public.example/ HTTP/1.1\r\n\r\n",
    b"GET http://user:pw@public.example/ HTTP/1.1\r\n\r\n",
    b"GET http://public.example/ HTTP/2.0\r\n\r\n",
    b"GET  http://public.example/ HTTP/1.1\r\n\r\n",
    b"GET http://public.example/ HTTP/1.1\r\nX-A: 1\r\n folded\r\n\r\n",
    b"GET http://public.example/ HTTP/1.1\r\nBad Header: 1\r\n\r\n",
    b"GET http://public.example/ HTTP/1.1\r\nX: a\nb\r\n\r\n",
    b"\r\n\r\n",
])
def test_odd_requests_are_refused_with_400(rig, head):
    p, port, net, _up = rig()
    raw = read_all(dial(port, head))
    assert status(raw) == 400
    assert net.connects == [] and net.lookups == []
    assert p.stats()["refused"] == 1


def test_rebinding_connects_to_the_checked_address_only(rig):
    answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))],
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]])
    calls = []

    def flip(host, port, *a, **k):
        calls.append(host)
        return next(answers)
    _p, port, net, _up = rig(resolve=flip)
    s = dial(port, b"CONNECT rebind.example:443 HTTP/1.1\r\n\r\n")
    assert status(read_head(s)) == 200
    assert calls == ["rebind.example"]  # resolved once
    assert net.connects == [(socket.AF_INET, (PUBLIC, 443))]
    s.close()


def test_a_slow_lookup_is_refused_after_the_lookup_timeout(rig):
    gate = threading.Event()

    def slow(host, port, *a, **k):
        gate.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]
    p, port, net, _up = rig(resolve=slow, lookup_timeout=0.3)
    t0 = time.monotonic()
    raw = read_all(dial(port, b"CONNECT slow.example:443 HTTP/1.1\r\n\r\n"))
    gate.set()
    assert status(raw) == 403 and time.monotonic() - t0 < 3
    assert net.connects == []


def test_an_unreachable_host_gets_502(rig):
    p, port, net, _up = rig()

    def refuse(family, sockaddr, timeout):
        net.connects.append(sockaddr)
        raise ConnectionRefusedError("no")
    p._connect = refuse
    raw = read_all(dial(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n"))
    assert status(raw) == 502
    assert net.connects == [(PUBLIC, 443)]


# --- absolute-form http ----------------------------------------------------------------------

def test_absolute_form_is_forwarded_with_the_urls_host(rig):
    _p, port, net, up = rig("http")
    s = dial(port, b"GET http://public.example/a?b=1#frag HTTP/1.1\r\n"
                   b"Host: 127.0.0.1\r\nProxy-Connection: keep-alive\r\n"
                   b"Proxy-Authorization: Basic eA==\r\nAccept: */*\r\n\r\n")
    raw = read_all(s)
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n") and raw.endswith(b"ok")
    assert b"Connection: close\r\n" in raw and b"keep-alive" not in raw.lower()
    assert net.connects == [(socket.AF_INET, (PUBLIC, 80))]
    head = up.got[0].decode("latin-1")
    lines = head.split("\r\n")
    assert lines[0] == "GET /a?b=1 HTTP/1.1"
    assert lines[1] == "Host: public.example"
    assert "Host: 127.0.0.1" not in head
    assert "Proxy-" not in head and "Accept: */*" in lines
    assert "Connection: close" in lines


def test_absolute_form_keeps_an_explicit_port_and_a_websocket_upgrade(rig):
    _p, port, net, up = rig("http")
    s = dial(port, b"GET http://public.example:8080 HTTP/1.1\r\nHost: other.example\r\n"
                   b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
    read_all(s)
    assert net.connects == [(socket.AF_INET, (PUBLIC, 8080))]
    lines = up.got[0].decode("latin-1").split("\r\n")
    assert lines[:2] == ["GET / HTTP/1.1", "Host: public.example:8080"]
    assert "Connection: Upgrade" in lines and "Upgrade: websocket" in lines
    assert "Connection: close" not in lines


def test_absolute_form_to_a_local_host_is_refused(rig):
    _p, port, net, up = rig("http")
    raw = read_all(dial(port, b"GET http://127.0.0.1:8080/ HTTP/1.1\r\n"
                              b"Host: public.example\r\n\r\n"))
    assert status(raw) == 403 and net.connects == [] and up.got == []


def test_close_heads_rewrites_only_the_final_response_head():
    out, held, more = proxy_mod._close_heads(b"HTTP/1.1 200 OK\r\nConn")
    assert (out, more) == (b"", True) and held
    out, held, more = proxy_mod._close_heads(
        b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 204 No\r\nconnection: keep-alive\r\n\r\nbody")
    assert out == (b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 204 No\r\nConnection: close"
                   b"\r\n\r\nbody")
    assert (held, more) == (b"", False)
    ws = b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n\r\n\x81\x00"
    assert proxy_mod._close_heads(ws) == (ws, b"", False)


# --- limits ----------------------------------------------------------------------------------

def test_a_head_over_16_kb_gets_400(rig):
    p, port, net, _up = rig()
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\nX-Pad: " + b"a" * 17000)
    raw = read_all(s)
    assert status(raw) == 400 and net.connects == []


def test_a_complete_head_over_16_kb_gets_400(rig):
    _p, port, net, _up = rig()
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\nX-Pad: " + b"a" * 16400
             + b"\r\n\r\n")
    assert status(read_all(s)) == 400 and net.connects == []


def test_a_head_that_trickles_past_the_deadline_is_closed_without_an_answer(rig):
    """The deadline closes the connection quietly (no 400), as RUNNING says."""
    p, port, net, _up = rig(head_timeout=1.0)
    s = dial(port)
    t0 = time.monotonic()
    for ch in b"CONNECT public.ex":  # trickles, then stalls before the deadline, so the proxy
        s.sendall(bytes([ch]))  # has read every byte and an answer could not be lost to a reset
        time.sleep(0.03)
    s.settimeout(3)
    got = read_all(s)  # up to the close: nothing was ever answered
    assert got == b"" and time.monotonic() - t0 < 4.5
    assert net.connects == []


def test_the_257th_connection_gets_503(rig):
    p, port, _net, _up = rig()
    held = [dial(port) for _ in range(256)]
    try:
        assert wait_for(lambda: p.stats()["open"] == 256, 10)
        raw = read_all(dial(port))
        assert status(raw) == 503
        assert p.stats() == {"open": 256, "refused": 1}
    finally:
        for s in held:
            s.close()
    assert wait_for(lambda: p.stats()["open"] == 0, 15)


def test_an_idle_tunnel_is_closed(rig):
    p, port, _net, _up = rig(idle_timeout=0.5)
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
    assert status(read_head(s)) == 200
    t0 = time.monotonic()
    assert s.recv(10) == b"" and time.monotonic() - t0 < 3
    assert wait_for(lambda: p.stats()["open"] == 0)


def test_stop_closes_everything(rig):
    p, port, _net, _up = rig()
    tunnel = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
    assert status(read_head(tunnel)) == 200
    waiting = dial(port, b"CONNECT public.exa")  # a head still coming in
    assert wait_for(lambda: p.stats()["open"] == 2)
    t0 = time.monotonic()
    p.stop()
    assert time.monotonic() - t0 < 4
    for s in (tunnel, waiting):
        s.settimeout(3)
        try:
            assert s.recv(10) == b""
        except OSError:
            pass  # reset: closed all the same
    assert p.stats()["open"] == 0
    try:
        late = socket.create_connection(("127.0.0.1", port), timeout=5)
    except OSError:
        pass
    else:
        late.close()
        pytest.fail("the proxy still listens after stop()")


# --- fix round 1 -----------------------------------------------------------------------------

def test_the_probe_is_recorded_and_answered_without_a_lookup(rig):
    p, port, net, _up = rig()
    assert p.probe_host.endswith(".primer-proxy-probe.invalid")
    assert p.probe_url == f"http://{p.probe_host}/"
    assert not p.wait_probe(0)
    raw = read_all(dial(port, f"GET {p.probe_url} HTTP/1.1\r\n\r\n".encode()))
    assert status(raw) == 403 and p.wait_probe(0)
    assert net.lookups == [] and net.connects == []
    assert p.stats()["refused"] == 0  # the probe isn't a refusal


def test_the_probe_also_counts_through_connect_and_another_proxys_does_not(rig):
    p, port, net, _up = rig()
    other = GuardProxy()
    assert other.probe_host != p.probe_host
    read_all(dial(port, f"CONNECT {other.probe_host}:443 HTTP/1.1\r\n\r\n".encode()))
    assert not p.wait_probe(0)  # a stranger's probe name: an ordinary (unresolvable) host
    read_all(dial(port, f"CONNECT {p.probe_host}:443 HTTP/1.1\r\n\r\n".encode()))
    assert p.wait_probe(0)


def test_a_new_probe_name_must_be_seen_again(rig):
    """The live service probes again while the browser runs: each probe gets a fresh name, and
    the old name's arrival no longer counts."""
    p, port, net, _up = rig()
    first = p.probe_host
    read_all(dial(port, f"GET http://{first}/ HTTP/1.1\r\n\r\n".encode()))
    assert p.wait_probe(0)
    second = p.new_probe()
    assert second == p.probe_host != first and second.endswith(".primer-proxy-probe.invalid")
    assert not p.wait_probe(0)
    read_all(dial(port, f"GET http://{first}/ HTTP/1.1\r\n\r\n".encode()))
    assert not p.wait_probe(0)  # the old name is an ordinary (unresolvable) host now
    raw = read_all(dial(port, f"GET {p.probe_url} HTTP/1.1\r\n\r\n".encode()))
    assert status(raw) == 403 and p.wait_probe(0) and net.connects == []


@pytest.mark.parametrize("reply", [
    b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nX-Pad: " + b"a" * (70 * 1024),
    b"ICY 200 OK\r\nConnection: keep-alive\r\n\r\nstream",
], ids=["over-the-cap", "not-http"])
def test_a_response_head_that_cant_be_rewritten_drops_the_connection(rig, reply):
    p, port, net, up = rig()

    class Odd(Upstream):
        def _one(self, c):
            try:
                c.recv(65536)
                c.sendall(reply)
                time.sleep(2)
            except OSError:
                pass
            finally:
                c.close()
    odd = Odd()
    net.upstream = odd
    try:
        s = dial(port, b"GET http://public.example/ HTTP/1.1\r\n\r\n")
        t0 = time.monotonic()
        raw = read_all(s)
        assert raw == b"" and time.monotonic() - t0 < 1.5  # nothing passed on, closed at once
    finally:
        odd.close()


def test_close_heads_refuses_what_it_cant_rewrite():
    with pytest.raises(proxy_mod._Drop):
        proxy_mod._close_heads(b"x" * (proxy_mod.RESPONSE_HEAD_MAX + 1))
    with pytest.raises(proxy_mod._Drop):
        proxy_mod._close_heads(b"HTTP/2 200\r\n\r\n")


def test_stop_aborts_a_connect_in_progress(monkeypatch):
    made = []

    class Stuck(socket.socket):
        def connect(self, addr):
            made.append(self)
            while self.fileno() != -1:  # until stop() closes it
                time.sleep(0.02)
            raise OSError("closed")
    monkeypatch.setattr(proxy_mod, "_new_socket", Stuck)
    p = GuardProxy(resolve=Net(None).resolve)  # the default, tracked connect
    port = p.start()
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
    assert wait_for(lambda: made)
    t0 = time.monotonic()
    p.stop()
    assert time.monotonic() - t0 < 1.5
    assert made[0].fileno() == -1
    assert wait_for(lambda: not p._threads, 2)
    s.close()


def test_stop_does_not_wait_for_a_hung_lookup_and_the_lookup_holds_nothing():
    import gc
    import weakref
    gate = threading.Event()

    def hung(host, port, *a, **k):
        gate.wait(30)
        raise socket.gaierror("late")
    p = GuardProxy(resolve=hung)
    port = p.start()
    s = dial(port, b"CONNECT public.example:443 HTTP/1.1\r\n\r\n")
    count, threads = p._lookups, p._threads
    assert wait_for(lambda: count.n == 1)
    t0 = time.monotonic()
    p.stop()
    assert time.monotonic() - t0 < 1.5
    assert wait_for(lambda: not threads, 2)
    lookups = [t for t in threading.enumerate() if t.name == "live-proxy-dns"]
    assert lookups and all(t.daemon for t in lookups)
    ref = weakref.ref(p)
    del p
    gc.collect()
    assert ref() is None  # the abandoned lookup keeps nothing of the proxy alive
    gate.set()
    s.close()


def test_start_after_stop_raises():
    p = GuardProxy()
    port = p.start()
    assert p.start() == port  # starting twice is the same proxy
    p.stop()
    with pytest.raises(RuntimeError):
        p.start()
    q = GuardProxy()
    q.stop()
    with pytest.raises(RuntimeError):
        q.start()
