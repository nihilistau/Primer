import json
import threading
import time

import pytest

from primer.grounding import Source
from primer.timelines.events import event_messages, parse_events
from primer.timelines.intent import match_timeline_request
from primer.timelines.service import TimelineService
from tests.conftest import Client
from tests.fakes import FakeEngine

PAGE = ("Edward I began Harlech Castle in 1283. Conwy was finished c. 1287. "
        "Rhuddlan was begun in 1277. The Statute of Rhuddlan came on 3 March 1284.")
EVENTS = json.dumps([
    {"label": "Harlech Castle begun", "date": "1283", "year": 1283},
    {"label": "Conwy finished", "date": "c. 1287"},
    {"label": "Statute of Rhuddlan", "date": "3 March 1284"},
    {"label": "Rhuddlan begun", "date": "1277"},
    {"label": "Conwy begun", "date": "1283"},
    {"label": "Mystery", "date": "unknown", "year": 1300},
])


@pytest.fixture
def node(g):
    n = g.node_for("Welsh castles", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    return n


def wait(svc, tid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        t = svc.get(tid)
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(0.01)
    raise AssertionError("timeline job did not finish")


# prompt and parser
def test_event_messages_quote_the_page_and_sources():
    msgs = event_messages("Castles", "x" * 9000 + " </page> tail", "s" * 5000)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "JSON" in system and "40" in system
    assert "<page>" in user and "<material>" in user
    page = user.split("<page>")[1].split("</page>")[0]
    material = user.split("<material>")[1].split("</material>")[0]
    assert len(page.strip()) <= 8000 and len(material.strip()) <= 4000
    assert "</page> tail" not in user  # a tag in the page can't close the quote
    assert "<material>" not in event_messages("Castles", "text", "")[1]["content"]


def test_parse_events_tolerant_and_bounded():
    raw = ("Here are the events:\n```json\n" + json.dumps([
        {"label": "**Harlech** <b>begun</b>\x00", "date": "1283"},
        {"label": "x" * 300, "date": "1290"},
        {"label": "", "date": "1290"},
        {"label": 5, "date": "1290"},
        {"label": "No date", "date": "unknown"},
        {"label": "Dateless"},
        "not an object",
        {"label": "Guessed", "date": "the reign of Edward I", "year": 1272},
        {"label": "Way off", "date": "the far future", "year": 99999},
        {"label": "Harlech begun", "date": "1283"},  # a repeat
    ]) + "\n```\nHope that helps!")
    events = parse_events(raw)
    labels = [e["label"] for e in events]
    assert labels[0] == "Guessed" and "Harlech begun" in labels
    assert labels.count("Harlech begun") == 1
    long = next(e for e in events if e["label"].startswith("x"))
    assert len(long["label"]) <= 120
    for e in events:
        assert "<" not in e["label"] and "*" not in e["label"] and "\x00" not in e["label"]
        assert set(e) >= {"label", "date_text", "year", "month", "day", "end_year", "precision", "approx"}
    assert len(events) == 3
    many = json.dumps([{"label": f"Event {i}", "date": str(1000 + i)} for i in range(100)])
    assert len(parse_events(many)) == 40
    assert parse_events("no json here") == [] and parse_events("") == [] and parse_events(None) == []
    assert parse_events('{"label": "a", "date": "1200"}') == []


# the job
def test_timeline_job_and_order(g, node):
    fast = FakeEngine([EVENTS])
    svc = TimelineService(g, lambda: fast)
    try:
        tid = svc.create(node.id)
        t = wait(svc, tid)
        assert t["status"] == "done" and t["error"] is None
        assert t["node_id"] == node.id and t["n"] == 1 and t["page_title"] == node.title
        got = [(e["label"], e["year"], e["month"], e["day"], e["approx"]) for e in t["events"]]
        assert got == [("Rhuddlan begun", 1277, None, None, False),
                       ("Harlech Castle begun", 1283, None, None, False),
                       ("Conwy begun", 1283, None, None, False),
                       ("Statute of Rhuddlan", 1284, 3, 3, False),
                       ("Conwy finished", 1287, None, None, True)]
        assert t["events"][3]["precision"] == "day" and t["events"][3]["date_text"] == "3 March 1284"
        assert len(fast.calls) == 1 and PAGE in fast.calls[0][1]["content"]
        assert g.timelines_for(node.id) == [{"id": tid, "node_id": node.id, "n": 1, "status": "done",
                                             "error": None, "created_at": t["created_at"],
                                             "event_count": 5}]
        again = svc.again(tid)
        assert again != tid and wait(svc, again)["status"] == "done"
        rows = svc.all_events()
        assert {r["timeline_id"] for r in rows} == {again}  # the latest finished timeline only
        assert rows[0]["node_id"] == node.id and rows[0]["page_title"] == node.title
        assert [r["label"] for r in rows][0] == "Rhuddlan begun"
        with pytest.raises(ValueError):
            svc.create("nope")
        with pytest.raises(ValueError):
            svc.again("nope")
        assert svc.get("nope") is None
    finally:
        svc.shutdown()


def test_approx_sorts_after_exact_on_a_tie(g, node):
    fast = FakeEngine([json.dumps([{"label": "B", "date": "c. 1283"}, {"label": "A", "date": "1283"}])])
    svc = TimelineService(g, lambda: fast)
    try:
        t = wait(svc, svc.create(node.id))
        assert [e["label"] for e in t["events"]] == ["A", "B"]
    finally:
        svc.shutdown()


def test_timeline_no_events(g, node):
    svc = TimelineService(g, lambda: FakeEngine(["[]"]))
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "done" and t["events"] == [] and t["error"] is None
    finally:
        svc.shutdown()


def test_timeline_unreadable_answer_or_model_failure_fails(g, node):
    class Broken:
        def complete(self, *a, **kw):
            raise RuntimeError("server down")
    for fast, want in ((FakeEngine(["I can't help with that."]), "couldn't be read"),
                       (Broken(), "server down")):
        svc = TimelineService(g, lambda: fast)
        try:
            t = wait(svc, svc.create(node.id))
            assert t["status"] == "failed" and want in t["error"]
        finally:
            svc.shutdown()
    svc = TimelineService(g, lambda: None)
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "failed" and "fast model" in t["error"]
    finally:
        svc.shutdown()


def test_grounded_page_sends_its_sources(g):
    n = g.node_for("Harlech", "concept")
    g.add_version(n.id, "Harlech Castle was begun in 1283.", "reference", "normal",
                  [Source(n=1, title="Harlech Castle", origin="reference", text="", book="wiki", path="A/H")])
    fast = FakeEngine(["[]"])
    asked = []

    def sources_text(node_id, version_n, stop=None):
        asked.append((node_id, version_n))
        return "The castle fell in 1468 after a long siege."
    svc = TimelineService(g, lambda: fast, sources_text=sources_text)
    try:
        wait(svc, svc.create(n.id))
        assert asked == [(n.id, 1)]
        user = fast.calls[0][1]["content"]
        assert user.index("Harlech Castle was begun") < user.index("fell in 1468")
    finally:
        svc.shutdown()


def test_timeline_restart(g, node):
    a = g.add_timeline(node.id, 1)
    b = g.add_timeline(node.id, 1)
    g.update_timeline(b, status="done")
    assert g.fail_unfinished_timelines("Primer restarted") == 1
    assert g.get_timeline(a)["status"] == "failed" and g.get_timeline(a)["error"] == "Primer restarted"
    assert g.get_timeline(b)["status"] == "done"
    g.update_timeline(a, status="done")  # a late writer can't revive a failed timeline
    assert g.get_timeline(a)["status"] == "failed"
    svc = TimelineService(g, lambda: FakeEngine([EVENTS]))
    try:
        again = svc.again(a)
        t = wait(svc, again)
        assert t["status"] == "done" and len(t["events"]) == 5
    finally:
        svc.shutdown()


def test_shutdown_fails_unfinished_jobs(g, node):
    gate = threading.Event()

    class Slow:
        def complete(self, *a, **kw):
            gate.wait(5)
            return EVENTS
    svc = TimelineService(g, lambda: Slow())
    tid = svc.create(node.id)
    queued = svc.create(node.id)
    svc.shutdown("Primer shut down")
    gate.set()
    time.sleep(0.1)
    for t in (tid, queued):
        got = g.get_timeline(t)
        assert got["status"] == "failed" and got["error"] == "Primer shut down"
    with pytest.raises(ValueError):
        svc.create(node.id)


# typed requests
@pytest.mark.parametrize("text, want", [
    ("timeline of the Tudors", "the Tudors"),
    ("a timeline of the Tudors", "the Tudors"),
    ("the timeline of the Tudors", "the Tudors"),
    ("Show me a timeline of the Wars of the Roses", "the Wars of the Roses"),
    ("make me a timeline of Welsh castles, please", "Welsh castles"),
    ("can you give me a timeline of the Roman Empire", "the Roman Empire"),
    ("Tudor timeline", "Tudor"),
    ("the Hundred Years' War timeline", "the Hundred Years' War"),
    ("When did the Great Fire of London happen?", "the Great Fire of London"),
    ("when did the Battle of Hastings take place", "the Battle of Hastings"),
    ("timeline for the space race.", "the space race"),
    ("the Tudors on a timeline", "the Tudors"),
    ("put the Tudors on a timeline", "the Tudors"),
    ("can you put the Wars of the Roses on a timeline?", None),  # a question about timelines
    ("please put the Wars of the Roses on a timeline", "the Wars of the Roses"),
    ("show me the Tudors as a timeline", "the Tudors"),
    ("the Tudors in a timeline", "the Tudors"),
])
def test_timeline_intent(text, want):
    assert match_timeline_request(text) == want


@pytest.mark.parametrize("text", [
    "time of day", "what time of day is it", "timeline", "a timeline", "make a timeline",
    "timeline of it", "what is a timeline?", "how did the timeline of the universe begin?",
    "when did the Romans leave Britain", "when was the Battle of Hastings", "the time of the Tudors",
    "why do timelines matter", "Tudors", "map of the Tudors", "is this a timeline?", "",
    "timelines", "show me the castle", "timeline of", "when did it happen?",
    "tell me about the Tudor timeline", "explain the Apollo timeline", "I want to see the Tudor timeline",
    "write an essay about the Tudor timeline", "me timeline", "about the Tudor timeline",
    "the long and winding history of the British monarchy timeline",
    "map of the Tudor timeline", "a picture of the Tudor timeline", "diagram of the Apollo timeline",
    "story of the Tudor timeline", "an image of the Tudor timeline", "on a timeline",
])
def test_timeline_intent_negatives(text):
    assert match_timeline_request(text) is None


# routes
def _wait_route(client, tid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"/v1/timelines/{tid}")
        if t["status"] in ("done", "failed"):
            return t
        time.sleep(0.05)
    raise AssertionError("timeline job did not finish")


def test_timeline_routes(app_parts, g):
    from primer.server import make_server

    app, _ = app_parts
    app.timelines.shutdown()
    app.timelines = TimelineService(g, lambda: FakeEngine([EVENTS]))
    n = g.node_for("Welsh castles", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = Client(server.server_address[1])
    try:
        assert client.get("/v1/status")["timeline_available"] == {"ok": True, "reason": ""}
        r = client.post("/v1/timelines", {"node_id": n.id}, expect=202)
        t = _wait_route(client, r["id"])
        assert t["status"] == "done" and len(t["events"]) == 5 and t["page_title"] == n.title
        page = client.get(f"/v1/pages/{n.id}")
        assert page["timelines"][0]["id"] == r["id"] and page["timelines"][0]["event_count"] == 5
        again = client.post(f"/v1/timelines/{r['id']}/again", {}, expect=202)
        assert again["id"] != r["id"]
        _wait_route(client, again["id"])
        every = client.get("/v1/timelines?all=1")
        assert len(every["events"]) == 5 and every["events"][0]["page_title"] == n.title
        assert {e["timeline_id"] for e in every["events"]} == {again["id"]}
        client.get("/v1/timelines", expect=400)
        client.get("/v1/timelines/nope", expect=404)
        client.post("/v1/timelines/nope/again", {}, expect=404)
        client.post("/v1/timelines", {"node_id": "nope"}, expect=404)
        client.post("/v1/timelines", {}, expect=400)
        client.post("/v1/timelines", {"node_id": 5}, expect=400)
        empty = g.node_for("Empty", "concept")
        client.post("/v1/timelines", {"node_id": empty.id}, expect=404)
        intent = client.post("/v1/intent", {"text": "show me a timeline of the Tudors"})
        assert intent["timeline"] == "the Tudors" and intent["image"] is None
        for text in ("a castle", "show me a castle on a map", "a picture of a castle", "tell me about the Tudor timeline"):
            got = client.post("/v1/intent", {"text": text})
            assert "timeline" in got and got["timeline"] is None  # the same shape in every answer
        # no fast engine: unavailable with the reason
        fast = app.pages.fast
        app.pages.fast = None
        try:
            status = client.get("/v1/status")["timeline_available"]
            assert status["ok"] is False and status["reason"]
            err = client.post("/v1/timelines", {"node_id": n.id}, expect=503)
            assert err["error"] == status["reason"]
            client.post(f"/v1/timelines/{r['id']}/again", {}, expect=503)
            # final review, item 6: a timeline phrase routes as before (a picture, or nothing)
            got = client.post("/v1/intent", {"text": "draw a timeline of the Tudors"})
            assert got["timeline"] is None and got["image"] == "a timeline of the Tudors"
            got = client.post("/v1/intent", {"text": "show me a timeline of the Tudors"})
            assert all(v is None for v in got.values())
        finally:
            app.pages.fast = fast
    finally:
        server.shutdown()
        server.server_close()
        app.timelines.shutdown()


def test_app_reads_a_grounded_pages_sources_again(app_parts, g, monkeypatch):
    from dataclasses import replace

    import primer.server as server
    from primer.config import ReferenceConfig

    app, _ = app_parts
    app.tier = replace(app.tier, reference=ReferenceConfig(base_url="http://127.0.0.1:1"))
    read = []

    class Kiwix:
        def __init__(self, url, timeout=10.0):
            pass

        def article(self, book, path):
            read.append((book, path))
            if path == "A/Bad":
                raise server.KiwixError("gone")
            return "Harlech   fell in 1468. " * 400

    monkeypatch.setattr(server, "KiwixClient", Kiwix)
    n = g.node_for("Harlech", "concept")
    g.add_version(n.id, "Harlech Castle was begun in 1283.", "reference", "normal",
                  [Source(n=1, title="Harlech Castle", origin="reference", text="", book="w", path="A/H"),
                   Source(n=2, title="Gone", origin="reference", text="", book="w", path="A/Bad"),
                   Source(n=3, title="Web", origin="web", text="", url="javascript:alert(1)")])
    text = app.page_sources_text(n.id, 1)
    assert read == [("w", "A/H"), ("w", "A/Bad")]
    assert text.startswith("[1] Harlech Castle: Harlech fell in 1468.") and len(text) <= 4000
    assert "Gone" not in text and "Web" not in text
    plain = g.node_for("Plain", "concept")
    g.add_version(plain.id, "No sources.", "storyteller", "normal")
    assert app.page_sources_text(plain.id, 1) == ""


def test_source_reread_has_a_wall_clock_budget_and_stops_when_closed(app_parts, g, monkeypatch):
    from dataclasses import replace

    import primer.server as server
    from primer.config import ReferenceConfig

    app, _ = app_parts
    app.tier = replace(app.tier, reference=ReferenceConfig(base_url="http://127.0.0.1:1"))
    now = [0.0]
    read = []

    class Kiwix:
        def __init__(self, url, timeout=10.0):
            pass

        def article(self, book, path):
            read.append(path)
            now[0] += 15.0  # each article takes 15 s
            return "Harlech fell in 1468."

    monkeypatch.setattr(server, "KiwixClient", Kiwix)
    n = g.node_for("Harlech", "concept")
    g.add_version(n.id, "Harlech Castle was begun in 1283.", "reference", "normal",
                  [Source(n=i, title=f"S{i}", origin="reference", text="", book="w", path=f"A/{i}")
                   for i in range(1, 5)])
    text = app.page_sources_text(n.id, 1, clock=lambda: now[0])
    assert read == ["A/1", "A/2"]  # 20 s budget: a third read isn't started at 30 s
    assert "S1" in text and "S2" in text
    read.clear()
    assert app.page_sources_text(n.id, 1, stop=lambda: True) == "" and read == []
    calls = []

    def sources_text(node_id, version_n, stop=None):
        calls.append(stop)
        return ""
    svc = TimelineService(g, lambda: FakeEngine(["[]"]), sources_text=sources_text)
    try:
        wait(svc, svc.create(n.id))
        assert callable(calls[0]) and calls[0]() is False
    finally:
        svc.shutdown()


CHATTY = ('The text cites [1] and [3]. If it dates nothing, reply []. Let us list them:\n'
          '[{"label": "Caernarfonshire established", "date": "1284", "year": 1284}]')


def test_parse_events_skips_citation_marks_and_quoted_empty_lists():
    """A small model thinks aloud before its answer: '[1]' citation marks and the prompt's own
    'reply []' must not be taken for the event list (seen live on LFM3 1.7B)."""
    events = parse_events(CHATTY)
    assert [(e["label"], e["year"]) for e in events] == [("Caernarfonshire established", 1284)]


def test_chatty_answer_with_events_is_done_and_truncated_answer_fails(g, node):
    svc = TimelineService(g, lambda: FakeEngine([CHATTY]))
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "done" and len(t["events"]) == 1
    finally:
        svc.shutdown()
    # ran out of tokens mid-answer: citation marks and a quoted [] are not "no events"
    cut = 'The text cites [1]. If it dates nothing, reply []. So: [{"label": "Castle beg'
    svc = TimelineService(g, lambda: FakeEngine([cut]))
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "failed" and "couldn't be read" in t["error"]
    finally:
        svc.shutdown()
    # only an answer that is just [] (fenced or not) is a finished, empty timeline: thinking
    # aloud that merely ends by quoting "reply []" is unreadable (and gets the writing model)
    for reply, status in (("```json\n[]\n```", "done"), ("Nothing here is dated [2], so reply []", "failed")):
        svc = TimelineService(g, lambda: FakeEngine([reply]))
        try:
            t = wait(svc, svc.create(node.id))
            assert t["status"] == status and t["events"] == []
        finally:
            svc.shutdown()


def test_unreadable_fast_answer_retries_once_on_the_writing_model_in_a_page_turn(g, node):
    """Ruling E (live, LFM3 1.7B): a fast model that thinks aloud past its budget gets one retry on
    the writing model, inside a GPU page turn; with no writing model it fails as before."""
    from tests.test_diagrams_service import FakeGate
    cut = 'Let me think. The text cites [1]. So: [{"label": "Castle beg'
    good = json.dumps([{"label": "Castle begun", "date": "1283", "year": 1283}])
    gate, seen = FakeGate(), []

    class Text(FakeEngine):
        def complete(self, *a, **kw):
            seen.append(gate.inside)
            return super().complete(*a, **kw)
    svc = TimelineService(g, lambda: FakeEngine([cut]), text_getter=lambda: Text([good]),
                          gate_getter=lambda: gate)
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "done" and [e["label"] for e in t["events"]] == ["Castle begun"]
        assert gate.turns == ["page"] and seen == [True]
    finally:
        svc.shutdown()
    svc = TimelineService(g, lambda: FakeEngine([cut]), text_getter=lambda: Text([cut]),
                          gate_getter=lambda: None)
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "failed" and "couldn't be read" in t["error"]
    finally:
        svc.shutdown()
    svc = TimelineService(g, lambda: FakeEngine(["[]"]), text_getter=lambda: Text([good]),
                          gate_getter=lambda: gate)
    try:
        t = wait(svc, svc.create(node.id))  # a readable empty answer is final: no retry
        assert t["status"] == "done" and t["events"] == [] and gate.turns == ["page"]
    finally:
        svc.shutdown()


def test_parse_events_flattens_an_array_of_one_event_arrays():
    """Live (LFM3 1.7B with sources): each event in its own array, [[{...}], [{...}]]; the inner
    first array alone would keep one event."""
    nested = json.dumps([[{"label": "St Albans", "date": "1455"}], [{"label": "Towton", "date": "1461"}],
                         [{"label": "Bosworth", "date": "1485"}]])
    assert [e["label"] for e in parse_events("Here: " + nested)] == ["St Albans", "Towton", "Bosworth"]


def test_parse_events_joins_a_run_of_one_event_arrays():
    """Live (LFM3 1.7B): no outer array, '[{...}],\n[{...}],\n[{...}]' then chatter quoting [1]."""
    run = ',\n'.join(json.dumps([{"label": name, "date": year}])
                     for name, year in (("St Albans", "1455"), ("Towton", "1461"), ("Bosworth", "1485")))
    events = parse_events(run + "\n\nWait, the text cites [1]. So [] is wrong.")
    assert [e["label"] for e in events] == ["St Albans", "Towton", "Bosworth"]


# the answer is the last, fullest list (final review, item 1): a small model that thinks aloud
# quotes the prompt's format line, its Harlech example or a draft before it answers
RHUDDLAN = '[{"label": "Rhuddlan begun", "date": "1277", "year": 1277}]'


@pytest.mark.parametrize("reply", [
    'The instructions say reply with [{"label": "...", "date": "...", "year": 1277}]. Let me look. '
    + RHUDDLAN,
    'Like the example [{"label": "Harlech Castle begun by Edward I", "date": "1283", "year": 1283}] '
    'I should list. Answer: ' + RHUDDLAN,
    'Like the example [{"label": "Harlech Castle begun by Edward I", "date": "1283", "year": 1283}, '
    '{"label": "Harlech Castle falls", "date": "1468", "year": 1468}] I should list. Answer: ' + RHUDDLAN,
])
def test_parse_events_skips_the_quoted_format_line_and_example(reply):
    assert [(e["label"], e["year"]) for e in parse_events(reply)] == [("Rhuddlan begun", 1277)]


def test_parse_events_takes_the_final_list_over_a_draft():
    reply = ('Thinking... the text says 1277. ' + RHUDDLAN + ' Wait, also 1282. Final: '
             '[{"label": "Rhuddlan begun", "date": "1277", "year": 1277}, '
             '{"label": "Welsh revolt", "date": "1282", "year": 1282}]')
    assert [e["label"] for e in parse_events(reply)] == ["Rhuddlan begun", "Welsh revolt"]


def test_parse_events_drops_labels_with_no_letter_or_digit():
    reply = json.dumps([{"label": "...", "date": "1277", "year": 1277},
                        {"label": " - ", "date": "1280"},
                        {"label": "Conwy begun", "date": "1283"}])
    assert [e["label"] for e in parse_events(reply)] == ["Conwy begun"]
    # an answer of placeholders only is not a finished, empty timeline
    from primer.timelines.events import read_event_list
    assert read_event_list('[{"label": "...", "date": "...", "year": 1277}]') is None


# live (LFM3 1.7B, Wars of the Roses with sources): a run of one-event arrays, then chatter that
# quotes one event again in an array; the full answer wins over the quoted repeat
LIVE_RUN = (
    '[{\n  "label": "Wars of the Roses began in 1455",\n  "date": "1455",\n  "year": 1455\n}\n],\n'
    '[{\n  "label": "First Battle of St Albans (York captures Henry VI)",\n  "date": "1455",\n'
    '  "year": 1455\n}\n],\n'
    '[{\n  "label": "Yorkists secure major victory at Towton",\n  "date": "1461",\n  "year": 1461\n}\n],\n'
    '[{\n  "label": "Edward IV crowned",\n  "date": "1461",\n  "year": 1461\n}\n],\n'
    '[{\n  "label": "Lancastrian line ends in 1471",\n  "date": "1471",\n  "year": 1471\n}\n]\n')
LIVE_LABELS = ["Wars of the Roses began in 1455", "First Battle of St Albans (York captures Henry VI)",
               "Yorkists secure major victory at Towton", "Edward IV crowned", "Lancastrian line ends in 1471"]


def test_live_run_then_chatter_quoting_one_event_keeps_the_full_answer():
    chatter = ('\nWait, need to check dates: The text says "From 1455 to 1487, a long series of conflicts '
               'known as the Wars of the Roses shook the English throne [1]." So overall period 1455-1487.\n\n'
               'Thus events with dates:\n\n'
               '[{"label": "Edward IV crowned", "date": "1461", "year": 1461}]\n'
               '- 1455: War began, York captured Henry at First Battle of St Albans [1].')
    assert [e["label"] for e in parse_events(LIVE_RUN + chatter)] == LIVE_LABELS
    # the same run closed by a stray extra ']' (seen live too)
    assert [e["label"] for e in parse_events(LIVE_RUN + "]\n")] == LIVE_LABELS


def test_live_century_answer_still_reads():
    reply = ('[{"label": "English conquest of Gwynedd", "date": "13th century", "year": 13}, '
             '{"label": "Establishment of English-style county of Caernarfonshire", "date": "1284", '
             '"year": 1284}]\n</parameter>\n}')
    events = parse_events(reply)
    assert [(e["label"], e["year"]) for e in events] == [
        ("English conquest of Gwynedd", 1250), ("Establishment of English-style county of Caernarfonshire", 1284)]


def test_parse_events_accepts_the_list_already_read():
    from primer.timelines.events import read_event_list
    items = read_event_list(CHATTY)
    assert parse_events(items) == parse_events(CHATTY)
    assert parse_events([]) == []


def test_hostile_reply_is_unreadable_fast():
    """Final review, item 2: the repair pass tried every [ ... ] pair (cubic); about 1000 '[' in a
    row hit the recursion limit."""
    from primer.timelines.events import read_event_list
    hostile = "[ 1, ]" * (200_000 // 6) + "[" * 5000
    t0 = time.perf_counter()
    assert read_event_list(hostile) is None
    assert read_event_list("[" * 5000) is None
    assert read_event_list("[ 1, ]" * 700) is None  # 4 KB took 61 s
    assert read_event_list('[{"a": 1},' * 20_000) is None  # thousands of starts, each nested deeper
    assert time.perf_counter() - t0 < 2.0
    # the bounded repair still reads a trailing comma or single quotes
    assert read_event_list("Here: [{'label': 'Conwy begun', 'date': '1283'},]") == [
        {"label": "Conwy begun", "date": "1283"}]


def test_job_reads_the_reply_once(g, node, monkeypatch):
    import primer.timelines.events as ev
    import primer.timelines.service as service
    calls = []
    real = ev.read_event_list

    def counting(raw):
        calls.append(raw)
        return real(raw)
    monkeypatch.setattr(ev, "read_event_list", counting)
    monkeypatch.setattr(service, "read_event_list", counting, raising=False)
    svc = TimelineService(g, lambda: FakeEngine([EVENTS]))
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "done" and len(t["events"]) == 5
        assert len(calls) == 1
    finally:
        svc.shutdown()


class HeldGate:
    """A page-turn gate the test holds: a job waits in page() until the test lets go."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lock.acquire()
        self.waiting = threading.Event()

    def page(self):
        gate = self

        class _Turn:
            def __enter__(self):
                gate.waiting.set()
                gate.lock.acquire()

            def __exit__(self, *exc):
                gate.lock.release()
        return _Turn()


def _shut_down_in_background(svc):
    shut = threading.Thread(target=svc.shutdown)
    shut.start()
    end = time.time() + 5
    while not svc._is_closed() and time.time() < end:
        time.sleep(0.005)
    return shut


def test_shutdown_while_the_retry_waits_for_its_page_turn(g, node):
    """Final review, item 4: shut down while the writing-model retry waits on the gate; the job
    ends with the shutdown error and the writing model is never called."""
    cut = 'Let me think. The text cites [1]. So: [{"label": "Castle beg'
    gate, called = HeldGate(), []

    def text_getter():
        called.append("read")
        return Text()

    class Text:
        def complete(self, *a, **kw):
            called.append("complete")
            raise RuntimeError("engine stopped")
    svc = TimelineService(g, lambda: FakeEngine([cut]), text_getter=text_getter, gate_getter=lambda: gate)
    tid = svc.create(node.id)
    assert gate.waiting.wait(5)
    shut = _shut_down_in_background(svc)
    gate.lock.release()
    shut.join(5)
    t = g.get_timeline(tid)
    assert t["status"] == "failed" and t["error"] == "Primer shut down"
    assert "complete" not in called


def test_shutdown_while_the_writing_model_runs_keeps_the_shutdown_error(g, node):
    cut = 'Let me think. So: [{"label": "Castle beg'
    holder = {}

    class Text:
        def complete(self, *a, **kw):
            holder["shut"] = _shut_down_in_background(holder["svc"])
            raise RuntimeError("engine stopped")
    svc = TimelineService(g, lambda: FakeEngine([cut]), text_getter=lambda: Text())
    holder["svc"] = svc
    tid = svc.create(node.id)
    end = time.time() + 5
    while "shut" not in holder and time.time() < end:
        time.sleep(0.005)
    holder["shut"].join(5)
    t = g.get_timeline(tid)
    assert t["status"] == "failed" and t["error"] == "Primer shut down"


def test_writing_model_retry_that_raises_fails_the_job(g, node):
    """Final review, item 9."""
    cut = 'Let me think. So: [{"label": "Castle beg'

    class Text:
        def complete(self, *a, **kw):
            raise RuntimeError("out of memory")
    svc = TimelineService(g, lambda: FakeEngine([cut]), text_getter=lambda: Text())
    try:
        t = wait(svc, svc.create(node.id))
        assert t["status"] == "failed"
        assert t["error"] == "Couldn't read the dates from the page: out of memory"
    finally:
        svc.shutdown()
