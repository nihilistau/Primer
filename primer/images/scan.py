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
SHARED = "shared"  # a scan root's folder of encoders/VAEs several models use
LORA_FOLDER = "loras"
FAST_LORA_HINTS = ("lightx2v", "4step", "turbo", "distill")
MAX_KVS = 2000
MAX_TENSORS = 8192
MAX_TENSOR_NAME = 1024
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


def _wan_in_channels(f, n_tensors: int, kvs_left: int, budget: list) -> int | None:
    """Input channels of a Wan GGUF's patch_embedding (from the tensor index, never the data):
    48 TI2V 5B, 36 I2V A14B, 16 T2V A14B. None when it can't be read (a truncated download)."""
    try:
        for _ in range(kvs_left):
            _take(f, struct.unpack("<Q", _take(f, 8))[0])
            (vtype,) = struct.unpack("<I", _take(f, 4))
            _skip_value(f, vtype, budget)
        for _ in range(min(n_tensors, MAX_TENSORS)):
            n = struct.unpack("<Q", _take(f, 8))[0]
            if n > MAX_TENSOR_NAME:
                return None
            name = _take(f, n)
            (nd,) = struct.unpack("<I", _take(f, 4))
            if nd > 8:
                return None
            dims = struct.unpack(f"<{nd}Q", _take(f, 8 * nd))
            _take(f, 12)  # type, offset
            if name == b"patch_embedding.weight":
                return dims[-2] if nd >= 2 else None  # ggml order: (2, 2, 1, in, dim)
    except (_Bad, struct.error):
        pass
    return None


def _gguf_info(f) -> dict:
    version, n_tensors, kvs = struct.unpack("<IQQ", _take(f, 20))
    if version < 2:
        return {"arch": ""}
    budget = [MAX_ELEMENTS]
    for i in range(min(kvs, MAX_KVS)):
        key = _take(f, struct.unpack("<Q", _take(f, 8))[0]).decode("utf-8", "replace")
        (vtype,) = struct.unpack("<I", _take(f, 4))
        if key == "general.architecture" and vtype == 8:
            n = struct.unpack("<Q", _take(f, 8))[0]
            arch = _take(f, n).decode("utf-8", "replace") if n <= MAX_ARCH else ""
            if arch == "wan" and kvs <= MAX_KVS:
                wan_in = _wan_in_channels(f, n_tensors, kvs - i - 1, budget)
                if wan_in is not None:
                    return {"arch": arch, "wan_in": wan_in}
            return {"arch": arch}
        _skip_value(f, vtype, budget)
    return {"arch": ""}


def _dim(header: dict, key: str, index: int) -> int | None:
    shape = header.get(key, {}).get("shape") if isinstance(header.get(key), dict) else None
    if isinstance(shape, list) and len(shape) > abs(index) and type(shape[index]) is int:
        return shape[index]
    return None


