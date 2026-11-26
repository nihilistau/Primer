"""The live-page routes: /v1/live/*, with a fake LiveService and a scripted fake engine."""
import http.client
import json
import threading
import time

import pytest

from primer.config import EngineSpec, Tier
from primer.live.service import LiveError
from tests.fakes import FakeEngine
from tests.test_server import _grounded

OFF = "Live pages are off while web search is off"


class FakeLive:
    """Stands in for primer.live.service.LiveService."""

    def __init__(self):
        self.tier = None
        self.on = True
        self.browser = True
        self.windows = {}  # name -> threading.Event (set when closed)
        self.gens = {}  # name -> generation
        self.reasons = {}  # name -> why it ended
        self.made = 0
        self.inputs = 0
        self.turned_off = False
        self.calls = []
        self.frames_ended = threading.Event()
        self.forgotten = False
        self.shut = False
        self.nav_event = None  # sent first on frames when set
        self.page = {"title": "Bodiam Castle", "url": "https://castle.test/", "text": "Tickets on sale.",
                     "controls": [{"n": 1, "kind": "link", "label": "Hours", "href": "https://castle.test/h"},
                                  {"n": 3, "kind": "button", "label": "Buy tickets"}]}

    @property
    def enabled(self):
        return self.on

    def status(self):
        return {"available": self.browser, "label": "Chrome 158" if self.browser else "",
                "enabled": self.on, "windows": sorted(self.windows)}

    def open(self, window, url, width, height):
        self.calls.append(("open", window, url, width, height))
        if not self.on:
            raise LiveError(403, OFF)
        if not url.startswith("https://"):
            raise LiveError(400, "That address can't be opened live")
        if not self.browser:
            raise LiveError(503, "Live pages need Chrome or Edge")
        if window not in self.windows and len(self.windows) >= 4:
            raise LiveError(409, "Up to 4 live pages at a time")
        if window not in self.windows:
            self.windows[window] = threading.Event()
            self.made += 1
            self.gens[window] = f"g{self.made}"
        return {"window": window, "label": "Chrome 158"}

    def generation(self, window):
        return self.gens.get(window) if window in self.windows else None

    def is_open(self, window, gen=None):
        g = self.generation(window)
        return g is not None and (gen is None or g == gen)

    def turn_off(self, reason=OFF):
        self.turned_off = True
        for name in list(self.windows):
            self.close(name, reason)

    def frames(self, window, keepalive=15.0):
        if window not in self.windows:
            raise LiveError(404, "That live page has closed")
        closed = self.windows[window]

        def gen():
            try:
                if self.nav_event:
                    yield {"nav": self.nav_event}
                yield {"data": "/9j/AA==", "w": 640, "h": 480}
                yield {"note": "Primer's browser doesn't download files"}
                while not closed.wait(0.05):
                    yield {"ping": True}
                yield {"gone": self.reasons.get(window, "closed")}
            finally:
                self.frames_ended.set()
        return gen()

    def input(self, window, event):
        if len(json.dumps(event, ensure_ascii=False).encode()) > 4096:
            raise LiveError(413, "That input is too large")
        self._need(window)
        self.inputs += 1
        self.calls.append(("input", window, event))

    def nav(self, window, action, url=None):
        self._need(window)
        self.calls.append(("nav", window, action, url))

    def resize(self, window, width, height):
        self._need(window)
        self.calls.append(("resize", window, width, height))

    def read_text(self, window):
        self._need(window)
        self.calls.append(("read_text", window))
        return {k: self.page[k] for k in ("title", "url", "text")}

    def close(self, window, reason="closed"):
        ev = self.windows.pop(window, None)
        if ev is None:
            return False
        self.reasons[window] = reason
        ev.set()
        return True

    def snapshot(self, window, gen=None, with_token=False):
        self._need(window, gen)
        return {**self.page, "token": "t"} if with_token else self.page

    def act(self, window, action, gen=None, token=None):
        self._need(window, gen)
        self.calls.append(("act", window, action))
        return {"done": action["action"]}

    def forget(self):
        for name in list(self.windows):
            self.close(name)
        self.forgotten = True

    def shutdown(self):
        self.shut = True

    def _need(self, window, gen=None):
        if not self.on:
            raise LiveError(403, OFF)
        if not self.is_open(window, gen):
            raise LiveError(404, "That live page has closed")


