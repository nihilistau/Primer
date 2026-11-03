"""HTTP routes for image jobs, image models, typed-request intent and media files (Task 9)."""
import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from primer.config import EngineSpec, ImagesConfig, Tier
from primer.graph import Graph
from primer.hardware import Hardware
from primer.images import service as service_mod
from primer.images.families import FAMILIES
from primer.images.models import ImageModel, ModelStore
from primer.images.service import NO_ANIMATE_MODEL
from primer.pages import PageService
from primer.server import App, TextPause, make_server
from primer.servers import ManagedServer, ServerManager
from tests.conftest import Client
from tests.fake_sd_server import PNG_1X1, TINY_WEBM
from tests.fakes import FakeEngine
from tests.test_images_scan import gguf, safet
from tests.test_images_service import FakeSdLauncher
from tests.test_images_service import _free_port as free_port
from tests.test_images_service import _until as until
from tests.test_server import _raw

PAGE_MD = "# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones over castle walls."
SD_EXE = "sd-server-from-tier"


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(service_mod, "POLL_S", 0.05)


def _model_file(tmp_path, name):
    p = tmp_path / "models" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"weights")
    return str(p)


class Env:
    pass


def _fast_reply(messages):
    """The image prompt for image-prompt requests, a highlight list for everything else."""
    return "castle, ink plate" if "Stable Diffusion prompt" in json.dumps(messages) else '["counterweight"]'


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.tmp = tmp_path
    e.var = tmp_path / "var"
    e.graph = Graph(e.var / "primer.sqlite")
    e.store = ModelStore(e.var)
    e.model = e.store.save(ImageModel("sdxl-one", "SDXL One", "sdxl",
                                      {"model": _model_file(tmp_path, "one.safetensors")}, {"steps": 2}))
    e.store.set_default(e.model.id)
    spec = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")
    e.tier = Tier(name="test", text=spec, fast=spec,
                  images=ImagesConfig(sd_server=SD_EXE, port=free_port(), ready_timeout_s=20.0))
    servers = ServerManager(e.tier)
    servers._servers = {"text": ManagedServer("text", spec, health=lambda: True),
                        "fast": ManagedServer("fast", spec, health=lambda: True)}
    e.launcher = FakeSdLauncher()
    pages = PageService(e.graph, FakeEngine([PAGE_MD]), FakeEngine(_fast_reply))
    ui = tmp_path / "ui"
    ui.mkdir()
    e.app = App(tier=e.tier, servers=servers, pages=pages, graph=e.graph,
                hardware=Hardware(None, 0.0, 16.0), var_dir=e.var, ui_dir=ui,
                tiers_dir=tmp_path / "tiers", make_sd_server=e.launcher)
    e.server = make_server(e.app, port=0)
    threading.Thread(target=e.server.serve_forever, daemon=True).start()
    e.client = Client(e.server.server_address[1])
    yield e
    e.server.shutdown()
    e.server.server_close()
    e.app.images.shutdown()
    e.app.pages.shutdown(drain=False)
    for srv in e.launcher.servers:
        assert srv.proc is None or srv.proc.poll() is not None, "fake sd-server left running"


def _wait_job(env, mid, *statuses, timeout=20.0):
    assert until(lambda: env.client.get(f"/v1/images/{mid}")["status"] in statuses, timeout), \
        env.client.get(f"/v1/images/{mid}")
    return env.client.get(f"/v1/images/{mid}")


def _page(env):
    return env.client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]


def test_intent_returns_video(env):
    c = env.client
    assert c.post("/v1/intent", {"text": "animate a trebuchet firing"}) == {"image": None, "video": "a trebuchet firing", "map": None, "story": None}
    assert c.post("/v1/intent", {"text": "make a video of a moat"})["image"] is None


def test_intent_route(env):
    c = env.client
    assert c.post("/v1/intent", {"text": "draw a picture of a trebuchet"}) == {"image": "a trebuchet", "video": None, "map": None, "story": None}
    assert c.post("/v1/intent", {"text": "trebuchets"}) == {"image": None, "video": None, "map": None, "story": None}
    assert c.post("/v1/intent", {"text": 7}, expect=400)["error"]
    assert c.post("/v1/intent", {}, expect=400)["error"]


def test_create_image_job_then_poll_to_done_and_fetch_media(env):
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "phrase": "counterweight", "subject": "a trebuchet"})
    assert m["node_id"] == nid and m["status"] in ("prompting", "queued") and m["model_id"] == "sdxl-one"
    job = _wait_job(env, m["id"], "done", "failed", "cancelled")
    assert job["status"] == "done", job
    assert job["prompt"] == "castle, ink plate"
    status, body = _raw(env.client.port, "GET", f"/v1/media/{job['file']}")
    assert status == 200 and body == PNG_1X1
    conn = __import__("http.client").client.HTTPConnection("127.0.0.1", env.client.port, timeout=10)
    conn.request("GET", f"/v1/media/{job['file']}")
    r = conn.getresponse()
    assert r.getheader("Content-Type") == "image/png"
    r.read()
    conn.close()
    # cancelling a finished job does nothing
    assert env.client.post(f"/v1/images/{m['id']}/cancel", {}) == {"cancelled": False}
    env.client.get("/v1/images/nope", expect=404)
    env.client.post("/v1/images/nope/cancel", {}, expect=404)


