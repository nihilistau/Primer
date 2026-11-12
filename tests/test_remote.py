"""Kairos drives Primer (Phase 15): the hub (primer/remote.py) and the /v1/tool routes."""
import http.client
import json
import queue
import re
import socket
import threading

import pytest

from primer.activity import ActivitySink
from primer.config import read_settings
from primer.reader.signals import Signals
import time

from primer.remote import (BUSY, CLOSED, DONE_S, MAX_LISTENERS, NO_FOCUS, NOT_FINISHED, OFF,
                           PICKUP_FAILED, NoListener, RemoteBusy, RemoteHub, RemoteOff,
                           TITLE_MAX, RemoteRateLimited, WrongListener, not_open, resolve_that)

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}  # what Primer's own page sends


def _switch(client, value: bool, expect: int = 200):
    return client.post("/v1/remote-enabled", {"value": value}, expect=expect, headers=SAME_ORIGIN)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _hub(clock=None) -> RemoteHub:
    hub = RemoteHub(clock=clock or Clock())
    hub.enabled = True
    return hub


def _page(g, title: str) -> str:
    node = g.node_for(title, "concept")
    g.add_edge(None, node.id, title, "typed")
    g.add_version(node.id, f"# {title}\nText.", "storyteller", "normal")
    return node.id


def _done(hub: RemoteHub, listener) -> dict:
    cmd = listener.get(1)
    hub.finish(cmd["id"], True, "ok")
    return cmd


# --- the hub ---
def test_switch_blocks_driving(client, app_parts):
    app, _ = app_parts
    nid = _page(app.graph, "Bodiam Castle")
    assert client.get("/v1/tool/now")["enabled"] is False
    for path, body in (("/v1/tool/show", {"text": "show me Bodiam Castle"}),
                       ("/v1/tool/open", {"id": nid}),
                       ("/v1/tool/lens", {"kind": "map", "id": nid})):
        assert client.post(path, body, expect=403) == {"error": OFF}
    assert client.get("/v1/tool/now") == {"enabled": False, "listening": False, "focus": None}
    assert client.get("/v1/tool/trail")["pages"][0]["id"] == nid
    assert client.get("/v1/tool/find?q=bodiam")["pages"] == [{"id": nid, "title": "Bodiam Castle"}]
    hub = RemoteHub()
    with pytest.raises(RemoteOff):
        hub.submit("show", {"text": "x"})


def test_remote_enabled_saved_and_in_status(client, app_parts):
    app, _ = app_parts
    assert client.get("/v1/status")["remote"] == {"enabled": False, "listening": False}
    assert _switch(client, True)["remote"]["enabled"] is True
    assert read_settings(app.var_dir)["remote_enabled"] is True and app.remote.enabled
    for bad in (1, "true", None):
        _switch(client, bad, expect=400)
    assert client.get("/v1/tool/now")["enabled"] is True
    _switch(client, False)
    assert read_settings(app.var_dir)["remote_enabled"] is False


def test_most_recent_focus_gets_it():
    hub = _hub()
    a, b = hub.listen(), hub.listen()
    hub.focus(a.id, "n1", "Rome")
    hub.focus(b.id, "n2", "Bodiam Castle")
    hub.focus(a.id, "n1", "Rome")  # a gained focus again: it is the most recent
    assert hub.now() == {"listening": True, "focus": {"node_id": "n1", "title": "Rome"}}
    cid = hub.submit("show", {"text": "a map of Rome"})
    assert re.fullmatch(r"[0-9a-f]{16}", cid)
    assert b.get(0.05) is None
    cmd = a.get(1)
    assert cmd["id"] == cid and cmd["status"] == "delivered" and cmd["args"] == {"text": "a map of Rome"}
    assert hub.command(cid)["status"] == "delivered"
    hub.finish(cid, True, "Started a map of *Rome*; it will appear in Primer")
    got = hub.command(cid)
    assert got["status"] == "done" and got["result"].startswith("Started a map")
    assert set(got) == {"id", "kind", "args", "status", "result", "error", "created_at"}


def test_no_listener():
    hub = _hub()
    with pytest.raises(NoListener) as e:
        hub.submit("show", {"text": "x"})
    assert str(e.value) == not_open(8810)
    hub.listen().close()
    with pytest.raises(NoListener):
        hub.submit("show", {"text": "x"})


