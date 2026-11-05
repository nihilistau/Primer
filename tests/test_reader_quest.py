import json
import threading

import pytest

from primer.engines import EngineError
from primer.reader.quest import (MAX_ROOMS, NEED_PAGES, REREAD, ROOM_CHARS, SKIP_AFTER,
                                 QuestConflict, QuestError, QuestNotFound, Quests, QuestUnavailable)
from primer.reader.quiz import Quiz
from primer.reader.signals import Signals
from tests.fakes import FakeEngine


def md(title: str) -> str:
    return (f"# {title}\n\nThe {title} stood beside a heavy counterweight.\n\n"
            f"People came to the {title} because a falling weight stores energy.")


def door(title: str) -> str:
    return json.dumps({"question": f"What stood beside the {title}?",
                       "answer": f"A heavy counterweight stood beside the {title}.",
                       "hint": f"Look at the first line about the {title}."})


def mark(m: str) -> str:
    return json.dumps({"mark": m, "feedback": "The page says 'a heavy counterweight'."})


class Gate:
    def __init__(self):
        self.turns = 0
        self.held = False

    def page(self):
        gate = self

        class Turn:
            def __enter__(self):
                gate.turns += 1
                gate.held = True

            def __exit__(self, *exc):
                gate.held = False
        return Turn()


class Model(FakeEngine):
    """Answers a room description, a door or a mark by the system prompt; per-title overrides
    for descriptions and doors, a queue of marks."""

    def __init__(self, gate=None):
        super().__init__(self._reply)
        self.gate = gate
        self.outside_gate = 0
        self.rooms: dict[str, list] = {}  # title -> replies (last repeats); callable allowed
        self.doors: dict[str, list] = {}
        self.marks: list[str] = []

    def complete(self, messages, **kw):
        if self.gate is not None and not self.gate.held:
            self.outside_gate += 1
        return super().complete(messages, **kw)

    @staticmethod
    def _title(messages) -> str:
        user = messages[-1]["content"]
        return user.split('"')[1]

    def _pick(self, table, title, default):
        replies = table.get(title)
        if not replies:
            return default
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return reply() if callable(reply) else reply

    def _reply(self, messages):
        system = messages[0]["content"]
        title = self._title(messages) if "Page title" in messages[-1]["content"] else ""
        if "room in a quest" in system:
            return self._pick(self.rooms, title, f"You walk into a hall of the {title}, hung with ropes.")
        if "riddle of a door" in system:
            return self._pick(self.doors, title, door(title))
        if self.marks:
            return self.marks.pop(0)
        return mark("right")


def _page(g, title, frm=None, kind="concept", parent=None, version=True):
    node = g.node_for(title, kind, parent)
    g.add_edge(frm, node.id, title, "start" if frm is None else "sideways")
    if version:
        g.add_version(node.id, md(title), "storyteller", "normal")
    return node.id


def _open(g, nid, at):
    g.add_reader_event(at, "open", nid, "start")


@pytest.fixture
def setup(g):
    gate = Gate()
    engine = Model(gate)
    quiz = Quiz(g, lambda: engine, gate, Signals(g))
    quests = Quests(g, quiz, lambda: engine, gate)
    return quests, engine, gate


def _stored(q) -> list[dict]:
    return json.loads(_G[0].get_quest(q["id"])["rooms_json"])


def _titles(q) -> list[str]:
    return [r["title"] for r in _stored(q)]


_G = []


@pytest.fixture(autouse=True)
def _graph(g):
    _G[:] = [g]


def _built(quests, **kw) -> dict:
    q = quests.build(**kw)
    assert quests.wait(5)
    return quests.get(q["id"])


