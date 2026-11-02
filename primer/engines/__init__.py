"""Text engines: one protocol, an OpenAI-compatible and an sp-daemon implementation."""
from __future__ import annotations

import urllib.request
from typing import Iterator, Protocol
from urllib.parse import urlsplit

from primer.config import EngineSpec

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# a local model server is never reached through a system HTTP proxy
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EngineError(RuntimeError):
    """Connect, HTTP or mid-stream failure of a text engine."""


class TextEngine(Protocol):
    def stream(self, messages: list[dict], *, max_tokens: int = 1200,
               temperature: float = 0.7) -> Iterator[str]: ...

    def complete(self, messages: list[dict], *, max_tokens: int = 400,
                 temperature: float = 0.2) -> str: ...

    def health(self) -> bool: ...


def opener_for(base_url: str) -> urllib.request.OpenerDirector:
    """No proxy for a loopback server; anything else (e.g. the hosted tier, which may sit
    behind a corporate proxy) uses the environment's proxies as usual."""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        host = ""
    return _DIRECT if host in LOOPBACK_HOSTS else urllib.request.build_opener()


def make_engine(spec: EngineSpec) -> TextEngine:
    if spec.kind == "openai":
        from primer.engines.openai_compat import OpenAICompatEngine
        return OpenAICompatEngine(spec)
    if spec.kind == "sp":
        from primer.engines.sp_daemon import SpDaemonEngine
        return SpDaemonEngine(spec)
    if spec.kind == "sdserver":
        raise EngineError("sdserver is not a text engine")
    raise EngineError(f"unknown engine kind {spec.kind!r}")
