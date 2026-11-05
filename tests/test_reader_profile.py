import threading

import pytest

from primer.prompts import clean_summary, profile_summary_messages
from primer.reader.profile import NO_FAST, NOTES_CHARS, RESET_HOOK_FAILED, RESETTING, Profile
from primer.reader.signals import Signals, counted_block
from tests.fakes import FakeEngine

SUMMARY = "Likes concrete examples and follows sideways jumps. Knows a lot about castles."


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _opens(g, sig, n, title="Castles", via="sideways"):
    nid = g.node_for(title, "concept").id
    for _ in range(n):
        sig.record("open", nid, via)
    return nid


@pytest.fixture
def parts(g):
    sig = Signals(g)
    fast = FakeEngine([SUMMARY])
    clock = Clock()
    busy = {"pages": False}
    prof = Profile(g, sig, fast, clock=clock, pages_busy=lambda: busy["pages"])
    yield prof, sig, fast, clock, busy
    prof.shutdown()


def test_summary_refresh_thresholds(g, parts):
    prof, sig, fast, clock, busy = parts
    _opens(g, sig, 7)
    assert prof.nudge() is False and fast.calls == []  # 7 new opens: not yet
    _opens(g, sig, 1)
    busy["pages"] = True
    assert prof.nudge() is False and fast.calls == []  # the pages are using the fast engine
    busy["pages"] = False
    assert prof.nudge() is True
    assert prof.wait(5)
    assert len(fast.calls) == 1 and prof.state()["summary"] == SUMMARY
    assert prof.state()["updated_at"] is not None and prof.state()["writing"] is False
    _opens(g, sig, 8, "Moats")
    clock.t += 60
    assert prof.nudge() is False  # at most every 120 s
    clock.t += 61
    assert prof.nudge() is True and prof.wait(5) and len(fast.calls) == 2
    clock.t += 200
    assert prof.nudge() is False  # nothing new since the last summary
    nid = g.node_for("Castles", "concept").id
    for _ in range(4):
        sig.record("answer", nid, "right")
    assert prof.nudge() is False
    sig.record("answer", nid, "not_yet")  # 5 quiz answers
    assert prof.nudge() is True and prof.wait(5) and len(fast.calls) == 3
    # the summary prompt: low temperature, short, and the previous summary goes in
    assert SUMMARY in fast.calls[2][1]["content"]
    prof.set_enabled(False)
    _opens(g, sig, 8, "Keeps")
    clock.t += 200
    assert prof.nudge() is False  # off: no background summaries


def test_no_fast_engine_says_why(g):
    sig = Signals(g)
    prof = Profile(g, sig, lambda: None)
    _opens(g, sig, 8)
    assert prof.nudge() is False
    assert prof.refresh() == NO_FAST == "no fast model in this tier"
    assert prof.state()["fast"] == {"ok": False, "reason": NO_FAST}


def test_fast_engine_read_at_call_time(g):
    """A tier switch builds a new fast engine: the profile uses whichever is current."""
    sig = Signals(g)
    old, new = FakeEngine(["old"]), FakeEngine([SUMMARY])
    current = {"fast": old}
    prof = Profile(g, sig, lambda: current["fast"])
    _opens(g, sig, 8)
    current["fast"] = new
    assert prof.refresh() == "" and prof.wait(5)
    assert old.calls == [] and len(new.calls) == 1 and prof.state()["summary"] == SUMMARY


def test_refresh_needs_something_to_go_on(g, parts):
    prof, sig, fast, *_ = parts
    assert prof.refresh() == "read a few pages first" and fast.calls == []
    _opens(g, sig, 3)
    assert prof.refresh() == "" and prof.wait(5) and len(fast.calls) == 1


