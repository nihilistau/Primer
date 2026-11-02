"""A fake sd-server (native /sdcpp/v1 API) for tests. Also runs as a script, standing in
for sd-server in launch commands: python tests/fake_sd_server.py --listen-port P
[--listen-ip IP] [--steps-delay S] [--fail-polls N]; every other sd-server flag is ignored."""
from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import uuid
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# a 1x1 PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")

PREVIEW_B64 = base64.b64encode(PNG_1X1).decode()

# exactly the request fields the client sends (types per sd-server's api.md)
_FIELDS = {"prompt": str, "negative_prompt": str, "width": int, "height": int, "seed": int,
           "sample_params": dict, "preview": str, "preview_interval": int,
           "vae_tiling_params": dict, "output_format": str}
_PREVIEW_MODES = ("none", "proj", "tae", "vae")
_DONE = ("completed", "failed", "cancelled")


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, obj, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bad(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def do_GET(self) -> None:
        srv = self.server
        if srv.delay:
            time.sleep(srv.delay)
        if self.path == "/sdcpp/v1/models/status":
            self._json(srv.raw_status if srv.raw_status is not None else
                       {"state": "ready", "error": None, "load_elapsed_seconds": 0.0})
        elif self.path.startswith("/sdcpp/v1/jobs/"):
            jid = unquote(self.path.rsplit("/", 1)[1])
            if jid in srv.gone:
                return self._bad(410, "job expired")
            with srv.lock:
                failing = srv.fail_polls > 0
                srv.fail_polls -= failing
            if failing:           # a transient error: the job itself keeps running
                return self._bad(503, "busy")
            if srv.truncate:      # promise more bytes than we send, then hang up
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b'{"id": ')
                self.close_connection = True
                return
            with srv.lock:
                job = srv.jobs.get(jid)
                view = None if job is None else _view(job)
            if view is None:
                self._bad(404, "job not found")
            else:
                self._json(view)
        else:
            self._bad(404, "not found")

    def do_POST(self) -> None:
        srv = self.server
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        parts = self.path.split("/")
        if self.path == "/sdcpp/v1/img_gen":
            if srv.fail_on == "submit":
                return self._bad(500, "boom")
            if srv.fail_on == "queue_full":
                return self._bad(429, "queue full")
            try:
                params = json.loads(raw or b"{}")
            except ValueError:
                return self._bad(400, "invalid JSON")
            err = _check(params)
            if err:
                return self._bad(400, err)
            jid = uuid.uuid4().hex
            job = {"id": jid, "kind": "img_gen", "status": "queued", "created": _now_ms(),
                   "params": params, "cancel": False, "preview": None, "result": None,
                   "error": None, "started": None, "completed": None}
            with srv.lock:
                srv.jobs[jid] = job
            threading.Thread(target=_run, args=(srv, job), daemon=True).start()
            self._json({"id": jid, "kind": "img_gen", "status": "queued",
                        "created": job["created"], "poll_url": f"/sdcpp/v1/jobs/{jid}"}, 202)
        elif len(parts) == 6 and parts[:4] == ["", "sdcpp", "v1", "jobs"] and parts[5] == "cancel":
            if srv.cancel_status:
                return self._bad(srv.cancel_status, "forced")
            with srv.lock:
                job = srv.jobs.get(unquote(parts[4]))
                if job is None:
                    return self._bad(404, "job not found")
                if job["status"] in _DONE:       # terminal jobs are returned unchanged
                    body = _view(job)
                elif job["status"] == "queued":
                    _cancelled(job)
                    body = _view(job)
                else:
                    job["cancel"] = True
                    body = {"id": job["id"], "status": "generating", "cancel_requested": True}
            self._json(body)
        else:
            self._bad(404, "not found")


def _check(p) -> str:
    if not isinstance(p, dict):
        return "body must be an object"
    for k, v in p.items():
        t = _FIELDS.get(k)
        if t is None:
            return f"unknown field {k}"
        if not isinstance(v, t) or (t is int and isinstance(v, bool)):
            return f"field {k} must be {t.__name__}"
    if "prompt" not in p:
        return "prompt required"
    if "preview" in p and p["preview"] not in _PREVIEW_MODES:
        return "bad preview mode"
    if set(p.get("sample_params", {})) - {"sample_steps", "sample_method", "guidance"}:
        return "unknown sample_params field"
    if set(p.get("vae_tiling_params", {})) - {
            "enabled", "temporal_tiling", "tile_size_w", "tile_size_h", "target_overlap",
            "rel_size_w", "rel_size_h", "extra_tiling_args"}:
        return "unknown vae_tiling_params field"
    return ""


def _view(job: dict) -> dict:
    out = {k: job[k] for k in ("id", "kind", "status", "created", "started", "completed",
                               "result", "error", "preview")}
    out["queue_position"] = 0 if job["status"] == "queued" else None
    return out


def _cancelled(job: dict) -> None:
    job["status"], job["completed"] = "cancelled", _now_ms()
    job["error"] = {"code": "cancelled"}


def _run(srv, job: dict) -> None:
    steps = int(job["params"].get("sample_params", {}).get("sample_steps", 4))
    with srv.lock:
        if job["status"] != "queued":      # cancelled while queued
            return
        job["status"], job["started"] = "generating", _now_ms()
    for step in range(1, steps + 1):
        if srv.steps_delay:
            time.sleep(srv.steps_delay)
        with srv.lock:
            if job["cancel"]:
                _cancelled(job)
                return
            job["preview"] = {"pass": 1, "step": step, "total_steps": steps}
            if job["params"].get("preview", "none") != "none":
                job["preview"]["b64_json"] = PREVIEW_B64
    with srv.lock:
        job["status"], job["completed"] = "completed", _now_ms()
        job["result"] = {"output_format": "png", "images": [
            {"index": 0, "b64_json": base64.b64encode(PNG_1X1).decode()}]}


class FakeSd(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, steps_delay: float, fail_on, ip: str = "127.0.0.1"):
        super().__init__((ip, port), _Handler)
        self.steps_delay, self.fail_on = steps_delay, fail_on
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.delay = 0.0                   # seconds to stall before answering a GET
        self.gone: set[str] = set()        # job ids that answer 410
        self.truncate = False              # job GETs hang up mid-body
        self.cancel_status = 0             # force this status on cancel
        self.raw_status = None             # tests may set a non-default status body
        self.fail_polls = 0                # this many job GETs answer 503 first

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def kill(self) -> None:
        self.shutdown()
        self.server_close()


def serve_fake_sd(port: int = 0, *, steps_delay: float = 0.0, fail_on=None,
                  ip: str = "127.0.0.1") -> FakeSd:
    server = FakeSd(port, steps_delay, fail_on, ip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--listen-ip", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8820)
    ap.add_argument("--steps-delay", type=float, default=0.0)
    ap.add_argument("--fail-polls", type=int, default=0)
    args, _ = ap.parse_known_args()      # other sd-server flags are ignored
    srv = FakeSd(args.listen_port, args.steps_delay, None, args.listen_ip)
    srv.fail_polls = args.fail_polls
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
