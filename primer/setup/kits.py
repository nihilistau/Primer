"""Starter kits: the curated manifest (kits.json) resolved against the live catalogues into a Plan,
what is already on disk, and whether a plan fits the free space.

A Plan item says where its file goes (`dest`, POSIX-style, relative to the destination root),
where it comes from (`url`) and how it is checked. Exactly one of these carries the check:

- `sha256`: the publisher's SHA-256, known when resolving (Hugging Face LFS, GitHub digests, the
  pinned fonts zip);
- `sha256_url` / `md5_url`: a small text file the installer fetches just before downloading
  (Kiwix ZIMs publish `<url>.sha256`; kiwix-tools only `<url>.md5`), so resolving stays cheap;
- none, for the map alone: tools/map_extract.py cuts it from a Protomaps build, and its size is an
  estimate (`size_estimate`).

Runtimes and the fonts are zips (`archive`) extracted into the `dest` folder. The installer's
contract (Task 4): extract each zip into a fresh staging folder, merge it into `dest` without
overwriting any existing file, and only once the whole item is in place record it in
`dest/.primer-setup.json` as `{"items": {"<item id>": "<zip sha256>", ...}}`. Several items may
share one `dest` (the CUDA runtime DLLs sit beside llama-server), so the record is per item id.
An extracted item is present only when that record names it (and, for a runtime, its `exe`
exists). `marker` is a file the item is expected to leave in `dest`, for checking by hand.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from primer.hardware import Hardware, suggest_tier
from primer.setup.net import CatalogueError

KITS_FILE = Path(__file__).with_name("kits.json")
MARGIN = 2 * 1024 ** 3  # free space kept back beyond the kit itself
MAP_WORKING_FACTOR = 3  # map_extract needs 2-3x the finished file's size while it runs
ZIP_WORKING_FACTOR = 4  # a zip, plus up to 3x its size once extracted
SETUP_MARKER = ".primer-setup.json"  # in an extracted item's dest; see the module docstring

MODEL_ROLES = ("text", "fast", "image", "image_encoder", "image_vae", "video", "video_encoder",
               "video_vae", "voice", "voice_mmproj")
RUNTIME_ROLES = ("runtime_llama", "runtime_llama_cudart", "runtime_sd", "runtime_sd_cudart", "runtime_kiwix")
ROLES = frozenset(MODEL_ROLES + RUNTIME_ROLES + ("reference", "map", "fonts"))
KIND = {**{r: "model" for r in MODEL_ROLES}, **{r: "runtime" for r in RUNTIME_ROLES},
        "reference": "zim", "map": "map", "fonts": "fonts"}
SOURCE_TYPES = {"model": ("hf",), "runtime": ("github", "url"), "zim": ("zim",), "map": ("map",),
                "fonts": ("archive",)}
ALLOWED_EXT = {"model": (".gguf", ".safetensors"), "zim": (".zim",), "map": (".pmtiles",),
               "runtime": (".zip",), "fonts": (".zip",)}
REFUSED_EXT = (".ckpt", ".pt", ".pth", ".bin", ".pkl")

_ID = re.compile(r"[a-z][a-z0-9_-]*")
_REPO = re.compile(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*")
_SEGMENT = re.compile(r"[A-Za-z0-9][\w.+@ -]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class KitError(ValueError):
    """A broken manifest, an unknown kit or choice."""


# ── the plan ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class PlanItem:
    id: str
    kind: str  # model, zim, runtime, map, fonts
    role: str
    label: str
    dest: str  # relative to the destination root; folders (extracted zips) end with "/"
    url: str | None = None
    size: int = 0
    sha256: str | None = None
    md5: str | None = None
    sha256_url: str | None = None
    md5_url: str | None = None
    licence: str = ""
    licence_link: str = ""
    gated: bool | str = False  # False, "auto" or "manual" (Hugging Face)
    present: bool = False
    choice: str = ""
    choices: list = field(default_factory=list)  # [{id, label, size}] to swap to
    origin: dict = field(default_factory=dict)  # repo / file / tag / book, for display
    allowed_ext: tuple = ()
    archive: str | None = None  # the zip's file name, for runtimes and fonts
    marker: str | None = None  # inside dest: a file the item is expected to leave there
    exe: str | None = None  # inside dest: the executable a runtime provides
    assets_subdir: str | None = None  # fonts: the folder holding fonts/ and sprites/
    symlinks: str | None = None  # fonts: "skip" the zip's symlinks instead of refusing it
    maxzoom: int | None = None  # map
    size_estimate: int | None = None  # map
    gpu: bool = False  # the writing model runs on the GPU
    use_path: str | None = None  # llama-server found on the PATH instead of downloading
    note: str = ""
    error: str = ""  # could not be resolved (the catalogue failed or the file is gone)

    @property
    def size_exact(self) -> bool:
        """False when the catalogue gives the size only roughly (Kiwix rounds up to the next KiB):
        the size is then for planning, and the hash alone decides whether the file is right."""
        return self.kind != "zim"

    def to_json(self) -> dict:
        d = asdict(self)
        d["allowed_ext"] = list(self.allowed_ext)
        return d


@dataclass
class Plan:
    kit: str
    label: str
    items: list[PlanItem]
    skipped: list[str] = field(default_factory=list)
    checked: str = ""
    error: str = ""  # the plan as a whole can't work (e.g. no writing model: no tier); see Installer

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def to_download(self) -> int:
        return sum(i.size for i in self.items if not i.present)

    @property
    def ok(self) -> bool:
        return not self.error and not any(i.error for i in self.items)

    def licences(self) -> list[dict]:
        """The distinct licences of what will be downloaded, by name and link: "other" for two
        repos is two sets of terms. `key` is what accepting one records (licence_key)."""
        seen: dict[str, dict] = {}
        for i in self.items:
            if not i.present and i.licence:
                key = licence_key(i.licence, i.licence_link)
                seen.setdefault(key, {"licence": i.licence, "link": i.licence_link, "key": key})
        return list(seen.values())

    def to_json(self) -> dict:
        return {"kit": self.kit, "label": self.label, "items": [i.to_json() for i in self.items],
                "skipped": list(self.skipped), "total": self.total, "to_download": self.to_download,
                "ok": self.ok, "licences": self.licences(), "checked": self.checked, "error": self.error}


def licence_key(licence: str, link: str) -> str:
    """The identity of one set of terms: the licence name and where its text is."""
    return f"{licence} <{link}>" if link else licence


# ── the manifest ──────────────────────────────────────────────────────────────────────────────


def _need(cond: bool, where: str, message: str) -> None:
    if not cond:
        raise KitError(f"{where}: {message}")


def _safe_rel(path: object, where: str) -> None:
    _need(isinstance(path, str) and path and "\\" not in path, where, "a relative path is required")
    parts = PurePosixPath(path).parts
    _need(not path.startswith("/") and all(_SEGMENT.fullmatch(p) for p in parts),
          where, f"unsafe path {path!r}")


def _ext_ok(name: str, kind: str, where: str) -> None:
    low = name.lower()
    _need(not low.endswith(REFUSED_EXT), where, f"{name}: format not allowed (it can run code)")
    _need(low.endswith(ALLOWED_EXT[kind]), where, f"{name}: format not expected for a {kind}")


def _https(url: object, where: str) -> None:
    _need(isinstance(url, str) and urllib.parse.urlsplit(url).scheme == "https", where,
          "an https:// address is required")


def validate(data: object) -> dict:
    """Check the manifest's shape; returns it, or raises KitError naming the problem."""
    _need(isinstance(data, dict), "kits.json", "expected an object")
    _need(isinstance(data.get("_checked"), str), "kits.json", "_checked date missing")
    est = data.get("map_estimates")
    _need(isinstance(est, dict) and all(isinstance(v, int) and v > 0 for v in est.values()),
          "kits.json", "map_estimates must map zoom levels to byte counts")
    kits = data.get("kits")
    _need(isinstance(kits, list) and kits, "kits.json", "no kits")
    kit_ids = set()
    for kit in kits:
        _need(isinstance(kit, dict) and isinstance(kit.get("id"), str) and _ID.fullmatch(kit["id"]),
              "kits.json", "every kit needs an id")
        kw = f"kit {kit['id']}"
        _need(kit["id"] not in kit_ids, kw, "duplicate kit id")
        kit_ids.add(kit["id"])
        _need(isinstance(kit.get("label"), str) and kit["label"], kw, "label missing")
        span = kit.get("for")
        _need(isinstance(span, dict) and isinstance(span.get("min_vram_gb"), (int, float))
              and (span.get("max_vram_gb") is None or isinstance(span["max_vram_gb"], (int, float))),
              kw, "for: {min_vram_gb, max_vram_gb} required")
        items = kit.get("items")
        _need(isinstance(items, list) and items, kw, "no items")
        seen: dict[str, dict] = {}
        for item in items:
            _validate_item(item, kw, seen, est)
            seen[item["id"]] = item
    return data


