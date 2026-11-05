"""The reader model: the counted profile, a short summary the fast model writes in the background,
and the reader's own notes, which always win. Its block() is the "About this reader" text for
page prompts.

Stored in the graph's reader_profile(key, value): summary, summary_at (wall clock), notes,
enabled ("1"/"0", default on), generation (bumped by every reset), opens_at_summary and
answers_at_summary (the signal counts the last summary was written from)."""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

from primer.prompts import clean_summary, profile_summary_messages

from .signals import MIN_OPENS, counted_block, counted_sentences, style_hint, topics_sentence

log = logging.getLogger(__name__)

NOTES_CHARS = 1000
BLOCK_CHARS = 800
BLOCK_NOTES_CHARS = 500  # the notes' share of the block: the summary and leans keep room
REFRESH_OPENS = 8  # a new summary after this many page opens...
REFRESH_ANSWERS = 5  # ...or this many quiz answers since the last one,
REFRESH_EVERY_S = 120.0  # ...at most this often, and only when the fast engine is free
# ...but a reader who keeps reading keeps branch cards queued: a due summary that has waited
# this long behind them runs anyway (the fast server takes both, a little slower)
BUSY_PATIENCE_S = 300.0
SUMMARY_TOKENS = 200
SUMMARY_TEMPERATURE = 0.2

NO_FAST = "no fast model in this tier"
FAST_BUSY = "the fast model is busy"
WRITING = "already writing"
TOO_LITTLE = "read a few pages first"
CLOSED = "shutting down"
SWITCHED_OFF = "the profile is switched off (Use this in pages)"
NOTES_LEAD = "In the reader's own words: "
RESET_HOOK_FAILED = "the reset could not clear everything; try Reset again"
RESETTING = "resetting"  # a reset's hooks are still clearing their tables


def _says(text: str, phrase: str) -> bool:
    """`phrase` appears in `text` as whole words, ignoring case."""
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.IGNORECASE) is not None


