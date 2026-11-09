"""Primer as an MCP server: one stable tool, `primer(action, args)`, for Kairos (or any MCP client).

    python -m primer.mcp_server            # stdio; PRIMER_URL (default http://127.0.0.1:8810)

WHY ONE TOOL. Kairos keeps every tool's schema in her cached prompt prefix and refuses a bridged
tool whose schema changes (her MCP bridge pins it). Primer's surface will keep growing, so the
schema stays fixed — an `action` string and an `args` object — and `help` says what the actions
are today. Adding an action changes nothing she has cached.

WHAT IT CAN DO (v1, read-only: nothing here starts a model or uses the GPU):
  help                    the actions, with their args
  status                  Primer's version, tier and what is configured
  page     {id}           a page's title and text (the id is the `ref` of a Kairos activity row)
  reader                  what Primer has noticed about how he reads, and his own notes
  quiz_due                how many review questions are due
  quest                   the quest in progress, if any

It talks to a RUNNING Primer over its localhost HTTP API (the same routes the UI uses), so it
sees exactly what he sees. Stdlib only, like the rest of Primer: newline-delimited JSON-RPC 2.0
on stdin/stdout, the MCP stdio transport.
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from primer.version import VERSION

PROTOCOL = "2025-06-18"
PAGE_CHARS = 4000
JSON_CHARS = 2000
TOOL = {
    "name": "primer",
    "description": ("Look at Primer, his illustrated reader app: what page he has open and what it says, "
                    "how he reads, review questions due, his quest. Call with action \"help\" for the "
                    "actions and their args. A Primer activity row's `ref` is a page id for action \"page\"."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "what to do; \"help\" lists the actions"},
            "args": {"type": "object", "description": "the action's arguments, e.g. {\"id\": \"...\"} for page"},
        },
        "required": ["action"],
    },
}
HELP = """Primer actions (pass as action, with args):
  help                 this list
  status               Primer's version, tier and what is configured
  page   {"id": ...}   a page's title and text; the id is a Primer activity row's ref
  reader               what Primer has noticed about how he reads, and his own notes
  quiz_due             how many review questions are due
  quest                the quest in progress, if any
All read-only: none of them starts a model."""


class PrimerDown(Exception):
    pass


def _base() -> str:
    return (os.environ.get("PRIMER_URL") or "http://127.0.0.1:8810").rstrip("/")


def _get(path: str, timeout: float = 10.0):
    req = urllib.request.Request(_base() + path, headers={"Accept": "application/json",
                                                          "User-Agent": "primer-mcp/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error") or str(e)
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise ValueError(f"Primer answered {e.code}: {msg}") from None
    except (urllib.error.URLError, OSError) as e:
        raise PrimerDown(f"Primer isn't answering at {_base()} ({getattr(e, 'reason', e)}). "
                         "Is it running?") from None


def _compact(obj, limit: int = JSON_CHARS) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def act(action: str, args: dict) -> str:
    """The text answer for one action; raises ValueError for a bad call, PrimerDown when Primer
    is not reachable."""
    action = (action or "").strip().lower()
    if action == "help":
        return HELP
    if action == "status":
        return _compact(_get("/v1/status"))
    if action == "page":
        node_id = str((args or {}).get("id") or "").strip()
        if not node_id:
            raise ValueError("page needs args {\"id\": ...}")
        p = _get("/v1/pages/" + urllib.parse.quote(node_id, safe=""))
        node, ver = p.get("node") or {}, p.get("version") or {}
        text = ver.get("markdown") or ""
        if len(text) > PAGE_CHARS:
            text = text[:PAGE_CHARS - 1].rstrip() + "…"
        return "# %s\n(page %s, version %s of %s)\n\n%s" % (
            node.get("title") or node_id, node_id, ver.get("n", "?"), p.get("versions", "?") if not
            isinstance(p.get("versions"), list) else len(p["versions"]), text)
    if action == "reader":
        return _compact(_get("/v1/reader"))
    if action == "quiz_due":
        return _compact(_get("/v1/quiz/due"))
    if action == "quest":
        return _compact(_get("/v1/quest"))
    raise ValueError(f"unknown action {action!r}; call action \"help\"")


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None                                   # notifications (initialized, cancelled) need no answer
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": want, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "primer", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "primer":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        a = params.get("arguments") or {}
        try:
            text, err = act(a.get("action", ""), a.get("args") if isinstance(a.get("args"), dict) else {}), False
        except (ValueError, PrimerDown) as e:
            text, err = str(e), True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> None:
    # UTF-8 on both pipes whatever the console code page is (Windows defaults to cp1252, which
    # would mangle a page title and read a BOM as three junk characters)
    if stdin is None:
        stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", errors="replace")
    if stdout is None:
        stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True)
    for line in stdin:
        line = line.strip().lstrip("\ufeff")   # a Windows pipe may lead with a BOM
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg) if isinstance(msg, dict) else None
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
