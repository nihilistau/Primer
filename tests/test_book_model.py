"""The book model: chapters in trail order, nested by first arrival, with their lenses."""
import time
from pathlib import Path

import pytest

from primer.book.model import BookPlan, Picture, collect, default_title, estimate
from primer.grounding import Source
from primer.maps import Place

T0 = 1_762_700_000.0  # a fixed clock, so the order comes from `at`, not from when a test ran
DRAWING = ('<svg viewBox="0 0 1000 700"><g id="arm"><line x1="1" y1="2" x2="3" y2="4" stroke="black"/></g>'
           '<text x="100" y="620">Counterweight</text><text x="450" y="350">Pivot</text>'
           '<text x="800" y="90">Sling</text><script>alert(1)</script></svg>')
ALL = {"kind": "all"}


def page(g, title, at=None, text=None, sources=()):
    """A read page; its version's time is `at` (default far in the past, after any edge)."""
    n = g.node_for(title, "concept")
    v = g.add_version(n.id, text or f"# {title}\n{title} is a page.", "storyteller", "normal", sources)
    with g._db:
        g._db.execute("UPDATE versions SET created_at=? WHERE node_id=? AND n=?",
                      (T0 + 10_000 if at is None else at, n.id, v.n))
    return n


def edge(g, from_node, to_node, at, phrase="it"):
    with g._db:
        g._db.execute("INSERT INTO edges(from_id, to_id, phrase, via, at) VALUES(?,?,?,?,?)",
                      (from_node.id if from_node else None, to_node.id, phrase, "context", at))


def image(g, node, i, phrase="", kind="image", status="done", file=None):
    m = g.add_media(node.id, phrase, kind, "m", "p", {})
    return g.update_media(m.id, status=status, file=file or f"{i:064x}.png")


def test_collect_orders_by_first_arrival_and_nests(g):
    a, b, c, d = (page(g, t) for t in "ABCD")
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1, "bee")
    edge(g, b, c, T0 + 2)
    edge(g, a, d, T0 + 3)
    plan = collect(g, ALL, {})
    assert isinstance(plan, BookPlan)
    assert [ch.title for ch in plan.chapters] == ["A", "B", "C", "D"]
    ids = {ch.title: ch.node_id for ch in plan.chapters}
    assert [ch.parent_id for ch in plan.chapters] == [None, ids["A"], ids["B"], ids["A"]]
    assert [ch.depth for ch in plan.chapters] == [1, 2, 3, 2]
    assert plan.chapters[1].via_phrase == "bee" and plan.chapters[0].via_phrase == "it"
    assert [ch.via_title for ch in plan.chapters] == ["", "A", "B", "A"]
    assert plan.chapters[1].arrived_at == T0 + 1
    assert plan.first_at == T0 and plan.last_at == T0 + 3
    assert plan.title == "My wanderings" and plan.by == ""
    assert "A is a page." in plan.chapters[0].html and "<h1>" not in plan.chapters[0].html


def test_collect_handles_cycles_and_repeats(g):
    a, b = page(g, "A"), page(g, "B")
    lone = page(g, "Lone", at=T0 + 50)  # never arrived at by an edge: its version's time
    g.node_for("Unread", "concept")  # no version: not a chapter
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1)
    edge(g, b, a, T0 + 2)
    edge(g, a, b, T0 + 3)
    edge(g, b, b, T0 + 4)
    plan = collect(g, ALL, {})
    assert [ch.title for ch in plan.chapters] == ["A", "B", "Lone"]
    assert [ch.parent_id for ch in plan.chapters] == [None, a.id, None]
    assert [ch.depth for ch in plan.chapters] == [1, 2, 1]
    assert plan.chapters[2].arrived_at == T0 + 50 and plan.chapters[2].via_phrase == ""
    # two pages each first reached from the other: whichever came first sits at the top
    x, y = page(g, "X"), page(g, "Y")
    edge(g, y, x, T0 + 100)
    edge(g, x, y, T0 + 101)
    plan = collect(g, ALL, {})
    got = {ch.title: (ch.parent_id, ch.depth) for ch in plan.chapters}
    assert got["X"] == (None, 1) and got["Y"] == (x.id, 2)


