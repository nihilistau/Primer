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
    assert dict(ev)["waiting"] == {"reason": "paused for an image"}
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