def sniff(path: Path) -> dict:
    """Header-only description of a model file: format, architecture, first tensor names.
    Wan models add wan_in (patch_embedding input channels), Wan VAEs vae_z (latent channels)."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == b"GGUF":
                try:
                    return {"format": "gguf", **_gguf_info(f)}
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
                out = {"format": "safetensors", "arch": arch if isinstance(arch, str) else "",
                       "keys": keys[:64], "n_keys": len(keys)}
                wan_in = (_dim(header, "patch_embedding.weight", 1)
                          or _dim(header, "model.diffusion_model.patch_embedding.weight", 1))
                if wan_in:
                    out["wan_in"] = wan_in
                vae_z = _dim(header, "conv2.bias", 0)
                if vae_z:
                    out["vae_z"] = vae_z
                return out
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
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_"))  # _unused and the like
                         and not _is_link(os.path.join(dirpath, d))) if depth < MAX_DEPTH else []
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            if _is_link(full):
                continue
            real = os.path.realpath(full)
            if os.path.commonpath([real, real_root]) != real_root:
                continue
            yield Path(full)


# Video files as their headers actually read (sniffed 2026-11-02 from D:\Files\Models\video,
# G:\MODELS\video, and Range-fetched header bytes of the Hugging Face files not yet on disk):
#   umt5-xxl-encoder-Q5_K_M.gguf              gguf arch "t5encoder" (tensors enc.blk.*, token_embd)
#   Wan2.2-TI2V-5B-Q6_K.gguf                  gguf arch "wan", patch_embedding.weight (2,2,1,48,3072)
#   Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf     gguf arch "wan", patch_embedding.weight (2,2,1,16,5120)
#   dasiwaWAN22I2V14B_midnightflirt{High,Low}  gguf arch "wan", patch_embedding.weight (2,2,1,36,5120)
#   ltx-2.3-22b-distilled-1.1-Q3_K_M.gguf     gguf arch "ltxv" (plus a "config" KV; transformer_blocks.*)
#   gemma-3-12b-it-abliterated-v2.q4_k_m.gguf gguf arch "gemma3"
#   Wan2.1_VAE / Wan2.2_VAE .safetensors      no arch; keys conv1.*, conv2.*, decoder.conv1.*,
#                                             encoder.*; conv2.bias [16] (2.1) vs [48] (2.2)
#   ltx-2.3-22b-distilled_video_vae           no arch; keys decoder.conv_in.conv.*, per_channel_statistics.*
#   ltx-2.3-22b-distilled_audio_vae           keys audio_vae.*, vocoder.* (not a video VAE: ignored)
#   ..._embeddings_connectors.safetensors     4 keys text_embedding_projection.{audio,video}_aggregate_embed.*
#   LoRAs (lightx2v, NSFW-22-*, Kijai Turbo)  keys diffusion_model.blocks.N.*.lora_down/lora_up (or
#                                             lora_A/lora_B).weight; none carry a modelspec.architecture
_LORA_KEYS = ("lora_down", "lora_up", "lora_A", "lora_B")
_WAN_A14B = {16: "wan2.2-t2v-a14b", 36: "wan2.2-i2v-a14b"}
_WAN_VAE = {16: ("wan2.2-t2v-a14b", "wan2.2-i2v-a14b"), 48: ("wan2.2-ti2v-5b",)}
_LTX = ("ltx-2.3-distilled", "ltx-2.3")
_WAN = ("wan2.2-ti2v-5b", "wan2.2-t2v-a14b", "wan2.2-i2v-a14b")


def _squash(name: str) -> str:
    """Lower case without separators: "Wan2.2-TI2V-5B" -> "wan22ti2v5b"."""
    return "".join(c for c in name.lower() if c.isalnum())


def is_lora(info: dict) -> bool:
    return info["format"] == "safetensors" and any(
        part in k for k in info["keys"] for part in _LORA_KEYS)


def _stage(text: str) -> str | None:
    """high or low from a file or folder name, None when it says neither. "High"/"Low" are
    matched with their capital so "slow_motion" or "highlights" don't pick a stage."""
    low = text.lower()
    if "high_noise" in low or "highnoise" in low or "-H-" in text or "High" in text or "HIGH" in text:
        return "high"
    if "low_noise" in low or "lownoise" in low or "-L-" in text or "Low" in text or "LOW" in text:
        return "low"
    return None


def _wan_family(path: Path, wan_in: int | None) -> str:
    if wan_in == 48:
        return "wan2.2-ti2v-5b"
    if wan_in in _WAN_A14B:
        return _WAN_A14B[wan_in]
    sq = _squash(path.stem)
    if "ti2v" in sq or "5b" in sq:
        return "wan2.2-ti2v-5b"
    return "wan2.2-i2v-a14b" if "i2v" in sq else "wan2.2-t2v-a14b"


