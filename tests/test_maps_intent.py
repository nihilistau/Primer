import pytest

from primer.maps.intent import match_map_request

YES = {
    "show me Warwick Castle on a map": "Warwick Castle",
    "Show Kenilworth Castle on the map": "Kenilworth Castle",
    "Warwick Castle on a map": "Warwick Castle",
    "map of Europe": "Europe",
    "a map of the Roman Empire": "the Roman Empire",
    "show me a map of medieval England": "medieval England",
    "put the Battle of Hastings on a map": "the Battle of Hastings",
    "can you put Rome on a map?": None,  # a question mark: left to the model
    "please show Paris on a map.": "Paris",
    "  PUT Ely Cathedral ON A MAP!!  ": "Ely Cathedral",
    "show me Coventry on a map for me, please": "Coventry",
    "give me a map of Normandy": "Normandy",
}
NO = ["what is a map projection?", "how do maps work", "mind map", "a mind map of my day",
      "map projection", "map", "map of", "show me on a map", "put it on a map",
      "where is Warwick on a map", "show me where Warwick is on a map",
      "the map of Europe is hard to read", "treasure map", "maps", "on a map", "",
      "what is the best map of Europe", "road map for learning python"]


@pytest.mark.parametrize("text,subject", YES.items())
def test_match_yes(text, subject):
    assert match_map_request(text) == subject


@pytest.mark.parametrize("text", NO)
def test_match_no(text):
    assert match_map_request(text) is None


def test_match_map_request_cases():
    assert match_map_request("map of the Silk Road\nand more text") == "the Silk Road"
    assert len(match_map_request("map of " + "y" * 900)) <= 300
