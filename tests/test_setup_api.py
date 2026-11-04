"""The /v1/setup/* routes and /v1/status's setup summary, over fake catalogues and downloads."""

import json
import threading
import time
from pathlib import Path

import pytest

from primer.config import REPO_ROOT, load_tier
from primer.images.models import ImageModel, ModelStore
from primer.setup import mover
from primer.setup.api import UPDATES_ERROR_TTL_S, UPDATES_TTL_S, SetupApi
from primer.setup.approvals import norm
from primer.setup.net import CatalogueError
from primer.setup.tierwriter import dumps
from tests.test_server import _raw
from tests.test_setup_installer import FakeDownloader, make, wait_for
from tests.test_setup_kits import FakeCatalogues


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def setup_app(app_parts, tmp_path):
    app, _ = app_parts
    dl = FakeDownloader(block="LFM3-Audio")
    app.installer = make(tmp_path, dl)
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: mover.DRIVE_FIXED)
    yield app, dl
    dl.release.set()
    app.setup_api.shutdown()
    app.installer.shutdown()


def _state(client):
    return client.get("/v1/setup")["state"]


def test_setup_api_flow(setup_app, client, tmp_path):
    app, dl = setup_app
    s = client.get("/v1/setup")
    assert [k["id"] for k in s["kits"]] == ["light", "hybrid", "workstation"]
    assert all(k["label"] and k["summary"] and k["size_hint"] > 0 for k in s["kits"])
    assert s["hardware"] == {"gpu": None, "vram_gb": 0.0, "ram_gb": 16.0}
    assert s["suggested"]["kit"] == "light" and s["suggested"]["reason"]
    assert s["state"]["state"] == "idle" and s["token"] is False
    assert Path(s["root_default"]).name == "primer-data" and not mover.inside(s["root_default"], REPO_ROOT)
    assert isinstance(s["free_bytes"], int)
    (app.var_dir / "keys").mkdir(parents=True)
    (app.var_dir / "keys" / "huggingface.key").write_text("hf_secret", encoding="utf-8")
    s = client.get("/v1/setup")
    assert s["token"] is True and "hf_secret" not in json.dumps(s)

    # the destination is checked
    root = tmp_path / "kit"
    for bad, why in (("relative/kit", "absolute"), (Path(tmp_path.anchor).as_posix(), "not the drive itself"),
                     (str(REPO_ROOT / "models"), "inside the Primer folder"),
                     (str(tmp_path / "no" / "such"), "does not exist")):
        assert why in client.post("/v1/setup/plan", {"kit": "light", "root": bad}, expect=400)["error"]
    assert "unknown kit" in client.post("/v1/setup/plan", {"kit": "nope", "root": str(root)}, expect=400)["error"]
    assert "choice" in client.post("/v1/setup/plan", {"kit": "light", "root": str(root),
                                                      "choices": {"map": "z99"}}, expect=400)["error"]
    app.setup_api._drive_type = lambda p: mover.DRIVE_REMOVABLE
    assert "removable" in client.post("/v1/setup/plan", {"kit": "light", "root": str(root)}, expect=400)["error"]
    app.setup_api._drive_type = lambda p: mover.DRIVE_FIXED

    plan = client.post("/v1/setup/plan", {"kit": "light", "root": str(root)})
    assert plan["kit"] == "light" and plan["items"] and plan["to_download"] > 0
    assert plan["fits"]["ok"] is True and plan["fits"]["shortfall_bytes"] == 0
    assert plan["fits"]["needed_bytes"] > plan["to_download"] and plan["fits"]["free_bytes"] > 0
    assert not root.exists()  # created on start, not on plan

    # start names the plan it was shown: another kit, folder or choices is a 409
    go = {"kit": "light", "root": str(root), "choices": {}}
    assert "kit" in client.post("/v1/setup/start", {}, expect=400)["error"]
    for other in ({**go, "kit": "hybrid"}, {**go, "root": str(tmp_path / "elsewhere")},
                  {**go, "choices": {"map": "z9"}}):
        assert "check it again" in client.post("/v1/setup/start", other, expect=409)["error"]
    # licences first, accepted by the key the plan lists (name and link)
    assert "licence" in client.post("/v1/setup/start", go, expect=400)["error"]
    assert all(l["key"] for l in plan["licences"])
    for lic in plan["licences"]:
        st = client.post("/v1/setup/licence", {"licence": lic["key"]})
    assert all(l["accepted"] for l in st["licences"])
    assert client.post("/v1/setup/licence", {}, expect=400)["error"]

    st = client.post("/v1/setup/start", go)
    assert st["state"] == "running" and root.is_dir()
    assert dl.blocked.wait(5)
    assert "already running" in client.post("/v1/setup/start", go, expect=409)["error"]
    assert client.get("/v1/status")["setup"]["running"] is True
    assert "downloading" in client.post("/v1/setup/move", {"paths": ["x"], "dest": str(tmp_path)}, expect=409)["error"]
    assert "pause" in client.post("/v1/setup/plan", {"kit": "light", "root": str(root)}, expect=409)["error"]
    assert client.post("/v1/setup/cancel", {"item": "bogus"}, expect=400)["error"]
    st = client.post("/v1/setup/pause", {})
    assert st["state"] == "paused"
    assert client.get("/v1/status")["setup"]["running"] is True
    dl.release.set()
    client.post("/v1/setup/resume", {})
    wait_for(lambda: _state(client)["state"] in ("ready", "failed"))
    st = _state(client)
    assert st["state"] == "ready", st
    assert client.get("/v1/status")["setup"] == {"needed": False, "running": False}

    # approvals: one file by the hash the UI showed, then the rest of the release
    rows = {r["file"]: r for r in st["approvals"]}
    exe = rows["llama-server.exe"]
    assert "sha256" in client.post("/v1/setup/approve", {"path": exe["path"]}, expect=400)["error"]
    assert "changed" in client.post("/v1/setup/approve", {"path": exe["path"], "sha256": "0" * 64},
                                    expect=400)["error"]
    assert "waiting" in client.post("/v1/setup/approve", {"path": str(tmp_path / "x.exe"), "sha256": "0" * 64},
                                    expect=400)["error"]
    out = client.post("/v1/setup/approve", {"path": exe["path"], "sha256": exe["sha256"]})
    assert out["approved"] == [exe["path"]]
    assert "llama-server.exe" not in {r["file"] for r in out["state"]["approvals"]}
    out = client.post("/v1/setup/approve-release", {"item": "llama_cpp"})
    assert [Path(p).name for p in out["approved"]] == ["ggml.dll"] and out["refused"] == []
    assert "nothing" in client.post("/v1/setup/approve-release", {"item": "llama_cpp"}, expect=400)["error"]
    assert {r["file"] for r in _state(client)["approvals"]} == {"kiwix-serve.exe"}


