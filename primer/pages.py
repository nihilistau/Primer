"""Page service: a click or typed topic becomes a streamed, cached, versioned page."""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Iterator, Literal

from .engines import EngineError
from .gpu import GpuGate
from .graph import Card, Graph, Node, Version, clean_typed
from .grounding import MODES, Source
from .prompts import (branch_messages, highlight_messages, page_messages, parse_branches,
                      parse_highlights, sentence_around)

log = logging.getLogger(__name__)

_PAGE_TOKENS = {"normal": 900, "deeper": 2000, "simpler": 500}
_EXCERPT_CHARS = 1200
_OWN = "written from the model's own knowledge."


@dataclass
class OpenRequest:
    concept: str
    via: Literal["start", "typed", "context", "general", "sideways"]
    from_id: str | None = None
    phrase: str = ""
    depth: Literal["normal", "deeper", "simpler"] = "normal"
    regenerate: bool = False
    node_id: str | None = None
    source: str | None = None


class PageService:
    def __init__(self, graph: Graph, text, fast, source_mode: str = "storyteller",
                 gate: GpuGate | None = None, grounder=None):
        self.graph = graph
        self.gate = gate  # when set, page text generation takes turns with image jobs
        self.text = text
        self.fast = fast
        self.source_mode = source_mode
        self.grounder = grounder
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._abandon = False  # shutdown(drain=False): drop whatever is still queued
        self._closing = threading.Lock()
        self._worker = threading.Thread(target=self._run_prefetch, name="primer-prefetch", daemon=True)
        self._worker.start()

    def _lock_for(self, node_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(node_id, threading.Lock())

    # public API
    def open(self, req: OpenRequest) -> Iterator[tuple[str, dict]]:
        node = self._resolve(req)
        if isinstance(node, str):
            yield ("error", {"message": node})
            return

        latest = self.graph.latest_version(node.id)
        serve_cached = latest is not None and not req.regenerate and req.depth == "normal"
        # Advisory: the badge can show this early; the `done` event carries the authoritative mode.
        mode = latest.source_mode if serve_cached else self._mode(req)
        yield ("node", {"id": node.id, "title": node.title, "kind": node.kind, "parent_id": node.parent_id,
                      "source_mode": mode})
        with self._lock_for(node.id):
            latest = self.graph.latest_version(node.id)
            note = ""
            if latest and not req.regenerate and req.depth == "normal":
                yield ("delta", {"text": latest.markdown})
                version, cached = latest, True
            else:
                mode = self._mode(req)
                sources: list[Source] = []
                if mode != "storyteller":
                    if self.grounder is None:
                        note = (f"Offline reference isn't set up in this tier; {_OWN}" if mode == "reference"
                                else f"Web search is off in this tier; {_OWN}")
                    else:
                        for ev in self._ground(mode, node.title):
                            if ev[0] == "result":
                                sources, note = ev[1]
                            else:
                                yield ev
                    if note:
                        mode, sources = "storyteller", []
                    elif sources:
                        yield ("sources", {"sources": [
                            {"n": s.n, "title": s.title, "origin": s.origin, "where": s.where}
                            for s in sources]})
                chunks: list[str] = []
                # Lock order: per-node lock, then the GPU gate; released in reverse.
                turn = self.gate.page() if self.gate else nullcontext()
                if self.gate and not turn.try_enter():  # a miss queues the turn (pages first)
                    try:
                        yield ("waiting", {"reason": "paused for a picture or video"})
                    except BaseException:  # closed at this yield: give up the queued turn
                        turn.cancel()
                        raise
                with turn:
                    try:
                        for piece in self.text.stream(self._messages(node, req.depth, sources),
                                                      max_tokens=_PAGE_TOKENS[req.depth], temperature=0.7):
                            chunks.append(piece)
                            yield ("delta", {"text": piece})
                    except EngineError as e:
                        yield ("error", {"message": str(e) or "engine error"})
                        return
                markdown = "".join(chunks)
                if not markdown.strip():
                    yield ("error", {"message": "empty response"})
                    return
                version = self.graph.add_version(node.id, markdown, mode, req.depth, sources)
                cached = False
            yield ("done", {"n": version.n, "source_mode": version.source_mode,
                            "depth": version.depth, "cached": cached, "note": note})
            phrases = self._highlights(node.id, version)
        yield ("highlights", {"phrases": phrases})
        for phrase in phrases:
            self._enqueue(node.id, phrase)

    def page(self, node_id: str) -> dict | None:
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if version is None:
            return None
        return {"node": node, "version": version,
                "versions": [v.n for v in self.graph.versions(node_id)],
                "highlights": self.graph.get_highlights(node_id, version.n) or []}

    def branches(self, node_id: str, phrase: str) -> list[Card]:
        cards = self.graph.get_cards(node_id, phrase)
        if cards is not None:
            return cards
        return self._compute_cards(node_id, phrase) or []

    def shutdown(self, drain: bool = True) -> None:
        """Stop the prefetch worker. drain=True finishes the queued work first; drain=False
        drops it and waits only for the item in flight. Idempotent."""
        with self._closing:
            if not drain:
                self._abandon = True
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._worker.join()

    # internals
    def _resolve(self, req: OpenRequest) -> Node | str:
        """The node to open (adding its edge), or an error message."""
        if req.node_id:
            return self.graph.get_node(req.node_id) or "not found"
        concept = clean_typed(req.concept) if req.via == "typed" else req.concept
        if not concept:
            return "empty"
        try:
            if req.via == "context":
                node = self.graph.node_for(concept, "context", parent_id=req.from_id)
            else:
                node = self.graph.node_for(concept, "concept")
        except ValueError:
            return "empty"
        self.graph.add_edge(req.from_id, node.id, req.phrase or concept, req.via)
        return node

    def _mode(self, req: OpenRequest) -> str:
        """The mode to write in; an unknown request mode is storyteller (the server rejects it earlier)."""
        if req.source is None:
            return self.source_mode
        return req.source if req.source in MODES else "storyteller"

    def _ground(self, mode: str, query: str):
        """Yield ("grounding", ...) events as retrieval reports, then ("result", (sources, note))."""
        box: queue.Queue = queue.Queue()
        result: list = []

        def run() -> None:
            try:
                result.append(self.grounder.gather(mode, query, box.put))
            except Exception as exc:  # gather should not raise; never let it break the page
                log.exception("grounder failed")
                result.append(([], f"Grounding failed ({exc}); {_OWN}"))
            box.put(None)

        threading.Thread(target=run, name="primer-ground", daemon=True).start()
        while (status := box.get()) is not None:
            yield ("grounding", {"status": status})
        yield ("result", result[0])

    def _messages(self, node: Node, depth: str, sources=()) -> list[dict]:
        parent_title = parent_excerpt = None
        if node.kind == "context" and node.parent_id:
            parent = self.graph.get_node(node.parent_id)
            if parent:
                parent_title = parent.title
                pv = self.graph.latest_version(parent.id)
                if pv:
                    parent_excerpt = pv.markdown[:_EXCERPT_CHARS]
        recent = [t for t in self.graph.recent_titles(30) if t != node.title]
        return page_messages(node.title, kind=node.kind, parent_title=parent_title,
                             parent_excerpt=parent_excerpt, recent=recent, depth=depth,
                             sources=sources)

    def _highlights(self, node_id: str, version: Version) -> list[str]:
        stored = self.graph.get_highlights(node_id, version.n)
        if stored is not None:
            return stored
        try:
            raw = self.fast.complete(highlight_messages(version.markdown), max_tokens=300, temperature=0.2)
            phrases = parse_highlights(raw, version.markdown)
        except Exception:
            log.exception("highlight generation failed")
            return []
        self.graph.set_highlights(node_id, version.n, phrases)
        return phrases

    def _compute_cards(self, node_id: str, phrase: str) -> list[Card] | None:
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if version is None:
            return None
        try:
            raw = self.fast.complete(
                branch_messages(phrase, node.title, sentence_around(version.markdown, phrase)),
                max_tokens=300, temperature=0.2)
        except Exception:
            log.exception("branch generation failed")
            return parse_branches("", phrase, node.title)  # fallback cards, not cached
        cards = parse_branches(raw, phrase, node.title)
        self.graph.set_cards(node_id, phrase, cards)
        return cards

    def _enqueue(self, node_id: str, phrase: str) -> None:
        with self._closing:
            if not self._closed:
                self._queue.put((node_id, phrase))

    def _run_prefetch(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._abandon:
                return
            node_id, phrase = item
            try:
                if self.graph.get_cards(node_id, phrase) is None:
                    self._compute_cards(node_id, phrase)
            except Exception:
                log.exception("prefetch failed")
