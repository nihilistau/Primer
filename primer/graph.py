"""SQLite concept graph: nodes, edges, page versions, highlights, branch cards, media, maps,
diagrams, stories and drafts (pages written ahead, invisible until opened)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import string
import threading
import unicodedata
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Sequence

from .grounding import Source

_QUOTES = "\"'“”‘’«»"
_LEAD_CHARS = string.whitespace + _QUOTES + "([{"
_TRAIL_CHARS = string.whitespace + _QUOTES + ")]}.,;:!?"
_ARTICLES = ("the ", "a ", "an ")


def normalize_concept(text: str) -> str:
    s = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    s = s.lstrip(_LEAD_CHARS).rstrip(_TRAIL_CHARS)
    for article in _ARTICLES:
        if s.startswith(article):
            s = s[len(article):].lstrip(_LEAD_CHARS)
            break
    return s


def clean_typed(text: str) -> str | None:
    s = text.strip()
    return s[:200] if s else None


@dataclass
class Node:
    id: str
    kind: Literal["concept", "context"]
    concept: str
    title: str
    parent_id: str | None


@dataclass
class Version:
    node_id: str
    n: int
    markdown: str
    source_mode: str
    depth: Literal["normal", "deeper", "simpler"]
    created_at: float


@dataclass
class Card:
    via: Literal["context", "general", "sideways"]
    concept: str
    title: str
    blurb: str


@dataclass
class Draft:
    """A page written ahead of the reader (thinking ahead); never a version until opened."""
    node_id: str
    depth: str
    source_mode: str
    markdown: str
    sources: list[Source]
    status: Literal["writing", "done"]
    created_at: float


@dataclass
class Media:
    id: str
    node_id: str
    phrase: str
    kind: str
    model_id: str
    prompt: str
    settings: dict
    status: str
    file: str | None
    error: str | None
    created_at: float


_MEDIA_STATUSES = ("prompting", "queued", "loading", "generating", "done", "failed", "cancelled")
_MEDIA_FINAL = ("done", "failed", "cancelled")
_MEDIA_UPDATABLE = ("status", "prompt", "file", "error", "settings")
_KEEP = object()
_MAP_STATUSES = ("finding", "locating", "done", "failed")
_MAP_FINAL = ("done", "failed")
_MAP_COLS = "id, node_id, n, phrase, status, error"
_STORY_STATUSES = ("writing", "narrating", "done", "failed")
_STORY_FINAL = ("done", "failed")
_SCENE_STATUSES = ("waiting", "narrating", "done", "failed")
_STORY_COLS = "id, node_id, n, kind, title, status, error, voice, created_at"
_STORY_KEYS = ("id", "node_id", "n", "kind", "title", "status", "error", "voice", "created_at")
_SCENE_FIELDS = ("title", "text", "spoken", "media_id", "audio", "status", "error")
_SCENE_KEYS = ("idx", "title", "text", "spoken", "media_id", "audio", "chunks", "status", "error")
_MAP_KEYS = ("id", "node_id", "n", "phrase", "status", "error")
_TIMELINE_STATUSES = ("reading", "done", "failed")
_TIMELINE_FINAL = ("done", "failed")
_TIMELINE_KEYS = ("id", "node_id", "n", "status", "error", "created_at", "updated_at")
_TIMELINE_COLS = ", ".join(_TIMELINE_KEYS)
_EVENT_KEYS = ("label", "date_text", "year", "month", "day", "end_year", "precision", "approx")
_EVENT_COLS = ", ".join(_EVENT_KEYS)
_DIAGRAM_STATUSES = ("queued", "drawing", "done", "failed")
_DIAGRAM_FINAL = ("done", "failed")
_DIAGRAM_KEYS = ("id", "node_id", "n", "phrase", "status", "error", "caption", "created_at")
_DIAGRAM_COLS = ", ".join(_DIAGRAM_KEYS)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, concept TEXT NOT NULL,
    title TEXT NOT NULL, parent_id TEXT);
CREATE TABLE IF NOT EXISTS edges(
    from_id TEXT, to_id TEXT NOT NULL, phrase TEXT NOT NULL, via TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions(
    node_id TEXT NOT NULL, n INTEGER NOT NULL, markdown TEXT NOT NULL, source_mode TEXT NOT NULL,
    depth TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(node_id, n));
CREATE TABLE IF NOT EXISTS sources(
    node_id TEXT NOT NULL, n INTEGER NOT NULL, idx INTEGER NOT NULL, title TEXT NOT NULL,
    origin TEXT NOT NULL, book TEXT NOT NULL, path TEXT NOT NULL, url TEXT NOT NULL,
    place TEXT NOT NULL, PRIMARY KEY(node_id, n, idx));
CREATE TABLE IF NOT EXISTS highlights(
    node_id TEXT NOT NULL, n INTEGER NOT NULL, phrases_json TEXT NOT NULL, PRIMARY KEY(node_id, n));
CREATE TABLE IF NOT EXISTS cards(
    node_id TEXT NOT NULL, phrase_norm TEXT NOT NULL, cards_json TEXT NOT NULL,
    PRIMARY KEY(node_id, phrase_norm));
CREATE TABLE IF NOT EXISTS media(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), phrase TEXT NOT NULL,
    kind TEXT NOT NULL, model_id TEXT NOT NULL, prompt TEXT NOT NULL, settings_json TEXT NOT NULL,
    status TEXT NOT NULL, file TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS maps(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), n INTEGER NOT NULL,
    phrase TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL,
    updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS map_places(
    map_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL, what TEXT NOT NULL,
    lat REAL, lon REAL, title TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(map_id, idx));
CREATE TABLE IF NOT EXISTS timelines(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), n INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS timelines_node ON timelines(node_id);
CREATE TABLE IF NOT EXISTS timeline_events(
    timeline_id TEXT NOT NULL, idx INTEGER NOT NULL, label TEXT NOT NULL, date_text TEXT NOT NULL,
    year INTEGER NOT NULL, month INTEGER, day INTEGER, end_year INTEGER, precision TEXT NOT NULL,
    approx INTEGER NOT NULL, PRIMARY KEY(timeline_id, idx));
CREATE TABLE IF NOT EXISTS stories(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), n INTEGER NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, error TEXT, voice TEXT NOT NULL,
    highlights_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS story_scenes(
    story_id TEXT NOT NULL, idx INTEGER NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
    spoken TEXT NOT NULL, media_id TEXT, audio TEXT, chunks_json TEXT NOT NULL, status TEXT NOT NULL,
    error TEXT, PRIMARY KEY(story_id, idx));
CREATE TABLE IF NOT EXISTS drafts(
    node_id TEXT NOT NULL, depth TEXT NOT NULL, source_mode TEXT NOT NULL, markdown TEXT NOT NULL,
    sources_json TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(node_id, source_mode));
CREATE TABLE IF NOT EXISTS place_cache(
    key TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    title TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reader_events(
    at REAL NOT NULL, kind TEXT NOT NULL, node_id TEXT, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reader_events_kind ON reader_events(kind, at);
CREATE TABLE IF NOT EXISTS reader_dwell(node_id TEXT PRIMARY KEY, seconds REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reader_profile(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quiz_items(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, version_n INTEGER NOT NULL, kind TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, created_at REAL NOT NULL, due_at REAL NOT NULL,
    step INTEGER NOT NULL, last_mark TEXT, misses INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS quiz_items_due ON quiz_items(due_at);
CREATE INDEX IF NOT EXISTS quiz_items_node ON quiz_items(node_id, version_n);
CREATE TABLE IF NOT EXISTS quiz_answers(
    item_id TEXT NOT NULL, at REAL NOT NULL, answer TEXT NOT NULL, mark TEXT NOT NULL,
    feedback TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS quiz_answers_item ON quiz_answers(item_id);
CREATE TABLE IF NOT EXISTS quests(
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, status TEXT NOT NULL, rooms_json TEXT NOT NULL,
    at_room INTEGER NOT NULL, tries INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS tales(
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, title TEXT NOT NULL,
    seed TEXT NOT NULL, bible_json TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tale_chapters(
    tale_id TEXT NOT NULL, n INTEGER NOT NULL, story_id TEXT NOT NULL, pages_json TEXT NOT NULL,
    summary TEXT NOT NULL, created_at REAL NOT NULL, bible_update TEXT NOT NULL DEFAULT 'done',
    PRIMARY KEY(tale_id, n));
CREATE TABLE IF NOT EXISTS tale_chapter_pages(
    tale_id TEXT NOT NULL, n INTEGER NOT NULL, pos INTEGER NOT NULL, node_id TEXT NOT NULL,
    PRIMARY KEY(tale_id, n, node_id));
CREATE INDEX IF NOT EXISTS tale_chapter_pages_node ON tale_chapter_pages(node_id);
CREATE TABLE IF NOT EXISTS diagrams(
    id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id), n INTEGER NOT NULL,
    phrase TEXT NOT NULL, status TEXT NOT NULL, error TEXT, svg TEXT NOT NULL, caption TEXT NOT NULL,
    labels_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS diagrams_node ON diagrams(node_id);
"""
# columns added after a table first shipped: (table, column, definition)
_ADDED_COLUMNS = (("tale_chapters", "bible_update", "TEXT NOT NULL DEFAULT 'done'"),)