def test_pickup_timeout():
    clock = Clock()
    hub = _hub(clock)
    hub.listen()  # listening, but never picks anything up
    cid = hub.submit("show", {"text": "x"})
    clock.t += 9.9
    assert hub.command(cid)["status"] == "queued"
    with pytest.raises(RemoteBusy):
        hub.submit("show", {"text": "y"})
    clock.t += 0.2
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == PICKUP_FAILED
    assert hub.submit("show", {"text": "y"})  # free again


def test_pickup_timeout_checked_on_submit_and_listen():
    clock = Clock()
    hub = _hub(clock)
    stale = hub.listen()
    cid = hub.submit("show", {"text": "x"})
    clock.t += 11
    fresh = hub.listen()  # the check runs here too
    assert hub.command(cid)["error"] == PICKUP_FAILED
    assert stale.get(0.01) is None  # the failed command was taken off its queue
    hub.focus(fresh.id, "n", "N")
    cid2 = hub.submit("show", {"text": "y"})
    assert fresh.get(1)["id"] == cid2


def test_one_at_a_time_and_rate():
    clock = Clock()
    hub = _hub(clock)
    lis = hub.listen()
    cid = hub.submit("show", {"text": "one"})
    with pytest.raises(RemoteBusy) as e:
        hub.submit("show", {"text": "two"})
    assert str(e.value) == BUSY
    lis.get(1)
    with pytest.raises(RemoteBusy):
        hub.submit("show", {"text": "two"})  # delivered, not done: still busy
    hub.finish(cid, True, "ok")
    for i in range(19):
        clock.t += 1
        hub.submit("show", {"text": f"n{i}"})
        _done(hub, lis)
    with pytest.raises(RemoteRateLimited):
        hub.submit("show", {"text": "21st"})
    clock.t += 41  # the first one is now over a minute old
    hub.submit("show", {"text": "21st"})


def test_inputs_checked_in_the_hub():
    hub = _hub()
    hub.listen()
    for kind, args in (("show", {"text": "x" * 301}), ("show", {"text": "   "}), ("show", {}),
                       ("show", {"text": 3}), ("dance", {}), ("open", {}), ("open", {"id": 4}),
                       ("lens", {"kind": "browser", "id": "n"}), ("lens", {"id": "n"})):
        with pytest.raises(ValueError):
            hub.submit(kind, args)
    with pytest.raises(ValueError) as e:
        hub.submit("lens", {"kind": "map"})
    assert str(e.value) == NO_FOCUS


