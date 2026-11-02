import base64
import time

import pytest

from primer.images.sdclient import SdClient, SdError, to_params
from tests.fake_sd_server import serve_fake_sd


@pytest.fixture
def sd():
    server = serve_fake_sd(0)
    yield server
    server.kill()


def _wait(client, jid, status, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = client.job(jid)
        if j["status"] == status:
            return j
        time.sleep(0.01)
    raise AssertionError(f"job never reached {status}: {client.job(jid)}")


def test_health_and_status(sd):
    c = SdClient(sd.url)
    assert c.health() is True
    assert c.status()["state"] == "ready"


def test_unreachable_health_false():
    assert SdClient("http://127.0.0.1:9", timeout=0.5).health() is False


def test_submit_poll_complete_returns_png_b64(sd):
    c = SdClient(sd.url)
    jid = c.submit(to_params("a cat", {"width": 512, "height": 512}))
    job = _wait(c, jid, "completed")
    assert job["result"]["output_format"] == "png"
    raw = base64.b64decode(job["result"]["images"][0]["b64_json"])
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert sd.jobs[jid]["params"]["prompt"] == "a cat"


def test_progress_advances():
    server = serve_fake_sd(0, steps_delay=0.05)
    try:
        c = SdClient(server.url)
        jid = c.submit(to_params("x", {"steps": 6}))
        seen = set()
        end = time.time() + 5
        while time.time() < end:
            j = c.job(jid)
            if j["status"] == "generating" and j["preview"]:
                assert j["preview"]["total_steps"] == 6
                seen.add(j["preview"]["step"])
            if j["status"] == "completed":
                break
            time.sleep(0.01)
        assert len(seen) >= 2 and max(seen) > min(seen)
    finally:
        server.kill()


def test_cancel():
    server = serve_fake_sd(0, steps_delay=0.2)
    try:
        c = SdClient(server.url)
        jid = c.submit(to_params("x", {"steps": 20}))
        _wait(c, jid, "generating")
        assert c.cancel(jid) is None
        assert _wait(c, jid, "cancelled")["status"] == "cancelled"
    finally:
        server.kill()


def test_submit_error_raises_sderror():
    server = serve_fake_sd(0, fail_on="submit")
    try:
        with pytest.raises(SdError):
            SdClient(server.url).submit(to_params("x", {}))
    finally:
        server.kill()


def test_non_dict_json_raises_sderror(sd):
    sd.raw_status = [1, 2]
    with pytest.raises(SdError):
        SdClient(sd.url).status()


def test_unknown_job_raises_sderror(sd):
    with pytest.raises(SdError):
        SdClient(sd.url).job("nope")


def test_to_params_exact_body():
    s = {"width": 768, "height": 1024, "steps": 25, "cfg": 5.5, "sampler": "euler_a",
         "seed": 42, "negative": "blurry", "vae_tiling": True, "flow_shift": 5}
    assert to_params("a fox", s) == {
        "prompt": "a fox",
        "negative_prompt": "blurry",
        "width": 768,
        "height": 1024,
        "seed": 42,
        "sample_params": {
            "sample_steps": 25,
            "sample_method": "euler_a",
            "guidance": {"txt_cfg": 5.5},
        },
        "preview": "proj",
        "preview_interval": 1,
        "vae_tiling_params": {"enabled": True},
    }


def test_to_params_omits_vae_tiling_when_unset():
    body = to_params("p", {"width": 512, "height": 512, "steps": 4, "cfg": 1.0,
                           "sampler": "euler", "seed": -1, "negative": ""})
    assert "vae_tiling_params" not in body


# --- fix round 1 ---
def test_truncated_response_raises_sderror(sd):
    jid = SdClient(sd.url).submit(to_params("x", {}))
    sd.truncate = True
    with pytest.raises(SdError) as e:
        SdClient(sd.url).job(jid)
    assert e.value.status is None


def test_cancel_finished_job_returns_unchanged(sd):
    c = SdClient(sd.url)
    jid = c.submit(to_params("x", {}))
    _wait(c, jid, "completed")
    c.cancel(jid)
    assert c.job(jid)["status"] == "completed"
    r = c._call("POST", f"/sdcpp/v1/jobs/{jid}/cancel")
    assert r["status"] == "completed"


@pytest.mark.parametrize("code", [409, 410])
def test_cancel_tolerates_409_and_410(sd, code):
    jid = SdClient(sd.url).submit(to_params("x", {}))
    sd.cancel_status = code
    assert SdClient(sd.url).cancel(jid) is None


def test_cancel_other_errors_raise(sd):
    jid = SdClient(sd.url).submit(to_params("x", {}))
    sd.cancel_status = 500
    with pytest.raises(SdError) as e:
        SdClient(sd.url).cancel(jid)
    assert e.value.status == 500


def test_expired_job_410_carries_status(sd):
    c = SdClient(sd.url)
    jid = c.submit(to_params("x", {}))
    sd.gone.add(jid)
    with pytest.raises(SdError) as e:
        c.job(jid)
    assert e.value.status == 410


def test_queue_full_429_carries_status():
    server = serve_fake_sd(0, fail_on="queue_full")
    try:
        with pytest.raises(SdError) as e:
            SdClient(server.url).submit(to_params("x", {}))
        assert e.value.status == 429
    finally:
        server.kill()


def test_timeout_status_is_none(sd):
    sd.delay = 1.0
    with pytest.raises(SdError) as e:
        SdClient(sd.url, timeout=0.2).status()
    assert e.value.status is None
    assert SdClient("http://127.0.0.1:9", timeout=0.5).health() is False


def test_ignores_proxy_environment(sd, monkeypatch):
    import urllib.request
    from primer.images import sdclient
    handlers = [h for h in sdclient._OPENER.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert not any(h.proxies for h in handlers)  # built with ProxyHandler({}): no proxy at all
    for k in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    assert SdClient(sd.url).health() is True


def test_job_id_is_quoted(sd):
    with pytest.raises(SdError) as e:
        SdClient(sd.url).job("a/b?c")
    assert e.value.status == 404


def test_cancelled_job_has_error_code(sd):
    server = serve_fake_sd(0, steps_delay=0.2)
    try:
        c = SdClient(server.url)
        jid = c.submit(to_params("x", {"steps": 20}))
        _wait(c, jid, "generating")
        c.cancel(jid)
        assert _wait(c, jid, "cancelled")["error"] == {"code": "cancelled"}
    finally:
        server.kill()


def test_preview_frame_when_mode_requested():
    server = serve_fake_sd(0, steps_delay=0.05)
    try:
        c = SdClient(server.url)
        jid = c.submit(to_params("x", {"steps": 4}))
        frames = []
        end = time.time() + 5
        while time.time() < end:
            j = c.job(jid)
            if j["status"] == "generating" and j["preview"]:
                frames.append(j["preview"].get("b64_json"))
            if j["status"] == "completed":
                break
            time.sleep(0.01)
        assert frames and all(frames)
        jid2 = c.submit({"prompt": "x", "preview": "none"})
        j2 = _wait(c, jid2, "completed")
        assert not (j2["preview"] or {}).get("b64_json")
    finally:
        server.kill()
