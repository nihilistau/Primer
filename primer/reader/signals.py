"""Reading signals: pages opened and how, Deeper/Simpler/Regenerate, time on a page and quiz
answers, stored in the graph; and the counted profile worked out from them with no model."""
from __future__ import annotations

import copy
import threading
import time

KINDS = ("open", "depth", "regenerate", "answer")
BRANCH_VIAS = ("context", "general", "sideways")
VISIT_CAP_S = 600.0  # dwell seconds counted per visit, so a page left open overnight isn't "loved"
MIN_OPENS = 3  # fewer opens: too little to say anything about the reader
LEAN_SHARE = 0.5  # a curiosity style is clear when one branch kind has this share...
LEAN_BRANCHES = 6  # ...of at least this many branch opens, and more than any other kind
LEVEL_MIN = 2  # Deeper or Simpler asked at least this often (and more than the other) to lean
COUNTED_TTL_S = 5.0  # counted() is reused this long (any new signal or a reset drops it)
KAIROS_OPENED = "kairos_opened_page"  # the activity event for an open Kairos asked for

_STYLE_SENTENCE = {
    "context": "Follows phrases within the page's own context most",
    "general": "Follows the general idea of a phrase most",
    "sideways": "Follows sideways jumps most",
}
# the level lean in words: the counted sentence, the summary prompt's fact and the check that a
# summary already states it all use the same phrase
LEVEL_PHRASE = {"simpler": "simpler pages more than deeper", "deeper": "deeper pages more than simpler"}
_STYLE_FACT = {
    "context": "They follow phrases within the page's own context most.",
    "general": "They follow the general idea of a phrase most.",
    "sideways": "They follow sideways jumps most.",
}
_STYLE_HINT = {
    "context": "This reader tends to follow phrases within the page's own context.",
    "general": "This reader tends to follow the general idea of a phrase.",
    "sideways": "This reader tends to follow sideways jumps.",
}


