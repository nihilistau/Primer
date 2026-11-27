"""The live browser service: up to 4 live windows (one tab each) in one headless Chrome or Edge.

Safety, for every target the browser makes (tabs, pop-ups, out-of-process iframes, workers).
What was checked against Chrome 158 is said so; the rest is how the protocol is documented.
- Fetch.enable on the *browser* session pauses every request: checked for pages, OOPIFs,
  dedicated workers and redirect hops. Each paused request goes to the guard pool, which
  resolves the host (on daemon lookup threads, at most DNS_WORKERS at once, refusing after
  DNS_TIMEOUT_S; a lookup given up frees its slot) and continues it or
  fails it with BlockedByClient. Frame acks and target set-up use their own pool, so slow DNS
  never stalls the picture or new tabs.
- Every connection the browser makes goes through the guarding proxy (proxy.py), started
  before the browser and stopped after it: the proxy resolves each host once, refuses unless
  every address is public, and connects to that checked address, so a name that answers
  differently to Chrome (DNS rebinding) can't reach anything local. The Fetch check stays as
  the first layer, with a clear page error; the proxy is the layer that enforces. WebSockets
  go through the proxy too and work. The proxy proves it is in the path (_prove_proxy) at each
  browser start, again before each new live window, and at most once every REPROBE_S while
  windows are open (Chrome applies a proxy policy change at once); a miss stops the browser.
- WebTransport and WebRTC use UDP, which the proxy can't see, so their constructors are
  replaced, before any script runs, by ones that throw (NO_SOCKETS_JS). Target.setAutoAttach
  with waitForDebuggerOnStart pauses each new target:
  - documents (pages, OOPIFs) get Page.addScriptToEvaluateOnNewDocument, then are resumed;
    checked for the main page, fresh, about:blank and srcdoc iframes. window.open and the
    contentWindow getters are wrapped too, so a pop-up's or frame's first document is locked
    before the opener can touch it;
  - workers are resumed into an instrumentation breakpoint before their first script, get the
    script there, then run on; checked for dedicated, nested dedicated, shared and service
    workers. Their set-up runs on its own pool, so a slow one never holds the others.
  A target whose set-up fails is never resumed.
- Service workers are blocked in live pages. A paused service worker doesn't always answer
  DevTools before it runs (checked against Chrome 158: some answered, some only after being
  resumed), so a failed set-up can't be guaranteed to close it before it runs. Registration
  is refused in the page (NO_SOCKETS_JS), every script request a registration or update makes
  (Service-Worker: script) is failed, and workers left in the profile from earlier versions
  are removed before the browser starts. Sites fall back to the network.
- Pop-ups are opened in their opener's tab; a new page that isn't one of our tabs is closed.
- Downloads are denied (Browser.setDownloadBehavior), every permission is denied
  (Browser.setPermission), dialogs are dismissed, and network prediction (preconnect, DNS
  prefetch) is off in the profile (browser.py).
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import socket
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

from primer.live import guard
from primer.live.browser import Browser, find_browser
from primer.live.cdp import CdpError
from primer.live.proxy import GuardProxy

log = logging.getLogger("primer.live.service")

MAX_WINDOWS = 4
IDLE_S = 600.0  # the browser is closed after 10 minutes with no live window
IDLE_CHECK_S = 30.0
ORPHAN_S = 30.0  # a window with no frames reader this long is closed (its view went away)
MAX_PAGE_TEXT = 200_000  # characters of live page text for Ask and Make a page (the reader's cap)
FRAME_INTERVAL_S = 0.1  # at most 10 frames a second per reader
KEEPALIVE_S = 15.0
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 4096
MAX_TYPED = 1000  # characters in one input post
MAX_ACT_TEXT = 200  # characters the agent may type
MAX_URL = 8192
ATTACH_WAIT_S = 10.0
GUARD_WORKERS = 8
DNS_WORKERS = 16  # lookups running at once; one given up after DNS_TIMEOUT_S frees its slot
MAX_DNS_THREADS = 64  # lookup threads alive in all, given-up ones included
CDP_WORKERS = 4
WORKER_SETUP_WORKERS = 4  # worker set-up (up to 3 x WORKER_PAUSE_S each) has its own pool
POOL_JOIN_S = 1.0  # how long Primer's stop waits for the pools' threads, in all
DNS_TIMEOUT_S = 3.0  # a lookup slower than this is refused
FIND_RETRY_S = 60.0  # look for a browser again this long after finding none
MAX_POPUPS = 8  # blank pop-ups waiting for an address; more are closed at once
POPUP_WAIT_S = 5.0
WORKER_PAUSE_S = 3.0  # a worker must reach its first-script breakpoint within this
MAX_DNS_BACKLOG = 64  # more lookups than this waiting: refuse at once

OFF = "Live pages are off while web search is off"
FULL = "Up to 4 live pages at a time"
NO_BROWSER = "Live pages need Chrome or Edge"
NO_PROXY = ("{label} isn't sending its traffic through Primer's guarding proxy (a browser "
            "proxy policy may be overriding it), so live pages stay off")
PROBE_TIMEOUT_S = 5.0  # for the new browser's probe request to reach the proxy
REPROBE_S = 60.0  # while live windows are open, the proxy is proved again at most this often
BAD_ADDRESS = "That address can't be opened live"
BROWSER_STOPPED = "The browser stopped"
NO_DOWNLOADS = "Primer's browser doesn't download files"
STALE = "The page has changed; read it again"
COVERED = "Something covers that control"
GONE = "That live page has closed"

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_AUTO = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_WORKERS = ("worker", "shared_worker", "service_worker")
_PERMISSIONS = (
    "geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read",
    "clipboard-write", "background-sync", "periodic-background-sync", "background-fetch",
    "persistent-storage", "screen-wake-lock", "idle-detection", "display-capture",
    "window-management", "local-fonts", "storage-access", "top-level-storage-access",
    "payment-handler", "nfc", "accelerometer", "gyroscope", "magnetometer",
    "ambient-light-sensor", "keyboard-lock", "pointer-lock", "speaker-selection",
    "local-network-access", "captured-surface-control",
)
# what _href and _settle read, in Primer's isolated world (the page's own getters on document
# aren't seen there), each capped so a reply stays small
HREF_JS = "[String(location.href).slice(0, 8192), String(document.readyState).slice(0, 16)]"
STATE_JS = "String(document.readyState).slice(0, 16)"
_CONTROL_CHARS = re.compile(r"[\u0000-\u001f\u007f-\u009f]")


def plain_text(s: str) -> str:
    """s without invisible format characters (Unicode Cf: bidi overrides and isolates,
    zero-width marks, the BOM) or control characters, with runs of whitespace as one space.
    What the reader is asked to allow then reads as what it is."""
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return " ".join(_CONTROL_CHARS.sub(" ", s).split())


NO_SOCKETS_JS = """(() => {
  "use strict";
  // Everything used later is captured now, before any page script runs, so a page that
  // replaces Object.defineProperty, Reflect.apply or the like can't switch the lock off.
  // Descriptors have a null prototype, so getters a page adds to Object.prototype can't
  // leak into them; loops are indexed, so a replaced Array iterator does nothing.
  const G = globalThis;
  const defineProperty = Object.defineProperty;
  const getOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
  const apply = Reflect.apply;
  const toString = String;
  const DOMExc = G.DOMException;
  const P = G.Promise;
  const reject = P.reject;
  const NAMES = ["WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection"];
  const refuse = function () {
    throw new DOMExc("Primer's live browser doesn't allow WebRTC or WebTransport", "SecurityError");
  };
  const fixed = (value) => ({__proto__: null, value: value, writable: false,
                             configurable: false});
  // service workers: registration is refused (a worker that registers would run outside
  // Primer's checks); the browser also fails every service-worker script request
  const noWorker = function register() {
    return apply(reject, P, [new DOMExc("Primer's live browser doesn't run service workers",
                                        "SecurityError")]);
  };
  const lock = (g) => {
    for (let i = 0; i < NAMES.length; i++) {
      try {
        if (NAMES[i] in g) defineProperty(g, NAMES[i], fixed(refuse));
      } catch (e) {}  // cross-origin: the caller can't reach its sockets either
    }
    try {
      const C = g.ServiceWorkerContainer;
      if (C && C.prototype) defineProperty(C.prototype, "register", fixed(noWorker));
    } catch (e) {}
  };
  lock(G);
  if (typeof G.Window !== "function" || !(G instanceof G.Window)) return;  // a worker
  // Document Picture-in-Picture would also hand the page a scriptable window: remove it
  // (browser.py also turns the feature off); if it can't be removed, pin a refusing one.
  try { delete G.documentPictureInPicture; } catch (e) {}
  if ("documentPictureInPicture" in G) {
    const no = {__proto__: null, requestWindow: function () {
      return Promise.reject(new DOMExc("Primer's live browser doesn't open windows",
                                       "NotAllowedError"));
    }};
    try { defineProperty(G, "documentPictureInPicture", fixed(no)); } catch (e) {}
  }
  // Pop-ups open in the same view: window.open (and the old three-argument document.open)
  // navigates this tab and returns null, so a page never holds a pop-up's first document.
  // This changes sites that rely on pop-ups (OAuth sign-in windows, ad pop-ups): the
  // address opens in this tab instead, and the opener gets no window back.
  const loc = G.location;
  const assign = loc.assign;
  const go = (url) => {
    if (url === undefined || url === null) return;
    const u = toString(url);
    if (u !== "") { try { apply(assign, loc, [u]); } catch (e) {} }
  };
  try { defineProperty(G, "open", fixed(function open(url) { go(url); return null; })); }
  catch (e) {}
  const docProto = G.Document && G.Document.prototype;
  const docOpen = docProto && docProto.open;
  if (docOpen) {
    try {
      defineProperty(docProto, "open", fixed(function open() {
        if (arguments.length >= 3) { go(arguments[0]); return null; }
        return apply(docOpen, this, arguments);
      }));
    } catch (e) {}
  }
  // defence in depth: a frame's window is locked before the caller gets it
  const FRAMES = [G.HTMLIFrameElement, G.HTMLFrameElement, G.HTMLObjectElement];
  for (let i = 0; i < FRAMES.length; i++) {
    const C = FRAMES[i];
    if (!C) continue;
    const d = getOwnPropertyDescriptor(C.prototype, "contentWindow");
    if (!d || !d.get) continue;
    const getter = d.get;
    try {
      defineProperty(C.prototype, "contentWindow", {__proto__: null, enumerable: d.enumerable,
        configurable: false,
        get: function () { const w = apply(getter, this, []); if (w) lock(w); return w; }});
    } catch (e) {}
  }
})()"""
SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8").strip()

# key name -> (windowsVirtualKeyCode, code, text a keyDown types)
_KEYS = {
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", None), "Backspace": (8, "Backspace", None),
    "Delete": (46, "Delete", None), "Escape": (27, "Escape", None),
    "ArrowLeft": (37, "ArrowLeft", None), "ArrowUp": (38, "ArrowUp", None),
    "ArrowRight": (39, "ArrowRight", None), "ArrowDown": (40, "ArrowDown", None),
    "Home": (36, "Home", None), "End": (35, "End", None), "PageUp": (33, "PageUp", None),
    "PageDown": (34, "PageDown", None), "Shift": (16, "ShiftLeft", None),
    "Control": (17, "ControlLeft", None), "Alt": (18, "AltLeft", None),
    "Meta": (91, "MetaLeft", None),
}
_MOUSE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}
_BUTTONS = ("left", "middle", "right", "none")
_KINDS = ("link", "button", "field", "list")
_TYPING_MODS = 1 | 2 | 4  # Alt, Ctrl, Meta: a keyDown with these is a shortcut, not text


class _Pool:
    """A fixed number of daemon threads (started as needed) running tasks from a queue.
    Daemon, so a task stuck in a lookup or a DevTools call never holds up Primer's exit;
    shutdown drops the queued tasks and joins the threads for at most join_s in all."""

    def __init__(self, workers: int, name: str):
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._max, self._name = workers, name
        self._threads: list[threading.Thread] = []
        self._idle = 0
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn, *args) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("the pool is shut down")
            self._q.put((fn, args))
            if self._idle > 0:
                self._idle -= 1  # an idle thread takes it
            elif len(self._threads) < self._max:
                t = threading.Thread(target=self._work, daemon=True,
                                     name=f"{self._name}-{len(self._threads)}")
                self._threads.append(t)
                t.start()

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:
                log.debug("live task failed", exc_info=True)
            with self._lock:
                self._idle += 1

    def shutdown(self, join_s: float = POOL_JOIN_S) -> None:
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        while True:  # queued tasks never run
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        for _t in threads:
            self._q.put(None)
        deadline = time.monotonic() + join_s
        for t in threads:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))


class LiveError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _jpeg_size(b64: str) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, reading only the first few KB."""
    chunk = b64[:16384]
    try:
        head = base64.b64decode(chunk[: len(chunk) // 4 * 4])
    except (ValueError, TypeError):
        return None
    i = 2
    if head[:2] != b"\xff\xd8":
        return None
    while i + 9 < len(head):
        if head[i] != 0xFF:
            return None
        marker = head[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(head[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(head[i + 5:i + 7], "big")
            w = int.from_bytes(head[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg
    return None


def _number(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _int(v, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return None
    return v


class _Window:
    def __init__(self, name: str, width: int, height: int):
        self.name = name
        self.width, self.height = width, height
        self.cdp = None
        self.target_id: str | None = None
        self.session: str | None = None
        self.cond = threading.Condition()
        self.frame: dict | None = None
        self.seq = 0
        self.note: str | None = None
        self.note_seq = 0
        self.gone: str | None = None
        self.frame_w, self.frame_h = width, height
        self.token: str | None = None
        self.controls: dict[int, dict] = {}
        self.world: int | None = None  # the isolated world snapshot.js runs in
        self.gen = secrets.token_hex(8)  # tells this window from a later one with the same name
        self.nav: dict | None = None  # {url, title} of the main frame, as last reported
        self.nav_seq = 0
        self.readers = 0  # frames streams reading this window now
        self.orphan_since: float | None = None  # when the last reader went (or the window opened)
        self.lock = threading.Lock()  # held while the window's size changes

    def push(self, frame: dict) -> None:
        with self.cond:
            if self.gone is not None:
                return
            self.frame = frame
            self.frame_w, self.frame_h = frame["w"], frame["h"]
            self.seq += 1
            self.cond.notify_all()

    def tell(self, note: str) -> None:
        with self.cond:
            self.note = note
            self.note_seq += 1
            self.cond.notify_all()

    def navigated(self, url: str, title: str) -> None:
        """The main frame's address or title changed: tell the frames readers (once each)."""
        nav = {"url": url[:2000], "title": title[:300]}
        with self.cond:
            if self.gone is not None or nav == self.nav:
                return
            self.nav = nav
            self.nav_seq += 1
            self.cond.notify_all()

    def end(self, reason: str) -> bool:
        """End the window; True if this call ended it (False if it had ended already)."""
        with self.cond:
            now = self.gone is None
            if now:
                self.gone = reason
                self.frame = None
            self.cond.notify_all()
            return now


class LiveService:
    def __init__(self, var_dir, tier, launcher=Browser, *, find=find_browser,
                 clock=time.monotonic, resolve=socket.getaddrinfo, idle_s: float = IDLE_S,
                 start_threads: bool = True, dns_timeout: float = DNS_TIMEOUT_S,
                 proxy=GuardProxy, probe_timeout: float = PROBE_TIMEOUT_S):
        self.var_dir = Path(var_dir)
        self.profile = self.var_dir / "browser"
        self._found: tuple[str, str] | None = None
        self._looked_at: float | None = None
        self._refind = False  # the browser path changed while a browser ran
        self._tier = tier
        self._launcher = launcher
        self._proxy_cls = proxy  # called as proxy(resolve=...) for each browser start
        self._proxy = None
        self._probe_timeout = probe_timeout
        self._label = ""  # the running browser's name, for NO_PROXY
        self._proved_at: float | None = None  # when the running browser last proved its proxy
        self._probe_host: str | None = None  # the probe name while a running browser re-proves
        self._probe_sessions: set[str] = set()  # the probe tab's own session (its Fetch is its own)
        self._probe_tabs: set[str] = set()  # a re-probe's tab: _on_attached leaves it alone
        self._probe_attach: dict[str, str] = {}  # while re-proving: new page tab -> auto session
        self._find = find
        self._clock = clock
        self._resolve = resolve
        self._idle_s = idle_s
        self._start_threads = start_threads
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._attach_cond = threading.Condition(self._lock)
        self._dns_timeout = dns_timeout
        self._browser = None
        self._cdp = None
        self._windows: dict[str, _Window] = {}
        self._by_session: dict[str, _Window] = {}
        self._by_target: dict[str, _Window] = {}
        self._attached: dict[str, str] = {}  # top-level page target -> its session
        self._popups: dict[str, tuple[_Window, float]] = {}  # blank pop-up -> (into, deadline)
        self._creating = 0  # Target.createTarget calls of ours not yet claimed
        self._sweeper: threading.Thread | None = None
        # worker session -> (cdp, target id, deadline to reach its first-script breakpoint)
        self._worker_init: dict[str, tuple] = {}
        self._dns_backlog = 0  # lookups asked for and not yet answered or given up
        self._dns_threads = 0  # lookup threads alive, given-up ones included
        self._dns_slots = threading.Semaphore(DNS_WORKERS)
        self._empty_since: float | None = None
        self._guard_pool = _Pool(GUARD_WORKERS, "live-guard")
        self._cdp_pool = _Pool(CDP_WORKERS, "live-cdp")
        self._worker_pool = _Pool(WORKER_SETUP_WORKERS, "live-worker")
        self._closed = False
        self._stop_ev = threading.Event()
        self._idle_thread: threading.Thread | None = None
        # called as on_window_end(name, gen, reason) once for each window that ends, outside the
        # lock (the App stops that window's agent and its pending ask)
        self.on_window_end = None

    # --- status ----------------------------------------------------------------------------

    @staticmethod
    def _path_of(tier) -> str:
        return getattr(getattr(tier, "browser", None), "path", "") or ""

    @property
    def tier(self):
        return self._tier

    @tier.setter
    def tier(self, tier) -> None:
        """A tier switch: a changed [browser] path is looked for again, with no restart of
        Primer. A browser already running keeps running (and is the one status reports)
        until it stops; the next start uses the new path."""
        with self._lock:
            changed = self._path_of(tier) != self._path_of(self._tier)
            self._tier = tier
            if changed:
                if self._browser is None:
                    self._found, self._looked_at = None, None
                else:
                    self._refind = True  # when this browser stops

    @property
    def enabled(self) -> bool:
        web = getattr(self.tier, "web", None)
        return getattr(web, "search", "duckduckgo") != "off"

    def _found_browser(self) -> tuple[str, str] | None:
        """The browser found; a failed look is tried again after FIND_RETRY_S."""
        if self._found is None and (self._looked_at is None
                                    or self._clock() - self._looked_at >= FIND_RETRY_S):
            cfg = self._path_of(self.tier)
            self._looked_at = self._clock()
            try:
                self._found = self._find(cfg)
            except Exception:
                log.exception("looking for a browser failed")
                self._found = None
        return self._found

    def status(self) -> dict:
        found = self._found_browser()
        with self._lock:
            names = sorted(self._windows)
        return {"available": found is not None, "label": found[1] if found else "",
                "enabled": self.enabled, "windows": names}

    # --- the browser -----------------------------------------------------------------------

    def _submit(self, fn, *args, pool=None) -> None:
        if self._closed:
            return
        try:
            (pool or self._cdp_pool).submit(self._safely, fn, *args)
        except RuntimeError:  # the pool is shut down
            pass

    def _timed_resolve(self, host, port, *a, **k):
        """getaddrinfo on a daemon thread of its own, refused after dns_timeout seconds (the
        wait for a free slot included). At most DNS_WORKERS run at once; one given up frees
        its slot at once and its thread ends on its own, uncounted, so stuck lookups never
        block later ones. Refused at once when MAX_DNS_BACKLOG lookups are already waiting,
        or MAX_DNS_THREADS lookup threads are alive."""
        deadline = time.monotonic() + self._dns_timeout
        with self._lock:
            if self._closed:
                raise OSError("stopping")
            if self._dns_backlog >= MAX_DNS_BACKLOG:
                raise OSError("too many lookups waiting")
            self._dns_backlog += 1
        try:
            if not self._dns_slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise OSError(f"looking up {host} took too long")
            try:
                with self._lock:
                    if self._dns_threads >= MAX_DNS_THREADS:
                        raise OSError("too many lookups running")
                    self._dns_threads += 1
                box: dict = {}
                done = threading.Event()

                def lookup():
                    try:
                        box["r"] = self._resolve(host, port, *a, **k)
                    except BaseException as e:  # handed to the caller
                        box["e"] = e
                    finally:
                        with self._lock:
                            self._dns_threads -= 1
                        done.set()
                try:
                    threading.Thread(target=lookup, name="live-dns", daemon=True).start()
                except RuntimeError:
                    with self._lock:
                        self._dns_threads -= 1
                    raise OSError("no thread for the lookup") from None
                if not done.wait(max(0.0, deadline - time.monotonic())):
                    raise OSError(f"looking up {host} took too long")  # its slot is freed now
                if "e" in box:
                    raise box["e"]
                return box["r"]
            finally:
                self._dns_slots.release()
        finally:
            with self._lock:
                self._dns_backlog -= 1

    def _allowed(self, url) -> bool:
        return guard.allowed(url, self._timed_resolve)

    @staticmethod
    def _safely(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.debug("live task %s failed", getattr(fn, "__name__", fn), exc_info=True)

    @staticmethod
    def _quiet(cdp, method: str, params: dict | None = None, session: str | None = None,
               timeout: float = 10) -> dict | None:
        try:
            return cdp.call(method, params, session=session, timeout=timeout)
        except CdpError as e:
            log.debug("%s failed: %s", method, e)
            return None

    def _ensure(self):
        """The running browser's Cdp, starting the browser if needed. Raises LiveError."""
        with self._start_lock:
            if self._closed:
                raise LiveError(503, "Primer is stopping")
            with self._lock:
                cdp = self._cdp
            if cdp is not None and not cdp.closed.is_set():
                self._reprove(cdp)  # before each new window: a policy may have come since
                return cdp
            if cdp is not None:
                self._stop_browser(BROWSER_STOPPED)
            found = self._found_browser()
            if not found:
                raise LiveError(503, NO_BROWSER)
            self._sweep_forgotten()
            self._drop_service_workers()
            proxy = browser = None
            try:  # whatever fails from here on, the browser stops first, then the proxy
                proxy = self._proxy_cls(resolve=self._resolve)  # a fresh one for each browser
                try:
                    port = proxy.start()
                except Exception as e:
                    raise LiveError(503, f"Couldn't start the live browser's proxy: {e}") from None
                browser = self._launcher(found[0], self.profile)
                try:
                    cdp = browser.start(port)
                except Exception as e:
                    raise LiveError(503, f"Couldn't start {found[1]}: {e}") from None
                try:
                    self._prove_proxy(cdp, proxy, found[1])
                    self._wire(cdp)
                    self._quiet(cdp, "Target.setDiscoverTargets", {"discover": True})
                    cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
                    cdp.call("Target.setAutoAttach", dict(_AUTO))
                    self._deny(cdp)
                    # pages the browser opened by itself (a new-tab page) aren't ours
                    for t in (self._quiet(cdp, "Target.getTargets") or {}).get("targetInfos",
                                                                               []):
                        if isinstance(t, dict) and t.get("type") == "page":
                            self._quiet(cdp, "Target.closeTarget",
                                        {"targetId": t.get("targetId")})
                except CdpError as e:
                    raise LiveError(503, f"Couldn't set up {found[1]}: {e}") from None
            except BaseException:
                if browser is not None:
                    try:
                        browser.stop()
                    except Exception:
                        log.exception("stopping the live browser failed")
                self._stop_proxy(proxy)
                raise
            with self._lock:
                self._browser, self._cdp, self._proxy = browser, cdp, proxy
                self._label, self._proved_at = found[1], self._clock()
                self._empty_since = self._clock() if not self._windows else None
            if self._start_threads:
                threading.Thread(target=self._watch, args=(cdp,), name="live-watch",
                                 daemon=True).start()
                if self._idle_thread is None:
                    self._idle_thread = threading.Thread(target=self._idle_loop,
                                                         name="live-idle", daemon=True)
                    self._idle_thread.start()
            return cdp

    def _prove_proxy(self, cdp, proxy, label: str, wired: bool = False) -> None:
        """Have the new browser ask for the proxy's probe URL (a random .invalid name only the
        proxy answers) and refuse to go on unless the proxy saw it. A browser proxy policy
        outranks --proxy-server, and the flag alone can't tell.

        The probe tab runs no page content, even when the proxy is bypassed and something
        else answers for the name: it is made blank, its scripts are switched off, and its own
        Fetch lets through only the probe's request (any scheme, the probe host only) and fails
        every response, redirect target and subresource. Then it is stopped and closed."""
        deadline = time.monotonic() + self._probe_timeout
        tid = cdp.call("Target.createTarget", {"url": "about:blank"}, timeout=10).get("targetId")
        if not tid:
            raise CdpError("no probe tab")
        if wired:
            with self._lock:
                self._probe_tabs.add(tid)
        paused = None
        sid = None
        try:
            sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True},
                           timeout=10).get("sessionId")
            if not sid:
                raise CdpError("no probe session")
            with self._lock:  # the browser-wide guard (_decide) leaves this session alone
                self._probe_sessions.add(sid)
            cdp.call("Emulation.setScriptExecutionDisabled", {"value": True}, session=sid)

            def paused(p, s):  # on the connection's thread: send only, never wait
                if s != sid:
                    return
                rid = p.get("requestId")
                req = p.get("request") if isinstance(p.get("request"), dict) else {}
                try:
                    host = urllib.parse.urlsplit(str(req.get("url", ""))).hostname
                except ValueError:
                    host = None
                response = "responseStatusCode" in p or "responseErrorReason" in p
                try:
                    if host == proxy.probe_host and not response:
                        cdp.send("Fetch.continueRequest", {"requestId": rid}, s)
                    else:
                        cdp.send("Fetch.failRequest",
                                 {"requestId": rid, "errorReason": "BlockedByClient"}, s)
                except CdpError:
                    pass
            cdp.on("Fetch.requestPaused", paused)
            cdp.call("Fetch.enable", {"patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
                {"urlPattern": "*", "requestStage": "Response"}]}, session=sid)
            if wired:  # auto-attach made the tab paused: resume it here, not behind the cdp pool
                self._resume_probe_tab(cdp, tid, deadline)
            try:  # returns once the navigation fails (the proxy's 403 is failed, too)
                cdp.call("Page.navigate", {"url": proxy.probe_url}, session=sid,
                         timeout=max(0.1, deadline - time.monotonic()))
            except CdpError:
                pass  # timed out: the wait below decides
            seen = proxy.wait_probe(max(0.0, deadline - time.monotonic()))
        finally:
            if sid:
                self._quiet(cdp, "Page.stopLoading", {}, sid, timeout=3)
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid}, timeout=3)
            if paused is not None:
                cdp.off("Fetch.requestPaused", paused)
            with self._lock:
                self._probe_sessions.discard(sid)
        if not seen:
            log.warning("live browser: %s did not use the guarding proxy; refusing it", label)
            raise LiveError(503, NO_PROXY.format(label=label))

    def _resume_probe_tab(self, cdp, tid: str, deadline: float) -> None:
        """The re-probe's tab, paused by auto-attach, resumed on this thread: its scripts are
        off and its own Fetch is on by now, so it needs none of _on_attached's set-up."""
        with self._lock:
            self._attach_cond.wait_for(lambda: tid in self._probe_attach or cdp.closed.is_set(),
                                       max(0.0, min(2.0, deadline - time.monotonic())))
            auto = self._probe_attach.get(tid)
        if auto:
            self._quiet(cdp, "Runtime.runIfWaitingForDebugger", None, auto,
                        timeout=max(0.1, deadline - time.monotonic()))

    def _reprove(self, cdp) -> None:
        """Prove again that the running browser uses its proxy, with a fresh probe name: Chrome
        applies a proxy policy change while it runs. On a miss (or a browser that can't run the
        probe) the browser stops, then the proxy, and LiveError(503) is raised. The caller
        holds _start_lock.

        The browser is wired by now, so: the probe tab counts as one being made (_creating), so
        it is never closed as a stray while the probe runs, and it is resumed by the probe
        itself, never behind the cdp pool; the probe's own request is let through the
        browser-wide guard on the connection's thread (_on_paused: only it, only while this
        runs, only at the request stage; the proxy answers it without a lookup), never behind
        the guard pool's page lookups; and the probe tab's own session is left to its own Fetch,
        which fails every response and anything else. A browser that crashed meanwhile gets
        "The browser stopped", not the proxy-policy message."""
        with self._lock:
            proxy, label = self._proxy, self._label or "The browser"
            if proxy is None or self._cdp is not cdp:
                return
            self._probe_host = proxy.new_probe()
            self._creating += 1
        missed = None
        try:
            self._prove_proxy(cdp, proxy, label, wired=True)
        except LiveError as e:
            missed = e
        except CdpError as e:
            log.warning("live browser: the proxy check couldn't run (%s); stopping it", e)
            missed = LiveError(503, f"Couldn't check the proxy of {label}: {e}")
        finally:
            with self._lock:
                self._probe_host = None
                self._probe_tabs.clear()
                self._probe_attach.clear()
                self._creating -= 1
                strays = list(self._attached) if self._creating == 0 else []
                for t in strays:
                    del self._attached[t]
            for t in strays:  # the probe tab (closed already), or a pop-up that came meanwhile
                self._quiet(cdp, "Target.closeTarget", {"targetId": t})
        if missed is not None and cdp.closed.is_set():
            # the browser crashed meanwhile: not a proxy policy. The next open starts a fresh one
            self._stop_browser(BROWSER_STOPPED, only=cdp)
            raise LiveError(503, BROWSER_STOPPED)
        if missed is not None:
            self._stop_browser(missed.message, only=cdp)
            raise missed
        with self._lock:
            if self._cdp is cdp:
                self._proved_at = self._clock()

    def _reprobe_due(self) -> bool:
        return (self._cdp is not None and bool(self._windows) and self._proved_at is not None
                and self._clock() - self._proved_at >= REPROBE_S)

    def _tick_probe(self) -> bool:
        """While live windows are open, prove the proxy again at most every REPROBE_S. True if
        that stopped the browser."""
        with self._lock:
            if not self._reprobe_due():
                return False
        with self._start_lock:
            with self._lock:
                if self._closed or not self._reprobe_due():
                    return False
                cdp = self._cdp
            if cdp.closed.is_set():
                return False  # _watch stops it
            try:
                self._reprove(cdp)
            except LiveError:
                return True
        return False

    def _deny(self, cdp) -> None:
        """Downloads and every permission denied (browser-wide; set again for each tab)."""
        self._quiet(cdp, "Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
        for name in _PERMISSIONS:
            self._quiet(cdp, "Browser.setPermission",
                        {"permission": {"name": name}, "setting": "denied"})

    def _on_paused(self, cdp, params: dict, sid) -> None:
        """On the connection's thread, send-only: a re-probe's requests never wait behind page
        lookups in the guard pool. Everything else goes to _decide on that pool."""
        req = params.get("request") if isinstance(params.get("request"), dict) else {}
        with self._lock:
            if sid is not None and sid in self._probe_sessions:
                return  # the probe tab's own Fetch decides its requests
            probe = self._probe_host
        if probe is not None and self._is_probe(str(req.get("url", "")), probe, params):
            try:
                cdp.send("Fetch.continueRequest", {"requestId": params.get("requestId")}, sid)
            except CdpError:
                pass
            return
        self._submit(self._decide, cdp, params, sid, pool=self._guard_pool)

    def _on_attach_event(self, cdp, params: dict, parent) -> None:
        """On the connection's thread: while a re-probe runs, note each new top-level page's
        session so the probe can resume its own tab; every target is still set up on the cdp
        pool (_on_attached), which leaves a probe tab alone."""
        info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
        if parent is None and info.get("type") == "page" and params.get("sessionId"):
            with self._lock:
                if self._probe_host is not None:
                    self._probe_attach[info.get("targetId")] = params["sessionId"]
                    self._attach_cond.notify_all()
        self._submit(self._on_attached, cdp, params, parent)

    def _wire(self, cdp) -> None:
        cdp.on("Fetch.requestPaused", lambda p, s: self._on_paused(cdp, p, s))
        cdp.on("Target.attachedToTarget", lambda p, s: self._on_attach_event(cdp, p, s))
        cdp.on("Target.detachedFromTarget", lambda p, s: self._on_detached(p, s))
        cdp.on("Target.targetDestroyed", lambda p, s: self._on_target_gone(p, "The page closed"))
        cdp.on("Target.targetCrashed", lambda p, s: self._on_target_gone(p, "The page crashed"))
        cdp.on("Target.targetInfoChanged", lambda p, s: self._on_info_changed(cdp, p))
        cdp.on("Inspector.targetCrashed", lambda p, s: self._on_session_gone(s, "The page crashed"))
        cdp.on("Page.screencastFrame", lambda p, s: self._on_frame(cdp, p, s))
        cdp.on("Page.javascriptDialogOpening", lambda p, s: self._submit(
            self._quiet, cdp, "Page.handleJavaScriptDialog",
            {"accept": p.get("type") == "beforeunload"}, s))
        cdp.on("Browser.downloadWillBegin", lambda p, s: self._on_download(p))
        cdp.on("Debugger.paused", lambda p, s: self._on_worker_paused(p, s))

    @staticmethod
    def _worker_script(req) -> bool:
        """A service worker's script request (a registration or an update check)."""
        headers = req.get("headers") if isinstance(req, dict) else None
        if not isinstance(headers, dict):
            return False
        return any(isinstance(k, str) and k.lower() == "service-worker" for k in headers)

    @staticmethod
    def _is_probe(url: str, probe: str, params: dict) -> bool:
        """The running browser's re-probe request (any scheme, the probe name only, not a
        response): the proxy answers it itself, never resolving or connecting."""
        if "responseStatusCode" in params or "responseErrorReason" in params:
            return False
        try:
            return urllib.parse.urlsplit(url).hostname == probe
        except ValueError:
            return False

    def _decide(self, cdp, params: dict, sid) -> None:
        """A paused request: continue it only if guard.allowed (runs in the pool). A service
        worker's script is always refused: service workers don't run in live pages."""
        rid = params.get("requestId")
        req = params.get("request") or {}
        url = req.get("url", "") if isinstance(req, dict) else ""
        if not self._worker_script(req) and self._allowed(url):
            self._quiet(cdp, "Fetch.continueRequest", {"requestId": rid}, sid)
        else:
            log.info("live browser: refused %s", url[:200])
            self._quiet(cdp, "Fetch.failRequest",
                        {"requestId": rid, "errorReason": "BlockedByClient"}, sid)

    def _on_attached(self, cdp, params: dict, parent) -> None:
        """A new target, paused: make it safe, then resume it (runs in the cdp pool)."""
        sid = params.get("sessionId")
        info = params.get("targetInfo") or {}
        ttype, tid = info.get("type"), info.get("targetId")
        if not sid:
            return
        with self._lock:
            if tid in self._probe_tabs:
                return  # a re-probe's tab: the probe resumes it and closes it
        try:
            if ttype in ("page", "iframe"):
                cdp.call("Page.enable", session=sid)
                cdp.call("Page.addScriptToEvaluateOnNewDocument",
                         {"source": NO_SOCKETS_JS, "runImmediately": True}, session=sid)
                cdp.call("Target.setAutoAttach", dict(_AUTO), session=sid)
                if ttype == "page":
                    self._quiet(cdp, "Page.setInterceptFileChooserDialog", {"enabled": True}, sid)
            elif ttype in _WORKERS and not params.get("waitingForDebugger"):
                # already running when we attached (the browser's own workers at start): lock
                # what's there now
                cdp.call("Runtime.evaluate", {"expression": NO_SOCKETS_JS}, session=sid)
                return
            elif ttype in _WORKERS:  # on its own pool: it can wait up to 3 x WORKER_PAUSE_S
                self._submit(self._setup_worker, cdp, sid, tid, ttype, pool=self._worker_pool)
                return
            elif params.get("waitingForDebugger"):  # a kind we don't know: never run it
                log.info("live browser: closing a %s target", ttype)
                self._quiet(cdp, "Target.closeTarget", {"targetId": tid})
                return
            else:
                return  # already running when we attached (the browser's own)
        except CdpError as e:  # never resume a target we couldn't make safe
            log.info("live browser: could not set up a %s target (%s); not running it", ttype, e)
            if ttype == "page" and tid:
                self._quiet(cdp, "Target.closeTarget", {"targetId": tid})
            return
        if params.get("waitingForDebugger"):
            self._quiet(cdp, "Runtime.runIfWaitingForDebugger", None, sid)
        if ttype != "page" or parent is not None or not tid:
            return
        opener = info.get("openerId")
        with self._lock:
            into = self._by_target.get(opener) if opener else None
            if into is None:
                if self._creating > 0:  # perhaps the tab one of our open() calls is making
                    self._attached[tid] = sid
                    self._attach_cond.notify_all()
                    return
                stray = True
            else:
                stray = False
        if stray:  # not ours and no opener of ours (noopener pop-ups): close it
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid})
            return
        self._submit(self._popup, cdp, tid, info.get("url", ""), into, pool=self._guard_pool)

    def _setup_worker(self, cdp, sid: str, tid, ttype: str) -> None:
        """Arm a breakpoint before the worker's first script, checking each reply, then let it
        run to there; _lock_worker locks it. Any failure, or no pause within WORKER_PAUSE_S,
        closes it. Dedicated and shared workers answer while paused, so their replies are
        checked before they run. Service workers are blocked in live pages (see the module
        docstring); this path is the fallback for any that still attach. A paused service
        worker may answer nothing until it runs, so its set-up is queued in order, it is
        resumed into the breakpoint, and the replies are checked then (a failure closes it,
        but it may have run briefly by then)."""
        with self._lock:
            self._worker_init[sid] = (cdp, tid, self._clock() + WORKER_PAUSE_S)
        steps = (("Debugger.enable", None),
                 ("Debugger.setInstrumentationBreakpoint",
                  {"instrumentation": "beforeScriptExecution"}),
                 ("Target.setAutoAttach", dict(_AUTO)))  # its own nested workers, paused too
        try:
            if ttype == "service_worker":
                pending = [cdp.start(m, prm, sid) for m, prm in steps]
                cdp.send("Runtime.runIfWaitingForDebugger", None, sid)
                for p in pending:
                    cdp.finish(p, WORKER_PAUSE_S)
            else:
                for m, prm in steps:
                    cdp.call(m, prm, session=sid, timeout=WORKER_PAUSE_S)
                cdp.send("Runtime.runIfWaitingForDebugger", None, sid)
        except CdpError as e:
            log.info("live browser: could not set up a %s (%s); closing it", ttype, e)
            self._close_worker(cdp, sid, tid)
            return
        self._ensure_sweeper(cdp)

    def _close_worker(self, cdp, sid: str, tid) -> None:
        with self._lock:
            self._worker_init.pop(sid, None)
        if tid:
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid}, timeout=3)
        self._quiet(cdp, "Runtime.terminateExecution", None, sid, timeout=3)

    def _on_worker_paused(self, params: dict, sid) -> None:
        with self._lock:
            entry = self._worker_init.pop(sid, None) if sid else None
        if entry is not None:
            self._submit(self._lock_worker, entry[0], sid, entry[1])

    def _lock_worker(self, cdp, sid, tid=None) -> None:
        """A worker stopped before its first script: lock its sockets, then let it run."""
        try:
            r = cdp.call("Runtime.evaluate", {"expression": NO_SOCKETS_JS}, session=sid)
        except CdpError as e:
            log.info("live browser: could not lock a worker (%s); closing it", e)
            self._close_worker(cdp, sid, tid)
            return
        if "exceptionDetails" in r:
            log.info("live browser: the socket lock failed in a worker; closing it")
            self._close_worker(cdp, sid, tid)
            return
        self._quiet(cdp, "Debugger.disable", None, sid)  # removes the breakpoint and resumes

    def _popup(self, cdp, tid: str, url: str, into: _Window) -> None:
        """A pop-up: open its address in the opener's tab instead, and close it."""
        if url and url != "about:blank":
            with self._lock:
                self._popups.pop(tid, None)
            if into.session and urllib.parse.urlsplit(url).scheme in ("http", "https") \
                    and self._allowed(url):
                self._quiet(cdp, "Page.navigate", {"url": url}, into.session)
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid})
            return
        with self._lock:  # its address isn't known yet: wait for targetInfoChanged
            full = len(self._popups) >= MAX_POPUPS
            if not full:
                self._popups[tid] = (into, self._clock() + POPUP_WAIT_S)
        if full:
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid})
        else:
            self._ensure_sweeper(cdp)

    def _ensure_sweeper(self, cdp) -> None:
        with self._lock:
            if self._start_threads and (self._sweeper is None or not self._sweeper.is_alive()):
                self._sweeper = threading.Thread(target=self._sweep, args=(cdp,),
                                                 name="live-sweep", daemon=True)
                self._sweeper.start()

    def _sweep(self, cdp) -> None:
        """One thread: close blank pop-ups whose address never came, and workers that never
        reached their first-script breakpoint. It ends when nothing is waiting."""
        while not self._stop_ev.wait(0.25):
            now = self._clock()
            with self._lock:
                late = [t for t, (_w, end) in self._popups.items() if end <= now]
                for t in late:
                    del self._popups[t]
                stuck = [(sid, e) for sid, e in self._worker_init.items() if e[2] <= now]
                idle = not self._popups and len(stuck) == len(self._worker_init)
            for t in late:
                self._quiet(cdp, "Target.closeTarget", {"targetId": t})
            for sid, (wcdp, tid, _end) in stuck:
                log.info("live browser: a worker never reached its first script; closing it")
                self._close_worker(wcdp, sid, tid)
            if idle:
                with self._lock:
                    if not self._popups and not self._worker_init:
                        return

    def _on_info_changed(self, cdp, params: dict) -> None:
        info = params.get("targetInfo") or {}
        tid, url = info.get("targetId"), info.get("url", "")
        with self._lock:
            pending = self._popups.get(tid)
            w = self._by_target.get(tid) if tid else None
        if pending is not None and url and url != "about:blank":
            self._submit(self._popup, cdp, tid, url, pending[0], pool=self._guard_pool)
        if w is not None and isinstance(url, str) and url:
            title = info.get("title")
            w.navigated(url, title if isinstance(title, str) and title != url else "")

    def _on_detached(self, params: dict, parent) -> None:
        with self._lock:
            self._worker_init.pop(params.get("sessionId"), None)
        if parent is None:
            self._on_session_gone(params.get("sessionId"), "The page closed")

    def _on_session_gone(self, sid, reason: str) -> None:
        with self._lock:
            w = self._by_session.get(sid) if sid else None
        if w is not None:
            self._forget_window(w, reason)

    def _on_target_gone(self, params: dict, reason: str) -> None:
        tid = params.get("targetId")
        with self._lock:
            w = self._by_target.get(tid) if tid else None
            self._attached.pop(tid, None)
        if w is not None:
            self._forget_window(w, reason)

    def _forget_window(self, w: _Window, reason: str) -> None:
        with self._lock:
            self._unlink(w)
        if w.end(reason):
            self._ended(w, reason)

    def _unlink(self, w: _Window) -> None:
        """Take the window out of every map (the caller holds the lock)."""
        if self._windows.get(w.name) is w:
            del self._windows[w.name]
            if not self._windows:
                self._empty_since = self._clock()
        self._by_session.pop(w.session, None)
        self._by_target.pop(w.target_id, None)

    def _ended(self, w: _Window, reason: str) -> None:
        hook = self.on_window_end
        if hook is not None:
            try:
                hook(w.name, w.gen, reason)
            except Exception:
                log.exception("on_window_end failed")

    def _on_frame(self, cdp, params: dict, sid) -> None:
        """Acknowledge every frame (in the pool); keep only the latest per window."""
        try:  # fire and forget, on this thread: no pool can hold up the picture
            cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, sid)
        except CdpError:
            return
        with self._lock:
            w = self._by_session.get(sid) if sid else None
        data = params.get("data")
        if w is None or not isinstance(data, str) or len(data) * 3 // 4 > MAX_FRAME_BYTES:
            return
        meta = params.get("metadata") or {}
        size = _jpeg_size(data)
        if size is None:
            fw, fh = _number(meta.get("deviceWidth")), _number(meta.get("deviceHeight"))
            size = (int(fw), int(fh)) if fw and fh else (w.width, w.height)
        w.push({"data": data, "w": size[0], "h": size[1]})

    def _on_download(self, params: dict) -> None:
        frame = params.get("frameId")
        with self._lock:
            ws = [self._by_target[frame]] if frame in self._by_target else list(self._windows.values())
        for w in ws:
            w.tell(NO_DOWNLOADS)

    def _watch(self, cdp) -> None:
        cdp.closed.wait()
        self._stop_browser(BROWSER_STOPPED, only=cdp)

    def _idle_loop(self) -> None:
        while not self._stop_ev.wait(IDLE_CHECK_S):
            try:
                self.tick()
            except Exception:
                log.exception("live idle check failed")

    def tick(self) -> bool:
        """Close windows nobody has watched for ORPHAN_S, prove the proxy again when due, then
        close the browser after idle_s with no live window. True if the browser was closed."""
        self.sweep_orphans()
        if self._tick_probe():
            return True
        return self._tick_browser()

    def sweep_orphans(self) -> list[str]:
        """Close every open window with no frames reader for ORPHAN_S (its tab reloaded or went
        away without a close). Returns the names closed."""
        def orphaned(w: _Window) -> bool:
            return (w.session is not None and w.gone is None and w.readers == 0
                    and w.orphan_since is not None
                    and self._clock() - w.orphan_since >= ORPHAN_S)
        with self._lock:
            seen = [w for w in self._windows.values() if orphaned(w)]
        closed = []
        for w in seen:  # checked again under the lock: a reader may have come since
            if self._close(w, orphaned):
                log.info("live window %s: no reader for %.0f s; closed it", w.name, ORPHAN_S)
                closed.append(w.name)
        return closed

    def _tick_browser(self) -> bool:
        def idle() -> bool:
            return (self._browser is not None and not self._windows
                    and self._empty_since is not None
                    and self._clock() - self._empty_since >= self._idle_s)
        with self._lock:
            if not idle():
                return False
        with self._start_lock:
            with self._lock:
                if not idle():
                    return False
            self._stop_browser("idle")
        return True

    def _stop_browser(self, reason: str, only=None) -> None:
        with self._lock:
            if only is not None and self._cdp is not only:
                return
            browser, self._browser, self._cdp = self._browser, None, None
            proxy, self._proxy = self._proxy, None
            if self._refind:  # the path changed while it ran: look again for the next start
                self._found, self._looked_at, self._refind = None, None, False
            # windows on this browser end; one still being opened (no tab yet) waits for the next
            windows = [w for w in self._windows.values() if w.cdp is not None]
            for w in windows:
                del self._windows[w.name]
            self._by_session.clear()
            self._by_target.clear()
            self._attached.clear()
            self._popups.clear()
            self._worker_init.clear()
            self._empty_since = None
        for w in windows:
            if w.end(reason):
                self._ended(w, reason)
        if browser is not None:
            try:
                browser.stop()
            except Exception:
                log.exception("stopping the live browser failed")
        self._stop_proxy(proxy)  # after the browser, so it never runs unproxied

    @staticmethod
    def _stop_proxy(proxy) -> None:
        if proxy is not None:
            try:
                proxy.stop()
            except Exception:
                log.exception("stopping the live proxy failed")

    # --- windows ---------------------------------------------------------------------------

    @staticmethod
    def _name(window) -> str:
        if not isinstance(window, str) or not _NAME.fullmatch(window):
            raise LiveError(400, "Bad window name")
        return window

    @staticmethod
    def _dim(v, lo: int, hi: int) -> int:
        n = _number(v)
        if n is None:
            raise LiveError(400, "Bad window size")
        return int(min(hi, max(lo, n)))

    def _openable(self, url) -> bool:
        if not isinstance(url, str) or not url or len(url) > MAX_URL:
            return False
        try:
            scheme = urllib.parse.urlsplit(url).scheme.lower()
        except ValueError:
            return False
        return scheme in ("http", "https", "data", "about") and self._allowed(url)

    def _live(self, window, gen: str | None = None, *, need_on: bool = False) -> _Window:
        if need_on and not self.enabled:
            raise LiveError(403, OFF)
        name = self._name(window)
        with self._lock:
            w = self._windows.get(name)
        if w is None or w.gone is not None or (gen is not None and w.gen != gen):
            raise LiveError(404, GONE)
        if w.session is None or w.cdp is None:
            raise LiveError(409, "That live page is still opening")
        return w

    def open(self, window: str, url: str, width: int, height: int) -> dict:
        if not self.enabled:
            raise LiveError(403, OFF)
        name = self._name(window)
        width, height = self._dim(width, 200, 3840), self._dim(height, 150, 2160)
        if not self._openable(url):
            raise LiveError(400, BAD_ADDRESS)
        found = self._found_browser()
        if not found:
            raise LiveError(503, NO_BROWSER)
        with self._lock:
            w = self._windows.get(name)
            fresh = w is None
            if fresh:
                if len(self._windows) >= MAX_WINDOWS:
                    raise LiveError(409, FULL)
                w = _Window(name, width, height)
                w.orphan_since = self._clock()  # closed if no frames reader ever comes
                self._windows[name] = w
                self._empty_since = None
        if not fresh:
            w = self._live(name)
            self._resize(w, width, height)
            self._navigate(w, url)
            return {"window": name, "label": found[1]}
        try:
            cdp = self._ensure()
            self._open_tab(cdp, w)
            self._navigate(w, url)
        except LiveError:
            self._discard(w)
            raise
        except CdpError as e:
            self._discard(w)
            raise LiveError(503, f"The browser couldn't open the page: {e}") from None
        return {"window": name, "label": found[1]}

    def generation(self, window) -> str | None:
        """The open window's generation token, or None when it isn't open."""
        name = self._name(window)
        with self._lock:
            w = self._windows.get(name)
        return w.gen if w is not None and w.gone is None else None

    def is_open(self, window, gen: str | None = None) -> bool:
        g = self.generation(window)
        return g is not None and (gen is None or g == gen)

    def turn_off(self, reason: str = OFF) -> None:
        """Web search went off: every live window ends with `reason` and the browser stops."""
        with self._start_lock:
            self._stop_browser(reason)
            with self._lock:
                rest = list(self._windows.values())  # still opening: no tab yet
            for w in rest:
                self._forget_window(w, reason)

    def _discard(self, w: _Window) -> None:
        cdp, tid = w.cdp, w.target_id
        self._forget_window(w, "closed")
        if cdp is not None and tid:
            self._quiet(cdp, "Target.closeTarget", {"targetId": tid})

    def _open_tab(self, cdp, w: _Window) -> None:
        w.cdp = cdp
        self._deny(cdp)
        with self._lock:
            self._creating += 1
        try:
            tid = cdp.call("Target.createTarget", {"url": "about:blank"}).get("targetId")
            if not isinstance(tid, str) or not tid:
                raise CdpError("no target id")
            sid = self._claim(cdp, w, tid)
        finally:
            with self._lock:
                self._creating -= 1
                strays = list(self._attached) if self._creating == 0 else []
                for t in strays:
                    del self._attached[t]
            for t in strays:  # pages nobody claimed: pop-ups without an opener of ours
                self._quiet(cdp, "Target.closeTarget", {"targetId": t})
        with w.lock:  # a resize from now on (w.session is set) waits for the first size
            cdp.call("Emulation.setDeviceMetricsOverride",
                     {"width": w.width, "height": w.height, "deviceScaleFactor": 1,
                      "mobile": False}, session=sid)
            self._quiet(cdp, "Page.setDownloadBehavior", {"behavior": "deny"}, sid)
            self._screencast(w)

    def _claim(self, cdp, w: _Window, tid: str) -> str:
        with self._lock:
            w.target_id = tid
            if self._windows.get(w.name) is w:
                self._by_target[tid] = w
            # the attach (set up and resumed) may have come before createTarget's reply
            if not self._attach_cond.wait_for(lambda: tid in self._attached or w.gone is not None
                                              or cdp.closed.is_set(), ATTACH_WAIT_S):
                raise CdpError("the new tab didn't attach")
            if w.gone is not None:
                raise LiveError(409, GONE)
            sid = self._attached.pop(tid, None)
            if sid is None:
                raise CdpError("browser closed")
            w.session = sid
            self._by_session[sid] = w
        return sid

    def _screencast(self, w: _Window) -> None:
        # everyNthFrame stays 1: headless Chrome only paints on change, so with a higher N a
        # single change (a click's result) may never be sent. The rate is capped by dropping.
        w.cdp.call("Page.startScreencast",
                   {"format": "jpeg", "quality": 70, "maxWidth": w.width, "maxHeight": w.height,
                    "everyNthFrame": 1}, session=w.session)

    def _resize(self, w: _Window, width: int, height: int) -> None:
        """Under the window's lock, so two resizes never interleave and the viewport and
        the screencast always agree."""
        with w.lock:
            if (width, height) == (w.width, w.height):
                return
            self._resize_now(w, width, height)

    def _resize_now(self, w: _Window, width: int, height: int) -> None:
        try:
            w.cdp.call("Emulation.setDeviceMetricsOverride",
                       {"width": width, "height": height, "deviceScaleFactor": 1,
                        "mobile": False}, session=w.session)
            w.width, w.height = width, height
            self._quiet(w.cdp, "Page.stopScreencast", None, w.session)
            self._screencast(w)
        except CdpError as e:
            raise LiveError(502, f"Couldn't resize the page: {e}") from None

    def _navigate(self, w: _Window, url: str) -> None:
        try:
            w.cdp.call("Page.navigate", {"url": url}, session=w.session)
        except CdpError as e:
            raise LiveError(502, f"Couldn't open the address: {e}") from None
        w.controls, w.token, w.world = {}, None, None

    def frames(self, window: str, keepalive: float = KEEPALIVE_S):
        """Yields {data, w, h} (the latest frame, at most 10 a second), {"note": text} (e.g. a
        refused download), {"ping": True} after `keepalive` seconds of nothing, and last
        {"gone": reason} when the window closes or the browser dies."""
        name = self._name(window)
        with self._lock:
            w = self._windows.get(name)
        if w is None:
            raise LiveError(404, GONE)
        return self._frames(w, keepalive)

    def _frames(self, w: _Window, keepalive: float):
        with self._lock:  # counted from the first read; a window nobody reads is an orphan
            w.readers += 1
            w.orphan_since = None
        try:
            seen = seen_note = seen_nav = 0
            while True:
                with w.cond:
                    ready = w.cond.wait_for(lambda: w.gone is not None or w.seq > seen
                                            or w.note_seq > seen_note or w.nav_seq > seen_nav,
                                            keepalive)
                    if w.gone is not None:
                        reason = w.gone
                        break
                    frame = note = nav = None
                    if w.note_seq > seen_note:
                        note, seen_note = w.note, w.note_seq
                    if w.nav_seq > seen_nav:
                        nav, seen_nav = dict(w.nav or {}), w.nav_seq
                    if w.seq > seen:
                        frame, seen = w.frame, w.seq
                if not ready:
                    yield {"ping": True}
                    continue
                if nav:
                    yield {"nav": nav}
                if note:
                    yield {"note": note}
                if frame:
                    yield dict(frame)
                    with w.cond:  # pace: later frames overwrite the slot meanwhile (dropped)
                        w.cond.wait_for(lambda: w.gone is not None, FRAME_INTERVAL_S)
            yield {"gone": reason}
        finally:
            with self._lock:
                w.readers -= 1
                if w.readers == 0:
                    w.orphan_since = self._clock()

    def input(self, window: str, event: dict) -> None:
        try:
            size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))  # as sent
        except (TypeError, ValueError):
            raise LiveError(400, "Bad input") from None
        if size > MAX_INPUT_BYTES:
            raise LiveError(413, "That input is too large")
        if not isinstance(event, dict):
            raise LiveError(400, "Bad input")
        w = self._live(window, need_on=True)
        method, params = self._map_input(w, event)
        try:
            w.cdp.call(method, params, session=w.session, timeout=5)
        except CdpError as e:
            raise LiveError(502, f"The page didn't take the input: {e}") from None

    def _point(self, w: _Window, event: dict) -> tuple[float, float]:
        x, y = _number(event.get("x")), _number(event.get("y"))
        fw, fh = w.frame_w or w.width, w.frame_h or w.height
        if x is None or y is None or not (0 <= x <= fw and 0 <= y <= fh):
            raise LiveError(400, "Bad coordinates")
        return x * w.width / fw, y * w.height / fh

    def _map_input(self, w: _Window, event: dict) -> tuple[str, dict]:
        kind = event.get("kind")
        mods = _int(event.get("modifiers", 0), 0, 15)
        if mods is None:
            raise LiveError(400, "Bad modifiers")
        if kind == "mouse":
            t = event.get("type")
            if t not in _MOUSE:
                raise LiveError(400, "Bad mouse event")
            x, y = self._point(w, event)
            button = event.get("button", "none" if t == "move" else "left")
            if button not in _BUTTONS:
                raise LiveError(400, "Bad mouse button")
            clicks = _int(event.get("clicks", 0 if t == "move" else 1), 0, 3)
            if clicks is None:
                raise LiveError(400, "Bad click count")
            return "Input.dispatchMouseEvent", {"type": _MOUSE[t], "x": x, "y": y,
                                                "button": button, "clickCount": clicks,
                                                "modifiers": mods}
        if kind == "wheel":
            x, y = self._point(w, event)
            dx, dy = _number(event.get("dx", 0)), _number(event.get("dy", 0))
            if dx is None or dy is None:
                raise LiveError(400, "Bad wheel delta")
            clamp = lambda d: max(-5000.0, min(5000.0, d))  # noqa: E731
            return "Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                "deltaX": clamp(dx), "deltaY": clamp(dy),
                                                "modifiers": mods}
        if kind == "key":
            t, key = event.get("type"), event.get("key")
            if t not in ("down", "up") or not isinstance(key, str):
                raise LiveError(400, "Bad key event")
            if key in _KEYS:
                vk, code, text = _KEYS[key]
            elif len(key) == 1 and key.isprintable():  # a shortcut such as Ctrl+A
                vk = ord(key.upper()) if key.isascii() and key.isalnum() else 0
                code = (f"Key{key.upper()}" if key.isascii() and key.isalpha()
                        else f"Digit{key}" if key.isascii() and key.isdigit() else "")
                text = None
            else:
                raise LiveError(400, "Unknown key")
            params = {"key": key, "code": code, "windowsVirtualKeyCode": vk,
                      "nativeVirtualKeyCode": vk, "modifiers": mods}
            if t == "up":
                params["type"] = "keyUp"
            elif text and not mods & _TYPING_MODS:
                params.update(type="keyDown", text=text, unmodifiedText=text)
            else:
                params["type"] = "rawKeyDown"
            return "Input.dispatchKeyEvent", params
        if kind == "text":
            text = event.get("text")
            if not isinstance(text, str) or not text or len(text) > MAX_TYPED or "\x00" in text:
                raise LiveError(400, "Bad text")
            return "Input.insertText", {"text": text}
        raise LiveError(400, "Unknown input kind")

    def nav(self, window: str, action: str, url: str | None = None) -> None:
        w = self._live(window, need_on=True)
        try:
            if action == "url":
                if not self._openable(url):
                    raise LiveError(400, BAD_ADDRESS)
                self._navigate(w, url)
            elif action == "reload":
                w.cdp.call("Page.reload", {}, session=w.session)
            elif action in ("back", "forward"):
                hist = w.cdp.call("Page.getNavigationHistory", session=w.session)
                entries, idx = hist.get("entries") or [], hist.get("currentIndex")
                if not isinstance(idx, int):
                    return
                to = idx - 1 if action == "back" else idx + 1
                if 0 <= to < len(entries) and isinstance(entries[to], dict):
                    w.cdp.call("Page.navigateToHistoryEntry", {"entryId": entries[to].get("id")},
                               session=w.session)
            else:
                raise LiveError(400, "Bad navigation")
        except CdpError as e:
            raise LiveError(502, f"Couldn't navigate: {e}") from None

    def close(self, window: str) -> bool:
        name = self._name(window)
        with self._lock:
            w = self._windows.get(name)
        return w is not None and self._close(w)

    def _close(self, w: _Window, only_if=None) -> bool:
        """Close this window (not a later one with its name). With only_if, only when
        only_if(w) still holds, checked under the lock it is taken from the windows in."""
        with self._lock:
            if self._windows.get(w.name) is not w or (only_if is not None and not only_if(w)):
                return False
            self._unlink(w)
        if w.end("closed"):  # told outside the lock
            self._ended(w, "closed")
        cdp, sid, tid = w.cdp, w.session, w.target_id
        if cdp is not None and not cdp.closed.is_set():
            if sid:
                self._quiet(cdp, "Page.stopScreencast", None, sid, timeout=3)
            if tid:
                self._quiet(cdp, "Target.closeTarget", {"targetId": tid}, timeout=3)
        return True

    # --- the model's view ------------------------------------------------------------------

    @staticmethod
    def _world(w: _Window) -> int:
        """Primer's isolated world in the window's main frame, made if there isn't one (a
        navigation takes it away). The page's own scripts can't reach into it."""
        if w.world is None:
            tree = w.cdp.call("Page.getFrameTree", session=w.session)
            frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
            world = w.cdp.call("Page.createIsolatedWorld",
                               {"frameId": frame_id, "worldName": "primer"},
                               session=w.session).get("executionContextId")
            if not isinstance(world, int) or isinstance(world, bool):
                raise CdpError("no isolated world")
            w.world = world
        return w.world

    def _peek(self, w: _Window, expr: str):
        """The value of a small, capped expression read in Primer's isolated world, or None
        if it can't be read (the world went with a navigation and a fresh one failed too)."""
        for _attempt in (0, 1):
            try:
                r = w.cdp.call("Runtime.evaluate",
                               {"expression": expr, "contextId": self._world(w),
                                "returnByValue": True},
                               session=w.session, timeout=5)
            except CdpError:
                w.world = None
                continue
            if "exceptionDetails" in (r or {}):
                w.world = None
                continue
            return ((r or {}).get("result") or {}).get("value")
        return None

    def _js(self, w: _Window, op: str, token: str, n=None, arg=None) -> dict:
        expr = (f"({SNAPSHOT_JS})({json.dumps(op)}, {json.dumps(token)}, {json.dumps(n)}, "
                f"{json.dumps(arg)})")
        for attempt in (0, 1):
            try:
                r = w.cdp.call("Runtime.evaluate",
                               {"expression": expr, "contextId": self._world(w),
                                "returnByValue": True},
                               session=w.session, timeout=10)
            except CdpError as e:
                w.world = None  # e.g. the page navigated and the world went with it
                if attempt:
                    raise LiveError(502, f"Couldn't read the page: {e}") from None
                continue
            if "exceptionDetails" in r:
                w.world = None
                if attempt:
                    raise LiveError(502, "Couldn't read the page")
                continue
            try:
                out = json.loads((r.get("result") or {}).get("value"))
            except (TypeError, ValueError):
                raise LiveError(502, "Couldn't read the page") from None
            if not isinstance(out, dict):
                raise LiveError(502, "Couldn't read the page")
            return out
        raise LiveError(502, "Couldn't read the page")

    @staticmethod
    def _clean_control(c) -> dict | None:
        if not isinstance(c, dict):
            return None
        n, kind, label = c.get("n"), c.get("kind"), c.get("label")
        if _int(n, 1, 10_000) is None or kind not in _KINDS or not isinstance(label, str):
            return None
        out = {"n": n, "kind": kind, "label": plain_text(label)[:80]}
        if kind == "link" and isinstance(c.get("href"), str):
            out["href"] = c["href"][:2000]
        if c.get("password") is True:
            out["password"] = True
            out["label"] = out["label"] or "password field"
        elif kind == "field" and c.get("filled") is True:
            out["filled"] = True  # whether it holds text, never what the text is
        if kind == "list":
            opts = c.get("options")
            out["options"] = [plain_text(o)[:80] for o in opts if isinstance(o, str)][:20] \
                if isinstance(opts, list) else []
        return out

    def _href(self, w: _Window):
        """The page's address and load state now, or None if they can't be read."""
        v = self._peek(w, HREF_JS)
        return tuple(v) if isinstance(v, list) and len(v) == 2 else None

    def _await_nav(self, w: _Window, before, limit: float = 0.8) -> None:
        """After a press or follow, wait briefly for a navigation to begin (the address
        changes or the page starts loading), so the next read sees the new page. Unknown
        state ends the wait at once."""
        if before is None:
            return
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            now = self._href(w)
            if now is None or now[0] != before[0] or now[1] == "loading":
                return
            time.sleep(0.1)

    def _settle(self, w: _Window, limit: float = 5.0) -> None:
        """Wait (up to `limit` s) for the page to finish loading before it is read, so a step
        right after a navigation sees the new page's controls, not an empty one. A reply that
        isn't a readyState string ends the wait at once."""
        deadline, waited = time.monotonic() + limit, False
        while True:
            state = self._peek(w, STATE_JS)
            if state not in ("loading", "interactive") or time.monotonic() >= deadline:
                break
            waited = True
            time.sleep(0.15)
        if waited:
            time.sleep(0.3)  # let the page's own scripts draw what they draw on load

    def snapshot(self, window: str, gen: str | None = None, with_token: bool = False) -> dict:
        """The page as {title, url, text, controls}, plus `token` with with_token=True (pass it
        to act to bind the action to this snapshot). `gen`: refuse (404) unless the window is
        still that generation."""
        w = self._live(window, gen, need_on=True)
        self._settle(w)
        token = secrets.token_hex(6)
        raw = self._js(w, "read", token)
        controls = [c for c in map(self._clean_control, raw.get("controls") or []
                                   if isinstance(raw.get("controls"), list) else []) if c]
        w.token = token
        w.controls = {c["n"]: c for c in controls}
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        out = {"title": str(raw.get("title") or "")[:300], "url": str(raw.get("url") or "")[:2000],
               "text": text[:6000], "controls": controls}
        if with_token:
            out["token"] = token
        return out

    def read_text(self, window: str) -> dict:
        """The live page as {title, url, text} for Ask and Make a page: up to MAX_PAGE_TEXT
        characters of its visible text (never a field's value). Unlike snapshot it marks no
        controls, so a Click through running on the page keeps its numbers."""
        w = self._live(window, need_on=True)
        raw = self._js(w, "text", "", None, MAX_PAGE_TEXT)
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        return {"title": str(raw.get("title") or "")[:300], "url": str(raw.get("url") or "")[:2000],
                "text": text[:MAX_PAGE_TEXT]}

    def resize(self, window: str, width, height) -> None:
        """The window's view changed size: the page's viewport and the screencast follow."""
        w = self._live(window, need_on=True)
        width, height = self._dim(width, 200, 3840), self._dim(height, 150, 2160)
        self._resize(w, width, height)

    def _recheck(self, w: _Window, n: int, ctl: dict, op: str = "find") -> dict:
        """Find control n again; refuse unless it is still the control the snapshot listed."""
        now = self._js(w, op, w.token or "", n)
        label = now.get("label")
        same = (now.get("ok") is True and now.get("kind") == ctl["kind"]
                and isinstance(label, str) and plain_text(label)[:80] == ctl["label"]
                and now.get("href") == ctl.get("href")
                and bool(now.get("password")) == bool(ctl.get("password")))
        if not same:
            raise LiveError(409, STALE)
        return now

    def _aim(self, w: _Window, n: int, ctl: dict) -> dict:
        """Before a press: in one step in the page, find control n again, check it is shown
        and that it is what a click at its centre lands on. A page that lays something over
        it (say a transparent frame from another site) gets COVERED, and nothing is clicked.
        If bringing it into view moved it, the page's scroll handlers get to run and it is
        aimed at once more, so an overlay moved in on that scroll is seen."""
        for attempt in (0, 1):
            now = self._recheck(w, n, ctl, "aim")
            if now.get("clear") is not True:
                raise LiveError(409, COVERED)
            if now.get("moved") is not True:
                return now
            if attempt == 0:
                time.sleep(0.1)  # let the page react to the scroll before the last look
        raise LiveError(409, COVERED)

    def act(self, window: str, action: dict, gen: str | None = None,
            token: str | None = None) -> dict:
        """Carry out one agent action: {action: scroll, dir: up|down} | {action: follow|press,
        n} | {action: type, n, text} | {action: choose, n, option}. Only n, text and option
        are taken from the action; everything else comes from the last snapshot."""
        if not isinstance(action, dict):
            raise LiveError(400, "Bad action")
        w = self._live(window, gen, need_on=True)
        if token is not None and token != w.token:
            raise LiveError(409, STALE)  # read again since the snapshot this action came from
        what = action.get("action")
        if what == "scroll":
            d = action.get("dir")
            if d not in ("up", "down"):
                raise LiveError(400, "Bad scroll direction")
            self._js(w, "scroll", w.token or "", None, -1 if d == "up" else 1)
            return {"done": "scroll"}
        if what not in ("follow", "press", "type", "choose"):
            raise LiveError(400, "Unknown action")
        n = _int(action.get("n"), 1, 10_000)
        ctl = w.controls.get(n) if n is not None else None
        if ctl is None:
            raise LiveError(400, "No such control; read the page again")
        if what == "follow":
            href = ctl.get("href") or ""
            if ctl["kind"] != "link" or urllib.parse.urlsplit(href).scheme not in ("http", "https") \
                    or not self._allowed(href):
                raise LiveError(400, "That link can't be followed")
            self._recheck(w, n, ctl)
            before = self._href(w)
            self._navigate(w, href)
            self._await_nav(w, before)
            return {"done": "follow", "url": href}
        if ctl.get("password"):
            if what == "type":
                raise LiveError(403, "Primer never types into a password field")
            raise LiveError(403, "Primer doesn't touch password fields")
        try:
            if what == "press":
                before = self._href(w)
                now = self._aim(w, n, ctl)
                x, y = _number(now.get("x")), _number(now.get("y"))
                if x is None or y is None or not (0 <= x <= w.width and 0 <= y <= w.height):
                    raise LiveError(409, STALE)
                # straight after the aim, and with no hover move first: a move would fire
                # mouseover on the control and tell the page exactly when to slip a frame
                # over it. The press is hit-tested by the browser before any page script runs.
                for t, extra in (("mousePressed", {"button": "left", "clickCount": 1}),
                                 ("mouseReleased", {"button": "left", "clickCount": 1})):
                    w.cdp.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, **extra},
                               session=w.session, timeout=5)
                self._await_nav(w, before)
                return {"done": "press", "label": ctl["label"]}
            if what == "type":
                text = action.get("text")
                # text with hidden characters is refused, not cleaned: what was shown is typed
                if ctl["kind"] != "field" or not isinstance(text, str) or not text \
                        or len(text) > MAX_ACT_TEXT or plain_text(text) != text:
                    raise LiveError(400, "Bad text to type")
                self._recheck(w, n, ctl)
                # the password check and the typing happen in one evaluate in the page, so no
                # page script can swap the field in between
                if self._js(w, "insert", w.token or "", n, text).get("ok") is not True:
                    raise LiveError(409, STALE)
                return {"done": "type", "label": ctl["label"]}
            option = action.get("option")  # choose
            if ctl["kind"] != "list" or not isinstance(option, str) \
                    or option not in ctl.get("options", []):
                raise LiveError(400, "No such option")
            self._recheck(w, n, ctl)
            if self._js(w, "choose", w.token or "", n, option).get("ok") is not True:
                raise LiveError(409, STALE)
            return {"done": "choose", "label": ctl["label"], "option": option}
        except CdpError as e:
            raise LiveError(502, f"The page didn't take the action: {e}") from None

    # --- forgetting and stopping -----------------------------------------------------------

    def _drop_service_workers(self) -> None:
        """Remove service workers registered in the profile by earlier versions (the browser
        isn't running): service workers don't run in live pages. Sign-ins stay."""
        for d in self.profile.glob("*/Service Worker"):
            shutil.rmtree(d, ignore_errors=True)

    def _sweep_forgotten(self) -> None:
        for p in self.var_dir.glob("browser-forget-*"):
            shutil.rmtree(p, ignore_errors=True)

    def forget(self) -> None:
        """Stop the browser, then remove its profile (sign-ins, cookies, history). The profile
        is renamed away first, so it is either all there or all gone, never half-deleted."""
        with self._start_lock:
            self._stop_browser("Forgotten")
            if not self.profile.exists():
                return
            aside = self.var_dir / f"browser-forget-{secrets.token_hex(4)}"
            deadline = time.monotonic() + 10
            while True:
                try:
                    os.replace(self.profile, aside)
                    break
                except FileNotFoundError:
                    return
                except OSError as e:  # Windows: a file still held for a moment after exit
                    if time.monotonic() > deadline:
                        raise LiveError(500, f"Couldn't remove the browser's profile: {e}") from None
                    time.sleep(0.2)
            shutil.rmtree(aside, ignore_errors=True)

    def shutdown(self) -> None:
        self._stop_ev.set()
        with self._start_lock:
            self._closed = True
            self._stop_browser("Primer stopped")
        deadline = time.monotonic() + POOL_JOIN_S  # daemon threads: a stuck one is left
        for pool in (self._guard_pool, self._cdp_pool, self._worker_pool):
            pool.shutdown(max(0.0, deadline - time.monotonic()))