def test_collect_scope_branch_and_since(g):
    a, b, c, d, e = (page(g, t) for t in "ABCDE")
    hidden = g.node_for("Hidden", "concept")  # no version, but in the tree between B and E
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1)
    edge(g, b, c, T0 + 2)
    edge(g, a, d, T0 + 3)
    edge(g, b, hidden, T0 + 4)
    edge(g, hidden, e, T0 + 5)
    edge(g, c, b, T0 + 6)  # a cycle back to the branch root
    plan = collect(g, {"kind": "branch", "node_id": b.id}, {})
    assert [ch.title for ch in plan.chapters] == ["B", "C", "E"]
    assert [ch.parent_id for ch in plan.chapters] == [None, b.id, None]  # A is out of the book
    assert [ch.via_title for ch in plan.chapters] == ["A", "B", "Hidden"]  # named even when out
    assert [ch.depth for ch in plan.chapters] == [1, 2, 1]
    assert plan.title == "B"
    plan = collect(g, {"kind": "since", "since": T0 + 2}, {}, title="  Mine  ", by="Ann")
    assert [ch.title for ch in plan.chapters] == ["C", "D", "E"]
    assert plan.title == "Mine" and plan.by == "Ann"
    for bad in ({"kind": "branch", "node_id": "0" * 16}, {"kind": "branch", "node_id": hidden.id},
                {"kind": "branch"}, {"kind": "since", "since": "yesterday"},
                {"kind": "since", "since": float("nan")}, {"kind": "since", "since": -1},
                {"kind": "since", "since": 1e20}, {"kind": "everything"}, "all", None):
        with pytest.raises(ValueError):
            collect(g, bad, {})
    with pytest.raises(ValueError):
        collect(g, ALL, ["pictures"])
    long = collect(g, ALL, {}, title="t" * 500, by="b" * 500)
    assert len(long.title) == 120 and len(long.by) == 80


def test_collect_caps_depth_and_pictures(g, tmp_path):
    chain = [page(g, f"P{i}") for i in range(9)]
    edge(g, None, chain[0], T0)
    for i in range(1, 9):
        edge(g, chain[i - 1], chain[i], T0 + i)
    for i in range(8):
        image(g, chain[0], i, phrase=f"pic {i}" if i else "")
    image(g, chain[0], 99, kind="video")
    image(g, chain[1], 98, status="failed")
    plan = collect(g, ALL, {})
    assert [ch.depth for ch in plan.chapters] == [1, 2, 3, 4, 5, 6, 6, 6, 6]
    assert plan.chapters[7].parent_id == chain[6].id  # still under its parent, listed at 6
    pics = plan.chapters[0].pictures
    assert len(pics) == 6 and plan.left_out["pictures_cap"] == 2
    assert pics[0] == Picture(pics[0].media_id, f"{0:064x}.png", "The whole page")
    assert [p.caption for p in pics[1:]] == [f"pic {i}" for i in range(1, 6)]
    assert plan.chapters[0].has_video and not plan.chapters[1].has_video
    assert plan.chapters[1].pictures == []
    assert plan.left_out == {"pictures_cap": 2, "chapters_cap": 0, "missing_media": 0}
    off = collect(g, ALL, {"pictures": False})
    assert all(ch.pictures == [] for ch in off.chapters) and off.left_out["pictures_cap"] == 0


def test_collect_book_wide_caps(g, monkeypatch):
    import primer.book.model as model
    monkeypatch.setattr(model, "MAX_PICTURES", 3)
    monkeypatch.setattr(model, "MAX_CHAPTERS", 2)
    a, b, c = page(g, "A"), page(g, "B"), page(g, "C")
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1)
    edge(g, a, c, T0 + 2)
    for i in range(2):
        image(g, a, i)
        image(g, b, 10 + i)
    plan = collect(g, ALL, {})
    assert [ch.title for ch in plan.chapters] == ["A", "B"]
    assert [len(ch.pictures) for ch in plan.chapters] == [2, 1]
    assert plan.left_out == {"pictures_cap": 1, "chapters_cap": 1, "missing_media": 0}


