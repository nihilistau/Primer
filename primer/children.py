"""Model servers Primer launched must not outlive it.

Two layers:
- Windows: every launched server is put in one Job Object per Primer process, created with
  kill-on-close, so the kernel ends them however Primer dies (crash, Task Manager, console
  closed). The job also allows silent breakaway, so whatever a *launcher* starts (`lms server
  start` -> the LM Studio daemon) is not in the job and is never killed with Primer.
- Everywhere: a ledger, var/children.json, records each launched server's pid, executable and
  creation time, and the same for the Primer that launched it; a clean stop removes the
  entry. At startup, an entry whose Primer is gone but whose pid is still running the same
  executable with the same creation time is a leftover from a run that crashed, and is
  stopped (a Primer still running, e.g. a second one started by mistake, keeps its servers).
  A pid alone (or a port) is never enough: both get reused."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

LEDGER_NAME = "children.json"
REAP_WAIT_S = 5.0


# --- process identity: (exe, creation time) or None when the pid isn't running ---------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _WAIT_OBJECT_0 = 0
    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD))
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                             wintypes.DWORD)
    _k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    def _open(pid: int, access: int):
        return _k32.OpenProcess(access, False, pid) or None

    def _handle_identity(h) -> tuple[str, str] | None:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(h, *map(ctypes.byref, times)):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return os.path.normcase(buf.value), str(created)

    def identity(pid: int) -> tuple[str, str] | None:
        h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if h is None:
            return None
        try:
            return _handle_identity(h)
        finally:
            _k32.CloseHandle(h)

    def _terminate(pid: int, exe: str, created: str) -> bool:
        """Kill pid if it still is (exe, created). Holding the handle, the pid can't be reused
        between the check and the kill."""
        h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE | _SYNCHRONIZE)
        if h is None:
            return False
        try:
            if _handle_identity(h) != (exe, created):
                return False
            _k32.TerminateProcess(h, 1)
            _k32.WaitForSingleObject(h, int(REAP_WAIT_S * 1000))
            return True
        finally:
            _k32.CloseHandle(h)

else:
    def identity(pid: int) -> tuple[str, str] | None:
        """(exe, start time in clock ticks since boot) from /proc; None where there's no /proc
        (then nothing is recorded, so nothing unverifiable is ever killed)."""
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) < 20 or fields[0] == "Z":  # a zombie has already exited
            return None
        return exe, fields[19]

    def _terminate(pid: int, exe: str, created: str) -> bool:
        if identity(pid) != (exe, created):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        end = time.monotonic() + REAP_WAIT_S
        while time.monotonic() < end and identity(pid) == (exe, created):
            time.sleep(0.1)
        if identity(pid) == (exe, created):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return True


# --- Windows Job Object -------------------------------------------------------------------------

class _Job:
    """One kill-on-close job per Primer process; the handle is never closed while Primer runs,
    so the kernel closes it (and ends the job's processes) when Primer exits in any way."""

    def __init__(self):
        self._handle = None
        self._failed = False
        self._lock = threading.Lock()

    def assign(self, proc: subprocess.Popen) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._handle is None and not self._failed:
                self._handle = self._create()
                self._failed = self._handle is None
            job = self._handle
        if job is None:
            return False
        if not _k32.AssignProcessToJobObject(job, int(proc._handle)):
            log.warning("could not put pid %s in Primer's job object (error %s); the pid "
                        "ledger still covers it", proc.pid, ctypes.get_last_error())
            return False
        return True

    @staticmethod
    def _create():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            log.warning("could not create a job object (error %s)", ctypes.get_last_error())
            return None
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = (_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                                                 | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
        if not _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            log.warning("could not set up the job object (error %s)", ctypes.get_last_error())
            _k32.CloseHandle(job)
            return None
        return job


# --- the ledger ---------------------------------------------------------------------------------

class Ledger:
    def __init__(self, var_dir: Path):
        self.path = Path(var_dir) / LEDGER_NAME
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.path.name, e)
            return []
        entries = data.get("children") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("pid"), int)
                and isinstance(e.get("exe"), str) and isinstance(e.get("create_time"), str)]

    def _write(self, entries: list[dict]) -> None:
        if not entries:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"children": entries}, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _alive(e: dict) -> bool:
        return identity(e["pid"]) == (e["exe"], e["create_time"])

    @staticmethod
    def _owner_alive(e: dict) -> bool:
        owner = e.get("owner")
        if not isinstance(owner, dict) or not isinstance(owner.get("pid"), int):
            return False
        return identity(owner["pid"]) == (owner.get("exe"), owner.get("create_time"))

    def record(self, role: str, pid: int, port: int | None = None) -> bool:
        ident, me = identity(pid), identity(os.getpid())
        if ident is None or me is None:  # already gone, or not verifiable here
            return False
        entry = {"role": role, "pid": pid, "exe": ident[0], "create_time": ident[1], "port": port,
                 "owner": {"pid": os.getpid(), "exe": me[0], "create_time": me[1]}}
        with self._lock:
            entries = [e for e in self._read() if e["pid"] != pid and self._alive(e)]
            self._write(entries + [entry])
        return True

    def forget(self, pid: int) -> None:
        with self._lock:
            entries = self._read()
            kept = [e for e in entries if e["pid"] != pid and self._alive(e)]
            if kept != entries:
                self._write(kept)

    def reap(self) -> list[str]:
        """Stop the leftovers of an earlier run; returns a line for each one stopped."""
        done, kept = [], []
        with self._lock:
            for e in self._read():
                if self._owner_alive(e):  # its Primer is still running: not a leftover
                    if self._alive(e):
                        kept.append(e)
                elif _terminate(e["pid"], e["exe"], e["create_time"]):
                    done.append(f"stopped a leftover {e.get('role') or 'model'} server from the "
                                f"last run (pid {e['pid']})")
            self._write(kept)
        return done


# --- module API used by ManagedServer and serve.py -----------------------------------------------

_JOB = _Job()
_ledger: Ledger | None = None


def use_ledger(var_dir: Path | None) -> Ledger | None:
    """Record launched servers in var_dir/children.json from now on (None: stop recording)."""
    global _ledger
    _ledger = Ledger(var_dir) if var_dir is not None else None
    return _ledger


def adopt(proc: subprocess.Popen, role: str, port: int | None = None) -> None:
    """A server Primer just launched: tie it to Primer's lifetime. Never raises."""
    try:
        _JOB.assign(proc)
    except Exception:
        log.exception("job object assignment failed")
    ledger = _ledger
    if ledger is not None:
        try:
            ledger.record(role, proc.pid, port)
        except Exception:
            log.exception("could not record pid %s in the ledger", proc.pid)


def release(pid: int) -> None:
    """The server with this pid has exited or been stopped. Never raises."""
    ledger = _ledger
    if ledger is not None:
        try:
            ledger.forget(pid)
        except Exception:
            log.exception("could not update the ledger")


def reap_leftovers(var_dir: Path) -> list[str]:
    try:
        return Ledger(var_dir).reap()
    except Exception:
        log.exception("could not check for leftover model servers")
        return []
