import base64
import hashlib
import socket
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from primer.config import ImagesConfig
from primer.engines import EngineError
from primer.gpu import GpuGate
from primer.images import service as service_mod
from primer.images.families import FAMILIES
from primer.images.models import ImageModel, ModelStore
from primer.images.prompting import image_prompt_messages
from primer.images.sdclient import SdClient
from primer.images.service import ImageService, decode_png
from primer.servers import ManagedServer
from tests.fake_sd_server import PNG_1X1, PREVIEW_B64, TINY_WEBM
from tests.fakes import FakeEngine

FAKE_SD = str(Path(__file__).resolve().parent / "fake_sd_server.py")
WAIT = 20.0
PAGE_MD = ("# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones over castle walls. "
           "It was a siege engine.")


def _until(pred, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recorder:
    def __init__(self):
        self.log: list[str] = []

    def pause(self):
        self.log.append("pause")

    def resume(self):
        self.log.append("resume")


class FakeSdLauncher:
    """make_server for tests: records each spec the service builds, then runs the fake
    sd-server script with the same flags in place of the configured executable."""

    def __init__(self, steps_delay: float = 0.0, extra: tuple[str, ...] = ()):
        self.steps_delay = steps_delay
        self.extra = extra  # more fake_sd_server.py args, e.g. ("--fail-polls", "3")
        self.specs = []
        self.servers: list[ManagedServer] = []

    def __call__(self, spec):
        self.specs.append(spec)
        launch = (sys.executable, FAKE_SD, *spec.launch[1:], "--steps-delay", str(self.steps_delay), *self.extra)
        srv = ManagedServer("image", replace(spec, launch=launch), health=SdClient(spec.base_url, timeout=2).health)
        self.servers.append(srv)
        return srv

    @property
    def launches(self):
        return [s.launch for s in self.specs]


def _model_file(tmp_path, name):
    p = tmp_path / "models" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"weights")
    return str(p)


@pytest.fixture
def env(tmp_path, g):
    """Everything an ImageService needs; build it with env.make(...)."""

    class Env:
        pass

    e = Env()
    e.tmp, e.graph = tmp_path, g
    e.store = ModelStore(tmp_path / "var")
    e.sdxl = e.store.save(ImageModel("sdxl-one", "SDXL One", "sdxl",
                                     {"model": _model_file(tmp_path, "one.safetensors")}, {"steps": 2}))
    e.store.set_default(e.sdxl.id)
    e.node = g.node_for("trebuchet", "concept", title="Trebuchet")
    g.add_version(e.node.id, PAGE_MD, "storyteller", "normal")
    e.rec = Recorder()
    e.gate = GpuGate("auto", e.rec.pause, e.rec.resume)
    e.images = ImagesConfig(sd_server="sd-server", port=_free_port(), ready_timeout_s=20.0)
    e.media_dir = tmp_path / "var" / "media"
    e.services = []

    def make(fast=None, images="default", steps_delay=0.0, make_server="fake", fake_args=()):
        e.launcher = FakeSdLauncher(steps_delay, fake_args)
        svc = ImageService(g, e.store, e.images if images == "default" else images,
                           fast or FakeEngine(["castle, siege engine, ink plate"]), e.gate, e.media_dir,
                           **({"make_server": e.launcher} if make_server == "fake" else {}))
        e.services.append(svc)
        return svc

    e.make = make
    yield e
    for svc in e.services:
        svc.shutdown()
    for srv in getattr(e, "launcher", FakeSdLauncher()).servers:
        assert srv.proc is None or srv.proc.poll() is not None, "fake sd-server left running"


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(service_mod, "POLL_S", 0.05)


def _status(svc, mid):
    return svc.job(mid)["status"]


def _wait_status(svc, mid, *statuses, timeout=WAIT):
    assert _until(lambda: _status(svc, mid) in statuses, timeout), svc.job(mid)
    return svc.job(mid)


def test_create_without_sd_server_fails_with_reason(env):
    svc = env.make(images=None)
    assert svc.available() == (False, "sd-server not configured in this tier")
    with pytest.raises(ValueError, match="sd-server not configured in this tier"):
        svc.create(env.node.id, "trebuchet", "a trebuchet")
    assert env.graph.media_for_node(env.node.id) == []
    assert svc.server_status()["state"] == "unavailable"


def test_create_without_any_model_fails_with_reason(env):
    env.store.delete(env.sdxl.id)
    svc = env.make()
    assert svc.available() == (False, "no image model set up")
    with pytest.raises(ValueError, match="no image model set up"):
        svc.create(env.node.id, "trebuchet", "a trebuchet")


def test_create_with_invalid_model_fails_with_reason(env):
    Path(env.sdxl.files["model"]).unlink()
    svc = env.make()
    ok, reason = svc.available()
    assert not ok and "file not found" in reason
    with pytest.raises(ValueError, match="file not found"):
        svc.create(env.node.id, "trebuchet", "a trebuchet")
    with pytest.raises(ValueError, match="unknown image model"):
        svc.create(env.node.id, "trebuchet", "a trebuchet", model_id="nope")
    assert env.graph.media_for_node(env.node.id) == []


def test_create_with_invalid_overrides_fails_with_reason(env):
    svc = env.make()
    with pytest.raises(ValueError, match="steps must be a number"):
        svc.create(env.node.id, "trebuchet", "a trebuchet", overrides={"steps": "many"})
    with pytest.raises(ValueError, match="overrides"):
        svc.create(env.node.id, "trebuchet", "a trebuchet", overrides=["steps"])
    assert env.graph.media_for_node(env.node.id) == []


