import threading
import time

import pytest

from primer.stories import service as service_mod
from primer.stories.service import StoryService
from primer.voice.client import VoiceError
from tests.fake_tts_server import make_wav
from tests.fakes import FakeEngine

PAGE = "# Castles\n\nKenilworth Castle was besieged in 1266. It fell after six months."
STORY = ("## The Siege\nThe castle stood proud in 1266.\n\n## The Hunger\nSix months passed slowly.\n\n"
         "## The End\nThe gates opened at last.")


class FakeVoice:
    def __init__(self, fail_on=None):
        self.spoken = []
        self.fail_on = fail_on  # fail from the Nth call on (1-based)

    def speak(self, text):
        self.spoken.append(text)
        if self.fail_on and len(self.spoken) >= self.fail_on:
            raise VoiceError("voice server is down")
        return make_wav(2400 + 100 * len(text))


class FakeImages:
    def __init__(self, ok=True, boom=False):
        self.ok, self.boom, self.creates = ok, boom, []

    def available(self, kind="image"):
        return (True, "") if self.ok else (False, "no image model set up")

    def create(self, node_id, phrase, subject, **kw):
        if self.boom:
            raise ValueError("no")
        self.creates.append((node_id, phrase, subject))
        n = len(self.creates)

        class M:
            id = f"media{n}"
        return M()


