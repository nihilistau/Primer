import pytest

from primer.grounding.grounder import Grounder
from primer.grounding.kiwix import KiwixClient, KiwixError
from primer.grounding.web import WebError
from tests.fake_kiwix_server import serve


def _page(text):
    return f"<html><body><article><p>{text}</p></article></body></html>"


@pytest.fixture
def kiwix_two():
    server, url = serve({
        "wiki_a": {"Whale": ("Whale", _page("Whales from book A.")),
                   "Whale_shark": ("Whale shark", _page("A shark."))},
        "wiki_b": {"Whale": ("Whale", _page("Whales from book B.")),
                   "Humpback": ("Humpback whale", _page("A humpback."))},
    })
    yield KiwixClient(url, timeout=2)
    server.kill()


def test_reference_exact_title_first(kiwix_two):
    g = Grounder(kiwix_two, None, book_order=["wiki_a", "wiki_b"])
    seen = []
    sources, note = g.gather("reference", "whale", seen.append)
    assert note == "" and sources[0].title == "Whale" and sources[0].n == 1
    assert [s.n for s in sources] == list(range(1, len(sources) + 1))
    assert seen[0] == "Searching the offline encyclopedia…" and seen[-1].startswith("Reading ")
    assert sources[0].origin == "reference" and sources[0].where == "wiki a"


def test_reference_dedupes_by_book_order(kiwix_two):
    g = Grounder(kiwix_two, None, book_order=["wiki_b", "wiki_a"])
    sources, _ = g.gather("reference", "Whale", lambda s: None)
    whales = [s for s in sources if s.title == "Whale"]
    assert len(whales) == 1 and whales[0].book == "wiki_b" and "book B" in whales[0].text


def test_reference_down_falls_back_with_note():
    g = Grounder(KiwixClient("http://127.0.0.1:1", timeout=0.5), None)
    assert g.available("reference")[0] is False
    sources, note = g.gather("reference", "Whale", lambda s: None)
    assert sources == [] and "isn't reachable" in note and note.endswith("model's own knowledge.")


def test_reference_no_hits_and_unconfigured(kiwix_two):
    g = Grounder(kiwix_two, None, book_order=["wiki_a"])
    assert g.gather("reference", "Zzyzx", lambda s: None) == (
        [], 'No offline article found for "Zzyzx"; written from the model\'s own knowledge.')
    hint = "written from the model's own knowledge. Change the default source in Engines."
    assert Grounder(None, None).gather("reference", "x", lambda s: None)[1] == (
        "Offline reference isn't set up in this tier; " + hint)
    assert Grounder(None, None).gather("web", "x", lambda s: None)[1] == (
        "Web search is off in this tier; " + hint)


class _Searcher:
    def __init__(self, results=None, exc=None):
        self.results, self.exc = results or [], exc

    def search(self, query, n=5):
        if self.exc:
            raise self.exc
        return self.results


def _results(n):
    return [{"title": f"T{i}", "url": f"https://www.site{i}.org/p", "snippet": ""} for i in range(1, n + 1)]


def test_web_skips_failed_fetches():
    def fetch(url):
        if "site2" in url:
            raise WebError("refused")
        return url, "Some text. " * 300

    seen = []
    g = Grounder(None, _Searcher(_results(5)), fetch=fetch)
    sources, note = g.gather("web", "q", seen.append)
    assert note == "" and [s.title for s in sources] == ["T1", "T3", "T4"]
    assert [s.n for s in sources] == [1, 2, 3] and sources[0].where == "site1.org"
    assert len(sources[0].text) <= 2400 and len(sources[1].text) <= 1200
    assert seen == ["Searching the web…", "Reading 3 pages…"]


def test_web_no_usable_source_falls_back():
    def refuse(url):
        raise WebError("private address")

    g = Grounder(None, _Searcher(_results(3)), fetch=refuse)
    assert g.gather("web", "q", lambda s: None) == (
        [], 'No usable web page found for "q"; written from the model\'s own knowledge.')
    down = Grounder(None, _Searcher(exc=WebError("blocked")), fetch=refuse)
    sources, note = down.gather("web", "q", lambda s: None)
    assert sources == [] and note.startswith("Web search isn't working (blocked)")
    boom = Grounder(None, _Searcher(exc=RuntimeError("bug")), fetch=refuse)
    assert boom.gather("web", "q", lambda s: None)[0] == []


