from concurrent.futures import ThreadPoolExecutor

import pytest

from primer.graph import Graph
from primer.pages import OpenRequest, PageService
from tests.fakes import FakeEngine

MD = "# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones over castle walls."


@pytest.fixture
def graph(tmp_path):
    return Graph(tmp_path / "primer.sqlite")


@pytest.fixture
def text_engine():
    return FakeEngine([MD])


@pytest.fixture
def fast_engine():
    return FakeEngine(['["counterweight"]'])


@pytest.fixture
def svc(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine)
    yield s
    s.shutdown()


def test_open_new_page_streams_saves_and_highlights(svc):
    ev = list(svc.open(OpenRequest("Trebuchet", "start")))
    kinds = [k for k, _ in ev]
    assert kinds[0] == "node" and "delta" in kinds and kinds[-2:] == ["done", "highlights"]
    assert "".join(d["text"] for k, d in ev if k == "delta") == MD
    assert ev[-1][1]["phrases"] == ["counterweight"] and ev[-2][1]["cached"] is False


def test_reopen_is_cached_and_not_regenerated(svc, text_engine):
    list(svc.open(OpenRequest("Trebuchet", "start")))
    n = len(text_engine.calls)
    ev = list(svc.open(OpenRequest("trebuchet", "typed")))
    assert dict(ev)["done"]["cached"] is True and len(text_engine.calls) == n
    assert dict(ev)["highlights"]["phrases"] == ["counterweight"]


def test_deeper_makes_new_version(svc, graph):
    nid = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    list(svc.open(OpenRequest("Trebuchet", "start", depth="deeper")))
    assert [v.n for v in graph.versions(nid)] == [1, 2] and graph.latest_version(nid).depth == "deeper"


def test_context_page_uses_parent_and_records_edge(svc, graph, text_engine):
    parent = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    node = dict(list(svc.open(OpenRequest("counterweight", "context", from_id=parent, phrase="counterweight"))))["node"]
    assert node["kind"] == "context" and node["parent_id"] == parent
    assert "Trebuchet" in str(text_engine.calls[-1])
    assert any(e["via"] == "context" and e["phrase"] == "counterweight" for e in graph.snapshot()["edges"])


def test_open_engine_fails_midstream_saves_nothing(graph):
    svc = PageService(graph, FakeEngine([MD], fail_after=2), FakeEngine(["[]"]))
    try:
        ev = list(svc.open(OpenRequest("Trebuchet", "start")))
    finally:
        svc.shutdown()
    assert ev[-1][0] == "error" and "done" not in [k for k, _ in ev]
    assert graph.latest_version(ev[0][1]["id"]) is None


def test_concurrent_open_single_version(svc, graph, text_engine):
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: list(svc.open(OpenRequest("Castle", "start"))), range(4)))
    nid = graph.node_for("Castle", "concept").id
    assert len(graph.versions(nid)) == 1 and len(text_engine.calls) == 1


def test_branches_prefetched_then_cached(svc, fast_engine):
    nid = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    svc.shutdown()  # drains the prefetch queue
    n = len(fast_engine.calls)
    assert [c.via for c in svc.branches(nid, "counterweight")][:2] == ["context", "general"]
    assert len(fast_engine.calls) == n  # served from cache


def test_shutdown_is_idempotent(svc):
    svc.shutdown()
    svc.shutdown()


def test_branches_computed_when_not_cached(svc):
    nid = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    cards = svc.branches(nid, "castle walls")
    assert [c.via for c in cards][:2] == ["context", "general"]


def test_deeper_on_context_page_keeps_node_and_adds_no_edge(svc, graph):
    parent = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    ctx = dict(list(svc.open(OpenRequest("counterweight", "context", from_id=parent))))["node"]["id"]
    edges = len(graph.snapshot()["edges"])
    again = dict(list(svc.open(OpenRequest("", "start", node_id=ctx, depth="deeper"))))
    assert again["node"]["id"] == ctx and again["done"]["n"] == 2 and len(graph.snapshot()["edges"]) == edges


def test_empty_typed_is_error(svc):
    assert list(svc.open(OpenRequest("   ", "typed"))) == [("error", {"message": "empty"})]


def test_punctuation_only_concept_is_error(svc):
    assert list(svc.open(OpenRequest("...", "start"))) == [("error", {"message": "empty"})]