def test_setup_posts_follow_the_origin_rules(setup_app, client):
    for path in ("/v1/setup/plan", "/v1/setup/start", "/v1/setup/move", "/v1/setup/approve"):
        status, _ = _raw(client.port, "POST", path, b"{}",
                         {"Content-Type": "application/json", "Origin": "http://evil.example"})
        assert status == 403
        status, _ = _raw(client.port, "POST", path, b"{}", {"Content-Type": "text/plain"})
        assert status == 415
    status, _ = _raw(client.port, "GET", "/v1/setup/updates")  # does network work: needs X-Primer
    assert status == 403
    assert _state(client)["state"] == "idle"


def test_setup_routes_without_an_installer(client):
    assert client.get("/v1/setup", expect=503)["error"]
    assert client.post("/v1/setup/start", {}, expect=503)["error"]
    client.get("/v1/setup/nope", expect=404)
    assert client.get("/v1/status")["setup"] == {"needed": False, "running": False}


def test_status_setup_needed(app_parts, client):
    app, _ = app_parts
    text = app.servers._servers["text"]
    text._state, text._reason = "failed", "llama-server.exe not found"
    assert client.get("/v1/status")["setup"] == {"needed": True, "running": False}

    class Ready:
        def state(self):
            return "ready"

    app.installer = Ready()
    assert client.get("/v1/status")["setup"]["needed"] is False
    app.installer = None
    text._state, text._reason = "ready", ""
    assert client.get("/v1/status")["setup"]["needed"] is False