def test_quest_builds_rooms_in_trail_order(g, setup):
    quests, engine, gate = setup
    ids = {t: _page(g, t) for t in "ABCDEF"}
    for i, t in enumerate("ABCDEF"):
        _open(g, ids[t], 100.0 + i)
    _open(g, ids["B"], 200.0)  # B read again, last
    engine.rooms["C"] = ['"**You enter** a <b>hall</b> of C." It is big. And more.']
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "You step into the D room, all ropes."
    engine.rooms["D"] = [slow]
    q = quests.build()
    assert q["status"] == "building" and q["building"] == {"done": 0, "total": 5}
    assert started.wait(5)
    now = quests.current()
    assert now["id"] == q["id"] and now["building"] == {"done": 1, "total": 5}
    with pytest.raises(QuestConflict):
        quests.build()
    release.set()
    assert quests.wait(5)
    q = quests.get(q["id"])
    assert q["status"] == "active" and "building" not in q
    assert _titles(q) == ["C", "D", "E", "F", "B"]  # oldest -> newest
    first = q["rooms"][0]
    assert first["description"] == "You enter a hall of C."  # plain text, one sentence
    assert all(len(r["description"]) <= ROOM_CHARS for r in _stored(q))
    assert q["rooms"][1:] == [{"index": i, "locked": True} for i in range(1, 5)]  # rooms unfold
    assert first["question"] == "What stood beside the C?"
    assert "answer" not in first and "hint" not in first  # never before the door opens
    assert (first["opened"], first["skipped"], first["tries"]) == (False, False, 0)
    assert q["at_room"] == 0 and q["can_skip"] is False
    assert engine.outside_gate == 0 and gate.held is False
    stored = json.loads(g.get_quest(q["id"])["rooms_json"])
    assert set(stored[0]) >= {"node_id", "title", "description", "question", "answer", "hint",
                              "opened", "skipped", "tries"}
    assert stored[0]["answer"].startswith("A heavy counterweight")
    # a chosen page's trail: its ancestors by edges and parent, oldest first, then itself
    x = _page(g, "X")
    gap = _page(g, "Gap", frm=x, version=False)  # never written: skipped
    y = _page(g, "Y", frm=gap)
    z = _page(g, "Z", kind="context", parent=y)
    g.add_edge(ids["A"], z, "Z", "sideways")  # a later arrival does not change the trail
    q2 = _built(quests, from_node_id=z)
    assert _titles(q2) == ["X", "Y", "Z"]
    assert quests.get(q["id"])["status"] == "abandoned"  # one quest at a time
    assert quests.current()["id"] == q2["id"]
    q3 = _built(quests, from_node_id=z, rooms=2)
    assert _titles(q3) == ["Y", "Z"]
    with pytest.raises(QuestNotFound):
        quests.build(from_node_id="missing")
    for bad in (1, MAX_ROOMS + 1, "5", True):
        with pytest.raises(ValueError):
            quests.build(rooms=bad)


def test_door_opens_on_right(g, setup):
    quests, engine, gate = setup
    a, b = _page(g, "Alpha"), _page(g, "Beta")
    _open(g, a, 1.0)
    _open(g, b, 2.0)
    q = _built(quests)
    calls = len(engine.calls)
    for bad in ("", "  ", "x" * 2001, None):
        with pytest.raises(ValueError):
            quests.answer(q["id"], bad)
    assert len(engine.calls) == calls
    with pytest.raises(QuestNotFound):
        quests.answer("nope", "an answer")
    engine.marks = [mark("right")]
    out = quests.answer(q["id"], "a counterweight")
    assert out["mark"] == "right" and out["opened"] is True and out["finished"] is False
    assert out["answer"].startswith("A heavy counterweight") and "hint" not in out
    marked = engine.calls[-1]
    assert "What stood beside the Alpha?" in marked[-1]["content"]
    assert "a counterweight" in marked[-1]["content"] and "Alpha" in marked[-1]["content"]
    now = quests.get(q["id"])
    assert now["at_room"] == 1 and now["rooms"][0]["opened"] is True
    assert now["rooms"][0]["answer"].startswith("A heavy") and now["rooms"][0]["hint"]
    assert "answer" not in now["rooms"][1] and "hint" not in now["rooms"][1]
    assert now["rooms"][1]["question"] == "What stood beside the Beta?"  # the current door
    engine.marks = [mark("right")]
    out = quests.answer(q["id"], "a counterweight")
    assert out["finished"] is True and out["summary"] == {"opened": 2, "skipped": 0, "rooms": 2}
    done = quests.get(q["id"])
    assert done["status"] == "finished" and done["summary"] == out["summary"]
    assert quests.current()["id"] == q["id"]  # the last finished quest
    with pytest.raises(QuestConflict):
        quests.answer(q["id"], "again")


