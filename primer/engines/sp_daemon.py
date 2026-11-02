# adapted from shannon-prime-kairos/harness/inference/client.py (SPDaemonClient.chat_stream)
"""The sp-daemon: POST /v1/chat, SSE `data:` lines carrying {"delta": ...}."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Iterator

from primer.config import EngineSpec
from primer.engines import EngineError, opener_for

STREAM_TIMEOUT_S = 300.0


class SpDaemonEngine:
    def __init__(self, spec: EngineSpec) -> None:
        self.spec = spec
        self.base = spec.base_url.rstrip("/")
        self._opener = opener_for(self.base)

    def stream(self, messages: list[dict], *, max_tokens: int = 1200,
               temperature: float = 0.7) -> Iterator[str]:
        body = {"messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": True}
        if self.spec.model:
            body["model"] = self.spec.model
        req = urllib.request.Request(
            self.base + "/v1/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST")
        try:
            resp = self._opener.open(req, timeout=STREAM_TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            raise EngineError(f"sp-daemon returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise EngineError(f"cannot reach sp-daemon at {self.base}: {exc}") from None
        event = "message"
        finished = False  # the sp-daemon always ends a turn with [DONE]
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    break
                name, event = event, "message"   # the name applies to this data line only
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if name == "kairos":
                    continue
                if not isinstance(evt, dict):
                    raise EngineError("stream failed: malformed event from the sp-daemon")
                if evt.get("error") and not evt.get("delta"):
                    raise EngineError(f"sp-daemon error: {evt['error']}")
                delta = evt.get("delta") or ""
                if not isinstance(delta, str):
                    raise EngineError("stream failed: malformed event from the sp-daemon")
                if delta:
                    yield delta
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise EngineError(f"stream failed: {exc}") from None
        finally:
            resp.close()
        if not finished:
            raise EngineError("stream failed: the sp-daemon closed the connection before the page finished")

    def complete(self, messages: list[dict], *, max_tokens: int = 400,
                 temperature: float = 0.2) -> str:
        return "".join(self.stream(messages, max_tokens=max_tokens, temperature=temperature))

    def health(self) -> bool:
        try:
            with self._opener.open(self.base + self.spec.health_path, timeout=2) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False
