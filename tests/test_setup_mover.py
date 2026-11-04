"""The model mover: copy configured model files to another folder, verify, update the paths."""

import json
import os
import threading
import tomllib
from pathlib import Path

import pytest

from primer.config import load_tier
from primer.images.models import ImageModel, ModelStore
from primer.setup import mover
from primer.setup.approvals import norm
from primer.setup.tierwriter import dumps


def _engine(model: Path, port: int, mmproj: Path | None = None) -> dict:
    launch = ["llama-server", "-m", model.as_posix(), "--port", str(port)]
    if mmproj:
        launch += ["--mmproj", mmproj.as_posix()]
    return {"kind": "openai", "base_url": f"http://127.0.0.1:{port}", "model": model.stem,
            "managed": True, "launch": launch, "extra": {"chat_template_kwargs": {"enable_thinking": False}}}


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "slow"
    src.mkdir()
    writing = src / "writing.gguf"
    writing.write_bytes(b"W" * 5000)
    voice = src / "voice.gguf"
    voice.write_bytes(b"V" * 3000)
    proj = src / "voice-mmproj.gguf"
    proj.write_bytes(b"P" * 1000)
    zim = src / "top.zim"
    zim.write_bytes(b"Z" * 2000)
    pic = src / "pic.safetensors"
    pic.write_bytes(b"I" * 4000)
    tiers = tmp_path / "tiers"
    tiers.mkdir()
    local = {"name": "local-light", "source_mode": "reference", "fast": "text",
             "text": _engine(writing, 8191),
             "reference": {"kiwix_serve": (src / "kiwix-serve.exe").as_posix(), "zims": [zim.as_posix()],
                           "port": 8830},
             "voice": {"kind": "lfm3", "base_url": "http://127.0.0.1:8840", "managed": True,
                       "launch": ["llama-server", "-m", voice.as_posix(), "--mmproj", proj.as_posix(),
                                  "--port", "8840"]}}
    (tiers / "local-light.toml").write_text(dumps(local), encoding="utf-8")
    mine = {"name": "mine", "fast": "text", "text": _engine(writing, 8200)}
    (tiers / "mine.toml").write_text("# my own tier\n" + dumps(mine), encoding="utf-8")
    var = tmp_path / "var"
    store = ModelStore(var)
    store.save(ImageModel(id="", name="Pic", family="sdxl", files={"model": str(pic)}, settings={}, loras=[]))
    dest = tmp_path / "fast"
    dest.mkdir()
    return {"tmp": tmp_path, "src": src, "tiers": tiers, "var": var, "store": store, "dest": dest,
            "writing": writing, "voice": voice, "proj": proj, "zim": zim, "pic": pic}


def test_list_models_from_tier_and_images(setup):
    tier = load_tier("local-light", setup["tiers"])
    rows = mover.list_models(tier, setup["store"], drive_type=lambda p: mover.DRIVE_REMOVABLE)
    by = {r["path"]: r for r in rows}
    assert set(by) == {str(setup[k]) for k in ("writing", "voice", "proj", "zim", "pic")}
    w = by[str(setup["writing"])]
    assert w["roles"] == ["text", "fast"] and w["source"] == "tier" and w["size"] == 5000
    assert w["removable"] is True and w["slow"] is True and w["drive"]
    assert by[str(setup["proj"])]["roles"] == ["voice_mmproj"]
    assert by[str(setup["zim"])]["roles"] == ["reference"]
    assert by[str(setup["pic"])]["source"] == "images" and by[str(setup["pic"])]["roles"] == ["image: Pic (model)"]
    rows = mover.list_models(tier, setup["store"], drive_type=lambda p: mover.DRIVE_FIXED)
    assert not any(r["removable"] or r["slow"] for r in rows)


