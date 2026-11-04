"""The reader: hostile or ordinary HTML in, inert Markdown out."""
import re
import time
import urllib.parse

from primer.browse.address import parse
from primer.browse.reader import MAX_MARKDOWN, Page, read

BOOK = "wikipedia_en_top_nopic_2026-09"
WEB = parse("https://site.example/dir/page")


def proxy(u: str) -> str:
    return "/v1/browse/image?u=" + urllib.parse.quote(u, safe="")


def md_links(md: str) -> list[str]:
    """Every Markdown link/image destination, i.e. what a renderer would make clickable."""
    return re.findall(r"(?<!\\)\]\(([^)\s]*)\)", md)


def test_reader_structure():
    html = """<html><head><title>The Title</title></head><body>
    <h1>Heading One</h1><p>Some <b>bold</b> text with a <a href="/other">link</a>.</p>
    <h2>Two</h2><h3>Three</h3><h4>Four</h4>
    <ul><li>first</li><li>second<ul><li>inner</li></ul></li></ul>
    <ol><li>one</li><li>two</li></ol>
    <blockquote><p>quoted words</p></blockquote>
    <pre>line one
  indented ``` fence</pre>
    <table><tr><th>A</th><th>B</th><th>C</th></tr>
      <tr><td>1</td><td>2 | x</td><td>3</td></tr></table>
    <p>Line<br>break</p>
    <img src="pics/cat.png" alt="A cat">
    <p><a href="https://elsewhere.example/x">far</a> and <a href="/other">again</a></p>
    </body></html>"""
    page = read(html, WEB, proxy)
    assert isinstance(page, Page)
    assert page.title == "The Title"
    md = page.markdown
    assert "# Heading One" in md and "\n## Two" in md and "\n### Three" in md and "\n#### Four" in md
    assert "- first" in md and "- second" in md and "  - inner" in md
    assert "1. one" in md and "2. two" in md
    assert "> quoted words" in md
    assert "````\nline one\n  indented ``` fence\n````" in md
    assert "| A | B | C |" in md and "| --- | --- | --- |" in md and r"| 1 | 2 \| x | 3 |" in md
    assert "Line  \nbreak" in md
    assert "![A cat](" + proxy("https://site.example/dir/pics/cat.png") + ")" in md
    assert "[link](https://site.example/other)" in md
    assert page.links == [{"n": 1, "text": "link", "url": "https://site.example/other"},
                          {"n": 2, "text": "far", "url": "https://elsewhere.example/x"}]


