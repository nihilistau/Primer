import gzip
import json
import random
import struct
import threading

import pytest

from primer.maps.pmtiles import (Archive, PMTilesError, parse_directory, tileid_to_zxy,
                                 write_archive, zxy_to_tileid)

_KW = dict(min_zoom=0, max_zoom=4, bounds=(-180.0, -85.0, 180.0, 85.0), center=(0.0, 0.0, 2))


def _all_tiles(maxz):
    return [(z, x, y) for z in range(maxz + 1) for x in range(2 ** z) for y in range(2 ** z)]


def test_tileid_roundtrip():
    vectors = {(0, 0, 0): 0, (1, 0, 0): 1, (1, 0, 1): 2, (1, 1, 1): 3, (1, 1, 0): 4}
    for zxy, tid in vectors.items():
        assert zxy_to_tileid(*zxy) == tid
        assert tileid_to_zxy(tid) == zxy
    # every tile of z0..5 maps onto exactly the ids of its zoom, in both directions
    for z in range(6):
        ids = sorted(zxy_to_tileid(z, x, y) for (zz, x, y) in _all_tiles(z) if zz == z)
        lo = (4 ** z - 1) // 3
        assert ids == list(range(lo, lo + 4 ** z))
    rnd = random.Random(5)
    for z in range(6, 13):
        for _ in range(200):
            x, y = rnd.randrange(2 ** z), rnd.randrange(2 ** z)
            tid = zxy_to_tileid(z, x, y)
            assert (4 ** z - 1) // 3 <= tid < (4 ** (z + 1) - 1) // 3
            assert tileid_to_zxy(tid) == (z, x, y)
    with pytest.raises(ValueError):
        zxy_to_tileid(2, 4, 0)
    with pytest.raises(ValueError):
        zxy_to_tileid(-1, 0, 0)


