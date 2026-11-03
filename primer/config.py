"""Tier presets (tiers/*.toml) and the small settings file in var/."""
from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
TIERS_DIR = REPO_ROOT / "tiers"
KINDS = ("openai", "sp", "sdserver")
GPU_MODES = ("auto", "manual")
TIER_NAME = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(Exception):
    pass


_OWN_FIELDS = {"messages", "max_tokens", "temperature", "stream", "model"}


@dataclass(frozen=True)
class EngineSpec:
    kind: Literal["openai", "sp", "sdserver"]
    base_url: str
    model: str = ""
    managed: bool = False
    launch: tuple[str, ...] = ()
    stop: tuple[str, ...] = ()
    # frees the GPU without stopping the server (e.g. `lms unload --all`); the GPU gate runs it
    # instead of stopping the server, and the model loads again on the next request
    pause: tuple[str, ...] = ()
    api_key_file: str = ""
    ready_timeout_s: float = 120.0
    gpu: bool = False  # uses the GPU: the GPU guard counts it
    # extra request fields, e.g. chat_template_kwargs = { enable_thinking = false } for a model
    # that would otherwise spend its token budget thinking
    extra: dict = field(default_factory=dict, hash=False)

    @property
    def health_path(self) -> str:
        if self.kind == "sdserver":
            return "/sdcpp/v1/models/status"
        return "/v1/models" if self.kind == "openai" else "/health"


@dataclass(frozen=True)
class ImagesConfig:
    sd_server: str
    port: int = 8820
    ready_timeout_s: float = 900.0
    stall_timeout_s: float = 1800.0  # a running job with no progress this long has failed


@dataclass(frozen=True)
class ReferenceConfig:
    kiwix_serve: str = ""
    zims: tuple[str, ...] = ()
    port: int = 8830
    base_url: str = ""

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") or f"http://127.0.0.1:{self.port}"

    @property
    def managed(self) -> bool:
        return bool(self.kiwix_serve)


@dataclass(frozen=True)
class MapConfig:
    pmtiles: str  # the PMTiles v3 map file
    assets: str  # folder of glyphs and sprites (Protomaps basemaps-assets)


VOICE_KINDS = ("lfm3", "openai")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class VoiceConfig:
    kind: Literal["lfm3", "openai"]
    base_url: str
    voice: str = "female"
    model: str = ""
    api_key_file: str = ""
    managed: bool = False
    launch: tuple[str, ...] = ()
    stop: tuple[str, ...] = ()
    ready_timeout_s: float = 120.0
    timeout_s: float = 180.0  # one passage; raise it on a busy or slow CPU


WEB_SEARCHES = ("duckduckgo", "searxng", "brave", "off")
SOURCE_MODES = ("storyteller", "reference", "web")


@dataclass(frozen=True)
class WebConfig:
    search: Literal["duckduckgo", "searxng", "brave", "off"] = "duckduckgo"
    searxng_url: str = ""
    api_key_file: str = ""


@dataclass(frozen=True)
class Tier:
    name: str
    text: EngineSpec
    fast: EngineSpec
    source_mode: str = "storyteller"
    gpu_mode: Literal["auto", "manual"] = "auto"
    images: ImagesConfig | None = None
    reference: ReferenceConfig | None = None
    web: WebConfig = field(default_factory=WebConfig)
    map: MapConfig | None = None
    voice: VoiceConfig | None = None


