import io
import json
import threading
from pathlib import Path

from primer.graph import Graph
from primer.grounding.kiwix import KiwixClient
from primer.maps import Place
from primer.maps.geocode import Geocoder, parse_geo
from tests.fake_kiwix_server import serve

FIXTURE = (Path(__file__).parent / "warwick_infobox.html").read_text(encoding="utf-8")
NOGEO = "<html><body><p>Camelot is legendary.</p></body></html>"


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.headers.append(dict(req.header_items()))
        body = self.replies.pop(0)
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(json.dumps(body).encode())


def _coords(title, lat, lon, desc="a place"):
    return {"query": {"pages": {"1": {"title": title, "description": desc,
                                       "coordinates": [{"lat": lat, "lon": lon}]}}}}


def _graph(tmp_path):
    return Graph(tmp_path / "g.sqlite")


def _kiwix(books):
    server, url = serve(books)
    return server, KiwixClient(url)


def test_parse_geo_real_infobox():
    assert parse_geo(FIXTURE) == (52.279444444444444, -1.5847222222222224)


def test_parse_geo_rejects_invalid():
    assert parse_geo('<a href="geo:0,0">x</a>') is None
    assert parse_geo('<a href="geo:91,0">x</a>') is None
    assert parse_geo('<a href="geo:10,181">x</a>') is None
    assert parse_geo('<a href="geo:abc,def">x</a>') is None
    assert parse_geo("<p>nothing</p>") is None
    # an invalid first link does not stop a later valid one
    assert parse_geo('<a href="geo:99,0">a</a><a href="geo:48.85,2.35">b</a>') == (48.85, 2.35)


def test_geocode_reference_exact_title(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"Warwick_Castle_Park": ("Warwick Castle Park", NOGEO),
                                          "Warwick_Castle": ("Warwick Castle", FIXTURE)}})
    try:
        g = Geocoder(_graph(tmp_path), kx, web=False)
        p = g.locate("warwick castle", 1e12)
        assert (p.lat, p.lon) == (52.279444444444444, -1.5847222222222224)
        assert p.title == "Warwick Castle" and p.source == "reference"
    finally:
        server.kill()


def test_geocode_no_coordinates_not_located(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"Camelot": ("Camelot", NOGEO)}})
    try:
        g = Geocoder(_graph(tmp_path), kx, web=False)
        assert g.locate("Camelot", 1e12) == Place(name="Camelot")
        assert g.locate("Nowhere", 1e12) == Place(name="Nowhere")
        assert g.graph.cached_place("camelot") is None  # misses are not cached
    finally:
        server.kill()


def test_geocode_kiwix_down_falls_through(tmp_path):
    kx = KiwixClient("http://127.0.0.1:9", timeout=0.5)
    op = FakeOpener([_coords("Paris", 48.85, 2.35)])
    p = Geocoder(_graph(tmp_path), kx, web=True, opener=op).locate("Paris", 1e12)
    assert p.source == "web" and p.lat == 48.85


def test_geocode_web_fallback(tmp_path):
    op = FakeOpener([{"query": {"pages": {"-1": {"title": "Kenilworth Cstle", "missing": ""}}}},
                     {"query": {"search": [{"title": "Kenilworth Castle"}]}},
                     _coords("Kenilworth Castle", 52.3453, -1.5834, "ruined castle")])
    g = Geocoder(_graph(tmp_path), None, web=True, opener=op)
    p = g.locate("Kenilworth Cstle", 1e12)
    assert (p.lat, p.lon, p.title, p.source) == (52.3453, -1.5834, "Kenilworth Castle", "web")
    assert all(u.startswith("https://en.wikipedia.org/w/api.php?") for u in op.urls)
    assert "list=search" in op.urls[1]
    ua = {k.lower(): v for k, v in op.headers[0].items()}["user-agent"]
    assert ua.startswith("Primer/") and "github.com/nihilistau/Primer" in ua
    assert g.graph.cached_place("kenilworth cstle").source == "web"


def test_geocode_web_rejects_invalid_coordinates(tmp_path):
    op = FakeOpener([_coords("Null Island", 0, 0), {"query": {"search": []}}])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Null Island", 1e12).lat is None


def test_geocode_web_errors_are_not_located(tmp_path):
    op = FakeOpener([OSError("down")])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Paris", 1e12) == Place(name="Paris")
    op = FakeOpener(["not an object"])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Paris", 1e12).lat is None


def test_geocode_web_off_never_calls_network(tmp_path):
    op = FakeOpener([])
    g = Geocoder(_graph(tmp_path), None, web=False, opener=op)
    assert g.locate("Paris", 1e12) == Place(name="Paris")
    assert op.urls == []


