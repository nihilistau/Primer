"""Web searchers and the guarded fetcher. No real internet: fixtures and a local server."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from primer.config import WebConfig
from primer.grounding import web
from primer.grounding.web import WebError, make_searcher, safe_fetch


def public(host):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def mapping(table):
    def resolve(host, port, *a, **kw):
        return [(2, 1, 6, "", (ip, 0)) for ip in table[host]]
    return resolve


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen[self.path] = dict(self.headers)
        if self.path == "/drip":
            import time
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass
            return
        route = self.server.routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        status, headers, body = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    srv.routes = {}
    srv.seen = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


class LocalOpener:
    """Routes any URL's connection to the local test server, keeping the path."""

    def __init__(self, srv):
        self.port = srv.server_address[1]
        self.inner = web.build_opener(proxies={})

    def open(self, req, timeout=None):
        sp = urllib.parse.urlsplit(req.full_url)
        url = f"http://127.0.0.1:{self.port}{sp.path or '/'}" + (f"?{sp.query}" if sp.query else "")
        return self.inner.open(urllib.request.Request(url, headers=dict(req.header_items())), timeout=timeout)


class NeverOpener:
    def open(self, req, timeout=None):
        raise AssertionError("must not connect: " + req.full_url)


@pytest.mark.parametrize("url,table", [
    ("http://127.0.0.1/", {}),
    ("http://[::1]/", {}),
    ("http://172.16.1.1/", {}),
    ("http://169.254.169.254/latest/meta-data", {}),
    ("http://internal.example/", {"internal.example": ["172.31.0.5"]}),
    ("http://mixed.example/", {"mixed.example": ["93.184.216.34", "172.31.0.5"]}),
    ("http://v6.example/", {"v6.example": ["fe80::1%eth0"]}),
    ("file:///c:/x", {}),
    ("ftp://x/", {}),
    ("http:///nohost", {}),
    ("http://127%2E0%2E0%2E1:8810/v1/", None),
    ("http://a%40127.0.0.1/", None),
    ("http://user@127.0.0.1/", None),
    ("http://a..example/", None),
    ("http://" + "a" * 70 + ".example/", None),
    ("http://[::1", None),
    ("http://[::127.0.0.1]/", None),
    ("http://[2002:7f00:1::]/", None),
    ("http://[64:ff9b::7f00:1]/", None),
    ("http://[::ffff:127.0.0.1]/", None),
])
def test_fetch_guard_refuses_private_and_redirects(url, table):
    if table is None:  # a resolver that would approve anything it is asked about
        def resolve(host, port, *a, **kw):
            if ".." in host or any(len(x) > 63 for x in host.split(".")):
                raise UnicodeError("label empty or too long")
            return public(host)
    else:
        resolve = mapping(table)
    with pytest.raises(WebError):
        safe_fetch(url, resolve=resolve, opener=NeverOpener())


def test_fetch_guard_refuses_redirect_to_private(server):
    server.routes["/go"] = (302, {"Location": "http://127.0.0.1/secret"}, b"")
    with pytest.raises(WebError, match="not a public"):
        safe_fetch("http://pub.example/go", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                   opener=LocalOpener(server))


def test_fetch_redirect_limit(server):
    server.routes["/a"] = (302, {"Location": "http://pub.example/a"}, b"")
    with pytest.raises(WebError, match="redirect"):
        safe_fetch("http://pub.example/a", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                   opener=LocalOpener(server))


def test_fetch_follows_public_redirect(server):
    server.routes["/old"] = (301, {"Location": "/new"}, b"")
    server.routes["/new"] = (200, {"Content-Type": "text/plain"}, b"hello there")
    url, text = safe_fetch("http://pub.example/old", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                           opener=LocalOpener(server))
    assert url == "http://pub.example/new"
    assert text == "hello there"


