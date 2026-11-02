import json
import struct
import time
from pathlib import Path

from primer.images.scan import scan_folder, sniff


def gguf(path: Path, arch: str | None, extra_kvs: int = 0):
    """A minimal GGUF v3 header: some leading KVs of mixed types, then general.architecture."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = [s("general.name") + struct.pack("<I", 8) + s("tiny"),
           s("general.some_array") + struct.pack("<I", 9) + struct.pack("<IQ", 8, 2) + s("a") + s("bb"),
           s("general.file_type") + struct.pack("<II", 4, 7)]
    kvs += [s(f"x.k{i}") + struct.pack("<If", 6, 1.0) for i in range(extra_kvs)]
    if arch is not None:
        kvs.append(s("general.architecture") + struct.pack("<I", 8) + s(arch))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs))
    return path


def safet(path: Path, keys=(), arch: str | None = None):
    header = {k: {"dtype": "F16", "shape": [0], "data_offsets": [0, 0]} for k in keys}
    if arch is not None:
        header["__metadata__"] = {"modelspec.architecture": arch}
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw)
    return path


def tok(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def test_sniff_gguf_arch(tmp_path):
    assert sniff(gguf(tmp_path / "a.gguf", "moss_image")) == {"format": "gguf", "arch": "moss_image"}
    assert sniff(gguf(tmp_path / "b.gguf", None))["arch"] == ""
    # arch found before the KV limit is not needed; one far past 2000 KVs is not read
    assert sniff(gguf(tmp_path / "c.gguf", "late", extra_kvs=2100))["arch"] == ""
    assert sniff(tmp_path / "missing.gguf") == {"format": "other"}
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"NOPE" + b"\0" * 40)
    assert sniff(bad) == {"format": "other"}
    trunc = tmp_path / "trunc.gguf"
    trunc.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 5))
    assert sniff(trunc)["arch"] == ""


def test_sniff_safetensors_keys_and_arch(tmp_path):
    keys = [f"conditioner.embedders.{i}.w" for i in range(100)]
    r = sniff(safet(tmp_path / "m.safetensors", keys, "stable-diffusion-xl-v1-base"))
    assert r["format"] == "safetensors"
    assert r["arch"] == "stable-diffusion-xl-v1-base"
    assert r["keys"] == keys[:64]
    assert r["n_keys"] == 100
    r2 = sniff(safet(tmp_path / "n.safetensors", ["a"]))
    assert r2["arch"] == "" and r2["n_keys"] == 1
    assert sniff(tok(tmp_path / "tokenizer.json")) == {"format": "other"}
    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"\xff" * 64)
    assert sniff(junk)["format"] == "other"


def test_sniff_reads_header_only(tmp_path):
    p = safet(tmp_path / "big.safetensors", ["a", "b", "c"])
    with open(p, "r+b") as f:
        f.truncate(100 * 1024 * 1024)
    t = time.time()
    r = sniff(p)
    assert time.time() - t < 1
    assert r["n_keys"] == 3


def names(props):
    return sorted(p["name"] for p in props)


def test_scan_proposes_three_qwen_variants_sharing_encoder_and_vae(tmp_path):
    d = tmp_path / "qwen-image-2.1"
    gguf(d / "qwen-image-2.1-UC-Q8_0.gguf", "qwen_image21")
    gguf(d / "qwen-image-2.1-Q4_K_M.gguf", None)  # no arch: the filename covers it
    safet(d / "qwen-image-2.1-UC-NVFP4.safetensors", ["transformer_blocks.0.w"])
    safet(d / "qwen3vl_8b_int8_convrot.safetensors", ["model.layers.0.w"])
    safet(d / "qwen_image_2.1_vae_bf16.safetensors", ["decoder.w"], "qwen_image_2.1_vae")
    props = scan_folder(tmp_path)
    assert len(props) == 3
    for p in props:
        assert p["family"] == "qwen-image-2.1"
        assert p["missing"] == []
        assert Path(p["files"]["llm"]).name == "qwen3vl_8b_int8_convrot.safetensors"
        assert Path(p["files"]["vae"]).name == "qwen_image_2.1_vae_bf16.safetensors"
        assert Path(p["files"]["diffusion"]).is_absolute()
    assert sorted(Path(p["files"]["diffusion"]).name for p in props) == [
        "qwen-image-2.1-Q4_K_M.gguf", "qwen-image-2.1-UC-NVFP4.safetensors",
        "qwen-image-2.1-UC-Q8_0.gguf"]


def test_scan_qwen_incomplete_names_missing(tmp_path):
    gguf(tmp_path / "qwen-image-2.1-Q4.gguf", "qwen_image21")
    [p] = scan_folder(tmp_path)
    assert p["missing"] == ["llm", "vae"]
    assert list(p["files"]) == ["diffusion"]


def test_scan_moss_complete_and_incomplete(tmp_path):
    d = tmp_path / "Moss-Image"
    gguf(d / "MOSS-Image-SFT-UD-Q4_K_XL.gguf", "moss_image")
    gguf(d / "Qwen3.5-9B-Q8_0.gguf", "qwen35")
    safet(d / "vae" / "diffusion_pytorch_model.safetensors", ["decoder.w"])
    [p] = scan_folder(tmp_path)
    assert p["family"] == "moss-image"
    assert p["missing"] == ["tokenizer"]
    assert set(p["files"]) == {"diffusion", "llm", "vae"}
    tok(d / "tokenizer" / "tokenizer.json")
    [p] = scan_folder(tmp_path)
    assert p["missing"] == []
    assert Path(p["files"]["tokenizer"]).name == "tokenizer.json"
    assert Path(p["files"]["vae"]).parent.name == "vae"
    assert Path(p["files"]["llm"]).name == "Qwen3.5-9B-Q8_0.gguf"


def test_scan_companions_fall_back_to_parent_folder(tmp_path):
    gguf(tmp_path / "sub" / "moss.gguf", "moss_image")
    gguf(tmp_path / "Qwen3.5-9B.gguf", None)
    tok(tmp_path / "tokenizer.json")
    [p] = scan_folder(tmp_path)
    assert p["missing"] == ["vae"]
    assert set(p["files"]) == {"diffusion", "llm", "tokenizer"}


def test_scan_sdxl_and_ignores_embeddings(tmp_path):
    safet(tmp_path / "bigLove_xl4.safetensors", ["conditioner.embedders.0.w"])
    safet(tmp_path / "other.safetensors", ["x"], "stable-diffusion-xl-v1-base")
    safet(tmp_path / "ltx-2-19b-distilled-lora-384.safetensors", ["lora_unet.w"])
    gguf(tmp_path / "Qwen3-Embedding-8B-Q8_0.gguf", "qwen3")
    gguf(tmp_path / "Qwen3-Embedding-qwen3vl.gguf", "qwen3")
    props = scan_folder(tmp_path)
    assert names(props) == ["bigLove_xl4", "other"]
    for p in props:
        assert p["family"] == "sdxl" and p["missing"] == []
        assert list(p["files"]) == ["model"]


def test_scan_depth_and_cache_skip(tmp_path):
    safet(tmp_path / "a" / "b" / "c" / "ok.safetensors", ["conditioner.embedders.0.w"])
    safet(tmp_path / "a" / "b" / "c" / "d" / "toodeep.safetensors", ["conditioner.embedders.0.w"])
    safet(tmp_path / ".cache" / "cached.safetensors", ["conditioner.embedders.0.w"])
    assert names(scan_folder(tmp_path)) == ["ok"]
    assert scan_folder(tmp_path / "nope") == []


# ---- fix round 1 ----
import os
import subprocess
import sys

import pytest


def raw_gguf(path: Path, body: bytes, n_kv: int = 2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, n_kv) + body)
    return path


def _key(k: str) -> bytes:
    return struct.pack("<Q", len(k)) + k.encode()


def test_sniff_nested_gguf_arrays_do_not_raise(tmp_path):
    body = _key("a") + struct.pack("<I", 9) + (struct.pack("<IQ", 9, 1) * 400_000)
    p = raw_gguf(tmp_path / "nest.gguf", body)
    assert sniff(p) == {"format": "gguf", "arch": ""}


@pytest.mark.parametrize("opener", [b"[", b'{"a":'])
def test_sniff_deeply_nested_json_header_does_not_raise(tmp_path, opener):
    raw = opener * 300_000
    p = tmp_path / "deep.safetensors"
    p.write_bytes(struct.pack("<Q", len(raw)) + raw)
    assert sniff(p) == {"format": "other"}


def test_scan_survives_hostile_files_and_still_proposes_good_models(tmp_path):
    body = _key("a") + struct.pack("<I", 9) + (struct.pack("<IQ", 9, 1) * 400_000)
    raw_gguf(tmp_path / "nest.gguf", body)
    raw = b"[" * 300_000
    (tmp_path / "deep.safetensors").write_bytes(struct.pack("<Q", len(raw)) + raw)
    safet(tmp_path / "good.safetensors", ["conditioner.embedders.0.w"])
    assert names(scan_folder(tmp_path)) == ["good"]


def test_sniff_huge_declared_array_is_quick(tmp_path):
    body = _key("a") + struct.pack("<I", 9) + struct.pack("<IQ", 8, 2**60)
    p = raw_gguf(tmp_path / "huge.gguf", body, n_kv=1)
    with open(p, "r+b") as f:
        f.truncate(70 * 1024 * 1024)
    t = time.time()
    assert sniff(p)["arch"] == ""
    assert time.time() - t < 1


def test_sniff_caps_total_array_elements(tmp_path):
    n = 1_200_000
    body = (_key("a") + struct.pack("<I", 9) + struct.pack("<IQ", 8, n) + b"\0" * 8 * n
            + _key("general.architecture") + struct.pack("<I", 8) + _key("moss_image"))
    p = raw_gguf(tmp_path / "many.gguf", body)
    assert sniff(p)["arch"] == ""
    n = 100_000  # a realistic vocabulary is fine
    body = (_key("a") + struct.pack("<I", 9) + struct.pack("<IQ", 8, n) + b"\0" * 8 * n
            + _key("general.architecture") + struct.pack("<I", 8) + _key("moss_image"))
    assert sniff(raw_gguf(tmp_path / "vocab.gguf", body))["arch"] == "moss_image"


def test_sniff_caps_arch_string_and_safetensors_header(tmp_path):
    long_arch = "x" * 300
    assert sniff(gguf(tmp_path / "l.gguf", long_arch))["arch"] == ""
    assert sniff(gguf(tmp_path / "ok.gguf", "y" * 256))["arch"] == "y" * 256
    raw = json.dumps({"a": {"dtype": "F16", "shape": [0], "data_offsets": [0, 0]},
                      "__metadata__": {"pad": "z" * (9 * 1024 * 1024)}}).encode()
    p = tmp_path / "big.safetensors"
    p.write_bytes(struct.pack("<Q", len(raw)) + raw)
    assert sniff(p) == {"format": "other"}


def test_scan_does_not_follow_file_symlink(tmp_path):
    outside = tmp_path / "outside"
    safet(outside / "secret.safetensors", ["conditioner.embedders.0.w"])
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside / "secret.safetensors", root / "link.safetensors")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted")
    assert scan_folder(root) == []


def test_scan_does_not_follow_directory_junction(tmp_path):
    if sys.platform != "win32":
        pytest.skip("junctions are Windows only")
    import _winapi
    outside = tmp_path / "outside"
    safet(outside / "secret.safetensors", ["conditioner.embedders.0.w"])
    root = tmp_path / "root"
    safet(root / "mine.safetensors", ["conditioner.embedders.0.w"])
    try:
        _winapi.CreateJunction(str(outside), str(root / "jn"))
        _winapi.CreateJunction(str(root), str(root / "loop"))
    except OSError:
        pytest.skip("junctions not permitted")
    assert names(scan_folder(root)) == ["mine"]


def test_scan_does_not_borrow_companions_from_sibling_folders(tmp_path):
    gguf(tmp_path / "Moss-Image" / "moss.gguf", "moss_image")
    safet(tmp_path / "flux-dev" / "vae" / "diffusion_pytorch_model.safetensors", ["w"])
    tok(tmp_path / "flux-dev" / "tokenizer" / "tokenizer.json")
    [p] = scan_folder(tmp_path)
    assert p["missing"] == ["llm", "vae", "tokenizer"]
    gguf(tmp_path / "m1" / "moss1.gguf", "moss_image")
    gguf(tmp_path / "m1" / "Qwen3.5-9B.gguf", None)
    gguf(tmp_path / "moss2" / "moss2.gguf", "moss_image")
    by = {p["name"]: p for p in scan_folder(tmp_path)}
    assert "llm" in by["moss1"]["files"]
    assert "llm" in by["moss2"]["missing"]


def test_scan_parent_fallback_is_recorded_in_notes(tmp_path):
    gguf(tmp_path / "sub" / "moss.gguf", "moss_image")
    gguf(tmp_path / "Qwen3.5-9B.gguf", None)
    tok(tmp_path / "tokenizer" / "tokenizer.json")
    [p] = scan_folder(tmp_path)
    assert set(p["files"]) == {"diffusion", "llm", "tokenizer"}
    assert len(p["notes"]) == 2
    assert all("parent folder" in n for n in p["notes"])
    assert any(n.startswith("llm") for n in p["notes"])


def test_scan_own_layout_has_no_notes(tmp_path):
    d = tmp_path / "Moss-Image"
    gguf(d / "moss.gguf", "moss_image")
    gguf(d / "Qwen3.5-9B.gguf", None)
    safet(d / "vae" / "diffusion_pytorch_model.safetensors", ["w"])
    tok(d / "tokenizer" / "tokenizer.json")
    [p] = scan_folder(tmp_path)
    assert p["missing"] == [] and p["notes"] == []


def test_scan_ignores_mmproj_as_llm(tmp_path):
    d = tmp_path / "Moss-Image"
    gguf(d / "moss.gguf", "moss_image")
    gguf(d / "mmproj-Qwen3.5-9B-F16.gguf", None)
    gguf(d / "Qwen3.5-9B-Q8_0.gguf", None)
    [p] = scan_folder(tmp_path)
    assert Path(p["files"]["llm"]).name == "Qwen3.5-9B-Q8_0.gguf"
    q = tmp_path / "q"
    gguf(q / "qwen-image-2.1-x.gguf", "qwen_image21")
    safet(q / "mmproj-qwen3vl.safetensors", ["a"])
    qp = [x for x in scan_folder(q)][0]
    assert "llm" in qp["missing"]


def test_scan_inspects_at_most_max_files(tmp_path):
    for name in ("a", "b", "c"):
        safet(tmp_path / name / f"{name}.safetensors", keys=["conditioner.embedders.0.x"])
    assert len(scan_folder(tmp_path)) == 3
    assert [p["name"] for p in scan_folder(tmp_path, max_files=2)] == ["a", "b"]


def test_scan_capped_reports_truncation(tmp_path):
    from primer.images.scan import scan_folder_capped

    for name in ("a", "b", "c"):
        safet(tmp_path / name / f"{name}.safetensors", keys=["conditioner.embedders.0.x"])
    proposals, truncated = scan_folder_capped(tmp_path, 2)
    assert [p["name"] for p in proposals] == ["a", "b"] and truncated is True
    assert scan_folder_capped(tmp_path, 3)[1] is False
    assert len(scan_folder_capped(tmp_path, 10)[0]) == 3
