import http.client
import json
import socket
import sys
import threading
import time

from primer.config import read_settings

TIER_TOML = """name = "alt"
fast = "text"
[text]
kind = "openai"
base_url = "http://127.0.0.1:1"
"""


def test_health_and_status_shape(client):
    assert client.get("/health") == {"ok": True}
    s = client.get("/v1/status")
    assert {"tier", "tiers", "suggested_tier", "hardware", "servers", "source_mode", "gpu_mode"} <= s.keys()
    assert s["hardware"]["ram_gb"] == 16.0 and [x["role"] for x in s["servers"]] == ["text", "fast"]


def test_open_streams_sse_events(client):
    events = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})
    assert [e for e, _ in events][0] == "node" and events[-1][0] == "highlights"
    assert "done" in [e for e, _ in events] and events[-1][1]["phrases"] == ["counterweight"]


def test_page_and_branches_and_graph(client):
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    assert client.get(f"/v1/pages/{nid}")["version"]["markdown"].startswith("# Trebuchet")
    assert client.get(f"/v1/branches?node={nid}&phrase=counterweight")["cards"][0]["via"] == "context"
    assert len(client.get("/v1/graph")["nodes"]) == 1
    client.get("/v1/pages/nope", expect=404)


def test_open_validation_400_before_streaming(client):
    assert client.sse("/v1/pages/open", {"concept": "x"}, expect=400)["error"]
    assert client.sse("/v1/pages/open", {"concept": "x", "via": "start", "depth": "huge"}, expect=400)["error"]
    ev = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start", "bogus": 1})
    assert ev[0][0] == "node"  # unknown fields are ignored


def test_unknown_tier_400_and_role_404(client):
    assert client.post("/v1/tier", {"name": "nope"}, expect=400)["error"]
    client.post("/v1/servers/bogus/start", {}, expect=404)
    client.post("/v1/servers/text/explode", {}, expect=404)
    assert client.post("/v1/servers/text/stop", {})["role"] == "text"
    assert client.post("/v1/servers/text/start", {})["role"] == "text"


def test_tier_switch_reloads_and_saves_settings(client, app_parts):
    app, tiers_dir = app_parts
    (tiers_dir / "alt.toml").write_text(TIER_TOML)
    old_pages = app.pages
    s = client.post("/v1/tier", {"name": "alt"})
    assert s["tier"] == "alt" and "alt" in s["tiers"]
    assert read_settings(app.var_dir)["tier"] == "alt" and app.pages is not old_pages
    assert old_pages._closed
    app.servers.stop_all()


