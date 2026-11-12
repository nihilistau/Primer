"""Kairos drives Primer: the hub between the tool API and the open Primer tabs (Phase 15).

Kairos (the companion on this machine) posts a command (`show`, `open` or `lens`) to the
/v1/tool routes; the hub hands it to the Primer tab that reported focus most recently, which
listens on /v1/tool/stream, carries it out through the Reader's own code and posts it done.

Commands live in memory only, one at a time, at most 20 a minute; a command no tab picks up
within 10 s fails, and so does one a tab took and didn't finish within 90 s. Nothing runs
unless `enabled` (the reader's "Let Kairos drive Primer" switch, saved by the App) is on, and
no tab holds a stream open while it is off: turning it off ends every stream (a browser allows
only six connections per site, so idle streams would stall the tabs' other requests).
The timeouts are checked whenever the hub is used, so no thread is needed.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque

KINDS = ("show", "open", "lens")
# what a finished command did: the window posts one, and the MCP tool speaks a sentence for it
WHATS = ("opened", "writing", "picture", "video", "map", "timeline", "diagram", "story", "listen")
LENS_KINDS = ("picture", "map", "timeline", "diagram", "story", "listen")
TEXT_MAX = 300
TITLE_MAX = 200
ID_MAX = 200
PICKUP_S = 10.0  # a command no tab has taken by then fails
DONE_S = 90.0  # a taken command no tab has finished by then fails (the MCP tool waits as long)
RATE_N = 20
RATE_WINDOW_S = 60.0
CLAIM_S = 60.0  # a page a command opened is Kairos's open for this long
KEEP = 50  # finished commands remembered for GET /v1/tool/commands/<id>
MAX_LISTENERS = 16  # a 17th tab closes the oldest unfocused one (else the oldest)
DEFAULT_PORT = 8810

OFF = "Kairos can't drive Primer: turn it on in Settings"
BUSY = "Primer is still doing the last thing Kairos asked"
RATE = "Kairos asked Primer for too much at once: wait a minute"
NOT_OPEN = "Primer isn't open in a browser. Open http://127.0.0.1:{port} first."
PICKUP_FAILED = "Primer isn't open in a browser"
NOT_FINISHED = "Primer didn't finish what Kairos asked in time"
CLOSED = "Primer was closed"
SHUT_DOWN = "Primer is shutting down"
NO_FOCUS = "Open a page in Primer first"

_OPEN = ("queued", "delivered")


class RemoteError(Exception):
    """A command refused; `status` is the HTTP answer."""
    status = 409


class RemoteOff(RemoteError):
    status = 403

    def __init__(self, message: str = OFF):
        super().__init__(message)


class RemoteBusy(RemoteError):
    status = 409

    def __init__(self, message: str = BUSY):
        super().__init__(message)


class RemoteRateLimited(RemoteError):
    status = 429

    def __init__(self, message: str = RATE):
        super().__init__(message)


class NoListener(RemoteError):
    status = 409

    def __init__(self, message: str = NOT_OPEN.format(port=DEFAULT_PORT)):
        super().__init__(message)


class WrongListener(RemoteError):
    """A `done` from a tab the command wasn't sent to."""
    status = 409

    def __init__(self, message: str = "That command was sent to another Primer window"):
        super().__init__(message)


# "that" in a `show` text means the focused page (spec §2.4); /v1/intent's matchers reject such
# words, so the hub puts the page's title in their place. Only where the word is the object: the
# whole text, or right after one of these words and ending the phrase (end, punctuation, or a
# word that can't be the noun "that" or "this" would point at).
_DEICTIC = r"(?:this\s+page|that\s+page|that|this|it)"
_AFTER = r"(?:about|of|on|for|show\s+me|open)"
_ENDS = r"(?=\s*(?:[,.;:!?]|$)|\s+(?:in|as|please|with|and|from|to|for|now)\b)"
_WHOLE_RE = re.compile(rf"^\s*(?P<d>{_DEICTIC})\s*[.!?]*\s*$", re.IGNORECASE)
_OBJECT_RE = re.compile(rf"\b{_AFTER}\s+(?P<d>{_DEICTIC}){_ENDS}", re.IGNORECASE)


