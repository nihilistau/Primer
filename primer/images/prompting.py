"""Typed image requests ("draw a moat") and the fast model's image-prompt messages."""
from __future__ import annotations

import re

from .families import Family

_NOUN = r"(?:image|picture|illustration|drawing|painting)"
_ART = r"(?:an?\s+|the\s+)?"
_SEP = r"(?:\s*[:,]\s*|\s+)"  # one pass, so long whitespace runs stay linear
_VERB_OF = r"(?:generate|create|make|draw|paint|render|show|sketch|illustrate|give)"
_HEY = r"(?:(?:hey|ok|okay)[,\s]+)?"
_POLITE = r"(?:(?:(?:can|could|would)\s+you|please)\s+)?"
_OF = r"(?:\s+for\s+me)?(?:\s+of\b\s*:?|\s*:)\s*"
# Anchored at the start. Groups: a = "<verb> [me] [a] <noun> of X", b = "<draw|paint|render|sketch|
# illustrate> X" (X must be article- or number-led), c = bare "[a] image/picture/illustration of X" (no
# "the": "the image of Dorian Gray" is a topic), d = "give me / I want / I'd like [a] <noun> of X",
# e = "show me what X looks like".
_REQUEST = re.compile(
    rf"^{_HEY}(?:"
    rf"{_POLITE}{_VERB_OF}(?:\s+me)?{_SEP}{_ART}{_NOUN}{_OF}(?P<a>.+)"
    rf"|{_POLITE}(?:draw|paint|render|sketch|illustrate){_SEP}(?P<b>.+)"
    rf"|(?:an?\s+)?(?:image|picture|illustration){_OF}(?P<c>.+)"
    rf"|{_POLITE}(?:give\s+me|i['’]d\s+like|i\s+would\s+like|i\s+want)\s+(?:an?\s+)?{_NOUN}{_OF}(?P<d>.+)"
    rf"|{_POLITE}show\s+me\s+what\s+(?P<e>(?:an?\s+|the\s+)?.+?)\s+looks?\s+like"
    rf")$",
    re.IGNORECASE)
_NUMBER = (r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|some|several|many|few|both|"
           r"a\s+couple\s+of|a\s+pair\s+of")
_ARTICLE_LED = re.compile(rf"^(?:an?|the|{_NUMBER})\s", re.IGNORECASE)
_BARE_NOUN_OF = re.compile(rf"^{_ART}{_NOUN}\s+of$", re.IGNORECASE)
# "draw a card", "draw the curtains", "render a verdict": idioms, not pictures
_IDIOM = re.compile(
    r"^(?:(?:an?|the|some)\s+)?(?:"
    r"concept|idea|point|principle|importance|difference|conclusions?|attention|comparisons?|parallels?|inferences?|distinctions?|analog(?:y|ies)|"
    r"lessons?|blood|breath|blank|bath|card|crowd|verdict|judg(?:e)?ment|decision|"
    r"curtains?|blinds?|sword|bow|gun|town\s+red|short\s+straw|line\b)",
    re.IGNORECASE)
# "a picture of X in words" is a figure of speech; "render X in blender" is a how-to
_NOT_PICTURE = re.compile(
    r"\bin\s+words$|\bin\s+(?:blender|unity|unreal(?:\s+engine)?|maya|3ds\s+max|cinema\s*4d)$",
    re.IGNORECASE)
# bare "picture of X": "picture of health", "an image of power...", "...is hard to build" are statements
_BARE_TOPIC = re.compile(r"^(?:health|power)\b|\b(?:is|are|was|were)\b", re.IGNORECASE)
_TAIL = re.compile(r"(?:,?\s+please|\s+for\s+me)$", re.IGNORECASE)
_TRIM = " \t\r\n?.!"
_MAX_SUBJECT = 300


def match_image_request(text: str) -> str | None:
    """The subject of a typed image request, or None when the text isn't one."""
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    m = _REQUEST.match(lines[0][:2000].strip().rstrip(_TRIM))
    if not m:
        return None
    kind = m.lastgroup
    subject = m.group(kind).strip()
    if kind == "b":
        subject = re.sub(r"^me\s+", "", subject, flags=re.IGNORECASE)
        if (not _ARTICLE_LED.match(subject) or _IDIOM.match(subject)
                or _BARE_NOUN_OF.match(subject)):
            return None
    elif kind == "c" and _BARE_TOPIC.search(subject):
        return None
    for _ in range(2):  # "a dragon for me, please"
        subject = _TAIL.sub("", subject).rstrip(_TRIM)
    if not subject or subject.casefold() == "me" or _NOT_PICTURE.search(subject):
        return None
    return subject[:_MAX_SUBJECT].strip()


