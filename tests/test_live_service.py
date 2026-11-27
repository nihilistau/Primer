"""primer.live: finding the browser, the request guard, and the live service against a fake
DevTools connection (plus one opt-in run against the real installed browser)."""
from __future__ import annotations

import base64
import http.server
import itertools
import json
import os
import socket
import threading
import time
import urllib.parse
from types import SimpleNamespace as NS

import pytest

from primer import children
from primer.live import guard
from primer.live.browser import Browser, find_browser
from primer.live.cdp import CdpError
from primer.live.service import (BAD_ADDRESS, COVERED, HREF_JS, NO_PROXY, NO_SOCKETS_JS,
                                 STATE_JS, LiveError, LiveService, _jpeg_size)

# --- fakes -------------------------------------------------------------------------------------

ADDRS = {"public.example": "93.184.215.14", "local.example": "127.0.0.1",
         "meta.example": "169.254.169.254", "lan.example": "172.31.0.5"}


def fake_resolve(host, port, *a, **k):
    if host in ADDRS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ADDRS[host], 0))]
    raise socket.gaierror(f"no such host {host}")


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeCdp:
    """Records calls; handlers answer them; fire() delivers events like the dispatcher."""

    def __init__(self):
        self.proxy_port = None  # where the probe goes; None: as if a policy bypassed the proxy
        self.calls = []
        self.listeners = {}
        self.closed = threading.Event()
        self.handlers = {}
        self._lock = threading.Lock()
        self._n = itertools.count(1)
        self.handlers["Target.createTarget"] = self._create
        self.handlers["Page.getFrameTree"] = lambda p, s: {"frameTree": {"frame": {"id": "F" + s}}}
        self.handlers["Page.createIsolatedWorld"] = lambda p, s: {"executionContextId": 7}
        self.snapshot = {"title": "T", "url": "https://public.example/", "text": "hi",
                         "controls": []}
        self.found = {}
        self.inserted = []
        self.insert_ok = True
        self.texts = []  # the caps asked of the "text" op
        self.aims = []  # replies for the "aim" op, in turn; after them, found with clear: True
        self.handlers["Runtime.evaluate"] = self._evaluate
        self.handlers["Target.attachToTarget"] = self._attach_probe
        self.handlers["Page.navigate"] = self._navigate
        self.probe_targets = set()  # tabs the service attached to itself: its probe tabs
        self.probe_held = []  # browser-wide pauses of a re-probe's request, as (id, url)

    def _attach_probe(self, params, session):
        """Only the proxy probe attaches to a tab itself: "PS" at start-up, "PS-<tab>" after."""
        tid = params.get("targetId")
        self.probe_targets.add(tid)
        return {"sessionId": "PS" if tid == "PROBE" else f"PS-{tid}"}

    probe_pauses = False  # True: the probe tab's Fetch sees PROBE_PAUSES as it navigates
    PROBE_PAUSES = (  # what the probe tab's Fetch sees, in turn, as a bypassed page would load
        ("probe", {}), ("probe-https", {}), ("probe", {"responseStatusCode": 200}),
        ("http://127.0.0.1:8810/x.png", {}), ("ws://172.31.0.5/", {}))

    def _navigate(self, params, session):
        url = params.get("url", "")
        if not str(session).startswith("PS") or ".primer-proxy-probe.invalid" not in url:
            return None
        if session.startswith("PS-T"):  # a re-probe's tab, made paused by auto-attach
            auto = "S" + session[len("PS-T"):]
            if ("Runtime.runIfWaitingForDebugger", {}, auto) not in self.calls:
                return {"frameId": "PF", "errorText": "net::ERR_TIMED_OUT"}  # never ran
        for i, (what, extra) in enumerate(self.PROBE_PAUSES if self.probe_pauses else ()):
            u = (url if what == "probe" else url.replace("http://", "https://")
                 if what == "probe-https" else what)
            self.fire("Fetch.requestPaused", {"requestId": f"P{i}", "request": {"url": u},
                                              **extra}, session)
        with self._lock:
            guarded = any(m == "Fetch.enable" and s is None for m, _p, s in self.calls)
        if guarded:
            # the browser-wide guard is on (a re-probe): the request pauses there too, and
            # goes on only if the service continues it
            rid = f"BP{len(self.probe_held)}"
            self.probe_held.append((rid, url))
            self.fire("Fetch.requestPaused", {"requestId": rid, "request": {"url": url}}, None)
            if not wait_for(lambda: {"requestId": rid} in self.named("Fetch.continueRequest")
                            or any(p.get("requestId") == rid
                                   for p in self.named("Fetch.failRequest")), 3):
                return {"frameId": "PF", "errorText": "net::ERR_TIMED_OUT"}
            if {"requestId": rid} not in self.named("Fetch.continueRequest"):
                return {"frameId": "PF", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}
        if self.proxy_port:  # the request Chrome sends through the proxy
            try:
                with socket.create_connection(("127.0.0.1", self.proxy_port), 3) as c:
                    c.sendall(f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                    c.recv(100)
            except OSError:
                pass  # the proxy is gone: the probe is simply never seen
        return {"frameId": "PF", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}

    def _create(self, params, session):
        if not self.listeners.get("Target.attachedToTarget"):
            return {"targetId": "PROBE"}  # before the service wires up: the start-up probe tab
        i = next(self._n)
        tid, sid = f"T{i}", f"S{i}"
        # the attach comes before createTarget's reply, as it can in Chrome
        self.fire("Target.attachedToTarget", {
            "sessionId": sid, "waitingForDebugger": True,
            "targetInfo": {"type": "page", "targetId": tid, "url": "about:blank"}})
        return {"targetId": tid}

    def _evaluate(self, params, session):
        expr = params.get("expression", "")
        if "contextId" not in params or expr in (HREF_JS, STATE_JS):
            return {"result": {"type": "undefined"}}
        op = json.loads("[" + expr.rsplit(")(", 1)[1].rstrip(")") + "]")
        if op[0] == "read":
            return {"result": {"value": json.dumps(self.snapshot)}}
        if op[0] == "text":
            self.texts.append(op[3])
            snap = self.snapshot
            return {"result": {"value": json.dumps({"title": snap["title"], "url": snap["url"],
                                                    "text": snap["text"]})}}
        if op[0] == "find":
            return {"result": {"value": json.dumps(self.found.get(op[2], {"ok": False}))}}
        if op[0] == "aim":
            got = self.aims.pop(0) if self.aims else {}
            out = {**self.found.get(op[2], {"ok": False}), "clear": True, **got}
            return {"result": {"value": json.dumps(out)}}
        if op[0] == "insert":
            if self.insert_ok:
                self.inserted.append((op[2], op[3]))
            return {"result": {"value": json.dumps({"ok": self.insert_ok})}}
        return {"result": {"value": json.dumps({"ok": True})}}

    def call(self, method, params=None, session=None, timeout=15):
        if self.closed.is_set():
            raise CdpError("browser closed")
        with self._lock:
            self.calls.append((method, params or {}, session))
        h = self.handlers.get(method)
        return (h(params or {}, session) if h else None) or {}

    def start(self, method, params=None, session=None):
        try:
            return ("ok", self.call(method, params, session))
        except CdpError as e:
            return ("err", e)

    def finish(self, pending, timeout=15):
        kind, val = pending
        if kind == "err":
            raise val
        return val

    def send(self, method, params=None, session=None):
        if self.closed.is_set():
            raise CdpError("browser closed")
        with self._lock:
            self.calls.append((method, params or {}, session))

    def on(self, event, fn):
        self.listeners.setdefault(event, []).append(fn)

    def off(self, event, fn):
        self.listeners.get(event, []).remove(fn)

    def fire(self, event, params, session=None):
        for fn in list(self.listeners.get(event, [])):
            fn(params, session)

    def close(self):
        self.closed.set()

    def named(self, method, session=None):
        with self._lock:
            return [p for m, p, s in self.calls
                    if m == method and (session is None or s == session)]

    def methods(self, session):
        with self._lock:
            return [m for m, _p, s in self.calls if s == session]


class FakeBrowser:
    made: list = []
    proxied = True  # False: the browser ignores --proxy-server (a proxy policy wins)

    def __init__(self, path, profile_dir):
        self.path, self.profile_dir = path, profile_dir
        self.cdp = FakeCdp()
        self.stopped = False
        self.profile_at_stop = None
        FakeBrowser.made.append(self)

    def start(self, proxy_port):
        self.proxy_port = proxy_port
        if self.proxied and proxy_port not in {p.port for p in FakeProxy.made}:
            self.cdp.proxy_port = proxy_port  # a real GuardProxy: send it the probe
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        (self.profile_dir / "Cookies").write_text("x")
        return self.cdp

    def stop(self):
        self.profile_at_stop = self.profile_dir.exists()
        self.stopped = True
        self.cdp.close()

    def alive(self):
        return not self.stopped


def tier(search="duckduckgo", path=""):
    return NS(web=NS(search=search), browser=NS(path=path))


@pytest.fixture
def make(tmp_path):
    FakeBrowser.made = []
    made = []

    def _make(t=None, **kw):
        kw.setdefault("find", lambda p: ("C:/Chrome/chrome.exe", "Chrome 158"))
        kw.setdefault("resolve", fake_resolve)
        svc = LiveService(tmp_path / "var", t or tier(), launcher=FakeBrowser, **kw)
        made.append(svc)
        return svc

    yield _make
    for s in made:
        s.shutdown()


def cdp_of(svc):
    return FakeBrowser.made[-1].cdp


def closed_but_probe(cdp):
    return [p for p in cdp.named("Target.closeTarget")
            if p.get("targetId") not in cdp.probe_targets]


def frame_event(i, w=800, h=600):
    return {"data": base64.b64encode(f"frame{i}".encode()).decode(), "sessionId": i,
            "metadata": {"deviceWidth": w, "deviceHeight": h}}


# --- find_browser ------------------------------------------------------------------------------

WIN_ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"E:\Profiles\Ann\AppData\Local"}


def _find(files, cfg="", platform="win32", env=WIN_ENV, which=lambda n: None):
    return find_browser(cfg, env=env, platform=platform, isfile=lambda p: p in files,
                        which=which, version=lambda p: "158.0.7000.12")


def test_find_browser_prefers_chrome_then_edge():
    chrome_local = os.path.join(WIN_ENV["LOCALAPPDATA"], "Google", "Chrome", "Application",
                                "chrome.exe")
    edge = os.path.join(WIN_ENV["ProgramFiles(x86)"], "Microsoft", "Edge", "Application",
                        "msedge.exe")
    assert _find({chrome_local, edge}) == (chrome_local, "Chrome 158")
    assert _find({edge}) == (edge, "Edge 158")
    assert _find(set()) is None


def test_find_browser_configured_path_overrides():
    assert _find({r"D:\Tools\chrome.exe"}, cfg=r"D:\Tools\chrome.exe") == (
        r"D:\Tools\chrome.exe", "Chrome 158")
    assert _find({r"C:\Program Files\Google\Chrome\Application\chrome.exe"},
                 cfg=r"D:\missing.exe") is None  # a set path that isn't there: no guessing


def test_find_browser_mac_and_linux():
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert _find({mac}, platform="darwin", env={"HOME": "/Users/ann"}) == (mac, "Chrome 158")
    which = {"microsoft-edge": "/usr/bin/microsoft-edge"}.get
    assert _find({"/usr/bin/microsoft-edge"}, platform="linux", env={}, which=which) == (
        "/usr/bin/microsoft-edge", "Edge 158")


def test_find_browser_label_without_version():
    p = r"C:\x\chrome.exe"
    assert find_browser(p, isfile=lambda q: True, version=lambda q: "") == (p, "Chrome")


def test_browser_command_has_the_required_flags_only(tmp_path):
    cmd = Browser("chrome", tmp_path / "browser").command(4321)
    assert cmd[1:] == ["--headless=new", "--remote-debugging-port=0",
                       f"--user-data-dir={tmp_path / 'browser'}", "--no-first-run",
                       "--no-default-browser-check",
                       "--disable-features=DocumentPictureInPictureAPI",
                       "--proxy-server=http://127.0.0.1:4321",
                       "--proxy-bypass-list=<-loopback>", "--disable-quic"]
    assert not any("remote-allow-origins" in a for a in cmd)


def test_browser_start_needs_the_proxy_port(tmp_path):
    from primer.live.browser import BrowserError
    b = Browser("chrome", tmp_path / "b", popen=lambda c, **k: 1 / 0)
    for bad in (None, 0, 70000, "4321"):
        with pytest.raises(BrowserError, match="proxy"):
            b.start(bad)


class FakeProcess:
    def __init__(self, pid, exe, created, cmd=""):
        self.pid, self.exe, self.created, self.cmd = pid, exe, created, cmd
        self.alive = True


class FakeHandle:
    """Like a real handle: it stays on the process it was opened on, even if the pid is reused."""

    def __init__(self, table, proc):
        self.table, self.proc, self.pid = table, proc, proc.pid
        self.closed = False

    def identity(self):
        return (self.proc.exe, str(self.proc.created)) if self.proc.alive else None

    def command_line(self):
        return self.proc.cmd

    def wait(self, timeout):
        return not self.proc.alive

    def terminate(self, ident, graceful=False):
        if self.identity() != ident:
            return False
        self.proc.alive = False
        self.table.killed.append(self.proc.pid)
        return True

    def assign_to(self, job):
        return False

    def close(self):
        self.closed = True


class FakeTable:
    """A fake process table, reachable only through handles (OpenProcess)."""

    def __init__(self):
        self.procs, self.killed, self.handles = {}, [], []

    def add(self, pid, exe, created, cmd=""):
        self.procs[pid] = FakeProcess(pid, exe, created, cmd)
        return self.procs[pid]

    def open(self, pid):
        p = self.procs.get(pid)
        if p is None or not p.alive:
            return None
        h = FakeHandle(self, p)
        self.handles.append(h)
        return h


