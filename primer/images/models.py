"""The user's image models (var/images.json), validation and sd-server flags."""
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

from .families import FAMILIES, SETTINGS

PICKLE_EXTS = {".ckpt", ".pt", ".pth", ".bin", ".pkl"}
NETWORK_PATH = "network and device paths aren't allowed"
MAX_NAME = 120
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
        typ, lo, hi = SETTINGS[key]
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
    return reasons


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
    return ImageModel(mid, name, family, files, settings)


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

    def _load(self) -> tuple[list[ImageModel], str | None]:
        with _LOCK:
            return self._load_locked()

    def _load_locked(self) -> tuple[list[ImageModel], str | None]:
        try:
            text = _retry(lambda: self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], None
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
        default = data.get("default")
        if default not in {m.id for m in models}:
            default = None
        return models, default

    def _write(self, models: list[ImageModel], default: str | None) -> None:
        """Call with _LOCK held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"models": [asdict(m) for m in models], "default": default}
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
                           settings=clean_settings(model.family, model.settings))
        with _LOCK:
            models, default = self._load()
            for i, m in enumerate(models):
                if m.id == model.id:
                    models[i] = model
                    break
            else:
                models.append(model)
            self._write(models, default or model.id)  # the first model becomes the default
        return model

    def delete(self, id: str) -> None:
        with _LOCK:
            models, default = self._load()
            if id not in {m.id for m in models}:
                raise KeyError(id)
            models = [m for m in models if m.id != id]
            if default == id:  # the first remaining model takes over, if any
                default = models[0].id if models else None
            self._write(models, default)

    def default_id(self) -> str | None:
        return self._load()[1]

    def set_default(self, id: str) -> None:
        with _LOCK:
            models, _ = self._load()
            if id not in {m.id for m in models}:
                raise KeyError(id)
            self._write(models, id)
