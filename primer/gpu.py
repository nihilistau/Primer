"""GPU gate: page writing and image jobs take turns on one GPU.

In "auto" mode one holder runs at a time, waiting pages go before waiting images, an image
pauses the text servers and a page resumes them. Consecutive images keep the text paused;
release_idle() resumes it once nobody holds or waits for the gate. In "manual" mode the
user runs things side by side, so the gate only tracks state.

background() hands out a pre-emptible turn (speculative drafting) only when the gate is
completely idle; it never pauses or resumes the text itself, and its holder steps aside as soon
as a real page or picture asks for the gate."""
from __future__ import annotations

import logging
import threading
from typing import Callable, Literal

log = logging.getLogger(__name__)

Kind = Literal["page", "image"]


class GpuGate:
    def __init__(self, mode: Literal["auto", "manual"], pause_text: Callable[[], None],
                 resume_text: Callable[[], None]):
        self.mode = mode
        self._pause_text = pause_text
        self._resume_text = resume_text
        self._cond = threading.Condition()
        self._active = {"page": 0, "image": 0}
        self._waiting = {"page": 0, "image": 0}
        self._text_paused = False
        self._switching = False  # release_idle() is resuming the text outside the lock
        self._background = 0  # background turns held (also counted in _active["page"])
        self._error: str | None = None

    def page(self) -> "_Turn":
        """Held while a page's text is generated."""
        return _Turn(self, "page")

    def image(self) -> "_Turn":
        """Held for one image job."""
        return _Turn(self, "image")

    def background(self) -> "_BackgroundTurn | None":
        """A pre-emptible page turn for work nobody asked for yet, or None unless the gate is
        completely idle: nothing held or waiting, no switch running and (auto mode) the text
        not paused for pictures. It never pauses or resumes the text on its own account. While
        held it counts as a page holder, so real pages and pictures queue behind it as usual;
        the holder checks preempted() / wait_preempted() and releases promptly."""
        with self._cond:
            if (self._switching or any(self._active.values()) or any(self._waiting.values())
                    or (self.mode == "auto" and self._text_paused)):
                return None
            self._active["page"] += 1
            self._background += 1
        return _BackgroundTurn(self)

    def state(self) -> dict:
        with self._cond:
            # manual mode lets both run at once; then the holder reads "image"
            holder = "image" if self._active["image"] else "page" if self._active["page"] else None
            return {"mode": self.mode, "holder": holder, "text_paused": self._text_paused,
                    "waiting_pages": self._waiting["page"], "waiting_images": self._waiting["image"],
                    "error": self._error,
                    # a draft (thinking ahead) holds the gate: the UI says so, not "writing a page"
                    "background": self._background > 0}

    def release_idle(self) -> None:
        """Resume the text when nobody holds or waits for the gate (ImageService calls this
        when its queue drains)."""
        if self.mode != "auto":
            return
        with self._cond:
            if (self._switching or not self._text_paused or any(self._active.values())
                    or any(self._waiting.values())):
                return
            self._switching = True  # keeps everyone out until the resume has finished
        try:
            self._call(self._resume_text, "resume", paused_after=False)
        finally:
            with self._cond:
                self._switching = False
                self._cond.notify_all()

    # internals
    def _free_for(self, kind: Kind) -> bool:
        if self._switching or any(self._active.values()):
            return False
        return kind == "page" or self._waiting["page"] == 0  # pages first

    def _acquire(self, kind: Kind, blocking: bool, queued: bool = False) -> bool:
        """Take a turn. A non-blocking miss leaves the turn counted as waiting (queued), so
        pages-first priority holds until it is entered or cancelled; any exception leaves
        nothing counted."""
        with self._cond:
            if not queued:
                self._waiting[kind] += 1
                self._cond.notify_all()  # a background holder steps aside for any waiter
            if self.mode == "auto" and not self._free_for(kind):
                if not blocking:
                    return False
                try:
                    self._cond.wait_for(lambda: self._free_for(kind))
                except BaseException:
                    self._waiting[kind] -= 1
                    self._cond.notify_all()
                    raise
            self._waiting[kind] -= 1
            self._active[kind] += 1
            if self.mode != "auto":
                self._cond.notify_all()  # manual: a background holder steps aside for it
                return True
            # As the holder, nobody else starts until the switch below is done, so a page
            # never runs while the text is still paused (unless resuming failed).
            switch = None
            if kind == "page" and self._text_paused:
                switch = (self._resume_text, "resume", False)
            elif kind == "image" and not self._text_paused:
                switch = (self._pause_text, "pause", True)
        if switch:
            try:
                self._call(*switch)
            except BaseException:  # e.g. KeyboardInterrupt: don't leave the gate held
                self._release(kind)
                raise
        return True

    def _unqueue(self, kind: Kind) -> None:
        with self._cond:
            self._waiting[kind] -= 1
            self._cond.notify_all()  # a waiting image may be free now

    def _release(self, kind: Kind) -> None:
        with self._cond:
            self._active[kind] -= 1
            self._cond.notify_all()

    def _preempted(self) -> bool:
        """Something real wants the GPU (caller holds the lock). In manual mode turns run side
        by side, so a real turn running next to the background one counts too."""
        if any(self._waiting.values()):
            return True
        others = self._active["page"] - self._background + self._active["image"]
        return self.mode != "auto" and others > 0

    def _call(self, fn: Callable[[], None], what: str, paused_after: bool) -> None:
        """Run pause_text/resume_text without holding the lock; errors are logged and
        recorded, never raised (the paused flag then keeps its old value, so it's retried)."""
        try:
            fn()
        except Exception as e:
            log.exception("%s_text failed", what)
            with self._cond:
                self._error = f"{what} text servers failed: {e}"
            return
        with self._cond:
            self._text_paused = paused_after
            self._error = None


