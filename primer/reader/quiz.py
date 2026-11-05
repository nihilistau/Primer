"""Test me and Review: three questions written from a page, free-text answers marked against
the page, and spaced repetition over them.

Questions and marks are written by the writing (text) model at the GPU gate's page turn, like a
page. Its output is JSON, parsed tolerantly (code fences, leading text) but validated strictly;
bad output gets one retry, then a QuizError and nothing is stored.

The review ladder (`step`, 0 = new): not_yet -> step 0, back in 1 day; partly -> same step, back
in 2 days; right -> the next step of 1, 3, 7, 21, 60 days (stays at 60). New items are due a day
after they were asked. `misses` counts not_yet marks."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Callable

from primer.engines import EngineError
from primer.prompts import (door_messages, mark_messages, parse_object, quiz_messages,
                            trim_page)

log = logging.getLogger(__name__)

DAY = 86400.0
LADDER_DAYS = (1, 3, 7, 21, 60)  # after 1, 2, ... rights in a row
NOT_YET_DAYS = 1
PARTLY_DAYS = 2
MARKS = ("right", "partly", "not_yet")
ANSWER_CHARS = 2000
QUESTION_CHARS = 300
MODEL_ANSWER_CHARS = 600
FEEDBACK_CHARS = 300
HINT_CHARS = 160
EXCERPT_CHARS = 3000
EARLIER_TITLES = 5
FACTS_LIMIT = 8
KNOWS_STEP = 2
HARD_MISSES = 2
QUIZ_TOKENS = 900
MARK_TOKENS = 250
DOOR_TOKENS = 400
QUIZ_TEMPERATURE = 0.4
MARK_TEMPERATURE = 0.1

BAD_QUESTIONS = "the model's questions couldn't be read; try again"
BAD_MARK = "the model's mark couldn't be read; try again"
BAD_DOOR = "the model's question couldn't be read; try again"
WAS_RESET = "Reset happened; try again"
NO_TEXT = "no writing model in this tier"
NO_PAGE = "this page hasn't been written yet"
UNKNOWN_PAGE = "page not found"
UNKNOWN_ITEM = "question not found"
REVEALED = "You looked at the answer: this one comes back tomorrow."

_PUBLIC = ("id", "node_id", "title", "version_n", "kind", "question", "due_at", "step", "last_mark")
_WORDS = re.compile(r"[a-z0-9]{4,}")


class QuizError(Exception):
    """The model's output couldn't be used (after one retry), or a reset discarded the work."""


class QuizReset(QuizError):
    """A reset happened while the work was in flight; it was discarded (nothing stored)."""


class QuizUnavailable(Exception):
    """The writing model can't be reached or this tier has none; the message says why."""


class QuizNotFound(LookupError):
    pass


def public(item: dict) -> dict:
    """An item as the API shows it while asking (no model answer)."""
    return {k: item[k] for k in _PUBLIC}


def _text(value, limit: int) -> str | None:
    """A non-empty string as one paragraph, cut at a word to at most `limit` characters;
    None when it is missing or empty."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return _cut(value, limit) or None


_KIND_SYNONYMS = {
    "recall": "recall", "fact": "recall", "facts": "recall", "remember": "recall",
    "why": "why", "explain": "why", "explain_why": "why", "explanation": "why", "reason": "why",
    "connect": "connect", "connection": "connect", "connections": "connect", "link": "connect",
    "links": "connect", "relate": "connect", "related": "connect",
}


def _norm_kind(value) -> str:
    """A question kind, with the near-synonyms small models use mapped ("explain-why" -> why,
    "link" -> connect, "fact" -> recall); "" when it isn't one."""
    if not isinstance(value, str):
        return ""
    return _KIND_SYNONYMS.get(re.sub(r"[\s-]+", "_", value.strip().lower()), "")


def _names(text: str, title: str) -> bool:
    """`title` appears in `text` as whole words, ignoring case."""
    title = " ".join(str(title).split())
    return bool(title) and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text,
                                     re.IGNORECASE) is not None


def _norm_mark(value) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().lower()) if isinstance(value, str) else ""


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else text[:limit]).rstrip()


