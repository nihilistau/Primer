"""Page service: a click or typed topic becomes a streamed, cached, versioned page."""
from __future__ import annotations

import logging
import queue
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, Iterator, Literal

from .browse.address import kiwix_address
from .engines import EngineError, StreamCancel
from .gpu import GpuGate
from .graph import Card, Draft, Graph, Node, Version, clean_typed
from .grounding import MODES, Source
from .prompts import (branch_messages, highlight_messages, page_messages, parse_branches,
                      parse_highlights, sentence_around)
from .reader.signals import counted_block, style_hint

log = logging.getLogger(__name__)

_PAGE_TOKENS = {"normal": 900, "deeper": 2000, "simpler": 500}
_EXCERPT_CHARS = 1200
_OWN = "written from the model's own knowledge."


def _sources_event(sources) -> dict:
    return {"sources": [{"n": s.n, "title": s.title, "origin": s.origin, "where": s.where}
                        for s in sources]}


@dataclass
class OpenRequest:
    concept: str
    via: Literal["start", "typed", "context", "general", "sideways", "kairos"]
    from_id: str | None = None
    phrase: str = ""
    depth: Literal["normal", "deeper", "simpler"] = "normal"
    regenerate: bool = False
    node_id: str | None = None
    source: str | None = None
    source_url: str | None = None  # write from this one page (http(s) or kiwix:) alone


