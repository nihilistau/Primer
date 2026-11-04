"""The user's image and video models (var/images.json), validation and sd-server flags."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .families import FAMILIES, SETTINGS, TWO_STAGE_SETTINGS, VIDEO_MAX_SIZE, VIDEO_SETTINGS

PICKLE_EXTS = {".ckpt", ".pt", ".pth", ".bin", ".pkl"}
NETWORK_PATH = "network and device paths aren't allowed"
MAX_NAME = 120
LORA_EXTS = (".safetensors",)
LORA_STAGES = ("both", "high", "low")
LORA_MAX_STRENGTH = 2.0
ONE_LORA_FOLDER = "all LoRAs must be in one folder"
LORA_VIDEO_ONLY = "LoRAs are only used by video models"
# the store file keeps Phase 2's "default" for images and adds one key per further kind
DEFAULT_KEYS = {"image": "default", "video": "video_default"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
log = logging.getLogger(__name__)
# every ModelStore reads and writes under this lock: read-modify-write stays whole, and on
# Windows os.replace can't hit a file this process has open for reading (a sharing violation)
_LOCK = threading.RLock()
_RETRIES, _RETRY_S = 3, 0.05  # PermissionError from another process (antivirus, indexer)


def _retry(fn):
    """fn(), retried a few times on PermissionError (a transient sharing violation on Windows)."""
    for _ in range(_RETRIES):
        try:
            return fn()
        except PermissionError:
            time.sleep(_RETRY_S)
    return fn()


class StoreCorrupt(Exception):
    """var/images.json exists but cannot be read as a model store."""


@dataclass
class ImageModel:
    id: str
    name: str
    family: str
    files: dict[str, str] = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    # each {"file": absolute .safetensors path, "strength": 0..2, "stage": "both"|"high"|"low"}
    loras: list[dict] = field(default_factory=list)


def kind_of(model: ImageModel) -> str:
    """"image" or "video"; a model whose family is gone counts as an image model, as in Phase 2."""
    fam = FAMILIES.get(model.family)
    return fam.kind if fam else "image"


def _make_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "model"
    salt = hashlib.sha256(f"{name}|{os.urandom(8).hex()}".encode()).hexdigest()[:6]
    return f"{slug}-{salt}"


def clean_settings(family: str, settings: dict) -> dict:
    """Drop unknown keys, clamp to the schema, round sizes down to the family's multiple."""
    fam = FAMILIES.get(family)
    out: dict = {}
    for key, val in (settings or {}).items():
        if key not in SETTINGS:
            continue
        if key in VIDEO_SETTINGS and (fam is None or fam.kind != "video"
                                      or (key in TWO_STAGE_SETTINGS and not fam.two_stage)):
            continue  # not this family's setting: dropped like an unknown key
        typ, lo, hi = SETTINGS[key]
        if key in ("width", "height") and fam and fam.kind == "video":
            hi = VIDEO_MAX_SIZE
        if key == "flow_shift" and val is None:
            out[key] = None
        elif typ is bool:
            if not isinstance(val, bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = val
        elif typ is str:
            if not isinstance(val, str):
                raise ValueError(f"{key} must be text")
            if key == "sampler":
                if val not in lo:
                    raise ValueError(f"sampler must be one of {', '.join(lo)}")
                out[key] = val
            else:
                out[key] = val[:hi]
        else:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"{key} must be a number")
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError(f"{key} must be a finite number")
            if typ is int:
                if isinstance(val, float):
                    if val != int(val):
                        raise ValueError(f"{key} must be a whole number")
                    val = int(val)
                val = max(lo, min(hi, val))
                if key in ("width", "height") and fam:
                    val -= val % fam.size_multiple
                elif key == "frames" and fam.frame_step:  # n*step + 1, rounded down, at least 1
                    val -= (val - 1) % fam.frame_step
                out[key] = val
            else:
                out[key] = float(max(lo, min(hi, val)))
    return out


def _family(model: ImageModel):
    fam = FAMILIES.get(model.family)
    if fam is None:
        raise ValueError(f"unknown family {model.family!r}")
    return fam


def is_network_or_device(path: str) -> bool:
    r"""UNC shares and the Windows device namespaces (\\server\share, \\.\, \\?\, //host)."""
    return path[:2] in ("\\\\", "//", "\\/", "/\\")


def _path_problem(path: str) -> str | None:
    if not isinstance(path, str) or not path:
        return "path must be text"
    if any(c in path for c in "\0\r\n"):
        return "path contains a control character"
    if path.startswith("-"):
        return "path must not start with a dash"
    if is_network_or_device(path):  # a slow SMB host would stall every status poll
        return NETWORK_PATH
    if not os.path.isabs(path):
        return "path must be absolute"
    return None