def test_available():
    assert Grounder(None, None).available("storyteller") == (True, "")
    assert Grounder(None, None).available("web")[0] is False
    assert Grounder(None, _Searcher()).available("web") == (True, "")


def test_reference_skips_books_not_loaded_and_failed_suggest(kiwix_two):
    g = Grounder(kiwix_two, None, book_order=["missing", "wiki_a"])
    sources, note = g.gather("reference", "Whale", lambda s: None)
    assert note == "" and sources[0].book == "wiki_a"

    class Flaky(KiwixClient):
        def suggest(self, book, query):
            if book == "wiki_a":
                raise KiwixError("boom")
            return super().suggest(book, query)

    flaky = Flaky(kiwix_two.base_url, timeout=2)
    sources, note = Grounder(flaky, None, book_order=["wiki_a", "wiki_b"]).gather(
        "reference", "Whale", lambda s: None)
    assert note == "" and sources[0].book == "wiki_b"


def test_reference_titles_are_plain_text():
    server, url = serve({"wiki_a": {"Whale": ("Whale", _page("Whales."))}})
    server.highlight = True
    try:
        sources, _ = Grounder(KiwixClient(url, timeout=2), None).gather("reference", "whale", lambda s: None)
    finally:
        server.kill()
    assert [s.title for s in sources] == ["Whale"]
    from primer.grounding.grounder import _plain
    assert _plain("<b>Whale</b> &amp; <i>co</i>\n x") == "Whale & co x"


