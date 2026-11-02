"""Launch, health-check and stop the model servers a tier manages."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from primer import children
from primer.config import REPO_ROOT, EngineSpec, ReferenceConfig, Tier
from primer.engines import make_engine
from primer.grounding.kiwix import KiwixClient

POLL_S = 0.5
STOP_WAIT_S = 5
STOP_CMD_TIMEOUT_S = 30
LAUNCHER_NOTE = "launched via a launcher; no stop command configured"


def _port(base_url: str) -> int | None:
    try:
        return urlsplit(base_url).port
    except ValueError:
        return None


class ManagedServer:
    def __init__(self, role: str, spec: EngineSpec, health: Callable[[], bool] | None = None):
        self.role = role
        self.spec = spec
        engine = None if health else make_engine(spec)
        self._health = health or engine.health
        self._engine = engine
        self.proc: subprocess.Popen | None = None
        self._detached = False  # the launch command exited 0 and its server then became healthy
        self._state = "stopped"
        self._reason = ""
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def _unhealthy_reason(self, base: str) -> str:
        """`base`, plus what the engine learned about why health never passed (e.g. HTTP 401)."""
        detail = getattr(self._engine, "last_error", "")
        return f"{base} ({detail})" if detail else base

    def _set(self, state: str, reason: str = "", ev: threading.Event | None = None) -> None:
        """Set state; a run that was cancelled (or superseded) by stop() leaves it alone."""
        with self._lock:
            if ev is not None and ev.is_set():
                return
            self._state, self._reason = state, reason

    def _running(self) -> bool:
        """Is a server we launched still up? (our process alive, or a detached server still healthy)"""
        with self._lock:
            proc, detached = self.proc, self._detached
        if proc is not None and proc.poll() is None:
            return True
        return detached and self._health()

    def start(self, wait: bool = True) -> None:
        with self._lock:
            ready = self._state == "ready"
        running = self._running() if ready else False  # health check stays outside the lock
        with self._lock:
            if self._state == "starting" or (self._state == "ready" and running):
                return
            ev = self._cancel = threading.Event()
            self._state, self._reason = "starting", ""
            if not wait:
                self._thread = threading.Thread(target=self._startup, args=(ev,), daemon=True)
                self._thread.start()
                return
        self._startup(ev)

    def _startup(self, ev: threading.Event) -> None:
        try:
            self._run(ev)
        except Exception as e:  # a failing health callable must not leave us "starting"
            self._set("failed", f"startup error: {e}", ev)

    def _run(self, ev: threading.Event) -> None:
        if self._health():
            self._set("external", "", ev)
            return
        if not self.spec.managed:
            self._set("stopped", self._unhealthy_reason("not managed and not running"), ev)
            return
        if not self.spec.launch:
            self._set("failed", "managed but no launch command configured", ev)
            return
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        with self._lock:  # cancel-check and launch are atomic with respect to stop()
            if ev.is_set():
                return
            self._detached = False
            try:
                self.proc = subprocess.Popen(self.spec.launch, cwd=REPO_ROOT, creationflags=flags)
            except FileNotFoundError:
                self._state, self._reason = "failed", f"launch command not found: {self.spec.launch[0]}"
                return
            except OSError as e:
                self._state, self._reason = "failed", f"launch failed: {e}"
                return
            children.adopt(self.proc, self.role, _port(self.spec.base_url))  # dies with Primer
            proc = self.proc
        deadline = time.monotonic() + self.spec.ready_timeout_s
        while time.monotonic() < deadline:
            if ev.is_set():
                self._kill(proc)  # stop() may have given up waiting for us
                return
            code = proc.poll()
            if code is not None and code != 0:
                children.release(proc.pid)
                self._set("failed", f"process exited with code {code}", ev)
                return
            if self._health():
                if code is None:  # a launcher may still be on its way out
                    try:
                        code = proc.wait(1)
                    except subprocess.TimeoutExpired:
                        pass
                if code == 0:  # a launcher, now gone: what it started isn't ours to track
                    children.release(proc.pid)
                with self._lock:
                    if code == 0 and not ev.is_set():
                        self._detached = True
                self._set("ready", "", ev)
                return
            ev.wait(POLL_S)
        self._kill(proc)
        if proc.poll() == 0:
            base = (f"not healthy after {self.spec.ready_timeout_s:g}s "
                    "(launcher exited; server never became healthy)")
        else:
            base = f"not healthy after {self.spec.ready_timeout_s:g}s"
        self._set("failed", self._unhealthy_reason(base), ev)

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        children.release(proc.pid)

    def stop(self) -> None:
        with self._lock:
            if self._state == "external":
                return
            ev = self._cancel
            ev.set()
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(STOP_WAIT_S + POLL_S + 1)
        with self._lock:
            proc, detached = self.proc, self._detached
            was_ready, prior = self._state == "ready", self._reason if self._state == "failed" else ""
        detached_up = detached and self._health()  # outside the lock
        exited_zero = proc is not None and proc.poll() == 0
        self._kill(proc)
        problem = ""
        if proc is not None and self.spec.stop and not (detached and not detached_up):
            problem = self._run_stop_command()  # the launcher may have left a server behind
        elif detached_up:
            problem = LAUNCHER_NOTE
        elif exited_zero and not detached:
            problem = "process had exited"
        with self._lock:
            if self._cancel is not ev:
                return  # a start() began a newer run while we were stopping: it owns the state
            if problem and was_ready and detached_up:
                self._reason = problem  # still running; the user needs to know
                return
            self.proc, self._detached = None, False
            self._state = "stopped"
            self._reason = "; ".join(r for r in (prior, problem) if r)

    def pause(self) -> None:
        """Give the GPU up for an image. With a pause command (e.g. `lms unload --all`) run it
        and leave the server up; the model loads again on the next request. Without one,
        stop(). Raises RuntimeError when the pause command fails."""
        if not self.spec.pause:
            self.stop()
            return
        problem = self._run_command(self.spec.pause, "pause")
        if problem:
            raise RuntimeError(problem)

    def _run_stop_command(self) -> str:
        return self._run_command(self.spec.stop, "stop")

    @staticmethod
    def _run_command(cmd: tuple[str, ...], what: str) -> str:
        """Run a configured stop/pause command; returns "" on success, else what went wrong."""
        try:
            done = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                                  timeout=STOP_CMD_TIMEOUT_S)
        except FileNotFoundError:
            return f"{what} command not found: {cmd[0]}"
        except subprocess.TimeoutExpired:
            return f"{what} command timed out after {STOP_CMD_TIMEOUT_S}s"
        except OSError as e:
            return f"{what} command failed: {e}"
        return "" if done.returncode == 0 else f"{what} command exited with code {done.returncode}"

    def status(self) -> dict:
        with self._lock:
            state, reason = self._state, self._reason
        pid = self.proc.pid if self.proc is not None and self.proc.poll() is None else None
        return {"role": self.role, "state": state, "base_url": self.spec.base_url,
                "model": self.spec.model, "pid": pid, "reason": reason}


def reference_spec(ref: ReferenceConfig) -> EngineSpec:
    """kiwix-serve as an EngineSpec (only launch, stop and base_url matter: health is the catalog)."""
    # normpath: kiwix-serve on Windows can't open a ZIM given as D:/x/y.zim
    launch = (ref.kiwix_serve, "--address=127.0.0.1", f"--port={ref.port}", *map(os.path.normpath, ref.zims)) if ref.managed else ()
    return EngineSpec("openai", ref.url, managed=ref.managed, launch=launch, ready_timeout_s=60.0)


class ServerManager:
    def __init__(self, tier: Tier):
        self._servers: dict[str, ManagedServer] = {"text": ManagedServer("text", tier.text)}
        if tier.fast == tier.text:
            self._servers["fast"] = self._servers["text"]
        else:
            self._servers["fast"] = ManagedServer("fast", tier.fast)
        if tier.reference is not None:  # never a GPU server
            spec = reference_spec(tier.reference)
            self._servers["reference"] = ManagedServer("reference", spec, health=KiwixClient(spec.base_url).health)

    @property
    def ROLES(self) -> tuple[str, ...]:
        """The roles of this tier: text, fast, and reference when it has one."""
        return tuple(self._servers)

    def _unique(self) -> list[ManagedServer]:
        return list({id(s): s for s in self._servers.values()}.values())

    def _get(self, role: str) -> ManagedServer:
        if role not in self._servers:
            raise ValueError(f"unknown role {role!r}")
        return self._servers[role]

    def gpu_servers(self) -> list[ManagedServer]:
        """The servers whose spec is marked gpu = true (the GPU gate pauses these for images)."""
        return [s for s in self._unique() if s.spec.gpu]

    def start_all(self, wait: bool = False) -> None:
        for s in self._unique():
            s.start(wait=wait)

    def stop_all(self) -> None:
        for s in self._unique():
            s.stop()

    def start(self, role: str) -> None:
        self._get(role).start(wait=False)

    def stop(self, role: str) -> None:
        self._get(role).stop()

    def statuses(self) -> list[dict]:
        return [{**self._servers[r].status(), "role": r} for r in self.ROLES]
