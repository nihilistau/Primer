"""Map jobs: find the places a page names, locate them, store the result with the page.

One worker thread runs jobs in order; locating inside a job runs a few lookups at a time."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Callable

from primer.graph import Graph
from primer.maps import Place
from primer.maps.places import parse_phrase_what, parse_places, phrase_place_messages, place_messages
from primer.prompts import sentence_around

log = logging.getLogger(__name__)

PARALLEL = 4
SHUT_DOWN = "Primer shut down"
TIER_SWITCH = "Primer switched tiers"


class MapService:
    def __init__(self, graph: Graph, fast, geocoder_factory: Callable[[], object | None],
                 deadline_s: float = 30.0) -> None:
        self.graph = graph
        self.fast = fast
        self.geocoder_factory = geocoder_factory
        self.deadline_s = deadline_s
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[str] = set()
        self._refresh: set[str] = set()  # jobs that skip the geocoder's cache read
        self._worker = threading.Thread(target=self._run, name="primer-maps", daemon=True)
        self._worker.start()

    def create(self, node_id: str, phrase: str = "") -> str:
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if node is None or version is None:
            raise ValueError("unknown page")
        return self._enqueue(node_id, version.n, phrase)

    def again(self, map_id: str) -> str:
        old = self.graph.get_map(map_id)
        if old is None:
            raise ValueError("unknown map")
        # a repeat looks the places up afresh, so it can replace a wrong cached pin
        return self._enqueue(old["node_id"], old["n"], old["phrase"], refresh=True)

    def get(self, map_id: str) -> dict | None:
        return self.graph.get_map(map_id)

    def shutdown(self, reason: str = SHUT_DOWN) -> None:
        """Stop without draining: jobs not yet finished are marked failed with `reason`."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        self._queue.put(None)
        for mid in pending:
            self._fail(mid, reason)
        self._worker.join(timeout=2)

    # internals
    def _enqueue(self, node_id: str, n: int, phrase: str, refresh: bool = False) -> str:
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN)
            mid = self.graph.add_map(node_id, n, phrase)
            self._pending.add(mid)
            if refresh:
                self._refresh.add(mid)
        self._queue.put(mid)
        return mid

    def _fail(self, mid: str, error: str) -> None:
        try:
            self.graph.update_map(mid, status="failed", error=error)
        except Exception:
            log.exception("could not mark map %s failed", mid)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _run(self) -> None:
        while True:
            mid = self._queue.get()
            if mid is None or self._is_closed():
                return
            try:
                self._job(mid)
            except Exception as e:  # a job never kills the worker
                log.exception("map job %s failed", mid)
                self._fail(mid, f"Map failed: {e}")
            finally:
                with self._lock:
                    self._pending.discard(mid)
                    self._refresh.discard(mid)

    def _job(self, mid: str) -> None:
        job = self.graph.get_map(mid)
        node = self.graph.get_node(job["node_id"])
        version = next((v for v in self.graph.versions(job["node_id"]) if v.n == job["n"]), None)
        geo = None
        try:
            with self._lock:
                refresh = mid in self._refresh
            geo = self.geocoder_factory(refresh=True) if refresh else self.geocoder_factory()  # one per job: it caches the book list for its threads
        except Exception:
            log.exception("no geocoder for map %s", mid)
        places = None
        if job["phrase"]:
            found = [Place(name=job["phrase"], what=self._phrase_what(job, node, version))]
            self.graph.update_map(mid, status="locating")
            places = self._locate(found, geo) if geo is not None else found
            if any(p.lat is not None for p in places):
                found = None
            else:  # "map of castles in Wales": the phrase is no place, so map what the page names
                places = None
                self.graph.update_map(mid, status="finding")
        if places is None:
            if node is None or version is None:
                self._fail(mid, "The page is gone")
                return
            try:
                raw = self.fast.complete(place_messages(node.title, version.markdown),
                                         max_tokens=600, temperature=0.2)
                found = parse_places(raw, version.markdown)
            except Exception as e:
                self._fail(mid, f"Couldn't read the places from the page: {e}")
                return
            self.graph.update_map(mid, status="locating")
            places = self._locate(found, geo) if geo is not None else found
        if self._is_closed():  # shutdown already marked this map failed
            return
        self.graph.set_map_places(mid, places)
        self.graph.update_map(mid, status="done")

    def _phrase_what(self, job: dict, node, version) -> str:
        """What a phrase map's place is, so the geocoder can tell "River Avon" here from the
        other ones: the words of a finished map of this page that has the place, else one tiny
        fast-model question about the phrase in its sentence; "" when neither works."""
        phrase = job["phrase"]
        want = phrase.casefold()
        try:
            for m in self.graph.maps_for(job["node_id"]):
                if m["id"] == job["id"] or m["status"] != "done":
                    continue
                full = self.graph.get_map(m["id"])
                for p in (full or {}).get("places") or []:
                    if p["name"].casefold() == want and p["what"]:
                        return p["what"]
        except Exception:
            log.exception("could not read the earlier maps of %s", job["node_id"])
        if node is None or version is None:
            return ""
        try:
            raw = self.fast.complete(
                phrase_place_messages(node.title, sentence_around(version.markdown, phrase), phrase),
                max_tokens=80, temperature=0.2)
            return parse_phrase_what(raw)
        except Exception:
            log.warning("could not describe %r for the map", phrase, exc_info=True)
            return ""

    def _locate(self, found: list[Place], geo) -> list[Place]:
        """Locate up to PARALLEL names at a time, waiting no longer than the deadline. Lookups
        still running then are abandoned (daemon threads): those places stay not located."""
        clock = getattr(geo, "clock", time.monotonic)
        deadline = clock() + self.deadline_s
        end = time.monotonic() + self.deadline_s
        tasks: queue.Queue = queue.Queue()
        results: queue.Queue = queue.Queue()
        stop = threading.Event()
        for item in enumerate(found):
            tasks.put(item)

        def work() -> None:
            while not stop.is_set():
                try:
                    i, p = tasks.get_nowait()
                except queue.Empty:
                    return
                got = p
                if clock() < deadline:
                    try:
                        got = geo.locate(p.name, deadline, p.what)
                        if not got.what:
                            got = replace(got, what=p.what)
                    except Exception:
                        log.exception("locating %r failed", p.name)
                        got = p
                results.put((i, got))

        for k in range(min(PARALLEL, len(found))):
            threading.Thread(target=work, name=f"primer-maps-locate-{k}", daemon=True).start()
        done: dict[int, Place] = {}
        while len(done) < len(found) and not self._is_closed():
            try:
                i, got = results.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                break
            done[i] = got
        stop.set()
        merged: dict[str, Place] = {}  # first of each article keeps its place in the list
        for i, p in enumerate(found):
            p = done.get(i, p)
            key = (p.title or p.name).casefold()
            if key not in merged or (merged[key].lat is None and p.lat is not None):
                merged[key] = p
        values = list(merged.values())
        return ([p for p in values if p.lat is not None and p.lon is not None]
                + [p for p in values if p.lat is None or p.lon is None])
