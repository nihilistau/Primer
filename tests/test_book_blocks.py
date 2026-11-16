"""The bound book's blocks: the book as an ordered list of safe HTML blocks, and their routes."""
import json
import re
import xml.etree.ElementTree as ET

from primer.book.blocks import KINDS, blocks, media_links
from primer.book.markdown import render
from primer.book.model import Picture
from primer.diagrams.svg import clean_svg
from tests.test_book_service import T0, page, wait_book
from tests.test_book_writers import DRAWING, NASTY, make_plan

HANDLER = re.compile(r"\son\w+\s*=", re.I)


def _section(b: dict) -> str:
    if b["chapter"]:
        return "chapter"
    if b["kind"] in ("cover", "contents", "closing", "back"):
        return b["kind"]
    return "story"  # the stories' headings, scenes and pictures


def _safe(html: str) -> None:
    low = html.lower()
    assert "<script" not in low and "javascript:" not in low and '="data:' not in low, html
    assert not HANDLER.search(html), html


def test_blocks_in_order_from_cover_to_back(tmp_path):
    plan = make_plan(tmp_path / "media")
    out = blocks(plan, tmp_path / "media")
    kinds = [b["kind"] for b in out]
    assert kinds[0] == "cover" and kinds[-1] == "back"
    assert kinds.count("cover") == 1 and kinds.count("back") == 1
    assert set(kinds) <= set(KINDS)
    # front matter, then the chapters, then the stories, the closing note and the back cover
    sections = [_section(b) for b in out]
    seen = []
    for s in sections:
        if not seen or seen[-1] != s:
            seen.append(s)
    assert seen == ["cover", "contents", "chapter", "story", "closing", "back"]
    for want in ("chapter-head", "para", "picture", "plate", "list", "timeline", "diagram",
                 "sources", "heading"):
        assert want in kinds, want
    for b in out:
        assert set(b) >= {"kind", "html", "chapter"}
        ET.fromstring(b["html"])  # each block is well-formed on its own
        _safe(b["html"])
    back = out[-1]["html"]
    assert "My wanderings" in back and "Made with Primer" in back


def test_every_chapter_block_has_its_number(tmp_path):
    plan = make_plan(tmp_path / "media")
    out = blocks(plan, tmp_path / "media")
    chapters = [b["chapter"] for b in out]
    inner = [c for c in chapters if c]
    assert inner == sorted(inner) and set(inner) == {1, 2, 3}
    heads = [b for b in out if b["kind"] == "chapter-head"]
    assert [h["chapter"] for h in heads] == [1, 2, 3]
    assert [h["node_id"] for h in heads] == ["n1", "n2", "n3"]
    assert 'id="c1"' in heads[0]["html"] and "Trebuchet" in heads[0]["html"]
    assert "From" in heads[1]["html"] and 'href="#c1"' in heads[1]["html"]
    for b in out:
        if b["kind"] in ("cover", "contents", "story", "closing", "back"):
            assert b["chapter"] == 0, b
    # each block of chapter 1 sits between its head and chapter 2's head
    first = chapters.index(1)
    assert out[first]["kind"] == "chapter-head"
    one = [b for b in out if b["chapter"] == 1]
    assert {"para", "picture", "plate", "list", "timeline", "diagram", "sources"} <= {b["kind"] for b in one}
    # a citation links to a source that is in a sources block of its chapter
    cites = re.findall(r'href="#(c1-src-\d+)"', "".join(b["html"] for b in one if b["kind"] == "para"))
    assert cites
    srcs = "".join(b["html"] for b in one if b["kind"] == "sources")
    for c in cites:
        assert f'id="{c}"' in srcs
    # contents entries link to chapters
    contents = "".join(b["html"] for b in out if b["kind"] == "contents")
    assert 'href="#c1"' in contents and 'href="#c3"' in contents and 'href="#end"' in contents
    # every link within the book lands on an id in some block
    whole = "".join(b["html"] for b in out)
    targets = set(re.findall(r'href="#([^"]+)"', whole))
    assert {"c1", "c3", "stories", "end", "c1-src-3"} <= targets
    for t in targets:
        assert f'id="{t}"' in whole, t


