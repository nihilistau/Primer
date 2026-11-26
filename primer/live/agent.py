"""Click through on a live page. The model may scroll, read and follow listed http(s) links on
its own; pressing, typing and choosing ask the reader first, every time. Each model call takes a
page turn on the GPU gate, as the reader's agent does (primer.browse.agent._call)."""
from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from typing import Callable

from primer.browse.agent import _alive, _call
from primer.engines import EngineError
from primer.live.prompts import MAX_TYPE, RETRY_NOTE, agent_messages, parse_reply
from primer.live.service import LiveError, plain_text

MAX_STEPS = 8
DEADLINE_S = 180      # not counting time spent waiting for the reader's answer
ASK_TIMEOUT_S = 120   # an unanswered ask counts as Stop
ASK_POLL_S = 0.25
LABEL_CHARS = 80

CLOSED = "The live page has closed"
STOPPED = "Stopped"
NO_TIME = "Ran out of time"
NO_STEPS = "Used all the steps"
NO_REPLY = "The model gave no usable reply"
BUSY = "The model is already working in this page"


def _label(c: dict) -> str:
    label = plain_text(c.get("label") or "")
    return label[:LABEL_CHARS] or f"{c.get('kind', 'control')} {c.get('n')}"


def ask_text(action: str, label: str, value: str = "") -> str:
    """The spec's copy, as plain text (the UI bolds the label and the value)."""
    if action == "press":
        return f"The model wants to press {label}"
    if action == "type":
        return f"The model wants to type '{value}' into {label}"
    return f"The model wants to choose {value} in {label}"


def _check(reply: dict, controls: dict[int, dict]) -> tuple[dict | None, str]:
    """(control, "") when the action may go ahead (free or after asking), else (None, why)."""
    a, n = reply["action"], reply.get("n")
    c = controls.get(n)
    if c is None:
        return None, f"There is no control {n} on this page."
    if a == "follow":
        href = c.get("href") or ""
        try:
            scheme = urllib.parse.urlsplit(href).scheme.lower()
        except ValueError:
            scheme = ""
        if c.get("kind") != "link" or scheme not in ("http", "https"):
            return None, f"Control {n} is not a link that can be followed."
        return c, ""
    if c.get("password"):
        if a == "type":
            return None, "Primer never types into a password field."
        return None, "Primer doesn't touch password fields."
    if a == "type":
        if c.get("kind") != "field":
            return None, f"Control {n} is not a field."
        if not reply["text"]:
            return None, "There is no text to type."
        if len(reply["text"]) > MAX_TYPE or "\x00" in reply["text"]:
            return None, f"Text to type must be at most {MAX_TYPE} characters."
    elif a == "choose":
        if c.get("kind") != "list":
            return None, f"Control {n} is not a list."
        if reply["option"] not in (c.get("options") or []):
            return None, f"That option is not in list {n}."
    return c, ""


def run_live_agent(live, window: str, goal: str, engine, gate, emit,
                   ask: Callable[[str, str], bool],
                   clock: Callable[[], float] = time.monotonic, gen: str | None = None) -> None:
    """Work toward `goal` in a live window: at most MAX_STEPS actions (free and asked alike) in
    DEADLINE_S seconds, not counting time spent waiting on `ask`.

    Events: step {n, saw, url, action, why, refused?}, ask {id, text, action, label, value},
    answer {text}, stopped {reason}, error {message}, and waiting {reason} from the GPU gate.
    `ask(id, text)` blocks until the reader answers; False means Stop (or a timeout, or the
    client gone). Ends quietly when the client has gone (emit.alive() is False; the server
    also makes it False when the window is closed).

    `gen` is the window's generation when the run started: the run reads and acts only on that
    generation (a window closed and reopened under the same name is not its window), and every
    action is bound to the snapshot it was chosen from (a later snapshot makes it stale)."""
    started, waited = clock(), 0.0
    history: list[str] = []
    steps = 0

    def late() -> bool:
        return clock() - started - waited > DEADLINE_S

    def stop(reason: str) -> None:
        emit("stopped", {"reason": reason})

    def gone() -> bool:
        return gen is not None and not live.is_open(window, gen)

    while True:
        if not _alive(emit):
            return
        if steps >= MAX_STEPS:
            return stop(NO_STEPS)
        if late():
            return stop(NO_TIME)
        try:
            snap = live.snapshot(window, gen=gen, with_token=True)
        except LiveError as e:
            if e.status == 404:
                return stop(CLOSED)
            if e.status == 403:  # live pages were turned off
                return stop(e.message)
            emit("error", {"message": e.message})
            return
        token = snap.get("token")
        controls = {c["n"]: c for c in snap.get("controls") or []}
        msgs = agent_messages(goal, snap, history)
        reply = None
        for _ in range(2):  # one retry on a reply that isn't the grammar
            try:
                raw = _call(engine, gate, msgs, emit, max_tokens=300, temperature=0.2)
            except EngineError as e:
                emit("error", {"message": str(e) or "engine error"})
                return
            if gone():  # closed (or reopened as a new window) while the model thought
                return stop(CLOSED)
            if not _alive(emit):
                return
            reply = parse_reply(raw)
            if reply is not None or late():
                break
            msgs = msgs + [{"role": "assistant", "content": raw[:500]},
                           {"role": "user", "content": RETRY_NOTE}]
        if late():
            return stop(NO_TIME)
        if reply is None:
            return stop(NO_REPLY)
        steps += 1
        a = reply["action"]
        if a == "type":
            # no hidden characters (bidi overrides, zero-width marks): the text in the ask
            # reads as what is typed, and is exactly what is typed
            reply["text"] = plain_text(reply["text"])
        saw = (snap.get("title") or snap.get("url") or "")[:120]
        base = {"n": steps, "saw": saw, "url": snap.get("url") or ""}
        if a == "answer":
            emit("step", {**base, "action": {"action": "answer"}, "why": ""})
            emit("answer", {"text": reply["text"]})
            return
        if a == "scroll":
            emit("step", {**base, "action": {"action": "scroll", "dir": reply["dir"]},
                          "why": reply["why"]})
            outcome = _act(live, window, {"action": "scroll", "dir": reply["dir"]}, gen, token)
            if outcome is None:
                return stop(CLOSED)
            history.append(f"{steps}. scroll {reply['dir']}: {outcome}")
            continue
        n = reply["n"]
        ctl, refused = _check(reply, controls)
        shown = {"action": a, "n": n}
        if ctl is not None:
            shown["label"] = _label(ctl)
            if a == "follow":
                shown["href"] = ctl.get("href") or ""
        if a == "type":
            shown["text"] = reply["text"][:MAX_TYPE]
        elif a == "choose":
            shown["option"] = reply["option"][:LABEL_CHARS]
        step = {**base, "action": shown, "why": reply["why"]}
        if ctl is None:
            emit("step", {**step, "refused": refused})
            history.append(f"{steps}. {a} {n}: refused. {refused}")
            continue
        emit("step", step)
        act = {"action": a, "n": n}
        if a in ("press", "type", "choose"):
            value = reply.get("text") if a == "type" else reply.get("option", "")
            text = ask_text(a, shown["label"], value or "")
            ask_id = secrets.token_hex(6)
            emit("ask", {"id": ask_id, "text": text, "action": a, "label": shown["label"],
                         "value": value or ""})
            t0 = clock()
            try:
                allowed = ask(ask_id, text)
            finally:
                waited += clock() - t0
            if gone():  # the window closed, crashed or lost its browser during the ask
                return stop(CLOSED)
            if not _alive(emit):
                return
            if not allowed:
                return stop(STOPPED)
            if a == "type":
                act["text"] = reply["text"]
            elif a == "choose":
                act["option"] = reply["option"]
        outcome = _act(live, window, act, gen, token)
        if outcome is None:
            return stop(CLOSED)
        history.append(f"{steps}. {a} {n}: {outcome}")