def test_unknown_node_id_is_not_found(svc):
    assert list(svc.open(OpenRequest("", "start", node_id="nope"))) == [("error", {"message": "not found"})]


def test_fast_engine_error_gives_no_highlights_but_saves_page(graph, text_engine):
    svc = PageService(graph, text_engine, FakeEngine(["[]"], fail_after=0))
    try:
        ev = list(svc.open(OpenRequest("Trebuchet", "start")))
    finally:
        svc.shutdown()
    assert ev[-1] == ("highlights", {"phrases": []})
    assert graph.latest_version(ev[0][1]["id"]) is not None


def test_page_returns_latest_version_and_highlights(svc):
    nid = dict(list(svc.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    p = svc.page(nid)
    assert p["version"].markdown == MD and p["versions"] == [1] and p["highlights"] == ["counterweight"]
    assert svc.page("nope") is None


def test_text_engine_params_by_depth(svc, text_engine):
    seen = []
    orig = text_engine.stream

    def spy(messages, *, max_tokens, temperature):
        seen.append((max_tokens, temperature))
        return orig(messages, max_tokens=max_tokens, temperature=temperature)

    text_engine.stream = spy
    list(svc.open(OpenRequest("Trebuchet", "start")))
    list(svc.open(OpenRequest("Trebuchet", "start", depth="deeper")))
    list(svc.open(OpenRequest("Trebuchet", "start", depth="simpler")))
    assert seen == [(900, 0.7), (2000, 0.7), (500, 0.7)]


def test_node_event_carries_source_mode(svc):
    ev = dict(svc.open(OpenRequest("Trebuchet", "start")))
    assert ev["node"]["source_mode"] == "storyteller"


@pytest.mark.parametrize("mode", ["error", "close", "truncated"])
def test_half_page_from_real_http_engine_is_not_saved(graph, fake_server, mode):
    """The final review's midstream PoC: a model server that sends half a page and then an
    in-band error, or just hangs up, must end in an error event with nothing saved."""
    from primer.config import EngineSpec
    from primer.engines import make_engine

    fake_server.mode = mode
    s = PageService(graph, make_engine(EngineSpec("openai", fake_server.url)), FakeEngine(["[]"]))
    try:
        ev = list(s.open(OpenRequest("Castle", "start")))
    finally:
        s.shutdown()
    assert [e for e, _ in ev][-1] == "error" and "done" not in [e for e, _ in ev]
    assert graph.latest_version(ev[0][1]["id"]) is None


class _SlowFast(FakeEngine):
    def complete(self, messages, **kw):
        import time
        time.sleep(0.2)
        return super().complete(messages, **kw)


def test_shutdown_without_drain_drops_queued_prefetch(graph, text_engine):
    import time

    fast = _SlowFast(['["counterweight"]'])
    s = PageService(graph, text_engine, fast)
    nid = dict(list(s.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
    for i in range(40):  # 40 x 0.2 s of queued branch prefetch
        s._enqueue(nid, f"phrase {i}")
    t0 = time.monotonic()
    s.shutdown(drain=False)
    assert time.monotonic() - t0 < 1.5 and not s._worker.is_alive()
    assert len(fast.calls) < 6  # highlights, the in-flight item, maybe one more; not all 40
    s.shutdown()  # still idempotent


# GPU gate integration

def _hold_image(gate):
    """Hold gate.image() on a thread until the returned Event is set."""
    import threading

    held, release = threading.Event(), threading.Event()

    def run():
        with gate.image():
            held.set()
            release.wait(5)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert held.wait(2)
    return release, t


def _gated(graph, text_engine, fast_engine):
    from primer.gpu import GpuGate

    calls = []
    gate = GpuGate("auto", lambda: calls.append("pause"), lambda: calls.append("resume"))
    return PageService(graph, text_engine, fast_engine, gate=gate), gate, calls


def test_open_emits_waiting_event_when_gate_busy(graph, text_engine, fast_engine):
    import threading
    import time

    s, gate, calls = _gated(graph, text_engine, fast_engine)
    try:
        release, holder = _hold_image(gate)
        ev = []
        t = threading.Thread(target=lambda: ev.extend(s.open(OpenRequest("Trebuchet", "start"))), daemon=True)
        t.start()
        end = time.monotonic() + 2
        while gate.state()["waiting_pages"] != 1 and time.monotonic() < end:
            time.sleep(0.005)
        assert gate.state()["waiting_pages"] == 1 and text_engine.calls == []
        release.set()
        t.join(2)
        holder.join(2)
        assert not t.is_alive()
    finally:
        s.shutdown()
    kinds = [k for k, _ in ev]
    assert kinds[:2] == ["node", "waiting"] and kinds.count("waiting") == 1
    assert dict(ev)["waiting"] == {"reason": "paused for a picture or video"}
    assert kinds[-2:] == ["done", "highlights"]
    assert "".join(d["text"] for k, d in ev if k == "delta") == MD
    assert calls == ["pause", "resume"] and gate.state()["holder"] is None


def test_open_with_free_gate_emits_no_waiting(graph, text_engine, fast_engine):
    s, gate, calls = _gated(graph, text_engine, fast_engine)
    try:
        kinds = [k for k, _ in s.open(OpenRequest("Trebuchet", "start"))]
    finally:
        s.shutdown()
    assert "waiting" not in kinds and kinds[-2:] == ["done", "highlights"] and calls == []


def test_open_releases_gate_when_engine_fails(graph):
    from primer.gpu import GpuGate

    gate = GpuGate("auto", lambda: None, lambda: None)
    s = PageService(graph, FakeEngine([MD], fail_after=2), FakeEngine(["[]"]), gate=gate)
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "start")))
    finally:
        s.shutdown()
    assert ev[-1][0] == "error" and gate.state()["holder"] is None


def test_cached_open_skips_gate(graph, text_engine, fast_engine):
    import threading

    s, gate, calls = _gated(graph, text_engine, fast_engine)
    try:
        list(s.open(OpenRequest("Trebuchet", "start")))
        release, holder = _hold_image(gate)
        ev = []
        t = threading.Thread(target=lambda: ev.extend(s.open(OpenRequest("Trebuchet", "start"))), daemon=True)
        t.start()
        t.join(2)
        assert not t.is_alive()  # did not wait behind the image
        release.set()
        holder.join(2)
    finally:
        s.shutdown()
    kinds = [k for k, _ in ev]
    assert "waiting" not in kinds and dict(ev)["done"]["cached"] is True
    assert calls == ["pause"]


def test_page_priority_holds_across_waiting_yield(graph, fast_engine):
    """A page suspended at its waiting yield is already queued: an image that arrives after
    the running image ends still goes after the page."""
    import threading
    import time

    from primer.gpu import GpuGate

    log = []
    gate = GpuGate("auto", lambda: log.append("pause"), lambda: log.append("resume"))
    text = FakeEngine(lambda m: (log.append("page"), MD)[1])
    s = PageService(graph, text, fast_engine, gate=gate)
    try:
        with gate.image():
            log.append("image1")
            g = s.open(OpenRequest("Trebuchet", "start"))
            assert [next(g)[0], next(g)[0]] == ["node", "waiting"]
            assert gate.state()["waiting_pages"] == 1
        started = threading.Event()

        def image2():
            with gate.image():
                log.append("image2")
                started.set()

        t = threading.Thread(target=image2, daemon=True)
        t.start()
        end = time.monotonic() + 2
        while gate.state()["waiting_images"] != 1 and time.monotonic() < end:
            time.sleep(0.005)
        assert gate.state()["waiting_images"] == 1 and not started.is_set()
        kinds = [k for k, _ in g]
        t.join(2)
        assert not t.is_alive()
    finally:
        s.shutdown()
    assert kinds[-2:] == ["done", "highlights"]
    assert log == ["image1", "pause", "resume", "page", "pause", "image2"] or \
        log == ["pause", "image1", "resume", "page", "pause", "image2"]


def test_closing_generator_midstream_releases_gate(graph, text_engine, fast_engine):
    s, gate, calls = _gated(graph, text_engine, fast_engine)
    try:
        g = s.open(OpenRequest("Trebuchet", "start"))
        while next(g)[0] != "delta":
            pass
        assert gate.state()["holder"] == "page"
        g.close()
        assert gate.state()["holder"] is None
        with gate.image():  # free for an image straight away
            pass
    finally:
        s.shutdown()


def test_closing_generator_at_waiting_yield_unqueues(graph, text_engine, fast_engine):
    s, gate, calls = _gated(graph, text_engine, fast_engine)
    try:
        with gate.image():
            g = s.open(OpenRequest("Trebuchet", "start"))
            assert [next(g)[0], next(g)[0]] == ["node", "waiting"]
            g.close()
            st = gate.state()
            assert st["waiting_pages"] == 0 and st["waiting_images"] == 0
        assert gate.state()["holder"] is None and text_engine.calls == []
        list(s.open(OpenRequest("Trebuchet", "start")))  # node lock was released too
    finally:
        s.shutdown()


# grounding

from primer.grounding import Source


class _FakeGrounder:
    def __init__(self, sources=(), note="", log=None):
        self.sources, self.note, self.log, self.calls = list(sources), note, log, []

    def gather(self, mode, query, status):
        self.calls.append((mode, query))
        if self.log is not None:
            self.log.append("gather")
        status("Searching the web…")
        return self.sources, self.note


_WEB = [Source(1, "Trebuchet facts", "web", "A trebuchet is a siege engine.", url="https://example.com/t",
               where="example.com")]


def test_open_grounded_emits_grounding_sources_done(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine, grounder=_FakeGrounder(_WEB))
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "start", source="web")))
    finally:
        s.shutdown()
    kinds = [k for k, _ in ev]
    assert kinds[:3] == ["node", "grounding", "sources"] and kinds.index("sources") < kinds.index("delta")
    assert dict(ev)["node"]["source_mode"] == "web"
    assert dict(ev)["sources"] == {"sources": [
        {"n": 1, "title": "Trebuchet facts", "origin": "web", "where": "example.com"}]}
    assert dict(ev)["done"]["source_mode"] == "web" and dict(ev)["done"]["note"] == ""
    assert "A trebuchet is a siege engine." in text_engine.calls[0][1]["content"]
    nid = dict(ev)["node"]["id"]
    assert graph.latest_version(nid).source_mode == "web"
    assert graph.sources(nid, 1)[0].url == "https://example.com/t"