def _ready_installer(tmp_path):
    inst = make(tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    for lic in inst.status()["licences"]:
        inst.acknowledge(lic["key"])
    inst.start()
    wait_for(lambda: inst.status()["state"] in ("ready", "failed"))
    assert inst.status()["state"] == "ready"
    return inst


class NewerZim(FakeCatalogues):
    def __init__(self, broken_assets=False):
        super().__init__()
        self.broken_assets = broken_assets

    def zims(self, lang="eng", name_prefix="wikipedia_en_"):
        out = super().zims(lang, name_prefix)
        url = "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_top_mini_2026-10.zim"
        out.append({"name": "wikipedia_en_top", "flavour": "mini", "date": "2026-10-12", "size": 300000000,
                    "url": url, "sha256_url": url + ".sha256"})
        return out

    def latest_asset(self, repo, pattern, prefer_tag_prefix=None):
        if self.broken_assets:
            self.calls.append(("latest_asset", repo, pattern))
            raise CatalogueError("api.github.com answered 503")
        return super().latest_asset(repo, pattern, prefer_tag_prefix)


def test_updates_lists_newer_zim(app_parts, client, tmp_path):
    app, _ = app_parts
    app.installer = inst = _ready_installer(tmp_path)
    clock = Clock()
    app.setup_api = api = SetupApi(app, clock=clock)
    inst.catalogues = cat = NewerZim()
    rows = api.updates()
    assert rows == [{"item": "zim_top", "label": rows[0]["label"], "kind": "zim", "installed": "2026-09-14",
                     "latest": "2026-10-12", "size": 300000000}]
    # llama.cpp's latest release is the installed tag: not listed; asked with the item's own pattern
    assert any(c[0] == "latest_asset" and c[1] == "ggml-org/llama.cpp" and "cpu" in c[2] for c in cat.calls)
    n = len(cat.calls)
    assert api.updates() == rows and len(cat.calls) == n  # cached
    clock.t += UPDATES_TTL_S + 1
    inst.catalogues = cat = NewerZim(broken_assets=True)
    rows = client.get("/v1/setup/updates")  # after 10 minutes: asked again; errors per item, never a 500
    assert any(c[0] == "zims" for c in cat.calls)
    err = next(r for r in rows if r["item"] == "llama_cpp")
    assert "503" in err["error"] and err["installed"] == "b12102"
    assert any(r["item"] == "zim_top" and r["latest"] == "2026-10-12" for r in rows)
    inst.shutdown()


def test_updates_with_nothing_installed(setup_app, client):
    assert client.get("/v1/setup/updates") == []


def test_setup_models_and_move(app_parts, client, tmp_path, monkeypatch):
    app, tiers = app_parts
    app.installer = inst = make(tmp_path)
    src, dest = tmp_path / "usb", tmp_path / "ssd"
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: (
        mover.DRIVE_REMOVABLE if mover.inside(p, src) else mover.DRIVE_FIXED))
    src.mkdir()
    dest.mkdir()
    model = src / "writing.gguf"
    model.write_bytes(b"W" * 3000)
    pic = src / "pic.safetensors"
    pic.write_bytes(b"I" * 2000)
    tier = {"name": "local-light", "fast": "text",
            "text": {"kind": "openai", "base_url": "http://127.0.0.1:1", "managed": True,
                     "launch": ["llama-server", "-m", model.as_posix(), "--port", "1"]}}
    (tiers / "local-light.toml").write_text(dumps(tier), encoding="utf-8")
    app.tier = load_tier("local-light", tiers)  # as if it were the current tier (no servers started)
    app.store.save(ImageModel(id="", name="Pic", family="sdxl", files={"model": str(pic)}, settings={}, loras=[]))
    reloaded = []
    monkeypatch.setattr(app, "reload_tier", lambda name: reloaded.append(name))

    got = client.get("/v1/setup/models")
    rows = {r["path"]: r for r in got["models"]}
    assert set(rows) == {str(model), str(pic)} and got["move"]["state"] == "idle"
    assert rows[str(model)]["roles"] == ["text", "fast"] and rows[str(model)]["slow"] is True
    assert rows[str(model)]["reloads_tier"] is True and rows[str(pic)]["reloads_tier"] is False
    assert all(r["movable"] and not r["setup"] for r in rows.values())
    assert "not a configured" in client.post("/v1/setup/move", {"paths": [str(tmp_path / "x.gguf")],
                                                                "dest": str(dest)}, expect=400)["error"]
    assert "choose" in client.post("/v1/setup/move", {"paths": [], "dest": str(dest)}, expect=400)["error"]

    out = client.post("/v1/setup/move", {"paths": [str(model), str(pic)], "dest": str(dest)})
    assert out["move"]["state"] in ("running", "done") and out["reloads_tier"] is True
    wait_for(lambda: client.get("/v1/setup/models")["move"]["state"] != "running")
    mv = client.get("/v1/setup/models")["move"]
    assert mv["state"] == "done" and mv["error"] == "", mv
    assert mv["done_bytes"] == mv["total_bytes"] == 5000
    assert len(mv["result"]["moved"]) == 2 and model.exists() and (dest / "writing.gguf").exists()
    assert reloaded == ["local-light"]  # Setup's tier changed and is the current one
    assert norm(load_tier("local-light", tiers).text.launch[2]) == norm(dest / "writing.gguf")
    assert norm(ModelStore(app.var_dir).list()[0].files["model"]) == norm(dest / "pic.safetensors")
    inst.shutdown()


