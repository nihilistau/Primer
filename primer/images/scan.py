"""Find image models in a folder by reading file headers (never tensors)."""
from __future__ import annotations

import difflib
import json
import os
import stat
import struct
from pathlib import Path

from .families import FAMILIES

MAX_HEADER = 64 * 1024 * 1024
MAX_ST_HEADER = 8 * 1024 * 1024   # real safetensors headers are well under 1 MiB
MAX_ARCH = 256
MAX_ELEMENTS = 1_000_000          # legit vocab arrays are ~150k
MAX_ARRAY_DEPTH = 4
SUBFOLDERS = ("vae", "tokenizer", "text_encoders")
MAX_KVS = 2000
MAX_DEPTH = 3
_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


class _Bad(Exception):
    pass


def _take(f, n: int) -> bytes:
    if n < 0 or f.tell() + n > MAX_HEADER:
        raise _Bad("header too large")
    b = f.read(n)
    if len(b) != n:
        raise _Bad("truncated")
    return b


def _skip_value(f, vtype: int, budget: list, depth: int = 0) -> None:
    if vtype in _FIXED:
        _take(f, _FIXED[vtype])
    elif vtype == 8:
        _take(f, struct.unpack("<Q", _take(f, 8))[0])
    elif vtype == 9:
        if depth >= MAX_ARRAY_DEPTH:
            raise _Bad("arrays nested too deep")
        etype, n = struct.unpack("<IQ", _take(f, 12))
        minsize = _FIXED.get(etype, 8 if etype == 8 else 12 if etype == 9 else None)
        if minsize is None or n > (MAX_HEADER - f.tell()) // minsize:
            raise _Bad("array larger than the header")
        budget[0] -= n
        if budget[0] < 0:
            raise _Bad("too many array elements")
        if etype in _FIXED:
            _take(f, _FIXED[etype] * n)
        else:  # strings and nested arrays: element by element
            for _ in range(n):
                _skip_value(f, etype, budget, depth + 1)
    else:
        raise _Bad(f"unknown value type {vtype}")


def _gguf_arch(f) -> str:
    version, _tensors, kvs = struct.unpack("<IQQ", _take(f, 20))
    if version < 2:
        return ""
    budget = [MAX_ELEMENTS]
    for _ in range(min(kvs, MAX_KVS)):
        key = _take(f, struct.unpack("<Q", _take(f, 8))[0]).decode("utf-8", "replace")
        (vtype,) = struct.unpack("<I", _take(f, 4))
        if key == "general.architecture" and vtype == 8:
            n = struct.unpack("<Q", _take(f, 8))[0]
            return _take(f, n).decode("utf-8", "replace") if n <= MAX_ARCH else ""
        _skip_value(f, vtype, budget)
    return ""


