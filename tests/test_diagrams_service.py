"""Diagram jobs: drawn by the writing model at a page turn, stored per page version, restartable."""
import threading
import time
from contextlib import contextmanager

import pytest

from primer.diagrams.intent import match_diagram_request
from primer.diagrams.service import SHUT_DOWN, DiagramService
from tests.fakes import FakeEngine

PAGE = "# Trebuchet\nA trebuchet throws stones with a swinging arm, a pivot and a counterweight."
DRAWING = ('<svg viewBox="0 0 1000 700"><defs><marker id="arrow"/></defs>'
           '<line x1="100" y1="600" x2="800" y2="100" stroke="black" marker-end="url(#arrow)"/>'
           '<text x="100" y="620">Counterweight</text><text x="450" y="350">Pivot</text>'
           '<text x="800" y="90">Sling</text><script>alert(1)</script></svg>\n'
           "<caption>The falling counterweight swings the arm and the sling throws the stone.</caption>")
NO_LABELS = '<svg><rect width="5" height="5"/><text>One</text></svg><caption>x</caption>'


class FakeGate:
    """Records page turns; `inside` is true while one is held."""

    def __init__(self):
        self.turns = []
        self.inside = False

    @contextmanager
    def page(self):
        self.turns.append("page")
        self.inside = True
        try:
            yield
        finally:
            self.inside = False