CHROME = "C:/Chrome/chrome.exe"
EXE = os.path.normcase(os.path.abspath(CHROME))
NOW = 10 ** 12


class Launcher:
    """The Popen'd chrome.exe: it writes the port file and exits 0 (it relaunched itself)."""
    pid = 100

    def __init__(self, prof):
        self.prof, self.returncode, self.killed = prof, 0, False

    def poll(self):
        return 0

    def wait(self, t=None):
        return 0

    def kill(self):
        self.killed = True


def _browser(tmp_path, monkeypatch, table, *, browser_exits_on_close=True):
    prof = tmp_path / "browser"
    adopted = []
    monkeypatch.setattr(children, "adopt", lambda *a, **k: None)
    monkeypatch.setattr(children, "release", lambda *a, **k: None)
    monkeypatch.setattr(children, "adopt_handle", lambda h, ident, role: adopted.append((h.pid, ident)))
    cdp = FakeCdp()
    cdp.handlers["SystemInfo.getProcessInfo"] = lambda p, s: {"processInfo": [
        {"type": "renderer", "id": 300}, {"type": "browser", "id": 200}]}

    def close(p, s):
        if browser_exits_on_close and 200 in table.procs:
            table.procs[200].alive = False
    cdp.handlers["Browser.close"] = close
    monkeypatch.setattr("primer.live.browser.Cdp", lambda ws: cdp)
    seen = {}

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        assert not (prof / "DevToolsActivePort").exists()  # the stale file went first
        (prof / "DevToolsActivePort").write_text("45678\n/devtools/browser/ab-12\n")
        seen["launcher"] = Launcher(prof)
        return seen["launcher"]

    def connect(url, timeout):
        seen["url"] = url
        return object()

    b = Browser(CHROME, prof, popen=popen, connect=connect, timeout=2,
                open_process=table.open, now_stamp=lambda: NOW)
    return b, cdp, seen, adopted, prof


def _good_cmd(prof):
    return f'"{CHROME}" --headless=new --user-data-dir="{prof}" --no-first-run'


def test_browser_start_reads_the_port_file_and_verifies_the_browser(tmp_path, monkeypatch):
    table = FakeTable()
    (tmp_path / "browser").mkdir()
    (tmp_path / "browser" / "DevToolsActivePort").write_text("1\n/devtools/browser/stale\n")
    table.add(200, EXE, NOW + 5, _good_cmd(tmp_path / "browser"))
    b, cdp, seen, adopted, prof = _browser(tmp_path, monkeypatch, table)
    b.start(4321)
    assert seen["url"] == "ws://127.0.0.1:45678/devtools/browser/ab-12"
    assert b.pid == 200 and adopted == [(200, (EXE, str(NOW + 5)))]
    prefs = json.loads((prof / "Default" / "Preferences").read_text())
    assert prefs["net"]["network_prediction_options"] == 2  # no preconnect, no DNS prefetch
    b.stop()
    assert table.killed == []  # it closed when asked: nothing killed
    assert all(h.closed for h in table.handles)


@pytest.mark.parametrize("change", ["exe", "older", "no-profile", "other-profile", "closed"])
def test_a_pid_that_is_not_verifiably_ours_is_never_adopted_or_killed(tmp_path, monkeypatch, change):
    table = FakeTable()
    prof = tmp_path / "browser"
    exe, created, cmd = EXE, NOW + 5, _good_cmd(prof)
    if change == "exe":
        exe = os.path.normcase("C:/Other/chrome.exe")
    elif change == "older":  # the user's Chrome, started before ours, with a reused pid
        created = NOW - 10 ** 9
    elif change == "no-profile":
        cmd = f'"{CHROME}" --headless=new'
    elif change == "other-profile":
        cmd = f'"{CHROME}" --user-data-dir="{prof}2"'
    table.add(200, exe, created, cmd)
    b, cdp, seen, adopted, _ = _browser(tmp_path, monkeypatch, table, browser_exits_on_close=False)
    if change == "closed":
        orig = cdp.handlers["SystemInfo.getProcessInfo"]

        def info_then_close(p, s):
            out = orig(p, s)
            cdp.closed.set()
            return out
        cdp.handlers["SystemInfo.getProcessInfo"] = info_then_close
    b.start(4321)
    assert b.pid is None and adopted == []
    assert all(h.closed for h in table.handles)
    b.stop()
    assert table.killed == []
    assert table.procs[200].alive


def test_stop_kills_only_the_verified_browser_through_its_handle(tmp_path, monkeypatch):
    table = FakeTable()
    prof = tmp_path / "browser"
    table.add(200, EXE, NOW + 5, _good_cmd(prof))
    # an older orphan whose parent pid happens to be 200: a tree kill would take it
    table.add(400, os.path.normcase("C:/Users/x/chrome.exe"), NOW - 10 ** 9, "user chrome")
    b, *_ = _browser(tmp_path, monkeypatch, table, browser_exits_on_close=False)
    b.start(4321)
    b.stop()
    assert table.killed == [200]
    assert table.procs[400].alive


def test_stop_never_kills_a_process_whose_creation_time_differs(tmp_path, monkeypatch):
    table = FakeTable()
    table.add(200, EXE, NOW + 5, _good_cmd(tmp_path / "browser"))
    b, *_ = _browser(tmp_path, monkeypatch, table, browser_exits_on_close=False)
    b.start(4321)
    table.procs[200].created = NOW + 999  # no longer the process we verified
    b.stop()
    assert table.killed == []


def test_browser_start_fails_when_the_process_dies(tmp_path, monkeypatch):
    class Proc:
        pid, returncode = 4243, 3

        def poll(self):
            return 3

        def wait(self, t=None):
            return 3

    monkeypatch.setattr(children, "adopt", lambda *a, **k: None)
    monkeypatch.setattr(children, "release", lambda *a, **k: None)
    b = Browser("chrome", tmp_path / "b", popen=lambda c, **k: Proc(), timeout=2,
                open_process=FakeTable().open)
    with pytest.raises(Exception, match="exited"):
        b.start(4321)


def _ledger_entry(pid, exe, created, role="browser"):
    return {"role": role, "pid": pid, "exe": exe, "create_time": created, "port": None,
            "owner": {"pid": 2 ** 31 - 2, "exe": "gone.exe", "create_time": "1"}}


def test_reap_kills_only_the_recorded_process_checked_on_its_handle(tmp_path, monkeypatch):
    table = FakeTable()
    table.add(500, EXE, 222)  # pid 500 now belongs to a different (later) process
    table.add(600, EXE, 333)
    monkeypatch.setattr(children, "open_process", table.open)
    (tmp_path / "children.json").write_text(json.dumps({"children": [
        _ledger_entry(500, EXE, "111"), _ledger_entry(600, EXE, "333")]}))
    assert children.reap_leftovers(tmp_path) == [
        "stopped a leftover browser from the last run (pid 600)"]
    assert table.killed == [600]
    assert table.procs[500].alive


def test_adopt_records_the_identity_read_through_the_handle(tmp_path, monkeypatch):
    table = FakeTable()
    table.add(700, EXE, 444, "cmd")
    h = table.open(700)
    monkeypatch.setattr(children, "_ledger", children.Ledger(tmp_path))
    children.adopt_handle(h, (EXE, "444"), "browser")
    entry = json.loads((tmp_path / "children.json").read_text())["children"][0]
    assert (entry["pid"], entry["exe"], entry["create_time"], entry["role"]) == (
        700, EXE, "444", "browser")


# --- guard -------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8080/x", "http://localhost.:1/",
    "http://[::1]/", "http://172.31.0.5/", "http://169.254.169.254/latest/meta-data",
    "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]:1/", "https://local.example/",
    "http://meta.example/", "http://lan.example/", "http://no-such.example/",
    "file:///C:/Windows/win.ini", "chrome://settings", "edge://settings",
    "javascript:alert(1)", "ws://127.0.0.1:9222/devtools", "wss://local.example/",
    "ftp://public.example/", "about:config", "http://public.example:99999/", "",
    "http://localhost/", "http://LOCALHOST./", "http://x.localhost:8810/v1",
    "http://a.b.localhost/", "wss://x.localhost/", "http://printer.local/",
    "http://router/", "http://2130706433/",
])
def test_guard_refuses(url):
    assert guard.allowed(url, resolve=fake_resolve) is False


def test_local_names_refused_without_asking_dns():
    """A resolver that rewrites unknown names to a public address can't make these pass."""
    asked = []

    def rewriting(host, port, *a, **k):
        asked.append(host)
        return fake_resolve("public.example", port)
    for url in ("http://x.localhost/", "http://localhost/", "http://nas.local/", "http://intranet/"):
        assert guard.allowed(url, resolve=rewriting) is False
    assert asked == []
    assert guard.allowed("http://public.example/", resolve=rewriting) is True


@pytest.mark.parametrize("url", [
    "https://93.184.215.14/", "http://public.example/page", "wss://public.example/socket",
    "data:text/html,<h1>hi</h1>", "blob:https://public.example/1c3e", "about:blank",
])
def test_guard_allows(url):
    assert guard.allowed(url, resolve=fake_resolve) is True


@pytest.mark.parametrize("url", [
    "https://public.example:9000/", "http://public.example:8079/", "wss://public.example:8444/",
    "http://public.example:22/", "https://public.example:0/",
])
def test_guard_refuses_ports_the_proxy_refuses(url):
    assert guard.allowed(url, resolve=fake_resolve) is False


@pytest.mark.parametrize("url", [
    "https://public.example:8443/", "http://public.example:80/", "ws://public.example:8080/",
    "https://public.example:443/x", "http://public.example:8200/",
])
def test_guard_allows_the_proxys_ports(url):
    assert guard.allowed(url, resolve=fake_resolve) is True


def test_the_guard_and_the_proxy_share_one_port_rule():
    from primer.live import proxy
    assert proxy.ALLOWED_PORTS is guard.ALLOWED_PORTS
    assert proxy.GuardProxy().allowed_ports == guard.ALLOWED_PORTS


def test_an_off_list_port_is_a_bad_address_not_a_tunnel_failure(make):
    svc = make()
    with pytest.raises(LiveError) as e:
        svc.open("w1", "https://public.example:9000/", 800, 600)
    assert (e.value.status, e.value.message) == (400, BAD_ADDRESS)
    svc.open("w1", "https://public.example/", 800, 600)
    with pytest.raises(LiveError) as e:
        svc.nav("w1", "url", "http://public.example:9000/")
    assert (e.value.status, e.value.message) == (400, BAD_ADDRESS)
    cdp = cdp_of(svc)
    cdp.fire("Fetch.requestPaused", {"requestId": "r9",
                                     "request": {"url": "https://public.example:9000/a.js"}})
    assert wait_for(lambda: cdp.named("Fetch.failRequest"))
    assert cdp.named("Fetch.failRequest")[0]["requestId"] == "r9"


# --- the service: requests, limits, settings ---------------------------------------------------