def test_create_returns_immediately_in_prompting_then_queues(env):
    release = threading.Event()

    def slow_reply(messages):
        release.wait(WAIT)
        return "a trebuchet, ink"

    svc = env.make(fast=FakeEngine(slow_reply))
    with env.gate.page():  # a page holds the GPU, so the job waits in "queued"
        t0 = time.monotonic()
        m = svc.create(env.node.id, "trebuchet", "a trebuchet")
        assert time.monotonic() - t0 < 1.0
        assert m.status == "prompting"
        assert svc.job(m.id)["status"] == "prompting"
        release.set()
        _wait_status(svc, m.id, "queued")
        assert svc.job(m.id)["prompt"] == "a trebuchet, ink"
    _wait_status(svc, m.id, "done")


def test_prompt_written_before_queue_and_uses_family_style(env):
    qwen = env.store.save(ImageModel("qwen-one", "Qwen", "qwen-image-2.1", {
        "diffusion": _model_file(env.tmp, "q.gguf"), "llm": _model_file(env.tmp, "llm.gguf"),
        "vae": _model_file(env.tmp, "vae.safetensors")}, {"steps": 2}))
    seen = []

    def reply(messages):
        seen.append(messages)
        return "Prompt: \"A trebuchet hurling a stone at a castle, engraved plate.\""

    svc = env.make(fast=FakeEngine(reply))
    with env.gate.page():
        m = svc.create(env.node.id, "trebuchet", "a trebuchet")
        m2 = svc.create(env.node.id, "trebuchet", "a trebuchet", model_id=qwen.id)
        job = _wait_status(svc, m.id, "queued")
        job2 = _wait_status(svc, m2.id, "queued")
    # the prompt is in place by the time the job is queued
    assert job["prompt"] == "A trebuchet hurling a stone at a castle, engraved plate."
    styles = {msgs[0]["content"] for msgs in seen}
    assert styles == {image_prompt_messages(FAMILIES["sdxl"], "x", None, None)[0]["content"],
                      image_prompt_messages(FAMILIES["qwen-image-2.1"], "x", None, None)[0]["content"]}
    user = seen[0][1]["content"]
    assert "Subject: a trebuchet" in user and "Page: Trebuchet" in user and "counterweight" in user
    assert job2["model_id"] == qwen.id
    _wait_status(svc, m.id, "done")
    _wait_status(svc, m2.id, "done")


def test_prompt_writing_error_falls_back_to_subject(env):
    def boom(messages):
        raise EngineError("fast model down")

    svc = env.make(fast=FakeEngine(boom))
    m = svc.create(env.node.id, "trebuchet", "a trebuchet")
    job = _wait_status(svc, m.id, "done")
    assert job["prompt"] == "a trebuchet"


def test_job_runs_to_done_and_writes_png_in_media_dir(env):
    svc = env.make()
    m = svc.create(env.node.id, "trebuchet", "a trebuchet")
    job = _wait_status(svc, m.id, "done", "failed")
    assert job["status"] == "done", job
    name = hashlib.sha256(PNG_1X1).hexdigest() + ".png"
    assert job["file"] == name and job["error"] is None
    assert (env.media_dir / name).read_bytes() == PNG_1X1
    assert "progress" not in job and "preview_b64" not in job
    # the same bytes again reuse the name; same model, so no relaunch
    m2 = svc.create(env.node.id, "trebuchet", "a trebuchet", prompt="again")
    assert _wait_status(svc, m2.id, "done")["file"] == name
    assert sorted(p.name for p in env.media_dir.iterdir()) == [name]  # no temp files left
    assert len(env.launcher.launches) == 1
    st = svc.server_status()
    assert st["state"] == "ready" and st["model_id"] == env.sdxl.id
    assert st["model_name"] == env.sdxl.name
    assert env.graph.media_for_node(env.node.id)[0].file == name


def test_progress_reported_while_generating(env):
    svc = env.make(steps_delay=0.25)
    m = svc.create(env.node.id, "trebuchet", "a trebuchet", overrides={"steps": 6})
    assert _until(lambda: svc.job(m.id).get("progress", {}).get("step", 0) >= 1), svc.job(m.id)
    job = svc.job(m.id)
    assert job["status"] == "generating"
    assert job["progress"]["total"] == 6
    assert job["preview_b64"] == PREVIEW_B64
    steps = {job["progress"]["step"]}
    while _status(svc, m.id) == "generating":
        steps.add(svc.job(m.id).get("progress", {}).get("step"))
        time.sleep(0.05)
    assert len(steps - {None}) >= 2  # it advanced
    assert _wait_status(svc, m.id, "done")["settings"]["steps"] == 6


def test_cancel_running_job(env):
    svc = env.make(steps_delay=0.3)
    m = svc.create(env.node.id, "trebuchet", "a trebuchet", overrides={"steps": 30})
    _wait_status(svc, m.id, "generating")
    assert svc.cancel(m.id) is True
    job = _wait_status(svc, m.id, "cancelled", "failed", "done", timeout=10)
    assert job["status"] == "cancelled" and job["file"] is None
    assert svc.cancel(m.id) is False
    assert not env.media_dir.exists() or list(env.media_dir.iterdir()) == []
    # the server stays up for the next job
    m2 = svc.create(env.node.id, "trebuchet", "next", prompt="next", overrides={"steps": 1})
    _wait_status(svc, m2.id, "done")
    assert len(env.launcher.launches) == 1


def test_cancel_queued_job_never_reaches_gpu(env):
    svc = env.make()
    with env.gate.page():
        m = svc.create(env.node.id, "trebuchet", "a trebuchet", prompt="queued one")
        _wait_status(svc, m.id, "queued")
        assert svc.cancel(m.id) is True
        assert svc.job(m.id)["status"] == "cancelled"  # at once
    time.sleep(0.5)
    assert svc.job(m.id)["status"] == "cancelled"
    assert env.launcher.launches == [] and "pause" not in env.rec.log
    assert env.gate.state()["holder"] is None and env.gate.state()["waiting_images"] == 0


