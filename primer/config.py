"""Tier presets (tiers/*.toml) and the small settings file in var/."""
from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
TIERS_DIR = REPO_ROOT / "tiers"
KINDS = ("openai", "sp", "sdserver")
GPU_MODES = ("auto", "manual")
TIER_NAME = re.compile(r"[A-Za-z0-9_-]+")


class ConfigError(Exception):
    pass


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
class Tier:
    name: str
    text: EngineSpec
    fast: EngineSpec
    source_mode: str = "storyteller"
    gpu_mode: Literal["auto", "manual"] = "auto"
    images: ImagesConfig | None = None


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
    return Tier(
        name=str(data.get("name", name)),
        text=text,
        fast=fast,
        source_mode=str(data.get("source_mode", "storyteller")),
        gpu_mode=gpu_mode,
        images=images,
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
