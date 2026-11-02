import pytest

from tests.fake_openai_server import serve_fake


@pytest.fixture
def fake_server():
    server = serve_fake(0, "Hello trebuchet world")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def g(tmp_path):
    from primer.graph import Graph

    return Graph(tmp_path / "primer.sqlite")


# --- HTTP client + server fixtures (Task 7) ---
import http.client
import json
import threading

from primer.config import EngineSpec, Tier


class Client:
    def __init__(self, port: int):
        self.port = port

    def _request(self, method, path, body=None, expect=200):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = None if body is None else json.dumps(body)
            c.request(method, path, body=data, headers={"Content-Type": "application/json", "X-Primer": "1"})
            r = c.getresponse()
            raw = r.read()
            assert r.status == expect, f"{method} {path}: {r.status} {raw[:200]!r}"
            return json.loads(raw) if raw else None
        finally:
            c.close()

    def get(self, path, expect=200):
        return self._request("GET", path, expect=expect)

    def post(self, path, body, expect=200):
        return self._request("POST", path, body, expect)

    def sse(self, path, body, expect=200):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", "X-Primer": "1"})
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            assert r.status == expect, f"POST {path}: {r.status} {raw[:200]}"
            if expect != 200:
                return json.loads(raw)
            assert r.getheader("Content-Type") == "text/event-stream"
            events = []
            for block in raw.split("\n\n"):
                if block.strip():
                    ev, data = block.split("\n", 1)
                    events.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
            return events
        finally:
            c.close()


@pytest.fixture
def app_parts(tmp_path, g):
    """(app, tiers_dir): an App on FakeEngines; its servers report healthy and never launch anything."""
    from primer.hardware import Hardware
    from primer.pages import PageService
    from primer.server import App
    from primer.servers import ManagedServer, ServerManager
    from tests.fakes import FakeEngine

    spec = EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")
    tier = Tier(name="test", text=spec, fast=spec)
    servers = ServerManager(tier)
    servers._servers = {"text": ManagedServer("text", spec, health=lambda: True),
                        "fast": ManagedServer("fast", spec, health=lambda: True)}
    md = "# Trebuchet\nThe trebuchet used a heavy counterweight to fling stones over castle walls."
    pages = PageService(g, FakeEngine([md]), FakeEngine(['["counterweight"]']))
    tiers_dir = tmp_path / "tiers"
    tiers_dir.mkdir()
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    app = App(tier=tier, servers=servers, pages=pages, graph=g, hardware=Hardware(None, 0.0, 16.0),
              var_dir=tmp_path / "var", ui_dir=ui_dir, tiers_dir=tiers_dir)
    yield app, tiers_dir
    app.pages.shutdown()


@pytest.fixture
def client(app_parts):
    from primer.server import make_server

    app, _ = app_parts
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Client(server.server_address[1])
    server.shutdown()
    server.server_close()


# --- no stray fake sd-servers (Phase 2 images) ---
import os
import subprocess
import sys


def _fake_sd_children() -> list[str]:
    """fake_sd_server processes this pytest process launched that are still running."""
    pid = os.getpid()
    try:
        if sys.platform == "win32":
            cmd = ["powershell", "-NoProfile", "-Command",
                   f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | "
                   # split so this query's own command line doesn't match itself
                   "Where-Object { $_.CommandLine -like ('*fake_sd' + '_server*') } | "
                   "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"]
        else:
            cmd = ["ps", "-o", "pid=,args=", "--ppid", str(pid)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if "fake_sd_server" in line]


@pytest.fixture(scope="session", autouse=True)
def no_stray_fake_sd_servers():
    yield
    left = _fake_sd_children()
    assert not left, f"fake_sd_server processes left running: {left}"


@pytest.fixture(autouse=True)
def _no_children_ledger():
    """build_app turns on the pid ledger for its var dir; don't let that leak into other tests."""
    yield
    from primer import children
    children.use_ledger(None)
