"""Voice engines: LFM3-Audio on llama-server (`POST /tts`) and OpenAI-style `/v1/audio/speech`."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from primer.config import REPO_ROOT, VoiceConfig
from primer.engines import opener_for

HEALTH_TIMEOUT_S = 2.0


class VoiceError(Exception):
    """The voice server couldn't be reached or didn't return speech."""


class VoiceClient:
    def __init__(self, cfg: VoiceConfig) -> None:
        self.cfg = cfg
        self.base = cfg.base_url.rstrip("/")
        if cfg.kind == "openai" and self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self._opener = opener_for(self.base)

    def _key(self) -> str:
        if not self.cfg.api_key_file or self.cfg.kind != "openai":
            return ""
        try:  # a relative path is relative to the repo root, like the launch commands
            return (REPO_ROOT / self.cfg.api_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise VoiceError(f"cannot read api key file: {exc.strerror or exc}") from None

    def _request(self, path: str, body: dict | None) -> urllib.request.Request:
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        key = self._key()
        if key:
            headers["Authorization"] = "Bearer " + key
        return urllib.request.Request(
            self.base + path, headers=headers,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method="GET" if body is None else "POST")

    def speak(self, text: str) -> bytes:
        """One request: `text` spoken, as WAV bytes."""
        if self.cfg.kind == "lfm3":
            path, body = "/tts", {"input": text, "voice": self.cfg.voice}
        else:
            path = "/v1/audio/speech"
            body = {"model": self.cfg.model, "input": text, "voice": self.cfg.voice,
                    "response_format": "wav"}
        req = self._request(path, body)
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_s) as r:
                data = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise VoiceError(f"the voice server rejected the request (HTTP {exc.code}); "
                                 "check api_key_file") from None
            raise VoiceError(f"the voice server returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise VoiceError(f"cannot reach the voice server at {self.base}: {exc}") from None
        if not data:
            raise VoiceError("the voice server returned empty audio")
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise VoiceError("the voice server did not return WAV audio")
        return data

    def health(self) -> bool:
        path = "/health" if self.cfg.kind == "lfm3" else "/v1/models"
        try:
            with self._opener.open(self._request(path, None), timeout=HEALTH_TIMEOUT_S) as r:
                return r.status == 200
        except (VoiceError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            return False


def make_voice(cfg: VoiceConfig) -> VoiceClient:
    if cfg.kind not in ("lfm3", "openai"):
        raise VoiceError(f"unknown voice kind {cfg.kind!r}")
    return VoiceClient(cfg)
