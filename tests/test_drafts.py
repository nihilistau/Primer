"""Thinking ahead: while the reader reads, the idle GPU drafts the likely next page."""
from __future__ import annotations

import json
import re
import threading
import time

import pytest

from primer.drafts import Drafter
from primer.engines import EngineError
from primer.gpu import GpuGate
from primer.graph import Graph
from primer.grounding import Source
from primer.pages import OpenRequest, PageService
from primer.prompts import parse_branches
from tests.fakes import FakeEngine

WAIT = 3.0    # every wait is bounded so a regression fails instead of hanging
BOUND = 1.5   # a real page or picture behind a draft starts within this (plan: pre-emption bound)
DRAFT = "counterweight@Trebuchet"  # the first phrase's "In this context" page
GENERAL = "counterweight"          # the same phrase's "General" page
SIEGE = "# Siege\nA siege brought a counterweight engine up against the walls."
SIEGE_DRAFT = "counterweight@Siege"  # the first phrase's "In this context" page of Siege

CARDS = json.dumps({"context": {"title": "Counterweight in the trebuchet", "blurb": "What made it throw"},
                    "general": {"title": "Counterweight", "blurb": "The idea on its own"}})


def _until(pred, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _fast(messages):
    system = messages[0]["content"]
    if system.startswith("Return a JSON array"):
        return '["counterweight"]'
    if system.startswith("You suggest three"):
        return CARDS
    return "[]"


class Text:
    """A text engine the test can pace. Each stream is keyed by the page asked for ("title", or
    "title@parent" for an in-context page). hold[key] = (n, Event) pauses that page after n
    pieces until the Event is set; pace[key] sleeps between pieces. A bound StreamCancel wakes a
    paused stream the way the real engines' socket shutdown does."""

    def __init__(self):
        self.calls: list[str] = []
        self.hold: dict[str, tuple[int, threading.Event]] = {}
        self.pace: dict[str, float] = {}
        self.yielded: dict[str, int] = {}
        self.closed: list[str] = []  # keys whose stream ended before the whole reply
        self.fail: set[str] = set()  # keys whose stream breaks after one piece
        self.after: dict = {}  # key -> fn(aborted Event), run after the last piece, before the end
        self.texts: dict[str, str] = {}  # key -> reply, instead of Text.reply(key)

    @staticmethod
    def key(messages) -> str:
        ask = messages[-1]["content"]
        m = re.search(r'about "([^"]+)"( as it relates to "([^"]+)")?', ask)
        return m.group(1) + (f"@{m.group(3)}" if m.group(2) else "")

    @staticmethod
    def reply(key: str) -> str:
        if key == "Trebuchet":
            return "# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones over castle walls."
        return f"# {key}\n" + "Some words about this page, written slowly. " * 3

    def stream(self, messages, *, max_tokens=1200, temperature=0.7, cancel=None):
        key = self.key(messages)
        self.calls.append(key)
        aborted = threading.Event()
        if cancel is not None:
            cancel.bind(aborted.set)
        reply = self.texts.get(key) or self.reply(key)
        n_hold, release = self.hold.get(key, (None, None))
        done = False
        try:
            for i, start in enumerate(range(0, len(reply), 5)):
                if i == n_hold:
                    while not release.wait(0.005):
                        if aborted.is_set():
                            raise EngineError("stream cancelled")
                if key in self.pace:
                    aborted.wait(self.pace[key])
                if aborted.is_set():
                    raise EngineError("stream cancelled")
                if key in self.fail and i == 1:
                    raise EngineError("engine fell over")
                self.yielded[key] = self.yielded.get(key, 0) + 1
                yield reply[start:start + 5]
            if key in self.after:
                self.after[key](aborted)
            done = True
        finally:
            if not done:
                self.closed.append(key)

    def complete(self, messages, **kw):
        return "".join(self.stream(messages))

    def health(self):
        return True


class _Grounder:
    def __init__(self, sources):
        self.sources, self.calls = list(sources), []

    def gather(self, mode, query, status):
        self.calls.append((mode, query))
        status("Searching…")
        return self.sources, ""


_WEB = [Source(1, "Trebuchet facts", "web", "A trebuchet is a siege engine.", url="https://example.com/t",
               where="example.com")]


class Env:
    def __init__(self, tmp_path, mode="storyteller", grounder=None, setting=1, gate_mode="auto"):
        self.graph = Graph(tmp_path / "primer.sqlite")
        self.text = Text()
        self.gate = GpuGate(gate_mode, lambda: None, lambda: None)
        self.pages = PageService(self.graph, self.text, FakeEngine(_fast), mode, self.gate, grounder)
        self.setting = [setting]
        self.drafter = Drafter(self.graph, self.pages, self.gate, lambda: self.setting[0])

    def open_parent(self) -> str:
        return dict(list(self.pages.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]

    def card(self, parent: str, via: str = "context") -> OpenRequest:
        """What the reader sends when it clicks the first phrase's card (Reader.jsx)."""
        return OpenRequest("counterweight", via, from_id=parent, phrase="counterweight")

    def ctx_node(self, parent: str):
        return self.graph.node_for("counterweight", "context", parent_id=parent)

    def mid_draft(self, key=DRAFT, pieces=2) -> bool:
        return _until(lambda: self.text.yielded.get(key, 0) >= pieces and self.gate.state()["background"])

    def close(self):
        self.pages.shutdown()  # shuts the drafter down too


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_draft_written_when_idle_and_promoted(tmp_path):
    grounder = _Grounder(_WEB)
    env = Env(tmp_path, mode="web", grounder=grounder)
    try:
        parent = env.open_parent()
        node = env.ctx_node(parent)
        assert _until(lambda: env.drafter.ready(node.id, "web"))
        assert env.graph.versions(node.id) == []  # nothing visible yet
        assert not env.drafter.ready(node.id, "storyteller")
        ev = list(env.pages.open(env.card(parent)))
        st = env.drafter.status()
        calls = list(env.text.calls)
    finally:
        env.close()
    kinds = [k for k, _ in ev]
    d = dict(ev)
    assert kinds.count("delta") == 1 and "waiting" not in kinds and "grounding" not in kinds
    assert kinds[-2:] == ["done", "highlights"] and kinds.index("sources") < kinds.index("delta")
    assert d["node"]["id"] == node.id
    assert d["delta"]["text"] == Text.reply(DRAFT)
    assert d["done"] == {"n": 1, "source_mode": "web", "depth": "normal", "cached": False, "note": "",
                         "drafted": True}
    assert d["sources"]["sources"][0]["title"] == "Trebuchet facts"
    v = env.graph.latest_version(node.id)
    assert v.markdown == Text.reply(DRAFT) and v.source_mode == "web"
    assert env.graph.sources(node.id, 1)[0].url == "https://example.com/t"
    assert calls.count(DRAFT) == 1 and grounder.calls == [("web", "Trebuchet"), ("web", "counterweight")]
    assert env.graph.get_draft(node.id, "web") is None  # promoted: the draft is gone
    assert st["setting"] == 1 and st["drafting"] is None
    assert (st["written"], st["used"], st["discarded"], st["hit_rate"]) == (1, 1, 0, 1.0)


def test_open_of_an_ordinary_page_says_not_drafted(env):
    ev = list(env.pages.open(OpenRequest("Castle", "typed")))
    assert dict(ev)["done"]["drafted"] is False
    again = list(env.pages.open(OpenRequest("Castle", "typed")))
    assert dict(again)["done"]["drafted"] is False and dict(again)["done"]["cached"] is True


@pytest.mark.parametrize("how", ["stalled", "trickling"])
def test_draft_preempted_by_page(env, how, caplog):
    caplog.set_level("WARNING", logger="primer.drafts")
    release = threading.Event()
    if how == "stalled":  # no piece arrives: only the watchdog can stop it
        env.text.hold[DRAFT] = (2, release)
    else:  # pieces keep coming: the drafter notices between them
        env.text.pace[DRAFT] = 0.05
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    seen = {}
    t0 = time.monotonic()
    events = []
    for k, data in env.pages.open(OpenRequest("Castle", "typed")):
        events.append(k)
        if k == "waiting":
            assert data["reason"] == "finishing a draft…"
        if k == "delta" and not seen:
            seen = {"at": time.monotonic() - t0, "draft": env.graph.get_draft(node.id, "storyteller"),
                    "versions": env.graph.versions(node.id), "closed": list(env.text.closed),
                    "background": env.gate.state()["background"]}
    assert "waiting" in events and events[-2:] == ["done", "highlights"]
    assert seen["at"] < BOUND
    assert seen["draft"] is None and seen["versions"] == [] and DRAFT in seen["closed"]
    assert seen["background"] is False
    assert env.drafter.status()["discarded"] == 1
    assert any("cancelled" in r.getMessage() for r in caplog.records if r.name == "primer.drafts")
    # thrown away, and not tried again: the reader has moved on to another page
    release.set()
    env.text.pace.pop(DRAFT, None)
    time.sleep(0.4)
    assert env.text.calls.count(DRAFT) == 1 and env.graph.drafts_for(node.id) == []


def test_draft_preempted_by_image(env):
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    started, seen = threading.Event(), {}

    def picture():
        with env.gate.image():
            seen.update(draft=env.graph.get_draft(node.id, "storyteller"), versions=env.graph.versions(node.id))
            started.set()

    t0 = time.monotonic()
    threading.Thread(target=picture, daemon=True).start()
    assert started.wait(BOUND) and time.monotonic() - t0 < BOUND
    assert seen == {"draft": None, "versions": []} and DRAFT in env.text.closed
    assert env.drafter.status()["discarded"] == 1
    # a picture is not the reader moving on: the draft is tried again once the GPU is idle
    env.gate.release_idle()  # what ImageService does when its queue drains
    assert _until(lambda: env.text.calls.count(DRAFT) == 2)


def test_open_adopts_running_draft(env):
    go = threading.Event()
    env.text.hold[DRAFT] = (2, go)
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    gen = env.pages.open(env.card(parent))
    kinds, deltas = [], []
    t0 = time.monotonic()
    for k, data in gen:
        kinds.append(k)
        if k == "delta":
            deltas.append(data["text"])
            break
    assert time.monotonic() - t0 < 0.5 and deltas == [Text.reply(DRAFT)[:10]]  # the text so far, at once
    assert "waiting" not in kinds
    assert env.gate.state()["background"] is False and env.gate.state()["holder"] == "page"  # real work now
    # adopted: a picture asking for the GPU now waits for the page instead of stopping it
    pictured = threading.Event()
    threading.Thread(target=lambda: env.gate.image().__enter__() and pictured.set(), daemon=True).start()
    assert _until(lambda: env.gate.state()["waiting_images"] == 1)
    time.sleep(0.2)
    assert DRAFT not in env.text.closed and not pictured.is_set()
    env.text.pace[DRAFT] = 0.01  # the rest arrives piece by piece
    go.set()
    rest = list(gen)
    deltas += [d["text"] for k, d in rest if k == "delta"]
    done = dict(rest)["done"]
    assert "".join(deltas) == Text.reply(DRAFT) and len(deltas) > 2  # the rest streamed
    assert done["drafted"] is True and done["n"] == 1 and done["cached"] is False
    assert [v.n for v in env.graph.versions(node.id)] == [1]
    assert env.text.calls.count(DRAFT) == 1  # no duplicate writing
    assert env.graph.get_draft(node.id, "storyteller") is None
    assert pictured.wait(WAIT)
    st = env.drafter.status()
    assert (st["written"], st["used"], st["discarded"]) == (1, 1, 0)


def test_draft_ignored_for_other_mode(tmp_path):
    env = Env(tmp_path, mode="storyteller", grounder=_Grounder(_WEB))
    try:
        parent = env.open_parent()
        node = env.ctx_node(parent)
        assert _until(lambda: env.drafter.ready(node.id, "storyteller"))
        env.pages.source_mode = "reference"  # the reader's default changed after drafting
        ev = list(env.pages.open(env.card(parent)))
    finally:
        env.close()
    done = dict(ev)["done"]
    assert done["drafted"] is False and done["source_mode"] == "reference"
    assert "".join(d["text"] for k, d in ev if k == "delta") == Text.reply(DRAFT)
    assert env.text.calls.count(DRAFT) == 2  # written afresh in the new mode
    assert env.graph.drafts_for(node.id) == []  # the storyteller draft was dropped
    assert env.drafter.status()["discarded"] == 1 and env.drafter.status()["used"] == 0


def test_no_edge_until_opened(env):
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert _until(lambda: env.drafter.ready(node.id, "storyteller"))
    assert not any(e["to_id"] == node.id for e in env.graph.snapshot()["edges"])
    assert env.graph.versions(node.id) == [] and env.pages.page(node.id) is None
    assert env.graph.recent_titles() == ["Trebuchet"]
    list(env.pages.open(env.card(parent)))
    assert [(e["from_id"], e["via"]) for e in env.graph.snapshot()["edges"] if e["to_id"] == node.id] == \
        [(parent, "context")]


def test_setting_off_and_two_ahead(env):
    offered = []
    real_offer = env.drafter.offer
    env.drafter.offer = lambda p: (offered.append(p), real_offer(p))
    env.setting[0] = 0
    parent = env.open_parent()
    assert _until(lambda: offered == [parent])
    time.sleep(0.3)
    assert env.text.calls == ["Trebuchet"] and env.drafter.status()["written"] == 0
    env.setting[0] = 2
    list(env.pages.open(OpenRequest("Trebuchet", "start")))  # reading it again offers again
    ctx, general = env.ctx_node(parent), env.graph.node_for("counterweight", "concept")
    assert _until(lambda: env.drafter.ready(ctx.id, "storyteller") and env.drafter.ready(general.id, "storyteller"))
    assert env.text.calls == ["Trebuchet", DRAFT, GENERAL]
    # the General card opens the drafted page too
    ev = list(env.pages.open(env.card(parent, via="general")))
    assert dict(ev)["node"]["id"] == general.id and dict(ev)["done"]["drafted"] is True


@pytest.mark.parametrize("via", ["drafter", "pages"])  # pages: a tier reload shuts the PageService down
def test_drafter_shutdown_mid_draft(env, via):
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    t0 = time.monotonic()
    (env.drafter if via == "drafter" else env.pages).shutdown()
    assert time.monotonic() - t0 < BOUND
    st = env.gate.state()
    assert st["holder"] is None and st["background"] is False
    assert env.graph.drafts_for(node.id) == [] and env.graph.versions(node.id) == []
    assert DRAFT in env.text.closed and env.drafter.status()["drafting"] is None
    env.drafter.offer(parent)  # nothing more once shut down
    time.sleep(0.2)
    assert env.text.calls.count(DRAFT) == 1
    env.drafter.shutdown()  # idempotent


def test_shutdown_while_adopted_ends_the_open_with_an_error(env):
    env.text.hold[DRAFT] = (2, threading.Event())
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    gen = env.pages.open(env.card(parent))
    for k, _ in gen:
        if k == "delta":
            break
    threading.Timer(0.1, env.drafter.shutdown).start()
    rest = list(gen)
    assert rest[-1][0] == "error" and "done" not in dict(rest)
    assert env.graph.versions(node.id) == [] and env.graph.drafts_for(node.id) == []
    assert env.gate.state()["holder"] is None


def test_failed_draft_leaves_nothing_and_the_page_is_written_as_usual(env, caplog):
    caplog.set_level("WARNING", logger="primer.drafts")
    env.text.fail.add(DRAFT)
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert _until(lambda: env.drafter.status()["discarded"] == 1)
    assert env.graph.drafts_for(node.id) == [] and env.drafter.status()["drafting"] is None
    assert any("failed" in r.getMessage() and "engine fell over" in r.getMessage() for r in caplog.records)
    assert _until(lambda: env.gate.state()["holder"] is None)
    env.text.fail.clear()
    ev = list(env.pages.open(env.card(parent)))
    assert dict(ev)["done"]["drafted"] is False and env.text.calls.count(DRAFT) == 2


def test_a_plain_fake_engine_can_be_drafted_with(tmp_path):
    """tests.fakes.FakeEngine honours cancel too (server tests run a drafter over it)."""
    graph = Graph(tmp_path / "primer.sqlite")
    gate = GpuGate("auto", lambda: None, lambda: None)
    pages = PageService(graph, FakeEngine(lambda m: Text.reply(Text.key(m))), FakeEngine(_fast),
                        "storyteller", gate)
    drafter = Drafter(graph, pages, gate, lambda: 1)
    try:
        parent = dict(list(pages.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
        node = graph.node_for("counterweight", "context", parent_id=parent)
        assert _until(lambda: drafter.ready(node.id, "storyteller"))
    finally:
        pages.shutdown()


def test_draft_preempted_before_the_server_answers(tmp_path):
    """llama-server can sit on the response headers while it reads the prompt: the draft must
    still step aside within the bound (the cancel is bound to the socket before the request)."""
    from primer.config import EngineSpec
    from primer.engines import make_engine
    from tests.fake_openai_server import serve_fake

    server = serve_fake(0, "# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones.")
    graph = Graph(tmp_path / "primer.sqlite")
    gate = GpuGate("auto", lambda: None, lambda: None)
    pages = PageService(graph, make_engine(EngineSpec("openai", server.url)), FakeEngine(_fast),
                        "storyteller", gate)
    setting = [0]
    drafter = Drafter(graph, pages, gate, lambda: setting[0])
    try:
        parent = dict(list(pages.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
        assert _until(lambda: graph.get_cards(parent, "counterweight") is not None)
        server.mode, setting[0] = "lateheaders", 1
        posts = server.posts
        drafter.offer(parent)
        assert _until(lambda: server.posts == posts + 1 and gate.state()["background"])
        time.sleep(0.1)  # the draft is now waiting for headers that don't come
        server.mode = ""
        t0, first = time.monotonic(), None
        for k, _ in pages.open(OpenRequest("Castle", "typed")):
            if k == "delta" and first is None:
                first = time.monotonic() - t0
        assert first is not None and first < BOUND
        node = graph.node_for("counterweight", "context", parent_id=parent)
        assert graph.versions(node.id) == []
    finally:
        server.unstall.set()
        pages.shutdown()
        server.shutdown()
        server.server_close()


def test_adopter_that_leaves_makes_the_draft_preemptible_again(env):
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    gen = env.pages.open(env.card(parent))
    for k, _ in gen:
        if k == "delta":
            break
    assert env.gate.state()["background"] is False
    gen.close()  # the reader went away mid-page
    assert env.gate.state()["background"] is True
    started = threading.Event()
    threading.Thread(target=lambda: env.gate.image().__enter__() and started.set(), daemon=True).start()
    assert started.wait(BOUND)  # pre-emptible again: the picture starts
    assert env.graph.versions(node.id) == [] and env.graph.drafts_for(node.id) == []
    assert DRAFT in env.text.closed


def test_draft_finished_as_preemption_lands_is_kept(env):
    """Pre-emption arriving after the last piece, before the stream ends, must not throw away
    a whole page."""
    pictured = threading.Event()

    def ask_for_the_gpu(aborted):
        threading.Thread(target=lambda: env.gate.image().__enter__() and pictured.set(),
                         daemon=True).start()
        aborted.wait(WAIT)  # the watchdog has reacted by the time the stream ends

    env.text.after[DRAFT] = ask_for_the_gpu
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert pictured.wait(WAIT)
    assert env.drafter.ready(node.id, "storyteller")
    st = env.drafter.status()
    assert (st["written"], st["discarded"]) == (1, 0)


def test_cancel_current_stops_the_draft_and_forgets_the_candidates(env):
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    node = env.ctx_node(parent)
    assert env.mid_draft()
    env.setting[0] = 0
    t0 = time.monotonic()
    env.drafter.cancel_current()
    assert _until(lambda: env.gate.state()["holder"] is None, BOUND) and time.monotonic() - t0 < BOUND
    assert env.graph.drafts_for(node.id) == [] and env.drafter.status()["drafting"] is None
    env.setting[0] = 1
    time.sleep(0.3)
    assert env.text.calls.count(DRAFT) == 1  # not retried: the candidates were forgotten


def test_new_page_forgets_the_old_pages_drafts(env):
    """Open page B while A's card is being drafted: A's draft is stopped and not started again,
    and B's offer drafts B's card next."""
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    env.text.texts["Siege"] = SIEGE
    parent = env.open_parent()
    a = env.ctx_node(parent)
    assert env.mid_draft()
    pending_at_write = None
    events = []
    for k, data in env.pages.open(OpenRequest("Siege", "typed")):
        events.append((k, data))
        if k == "delta" and pending_at_write is None:
            with env.drafter._cond:
                pending_at_write = list(env.drafter._pending)
    assert pending_at_write == []  # the reader moved on: A's waiting candidates are gone
    siege = dict(events)["node"]["id"]
    b = env.graph.node_for("counterweight", "context", parent_id=siege)
    assert _until(lambda: env.drafter.ready(b.id, "storyteller"))
    assert env.text.calls.count(DRAFT) == 1 and env.text.calls.count(SIEGE_DRAFT) == 1
    assert env.graph.drafts_for(a.id) == [] and env.graph.versions(a.id) == []


def test_offer_for_another_page_cancels_the_running_draft(env):
    env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    a = env.ctx_node(parent)
    assert env.mid_draft()
    env.drafter.offer(parent)  # the same page again: its draft is one of the candidates, kept
    time.sleep(0.2)
    assert DRAFT not in env.text.closed
    siege = env.graph.node_for("Siege", "concept")  # B, read without an open (cards already made)
    v = env.graph.add_version(siege.id, SIEGE, "storyteller", "normal", [])
    env.graph.set_highlights(siege.id, v.n, ["counterweight"])
    env.graph.set_cards(siege.id, "counterweight", parse_branches(CARDS, "counterweight", "Siege"))
    env.drafter.offer(siege.id)
    b = env.graph.node_for("counterweight", "context", parent_id=siege.id)
    assert _until(lambda: env.drafter.ready(b.id, "storyteller"))
    assert DRAFT in env.text.closed and env.text.calls.count(DRAFT) == 1
    assert env.graph.drafts_for(a.id) == []


def test_lowering_the_setting_cancels_a_draft_two_ahead(env):
    env.setting[0] = 2
    env.text.hold[GENERAL] = (2, threading.Event())  # stalled for good
    parent = env.open_parent()
    ctx = env.ctx_node(parent)
    assert _until(lambda: env.drafter.ready(ctx.id, "storyteller"))
    assert env.mid_draft(GENERAL)
    env.drafter.cancel_beyond(2)  # not lowered: nothing stops
    time.sleep(0.2)
    assert GENERAL not in env.text.closed
    env.setting[0] = 1
    t0 = time.monotonic()
    env.drafter.cancel_beyond(1)
    assert _until(lambda: GENERAL in env.text.closed and env.gate.state()["holder"] is None, BOUND)
    assert time.monotonic() - t0 < BOUND
    time.sleep(0.3)
    general = env.graph.node_for("counterweight", "concept")
    assert env.text.calls.count(GENERAL) == 1 and env.graph.drafts_for(general.id) == []
    assert env.drafter.ready(ctx.id, "storyteller")  # the one-ahead draft is kept


def test_adopter_closed_at_the_sources_event_gives_the_draft_back(tmp_path):
    env = Env(tmp_path, mode="web", grounder=_Grounder(_WEB))
    try:
        env.text.hold[DRAFT] = (2, threading.Event())  # stalled for good
        parent = env.open_parent()
        node = env.ctx_node(parent)
        assert env.mid_draft()
        gen = env.pages.open(env.card(parent))
        for k, _ in gen:
            if k == "sources":
                break
        assert env.gate.state()["background"] is False  # adopted
        gen.close()  # the reader went away before any text
        assert env.gate.state()["background"] is True
        started = threading.Event()
        threading.Thread(target=lambda: env.gate.image().__enter__() and started.set(), daemon=True).start()
        assert started.wait(BOUND)  # pre-emptible again: the picture starts
        assert env.graph.versions(node.id) == []
    finally:
        env.close()