def _engine(raw: object, where: str) -> EngineSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{where}: kind must be one of {KINDS}, got {kind!r}")
    if kind == "sdserver":  # [text] and [fast] only: sd-server is configured under [images]
        raise ConfigError(f'{where}: kind "sdserver" is not a text engine; '
                          "set sd-server up under [images] instead")
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ConfigError(f"{where}: base_url is required")
    lists = {}
    for key in ("launch", "stop", "pause"):
        val = raw.get(key, [])
        if not isinstance(val, list) or not all(isinstance(a, str) for a in val):
            raise ConfigError(f"{where}: {key} must be a list of strings")
        lists[key] = tuple(val)
    try:
        timeout = float(raw.get("ready_timeout_s", 120.0))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: ready_timeout_s must be a number") from None
    extra = raw.get("extra", {})
    if not isinstance(extra, dict):
        raise ConfigError(f"{where}: extra must be a table")
    clash = sorted(set(extra) & _OWN_FIELDS)
    if clash:
        raise ConfigError(f"{where}: extra can't set {', '.join(clash)}")
    gpu = raw.get("gpu", False)
    if not isinstance(gpu, bool):
        raise ConfigError(f"{where}: gpu must be true or false")
    return EngineSpec(
        kind=kind,
        base_url=base_url,
        model=str(raw.get("model", "")),
        managed=bool(raw.get("managed", False)),
        launch=lists["launch"],
        stop=lists["stop"],
        pause=lists["pause"],
        api_key_file=str(raw.get("api_key_file", "")),
        ready_timeout_s=timeout,
        gpu=gpu,
        extra=dict(extra),
    )


def _images(raw: object, where: str) -> ImagesConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    sd_server = raw.get("sd_server")
    if not isinstance(sd_server, str) or not sd_server:
        raise ConfigError(f"{where}: sd_server must be a path string")
    port = raw.get("port", 8820)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ConfigError(f"{where}: port must be an integer from 1024 to 65535")
    try:
        timeout = float(raw.get("ready_timeout_s", 900.0))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: ready_timeout_s must be a number") from None
    stall = raw.get("stall_timeout_s", 1800.0)
    if isinstance(stall, bool) or not isinstance(stall, (int, float)) or not 0 < stall < math.inf:
        raise ConfigError(f"{where}: stall_timeout_s must be a positive number of seconds")
    return ImagesConfig(sd_server=sd_server, port=port, ready_timeout_s=timeout,
                        stall_timeout_s=float(stall))


def _reference(raw: object, where: str) -> ReferenceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    kiwix_serve, base_url = raw.get("kiwix_serve", ""), raw.get("base_url", "")
    if not isinstance(kiwix_serve, str) or not isinstance(base_url, str):
        raise ConfigError(f"{where}: kiwix_serve and base_url must be strings")
    zims = raw.get("zims", [])
    if not isinstance(zims, list) or not all(isinstance(z, str) and z for z in zims):
        raise ConfigError(f"{where}: zims must be a list of path strings")
    port = raw.get("port", 8830)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ConfigError(f"{where}: port must be an integer from 1024 to 65535")
    if bool(kiwix_serve) == bool(base_url):
        raise ConfigError(f"{where}: set either kiwix_serve (with zims) or base_url, not both or neither")
    if kiwix_serve and not zims:
        raise ConfigError(f"{where}: kiwix_serve needs at least one entry in zims")
    return ReferenceConfig(kiwix_serve=kiwix_serve, zims=tuple(zims), port=port, base_url=base_url)


def _web(raw: object, where: str) -> WebConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    search = raw.get("search", "duckduckgo")
    if search not in WEB_SEARCHES:
        raise ConfigError(f"{where}: search must be one of {WEB_SEARCHES}, got {search!r}")
    url, key = raw.get("searxng_url", ""), raw.get("api_key_file", "")
    if not isinstance(url, str) or not isinstance(key, str):
        raise ConfigError(f"{where}: searxng_url and api_key_file must be strings")
    if search == "searxng" and not url:
        raise ConfigError(f"{where}: searxng needs searxng_url")
    if search == "brave" and not key:
        raise ConfigError(f"{where}: brave needs api_key_file")
    return WebConfig(search=search, searxng_url=url, api_key_file=key)


def _map(raw: object, where: str) -> MapConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    pmtiles, assets = raw.get("pmtiles"), raw.get("assets")
    if not isinstance(pmtiles, str) or not pmtiles or not isinstance(assets, str) or not assets:
        raise ConfigError(f"{where}: pmtiles and assets are both required path strings")
    return MapConfig(pmtiles=pmtiles, assets=assets)