def test_reference_search_duplicate_of_suggest_appears_once(kiwix_two):
    sources, _ = Grounder(kiwix_two, None, book_order=["wiki_a"]).gather("reference", "Whale", lambda s: None)
    titles = [s.title.casefold() for s in sources]
    assert titles.count("whale") == 1 and len(titles) == len(set(titles))


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_reference_deadline_uses_what_was_gathered(kiwix_two):
    clock = _Clock()

    class Slow(KiwixClient):
        def article(self, book, path):
            clock.t += 15
            return super().article(book, path)

    g = Grounder(Slow(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a"], clock=clock)
    sources, note = g.gather("reference", "Whale", lambda s: None)
    assert note == "" and len(sources) == 2  # 15 s, 30 s: stops before a third


def test_reference_deadline_nothing_gathered_falls_back(kiwix_two):
    clock = _Clock()

    class Late(KiwixClient):
        def suggest(self, book, query):
            clock.t += 25
            return super().suggest(book, query)

    g = Grounder(Late(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a", "wiki_b"], clock=clock)
    assert g.gather("reference", "Zzyzx", lambda s: None)[1].startswith("No offline article found")


def test_web_fetches_in_parallel_and_stops_at_the_deadline(monkeypatch):
    import primer.grounding.grounder as grounder_mod
    import threading
    import time as _time
    monkeypatch.setattr(grounder_mod, "_WEB_DEADLINE", 0.8)
    seen = []

    def fetch(url):
        seen.append(threading.get_ident())
        _time.sleep(0.3 if ("site1." in url or "site2." in url) else 3)  # results 3-5 are too slow
        return url, "Text. " * 50

    t0 = _time.monotonic()
    sources, note = Grounder(None, _Searcher(_results(5)), fetch=fetch).gather("web", "q", lambda s: None)
    took = _time.monotonic() - t0
    assert note == "" and [s.n for s in sources] == [1, 2]
    assert took < 1.5  # three sequential fast fetches alone would be 0.9 s; slow ones not awaited
    assert len(set(seen)) == 5  # all five fetched at once


def test_reference_full_text_failure_keeps_title_hits(kiwix_two):
    class NoFullText(KiwixClient):
        def search(self, query, n=5):
            raise KiwixError("HTTP 400")

    g = Grounder(NoFullText(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a"])
    sources, note = g.gather("reference", "Whale", lambda s: None)
    assert note == "" and sources[0].title == "Whale"


def test_reference_title_search_answered_but_full_text_failed_is_not_unreachable(kiwix_two):
    class NoFullText(KiwixClient):
        def search(self, query, n=5):
            raise KiwixError("HTTP 400")

    g = Grounder(NoFullText(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a"])
    sources, note = g.gather("reference", "Zzyzx", lambda s: None)
    assert sources == [] and note.startswith("No offline article found")

    class AllDown(NoFullText):
        def suggest(self, book, query):
            raise KiwixError("boom")

    g = Grounder(AllDown(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a"])
    sources, note = g.gather("reference", "Zzyzx", lambda s: None)
    assert sources == [] and "isn't reachable" in note


def test_reference_one_book_failing_is_not_unreachable(kiwix_two):
    class OneDown(KiwixClient):
        def suggest(self, book, query):
            if book == "wiki_a":
                raise KiwixError("boom")
            return super().suggest(book, query)

    g = Grounder(OneDown(kiwix_two.base_url, timeout=2), None, book_order=["wiki_a", "wiki_b"])
    sources, note = g.gather("reference", "Zzyzx", lambda s: None)
    assert sources == [] and note.startswith("No offline article found")


def test_reference_books_outside_book_order_still_searched(kiwix_two):
    g = Grounder(kiwix_two, None, book_order=["wiki_b"])  # wiki_a's file stem didn't match
    seen = []
    orig = kiwix_two.suggest
    kiwix_two.suggest = lambda book, query: seen.append(book) or orig(book, query)
    g.gather("reference", "Whale", lambda s: None)
    assert seen == ["wiki_b", "wiki_a"]


# --- gather_url: one given page as the single source ---

def test_gather_url_web():
    html = "<html><head><title>Warwick Castle</title></head><body><nav>menu</nav><p>A castle.</p></body></html>"
    seen = []

    def fetch_html(url):
        seen.append(url)
        return "https://www.example.org/castle/", "text/html", html

    sources, note = Grounder(None, _Searcher(), fetch_html=fetch_html).gather_url("https://example.org/castle")
    assert note == "" and seen == ["https://example.org/castle"]
    assert len(sources) == 1 and sources[0].n == 1 and sources[0].origin == "web"
    assert sources[0].title == "Warwick Castle" and sources[0].where == "example.org"
    assert sources[0].url == "https://www.example.org/castle/" and "A castle." in sources[0].text
    assert "menu" not in sources[0].text


def test_gather_url_web_failure_is_a_note():
    def refuse(url):
        raise WebError("127.0.0.1 is not a public address")

    sources, note = Grounder(None, _Searcher(), fetch_html=refuse).gather_url("http://127.0.0.1/x")
    assert sources == [] and "not a public address" in note and note.endswith("model's own knowledge.")
    assert Grounder(None, None).gather_url("ftp://x/y")[0] == []


def test_gather_url_strips_head_and_needs_web_on():
    html = "<html><head><title>Castle</title></head><body><p>Body text.</p></body></html>"
    sources, _ = Grounder(None, _Searcher(), fetch_html=lambda u: (u, "text/html", html)).gather_url(
        "https://e.org/c")
    assert sources[0].title == "Castle" and sources[0].text == "Body text."
    seen = []

    def fetch(u):
        seen.append(u)
        return u, "text/html", html

    sources, note = Grounder(None, None, fetch_html=fetch).gather_url("https://e.org/c")  # web is off
    assert sources == [] and seen == [] and note.startswith("Web search is off in this tier;")
    assert "model's own knowledge." in note


def test_gather_url_reference(kiwix_two):
    g = Grounder(kiwix_two, None)
    sources, note = g.gather_url("kiwix:wiki_b/Humpback")
    assert note == "" and len(sources) == 1
    s = sources[0]
    assert (s.origin, s.book, s.path, s.n) == ("reference", "wiki_b", "Humpback", 1)
    assert "A humpback." in s.text and s.title == "Humpback"
    sources, note = g.gather_url("kiwix:wiki_b/Missing")
    assert sources == [] and "couldn't be read" in note and note.endswith("model's own knowledge.")
    assert "isn't set up" in Grounder(None, None).gather_url("kiwix:wiki_b/Humpback")[1]


def test_gather_url_reference_where_is_the_book_title(kiwix_two):
    title = {b["name"]: b["title"] for b in kiwix_two.books()}["wiki_b"]
    sources, _ = Grounder(kiwix_two, None).gather_url("kiwix:wiki_b/Humpback")
    assert sources[0].where == title
