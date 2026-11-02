import datetime
import http.server
import importlib.util
import math
import threading
from pathlib import Path

import pytest

from primer.maps.pmtiles import Archive, tileid_to_zxy, write_archive, zxy_to_tileid

_SPEC = importlib.util.spec_from_file_location(
    "map_extract", Path(__file__).resolve().parent.parent / "tools" / "map_extract.py")
mx = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mx)


def _content(z, x, y):
    return f"tile {z}/{x}/{y}".encode() * 3


def _source(tmp_path, maxz=4):
    p = tmp_path / "src.pmtiles"
    tiles = []
    for z in range(maxz + 1):
        for x in range(2 ** z):
            for y in range(2 ** z):
                # a couple of identical "ocean" tiles too, so dedup and runs are exercised
                body = b"ocean" if (z == 4 and x == 0) else _content(z, x, y)
                tiles.append((zxy_to_tileid(z, x, y), body))
    write_archive(p, tiles, {"name": "src", "maxzoom": maxz, "attribution": "OSM"}, min_zoom=0,
                  max_zoom=maxz, bounds=(-180, -85.05, 180, 85.05), center=(0.0, 0.0, 3))
    return p, dict(tiles)


def _fetcher(path, calls=None, fail_after=None):
    data = path.read_bytes()

    def fetch(offset, length):
        if calls is not None:
            calls.append((offset, length))
            if fail_after is not None and len(calls) > fail_after:
                raise RuntimeError("connection dropped")
        assert 0 <= offset and offset + length <= len(data)
        return data[offset:offset + length]
    return fetch


def _tile_bbox(z, x, y):
    n = 2 ** z
    lat = lambda t: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def _ids_in(path):
    with Archive(path) as a:
        found = {}
        for z in range(a.header["max_zoom"] + 1):
            for x in range(2 ** z):
                for y in range(2 ** z):
                    b = a.tile(z, x, y)
                    if b is not None:
                        found[(z, x, y)] = b
        return found, a.header, a.metadata()


def test_extract_maxzoom_and_bbox(tmp_path):
    src, tiles = _source(tmp_path)
    out = tmp_path / "z2.pmtiles"
    mx.extract(_fetcher(src), out, maxzoom=2, log=lambda *a: None)
    found, header, meta = _ids_in(out)
    expect = {tileid_to_zxy(t): b for t, b in tiles.items() if tileid_to_zxy(t)[0] <= 2}
    assert found == expect
    assert header["max_zoom"] == 2 and meta == {"name": "src", "maxzoom": 2, "attribution": "OSM"}
    assert header["center"][2] == 2

    bbox = (10.0, 10.0, 80.0, 50.0)
    out2 = tmp_path / "region.pmtiles"
    mx.extract(_fetcher(src), out2, maxzoom=2, bbox=bbox, region_maxzoom=4, log=lambda *a: None)
    found, header, meta = _ids_in(out2)
    W, S, E, N = bbox
    want = dict(expect)
    for z in (3, 4):
        for x in range(2 ** z):
            for y in range(2 ** z):
                w, s, e, n = _tile_bbox(z, x, y)
                if w < E and e > W and s < N and n > S:
                    want[(z, x, y)] = tiles[zxy_to_tileid(z, x, y)]
    assert len(want) > len(expect) + 4  # the bbox really adds z3-4 tiles
    assert found == want
    assert header["max_zoom"] == 4 and meta["maxzoom"] == 4
    assert not list(tmp_path.glob("*.part*"))


def test_extract_dry_run_writes_nothing(tmp_path):
    src, _ = _source(tmp_path, maxz=3)
    lines = []
    calls = []
    plan = mx.extract(_fetcher(src, calls), tmp_path / "x.pmtiles", maxzoom=3, dry_run=True,
                      log=lines.append)
    assert not (tmp_path / "x.pmtiles").exists()
    assert plan["tiles"] == 1 + 4 + 16 + 64 and plan["bytes"] > 0
    assert any("MB" in str(line) for line in lines)
    data_start = Archive(src).header["tile_data_offset"]
    assert all(off < data_start for off, _ in calls)  # no tile data was downloaded


def test_extract_resumes_part_file(tmp_path):
    src, tiles = _source(tmp_path)
    out = tmp_path / "r.pmtiles"
    calls = []
    with pytest.raises(RuntimeError):
        mx.extract(_fetcher(src, calls, fail_after=8), out, maxzoom=4, chunk=256, log=lambda *a: None)
    assert not out.exists()
    part = Path(str(out) + ".part")
    assert part.exists() and part.stat().st_size > 0
    have = part.stat().st_size
    calls2 = []
    mx.extract(_fetcher(src, calls2), out, maxzoom=4, chunk=256, log=lambda *a: None)
    data_start = Archive(src).header["tile_data_offset"]
    fetched = sum(n for off, n in calls2 if off >= data_start)
    fresh = []
    mx.extract(_fetcher(src, fresh), tmp_path / "fresh.pmtiles", maxzoom=4, chunk=256,
               log=lambda *a: None)
    full = sum(n for off, n in fresh if off >= data_start)
    assert fetched == full - have  # only what the part file lacked
    found, _, _ = _ids_in(out)
    assert found == {tileid_to_zxy(t): b for t, b in tiles.items()}
    assert not part.exists()


