# adapted from shannon-prime-kairos/harness/inference/backends/openai.py
"""Any OpenAI-compatible /v1/chat/completions server, over urllib."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Iterator

from primer.config import REPO_ROOT, EngineSpec
from primer.engines import EngineError, StreamCancel, open_stream, opener_for

STREAM_TIMEOUT_S = 300.0


class OpenAICompatEngine:
    def __init__(self, spec: EngineSpec) -> None:
        self.spec = spec
        self.base = spec.base_url.rstrip("/")
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self._opener = opener_for(self.base)
        self.last_error = ""  # why the last health() check failed, when we know

    def _auth_error(self, code: int) -> str:
        if self.spec.api_key_file:
            return f"HTTP {code}: the server rejected the API key in api_key_file"
        return f"HTTP {code}: the server requires an API key — set api_key_file in the tier"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream, application/json"}
        if self.spec.api_key_file:
            try:
                # a relative path is relative to the repo root, like the launch commands
                key = (REPO_ROOT / self.spec.api_key_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise EngineError(f"cannot read api key file: {exc.strerror or exc}") from None
            if key:
                headers["Authorization"] = "Bearer " + key
        return headers

    def _body(self, messages, max_tokens, temperature, stream) -> dict:
        body = {**self.spec.extra, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": stream}
        if self.spec.model:
            body["model"] = self.spec.model
        return body

    def _open(self, body: dict, cancel: StreamCancel | None = None):
        try:
            return open_stream(self._opener, self.base + "/v1/chat/completions",
                               json.dumps(body).encode("utf-8"), self._headers(), STREAM_TIMEOUT_S, cancel)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise EngineError(self._auth_error(exc.code)) from None
            raise EngineError(f"engine returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            if cancel is not None and cancel.cancelled:
                raise EngineError("stream cancelled") from None
            raise EngineError(f"cannot reach engine at {self.base}: {exc}") from None

    def stream(self, messages: list[dict], *, max_tokens: int = 1200,
               temperature: float = 0.7, cancel: StreamCancel | None = None) -> Iterator[str]:
        """Text pieces as they arrive. Closing the generator closes the connection; `cancel`
        stops it from another thread, even mid-read."""
        resp = self._open(self._body(messages, max_tokens, temperature, True), cancel)
        finished = False  # [DONE] or a finish_reason: anything else is a page cut short
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    raise EngineError("stream failed: malformed event from the engine")
                if evt.get("error"):
                    err = evt["error"]
                    msg = err.get("message") if isinstance(err, dict) else err
                    raise EngineError(f"engine error: {msg or err}")
                try:
                    choice = (evt.get("choices") or [{}])[0]
                    # reasoning_content is private thought, never text
                    text = (choice.get("delta") or {}).get("content") or ""
                    finished = finished or bool(choice.get("finish_reason"))
                except (AttributeError, IndexError, KeyError, TypeError):
                    raise EngineError("stream failed: malformed event from the engine") from None
                if not isinstance(text, str):
                    raise EngineError("stream failed: malformed event from the engine")
                if text:
                    yield text
        except (OSError, ValueError, http.client.HTTPException) as exc:
            if cancel is not None and cancel.cancelled:
                raise EngineError("stream cancelled") from None
            raise EngineError(f"stream failed: {exc}") from None
        finally:
            resp.close()
        if cancel is not None and cancel.cancelled:
            raise EngineError("stream cancelled")
        if not finished:
            raise EngineError("stream failed: the engine closed the connection before the page finished")

    def complete(self, messages: list[dict], *, max_tokens: int = 400,
                 temperature: float = 0.2) -> str:
        resp = self._open(self._body(messages, max_tokens, temperature, False))
        try:
            with resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (OSError, ValueError, AttributeError, IndexError, KeyError, TypeError,
                http.client.HTTPException) as exc:
            raise EngineError(f"bad completion response: {exc}") from None

    def health(self) -> bool:
        self.last_error = ""
        try:
            req = urllib.request.Request(self.base + self.spec.health_path)
            try:
                key = self._headers().get("Authorization")
            except EngineError as exc:
                self.last_error = str(exc)
                key = None
            if key:
                req.add_header("Authorization", key)
            with self._opener.open(req, timeout=2) as r:
                return r.status == 200
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and not self.last_error:
                self.last_error = self._auth_error(exc.code)
            return False
        except (OSError, ValueError):
            return False