def test_hint_then_skip_after_three(g, setup):
    quests, engine, gate = setup
    a, b, c = _page(g, "Alpha"), _page(g, "Beta"), _page(g, "Gamma")
    for i, n in enumerate((a, b, c)):
        _open(g, n, float(i))
    q = _built(quests)
    engine.marks = [mark("partly")]
    out = quests.answer(q["id"], "a rope")
    assert out["mark"] == "partly" and out["opened"] is False
    assert out["hint"] == "Look at the first line about the Alpha." and out["can_skip"] is False
    assert "answer" not in out
    shown = quests.get(q["id"])["rooms"]
    assert shown[0]["hint"] == out["hint"] and "answer" not in shown[0]  # survives a reload
    assert shown[1] == {"index": 1, "locked": True}
    with pytest.raises(QuestConflict):
        quests.skip(q["id"])
    engine.marks = [mark("not_yet")]
    out = quests.answer(q["id"], "pancakes")
    assert out["hint"] == REREAD and out["can_skip"] is False and out["tries"] == 2
    engine.marks = [mark("not_yet")]
    out = quests.answer(q["id"], "pancakes")
    assert out["tries"] == SKIP_AFTER == 3 and out["can_skip"] is True
    assert quests.get(q["id"])["can_skip"] is True
    out = quests.skip(q["id"])
    assert out["skipped"] is True and out["answer"].startswith("A heavy") and out["finished"] is False
    now = quests.get(q["id"])
    assert now["at_room"] == 1 and now["can_skip"] is False
    room = now["rooms"][0]
    assert room["skipped"] is True and room["opened"] is False and room["tries"] == 3
    assert room["hint"] and room["answer"]
    engine.marks = [mark("right"), mark("right")]
    quests.answer(q["id"], "x")
    out = quests.answer(q["id"], "x")
    assert out["summary"] == {"opened": 2, "skipped": 1, "rooms": 3}
    with pytest.raises(QuestNotFound):
        quests.skip("nope")


def test_quest_short_trail(g, setup):
    quests, engine, gate = setup
    with pytest.raises(QuestError) as e:
        quests.build()
    assert str(e.value) == NEED_PAGES == "read at least two pages first"
    a = _page(g, "Alpha")
    _open(g, a, 1.0)
    with pytest.raises(QuestError):
        quests.build()
    with pytest.raises(QuestError):  # a trail of one page
        quests.build(from_node_id=a)
    assert quests.current() is None
    b = _page(g, "Beta", frm=a)
    _open(g, b, 2.0)
    q = _built(quests)  # five asked for, two read: a shorter quest
    assert q["status"] == "active" and _titles(q) == ["Alpha", "Beta"]
    assert _titles(_built(quests, from_node_id=b)) == ["Alpha", "Beta"]
    # a room whose generation fails twice is dropped
    c = _page(g, "Gamma")
    _open(g, c, 3.0)
    engine.rooms["Beta"] = ["   ", "``` ```"]
    q = _built(quests)
    assert q["status"] == "active" and _titles(q) == ["Alpha", "Gamma"]
    # ...and when that leaves fewer than two rooms, the quest fails with a reason
    engine.doors["Gamma"] = ["junk"]
    q = _built(quests)
    assert q["status"] == "failed" and q["rooms"] == []
    assert "two rooms" in q["error"]
    # opens fall back to the trail of arrivals when the reader has no open signals
    g.reset_reader({})
    engine.rooms, engine.doors = {}, {}
    q = _built(quests)
    assert _titles(q) == ["Alpha", "Beta", "Gamma"]
    # the engine down: before building, a 503-style error; while building, the quest fails
    engine_ref = quests.text_engine
    quests.text_engine = lambda: None
    with pytest.raises(QuestUnavailable):
        quests.build()
    quests.text_engine = engine_ref

    def down():
        raise EngineError("cannot reach engine at http://127.0.0.1:1")
    engine.rooms["Alpha"] = [down]
    q = _built(quests)
    assert q["status"] == "failed" and "cannot reach engine" in q["error"]


