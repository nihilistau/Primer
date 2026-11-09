"""Story jobs: write a page as scenes, request their pictures, narrate them in order.

A Tale's chapter (kind "chapter") is a story whose scenes the Tale wrote: it skips the writing,
and each scene's picture prompt starts with the look of the hero and of the companions in it.

One worker thread runs jobs in order. Writing takes a page turn at the GPU gate (it uses the
text model); narration runs on the voice server, on the CPU, while pictures render."""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import tempfile
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from primer.graph import Graph
from primer.prompts import highlight_messages, parse_highlights
from primer.stories.prompts import parse_scenes, story_messages
from primer.voice import wav
from primer.voice.speech import chunks, speakable

log = logging.getLogger(__name__)

SHUT_DOWN = "Primer shut down"
NO_CHAPTER_SCENES = "this chapter has no scenes"  # Again on a chapter with none: a 409
KINDS = ("story", "listen")  # what create() makes; create_chapter() makes "chapter"
CHAPTER = "chapter"
GAP_S = 0.25  # silence between chunks
LISTEN_PART = 600  # speakable characters a Listen part holds, so the first plays early


def listen_parts(markdown: str) -> list[str]:
    """A page split at paragraph breaks into parts of about LISTEN_PART speakable characters (a
    longer paragraph is a part of its own); the parts joined by blank lines give the page back."""
    parts: list[list[str]] = []
    size = 0
    for para in re.split(r"\n[ \t]*\n", markdown.strip()):
        if not para.strip():
            continue
        n = len(speakable(para))
        if parts and size + n <= LISTEN_PART:
            parts[-1].append(para)
            size += n
        else:
            parts.append([para])
            size = n
    return ["\n\n".join(p) for p in parts] or [markdown]


def _scene_text(scene: dict) -> str:
    return f"{scene['title']}\n{scene['text']}"


class Closed(Exception):
    """The service was shut down part-way through narrating."""