def test_geocode_cache_hit_skips_backends(tmp_path):
    graph = _graph(tmp_path)
    graph.cache_place("paris", Place(name="Paris", lat=48.85, lon=2.35, title="Paris", source="web"))
    op = FakeOpener([])
    g = Geocoder(graph, KiwixClient("http://127.0.0.1:9", timeout=0.5), web=True, opener=op)
    p = g.locate("The Paris", 1e12)
    assert (p.lat, p.title, p.source, p.name) == (48.85, "Paris", "web", "The Paris")
    assert op.urls == []


def test_geocode_deadline_passed_skips_lookups(tmp_path):
    op = FakeOpener([])
    g = Geocoder(_graph(tmp_path), None, web=True, opener=op, clock=lambda: 100.0)
    assert g.locate("Paris", 50.0) == Place(name="Paris")
    assert op.urls == []


def test_geocode_threads_share_the_graph(tmp_path):
    graph = _graph(tmp_path)
    op = FakeOpener([_coords(f"T{i}", 10 + i, 20) for i in range(8)])
    g = Geocoder(graph, None, web=True, opener=op)
    out = []
    ts = [threading.Thread(target=lambda i=i: out.append(g.locate(f"T{i}", 1e12))) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(out) == 8 and all(p.lat is not None for p in out)


def test_geocode_web_fallback_query_params(tmp_path):
    op = FakeOpener([_coords("Paris", 48.85, 2.35)])
    Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Paris", 1e12)
    assert "redirects=1" in op.urls[0] and "titles=Paris" in op.urls[0]
    assert "prop=coordinates" in op.urls[0] and "description" in op.urls[0]


def test_geocode_reference_starts_with_branch(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"Warwick_Castle": ("Warwick Castle", FIXTURE)}})
    try:
        p = Geocoder(_graph(tmp_path), kx, web=False).locate("Warwick", 1e12)
        assert p.title == "Warwick Castle" and p.source == "reference"
    finally:
        server.kill()


def test_geocode_reference_later_book(tmp_path):
    server, kx = _kiwix({"a_en": {"Other": ("Other", NOGEO)},
                         "b_en": {"Warwick_Castle": ("Warwick Castle", FIXTURE)}})
    try:
        p = Geocoder(_graph(tmp_path), kx, web=False).locate("Warwick Castle", 1e12)
        assert p.source == "reference" and p.lat is not None
    finally:
        server.kill()


def test_geocode_fuzzy_reference_pick_needs_first_word(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"Castle_Rock": ("Castle Rock", FIXTURE)}})
    try:
        # "Cas" is a prefix of "Castle Rock" but not the same first word
        assert Geocoder(_graph(tmp_path), kx, web=False).locate("Cas", 1e12).lat is None
    finally:
        server.kill()


def test_geocode_fuzzy_pick_ignores_leading_the(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"The_Castle_Rock": ("The Castle Rock", FIXTURE)}})
    try:
        assert Geocoder(_graph(tmp_path), kx, web=False).locate("Castle", 1e12).lat is not None
    finally:
        server.kill()


def test_geocode_fuzzy_search_hit_needs_first_word(tmp_path):
    op = FakeOpener([{"query": {"pages": [{"title": "Camelot", "missing": True}]}},
                     {"query": {"search": [{"title": "Camelot (musical)"}]}}])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Avalon", 1e12).lat is None
    assert len(op.urls) == 2  # the unrelated hit was never looked up


def test_geocode_kiwix_down_checked_once(tmp_path):
    class Down:
        calls = 0

        def books(self):
            Down.calls += 1
            from primer.grounding.kiwix import KiwixError
            raise KiwixError("down")

    op = FakeOpener([_coords("A", 1, 2), _coords("B", 3, 4)])
    g = Geocoder(_graph(tmp_path), Down(), web=True, opener=op)
    assert g.locate("A", 1e12).source == "web" and g.locate("B", 1e12).source == "web"
    assert Down.calls == 1


def test_geocode_malformed_book_entry_falls_to_web(tmp_path):
    class Bad:
        def books(self):
            return [{"title": "no name key"}]

    op = FakeOpener([_coords("Paris", 48.85, 2.35)])
    assert Geocoder(_graph(tmp_path), Bad(), web=True, opener=op).locate("Paris", 1e12).source == "web"


def test_geocode_deadline_passes_between_backend_calls(tmp_path):
    now = [0.0]

    class Slow(FakeOpener):
        def open(self, req, timeout=None):
            now[0] += 20.0
            return super().open(req, timeout)

    op = Slow([{"query": {"pages": [{"title": "X", "missing": True}]}}])
    g = Geocoder(_graph(tmp_path), None, web=True, opener=op, clock=lambda: now[0])
    assert g.locate("Xanadu", 10.0) == Place(name="Xanadu")
    assert len(op.urls) == 1  # no search call after the deadline