def test_cancel_route(env):
    nid = _page(env)
    with env.app.gate.page():  # holds the GPU so the job waits in "queued"
        m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
        assert m["status"] == "queued"
        assert env.client.post(f"/v1/images/{m['id']}/cancel", {}) == {"cancelled": True}
    assert env.client.get(f"/v1/images/{m['id']}")["status"] == "cancelled"
    assert env.launcher.specs == []


def test_create_for_concept_without_page(env):
    m = env.client.post("/v1/images", {"concept": "Windmill", "subject": "a windmill", "prompt": "a windmill"})
    node = env.graph.get_node(m["node_id"])
    assert node is not None and node.title == "Windmill"
    assert env.graph.latest_version(node.id) is None  # no page was written
    assert env.app.pages.text.calls == []
    assert _wait_job(env, m["id"], "done", "failed")["status"] == "done"
    # neither node_id nor concept
    assert env.client.post("/v1/images", {"subject": "a windmill"}, expect=400)["error"]
    assert env.client.post("/v1/images", {"concept": "   ", "subject": "a windmill"}, expect=400)["error"]
    assert env.client.post("/v1/images", {"node_id": "nope", "subject": "x"}, expect=404)["error"]
    assert env.client.post("/v1/images", {"concept": "Windmill"}, expect=400)["error"]  # no subject
    assert env.client.post("/v1/images", {"concept": "Windmill", "subject": "x",
                                          "overrides": {"steps": "many"}}, expect=400)["error"]
    assert env.client.post("/v1/images", {"concept": "Windmill", "subject": "x",
                                          "overrides": ["steps"]}, expect=400)["error"]


def test_create_unavailable_returns_409_with_reason(env):
    nid = _page(env)
    env.store.delete("sdxl-one")
    r = env.client.post("/v1/images", {"node_id": nid, "subject": "x"}, expect=409)
    assert r["error"] == "no image model set up"
    r = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "model_id": "gone"}, expect=409)
    assert "unknown image model" in r["error"]
    # a concept node is not created when the model is unavailable
    env.client.post("/v1/images", {"concept": "Windmill", "subject": "x"}, expect=409)
    assert [n["title"] for n in env.graph.snapshot()["nodes"]] == ["Trebuchet"]
    broken = env.store.save(ImageModel("broken", "Broken", "sdxl", {"model": str(env.tmp / "missing.gguf")}))
    r = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "model_id": broken.id}, expect=409)
    assert "file not found" in r["error"]
    env.app.images.images = None  # a tier without [images]
    r = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "model_id": broken.id}, expect=409)
    assert r["error"] == "sd-server not configured in this tier"
    assert env.graph.media_for_node(nid) == []


def test_media_route_rejects_bad_names_and_traversal(env):
    media = env.var / "media"
    media.mkdir(parents=True)
    name = "a" * 64 + ".png"
    (media / name).write_bytes(PNG_1X1)
    (env.var / "secret.png").write_bytes(b"secret")
    (env.var / ("b" * 64 + ".png")).write_bytes(b"outside")
    p = env.client.port
    assert _raw(p, "GET", f"/v1/media/{name}") == (200, PNG_1X1)
    for bad in ["../secret.png", "..%2Fsecret.png", "%2e%2e/secret.png", "..%5c" + "b" * 64 + ".png",
                "A" * 64 + ".png", "a" * 63 + ".png", "a" * 64 + ".PNG", "a" * 64 + ".png.txt",
                "a" * 64 + ".png%00", "c" * 64 + ".png", "", "x/" + name, "%2e%2e%2f" + "b" * 64 + ".png"]:
        status, body = _raw(p, "GET", f"/v1/media/{bad}")
        assert status == 404, bad
        assert body not in (b"secret", b"outside")
    # a directory with a valid name is not a file
    (media / ("d" * 64 + ".png")).mkdir()
    assert _raw(p, "GET", "/v1/media/" + "d" * 64 + ".png")[0] == 404
    # read-only, so no X-Primer header is needed; a foreign Origin is still refused
    assert _raw(p, "GET", f"/v1/media/{name}", headers={"Origin": "https://evil.example"})[0] == 403


def test_image_models_crud_and_default(env, tmp_path):
    c = env.client
    listing = c.get("/v1/image-models")
    assert listing["default"] == "sdxl-one" and listing["available"] == {"ok": True, "reason": ""}
    assert [m["id"] for m in listing["models"]] == ["sdxl-one"] and listing["models"][0]["problems"] == []
    fams = listing["families"]
    assert {"sdxl", "qwen-image-2.1", "moss-image"} <= set(fams)  # plus the video families
    assert fams["sdxl"]["label"] == "SDXL" and fams["sdxl"]["defaults"]["steps"] == 20
    assert [s["name"] for s in fams["sdxl"]["slots"]] == ["model", "vae"]
    assert fams["sdxl"]["slots"][0]["required"] is True

    path = _model_file(tmp_path, "two.gguf")
    saved = c.post("/v1/image-models", {"model": {"name": "Two", "family": "sdxl", "files": {"model": path},
                                                  "settings": {"steps": 500, "bogus": 1}}})
    assert saved["name"] == "Two" and saved["files"] == {"model": path}
    assert saved["settings"] == {"steps": 150}
    mid = saved["id"]
    again = c.post("/v1/image-models", {"model": {**saved, "name": "Two again"}})
    assert again["id"] == mid
    listing = c.get("/v1/image-models")
    assert {m["id"]: m["name"] for m in listing["models"]}[mid] == "Two again"

    missing = c.post("/v1/image-models", {"model": {"name": "Gone", "family": "sdxl",
                                                    "files": {"model": str(tmp_path / "nope.gguf")}}})
    problems = {m["id"]: m["problems"] for m in c.get("/v1/image-models")["models"]}
    assert "file not found" in problems[missing["id"]][0]

    assert c.post("/v1/image-models/default", {"id": mid}) == {"ok": True}
    assert c.get("/v1/image-models")["default"] == mid
    assert c.post("/v1/image-models/delete", {"id": mid}) == {"ok": True}
    assert c.get("/v1/image-models")["default"] == "sdxl-one"  # the first remaining model
    assert c.post("/v1/image-models/delete", {"id": mid}, expect=404)["error"]
    assert c.post("/v1/image-models/default", {"id": "nope"}, expect=404)["error"]
    assert c.post("/v1/image-models/default", {}, expect=400)["error"]
    # invalid models are 400 with the reason
    for bad in [{"name": "", "family": "sdxl"}, {"name": "x", "family": "nope"},
                {"name": "x", "family": "sdxl", "files": {"model": "relative.gguf"}},
                {"name": "x", "family": "sdxl", "settings": {"steps": "many"}},
                {"name": "x", "family": "sdxl", "settings": ["steps"]},
                {"name": "x", "family": "sdxl", "files": ["a"]}]:
        assert c.post("/v1/image-models", {"model": bad}, expect=400)["error"], bad
    assert c.post("/v1/image-models", {"model": "x"}, expect=400)["error"]
    assert c.post("/v1/image-models", {}, expect=400)["error"]