def test_cancel_while_prompting_is_not_queued(env):
    release = threading.Event()
    svc = env.make(fast=FakeEngine(lambda msgs: release.wait(WAIT) and "ink"))
    m = svc.create(env.node.id, "trebuchet", "a trebuchet")
    assert svc.cancel(m.id) is True
    assert svc.job(m.id)["status"] == "prompting"
    release.set()
    _wait_status(svc, m.id, "cancelled")
    time.sleep(0.3)
    assert svc.job(m.id)["status"] == "cancelled"
    assert env.launcher.launches == []


def test_server_dies_midjob_fails_and_next_job_relaunches(env):
    svc = env.make(steps_delay=0.3)
    m = svc.create(env.node.id, "trebuchet", "a trebuchet", overrides={"steps": 30})
    _wait_status(svc, m.id, "generating")
    first = env.launcher.servers[0]
    first.proc.kill()
    job = _wait_status(svc, m.id, "failed", "done", "cancelled")
    assert job["status"] == "failed" and "sd-server stopped while generating" in job["error"]
    assert env.gate.state()["holder"] is None
    m2 = svc.create(env.node.id, "trebuchet", "again", prompt="again", overrides={"steps": 1})
    assert _wait_status(svc, m2.id, "done", "failed")["status"] == "done"
    assert len(env.launcher.launches) == 2


def test_switching_model_restarts_server_with_new_flags(env):
    other = env.store.save(ImageModel("sdxl-two", "SDXL Two", "sdxl",
                                      {"model": _model_file(env.tmp, "two.safetensors")},
                                      {"steps": 1, "offload": True}))
    svc = env.make()
    m1 = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    _wait_status(svc, m1.id, "done")
    m2 = svc.create(env.node.id, "trebuchet", "b", prompt="b", model_id=other.id)
    assert _wait_status(svc, m2.id, "done", "failed")["status"] == "done"
    first, second = env.launcher.launches
    port = str(env.images.port)
    assert first[:5] == ("sd-server", "--listen-ip", "127.0.0.1", "--listen-port", port)
    assert first[5:] == ("-m", env.sdxl.files["model"], "--vae-tiling")
    assert second[5:] == ("-m", other.files["model"], "--offload-to-cpu", "--vae-tiling")
    spec = env.launcher.specs[1]
    assert spec.kind == "sdserver" and spec.gpu and spec.managed
    assert spec.base_url == f"http://127.0.0.1:{port}" and spec.ready_timeout_s == 20.0
    assert env.launcher.servers[0].proc is None  # the first server was stopped
    assert svc.server_status()["model_id"] == other.id
    assert svc.server_status()["model_name"] == other.name


def test_queue_drain_releases_gate(env):
    svc = env.make()
    with env.gate.page():
        a = svc.create(env.node.id, "trebuchet", "a", prompt="a")
        b = svc.create(env.node.id, "trebuchet", "b", prompt="b")
    _wait_status(svc, a.id, "done")
    _wait_status(svc, b.id, "done")
    # consecutive images keep the text paused; the drained queue resumes it once
    assert _until(lambda: env.rec.log == ["pause", "resume"]), env.rec.log
    assert env.gate.state() == {"mode": "auto", "holder": None, "text_paused": False, "waiting_pages": 0,
                                "waiting_images": 0, "error": None, "background": False}


def test_edit_and_run_skips_prompt_writing(env):
    fast = FakeEngine(["should not be used"])
    svc = env.make(fast=fast)
    with env.gate.page():
        m = svc.create(env.node.id, "trebuchet", "a trebuchet", prompt="  my own prompt  ")
        assert m.status == "queued" and m.prompt == "my own prompt"
    job = _wait_status(svc, m.id, "done")
    assert job["prompt"] == "my own prompt" and fast.calls == []


def test_launch_failure_fails_job_and_releases_gate(env):
    env.images = replace(env.images, sd_server=str(env.tmp / "no-such-sd-server.exe"))
    svc = env.make(make_server="default")
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    job = _wait_status(svc, m.id, "failed", "done")
    assert job["status"] == "failed" and "launch command not found" in job["error"]
    assert env.gate.state()["holder"] is None
    assert _until(lambda: env.rec.log == ["pause", "resume"]), env.rec.log
    st = svc.server_status()
    assert st["state"] == "failed" and "launch command not found" in st["reason"]


def test_non_png_result_fails_with_clear_reason(env, monkeypatch):
    real_job = SdClient.job

    def gif_job(self, id):
        j = real_job(self, id)
        if j.get("result"):
            j["result"]["images"][0]["b64_json"] = base64.b64encode(b"GIF89a....").decode()
        return j

    monkeypatch.setattr(SdClient, "job", gif_job)
    svc = env.make()
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    job = _wait_status(svc, m.id, "failed", "done")
    assert job["status"] == "failed" and "not a PNG" in job["error"]
    assert not env.media_dir.exists() or list(env.media_dir.iterdir()) == []


@pytest.mark.parametrize("payload, reason", [
    ({}, "no image"),
    ({"result": None}, "no image"),
    ({"result": {"images": []}}, "no image"),
    ({"result": {"images": [{"index": 0}]}}, "no image"),
    ({"result": {"images": [{"b64_json": ""}]}}, "no image"),
    ({"result": {"images": [{"b64_json": "!!not base64!!"}]}}, "not valid base64"),
    ({"result": {"images": [{"b64_json": base64.b64encode(b"hello").decode()}]}}, "not a PNG"),
    ({"result": {"images": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}]}}, "empty"),
    ({"result": {"images": "nope"}}, "no image"),
])
def test_decode_png_rejects_bad_results(payload, reason):
    with pytest.raises(ValueError, match=reason):
        decode_png(payload)


def test_decode_png_accepts_png():
    assert decode_png({"result": {"images": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]}}) == PNG_1X1


