import json
import threading

import pytest

from primer.engines import EngineError
from primer.stories.service import StoryService
from primer.tales.bible import Bible
from primer.tales.service import (BUSY, NO_MATERIAL, START_FAILED, TaleConflict, TaleError,
                                  TaleNotFound, TaleService, TaleUnavailable)
from tests.fakes import FakeEngine
from tests.test_stories_service import FakeVoice

START = json.dumps({
    "title": "Nell and the Brass Heron",
    "hero": {"name": "Nell", "description": "A girl who builds things.",
             "look": "a girl of twelve with a red scarf and a brass goggle"},
    "companions": [{"name": "Pip", "look": "a small grey mouse in a blue waistcoat",
                    "note": "Nell's mouse"}],
    "place": "the workshop by the sea",
    "thread": "who left the heron on the doorstep?"})
SCENES = ("## The Siege\nNell watched the trebuchet fling its stone.\n\n"
          "## Pip Helps\nPip ran along the beam to the counterweight.\n\n"
          "## Home\nThe tide came in as they walked home.")
UPDATE = json.dumps({"summary": "Nell saw a trebuchet at a siege and Pip helped.",
                     "new_companions": [{"name": "Ada", "look": "a tall engineer", "note": "builder"}],
                     "new_places": ["the castle walls"], "open_threads": ["where is the heron?"]})


class Gate:
    def __init__(self):
        self.turns = 0

    def page(self):
        gate = self

        class Turn:
            def __enter__(self):
                gate.turns += 1

            def __exit__(self, *exc):
                return False
        return Turn()


class Model(FakeEngine):
    """Answers the start, the chapter and the bible update by the system prompt; each answer
    can be replaced (a string, or a callable of the messages)."""

    def __init__(self):
        super().__init__(self._reply)
        self.start, self.chapter, self.update = START, SCENES, UPDATE
        self.temps = []

    def complete(self, messages, *, max_tokens=400, temperature=0.2):
        self.temps.append((self.kind(messages), temperature))
        return super().complete(messages, max_tokens=max_tokens, temperature=temperature)

    @staticmethod
    def kind(messages):
        system = messages[0]["content"]
        if "begin a continuing" in system:
            return "start"
        if "next chapter" in system:
            return "chapter"
        return "update"

    def _reply(self, messages):
        out = getattr(self, self.kind(messages))
        return out(messages) if callable(out) else out

    def of(self, kind):
        return [m for m in self.calls if self.kind(m) == kind]


class Images:
    def __init__(self):
        self.creates = []

    def available(self, kind="image"):
        return True, ""

    def create(self, node_id, phrase, subject, **kw):
        self.creates.append({"phrase": phrase, "subject": subject, **kw})
        n = len(self.creates)

        class M:
            id = f"media{n}"
        return M()


@pytest.fixture
def world(g, tmp_path):
    made = []

    def make(model=None, images=None, notes="I'm 12 and love machines."):
        model = model or Model()
        gate = Gate()
        stories = StoryService(g, FakeEngine(["unused"]), FakeEngine(['["trebuchet"]']),
                               lambda: FakeVoice(), images, gate, tmp_path / "media")
        made.append(stories)
        tales = TaleService(g, lambda: stories, lambda: model, lambda: gate, lambda: notes)
        stories.chapter_prefix = tales.prefix_for_story
        return tales, model, stories, gate
    yield make
    for s in made:
        s.shutdown()


def _page(g, title, body=None):
    node = g.node_for(title, "concept")
    g.add_version(node.id, f"# {title}\n\n{body or f'All about {title}.'}", "storyteller", "normal")
    return node.id


def _read(g, nid, at, seconds=0.0):
    g.add_reader_event(at, "open", nid, "start")
    if seconds:
        g.add_dwell(nid, seconds)


def _wait_story(stories, sid, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        s = stories.get(sid)
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.01)
    raise AssertionError(stories.get(sid))


