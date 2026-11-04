"""Downloader and safe extraction; no network (injected stream transport)."""

import errno
import hashlib
import io
import stat
import threading
import zipfile

import pytest

from primer.setup import download as dl
from primer.setup.download import DownloadCancelled, DownloadError, download
from primer.setup.extract import ExtractError, extract_zip

URL = "https://huggingface.co/org/repo/resolve/main/model.gguf"
PUBLIC = [(2, 1, 6, "", ("104.18.2.2", 443))]


def public_resolve(host, port):
    if host == "internal.example":
        return [(2, 1, 6, "", ("172.31.0.5", 443))]
    return PUBLIC


class Stream:
    def __init__(self, data: bytes, fail_after=None, exc=None):
        self.buf = io.BytesIO(data)
        self.fail_after = fail_after
        self.exc = exc or ConnectionResetError("dropped")
        self.sent = 0
        self.closed = False

    def read(self, n=-1):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise self.exc
        if self.fail_after is not None:
            n = min(n, self.fail_after - self.sent)
        chunk = self.buf.read(n)
        self.sent += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class Server:
    """Serves one blob with Range support; optional redirect and mid-stream failure."""

    def __init__(self, data, *, redirect_to=None, fail_after=None, honour_range=True, exc=None, cr=None, always_416=False):
        self.cr, self.always_416 = cr, always_416
        self.data, self.redirect_to = data, redirect_to
        self.fail_after, self.honour_range, self.exc = fail_after, honour_range, exc
        self.calls = []
        self.streams = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.redirect_to and url == URL:
            return 302, {"location": self.redirect_to}, self._s(Stream(b""))
        rng = headers.get("Range")
        if rng and self.honour_range:
            start = int(rng.split("=")[1].rstrip("-"))
            if self.always_416 or start >= len(self.data):
                return 416, {}, self._s(Stream(b""))
            hdr = self.cr or f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"
            return 206, {"content-range": hdr}, self._s(Stream(self.data[start:], self.fail_after, self.exc))
        return 200, {}, self._s(Stream(self.data, self.fail_after, self.exc))

    def _s(self, s):
        self.streams.append(s)
        return s


def run(server, dest, **kw):
    kw.setdefault("sha256", hashlib.sha256(server.data).hexdigest())
    return download(URL, dest, transport=server, resolve=public_resolve, **kw)


DATA = bytes(range(256)) * 8192  # 2 MiB


def test_download_refuses_bad_redirect_and_bad_hash(tmp_path):
    dest = tmp_path / "m.gguf"
    for target in ("http://cdn.example.com/m.gguf", "https://internal.example/m.gguf", "https://172.31.0.5/m.gguf"):
        with pytest.raises(DownloadError):
            run(Server(DATA, redirect_to=target), dest)
        assert not dest.exists()
    bad = Server(DATA)
    with pytest.raises(DownloadError, match="hash"):
        download(URL, dest, transport=bad, resolve=public_resolve, sha256="0" * 64)
    assert not dest.exists() and not (tmp_path / "m.gguf.part").exists()
    # a good redirect to another https host works, and the token does not follow it
    ok = Server(DATA, redirect_to="https://cdn.example.com/m.gguf")
    assert run(ok, dest, token="hf_secret").read_bytes() == DATA
    assert ok.calls[0][1]["Authorization"] == "Bearer hf_secret"
    assert "Authorization" not in ok.calls[1][1]
    assert all(s.closed for s in ok.streams)


def test_refuses_without_hash_or_bad_extension_before_any_request(tmp_path):
    s = Server(DATA)
    with pytest.raises(DownloadError, match="no published hash"):
        download(URL, tmp_path / "m.gguf", transport=s, resolve=public_resolve)
    for ext in (".ckpt", ".pt", ".pth", ".bin", ".pkl"):
        with pytest.raises(DownloadError):
            run(s, tmp_path / ("m" + ext))
    with pytest.raises(DownloadError):
        run(s, tmp_path / "m.zip", allowed_ext=(".gguf",))
    assert s.calls == []
    out = download(URL, tmp_path / "ok.gguf", transport=s, resolve=public_resolve, allow_unhashed=True)
    assert out.read_bytes() == DATA