def test_shutdown_stops_server(env):
    release = threading.Event()
    svc = env.make(fast=FakeEngine(lambda msgs: release.wait(WAIT) and "ink"), steps_delay=0.3)
    running = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 30})
    _wait_status(svc, running.id, "generating")
    queued = svc.create(env.node.id, "trebuchet", "b", prompt="b")
    prompting = svc.create(env.node.id, "trebuchet", "c")
    proc = env.launcher.servers[0].proc
    assert proc.poll() is None
    t0 = time.monotonic()
    svc.shutdown()
    assert time.monotonic() - t0 < 10
    assert proc.poll() is not None
    assert svc.server_status()["state"] == "stopped"
    assert svc.job(running.id)["status"] == "cancelled"
    for mid in (queued.id, prompting.id):
        job = svc.job(mid)
        assert job["status"] == "failed" and job["error"] == "Primer shut down"
    release.set()
    time.sleep(0.3)
    assert svc.job(prompting.id)["status"] == "failed"
    with pytest.raises(ValueError, match="shut"):
        svc.create(env.node.id, "trebuchet", "d", prompt="d")
    svc.shutdown()  # idempotent
    assert env.gate.state()["holder"] is None
    # the text servers paused for the cancelled job are resumed
    assert env.gate.state()["text_paused"] is False and env.rec.log[-1] == "resume"


def test_job_of_unknown_id_raises_keyerror(env):
    svc = env.make()
    with pytest.raises(KeyError):
        svc.job("nope")
    assert svc.cancel("nope") is False


# --- fix round 1 ---

def _wait_drained(env):
    assert _until(lambda: env.rec.log[-1:] == ["resume"]), env.rec.log
    assert env.gate.state()["text_paused"] is False and env.gate.state()["holder"] is None


def test_cancel_hung_job_ends_cancelled_within_grace(env, monkeypatch):
    monkeypatch.setattr(service_mod, "CANCEL_GRACE_S", 1.0)
    svc = env.make(steps_delay=1000)  # alive, but never advances or honours a cancel
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 5})
    _wait_status(svc, m.id, "generating")
    proc = env.launcher.servers[0].proc
    assert svc.cancel(m.id) is True
    t0 = time.monotonic()
    job = _wait_status(svc, m.id, "cancelled", "failed", "done", timeout=10)
    assert job["status"] == "cancelled"
    assert time.monotonic() - t0 < 5
    assert _until(lambda: proc.poll() is not None)  # the hung server was stopped
    _wait_drained(env)
    m2 = svc.create(env.node.id, "trebuchet", "b", prompt="b", overrides={"steps": 5})
    _wait_status(svc, m2.id, "generating")  # the next job runs (on a relaunched server)
    assert len(env.launcher.launches) == 2


def test_stalled_job_fails_with_reason(env):
    env.images = replace(env.images, stall_timeout_s=1.5)
    svc = env.make(steps_delay=1000)
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 5})
    _wait_status(svc, m.id, "generating")
    proc = env.launcher.servers[0].proc
    job = _wait_status(svc, m.id, "failed", "done", "cancelled", timeout=10)
    assert job["status"] == "failed"
    assert job["error"] == "sd-server stopped making progress (no update for 1.5s)"
    assert _until(lambda: proc.poll() is not None)
    _wait_drained(env)


def test_slow_steps_are_not_a_stall(env):
    env.images = replace(env.images, stall_timeout_s=1.0)
    svc = env.make(steps_delay=0.4)  # each step is quicker than the stall timeout
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 6})
    assert _wait_status(svc, m.id, "done", "failed")["status"] == "done"


def test_failed_image_job_still_restores_text_servers(env):
    svc = env.make(steps_delay=0.3)
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 30})
    _wait_status(svc, m.id, "generating")
    env.launcher.servers[0].proc.kill()
    assert _wait_status(svc, m.id, "failed", "done")["status"] == "failed"
    _wait_drained(env)
    assert env.rec.log == ["pause", "resume"]


def test_save_failure_fails_without_restarting_server(env, monkeypatch):
    def no_space(*a, **kw):
        raise OSError(28, "No space left on device")

    svc = env.make()
    monkeypatch.setattr(service_mod, "tempfile", types.SimpleNamespace(mkstemp=no_space))
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    job = _wait_status(svc, m.id, "failed", "done")
    assert job["status"] == "failed"
    assert job["error"].startswith("could not save the image:") and "No space" in job["error"]
    proc = env.launcher.servers[0].proc
    assert proc is not None and proc.poll() is None  # sd-server is not blamed
    monkeypatch.undo()
    m2 = svc.create(env.node.id, "trebuchet", "b", prompt="b")
    assert _wait_status(svc, m2.id, "done", "failed")["status"] == "done"
    assert len(env.launcher.launches) == 1


def test_unexpected_error_fails_job_and_stops_server(env, monkeypatch):
    def broken(job):
        raise RuntimeError("surprise")

    monkeypatch.setattr(service_mod, "decode_png", broken)
    svc = env.make()
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    job = _wait_status(svc, m.id, "failed", "done")
    assert job["status"] == "failed" and "internal error" in job["error"]
    proc = env.launcher.servers[0].proc  # stopped just after the row is marked failed
    assert _until(lambda: proc is None or proc.poll() is not None)
    _wait_drained(env)


def test_prompting_job_holds_off_release_idle(env):
    release = threading.Event()
    svc = env.make(fast=FakeEngine(lambda msgs: release.wait(WAIT) and "ink"))
    a = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    b = svc.create(env.node.id, "trebuchet", "b")  # still prompting when a finishes
    _wait_status(svc, a.id, "done")
    time.sleep(0.5)
    assert env.rec.log == ["pause"]  # no resume while another job is on its way
    svc.cancel(b.id)
    release.set()
    _wait_status(svc, b.id, "cancelled")
    _wait_drained(env)
    assert env.rec.log == ["pause", "resume"]


# --- final fix wave ---

def test_transient_poll_errors_do_not_fail_the_job(env):
    svc = env.make(steps_delay=0.1, fake_args=("--fail-polls", "3"))
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 4})
    job = _wait_status(svc, m.id, "done", "failed", "cancelled")
    assert job["status"] == "done", job
    assert len(env.launcher.launches) == 1  # the server was not restarted