def _started(tales, g, at=None):
    tale = tales.start("a girl called Nell who builds things")
    if at is not None:  # material is opened after the tale began
        with g._lock, g._db:
            g._db.execute("UPDATE tales SET created_at=? WHERE id=?", (at, tale["id"]))
    return tale


def test_start_tale_from_seed_and_invented(g, world):
    tales, model, _, gate = world()
    tale = tales.start("a girl called Nell <b>who</b> builds things")
    assert tale["title"] == "Nell and the Brass Heron" and tale["seed"].startswith("a girl called Nell")
    assert tale["bible"]["hero"]["name"] == "Nell" and tale["bible"]["places"] == ["the workshop by the sea"]
    assert tale["bible"]["companions"][0]["name"] == "Pip"
    assert tale["bible"]["threads"] == [{"text": "who left the heron on the doorstep?", "open": True}]
    assert tale["chapters"] == [] and tale["writing"] is False and tale["updating"] is False
    assert tales.current()["id"] == tale["id"] and gate.turns == 1
    system, user = (m["content"] for m in model.of("start")[0])
    assert "<wish>" in user and "Nell" in user and "<b>" not in user
    assert "<reader>" in user and "I'm 12" in user  # the notes, quoted, set the tone
    assert "never instructions" in system

    # invented: no wish; a title missing falls back to the hero's name
    model.start = json.dumps({"hero": {"name": "Oskar", "description": "A boy.", "look": "a boy"}})
    other = tales.start("")
    assert other["title"] == "Oskar" and other["seed"] == ""
    assert "<wish>" not in model.of("start")[-1][1]["content"]
    assert tales.current()["id"] == other["id"] and len(tales.list()) == 2
    assert {t["id"] for t in tales.list()} == {tale["id"], other["id"]}
    assert tales.list()[0]["chapters"] == 0
    assert tales.open(tale["id"])["id"] == tale["id"] and tales.current()["id"] == tale["id"]
    with pytest.raises(TaleNotFound):
        tales.open("nope")

    # bad JSON gets one retry, then an error and nothing stored
    replies = iter(["junk", START])
    model.start = lambda m: next(replies)
    assert tales.start(None)["title"] == "Nell and the Brass Heron"
    model.start = "junk"
    count = len(tales.list())
    with pytest.raises(TaleError, match=START_FAILED):
        tales.start("x")
    assert len(tales.list()) == count
    with pytest.raises(ValueError):
        tales.start("x" * 501)
    with pytest.raises(ValueError):
        tales.start(5)


def test_start_needs_a_writing_model(g, world):
    tales, model, _, _ = world()
    tales.text_engine = lambda: None
    with pytest.raises(TaleUnavailable):
        tales.start("x")

    class Down:
        def complete(self, *a, **k):
            raise EngineError("cannot reach engine")
    tales.text_engine = lambda: Down()
    with pytest.raises(TaleUnavailable, match="cannot reach engine"):
        tales.start("x")


