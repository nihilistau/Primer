"""Typed story requests ("tell me a story about Kenilworth Castle", "story of Rome")."""
from __future__ import annotations

import re

_HEY = r"(?:(?:hey|ok|okay)[,\s]+)?"
_POLITE = r"(?:(?:(?:can|could|would)\s+you|please)\s+)?"
_ART = r"(?:an?\s+|the\s+)"
_STORY = r"(?:short\s+)?story\s+(?:about|of)\s+"
# "tell/read [me] a story about X", "[a|the] story about|of X" (a bare "story of X" included).
_REQUEST = re.compile(
    rf"^{_HEY}{_POLITE}(?:(?:tell|read)(?:\s+me)?\s+(?:{_ART})?|(?:{_ART})?){_STORY}(?P<s>.+)$",
    re.IGNORECASE)
_QUESTION = re.compile(r"^(?:what|how|why|who|whom|whose|when|where|which|is|are|was|were|do|does|did|"
                       r"can|could|should|would|will|if|that|this|it|there)\b", re.IGNORECASE)
_TAIL = re.compile(r"(?:,?\s+please|\s+for\s+me)$", re.IGNORECASE)
_TRIM = " \t\r\n?.!"
_MAX_SUBJECT = 300


def match_story_request(text: str) -> str | None:
    """The subject of a typed story request, or None when the text isn't one."""
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    first = lines[0][:2000].strip()
    if first.endswith("?"):
        return None  # a question about a story, not a request
    for _ in range(2):  # "a story about Coventry for me, please"
        first = _TAIL.sub("", first.rstrip(_TRIM))
    m = _REQUEST.match(first.rstrip(_TRIM))
    if not m:
        return None
    subject = m.group("s").strip()
    if (not subject or subject.casefold() in ("me", "it", "them", "this", "that")
            or _QUESTION.match(subject) or re.search(r"\b(?:is|are|was|were)\b", subject, re.IGNORECASE)):
        return None
    return subject[:_MAX_SUBJECT].strip()
