"""SQLite concept graph: nodes, edges, page versions, highlights and branch cards."""
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
"""

_NODE_COLS = "id, kind, concept, title, parent_id"
_VERSION_COLS = "node_id, n, markdown, source_mode, depth, created_at"
_MEDIA_COLS = "id, node_id, phrase, kind, model_id, prompt, settings_json, status, file, error, created_at"


def _media(row) -> Media:
    return Media(*row[:6], json.loads(row[6]), *row[7:])


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
    def node_for(self, concept: str, kind: str, parent_id: str | None = None,
                 title: str | None = None) -> Node:
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
        node_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
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

    # history and map
    def recent_titles(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT n.title FROM nodes n JOIN edges e ON e.to_id = n.id "
                "GROUP BY n.id ORDER BY MAX(e.at) DESC, MAX(e.rowid) DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

    def snapshot(self) -> dict:
        with self._lock:
            nodes = self._db.execute(f"SELECT {_NODE_COLS} FROM nodes ORDER BY rowid").fetchall()
            edges = self._db.execute(
                "SELECT from_id, to_id, phrase, via, at FROM edges ORDER BY rowid").fetchall()
        return {
            "nodes": [dict(zip(("id", "kind", "concept", "title", "parent_id"), r)) for r in nodes],
            "edges": [dict(zip(("from_id", "to_id", "phrase", "via", "at"), r)) for r in edges],
        }
