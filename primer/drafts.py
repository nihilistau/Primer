"""Thinking ahead: while the reader reads, the idle GPU drafts the page they are likely to open
next, so that opening it is instant.

After a page is shown and its first highlighted phrase's branch cards exist, PageService offers
that page to the Drafter. Its candidates are the phrase's "In this context" card and (two pages
ahead) its "General" card. One worker thread drafts them, one at a time, only while the GPU gate
is completely idle (GpuGate.background()), and steps aside the moment a real page or picture
asks for the gate: it stops between streamed pieces, and a watchdog closes the stream at once
if pieces stall. A stopped draft is thrown away. One stopped for a picture is tried again when
the GPU is idle again; one stopped because the reader opened another page is not (that page
offers its own candidates).

Drafts live in their own table (no edge, version or history entry) until PageService.open
promotes one (take() -> Draft) or adopts one still being written (take() -> Adoption)."""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Iterator

from .engines import EngineError, StreamCancel
from .graph import Draft, Graph, Node
from .gpu import GpuGate

if TYPE_CHECKING:
    from .pages import PageService

log = logging.getLogger(__name__)

KEEP = 30           # finished drafts kept (oldest dropped)
_IDLE_POLL_S = 0.2  # how often a waiting candidate looks for an idle gate
_JOIN_S = 10.0      # shutdown waits this long at most (grounding can't be interrupted)


@dataclass
class _Candidate:
    concept: str
    kind: str              # "context" or "concept", as PageService._resolve makes the node
    parent_id: str | None
    ahead: int             # 1: the context card, 2: the general card (setting = pages ahead)
    grounded: dict = field(default_factory=dict)  # mode -> sources, kept across retries
    node: Node | None = None


@dataclass
class _Run:
    """The draft being written. Guarded by the Drafter's condition."""
    node_id: str
    title: str
    mode: str
    sources: list
    cancel: StreamCancel
    bg: object               # its background gate turn
    ahead: int = 1           # the candidate's pages ahead: lowering the setting below it stops it
    chunks: list = field(default_factory=list)
    state: str = "writing"   # writing -> done | failed | cancelled
    adopted: bool = False    # an open took it over: no longer pre-emptible
    cancelling: bool = False # pre-empted or shut down: the stream is being stopped
    ended: bool = False      # the stream ran to its end: too late to pre-empt
    error: str = ""


class Adoption:
    """A draft still being written, taken over by PageService.open: pieces() yields the text so
    far at once, then each new piece until the draft finishes; markdown is the whole text after."""

    def __init__(self, drafter: "Drafter", run: _Run):
        self._drafter, self._run = drafter, run
        self.sources = run.sources
        self.markdown = ""

    def release(self) -> None:
        """Give the draft back if it is still being written: background work again,
        pre-emptible as before. Idempotent; PageService.open calls it if it is closed before
        (or while) reading pieces()."""
        d, run = self._drafter, self._run
        with d._cond:
            if run.state == "writing" and run.adopted:
                run.adopted = False
                run.bg.set_requested(False)

    def pieces(self) -> Iterator[str]:
        """If the adopter goes away (the generator is closed) while the draft is still being
        written, the draft is given back: background work again, pre-emptible as before."""
        d, run, i = self._drafter, self._run, 0
        state = "writing"
        try:
            while True:
                with d._cond:
                    d._cond.wait_for(lambda: len(run.chunks) > i or run.state != "writing")
                    new, i, state = "".join(run.chunks[i:]), len(run.chunks), run.state
                if new:
                    yield new
                if state != "writing":
                    break
        finally:
            if state == "writing":
                self.release()
        if state != "done":
            raise EngineError(run.error or "the page stopped before it finished")
        self.markdown = "".join(run.chunks)
        with d._cond:
            d._used += 1