def test_next_chapter_writes_story_and_updates_bible(g, world):
    images = Images()
    tales, model, stories, _ = world(images=images)
    with pytest.raises(TaleNotFound):
        tales.next_chapter()  # no tale yet
    tale = _started(tales, g, at=100.0)
    with pytest.raises(ValueError, match=NO_MATERIAL):
        tales.next_chapter()
    a = _page(g, "Trebuchet", "The trebuchet used a counterweight. " + "x" * 3000)
    b, c, d = _page(g, "Siege"), _page(g, "Castle"), _page(g, "Moat")
    _read(g, a, 110.0, 60.0)
    _read(g, b, 111.0, 30.0)
    _read(g, c, 112.0, 20.0)
    _read(g, d, 113.0, 1.0)
    preview = tales.preview()
    assert preview["waiting"] == 4 and [p["node_id"] for p in preview["pages"]] == [a, b, c]
    assert set(preview["pages"][0]) == {"node_id", "title", "seconds", "opens"}

    out = tales.next_chapter()
    assert out["n"] == 1 and out["story_id"]
    assert tales.wait(5)
    story = _wait_story(stories, out["story_id"])
    assert story["kind"] == "chapter" and story["node_id"] == a  # the main page: the first used
    assert story["title"] == "Chapter 1: The Siege"
    assert [s["title"] for s in story["scenes"]] == ["The Siege", "Pip Helps", "Home"]
    assert story["highlights"] == ["trebuchet"]
    assert stories.text.calls == []  # the scenes came from the tale, not the story writer

    system, user = (m["content"] for m in model.of("chapter")[0])
    assert "Never mention being an AI" in system and "## " in system
    assert "<story_notes>" in user and "Hero: Nell" in user
    assert user.count("<source ") == 3 and 'title="Trebuchet"' in user and "counterweight" in user
    assert "x" * 2600 not in user  # excerpts are cut to 2500 characters
    assert "<reader>" in user and "I'm 12" in user
    assert ("chapter", 0.8) in model.temps and any(k == "update" and t <= 0.3 for k, t in model.temps)
    upd = model.of("update")[0][1]["content"]
    assert "<chapter>" in upd and "Pip ran along the beam" in upd

    cur = tales.current()
    ch = cur["chapters"][0]
    assert ch["n"] == 1 and ch["story_id"] == out["story_id"] and ch["title"] == "Chapter 1: The Siege"
    assert [p["node_id"] for p in ch["pages"]] == [a, b, c] and ch["pages"][0]["title"] == "Trebuchet"
    assert ch["summary"] == "Nell saw a trebuchet at a siege and Pip helped."
    assert ch["update_failed"] is False and ch["status"] in ("narrating", "done")
    bible = cur["bible"]
    assert bible["chapters"] == [{"n": 1, "text": ch["summary"]}]  # the row and the bible agree
    assert [c["name"] for c in bible["companions"]] == ["Pip", "Ada"]
    assert "the castle walls" in bible["places"]
    assert {"text": "where is the heron?", "open": True} in bible["threads"]
    # the next chapter waits for pages opened after this one began (Moat, read before, is past)
    assert tales.preview() == {"pages": [], "waiting": 0}

    # the reader's picks: used pages are fine when chosen
    out2 = tales.next_chapter([b, "missing", b])
    assert out2["n"] == 2 and tales.wait(5)
    assert tales.current()["chapters"][1]["pages"] == [{"node_id": b, "title": "Siege"}]
    assert "Chapter 2" in model.of("chapter")[-1][1]["content"] or \
        "chapter 2" in model.of("chapter")[-1][1]["content"].lower()
    with pytest.raises(ValueError, match=NO_MATERIAL):
        tales.next_chapter(["missing"])
    with pytest.raises(ValueError):
        tales.next_chapter("abc")


def test_empty_chapter_fails_and_adds_nothing(g, world):
    tales, model, _, _ = world()
    tale = _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    model.chapter = "   "
    with pytest.raises(TaleError):
        tales.next_chapter()
    assert g.tale_chapters(tale["id"]) == [] and tales.current()["writing"] is False
    model.chapter = SCENES
    assert tales.next_chapter()["n"] == 1


def test_update_failure_keeps_chapter(g, world):
    tales, model, stories, _ = world()
    tale = _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    before = tales.current()["bible"]
    model.update = '{"summary": ""} and then {"new_places": ["x"]}'
    out = tales.next_chapter()
    assert tales.wait(5)
    assert len(model.of("update")) == 2  # one retry
    cur = tales.current()
    ch = cur["chapters"][0]
    assert ch["story_id"] == out["story_id"] and ch["update_failed"] is True and ch["summary"] == ""
    assert cur["bible"] == before and cur["updating"] is False
    assert stories.get(out["story_id"])["kind"] == "chapter"
    with pytest.raises(TaleNotFound):
        tales.retry_update(tale["id"], 9)
    with pytest.raises(TaleNotFound):
        tales.retry_update("nope", 1)

    model.update = UPDATE
    tales.retry_update(tale["id"], 1)
    assert tales.wait(5)
    cur = tales.current()
    assert cur["chapters"][0]["update_failed"] is False
    assert cur["chapters"][0]["summary"] == cur["bible"]["chapters"][0]["text"]
    with pytest.raises(TaleConflict):
        tales.retry_update(tale["id"], 1)  # nothing to retry


