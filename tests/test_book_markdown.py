"""Page Markdown to a safe XHTML fragment for the book."""
import time
import xml.etree.ElementTree as ET

from primer.book.markdown import escape, render

ALLOWED = {"div", "h1", "h2", "h3", "h4", "p", "em", "strong", "ul", "ol", "li", "sup", "a", "br"}


def parse(fragment: str) -> ET.Element:
    return ET.fromstring("<div>" + fragment + "</div>")


def tags(fragment: str) -> set[str]:
    return {el.tag for el in parse(fragment).iter()}


def test_render_escapes_hostile_markdown():
    md = ("# Title\n"
          "Hello <script>alert(1)</script> there.\n\n"
          "<img src=x onerror=a()>\n\n"
          "[x](javascript:alert(1)) and [y](JavaScript:alert(2))\n\n"
          "&amp;nbsp; &copy; &#0;\n\n"
          "bad \x00\x07 chars ￾\n\n"
          "- item <b>bold</b>\n\n"
          "## Heading with <i>tag</i>\n\n"
          + "a*" * 50_000 + "\n\n"
          + "x" * 200_000 + "\n")
    start = time.time()
    f = render(md, anchor="c1", n_sources=3)
    assert time.time() - start < 2.0
    root = parse(f)
    assert {el.tag for el in root.iter()} <= ALLOWED
    low = f.lower()
    assert "<script" not in low and "<img" not in low and "javascript:" not in low
    # the raw HTML is shown as text, not run
    text = "".join(root.itertext())
    assert "<script>alert(1)</script>" in text and "<img src=x onerror=a()>" in text
    assert "onerror=" not in f  # "=" is written as a character reference in text
    assert "&amp;nbsp;" in text  # the entity stays as written
    assert "\x00" not in f and "\x07" not in f and "￾" not in f
    for el in root.iter("a"):
        href = el.get("href")
        assert href.startswith("#") or href.startswith(("http://", "https://"))
    assert max(len(line) for line in f.splitlines()) < 30_000  # a 200 KB line is capped


def test_render_citations_and_links():
    f = render("Stones [1] fly [5] far [2].\n\n[w](https://example.org/a) and [v](file:///etc) "
               "and [u](http://172.31.0.5/x) and [l](http://localhost:8080/k)\n\n"
               "*a* and **b** and **x unbalanced and *y too",
               anchor="c3", n_sources=2)
    root = parse(f)
    sups = list(root.iter("sup"))
    assert [s.find("a").get("href") for s in sups] == ["#c3-src-1", "#c3-src-2"]
    assert [s.find("a").text for s in sups] == ["1", "2"]
    assert '<sup><a href="#c3-src-1">1</a></sup>' in f
    text = "".join(root.itertext())
    assert "[5]" in text
    links = [a for a in root.iter("a") if not a.get("href").startswith("#")]
    assert [(a.get("href"), a.text) for a in links] == [("https://example.org/a", "w")]
    assert "file:" not in f and " v " in text  # the unsafe link keeps only its text
    assert "172.31.0.5" not in f and "localhost" not in f
    assert [e.text for e in root.iter("em")] == ["a"]
    assert [s.text for s in root.iter("strong")] == ["b"]
    assert "**x unbalanced" in text and "*y too" in text


def test_render_citation_bounds():
    f = render("[0] [1] [3] [12345]", anchor="c9", n_sources=3)
    root = parse(f)
    assert [s.find("a").get("href") for s in root.iter("sup")] == ["#c9-src-1", "#c9-src-3"]
    assert "[0]" in "".join(root.itertext()) and "[12345]" in "".join(root.itertext())
    assert render("[1]", anchor="c1", n_sources=0).count("<sup>") == 0


def test_render_drops_leading_title_and_keeps_lists():
    md = ("\n# Trebuchet\n\nA siege engine.\nIt throws stones.\n\n"
          "## Parts\n- the arm\n- the **counterweight**\n\n"
          "1. Pull down the arm.\n2. Let go.\n\n"
          "### Deeper\n##### Deepest\n\nEnd.")
    f = render(md, anchor="c1", n_sources=0)
    root = parse(f)
    assert "Trebuchet" not in f
    assert [el.tag for el in root] == ["p", "h2", "ul", "ol", "h3", "h4", "p"]
    assert root[0].text == "A siege engine. It throws stones."
    assert [li.text for li in root[2]] == ["the arm", "the "]
    assert root[2][1].find("strong").text == "counterweight"
    assert [li.text for li in root[3]] == ["Pull down the arm.", "Let go."]
    # a "# Title" that is not the first line is a heading, not dropped
    assert "<h1>Later</h1>" in render("Intro.\n\n# Later", anchor="c1", n_sources=0)


def test_escape():
    assert escape('<b>&"x"</b>') == "&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;"
    assert escape("a\x00b\x1fc") == "abc"
    ET.fromstring(f'<p title="{escape(chr(39) + chr(34))}">{escape("<&>")}</p>')


def test_render_cites_only_the_given_numbers():
    f = render("a [1] b [2] c [3] d [1, 3] e [2, 3]", anchor="c2", n_sources=0, numbers=[1, 3])
    root = parse(f)
    assert [s.find("a").get("href") for s in root.iter("sup")] == [
        "#c2-src-1", "#c2-src-3", "#c2-src-1", "#c2-src-3"]
    text = "".join(root.itertext())
    assert "[2]" in text and "[2, 3]" in text


def test_render_indented_title_and_headings():
    f = render("   # Title\n\nBody.\n\n  ## Part\nMore.", anchor="c1", n_sources=0)
    root = parse(f)
    assert "Title" not in f
    assert [el.tag for el in root] == ["p", "h2", "p"] and root[1].text == "Part"


def test_safe_url_refuses_disguised_and_local_hosts():
    from primer.book.markdown import safe_url
    for bad in ("http://127.1/", "http://0x7f.1/", "http://2130706433/", "http://0x7f000001/",
                "http://172.31.0.5/", "http://169.254.169.254/latest", "http://[::1]/",
                "https://user:pw@example.org/", "https://user@example.org/",
                "http://box.localhost/", "http://printer.local/", "http://intranet/",
                "ftp://example.org/", "javascript:alert(1)",
                "http://%31%32%37.0.0.1/", "http://exa mple.com/"):
        assert safe_url(bad) is None, bad
    for good in ("https://example.org/a?b=c", "http://en.wikipedia.org/wiki/X",
                 "http://172-31-0-5.nip.io/", "http://172.31.0.5.nip.io/"):
        assert safe_url(good) == good


def test_safe_url_refuses_hosts_that_are_not_ascii():
    from primer.book.markdown import safe_url
    # browsers fold fullwidth digits and dots to ASCII, so these are 127.0.0.1 and 172.31.0.5
    for bad in ("http://１２７.０.０.１/x", "http://１７２.３１.０.５/", "http://１２７．０．０．１/",
                "https://ｅｘａｍｐｌｅ.org/"):
        assert safe_url(bad) is None, bad