def test_fetch_size_and_type_caps(server, monkeypatch):
    res = mapping({"pub.example": ["93.184.216.34"]})
    op = LocalOpener(server)
    server.routes["/img"] = (200, {"Content-Type": "image/png"}, b"\x89PNG")
    with pytest.raises(WebError, match="content type"):
        safe_fetch("http://pub.example/img", resolve=res, opener=op)
    monkeypatch.setattr(web, "MAX_BYTES", 100)
    server.routes["/big"] = (200, {"Content-Type": "text/plain"}, b"x" * 500)
    with pytest.raises(WebError, match="too large"):
        safe_fetch("http://pub.example/big", resolve=res, opener=op)
    server.routes["/404"] = (404, {}, b"")
    with pytest.raises(WebError, match="404"):
        safe_fetch("http://pub.example/404", resolve=res, opener=op)


def test_fetch_extracts_main_text(server):
    html = (b"<html><head><style>x{}</style></head><body><nav>MENU</nav>"
            b"<main><h1>Title</h1><p>The real content.</p></main><footer>FOOT</footer></body></html>")
    server.routes["/p"] = (200, {"Content-Type": "text/html; charset=utf-8"}, html)
    url, text = safe_fetch("http://pub.example/p", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                           opener=LocalOpener(server))
    assert "The real content." in text
    assert "MENU" not in text and "FOOT" not in text


DDG_HTML = """
<div class="result"><h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ffoo%3Fa%3D1&amp;rut=abc">Foo <b>Page</b></a></h2>
<a class="result__snippet" href="x">About &amp; around <b>foo</b></a></div>
<div class="result"><a class="result__a" href="https://direct.example/bar">Bar</a>
<a class="result__snippet" href="x">Bar snippet</a></div>
"""


class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, body):
        self.body = body
        self.reqs = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        return FakeResp(self.body)


def test_ddg_parses_results_and_unwraps_redirects():
    op = FakeOpener(DDG_HTML.encode())
    hits = web.DuckDuckGo(opener=op).search("foo", 5)
    assert hits[0] == {"title": "Foo Page", "url": "https://example.org/foo?a=1",
                       "snippet": "About & around foo"}
    assert hits[1]["url"] == "https://direct.example/bar"
    assert op.reqs[0].full_url == "https://html.duckduckgo.com/html/"
    assert op.reqs[0].data == b"q=foo"
    assert "Mozilla" in op.reqs[0].get_header("User-agent")
    assert len(web.DuckDuckGo(opener=op).search("foo", 1)) == 1


def test_ddg_drops_non_http_results():
    html = DDG_HTML + (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=javascript%3Aalert(1)">XSS</a>'
        '<a class="result__a" href="/y.js?ad=1">Ad</a>')
    urls = [h["url"] for h in web.DuckDuckGo(opener=FakeOpener(html.encode())).search("foo", 10)]
    assert urls == ["https://example.org/foo?a=1", "https://direct.example/bar"]


def test_searxng_and_brave_drop_non_http():
    rows = [{"title": "J", "url": "javascript:alert(1)", "content": "x", "description": "x"},
            {"title": "OK", "url": "https://ok.example/", "content": "x", "description": "x"}]
    sx = web.SearXNG("http://sx", opener=FakeOpener(json.dumps({"results": rows}).encode()))
    assert [h["url"] for h in sx.search("q")] == ["https://ok.example/"]


def test_fetch_total_deadline(server, monkeypatch):
    server.routes["/slow"] = (200, {"Content-Type": "text/plain"}, b"x" * 200000)
    ticks = iter([0, 0, 100, 100, 100])
    monkeypatch.setattr(web.time, "monotonic", lambda: next(ticks))
    with pytest.raises(WebError, match="too long"):
        safe_fetch("http://pub.example/slow", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                   opener=LocalOpener(server))


def test_searcher_follows_redirect_from_endpoint(server):
    server.routes["/search?q=a&format=json&language=en"] = (
        302, {"Location": "/real"}, b"")
    server.routes["/real"] = (200, {"Content-Type": "application/json"},
                              json.dumps({"results": [{"title": "T", "url": "https://a.example/",
                                                       "content": "C"}]}).encode())
    s = web.SearXNG(f"http://127.0.0.1:{server.server_address[1]}")
    s.opener = web.build_opener(proxies={}, follow=True)
    assert s.search("a")[0]["url"] == "https://a.example/"


def test_http_error_message_is_not_search_specific():
    import urllib.error

    class H:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    with pytest.raises(WebError, match="HTTP 401"):
        web.Brave("x", opener=H()).opener.open  # constructed fine
        web.SearXNG("http://x", opener=H()).search("q")