def test_download_resumes_after_restart(tmp_path):
    dest = tmp_path / "m.gguf"
    cut = int(len(DATA) * 0.6)
    with pytest.raises(DownloadError):
        run(Server(DATA, fail_after=cut), dest, size=len(DATA))
    part = tmp_path / "m.gguf.part"
    assert part.exists() and 0 < part.stat().st_size <= cut and not dest.exists()
    have = part.stat().st_size
    s2 = Server(DATA)
    seen = []
    out = run(s2, dest, size=len(DATA), progress=lambda d, t: seen.append((d, t)))
    assert s2.calls[0][1]["Range"] == f"bytes={have}-"
    assert out.read_bytes() == DATA and not part.exists()
    assert seen[-1] == (len(DATA), len(DATA))


def test_resume_server_ignores_range_restarts_and_complete_part_416(tmp_path):
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(b"garbage-prefix")
    assert run(Server(DATA, honour_range=False), dest).read_bytes() == DATA
    dest.unlink()
    (tmp_path / "m.gguf.part").write_bytes(DATA)
    assert run(Server(DATA), dest).read_bytes() == DATA


def test_stall_keeps_part(tmp_path):
    dest = tmp_path / "m.gguf"
    with pytest.raises(DownloadError, match="stalled"):
        run(Server(DATA, fail_after=1000, exc=TimeoutError("timed out")), dest)
    assert (tmp_path / "m.gguf.part").stat().st_size == 1000


def test_download_cancel_keeps_part(tmp_path):
    dest = tmp_path / "m.gguf"
    ev = threading.Event()
    srv = Server(DATA)

    def prog(done, total):
        ev.set()

    with pytest.raises(DownloadCancelled):
        run(srv, dest, cancel=ev, progress=prog)
    assert (tmp_path / "m.gguf.part").exists() and not dest.exists()
    assert all(s.closed for s in srv.streams)
    assert issubclass(DownloadCancelled, DownloadError)


def test_download_disk_full_is_clean(tmp_path, monkeypatch):
    dest = tmp_path / "m.gguf"
    n = {"c": 0}
    real = dl._write

    def flaky(fh, chunk):
        n["c"] += 1
        if n["c"] == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(fh, chunk)

    monkeypatch.setattr(dl, "_write", flaky)
    srv = Server(DATA)
    with pytest.raises(DownloadError, match="disk is full"):
        run(srv, dest, size=len(DATA))
    assert (tmp_path / "m.gguf.part").exists() and not dest.exists()
    assert all(s.closed for s in srv.streams)
    monkeypatch.setattr(dl, "_write", real)
    assert run(Server(DATA), dest, size=len(DATA)).read_bytes() == DATA


def test_precheck_refuses_when_no_space(tmp_path, monkeypatch):
    class U:
        free = 10 * 1024 * 1024

    monkeypatch.setattr(dl.shutil, "disk_usage", lambda p: U)
    s = Server(DATA)
    with pytest.raises(DownloadError, match="disk"):
        run(s, tmp_path / "m.gguf", size=200 * 1024 * 1024)
    assert s.calls == []


def test_md5_only_allowed_where_given(tmp_path):
    dest = tmp_path / "t.zip"
    good = hashlib.md5(DATA).hexdigest()
    assert download(URL, dest, md5=good, transport=Server(DATA), resolve=public_resolve).read_bytes() == DATA
    dest.unlink()
    with pytest.raises(DownloadError, match="hash"):
        download(URL, dest, md5="0" * 32, transport=Server(DATA), resolve=public_resolve)
    # sha256 wins when both are given
    with pytest.raises(DownloadError):
        download(URL, dest, md5=good, sha256="0" * 64, transport=Server(DATA), resolve=public_resolve)


def test_416_with_short_part_recovers(tmp_path):
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(DATA[:1000])
    srv = Server(DATA, always_416=True)
    assert run(srv, dest, size=len(DATA)).read_bytes() == DATA
    assert "Range" in srv.calls[0][1] and "Range" not in srv.calls[1][1]