def _cut(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    return (cut[:space] if space > 0 else text[:limit]).rstrip()


class Profile:
    """One reader's profile over a graph and its Signals.

    `fast` is the fast engine (anything with complete(messages, max_tokens=, temperature=)) or a
    zero-argument callable returning the current one (or None): App passes a callable so a tier
    switch is picked up. `quiz_facts() -> (knows, hard)` lists what quizzes say the reader knows
    and found hard (default: nothing). `pages_busy() -> bool` is True while the pages are using
    the fast engine; a summary is then skipped (not queued) until the next nudge. `clock` is
    monotonic (the 120 s spacing); `wall` stamps summary_at.

    `on_reset` is a list of zero-argument callables reset() runs to clear in-memory state (Test
    me/Review and Quests bump their generations, so work in flight is discarded); the rows
    themselves -- signals, dwell, profile, quiz items and answers, quests -- go in one
    transaction (Graph.reset_all_reader) right after the hooks. Hooks run outside the profile's
    own lock, each on its own: one that raises is logged, the rest still run, and
    state()["error"] says the reset was incomplete until a later reset completes (a summary
    written meanwhile doesn't clear it). No summary starts while a reset runs. Hooks must be
    idempotent: two concurrent resets may run them twice.

    `on_change` is a list of zero-argument callables run (outside the lock, errors logged)
    whenever what page prompts get changes for a reason other than reading: the notes, the
    switch, or a reset. App wires the drafter's discard: a page drafted ahead with the old
    profile is never served.

    Lock order: nudge(), refresh() and state() take the profile's lock briefly. Callers (Quiz's
    on_answer, say) must not call them while holding their own lock, since a reset hook may be
    holding that lock's counterpart; call them after releasing it."""

    def __init__(self, graph, signals, fast, *, quiz_facts: Callable[[], tuple] | None = None,
                 pages_busy: Callable[[], bool] | None = None, clock=time.monotonic, wall=time.time):
        self.graph = graph
        self.signals = signals
        self._fast = fast
        self.quiz_facts = quiz_facts or (lambda: ([], []))
        self.pages_busy = pages_busy or (lambda: False)
        self.clock = clock
        self.wall = wall
        self.on_reset: list[Callable[[], None]] = []
        self.on_change: list[Callable[[], None]] = []
        self._state = threading.Lock()  # guards the values below and their rows
        self._writing = threading.Lock()  # held (non-blocking) for the whole of a summary call
        self._thread: threading.Thread | None = None
        self._last_start: float | None = None
        self._due_since: float | None = None  # when a due summary was first put off as busy
        self._closed = False
        self._error = ""  # the last summary's failure
        self._reset_error = ""  # an incomplete reset; cleared by the next complete one
        self._resetting = 0  # resets whose hooks are running: no summary may start
        stored = graph.reader_profile()
        self._summary = stored.get("summary", "")
        self._summary_at = float(stored["summary_at"]) if stored.get("summary_at") else None
        self._notes = stored.get("notes", "")
        self._enabled = stored.get("enabled", "1") != "0"
        self._generation = int(stored.get("generation", "0") or 0)
        self._opens_mark = int(stored.get("opens_at_summary", "0") or 0)
        self._answers_mark = int(stored.get("answers_at_summary", "0") or 0)

    # what the Reader window shows
    def state(self) -> dict:
        knows, hard = self._facts()
        with self._state:
            out = {"summary": self._summary, "notes": self._notes, "enabled": self._enabled,
                   "updated_at": self._summary_at, "generation": self._generation,
                   "error": self._reset_error or self._error}
        reason = NO_FAST if self.fast_engine() is None else ""
        out.update(counted=self.signals.counted(), knows=knows, hard=hard,
                   writing=self._writing.locked(), fast={"ok": not reason, "reason": reason})
        return out

    def set_notes(self, text: str) -> None:
        if not isinstance(text, str):
            raise ValueError("notes must be a string")
        if len(text) > NOTES_CHARS:
            raise ValueError(f"notes must be at most {NOTES_CHARS} characters")
        text = text.strip()
        with self._state:
            changed = text != self._notes
            self._notes = text
            self.graph.set_reader_profile({"notes": text})
        if changed:
            self._changed()

    def set_enabled(self, on: bool) -> None:
        if not isinstance(on, bool):
            raise ValueError("enabled must be true or false")
        with self._state:
            changed = on != self._enabled
            self._enabled = on
            self.graph.set_reader_profile({"enabled": "1" if on else "0"})
        if changed:
            self._changed()

    def _changed(self) -> None:
        for hook in list(self.on_change):
            try:
                hook()
            except Exception:
                log.exception("a reader change hook failed")

    def reset(self) -> None:
        """Forget the reader: signals, dwell, summary, notes, quiz items and answers, and
        quests, in one transaction. Pages and the enabled switch are kept. A summary being
        written now is discarded when it returns (its generation is stale), and so is quiz or
        quest work in flight (the on_reset hooks bump their generations first)."""
        with self._state:
            self._generation += 1
            self._resetting += 1
            self._last_start = None
            self._due_since = None
            hooks = list(self.on_reset)
        # The hooks run first and outside the lock (the bumped generation already discards a
        # late summary), each on its own: one that fails doesn't stop the rest. Quiz or quest
        # rows written before a hook ran are deleted by the transaction after it; any written
        # after were refused by the bumped generation.
        failed = False
        try:
            for hook in hooks:
                try:
                    hook()
                except Exception:
                    log.exception("a reader reset hook failed")
                    failed = True
            with self._state:
                self.graph.reset_all_reader({"enabled": "1" if self._enabled else "0",
                                             "generation": str(self._generation)})
                self._summary, self._summary_at, self._notes, self._error = "", None, "", ""
                self._opens_mark = self._answers_mark = 0
                self.signals.forget_visit()  # (also drops the cached counts)
        finally:
            with self._state:
                self._resetting -= 1
                self._reset_error = RESET_HOOK_FAILED if failed else ""
        self._changed()

    # the background summary
    def fast_engine(self):
        """The current fast engine, or None when the tier has none usable."""
        fast = self._fast
        if fast is not None and not hasattr(fast, "complete") and callable(fast):
            try:
                fast = fast()
            except Exception:
                log.exception("finding the fast engine failed")
                return None
        return fast if fast is not None and hasattr(fast, "complete") else None

    def nudge(self) -> bool:
        """Start a background summary if one is due: on, 8+ new opens or 5+ new quiz answers
        since the last summary, 120 s since the last start, and the fast engine free. Never
        waits; True when one was started."""
        try:
            with self._state:
                if not self._enabled or self._closed:
                    return False
                last, opens_mark, answers_mark = self._last_start, self._opens_mark, self._answers_mark
            if last is not None and self.clock() - last < REFRESH_EVERY_S:
                return False
            opens, answers = self._totals()
            if opens - opens_mark < REFRESH_OPENS and answers - answers_mark < REFRESH_ANSWERS:
                return False
            now = self.clock()
            with self._state:
                waited = now - self._due_since if self._due_since is not None else 0.0
            started = self._start(force=waited >= BUSY_PATIENCE_S) == ""
            with self._state:
                if started:
                    self._due_since = None
                elif self._due_since is None:
                    self._due_since = now
            return started
        except Exception:
            log.exception("the reader model's nudge failed")
            return False

    def refresh(self) -> str:
        """Start a summary now (the reader asked: thresholds, spacing and branch-card prefetch are
        ignored; the switch is not: off, nothing is written). Returns "" when started, else the
        reason it was skipped."""
        with self._state:
            if not self._enabled:
                return SWITCHED_OFF
        opens, _ = self._totals()
        knows, hard = self._facts()
        if opens < MIN_OPENS and not knows and not hard:
            return TOO_LITTLE
        return self._start(force=True)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a summary in progress; True when none is running (tests, shutdown)."""
        t = self._thread
        if t is not None:
            t.join(timeout)
            return not t.is_alive()
        return True

    def shutdown(self, timeout: float = 2.0) -> None:
        """No new summaries; waits briefly for one in progress (its thread is a daemon)."""
        with self._state:
            self._closed = True
        self.wait(timeout)

    def _start(self, force: bool = False) -> str:
        with self._state:
            if self._closed:
                return CLOSED
        engine = self.fast_engine()
        if engine is None:
            return NO_FAST
        if not self._writing.acquire(blocking=False):
            return WRITING
        try:  # until the thread runs, releasing the lock is ours to do
            if not force and self.pages_busy():
                self._writing.release()
                return FAST_BUSY
            with self._state:
                if self._resetting:  # checked with the generation, under the same lock
                    self._writing.release()
                    return RESETTING
                self._last_start = self.clock()
                generation = self._generation
            t = threading.Thread(target=self._write, args=(engine, generation),
                                 name="primer-reader-summary", daemon=True)
            self._thread = t
            t.start()
        except BaseException:
            self._writing.release()
            raise
        return ""

    def _write(self, engine, generation: int) -> None:
        try:
            opens, answers = self._totals()
            knows, hard = self._facts()
            with self._state:
                previous = self._summary
            messages = profile_summary_messages(self.signals.counted(),
                                                self.graph.reader_recent_titles(12),
                                                knows, hard, previous=previous)
            try:
                summary = clean_summary(engine.complete(messages, max_tokens=SUMMARY_TOKENS,
                                                        temperature=SUMMARY_TEMPERATURE))
                error = "" if summary else "the fast model wrote nothing"
            except Exception as e:
                log.warning("the reader summary failed: %s", e)
                summary, error = "", f"the summary failed ({e})"
            with self._state:
                if generation != self._generation:  # a reset happened meanwhile: discard
                    return
                self._error = error
                if not summary:
                    return
                self._summary, self._summary_at = summary, self.wall()
                self._opens_mark, self._answers_mark = opens, answers
                self.graph.set_reader_profile({
                    "summary": summary, "summary_at": repr(self._summary_at),
                    "opens_at_summary": str(opens), "answers_at_summary": str(answers)})
        except Exception:
            log.exception("the reader summary failed")
        finally:
            self._writing.release()

    def _totals(self) -> tuple[int, int]:
        """All page opens and quiz answers recorded (since the last reset)."""
        return (sum(self.graph.reader_event_counts("open").values()),
                sum(self.graph.reader_event_counts("answer").values()))

    def _facts(self) -> tuple[list[str], list[str]]:
        try:
            knows, hard = self.quiz_facts()
            return list(knows), list(hard)
        except Exception:
            log.exception("reading quiz results failed")
            return [], []

    # what page prompts get
    def style(self) -> str:
        """The curiosity-style line for branch-card and highlight prompts; "" when the
        profile is switched off (or the style isn't clear)."""
        with self._state:
            if not self._enabled:
                return ""
        return style_hint({"style": self.signals.style_counts()})

    def block(self) -> str:
        """"About this reader" for page prompts (<= 800 chars; prompts.reader_section quotes and
        sanitises it again): the reader's notes first, then the summary, then the counted
        sentences the summary doesn't already cover; with no summary yet, the counted block.
        The notes take at most 500 characters, so the summary and leans keep room. The level
        lean is left out only when the summary already says it in the same words. "" when the
        profile is switched off."""
        with self._state:
            if not self._enabled:
                return ""
            notes, summary = self._notes, self._summary
        counted = self.signals.counted()
        parts = [_cut(NOTES_LEAD + " ".join(notes.split()), BLOCK_NOTES_CHARS)] if notes else []
        if summary:
            rest = []
            for part, keys, sentence in counted_sentences(counted):
                if part in ("style", "level") and _says(summary, keys[0]):
                    continue  # the summary already names this curiosity style or level lean
                if part == "topics":  # only the titles the summary doesn't name
                    titles = [t for t in keys if not _says(summary, t)]
                    if not titles:
                        continue
                    sentence = topics_sentence(titles)
                rest.append(sentence)
            parts.append(" ".join([summary, *rest]))
        else:
            counted_text = counted_block(counted)
            if counted_text:
                parts.append(counted_text)
        return _cut("\n".join(parts), BLOCK_CHARS)
