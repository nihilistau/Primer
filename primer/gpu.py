"""GPU gate: page writing and image jobs take turns on one GPU.

In "auto" mode one holder runs at a time, waiting pages go before waiting images, an image
pauses the text servers and a page resumes them. Consecutive images keep the text paused;
release_idle() resumes it once nobody holds or waits for the gate. In "manual" mode the
user runs things side by side, so the gate only tracks state."""
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
        self._error: str | None = None

    def page(self) -> "_Turn":
        """Held while a page's text is generated."""
        return _Turn(self, "page")

    def image(self) -> "_Turn":
        """Held for one image job."""
        return _Turn(self, "image")

    def state(self) -> dict:
        with self._cond:
            # manual mode lets both run at once; then the holder reads "image"
            holder = "image" if self._active["image"] else "page" if self._active["page"] else None
            return {"mode": self.mode, "holder": holder, "text_paused": self._text_paused,
                    "waiting_pages": self._waiting["page"], "waiting_images": self._waiting["image"],
                    "error": self._error}

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
