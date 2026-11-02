import json
import threading
import time
from dataclasses import replace

import pytest

from primer.maps import Place
from primer.maps.service import MapService
from tests.fakes import FakeEngine

PAGE = "Kenilworth Castle stands near Coventry. Warwick Castle is nearby."
FOUND = json.dumps([{"name": "Kenilworth Castle", "what": "castle in Warwickshire"},
                    {"name": "Coventry", "what": "city"}, {"name": "Camelot", "what": "legend"}])


class FakeGeo:
    """Locates names from a table; a name not in it is unlocated. `clock` is fake time."""

    def __init__(self, table, tick=0.0):
        self.table = table
        self.tick = tick
        self.now = 0.0
        self.calls = []
        self._lock = threading.Lock()

    def clock(self):
        return self.now

    def locate(self, name, deadline, what=""):
        with self._lock:
            self.calls.append(name)
            self.now += self.tick
            late = self.now >= deadline
        if late:
            return Place(name=name)
        return self.table.get(name, Place(name=name))


def loc(name, title="", lat=52.0, lon=-1.5):
    return Place(name=name, lat=lat, lon=lon, title=title or name, source="reference")


@pytest.fixture
def node(g):
    n = g.node_for("Castles", "concept")
    g.add_version(n.id, PAGE, "storyteller", "normal")
    return n


def make(g, fast, geo, **kw):
    return MapService(g, fast, lambda: geo, **kw)


