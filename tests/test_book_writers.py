"""The book writers: an EPUB 3 and a single HTML file from one BookPlan."""
import re
import xml.etree.ElementTree as ET
import zipfile
from posixpath import dirname, join, normpath

from primer.book.epub import write_epub
from primer.book.html import write_html
from primer.book.markdown import render
from primer.book.model import BookPlan, Chapter, MapData, Picture, Story
from primer.diagrams.svg import clean_svg

T0 = 1_762_700_000.0
XHTML = "{http://www.w3.org/1999/xhtml}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
EPUB_NS = "{http://www.idpf.org/2007/ops}"
DRAWING = ('<svg viewBox="0 0 1000 700"><g id="arm"><line x1="1" y1="2" x2="3" y2="4" stroke="black"/></g>'
           '<text x="100" y="620">Counterweight</text><text x="450" y="350">Pivot</text>'
           '<text x="800" y="90">Sling</text><script>alert(1)</script></svg>')
FORBIDDEN = (b"<script", b"127.0.0.1", b"javascript:")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
NASTY = '<b>&"x"</b>'


def _event(label, date_text, year, end_year=None, approx=False):
    return {"label": label, "date_text": date_text, "year": year, "month": None, "day": None,
            "end_year": end_year, "precision": "year", "approx": approx}


def make_plan(media, title_one="Trebuchet", pic_file=None):
    media.mkdir(exist_ok=True)
    name = pic_file or ("a" * 64 + ".png")
    if pic_file is None:
        (media / name).write_bytes(PNG)
    pic = Picture("m1", name, "the arm")
    svg, _ = clean_svg(DRAWING, prefix="dgabc123-")
    sources = [{"n": 1, "title": "Trebuchet", "kind": "Wikipedia", "url": ""},
               {"n": 3, "title": "Siege <engines>", "kind": "web", "url": "https://example.org/siege"}]
    one = Chapter("n1", title_one, "it", None, 1, T0,
                  render("# T\nIt throws [1] and [3].", anchor="c1", n_sources=2, numbers=[1, 3]),
                  sources, pictures=[pic],
                  maps=[MapData("map1", "the castles", [
                      {"n": 1, "name": "Harlech", "what": "a castle", "lat": 52.86, "lon": -4.11},
                      {"n": 2, "name": "Conwy", "what": "", "lat": 53.28, "lon": -3.83}])],
                  timeline=[_event("Built", "1283", 1283), _event("Besieged", "c. 1294", 1294, approx=True),
                            _event("Rebuilt", "1300", 1300, end_year=1310)],
                  diagram={"svg": svg, "caption": "How it <throws>"}, has_video=True)
    two = Chapter("n2", "Counterweight", "counterweight", "n1", 2, T0 + 1,
                  render("# C\nHeavy.", anchor="c2", n_sources=0), via_title=title_one)
    three = Chapter("n3", "Castles", "castles", None, 1, T0 + 2,
                    render("# K\nStone.", anchor="c3", n_sources=0), via_title="Somewhere else")
    story = Story("The throw", [{"title": "One", "text": "She pulls.", "picture": pic},
                                {"title": "Two", "text": "He lets go.", "picture": None}])
    return BookPlan("My wanderings", "A reader", [one, two, three], [story], T0, T0 + 2)


def _xml(data: bytes):
    return ET.fromstring(data)


def _text(el) -> str:
    return "".join(el.itertext())