def test_poll_errors_past_the_budget_fail_with_reason(env, monkeypatch):
    monkeypatch.setattr(service_mod, "POLL_ERRORS", 3)
    monkeypatch.setattr(service_mod, "POLL_ERROR_S", 0.3)
    svc = env.make(steps_delay=0.1, fake_args=("--fail-polls", "100000"))
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 4})
    job = _wait_status(svc, m.id, "done", "failed", "cancelled")
    assert job["status"] == "failed"
    assert job["error"].startswith("sd-server stopped answering"), job["error"]
    assert "503" in job["error"]
    _wait_drained(env)


def test_create_picks_a_concrete_random_seed(env):
    svc = env.make()
    with env.gate.page():  # keep the jobs from running; only the stored settings matter
        a = svc.create(env.node.id, "trebuchet", "a", prompt="a")
        b = svc.create(env.node.id, "trebuchet", "b", prompt="b", overrides={"seed": -1})
        c = svc.create(env.node.id, "trebuchet", "c", prompt="c", overrides={"seed": 1234})
    seeds = [m.settings["seed"] for m in (a, b)]
    assert all(isinstance(s, int) and 0 <= s < 2**31 - 1 for s in seeds), seeds
    assert seeds[0] != seeds[1]
    assert env.graph.get_media(a.id).settings["seed"] == seeds[0]  # the row records it
    assert c.settings["seed"] == 1234  # a chosen seed is kept
    assert service_mod.to_params("a", a.settings)["seed"] == seeds[0]
    for m in (a, b, c):
        svc.cancel(m.id)


def test_models_without_a_default_say_so(env):
    import json
    raw = json.loads(env.store.path.read_text(encoding="utf-8"))
    raw["default"] = None  # e.g. written by an older Primer
    env.store.path.write_text(json.dumps(raw), encoding="utf-8")
    svc = env.make()
    assert svc.available() == (False, "no default image model (choose one in Models)")


# --- Phase 3: video jobs ---

def _video_model(env, family="wan2.2-ti2v-5b", name="Wan Small", loras=(), **settings):
    fam = FAMILIES[family]
    files = {s.name: _model_file(env.tmp, f"{family}-{s.name}.safetensors") for s in fam.slots}
    return env.store.save(ImageModel(f"vid-{family.replace('.', '')}", name, family, files,
                                     {"steps": 2, "frames": 9, "fps": 8, **settings}, list(loras)))


def _done_image(env, file=None, status="done", kind="image", data=PNG_1X1):
    m = env.graph.add_media(env.node.id, "trebuchet", kind, env.sdxl.id, "an old picture", {})
    if file is None and data is not None:
        env.media_dir.mkdir(parents=True, exist_ok=True)
        file = hashlib.sha256(data).hexdigest() + ".png"
        (env.media_dir / file).write_bytes(data)
    return env.graph.update_media(m.id, status=status, file=file)


def _spy_video(monkeypatch):
    sent = []
    real = SdClient.submit_video

    def spy(self, params):
        sent.append(params)
        return real(self, params)

    monkeypatch.setattr(SdClient, "submit_video", spy)
    return sent


def test_video_job_runs_end_to_end_with_fake(env):
    vid = _video_model(env)
    seen = []
    svc = env.make(fast=FakeEngine(lambda msgs: seen.append(msgs) or "a trebuchet swings, camera pans"))
    assert svc.available("video") == (True, "")
    m = svc.create(env.node.id, "trebuchet", "a trebuchet", kind="video")
    assert m.kind == "video" and m.model_id == vid.id
    job = _wait_status(svc, m.id, "done", "failed", "cancelled")
    assert job["status"] == "done", job
    name = hashlib.sha256(TINY_WEBM).hexdigest() + ".webm"
    assert job["file"] == name and (env.media_dir / name).read_bytes() == TINY_WEBM
    assert job["kind"] == "video" and job["fps"] == 8 and job["frames"] == 9
    assert env.graph.get_media(m.id).settings["result_frames"] == 9
    style = image_prompt_messages(FAMILIES[vid.family], "x", None, None)[0]["content"]
    assert seen[0][0]["content"] == style and "camera movement" in style
    # images are untouched: no fps/frames on their jobs
    img = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    ij = _wait_status(svc, img.id, "done", "failed")
    assert ij["kind"] == "image" and "fps" not in ij and "frames" not in ij


def test_image_then_video_restarts_server_with_new_flags(env, monkeypatch):
    lora_dir = env.tmp / "loras"
    lora_dir.mkdir()
    (lora_dir / "fast.safetensors").write_bytes(b"lora")
    vid = _video_model(env, "wan2.2-t2v-a14b", "Wan Big",
                       loras=[{"file": str(lora_dir / "fast.safetensors"), "strength": 1.0}])
    sent = _spy_video(monkeypatch)
    svc = env.make()
    a = svc.create(env.node.id, "trebuchet", "a", prompt="a")
    _wait_status(svc, a.id, "done")
    v = svc.create(env.node.id, "trebuchet", "v", prompt="v", kind="video", model_id=vid.id)
    assert _wait_status(svc, v.id, "done", "failed")["status"] == "done"
    b = svc.create(env.node.id, "trebuchet", "b", prompt="b")
    assert _wait_status(svc, b.id, "done", "failed")["status"] == "done"
    first, second, third = env.launcher.launches
    assert first[5:] == ("-m", env.sdxl.files["model"], "--vae-tiling")
    assert "--high-noise-diffusion-model" in second and vid.files["high_noise"] in second
    assert second[second.index("--lora-model-dir") + 1] == str(lora_dir)
    assert third == first  # the video model's flags never leak into the image launch
    assert env.launcher.servers[1].proc is None
    assert sent[0]["lora"] == [{"path": "fast.safetensors", "multiplier": 1.0, "is_high_noise": False},
                               {"path": "fast.safetensors", "multiplier": 1.0, "is_high_noise": True}]
    assert sent[0]["output_format"] == "webm" and "init_image" not in sent[0]


