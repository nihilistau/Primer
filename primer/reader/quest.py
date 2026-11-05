"""The Quest: a MindMaze-style run through rooms built from the reader's trail.

Each room is one page: a one-sentence description of the page as a room (the writing model at
the GPU gate) and a door question from Quiz.door. A right answer opens the door; partly or
not_yet gives the door's hint (first miss), then a nudge to reread the page; after the third
miss the room may be skipped. The last door opens onto a summary of the run.

Building runs in a background thread, one at a time; a new quest abandons the current one.
Quests are stored in the graph (`quests`); the reader reset deletes them (an idempotent
Profile.on_reset hook), and a build in flight across a reset stores nothing."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Callable

from primer.engines import EngineError
from primer.prompts import clean_room, room_messages
from primer.reader.quiz import QuizError, QuizNotFound, QuizUnavailable, check_answer

log = logging.getLogger(__name__)

ROOMS = 5
MIN_ROOMS = 2
MAX_ROOMS = 8
ROOM_CHARS = 160
ROOM_TOKENS = 120
ROOM_TEMPERATURE = 0.3
ROOM_ATTEMPTS = 2  # a room whose description or door fails twice is dropped
SKIP_AFTER = 3  # misses before a room may be skipped

NEED_PAGES = "read at least two pages first"
TOO_FEW_ROOMS = "too few rooms could be made (a quest needs at least two rooms); try again"
INTERRUPTED = "the quest was interrupted while it was being built; start another"
REREAD = "Reread the page: the answer is in it."
NO_TEXT = "no writing model in this tier"
UNKNOWN_PAGE = "page not found"
UNKNOWN_QUEST = "quest not found"
BUILDING = "a quest is being built; wait for it"
NOT_PLAYING = "this quest isn't being played"
MOVED_ON = "the quest moved on meanwhile; try again"
NO_SKIP = f"a room can be skipped after {SKIP_AFTER} tries"
WAS_RESET = "the reader was reset meanwhile; try again"

_PUBLIC_ROOM = ("node_id", "title", "description", "question", "opened", "skipped", "tries")


class QuestError(Exception):
    """The quest can't be built (too few pages read): a 400 with the reason."""


class QuestNotFound(LookupError):
    pass


class QuestConflict(Exception):
    """Not now: a quest is being built, this one isn't being played, or a skip isn't allowed."""


class QuestUnavailable(Exception):
    """No writing model; the message says why."""


def _rooms_arg(rooms) -> int:
    if isinstance(rooms, bool) or not isinstance(rooms, int) or not MIN_ROOMS <= rooms <= MAX_ROOMS:
        raise ValueError(f"rooms must be a whole number from {MIN_ROOMS} to {MAX_ROOMS}")
    return rooms


def public_room(room: dict, index: int, at_room: int) -> dict:
    """A room as the API shows it, unfolding as the quest goes: a room after the current one is
    only {index, locked: true}; the current one shows its door, plus the door's hint once it
    has been missed; a room passed (opened or skipped) also shows the model answer."""
    if index > at_room:
        return {"index": index, "locked": True}
    out = {"index": index, "locked": False, **{k: room[k] for k in _PUBLIC_ROOM}}
    if room["opened"] or room["skipped"]:
        out["answer"], out["hint"] = room["answer"], room["hint"]
    elif room["tries"] >= 1:
        out["hint"] = room["hint"]
    return out


def summary(rooms: list[dict]) -> dict:
    return {"opened": sum(r["opened"] for r in rooms), "skipped": sum(r["skipped"] for r in rooms),
            "rooms": len(rooms)}


