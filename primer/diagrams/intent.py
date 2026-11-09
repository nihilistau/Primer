"""Typed diagram requests ("diagram of a trebuchet", "draw a diagram of X", "show me a diagram
of how X works", "draw a trebuchet diagram"). "how does X work" alone is a question: it stays a page."""
from __future__ import annotations

import re

_HEY = r"(?:(?:hey|ok|okay)[,\s]+)?"
_POLITE = r"(?:(?:(?:can|could|would)\s+you|please)\s+)?"
_ART = r"(?:an?\s+|the\s+)?"
_MAKE = r"(?:show|give|make|draw|create|generate|sketch)"
# a = "[draw [me]] [a] diagram of X", b = "draw/make/show [me] X diagram" (the verb is needed: a
# bare "Venn diagram" or "tell me about the Feynman diagram" is a topic, not a request)
_REQUEST = re.compile(
    rf"^{_HEY}(?:{_POLITE}(?:{_MAKE}(?:\s+(?:me|us))?\s+)?{_ART}diagram\s+(?:of|for|showing)\s+(?P<a>.+)"
    rf"|{_POLITE}{_MAKE}(?:\s+(?:me|us))?\s+(?P<b>.+?)\s+diagram)$",
    re.IGNORECASE)
# "how X works" / "how does X work" after "diagram of": the thing is X
_HOW = re.compile(r"^how\s+(?:(?:does|do)\s+)?(?P<x>.+?)\s+works?$", re.IGNORECASE)
_QUESTION = re.compile(r"^(?:what|how|why|who|whom|whose|when|where|which|is|are|was|were|do|does|did|"
                       r"can|could|should|would|will|if|that|this|it|there)\b", re.IGNORECASE)
_FILLER = re.compile(r"^(?:(?:please|draw|show|make|give|create|generate|sketch|me|us|an?|the|"
                     r"can|could|would|you)\s*)+$", re.IGNORECASE)
_OTHER_MEDIA = re.compile(r"^(?:an?\s+|the\s+)?(?:picture|image|photo|drawing|video|clip|map|story)\s+of(?!\w)",
                          re.IGNORECASE)  # "a picture of a circuit diagram" is a picture request
_TAIL = re.compile(r"(?:,?\s+please|\s+for\s+me)$", re.IGNORECASE)
_TRIM = " \t\r\n?.!"
_MAX_SUBJECT = 300


def match_diagram_request(text: str) -> str | None:
    """The thing to diagram in a typed diagram request, or None when the text isn't one."""
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    first = lines[0][:2000].strip()
    if first.endswith("?"):
        return None  # a question about a diagram, not a request
    for _ in range(2):  # "a diagram of a siphon for me, please"
        first = _TAIL.sub("", first.rstrip(_TRIM))
    m = _REQUEST.match(first.rstrip(_TRIM))
    if not m:
        return None
    subject = m.group(m.lastgroup).strip()
    how = _HOW.match(subject) if m.lastgroup == "a" else None
    if how:
        subject = how.group("x").strip()
    if (not subject or _FILLER.match(subject) or _OTHER_MEDIA.match(subject)
            or subject.casefold() in ("me", "it", "them", "this", "that")
            or _QUESTION.match(subject) or re.search(r"\b(?:diagrams?|is|are|was|were)\b", subject, re.IGNORECASE)):
        return None
    return subject[:_MAX_SUBJECT].strip()