def test_lora_folder_change_restarts_server(env):
    d1, d2 = env.tmp / "l1", env.tmp / "l2"
    for d in (d1, d2):
        d.mkdir()
        (d / "x.safetensors").write_bytes(b"lora")
    vid = _video_model(env, loras=[{"file": str(d1 / "x.safetensors")}])
    svc = env.make()
    v = svc.create(env.node.id, "trebuchet", "v", prompt="v", kind="video")
    _wait_status(svc, v.id, "done")
    env.store.save(replace(vid, loras=[{"file": str(d2 / "x.safetensors")}]))
    v2 = svc.create(env.node.id, "trebuchet", "v", prompt="v2", kind="video")
    _wait_status(svc, v2.id, "done")
    assert len(env.launcher.launches) == 2
    assert str(d2) in env.launcher.launches[1]


def test_text_request_to_i2v_only_model_refused(env):
    svc = env.make()
    assert svc.available("video") == (False, "no video model added yet")
    with pytest.raises(ValueError, match="no video model added yet"):
        svc.create(env.node.id, "trebuchet", "a", kind="video")
    i2v = _video_model(env, "wan2.2-i2v-a14b", "Wan Animate")
    # the Video button is off with the same reason a text request is refused with
    assert svc.available("video") == (False, "Wan 2.2 I2V A14B only animates a picture: use Animate on an image")
    with pytest.raises(ValueError) as e:
        svc.create(env.node.id, "trebuchet", "a trebuchet", kind="video")
    assert str(e.value) == "Wan 2.2 I2V A14B only animates a picture: use Animate on an image"
    with pytest.raises(ValueError, match="^Wan Animate is a video model$"):
        svc.create(env.node.id, "trebuchet", "a", model_id=i2v.id)
    with pytest.raises(ValueError, match="^SDXL One is an image model$"):
        svc.create(env.node.id, "trebuchet", "a", kind="video", model_id=env.sdxl.id)
    src = _done_image(env)
    with pytest.raises(ValueError, match="only a video"):
        svc.create(env.node.id, "trebuchet", "a", animate_from=src.id)
    with pytest.raises(ValueError, match="kind"):
        svc.create(env.node.id, "trebuchet", "a", kind="audio")
    assert svc.available() == (True, "")  # the image default is unaffected
    assert [m.id for m in env.graph.media_for_node(env.node.id)] == [src.id]  # nothing was created
    assert env.launcher.launches == []


def test_animate_requires_done_image_in_media_dir(env):
    _video_model(env, "wan2.2-i2v-a14b", "Wan Animate")
    svc = env.make()
    outside = env.tmp / "secret.png"
    outside.write_bytes(PNG_1X1)
    cases = {
        "the picture to animate isn't finished": _done_image(env, status="queued").id,
        "only a picture can be animated": _done_image(env, kind="video").id,
        "the picture to animate is gone": _done_image(env, file="missing.png").id,
        "unknown picture to animate": "nope",
    }
    for bad in ("../secret.png", "..\\secret.png", str(outside), "..", "", "sub/x.png"):
        cases[f"gone: {bad!r}"] = _done_image(env, file=bad).id
    before = len(env.graph.media_for_node(env.node.id))
    for reason, src in cases.items():
        with pytest.raises(ValueError) as e:
            svc.create(env.node.id, "trebuchet", "a", kind="video", animate_from=src)
        want = "the picture to animate is gone" if reason.startswith("gone") else reason
        assert str(e.value) == want, reason
    assert len(env.graph.media_for_node(env.node.id)) == before
    assert env.launcher.launches == []


def test_animate_sends_init_image(env, monkeypatch):
    vid = _video_model(env, "wan2.2-i2v-a14b", "Wan Animate")
    src = _done_image(env)
    sent = _spy_video(monkeypatch)
    seen = []
    svc = env.make(fast=FakeEngine(lambda msgs: seen.append(msgs) or "the stone flies, slow push in"))
    m = svc.create(env.node.id, "trebuchet", "a trebuchet", kind="video", animate_from=src.id)
    assert m.settings["animate_from"] == src.id and m.model_id == vid.id
    job = _wait_status(svc, m.id, "done", "failed")
    assert job["status"] == "done", job
    assert job["settings"]["animate_from"] == src.id
    assert base64.b64decode(sent[0]["init_image"]) == PNG_1X1
    assert "animate_from" not in sent[0] and "result_fps" not in sent[0]
    user = seen[0][1]["content"]
    assert "animates the given picture" in user and "an old picture" in user
    # the source is read when the job runs: gone by then, the job fails with the reason
    with env.gate.page():
        m2 = svc.create(env.node.id, "trebuchet", "x", prompt="x", kind="video", animate_from=src.id)
        _wait_status(svc, m2.id, "queued")
        (env.media_dir / src.file).unlink()
    job2 = _wait_status(svc, m2.id, "failed", "done")
    assert job2["status"] == "failed" and job2["error"] == "the picture to animate is gone"


def test_video_cancel_and_stall_like_images(env):
    _video_model(env)
    svc = env.make(steps_delay=0.3)
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", overrides={"steps": 30})
    _wait_status(svc, m.id, "generating")
    assert _until(lambda: svc.job(m.id).get("progress", {}).get("total") == 30), svc.job(m.id)
    assert svc.cancel(m.id) is True
    job = _wait_status(svc, m.id, "cancelled", "failed", "done", timeout=10)
    assert job["status"] == "cancelled" and job["file"] is None and job["fps"] is None
    assert not env.media_dir.exists() or list(env.media_dir.iterdir()) == []
    svc.shutdown()

    env.images = replace(env.images, stall_timeout_s=1.5)
    svc2 = env.make(steps_delay=1000)
    m2 = svc2.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", overrides={"steps": 5})
    _wait_status(svc2, m2.id, "generating")
    proc = env.launcher.servers[0].proc
    job2 = _wait_status(svc2, m2.id, "failed", "done", "cancelled", timeout=10)
    assert job2["status"] == "failed"
    assert job2["error"] == "sd-server stopped making progress (no update for 1.5s)"
    assert _until(lambda: proc.poll() is not None)
    _wait_drained(env)


