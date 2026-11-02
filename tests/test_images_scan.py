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


# ---- video (phase 3) ----
from primer.images.scan import _roles  # noqa: E402


def wan_gguf(path: Path, in_ch: int | None, truncate: bool = False):
    """arch "wan" plus a tensor index whose patch_embedding.weight has in_ch input channels."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = [s("general.architecture") + struct.pack("<I", 8) + s("wan"),
           s("general.file_type") + struct.pack("<II", 4, 15)]
    tensors = [s("blocks.0.cross_attn.k.weight") + struct.pack("<I2Q", 2, 5120, 5120) + struct.pack("<IQ", 12, 0)]
    if in_ch is not None:
        tensors.append(s("patch_embedding.weight") + struct.pack("<I5Q", 5, 2, 2, 1, in_ch, 5120)
                       + struct.pack("<IQ", 12, 0))
    raw = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kvs)) + b"".join(kvs) + b"".join(tensors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw[:-10] if truncate else raw)
    return path


def shaped(path: Path, shapes: dict):
    """A safetensors header with real shapes for its keys."""
    header = {k: {"dtype": "F16", "shape": v, "data_offsets": [0, 0]} for k, v in shapes.items()}
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw)
    return path


def wan_vae(path: Path, z: int | None):
    """Wan VAE keys; conv2.bias is [16] for 2.1, [48] for 2.2. z None: unreadable, the name decides."""
    c = z or 16
    return shaped(path, {"conv1.bias": [2 * c], "conv2.bias": [z] if z else "x",
                         "conv2.weight": [c, c, 1, 1, 1], "decoder.conv1.weight": [384, c, 3, 3, 3],
                         "encoder.conv1.weight": [96, 3, 3, 3, 3]})


def lora(path: Path, style: str = "down"):
    a, b = ("lora_down", "lora_up") if style == "down" else ("lora_A", "lora_B")
    return safet(path, [f"diffusion_model.blocks.0.cross_attn.k.{a}.weight",
                        f"diffusion_model.blocks.0.cross_attn.k.{b}.weight"])


def t5(path: Path):
    return gguf(path, "t5encoder")


def ltx(path: Path):
    return gguf(path, "ltxv")


def gemma(path: Path):
    return gguf(path, "gemma3")


def connectors(path: Path):
    return safet(path, ["text_embedding_projection.audio_aggregate_embed.bias",
                        "text_embedding_projection.video_aggregate_embed.bias"])


def ltx_vae(path: Path):
    return safet(path, ["decoder.conv_in.conv.bias", "decoder.conv_in.conv.weight", "per_channel_statistics.mean"])


def ltx_audio_vae(path: Path):
    return safet(path, ["audio_vae.decoder.conv_in.conv.bias", "vocoder.conv_pre.weight"])


def roles(path: Path):
    return sorted(_roles(path, sniff(path)))


def test_sniff_reads_wan_input_channels_from_the_tensor_index(tmp_path):
    assert sniff(wan_gguf(tmp_path / "a.gguf", 36)) == {"format": "gguf", "arch": "wan", "wan_in": 36}
    assert sniff(wan_gguf(tmp_path / "b.gguf", None)) == {"format": "gguf", "arch": "wan"}
    assert sniff(wan_gguf(tmp_path / "c.gguf", 48, truncate=True)) == {"format": "gguf", "arch": "wan"}
    assert sniff(wan_vae(tmp_path / "v.safetensors", 48))["vae_z"] == 48
    assert "vae_z" not in sniff(wan_vae(tmp_path / "w.safetensors", None))
    st = sniff(shaped(tmp_path / "m.safetensors", {"patch_embedding.weight": [5120, 16, 1, 2, 2]}))
    assert st["wan_in"] == 16


def test_video_roles_from_headers(tmp_path):
    i2v = [("wan2.2-i2v-a14b", "diffusion")]
    assert roles(wan_gguf(tmp_path / "Wan2.2-TI2V-5B-Q6_K.gguf", 48)) == [("wan2.2-ti2v-5b", "diffusion")]
    assert roles(wan_gguf(tmp_path / "Wan2.2-TI2V-5B-Q8.gguf", None)) == [("wan2.2-ti2v-5b", "diffusion")]
    assert roles(wan_gguf(tmp_path / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf", 16)) == [
        ("wan2.2-t2v-a14b", "high_noise")]
    assert roles(wan_gguf(tmp_path / "Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf", 16)) == [("wan2.2-t2v-a14b", "diffusion")]
    assert roles(wan_gguf(tmp_path / "Wan2.2-T2V-A14B-LowNoise-Q8.gguf", None)) == [("wan2.2-t2v-a14b", "diffusion")]
    # the I2V finetune: by its header, and by its name when the header can't tell
    assert roles(wan_gguf(tmp_path / "dasiwaWAN22I2V14B_midnightflirtLow-Q4_K_M.gguf", 36)) == i2v
    assert roles(wan_gguf(tmp_path / "n" / "dasiwaWAN22I2V14B_midnightflirtLow-Q4_K_M.gguf", None)) == i2v
    assert roles(wan_gguf(tmp_path / "n" / "dasiwaWAN22I2V14B_midnightflirtHigh-Q4_K_M.gguf", None)) == [
        ("wan2.2-i2v-a14b", "high_noise")]
    # the stage from the folder when the name doesn't say
    assert roles(wan_gguf(tmp_path / "HighNoise" / "wan-a14b-q4.gguf", 16)) == [("wan2.2-t2v-a14b", "high_noise")]
    assert roles(t5(tmp_path / "umt5-xxl-encoder-Q5_K_M.gguf")) == sorted(
        [("wan2.2-ti2v-5b", "t5xxl"), ("wan2.2-t2v-a14b", "t5xxl"), ("wan2.2-i2v-a14b", "t5xxl")])
    assert roles(wan_vae(tmp_path / "Wan2.1_VAE.safetensors", 16)) == [
        ("wan2.2-i2v-a14b", "vae"), ("wan2.2-t2v-a14b", "vae")]
    assert roles(wan_vae(tmp_path / "Wan2.2_VAE.safetensors", 48)) == [("wan2.2-ti2v-5b", "vae")]
    assert roles(wan_vae(tmp_path / "x" / "Wan2.2_VAE.safetensors", None)) == [("wan2.2-ti2v-5b", "vae")]
    assert roles(wan_vae(tmp_path / "x" / "some_vae.safetensors", None)) == []
    assert roles(ltx(tmp_path / "ltx-2.3-22b-distilled-1.1-Q3_K_M.gguf")) == [("ltx-2.3-distilled", "diffusion")]
    assert roles(ltx(tmp_path / "ltx-2.3-22b-dev-Q4_K_M.gguf")) == [("ltx-2.3", "diffusion")]
    assert roles(ltx(tmp_path / "ltx-2-19b-dev-Q4_K_M.gguf")) == []  # LTX-2, not 2.3
    assert roles(gemma(tmp_path / "gemma-3-12b-it-abliterated-v2.q4_k_m.gguf")) == [
        ("ltx-2.3", "llm"), ("ltx-2.3-distilled", "llm")]
    assert roles(gemma(tmp_path / "mmproj-gemma-3-12b.gguf")) == []
    assert roles(connectors(tmp_path / "ltx-2.3-22b-distilled_embeddings_connectors.safetensors")) == [
        ("ltx-2.3", "connectors"), ("ltx-2.3-distilled", "connectors")]
    assert roles(ltx_vae(tmp_path / "ltx-2.3-22b-distilled_video_vae.safetensors")) == [
        ("ltx-2.3", "vae"), ("ltx-2.3-distilled", "vae")]
    assert roles(ltx_audio_vae(tmp_path / "ltx-2.3-22b-distilled_audio_vae.safetensors")) == []
    assert roles(lora(tmp_path / "wan2.2_i2v_A14b_high_noise_lora_rank64_lightx2v_4step_1022.safetensors")) == []


def by_family(props):
    return {p["family"]: p for p in props}


def test_a14b_pairs_low_with_high_and_never_t2v_with_i2v(tmp_path):
    m = tmp_path / "wan"
    wan_gguf(m / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf", 16)
    wan_gguf(m / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q8_0.gguf", 16)
    wan_gguf(m / "LowNoise" / "Wan2.2-T2V-A14B-LowNoise-Q8_0.gguf", 16)
    wan_gguf(m / "LowNoise" / "dasiwaWAN22I2V14B_midnightflirtLow-Q4_K_M.gguf", 36)
    props = by_family(scan_folder(tmp_path))
    t2v = props["wan2.2-t2v-a14b"]
    assert Path(t2v["files"]["high_noise"]).name == "Wan2.2-T2V-A14B-HighNoise-Q8_0.gguf"
    i2v = props["wan2.2-i2v-a14b"]
    assert "high_noise" not in i2v["files"] and "high_noise" in i2v["missing"]
    # a high-noise file is only a partner: alone it is not a model
    assert scan_folder(m / "HighNoise") == []


def test_a14b_does_not_pair_across_model_folders(tmp_path):
    wan_gguf(tmp_path / "a" / "LowNoise" / "Wan2.2-T2V-A14B-LowNoise-Q4.gguf", 16)
    wan_gguf(tmp_path / "b" / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q4.gguf", 16)
    [p] = scan_folder(tmp_path)
    assert "high_noise" in p["missing"]


def test_shared_t5_and_vae_found_from_the_scan_root(tmp_path):
    t5(tmp_path / "shared" / "umt5-xxl-encoder-Q5_K_M.gguf")
    wan_vae(tmp_path / "shared" / "VAE" / "Wan2.1_VAE.safetensors", 16)
    m = tmp_path / "wan22-t2v-a14b"
    wan_gguf(m / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf", 16)
    wan_gguf(m / "LowNoise" / "Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf", 16)
    [p] = scan_folder(tmp_path)
    assert p["missing"] == []
    assert Path(p["files"]["t5xxl"]).parent.name == "shared"
    assert Path(p["files"]["vae"]).name == "Wan2.1_VAE.safetensors"
    assert len(p["notes"]) == 2 and all("shared files" in n for n in p["notes"])
    # a model one level down takes the root's shared files too (the root is its parent)
    wan_gguf(tmp_path / "wan22-ti2v-5b" / "Wan2.2-TI2V-5B-Q6_K.gguf", 48)
    wan_vae(tmp_path / "wan22-ti2v-5b" / "VAE" / "Wan2.2_VAE.safetensors", 48)
    ti2v = by_family(scan_folder(tmp_path))["wan2.2-ti2v-5b"]
    assert ti2v["missing"] == []
    assert Path(ti2v["files"]["vae"]).name == "Wan2.2_VAE.safetensors"  # never the 2.1 VAE
    # never a sibling model folder's files
    t5(tmp_path / "other-model" / "umt5-xxl.gguf")
    assert Path(by_family(scan_folder(tmp_path))["wan2.2-t2v-a14b"]["files"]["t5xxl"]).parent.name == "shared"


def test_loras_staged_by_name_and_fast_defaults(tmp_path):
    from primer.images.families import FAMILIES
    m = tmp_path / "wan22-i2v"
    wan_gguf(m / "HighNoise" / "x-I2V-High.gguf", 36)
    wan_gguf(m / "LowNoise" / "x-I2V-Low.gguf", 36)
    lora(m / "loras" / "NSFW-22-H-e8.safetensors", "A")
    lora(m / "loras" / "NSFW-22-L-e8.safetensors", "A")
    lora(m / "loras" / "slow_motion.safetensors")
    [p] = scan_folder(tmp_path)
    assert [(Path(x["file"]).name, x["stage"], x["strength"]) for x in p["loras"]] == [
        ("NSFW-22-H-e8.safetensors", "high", 1.0), ("NSFW-22-L-e8.safetensors", "low", 1.0),
        ("slow_motion.safetensors", "both", 1.0)]
    assert p["settings"] == {}  # no few-step LoRA
    lora(m / "loras" / "wan2.2_i2v_A14b_low_noise_lora_rank64_lightx2v_4step_1022.safetensors")
    [p] = scan_folder(tmp_path)
    assert p["settings"] == FAMILIES["wan2.2-i2v-a14b"].fast_defaults and p["settings"]["steps"] == 4
    # TI2V 5B: its Turbo LoRA (in LoRAs/, one level down) is proposed, but the family has no fast defaults
    f = tmp_path / "wan22-ti2v-5b"
    wan_gguf(f / "Wan2.2-TI2V-5B-Q6_K.gguf", 48)
    lora(f / "LoRAs" / "Wan22-Turbo" / "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors")
    ti2v = by_family(scan_folder(tmp_path))["wan2.2-ti2v-5b"]
    assert [x["stage"] for x in ti2v["loras"]] == ["both"] and ti2v["settings"] == {}


def test_loras_come_from_one_folder(tmp_path):
    m = tmp_path / "m"
    wan_gguf(m / "Wan2.2-T2V-A14B-LowNoise.gguf", 16)
    lora(m / "loras" / "a" / "one.safetensors")
    lora(m / "loras" / "b" / "two.safetensors")
    [p] = scan_folder(tmp_path)
    assert [Path(x["file"]).name for x in p["loras"]] == ["one.safetensors"]
    assert [n for n in p["notes"] if "not added" in n] and all("two" not in n for n in p["notes"])
    lora(m / "loras" / "direct.safetensors")
    [p] = scan_folder(tmp_path)
    assert [Path(x["file"]).name for x in p["loras"]] == ["direct.safetensors"]


def test_image_proposals_have_no_loras_or_settings(tmp_path):
    safet(tmp_path / "xl.safetensors", ["conditioner.embedders.0.w"])
    lora(tmp_path / "loras" / "style.safetensors")
    [p] = scan_folder(tmp_path)
    assert p["loras"] == [] and p["settings"] == {}


def test_underscore_folders_are_ignored(tmp_path):
    wan_gguf(tmp_path / "_unused" / "Wan2.2-TI2V-5B-Q6_K.gguf", 48)
    safet(tmp_path / "_old" / "xl.safetensors", ["conditioner.embedders.0.w"])
    assert scan_folder(tmp_path) == []


def build_video_tree(root: Path):
    """The layout of the user's video models folder, with synthetic headers."""
    t5(root / "shared" / "umt5-xxl-encoder-Q5_K_M.gguf")
    wan_vae(root / "shared" / "VAE" / "Wan2.1_VAE.safetensors", 16)
    wan_gguf(root / "wan22-ti2v-5b" / "Wan2.2-TI2V-5B-Q6_K.gguf", 48)
    wan_vae(root / "wan22-ti2v-5b" / "VAE" / "Wan2.2_VAE.safetensors", 48)
    t = root / "wan22-t2v-a14b"
    wan_gguf(t / "HighNoise" / "Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf", 16)
    wan_gguf(t / "LowNoise" / "Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf", 16)
    for st in ("high", "low"):
        lora(t / "loras" / f"wan2.2_t2v_A14b_{st}_noise_lora_rank64_lightx2v_4step_1217.safetensors")
    i = root / "wan22-i2v-a14b-nsfw"
    wan_gguf(i / "HighNoise" / "dasiwaWAN22I2V14B_midnightflirtHigh-Q4_K_M.gguf", 36)
    wan_gguf(i / "LowNoise" / "dasiwaWAN22I2V14B_midnightflirtLow-Q4_K_M.gguf", 36)
    for st in ("high", "low"):
        lora(i / "loras" / f"wan2.2_i2v_A14b_{st}_noise_lora_rank64_lightx2v_4step_1022.safetensors")
    lora(i / "loras" / "NSFW-22-H-e8.safetensors", "A")
    lora(i / "loras" / "NSFW-22-L-e8.safetensors", "A")
    x = root / "ltx23-distilled"
    ltx(x / "distilled-1.1" / "ltx-2.3-22b-distilled-1.1-Q3_K_M.gguf")
    connectors(x / "text_encoders" / "ltx-2.3-22b-distilled_embeddings_connectors.safetensors")
    ltx_vae(x / "vae" / "ltx-2.3-22b-distilled_video_vae.safetensors")
    ltx_audio_vae(x / "vae" / "ltx-2.3-22b-distilled_audio_vae.safetensors")
    gemma(x / "gemma-3-12b-it-abliterated-v2.q4_k_m.gguf")
    lora(root / "_unused" / "wan22-ti2v-5b-LoRAs" / "Wan22-Turbo" / "Wan22_TI2V_5B_Turbo_lora_rank_64_fp16.safetensors")
    wan_gguf(root / "_unused" / "Wan2.2-TI2V-5B-Q8_0.gguf", 48)
    # a download in progress lives under the dot-folder .cache
    wan_gguf(t / ".cache" / "huggingface" / "LowNoise" / "x.gguf", 16)