def test_pictures_are_media_urls_never_data(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    out = blocks(plan, media)
    srcs = re.findall(r'src="([^"]*)"', "".join(b["html"] for b in out))
    assert srcs
    for s in srcs:
        assert re.fullmatch(r"/v1/media/[0-9a-f]{64}\.png", s), s
    cover = out[0]["html"]
    assert "/v1/media/" + "a" * 64 + ".png" in cover  # the cover has the first picture
    pics = [b for b in out if b["kind"] == "picture"]
    assert {b["chapter"] for b in pics} == {0, 1}  # chapter 1's and the story scene's


def test_bad_or_missing_picture_left_out(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    plan.chapters[0].pictures.append(Picture("m2", "../../secret.png", "bad"))
    plan.chapters[0].pictures.append(Picture("m3", "b" * 64 + ".png", "gone"))  # not on disk
    out = blocks(plan, media)
    html = "".join(b["html"] for b in out)
    assert "secret" not in html and "b" * 64 not in html
    assert "could not be found" in out[-2]["html"]  # the closing note says so
    links = media_links(media)
    assert links.picture(Picture("x", "../x.png", "")) is None
    assert links.picture(Picture("x", "A" * 64 + ".png", "")) is None
    assert links.picture(Picture("x", "a" * 64 + ".png", "")) == "/v1/media/" + "a" * 64 + ".png"
    assert links.chapter(7) == "#c7"


def test_hostile_titles_markdown_and_svg_are_cleaned(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media, title_one='<script>alert(1)</script><img src=x onerror="go()">')
    plan.title = '"><script>alert(2)</script>'
    plan.by = NASTY
    one = plan.chapters[0]
    one.html = render('# Hi\n<script>alert(3)</script> [x](javascript:alert(4)) '
                      '<a href="javascript:x" onclick="y">z</a>\n\n- <b onmouseover="q">li</b>',
                      anchor="c1", n_sources=2, numbers=[1, 3])
    hostile = DRAWING.replace("<g id=\"arm\">",
                              '<g id="arm" onload="bad()"><a href="javascript:x"><text>t</text></a>'
                              '<foreignObject><div>f</div></foreignObject>')
    svg, _ = clean_svg(hostile, prefix="dg1-")
    one.diagram = {"svg": svg, "caption": "<script>c</script>"}
    one.sources[1]["url"] = "javascript:alert(5)"
    plan.stories[0].title = "<script>s</script>"
    plan.stories[0].scenes[0]["text"] = '<img src=x onerror="go()">'
    out = blocks(plan, media)
    for b in out:
        ET.fromstring(b["html"])
        _safe(b["html"])
        assert "<b>" not in b["html"] and "<b " not in b["html"] and "<img src=x" not in b["html"]
        assert "foreignObject" not in b["html"]
    text = "".join("".join(ET.fromstring(b["html"]).itertext()) for b in out)
    assert "<script>alert(1)</script>" in text  # shown as text, not run
    assert '"><script>alert(2)</script>' in text
    assert "javascript:x" in text  # hostile Markdown is shown, never linked


def test_split_keeps_every_part_of_each_section(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    out = blocks(plan, media)
    text = " ".join(" ".join(ET.fromstring(b["html"]).itertext()) for b in out)
    for want in ("It throws", "Heavy.", "Stone.", "Harlech", "Conwy", "Built", "Besieged",
                 "Counterweight", "Pivot", "Siege <engines>", "The throw", "He lets go.",
                 "Closing note", "Contents", "Stories"):
        assert want in text, want
    plates = [b for b in out if b["kind"] == "plate"]
    assert len(plates) == 1 and "<svg" in plates[0]["html"]
    assert 'xmlns="http://www.w3.org/2000/svg"' in plates[0]["html"]
    assert "ns0:" not in "".join(b["html"] for b in out)
    diagrams = [b for b in out if b["kind"] == "diagram"]
    assert len(diagrams) == 1 and "<svg" in diagrams[0]["html"]


def test_blocks_share_the_plate_cache(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    drawn = []

    def plate(places, archive=None):
        drawn.append(1)
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="1" cy="1" r="1"/></svg>'

    plates: dict = {}
    blocks(plan, media, plate=plate, plates=plates)
    blocks(plan, media, plate=plate, plates=plates)
    assert len(drawn) == 1


# routes

def test_made_book_serves_its_saved_blocks(client, app_parts, g):
    app, _ = app_parts
    a = page(g, "Trebuchet")
    page(g, "Counterweight", a, T0 + 1)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}, "title": "Siege"}, expect=202)
    assert wait_book(client, r["id"])["status"] == "done"
    saved = app.var_dir / "books" / f"{r['id']}.blocks.json"
    assert saved.is_file()
    assert not list((app.var_dir / "books").glob("*.part"))
    got = client.get(f"/v1/books/{r['id']}/blocks")
    assert got["title"] == "Siege" and got["rebuilt"] is False
    kinds = [b["kind"] for b in got["blocks"]]
    assert kinds[0] == "cover" and kinds[-1] == "back"
    assert any("Trebuchet is a page." in b["html"] for b in got["blocks"])
    # what is served is what was saved, not the trail as it is now
    page(g, "Later page", a, T0 + 2)
    again = client.get(f"/v1/books/{r['id']}/blocks")
    assert again == got
    assert not any("Later page" in b["html"] for b in again["blocks"])
    # deleting the book deletes its blocks
    client.post(f"/v1/books/{r['id']}/delete", {})
    assert not saved.exists()
    client.get(f"/v1/books/{r['id']}/blocks", expect=404)


def test_book_without_saved_blocks_is_rebuilt(client, app_parts, g):
    app, _ = app_parts
    a = page(g, "Trebuchet")
    page(g, "Counterweight", a, T0 + 1)
    page(g, "Elsewhere", None, T0 + 2)
    # a book made before v0.15.0: a done row with its params, and no blocks file
    old = g.add_book("Old engines", {"scope": {"kind": "branch", "node_id": a.id},
                                     "include": {"pictures": True, "maps": True, "timelines": True,
                                                 "diagrams": True, "stories": True},
                                     "title": "Old engines", "by": "A reader"})
    g.update_book(old, status="done", epub_size=1, html_size=1)
    got = client.get(f"/v1/books/{old}/blocks")
    assert got["rebuilt"] is True and got["title"] == "Old engines"
    html = "".join(b["html"] for b in got["blocks"])
    assert "Counterweight" in html and "Elsewhere" not in html and "By A reader" in html
    assert not (app.var_dir / "books" / f"{old}.blocks.json").exists()  # nothing is written
    # a saved file that can't be read is rebuilt too
    books = app.var_dir / "books"
    books.mkdir(parents=True, exist_ok=True)
    (books / f"{old}.blocks.json").write_text("{not json", encoding="utf-8")
    assert client.get(f"/v1/books/{old}/blocks")["rebuilt"] is True


def test_blocks_route_404_and_409(client, app_parts, g, monkeypatch):
    import threading

    import primer.book.service as service

    for bad in ("nope", "0" * 16, "F" * 16, "..%2f..%2fx"):
        client.get(f"/v1/books/{bad}/blocks", expect=404)
    page(g, "Trebuchet")
    go = threading.Event()
    real = service.write_epub

    def slow(*a, **kw):
        go.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(service, "write_epub", slow)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    try:
        client.get(f"/v1/books/{r['id']}/blocks", expect=409)
    finally:
        go.set()
    assert wait_book(client, r["id"])["status"] == "done"
    failed = g.add_book("F", {"scope": {"kind": "all"}})
    g.update_book(failed, status="failed", error="x")
    client.get(f"/v1/books/{failed}/blocks", expect=409)


def test_live_blocks_follow_the_scope(client, app_parts, g, monkeypatch):
    app, _ = app_parts
    a = page(g, "Trebuchet")
    page(g, "Counterweight", a, T0 + 10)
    page(g, "Castles", None, T0 + 20)
    unread = g.node_for("Unread", "concept")

    def heads(body):
        got = client.post("/v1/books/blocks", body)
        assert set(got) == {"blocks", "title"}
        return got, ["".join(ET.fromstring(b["html"]).itertext()) for b in got["blocks"]
                     if b["kind"] == "chapter-head"]

    got, h = heads({"scope": {"kind": "all"}, "include": {}})
    assert got["title"] == "My wanderings" and len(h) == 3
    got, h = heads({"scope": {"kind": "branch", "node_id": a.id}, "include": {}, "title": "Mine", "by": "Me"})
    assert got["title"] == "Mine" and len(h) == 2 and h[0].startswith("Trebuchet")
    got, h = heads({"scope": {"kind": "since", "since": T0 + 5}})
    assert len(h) == 2 and h[0].startswith("Counterweight")
    long = client.post("/v1/books/blocks", {"scope": {"kind": "all"}, "title": "x" * 500, "by": "y" * 500})
    assert long["title"] == "x" * 120
    assert not (app.var_dir / "books").exists() or not list((app.var_dir / "books").iterdir())
    assert client.get("/v1/books") == {"books": []}  # read-only: no book, no files
    # bad requests
    for scope in ({"kind": "nope"}, {"kind": "since", "since": "x"}, "all", {"kind": "branch"}):
        client.post("/v1/books/blocks", {"scope": scope}, expect=400)
    client.post("/v1/books/blocks", {"include": {}}, expect=400)
    client.post("/v1/books/blocks", {"scope": {"kind": "all"}, "include": "all"}, expect=400)
    client.post("/v1/books/blocks", {"scope": {"kind": "all"}, "title": 5}, expect=400)
    client.post("/v1/books/blocks", {"scope": {"kind": "all"}, "by": ["x"]}, expect=400)
    client.post("/v1/books/blocks", {"scope": {"kind": "branch", "node_id": "f" * 16}}, expect=404)
    client.post("/v1/books/blocks", {"scope": {"kind": "branch", "node_id": unread.id}}, expect=404)
    # over the limits
    import primer.book.service as service

    monkeypatch.setattr(service, "MAX_BYTES", 10)
    big = client.post("/v1/books/blocks", {"scope": {"kind": "all"}}, expect=413)
    assert big["error"] == "Too big: leave out pictures or choose less"


def test_start_up_sweep_keeps_done_blocks_only(g, tmp_path):
    from primer.book.service import BookService

    books = tmp_path / "var" / "books"
    books.mkdir(parents=True)
    done = g.add_book("Done", {})
    g.update_book(done, status="done", epub_size=1, html_size=1)
    failed = g.add_book("Failed", {})
    g.update_book(failed, status="failed", error="x")
    orphan = "0123456789abcdef"
    keep = [f"{done}.blocks.json", f"{done}.epub"]
    drop = [f"{done}.blocks.json.part", f"{failed}.blocks.json", f"{orphan}.blocks.json"]
    for name in keep + drop:
        (books / name).write_bytes(b"{}")
    svc = BookService(g, tmp_path / "var", lambda: None)
    try:
        assert sorted(p.name for p in books.iterdir()) == sorted(keep)
    finally:
        svc.shutdown()


def test_saved_blocks_are_json_with_the_blocks(client, app_parts, g):
    app, _ = app_parts
    page(g, "Trebuchet")
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert wait_book(client, r["id"])["status"] == "done"
    data = json.loads((app.var_dir / "books" / f"{r['id']}.blocks.json").read_text(encoding="utf-8"))
    assert data["blocks"] == client.get(f"/v1/books/{r['id']}/blocks")["blocks"]


# Fix round 1

def _all_text(html: str) -> str:
    return "".join(ET.fromstring(html).itertext())


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_guard_allowlists_elements_and_attributes():
    from primer.book.blocks import _html, guard

    def g(s):
        return _html(ET.fromstring(s))

    media = "/v1/media/" + "c" * 64 + ".png"
    for bad in ("<script>x</script>", "<SCRIPT>x</SCRIPT>", "<sCrIpT>x</sCrIpT>", "<style>p{}</style>",
                "<iframe/>", "<object/>", "<video/>", "<form/>"):
        assert g(bad) == "", bad
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
           '<animate attributeName="href" to="javascript:x"/><set attributeName="x" to="1"/>'
           '<use href="#a"/><foreignObject><p>f</p></foreignObject><FOREIGNOBJECT/>'
           '<a href="https://example.org">t</a><SCRIPT>1</SCRIPT>'
           '<rect style="fill:url(https://example.org/x)" width="1" height="1"/>'
           '<RECT width="2" height="2"/><text x="1" onclick="y">ok</text></svg>')
    out = g(svg)
    for gone in ("animate", "<set", "<use", "oreign", "<a", "SCRIPT", "style", "onclick", "example.org"):
        assert gone not in out, gone
    assert '<rect width="1" height="1"></rect>' in out and '<rect width="2" height="2"></rect>' in out
    assert '<text x="1">ok</text>' in out and out.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert g('<p style="background:url(https://example.org/x)">a</p>') == "<p>a</p>"
    assert g('<P CLASS="x">a</P>') == '<p CLASS="x">a</p>'
    # http(s) only on a@href; img@src only our media
    assert g('<a href="https://example.org/a">a</a>') == '<a href="https://example.org/a">a</a>'
    assert g('<a href="#c1-src-2">1</a>') == '<a href="#c1-src-2">1</a>'
    assert g('<a href="/v1/media/x">a</a>') == "<a>a</a>"
    assert g('<p href="https://example.org">a</p>') == "<p>a</p>"
    assert g('<img src="https://example.org/x.png" alt="a"/>') == '<img alt="a"/>'
    assert g('<IMG SRC="https://example.org/x.png"/>') == "<img/>"
    assert g(f'<img src="{media}" alt="a"/>') == f'<img src="{media}" alt="a"/>'
    assert g(f'<a href="{media}">a</a>') == "<a>a</a>"
    # no attribute that says javascript:, in any case or spacing
    for v in ("javascript:alert(1)", "JavaScript :x", "  javascript\t:x"):
        assert g(f'<img src="{media}" alt="{v}" title="ok"/>') == f'<img src="{media}" title="ok"/>'
    # text keeps its words and spacing, with the colon as a reference
    assert g("<p>see javascript  :x and JAVASCRIPT:y</p>") == "<p>see javascript  &#58;x and JAVASCRIPT&#58;y</p>"
    assert _all_text(g("<p>see javascript  :x</p>")) == "see javascript  :x"
    # the guard is idempotent, and `guard` says so for one element only
    assert guard("<p>a</p>") and guard(out) and guard(g("<p>javascript:x</p>"))
    assert not guard("<p>a</p><p>b</p>") and not guard("text<p>a</p>") and not guard("<p>a")
    assert not guard('<p onclick="x">a</p>') and not guard("<SCRIPT>1</SCRIPT>")


