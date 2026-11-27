"""primer.live.cdp against the tiny WebSocket server from test_live_ws."""
from __future__ import annotations

import json
import threading
import time

import pytest

from primer.live.cdp import Cdp, CdpError
from primer.live.ws import WebSocket
from tests.test_live_ws import serve  # noqa: F401  (fixture)


def _send(conn, obj):
    conn.send_text(json.dumps(obj))


def _read_json(conn):
    op, _f, data = conn.read_frame()
    assert op == 1
    return json.loads(data)


def _requests(conn):
    """Each JSON request until the client's close frame."""
    while True:
        op, _f, data = conn.read_frame()
        if op == 8:
            return
        assert op == 1
        yield json.loads(data)


def _connect(s):
    return Cdp(WebSocket.connect(s.url, 2))


def test_calls_match_ids_out_of_order(serve):
    first_read = threading.Event()

    def handler(conn):
        a = _read_json(conn)
        first_read.set()
        b = _read_json(conn)
        _send(conn, {"id": b["id"], "result": {"m": b["method"]}})
        _send(conn, {"id": a["id"], "result": {"m": a["method"]}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    out = {}

    def call(name):
        out[name] = cdp.call(name, timeout=2)

    t1 = threading.Thread(target=call, args=("A.one",))
    t1.start()
    assert first_read.wait(3)  # A is the first request the server saw
    t2 = threading.Thread(target=call, args=("B.two",))
    t2.start()
    t1.join(3)
    t2.join(3)
    cdp.close()
    assert out == {"A.one": {"m": "A.one"}, "B.two": {"m": "B.two"}}


def test_session_id_is_sent_and_events_carry_it(serve):
    seen = {}

    def handler(conn):
        m = _read_json(conn)
        seen["msg"] = m
        _send(conn, {"method": "Page.loadEventFired", "params": {"t": 1}, "sessionId": "S1"})
        _send(conn, {"method": "Target.targetCreated", "params": {"x": 2}})
        _send(conn, {"id": m["id"], "result": {}, "sessionId": "S1"})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    events = []
    done = threading.Event()
    cdp.on("Page.loadEventFired", lambda p, sid: events.append(("load", p, sid)))
    cdp.on("Target.targetCreated", lambda p, sid: (events.append(("tgt", p, sid)), done.set()))
    cdp.call("Page.enable", {"a": 1}, session="S1", timeout=2)
    assert done.wait(2)
    cdp.close()
    assert seen["msg"]["sessionId"] == "S1"
    assert seen["msg"]["params"] == {"a": 1}
    assert ("load", {"t": 1}, "S1") in events
    assert ("tgt", {"x": 2}, None) in events


def test_off_removes_listener(serve):
    def handler(conn):
        m = _read_json(conn)
        _send(conn, {"method": "E.x", "params": {}})
        _send(conn, {"id": m["id"], "result": {}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    hits = []

    def fn(p, sid):
        hits.append(1)

    cdp.on("E.x", fn)
    cdp.off("E.x", fn)
    cdp.call("A.b", timeout=2)
    cdp.close()
    assert hits == []


def test_listener_exception_does_not_kill_reader(serve):
    def handler(conn):
        m = _read_json(conn)
        _send(conn, {"method": "E.boom", "params": {}})
        _send(conn, {"method": "E.ok", "params": {}})
        _send(conn, {"id": m["id"], "result": {"ok": True}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    ok = threading.Event()

    def boom(p, sid):
        raise RuntimeError("listener bug")

    cdp.on("E.boom", boom)
    cdp.on("E.ok", lambda p, sid: ok.set())
    assert cdp.call("A.b", timeout=2) == {"ok": True}
    assert ok.wait(2)
    cdp.close()


def test_error_reply_raises_cdp_error(serve):
    def handler(conn):
        m = _read_json(conn)
        _send(conn, {"id": m["id"], "error": {"code": -32601, "message": "no such method"}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    with pytest.raises(CdpError, match="no such method"):
        cdp.call("Nope.nope", timeout=2)
    cdp.close()


def test_dropped_socket_fails_pending_calls_fast(serve):
    def handler(conn):
        _read_json(conn)
        # drop without replying

    s = serve(handler)
    cdp = _connect(s)
    t0 = time.monotonic()
    with pytest.raises(CdpError, match="browser closed"):
        cdp.call("A.b", timeout=10)
    assert time.monotonic() - t0 < 1
    assert cdp.closed.wait(1)
    with pytest.raises(CdpError, match="browser closed"):
        cdp.call("A.c", timeout=1)


def test_call_timeout(serve):
    def handler(conn):
        _read_json(conn)
        try:
            conn.read_frame()
        except (EOFError, OSError):
            pass

    s = serve(handler)
    cdp = _connect(s)
    with pytest.raises(CdpError, match="timed out"):
        cdp.call("A.b", timeout=0.3)
    cdp.close()


def test_large_event_payload(serve):
    big = "A" * 400_000

    def handler(conn):
        m = _read_json(conn)
        _send(conn, {"method": "Page.screencastFrame", "params": {"data": big}, "sessionId": "S"})
        _send(conn, {"id": m["id"], "result": {}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    got = []
    cdp.on("Page.screencastFrame", lambda p, sid: got.append(len(p["data"])))
    cdp.call("A.b", timeout=2)
    cdp.close()
    assert got == [400_000]


def test_listener_may_call_and_gets_its_reply_quickly(serve):
    def handler(conn):
        for m in _requests(conn):
            if m["method"] == "A.b":
                _send(conn, {"method": "E.x", "params": {}})
            _send(conn, {"id": m["id"], "result": {"m": m["method"]}})

    s = serve(handler)
    cdp = _connect(s)
    out = {}
    done = threading.Event()

    def listener(p, sid):
        t0 = time.monotonic()
        out["r"] = cdp.call("Inner.m", timeout=3)
        out["dt"] = time.monotonic() - t0
        done.set()

    cdp.on("E.x", listener)
    cdp.call("A.b", timeout=2)
    assert done.wait(3)
    cdp.close()
    assert out["r"] == {"m": "Inner.m"}
    assert out["dt"] < 1


def test_events_keep_their_order(serve):
    def handler(conn):
        m = _read_json(conn)
        for i in range(50):
            _send(conn, {"method": "E.n", "params": {"i": i}})
        _send(conn, {"id": m["id"], "result": {}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    seen = []
    done = threading.Event()
    cdp.on("E.n", lambda p, sid: (seen.append(p["i"]), len(seen) == 50 and done.set()))
    cdp.call("A.b", timeout=2)
    assert done.wait(2)
    cdp.close()
    assert seen == list(range(50))


def test_concurrent_calls_have_unique_ids_and_whole_frames(serve):
    ids = []

    def handler(conn):
        for m in _requests(conn):  # a torn or interleaved frame would fail here
            ids.append(m["id"])
            _send(conn, {"id": m["id"], "result": {"n": m["params"]["n"]}})

    s = serve(handler)
    cdp = _connect(s)
    errors = []

    def worker(w):
        try:
            for i in range(50):
                n = w * 100 + i
                pad = "p" * 70_000 if i % 25 == 0 else ""
                assert cdp.call("A.b", {"n": n, "pad": pad}, timeout=10) == {"n": n}
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    cdp.close()
    assert errors == []
    assert len(ids) == 1000 and len(set(ids)) == 1000


def test_send_does_not_wait_and_its_reply_is_dropped(serve):
    seen = []

    def handler(conn):
        a = _read_json(conn)
        b = _read_json(conn)
        seen.extend([a, b])
        _send(conn, {"id": a["id"], "result": {"ignored": True}})  # the send's reply
        _send(conn, {"id": b["id"], "result": {"m": b["method"]}})
        conn.read_frame()

    s = serve(handler)
    cdp = _connect(s)
    t0 = time.monotonic()
    cdp.send("Page.screencastFrameAck", {"sessionId": 3}, session="S1")
    assert time.monotonic() - t0 < 0.5
    assert cdp.call("B.two", timeout=2) == {"m": "B.two"}
    assert seen[0]["method"] == "Page.screencastFrameAck" and seen[0]["sessionId"] == "S1"
    cdp.close()
    with pytest.raises(CdpError):
        cdp.send("X.y")