def sniff(path: Path) -> dict:
    """Header-only description of a model file: format, architecture, first tensor names."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == b"GGUF":
                try:
                    return {"format": "gguf", "arch": _gguf_arch(f)}
                except (_Bad, struct.error):
                    return {"format": "gguf", "arch": ""}
            if path.suffix.lower() == ".safetensors":
                f.seek(0)
                (n,) = struct.unpack("<Q", _take(f, 8))
                if n > MAX_ST_HEADER:
                    return {"format": "other"}
                header = json.loads(_take(f, n))
                if not isinstance(header, dict):
                    return {"format": "other"}
                meta = header.pop("__metadata__", None)
                arch = meta.get("modelspec.architecture", "") if isinstance(meta, dict) else ""
                keys = list(header)
                return {"format": "safetensors", "arch": arch if isinstance(arch, str) else "",
                        "keys": keys[:64], "n_keys": len(keys)}
    except (OSError, _Bad, struct.error, ValueError, RecursionError, MemoryError):
        pass
    return {"format": "other"}


def _is_link(p: str) -> bool:
    """Symlinks and Windows reparse points (junctions): never followed."""
    try:
        if os.path.islink(p):
            return True
        attrs = getattr(os.lstat(p), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _walk(root: Path):
    real_root = os.path.realpath(root)
    for dirpath, dirs, files in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and not _is_link(os.path.join(dirpath, d))) if depth < MAX_DEPTH else []
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            if _is_link(full):
                continue
            real = os.path.realpath(full)
            if os.path.commonpath([real, real_root]) != real_root:
                continue
            yield Path(full)


def _roles(path: Path, info: dict) -> list[tuple[str, str]]:
    """The (family, slot) roles a file could fill."""
    name, fmt, arch = path.name.lower(), info["format"], info.get("arch", "")
    if "embedding" in name:
        return []
    roles = []
    if fmt == "safetensors":
        keys = info["keys"]
        if "stable-diffusion-xl" in arch or any(k.startswith("conditioner.embedders.") for k in keys):
            roles.append(("sdxl", "model"))
        if "qwen-image-2.1" in name and any(k.startswith("transformer_blocks.") for k in keys):
            roles.append(("qwen-image-2.1", "diffusion"))
        if arch == "qwen_image_2.1_vae" or "qwen_image_2.1_vae" in name:
            roles.append(("qwen-image-2.1", "vae"))
        if path.parent.name.lower() == "vae" and name == "diffusion_pytorch_model.safetensors":
            roles.append(("moss-image", "vae"))
    elif fmt == "gguf":
        if arch == "qwen_image21" or (arch == "" and "qwen-image-2.1" in name):
            roles.append(("qwen-image-2.1", "diffusion"))
        if arch == "moss_image":
            roles.append(("moss-image", "diffusion"))
        if "qwen3.5" in name and "mmproj" not in name:
            roles.append(("moss-image", "llm"))
    if fmt in ("gguf", "safetensors") and "mmproj" not in name and ("qwen3vl" in name or "qwen3-vl" in name):
        roles.append(("qwen-image-2.1", "llm"))
    if name == "tokenizer.json":
        roles.append(("moss-image", "tokenizer"))
    return roles


def _pick(diffusion: Path, cands: list[Path], root: Path) -> tuple[Path | None, bool]:
    """Nearest companion and whether it came from the parent folder.

    Searched: the diffusion file's own folder and its direct vae/tokenizer/text_encoders
    subfolders, then the parent folder's own files and those same direct subfolders.
    Never a sibling model folder."""
    stem = diffusion.stem.lower()
    levels = [(diffusion.parent, False)]
    if diffusion.parent != root:
        levels.append((diffusion.parent.parent, True))
    for folder, from_parent in levels:
        allowed = {folder, *(folder / sub for sub in SUBFOLDERS)}
        near = [c for c in cands if c.parent in allowed]
        if near:
            best = max(near, key=lambda c: (
                c.parent == folder,
                difflib.SequenceMatcher(None, stem, c.stem.lower()).ratio(), str(c)))
            return best, from_parent
    return None, False


def scan_folder(folder: Path, max_files: int | None = None) -> list[dict]:
    """Propose image models from the files under folder (depth <= 3, dot-folders skipped),
    looking at no more than max_files files."""
    return scan_folder_capped(folder, max_files)[0]


def scan_folder_capped(folder: Path, max_files: int | None) -> tuple[list[dict], bool]:
    """scan_folder, plus whether files were left unlooked-at because of max_files."""
    root = Path(os.path.abspath(folder))
    if not root.is_dir():
        return [], False
    found: dict[tuple[str, str], list[Path]] = {}
    truncated = False
    for n, path in enumerate(_walk(root)):
        if max_files is not None and n >= max_files:
            truncated = True
            break
        if path.suffix.lower() not in (".gguf", ".safetensors") and path.name.lower() != "tokenizer.json":
            continue
        for role in _roles(path, sniff(path)):
            found.setdefault(role, []).append(path)
    proposals = []
    for fam_id, fam in FAMILIES.items():
        main = fam.slots[0].name
        for main_file in found.get((fam_id, main), []):
            files = {main: str(main_file)}
            missing, notes = [], []
            for slot in fam.slots[1:]:
                hit, from_parent = _pick(
                    main_file, [c for c in found.get((fam_id, slot.name), []) if c != main_file], root)
                if hit:
                    files[slot.name] = str(hit)
                    if from_parent:
                        notes.append(f"{slot.name} taken from the parent folder: {hit}")
                elif slot.required:
                    missing.append(slot.name)
            proposals.append({"name": main_file.stem, "family": fam_id,
                              "files": files, "missing": missing, "notes": notes})
    return proposals, truncated