def test_extract_restarts_part_from_another_source(tmp_path):
    src, tiles = _source(tmp_path)
    out = tmp_path / "s.pmtiles"
    Path(str(out) + ".part").write_bytes(b"garbage from another run" * 10)
    mx.extract(_fetcher(src), out, maxzoom=3, log=lambda *a: None)
    found, _, _ = _ids_in(out)
    assert found == {tileid_to_zxy(t): b for t, b in tiles.items() if tileid_to_zxy(t)[0] <= 3}


def test_extract_rejects_non_pmtiles_source(tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"<html>not found</html>" * 20)
    with pytest.raises(mx.PMTilesError):
        mx.extract(_fetcher(bad), tmp_path / "o.pmtiles", maxzoom=2, log=lambda *a: None)
    assert not (tmp_path / "o.pmtiles").exists()


def test_default_source_steps_back_a_day():
    tried = []

    def probe(url):
        tried.append(url)
        return url.endswith("/20261030.pmtiles")
    url = mx.find_source(probe, today=datetime.date(2026, 11, 2))
    assert url == "https://build.protomaps.com/20261030.pmtiles"
    assert tried == ["https://build.protomaps.com/20261102.pmtiles",
                     "https://build.protomaps.com/20261101.pmtiles",
                     "https://build.protomaps.com/20261031.pmtiles", url]
    with pytest.raises(SystemExit):
        mx.find_source(lambda u: False, today=datetime.date(2026, 11, 2))
    # no date given: today in UTC (the build server's day), not the local day
    tried.clear()
    mx.find_source(lambda u: tried.append(u) or True)
    assert tried == [mx.BUILD_URL.format(datetime.datetime.now(datetime.timezone.utc).date())]


def test_help_mentions_disk_space(capsys):
    with pytest.raises(SystemExit):
        mx.main(["--help"])
    assert "free" in capsys.readouterr().out


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    data = b""
    seen: list = []
    ignore_range = False
    garble = 0  # this many next responses are a broken status line (http.client.BadStatusLine)

    def do_GET(self):
        self.seen.append((self.headers.get("Range"), self.headers.get("User-Agent")))
        rng = self.headers.get("Range", "")
        if self.ignore_range or not rng.startswith("bytes="):
            body, code = self.data, 200
        else:
            a, b = rng[6:].split("-")
            body, code = self.data[int(a):int(b) + 1], 206
        if type(self).garble > 0:
            type(self).garble -= 1
            self.wfile.write(b"garbage\r\n\r\n")
            self.close_connection = True
            return
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_cli_over_local_http(tmp_path):
    src, tiles = _source(tmp_path, maxz=3)
    _RangeHandler.data = src.read_bytes()
    _RangeHandler.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/src.pmtiles"
        out = tmp_path / "cli.pmtiles"
        assert mx.main(["--source", url, "--out", str(out), "--maxzoom", "1"]) == 0
        found, _, _ = _ids_in(out)
        assert found == {tileid_to_zxy(t): b for t, b in tiles.items() if tileid_to_zxy(t)[0] <= 1}
        assert all(r and r.startswith("bytes=") for r, _ in _RangeHandler.seen)
        assert all(ua == mx.USER_AGENT for _, ua in _RangeHandler.seen)
        assert mx.USER_AGENT.startswith("Primer/") and "github.com/nihilistau/Primer" in mx.USER_AGENT
        # a server that ignores Range (200 with the whole file) is refused, never read whole
        # an http.client.HTTPException (not an OSError) is retried too
        _RangeHandler.garble = 1
        assert mx.http_fetcher(url, retries=2, sleep=lambda s: None)(0, 127) == _RangeHandler.data[:127]
        _RangeHandler.ignore_range = True
        fetch = mx.http_fetcher(url, retries=1, sleep=lambda s: None)
        with pytest.raises(OSError):
            fetch(0, 127)
    finally:
        _RangeHandler.ignore_range = False
        _RangeHandler.garble = 0
        srv.shutdown()
        srv.server_close()


def test_cli_bbox_needs_region_maxzoom(capsys):
    with pytest.raises(SystemExit):
        mx.main(["--source", "http://127.0.0.1:1/x", "--out", "o", "--maxzoom", "2",
                 "--bbox", "1,2,3,4"])
    with pytest.raises(SystemExit):
        mx.main(["--source", "http://127.0.0.1:1/x", "--out", "o", "--maxzoom", "2",
                 "--bbox", "1,2,3", "--region-maxzoom", "5"])


def test_extract_locked_output_keeps_part_and_explains(tmp_path, monkeypatch):
    import os as _os
    src, _ = _source(tmp_path)
    out = tmp_path / "locked.pmtiles"

    def deny(a, b):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(_os, "replace", deny)
    with pytest.raises(PermissionError) as ei:
        mx.extract(_fetcher(src), out, maxzoom=2, log=lambda *a: None)
    msg = str(ei.value)
    assert "Can't replace" in msg and "locked.pmtiles" in msg
    assert "stop Primer first" in msg and "run the same command again" in msg
    assert Path(str(out) + ".part").exists()  # the downloaded data is kept
    monkeypatch.undo()
    mx.extract(_fetcher(src, []), out, maxzoom=2, log=lambda *a: None)
    assert out.exists()