def test_video_bad_result_fails_with_reason(env, monkeypatch):
    real_job = SdClient.job

    def gif_job(self, id):
        j = real_job(self, id)
        if j.get("result"):
            j["result"]["mime_type"] = "image/gif"
        return j

    monkeypatch.setattr(SdClient, "job", gif_job)
    _video_model(env)
    svc = env.make()
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video")
    job = _wait_status(svc, m.id, "failed", "done")
    assert job["status"] == "failed" and "unexpected video type" in job["error"]
    assert not env.media_dir.exists() or list(env.media_dir.iterdir()) == []


def test_image_available_ignores_video_models(env):
    env.store.delete(env.sdxl.id)
    _video_model(env)
    svc = env.make()
    assert svc.available() == (False, "no image model set up")
    assert svc.available("video") == (True, "")


# --- fix round 1: Animate picks a model that can animate ---

def test_can_animate_flags():
    from primer.images.families import FAMILIES as F
    assert {f.id for f in F.values() if f.can_animate} == {
        "wan2.2-ti2v-5b", "wan2.2-i2v-a14b", "ltx-2.3-distilled", "ltx-2.3"}
    for f in F.values():
        if f.needs_init:
            assert f.can_animate, f.id
        if f.kind == "image":
            assert not f.can_animate, f.id


def test_animate_with_explicit_model_that_cannot_animate_refused(env):
    t2v = _video_model(env, "wan2.2-t2v-a14b", "Wan Text")
    src = _done_image(env)
    svc = env.make()
    before = len(env.graph.media_for_node(env.node.id))
    with pytest.raises(ValueError) as e:
        svc.create(env.node.id, "trebuchet", "a", kind="video", model_id=t2v.id, animate_from=src.id)
    assert str(e.value) == "Wan Text can't animate a picture: choose a video model that can"
    assert len(env.graph.media_for_node(env.node.id)) == before


def test_animate_without_model_skips_default_that_cannot_animate(env):
    from primer.images.models import animate_model
    t2v = _video_model(env, "wan2.2-t2v-a14b", "Wan Text")  # first video model: the video default
    assert env.store.default_id("video") == t2v.id
    broken = _video_model(env, "ltx-2.3", "LTX Broken")
    Path(broken.files["vae"]).unlink()  # can animate, but doesn't validate: skipped
    ti2v = _video_model(env, "wan2.2-ti2v-5b", "Wan Small")
    assert animate_model(env.store).id == ti2v.id
    src = _done_image(env)
    svc = env.make()
    with env.gate.page():
        m = svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", animate_from=src.id)
        assert m.model_id == ti2v.id
        svc.cancel(m.id)
    # a default that can animate is used as is
    env.store.set_default(ti2v.id)
    assert animate_model(env.store).id == ti2v.id


def test_animate_without_model_falls_back_when_default_does_not_validate(env):
    from primer.images.models import animate_model
    broken = _video_model(env, "ltx-2.3", "LTX Broken")  # first video model: the video default
    assert env.store.default_id("video") == broken.id
    Path(broken.files["vae"]).unlink()  # can animate, but doesn't validate
    ti2v = _video_model(env, "wan2.2-ti2v-5b", "Wan Small")
    assert animate_model(env.store).id == ti2v.id
    src = _done_image(env)
    svc = env.make()
    with env.gate.page():
        m = svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", animate_from=src.id)
        assert m.model_id == ti2v.id
        svc.cancel(m.id)
    # none validates: the default is reported, with its own reason
    Path(ti2v.files["vae"]).unlink()
    assert animate_model(env.store).id == broken.id
    with pytest.raises(ValueError) as e:
        svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", animate_from=src.id)
    assert "vae: file not found" in str(e.value)


def test_animate_without_any_capable_model_refused(env):
    from primer.images.models import animate_model
    _video_model(env, "wan2.2-t2v-a14b", "Wan Text")
    src = _done_image(env)
    svc = env.make()
    assert animate_model(env.store) is None
    with pytest.raises(ValueError) as e:
        svc.create(env.node.id, "trebuchet", "a", kind="video", animate_from=src.id)
    assert str(e.value) == "no video model that can animate a picture has been added"
    assert svc.available("video") == (True, "")  # text-to-video is still fine


def test_animate_refuses_drive_relative_file_name(env):
    _video_model(env, "wan2.2-i2v-a14b", "Wan Animate")
    svc = env.make()
    src = _done_image(env, file="C:x.png")
    with pytest.raises(ValueError, match="^the picture to animate is gone$"):
        svc.create(env.node.id, "trebuchet", "a", kind="video", animate_from=src.id)


def test_again_reuses_animate_from_settings(env):
    """Again/Retry (Task 6): pass the row's settings["animate_from"] back as animate_from."""
    _video_model(env, "wan2.2-i2v-a14b", "Wan Animate")
    src = _done_image(env)
    svc = env.make()
    first = svc.create(env.node.id, "trebuchet", "a", prompt="a", kind="video", animate_from=src.id)
    row = svc.job(first.id)
    assert _wait_status(svc, first.id, "done", "failed")["status"] == "done"
    again = svc.create(env.node.id, row["phrase"], "a", prompt=row["prompt"], kind="video",
                       model_id=row["model_id"], animate_from=row["settings"]["animate_from"])
    job = _wait_status(svc, again.id, "done", "failed")
    assert job["status"] == "done" and job["settings"]["animate_from"] == src.id


