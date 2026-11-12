"""Primer as an MCP server: one stable tool, `primer(action, args)`, for Kairos (or any MCP client).

    python -m primer.mcp_server            # stdio; PRIMER_URL (default http://127.0.0.1:8810)

WHY ONE TOOL. Kairos keeps every tool's schema in her cached prompt prefix and refuses a bridged
tool whose schema changes (her MCP bridge pins it). Primer's surface will keep growing, so the
schema stays fixed — an `action` string and an `args` object — and `help` says what the actions
are today. Adding an action changes nothing she has cached.

WHAT IT CAN DO (all but show, open and lens only read; those three drive his Primer when he allows it):
  help                    the actions, with their args
  status                  Primer's version, tier and what is configured
  page     {id}           a page's title and text (the id is the `ref` of a Kairos activity row)
  reader                  what Primer has noticed about how he reads, and his own notes
  quiz_due                how many review questions are due
  quest                   the quest in progress, if any
  now, trail, find        where he is, what he has read, find a read page by title
  show, open, lens        put text, a page or a lens on his screen (needs Let Kairos drive Primer)

It talks to a RUNNING Primer over its localhost HTTP API (the same routes the UI uses), so it
sees exactly what he sees. Stdlib only, like the rest of Primer: newline-delimited JSON-RPC 2.0
on stdin/stdout, the MCP stdio transport.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
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
  now                  the page he has open in Primer, and whether Kairos can drive it
  trail  {"limit": n}  the pages he has read, newest first (default 20, at most 100)
  find   {"q": ...}    pages he has read whose title contains q (at most 20)
  show   {"text": ...} put a short text on his screen in Primer (needs Let Kairos drive Primer)
  open   {"id": ...}   open a page he has read in Primer (needs Let Kairos drive Primer)
  lens   {"kind": ..., "id"?}  start a picture, map, timeline, diagram, story or listen lens on
                       a page, default the open one (needs Let Kairos drive Primer)
All but show, open and lens are read-only: none of them starts a model."""
LENS_KINDS = ("picture", "map", "timeline", "diagram", "story", "listen")
TRAIL_DEFAULT, TRAIL_MAX, FIND_MAX = 20, 100, 20
WAIT_SECONDS = 90.0
STILL_WORKING = "Primer hasn't finished yet; it's still working on it"


class PrimerDown(Exception):
    pass


def _base() -> str:
    return (os.environ.get("PRIMER_URL") or "http://127.0.0.1:8810").rstrip("/")


def _request(method: str, path: str, body=None, timeout: float = 10.0):
    headers = {"Accept": "application/json", "User-Agent": "primer-mcp/" + VERSION}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"      # and no Origin: this is not a browser
    req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error") or str(e)
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise ValueError(msg if method == "POST" else f"Primer answered {e.code}: {msg}") from None
    except (urllib.error.URLError, OSError) as e:
        raise PrimerDown(f"Primer isn't answering at {_base()} ({getattr(e, 'reason', e)}). "
                         "Is it running?") from None


def _get(path: str, timeout: float = 10.0):
    return _request("GET", path, None, timeout)


def _post(path: str, body: dict, timeout: float = 10.0):
    return _request("POST", path, body, timeout)


def _await(command_id: str) -> dict:
    """Poll a driving command until it is done or failed: every 0.5 s for the first 5 s, then 1 s."""
    start = time.monotonic()
    while True:
        cmd = _get("/v1/tool/commands/" + urllib.parse.quote(str(command_id), safe=""))
        if cmd.get("status") in ("done", "failed"):
            return cmd
        elapsed = time.monotonic() - start
        if elapsed >= WAIT_SECONDS:
            raise ValueError(STILL_WORKING)
        time.sleep(0.5 if elapsed < 5 else 1.0)


def _drive(path: str, body: dict) -> dict:
    cmd = (_post(path, body) or {}).get("command") or {}
    if not cmd.get("id"):
        raise ValueError("Primer didn't accept that")
    done = _await(cmd["id"])
    if done.get("status") == "failed":
        raise ValueError(str(done.get("error") or "Primer couldn't do that"))
    return done


