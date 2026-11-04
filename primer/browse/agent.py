"""Ask about a page, and click through links toward a goal. Each model call takes a page turn on
the GPU gate so it never collides with a picture job."""
from __future__ import annotations

import time
from contextlib import nullcontext
from typing import Callable

from primer.browse.prompts import (EXCERPT, MAX_LINKS, agent_messages, ask_messages,
                                   parse_agent_reply)
from primer.browse.service import BrowseError
from primer.engines import EngineError

MAX_STEPS = 5
DEADLINE_S = 120
_RETRY_NOTE = ('That reply was not valid. Reply with only JSON: {"action":"follow","link":<a listed '
               'number>,"why":"..."} or {"action":"answer","text":"..."}.')


def _alive(emit) -> bool:
    """False once the client has gone (the SSE emit says so); True for emits that don't say."""
    check = getattr(emit, "alive", None)
    return check() if check else True


def _call(engine, gate, messages: list[dict], emit, on_piece=None, *, max_tokens: int,
          temperature: float) -> str:
    """One model call inside a page turn; raises EngineError. Stops reading the engine (and
    gives the turn back) as soon as the client has gone. The wait for the turn itself is not
    bounded by the agent's deadline."""
    turn = gate.page() if gate else nullcontext()
    if gate and not turn.try_enter():
        try:
            emit("waiting", {"reason": "paused for a picture or video"})
        except BaseException:
            turn.cancel()
            raise
    chunks: list[str] = []
    with turn:
        for piece in engine.stream(messages, max_tokens=max_tokens, temperature=temperature):
            if not _alive(emit):
                break
            chunks.append(piece)
            if on_piece:
                on_piece(piece)
    return "".join(chunks)


def ask_stream(page: dict, question: str, engine, gate, emit) -> None:
    """Answer `question` about an opened page: delta events, then done (or error)."""
    msgs = ask_messages(page["title"], page["markdown"], question)
    try:
        text = _call(engine, gate, msgs, emit, lambda p: emit("delta", {"text": p}),
                     max_tokens=700, temperature=0.3)
    except EngineError as e:
        emit("error", {"message": str(e) or "engine error"})
        return
    if not _alive(emit):
        return
    if not text.strip():
        emit("error", {"message": "empty response"})
        return
    emit("done", {})


def run_agent(start_u: str, goal: str, open_page: Callable[[str], dict], text_engine, gate, emit,
              clock: Callable[[], float] = time.monotonic, first_page: dict | None = None) -> None:
    """Click through from `start_u` toward `goal`: at most MAX_STEPS steps in DEADLINE_S seconds.
    Only links the page offered are ever followed. Events: step, answer, done, error.
    The deadline is checked before every model call, retry and fetch and after each; the real
    bound is DEADLINE_S plus one model call plus one fetch (a wait for the GPU turn counts
    toward the call). Stops quietly when the client has gone (emit.alive() is False)."""
    started = clock()
    page = first_page
    if page is None:
        try:
            page = open_page(start_u)
        except BrowseError as e:
            emit("error", {"message": str(e)})
            return
    history: list[dict] = []
    steps = 0

    def finish(reason: str, url: str, title: str) -> None:
        emit("done", {"found": False, "steps": steps, "url": url, "title": title, "reason": reason})

    def late() -> bool:
        return clock() - started > DEADLINE_S

    while steps < MAX_STEPS:
        if not _alive(emit):
            return
        if late():
            return finish("ran out of time", page["url"], page["title"])
        links = page["links"][:MAX_LINKS]
        offered = {l["n"]: l for l in links}
        msgs = agent_messages(goal, page["title"], page["markdown"][:EXCERPT * 2], links, history)
        reply = None
        for _ in range(2):  # one retry on an invalid reply
            try:
                raw = _call(text_engine, gate, msgs, emit, max_tokens=300, temperature=0.2)
            except EngineError as e:
                emit("error", {"message": str(e) or "engine error"})
                return
            if not _alive(emit):
                return
            reply = parse_agent_reply(raw, max(offered, default=0))
            if reply and (reply["action"] == "answer" or reply["link"] in offered):
                break
            reply = None
            msgs = msgs + [{"role": "assistant", "content": raw[:500]},
                           {"role": "user", "content": _RETRY_NOTE}]
            if late():
                return finish("ran out of time", page["url"], page["title"])
        if reply is None:
            return finish("the model gave no usable reply", page["url"], page["title"])
        if late():
            return finish("ran out of time", page["url"], page["title"])
        steps += 1
        base = {"n": steps, "url": page["url"], "title": page["title"], "action": reply["action"]}
        if reply["action"] == "answer":
            emit("step", {**base, "link": None, "why": ""})
            emit("answer", {"text": reply["text"]})
            emit("done", {"found": True, "steps": steps, "url": page["url"], "title": page["title"]})
            return
        link = offered[reply["link"]]
        emit("step", {**base, "link": {"n": link["n"], "text": link["text"], "url": link["url"]},
                      "why": reply["why"]})
        history.append({"title": page["title"], "url": page["url"], "why": reply["why"]})
        if steps == MAX_STEPS:
            return finish("used all the steps", link["url"], link["text"])
        if not _alive(emit):
            return
        try:
            page = open_page(link["url"])
        except BrowseError as e:
            return finish(f"couldn't open that link ({e})", page["url"], page["title"])