def _validate_item(item: object, kw: str, seen: dict, est: dict) -> None:
    _need(isinstance(item, dict) and isinstance(item.get("id"), str) and _ID.fullmatch(item["id"]),
          kw, "every item needs an id")
    w = f"{kw}, {item['id']}"
    _need(item["id"] not in seen, w, "duplicate item id")
    _need(item.get("role") in ROLES, w, f"unknown role {item.get('role')!r}")
    kind = KIND[item["role"]]
    _need(isinstance(item.get("label"), str) and item["label"], w, "label missing")
    _need(isinstance(item.get("gpu", False), bool), w, "gpu must be true or false")
    src = item.get("source")
    _need(isinstance(src, dict) and src.get("type") in SOURCE_TYPES[kind], w,
          f"source type must be one of {SOURCE_TYPES[kind]}")
    choices = item.get("choices")
    _need(isinstance(choices, list) and choices and all(isinstance(c, dict) for c in choices),
          w, "choices must be a non-empty list")
    ids = [c.get("id") for c in choices]
    _need(all(isinstance(i, str) and _ID.fullmatch(i) for i in ids) and len(set(ids)) == len(ids),
          w, "choice ids must be unique")
    _need(item.get("default") in ids + ["skip"], w, f"default must be a choice id or 'skip'")
    for other, want in (item.get("requires") or {}).items():
        _need(other in seen and want in [c["id"] for c in seen[other]["choices"]], w,
              f"requires an earlier item {other!r} with choice {want!r}")
    t = src["type"]
    if t != "hf":
        _need(isinstance(src.get("licence"), str) and src["licence"], w, "licence missing")
    if t == "hf":
        _need(isinstance(src.get("repo"), str) and _REPO.fullmatch(src["repo"]), w, "bad repo")
        for c in choices:
            _safe_rel(c.get("file"), w)
            _ext_ok(c["file"], kind, w)
    elif t == "zim":
        _need(isinstance(src.get("name"), str) and src["name"], w, "book name missing")
        _need(all(isinstance(c.get("flavour"), str) for c in choices), w, "flavour missing")
    elif t == "github":
        _need(isinstance(src.get("repo"), str) and _REPO.fullmatch(src["repo"]), w, "bad repo")
        if "beside" in src:
            _need(src["beside"] in seen and isinstance(src.get("marker"), str), w,
                  "beside must name an earlier item, with a marker file")
            _safe_rel(src["marker"], w)
        else:
            _safe_rel(src.get("tool"), w)
            _safe_rel(src.get("exe"), w)
        for c in choices:
            pat = c.get("pattern")
            _need(isinstance(pat, str) and pat.endswith(r"\.zip$"), w, f"{pat!r}: format must be a .zip")
            try:
                re.compile(pat)
            except re.error:
                raise KitError(f"{w}: bad pattern {pat!r}") from None
    elif t == "url":
        _safe_rel(src.get("tool"), w)
        _safe_rel(src.get("release"), w)
        _safe_rel(src.get("exe"), w)
        for c in choices:
            _https(c.get("url"), w)
            _ext_ok(urllib.parse.urlsplit(c["url"]).path, kind, w)
            _need(isinstance(c.get("size"), int) and c["size"] > 0, w, "size required")
            hashes = [k for k in ("sha256", "md5_url", "sha256_url") if c.get(k)]
            _need(len(hashes) == 1, w, "exactly one of sha256, sha256_url, md5_url")
            for k in ("md5_url", "sha256_url"):
                if c.get(k):
                    _https(c[k], w)
    elif t == "map":
        for c in choices:
            _need(isinstance(c.get("maxzoom"), int) and str(c["maxzoom"]) in est, w,
                  "maxzoom needs an entry in map_estimates")
    elif t == "archive":
        _safe_rel(src.get("assets_subdir"), w)
        _safe_rel(src.get("marker"), w)
        _need(src.get("symlinks") in (None, "skip"), w, "symlinks may only be 'skip'")
        for c in choices:
            _https(c.get("url"), w)
            _need(isinstance(c.get("size"), int) and c["size"] > 0, w, "size required")
            sha = c.get("sha256")
            _need(sha is None and isinstance(src.get("commit"), str)
                  or isinstance(sha, str) and _HEX64.fullmatch(sha), w,
                  "sha256 (64 hex) or a pinned commit required")


