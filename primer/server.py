"""Stdlib HTTP server: JSON routes, SSE page streaming and the built UI."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import select
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from primer.browse.address import parse as parse_address
from primer.browse.agent import ask_stream, run_agent
from primer.browse.prompts import DEFAULT_QUESTION
from primer.browse.service import BrowseError, BrowseService
from primer.config import (TIERS_DIR, ConfigError, Tier, list_tiers, load_tier, read_settings,
                           write_settings)
from primer.drafts import Drafter
from primer.engines import LOOPBACK_HOSTS, make_engine
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
from primer.stories.service import NO_CHAPTER_SCENES, SHUT_DOWN as STORIES_SHUT_DOWN
from primer.stories.service import StoryService
from primer.voice.client import VoiceError, make_voice
from primer.pages import OpenRequest, PageService
from primer.reader.profile import NOTES_CHARS, Profile
from primer.reader.quest import (QuestConflict, QuestError, QuestNotFound, Quests,
                                 QuestUnavailable)
from primer.reader.quiz import Quiz, QuizError, QuizNotFound, QuizReset, QuizUnavailable
from primer.tales.service import (TaleConflict, TaleError, TaleNotFound, TaleService,
                                  TaleUnavailable)
from primer.reader.signals import VISIT_CAP_S, Signals
from primer.activity import ActivitySink
from primer.servers import ManagedServer, ServerManager
from primer.setup.api import ApiError, SetupApi, setup_status

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
NEEDS_PRIMER_HEADER = ("/v1/branches", "/v1/setup/updates")
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
NO_BROWSE = "Web is off in this tier and there is no offline encyclopedia"
BROWSE_REFERENCE_DOWN = "Web is off in this tier and the offline encyclopedia isn't running"
IMAGE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'",
                 "Cache-Control": "max-age=3600"}
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


def _is_setting(value) -> bool:
    return type(value) is int and 0 <= value <= 2


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
    browse: BrowseService | None = None
    drafter: Drafter | None = None  # thinking ahead; a new one with every PageService
    make_sd_server: Callable | None = None  # tests: stands in for launching sd-server
    installer: object | None = None  # Setup's primer.setup.installer.Installer (serve.py builds it)
    setup_api: SetupApi | None = None  # the /v1/setup/* logic; made on first use
    signals: Signals | None = None  # reading signals; made over the graph when not passed
    profile: Profile | None = None  # the reader model; made over the signals when not passed
    quiz: Quiz | None = None  # Test me and Review; made over the graph when not passed
    quests: Quests | None = None  # the Quest; made over the quiz when not passed
    tales: TaleService | None = None  # the Tale; made over the graph and stories when not passed
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ref_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ref_cache: tuple | None = field(default=None, repr=False)  # (expires, url, books or None)
    _map_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _archive: Archive | None = field(default=None, repr=False)  # opened on first use
    _voice_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _voice_cache: tuple | None = field(default=None, repr=False)  # (expires, url, healthy)
    _thinking: int | None = field(default=None, repr=False)  # saved thinking_ahead, None: unset
    _setup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Fill in what wasn't passed: the model store, a gate over these servers (also given
        to the pages when they have none) and the image service."""
        if self.pages.grounder is None:
            self.pages.grounder = self.make_grounder(self.tier)
        if self.signals is None:  # (tests may pass a stand-in pages object without signals)
            self.signals = getattr(self.pages, "signals", None) or Signals(self.graph)
        if getattr(self.signals, "activity", None) is None:  # PRIMER_ACTIVITY_DIR: tell Kairos (primer.activity)
            self.signals.activity = ActivitySink.from_env()
        if getattr(self.pages, "signals", None) is None:
            self.pages.signals = self.signals
        if self.profile is None:
            # the fast engine and the pages' busy flag are read through the app at call time:
            # a tier switch builds a new PageService (and engines)
            self.profile = Profile(self.graph, self.signals, lambda: getattr(self.pages, "fast", None),
                                   pages_busy=self._pages_fast_busy)
        if self.quiz is None:
            # the writing engine and the gate are read through the pages at call time (a tier
            # switch builds a new PageService, engines and gate)
            self.quiz = Quiz(self.graph, lambda: getattr(self.pages, "text", None),
                             lambda: getattr(self.pages, "gate", None), self.signals)
        self.quiz.on_answer = self.profile.nudge
        self.profile.quiz_facts = self.quiz.facts
        if self.quiz.forget not in self.profile.on_reset:
            self.profile.on_reset.append(self.quiz.forget)
        if self.quests is None:  # (engine and gate read through the pages, as for the quiz)
            self.quests = Quests(self.graph, self.quiz, lambda: getattr(self.pages, "text", None),
                                 lambda: getattr(self.pages, "gate", None))
        if self.quests.forget not in self.profile.on_reset:
            self.profile.on_reset.append(self.quests.forget)
        if self._discard_drafts not in self.profile.on_change:
            self.profile.on_change.append(self._discard_drafts)
        self._wire_reader(self.pages)
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
        if self.tales is None:
            # the stories, writing engine and gate are read through the app at call time (a
            # tier switch replaces them); the reader's notes set the tale's tone and age.
            # The reader reset keeps tales: they are content, like pages.
            self.tales = TaleService(self.graph, lambda: self.stories,
                                     lambda: getattr(self.pages, "text", None),
                                     lambda: getattr(self.pages, "gate", None), self.profile.block,
                                     voice=lambda: self.voice_available())
        self.stories.chapter_prefix = self.tales.prefix_for_story
        if self.browse is None:
            self.browse = self.make_browse(self.tier)
        self._thinking = self.saved_thinking_ahead()
        if self.pages.drafter is None:
            self.drafter = Drafter(self.graph, self.pages, self.gate, self.thinking_ahead)
        else:
            self.drafter = self.pages.drafter

    def _discard_drafts(self) -> None:
        """The profile changed (notes, switch, reset): pages drafted ahead with the old one are
        thrown away (Profile.on_change). Read through the app: a tier switch makes a new drafter."""
        drafter = self.drafter
        if drafter is not None:
            drafter.discard_all()

    def _pages_fast_busy(self) -> bool:
        busy = getattr(self.pages, "fast_busy", None)
        return bool(busy()) if busy is not None else False

    def _wire_reader(self, pages) -> None:
        """The reader model's block goes into page prompts and its style line into branch-card
        and highlight prompts (both "" while it is switched off), and page opens nudge it."""
        if getattr(pages, "reader_block", None) is None:
            pages.reader_block = self.profile.block
        if getattr(pages, "reader_style", None) is None:
            pages.reader_style = self.profile.style
        pages.nudge = self.profile.nudge

    def reader_hosted(self) -> list[str]:
        """Which models the profile leaves this machine for: "writing" (the block goes with
        page requests) and/or "fast" (titles, counts and quiz results go to it for the
        summary) when that model is not on a loopback address; [] when both are local."""
        out = []
        for name, spec in (("writing", self.tier.text), ("fast", self.tier.fast)):
            try:
                host = (urlsplit(spec.base_url).hostname or "").lower()
            except ValueError:
                host = ""
            if host not in LOOPBACK_HOSTS:
                out.append(name)
        return out

    def reader_state(self) -> dict:
        hosted = self.reader_hosted()
        return {**self.profile.state(), "hosted": bool(hosted), "hosted_models": hosted}

    @staticmethod
    def make_browse(tier: Tier) -> BrowseService:
        ref = tier.reference
        return BrowseService(KiwixClient(ref.url) if ref is not None else None,
                             tier.web.search != "off", make_searcher(tier.web))

    def make_stories(self, tier: Tier, pages: PageService, images: ImageService, gate: GpuGate) -> StoryService:
        def voice():
            if tier.voice is None:
                raise VoiceError(NO_VOICE)
            return make_voice(tier.voice)
        stories = StoryService(self.graph, pages.text, pages.fast, voice, images, gate,
                               Path(self.var_dir) / "media",
                               voice_name=tier.voice.voice if tier.voice is not None else "")
        if self.tales is not None:  # a chapter told again gets its pictures' looks from its tale
            stories.chapter_prefix = self.tales.prefix_for_story
        return stories

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

    def saved_thinking_ahead(self) -> int | None:
        """The saved thinking_ahead setting (0, 1 or 2), if there is a valid one."""
        try:
            value = read_settings(self.var_dir).get("thinking_ahead")
        except ConfigError:
            return None
        return value if _is_setting(value) else None

    def thinking_ahead(self) -> int:
        """Pages to draft ahead: the saved setting, else 1 where drafting costs nothing (auto
        GPU mode, the writing model on this machine's GPU) and 0 otherwise: a hosted (paid)
        API, a CPU-only model, or manual GPU mode. The reader can still turn it on."""
        value = self._thinking
        if value is not None:
            return value
        text = self.tier.text
        try:
            host = (urlsplit(text.base_url).hostname or "").lower()
        except ValueError:
            host = ""
        return 1 if self.tier.gpu_mode == "auto" and text.gpu and host in LOOPBACK_HOSTS else 0

    def set_thinking_ahead(self, value: int) -> None:
        """Remember `value` across restarts and tiers; off (or lowering it below the draft being
        written) stops that draft."""
        with self._lock:
            self._thinking = value
            try:
                settings = read_settings(self.var_dir)
            except ConfigError:
                settings = {}
            write_settings(self.var_dir, {**settings, "thinking_ahead": value})
            drafter = self.drafter
        if drafter is None:
            return
        if value == 0:
            drafter.cancel_current()
        else:  # lowered (2 -> 1): a draft two pages ahead stops
            drafter.cancel_beyond(value)

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

    def browse_available(self) -> dict:
        """Whether the browser can open anything: the web (a public fetch needs no search
        engine, but web off in the tier means off) or the offline encyclopedia."""
        ref, web = self.tier.reference, self.tier.web.search != "off"
        reference = ref is not None and self._cached_books(ref.url) is not None
        if web or reference:
            return {"ok": True, "reason": "", "web": web, "reference": reference}
        return {"ok": False, "reason": NO_BROWSE if ref is None else BROWSE_REFERENCE_DOWN,
                "web": False, "reference": False}

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
        old = self.drafter
        if old is not None:
            old.shutdown()  # may wait on a web fetch: not under the lock other requests need
        try:
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
                                    self.make_grounder(tier), signals=self.signals,
                                    reader_block=getattr(self.pages, "reader_block", None),
                                    reader_style=getattr(self.pages, "reader_style", None))
                self._wire_reader(pages)
                images = self.make_images(tier, gate, fast)
                text_pause.release_gpu = images.release_gpu
                self.tier, self.pages, self.servers = tier, pages, servers
                self.drafter = Drafter(self.graph, pages, gate, self.thinking_ahead)
                self.text_pause, self.gate, self.images = text_pause, gate, images
                self.maps = self.make_maps(self.graph, tier, fast)
                self.stories = self.make_stories(tier, pages, images, gate)
                self.browse = self.make_browse(tier)
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
        except BaseException:
            if old is not None and self.drafter is old:  # the switch failed: keep drafting for what is current
                self.drafter = Drafter(self.graph, self.pages, self.gate, self.thinking_ahead)
            raise

    def server_action(self, role: str, action: str) -> dict:
        """Start or stop one model server. Raises ValueError for an unknown role. Holds the
        lock so it can't act on a ServerManager that a concurrent tier switch discards."""
        with self._lock:
            getattr(self.servers, action)(role)
            self._forget_reference()
            with self._voice_lock:
                self._voice_cache = None
            return next(s for s in self.servers.statuses() if s["role"] == role)

    def setup(self) -> SetupApi:
        with self._setup_lock:
            if self.setup_api is None:
                self.setup_api = SetupApi(self)
            return self.setup_api

    def status(self) -> dict:
        servers = self.servers.statuses()
        return {"tier": self.tier.name, "tiers": list_tiers(self.tiers_dir),
                "suggested_tier": suggest_tier(self.hardware),
                "hardware": asdict(self.hardware), "servers": servers,
                "setup": setup_status(self.installer, servers),
                "source_mode": self.pages.source_mode, "sources": self.sources_status(),
                "gpu_mode": self.tier.gpu_mode,
                "images": self.images.server_status(), "image_available": self.image_available(),
                "video_available": self.image_available("video"),
                "animate_available": self.animate_available(), "gpu": self.gate.state(),
                "map_available": self.map_available(), "browse_available": self.browse_available(),
                "voice_available": self.voice_available(),
                "voice": {"name": self.tier.voice.voice if self.tier.voice is not None else ""},
                "thinking_ahead": {**(self.drafter.status() if self.drafter is not None else {}),
                                   "setting": self.thinking_ahead()}}

    def cards_json(self, node_id: str, phrase: str) -> list[dict]:
        """The phrase's branch cards, each with `ready`: a finished draft of the page opening
        it would show (for the current source mode) is waiting."""
        mode, drafter = self.pages.source_mode, self.drafter
        out = []
        for c in self.pages.branches(node_id, phrase):
            ready = False
            if drafter is not None:
                try:  # the node exactly as opening the card resolves it, created by no one
                    nid = (self.graph.node_id_for(c.concept, "context", node_id) if c.via == "context"
                           else self.graph.node_id_for(c.concept, "concept"))
                    ready = drafter.ready(nid, mode)
                except ValueError:
                    pass
            out.append({**asdict(c), "ready": ready})
        return out

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