def test_scan_of_the_real_video_layout(tmp_path):
    root = tmp_path / "video"
    build_video_tree(root)
    props = scan_folder(root)
    assert sorted(p["family"] for p in props) == [
        "ltx-2.3-distilled", "wan2.2-i2v-a14b", "wan2.2-t2v-a14b", "wan2.2-ti2v-5b"]
    for p in props:
        assert p["missing"] == [], p
    by = by_family(props)

    def name(p, slot):
        return Path(p["files"][slot]).name
    ti2v = by["wan2.2-ti2v-5b"]
    assert (name(ti2v, "t5xxl"), name(ti2v, "vae")) == ("umt5-xxl-encoder-Q5_K_M.gguf", "Wan2.2_VAE.safetensors")
    assert ti2v["loras"] == [] and ti2v["settings"] == {}
    t2v = by["wan2.2-t2v-a14b"]
    assert name(t2v, "diffusion") == "Wan2.2-T2V-A14B-LowNoise-Q4_K_M.gguf"
    assert name(t2v, "high_noise") == "Wan2.2-T2V-A14B-HighNoise-Q4_K_M.gguf"
    assert name(t2v, "vae") == "Wan2.1_VAE.safetensors"
    assert sorted(x["stage"] for x in t2v["loras"]) == ["high", "low"]
    assert t2v["settings"]["steps"] == 4
    i2v = by["wan2.2-i2v-a14b"]
    assert name(i2v, "diffusion") == "dasiwaWAN22I2V14B_midnightflirtLow-Q4_K_M.gguf"
    assert name(i2v, "high_noise") == "dasiwaWAN22I2V14B_midnightflirtHigh-Q4_K_M.gguf"
    assert len(i2v["loras"]) == 4
    assert sorted(x["stage"] for x in i2v["loras"]) == ["high", "high", "low", "low"]
    assert i2v["settings"]["steps"] == 4
    lt = by["ltx-2.3-distilled"]
    assert {s: name(lt, s) for s in ("llm", "connectors", "vae")} == {
        "llm": "gemma-3-12b-it-abliterated-v2.q4_k_m.gguf",
        "connectors": "ltx-2.3-22b-distilled_embeddings_connectors.safetensors",
        "vae": "ltx-2.3-22b-distilled_video_vae.safetensors"}


def test_scanned_video_proposals_pass_the_model_checks(tmp_path):
    """LoRA stages and settings in each proposal are what the model store accepts."""
    from primer.images.models import clean_loras, clean_settings
    build_video_tree(tmp_path)
    for p in scan_folder(tmp_path):
        assert clean_loras(p["family"], p["loras"]) == p["loras"]
        assert clean_settings(p["family"], p["settings"]) == p["settings"]