def test_block_order_and_bound(g, parts):
    prof, sig, fast, *_ = parts
    nid = _opens(g, sig, 8)
    sig.record("depth", nid, "simpler")
    sig.record("depth", nid, "simpler")
    assert prof.block() == counted_block(sig.counted())  # no notes, no summary yet
    prof.set_notes("I'm twelve; skip the maths.")
    block = prof.block()
    assert block.startswith("In the reader's own words: I'm twelve; skip the maths.")
    assert counted_block(sig.counted()) in block
    assert prof.refresh() == "" and prof.wait(5)
    block = prof.block()
    notes_at, summary_at = block.index("I'm twelve"), block.index(SUMMARY)
    assert notes_at < summary_at
    # counted sentences the summary already covers are left out (it mentions sideways jumps
    # and castles); the simpler lean it doesn't mention comes after it
    assert "Follows sideways jumps most" not in block and "Reads most about" not in block
    assert block.index("Asks for simpler pages") > summary_at
    prof.set_notes("word " * 199 + "end")  # 999 chars
    long = prof.block()
    assert len(long) <= 800 and long.startswith("In the reader's own words: word word")
    with pytest.raises(ValueError):
        prof.set_notes("x" * (NOTES_CHARS + 1))
    with pytest.raises(ValueError):
        prof.set_notes(5)


def test_disabled_block_empty(g, parts):
    prof, sig, *_ = parts
    _opens(g, sig, 8)
    prof.set_notes("Skip the maths.")
    assert prof.block()
    prof.set_enabled(False)
    assert prof.block() == "" and prof.state()["enabled"] is False
    again = Profile(g, sig, FakeEngine([SUMMARY]))  # remembered
    assert again.block() == "" and again.state()["notes"] == "Skip the maths."
    again.set_enabled(True)
    assert Profile(g, sig, None).block().startswith("In the reader's own words: Skip the maths.")
    with pytest.raises(ValueError):
        prof.set_enabled("yes")


def test_reset_discards_running_summary(g):
    """Review Focus 4: a reset while the summary is being written discards the late summary."""
    entered, release = threading.Event(), threading.Event()

    class Blocked(FakeEngine):
        def complete(self, messages, **kw):
            self.calls.append(messages)
            entered.set()
            release.wait(5)
            return "Remembers everything from before the reset."

    sig = Signals(g)
    fast = Blocked([])
    prof = Profile(g, sig, fast)
    hooks = []
    prof.on_reset.append(lambda: hooks.append("quiz"))
    nid = _opens(g, sig, 8)
    sig.dwell(nid, 30)
    prof.set_notes("Skip the maths.")
    assert prof.nudge() is True and entered.wait(5)
    assert prof.state()["writing"] is True
    assert prof.refresh() == "already writing"
    before = prof.state()["generation"]
    prof.reset()
    release.set()
    assert prof.wait(5)
    st = prof.state()
    assert st["summary"] == "" and st["notes"] == "" and st["updated_at"] is None
    assert st["generation"] == before + 1 and st["counted"]["opens"] == 0
    assert st["counted"]["topics"] == [] and hooks == ["quiz"]
    assert sig.since("open", 0) == 0 and prof.block() == ""
    assert sig.dwell(nid, 10) == 10  # the visit was forgotten too
    fresh = Profile(g, sig, None)  # and nothing old comes back from the database
    assert fresh.state()["summary"] == "" and fresh.state()["generation"] == before + 1
    assert g.node_for("Castles", "concept").title == "Castles"  # pages are kept


def test_profile_persists(g, parts):
    prof, sig, *_ = parts
    _opens(g, sig, 8)
    prof.set_notes("Likes ships.")
    assert prof.refresh() == "" and prof.wait(5)
    again = Profile(g, sig, None)
    st = again.state()
    assert st["notes"] == "Likes ships." and st["summary"] == SUMMARY and st["enabled"] is True
    assert set(st) >= {"summary", "notes", "enabled", "counted", "knows", "hard", "updated_at",
                       "writing", "fast", "generation"}


def test_quiz_facts_feed_state_and_prompt(g):
    sig = Signals(g)
    fast = FakeEngine([SUMMARY])
    prof = Profile(g, sig, fast, quiz_facts=lambda: (["Trebuchet"], ["Siege dates"]))
    _opens(g, sig, 8)
    st = prof.state()
    assert st["knows"] == ["Trebuchet"] and st["hard"] == ["Siege dates"]
    assert prof.refresh() == "" and prof.wait(5)
    user = fast.calls[0][1]["content"]
    assert "Trebuchet" in user and "Siege dates" in user


