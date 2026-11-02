"""Prompt builders for pages, highlights and branch cards, plus tolerant parsers of model output."""
from __future__ import annotations

import json
import re

from .graph import Card

_SYSTEM = (
    "You are the Illustrated Primer, a storyteller writing one page of an endless illustrated book. "
    "Mode: storyteller. Write vividly; accuracy is best-effort. "
    "Never mention being an AI, a model or an assistant. "
    "Output a Markdown article that opens with a single `# Title` line."
)

_LENGTH = {
    "normal": "Length: roughly 250-400 words. Use no `##` subheads.",
    "deeper": "Length: roughly 600-900 words. Organise it with `##` subheads.",
    "simpler": "Length: roughly 150 words. Use plain words and short sentences. Use no `##` subheads.",
}

_TRAILING = re.compile(r",\s*([\]}])")
_FENCE = re.compile(r"```[a-zA-Z]*")


def page_messages(concept: str, *, kind: str, parent_title: str | None,
                  parent_excerpt: str | None, recent: list[str], depth: str) -> list[dict]:
    parts = [_SYSTEM, _LENGTH.get(depth, _LENGTH["normal"])]
    if kind == "context" and parent_title:
        ask = f"Write the page about \"{concept}\" as it relates to \"{parent_title}\"."
        if parent_excerpt:
            ask += f"\n\nThe reader clicked it while reading this passage of \"{parent_title}\":\n{parent_excerpt}"
    else:
        ask = f"Write the page about \"{concept}\"."
    if recent:
        ask += ("\n\nThe reader has already read about: " + "; ".join(recent)
                + ". Build on them, don't repeat them.")
    return [{"role": "system", "content": "\n".join(parts)}, {"role": "user", "content": ask}]


def highlight_messages(markdown: str) -> list[dict]:
    return [
        {"role": "system", "content": (
            "Return a JSON array of 6-12 short phrases copied exactly from the text that a curious "
            "reader might want to explore next. Prefer concrete nouns and named things. "
            "Output only the JSON array.")},
        {"role": "user", "content": markdown},
    ]


def branch_messages(phrase: str, page_title: str, sentence: str) -> list[dict]:
    return [
        {"role": "system", "content": (
            "You suggest three directions a curious reader could follow next. Return only JSON of the form "
            '{"context": {"title", "blurb"}, "general": {"title", "blurb"}, '
            '"sideways": {"concept", "title", "blurb"}}. '
            "context: the phrase as it relates to the current page. general: the phrase on its own. "
            "sideways: a related but different concept. A blurb is at most 8 words.")},
        {"role": "user", "content": (
            f'Page: "{page_title}"\nSentence: {sentence}\nPhrase: "{phrase}"')},
    ]


def _extract_json(raw: str, opener: str, closer: str, accept=lambda v: True):
    """First span opening with `opener` that parses to the matching type (list or dict).

    Tries strict JSON at each opener, then trailing-comma repair, then single-quote
    swap (only once the earlier passes failed, so apostrophes in valid JSON survive).
    """
    text = _FENCE.sub("", raw).replace("```", "")
    want = list if opener == "[" else dict
    starts = [m.start() for m in re.finditer(re.escape(opener), text)]
    decoder = json.JSONDecoder()
    for i in starts:
        try:
            value, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(value, want) and accept(value):
            return value
    ends = [m.start() for m in re.finditer(re.escape(closer), text)][::-1]
    repairs = (
        lambda t: _TRAILING.sub(r"\1", t),
        lambda t: _TRAILING.sub(r"\1", t).replace("'", '"'),
    )
    for repair in repairs:
        for i in starts:
            for j in ends:
                if j <= i:
                    break
                try:
                    value = json.loads(repair(text[i:j + 1]))
                except ValueError:
                    continue
                if isinstance(value, want) and accept(value):
                    return value
    return None