def _spec():
    return EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")


@pytest.fixture
def env(tmp_path, g):
    app, client, close = _grounded(tmp_path, g, Tier("t", _spec(), _spec()))
    real = app.live
    app.live = FakeLive()
    yield app, client
    close()
    real.shutdown()


def opened(client, window="w1"):
    return client.post("/v1/live/open", {"window": window, "url": "https://castle.test/",
                                         "width": 800, "height": 600})


def raw(port, method, path, body=b"", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        hs = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "X-Primer": "1",
              **(headers or {})}
        c.request(method, path, body=body, headers=hs)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


class Stream:
    """Reads an SSE response one event at a time; comments come back as ("comment", text)."""

    def __init__(self, port, method, path, body=None, headers=None):
        self.c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        hs = {"Content-Type": "application/json", "X-Primer": "1", **(headers or {})}
        self.c.request(method, path, body=None if body is None else json.dumps(body), headers=hs)
        self.r = self.c.getresponse()
        self.status = self.r.status

    def next(self):
        lines = []
        while True:
            line = self.r.fp.readline()
            if not line:
                return None
            line = line.decode("utf-8").rstrip("\r\n")
            if line == "":
                if lines:
                    break
                continue
            lines.append(line)
        if lines[0].startswith(":"):
            return "comment", lines[0][1:].strip()
        kind = lines[0].removeprefix("event: ")
        return kind, json.loads(lines[1].removeprefix("data: "))

    def until(self, kind):
        seen = []
        while True:
            ev = self.next()
            if ev is None:
                return seen
            seen.append(ev)
            if ev[0] == kind:
                return seen

    def close(self):
        self.c.close()


def test_status_has_live(env):
    app, client = env
    assert client.get("/v1/status")["live"] == {"available": True, "label": "Chrome 158", "enabled": True}
    opened(client)
    assert client.get("/v1/live/status")["windows"] == ["w1"]


def test_real_service_is_built_and_reported(tmp_path, g):
    from primer.live.service import LiveService
    app, client, close = _grounded(tmp_path, g, Tier("t", _spec(), _spec()))
    try:
        assert isinstance(app.live, LiveService)
        live = client.get("/v1/status")["live"]
        assert set(live) == {"available", "label", "enabled"} and live["enabled"] is True
    finally:
        close()
        app.shutdown_live()


def test_open_validates(env):
    app, client = env
    assert opened(client) == {"window": "w1", "label": "Chrome 158"}
    assert app.live.calls[-1] == ("open", "w1", "https://castle.test/", 800, 600)
    good = {"window": "w2", "url": "https://castle.test/", "width": 800, "height": 600}
    for bad in ({"window": "W2"}, {"window": "a" * 33}, {"window": ""}, {"window": "a/b"}, {"window": 5},
                {"url": ""}, {"url": None}, {"width": 199}, {"width": 4001}, {"height": 100},
                {"height": 5000}, {"width": True}, {"width": 800.5}, {"height": "600"}):
        assert "error" in client.post("/v1/live/open", {**good, **bad}, expect=400), bad
    assert len(app.live.calls) == 1


def test_open_maps_live_errors(env):
    app, client = env
    client.post("/v1/live/open", {"window": "w1", "url": "http://172.31.0.5/", "width": 800, "height": 600},
                expect=400)
    for i in range(4):
        opened(client, f"w{i}")
    assert client.post("/v1/live/open", {"window": "w9", "url": "https://castle.test/", "width": 800,
                                         "height": 600}, expect=409)["error"] == "Up to 4 live pages at a time"
    app.live.on = False
    assert client.post("/v1/live/open", {"window": "w0", "url": "https://castle.test/", "width": 800,
                                         "height": 600}, expect=403)["error"] == OFF
    app.live.on, app.live.browser = True, False
    client.post("/v1/live/open", {"window": "w5", "url": "https://castle.test/", "width": 800,
                                  "height": 600}, expect=503)


