import pytest

from primer.images.families import FAMILIES
from primer.images.prompting import (image_prompt_messages, match_image_request,
                                     parse_image_prompt)

YES = {
    "generate an image of a trebuchet at dawn": "a trebuchet at dawn",
    "Create a picture of the castle keep": "the castle keep",
    "make an illustration of siege towers": "siege towers",
    "draw a counterweight": "a counterweight",
    "paint the great hall at night": "the great hall at night",
    "show me an image of a portcullis": "a portcullis",
    "picture of a moat": "a moat",
    "render a drawbridge, watercolour": "a drawbridge, watercolour",
    "can you draw me a catapult?": "a catapult",
    "  DRAW me a Castle Gate!!  ": "a Castle Gate",
    "please paint a harbour.": "a harbour",
    "draw a picture of a dragon": "a dragon",
}
NO = ["the image of Dorian Gray", "image processing", "how do painters draw perspective",
      "picture perfect", "drawbridge", "draw", "generate an image of", "Medieval castle",
      # realistic false positives
      "draw conclusions about castles", "draw a conclusion from the siege",
      "draw attention to the moat", "draw on the history of the keep",
      "painting techniques of the renaissance", "paint", "draw me", "draw a picture of",
      "render unto caesar", "the picture of health", "make a moat"]


@pytest.mark.parametrize("text,subject", YES.items())
def test_match_yes(text, subject):
    assert match_image_request(text) == subject


@pytest.mark.parametrize("text", NO)
def test_match_no(text):
    assert match_image_request(text) is None


def test_match_image_request_cases():
    assert {t: match_image_request(t) for t in YES} == YES
    assert all(match_image_request(t) is None for t in NO)


def test_image_prompt_messages_style_by_family():
    tags = image_prompt_messages(FAMILIES["sdxl"], "a moat", None, None)
    prose = image_prompt_messages(FAMILIES["qwen-image-2.1"], "a moat", None, None)
    assert "comma-separated visual tags" in tags[0]["content"]
    assert "at most 60 words" in tags[0]["content"]
    assert "one or two vivid descriptive sentences" in prose[0]["content"]
    assert "at most 80 words" in prose[0]["content"]
    assert image_prompt_messages(FAMILIES["moss-image"], "x", None, None)[0] == prose[0]
    assert "illustrated primer" in tags[0]["content"]
    assert [m["role"] for m in tags] == ["system", "user"]
    assert "a moat" in tags[1]["content"] and "Page" not in tags[1]["content"]
    rich = image_prompt_messages(FAMILIES["sdxl"], "a moat", "Castles", "x" * 1000)
    user = rich[1]["content"]
    assert "Castles" in user and "x" * 600 in user and "x" * 601 not in user


def test_parse_image_prompt_cleans_and_falls_back():
    assert parse_image_prompt('  "a moat, ink"  ', "fb") == "a moat, ink"
    assert parse_image_prompt("```\nPrompt: a moat, ink\n```", "fb") == "a moat, ink"
    assert parse_image_prompt("```text\nprompt : 'a moat'\n```", "fb") == "a moat"
    assert parse_image_prompt("a" * 700, "fb") == "a" * 600
    assert parse_image_prompt("", "fb") == "fb"
    assert parse_image_prompt("```\n```", "fb") == "fb"
    assert parse_image_prompt('""', "fb") == "fb"
    assert parse_image_prompt("Prompt:", "fb") == "fb"


# fix round 1: topic prompts that only look like requests must open a normal page
NO_TOPICS = [
    "paint a picture of medieval life in words", "render a verdict on the siege",
    "render lard from pork fat", "draw water from the well", "draw the curtains",
    "paint is made from pigments", "picture of health", "an image of power in medieval politics",
    "a picture of the world in 1300 is hard to build", "draw me into the story",
    "paint the town red", "draw a bath", "draw the sword", "draw a blank",
    "draw inspiration from gothic architecture", "draw a card", "draw a crowd",
    "draw interest from investors", "render assistance to the wounded", "paint stripper recipe",
    "render the trebuchet in blender", "a drawing of the plan is attached",
    "draw straws", "draw lots to pick the garrison", "draw a comparison between motte and bailey",
    "draw a line between keep and bailey", "draw back the bolt", "draw upon the sources",
]
YES_MORE = {
    "could you sketch a moat": "a moat",
    "sketch a castle": "a castle",
    "sketch me a castle": "a castle",
    "illustrate a trebuchet": "a trebuchet",
    "illustrate the great hall": "the great hall",
    "show me what a trebuchet looks like": "a trebuchet",
    "I'd like a picture of a moat": "a moat",
    "I want an image of a moat": "a moat",
    "give me a picture of a moat": "a moat",
    "give me an image of a trebuchet": "a trebuchet",
    "create an image: a moat": "a moat",
    "draw: a moat": "a moat",
    "hey, draw a moat": "a moat",
    "ok draw a moat": "a moat",
    "draw a dragon please": "a dragon",
    "draw me a dragon, please": "a dragon",
    "draw a moat for me": "a moat",
    "draw a picture for me of a moat": "a moat",
    "draw three owls": "three owls",
    "draw 2 knights": "2 knights",
}


@pytest.mark.parametrize("text", NO_TOPICS)
def test_topics_that_look_like_requests_are_not_matched(text):
    assert match_image_request(text) is None


@pytest.mark.parametrize("text,subject", YES_MORE.items())
def test_wider_genuine_requests(text, subject):
    assert match_image_request(text) == subject


def test_subject_first_line_only_and_capped():
    assert match_image_request("draw a moat" + chr(10) + "and then a bridge" + chr(10) + "please") == "a moat"
    assert len(match_image_request("draw a " + "x" * 1000)) == 300


def test_parse_image_prompt_article_shaped_answers():
    article = "# Trebuchet\n\n**Trebuchet** sits at *dawn*, ink.\n\n## History\n\nIt was used in sieges."
    assert parse_image_prompt(article, "fb") == "Trebuchet"
    body = "## Scene\nA **trebuchet** at dawn, *misty*.\n\nMore prose here."
    assert parse_image_prompt(body, "fb") == "Scene\nA trebuchet at dawn, misty."
    assert parse_image_prompt("a **moat**, _ink_", "fb") == "a moat, ink"
    assert parse_image_prompt("# \n\n", "fb") == "fb"