def test_concurrent_tier_switches_do_not_break(app_parts):
    app, tiers_dir = app_parts
    (tiers_dir / "alt.toml").write_text(TIER_TOML)
    ts = [threading.Thread(target=app.reload_tier, args=("alt",)) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert app.tier.name == "alt" and not app.pages._closed
    app.servers.stop_all()


def test_static_traversal_blocked_and_unbuilt_ui_503(client):
    client.get("/../pyproject.toml", expect=404)
    client.get("/%2e%2e/pyproject.toml", expect=404)
    assert client.get("/", expect=503)["error"].startswith("UI not built")


def test_static_serves_files_and_falls_back_to_index(client, app_parts):
    app, _ = app_parts
    (app.ui_dir / "index.html").write_text("<h1>hi</h1>")
    (app.ui_dir / "app.js").write_text("1")
    c = http.client.HTTPConnection("127.0.0.1", client.port)
    c.request("GET", "/app.js")
    r = c.getresponse()
    assert r.status == 200 and "javascript" in r.getheader("Content-Type")
    r.read()
    c.request("GET", "/some/route")
    r = c.getresponse()
    assert r.status == 200 and b"<h1>hi</h1>" in r.read()
    c.request("GET", "/../pyproject.toml")
    r = c.getresponse()
    assert r.status == 404
    r.read()
    c.close()


def test_page_saved_when_client_disconnects(client, app_parts):
    app, _ = app_parts
    body = json.dumps({"concept": "Trebuchet", "via": "start"})
    sock = socket.create_connection(("127.0.0.1", client.port), timeout=10)
    head = (f"POST /v1/pages/open HTTP/1.1\r\nHost: 127.0.0.1:{client.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n")
    sock.sendall((head + body).encode())
    sock.recv(16)  # read a little, then drop the connection
    sock.close()
    for _ in range(50):
        nodes = app.graph.snapshot()["nodes"]
        if nodes and app.graph.latest_version(nodes[0]["id"]):
            break
        time.sleep(0.1)
    assert app.graph.latest_version(app.graph.snapshot()["nodes"][0]["id"]) is not None


def test_open_by_node_id_needs_no_via(client):
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    ev = dict(client.sse("/v1/pages/open", {"node_id": nid, "regenerate": True}))
    assert ev["node"]["id"] == nid and ev["done"]["n"] == 2
    ev = dict(client.sse("/v1/pages/open", {"node_id": nid, "depth": "deeper"}))
    assert ev["done"]["depth"] == "deeper" and ev["done"]["n"] == 3
    assert client.sse("/v1/pages/open", {"node_id": nid, "via": "bogus"}, expect=400)["error"]


# --- localhost-only request checks (final review item 1/2) ---
def _raw(port, method, path, body=b"", headers=None):
    """Send one request with exactly these headers (Host defaults to 127.0.0.1:port)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
        h = {k: v for k, v in h.items() if v is not None}
        c.request(method, path, body=body if method == "POST" else None, headers=h)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def _evil_tier(tmp_path):
    """A tier file outside tiers/ whose launch command writes a marker file."""
    marker = tmp_path / "pwned.txt"
    evil = tmp_path / "evil.toml"
    exe, out = sys.executable.replace("\\", "/"), str(marker).replace("\\", "/")
    evil.write_text(f'[text]\nkind="openai"\nbase_url="http://127.0.0.1:9"\nmanaged=true\n'
                    f'ready_timeout_s=3\nlaunch=["{exe}","-c","open(r\'{out}\',\'w\').write(\'x\')"]\n')
    return evil, marker


def _assert_nothing_launched(app, marker):
    assert app.tier.name == "test" and not (app.var_dir / "settings.json").exists()
    time.sleep(1.0)  # a launched python would have written the marker by now
    assert not marker.exists()


def test_rce_poc_cross_origin_tier_switch_is_refused(client, app_parts, tmp_path):
    """The final review's PoC: a no-cors text/plain POST from a foreign page naming a tier file
    outside tiers/. Must not load it, persist it, or run its launch command."""
    app, _ = app_parts
    evil, marker = _evil_tier(tmp_path)
    name = str(evil)[:-5].replace("\\", "/")
    try:
        status, _ = _raw(client.port, "POST", "/v1/tier", json.dumps({"name": name}).encode(),
                         {"Content-Type": "text/plain", "Origin": "https://evil.example"})
        assert status in (403, 415)
        _assert_nothing_launched(app, marker)
    finally:
        app.servers.stop_all()


def test_tier_names_outside_tiers_dir_are_400_and_launch_nothing(client, app_parts, tmp_path):
    app, _ = app_parts
    evil, marker = _evil_tier(tmp_path)
    stem = str(evil)[:-5]
    names = ["../evil", r"..\evil", stem, stem.replace("\\", "/"), "//host/share/evil", "nope"]
    try:
        for name in names:
            assert client.post("/v1/tier", {"name": name}, expect=400)["error"], name
        _assert_nothing_launched(app, marker)
    finally:
        app.servers.stop_all()


def test_post_with_foreign_origin_is_403(client, app_parts):
    app, _ = app_parts
    before = app.servers.statuses()
    for origin in ("https://evil.example", "null", f"http://127.0.0.1:{client.port + 1}"):
        status, _ = _raw(client.port, "POST", "/v1/servers/text/stop", b"{}",
                         {"Content-Type": "application/json", "Origin": origin})
        assert status == 403, origin
    assert app.servers.statuses() == before


def test_post_that_is_not_json_is_415(client, app_parts):
    app, _ = app_parts
    before = app.servers.statuses()
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        status, _ = _raw(client.port, "POST", "/v1/servers/text/stop", b"{}", {"Content-Type": ctype})
        assert status == 415, ctype
    assert app.servers.statuses() == before


def test_bad_host_is_403_dns_rebinding(client, app_parts):
    app, _ = app_parts
    before = app.servers.statuses()
    for host in ("evil.example", f"evil.example:{client.port}", "127.0.0.1", f"127.0.0.1:{client.port + 1}"):
        assert _raw(client.port, "GET", "/v1/graph", headers={"Host": host})[0] == 403, host
        status, _ = _raw(client.port, "POST", "/v1/servers/text/stop", b"{}",
                         {"Host": host, "Content-Type": "application/json"})
        assert status == 403, host
    assert app.servers.statuses() == before


def test_same_origin_requests_still_work(client):
    p = client.port
    for host in (f"127.0.0.1:{p}", f"localhost:{p}"):
        origin = f"http://{host}"
        assert _raw(p, "GET", "/v1/graph", headers={"Host": host})[0] == 200
        status, body = _raw(p, "POST", "/v1/servers/text/start", b"{}",
                            {"Host": host, "Origin": origin, "Content-Type": "application/json; charset=utf-8"})
        assert status == 200 and json.loads(body)["role"] == "text"


def test_tier_switch_drops_queued_prefetch(app_parts):
    app, tiers_dir = app_parts
    (tiers_dir / "alt.toml").write_text(TIER_TOML)
    old, seen = app.pages, []
    real = old.shutdown
    old.shutdown = lambda drain=True: (seen.append(drain), real(drain=drain))
    app.reload_tier("alt")
    assert seen == [False]
    app.servers.stop_all()


def test_server_start_stop_waits_for_a_tier_switch(client, app_parts):
    """Start/Stop take App._lock, so they can't act on a ServerManager a tier switch discards."""
    app, _ = app_parts
    done = []
    with app._lock:  # stands in for a tier switch in progress
        t = threading.Thread(target=lambda: done.append(client.post("/v1/servers/text/stop", {})))
        t.start()
        t.join(0.5)
        assert t.is_alive() and not done
    t.join(10)
    assert done and done[0]["role"] == "text"


# --- cross-site GETs with side effects (final residual item 1) ---
def _fast_saw(app, phrase):
    return any(phrase in json.dumps(m) for m in app.pages.fast.calls)


def test_img_style_branches_get_is_refused(client, app_parts):
    """The re-review's PoC: an <img src=".../v1/branches?node=..&phrase=.."> on a foreign page sends a
    valid Host and no Origin. It must not run the fast model or save cards."""
    app, _ = app_parts
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    img = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Referer": "https://evil.example/"}
    for i, headers in enumerate([img, {}]):  # a Fetch-Metadata browser, and one without it
        phrase = f"attacker{i}"
        status, _ = _raw(client.port, "GET", f"/v1/branches?node={nid}&phrase={phrase}", headers=headers)
        assert status == 403, headers
        assert app.graph.get_cards(nid, phrase) is None and not _fast_saw(app, phrase)


def test_cross_site_fetch_metadata_is_403_even_with_the_header(client, app_parts):
    app, _ = app_parts
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    for site in ("cross-site", "same-site"):
        h = {"Sec-Fetch-Site": site, "X-Primer": "1"}
        assert _raw(client.port, "GET", f"/v1/branches?node={nid}&phrase=evil", headers=h)[0] == 403
        assert _raw(client.port, "GET", "/v1/graph", headers=h)[0] == 403
        status, _ = _raw(client.port, "POST", "/v1/servers/text/stop", b"{}",
                         {**h, "Content-Type": "application/json"})
        assert status == 403, site
    assert app.graph.get_cards(nid, "evil") is None and not _fast_saw(app, "evil")


def test_same_origin_branches_with_header_works(client):
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    for site in ("same-origin", "none", None):
        h = {"Sec-Fetch-Site": site, "X-Primer": "1"}
        status, body = _raw(client.port, "GET", f"/v1/branches?node={nid}&phrase=counterweight", headers=h)
        assert status == 200 and json.loads(body)["cards"], site


def test_get_branches_with_foreign_origin_is_403(client, app_parts):
    """Through the dev proxy a foreign page's preflighted GET arrives with its Origin and X-Primer
    but no Sec-Fetch-Site. The Origin alone must refuse it."""
    app, _ = app_parts
    nid = client.sse("/v1/pages/open", {"concept": "Trebuchet", "via": "start"})[0][1]["id"]
    h = {"Origin": "http://localhost:3000", "X-Primer": "1"}
    assert _raw(client.port, "GET", f"/v1/branches?node={nid}&phrase=foreign", headers=h)[0] == 403
    assert _raw(client.port, "GET", "/v1/graph", headers=h)[0] == 403
    assert app.graph.get_cards(nid, "foreign") is None and not _fast_saw(app, "foreign")
    own = {"Origin": f"http://localhost:{client.port}", "Host": f"localhost:{client.port}", "X-Primer": "1"}
    assert _raw(client.port, "GET", f"/v1/branches?node={nid}&phrase=counterweight", headers=own)[0] == 200


# --- refusals drain the request body (final fix wave) ---

def _split_post(port, headers: dict, body: bytes, gap=0.2, length=None) -> bytes:
    """Send a POST's headers, wait, then its body (as http.client does in two send() calls
    under load) and return everything the server answers."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        size = len(body) if length is None else length
        head = "".join(f"{k}: {v}\r\n" for k, v in {
            "Host": f"127.0.0.1:{port}", "Content-Length": str(size), **headers}.items())
        s.sendall(f"POST /v1/servers/text/stop HTTP/1.1\r\n{head}\r\n".encode())
        time.sleep(gap)
        s.sendall(body)
        time.sleep(gap)  # an RST for unread data would arrive by now and discard the answer
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk
    finally:
        s.close()


def test_refusal_reads_a_late_body_before_answering(client, app_parts):
    app, _ = app_parts
    before = app.servers.statuses()
    body = b'{"x": "' + b"y" * 4000 + b'"}'
    for headers, status in (({"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),
                            ({"Content-Type": "text/plain"}, 415)):
        out = _split_post(client.port, headers, body)
        assert out.startswith(f"HTTP/1.1 {status} ".encode()), out[:80]
    assert app.servers.statuses() == before


def test_body_too_large_closes_the_connection(client, monkeypatch):
    from primer import server as server_mod
    monkeypatch.setattr(server_mod, "MAX_BODY", 10)
    # the body never comes: the answer must, followed by EOF (not a kept-alive connection whose
    # next "request" would be the unread body)
    out = _split_post(client.port, {"Content-Type": "application/json"}, b"", length=50)
    assert out.startswith(b"HTTP/1.1 400 ") and b"body too large" in out


# --- grounding: source modes API ---

import pytest

from primer.config import EngineSpec, ReferenceConfig, Tier, WebConfig, write_settings
from primer.grounding import Source
from tests import fake_kiwix_server
from tests.conftest import Client
from tests.fakes import FakeEngine


def _grounded(tmp_path, g, tier, settings=None, replies=None):
    """(app, client, shutdown) for `tier`; servers report healthy and launch nothing."""
    from primer.hardware import Hardware
    from primer.pages import PageService
    from primer.server import App, make_server
    from primer.servers import ManagedServer, ServerManager

    var = tmp_path / "var"
    if settings is not None:
        write_settings(var, settings)
    servers = ServerManager(tier)
    servers._servers.update({r: ManagedServer(r, tier.text, health=lambda: True) for r in ("text", "fast")})
    md = "# Whales\nWhales are big marine mammals that breathe air through blowholes."
    pages = PageService(g, FakeEngine(replies or [md]), FakeEngine(['["blowholes"]']))
    (tmp_path / "ui").mkdir(exist_ok=True)
    (tmp_path / "tiers").mkdir(exist_ok=True)
    app = App(tier=tier, servers=servers, pages=pages, graph=g, hardware=Hardware(None, 0.0, 16.0),
              var_dir=var, ui_dir=tmp_path / "ui", tiers_dir=tmp_path / "tiers")
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def close():
        server.shutdown()
        server.server_close()
        app.pages.shutdown()
        app.shutdown_maps()
    return app, Client(server.server_address[1]), close


def _spec():
    return EngineSpec(kind="openai", base_url="http://127.0.0.1:1", model="fake")


def test_status_sources_availability(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        s = client.get("/v1/status")["sources"]
        assert s["storyteller"] == {"available": True, "reason": ""}
        assert s["reference"]["available"] is True
        assert s["reference"]["books"] == [{"name": "wikipedia_en", "title": "wikipedia en"}]
        assert s["web"]["available"] is True and s["web"]["engine"] == "duckduckgo"
        kiwix.kill()
        app._forget_reference()  # status remembers the reference for a few seconds
        s = client.get("/v1/status")["sources"]
        assert s["reference"] == {"available": False, "reason": "The reference server isn't running",
                                  "books": []}
    finally:
        close()


def test_status_sources_unconfigured(tmp_path, g):
    tier = Tier("t", _spec(), _spec(), web=WebConfig(search="off"))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        s = client.get("/v1/status")["sources"]
        assert s["reference"] == {"available": False, "reason": "No [reference] section in this tier",
                                  "books": []}
        assert s["web"] == {"available": False, "reason": "Web search is off in this tier", "engine": ""}
        assert client.get("/v1/status")["source_mode"] == "storyteller"
    finally:
        close()


def test_source_mode_set_and_persisted(tmp_path, g):
    tier = Tier("t", _spec(), _spec())
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        st = client.post("/v1/source-mode", {"mode": "web"})
        assert st["source_mode"] == "web" and app.pages.source_mode == "web"
        assert read_settings(tmp_path / "var")["source_mode"] == "web"
    finally:
        close()
    g2 = type(g)(tmp_path / "other.sqlite")
    app2, client2, close2 = _grounded(tmp_path, g2, tier)  # a restart reads it back
    try:
        assert client2.get("/v1/status")["source_mode"] == "web"
        assert app2.pages.source_mode == "web"
    finally:
        close2()


def test_source_mode_survives_tier_reload(tmp_path, g):
    tier = Tier("t", _spec(), _spec())
    app, client, close = _grounded(tmp_path, g, tier, settings={"source_mode": "web"})
    try:
        (tmp_path / "tiers" / "other.toml").write_text(
            '[text]\nkind="openai"\nbase_url="http://127.0.0.1:1"\n', encoding="utf-8")
        client.post("/v1/tier", {"name": "other"})
        assert client.get("/v1/status")["source_mode"] == "web"
        assert app.pages.grounder is not None
    finally:
        close()


def test_settings_mode_beats_tier_mode_but_invalid_is_ignored(tmp_path, g):
    tier = Tier("t", _spec(), _spec(), source_mode="reference")
    app, client, close = _grounded(tmp_path, g, tier, settings={"source_mode": "bogus"})
    try:
        assert client.get("/v1/status")["source_mode"] == "reference"
    finally:
        close()


def test_source_mode_rejects_unknown(tmp_path, g):
    app, client, close = _grounded(tmp_path, g, Tier("t", _spec(), _spec()))
    try:
        for body in ({"mode": "oracle"}, {"mode": 3}, {}):
            client.post("/v1/source-mode", body, expect=400)
        assert app.pages.source_mode == "storyteller"
    finally:
        close()


def test_open_with_source_param(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        client.sse("/v1/pages/open", {"concept": "Whale", "via": "typed", "source": "bogus"}, expect=400)
        ev = dict(client.sse("/v1/pages/open", {"concept": "Whale", "via": "typed", "source": "reference"}))
        assert ev["done"]["source_mode"] == "reference" and ev["sources"]["sources"]
    finally:
        close()
        kiwix.kill()


def test_reference_href_built_per_request(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        ev = dict(client.sse("/v1/pages/open", {"concept": "Whale", "via": "typed", "source": "reference"}))
        node = ev["node"]["id"]
        srcs = client.get(f"/v1/pages/{node}")["version"]["sources"]
        assert srcs[0]["n"] == 1 and srcs[0]["origin"] == "reference"
        assert srcs[0]["href"] == f"{url}/content/wikipedia_en/Whale"
        import dataclasses
        app.tier = dataclasses.replace(app.tier, reference=ReferenceConfig(base_url="http://127.0.0.1:9911"))
        srcs = client.get(f"/v1/pages/{node}")["version"]["sources"]
        assert srcs[0]["href"] == "http://127.0.0.1:9911/content/wikipedia_en/Whale"
        app.tier = dataclasses.replace(app.tier, reference=None)  # no reference now: no link
        srcs = client.get(f"/v1/pages/{node}")["version"]["sources"]
        assert srcs[0]["origin"] == "reference" and "href" not in srcs[0]
    finally:
        close()
        kiwix.kill()


def test_web_href_only_http(tmp_path, g):
    app, client, close = _grounded(tmp_path, g, Tier("t", _spec(), _spec()))
    try:
        ev = dict(client.sse("/v1/pages/open", {"concept": "Whale", "via": "typed"}))
        node = ev["node"]["id"]
        g.add_version(node, "# Whale\nbody text here.", "web", "normal", [
            Source(1, "Good", "web", "t", url="https://example.com/a", where="example.com"),
            Source(2, "Bad", "web", "t", url="javascript:alert(1)", where="x"),
            Source(3, "File", "web", "t", url="file:///etc/passwd", where="y")])
        srcs = client.get(f"/v1/pages/{node}")["version"]["sources"]
        assert srcs[0]["href"] == "https://example.com/a"
        assert "href" not in srcs[1] and "href" not in srcs[2]
        assert srcs[1]["title"] == "Bad" and srcs[1]["where"] == "x"
    finally:
        close()


def test_reference_role_in_status_and_404_without_config(tmp_path, g):
    app, client, close = _grounded(tmp_path, g, Tier("t", _spec(), _spec()))
    try:
        assert [x["role"] for x in client.get("/v1/status")["servers"]] == ["text", "fast"]
        client.post("/v1/servers/reference/start", {}, expect=404)
        client.post("/v1/servers/reference/stop", {}, expect=404)
    finally:
        close()


def test_reference_role_listed_and_controllable_over_http(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        assert [x["role"] for x in client.get("/v1/status")["servers"]] == ["text", "fast", "reference"]
        assert client.post("/v1/servers/reference/start", {})["role"] == "reference"
    finally:
        close()
        kiwix.kill()


def test_reload_tier_stops_old_and_starts_new_reference(tmp_path, g, monkeypatch):
    from primer.servers import ManagedServer

    calls = []
    monkeypatch.setattr(ManagedServer, "stop", lambda self: calls.append(("stop", self.role, self.spec.base_url)))
    monkeypatch.setattr(ManagedServer, "start", lambda self, wait=True: calls.append(("start", self.role, self.spec.base_url)))
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url="http://127.0.0.1:9001"))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        (tmp_path / "tiers" / "other.toml").write_text(
            '[text]\nkind="openai"\nbase_url="http://127.0.0.1:1"\n'
            '[reference]\nbase_url="http://127.0.0.1:9002"\n', encoding="utf-8")
        client.post("/v1/tier", {"name": "other"})
        ref = [c for c in calls if c[1] == "reference"]
        assert ref == [("stop", "reference", "http://127.0.0.1:9001"),
                       ("start", "reference", "http://127.0.0.1:9002")]
    finally:
        close()


def test_status_reference_check_is_bounded(tmp_path, g, monkeypatch):
    import time as _t
    from primer import server as server_mod
    from primer.grounding.kiwix import KiwixClient

    monkeypatch.setattr(server_mod, "STATUS_REFERENCE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(KiwixClient, "books", lambda self: (_t.sleep(5), [])[1])  # a trickling kiwix
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url="http://127.0.0.1:9003"))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        t0 = _t.monotonic()
        s = client.get("/v1/status")["sources"]["reference"]
        assert _t.monotonic() - t0 < 2.5
        assert s == {"available": False, "reason": "The reference server isn't running", "books": []}
    finally:
        close()


def test_reference_status_is_cached_briefly(tmp_path, g, monkeypatch):
    kiwix, url = fake_kiwix_server.serve()
    tier = Tier("t", _spec(), _spec(), reference=ReferenceConfig(base_url=url))
    app, client, close = _grounded(tmp_path, g, tier)
    try:
        calls = []
        real = type(app)._books
        monkeypatch.setattr(type(app), "_books", staticmethod(lambda u: calls.append(u) or real(u)))
        client.get("/v1/status")
        client.get("/v1/status")
        assert len(calls) == 1
        app._forget_reference()
        client.get("/v1/status")
        assert len(calls) == 2
    finally:
        kiwix.kill()
        close()


def test_managed_reference_passes_book_order(tmp_path):
    from primer.server import App
    ref = ReferenceConfig(kiwix_serve="kiwix-serve", zims=("D:/z/b_book.zim", "D:/z/a_book.zim"))
    assert App.make_grounder(Tier("t", _spec(), _spec(), reference=ref)).book_order == ["b_book", "a_book"]
    ext = ReferenceConfig(base_url="http://127.0.0.1:9")
    assert App.make_grounder(Tier("t", _spec(), _spec(), reference=ext)).book_order == []


# --- Phase 5: map tiles, assets and map jobs ---
import gzip
import http.client as _hc

from primer.config import MapConfig
from primer.maps.pmtiles import write_archive, zxy_to_tileid

TILE = gzip.compress(b"mvt-bytes")


def _archive(tmp_path, name="m.pmtiles", compression=2):
    p = tmp_path / name
    tiles = [(zxy_to_tileid(0, 0, 0), TILE), (zxy_to_tileid(1, 0, 0), TILE), (zxy_to_tileid(1, 1, 1), TILE)]
    write_archive(p, tiles, {"name": "t", "vector_layers": [{"id": "water"}]}, tile_compression=compression,
                  min_zoom=0, max_zoom=1, bounds=(-10.0, -20.0, 30.0, 40.0), center=(1.0, 2.0, 1))
    return p


def _mapped(tmp_path, g, reference=None, search="off", pmtiles=None, assets=None, with_map=True):
    assets = assets if assets is not None else tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    pm = pmtiles if pmtiles is not None else _archive(tmp_path)
    tier = Tier("t", _spec(), _spec(), reference=reference, web=WebConfig(search=search),
                map=MapConfig(str(pm), str(assets)) if with_map else None)
    return _grounded(tmp_path, g, tier)


def _get(client, path, headers=None):
    c = _hc.HTTPConnection("127.0.0.1", client.port, timeout=10)
    try:
        c.request("GET", path, headers={"Host": f"127.0.0.1:{client.port}", **(headers or {})})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def test_tiles_served_gzip_and_204(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        status, h, body = _get(client, "/v1/map/tiles/1/1/1.mvt")
        assert status == 200 and body == TILE
        assert h["Content-Encoding"] == "gzip"
        assert h["Content-Type"] == "application/vnd.mapbox-vector-tile"
        assert h["Cache-Control"] == "max-age=86400"
        status, h, body = _get(client, "/v1/map/tiles/1/0/1.mvt")
        assert status == 204 and body == b""
    finally:
        close()


def test_tiles_uncompressed_archive_has_no_content_encoding(tmp_path, g):
    app, client, close = _mapped(tmp_path, g, pmtiles=_archive(tmp_path, "raw.pmtiles", compression=1))
    try:
        status, h, _ = _get(client, "/v1/map/tiles/0/0/0.mvt")
        assert status == 200 and "Content-Encoding" not in h
    finally:
        close()


def test_tiles_bad_coords_400(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        for p in ("/v1/map/tiles/1/2/0.mvt", "/v1/map/tiles/0/0/1.mvt", "/v1/map/tiles/40/0/0.mvt",
                  "/v1/map/tiles/99999999999/0/0.mvt"):
            assert _get(client, p)[0] == 400, p
        for p in ("/v1/map/tiles/a/0/0.mvt", "/v1/map/tiles/1/0/0.png", "/v1/map/tiles/1/0.mvt"):
            assert _get(client, p)[0] == 404, p
    finally:
        close()


def test_tiles_unavailable_503(tmp_path, g):
    bad = tmp_path / "bad.pmtiles"
    bad.write_bytes(b"PMTiles\x03" + b"\0" * 20)  # truncated
    for i, kw in enumerate(({"with_map": False}, {"pmtiles": bad}, {"pmtiles": tmp_path / "missing.pmtiles"})):
        sub = tmp_path / f"s{i}"
        sub.mkdir()
        app, client, close = _mapped(sub, g, **kw)
        try:
            status, _, body = _get(client, "/v1/map/tiles/0/0/0.mvt")
            assert status == 503 and json.loads(body)["error"]
            assert _get(client, "/v1/map/tilejson")[0] == 503
        finally:
            close()


def test_tilejson_from_archive(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        status, h, body = _get(client, "/v1/map/tilejson")
        tj = json.loads(body)
        assert status == 200 and tj["tilejson"] == "3.0.0"
        assert tj["tiles"] == [f"http://127.0.0.1:{client.port}/v1/map/tiles/{{z}}/{{x}}/{{y}}.mvt"]
        assert (tj["minzoom"], tj["maxzoom"]) == (0, 1)
        assert tj["bounds"] == pytest.approx([-10.0, -20.0, 30.0, 40.0])
        assert tj["center"] == pytest.approx([1.0, 2.0, 1])
        assert tj["attribution"] == "© OpenStreetMap contributors · Protomaps"
        assert tj["vector_layers"] == [{"id": "water"}]
    finally:
        close()


def test_map_assets_confined(tmp_path, g):
    assets = tmp_path / "assets"
    (assets / "fonts" / "Noto").mkdir(parents=True)
    (assets / "fonts" / "Noto" / "0-255.pbf").write_bytes(b"pbf")
    (assets / "sprites.json").write_text("{}")
    (assets / "sprites.png").write_bytes(b"png")
    (assets / "notes.txt").write_text("no")
    (tmp_path / "secret.json").write_text('{"secret": 1}')
    (tmp_path / "var").mkdir(exist_ok=True)
    (tmp_path / "var" / "primer.json").write_text("{}")
    app, client, close = _mapped(tmp_path, g, assets=assets)
    try:
        status, h, body = _get(client, "/v1/map/assets/fonts/Noto/0-255.pbf")
        assert (status, body, h["Content-Type"]) == (200, b"pbf", "application/x-protobuf")
        assert h["Cache-Control"] == "max-age=86400"
        assert _get(client, "/v1/map/assets/sprites.json")[2] == b"{}"
        assert _get(client, "/v1/map/assets/sprites.json")[1]["Content-Type"] == "application/json"
        assert _get(client, "/v1/map/assets/sprites.png")[1]["Content-Type"] == "image/png"
        for p in ("/v1/map/assets/notes.txt", "/v1/map/assets/missing.json", "/v1/map/assets/",
                  "/v1/map/assets/fonts", "/v1/map/assets/../secret.json", "/v1/map/assets/%2e%2e/secret.json",
                  "/v1/map/assets/..%2fsecret.json", "/v1/map/assets/%2e%2e%2f%2e%2e%2fvar/primer.json",
                  "/v1/map/assets//etc/passwd", "/v1/map/assets/" + str(tmp_path / "secret.json").replace("\\", "/"),
                  "/v1/map/assets/..\\secret.json", "/v1/map/assets/%5c..%5csecret.json",
                  "/v1/map/assets/C:/Windows/win.ini", "/v1/map/assets/sprites.json%00.png",
                  "/v1/map/assets/fonts/Noto/../../../secret.json",
                  "/v1/map/assets/sprites.json:hid.json", "/v1/map/assets/sprites.json::$DATA",
                  "/v1/map/assets/fonts:x/y.pbf"):
            status, _, body = _get(client, p)
            assert status == 404 and b"secret" not in body, p
    finally:
        close()


def test_map_assets_404_without_map(tmp_path, g):
    app, client, close = _mapped(tmp_path, g, with_map=False)
    try:
        assert _get(client, "/v1/map/assets/sprites.json")[0] == 404
    finally:
        close()


def test_worker_style_request_without_origin_is_allowed(tmp_path, g):
    """MapLibre fetches tiles and glyphs from a web worker: same-origin, no Origin header."""
    app, client, close = _mapped(tmp_path, g)
    try:
        for p in ("/v1/map/tiles/0/0/0.mvt", "/v1/map/tilejson"):
            assert _get(client, p, {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"})[0] == 200, p
            assert _get(client, p, {"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{client.port}"})[0] == 200
            assert _get(client, p, {"Sec-Fetch-Site": "cross-site"})[0] == 403
            assert _get(client, p, {"Origin": "http://evil.example"})[0] == 403
    finally:
        close()


def test_map_available_reasons(tmp_path, g):
    kiwix, url = fake_kiwix_server.serve()
    app, client, close = _mapped(tmp_path, g, with_map=False)
    try:
        assert client.get("/v1/status")["map_available"] == {"ok": False, "reason": "No [map] section in this tier"}
    finally:
        close()
    sub = tmp_path / "a"
    sub.mkdir()
    app, client, close = _mapped(sub, g, pmtiles=sub / "nope.pmtiles")
    try:
        m = client.get("/v1/status")["map_available"]
        assert m["ok"] is False and m["reason"].startswith("The map file can't be read (") and m["reason"].endswith(")")
    finally:
        close()
    sub = tmp_path / "b"
    sub.mkdir()
    app, client, close = _mapped(sub, g)  # no reference, web off
    try:
        assert client.get("/v1/status")["map_available"] == {
            "ok": False, "reason": "No way to look places up: no offline reference and web search is off"}
    finally:
        close()
    sub = tmp_path / "c"
    sub.mkdir()
    app, client, close = _mapped(sub, g, search="duckduckgo")  # web on, no reference
    try:
        assert client.get("/v1/status")["map_available"] == {"ok": True, "reason": ""}
    finally:
        close()
    sub = tmp_path / "d"
    sub.mkdir()
    app, client, close = _mapped(sub, g, reference=ReferenceConfig(base_url=url))  # reference up
    try:
        assert client.get("/v1/status")["map_available"] == {"ok": True, "reason": ""}
        kiwix.kill()
        app._forget_reference()
        assert client.get("/v1/status")["map_available"] == {
            "ok": False, "reason": "The reference server isn't running and web search is off"}
    finally:
        close()
    sub = tmp_path / "e"
    sub.mkdir()
    app, client, close = _mapped(sub, g, reference=ReferenceConfig(base_url=url), search="duckduckgo")
    try:  # reference down but web on: still a way
        assert client.get("/v1/status")["map_available"] == {"ok": True, "reason": ""}
    finally:
        close()


def _page_node(g):
    n = g.node_for("Castles", "concept")
    g.add_version(n.id, "Kenilworth Castle stands near Coventry.", "storyteller", "normal")
    return n


def _wait_map(client, mid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        m = client.get(f"/v1/maps/{mid}")
        if m["status"] in ("done", "failed"):
            return m
        time.sleep(0.05)
    raise AssertionError("map job did not finish")


def test_maps_api_create_get_again(tmp_path, g):
    app, client, close = _mapped(tmp_path, g, search="duckduckgo")
    node = _page_node(g)
    app.maps.shutdown()
    from primer.maps import Place
    from primer.maps.service import MapService

    class Geo:
        clock = time.monotonic

        def locate(self, name, deadline, what=""):
            return Place(name=name, lat=52.3, lon=-1.6, title=name, source="reference")

    app.maps = MapService(g, FakeEngine([]), lambda refresh=False: Geo())
    try:
        r = client.post("/v1/maps", {"node_id": node.id, "phrase": "Kenilworth Castle"}, expect=202)
        done = _wait_map(client, r["id"])
        assert done["status"] == "done" and done["phrase"] == "Kenilworth Castle"
        assert [p["name"] for p in done["places"]] == ["Kenilworth Castle"]
        again = client.post(f"/v1/maps/{r['id']}/again", {}, expect=202)
        assert again["id"] != r["id"]
        assert _wait_map(client, again["id"])["places"][0]["lat"] == 52.3
        client.get("/v1/maps/nope", expect=404)
        client.post("/v1/maps/nope/again", {}, expect=404)
        client.post("/v1/maps", {"node_id": "nope"}, expect=404)
        client.post("/v1/maps", {}, expect=400)
        client.post("/v1/maps", {"node_id": 5}, expect=400)
        client.post("/v1/maps", {"node_id": node.id, "phrase": 5}, expect=400)
        empty = g.node_for("Empty", "concept")  # no page written yet
        client.post("/v1/maps", {"node_id": empty.id}, expect=404)
    finally:
        close()
        app.maps.shutdown()


def test_maps_api_refused_when_unavailable(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)  # no reference, web off
    node = _page_node(g)
    try:
        err = client.post("/v1/maps", {"node_id": node.id}, expect=409)
        assert err["error"].startswith("No way to look places up")
        mid = g.add_map(node.id, 1, "")
        assert client.post(f"/v1/maps/{mid}/again", {}, expect=409)["error"]
        assert len(g.maps_for(node.id)) == 1  # nothing new was created
    finally:
        close()


def test_page_lists_maps(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    node = _page_node(g)
    try:
        assert client.get(f"/v1/pages/{node.id}")["maps"] == []
        mid = g.add_map(node.id, 1, "Coventry")
        maps = client.get(f"/v1/pages/{node.id}")["maps"]
        assert [m["id"] for m in maps] == [mid] and maps[0]["phrase"] == "Coventry"
    finally:
        close()


def test_tier_reload_closes_old_map_service_and_archive(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        assert _get(client, "/v1/map/tiles/0/0/0.mvt")[0] == 200
        old_maps, old_archive = app.maps, app._archive
        assert old_archive is not None
        (tmp_path / "tiers" / "other.toml").write_text(
            '[text]\nkind="openai"\nbase_url="http://127.0.0.1:1"\nmodel="m"\n'
            '[fast]\nkind="openai"\nbase_url="http://127.0.0.1:1"\nmodel="m"\n')
        client.post("/v1/tier", {"name": "other"})
        assert old_maps._closed and app.maps is not old_maps
        assert old_archive._f.closed and app._archive is None
        assert client.get("/v1/status")["map_available"]["reason"] == "No [map] section in this tier"
        assert _get(client, "/v1/map/tiles/0/0/0.mvt")[0] == 503
    finally:
        close()
        app.shutdown_maps()


def test_intent_map(client):
    assert client.post("/v1/intent", {"text": "show me Warwick Castle on a map"})["map"] == "Warwick Castle"
    r = client.post("/v1/intent", {"text": "what is a map projection?"})
    assert r["map"] is None and "image" in r and "video" in r
    assert client.post("/v1/intent", {"text": "draw a moat"})["map"] is None


def test_unsupported_tile_compression_is_unavailable(tmp_path, g):
    app, client, close = _mapped(tmp_path, g, search="duckduckgo",
                                 pmtiles=_archive(tmp_path, "br.pmtiles", compression=3))
    try:
        status, _, body = _get(client, "/v1/map/tiles/0/0/0.mvt")
        assert status == 503 and "unsupported tile compression" in json.loads(body)["error"]
        assert "unsupported tile compression" in client.get("/v1/status")["map_available"]["reason"]
        assert _get(client, "/v1/map/tilejson")[0] == 503
    finally:
        close()


def test_tier_reload_mid_request_does_not_leave_old_archive_open(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        (tmp_path / "tiers" / "other.toml").write_text(
            '[text]' + chr(10) + 'kind="openai"' + chr(10) + 'base_url="http://127.0.0.1:1"' + chr(10)
            + 'model="m"' + chr(10) + '[fast]' + chr(10) + 'kind="openai"' + chr(10)
            + 'base_url="http://127.0.0.1:1"' + chr(10) + 'model="m"' + chr(10))
        gate, entered = threading.Event(), threading.Event()
        real_stop = app.servers.stop_all

        def slow_stop():
            entered.set()
            gate.wait(10)
            real_stop()
        app.servers.stop_all = slow_stop
        t = threading.Thread(target=lambda: app.reload_tier("other"))
        t.start()
        assert entered.wait(10)  # shutdown_maps has run; the old tier is still current
        assert _get(client, "/v1/map/tiles/0/0/0.mvt")[0] == 200  # reopens the old file
        reopened = app._archive
        assert reopened is not None
        gate.set()
        t.join(10)
        assert reopened._f.closed and app._archive is None
    finally:
        gate.set()
        close()


def test_archive_reopened_when_tier_points_elsewhere(tmp_path, g):
    app, client, close = _mapped(tmp_path, g)
    try:
        first = app.map_archive()
        other = _archive(tmp_path, "other.pmtiles")
        app.tier = Tier("t", _spec(), _spec(), map=MapConfig(str(other), str(tmp_path / "assets")))
        second = app.map_archive()
        assert second is not first and first._f.closed and str(second.path) == str(other)
    finally:
        close()


def test_intent_map_wins_over_image_and_video(client):
    for text in ("draw a map of England", "show me a picture of Paris on a map",
                 "make a map of the Roman Empire"):
        r = client.post("/v1/intent", {"text": text})
        assert r["map"] and r["image"] is None and r["video"] is None, text
