"""primer.mcp_server: one stable tool over stdio, answering from a running Primer's HTTP API."""
import io
import json
import subprocess
import sys
import threading

import pytest

from primer import mcp_server


def _call(msgs):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _tool(action, args=None, mid=3):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": "primer", "arguments": {"action": action, "args": args or {}}}}


def test_handshake_and_one_stable_tool():
    init, listed = _call([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {}}
    [tool] = listed["result"]["tools"]
    assert tool["name"] == "primer"
    # the schema must not grow with the actions: an action is a free string, help lists them
    assert set(tool["inputSchema"]["properties"]) == {"action", "args"}
    assert "enum" not in tool["inputSchema"]["properties"]["action"]


def test_help_and_bad_calls():
    [h] = _call([_tool("help")])
    assert not h["result"]["isError"] and "page" in h["result"]["content"][0]["text"]
    [bad] = _call([_tool("launch_missiles")])
    assert bad["result"]["isError"] and "help" in bad["result"]["content"][0]["text"]
    [nopage] = _call([_tool("page")])
    assert nopage["result"]["isError"]
    [unknown] = _call([{"jsonrpc": "2.0", "id": 9, "method": "nope"}])
    assert unknown["error"]["code"] == -32601


def test_primer_down_is_a_tool_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("PRIMER_URL", "http://127.0.0.1:1")
    [r] = _call([_tool("status")])
    assert r["result"]["isError"] and "isn't answering" in r["result"]["content"][0]["text"]


def test_reads_a_running_primer(client, app_parts, monkeypatch):
    app, _ = app_parts
    monkeypatch.setenv("PRIMER_URL", f"http://127.0.0.1:{client.port}")
    client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})
    node_id = app.graph.node_for("Trebuchet", "concept").id
    [st, page, rd, due, quest] = _call([_tool("status", mid=1), _tool("page", {"id": node_id}, mid=2),
                                        _tool("reader", mid=3), _tool("quiz_due", mid=4), _tool("quest", mid=5)])
    for r in (st, page, rd, due, quest):
        assert not r["result"]["isError"], r["result"]["content"][0]["text"]
    text = page["result"]["content"][0]["text"]
    assert text.startswith("# Trebuchet") and "counterweight" in text
    [missing] = _call([_tool("page", {"id": "no-such-node"})])
    assert missing["result"]["isError"] and "404" in missing["result"]["content"][0]["text"]


def test_runs_as_a_module_over_stdio():
    p = subprocess.run([sys.executable, "-m", "primer.mcp_server"], input=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", capture_output=True, text=True, timeout=30)
    assert json.loads(p.stdout)["result"]["tools"][0]["name"] == "primer"


def test_a_leading_bom_is_not_a_parse_error():
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\ufeff" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"), out)
    assert json.loads(out.getvalue())["result"] == {}



# --- Phase 15: now, trail, find, show, open, lens against a stub of the /v1/tool routes ---------

import http.server  # noqa: E402

TOOL_PINNED = ('{"description": "Look at Primer, his illustrated reader app: what page he has open and what it '
               'says, how he reads, review questions due, his quest. Call with action \\"help\\" for the actions '
               'and their args. A Primer activity row\'s `ref` is a page id for action \\"page\\".", '
               '"inputSchema": {"properties": {"action": {"description": "what to do; \\"help\\" lists the '
               'actions", "type": "string"}, "args": {"description": "the action\'s arguments, e.g. '
               '{\\"id\\": \\"...\\"} for page", "type": "object"}}, "required": ["action"], "type": "object"}, '
               '"name": "primer"}')
PAGE_ID = "39dd" + "0" * 12


class _Stub:
    def __init__(self):
        self.posts, self.gets, self.headers = [], [], []
        self.post_status, self.post_body = 202, {"command": {"id": "a" * 16, "status": "queued"}}
        self.polls = [{"id": "a" * 16, "status": "done",
                       "result": {"node_id": PAGE_ID, "title": "Bodiam Castle", "what": "opened"}}]
        self.now = {"enabled": True, "listening": True, "focus": {"node_id": PAGE_ID, "title": "Bodiam Castle"}}