def test_searxng_parses_json():
    body = json.dumps({"results": [{"title": "T", "url": "https://a.example/", "content": "C"},
                                   {"title": "U", "url": "https://b.example/", "content": "D"}]}).encode()
    op = FakeOpener(body)
    hits = web.SearXNG("http://sx.local:8080/", opener=op).search("q w", 1)
    assert hits == [{"title": "T", "url": "https://a.example/", "snippet": "C"}]
    assert op.reqs[0].full_url.startswith("http://sx.local:8080/search?")
    assert "format=json" in op.reqs[0].full_url


def test_brave_reads_key_file(tmp_path):
    key = tmp_path / "brave.key"
    key.write_text("  sekrit\n", encoding="utf-8")
    cfg = WebConfig(search="brave", api_key_file=str(key))
    s = make_searcher(cfg)
    assert s.name == "brave"
    body = json.dumps({"web": {"results": [{"title": "T", "url": "https://a.example/",
                                            "description": "D"}]}}).encode()
    s.opener = FakeOpener(body)
    assert s.search("q") == [{"title": "T", "url": "https://a.example/", "snippet": "D"}]
    assert s.opener.reqs[0].get_header("X-subscription-token") == "sekrit"


def test_brave_missing_key_file_is_weberror(tmp_path):
    s = make_searcher(WebConfig(search="brave", api_key_file=str(tmp_path / "nope")))
    with pytest.raises(WebError, match="key file"):
        s.search("q")


def test_search_failure_is_weberror():
    class Boom:
        def open(self, req, timeout=None):
            raise OSError("down")
    with pytest.raises(WebError):
        web.DuckDuckGo(opener=Boom()).search("q")


def test_make_searcher_off_is_none():
    assert make_searcher(WebConfig(search="off")) is None
    assert make_searcher(WebConfig()).name == "duckduckgo"
    assert make_searcher(WebConfig(search="searxng", searxng_url="http://x")).name == "searxng"


def test_fetch_total_deadline_real_drip_server(server, monkeypatch):
    import time
    monkeypatch.setattr(web, "TIMEOUT", 0.5)
    t0 = time.monotonic()
    with pytest.raises(WebError, match="too long"):
        safe_fetch("http://pub.example/drip", resolve=mapping({"pub.example": ["93.184.216.34"]}),
                   opener=LocalOpener(server))
    assert time.monotonic() - t0 < 2.5


def test_searcher_redirects_drop_token_cross_host_and_refuse_non_http(server):
    port = server.server_address[1]
    op = web.build_opener(proxies={}, follow=True)
    server.routes["/r"] = (302, {"Location": f"http://localhost:{port}/final"}, b"")
    server.routes["/final"] = (200, {"Content-Type": "application/json"}, b"{}")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/r",
                                 headers={"X-Subscription-Token": "sekrit"})
    op.open(req, timeout=5).read()
    assert "X-Subscription-Token" not in server.seen["/final"]
    assert server.seen["/r"]["X-Subscription-Token"] == "sekrit"
    # same host keeps it
    server.routes["/s"] = (302, {"Location": "/t"}, b"")
    server.routes["/t"] = (200, {}, b"{}")
    op.open(urllib.request.Request(f"http://127.0.0.1:{port}/s",
                                   headers={"X-Subscription-Token": "k"}), timeout=5).read()
    assert server.seen["/t"]["X-Subscription-Token"] == "k"
    server.routes["/ftp"] = (302, {"Location": "ftp://example.org/x"}, b"")
    with pytest.raises(urllib.error.HTTPError):
        op.open(f"http://127.0.0.1:{port}/ftp", timeout=5)


def test_loopback_searxng_bypasses_the_proxy(monkeypatch):
    def uses_proxy_handler(opener):  # an empty ProxyHandler({}) registers nothing
        return any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)

    # the system proxy applies to every other searcher, as before
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    assert uses_proxy_handler(web.SearXNG("http://sx.example.org:8888").opener)
    for url in ("http://127.0.0.1:8888", "http://localhost:8888", "http://[::1]:8888", "http://127.1.2.3/"):
        assert not uses_proxy_handler(web.SearXNG(url).opener), url
