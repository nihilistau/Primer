"""Image and video jobs: write the prompt, run it on a Primer-managed sd-server, save the file.

One worker thread runs one job at a time, taking turns with page writing through the GpuGate.
The sd-server stays up between jobs and is relaunched when a job needs another model."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import queue
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from ..config import EngineSpec, ImagesConfig
from ..gpu import GpuGate
from ..graph import Graph, Media
from ..prompts import sentence_around
from ..servers import ManagedServer
from .families import FAMILIES
from .models import (ImageModel, ModelStore, StoreCorrupt, animate_model, available_for, build_flags,
                     can_animate, effective_settings, kind_of, lora_entries, only_animates_reason,
                     validate_model)
from .prompting import image_prompt_messages, parse_image_prompt
from .sdclient import SdClient, SdError, decode_video, to_params, to_video_params

log = logging.getLogger(__name__)

POLL_S = 1.0          # job polling interval
GATE_POLL_S = 0.2     # while waiting for the GPU, how often a cancel is noticed
JOIN_S = 10.0         # shutdown waits this long for the worker
CANCEL_GRACE_S = 15.0  # a cancelled job sd-server doesn't stop by then: stop the server
# a status poll that errors (timeout, reset, 5xx) while sd-server is still running is retried;
# the job fails once this many polls in a row have failed AND this long has passed since the
# last good one (a slow reply during VAE decode or heavy offload must not lose a long job)
POLL_ERRORS = 5
POLL_ERROR_S = 60.0
PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PROMPT_CAP = 2000    # an edited prompt
_EXCERPT = 600
_FINAL = ("done", "failed", "cancelled")
NOT_CONFIGURED = "sd-server not configured in this tier"
NO_MODEL = "no image model set up"
NO_DEFAULT = "no default image model (choose one in Models)"
SHUT_DOWN = "Primer shut down"
SHUT_DOWN_REASON = "image jobs are shut down"
KINDS = ("image", "video")
ANIMATE_GONE = "the picture to animate is gone"
NO_ANIMATE_MODEL = "no video model that can animate a picture has been added"


class _Failed(Exception):
    """The job fails with this reason; stop_server=False when sd-server isn't to blame."""

    def __init__(self, reason: str, stop_server: bool = True):
        super().__init__(reason)
        self.stop_server = stop_server


class _Cancelled(Exception):
    pass


def _a(kind: str) -> str:
    return "an image" if kind == "image" else f"a {kind}"


_PREFIX_CAP = 400
_TAGS_PREFIX_CAP = 150  # a tags prompt is short: the look must leave room for the scene
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_prefix(prefix) -> str:
    """A prompt prefix as one plain line (angle brackets and control characters dropped),
    at most 400 characters."""
    if not isinstance(prefix, str):
        return ""
    text = _CONTROL_CHARS.sub(" ", prefix).replace("<", "").replace(">", "")
    return " ".join(text.split())[:_PREFIX_CAP].strip(" ,;")


def _cut_prefix(prefix: str, limit: int) -> str:
    if len(prefix) <= limit:
        return prefix
    cut = prefix[:limit + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else prefix[:limit]).rstrip(" ,;")


def _prefixed(prefix: str, prompt: str) -> str:
    return f"{prefix}, {prompt}"[:_PROMPT_CAP] if prefix else prompt