def _voice(raw: object, where: str) -> VoiceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    kind = raw.get("kind")
    if kind not in VOICE_KINDS:
        raise ConfigError(f"{where}: kind must be one of {VOICE_KINDS}, got {kind!r}")
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ConfigError(f"{where}: base_url is required")
    try:
        parts = urlsplit(base_url)
        host, _port = (parts.hostname or "").lower(), parts.port  # .port raises on a bad port
    except ValueError:
        raise ConfigError(f"{where}: base_url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ConfigError(f"{where}: base_url must start with http:// or https://")
    if kind == "lfm3":  # the LFM3 server has no auth: it must be on this machine
        if host not in _LOOPBACK:
            raise ConfigError(f"{where}: an lfm3 voice server must be on this machine "
                              "(127.0.0.1, localhost or ::1)")
    strings = {}
    for key, default in (("voice", "female"), ("model", ""), ("api_key_file", "")):
        val = raw.get(key, default)
        if not isinstance(val, str):
            raise ConfigError(f"{where}: {key} must be a string")
        strings[key] = val
    lists = {}
    for key in ("launch", "stop"):
        val = raw.get(key, [])
        if not isinstance(val, list) or not all(isinstance(a, str) for a in val):
            raise ConfigError(f"{where}: {key} must be a list of strings")
        lists[key] = tuple(val)
    timeout = raw.get("ready_timeout_s", 120.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < math.inf:
        raise ConfigError(f"{where}: ready_timeout_s must be a positive number")
    speak_timeout = raw.get("timeout_s", 180.0)
    if (isinstance(speak_timeout, bool) or not isinstance(speak_timeout, (int, float))
            or not 0 < speak_timeout < math.inf):
        raise ConfigError(f"{where}: timeout_s must be a positive number of seconds")
    managed = raw.get("managed", False)
    if not isinstance(managed, bool):
        raise ConfigError(f"{where}: managed must be true or false")
    return VoiceConfig(kind=kind, base_url=base_url, managed=managed, launch=lists["launch"],
                       stop=lists["stop"], ready_timeout_s=float(timeout),
                       timeout_s=float(speak_timeout), **strings)


def load_tier(name: str, tiers_dir: Path = TIERS_DIR) -> Tier:
    # only a plain name listed in tiers_dir: a path here would run another file's launch command
    if not isinstance(name, str) or not TIER_NAME.fullmatch(name) or name not in list_tiers(tiers_dir):
        raise ConfigError(f"unknown tier {name!r}")
    path = Path(tiers_dir) / f"{name}.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from e
    text = _engine(data.get("text"), f"{name}.text")
    fast_raw = data.get("fast", "text")
    fast = text if fast_raw == "text" else _engine(fast_raw, f"{name}.fast")
    gpu_mode = data.get("gpu_mode", "auto")
    if gpu_mode not in GPU_MODES:
        raise ConfigError(f"{name}: gpu_mode must be one of {GPU_MODES}, got {gpu_mode!r}")
    images = _images(data["images"], f"{name}.images") if "images" in data else None
    source_mode = data.get("source_mode", "storyteller")
    if source_mode not in SOURCE_MODES:
        raise ConfigError(f"{name}: source_mode must be one of {SOURCE_MODES}, got {source_mode!r}")
    reference = _reference(data["reference"], f"{name}.reference") if "reference" in data else None
    web = _web(data["web"], f"{name}.web") if "web" in data else WebConfig()
    map_ = _map(data["map"], f"{name}.map") if "map" in data else None
    voice = _voice(data["voice"], f"{name}.voice") if "voice" in data else None
    return Tier(
        name=str(data.get("name", name)),
        text=text,
        fast=fast,
        source_mode=source_mode,
        gpu_mode=gpu_mode,
        images=images,
        reference=reference,
        web=web,
        map=map_,
        voice=voice,
    )


def list_tiers(tiers_dir: Path = TIERS_DIR) -> list[str]:
    return sorted(p.stem for p in Path(tiers_dir).glob("*.toml"))


def read_settings(var_dir: Path) -> dict:
    path = Path(var_dir) / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"settings.json: {e}") from e
    return data if isinstance(data, dict) else {}


def write_settings(var_dir: Path, settings: dict) -> None:
    var_dir = Path(var_dir)
    var_dir.mkdir(parents=True, exist_ok=True)
    (var_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