def test_image_models_corrupt_store_is_a_json_error(env):
    env.store.path.write_text("{not json")
    for method, path, body in [("GET", "/v1/image-models", None),
                               ("POST", "/v1/image-models", {"model": {"name": "x", "family": "sdxl"}}),
                               ("POST", "/v1/image-models/delete", {"id": "sdxl-one"}),
                               ("POST", "/v1/image-models/default", {"id": "sdxl-one"})]:
        r = env.client._request(method, path, body, expect=500)
        assert "not a valid model store" in r["error"], path
    s = env.client.get("/v1/status")
    assert s["image_available"]["ok"] is False and "not a valid model store" in s["image_available"]["reason"]


def test_scan_route_validates_folder(env, tmp_path, monkeypatch):
    c = env.client
    f = tmp_path / "file.txt"
    f.write_text("x")
    for bad in ["relative/dir", str(tmp_path / "missing"), str(f), "", 7, None]:
        assert c.post("/v1/image-models/scan", {"folder": bad}, expect=400)["error"], bad
    folder = tmp_path / "scan"
    safet(folder / "xl.safetensors", keys=["conditioner.embedders.0.x"])
    gguf(folder / "other.gguf", "llama")
    r = c.post("/v1/image-models/scan", {"folder": str(folder)})
    assert [(p["family"], p["name"]) for p in r["proposals"]] == [("sdxl", "xl")]
    # at most MAX_SCAN_FILES files are inspected
    monkeypatch.setattr("primer.server.MAX_SCAN_FILES", 1)
    big = tmp_path / "big"
    safet(big / "a" / "a.safetensors", keys=["conditioner.embedders.0.x"])
    safet(big / "b" / "b.safetensors", keys=["conditioner.embedders.0.x"])
    assert len(c.post("/v1/image-models/scan", {"folder": str(big)})["proposals"]) == 1


def test_page_includes_media(env):
    nid = _page(env)
    assert env.client.get(f"/v1/pages/{nid}")["media"] == []
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
    _wait_job(env, m["id"], "done")
    media = env.client.get(f"/v1/pages/{nid}")["media"]
    assert [x["id"] for x in media] == [m["id"]] and media[0]["status"] == "done" and media[0]["file"]


def test_status_includes_images_and_gpu(env):
    s = env.client.get("/v1/status")
    assert s["images"] == {"state": "stopped", "model_id": None, "model_name": None, "reason": ""}
    assert s["image_available"] == {"ok": True, "reason": ""}
    assert s["gpu"]["mode"] == "auto" and s["gpu"]["holder"] is None
    env.app.images.images = None
    s = env.client.get("/v1/status")
    assert s["images"]["state"] == "unavailable"
    assert s["image_available"] == {"ok": False, "reason": "sd-server not configured in this tier"}


def test_waiting_event_passes_through(env):
    events = []
    with env.app.gate.image():
        t = threading.Thread(target=lambda: events.extend(
            env.client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})))
        t.start()
        assert until(lambda: env.app.gate.state()["waiting_pages"] == 1, 10)
    t.join(10)
    kinds = [k for k, _ in events]
    assert kinds[:2] == ["node", "waiting"] and "done" in kinds
    assert dict(events)["waiting"]["reason"]