def _stage_problem(fam, stage) -> str | None:
    if stage not in LORA_STAGES:
        return f"stage must be one of {', '.join(LORA_STAGES)}"
    if stage != "both" and not (fam and fam.two_stage):
        return "stage high or low needs a model with a high-noise stage"
    return None


def _strength_ok(val) -> bool:
    return not isinstance(val, bool) and isinstance(val, (int, float)) and math.isfinite(val)


def _takes_loras(fam) -> bool:
    return fam is not None and fam.kind == "video"


def clean_loras(family: str, loras, *, refuse_image: bool = True) -> list[dict]:
    """Shape-check a model's LoRA list and clamp strengths; ValueError on anything malformed,
    and (unless refuse_image is false, as when loading a stored model) on LoRAs given to an
    image family. Whether the files exist (and share one folder) is validate_model's job."""
    if loras is None:
        return []
    if not isinstance(loras, list):
        raise ValueError("loras must be a list")
    fam = FAMILIES.get(family)
    if loras and refuse_image and fam is not None and not _takes_loras(fam):
        raise ValueError(LORA_VIDEO_ONLY)
    out: list[dict] = []
    for i, lora in enumerate(loras, 1):
        if not isinstance(lora, dict):
            raise ValueError(f"LoRA {i}: must be an object")
        path = lora.get("file")
        problem = _path_problem(path)
        if problem:
            raise ValueError(f"LoRA {i}: {problem}")
        strength = lora.get("strength", 1.0)
        if not _strength_ok(strength):
            raise ValueError(f"LoRA {i}: strength must be a number")
        stage = lora.get("stage", "both")
        problem = _stage_problem(fam, stage)
        if problem:
            raise ValueError(f"LoRA {i}: {problem}")
        out.append({"file": path, "strength": float(max(0, min(LORA_MAX_STRENGTH, strength))),
                    "stage": stage})
    return out


def _lora_reasons(fam, loras) -> list[str]:
    if not isinstance(loras, list):
        return ["loras must be a list"]
    if loras and not _takes_loras(fam):  # sd-server only applies them to video requests here
        return [LORA_VIDEO_ONLY]
    reasons: list[str] = []
    folders: set[str] = set()
    for i, lora in enumerate(loras, 1):
        if not isinstance(lora, dict):
            reasons.append(f"LoRA {i}: must be an object")
            continue
        path = lora.get("file")
        problem = _path_problem(path)
        if problem:
            reasons.append(f"LoRA {i}: {problem} ({path!r})")
            continue
        if not _strength_ok(lora.get("strength", 1.0)):
            reasons.append(f"LoRA {i}: strength must be a number")
        problem = _stage_problem(fam, lora.get("stage", "both"))
        if problem:
            reasons.append(f"LoRA {i}: {problem}")
        ext = Path(path).suffix.lower()
        if ext in PICKLE_EXTS:
            reasons.append(f"LoRA {i}: {ext} refused, pickle files can run code when loaded ({path})")
        elif ext not in LORA_EXTS:
            reasons.append(f"LoRA {i}: extension {ext or '(none)'} not allowed, use {', '.join(LORA_EXTS)} ({path})")
        elif not Path(path).is_file():
            reasons.append(f"LoRA {i}: file not found: {path}")
        elif not os.access(path, os.R_OK):
            reasons.append(f"LoRA {i}: file not readable: {path}")
        folders.add(os.path.normcase(os.path.normpath(os.path.dirname(path))))
    if len(folders) > 1:  # sd-server takes a single --lora-model-dir
        reasons.append(ONE_LORA_FOLDER)
    return reasons


def lora_entries(model: ImageModel) -> list[dict]:
    """The request's lora list: file names relative to --lora-model-dir. A "both" LoRA on a
    two-stage model is sent once per stage."""
    fam = _family(model)
    stages = {"both": (False, True) if fam.two_stage else (False,), "high": (True,), "low": (False,)}
    return [{"path": Path(lora["file"]).name, "multiplier": float(lora["strength"]), "is_high_noise": high}
            for lora in model.loras or [] for high in stages[lora.get("stage", "both")]]


def effective_settings(model: ImageModel, overrides: dict | None = None) -> dict:
    fam = _family(model)
    merged = {**fam.defaults, **(model.settings or {}), **(overrides or {})}
    return clean_settings(model.family, merged)