def deictic(text: str) -> re.Match | None:
    """Where `text` says "that", "it", "this page"... meaning the focused page; None if nowhere."""
    return _WHOLE_RE.match(text) or _OBJECT_RE.search(text)


def resolve_that(text: str, title: str | None) -> str:
    """`text` with its one "that" (see `deictic`) replaced by `title`; ValueError(NO_FOCUS) when
    it has one and there is no title. Text without one comes back unchanged."""
    m = deictic(text)
    if m is None:
        return text
    if not title:
        raise ValueError(NO_FOCUS)
    return text[:m.start("d")] + title + text[m.end("d"):]


def not_open(port: int) -> str:
    return NOT_OPEN.format(port=port)


def _text(value, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return value


def _title(value) -> dict:
    """{"title": the page's title, one line, at most TITLE_MAX} for the window's note; {} without one."""
    if not isinstance(value, str) or not value.strip():
        return {}
    return {"title": " ".join(value.split())[:TITLE_MAX]}


class Listener:
    """One Primer tab's event stream. `get` is called by that stream's thread only."""

    def __init__(self, hub: "RemoteHub", listener_id: str, order: int):
        self.id = listener_id
        self._hub = hub
        self._queue: deque[str] = deque()  # command ids
        self.order = order  # when this tab connected (the hub's counter)
        self.focused_at = 0  # the hub's counter when this tab last reported focus; 0: never
        self.focus: dict | None = None
        self.closed = False

    def get(self, timeout: float) -> dict | None:
        """The next command for this tab (now delivered), or None after `timeout` or once closed."""
        return self._hub._take(self, timeout)

    def close(self) -> None:
        self._hub._close(self)


class RemoteHub:
    """Thread-safe; one per App. `port` is the server's, for the "not open" answer."""

    def __init__(self, clock=time.monotonic, wall=time.time, port: int = DEFAULT_PORT):
        self.clock = clock
        self.wall = wall
        self.port = port
        self._enabled = False
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._listeners: dict[str, Listener] = {}
        self._commands: dict[str, dict] = {}  # id -> command (oldest first)
        self._meta: dict[str, dict] = {}  # id -> {"t": created, "dt": delivered, "to": listener id}
        self._current: str | None = None  # the command not yet finished
        self._sent: deque[float] = deque()  # when accepted commands were submitted
        self._claims: dict[str, float] = {}  # node id -> when a command opened it
        self._count = 0
        self._shut = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Turning it off fails the command in hand, whether a tab has taken it or not (the
        reader switched Kairos off), and ends every stream; a tab reconnects only once it sees
        the switch on again."""
        with self._lock:
            self._enabled = bool(value)
            if self._enabled:
                return
            cid = self._current
            if cid is not None and self._commands[cid]["status"] in ("queued", "delivered"):
                self._end(cid, "failed", error=OFF)
            for lis in self._listeners.values():
                lis.closed = True
                lis._queue.clear()
            self._listeners.clear()
            self._cond.notify_all()

    # the tabs
    def listen(self) -> Listener:
        """A new tab's stream. Over 16 tabs, the oldest unfocused one (else the oldest) is closed.
        With the switch off (or after shutdown) it starts closed: the stream ends at once."""
        with self._lock:
            self._expire()
            self._count += 1
            lis = Listener(self, secrets.token_hex(8), self._count)
            if self._shut or not self._enabled:
                lis.closed = True
                return lis
            if len(self._listeners) >= MAX_LISTENERS:
                tabs = list(self._listeners.values())
                unfocused = [t for t in tabs if t.focus is None]
                self._drop(min(unfocused or tabs, key=lambda t: t.order))
            self._listeners[lis.id] = lis
            return lis

    def focus(self, listener_id: str, node_id: str, title: str, hidden: bool = False) -> None:
        """The tab's focused Reader page (raises KeyError for an unknown or closed listener).
        A report from a `hidden` tab updates its page but doesn't make it the focused tab (it
        stays so only if it already was), so a background tab can't take Kairos's commands
        from the one the reader moved to."""
        with self._lock:
            lis = self._listeners.get(listener_id)
            if lis is None:
                raise KeyError(listener_id)
            if not hidden or self._focused() is lis:
                self._count += 1
                lis.focused_at = self._count
            lis.focus = {"node_id": node_id, "title": title}

    def now(self) -> dict:
        with self._lock:
            self._expire()
            focused = self._focused()
            return {"listening": bool(self._listeners),
                    "focus": dict(focused.focus) if focused is not None else None}

    def listening(self) -> bool:
        with self._lock:
            return bool(self._listeners)

    # Kairos's side
    def submit(self, kind: str, args: dict) -> str:
        """Queue a command for the most recently focused tab (the newest tab when none has
        reported focus); its id."""
        with self._lock:
            if not self._enabled:
                raise RemoteOff()
            if self._shut:
                raise NoListener(SHUT_DOWN)
            args = self._checked(kind, args)
            self._expire()
            self._prune_claims()
            if self._current is not None:
                raise RemoteBusy()
            now = self.clock()
            while self._sent and now - self._sent[0] >= RATE_WINDOW_S:
                self._sent.popleft()
            if len(self._sent) >= RATE_N:
                raise RemoteRateLimited()
            if not self._listeners:
                raise NoListener(not_open(self.port))
            focused = self._focused()
            target = focused or max(self._listeners.values(), key=lambda t: t.order)
            cid = secrets.token_hex(8)
            while cid in self._commands:
                cid = secrets.token_hex(8)
            self._commands[cid] = {"id": cid, "kind": kind, "args": args, "status": "queued",
                                   "result": None, "error": None, "created_at": self.wall()}
            self._meta[cid] = {"t": now, "dt": None, "to": target.id, "claim": None}
            self._current = cid
            self._sent.append(now)
            # the page a command opens is Kairos's open; a lens on the page already in focus opens none
            on_focus = focused is not None and focused.focus["node_id"] == args.get("id")
            if kind == "open" or (kind == "lens" and not on_focus):
                self._claims[args["id"]] = now
                self._meta[cid]["claim"] = args["id"]
            target._queue.append(cid)
            self._forget_old()
            self._cond.notify_all()
            return cid

    def _checked(self, kind: str, args) -> dict:
        """The command's arguments, cleaned; ValueError when they are wrong."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        if kind == "show":  # "that" becomes the focused page's title
            focused = self._focused()
            text = _text(args.get("text"), "text", TEXT_MAX)
            resolved = resolve_that(text, focused.focus["title"] if focused is not None else None)
            return {"text": resolved if len(resolved) <= TEXT_MAX else text}  # too long: as Kairos said it
        if kind == "open":
            return {"id": _text(args.get("id"), "id", ID_MAX), **_title(args.get("title"))}
        lens = args.get("kind")
        if lens not in LENS_KINDS:
            raise ValueError(f"kind must be one of {LENS_KINDS}")
        if args.get("id") is None:
            focused = self._focused()
            if focused is None:
                raise ValueError(NO_FOCUS)
            return {"kind": lens, "id": focused.focus["node_id"], **_title(focused.focus["title"])}
        return {"kind": lens, "id": _text(args.get("id"), "id", ID_MAX), **_title(args.get("title"))}

    def command(self, command_id: str) -> dict | None:
        with self._lock:
            self._expire()
            cmd = self._commands.get(command_id)
            return _copy(cmd) if cmd is not None else None

    def finish(self, command_id: str, ok: bool, text: str | dict | None = None,
               listener_id: str | None = None) -> bool:
        """The tab did it (`text` is the result) or couldn't (`text` is the error). False when
        the command is unknown or already finished; WrongListener when `listener_id` is given
        and the command went to another tab."""
        with self._lock:
            self._expire()
            cmd = self._commands.get(command_id)
            if cmd is None or cmd["status"] not in _OPEN:
                return False
            if listener_id is not None and self._meta[command_id]["to"] != listener_id:
                raise WrongListener()
            if ok:
                self._end(command_id, "done", result=text or "")
                self._drop_unused_claim(command_id, text)
            else:
                self._end(command_id, "failed", error=text or "Primer couldn't do it")
            return True

    def claim_open(self, node_id: str) -> bool:
        """True once for a page a command opened in the last 60 s (its open signal is Kairos's)."""
        with self._lock:
            at = self._claims.pop(node_id, None)
            self._prune_claims()
            return at is not None and self.clock() - at <= CLAIM_S

    def shutdown(self) -> None:
        """Fail the unfinished command and end every stream; new listeners start closed."""
        with self._lock:
            self._shut = True
            if self._current is not None:
                self._end(self._current, "failed", error=SHUT_DOWN)
            for lis in self._listeners.values():
                lis.closed = True
                lis._queue.clear()
            self._listeners.clear()
            self._cond.notify_all()

    # internals (under the lock)
    def _focused(self) -> Listener | None:
        """The open tab that reported focus most recently; None when none has."""
        tabs = [t for t in self._listeners.values() if t.focus is not None]
        return max(tabs, key=lambda t: t.focused_at) if tabs else None

    def _take(self, lis: Listener, timeout: float) -> dict | None:
        end = time.monotonic() + max(0.0, timeout)
        with self._lock:
            while True:
                self._expire()
                if lis.closed:
                    return None
                while lis._queue:
                    cid = lis._queue.popleft()
                    cmd = self._commands.get(cid)
                    if cmd is None or cmd["status"] != "queued":
                        continue
                    if not self._enabled:  # the switch went off before the tab took it
                        self._end(cid, "failed", error=OFF)
                        continue
                    cmd["status"] = "delivered"
                    self._meta[cid]["dt"] = self.clock()
                    return _copy(cmd)
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def _close(self, lis: Listener) -> None:
        with self._lock:
            self._drop(lis)

    def _drop(self, lis: Listener) -> None:
        """Close a tab's stream; the command it was sent and hasn't finished fails."""
        lis.closed = True
        lis._queue.clear()
        self._listeners.pop(lis.id, None)
        cid = self._current
        if cid is not None and self._meta[cid]["to"] == lis.id:
            self._end(cid, "failed", error=CLOSED)
        self._cond.notify_all()

    def _expire(self) -> None:
        cid = self._current
        if cid is None:
            return
        cmd, meta, now = self._commands[cid], self._meta[cid], self.clock()
        if cmd["status"] == "queued" and now - meta["t"] >= PICKUP_S:
            lis = self._listeners.get(meta["to"])
            if lis is not None and cid in lis._queue:
                lis._queue.remove(cid)
            self._end(cid, "failed", error=PICKUP_FAILED)
        elif cmd["status"] == "delivered" and now - meta["dt"] >= DONE_S:
            self._end(cid, "failed", error=NOT_FINISHED)

    def _end(self, cid: str, status: str, result: str | dict | None = None,
             error: str | None = None) -> None:
        cmd = self._commands[cid]
        cmd.update(status=status, result=result, error=error)
        if self._current == cid:
            self._current = None
        claim = self._meta[cid].get("claim")
        if status == "failed" and claim is not None:  # a failed command opened nothing
            if self._claims.get(claim) is not None and self._claims[claim] >= self._meta[cid]["t"]:
                del self._claims[claim]

    def _prune_claims(self) -> None:
        now = self.clock()
        for nid in [n for n, t in self._claims.items() if now - t > CLAIM_S]:
            del self._claims[nid]

    def _drop_unused_claim(self, cid: str, result) -> None:
        """A command that finished without opening its page (the tab already showed it: the
        result isn't `opened` and names the page that tab has in focus) gives up its claim, so
        the reader's own next open of that page stays the reader's."""
        claim = self._meta[cid].get("claim")
        if claim is None or not isinstance(result, dict) or result.get("what") == "opened":
            return
        lis = self._listeners.get(self._meta[cid]["to"])
        if (result.get("node_id") != claim or lis is None or lis.focus is None
                or lis.focus["node_id"] != claim):
            return
        if self._claims.get(claim) is not None and self._claims[claim] >= self._meta[cid]["t"]:
            del self._claims[claim]

    def _forget_old(self) -> None:
        while len(self._commands) > KEEP:
            old = next(iter(self._commands))
            if old == self._current:
                break
            del self._commands[old]
            del self._meta[old]


def _copy(cmd: dict) -> dict:
    return {**cmd, "args": dict(cmd["args"])}
