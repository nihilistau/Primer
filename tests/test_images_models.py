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