def test_collect_lenses_and_stories(g):
    src = [Source(1, "Trebuchet", "reference", "", "wikipedia", "A/Trebuchet",
                  "http://172.31.0.5:8080/content/wikipedia/A/Trebuchet", "Wikipedia"),
           Source(2, "Siege <engines>", "web", "", "", "", "https://example.org/siege", "example.org")]
    a = page(g, "Trebuchet", text="# Trebuchet\nIt throws [1] and [2] and [3].", sources=src)
    other = page(g, "Elsewhere")
    edge(g, None, a, T0)
    v = g.latest_version(a.id)
    # maps: a done map with an unlocated place; a failed map; a done map with nothing located
    m1 = g.add_map(a.id, v.n, "the castles")
    g.set_map_places(m1, [Place("Harlech", "a castle", 52.86, -4.11), Place("Camelot", "a legend"),
                          Place("Conwy", "", 53.28, -3.83)])
    g.update_map(m1, status="done")
    m2 = g.add_map(a.id, v.n, "failed")
    g.set_map_places(m2, [Place("X", "", 1.0, 2.0)])
    g.update_map(m2, status="failed")
    m3 = g.add_map(a.id, v.n, "nowhere")
    g.set_map_places(m3, [Place("Camelot")])
    g.update_map(m3, status="done")
    # timelines: an older done one, the latest done one, and a newer failed one
    ev = {"date_text": "1283", "year": 1283, "month": None, "day": None, "end_year": None,
          "precision": "year", "approx": False}
    t1 = g.add_timeline(a.id, v.n)
    g.set_timeline_events(t1, [{**ev, "label": "old"}])
    g.update_timeline(t1, status="done")
    time.sleep(0.01)
    t2 = g.add_timeline(a.id, v.n)
    g.set_timeline_events(t2, [{**ev, "label": "new"}, {**ev, "label": "newer", "year": 1290}])
    g.update_timeline(t2, status="done")
    time.sleep(0.01)
    t3 = g.add_timeline(a.id, v.n)
    g.update_timeline(t3, status="failed")
    # diagrams: an older done one and the latest done one (with a script to clean away)
    d1 = g.add_diagram(a.id, v.n, "")
    g.update_diagram(d1, status="done", svg=DRAWING, caption="old")
    time.sleep(0.01)
    d2 = g.add_diagram(a.id, v.n, "")
    g.update_diagram(d2, status="done", svg=DRAWING, caption="How it <throws>")
    # stories: a done one with a pictured scene, a failed one, and one about a page not in the book
    pic = image(g, a, 7, phrase="the arm")
    s1 = g.add_story(a.id, v.n, "story", "female")
    g.set_scenes(s1, [{"title": "One", "text": "She pulls."}, {"title": "Two", "text": "He lets go."}])
    g.update_scene(s1, 0, media_id=pic.id)
    g.update_scene(s1, 1, media_id="missing")
    g.update_story(s1, status="done", title="The throw")
    s2 = g.add_story(a.id, v.n, "listen", "male")
    g.update_story(s2, status="failed")
    s3 = g.add_story(other.id, 1, "story", "male")
    g.update_story(s3, status="done", title="Not in the book")

    plan = collect(g, {"kind": "branch", "node_id": a.id}, {})
    (ch,) = plan.chapters
    assert ch.sources == [
        {"n": 1, "title": "Trebuchet", "kind": "Wikipedia", "url": ""},  # local Kiwix: text only
        {"n": 2, "title": "Siege <engines>", "kind": "web", "url": "https://example.org/siege"}]
    assert ch.html.count("<sup>") == 2 and "[3]" in ch.html
    assert [mp.map_id for mp in ch.maps] == [m1]
    assert ch.maps[0].phrase == "the castles"
    assert ch.maps[0].places == [
        {"n": 1, "name": "Harlech", "what": "a castle", "lat": 52.86, "lon": -4.11},
        {"n": 2, "name": "Conwy", "what": "", "lat": 53.28, "lon": -3.83}]
    assert [e["label"] for e in ch.timeline] == ["new", "newer"]
    assert ch.diagram["caption"] == "How it <throws>"
    assert "<script" not in ch.diagram["svg"] and ch.diagram["svg"].startswith("<svg")
    assert 'id="dg1-arm"' in ch.diagram["svg"]  # the chapter's number: unique in the book
    assert [p.media_id for p in ch.pictures] == [pic.id]
    (story,) = plan.stories
    assert story.title == "The throw"
    assert [s["title"] for s in story.scenes] == ["One", "Two"]
    assert story.scenes[0]["text"] == "She pulls."
    assert story.scenes[0]["picture"] == Picture(pic.id, pic.file, "the arm")
    assert story.scenes[1]["picture"] is None

    off = collect(g, {"kind": "branch", "node_id": a.id},
                  {"maps": False, "timelines": False, "diagrams": False, "stories": False,
                   "pictures": False})
    (ch,) = off.chapters
    assert ch.maps == [] and ch.timeline is None and ch.diagram is None and off.stories == []
    # a diagram that no longer cleans is left out
    with g._db:
        g._db.execute("UPDATE diagrams SET svg=? WHERE id=?", ("<svg><text>One</text></svg>", d2))
    assert collect(g, {"kind": "branch", "node_id": a.id}, {}).chapters[0].diagram is None