def test_cross_site_and_non_json_posts_are_refused(env):
    app, client = env
    port = client.port
    body = json.dumps({"window": "w1", "url": "https://castle.test/", "width": 800, "height": 600}).encode()
    assert raw(port, "POST", "/v1/live/open", body, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert raw(port, "POST", "/v1/live/open", body, {"Origin": "https://evil.test"})[0] == 403
    assert raw(port, "POST", "/v1/live/open", body, {"Content-Type": "text/plain"})[0] == 415
    assert raw(port, "POST", "/v1/live/open", body, {"Host": "evil.test"})[0] == 403
    assert raw(port, "POST", "/v1/live/forget", b"{}", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert app.live.calls == [] and not app.live.forgotten
    opened(client)
    assert raw(port, "GET", "/v1/live/w1/frames", headers={"X-Primer": ""})[0] == 403
    assert raw(port, "GET", "/v1/live/w1/frames", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403


def test_window_ids_in_the_path(env):
    app, client = env
    for bad in ("W1", "a" * 33, "a_b", "%2e%2e"):
        assert "error" in client.post(f"/v1/live/{bad}/close", {}, expect=400), bad
    client.post("/v1/live/w1/teleport", {}, expect=404)
    client.get("/v1/live/w1/input", expect=404)
    client.post("/v1/live/w1/frames", {}, expect=404)


def test_input_nav_close(env):
    app, client = env
    client.post("/v1/live/w1/input", {"kind": "text", "text": "hi"}, expect=404)
    opened(client)
    assert client.post("/v1/live/w1/input", {"kind": "text", "text": "hi"}) == {"ok": True}
    client.post("/v1/live/w1/input", {"kind": "text", "text": "x" * 5000}, expect=413)
    assert client.post("/v1/live/w1/nav", {"action": "back"}) == {"ok": True}
    assert client.post("/v1/live/w1/nav", {"action": "url", "url": "https://castle.test/b"}) == {"ok": True}
    assert ("nav", "w1", "url", "https://castle.test/b") in app.live.calls
    client.post("/v1/live/w1/nav", {"action": "url"}, expect=400)
    client.post("/v1/live/w1/nav", {"action": "jump"}, expect=400)
    assert client.post("/v1/live/w1/close", {}) == {"closed": True}
    assert client.post("/v1/live/w1/close", {}) == {"closed": False}
    client.post("/v1/live/w1/nav", {"action": "back"}, expect=404)


def test_forget(env):
    app, client = env
    opened(client)
    client.post("/v1/live/forget", {"all": True}, expect=400)
    assert not app.live.forgotten
    assert client.post("/v1/live/forget", {}) == {"ok": True}
    assert app.live.forgotten and app.live.windows == {}


def test_frames_stream_ends_when_the_window_closes(env):
    app, client = env
    client.get("/v1/live/w1/frames", expect=404)
    opened(client)
    s = Stream(client.port, "GET", "/v1/live/w1/frames")
    try:
        assert s.status == 200 and s.r.getheader("Content-Type") == "text/event-stream"
        assert s.next() == ("frame", {"data": "/9j/AA==", "w": 640, "h": 480})
        assert s.next() == ("note", {"text": "Primer's browser doesn't download files"})
        assert s.next() == ("comment", "ping")
        client.post("/v1/live/w1/close", {})
        rest = s.until("gone")
        assert rest[-1] == ("gone", {"reason": "closed"})
        assert s.next() is None  # the response ends
    finally:
        s.close()
    assert app.live.frames_ended.wait(5)


def test_frames_stream_ends_when_the_client_goes(env):
    app, client = env
    opened(client)
    s = Stream(client.port, "GET", "/v1/live/w1/frames")
    assert s.next()[0] == "frame"
    s.r.close()
    s.close()
    assert app.live.frames_ended.wait(10)  # the request thread stopped reading frames


def agent_env(app, replies):
    app.pages.text = FakeEngine(replies)


def test_agent_route_errors(env):
    app, client = env
    client.sse("/v1/live/w1/agent", {"goal": "hours"}, expect=404)
    opened(client)
    client.sse("/v1/live/w1/agent", {"goal": ""}, expect=400)
    client.sse("/v1/live/w1/agent", {"goal": "x" * 1001}, expect=400)
    client.sse("/v1/live/w1/agent", {}, expect=400)
    app.live.on = False
    assert client.sse("/v1/live/w1/agent", {"goal": "hours"}, expect=403)["error"] == OFF
    app.live.on = True
    assert app.live_agents.begin("w1") is not None
    try:
        client.sse("/v1/live/w1/agent", {"goal": "hours"}, expect=409)
    finally:
        app.live_agents.end("w1")


def test_answer_route_errors(env):
    app, client = env
    opened(client)
    client.post("/v1/live/w1/agent/answer", {"id": "abc", "allow": True}, expect=404)
    client.post("/v1/live/w1/agent/answer", {"id": "abc", "allow": "yes"}, expect=400)
    client.post("/v1/live/w1/agent/answer", {"allow": True}, expect=400)
    app.live_agents.expect("w1", "abc")
    client.post("/v1/live/w1/agent/answer", {"id": "zzz", "allow": True}, expect=404)
    assert client.post("/v1/live/w1/agent/answer", {"id": "abc", "allow": False}) == {"ok": True}


def test_agent_follows_freely_and_answers(env):
    app, client = env
    opened(client)
    agent_env(app, [json.dumps({"action": "follow", "n": 1, "why": "hours"}),
                    json.dumps({"action": "answer", "text": "10 to 5"})])
    events = client.sse("/v1/live/w1/agent", {"goal": "Find the opening hours"})
    assert [k for k, _ in events] == ["step", "step", "answer"]
    assert ("act", "w1", {"action": "follow", "n": 1}) in app.live.calls
    assert not app.live_agents.running("w1")


def test_agent_asks_and_allow_runs_it(env):
    app, client = env
    opened(client)
    agent_env(app, [json.dumps({"action": "press", "n": 3, "why": "buy"}),
                    json.dumps({"action": "answer", "text": "bought"})])
    s = Stream(client.port, "POST", "/v1/live/w1/agent", {"goal": "Buy a ticket"})
    try:
        seen = s.until("ask")
        ask = seen[-1][1]
        assert ask["text"] == "The model wants to press Buy tickets"
        assert not any(c[0] == "act" for c in app.live.calls)
        assert client.post("/v1/live/w1/agent/answer", {"id": ask["id"], "allow": True}) == {"ok": True}
        rest = s.until("answer")
        assert rest[-1] == ("answer", {"text": "bought"})
        assert ("act", "w1", {"action": "press", "n": 3}) in app.live.calls
    finally:
        s.close()


def test_agent_stop_ends_it(env):
    app, client = env
    opened(client)
    agent_env(app, [json.dumps({"action": "press", "n": 3})])
    s = Stream(client.port, "POST", "/v1/live/w1/agent", {"goal": "Buy a ticket"})
    try:
        ask = s.until("ask")[-1][1]
        client.post("/v1/live/w1/agent/answer", {"id": ask["id"], "allow": False})
        assert s.until("stopped")[-1] == ("stopped", {"reason": "Stopped"})
        assert s.next() is None
    finally:
        s.close()
    assert not any(c[0] == "act" for c in app.live.calls)
    deadline = time.monotonic() + 5
    while app.live_agents.running("w1") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not app.live_agents.running("w1")


def test_closing_the_window_answers_a_pending_ask(env):
    app, client = env
    opened(client)
    agent_env(app, [json.dumps({"action": "press", "n": 3})])
    s = Stream(client.port, "POST", "/v1/live/w1/agent", {"goal": "Buy a ticket"})
    try:
        s.until("ask")
        client.post("/v1/live/w1/close", {})
        assert s.until("stopped")[-1][0] == "stopped"
        assert s.next() is None
    finally:
        s.close()
    assert not any(c[0] == "act" for c in app.live.calls)


def test_shutdown_live(env):
    app, client = env
    app.shutdown_live()
    assert app.live.shut and app.live_agents.begin("w1") is None


# --- fix round 1 ---------------------------------------------------------------------------

OFF_TIER = """name = "off"
fast = "text"
[text]
kind = "openai"
base_url = "http://127.0.0.1:1"
[web]
search = "off"
"""


def claimed(port, path, length, body):
    """POST with a Content-Length that claims `length` bytes but sends only `body`."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest("POST", path, skip_host=True)
        for k, v in (("Host", f"127.0.0.1:{port}"), ("Content-Type", "application/json"),
                     ("Content-Length", str(length))):
            c.putheader(k, v)
        c.endheaders()
        c.send(body)  # a short body: the server must not wait for the rest
        t0 = time.monotonic()
        r = c.getresponse()
        return r.status, json.loads(r.read()), time.monotonic() - t0
    finally:
        c.close()


def test_input_limit_is_checked_before_the_body_is_read(env):
    app, client = env
    opened(client)
    port = client.port
    status, out, took = claimed(port, "/v1/live/w1/input", 5000, b'{"kind": "text"')
    assert status == 413 and out["error"] == "That input is too large" and took < 5
    body = json.dumps({"kind": "text", "text": "x" * 4500}).encode()
    assert raw(port, "POST", "/v1/live/w1/input", body)[0] == 413
    assert app.live.inputs == 0
    status, _, took = claimed(port, "/v1/live/open", 1_100_000, b'{"window": "w2"')
    assert status == 413 and took < 5
    assert client.post("/v1/live/w1/input", {"kind": "text", "text": "hi"}) == {"ok": True}


def test_body_window_ids_are_not_percent_decoded(env):
    app, client = env
    client.post("/v1/live/open", {"window": "%61", "url": "https://castle.test/", "width": 800,
                                  "height": 600}, expect=400)
    client.post("/v1/live/%61/close", {}, expect=400)
    assert app.live.calls == []


def test_search_off_closes_live_windows_and_stops_runs(env):
    app, client = env
    (app.tiers_dir / "off.toml").write_text(OFF_TIER)
    opened(client)
    agent_env(app, [json.dumps({"action": "press", "n": 3})])
    frames = Stream(client.port, "GET", "/v1/live/w1/frames")
    agent = Stream(client.port, "POST", "/v1/live/w1/agent", {"goal": "Buy a ticket"})
    try:
        assert frames.next()[0] == "frame"
        agent.until("ask")
        app.live.on = False  # what the new tier's [web] search = "off" means to the service
        try:
            client.post("/v1/tier", {"name": "off"})
        finally:
            app.servers.stop_all()
        assert app.live.turned_off and app.live.tier.web.search == "off"
        assert frames.until("gone")[-1] == ("gone", {"reason": OFF})
        assert agent.until("stopped")[-1][0] == "stopped"
        assert agent.next() is None
    finally:
        frames.close()
        agent.close()
    assert not any(c[0] == "act" for c in app.live.calls)
    app.live.windows["w1"] = threading.Event()  # even a window the service still lists
    assert client.post("/v1/live/w1/nav", {"action": "back"}, expect=403)["error"] == OFF


def test_a_close_while_the_model_thinks_ends_the_run(env):
    app, client = env
    opened(client)
    thinking, release = threading.Event(), threading.Event()

    def reply(messages):
        thinking.set()
        release.wait(10)
        return json.dumps({"action": "follow", "n": 1})
    app.pages.text = FakeEngine(reply)
    s = Stream(client.port, "POST", "/v1/live/w1/agent", {"goal": "Find the hours"})
    try:
        assert thinking.wait(5)
        client.post("/v1/live/w1/close", {})
        opened(client)  # the same id, reopened on a fresh window
        assert app.live.generation("w1") == "g2"
        release.set()
        assert s.until("never") == [("stopped", {"reason": "The live page has closed"})]
    finally:
        s.close()
    assert not any(c[0] == "act" for c in app.live.calls)
    deadline = time.monotonic() + 5
    while app.live_agents.running("w1") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not app.live_agents.running("w1")


# --- fix round 1 (Task 4 review) ---------------------------------------------------------------

def test_accented_input_is_measured_in_utf8_bytes(env):
    app, client = env
    opened(client)
    port = client.port
    body = json.dumps({"kind": "text", "text": "é" * 700}, ensure_ascii=False).encode()
    assert len(body) < 4096 < len(json.dumps({"kind": "text", "text": "é" * 700}))
    assert raw(port, "POST", "/v1/live/w1/input", body)[0] == 200
    assert app.live.calls[-1] == ("input", "w1", {"kind": "text", "text": "é" * 700})


def test_frames_carry_nav_events(env):
    app, client = env
    opened(client)
    app.live.nav_event = {"url": "https://castle.test/visit", "title": "Visit us"}
    s = Stream(client.port, "GET", "/v1/live/w1/frames")
    try:
        assert s.next() == ("nav", {"url": "https://castle.test/visit", "title": "Visit us"})
        assert s.next()[0] == "frame"
    finally:
        s.close()


def test_resize_route(env):
    app, client = env
    client.post("/v1/live/w1/resize", {"width": 900, "height": 700}, expect=404)
    opened(client)
    assert client.post("/v1/live/w1/resize", {"width": 900, "height": 700}) == {"ok": True}
    assert app.live.calls[-1] == ("resize", "w1", 900, 700)
    for bad in ({"width": 199, "height": 700}, {"width": 900, "height": 4001}, {"width": 900.5, "height": 700},
                {"width": "900", "height": 700}, {"width": 900}, {"width": 900, "height": 700, "x": 1}):
        assert "error" in client.post("/v1/live/w1/resize", bad, expect=400), bad
    client.post("/v1/live/W1/resize", {"width": 900, "height": 700}, expect=400)
    port = client.port
    body = json.dumps({"width": 900, "height": 700}).encode()
    assert raw(port, "POST", "/v1/live/w1/resize", body, {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert raw(port, "POST", "/v1/live/w1/resize", body, {"Content-Type": "text/plain"})[0] == 415
    app.live.on = False
    assert client.post("/v1/live/w1/resize", {"width": 900, "height": 700}, expect=403)["error"] == OFF
    assert [c for c in app.live.calls if c[0] == "resize"] == [("resize", "w1", 900, 700)]


def test_ask_reads_the_live_page_text(env):
    app, client = env
    agent_env(app, ["It opens at ten."])
    client.sse("/v1/browse/ask", {"live": "w1", "question": "When?"}, expect=404)
    client.sse("/v1/browse/ask", {"live": "W 1", "question": "When?"}, expect=400)
    opened(client)
    app.live.page = {**app.live.page, "text": "Signed in. Open 10 to 5 on Saturday."}
    ev = client.sse("/v1/browse/ask", {"live": "w1", "u": "https://ignored.test/", "question": "When?"})
    assert ev[-1] == ("done", {}) and "".join(d["text"] for k, d in ev if k == "delta") == "It opens at ten."
    prompt = app.pages.text.calls[-1][1]["content"]
    assert "Signed in. Open 10 to 5 on Saturday." in prompt and "Bodiam Castle" in prompt
    assert app.live.calls[-1] == ("read_text", "w1")
    app.live.page = {**app.live.page, "url": "about:blank"}
    client.sse("/v1/browse/ask", {"live": "w1", "question": "When?"}, expect=409)
    client.sse("/v1/browse/agent", {"live": "w1", "u": "https://castle.test/", "goal": "g"}, expect=400)
    app.live.on = False
    assert client.sse("/v1/browse/ask", {"live": "w1", "question": "When?"}, expect=403)["error"] == OFF


def test_ask_caps_the_live_text_like_the_reader(env):
    from primer.browse.prompts import ASK_CONTEXT
    app, client = env
    agent_env(app, ["ok"])
    opened(client)
    app.live.page = {**app.live.page, "text": "word " * 20000}
    client.sse("/v1/browse/ask", {"live": "w1", "question": "What?"})
    prompt = app.pages.text.calls[-1][1]["content"]
    assert prompt.count("word") <= ASK_CONTEXT // 5 + 1


def test_make_a_page_from_the_live_text(env):
    app, client = env
    seen = []

    def fake_open(req):
        seen.append(req)
        yield ("done", {})
    app.pages.open = fake_open
    body = {"concept": "Bodiam", "via": "typed", "source_url": "https://castle.test/", "source_live": "w1"}
    client.sse("/v1/pages/open", body, expect=404)
    opened(client)
    app.live.page = {**app.live.page, "url": "https://castle.test/visit", "title": "Visit us",
                     "text": "Open 10 to 5."}
    client.sse("/v1/pages/open", body)
    req = seen[-1]
    assert req.source_url == "https://castle.test/visit"  # the live page's own address is cited
    assert req.source_page == {"url": "https://castle.test/visit", "title": "Visit us", "text": "Open 10 to 5."}
    client.sse("/v1/pages/open", {**body, "source_live": "../x"}, expect=400)
    client.sse("/v1/pages/open", {"concept": "Bodiam", "via": "typed", "source_url": "https://castle.test/"})
    assert seen[-1].source_page is None and seen[-1].source_url == "https://castle.test/"