def _short(page_id: str) -> str:
    return page_id[:4] + "…" if len(page_id) > 4 else page_id


_ANSWERS = {
    "opened": "Opened *%s*",
    "writing": "Primer is writing *%s*; it's open in the Reader",
    "picture": "Started a picture of *%s*; it will appear in Primer",
    "video": "Started a clip of *%s*; it will appear in Primer",
    "map": "Started a map of *%s*; it will appear in Primer",
    "timeline": "Started a timeline of *%s*; it will appear in Primer",
    "diagram": "Started a diagram of *%s*; it will appear in Primer",
    "story": "Started a story about *%s*; it will appear in Primer",
    "listen": "Reading *%s* aloud in Primer",
}


def _answer(done: dict, force_what: str = "", fallback_id: str = "") -> str:
    """The short spoken answer for a finished driving command, from its result.what and title."""
    res = done.get("result") if isinstance(done.get("result"), dict) else {}
    what, title = force_what or res.get("what"), str(res.get("title") or "").strip()
    if what not in _ANSWERS or not title:
        return "Done in Primer"
    text = _ANSWERS[what] % title
    if what == "opened":
        text += " (page %s)" % _short(str(res.get("node_id") or fallback_id))
    return text


def _compact(obj, limit: int = JSON_CHARS) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit - 1] + "…"


def act(action: str, args: dict) -> str:
    """The text answer for one action; raises ValueError for a bad call, PrimerDown when Primer
    is not reachable."""
    action = str(action or "").strip().lower()
    args = args if isinstance(args, dict) else {}
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
    if action == "now":
        n = _get("/v1/tool/now")
        f = n.get("focus") or {}
        if not n.get("listening"):  # no tab: any page it last reported is stale
            out = "Primer isn't open in a browser right now."
        else:
            out = f"He has {f['title']} open." if f.get("title") else "He has no page open."
        if not n.get("enabled"):
            out += " Kairos isn't allowed to drive Primer; he can turn on Let Kairos drive Primer."
        return out
    if action == "trail":
        raw = (args or {}).get("limit", TRAIL_DEFAULT)
        try:
            limit = max(1, min(int(raw), TRAIL_MAX))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("trail's limit must be a number") from None
        pages = (_get("/v1/tool/trail?limit=%d" % limit).get("pages") or [])[:limit]
        if not pages:
            return "He hasn't read anything yet."
        return "\n".join("%s (page %s)" % (p.get("title") or "?", p.get("id", "")) for p in pages)
    if action == "find":
        q = str((args or {}).get("q") or "").strip()
        if not q:
            raise ValueError("find needs args {\"q\": ...}")
        pages = (_get("/v1/tool/find?q=" + urllib.parse.quote(q)).get("pages") or [])[:FIND_MAX]
        if not pages:
            return f"No page he has read has {q} in its title."
        return "\n".join("%s (page %s)" % (p.get("title") or "?", p.get("id", "")) for p in pages)
    if action == "show":
        text = str((args or {}).get("text") or "").strip()
        if not text:
            raise ValueError("show needs args {\"text\": ...}")
        return _answer(_drive("/v1/tool/show", {"text": text}))
    if action == "open":
        node_id = str((args or {}).get("id") or "").strip()
        if not node_id:
            raise ValueError("open needs args {\"id\": ...}")
        return _answer(_drive("/v1/tool/open", {"id": node_id}), "opened", node_id)
    if action == "lens":
        kind = str((args or {}).get("kind") or "").strip().lower()
        if kind not in LENS_KINDS:
            raise ValueError("lens needs args {\"kind\": one of %s}" % ", ".join(LENS_KINDS))
        body = {"kind": kind}
        node_id = str((args or {}).get("id") or "").strip()
        if node_id:
            body["id"] = node_id
        return _answer(_drive("/v1/tool/lens", body))
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
