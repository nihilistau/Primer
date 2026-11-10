import gzip
import math
import random
import xml.etree.ElementTree as ET

import pytest

from primer.book import mvt, plate
from primer.book.mvt import MVTError
from primer.maps.pmtiles import Archive, write_archive, zxy_to_tileid

SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" width="800" height="500">'


# ── a tiny MVT encoder ────────────────────────────────────────────────────────────────────

def _uv(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        out.append(c | 0x80 if v else c)
        if not v:
            return bytes(out)


def _key(field, wt):
    return _uv(field << 3 | wt)


def _ld(field, payload):
    return _key(field, 2) + _uv(len(payload)) + payload


def _zz(n):
    return (n << 1) ^ (n >> 63)


def _geom(rings, close):
    cmds, cx, cy = [], 0, 0
    for ring in rings:
        x, y = ring[0]
        cmds += [1 | 1 << 3, _zz(x - cx), _zz(y - cy)]
        cx, cy = x, y
        cmds.append(2 | (len(ring) - 1) << 3)
        for x, y in ring[1:]:
            cmds += [_zz(x - cx), _zz(y - cy)]
            cx, cy = x, y
        if close:
            cmds.append(7 | 1 << 3)
    return b"".join(_uv(c) for c in cmds)


def _feature(gtype, rings):
    return _key(1, 0) + _uv(7) + _key(3, 0) + _uv(gtype) + _ld(4, _geom(rings, gtype == 3))


def _layer(name, feats, extent=4096):
    body = _key(15, 0) + _uv(2) + _ld(1, name.encode())
    body += b"".join(_ld(2, f) for f in feats)
    body += _ld(3, b"kind") + _ld(4, _ld(1, b"ocean"))  # keys and values, ignored by the drawing
    return body + _key(5, 0) + _uv(extent)


def _tile(layers):
    return b"".join(_ld(3, _layer(name, feats, *rest)) for name, feats, *rest in layers)


SQUARE = [(256, 256), (3840, 256), (3840, 3840), (256, 3840)]
LAKE = [(1000, 1000), (1500, 1000), (1500, 1500), (1000, 1500)]
BORDER = [(0, 2048), (4096, 2100)]


def _land_tile():
    return _tile([("earth", [_feature(3, [SQUARE])]),
                  ("water", [_feature(3, [LAKE])]),
                  ("boundaries", [_feature(2, [BORDER])]),
                  ("roads", [_feature(2, [[(0, 0), (4096, 4096)]])])])


def _archive(tmp_path, body_for, maxz=3, name="a.pmtiles"):
    p = tmp_path / name
    tiles = [(zxy_to_tileid(z, x, y), body_for(z, x, y))
             for z in range(maxz + 1) for x in range(2 ** z) for y in range(2 ** z)]
    write_archive(p, tiles, {}, min_zoom=0, max_zoom=maxz, bounds=(-180.0, -85.0, 180.0, 85.0),
                  center=(0.0, 0.0, 0))
    return Archive(p)


def _gz(b):
    return gzip.compress(b, mtime=0)


def _places(*pts):
    return [{"n": i + 1, "name": f"Place {i + 1}", "what": "a town", "lat": lat, "lon": lon}
            for i, (lat, lon) in enumerate(pts)]


WALES = _places((53.14, -4.27), (52.86, -4.11))


def _parse(svg):
    assert svg.startswith(SVG_OPEN)
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("viewBox") == "0 0 800 500"
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    return root


# ── mvt ───────────────────────────────────────────────────────────────────────────────────

def test_decode_roundtrip():
    data = _tile([("earth", [_feature(3, [SQUARE, LAKE])], 4096),
                  ("boundaries", [_feature(2, [BORDER])], 512)])
    out = mvt.decode(data)
    assert set(out) == {"earth", "boundaries"}
    (poly,) = out["earth"]
    assert poly["type"] == 3 and poly["extent"] == 4096
    assert poly["rings"] == [[tuple(map(float, p)) for p in SQUARE],
                             [tuple(map(float, p)) for p in LAKE]]
    (line,) = out["boundaries"]
    assert line["type"] == 2 and line["extent"] == 512
    assert line["rings"] == [[(0.0, 2048.0), (4096.0, 2100.0)]]
    # only the asked-for layers are decoded
    assert set(mvt.decode(data, layers={"earth"})) == {"earth"}


def test_decode_rejects_garbage_bounded():
    for seed in range(5):
        with pytest.raises(MVTError):
            mvt.decode(random.Random(seed).randbytes(500))
    with pytest.raises(MVTError):  # a length varint 11 bytes long
        mvt.decode(b"\x1a" + b"\xff" * 10 + b"\x01")
    with pytest.raises(MVTError):
        mvt.decode(b"\x00" * (6 * 1024 * 1024))
    with pytest.raises(MVTError):  # a length running past the end
        mvt.decode(b"\x1a\x7f\x0a")
    many = [(i % 4000, (i * 7) % 4000) for i in range(200_001)]
    with pytest.raises(MVTError):  # more than 200 000 points
        mvt.decode(_tile([("earth", [_feature(2, [many])])]))


# ── plate ─────────────────────────────────────────────────────────────────────────────────

def test_plate_from_archive(tmp_path):
    arch = _archive(tmp_path, lambda z, x, y: _gz(_land_tile()))
    try:
        svg = plate.map_plate(WALES, arch)
    finally:
        arch.close()
    root = _parse(svg)
    assert "Drawn without the world map" not in svg
    assert "<path" in svg or "<polygon" in svg
    assert f'<path fill="{plate._LAND}"' in svg  # the earth layer is drawn, as land
    ns = "{http://www.w3.org/2000/svg}"
    assert len(root.findall(f".//{ns}circle")) == 2
    assert [t.text for t in root.findall(f".//{ns}text")] == ["1", "2"]
    assert len(svg.encode()) < 300_000
    # a path to the archive works too
    assert plate.map_plate(WALES, tmp_path / "a.pmtiles") == svg


def test_plate_plain_without_archive_or_on_error(tmp_path):
    svg = plate.map_plate(WALES, None)
    _parse(svg)
    assert "Drawn without the world map" in svg
    assert svg.count("<circle") == 2
    bad = _archive(tmp_path, lambda z, x, y: b"not gzip at all", name="bad.pmtiles")
    try:
        svg = plate.map_plate(WALES, bad)
    finally:
        bad.close()
    _parse(svg)
    assert "Drawn without the world map" in svg
    junk = _archive(tmp_path, lambda z, x, y: _gz(b"\x1a\xff\xff\xff\x0f"), name="junk.pmtiles")
    try:
        assert "Drawn without the world map" in plate.map_plate(WALES, junk)
    finally:
        junk.close()
    assert "Drawn without the world map" in plate.map_plate(WALES, tmp_path / "missing.pmtiles")


def test_plate_edge_frames(tmp_path):
    cases = [_places((51.5, -0.12)),
             _places((10.0, 179.0), (12.0, -179.0)),
             _places((89.0, 20.0), (70.0, 25.0)),
             _places((40.0, 10.0), (40.0, 10.0), (40.0, 10.0))]
    arch = _archive(tmp_path, lambda z, x, y: _gz(_land_tile()))
    try:
        for places in cases:
            for a in (None, arch):
                _parse(plate.map_plate(places, a))
    finally:
        arch.close()
    # across the antimeridian the frame is small, not the whole world
    f = plate.frame(cases[1])
    assert f.x1 - f.x0 < 0.05
    # one place: centred on it, at least one zoom-7 tile wide, chosen at zoom 7
    f = plate.frame(cases[0])
    cx = (-0.12 + 180) / 360
    assert (f.x0 + f.x1) / 2 == pytest.approx(cx)
    assert f.x1 - f.x0 >= 1 / 128 and f.zoom == 7
    # lat 89 is clamped: the frame stays inside the Mercator world
    f = plate.frame(cases[2])
    assert f.y0 >= 0 and f.y1 <= 1
    # everything is wider than a zoom-7 tile and uses at most 16 tiles
    for places in cases:
        f = plate.frame(places)
        n = 2 ** f.zoom
        tiles = (math.floor(f.x1 * n) - math.floor(f.x0 * n) + 1) * \
                (math.floor(min(f.y1, 1 - 1e-12) * n) - math.floor(f.y0 * n) + 1)
        assert tiles <= 16


def test_plate_size_cap(tmp_path):
    k = 50_000
    circle = [(round(2048 + 1500 * math.cos(2 * math.pi * i / k)),
               round(2048 + 1500 * math.sin(2 * math.pi * i / k))) for i in range(k)]
    body = _gz(_tile([("earth", [_feature(3, [circle])])]))
    arch = _archive(tmp_path, lambda z, x, y: body, maxz=0)
    wide = _places((60.0, -90.0), (-60.0, 90.0))
    try:
        assert len(mvt.decode(gzip.decompress(body))["earth"][0]["rings"][0]) == k
        svg = plate.map_plate(wide, arch, max_bytes=20_000)
        _parse(svg)
        assert len(svg.encode()) <= 20_000
        assert "Drawn without the world map" not in svg  # simplified, not given up
        rnd = random.Random(3)
        noise = [(rnd.randrange(4096), rnd.randrange(4096)) for _ in range(k)]
        body2 = _gz(_tile([("earth", [_feature(3, [noise])])]))
        arch2 = _archive(tmp_path, lambda z, x, y: body2, maxz=0, name="noise.pmtiles")
        try:
            svg = plate.map_plate(wide, arch2, max_bytes=20_000)
        finally:
            arch2.close()
        _parse(svg)
        assert len(svg.encode()) <= 20_000
    finally:
        arch.close()


def test_plate_has_no_text_from_places(tmp_path):
    places = [{"n": 1, "name": "<script>", "what": "<script>alert(1)</script>",
               "lat": 53.14, "lon": -4.27}]
    arch = _archive(tmp_path, lambda z, x, y: _gz(_land_tile()))
    try:
        for a in (None, arch):
            svg = plate.map_plate(places, a)
            _parse(svg)
            assert "script" not in svg.lower()
    finally:
        arch.close()


# ── fix round 1 ───────────────────────────────────────────────────────────────────────────

NS = "{http://www.w3.org/2000/svg}"


def _circles(svg):
    return [(float(c.get("cx")), float(c.get("cy")), float(c.get("r")))
            for c in _parse(svg).findall(f".//{NS}circle")]


def _labels(svg):
    return [t.text for t in _parse(svg).findall(f".//{NS}text") if t.text != plate.PLAIN_NOTE]


def _circle_tile(k):
    ring = [(round(2048 + 1500 * math.cos(2 * math.pi * i / k)),
             round(2048 + 1500 * math.sin(2 * math.pi * i / k))) for i in range(k)]
    return _gz(_tile([("earth", [_feature(3, [ring])])]))


def test_decode_counts_commands_and_checks_moveto_and_type():
    # a MoveTo then 300 000 ClosePaths: no points, but every command is counted
    geom = _uv(1 | 1 << 3) + _uv(0) + _uv(0) + _uv(7 | 1 << 3) * 300_000
    feat = _key(3, 0) + _uv(3) + _ld(4, geom)
    with pytest.raises(MVTError):
        mvt.decode(_tile([("earth", [feat])]))
    # an empty MoveTo (count 0) in a line, and a MoveTo of two points in a polygon
    for gtype, cmd in ((2, _uv(1)), (3, _uv(1 | 2 << 3) + _uv(0) * 4)):
        bad = _key(3, 0) + _uv(gtype) + _ld(4, cmd)
        with pytest.raises(MVTError):
            mvt.decode(_tile([("earth", [bad])]))
    # a multipoint (a POINT feature, one MoveTo of two points) is allowed by the spec
    pts = _key(3, 0) + _uv(1) + _ld(4, _uv(1 | 2 << 3) + _uv(_zz(5)) + _uv(_zz(6))
                                    + _uv(2) + _uv(2))
    assert mvt.decode(_tile([("places", [pts])]))["places"][0]["rings"] == [[(5.0, 6.0)],
                                                                             [(6.0, 7.0)]]
    with pytest.raises(MVTError):  # a feature type outside 0..3
        mvt.decode(_tile([("earth", [_key(3, 0) + _uv(4)])]))


def test_decode_shared_budget():
    big = [(i % 4000, (i * 7) % 4000) for i in range(120_000)]
    data = _tile([("earth", [_feature(2, [big])])])
    mvt.decode(data)
    mvt.decode(data)  # each alone is under the cap
    budget = mvt.Budget()
    mvt.decode(data, budget=budget)
    with pytest.raises(MVTError):  # together they are not
        mvt.decode(data, budget=budget)


def test_plate_point_cap_is_per_plate(tmp_path):
    body = _circle_tile(120_000)
    p = tmp_path / "z1.pmtiles"  # z1 only, so there is no lower zoom to step down to
    write_archive(p, [(zxy_to_tileid(1, x, y), body) for x in range(2) for y in range(2)], {},
                  min_zoom=1, max_zoom=1, bounds=(-180.0, -85.0, 180.0, 85.0),
                  center=(0.0, 0.0, 1))
    arch = Archive(p)
    one = _places((40.0, -100.0), (30.0, -80.0))  # one z1 tile
    two = _places((10.0, -60.0), (10.0, 60.0))  # several z1 tiles
    try:
        assert plate.frame(one, max_zoom=1).zoom == 1
        assert plate.PLAIN_NOTE not in plate.map_plate(one, arch)
        assert plate.PLAIN_NOTE in plate.map_plate(two, arch)
    finally:
        arch.close()


def test_plate_still_too_big_is_plain(tmp_path):
    body = _circle_tile(50_000)
    arch = _archive(tmp_path, lambda z, x, y: body, maxz=0)
    try:
        svg = plate.map_plate(_places((60.0, -90.0), (-60.0, 90.0)), arch, max_bytes=1)
    finally:
        arch.close()
    _parse(svg)
    assert plate.PLAIN_NOTE in svg


def test_plate_gzip_bomb_and_huge_tile(tmp_path, monkeypatch):
    bomb = _gz(b"\x00" * (6 * 1024 * 1024))
    assert len(bomb) < 100_000
    huge = b"\x1a\x00" * (3 * 1024 * 1024)  # stored uncompressed, over 5 MB
    calls = []
    real = plate.decode
    monkeypatch.setattr(plate, "decode", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    a1 = _archive(tmp_path, lambda z, x, y: bomb, maxz=0, name="bomb.pmtiles")
    p2 = tmp_path / "huge.pmtiles"
    write_archive(p2, [(0, huge)], {}, tile_compression=1, min_zoom=0, max_zoom=0,
                  bounds=(-180.0, -85.0, 180.0, 85.0), center=(0.0, 0.0, 0))
    a2 = Archive(p2)
    try:
        for a in (a1, a2):
            svg = plate.map_plate(WALES, a)
            _parse(svg)
            assert plate.PLAIN_NOTE in svg
    finally:
        a1.close()
        a2.close()
    assert calls == []  # both refused before decoding


def test_plate_every_tile_missing_is_plain(tmp_path):
    p = tmp_path / "sparse.pmtiles"
    write_archive(p, [(0, _gz(_land_tile()))], {}, min_zoom=0, max_zoom=3,
                  bounds=(-180.0, -85.0, 180.0, 85.0), center=(0.0, 0.0, 0))
    with Archive(p) as a:
        svg = plate.map_plate(WALES, a)  # drawn at z3, where there are no tiles
    _parse(svg)
    assert plate.PLAIN_NOTE in svg


def test_plate_odd_places(tmp_path):
    good = {"n": 1, "name": "x", "what": "", "lat": 53.14, "lon": -4.27}
    odd = [None, "x", 7, ["lat", 1], {"lat": float("nan"), "lon": 0.0},
           {"lat": 10.0, "lon": float("inf")}, {"lat": "far", "lon": 1}, {"lon": 3.0}, {"lat": 10**5000, "lon": 0}, good]
    arch = _archive(tmp_path, lambda z, x, y: _gz(_land_tile()))
    try:
        for a in (None, arch):
            assert len(_circles(plate.map_plate(odd, a))) == 1
            assert _circles(plate.map_plate(odd[:-1], a)) == []  # nothing located at all
    finally:
        arch.close()


def test_plate_pin_numbers():
    ns = [float("inf"), float("nan"), 10 ** 5000, 1e300, True, -3, 0, 10_000, "5", 2.0, 9999, 7,
          None]
    places = [{"n": n, "lat": 50.0 + i * 0.01, "lon": 0.0} for i, n in enumerate(ns)]
    svg = plate.map_plate(places, None)
    assert _labels(svg) == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "9999", "7", "13"]


def test_plate_pins_stay_on_the_plate(tmp_path):
    cases = [_places((89.0, 20.0)), _places((-89.0, 20.0)),
             _places((89.0, -179.9), (-89.0, 179.9)), _places((89.0, 0.0), (70.0, 5.0))]
    arch = _archive(tmp_path, lambda z, x, y: _gz(_land_tile()))
    try:
        for places in cases:
            for a in (None, arch):
                circles = _circles(plate.map_plate(places, a))
                assert len(circles) == len(places)
                for cx, cy, r in circles:
                    edge = r + 0.75  # half the stroke
                    assert edge <= cx <= 800 - edge and edge <= cy <= 500 - edge
    finally:
        arch.close()


def test_plate_at_most_300_pins():
    places = [{"n": i + 1, "lat": (i % 20) * 3.0, "lon": (i // 20) * 3.0} for i in range(400)]
    assert len(_circles(plate.map_plate(places, None))) == plate.MAX_PINS == 300


def test_plate_steps_down_a_zoom_when_over_budget(tmp_path, monkeypatch):
    fr = plate.frame(WALES)
    assert fr.zoom == 7
    dense = _circle_tile(6_000)  # over the (lowered) budget on its own
    light = _gz(_land_tile())

    def tiles(zooms):
        out = []
        for z in zooms:
            n = 2 ** z
            for x in range(math.floor(fr.x0 * n), math.floor(fr.x1 * n) + 1):
                for y in range(math.floor(fr.y0 * n), math.floor(fr.y1 * n) + 1):
                    out.append((zxy_to_tileid(z, x, y), dense if z == 7 else light))
        return out

    kw = dict(bounds=(-180.0, -85.0, 180.0, 85.0), center=(0.0, 0.0, 0))
    write_archive(tmp_path / "steps.pmtiles", tiles((6, 7)), {}, min_zoom=6, max_zoom=7, **kw)
    write_archive(tmp_path / "z7.pmtiles", tiles((7,)), {}, min_zoom=7, max_zoom=7, **kw)
    monkeypatch.setattr(mvt, "MAX_POINTS", 5_000)
    svg = plate.map_plate(WALES, tmp_path / "steps.pmtiles")
    _parse(svg)
    assert plate.PLAIN_NOTE not in svg  # drawn from z6
    assert f'<path fill="{plate._LAND}"' in svg
    # with no lower zoom to step down to, it is the plain plate
    assert plate.PLAIN_NOTE in plate.map_plate(WALES, tmp_path / "z7.pmtiles")