def test_summary_prompt_and_cleaning():
    counted = {"opens": 9, "level": {"deeper": 0, "simpler": 2, "lean": "simpler"},
               "style": {"context": 0.1, "general": 0.1, "sideways": 0.8, "branches": 9, "lean": "sideways"},
               "topics": [{"title": "Castles", "opens": 5, "seconds": 120.0}]}
    msgs = profile_summary_messages(counted, ["Moats", "Keeps <b>"], ["Trebuchet"], ["Dates"],
                                    previous="Likes castles.")
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "2" in system and "4" in system and "plain" in system.lower()
    assert "name" in system.lower() and "age" in system.lower()
    for word in ("Moats", "Castles", "Trebuchet", "Dates", "Likes castles.", "simpler", "sideways"):
        assert word in user
    assert "<b>" not in user
    raw = "**Summary:**\n<p>Likes *plain* words.</p>\n# Heading\n- follows `sideways` jumps " + "x " * 400
    clean = clean_summary(raw)
    assert "<" not in clean and "*" not in clean and "#" not in clean and "`" not in clean
    assert clean.startswith("Summary: Likes plain words.") and len(clean) <= 600
    assert clean_summary("   ") == ""


def test_reader_routes(client, app_parts):
    app, _ = app_parts
    st = client.get("/v1/reader")
    assert st["enabled"] is True and st["notes"] == "" and st["hosted"] is False
    assert st["writing"] is False and st["counted"]["opens"] == 0
    st = client.post("/v1/reader", {"notes": "Skip the maths.", "enabled": False})
    assert st["notes"] == "Skip the maths." and st["enabled"] is False
    assert app.pages.reader_block() == ""
    for bad in ({"notes": 5}, {"notes": "x" * 1001}, {"enabled": "yes"}, {"enabled": 1}):
        client.post("/v1/reader", bad, expect=400)
    client.post("/v1/reader", {"enabled": True})
    assert "Skip the maths." in app.pages.reader_block()
    assert client.post("/v1/reader/refresh", {}) == {"skipped": "read a few pages first"}
    app.pages.fast = FakeEngine([SUMMARY])
    for _ in range(3):
        app.signals.record("open", app.graph.node_for("Castles", "concept").id, "start")
    assert client.post("/v1/reader/refresh", {}, expect=202) == {"started": True}
    assert app.profile.wait(5)
    assert client.get("/v1/reader")["summary"] == SUMMARY
    st = client.post("/v1/reader/reset", {})
    assert st["summary"] == "" and st["notes"] == "" and st["counted"]["opens"] == 0


def test_reader_route_says_hosted(client, app_parts):
    from dataclasses import replace

    from primer.config import EngineSpec
    app, _ = app_parts
    app.tier = replace(app.tier, text=EngineSpec(kind="openai", base_url="https://api.example.com/v1",
                                                 model="m"))
    st = client.get("/v1/reader")
    assert st["hosted"] is True and st["hosted_models"] == ["writing"]
    hosted = EngineSpec(kind="openai", base_url="https://api.example.com/v1", model="m")
    app.tier = replace(app.tier, fast=hosted)
    assert client.get("/v1/reader")["hosted_models"] == ["writing", "fast"]
    local = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="m")
    app.tier = replace(app.tier, text=local)
    assert client.get("/v1/reader")["hosted_models"] == ["fast"]
    app.tier = replace(app.tier, fast=local)
    st = client.get("/v1/reader")
    assert st["hosted"] is False and st["hosted_models"] == []


def test_page_open_nudges_the_profile(client, app_parts):
    app, _ = app_parts
    nudges = []
    app.pages.nudge = lambda: nudges.append(1)
    client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})
    assert nudges  # after a plain open


def test_reset_hooks_all_run_even_if_one_fails(g):
    sig = Signals(g)
    prof = Profile(g, sig, None)
    ran = []

    def bad():
        ran.append("bad")
        raise RuntimeError("quiz table locked")

    prof.on_reset.extend([bad, lambda: ran.append("quests")])
    prof.on_reset.append(lambda: ran.append(prof.state()["generation"]))  # may call back in
    _opens(g, sig, 3)
    prof.reset()
    assert ran == ["bad", "quests", 1]
    st = prof.state()
    assert st["counted"]["opens"] == 0 and "reset" in st["error"]


