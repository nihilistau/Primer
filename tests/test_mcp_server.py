"""primer.mcp_server: one stable tool over stdio, answering from a running Primer's HTTP API."""
import io
import json
import subprocess
import sys
import threading

import pytest

from primer import mcp_server


def _call(msgs):
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _tool(action, args=None, mid=3):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": "primer", "arguments": {"action": action, "args": args or {}}}}


def test_handshake_and_one_stable_tool():
    init, listed = _call([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {}}
    [tool] = listed["result"]["tools"]
    assert tool["name"] == "primer"
    # the schema must not grow with the actions: an action is a free string, help lists them
    assert set(tool["inputSchema"]["properties"]) == {"action", "args"}
    assert "enum" not in tool["inputSchema"]["properties"]["action"]


def test_help_and_bad_calls():
    [h] = _call([_tool("help")])
    assert not h["result"]["isError"] and "page" in h["result"]["content"][0]["text"]
    [bad] = _call([_tool("launch_missiles")])
    assert bad["result"]["isError"] and "help" in bad["result"]["content"][0]["text"]
    [nopage] = _call([_tool("page")])
    assert nopage["result"]["isError"]
    [unknown] = _call([{"jsonrpc": "2.0", "id": 9, "method": "nope"}])
    assert unknown["error"]["code"] == -32601


def test_primer_down_is_a_tool_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("PRIMER_URL", "http://127.0.0.1:1")
    [r] = _call([_tool("status")])
    assert r["result"]["isError"] and "isn't answering" in r["result"]["content"][0]["text"]


def test_reads_a_running_primer(client, app_parts, monkeypatch):
    app, _ = app_parts
    monkeypatch.setenv("PRIMER_URL", f"http://127.0.0.1:{client.port}")
    client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})
    node_id = app.graph.node_for("Trebuchet", "concept").id
    [st, page, rd, due, quest] = _call([_tool("status", mid=1), _tool("page", {"id": node_id}, mid=2),
                                        _tool("reader", mid=3), _tool("quiz_due", mid=4), _tool("quest", mid=5)])
    for r in (st, page, rd, due, quest):
        assert not r["result"]["isError"], r["result"]["content"][0]["text"]
    text = page["result"]["content"][0]["text"]
    assert text.startswith("# Trebuchet") and "counterweight" in text
    [missing] = _call([_tool("page", {"id": "no-such-node"})])
    assert missing["result"]["isError"] and "404" in missing["result"]["content"][0]["text"]


def test_runs_as_a_module_over_stdio():
    p = subprocess.run([sys.executable, "-m", "primer.mcp_server"], input=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", capture_output=True, text=True, timeout=30)
    assert json.loads(p.stdout)["result"]["tools"][0]["name"] == "primer"


def test_a_leading_bom_is_not_a_parse_error():
    out = io.StringIO()
    mcp_server.serve(io.StringIO("\ufeff" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"), out)
    assert json.loads(out.getvalue())["result"] == {}
