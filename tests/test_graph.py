import pytest

from primer.graph import Card, Graph, clean_typed, normalize_concept


def test_normalize_concept_variants():
    assert {normalize_concept(s) for s in ["Trebuchet", " trebuchet ", "The trebuchet.", "“trebuchet”"]} == {"trebuchet"}


def test_normalize_concept_edges():
    assert normalize_concept("") == "" and normalize_concept("  ") == ""
    assert normalize_concept("An  Inclined   Plane") == "inclined plane"
    assert normalize_concept("a lever") == "lever"
    assert normalize_concept("another") == "another"


def test_typed_input_limits():
    assert clean_typed("   ") is None and len(clean_typed("x" * 500)) == 200
    assert clean_typed("  hi ") == "hi"


def test_general_nodes_shared_context_nodes_per_parent(g):
    t = g.node_for("Trebuchet", "concept"); lift = g.node_for("Lift", "concept")
    assert g.node_for("counterweight", "concept").id == g.node_for("Counterweight", "concept").id
    assert g.node_for("counterweight", "context", t.id).id != g.node_for("counterweight", "context", lift.id).id


def test_node_fields_and_first_spelling(g):
    n = g.node_for("Counterweight", "concept")
    again = g.node_for("the counterweight", "concept")
    assert again == n and n.title == "Counterweight" and len(n.id) == 16
    assert g.get_node(n.id) == n and g.get_node("nope") is None
    c = g.node_for("Counterweight", "context", n.id, title="Counterweight, in a trebuchet")
    assert c.kind == "context" and c.parent_id == n.id and c.title == "Counterweight, in a trebuchet"


def test_versions_increment_and_latest(g):
    n = g.node_for("Trebuchet", "concept")
    assert g.latest_version(n.id) is None
    g.add_version(n.id, "a", "storyteller", "normal"); v2 = g.add_version(n.id, "b", "storyteller", "deeper")
    assert v2.n == 2 and g.latest_version(n.id).markdown == "b" and len(g.versions(n.id)) == 2
    assert [v.n for v in g.versions(n.id)] == [1, 2] and g.versions(n.id)[1].depth == "deeper"


def test_edges_snapshot_and_recent(g):
    t = g.node_for("Trebuchet", "concept"); c = g.node_for("Counterweight", "concept")
    g.add_edge(None, t.id, "", "start"); g.add_edge(t.id, c.id, "counterweight", "general")
    snap = g.snapshot()
    assert {e["via"] for e in snap["edges"]} == {"start", "general"} and len(snap["nodes"]) == 2
    assert g.recent_titles()[0] == "Counterweight"


def test_snapshot_shape_and_order(g):
    a = g.node_for("Alpha", "concept"); b = g.node_for("Beta", "concept"); c = g.node_for("Gamma", "concept")
    g.add_edge(None, a.id, "", "start"); g.add_edge(a.id, c.id, "gamma", "general"); g.add_edge(a.id, b.id, "beta", "sideways")
    snap = g.snapshot()
    assert set(snap) == {"nodes", "edges"}
    assert [set(n) for n in snap["nodes"]] == [{"id", "title", "kind", "concept", "parent_id"}] * 3
    assert [n["title"] for n in snap["nodes"]] == ["Alpha", "Beta", "Gamma"]
    assert all(set(e) == {"from_id", "to_id", "phrase", "via", "at"} for e in snap["edges"])
    assert [e["via"] for e in snap["edges"]] == ["start", "general", "sideways"]
    assert snap["edges"][0]["from_id"] is None and isinstance(snap["edges"][0]["at"], float)


def test_recent_titles_order_unique_and_limit(g):
    a = g.node_for("Alpha", "concept"); b = g.node_for("Beta", "concept"); c = g.node_for("Gamma", "concept")
    g.add_edge(None, a.id, "", "start"); g.add_edge(a.id, b.id, "b", "general"); g.add_edge(b.id, c.id, "c", "general")
    g.add_edge(c.id, a.id, "a", "general")  # revisit Alpha: now most recent
    assert g.recent_titles() == ["Alpha", "Gamma", "Beta"]
    assert g.recent_titles(2) == ["Alpha", "Gamma"]
    d = g.node_for("Delta", "concept")  # never visited: not listed
    assert "Delta" not in g.recent_titles()