def test_every_block_passes_the_guard_unchanged(tmp_path):
    from primer.book.blocks import guard

    media = tmp_path / "media"
    plan = make_plan(media)
    for b in blocks(plan, media):
        assert guard(b["html"]), b


def test_stray_text_becomes_a_para():
    from primer.book.blocks import _chapter, _stories

    section = ET.fromstring('<section class="chapter" id="c1"><h1>T</h1> lost words <p>a</p>'
                            ' more <div class="text">in div<p>b</p>after</div></section>')
    out = _chapter(1, "n1", section)
    assert [b["kind"] for b in out] == ["chapter-head", "para", "para", "para", "para", "para", "para"]
    assert [_all_text(b["html"]) for b in out[1:]] == ["lost words", "a", "more", "in div", "b", "after"]
    assert all(b["chapter"] == 1 for b in out)
    stories = _stories(ET.fromstring('<section id="stories">x<h1>Stories</h1><section class="story">'
                                     "<h2>S</h2>y<p>z</p></section>w</section>"))
    assert [_all_text(b["html"]) for b in stories] == ["x", "Stories", "S", "y", "z", "w"]


def test_contents_is_one_block_per_entry_with_its_depth(tmp_path):
    from primer.book.model import BookPlan
    from tests.test_book_writers import T0 as W0
    from tests.test_book_writers import _chapter as wchapter

    chapters = [wchapter(1, "P1")] + [wchapter(i, f"P{i}", f"n{i - 1}", min(i, 6)) for i in range(2, 10)]
    chapters.append(wchapter(10, "Q", "n2", 3))
    plan = BookPlan("T", "", chapters, [], W0, W0 + 10)
    entries = [b for b in blocks(plan, tmp_path) if b["kind"] == "contents"]
    assert _all_text(entries[0]["html"]) == "Contents"
    got = [(re.search(r'class="contents (d\d)"', b["html"]).group(1), _all_text(b["html"]))
           for b in entries[1:]]
    assert got == [("d1", "P1"), ("d2", "P2"), ("d3", "P3"), ("d4", "P4"), ("d5", "P5"), ("d6", "P6"),
                   ("d6", "P7"), ("d6", "P8"), ("d6", "P9"), ("d3", "Q"), ("d1", "Closing note")]
    for b in entries[1:]:
        assert b["html"].count("<li>") == 1  # never nested: no entry holds another