class PageService:
    def __init__(self, graph: Graph, text, fast, source_mode: str = "storyteller",
                 gate: GpuGate | None = None, grounder=None, signals=None,
                 reader_block: Callable[[], str] | None = None,
                 reader_style: Callable[[], str] | None = None):
        self.graph = graph
        self.signals = signals  # a primer.reader.signals.Signals: opens, depth and regenerate are recorded
        # "About this reader" for page prompts; None: the counted profile of `signals` (or nothing)
        self.reader_block = reader_block
        # the curiosity-style line for branch-card and highlight prompts; None: the counted style
        # of `signals`. App wires the profile's, so its switch and Reset cover cards too
        self.reader_style = reader_style
        # called (no arguments) after a plain page open and when the prefetch queue runs dry:
        # the reader model may start a background summary then. Must not block or raise.
        self.nudge: Callable[[], None] | None = None
        self._fast_calls = 0
        self._fast_guard = threading.Lock()
        self.gate = gate  # when set, page text generation takes turns with image jobs
        self.text = text
        self.fast = fast
        self.source_mode = source_mode
        self.grounder = grounder
        self.drafter = None  # a Drafter attaches itself (thinking ahead); None: no drafting
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
        self._record_open(node, req)

        latest = self.graph.latest_version(node.id)
        regenerate = req.regenerate or self._not_from_url(node, latest, req)
        serve_cached = latest is not None and not regenerate and req.depth == "normal"
        # Advisory: the badge can show this early; the `done` event carries the authoritative mode.
        mode = latest.source_mode if serve_cached else self._mode(req)
        yield ("node", {"id": node.id, "title": node.title, "kind": node.kind, "parent_id": node.parent_id,
                      "source_mode": mode, "cached": serve_cached})
        with self._lock_for(node.id):
            latest = self.graph.latest_version(node.id)
            regenerate = req.regenerate or self._not_from_url(node, latest, req)
            note, draft = "", None
            if latest and not regenerate and req.depth == "normal":
                yield ("delta", {"text": latest.markdown})
                version, cached = latest, True
            else:
                mode = self._mode(req)
                draft = self._take_draft(node, req, mode)
                if isinstance(draft, Draft):  # written ahead: shown at once
                    sources, markdown = draft.sources, draft.markdown
                    if sources:
                        yield ("sources", _sources_event(sources))
                    yield ("delta", {"text": markdown})
                elif draft is not None:  # still being written: adopted mid-stream
                    sources = draft.sources
                    pieces = draft.pieces()
                    try:
                        if sources:
                            yield ("sources", _sources_event(sources))
                        for text in pieces:
                            yield ("delta", {"text": text})
                    except EngineError as e:
                        yield ("error", {"message": str(e) or "engine error"})
                        return
                    finally:
                        pieces.close()
                        draft.release()  # the reader left: the draft becomes pre-emptible again
                    markdown = draft.markdown
                else:
                    if self.drafter is not None:
                        self.drafter.moved_on()  # the old page's waiting drafts are stale now
                    mode, sources, note = yield from self.ground(mode, node.title, req.source_url)
                    chunks: list[str] = []
                    # Lock order: per-node lock, then the GPU gate; released in reverse.
                    turn = self.gate.page() if self.gate else nullcontext()
                    if self.gate and not turn.try_enter():  # a miss queues the turn (pages first)
                        reason = ("finishing a draft…" if self.gate.state()["background"]
                                  else "paused for a picture or video")
                        try:
                            yield ("waiting", {"reason": reason})
                        except BaseException:  # closed at this yield: give up the queued turn
                            turn.cancel()
                            raise
                    with turn:
                        try:
                            for piece in self.write(node, req.depth, sources):
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
                if draft is not None:
                    self.graph.delete_draft(node.id, mode)
                cached = False
            yield ("done", {"n": version.n, "source_mode": version.source_mode,
                            "depth": version.depth, "cached": cached, "note": note,
                            "drafted": draft is not None})
            phrases = self._highlights(node.id, version)
        yield ("highlights", {"phrases": phrases})
        for i, phrase in enumerate(phrases):
            self._enqueue(node.id, phrase, first=i == 0)
        if not req.regenerate and req.depth == "normal":
            self._nudge()

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

    def ground(self, mode: str, title: str, url: str | None = None):
        """Gather sources for a page about `title` in `mode`. A generator: yields ("grounding",
        ...) and ("sources", ...) events and returns (mode, sources, note), where a note means
        the page falls back to storyteller (no grounder, or grounding failed)."""
        sources: list[Source] = []
        note = ""
        if mode == "storyteller":
            return mode, sources, note
        if self.grounder is None:
            note = (f"Offline reference isn't set up in this tier; {_OWN}" if mode == "reference"
                    else f"Web search is off in this tier; {_OWN}")
        else:
            for ev in self._ground(mode, title, url):
                if ev[0] == "result":
                    sources, note = ev[1]
                else:
                    yield ev
        if note:
            return "storyteller", [], note
        if sources:
            yield ("sources", _sources_event(sources))
        return mode, sources, note

    def write(self, node: Node, depth: str, sources=(), cancel: StreamCancel | None = None):
        """The page's text as it streams from the text engine: the one writing path shared by
        open() and the drafter, so a draft is written exactly as the page would be. `cancel`
        (drafts only) stops the stream from another thread."""
        kw = {} if cancel is None else {"cancel": cancel}
        return self.text.stream(self._messages(node, depth, sources), max_tokens=_PAGE_TOKENS[depth],
                                temperature=0.7, **kw)

    def fast_busy(self) -> bool:
        """True while this service is calling the fast engine (highlights, branch cards) or has
        branch-card prefetch queued: the reader model's background summary waits for a free
        fast engine, never the reverse (the queue running dry nudges it)."""
        with self._fast_guard:
            return self._fast_calls > 0 or not self._queue.empty()

    def shutdown(self, drain: bool = True) -> None:
        """Stop the drafter (if attached) and the prefetch worker. drain=True finishes the
        queued prefetch first; drain=False drops it and waits only for the item in flight.
        Idempotent."""
        if self.drafter is not None:
            self.drafter.shutdown()
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
        # what Kairos asked for is typed text too ("show me Bodiam Castle")
        concept = clean_typed(req.concept) if req.via in ("typed", "kairos") else req.concept
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

    def _take_draft(self, node: Node, req: OpenRequest, mode: str):
        """A draft of this page for `mode` (finished, or adopted while still writing), or None.
        Only for a plain open: normal depth, no regenerate, not written from a given page."""
        if self.drafter is None or req.depth != "normal" or req.regenerate or req.source_url:
            return None
        return self.drafter.take(node.id, mode)

    def _not_from_url(self, node: Node, latest: Version | None, req: OpenRequest) -> bool:
        """A page made from a given URL never silently reuses a version grounded elsewhere."""
        if not req.source_url or latest is None:
            return False
        for s in self.graph.sources(node.id, latest.n):
            if s.url == req.source_url or (s.origin == "reference" and s.book and
                                           kiwix_address(s.book, s.path) == req.source_url):
                return False
        return True

    def _mode(self, req: OpenRequest) -> str:
        """The mode to write in; an unknown request mode is storyteller (the server rejects it earlier)."""
        if req.source_url:  # a page from a page: that page's kind decides
            return "reference" if req.source_url.startswith("kiwix:") else "web"
        if req.source is None:
            return self.source_mode
        return req.source if req.source in MODES else "storyteller"

    def _ground(self, mode: str, query: str, url: str | None = None):
        """Yield ("grounding", ...) events as retrieval reports, then ("result", (sources, note)).
        With a url, the one source is that page and nothing is searched."""
        box: queue.Queue = queue.Queue()
        result: list = []

        def run() -> None:
            try:
                if url:
                    box.put("Reading the page…")
                    result.append(self.grounder.gather_url(url))
                else:
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
                             sources=sources, reader=self._reader())

    # the reader model: signals in, preferences out; neither may ever break a page
    def _record(self, kind: str, node_id: str, detail: str = "") -> None:
        if self.signals is None:
            return
        try:
            self.signals.record(kind, node_id, detail)
        except Exception:
            log.exception("recording a reading signal failed")

    def _record_open(self, node: Node, req: OpenRequest) -> None:
        """A plain open is an `open` (detail: how the reader came); Deeper/Simpler and Regenerate
        re-ask for a page already open, so they are their own kinds and don't count as opens
        (their requests carry a default via that would skew the curiosity style)."""
        if req.regenerate:
            self._record("regenerate", node.id)
        if req.depth != "normal":
            self._record("depth", node.id, req.depth)
        if not req.regenerate and req.depth == "normal":
            self._record("open", node.id, req.via)

    def _nudge(self) -> None:
        if self.nudge is None:
            return
        try:
            self.nudge()
        except Exception:
            log.exception("nudging the reader model failed")

    def _counted(self) -> dict | None:
        if self.signals is None:
            return None
        try:
            return self.signals.counted()
        except Exception:
            log.exception("counting reading signals failed")
            return None

    def _reader(self) -> str:
        """The "About this reader" text for page prompts ("" for none)."""
        try:
            if self.reader_block is not None:
                return self.reader_block() or ""
        except Exception:
            log.exception("the reader block failed")
            return ""
        counted = self._counted()
        return counted_block(counted) if counted else ""

    def _style(self) -> str:
        if self.reader_style is not None:
            try:
                return self.reader_style() or ""
            except Exception:
                log.exception("the reader style failed")
                return ""
        if self.signals is None:
            return ""
        try:
            return style_hint({"style": self.signals.style_counts()})
        except Exception:
            log.exception("counting reading signals failed")
            return ""

    def _fast(self, messages: list[dict]) -> str:
        """One fast-engine call, counted for fast_busy()."""
        with self._fast_guard:
            self._fast_calls += 1
        try:
            return self.fast.complete(messages, max_tokens=300, temperature=0.2)
        finally:
            with self._fast_guard:
                self._fast_calls -= 1

    def _highlights(self, node_id: str, version: Version) -> list[str]:
        stored = self.graph.get_highlights(node_id, version.n)
        if stored is not None:
            return stored
        try:
            raw = self._fast(highlight_messages(version.markdown, style=self._style()))
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
            raw = self._fast(branch_messages(phrase, node.title, sentence_around(version.markdown, phrase),
                                             style=self._style()))
        except Exception:
            log.exception("branch generation failed")
            return parse_branches("", phrase, node.title)  # fallback cards, not cached
        cards = parse_branches(raw, phrase, node.title)
        self.graph.set_cards(node_id, phrase, cards)
        return cards

    def _enqueue(self, node_id: str, phrase: str, first: bool = False) -> None:
        """Queue a phrase's branch cards; `first` (the page's first phrase) offers the drafter
        its candidates once those cards exist."""
        with self._closing:
            if not self._closed:
                self._queue.put((node_id, phrase, first))

    def _run_prefetch(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._abandon:
                return
            node_id, phrase, first = item
            try:
                if self.graph.get_cards(node_id, phrase) is None:
                    self._compute_cards(node_id, phrase)
                if first and self.drafter is not None:
                    self.drafter.offer(node_id)
            except Exception:
                log.exception("prefetch failed")
            if self._queue.empty():  # the fast engine is free again: a summary may be due
                self._nudge()