class Quests:
    """Quests over a graph. `quiz` writes and marks the doors (Quiz.door / Quiz.judge).
    `text_engine` is a zero-argument callable returning the writing engine (or None) and `gate`
    a GpuGate, a callable returning one, or None: both are read for each model call. `clock`
    stamps quests.

    Lock order: the GPU gate (inside a model call) is never taken while the Quests lock is
    held; the lock only guards the generation, the builder slot and read-modify-write of rows."""

    def __init__(self, graph, quiz, text_engine: Callable, gate, *, clock=time.time):
        self.graph = graph
        self.quiz = quiz
        self.text_engine = text_engine
        self._gate = gate
        self.clock = clock
        self._lock = threading.Lock()
        self._generation = 0  # bumped by reset(): a build started before it stores nothing
        self._building: str | None = None  # the quest id being built
        self._progress: dict[str, tuple[int, int]] = {}
        self._thread: threading.Thread | None = None
        graph.fail_building_quests(INTERRUPTED)  # a build cut off by a restart never finishes

    # building
    def build(self, from_node_id: str | None = None, rooms: int = ROOMS) -> dict:
        """Start building a quest from the pages read most recently (or `from_node_id`'s trail)
        in the background and return it (status "building"). It abandons the current quest.
        ValueError for a bad `rooms`, QuestNotFound for an unknown page, QuestError when fewer
        than two pages can be rooms, QuestUnavailable with no writing model, QuestConflict
        while another quest is being built."""
        rooms = _rooms_arg(rooms)
        if from_node_id is not None and (not isinstance(from_node_id, str)
                                         or self.graph.get_node(from_node_id) is None):
            raise QuestNotFound(UNKNOWN_PAGE)
        engine = self.text_engine()
        if engine is None or not hasattr(engine, "complete"):
            raise QuestUnavailable(NO_TEXT)
        with self._lock:
            if self._building is not None:
                raise QuestConflict(BUILDING)
            pages = self._pages(from_node_id, rooms)
            if len(pages) < MIN_ROOMS:
                raise QuestError(NEED_PAGES)
            quest_id = uuid.uuid4().hex
            self.graph.add_quest(quest_id, self.clock(), "building", "[]", 0, 0, abandon=True)
            self._building = quest_id
            self._progress[quest_id] = (0, len(pages))
            generation = self._generation
            started = self.get(quest_id)  # as it starts (the thread may move on at once)
            self._thread = threading.Thread(target=self._build, args=(quest_id, pages, generation),
                                            name="quest-build", daemon=True)
            self._thread.start()
        return started

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the build in progress (if any); True when none is running."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _pages(self, from_node_id: str | None, limit: int) -> list[str]:
        """Page ids for the rooms, oldest first along the trail."""
        if from_node_id is not None:
            trail, seen, node = [], set(), from_node_id
            while node is not None and node not in seen and len(trail) < limit:
                seen.add(node)
                if self.graph.latest_version(node) is not None:
                    trail.append(node)
                node = self.graph.trail_parent(node)
            return trail[::-1]
        recent = self.graph.recent_open_pages(limit)
        if len(recent) < MIN_ROOMS:  # no reading signals (an older database, or after a reset)
            recent = self.graph.recent_arrival_pages(limit)
        return recent[::-1]

    def _build(self, quest_id: str, pages: list[str], generation: int) -> None:
        try:
            rooms, error = [], None
            for i, node_id in enumerate(pages):
                with self._lock:
                    if generation != self._generation:
                        return  # reset meanwhile: stop before the next model call
                try:
                    room = self._room(node_id)
                except QuestUnavailable as e:
                    error = str(e)
                    break
                if room is not None:
                    rooms.append(room)
                with self._lock:
                    if generation != self._generation:
                        return
                    self._progress[quest_id] = (i + 1, len(pages))
            if error is None and len(rooms) < MIN_ROOMS:
                error = TOO_FEW_ROOMS
            with self._lock:
                if generation != self._generation:
                    return  # reset meanwhile: the quest is gone; store nothing
                if error is not None:
                    self.graph.update_quest(quest_id, status="failed", error=error)
                else:
                    self.graph.update_quest(quest_id, status="active", rooms_json=json.dumps(rooms),
                                            at_room=0, tries=0)
        except Exception:
            log.exception("building a quest failed")
            with self._lock:
                if generation == self._generation:
                    self.graph.update_quest(quest_id, status="failed",
                                            error="the quest couldn't be built; try again")
        finally:
            with self._lock:
                self._progress.pop(quest_id, None)
                if self._building == quest_id:
                    self._building = None

    def _room(self, node_id: str) -> dict | None:
        """One room, or None when its description or door failed twice. QuestUnavailable when
        the writing model can't be reached."""
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id)
        if node is None or version is None:
            return None
        description = door = None
        for _ in range(ROOM_ATTEMPTS):
            if description is None:
                description = self._describe(node.title, version.markdown)
                if description is None:
                    continue
            try:
                door = self.quiz.door(node_id)
                latest = self.graph.latest_version(node_id)
                if latest is not None and latest.n != version.n:
                    # the page was rewritten while the door was asked: ask the new version once
                    version = latest
                    door = self.quiz.door(node_id)
                    version = self.graph.latest_version(node_id) or version
            except QuizUnavailable as e:
                raise QuestUnavailable(str(e)) from None
            except (QuizError, QuizNotFound) as e:
                log.warning("a quest door for %s failed: %s", node_id, e)
                door = None
                continue
            break
        if description is None or door is None:
            return None
        return {"node_id": node_id, "title": node.title, "version_n": version.n,
                "description": description, "question": door["question"],
                "answer": door["answer"], "hint": door["hint"], "opened": False,
                "skipped": False, "tries": 0}

    def _describe(self, title: str, markdown: str) -> str | None:
        engine = self.text_engine()  # read for each call: a tier switch may have replaced it
        if engine is None or not hasattr(engine, "complete"):
            raise QuestUnavailable(NO_TEXT)
        gate = self.gate()
        try:
            with gate.page() if gate is not None else nullcontext():
                raw = engine.complete(room_messages(title, markdown), max_tokens=ROOM_TOKENS,
                                      temperature=ROOM_TEMPERATURE)
        except EngineError as e:
            raise QuestUnavailable(str(e) or "the writing model failed") from None
        text = clean_room(raw if isinstance(raw, str) else "")
        if not text:
            log.warning("unusable room description from the model: %r", (raw or "")[:200])
        return text or None

    # playing
    def answer(self, quest_id: str, answer) -> dict:
        """Try the current room's door: {mark, opened, finished, can_skip, tries, quest} plus
        `feedback` and `answer` (the model answer) when it opens, `hint` when it doesn't (no
        feedback then: it tends to quote the answer and spoil the door) and `summary` when
        the quest is finished. ValueError for a bad answer, QuestNotFound,
        QuestConflict when the quest isn't being played (or moved on meanwhile); QuizError /
        QuizUnavailable from marking."""
        answer = check_answer(answer)
        quest, rooms = self._playing(quest_id)
        at, generation = quest["at_room"], self._generation
        room = rooms[at]
        result = self.quiz.judge(room["question"], room["answer"], self._markdown(room), answer)
        with self._lock:
            if generation != self._generation:
                raise QuestConflict(WAS_RESET)
            quest, rooms = self._playing(quest_id)
            if quest["at_room"] != at:
                raise QuestConflict(MOVED_ON)
            room = rooms[at]
            out = {"mark": result["mark"]}
            if result["mark"] == "right":
                room["opened"] = True
                out["feedback"], out["answer"] = result["feedback"], room["answer"]
                finished = self._advance(quest_id, rooms, at)
            else:
                room["tries"] += 1
                out["hint"] = room["hint"] if room["tries"] == 1 else REREAD
                finished = False
                self.graph.update_quest(quest_id, rooms_json=json.dumps(rooms), tries=room["tries"])
            out.update(opened=room["opened"], finished=finished, tries=room["tries"],
                       can_skip=not room["opened"] and room["tries"] >= SKIP_AFTER)
            if finished:
                out["summary"] = summary(rooms)
        out["quest"] = self.get(quest_id)
        return out

    def skip(self, quest_id: str) -> dict:
        """Skip the current room after its third miss: {skipped, answer, hint, finished,
        quest} (+ summary). QuestConflict when it can't be skipped yet."""
        with self._lock:
            quest, rooms = self._playing(quest_id)
            at = quest["at_room"]
            room = rooms[at]
            if room["tries"] < SKIP_AFTER:
                raise QuestConflict(NO_SKIP)
            room["skipped"] = True
            finished = self._advance(quest_id, rooms, at)
            out = {"skipped": True, "answer": room["answer"], "hint": room["hint"],
                   "finished": finished}
            if finished:
                out["summary"] = summary(rooms)
        out["quest"] = self.get(quest_id)
        return out

    def abandon(self, quest_id: str) -> dict:
        """Leave the quest being played: it is marked abandoned and a new one may be built.
        {quest: None}. QuestNotFound, QuestConflict when it isn't being played."""
        with self._lock:
            self._playing(quest_id)
            self.graph.update_quest(quest_id, status="abandoned")
        return {"quest": self.current()}

    def _advance(self, quest_id: str, rooms: list[dict], at: int) -> bool:
        finished = at + 1 >= len(rooms)
        self.graph.update_quest(quest_id, rooms_json=json.dumps(rooms), at_room=at + 1, tries=0,
                                **({"status": "finished"} if finished else {}))
        return finished

    def _playing(self, quest_id: str) -> tuple[dict, list[dict]]:
        quest = self.graph.get_quest(quest_id) if isinstance(quest_id, str) else None
        if quest is None:
            raise QuestNotFound(UNKNOWN_QUEST)
        if quest["status"] != "active":
            raise QuestConflict(NOT_PLAYING)
        return quest, json.loads(quest["rooms_json"])

    def _markdown(self, room: dict) -> str:
        for v in self.graph.versions(room["node_id"]):
            if v.n == room.get("version_n"):
                return v.markdown
        latest = self.graph.latest_version(room["node_id"])
        return latest.markdown if latest is not None else ""

    # reading
    def get(self, quest_id: str) -> dict:
        quest = self.graph.get_quest(quest_id) if isinstance(quest_id, str) else None
        if quest is None:
            raise QuestNotFound(UNKNOWN_QUEST)
        return self._public(quest)

    def current(self) -> dict | None:
        """The quest being built or played, else the last one finished (or failed); None."""
        quest = self.graph.latest_quest()
        return self._public(quest) if quest is not None else None

    def _public(self, quest: dict) -> dict:
        rooms = json.loads(quest["rooms_json"])
        at = quest["at_room"]
        out = {"id": quest["id"], "created_at": quest["created_at"], "status": quest["status"],
               "at_room": at, "tries": quest["tries"],
               "rooms": [public_room(r, i, at) for i, r in enumerate(rooms)],
               "can_skip": quest["status"] == "active" and quest["tries"] >= SKIP_AFTER}
        if quest["status"] == "building":
            done, total = self._progress.get(quest["id"], (0, 0))
            out["building"] = {"done": done, "total": total}
        if quest["status"] == "failed":
            out["error"] = quest["error"] or TOO_FEW_ROOMS
        if quest["status"] == "finished":
            out["summary"] = summary(rooms)
        return out

    def forget(self) -> None:
        """The reader reset's in-memory part (a Profile.on_reset hook; idempotent): a build in
        flight stores nothing when it returns, and a new one may start at once. The rows go in
        the reset's own transaction (Graph.reset_all_reader)."""
        with self._lock:
            self._generation += 1
            self._building = None
            self._progress.clear()

    def reset(self) -> None:
        """Forget, and delete every quest (on its own, outside a reader reset)."""
        with self._lock:
            self._generation += 1
            self._building = None
            self._progress.clear()
            self.graph.reset_quests()

    def gate(self):
        gate = self._gate
        if gate is not None and not hasattr(gate, "page") and callable(gate):
            gate = gate()
        return gate