# ── fix round 1 ───────────────────────────────────────────────────────────────────────────────


class StubbornDownloader(FakeDownloader):
    """Ignores cancel until `release` is set: a pause that is still winding down."""

    def __call__(self, url, dest, **kw):
        if "LFM3-Audio" in url and not self.release.is_set():
            self.blocked.set()
            self.release.wait(10)
        return super().__call__(url, dest, **kw)


def _hand_file(app, tmp_path):
    """A hand-configured image model in its own folder (not Setup's)."""
    own = tmp_path / "hand"
    own.mkdir(exist_ok=True)
    pic = own / "pic.safetensors"
    pic.write_bytes(b"I" * 100)
    app.store.save(ImageModel(id="", name="Hand", family="sdxl", files={"model": str(pic)}, settings={}, loras=[]))
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    return pic, dest


def _accept(inst):
    for lic in inst.status()["licences"]:
        inst.acknowledge(lic["key"])


def test_move_refused_while_a_pause_winds_down(app_parts, client, tmp_path):
    app, _ = app_parts
    dl = StubbornDownloader()
    app.installer = inst = make(tmp_path, dl)
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: mover.DRIVE_FIXED)
    pic, dest = _hand_file(app, tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    _accept(inst)
    inst.start()
    assert dl.blocked.wait(5)
    inst.pause()
    assert inst.state() == "paused" and inst.busy()  # the worker is still in the download
    assert "downloading or finishing" in client.post("/v1/setup/move", {"paths": [str(pic)], "dest": str(dest)},
                                                     expect=409)["error"]
    dl.release.set()
    wait_for(lambda: not inst.busy())
    out = client.post("/v1/setup/move", {"paths": [str(pic)], "dest": str(dest)})  # paused and stopped: fine
    assert out["reloads_tier"] is False
    app.setup_api.job.join(5)
    inst.shutdown()


def test_move_refused_while_the_tier_is_written(app_parts, client, tmp_path):
    app, _ = app_parts
    gate, inside = threading.Event(), threading.Event()

    def probe(port):
        inside.set()
        gate.wait(10)
        return True

    app.installer = inst = make(tmp_path, probe=probe)
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: mover.DRIVE_FIXED)
    pic, dest = _hand_file(app, tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    _accept(inst)
    inst.start()
    assert inside.wait(10)
    assert inst.state() == "done" and inst.busy()
    assert client.post("/v1/setup/move", {"paths": [str(pic)], "dest": str(dest)}, expect=409)["error"]
    gate.set()
    wait_for(lambda: inst.state() == "ready")
    inst.shutdown()


def test_start_refused_while_a_move_runs_and_move_cancel(app_parts, client, tmp_path, monkeypatch):
    app, _ = app_parts
    app.installer = inst = make(tmp_path)
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: mover.DRIVE_FIXED)
    pic, dest = _hand_file(app, tmp_path)
    inst.plan("light", {}, tmp_path / "kit")
    _accept(inst)
    started = threading.Event()
    real_copy = mover._copy

    def slow_copy(src, part, cancel, step):
        part.write_bytes(b"half")
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        return real_copy(src, part, cancel, step)  # sees the cancel and stops

    monkeypatch.setattr(mover, "_copy", slow_copy)
    client.post("/v1/setup/move", {"paths": [str(pic)], "dest": str(dest)})
    assert started.wait(5)
    go = {"kit": "light", "root": str(tmp_path / "kit"), "choices": {}}
    assert "move is running" in client.post("/v1/setup/start", go, expect=409)["error"]
    assert "move is running" in client.post("/v1/setup/resume", {}, expect=409)["error"]
    assert "already running" in client.post("/v1/setup/move", {"paths": [str(pic)], "dest": str(dest)},
                                            expect=409)["error"]
    out = client.post("/v1/setup/move/cancel", {})
    assert out["move"]["state"] == "cancelled" and out["move"]["result"]["moved"] == []
    assert not list(dest.iterdir())  # the .part is removed
    assert norm(ModelStore(app.var_dir).list()[0].files["model"]) == norm(pic)
    assert client.post("/v1/setup/start", go)["state"] == "running"
    wait_for(lambda: inst.state() in ("ready", "failed"))
    inst.shutdown()


