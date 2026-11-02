"""Locating a place name: cache, offline Wikipedia (kiwix), then Wikipedia's API. Never guesses."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import replace

from primer.graph import Graph, normalize_concept
from primer.grounding.grounder import plain_title
from primer.grounding.kiwix import KiwixClient, KiwixError
from primer.maps import USER_AGENT, Place

log = logging.getLogger(__name__)

_API = "https://en.wikipedia.org/w/api.php"
_TIMEOUT = 10.0
_WIKIDATA = "https://www.wikidata.org/w/api.php"
_QUALIFIER = re.compile(
    r"(?:\b(?:in|of|near|at|by)\s+|,\s*)((?:the\s+)?[A-Z][^\s,;.()]*(?:\s+[A-Z][^\s,;.()]*)*)")
_MAX_QUALIFIERS = 2
_GEO = re.compile(r'href="geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)([^"]*)"')
_GLOBE = re.compile(r"globe[:=]\s*([A-Za-z]+)", re.IGNORECASE)
_EARTH_ENTITY = "http://www.wikidata.org/entity/Q2"


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _bare(text: str) -> str:
    """Casefolded, without a leading "the"."""
    t = " ".join(text.casefold().split())
    return t[4:] if t.startswith("the ") and len(t) > 4 else t


def _first_word(text: str) -> str:
    return (_bare(text).split() or [""])[0]


def qualifiers(what: str) -> list[str]:
    """Proper-noun places in a finder description: "castle in Warwickshire, England" -> both."""
    out: list[str] = []
    for m in _QUALIFIER.finditer(what or ""):
        q = m.group(1)
        if q.lower().startswith("the "):
            q = q[4:]
        if q and q not in out:
            out.append(q)
    return out[:_MAX_QUALIFIERS]


def _candidates(name: str, quals: list[str]) -> list[str]:
    return [t for q in quals for t in (f"{name} ({q})", f"{name}, {q}")]


def _earth(globe) -> bool:
    return not globe or str(globe).strip().casefold() == "earth"


def parse_geo(html: str) -> tuple[float, float] | None:
    """The first valid geo:LAT,LON link (Wikipedia's coordinates template) on Earth.

    Not verifiable offline (no ZIM of a Moon article here): Wikipedia's {{coord}} puts a non-Earth
    body in its GeoHack link as "params=...globe:moon", and a geo: URI may carry a "globe"
    parameter. A geo: link with a non-Earth globe is skipped, and so is the whole page when its
    GeoHack params name another body (any geo: link there would be that body's, not Earth's)."""
    html = html or ""
    for g in re.finditer(r"geohack[^\"']*?params=[^\"']*", html):
        gm = _GLOBE.search(g.group(0))
        if gm and not _earth(gm.group(1)):
            return None
    for m in _GEO.finditer(html):
        gm = _GLOBE.search(m.group(3) or "")
        if gm and not _earth(gm.group(1)):
            continue
        lat, lon = float(m.group(1)), float(m.group(2))
        if _valid(lat, lon):
            return lat, lon
    return None


class Geocoder:
    """Thread-safe: it holds no state of its own, and the graph serialises its own access."""

    def __init__(self, graph: Graph, kiwix: KiwixClient | None, web: bool, opener=None,
                 clock=time.monotonic, refresh: bool = False) -> None:
        self.graph = graph
        self.refresh = refresh  # skip the cache read (a fresh lookup still writes, replacing the entry)
        self.kiwix = kiwix
        self.web = web
        self.opener = opener or urllib.request.build_opener()
        self.clock = clock
        self._lock = threading.Lock()
        self._books: list[str] | None = None  # known once per Geocoder (one map job)
        self._kiwix_down = False

    def locate(self, name: str, deadline: float, what: str = "") -> Place:
        """A Place with coordinates when an article gave them, else Place(name) (not located).

        `what` (the finder's "castle in Warwickshire") disambiguates: "Name (Warwickshire)" and
        "Name, Warwickshire" are tried before the plain name."""
        try:
            return self._locate(name, deadline, what)
        except Exception:  # a map job must finish, whatever one lookup does
            log.exception("locating %r failed", name)
            return Place(name=name)

    def _locate(self, name: str, deadline: float, what: str) -> Place:
        base = normalize_concept(name)
        if not base:
            return Place(name=name)
        quals = qualifiers(what)
        # a qualified lookup caches under its own key, so the plain name is never poisoned
        key = "|".join([base] + [q.casefold() for q in quals])
        cached = None if self.refresh else self.graph.cached_place(key)
        if cached:
            return replace(cached, name=name)
        cands = _candidates(name, quals)
        found = None
        if self.kiwix is not None and self.clock() < deadline:
            found = self._reference(name, deadline, cands)
        if found is None and self.web and self.clock() < deadline:
            found = self._web(name, deadline, cands)
        if found is None:
            if self.refresh:  # Again found nothing: a pin cached before can't be trusted either
                self.graph.forget_place(key)
            return Place(name=name)
        place = Place(name=name, lat=found[0], lon=found[1], title=found[2], source=found[3])
        self.graph.cache_place(key, place)
        return place

    def _book_names(self) -> list[str] | None:
        """The books, fetched once; None once kiwix has failed (then the reference is skipped)."""
        with self._lock:
            if self._kiwix_down:
                return None
            if self._books is None:
                try:
                    self._books = [b["name"] for b in self.kiwix.books()]
                except KiwixError as exc:
                    self._kiwix_down = True
                    log.warning("the offline reference is not answering: %s", exc)
                    return None
            return self._books

    # offline reference: the qualified titles (exact), then the plain name: an exact title, else a
    # title starting with it (same first word)
    def _reference(self, name: str, deadline: float, cands: list[str]):
        kx = self.kiwix
        try:
            books = self._book_names() or []
            for cand, fuzzy in [(c, False) for c in cands] + [(name, True)]:
                want = cand.strip().casefold()
                for book in books:
                    if self.clock() >= deadline:
                        return None
                    hits = [(plain_title(e.get("value") or e.get("label") or e["path"]), e["path"])
                            for e in kx.suggest(book, cand)]
                    pick = next((h for h in hits if h[0].casefold() == want), None)
                    if pick is None and fuzzy:
                        pick = next((h for h in hits if _bare(h[0]).startswith(_bare(cand))
                                     and _first_word(h[0]) == _first_word(cand)), None)
                    if pick is None or self.clock() >= deadline:
                        continue
                    geo = parse_geo(kx.article_html(book, pick[1]))
                    if geo:
                        return geo[0], geo[1], pick[0], "reference"
        except KiwixError as exc:
            with self._lock:
                self._kiwix_down = True
            log.warning("locating %r in the offline reference failed: %s", name, exc)
        except (KeyError, TypeError, AttributeError) as exc:  # a malformed answer
            log.warning("the offline reference sent something unexpected for %r: %r", name, exc)
        return None

    # Wikipedia and Wikidata APIs
    def _get_json(self, base: str, **params) -> dict:
        req = urllib.request.Request(f"{base}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": USER_AGENT})
        with self.opener.open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise ValueError("unexpected reply")
        return data

    def _api(self, **params) -> dict:
        return self._get_json(_API, format="json", formatversion="2", **params)

    def _wiki_page(self, title: str):
        """(title, coordinates or None, wikidata id or None) of an existing article, else None."""
        data = self._api(action="query", prop="coordinates|description|pageprops", ppprop="wikibase_item",
                         redirects="1", titles=title)
        pages = (data.get("query") or {}).get("pages") or []
        if isinstance(pages, dict):  # formatversion 1 shape
            pages = list(pages.values())
        for page in pages:
            if not isinstance(page, dict) or page.get("missing") is not None:
                continue
            got = None
            for c in page.get("coordinates") or []:
                if not isinstance(c, dict) or not _earth(c.get("globe")):
                    continue
                try:
                    lat, lon = float(c["lat"]), float(c["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                if _valid(lat, lon):
                    got = (lat, lon)
                    break
            props = page.get("pageprops")
            qid = props.get("wikibase_item") if isinstance(props, dict) else None
            return str(page.get("title") or title), got, qid if isinstance(qid, str) else None
        return None

    def _wikidata_coords(self, qid: str):
        data = self._get_json(_WIKIDATA, action="wbgetclaims", entity=qid, property="P625", format="json")
        for claim in (data.get("claims") or {}).get("P625") or []:
            try:
                v = claim["mainsnak"]["datavalue"]["value"]
                if isinstance(v, dict) and v.get("globe") and v["globe"] != _EARTH_ENTITY:
                    continue
                lat, lon = float(v["latitude"]), float(v["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            if _valid(lat, lon):
                return lat, lon
        return None

    def _wiki_search(self, name: str) -> str | None:
        data = self._api(action="query", list="search", srsearch=name, srlimit="1")
        hits = (data.get("query") or {}).get("search") or []
        if hits and isinstance(hits[0], dict) and isinstance(hits[0].get("title"), str):
            return hits[0]["title"]
        return None

    def _web(self, name: str, deadline: float, cands: list[str]):
        def left() -> bool:
            return self.clock() < deadline

        try:
            existing = None  # the first article found without coordinates: (title, coords, wikidata id)
            for t in [*cands, name]:
                if not left():
                    return None
                page = self._wiki_page(t)
                if page is None:
                    continue
                if page[1]:
                    return page[1][0], page[1][1], page[0], "web"
                if existing is None and page[2]:
                    existing = page
            if existing is None and left():
                title = self._wiki_search(name)
                if title and _first_word(title) == _first_word(name) and left():
                    page = self._wiki_page(title)
                    if page and page[1]:
                        return page[1][0], page[1][1], page[0], "web"
                    existing = page if page and page[2] else None
            if existing is not None and left():  # counties and regions often only have Wikidata coordinates
                got = self._wikidata_coords(existing[2])
                if got:
                    return got[0], got[1], existing[0], "web"
        except (OSError, ValueError, AttributeError) as exc:  # URLError, timeouts, bad JSON, bad shape
            log.warning("locating %r on Wikipedia failed: %s", name, exc)
        return None