def test_retry_of_an_earlier_chapter_keeps_summary_order(g, world):
    tales, model, _, _ = world()
    tale = _started(tales, g, at=100.0)
    a, b = _page(g, "A"), _page(g, "B")
    _read(g, a, 110.0)
    model.update = "junk"
    tales.next_chapter()
    assert tales.wait(5)
    _read(g, b, g.tale_chapters(tale["id"])[-1]["created_at"] + 1)
    model.update = json.dumps({"summary": "second"})
    tales.next_chapter()
    assert tales.wait(5)
    model.update = json.dumps({"summary": "first"})
    tales.retry_update(tale["id"], 1)
    assert tales.wait(5)
    cur = tales.current()
    assert cur["bible"]["chapters"] == [{"n": 1, "text": "first"}, {"n": 2, "text": "second"}]
    assert [c["summary"] for c in cur["chapters"]] == ["first", "second"]
    # the notes label each summary by its own chapter number
    block = Bible.from_json(g.get_tale(tale["id"])["bible_json"]).prompt_block()
    assert "Chapter 1: first" in block and "Chapter 2: second" in block


def test_one_chapter_at_a_time(g, world):
    tales, model, _, _ = world()
    tale = _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    _read(g, _page(g, "Siege"), 111.0)
    writing, go = threading.Event(), threading.Event()

    def slow(messages):
        writing.set()
        go.wait(5)
        return SCENES
    model.chapter = slow
    result = {}
    t = threading.Thread(target=lambda: result.update(tales.next_chapter()))
    t.start()
    assert writing.wait(5)
    assert tales.current()["writing"] is True
    with pytest.raises(TaleConflict, match=BUSY):
        tales.next_chapter()
    go.set()
    t.join(5)
    assert result["n"] == 1 and tales.wait(5)

    # while the bible update runs, the next chapter waits too (it continues from the bible)
    updating, go2 = threading.Event(), threading.Event()

    def slow_update(messages):
        updating.set()
        go2.wait(5)
        return UPDATE
    model.chapter = SCENES
    model.update = slow_update
    _read(g, _page(g, "Moat"), g.tale_chapters(tale["id"])[-1]["created_at"] + 1)
    tales.next_chapter()
    assert updating.wait(5)
    cur = tales.current()
    assert cur["updating"] is True and cur["chapters"][-1]["update_failed"] is False
    with pytest.raises(TaleConflict, match=BUSY):
        tales.next_chapter()
    go2.set()
    assert tales.wait(5)
    assert tales.current()["updating"] is False


def test_pages_opened_while_writing_wait_for_the_next_chapter(g, world):
    tales, model, _, _ = world()
    tale = _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    later = _page(g, "Later")

    def scenes(messages):
        import time
        _read(g, later, time.time() + 0.05)  # opened while the chapter is being written
        return SCENES
    model.chapter = scenes
    tales.next_chapter()
    assert tales.wait(5)
    assert [p["node_id"] for p in tales.preview()["pages"]] == [later]