@pytest.fixture
def stub(monkeypatch):
    st = _Stub()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            st.gets.append(self.path)
            if self.path == "/v1/tool/now":
                self._send(200, st.now)
            elif self.path.startswith("/v1/tool/trail"):
                self._send(200, {"pages": [{"id": PAGE_ID, "title": "Bodiam Castle", "phrase": "moat",
                                            "via": "link", "at": 1}]})
            elif self.path.startswith("/v1/tool/find"):
                self._send(200, {"pages": [{"id": PAGE_ID, "title": "Bodiam Castle"}] * 25})
            elif self.path.startswith("/v1/tool/commands/"):
                self._send(200, st.polls.pop(0) if len(st.polls) > 1 else st.polls[0])
            else:
                self._send(404, {"error": "nope"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            st.posts.append((self.path, body))
            st.headers.append((self.headers.get("Content-Type"), self.headers.get("Origin")))
            self._send(st.post_status, st.post_body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("PRIMER_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    sleeps = []
    monkeypatch.setattr(mcp_server.time, "sleep", sleeps.append)
    st.sleeps = sleeps
    yield st
    srv.shutdown()
    srv.server_close()


def _text(action, args=None):
    [r] = _call([_tool(action, args)])
    return r["result"]["content"][0]["text"], r["result"]["isError"]


def test_tool_schema_is_pinned_byte_for_byte():
    assert json.dumps(mcp_server.TOOL, sort_keys=True) == TOOL_PINNED
    [listed] = _call([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert json.dumps(listed["result"]["tools"][0], sort_keys=True) == TOOL_PINNED


def test_help_marks_the_driving_actions():
    text, err = _text("help")
    assert not err
    lines = text.splitlines()
    for name in ("now", "trail", "find", "show", "open", "lens"):
        assert any(ln.split()[:1] == [name] for ln in lines), name
    mark = "(needs Let Kairos drive Primer)"
    for name in ("show", "open", "lens"):
        i = next(i for i, ln in enumerate(lines) if ln.split()[:1] == [name])
        assert mark in lines[i] or mark in lines[i + 1], name
    for name in ("now", "trail", "find"):
        assert "needs Let Kairos" not in next(ln for ln in lines if ln.split()[:1] == [name])


def test_read_actions(stub):
    text, err = _text("now")
    assert not err and "Bodiam Castle" in text and "allowed" not in text
    stub.now = {"enabled": True, "listening": True, "focus": None}
    assert _text("now")[0] == "He has no page open."
    stub.now = {"enabled": False, "listening": False, "focus": None}
    text, _ = _text("now")
    assert text.startswith("Primer isn't open in a browser right now.") and "Let Kairos drive Primer" in text
    # the tab closed: the page it reported last is stale, so it isn't named
    stub.now = {"enabled": True, "listening": False, "focus": {"node_id": PAGE_ID, "title": "Bodiam Castle"}}
    assert _text("now")[0] == "Primer isn't open in a browser right now."
    text, _ = _text("trail")
    assert "Bodiam Castle (page " + PAGE_ID + ")" in text
    assert stub.gets[-1] == "/v1/tool/trail?limit=20"
    _text("trail", {"limit": 500})
    assert stub.gets[-1] == "/v1/tool/trail?limit=100"
    _text("trail", {"limit": 3})
    assert stub.gets[-1] == "/v1/tool/trail?limit=3"
    assert _text("trail", {"limit": "many"})[1]
    text, _ = _text("find", {"q": "bodi am"})
    assert stub.gets[-1] == "/v1/tool/find?q=bodi%20am"
    assert len(text.splitlines()) == 20                      # capped at 20
    assert _text("find")[1]


def test_open_waits_for_done_and_answers_briefly(stub):
    stub.polls = [{"id": "a" * 16, "status": "queued"}, {"id": "a" * 16, "status": "delivered"},
                  {"id": "a" * 16, "status": "done",
                   "result": {"node_id": PAGE_ID, "title": "Bodiam Castle", "what": "opened"}}]
    text, err = _text("open", {"id": PAGE_ID})
    assert not err and text == "Opened *Bodiam Castle* (page 39dd…)"
    assert stub.posts == [("/v1/tool/open", {"id": PAGE_ID})]
    assert stub.headers == [("application/json", None)]       # JSON Content-Type, no Origin
    assert stub.sleeps == [0.5, 0.5]
    assert _text("open")[1]
def _done(what, title="Bodiam Castle"):
    res = {"node_id": PAGE_ID, "what": what}
    if title is not None:
        res["title"] = title
    return [{"id": "a" * 16, "status": "done", "result": res}]


def test_lens_and_show_send_the_right_bodies(stub):
    stub.polls = _done("map")
    assert _text("lens", {"kind": "map"}) == ("Started a map of *Bodiam Castle*; it will appear in Primer", False)
    assert stub.posts[-1] == ("/v1/tool/lens", {"kind": "map"})
    _text("lens", {"kind": "Diagram", "id": PAGE_ID})
    assert stub.posts[-1] == ("/v1/tool/lens", {"kind": "diagram", "id": PAGE_ID})
    assert _text("lens", {"kind": "hologram"})[1] and _text("lens")[1]
    _text("show", {"text": "Hello"})
    assert stub.posts[-1] == ("/v1/tool/show", {"text": "Hello"})
    assert _text("show", {"text": "  "})[1]


@pytest.mark.parametrize("what,expected", [
    ("opened", "Opened *Bodiam Castle* (page 39dd…)"),
    ("writing", "Primer is writing *Bodiam Castle*; it's open in the Reader"),
    ("picture", "Started a picture of *Bodiam Castle*; it will appear in Primer"),
    ("video", "Started a clip of *Bodiam Castle*; it will appear in Primer"),
    ("map", "Started a map of *Bodiam Castle*; it will appear in Primer"),
    ("timeline", "Started a timeline of *Bodiam Castle*; it will appear in Primer"),
    ("diagram", "Started a diagram of *Bodiam Castle*; it will appear in Primer"),
    ("story", "Started a story about *Bodiam Castle*; it will appear in Primer"),
    ("listen", "Reading *Bodiam Castle* aloud in Primer"),
    ("surprise", "Done in Primer"),
    (None, "Done in Primer"),
])
def test_every_what_has_its_exact_answer(stub, what, expected):
    stub.polls = _done(what)
    assert _text("show", {"text": "x"}) == (expected, False)
    stub.polls = _done(what)
    assert _text("lens", {"kind": "map"}) == (expected, False)
    stub.polls = _done(what)
    # open always says "Opened", with the short page id
    assert _text("open", {"id": PAGE_ID}) == ("Opened *Bodiam Castle* (page 39dd…)", False)


def test_missing_title_is_done_in_primer(stub):
    stub.polls = _done("map", title=None)
    assert _text("lens", {"kind": "map"}) == ("Done in Primer", False)
    stub.polls = _done("opened", title="")
    assert _text("open", {"id": PAGE_ID}) == ("Done in Primer", False)


def test_primer_going_away_mid_poll_is_an_error(stub, monkeypatch):
    real = mcp_server._get
    calls = []

    def flaky(path, timeout=10.0):
        calls.append(path)
        if path.startswith("/v1/tool/commands/"):
            raise mcp_server.PrimerDown("Primer isn't answering at x (gone). Is it running?")
        return real(path, timeout)

    monkeypatch.setattr(mcp_server, "_get", flaky)
    text, err = _text("open", {"id": PAGE_ID})
    assert err and "isn't answering" in text


def test_lens_with_an_unknown_id_is_a_404(stub):
    stub.post_status, stub.post_body = 404, {"error": "I can't find that page"}
    assert _text("lens", {"kind": "map", "id": "ffffffffffffffff"}) == ("I can't find that page", True)


def test_odd_arguments_are_clean_errors(stub):
    for bad in (["x"], "x", 5):
        with pytest.raises(ValueError):
            mcp_server.act("open", bad)
    [r] = _call([{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "primer", "arguments": {"action": "open", "args": ["x"]}}}])
    assert r["result"]["isError"]
    [r] = _call([{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "primer", "arguments": {"action": 7}}}])
    assert r["result"]["isError"] and "unknown action" in r["result"]["content"][0]["text"]
    assert _text("trail", {"limit": 1e999})[1]


def test_find_with_no_match(stub, monkeypatch):
    real = mcp_server._get
    monkeypatch.setattr(mcp_server, "_get",
                        lambda p, timeout=10.0: {"pages": []} if p.startswith("/v1/tool/find") else real(p, timeout))
    assert _text("find", {"q": "zzz"}) == ("No page he has read has zzz in its title.", False)




def test_a_failed_command_returns_its_error(stub):
    stub.polls = [{"id": "a" * 16, "status": "failed", "error": "Primer was closed"}]
    assert _text("open", {"id": PAGE_ID}) == ("Primer was closed", True)


def test_waiting_stops_at_ninety_seconds(stub, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mcp_server.time, "sleep", lambda s: (stub.sleeps.append(s), clock.__setitem__(0, clock[0] + s)))
    stub.polls = [{"id": "a" * 16, "status": "delivered"}]
    assert _text("lens", {"kind": "picture"}) == ("Primer hasn't finished yet; it's still working on it", True)
    assert 90 <= sum(stub.sleeps) <= 91
    assert stub.sleeps[:10] == [0.5] * 10 and set(stub.sleeps[10:]) == {1.0}


@pytest.mark.parametrize("code,msg", [(403, "Turn on Let Kairos drive Primer first"),
                                      (409, "Primer is busy with another command"),
                                      (429, "Too many commands; wait a minute"),
                                      (404, "No such page")])
def test_driving_errors_come_back_as_error_text(stub, code, msg):
    stub.post_status, stub.post_body = code, {"error": msg}
    assert _text("open", {"id": PAGE_ID}) == (msg, True)
    assert stub.gets == []                                    # never polled