def test_image_routes_refuse_foreign_origin_and_text_plain(env):
    p = env.client.port
    nid = _page(env)
    posts = [("/v1/intent", {"text": "draw a cat"}),
             ("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"}),
             ("/v1/images/x/cancel", {}),
             ("/v1/image-models", {"model": {"name": "Evil", "family": "sdxl"}}),
             ("/v1/image-models/delete", {"id": "sdxl-one"}),
             ("/v1/image-models/default", {"id": "sdxl-one"}),
             ("/v1/image-models/scan", {"folder": str(env.tmp)})]
    for path, body in posts:
        data = json.dumps(body).encode()
        assert _raw(p, "POST", path, data, {"Content-Type": "text/plain"})[0] == 415, path
        assert _raw(p, "POST", path, data, {"Content-Type": "application/json",
                                            "Origin": "https://evil.example"})[0] == 403, path
        assert _raw(p, "POST", path, data, {"Content-Type": "application/json",
                                            "Sec-Fetch-Site": "cross-site"})[0] == 403, path
    for path in ("/v1/image-models", "/v1/images/x", "/v1/media/" + "a" * 64 + ".png"):
        assert _raw(p, "GET", path, headers={"Origin": "https://evil.example"})[0] == 403, path
        assert _raw(p, "GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403, path
    assert env.graph.media_for_node(nid) == []
    assert [m.id for m in env.store.list()] == ["sdxl-one"] and env.store.default_id() == "sdxl-one"


def test_no_route_accepts_executable_or_flags(env, tmp_path):
    c = env.client
    evil = {"launch": ["calc.exe"], "sd_server": "calc.exe", "args": ["--evil"], "flags": ["--evil"]}
    saved = c.post("/v1/image-models", {"model": {"id": "sneaky", "name": "Sneaky", "family": "sdxl",
                                                  "files": {"model": _model_file(tmp_path, "s.gguf"),
                                                            "launch": "calc.exe"},
                                                  "settings": {"steps": 2, **evil}, **evil}, **evil})
    assert set(saved) == {"id", "name", "family", "files", "settings", "loras"}
    assert saved["loras"] == []
    assert saved["files"] == {"model": str(tmp_path / "models" / "s.gguf")}
    assert saved["settings"] == {"steps": 2}
    raw = env.store.path.read_text()
    assert "calc.exe" not in raw and "--evil" not in raw
    nid = _page(env)
    m = c.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle", "model_id": "sneaky",
                              "overrides": {"steps": 2, **evil}, **evil})
    assert _wait_job(env, m["id"], "done", "failed")["status"] == "done"
    launch = env.launcher.launches[0]
    assert launch[0] == SD_EXE and "calc.exe" not in launch and "--evil" not in launch
    assert launch[1:] == ("--listen-ip", "127.0.0.1", "--listen-port", str(env.tier.images.port),
                          "-m", saved["files"]["model"], "--vae-tiling")


def test_reload_tier_rebuilds_gate_and_images(env):
    (env.app.tiers_dir).mkdir()
    (env.app.tiers_dir / "alt.toml").write_text(
        'name = "alt"\nfast = "text"\ngpu_mode = "manual"\n[text]\nkind = "openai"\n'
        'base_url = "http://127.0.0.1:1"\n')
    old_images, old_gate = env.app.images, env.app.gate
    env.client.post("/v1/tier", {"name": "alt"})
    try:
        assert env.app.images is not old_images and old_images._closed
        assert env.app.gate is not old_gate and env.app.gate.mode == "manual"
        assert env.app.pages.gate is env.app.gate and env.app.images.gate is env.app.gate
        assert env.app.images.store is env.app.store and env.app.images.graph is env.graph
        assert env.app.text_pause.release_gpu == env.app.images.release_gpu
        assert env.client.get("/v1/status")["image_available"]["reason"] == "sd-server not configured in this tier"
    finally:
        env.app.servers.stop_all()


class _FakeServer:
    def __init__(self, gpu, state="ready", pause=()):
        self.spec = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", gpu=gpu, pause=pause)
        self.state, self.log = state, []

    def pause(self):
        self.log.append("pause")

    def status(self):
        return {"state": self.state}

    def stop(self):
        self.log.append("stop")
        self.state = "stopped"

    def start(self, wait=True):
        self.log.append(f"start(wait={wait})")
        self.state = "ready"


class _Servers:
    def __init__(self, *servers):
        self.servers = servers

    def gpu_servers(self):
        return [s for s in self.servers if s.spec.gpu]


def test_text_pause_stops_and_restarts_only_running_gpu_servers():
    gpu, cpu, stopped = _FakeServer(True), _FakeServer(False), _FakeServer(True, "stopped")
    tp = TextPause(_Servers(gpu, cpu, stopped))
    tp.pause()
    assert gpu.log == ["stop"] and cpu.log == [] and stopped.log == []
    tp.resume()
    assert gpu.log == ["stop", "start(wait=False)"] and stopped.log == []
    tp.resume()  # nothing paused any more
    assert gpu.log == ["stop", "start(wait=False)"]
    tp.pause()
    tp.retire()  # the servers are about to be stopped for good: don't restart them first
    tp.resume()
    assert gpu.log == ["stop", "start(wait=False)", "stop"]


def test_text_pause_unloads_with_a_pause_command_and_resume_restarts_nothing():
    unload = ("lms", "unload", "--all")
    lms, ext = _FakeServer(True, pause=unload), _FakeServer(True, "external", pause=unload)
    plain_ext = _FakeServer(True, "external")  # no pause command: an external server is left alone
    tp = TextPause(_Servers(lms, ext, plain_ext))
    released = []
    tp.release_gpu = lambda: released.append(True)
    tp.pause()
    assert lms.log == ["pause"] and ext.log == ["pause"] and plain_ext.log == []
    tp.resume()  # still serving: the model loads again on the next request
    assert lms.log == ["pause"] and ext.log == ["pause"]
    assert released == [True]  # sd-server still leaves the GPU first
    lms.state = "stopped"  # e.g. stopped from the Engines window meanwhile
    tp.pause()  # a stopped server isn't paused, so not restarted either
    tp.resume()
    assert lms.log == ["pause"]


def test_server_manager_gpu_servers():
    a = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", gpu=True)
    b = EngineSpec(kind="openai", base_url="http://127.0.0.1:2")
    assert [s.role for s in ServerManager(Tier("t", a, b)).gpu_servers()] == ["text"]
    assert [s.role for s in ServerManager(Tier("t", a, a)).gpu_servers()] == ["text"]
    assert ServerManager(Tier("t", b, b)).gpu_servers() == []