def test_restart_mid_chapter(g, world, monkeypatch):
    tales, model, stories, _ = world()
    tale = _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)

    # dying between the scenes and the chapter row: no half chapter, and Next works again
    real = g.add_tale_chapter

    def die(*a, **k):
        raise RuntimeError("power cut")
    monkeypatch.setattr(g, "add_tale_chapter", die)
    with pytest.raises(RuntimeError):
        tales.next_chapter()
    assert g.tale_chapters(tale["id"]) == [] and tales.current()["writing"] is False
    monkeypatch.setattr(g, "add_tale_chapter", real)

    # dying during the bible update: after a restart the chapter says its update failed
    hold = threading.Event()
    model.update = lambda m: (hold.wait(5), UPDATE)[1]
    out = tales.next_chapter()
    g.fail_unfinished_stories("Primer restarted")  # what serve.py does on start
    tales2, _, stories2, _ = world(model=Model())
    cur = tales2.current()
    assert cur["writing"] is False and cur["updating"] is False
    assert cur["chapters"][0]["update_failed"] is True
    assert cur["chapters"][0]["status"] == "failed"
    # the story's Again redoes the chapter's story from its scenes and the chapter follows it
    again = stories2.again(out["story_id"])
    s = _wait_story(stories2, again)
    assert s["kind"] == "chapter" and [x["title"] for x in s["scenes"]] == ["The Siege", "Pip Helps", "Home"]
    assert s["title"] == "Chapter 1: The Siege"
    assert tales2.current()["chapters"][0]["story_id"] == again
    tales2.retry_update(tale["id"], 1)
    assert tales2.wait(5) and tales2.current()["chapters"][0]["update_failed"] is False
    _read(g, _page(g, "Moat"), g.tale_chapters(tale["id"])[-1]["created_at"] + 1)
    assert tales2.next_chapter()["n"] == 2
    hold.set()
    tales.wait(5)


def test_chapter_pictures_get_the_look(g, world):
    images = Images()
    tales, model, stories, _ = world(images=images)
    _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    out = tales.next_chapter()
    _wait_story(stories, out["story_id"])
    looks = [c.get("prefix", "") for c in images.creates]
    hero = "Nell: a girl of twelve with a red scarf and a brass goggle"  # the name says who
    assert len(looks) == 3 and all(p.startswith(hero) for p in looks)
    assert "Pip: a small grey mouse" in looks[1]
    assert "Pip" not in looks[0] and "Pip" not in looks[2]
    assert [c["phrase"] for c in images.creates] == ["The Siege", "Pip Helps", "Home"]
    assert all(c.get("scene") is True for c in images.creates)  # storybook scenes, not plates


def test_edit_the_bible(g, world):
    tales, model, _, _ = world()
    tale = _started(tales, g)
    tid = tale["id"]
    cur = tales.edit(tid, {"name": "Nella", "look": "a girl in a green coat",
                           "remove_thread": "who left the heron on the doorstep?",
                           "remove_place": "the workshop by the sea"})
    assert cur["bible"]["hero"]["name"] == "Nella" and cur["bible"]["hero"]["look"] == "a girl in a green coat"
    assert cur["bible"]["threads"] == [] and cur["bible"]["places"] == []
    for bad in ({"name": 5}, {"name": ""}, {"look": "x" * 201}, {"remove_thread": "no such"},
                {"remove_place": 3}, {}, {"weird": "x"}):
        with pytest.raises(ValueError):
            tales.edit(tid, bad)
    with pytest.raises(TaleNotFound):
        tales.edit("nope", {"name": "x"})
    assert Bible.from_json(g.get_tale(tid)["bible_json"]).hero["name"] == "Nella"


