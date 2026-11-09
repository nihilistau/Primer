from primer.tales.bible import Bible
from primer.tales.material import pick_material, waiting_count


def _page(g, title, body="Para one.\n\nPara two."):
    node = g.node_for(title, "concept")
    g.add_version(node.id, f"# {title}\n\n{body}", "model", "normal")
    return node.id


def _tale(g, at=100.0):
    return g.add_tale("a fox", "The Fox", Bible().to_json(), at=at)


def test_tale_helpers(g):
    a = _tale(g, 100.0)
    b = _tale(g, 200.0)
    assert g.active_tale()["id"] == b and g.get_tale(a)["status"] == "archived"
    assert [t["id"] for t in g.list_tales()] == [b, a]
    assert g.set_active_tale(a) and g.active_tale()["id"] == a and not g.set_active_tale("nope")
    g.set_tale_bible(a, '{"x": 1}', title="New")
    assert g.get_tale(a)["bible_json"] == '{"x": 1}' and g.get_tale(a)["title"] == "New"
    assert g.add_tale_chapter(a, "s1", ["p1", "p2"], "sum1", at=150.0) == 1
    assert g.add_tale_chapter(a, "s2", ["p3"], "sum2", at=160.0) == 2
    ch = g.tale_chapters(a)
    assert [c["n"] for c in ch] == [1, 2] and ch[0]["pages"] == ["p1", "p2"] and ch[0]["story_id"] == "s1"
    g.set_chapter_summary(a, 2, "better")
    assert g.tale_chapters(a)[1]["summary"] == "better"
    assert g.used_pages(a) == {"p1", "p2", "p3"} and g.used_pages(b) == set()


def test_material_ranking_and_skip_used(g, monkeypatch):
    monkeypatch.setattr("primer.tales.material.LOOK_BACK_S", 0)  # (the look-back has its own test)
    tid = _tale(g, 100.0)
    old = _page(g, "Old")
    g.add_reader_event(50.0, "open", old, "start")  # before the tale: not material
    long_, often, recent, plain, dup = (_page(g, t) for t in ("Long", "Often", "Recent", "Plain", "Extra"))
    g.add_reader_event(110.0, "open", long_, "start")
    g.add_dwell(long_, 90.0)
    for t in (120.0, 121.0):
        g.add_reader_event(t, "open", often, "context")
    g.add_dwell(often, 10.0)
    g.add_reader_event(130.0, "open", recent, "context")
    g.add_dwell(recent, 10.0)
    g.add_reader_event(105.0, "open", plain, "context")
    g.add_reader_event(106.0, "open", dup, "context")
    g.node_for("No version", "concept")  # opened but nothing stored
    g.add_reader_event(140.0, "open", g.node_id_for("No version", "concept"), "context")
    g.add_reader_event(141.0, "answer", often, "x")  # not an open

    got = pick_material(g, tid)
    assert [p["node_id"] for p in got] == [long_, often, recent]
    assert got[0]["seconds"] == 90.0 and got[1]["opens"] == 2 and got[0]["title"] == "Long"
    assert got[0]["excerpt"] == "Para one.\n\nPara two."  # the title line is gone
    assert waiting_count(g, tid) == 5
    assert [p["node_id"] for p in pick_material(g, tid, limit=1)] == [long_]

    # used by chapter 1: skipped; the next chapter counts opens only after chapter 1
    g.add_tale_chapter(tid, "s1", [long_, often, recent], "sum", at=200.0)
    assert pick_material(g, tid) == [] and waiting_count(g, tid) == 0
    g.add_reader_event(210.0, "open", long_, "context")  # reopened: still used
    g.add_reader_event(211.0, "open", plain, "context")
    assert [p["node_id"] for p in pick_material(g, tid)] == [plain]
    # the reader's picks are used as given (even used ones), unknown/unwritten dropped
    picked = pick_material(g, tid, chosen=[often, "ghost", g.node_id_for("No version", "concept"), often, old])
    assert [p["node_id"] for p in picked] == [often, old] and picked[0]["opens"] == 2


def test_next_needs_material(g):
    tid = _tale(g)
    assert pick_material(g, tid) == [] and waiting_count(g, tid) == 0
    assert pick_material(g, tid, chosen=[]) == []
    assert pick_material(g, tid, chosen=["ghost"]) == []