def test_grounding_happens_before_gpu_turn(graph, text_engine, fast_engine):
    order = []

    class _RecordingGate:
        def page(self):
            order.append("turn")
            return self

        def try_enter(self):
            return True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    s = PageService(graph, text_engine, fast_engine, gate=_RecordingGate(),
                    grounder=_FakeGrounder(_WEB, log=order))
    try:
        list(s.open(OpenRequest("Trebuchet", "start", source="web")))
    finally:
        s.shutdown()
    assert order == ["gather", "turn"]


def test_open_cached_page_ignores_default_mode(graph, text_engine, fast_engine):
    g = _FakeGrounder(_WEB)
    s = PageService(graph, text_engine, fast_engine, grounder=g)
    try:
        list(s.open(OpenRequest("Trebuchet", "start")))
        s.source_mode = "web"
        ev = list(s.open(OpenRequest("Trebuchet", "start")))
    finally:
        s.shutdown()
    assert dict(ev)["node"]["source_mode"] == "storyteller"
    assert dict(ev)["done"]["source_mode"] == "storyteller"
    assert dict(ev)["done"]["cached"] is True and g.calls == []


def test_rewrite_from_web_adds_version_with_sources(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine, grounder=_FakeGrounder(_WEB))
    try:
        nid = dict(list(s.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
        ev = list(s.open(OpenRequest("Trebuchet", "start", regenerate=True, source="web")))
    finally:
        s.shutdown()
    assert dict(ev)["node"]["source_mode"] == "web"
    assert [v.source_mode for v in graph.versions(nid)] == ["storyteller", "web"]
    assert graph.sources(nid, 1) == [] and len(graph.sources(nid, 2)) == 1


def test_fallback_records_storyteller_and_note(graph, text_engine, fast_engine):
    note = "No usable web page found for \"Trebuchet\"; written from the model's own knowledge."
    s = PageService(graph, text_engine, fast_engine, grounder=_FakeGrounder([], note))
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "start", source="web")))
    finally:
        s.shutdown()
    kinds = [k for k, _ in ev]
    assert "sources" not in kinds and dict(ev)["done"]["note"] == note
    assert dict(ev)["done"]["source_mode"] == "storyteller"
    nid = dict(ev)["node"]["id"]
    assert graph.latest_version(nid).source_mode == "storyteller" and graph.sources(nid, 1) == []
    assert "Sources" not in text_engine.calls[0][1]["content"]