def test_tale_routes(client, app_parts):
    app, _ = app_parts
    g = app.graph
    model = Model()
    app.pages.text = model
    voice = {"ok": True, "reason": ""}
    app.voice_available = lambda: voice  # the tier has no voice: say it can narrate
    assert app.stories.chapter_prefix == app.tales.prefix_for_story
    state = client.get("/v1/tales")
    assert state == {"current": None, "tales": [], "preview": {"pages": [], "waiting": 0}}
    for bad in ({"seed": 5}, {"seed": "x" * 501}):
        client.post("/v1/tales", bad, expect=400)
    tale = client.post("/v1/tales", {"seed": "a girl called Nell"})["current"]
    assert tale["bible"]["hero"]["name"] == "Nell"
    tid = tale["id"]
    err = client.post("/v1/tales/next", {}, expect=400)
    assert err["error"] == NO_MATERIAL
    client.post("/v1/tales/next", {"pages": "x"}, expect=400)
    nid = _page(g, "Trebuchet")
    g.add_reader_event(g.get_tale(tid)["created_at"] + 1, "open", nid, "start")
    state = client.get("/v1/tales")
    assert state["preview"]["waiting"] == 1 and state["preview"]["pages"][0]["node_id"] == nid
    # the voice can't narrate: refused like Story, before any model call
    voice = {"ok": False, "reason": "the voice server is starting"}
    calls = len(model.calls)
    assert client.post("/v1/tales/next", {"pages": [nid]}, expect=409)["error"] == \
        "the voice server is starting"
    assert len(model.calls) == calls and app.tales.current()["writing"] is False
    voice = {"ok": True, "reason": ""}
    out = client.post("/v1/tales/next", {"pages": [nid]}, expect=202)
    assert out["n"] == 1 and out["story_id"]
    assert app.tales.wait(5)
    state = client.get("/v1/tales")
    cur = state["current"]
    assert set(cur) == {"id", "title", "seed", "bible", "chapters", "writing", "updating"}
    ch = cur["chapters"][0]
    assert set(ch) == {"n", "story_id", "title", "status", "pages", "summary", "update_failed"}
    assert state["tales"][0] == {"id": tid, "title": "Nell and the Brass Heron",
                                 "updated_at": state["tales"][0]["updated_at"], "chapters": 1}
    story = client.get(f"/v1/stories/{out['story_id']}")
    assert story["kind"] == "chapter"
    assert story["tale"] == {"id": tid, "title": "Nell and the Brass Heron", "n": 1}
    # told again: the new story is the chapter, the old one is superseded
    voice = {"ok": False, "reason": "no voice"}
    client.post(f"/v1/stories/{out['story_id']}/again", {}, expect=409)
    voice = {"ok": True, "reason": ""}
    app.stories.chapter_prefix = app.tales.prefix_for_story
    new_id = app.stories.again(out["story_id"])
    assert client.get(f"/v1/stories/{new_id}")["tale"]["n"] == 1
    assert client.get(f"/v1/stories/{out['story_id']}")["tale"] is None
    out = {**out, "story_id": new_id}
    # the page's stories list says which are chapters (the story tray labels them)
    listed = client.get(f"/v1/pages/{nid}")["stories"]
    assert [x["kind"] for x in listed if x["id"] == out["story_id"]] == ["chapter"]
    # a chapter with no scenes can't be told again: 409, not 404
    empty = g.add_story(nid, 1, "chapter", "")
    assert client.post(f"/v1/stories/{empty}/again", {}, expect=409)["error"] == \
        "this chapter has no scenes"
    client.post("/v1/stories/nope/again", {}, expect=404)
    # edit, retry, open
    cur = client.post("/v1/tales/edit", {"tale_id": tid, "name": "Nella"})["current"]
    assert cur["bible"]["hero"]["name"] == "Nella"
    client.post("/v1/tales/edit", {"tale_id": tid, "name": 5}, expect=400)
    client.post("/v1/tales/edit", {"name": "x"}, expect=400)
    client.post("/v1/tales/edit", {"tale_id": "nope", "name": "x"}, expect=404)
    client.post("/v1/tales/retry-update", {"tale_id": tid, "n": 1}, expect=409)
    client.post("/v1/tales/retry-update", {"tale_id": tid, "n": "1"}, expect=400)
    client.post("/v1/tales/retry-update", {"tale_id": tid, "n": 7}, expect=404)
    other = client.post("/v1/tales", {})["current"]
    assert other["id"] != tid
    assert client.post("/v1/tales/open", {"tale_id": tid})["current"]["id"] == tid
    client.post("/v1/tales/open", {"tale_id": "nope"}, expect=404)
    client.post("/v1/tales/open", {}, expect=400)
    # the model's start can't be read: 502; no writing model: 503
    model.start = "junk"
    client.post("/v1/tales", {"seed": "x"}, expect=502)
    app.pages.text = None
    client.post("/v1/tales", {"seed": "x"}, expect=503)
    client.post("/v1/tales/next", {"pages": [nid]}, expect=503)
    # the reader reset keeps tales (Ruling B)
    client.post("/v1/reader/reset", {})
    assert len(client.get("/v1/tales")["tales"]) == 2


