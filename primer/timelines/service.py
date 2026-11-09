"""Timeline jobs: the fast model lists the dated events of a page version; we normalise and order
them and store them with the page. One worker thread runs jobs in order."""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import nullcontext
from typing import Callable

from primer.graph import Graph
from primer.timelines.events import event_messages, parse_events, read_event_list

log = logging.getLogger(__name__)

SHUT_DOWN = "Primer shut down"
NO_FAST = "No fast model in this tier"
UNREADABLE = "The fast model's answer couldn't be read; try Again"


class TimelineService:
    """`fast_getter()` is the fast engine (read at call time: a tier switch replaces it; None when
    there is none). `sources_text(node_id, n, stop=...)` is a grounded version's sources' text ("" if
    none); `stop()` turns true once the service is shut down, so it can give up early."""

    def __init__(self, graph: Graph, fast_getter: Callable[[], object | None],
                 sources_text: Callable[[str, int], str] | None = None,
                 text_getter: Callable[[], object | None] | None = None,
                 gate_getter: Callable[[], object | None] | None = None) -> None:
        self.graph = graph
        self.fast_getter = fast_getter
        self.sources_text = sources_text
        # an unreadable fast answer (a small model thinking aloud past its budget) gets one retry
        # on the writing model, inside a GPU page turn; both read at call time like fast_getter
        self.text_getter = text_getter or (lambda: None)
        self.gate_getter = gate_getter or (lambda: None)
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[str] = set()
        self._worker = threading.Thread(target=self._run, name="primer-timelines", daemon=True)
        self._worker.start()

    def create(self, node_id: str) -> str:
        """A timeline of the page's latest version; ValueError for an unknown page."""
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if node is None or version is None:
            raise ValueError("unknown page")
        return self._enqueue(node_id, version.n)

    def again(self, timeline_id: str) -> str:
        """A fresh timeline of the same page version as `timeline_id`."""
        old = self.graph.get_timeline(timeline_id)
        if old is None:
            raise ValueError("unknown timeline")
        return self._enqueue(old["node_id"], old["n"])

    def get(self, timeline_id: str) -> dict | None:
        return self.graph.get_timeline(timeline_id)

    def all_events(self) -> list[dict]:
        return self.graph.all_timeline_events()

    def shutdown(self, reason: str = SHUT_DOWN) -> None:
        """Stop without draining: jobs not yet finished are marked failed with `reason`."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        self._queue.put(None)
        for tid in pending:
            self._fail(tid, reason)
        self._worker.join(timeout=2)

    # internals
    def _enqueue(self, node_id: str, n: int) -> str:
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN)
            tid = self.graph.add_timeline(node_id, n)
            self._pending.add(tid)
        self._queue.put(tid)
        return tid

    def _fail(self, tid: str, error: str) -> None:
        try:
            self.graph.update_timeline(tid, status="failed", error=error)
        except Exception:
            log.exception("could not mark timeline %s failed", tid)

    def _fail_unless_closed(self, tid: str, error: str) -> None:
        """Fail the job, unless shutdown already marked it failed ("Primer shut down" stays)."""
        if not self._is_closed():
            self._fail(tid, error)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _run(self) -> None:
        while True:
            tid = self._queue.get()
            if tid is None or self._is_closed():
                return
            try:
                self._job(tid)
            except Exception as e:  # a job never kills the worker
                log.exception("timeline job %s failed", tid)
                self._fail(tid, f"Timeline failed: {e}")
            finally:
                with self._lock:
                    self._pending.discard(tid)

    def _job(self, tid: str) -> None:
        job = self.graph.get_timeline(tid)
        node = self.graph.get_node(job["node_id"])
        version = next((v for v in self.graph.versions(job["node_id"]) if v.n == job["n"]), None)
        if node is None or version is None:
            self._fail(tid, "The page is gone")
            return
        fast = self.fast_getter()
        if fast is None:
            self._fail(tid, NO_FAST)
            return
        sources = ""
        if self.sources_text is not None and not self._is_closed():
            try:
                sources = self.sources_text(node.id, version.n, stop=self._is_closed) or ""
            except Exception:
                log.warning("could not read the sources of %s/%s", node.id, version.n, exc_info=True)
        messages = event_messages(node.title, version.markdown, sources)
        try:
            raw = fast.complete(messages, max_tokens=2000, temperature=0.1)
        except Exception as e:
            self._fail_unless_closed(tid, f"Couldn't read the dates from the page: {e}")
            return
        items = read_event_list(raw)  # read once: a long hostile reply is costly to read
        if items is None and self.text_getter() is not None and not self._is_closed():
            log.info("timeline %s: the fast answer was unreadable (%d chars); asking the writing model", tid,
                     len(raw or ""))
            gate = self.gate_getter()
            try:
                with gate.page() if gate is not None else nullcontext():  # a page turn
                    if self._is_closed():  # shut down while waiting for the turn
                        return
                    text = self.text_getter()  # read inside the turn: a tier switch may replace it
                    if text is not None:
                        raw = text.complete(messages, max_tokens=2000, temperature=0.1)
                        items = read_event_list(raw)
            except Exception as e:
                self._fail_unless_closed(tid, f"Couldn't read the dates from the page: {e}")
                return
        if items is None:
            self._fail_unless_closed(tid, UNREADABLE)
            return
        events = parse_events(items)  # [] is a finished timeline: "No dated events on this page"
        if self._is_closed():  # shutdown already marked this timeline failed
            return
        self.graph.set_timeline_events(tid, events)
        self.graph.update_timeline(tid, status="done")