def load_kits(path: Path = KITS_FILE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise KitError(f"kits.json: {e}") from None
    return validate(data)


def _kit(data: dict, kit_id: str) -> dict:
    for kit in data["kits"]:
        if kit["id"] == kit_id:
            return kit
    raise KitError(f"unknown kit {kit_id!r}")


def _active(kit: dict, picks: dict) -> list[tuple[dict, dict]]:
    """(item, chosen choice) for every item not skipped and whose requirements hold."""
    out = []
    for item in kit["items"]:
        pick = picks[item["id"]]
        if pick == "skip":
            continue
        if any(picks.get(other) != want for other, want in (item.get("requires") or {}).items()):
            continue
        out.append((item, next(c for c in item["choices"] if c["id"] == pick)))
    return out


def _picks(kit: dict, choices: dict | None) -> dict:
    picks = {i["id"]: i["default"] for i in kit["items"]}
    for item_id, pick in (choices or {}).items():
        item = next((i for i in kit["items"] if i["id"] == item_id), None)
        if item is None:
            raise KitError(f"unknown item {item_id!r} in kit {kit['id']}")
        if pick != "skip" and pick not in [c["id"] for c in item["choices"]]:
            raise KitError(f"{item_id} has no choice {pick!r}")
        picks[item_id] = pick
    return picks


def kit_size_hint(kit: dict, data: dict | None = None) -> int:
    """The kit's default size from the sizes recorded in the manifest (no network)."""
    est = (data or load_kits())["map_estimates"]
    total = 0
    for item, ch in _active(kit, _picks(kit, None)):
        total += est[str(ch["maxzoom"])] if "maxzoom" in ch else ch.get("size_hint", 0)
    return total


# ── resolving ─────────────────────────────────────────────────────────────────────────────────


class _Cached:
    """One listing per repo, one card per repo, the ZIM catalogue once, one lookup per asset."""

    def __init__(self, cat):
        self.cat, self.memo = cat, {}

    def get(self, key, fn):
        if key not in self.memo:
            try:
                self.memo[key] = ("ok", fn())
            except CatalogueError as e:
                self.memo[key] = ("err", str(getattr(e, "reason", "") or e))
        state, value = self.memo[key]
        if state == "err":
            raise CatalogueError(value)
        return value

    def files(self, repo):
        return self.get(("files", repo), lambda: self.cat.hf_files(repo))

    def card(self, repo):
        return self.get(("card", repo), lambda: self.cat.hf_card(repo))

    def zims(self):
        return self.get(("zims",), lambda: self.cat.zims())

    def asset(self, repo, pattern):
        return self.get(("asset", repo, pattern), lambda: self.cat.latest_asset(repo, pattern))


def _hf_item(base: PlanItem, item: dict, ch: dict, cat: _Cached) -> None:
    repo, file = item["source"]["repo"], ch["file"]
    publisher, name = repo.split("/")
    base.dest = f"models/{publisher}/{name}/{file}"
    base.origin = {"repo": repo, "file": file}
    listing = {f["path"]: f for f in cat.files(repo)}
    base.choices = [{"id": c["id"], "label": c["label"],
                     "size": listing[c["file"]]["size"] if c["file"] in listing else None}
                    for c in item["choices"]]
    found = listing.get(file)
    if found is None:
        raise CatalogueError(f"{file} is no longer listed in {repo}")
    if not found.get("sha256"):
        raise CatalogueError(f"{file} has no published SHA-256")
    base.url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(file, safe='/')}"
    base.size, base.sha256 = int(found["size"]), found["sha256"]
    card = cat.card(repo)
    base.licence = card.get("license") or "unknown"
    base.licence_link = card.get("license_link") or f"https://huggingface.co/{repo}"
    base.gated = card.get("gated", False)


