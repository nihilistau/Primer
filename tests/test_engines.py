import pytest

from primer.config import EngineSpec
from primer.engines import EngineError, make_engine
from tests.fakes import FakeEngine

MSG = [{"role": "user", "content": "x"}]


def test_openai_stream_and_complete(fake_server):
    e = make_engine(EngineSpec("openai", fake_server.url))
    assert e.health() is True
    chunks = list(e.stream(MSG))
    assert len(chunks) > 1
    assert "".join(chunks) == "Hello trebuchet world"
    assert e.complete(MSG) == "Hello trebuchet world"


def test_openai_unreachable_raises():
    e = make_engine(EngineSpec("openai", "http://127.0.0.1:9"))
    assert e.health() is False
    with pytest.raises(EngineError):
        list(e.stream(MSG))


def test_api_key_read_from_file(fake_server, tmp_path):
    (tmp_path / "k").write_text("sekret\n")
    e = make_engine(EngineSpec("openai", fake_server.url, api_key_file=str(tmp_path / "k")))
    e.complete(MSG)
    assert fake_server.last_headers["Authorization"] == "Bearer sekret"


def test_model_sent_only_when_named(fake_server):
    make_engine(EngineSpec("openai", fake_server.url)).complete(MSG)
    assert "model" not in fake_server.last_body
    make_engine(EngineSpec("openai", fake_server.url, model="m1")).complete(MSG)
    assert fake_server.last_body["model"] == "m1"


def test_make_engine_sp_kind():
    assert type(make_engine(EngineSpec("sp", "http://127.0.0.1:3000"))).__name__ == "SpDaemonEngine"


def test_fake_engine_fails_after():
    with pytest.raises(EngineError):
        list(FakeEngine(["abcdefghijkl"], fail_after=1).stream([]))


def test_health_401_records_last_error(fake_server):
    fake_server.auth_status = 401
    e = make_engine(EngineSpec("openai", fake_server.url))
    assert e.health() is False and "HTTP 401" in e.last_error and "api_key_file" in e.last_error
    fake_server.auth_status = 0
    assert e.health() is True and e.last_error == ""


def test_page_error_for_401_and_403_names_api_key(fake_server):
    e = make_engine(EngineSpec("openai", fake_server.url))
    for code in (401, 403):
        fake_server.auth_status = code
        with pytest.raises(EngineError, match=f"HTTP {code}.*api_key_file"):
            e.complete(MSG)
        with pytest.raises(EngineError, match=f"HTTP {code}.*api_key_file"):
            list(e.stream(MSG))
    fake_server.auth_status = 500
    with pytest.raises(EngineError, match="HTTP 500"):
        e.complete(MSG)


def test_auth_hint_depends_on_whether_a_key_is_configured(fake_server, tmp_path):
    fake_server.auth_status = 401
    e = make_engine(EngineSpec("openai", fake_server.url))
    e.health()
    assert "requires an API key" in e.last_error
    (tmp_path / "k").write_text("sekret")
    e = make_engine(EngineSpec("openai", fake_server.url, api_key_file=str(tmp_path / "k")))
    e.health()
    assert "rejected the API key" in e.last_error
    with pytest.raises(EngineError, match="rejected the API key"):
        e.complete(MSG)


def test_unreadable_key_error_not_overwritten_by_401(fake_server, tmp_path):
    fake_server.auth_status = 401
    e = make_engine(EngineSpec("openai", fake_server.url, api_key_file=str(tmp_path / "missing")))
    e.health()
    assert "cannot read api key file" in e.last_error


# --- a model server that fails mid-page must not look like a finished page (final review item 3) ---
@pytest.mark.parametrize("mode", ["error", "close", "nondict", "truncated"])
def test_openai_stream_failure_modes_raise(fake_server, mode):
    fake_server.mode = mode
    e = make_engine(EngineSpec("openai", fake_server.url))
    got = []
    with pytest.raises(EngineError):
        for piece in e.stream(MSG):
            got.append(piece)
    assert "".join(got) == "Hello trebuchet world"  # the half page arrived first, then the failure