@pytest.fixture
def node(g):
    n = g.node_for("Trebuchet", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    return n


def wait(svc, did, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        d = svc.get(did)
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(0.01)
    raise AssertionError("diagram job did not finish")


def make(g, engine, gate=None):
    return DiagramService(g, lambda: engine, lambda: gate)


def test_diagram_draws_and_stores(g, node):
    engine = FakeEngine([DRAWING])
    svc = make(g, engine)
    try:
        did = svc.create(node.id)
        assert svc.get(did)["status"] in ("queued", "drawing", "done")
        d = wait(svc, did)
    finally:
        svc.shutdown()
    assert d["status"] == "done" and d["error"] is None and d["phrase"] == "" and d["n"] == 1
    assert d["labels"] == ["Counterweight", "Pivot", "Sling"]
    assert d["caption"].startswith("The falling counterweight")
    assert "<script" not in d["svg"] and d["svg"].startswith("<svg")
    assert f'id="dg{did[:6]}-arrow"' in d["svg"]  # ids are this diagram's own
    assert [x["id"] for x in g.diagrams_for(node.id)] == [did]
    listed = g.diagrams_for(node.id)[0]
    assert listed["status"] == "done" and "svg" not in listed  # the page payload stays small
    sent = engine.calls[0]
    assert "Trebuchet" in sent[1]["content"]


def test_diagram_takes_a_page_turn(g, node):
    gate = FakeGate()
    seen = []

    def reply(messages):
        seen.append(gate.inside)
        return DRAWING
    svc = make(g, FakeEngine(reply), gate)
    try:
        d = wait(svc, svc.create(node.id, "the sling"))
    finally:
        svc.shutdown()
    assert d["status"] == "done" and d["phrase"] == "the sling"
    assert gate.turns == ["page"] and seen == [True]  # the model ran inside a page turn


def test_diagram_reads_engine_and_gate_at_call_time(g, node):
    holder = {"engine": None, "gate": None}
    svc = DiagramService(g, lambda: holder["engine"], lambda: holder["gate"])
    try:
        d = wait(svc, svc.create(node.id))
        assert d["status"] == "failed" and d["error"] == "no writing model in this tier"
        holder["engine"], holder["gate"] = FakeEngine([DRAWING]), FakeGate()  # a tier switch
        d = wait(svc, svc.again(d["id"]))
        assert d["status"] == "done" and holder["gate"].turns == ["page"]
    finally:
        svc.shutdown()


def test_diagram_retries_once(g, node):
    engine = FakeEngine(["no drawing here", DRAWING])
    svc = make(g, engine)
    try:
        d = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert d["status"] == "done" and len(engine.calls) == 2


def test_diagram_refuses_without_labels(g, node):
    engine = FakeEngine([NO_LABELS])
    svc = make(g, engine)
    try:
        d = wait(svc, svc.create(node.id))
        assert d["status"] == "failed" and d["error"] == "the drawing has too few labels"
        assert len(engine.calls) == 2 and d["svg"] == ""  # one retry, nothing kept
        none = FakeEngine(["NONE"])
        svc2 = make(g, none)
        try:
            d = wait(svc2, svc2.create(node.id))
        finally:
            svc2.shutdown()
        assert d["status"] == "failed" and d["error"] == "nothing on this page to diagram"
        assert len(none.calls) == 1  # no retry when the model says there is nothing to draw
    finally:
        svc.shutdown()


def test_diagram_engine_error_fails_with_reason(g, node):
    svc = make(g, FakeEngine([DRAWING], fail_after=0))
    try:
        d = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert d["status"] == "failed" and "fake engine failure" in d["error"]


def test_diagram_create_validates(g, node):
    svc = make(g, FakeEngine([DRAWING]))
    try:
        with pytest.raises(ValueError):
            svc.create("nope")
        empty = g.node_for("Empty", "concept")
        with pytest.raises(ValueError):
            svc.create(empty.id)
        with pytest.raises(ValueError):
            svc.again("nope")
    finally:
        svc.shutdown()
    with pytest.raises(ValueError, match=SHUT_DOWN):
        svc.create(node.id)


def test_diagram_restart(g, node):
    """Unfinished diagrams fail on start; Again draws a fresh one (Review Focus 4)."""
    queued = g.add_diagram(node.id, 1, "")
    drawing = g.add_diagram(node.id, 1, "the sling")
    g.update_diagram(drawing, status="drawing")
    done = g.add_diagram(node.id, 1, "")
    g.update_diagram(done, status="done", svg="<svg/>", caption="c", labels=["a", "b", "c"])
    assert g.fail_unfinished_diagrams("Primer restarted") == 2
    for did in (queued, drawing):
        d = g.get_diagram(did)
        assert d["status"] == "failed" and d["error"] == "Primer restarted"
    assert g.get_diagram(done)["status"] == "done"
    g.update_diagram(queued, status="done")  # a late writer cannot revive a failed diagram
    assert g.get_diagram(queued)["status"] == "failed"
    svc = make(g, FakeEngine([DRAWING]))
    try:
        new = wait(svc, svc.again(drawing))
    finally:
        svc.shutdown()
    assert new["id"] != drawing and new["status"] == "done" and new["phrase"] == "the sling"


def test_diagram_shutdown_fails_unfinished(g, node):
    release = threading.Event()
    started = threading.Event()

    def slow(messages):
        started.set()
        release.wait(5)
        return DRAWING
    svc = make(g, FakeEngine(slow))
    did = svc.create(node.id)
    second = svc.create(node.id)
    assert started.wait(5)
    t = threading.Thread(target=svc.shutdown)
    t.start()
    time.sleep(0.1)
    release.set()
    t.join(5)
    assert g.get_diagram(did)["status"] == "failed" and g.get_diagram(did)["error"] == SHUT_DOWN
    assert g.get_diagram(second)["status"] == "failed"


@pytest.mark.parametrize("text,subject", [
    ("diagram of a trebuchet", "a trebuchet"),
    ("Draw a diagram of the water clock", "the water clock"),
    ("draw me a diagram of a siphon, please", "a siphon"),
    ("show me a diagram of how a trebuchet works", "a trebuchet"),
    ("a diagram of how siphons work", "siphons"),
    ("can you make a diagram of the heart?", None),
    ("make a diagram of the heart", "the heart"),
    ("trebuchet diagram", None),
    ("water clock diagram.", None),
    ("Venn diagram", None),
    ("Feynman diagram", None),
    ("Hertzsprung-Russell diagram", None),
    ("I need a diagram", None),
    ("explain the Feynman diagram", None),
    ("tell me about the Venn diagram", None),
    ("the history of the Venn diagram", None),
    ("draw a trebuchet diagram", "a trebuchet"),
    ("make me a water clock diagram", "a water clock"),
    ("a diagram showing how a siphon works", "a siphon"),
    ("diagram of how does a trebuchet work", "a trebuchet"),
    ("draw a diagram", None),
    ("how does a trebuchet work", None),
    ("how does a trebuchet work?", None),
    ("diagram", None),
    ("diagram of it", None),
    ("what is a diagram of a trebuchet", None),
    ("Venn diagrams are useful", None),
    ("a trebuchet", None),
    ("a picture of a circuit diagram", None),
    ("show me the trebuchet diagram", "the trebuchet"),
    ("", None),
])
def test_diagram_intent(text, subject):
    assert match_diagram_request(text) == subject


# --- routes ---

def test_diagram_routes(client, app_parts, g):
    app, _ = app_parts
    node = g.node_for("Trebuchet", "concept")
    g.add_version(node.id, PAGE, "storyteller", "normal")
    app.diagrams.shutdown()
    gate = FakeGate()
    engine = {"text": FakeEngine([DRAWING])}
    app.diagrams = DiagramService(g, lambda: engine["text"], lambda: gate)
    try:
        assert client.get("/v1/status")["diagram_available"] == {"ok": True, "reason": ""}
        assert client.get(f"/v1/pages/{node.id}")["diagrams"] == []
        r = client.post("/v1/diagrams", {"node_id": node.id, "phrase": "the sling"}, expect=202)
        end = time.time() + 5
        while (d := client.get(f"/v1/diagrams/{r['id']}"))["status"] not in ("done", "failed"):
            assert time.time() < end
            time.sleep(0.02)
        assert d["status"] == "done" and d["labels"] == ["Counterweight", "Pivot", "Sling"]
        assert d["svg"].startswith("<svg") and d["caption"] and d["phrase"] == "the sling"
        listed = client.get(f"/v1/pages/{node.id}")["diagrams"]
        assert [x["id"] for x in listed] == [r["id"]] and "svg" not in listed[0]
        again = client.post(f"/v1/diagrams/{r['id']}/again", {}, expect=202)
        assert again["id"] != r["id"]
        client.get("/v1/diagrams/nope", expect=404)
        client.post("/v1/diagrams/nope/again", {}, expect=404)
        client.post("/v1/diagrams", {"node_id": "nope"}, expect=404)
        client.post("/v1/diagrams", {}, expect=400)
        client.post("/v1/diagrams", {"node_id": 5}, expect=400)
        client.post("/v1/diagrams", {"node_id": node.id, "phrase": 5}, expect=400)
        empty = g.node_for("Empty", "concept")
        client.post("/v1/diagrams", {"node_id": empty.id}, expect=404)
        intent = client.post("/v1/intent", {"text": "draw a diagram of a trebuchet"})
        assert intent["diagram"] == "a trebuchet" and intent["image"] is None and intent["map"] is None
        plain = client.post("/v1/intent", {"text": "how does a trebuchet work"})
        assert "diagram" in plain and plain["diagram"] is None
        for text in ("a picture of a castle", "map of Wales", "tell me a story about Rome"):
            assert client.post("/v1/intent", {"text": text})["diagram"] is None
        # no writing model: refused with the reason, nothing created
        engine["text"] = None
        status = client.get("/v1/status")["diagram_available"]
        assert status == {"ok": False, "reason": "no writing model in this tier"}
        count = len(g.diagrams_for(node.id))
        assert client.post("/v1/diagrams", {"node_id": node.id}, expect=503)["error"] == status["reason"]
        assert client.post(f"/v1/diagrams/{r['id']}/again", {}, expect=503)["error"]
        assert len(g.diagrams_for(node.id)) == count
        # final review, item 6: a diagram phrase routes as before (a picture)
        got = client.post("/v1/intent", {"text": "draw a diagram of a trebuchet"})
        assert got["diagram"] is None and got["image"] == "a diagram of a trebuchet"
    finally:
        app.diagrams.shutdown()


def test_app_diagrams_follow_the_pages(app_parts):
    """The App's diagram service reads the writing engine and gate through the pages, so a
    tier switch (a new PageService) needs no new service."""
    app, _ = app_parts
    try:
        assert app.diagram_available() == {"ok": True, "reason": ""}
        assert app.diagrams.gate_getter() is app.pages.gate
        text, app.pages.text = app.pages.text, None
        assert app.diagram_available() == {"ok": False, "reason": "no writing model in this tier"}
        app.pages.text = text
    finally:
        app.shutdown_diagrams()
    assert app.diagrams._closed


def test_serve_marks_unfinished_diagrams_failed_on_start(tmp_path):
    import serve
    from primer.config import TIERS_DIR
    from primer.graph import Graph
    from primer.hardware import Hardware

    var = tmp_path / "var"
    g = Graph(var / "primer.sqlite")
    n = g.node_for("Trebuchet", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    did = g.add_diagram(n.id, 1, "")
    g.update_diagram(did, status="drawing")
    app = serve.build_app(serve.load_tier("light"), var, tmp_path / "ui", TIERS_DIR,
                          hardware=Hardware(None, 0.0, 16.0))
    try:
        d = app.graph.get_diagram(did)
        assert d["status"] == "failed" and d["error"] == "Primer restarted"
        assert app.diagrams.gate_getter() is app.gate
    finally:
        app.shutdown_diagrams()
        app.shutdown_maps()
        app.shutdown_stories()
        app.images.shutdown()
        app.pages.shutdown(drain=False)


def test_diagram_shutdown_while_waiting_for_the_page_turn(g, node):
    """Final review, item 4: shut down while the job waits on the gate; it ends with the
    shutdown error and the writing model is never called."""
    from tests.test_timelines_service import HeldGate, _shut_down_in_background
    gate = HeldGate()
    engine = FakeEngine([DRAWING])
    svc = make(g, engine, gate)
    did = svc.create(node.id)
    assert gate.waiting.wait(5)
    shut = _shut_down_in_background(svc)
    gate.lock.release()
    shut.join(5)
    d = g.get_diagram(did)
    assert d["status"] == "failed" and d["error"] == SHUT_DOWN
    assert engine.calls == []


def test_diagram_engine_error_after_shutdown_keeps_the_shutdown_error(g, node):
    holder = {}

    class Raising:
        def complete(self, *a, **kw):
            holder["shut"] = _shut(holder["svc"])
            raise RuntimeError("engine stopped")

        def health(self):
            return True
    from tests.test_timelines_service import _shut_down_in_background as _shut
    svc = make(g, Raising())
    holder["svc"] = svc
    did = svc.create(node.id)
    end = time.time() + 5
    while "shut" not in holder and time.time() < end:
        time.sleep(0.005)
    holder["shut"].join(5)
    d = g.get_diagram(did)
    assert d["status"] == "failed" and d["error"] == SHUT_DOWN