def test_setup_files_are_not_movable_through_the_api(app_parts, client, tmp_path):
    app, _ = app_parts
    app.installer = inst = make(tmp_path)
    app.setup_api = SetupApi(app, drives=lambda: [], drive_type=lambda p: mover.DRIVE_FIXED)
    root = tmp_path / "kit"
    inst.plan("light", {}, root)  # the pending plan's root counts, before anything was started
    model = root / "models" / "x.safetensors"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x" * 10)
    app.store.save(ImageModel(id="", name="Kit", family="sdxl", files={"model": str(model)}, settings={}, loras=[]))
    dest = tmp_path / "dest"
    dest.mkdir()
    row = client.get("/v1/setup/models")["models"][0]
    assert row["setup"] is True and row["movable"] is False and "installed by Setup" in row["why"]
    err = client.post("/v1/setup/move", {"paths": [str(model)], "dest": str(dest)}, expect=400)["error"]
    assert "installed by Setup" in err and not list(dest.iterdir())
    inst.shutdown()


def test_check_root_normalises_and_refuses_device_paths(setup_app, client, tmp_path):
    win = Path(tmp_path.anchor) / "Windows" / ".."
    assert "not the drive itself" in client.post("/v1/setup/plan", {"kit": "light", "root": str(win)},
                                                 expect=400)["error"]
    for probe in (r"\\?" + "\\" + str(tmp_path / "kit"), r"\\localhost\share\kit"):
        assert "local drive" in client.post("/v1/setup/plan", {"kit": "light", "root": probe}, expect=400)["error"]
    plan = client.post("/v1/setup/plan", {"kit": "light", "root": str(tmp_path / "x" / ".." / "kit")})
    assert norm(_state(client)["root"]) == norm(tmp_path / "kit") and plan["items"]
    # a folder holding the Primer folder (its parent, say) is refused too
    assert "holds the Primer folder" in client.post("/v1/setup/plan", {"kit": "light", "root": str(REPO_ROOT.parent)},
                                                   expect=400)["error"]


class SlowZims(NewerZim):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.inflight = 0
        self.max_inflight = 0
        self.lock = threading.Lock()

    def zims(self, lang="eng", name_prefix="wikipedia_en_"):
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        time.sleep(0.2)
        try:
            return super().zims(lang, name_prefix)
        finally:
            with self.lock:
                self.inflight -= 1


def test_updates_errors_cached_briefly_and_requests_coalesce(app_parts, tmp_path):
    app, _ = app_parts
    app.installer = inst = _ready_installer(tmp_path)
    clock = Clock()
    api = SetupApi(app, clock=clock)
    inst.catalogues = cat = SlowZims(broken_assets=True)
    results = []
    ts = [threading.Thread(target=lambda: results.append(api.updates())) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 4 and all(r == results[0] for r in results)
    assert sum(1 for c in cat.calls if c[0] == "zims") == 1 and cat.max_inflight == 1  # one in flight
    assert any("error" in r for r in results[0])
    n = len(cat.calls)
    clock.t += 30
    api.updates()
    assert len(cat.calls) == n  # still cached
    clock.t += UPDATES_ERROR_TTL_S
    api.updates()
    assert len(cat.calls) > n  # an error row is checked again after a minute, not ten
    inst.shutdown()


def test_updates_zim_without_a_date(app_parts, tmp_path):
    app, _ = app_parts
    app.installer = inst = _ready_installer(tmp_path)
    for it in inst._plan.items:
        if it.kind == "zim":
            it.origin.pop("date", None)
    inst.catalogues = NewerZim()
    rows = SetupApi(app).updates()
    z = next(r for r in rows if r["item"] == "zim_top")
    assert z["installed"] == "unknown" and z["latest"] is None and z["error"] == "installed date unknown"
    inst.shutdown()


def test_conflicts_are_typed():
    from primer.setup.api import _setup_error
    from primer.setup.installer import SetupConflict, SetupError
    assert _setup_error(SetupConflict("anything at all")).status == 409
    assert _setup_error(SetupError("Setup is already running, says the text")).status == 400
    assert _setup_error(mover.MoveConflict("x")).status == 409