@pytest.mark.parametrize("folder", [r"\\.\PhysicalDrive0", "\\\\.\\pipe\\", r"\\?\UNC\server\share",
                                    r"\\server\share", "//server/share", r"\\?\C:\Windows"])
def test_scan_rejects_network_and_device_paths(env, folder, caplog):
    r = env.client.post("/v1/image-models/scan", {"folder": folder}, expect=400)
    assert r["error"] == "network and device paths aren't allowed"
    assert "Traceback" not in caplog.text


def test_scan_os_errors_are_400_not_logged(env, tmp_path, monkeypatch, caplog):
    def boom(*a, **kw):
        raise OSError("drive went away")

    monkeypatch.setattr("primer.server.scan_folder_capped", boom)
    r = env.client.post("/v1/image-models/scan", {"folder": str(tmp_path)}, expect=400)
    assert "drive went away" in r["error"]
    assert "failed" not in caplog.text


def test_scan_reports_truncated(env, tmp_path, monkeypatch):
    big = tmp_path / "big"
    for name in ("a", "b"):
        safet(big / name / f"{name}.safetensors", keys=["conditioner.embedders.0.x"])
    assert env.client.post("/v1/image-models/scan", {"folder": str(big)})["truncated"] is False
    monkeypatch.setattr("primer.server.MAX_SCAN_FILES", 1)
    assert env.client.post("/v1/image-models/scan", {"folder": str(big)})["truncated"] is True


def test_model_name_capped_at_120(env):
    ok = env.client.post("/v1/image-models", {"model": {"name": "n" * 120, "family": "sdxl"}})
    assert ok["name"] == "n" * 120
    r = env.client.post("/v1/image-models", {"model": {"name": "n" * 121, "family": "sdxl"}}, expect=400)
    assert "120" in r["error"]


def test_concept_409_after_shutdown_leaves_no_new_node(env):
    env.app.images.shutdown()
    r = env.client.post("/v1/images", {"concept": "Windmill", "subject": "a windmill"}, expect=409)
    assert "shut down" in r["error"]
    assert env.graph.snapshot()["nodes"] == []


def _proc_server(role, ready_after):
    """A real gpu=True ManagedServer running a sleeping python; healthy ready_after["s"] seconds
    after each launch."""
    spec = EngineSpec(kind="openai", base_url=f"http://127.0.0.1:1/{role}", managed=True,
                      launch=(sys.executable, "-c", "import time; time.sleep(120)"), gpu=True,
                      ready_timeout_s=60)
    launched = {}

    def health():
        p = srv.proc
        if p is None or p.poll() is not None:
            return False
        t = launched.setdefault(p.pid, time.monotonic())
        return time.monotonic() - t >= ready_after["s"]

    srv = ManagedServer(role, spec, health=health)
    return srv


def test_tier_switch_during_text_resume_is_quick_and_orphans_nothing(env):
    ready_after = {"s": 0.2}
    old = {"text": _proc_server("text", ready_after), "fast": _proc_server("fast", ready_after)}
    env.app.servers._servers = old
    resumer = None
    try:
        env.app.servers.start_all(wait=True)
        assert [s.status()["state"] for s in old.values()] == ["ready", "ready"]
        with env.app.gate.image():  # an image job pauses both gpu text servers
            pass
        assert all(s.proc is None for s in old.values())
        ready_after["s"] = 60  # the text model now takes a long time to load
        resumer = threading.Thread(target=env.app.gate.release_idle)  # the image queue drained
        resumer.start()
        assert until(lambda: old["text"].status()["state"] == "starting", 10)
        env.app.tiers_dir.mkdir()
        (env.app.tiers_dir / "alt.toml").write_text(
            'name = "alt"\nfast = "text"\n[text]\nkind = "openai"\nbase_url = "http://127.0.0.1:1"\n')
        t0 = time.monotonic()
        env.app.reload_tier("alt")
        assert time.monotonic() - t0 < 2
        resumer.join(5)
        assert not resumer.is_alive()
        time.sleep(0.5)
        for s in old.values():  # nothing of the old tier is left running
            assert s.proc is None or s.proc.poll() is not None, s.role
        assert env.app.tier.name == "alt"
    finally:
        for s in old.values():
            s.stop()
        env.app.servers.stop_all()
        if resumer is not None:
            resumer.join(10)


class _SlowServer(_FakeServer):
    """Loading takes until it is stopped."""

    def __init__(self, gpu=True):
        super().__init__(gpu)
        self.stopped = threading.Event()

    def start(self, wait=True):
        self.log.append(f"start(wait={wait})")
        self.state = "starting"
        self.stopped.clear()
        if wait:
            self.stopped.wait(10)

    def stop(self):
        super().stop()
        self.stopped.set()


def test_retire_stops_a_resume_in_flight_and_starts_nothing_after():
    text, fast = _SlowServer(), _SlowServer()
    tp = TextPause(_Servers(text, fast))
    tp.pause()
    t = threading.Thread(target=tp.resume)
    t.start()
    assert until(lambda: text.state == "starting", 5)
    t0 = time.monotonic()
    tp.retire()
    at_retire = {s: len(s.log) for s in (text, fast)}
    t.join(2)
    assert not t.is_alive() and time.monotonic() - t0 < 2
    for s in (text, fast):  # loads in flight were stopped, and nothing started after retire
        assert s.state == "stopped" and s.log[-1] == "stop"
        assert not any(e.startswith("start") for e in s.log[at_retire[s]:])
    tp.resume()  # a later resume starts nothing either
    assert all(s.state == "stopped" for s in (text, fast))