def _zim_item(base: PlanItem, item: dict, ch: dict, cat: _Cached) -> None:
    book, flavour = item["source"]["name"], ch["flavour"]
    base.origin = {"book": book, "flavour": flavour}
    base.choices = [{"id": c["id"], "label": c["label"], "size": c.get("size_hint")} for c in item["choices"]]
    found = sorted((z for z in cat.zims() if z["name"] == book and z["flavour"] == flavour),
                   key=lambda z: z.get("date", ""))
    if not found:
        raise CatalogueError(f"{book} ({flavour}) is not in the Kiwix catalogue")
    z = found[-1]
    fname = PurePosixPath(urllib.parse.urlsplit(z["url"]).path).name
    if not _SEGMENT.fullmatch(fname) or not fname.endswith(".zim"):
        raise CatalogueError(f"unexpected ZIM address {z['url']}")
    base.dest = f"reference/{fname}"
    base.url, base.size, base.sha256_url = z["url"], int(z["size"]), z["sha256_url"]
    base.origin["date"] = z.get("date", "")


def _github_item(base: PlanItem, item: dict, ch: dict, cat: _Cached, placed: dict) -> None:
    src = item["source"]
    base.choices = [{"id": c["id"], "label": c["label"], "size": c.get("size_hint")} for c in item["choices"]]
    a = cat.asset(src["repo"], ch["pattern"])
    name, tag = a["name"], a["tag"]
    if not re.search(ch["pattern"], name) or not name.lower().endswith(".zip"):
        raise CatalogueError(f"unexpected asset {name}")
    if not _SEGMENT.fullmatch(tag):
        raise CatalogueError(f"unexpected release tag {tag!r}")
    if "beside" in src:  # e.g. the CUDA DLLs, unpacked next to the executables
        # sharing dest is safe: the installer merges without overwriting and records each item
        # by id in dest/.primer-setup.json (see the module docstring)
        owner = placed.get(src["beside"])
        if owner is None:
            raise CatalogueError(f"needs {src['beside']}")
        base.dest, base.marker = owner.dest, src["marker"]
    else:
        base.dest = f"tools/{src['tool']}/{tag}/"
        base.exe = base.marker = src["exe"]
    base.url, base.size, base.sha256, base.archive = a["url"], int(a["size"]), a["sha256"], name
    base.origin = {"repo": src["repo"], "tag": tag, "asset": name}


