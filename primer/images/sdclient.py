"""Client for stable-diffusion.cpp's sd-server native async API (/sdcpp/v1)."""
from __future__ import annotations

import base64
import binascii
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request


class SdError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status      # HTTP status; None for transport errors (refused, timeout, ...)


# never route sd-server calls through a system HTTP proxy
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def to_params(prompt: str, s: dict) -> dict:
    """Effective settings -> native img_gen body. Flow shift is a startup flag, not sent."""
    body: dict = {"prompt": prompt}
    for key, field in (("negative", "negative_prompt"), ("width", "width"),
                       ("height", "height"), ("seed", "seed")):
        if key in s:
            body[field] = s[key]
    sample: dict = {}
    if "steps" in s:
        sample["sample_steps"] = s["steps"]
    if "sampler" in s:
        sample["sample_method"] = s["sampler"]
    if "cfg" in s:
        sample["guidance"] = {"txt_cfg": s["cfg"]}
    if sample:
        body["sample_params"] = sample
    # preview fields per examples/server/api.md (master): without them the server reports no
    # step while generating
    body["preview"] = "proj"
    body["preview_interval"] = 1
    if s.get("vae_tiling"):
        body["vae_tiling_params"] = {"enabled": True}
    return body


def to_video_params(prompt: str, s: dict, loras: list[dict],
                    init_png_b64: str | None) -> dict:
    """Effective video settings -> native vid_gen body. Flow shift is a startup flag, not sent."""
    body = to_params(prompt, s)
    if "frames" in s:
        body["video_frames"] = s["frames"]
    if "fps" in s:
        body["fps"] = s["fps"]
    if "moe_boundary" in s:
        body["moe_boundary"] = s["moe_boundary"]
    if "high_noise_steps" in s:
        hn: dict = {"sample_steps": s["high_noise_steps"]}
        if "sampler" in s:
            hn["sample_method"] = s["sampler"]
        if "high_noise_cfg" in s:
            hn["guidance"] = {"txt_cfg": s["high_noise_cfg"]}
        body["high_noise_sample_params"] = hn
    if loras:
        body["lora"] = [{"path": l["path"], "multiplier": l["multiplier"],
                         "is_high_noise": l["is_high_noise"]} for l in loras]
    if init_png_b64:
        body["init_image"] = init_png_b64
    body["output_format"] = "webm"
    return body


def decode_video(job: dict) -> tuple[bytes, dict]:
    """A completed vid_gen job -> (container bytes, {mime, fps, frame_count})."""
    res = job.get("result")
    if not isinstance(res, dict):
        raise ValueError("job has no result")
    mime = res.get("mime_type")
    if mime != "video/webm":
        raise ValueError(f"unexpected video type {mime!r}")
    b64 = res.get("b64_json")
    if not isinstance(b64, str) or not b64:
        raise ValueError("result has no video data")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("video data is not valid base64") from e
    if not raw:
        raise ValueError("video data is empty")
    return raw, {"mime": mime, "fps": res.get("fps"), "frame_count": res.get("frame_count")}


class SdClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None,
              ok: tuple[int, ...] = (200,)) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            raise SdError(f"{method} {path}: HTTP {e.code} {_detail(e)}", e.code) from e
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise SdError(f"{method} {path}: {e}") from e
        if status not in ok:
            raise SdError(f"{method} {path}: HTTP {status}", status)
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise SdError(f"{method} {path}: response is not JSON") from e
        if not isinstance(obj, dict):
            raise SdError(f"{method} {path}: response is not a JSON object")
        return obj

    def status(self) -> dict:
        return self._call("GET", "/sdcpp/v1/models/status")

    def health(self) -> bool:
        try:
            return self.status().get("state") != "error"
        except SdError:
            return False

    def submit(self, params: dict) -> str:
        obj = self._call("POST", "/sdcpp/v1/img_gen", params, ok=(202,))
        jid = obj.get("id")
        if not isinstance(jid, str) or not jid:
            raise SdError("img_gen response has no job id")
        return jid

    def submit_video(self, params: dict) -> str:
        obj = self._call("POST", "/sdcpp/v1/vid_gen", params, ok=(202,))
        jid = obj.get("id")
        if not isinstance(jid, str) or not jid:
            raise SdError("vid_gen response has no job id")
        return jid

    def job(self, id: str) -> dict:
        return self._call("GET", f"/sdcpp/v1/jobs/{_q(id)}")

    def cancel(self, id: str) -> None:
        try:
            self._call("POST", f"/sdcpp/v1/jobs/{_q(id)}/cancel")
        except SdError as e:
            if e.status not in (409, 410):    # already over: finished while we were cancelling
                raise


def _q(id: str) -> str:
    return urllib.parse.quote(id, safe="")


def _detail(e: urllib.error.HTTPError) -> str:
    try:
        return e.read(300).decode("utf-8", "replace")
    except Exception:
        return ""
