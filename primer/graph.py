"""SQLite concept graph: nodes, edges, page versions, highlights, branch cards, media, maps,
stories and drafts (pages written ahead, invisible until opened)."""
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
"""

_NODE_COLS = "id, kind, concept, title, parent_id"
_VERSION_COLS = "node_id, n, markdown, source_mode, depth, created_at"
_DRAFT_COLS = "node_id, depth, source_mode, markdown, sources_json, status, created_at"
_DRAFT_STATUSES = ("writing", "done")
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

    # stories
    def add_story(self, node_id: str, n: int, kind: str, voice: str) -> str:
        if kind not in ("story", "listen"):
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
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_STORY_COLS} FROM stories WHERE node_id=? ORDER BY created_at DESC, rowid DESC",
                (node_id,)).fetchall()
        return [dict(zip(_STORY_KEYS, r)) for r in rows]

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
