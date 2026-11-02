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