def test_auto_mode_stops_sd_server_before_text_resumes_and_next_job_relaunches(env):
    order = []

    class Text(_FakeServer):
        def start(self, wait=True):
            sd = env.launcher.servers[-1]
            order.append(("start text", sd.proc is None or sd.proc.poll() is not None))
            super().start(wait)

    text = Text(True)
    env.app.text_pause._servers = _Servers(text)
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
    assert _wait_job(env, m["id"], "done")["status"] == "done"
    # the queue drained: sd-server gives the GPU back, then the text server is started again
    assert until(lambda: order, 10)
    assert order == [("start text", True)]  # sd-server had exited before the text started
    assert env.app.images.server_status()["state"] == "stopped"
    assert env.app.gate.state()["text_paused"] is False
    m2 = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a tower"})
    assert _wait_job(env, m2["id"], "done")["status"] == "done"
    assert len(env.launcher.specs) == 2  # the next job relaunched sd-server


def test_manual_mode_keeps_sd_server_resident(env):
    from primer.gpu import GpuGate

    gate = GpuGate("manual", env.app.text_pause.pause, env.app.text_pause.resume)
    env.app.gate = env.app.images.gate = gate
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
    assert _wait_job(env, m["id"], "done")["status"] == "done"
    time.sleep(0.5)
    sd = env.launcher.servers[-1]
    assert sd.proc is not None and sd.proc.poll() is None
    assert env.app.images.server_status()["state"] == "ready"
    m2 = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a tower"})
    assert _wait_job(env, m2["id"], "done")["status"] == "done"
    assert len(env.launcher.specs) == 1  # reused, not relaunched


def test_release_gpu_is_a_noop_while_a_job_runs(env):
    env.launcher.steps_delay = 0.2
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle",
                                       "overrides": {"steps": 10}})
    _wait_job(env, m["id"], "generating")
    env.app.images.release_gpu()
    sd = env.launcher.servers[-1]
    assert sd.proc is not None and sd.proc.poll() is None  # the running job keeps its server
    assert _wait_job(env, m["id"], "done", "failed")["status"] == "done"


def test_auto_mode_without_gpu_text_servers_keeps_sd_server(env):
    assert env.app.gate.mode == "auto" and env.app.servers.gpu_servers() == []
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
    assert _wait_job(env, m["id"], "done")["status"] == "done"
    assert until(lambda: env.app.gate.state()["text_paused"] is False, 10)  # the queue drained
    sd = env.launcher.servers[-1]
    assert sd.proc is not None and sd.proc.poll() is None  # nothing to give the GPU to
    m2 = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a tower"})
    assert _wait_job(env, m2["id"], "done")["status"] == "done"
    assert len(env.launcher.specs) == 1


# --- video (Phase 3) ---

def _video_model(env, family="wan2.2-ti2v-5b", mid=None, loras=(), **settings):
    files = {s.name: _model_file(env.tmp, f"{family}-{s.name}.safetensors") for s in FAMILIES[family].slots}
    return env.store.save(ImageModel(mid or f"vid-{family.replace('.', '')}", f"Video {family}", family, files,
                                     {"steps": 2, "frames": 9, "fps": 8, **settings}, list(loras)))


def _get(port, path, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def test_video_job_through_api_and_webm_served(env):
    vid = _video_model(env)
    nid = _page(env)
    m = env.client.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a trebuchet swings",
                                       "kind": "video"})
    assert m["kind"] == "video" and m["model_id"] == vid.id
    job = _wait_job(env, m["id"], "done", "failed", "cancelled")
    assert job["status"] == "done", job
    assert job["kind"] == "video" and job["fps"] == 8 and job["frames"] == 9 and job["file"].endswith(".webm")
    status, headers, body = _get(env.client.port, f"/v1/media/{job['file']}", {})
    assert status == 200 and body == TINY_WEBM
    assert headers["Content-Type"] == "video/webm" and headers["Accept-Ranges"] == "bytes"