def test_timeline_split_into_blocks_of_eight(tmp_path):
    from tests.test_book_writers import _event

    media = tmp_path / "media"
    plan = make_plan(media)
    plan.chapters[0].timeline = [_event(f"Event {k}", str(1200 + k), 1200 + k) for k in range(20)]
    out = [b for b in blocks(plan, media) if b["kind"] == "timeline"]
    assert len(out) == 3 and all(b["chapter"] == 1 for b in out)
    assert [b["html"].count("<li>") for b in out] == [8, 8, 4]
    assert "<h2>Timeline</h2>" in out[0]["html"]
    assert "Timeline" not in "".join(_all_text(b["html"]) for b in out[1:])
    labels = re.findall(r"Event \d+", "".join(b["html"] for b in out))
    assert labels == [f"Event {k}" for k in range(20)]
    one = [b for b in blocks(plan, media) if b["chapter"] == 1]
    assert not any(b["kind"] == "heading" and "Timeline" in b["html"] for b in one)


def test_no_text_lost_or_repeated_in_the_split(tmp_path):
    from primer.book import xhtml
    from primer.book.plate import map_plate

    media = tmp_path / "media"
    plan = make_plan(media)
    out = blocks(plan, media)
    links = media_links(media)
    index = {ch.node_id: i for i, ch in enumerate(plan.chapters, start=1)}
    for i, ch in enumerate(plan.chapters, start=1):
        whole = ET.fromstring(xhtml.chapter(i, ch, index, links, map_plate, None))
        mine = "".join(_all_text(b["html"]) for b in out if b["chapter"] == i)
        assert _squash(mine) == _squash("".join(whole.itertext())), i