def test_cards_and_highlights_roundtrip(g):
    n = g.node_for("Trebuchet", "concept"); g.add_version(n.id, "x", "storyteller", "normal")
    g.set_highlights(n.id, 1, ["counterweight"]); assert g.get_highlights(n.id, 1) == ["counterweight"]
    cards = [Card("general", "counterweight", "Counterweight", "The principle")]
    g.set_cards(n.id, "Counterweight", cards); assert g.get_cards(n.id, "counterweight") == cards


def test_missing_highlights_and_cards_are_none(g):
    n = g.node_for("Trebuchet", "concept")
    assert g.get_highlights(n.id, 1) is None and g.get_cards(n.id, "x") is None
    g.set_highlights(n.id, 1, [])
    assert g.get_highlights(n.id, 1) == []
    g.set_highlights(n.id, 1, ["a", "b"])
    assert g.get_highlights(n.id, 1) == ["a", "b"]


def test_persists_across_reopen(tmp_path):
    Graph(tmp_path / "p.sqlite").node_for("Castle", "concept")
    assert Graph(tmp_path / "p.sqlite").snapshot()["nodes"][0]["title"] == "Castle"


def test_normalize_keeps_symbols():
    assert normalize_concept("C++") == "c++" and normalize_concept("C++") != normalize_concept("C")
    assert normalize_concept("C#") == "c#" and normalize_concept("F#") == "f#"
    assert normalize_concept(".NET") == ".net" and normalize_concept("A*") == "a*"
    assert normalize_concept("(The trebuchet?)") == "trebuchet"


def test_normalize_unicode_forms_equal():
    assert normalize_concept("café") == normalize_concept("café")


def test_node_for_validates(g):
    import pytest
    for bad in ("!!!", "   ", ""):
        with pytest.raises(ValueError):
            g.node_for(bad, "concept")
    with pytest.raises(ValueError):
        g.node_for("x", "bogus")
    with pytest.raises(ValueError):
        g.node_for("x", "context")
    t = g.node_for("Trebuchet", "concept")
    assert g.node_for("C++", "concept").id != g.node_for("C", "concept").id
    assert g.node_for("Lift", "concept", t.id).parent_id is None
    assert g.node_for("Lift", "concept").id == g.node_for("Lift", "concept", t.id).id


def test_media_roundtrip_and_order(g):
    n = g.node_for("Trebuchet", "concept")
    a = g.add_media(n.id, "counterweight", "image", "sd15", "a drawing", {"steps": 20, "seed": 7})
    b = g.add_media(n.id, "sling", "image", "sd15", "another", {})
    assert a.status == "prompting" and len(a.id) == 32 and a.file is None and a.error is None
    assert a.settings == {"steps": 20, "seed": 7} and a.node_id == n.id and a.phrase == "counterweight"
    assert g.get_media(a.id) == a and g.get_media("nope") is None
    assert [m.id for m in g.media_for_node(n.id)] == [a.id, b.id]
    assert g.media_for_node("other") == []
    u = g.update_media(a.id, status="done", file="x.png", prompt="p2", settings={"steps": 5})
    assert (u.status, u.file, u.prompt, u.settings) == ("done", "x.png", "p2", {"steps": 5})
    assert g.get_media(a.id) == u and u.created_at == a.created_at
    assert g.update_media(a.id, error="boom").error == "boom"


def test_update_media_rejects_unknown_field_and_status(g):
    n = g.node_for("Trebuchet", "concept")
    m = g.add_media(n.id, "p", "image", "sd15", "x", {})
    with pytest.raises(ValueError):
        g.update_media(m.id, node_id="other")
    with pytest.raises(ValueError):
        g.update_media(m.id, status="bogus")
    with pytest.raises(ValueError):
        g.update_media("nope", status="done")
    assert g.get_media(m.id).status == "prompting"


def test_fail_unfinished_media_on_start(g):
    n = g.node_for("Trebuchet", "concept")
    ms = {s: g.add_media(n.id, s, "image", "sd15", "x", {}) for s in
          ("prompting", "queued", "generating", "done", "cancelled")}
    for s in ("queued", "generating", "done", "cancelled"):
        g.update_media(ms[s].id, status=s)
    assert g.fail_unfinished_media("Primer restarted") == 3
    for s in ("prompting", "queued", "generating"):
        m = g.get_media(ms[s].id)
        assert m.status == "failed" and m.error == "Primer restarted"
    assert g.get_media(ms["done"].id).status == "done" and g.get_media(ms["cancelled"].id).status == "cancelled"
    assert g.fail_unfinished_media("again") == 0