def test_media_webm_names_and_range_requests(env):
    media = env.var / "media"
    media.mkdir(parents=True)
    data = bytes(range(100))
    name = "e" * 64 + ".webm"
    (media / name).write_bytes(data)
    (media / ("a" * 64 + ".png")).write_bytes(PNG_1X1)
    (env.var / ("b" * 64 + ".webm")).write_bytes(b"outside")
    p, path = env.client.port, f"/v1/media/{name}"
    for rng, first, last in [("bytes=0-9", 0, 9), ("bytes=90-", 90, 99), ("bytes=-5", 95, 99),
                             ("bytes=95-200", 95, 99), ("bytes=0-0", 0, 0), ("bytes=-500", 0, 99)]:
        status, h, body = _get(p, path, {"Range": rng})
        assert status == 206, rng
        assert body == data[first:last + 1] and h["Content-Range"] == f"bytes {first}-{last}/100", rng
        assert h["Content-Type"] == "video/webm" and h["Content-Length"] == str(last - first + 1)
    for rng in ["bytes=100-", "bytes=5-2", "bytes=0-1,4-5", "items=0-1", "bytes=-0", "bytes=-", "bytes=a-b", ""]:
        status, h, body = _get(p, path, {"Range": rng})
        assert status == 416 and body == b"" and h["Content-Range"] == "bytes */100", rng
    # an image ignores Range
    status, h, body = _get(p, "/v1/media/" + "a" * 64 + ".png", {"Range": "bytes=0-1"})
    assert status == 200 and body == PNG_1X1 and h["Content-Type"] == "image/png"
    for bad in ["E" * 64 + ".webm", "e" * 64 + ".WEBM", "e" * 64 + ".mp4", "e" * 64 + ".webm.png",
                "..%2F" + "b" * 64 + ".webm", "%2e%2e%5c" + "b" * 64 + ".webm", "b" * 64 + ".webm"]:
        assert _raw(p, "GET", f"/v1/media/{bad}")[0] == 404, bad
    assert _raw(p, "GET", path, headers={"Origin": "https://evil.example"})[0] == 403
    assert _raw(p, "GET", path, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _raw(p, "GET", path, headers={"Host": "evil.example"})[0] == 403


def test_create_video_request_errors(env):
    c, nid = env.client, _page(env)
    base = {"node_id": nid, "subject": "x", "prompt": "a moat"}
    for kind in ["gif", 7, None, ""]:
        assert "kind must be one of" in c.post("/v1/images", {**base, "kind": kind}, expect=400)["error"], kind
    assert c.post("/v1/images", {**base, "animate_from": "m1"}, expect=400)["error"] == "animate_from needs kind video"
    assert c.post("/v1/images", {**base, "kind": "image", "animate_from": "m1"}, expect=400)["error"] == \
        "animate_from needs kind video"
    for bad in [7, "", ["m1"]]:
        c.post("/v1/images", {**base, "kind": "video", "animate_from": bad}, expect=400)
    # no video model yet: unavailable with the reason, like images
    assert c.post("/v1/images", {**base, "kind": "video"}, expect=409)["error"] == "no video model added yet"
    assert c.post("/v1/images", {**base, "kind": "video", "animate_from": "m1"}, expect=409)["error"] == \
        NO_ANIMATE_MODEL
    # the wrong kind of model for the request
    assert "is an image model" in c.post("/v1/images", {**base, "kind": "video", "model_id": "sdxl-one"},
                                         expect=400)["error"]
    i2v = _video_model(env, "wan2.2-i2v-a14b")
    assert "is a video model" in c.post("/v1/images", {**base, "model_id": i2v.id}, expect=400)["error"]
    # an image-to-video-only model refuses a text request
    # (the default: unavailable, as the Video button says; named: a bad request)
    i2v_only = "Wan 2.2 I2V A14B only animates a picture: use Animate on an image"
    assert c.post("/v1/images", {**base, "kind": "video"}, expect=409)["error"] == i2v_only
    assert c.post("/v1/images", {**base, "kind": "video", "model_id": i2v.id}, expect=400)["error"] == i2v_only
    # Animate: the picture must be a finished image of Primer's own
    r = c.post("/v1/images", {**base, "kind": "video", "animate_from": "nope"}, expect=400)
    assert r["error"] == "unknown picture to animate"
    with env.app.gate.page():  # holds the GPU so the image stays queued
        pending = c.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
        r = c.post("/v1/images", {**base, "kind": "video", "animate_from": pending["id"]}, expect=400)
        assert r["error"] == "the picture to animate isn't finished"
        c.post(f"/v1/images/{pending['id']}/cancel", {})
    # a model that can't animate, named for an Animate job
    t2v = _video_model(env, "wan2.2-t2v-a14b")
    r = c.post("/v1/images", {**base, "kind": "video", "animate_from": pending["id"], "model_id": t2v.id},
               expect=400)
    assert "can't animate a picture" in r["error"]
    assert [m.id for m in env.graph.media_for_node(nid)] == [pending["id"]]  # nothing else was queued


def test_animate_through_api(env):
    c, nid = env.client, _page(env)
    _video_model(env, "wan2.2-t2v-a14b")  # the video default, which can't animate
    vid = _video_model(env)
    img = c.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "a castle"})
    assert _wait_job(env, img["id"], "done")["status"] == "done"
    m = c.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "the castle, camera pans",
                              "kind": "video", "animate_from": img["id"]})
    assert m["model_id"] == vid.id and m["settings"]["animate_from"] == img["id"]
    job = _wait_job(env, m["id"], "done", "failed")
    assert job["status"] == "done", job
    assert job["settings"]["animate_from"] == img["id"]  # what a video Again sends back
    # a video can't be animated
    r = c.post("/v1/images", {"node_id": nid, "subject": "x", "prompt": "p", "kind": "video",
                              "animate_from": m["id"]}, expect=400)
    assert r["error"] == "only a picture can be animated"