@pytest.fixture
def node(g):
    n = g.node_for("Castles", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    return n


@pytest.fixture
def made():
    svcs = []

    def make(g, tmp_path, text=None, fast=None, voice=None, images=None, gate=None):
        svc = StoryService(g, text or FakeEngine([STORY]), fast or FakeEngine(['["castle"]']),
                           lambda: voice or FakeVoice(), images, gate, tmp_path / "media",
                           voice_name="female")
        svcs.append(svc)
        return svc
    yield make
    for s in svcs:
        s.shutdown()


def wait(svc, sid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = svc.get(sid)
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.01)
    raise AssertionError(svc.get(sid))


def test_story_writes_narrates_and_illustrates(g, node, tmp_path, made):
    images, voice = FakeImages(), FakeVoice()
    svc = made(g, tmp_path, voice=voice, images=images)
    s = wait(svc, svc.create(node.id))
    assert s["status"] == "done" and s["kind"] == "story" and s["voice"] == "female"
    assert s["highlights"] == ["castle"]
    assert [sc["title"] for sc in s["scenes"]] == ["The Siege", "The Hunger", "The End"]
    assert [i[1] for i in images.creates] == ["The Siege", "The Hunger", "The End"]
    assert all(c[0] == node.id for c in images.creates)
    for i, sc in enumerate(s["scenes"]):
        assert sc["status"] == "done" and sc["media_id"] == f"media{i + 1}"
        assert (tmp_path / "media" / sc["audio"]).is_file() and sc["audio"].endswith(".wav")
        a, b, t0, t1 = sc["chunks"][0]
        assert t0 == 0 and t1 > 0 and sc["spoken"][a:b] in voice.spoken  # ranges index the spoken text
    assert "twelve sixty-six" in s["scenes"][0]["spoken"]  # numbers spelled out for the voice
    assert "1266" in s["scenes"][0]["text"]  # the original text is kept
    assert svc.get("nope") is None


def test_listen_is_one_scene_no_pictures(g, node, tmp_path, made):
    images, text = FakeImages(), FakeEngine([STORY])
    svc = made(g, tmp_path, text=text, images=images)
    s = wait(svc, svc.create(node.id, kind="listen"))
    assert s["status"] == "done" and s["kind"] == "listen" and len(s["scenes"]) == 1
    assert s["scenes"][0]["text"] == PAGE and s["scenes"][0]["status"] == "done"
    assert s["scenes"][0]["title"] == ""
    assert images.creates == [] and text.calls == [] and s["scenes"][0]["media_id"] is None
    with pytest.raises(ValueError):
        svc.create(node.id, kind="weird")
    with pytest.raises(ValueError):
        svc.create("nope")


def test_story_voice_down_mid_story(g, node, tmp_path, made):
    voice = FakeVoice(fail_on=2)
    svc = made(g, tmp_path, voice=voice)
    s = wait(svc, svc.create(node.id))
    assert s["status"] == "failed" and "voice server is down" in s["error"]
    assert [sc["status"] for sc in s["scenes"]] == ["done", "failed", "waiting"]
    assert "voice server is down" in s["scenes"][1]["error"]
    assert (tmp_path / "media" / s["scenes"][0]["audio"]).is_file()  # the finished scene stays playable
    assert len(voice.spoken) == 2  # the third scene was never attempted


def test_story_without_pictures(g, node, tmp_path, made):
    for images in (None, FakeImages(ok=False), FakeImages(boom=True)):  # boom: a failed request is only logged
        svc = made(g, tmp_path, images=images)
        s = wait(svc, svc.create(node.id))
        assert s["status"] == "done" and s["error"] is None
        assert all(sc["media_id"] is None and sc["error"] is None and sc["status"] == "done"
                   for sc in s["scenes"])


def test_story_text_engine_failure_fails(g, node, tmp_path, made):
    svc = made(g, tmp_path, text=FakeEngine([STORY], fail_after=2))
    s = wait(svc, svc.create(node.id))
    assert s["status"] == "failed" and "fake engine failure" in s["error"] and s["scenes"] == []
    svc2 = made(g, tmp_path, text=FakeEngine(["   "]))
    s = wait(svc2, svc2.create(node.id))
    assert s["status"] == "failed" and s["error"]


def test_highlights_failure_is_not_fatal(g, node, tmp_path, made):
    class Boom(FakeEngine):
        def complete(self, *a, **k):
            raise RuntimeError("fast down")
    svc = made(g, tmp_path, fast=Boom([""]))
    s = wait(svc, svc.create(node.id))
    assert s["status"] == "done" and s["highlights"] == []


def test_stories_restart_marks_failed(g, node):
    a = g.add_story(node.id, 1, "story", "female")
    b = g.add_story(node.id, 1, "story", "female")
    g.set_scenes(b, [{"title": "x", "text": "y"}, {"title": "z", "text": "w"}])
    g.update_scene(b, 0, status="narrating")
    g.update_story(b, status="narrating")
    c = g.add_story(node.id, 1, "listen", "female")
    g.update_story(c, status="done")
    assert g.fail_unfinished_stories("Primer restarted") == 2
    assert g.get_story(a)["status"] == "failed" and g.get_story(a)["error"] == "Primer restarted"
    gb = g.get_story(b)
    assert gb["status"] == "failed" and gb["scenes"][0]["status"] == "failed"
    assert gb["scenes"][1]["status"] == "waiting"
    assert g.get_story(c)["status"] == "done"
    assert g.fail_unfinished_stories("again") == 0


def test_again_new_story(g, node, tmp_path, made):
    svc = made(g, tmp_path)
    first = wait(svc, svc.create(node.id))
    second_id = svc.again(first["id"])
    assert second_id != first["id"]
    second = wait(svc, second_id)
    assert second["status"] == "done" and second["kind"] == "story"
    assert [x["id"] for x in g.stories_for(node.id)] == [second_id, first["id"]]
    assert "scenes" not in g.stories_for(node.id)[0]
    with pytest.raises(ValueError):
        svc.again("nope")


def test_shutdown_fails_pending_and_refuses_new(g, node, tmp_path):
    release = threading.Event()

    class Slow(FakeEngine):
        def stream(self, *a, **k):
            release.wait(5)
            yield STORY
    svc = StoryService(g, Slow([""]), FakeEngine(["[]"]), lambda: FakeVoice(), None, None, tmp_path / "m")
    sid = svc.create(node.id)
    svc.shutdown()
    release.set()
    time.sleep(0.2)
    s = svc.get(sid)
    assert s["status"] == "failed" and s["error"] == service_mod.SHUT_DOWN
    with pytest.raises(ValueError):
        svc.create(node.id)


def test_graph_story_guards(g, node):
    sid = g.add_story(node.id, 1, "story", "v")
    g.set_scenes(sid, [{"title": "a", "text": "b"}])
    g.update_story(sid, status="failed", error="why")
    g.update_story(sid, status="done")
    assert g.get_story(sid)["status"] == "failed" and g.get_story(sid)["error"] == "why"
    g.update_scene(sid, 0, status="done", audio="x.wav", chunks=[[0, 1, 0.0, 0.5]])  # a late writer
    assert g.get_story(sid)["scenes"][0]["status"] == "waiting"
    s2 = g.add_story(node.id, 1, "story", "v")
    g.set_scenes(s2, [{"title": "a", "text": "b"}])
    g.update_scene(s2, 0, status="done", audio="x.wav", chunks=[[0, 1, 0.0, 0.5]])
    g.update_scene(s2, 0, status="failed", error="late")
    sc = g.get_story(s2)["scenes"][0]
    assert sc["status"] == "done" and sc["chunks"] == [[0, 1, 0.0, 0.5]]
    g.update_story(s2, status="done")
    g.update_story(s2, status="failed", error="late")
    assert g.get_story(s2)["status"] == "done"
    with pytest.raises(ValueError):
        g.update_story(s2, status="weird")
    with pytest.raises(ValueError):
        g.update_scene(s2, 0, bogus=1)
    with pytest.raises(ValueError):
        g.update_story("nope", status="done")


def test_highlights_come_before_pictures(g, node, tmp_path, made):
    state = {"queued": False}

    class Images(FakeImages):
        def create(self, *a, **k):
            state["queued"] = True
            return super().create(*a, **k)

    class Fast(FakeEngine):
        def complete(self, *a, **k):
            if state["queued"]:
                raise RuntimeError("fast server paused")
            return '["castle"]'
    svc = made(g, tmp_path, fast=Fast([""]), images=Images())
    s = wait(svc, svc.create(node.id))
    assert s["highlights"] == ["castle"] and s["scenes"][0]["media_id"] == "media1"


def test_highlights_line_up_with_spoken_text(g, node, tmp_path, made):
    svc = made(g, tmp_path, fast=FakeEngine(['["twelve sixty-six"]']))
    s = wait(svc, svc.create(node.id))
    assert s["highlights"] == ["twelve sixty-six"]


def test_listen_splits_into_early_playable_parts(g, tmp_path, made):
    n = g.node_for("Long", "concept")
    paras = [f"Paragraph {i} " + " ".join(["word"] * 40) for i in range(8)]  # ~230 chars each
    g.add_version(n.id, "\n\n".join(paras), "storyteller", "normal")
    svc = made(g, tmp_path)
    s = wait(svc, svc.create(n.id, kind="listen"))
    assert s["status"] == "done" and 2 <= len(s["scenes"]) <= 4
    assert all(sc["title"] == "" and sc["status"] == "done" for sc in s["scenes"])
    assert all(len(sc["spoken"]) <= 700 for sc in s["scenes"])
    assert "\n\n".join(sc["text"] for sc in s["scenes"]) == "\n\n".join(paras)


def test_empty_spoken_scene_is_done_without_audio(g, node, tmp_path, made):
    text = FakeEngine(["## A\n[1]\n\n## B\nSomething to say."])
    voice = FakeVoice()
    svc = made(g, tmp_path, text=text, voice=voice)
    s = wait(svc, svc.create(node.id))
    assert s["status"] == "done"
    assert s["scenes"][0]["status"] == "done" and s["scenes"][0]["audio"] is None
    assert s["scenes"][1]["audio"] and len(voice.spoken) == 1


def test_voice_factory_failure_fails_clearly(g, node, tmp_path):
    def boom():
        raise RuntimeError("not configured")
    svc = StoryService(g, FakeEngine([STORY]), FakeEngine(["[]"]), boom, None, None, tmp_path / "m")
    try:
        s = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert s["status"] == "failed" and s["error"].startswith("The voice server isn't available: ")
    assert all(sc["status"] == "waiting" for sc in s["scenes"])


def test_closed_service_stops_speaking_between_chunks(g, node, tmp_path):
    svc = StoryService(g, FakeEngine([STORY]), FakeEngine(["[]"]), lambda: FakeVoice(), None, None, tmp_path / "m")
    calls = []

    class Voice:
        def speak(self, text):
            calls.append(text)
            svc.shutdown()
            return make_wav(2400)
    with pytest.raises(service_mod.Closed):
        svc._speak(Voice(), "One sentence here. Another sentence there. And a third one. " * 12)
    assert len(calls) == 1


def test_gate_order_with_a_real_gate(g, node, tmp_path):
    from primer.gpu import GpuGate
    log = []
    gate = GpuGate("auto", lambda: log.append("pause"), lambda: log.append("resume"))
    gate.image().__enter__()  # an image holds the gate and has paused the text
    gate._release("image")
    assert gate.state()["text_paused"]
    log.clear()

    class Text(FakeEngine):
        def stream(self, *a, **k):
            log.append("stream")
            yield from super().stream(*a, **k)

    class Images(FakeImages):
        def create(self, *a, **k):
            log.append(("create", gate.state()["holder"]))
            return super().create(*a, **k)
    svc = StoryService(g, Text([STORY]), FakeEngine(["[]"]), lambda: FakeVoice(), Images(), gate, tmp_path / "m")
    try:
        wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert log[:2] == ["resume", "stream"]
    assert [e for e in log if isinstance(e, tuple)][0] == ("create", None)  # the page turn was released


class KwImages(FakeImages):
    def __init__(self):
        super().__init__()
        self.kws = []

    def create(self, node_id, phrase, subject, **kw):
        self.kws.append(kw)
        return super().create(node_id, phrase, subject)


def test_chapter_kind_takes_scenes_and_prefixes_pictures(g, node, tmp_path, made):
    images, text = KwImages(), FakeEngine([STORY])
    svc = made(g, tmp_path, text=text, images=images)
    scenes = [{"title": "One", "text": "Nell meets Pip at the castle."}, {"title": "Two", "text": "Nell alone."}]
    with pytest.raises(ValueError):
        svc.create(node.id, kind="chapter")  # a chapter's scenes come from the tale
    with pytest.raises(ValueError):
        svc.create_chapter("nope", 1, "Chapter 1", scenes, "")
    with pytest.raises(ValueError):
        svc.create_chapter(node.id, 1, "Chapter 1", [], "")
    sid = svc.create_chapter(node.id, 1, "Chapter 1: One", scenes,
                             lambda t: "a girl" + ("; Pip: a mouse" if "Pip" in t else ""))
    assert [s["title"] for s in g.get_story(sid)["scenes"]] == ["One", "Two"]  # stored at once
    s = wait(svc, sid)
    assert s["status"] == "done" and s["kind"] == "chapter" and s["title"] == "Chapter 1: One"
    assert text.calls == [] and s["highlights"] == ["castle"]
    assert [k.get("prefix") for k in images.kws] == ["a girl; Pip: a mouse", "a girl"]
    assert all(sc["status"] == "done" and sc["media_id"] for sc in s["scenes"])
    # a plain prefix, or none
    sid2 = svc.create_chapter(node.id, 1, "Chapter 2", scenes, "a fixed look")
    wait(svc, sid2)
    assert [k.get("prefix") for k in images.kws[2:]] == ["a fixed look", "a fixed look"]
    sid3 = svc.create_chapter(node.id, 1, "Chapter 3", scenes, "")
    wait(svc, sid3)
    assert images.kws[4:] == [{"scene": True}, {"scene": True}]  # drawn as storybook scenes
    assert all(k.get("scene") is True for k in images.kws)
    # Again redoes it from the stored scenes; the chapter_prefix hook gives the looks back
    svc.chapter_prefix = lambda story_id, text: "hooked"
    again = wait(svc, svc.again(sid))
    assert again["kind"] == "chapter" and again["title"] == "Chapter 1: One"
    assert [sc["text"] for sc in again["scenes"]] == ["Nell meets Pip at the castle.", "Nell alone."]
    assert [k.get("prefix") for k in images.kws[6:]] == ["hooked", "hooked"]
    assert text.calls == []