def test_old_tale_chapters_table_gets_the_update_column(tmp_path):
    import sqlite3

    from primer.graph import Graph
    db = sqlite3.connect(str(tmp_path / "old.sqlite"))
    db.execute("CREATE TABLE tale_chapters(tale_id TEXT NOT NULL, n INTEGER NOT NULL, "
               "story_id TEXT NOT NULL, pages_json TEXT NOT NULL, summary TEXT NOT NULL, "
               "created_at REAL NOT NULL, PRIMARY KEY(tale_id, n))")
    db.execute("INSERT INTO tale_chapters VALUES('t', 1, 's', '[]', 'sum', 1.0)")
    db.commit()
    db.close()
    g = Graph(tmp_path / "old.sqlite")
    assert g.tale_chapters("t")[0]["bible_update"] == "done"  # chapters from before: merged
    assert g.add_tale_chapter("t", "s2", [], "") == 2
    assert g.tale_chapters("t")[1]["bible_update"] == "pending"
    assert g.chapter_for_story("s2") == {"tale_id": "t", "n": 2} and g.chapter_for_story("x") is None
    g.replace_chapter_story("s2", "s3")
    assert g.chapter_for_story("s3") == {"tale_id": "t", "n": 2}
    with pytest.raises(ValueError):
        g.set_chapter_update("t", 2, "weird")


def test_chapter_edges(g, world, monkeypatch):
    from primer.stories.service import SHUT_DOWN
    tales, model, stories, _ = world()
    tale = _started(tales, g, at=100.0)
    tid = tale["id"]
    a = _page(g, "Trebuchet")
    _read(g, a, 110.0)

    # 5: an empty pick list is the automatic pick; more than 20 picks is refused
    with pytest.raises(ValueError):
        tales.next_chapter(["x"] * 21)
    # 1: the chapter just added is never shown as a failed update
    seen = []
    real_add = g.add_tale_chapter

    def add(*a, **k):
        n = real_add(*a, **k)
        seen.append(tales.current()["chapters"][-1]["update_failed"])
        return n
    monkeypatch.setattr(g, "add_tale_chapter", add)
    hold = threading.Event()
    model.update = lambda m: (hold.wait(5), UPDATE)[1]
    out = tales.next_chapter([])
    assert out["n"] == 1 and seen == [False]
    assert tales.current()["chapters"][0]["update_failed"] is False
    hold.set()
    assert tales.wait(5)
    monkeypatch.setattr(g, "add_tale_chapter", real_add)

    # 2: finished update threads are pruned as new ones start
    model.update = UPDATE
    b = _page(g, "Siege")
    _read(g, b, g.tale_chapters(tid)[-1]["created_at"] + 1)
    tales.next_chapter()
    for t in list(tales._threads):
        t.join(5)
    _read(g, _page(g, "Moat"), g.tale_chapters(tid)[-1]["created_at"] + 1)
    tales.next_chapter()
    assert len(tales._threads) == 1
    assert tales.wait(5)

    # 4: Primer shutting down is a conflict; any other refusal is a bad request
    _read(g, _page(g, "Wall"), g.tale_chapters(tid)[-1]["created_at"] + 1)
    count = len(g.tale_chapters(tid))
    for err, kind in ((SHUT_DOWN, TaleConflict), ("a chapter needs scenes", ValueError)):
        def refuse(*a, err=err, **k):
            raise ValueError(err)
        monkeypatch.setattr(stories, "create_chapter", refuse)
        with pytest.raises(kind, match=err) as info:
            tales.next_chapter()
        assert kind is ValueError or not isinstance(info.value, ValueError)
    assert len(g.tale_chapters(tid)) == count and tales.current()["writing"] is False

    # 6: a chapter whose story has no scenes can't have its update retried
    sid = g.add_story(a, 1, "chapter", "")
    n = g.add_tale_chapter(tid, sid, [a], "")
    g.set_chapter_update(tid, n, "failed")
    with pytest.raises(TaleConflict, match="the chapter has no scenes yet"):
        tales.retry_update(tid, n)