def test_mover_copies_verifies_and_updates(setup):
    tier = load_tier("local-light", setup["tiers"])
    allowed = [r["path"] for r in mover.list_models(tier, setup["store"])]
    mine_before = (setup["tiers"] / "mine.toml").read_bytes()
    seen = []
    out = mover.move([str(setup["writing"]), str(setup["pic"]), str(setup["zim"])], setup["dest"],
                     allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"],
                     progress=lambda cur, done, total: seen.append((cur, done, total)))
    dest = setup["dest"]
    assert not out["failed"], out
    assert {(norm(m["from"]), norm(m["to"])) for m in out["moved"]} == {
        (norm(setup["writing"]), norm(dest / "writing.gguf")), (norm(setup["pic"]), norm(dest / "pic.safetensors")),
        (norm(setup["zim"]), norm(dest / "top.zim"))}
    # copied and verified; the originals are kept; no .part left behind
    assert (dest / "writing.gguf").read_bytes() == b"W" * 5000 and setup["writing"].exists()
    assert (dest / "pic.safetensors").read_bytes() == b"I" * 4000 and setup["pic"].exists()
    assert not list(dest.glob("*.part"))
    assert seen and seen[-1][1] == seen[-1][2] == 5000 + 4000 + 2000
    # Setup's own tier is rewritten; it still loads and points at the copies
    t = load_tier("local-light", setup["tiers"])
    assert norm(t.text.launch[2]) == norm(dest / "writing.gguf")
    assert [norm(z) for z in t.reference.zims] == [norm(dest / "top.zim")]
    assert norm(t.voice.launch[2]) == norm(setup["voice"])  # not moved: untouched
    raw = tomllib.loads((setup["tiers"] / "local-light.toml").read_text(encoding="utf-8"))
    assert raw["text"]["extra"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert out["tiers_updated"] == ["local-light"]
    # the user's own tier is never edited, only reported
    assert (setup["tiers"] / "mine.toml").read_bytes() == mine_before
    assert out["other_tiers"] == [{"tier": "mine", "path": str(setup["writing"])}]
    # var/images.json, through the store
    m = ModelStore(setup["var"]).list()[0]
    assert norm(m.files["model"]) == norm(dest / "pic.safetensors") and m.name == "Pic"
    assert out["images_updated"] == ["Pic"]


def test_move_refuses_paths_not_listed_and_bad_destinations(setup, monkeypatch):
    allowed = [str(setup["writing"])]
    kw = dict(allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    with pytest.raises(mover.MoveError, match="not a configured model file"):
        mover.move([str(setup["tmp"] / "other.gguf")], setup["dest"], **kw)
    with pytest.raises(mover.MoveError, match="absolute"):
        mover.move(allowed, "relative/dir", **kw)
    with pytest.raises(mover.MoveError, match="does not exist"):
        mover.move(allowed, setup["tmp"] / "nope", **kw)
    with pytest.raises(mover.MoveError, match="inside the Primer folder"):
        mover.move(allowed, mover.REPO_ROOT / "tests", **kw)
    with pytest.raises(mover.MoveError, match="already in"):
        mover.move(allowed, setup["src"], **kw)
    with pytest.raises(mover.MoveError, match="not enough space"):
        mover.move(allowed, setup["dest"], free_space=lambda p: 1000, **kw)
    with pytest.raises(mover.MoveError, match="choose"):
        mover.move([], setup["dest"], **kw)


def test_move_reuses_identical_and_refuses_a_different_file(setup):
    allowed = [str(setup["writing"]), str(setup["zim"])]
    (setup["dest"] / "writing.gguf").write_bytes(b"W" * 5000)  # identical: reused
    (setup["dest"] / "top.zim").write_bytes(b"not the same")  # different: refused, left alone
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    assert [norm(m["to"]) for m in out["moved"]] == [norm(setup["dest"] / "writing.gguf")]
    assert out["failed"][0]["path"] == str(setup["zim"]) and "different file" in out["failed"][0]["error"]
    assert (setup["dest"] / "top.zim").read_bytes() == b"not the same"
    t = load_tier("local-light", setup["tiers"])
    assert norm(t.text.launch[2]) == norm(setup["dest"] / "writing.gguf")
    assert [norm(z) for z in t.reference.zims] == [norm(setup["zim"])]


def test_move_bad_copy_is_not_used(setup, monkeypatch):
    allowed = [str(setup["writing"])]
    monkeypatch.setattr(mover, "sha256_file", lambda p: "0" * 64)  # the copy reads back wrong
    before = (setup["tiers"] / "local-light.toml").read_bytes()
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    assert out["moved"] == [] and "did not match" in out["failed"][0]["error"]
    assert not (setup["dest"] / "writing.gguf").exists() and not list(setup["dest"].iterdir())
    assert (setup["tiers"] / "local-light.toml").read_bytes() == before


def test_move_cancel_stops_and_cleans_up(setup):
    allowed = [str(setup["writing"]), str(setup["zim"])]
    cancel = threading.Event()
    cancel.set()
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"],
                     cancel=cancel)
    assert out["moved"] == [] and out["cancelled"] is True
    assert not list(setup["dest"].iterdir())


def test_unparseable_local_tier_is_reported(setup):
    (setup["tiers"] / "local-broken.toml").write_text("this is [not toml", encoding="utf-8")
    allowed = [str(setup["writing"])]
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    assert any(f["path"].endswith("local-broken.toml") and "could not be read" in f["error"] for f in out["failed"])
    assert out["tiers_updated"] == ["local-light"]


# ── fix round 1 ───────────────────────────────────────────────────────────────────────────────


def test_move_refuses_device_network_and_removable_destinations(setup):
    allowed = [str(setup["writing"])]
    kw = dict(allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    repo_tests = str(mover.REPO_ROOT / "tests")
    long_, device = r"\\?" + "\\", r"\\." + "\\"
    assert mover.inside(long_ + repo_tests, mover.REPO_ROOT)
    assert mover.inside(device + repo_tests, mover.REPO_ROOT)
    drive, rest = os.path.splitdrive(repo_tests)
    for probe in (long_ + repo_tests, device + repo_tests,
                  r"\\localhost" + "\\" + drive.rstrip(":") + "$" + rest, "//localhost/share/models"):
        with pytest.raises(mover.MoveError, match="local drive"):
            mover.move(allowed, probe, **kw)
    with pytest.raises(mover.MoveError, match="network drive"):
        mover.move(allowed, setup["dest"], drive_type=lambda p: mover.DRIVE_REMOTE, **kw)
    with pytest.raises(mover.MoveError, match="removable"):
        mover.move(allowed, setup["dest"], drive_type=lambda p: mover.DRIVE_REMOVABLE, **kw)
    assert setup["writing"].exists() and not list(setup["dest"].iterdir())


def test_setup_installed_files_are_not_movable(setup):
    tier = load_tier("local-light", setup["tiers"])
    src = norm(setup["src"])
    owned = lambda p: norm(p).startswith(src + os.sep) and p.endswith(".zim")
    rows = {r["path"]: r for r in mover.list_models(tier, setup["store"], owned=owned, reloads=True)}
    z = rows[str(setup["zim"])]
    assert z["setup"] is True and z["movable"] is False and z["why"] == mover.SETUP_OWNED
    w = rows[str(setup["writing"])]
    assert w["setup"] is False and w["movable"] is True and w["why"] == "" and w["reloads_tier"] is True
    assert rows[str(setup["pic"])]["reloads_tier"] is False  # images.json only
    allowed = list(rows)  # even if a caller lists it, move() refuses it
    with pytest.raises(mover.MoveError, match="installed by Setup"):
        mover.move([str(setup["zim"])], setup["dest"], allowed=allowed, tiers_dir=setup["tiers"],
                   var_dir=setup["var"], owned=owned)
    # and the Setup marker makes a folder Setup's with no setup.json at all
    from primer.setup.approvals import Approvals
    (setup["src"] / ".primer-setup.json").write_text('{"items": {}}', encoding="utf-8")
    assert Approvals(setup["var"]).owned_check()(str(setup["writing"]))
    assert not Approvals(setup["var"]).owned_check()(str(setup["dest"] / "x.gguf"))


def test_free_space_does_not_count_files_already_at_dest(setup):
    allowed = [str(setup["writing"]), str(setup["zim"])]
    (setup["dest"] / "writing.gguf").write_bytes(b"W" * 5000)  # identical: no space needed
    free = mover.SPACE_MARGIN + 2000  # enough for the zim alone
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"],
                     free_space=lambda p: free)
    assert len(out["moved"]) == 2 and not out["failed"]
    with pytest.raises(mover.MoveError, match="not enough space"):
        mover.move([str(setup["pic"])], setup["dest"], allowed=[str(setup["pic"])], tiers_dir=setup["tiers"],
                   var_dir=setup["var"], free_space=lambda p: free)


def test_images_update_rereads_the_model_under_the_lock(setup, monkeypatch):
    """A save between the mover's listing and its update (here: a rename) isn't lost."""
    real = ModelStore

    class Racing(real):
        def list(self):
            stale = super().list()
            m = stale[0]
            real(setup["var"]).save(ImageModel(id=m.id, name="Renamed", family=m.family, files=m.files,
                                               settings=m.settings, loras=m.loras))
            monkeypatch.setattr(mover, "ModelStore", real)
            return stale

    monkeypatch.setattr(mover, "ModelStore", Racing)
    allowed = [str(setup["pic"])]
    out = mover.move(allowed, setup["dest"], allowed=allowed, tiers_dir=setup["tiers"], var_dir=setup["var"])
    m = real(setup["var"]).list()[0]
    assert m.name == "Renamed" and norm(m.files["model"]) == norm(setup["dest"] / "pic.safetensors")
    assert out["images_updated"] == ["Renamed"]


def test_external_usb_disk_is_slow_and_never_the_default(monkeypatch):
    # an external USB disk reports DRIVE_FIXED; its bus type is what gives it away
    monkeypatch.setattr(mover, "bus_type", lambda p: 7 if str(p).upper().startswith("H:") else 17)
    assert mover.slow("H:/models/a.gguf", mover.DRIVE_FIXED)
    assert not mover.slow("D:/models/a.gguf", mover.DRIVE_FIXED)
    monkeypatch.setattr(mover, "drive_type", lambda p: mover.DRIVE_FIXED)
    monkeypatch.setattr(mover.sys, "platform", "win32")
    import ctypes
    if not hasattr(ctypes, "windll"):
        return
    assert "H:" not in mover.fixed_drives()