def test_paused_requests_local_failed_public_continued(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    assert {"patterns": [{"urlPattern": "*"}]} in cdp.named("Fetch.enable", None)
    for rid, url in (("r1", "http://127.0.0.1:8810/v1"), ("r2", "http://local.example/"),
                     ("r3", "http://172.31.0.5/"), ("r4", "https://public.example/app.js")):
        cdp.fire("Fetch.requestPaused", {"requestId": rid, "request": {"url": url}})
    assert wait_for(lambda: len(cdp.named("Fetch.failRequest")) == 3
                    and len(cdp.named("Fetch.continueRequest")) == 1)
    assert {p["requestId"] for p in cdp.named("Fetch.failRequest")} == {"r1", "r2", "r3"}
    assert all(p["errorReason"] == "BlockedByClient" for p in cdp.named("Fetch.failRequest"))
    assert cdp.named("Fetch.continueRequest") == [{"requestId": "r4"}]


def test_guard_runs_off_the_event_thread(make):
    """A slow DNS answer doesn't stall later events (Ruling A)."""
    gate = threading.Event()

    def slow_resolve(host, port, *a, **k):
        if host == "slow.example":
            gate.wait(5)
        return fake_resolve("public.example", port)

    svc = make(resolve=slow_resolve)
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    t0 = time.monotonic()
    cdp.fire("Fetch.requestPaused", {"requestId": "slow", "request": {"url": "http://slow.example/"}})
    cdp.fire("Fetch.requestPaused", {"requestId": "fast", "request": {"url": "http://127.0.0.1/"}})
    assert time.monotonic() - t0 < 1
    assert wait_for(lambda: cdp.named("Fetch.failRequest"))
    gate.set()
    assert wait_for(lambda: cdp.named("Fetch.continueRequest"))


def test_slow_dns_is_refused_and_never_stalls_frames_or_new_targets(make):
    gate = threading.Event()

    def stuck(host, port, *a, **k):
        if host.endswith("slow.example"):
            gate.wait(10)
        return fake_resolve("public.example", port)

    svc = make(resolve=stuck, dns_timeout=0.3)
    try:
        svc.open("w1", "https://public.example/", 800, 600)
        cdp = cdp_of(svc)
        for i in range(30):  # more stuck lookups than every pool has workers
            cdp.fire("Fetch.requestPaused", {"requestId": f"s{i}",
                                             "request": {"url": f"http://h{i}.slow.example/"}})
        t0 = time.monotonic()
        cdp.fire("Page.screencastFrame", frame_event(77), "S1")
        cdp.fire("Target.attachedToTarget", {"sessionId": "IF2", "waitingForDebugger": True,
                                             "targetInfo": {"type": "iframe", "targetId": "X5"}}, "S1")
        assert wait_for(lambda: {"sessionId": 77} in cdp.named("Page.screencastFrameAck")
                        and "Runtime.runIfWaitingForDebugger" in cdp.methods("IF2"), 1.0)
        assert time.monotonic() - t0 < 1.0
        # and the stuck lookups end refused, not allowed
        assert wait_for(lambda: len(cdp.named("Fetch.failRequest")) == 30, 6)
        assert not cdp.named("Fetch.continueRequest")
    finally:
        gate.set()


def test_fifth_window_gets_409(make):
    svc = make()
    for i in range(4):
        svc.open(f"w{i}", "https://public.example/", 800, 600)
    with pytest.raises(LiveError) as e:
        svc.open("w5", "https://public.example/", 800, 600)
    assert (e.value.status, e.value.message) == (409, "Up to 4 live pages at a time")
    assert svc.status()["windows"] == ["w0", "w1", "w2", "w3"]
    svc.open("w1", "https://public.example/other", 800, 600)  # an open window just navigates
    svc.close("w0")
    svc.open("w5", "https://public.example/", 800, 600)


def test_web_search_off_gets_403(make):
    svc = make(tier("off"))
    with pytest.raises(LiveError) as e:
        svc.open("w1", "https://public.example/", 800, 600)
    assert (e.value.status, e.value.message) == (403, "Live pages are off while web search is off")
    assert svc.status()["enabled"] is False
    assert FakeBrowser.made == []


def test_no_browser_gets_503(make):
    svc = make(find=lambda p: None)
    with pytest.raises(LiveError) as e:
        svc.open("w1", "https://public.example/", 800, 600)
    assert (e.value.status, e.value.message) == (503, "Live pages need Chrome or Edge")
    assert svc.status() == {"available": False, "label": "", "enabled": True, "windows": []}


@pytest.mark.parametrize("url", ["http://127.0.0.1:8810/", "file:///C:/x", "chrome://version",
                                 "http://local.example/", "javascript:1", 7, None])
def test_bad_addresses_get_400(make, url):
    svc = make()
    with pytest.raises(LiveError) as e:
        svc.open("w1", url, 800, 600)
    assert e.value.status == 400
    assert svc.status()["windows"] == []


def test_status_reports_the_browser(make):
    svc = make()
    assert svc.status() == {"available": True, "label": "Chrome 158", "enabled": True,
                            "windows": []}


# --- each tab: safety set-up -------------------------------------------------------------------

def test_downloads_and_permissions_denied_on_each_tab(make):
    svc = make()
    cdp = None
    for i, name in enumerate(("a", "b")):
        svc.open(name, "https://public.example/", 800, 600)
        cdp = cdp_of(svc)
        assert len([p for p in cdp.named("Browser.setDownloadBehavior")
                    if p["behavior"] == "deny"]) >= i + 2  # browser start + each tab
        sid = svc._windows[name].session  # b's tab comes after a re-probe's tab
        assert cdp.named("Page.setDownloadBehavior", sid) == [{"behavior": "deny"}]
    denied = [p["permission"]["name"] for p in cdp.named("Browser.setPermission")
              if p["setting"] == "denied"]
    for name in ("geolocation", "notifications", "camera", "microphone", "local-network-access"):
        assert denied.count(name) >= 2


def test_new_targets_are_made_safe_before_they_run(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    assert wait_for(lambda: "Runtime.runIfWaitingForDebugger" in cdp.methods("S1"))
    m = cdp.methods("S1")
    assert m.index("Page.addScriptToEvaluateOnNewDocument") < m.index("Runtime.runIfWaitingForDebugger")
    assert m.index("Target.setAutoAttach") < m.index("Runtime.runIfWaitingForDebugger")
    script = cdp.named("Page.addScriptToEvaluateOnNewDocument", "S1")[0]
    assert script["source"] == NO_SOCKETS_JS and script["runImmediately"] is True
    # an out-of-process iframe and a worker, attached under the tab's session
    cdp.fire("Target.attachedToTarget", {"sessionId": "IF", "waitingForDebugger": True,
                                         "targetInfo": {"type": "iframe", "targetId": "X1"}}, "S1")
    cdp.fire("Target.attachedToTarget", {"sessionId": "WK", "waitingForDebugger": True,
                                         "targetInfo": {"type": "worker", "targetId": "X2"}}, "S1")
    assert wait_for(lambda: "Runtime.runIfWaitingForDebugger" in cdp.methods("IF")
                    and "Runtime.runIfWaitingForDebugger" in cdp.methods("WK"))
    assert cdp.methods("IF")[:3] == ["Page.enable", "Page.addScriptToEvaluateOnNewDocument",
                                     "Target.setAutoAttach"]
    # the worker runs only into a breakpoint before its first script
    assert cdp.methods("WK") == ["Debugger.enable", "Debugger.setInstrumentationBreakpoint",
                                 "Target.setAutoAttach", "Runtime.runIfWaitingForDebugger"]
    assert cdp.named("Debugger.setInstrumentationBreakpoint", "WK") == [
        {"instrumentation": "beforeScriptExecution"}]
    cdp.fire("Debugger.paused", {"reason": "instrumentation"}, "WK")
    assert wait_for(lambda: "Debugger.disable" in cdp.methods("WK"))
    m = cdp.methods("WK")
    assert cdp.named("Runtime.evaluate", "WK")[0]["expression"] == NO_SOCKETS_JS
    assert m.index("Runtime.evaluate") < m.index("Debugger.disable")  # locked, then resumed


def test_a_worker_already_running_is_locked_directly(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    cdp.fire("Target.attachedToTarget", {"sessionId": "RUN", "waitingForDebugger": False,
                                         "targetInfo": {"type": "service_worker", "targetId": "X8"}})
    assert wait_for(lambda: cdp.named("Runtime.evaluate", "RUN"))
    assert cdp.named("Runtime.evaluate", "RUN")[0]["expression"] == NO_SOCKETS_JS
    assert "Debugger.enable" not in cdp.methods("RUN")


def test_a_worker_that_cannot_be_locked_is_closed(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)

    def refuse(p, s):
        if s == "SW":
            raise CdpError("nope")
        return {}
    cdp.handlers["Runtime.evaluate"] = refuse
    cdp.fire("Target.attachedToTarget", {"sessionId": "SW", "waitingForDebugger": True,
                                         "targetInfo": {"type": "service_worker", "targetId": "X9"}})
    assert wait_for(lambda: "Runtime.runIfWaitingForDebugger" in cdp.methods("SW"))
    cdp.fire("Debugger.paused", {"reason": "instrumentation"}, "SW")
    assert wait_for(lambda: {"targetId": "X9"} in cdp.named("Target.closeTarget"))
    assert "Debugger.disable" not in cdp.methods("SW")


@pytest.mark.parametrize("failing", ["Debugger.enable", "Debugger.setInstrumentationBreakpoint",
                                     "Target.setAutoAttach"])
@pytest.mark.parametrize("ttype", ["worker", "shared_worker", "service_worker"])
def test_worker_set_up_failure_closes_it(make, failing, ttype):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)

    def refuse(p, s):
        if s == "WF":
            raise CdpError("nope")
        return {}
    cdp.handlers[failing] = refuse
    cdp.fire("Target.attachedToTarget", {"sessionId": "WF", "waitingForDebugger": True,
                                         "targetInfo": {"type": ttype, "targetId": "XW"}})
    assert wait_for(lambda: {"targetId": "XW"} in cdp.named("Target.closeTarget"))
    if ttype != "service_worker":  # these answer while paused: checked before they run
        assert "Runtime.runIfWaitingForDebugger" not in cdp.methods("WF")
    assert "WF" not in svc._worker_init


def test_a_worker_that_never_pauses_is_closed(make):
    now = [0.0]
    svc = make(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    cdp.fire("Target.attachedToTarget", {"sessionId": "NP", "waitingForDebugger": True,
                                         "targetInfo": {"type": "worker", "targetId": "XN"}})
    assert wait_for(lambda: "Runtime.runIfWaitingForDebugger" in cdp.methods("NP"))
    now[0] += 4  # no Debugger.paused within WORKER_PAUSE_S
    assert wait_for(lambda: {"targetId": "XN"} in cdp.named("Target.closeTarget"))


def test_worker_entry_dropped_when_it_detaches(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    cdp.fire("Target.attachedToTarget", {"sessionId": "DT", "waitingForDebugger": True,
                                         "targetInfo": {"type": "worker", "targetId": "XD"}})
    assert wait_for(lambda: "DT" in svc._worker_init)
    cdp.fire("Target.detachedFromTarget", {"sessionId": "DT", "targetId": "XD"}, "S1")
    assert "DT" not in svc._worker_init


def test_unknown_paused_target_types_are_closed_not_run(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    cdp.fire("Target.attachedToTarget", {"sessionId": "OT", "waitingForDebugger": True,
                                         "targetInfo": {"type": "auction_worklet", "targetId": "XO"}})
    assert wait_for(lambda: {"targetId": "XO"} in cdp.named("Target.closeTarget"))
    assert "Runtime.runIfWaitingForDebugger" not in cdp.methods("OT")


def test_dns_backlog_is_capped_and_slow_lookups_time_out(make):
    gate = threading.Event()
    started = []

    def stuck(host, port, *a, **k):
        started.append(host)
        gate.wait(10)
        return fake_resolve("public.example", port)
    svc = make(resolve=stuck, dns_timeout=0.2)
    try:
        t0 = time.monotonic()
        with svc._lock:
            svc._dns_backlog = 64  # as if 64 lookups were waiting
        with pytest.raises(OSError, match="too many"):
            svc._timed_resolve("a.example", None)
        assert time.monotonic() - t0 < 0.1 and started == []
        with svc._lock:
            svc._dns_backlog = 0
        for i in range(20):  # each is given up after the timeout (and frees its slot)
            with pytest.raises(OSError, match="took too long"):
                svc._timed_resolve(f"h{i}.example", None)
        assert svc._dns_backlog == 0
    finally:
        gate.set()
    assert wait_for(lambda: svc._dns_backlog == 0 and svc._dns_threads == 0, 3)


def test_a_target_that_cannot_be_made_safe_never_runs(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)

    def refuse(p, s):
        if s == "BAD":
            raise CdpError("nope")
    cdp.handlers["Page.addScriptToEvaluateOnNewDocument"] = refuse
    cdp.fire("Target.attachedToTarget", {"sessionId": "BAD", "waitingForDebugger": True,
                                         "targetInfo": {"type": "iframe", "targetId": "X3"}}, "S1")
    time.sleep(0.3)
    assert "Runtime.runIfWaitingForDebugger" not in cdp.methods("BAD")


def test_popups_open_in_the_same_tab(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    cdp.fire("Target.attachedToTarget", {"sessionId": "P1", "waitingForDebugger": True, "targetInfo": {
        "type": "page", "targetId": "POP", "url": "https://public.example/popup", "openerId": "T1"}})
    assert wait_for(lambda: {"targetId": "POP"} in cdp.named("Target.closeTarget"))
    assert {"url": "https://public.example/popup"} in cdp.named("Page.navigate", "S1")
    # a pop-up to a local address is closed without being followed
    cdp.fire("Target.attachedToTarget", {"sessionId": "P2", "waitingForDebugger": True, "targetInfo": {
        "type": "page", "targetId": "POP2", "url": "http://127.0.0.1:8810/", "openerId": "T1"}})
    assert wait_for(lambda: {"targetId": "POP2"} in cdp.named("Target.closeTarget"))
    assert {"url": "http://127.0.0.1:8810/"} not in cdp.named("Page.navigate")


def test_pages_that_are_not_ours_are_closed(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    # a noopener pop-up: no openerId, and no open() of ours is making a tab
    cdp.fire("Target.attachedToTarget", {"sessionId": "N1", "waitingForDebugger": True, "targetInfo": {
        "type": "page", "targetId": "NOOP", "url": "https://public.example/x"}})
    assert wait_for(lambda: {"targetId": "NOOP"} in cdp.named("Target.closeTarget"))
    assert svc.status()["windows"] == ["w1"]


def test_stray_page_during_open_is_closed_once_our_tab_is_claimed(make):
    svc = make()
    svc.open("w0", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    orig = cdp.handlers["Target.createTarget"]

    def create_with_stray(p, s):
        cdp.fire("Target.attachedToTarget", {"sessionId": "ST", "waitingForDebugger": True,
                                             "targetInfo": {"type": "page", "targetId": "STRAY"}})
        time.sleep(0.2)  # let the stray's set-up land while our createTarget is pending
        return orig(p, s)
    cdp.handlers["Target.createTarget"] = create_with_stray
    svc.open("w1", "https://public.example/", 800, 600)
    assert {"targetId": "STRAY"} in cdp.named("Target.closeTarget")
    assert svc.status()["windows"] == ["w0", "w1"]


def test_blank_popups_are_capped_and_swept(make):
    now = [0.0]
    svc = make(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    for i in range(10):
        cdp.fire("Target.attachedToTarget", {"sessionId": f"B{i}", "waitingForDebugger": True,
                                             "targetInfo": {"type": "page", "targetId": f"BL{i}",
                                                            "url": "", "openerId": "T1"}})
    # beyond MAX_POPUPS (8) waiting, the extras are closed at once
    assert wait_for(lambda: len(closed_but_probe(cdp)) == 2)
    sweepers = [t for t in threading.enumerate() if t.name == "live-sweep"]
    assert sweepers and all(t.daemon for t in sweepers)  # one sweeper, not a thread a pop-up
    assert svc._sweeper in sweepers
    now[0] += 6  # the address never came
    assert wait_for(lambda: len(closed_but_probe(cdp)) == 10, 3)


def test_finding_no_browser_is_retried_after_a_minute(make):
    now, answers = [0.0], [None, None, ("C:/Chrome/chrome.exe", "Chrome 158")]
    calls = []

    def find(p):
        calls.append(p)
        return answers[len(calls) - 1]
    svc = make(find=find, clock=lambda: now[0])
    assert svc.status()["available"] is False
    now[0] += 30
    assert svc.status()["available"] is False and len(calls) == 1
    now[0] += 31
    svc.status()
    now[0] += 61
    assert svc.status() == {"available": True, "label": "Chrome 158", "enabled": True,
                            "windows": []}
    assert len(calls) == 3


def test_download_attempt_is_told_to_the_window(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    it = svc.frames("w1", keepalive=2)
    cdp_of(svc).fire("Browser.downloadWillBegin", {"frameId": "T1", "url": "https://x/y.zip"})
    assert next(it) == {"note": "Primer's browser doesn't download files"}


# --- input -------------------------------------------------------------------------------------

@pytest.fixture
def opened(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    return svc, cdp_of(svc)


@pytest.mark.parametrize("event,status", [
    ({"kind": "text", "text": "x" * 5000}, 413),
    ({"kind": "teleport"}, 400),
    ({"kind": "mouse", "type": "down", "x": float("nan"), "y": 3}, 400),
    ({"kind": "mouse", "type": "down", "x": float("inf"), "y": 3}, 400),
    ({"kind": "mouse", "type": "down", "x": True, "y": 3}, 400),
    ({"kind": "mouse", "type": "down", "x": 900, "y": 3}, 400),
    ({"kind": "mouse", "type": "down", "x": -1, "y": 3}, 400),
    ({"kind": "mouse", "type": "fling", "x": 1, "y": 3}, 400),
    ({"kind": "wheel", "x": 1, "y": 1, "dy": float("nan")}, 400),
    ({"kind": "key", "type": "down", "key": "F13Whatever"}, 400),
    ({"kind": "text", "text": "x" * 1001}, 400),
    ({"kind": "text", "text": ""}, 400),
    ({"kind": "mouse", "type": "down", "x": 1, "y": 1, "modifiers": 99}, 400),
])
def test_bad_input_refused(opened, event, status):
    svc, cdp = opened
    with pytest.raises(LiveError) as e:
        svc.input("w1", event)
    assert e.value.status == status
    assert not cdp.named("Input.dispatchMouseEvent") and not cdp.named("Input.insertText")


def test_input_to_unknown_window_404(opened):
    svc, _ = opened
    with pytest.raises(LiveError) as e:
        svc.input("nope", {"kind": "text", "text": "a"})
    assert e.value.status == 404


def test_key_and_text_mapping(opened):
    svc, cdp = opened
    svc.input("w1", {"kind": "key", "type": "down", "key": "Enter"})
    svc.input("w1", {"kind": "key", "type": "up", "key": "Enter"})
    svc.input("w1", {"kind": "key", "type": "down", "key": "a", "modifiers": 2})
    svc.input("w1", {"kind": "key", "type": "down", "key": "Backspace"})
    svc.input("w1", {"kind": "text", "text": "Bodiam castle"})
    keys = cdp.named("Input.dispatchKeyEvent", "S1")
    assert keys[0] == {"type": "keyDown", "key": "Enter", "code": "Enter", "text": "\r",
                       "unmodifiedText": "\r", "windowsVirtualKeyCode": 13,
                       "nativeVirtualKeyCode": 13, "modifiers": 0}
    assert keys[1]["type"] == "keyUp" and "text" not in keys[1]
    assert keys[2] == {"type": "rawKeyDown", "key": "a", "code": "KeyA",
                       "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65, "modifiers": 2}
    assert keys[3]["type"] == "rawKeyDown" and keys[3]["windowsVirtualKeyCode"] == 8
    assert cdp.named("Input.insertText", "S1") == [{"text": "Bodiam castle"}]


def test_mouse_coordinates_scaled_from_the_frame(opened):
    svc, cdp = opened
    cdp.fire("Page.screencastFrame", frame_event(1, w=400, h=300), "S1")  # a half-size frame
    svc.input("w1", {"kind": "mouse", "type": "down", "x": 100, "y": 150, "button": "left"})
    svc.input("w1", {"kind": "wheel", "x": 400, "y": 300, "dy": 120})
    m = cdp.named("Input.dispatchMouseEvent", "S1")
    assert m[0] == {"type": "mousePressed", "x": 200.0, "y": 300.0, "button": "left",
                    "clickCount": 1, "modifiers": 0}
    assert m[1]["type"] == "mouseWheel" and (m[1]["x"], m[1]["y"]) == (800.0, 600.0)
    assert m[1]["deltaY"] == 120.0


# --- frames ------------------------------------------------------------------------------------

def test_jpeg_size_reads_the_sof_marker():
    sof = b"\xff\xc0\x00\x11\x08" + (480).to_bytes(2, "big") + (640).to_bytes(2, "big") + b"\x03"
    jpeg = b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + sof + b"\x00" * 20
    assert _jpeg_size(base64.b64encode(jpeg).decode()) == (640, 480)
    assert _jpeg_size(base64.b64encode(b"not a jpeg").decode()) is None


def test_every_frame_acked_and_only_the_latest_kept(opened):
    svc, cdp = opened
    it = svc.frames("w1")
    for i in range(1, 31):
        cdp.fire("Page.screencastFrame", frame_event(i), "S1")
    first = next(it)
    assert base64.b64decode(first["data"]) == b"frame30" and (first["w"], first["h"]) == (800, 600)
    assert wait_for(lambda: len(cdp.named("Page.screencastFrameAck", "S1")) == 30)
    assert sorted(p["sessionId"] for p in cdp.named("Page.screencastFrameAck")) == list(range(1, 31))


def test_frame_rate_capped_by_dropping(opened):
    svc, cdp = opened
    stop = threading.Event()

    def pump():
        i = 0
        while not stop.is_set():
            i += 1
            cdp.fire("Page.screencastFrame", frame_event(i), "S1")
            time.sleep(0.002)
    threading.Thread(target=pump, daemon=True).start()
    got, t0 = 0, time.monotonic()
    try:
        for f in svc.frames("w1"):
            if "data" in f:
                got += 1
            if time.monotonic() - t0 >= 1.0:
                break
    finally:
        stop.set()
    assert 5 <= got <= 12  # ~10 a second although hundreds arrived


def test_oversized_frame_dropped(opened):
    svc, cdp = opened
    big = {"data": "A" * (3 * 1024 * 1024), "sessionId": 1, "metadata": {}}
    cdp.fire("Page.screencastFrame", big, "S1")
    it = svc.frames("w1", keepalive=0.3)
    assert next(it) == {"ping": True}
    assert wait_for(lambda: cdp.named("Page.screencastFrameAck"))  # still acknowledged


def test_browser_dying_ends_frames_with_gone(opened):
    svc, cdp = opened
    out = []
    t = threading.Thread(target=lambda: out.extend(svc.frames("w1")), daemon=True)
    t.start()
    time.sleep(0.1)
    cdp.close()  # the connection drops: killed or crashed
    t.join(3)
    assert not t.is_alive()
    assert out[-1] == {"gone": "The browser stopped"}
    assert svc.status()["windows"] == []
    svc.open("w1", "https://public.example/", 800, 600)  # Reload restarts it
    assert len(FakeBrowser.made) == 2


def test_closing_a_window_ends_frames_and_closes_its_tab(opened):
    svc, cdp = opened
    it = svc.frames("w1")
    assert svc.close("w1") is True
    assert next(it) == {"gone": "closed"}
    assert {"targetId": "T1"} in cdp.named("Target.closeTarget")
    assert cdp.named("Page.stopScreencast", "S1")


def test_a_crashed_tab_ends_its_window(opened):
    svc, cdp = opened
    it = svc.frames("w1")
    cdp.fire("Inspector.targetCrashed", {}, "S1")
    assert next(it) == {"gone": "The page crashed"}


# --- lifecycle ---------------------------------------------------------------------------------

def test_idle_shutdown_after_10_minutes(make):
    now = [1000.0]
    svc = make(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    now[0] += 3600
    assert svc.tick() is False  # a window is open
    svc.close("w1")
    now[0] += 599
    assert svc.tick() is False
    now[0] += 2
    assert svc.tick() is True
    assert FakeBrowser.made[0].stopped
    svc.open("w1", "https://public.example/", 800, 600)
    assert len(FakeBrowser.made) == 2


class FakeProxy:
    made: list = []
    events: list = []

    def __init__(self, resolve):
        self.resolve = resolve
        self.port = 40000 + len(FakeProxy.made)
        self.stopped = False
        self.probe_host = f"p{self.port}.primer-proxy-probe.invalid"
        self.probe_url = f"http://{self.probe_host}/"
        self.probes = 1
        FakeProxy.made.append(self)

    seen = True  # False: the probe never arrives (a proxy policy now routes around it)

    def new_probe(self):
        self.probes += 1
        self.probe_host = f"p{self.port}-{self.probes}.primer-proxy-probe.invalid"
        self.probe_url = f"http://{self.probe_host}/"
        return self.probe_host

    def wait_probe(self, timeout):
        return FakeProxy.seen

    def start(self):
        FakeProxy.events.append(("proxy start", self.port))
        return self.port

    def stop(self):
        FakeProxy.events.append(("proxy stop", self.port,
                                 all(b.stopped for b in FakeBrowser.made)))
        self.stopped = True


@pytest.fixture
def proxied(make):
    FakeProxy.made, FakeProxy.events, FakeProxy.seen = [], [], True
    return lambda **kw: make(proxy=FakeProxy, **kw)


def test_the_proxy_starts_before_the_browser_and_stops_after_it(proxied):
    svc = proxied()
    svc.open("w1", "https://public.example/", 800, 600)
    p = FakeProxy.made[0]
    assert p.resolve is fake_resolve
    assert FakeBrowser.made[0].proxy_port == p.port
    assert FakeProxy.events == [("proxy start", p.port)]
    svc.turn_off()
    assert FakeProxy.events[-1] == ("proxy stop", p.port, True)  # the browser stopped first


def test_a_restarted_browser_gets_a_fresh_proxy(proxied):
    svc = proxied()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp_of(svc).close()  # the browser died
    svc.open("w2", "https://public.example/", 800, 600)
    first, second = FakeProxy.made
    assert first.stopped and not second.stopped
    assert FakeBrowser.made[1].proxy_port == second.port != first.port
    svc.shutdown()
    assert second.stopped


def test_the_proxy_stops_with_the_idle_browser_and_when_the_start_fails(proxied):
    now = [1000.0]
    svc = proxied(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    svc.close("w1")
    now[0] += 601
    assert svc.tick() is True and FakeProxy.made[0].stopped

    class Broken(FakeBrowser):
        def start(self, proxy_port):
            raise OSError("no")
    svc._launcher = Broken
    with pytest.raises(LiveError):
        svc.open("w1", "https://public.example/", 800, 600)
    assert FakeProxy.made[1].stopped


def test_a_browser_that_bypasses_the_proxy_is_refused(make, monkeypatch):
    from primer.live.proxy import GuardProxy
    proxies = []

    class Recorded(GuardProxy):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.browser_stopped_first = None
            proxies.append(self)

        def stop(self):
            if self.browser_stopped_first is None:
                self.browser_stopped_first = all(b.stopped for b in FakeBrowser.made)
            super().stop()
    monkeypatch.setattr(FakeBrowser, "proxied", False)
    svc = make(probe_timeout=0.3, proxy=Recorded)
    with pytest.raises(LiveError) as e:
        svc.open("w1", "https://public.example/", 800, 600)
    assert e.value.status == 503 and "guarding proxy" in e.value.message
    b = FakeBrowser.made[0]
    assert b.stopped
    probe = b.cdp.named("Page.navigate", "PS")[0]["url"]
    assert probe == proxies[0].probe_url and probe.endswith(".primer-proxy-probe.invalid/")
    assert {"targetId": "PROBE"} in b.cdp.named("Target.closeTarget")  # the probe tab closed
    assert not b.cdp.named("Fetch.enable", None) or all(
        s == "PS" for m, _p, s in b.cdp.calls if m == "Fetch.enable")  # no page ever ran
    assert svc._proxy is None and svc.status()["windows"] == []
    p = proxies[0]
    assert p._stopping.is_set() and p._listener is None  # the real proxy was stopped
    assert p.browser_stopped_first is True


def test_the_probe_tab_runs_no_page_content(make, monkeypatch):
    monkeypatch.setattr(FakeCdp, "probe_pauses", True)
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    calls = [(m, p, s) for m, p, s in cdp.calls]

    def at(method, session="any", **match):
        return next(i for i, (m, p, s) in enumerate(calls) if m == method
                    and (session == "any" or s == session)
                    and all(p.get(k) == v for k, v in match.items()))
    made = at("Target.createTarget", url="about:blank")
    attach = at("Target.attachToTarget", targetId="PROBE")
    noscript = at("Emulation.setScriptExecutionDisabled", "PS", value=True)
    fetch = at("Fetch.enable", "PS")
    nav = at("Page.navigate", "PS", url=svc._proxy.probe_url)
    stop = at("Page.stopLoading", "PS")
    close = at("Target.closeTarget", targetId="PROBE")
    assert made < attach < noscript < fetch < nav < stop < close
    assert close < at("Fetch.enable", None)  # the browser-wide guard comes after
    stages = {p["requestStage"] for p in calls[fetch][1]["patterns"]}
    assert stages == {"Request", "Response"}
    # only the probe host's request goes on; its response, other hosts and sockets fail
    assert cdp.named("Fetch.continueRequest", "PS") == [{"requestId": "P0"},
                                                        {"requestId": "P1"}]
    assert [p["requestId"] for p in cdp.named("Fetch.failRequest", "PS")] == ["P2", "P3", "P4"]
    assert svc._proxy.wait_probe(0)
    assert not [fn for fn in cdp.listeners.get("Fetch.requestPaused", [])
                if getattr(fn, "__name__", "") == "paused"]  # the probe's handler is gone


@pytest.mark.parametrize("where", ["launcher", "wire", "deny"])
def test_nothing_leaks_when_start_up_fails_anywhere(proxied, monkeypatch, where):
    svc = proxied()
    if where == "launcher":
        def boom(*a, **k):
            raise ValueError("launcher")
        svc._launcher = boom
    else:
        def boom(*a, **k):
            raise TimeoutError(where)
        monkeypatch.setattr(svc, "_" + where, boom)
    with pytest.raises((ValueError, TimeoutError)):
        svc.open("w1", "https://public.example/", 800, 600)
    assert FakeProxy.made[0].stopped
    assert all(b.stopped for b in FakeBrowser.made)
    assert FakeProxy.events[-1][2] is True  # the browser stopped before the proxy


def probes_of(cdp):
    """The probe URLs the service had a probe tab load, in order."""
    with cdp._lock:
        return [p["url"] for m, p, s in cdp.calls
                if m == "Page.navigate" and str(s).startswith("PS")]


def test_each_new_window_proves_the_proxy_again(make):
    """Chrome applies a proxy policy change at once, so a running browser proves its proxy
    again, with a fresh probe name, before each new live window opens."""
    svc = make(probe_timeout=2)
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    assert len(probes_of(cdp)) == 1
    svc.open("w1", "https://public.example/", 800, 600)  # the same window again: no new probe
    assert len(probes_of(cdp)) == 1
    svc.open("w2", "https://public.example/", 800, 600)
    first, second = probes_of(cdp)
    assert first != second and second == svc._proxy.probe_url and svc._proxy.wait_probe(0)
    # the browser-wide guard let the re-probe's request (and only it) through to the proxy
    (held,) = cdp.probe_held
    assert {"requestId": held[0]} in cdp.named("Fetch.continueRequest", None)
    assert svc.status()["windows"] == ["w1", "w2"] and not FakeBrowser.made[0].stopped
    probe_tab = next(t for t in cdp.probe_targets if t != "PROBE")
    assert {"targetId": probe_tab} in cdp.named("Target.closeTarget")
    assert probe_tab not in svc._by_target and not svc._attached and svc._creating == 0
    assert svc._probe_host is None and not svc._probe_sessions
    # once the re-probe is over, a request for that name is an ordinary (unresolvable) one
    cdp.fire("Fetch.requestPaused", {"requestId": "late", "request": {"url": second}})
    assert wait_for(lambda: any(p["requestId"] == "late" for p in cdp.named("Fetch.failRequest")))


def test_a_proxy_policy_that_appears_while_running_stops_the_browser(make):
    proxies = []
    from primer.live.proxy import GuardProxy

    class Recorded(GuardProxy):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.browser_stopped_first = None
            proxies.append(self)

        def stop(self):
            if self.browser_stopped_first is None:
                self.browser_stopped_first = all(b.stopped for b in FakeBrowser.made)
            super().stop()
    ended = []
    svc = make(probe_timeout=0.5, proxy=Recorded)
    svc.on_window_end = lambda name, gen, reason: ended.append((name, reason))
    svc.open("w1", "https://public.example/", 800, 600)
    b = FakeBrowser.made[0]
    b.cdp.proxy_port = None  # a policy now routes the browser around the proxy
    with pytest.raises(LiveError) as e:
        svc.open("w2", "https://public.example/", 800, 600)
    message = NO_PROXY.format(label="Chrome 158")
    assert (e.value.status, e.value.message) == (503, message)
    assert b.stopped and proxies[0]._stopping.is_set() and proxies[0].browser_stopped_first
    assert svc._proxy is None and svc.status()["windows"] == []
    assert ("w1", message) in ended  # (w2, still being opened, is discarded: "closed")


def test_while_windows_are_open_the_proxy_is_proved_again_each_minute(make):
    now = [1000.0]
    svc = make(clock=lambda: now[0], probe_timeout=0.5)
    svc.open("w1", "https://public.example/", 800, 600)
    it = svc.frames("w1", keepalive=0.05)
    assert next(it) == {"ping": True}  # read, so the orphan sweep leaves it open
    cdp = cdp_of(svc)
    now[0] += 59
    assert svc.tick() is False and len(probes_of(cdp)) == 1  # not yet a minute
    now[0] += 2
    assert svc.tick() is False and len(probes_of(cdp)) == 2  # proved again: still in the path
    now[0] += 30
    assert svc.tick() is False and len(probes_of(cdp)) == 2  # at most once a minute
    cdp.proxy_port = None  # a policy now routes around the proxy
    now[0] += 31
    assert svc.tick() is True and len(probes_of(cdp)) == 3
    assert FakeBrowser.made[0].stopped and svc._proxy is None
    assert svc.status()["windows"] == []
    it.close()


def test_no_windows_no_reprobe(proxied):
    now = [1000.0]
    svc = proxied(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    svc.close("w1")
    now[0] += 120
    FakeProxy.seen = False
    assert svc.tick() is False and not FakeBrowser.made[0].stopped  # only the idle timer runs
    with pytest.raises(LiveError) as e:  # the next window proves it first, and is refused
        svc.open("w2", "https://public.example/", 800, 600)
    assert e.value.status == 503 and "guarding proxy" in e.value.message
    assert FakeBrowser.made[0].stopped and FakeProxy.made[0].stopped
    assert FakeProxy.events[-1][2] is True  # the browser stopped before the proxy
    assert svc.status()["windows"] == []


def test_a_reprobe_never_waits_behind_page_lookups_or_tab_set_up(make):
    """A page with many slow-resolving names fills the guard pool, and slow targets fill the
    cdp pool: the re-probe is handled on the connection's thread and resumes its own tab, so it
    still passes, and no live window is closed."""
    gate = threading.Event()

    def slow_resolve(host, port, *a, **k):
        if host.startswith("slow"):
            gate.wait(10)
        return fake_resolve("public.example", port)
    svc = make(resolve=slow_resolve, dns_timeout=10, probe_timeout=1.5)
    try:
        svc.open("w1", "https://public.example/", 800, 600)
        cdp = cdp_of(svc)
        for i in range(40):  # far more than the guard pool's workers
            cdp.fire("Fetch.requestPaused", {"requestId": f"slow{i}",
                                             "request": {"url": f"https://slow{i}.example/x.js"}})
        block = threading.Event()
        cdp.handlers["Page.enable"] = lambda p, sess: block.wait(10) if sess.startswith("F") else None
        for i in range(8):  # frames whose set-up hangs: the cdp pool's workers are all busy
            cdp.fire("Target.attachedToTarget", {"sessionId": f"F{i}", "waitingForDebugger": True,
                                                 "targetInfo": {"type": "iframe",
                                                                "targetId": f"FR{i}"}}, "S1")
        # both pools stay blocked for longer than the probe may take (1.5 s); then they clear,
        # so w2's own tab can be set up
        threading.Timer(3.0, lambda: (gate.set(), block.set())).start()
        svc.open("w2", "https://public.example/", 800, 600)  # proves the proxy first
        assert len(probes_of(cdp)) == 2 and svc._proxy.wait_probe(0)
        assert not FakeBrowser.made[0].stopped and svc.status()["windows"] == ["w1", "w2"]
    finally:
        gate.set()
        block.set()


def test_a_crash_during_a_reprobe_says_the_browser_stopped_and_the_next_open_restarts(make):
    svc = make(probe_timeout=0.5)
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    real = cdp.handlers["Page.navigate"]

    def crash(params, session):
        if str(session).startswith("PS-"):
            cdp.close()  # the browser dies mid-probe
            raise CdpError("browser closed")
        return real(params, session)
    cdp.handlers["Page.navigate"] = crash
    with pytest.raises(LiveError) as e:
        svc.open("w2", "https://public.example/", 800, 600)
    assert (e.value.status, e.value.message) == (503, "The browser stopped")
    assert FakeBrowser.made[0].stopped and svc.status()["windows"] == []
    svc.open("w3", "https://public.example/", 800, 600)  # a fresh browser
    assert len(FakeBrowser.made) == 2 and svc.status()["windows"] == ["w3"]


def test_the_default_proxy_is_a_real_guard_proxy(make):
    from primer.live.proxy import GuardProxy
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    proxy = svc._proxy
    assert isinstance(proxy, GuardProxy)
    assert FakeBrowser.made[0].proxy_port == proxy.port
    svc.shutdown()
    try:
        s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    except OSError:
        pass
    else:
        s.close()
        pytest.fail("the proxy still listens after shutdown")


def test_no_sockets_js_leaves_websockets_to_the_proxy():
    names = NO_SOCKETS_JS[NO_SOCKETS_JS.index("const NAMES"):]
    names = names[:names.index(";")]
    assert "WebSocket" not in names and "WebSocketStream" not in names
    for kept in ("WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection"):
        assert f'"{kept}"' in names


def test_forget_stops_first_then_deletes(make, tmp_path):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    it = svc.frames("w1")
    profile = tmp_path / "var" / "browser"
    assert (profile / "Cookies").exists()
    svc.forget()
    assert FakeBrowser.made[0].stopped and FakeBrowser.made[0].profile_at_stop is True
    assert not profile.exists()
    assert not list((tmp_path / "var").glob("browser*"))
    assert next(it) == {"gone": "Forgotten"}
    svc.open("w1", "https://public.example/", 800, 600)  # the next open starts clean
    assert len(FakeBrowser.made) == 2


def test_shutdown_stops_the_browser(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    svc.shutdown()
    assert FakeBrowser.made[0].stopped
    with pytest.raises(LiveError):
        svc.open("w2", "https://public.example/", 800, 600)


# --- snapshot and act --------------------------------------------------------------------------

CONTROLS = [
    {"n": 1, "kind": "link", "label": "Opening hours", "href": "https://public.example/hours"},
    {"n": 2, "kind": "field", "label": "Search"},
    {"n": 3, "kind": "field", "label": "Password", "password": True},
    {"n": 4, "kind": "list", "label": "Day", "options": ["Saturday", "Sunday"]},
    {"n": 5, "kind": "button", "label": "Buy"},
    {"n": 6, "kind": "link", "label": "Admin", "href": "http://127.0.0.1:8810/"},
]


@pytest.fixture
def snapped(opened):
    svc, cdp = opened
    cdp.snapshot = {"title": "Castle", "url": "https://public.example/", "text": "Open daily",
                    "controls": CONTROLS + [{"n": "x", "kind": "evil"}]}
    for c in CONTROLS:
        cdp.found[c["n"]] = dict(c, ok=True, x=100, y=50)
    snap = svc.snapshot("w1")
    return svc, cdp, snap


def test_snapshot_shape(snapped):
    _svc, cdp, snap = snapped
    assert snap == {"title": "Castle", "url": "https://public.example/", "text": "Open daily",
                    "controls": CONTROLS}
    ev = [p for p in cdp.named("Runtime.evaluate", "S1") if "contextId" in p]
    assert ev and all(p["contextId"] == 7 for p in ev)  # an isolated world


def test_act_follow_uses_the_listed_href(snapped):
    svc, cdp, _ = snapped
    assert svc.act("w1", {"action": "follow", "n": 1, "href": "http://127.0.0.1/"}) == {
        "done": "follow", "url": "https://public.example/hours"}
    assert cdp.named("Page.navigate", "S1")[-1] == {"url": "https://public.example/hours"}
    with pytest.raises(LiveError):
        svc.act("w1", {"action": "follow", "n": 6})  # a listed link to a local address
    with pytest.raises(LiveError):
        svc.act("w1", {"action": "follow", "n": 5})  # not a link


def test_act_type_never_into_a_password_field(snapped):
    svc, cdp, _ = snapped
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "type", "n": 3, "text": "hunter2"})
    assert e.value.status == 403
    assert not cdp.named("Input.insertText")
    svc.act("w1", {"action": "type", "n": 2, "text": "Bodiam"})
    assert cdp.inserted == [(2, "Bodiam")]  # checked and typed in one evaluate in the page
    assert not cdp.named("Input.insertText")
    with pytest.raises(LiveError):
        svc.act("w1", {"action": "type", "n": 2, "text": "x" * 201})


def test_act_press_clicks_the_centre_and_refuses_a_changed_page(snapped):
    svc, cdp, _ = snapped
    svc.act("w1", {"action": "press", "n": 5, "x": 1, "y": 1})
    m = cdp.named("Input.dispatchMouseEvent", "S1")
    assert [p["type"] for p in m] == ["mousePressed", "mouseReleased"]
    assert all((p["x"], p["y"]) == (100, 50) for p in m)
    cdp.found[5] = dict(CONTROLS[1], ok=True, x=100, y=50)  # the page swapped the element
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5})
    assert e.value.status == 409
    del cdp.found[4]
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "choose", "n": 4, "option": "Sunday"})
    assert e.value.status == 409


def test_act_choose_and_scroll(snapped):
    svc, _cdp, _ = snapped
    assert svc.act("w1", {"action": "choose", "n": 4, "option": "Sunday"})["option"] == "Sunday"
    with pytest.raises(LiveError):
        svc.act("w1", {"action": "choose", "n": 4, "option": "Monday"})
    assert svc.act("w1", {"action": "scroll", "dir": "down"}) == {"done": "scroll"}
    for bad in ({"action": "scroll", "dir": "left"}, {"action": "press", "n": 99},
                {"action": "navigate", "url": "https://public.example/"}, "press"):
        with pytest.raises(LiveError):
            svc.act("w1", bad)


def test_act_is_bound_to_the_snapshot_it_came_from(snapped):
    svc, _cdp, _ = snapped
    first = svc.snapshot("w1", with_token=True)
    assert set(first) == {"title", "url", "text", "controls", "token"}
    svc.snapshot("w1")  # someone else read the page meanwhile: the numbering may have changed
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5}, token=first["token"])
    assert e.value.status == 409 and "changed" in e.value.message
    now = svc.snapshot("w1", with_token=True)
    assert svc.act("w1", {"action": "press", "n": 5}, token=now["token"])["done"] == "press"


def test_a_reopened_window_is_a_new_generation(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    old = svc.generation("w1")
    assert old and svc.is_open("w1", old)
    ended = []
    svc.on_window_end = lambda name, gen, reason: ended.append((name, gen, reason))
    svc.close("w1")
    svc.close("w1")  # ends once
    assert ended == [("w1", old, "closed")]
    assert svc.generation("w1") is None and not svc.is_open("w1")
    svc.open("w1", "https://public.example/", 800, 600)
    new = svc.generation("w1")
    assert new != old and svc.is_open("w1", new) and not svc.is_open("w1", old)
    for call in (lambda: svc.snapshot("w1", gen=old), lambda: svc.act("w1", {"action": "scroll", "dir": "down"}, gen=old)):
        with pytest.raises(LiveError) as e:
            call()
        assert e.value.status == 404


def test_browser_death_reports_each_window_once(make):
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    ended = []
    svc.on_window_end = lambda name, gen, reason: ended.append((name, reason))
    svc._stop_browser("The browser stopped")
    assert ended == [("w1", "The browser stopped")]


def test_search_off_refuses_and_turn_off_closes(snapped):
    svc, _cdp, snap = snapped
    it = svc.frames("w1")
    ended = []
    svc.on_window_end = lambda name, gen, reason: ended.append((name, reason))
    svc.tier = tier(search="off")
    for call in (lambda: svc.snapshot("w1"), lambda: svc.act("w1", {"action": "scroll", "dir": "down"}),
                 lambda: svc.nav("w1", "reload"), lambda: svc.input("w1", {"kind": "text", "text": "a"})):
        with pytest.raises(LiveError) as e:
            call()
        assert e.value.status == 403 and e.value.message == "Live pages are off while web search is off"
    svc.turn_off()
    assert FakeBrowser.made[0].stopped
    assert ended == [("w1", "Live pages are off while web search is off")]
    assert list(it)[-1] == {"gone": "Live pages are off while web search is off"}
    assert svc.status()["windows"] == []


# --- the real browser (opt-in) -----------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("PRIMER_LIVE_CHROME") != "1",
                    reason="set PRIMER_LIVE_CHROME=1 to run against the installed browser")
def test_real_browser_smoke(tmp_path):
    if find_browser() is None:
        pytest.skip("no Chrome or Edge installed")
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

        def log_message(self, *a):
            pass

    local = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=local.serve_forever, daemon=True).start()
    raw = socket.socket()
    for p in range(8080, 8444):  # a port the proxy allows, so only the address rule refuses
        try:
            raw.bind(("127.0.0.1", p))
            break
        except OSError:
            continue
    else:
        raw.close()
        pytest.skip("no free port in 8080-8443 for the local listener")
    raw.listen(8)
    raw_port, accepts = raw.getsockname()[1], []

    def accept():
        while True:
            try:
                c, a = raw.accept()
            except OSError:
                return
            accepts.append(a)
            c.close()
    threading.Thread(target=accept, daemon=True).start()
    svc = LiveService(tmp_path / "var", tier())
    browser = None
    try:
        svc.open("smoke", "data:text/html,<h1>hi</h1>", 640, 480)
        browser = svc._browser
        frame = next(f for f in svc.frames("smoke") if "data" in f or "gone" in f)
        assert base64.b64decode(frame["data"])[:2] == b"\xff\xd8"
        assert (frame["w"], frame["h"]) == (640, 480)
        snap = svc.snapshot("smoke")
        assert "hi" in snap["text"]
        w = svc._windows["smoke"]
        port = local.server_address[1]
        r = w.cdp.call("Runtime.evaluate", {
            "expression": f"fetch('http://127.0.0.1:{port}/x', {{mode: 'no-cors'}})"
                          ".then(() => 'reached', e => 'failed')",
            "awaitPromise": True, "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "failed"
        # WebSockets are allowed now; the guarding proxy refuses one to a local address
        proxy = svc._proxy
        assert proxy.wait_probe(0)  # the start-up probe came through it
        assert raw_port in proxy.allowed_ports
        before = proxy.stats()["refused"]
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "new Promise(done => { let ws; try { ws = new WebSocket("
                          f"'ws://127.0.0.1:{raw_port}/'); }} catch (e) {{ "
                          "return done('threw') } ws.onopen = () => done('opened'); "
                          "ws.onerror = ws.onclose = () => done('failed'); "
                          "setTimeout(() => done('timeout'), 8000) })",
            "awaitPromise": True, "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "failed"
        assert proxy.stats()["refused"] > before  # Chrome's traffic goes through the proxy
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "try { new RTCPeerConnection(); 'made' } catch (e) { 'refused' }",
            "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "refused"
        # a page that tampers with the builtins first still can't get a pop-up's first
        # document (window.open returns null: pop-ups open in the same view) or a socket
        tamper = ("Object.defineProperty = function () {}; Reflect.apply = function () "
                  "{ return window; }; Object.prototype.get = function () {}; "
                  "Function.prototype.call = function () {}; ")
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(() => { " + tamper + "const p = window.open(''); "
                          "const q = document.open('', 'x', ''); "
                          "if (p || q) { try { new (p || q).RTCPeerConnection(); "
                          "return 'made' } catch (e) { "
                          "return 'refused' } } return 'no-popup' })()",
            "userGesture": True, "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "no-popup"
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(typeof documentPictureInPicture === 'undefined') ? 'none' : "
                          "documentPictureInPicture.requestWindow().then(() => 'opened', "
                          "() => 'rejected')",
            "awaitPromise": True, "userGesture": True, "returnByValue": True}, session=w.session)
        assert r["result"]["value"] in ("none", "rejected")
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(() => { " + tamper + "try { new RTCPeerConnection(); "
                          "return 'made' } catch (e) { return 'refused' } })()",
            "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "refused"
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(() => { const f = document.createElement('iframe'); "
                          "document.body.appendChild(f); "
                          "try { new f.contentWindow.RTCPeerConnection(); "
                          "return 'made' } catch (e) { return 'refused' } })()",
            "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "refused"
        # preconnect opens a TCP connection without a request: network prediction is off
        w.cdp.call("Runtime.evaluate", {"expression": (
            "document.head.insertAdjacentHTML('beforeend', "
            f"'<link rel=preconnect href=\"http://127.0.0.1:{raw_port}\">"
            f"<link rel=dns-prefetch href=\"http://127.0.0.1:{raw_port}\">')")},
            session=w.session)
        time.sleep(2)
        assert accepts == []
        assert hits == []
        # clickjacking: a transparent frame from another origin lies over the button. The
        # allowed press is refused and nothing is clicked; once the frame is gone, it presses.
        frame = urllib.parse.quote(
            "<button style='width:100%;height:100%' onclick='parent.postMessage(1, \"*\")'>"
            "Confirm</button>")
        page = urllib.parse.quote(
            "<html><body style='margin:0;height:2000px'>"
            "<button style='position:absolute;left:100px;top:100px;width:200px;height:60px' "
            "onclick='window.pressed = (window.pressed || 0) + 1'>Search</button>"
            "<div id=faint style='opacity:0.05'><button style='position:absolute;left:100px;"
            "top:300px;width:200px;height:60px'>Faint</button></div>"
            f"<iframe src=\"data:text/html,{frame}\" style='position:fixed;left:0;top:0;"
            "width:100%;height:100%;border:0;opacity:0'></iframe>"
            "<script>window.framed = 0; addEventListener('message', () => window.framed++)"
            "</script></body></html>")
        first_probe = proxy.probe_host
        svc.open("jack", "data:text/html," + page, 640, 480)
        # a new window: the running browser proved its proxy again, through the page guard
        assert svc._proxy is proxy and proxy.probe_host != first_probe and proxy.wait_probe(0)
        jw = svc._windows["jack"]

        def main(expr):
            return jw.cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True},
                               session=jw.session)["result"].get("value")
        assert wait_for(lambda: main("document.readyState") == "complete", 10)
        time.sleep(0.5)  # the frame's own document
        snap = svc.snapshot("jack", with_token=True)
        by = {c["label"]: c["n"] for c in snap["controls"]}
        with pytest.raises(LiveError) as e:
            svc.act("jack", {"action": "press", "n": by["Search"]}, token=snap["token"])
        assert e.value.message == COVERED
        time.sleep(0.3)
        assert main("[window.pressed || 0, window.framed]") == [0, 0]
        main("document.querySelector('iframe').remove()")
        # the same frame with pointer-events:none, which elementFromPoint skips and a hover
        # handler could switch back on: still refused, and nothing is clicked
        main("var f = document.createElement('iframe'); f.src = 'data:text/html," + frame + "';"
             "f.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;border:0;"
             "opacity:0;pointer-events:none'; document.body.appendChild(f); 1")
        time.sleep(0.5)
        snap = svc.snapshot("jack", with_token=True)
        by = {c["label"]: c["n"] for c in snap["controls"]}
        with pytest.raises(LiveError) as e:
            svc.act("jack", {"action": "press", "n": by["Search"]}, token=snap["token"])
        assert e.value.message == COVERED
        time.sleep(0.3)
        assert main("[window.pressed || 0, window.framed]") == [0, 0]
        main("document.querySelector('iframe').remove()")
        snap = svc.snapshot("jack", with_token=True)
        by = {c["label"]: c["n"] for c in snap["controls"]}
        with pytest.raises(LiveError) as e:  # nearly transparent through its parent
            svc.act("jack", {"action": "press", "n": by["Faint"]}, token=snap["token"])
        assert e.value.message == COVERED
        assert svc.act("jack", {"action": "press", "n": by["Search"]},
                       token=snap["token"])["done"] == "press"
        assert wait_for(lambda: main("window.pressed || 0") == 1, 3)
    finally:
        svc.shutdown()
        local.shutdown()
        raw.close()
    if browser is not None and browser.pid is not None:
        assert wait_for(lambda: children.identity(browser.pid) is None, 5)


def test_act_type_refused_when_the_page_swaps_in_a_password_field(snapped):
    """The page turns the field into a password field after the recheck: the in-page insert
    (check and typing in one step) refuses, and nothing is typed."""
    svc, cdp, _ = snapped
    cdp.insert_ok = False
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "type", "n": 2, "text": "Bodiam"})
    assert e.value.status == 409
    assert cdp.inserted == [] and not cdp.named("Input.insertText")


def test_insert_op_checks_and_types_in_one_step():
    from primer.live.service import SNAPSHOT_JS
    body = SNAPSHOT_JS[SNAPSHOT_JS.index('if (op === "insert")'):]
    body = body[:body.index("return JSON.stringify({ok: true});")]
    assert body.index("isPassword(el2)") < body.index("setter.call")
    assert "await" not in body and "setTimeout" not in body and "then(" not in body


# --- fix round 1 (Task 4 review): UTF-8 input size, orphan windows, nav events, text, resize ---

def test_input_size_is_measured_as_utf8(opened):
    svc, cdp = opened
    svc.input("w1", {"kind": "text", "text": "é" * 700})  # 1.4 KB in UTF-8, 4.2 KB escaped
    assert cdp.named("Input.insertText", "S1")[-1] == {"text": "é" * 700}
    svc.input("w1", {"kind": "text", "text": "中" * 1000})  # 3 KB in UTF-8
    with pytest.raises(LiveError) as e:
        svc.input("w1", {"kind": "text", "text": "中" * 900 + "x" * 1400})
    assert e.value.status == 413
    with pytest.raises(LiveError) as e:
        svc.input("w1", {"kind": "text", "text": "\ud800"})  # a lone surrogate isn't UTF-8
    assert e.value.status == 400


def test_a_window_nobody_reads_is_closed_after_30_s(make):
    now = [1000.0]
    svc = make(clock=lambda: now[0])
    ended = []
    svc.on_window_end = lambda name, gen, reason: ended.append((name, reason))
    svc.open("w1", "https://public.example/", 800, 600)
    svc.open("w2", "https://public.example/", 800, 600)
    it = svc.frames("w2", keepalive=0.05)
    assert next(it) == {"ping": True}  # w2 has a reader now
    now[0] += 29
    assert svc.sweep_orphans() == []
    now[0] += 2
    assert svc.tick() is False  # w1 never had a reader: closed; w2 is still read
    assert svc.status()["windows"] == ["w2"] and ended == [("w1", "closed")]
    assert {"targetId": "T1"} in cdp_of(svc).named("Target.closeTarget")
    it.close()  # its reader went (a reload): the 30 s start now
    now[0] += 20
    assert svc.sweep_orphans() == []
    again = svc.frames("w2", keepalive=0.05)
    assert next(again) == {"ping": True}  # came back in time
    now[0] += 100
    assert svc.sweep_orphans() == []
    again.close()
    now[0] += 31
    assert svc.sweep_orphans() == ["w2"] and svc.status()["windows"] == []


def test_two_readers_count_and_a_closed_window_is_not_swept(make):
    now = [0.0]
    svc = make(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    a, b = svc.frames("w1", keepalive=0.05), svc.frames("w1", keepalive=0.05)
    next(a), next(b)
    a.close()
    now[0] += 60
    assert svc.sweep_orphans() == []  # b still reads
    b.close()
    svc.close("w1")
    now[0] += 60
    assert svc.sweep_orphans() == []


def test_navigation_and_title_changes_are_sent_as_nav(opened):
    svc, cdp = opened
    it = svc.frames("w1", keepalive=0.05)
    assert next(it) == {"ping": True}
    info = {"targetId": "T1", "type": "page", "url": "https://public.example/visit", "title": "Visit us"}
    cdp.fire("Target.targetInfoChanged", {"targetInfo": info})
    assert next(it) == {"nav": {"url": "https://public.example/visit", "title": "Visit us"}}
    cdp.fire("Target.targetInfoChanged", {"targetInfo": info})  # nothing changed: nothing sent
    assert next(it) == {"ping": True}
    # a title that is only the address (still loading) is no title
    cdp.fire("Target.targetInfoChanged", {"targetInfo": {**info, "url": "https://public.example/b",
                                                         "title": "https://public.example/b"}})
    assert next(it) == {"nav": {"url": "https://public.example/b", "title": ""}}
    cdp.fire("Target.targetInfoChanged", {"targetInfo": {**info, "targetId": "T9", "url": "https://x.example/"}})
    assert next(it) == {"ping": True}  # not one of our windows
    late = svc.frames("w1", keepalive=0.05)
    assert next(late) == {"nav": {"url": "https://public.example/b", "title": ""}}  # a new reader hears it


def test_read_text_reads_without_renumbering_controls(opened):
    svc, cdp = opened
    cdp.snapshot = {"title": "Bodiam", "url": "https://public.example/b", "text": "Open 10 to 5.",
                    "controls": [{"n": 1, "kind": "button", "label": "Go"}]}
    snap = svc.snapshot("w1", with_token=True)
    w = svc._windows["w1"]
    token, controls = w.token, dict(w.controls)
    assert svc.read_text("w1") == {"title": "Bodiam", "url": "https://public.example/b", "text": "Open 10 to 5."}
    assert cdp.texts == [200_000]
    assert w.token == token == snap["token"] and w.controls == controls
    with pytest.raises(LiveError) as e:
        svc.read_text("w9")
    assert e.value.status == 404


def test_resize_sets_the_viewport_and_restarts_the_screencast(opened):
    svc, cdp = opened
    svc.resize("w1", 1024, 700)
    assert cdp.named("Emulation.setDeviceMetricsOverride", "S1")[-1] == {
        "width": 1024, "height": 700, "deviceScaleFactor": 1, "mobile": False}
    assert cdp.named("Page.stopScreencast", "S1")
    last = cdp.named("Page.startScreencast", "S1")[-1]
    assert (last["maxWidth"], last["maxHeight"]) == (1024, 700)
    n = len(cdp.named("Emulation.setDeviceMetricsOverride", "S1"))
    svc.resize("w1", 1024, 700)  # the same size: nothing to do
    assert len(cdp.named("Emulation.setDeviceMetricsOverride", "S1")) == n
    with pytest.raises(LiveError) as e:
        svc.resize("w1", "big", 700)
    assert e.value.status == 400


def test_snapshot_waits_for_the_page_to_load(opened):
    svc, cdp = opened
    states = iter(["loading", "loading", "interactive", "complete"])
    plain = cdp.handlers["Runtime.evaluate"]

    def evaluate(params, session):
        if params.get("expression") == STATE_JS and params.get("contextId") == 7:
            return {"result": {"type": "string", "value": next(states, "complete")}}
        return plain(params, session)
    cdp.handlers["Runtime.evaluate"] = evaluate
    svc.snapshot("w1")
    asked = [p for p in cdp.named("Runtime.evaluate", "S1") if p.get("expression") == STATE_JS]
    assert all(p.get("contextId") == 7 for p in asked)  # Primer's world, not the page's
    assert len(asked) == 4  # it asked until the page said complete, then read it


def test_snapshot_says_a_field_holds_text_but_never_what(opened):
    svc, cdp = opened
    cdp.snapshot = {"title": "T", "url": "https://public.example/", "text": "", "controls": [
        {"n": 1, "kind": "field", "label": "Search", "filled": True, "value": "Bodiam"},
        {"n": 2, "kind": "field", "label": "Password", "password": True, "filled": True},
        {"n": 3, "kind": "button", "label": "Go", "filled": True}]}
    snap = svc.snapshot("w1")
    assert snap["controls"] == [
        {"n": 1, "kind": "field", "label": "Search", "filled": True},
        {"n": 2, "kind": "field", "label": "Password", "password": True},
        {"n": 3, "kind": "button", "label": "Go"}]


def test_press_waits_for_the_navigation_to_begin(snapped):
    svc, cdp, _ = snapped
    seen = iter([["https://public.example/", "complete"], ["https://public.example/", "complete"],
                 ["https://public.example/results", "loading"]])
    plain = cdp.handlers["Runtime.evaluate"]

    def evaluate(params, session):
        if params.get("expression") == HREF_JS and params.get("contextId") == 7:
            return {"result": {"type": "object", "value": next(seen, ["https://public.example/results", "complete"])}}
        return plain(params, session)
    cdp.handlers["Runtime.evaluate"] = evaluate
    assert svc.act("w1", {"action": "press", "n": 5})["done"] == "press"
    asked = [p for p in cdp.named("Runtime.evaluate", "S1") if p.get("expression") == HREF_JS]
    assert all(p.get("contextId") == 7 for p in asked)
    assert len(asked) == 3  # the address before, then until the new page started loading


# --- final review: presses can't be clickjacked, reads in Primer's world, hidden characters ---

def _aims(cdp):
    return [json.loads("[" + p["expression"].rsplit(")(", 1)[1].rstrip(")") + "]")
            for p in cdp.named("Runtime.evaluate", "S1")
            if "contextId" in p and p["expression"] not in (HREF_JS, STATE_JS)
            and '("aim"' in p["expression"]]


def test_press_aims_in_one_step_and_clicks_there(snapped):
    svc, cdp, _ = snapped
    cdp.found[5] = dict(CONTROLS[4], ok=True, x=120, y=60)
    svc.act("w1", {"action": "press", "n": 5})
    assert [a[0] for a in _aims(cdp)] == ["aim"]
    m = cdp.named("Input.dispatchMouseEvent", "S1")
    assert all((p["x"], p["y"]) == (120, 60) for p in m) and len(m) == 2


def test_press_refused_when_something_covers_the_control(snapped):
    """A transparent frame from another site laid over the button: the aim's hit test finds
    the frame, not the button, so the press is refused and nothing is clicked."""
    svc, cdp, _ = snapped
    cdp.aims = [{"clear": False}]
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5})
    assert e.value.status == 409 and e.value.message == COVERED
    assert not cdp.named("Input.dispatchMouseEvent")


def test_press_refused_when_an_overlay_comes_with_the_scroll(snapped):
    """Bringing the button into view scrolled the page; the page moved an overlay in on that
    scroll. The second aim sees it, and nothing is clicked."""
    svc, cdp, _ = snapped
    cdp.aims = [{"moved": True}, {"clear": False}]
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5})
    assert e.value.message == COVERED and len(_aims(cdp)) == 2
    assert not cdp.named("Input.dispatchMouseEvent")
    cdp.aims = [{"moved": True}, {"moved": True}]  # it keeps moving: refused too
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5})
    assert e.value.message == COVERED and not cdp.named("Input.dispatchMouseEvent")
    cdp.aims = [{"moved": True}, {}]  # still there and clear the second time: pressed
    svc.act("w1", {"action": "press", "n": 5})
    assert len(cdp.named("Input.dispatchMouseEvent", "S1")) == 2


def test_press_refused_when_the_aim_finds_a_different_control(snapped):
    svc, cdp, _ = snapped
    cdp.aims = [dict(CONTROLS[1], ok=True, x=100, y=50)]
    with pytest.raises(LiveError) as e:
        svc.act("w1", {"action": "press", "n": 5})
    assert e.value.status == 409 and not cdp.named("Input.dispatchMouseEvent")


def test_aim_op_checks_and_reads_the_point_in_one_step():
    from primer.live.service import SNAPSHOT_JS
    body = SNAPSHOT_JS[SNAPSHOT_JS.index('if (op === "find" || op === "aim")'):]
    body = body[:body.index('if (op === "insert")')]
    assert body.index("scrollIntoView") < body.index("getBoundingClientRect()", body.index("scrollIntoView"))
    assert "shown(el2)" in body and "hits(el2, out.x, out.y)" in body
    assert "await" not in body and "setTimeout" not in body and "then(" not in body
    hits = SNAPSHOT_JS[SNAPSHOT_JS.index("function hits("):]
    hits = hits[:hits.index("\n  }\n")]
    assert "elementFromPoint" in hits and "IFRAME" in hits
    shown = SNAPSHOT_JS[SNAPSHOT_JS.index("function shown("):]
    shown = shown[:shown.index("\n  }\n")]
    for word in ("display", "visibility", "opacity", "width", "parentElement", "0.1"):
        assert word in shown


def test_href_and_settle_read_in_primers_world(snapped):
    svc, cdp, _ = snapped
    svc.act("w1", {"action": "press", "n": 5})
    svc.snapshot("w1")
    peeks = [p for p in cdp.named("Runtime.evaluate", "S1") if p["expression"] in (HREF_JS, STATE_JS)]
    assert {p["expression"] for p in peeks} == {HREF_JS, STATE_JS}
    assert all(p.get("contextId") == 7 for p in peeks)
    assert not [p for p in cdp.named("Runtime.evaluate", "S1") if "contextId" not in p]
    # what they read is capped in the page, so a huge value can't fill the connection
    assert ".slice(0, 8192)" in HREF_JS and ".slice(0, 16)" in HREF_JS
    assert ".slice(0, 16)" in STATE_JS


def test_href_makes_a_new_world_when_the_old_one_went(opened):
    svc, cdp = opened
    w = svc._windows["w1"]
    worlds = itertools.count(7)
    cdp.handlers["Page.createIsolatedWorld"] = lambda p, s: {"executionContextId": next(worlds)}
    plain = cdp.handlers["Runtime.evaluate"]

    def evaluate(params, session):
        if params.get("expression") == HREF_JS:
            if params.get("contextId") == 7:
                raise CdpError("Cannot find context with specified id")
            return {"result": {"value": ["https://public.example/next", "loading"]}}
        return plain(params, session)
    cdp.handlers["Runtime.evaluate"] = evaluate
    w.world = None
    assert svc._href(w) == ("https://public.example/next", "loading")
    assert w.world == 8

    def dead(params, session):
        raise CdpError("gone")
    cdp.handlers["Runtime.evaluate"] = dead
    assert svc._href(w) is None  # unknown: the caller stops waiting
    t0 = time.monotonic()
    svc._settle(w)  # can't read the state: no wait
    assert time.monotonic() - t0 < 1


def test_labels_lose_hidden_characters(opened):
    svc, cdp = opened
    cdp.snapshot = {"title": "T", "url": "https://public.example/", "text": "", "controls": [
        {"n": 1, "kind": "button", "label": "Pay \u202emoc.elpmaxe\u202c now\u200b"},
        {"n": 2, "kind": "list", "label": "\u2066Day\u2069", "options": ["Sun\ufeffday"]}]}
    snap = svc.snapshot("w1")
    assert snap["controls"] == [
        {"n": 1, "kind": "button", "label": "Pay moc.elpmaxe now"},
        {"n": 2, "kind": "list", "label": "Day", "options": ["Sunday"]}]


def test_plain_text():
    from primer.live.service import plain_text
    assert plain_text("a\u202eb\u202c c\u200b\u200d\u200e\u200f\ufeff\u2066d\u2069") == "ab cd"
    assert plain_text(" x\t\ny\x07\x85z ") == "x y z"
    assert plain_text("Bodiam Castle") == "Bodiam Castle"


def test_act_refuses_text_with_hidden_characters(snapped):
    svc, cdp, _ = snapped
    for bad in ("moc\u202e.evil", "a\u200bb", "line\x07"):
        with pytest.raises(LiveError) as e:
            svc.act("w1", {"action": "type", "n": 2, "text": bad})
        assert e.value.status == 400
    assert cdp.inserted == []


def test_snapshot_js_cleans_hidden_characters_from_labels():
    from primer.live.service import SNAPSHOT_JS
    clean = SNAPSHOT_JS[SNAPSHOT_JS.index("function clean("):]
    clean = clean[:clean.index("\n  }\n")]
    assert "HIDDEN" in clean and "GAPS" in clean
    assert r"/\p{Cf}/gu" in SNAPSHOT_JS and r"\p{Cc}" in SNAPSHOT_JS


def test_press_sends_no_hover_move_first(snapped):
    """A hover move would fire mouseover on the control and tell the page when to slip a
    frame over it: the press is a bare press and release."""
    svc, cdp, _ = snapped
    svc.act("w1", {"action": "press", "n": 5})
    kinds = [p["type"] for p in cdp.named("Input.dispatchMouseEvent", "S1")]
    assert kinds == ["mousePressed", "mouseReleased"]


# --- Phase 17: the browser's parked items ------------------------------------------------------

def test_service_worker_scripts_are_refused(make):
    """A paused service worker doesn't always answer before it runs (checked against Chrome
    158), so its set-up can't be checked before it runs: registration is blocked instead. The
    script request every registration and update makes carries Service-Worker: script."""
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    cdp = cdp_of(svc)
    for i, headers in enumerate(({"Service-Worker": "script"}, {"service-worker": "script"})):
        cdp.fire("Fetch.requestPaused", {"requestId": f"sw{i}", "request": {
            "url": "https://public.example/sw.js", "headers": headers}})
    cdp.fire("Fetch.requestPaused", {"requestId": "page", "request": {
        "url": "https://public.example/app.js", "headers": {"Accept": "*/*"}}})
    assert wait_for(lambda: len(cdp.named("Fetch.failRequest")) == 2
                    and cdp.named("Fetch.continueRequest"))
    assert {p["requestId"] for p in cdp.named("Fetch.failRequest")} == {"sw0", "sw1"}
    assert cdp.named("Fetch.continueRequest") == [{"requestId": "page"}]


def test_no_sockets_js_refuses_service_worker_registration():
    body = NO_SOCKETS_JS[:NO_SOCKETS_JS.index("lock(G);")]
    assert "ServiceWorkerContainer" in body and '"register"' in body
    assert "Promise" in NO_SOCKETS_JS[:NO_SOCKETS_JS.index("const lock")]  # captured first


def test_service_workers_left_in_the_profile_are_removed_before_start(make, tmp_path):
    sw = tmp_path / "var" / "browser" / "Default" / "Service Worker"
    (sw / "ScriptCache").mkdir(parents=True)
    (sw / "ScriptCache" / "x").write_text("old")
    cookies = tmp_path / "var" / "browser" / "Default" / "Network"
    cookies.mkdir(parents=True)
    (cookies / "Cookies").write_text("sign-ins")
    svc = make()
    svc.open("w1", "https://public.example/", 800, 600)
    assert not sw.exists()
    assert (cookies / "Cookies").read_text() == "sign-ins"  # sign-ins stay


def test_worker_set_up_has_its_own_pool(make):
    """Workers whose set-up hangs (each step can wait WORKER_PAUSE_S) never hold the pool
    that sets up pages and frames."""
    gate = threading.Event()
    svc = make()
    try:
        svc.open("w1", "https://public.example/", 800, 600)
        cdp = cdp_of(svc)

        def hang(p, s):
            if s.startswith("SWH"):
                gate.wait(10)
            return {}
        cdp.handlers["Debugger.enable"] = hang
        for i in range(8):  # more than the DevTools pool has threads
            cdp.fire("Target.attachedToTarget", {"sessionId": f"SWH{i}", "waitingForDebugger": True,
                                                 "targetInfo": {"type": "worker",
                                                                "targetId": f"XH{i}"}})
        time.sleep(0.1)
        cdp.fire("Target.attachedToTarget", {"sessionId": "IFP", "waitingForDebugger": True,
                                             "targetInfo": {"type": "iframe", "targetId": "XP"}}, "S1")
        assert wait_for(lambda: "Runtime.runIfWaitingForDebugger" in cdp.methods("IFP"), 1.0)
    finally:
        gate.set()


def test_a_stuck_lookup_frees_its_slot(make):
    """A lookup that never returns is given up after the timeout and frees its slot, so
    later lookups still run (they used to queue behind 16 stuck ones and be refused)."""
    gate = threading.Event()

    def resolve(host, port, *a, **k):
        if host.endswith("stuck.example"):
            gate.wait(10)
        return fake_resolve(host, port)
    svc = make(resolve=resolve, dns_timeout=0.2)
    try:
        for i in range(20):  # more than there are slots
            with pytest.raises(OSError):
                svc._timed_resolve(f"h{i}.stuck.example", 443)
        assert svc._timed_resolve("public.example", 443)[0][4][0] == "93.184.215.14"
    finally:
        gate.set()


def test_running_lookups_are_capped(make, monkeypatch):
    from primer.live import service as service_mod
    monkeypatch.setattr(service_mod, "MAX_DNS_THREADS", 5)
    gate = threading.Event()
    started = []

    def stuck(host, port, *a, **k):
        started.append(host)
        gate.wait(10)
        return fake_resolve("public.example", port)
    svc = make(resolve=stuck, dns_timeout=0.1)
    try:
        for i in range(8):
            with pytest.raises(OSError):
                svc._timed_resolve(f"h{i}.example", None)
        assert len(started) == 5  # the rest were refused without a thread
    finally:
        gate.set()
    assert wait_for(lambda: svc._dns_threads == 0 and svc._dns_backlog == 0)


def test_pool_threads_are_daemons_and_shutdown_is_bounded(make):
    gate = threading.Event()

    def stuck(host, port, *a, **k):
        if host == "x.example":
            gate.wait(30)
        return fake_resolve("public.example", port)
    svc = make(resolve=stuck, dns_timeout=5)
    try:
        svc.open("w1", "https://public.example/", 800, 600)
        cdp = cdp_of(svc)
        cdp.fire("Fetch.requestPaused", {"requestId": "s", "request": {"url": "http://x.example/"}})
        assert wait_for(lambda: svc._dns_threads == 1)
        live = [t for t in threading.enumerate() if t.name.startswith("live-")]
        assert live and all(t.daemon for t in live)
        t0 = time.monotonic()
        svc.shutdown()
        assert time.monotonic() - t0 < 3
    finally:
        gate.set()


def test_primers_stop_does_not_wait_on_a_stuck_lookup(tmp_path):
    """A process whose live service has a lookup that never returns still exits."""
    import subprocess
    import sys
    code = (
        "import threading, sys\n"
        "from types import SimpleNamespace as NS\n"
        "from primer.live.service import LiveService\n"
        "def stuck(*a, **k):\n"
        "    threading.Event().wait()\n"
        "svc = LiveService(sys.argv[1], NS(web=NS(search='duckduckgo'), browser=NS(path='')),\n"
        "                  resolve=stuck, dns_timeout=0.2, start_threads=False)\n"
        "try:\n"
        "    svc._timed_resolve('stuck.example', 443)\n"
        "except OSError:\n"
        "    pass\n"
        "svc._submit(svc._timed_resolve, 'stuck2.example', 443, pool=svc._guard_pool)\n"
        "svc.shutdown()\n"
        "print('stopped', flush=True)\n")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path / "var")], cwd=root,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and "stopped" in p.stdout, p.stderr


def test_resize_takes_the_windows_lock(opened):
    """Two resizes at once run one after the other, so the viewport and the screencast
    never disagree."""
    svc, cdp = opened
    first_in, release = threading.Event(), threading.Event()

    def metrics(p, s):
        if p["width"] == 1000 and not first_in.is_set():
            first_in.set()
            release.wait(5)
        return {}
    cdp.handlers["Emulation.setDeviceMetricsOverride"] = metrics
    a = threading.Thread(target=svc.resize, args=("w1", 1000, 700))
    a.start()
    assert first_in.wait(3)
    b = threading.Thread(target=svc.resize, args=("w1", 1200, 800))
    b.start()
    time.sleep(0.2)  # b would call in now if nothing held it back
    release.set()
    a.join(5), b.join(5)
    seq = [(m, p.get("width", p.get("maxWidth"))) for m, p, s in cdp.calls
           if s == "S1" and m in ("Emulation.setDeviceMetricsOverride", "Page.startScreencast")]
    assert seq[-4:] == [("Emulation.setDeviceMetricsOverride", 1000), ("Page.startScreencast", 1000),
                        ("Emulation.setDeviceMetricsOverride", 1200), ("Page.startScreencast", 1200)]
    w = svc._windows["w1"]
    assert (w.width, w.height) == (1200, 800)


def test_the_orphan_sweep_rechecks_under_the_lock(make):
    """A reader that comes between the sweep's look and its close keeps the window open."""
    now = [0.0]
    svc = make(clock=lambda: now[0])
    svc.open("w1", "https://public.example/", 800, 600)
    w = svc._windows["w1"]
    reads = []

    class Watched(type(w)):
        @property
        def readers(self):
            reads.append(1)
            return 0 if len(reads) == 1 else 1  # a reader arrives just after the first look

        @readers.setter
        def readers(self, v):
            pass
    w.__class__ = Watched
    now[0] += 60
    assert svc.sweep_orphans() == []
    assert svc.status()["windows"] == ["w1"] and w.gone is None
    assert {"targetId": "T1"} not in cdp_of(svc).named("Target.closeTarget")


def test_a_changed_browser_path_applies_on_a_tier_switch(make):
    looked = []

    def find(cfg):
        looked.append(cfg)
        return (cfg or "C:/Chrome/chrome.exe", "Edge 158" if "Edge" in cfg else "Chrome 158")
    svc = make(tier(path=""), find=find)
    assert svc.status()["label"] == "Chrome 158"
    svc.tier = tier(path="C:/Edge/msedge.exe")
    assert svc.status()["label"] == "Edge 158"
    assert looked == ["", "C:/Edge/msedge.exe"]
    svc.tier = tier(path="C:/Edge/msedge.exe")  # the same path: no new look
    svc.status()
    assert looked == ["", "C:/Edge/msedge.exe"]


def test_shown_checks_filter_clip_mask_and_scale():
    from primer.live.service import SNAPSHOT_JS
    shown = SNAPSHOT_JS[SNAPSHOT_JS.index("function shown("):]
    shown = shown[:shown.index("\n  }\n")]
    for word in ("filterAlpha(", "clipsAll(", "masksAll(", "scaleOf(", "0.05"):
        assert word in shown


HIDDEN_WAYS = {
    "Filtered": "<div style='filter:opacity(0)'>{b}</div>",
    "Filtered half twice": "<div style='filter:opacity(0.3)'><div style='filter:opacity(30%)'>"
                           "{b}</div></div>",
    "Inset": "<div style='clip-path:inset(50%)'>{b}</div>",
    "Circle": "<div style='clip-path:circle(0px at 50% 50%)'>{b}</div>",
    "Polygon": "<div style='clip-path:polygon(10px 10px, 10px 10px, 10px 10px)'>{b}</div>",
    "Line": "<div style='clip-path:polygon(0% 0%, 50% 50%, 100% 100%)'>{b}</div>",
    "Masked": "<div style='mask-image:linear-gradient(transparent, rgba(0,0,0,0));"
              "-webkit-mask-image:linear-gradient(transparent, rgba(0,0,0,0))'>{b}</div>",
    "Mask sized 0": "<div style='mask-image:linear-gradient(black, black);mask-size:0 0;"
                    "-webkit-mask-image:linear-gradient(black, black);-webkit-mask-size:0 0'>"
                    "{b}</div>",
    "Tiny": "<div style='transform:scale(0.2)'><div style='transform:scale(0.2)'>{b}</div></div>",
    "Shrunk": "<div style='scale:0.02'>{b}</div>",
}
SHOWN_WAYS = {
    "Half size": "<div style='transform:scale(0.5)'>{b}</div>",
    "Icon mask": "<div style='mask-image:linear-gradient(black, transparent);"
                 "-webkit-mask-image:linear-gradient(black, transparent)'>{b}</div>",
    "Rounded": "<div style='clip-path:inset(10% round 8px)'>{b}</div>",
    # self-crossing, so its signed area is 0, but the centre is inside and shows
    "Bowtie": "<div style='clip-path:polygon(0% 0%, 0% 75%, 100% 0%, 50% 75%)'>{b}</div>",
}


@pytest.mark.skipif(os.environ.get("PRIMER_LIVE_CHROME") != "1",
                    reason="set PRIMER_LIVE_CHROME=1 to run against the installed browser")
def test_real_browser_press_refuses_controls_hidden_by_filter_clip_mask_or_scale(tmp_path):
    if find_browser() is None:
        pytest.skip("no Chrome or Edge installed")
    rows = []
    for i, (label, wrap) in enumerate({**HIDDEN_WAYS, **SHOWN_WAYS}.items()):
        button = (f"<button style='width:200px;height:60px' onclick='window.pressed.push("
                  f"&quot;{label}&quot;)'>{label}</button>")
        rows.append(f"<div style='position:absolute;left:150px;top:{40 + i * 90}px'>"
                    + wrap.format(b=button) + "</div>")
    page = urllib.parse.quote("<html><body style='margin:0;height:3000px'><script>"
                              "window.pressed = []</script>" + "".join(rows) + "</body></html>")
    svc = LiveService(tmp_path / "var", tier())
    try:
        svc.open("vis", "data:text/html," + page, 640, 480)
        w = svc._windows["vis"]

        def main(expr):
            return w.cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True},
                              session=w.session)["result"].get("value")
        assert wait_for(lambda: main("document.readyState") == "complete", 10)
        snap = svc.snapshot("vis", with_token=True)
        by = {c["label"]: c["n"] for c in snap["controls"]}
        refused = []
        for label in HIDDEN_WAYS:
            if label not in by:
                continue  # not even listed: never pressed
            try:
                svc.act("vis", {"action": "press", "n": by[label]}, token=snap["token"])
            except LiveError as e:
                assert e.message == COVERED, (label, e.message)
                refused.append(label)
        for label in SHOWN_WAYS:
            assert svc.act("vis", {"action": "press", "n": by[label]},
                           token=snap["token"])["done"] == "press", label
        time.sleep(0.3)
        assert main("window.pressed") == list(SHOWN_WAYS)
        assert refused == [k for k in HIDDEN_WAYS if k in by]
    finally:
        svc.shutdown()


# --- Phase 17 fix round 1 ------------------------------------------------------------------------

def test_a_new_tabs_first_size_is_set_under_the_windows_lock(make):
    """A resize that comes while a new tab gets its first size waits for it, so the viewport
    and the screencast end at the size the window says."""
    svc = make()
    first_in, release = threading.Event(), threading.Event()
    seen = []

    def metrics(p, s):
        if p["width"] == 800 and not first_in.is_set():
            first_in.set()
            release.wait(5)
        return {}

    real_ensure = svc._ensure

    def ensure():
        cdp = real_ensure()
        cdp.handlers["Emulation.setDeviceMetricsOverride"] = metrics
        seen.append(cdp)
        return cdp
    svc._ensure = ensure
    opener = threading.Thread(target=svc.open, args=("w1", "https://public.example/", 800, 600))
    opener.start()
    assert first_in.wait(3)
    resizer = threading.Thread(target=svc.resize, args=("w1", 1000, 700))
    resizer.start()
    time.sleep(0.2)  # the resize would call in now if nothing held it back
    release.set()
    opener.join(5), resizer.join(5)
    cdp = seen[0]
    seq = [(m, p.get("width", p.get("maxWidth"))) for m, p, s in cdp.calls
           if s == "S1" and m in ("Emulation.setDeviceMetricsOverride", "Page.startScreencast")]
    assert seq == [("Emulation.setDeviceMetricsOverride", 800), ("Page.startScreencast", 800),
                   ("Emulation.setDeviceMetricsOverride", 1000), ("Page.startScreencast", 1000)]
    assert (svc._windows["w1"].width, svc._windows["w1"].height) == (1000, 700)


def test_status_shows_the_running_browser_until_it_stops(make):
    def find(cfg):
        return (cfg or "C:/Chrome/chrome.exe", "Edge 158" if "Edge" in cfg else "Chrome 158")
    svc = make(tier(path=""), find=find)
    svc.open("w1", "https://public.example/", 800, 600)
    svc.tier = tier(path="C:/Edge/msedge.exe")
    assert svc.status()["label"] == "Chrome 158"  # the browser still running
    assert svc.status()["windows"] == ["w1"]
    svc.forget()  # it stops: the new path is looked for
    assert svc.status()["label"] == "Edge 158"
    svc.open("w2", "https://public.example/", 800, 600)
    assert FakeBrowser.made[-1].path == "C:/Edge/msedge.exe"