def resolve(kit_id: str, catalogues, have: Callable[[PlanItem], bool], choices: dict | None = None,
            *, llama_server: str | None = None, manifest: dict | None = None) -> Plan:
    """The kit as a Plan of exact files. `choices[item_id]` is a choice id or "skip".
    `have(item)` says whether an item is already on disk. `llama_server`, when given (see
    which_llama_server), stands in for the llama.cpp download."""
    data = manifest if manifest is not None else load_kits()
    kit = _kit(data, kit_id)
    picks = _picks(kit, choices)
    cat = _Cached(catalogues)
    est = data["map_estimates"]
    items: list[PlanItem] = []
    placed: dict[str, PlanItem] = {}
    for item, ch in _active(kit, picks):
        src, kind = item["source"], KIND[item["role"]]
        base = PlanItem(id=item["id"], kind=kind, role=item["role"], label=item["label"], dest="",
                        choice=ch["id"], allowed_ext=ALLOWED_EXT[kind], gpu=item.get("gpu", False),
                        licence=src.get("licence", ""), licence_link=src.get("licence_link", ""))
        try:
            if src["type"] == "hf":
                _hf_item(base, item, ch, cat)
            elif src["type"] == "zim":
                _zim_item(base, item, ch, cat)
            elif src["type"] == "github":
                _github_item(base, item, ch, cat, placed)
            elif src["type"] == "url":
                base.dest = f"tools/{src['tool']}/{src['release']}/"
                base.exe = base.marker = src["exe"]
                base.url, base.size = ch["url"], ch["size"]
                base.sha256, base.sha256_url, base.md5_url = ch.get("sha256"), ch.get("sha256_url"), ch.get("md5_url")
                base.archive = PurePosixPath(urllib.parse.urlsplit(ch["url"]).path).name
                base.origin = {"url": ch["url"], "release": src["release"]}
            elif src["type"] == "map":
                z = ch["maxzoom"]
                base.dest, base.maxzoom = f"maps/world-z{z}.pmtiles", z
                base.size = base.size_estimate = est[str(z)]
                base.note = "cut from the newest Protomaps world build by tools/map_extract.py"
            elif src["type"] == "archive":
                base.dest = "maps/basemaps-assets/"
                base.url, base.size, base.sha256 = ch["url"], ch["size"], ch.get("sha256")
                base.archive = f"basemaps-assets-{src.get('commit', ch['id'])}.zip"
                base.assets_subdir, base.marker = src["assets_subdir"], src["marker"]
                base.symlinks = src.get("symlinks")
                base.note = src.get("licence_note", "")
                base.origin = {"repo": src["repo"], "commit": src.get("commit", "")}
            if not base.choices:
                base.choices = [{"id": c["id"], "label": c["label"], "size": c.get("size_hint")}
                                for c in item["choices"]]
        except CatalogueError as e:
            base.error = str(e) or "the catalogue did not answer"
            base.size = 0
        placed[item["id"]] = base
        items.append(base)

    for it in items:
        if it.error:
            continue
        if llama_server and it.role == "runtime_llama":
            it.present, it.use_path = True, llama_server
            it.note = "using llama-server found on the PATH"
        elif llama_server and it.role == "runtime_llama_cudart":
            it.present, it.note = True, "not needed: using llama-server found on the PATH"
        else:
            it.present = bool(have(it))
    skipped = [i["id"] for i in kit["items"] if picks[i["id"]] == "skip"]
    return Plan(kit=kit["id"], label=kit["label"], items=items, skipped=skipped,
                checked=data.get("_checked", ""))