def test_grounded_mode_without_grounder_falls_back(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine, source_mode="reference")
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "start")))
    finally:
        s.shutdown()
    assert dict(ev)["done"]["source_mode"] == "storyteller"
    assert "isn't set up" in dict(ev)["done"]["note"]


def test_closing_generator_at_grounding_yield_releases_node_lock(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine, grounder=_FakeGrounder(_WEB))
    try:
        gen = s.open(OpenRequest("Trebuchet", "start", source="web"))
        assert next(gen)[0] == "node" and next(gen)[0] == "grounding"
        gen.close()
        nid = graph.node_for("Trebuchet", "concept").id
        lock = s._lock_for(nid)
        assert lock.acquire(timeout=1)
        lock.release()
        assert graph.latest_version(nid) is None
        ev = list(s.open(OpenRequest("Trebuchet", "start", source="web")))
        assert dict(ev)["done"]["source_mode"] == "web"
    finally:
        s.shutdown()


def test_unknown_source_is_storyteller_without_note(graph, text_engine, fast_engine):
    g = _FakeGrounder(_WEB)
    s = PageService(graph, text_engine, fast_engine, grounder=g)
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "start", source="telepathy")))
    finally:
        s.shutdown()
    assert dict(ev)["node"]["source_mode"] == "storyteller" and g.calls == []
    assert dict(ev)["done"]["source_mode"] == "storyteller" and dict(ev)["done"]["note"] == ""


