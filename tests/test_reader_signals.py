import pytest

from primer.reader.signals import Signals, counted_block, style_hint


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def sig(g):
    return Signals(g, clock=Clock())


def _nodes(g, *titles):
    return [g.node_for(t, "concept").id for t in titles]


def test_signals_count_level_and_style(g, sig):
    castle, siege, moat = _nodes(g, "Castles", "Siege engines", "Moat")
    sig.record("open", castle, "start")
    for _ in range(3):
        sig.record("open", siege, "sideways")
    sig.record("open", moat, "context")
    sig.record("open", castle, "general")
    sig.record("depth", castle, "simpler")
    sig.record("depth", siege, "simpler")
    sig.record("depth", moat, "deeper")
    sig.record("regenerate", moat)
    c = sig.counted()
    assert c["opens"] == 6
    assert c["level"] == {"deeper": 1, "simpler": 2, "lean": "simpler"}
    style = c["style"]
    assert style["sideways"] == pytest.approx(0.6) and style["context"] == pytest.approx(0.2)
    assert style["general"] == pytest.approx(0.2) and style["branches"] == 5
    assert style["lean"] == "even"  # 5 branch opens: too few to call
    assert [t["title"] for t in c["topics"]][:2] == ["Siege engines", "Castles"]
    assert c["topics"][0] == {"title": "Siege engines", "opens": 3, "seconds": 0.0}
    sig.record("open", siege, "sideways")
    c = sig.counted()
    assert c["style"]["lean"] == "sideways" and c["style"]["branches"] == 6
    assert style_hint(c) == "This reader tends to follow sideways jumps."
    sig.record("depth", castle, "deeper")
    assert sig.counted()["level"]["lean"] == "even"


def test_style_lean_needs_half(g, sig):
    a, b, c_ = _nodes(g, "A thing", "B thing", "C thing")
    for node, via in [(a, "context")] * 2 + [(b, "general")] * 2 + [(c_, "sideways")] * 2:
        sig.record("open", node, via)
    c = sig.counted()
    assert c["style"]["lean"] == "even" and style_hint(c) == ""


def test_empty_counted(sig):
    c = sig.counted()
    assert c == {"opens": 0, "level": {"deeper": 0, "simpler": 0, "lean": "even"},
                 "style": {"context": 0.0, "general": 0.0, "sideways": 0.0, "branches": 0, "lean": "even"},
                 "topics": []}
    assert counted_block(c) == "" and style_hint(c) == ""


def test_counted_block_sentences(g, sig):
    castle, siege = _nodes(g, "Castles", "Siege engines")
    sig.record("open", castle, "start")
    sig.record("open", siege, "typed")
    assert counted_block(sig.counted()) == ""  # under 3 opens: too little to say
    for _ in range(6):
        sig.record("open", siege, "sideways")
    sig.record("depth", castle, "simpler")
    sig.record("depth", siege, "simpler")
    block = counted_block(sig.counted())
    assert "Asks for simpler pages more than deeper ones." in block
    assert "Follows sideways jumps most (100%)." in block
    assert "Reads most about: Siege engines, Castles." in block


def test_topics_top_eight_and_dwell(g, sig):
    ids = _nodes(g, *[f"Topic {i}" for i in range(10)])
    for i, nid in enumerate(ids):
        for _ in range(i + 1):
            sig.record("open", nid, "start")
    sig.dwell(ids[0], 30)
    topics = sig.counted()["topics"]
    assert len(topics) == 8 and topics[0]["title"] == "Topic 9"
    sig.dwell(ids[1], 5)
    assert all(t["title"] not in ("Topic 0", "Topic 1") for t in sig.counted()["topics"])


def test_dwell_capped_per_visit(g, sig):
    a, b = _nodes(g, "Alpha", "Beta")
    assert sig.dwell(a, 300) == 300
    assert sig.dwell(a, 250) == 250
    assert sig.dwell(a, 100) == 50  # 600 per visit
    assert sig.dwell(a, 30) == 0
    assert sig.dwell(b, 20) == 20  # another node's report ends the visit
    assert sig.dwell(a, 30) == 30  # a new visit
    assert sig.dwell("no-such-node", 30) == 0  # unknown ids are ignored
    assert sig.dwell(a, 30) == 30  # ...and do not end the visit
    seconds = {t["title"]: t["seconds"] for t in sig.counted()["topics"]}
    assert seconds == {"Alpha": 660.0, "Beta": 20.0}