class StoryService:
    def __init__(self, graph: Graph, text, fast, voice_factory: Callable[[], object], images, gate,
                 media_dir: Path, voice_name: str = "") -> None:
        self.graph = graph
        self.text = text
        self.fast = fast
        self.voice_factory = voice_factory
        self.images = images  # ImageService or None
        self.gate = gate  # GpuGate or None: writing the story takes a page turn
        self.media_dir = Path(media_dir)
        self.voice_name = voice_name
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._pending: set[str] = set()
        self._prefixes: dict[str, list[str]] = {}  # chapter story id -> each scene's picture prefix
        # (story_id, scene text) -> the picture prefix of a chapter told again (App wires
        # TaleService.prefix_for_story); None: no prefix
        self.chapter_prefix: Callable[[str, str], str] | None = None
        self._worker = threading.Thread(target=self._run, name="primer-stories", daemon=True)
        self._worker.start()

    def create(self, node_id: str, kind: str = "story") -> str:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        node = self.graph.get_node(node_id)
        version = self.graph.latest_version(node_id) if node else None
        if node is None or version is None:
            raise ValueError("unknown page")
        return self._enqueue(node_id, version.n, kind)

    def create_chapter(self, node_id: str, n: int, title: str, scenes: list[dict],
                       picture_prefix: str | Callable[[str], str] | list[str] = "") -> str:
        """A Tale's chapter told from the given scenes ({title, text}) about the page `node_id`
        (version `n`): stored at once, then highlighted, illustrated and narrated like a story.
        `picture_prefix` starts each scene's picture prompt: one string for all, a callable of
        the scene's title and text, or one string per scene."""
        if not isinstance(node_id, str) or self.graph.get_node(node_id) is None:
            raise ValueError("unknown page")
        scenes = [{"title": str(s.get("title") or title), "text": str(s.get("text") or "")}
                  for s in scenes or [] if isinstance(s, dict) and str(s.get("text") or "").strip()]
        if not scenes:
            raise ValueError("a chapter needs scenes")
        if callable(picture_prefix):
            prefixes = [picture_prefix(_scene_text(s)) or "" for s in scenes]
        elif isinstance(picture_prefix, (list, tuple)):
            prefixes = [str(p or "") for p in picture_prefix][:len(scenes)]
            prefixes += [""] * (len(scenes) - len(prefixes))
        else:
            prefixes = [str(picture_prefix or "")] * len(scenes)
        return self._enqueue(node_id, n, CHAPTER, scenes=scenes, title=title, prefixes=prefixes)

    def again(self, story_id: str) -> str:
        """Tell a story again. A chapter is told again from its stored scenes, its pictures
        prefixed through `chapter_prefix`, and its Tale chapter follows the new story."""
        old = self.graph.get_story(story_id)
        if old is None:
            raise ValueError("unknown story")
        if old["kind"] != CHAPTER:
            return self._enqueue(old["node_id"], old["n"], old["kind"])
        scenes = [{"title": sc["title"], "text": sc["text"]} for sc in old["scenes"]]
        if not scenes:
            raise ValueError(NO_CHAPTER_SCENES)
        hook = self.chapter_prefix
        prefixes = []
        for sc in scenes:
            try:
                prefixes.append((hook(story_id, _scene_text(sc)) or "") if hook else "")
            except Exception:
                log.exception("chapter picture prefix failed")
                prefixes.append("")
        return self._enqueue(old["node_id"], old["n"], CHAPTER, scenes=scenes, title=old["title"],
                             prefixes=prefixes, replaces=story_id)

    def get(self, story_id: str) -> dict | None:
        return self.graph.get_story(story_id)

    def shutdown(self, reason: str = SHUT_DOWN) -> None:
        """Stop without draining: stories not yet finished are marked failed with `reason`."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
            self._pending.clear()
        self._queue.put(None)
        for sid in pending:
            self._fail(sid, reason)
        self._worker.join(timeout=2)

    # internals
    def _enqueue(self, node_id: str, n: int, kind: str, scenes: list[dict] | None = None,
                 title: str = "", prefixes: list[str] | None = None,
                 replaces: str | None = None) -> str:
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN)
            sid = self.graph.add_story(node_id, n, kind, self.voice_name)
            if scenes is not None:  # a chapter: its scenes are kept from the start (Again reads them)
                self.graph.set_scenes(sid, [{**s, "spoken": speakable(s["text"])} for s in scenes])
                self.graph.update_story(sid, title=title)
                self._prefixes[sid] = list(prefixes or [])
            if replaces is not None:
                self.graph.replace_chapter_story(replaces, sid)
            self._pending.add(sid)
        self._queue.put(sid)
        return sid

    def _fail(self, sid: str, error: str) -> None:
        try:
            story = self.graph.get_story(sid)
            self.graph.update_story(sid, status="failed", error=error)
            for sc in (story or {}).get("scenes", []):
                if sc["status"] == "narrating":
                    self.graph.update_scene(sid, sc["idx"], status="failed", error=error)
        except Exception:
            log.exception("could not mark story %s failed", sid)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _run(self) -> None:
        while True:
            sid = self._queue.get()
            if sid is None or self._is_closed():
                return
            try:
                self._job(sid)
            except Exception as e:  # a job never kills the worker
                log.exception("story job %s failed", sid)
                self._fail(sid, f"Story failed: {e}")
            finally:
                with self._lock:
                    self._pending.discard(sid)
                    self._prefixes.pop(sid, None)

    def _job(self, sid: str) -> None:
        story = self.graph.get_story(sid)
        node = self.graph.get_node(story["node_id"])
        version = next((v for v in self.graph.versions(story["node_id"]) if v.n == story["n"]), None)
        if node is None or version is None:
            self._fail(sid, "The page is gone")
            return
        if story["kind"] == "listen":
            scenes = [{"title": "", "text": t} for t in listen_parts(version.markdown)]
        elif story["kind"] == CHAPTER:
            scenes = [{"title": sc["title"], "text": sc["text"]} for sc in story["scenes"]]
        else:
            scenes = self._write(sid, node, version)
            if scenes is None:
                return
        for s in scenes:
            s["spoken"] = speakable(s["text"])
        if self._is_closed():
            return
        self.graph.set_scenes(sid, scenes)
        title = story["title"] if story["kind"] == CHAPTER and story["title"] else node.title
        self.graph.update_story(sid, status="narrating", title=title)
        if story["kind"] in ("story", CHAPTER):
            self._highlight(sid, scenes)  # before the pictures: an image job pauses the fast server
            with self._lock:
                prefixes = self._prefixes.get(sid, [])
            self._illustrate(sid, node.id, scenes, prefixes, chapter=story["kind"] == CHAPTER)
        self._narrate(sid, scenes)

    def _write(self, sid: str, node, version) -> list[dict] | None:
        turn = self.gate.page() if self.gate else nullcontext()
        try:
            with turn:  # pages first: a queued turn goes before waiting images
                raw = "".join(self.text.stream(story_messages(node.title, version.markdown),
                                               max_tokens=1400, temperature=0.8))
        except Exception as e:
            self._fail(sid, f"Couldn't write the story: {e}")
            return None
        scenes = parse_scenes(raw, node.title)
        if not scenes:
            self._fail(sid, "The story came back empty")
            return None
        return scenes

    def _illustrate(self, sid: str, node_id: str, scenes: list[dict],
                    prefixes: list[str] | None = None, chapter: bool = False) -> None:
        """Ask for each scene's picture in order (a chapter's are storybook scenes whose prompts
        start with the scene's prefix); no image model or a refused request means no picture,
        never a failed story."""
        if self.images is None:
            return
        try:
            ok, _ = self.images.available("image")
        except Exception:
            log.exception("image availability check failed")
            return
        if not ok:
            return
        for i, s in enumerate(scenes):
            if self._is_closed():
                return
            try:
                prefix = prefixes[i] if prefixes and i < len(prefixes) else ""
                extra = {"scene": True} if chapter else {}
                if prefix:
                    extra["prefix"] = prefix
                media = self.images.create(node_id, s["title"], s["text"], **extra)
                self.graph.update_scene(sid, i, media_id=media.id)
            except Exception:
                log.warning("no picture for scene %d of story %s", i, sid, exc_info=True)

    def _highlight(self, sid: str, scenes: list[dict]) -> None:
        body = "\n\n".join(s["spoken"] for s in scenes)  # what the window shows
        try:
            raw = self.fast.complete(highlight_messages(body), max_tokens=300, temperature=0.2)
            self.graph.update_story(sid, highlights=parse_highlights(raw, body))
        except Exception:
            log.exception("story highlights failed")

    def _narrate(self, sid: str, scenes: list[dict]) -> None:
        try:
            voice = self.voice_factory()
        except Exception as e:
            reason = f"The voice server isn't available: {e}"
            self.graph.update_story(sid, status="failed", error=reason)
            return
        for i, s in enumerate(scenes):
            if self._is_closed():  # shutdown already marked the story failed
                return
            if not s["spoken"].strip():  # nothing to say (a lone citation): done, silent
                self.graph.update_scene(sid, i, status="done", chunks=[])
                continue
            self.graph.update_scene(sid, i, status="narrating")
            try:
                audio, ranges = self._speak(voice, s["spoken"])
            except Closed:  # shutdown already marked the story failed
                return
            except Exception as e:
                reason = f"Narration failed: {e}"
                self.graph.update_scene(sid, i, status="failed", error=reason)
                self.graph.update_story(sid, status="failed", error=reason)
                return
            if self._is_closed():
                return
            self.graph.update_scene(sid, i, status="done", audio=audio, chunks=ranges)
        self.graph.update_story(sid, status="done")

    def _speak(self, voice, spoken: str) -> tuple[str, list[list]]:
        """Narrate one scene: its audio file name and [start, end, t0, t1] per chunk, the ranges
        indexing `spoken` and the times in seconds from the start of the scene's audio (the
        silences between chunks counted)."""
        spans = chunks(spoken)
        if not spans:
            raise ValueError("nothing to say")
        parts = []
        for a, b in spans:
            if self._is_closed():
                raise Closed()
            parts.append(voice.speak(spoken[a:b]))
        data, seconds = wav.join(parts, GAP_S)
        name = self._save(data)
        out, t = [], 0.0
        for (a, b), sec in zip(spans, seconds):
            out.append([a, b, round(t, 3), round(t + sec, 3)])
            t += sec + GAP_S
        return name, out

    def _save(self, data: bytes, ext: str = ".wav") -> str:
        """Write the file as var/media/<sha256><ext> (atomically); identical bytes share a file."""
        name = hashlib.sha256(data).hexdigest() + ext
        self.media_dir.mkdir(parents=True, exist_ok=True)
        path = self.media_dir / name
        if path.is_file():
            return name
        fd, tmp = tempfile.mkstemp(dir=self.media_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return name