def test_reset_discards_a_building_quest(g, setup):
    quests, engine, gate = setup
    a, b = _page(g, "Alpha"), _page(g, "Beta")
    _open(g, a, 1.0)
    _open(g, b, 2.0)
    finished = _built(quests)
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "You enter the Beta hall."
    engine.rooms["Beta"] = [slow]
    q = quests.build()
    assert started.wait(5)
    quests.reset()
    assert quests.current() is None and g.get_quest(finished["id"]) is None
    engine.rooms["Beta"] = []
    again = quests.build()  # a reset frees the builder at once
    release.set()
    assert quests.wait(5)
    assert g.get_quest(q["id"]) is None  # the late build stored nothing
    assert quests.get(again["id"])["status"] == "active"
    quests.reset()
    quests.reset()  # idempotent
    assert quests.current() is None


def test_interrupted_build_fails_on_start(g, setup):
    quests, engine, gate = setup
    g.add_quest("q1", 1.0, "building", "[]", 0, 0)
    fresh = Quests(g, quests.quiz, lambda: engine, gate)
    q = fresh.get("q1")
    assert q["status"] == "failed" and q["error"]


def test_quest_routes(client, app_parts):
    app, _ = app_parts
    g = app.graph
    engine = Model()
    app.pages.text = engine
    assert app.quests.forget in app.profile.on_reset
    assert client.get("/v1/quest") == {"quest": None}
    err = client.post("/v1/quest", {}, expect=400)
    assert err["error"] == NEED_PAGES
    a, b, c = _page(g, "Alpha"), _page(g, "Beta"), _page(g, "Gamma")
    for i, n in enumerate((a, b, c)):
        _open(g, n, float(i))
    for bad in ({"rooms": 1}, {"rooms": 9}, {"rooms": "5"}, {"rooms": True},
                {"from_node_id": 5}, {"from_node_id": ""}):
        client.post("/v1/quest", bad, expect=400)
    client.post("/v1/quest", {"from_node_id": "missing"}, expect=404)
    out = client.post("/v1/quest", {"rooms": 3}, expect=202)
    qid = out["quest_id"]
    assert out["quest"]["id"] == qid
    assert app.quests.wait(5)
    q = client.get("/v1/quest")["quest"]
    assert q["id"] == qid and q["status"] == "active" and len(q["rooms"]) == 3
    assert all("answer" not in r and "hint" not in r for r in q["rooms"])
    assert [r.get("locked") for r in q["rooms"]] == [False, True, True]
    for bad in ({"quest_id": qid, "answer": ""}, {"quest_id": qid, "answer": "x" * 2001},
                {"quest_id": qid}, {"answer": "x"}):
        client.post("/v1/quest/answer", bad, expect=400)
    client.post("/v1/quest/answer", {"quest_id": "nope", "answer": "x"}, expect=404)
    client.post("/v1/quest/skip", {"quest_id": qid}, expect=409)
    client.post("/v1/quest/skip", {}, expect=400)
    engine.replies = lambda m: mark("not_yet")
    for _ in range(3):
        out = client.post("/v1/quest/answer", {"quest_id": qid, "answer": "pancakes"})
    assert out["mark"] == "not_yet" and out["can_skip"] is True and out["hint"] == REREAD
    out = client.post("/v1/quest/skip", {"quest_id": qid})
    assert out["skipped"] is True and out["answer"]
    engine.replies = lambda m: "junk"
    err = client.post("/v1/quest/answer", {"quest_id": qid, "answer": "x"}, expect=502)
    assert err["error"]

    class Down:
        def complete(self, *a, **k):
            raise EngineError("cannot reach engine")
    app.pages.text = Down()
    err = client.post("/v1/quest/answer", {"quest_id": qid, "answer": "x"}, expect=503)
    assert "cannot reach engine" in err["error"]
    app.pages.text = None
    client.post("/v1/quest", {}, expect=503)
    app.pages.text = engine
    engine.replies = engine._reply
    client.post("/v1/quest/answer", {"quest_id": qid, "answer": "x"})
    out = client.post("/v1/quest/answer", {"quest_id": qid, "answer": "x"})
    assert out["finished"] is True and out["summary"] == {"opened": 2, "skipped": 1, "rooms": 3}
    assert client.get("/v1/quest")["quest"]["status"] == "finished"
    client.post("/v1/quest/answer", {"quest_id": qid, "answer": "x"}, expect=409)
    # the reader reset deletes quests
    client.post("/v1/reader/reset", {})
    assert client.get("/v1/quest") == {"quest": None}


