from primer.maps import Place
from primer.maps.places import parse_places, place_messages

PAGE = "Warwick Castle stands on the River Avon. Kenilworth Castle is nearby, in Warwickshire."


def test_parse_places_snaps_and_dedupes():
    raw = ('```json\n[{"name":"warwick castle","what":"castle in Warwickshire"},'
           '{"name":"Warwick Castle","what":"River Avon"},'
           '{"name":"river avon","what":"river"},{"name":"Atlantis","what":"lost island"}]\n```')
    got = parse_places(raw, PAGE)
    assert [p.name for p in got] == ["Warwick Castle", "River Avon", "Atlantis"]
    assert got[0] == Place(name="Warwick Castle", what="castle in Warwickshire")
    assert got[0].lat is None


def test_parse_places_caps_at_twelve():
    raw = "[" + ",".join(f'{{"name":"Town {i}","what":"town"}}' for i in range(20)) + "]"
    assert len(parse_places(raw, "")) == 12


def test_parse_places_drops_non_places_shape():
    raw = ('[{"what":"no name"}, "Paris", 7, null, {"name":"' + "x" * 81 + '"},'
           '{"name":"  ","what":"blank"}, {"name":"Rome","what":5}]')
    got = parse_places(raw, "")
    assert [p.name for p in got] == ["Rome"]
    assert got[0].what == ""
    assert parse_places("no json here", PAGE) == []
    assert parse_places('{"name":"Rome"}', PAGE) == []


def test_place_messages_says_no_coordinates():
    msgs = place_messages("Warwick Castle", PAGE)
    assert [m["role"] for m in msgs] == ["system", "user"]
    system = msgs[0]["content"].lower()
    assert "coordinates" in system and "json array" in system
    assert "once" in system and "castle in warwickshire" in system
    assert "people" in system and "artworks" in system
    assert "Warwick Castle" in msgs[1]["content"] and PAGE in msgs[1]["content"]


def test_parse_places_ignores_model_coordinates():
    got = parse_places('[{"name":"Rome","what":"city","lat":41.9,"lon":12.5}]', "")
    assert got[0].lat is None and got[0].lon is None


def test_phrase_place_messages_and_parse():
    from primer.maps.places import parse_phrase_what, phrase_place_messages
    msgs = phrase_place_messages("Warwick", "Warwick stands on the River Avon.", "River Avon")
    assert msgs[0]["role"] == "system" and "Place: River Avon" in msgs[1]["content"]
    assert "Warwick stands on the River Avon." in msgs[1]["content"]
    assert parse_phrase_what('{"name": "River Avon", "what": "river in  Warwickshire"}') == "river in Warwickshire"
    assert parse_phrase_what("nonsense") == "" and parse_phrase_what('{"what": 3}') == ""