_VNOUN = r"(?:(?:short|quick|little|brief)\s+)?(?:video|clip|animation|film)"
_VVERB_OF = r"(?:generate|create|make|render|show|give|produce)"
# Groups: a = "<verb> [me] [a] <video noun> of X", b = "animate|film X" (X must be article- or number-led),
# d = "give me / I want / I'd like [a] <video noun> of X".
_VIDEO_REQUEST = re.compile(
    rf"^{_HEY}(?:"
    rf"{_POLITE}{_VVERB_OF}(?:\s+me)?{_SEP}{_ART}{_VNOUN}{_OF}(?P<a>.+)"
    rf"|{_POLITE}(?:animate|film){_SEP}(?P<b>.+)"
    rf"|{_POLITE}(?:give\s+me|i['’]d\s+like|i\s+would\s+like|i\s+want)\s+(?:an?\s+)?{_VNOUN}{_OF}(?P<d>.+)"
    rf")$",
    re.IGNORECASE)
# "animate X in powerpoint" / "film X with css" are how-tos, not requests
_NOT_VIDEO = re.compile(
    r"\b(?:in|with|using|on)\s+(?:power\s*point|keynote|after\s+effects|blender|unity|unreal(?:\s+engine)?|"
    r"maya|premiere|css|javascript|js|figma|canva|html|react|powerpoint)$|\bin\s+words$",
    re.IGNORECASE)


def match_video_request(text: str) -> str | None:
    """The subject of a typed video request, or None when the text isn't one."""
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    m = _VIDEO_REQUEST.match(lines[0][:2000].strip().rstrip(_TRIM))
    if not m:
        return None
    kind = m.lastgroup
    subject = m.group(kind).strip()
    if kind == "b":
        subject = re.sub(r"^me\s+", "", subject, flags=re.IGNORECASE)
        if not _ARTICLE_LED.match(subject):
            return None
    for _ in range(2):
        subject = _TAIL.sub("", subject).rstrip(_TRIM)
    if not subject or subject.casefold() == "me" or _NOT_VIDEO.search(subject):
        return None
    return subject[:_MAX_SUBJECT].strip()


_STYLE = {
    "tags": ("Write one Stable Diffusion prompt: comma-separated visual tags "
             "(subject, setting, style, lighting), at most 60 words. Output only the prompt."),
    "prose": ("Write one image prompt as one or two vivid descriptive sentences "
              "(subject, setting, composition, style), at most 80 words. Output only the prompt."),
    "motion": ("Write one short-video prompt as one or two sentences describing the subject, its "
               "action and motion, and the camera movement, at most 80 words. "
               "Output only the prompt."),
}
_HOUSE = ("Unless the subject asks for another style, make it look like an illustrated primer / "
          "encyclopedia plate.")
# a scene of a story (a Tale's chapter): the scene is the subject, drawn as a storybook picture
_SCENE_HOUSE = ("The subject is a scene from an illustrated children's storybook: make it a "
                "storybook illustration of that moment, the characters in action in its setting, "
                "warm and painterly; not a diagram, a plate or a textbook figure.")
_CAP = 600


def image_prompt_messages(family: Family, subject: str, page_title: str | None,
                          excerpt: str | None, scene: bool = False) -> list[dict]:
    """The prompt writer's messages. A story `scene` gets the storybook style instead of the
    primer plate (callers pass no page title or excerpt for one: the scene is the subject)."""
    system = f"{_STYLE[family.prompt_style]} {_SCENE_HOUSE if scene else _HOUSE}"
    user = f"Subject: {subject}"
    if page_title:
        user += f"\nPage: {page_title}"
    if excerpt:
        user += f"\nExcerpt from the page:\n{excerpt[:_CAP]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_FENCE = re.compile(r"```[A-Za-z0-9_-]*")
# a leading label: "Prompt:", "Image prompt:", "Stable Diffusion prompt:", "Here is the prompt:",
# "Tags:" (the colon is required, so a prompt that starts with the word "prompt" is kept)
_PREFIX = re.compile(
    r"^\s*(?:here(?:'s|\s+is)\s+(?:the|an?|your)\s+)?(?:(?:final|improved|stable\s+diffusion|sd|"
    r"image|picture|illustration|video|short[- ]video)\s+)*(?:prompt|tags)\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'`“”‘’"
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])\*(?!\s)(.+?)\*(?![\w*])|(?<!\w)_(?!\s)(.+?)_(?!\w)", re.DOTALL)


def parse_image_prompt(raw: str, fallback: str) -> str:
    """Strip fences, a "Prompt:" label, quotes, Markdown headings and emphasis; an article-shaped
    answer (several paragraphs) keeps its first paragraph; cap at 600 characters; junk falls back."""
    text = _FENCE.sub("", raw).strip()
    text = _HEADING.sub("", text)
    text = _EMPHASIS.sub(lambda m: m.group(2) or m.group(3) or m.group(4), text).strip()
    for _ in range(3):  # a label inside quotes, or quotes after a label
        text = _PREFIX.sub("", text).strip().strip(_QUOTES).strip()
    text = re.split(r"\n\s*\n", text, maxsplit=1)[0].strip()
    return text[:_CAP].strip() or fallback
