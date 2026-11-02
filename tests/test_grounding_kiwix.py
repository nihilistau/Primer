import socket

import pytest

from primer.grounding.kiwix import KiwixClient, KiwixError
from tests.fake_kiwix_server import serve


@pytest.fixture
def kiwix():
    server, url = serve()
    yield KiwixClient(url)
    server.kill()


def test_kiwix_books_use_the_content_name():
    # real kiwix-serve 3.8.1: <name>wikipedia_en_top</name> but articles live at
    # /content/wikipedia_en_top_nopic_2026-09
    feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><name>wikipedia_en_top</name>'
            '<title>Best of Wikipedia</title><link rel="http://opds-spec.org/image/thumbnail" href="/x"/>'
            '<link type="text/html" href="/content/wikipedia_en_top_nopic_2026-09" /></entry>'
            '<entry><name>only_name</name><title>T</title></entry></feed>')
    c = KiwixClient("http://127.0.0.1:1")
    c._get = lambda path: feed.encode()
    assert c.books() == [{"name": "wikipedia_en_top_nopic_2026-09", "title": "Best of Wikipedia"},
                         {"name": "only_name", "title": "T"}]


def test_kiwix_books(kiwix):
    assert kiwix.books() == [{"name": "wikipedia_en", "title": "wikipedia en"}]
    assert kiwix.health() is True


def test_kiwix_search_parses_xml(kiwix):
    hits = kiwix.search("whale", n=5)
    assert {"book": "wikipedia_en", "path": "Whale", "title": "Whale",
            "snippet": "Whale snippet"} in hits
    assert len(kiwix.search("whale", n=1)) == 1
    assert kiwix.search("zzzz") == []


def test_kiwix_suggest_paths_only(kiwix):
    out = kiwix.suggest("wikipedia_en", "whale")
    assert [e["path"] for e in out] == ["Whale", "Whale_shark"]
    assert all("kind" not in e or e["kind"] == "path" for e in out)


def test_kiwix_article_text(kiwix):
    assert kiwix.article("wikipedia_en", "Whale") == "Whales are marine mammals. They breathe air."
    with pytest.raises(KiwixError):
        kiwix.article("wikipedia_en", "Nope")


def test_kiwix_href(kiwix):
    assert kiwix.href("wikipedia_en", "A b/c") == f"{kiwix.base_url}/content/wikipedia_en/A%20b/c"


def test_kiwix_down_raises():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = KiwixClient(f"http://127.0.0.1:{port}", timeout=2)
    assert c.health() is False
    with pytest.raises(KiwixError, match=r"^(nothing is answering at|kiwix-serve at) http://127.0.0.1:\d+( didn.t answer in time)?$"):
        c.books()


def test_kiwix_href_quotes_the_book(kiwix):
    assert kiwix.href("a b/c?d", "X") == f"{kiwix.base_url}/content/a%20b%2Fc%3Fd/X"


def test_kiwix_article_html():
    server, url = serve({"b": {"P": ("P", '<html><a href="geo:1,2">x</a></html>')}})
    try:
        assert 'href="geo:1,2"' in KiwixClient(url).article_html("b", "P")
    finally:
        server.kill()