def test_media_requires_existing_node(g):
    with pytest.raises(ValueError):
        g.add_media("nope", "p", "image", "sd15", "x", {})


def test_add_version_with_sources_roundtrip(g):
    from primer.grounding import Source
    node = g.node_for("Whale", "concept")
    srcs = [Source(1, "Whale", "reference", "full text", book="wikipedia_en", path="Whale", where="Wikipedia"),
            Source(2, "Whales", "web", "x", url="https://example.com/w", where="example.com")]
    v = g.add_version(node.id, "# Whale", "reference", "normal", srcs)
    got = g.sources(node.id, v.n)
    assert got == [Source(1, "Whale", "reference", "", "wikipedia_en", "Whale", "", "Wikipedia"),
                   Source(2, "Whales", "web", "", "", "", "https://example.com/w", "example.com")]
    v2 = g.add_version(node.id, "# Whale 2", "storyteller", "normal")
    assert g.sources(node.id, v2.n) == [] and len(g.sources(node.id, v.n)) == 2


def test_map_roundtrip(g):
    from primer.maps import Place
    n = g.node_for("Castles", "concept")
    mid = g.add_map(n.id, 1, "")
    assert len(mid) == 16 and g.get_map(mid)["status"] == "finding" and g.get_map(mid)["places"] == []
    g.set_map_places(mid, [Place("A", "x", 1.0, 2.0, "A", "web"), Place("B")])
    g.update_map(mid, status="done")
    m = g.get_map(mid)
    assert m["status"] == "done" and m["error"] is None
    assert [p["name"] for p in m["places"]] == ["A", "B"] and m["places"][1]["lat"] is None
    assert m["places"][0] == {"name": "A", "what": "x", "lat": 1.0, "lon": 2.0, "title": "A", "source": "web"}
    other = g.add_map(n.id, 1, "p")
    listed = g.maps_for(n.id)
    assert [x["id"] for x in listed] == [other, mid] and "places" not in listed[0]
    assert g.get_map("nope") is None
    with pytest.raises(ValueError):
        g.update_map(mid, status="weird")
    with pytest.raises(ValueError):
        g.update_map("nope", status="done")


def test_update_map_keeps_error_and_failed(g):
    n = g.node_for("Castles", "concept")
    mid = g.add_map(n.id, 1, "")
    g.update_map(mid, status="failed", error="why")
    g.update_map(mid, status="done")  # a late writer cannot revive a failed map
    assert g.get_map(mid)["status"] == "failed" and g.get_map(mid)["error"] == "why"
    m2 = g.add_map(n.id, 1, "")
    g.update_map(m2, error="note")
    g.update_map(m2, status="locating")  # no error given: the old one stays
    assert g.get_map(m2)["error"] == "note"
    g.update_map(m2, status="done", error=None)  # explicit None clears
    assert g.get_map(m2)["error"] is None


def test_story_roundtrip(g):
    n = g.node_for("Castles", "concept")
    sid = g.add_story(n.id, 1, "story", "female")
    assert len(sid) == 16 and g.get_story(sid)["status"] == "writing" and g.get_story(sid)["scenes"] == []
    g.set_scenes(sid, [{"title": "A", "text": "x", "spoken": "x"}, {"title": "B", "text": "y"}])
    g.update_scene(sid, 0, status="done", audio="a.wav", chunks=[[0, 1, 0.0, 0.5]], media_id="m1")
    g.update_story(sid, status="narrating", title="Castles", highlights=["x"])
    s = g.get_story(sid)
    assert s["title"] == "Castles" and s["highlights"] == ["x"] and s["voice"] == "female"
    assert s["scenes"][0]["chunks"] == [[0, 1, 0.0, 0.5]] and s["scenes"][0]["media_id"] == "m1"
    assert s["scenes"][1]["status"] == "waiting" and s["scenes"][1]["chunks"] == []
    assert g.get_story("nope") is None
    with pytest.raises(ValueError):
        g.add_story(n.id, 1, "weird", "v")