def test_geocode_deadline_before_article_read(tmp_path):
    now = [0.0]

    class Slow:
        def books(self):
            return [{"name": "b"}]

        def suggest(self, book, q):
            now[0] += 20.0
            return [{"value": "Paris", "path": "Paris"}]

        def article_html(self, *a):
            raise AssertionError("read after the deadline")

    g = Geocoder(_graph(tmp_path), Slow(), web=False, clock=lambda: now[0])
    assert g.locate("Paris", 10.0) == Place(name="Paris")


AVON_BRISTOL = FIXTURE.replace("52.279444444444444,-1.5847222222222224", "51.45,-2.6")
BOOKS_AVON = {"wikipedia_en": {"River_Avon_(Bristol)": ("River Avon (Bristol)", AVON_BRISTOL),
                               "River_Avon_(Warwickshire)": ("River Avon (Warwickshire)", FIXTURE)}}


def test_qualifiers_from_what():
    from primer.maps.geocode import qualifiers
    assert qualifiers("castle in Warwickshire, England") == ["Warwickshire", "England"]
    assert qualifiers("river in the West Midlands") == ["West Midlands"]
    assert qualifiers("Castle near Coventry") == ["Coventry"]
    assert qualifiers("a big castle") == [] and qualifiers("") == []


def test_geocode_what_picks_qualified_article(tmp_path):
    server, kx = _kiwix(BOOKS_AVON)
    try:
        g = Geocoder(_graph(tmp_path), kx, web=False)
        p = g.locate("River Avon", 1e12, "river in Warwickshire")
        assert p.title == "River Avon (Warwickshire)" and p.lat == 52.279444444444444
        # without context the plain name is not poisoned by the qualified hit
        assert g.graph.cached_place("river avon") is None
        assert g.locate("River Avon", 1e12).title == "River Avon (Bristol)"  # first fuzzy pick, as before
    finally:
        server.kill()


def test_geocode_what_qualified_cache_key_is_separate(tmp_path):
    graph = _graph(tmp_path)
    graph.cache_place("river avon", Place(name="River Avon", lat=51.45, lon=-2.6, title="River Avon (Bristol)",
                                          source="reference"))
    server, kx = _kiwix(BOOKS_AVON)
    try:
        g = Geocoder(graph, kx, web=False)
        assert g.locate("River Avon", 1e12, "river in Warwickshire").title == "River Avon (Warwickshire)"
        assert g.locate("River Avon", 1e12).title == "River Avon (Bristol)"
    finally:
        server.kill()


def test_geocode_what_comma_form_on_web(tmp_path):
    op = FakeOpener([{"query": {"pages": [{"title": "Kenilworth (Warwickshire)", "missing": True}]}},
                     _coords("Kenilworth, Warwickshire", 52.34, -1.58)])
    p = Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Kenilworth", 1e12, "town in Warwickshire")
    assert p.title == "Kenilworth, Warwickshire" and p.source == "web"
    assert "Kenilworth+%28Warwickshire%29" in op.urls[0]


def test_geocode_reference_without_coordinates_goes_on_to_web(tmp_path):
    server, kx = _kiwix({"wikipedia_en": {"Camelot": ("Camelot", NOGEO)}})
    try:
        op = FakeOpener([_coords("Camelot", 51.0, -2.5)])
        p = Geocoder(_graph(tmp_path), kx, web=True, opener=op).locate("Camelot", 1e12)
        assert p.source == "web" and p.lat == 51.0
    finally:
        server.kill()


def _page(title, qid=None, missing=False):
    page = {"title": title}
    if missing:
        page["missing"] = True
    if qid:
        page["pageprops"] = {"wikibase_item": qid}
    return {"query": {"pages": [page]}}


def _claims(lat, lon):
    return {"claims": {"P625": [{"mainsnak": {"datavalue": {"value": {"latitude": lat, "longitude": lon}}}}]}}


def test_geocode_wikidata_fallback(tmp_path):
    op = FakeOpener([_page("Warwickshire", "Q23135"), _claims(52.28, -1.58)])
    p = Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Warwickshire", 1e12)
    assert (p.lat, p.lon, p.title, p.source) == (52.28, -1.58, "Warwickshire", "web")
    assert "ppprop=wikibase_item" in op.urls[0] and "pageprops" in op.urls[0]
    assert op.urls[1].startswith("https://www.wikidata.org/w/api.php?")
    assert "action=wbgetclaims" in op.urls[1] and "entity=Q23135" in op.urls[1] and "property=P625" in op.urls[1]
    ua = {k.lower(): v for k, v in op.headers[1].items()}["user-agent"]
    assert ua.startswith("Primer/")


