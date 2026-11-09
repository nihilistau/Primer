"""The Tale: one ongoing story with a recurring hero. Starting a tale turns the reader's wish into
the first story bible; each next chapter is written from the bible and the pages read since the
last one, told as a story of kind "chapter" (illustrated with the hero's look, narrated), then
merged back into the bible by a second, strict update call.

Model calls use the writing model at the GPU gate's page turn; the engine, the gate and the
StoryService are read through the app at call time (a tier switch replaces them). One chapter
is written at a time: Next chapter is refused while one is being written or its bible update is
running (the next chapter continues from the updated bible). The flags are in memory only, so a
restart never leaves a tale stuck; a chapter row is added only once its scenes exist, and a
chapter whose update was cut off by a restart shows as failed and can be retried."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import nullcontext
from typing import Callable

from primer.engines import EngineError
from primer.stories.prompts import parse_scenes
from primer.stories.service import SHUT_DOWN
from primer.tales import prompts
from primer.tales.bible import (ENTRY_MAX, NAME_MAX, SEED_MAX, Bible, clean, parse_update)
from primer.tales.material import LIMIT, candidates, pick_material

log = logging.getLogger(__name__)

NO_TEXT = "no writing model in this tier"
NO_STORIES = "stories aren't available"
START_FAILED = "couldn't start the tale; try again"
CHAPTER_FAILED = "the chapter came back empty; try again"
NO_MATERIAL = "read a page or two first"
BUSY = "a chapter is being written"
NO_TALE = "no tale yet: start one first"
UNKNOWN_TALE = "tale not found"
UNKNOWN_CHAPTER = "chapter not found"
NOTHING_TO_RETRY = "this chapter's bible is up to date"
NO_SCENES = "the chapter has no scenes yet"
CHAPTER_TITLE_MAX = 80
MAX_PICKS = 20  # pages the reader can pick for one chapter (three are used)
MIN_SCENES = 3
_HERO_FIELDS = {"name": 60, "gender": 6, "description": 300, "look": 200}
_EDIT_FIELDS = set(_HERO_FIELDS) | {"remove_thread", "remove_place"}


class TaleError(Exception):
    """The model's output couldn't be used (after one retry): a 502 with the reason."""


class TaleUnavailable(Exception):
    """No writing model (or StoryService), or it failed: a 503 with the reason."""


class TaleConflict(Exception):
    """Not now: a chapter is being written, or there is nothing to retry (409)."""


class TaleNotFound(LookupError):
    pass


def _getter(value):
    """A zero-argument callable returning the current object: `value` itself when it is one,
    else a callable returning `value` (a fixed object, or None)."""
    return value if callable(value) else (lambda: value)


class TaleService:
    """Tales over a graph. `stories`, `text_engine` and `gate` are zero-argument callables
    returning the current StoryService, writing engine and GpuGate (or None); `profile_block`
    returns the reader's notes and leans ("" when off), quoted into prompts as wishes.
    `spawn(fn)` runs the bible update in the background (a thread by default). `voice`
    returns {ok, reason} like App.voice_available: a chapter is narrated, so Next chapter is
    refused (409) before any model call when the voice can't run (None: not checked)."""

    def __init__(self, graph, stories: Callable, text_engine: Callable, gate: Callable,
                 profile_block: Callable[[], str], *, clock=time.time, spawn=None,
                 voice: Callable[[], dict] | None = None):
        self.graph = graph
        self.stories = _getter(stories)
        self.text_engine = text_engine
        self.gate = _getter(gate)
        self.profile_block = profile_block
        self.clock = clock
        self._spawn = spawn
        self.voice = voice
        self._lock = threading.Lock()  # the flags below
        self._bible_lock = threading.Lock()  # read-modify-write of a tale's bible
        self._writing: str | None = None  # the tale whose next chapter is being written
        self._updating: set[tuple[str, int]] = set()  # (tale_id, n) bible updates running
        self._threads: list[threading.Thread] = []

    # --- reading ---
    def current(self) -> dict | None:
        tale = self.graph.active_tale()
        return self._view(tale) if tale else None

    def list(self) -> list[dict]:
        counts = self.graph.tale_chapter_counts()
        return [{"id": t["id"], "title": t["title"], "updated_at": t["updated_at"],
                 "chapters": counts.get(t["id"], 0)} for t in self.graph.list_tales()]

    def preview(self) -> dict:
        """The pages the next chapter would use and how many unused pages wait (the
        candidates are read once)."""
        tale = self.graph.active_tale()
        if tale is None:
            return {"pages": [], "waiting": 0}
        pages = candidates(self.graph, tale["id"])
        return {"pages": [{k: p[k] for k in ("node_id", "title", "seconds", "opens")}
                          for p in pages[:LIMIT]],
                "waiting": len(pages)}

    def state(self) -> dict:
        """GET /v1/tales: {current, tales, preview}."""
        return {"current": self.current(), "tales": self.list(), "preview": self.preview()}

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the background bible updates; True when none is running."""
        end = None if timeout is None else time.monotonic() + timeout
        for t in list(self._threads):
            t.join(None if end is None else max(0.0, end - time.monotonic()))
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            return not self._threads and not self._updating

    def prefix_for_story(self, story_id: str, text: str) -> str:
        """The picture prefix for a scene of the chapter told by `story_id` (Story's Again)."""
        found = self.graph.chapter_for_story(story_id)
        tale = self.graph.get_tale(found["tale_id"]) if found else None
        return Bible.from_json(tale["bible_json"]).look_prefix(text) if tale else ""

    # --- starting and choosing ---
    def start(self, seed) -> dict:
        """A new tale from the reader's wish (invented when empty); it becomes the current one.
        ValueError for a bad seed, TaleError when the model's bible can't be read twice."""
        if seed is None:
            seed = ""
        if not isinstance(seed, str) or len(seed) > SEED_MAX:
            raise ValueError(f"seed must be a string of at most {SEED_MAX} characters")
        seed = clean(seed, SEED_MAX)
        messages = prompts.start_messages(seed, self._reader())
        title, bible = self._ask(messages, prompts.START_TOKENS, prompts.START_TEMPERATURE,
                                 prompts.parse_start, START_FAILED)
        tid = self.graph.add_tale(seed, title, bible.to_json(), at=self.clock())
        return self._view(self.graph.get_tale(tid))

    def open(self, tale_id) -> dict:
        if not isinstance(tale_id, str) or not self.graph.set_active_tale(tale_id):
            raise TaleNotFound(UNKNOWN_TALE)
        return self._view(self.graph.get_tale(tale_id))

    def edit(self, tale_id, fields: dict) -> dict:
        """The reader's changes: the hero's name, gender, description and look; remove_thread and
        remove_place (the text of one). ValueError for anything else or a bad value."""
        tale = self._tale(tale_id)
        if not isinstance(fields, dict) or not fields:
            raise ValueError("nothing to change")
        unknown = set(fields) - _EDIT_FIELDS
        if unknown:
            raise ValueError(f"unknown field {sorted(unknown)[0]!r}")
        for key, limit in _HERO_FIELDS.items():
            if key in fields:
                value = fields[key]
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError(f"{key} must be a string of at most {limit} characters")
                if key == "name" and not clean(value, NAME_MAX):
                    raise ValueError("name can't be empty")
                if key == "gender" and value not in ("", "female", "male"):
                    raise ValueError("gender is female, male or empty")
        for key in ("remove_thread", "remove_place"):
            if key in fields and (not isinstance(fields[key], str) or len(fields[key]) > ENTRY_MAX
                                  or not fields[key].strip()):
                raise ValueError(f"{key} must be the text of one")
        with self._bible_lock:
            bible = Bible.from_json(self.graph.get_tale(tale["id"])["bible_json"])
            bible.edit({k: fields[k] for k in _HERO_FIELDS if k in fields})
            if "remove_thread" in fields and not bible.remove_thread(fields["remove_thread"]):
                raise ValueError("no such thread")
            if "remove_place" in fields and not bible.remove_place(fields["remove_place"]):
                raise ValueError("no such place")
            self.graph.set_tale_bible(tale["id"], bible.to_json())
        return self._view(self.graph.get_tale(tale["id"]))

    # --- chapters ---
    def next_chapter(self, chosen: list[str] | None = None) -> dict:
        """Write the current tale's next chapter (from the reader's `chosen` pages, or the best
        pages read since the last chapter) and start telling it: {story_id, n}. The bible
        update follows in the background."""
        if chosen is not None and (not isinstance(chosen, list) or len(chosen) > MAX_PICKS
                                   or not all(isinstance(c, str) for c in chosen)):
            raise ValueError(f"pages must be a list of at most {MAX_PICKS} page ids")
        if not chosen:  # nothing picked: the best pages read since the last chapter
            chosen = None
        tale = self.graph.active_tale()
        if tale is None:
            raise TaleNotFound(NO_TALE)
        tid = tale["id"]
        with self._lock:
            if self._writing is not None or any(t == tid for t, _ in self._updating):
                raise TaleConflict(BUSY)
            self._writing = tid
        try:
            return self._write_chapter(tid, chosen)
        finally:
            with self._lock:
                self._writing = None

    def retry_update(self, tale_id, n) -> dict:
        """Run a chapter's failed bible update again (in the background)."""
        tale = self._tale(tale_id)
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError("n must be a chapter number")
        chapter = next((c for c in self.graph.tale_chapters(tale["id"]) if c["n"] == n), None)
        if chapter is None:
            raise TaleNotFound(UNKNOWN_CHAPTER)
        story = self.graph.get_story(chapter["story_id"])
        scenes = [{"title": s["title"], "text": s["text"]} for s in (story or {}).get("scenes", [])]
        with self._lock:
            if chapter["bible_update"] == "done" or (tale["id"], n) in self._updating:
                raise TaleConflict(NOTHING_TO_RETRY)
            if self._writing == tale["id"]:
                raise TaleConflict(BUSY)
            if not scenes:
                raise TaleConflict(NO_SCENES)
            self._engine()  # 503 now rather than a failed update later
            self._updating.add((tale["id"], n))
        self._start_update(tale["id"], n, scenes)
        return self._view(self.graph.get_tale(tale["id"]))

    # --- internals ---
    def _write_chapter(self, tid: str, chosen) -> dict:
        started = self.clock()  # the chapter's boundary: pages opened from now on wait for the next
        self._engine()
        self._check_voice()  # before any GPU work: the chapter is narrated
        stories = self.stories()
        if stories is None:
            raise TaleUnavailable(NO_STORIES)
        material = pick_material(self.graph, tid, chosen=chosen)
        if not material:
            raise ValueError(NO_MATERIAL)
        tale = self.graph.get_tale(tid)
        bible = Bible.from_json(tale["bible_json"])
        n = len(self.graph.tale_chapters(tid)) + 1
        messages = prompts.chapter_messages(bible, n, material, self._reader(), tale["seed"])
        scenes = self._scenes(messages, n)
        main = material[0]["node_id"]
        version = self.graph.latest_version(main)
        title = clean(f"Chapter {n}: {scenes[0]['title']}", CHAPTER_TITLE_MAX)
        try:
            sid = stories.create_chapter(main, version.n if version else 1, title, scenes,
                                         bible.look_prefix)
        except ValueError as e:
            if str(e) == SHUT_DOWN:  # Primer shutting down or switching tiers
                raise TaleConflict(str(e)) from None
            raise
        # only now, with the scenes told, does the tale get the chapter; its update counts as
        # running from before the row exists, so the chapter never shows a failed update
        with self._lock:
            self._updating.add((tid, n))
        try:
            added = self.graph.add_tale_chapter(tid, sid, [m["node_id"] for m in material], "",
                                                at=started)
        except BaseException:
            with self._lock:
                self._updating.discard((tid, n))
            raise
        if added != n:  # (one writer at a time, so it is n)
            with self._lock:
                self._updating.discard((tid, n))
                self._updating.add((tid, added))
            n = added
        self._start_update(tid, n, scenes)
        return {"story_id": sid, "n": n}

    def _scenes(self, messages, n: int) -> list[dict]:
        """The chapter's scenes; fewer than three get one more try, and the longer answer is
        kept (a short chapter is still a chapter)."""
        def read(raw):
            return parse_scenes(raw, f"Chapter {n}") or None
        scenes = self._ask(messages, prompts.CHAPTER_TOKENS, prompts.CHAPTER_TEMPERATURE, read,
                           CHAPTER_FAILED, retry=False)
        if len(scenes) >= MIN_SCENES:
            return scenes
        log.warning("chapter %d came back with %d scene(s); asking once more", n, len(scenes))
        try:
            again = self._ask(messages, prompts.CHAPTER_TOKENS, prompts.CHAPTER_TEMPERATURE, read,
                              CHAPTER_FAILED, retry=False)
        except (TaleError, TaleUnavailable) as e:
            log.warning("the second try failed (%s); keeping the short chapter", e)
            return scenes
        if len(again) < len(scenes):
            log.warning("keeping chapter %d's first answer (%d scene(s))", n, len(scenes))
            return scenes
        return again

    def _start_update(self, tid: str, n: int, scenes: list[dict]) -> None:
        if self._spawn is not None:
            self._spawn(lambda: self._update(tid, n, scenes))
            return
        t = threading.Thread(target=self._update, args=(tid, n, scenes), name="tale-update",
                             daemon=True)
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    def _update(self, tid: str, n: int, scenes: list[dict]) -> None:
        """The bible update for chapter `n`: merged on success; on failure the chapter stays
        and says its update failed."""
        try:
            tale = self.graph.get_tale(tid)
            bible = Bible.from_json(tale["bible_json"]) if tale else Bible()
            update = None
            seed = tale["seed"] if tale else ""
            try:
                update = self._ask(prompts.update_messages(bible, n, scenes, seed), prompts.UPDATE_TOKENS,
                                   prompts.UPDATE_TEMPERATURE, parse_update, "no usable update")
            except (TaleError, TaleUnavailable) as e:
                log.warning("bible update for chapter %d of tale %s failed: %s", n, tid, e)
            with self._bible_lock:
                chapters = self.graph.tale_chapters(tid)
                chapter = next((c for c in chapters if c["n"] == n), None)
                if chapter is None or chapter["bible_update"] == "done":
                    return  # gone, or a retry got there first
                if update is None:
                    self.graph.set_chapter_update(tid, n, "failed")
                    return
                bible = Bible.from_json(self.graph.get_tale(tid)["bible_json"])
                bible.apply_update(update, n)  # the summary is kept under its chapter's number
                summary = next(c["text"] for c in bible.chapters if c["n"] == n)
                self._save_summary(tid, n, bible, summary)
        except Exception:
            log.exception("bible update for chapter %d of tale %s failed", n, tid)
            try:
                self.graph.set_chapter_update(tid, n, "failed")
            except Exception:
                log.exception("could not mark the update failed")
        finally:
            with self._lock:
                self._updating.discard((tid, n))

    def _save_summary(self, tid: str, n: int, bible: Bible, summary: str) -> None:
        """The one place a chapter's summary is written: the bible (holding it in its chapters
        list) and the chapter row together, and the update marked done."""
        self.graph.set_tale_bible(tid, bible.to_json())
        self.graph.set_chapter_summary(tid, n, summary)
        self.graph.set_chapter_update(tid, n, "done")

    def _reader(self) -> str:
        try:
            return self.profile_block() or ""
        except Exception:
            log.exception("the reader's notes couldn't be read")
            return ""

    def _check_voice(self) -> None:
        if self.voice is None:
            return
        status = self.voice()
        if not status.get("ok"):
            raise TaleConflict(status.get("reason") or "the voice isn't available")

    def _engine(self):
        engine = self.text_engine()
        if engine is None or not hasattr(engine, "complete"):
            raise TaleUnavailable(NO_TEXT)
        return engine

    def _ask(self, messages, max_tokens: int, temperature: float, read, error: str, retry=True):
        """The writing model's reply at the gate, read by `read` (None = unusable); one retry."""
        for _ in range(2 if retry else 1):
            engine = self._engine()  # read for each call: a tier switch may have replaced it
            gate = self.gate()
            try:
                with gate.page() if gate is not None else nullcontext():
                    raw = engine.complete(messages, max_tokens=max_tokens, temperature=temperature)
            except EngineError as e:
                raise TaleUnavailable(str(e) or "the writing model failed") from None
            value = read(raw if isinstance(raw, str) else "")
            if value is not None:
                return value
            log.warning("unusable tale output from the model: %r", (raw or "")[:200])
        raise TaleError(error)

    def _tale(self, tale_id) -> dict:
        tale = self.graph.get_tale(tale_id) if isinstance(tale_id, str) else None
        if tale is None:
            raise TaleNotFound(UNKNOWN_TALE)
        return tale

    def _view(self, tale: dict) -> dict:
        with self._lock:
            writing = self._writing == tale["id"]
            updating = {n for t, n in self._updating if t == tale["id"]}
        chapters = []
        rows = self.graph.tale_chapters(tale["id"])
        stories = self.graph.story_states([c["story_id"] for c in rows])  # one query each
        titles = self.graph.chapter_page_titles(tale["id"])
        for c in rows:
            story = stories.get(c["story_id"])
            pages = titles.get(c["n"], [])
            chapters.append({"n": c["n"], "story_id": c["story_id"],
                             "title": story["title"] if story else f"Chapter {c['n']}",
                             "status": story["status"] if story else "failed", "pages": pages,
                             "summary": c["summary"],
                             "update_failed": c["bible_update"] != "done" and c["n"] not in updating})
        bible = json.loads(Bible.from_json(tale["bible_json"]).to_json())
        return {"id": tale["id"], "title": tale["title"], "seed": tale["seed"], "bible": bible,
                "chapters": chapters, "writing": writing, "updating": bool(updating)}