def _video_roles(path: Path, info: dict) -> list[tuple[str, str]]:
    name, fmt, arch = path.name.lower(), info["format"], info.get("arch", "")
    sq = _squash(path.stem)
    keys = info.get("keys", [])
    if fmt == "safetensors" and is_lora(info):
        return []
    roles: list[tuple[str, str]] = []
    wan = fmt == "gguf" and arch == "wan"
    if fmt == "safetensors" and "wan" in sq and any(
            k.startswith(("blocks.0.cross_attn.", "model.diffusion_model.blocks.0.cross_attn.")) for k in keys):
        wan = True
    if wan:
        fam = _wan_family(path, info.get("wan_in"))
        if fam == "wan2.2-ti2v-5b":
            roles.append((fam, "diffusion"))
        else:
            stage = _stage(path.stem) or _stage(path.parent.name)
            roles.append((fam, "high_noise" if stage == "high" else "diffusion"))
    if arch == "t5encoder" or (fmt == "safetensors" and "umt5" in sq):
        roles += [(fam, "t5xxl") for fam in _WAN]
    if fmt == "safetensors" and "conv2.weight" in keys and "decoder.conv1.weight" in keys and "qwen" not in sq:
        z = info.get("vae_z") or (16 if "wan21" in sq else 48 if "wan22" in sq else None)
        roles += [(fam, "vae") for fam in _WAN_VAE.get(z, ())]
    if (fmt == "gguf" and arch == "ltxv" and "ltx23" in sq) or (
            fmt == "safetensors" and "ltx23" in sq and "vae" not in sq
            and any(k.startswith(("adaln_single.", "model.diffusion_model.adaln_single.")) for k in keys)):
        roles.append(("ltx-2.3-distilled" if "distilled" in sq else "ltx-2.3", "diffusion"))
    if arch == "gemma3" and "mmproj" not in name:
        roles += [(fam, "llm") for fam in _LTX]
    if fmt == "safetensors" and (any(k.startswith("text_embedding_projection.") for k in keys)
                                 or ("ltx" in sq and "connector" in sq)):
        roles += [(fam, "connectors") for fam in _LTX]
    if fmt == "safetensors" and "audio" not in sq and any(
            k.startswith(("decoder.conv_in.conv.", "per_channel_statistics.")) for k in keys):
        roles += [(fam, "vae") for fam in _LTX]
    return roles


def _roles(path: Path, info: dict) -> list[tuple[str, str]]:
    """The (family, slot) roles a file could fill."""
    name, fmt, arch = path.name.lower(), info["format"], info.get("arch", "")
    if fmt == "other" and name != "tokenizer.json":
        return []
    roles = _video_roles(path, info)
    if "embedding" in name:
        return roles  # LTX's embeddings connectors are a video role; image embeddings are not
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


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _in_sub(c: Path, folder: Path, subs) -> bool:
    """c sits directly in one of folder's subfolders named in subs (any case)."""
    return c.parent.name.lower() in subs and _same(c.parent.parent, folder)


def _at_root(c: Path, root: Path) -> bool:
    """The scan root's own files, its direct vae/tokenizer/text_encoders/shared subfolders,
    and shared's own direct vae/tokenizer/text_encoders subfolders."""
    return (_same(c.parent, root) or _in_sub(c, root, (*SUBFOLDERS, SHARED))
            or (c.parent.parent.name.lower() == SHARED and _same(c.parent.parent.parent, root)
                and c.parent.name.lower() in SUBFOLDERS))


def _pick(diffusion: Path, cands: list[Path], root: Path) -> tuple[Path | None, str]:
    """Nearest companion and where it came from: "" (the model's own folder), "parent", "root".

    Searched: the diffusion file's own folder and its direct vae/tokenizer/text_encoders
    subfolders, then the parent folder's own files and those same direct subfolders, then the
    scan root's own files and its direct shared/vae/text_encoders subfolders (encoders several
    models share). Never a sibling model folder. Subfolder names match in any case."""
    stem = diffusion.stem.lower()
    levels = [(diffusion.parent, "")]
    if not _same(diffusion.parent, root):
        levels.append((diffusion.parent.parent, "parent"))
        if not _same(diffusion.parent.parent, root):
            levels.append((root, "root"))
    for folder, source in levels:
        if _same(folder, root):
            near = [c for c in cands if _at_root(c, root)]
        else:
            near = [c for c in cands if _same(c.parent, folder) or _in_sub(c, folder, SUBFOLDERS)]
        if near:
            best = max(near, key=lambda c: (
                _same(c.parent, folder),
                difflib.SequenceMatcher(None, stem, c.stem.lower()).ratio(), str(c)))
            shared = any(p.name.lower() == SHARED and _same(p.parent, root) for p in best.parents)
            return best, "root" if shared else source
    return None, ""


def _model_folder(diffusion: Path, root: Path) -> Path:
    """The folder a model lives in: the diffusion file's own, or its parent when the file sits in
    a stage subfolder (HighNoise/, LowNoise/)."""
    own = diffusion.parent
    return own.parent if not _same(own, root) and _stage(own.name) else own