def test_openai_finish_reason_without_done_is_a_clean_end(fake_server):
    fake_server.mode = "finish"
    assert "".join(make_engine(EngineSpec("openai", fake_server.url)).stream(MSG)) == "Hello trebuchet world"


@pytest.mark.parametrize("mode", ["nondict", "truncated"])
def test_openai_complete_failure_modes_raise(fake_server, mode):
    fake_server.mode = mode
    with pytest.raises(EngineError):
        make_engine(EngineSpec("openai", fake_server.url)).complete(MSG)


@pytest.fixture
def fake_sp():
    """A minimal sp-daemon: POST /v1/chat answers SSE {"delta": ...} lines; .mode picks the ending."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code = 200 if self.path == "/health" else 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            mode = self.server.mode
            self.wfile.write(b'event: kairos\ndata: [1]\n\ndata: {"delta": "Half a page"}\n\n')
            self.wfile.write({"": b"data: [DONE]\n\n",
                              "error": b'data: {"error": "model crashed"}\n\ndata: [DONE]\n\n',
                              "nondict": b"data: [1, 2]\n\ndata: [DONE]\n\n",
                              "nonstr": b'data: {"delta": {"text": "x"}}\n\ndata: [DONE]\n\n'}.get(mode, b""))
            self.wfile.flush()
            self.close_connection = True

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    s.mode = ""
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def _sp(server):
    return make_engine(EngineSpec("sp", f"http://127.0.0.1:{server.server_address[1]}"))


def test_sp_stream_ok_skips_kairos_events(fake_sp):
    assert "".join(_sp(fake_sp).stream(MSG)) == "Half a page"


@pytest.mark.parametrize("mode", ["error", "close", "nondict", "nonstr"])
def test_sp_stream_failure_modes_raise(fake_sp, mode):
    fake_sp.mode = mode
    with pytest.raises(EngineError):
        list(_sp(fake_sp).stream(MSG))


def test_relative_api_key_file_resolves_against_repo_root(fake_server, tmp_path, monkeypatch):
    import primer.engines.openai_compat as oc

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "key.txt").write_text("sekret\n")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(oc, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.chdir(tmp_path / "elsewhere")
    make_engine(EngineSpec("openai", fake_server.url, api_key_file="key.txt")).complete(MSG)
    assert fake_server.last_headers["Authorization"] == "Bearer sekret"


# --- loopback engines ignore system proxies; remote ones still use them ---

@pytest.fixture
def dead_proxy(monkeypatch):
    """Point the proxy environment at a dead port; rebuild urllib's cached default opener."""
    import urllib.request
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)


def test_openai_loopback_ignores_proxy_environment(fake_server, dead_proxy):
    e = make_engine(EngineSpec("openai", fake_server.url))
    assert e.health() is True
    assert e.complete(MSG) == "Hello trebuchet world"
    assert "".join(e.stream(MSG)) == "Hello trebuchet world"
    local = make_engine(EngineSpec("openai", fake_server.url.replace("127.0.0.1", "localhost")))
    assert local.health() is True


def test_sp_loopback_ignores_proxy_environment(fake_sp, dead_proxy):
    e = _sp(fake_sp)
    assert e.health() is True
    assert "".join(e.stream(MSG)) == "Half a page"


def _proxy_handlers(opener):
    import urllib.request
    return [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]


@pytest.mark.parametrize("url, direct", [
    ("http://127.0.0.1:1234", True), ("http://localhost:8081/v1", True), ("http://[::1]:3000", True),
    ("https://api.x.ai", False), ("http://gpu-box.lan:1234", False)])
def test_only_loopback_hosts_bypass_proxies(url, direct, dead_proxy):
    from primer.engines import opener_for
    handlers = _proxy_handlers(opener_for(url))
    if direct:
        assert not any(h.proxies for h in handlers)  # an empty ProxyHandler isn't even kept
    else:  # the hosted tier may need a corporate proxy: the environment's proxies apply
        assert handlers and handlers[0].proxies.get("https") == "http://127.0.0.1:9"