def test_block_dedupe_is_word_level(g):
    sig = Signals(g)
    fast = FakeEngine(["Plain words suit this reader; follows sideways jumps; knows keeps well."])
    prof = Profile(g, sig, fast)
    for title in ("Keep", "Moats", "Keeps"):
        _opens(g, sig, 3, title)
    nid = g.node_for("Keep", "concept").id
    sig.record("depth", nid, "simpler")
    sig.record("depth", nid, "simpler")
    assert prof.refresh() == "" and prof.wait(5)
    block = prof.block()
    assert "Asks for simpler pages more than deeper ones." in block  # the level sentence stays
    assert "Follows sideways jumps most" not in block
    reads = block[block.index("Reads most about: "):]
    assert "Keep" in reads and "Moats" in reads and "Keeps" not in reads  # title by title, whole words


def test_no_summary_starts_while_reset_hooks_run(g):
    """A summary can't start between the reset and its hooks clearing their tables: it would
    be written from pre-reset quiz facts with the post-reset generation."""
    sig = Signals(g)
    fast = FakeEngine([SUMMARY])
    facts = {"knows": ["Castles"]}
    prof = Profile(g, sig, fast, quiz_facts=lambda: (facts["knows"], []))
    seen = []

    def quiz_hook():
        seen.append(prof.refresh())  # e.g. a Reader window's Refresh arriving mid-reset
        seen.append(prof.nudge())
        facts["knows"] = []
    prof.on_reset.append(quiz_hook)
    _opens(g, sig, 9)
    prof.reset()
    assert prof.wait(5)
    assert seen[0] == RESETTING and seen[1] is False
    assert fast.calls == [] and prof.state()["summary"] == ""
    _opens(g, sig, 3)
    assert prof.refresh() == "" and prof.wait(5)  # after the reset: fine again
    assert prof.state()["summary"] == SUMMARY


def test_reset_hook_error_outlives_a_summary(g):
    sig = Signals(g)
    fast = FakeEngine([SUMMARY])
    prof = Profile(g, sig, fast)
    fail = {"on": True}

    def hook():
        if fail["on"]:
            raise RuntimeError("quiz table locked")
    prof.on_reset.append(hook)
    prof.reset()
    assert prof.state()["error"] == RESET_HOOK_FAILED
    _opens(g, sig, 3)
    assert prof.refresh() == "" and prof.wait(5)
    st = prof.state()
    assert st["summary"] == SUMMARY and st["error"] == RESET_HOOK_FAILED  # still incomplete
    fail["on"] = False
    prof.reset()
    assert prof.state()["error"] == ""


def test_busy_prefetch_does_not_starve_the_summary(g, parts):
    # found live: a dozen pages queue ~300 branch cards, a quarter of an hour of CPU fast model
    prof, sig, fast, clock, busy = parts
    _opens(g, sig, 8)
    busy["pages"] = True
    assert prof.nudge() is False and fast.calls == []  # put off while cards are queued...
    clock.t += 200
    assert prof.nudge() is False
    clock.t += 101  # ...but not for more than BUSY_PATIENCE_S
    assert prof.nudge() is True and prof.wait(5) and len(fast.calls) == 1


def test_refresh_runs_while_prefetch_is_busy(g, parts):
    prof, sig, fast, clock, busy = parts
    _opens(g, sig, 3)
    busy["pages"] = True
    assert prof.refresh() == ""  # the reader asked
    assert prof.wait(5) and prof.state()["summary"] == SUMMARY


# --- the final fix wave ---

def test_style_follows_the_switch_and_reset(g):
    """Branch cards and highlights get the profile's style line: "" when it is off or reset."""
    from primer.pages import OpenRequest, PageService
    sig = Signals(g)
    prof = Profile(g, sig, None)
    _opens(g, sig, 6, via="sideways")
    hint = "This reader tends to follow sideways jumps."
    assert prof.style() == hint
    fast = FakeEngine(lambda m: "[]")
    s = PageService(g, FakeEngine(["# Moat\nA ditch round a castle."] * 3), fast, signals=sig,
                    reader_block=prof.block, reader_style=prof.style)
    try:
        list(s.open(OpenRequest("Moat", "start")))
        assert hint in str(fast.calls[-1])
        prof.set_enabled(False)
        assert prof.style() == ""
        list(s.open(OpenRequest("Ditch", "start")))
        assert hint not in str(fast.calls[-1])
        prof.set_enabled(True)
        _opens(g, sig, 6, via="sideways")
        assert prof.style() == hint
        prof.reset()
        assert prof.style() == ""
    finally:
        s.shutdown()


