"""Setup kits: the manifest, the resolver (against fake catalogues), presence and disk space."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from primer.hardware import Hardware
from primer.setup import kits
from primer.setup.kits import KitError, Plan, fits, have_under, load_kits, resolve, suggest_kit
from primer.setup.net import CatalogueError

FIX = Path(__file__).parent / "fixtures" / "setup"
GB = 1024 ** 3


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeCatalogues:
    """The four Catalogues methods over hand-written data: every file the manifest names exists
    (sizes made up, 1 MB steps), except LiquidAI/LFM3-1.7B-GGUF which uses the recorded listing."""

    def __init__(self, manifest=None, broken=()):
        self.calls = []
        self.broken = set(broken)
        self.files: dict[str, list[dict]] = {}
        n = 0
        for kit in (manifest or load_kits())["kits"]:
            for item in kit["items"]:
                src = item["source"]
                if src["type"] != "hf":
                    continue
                for ch in item["choices"]:
                    f = ch.get("file", src.get("file"))
                    listing = self.files.setdefault(ch.get("repo", src["repo"]), [])
                    if all(e["path"] != f for e in listing):
                        n += 1
                        listing.append({"path": f, "size": n * 1_000_000, "sha256": _sha(f)})
        tree = json.loads((FIX / "hf_tree_lfm.json").read_text(encoding="utf-8"))
        real = [{"path": e["path"], "size": e["lfs"]["size"], "sha256": e["lfs"]["oid"]}
                for e in tree if e.get("type") == "file" and e.get("lfs")]
        names = {e["path"] for e in real}
        self.files["LiquidAI/LFM3-1.7B-GGUF"] = real + [
            e for e in self.files.get("LiquidAI/LFM3-1.7B-GGUF", []) if e["path"] not in names]

    def hf_files(self, repo):
        self.calls.append(("hf_files", repo))
        if repo in self.broken:
            raise CatalogueError(f"{repo} answered 503")
        return [dict(e) for e in self.files[repo]]

    def hf_card(self, repo):
        self.calls.append(("hf_card", repo))
        if repo.startswith("LiquidAI/"):
            return {"license": "other", "gated": False, "summary": "",
                    "license_link": f"https://huggingface.co/{repo}/blob/main/LICENSE"}
        return {"license": "apache-2.0", "gated": False, "summary": ""}

    def zims(self, lang="eng", name_prefix="wikipedia_en_"):
        self.calls.append(("zims", lang))
        out = []
        for name, flavour, size in (("wikipedia_en_top", "mini", 296858624),
                                    ("wikipedia_en_top", "nopic", 2745850880),
                                    ("wikipedia_en_all", "mini", 14386799616)):
            url = f"https://lb.download.kiwix.org/zim/wikipedia/{name}_{flavour}_2026-09.zim"
            out.append({"name": name, "flavour": flavour, "date": "2026-09-14", "size": size,
                        "url": url, "sha256_url": url + ".sha256"})
        return out

    def latest_asset(self, repo, pattern, prefer_tag_prefix=None):
        self.calls.append(("latest_asset", repo, pattern))
        tag = "b12102" if "llama" in repo else "master-1047-984eb06"
        names = {
            "ggml-org/llama.cpp": ["llama-b12102-bin-win-cpu-x64.zip",
                                   "llama-b12102-bin-win-cuda-12.4-x64.zip",
                                   "llama-b12102-bin-win-vulkan-x64.zip",
                                   "cudart-llama-bin-win-cuda-12.4-x64.zip"],
            "leejet/stable-diffusion.cpp": ["sd-master-984eb06-bin-win-cpu-x64.zip",
                                            "sd-master-984eb06-bin-win-cuda12-x64.zip",
                                            "sd-master-984eb06-bin-win-vulkan-x64.zip",
                                            "cudart-sd-bin-win-cu12-x64.zip"],
        }[repo]
        for i, name in enumerate(names):
            if re.search(pattern, name):
                return {"tag": tag, "name": name, "size": (i + 1) * 10_000_000,
                        "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
                        "sha256": _sha(name)}
        raise CatalogueError("no recent release has a matching file with a published SHA-256")


def nothing(item):
    return False


# ── the manifest ──────────────────────────────────────────────────────────────────────────────


def test_kits_json_valid():
    data = load_kits()  # validates; raises KitError on any problem
    assert data["_checked"] == "2026-11-04"
    ids = [k["id"] for k in data["kits"]]
    assert ids == ["light", "hybrid", "workstation"]
    pickle = re.compile(r"\.(ckpt|pt|pth|bin|pkl)$", re.I)
    for kit in data["kits"]:
        roles = {i["role"] for i in kit["items"]}
        assert roles <= kits.ROLES
        assert {"text", "voice", "voice_mmproj", "reference", "map", "fonts",
                "runtime_llama", "runtime_kiwix"} <= roles
        for item in kit["items"]:
            choice_ids = [c["id"] for c in item["choices"]]
            assert item["default"] in choice_ids + ["skip"]
            for ch in item["choices"]:
                for key in ("file", "url"):
                    if key in ch or key in item["source"]:
                        assert not pickle.search(ch.get(key, item["source"].get(key, "")))
    hybrid = next(k for k in data["kits"] if k["id"] == "hybrid")
    assert {"image", "image_encoder", "image_vae", "runtime_sd", "fast"} <= {i["role"] for i in hybrid["items"]}
    work = next(k for k in data["kits"] if k["id"] == "workstation")
    assert {"video", "video_encoder", "video_vae"} <= {i["role"] for i in work["items"]}
    text = json.dumps(data)
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[/\\]", text)  # no local drive paths


@pytest.mark.parametrize("breakage, message", [
    (lambda k: k["kits"][0]["items"][0].update(role="wizard"), "role"),
    (lambda k: k["kits"][0]["items"][0]["choices"][0].update(file="model.ckpt"), "format"),
    (lambda k: k["kits"][0]["items"][0].update(default="nope"), "default"),
    (lambda k: k["kits"][0]["items"].append(dict(k["kits"][0]["items"][0])), "duplicate"),
])
def test_kits_json_problems_refused(breakage, message):
    data = json.loads(kits.KITS_FILE.read_text(encoding="utf-8"))
    breakage(data)
    with pytest.raises(KitError, match=message):
        kits.validate(data)


# ── resolving ─────────────────────────────────────────────────────────────────────────────────


def test_resolve_with_fixture_catalogues():
    cat = FakeCatalogues()
    plan = resolve("light", cat, nothing)
    assert isinstance(plan, Plan) and plan.kit == "light"
    by_role = {i.role: i for i in plan.items}
    text = by_role["text"]
    assert text.kind == "model"
    assert text.dest == "models/LiquidAI/LFM3-1.7B-GGUF/LFM3-1.7B-Q8_0.gguf"
    assert text.url == "https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF/resolve/main/LFM3-1.7B-Q8_0.gguf"
    assert text.size > 0
    assert re.fullmatch(r"[0-9a-f]{64}", text.sha256)
    assert text.licence == "other" and text.licence_link.endswith("/LICENSE")
    assert not text.present and not text.gated
    # the swap list comes from the same listing, with sizes
    assert {"q4_k_m", "q8_0"} <= {c["id"] for c in text.choices}

    zim = by_role["reference"]
    assert zim.kind == "zim" and zim.dest == "reference/wikipedia_en_top_mini_2026-09.zim"
    assert zim.sha256 is None and zim.sha256_url == zim.url + ".sha256"
    assert zim.size == 296858624

    rt = by_role["runtime_llama"]
    assert rt.kind == "runtime" and rt.dest == "tools/llama.cpp/b12102/"
    assert rt.url.endswith("llama-b12102-bin-win-cpu-x64.zip") and rt.exe == "llama-server.exe"

    kiwix = by_role["runtime_kiwix"]
    assert kiwix.md5 is None and kiwix.md5_url == kiwix.url + ".md5" and kiwix.sha256 is None
    assert kiwix.dest.startswith("tools/kiwix-tools/")

    m = by_role["map"]
    assert m.kind == "map" and m.maxzoom == 8 and m.size_estimate == m.size > 0
    assert m.sha256 is None and m.dest == "maps/world-z8.pmtiles"

    fonts = by_role["fonts"]
    assert fonts.kind == "fonts" and fonts.dest == "maps/basemaps-assets/"
    assert fonts.url.startswith("https://codeload.github.com/protomaps/basemaps-assets/zip/028c18f")
    assert fonts.sha256 is None or re.fullmatch(r"[0-9a-f]{64}", fonts.sha256)

    assert plan.total == sum(i.size for i in plan.items)
    assert plan.to_download == plan.total
    js = plan.to_json()
    json.dumps(js)
    assert js["kit"] == "light" and len(js["items"]) == len(plan.items)
    # resolving stays cheap: one listing and one card per repo, the ZIM catalogue once
    hf_calls = [c for c in cat.calls if c[0] == "hf_files"]
    assert len(hf_calls) == len({c[1] for c in hf_calls})
    assert sum(1 for c in cat.calls if c[0] == "zims") == 1


def test_resolve_hybrid_choices_and_skips():
    cat = FakeCatalogues()
    plan = resolve("hybrid", cat, nothing)
    roles = [i.role for i in plan.items]
    assert "runtime_llama_cudart" in roles and "runtime_sd_cudart" in roles
    by_role = {i.role: i for i in plan.items}
    assert re.search(r"cuda-12\.\d+-x64\.zip$", by_role["runtime_llama"].url)
    # the CUDA runtime DLLs go beside the executables
    assert by_role["runtime_llama_cudart"].dest == by_role["runtime_llama"].dest
    assert by_role["runtime_sd_cudart"].dest == by_role["runtime_sd"].dest
    assert by_role["fast"].dest.startswith("models/LiquidAI/LFM3-1.7B-GGUF/")

    # swap llama.cpp to the Vulkan build: its CUDA runtime falls away; skip the image model
    plan2 = resolve("hybrid", cat, nothing, {"llama_cpp": "vulkan", "image_model": "skip"})
    roles2 = {i.role for i in plan2.items}
    assert "runtime_llama_cudart" not in roles2 and "image" not in roles2
    assert "image_model" in plan2.skipped
    assert next(i for i in plan2.items if i.role == "runtime_llama").url.endswith("vulkan-x64.zip")
    # a quant swap from the same repo
    plan3 = resolve("hybrid", cat, nothing, {"writing_model": "q4_k_m"})
    assert next(i for i in plan3.items if i.role == "text").dest.endswith("gemma-4-E4B-it-Q4_K_M.gguf")

    with pytest.raises(KitError, match="choice"):
        resolve("hybrid", cat, nothing, {"writing_model": "q99"})
    with pytest.raises(KitError, match="unknown item"):
        resolve("hybrid", cat, nothing, {"nope": "skip"})
    with pytest.raises(KitError, match="unknown kit"):
        resolve("galactic", cat, nothing)


def test_resolve_catalogue_failure_is_per_item():
    cat = FakeCatalogues(broken={"LiquidAI/LFM3-Audio-2B-GGUF"})
    plan = resolve("light", cat, nothing)
    voice = next(i for i in plan.items if i.role == "voice")
    assert "503" in voice.error and not plan.ok
    assert next(i for i in plan.items if i.role == "text").error == ""


def test_present_items_skipped(tmp_path):
    cat = FakeCatalogues()
    first = resolve("light", cat, nothing)
    text = next(i for i in first.items if i.role == "text")
    rt = next(i for i in first.items if i.role == "runtime_llama")
    zim = next(i for i in first.items if i.role == "reference")
    # the model at the right size, the runtime extracted, a ZIM of the wrong size (truncated)
    p = tmp_path / text.dest
    p.parent.mkdir(parents=True)
    with open(p, "wb") as f:
        f.truncate(text.size)
    rt_dir = tmp_path / rt.dest
    rt_dir.mkdir(parents=True)
    (rt_dir / rt.exe).write_bytes(b"MZ")
    z = tmp_path / zim.dest
    z.parent.mkdir(parents=True)
    z.write_bytes(b"short")

    # an exe alone is not an install: the installer records the item once it is all in place
    assert not next(i for i in resolve("light", cat, have_under(tmp_path)).items
                    if i.role == "runtime_llama").present
    marker = rt_dir / kits.SETUP_MARKER
    marker.write_text(json.dumps({"items": {"some_other_item": "ab" * 32}}), encoding="utf-8")
    assert not have_under(tmp_path)(rt)  # a record for another item sharing the folder
    marker.write_text("{not json", encoding="utf-8")
    assert not have_under(tmp_path)(rt)
    marker.write_text(json.dumps({"items": {rt.id: "ab" * 32}}), encoding="utf-8")

    plan = resolve("light", cat, have_under(tmp_path))
    by_role = {i.role: i for i in plan.items}
    assert by_role["text"].present and by_role["runtime_llama"].present
    assert not by_role["reference"].present
    assert plan.to_download == plan.total - text.size - rt.size
    # recorded but the exe is gone: not present
    (rt_dir / rt.exe).unlink()
    assert not have_under(tmp_path)(rt)

    # the fonts: present only once recorded
    fonts = by_role["fonts"]
    fdir = tmp_path / fonts.dest
    (fdir / fonts.assets_subdir / "fonts").mkdir(parents=True)
    assert not have_under(tmp_path)(fonts)
    (fdir / kits.SETUP_MARKER).write_text(json.dumps({"items": {fonts.id: "cd" * 32}}), encoding="utf-8")
    assert have_under(tmp_path)(fonts)

    # llama-server on the PATH stands in for the runtime download
    plan = resolve("light", cat, nothing, llama_server="/opt/llama/llama-server")
    rt = next(i for i in plan.items if i.role == "runtime_llama")
    assert rt.present and rt.use_path == "/opt/llama/llama-server"
    plan = resolve("hybrid", cat, nothing, llama_server="/opt/llama/llama-server")
    assert next(i for i in plan.items if i.role == "runtime_llama_cudart").present


def test_which_llama_server(monkeypatch):
    monkeypatch.setattr(kits.shutil, "which", lambda name: "/usr/bin/llama-server" if name == "llama-server" else None)
    assert kits.which_llama_server() == "/usr/bin/llama-server"
    monkeypatch.setattr(kits.shutil, "which", lambda name: None)
    assert kits.which_llama_server() is None


# ── disk space ────────────────────────────────────────────────────────────────────────────────


def test_kit_refused_when_no_space():
    plan = resolve("light", FakeCatalogues(), nothing)
    m = next(i for i in plan.items if i.role == "map")
    zips = sum(i.size for i in plan.items if i.kind in ("runtime", "fonts"))
    assert zips > 0
    # the map needs 3x while it's cut; each zip needs itself plus 3x while it's extracted
    need = plan.to_download + 2 * m.size_estimate + 3 * zips + kits.MARGIN
    assert kits.space_needed(plan) == need
    assert kits.MARGIN == 2 * GB
    assert fits(plan, need) == (True, 0)
    assert fits(plan, need + 10**9) == (True, 0)
    ok, short = fits(plan, need - 12345)
    assert not ok and short == 12345
    ok, short = fits(plan, 0)
    assert not ok and short == need

    # present items don't count
    have_all = resolve("light", FakeCatalogues(), lambda item: True)
    assert have_all.to_download == 0
    assert fits(have_all, kits.MARGIN) == (True, 0)
    assert fits(have_all, kits.MARGIN - 1) == (False, 1)


def test_hybrid_size_is_about_28_gb():
    # the real sizes recorded in kits.json while building (2026-11-04)
    data = load_kits()
    hybrid = next(k for k in data["kits"] if k["id"] == "hybrid")
    assert 26 * 10**9 <= kits.kit_size_hint(hybrid) <= 30 * 10**9


# ── hardware → kit ────────────────────────────────────────────────────────────────────────────


def test_suggest_kit():
    kit, why = suggest_kit(Hardware("NVIDIA GeForce RTX 4070", 12.0, 32.0))
    assert kit == "hybrid" and why == "12 GB GPU (NVIDIA GeForce RTX 4070) → Hybrid"
    kit, why = suggest_kit(Hardware(None, 0.0, 16.0))
    assert kit == "light" and "Light" in why and "GPU" in why
    assert suggest_kit(Hardware("NVIDIA RTX 6000", 48.0, 128.0))[0] == "workstation"


def test_zim_size_is_rough(tmp_path):
    # the Kiwix catalogue rounds sizes up to the next KiB, so an installed ZIM is a little smaller
    item = kits.PlanItem(id="z", kind="zim", role="reference", label="z", dest="reference/w.zim",
                         size=296_858_624)
    assert not item.size_exact
    f = tmp_path / "reference" / "w.zim"
    f.parent.mkdir()
    f.write_bytes(b"")
    have = have_under(tmp_path)
    with open(f, "wb") as fh:
        fh.truncate(296_858_160)
    assert have(item)
    with open(f, "wb") as fh:
        fh.truncate(296_000_000)
    assert not have(item)
    model = kits.PlanItem(id="m", kind="model", role="text", label="m", dest="m.gguf", size=5)
    assert model.size_exact


def test_light_offers_only_the_cpu_build_of_llama_cpp():
    light = next(k for k in load_kits()["kits"] if k["id"] == "light")
    llama = next(i for i in light["items"] if i["role"] == "runtime_llama")
    assert [c["id"] for c in llama["choices"]] == ["cpu"]  # the Light kit runs everything on the CPU
