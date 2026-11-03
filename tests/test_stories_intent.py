import pytest

from primer.stories.intent import match_story_request

YES = {
    "tell me a story about Kenilworth Castle": "Kenilworth Castle",
    "Tell me a story about the Roman Empire.": "the Roman Empire",
    "read me a story about dragons": "dragons",
    "a story about a brave knight": "a brave knight",
    "story of Rome": "Rome",
    "the story of the Battle of Hastings": "the Battle of Hastings",
    "tell me the story of Robin Hood": "Robin Hood",
    "please tell a story about whales": "whales",
    "can you tell me a story about Ely Cathedral": "Ely Cathedral",
    "  TELL ME A STORY ABOUT Normandy!!  ": "Normandy",
    "tell me a story about Coventry for me, please": "Coventry",
}
NO = ["what is the story of the Iliad about?", "what's the story of Rome", "how long is the story of Rome?",
      "story", "a story", "story of", "tell me a story", "tell me a story about", "tell me a story about it",
      "story of it", "the story is long", "this story about Rome is good", "short story collections",
      "is there a story about Rome", "who wrote the story of Rome", "history of Rome", "", "stories about Rome",
      "tell me a story about what is a dragon?"]


@pytest.mark.parametrize("text,subject", YES.items())
def test_match_yes(text, subject):
    assert match_story_request(text) == subject


@pytest.mark.parametrize("text", NO)
def test_match_no(text):
    assert match_story_request(text) is None


def test_match_story_request_cases():
    assert match_story_request("tell me a story about the Silk Road\nand more text") == "the Silk Road"
    assert len(match_story_request("story of " + "y" * 900)) <= 300