def test_reader_hostile_html():
    html = """<html><head><title>T</title><script>alert('head')</script></head><body>
    <script>alert(1)</script><style>body{display:none}</style>
    <iframe src="https://evil.example/"><p>iframe text</p></iframe>
    <form action="/steal"><input name="pw"><button>Send FORMTEXT</button></form>
    <noscript>NOSCRIPT</noscript><template>TEMPLATE</template>
    <svg onload="alert(2)"><script>alert(3)</script><text>SVGTEXT</text></svg>
    <object data="x.swf">OBJECT</object>
    <div hidden>HIDDEN1</div><div style="display: none">HIDDEN2</div>
    <!-- a comment COMMENT -->
    <p><a href="javascript:alert(4)">js link</a> <a href=" jav&#x09;ascript:alert(5)">js2</a>
    <a href="data:text/html,<script>alert(6)</script>">data link</a>
    <a href="vbscript:x">vb</a> <a href="mailto:a@b.c">mail</a></p>
    <p><img src="x" onerror="alert(7)" alt="pic"> <img src="javascript:alert(8)" alt="jsimg">
    <img src="data:image/png;base64,AAAA" alt="dataimg"> <img src="evil.svg" alt="a](javascript:alert(9))"></p>
    <p>Text: &lt;img src=x onerror=alert(10)&gt; and [x](javascript:alert(11)) and
    ![y](javascript:alert(12)) and &lt;script&gt;alert(13)&lt;/script&gt; *em* _u_ `code` # not
    | pipe \\ back &amp;lt; ~~s~~</p>
    <p>- not a list<br>1. not ordered<br>=== not setext<br># not heading<br>&gt; not quote</p>
    <p onclick="alert(14)" style="x">clickable</p>
    <a href="https://ok.example/a b(c)<d>">weird url</a>
    </body></html>"""
    page = read(html, WEB, proxy)
    md = page.markdown
    for gone in ["alert(1)", "alert(2)", "alert(3)", "iframe text", "FORMTEXT", "NOSCRIPT",
                 "TEMPLATE", "SVGTEXT", "OBJECT", "HIDDEN1", "HIDDEN2", "COMMENT", "onerror=\"",
                 "onclick", "head')"]:
        assert gone not in md, gone
    # every link/image destination is http(s), kiwix, or the image proxy
    for dest in md_links(md):
        assert dest.startswith(("https://", "http://", "kiwix:", "/v1/browse/image?u=")), dest
        assert not re.search(r"[\s<>()\"'\\]", dest), dest
    for link in page.links:
        assert link["url"].startswith(("https://", "http://")), link
    assert "javascript" not in "".join(md_links(md)).lower()
    assert "data:" not in "".join(md_links(md))
    # no raw HTML: every < is escaped
    assert not re.search(r"(?<!\\)<", md)
    # source text that looks like Markdown is escaped, shown as text
    assert r"\<img src=x onerror=alert(10)\>" in md
    assert r"\[x\](javascript\:alert(11))" in md
    assert r"!\[y\](javascript\:alert(12))" in md
    assert r"\*em\* \_u\_ \`code\` \# not \| pipe \\ back \&lt; \~\~s\~\~" in md
    assert "\\- not a list  \n1\\. not ordered  \n\\=== not setext  \n\\# not heading  \n\\> not quote" in md
    assert "js link" in md and "clickable" in md
    assert r"![a\](javascript\:alert(9))](" in md
    assert "[weird url](https://ok.example/a%20b%28c%29%3Cd%3E)" in md

    big = "<html><body>" + ("<p>" + "word " * 200 + "<a href='/x'>l</a></p>") * 5000 + "</body></html>"
    assert len(big) > 5_000_000
    t = time.monotonic()
    page = read(big, WEB, proxy)
    assert time.monotonic() - t < 30
    assert len(page.markdown) <= MAX_MARKDOWN

    deep = "<div>" * 5000 + "<script>DEEPSCRIPT</script><p>deep text</p>" + "</div>" * 5000
    page = read(deep, WEB, proxy)
    assert "DEEPSCRIPT" not in page.markdown and "deep text" in page.markdown


def test_reader_prefers_main_and_drops_furniture():
    html = """<html><body><header>SITE HEADER</header><nav>MENU</nav>
    <div>OUTSIDE MAIN</div>
    <main><h1>Real</h1>
      <table class="infobox"><tr><th>Built</th><td>INFOBOXDATA</td></tr></table>
      <p>Body text<sup class="reference"><a href="#cite_note-1">[1]</a></sup>.</p>
      <div class="navbox">NAVBOX</div><div role="note" class="hatnote">HATNOTE</div>
      <h2>Notes<span class="mw-editsection">[edit]</span></h2>
      <div class="reflist"><ol class="references"><li>REFITEM</li></ol></div>
      <aside>ASIDE</aside>
    </main><footer>FOOTER</footer></body></html>"""
    page = read(html, WEB, proxy)
    md = page.markdown
    assert "# Real" in md and "Body text." in md and "## Notes" in md
    for gone in ["SITE HEADER", "MENU", "OUTSIDE MAIN", "INFOBOXDATA", "[1]", "NAVBOX", "HATNOTE",
                 "[edit]", "edit\\]", "REFITEM", "ASIDE", "FOOTER"]:
        assert gone not in md, gone
    assert page.title == "Real"  # no <title>: the first h1

    with open("tests/warwick_infobox.html", encoding="utf-8") as f:
        page = read(f.read(), parse(f"kiwix:{BOOK}/Warwick_Castle"), proxy)
    assert page.markdown.strip() == "Warwick Castle is a medieval castle."


def test_reader_kiwix_links():
    base = parse(f"kiwix:{BOOK}/Warwick_Castle")
    html = """<html><head><title>Warwick Castle</title></head><body>
    <p><a href="Kenilworth_Castle">Kenilworth</a>, <a href="./River_Avon_(Warwickshire)">Avon</a>,
    <a href="Warwick_Castle#History">history</a>, <a href="#cite_note-4">[4]</a>,
    <a href="../Warwick">Warwick</a>, <a href="Kenilworth_Castle">again</a>,
    <a href="https://www.example.org/">external</a></p>
    <img src="../I/m/Castle.jpg" alt="castle"></body></html>"""
    page = read(html, base, proxy)
    avon = f"kiwix:{BOOK}/River_Avon_%28Warwickshire%29"
    assert [lk["url"] for lk in page.links] == [
        f"kiwix:{BOOK}/Kenilworth_Castle", avon, f"kiwix:{BOOK}/Warwick_Castle",
        f"kiwix:{BOOK}/Warwick", "https://www.example.org/"]
    assert [lk["n"] for lk in page.links] == [1, 2, 3, 4, 5]
    assert page.links[0]["text"] == "Kenilworth"
    assert f"[Avon]({avon})" in page.markdown
    assert "\\[4\\]" in page.markdown  # in-page anchor: text only
    assert "![castle](" + proxy(f"kiwix:{BOOK}/I/m/Castle.jpg") + ")" in page.markdown


