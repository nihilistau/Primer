"""Stdlib HTTP server: JSON routes, SSE page streaming and the built UI."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
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
from primer.grounding import MODES
from primer.grounding.grounder import Grounder
from primer.grounding.kiwix import KiwixClient, KiwixError
from primer.grounding.web import make_searcher
from primer.hardware import Hardware, suggest_tier
from primer.images.families import FAMILIES, SETTINGS, TWO_STAGE_SETTINGS, VIDEO_SETTINGS
from primer.images.models import (NETWORK_PATH, ImageModel, ModelStore, StoreCorrupt, animate_model,
                                  can_animate, effective_settings, is_network_or_device, kind_of,
                                  only_animates_reason, validate_model)
from primer.images.prompting import match_image_request, match_video_request
from primer.images.scan import scan_folder_capped
from primer.images.service import (KINDS, NO_ANIMATE_MODEL, NOT_CONFIGURED, SHUT_DOWN_REASON,
                                   ImageService)
from primer.maps.geocode import Geocoder
from primer.maps.intent import match_map_request
from primer.maps.pmtiles import Archive, PMTilesError
from primer.maps.service import SHUT_DOWN as MAPS_SHUT_DOWN
from primer.maps.service import TIER_SWITCH as MAPS_TIER_SWITCH
from primer.maps.service import MapService
from primer.stories.intent import match_story_request
from primer.stories.service import KINDS as STORY_KINDS
from primer.stories.service import SHUT_DOWN as STORIES_SHUT_DOWN
from primer.stories.service import StoryService
from primer.voice.client import VoiceError, make_voice
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
MEDIA_NAME = re.compile(r"[0-9a-f]{64}\.(png|webm|wav)")
MEDIA_TYPES = {".png": "image/png", ".webm": "video/webm", ".wav": "audio/wav"}
RANGE_RE = re.compile(r"bytes=([0-9]{0,18})-([0-9]{0,18})")  # one range only: a list (with a comma) doesn't match
MAP_TILE = re.compile(r"/v1/map/tiles/([0-9]{1,12})/([0-9]{1,12})/([0-9]{1,12})\.mvt")
MAP_ASSET_TYPES = {".pbf": "application/x-protobuf", ".json": "application/json", ".png": "image/png"}
MAP_CACHE = {"Cache-Control": "max-age=86400"}
MAP_ATTRIBUTION = "© OpenStreetMap contributors · Protomaps"
MAX_TILE_ZOOM = 26
TILE_ENCODINGS = {1: None, 2: "gzip"}  # PMTiles tile compression -> Content-Encoding; others unsupported
NO_MAP = "No [map] section in this tier"
NO_VOICE = "No [voice] section in this tier"
VOICE_DOWN = "The voice server isn't running"
VOICE_STARTING = "The voice server is starting"
STORIES_TIER_SWITCH = "Primer switched tiers"
VOICE_STATUS_TTL_S = 5.0
NO_LOOKUP = "No way to look places up: no offline reference and web search is off"
REFERENCE_DOWN_WEB_OFF = "The reference server isn't running and web search is off"
MAX_SCAN_FILES = 2000
MODEL_FIELDS = ("id", "name", "family", "files", "settings", "loras")  # all a client may set on a model
RESUME_POLL_S = 0.1
STATUS_REFERENCE_TIMEOUT_S = 2.0  # /v1/status asks kiwix-serve once, and must stay quick
REFERENCE_STATUS_TTL_S = 5.0
NO_REFERENCE = "No [reference] section in this tier"
REFERENCE_DOWN = "The reference server isn't running"
WEB_OFF = "Web search is off in this tier"


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
    maps: MapService | None = None
    stories: StoryService | None = None
    make_sd_server: Callable | None = None  # tests: stands in for launching sd-server
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ref_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ref_cache: tuple | None = field(default=None, repr=False)  # (expires, url, books or None)
    _map_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _archive: Archive | None = field(default=None, repr=False)  # opened on first use
    _voice_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _voice_cache: tuple | None = field(default=None, repr=False)  # (expires, url, healthy)

    def __post_init__(self) -> None:
        """Fill in what wasn't passed: the model store, a gate over these servers (also given
        to the pages when they have none) and the image service."""
        if self.pages.grounder is None:
            self.pages.grounder = self.make_grounder(self.tier)
        self.pages.source_mode = self.saved_source_mode() or self.tier.source_mode
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
        if self.maps is None:
            self.maps = self.make_maps(self.graph, self.tier, self.pages.fast)
        if self.stories is None:
            self.stories = self.make_stories(self.tier, self.pages, self.images, self.gate)

    def make_stories(self, tier: Tier, pages: PageService, images: ImageService, gate: GpuGate) -> StoryService:
        def voice():
            if tier.voice is None:
                raise VoiceError(NO_VOICE)
            return make_voice(tier.voice)
        return StoryService(self.graph, pages.text, pages.fast, voice, images, gate,
                            Path(self.var_dir) / "media",
                            voice_name=tier.voice.voice if tier.voice is not None else "")

    @staticmethod
    def make_maps(graph, tier: Tier, fast) -> MapService:
        ref, web = tier.reference, tier.web.search != "off"
        return MapService(graph, fast, lambda refresh=False: Geocoder(
            graph, KiwixClient(ref.url) if ref is not None else None, web, refresh=refresh))

    def make_images(self, tier: Tier, gate: GpuGate, fast) -> ImageService:
        extra = {"make_server": self.make_sd_server} if self.make_sd_server else {}
        return ImageService(self.graph, self.store, tier.images, fast, gate,
                            self.var_dir / "media", **extra)

    @staticmethod
    def make_grounder(tier: Tier) -> Grounder:
        """A managed reference lists its books first-wins in zims order (kiwix-serve names a
        book after its ZIM file, without .zim); an external server's order is left to it."""
        ref = tier.reference
        kiwix = KiwixClient(ref.url) if ref is not None else None
        order = [Path(z).stem for z in ref.zims] if ref is not None and ref.managed else None
        return Grounder(kiwix, make_searcher(tier.web), book_order=order)

    def saved_source_mode(self) -> str | None:
        """The reader's saved default mode (settings.json), if it is a valid one."""
        try:
            mode = read_settings(self.var_dir).get("source_mode")
        except ConfigError:
            return None
        return mode if mode in MODES else None

    def set_source_mode(self, mode: str) -> None:
        """Make `mode` the default for new pages and remember it across restarts and tiers."""
        with self._lock:
            self.pages.source_mode = mode
            try:
                settings = read_settings(self.var_dir)
            except ConfigError:
                settings = {}
            write_settings(self.var_dir, {**settings, "source_mode": mode})

    def shutdown_images(self) -> None:
        """Stop image jobs and sd-server without restarting text servers that are about to stop."""
        if self.text_pause is not None:
            self.text_pause.retire()
        self.images.shutdown()

    def shutdown_maps(self, reason: str = MAPS_SHUT_DOWN) -> None:
        """Stop map jobs (unfinished ones fail with `reason`) and close the map file."""
        try:
            if self.maps is not None:
                self.maps.shutdown(reason)
        finally:
            self._close_archive()

    def shutdown_stories(self, reason: str = STORIES_SHUT_DOWN) -> None:
        """Stop story jobs; unfinished stories fail with `reason`."""
        if self.stories is not None:
            self.stories.shutdown(reason)

    def _close_archive(self) -> None:
        with self._map_lock:
            archive, self._archive = self._archive, None
        if archive is not None:
            archive.close()

    def map_archive(self) -> Archive:
        """The tier's map file, opened on first use. PMTilesError if there is none or it can't be
        read (tried again next time, so a file put in place later is picked up)."""
        with self._map_lock:
            cfg = self.tier.map
            if self._archive is not None and (cfg is None or self._archive.path != Path(cfg.pmtiles)):
                self._archive.close()  # opened for a tier that is no longer current
                self._archive = None
            if cfg is None:
                raise PMTilesError(NO_MAP)
            if self._archive is None:
                archive = Archive(cfg.pmtiles)
                if archive.header["tile_compression"] not in TILE_ENCODINGS:
                    archive.close()
                    raise PMTilesError("unsupported tile compression")
                self._archive = archive
            return self._archive

    def map_available(self) -> dict:
        if self.tier.map is None:
            return {"ok": False, "reason": NO_MAP}
        try:
            self.map_archive()
        except PMTilesError as e:
            return {"ok": False, "reason": f"The map file can't be read ({e})"}
        ref, web = self.tier.reference, self.tier.web.search != "off"
        if not web:
            if ref is None:
                return {"ok": False, "reason": NO_LOOKUP}
            if self._cached_books(ref.url) is None:  # the status check's cached answer
                return {"ok": False, "reason": REFERENCE_DOWN_WEB_OFF}
        return {"ok": True, "reason": ""}

    def voice_available(self) -> dict:
        """Whether Listen and Story can run now. A launched server's state says so; one that
        wasn't launched by Primer is asked (briefly, and at most every VOICE_STATUS_TTL_S)."""
        cfg = self.tier.voice
        if cfg is None:
            return {"ok": False, "reason": NO_VOICE}
        server = self.servers._servers.get("voice")
        state = server.status()["state"] if server is not None else "stopped"
        if state == "ready":
            if cfg.managed and not server.alive():
                return {"ok": False, "reason": VOICE_DOWN}  # it died after becoming ready
            return {"ok": True, "reason": ""}
        if cfg.managed and state != "external":
            return {"ok": False, "reason": VOICE_STARTING if state == "starting" else VOICE_DOWN}
        now = time.monotonic()
        with self._voice_lock:
            hit = self._voice_cache
        if hit is not None and hit[1] == cfg.base_url and now < hit[0]:
            up = hit[2]
        else:
            up = bool(self._voice_health())  # up to HEALTH_TIMEOUT_S: not under any lock
            with self._voice_lock:
                self._voice_cache = (time.monotonic() + VOICE_STATUS_TTL_S, cfg.base_url, up)
        return {"ok": True, "reason": ""} if up else {"ok": False, "reason": VOICE_DOWN}

    def _voice_health(self) -> bool:
        return make_voice(self.tier.voice).health()  # HEALTH_TIMEOUT_S bounds it

    def reload_tier(self, name: str) -> None:
        """Switch tiers. Raises ConfigError, changing nothing, if the tier is unknown or invalid."""
        tier = load_tier(name, self.tiers_dir)
        self._forget_reference()
        with self._lock:
            self.shutdown_images()
            self.shutdown_maps(MAPS_TIER_SWITCH)
            self.shutdown_stories(STORIES_TIER_SWITCH)
            self.servers.stop_all()
            self.pages.shutdown(drain=False)  # queued prefetch is for the old tier's servers
            servers = ServerManager(tier)
            text_pause, gate = make_gate(tier, servers)
            fast = make_engine(tier.fast)
            mode = self.saved_source_mode() or tier.source_mode
            pages = PageService(self.graph, make_engine(tier.text), fast, mode, gate,
                                self.make_grounder(tier))
            images = self.make_images(tier, gate, fast)
            text_pause.release_gpu = images.release_gpu
            self.tier, self.pages, self.servers = tier, pages, servers
            self.text_pause, self.gate, self.images = text_pause, gate, images
            self.maps = self.make_maps(self.graph, tier, fast)
            self.stories = self.make_stories(tier, pages, images, gate)
            with self._voice_lock:
                self._voice_cache = None
            self._close_archive()  # a tile request during the switch may have reopened the old file
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
            self._forget_reference()
            with self._voice_lock:
                self._voice_cache = None
            return next(s for s in self.servers.statuses() if s["role"] == role)

    def status(self) -> dict:
        return {"tier": self.tier.name, "tiers": list_tiers(self.tiers_dir),
                "suggested_tier": suggest_tier(self.hardware),
                "hardware": asdict(self.hardware), "servers": self.servers.statuses(),
                "source_mode": self.pages.source_mode, "sources": self.sources_status(),
                "gpu_mode": self.tier.gpu_mode,
                "images": self.images.server_status(), "image_available": self.image_available(),
                "video_available": self.image_available("video"),
                "animate_available": self.animate_available(), "gpu": self.gate.state(),
                "map_available": self.map_available(), "voice_available": self.voice_available(),
                "voice": {"name": self.tier.voice.voice if self.tier.voice is not None else ""}}

    def sources_status(self) -> dict:
        """Which source modes can be used now, and why not. One quick call to kiwix-serve,
        bounded in total (a server that trickles bytes can't hold /v1/status)."""
        tier, pages = self.tier, self.pages
        books: list[dict] = []
        if tier.reference is None:
            ref_ok, ref_reason = False, NO_REFERENCE
        else:
            books = self._cached_books(tier.reference.url)
            ref_ok = books is not None
            ref_reason = "" if ref_ok else REFERENCE_DOWN
            books = books or []
        grounder = pages.grounder
        web_ok = grounder is not None and grounder.searcher is not None
        return {"storyteller": {"available": True, "reason": ""},
                "reference": {"available": ref_ok, "reason": ref_reason, "books": books},
                "web": {"available": web_ok, "reason": "" if web_ok else WEB_OFF,
                        "engine": tier.web.search if web_ok else ""}}

    def _forget_reference(self) -> None:
        with self._ref_lock:
            self._ref_cache = None

    def _cached_books(self, url: str) -> list[dict] | None:
        """_books, remembered for REFERENCE_STATUS_TTL_S (status is polled every couple of
        seconds, and a silent kiwix-serve would cost the full deadline each time)."""
        with self._ref_lock:
            hit = self._ref_cache
            if hit is not None and hit[1] == url and time.monotonic() < hit[0]:
                return hit[2]
            books = self._books(url)
            self._ref_cache = (time.monotonic() + REFERENCE_STATUS_TTL_S, url, books)
            return books

    @staticmethod
    def _books(url: str) -> list[dict] | None:
        """kiwix-serve's books, or None when it doesn't answer within the status deadline."""
        box: list = []

        def ask() -> None:
            try:
                box.append(KiwixClient(url, STATUS_REFERENCE_TIMEOUT_S).books())
            except KiwixError:
                pass

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        t.join(STATUS_REFERENCE_TIMEOUT_S)
        return box[0] if box else None

    def page_sources(self, node_id: str, n: int) -> list[dict]:
        """A version's sources for the reader. Reference links use the reference url of the
        current tier (kiwix-serve may have moved); web links only if they are http(s)."""
        ref = self.tier.reference
        kiwix = KiwixClient(ref.url) if ref is not None else None
        out = []
        for s in self.graph.sources(node_id, n):
            item = {"n": s.n, "title": s.title, "origin": s.origin, "where": s.where}
            if s.origin == "reference" and kiwix is not None and s.book and s.path:
                item["href"] = kiwix.href(s.book, s.path)
            elif s.origin == "web" and s.url.lower().startswith(("http://", "https://")):
                item["href"] = s.url
            out.append(item)
        return out

    def image_available(self, kind: str = "image") -> dict:
        ok, reason = self.images.available(kind)
        return {"ok": ok, "reason": reason}

    def animate_available(self) -> dict:
        """Whether Animate can run, on which model, else the reason it can't."""
        if self.images.images is None:
            return {"ok": False, "reason": NOT_CONFIGURED, "model_id": None}
        try:
            model = animate_model(self.store)
        except StoreCorrupt as e:
            return {"ok": False, "reason": str(e), "model_id": None}
        if model is None:
            return {"ok": False, "reason": NO_ANIMATE_MODEL, "model_id": None}
        reasons = validate_model(model)
        return {"ok": not reasons, "reason": reasons[0] if reasons else "", "model_id": model.id}