def test_inputs_checked(client, app_parts):
    app, _ = app_parts
    nid = _page(app.graph, "Bodiam Castle")
    _switch(client, True)
    lis = app.remote.listen()
    for path, body, status in (
            ("/v1/tool/show", {"text": "x" * 301}, 400),
            ("/v1/tool/show", {"text": "  "}, 400),
            ("/v1/tool/show", {"text": None}, 400),
            ("/v1/tool/open", {"id": "0123456789abcdef"}, 404),
            ("/v1/tool/open", {}, 400),
            ("/v1/tool/lens", {"kind": "browser", "id": nid}, 400),
            ("/v1/tool/lens", {"kind": "map", "id": "0123456789abcdef"}, 404),
            ("/v1/tool/dance", {}, 404)):
        assert client.post(path, body, expect=status)["error"], path
    assert client.post("/v1/tool/lens", {"kind": "map"}, expect=400) == {"error": NO_FOCUS}
    # a non-dict body
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    c.request("POST", "/v1/tool/show", body="[1, 2]", headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 400 and json.loads(r.read())["error"]
    c.close()
    client.get("/v1/tool/find", expect=400)
    client.get("/v1/tool/trail?limit=0", expect=400)
    client.get("/v1/tool/trail?limit=abc", expect=400)
    client.get("/v1/tool/commands/nothex", expect=404)
    client.get("/v1/tool/commands/0123456789abcdef", expect=404)
    client.post("/v1/tool/focus", {"listener": "nope", "node_id": nid, "title": "B"}, expect=404)
    client.post("/v1/tool/focus", {"listener": lis.id, "node_id": "0123456789abcdef",
                                   "title": "B"}, expect=404)
    client.post("/v1/tool/focus", {"listener": lis.id, "node_id": nid, "title": 3}, expect=400)
    # lens with no id uses the reported focus
    client.post("/v1/tool/focus", {"listener": lis.id, "node_id": nid, "title": "Bodiam Castle"})
    cmd = client.post("/v1/tool/lens", {"kind": "map"}, expect=202)["command"]
    assert cmd["args"] == {"kind": "map", "id": nid, "title": "Bodiam Castle"} and cmd["status"] == "queued"
    # one at a time over HTTP, and nothing listening
    assert client.post("/v1/tool/show", {"text": "x"}, expect=409)["error"] == BUSY
    lis.close()
    assert client.post("/v1/tool/show", {"text": "x"}, expect=409)["error"] == not_open(client.port)
    assert str(client.port) in not_open(client.port)


def test_rate_limited_route(client, app_parts):
    app, _ = app_parts
    _switch(client, True)
    lis = app.remote.listen()
    for i in range(20):
        cid = client.post("/v1/tool/show", {"text": f"n{i}"}, expect=202)["command"]["id"]
        lis.get(1)
        client.post(f"/v1/tool/commands/{cid}/done", {"ok": True, "listener": lis.id,
                                                      "result": {"title": f"n{i}", "what": "writing"}})
    assert client.post("/v1/tool/show", {"text": "more"}, expect=429)["error"]


def test_listener_close_fails_command():
    hub = _hub()
    lis = hub.listen()
    cid = hub.submit("open", {"id": "n1"})
    assert lis.get(1)["id"] == cid
    lis.close()
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == CLOSED
    assert hub.now()["listening"] is False


def test_finish_only_once():
    hub = _hub()
    lis = hub.listen()
    cid = hub.submit("show", {"text": "x"})
    lis.get(1)
    assert hub.finish(cid, False, "Maps are not available right now.") is True
    assert hub.finish(cid, True, "late") is False
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == "Maps are not available right now."
    assert hub.command("0123456789abcdef") is None


def test_claim_open():
    clock = Clock()
    hub = _hub(clock)
    lis = hub.listen()
    hub.submit("open", {"id": "n1"})
    assert hub.claim_open("n2") is False
    assert hub.claim_open("n1") is True
    assert hub.claim_open("n1") is False  # once
    _done(hub, lis)
    hub.submit("open", {"id": "n3"})
    clock.t += 61
    assert hub.claim_open("n3") is False  # too late


def test_shutdown_ends_listeners_and_fails_command():
    hub = _hub()
    lis = hub.listen()
    cid = hub.submit("show", {"text": "x"})
    hub.shutdown()
    assert lis.get(1) is None and lis.closed
    assert hub.command(cid)["status"] == "failed"
    late = hub.listen()
    assert late.closed and late.get(0.01) is None


# --- through the server ---
class _Stream:
    """Reads /v1/tool/stream on a thread: events go on a queue."""

    def __init__(self, port: int):
        self.events: queue.Queue = queue.Queue()
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.conn.request("GET", "/v1/tool/stream", headers={"X-Primer": "1"})
        self.sock = self.conn.sock  # the response takes it over (Connection: close)
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200
        assert self.resp.getheader("Content-Type") == "text/event-stream"
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        kind = None
        try:
            for raw in self.resp:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: "):
                    self.events.put((kind, json.loads(line[len("data: "):])))
        except (OSError, ValueError):
            pass
        self.events.put(("end", None))

    def next(self, timeout: float = 5.0):
        return self.events.get(timeout=timeout)

    def close(self) -> None:
        self.conn.close()


def test_routes_end_to_end(client, app_parts):
    app, _ = app_parts
    nid = _page(app.graph, "Bodiam Castle")
    _switch(client, True)
    stream = _Stream(client.port)
    try:
        kind, hello = stream.next()
        assert kind == "hello" and hello["listener"]
        client.post("/v1/tool/focus", {"listener": hello["listener"], "node_id": nid,
                                       "title": "Bodiam Castle"})
        assert client.get("/v1/tool/now") == {
            "enabled": True, "listening": True, "focus": {"node_id": nid, "title": "Bodiam Castle"}}
        assert client.get("/v1/status")["remote"] == {"enabled": True, "listening": True}
        cmd = client.post("/v1/tool/open", {"id": nid}, expect=202)["command"]
        kind, got = stream.next()
        # the server names the page, so the window's note can say "open Bodiam Castle"
        assert kind == "command" and got["id"] == cmd["id"] and got["args"] == {"id": nid, "title": "Bodiam Castle"}
        assert client.get(f"/v1/tool/commands/{cmd['id']}")["status"] == "delivered"
        me = hello["listener"]
        opened = {"node_id": nid, "title": "Bodiam Castle", "what": "opened"}
        url = f"/v1/tool/commands/{cmd['id']}/done"
        client.post(url, {"ok": True, "result": opened, "listener": "0123456789abcdef"},
                    expect=409)  # not the tab it went to
        client.post(url, {"ok": True, "result": opened}, expect=400)  # no listener
        # the result must say what happened, in one of the words Kairos speaks from, and name the page
        for bad in ("Opened *Bodiam Castle*", ["x"], None, {**opened, "what": "drew"},
                    {"node_id": nid, "what": "opened"}, {**opened, "title": "  "}, {**opened, "node_id": 3}):
            client.post(url, {"ok": True, "result": bad, "listener": me}, expect=400)
        client.post(url, {"ok": True, "listener": me, "result": {**opened, "extra": 1}})
        done = client.get(url[:-len("/done")])
        assert done["status"] == "done" and done["result"] == opened
        client.post(url, {"ok": True, "result": opened, "listener": me}, expect=409)
        client.post("/v1/tool/commands/0123456789abcdef/done", {"ok": True, "result": opened, "listener": me},
                    expect=404)
        client.post(url, {"ok": "yes", "listener": me}, expect=400)
        # a failure carries an error string; a picture with no page carries no node_id
        cmd = client.post("/v1/tool/lens", {"kind": "picture"}, expect=202)["command"]
        assert stream.next()[1]["args"] == {"kind": "picture", "id": nid, "title": "Bodiam Castle"}
        client.post(f"/v1/tool/commands/{cmd['id']}/done", {"ok": False, "error": 3, "listener": me}, expect=400)
        client.post(f"/v1/tool/commands/{cmd['id']}/done",
                    {"ok": False, "error": "Images are not available right now.", "listener": me})
        assert client.get(f"/v1/tool/commands/{cmd['id']}")["error"] == "Images are not available right now."
        cmd = client.post("/v1/tool/lens", {"kind": "map", "id": nid}, expect=202)["command"]
        assert stream.next()[1]["args"] == {"kind": "map", "id": nid, "title": "Bodiam Castle"}
        cmd2 = cmd["id"]
        client.post(f"/v1/tool/commands/{cmd2}/done",
                    {"ok": True, "result": {"title": "a moat", "what": "picture"}, "listener": me})
        assert client.get(f"/v1/tool/commands/{cmd2}")["result"] == {"title": "a moat", "what": "picture"}
    finally:
        stream.close()
    app.remote.shutdown()


def test_stream_ends_on_shutdown(client, app_parts):
    app, _ = app_parts
    _switch(client, True)
    stream = _Stream(client.port)
    try:
        assert stream.next()[0] == "hello"
        assert app.remote.now()["listening"] is True
        app.remote.shutdown()
        assert stream.next()[0] == "end"
    finally:
        stream.close()


def test_trail_newest_first_by_first_arrival(client, app_parts):
    app, _ = app_parts
    a = _page(app.graph, "Rome")
    b = _page(app.graph, "Bodiam Castle")
    app.graph.add_edge(b, a, "Rome", "kairos")  # a later arrival doesn't move Rome up
    pages = client.get("/v1/tool/trail")["pages"]
    assert [p["id"] for p in pages] == [b, a]
    assert pages[0] == {"id": b, "title": "Bodiam Castle", "phrase": "Bodiam Castle",
                        "via": "typed", "at": pages[0]["at"]}
    assert [p["id"] for p in client.get("/v1/tool/trail?limit=1")["pages"]] == [b]
    client.get("/v1/tool/trail?limit=101", expect=400)


def test_kairos_via_accepted(client, app_parts):
    app, _ = app_parts
    ev = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "kairos"})
    nid = ev[0][1]["id"]
    assert app.graph.snapshot()["edges"][-1]["to_id"] == nid
    assert app.graph.snapshot()["edges"][-1]["via"] == "kairos"