def test_416_restart_is_bounded(tmp_path):
    class Always416(Server):
        def __call__(self, url, headers):
            self.calls.append((url, dict(headers)))
            return 416, {}, self._s(Stream(b""))

    srv = Always416(DATA)
    (tmp_path / "m.gguf.part").write_bytes(DATA[:10])
    with pytest.raises(DownloadError):
        run(srv, tmp_path / "m.gguf", size=len(DATA))
    assert len(srv.calls) == 2


def test_206_wrong_start_restarts_and_total_mismatch_fails(tmp_path):
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(DATA[:1000])
    srv = Server(DATA, cr=f"bytes 0-{len(DATA) - 1}/{len(DATA)}")
    assert run(srv, dest, size=len(DATA)).read_bytes() == DATA
    dest.unlink()
    (tmp_path / "m.gguf.part").write_bytes(DATA[:1000])
    bad = Server(DATA, cr=f"bytes 1000-{len(DATA) - 1}/{len(DATA) + 5}")
    with pytest.raises(DownloadError, match="different size"):
        run(bad, dest, size=len(DATA))
    assert not (tmp_path / "m.gguf.part").exists()


def test_incomplete_read_keeps_part(tmp_path):
    import http.client

    exc = http.client.IncompleteRead(b"x", 10)
    with pytest.raises(DownloadError, match="connection dropped"):
        run(Server(DATA, fail_after=2000, exc=exc), tmp_path / "m.gguf")
    assert (tmp_path / "m.gguf.part").stat().st_size == 2000


def test_200_restart_rechecks_space(tmp_path, monkeypatch):
    (tmp_path / "m.gguf.part").write_bytes(DATA[:1000])
    calls = []

    class U:
        free = 10**12

    def du(p):
        calls.append(1)
        return U

    monkeypatch.setattr(dl.shutil, "disk_usage", du)
    srv = Server(DATA, honour_range=False)
    run(srv, tmp_path / "m.gguf", size=len(DATA))
    assert len(calls) == 1
    (tmp_path / "m.gguf").unlink()
    (tmp_path / "m.gguf.part").write_bytes(DATA[:1000])
    calls.clear()
    run(Server(DATA, always_416=True), tmp_path / "m.gguf", size=len(DATA))
    assert len(calls) == 2


def test_auth_error_names_host(tmp_path):
    class S(Server):
        def __call__(self, url, headers):
            return 401, {}, Stream(b"")

    with pytest.raises(DownloadError, match="huggingface.co"):
        run(S(DATA), tmp_path / "m.gguf")


# ---------------------------------------------------------------- extraction

def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return path


def test_extract_ok(tmp_path):
    z = make_zip(tmp_path / "a.zip", [("bin/tool.exe", b"MZ" * 50), ("readme.txt", b"hi"), ("d/", b"")])
    out = extract_zip(z, tmp_path / "into" / "x")
    assert (tmp_path / "into" / "x" / "bin" / "tool.exe").read_bytes() == b"MZ" * 50
    assert {p.name for p in out} >= {"tool.exe", "readme.txt"}


def test_extract_rejects_traversal_and_bombs(tmp_path):
    into = tmp_path / "into"
    bad_names = [
        "../../evil.exe", "/abs.txt", "C:/win.txt", "C:evil.txt",
        "a\\..\\..\\evil.txt", "\\\\srv\\share\\x", "ok/../../e.txt", "a:b",
    ]
    for i, name in enumerate(bad_names):
        z = make_zip(tmp_path / f"b{i}.zip", [("fine.txt", b"x"), (name, b"x")])
        with pytest.raises(ExtractError):
            extract_zip(z, into)
    assert not into.exists() or not any(into.rglob("*"))
    assert not (tmp_path.parent / "evil.exe").exists()
    # symlink member
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    z = make_zip(tmp_path / "sym.zip", [(info, b"target")])
    with pytest.raises(ExtractError, match="symlink"):
        extract_zip(z, into)
    # bomb by ratio
    z = make_zip(tmp_path / "bomb.zip", [("zeros.bin", bytes(20 * 1024 * 1024))])
    with pytest.raises(ExtractError, match="too large"):
        extract_zip(z, into)
    # too many members
    z = make_zip(tmp_path / "many.zip", [(f"f{i}", b"") for i in range(20_001)])
    with pytest.raises(ExtractError, match="members"):
        extract_zip(z, into)
    assert not into.exists() or not any(into.rglob("*"))