class _Turn:
    """One turn at the gate, used as a context manager. try_enter() takes it without
    waiting, so a caller can tell the user it is about to wait first; a miss queues the
    turn, and the caller then either enters it (waiting) or cancel()s it."""

    def __init__(self, gate: GpuGate, kind: Kind):
        self._gate, self._kind = gate, kind
        self._state: Literal["idle", "queued", "held"] = "idle"

    def try_enter(self) -> bool:
        if self._state != "held":
            self._take(blocking=False)
        return self._state == "held"

    def cancel(self) -> None:
        """Give up a queued turn without entering it. Idempotent; no-op unless queued."""
        if self._state == "queued":
            self._state = "idle"
            self._gate._unqueue(self._kind)

    def __enter__(self) -> "_Turn":
        if self._state != "held":
            self._take(blocking=True)
        return self

    def __exit__(self, *exc) -> None:
        if self._state == "held":
            self._state = "idle"
            self._gate._release(self._kind)

    def _take(self, blocking: bool) -> None:
        queued = self._state == "queued"
        try:
            got = self._gate._acquire(self._kind, blocking, queued=queued)
        except BaseException:
            self._state = "idle"  # _acquire leaves nothing counted when it raises
            raise
        self._state = "held" if got else "queued"


class _BackgroundTurn:
    """A held, pre-emptible turn from GpuGate.background(). Callers must use it as a context
    manager (`with gate.background() as bg:` when not None) so it is released on every path,
    exceptions included; release() and leaving the block are both idempotent."""

    def __init__(self, gate: GpuGate):
        self._gate = gate
        self._held = True  # guarded by the gate's lock
        self._requested = False  # adopted by a real request: not background, not pre-emptible

    def preempted(self) -> bool:
        """True while held, not requested, and a real page or picture is waiting (manual mode:
        or running)."""
        with self._gate._cond:
            return self._is_preempted()

    def wait_preempted(self, timeout: float) -> bool:
        """Block until preempted(), the turn is released, or `timeout` seconds pass; returns
        preempted()."""
        gate = self._gate
        with gate._cond:
            gate._cond.wait_for(lambda: not self._held or self._is_preempted(), timeout)
            return self._is_preempted()

    def set_requested(self, requested: bool) -> None:
        """A real request took the work over (True), or gave it back (False). While requested
        the turn is an ordinary page holder: state()["background"] is false and it is never
        pre-empted. No-op once released."""
        gate = self._gate
        with gate._cond:
            if not self._held or self._requested == requested:
                return
            self._requested = requested
            gate._background += -1 if requested else 1
            gate._cond.notify_all()  # a watcher re-checks for waiters it now has to yield to

    def release(self) -> None:
        gate = self._gate
        with gate._cond:
            if not self._held:
                return
            self._held = False
            if not self._requested:
                gate._background -= 1
            gate._active["page"] -= 1
            gate._cond.notify_all()

    def _is_preempted(self) -> bool:  # caller holds the gate's lock
        return self._held and not self._requested and self._gate._preempted()

    def __enter__(self) -> "_BackgroundTurn":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