def test_two_stage_video_progress_reports_the_stage(env):
    """sd-server counts each Wan 2.2 A14B stage on its own (0/4, 1/2, 2/2, then 1/4 .. 4/4):
    the job says "high" until the low-noise stage starts."""
    _video_model(env, "wan2.2-t2v-a14b", "Wan Big", steps=4, high_noise_steps=2)
    script = "0/4,1/2,2/2,1/4,2/4,3/4,4/4"
    svc = env.make(steps_delay=0.2, fake_args=("--script", script))
    m = svc.create(env.node.id, "trebuchet", "v", prompt="v", kind="video")
    seen = []
    while _status(svc, m.id) not in ("done", "failed", "cancelled"):
        p = svc.job(m.id).get("progress")
        if p and (not seen or seen[-1] != p):
            seen.append((p["step"], p["total"], p.get("stage")))
        time.sleep(0.02)
    assert _status(svc, m.id) == "done"
    assert (2, 2, "high") in seen and (1, 2, "high") in seen
    assert (1, 4, "low") in seen and (3, 4, "low") in seen
    assert not [s for s in seen if s[:2] in ((1, 2), (2, 2)) and s[2] != "high"]
    assert not [s for s in seen if s[0] >= 1 and s[1] == 4 and s[2] != "low"]
    assert "progress" not in svc.job(m.id)  # finished: no live progress


def test_single_stage_progress_has_no_stage(env):
    svc = env.make(steps_delay=0.25)
    m = svc.create(env.node.id, "trebuchet", "a", prompt="a", overrides={"steps": 4})
    assert _until(lambda: svc.job(m.id).get("progress", {}).get("step", 0) >= 1), svc.job(m.id)
    assert "stage" not in svc.job(m.id)["progress"]
    _wait_status(svc, m.id, "done")


@pytest.mark.parametrize("reports,stage", [
    ([(0, 4)], "high"), ([(0, 4), (1, 2)], "high"), ([(0, 4), (1, 2), (2, 2)], "high"),
    ([(0, 4), (1, 2), (2, 2), (1, 4)], "low"), ([(0, 4), (1, 4), (2, 4), (1, 4)], "low"),
    ([(0, 4), (1, 1), (1, 4)], "low")])
def test_next_stage(reports, stage):
    live = {"step": 0, "total": 0, "stage": "high"}
    for step, total in reports:
        if live["stage"] == "high":
            live["stage"] = service_mod.next_stage(live, {"step": step, "total_steps": total})
        live["step"], live["total"] = step, total
    assert live["stage"] == stage


def test_prefix_starts_the_prompt(env):
    svc = env.make(fast=FakeEngine(["a trebuchet at dawn, ink"]))
    with env.gate.page():  # held: the jobs wait in "queued" with their prompts written
        m = svc.create(env.node.id, "Siege", "Nell watches", prefix="a girl in a <red> scarf")
        job = _wait_status(svc, m.id, "queued")
        assert job["prompt"] == "a girl in a red scarf, a trebuchet at dawn, ink"
        given = svc.create(env.node.id, "Siege", "", prompt="a castle", prefix="a girl")
        assert svc.job(given.id)["prompt"] == "a girl, a castle"
        plain = svc.create(env.node.id, "Siege", "Nell watches")
        assert _wait_status(svc, plain.id, "queued")["prompt"] == "a trebuchet at dawn, ink"
    for mid in (m.id, given.id, plain.id):
        _wait_status(svc, mid, "done", "failed")


def test_prefix_is_told_to_the_prompt_writer_and_short_for_tags(env):
    seen = []

    def reply(messages):
        seen.append(messages)
        return "a castle, ink"

    svc = env.make(fast=FakeEngine(reply))
    look = "a girl of twelve with a red scarf " + "and a very long description " * 12
    with env.gate.page():
        m = svc.create(env.node.id, "Siege", "Nell watches", prefix=look)
        job = _wait_status(svc, m.id, "queued")
    head = job["prompt"][:-len(", a castle, ink")]
    assert job["prompt"].endswith(", a castle, ink") and len(head) <= 150  # sdxl takes tags
    assert look.startswith(head) and not head.endswith(" ")
    user = seen[0][1]["content"]
    assert "a girl of twelve with a red scarf" in user and "look" in user.lower()
    _wait_status(svc, m.id, "done", "failed")


def test_scene_pictures_are_storybook_and_the_look_is_not_repeated(env):
    """Live L3-L5: a chapter's scene is written from the scene alone (no page title or excerpt)
    in the storybook style; the writer is told not to repeat the look, a repeat is dropped, and a
    label is stripped. Page pictures still get the page's excerpt and the plate style."""
    seen = []
    look = "Wren, female: a girl of sixteen with a satchel of tools"

    def reply(messages):
        seen.append(messages)
        return "Prompt: a girl of sixteen with a satchel of tools, climbing a clock tower at dusk, ink"

    svc = env.make(fast=FakeEngine(reply))
    with env.gate.page():
        m = svc.create(env.node.id, "The Tower", "Wren climbs the clock tower.", prefix=look, scene=True)
        job = _wait_status(svc, m.id, "queued")
        page = svc.create(env.node.id, "Siege", "a siege")
        _wait_status(svc, page.id, "queued")
    assert job["prompt"] == f"{look}, climbing a clock tower at dusk, ink"
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert "storybook illustration" in system and "encyclopedia plate" not in system
    assert "Excerpt" not in user and "Page:" not in user and "Wren climbs the clock tower." in user
    assert "do not repeat it" in user and look in user
    page_system, page_user = seen[1][0]["content"], seen[1][1]["content"]
    assert "encyclopedia plate" in page_system and "Page: Trebuchet" in page_user
    assert "Excerpt from the page" in page_user
    for mid in (m.id, page.id):
        _wait_status(svc, mid, "done", "failed")
