"""Stdlib HTTP server: JSON routes, SSE page streaming and the built UI."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from primer.config import (TIERS_DIR, ConfigError, Tier, list_tiers, load_tier, read_settings,
                           write_settings)
from primer.engines import make_engine
from primer.gpu import GpuGate
from primer.graph import clean_typed
from primer.hardware import Hardware, suggest_tier
from primer.images.families import FAMILIES
from primer.images.models import (NETWORK_PATH, ImageModel, ModelStore, StoreCorrupt, effective_settings,
                                  is_network_or_device, validate_model)
from primer.images.prompting import match_image_request
from primer.images.scan import scan_folder_capped
from primer.images.service import NOT_CONFIGURED, SHUT_DOWN_REASON, ImageService
from primer.pages import OpenRequest, PageService
from primer.servers import ManagedServer, ServerManager

log = logging.getLogger(__name__)

SSE_WRITE_TIMEOUT_S = 30
MAX_BODY = 1_000_000
DRAIN_TIMEOUT_S = 2.0  # a refused request's body is read and dropped for at most this long
VIAS = ("start", "typed", "context", "general", "sideways")
DEPTHS = ("normal", "deeper", "simpler")
UI_NOT_BUILT = {"error": "UI not built: run npm run build in ui/"}
# GET routes that run a model or write, so they need a header a cross-site <img> or form can't
# send (the UI sends it on every request; a fetch that adds it gets a preflight, which fails)
PRIMER_HEADER = "X-Primer"
NEEDS_PRIMER_HEADER = ("/v1/branches",)
MEDIA_NAME = re.compile(r"[0-9a-f]{64}\.png")
MAX_SCAN_FILES = 2000
MODEL_FIELDS = ("id", "name", "family", "files", "settings")  # all a client may set on a model
RESUME_POLL_S = 0.1


class TextPause:
    """The GPU gate's pause_text/resume_text: stop the text and fast servers marked gpu = true
    while an image runs, then start again the ones that were running; before that, a resume
    with servers to restart has the images give the GPU back (release_gpu stops sd-server). The gate only pauses and
    resumes in auto mode, so in manual mode sd-server stays resident. Once retire() returns
    (the servers are about to be stopped for good) no start can begin, and any load a resume
    had in flight has been stopped.

    A server whose spec has a pause command (LM Studio: `lms unload --all`) is paused by
    running it instead, also when it was already running before Primer ("external"); it keeps
    serving, so a resume has nothing to start and the model loads again on the next request."""

    def __init__(self, servers: ServerManager):
        self._servers = servers
        self._lock = threading.Lock()
        self._paused: list[ManagedServer] = []
        self._starting: list[ManagedServer] = []  # started by a resume still waiting for them
        self._retired = threading.Event()
        self.release_gpu: Callable[[], None] | None = None  # set to ImageService.release_gpu

    def pause(self) -> None:
        for s in self._servers.gpu_servers():
            state = s.status()["state"]
            unload = bool(s.spec.pause)
            if state in ("ready", "starting") or (unload and state == "external"):
                with self._lock:
                    if s not in self._paused:
                        self._paused.append(s)
                if unload:
                    s.pause()
                else:
                    s.stop()

    def resume(self) -> None:
        with self._lock:
            paused, self._paused = self._paused, []
        if paused and self.release_gpu is not None:
            # sd-server out of VRAM before the text models load; with nothing to restart it
            # stays resident (a reload can take minutes)
            self.release_gpu()
        started = []
        for s in paused:
            if s.spec.pause and s.status()["state"] in ("ready", "external"):
                continue  # still serving: the next request loads the model again
            with self._lock:  # retire() can't slip in between this check and the start
                if self._retired.is_set():
                    return
                s.start(wait=False)
                self._starting.append(s)
            started.append(s)
        try:
            for s in started:  # a page must not start against a server still loading
                while s.status()["state"] == "starting":
                    if self._retired.wait(RESUME_POLL_S):
                        return
        finally:
            with self._lock:
                self._starting = [s for s in self._starting if s not in started]

    def retire(self) -> None:
        with self._lock:
            self._retired.set()
            starting, self._starting = self._starting, []
        for s in starting:
            s.stop()


@dataclass
class App:
    tier: Tier
    servers: ServerManager
    pages: PageService
    graph: object
    hardware: Hardware
    var_dir: Path
    ui_dir: Path
    tiers_dir: Path = TIERS_DIR
    store: ModelStore | None = None
    gate: GpuGate | None = None
    images: ImageService | None = None
    text_pause: TextPause | None = None
    make_sd_server: Callable | None = None  # tests: stands in for launching sd-server
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Fill in what wasn't passed: the model store, a gate over these servers (also given
        to the pages when they have none) and the image service."""
        if self.store is None:
            self.store = ModelStore(self.var_dir)
        if self.gate is None:
            self.text_pause, self.gate = make_gate(self.tier, self.servers)
            if self.pages.gate is None:
                self.pages.gate = self.gate
        if self.images is None:
            self.images = self.make_images(self.tier, self.gate, self.pages.fast)
        if self.text_pause is not None:
            self.text_pause.release_gpu = self.images.release_gpu

    def make_images(self, tier: Tier, gate: GpuGate, fast) -> ImageService:
        extra = {"make_server": self.make_sd_server} if self.make_sd_server else {}
        return ImageService(self.graph, self.store, tier.images, fast, gate,
                            self.var_dir / "media", **extra)

    def shutdown_images(self) -> None:
        """Stop image jobs and sd-server without restarting text servers that are about to stop."""
        if self.text_pause is not None:
            self.text_pause.retire()
        self.images.shutdown()

    def reload_tier(self, name: str) -> None:
        """Switch tiers. Raises ConfigError, changing nothing, if the tier is unknown or invalid."""
        tier = load_tier(name, self.tiers_dir)
        with self._lock:
            self.shutdown_images()
            self.servers.stop_all()
            self.pages.shutdown(drain=False)  # queued prefetch is for the old tier's servers
            servers = ServerManager(tier)
            text_pause, gate = make_gate(tier, servers)
            fast = make_engine(tier.fast)
            pages = PageService(self.graph, make_engine(tier.text), fast, tier.source_mode, gate)
            images = self.make_images(tier, gate, fast)
            text_pause.release_gpu = images.release_gpu
            self.tier, self.pages, self.servers = tier, pages, servers
            self.text_pause, self.gate, self.images = text_pause, gate, images
            try:
                settings = read_settings(self.var_dir)
            except ConfigError:
                settings = {}
            try:
                write_settings(self.var_dir, {**settings, "tier": name})
            finally:
                servers.start_all(wait=False)

    def server_action(self, role: str, action: str) -> dict:
        """Start or stop one model server. Raises ValueError for an unknown role. Holds the
        lock so it can't act on a ServerManager that a concurrent tier switch discards."""
        with self._lock:
            getattr(self.servers, action)(role)
            return next(s for s in self.servers.statuses() if s["role"] == role)

    def status(self) -> dict:
        return {"tier": self.tier.name, "tiers": list_tiers(self.tiers_dir),
                "suggested_tier": suggest_tier(self.hardware),
                "hardware": asdict(self.hardware), "servers": self.servers.statuses(),
                "source_mode": self.tier.source_mode, "gpu_mode": self.tier.gpu_mode,
                "images": self.images.server_status(), "image_available": self.image_available(),
                "gpu": self.gate.state()}

    def image_available(self) -> dict:
        ok, reason = self.images.available()
        return {"ok": ok, "reason": reason}