def test_extract_cap_enforced_while_writing(tmp_path, monkeypatch):
    from primer.setup import extract as ex

    z = make_zip(tmp_path / "a.zip", [("a.bin", bytes(1_000_000)), ("b.bin", bytes(1_000_000))])
    monkeypatch.setattr(ex, "_cap_for", lambda archive_size: 1_500_000)
    monkeypatch.setattr(ex, "_declared_cap_for", lambda archive_size: 10**12)
    with pytest.raises(ExtractError):
        extract_zip(z, tmp_path / "into")
    assert not list((tmp_path / "into").rglob("*.bin"))


def test_extract_refuses_non_empty_target_and_keeps_files(tmp_path):
    into = tmp_path / "into"
    into.mkdir()
    (into / "mine.txt").write_text("keep")
    z = make_zip(tmp_path / "a.zip", [("x.txt", b"x")])
    with pytest.raises(ExtractError, match="not empty"):
        extract_zip(z, into)
    assert (into / "mine.txt").read_text() == "keep" and not (into / "x.txt").exists()


def test_extract_refuses_windows_reserved_names(tmp_path):
    for i, name in enumerate(["CON", "nul.txt", "a/Com1.dat", "lpt9", "dir./f", "trail ", "x/aux.tar.gz"]):
        z = make_zip(tmp_path / f"r{i}.zip", [(name, b"x")])
        with pytest.raises(ExtractError):
            extract_zip(z, tmp_path / f"into{i}")
    z = make_zip(tmp_path / "okname.zip", [("console.txt", b"x"), ("com10.txt", b"x")])
    assert len(extract_zip(z, tmp_path / "intook")) == 2


def test_extract_forged_declared_size_fails_cleanly(tmp_path):
    z = make_zip(tmp_path / "f.zip", [("a.bin", bytes(1_000_000)), ("b.bin", b"hello" * 100)])
    raw = bytearray(z.read_bytes())
    i = raw.index(b"PK\x01\x02")  # first central-directory entry: forge its declared size to 1 byte
    raw[i + 24:i + 28] = (1).to_bytes(4, "little")
    z.write_bytes(bytes(raw))
    into = tmp_path / "into"
    # the declared total now passes the up-front cap, so this fails in the extraction loop itself
    with pytest.raises(ExtractError, match="extraction failed"):
        extract_zip(z, into)
    assert not any(p.is_file() for p in into.rglob("*"))


def test_extract_skip_symlinks_leaves_them_out(tmp_path):
    link = zipfile.ZipInfo("fonts/alias.pbf")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    z = make_zip(tmp_path / "f.zip", [("fonts/real.pbf", b"glyphs"), (link, b"real.pbf")])
    with pytest.raises(ExtractError, match="symlink"):
        extract_zip(z, tmp_path / "strict")
    out = extract_zip(z, tmp_path / "into", skip_symlinks=True)
    assert [p.name for p in out] == ["real.pbf"]
    assert (tmp_path / "into" / "fonts" / "real.pbf").read_bytes() == b"glyphs"
    assert not (tmp_path / "into" / "fonts" / "alias.pbf").exists()


def test_extract_cancel_stops_and_cleans_up(tmp_path):
    import threading

    from primer.setup.extract import ExtractCancelled

    z = make_zip(tmp_path / "a.zip", [("a.txt", b"a"), ("b.txt", b"b")])
    ev = threading.Event()
    ev.set()
    with pytest.raises(ExtractCancelled):
        extract_zip(z, tmp_path / "into", cancel=ev)
    assert not any((tmp_path / "into").rglob("*"))