# ── presence, space, hardware ─────────────────────────────────────────────────────────────────


def recorded(dest_dir, item_id: str) -> bool:
    """dest_dir/.primer-setup.json names item_id (the installer finished extracting it)."""
    try:
        data = json.loads((Path(dest_dir) / SETUP_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    items = data.get("items") if isinstance(data, dict) else None
    return isinstance(items, dict) and item_id in items


def have_under(root, recorded_exe: Callable[[Path], bool] | None = None) -> Callable[[PlanItem], bool]:
    """have(item): its file is under `root` at the expected size; an extracted item (runtime,
    fonts) counts only when dest/.primer-setup.json records it, and a runtime's exe exists;
    the map counts when it exists. With `recorded_exe` (var/setup.json's installed files, from
    the installer), a runtime counts only when its exe (or, for the CUDA DLLs, its marker DLL)
    is recorded there too: a data folder from another Primer is installed again, which records
    its programs (identical files merge) so they can be approved as one release."""
    root = Path(root)

    def have(item: PlanItem) -> bool:
        target = root / item.dest
        try:
            if item.kind in ("runtime", "fonts"):
                if not recorded(target, item.id) or (item.exe and not (target / item.exe).is_file()):
                    return False
                program = item.exe or item.marker
                if (recorded_exe is not None and item.kind == "runtime" and program
                        and program.lower().endswith((".exe", ".dll"))):
                    return recorded_exe(target / program)
                return True
            if item.kind == "map":
                return target.is_file() and target.stat().st_size > 0
            if not target.is_file():
                return False
            if item.size_exact:
                return target.stat().st_size == item.size
            return 0 <= item.size - target.stat().st_size < 1024
        except OSError:
            return False

    return have


def which_llama_server() -> str | None:
    """llama-server on the PATH (e.g. a winget install), or None."""
    return shutil.which("llama-server")


def space_needed(plan: Plan) -> int:
    need = MARGIN
    for i in plan.items:
        if i.present:
            continue
        if i.kind == "map":
            need += i.size * MAP_WORKING_FACTOR
        elif i.kind in ("runtime", "fonts"):
            need += i.size * ZIP_WORKING_FACTOR
        else:
            need += i.size
    return need


def fits(plan: Plan, free_bytes: int) -> tuple[bool, int]:
    """(ok, shortfall): the downloads, the map at 3x while it's cut, each zip plus 3x for its
    extraction, and a 2 GB margin."""
    short = max(0, space_needed(plan) - int(free_bytes))
    return short == 0, short


def suggest_kit(hw: Hardware, manifest: dict | None = None) -> tuple[str, str]:
    """(kit id, one-line reason) from the hardware detection."""
    kit_id = suggest_tier(hw)
    try:
        label = _kit(manifest or load_kits(), kit_id)["label"]
    except KitError:
        label = kit_id.title()
    if hw.gpu_name and hw.vram_gb > 0:
        return kit_id, f"{hw.vram_gb:.0f} GB GPU ({hw.gpu_name}) → {label}"
    return kit_id, f"No NVIDIA GPU found → {label}"
