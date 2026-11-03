"""A fake TTS server for tests: POST /tts (LFM3-style) and POST /v1/audio/speech (OpenAI-style)
answer a tiny valid 24 kHz mono WAV whose length depends on the input; GET /health and
/v1/models. `fail_after=N` makes every request after the N-th good one fail with HTTP 503 (the
voice server going down mid-story). Also a script: python tests/fake_tts_server.py --port P
[--fail-after N]"""
from __future__ import annotations

import argparse
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 24000
FRAMES_PER_CHAR = 240  # 10 ms of speech per character of input


def make_wav(frames: int, rate: int = RATE, width: int = 2, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes((b"\x10" * width * channels) * frames)
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(b'{"status":"ok"}', "application/json")
        if self.path == "/v1/models":
            self.server.requests.append(("GET", self.path, None, dict(self.headers)))
            return self._send(b'{"data":[]}', "application/json")
        self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(b"bad json", "text/plain", 400)
        if self.path not in ("/tts", "/v1/audio/speech"):
            return self._send(b"not found", "text/plain", 404)
        with srv.lock:
            srv.requests.append(("POST", self.path, body, dict(self.headers)))
            if srv.fail_after is not None and srv.good >= srv.fail_after:
                return self._send(b"voice server down", "text/plain", 503)
            srv.good += 1
        if srv.reply is not None:
            return self._send(srv.reply, "audio/wav")
        text = str(body.get("input", ""))
        self._send(make_wav(FRAMES_PER_CHAR * max(len(text), 1)), "audio/wav")


class FakeTTS(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, fail_after: int | None = None, reply: bytes | None = None) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.fail_after, self.reply = fail_after, reply
        self.good = 0
        self.requests: list[tuple[str, str, dict | None, dict]] = []
        self.lock = threading.Lock()

    def kill(self) -> None:
        self.shutdown()
        self.server_close()


def serve(fail_after: int | None = None, reply: bytes | None = None,
          port: int = 0) -> tuple[FakeTTS, str]:
    server = FakeTTS(port, fail_after, reply)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--port", type=int, default=8840)
    ap.add_argument("--fail-after", type=int, default=None)
    args, _ = ap.parse_known_args()
    try:
        FakeTTS(args.port, args.fail_after).serve_forever()
    except KeyboardInterrupt:
        pass