def _two_room_quest(g, quests):
    a, b = _page(g, "Alpha"), _page(g, "Beta")
    _open(g, a, 1.0)
    _open(g, b, 2.0)
    return _built(quests)


def _in_thread(fn, *args):
    box = {}

    def run():
        try:
            box["out"] = fn(*args)
        except Exception as e:  # noqa: BLE001
            box["err"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, box


def test_two_answers_at_once_one_wins(g, setup):
    quests, engine, gate = setup
    q = _two_room_quest(g, quests)
    both = threading.Barrier(2, timeout=5)
    engine.replies = lambda m: (both.wait(), mark("right"))[1]
    (t1, b1), (t2, b2) = _in_thread(quests.answer, q["id"], "x"), _in_thread(quests.answer, q["id"], "y")
    t1.join(5)
    t2.join(5)
    outs, errs = [b for b in (b1, b2) if "out" in b], [b["err"] for b in (b1, b2) if "err" in b]
    assert len(outs) == 1 and outs[0]["out"]["opened"] is True
    assert len(errs) == 1 and isinstance(errs[0], QuestConflict) and "moved on" in str(errs[0])
    now = quests.get(q["id"])
    assert now["at_room"] == 1 and now["rooms"][0]["tries"] == 0


def test_answer_across_a_reset(g, setup):
    quests, engine, gate = setup
    q = _two_room_quest(g, quests)
    started, release = threading.Event(), threading.Event()

    def slow(m):
        started.set()
        release.wait(5)
        return mark("right")
    engine.replies = slow
    t, box = _in_thread(quests.answer, q["id"], "x")
    assert started.wait(5)
    quests.reset()
    release.set()
    t.join(5)
    assert isinstance(box.get("err"), QuestConflict)  # 409, nothing stored
    assert g.get_quest(q["id"]) is None
    with pytest.raises(QuestNotFound):  # after the reset: 404
        quests.answer(q["id"], "x")


def test_skip_racing_an_answer(g, setup):
    quests, engine, gate = setup
    q = _two_room_quest(g, quests)
    engine.marks = [mark("not_yet")] * 3
    for _ in range(3):
        quests.answer(q["id"], "pancakes")
    assert quests.get(q["id"])["can_skip"] is True and quests.get(q["id"])["tries"] == 3
    started, release = threading.Event(), threading.Event()

    def slow(m):
        started.set()
        release.wait(5)
        return mark("right")
    engine.replies = slow
    t, box = _in_thread(quests.answer, q["id"], "a counterweight")
    assert started.wait(5)
    out = quests.skip(q["id"])  # wins while the answer is being marked
    release.set()
    t.join(5)
    assert out["skipped"] is True
    assert isinstance(box.get("err"), QuestConflict) and "moved on" in str(box["err"])
    now = quests.get(q["id"])
    assert now["at_room"] == 1 and now["tries"] == 0 and now["rooms"][0]["skipped"] is True
    assert now["rooms"][0]["opened"] is False


def test_door_follows_a_rewritten_page(g, setup):
    quests, engine, gate = setup
    a, b = _page(g, "Alpha"), _page(g, "Beta")
    _open(g, a, 1.0)
    _open(g, b, 2.0)
    asked = []

    def rewrite():
        asked.append(1)
        if len(asked) == 1:  # the page is regenerated while its door is being asked
            g.add_version(b, md("Beta") + "\n\nA second version.", "storyteller", "normal")
        return door("Beta")
    engine.doors["Beta"] = [rewrite]
    q = _built(quests)
    rooms = _stored(q)
    assert len(asked) == 2  # asked again for the new version
    assert rooms[1]["version_n"] == 2 and rooms[0]["version_n"] == 1


def test_stale_build_stops_after_reset(g, setup):
    quests, engine, gate = setup
    ids = [_page(g, t) for t in ("Alpha", "Beta", "Gamma")]
    for i, n in enumerate(ids):
        _open(g, n, float(i))
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return "You enter the Alpha hall."
    engine.rooms["Alpha"] = [slow]
    quests.build()
    stale = quests._thread
    assert started.wait(5)
    quests.reset()
    release.set()
    stale.join(5)
    assert not stale.is_alive()
    asked = [m[-1]["content"] for m in engine.calls if "room in a quest" in m[0]["content"]]
    assert not any('"Beta"' in c or '"Gamma"' in c for c in asked)  # stopped before room 2


def test_a_miss_gives_no_feedback_only_the_hint(g, setup):
    """Found live: the mark's feedback quoted the answer and spoiled the door."""
    quests, engine, _ = setup
    ids = [_page(g, t) for t in "AB"]
    for i, nid in enumerate(ids):
        _open(g, nid, 100.0 + i)
    q = _built(quests)
    engine.marks = [json.dumps({"mark": "partly", "feedback": "The page says 'a heavy counterweight'."}),
                    mark("right")]
    miss = quests.answer(q["id"], "a rope")
    assert miss["mark"] == "partly" and "feedback" not in miss and "answer" not in miss
    assert miss["hint"] == "Look at the first line about the A."
    hit = quests.answer(q["id"], "a heavy counterweight")
    assert hit["mark"] == "right" and hit["feedback"] and hit["answer"]


def test_leave_a_quest(g, setup):
    quests, engine, _ = setup
    ids = [_page(g, t) for t in "ABC"]
    for i, nid in enumerate(ids):
        _open(g, nid, 100.0 + i)
    first = _built(quests)
    quests.answer(first["id"], "a heavy counterweight")
    assert quests.abandon(first["id"]) == {"quest": None}
    assert quests.current() is None and g.get_quest(first["id"])["status"] == "abandoned"
    with pytest.raises(QuestConflict):
        quests.abandon(first["id"])  # not being played any more
    with pytest.raises(QuestNotFound):
        quests.abandon("nope")
    again = _built(quests)  # a new build is allowed at once
    assert again["status"] == "active" and quests.current()["id"] == again["id"]


def test_leave_route(client, app_parts):
    app, _ = app_parts
    g = app.graph
    engine = Model()
    app.pages.text = engine
    for i, t in enumerate("AB"):
        _open(g, _page(g, t), 100.0 + i)
    qid = client.post("/v1/quest", {}, expect=202)["quest_id"]
    assert app.quests.wait(5)
    assert client.post("/v1/quest/abandon", {"quest_id": qid}) == {"quest": None}
    assert client.get("/v1/quest")["quest"] is None
    client.post("/v1/quest/abandon", {"quest_id": qid}, expect=409)
    client.post("/v1/quest/abandon", {"quest_id": "nope"}, expect=404)
    client.post("/v1/quest/abandon", {}, expect=400)