def test_geocode_wikidata_invalid_or_missing_claims(tmp_path):
    op = FakeOpener([_page("Nowhere", "Q1"), _claims(0, 0), {"query": {"search": []}}])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Nowhere", 1e12).lat is None
    op = FakeOpener([_page("Nowhere", "Q1"), {"claims": {}}, {"query": {"search": []}}])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Nowhere", 1e12).lat is None


def test_geocode_wikidata_respects_deadline_and_web_off(tmp_path):
    now = [0.0]

    class Slow(FakeOpener):
        def open(self, req, timeout=None):
            now[0] += 20.0
            return super().open(req, timeout)

    op = Slow([_page("Warwickshire", "Q23135")])
    g = Geocoder(_graph(tmp_path), None, web=True, opener=op, clock=lambda: now[0])
    assert g.locate("Warwickshire", 10.0).lat is None and len(op.urls) == 1


def test_parse_geo_skips_other_globes():
    assert parse_geo('<a href="geo:0.67,23.47?globe=moon">x</a><a href="geo:10,20">y</a>') == (10.0, 20.0)
    assert parse_geo('<a href="geo:0.67,23.47;globe=moon">x</a>') is None
    assert parse_geo('<a href="geo:10,20?globe=earth">x</a>') == (10.0, 20.0)
    moon = ('<a href="//geohack.toolforge.org/geohack.php?pagename=Mare&amp;params=8.5_N_31.4_E_globe:moon">c</a>'
            '<a href="geo:8.5,31.4">c</a>')
    assert parse_geo(moon) is None
    earth = ('<a href="//geohack.toolforge.org/geohack.php?pagename=W&amp;params=52_N_1_W_region:GB">c</a>'
             '<a href="geo:52,-1">c</a>')
    assert parse_geo(earth) == (52.0, -1.0)


def test_geocode_web_skips_other_globes(tmp_path):
    moon = {"query": {"pages": [{"title": "Mare Tranquillitatis", "pageprops": {"wikibase_item": "Q1"},
                                 "coordinates": [{"lat": 8.5, "lon": 31.4, "globe": "moon"}]}]}}
    op = FakeOpener([moon, {"claims": {"P625": [{"mainsnak": {"datavalue": {"value": {
        "latitude": 8.5, "longitude": 31.4, "globe": "http://www.wikidata.org/entity/Q405"}}}}]}},
        {"query": {"search": []}}])
    p = Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Mare Tranquillitatis", 1e12)
    assert p.lat is None


def test_geocode_web_earth_globe_and_missing_globe_pass(tmp_path):
    page = {"query": {"pages": [{"title": "X", "coordinates": [{"lat": 1.0, "lon": 2.0, "globe": "earth"}]}]}}
    assert Geocoder(_graph(tmp_path), None, web=True, opener=FakeOpener([page])).locate("X", 1e12).lat == 1.0
    wd = {"claims": {"P625": [{"mainsnak": {"datavalue": {"value": {
        "latitude": 3.0, "longitude": 4.0, "globe": "http://www.wikidata.org/entity/Q2"}}}}]}}
    op = FakeOpener([_page("Y", "Q9"), wd])
    assert Geocoder(_graph(tmp_path), None, web=True, opener=op).locate("Y", 1e12).lat == 3.0


def test_geocode_refresh_skips_cache_read_but_writes(tmp_path):
    g = _graph(tmp_path)
    Geocoder(g, None, web=True, opener=FakeOpener([_coords("Bristol Avon", 51.4, -2.6)])).locate("River Avon", 1e12)
    assert g.cached_place("river avon").lat == 51.4
    cached = Geocoder(g, None, web=True, opener=FakeOpener([])).locate("River Avon", 1e12)
    assert cached.lat == 51.4  # normal: served from the cache, no request
    fresh = Geocoder(g, None, web=True, opener=FakeOpener([_coords("Avon (Warwickshire)", 52.2, -1.6)]),
                     refresh=True).locate("River Avon", 1e12)
    assert fresh.lat == 52.2
    assert g.cached_place("river avon").lat == 52.2  # the cache entry was replaced


def test_geocode_refresh_miss_forgets_a_stale_pin(tmp_path):
    # a pin cached before a fix (say a lunar crater pinned on Earth) is dropped when Again finds nothing
    graph = _graph(tmp_path)
    graph.cache_place("crater", Place(name="Crater", lat=8.5, lon=31.4, title="Crater", source="web"))
    g = Geocoder(graph, None, web=False, refresh=True)
    assert g.locate("Crater", 1e12).lat is None
    assert graph.cached_place("crater") is None