def test_reader_caps_size():
    html = "<p>" + "a" * 300_000 + "</p>" + "".join(f"<p><a href='/l{i}'>x{i}</a></p>" for i in range(10))
    page = read(html, WEB, proxy)
    assert len(page.markdown) <= MAX_MARKDOWN == 200_000
    assert page.links == []  # the cut-off links are not offered
    html = "".join(f"<p>para {i} " + "b" * 1000 + f" <a href='/l{i}'>x{i}</a></p>" for i in range(400))
    page = read(html, WEB, proxy)
    assert len(page.markdown) <= MAX_MARKDOWN
    assert page.links and all(lk["url"] in page.markdown for lk in page.links)
    assert len(page.links) < 400
    wide = "<table><tr>" + "".join(f"<td>c{i}</td>" for i in range(12)) + "</tr><tr>" + \
        "".join(f"<td>d{i}</td>" for i in range(12)) + "</tr></table>"
    md = read(wide, WEB, proxy).markdown
    assert "c7" in md and "c8" not in md


def test_reader_pre_line_breaks_cannot_escape():
    """A lone CR (or U+2028/2029/0085) in <pre> must not end a list/quote line early:
    marked reads CR as a newline, so the rest would escape the fence as live Markdown."""
    for sep in ("\r", "\r\n", "\u2028", "\u2029", "\x85", "&#13;", "&#x2028;"):
        quote = read(f"<blockquote><pre>x{sep}[c](https://evil.example){sep}"
                     f"![i](https://evil.example/i.png)</pre></blockquote>", WEB, proxy).markdown
        lst = read(f"<ul><li><pre>x{sep}[click](https://evil.example/p){sep}# Fake{sep}"
                   f"<b>y</b></pre></li></ul>", WEB, proxy).markdown
        for md in (quote, lst):
            assert not re.search("[\r\u2028\u2029\x85]", md), repr(md)
        assert all(ln.startswith(">") for ln in quote.split("\n")), repr(quote)
        lines = lst.split("\n")
        assert lines[0] == "- ```" and all(ln.startswith("  ") for ln in lines[1:]), repr(lst)
    page = read("<p>a\rb</p><h2>t\r# x</h2>", WEB, proxy)
    assert "\r" not in page.markdown and "\n# x" not in page.markdown


def test_reader_split_autolinks_are_inert():
    for html in ["<p>https<span>://evil.example/x</span> and www<b>.evil.example</b></p>",
                 "<p>htt<b>p:/</b>/x.example and WW<i>W.evil.example</i></p>",
                 "<p>w<b>w</b>w.evil.example HTTPS://evil.example x@y.example ftp://e.example</p>",
                 "<p>mail<b>@</b>evil.example and <i>www</i>.e.example</p>"]:
        md = read(html, WEB, proxy).markdown
        assert not re.search(r"(?<!\\):", md), md     # no scheme can form, whatever the split
        assert not re.search(r"(?<!\\)@", md), md
        assert not re.search(r"(?i)www\.", md), md
        assert md_links(md) == []


def test_reader_links_follow_what_was_emitted():
    # a link whose markup survives only as text (cut, or written in the page) is never offered
    page = read("<p>[x](https://site.example/l1)</p><p><a href='/l2'>real</a></p>", WEB, proxy)
    assert [lk["url"] for lk in page.links] == ["https://site.example/l2"]
    nested = "<div><div>" + "".join(f"<p>p{i} " + "c" * 900 + f" <a href='/n{i}'>n</a></p>"
                                     for i in range(300)) + "</div></div>"
    page = read(nested, WEB, proxy)
    assert 100 < len(page.links) < 300
    assert all(f"]({lk['url']})" in page.markdown for lk in page.links)