def test_epub_structure(tmp_path):
    plan = make_plan(tmp_path / "media")
    out = tmp_path / "book.epub"
    seen = []
    size = write_epub(plan, tmp_path / "media", out, progress=lambda i, n: seen.append((i, n)))
    assert size == out.stat().st_size and seen[-1] == (3, 3)
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert infos[1].filename == "META-INF/container.xml"
        names = set(z.namelist())
        container = _xml(z.read("META-INF/container.xml"))
        (rootfile,) = container.iter("{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = rootfile.get("full-path")
        assert opf_path == "OEBPS/content.opf" and opf_path in names
        opf = _xml(z.read(opf_path))
        base = dirname(opf_path)
        items = {i.get("id"): i for i in opf.iter(f"{OPF}item")}
        hrefs = {normpath(join(base, i.get("href"))): i for i in items.values()}
        assert all(h in names for h in hrefs)  # every manifest item exists
        for name in names - {"mimetype", opf_path}:  # every file is in the manifest
            assert name.startswith("META-INF/") or name in hrefs, name
        assert [n for n in names if n.startswith("OEBPS/images/")] == ["OEBPS/images/" + "a" * 64 + ".png"]
        # metadata
        meta = opf.find(f"{OPF}metadata")
        assert meta.find(f"{DC}identifier").text.startswith("urn:uuid:")
        assert opf.get("unique-identifier") == meta.find(f"{DC}identifier").get("id")
        assert meta.find(f"{DC}title").text == "My wanderings"
        assert meta.find(f"{DC}language").text == "en"
        assert meta.find(f"{DC}creator").text == "A reader"
        modified = [m for m in meta.iter(f"{OPF}meta") if m.get("property") == "dcterms:modified"]
        assert len(modified) == 1 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", modified[0].text)
        # spine: cover, nav, chapters, stories, end
        spine = [items[r.get("idref")].get("href") for r in opf.iter(f"{OPF}itemref")]
        assert spine == ["cover.xhtml", "nav.xhtml", "chapter-0001.xhtml", "chapter-0002.xhtml",
                         "chapter-0003.xhtml", "stories.xhtml", "end.xhtml"]
        # every XHTML file parses; inline SVG is declared
        for path, item in hrefs.items():
            if item.get("media-type") == "application/xhtml+xml":
                data = z.read(path)
                _xml(data)
                props = (item.get("properties") or "").split()
                assert ("svg" in props) == (b"<svg" in data), path
        assert "nav" in (items[[k for k, v in items.items() if v.get("href") == "nav.xhtml"][0]]
                         .get("properties") or "").split()
        # the nav nests by depth
        nav = _xml(z.read("OEBPS/nav.xhtml"))
        toc = next(n for n in nav.iter(f"{XHTML}nav") if n.get(f"{EPUB_NS}type") == "toc")
        top = toc.find(f"{XHTML}ol")
        links = [a.get("href") for a in toc.iter(f"{XHTML}a") if a.get("href").startswith("chapter-")]
        assert links == ["chapter-0001.xhtml#c1", "chapter-0002.xhtml#c2", "chapter-0003.xhtml#c3"]
        firsts = top.findall(f"{XHTML}li")
        assert firsts[0].find(f"{XHTML}a").get("href") == "chapter-0001.xhtml#c1"
        assert firsts[0].find(f"{XHTML}ol/{XHTML}li/{XHTML}a").get("href") == "chapter-0002.xhtml#c2"
        assert firsts[1].find(f"{XHTML}a").get("href") == "chapter-0003.xhtml#c3"
        # the chapters
        one = z.read("OEBPS/chapter-0001.xhtml").decode("utf-8")
        assert 'id="c1"' in one and 'id="c1-src-1"' in one and 'id="c1-src-3"' in one
        assert 'href="#c1-src-3"' in one and 'href="https://example.org/siege"' in one
        assert "Siege &lt;engines&gt;" in one and "How it &lt;throws&gt;" in one
        assert 'src="images/' + "a" * 64 + '.png"' in one and "the arm" in one
        assert "Drawn without the world map" in one and "Harlech, a castle" in one and "Conwy" in one
        assert "A video clip was made of this page in Primer." in one
        assert 'class="timeline"' in one and "about c. 1294" not in one and "about" in one
        assert "to 1310" in one
        assert 'id="dgabc123-arm"' in one
        two = z.read("OEBPS/chapter-0002.xhtml").decode("utf-8")
        assert 'href="chapter-0001.xhtml#c1"' in two and "counterweight" in two
        three = _xml(z.read("OEBPS/chapter-0003.xhtml"))
        via = next(p for p in three.iter(f"{XHTML}p") if p.get("class") == "via")
        assert _text(via) == "From castles in Somewhere else" and via.find(f".//{XHTML}a") is None
        stories = z.read("OEBPS/stories.xhtml").decode("utf-8")
        assert "The throw" in stories and "She pulls." in stories and "images/" in stories
        end = z.read("OEBPS/end.xhtml").decode("utf-8")
        assert "Made with Primer, from what I read between" in end
        cover = z.read("OEBPS/cover.xhtml").decode("utf-8")
        assert "My wanderings" in cover and "A reader" in cover and "images/" in cover
        for name in names:
            data = z.read(name).lower()
            assert not any(bad in data for bad in FORBIDDEN), name


def test_html_single_file(tmp_path):
    plan = make_plan(tmp_path / "media")
    out = tmp_path / "book.html"
    seen = []
    size = write_html(plan, tmp_path / "media", out, progress=lambda i, n: seen.append((i, n)))
    data = out.read_bytes()
    assert size == len(data) and seen[-1] == (3, 3)
    assert data.startswith(b"<!DOCTYPE html>")
    assert not any(bad in data.lower() for bad in FORBIDDEN)
    root = _xml(data)
    ids = {el.get("id") for el in root.iter() if el.get("id")}
    chapters = [el for el in root.iter(f"{XHTML}section") if "chapter" in (el.get("class") or "").split()]
    assert [c.get("id") for c in chapters] == ["c1", "c2", "c3"]
    contents = next(n for n in root.iter(f"{XHTML}nav") if n.get("id") == "contents")
    links = [a.get("href") for a in contents.iter(f"{XHTML}a")]
    assert [x for x in links if re.fullmatch(r"#c\d+", x)] == ["#c1", "#c2", "#c3"]
    for href in [a.get("href") for a in root.iter(f"{XHTML}a")]:
        if href.startswith("#"):
            assert href[1:] in ids, href  # every in-book link resolves
        else:
            assert href.startswith("https://"), href
    imgs = [i.get("src") for i in root.iter(f"{XHTML}img")]
    assert imgs and all(s.startswith("data:image/png;base64,") for s in imgs)
    text = data.decode("utf-8")
    assert "<style>" in text and "<link" not in text
    assert "Drawn without the world map" in text and 'id="dgabc123-arm"' in text
    assert "c1-src-3" in ids and "She pulls." in text


def test_writers_escape_titles(tmp_path):
    plan = make_plan(tmp_path / "media", title_one=NASTY)
    plan.title = NASTY
    plan.by = NASTY
    write_epub(plan, tmp_path / "media", tmp_path / "b.epub")
    write_html(plan, tmp_path / "media", tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        for name in ("OEBPS/nav.xhtml", "OEBPS/chapter-0001.xhtml", "OEBPS/chapter-0002.xhtml",
                     "OEBPS/cover.xhtml", "OEBPS/content.opf"):
            data = z.read(name)
            root = _xml(data)
            assert root.find(f".//{XHTML}b") is None, name
            assert NASTY in _text(root) or any(NASTY == (e.text or "") for e in root.iter()), name
        nav = _xml(z.read("OEBPS/nav.xhtml"))
        assert any(_text(a) == NASTY for a in nav.iter(f"{XHTML}a"))
        one = _xml(z.read("OEBPS/chapter-0001.xhtml"))
        assert any(_text(h) == NASTY for h in one.iter(f"{XHTML}h1"))
    root = _xml((tmp_path / "b.html").read_bytes())
    assert root.find(f".//{XHTML}b") is None
    assert any(_text(h) == NASTY for h in root.iter(f"{XHTML}h1"))
    assert root.find(f"{XHTML}head/{XHTML}title").text == NASTY


def test_missing_media_skipped(tmp_path):
    media = tmp_path / "media"
    gone = "b" * 64 + ".png"  # collected, then the file vanished
    plan = make_plan(media, pic_file=gone)
    plan.chapters[1].pictures = [Picture("m2", "../../secret.png", "sneaky")]  # not a media name
    (tmp_path / "secret.png").write_bytes(PNG)
    write_epub(plan, media, tmp_path / "b.epub")
    write_html(plan, media, tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        assert not [n for n in z.namelist() if n.startswith("OEBPS/images/")]
        for name in z.namelist():
            if name.endswith(".xhtml"):
                data = z.read(name)
                _xml(data)
                assert b"<img" not in data and b"the arm" not in data and b"sneaky" not in data
    html = (tmp_path / "b.html").read_bytes()
    _xml(html)
    assert b"<img" not in html and b"data:image" not in html


def _chapter(i, title, parent=None, depth=1, pictures=()):
    return Chapter(f"n{i}", title, "it", parent, depth, T0 + i, f"<p>{title}</p>",
                   pictures=list(pictures), via_title="")


def _nav_depths(nav_xml: bytes, prefix="chapter-") -> list[tuple[str, int, str]]:
    """(link label, nesting depth, label of the link the list hangs from) for each chapter link."""
    root = _xml(nav_xml)
    parents = {c: p for p in root.iter() for c in p}
    out = []
    for a in root.iter(f"{XHTML}a"):
        if not a.get("href").startswith(prefix):
            continue
        depth, el, owner = 0, a, ""
        while el in parents:
            el = parents[el]
            if el.tag == f"{XHTML}ol":
                depth += 1
            elif el.tag == f"{XHTML}li" and not owner and el is not parents[a]:
                owner = _text(el.find(f"{XHTML}a"))
        out.append((_text(a), depth, owner))
    return out


def test_contents_nest_under_the_chapter_branched_from(tmp_path):
    # read A, then B (a fresh start), then branch from A to C: C sits under A, not under B
    plan = BookPlan("T", "", [_chapter(1, "A"), _chapter(2, "B"), _chapter(3, "C", "n1", 2)], [],
                    T0, T0 + 3)
    write_epub(plan, tmp_path, tmp_path / "b.epub")
    write_html(plan, tmp_path, tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        assert _nav_depths(z.read("OEBPS/nav.xhtml")) == [("A", 1, ""), ("C", 2, "A"), ("B", 1, "")]
        spine = [r.get("idref") for r in _xml(z.read("OEBPS/content.opf")).iter(f"{OPF}itemref")]
        assert spine[2:5] == ["ch0001", "ch0002", "ch0003"]  # the spine stays in trail order
    assert _nav_depths((tmp_path / "b.html").read_bytes(), "#c") == [("A", 1, ""), ("C", 2, "A"),
                                                                     ("B", 1, "")]


def test_contents_chain_of_nine_stops_nesting_at_six(tmp_path):
    chapters = [_chapter(1, "P1")] + [_chapter(i, f"P{i}", f"n{i - 1}", min(i, 6)) for i in range(2, 10)]
    chapters.append(_chapter(10, "Q", "n2", 3))  # a later branch from P2
    plan = BookPlan("T", "", chapters, [], T0, T0 + 10)
    write_epub(plan, tmp_path, tmp_path / "b.epub")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        got = _nav_depths(z.read("OEBPS/nav.xhtml"))
    assert got == [("P1", 1, ""), ("P2", 2, "P1"), ("P3", 3, "P2"), ("P4", 4, "P3"), ("P5", 5, "P4"),
                   ("P6", 6, "P5"), ("P7", 6, "P5"), ("P8", 6, "P5"), ("P9", 6, "P5"), ("Q", 3, "P2")]


def test_event_date_end_year_as_a_whole_number():
    from primer.book.xhtml import event_date

    assert event_date(_event("x", "1919", 5, end_year=19)) == "1919 to 19"
    assert event_date(_event("x", "1910-1919", 1910, end_year=1919)) == "1910-1919"
    assert event_date(_event("x", "c. 1294", 1294, approx=True)) == "about 1294"
    assert event_date(_event("x", "", -500, end_year=-400)) == "500 BC to 400 BC"


def test_unreadable_picture_left_out_at_write_time(tmp_path, monkeypatch):
    import primer.book.xhtml as xhtml_mod

    plan = make_plan(tmp_path / "media")
    real = xhtml_mod.picture_path
    # the name checks out and the file was there when looked for, but it can't be read
    monkeypatch.setattr(xhtml_mod, "picture_path",
                        lambda media_dir, name: tmp_path / "gone" / name if real(media_dir, name) else None)
    write_epub(plan, tmp_path / "media", tmp_path / "b.epub")
    write_html(plan, tmp_path / "media", tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        names = z.namelist()
        assert not [n for n in names if "images/" in n]
        opf = z.read("OEBPS/content.opf")
        assert b"images/" not in opf and b"cover-image" not in opf
        for n in names:
            if n.endswith(".xhtml"):
                assert b"<img" not in z.read(n)
        assert b"1 picture could not be found" in z.read("OEBPS/end.xhtml")
    assert b"<img" not in (tmp_path / "b.html").read_bytes()


def test_writers_check_for_a_stop_before_each_picture(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    second = "c" * 64 + ".png"
    (media / second).write_bytes(PNG)
    plan.chapters[1].pictures = [Picture("m3", second, "another")]
    for write in (write_epub, write_html):
        calls = []
        write(plan, media, tmp_path / "b.out", progress=lambda i, n: calls.append((i, n)))
        assert len(calls) >= 1 + 3 + 2  # the start, each chapter, each picture

        class Stop(Exception):
            pass

        def stop_at_picture(i, n, seen=[]):
            seen.append(i)
            if len(seen) == 2:  # the first call after the start: the cover's picture
                raise Stop()

        try:
            write(plan, media, tmp_path / "b.out", progress=stop_at_picture)
        except Stop:
            pass
        else:
            raise AssertionError(f"{write.__name__} did not stop at a picture")


def test_hostile_values_escaped_everywhere(tmp_path):
    media = tmp_path / "media"
    plan = make_plan(media)
    one = plan.chapters[0]
    one.maps[0].places[0]["name"] = NASTY
    one.maps[0].places[0]["what"] = NASTY
    one.maps[0].phrase = NASTY
    one.timeline[0]["label"] = NASTY
    one.timeline[0]["date_text"] = NASTY
    one.pictures[0].caption = NASTY
    one.diagram["caption"] = NASTY
    one.sources[1]["title"] = NASTY
    plan.by = NASTY
    plan.stories[0].title = NASTY
    plan.stories[0].scenes[0]["text"] = NASTY
    write_epub(plan, media, tmp_path / "b.epub")
    write_html(plan, media, tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        docs = {n: z.read(n) for n in z.namelist() if n.endswith((".xhtml", ".opf"))}
    docs["html"] = (tmp_path / "b.html").read_bytes()
    for name, data in docs.items():
        root = _xml(data)
        assert root.find(f".//{XHTML}b") is None, name
        assert b"<b>" not in data, name
    chapter = _text(_xml(docs["OEBPS/chapter-0001.xhtml"]))
    html = _text(_xml(docs["html"]))
    for text in (chapter, html):
        assert f"{NASTY}, {NASTY}" in text  # the place
        assert text.count(NASTY) >= 7  # place x2, map phrase, label, date, caption, diagram caption...
    assert f"By {NASTY}" in _text(_xml(docs["OEBPS/cover.xhtml"])) and f"By {NASTY}" in html
    assert NASTY in _text(_xml(docs["OEBPS/stories.xhtml"]))


FULLWIDTH_HOSTS = ("http://１２７.０.０.１/x", "http://１７２.３１.０.５/")


def test_unicode_hosts_come_out_as_text_end_to_end(g, tmp_path):
    from primer.book.model import collect
    from primer.grounding import Source

    for k, url in enumerate(FULLWIDTH_HOSTS):
        src = [Source(1, "Local copy", "web", "", url=url)]
        n = g.node_for(f"Page {k}", "concept")
        g.add_version(n.id, f"# Page {k}\nSee [the copy]({url}) and [1].", "storyteller", "normal", src)
        with g._db:
            g._db.execute("INSERT INTO edges(from_id, to_id, phrase, via, at) VALUES(?,?,?,?,?)",
                          (None, n.id, "it", "context", T0 + k))
    media = tmp_path / "media"
    media.mkdir()
    plan = collect(g, {"kind": "all"}, {}, media_dir=media)
    assert [ch.sources[0]["url"] for ch in plan.chapters] == ["", ""]
    write_epub(plan, media, tmp_path / "b.epub")
    write_html(plan, media, tmp_path / "b.html")
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        docs = [z.read(f"OEBPS/chapter-000{i}.xhtml") for i in (1, 2)]
    docs.append((tmp_path / "b.html").read_bytes())
    for data in docs:
        root = _xml(data)
        hrefs = [a.get("href") or "" for a in root.iter(f"{XHTML}a")]
        assert all(h.startswith(("#", "chapter-")) for h in hrefs), hrefs  # no outbound link at all
        assert "the copy" in _text(root) and "Local copy" in _text(root)


def test_html_streams_a_picture_bigger_than_one_chunk(tmp_path):
    import base64
    import os

    media = tmp_path / "media"
    plan = make_plan(media)
    big = "d" * 64 + ".png"
    data = PNG + os.urandom(3 * 64 * 1024 * 2 + 1234)  # over two chunks, not a multiple of 3
    (media / big).write_bytes(data)
    plan.chapters[0].pictures = [Picture("m9", big, "big")]
    plan.stories[0].scenes[0]["picture"] = None
    write_html(plan, media, tmp_path / "b.html")
    root = _xml((tmp_path / "b.html").read_bytes())
    want = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    srcs = [i.get("src") for i in root.iter(f"{XHTML}img")]
    assert srcs == [want, want]  # the cover and the chapter
    assert [i.get("alt") for i in root.iter(f"{XHTML}img")] == ["big", "big"]


def test_html_picture_memory_does_not_grow_with_the_picture(tmp_path):
    """A 6 MB picture is 8 MB of base64. Built as one string it would cost at least the file
    plus its base64 (14 MB, more with the chapter string around it); streamed in 192 KiB chunks
    it costs well under 1 MB. 3 MB sits far from both, so it holds on any machine."""
    import os
    import tracemalloc

    media = tmp_path / "media"
    media.mkdir()
    name = "e" * 64 + ".png"
    (media / name).write_bytes(os.urandom(6 * 1024 * 1024))
    pic = Picture("m1", name, "big")
    plan = BookPlan("Big", "", [Chapter("n1", "One", "", None, 1, T0, "<p>x</p>", pictures=[pic])],
                    [], T0, T0)
    write_html(plan, media, tmp_path / "warm.html")  # imports and caches out of the way
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        write_html(plan, media, tmp_path / "b.html")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak - base < 3 * 1024 * 1024, peak - base
    assert (tmp_path / "b.html").stat().st_size > 2 * 8 * 1024 * 1024  # both copies went in


def test_a_shared_plate_cache_draws_each_map_once(tmp_path):
    from primer.book.plate import map_plate

    plan = make_plan(tmp_path / "media")
    drawn = []

    def plate(places, archive=None):
        drawn.append(len(places))
        return map_plate(places, archive)

    plates: dict = {}
    write_epub(plan, tmp_path / "media", tmp_path / "b.epub", plate=plate, plates=plates)
    write_html(plan, tmp_path / "media", tmp_path / "b.html", plate=plate, plates=plates)
    assert drawn == [2] and list(plates) == ["map1"]
    with zipfile.ZipFile(tmp_path / "b.epub") as z:
        assert b"Drawn without the world map" in z.read("OEBPS/chapter-0001.xhtml")
    assert b"Drawn without the world map" in (tmp_path / "b.html").read_bytes()
