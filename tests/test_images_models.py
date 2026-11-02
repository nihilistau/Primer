import json

import pytest

from primer.images.families import FAMILIES
from primer.images.models import (ImageModel, ModelStore, build_flags, clean_settings,
                                  effective_settings, validate_model)


def touch(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def sdxl(tmp_path, **settings):
    return ImageModel("", "My SDXL", "sdxl", {"model": touch(tmp_path, "a.safetensors")}, settings)


def test_families_values():
    f = FAMILIES["sdxl"]
    assert (f.recipe, f.size_multiple, f.prompt_style) == ("sdxl-checkpoint", 8, "tags")
    assert [(s.name, s.flag, s.exts, s.required) for s in f.slots] == [
        ("model", "-m", (".safetensors", ".gguf"), True),
        ("vae", "--vae", (".safetensors", ".gguf"), False)]
    assert f.defaults == {"width": 1024, "height": 1024, "steps": 20, "cfg": 6,
                          "sampler": "euler_a", "vae_tiling": True, "offload": False}
    q = FAMILIES["qwen-image-2.1"]
    assert (q.recipe, q.size_multiple, q.prompt_style) == ("qwen-image-21", 32, "prose")
    assert [(s.name, s.flag, s.exts, s.required) for s in q.slots] == [
        ("diffusion", "--diffusion-model", (".gguf", ".safetensors"), True),
        ("llm", "--llm", (".gguf", ".safetensors"), True),
        ("vae", "--vae", (".safetensors",), True)]
    assert q.defaults == {"width": 1024, "height": 1024, "steps": 20, "cfg": 4,
                          "sampler": "euler", "offload": True}
    m = FAMILIES["moss-image"]
    assert (m.recipe, m.size_multiple, m.prompt_style) == ("moss-image-sft", 16, "prose")
    assert [(s.name, s.flag, s.required) for s in m.slots] == [
        ("diffusion", "--diffusion-model", True), ("llm", "--llm", True),
        ("vae", "--vae", True), ("tokenizer", "--tokenizer", True)]
    assert m.slots[3].exts == (".json",)
    assert m.defaults == {"width": 1024, "height": 1024, "steps": 20, "cfg": 3,
                          "sampler": "euler", "flow_shift": 5, "offload": True}


def test_store_roundtrip_and_default(tmp_path):
    store = ModelStore(tmp_path)
    a = store.save(sdxl(tmp_path))
    b = store.save(ImageModel("", "My SDXL", "sdxl", {"model": "/x.gguf"}))
    assert a.id != b.id and a.id.startswith("my-sdxl-")
    store.set_default(b.id)
    again = ModelStore(tmp_path)
    assert [m.id for m in again.list()] == [a.id, b.id]
    assert again.default_id() == b.id
    assert again.get(a.id).files == a.files
    assert json.loads((tmp_path / "images.json").read_text())["default"] == b.id
    # saving again keeps the id
    assert again.save(a).id == a.id and len(again.list()) == 2
    again.delete(b.id)  # the default: the first remaining model takes over
    assert again.default_id() == a.id and [m.id for m in again.list()] == [a.id]
    again.delete(a.id)
    assert again.default_id() is None and again.list() == []


def test_first_saved_model_becomes_default(tmp_path):
    store = ModelStore(tmp_path)
    assert store.default_id() is None
    a = store.save(sdxl(tmp_path))
    assert store.default_id() == a.id
    b = store.save(ImageModel("", "Second", "sdxl", {"model": "/x.gguf"}))
    assert store.default_id() == a.id  # a later save doesn't move it
    store.set_default(b.id)
    store.save(a)  # re-saving another model keeps the chosen default
    assert store.default_id() == b.id


def test_deleting_default_falls_back_to_first_remaining(tmp_path):
    store = ModelStore(tmp_path)
    a = store.save(sdxl(tmp_path))
    b = store.save(ImageModel("", "Second", "sdxl", {"model": "/x.gguf"}))
    c = store.save(ImageModel("", "Third", "sdxl", {"model": "/y.gguf"}))
    store.set_default(b.id)
    store.delete(a.id)  # not the default: unchanged
    assert store.default_id() == b.id
    store.delete(b.id)
    assert store.default_id() == c.id


def test_validate_reports_missing_slot_file(tmp_path):
    m = ImageModel("i", "n", "qwen-image-2.1", {
        "diffusion": touch(tmp_path, "d.gguf"), "llm": str(tmp_path / "gone.gguf"),
        "vae": touch(tmp_path, "v.safetensors")})
    assert validate_model(m) == [f"llm: file not found: {tmp_path / 'gone.gguf'}"]
    m.files.pop("llm")
    assert validate_model(m) == ["llm: a file is required"]
    assert validate_model(sdxl(tmp_path)) == []


def test_validate_refuses_pickle(tmp_path):
    m = ImageModel("i", "n", "sdxl", {"model": touch(tmp_path, "a.ckpt")})
    assert "pickle" in validate_model(m)[0]
    m.files["model"] = touch(tmp_path, "a.png")
    assert "not allowed" in validate_model(m)[0]


def test_validate_unknown_family():
    assert "unknown family" in validate_model(ImageModel("i", "n", "nope", {}))[0]


def test_clean_settings_clamps_rounds_and_drops_unknown():
    out = clean_settings("qwen-image-2.1", {"width": 1000, "steps": 999, "rm": 1, "flow_shift": None})
    assert out == {"width": 992, "steps": 150, "flow_shift": None}
    assert clean_settings("sdxl", {"width": 100})["width"] == 256
    assert clean_settings("sdxl", {"cfg": 99})["cfg"] == 30.0
    with pytest.raises(ValueError):
        clean_settings("sdxl", {"cfg": "x"})
    with pytest.raises(ValueError):
        clean_settings("sdxl", {"sampler": "bogus"})
    with pytest.raises(ValueError):
        clean_settings("sdxl", {"offload": 1})


def test_build_flags_per_family(tmp_path):
    s = sdxl(tmp_path)
    assert build_flags(s) == ["-m", s.files["model"], "--vae-tiling"]
    s.settings = {"vae_tiling": False, "offload": True, "flash_attn": True}
    assert build_flags(s) == ["-m", s.files["model"], "--offload-to-cpu",
                              "--flash-attn", "off,diffusion=on"]
    q = ImageModel("", "q", "qwen-image-2.1", {
        "diffusion": touch(tmp_path, "d.gguf"), "llm": touch(tmp_path, "l.gguf"),
        "vae": touch(tmp_path, "v.safetensors")})
    base = ["--diffusion-model", q.files["diffusion"], "--llm", q.files["llm"], "--vae", q.files["vae"]]
    assert build_flags(q) == base + ["--offload-to-cpu"]
    q.settings = {"offload": False, "vae_tiling": True}
    assert build_flags(q) == base + ["--vae-tiling"]
    m = ImageModel("", "m", "moss-image", {
        "diffusion": touch(tmp_path, "d2.gguf"), "llm": touch(tmp_path, "l2.gguf"),
        "vae": touch(tmp_path, "v2.safetensors"), "tokenizer": touch(tmp_path, "t.json")})
    mbase = ["--diffusion-model", m.files["diffusion"], "--llm", m.files["llm"],
             "--vae", m.files["vae"], "--tokenizer", m.files["tokenizer"]]
    assert build_flags(m) == mbase + ["--offload-to-cpu", "--flow-shift", "5.0"]
    m.settings = {"offload": False, "flow_shift": None}
    assert build_flags(m) == mbase


def test_effective_settings_layering(tmp_path):
    m = sdxl(tmp_path, steps=30, cfg=7)
    eff = effective_settings(m, {"steps": 10, "width": 1001, "junk": 1})
    assert eff["steps"] == 10 and eff["cfg"] == 7.0 and eff["width"] == 1000
    assert eff["sampler"] == "euler_a" and "junk" not in eff


# ---- fix round 1 ----
import os
import logging

from primer.images import models as models_mod
from primer.images.models import StoreCorrupt


def test_missing_store_is_empty(tmp_path):
    assert ModelStore(tmp_path).list() == []


def test_corrupt_store_refuses_and_preserves(tmp_path):
    store = ModelStore(tmp_path)
    store.save(sdxl(tmp_path))
    raw = (tmp_path / "images.json").read_bytes()[:-20]
    (tmp_path / "images.json").write_bytes(raw)
    with pytest.raises(StoreCorrupt):
        store.list()
    with pytest.raises(StoreCorrupt):
        store.save(sdxl(tmp_path))
    with pytest.raises(StoreCorrupt):
        store.delete("x")
    with pytest.raises(StoreCorrupt):
        store.set_default("x")
    assert (tmp_path / "images.json").read_bytes() == raw
    baks = list(tmp_path.glob("images.json.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == raw


def test_wrong_top_level_shape_is_corrupt(tmp_path):
    (tmp_path / "images.json").write_text("[1, 2]")
    with pytest.raises(StoreCorrupt):
        ModelStore(tmp_path).list()


def test_load_tolerates_bad_entries(tmp_path, caplog):
    good = touch(tmp_path, "a.safetensors")
    data = {"models": [
        {"id": "ok-1", "name": "ok", "family": "sdxl", "files": {"model": good},
         "settings": {"steps": 5, "cfg": "bad", "rm": 1}, "extra": 9},
        "junk", {"id": "n", "family": "sdxl"}, {"id": "stale-1", "name": "s", "family": "gone"}],
        "default": "ok-1"}
    (tmp_path / "images.json").write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        ms = ModelStore(tmp_path).list()
    assert [m.id for m in ms] == ["ok-1", "stale-1"]
    assert ms[0].settings == {"steps": 5}
    assert caplog.records
    assert any("unknown family" in r for r in validate_model(ms[1])) or "unknown family" in validate_model(ms[1])[0]
    with pytest.raises(ValueError, match="unknown family"):
        build_flags(ms[1])
    with pytest.raises(ValueError, match="unknown family"):
        effective_settings(ms[1])


def test_non_list_models_is_corrupt(tmp_path):
    (tmp_path / "images.json").write_text(json.dumps({"models": {"a": 1}}))
    with pytest.raises(StoreCorrupt):
        ModelStore(tmp_path).list()


@pytest.mark.parametrize("bad", ["--api-key", "rel/a.safetensors", "a.safetensors",
                                 "/tmp/a\n.safetensors", "/tmp/a\r.safetensors", "/tmp/a\0.safetensors"])
def test_bad_paths_rejected(tmp_path, bad):
    m = ImageModel("", "n", "sdxl", {"model": bad})
    reasons = validate_model(m)
    assert reasons and reasons[0].startswith("model:")
    with pytest.raises(ValueError, match="model"):
        ModelStore(tmp_path).save(m)
    with pytest.raises(ValueError):
        build_flags(m)


def test_build_flags_on_unusable_model_raises(tmp_path):
    with pytest.raises(ValueError, match="file not found"):
        build_flags(ImageModel("", "n", "sdxl", {"model": str(tmp_path / "nope.gguf")}))


def test_clean_settings_nonfinite():
    for bad in (float("inf"), 1e999, float("nan")):
        for key in ("cfg", "steps", "flow_shift"):
            with pytest.raises(ValueError):
                clean_settings("sdxl", {key: bad})


def test_id_rules_and_unknown_ids(tmp_path):
    store = ModelStore(tmp_path)
    kept = store.save(ImageModel("good-id-1", "n", "sdxl", {"model": touch(tmp_path, "a.gguf")}))
    assert kept.id == "good-id-1"
    for bad in ("../evil", "Upper", "-x", "a b", "x" * 65):
        got = store.save(ImageModel(bad, "n", "sdxl", {"model": touch(tmp_path, "a.gguf")}))
        assert got.id != bad and got.id.startswith("n-")
    before = (tmp_path / "images.json").read_bytes()
    with pytest.raises(KeyError):
        store.delete("nope")
    with pytest.raises(KeyError):
        store.set_default("nope")
    assert (tmp_path / "images.json").read_bytes() == before


def test_not_readable(tmp_path, monkeypatch):
    m = sdxl(tmp_path)
    monkeypatch.setattr(models_mod.os, "access", lambda p, mode: False)
    assert "not readable" in validate_model(m)[0]


def test_extra_coverage(tmp_path):
    assert clean_settings("sdxl", {"seed": -5})["seed"] == -1
    assert clean_settings("sdxl", {"seed": 2**40})["seed"] == 2**31 - 1
    assert len(clean_settings("sdxl", {"negative": "x" * 1500})["negative"]) == 1000
    with pytest.raises(ValueError):
        clean_settings("sdxl", {"steps": 20.5})
    assert clean_settings("sdxl", {"steps": 20.0})["steps"] == 20
    assert clean_settings("sdxl", {"width": 1023})["width"] == 1016
    q = ImageModel("", "q", "qwen-image-2.1", {"diffusion": touch(tmp_path, "d.gguf"),
                                               "llm": touch(tmp_path, "l.gguf"),
                                               "vae": touch(tmp_path, "v.gguf")})
    assert "vae: extension .gguf not allowed" in validate_model(q)[0]
    store = ModelStore(tmp_path)
    for bad in (ImageModel("", " ", "sdxl", {}), ImageModel("", "n", "nope", {}),
                ImageModel("", "n", "sdxl", {"model": 5})):
        with pytest.raises(ValueError):
            store.save(bad)


def test_invalid_utf8_is_corrupt(tmp_path):
    raw = b"\xff\xfe{"
    (tmp_path / "images.json").write_bytes(raw)
    with pytest.raises(StoreCorrupt):
        ModelStore(tmp_path).list()
    assert (tmp_path / "images.json").read_bytes() == raw
    baks = list(tmp_path.glob("images.json.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == raw


def test_id_with_trailing_newline_regenerated(tmp_path):
    got = ModelStore(tmp_path).save(ImageModel("ok-1\n", "n", "sdxl", {"model": touch(tmp_path, "a.gguf")}))
    assert got.id != "ok-1\n" and "\n" not in got.id


@pytest.mark.parametrize("bad", [r"\\server\share\a.safetensors", "//server/share/a.safetensors",
                                 r"\\?\C:\m\a.safetensors", r"\\.\PhysicalDrive0"])
def test_network_and_device_paths_rejected(tmp_path, bad):
    m = ImageModel("", "n", "sdxl", {"model": bad})
    reasons = validate_model(m)
    assert reasons and "network and device paths aren't allowed" in reasons[0]
    with pytest.raises(ValueError, match="network and device paths"):
        ModelStore(tmp_path).save(m)


def test_concurrent_saves_lose_nothing(tmp_path):
    import threading
    store = ModelStore(tmp_path)
    path = touch(tmp_path, "a.safetensors")
    errors = []

    def worker(n):
        try:
            for i in range(6):
                ModelStore(tmp_path).save(ImageModel("", f"m{n}-{i}", "sdxl", {"model": path}))
                store.list()  # readers race the writers too
        except Exception as e:  # noqa: BLE001 - collected for the assert
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list()) == 48
    assert not list(tmp_path.glob("images.json.bak-*"))


def test_replace_retries_a_transient_permission_error(tmp_path, monkeypatch):
    import os
    store = ModelStore(tmp_path)
    store.save(sdxl(tmp_path))
    real, fails = os.replace, [2]

    def flaky(src, dst):
        if str(dst).endswith("images.json") and fails[0]:
            fails[0] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(models_mod.os, "replace", flaky)
    saved = store.save(ImageModel("", "Second", "sdxl", {"model": "/x.gguf"}))
    assert fails == [0] and store.get(saved.id) is not None
    assert not list(tmp_path.glob("*.tmp"))


def test_load_retries_a_transient_permission_error(tmp_path, monkeypatch):
    from pathlib import Path
    store = ModelStore(tmp_path)
    a = store.save(sdxl(tmp_path))
    real, fails = Path.read_text, [2]

    def flaky(self, *args, **kw):
        if self.name == "images.json" and fails[0]:
            fails[0] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert [m.id for m in store.list()] == [a.id]
    assert not list(tmp_path.glob("images.json.bak-*"))  # not treated as corrupt


# ---- phase 3: video families, LoRAs, a default per kind ----
from primer.images.models import available_for, lora_entries


def wan14(tmp_path, family="wan2.2-t2v-a14b", **kw):
    return ImageModel("", "Wan", family, {
        "diffusion": touch(tmp_path, "low.gguf"), "high_noise": touch(tmp_path, "high.gguf"),
        "t5xxl": touch(tmp_path, "t5.safetensors"), "vae": touch(tmp_path, "wan_vae.safetensors")}, **kw)


def ltx(tmp_path, **kw):
    return ImageModel("", "LTX", "ltx-2.3-distilled", {
        "diffusion": touch(tmp_path, "ltx.gguf"), "llm": touch(tmp_path, "gemma.gguf"),
        "connectors": touch(tmp_path, "conn.safetensors"), "vae": touch(tmp_path, "ltx_vae.safetensors")}, **kw)


def lora_file(tmp_path, name, sub="loras"):
    d = tmp_path / sub
    d.mkdir(exist_ok=True)
    return touch(d, name)


def test_video_families_declared():
    vids = {k: f for k, f in FAMILIES.items() if f.kind == "video"}
    assert set(vids) == {"wan2.2-ti2v-5b", "wan2.2-t2v-a14b", "wan2.2-i2v-a14b", "ltx-2.3-distilled", "ltx-2.3"}
    assert {f.kind for k, f in FAMILIES.items() if k not in vids} == {"image"}
    assert all(f.frame_step == 0 and not f.needs_init and f.fast_defaults == {}
               for k, f in FAMILIES.items() if k not in vids)
    t = FAMILIES["wan2.2-ti2v-5b"]
    assert (t.label, t.recipe, t.frame_step, t.needs_init, t.size_multiple) == (
        "Wan 2.2 TI2V 5B", "wan22-ti2v-5b", 4, False, 32)
    assert [(s.name, s.flag, s.required) for s in t.slots] == [
        ("diffusion", "--diffusion-model", True), ("t5xxl", "--t5xxl", True), ("vae", "--vae", True)]
    assert t.defaults == {"width": 832, "height": 480, "frames": 49, "fps": 24, "steps": 20, "cfg": 5,
                          "sampler": "euler", "flow_shift": 5, "offload": True, "vae_tiling": True}
    assert t.fast_defaults == {}
    a = FAMILIES["wan2.2-t2v-a14b"]
    assert (a.label, a.recipe, a.frame_step, a.needs_init, a.size_multiple) == (
        "Wan 2.2 T2V A14B", "wan22-t2v", 4, False, 16)
    assert [(s.name, s.flag, s.required) for s in a.slots] == [
        ("diffusion", "--diffusion-model", True), ("high_noise", "--high-noise-diffusion-model", True),
        ("t5xxl", "--t5xxl", True), ("vae", "--vae", True)]
    assert a.defaults == {"width": 832, "height": 480, "frames": 33, "fps": 16, "steps": 20, "cfg": 3.5, "sampler": "euler",
                          "high_noise_steps": -1, "high_noise_cfg": 3.5, "moe_boundary": 0.875,
                          "flow_shift": 5, "offload": True, "vae_tiling": True}
    assert a.fast_defaults == {"steps": 4, "high_noise_steps": 2, "cfg": 1, "high_noise_cfg": 1}
    i = FAMILIES["wan2.2-i2v-a14b"]
    assert (i.label, i.recipe, i.frame_step, i.needs_init) == ("Wan 2.2 I2V A14B", "wan22-i2v", 4, True)
    assert i.slots == a.slots and i.defaults == {**a.defaults, "moe_boundary": 0.9}
    assert i.fast_defaults == a.fast_defaults
    d = FAMILIES["ltx-2.3-distilled"]
    assert (d.label, d.recipe, d.frame_step, d.needs_init, d.size_multiple) == (
        "LTX-2.3 distilled", "ltx23-distilled", 8, False, 32)
    assert [(s.name, s.flag, s.required) for s in d.slots] == [
        ("diffusion", "--diffusion-model", True), ("llm", "--llm", True),
        ("connectors", "--embeddings-connectors", True), ("vae", "--vae", True)]
    assert d.defaults == {"width": 768, "height": 512, "frames": 49, "fps": 24, "steps": 8, "cfg": 1,
                          "sampler": "euler", "offload": True, "vae_tiling": True}
    v = FAMILIES["ltx-2.3"]
    assert (v.label, v.recipe, v.frame_step) == ("LTX-2.3 dev", "ltx23-dev", 8)
    assert v.slots == d.slots and v.defaults == {**d.defaults, "steps": 20, "cfg": 3}


def test_frames_aligned_down_per_family():
    wan, lx = "wan2.2-t2v-a14b", "ltx-2.3"
    assert clean_settings(wan, {"frames": 34})["frames"] == 33
    assert clean_settings(wan, {"frames": 32})["frames"] == 29
    assert clean_settings(lx, {"frames": 50})["frames"] == 49
    assert clean_settings(lx, {"frames": 48})["frames"] == 41
    for fam in (wan, lx, "wan2.2-ti2v-5b"):
        assert clean_settings(fam, {"frames": 0})["frames"] == 1
        assert clean_settings(fam, {"frames": 999})["frames"] == 241
        assert clean_settings(fam, {"frames": 3})["frames"] == 1
    assert clean_settings(wan, {"fps": 0, "moe_boundary": 2, "high_noise_steps": -9,
                                "high_noise_cfg": 99}) == {
        "fps": 1, "moe_boundary": 1.0, "high_noise_steps": -1, "high_noise_cfg": 30.0}
    assert clean_settings(wan, {"fps": 999})["fps"] == 60
    # video sizes stop at 1280, images keep 2048
    assert clean_settings(wan, {"width": 4000, "height": 100}) == {"width": 1280, "height": 256}
    assert clean_settings("sdxl", {"width": 4000})["width"] == 2048
    # family defaults are already aligned: cleaning them changes nothing
    for f in FAMILIES.values():
        if f.kind == "video":
            assert clean_settings(f.id, f.defaults) == f.defaults


def test_video_settings_rejected_for_image_family():
    vid = {"frames": 33, "fps": 16, "high_noise_steps": 4, "high_noise_cfg": 1, "moe_boundary": 0.9}
    assert clean_settings("sdxl", {**vid, "steps": 5}) == {"steps": 5}
    assert clean_settings("qwen-image-2.1", vid) == {}
    # two-stage settings mean nothing to a single-stage video family
    assert clean_settings("ltx-2.3", vid) == {"frames": 33, "fps": 16}
    assert clean_settings("wan2.2-ti2v-5b", vid) == {"frames": 33, "fps": 16}


def test_loras_validated(tmp_path):
    a = lora_file(tmp_path, "a.safetensors")
    b = lora_file(tmp_path, "b.safetensors")
    m = wan14(tmp_path, loras=[{"file": a, "strength": 1.0, "stage": "both"},
                               {"file": b, "strength": 0.5, "stage": "high"}])
    assert validate_model(m) == []
    other = lora_file(tmp_path, "c.safetensors", sub="elsewhere")
    m.loras.append({"file": other, "strength": 1, "stage": "low"})
    assert "all LoRAs must be in one folder" in validate_model(m)
    m.loras = [{"file": lora_file(tmp_path, "p.pt"), "strength": 1, "stage": "both"}]
    assert "pickle" in validate_model(m)[0]
    m.loras = [{"file": lora_file(tmp_path, "g.gguf"), "strength": 1, "stage": "both"}]
    assert "not allowed" in validate_model(m)[0]
    m.loras = [{"file": "rel/a.safetensors", "strength": 1, "stage": "both"}]
    assert "absolute" in validate_model(m)[0]
    m.loras = [{"file": str(tmp_path / "loras" / "gone.safetensors"), "strength": 1, "stage": "both"}]
    assert "file not found" in validate_model(m)[0]
    m.loras = [{"file": a, "strength": 1, "stage": "sideways"}]
    assert "stage" in validate_model(m)[0]
    # high/low only make sense with a high-noise model
    single = ltx(tmp_path, loras=[{"file": a, "strength": 1, "stage": "high"}])
    assert "stage" in validate_model(single)[0]
    single.loras[0]["stage"] = "both"
    assert validate_model(single) == []
    # saving clamps strength and refuses a bad stage or a relative path
    store = ModelStore(tmp_path)
    saved = store.save(wan14(tmp_path, loras=[{"file": a, "strength": 5, "stage": "low"},
                                              {"file": b, "strength": -1}]))
    assert saved.loras == [{"file": a, "strength": 2.0, "stage": "low"},
                           {"file": b, "strength": 0.0, "stage": "both"}]
    assert ModelStore(tmp_path).get(saved.id).loras == saved.loras
    for bad in ([{"file": a, "strength": 1, "stage": "sideways"}],
                [{"file": "rel.safetensors", "strength": 1}],
                [{"file": a, "strength": "x"}], [{"file": a, "strength": float("nan")}],
                ["junk"], "junk"):
        with pytest.raises(ValueError):
            store.save(wan14(tmp_path, loras=bad))
    with pytest.raises(ValueError, match="stage"):
        store.save(ltx(tmp_path, loras=[{"file": a, "strength": 1, "stage": "high"}]))


def test_build_flags_lora_dir_and_high_noise(tmp_path):
    m = wan14(tmp_path)
    base = ["--diffusion-model", m.files["diffusion"], "--high-noise-diffusion-model", m.files["high_noise"],
            "--t5xxl", m.files["t5xxl"], "--vae", m.files["vae"]]
    tail = ["--offload-to-cpu", "--vae-tiling", "--flow-shift", "5.0"]
    assert build_flags(m) == base + tail
    a = lora_file(tmp_path, "a.safetensors")
    m.loras = [{"file": a, "strength": 1, "stage": "both"}]
    m.settings = {"frames": 17, "fps": 8, "high_noise_steps": 3, "moe_boundary": 0.5}
    flags = build_flags(m)
    assert flags == base + ["--lora-model-dir", str(tmp_path / "loras")] + tail
    # request-time settings never reach the command line
    assert not any(f in " ".join(flags) for f in ("frames", "fps", "moe", "high-noise-steps", "cfg"))
    lx = ltx(tmp_path)
    assert build_flags(lx) == ["--diffusion-model", lx.files["diffusion"], "--llm", lx.files["llm"],
                               "--embeddings-connectors", lx.files["connectors"], "--vae", lx.files["vae"],
                               "--offload-to-cpu", "--vae-tiling"]


def test_lora_entries_both_on_two_stage_model_yields_two(tmp_path):
    a = lora_file(tmp_path, "a.safetensors")
    b = lora_file(tmp_path, "b.safetensors")
    c = lora_file(tmp_path, "c.safetensors")
    m = wan14(tmp_path, loras=[{"file": a, "strength": 0.8, "stage": "both"},
                               {"file": b, "strength": 1.0, "stage": "high"},
                               {"file": c, "strength": 0.5, "stage": "low"}])
    assert lora_entries(m) == [
        {"path": "a.safetensors", "multiplier": 0.8, "is_high_noise": False},
        {"path": "a.safetensors", "multiplier": 0.8, "is_high_noise": True},
        {"path": "b.safetensors", "multiplier": 1.0, "is_high_noise": True},
        {"path": "c.safetensors", "multiplier": 0.5, "is_high_noise": False}]
    single = ltx(tmp_path, loras=[{"file": a, "strength": 0.8, "stage": "both"}])
    assert lora_entries(single) == [{"path": "a.safetensors", "multiplier": 0.8, "is_high_noise": False}]
    assert lora_entries(sdxl(tmp_path)) == []


def test_defaults_per_kind(tmp_path):
    store = ModelStore(tmp_path)
    img = store.save(sdxl(tmp_path))
    assert store.default_id() == img.id and store.default_id("video") is None
    v1 = store.save(wan14(tmp_path))  # the first video model becomes the video default
    assert store.default_id("video") == v1.id and store.default_id() == img.id
    v2 = store.save(ltx(tmp_path))
    assert store.default_id("video") == v1.id
    store.set_default(v2.id)  # sets the video default, leaves the image one
    assert store.default_id("video") == v2.id and store.default_id("image") == img.id
    img2 = store.save(ImageModel("", "Second", "sdxl", {"model": "/x.gguf"}))
    store.set_default(img2.id)
    assert store.default_id() == img2.id and store.default_id("video") == v2.id
    data = json.loads((tmp_path / "images.json").read_text())
    assert (data["default"], data["video_default"]) == (img2.id, v2.id)
    store.delete(v2.id)  # falls back to a model of the same kind
    assert store.default_id("video") == v1.id and store.default_id() == img2.id
    store.delete(img2.id)
    assert store.default_id() == img.id and store.default_id("video") == v1.id
    store.delete(img.id)  # no image model left: no image default, video untouched
    assert store.default_id() is None and store.default_id("video") == v1.id
    store.delete(v1.id)
    assert store.default_id("video") is None


def test_resave_into_other_kind_moves_defaults(tmp_path):
    store = ModelStore(tmp_path)
    img = store.save(sdxl(tmp_path))
    img2 = store.save(ImageModel("", "Second", "sdxl", {"model": "/x.gguf"}))
    moved = wan14(tmp_path)
    moved.id = img.id  # the image default becomes a video model
    store.save(moved)
    assert store.default_id() == img2.id and store.default_id("video") == img.id


def test_phase2_store_loads_with_image_default(tmp_path):
    good = touch(tmp_path, "a.safetensors")
    data = {"models": [{"id": "old-1", "name": "Old", "family": "sdxl", "files": {"model": good},
                        "settings": {"steps": 12}},
                       {"id": "old-2", "name": "Older", "family": "sdxl", "files": {"model": good},
                        "settings": {}}],
            "default": "old-2"}
    (tmp_path / "images.json").write_text(json.dumps(data))
    store = ModelStore(tmp_path)
    ms = store.list()
    assert [m.id for m in ms] == ["old-1", "old-2"] and all(m.loras == [] for m in ms)
    assert ms[0].settings == {"steps": 12}
    assert store.default_id() == "old-2" and store.default_id("image") == "old-2"
    assert store.default_id("video") is None
    # a stored default pointing at a model of the other kind isn't honoured
    data["video_default"] = "old-1"
    (tmp_path / "images.json").write_text(json.dumps(data))
    assert store.default_id("video") is None and store.default_id() == "old-2"


def test_load_drops_bad_loras(tmp_path, caplog):
    good = lora_file(tmp_path, "a.safetensors")
    data = {"models": [{"id": "w-1", "name": "W", "family": "wan2.2-t2v-a14b", "files": {},
                        "loras": [{"file": good, "strength": 9, "stage": "high"}, "junk",
                                  {"file": good, "strength": "x"}, {"file": 5}]}]}
    (tmp_path / "images.json").write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        ms = ModelStore(tmp_path).list()
    assert ms[0].loras == [{"file": good, "strength": 2.0, "stage": "high"}]
    assert caplog.records


def test_video_available_reasons(tmp_path):
    store = ModelStore(tmp_path)
    assert available_for(store, "video") == (False, "no video model added yet")
    store.save(sdxl(tmp_path))
    assert available_for(store, "video") == (False, "no video model added yet")
    assert available_for(store, "image") == (True, "")
    v = store.save(wan14(tmp_path))
    assert available_for(store, "video") == (True, "")
    data = json.loads((tmp_path / "images.json").read_text())
    data["video_default"] = None
    (tmp_path / "images.json").write_text(json.dumps(data))
    assert available_for(store, "video") == (False, "no default video model chosen")
    store.set_default(v.id)
    os.remove(v.files["t5xxl"])
    ok, reason = available_for(store, "video")
    assert not ok and "t5xxl: file not found" in reason
    # a default that only animates a picture can't make a video from text: the reason says so,
    # in the words ImageService.create uses
    i2v = store.save(wan14(tmp_path, family="wan2.2-i2v-a14b"))
    store.set_default(i2v.id)
    assert available_for(store, "video") == (
        False, "Wan 2.2 I2V A14B only animates a picture: use Animate on an image")


def test_loras_only_for_video_families(tmp_path):
    a = lora_file(tmp_path, "a.safetensors")
    store = ModelStore(tmp_path)
    m = sdxl(tmp_path)
    m.loras = [{"file": a, "strength": 1.0, "stage": "both"}]
    with pytest.raises(ValueError, match="LoRAs are only used by video models"):
        store.save(m)
    assert "LoRAs are only used by video models" in validate_model(m)
    with pytest.raises(ValueError, match="LoRAs are only used by video models"):
        build_flags(m)
    m.loras = []
    assert "--lora-model-dir" not in build_flags(m)
    # an image model stored with LoRAs (by hand, or before this rule) loads and shows the reason
    data = {"models": [{"id": "img-1", "name": "Pic", "family": "sdxl",
                        "files": {"model": touch(tmp_path, "b.safetensors")},
                        "loras": [{"file": a, "strength": 1.0, "stage": "both"}]}], "default": "img-1"}
    (tmp_path / "images.json").write_text(json.dumps(data))
    loaded = ModelStore(tmp_path).get("img-1")
    assert loaded.loras == [{"file": a, "strength": 1.0, "stage": "both"}]
    assert validate_model(loaded) == ["LoRAs are only used by video models"]
    assert available_for(ModelStore(tmp_path), "image") == (False, "LoRAs are only used by video models")
    # re-saving it without them clears the problem
    loaded.loras = []
    assert validate_model(ModelStore(tmp_path).save(loaded)) == []