def _without_look(prefix: str, text: str) -> str:
    """The written prompt without the look it will be prefixed with: each entry of `prefix`
    ("Name: look; ...") and each look on its own, wherever the writer repeated it."""
    if not prefix or not text:
        return text
    parts = []
    for entry in prefix.split("; "):
        parts.append(entry)
        if ": " in entry:
            parts.append(entry.split(": ", 1)[1])
    out = text
    for part in sorted({p.strip(" ,;") for p in parts}, key=len, reverse=True):
        if len(part) >= 12:
            out = re.sub(re.escape(part), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s*([,;])(?:\s*[,;])+", r"\1", out)  # the separators left behind
    out = " ".join(out.split()).strip(" ,;")
    return out or text


def _default_server(spec: EngineSpec) -> ManagedServer:
    return ManagedServer("image", spec, health=SdClient(spec.base_url).health)


def decode_png(job: dict) -> bytes:
    """The first image of a completed sd-server job as PNG bytes; ValueError says what's wrong."""
    result = job.get("result") if isinstance(job, dict) else None
    images = result.get("images") if isinstance(result, dict) else None
    first = images[0] if isinstance(images, list) and images else None
    b64 = first.get("b64_json") if isinstance(first, dict) else None
    if not isinstance(b64, str) or not b64:
        raise ValueError("sd-server returned no image")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("sd-server returned an image that is not valid base64") from None
    if not data.startswith(PNG_SIG):
        raise ValueError("sd-server returned an image that is not a PNG")
    if len(data) == len(PNG_SIG):
        raise ValueError("sd-server returned an empty image")
    return data


def next_stage(live: dict, preview: dict) -> str:
    """The stage of a two-stage (Wan 2.2 A14B) job after a progress report. sd-server counts each
    stage on its own: high-noise 0/N..H/H, then low-noise 1/N..N/N. The job stays "high" until
    the high stage has reported a step >= 1 and then the step goes backwards or the total
    changes (a first 0/N report before the high stage begins is not a switch)."""
    step, total = preview.get("step"), preview.get("total_steps")
    if not isinstance(step, int) or live.get("step", 0) < 1:
        return "high"
    if step < live["step"] or (isinstance(total, int) and total != live.get("total")):
        return "low"
    return "high"


class ImageService:
    def __init__(self, graph: Graph, store: ModelStore, images: ImagesConfig | None, fast,
                 gate: GpuGate, media_dir: Path,
                 make_server: Callable[[EngineSpec], ManagedServer] = _default_server):
        self.graph = graph
        self.store = store
        self.images = images
        self.fast = fast
        self.gate = gate
        self.media_dir = Path(media_dir)
        self._make_server = make_server
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._prompting: set[str] = set()      # prompt thread still writing
        self._queued: set[str] = set()         # waiting for the worker (or the GPU)
        self._cancel_prompting: set[str] = set()
        self._busy = False                     # the worker is handling a job
        self._current: str | None = None       # the job holding the GPU
        self._holding = False                  # a job is inside its GPU turn
        self._cancel_current = False
        self._sd_job: str | None = None
        self._live: dict[str, dict] = {}       # id -> {"step", "total", "preview_b64"}
        self._server: ManagedServer | None = None
        self._server_key: tuple[str, tuple[str, ...]] | None = None  # (model id, flags)
        self._server_reason = ""               # why the last server went away
        self._closed = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="primer-images", daemon=True)
        self._worker.start()

    # public API
    @property
    def closed(self) -> bool:
        """shutdown() has been called: no new jobs are taken."""
        with self._lock:
            return self._closed

    def release_gpu(self) -> None:
        """Stop sd-server so the text servers can have the GPU back (auto GPU mode calls this
        before resuming them). A no-op while a job is inside its GPU turn; the next job
        relaunches the server, as on a model switch."""
        with self._lock:
            if self._holding:
                return
        self._stop_server("")

    def available(self, kind: str = "image") -> tuple[bool, str]:
        if self.images is None:
            return False, NOT_CONFIGURED
        if kind != "image":
            return available_for(self.store, kind)
        try:
            default = self.store.default_id()
            model = self.store.get(default) if default else None
            if model is None:
                return False, NO_DEFAULT if any(kind_of(m) == "image" for m in self.store.list()) else NO_MODEL
        except StoreCorrupt as e:
            return False, str(e)
        reasons = validate_model(model)
        return (False, reasons[0]) if reasons else (True, "")

    def create(self, node_id: str, phrase: str, subject: str, model_id: str | None = None,
               overrides: dict | None = None, prompt: str | None = None, kind: str = "image",
               animate_from: str | None = None, prefix: str = "", scene: bool = False) -> Media:
        """A job. `prefix` (a Tale's look for the hero and companions) starts the prompt,
        before the written or given prompt. A `scene` (of a Tale's chapter) is drawn as a
        storybook picture of the subject alone: the prompt writer gets no page excerpt."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        animating = animate_from is not None
        if animating and kind != "video":
            raise ValueError("only a video can animate a picture")
        if animating and model_id is None and self.images is not None:
            # never fall back to text-to-video: pick a model that takes a start image
            try:
                picked = animate_model(self.store)
            except StoreCorrupt as e:
                raise ValueError(str(e)) from e
            if picked is None:
                raise ValueError(NO_ANIMATE_MODEL)
            model_id = picked.id
        model = self._model_for(model_id, kind)
        source = None
        if animating:
            if not can_animate(model):
                raise ValueError(f"{model.name} can't animate a picture: choose a video model that can")
            source = self._animate_source(animate_from)
        elif FAMILIES[model.family].needs_init:
            raise ValueError(only_animates_reason(FAMILIES[model.family]))
        if overrides is not None and not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        settings = effective_settings(model, overrides)
        if settings.get("seed", -1) == -1:  # random: pick it here so the row records what ran
            settings["seed"] = secrets.randbelow(2**31 - 1)
        if source is not None:  # Again/Retry read it back to animate the same picture
            settings["animate_from"] = source.id
        subject = (subject or "").strip() if isinstance(subject, str) else ""
        given = prompt.strip()[:_PROMPT_CAP] if isinstance(prompt, str) else ""
        if not subject and not given:
            raise ValueError("subject is required")
        prefix = _clean_prefix(prefix)
        if prefix and FAMILIES[model.family].prompt_style == "tags":
            prefix = _cut_prefix(prefix, _TAGS_PREFIX_CAP)
        if given:
            given = _prefixed(prefix, given)
        if source is not None and not given:  # the prompt writer is told it animates a picture
            subject = (f"{subject} (this animates the given picture"
                       + (f", whose prompt was: {source.prompt[:_EXCERPT]}" if source.prompt else "")
                       + ")")
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN_REASON)
            media = self.graph.add_media(node_id, phrase or "", kind, model.id, given, settings)
            if given:
                media = self.graph.update_media(media.id, status="queued")
                self._queued.add(media.id)
                self._queue.put(media.id)
                return media
            self._prompting.add(media.id)
        threading.Thread(target=self._write_prompt, args=(media, model, subject, prefix, scene),
                         name="primer-image-prompt", daemon=True).start()
        return media

    def job(self, id: str) -> dict:
        media = self.graph.get_media(id)
        if media is None:
            raise KeyError(id)
        out = asdict(media)
        if media.kind == "video":  # what was made: known once the job is done
            out["fps"] = media.settings.get("result_fps")
            out["frames"] = media.settings.get("result_frames")
        with self._lock:
            live = dict(self._live.get(id) or {})
        if media.status == "generating" and live:
            out["progress"] = {"step": live.get("step", 0), "total": live.get("total", 0)}
            if live.get("stage"):  # two-stage video models: "high" then "low"
                out["progress"]["stage"] = live["stage"]
            if live.get("preview_b64"):
                out["preview_b64"] = live["preview_b64"]
        return out

    def cancel(self, id: str) -> bool:
        with self._lock:
            if id in self._prompting:
                self._cancel_prompting.add(id)  # ends "cancelled" when the prompt is written
                return True
            if id in self._queued:
                self._queued.discard(id)        # the worker skips it
                self._finish(id, "cancelled")
                return True
            if id != self._current or self._cancel_current:
                return False
            self._cancel_current = True
            sd_job, server = self._sd_job, self._server
        self._cancel_running(sd_job, server)
        return True

    def server_status(self) -> dict:
        if self.images is None:
            return {"state": "unavailable", "model_id": None, "model_name": None,
                    "reason": NOT_CONFIGURED}
        with self._lock:
            server, key, reason = self._server, self._server_key, self._server_reason
        if server is None:
            return {"state": "failed" if reason else "stopped", "model_id": None, "model_name": None, "reason": reason}
        st = server.status()
        mid = key[0] if key else None
        return {"state": st["state"], "model_id": mid, "model_name": self._model_name(mid),
                "reason": st["reason"]}

    def _model_name(self, model_id):
        """The model's display name, or its id when the store can't say."""
        if not model_id:
            return None
        try:
            model = self.store.get(model_id)
        except Exception:  # a corrupt store must not break the status poll
            return model_id
        return model.name if model else model_id

    def shutdown(self) -> None:
        """Stop without draining: the running job is cancelled, waiting ones fail, sd-server stops."""
        with self._lock:
            first = not self._closed
            self._closed = True
            pending = self._queued | self._prompting
            self._queued.clear()
            for mid in pending:
                self._finish(mid, "failed", SHUT_DOWN)
            running = self._current is not None and not self._cancel_current
            self._cancel_current = self._cancel_current or self._current is not None
            sd_job, server = self._sd_job, self._server
        if first:
            self._queue.put(None)
        self._stop.set()
        if running:
            self._cancel_running(sd_job, server)
        if self._worker is not threading.current_thread():
            self._worker.join(JOIN_S)
            if self._worker.is_alive():
                log.warning("image worker did not stop within %ss", JOIN_S)
        self._stop_server("")
        self.gate.release_idle()  # resume text servers paused for the cancelled job

    # internals
    def _model_for(self, model_id: str | None, kind: str = "image") -> ImageModel:
        if self.images is None:
            raise ValueError(NOT_CONFIGURED)
        if model_id is None:
            ok, reason = self.available(kind)
            if not ok:
                raise ValueError(reason)
            model_id = self.store.default_id(kind)
        try:
            model = self.store.get(model_id) if isinstance(model_id, str) else None
        except StoreCorrupt as e:
            raise ValueError(str(e)) from e
        if model is None:
            raise ValueError(f"unknown {kind} model {model_id!r}")
        if kind_of(model) != kind:
            raise ValueError(f"{model.name} is {_a(kind_of(model))} model")
        reasons = validate_model(model)
        if reasons:
            raise ValueError(reasons[0])
        return model

    def _animate_source(self, media_id) -> Media:
        """The finished image an Animate job starts from; ValueError says why it can't be used."""
        src = self.graph.get_media(media_id) if isinstance(media_id, str) and media_id else None
        if src is None:
            raise ValueError("unknown picture to animate")
        if src.kind != "image":
            raise ValueError("only a picture can be animated")
        if src.status != "done":
            raise ValueError("the picture to animate isn't finished")
        self._source_path(src)
        return src

    def _source_path(self, src: Media) -> Path:
        """The source image's file, which must be a bare name directly inside media_dir."""
        name = src.file
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(c in name for c in "/\\:\0")):
            raise ValueError(ANIMATE_GONE)
        root = self.media_dir.resolve()
        path = (self.media_dir / name).resolve()
        if path.parent != root or not path.is_file():
            raise ValueError(ANIMATE_GONE)
        return path

    def _init_b64(self, media: Media) -> str:
        """Base64 of the Animate source PNG, read now (it may have gone since the job was made)."""
        src = self.graph.get_media(media.settings.get("animate_from"))
        try:
            if src is None or src.kind != "image" or src.status != "done":
                raise ValueError(ANIMATE_GONE)
            data = self._source_path(src).read_bytes()
        except (ValueError, OSError) as e:
            raise _Failed(str(e) if isinstance(e, ValueError) else f"{ANIMATE_GONE}: {e}",
                          stop_server=False) from e
        if not data.startswith(PNG_SIG):
            raise _Failed("the picture to animate is not a PNG", stop_server=False)
        return base64.b64encode(data).decode()

    def _finish(self, mid: str, status: str, error: str | None = None, **fields) -> None:
        """Set a final status unless the row already has one. Call with self._lock held."""
        media = self.graph.get_media(mid)
        if media is None or media.status in _FINAL:
            return
        self.graph.update_media(mid, status=status, error=error, **fields)

    def _write_prompt(self, media: Media, model: ImageModel, subject: str, prefix: str = "",
                      scene: bool = False) -> None:
        """Prompt thread: the fast model writes the prompt, then the job is queued."""
        try:
            node = None if scene else self.graph.get_node(media.node_id)
            latest = None if scene else self.graph.latest_version(media.node_id)
            excerpt = None
            if latest:  # the sentence around the phrase, else the page's opening (headings skipped)
                body = "\n".join(line for line in latest.markdown.splitlines()
                                 if not line.lstrip().startswith("#")).strip()
                excerpt = sentence_around(body, media.phrase) or body[:_EXCERPT] or None
            told = subject
            if prefix:  # the prompt writer keeps to the look the prompt will start with
                told = (f"{subject}\n(The prompt will begin with how the people in it look: "
                        f"{prefix}. Keep to it, but do not repeat it: write only the scene, the "
                        "action and the setting.)")
            messages = image_prompt_messages(FAMILIES[model.family], told,
                                             node.title if node else None, excerpt, scene=scene)
            text = parse_image_prompt(self.fast.complete(messages, max_tokens=200, temperature=0.7),
                                      subject)
            text = _without_look(prefix, text)  # the room goes to the scene, not a second look
        except Exception:  # the fast model failing must not lose the job
            log.warning("image prompt writing failed; using the subject", exc_info=True)
            text = subject
        text = _prefixed(prefix, text)
        try:
            with self._lock:
                self._prompting.discard(media.id)
                if media.id in self._cancel_prompting:
                    self._cancel_prompting.discard(media.id)
                    self._finish(media.id, "cancelled", prompt=text)
                elif self._closed:
                    self._finish(media.id, "failed", SHUT_DOWN, prompt=text)
                else:
                    self.graph.update_media(media.id, prompt=text, status="queued")
                    self._queued.add(media.id)
                    self._queue.put(media.id)
        except Exception:
            log.exception("could not queue image job %s", media.id)
        finally:
            with self._lock:
                self._prompting.discard(media.id)
            self._release_if_idle()  # this job held off resuming the text; it won't run now

    def _release_if_idle(self) -> None:
        """Resume the text servers once no image job is running, queued or being prompted."""
        with self._lock:
            idle = self._queue.empty() and not self._prompting and not self._busy
        if idle:
            self.gate.release_idle()

    def _run(self) -> None:
        while True:
            mid = self._queue.get()
            if mid is None:
                self.gate.release_idle()
                return
            with self._lock:
                self._busy = True
            try:
                self._run_job(mid)
            except Exception:  # never let one job kill the worker
                log.exception("image job %s crashed", mid)
                reason = "internal error (see the Primer log)"
                try:
                    with self._lock:
                        self._finish(mid, "failed", reason)
                except Exception:
                    log.exception("could not mark image job %s failed", mid)
                self._stop_server(reason)
            finally:
                with self._lock:
                    self._live.pop(mid, None)
                    self._busy = False
                    if self._current == mid:
                        self._current, self._sd_job, self._cancel_current = None, None, False
            self._release_if_idle()

    def _run_job(self, mid: str) -> None:
        turn = self.gate.image()
        try:  # poll for the GPU so a cancel while waiting never pauses the text servers
            while True:
                with self._lock:
                    if mid not in self._queued:  # cancelled (or shut down) while waiting
                        turn.cancel()
                        return
                if turn.try_enter():
                    break
                self._stop.wait(GATE_POLL_S)
        except BaseException:
            turn.cancel()
            raise
        with turn:
            with self._lock:
                if mid not in self._queued:  # cancelled or shut down just now
                    return
                self._queued.discard(mid)
                self._current, self._cancel_current, self._sd_job = mid, False, None
                self._holding = True
            try:
                self._generate(mid)
            except _Cancelled:
                with self._lock:
                    self._finish(mid, "cancelled")
            except (_Failed, SdError, OSError) as e:
                reason = self._failure_reason(e)
                with self._lock:
                    self._finish(mid, "failed", reason)
                if not isinstance(e, _Failed) or e.stop_server:
                    self._stop_server(reason)  # the next job relaunches cleanly
            finally:
                with self._lock:
                    self._holding = False  # before the turn is given up: see release_gpu()

    def _failure_reason(self, e: Exception) -> str:
        with self._lock:
            server = self._server
        if isinstance(e, SdError) and server is not None:
            st = server.status()
            if st["state"] == "ready" and st["pid"] is None:
                code = server.proc.poll() if server.proc is not None else None
                return f"sd-server stopped while generating (exit code {code})"
        if isinstance(e, _Failed):
            return str(e)
        return f"sd-server error: {e}"

    def _check_cancel(self) -> None:
        with self._lock:
            if self._cancel_current:
                raise _Cancelled()

    def _generate(self, mid: str) -> None:
        media = self.graph.get_media(mid)
        try:
            model = self.store.get(media.model_id)
        except StoreCorrupt as e:
            raise _Failed(str(e)) from e
        if model is None:
            raise _Failed(f"{media.kind} model {media.model_id!r} no longer exists")
        if kind_of(model) != media.kind:
            raise _Failed(f"{model.name} is now {_a(kind_of(model))} model", stop_server=False)
        video = media.kind == "video"
        init_b64 = self._init_b64(media) if video and media.settings.get("animate_from") else None
        if init_b64 is not None and not can_animate(model):  # never a silent text-to-video
            raise _Failed(f"{model.name} can't animate a picture: choose a video model that can",
                          stop_server=False)
        if video and init_b64 is None and FAMILIES[model.family].needs_init:
            raise _Failed(only_animates_reason(FAMILIES[model.family]), stop_server=False)
        self._check_cancel()
        client = self._ensure_server(mid, model)
        self._check_cancel()
        if video:
            sd_job = client.submit_video(to_video_params(media.prompt, media.settings,
                                                         lora_entries(model), init_b64))
        else:
            sd_job = client.submit(to_params(media.prompt, media.settings))
        with self._lock:
            self._sd_job = sd_job
            cancelled = self._cancel_current
            self._live[mid] = {"step": 0, "total": media.settings.get("steps", 0)}
            two_stage = video and FAMILIES[model.family].two_stage
            if two_stage:
                self._live[mid]["stage"] = "high"
            self.graph.update_media(mid, status="generating")
        if cancelled:  # cancel() ran before the job id was known
            self._cancel_running(sd_job, None, client)
        stall = self.images.stall_timeout_s
        last_seen, last_change, cancel_at = None, time.monotonic(), None
        errors, last_ok = 0, time.monotonic()
        while True:
            try:
                job = client.job(sd_job)
            except SdError as e:
                if not self._server_alive():
                    raise  # _failure_reason reports the exit
                now = time.monotonic()
                errors += 1
                with self._lock:
                    cancelled = self._cancel_current
                if cancelled:
                    cancel_at = now if cancel_at is None else cancel_at
                    if self._stop.is_set() or now - cancel_at >= CANCEL_GRACE_S:
                        self._stop_server("")
                        raise _Cancelled() from None
                elif errors >= POLL_ERRORS and now - last_ok >= POLL_ERROR_S:
                    raise _Failed(f"sd-server stopped answering ({errors} status checks in a row "
                                  f"failed over {now - last_ok:.0f}s; last: {e})") from e
                log.warning("image job %s: status check failed (%d in a row), retrying: %s",
                            mid, errors, e)
                self._stop.wait(POLL_S)
                continue
            errors, last_ok = 0, time.monotonic()
            status = job.get("status")
            preview = job.get("preview") if isinstance(job.get("preview"), dict) else {}
            with self._lock:
                live = self._live.setdefault(mid, {})
                if two_stage and live.get("stage") == "high":
                    live["stage"] = next_stage(live, preview)
                if isinstance(preview.get("step"), int):
                    live["step"] = preview["step"]
                if isinstance(preview.get("total_steps"), int):
                    live["total"] = preview["total_steps"]
                if isinstance(preview.get("b64_json"), str):
                    live["preview_b64"] = preview["b64_json"]
                cancelled = self._cancel_current
                seen = (status, live.get("step"))
            now = time.monotonic()
            if seen != last_seen:
                last_seen, last_change = seen, now
            if status == "completed":
                if cancelled:
                    raise _Cancelled()
                break
            if status == "cancelled" or (cancelled and self._stop.is_set()):
                raise _Cancelled()
            if status == "failed":
                err = job.get("error")
                detail = (err.get("message") or err.get("code")) if isinstance(err, dict) else err
                raise _Failed(f"sd-server failed the job: {detail or 'no reason given'}")
            if cancelled:
                cancel_at = now if cancel_at is None else cancel_at
                if now - cancel_at >= CANCEL_GRACE_S:  # it ignored the cancel: stop it
                    self._stop_server("")
                    raise _Cancelled()
            elif now - last_change >= stall:
                raise _Failed(f"sd-server stopped making progress (no update for {stall:g}s)")
            self._stop.wait(POLL_S)
        if video:
            self._finish_video(mid, media, job)
            return
        try:
            data = decode_png(job)
        except ValueError as e:
            raise _Failed(str(e)) from e
        try:
            name = self._save(data)
        except OSError as e:  # our disk, not sd-server: keep the server up
            raise _Failed(f"could not save the image: {e}", stop_server=False) from e
        with self._lock:
            self._finish(mid, "done", None, file=name)

    def _finish_video(self, mid: str, media: Media, job: dict) -> None:
        try:
            data, info = decode_video(job)
        except ValueError as e:
            raise _Failed(f"sd-server returned a bad video: {e}") from e
        try:
            name = self._save(data, ".webm")
        except OSError as e:  # our disk, not sd-server: keep the server up
            raise _Failed(f"could not save the video: {e}", stop_server=False) from e

        def made(v, asked):  # the server's figure when it gives one, else what was asked for
            return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else asked

        settings = {**media.settings,
                    "result_fps": made(info.get("fps"), media.settings.get("fps")),
                    "result_frames": made(info.get("frame_count"), media.settings.get("frames"))}
        with self._lock:
            self._finish(mid, "done", None, file=name, settings=settings)

    def _server_alive(self) -> bool:
        """Is the sd-server process we launched still running?"""
        with self._lock:
            server = self._server
        return server is not None and server.status()["pid"] is not None

    def _ensure_server(self, mid: str, model: ImageModel) -> SdClient:
        """sd-server up for this model (relaunching for another model or changed flags)."""
        try:
            flags = tuple(build_flags(model))
        except ValueError as e:
            raise _Failed(str(e)) from e
        port = self.images.port
        base = f"http://127.0.0.1:{port}"
        key = (model.id, flags)
        with self._lock:
            server, current_key = self._server, self._server_key
        if server is not None and current_key == key:
            st = server.status()
            if st["state"] == "ready" and st["pid"] is not None:
                return SdClient(base)
        if server is not None:
            self._stop_server("")
        spec = EngineSpec(kind="sdserver", base_url=base, managed=True,
                          launch=(self.images.sd_server, "--listen-ip", "127.0.0.1",
                                  "--listen-port", str(port), *flags),
                          ready_timeout_s=self.images.ready_timeout_s, gpu=True)
        server = self._make_server(spec)
        with self._lock:
            self._server, self._server_key, self._server_reason = server, key, ""
            self.graph.update_media(mid, status="loading")
            cancelled = self._cancel_current
        if cancelled:
            raise _Cancelled()
        server.start(wait=True)
        st = server.status()
        if st["state"] == "ready":
            return SdClient(base)
        self._check_cancel()  # cancel() stops a loading server
        if st["state"] == "external":
            raise _Failed(f"another server is already answering on 127.0.0.1:{port} "
                          "(a leftover sd-server? Restart Primer or stop it)")
        raise _Failed(f"sd-server did not start: {st['reason'] or st['state']}")

    def _cancel_running(self, sd_job: str | None, server: ManagedServer | None,
                        client: SdClient | None = None) -> None:
        """Ask sd-server to cancel; while it is still loading, stop the load instead."""
        if sd_job is None:
            if server is not None and server.status()["state"] == "starting":
                server.stop()
            return
        try:
            (client or SdClient(f"http://127.0.0.1:{self.images.port}")).cancel(sd_job)
        except SdError as e:  # the poll loop reports what happened to the server
            log.warning("cancelling image job failed: %s", e)

    def _stop_server(self, reason: str) -> None:
        with self._lock:
            server = self._server
        if server is None:
            return
        try:
            server.stop()
        except Exception:
            log.exception("stopping sd-server failed")
        with self._lock:
            if self._server is server:
                self._server, self._server_key = None, None
                self._server_reason = reason

    def _save(self, data: bytes, ext: str = ".png") -> str:
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