def test_deeper_without_source_uses_default_mode(graph, text_engine, fast_engine):
    g = _FakeGrounder(_WEB)
    s = PageService(graph, text_engine, fast_engine, grounder=g)
    try:
        list(s.open(OpenRequest("Trebuchet", "start")))
        s.source_mode = "web"
        ev = list(s.open(OpenRequest("Trebuchet", "start", depth="deeper")))
    finally:
        s.shutdown()
    assert dict(ev)["done"]["source_mode"] == "web" and dict(ev)["node"]["source_mode"] == "web"
    assert g.calls == [("web", "Trebuchet")]


def test_source_url_regenerates_a_version_not_grounded_in_it(graph, text_engine, fast_engine):
    class UrlGrounder(_FakeGrounder):
        def gather_url(self, u):
            self.calls.append(("url", u))
            return self.sources, self.note

    g = UrlGrounder(_WEB)
    s = PageService(graph, text_engine, fast_engine, grounder=g)
    try:
        list(s.open(OpenRequest("Trebuchet", "typed")))  # storyteller version exists
        n = len(text_engine.calls)
        ev = list(s.open(OpenRequest("Trebuchet", "typed", source_url="https://example.com/t")))
        assert dict(ev)["done"]["cached"] is False and len(text_engine.calls) == n + 1
        again = list(s.open(OpenRequest("Trebuchet", "typed", source_url="https://example.com/t")))
        assert dict(again)["done"]["cached"] is True and len(text_engine.calls) == n + 1
        other = list(s.open(OpenRequest("Trebuchet", "typed", source_url="https://example.com/other")))
        assert dict(other)["done"]["cached"] is False
    finally:
        s.shutdown()


def test_open_from_source_url_uses_only_that_page(graph, text_engine, fast_engine):
    class UrlGrounder(_FakeGrounder):
        def gather_url(self, u):
            self.calls.append(("url", u))
            return self.sources, self.note

    g = UrlGrounder(_WEB)
    s = PageService(graph, text_engine, fast_engine, grounder=g)
    try:
        ev = list(s.open(OpenRequest("Trebuchet", "typed", source_url="https://example.com/t")))
        ref = list(s.open(OpenRequest("Whale", "typed", source_url="kiwix:wiki/Whale")))
    finally:
        s.shutdown()
    assert g.calls == [("url", "https://example.com/t"), ("url", "kiwix:wiki/Whale")]  # never gather()
    assert dict(ev)["done"]["source_mode"] == "web" and dict(ref)["node"]["source_mode"] == "reference"
    assert dict(ev)["sources"]["sources"][0]["title"] == "Trebuchet facts"
    assert "A trebuchet is a siege engine." in text_engine.calls[0][1]["content"]


