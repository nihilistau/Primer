"""A Chrome DevTools Protocol connection over primer.live.ws (flattened sessions)."""
from __future__ import annotations

import itertools
import json
import logging
import queue
import threading

from primer.live.ws import WebSocket, WsError

log = logging.getLogger("primer.live.cdp")


class CdpError(Exception):
    pass


class _Pending:
    __slots__ = ("event", "result", "error", "mid", "method")

    def __init__(self):
        self.mid, self.method = 0, ""
        self.event = threading.Event()
        self.result = None
        self.error = None


class Cdp:
    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._listeners: dict[str, list] = {}
        self._lock = threading.Lock()
        self.closed = threading.Event()
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._dispatcher = threading.Thread(target=self._dispatch, name="cdp-events", daemon=True)
        self._dispatcher.start()
        self._reader = threading.Thread(target=self._run, name="cdp-reader", daemon=True)
        self._reader.start()

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> None:
        """Send without waiting for the reply (it is dropped when it comes). Messages go out
        in the order send()/call() were made on one thread. Raises CdpError if closed."""
        with self._lock:
            if self.closed.is_set():
                raise CdpError("browser closed")
            mid = next(self._ids)
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._ws.send_text(json.dumps(msg))
        except WsError as e:
            self._ws.close()
            raise CdpError("browser closed") from e

    def start(self, method: str, params: dict | None = None,
              session: str | None = None) -> "_Pending":
        """Send now; collect the reply later with finish(). Lets a caller queue several
        messages in order before waiting on any of them."""
        p = _Pending()
        with self._lock:
            if self.closed.is_set():
                raise CdpError("browser closed")
            mid = next(self._ids)
            self._pending[mid] = p
        p.mid, p.method = mid, method
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._ws.send_text(json.dumps(msg))
        except WsError as e:
            self._drop(mid)
            self._ws.close()  # a failed or timed-out send: the connection is unusable
            raise CdpError("browser closed") from e
        return p

    def finish(self, p: "_Pending", timeout: float = 15) -> dict:
        if not p.event.wait(timeout):
            self._drop(p.mid)
            raise CdpError(f"{p.method} timed out")
        if p.error is not None:
            raise CdpError(p.error)
        return p.result or {}

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 15) -> dict:
        return self.finish(self.start(method, params, session), timeout)

    def _drop(self, mid: int) -> None:
        with self._lock:
            self._pending.pop(mid, None)

    def on(self, event: str, fn) -> None:
        with self._lock:
            self._listeners.setdefault(event, []).append(fn)

    def off(self, event: str, fn) -> None:
        with self._lock:
            fns = self._listeners.get(event, [])
            if fn in fns:
                fns.remove(fn)

    def _run(self) -> None:
        try:
            while True:
                raw = self._ws.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg:
                    with self._lock:
                        p = self._pending.pop(msg["id"], None)
                    if p is not None:
                        err = msg.get("error")
                        if err is not None:
                            p.error = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        else:
                            p.result = msg.get("result")
                        p.event.set()
                elif "method" in msg:
                    self._events.put((msg["method"], msg.get("params") or {},
                                      msg.get("sessionId")))
        except Exception:
            log.debug("cdp reader ended", exc_info=True)
        finally:
            self._finish()

    def _dispatch(self) -> None:
        """Runs listeners in order, off the reader, so a listener may call()."""
        while True:
            item = self._events.get()
            if item is None:
                return
            method, params, sid = item
            with self._lock:
                fns = list(self._listeners.get(method, ()))
            for fn in fns:
                try:
                    fn(params, sid)
                except Exception:
                    log.exception("cdp listener for %s failed", method)

    def _finish(self) -> None:
        with self._lock:
            self.closed.set()
            pend = list(self._pending.values())
            self._pending.clear()
        for p in pend:
            p.error = "browser closed"
            p.event.set()
        self._events.put(None)

    def close(self) -> None:
        self._ws.close()
        self._finish()