def make_gate(tier: Tier, servers: ServerManager) -> tuple[TextPause, GpuGate]:
    text_pause = TextPause(servers)
    return text_pause, GpuGate(tier.gpu_mode, text_pause.pause, text_pause.resume)


class _BadRequest(Exception):
    pass


def _family_settings(fam) -> list[str]:
    """The setting keys a family takes (clean_settings drops the others)."""
    return [k for k in SETTINGS if k not in VIDEO_SETTINGS or (
        fam.kind == "video" and (k not in TWO_STAGE_SETTINGS or fam.two_stage))]


def _byte_range(header: str, size: int) -> tuple[int, int] | None:
    """(first, last) byte of a single `bytes=a-b`, `bytes=a-` or `bytes=-n` range, clipped to
    the file; None when it is malformed, a multi-range or unsatisfiable (answered 416)."""
    m = RANGE_RE.fullmatch(header.strip())
    if m is None or (not m[1] and not m[2]):
        return None
    if not m[1]:  # the last n bytes
        n = int(m[2])
        return (max(0, size - n), size - 1) if n and size else None
    start = int(m[1])
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    return (start, end) if start <= end and start < size else None


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
        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
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
                        version = {**asdict(page["version"]),
                                   "sources": app.page_sources(node.id, page["version"].n)}
                        self._json({"node": asdict(node), "version": version,
                                    "versions": page["versions"], "highlights": page["highlights"],
                                    "media": [asdict(m) for m in app.graph.media_for_node(node.id)],
                                    "maps": app.graph.maps_for(node.id),
                                    "stories": app.graph.stories_for(node.id)})
                elif path == "/v1/image-models":
                    self._image_models()
                elif path.startswith("/v1/images/") and "/" not in path[len("/v1/images/"):]:
                    try:
                        self._json(app.images.job(unquote(path[len("/v1/images/"):])))
                    except KeyError:
                        self._json({"error": "not found"}, 404)
                elif path.startswith("/v1/media/"):
                    self._media(path[len("/v1/media/"):])
                elif path.startswith("/v1/map/tiles/"):
                    self._map_tile(path)
                elif path == "/v1/map/tilejson":
                    self._map_tilejson()
                elif path.startswith("/v1/map/assets/"):
                    self._map_asset(path[len("/v1/map/assets/"):])
                elif path.startswith("/v1/stories/") and "/" not in path[len("/v1/stories/"):]:
                    story = app.stories.get(unquote(path[len("/v1/stories/"):]))
                    if story is None:
                        self._json({"error": "not found"}, 404)
                    else:
                        self._json(self._with_pictures(story))
                elif path.startswith("/v1/maps/") and "/" not in path[len("/v1/maps/"):]:
                    job = app.maps.get(unquote(path[len("/v1/maps/"):]))
                    if job is None:
                        self._json({"error": "not found"}, 404)
                    else:
                        self._json(job)
                elif path.startswith("/v1/"):
                    self._json({"error": "not found"}, 404)
                else:
                    self._static(path)
            except _HttpError as e:
                self._json({"error": str(e)}, e.status)
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
                elif path == "/v1/source-mode":
                    mode = body.get("mode")
                    if not isinstance(mode, str) or mode not in MODES:
                        raise _BadRequest(f"mode must be one of {MODES}")
                    app.set_source_mode(mode)
                    self._json(app.status())
                elif path == "/v1/pages/open":
                    self._open(body)
                elif path.startswith("/v1/servers/"):
                    self._server_action(path[len("/v1/servers/"):])
                elif path == "/v1/intent":
                    text = body.get("text")
                    if not isinstance(text, str):
                        raise _BadRequest("text must be a string")
                    video = match_video_request(text)
                    place = match_map_request(text)
                    tale = match_story_request(text)
                    if tale and not app.voice_available()["ok"]:
                        tale = None  # no voice: route it as before (a page, a picture...)
                    if tale:  # a story request is not also a picture, clip or map request
                        self._json({"image": None, "video": None, "map": None, "story": tale})
                    elif place:  # a map request is not also a picture or clip request
                        self._json({"image": None, "video": None, "map": place, "story": None})
                    else:
                        self._json({"image": None if video else match_image_request(text), "video": video,
                                    "map": None, "story": None})
                elif path == "/v1/stories":
                    self._create_story(body)
                elif path.startswith("/v1/stories/") and path.endswith("/again"):
                    self._again_story(unquote(path[len("/v1/stories/"):-len("/again")]))
                elif path == "/v1/maps":
                    self._create_map(body)
                elif path.startswith("/v1/maps/") and path.endswith("/again"):
                    self._again_map(unquote(path[len("/v1/maps/"):-len("/again")]))
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
            from_id, source = body.get("from_id"), body.get("source")
            if source is not None and (not isinstance(source, str) or source not in MODES):
                raise _BadRequest(f"source must be one of {MODES}")
            if not isinstance(concept, str) or not isinstance(phrase, str):
                raise _BadRequest("concept and phrase must be strings")
            if any(v is not None and not isinstance(v, str) for v in (from_id, node_id)):
                raise _BadRequest("from_id and node_id must be strings")
            return OpenRequest(concept=concept, via=via, from_id=from_id, phrase=phrase,
                               depth=depth, regenerate=bool(body.get("regenerate", False)),
                               node_id=node_id, source=source)

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
            families = {fid: {"label": f.label, "kind": f.kind, "needs_init": f.needs_init,
                              "can_animate": f.can_animate, "frame_step": f.frame_step,
                              "slots": [asdict(s) for s in f.slots], "defaults": f.defaults,
                              "fast_defaults": f.fast_defaults, "settings": _family_settings(f)}
                        for fid, f in FAMILIES.items()}
            models = [{**asdict(m), "kind": kind_of(m), "problems": validate_model(m)} for m in app.store.list()]
            self._json({"models": models, "default": app.store.default_id(),
                        "video_default": app.store.default_id("video"), "families": families,
                        "available": app.image_available(), "video_available": app.image_available("video"),
                        "animate_available": app.animate_available()})

        def _save_model(self, body: dict) -> None:
            raw = body.get("model")
            if not isinstance(raw, dict):
                raise _BadRequest("model must be an object")
            # only these fields: no launch command, executable or flags can come from a request
            fields = {k: raw.get(k) for k in MODEL_FIELDS}
            files, settings = fields["files"] or {}, fields["settings"] or {}
            if not isinstance(files, dict) or not isinstance(settings, dict):
                raise _BadRequest("files and settings must be objects")
            loras = fields["loras"]  # [{file, strength, stage}]: the store shape-checks each one
            if loras is not None and not isinstance(loras, list):
                raise _BadRequest("loras must be a list")
            try:
                saved = app.store.save(ImageModel(id=fields["id"], name=fields["name"], family=fields["family"],
                                                  files=files, settings=settings, loras=loras or []))
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

        def _image_model(self, model_id, kind: str = "image", animating: bool = False) -> ImageModel:
            """The model a new job would use: _HttpError 409 with the reason when it can't run,
            400 when the request asks a model for what it can't do."""
            if app.images.images is None:
                raise _HttpError(409, NOT_CONFIGURED)
            if model_id is None and animating:  # never text-to-video: a model that takes a picture
                try:
                    picked = animate_model(app.store)
                except StoreCorrupt as e:
                    raise _HttpError(409, str(e)) from None
                if picked is None:
                    raise _HttpError(409, NO_ANIMATE_MODEL)
                model_id = picked.id
            elif model_id is None:
                ok, reason = app.images.available(kind)
                if not ok:
                    raise _HttpError(409, reason)
                model_id = app.store.default_id(kind)
            if not isinstance(model_id, str):
                raise _BadRequest("model_id must be a string")
            try:
                model = app.store.get(model_id)
            except StoreCorrupt as e:
                raise _HttpError(409, str(e)) from None
            if model is None:
                raise _HttpError(409, f"unknown {kind} model {model_id!r}")
            if kind_of(model) != kind:
                raise _BadRequest(f"{model.name} is {'an image' if kind_of(model) == 'image' else 'a video'} model")
            reasons = validate_model(model)
            if reasons:
                raise _HttpError(409, reasons[0])
            fam = FAMILIES[model.family]
            if animating and not can_animate(model):
                raise _BadRequest(f"{model.name} can't animate a picture: choose a video model that can")
            if not animating and fam.needs_init:
                raise _BadRequest(only_animates_reason(fam))
            return model

        def _create_image(self, body: dict) -> None:
            """An image or video job. Retry and Again are the UI posting the request again; a
            video Again/Retry of an Animate job must send animate_from from the old job's
            settings (GET /v1/images/<id> returns them), or it becomes a text-to-video job."""
            node_id, concept = body.get("node_id"), body.get("concept")
            phrase, subject, prompt = body.get("phrase", ""), body.get("subject", ""), body.get("prompt")
            overrides = body.get("overrides")
            kind, animate_from = body.get("kind", "image"), body.get("animate_from")
            if kind not in KINDS:
                raise _BadRequest(f"kind must be one of {', '.join(KINDS)}")
            if animate_from is not None and (not isinstance(animate_from, str) or not animate_from):
                raise _BadRequest("animate_from must be a media id")
            if animate_from is not None and kind != "video":
                raise _BadRequest("animate_from needs kind video")
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
            model = self._image_model(body.get("model_id"), kind, animate_from is not None)
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
                media = app.images.create(node_id, phrase or "", subject or "", model.id, overrides, prompt,
                                          kind=kind, animate_from=animate_from)
            except ValueError as e:
                if kind == "video" and str(e) != SHUT_DOWN_REASON:  # e.g. the picture to animate
                    raise _BadRequest(str(e)) from None
                raise _HttpError(409, str(e)) from None  # the model changed or Primer is shutting down
            self._json(asdict(media))

        def _media(self, name: str) -> None:
            """A generated image, video or narration: var/media/<sha256>.png|.webm|.wav only, contained like the
            static UI. A video answers a single byte Range (browsers seek a video with one)."""
            name = unquote(name)
            root = (Path(app.var_dir) / "media").resolve()
            target = (root / name).resolve() if MEDIA_NAME.fullmatch(name) else None
            if target is None or target.parent != root or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            ctype = MEDIA_TYPES[Path(name).suffix]
            if ctype == "image/png":
                self._send(200, target.read_bytes(), ctype)
                return
            asked, size = self.headers.get("Range"), target.stat().st_size
            if asked is None:
                self._send(200, target.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
                return
            span = _byte_range(asked, size)
            if span is None:
                self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            start, end = span
            with target.open("rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            self._send(206, data, ctype, {"Accept-Ranges": "bytes",
                                          "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}"})

        # maps
        def _map_error(self, e: Exception) -> _HttpError:
            if isinstance(e, PMTilesError):
                return _HttpError(503, f"The map file can't be read ({e})" if app.tier.map else NO_MAP)
            return _HttpError(503, "The map is being reloaded")  # closed by a tier switch mid-request

        def _map_tile(self, path: str) -> None:
            m = MAP_TILE.fullmatch(path)
            if m is None:
                self._json({"error": "not found"}, 404)
                return
            z, x, y = (int(v) for v in m.groups())
            if z > MAX_TILE_ZOOM or x >= 1 << z or y >= 1 << z:
                self._json({"error": "tile coordinates out of range"}, 400)
                return
            try:
                archive = app.map_archive()
                data = archive.tile(z, x, y)
                encoding = TILE_ENCODINGS[archive.header["tile_compression"]]
            except (PMTilesError, ValueError, OSError) as e:
                raise self._map_error(e) from None
            if data is None:
                self._send(204, b"", "application/vnd.mapbox-vector-tile", MAP_CACHE)
                return
            headers = dict(MAP_CACHE)
            if encoding:
                headers["Content-Encoding"] = encoding
            self._send(200, data, "application/vnd.mapbox-vector-tile", headers)

        def _map_tilejson(self) -> None:
            try:
                archive = app.map_archive()
                h, meta = archive.header, archive.metadata()
            except (PMTilesError, ValueError, OSError) as e:
                raise self._map_error(e) from None
            host = self.headers.get("Host")  # already checked against 127.0.0.1/localhost:port
            tj = {"tilejson": "3.0.0", "tiles": [f"http://{host}/v1/map/tiles/{{z}}/{{x}}/{{y}}.mvt"],
                  "minzoom": h["min_zoom"], "maxzoom": h["max_zoom"], "bounds": list(h["bounds"]),
                  "center": list(h["center"]), "attribution": MAP_ATTRIBUTION}
            layers = meta.get("vector_layers")
            if isinstance(layers, list):
                tj["vector_layers"] = layers
            self._send(200, json.dumps(tj).encode("utf-8"), "application/json", MAP_CACHE)

        def _map_asset(self, name: str) -> None:
            """A glyph range or sprite from the tier's assets folder: .pbf, .json, .png only,
            and the resolved path must stay inside the folder."""
            cfg = app.tier.map
            name = unquote(name)
            ctype = MAP_ASSET_TYPES.get(Path(name).suffix.lower())
            data = None
            if cfg is not None and ctype is not None and "\0" not in name and ":" not in name:
                try:
                    root = Path(cfg.assets).resolve()
                    target = (root / name.lstrip("/\\")).resolve()
                    if (root in target.parents and target.suffix.lower() in MAP_ASSET_TYPES
                            and target.is_file()):
                        data = target.read_bytes()
                except (OSError, ValueError):
                    data = None
            if data is None:
                self._json({"error": "not found"}, 404)
                return
            self._send(200, data, ctype, MAP_CACHE)

        def _check_map(self) -> None:
            status = app.map_available()
            if not status["ok"]:
                raise _HttpError(409, status["reason"])

        def _create_map(self, body: dict) -> None:
            node_id, phrase = body.get("node_id"), body.get("phrase", "")
            if not isinstance(node_id, str) or not node_id:
                raise _BadRequest("node_id is required")
            if not isinstance(phrase, str):
                raise _BadRequest("phrase must be a string")
            phrase = clean_typed(phrase) if phrase.strip() else ""
            if app.graph.get_node(node_id) is None or app.graph.latest_version(node_id) is None:
                raise _HttpError(404, "page not found")
            self._check_map()
            try:
                mid = app.maps.create(node_id, phrase)
            except ValueError as e:
                raise _HttpError(409 if str(e) == MAPS_SHUT_DOWN else 404, str(e)) from None
            self._json({"id": mid}, 202)

        def _again_map(self, map_id: str) -> None:
            if app.maps.get(map_id) is None:
                raise _HttpError(404, "not found")
            self._check_map()
            try:
                mid = app.maps.again(map_id)
            except ValueError as e:
                raise _HttpError(409 if str(e) == MAPS_SHUT_DOWN else 404, str(e)) from None
            self._json({"id": mid}, 202)

        # stories
        def _check_voice(self) -> None:
            status = app.voice_available()
            if not status["ok"]:
                raise _HttpError(409, status["reason"])

        def _with_pictures(self, story: dict) -> dict:
            """The story with each scene's picture `{media_id, status, file}` (None when it has none)."""
            scenes = []
            for sc in story["scenes"]:
                media = app.graph.get_media(sc["media_id"]) if sc["media_id"] else None
                picture = ({"media_id": media.id, "status": media.status, "file": media.file}
                           if media is not None else None)
                scenes.append({**sc, "picture": picture})
            return {**story, "scenes": scenes}

        def _create_story(self, body: dict) -> None:
            node_id, kind = body.get("node_id"), body.get("kind", "story")
            if not isinstance(node_id, str) or not node_id:
                raise _BadRequest("node_id is required")
            if not isinstance(kind, str) or kind not in STORY_KINDS:
                raise _BadRequest(f"kind must be one of {', '.join(STORY_KINDS)}")
            if app.graph.get_node(node_id) is None or app.graph.latest_version(node_id) is None:
                raise _HttpError(404, "page not found")
            self._check_voice()
            try:
                sid = app.stories.create(node_id, kind)
            except ValueError as e:
                raise _HttpError(409 if str(e) == STORIES_SHUT_DOWN else 404, str(e)) from None
            self._json({"id": sid}, 202)

        def _again_story(self, story_id: str) -> None:
            if app.stories.get(story_id) is None:
                raise _HttpError(404, "not found")
            self._check_voice()
            try:
                sid = app.stories.again(story_id)
            except ValueError as e:
                raise _HttpError(409 if str(e) == STORIES_SHUT_DOWN else 404, str(e)) from None
            self._json({"id": sid}, 202)

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