class Signals:
    """Records what the reader does and counts it. Thread-safe (the graph serialises rows; the
    visit state has its own lock)."""

    def __init__(self, graph, *, clock=time.time, activity=None, kairos_claim=None):
        self.graph = graph
        self.clock = clock
        # a primer.activity.ActivitySink: each signal is also told to Kairos (None: not shared)
        self.activity = activity
        # node_id -> bool, true once for a page a Kairos command opened (primer.remote's
        # RemoteHub.claim_open; the App wires it): that open is told as kairos_opened_page
        self.kairos_claim = kairos_claim
        self._visit_lock = threading.Lock()
        self._visit_node: str | None = None
        self._visit_seconds = 0.0
        self._cache_lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None  # (monotonic time, counted())
        self._version = 0  # bumped by every invalidation: a count older than it is never stored

    def _invalidate(self) -> None:
        with self._cache_lock:
            self._cached = None
            self._version += 1

    def forget_visit(self) -> None:
        """Forget the in-memory visit (and the cached counts): the next dwell report starts a
        new visit. Used by the reader model's reset."""
        with self._visit_lock:
            self._visit_node, self._visit_seconds = None, 0.0
        self._invalidate()

    def record(self, kind: str, node_id: str | None = None, detail: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        self.graph.add_reader_event(self.clock(), kind, node_id, detail)
        self._invalidate()
        self._tell_kairos(kind, node_id, detail)

    def _tell_kairos(self, kind: str, node_id: str | None, detail: str) -> None:
        """The same signal for Kairos's activity record, when it is shared. Never raises: telling
        Kairos must not cost a reading signal."""
        if self.activity is None or not node_id:
            return
        try:
            node = self.graph.get_node(node_id)
            if node is not None:
                if kind == "open" and self._kairos_opened(node_id, detail):
                    kind = KAIROS_OPENED  # not something the reader chose: Kairos asked
                self.activity.note(kind, node.title, node_id, detail)
        except Exception:  # noqa: BLE001
            pass

    def _kairos_opened(self, node_id: str, detail: str) -> bool:
        """A page Kairos made (it arrived with via "kairos") or a command opened (claimed)."""
        if detail == "kairos":
            return True
        claim = self.kairos_claim
        return bool(claim is not None and claim(node_id))

    def dwell(self, node_id: str, seconds: float) -> float:
        """Add visible seconds on a page; returns the seconds counted. A visit is the run of
        reports for one node with no other node's report between; it counts at most 600 s.
        Unknown node ids are ignored (and do not end the visit)."""
        if seconds <= 0 or self.graph.get_node(node_id) is None:
            return 0.0
        with self._visit_lock:
            if node_id != self._visit_node:
                self._visit_node, self._visit_seconds = node_id, 0.0
            add = min(float(seconds), VISIT_CAP_S - self._visit_seconds)
            if add <= 0:
                return 0.0
            self._visit_seconds += add
            self.graph.add_dwell(node_id, add)
        self._invalidate()
        return add

    def since(self, kind: str, t: float) -> int:
        """Events of `kind` recorded after time `t` (the clock's time)."""
        return self.graph.reader_events_since(kind, t)

    def style_counts(self, opens: dict | None = None) -> dict:
        """The curiosity style alone (no topics query): shares of the branch kinds, the number
        of branch opens and the lean, which is set only over 6+ branch opens when one kind has
        at least half and strictly more than any other (a tie is "even")."""
        if opens is None:
            opens = self.graph.reader_event_counts("open")
        branches = sum(opens.get(v, 0) for v in BRANCH_VIAS)
        style = {v: (opens.get(v, 0) / branches if branches else 0.0) for v in BRANCH_VIAS}
        first, second = sorted((style[v] for v in BRANCH_VIAS), reverse=True)[:2]
        top = max(BRANCH_VIAS, key=lambda v: style[v])
        clear = branches >= LEAN_BRANCHES and first >= LEAN_SHARE and first > second
        style.update(branches=branches, lean=top if clear else "even")
        return style

    def counted(self) -> dict:
        """The counted profile (see the module doc); reused for up to 5 s unless a signal is
        recorded or the visit is forgotten in between. Callers get their own copy."""
        now = time.monotonic()
        with self._cache_lock:
            hit, version = self._cached, self._version
        if hit is not None and now - hit[0] < COUNTED_TTL_S:
            return copy.deepcopy(hit[1])
        opens = self.graph.reader_event_counts("open")
        depth = self.graph.reader_event_counts("depth")
        deeper, simpler = depth.get("deeper", 0), depth.get("simpler", 0)
        lead = max(deeper, simpler)
        level_lean = "even"
        if lead >= LEVEL_MIN and deeper != simpler:
            level_lean = "deeper" if deeper > simpler else "simpler"
        out = {"opens": sum(opens.values()),
               "level": {"deeper": deeper, "simpler": simpler, "lean": level_lean},
               "style": self.style_counts(opens),
               "topics": self.graph.reader_topics(8)}
        with self._cache_lock:
            if self._version == version:  # no signal arrived while counting
                self._cached = (now, out)
        return copy.deepcopy(out)


def counted_sentences(counted: dict) -> list[tuple[str, tuple[str, ...], str]]:
    """The counted profile as plain sentences: [(part, keys, sentence)] where part is "level",
    "style" or "topics" and keys are the words (the style kind) or titles (the topics) a summary
    may already state; [] under 3 opens."""
    if counted.get("opens", 0) < MIN_OPENS:
        return []
    out = []
    lean = counted["level"]["lean"]
    if lean in LEVEL_PHRASE:
        out.append(("level", (LEVEL_PHRASE[lean],), f"Asks for {LEVEL_PHRASE[lean]} ones."))
    style = counted["style"]
    if style["lean"] in _STYLE_SENTENCE:
        out.append(("style", (style["lean"],),
                    f"{_STYLE_SENTENCE[style['lean']]} ({round(style[style['lean']] * 100)}%)."))
    titles = tuple(t["title"] for t in counted["topics"][:5])
    if titles:
        out.append(("topics", titles, topics_sentence(titles)))
    return out


def lean_facts(counted: dict) -> list[str]:
    """The counted leans as facts for the summary prompt to repeat ("They ask for simpler pages
    more than deeper ones."); [] when neither lean is clear."""
    out = []
    lean = counted.get("level", {}).get("lean")
    if lean in LEVEL_PHRASE:
        out.append(f"They ask for {LEVEL_PHRASE[lean]} ones.")
    style = counted.get("style", {}).get("lean")
    if style in _STYLE_FACT:
        out.append(_STYLE_FACT[style])
    return out


def topics_sentence(titles) -> str:
    return "Reads most about: " + ", ".join(titles) + "."

def counted_block(counted: dict) -> str:
    """Short plain sentences about the reader from the counted profile; "" under 3 opens."""
    return " ".join(sentence for _, _, sentence in counted_sentences(counted))

def style_hint(counted: dict) -> str:
    """One line for the branch-card and highlight prompts when the curiosity style is clear."""
    return _STYLE_HINT.get(counted["style"]["lean"], "")