def _pair_high(low: Path, cands: list[Path], root: Path) -> Path | None:
    """The high-noise file for a low-noise one: inside the same model folder, preferring the
    name that matches after swapping Low for High, then the most similar name."""
    home = _model_folder(low, root)
    inside = [c for c in cands if any(_same(p, home) for p in c.parents)]
    if not inside:
        return None
    want = low.stem.lower().replace("low", "high")
    return max(inside, key=lambda c: (c.stem.lower() == want,
                                      difflib.SequenceMatcher(None, want, c.stem.lower()).ratio(), str(c)))


def _pick_loras(diffusion: Path, loras: list[Path], root: Path) -> tuple[list[Path], list[str]]:
    """LoRAs from a loras/ (any case) folder beside the diffusion file or in its parent folder,
    the nearest first. sd-server takes one LoRA folder, so the loras folder's own files win,
    else its first subfolder that has any; other folders are named in a note."""
    own = diffusion.parent
    levels = [own] if _same(own, root) else [own, own.parent]
    for folder in levels:
        under = [c for c in loras if _in_sub(c, folder, (LORA_FOLDER,))
                 or (c.parent.parent.name.lower() == LORA_FOLDER and _same(c.parent.parent.parent, folder))]
        if not under:
            continue
        by_dir: dict[str, list[Path]] = {}
        for c in sorted(under, key=str):
            by_dir.setdefault(str(c.parent), []).append(c)
        direct = [d for d in by_dir if by_dir[d][0].parent.name.lower() == LORA_FOLDER]
        chosen = direct[0] if direct else sorted(by_dir)[0]
        notes = [f"LoRAs in {d} not added: a model takes LoRAs from one folder" for d in sorted(by_dir)
                 if d != chosen]
        return by_dir[chosen], notes
    return [], []


def _lora_stage(path: Path, two_stage: bool) -> str:
    return (_stage(path.stem) or "both") if two_stage else "both"


def scan_folder(folder: Path, max_files: int | None = None) -> list[dict]:
    """Propose image and video models (with their LoRAs) from the files under folder
    (depth <= 3, dot- and underscore-folders skipped), looking at no more than max_files files."""
    return scan_folder_capped(folder, max_files)[0]


def scan_folder_capped(folder: Path, max_files: int | None) -> tuple[list[dict], bool]:
    """scan_folder, plus whether files were left unlooked-at because of max_files."""
    root = Path(os.path.abspath(folder))
    if not root.is_dir():
        return [], False
    found: dict[tuple[str, str], list[Path]] = {}
    loras: list[Path] = []
    truncated = False
    for n, path in enumerate(_walk(root)):
        if max_files is not None and n >= max_files:
            truncated = True
            break
        if path.suffix.lower() not in (".gguf", ".safetensors") and path.name.lower() != "tokenizer.json":
            continue
        info = sniff(path)
        if path.suffix.lower() == ".safetensors" and is_lora(info):
            loras.append(path)
            continue
        for role in _roles(path, info):
            found.setdefault(role, []).append(path)
    proposals = []
    for fam_id, fam in FAMILIES.items():
        main = fam.slots[0].name
        for main_file in found.get((fam_id, main), []):
            files = {main: str(main_file)}
            missing, notes = [], []
            for slot in fam.slots[1:]:
                cands = [c for c in found.get((fam_id, slot.name), []) if c != main_file]
                if slot.name == "high_noise":
                    hit, source = _pair_high(main_file, cands, root), ""
                else:
                    hit, source = _pick(main_file, cands, root)
                if hit:
                    files[slot.name] = str(hit)
                    if source == "parent":
                        notes.append(f"{slot.name} taken from the parent folder: {hit}")
                    elif source == "root":
                        notes.append(f"{slot.name} taken from the shared files of the scanned folder: {hit}")
                elif slot.required:
                    missing.append(slot.name)
            picked, lora_notes = _pick_loras(main_file, loras, root) if fam.kind == "video" else ([], [])
            settings = {}
            if fam.fast_defaults and any(h in p.name.lower() for p in picked for h in FAST_LORA_HINTS):
                settings = dict(fam.fast_defaults)
            proposals.append({"name": main_file.stem, "family": fam_id,
                              "files": files, "missing": missing, "notes": notes + lora_notes,
                              "settings": settings,
                              "loras": [{"file": str(p), "strength": 1.0,
                                         "stage": _lora_stage(p, fam.two_stage)} for p in picked]})
    return proposals, truncated
