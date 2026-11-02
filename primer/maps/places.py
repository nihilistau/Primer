"""Finding the places a page names: a fast-model prompt and a tolerant parser. No coordinates here."""
from __future__ import annotations

import re

from primer.maps import Place
from primer.prompts import _extract_json

_MAX_PLACES = 12
_MAX_NAME = 80
_PAGE_CHARS = 6000

_SYSTEM = (
    "You list the real places a text names, so they can be pinned on a map.\n"
    'Reply with only a JSON array of at most 12 objects: [{"name": "...", "what": "..."}].\n'
    '"name" is the place as the text writes it. "what" says what kind of place it is and where, '
    "in at most 6 words (for example: castle in Warwickshire).\n"
    "Rules: list each place once. Only real, mappable places: towns and cities, buildings, "
    "natural features, regions, countries, battle sites. Leave out people, artworks, books, "
    "companies, events, ideas and eras. Do not give coordinates or guess; if the text names no "
    "place, reply [].\n"
    'Example: text "Kenilworth Castle is a ruin near Coventry." gives '
    '[{"name": "Kenilworth Castle", "what": "castle in Warwickshire"}, '
    '{"name": "Coventry", "what": "city in the West Midlands"}]\n'
    'Example: text "Leonardo painted the Mona Lisa." gives []'
)


def place_messages(title: str, markdown: str) -> list[dict]:
    user = f"Page title: {title}\n\nText:\n{markdown[:_PAGE_CHARS]}"
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


_PHRASE_SYSTEM = (
    "You say what one named place is, so it can be found on a map.\n"
    'Reply with only one JSON object: {"name": "...", "what": "..."}.\n'
    '"name" is the place as given. "what" says what kind of place it is and where, in at most 6 '
    "words (for example: castle in Warwickshire), judged from the sentence it appears in. "
    'Do not give coordinates or guess; if you cannot tell, use an empty "what".\n'
    'Example: place "River Avon" in "Warwick stands on the River Avon." gives '
    '{"name": "River Avon", "what": "river in Warwickshire"}'
)


def phrase_place_messages(title: str, sentence: str, phrase: str) -> list[dict]:
    """One tiny question: what is `phrase` (a place named in `sentence` of the page `title`)?"""
    user = f"Page title: {title}\n\nSentence: {sentence[:600] or '(not found)'}\n\nPlace: {phrase}"
    return [{"role": "system", "content": _PHRASE_SYSTEM}, {"role": "user", "content": user}]


def parse_phrase_what(raw: str) -> str:
    item = _extract_json(raw or "", "{", "}")
    what = item.get("what") if isinstance(item, dict) else None
    return " ".join(what.split())[:120] if isinstance(what, str) else ""


def parse_places(raw: str, markdown: str) -> list[Place]:
    items = _extract_json(raw or "", "[", "]")
    if not isinstance(items, list):
        return []
    out: list[Place] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = " ".join(item["name"].split())
        if not name or len(name) > _MAX_NAME:
            continue
        found = re.search(re.escape(name), markdown, re.IGNORECASE)
        if found:
            name = found.group(0)  # snap to the page's own spelling
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        what = item.get("what")
        out.append(Place(name=name, what=" ".join(what.split())[:120] if isinstance(what, str) else ""))
        if len(out) == _MAX_PLACES:
            break
    return out
