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
from primer.live.service import (COVERED, HREF_JS, NO_SOCKETS_JS, STATE_JS, LiveError,
                                 LiveService, _jpeg_size)

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

    def _create(self, params, session):
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

    def __init__(self, path, profile_dir):
        self.path, self.profile_dir = path, profile_dir
        self.cdp = FakeCdp()
        self.stopped = False
        self.profile_at_stop = None
        FakeBrowser.made.append(self)

    def start(self):
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
    cmd = Browser("chrome", tmp_path / "browser").command()
    assert cmd[1:] == ["--headless=new", "--remote-debugging-port=0",
                       f"--user-data-dir={tmp_path / 'browser'}", "--no-first-run",
                       "--no-default-browser-check",
                       "--disable-features=DocumentPictureInPictureAPI"]
    assert not any("remote-allow-origins" in a for a in cmd)


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
    b.start()
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
    b.start()
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
    b.start()
    b.stop()
    assert table.killed == [200]
    assert table.procs[400].alive


def test_stop_never_kills_a_process_whose_creation_time_differs(tmp_path, monkeypatch):
    table = FakeTable()
    table.add(200, EXE, NOW + 5, _good_cmd(tmp_path / "browser"))
    b, *_ = _browser(tmp_path, monkeypatch, table, browser_exits_on_close=False)
    b.start()
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
        b.start()


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
        assert cdp.named("Page.setDownloadBehavior", f"S{i + 1}") == [{"behavior": "deny"}]
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


def test_dns_backlog_is_capped_and_timeouts_cancel(make):
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
        for i in range(20):  # 16 run (and stick), the rest queue, then are cancelled
            with pytest.raises(OSError):
                svc._timed_resolve(f"h{i}.example", None)
        time.sleep(0.2)
        assert len(started) == 16
    finally:
        gate.set()
    assert wait_for(lambda: svc._dns_backlog == 0, 3)
    assert len(started) == 16  # the cancelled ones never ran


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
    assert wait_for(lambda: len(cdp.named("Target.closeTarget")) == 2)
    sweepers = [t for t in threading.enumerate() if t.name == "live-sweep"]
    assert sweepers and all(t.daemon for t in sweepers)  # one sweeper, not a thread a pop-up
    assert svc._sweeper in sweepers
    now[0] += 6  # the address never came
    assert wait_for(lambda: len(cdp.named("Target.closeTarget")) == 10, 3)


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
    raw.bind(("127.0.0.1", 0))
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
        r = w.cdp.call("Runtime.evaluate", {
            "expression": f"try {{ new WebSocket('ws://127.0.0.1:{port}/'); 'made' }} "
                          "catch (e) { 'refused' }", "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "refused"
        # a page that tampers with the builtins first still can't get a pop-up's first
        # document (window.open returns null: pop-ups open in the same view) or a socket
        tamper = ("Object.defineProperty = function () {}; Reflect.apply = function () "
                  "{ return window; }; Object.prototype.get = function () {}; "
                  "Function.prototype.call = function () {}; ")
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(() => { " + tamper + "const p = window.open(''); "
                          "const q = document.open('', 'x', ''); "
                          "if (p || q) { try { new (p || q).WebSocket("
                          f"'ws://127.0.0.1:{port}/'); return 'made' }} catch (e) {{ "
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
            "expression": "(() => { " + tamper + "try { new WebSocket("
                          f"'ws://127.0.0.1:{port}/'); return 'made' }} "
                          "catch (e) { return 'refused' } })()",
            "returnByValue": True}, session=w.session)
        assert r["result"]["value"] == "refused"
        r = w.cdp.call("Runtime.evaluate", {
            "expression": "(() => { const f = document.createElement('iframe'); "
                          "document.body.appendChild(f); "
                          f"try {{ new f.contentWindow.WebSocket('ws://127.0.0.1:{port}/'); "
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
        svc.open("jack", "data:text/html," + page, 640, 480)
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
