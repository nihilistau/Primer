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
from .prompts import (branch_messages, highlight_messages, page_messages, parse_branches,
                      parse_highlights, sentence_around)

log = logging.getLogger(__name__)

_PAGE_TOKENS = {"normal": 900, "deeper": 2000, "simpler": 500}
_EXCERPT_CHARS = 1200


@dataclass
class OpenRequest:
    concept: str
    via: Literal["start", "typed", "context", "general", "sideways"]
    from_id: str | None = None
    phrase: str = ""
    depth: Literal["normal", "deeper", "simpler"] = "normal"
    regenerate: bool = False
    node_id: str | None = None


class PageService:
    def __init__(self, graph: Graph, text, fast, source_mode: str = "storyteller",
                 gate: GpuGate | None = None):
        self.graph = graph
        self.gate = gate  # when set, page text generation takes turns with image jobs
        self.text = text
        self.fast = fast
        self.source_mode = source_mode
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

        yield ("node", {"id": node.id, "title": node.title, "kind": node.kind, "parent_id": node.parent_id,
                      "source_mode": self.source_mode})
        with self._lock_for(node.id):
            latest = self.graph.latest_version(node.id)
            if latest and not req.regenerate and req.depth == "normal":
                yield ("delta", {"text": latest.markdown})
                version, cached = latest, True
            else:
                chunks: list[str] = []
                # Lock order: per-node lock, then the GPU gate; released in reverse.
                turn = self.gate.page() if self.gate else nullcontext()
                if self.gate and not turn.try_enter():  # a miss queues the turn (pages first)
                    try:
                        yield ("waiting", {"reason": "paused for an image"})
                    except BaseException:  # closed at this yield: give up the queued turn
                        turn.cancel()
                        raise
                with turn:
                    try:
                        for piece in self.text.stream(self._messages(node, req.depth),
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
                version = self.graph.add_version(node.id, markdown, self.source_mode, req.depth)
                cached = False
            yield ("done", {"n": version.n, "source_mode": version.source_mode,
                            "depth": version.depth, "cached": cached})
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

    def _messages(self, node: Node, depth: str) -> list[dict]:
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
                             parent_excerpt=parent_excerpt, recent=recent, depth=depth)

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