def test_activity_event(g, tmp_path):
    hub = _hub()
    lis = hub.listen()
    sig = Signals(g, activity=ActivitySink(tmp_path), kairos_claim=hub.claim_open)
    mine = g.node_for("Kookaburra", "concept")
    theirs = g.node_for("Bodiam Castle", "concept")
    hub.submit("open", {"id": theirs.id})
    lis.get(1)
    sig.record("open", theirs.id, "start")
    sig.record("open", mine.id, "typed")
    sig.record("open", theirs.id, "start")  # the reader's own later open is theirs
    rows = [json.loads(line) for f in sorted((tmp_path / "primer").glob("*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]
    assert [(r["event"], r["ref"]) for r in rows] == [
        ("kairos_opened_page", theirs.id), ("open_page", mine.id), ("open_page", theirs.id)]


def test_activity_event_for_a_kairos_made_page(g, tmp_path):
    sig = Signals(g, activity=ActivitySink(tmp_path))
    node = g.node_for("Rome", "concept")
    sig.record("open", node.id, "kairos")  # show: the window opened it with via kairos
    rows = [json.loads(line) for f in (tmp_path / "primer").glob("*.jsonl")
            for line in f.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["kairos_opened_page"]


def test_app_wires_the_claim(app_parts):
    app, _ = app_parts
    assert app.signals.kairos_claim == app.remote.claim_open


# --- fix round 1 ---
def test_unfocused_new_tab_does_not_take_the_command():
    hub = _hub()
    a = hub.listen()
    hub.focus(a.id, "n1", "Rome")
    b = hub.listen()  # connects (or reconnects) after, never reports focus
    cid = hub.submit("show", {"text": "x"})
    assert b.get(0.05) is None and a.get(1)["id"] == cid


def test_newest_tab_when_none_focused():
    hub = _hub()
    a, b = hub.listen(), hub.listen()
    cid = hub.submit("show", {"text": "x"})
    assert a.get(0.05) is None and b.get(1)["id"] == cid


def test_trail_limit_must_be_ascii_digits(client):
    client.get("/v1/tool/trail?limit=%C2%B2", expect=400)  # a superscript two isdigit()s
    client.get("/v1/tool/trail?limit=%D9%A3", expect=400)  # an Arabic-Indic three
    client.get("/v1/tool/trail?limit=-1", expect=400)


def test_failed_command_drops_its_claim():
    clock = Clock()
    hub = _hub(clock)
    hub.listen()
    hub.submit("open", {"id": "n1"})
    clock.t += 11  # nobody picked it up
    hub.now()
    assert hub.claim_open("n1") is False


def test_lens_on_the_focused_page_claims_nothing():
    hub = _hub()
    lis = hub.listen()
    hub.focus(lis.id, "n1", "Rome")
    hub.submit("lens", {"kind": "map"})  # the focused page
    assert hub.claim_open("n1") is False
    _done(hub, lis)
    hub.submit("lens", {"kind": "map", "id": "n2"})  # another page: the window opens it
    assert hub.claim_open("n2") is True


def test_switch_off_fails_a_queued_command_and_closes_every_stream():
    hub = _hub()
    lis = hub.listen()
    cid = hub.submit("show", {"text": "x"})
    hub.enabled = False
    assert lis.closed and lis.get(0.05) is None and hub.listening() is False
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == OFF
    late = hub.listen()  # a tab that connects while it is off is ended at once
    assert late.closed and late.get(0.01) is None and hub.listening() is False
    hub.enabled = True
    lis = hub.listen()
    cid2 = hub.submit("show", {"text": "y"})  # free again
    assert lis.get(1)["id"] == cid2
    hub.enabled = False  # a delivered command fails too: the reader switched Kairos off
    got = hub.command(cid2)
    assert lis.closed and got["status"] == "failed" and got["error"] == OFF
    hub.enabled = True
    lis = hub.listen()
    cid3 = hub.submit("show", {"text": "z"})  # and the hub is free
    assert lis.get(1)["id"] == cid3


def test_switch_off_ends_open_streams_over_http(client, app_parts):
    app, _ = app_parts
    _switch(client, True)
    streams = [_Stream(client.port), _Stream(client.port)]
    try:
        assert [s.next()[0] for s in streams] == ["hello", "hello"]
        _switch(client, False)
        assert [s.next()[0] for s in streams] == ["end", "end"]
        assert app.remote.listening() is False
    finally:
        for s in streams:
            s.close()


def test_stream_refused_while_off(client, app_parts):
    app, _ = app_parts
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    try:
        conn.request("GET", "/v1/tool/stream", headers={"X-Primer": "1"})
        resp = conn.getresponse()
        assert resp.status == 403 and json.loads(resp.read()) == {"error": OFF}
    finally:
        conn.close()
    assert app.remote.listening() is False


def test_stream_needs_the_primer_header(client, app_parts):
    app, _ = app_parts
    _switch(client, True)
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    try:
        conn.request("GET", "/v1/tool/stream")  # a cross-site page could open this without it
        resp = conn.getresponse()
        assert resp.status == 403 and "X-Primer" in json.loads(resp.read())["error"]
    finally:
        conn.close()
    assert app.remote.listening() is False


def test_hidden_tab_does_not_take_the_focus():
    hub = _hub()
    front, back = hub.listen(), hub.listen()
    hub.focus(back.id, "n1", "Rome")
    hub.focus(front.id, "n2", "Carthage")  # the reader moved to this tab
    hub.focus(back.id, "n3", "Ostia", hidden=True)  # the background tab's page changed
    assert hub.now()["focus"] == {"node_id": "n2", "title": "Carthage"}
    cid = hub.submit("show", {"text": "x"})
    assert front.get(1)["id"] == cid
    hub.finish(cid, True, "ok")
    assert back.focus == {"node_id": "n3", "title": "Ostia"}  # its page is kept for later
    hub.focus(front.id, "n4", "Utica", hidden=True)  # the focused tab, hidden: still the focused one
    assert hub.now()["focus"] == {"node_id": "n4", "title": "Utica"}
    hub.focus(back.id, "n3", "Ostia")  # shown again
    assert hub.now()["focus"] == {"node_id": "n3", "title": "Ostia"}


def test_hidden_focus_over_http(client, app_parts):
    app, _ = app_parts
    rome, ostia = _page(app.graph, "Rome"), _page(app.graph, "Ostia")
    _switch(client, True)
    front, back = app.remote.listen(), app.remote.listen()
    client.post("/v1/tool/focus", {"listener": front.id, "node_id": rome, "title": "Rome"})
    client.post("/v1/tool/focus", {"listener": back.id, "node_id": ostia, "title": "Ostia", "hidden": "yes"},
                expect=400)
    client.post("/v1/tool/focus", {"listener": back.id, "node_id": ostia, "title": "Ostia", "hidden": True})
    assert client.get("/v1/tool/now")["focus"] == {"node_id": rome, "title": "Rome"}
    client.post("/v1/tool/focus", {"listener": back.id, "node_id": ostia, "title": "Ostia", "hidden": False})
    assert client.get("/v1/tool/now")["focus"] == {"node_id": ostia, "title": "Ostia"}
    app.remote.shutdown()


def test_old_claims_pruned_on_submit():
    clock = Clock()
    hub = _hub(clock)
    lis = hub.listen()
    hub.submit("open", {"id": "n1"})
    _done(hub, lis)
    clock.t += 61
    hub.submit("open", {"id": "n2"})
    assert "n1" not in hub._claims and "n2" in hub._claims


def test_lens_on_a_page_the_tab_already_showed_drops_its_claim():
    hub = _hub()
    lis = hub.listen()
    hub.focus(lis.id, "n1", "Rome")
    cid = hub.submit("lens", {"kind": "map", "id": "n2"})  # the hub thinks the tab shows n1
    lis.get(1)
    hub.focus(lis.id, "n2", "Ostia")  # it showed n2 already: the lens opened nothing
    hub.finish(cid, True, {"node_id": "n2", "title": "Ostia", "what": "map"}, lis.id)
    assert hub.claim_open("n2") is False  # the reader's own next open of n2 is the reader's
    # an open command keeps its claim until the page's open signal takes it
    cid = hub.submit("open", {"id": "n3"})
    lis.get(1)
    hub.focus(lis.id, "n3", "Utica")
    hub.finish(cid, True, {"node_id": "n3", "title": "Utica", "what": "opened"}, lis.id)
    assert hub.claim_open("n3") is True
    # a lens whose result names a page the tab isn't showing keeps it too
    cid = hub.submit("lens", {"kind": "map", "id": "n4"})
    lis.get(1)
    hub.finish(cid, True, {"node_id": "n4", "title": "Hippo", "what": "map"}, lis.id)
    assert hub.claim_open("n4") is True


def test_done_from_the_wrong_tab_refused():
    hub = _hub()
    a, b = hub.listen(), hub.listen()
    hub.focus(a.id, "n1", "Rome")
    cid = hub.submit("show", {"text": "x"})
    a.get(1)
    with pytest.raises(WrongListener):
        hub.finish(cid, True, "ok", b.id)
    assert hub.command(cid)["status"] == "delivered"
    assert hub.finish(cid, True, "ok", a.id) is True


def test_listener_cap_closes_oldest_unfocused_then_oldest():
    hub = _hub()
    tabs = [hub.listen() for _ in range(MAX_LISTENERS)]
    hub.focus(tabs[0].id, "n0", "N0")
    extra = hub.listen()
    assert tabs[1].closed and not tabs[0].closed and not extra.closed  # tabs[1]: oldest unfocused
    for t in [t for t in tabs if not t.closed] + [extra]:
        hub.focus(t.id, "n", "N")
    last = hub.listen()
    assert tabs[0].closed and not last.closed  # all focused: the oldest goes
    assert sum(not t.closed for t in tabs + [extra, last]) == MAX_LISTENERS


def test_listener_cap_fails_a_dropped_tabs_command():
    hub = _hub()
    tabs = [hub.listen() for _ in range(MAX_LISTENERS)]
    cid = hub.submit("show", {"text": "x"})  # none focused: the newest, tabs[-1]
    for t in tabs[:-1]:
        hub.focus(t.id, "n", "N")
    hub.listen()  # tabs[-1] is the only unfocused one, so it goes, and its command fails
    assert tabs[-1].closed and not tabs[0].closed
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == CLOSED


def test_remote_enabled_needs_same_origin(client, app_parts):
    app, _ = app_parts
    for headers in ({}, {"Sec-Fetch-Site": "none"}):
        assert client.post("/v1/remote-enabled", {"value": True}, expect=403,
                           headers=headers) == {"error": "Turn this on in Primer's Settings"}
    assert app.remote.enabled is False
    _switch(client, True)
    assert app.remote.enabled is True


def test_done_s_fails_an_unfinished_command():
    clock = Clock()
    hub = _hub(clock)
    lis = hub.listen()
    cid = hub.submit("show", {"text": "x"})
    lis.get(1)
    clock.t += DONE_S - 1
    assert hub.command(cid)["status"] == "delivered"
    clock.t += 1
    got = hub.command(cid)
    assert got["status"] == "failed" and got["error"] == NOT_FINISHED
    assert hub.finish(cid, True, "late", lis.id) is False
    assert hub.submit("show", {"text": "y"})  # free again


def test_rate_window_rolls_over():
    clock = Clock()
    hub = _hub(clock)
    lis = hub.listen()
    for i in range(20):
        hub.submit("show", {"text": f"n{i}"})
        _done(hub, lis)
    with pytest.raises(RemoteRateLimited):
        hub.submit("show", {"text": "more"})
    clock.t += 59.9
    with pytest.raises(RemoteRateLimited):
        hub.submit("show", {"text": "more"})
    clock.t += 0.1  # a minute after all twenty
    for i in range(20):
        hub.submit("show", {"text": f"m{i}"})
        _done(hub, lis)
    with pytest.raises(RemoteRateLimited):
        hub.submit("show", {"text": "more"})


def test_dropped_stream_closes_listener_and_fails_command(client, app_parts):
    app, _ = app_parts
    nid = _page(app.graph, "Bodiam Castle")
    _switch(client, True)
    stream = _Stream(client.port)
    assert stream.next()[0] == "hello"
    cmd = client.post("/v1/tool/open", {"id": nid}, expect=202)["command"]
    assert stream.next()[1]["id"] == cmd["id"]
    stream.sock.shutdown(socket.SHUT_RDWR)  # the tab goes away
    stream.close()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and app.remote.listening():
        time.sleep(0.05)
    assert app.remote.listening() is False
    got = client.get(f"/v1/tool/commands/{cmd['id']}")
    assert got["status"] == "failed" and got["error"] == CLOSED


def test_timed_out_command_frees_the_hub_over_http(client, app_parts):
    app, _ = app_parts
    clock = Clock()
    app.remote.clock = clock
    _switch(client, True)
    app.remote.listen()  # a tab that never picks anything up
    cid = client.post("/v1/tool/show", {"text": "x"}, expect=202)["command"]["id"]
    assert client.post("/v1/tool/show", {"text": "y"}, expect=409)["error"] == BUSY
    clock.t += 10
    got = client.get(f"/v1/tool/commands/{cid}")
    assert got["status"] == "failed" and got["error"] == PICKUP_FAILED
    assert client.post("/v1/tool/show", {"text": "y"}, expect=202)["command"]["status"] == "queued"


def test_open_and_lens_args_carry_the_page_title():
    hub = _hub()
    lis = hub.listen()
    long = "Bodiam  Castle\n" + "x" * 400
    cid = hub.submit("open", {"id": "n1", "title": long})
    got = _done(hub, lis)
    assert got["id"] == cid and got["args"]["id"] == "n1"
    assert got["args"]["title"].startswith("Bodiam Castle x") and len(got["args"]["title"]) == TITLE_MAX
    hub.submit("open", {"id": "n1"})  # no title: none in the args (the window falls back)
    assert _done(hub, lis)["args"] == {"id": "n1"}
    hub.focus(lis.id, "n2", "Kenilworth Castle")
    hub.submit("lens", {"kind": "story"})
    assert _done(hub, lis)["args"] == {"kind": "story", "id": "n2", "title": "Kenilworth Castle"}
    hub.submit("lens", {"kind": "map", "id": "n3", "title": "Trebuchet"})
    assert _done(hub, lis)["args"] == {"kind": "map", "id": "n3", "title": "Trebuchet"}


# --- "that" means the focused page (spec 2.4) ---
@pytest.mark.parametrize("text, want", [
    ("tell me a story about that", "tell me a story about Trebuchet"),
    ("a map of it", "a map of Trebuchet"),
    ("show me that", "show me Trebuchet"),
    ("that", "Trebuchet"),
    ("That?", "Trebuchet?"),
    ("tell me more ABOUT This Page.", "tell me more ABOUT Trebuchet."),
    ("open that page", "open Trebuchet"),
    ("a timeline for this", "a timeline for Trebuchet"),
    ("more on it, please", "more on Trebuchet, please"),
    ("a story about it and the knights", "a story about Trebuchet and the knights"),
])
def test_resolve_that(text, want):
    assert resolve_that(text, "Trebuchet") == want


@pytest.mark.parametrize("text", [
    "the castle that Edward built",
    "a map of this region",
    "what is it",
    "is that a castle",
    "tell me about thatcher",
    "proof it",
    "it is a siege engine",
    "a map of Bodiam Castle",
])
def test_resolve_that_leaves_other_uses(text):
    assert resolve_that(text, "Trebuchet") == text
    assert resolve_that(text, None) == text  # nothing to resolve: no focus needed


def test_resolve_that_needs_a_focused_page():
    with pytest.raises(ValueError) as e:
        resolve_that("a map of it", None)
    assert str(e.value) == NO_FOCUS
    hub = _hub()
    hub.listen()  # a tab, but it never reported focus
    with pytest.raises(ValueError):
        hub.submit("show", {"text": "a map of it"})
    assert hub.submit("show", {"text": "a map of Rome"})


def test_show_keeps_that_when_the_title_makes_it_too_long():
    hub = _hub()
    lis = hub.listen()
    hub.focus(lis.id, "n1", "T" * 200)
    text = "x" * 120 + " tell me a story about that"  # under 300 as said, over once resolved
    assert len(text) <= 300
    cid = hub.submit("show", {"text": text})
    assert hub.command(cid)["args"] == {"text": text}
    _done(hub, lis)
    cid = hub.submit("show", {"text": "a map of that"})  # short enough: resolved
    assert hub.command(cid)["args"] == {"text": "a map of " + "T" * 200}


def test_show_resolves_that_over_http(client, app_parts):
    app, _ = app_parts
    nid = _page(app.graph, "Trebuchet")
    _switch(client, True)
    lis = app.remote.listen()
    assert client.post("/v1/tool/show", {"text": "tell me a story about that"},
                       expect=400) == {"error": NO_FOCUS}
    client.post("/v1/tool/focus", {"listener": lis.id, "node_id": nid, "title": "Trebuchet"})
    cmd = client.post("/v1/tool/show", {"text": "tell me a story about that"}, expect=202)["command"]
    assert cmd["args"] == {"text": "tell me a story about Trebuchet"}
    got = lis.get(1)
    assert got["id"] == cmd["id"] and got["args"]["text"] == "tell me a story about Trebuchet"
    client.post(f"/v1/tool/commands/{cmd['id']}/done", {"ok": True, "listener": lis.id,
                                                        "result": {"title": "Trebuchet", "what": "story"}})
    cmd = client.post("/v1/tool/show", {"text": "the castle that Edward built"}, expect=202)["command"]
    assert cmd["args"] == {"text": "the castle that Edward built"}
