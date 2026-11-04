"""Test doubles for the text engine."""
from __future__ import annotations

from typing import Callable, Iterator

from primer.engines import EngineError


class FakeEngine:
    def __init__(self, replies: list[str] | Callable[[list[dict]], str],
                 fail_after: int | None = None) -> None:
        self.replies = replies
        self.fail_after = fail_after
        self.calls: list[list[dict]] = []
        self._n = 0

    def _next(self, messages: list[dict]) -> str:
        if callable(self.replies):
            return self.replies(messages)
        reply = self.replies[min(self._n, len(self.replies) - 1)]
        self._n += 1
        return reply

    def stream(self, messages: list[dict], *, max_tokens: int = 1200,
               temperature: float = 0.7, cancel=None) -> Iterator[str]:
        self.calls.append(messages)
        reply = self._next(messages)
        cancelled: list[bool] = []
        if cancel is not None:
            cancel.bind(lambda: cancelled.append(True))
        for i in range(0, len(reply), 5):
            if self.fail_after is not None and i // 5 >= self.fail_after:
                raise EngineError("fake engine failure")
            if cancelled:
                raise EngineError("stream cancelled")
            yield reply[i:i + 5]

    def complete(self, messages: list[dict], *, max_tokens: int = 400,
                 temperature: float = 0.2) -> str:
        return "".join(self.stream(messages, max_tokens=max_tokens, temperature=temperature))

    def health(self) -> bool:
        return True
