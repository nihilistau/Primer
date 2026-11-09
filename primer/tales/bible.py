"""The story bible: who the hero is, who they travel with, where they have been, what they carry,
what is still open, and a summary of each chapter. Everything a model writes into it passes the
strict parser and the limits here; page text never reaches it except through that update."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from primer.prompts import parse_object

NAME_MAX, DESC_MAX, LOOK_MAX = 60, 300, 200
GENDERS = ("female", "male")
ENTRY_MAX, LIST_MAX = 160, 12
SUMMARY_MAX = 600
SEED_MAX = 500
BLOCK_MAX = 1500
PREFIX_MAX = 400

_CONTROL = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_TAG_LIKE = re.compile(r"<[^<>]*>")


def clean(value, limit: int) -> str:
    """Plain text: control characters and markup removed, spaces collapsed, at most `limit`
    characters. Anything that is not a string is ''."""
    if not isinstance(value, str):
        return ""
    text = _TAG_LIKE.sub(" ", _CONTROL.sub(" ", value)).replace("<", " ").replace(">", " ")
    return " ".join(text.split())[:limit].rstrip()


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _has(items: list[str], text: str) -> bool:
    return any(_same(i, text) for i in items)


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (clean(v, ENTRY_MAX) for v in value[:LIST_MAX * 2]) if s]


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def gender_of(value) -> str:
    """"female", "male" or "" from what a model or an older bible gave (female, girl, woman,
    she/her; male, boy, man, he/him); anything else is unset."""
    words = re.findall(r"[a-z]+", value.lower()) if isinstance(value, str) else []
    if any(w in ("female", "girl", "woman", "she", "her") for w in words):
        return "female"
    if any(w in ("male", "boy", "man", "he", "him") for w in words):
        return "male"
    return ""


def _list(value) -> list:
    return value if isinstance(value, list) else []


@dataclass
class Bible:
    hero: dict = field(default_factory=lambda: {"name": "", "gender": "", "description": "",
                                                "look": ""})
    companions: list[dict] = field(default_factory=list)  # {name, look, note}
    places: list[str] = field(default_factory=list)
    things: list[str] = field(default_factory=list)
    threads: list[dict] = field(default_factory=list)  # {text, open}
    chapters: list[dict] = field(default_factory=list)  # {n, text}: each merged chapter's summary, by n

    # --- storage ---
    def to_json(self) -> str:
        return json.dumps({"hero": self.hero, "companions": self.companions, "places": self.places,
                           "things": self.things, "threads": self.threads,
                           "chapters": self.chapters}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw) -> "Bible":
        """Tolerant: missing or wrong-typed keys take their defaults; everything is clipped."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data = None
        data = _dict(data)
        hero = _dict(data.get("hero"))
        bible = cls()
        bible.hero = {"name": clean(hero.get("name"), NAME_MAX),
                      "gender": gender_of(hero.get("gender") or hero.get("pronouns")),
                      "description": clean(hero.get("description"), DESC_MAX),
                      "look": clean(hero.get("look"), LOOK_MAX)}
        for c in _list(data.get("companions")):
            c = _dict(c)
            name = clean(c.get("name"), NAME_MAX)
            if name and len(bible.companions) < LIST_MAX \
                    and not any(_same(name, x["name"]) for x in bible.companions):
                bible.companions.append({"name": name, "look": clean(c.get("look"), LOOK_MAX),
                                         "note": clean(c.get("note"), ENTRY_MAX)})
        for key in ("places", "things"):
            out: list[str] = []
            for s in _strings(data.get(key)):
                if not _has(out, s):
                    out.append(s)
            setattr(bible, key, out[-LIST_MAX:])
        threads: list[dict] = []
        for t in _list(data.get("threads"))[:LIST_MAX * 2]:
            if isinstance(t, str):
                t = {"text": t, "open": True}
            t = _dict(t)
            text = clean(t.get("text"), ENTRY_MAX)
            if text and not any(_same(text, x["text"]) for x in threads):
                threads.append({"text": text, "open": bool(t.get("open", True))})
        bible.threads = threads[-LIST_MAX:]
        # {n, text} entries; an older bible's plain strings are numbered by their place
        summaries: dict[int, str] = {}
        for i, c in enumerate(_list(data.get("chapters")), start=1):
            n, text = (i, c) if isinstance(c, str) else (_dict(c).get("n"), _dict(c).get("text"))
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                summaries[n] = clean(text, SUMMARY_MAX)
        bible.chapters = [{"n": n, "text": summaries[n]} for n in sorted(summaries)][-200:]
        return bible

    # --- the model's update ---
    def apply_update(self, update: dict, n: int | None = None) -> list[str]:
        """Merge a model-written update for chapter `n` (default: the one after the last
        summary). Only the known keys count; every string is cleaned and clipped, duplicates are
        skipped, lists stay within 12. Returns what changed, in words. An update with no summary
        changes nothing."""
        if not isinstance(update, dict):
            return []
        summary = clean(update.get("summary"), SUMMARY_MAX)
        if not summary:
            return []
        changes: list[str] = []
        hero = self.hero["name"].casefold()

        for c in _list(update.get("new_companions"))[:LIST_MAX]:
            c = _dict(c)
            name = clean(c.get("name"), NAME_MAX)
            if not name or name.casefold() == hero \
                    or any(_same(name, x["name"]) for x in self.companions):
                continue
            if len(self.companions) >= LIST_MAX:
                recent = " ".join([c["text"] for c in self.chapters[-1:]] + [summary]).casefold()
                gone = next((x for x in self.companions[:-1]
                             if x["name"].casefold() not in recent), None)
                if gone is None:
                    continue
                self.companions.remove(gone)
                changes.append(f"Companion left the cast: {gone['name']}")
            self.companions.append({"name": name, "look": clean(c.get("look"), LOOK_MAX),
                                    "note": clean(c.get("note"), ENTRY_MAX)})
            changes.append(f"New companion: {name}")

        for key, label in (("places", "New place"), ("things", "New thing")):
            for s in _strings(update.get("new_" + key))[:LIST_MAX]:
                items = getattr(self, key)
                if _has(items, s):
                    continue
                items.append(s)
                del items[:-LIST_MAX]  # the oldest are dropped
                changes.append(f"{label}: {s}")

        for s in _strings(update.get("lost_things"))[:LIST_MAX]:
            for t in [t for t in self.things if _same(t, s)]:
                self.things.remove(t)
                changes.append(f"Lost: {t}")

        for s in _strings(update.get("closed_threads"))[:LIST_MAX]:
            for t in self.threads:
                if t["open"] and _same(t["text"], s):
                    t["open"] = False
                    changes.append(f"Thread closed: {t['text']}")
                    break

        for s in _strings(update.get("open_threads"))[:LIST_MAX]:
            if any(_same(t["text"], s) for t in self.threads):
                continue
            self.threads.append({"text": s, "open": True})
            changes.append(f"New thread: {s}")
            while len(self.threads) > LIST_MAX:  # the oldest closed thread goes first
                closed = next((t for t in self.threads if not t["open"]), self.threads[0])
                self.threads.remove(closed)

        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            n = (self.chapters[-1]["n"] if self.chapters else 0) + 1
        self.chapters = sorted([c for c in self.chapters if c["n"] != n] + [{"n": n, "text": summary}],
                               key=lambda c: c["n"])
        return changes

    # --- the reader's edits ---
    def edit(self, fields: dict) -> list[str]:
        """The reader's changes to the hero: name, gender, description and look (other keys
        ignored).
        A blank name, or a gender other than female, male or "" (unset), is refused with
        ValueError and nothing is changed."""
        fields = _dict(fields)
        if isinstance(fields.get("name"), str) and not clean(fields["name"], NAME_MAX):
            raise ValueError("the hero needs a name")
        changes = []
        if "gender" in fields:
            gender = fields["gender"]
            if gender not in ("", *GENDERS):
                raise ValueError("gender is female, male or empty")
            if gender != self.hero.get("gender", ""):
                self.hero["gender"] = gender
                changes.append("Hero gender changed")
        for key, limit in (("name", NAME_MAX), ("description", DESC_MAX), ("look", LOOK_MAX)):
            if isinstance(fields.get(key), str):
                value = clean(fields[key], limit)
                if value != self.hero.get(key, ""):
                    self.hero[key] = value
                    changes.append(f"Hero {key} changed")
        return changes

    def remove_thread(self, text: str) -> bool:
        want = clean(text, ENTRY_MAX)
        kept = [t for t in self.threads if not _same(t["text"], want)]
        removed, self.threads = len(kept) != len(self.threads), kept
        return removed

    def remove_place(self, text: str) -> bool:
        want = clean(text, ENTRY_MAX)
        kept = [p for p in self.places if not _same(p, want)]
        removed, self.places = len(kept) != len(self.places), kept
        return removed

    # --- for prompts ---
    def prompt_block(self) -> str:
        """The bible as compact plain text (at most 1500 characters). The hero, the open
        threads and the latest chapter summaries are placed first and trimmed to their share;
        companions (newest first), places and things take what is left."""
        h = self.hero
        room = [BLOCK_MAX]

        def fit(text: str, cap: int) -> str:
            cap = min(cap, room[0])
            if cap <= 0 or not text:
                return ""
            if len(text) > cap:
                cut = text[:cap - 1]
                space = cut.rfind(" ")
                text = (cut[:space] if space > cap // 2 else cut).rstrip() + "\u2026"
            room[0] -= len(text) + 1
            return text

        def joined(label: str, items: list[str], cap: int) -> str:
            take: list[str] = []
            for item in reversed(items):  # the newest entries are kept
                if len(label) + len("; ".join([item] + take)) > min(cap, room[0]):
                    break
                take.insert(0, item)
            return fit(label + "; ".join(take), cap) if take else ""

        if h["name"]:
            who = (f"Hero: {h['name']}" + (f", {h['gender']}" if h.get("gender") else "")
                   + (f". {h['description']}" if h["description"] else ""))
        else:
            who = f"Hero: {h['description']}" if h["description"] else ""
        hero_line = fit(who, 250)
        look_line = fit(f"Hero's look: {h['look']}", 150) if h["look"] else ""
        threads = joined("Open threads: ", [t["text"] for t in self.threads if t["open"]], 350)
        latest = [(c["n"], c["text"]) for c in self.chapters[-2:]]
        chapter_lines = {i: fit(f"Chapter {i}: {t}", 600) for i, t in reversed(latest)}
        comps = {}
        for c in reversed(self.companions):
            note = f" - {c['note']}" if c["note"] else ""
            comps[c["name"]] = fit(f"Companion: {c['name']}{note}", 200)
        places = joined("Places: ", self.places, 300)
        things = joined("Things carried or kept: ", self.things, 300)
        lines = [hero_line, look_line, *[comps[c["name"]] for c in self.companions],
                 places, things, threads, *[chapter_lines[i] for i, _ in latest]]
        return "\n".join(line for line in lines if line)

    def hero_label(self) -> str:
        """The hero's name and gender ("Wren, female"), as far as they are known."""
        return ", ".join(x for x in (self.hero["name"], self.hero.get("gender", "")) if x)

    def look_prefix(self, text) -> str:
        """The hero's "Name, female: look" (when they have a look), then "Name: look" for
        each companion named in `text` (a scene, or a list of names); '' when nobody has a look.
        At most 400 characters."""
        if not isinstance(text, str):
            text = " ".join(str(t) for t in (text or []))
        entries = []
        if self.hero["look"]:
            label = self.hero_label()
            entries.append(f"{label}: {self.hero['look']}" if label else self.hero["look"])
        for c in self.companions:
            if c["look"] and re.search(r"(?<!\w)" + re.escape(c["name"]) + r"(?!\w)", text, re.I):
                entries.append(f"{c['name']}: {c['look']}")
        out = ""
        for entry in entries:
            entry = entry[:PREFIX_MAX] if not out else entry
            if len(out) + (2 if out else 0) + len(entry) > PREFIX_MAX:
                break  # never cut an entry in the middle
            out += ("; " if out else "") + entry
        return out


def parse_update(raw: str) -> dict | None:
    """The first JSON object in a model's reply (code fences and leading text are fine) that has
    a non-empty `summary` string; None otherwise."""
    return parse_object(raw or "", lambda v: isinstance(v.get("summary"), str)
                        and bool(clean(v["summary"], SUMMARY_MAX)))
