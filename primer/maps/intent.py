"""Typed map requests ("show Warwick Castle on a map", "map of Europe")."""
from __future__ import annotations

import re

_HEY = r"(?:(?:hey|ok|okay)[,\s]+)?"
_POLITE = r"(?:(?:(?:can|could|would)\s+you|please)\s+)?"
_ART = r"(?:an?\s+|the\s+)?"
_ON_MAP = r"\s+on\s+(?:an?\s+|the\s+|my\s+)?(?:\w+\s+)?map"
_VERB = r"(?:show|put|plot|place|mark|pin|find|locate)"
_MAKE = r"(?:show|give|make|draw|create|generate)"
# Groups: a = "<verb> [me] X on a map", b = "[a] map of X" (also "show/make me a map of X"),
# c = bare "X on a map".
_REQUEST = re.compile(
    rf"^{_HEY}(?:"
    rf"{_POLITE}{_VERB}(?:\s+me)?\s+(?P<a>.+?){_ON_MAP}"
    rf"|{_POLITE}(?:{_MAKE}(?:\s+me)?\s+)?{_ART}map\s+of\s+(?P<b>.+)"
    rf"|(?P<c>.+?){_ON_MAP}"
    rf")$",
    re.IGNORECASE)
_QUESTION = re.compile(r"^(?:what|how|why|who|whom|whose|when|where|which|is|are|was|were|do|does|did|"
                       r"can|could|should|would|will|if|that|this|it|there)\b", re.IGNORECASE)
_TAIL = re.compile(r"(?:,?\s+please|\s+for\s+me)$", re.IGNORECASE)
_TRIM = " \t\r\n?.!"
_MAX_SUBJECT = 300


def match_map_request(text: str) -> str | None:
    """The place or topic of a typed map request, or None when the text isn't one."""
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    first = lines[0][:2000].strip()
    if first.endswith("?"):
        return None  # a question about maps, not a request
    for _ in range(2):  # "Warwick Castle on a map for me, please"
        first = _TAIL.sub("", first.rstrip(_TRIM))
    m = _REQUEST.match(first.rstrip(_TRIM))
    if not m:
        return None
    subject = m.group(m.lastgroup).strip()
    subject = re.sub(r"^me\s+", "", subject, flags=re.IGNORECASE)
    if (not subject or subject.casefold() in ("me", "it", "them", "this", "that")
            or _QUESTION.match(subject) or re.search(r"\b(?:maps?|is|are|was|were)\b", subject, re.IGNORECASE)):
        return None
    return subject[:_MAX_SUBJECT].strip()
