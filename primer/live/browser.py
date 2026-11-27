"""Find an installed Chrome or Edge, start it headless with Primer's own profile, and stop it.

Nothing is downloaded. The DevTools port is one Chrome picks (--remote-debugging-port=0) and
reads back from <profile>/DevToolsActivePort; the connection is to 127.0.0.1 only, and
--remote-allow-origins is never passed, so web pages can't reach the control port."""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from primer import children
from primer.live.cdp import Cdp
from primer.live.ws import WebSocket

log = logging.getLogger("primer.live.browser")

START_TIMEOUT_S = 15.0
STOP_WAIT_S = 5.0
PORT_FILE = "DevToolsActivePort"

_WIN_PLACES = (  # Chrome first, then Edge, in each install root
    ("Chrome", ("Google", "Chrome", "Application", "chrome.exe")),
    ("Edge", ("Microsoft", "Edge", "Application", "msedge.exe")),
)
_WIN_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
_MAC_APPS = (
    ("Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
)
_UNIX_NAMES = (
    ("Chrome", ("google-chrome", "google-chrome-stable")),
    ("Chromium", ("chromium", "chromium-browser")),
    ("Edge", ("microsoft-edge", "microsoft-edge-stable")),
)


class BrowserError(Exception):
    pass


def _name_for(path: str) -> str:
    low = os.path.basename(path).lower()
    if "edge" in low:
        return "Edge"
    if "chromium" in low:
        return "Chromium"
    return "Chrome"


def _win_file_version(path: str) -> str:
    """The file's version resource (e.g. "158.0.7000.12"), or "" if it can't be read."""
    try:
        import ctypes
        from ctypes import wintypes
        ver = ctypes.WinDLL("version", use_last_error=True)
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ""
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)) or n.value < 52:
            return ""
        words = (wintypes.DWORD * 13).from_address(ptr.value)  # VS_FIXEDFILEINFO
        if words[0] != 0xFEEF04BD:
            return ""
        ms, ls = words[2], words[3]  # dwFileVersionMS, dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return ""


def _version(path: str) -> str:
    """The browser's version string. On Windows from the file (running chrome.exe --version
    there opens a browser instead of printing); elsewhere from --version."""
    if sys.platform == "win32":
        return _win_file_version(path)
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+(?:\.\d+)+", out or "")
    return m.group(0) if m else ""


def _label(path: str, version) -> str:
    v = version(path) or ""
    major = v.split(".", 1)[0] if v and v.split(".", 1)[0].isdigit() else ""
    return f"{_name_for(path)} {major}".strip()


def _candidates(env, platform: str, which) -> list[str]:
    out: list[str] = []
    if platform == "win32":
        for _name, parts in _WIN_PLACES:
            for root in _WIN_ROOTS:
                base = env.get(root)
                if base:
                    out.append(os.path.join(base, *parts))
    elif platform == "darwin":
        home = env.get("HOME", "")
        for _name, rel in _MAC_APPS:
            out.append(posixpath.join("/Applications", rel))
            if home:
                out.append(posixpath.join(home, "Applications", rel))
    else:
        for _name, names in _UNIX_NAMES:
            for n in names:
                found = which(n)
                if found:
                    out.append(found)
    return out