def _made(client, g):
    page(g, "Trebuchet")
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert wait_book(client, r["id"])["status"] == "done"
    return r["id"]


def test_saved_blocks_are_checked_on_read(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    app, _ = app_parts
    book_id = _made(client, g)
    path = app.var_dir / "books" / f"{book_id}.blocks.json"
    good = json.loads(path.read_text(encoding="utf-8"))
    assert client.get(f"/v1/books/{book_id}/blocks")["rebuilt"] is False
    head = {"kind": "para", "html": "<p>a</p>", "chapter": 1}
    bad = [{**head, "html": '<p onclick="x">a</p>'}, {**head, "html": "<script>1</script>"},
           {**head, "html": "<p>a</p><p>b</p>"}, {**head, "html": '<img src="https://example.org/x.png"/>'},
           {**head, "kind": "iframe"}, {**head, "chapter": -1}, {**head, "chapter": True},
           {**head, "chapter": "1"}, {**head, "html": 5}, {**head, "node_id": 3}, "a block", None]
    for b in bad:
        path.write_text(json.dumps({"blocks": good["blocks"] + [b]}), encoding="utf-8")
        got = client.get(f"/v1/books/{book_id}/blocks")
        assert got["rebuilt"] is True, b
        assert all("onclick" not in x["html"] and "example.org" not in x["html"] for x in got["blocks"])
    path.write_text(json.dumps({"blocks": good["blocks"] + [head]}), encoding="utf-8")
    assert client.get(f"/v1/books/{book_id}/blocks")["rebuilt"] is False  # a good block is fine
    monkeypatch.setattr(service, "MAX_BLOCKS_FILE", 10)  # too big to read: rebuilt
    assert client.get(f"/v1/books/{book_id}/blocks")["rebuilt"] is True


def test_one_layout_at_a_time_and_rebuilds_are_limited(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    app, _ = app_parts
    book_id = _made(client, g)
    (app.var_dir / "books" / f"{book_id}.blocks.json").unlink()
    assert app.books._layout.acquire(blocking=False)  # another layout is running
    try:
        busy = client.post("/v1/books/blocks", {"scope": {"kind": "all"}}, expect=409)
        assert busy["error"] == "Primer is already laying out a book"
        assert client.get(f"/v1/books/{book_id}/blocks", expect=409)["error"] == busy["error"]
    finally:
        app.books._layout.release()
    assert client.post("/v1/books/blocks", {"scope": {"kind": "all"}})["blocks"]
    assert client.get(f"/v1/books/{book_id}/blocks")["rebuilt"] is True
    monkeypatch.setattr(service, "MAX_BYTES", 10)
    big = client.get(f"/v1/books/{book_id}/blocks", expect=413)
    assert big["error"] == "Too big: leave out pictures or choose less"
    assert app.books._layout.acquire(blocking=False)  # every path let it go
    app.books._layout.release()


def test_a_failing_blocks_stage_does_not_fail_the_book(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    app, _ = app_parts
    page(g, "Trebuchet")
    real = service.book_blocks

    def broken(*a, **kw):
        raise RuntimeError("no blocks")

    monkeypatch.setattr(service, "book_blocks", broken)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    b = wait_book(client, r["id"])
    assert b["status"] == "done" and b["epub_size"] > 0
    books = app.var_dir / "books"
    assert sorted(p.name for p in books.iterdir()) == sorted([f"{r['id']}.epub", f"{r['id']}.html"])
    monkeypatch.setattr(service, "book_blocks", real)
    assert client.get(f"/v1/books/{r['id']}/blocks")["rebuilt"] is True


def test_a_delete_or_stop_in_the_blocks_stage_leaves_no_blocks(client, app_parts, g, monkeypatch, tmp_path):
    import threading
    import time

    import primer.book.service as service
    from primer.book.service import BookService

    app, _ = app_parts
    page(g, "Trebuchet")
    real = service.book_blocks
    started, go = threading.Event(), threading.Event()

    def slow(*a, **kw):
        started.set()
        go.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(service, "book_blocks", slow)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert started.wait(5)
    assert client.post(f"/v1/books/{r['id']}/delete", {}) == {"deleted": True}
    go.set()
    end = time.time() + 5
    while app.books._busy is not None and time.time() < end:
        time.sleep(0.05)
    assert app.books._busy is None
    assert not list((app.var_dir / "books").glob(f"{r['id']}*"))
    # a shutdown in the blocks stage: the book fails and leaves nothing
    started.clear()
    go.clear()
    svc = BookService(g, tmp_path / "v3", lambda: None)
    bid = svc.create({"kind": "all"}, {}, "", "")
    assert started.wait(5)
    svc.shutdown("Primer shut down")
    go.set()
    svc._worker.join(5)
    assert g.get_book(bid)["status"] == "failed"
    assert list((tmp_path / "v3" / "books").iterdir()) == []


def test_live_route_cuts_the_by_line(client, app_parts, g):
    page(g, "Trebuchet")
    got = client.post("/v1/books/blocks", {"scope": {"kind": "all"}, "by": "y" * 500})
    cover = _all_text(got["blocks"][0]["html"])
    assert "By " + "y" * 80 in cover and "y" * 81 not in cover


def test_guard_keeps_only_attribute_names_the_builders_use():
    from primer.book.blocks import guard
    media = "/v1/media/" + "a" * 64 + ".png"
    assert guard(f'<figure><img src="{media}" alt="x"/></figure>')
    for bad in (f'<figure><img src="{media}" srcset="http://169.254.169.254/x 1x"/></figure>',
                '<p><a href="#c1" ping="http://169.254.169.254/">x</a></p>',
                '<p><a href="#c1" formaction="#c2">x</a></p>',
                '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="1" cy="1" r="1" filter="url(#x)"></circle></svg>'):
        assert not guard(bad), bad


def test_guard_refuses_deep_nesting_without_blowing_up():
    from primer.book.blocks import guard
    deep = "<div>" * 3000 + "x" + "</div>" * 3000
    assert guard(deep) is False
    assert guard("<div>" * 70 + "x" + "</div>" * 70) is False