def make_gate(tier: Tier, servers: ServerManager) -> tuple[TextPause, GpuGate]:
    text_pause = TextPause(servers)
    return text_pause, GpuGate(tier.gpu_mode, text_pause.pause, text_pause.resume)


class _BadRequest(Exception):
    pass


class _HttpError(Exception):
    """A JSON {error} response with this status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        # responses
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True  # the body can't be skipped: don't read on
                raise _BadRequest("bad Content-Length") from None
            if length > MAX_BODY:
                self.close_connection = True
                raise _BadRequest("body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise _BadRequest("body is not valid JSON") from None
            if not isinstance(data, dict):
                raise _BadRequest("body must be a JSON object")
            return data

        def _drain(self) -> None:
            """Read and drop the request body before refusing. Closing a socket with unread
            data makes Windows send an RST, which can destroy the response before the client
            reads it (http.client sends headers and body in separate send() calls)."""
            try:
                left = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if not 0 < left <= MAX_BODY:
                return
            try:
                self.connection.settimeout(DRAIN_TIMEOUT_S)
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:  # includes the timeout: the connection is closed anyway
                pass

        # localhost-only: a foreign Host is DNS rebinding, a foreign Origin or a cross-site
        # Sec-Fetch-Site is another page, a non-JSON POST is a form or no-cors fetch that skipped
        # the CORS preflight, and a GET that does model work needs a header <img> can't send
        def _refused(self, post: bool) -> bool:
            port = self.server.server_address[1]
            local = (f"127.0.0.1:{port}", f"localhost:{port}")
            path = urlsplit(self.path).path
            origin = self.headers.get("Origin")
            site = self.headers.get("Sec-Fetch-Site")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if (self.headers.get("Host") or "").lower() not in local:
                status, error = 403, "forbidden host"
            elif path.startswith("/v1/") and site is not None and site.lower() not in ("same-origin", "none"):
                status, error = 403, "forbidden cross-site request"
            elif path in NEEDS_PRIMER_HEADER and self.headers.get(PRIMER_HEADER) != "1":
                status, error = 403, f"{PRIMER_HEADER} header required"
            elif (post or path.startswith("/v1/")) and origin is not None and \
                    origin.lower() not in ["http://" + h for h in local]:
                status, error = 403, "forbidden origin"
            elif post and ctype != "application/json":
                status, error = 415, "Content-Type must be application/json"
            else:
                return False
            self._drain()
            self.close_connection = True
            self._json({"error": error}, status)
            return True

        # routing
        def do_GET(self) -> None:
            if self._refused(post=False):
                return
            url = urlsplit(self.path)
            path = url.path
            try:
                if path == "/health":
                    self._json({"ok": True})
                elif path == "/v1/status":
                    self._json(app.status())
                elif path == "/v1/graph":
                    self._json(app.graph.snapshot())
                elif path == "/v1/branches":
                    q = parse_qs(url.query)
                    node, phrase = q.get("node", [""])[0], q.get("phrase", [""])[0]
                    if not node or not phrase:
                        self._json({"error": "node and phrase are required"}, 400)
                    else:
                        self._json({"cards": [asdict(c) for c in app.pages.branches(node, phrase)]})
                elif path.startswith("/v1/pages/"):
                    page = app.pages.page(unquote(path[len("/v1/pages/"):]))
                    if page is None:
                        self._json({"error": "not found"}, 404)
                    else:
                        node = page["node"]
                        self._json({"node": asdict(node), "version": asdict(page["version"]),
                                    "versions": page["versions"], "highlights": page["highlights"],
                                    "media": [asdict(m) for m in app.graph.media_for_node(node.id)]})
                elif path == "/v1/image-models":
                    self._image_models()
                elif path.startswith("/v1/images/") and "/" not in path[len("/v1/images/"):]:
                    try:
                        self._json(app.images.job(unquote(path[len("/v1/images/"):])))
                    except KeyError:
                        self._json({"error": "not found"}, 404)
                elif path.startswith("/v1/media/"):
                    self._media(path[len("/v1/media/"):])
                elif path.startswith("/v1/"):
                    self._json({"error": "not found"}, 404)
                else:
                    self._static(path)
            except StoreCorrupt as e:
                self._json({"error": str(e)}, 500)
            except Exception:
                log.exception("GET %s failed", path)
                self._json({"error": "internal error"}, 500)

        def do_POST(self) -> None:
            if self._refused(post=True):
                return
            path = urlsplit(self.path).path
            try:
                body = self._body()
                if path == "/v1/tier":
                    self._set_tier(body)
                elif path == "/v1/pages/open":
                    self._open(body)
                elif path.startswith("/v1/servers/"):
                    self._server_action(path[len("/v1/servers/"):])
                elif path == "/v1/intent":
                    text = body.get("text")
                    if not isinstance(text, str):
                        raise _BadRequest("text must be a string")
                    self._json({"image": match_image_request(text)})
                elif path == "/v1/images":
                    self._create_image(body)
                elif path.startswith("/v1/images/") and path.endswith("/cancel"):
                    mid = unquote(path[len("/v1/images/"):-len("/cancel")])
                    if app.graph.get_media(mid) is None:
                        raise _HttpError(404, "not found")
                    self._json({"cancelled": app.images.cancel(mid)})
                elif path == "/v1/image-models":
                    self._save_model(body)
                elif path in ("/v1/image-models/delete", "/v1/image-models/default"):
                    mid = body.get("id")
                    if not isinstance(mid, str) or not mid:
                        raise _BadRequest("id is required")
                    try:
                        (app.store.delete if path.endswith("/delete") else app.store.set_default)(mid)
                    except KeyError:
                        raise _HttpError(404, f"unknown image model {mid!r}") from None
                    self._json({"ok": True})
                elif path == "/v1/image-models/scan":
                    self._scan(body)
                else:
                    self._json({"error": "not found"}, 404)
            except _BadRequest as e:
                self._json({"error": str(e)}, 400)
            except _HttpError as e:
                self._json({"error": str(e)}, e.status)
            except StoreCorrupt as e:
                self._json({"error": str(e)}, 500)
            except Exception:
                log.exception("POST %s failed", path)
                self._json({"error": "internal error"}, 500)

        def _set_tier(self, body: dict) -> None:
            name = body.get("name")
            if not isinstance(name, str) or not name:
                raise _BadRequest("name is required")
            try:
                app.reload_tier(name)
            except ConfigError as e:
                raise _BadRequest(str(e)) from None
            self._json(app.status())

        def _server_action(self, rest: str) -> None:
            role, _, action = rest.partition("/")
            if action not in ("start", "stop"):
                self._json({"error": "not found"}, 404)
                return
            try:
                result = app.server_action(role, action)
            except ValueError as e:
                self._json({"error": str(e)}, 404)
                return
            self._json(result)

        def _open_request(self, body: dict) -> OpenRequest:
            node_id = body.get("node_id")
            # with node_id set, concept and via are ignored, so they may be left out
            via, depth = body.get("via", "start" if node_id else None), body.get("depth", "normal")
            if via not in VIAS:
                raise _BadRequest(f"via must be one of {VIAS}")
            if depth not in DEPTHS:
                raise _BadRequest(f"depth must be one of {DEPTHS}")
            concept, phrase = body.get("concept", ""), body.get("phrase", "")
            from_id = body.get("from_id")
            if not isinstance(concept, str) or not isinstance(phrase, str):
                raise _BadRequest("concept and phrase must be strings")
            if any(v is not None and not isinstance(v, str) for v in (from_id, node_id)):
                raise _BadRequest("from_id and node_id must be strings")
            return OpenRequest(concept=concept, via=via, from_id=from_id, phrase=phrase,
                               depth=depth, regenerate=bool(body.get("regenerate", False)),
                               node_id=node_id)

        def _open(self, body: dict) -> None:
            req = self._open_request(body)
            self.close_connection = True
            self.connection.settimeout(SSE_WRITE_TIMEOUT_S)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            alive = True

            def emit(kind: str, data: dict) -> None:
                nonlocal alive
                if not alive:
                    return
                try:
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except OSError:  # disconnect or stalled client: stop writing, keep generating
                    alive = False

            try:
                for kind, data in app.pages.open(req):
                    emit(kind, data)
            except Exception:
                log.exception("page open failed")
                emit("error", {"message": "internal error"})

        # images
        def _image_models(self) -> None:
            families = {fid: {"label": f.label, "slots": [asdict(s) for s in f.slots],
                              "defaults": f.defaults} for fid, f in FAMILIES.items()}
            self._json({"models": [{**asdict(m), "problems": validate_model(m)} for m in app.store.list()],
                        "default": app.store.default_id(), "families": families,
                        "available": app.image_available()})

        def _save_model(self, body: dict) -> None:
            raw = body.get("model")
            if not isinstance(raw, dict):
                raise _BadRequest("model must be an object")
            # only these fields: no launch command, executable or flags can come from a request
            fields = {k: raw.get(k) for k in MODEL_FIELDS}
            files, settings = fields["files"] or {}, fields["settings"] or {}
            if not isinstance(files, dict) or not isinstance(settings, dict):
                raise _BadRequest("files and settings must be objects")
            try:
                saved = app.store.save(ImageModel(id=fields["id"], name=fields["name"],
                                                  family=fields["family"], files=files, settings=settings))
            except ValueError as e:
                raise _BadRequest(str(e)) from None
            self._json(asdict(saved))

        def _scan(self, body: dict) -> None:
            folder = body.get("folder")
            if not isinstance(folder, str) or not folder or "\0" in folder:
                raise _BadRequest("folder must be an absolute path")
            if is_network_or_device(folder):  # checked before anything touches the path
                raise _BadRequest(NETWORK_PATH)
            if not os.path.isabs(folder):
                raise _BadRequest("folder must be an absolute path")
            try:  # a user's bad folder, not a Primer fault: no traceback in the log
                if not Path(folder).is_dir():
                    raise _BadRequest(f"not a folder: {folder}")
                proposals, truncated = scan_folder_capped(Path(folder), MAX_SCAN_FILES)
            except (OSError, ValueError) as e:
                raise _BadRequest(f"cannot scan {folder}: {e}") from None
            self._json({"proposals": proposals, "truncated": truncated})

        def _image_model(self, model_id) -> ImageModel:
            """The model a new job would use; _HttpError 409 with the reason when it can't run."""
            if app.images.images is None:
                raise _HttpError(409, NOT_CONFIGURED)
            if model_id is None:
                ok, reason = app.images.available()
                if not ok:
                    raise _HttpError(409, reason)
                model_id = app.store.default_id()
            if not isinstance(model_id, str):
                raise _BadRequest("model_id must be a string")
            try:
                model = app.store.get(model_id)
            except StoreCorrupt as e:
                raise _HttpError(409, str(e)) from None
            if model is None:
                raise _HttpError(409, f"unknown image model {model_id!r}")
            reasons = validate_model(model)
            if reasons:
                raise _HttpError(409, reasons[0])
            return model

        def _create_image(self, body: dict) -> None:
            node_id, concept = body.get("node_id"), body.get("concept")
            phrase, subject, prompt = body.get("phrase", ""), body.get("subject", ""), body.get("prompt")
            overrides = body.get("overrides")
            if any(v is not None and not isinstance(v, str) for v in (node_id, concept, phrase, subject, prompt)):
                raise _BadRequest("node_id, concept, phrase, subject and prompt must be strings")
            if overrides is not None and not isinstance(overrides, dict):
                raise _BadRequest("overrides must be an object")
            if not (subject or "").strip() and not (prompt or "").strip():
                raise _BadRequest("subject is required")
            if node_id:
                if app.graph.get_node(node_id) is None:
                    raise _HttpError(404, "page not found")
            elif not clean_typed(concept or ""):
                raise _BadRequest("node_id or concept is required")
            model = self._image_model(body.get("model_id"))
            try:
                effective_settings(model, overrides)
            except ValueError as e:
                raise _BadRequest(str(e)) from None
            if app.images.closed:  # checked before a concept node is created
                raise _HttpError(409, SHUT_DOWN_REASON)
            if not node_id:  # an image for a concept with no page: just the node, no page written
                try:
                    node_id = app.graph.node_for(clean_typed(concept), "concept").id
                except ValueError as e:
                    raise _BadRequest(str(e)) from None
            try:
                media = app.images.create(node_id, phrase or "", subject or "", model.id, overrides, prompt)
            except ValueError as e:  # e.g. the model changed or Primer is shutting down meanwhile
                raise _HttpError(409, str(e)) from None
            self._json(asdict(media))

        def _media(self, name: str) -> None:
            """A generated image: var/media/<sha256>.png only, contained like the static UI."""
            name = unquote(name)
            root = (Path(app.var_dir) / "media").resolve()
            target = (root / name).resolve() if MEDIA_NAME.fullmatch(name) else None
            if target is None or target.parent != root or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            self._send(200, target.read_bytes(), "image/png")

        # static UI
        def _static(self, raw_path: str) -> None:
            root = Path(app.ui_dir).resolve()
            target = (root / unquote(raw_path).lstrip("/\\")).resolve()
            if target != root and root not in target.parents:
                self._json({"error": "not found"}, 404)
                return
            index = root / "index.html"
            if not index.is_file():
                self._json(UI_NOT_BUILT, 503)
                return
            if not target.is_file():
                target = index
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets us silently share a port that is already in use
    allow_reuse_address = sys.platform != "win32"


def make_server(app: App, host: str = "127.0.0.1", port: int = 8810) -> ThreadingHTTPServer:
    return _Server((host, port), _make_handler(app))