def test_pages_record_signals(graph, text_engine, fast_engine):
    from primer.reader.signals import Signals
    sig = Signals(graph)
    s = PageService(graph, text_engine, fast_engine, signals=sig)
    try:
        nid = dict(list(s.open(OpenRequest("Trebuchet", "start"))))["node"]["id"]
        list(s.open(OpenRequest("trebuchet", "typed")))  # cached: still an open
        list(s.open(OpenRequest("counterweight", "sideways", from_id=nid, phrase="counterweight")))
        list(s.open(OpenRequest("", "start", node_id=nid, depth="simpler")))
        list(s.open(OpenRequest("", "start", node_id=nid, depth="deeper")))
        list(s.open(OpenRequest("", "start", node_id=nid, regenerate=True)))
        list(s.open(OpenRequest("", "typed")))  # never reaches a node: nothing recorded
        list(s.open(OpenRequest("", "start", node_id="missing")))
    finally:
        s.shutdown()
    c = sig.counted()
    assert c["opens"] == 3 and c["level"] == {"deeper": 1, "simpler": 1, "lean": "even"}
    assert c["style"]["sideways"] == 1.0 and sig.since("regenerate", 0) == 1
    assert {t["title"]: t["opens"] for t in c["topics"]} == {"Trebuchet": 2, "counterweight": 1}


def test_reader_block_reaches_page_prompt_and_style_reaches_cards(graph, text_engine, fast_engine):
    s = PageService(graph, text_engine, fast_engine, reader_block=lambda: "Likes <b>plain</b> words.")
    try:
        list(s.open(OpenRequest("Trebuchet", "start")))
        s.branches(graph.node_for("Trebuchet", "concept").id, "castle walls")
    finally:
        s.shutdown()
    system, user = text_engine.calls[0][0]["content"], text_engine.calls[0][1]["content"]
    assert "plain" not in system and "<reader>\nLikes plain words.\n</reader>" in user


def test_counted_block_is_the_default_reader_block(graph, text_engine, fast_engine):
    from primer.reader.signals import Signals
    sig = Signals(graph)
    s = PageService(graph, text_engine, fast_engine, signals=sig)
    try:
        for c in ("Castles", "Moats", "Keeps"):
            nid = graph.node_for(c, "concept").id
            for _ in range(2):
                sig.record("open", nid, "sideways")
        list(s.open(OpenRequest("Trebuchet", "start")))
        nid = graph.node_for("Trebuchet", "concept").id
        s.branches(nid, "castle walls")
    finally:
        s.shutdown()
    assert "Follows sideways jumps most" in text_engine.calls[0][1]["content"]
    assert any("This reader tends to follow sideways jumps." in str(m) for m in fast_engine.calls)


def test_fast_busy_counts_fast_calls(graph, text_engine):
    import threading
    started, release = threading.Event(), threading.Event()

    class Slow(FakeEngine):
        def complete(self, messages, **kw):
            started.set()
            release.wait(5)
            return "[]"

    s = PageService(graph, text_engine, Slow([]))
    try:
        assert s.fast_busy() is False
        t = threading.Thread(target=lambda: list(s.open(OpenRequest("Trebuchet", "start"))))
        t.start()
        assert started.wait(5)
        assert s.fast_busy() is True
        release.set()
        t.join(5)
        assert s.fast_busy() is False
    finally:
        release.set()
        s.shutdown()


def test_fast_busy_while_prefetch_is_queued(graph, text_engine):
    import threading
    started, release = threading.Event(), threading.Event()

    class Slow(FakeEngine):
        def complete(self, messages, **kw):
            started.set()
            release.wait(5)
            return "[]"

    s = PageService(graph, text_engine, Slow([]))
    try:
        nid = graph.node_for("Trebuchet", "concept").id
        graph.add_version(nid, "# Trebuchet\nA counterweight.", "storyteller", "normal", [])
        s._enqueue(nid, "counterweight")
        s._enqueue(nid, "stone")
        assert started.wait(5)
        s._fast_calls = 0  # even between fast calls, queued prefetch means busy
        assert s.fast_busy() is True
        s._fast_calls = 1
    finally:
        release.set()
        s.shutdown()
    assert s.fast_busy() is False
