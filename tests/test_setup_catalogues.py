"""Setup catalogue clients against recorded (trimmed) fixtures; no network."""

import json
from pathlib import Path

import pytest

from primer.setup import CatalogueError, github, hf, kiwixcat
from primer.setup.catalogues import Catalogues
from primer.setup.net import Net, check_url

FIX = Path(__file__).parent / "fixtures" / "setup"
PUBLIC = [(2, 1, 6, "", ("104.18.2.2", 443))]


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def public_resolve(host, port):
    return PUBLIC


class FakeTransport:
    """Maps URL -> (status, headers, body) or a list of them; records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected request {url}")


def net_for(routes):
    t = FakeTransport(routes)
    return Net(transport=t, resolve=public_resolve), t


def ok(name):
    return 200, {}, fixture(name)


def test_hf_files_and_card():
    net, t = net_for({
        "https://huggingface.co/api/models/LiquidAI/LFM3-1.7B-GGUF/tree/main?recursive=1": ok("hf_tree_lfm.json"),
        "https://huggingface.co/api/models/LiquidAI/LFM3-1.7B-GGUF": ok("hf_model_lfm.json"),
        "https://huggingface.co/api/models/unsloth/gemma-4-E4B-it-GGUF": ok("hf_model_gemma.json"),
    })
    files = {f["path"]: f for f in hf.files("LiquidAI/LFM3-1.7B-GGUF", net=net)}
    assert "leap" not in files  # directories are not files
    nv = files["LFM3-1.7B-NVFP4.gguf"]
    assert nv["size"] == 1280019776
    assert nv["sha256"] == "462c2379586a45b30fe3dbfa30e6ff08cbb440734d6934689e24e4fe59a67bec"
    assert files[".gitattributes"]["sha256"] is None  # not an LFS file

    c = hf.card("LiquidAI/LFM3-1.7B-GGUF", net=net)
    assert c["license"] == "other" and c["gated"] is False
    assert c["license_link"] == "https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF/blob/main/LICENSE"
    assert "text-generation" in c["summary"]
    g = hf.card("unsloth/gemma-4-E4B-it-GGUF", net=net)
    assert g["license"] == "apache-2.0"
    assert g["license_link"] == "https://ai.google.dev/gemma/docs/gemma_4_license"


def test_hf_rejects_odd_repo_names():
    net, _ = net_for({})
    for bad in ("../etc/passwd", "a/b/c", "x", "a/b?x=1"):
        with pytest.raises(CatalogueError):
            hf.files(bad, net=net)


def test_hf_gated_needs_token(tmp_path):
    net, t = net_for({"https://huggingface.co/": (401, {}, b'{"error":"Unauthorized"}')})
    with pytest.raises(CatalogueError, match="token is needed"):
        hf.files("meta/gated-model", net=net)
    with pytest.raises(CatalogueError, match="refused the token"):
        hf.files("meta/gated-model", token="hf_x", net=net)
    assert "Authorization" not in t.calls[0][1]
    assert t.calls[1][1]["Authorization"] == "Bearer hf_x"
    # the Catalogues wrapper reads the token file (stripped) and passes it on
    keyfile = tmp_path / "huggingface.key"
    keyfile.write_text("  hf_secret\n")
    cats = Catalogues(net=net, token_file=keyfile)
    with pytest.raises(CatalogueError):
        cats.hf_files("meta/gated-model")
    assert t.calls[-1][1]["Authorization"] == "Bearer hf_secret"
    assert Catalogues(net=net, token_file=tmp_path / "missing.key").token() is None


def test_kiwix_zims_parsed():
    net, t = net_for({"https://library.kiwix.org/": ok("kiwix_entries.xml")})
    zims = kiwixcat.zims(net=net)
    assert t.calls[0][0] == "https://library.kiwix.org/catalog/v2/entries?lang=eng&count=-1"
    assert {z["name"] for z in zims} == {"wikipedia_en_all", "wikipedia_en_chemistry", "wikipedia_en_top"}
    top = next(z for z in zims if z["name"] == "wikipedia_en_top" and z["flavour"] == "mini")
    assert top["url"] == "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_top_mini_2026-09.zim"
    assert top["sha256_url"] == top["url"] + ".sha256"
    assert top["size"] == 296858624 and top["date"] == "2026-09-14"
    assert {z["flavour"] for z in zims} == {"mini", "nopic", "maxi"}
    assert not any(z["url"].endswith(".meta4") for z in zims)
    assert [z["name"] for z in kiwixcat.zims(name_prefix="ja.stack", net=net)] == ["ja.stackoverflow.com_mul_all"]
    assert kiwixcat.zims(name_prefix="nothing_", net=net) == []
    assert kiwixcat.zims(lang="fra", net=net) == []
    # every download address stays inside the allowlist
    for z in zims:
        check_url(z["url"], first=True, resolve=public_resolve)


def test_kiwix_follows_redirect_and_rejects_bad_xml():
    net, t = net_for({
        "https://library.kiwix.org/catalog/v2/entries?lang=eng&count=-1&x=1": ok("kiwix_entries.xml"),
        "https://library.kiwix.org/catalog": (301, {"location": "/catalog/v2/entries?lang=eng&count=-1&x=1"}, b""),
    })
    assert kiwixcat.zims(net=net)
    bad, _ = net_for({"https://library.kiwix.org/": (200, {}, b"<feed")})
    with pytest.raises(CatalogueError):
        kiwixcat.zims(net=bad)


def test_github_latest_asset_skips_releases_without_windows():
    net, t = net_for({"https://api.github.com/repos/ggml-org/llama.cpp/releases": ok("gh_llama_releases.json")})
    a = github.latest_asset("ggml-org/llama.cpp", r"bin-win-cpu-x64\.zip$", net=net)
    assert a["tag"] == "b12100"  # b12101 is Linux only
    assert a["name"] == "llama-b12100-bin-win-cpu-x64.zip"
    assert len(a["sha256"]) == 64 and a["url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/")
    assert a["size"] > 0
    cuda = github.latest_asset("ggml-org/llama.cpp", r"^llama-.*-bin-win-cuda-12\.4-x64\.zip$", net=net)
    assert cuda["tag"] == "b12100"
    # a tag prefix that matches nothing falls back to the newest match
    assert github.latest_asset("ggml-org/llama.cpp", r"win-cpu-x64", prefer_tag_prefix="zzz", net=net)["tag"] == "b12100"
    # a preferred prefix wins over newer releases
    assert github.latest_asset("ggml-org/llama.cpp", r"win-cpu-x64", prefer_tag_prefix="b12099", net=net)["tag"] == "b12099"


def test_github_asset_without_digest_is_skipped_and_none_found_errors():
    rels = json.loads(fixture("gh_sd_releases.json"))
    for a in rels[0]["assets"]:
        a["digest"] = None
    net, _ = net_for({"https://api.github.com/": (200, {}, json.dumps(rels).encode())})
    a = github.latest_asset("leejet/stable-diffusion.cpp", r"bin-win-cuda12-x64\.zip$", net=net)
    assert a["tag"] == rels[1]["tag_name"]
    with pytest.raises(CatalogueError, match="no recent release"):
        github.latest_asset("leejet/stable-diffusion.cpp", r"nothing-like-this", net=net)
    with pytest.raises(CatalogueError):
        github.latest_asset("leejet/stable-diffusion.cpp", r"(", net=net)


def test_net_allowlist_and_https_only():
    net, t = net_for({})
    for url in ("http://huggingface.co/x", "https://example.com/x", "https://evilhuggingface.co/x",
                "https://huggingface.co.evil.net/x", "https://127.0.0.1/x", "https://[::1]/x",
                "https://169.254.169.254/latest", "ftp://huggingface.co/x"):
        with pytest.raises(CatalogueError):
            net.get(url)
    assert t.calls == []
    # subdomains of an allowlisted host pass the first-request check
    assert check_url("https://cdn-lfs.huggingface.co/x", True, resolve=public_resolve) == "cdn-lfs.huggingface.co"
    # a non-allowlisted host is fine on a later hop (redirect), but still https and public only
    assert check_url("https://cdn.example.net/x", False, resolve=public_resolve)
    with pytest.raises(CatalogueError):
        check_url("http://cdn.example.net/x", False, resolve=public_resolve)
    with pytest.raises(CatalogueError):
        check_url("https://172.31.0.5/x", False, resolve=public_resolve)


def test_net_refuses_private_resolution_and_bad_redirects():
    private = lambda host, port: [(2, 1, 6, "", ("172.31.0.5", 443))]
    t = FakeTransport({"https://huggingface.co/": (200, {}, b"{}")})
    with pytest.raises(CatalogueError, match="not a public address"):
        Net(transport=t, resolve=private).get("https://huggingface.co/api/x")
    assert t.calls == []
    metadata = lambda host, port: [(2, 1, 6, "", ("169.254.169.254", 443))]
    with pytest.raises(CatalogueError):
        Net(transport=t, resolve=metadata).get("https://huggingface.co/api/x")
    # redirect to http, to an IP literal, and a redirect loop are all refused
    for loc in ("http://cdn.example.net/f", "https://127.0.0.1/f", "https://172.31.0.5/f"):
        net, _ = net_for({"https://huggingface.co/": (302, {"location": loc}, b"")})
        with pytest.raises(CatalogueError):
            net.get("https://huggingface.co/a")
    loop, _ = net_for({"https://huggingface.co/": (302, {"location": "https://huggingface.co/a"}, b"")})
    with pytest.raises(CatalogueError, match="too many redirects"):
        loop.get("https://huggingface.co/a")


def test_net_token_only_to_huggingface_and_dropped_on_other_hosts():
    net, t = net_for({
        "https://huggingface.co/start": (302, {"location": "https://cdn-lfs.huggingface.co/blob"}, b""),
        "https://cdn-lfs.huggingface.co/blob": (302, {"location": "https://cdn.example.net/real"}, b""),
        "https://cdn.example.net/real": (200, {}, b"data"),
        "https://github.com/": (200, {}, b"gh"),
    })
    r = net.get("https://huggingface.co/start", token="hf_t")
    assert r.status == 200 and r.body == b"data" and r.url == "https://cdn.example.net/real"
    auth = [c[1].get("Authorization") for c in t.calls]
    assert auth == ["Bearer hf_t", "Bearer hf_t", None]
    net.get("https://github.com/x", token="hf_t")
    assert "Authorization" not in t.calls[-1][1]


# --- fix round 1 ---

def test_github_403_is_not_a_token_message_and_404_names_host_and_repo():
    net, _ = net_for({"https://api.github.com/": (403, {}, b'{"message":"rate limit"}')})
    with pytest.raises(CatalogueError, match=r"api\.github\.com refused the request \(rate limited"):
        github.latest_asset("a/b", "x", net=net)
    net, _ = net_for({"https://api.github.com/": (404, {}, b"{}")})
    with pytest.raises(CatalogueError, match=r"api\.github\.com: a/b not found"):
        github.latest_asset("a/b", "x", net=net)
    net, _ = net_for({"https://huggingface.co/": (404, {}, b"{}")})
    with pytest.raises(CatalogueError, match=r"huggingface\.co: LiquidAI/foo not found"):
        hf.card("LiquidAI/foo", net=net)


def test_hf_files_follows_link_next_pages():
    p1 = [{"type": "file", "path": "a.gguf", "size": 1, "lfs": {"oid": "a" * 64, "size": 5}}]
    p2 = [{"type": "file", "path": "b.gguf", "size": 2, "lfs": {"oid": "b" * 64, "size": 6}}]
    base = "https://huggingface.co/api/models/o/r/tree/main?recursive=1"
    net, t = net_for({
        base + "&cursor=2": (200, {}, json.dumps(p2).encode()),
        base: (200, {"link": f'<{base}&cursor=2>; rel="next"'}, json.dumps(p1).encode()),
    })
    assert [f["path"] for f in hf.files("o/r", net=net)] == ["a.gguf", "b.gguf"]
    assert len(t.calls) == 2
    # a page that links to itself forever stops at the cap
    loop, _ = net_for({base: (200, {"link": f'<{base}>; rel="next"'}, b"[]")})
    with pytest.raises(CatalogueError, match="too many files"):
        hf.files("o/r", net=loop)
    # a next link pointing off-policy is refused
    bad, _ = net_for({base: (200, {"link": '<http://huggingface.co/x>; rel="next"'}, b"[]")})
    with pytest.raises(CatalogueError):
        hf.files("o/r", net=bad)


def test_malformed_sizes_are_catalogue_errors():
    rels = json.loads(fixture("gh_sd_releases.json"))
    for a in rels[0]["assets"]:
        a["size"] = "big"
    net, _ = net_for({"https://api.github.com/": (200, {}, json.dumps(rels).encode())})
    with pytest.raises(CatalogueError, match="size"):
        github.latest_asset("leejet/stable-diffusion.cpp", r"bin-win-cuda12-x64\.zip$", net=net)
    tree = [{"type": "file", "path": "a", "size": "x"}]
    net, _ = net_for({"https://huggingface.co/": (200, {}, json.dumps(tree).encode())})
    with pytest.raises(CatalogueError, match="size"):
        hf.files("o/r", net=net)


class _Resp:
    def __init__(self, body=b"", status=200, headers=None):
        self._body, self.status, self.headers = body, status, headers or {}
        self.closed = False

    def read(self, n=-1):
        return self._body[:n] if n >= 0 else self._body

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, result):
        self.result = result

    def open(self, req, timeout=None):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_urllib_transport_oversize_redirect_and_timeout():
    import socket
    import urllib.error
    import urllib.request

    from primer.setup import net as netmod

    url = "https://huggingface.co/x"
    ok_resp = _Resp(b"hello", headers={"Content-Type": "text/plain"})
    assert netmod.urllib_transport(url, {}, opener=_Opener(ok_resp)) == (200, {"content-type": "text/plain"}, b"hello")
    assert ok_resp.closed
    with pytest.raises(CatalogueError, match="too large"):
        netmod.urllib_transport(url, {}, max_bytes=4, opener=_Opener(_Resp(b"12345")))
    # a 3xx comes back as a response (HTTPError from the no-redirect opener), never followed here
    err = urllib.error.HTTPError(url, 302, "Found", {"Location": "https://cdn.example.net/f"}, None)
    status, hdrs, _ = netmod.urllib_transport(url, {}, opener=_Opener(err))
    assert status == 302 and hdrs["location"] == "https://cdn.example.net/f"
    assert netmod._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x/") is None
    for exc in (socket.timeout("timed out"), TimeoutError(), urllib.error.URLError("refused")):
        with pytest.raises(CatalogueError, match="network error"):
            netmod.urllib_transport(url, {}, opener=_Opener(exc))


def test_catalogues_cache_github_listings_per_repo():
    from primer.setup.catalogues import GITHUB_TTL_S

    net, t = net_for({"https://api.github.com/repos/ggml-org/llama.cpp/releases": ok("gh_llama_releases.json"),
                      "https://api.github.com/repos/leejet/stable-diffusion.cpp/releases": ok("gh_sd_releases.json")})
    now = [1000.0]
    cats = Catalogues(net=net, clock=lambda: now[0])

    def listings():
        return [c[0] for c in t.calls if "/releases" in c[0]]

    a = cats.latest_asset("ggml-org/llama.cpp", r"bin-win-cpu-x64\.zip$")
    b = cats.latest_asset("ggml-org/llama.cpp", r"^llama-.*-bin-win-cuda-12\.4-x64\.zip$")  # a swap: no new request
    assert a["tag"] == b["tag"] == "b12100" and len(listings()) == 1
    cats.latest_asset("leejet/stable-diffusion.cpp", r"bin-win-cuda12-x64\.zip$")  # per repo
    assert len(listings()) == 2
    now[0] += GITHUB_TTL_S - 1
    cats.latest_asset("ggml-org/llama.cpp", r"bin-win-cpu-x64\.zip$")
    assert len(listings()) == 2
    now[0] += 2  # ten minutes on: asked again
    cats.latest_asset("ggml-org/llama.cpp", r"bin-win-cpu-x64\.zip$")
    assert len(listings()) == 3