class Drafter:
    """Writes the likely next page while the GPU is idle. Attaches itself to `pages` (which then
    offers candidates, promotes/adopts drafts on open, and shuts the drafter down with itself).
    `setting()` is how many pages ahead to draft: 0 off, 1 the context card, 2 also general."""

    def __init__(self, graph: Graph, pages: "PageService", gate: GpuGate, setting: Callable[[], int]):
        self.graph, self.pages, self.gate, self._setting = graph, pages, gate, setting
        self._cond = threading.Condition()
        self._pending: deque[_Candidate] = deque()
        self._run: _Run | None = None
        self._closed = False
        self._written = self._used = self._discarded = 0
        pages.drafter = self
        self._worker = threading.Thread(target=self._work, name="primer-drafter", daemon=True)
        self._worker.start()

    # public API
    def offer(self, parent_node_id: str) -> None:
        """The page `parent_node_id` is being read and its first phrase's cards exist: its
        candidates replace whatever was waiting (the reader has moved on), and a draft being
        written for some other page stops (unless a reader has adopted it)."""
        version = self.graph.latest_version(parent_node_id)
        phrases = self.graph.get_highlights(parent_node_id, version.n) if version else None
        cards = self.graph.get_cards(parent_node_id, phrases[0]) if phrases else None
        by_via = {c.via: c for c in cards or []}
        found = []
        if "context" in by_via:
            found.append(_Candidate(by_via["context"].concept, "context", parent_node_id, 1))
        if "general" in by_via:
            found.append(_Candidate(by_via["general"].concept, "concept", None, 2))
        ids = set()
        for c in found:
            try:
                ids.add(self.graph.node_id_for(c.concept, c.kind, parent_id=c.parent_id))
            except ValueError:
                pass
        with self._cond:
            if self._closed or not found:
                return
            self._pending = deque(found)
            self._cond.notify_all()
            run = self._stoppable()
            if run is None or run.node_id in ids:
                return
            run.cancelling = True
        run.cancel.cancel()

    def moved_on(self) -> None:
        """PageService.open is writing a page that wasn't drafted: the waiting candidates were
        for the page the reader left, so forget them. The draft being written steps aside for
        the page through the gate as usual, and isn't tried again."""
        with self._cond:
            self._pending.clear()

    def status(self) -> dict:
        with self._cond:
            run = self._run
            drafting = run.title if run is not None and run.state == "writing" else None
            written, used, discarded = self._written, self._used, self._discarded
        try:
            setting = self._setting()
        except Exception:
            setting = 0
        return {"setting": setting, "drafting": drafting, "written": written, "used": used,
                "discarded": discarded,
                "hit_rate": round(min(1.0, used / written), 2) if written else None}

    def ready(self, node_id: str, source_mode: str) -> bool:
        """A finished draft of this page for this source mode is waiting."""
        d = self.graph.get_draft(node_id, source_mode)
        return d is not None and d.status == "done"

    def take(self, node_id: str, source_mode: str) -> Draft | Adoption | None:
        """For PageService.open (under its per-node lock): the finished draft of this page for
        `source_mode` (the caller promotes it and deletes the row), the running draft adopted,
        or None. Finished drafts of this page for other modes are dropped."""
        with self._cond:
            run = self._run
            if (run is not None and run.node_id == node_id and run.mode == source_mode
                    and run.state == "writing"):
                if run.cancelling:  # already stepping aside: write the page as usual
                    return None
                run.adopted = True
                run.bg.set_requested(True)  # real work now: not "thinking ahead", not pre-emptible
                return Adoption(self, run)
        found = None
        for d in self.graph.drafts_for(node_id):
            if d.status != "done":
                continue
            if d.source_mode == source_mode:
                found = d
            elif self.graph.delete_draft(node_id, d.source_mode):
                with self._cond:
                    self._discarded += 1
        if found is not None:
            with self._cond:
                self._used += 1
        return found

    def cancel_current(self) -> None:
        """Thinking ahead was turned off: stop the draft being written (unless a reader has
        adopted it) and forget the waiting candidates. Nothing half-written is kept."""
        with self._cond:
            self._pending.clear()
        self.cancel_beyond(0)

    def cancel_beyond(self, setting: int) -> None:
        """The setting is now `setting`: stop the draft being written if it is further ahead
        than that (unless a reader has adopted it). Waiting candidates beyond it are dropped
        when their turn comes."""
        with self._cond:
            run = self._stoppable()
            if run is None or run.ahead <= setting:
                return
            run.cancelling = True
        run.cancel.cancel()

    def _stoppable(self) -> _Run | None:
        """The draft being written, if it can still be stopped (caller holds the condition)."""
        run = self._run
        if run is None or run.state != "writing" or run.adopted or run.ended or run.cancelling:
            return None
        return run

    def shutdown(self) -> None:
        """Stop drafting: a draft being written (adopted or not) stops, its turn is released and
        nothing half-written is kept. Idempotent. A draft's grounding can't be interrupted, so
        while one is fetching web sources this may block for up to _JOIN_S (10 s); after that it
        returns anyway and the worker exits without writing anything once the fetch ends."""
        with self._cond:
            self._closed = True
            self._pending.clear()
            run = self._run
            if run is not None and run.state == "writing":
                run.cancelling = True
            self._cond.notify_all()
        if run is not None:
            run.cancel.cancel()
        if threading.current_thread() is not self._worker:
            self._worker.join(_JOIN_S)

    # the worker
    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._pending:
                    self._cond.wait()
                if self._closed:
                    return
                cand = self._pending[0]
            try:
                outcome = self._attempt(cand)
            except Exception:
                log.exception("drafting failed")
                outcome = "drop"
            with self._cond:
                if outcome == "drop":
                    if self._pending and self._pending[0] is cand:
                        self._pending.popleft()
                elif outcome == "busy" and not self._closed:
                    self._cond.wait(_IDLE_POLL_S)  # an offer or shutdown wakes it sooner
                # "retry": straight back round; the gate is busy with whatever pre-empted it.
                # A new page clears the queue first (moved_on) and an offer replaces it, so
                # only a candidate nobody moved away from is tried again.

    def _attempt(self, cand: _Candidate) -> str:
        """One go at a candidate: "drop" (done with it), "busy" (gate not idle) or "retry"."""
        if self._setting() < cand.ahead:
            return "drop"
        if cand.node is None:
            try:  # the node exactly as opening the card makes it, but no edge
                cand.node = self.graph.node_for(cand.concept, cand.kind, parent_id=cand.parent_id)
            except ValueError:
                return "drop"
        node = cand.node
        mode = self.pages.source_mode  # what a plain open would write in now
        if self.graph.latest_version(node.id) is not None or self.ready(node.id, mode):
            return "drop"
        if mode not in cand.grounded:  # before the GPU turn: grounding can't be pre-empted
            got_mode, sources, note = _drain(self.pages.ground(mode, node.title))
            if note or got_mode != mode:  # the page would fall back to storyteller: not now
                return "drop"
            cand.grounded[mode] = sources
        with self._cond:
            if self._closed:
                return "drop"
        bg = self.gate.background()
        if bg is None:
            return "busy"
        with bg:
            return self._write(node, mode, cand.grounded[mode], bg, cand.ahead)

    def _write(self, node, mode: str, sources: list, bg, ahead: int = 1) -> str:
        if bg.preempted():  # something asked for the GPU already: nothing started, nothing lost
            return "retry"
        run = _Run(node.id, node.title, mode, sources, StreamCancel(), bg, ahead)
        with self._cond:
            if self._closed:
                return "drop"
            self._run = run
        stop = threading.Event()
        pieces = None
        error = ""
        try:  # from here every path ends in _finish, so no run or "writing" row is left behind
            self.graph.put_draft(node.id, mode, "", sources, "writing")
            threading.Thread(target=self._watch, args=(run, bg, stop), name="primer-draft-watch",
                             daemon=True).start()
            pieces = self.pages.write(node, "normal", sources, cancel=run.cancel)
            for piece in pieces:
                preempted = bg.preempted()
                with self._cond:
                    run.chunks.append(piece)
                    self._cond.notify_all()
                    if preempted and not run.adopted:
                        run.cancelling = True
                    if run.cancelling:
                        break
            else:  # the whole page arrived: a pre-emption landing now must not throw it away
                with self._cond:
                    run.ended = True
        except EngineError as e:
            error = str(e) or "engine error"
        except Exception as e:  # a draft must never take the worker down
            log.exception("draft stream failed")
            error = str(e) or type(e).__name__
        finally:
            if pieces is not None:
                pieces.close()  # closes the HTTP stream if it is still open
            stop.set()
        with self._cond:
            cancelled = run.cancelling and not run.ended
            adopted, closed = run.adopted, self._closed
        markdown = "".join(run.chunks)
        if cancelled:
            return self._finish(run, "cancelled", "drop" if closed else "retry", "the page was stopped")
        if error or not markdown.strip():
            return self._finish(run, "failed", "drop", error or "empty response")
        try:
            if not adopted and self.graph.latest_version(node.id) is not None:
                return self._finish(run, "cancelled", "drop", "written meanwhile")  # opened another way
            # A page opened in another mode between this check and the save leaves a finished
            # draft for a node that now has a version. Harmless: open serves the cached version
            # and never takes it, and prune_drafts drops it in time.
            self.graph.put_draft(node.id, mode, markdown, sources, "done")
            self.graph.prune_drafts(KEEP)
        except Exception as e:
            log.exception("saving the draft failed")
            return self._finish(run, "failed", "drop", str(e) or type(e).__name__)
        return self._finish(run, "done", "drop")

    def _finish(self, run: _Run, state: str, outcome: str, error: str = "") -> str:
        """Record how the draft ended (the row is written or deleted first, so an adopter that
        wakes on the new state finds the table already right) and wake anyone waiting on it."""
        if state != "done":
            log.warning("draft of %r (%s) %s: %s", run.title, run.mode, state, error or "no reason given")
            try:
                self.graph.delete_draft(run.node_id, run.mode)
            except Exception:
                log.exception("deleting the draft failed")
        with self._cond:
            run.state, run.error = state, error
            if state == "done":
                self._written += 1
            else:
                self._discarded += 1
            if self._run is run:
                self._run = None
            self._cond.notify_all()
        return outcome

    def _watch(self, run: _Run, bg, stop: threading.Event) -> None:
        """Stop the stream the moment a real page or picture waits, even if no piece arrives
        (a stalled read only ends when its connection is closed)."""
        while not stop.is_set():
            if not bg.wait_preempted(_IDLE_POLL_S):
                continue  # timed out, adopted (never pre-empted then), or released (stop is set)
            with self._cond:
                if run.ended or run.state != "writing":
                    return  # the page is complete: nothing to stop
                if run.adopted:
                    continue  # adopted after the wait: keep watching, it may be given back
                run.cancelling = True
            run.cancel.cancel()
            return


def _drain(gen):
    """Run a generator to its end, ignoring what it yields; return its return value."""
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value