def find_browser(cfg_path: str = "", *, env=None, platform: str | None = None,
                 isfile=os.path.isfile, which=shutil.which, version=_version
                 ) -> tuple[str, str] | None:
    """(path, label) for the browser to use, e.g. ("C:/.../chrome.exe", "Chrome 158"), or None.

    A configured path is used as given (or not at all, if it isn't a file); otherwise Chrome,
    then Edge, in the standard install places."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if cfg_path:
        return (cfg_path, _label(cfg_path, version)) if isfile(cfg_path) else None
    for path in _candidates(env, platform, which):
        if isfile(path):
            return path, _label(path, version)
    return None


SAFETY_PREFS = {"net": {"network_prediction_options": 2}}  # no preconnect or DNS prefetch


def _write_safety_prefs(profile: Path) -> None:
    """Turn off network prediction in the profile's Preferences before the browser starts.

    `<link rel=preconnect>` and DNS prefetch open connections that never reach the Fetch
    domain; no command-line switch stopped them in Chrome 158, this preference did."""
    path = profile / "Default" / "Preferences"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    net = data.get("net") if isinstance(data.get("net"), dict) else {}
    net.update(SAFETY_PREFS["net"])
    data["net"] = net
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _same_exe(found: str, path: str) -> bool:
    return os.path.normcase(os.path.abspath(found)) == os.path.normcase(os.path.abspath(path))


def _has_profile(cmdline: str, profile: Path) -> bool:
    """True if the command line passes --user-data-dir=<profile> (quoted or not)."""
    flat = os.path.normcase(cmdline.replace('"', ""))
    want = os.path.normcase(f"--user-data-dir={profile}")
    i = flat.find(want)
    while i >= 0:
        after = flat[i + len(want):i + len(want) + 1]
        if after in ("", " ", "\t"):
            return True
        i = flat.find(want, i + 1)
    return False


class Browser:
    """One headless browser process with Primer's own profile directory.

    On Windows chrome.exe may relaunch itself and exit 0 at once, so the process we Popen is
    not always the browser. The browser's pid comes from SystemInfo.getProcessInfo; a handle to
    it is opened and kept, and the pid is accepted only if, through that handle, its exe is the
    configured browser, it was created after we launched it, and its command line carries our
    --user-data-dir. Only then is it recorded (ledger, job) or ever killed, and the kill goes
    through the same handle. Nothing is ever killed by tree or by name."""

    def __init__(self, path: str, profile_dir, *, popen=subprocess.Popen,
                 connect=WebSocket.connect, timeout: float = START_TIMEOUT_S,
                 open_process=None, now_stamp=None):
        self.path = str(path)
        self.profile_dir = Path(profile_dir)
        self._popen = popen
        self._connect = connect
        self._timeout = timeout
        self._open_process = open_process or children.open_process
        self._now_stamp = now_stamp or children.now_stamp
        self.proc: subprocess.Popen | None = None
        self.cdp: Cdp | None = None
        self.pid: int | None = None  # the verified browser process
        self._handle = None
        self._ident: tuple[str, str] | None = None
        self._launched_at: int | None = None

    def command(self, proxy_port: int) -> list[str]:
        return [self.path, "--headless=new", "--remote-debugging-port=0",
                f"--user-data-dir={self.profile_dir}", "--no-first-run",
                "--no-default-browser-check",
                # safety only (Ruling F): requestWindow() would hand the page a scriptable
                # window, like a pop-up's first document; this is the Chrome 158 feature name
                "--disable-features=DocumentPictureInPictureAPI",
                # safety only (Ruling F): every connection goes through Primer's guarding proxy
                # (proxy.py), loopback names included, and nothing goes around it over UDP
                f"--proxy-server=http://127.0.0.1:{int(proxy_port)}",
                "--proxy-bypass-list=<-loopback>", "--disable-quic"]

    def start(self, proxy_port: int) -> Cdp:
        if not isinstance(proxy_port, int) or not 0 < proxy_port < 65536:
            raise BrowserError("the browser needs the guarding proxy's port")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port_file = self.profile_dir / PORT_FILE
        try:
            port_file.unlink()  # a stale file from an earlier run would point at a dead port
        except FileNotFoundError:
            pass
        except OSError as e:
            raise BrowserError(f"cannot clear {PORT_FILE}: {e}") from e
        _write_safety_prefs(self.profile_dir)
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        stamp = self._now_stamp()
        self._launched_at = stamp - 2 * (10_000_000 if sys.platform == "win32" else 100) \
            if stamp is not None else None  # a little slack for clock granularity
        try:
            self.proc = self._popen(self.command(proxy_port), **kwargs)
        except OSError as e:
            raise BrowserError(f"cannot start the browser: {e}") from e
        children.adopt(self.proc, "browser")  # dies with Primer, however Primer ends
        try:
            port, path = self._wait_for_port(port_file)
            ws = self._connect(f"ws://127.0.0.1:{port}{path}", timeout=10.0)
            self.cdp = Cdp(ws)
            self._find_pid()
        except Exception:
            self.stop()
            raise
        return self.cdp

    def _find_pid(self) -> None:
        """Find, verify and hold the browser process (see the class docstring)."""
        pid = None
        try:
            info = self.cdp.call("SystemInfo.getProcessInfo", timeout=5).get("processInfo") or []
            pid = next((int(p["id"]) for p in info
                        if isinstance(p, dict) and p.get("type") == "browser"), None)
        except Exception:
            log.debug("SystemInfo.getProcessInfo failed", exc_info=True)
        if pid is None and self.proc is not None and self.proc.poll() is None:
            pid = self.proc.pid
        if pid is None or pid <= 0:
            return
        h = self._open_process(pid)
        if h is None:
            return
        ok, why = False, ""
        try:
            ident = h.identity()
            cmd = h.command_line() or ""
            if ident is None:
                why = "not running"
            elif not _same_exe(ident[0], self.path):
                why = f"its exe is {ident[0]}"
            elif self._launched_at is None or int(ident[1]) < self._launched_at:
                why = "it is older than our launch"
            elif not _has_profile(cmd, self.profile_dir):
                why = "its command line lacks our profile"
            elif self.cdp is None or self.cdp.closed.is_set():
                why = "the connection closed"
            else:
                ok = True
        except Exception as e:
            why = str(e)
        if not ok:
            log.warning("not tracking pid %s as the browser: %s", pid, why)
            h.close()
            return
        self._handle, self._ident, self.pid = h, ident, pid
        children.adopt_handle(h, ident, "browser")

    def _wait_for_port(self, port_file: Path) -> tuple[int, str]:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            code = self.proc.poll() if self.proc is not None else None
            if code:  # 0 is a launcher handing off to the browser it started
                raise BrowserError(f"the browser exited (code {code})")
            try:
                lines = port_file.read_text(encoding="utf-8").splitlines()
            except (FileNotFoundError, PermissionError, UnicodeDecodeError):
                lines = []
            if len(lines) >= 2:  # written whole once both lines are there
                port, path = lines[0].strip(), lines[1].strip()
                if not port.isdigit() or not 0 < int(port) < 65536:
                    raise BrowserError(f"bad port in {PORT_FILE}")
                if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]{1,100}", path):
                    raise BrowserError(f"bad path in {PORT_FILE}")
                return int(port), path
            time.sleep(0.05)
        raise BrowserError(f"the browser did not open its DevTools port within "
                           f"{self._timeout:g}s (is another copy using Primer's profile?)")

    def _browser_running(self) -> bool:
        if self._handle is not None:
            return not self._handle.wait(0)
        return self.proc is not None and self.proc.poll() is None

    def alive(self) -> bool:
        return self.cdp is not None and not self.cdp.closed.is_set() and self._browser_running()

    def stop(self) -> None:
        """Browser.close first; if the verified browser is still there after STOP_WAIT_S, kill
        that one process through its held handle (after checking it is still the same one).
        Its renderers and GPU process exit with it; any that don't are left alone."""
        cdp, self.cdp = self.cdp, None
        if cdp is not None:
            if not cdp.closed.is_set():
                try:
                    cdp.call("Browser.close", timeout=3)
                except Exception:
                    pass
            cdp.close()
        h, ident, pid = self._handle, self._ident, self.pid
        self._handle = None
        if h is not None:
            try:
                if not h.wait(STOP_WAIT_S):
                    if h.terminate(ident):
                        h.wait(STOP_WAIT_S)
                    else:
                        log.warning("pid %s is no longer the browser; not killing it", pid)
            finally:
                h.close()
                children.release(pid)
        proc = self.proc
        if proc is not None:  # the launcher: our own child, held by Popen's handle
            try:
                proc.wait(0.1 if h is not None else STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(STOP_WAIT_S)
                except (OSError, subprocess.TimeoutExpired):
                    log.warning("launcher pid %s did not exit", proc.pid)
            children.release(proc.pid)