def test_collect_missing_media_counted(g, tmp_path):
    a = page(g, "A")
    edge(g, None, a, T0)
    media = tmp_path / "media"
    media.mkdir()
    there = image(g, a, 1)
    (media / there.file).write_bytes(b"\x89PNG" + b"x" * 100)
    image(g, a, 2)  # no file on disk
    image(g, a, 3, file="../../etc/passwd.png")  # not a media name
    image(g, a, 4, file=f"{0xABC:064X}.png")  # upper-case hex is not a media name either
    plan = collect(g, ALL, {}, media_dir=media)
    assert [p.media_id for p in plan.chapters[0].pictures] == [there.id]
    assert plan.left_out["missing_media"] == 3
    # without a media folder only the name is checked
    assert collect(g, ALL, {}).left_out["missing_media"] == 2


def test_estimate_and_default_title(g, tmp_path):
    a, b = page(g, "Castles"), page(g, "Moats")
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1)
    m = g.add_map(a.id, 1, "")
    g.set_map_places(m, [Place("Harlech", "", 52.86, -4.11)])
    g.update_map(m, status="done")
    media = tmp_path / "media"
    media.mkdir()
    for i in range(2):
        p = image(g, a, i)
        (media / p.file).write_bytes(b"x" * 1000)
    plan = collect(g, ALL, {}, media_dir=media)
    assert estimate(plan, media) == {"chapters": 2, "pictures": 2,
                                     "bytes": int(3000 * 1.37) + 2 * 4096 + 150_000}  # the cover repeats one
    assert default_title(g, ALL) == "My wanderings"
    assert default_title(g, {"kind": "branch", "node_id": b.id}) == "Moats"
    noon = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1))
    assert default_title(g, {"kind": "since", "since": noon}) == "Since 9 November 2026"
    assert collect(g, {"kind": "since", "since": noon}, {}).title == "Since 9 November 2026"
    with pytest.raises(ValueError):
        default_title(g, {"kind": "branch", "node_id": "nope"})
    for bad in (-1, 1e20, 4_102_444_801):
        with pytest.raises(ValueError):
            default_title(g, {"kind": "since", "since": bad})
    empty = collect(g, {"kind": "since", "since": T0 + 99_999}, {})
    assert empty.chapters == [] and empty.first_at is None and empty.last_at is None
    assert estimate(empty, media) == {"chapters": 0, "pictures": 0, "bytes": 0}


def test_collect_keeps_stored_source_numbers(g):
    src = [Source(1, "One", "web", "", url="https://example.org/1"),
           Source(3, "Three", "web", "", url="https://example.org/3")]
    a = page(g, "A", text="# A\nFirst [1], second [2], third [3].", sources=src)
    edge(g, None, a, T0)
    (ch,) = collect(g, ALL, {}).chapters
    assert [s["n"] for s in ch.sources] == [1, 3]
    assert '<sup><a href="#c1-src-3">3</a></sup>' in ch.html
    assert "#c1-src-2" not in ch.html and "[2]" in ch.html


