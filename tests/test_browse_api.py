"""The browse API: /v1/browse, /v1/browse/search, /v1/browse/image, status, pages from a URL.
No network: injected fetchers, a fake kiwix-serve, and the real guard only on literal private IPs."""
import http.client
import json
import urllib.parse

import pytest

from primer.browse.service import BrowseError, BrowseService
from primer.config import EngineSpec, ReferenceConfig, Tier, WebConfig
from primer.grounding import Source
from primer.grounding.kiwix import KiwixClient
from primer.grounding.web import WebError, WebRefused, WebUnsupported
from tests import fake_kiwix_server
from tests.test_server import _grounded

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 30

PAGE = ("<html><head><title>Warwick</title></head><body><main><h1>Warwick Castle</h1>"
        "<p>A <a href='/history'>history</a> and <a href='http://127.0.0.1:8810/v1/status'>"
        "a private link</a>.</p><img src='/pic.png' alt='castle'></main></body></html>")


def html_fetch(pages, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url not in pages:
            raise WebError("the page returned HTTP 404", 404)
        final, body = pages[url]
        return final, "text/html", body
    return fetch


def svc(**kw):
    kw.setdefault("kiwix", None)
    kw.setdefault("web", True)
    kw.setdefault("searcher", None)
    return BrowseService(**kw)


def test_browse_web_page():
    s = svc(fetch_html=html_fetch({"https://example.com/castle": ("https://example.com/castle/", PAGE)}))
    out = s.open("https://example.com/castle")
    assert out["where"] == "web" and out["title"] == "Warwick"
    assert out["url"] == "https://example.com/castle" and out["final_url"] == "https://example.com/castle/"
    assert out["origin_href"] == "https://example.com/castle/"
    assert "# Warwick Castle" in out["markdown"]
    assert [l["url"] for l in out["links"]] == ["https://example.com/history", "http://127.0.0.1:8810/v1/status"]
    assert "![castle](/v1/browse/image?u=https%3A%2F%2Fexample.com%2Fpic.png)" in out["markdown"]
    assert set(out) == {"url", "final_url", "title", "markdown", "links", "where", "origin_href"}


def test_browse_text_plain_is_shown_as_text():
    s = svc(fetch_html=lambda u: (u, "text/plain", "hello <b>world</b>"))
    out = s.open("https://example.com/t.txt")
    assert out["markdown"].startswith("```") and "hello <b>world</b>" in out["markdown"]  # a code block: inert


@pytest.fixture
def kiwix_env():
    wiki = {"Whale": ("Whale", "<html><head><title>Whale</title></head><body><article><h1>Whale</h1>"
                      "<p>See the <a href='Whale_shark'>whale shark</a>.</p>"
                      "<img src='../I/pic.png' alt='a whale'></article></body></html>"),
            "Whale_shark": ("Whale shark", "<html><body><p>A fish.</p></body></html>"),
            "I/pic.png": ("pic", PNG, "image/png")}
    server, url = fake_kiwix_server.serve({"wikipedia_en": wiki})
    yield KiwixClient(url, timeout=2), url
    server.kill()


def test_browse_kiwix_page(kiwix_env):
    kiwix, url = kiwix_env
    s = svc(kiwix=kiwix, web=False)
    out = s.open("kiwix:wikipedia_en/Whale")
    assert out["where"] == "reference" and out["title"] == "Whale"
    assert out["links"] == [{"n": 1, "text": "whale shark", "url": "kiwix:wikipedia_en/Whale_shark"}]
    assert "![a whale](/v1/browse/image?u=kiwix%3Awikipedia_en%2FI%2Fpic.png)" in out["markdown"]
    assert out["origin_href"] == f"{url}/content/wikipedia_en/Whale"
    assert out["url"] == out["final_url"] == "kiwix:wikipedia_en/Whale"
    assert s.image("kiwix:wikipedia_en/I/pic.png") == (PNG, "image/png")
    with pytest.raises(BrowseError) as e:
        s.open("kiwix:wikipedia_en/Missing")
    assert e.value.status == 404
    with pytest.raises(BrowseError) as e:
        svc(kiwix=None).open("kiwix:wikipedia_en/Whale")
    assert e.value.status == 503 and "offline" in str(e.value).lower()


def test_browse_refuses_private():
    s = svc()  # the real guard: literal private addresses are refused before any connection
    for u in ("http://127.0.0.1:8810/v1/status", "http://[::1]/", "http://172.31.0.5/",
              "http://169.254.169.254/"):
        with pytest.raises(BrowseError) as e:
            s.open(u)
        assert e.value.status == 403 and "public" in str(e.value), u
    with pytest.raises(BrowseError) as e:
        s.image("http://127.0.0.1:8810/v1/status")
    assert e.value.status == 403
    for bad in ("ftp://x.org/", "javascript:alert(1)", "", "file:///c:/x"):
        with pytest.raises(BrowseError) as e:
            s.open(bad)
        assert e.value.status == 400, bad


def test_browse_error_statuses():
    def boom(exc):
        def f(url):
            raise exc
        return f
    for exc, status in ((WebRefused("x is not a public address"), 403), (WebError("nope", 404), 404),
                        (WebError("gone", 410), 404), (WebError("the page returned HTTP 500", 500), 502),
                        (WebError("cannot fetch the page: timed out"), 502),
                        (WebUnsupported("unsupported content type image/png"), 415)):
        with pytest.raises(BrowseError) as e:
            svc(fetch_html=boom(exc)).open("https://example.com/")
        assert e.value.status == status, exc


def test_browse_offline_only(kiwix_env):
    kiwix, _ = kiwix_env
    s = svc(kiwix=kiwix, web=False, fetch_html=html_fetch({}))
    with pytest.raises(BrowseError) as e:
        s.open("https://example.com/")
    assert e.value.status == 503 and "web is off" in str(e.value).lower()
    with pytest.raises(BrowseError) as e:
        s.image("https://example.com/p.png")
    assert e.value.status == 503
    # search words go to the offline encyclopedia
    assert [r["url"] for r in s.search("whale", "")][:1] == ["kiwix:wikipedia_en/Whale"]
    with pytest.raises(BrowseError) as e:
        s.search("whale", "web")
    assert e.value.status == 503


class _Searcher:
    def search(self, q, n=5):
        return [{"title": "Castle", "url": "https://example.com/c", "snippet": "A castle"},
                {"title": "Bad", "url": "javascript:alert(1)", "snippet": ""}]


def test_browse_search_web_and_reference(kiwix_env):
    kiwix, _ = kiwix_env
    s = svc(kiwix=kiwix, web=True, searcher=_Searcher())
    assert s.search("castle", "") == [{"title": "Castle", "url": "https://example.com/c",
                                       "snippet": "A castle"}]  # web is the default when it is on
    ref = s.search("whale", "reference")
    assert ref[0] == {"title": "Whale", "url": "kiwix:wikipedia_en/Whale", "snippet": "Whale snippet"}
    with pytest.raises(BrowseError) as e:
        s.search("x", "elsewhere")
    assert e.value.status == 400
    with pytest.raises(BrowseError) as e:
        s.search("  ", "web")
    assert e.value.status == 400
    with pytest.raises(BrowseError) as e:
        svc(kiwix=None, web=False).search("x", "")
    assert e.value.status == 503


def test_image_proxy_sniffs():
    table = {"https://e.com/a.png": ("image/png", PNG), "https://e.com/a.jpg": ("image/jpeg", JPEG),
             "https://e.com/a.gif": ("image/gif", GIF), "https://e.com/a.webp": ("image/webp", WEBP),
             # right type, SVG / HTML bytes: refused
             "https://e.com/x.png": ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
             "https://e.com/h.png": ("image/png", b"<html><script>alert(1)</script></html>"),
             "https://e.com/s.svg": ("image/svg+xml", b"<svg/>"),
             "https://e.com/odd": ("text/html", PNG)}

    def fetch_bytes(url):
        ctype, body = table[url]
        if not ctype.startswith("image/"):  # the real fetcher refuses a non-image type
            raise WebUnsupported("unsupported content type " + ctype)
        return url, ctype, body

    s = svc(fetch_bytes=fetch_bytes)
    assert s.image("https://e.com/a.png") == (PNG, "image/png")
    assert s.image("https://e.com/a.jpg")[1] == "image/jpeg"
    assert s.image("https://e.com/a.gif")[1] == "image/gif"
    assert s.image("https://e.com/a.webp")[1] == "image/webp"
    for u in ("https://e.com/x.png", "https://e.com/h.png", "https://e.com/s.svg", "https://e.com/odd"):
        with pytest.raises(BrowseError) as e:
            s.image(u)
        assert e.value.status == 415, u


def _spec():
    return EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")


def _env(tmp_path, g, tier, **kw):
    app, client, close = _grounded(tmp_path, g, tier)
    app.browse = BrowseService(**kw)
    return app, client, close


def _raw_get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def test_image_route_headers_and_statuses(tmp_path, g):
    tier = Tier("t", _spec(), _spec())

    def fetch_bytes(url):
        if "boom" in url:
            raise WebError("cannot fetch the picture: refused")
        return url, "image/png", PNG if url.endswith("ok.png") else b"<svg/>"

    app, client, close = _env(tmp_path, g, tier, kiwix=None, web=True, searcher=None,
                              fetch_bytes=fetch_bytes)
    port = client.port
    try:
        def q(u):
            return "/v1/browse/image?u=" + urllib.parse.quote(u, safe="")
        status, headers, body = _raw_get(port, q("https://e.com/ok.png"))
        assert status == 200 and body == PNG
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == "default-src 'none'"
        assert headers["Cache-Control"] == "max-age=3600"
        status, headers, body = _raw_get(port, q("https://e.com/bad.png"))
        assert status == 415 and "error" in json.loads(body)
        assert _raw_get(port, q("https://e.com/boom.png"))[0] == 502
        assert _raw_get(port, q("ftp://e.com/x.png"))[0] == 400
        assert _raw_get(port, "/v1/browse/image")[0] == 400
        assert _raw_get(port, q("https://e.com/ok.png"), host="evil.example")[0] == 403
    finally:
        close()


def test_browse_routes(tmp_path, g):
    tier = Tier("t", _spec(), _spec())
    pages = {"https://example.com/castle": ("https://example.com/castle", PAGE)}
    app, client, close = _env(tmp_path, g, tier, kiwix=None, web=True, searcher=_Searcher(),
                              fetch_html=html_fetch(pages))

    def q(u):
        return urllib.parse.quote(u, safe="")
    try:
        out = client.get("/v1/browse?u=" + q("https://example.com/castle"))
        assert out["title"] == "Warwick" and out["where"] == "web"
        assert "error" in client.get("/v1/browse?u=" + q("ftp://x/"), expect=400)
        assert "error" in client.get("/v1/browse", expect=400)
        assert "404" in client.get("/v1/browse?u=" + q("https://example.com/none"), expect=404)["error"]
        res = client.get("/v1/browse/search?q=castle&where=web")
        assert res["results"][0]["url"] == "https://example.com/c"
        client.get("/v1/browse/search?q=castle&where=reference", expect=503)
        client.get("/v1/browse/search?q=&where=web", expect=400)
        client.get("/v1/browse/search?q=a&where=moon", expect=400)
    finally:
        close()


def test_search_and_cache_robustness(kiwix_env):
    class Bad:
        def search(self, q, n=5):
            raise RuntimeError("engine bug")
    with pytest.raises(BrowseError) as e:
        svc(searcher=Bad()).search("x", "web")
    assert e.value.status == 502 and "engine bug" in str(e.value)
    s = svc(fetch_html=html_fetch({"https://e.com/a": ("https://e.com/a", PAGE)}))
    first = s.open("https://e.com/a")
    first["links"].append("x")
    first["links"][0]["text"] = "mutated"
    again = s.open("https://e.com/a")
    assert len(again["links"]) == 2 and again["links"][0]["text"] != "mutated"


def test_too_large_picture_same_status_web_and_kiwix(kiwix_env, monkeypatch):
    from primer.browse import service as sv
    kiwix, _ = kiwix_env
    monkeypatch.setattr(sv, "IMAGE_MAX", 10)
    with pytest.raises(BrowseError) as a:
        svc(kiwix=kiwix, web=False).image("kiwix:wikipedia_en/I/pic.png")

    def big(url):
        raise sv.WebTooLarge("the picture is too large")
    with pytest.raises(BrowseError) as b:
        svc(fetch_bytes=big).image("https://e.com/x.png")
    assert a.value.status == b.value.status == 413


def test_browse_cache():
    calls = []
    now = [1000.0]
    pages = {f"https://e.com/{i}": (f"https://e.com/{i}", PAGE) for i in range(60)}
    s = svc(fetch_html=html_fetch(pages, calls), clock=lambda: now[0])
    s.open("https://e.com/0")
    s.open("https://e.com/0#frag")  # same address once the fragment is dropped
    assert calls == ["https://e.com/0"]
    now[0] += 599
    s.open("https://e.com/0")
    assert len(calls) == 1
    now[0] += 2  # past 600 s
    s.open("https://e.com/0")
    assert len(calls) == 2
    for i in range(1, 51):  # 50 more pages: the oldest (0) is evicted, the cache holds 50
        s.open(f"https://e.com/{i}")
    n = len(calls)
    s.open("https://e.com/50")
    assert len(calls) == n
    s.open("https://e.com/0")
    assert len(calls) == n + 1
    # a failure is not cached
    missing = []
    s2 = svc(fetch_html=html_fetch({}, missing))
    for _ in range(2):
        with pytest.raises(BrowseError):
            s2.open("https://e.com/missing")
    assert len(missing) == 2


def test_status_browse_available(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    try:
        tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
        app, client, close = _grounded(tmp_path, g, tier)
        try:
            assert client.get("/v1/status")["browse_available"] == {
                "ok": True, "reason": "", "web": True, "reference": True}
        finally:
            close()
        off = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url),
                   web=WebConfig(search="off"))
        (tmp_path / "b").mkdir()
        app, client, close = _grounded(tmp_path / "b", g, off)
        try:
            assert client.get("/v1/status")["browse_available"] == {
                "ok": True, "reason": "", "web": False, "reference": True}
            kiwix.kill()
            app._forget_reference()
            b = client.get("/v1/status")["browse_available"]
            assert b["ok"] is False and b["web"] is False and b["reference"] is False and b["reason"]
        finally:
            close()
    finally:
        kiwix.kill()
    (tmp_path / "c").mkdir()
    app, client, close = _grounded(tmp_path / "c", g, Tier("t", _spec(), _spec(), web=WebConfig(search="off")))
    try:
        b = client.get("/v1/status")["browse_available"]
        assert b["ok"] is False and b["web"] is False and b["reference"] is False
        assert "Web" in b["reason"] and "offline" in b["reason"]
    finally:
        close()