def test_write_then_read_tiles_and_metadata(tmp_path):
    p = tmp_path / "a.pmtiles"
    tiles = {zxy: gzip.compress(f"tile {zxy}".encode(), mtime=0) for zxy in _all_tiles(3)}
    del tiles[(3, 1, 1)]  # one absent tile
    # given out of id order: the writer sorts
    items = [(zxy_to_tileid(*zxy), b) for zxy, b in tiles.items()]
    items.reverse()
    write_archive(p, items, {"name": "test", "maxzoom": 3}, min_zoom=0, max_zoom=3,
                  bounds=(-10.5, -20.25, 30.0, 40.0), center=(1.5, 2.5, 2))
    raw = p.read_bytes()
    assert raw[:7] == b"PMTiles" and raw[7] == 3
    a = Archive(p)
    try:
        h = a.header
        assert (h["min_zoom"], h["max_zoom"]) == (0, 3)
        assert h["bounds"] == pytest.approx((-10.5, -20.25, 30.0, 40.0))
        assert h["center"] == pytest.approx((1.5, 2.5, 2))
        assert h["tile_type"] == 1 and h["tile_compression"] == 2
        assert h["clustered"] is True
        assert a.metadata() == {"name": "test", "maxzoom": 3}
        for zxy, b in tiles.items():
            assert a.tile(*zxy) == b
        assert a.tile(3, 1, 1) is None
        assert a.tile(4, 0, 0) is None
        # concurrent readers get the right bytes
        errors = []

        def read_all():
            for zxy, b in tiles.items():
                if a.tile(*zxy) != b:
                    errors.append(zxy)
        threads = [threading.Thread(target=read_all) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
    finally:
        a.close()
    # data is clustered: contents appear in tile-id order
    root_off, root_len = struct.unpack_from("<2Q", raw, 8)
    d = parse_directory(raw[root_off:root_off + root_len], 2)
    assert [e[0] for e in d] == sorted(e[0] for e in d)
    assert [e[3] for e in d] == sorted(e[3] for e in d)


def test_leaf_directories_when_many_tiles(tmp_path):
    p = tmp_path / "big.pmtiles"
    rnd = random.Random(7)
    # scattered ids and uneven sizes: a root directory for these can't fit in 16 KiB
    tids = sorted(rnd.sample(range(zxy_to_tileid(12, 0, 0), zxy_to_tileid(13, 0, 0)), 12000))
    body = lambda t: struct.pack("<Q", t) * (1 + t % 40)
    write_archive(p, ((t, body(t)) for t in tids), {}, tile_compression=1,
                  min_zoom=12, max_zoom=12, bounds=(-180, -85, 180, 85), center=(0, 0, 12))
    raw = p.read_bytes()
    root_off, root_len, _, _, leaf_off, leaf_len = struct.unpack_from("<6Q", raw, 8)
    assert root_off + root_len <= 16384
    assert leaf_len > 0
    root = parse_directory(raw[root_off:root_off + root_len], 2)
    assert root and all(run == 0 for _, run, _, _ in root)
    with Archive(p) as a:
        for t in tids[::37] + [tids[-1]]:
            assert a.tile(*tileid_to_zxy(t)) == body(t)
        missing = next(t for t in range(tids[0], tids[-1]) if t not in set(tids))
        assert a.tile(*tileid_to_zxy(missing)) is None
        assert a.header["addressed_tiles"] == 12000


def test_dedup_and_runs(tmp_path):
    p = tmp_path / "d.pmtiles"
    ocean = b"ocean" * 10
    tiles = [(t, ocean) for t in range(1, 21)] + [(21, b"land"), (22, ocean), (30, b"land")]
    write_archive(p, tiles, {}, **_KW)
    raw = p.read_bytes()
    root_off, root_len = struct.unpack_from("<2Q", raw, 8)
    data_len = struct.unpack_from("<Q", raw, 8 + 7 * 8)[0]
    addressed, entries, contents = struct.unpack_from("<3Q", raw, 8 + 8 * 8)
    assert data_len == len(ocean) + len(b"land")
    assert (addressed, contents) == (23, 2)
    d = parse_directory(raw[root_off:root_off + root_len], 2)
    assert entries == len(d) == 4
    assert d[0][:2] == (1, 20)  # one entry covers the run of 20 identical tiles
    assert d[2][3] == d[0][3]  # tile 22 points back at the first ocean bytes
    with Archive(p) as a:
        assert a.tile(*tileid_to_zxy(13)) == ocean
        assert a.tile(*tileid_to_zxy(30)) == b"land"
        assert a.tile(*tileid_to_zxy(25)) is None
    with pytest.raises(ValueError):
        write_archive(tmp_path / "dup.pmtiles", [(3, b"a"), (3, b"b")], {}, **_KW)


def test_pmtiles_rejects_bad_files(tmp_path):
    good = tmp_path / "g.pmtiles"
    write_archive(good, [(0, b"x")], {}, **_KW)
    raw = good.read_bytes()
    cases = {
        "magic": b"NOTILES" + raw[7:],
        "version": raw[:7] + bytes([2]) + raw[8:],
        "short": raw[:60],
        "empty": b"",
        "dir": raw[:140],  # header fine, the root directory cut off
    }
    for name, body in cases.items():
        p = tmp_path / f"{name}.pmtiles"
        p.write_bytes(body)
        with pytest.raises(PMTilesError):
            Archive(p)
    with pytest.raises(PMTilesError):
        Archive(tmp_path / "missing.pmtiles")
    # metadata that isn't JSON is a PMTilesError too, not a crash
    bad_meta = bytearray(raw)
    meta_off, meta_len = struct.unpack_from("<2Q", raw, 8 + 16)
    bad_meta[meta_off:meta_off + meta_len] = b"\xff" * meta_len
    (tmp_path / "meta.pmtiles").write_bytes(bytes(bad_meta))
    with Archive(tmp_path / "meta.pmtiles") as a:
        with pytest.raises(PMTilesError):
            a.metadata()


def test_metadata_is_gzip_json(tmp_path):
    p = tmp_path / "m.pmtiles"
    write_archive(p, [(0, b"x")], {"attribution": "© OSM"}, **_KW)
    raw = p.read_bytes()
    meta_off, meta_len = struct.unpack_from("<2Q", raw, 8 + 16)
    assert json.loads(gzip.decompress(raw[meta_off:meta_off + meta_len])) == {"attribution": "© OSM"}


# ── hand-built files: the reader checked independently of the writer ──────────────────────

from primer.maps import pmtiles as _pm  # noqa: E402


def _hand_built(tmp_path, root, leaves, data, *, data_len=None, trailing=b""):
    root_off = 127
    meta = gzip.compress(b"{}", mtime=0)
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    h = dict(root_offset=root_off, root_length=len(root), metadata_offset=meta_off,
             metadata_length=len(meta), leaf_offset=leaf_off, leaf_length=len(leaves),
             tile_data_offset=data_off, tile_data_length=len(data) if data_len is None else data_len,
             addressed_tiles=0, tile_entries=0, tile_contents=0, clustered=True,
             internal_compression=2, tile_compression=1, tile_type=1, min_zoom=0, max_zoom=12,
             bounds=(-180, -85, 180, 85), center=(0, 0, 0))
    p = tmp_path / "hand.pmtiles"
    p.write_bytes(_pm._pack_header(h) + root + meta + leaves + data + trailing)
    return p


def test_directory_byte_literal_offset_zero_follows_previous():
    # 4 entries: ids 0,1,2,5 (deltas 0,1,1,3); runs 1; lengths 10,20,30,5;
    # offsets stored 1 (=0), 0 (follows: 10), 0 (follows: 30), 101 (=100)
    raw = bytes([4, 0, 1, 1, 3, 1, 1, 1, 1, 10, 20, 30, 5, 1, 0, 0, 101])
    want = [(0, 1, 10, 0), (1, 1, 20, 10), (2, 1, 30, 30), (5, 1, 5, 100)]
    assert [tuple(e) for e in parse_directory(raw, 1)] == want
    assert [tuple(e) for e in parse_directory(gzip.compress(raw), 2)] == want
    # a multi-byte varint: id 300 = 0xAC 0x02
    assert parse_directory(bytes([1, 0xAC, 0x02, 1, 7, 1]), 1)[0] == (300, 1, 7, 0)


def test_leaf_lru_evicts_beyond_64(tmp_path):
    n = 70  # one tile per leaf directory, 70 leaves
    leaves, root, data = bytearray(), [], bytearray()
    for i in range(n):
        tid = 1000 + 10 * i
        blob = _pm.serialize_directory([(tid, 1, 4, len(data))])
        root.append((tid, 0, len(blob), len(leaves)))
        leaves += blob
        data += struct.pack("<I", tid)
    p = _hand_built(tmp_path, _pm.serialize_directory(root), bytes(leaves), bytes(data))
    with Archive(p) as a:
        for _ in range(2):
            for i in range(n):
                tid = 1000 + 10 * i
                assert a.tile(*tileid_to_zxy(tid)) == struct.pack("<I", tid)
                assert a.tile(*tileid_to_zxy(tid + 1)) is None
        assert len(a._leaves) == 64


def test_depth_two_leaf_chain(tmp_path):
    # root -> leaf -> leaf -> tiles
    inner = _pm.serialize_directory([(50, 3, 2, 0), (60, 1, 2, 2)])
    outer = _pm.serialize_directory([(50, 0, len(inner), 0)])
    root = _pm.serialize_directory([(50, 0, len(outer), len(inner))])
    p = _hand_built(tmp_path, root, inner + outer, b"abcd")
    with Archive(p) as a:
        got = [a.tile(*tileid_to_zxy(t)) for t in (50, 51, 52, 53, 60, 61, 49)]
    assert got == [b"ab", b"ab", b"ab", None, b"cd", None, None]


def test_tile_outside_data_section_is_an_error(tmp_path):
    root = _pm.serialize_directory([(0, 1, 4, 0), (1, 1, 8, 4)])
    # the second tile runs past tile_data_length, though the file has bytes there
    p = _hand_built(tmp_path, root, b"", b"tile", data_len=4, trailing=b"xxxxxxxxxxxx")
    with Archive(p) as a:
        assert a.tile(0, 0, 0) == b"tile"
        with pytest.raises(PMTilesError):
            a.tile(1, 0, 0)


def test_concurrent_reads_through_leaves(tmp_path):
    p = tmp_path / "c.pmtiles"
    start = zxy_to_tileid(12, 0, 0)
    tids = sorted(random.Random(3).sample(range(start, start + 4 ** 12), 12000))
    have = set(tids)
    body = lambda t: struct.pack("<Q", t) * (1 + t % 13)
    write_archive(p, ((t, body(t)) for t in tids), {}, tile_compression=1, min_zoom=12,
                  max_zoom=12, bounds=(-180, -85, 180, 85), center=(0, 0, 12))
    errors = []
    with Archive(p) as a:
        assert a.header["leaf_length"] > 0

        def worker(seed):
            rnd = random.Random(seed)
            for _ in range(3000):
                t = rnd.choice(tids)
                if a.tile(*tileid_to_zxy(t)) != body(t) or (t + 1 not in have and a.tile(*tileid_to_zxy(t + 1)) is not None):
                    errors.append(t)
        threads = [threading.Thread(target=worker, args=(s,)) for s in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []


def test_version_from_pyproject(tmp_path):
    import tomllib
    from pathlib import Path

    from primer import version
    from primer.maps import USER_AGENT
    root = Path(__file__).resolve().parent.parent
    want = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert version.VERSION == want
    assert USER_AGENT == f"Primer/{want} (https://github.com/nihilistau/Primer)"
    assert version.read_version(tmp_path / "missing.toml") == "dev"
    (tmp_path / "bad.toml").write_text("[project\n")
    assert version.read_version(tmp_path / "bad.toml") == "dev"
