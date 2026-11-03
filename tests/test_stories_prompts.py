from primer.stories.prompts import parse_scenes, story_messages


def test_story_messages_shape():
    msgs = story_messages("Whales", "# Whales\n\nBig animals of the sea.")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "## " in msgs[0]["content"] and "60" in msgs[0]["content"] and "120" in msgs[0]["content"]
    assert "Whales" in msgs[1]["content"] and "Big animals of the sea." in msgs[1]["content"]


def test_parse_scenes_coerces():
    good = "## One\nAlpha beta.\n\n## Two\nGamma.\n\n## Three\nDelta."
    assert parse_scenes(good, "T") == [{"title": "One", "text": "Alpha beta."},
                                       {"title": "Two", "text": "Gamma."},
                                       {"title": "Three", "text": "Delta."}]
    # no headings: one scene named for the page
    assert parse_scenes("Just a tale.\nIt went on.", "Page") == [
        {"title": "Page", "text": "Just a tale.\nIt went on."}]
    # seven scenes: at most five, the extras merged into the last
    seven = "\n\n".join(f"## S{i}\nText {i}." for i in range(7))
    got = parse_scenes(seven, "T")
    assert len(got) == 5 and [s["title"] for s in got] == ["S0", "S1", "S2", "S3", "S4"]
    assert "Text 4." in got[4]["text"] and "Text 5." in got[4]["text"] and "Text 6." in got[4]["text"]
    # a preamble before the first heading is dropped; empty scenes are dropped
    assert parse_scenes("Sure! Here:\n## A\nx\n## Empty\n\n## B\ny", "T") == [
        {"title": "A", "text": "x"}, {"title": "B", "text": "y"}]
    # nothing usable
    assert parse_scenes("   ", "T") == []
    assert parse_scenes("## Only a heading", "T") == [{"title": "T", "text": "Only a heading"}]


def test_parse_scenes_heading_levels():
    assert parse_scenes("# One\nA.\n\n# Two\nB.", "T") == [{"title": "One", "text": "A."}, {"title": "Two", "text": "B."}]
    # a single # title above ## scenes is not a scene
    got = parse_scenes("# Story\n\n## One\nA.\n\n## Two\nB.", "T")
    assert [s["title"] for s in got] == ["One", "Two"]