def _act(live, window: str, action: dict, gen, token) -> str | None:
    """Carry out one action; the result as a line for the model, or None if the window closed.
    A refused or stale action is told to the model rather than ending the run."""
    try:
        live.act(window, action, gen=gen, token=token)
    except LiveError as e:
        if e.status == 404:
            return None
        return f"not done. {e.message}"
    return "done"


class LiveAgents:
    """One agent at a time per live window, and its pending ask. The SSE route calls expect()
    before it writes an ask event, so an answer can't arrive before the ask exists. Each run
    has the window generation it started on and a cancel event that stop() sets."""

    def __init__(self, timeout: float = ASK_TIMEOUT_S, poll: float = ASK_POLL_S):
        self._lock = threading.Lock()
        self._running: dict[str, tuple[str | None, threading.Event]] = {}
        self._pending: dict[str, tuple[str, threading.Event, list]] = {}
        self._timeout = timeout
        self._poll = poll
        self._closed = False

    def begin(self, window: str, gen: str | None = None) -> threading.Event | None:
        """Start a run: its cancel event, or None if one is already running (or after
        shutdown)."""
        with self._lock:
            if self._closed or window in self._running:
                return None
            cancel = threading.Event()
            self._running[window] = (gen, cancel)
            return cancel

    def end(self, window: str) -> None:
        self.stop(window)
        with self._lock:
            self._running.pop(window, None)

    def running(self, window: str) -> bool:
        with self._lock:
            return window in self._running

    def expect(self, window: str, ask_id: str) -> None:
        with self._lock:
            old = self._pending.get(window)
            if old is not None:
                old[2].append(False)
                old[1].set()
            self._pending[window] = (ask_id, threading.Event(), [])

    def wait(self, window: str, ask_id: str, alive: Callable[[], bool] = lambda: True,
             clock: Callable[[], float] = time.monotonic) -> bool:
        """Block until the ask is answered: True for Allow once; False for Stop, a timeout, the
        client gone, the window closed or Primer stopping."""
        with self._lock:
            entry = self._pending.get(window)
        if entry is None or entry[0] != ask_id:
            return False
        _, ev, slot = entry
        deadline = clock() + self._timeout
        try:
            while not ev.wait(self._poll):
                if clock() >= deadline or not alive():
                    return False
            return bool(slot and slot[0])
        finally:
            with self._lock:
                if self._pending.get(window) is entry:
                    del self._pending[window]

    def answer(self, window: str, ask_id, allow: bool) -> bool:
        """Resolve the pending ask; False when there is none with this id."""
        with self._lock:
            entry = self._pending.get(window)
            if entry is None or entry[0] != ask_id or entry[1].is_set():
                return False
            entry[2].append(bool(allow))
            entry[1].set()
            return True

    def stop(self, window: str, gen: str | None = None) -> None:
        """Cancel the window's run and answer its pending ask with Stop (the window closed or
        ended, or the run finished). With `gen`, only a run started on that generation."""
        with self._lock:
            run = self._running.get(window)
            if gen is not None and run is not None and run[0] is not None and run[0] != gen:
                return  # an older window of this name ended: not this run's window
            if run is not None:
                run[1].set()
            entry = self._pending.pop(window, None)
        if entry is not None:
            entry[2].append(False)
            entry[1].set()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            windows = set(self._pending) | set(self._running)
        for w in windows:
            self.stop(w)