def test_material_falls_back_to_arrivals_without_signals(g):
    tid = g.add_tale("a fox", "The Fox", Bible().to_json(), at=1.0)  # long ago
    a, b = _page(g, "Alpha"), _page(g, "Beta")
    g.add_edge(None, a, "Alpha", "start")
    g.add_edge(a, b, "beta", "context")
    assert {p["node_id"] for p in pick_material(g, tid)} == {a, b}
    assert waiting_count(g, tid) == 2
    # once there are open signals, they decide
    g.add_reader_event(10**10, "open", a, "start")
    assert [p["node_id"] for p in pick_material(g, tid)] == [a]


def test_waiting_count(g):
    tid = _tale(g, 100.0)
    pages = [_page(g, f"P{i}") for i in range(5)]
    for i, p in enumerate(pages):
        g.add_reader_event(110.0 + i, "open", p, "context")
    assert waiting_count(g, tid) == 5 and len(pick_material(g, tid)) == 3


def test_excerpt_cut(g):
    tid = _tale(g, 100.0)
    body = "\n\n".join(("word " * 100).strip() for _ in range(10))
    p = _page(g, "Long page", body)
    g.add_reader_event(110.0, "open", p, "start")
    [m] = pick_material(g, tid)
    assert len(m["excerpt"]) <= 2500 and m["excerpt"].endswith("word") and not m["excerpt"].endswith("\n")
    assert m["excerpt"].count("\n\n") == 3  # cut at a paragraph boundary
    [m] = pick_material(g, tid, excerpt_chars=50)
    assert len(m["excerpt"]) <= 50 and m["excerpt"].startswith("word word")


def test_chapter_one_looks_back_a_day(g):
    """The reading that led to the tale counts for chapter 1 (still ranked, used pages skipped);
    later chapters count from the last chapter."""
    day = 24 * 3600
    tid = _tale(g, 10 * day)
    hour_before, long_before, after = _page(g, "Hour"), _page(g, "Long ago"), _page(g, "After")
    g.add_reader_event(10 * day - 3600, "open", hour_before, "start")
    g.add_dwell(hour_before, 5.0)
    g.add_reader_event(9 * day - 10, "open", long_before, "start")  # more than a day before
    g.add_reader_event(10 * day + 5, "open", after, "context")
    g.add_dwell(after, 50.0)
    assert [p["node_id"] for p in pick_material(g, tid)] == [after, hour_before]
    assert waiting_count(g, tid) == 2
    g.add_tale_chapter(tid, "s1", [after], "", at=10 * day + 10)
    assert pick_material(g, tid) == []  # the hour before is not after chapter 1


def test_chapter_pages_index(g, tmp_path):
    """Every page a chapter drew on lists the chapter's story; the index is filled in for
    chapters from before it, and the Tale's view reads titles and statuses in one go."""
    from primer.graph import Graph
    tid = _tale(g)
    a, b, c = _page(g, "A"), _page(g, "B"), _page(g, "C")
    sid = g.add_story(a, 1, "chapter", "")
    plain = g.add_story(b, 1, "story", "")
    g.add_tale_chapter(tid, sid, [a, b, "gone"], "")
    assert [s["id"] for s in g.stories_for(a)] == [sid]
    assert {s["id"] for s in g.stories_for(b)} == {sid, plain}
    assert g.stories_for(c) == []
    assert g.chapter_page_titles(tid) == {1: [{"node_id": a, "title": "A"}, {"node_id": b, "title": "B"},
                                              {"node_id": "gone", "title": ""}]}
    assert g.story_states([sid, "nope", sid]) == {sid: {"title": "", "status": "writing"}}
    assert g.story_states([]) == {}
    assert g.tale_chapter_counts() == {tid: 1}
    # told again: the pages follow the chapter's new story
    new = g.add_story(a, 1, "chapter", "")
    g.replace_chapter_story(sid, new)
    assert new in [s["id"] for s in g.stories_for(b)] and sid not in [s["id"] for s in g.stories_for(b)]

    # an older database: chapters without the index get it on open
    path = tmp_path / "old.sqlite"
    old = Graph(path)
    old_tid = old.add_tale("x", "X", Bible().to_json(), at=1.0)
    p1, p2 = _page(old, "P1"), _page(old, "P2")
    old_sid = old.add_story(p1, 1, "chapter", "")
    old.add_tale_chapter(old_tid, old_sid, [p1, p2], "")
    with old._lock, old._db:
        old._db.execute("DELETE FROM tale_chapter_pages")
    assert old.stories_for(p2) == []
    old._db.close()
    again = Graph(path)
    assert [s["id"] for s in again.stories_for(p2)] == [old_sid]
    assert [p["node_id"] for p in again.chapter_page_titles(old_tid)[1]] == [p1, p2]