def check_answer(answer) -> str:
    """The reader's answer, or ValueError: a string of 1-2000 characters, not only spaces."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer is required")
    if len(answer) > ANSWER_CHARS:
        raise ValueError(f"answer must be at most {ANSWER_CHARS} characters")
    return answer.strip()


def excerpt(markdown: str, model_answer: str, limit: int = EXCERPT_CHARS) -> str:
    """The paragraph that shares most words with the model answer and its neighbours, or the
    page's start when none does; at most `limit` characters."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", markdown) if p.strip()]
    want = set(_WORDS.findall(model_answer.lower()))
    scores = [len(want & set(_WORDS.findall(p.lower()))) for p in paras]
    if not paras or max(scores) == 0:
        return trim_page(markdown, limit)
    best = scores.index(max(scores))
    text = "\n\n".join(paras[max(0, best - 1):best + 2])
    if len(text) > limit:
        text = trim_page(paras[best], limit)
    return text


def schedule(step: int, misses: int, mark: str, now: float) -> tuple[float, int, int]:
    """(due_at, step, misses) after a mark."""
    if mark == "right":
        step = min(step + 1, len(LADDER_DAYS))
        return now + LADDER_DAYS[step - 1] * DAY, step, misses
    if mark == "partly":
        return now + PARTLY_DAYS * DAY, step, misses
    return now + NOT_YET_DAYS * DAY, 0, misses + 1