_WORD = re.compile(r"\w+(?:[-'’]\w+)*")
_STOPWORDS = frozenset(
    "a about above after again all also an and any are as at be because been before being between both "
    "but by can could did do does down during each few for from further had has have having he her here "
    "him his how i if in into is it its just like more most no nor not of off on once only or other our "
    "out over own same she should so some such than that the their them then there these they this those "
    "through to too under until up very was we were what when where which while who why will with would "
    "you your description details detail overview introduction".split())


def _good_word(word: str) -> bool:
    return sum(c.isalpha() for c in word) >= 4 and word.casefold() not in _STOPWORDS


def _snap(phrase: str, body: str, title: str) -> str | None:
    """The longest contiguous run of the phrase's words found verbatim (on one line) in `body`.

    Returns the page's own text. Rejected when it keeps under half of the phrase's content
    words, or is a single word that the page title already contains.
    """
    words = _WORD.findall(phrase)
    content = sum(w.casefold() not in _STOPWORDS for w in words)
    for length in range(len(words), 0, -1):
        for i in range(len(words) - length + 1):
            run = words[i:i + length]
            if not any(_good_word(w) for w in run):
                continue
            if sum(w.casefold() not in _STOPWORDS for w in run) * 2 < content:
                continue
            pattern = r"(?<!\w)" + r"[ \t]+".join(re.escape(w) for w in run) + r"(?!\w)"
            m = re.search(pattern, body, re.IGNORECASE)
            if not m:
                continue
            if length == 1 and re.search(r"(?<!\w)" + re.escape(run[0]) + r"(?!\w)", title, re.IGNORECASE):
                continue
            return m.group(0)
    return None


def parse_highlights(raw: str, markdown: str) -> list[str]:
    data = _extract_json(raw, "[", "]")
    if not isinstance(data, list):
        return []
    lines = markdown.split("\n")
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip().casefold()
        lines = lines[1:]
    body = "\n".join(lines)
    haystack = body.casefold()
    seen: set[str] = set()
    out: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        phrase = item.strip()
        if not phrase or phrase.casefold() == title:
            continue
        if phrase.casefold() not in haystack:  # small models paraphrase: snap to what the page says
            phrase = _snap(phrase, body, title) or ""
        key = phrase.casefold()
        if not 1 <= len(phrase) <= 60 or key in seen or key == title:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) == 12:
            break
    return out


def _title_case(phrase: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in phrase.split())


def _field(d, key: str) -> str | None:
    v = d.get(key) if isinstance(d, dict) else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def parse_branches(raw: str, phrase: str, page_title: str) -> list[Card]:
    """Context and general are always present (the model's card, else a fallback); sideways only if valid."""
    data = _extract_json(raw, "{", "}", lambda d: any(k in d for k in ("context", "general", "sideways")))
    data = data if isinstance(data, dict) else {}
    name = _title_case(phrase)
    fallback = {
        "context": Card("context", phrase, f"{name} in {page_title}", "In this context"),
        "general": Card("general", phrase, name, "The concept on its own"),
    }
    cards: list[Card] = []
    for via in ("context", "general", "sideways"):
        d = data.get(via)
        title, blurb = _field(d, "title"), _field(d, "blurb")
        concept = _field(d, "concept") if via == "sideways" else phrase
        if title and blurb and concept:
            cards.append(Card(via, concept, title, blurb))
        elif via in fallback:
            cards.append(fallback[via])
    return cards


def sentence_around(markdown: str, phrase: str) -> str:
    m = re.search(re.escape(phrase), markdown, re.IGNORECASE) if phrase else None
    if not m:
        return ""
    idx = m.start()
    start = max((markdown.rfind(c, 0, idx) for c in ".!?\n"), default=-1) + 1
    ends = [i for i in (markdown.find(c, idx) for c in ".!?\n") if i >= 0]
    end = min(ends) + 1 if ends else len(markdown)
    return markdown[start:end].strip()