def test_image_models_lists_video_families_defaults_and_availability(env, tmp_path):
    c = env.client
    listing = c.get("/v1/image-models")
    fams = listing["families"]
    sdxl, ti2v, i2v = fams["sdxl"], fams["wan2.2-ti2v-5b"], fams["wan2.2-i2v-a14b"]
    assert sdxl["kind"] == "image" and sdxl["needs_init"] is False and sdxl["frame_step"] == 0
    assert sdxl["can_animate"] is False and sdxl["fast_defaults"] == {}
    assert "frames" not in sdxl["settings"] and {"width", "steps", "seed", "negative"} <= set(sdxl["settings"])
    assert ti2v["kind"] == "video" and ti2v["can_animate"] is True and ti2v["needs_init"] is False
    assert {"frames", "fps"} <= set(ti2v["settings"]) and "high_noise_steps" not in ti2v["settings"]
    assert i2v["needs_init"] is True and i2v["frame_step"] == 4 and i2v["fast_defaults"]["steps"] == 4
    assert {"high_noise_steps", "high_noise_cfg", "moe_boundary"} <= set(i2v["settings"])
    assert fams["ltx-2.3"]["frame_step"] == 8 and fams["ltx-2.3"]["defaults"]["fps"] == 24
    assert listing["video_default"] is None
    assert listing["video_available"] == {"ok": False, "reason": "no video model added yet"}
    assert listing["animate_available"] == {"ok": False, "model_id": None, "reason": NO_ANIMATE_MODEL}
    assert listing["models"][0]["kind"] == "image" and listing["models"][0]["loras"] == []

    lora_dir = tmp_path / "loras"
    lora_dir.mkdir()
    (lora_dir / "fast.safetensors").write_bytes(b"lora")
    files = {s: _model_file(tmp_path, f"w-{s}.safetensors") for s in ("diffusion", "high_noise", "t5xxl", "vae")}
    loras = [{"file": str(lora_dir / "fast.safetensors"), "strength": 0.8, "stage": "high"}]
    saved = c.post("/v1/image-models", {"model": {"name": "Wan I2V", "family": "wan2.2-i2v-a14b",
                                                  "files": files, "loras": loras,
                                                  "settings": {"frames": 30, "high_noise_steps": 2}}})
    assert saved["loras"] == loras and saved["settings"] == {"frames": 29, "high_noise_steps": 2}
    listing = c.get("/v1/image-models")
    entry = {m["id"]: m for m in listing["models"]}[saved["id"]]
    assert entry["kind"] == "video" and entry["loras"] == loras and entry["problems"] == []
    assert listing["video_default"] == saved["id"] and listing["default"] == "sdxl-one"
    # an image-to-video default can't make a video from text: the Video button says why
    i2v_only = {"ok": False, "reason": "Wan 2.2 I2V A14B only animates a picture: use Animate on an image"}
    assert listing["video_available"] == i2v_only and c.get("/v1/status")["video_available"] == i2v_only
    assert listing["animate_available"] == {"ok": True, "reason": "", "model_id": saved["id"]}
    # re-saving through the API keeps the LoRAs it is sent
    again = c.post("/v1/image-models", {"model": {**saved, "name": "Wan I2V again"}})
    assert again["loras"] == loras and env.store.get(saved["id"]).loras == loras
    # the default route sets the default of the model's own kind
    ti2v_model = _video_model(env)
    assert c.post("/v1/image-models/default", {"id": ti2v_model.id}) == {"ok": True}
    listing = c.get("/v1/image-models")
    assert listing["video_default"] == ti2v_model.id and listing["default"] == "sdxl-one"
    s = c.get("/v1/status")
    assert s["video_available"] == {"ok": True, "reason": ""}
    assert s["animate_available"] == {"ok": True, "reason": "", "model_id": ti2v_model.id}
    # a missing file is the reason
    Path(ti2v_model.files["vae"]).unlink()
    s = c.get("/v1/status")
    assert s["video_available"]["ok"] is False and "file not found" in s["video_available"]["reason"]
    # Animate falls back to the usable I2V model; with none usable, the default's reason
    assert s["animate_available"] == {"ok": True, "reason": "", "model_id": saved["id"]}
    Path(files["vae"]).unlink()
    s = c.get("/v1/status")
    assert s["animate_available"]["ok"] is False and s["animate_available"]["model_id"] == ti2v_model.id
    assert "file not found" in s["animate_available"]["reason"]


def test_status_video_availability_without_images_or_with_corrupt_store(env):
    real = env.app.images.images
    env.app.images.images = None
    s = env.client.get("/v1/status")
    assert s["video_available"] == {"ok": False, "reason": "sd-server not configured in this tier"}
    assert s["animate_available"] == {"ok": False, "reason": "sd-server not configured in this tier",
                                      "model_id": None}
    env.app.images.images = real
    env.store.path.write_text("{not json")
    s = env.client.get("/v1/status")
    assert "not a valid model store" in s["video_available"]["reason"]
    assert "not a valid model store" in s["animate_available"]["reason"]


def test_save_model_validates_loras(env, tmp_path):
    c = env.client
    good = str(tmp_path / "loras" / "a.safetensors")
    base = {"name": "Wan", "family": "wan2.2-ti2v-5b"}
    for bad in ["x", 7, {"file": good}, [1], ["x"], [{"file": "relative.safetensors"}],
                [{"file": good, "strength": "strong"}], [{"file": good, "strength": True}],
                [{"file": good, "stage": "sideways"}], [{"file": good, "stage": "high"}],  # one-stage family
                [{"file": None}]]:
        assert c.post("/v1/image-models", {"model": {**base, "loras": bad}}, expect=400)["error"], bad
    saved = c.post("/v1/image-models", {"model": {**base, "loras": [{"file": good, "strength": 5}]}})
    assert saved["loras"] == [{"file": good, "strength": 2.0, "stage": "both"}]  # clamped, stage defaulted
    assert c.post("/v1/image-models", {"model": {**base, "loras": None}})["loras"] == []
    # a picture model takes no LoRAs
    pic = {"name": "Pic", "family": "sdxl", "files": {"model": _model_file(tmp_path, "pic.safetensors")}}
    err = c.post("/v1/image-models", {"model": {**pic, "loras": [{"file": good}]}}, expect=400)["error"]
    assert err == "LoRAs are only used by video models"
    assert c.post("/v1/image-models", {"model": {**pic, "loras": []}})["loras"] == []