def test_short_chapter_gets_one_more_try(g, world):
    tales, model, stories, _ = world()
    tale = _started(tales, g, at=100.0)
    two = "## One\nNell sets out.\n\n## Two\nNell comes home."
    replies = iter([two, SCENES])
    model.chapter = lambda m: next(replies)
    _read(g, _page(g, "Trebuchet"), 110.0)
    out = tales.next_chapter()
    assert len(model.of("chapter")) == 2
    assert [s["title"] for s in stories.get(out["story_id"])["scenes"]] == ["The Siege", "Pip Helps", "Home"]
    assert tales.wait(5)
    # a second short answer (or none) keeps the first: a short chapter is still a chapter
    replies = iter([two, "   "])
    _read(g, _page(g, "Siege"), g.tale_chapters(tale["id"])[-1]["created_at"] + 1)
    out = tales.next_chapter()
    assert [s["title"] for s in stories.get(out["story_id"])["scenes"]] == ["One", "Two"]
    assert tales.wait(5)


def test_the_wish_keeps_who_the_hero_is(g, world):
    """Live L1/L2: "a girl called Wren" stays a girl: the start asks for a gender (female or male)
    and to keep the wish's hero, the bible keeps it, and the chapter and update prompts quote the
    wish and state it; the update keeps things to objects the characters carry or own."""
    tales, model, stories, _ = world()
    model.start = json.dumps({"title": "Wren", "hero": {
        "name": "Wren", "gender": "female", "description": "A girl who mends clocks.",
        "look": "16 years old, slender, a satchel of tools"}})
    tale = tales.start("a girl called Wren who mends clocks")
    system = model.of("start")[0][0]["content"]
    assert '"gender"' in system and "female or male" in system and "wish" in system
    assert "pronoun" not in system
    assert tale["bible"]["hero"]["gender"] == "female"
    with g._lock, g._db:
        g._db.execute("UPDATE tales SET created_at=100.0 WHERE id=?", (tale["id"],))
    _read(g, _page(g, "Water clock"), 110.0)
    out = tales.next_chapter()
    assert tales.wait(5)
    _wait_story(stories, out["story_id"])
    system, user = (m["content"] for m in model.of("chapter")[0])
    assert "<wish>" in user and "a girl called Wren who mends clocks" in user
    assert "Hero: Wren, female. A girl who mends clocks." in user and "female hero is a girl" in system
    assert "pronoun" not in system
    upd_system, upd = (m["content"] for m in model.of("update")[0])
    assert "<wish>" in upd and "a girl called Wren" in upd and "Hero: Wren, female" in upd
    assert "objects a character carries, owns" in upd_system and "never ideas" in upd_system
    assert "places in the story" in upd_system
    # the reader can set the gender too: female, male or unset
    assert tales.edit(tale["id"], {"gender": "male"})["bible"]["hero"]["gender"] == "male"
    assert tales.edit(tale["id"], {"gender": ""})["bible"]["hero"]["gender"] == ""
    with pytest.raises(ValueError):
        tales.edit(tale["id"], {"gender": "they/them"})


def test_next_chapter_needs_the_voice(g, world):
    """A chapter is narrated: with the voice unavailable Next chapter is a 409 with the reason,
    before any model call (like Story)."""
    tales, model, _, _ = world()
    _started(tales, g, at=100.0)
    _read(g, _page(g, "Trebuchet"), 110.0)
    tales.voice = lambda: {"ok": False, "reason": "voice server isn't running"}
    calls = len(model.calls)
    with pytest.raises(TaleConflict, match="voice server isn't running"):
        tales.next_chapter()
    assert len(model.calls) == calls and tales.current()["writing"] is False
    assert tales.current()["chapters"] == []
    tales.voice = lambda: {"ok": True, "reason": ""}
    assert tales.next_chapter()["n"] == 1 and tales.wait(5)