def _peer_gone(sock) -> bool:
    """True when the client has closed its end (readable but peeks empty), or the socket errors."""
    try:
        if not select.select([sock], [], [], 0)[0]:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


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
                elif path == "/v1/reader":
                    self._json(app.reader_state())
                elif path in ("/v1/quiz", "/v1/quiz/due", "/v1/quiz/item"):
                    self._quiz_get(path, parse_qs(url.query))
                elif path == "/v1/quest":
                    self._json({"quest": app.quests.current()})
                elif path == "/v1/tales":
                    self._json(app.tales.state())
                elif path == "/v1/graph":
                    self._json(app.graph.snapshot(visible_only=True))
                elif path == "/v1/branches":
                    q = parse_qs(url.query)
                    node, phrase = q.get("node", [""])[0], q.get("phrase", [""])[0]
                    if not node or not phrase:
                        self._json({"error": "node and phrase are required"}, 400)
                    else:
                        self._json({"cards": app.cards_json(node, phrase)})
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
                elif path == "/v1/setup" or path.startswith("/v1/setup/"):
                    self._setup_get(path, parse_qs(url.query))
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
                elif path in ("/v1/browse", "/v1/browse/search", "/v1/browse/image"):
                    self._browse(path, parse_qs(url.query))
                elif path.startswith("/v1/stories/") and "/" not in path[len("/v1/stories/"):]:
                    story = app.stories.get(unquote(path[len("/v1/stories/"):]))
                    if story is None:
                        self._json({"error": "not found"}, 404)
                    else:
                        self._json({**self._with_pictures(story), "tale": self._story_tale(story)})
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
                elif path == "/v1/thinking-ahead":
                    value = body.get("setting")
                    if not _is_setting(value):
                        raise _BadRequest("setting must be 0, 1 or 2")
                    app.set_thinking_ahead(value)
                    self._json(app.status())
                elif path == "/v1/pages/open":
                    self._open(body)
                elif path == "/v1/reader/dwell":
                    self._dwell(body)
                elif path == "/v1/reader":
                    self._reader(body)
                elif path == "/v1/reader/reset":
                    app.profile.reset()
                    self._json(app.reader_state())
                elif path in ("/v1/quiz/make", "/v1/quiz/answer"):
                    self._quiz_post(path, body)
                elif path in ("/v1/quest", "/v1/quest/answer", "/v1/quest/skip", "/v1/quest/abandon"):
                    self._quest_post(path, body)
                elif path in ("/v1/tales", "/v1/tales/open", "/v1/tales/next", "/v1/tales/edit",
                              "/v1/tales/retry-update"):
                    self._tales_post(path, body)
                elif path == "/v1/reader/refresh":
                    reason = app.profile.refresh()
                    self._json({"skipped": reason} if reason else {"started": True},
                               200 if reason else 202)
                elif path in ("/v1/browse/ask", "/v1/browse/agent"):
                    self._browse_model(path, body)
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
                elif path.startswith("/v1/setup/"):
                    self._setup_post(path[len("/v1/setup/"):], body)
                else:
                    self._json({"error": "not found"}, 404)
            except _BadRequest as e:
                self._json({"error": str(e)}, 400)
            except _HttpError as e:
                self._json({"error": str(e)}, e.status)
            except BrowseError as e:
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

        def _setup_get(self, path: str, q: dict) -> None:
            try:
                if path == "/v1/setup":
                    self._json(app.setup().overview())
                elif path == "/v1/setup/updates":
                    self._json(app.setup().updates(refresh=q.get("refresh", [""])[0] == "1"))
                elif path == "/v1/setup/models":
                    self._json(app.setup().models())
                else:
                    raise _HttpError(404, "not found")
            except ApiError as e:
                raise _HttpError(e.status, e.reason) from None

        def _setup_post(self, action: str, body: dict) -> None:
            """POST /v1/setup/<action>; approvals run without app._lock (see primer.setup.api)."""
            api = app.setup()
            routes = {"plan": lambda: api.plan(body), "start": lambda: api.start(body), "pause": api.pause,
                      "resume": api.resume, "cancel": lambda: api.cancel(body),
                      "licence": lambda: api.licence(body), "approve": lambda: api.approve(body),
                      "approve-release": lambda: api.approve_release(body), "move": lambda: api.move(body),
                      "move/cancel": api.cancel_move}
            if action not in routes:
                raise _HttpError(404, "not found")
            try:
                self._json(routes[action]())
            except ApiError as e:
                raise _HttpError(e.status, e.reason) from None

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

        def _browse(self, path: str, q: dict) -> None:
            def arg(name: str) -> str:
                return q.get(name, [""])[0]
            try:
                if path == "/v1/browse":
                    self._json(app.browse.open(arg("u")))
                elif path == "/v1/browse/search":
                    self._json({"results": app.browse.search(arg("q"), arg("where"))})
                else:
                    data, ctype = app.browse.image(arg("u"))
                    self._send(200, data, ctype, IMAGE_HEADERS)
            except BrowseError as e:
                self._json({"error": str(e)}, e.status)

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
            source_url = body.get("source_url")
            if source_url == "":
                source_url = None  # empty is absent
            if source_url is not None:
                if not isinstance(source_url, str):
                    raise _BadRequest("source_url must be a string")
                try:
                    source_url = parse_address(source_url).url  # http(s) or kiwix: only, cleaned
                except ValueError as exc:
                    raise _BadRequest(f"source_url: {exc}") from None
            return OpenRequest(concept=concept, via=via, from_id=from_id, phrase=phrase,
                               depth=depth, regenerate=bool(body.get("regenerate", False)),
                               node_id=node_id, source=source, source_url=source_url)

        def _sse(self) -> Callable[[str, dict], None]:
            """Start a text/event-stream response; the returned emit(kind, data) writes one event."""
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
                except OSError:  # disconnect or stalled client: stop writing
                    alive = False
            emit.alive = lambda: alive and not _peer_gone(self.connection)  # page opens keep generating; browse model calls stop
            return emit

        def _dwell(self, body: dict) -> None:
            """POST /v1/reader/dwell {node_id, seconds}: seconds a page was visible (0 < s <= 600)."""
            node_id, seconds = body.get("node_id"), body.get("seconds")
            if not isinstance(node_id, str) or not node_id:
                raise _BadRequest("node_id is required")
            number = isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
            if not number or not 0 < seconds <= VISIT_CAP_S:  # NaN fails the comparison too
                raise _BadRequest(f"seconds must be a number above 0 and at most {VISIT_CAP_S:g}")
            if app.graph.get_node(node_id) is None:
                raise _HttpError(404, "page not found")
            app.signals.dwell(node_id, seconds)
            self._send(204, b"", "application/json")

        def _reader(self, body: dict) -> None:
            """POST /v1/reader {notes?: str (<= 1000), enabled?: bool}: the reader's state."""
            notes, enabled = body.get("notes"), body.get("enabled")
            if notes is not None and (not isinstance(notes, str) or len(notes) > NOTES_CHARS):
                raise _BadRequest(f"notes must be a string of at most {NOTES_CHARS} characters")
            if enabled is not None and not isinstance(enabled, bool):
                raise _BadRequest("enabled must be true or false")
            if notes is not None:
                app.profile.set_notes(notes)
            if enabled is not None:
                app.profile.set_enabled(enabled)
            self._json(app.reader_state())

        def _quiz_get(self, path: str, q: dict) -> None:
            """GET /v1/quiz?node_id= {items}, /v1/quiz/due?limit= {due, counts},
            /v1/quiz/item?id= (the item with its model answer)."""
            try:
                if path == "/v1/quiz/due":
                    raw = q.get("limit", ["20"])[0]
                    if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) <= 200:
                        raise _HttpError(400, "limit must be 1-200")
                    self._json({"due": app.quiz.due(int(raw)), "counts": app.quiz.counts()})
                    return
                key = "node_id" if path == "/v1/quiz" else "id"
                value = q.get(key, [""])[0]
                if not value:
                    raise _HttpError(400, f"{key} is required")
                if path == "/v1/quiz":
                    self._json({"items": app.quiz.for_node(value)})
                else:
                    self._json(app.quiz.item(value))
            except QuizNotFound as e:
                raise _HttpError(404, str(e)) from None

        def _quiz_post(self, path: str, body: dict) -> None:
            """POST /v1/quiz/make {node_id, fresh?} {items}; POST /v1/quiz/answer
            {item_id, answer, revealed?} {mark, feedback, answer, item} (revealed: the model
            answer was shown first, so not_yet without asking the model). 404 unknown page or item, 409 a reset
            discarded it, 502 the model's output couldn't be read, 503 the writing model is
            unavailable."""
            try:
                if path == "/v1/quiz/make":
                    node_id, fresh = body.get("node_id"), body.get("fresh", False)
                    if not isinstance(node_id, str) or not node_id:
                        raise _BadRequest("node_id is required")
                    if not isinstance(fresh, bool):
                        raise _BadRequest("fresh must be true or false")
                    self._json({"items": app.quiz.make(node_id, fresh=fresh)})
                else:
                    item_id, answer = body.get("item_id"), body.get("answer")
                    revealed = body.get("revealed", False)
                    if not isinstance(item_id, str) or not item_id:
                        raise _BadRequest("item_id is required")
                    if not isinstance(revealed, bool):
                        raise _BadRequest("revealed must be true or false")
                    try:
                        self._json(app.quiz.mark(item_id, answer, revealed=revealed))
                    except ValueError as e:
                        raise _BadRequest(str(e)) from None
            except QuizNotFound as e:
                raise _HttpError(404, str(e)) from None
            except QuizReset as e:  # discarded by a reader reset: nothing was stored
                raise _HttpError(409, str(e)) from None
            except QuizError as e:
                raise _HttpError(502, str(e)) from None
            except QuizUnavailable as e:
                raise _HttpError(503, str(e)) from None

        def _quest_post(self, path: str, body: dict) -> None:
            """POST /v1/quest {from_node_id?, rooms? (2-8, default 5)} 202 {quest_id, quest};
            POST /v1/quest/answer {quest_id, answer}; POST /v1/quest/skip {quest_id};
            POST /v1/quest/abandon {quest_id} (leave the quest being played). 400 bad
            body or too few pages read, 404 unknown page or quest, 409 a quest is being built
            or this one can't take that now, 502 the mark couldn't be read, 503 no writing
            model."""
            try:
                if path == "/v1/quest":
                    node_id, rooms = body.get("from_node_id"), body.get("rooms", 5)
                    if node_id is not None and (not isinstance(node_id, str) or not node_id):
                        raise _BadRequest("from_node_id must be a page id")
                    try:
                        quest = app.quests.build(node_id, rooms)
                    except ValueError as e:
                        raise _BadRequest(str(e)) from None
                    self._json({"quest_id": quest["id"], "quest": quest}, 202)
                    return
                quest_id = body.get("quest_id")
                if not isinstance(quest_id, str) or not quest_id:
                    raise _BadRequest("quest_id is required")
                if path == "/v1/quest/skip":
                    self._json(app.quests.skip(quest_id))
                    return
                if path == "/v1/quest/abandon":
                    self._json(app.quests.abandon(quest_id))
                    return
                try:
                    self._json(app.quests.answer(quest_id, body.get("answer")))
                except ValueError as e:
                    raise _BadRequest(str(e)) from None
            except QuestError as e:
                raise _HttpError(400, str(e)) from None
            except (QuestNotFound, QuizNotFound) as e:
                raise _HttpError(404, str(e)) from None
            except QuestConflict as e:
                raise _HttpError(409, str(e)) from None
            except QuizError as e:
                raise _HttpError(502, str(e)) from None
            except (QuestUnavailable, QuizUnavailable) as e:
                raise _HttpError(503, str(e)) from None

        def _tales_post(self, path: str, body: dict) -> None:
            """POST /v1/tales {seed?} {current} (a new tale from the reader's wish, invented
            when empty); /v1/tales/open {tale_id} {current}; /v1/tales/next {pages?} 202
            {story_id, n} (the bible update follows in the background); /v1/tales/edit
            {tale_id, name?, description?, look?, remove_thread?, remove_place?} {current};
            /v1/tales/retry-update {tale_id, n} 202 {current}. 400 bad body or no pages to draw
            on, 404 unknown tale or chapter, 409 a chapter is being written (or nothing to
            retry, or the voice can't narrate a chapter now), 502 the model's output couldn't be read, 503 no writing model."""
            try:
                if path == "/v1/tales":
                    self._json({"current": app.tales.start(body.get("seed"))})
                    return
                if path == "/v1/tales/next":
                    self._json(app.tales.next_chapter(body.get("pages")), 202)
                    return
                tale_id = body.get("tale_id")
                if not isinstance(tale_id, str) or not tale_id:
                    raise _BadRequest("tale_id is required")
                if path == "/v1/tales/open":
                    self._json({"current": app.tales.open(tale_id)})
                elif path == "/v1/tales/edit":
                    fields = {k: v for k, v in body.items() if k != "tale_id"}
                    self._json({"current": app.tales.edit(tale_id, fields)})
                else:
                    self._json({"current": app.tales.retry_update(tale_id, body.get("n"))}, 202)
            except TaleNotFound as e:
                raise _HttpError(404, str(e)) from None
            except TaleConflict as e:
                raise _HttpError(409, str(e)) from None
            except TaleError as e:
                raise _HttpError(502, str(e)) from None
            except TaleUnavailable as e:
                raise _HttpError(503, str(e)) from None
            except ValueError as e:
                raise _BadRequest(str(e)) from None

        def _open(self, body: dict) -> None:
            req = self._open_request(body)
            emit = self._sse()
            try:
                for kind, data in app.pages.open(req):
                    emit(kind, data)
            except Exception:
                log.exception("page open failed")
                emit("error", {"message": "internal error"})

        def _browse_model(self, path: str, body: dict) -> None:
            """POST /v1/browse/ask {u, question} and /v1/browse/agent {u, goal}: SSE."""
            u, agent = body.get("u"), path.endswith("/agent")
            field = "goal" if agent else "question"
            text = body.get(field)
            if text is None and not agent:
                text = DEFAULT_QUESTION
            if not isinstance(u, str) or not u:
                raise _BadRequest("u is required")
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise _BadRequest(f"{field} must be a short string")
            text = text.strip()
            try:
                page = app.browse.open(u)  # errors are JSON with a status, before the stream starts
            except ValueError as e:
                raise _BadRequest(str(e)) from None
            emit = self._sse()
            try:
                if agent:
                    run_agent(u, text, app.browse.open, app.pages.text, app.pages.gate, emit,
                              first_page=page)
                else:
                    ask_stream(page, text, app.pages.text, app.pages.gate, emit)
            except Exception:
                log.exception("browse model call failed")
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

        def _story_tale(self, story: dict) -> dict | None:
            """A chapter's {id, title, n}: its tale and chapter number; None for other stories
            and for a chapter told again since (the chapter follows the new story)."""
            if story.get("kind") != "chapter":
                return None
            found = app.graph.chapter_for_story(story["id"])
            tale = app.graph.get_tale(found["tale_id"]) if found else None
            return {"id": tale["id"], "title": tale["title"], "n": found["n"]} if tale else None

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
                conflict = str(e) in (STORIES_SHUT_DOWN, NO_CHAPTER_SCENES)
                raise _HttpError(409 if conflict else 404, str(e)) from None
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