def validate_model(model: ImageModel) -> list[str]:
    """Reasons the model can't run; empty means usable."""
    fam = FAMILIES.get(model.family)
    if fam is None:
        return [f"unknown family {model.family!r}"]
    reasons: list[str] = []
    for slot in fam.slots:
        path = (model.files or {}).get(slot.name)
        if not path:
            if slot.required:
                reasons.append(f"{slot.name}: a file is required")
            continue
        problem = _path_problem(path)
        if problem:
            reasons.append(f"{slot.name}: {problem} ({path!r})")
            continue
        ext = Path(path).suffix.lower()
        if ext in PICKLE_EXTS:
            reasons.append(f"{slot.name}: {ext} refused, pickle files can run code when loaded ({path})")
        elif ext not in slot.exts:
            reasons.append(f"{slot.name}: extension {ext or '(none)'} not allowed, use {', '.join(slot.exts)} ({path})")
        elif not Path(path).is_file():
            reasons.append(f"{slot.name}: file not found: {path}")
        elif not os.access(path, os.R_OK):
            reasons.append(f"{slot.name}: file not readable: {path}")
    return reasons + _lora_reasons(fam, model.loras)


def build_flags(model: ImageModel) -> list[str]:
    """sd-server startup args from the slots and the model's saved settings."""
    fam = _family(model)
    reasons = validate_model(model)
    if reasons:
        raise ValueError(reasons[0])
    eff = effective_settings(model)
    args: list[str] = []
    for slot in fam.slots:
        path = (model.files or {}).get(slot.name)
        if path:
            args += [slot.flag, path]
    if model.loras and _takes_loras(fam):  # validate_model has checked they share one folder
        args += ["--lora-model-dir", os.path.dirname(model.loras[0]["file"])]
    if eff.get("offload"):
        args.append("--offload-to-cpu")
    if eff.get("vae_tiling"):
        args.append("--vae-tiling")
    if eff.get("flash_attn"):
        args += ["--flash-attn", "off,diffusion=on"]
    if eff.get("flow_shift") is not None:
        args += ["--flow-shift", str(eff["flow_shift"])]
    return args