def wait(svc, mid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        m = svc.get(mid)
        if m["status"] in ("done", "failed"):
            return m
        time.sleep(0.01)
    raise AssertionError("map job did not finish")


def test_map_job_finds_and_locates(g, node):
    geo = FakeGeo({"Kenilworth Castle": loc("Kenilworth Castle"), "Coventry": loc("Coventry")})
    svc = make(g, FakeEngine([FOUND]), geo)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and m["error"] is None and m["phrase"] == ""
    names = [p["name"] for p in m["places"]]
    assert names == ["Kenilworth Castle", "Coventry", "Camelot"]  # located first, then not
    assert m["places"][0]["what"] == "castle in Warwickshire"  # the finder's words are kept
    assert m["places"][0]["lat"] == 52.0 and m["places"][2]["lat"] is None


def test_map_job_phrase_skips_model(g, node):
    fast = FakeEngine(["not json"])  # the describing question fails to parse: empty what
    geo = FakeGeo({"Warwick Castle": loc("Warwick Castle")})
    svc = make(g, fast, geo)
    try:
        m = wait(svc, svc.create(node.id, "Warwick Castle"))
    finally:
        svc.shutdown()
    assert len(fast.calls) == 1 and m["phrase"] == "Warwick Castle" and m["places"][0]["what"] == ""
    assert [p["name"] for p in m["places"]] == ["Warwick Castle"] and m["places"][0]["lat"] == 52.0


def test_map_job_no_geocoder_all_unlocated(g, node):
    svc = MapService(g, FakeEngine([FOUND]), lambda: None)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and m["error"] is None
    assert len(m["places"]) == 3 and all(p["lat"] is None for p in m["places"])


def test_map_job_dedupes_by_article(g, node):
    raw = json.dumps([{"name": "Warwick Castle"}, {"name": "Warwick castle"},
                      {"name": "The castle at Warwick"}, {"name": "Coventry"}])
    same = loc("x", title="Warwick Castle")
    geo = FakeGeo({n: replace(same, name=n) for n in
                   ("Warwick Castle", "Warwick castle", "The castle at Warwick")})
    svc = make(g, FakeEngine([raw]), geo)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert [p["name"] for p in m["places"]] == ["Warwick Castle", "Coventry"]


class KGeo(FakeGeo):
    """Locates exactly the first k calls, then the (fake) clock is past the deadline."""

    def __init__(self, k):
        super().__init__({})
        self.k = k

    def locate(self, name, deadline, what=""):
        with self._lock:
            self.calls.append(name)
            if len(self.calls) <= self.k:
                return loc(name)
            self.now = deadline + 1
            return Place(name=name)


def test_map_job_deadline_keeps_located(g, node):
    raw = json.dumps([{"name": f"P{i}"} for i in range(6)])
    svc = make(g, FakeEngine([raw]), KGeo(2), deadline_s=25.0)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and len(m["places"]) == 6
    assert [p["lat"] is not None for p in m["places"]] == [True, True, False, False, False, False]


def test_map_job_blocked_lookup_does_not_hold_the_job(g, node):
    never = threading.Event()

    class Stuck(FakeGeo):
        def locate(self, name, deadline, what=""):
            if name == "Coventry":
                never.wait(30)
            return super().locate(name, deadline, what)

    geo = Stuck({"Kenilworth Castle": loc("Kenilworth Castle")})
    svc = make(g, FakeEngine([FOUND]), geo, deadline_s=0.3)
    t0 = time.time()
    try:
        m = wait(svc, svc.create(node.id), timeout=3)
        took = time.time() - t0
    finally:
        svc.shutdown()
        never.set()
    assert took < 2 and m["status"] == "done"
    assert [p["name"] for p in m["places"]][0] == "Kenilworth Castle" and m["places"][0]["lat"] == 52.0
    assert {p["name"] for p in m["places"]} == {"Kenilworth Castle", "Coventry", "Camelot"}
    assert all(p["lat"] is None for p in m["places"][1:])


def test_map_job_raising_locate_leaves_place_unlocated(g, node):
    class Boom(FakeGeo):
        def locate(self, name, deadline, what=""):
            if name == "Coventry":
                raise RuntimeError("boom")
            return super().locate(name, deadline, what)

    svc = make(g, FakeEngine([FOUND]), Boom({"Kenilworth Castle": loc("Kenilworth Castle")}))
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and len(m["places"]) == 3
    assert [p["lat"] is not None for p in m["places"]] == [True, False, False]


def test_map_job_raising_factory_means_no_geocoder(g, node):
    def factory():
        raise RuntimeError("no kiwix")

    svc = MapService(g, FakeEngine([FOUND]), factory)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and len(m["places"]) == 3 and all(p["lat"] is None for p in m["places"])


def test_shutdown_does_not_revive_a_failed_map(g, node):
    release = threading.Event()
    entered = threading.Event()

    def slow(messages):
        entered.set()
        release.wait(5)
        return FOUND

    svc = make(g, FakeEngine(slow), FakeGeo({"Coventry": loc("Coventry")}))
    mid = svc.create(node.id)
    assert entered.wait(3)
    t = threading.Thread(target=svc.shutdown)
    t.start()
    end = time.time() + 3
    while g.get_map(mid)["status"] != "failed" and time.time() < end:
        time.sleep(0.01)
    release.set()
    t.join(5)
    svc._worker.join(3)
    m = g.get_map(mid)
    assert m["status"] == "failed" and m["error"] == "Primer shut down" and m["places"] == []


def test_dedupe_prefers_located(g, node):
    raw = json.dumps([{"name": "Warwick castle"}, {"name": "Warwick Castle"}])
    geo = FakeGeo({"Warwick Castle": loc("Warwick Castle")})
    svc = make(g, FakeEngine([raw]), geo)
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert len(m["places"]) == 1 and m["places"][0]["lat"] == 52.0


def test_map_job_fast_model_failure_fails(g, node):
    svc = make(g, FakeEngine([FOUND], fail_after=0), FakeGeo({}))
    try:
        m = wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert m["status"] == "failed" and m["error"].startswith("Couldn't read the places from the page: ")
    assert m["places"] == []


def test_maps_restart_marks_failed(g, node):
    a = g.add_map(node.id, 1, "")
    b = g.add_map(node.id, 1, "")
    c = g.add_map(node.id, 1, "")
    g.update_map(b, status="locating")
    g.update_map(c, status="done")
    assert g.fail_unfinished_maps("Primer restarted") == 2
    assert g.get_map(a)["status"] == "failed" and g.get_map(a)["error"] == "Primer restarted"
    assert g.get_map(b)["status"] == "failed" and g.get_map(c)["status"] == "done"


def test_again_creates_new_map(g, node):
    geo = FakeGeo({"Warwick Castle": loc("Warwick Castle")})
    svc = make(g, FakeEngine([FOUND]), geo)
    try:
        first = svc.create(node.id, "Warwick Castle")
        wait(svc, first)
        second = svc.again(first)
        m = wait(svc, second)
    finally:
        svc.shutdown()
    assert second != first and m["phrase"] == "Warwick Castle" and m["node_id"] == node.id
    assert [x["id"] for x in g.maps_for(node.id)] == [second, first]  # newest first


def test_create_validates_node(g):
    svc = make(g, FakeEngine([FOUND]), FakeGeo({}))
    try:
        with pytest.raises(ValueError):
            svc.create("nope")
        n = g.node_for("Empty", "concept")  # no version yet
        with pytest.raises(ValueError):
            svc.create(n.id)
        with pytest.raises(ValueError):
            svc.again("nope")
    finally:
        svc.shutdown()



def test_map_job_passes_what_to_locate(g, node):
    seen = {}

    class Spy(FakeGeo):
        def locate(self, name, deadline, what=""):
            seen[name] = what
            return super().locate(name, deadline, what)

    svc = make(g, FakeEngine([FOUND]), Spy({}))
    try:
        wait(svc, svc.create(node.id))
    finally:
        svc.shutdown()
    assert seen["Kenilworth Castle"] == "castle in Warwickshire"


class WhatGeo(FakeGeo):
    def locate(self, name, deadline, what=""):
        with self._lock:
            self.calls.append((name, what))
        return loc(name)


def test_phrase_map_reuses_what_from_a_finished_map(g, node):
    geo = WhatGeo({})
    fast = FakeEngine([json.dumps([{"name": "Coventry", "what": "city in the West Midlands"}])])
    svc = make(g, fast, geo)
    try:
        wait(svc, svc.create(node.id))
        n_calls = len(fast.calls)
        m = wait(svc, svc.create(node.id, "coventry"))
    finally:
        svc.shutdown()
    assert len(fast.calls) == n_calls  # no extra model question
    assert ("coventry", "city in the West Midlands") in geo.calls
    assert m["places"][0]["what"] == "city in the West Midlands"


def test_phrase_map_asks_the_fast_model_once_in_context(g):
    n = g.node_for("Warwick", "concept")
    g.add_version(n.id, "Warwick stands on the River Avon. It has a castle.", "storyteller", "normal")
    geo = WhatGeo({})
    fast = FakeEngine([json.dumps({"name": "River Avon", "what": "river in Warwickshire"})])
    svc = make(g, fast, geo)
    try:
        m = wait(svc, svc.create(n.id, "River Avon"))
    finally:
        svc.shutdown()
    assert len(fast.calls) == 1
    prompt = json.dumps(fast.calls[0])
    assert "Warwick stands on the River Avon." in prompt and "It has a castle" not in prompt
    assert geo.calls == [("River Avon", "river in Warwickshire")]
    assert m["places"][0]["what"] == "river in Warwickshire"


def test_phrase_map_model_failure_falls_back_to_empty_what(g, node):
    geo = WhatGeo({})
    svc = make(g, FakeEngine([FOUND], fail_after=0), geo)
    try:
        m = wait(svc, svc.create(node.id, "Coventry"))
    finally:
        svc.shutdown()
    assert m["status"] == "done" and geo.calls == [("Coventry", "")]


def test_again_asks_the_geocoder_to_refresh(g, node):
    seen = []

    def factory(refresh=False):
        seen.append(refresh)
        return FakeGeo({})

    svc = MapService(g, FakeEngine([FOUND, FOUND]), factory)
    try:
        first = wait(svc, svc.create(node.id))
        wait(svc, svc.again(first["id"]))
    finally:
        svc.shutdown()
    assert seen == [False, True]


def test_done_map_is_not_failed_by_shutdown(g, node):
    mid = g.add_map(node.id, 1, "")
    g.update_map(mid, status="done")
    g.update_map(mid, status="failed", error="Primer shut down")
    m = g.get_map(mid)
    assert m["status"] == "done" and m["error"] is None


def test_shutdown_reason_is_used(g, node):
    release = threading.Event()
    entered = threading.Event()

    def slow(messages):
        entered.set()
        release.wait(5)
        return FOUND

    svc = make(g, FakeEngine(slow), FakeGeo({}))
    mid = svc.create(node.id)
    assert entered.wait(3)
    t = threading.Thread(target=svc.shutdown, args=("Primer switched tiers",))
    t.start()
    end = time.time() + 3
    while g.get_map(mid)["status"] != "failed" and time.time() < end:
        time.sleep(0.01)
    release.set()
    t.join(5)
    assert g.get_map(mid)["error"] == "Primer switched tiers"


def test_phrase_map_that_pins_nothing_maps_the_page(g, node):
    # "map of castles in Wales": the phrase itself is no place, so the page's places are mapped
    fast = FakeEngine(["not json", FOUND])
    geo = FakeGeo({"Kenilworth Castle": loc("Kenilworth Castle"), "Coventry": loc("Coventry")})
    svc = make(g, fast, geo)
    try:
        m = wait(svc, svc.create(node.id, "castles in Wales"))
    finally:
        svc.shutdown()
    assert m["status"] == "done"
    assert [p["name"] for p in m["places"]] == ["Kenilworth Castle", "Coventry", "Camelot"]


def test_phrase_map_that_pins_the_phrase_does_not_read_the_page(g, node):
    fast = FakeEngine(["not json", FOUND])
    geo = FakeGeo({"Warwick Castle": loc("Warwick Castle")})
    svc = make(g, fast, geo)
    try:
        m = wait(svc, svc.create(node.id, "Warwick Castle"))
    finally:
        svc.shutdown()
    assert len(fast.calls) == 1 and [p["name"] for p in m["places"]] == ["Warwick Castle"]
