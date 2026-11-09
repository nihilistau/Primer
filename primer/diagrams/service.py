"""Diagram jobs: the writing model draws how a page's subject works as SVG, the drawing is
rebuilt from the allowlist (primer.diagrams.svg) and stored with the page version.

One worker thread runs jobs in order. Drawing takes a page turn at the GPU gate (it uses the
writing model, as a page does: pages first, drafts pre-empted). The engine and gate are read
through getters at call time: a tier switch replaces them."""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import nullcontext
from typing import Callable

from primer.diagrams import DiagramError
from primer.diagrams.prompts import NOTHING, diagram_messages, parse_diagram
from primer.diagrams.svg import clean_svg
from primer.graph import Graph

log = logging.getLogger(__name__)

SHUT_DOWN = "Primer shut down"
NO_TEXT = "no writing model in this tier"
MAX_TOKENS = 4000
TEMPERATURE = 0.4


class DiagramService:
    def __init__(self, graph: Graph, text_getter: Callable[[], object | None],
                 gate_getter: Callable[[], object | None]) -> None:
        self.graph = graph
        self.text_getter = text_getter
        self.gate_getter = gate_getter
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[str] = set()
        self._worker = threading.Thread(target=self._run, name="primer-diagrams", daemon=True)
        self._worker.start()

    def available(self) -> dict:
        """{ok, reason}: a diagram needs the writing model."""
        engine = self._engine()
        return {"ok": True, "reason": ""} if engine is not None else {"ok": False, "reason": NO_TEXT}

    def create(self, node_id: str, phrase: str = "") -> str:
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if node is None or version is None:
            raise ValueError("unknown page")
        return self._enqueue(node_id, version.n, phrase or "")

    def again(self, diagram_id: str) -> str:
        """A fresh drawing of the same page version and phrase (a new diagram id)."""
        old = self.graph.get_diagram(diagram_id)
        if old is None:
            raise ValueError("unknown diagram")
        return self._enqueue(old["node_id"], old["n"], old["phrase"])

    def get(self, diagram_id: str) -> dict | None:
        return self.graph.get_diagram(diagram_id)

    def shutdown(self, reason: str = SHUT_DOWN) -> None:
        """Stop without draining: diagrams not yet finished are marked failed with `reason`."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        self._queue.put(None)
        for did in pending:
            self._fail(did, reason)
        self._worker.join(timeout=2)

    # internals
    def _engine(self):
        try:
            engine = self.text_getter()
        except Exception:
            log.exception("could not read the writing engine")
            return None
        return engine if engine is not None and hasattr(engine, "complete") else None

    def _gate(self):
        try:
            return self.gate_getter()
        except Exception:
            log.exception("could not read the GPU gate")
            return None

    def _enqueue(self, node_id: str, n: int, phrase: str) -> str:
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN)
            did = self.graph.add_diagram(node_id, n, phrase)
            self._pending.add(did)
        self._queue.put(did)
        return did

    def _fail(self, did: str, error: str) -> None:
        try:
            self.graph.update_diagram(did, status="failed", error=error)
        except Exception:
            log.exception("could not mark diagram %s failed", did)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _run(self) -> None:
        while True:
            did = self._queue.get()
            if did is None or self._is_closed():
                return
            try:
                self._job(did)
            except Exception as e:  # a job never kills the worker
                log.exception("diagram job %s failed", did)
                self._fail(did, f"Diagram failed: {e}")
            finally:
                with self._lock:
                    self._pending.discard(did)

    def _job(self, did: str) -> None:
        job = self.graph.get_diagram(did)
        node = self.graph.get_node(job["node_id"])
        version = next((v for v in self.graph.versions(job["node_id"]) if v.n == job["n"]), None)
        if node is None or version is None:
            self._fail(did, "The page is gone")
            return
        messages = diagram_messages(node.title, version.markdown, job["phrase"])
        self.graph.update_diagram(did, status="drawing")
        error = None
        for _ in range(2):  # one retry on an unusable drawing
            engine = self._engine()  # read for each try: a tier switch may have replaced it
            if engine is None:
                self._fail(did, NO_TEXT)
                return
            gate = self._gate()
            try:
                with gate.page() if gate is not None else nullcontext():  # a page turn
                    if self._is_closed():  # shut down while waiting for the turn
                        return
                    raw = engine.complete(messages, max_tokens=MAX_TOKENS, temperature=TEMPERATURE)
            except Exception as e:
                if not self._is_closed():  # shutdown already marked this diagram failed
                    self._fail(did, f"Couldn't draw the diagram: {e}")
                return
            if self._is_closed():  # shutdown already marked this diagram failed
                return
            try:
                svg_text, caption = parse_diagram(raw)
                # this diagram's own id prefix: two diagrams shown together can't clash
                svg, labels = clean_svg(svg_text, prefix=f"dg{did[:6]}-")
            except DiagramError as e:
                error = str(e)
                if error == NOTHING:
                    break
                log.warning("unusable diagram from the model (%s): %r", error, (raw or "")[:200])
                continue
            self.graph.update_diagram(did, status="done", error=None, svg=svg, caption=caption,
                                      labels=labels)
            return
        self._fail(did, error or "the drawing could not be read")
