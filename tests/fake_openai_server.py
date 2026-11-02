"""A tiny OpenAI-compatible server for tests. Also runs as a script:
python tests/fake_openai_server.py --port 8099 --reply-file F"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _rejected(self) -> bool:
        if self.server.auth_status:
            self.send_error(self.server.auth_status)
            return True
        return False

    def do_GET(self) -> None:
        if self._rejected():
            return
        if self.path == "/v1/models":
            self._json({"data": [{"id": "fake"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self._rejected():
            return
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        self.server.last_headers = dict(self.headers)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.server.last_body = body
        reply = self.server.reply
        mode = self.server.mode
        if not body.get("stream"):
            if mode == "nondict":
                self._json([1, 2])
            elif mode == "truncated":
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b'{"choices": [')
                self.close_connection = True
            else:
                self._json({"choices": [{"message": {"role": "assistant", "content": reply}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if mode == "truncated":  # promise more bytes than we send: the client sees IncompleteRead
            self.send_header("Content-Length", "100000")
        self.end_headers()
        for i in range(0, len(reply), 5):
            evt = {"choices": [{"delta": {"content": reply[i:i + 5]}}]}
            self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
            self.wfile.flush()
        tail = {"": b"data: [DONE]\n\n",
                "error": b'data: {"error": {"message": "model crashed"}}\n\ndata: [DONE]\n\n',
                "nondict": b"data: [1, 2]\n\ndata: [DONE]\n\n",
                "finish": b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'}
        self.wfile.write(tail.get(mode, b""))  # "close" and "truncated" just stop
        self.wfile.flush()
        self.close_connection = True


def serve_fake(port: int, reply: str) -> ThreadingHTTPServer:
    """Start the fake on 127.0.0.1 (port 0 picks a free one) and return it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.reply = reply
    server.last_headers = {}
    server.last_body = {}
    server.auth_status = 0  # set to 401 to make every request fail like LM Studio with auth on
    # how a reply ends: "" normally; "error" an in-band error event; "nondict" a JSON list;
    # "finish" finish_reason but no [DONE]; "close" EOF with neither; "truncated" IncompleteRead
    server.mode = ""
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--reply-file", required=True)
    args = ap.parse_args()
    serve_fake(args.port, Path(args.reply_file).read_text(encoding="utf-8"))
    threading.Event().wait()