class Quiz:
    """Test me and Review over a graph.

    `text_engine` is a zero-argument callable returning the current writing engine (or None),
    and `gate` a GpuGate, a zero-argument callable returning the current one, or None: both are
    read at call time, never kept across a tier switch. `signals` records an `answer` signal per
    mark; `on_answer()` (App wires Profile.nudge) is called after each mark, outside any lock
    of the Quiz. `clock` is wall-clock seconds (due dates).

    Lock order: a page's lock, then the GPU gate (as pages do); the Quiz's own lock is taken
    last and only around database reads and writes, never around a model call."""

    def __init__(self, graph, text_engine: Callable, gate, signals, *,
                 on_answer: Callable[[], object] | None = None, clock=time.time):
        self.graph = graph
        self.text_engine = text_engine
        self._gate = gate
        self.signals = signals
        self.on_answer = on_answer
        self.clock = clock
        self._lock = threading.Lock()  # guards the generation and read-modify-write of items
        self._generation = 0  # bumped by reset(): work started before it is discarded
        self._node_locks: dict[str, threading.Lock] = {}
        self._node_locks_guard = threading.Lock()

    # asking
    def make(self, node_id: str, fresh: bool = False) -> list[dict]:
        """Three items for the page's latest version: the ones already asked for it unless
        `fresh` (then new ones replace those never answered)."""
        node, version = self._page(node_id)
        with self._node_lock(node_id):
            if not fresh:
                existing = self._latest_batch(node_id, version.n)
                if existing:
                    return [public(i) for i in existing]
            generation = self._generation
            earlier = self.graph.titles_read_before(node_id, EARLIER_TITLES)
            kinds = ["recall", "why", "connect" if earlier else "why"]
            messages = quiz_messages(node.title, version.markdown, earlier)
            questions = self._ask(messages, QUIZ_TOKENS, QUIZ_TEMPERATURE,
                                  lambda v: self._questions(v, kinds, earlier), BAD_QUESTIONS)
            now = self.clock()
            items = [{"id": uuid.uuid4().hex, "node_id": node_id, "version_n": version.n,
                      "kind": q["kind"], "question": q["question"], "answer": q["answer"],
                      "created_at": now, "due_at": now + NOT_YET_DAYS * DAY, "step": 0,
                      "last_mark": None, "misses": 0} for q in questions]
            with self._lock:
                if generation != self._generation:
                    raise QuizReset(WAS_RESET)
                self.graph.add_quiz_items(items, replace=(node_id, version.n) if fresh else None)
        return [public({**i, "title": node.title}) for i in items]

    def door(self, node_id: str) -> dict:
        """One question about the page's latest version with its model answer and a one-line
        hint, for a quest's door: {question, answer, hint}. Not stored."""
        node, version = self._page(node_id)
        return self._ask(door_messages(node.title, version.markdown), DOOR_TOKENS,
                         QUIZ_TEMPERATURE, self._door, BAD_DOOR)

    # answering
    def mark(self, item_id: str, answer, revealed: bool = False) -> dict:
        """Mark an answer against the page and reschedule the item:
        {mark, feedback, answer (the model answer), item}. `revealed`: the reader saw the
        model answer first, so it is not_yet (back tomorrow) and no model is asked."""
        answer = check_answer(answer)
        item = self.graph.get_quiz_item(item_id)
        if item is None:
            raise QuizNotFound(UNKNOWN_ITEM)
        generation = self._generation
        if revealed:
            result = {"mark": "not_yet", "feedback": REVEALED}
        else:
            result = self.judge(item["question"], item["answer"], self._markdown(item), answer)
        with self._lock:  # lock order: the Quiz's lock, then the graph's (via signals too)
            current = self.graph.get_quiz_item(item_id)
            if generation != self._generation or current is None:
                raise QuizReset(WAS_RESET)
            now = self.clock()
            due_at, step, misses = schedule(current["step"], current["misses"], result["mark"], now)
            self.graph.mark_quiz_item(item_id, now, answer, result["mark"], result["feedback"],
                                      due_at, step, misses)
            updated = self.graph.get_quiz_item(item_id)
            try:  # inside the lock: a reset can't come between the answer and its signal
                self.signals.record("answer", item["node_id"], result["mark"])
            except Exception:
                log.exception("recording an answer signal failed")
        if self.on_answer is not None:  # outside the Quiz's lock (Profile's lock-order rule)
            try:
                self.on_answer()
            except Exception:
                log.exception("the answer callback failed")
        return {"mark": result["mark"], "feedback": result["feedback"], "answer": item["answer"],
                "item": public(updated)}

    def judge(self, question: str, model_answer: str, markdown: str, answer) -> dict:
        """Mark an answer to any question about a page (quests use it for doors): {mark,
        feedback}. ValueError for a bad answer; QuizError when the mark can't be read."""
        answer = check_answer(answer)
        messages = mark_messages(question, model_answer, excerpt(markdown, model_answer), answer)
        return self._ask(messages, MARK_TOKENS, MARK_TEMPERATURE, self._mark, BAD_MARK)

    # reading
    def item(self, item_id: str) -> dict:
        """One item with its model answer ("Show answer")."""
        item = self.graph.get_quiz_item(item_id)
        if item is None:
            raise QuizNotFound(UNKNOWN_ITEM)
        return {**public(item), "answer": item["answer"]}

    def for_node(self, node_id: str) -> list[dict]:
        """The page's items asked for its latest version (newest batch), else any it has."""
        version = self.graph.latest_version(node_id)
        items = self._latest_batch(node_id, version.n) if version is not None else []
        return [public(i) for i in (items or self.graph.quiz_items_for(node_id))]

    def due(self, limit: int = 20) -> list[dict]:
        return [public(i) for i in self.graph.quiz_due(self.clock(), max(0, int(limit)))]

    def counts(self) -> dict:
        return self.graph.quiz_counts(self.clock())

    def facts(self) -> tuple[list[str], list[str]]:
        """(knows, hard) page titles for the reader model: items last marked right at step 2
        or more, and items missed twice or more; at most 8 each."""
        return self.graph.quiz_facts(KNOWS_STEP, HARD_MISSES, FACTS_LIMIT)

    def earlier_titles(self, node_id: str) -> list[str]:
        return self.graph.titles_read_before(node_id, EARLIER_TITLES)

    def forget(self) -> None:
        """The reader reset's in-memory part (a Profile.on_reset hook; idempotent): questions
        being written or marked now are discarded when they return. The rows go in the reset's
        own transaction (Graph.reset_all_reader)."""
        with self._lock:
            self._generation += 1

    def reset(self) -> None:
        """Forget, and delete every item and answer (on its own, outside a reader reset)."""
        with self._lock:
            self._generation += 1
            self.graph.reset_quiz()

    # internals
    def gate(self):
        gate = self._gate
        if gate is not None and not hasattr(gate, "page") and callable(gate):
            gate = gate()
        return gate

    def _page(self, node_id: str):
        node = self.graph.get_node(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise QuizNotFound(UNKNOWN_PAGE)
        version = self.graph.latest_version(node_id)
        if version is None or not version.markdown.strip():
            raise QuizNotFound(NO_PAGE)
        return node, version

    def _node_lock(self, node_id: str) -> threading.Lock:
        with self._node_locks_guard:
            return self._node_locks.setdefault(node_id, threading.Lock())

    def _latest_batch(self, node_id: str, version_n: int) -> list[dict]:
        items = self.graph.quiz_items_for(node_id, version_n)
        return [i for i in items if i["created_at"] == items[0]["created_at"]] if items else []

    def _markdown(self, item: dict) -> str:
        for v in self.graph.versions(item["node_id"]):
            if v.n == item["version_n"]:
                return v.markdown
        latest = self.graph.latest_version(item["node_id"])
        return latest.markdown if latest is not None else ""

    def _ask(self, messages, max_tokens: int, temperature: float, read, error: str):
        """The writing model's reply at the gate, read by `read` (None = unusable); one retry."""
        for _ in range(2):
            engine = self.text_engine()  # read for each call: a tier switch may have replaced it
            if engine is None or not hasattr(engine, "complete"):
                raise QuizUnavailable(NO_TEXT)
            gate = self.gate()
            try:
                with gate.page() if gate is not None else nullcontext():
                    raw = engine.complete(messages, max_tokens=max_tokens, temperature=temperature)
            except EngineError as e:
                raise QuizUnavailable(str(e) or "the writing model failed") from None
            value = read(raw if isinstance(raw, str) else "")
            if value is not None:
                return value
            log.warning("unusable quiz output from the model: %r", (raw or "")[:200])
        raise QuizError(error)

    @staticmethod
    def _questions(raw: str, kinds: list[str], earlier=()) -> list[dict] | None:
        """Exactly three questions, each with a question and a model answer (cut at a word
        when too long). Kinds: near-synonyms mapped; when they still don't make the expected
        set, they are assigned by position (small models mislabel them). A connect question
        that names none of the `earlier` titles (whole words, any case) stays on this page:
        it is labelled why."""
        def ok(v) -> bool:
            qs = v.get("questions")
            if not isinstance(qs, list) or len(qs) != 3 or not all(isinstance(q, dict) for q in qs):
                return False
            return all(_text(q.get("question"), QUESTION_CHARS) and
                       _text(q.get("answer"), MODEL_ANSWER_CHARS) for q in qs)
        value = parse_object(raw, ok)
        if value is None:
            return None
        qs = [{"kind": _norm_kind(q.get("kind")), "question": _text(q["question"], QUESTION_CHARS),
               "answer": _text(q["answer"], MODEL_ANSWER_CHARS)} for q in value["questions"]]
        if sorted(q["kind"] for q in qs) != sorted(kinds):
            qs = [{**q, "kind": k} for q, k in zip(qs, kinds)]
        else:
            order = {"recall": 0, "why": 1, "connect": 2}
            qs = sorted(qs, key=lambda q: order[q["kind"]])  # stable: two whys keep their order
        for q in qs:
            if q["kind"] == "connect" and not any(_names(q["question"], t) for t in earlier):
                q["kind"] = "why"
        return qs

    @staticmethod
    def _mark(raw: str) -> dict | None:
        def ok(v) -> bool:
            return (_norm_mark(v.get("mark")) in MARKS and isinstance(v.get("feedback"), str)
                    and bool(v["feedback"].strip()))
        value = parse_object(raw, ok)
        if value is None:
            return None
        return {"mark": _norm_mark(value["mark"]),
                "feedback": _cut(" ".join(value["feedback"].split()), FEEDBACK_CHARS)}

    @staticmethod
    def _door(raw: str) -> dict | None:
        def ok(v) -> bool:
            return bool(_text(v.get("question"), QUESTION_CHARS)
                        and _text(v.get("answer"), MODEL_ANSWER_CHARS)
                        and isinstance(v.get("hint"), str) and v["hint"].strip())
        value = parse_object(raw, ok)
        if value is None:
            return None
        return {"question": _text(value["question"], QUESTION_CHARS),
                "answer": _text(value["answer"], MODEL_ANSWER_CHARS),
                "hint": _cut(" ".join(value["hint"].split()), HINT_CHARS)}