_NODE_COLS = "id, kind, concept, title, parent_id"
_VERSION_COLS = "node_id, n, markdown, source_mode, depth, created_at"
_DRAFT_COLS = "node_id, depth, source_mode, markdown, sources_json, status, created_at"
_DRAFT_STATUSES = ("writing", "done")
_QUIZ_KEYS = ("id", "node_id", "version_n", "kind", "question", "answer", "created_at", "due_at",
              "step", "last_mark", "misses")
_QUIZ_COLS = ", ".join(_QUIZ_KEYS)
_MEDIA_COLS = "id, node_id, phrase, kind, model_id, prompt, settings_json, status, file, error, created_at"


def _media(row) -> Media:
    return Media(*row[:6], json.loads(row[6]), *row[7:])


_SOURCE_KEYS = ("n", "title", "origin", "book", "path", "url", "where")


def _draft(row) -> Draft:
    sources = [Source(text="", **{k: s[k] for k in _SOURCE_KEYS}) for s in json.loads(row[4])]
    return Draft(*row[:4], sources, *row[5:])


class Graph:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            with self._db:
                self._db.executescript(_SCHEMA)
                for table, column, definition in _ADDED_COLUMNS:
                    have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                    if column not in have:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                # chapters from before tale_chapter_pages: their pages are filled in from the JSON
                for tale_id, n, raw in self._db.execute(
                        "SELECT c.tale_id, c.n, c.pages_json FROM tale_chapters c WHERE NOT EXISTS("
                        "SELECT 1 FROM tale_chapter_pages p WHERE p.tale_id=c.tale_id AND p.n=c.n)"
                        ).fetchall():
                    self._add_chapter_pages(tale_id, n, json.loads(raw))

    # nodes
    @staticmethod
    def _node_key(concept: str, kind: str, parent_id: str | None) -> tuple[str, str, str | None]:
        """(node id, normalised concept, effective parent) for a node; ValueError if it can't be."""
        if kind not in ("concept", "context"):
            raise ValueError(f"kind must be 'concept' or 'context', got {kind!r}")
        if kind == "context" and not parent_id:
            raise ValueError("a context node needs a parent_id")
        norm = normalize_concept(concept)
        if not norm:
            raise ValueError("concept is empty after normalization")
        if kind == "concept":
            parent_id = None
        key = norm if kind == "concept" else f"{norm}\x00{parent_id}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16], norm, parent_id

    def node_id_for(self, concept: str, kind: str, parent_id: str | None = None) -> str:
        """The id node_for would give this node, without creating it."""
        return self._node_key(concept, kind, parent_id)[0]

    def node_for(self, concept: str, kind: str, parent_id: str | None = None,
                 title: str | None = None) -> Node:
        node_id, norm, parent_id = self._node_key(concept, kind, parent_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO nodes(id, kind, concept, title, parent_id) VALUES(?,?,?,?,?)",
                (node_id, kind, norm, title or concept.strip(), parent_id))
            row = self._db.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE id=?", (node_id,)).fetchone()
        return Node(*row)

    def get_node(self, id: str) -> Node | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_NODE_COLS} FROM nodes WHERE id=?", (id,)).fetchone()
        return Node(*row) if row else None

    # edges
    def add_edge(self, from_id: str | None, to_id: str, phrase: str, via: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO edges(from_id, to_id, phrase, via, at) VALUES(?,?,?,?,?)",
                             (from_id, to_id, phrase, via, time.time()))

    # versions
    def latest_version(self, node_id: str) -> Version | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_VERSION_COLS} FROM versions WHERE node_id=? ORDER BY n DESC LIMIT 1",
                (node_id,)).fetchone()
        return Version(*row) if row else None

    def add_version(self, node_id: str, markdown: str, source_mode: str, depth: str,
                    sources: Sequence[Source] = ()) -> Version:
        with self._lock, self._db:
            n = self._db.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM versions WHERE node_id=?",
                                 (node_id,)).fetchone()[0]
            now = time.time()
            self._db.execute(f"INSERT INTO versions({_VERSION_COLS}) VALUES(?,?,?,?,?,?)",
                             (node_id, n, markdown, source_mode, depth, now))
            self._db.executemany(
                "INSERT INTO sources(node_id, n, idx, title, origin, book, path, url, place) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(node_id, n, s.n, s.title, s.origin, s.book, s.path, s.url, s.where) for s in sources])
        return Version(node_id, n, markdown, source_mode, depth, now)

    def sources(self, node_id: str, n: int) -> list[Source]:
        """The numbered sources of a version (their text is not stored)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idx, title, origin, book, path, url, place FROM sources "
                "WHERE node_id=? AND n=? ORDER BY idx", (node_id, n)).fetchall()
        return [Source(idx, title, origin, "", book, path, url, place)
                for idx, title, origin, book, path, url, place in rows]

    def versions(self, node_id: str) -> list[Version]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_VERSION_COLS} FROM versions WHERE node_id=? ORDER BY n", (node_id,)).fetchall()
        return [Version(*r) for r in rows]

    # highlights
    def set_highlights(self, node_id: str, n: int, phrases: list[str]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO highlights(node_id, n, phrases_json) VALUES(?,?,?)",
                             (node_id, n, json.dumps(phrases)))

    def get_highlights(self, node_id: str, n: int) -> list[str] | None:
        with self._lock:
            row = self._db.execute("SELECT phrases_json FROM highlights WHERE node_id=? AND n=?",
                                   (node_id, n)).fetchone()
        return json.loads(row[0]) if row else None

    # branch cards
    def set_cards(self, node_id: str, phrase: str, cards: list[Card]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO cards(node_id, phrase_norm, cards_json) VALUES(?,?,?)",
                             (node_id, normalize_concept(phrase), json.dumps([asdict(c) for c in cards])))

    def get_cards(self, node_id: str, phrase: str) -> list[Card] | None:
        with self._lock:
            row = self._db.execute("SELECT cards_json FROM cards WHERE node_id=? AND phrase_norm=?",
                                   (node_id, normalize_concept(phrase))).fetchone()
        return [Card(**c) for c in json.loads(row[0])] if row else None

    # media
    def add_media(self, node_id: str, phrase: str, kind: str, model_id: str, prompt: str,
                  settings: dict) -> Media:
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() is None:
                raise ValueError(f"unknown node {node_id!r}")
            media_id = uuid.uuid4().hex
            now = time.time()
            self._db.execute(
                f"INSERT INTO media({_MEDIA_COLS}, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (media_id, node_id, phrase, kind, model_id, prompt, json.dumps(settings),
                 "prompting", None, None, now, now))
        return Media(media_id, node_id, phrase, kind, model_id, prompt, settings, "prompting", None, None, now)

    def update_media(self, id: str, **fields) -> Media:
        bad = set(fields) - set(_MEDIA_UPDATABLE)
        if bad:
            raise ValueError(f"cannot update media field(s): {', '.join(sorted(bad))}")
        if "status" in fields and fields["status"] not in _MEDIA_STATUSES:
            raise ValueError(f"unknown media status {fields['status']!r}")
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"])
        cols = {("settings_json" if k == "settings" else k): v for k, v in fields.items()}
        sets = "".join(f"{c}=?, " for c in cols)
        with self._lock, self._db:
            cur = self._db.execute(f"UPDATE media SET {sets}updated_at=? WHERE id=?",
                                   (*cols.values(), time.time(), id))
            if cur.rowcount == 0:
                raise ValueError(f"unknown media {id!r}")
            row = self._db.execute(f"SELECT {_MEDIA_COLS} FROM media WHERE id=?", (id,)).fetchone()
        return _media(row)

    def get_media(self, id: str) -> Media | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_MEDIA_COLS} FROM media WHERE id=?", (id,)).fetchone()
        return _media(row) if row else None

    def media_for_node(self, node_id: str) -> list[Media]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_MEDIA_COLS} FROM media WHERE node_id=? ORDER BY created_at, rowid",
                (node_id,)).fetchall()
        return [_media(r) for r in rows]

    def fail_unfinished_media(self, reason: str) -> int:
        marks = ",".join("?" * len(_MEDIA_FINAL))
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE media SET status='failed', error=?, updated_at=? WHERE status NOT IN ({marks})",
                (reason, time.time(), *_MEDIA_FINAL))
        return cur.rowcount

    # maps
    def add_map(self, node_id: str, n: int, phrase: str) -> str:
        map_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO maps(id, node_id, n, phrase, status, error, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (map_id, node_id, n, phrase, "finding", None, now, now))
        return map_id

    def update_map(self, id: str, status: str | None = None, error=_KEEP) -> None:
        """Change a map's status and/or error. A failed map stays failed (a late writer, such
        as a job running through shutdown, cannot revive it) and a done map stays done; an error is kept unless given."""
        if status is not None and status not in _MAP_STATUSES:
            raise ValueError(f"unknown map status {status!r}")
        with self._lock, self._db:
            row = self._db.execute("SELECT status, error FROM maps WHERE id=?", (id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown map {id!r}")
            if row[0] == "failed" and status != "failed":
                return
            if row[0] == "done" and status == "failed":  # a finished map is not un-finished by a late failure
                return
            self._db.execute("UPDATE maps SET status=?, error=?, updated_at=? WHERE id=?",
                             (status or row[0], row[1] if error is _KEEP else error, time.time(), id))

    def set_map_places(self, id: str, places) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM map_places WHERE map_id=?", (id,))
            self._db.executemany(
                "INSERT INTO map_places(map_id, idx, name, what, lat, lon, title, source) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(id, i, p.name, p.what, p.lat, p.lon, p.title, p.source) for i, p in enumerate(places)])

    def get_map(self, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_MAP_COLS} FROM maps WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            places = self._db.execute(
                "SELECT name, what, lat, lon, title, source FROM map_places WHERE map_id=? ORDER BY idx",
                (id,)).fetchall()
        out = dict(zip(_MAP_KEYS, row))
        out["places"] = [dict(zip(("name", "what", "lat", "lon", "title", "source"), p)) for p in places]
        return out

    def maps_for(self, node_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_MAP_COLS} FROM maps WHERE node_id=? ORDER BY created_at DESC, rowid DESC",
                (node_id,)).fetchall()
        return [dict(zip(_MAP_KEYS, r)) for r in rows]

    def fail_unfinished_maps(self, reason: str) -> int:
        marks = ",".join("?" * len(_MAP_FINAL))
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE maps SET status='failed', error=?, updated_at=? WHERE status NOT IN ({marks})",
                (reason, time.time(), *_MAP_FINAL))
        return cur.rowcount

    # timelines (Phase 12): the dated events of a page version
    def add_timeline(self, node_id: str, n: int) -> str:
        timeline_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO timelines(id, node_id, n, status, error, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)", (timeline_id, node_id, n, "reading", None, now, now))
        return timeline_id

    def update_timeline(self, id: str, status: str | None = None, error=_KEEP) -> None:
        """Change a timeline's status and/or error. Failed stays failed and done stays done
        (a late writer can't revive or un-finish one); an error is kept unless given."""
        if status is not None and status not in _TIMELINE_STATUSES:
            raise ValueError(f"unknown timeline status {status!r}")
        with self._lock, self._db:
            row = self._db.execute("SELECT status, error FROM timelines WHERE id=?", (id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown timeline {id!r}")
            if row[0] == "failed" and status != "failed":
                return
            if row[0] == "done" and status == "failed":
                return
            self._db.execute("UPDATE timelines SET status=?, error=?, updated_at=? WHERE id=?",
                             (status or row[0], row[1] if error is _KEEP else error, time.time(), id))

    def set_timeline_events(self, id: str, events: Sequence[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM timeline_events WHERE timeline_id=?", (id,))
            self._db.executemany(
                f"INSERT INTO timeline_events(timeline_id, idx, {_EVENT_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(id, i, e["label"], e["date_text"], e["year"], e["month"], e["day"], e["end_year"],
                  e["precision"], int(bool(e["approx"]))) for i, e in enumerate(events)])

    @staticmethod
    def _event(row) -> dict:
        out = dict(zip(_EVENT_KEYS, row))
        out["approx"] = bool(out["approx"])
        return out

    def get_timeline(self, id: str) -> dict | None:
        """The timeline with its page's title and its events in time order."""
        with self._lock:
            row = self._db.execute(f"SELECT {_TIMELINE_COLS} FROM timelines WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            title = self._db.execute("SELECT title FROM nodes WHERE id=?", (row[1],)).fetchone()
            events = self._db.execute(
                f"SELECT {_EVENT_COLS} FROM timeline_events WHERE timeline_id=? ORDER BY idx", (id,)).fetchall()
        out = dict(zip(_TIMELINE_KEYS, row))
        out["page_title"] = title[0] if title else ""
        out["events"] = [self._event(e) for e in events]
        return out

    def timelines_for(self, node_id: str) -> list[dict]:
        """A page's timelines, newest first: id, node_id, n, status, error, created_at, event_count."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.id, t.node_id, t.n, t.status, t.error, t.created_at, "
                "(SELECT COUNT(*) FROM timeline_events e WHERE e.timeline_id=t.id) "
                "FROM timelines t WHERE t.node_id=? ORDER BY t.created_at DESC, t.rowid DESC",
                (node_id,)).fetchall()
        return [dict(zip(("id", "node_id", "n", "status", "error", "created_at", "event_count"), r))
                for r in rows]

    def all_timeline_events(self) -> list[dict]:
        """The events of the latest finished timeline of every page, each with timeline_id,
        node_id and page_title, in time order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT t.id, t.node_id, n.title, "
                + ", ".join(f"e.{k}" for k in _EVENT_KEYS) +
                " FROM timelines t JOIN nodes n ON n.id = t.node_id"
                " JOIN timeline_events e ON e.timeline_id = t.id"
                " WHERE t.status='done' AND t.rowid = (SELECT t2.rowid FROM timelines t2"
                "   WHERE t2.node_id = t.node_id AND t2.status='done'"
                "   ORDER BY t2.created_at DESC, t2.rowid DESC LIMIT 1)"
                " ORDER BY e.year, COALESCE(e.month, 0), COALESCE(e.day, 0), e.approx, t.rowid, e.idx"
            ).fetchall()
        return [{"timeline_id": r[0], "node_id": r[1], "page_title": r[2], **self._event(r[3:])} for r in rows]

    def fail_unfinished_timelines(self, reason: str) -> int:
        marks = ",".join("?" * len(_TIMELINE_FINAL))
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE timelines SET status='failed', error=?, updated_at=? WHERE status NOT IN ({marks})",
                (reason, time.time(), *_TIMELINE_FINAL))
        return cur.rowcount

    # diagrams (primer.diagrams)
    def add_diagram(self, node_id: str, n: int, phrase: str) -> str:
        diagram_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO diagrams(id, node_id, n, phrase, status, error, svg, caption, labels_json, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (diagram_id, node_id, n, phrase, "queued", None, "", "", "[]", now, now))
        return diagram_id

    def update_diagram(self, id: str, status: str | None = None, error=_KEEP, svg: str | None = None,
                       caption: str | None = None, labels: list[str] | None = None) -> None:
        """Change a diagram's status, error or drawing. A failed diagram stays failed (a late
        writer, such as a job running through shutdown, cannot revive it) and a done one stays
        done; fields not given are kept."""
        if status is not None and status not in _DIAGRAM_STATUSES:
            raise ValueError(f"unknown diagram status {status!r}")
        with self._lock, self._db:
            row = self._db.execute("SELECT status, error, svg, caption, labels_json FROM diagrams "
                                   "WHERE id=?", (id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown diagram {id!r}")
            if row[0] == "failed" and status != "failed":
                return
            if row[0] == "done" and status == "failed":
                return
            self._db.execute(
                "UPDATE diagrams SET status=?, error=?, svg=?, caption=?, labels_json=?, updated_at=? "
                "WHERE id=?",
                (status or row[0], row[1] if error is _KEEP else error,
                 row[2] if svg is None else svg, row[3] if caption is None else caption,
                 row[4] if labels is None else json.dumps(list(labels)), time.time(), id))

    def get_diagram(self, id: str) -> dict | None:
        """The diagram with its drawing: svg, caption and labels (a list)."""
        with self._lock:
            row = self._db.execute(f"SELECT {_DIAGRAM_COLS}, svg, labels_json FROM diagrams WHERE id=?",
                                   (id,)).fetchone()
        if row is None:
            return None
        out = dict(zip(_DIAGRAM_KEYS, row))
        out["svg"], out["labels"] = row[-2], json.loads(row[-1])
        return out

    def diagrams_for(self, node_id: str) -> list[dict]:
        """A page's diagrams, newest first, without their drawings (GET /v1/diagrams/<id> has them)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_DIAGRAM_COLS} FROM diagrams WHERE node_id=? ORDER BY created_at DESC, rowid DESC",
                (node_id,)).fetchall()
        return [dict(zip(_DIAGRAM_KEYS, r)) for r in rows]

    def fail_unfinished_diagrams(self, reason: str) -> int:
        marks = ",".join("?" * len(_DIAGRAM_FINAL))
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE diagrams SET status='failed', error=?, updated_at=? WHERE status NOT IN ({marks})",
                (reason, time.time(), *_DIAGRAM_FINAL))
        return cur.rowcount

    # stories
    def add_story(self, node_id: str, n: int, kind: str, voice: str) -> str:
        if kind not in ("story", "listen", "chapter"):
            raise ValueError(f"unknown story kind {kind!r}")
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO stories(id, node_id, n, kind, title, status, error, voice, highlights_json, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, node_id, n, kind, "", "writing", None, voice, "[]", now, now))
        return sid

    def update_story(self, id: str, status: str | None = None, error=_KEEP, title: str | None = None,
                     highlights: list[str] | None = None) -> None:
        """Change a story's status, error, title and/or highlights. A failed story stays failed
        and a done story keeps its status (a late writer cannot revive or un-finish one); an
        error is kept unless given."""
        if status is not None and status not in _STORY_STATUSES:
            raise ValueError(f"unknown story status {status!r}")
        with self._lock, self._db:
            row = self._db.execute("SELECT status, error, title FROM stories WHERE id=?", (id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown story {id!r}")
            if row[0] in _STORY_FINAL:
                return
            self._db.execute(
                "UPDATE stories SET status=?, error=?, title=?, updated_at=? WHERE id=?",
                (status or row[0], row[1] if error is _KEEP else error,
                 row[2] if title is None else title, time.time(), id))
            if highlights is not None:
                self._db.execute("UPDATE stories SET highlights_json=? WHERE id=?",
                                 (json.dumps(highlights), id))

    def set_scenes(self, id: str, scenes: Sequence[dict]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM story_scenes WHERE story_id=?", (id,))
            self._db.executemany(
                "INSERT INTO story_scenes(story_id, idx, title, text, spoken, media_id, audio, chunks_json, "
                "status, error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(id, i, s["title"], s["text"], s.get("spoken", ""), None, None, "[]", "waiting", None)
                 for i, s in enumerate(scenes)])

    def update_scene(self, id: str, idx: int, **fields) -> None:
        """Change a scene's fields (title, text, spoken, media_id, audio, status, error, chunks).
        A done or failed scene keeps its status, and none is marked narrating or done once the
        story has failed."""
        bad = set(fields) - set(_SCENE_FIELDS) - {"chunks"}
        if bad:
            raise ValueError(f"unknown scene field {sorted(bad)[0]!r}")
        if fields.get("status") is not None and fields["status"] not in _SCENE_STATUSES:
            raise ValueError(f"unknown scene status {fields['status']!r}")
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT sc.status, st.status FROM story_scenes sc JOIN stories st ON st.id = sc.story_id "
                "WHERE sc.story_id=? AND sc.idx=?", (id, idx)).fetchone()
            if row is None:
                raise ValueError(f"unknown scene {id!r}/{idx}")
            new = fields.get("status")
            if new is not None and (row[0] in ("done", "failed")
                                    or (row[1] == "failed" and new in ("narrating", "done"))):
                fields = {k: v for k, v in fields.items()
                          if k not in ("status", "error", "chunks", "audio")}
            sets, vals = [], []
            for k, v in fields.items():
                sets.append("chunks_json=?" if k == "chunks" else f"{k}=?")
                vals.append(json.dumps(v) if k == "chunks" else v)
            if sets:
                self._db.execute(f"UPDATE story_scenes SET {', '.join(sets)} WHERE story_id=? AND idx=?",
                                 (*vals, id, idx))

    def get_story(self, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_STORY_COLS}, highlights_json FROM stories WHERE id=?", (id,)).fetchone()
            if row is None:
                return None
            scenes = self._db.execute(
                "SELECT idx, title, text, spoken, media_id, audio, chunks_json, status, error "
                "FROM story_scenes WHERE story_id=? ORDER BY idx", (id,)).fetchall()
        out = dict(zip(_STORY_KEYS, row))
        out["highlights"] = json.loads(row[-1])
        out["scenes"] = [dict(zip(_SCENE_KEYS, (*r[:6], json.loads(r[6]), *r[7:]))) for r in scenes]
        return out

    def stories_for(self, node_id: str) -> list[dict]:
        """The page's stories, newest first: those told about it, and the Tale chapters that
        drew on it (a chapter's story is about its first page only)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_STORY_COLS} FROM stories WHERE node_id=? OR id IN ("
                "SELECT c.story_id FROM tale_chapter_pages p JOIN tale_chapters c "
                "ON c.tale_id = p.tale_id AND c.n = p.n WHERE p.node_id=?) "
                "ORDER BY created_at DESC, rowid DESC", (node_id, node_id)).fetchall()
        return [dict(zip(_STORY_KEYS, r)) for r in rows]

    def story_states(self, ids: Sequence[str]) -> dict[str, dict]:
        """{story id: {title, status}} for these stories (unknown ids are left out), in one query."""
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, title, status FROM stories WHERE id IN ({','.join('?' * len(ids))})",
                ids).fetchall()
        return {i: {"title": t, "status": s} for i, t, s in rows}

    def fail_unfinished_stories(self, reason: str) -> int:
        """Fail every story not done or failed, and the scene being narrated in it (scenes not
        yet started stay waiting)."""
        marks = ",".join("?" * len(_STORY_FINAL))
        with self._lock, self._db:
            self._db.execute(
                "UPDATE story_scenes SET status='failed', error=? WHERE status='narrating' AND story_id IN "
                f"(SELECT id FROM stories WHERE status NOT IN ({marks}))", (reason, *_STORY_FINAL))
            cur = self._db.execute(
                f"UPDATE stories SET status='failed', error=?, updated_at=? WHERE status NOT IN ({marks})",
                (reason, time.time(), *_STORY_FINAL))
        return cur.rowcount

    # drafts (thinking ahead): keyed by (node, source mode); no edge, version or history entry
    def put_draft(self, node_id: str, source_mode: str, markdown: str, sources: Sequence[Source],
                  status: str, depth: str = "normal") -> None:
        """Insert or replace the draft for (node, mode). Sources keep everything but their text,
        like a version's."""
        if status not in _DRAFT_STATUSES:
            raise ValueError(f"unknown draft status {status!r}")
        src = json.dumps([{k: getattr(s, k) for k in _SOURCE_KEYS} for s in sources])
        with self._lock, self._db:
            self._db.execute(f"INSERT OR REPLACE INTO drafts({_DRAFT_COLS}) VALUES(?,?,?,?,?,?,?)",
                             (node_id, depth, source_mode, markdown, src, status, time.time()))

    def get_draft(self, node_id: str, source_mode: str) -> Draft | None:
        with self._lock:
            row = self._db.execute(f"SELECT {_DRAFT_COLS} FROM drafts WHERE node_id=? AND source_mode=?",
                                   (node_id, source_mode)).fetchone()
        return _draft(row) if row else None

    def drafts_for(self, node_id: str) -> list[Draft]:
        with self._lock:
            rows = self._db.execute(f"SELECT {_DRAFT_COLS} FROM drafts WHERE node_id=? ORDER BY rowid",
                                    (node_id,)).fetchall()
        return [_draft(r) for r in rows]

    def delete_draft(self, node_id: str, source_mode: str | None = None) -> int:
        """Delete the node's draft for one mode, or for every mode; returns how many."""
        with self._lock, self._db:
            if source_mode is None:
                cur = self._db.execute("DELETE FROM drafts WHERE node_id=?", (node_id,))
            else:
                cur = self._db.execute("DELETE FROM drafts WHERE node_id=? AND source_mode=?",
                                       (node_id, source_mode))
        return cur.rowcount

    def prune_drafts(self, keep: int) -> int:
        """Keep the newest `keep` finished drafts (one being written is never pruned)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM drafts WHERE status='done' AND rowid NOT IN (SELECT rowid FROM drafts "
                "WHERE status='done' ORDER BY created_at DESC, rowid DESC LIMIT ?)", (keep,))
        return cur.rowcount

    def clear_finished_drafts(self) -> int:
        """Delete every finished draft (the reader's profile changed); returns how many."""
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM drafts WHERE status='done'")
        return cur.rowcount

    def clear_unfinished_drafts(self) -> int:
        """At start: a draft left half-written by a previous run is never kept."""
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM drafts WHERE status != 'done'")
        return cur.rowcount

    # located places (only hits are cached)
    def cached_place(self, key: str):
        from .maps import Place
        with self._lock:
            row = self._db.execute(
                "SELECT name, lat, lon, title, source FROM place_cache WHERE key=?", (key,)).fetchone()
        return Place(name=row[0], lat=row[1], lon=row[2], title=row[3], source=row[4]) if row else None

    def cache_place(self, key: str, place) -> None:
        if place.lat is None or place.lon is None:
            return
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO place_cache VALUES(?,?,?,?,?,?,?)",
                             (key, place.name, place.lat, place.lon, place.title, place.source, time.time()))

    def forget_place(self, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM place_cache WHERE key=?", (key,))

    # reading signals (primer.reader.signals owns the meaning; these are the rows)
    def add_reader_event(self, at: float, kind: str, node_id: str | None, detail: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO reader_events(at, kind, node_id, detail) VALUES(?,?,?,?)",
                             (at, kind, node_id, detail))

    def reader_event_counts(self, kind: str) -> dict[str, int]:
        """{detail: count} over the events of one kind."""
        with self._lock:
            rows = self._db.execute("SELECT detail, COUNT(*) FROM reader_events WHERE kind=? "
                                    "GROUP BY detail", (kind,)).fetchall()
        return dict(rows)

    def reader_events_since(self, kind: str, at: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM reader_events WHERE kind=? AND at>?",
                                    (kind, at)).fetchone()[0]

    def add_dwell(self, node_id: str, seconds: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO reader_dwell(node_id, seconds) VALUES(?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET seconds = seconds + excluded.seconds",
                (node_id, seconds))

    def reader_topics(self, limit: int = 8) -> list[dict]:
        """The nodes opened most (then stayed on longest): [{title, opens, seconds}]."""
        with self._lock:
            rows = self._db.execute(
                "SELECT n.title, COALESCE(o.opens, 0) AS opens, COALESCE(d.seconds, 0.0) AS secs "
                "FROM nodes n "
                "LEFT JOIN (SELECT node_id, COUNT(*) AS opens FROM reader_events WHERE kind='open' "
                "           GROUP BY node_id) o ON o.node_id = n.id "
                "LEFT JOIN reader_dwell d ON d.node_id = n.id "
                "WHERE o.opens IS NOT NULL OR d.seconds IS NOT NULL "
                "ORDER BY opens DESC, secs DESC, n.rowid LIMIT ?", (limit,)).fetchall()
        return [{"title": t, "opens": o, "seconds": float(sec)} for t, o, sec in rows]

    def reader_recent_titles(self, limit: int = 12) -> list[str]:
        """Titles of the pages opened most recently (by open signals, so a reset forgets them)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT n.title FROM reader_events e JOIN nodes n ON n.id = e.node_id "
                "WHERE e.kind='open' GROUP BY n.id ORDER BY MAX(e.at) DESC, MAX(e.rowid) DESC "
                "LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    # the reader model (primer.reader.profile): summary, notes, enabled, generation, marks
    def reader_profile(self) -> dict[str, str]:
        with self._lock:
            return dict(self._db.execute("SELECT key, value FROM reader_profile").fetchall())

    def set_reader_profile(self, values: dict[str, str]) -> None:
        """Upsert several keys in one transaction."""
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO reader_profile(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, str(v)) for k, v in values.items()])

    def reset_reader(self, keep: dict[str, str]) -> None:
        """Delete every reading signal, dwell total and profile value, then store `keep`; one
        transaction. Pages are untouched."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM reader_events")
            self._db.execute("DELETE FROM reader_dwell")
            self._db.execute("DELETE FROM reader_profile")
            self._db.executemany("INSERT INTO reader_profile(key, value) VALUES(?,?)",
                                 [(k, str(v)) for k, v in keep.items()])

    def reset_all_reader(self, keep: dict[str, str]) -> None:
        """The reader reset, in one transaction: every reading signal, dwell total, profile
        value (then `keep` is stored), quiz item and answer, and quest. Pages are untouched."""
        with self._lock, self._db:
            for table in ("reader_events", "reader_dwell", "reader_profile", "quiz_answers",
                          "quiz_items", "quests"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.executemany("INSERT INTO reader_profile(key, value) VALUES(?,?)",
                                 [(k, str(v)) for k, v in keep.items()])

    # Test me and Review (primer.reader.quiz)
    def add_quiz_items(self, items: Sequence[dict], replace: tuple[str, int] | None = None) -> None:
        """Store a batch of quiz items in one transaction. `replace` = (node_id, version_n):
        first delete that version's items that were never answered (asked again, fresh)."""
        with self._lock, self._db:
            if replace is not None:
                self._db.execute("DELETE FROM quiz_items WHERE node_id=? AND version_n=? "
                                 "AND last_mark IS NULL", replace)
            self._db.executemany(
                f"INSERT INTO quiz_items({_QUIZ_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(i[k] for k in _QUIZ_KEYS) for i in items])

    def _quiz_rows(self, where: str, args: tuple, tail: str = "") -> list[dict]:
        rows = self._db.execute(
            f"SELECT {', '.join('q.' + k for k in _QUIZ_KEYS)}, COALESCE(n.title, '') "
            f"FROM quiz_items q LEFT JOIN nodes n ON n.id = q.node_id WHERE {where} {tail}",
            args).fetchall()
        return [{**dict(zip(_QUIZ_KEYS, r[:-1])), "title": r[-1]} for r in rows]

    def get_quiz_item(self, id: str) -> dict | None:
        """One item with its page title (and model answer), or None."""
        with self._lock:
            rows = self._quiz_rows("q.id=?", (id,))
        return rows[0] if rows else None

    def quiz_items_for(self, node_id: str, version_n: int | None = None) -> list[dict]:
        """A page's items, newest batch first (one version's when given), in asked order."""
        with self._lock:
            if version_n is None:
                return self._quiz_rows("q.node_id=?", (node_id,),
                                       "ORDER BY q.created_at DESC, q.rowid")
            return self._quiz_rows("q.node_id=? AND q.version_n=?", (node_id, version_n),
                                   "ORDER BY q.created_at DESC, q.rowid")

    def quiz_due(self, now: float, limit: int) -> list[dict]:
        with self._lock:
            return self._quiz_rows("q.due_at <= ?", (now,),
                                   f"ORDER BY q.due_at, q.rowid LIMIT {int(limit)}")

    def quiz_counts(self, now: float) -> dict:
        """{due, total, next_due_at}: next_due_at is the soonest due time after `now` (None
        when nothing is waiting)."""
        with self._lock:
            due, total, soonest = self._db.execute(
                "SELECT COALESCE(SUM(due_at <= ?), 0), COUNT(*), MIN(CASE WHEN due_at > ? "
                "THEN due_at END) FROM quiz_items", (now, now)).fetchone()
        return {"due": int(due), "total": int(total), "next_due_at": soonest}

    def mark_quiz_item(self, id: str, at: float, answer: str, mark: str, feedback: str,
                       due_at: float, step: int, misses: int) -> None:
        """Record an answer and the item's new schedule in one transaction."""
        with self._lock, self._db:
            self._db.execute("INSERT INTO quiz_answers(item_id, at, answer, mark, feedback) "
                             "VALUES(?,?,?,?,?)", (id, at, answer, mark, feedback))
            self._db.execute("UPDATE quiz_items SET due_at=?, step=?, last_mark=?, misses=? "
                             "WHERE id=?", (due_at, step, mark, misses, id))

    def quiz_answers(self, item_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT at, answer, mark, feedback FROM quiz_answers "
                                    "WHERE item_id=? ORDER BY rowid", (item_id,)).fetchall()
        return [dict(zip(("at", "answer", "mark", "feedback"), r)) for r in rows]

    def quiz_facts(self, step: int, misses: int, limit: int) -> tuple[list[str], list[str]]:
        """Page titles (most recently answered first, each once): items last marked right at
        `step` or above, and items missed `misses` times or more."""
        def titles(where: str, arg: int) -> list[str]:
            rows = self._db.execute(
                "SELECT n.title FROM quiz_items q JOIN nodes n ON n.id = q.node_id "
                "LEFT JOIN quiz_answers a ON a.item_id = q.id "
                f"WHERE {where} GROUP BY n.id ORDER BY MAX(a.rowid) DESC LIMIT ?",
                (arg, limit)).fetchall()
            return [r[0] for r in rows]
        with self._lock:
            return (titles("q.last_mark='right' AND q.step >= ?", step),
                    titles("q.misses >= ?", misses))

    def reset_quiz(self) -> None:
        """Delete every quiz item and answer (the reader reset); one transaction."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM quiz_answers")
            self._db.execute("DELETE FROM quiz_items")

    def titles_read_before(self, node_id: str, limit: int = 5) -> list[str]:
        """Titles of pages the reader arrived at before first arriving at this one (any page
        with a version, most recent first); all arrivals when this page has none."""
        with self._lock:
            first = self._db.execute("SELECT MIN(rowid) FROM edges WHERE to_id=?",
                                     (node_id,)).fetchone()[0]
            rows = self._db.execute(
                "SELECT n.title FROM edges e JOIN nodes n ON n.id = e.to_id "
                "WHERE e.to_id != ? AND e.rowid < ? "
                "AND EXISTS(SELECT 1 FROM versions v WHERE v.node_id = n.id) "
                "GROUP BY n.id ORDER BY MAX(e.rowid) DESC LIMIT ?",
                (node_id, first if first is not None else 2**62, limit)).fetchall()
        return [r[0] for r in rows]

    # the Quest (primer.reader.quest)
    def recent_open_pages(self, limit: int) -> list[str]:
        """Ids of pages with a version, most recently opened first (by open signals)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.node_id FROM reader_events e WHERE e.kind='open' "
                "AND EXISTS(SELECT 1 FROM versions v WHERE v.node_id = e.node_id) "
                "GROUP BY e.node_id ORDER BY MAX(e.at) DESC, MAX(e.rowid) DESC LIMIT ?",
                (limit,)).fetchall()
        return [r[0] for r in rows]

    def recent_arrival_pages(self, limit: int) -> list[str]:
        """Ids of pages with a version, most recently arrived at first (by edges)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.to_id FROM edges e "
                "WHERE EXISTS(SELECT 1 FROM versions v WHERE v.node_id = e.to_id) "
                "GROUP BY e.to_id ORDER BY MAX(e.rowid) DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def trail_parent(self, node_id: str) -> str | None:
        """The page the reader came to this one from: its parent (a context page), else where
        its first arrival came from; None at the start of a trail."""
        with self._lock:
            row = self._db.execute("SELECT parent_id FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is not None and row[0]:
                return row[0]
            row = self._db.execute("SELECT from_id FROM edges WHERE to_id=? ORDER BY rowid LIMIT 1",
                                   (node_id,)).fetchone()
        return row[0] if row else None

    _QUEST_KEYS = ("id", "created_at", "status", "rooms_json", "at_room", "tries", "error")

    def add_quest(self, id: str, created_at: float, status: str, rooms_json: str, at_room: int,
                  tries: int, abandon: bool = False) -> None:
        """Store a quest; `abandon` first marks every building or active quest abandoned (one
        transaction)."""
        with self._lock, self._db:
            if abandon:
                self._db.execute("UPDATE quests SET status='abandoned' "
                                 "WHERE status IN ('building', 'active')")
            self._db.execute("INSERT INTO quests(id, created_at, status, rooms_json, at_room, tries) "
                             "VALUES(?,?,?,?,?,?)", (id, created_at, status, rooms_json, at_room, tries))

    def update_quest(self, id: str, **fields) -> None:
        bad = set(fields) - set(self._QUEST_KEYS[2:])
        if bad:
            raise ValueError(f"unknown quest fields {sorted(bad)}")
        if not fields:
            return
        with self._lock, self._db:
            self._db.execute(f"UPDATE quests SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                             (*fields.values(), id))

    def get_quest(self, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self._QUEST_KEYS)} FROM quests WHERE id=?",
                                   (id,)).fetchone()
        return dict(zip(self._QUEST_KEYS, row)) if row else None

    def latest_quest(self) -> dict | None:
        """The newest quest, or None (also when the newest was abandoned: the reader left it)."""
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self._QUEST_KEYS)} FROM quests "
                                   "ORDER BY rowid DESC LIMIT 1").fetchone()
        quest = dict(zip(self._QUEST_KEYS, row)) if row else None
        return quest if quest is not None and quest["status"] != "abandoned" else None

    def fail_building_quests(self, reason: str) -> int:
        with self._lock, self._db:
            return self._db.execute("UPDATE quests SET status='failed', error=? "
                                    "WHERE status='building'", (reason,)).rowcount

    def reset_quests(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM quests")

    # the Tale (primer.tales)
    _TALE_KEYS = ("id", "created_at", "updated_at", "title", "seed", "bible_json", "status")
    _CHAPTER_KEYS = ("tale_id", "n", "story_id", "pages", "summary", "created_at", "bible_update")
    _BIBLE_UPDATES = ("pending", "done", "failed")

    def add_tale(self, seed: str, title: str, bible_json: str, at: float | None = None) -> str:
        """Start a tale and make it the active one; the previous active tale is archived."""
        tid = uuid.uuid4().hex[:16]
        now = time.time() if at is None else at
        with self._lock, self._db:
            self._db.execute("UPDATE tales SET status='archived' WHERE status='active'")
            self._db.execute("INSERT INTO tales(id, created_at, updated_at, title, seed, bible_json, "
                             "status) VALUES(?,?,?,?,?,?,?)",
                             (tid, now, now, title, seed, bible_json, "active"))
        return tid

    def get_tale(self, id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self._TALE_KEYS)} FROM tales WHERE id=?",
                                   (id,)).fetchone()
        return dict(zip(self._TALE_KEYS, row)) if row else None

    def active_tale(self) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self._TALE_KEYS)} FROM tales "
                                   "WHERE status='active' ORDER BY created_at DESC, rowid DESC "
                                   "LIMIT 1").fetchone()
        return dict(zip(self._TALE_KEYS, row)) if row else None

    def list_tales(self) -> list[dict]:
        """Every tale, newest first."""
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(self._TALE_KEYS)} FROM tales "
                                    "ORDER BY created_at DESC, rowid DESC").fetchall()
        return [dict(zip(self._TALE_KEYS, r)) for r in rows]

    def set_tale_bible(self, id: str, bible_json: str, title: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE tales SET bible_json=?, updated_at=? WHERE id=?",
                             (bible_json, time.time(), id))
            if title is not None:
                self._db.execute("UPDATE tales SET title=? WHERE id=?", (title, id))

    def set_active_tale(self, id: str) -> bool:
        """Make this tale the active one (the others are archived); False for an unknown id."""
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM tales WHERE id=?", (id,)).fetchone() is None:
                return False
            self._db.execute("UPDATE tales SET status='archived' WHERE status='active'")
            self._db.execute("UPDATE tales SET status='active', updated_at=? WHERE id=?",
                             (time.time(), id))
        return True

    def add_tale_chapter(self, tale_id: str, story_id: str, pages: Sequence[str], summary: str,
                         at: float | None = None) -> int:
        """Add the next chapter (numbered from 1) and return its number. Its bible update starts
        "pending" (set_chapter_update says how it ended)."""
        now = time.time() if at is None else at
        with self._lock, self._db:
            n = self._db.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM tale_chapters WHERE tale_id=?",
                                 (tale_id,)).fetchone()[0]
            self._db.execute("INSERT INTO tale_chapters(tale_id, n, story_id, pages_json, summary, "
                             "created_at, bible_update) VALUES(?,?,?,?,?,?,?)",
                             (tale_id, n, story_id, json.dumps(list(pages)), summary, now, "pending"))
            self._add_chapter_pages(tale_id, n, pages)
            self._db.execute("UPDATE tales SET updated_at=? WHERE id=?", (time.time(), tale_id))
        return n

    def _add_chapter_pages(self, tale_id: str, n: int, pages) -> None:
        """Index a chapter's pages (call with the lock held, in a transaction)."""
        self._db.executemany(
            "INSERT OR IGNORE INTO tale_chapter_pages(tale_id, n, pos, node_id) VALUES(?,?,?,?)",
            [(tale_id, n, i, p) for i, p in enumerate(pages if isinstance(pages, list) else list(pages))
             if isinstance(p, str)])

    def chapter_page_titles(self, tale_id: str) -> dict[int, list[dict]]:
        """{chapter n: [{node_id, title}]} in the order each chapter used them; a page since
        gone has the title ''. One query."""
        with self._lock:
            rows = self._db.execute(
                "SELECT p.n, p.node_id, COALESCE(nd.title, '') FROM tale_chapter_pages p "
                "LEFT JOIN nodes nd ON nd.id = p.node_id WHERE p.tale_id=? ORDER BY p.n, p.pos",
                (tale_id,)).fetchall()
        out: dict[int, list[dict]] = {}
        for n, nid, title in rows:
            out.setdefault(n, []).append({"node_id": nid, "title": title})
        return out

    def tale_chapter_counts(self) -> dict[str, int]:
        """{tale id: chapters} for every tale with at least one."""
        with self._lock:
            rows = self._db.execute(
                "SELECT tale_id, COUNT(*) FROM tale_chapters GROUP BY tale_id").fetchall()
        return dict(rows)

    def tale_chapters(self, tale_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT tale_id, n, story_id, pages_json, summary, created_at, bible_update "
                "FROM tale_chapters WHERE tale_id=? ORDER BY n", (tale_id,)).fetchall()
        return [dict(zip(self._CHAPTER_KEYS, (*r[:3], json.loads(r[3]), *r[4:]))) for r in rows]

    def set_chapter_update(self, tale_id: str, n: int, state: str) -> None:
        """How the chapter's bible update ended: "pending", "done" or "failed"."""
        if state not in self._BIBLE_UPDATES:
            raise ValueError(f"unknown bible update state {state!r}")
        with self._lock, self._db:
            self._db.execute("UPDATE tale_chapters SET bible_update=? WHERE tale_id=? AND n=?",
                             (state, tale_id, n))

    def chapter_for_story(self, story_id: str) -> dict | None:
        """{tale_id, n} of the chapter told by this story, or None."""
        with self._lock:
            row = self._db.execute("SELECT tale_id, n FROM tale_chapters WHERE story_id=?",
                                   (story_id,)).fetchone()
        return {"tale_id": row[0], "n": row[1]} if row else None

    def replace_chapter_story(self, old_id: str, new_id: str) -> None:
        """A chapter's story was told again (Story's Again): the chapter follows the new one."""
        with self._lock, self._db:
            self._db.execute("UPDATE tale_chapters SET story_id=? WHERE story_id=?", (new_id, old_id))

    def set_chapter_summary(self, tale_id: str, n: int, summary: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE tale_chapters SET summary=? WHERE tale_id=? AND n=?",
                             (summary, tale_id, n))

    def used_pages(self, tale_id: str) -> set[str]:
        """Node ids of every page an earlier chapter of this tale drew on."""
        with self._lock:
            rows = self._db.execute("SELECT pages_json FROM tale_chapters WHERE tale_id=?",
                                    (tale_id,)).fetchall()
        return {p for (raw,) in rows for p in json.loads(raw)}

    def pages_read_since(self, since: float) -> list[dict]:
        """Pages with a stored version opened after `since`: [{node_id, title, opens, seconds,
        last}] (unordered). By open signals; when there are none at all (the reader was reset),
        by arrivals along the trail."""
        with self._lock:
            have = self._db.execute("SELECT 1 FROM reader_events WHERE kind='open' LIMIT 1").fetchone()
            if have:
                rows = self._db.execute(
                    "SELECT n.id, n.title, COUNT(*), MAX(e.at), COALESCE(d.seconds, 0.0) "
                    "FROM reader_events e JOIN nodes n ON n.id = e.node_id "
                    "LEFT JOIN reader_dwell d ON d.node_id = n.id "
                    "WHERE e.kind='open' AND e.at>? "
                    "AND EXISTS(SELECT 1 FROM versions v WHERE v.node_id = n.id) "
                    "GROUP BY n.id", (since,)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT n.id, n.title, COUNT(*), MAX(e.at), COALESCE(d.seconds, 0.0) "
                    "FROM edges e JOIN nodes n ON n.id = e.to_id "
                    "LEFT JOIN reader_dwell d ON d.node_id = n.id "
                    "WHERE e.at>? AND EXISTS(SELECT 1 FROM versions v WHERE v.node_id = n.id) "
                    "GROUP BY n.id", (since,)).fetchall()
        return [{"node_id": i, "title": t, "opens": o, "last": a, "seconds": float(s)}
                for i, t, o, a, s in rows]

    # history and map
    def recent_titles(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT n.title FROM nodes n JOIN edges e ON e.to_id = n.id "
                "GROUP BY n.id ORDER BY MAX(e.at) DESC, MAX(e.rowid) DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def snapshot(self, visible_only: bool = False) -> dict:
        """All nodes and edges; visible_only leaves out nodes a draft made ahead of the reader."""
        with self._lock:
            where = ""
            if visible_only:  # a node nobody arrived at (no edge in) with no page, picture, map or story was made by a draft
                # (older databases may hold such nodes from before edges; they had nothing to show either)
                where = (" WHERE EXISTS(SELECT 1 FROM edges WHERE to_id=nodes.id)"
                         " OR EXISTS(SELECT 1 FROM versions WHERE node_id=nodes.id)"
                         " OR EXISTS(SELECT 1 FROM media WHERE node_id=nodes.id)"
                         " OR EXISTS(SELECT 1 FROM maps WHERE node_id=nodes.id)"
                         " OR EXISTS(SELECT 1 FROM stories WHERE node_id=nodes.id)")
            nodes = self._db.execute(f"SELECT {_NODE_COLS} FROM nodes{where} ORDER BY rowid").fetchall()
            edges = self._db.execute(
                "SELECT from_id, to_id, phrase, via, at FROM edges ORDER BY rowid").fetchall()
        return {
            "nodes": [dict(zip(("id", "kind", "concept", "title", "parent_id"), r)) for r in nodes],
            "edges": [dict(zip(("from_id", "to_id", "phrase", "via", "at"), r)) for r in edges],
        }