def _load_entry(raw) -> ImageModel | None:
    if not isinstance(raw, dict):
        log.warning("images.json: skipping non-object entry")
        return None
    mid, name, family = raw.get("id"), raw.get("name"), raw.get("family")
    if not all(isinstance(x, str) and x for x in (mid, name, family)):
        log.warning("images.json: skipping entry without id/name/family: %r", mid or name)
        return None
    files = raw.get("files")
    files = {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(files, dict) else {}
    settings: dict = {}
    if isinstance(raw.get("settings"), dict):
        for k, v in raw["settings"].items():
            try:
                settings.update(clean_settings(family, {k: v}))
            except (ValueError, TypeError):
                log.warning("images.json: dropping invalid setting %r of %r", k, mid)
    loras: list[dict] = []
    for lora in raw["loras"] if isinstance(raw.get("loras"), list) else []:
        try:
            loras += clean_loras(family, [lora], refuse_image=False)  # validate_model says why
        except ValueError:
            log.warning("images.json: dropping invalid LoRA of %r", mid)
    return ImageModel(mid, name, family, files, settings, loras)


def _first_of(models: list[ImageModel], kind: str) -> str | None:
    return next((m.id for m in models if kind_of(m) == kind), None)


class ModelStore:
    def __init__(self, var_dir: Path):
        self.path = Path(var_dir) / "images.json"

    def _backup(self) -> None:
        """Copy the bad file aside (once per distinct content) so it can be recovered."""
        try:
            raw = self.path.read_bytes()
        except OSError:
            return
        for old in self.path.parent.glob(self.path.name + ".bak-*"):
            try:
                if old.read_bytes() == raw:
                    return
            except OSError:
                pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 0
        while True:
            dest = self.path.with_name(f"{self.path.name}.bak-{stamp}" + (f"-{n}" if n else ""))
            if not dest.exists():
                break
            n += 1
        dest.write_bytes(raw)

    def _load(self) -> tuple[list[ImageModel], dict[str, str | None]]:
        """The models and each kind's default ({"image": id|None, "video": id|None})."""
        with _LOCK:
            return self._load_locked()

    def _load_locked(self) -> tuple[list[ImageModel], dict[str, str | None]]:
        try:
            text = _retry(lambda: self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], dict.fromkeys(DEFAULT_KEYS)
        except (OSError, UnicodeDecodeError) as e:
            self._backup()
            raise StoreCorrupt(f"cannot read {self.path.name}: {e}") from e
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            entries = data.get("models", [])
            if not isinstance(entries, list):
                raise ValueError("models is not a list")
        except ValueError as e:
            self._backup()
            raise StoreCorrupt(f"{self.path.name} is not a valid model store: {e}") from e
        models = [m for m in map(_load_entry, entries) if m]
        defaults = {}
        for kind, key in DEFAULT_KEYS.items():  # a default must name a model of its own kind
            mid = data.get(key)
            defaults[kind] = mid if any(m.id == mid and kind_of(m) == kind for m in models) else None
        return models, defaults

    def _write(self, models: list[ImageModel], defaults: dict[str, str | None]) -> None:
        """Call with _LOCK held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"models": [asdict(m) for m in models],
                   **{key: defaults.get(kind) for kind, key in DEFAULT_KEYS.items()}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            _retry(lambda: os.replace(tmp, self.path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self) -> list[ImageModel]:
        return self._load()[0]

    def get(self, id: str) -> ImageModel | None:
        return next((m for m in self.list() if m.id == id), None)

    def save(self, model: ImageModel) -> ImageModel:
        if not isinstance(model.name, str) or not model.name.strip():
            raise ValueError("name is required")
        if len(model.name.strip()) > MAX_NAME:
            raise ValueError(f"name must be at most {MAX_NAME} characters")
        if model.family not in FAMILIES:
            raise ValueError(f"unknown family {model.family!r}")
        if not isinstance(model.files, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in model.files.items()):
            raise ValueError("files must map slot names to paths")
        slots = {s.name for s in FAMILIES[model.family].slots}
        files = {k: v for k, v in model.files.items() if k in slots and v}
        for k, v in files.items():
            problem = _path_problem(v)
            if problem:
                raise ValueError(f"{k}: {problem}")
        mid = model.id if isinstance(model.id, str) and ID_RE.fullmatch(model.id) else _make_id(model.name)
        model = ImageModel(id=mid, name=model.name.strip(), family=model.family, files=files,
                           settings=clean_settings(model.family, model.settings),
                           loras=clean_loras(model.family, model.loras))
        kind = kind_of(model)
        with _LOCK:
            models, defaults = self._load()
            for i, m in enumerate(models):
                if m.id == model.id:
                    models[i] = model
                    break
            else:
                models.append(model)
            for other, cur in defaults.items():  # re-saved into the other kind: that default moves on
                if cur == model.id and other != kind:
                    defaults[other] = _first_of(models, other)
            defaults[kind] = defaults[kind] or model.id  # the first model of a kind becomes its default
            self._write(models, defaults)
        return model

    def update(self, id: str, change: Callable[[ImageModel], ImageModel | None]) -> ImageModel | None:
        """Read the model and save change(model) under the store's lock, so a concurrent save
        isn't lost; change returns None to leave it alone. The save validates as save() does.
        KeyError when there is no such model."""
        with _LOCK:
            model = self.get(id)
            if model is None:
                raise KeyError(id)
            new = change(model)
            return self.save(new) if new is not None else None

    def delete(self, id: str) -> None:
        with _LOCK:
            models, defaults = self._load()
            if id not in {m.id for m in models}:
                raise KeyError(id)
            models = [m for m in models if m.id != id]
            for kind, cur in defaults.items():  # the first remaining model of that kind takes over
                if cur == id:
                    defaults[kind] = _first_of(models, kind)
            self._write(models, defaults)

    def default_id(self, kind: str = "image") -> str | None:
        return self._load()[1].get(kind)

    def set_default(self, id: str) -> None:
        """Make id the default of its own kind; the other kind's default is untouched."""
        with _LOCK:
            models, defaults = self._load()
            model = next((m for m in models if m.id == id), None)
            if model is None:
                raise KeyError(id)
            defaults[kind_of(model)] = id
            self._write(models, defaults)


def can_animate(model: ImageModel) -> bool:
    fam = FAMILIES.get(model.family)
    return bool(fam and fam.kind == "video" and fam.can_animate)


def animate_model(store: ModelStore) -> ImageModel | None:
    """The model an Animate job uses: the video default when it can animate and is usable, else
    the first video model (store order) that can animate and is usable. When none is usable, the
    default if it can animate (so its reason is the one shown), else None. Raises StoreCorrupt."""
    models, defaults = store._load()
    default = next((m for m in models if m.id == defaults.get("video")), None)
    default_animates = default is not None and can_animate(default)
    if default_animates and not validate_model(default):
        return default
    usable = next((m for m in models if can_animate(m) and not validate_model(m)), None)
    return usable or (default if default_animates else None)


def only_animates_reason(fam) -> str:
    """Why a text-to-video job can't run on a family that needs a start image."""
    return f"{fam.label} only animates a picture: use Animate on an image"


def available_for(store: ModelStore, kind: str) -> tuple[bool, str]:
    """Whether a job of this kind can run on the default model, else the reason it can't."""
    try:
        models, defaults = store._load()
    except StoreCorrupt as e:
        return False, str(e)
    if not any(kind_of(m) == kind for m in models):
        return False, f"no {kind} model added yet"
    model = next((m for m in models if m.id == defaults.get(kind)), None)
    if model is None:
        return False, f"no default {kind} model chosen"
    fam = FAMILIES.get(model.family)
    if kind == "video" and fam is not None and fam.needs_init:
        return False, only_animates_reason(fam)
    reasons = validate_model(model)
    return (False, reasons[0]) if reasons else (True, "")