# --- pages from a URL ---------------------------------------------------------

class _UrlGrounder:
    def __init__(self, sources=(), note=""):
        self.sources, self.note, self.urls = list(sources), note, []

    def gather(self, mode, query, status):
        raise AssertionError("a page from a URL must not search")

    def gather_url(self, u):
        self.urls.append(u)
        return self.sources, self.note


def test_open_page_from_url(tmp_path, g):
    tier = Tier("t", _spec(), _spec())
    app, client, close = _grounded(tmp_path, g, tier)
    web_src = [Source(1, "Warwick Castle", "web", "A castle in England.", url="https://e.com/w",
                      where="e.com")]
    ref_src = [Source(1, "Whale", "reference", "Marine mammals.", book="wikipedia_en", path="Whale",
                      where="wikipedia en")]
    try:
        gr = app.pages.grounder = _UrlGrounder(web_src)
        ev = dict(client.sse("/v1/pages/open", {"concept": "Warwick Castle", "via": "typed",
                                                "source_url": "https://e.com/w#top"}))
        assert gr.urls == ["https://e.com/w"]  # cleaned: no fragment
        assert ev["node"]["source_mode"] == "web" and ev["done"]["source_mode"] == "web"
        assert ev["sources"]["sources"] == [{"n": 1, "title": "Warwick Castle", "origin": "web",
                                             "where": "e.com"}]
        assert "A castle in England." in app.pages.text.calls[-1][1]["content"]
        gr2 = app.pages.grounder = _UrlGrounder(ref_src)
        ev = dict(client.sse("/v1/pages/open", {"concept": "Whale", "via": "typed",
                                                "source_url": "kiwix:wikipedia_en/Whale"}))
        assert gr2.urls == ["kiwix:wikipedia_en/Whale"] and ev["done"]["source_mode"] == "reference"
        # a failure: a note, written from the model's own knowledge
        app.pages.grounder = _UrlGrounder([], "That page couldn't be read (x); written from the model's own knowledge.")
        ev = dict(client.sse("/v1/pages/open", {"concept": "Other thing", "via": "typed",
                                                "source_url": "https://e.com/z"}))
        assert ev["done"]["source_mode"] == "storyteller" and "couldn't be read" in ev["done"]["note"]
        # validation
        for bad in ("ftp://x/y", "javascript:alert(1)", 7):
            err = client.post("/v1/pages/open", {"concept": "x", "via": "typed", "source_url": bad}, expect=400)
            assert err["error"], bad
        # an empty source_url is absent: an ordinary page
        n = len(gr2.urls)
        client.sse("/v1/pages/open", {"concept": "Plain", "via": "typed", "source_url": ""})
        assert len(gr2.urls) == n
    finally:
        close()


def test_browse_routes_refuse_private_with_the_real_guard(tmp_path, g):
    app, client, close = _env(tmp_path, g, Tier("t", _spec(), _spec()), kiwix=None, web=True, searcher=None)
    try:
        u = urllib.parse.quote("http://127.0.0.1:8810/v1/status", safe="")
        assert "public" in client.get("/v1/browse?u=" + u, expect=403)["error"]
        assert "public" in client.get("/v1/browse/image?u=" + u, expect=403)["error"]
    finally:
        close()


def test_peer_gone_probes_the_socket():
    import socket
    from primer.server import _peer_gone
    a, b = socket.socketpair()
    try:
        assert _peer_gone(a) is False
        b.sendall(b"x")
        assert _peer_gone(a) is False  # unread data is a live peer, and is not consumed
        assert a.recv(1) == b"x"
        b.close()
        assert _peer_gone(a) is True
    finally:
        a.close()
    assert _peer_gone(a) is True  # a closed socket is gone