def test_profile_changes_call_on_change(g):
    sig = Signals(g)
    prof = Profile(g, sig, None)
    seen = []
    prof.on_change.append(lambda: seen.append("change"))
    prof.set_notes("Skip the maths.")
    prof.set_notes("Skip the maths.")  # unchanged: nothing to discard
    prof.set_enabled(True)  # already on
    prof.set_enabled(False)
    prof.reset()
    assert seen == ["change"] * 3


def test_refresh_honours_the_switch(g, parts):
    from primer.reader.profile import SWITCHED_OFF
    prof, sig, fast, *_ = parts
    _opens(g, sig, 8)
    prof.set_enabled(False)
    assert prof.refresh() == SWITCHED_OFF and "switched off" in SWITCHED_OFF and fast.calls == []
    prof.set_enabled(True)
    assert prof.refresh() == "" and prof.wait(5)


def test_long_notes_leave_room_for_the_summary(g):
    sig = Signals(g)
    prof = Profile(g, sig, FakeEngine([SUMMARY]))
    _opens(g, sig, 8)
    prof.set_notes("word " * 199 + "end")
    assert prof.refresh() == "" and prof.wait(5)
    block = prof.block()
    notes = block.split("\n")[0]
    assert notes.startswith("In the reader's own words: word") and len(notes) <= 500
    assert SUMMARY in block and len(block) <= 800


@pytest.mark.parametrize("summary, kept", [
    ("Reads about castles. They ask for simpler pages more than deeper ones.", False),
    ("Reads about castles and moats.", True),  # says nothing of the level: the count stays
    ("Prefers detailed pages.", True),
])
def test_level_sentence_only_when_the_summary_omits_it(g, summary, kept):
    sig = Signals(g)
    prof = Profile(g, sig, FakeEngine([summary]))
    nid = _opens(g, sig, 8)
    sig.record("depth", nid, "simpler")
    sig.record("depth", nid, "simpler")
    assert prof.refresh() == "" and prof.wait(5)
    block = prof.block()
    assert block.count("simpler pages more than deeper") == (2 if kept and "simpler" in summary else 1)
    assert ("Asks for simpler pages more than deeper ones." in block) is kept


def test_summary_prompt_states_the_leans_as_facts():
    counted = {"opens": 9, "level": {"deeper": 0, "simpler": 3, "lean": "simpler"},
               "style": {"context": 0.1, "general": 0.1, "sideways": 0.8, "branches": 9, "lean": "sideways"},
               "topics": []}
    system, user = (m["content"] for m in profile_summary_messages(counted, [], [], []))
    assert "never" in system and "contradict" in system
    assert "They ask for simpler pages more than deeper ones." in user
    assert "They follow sideways jumps most." in user
    even = {**counted, "level": {"deeper": 1, "simpler": 1, "lean": "even"},
            "style": {**counted["style"], "lean": "even"}}
    assert "Facts" not in profile_summary_messages(even, [], [], [])[1]["content"]


def test_reset_is_one_transaction_and_hooks_only_forget(g):
    """Signals, dwell, profile, quiz items/answers and quests go together; the hooks run first
    (in-memory only) and see the rows still there."""
    sig = Signals(g)
    prof = Profile(g, sig, None)
    nid = _opens(g, sig, 3)
    sig.dwell(nid, 20)
    prof.set_notes("Skip the maths.")
    item = {"id": "q1", "node_id": nid, "version_n": 1, "kind": "recall", "question": "Q?",
            "answer": "A.", "created_at": 1.0, "due_at": 2.0, "step": 0, "last_mark": None,
            "misses": 0}
    g.add_quiz_items([item])
    g.mark_quiz_item("q1", 3.0, "a", "right", "ok", 9.0, 1, 0)
    g.add_quest("x", 1.0, "active", "[]", 0, 0)
    seen = []
    prof.on_reset.append(lambda: seen.append((g.get_quiz_item("q1") is not None,
                                              g.get_quest("x") is not None)))
    prof.reset()
    assert seen == [(True, True)]
    assert g.get_quiz_item("q1") is None and g.quiz_answers("q1") == [] and g.get_quest("x") is None
    assert sig.since("open", 0) == 0 and g.reader_topics() == []
    assert g.reader_profile() == {"enabled": "1", "generation": "1"}