def test_since_counts_kind_after_time(g):
    clock = Clock(100.0)
    sig = Signals(g, clock=clock)
    (a,) = _nodes(g, "Alpha")
    sig.record("open", a, "start")
    clock.t = 200.0
    sig.record("open", a, "start")
    sig.record("answer", a, "right")
    assert sig.since("open", 150.0) == 1 and sig.since("open", 0) == 2
    assert sig.since("answer", 150.0) == 1 and sig.since("depth", 0) == 0


def test_record_rejects_unknown_kind(sig):
    with pytest.raises(ValueError):
        sig.record("frobnicate")


def test_dwell_route(client, app_parts):
    app, _ = app_parts
    nid = app.graph.node_for("Trebuchet", "concept").id
    assert client.post("/v1/reader/dwell", {"node_id": nid, "seconds": 30}, expect=204) is None
    assert client.post("/v1/reader/dwell", {"node_id": nid, "seconds": 12.5}, expect=204) is None
    for bad in ({}, {"node_id": nid}, {"node_id": nid, "seconds": 0}, {"node_id": nid, "seconds": -1},
                {"node_id": nid, "seconds": 601}, {"node_id": nid, "seconds": "30"},
                {"node_id": nid, "seconds": True}, {"node_id": 5, "seconds": 30},
                {"node_id": nid, "seconds": float("nan")}):
        client.post("/v1/reader/dwell", bad, expect=400)
    client.post("/v1/reader/dwell", {"node_id": "nope", "seconds": 30}, expect=404)
    assert app.signals.counted()["topics"] == [{"title": "Trebuchet", "opens": 0, "seconds": 42.5}]


def test_style_lean_tie_is_even(g, sig):
    a, b = _nodes(g, "A thing", "B thing")
    for node, via in [(a, "context")] * 4 + [(b, "sideways")] * 4:
        sig.record("open", node, via)
    c = sig.counted()
    assert c["style"]["context"] == c["style"]["sideways"] == 0.5
    assert c["style"]["lean"] == "even" and style_hint(c) == ""
    sig.record("open", b, "sideways")
    assert sig.counted()["style"]["lean"] == "sideways"


def test_level_lean_needs_two(g, sig):
    (a,) = _nodes(g, "A thing")
    sig.record("depth", a, "simpler")
    assert sig.counted()["level"]["lean"] == "even"  # one ask is too little to call
    sig.record("depth", a, "simpler")
    assert sig.counted()["level"]["lean"] == "simpler"
    sig.record("depth", a, "deeper")
    assert sig.counted()["level"]["lean"] == "simpler"  # 2 to 1: still a lead of one
    sig.record("depth", a, "deeper")
    assert sig.counted()["level"]["lean"] == "even"


def test_style_counts_matches_counted_without_topics(g, sig):
    (a,) = _nodes(g, "A thing")
    for _ in range(6):
        sig.record("open", a, "general")
    assert sig.style_counts() == sig.counted()["style"]
    assert sig.style_counts()["lean"] == "general"


def test_forget_visit_starts_a_new_visit(g, sig):
    (a,) = _nodes(g, "A thing")
    assert sig.dwell(a, 600) == 600 and sig.dwell(a, 10) == 0
    sig.forget_visit()
    assert sig.dwell(a, 10) == 10


def test_counted_cache_never_keeps_a_count_older_than_a_signal(g, sig):
    """A count computed while a signal arrives is not cached: the next call sees the signal."""
    (a,) = _nodes(g, "A thing")
    sig.record("open", a, "start")
    real = g.reader_topics
    fired = []

    def topics_then_signal(limit=8):
        out = real(limit)
        if not fired:  # a signal lands between the queries and the store
            fired.append(1)
            sig.record("open", a, "start")
        return out

    g.reader_topics = topics_then_signal
    assert sig.counted()["opens"] == 1  # computed before the second open
    assert sig.counted()["opens"] == 2  # ...and not served from the cache afterwards