def test_scene_pictures_count_towards_the_cap_once(g, monkeypatch):
    import primer.book.model as model
    monkeypatch.setattr(model, "MAX_PICTURES", 3)
    monkeypatch.setattr(model, "PICTURES_PER_CHAPTER", 2)
    a = page(g, "A")
    edge(g, None, a, T0)
    shared, other = image(g, a, 1, phrase="shared"), image(g, a, 2)
    extra = [image(g, a, 10), image(g, a, 11)]  # over the chapter's 2, so only scenes can use them
    sid = g.add_story(a.id, 1, "story", "female")
    g.set_scenes(sid, [{"title": "S1", "text": "x"}, {"title": "S2", "text": "y"},
                       {"title": "S3", "text": "z"}])
    g.update_scene(sid, 0, media_id=shared.id)
    g.update_scene(sid, 1, media_id=extra[0].id)
    g.update_scene(sid, 2, media_id=extra[1].id)
    g.update_story(sid, status="done", title="T")
    plan = collect(g, ALL, {})
    assert [p.media_id for p in plan.chapters[0].pictures] == [shared.id, other.id]
    pics = [sc["picture"] for sc in plan.stories[0].scenes]
    # the shared picture is reused without counting again, so one more fits the book's 3
    assert pics[0].media_id == shared.id and pics[1].media_id == extra[0].id and pics[2] is None
    # the chapter left 2 out (its own cap) and the scenes 1 (the book's cap)
    assert plan.left_out["pictures_cap"] == 3
    assert estimate(plan, Path("."))["pictures"] == 3


def test_light_collect_counts_what_the_full_one_does(g, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    a, b, c = page(g, "A"), page(g, "B"), page(g, "C")
    edge(g, None, a, T0)
    edge(g, a, b, T0 + 1)
    edge(g, b, c, T0 + 2)
    for k, node in enumerate((a, b, c)):
        for i in range(2):
            p = image(g, node, 10 * k + i, phrase=f"pic {k}{i}")
            (media / p.file).write_bytes(b"x" * (1000 + k))
    image(g, a, 99)  # no file: missing, in both
    for node in (a, c):
        m = g.add_map(node.id, 1, "places")
        g.set_map_places(m, [Place("Harlech", "", 52.86, -4.11)])
        g.update_map(m, status="done")
    empty = g.add_map(b.id, 1, "nowhere")
    g.set_map_places(empty, [Place("Camelot")])
    g.update_map(empty, status="done")
    for node in (a, b):
        d = g.add_diagram(node.id, 1, "")
        g.update_diagram(d, status="done", svg=DRAWING, caption="arm")
    extra = image(g, c, 77)
    (media / extra.file).write_bytes(b"y" * 5000)
    s = g.add_story(b.id, 1, "story", "male")
    g.set_scenes(s, [{"title": "S", "text": "x"}, {"title": "T", "text": "y"}])
    g.update_scene(s, 1, media_id=extra.id)
    g.update_story(s, status="done", title="Tale")
    for scope in (ALL, {"kind": "branch", "node_id": b.id}):
        for include in ({}, {"pictures": False}, {"maps": False, "stories": False}):
            full = collect(g, scope, include, media_dir=media)
            light = collect(g, scope, include, media_dir=media, light=True)
            assert estimate(light, media) == estimate(full, media), (scope, include)
            assert light.title == full.title and light.left_out == full.left_out
            assert [ch.node_id for ch in light.chapters] == [ch.node_id for ch in full.chapters]
            assert all(ch.html == "" and ch.sources == [] and ch.diagram is None
                       and ch.timeline is None for ch in light.chapters)
    full = collect(g, ALL, {}, media_dir=media)
    assert estimate(full, media)["pictures"] == 7
    # diagrams take the chapter's number as their prefix, so two never share ids
    assert 'id="dg1-arm"' in full.chapters[0].diagram["svg"]
    assert 'id="dg2-arm"' in full.chapters[1].diagram["svg"]
