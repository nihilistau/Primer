"""Typed timeline requests ("timeline of the Tudors", "Tudor timeline", "when did X happen")."""
from __future__ import annotations

import re

_HEY = r"(?:(?:hey|ok|okay)[,\s]+)?"
_POLITE = r"(?:(?:(?:can|could|would)\s+you|please)\s+)?"
_MAKE = r"(?:show|give|make|draw|create|generate|build)"
_ART = r"(?:an?\s+|the\s+|my\s+)?"
# Groups: a = "[show me] [a] timeline of/for X", b = "X timeline", c = "when did X happen".
_REQUEST = re.compile(
    rf"^{_HEY}(?:"
    rf"{_POLITE}(?:{_MAKE}(?:\s+me)?\s+)?{_ART}time\s?line\s+(?:of|for)\s+(?P<a>.+)"
    rf"|(?P<b>.+?)\s+time\s?line"
    rf"|when\s+did\s+(?P<c>.+?)\s+(?:happen|take\s+place|occur)"
    rf")$",
    re.IGNORECASE)
_QUESTION = re.compile(r"^(?:what|how|why|who|whom|whose|when|where|which|is|are|was|were|do|does|did|"
                       r"can|could|should|would|will|if|that|this|it|there)\b", re.IGNORECASE)
_NOT_SUBJECT = re.compile(rf"^(?:{_MAKE}|me|a|an|the|my|your|this|that|it|them|of|for)$|"
                          rf"^{_MAKE}\b|\btime\s?lines?\b", re.IGNORECASE)
# "X timeline" is only a request when X is a short noun phrase: a sentence that ends in
# "timeline" ("tell me about the Tudor timeline") stays an ordinary page query
_BARE_MAX_WORDS = 5
_BARE_LEAD = re.compile(
    r"^(?:i|i'm|im|me|my|we|you|he|she|they|it|about|tell|explain|describe|write|read|see|look|"
    r"want|need|show|give|make|draw|create|generate|build|find|get|let|let's|lets|please|"
    r"what|how|why|who|when|where|which|is|are|was|were|do|does|did|can|could|would|should)\b",
    re.IGNORECASE)
_BARE_INSIDE = re.compile(r"\b(?:about|me|i|want|essay|explain|tell)\b", re.IGNORECASE)
# "the Tudors on a timeline", "put the Tudors on a timeline", "show me the Tudors as a timeline"
_ON_A = re.compile(r"(?:^|\s+)(?:on|in|as)\s+(?:an?|the)$", re.IGNORECASE)
_PUT = re.compile(rf"^{_POLITE}(?:(?:put|place|plot|lay\s+out)|{_MAKE}(?:\s+me)?)\s+", re.IGNORECASE)
# "map of the Tudor timeline" is a map request (as primer.diagrams.intent._OTHER_MEDIA)
_OTHER_MEDIA = re.compile(r"^(?:an?\s+|the\s+)?(?:picture|image|photo|drawing|video|clip|map|story|diagram)"
                          r"\s+of(?!\w)", re.IGNORECASE)
_TAIL = re.compile(r"(?:,?\s+please|\s+for\s+me)$", re.IGNORECASE)
_TRIM = " \t\r\n?.!"
_MAX_SUBJECT = 300


def match_timeline_request(text) -> str | None:
    """The topic of a typed timeline request, or None when the text isn't one."""
    if not isinstance(text, str):
        return None
    lines = text[:5000].strip().splitlines()
    if not lines:
        return None
    first = lines[0][:2000].strip()
    question = first.endswith("?")
    for _ in range(2):  # "a timeline of the Tudors for me, please"
        first = _TAIL.sub("", first.rstrip(_TRIM))
    m = _REQUEST.match(first.rstrip(_TRIM))
    if not m:
        return None
    if question and m.lastgroup != "c":
        return None  # a question about timelines, not a request
    subject = m.group(m.lastgroup).strip(" ,")
    if m.lastgroup == "b":
        on_a = _ON_A.search(subject)
        if on_a:
            subject = _PUT.sub("", subject[:on_a.start()]).strip(" ,")
        if _OTHER_MEDIA.match(subject):
            return None
    if not subject or _NOT_SUBJECT.search(subject) or (m.lastgroup != "c" and _QUESTION.match(subject)):
        return None
    if m.lastgroup == "b" and (len(subject.split()) > _BARE_MAX_WORDS or _BARE_LEAD.match(subject)
                               or _BARE_INSIDE.search(subject)):
        return None
    if m.lastgroup == "c" and subject.casefold() in ("it", "that", "this", "they", "them", "these", "those"):
        return None
    return subject[:_MAX_SUBJECT].strip()
