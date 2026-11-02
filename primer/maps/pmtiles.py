"""PMTiles v3: tile ids, a reader (Archive) and a writer (write_archive). Stdlib only.

Spec: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md. A file is a 127-byte
little-endian header, a root directory (inside the first 16 KiB), JSON metadata, optional leaf
directories and the tile data. A directory is varints: the entry count, then the tile ids
(delta-encoded), run lengths, lengths and offsets (0 = right after the previous entry, else
offset + 1). A run length of 0 marks a pointer to a leaf directory. Tile ids number the tiles
zoom by zoom, each zoom along a Hilbert curve.
"""
from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import os
import shutil
import struct
import threading
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, NamedTuple

HEADER_LEN = 127
ROOT_LIMIT = 16384  # the header and the root directory fit in the first 16 KiB
MAX_ZOOM = 26
NONE, GZIP = 1, 2  # compression codes this module can read (and write: gzip)
_LEAF_CACHE = 64
_MAX_DEPTH = 4  # root -> leaf -> leaf ...: deeper than this is a broken (or looping) file


class PMTilesError(Exception):
    pass


class Entry(NamedTuple):
    tile_id: int
    run_length: int  # 0: a leaf directory, not tiles
    length: int
    offset: int


def zoom_start(z: int) -> int:
    """The first tile id of zoom z: (4^z - 1) / 3."""
    return ((1 << (2 * z)) - 1) // 3


def _rotate(n: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def zxy_to_tileid(z: int, x: int, y: int) -> int:
    if not 0 <= z <= MAX_ZOOM:
        raise ValueError(f"zoom {z} out of range")
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError(f"tile {z}/{x}/{y} out of range")
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        x, y = _rotate(s, x, y, rx, ry)
        s >>= 1
    return zoom_start(z) + d


def tileid_to_zxy(tile_id: int) -> tuple[int, int, int]:
    if tile_id < 0:
        raise ValueError(f"tile id {tile_id} out of range")
    for z in range(MAX_ZOOM + 1):
        if tile_id < zoom_start(z + 1):
            t = tile_id - zoom_start(z)
            x = y = 0
            s = 1
            while s < (1 << z):
                rx = 1 & (t >> 1)
                ry = 1 & (t ^ rx)
                x, y = _rotate(s, x, y, rx, ry)
                x += s * rx
                y += s * ry
                t >>= 2
                s <<= 1
            return z, x, y
    raise ValueError(f"tile id {tile_id} out of range")


# ── encoding ──────────────────────────────────────────────────────────────────────────────


def _uvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        out.append(c | 0x80 if v else c)
        if not v:
            return bytes(out)


def _varints(b: bytes):
    i, n = 0, len(b)
    while i < n:
        v = s = 0
        while True:
            c = b[i]
            i += 1
            v |= (c & 0x7F) << s
            s += 7
            if c < 0x80:
                break
        yield v


def decompress(raw: bytes, compression: int) -> bytes:
    if compression == GZIP:
        return gzip.decompress(raw)
    if compression == NONE:
        return raw
    raise PMTilesError(f"unsupported internal compression {compression} (only none or gzip)")


def parse_directory(raw: bytes, compression: int = GZIP) -> list[Entry]:
    try:
        it = _varints(decompress(raw, compression))
        n = next(it)
        ids, last = [], 0
        for _ in range(n):
            last += next(it)
            ids.append(last)
        runs = [next(it) for _ in range(n)]
        lens = [next(it) for _ in range(n)]
        offs: list[int] = []
        for k in range(n):
            o = next(it)
            offs.append(offs[-1] + lens[k - 1] if o == 0 and k > 0 else o - 1)
    except PMTilesError:
        raise
    except (StopIteration, IndexError, EOFError, OSError, zlib.error) as e:
        raise PMTilesError(f"broken directory: {e or type(e).__name__}") from None
    if any(o < 0 for o in offs):
        raise PMTilesError("broken directory: negative offset")
    return [Entry(*e) for e in zip(ids, runs, lens, offs)]


def serialize_directory(entries: list, compression: int = GZIP) -> bytes:
    b = bytearray(_uvarint(len(entries)))
    last = 0
    for e in entries:
        b += _uvarint(e[0] - last)
        last = e[0]
    for e in entries:
        b += _uvarint(e[1])
    for e in entries:
        b += _uvarint(e[2])
    for k, e in enumerate(entries):
        follows = k > 0 and e[3] == entries[k - 1][3] + entries[k - 1][2]
        b += _uvarint(0 if follows else e[3] + 1)
    return gzip.compress(bytes(b), mtime=0) if compression == GZIP else bytes(b)


_SECTIONS = ("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset",
             "leaf_length", "tile_data_offset", "tile_data_length", "addressed_tiles",
             "tile_entries", "tile_contents")


def parse_header(raw: bytes) -> dict:
    if len(raw) < HEADER_LEN:
        raise PMTilesError(f"not a PMTiles file: truncated header ({len(raw)} of {HEADER_LEN} bytes)")
    if raw[:7] != b"PMTiles":
        raise PMTilesError("not a PMTiles file (bad magic)")
    if raw[7] != 3:
        raise PMTilesError(f"PMTiles version {raw[7]} is not supported (only version 3)")
    h = dict(zip(_SECTIONS, struct.unpack_from("<11Q", raw, 8)))
    clustered, icomp, tcomp, ttype, minz, maxz = struct.unpack_from("<6B", raw, 96)
    w, s, e, n = (v / 1e7 for v in struct.unpack_from("<4i", raw, 102))
    cz = raw[118]
    clon, clat = (v / 1e7 for v in struct.unpack_from("<2i", raw, 119))
    h.update(clustered=bool(clustered), internal_compression=icomp, tile_compression=tcomp,
             tile_type=ttype, min_zoom=minz, max_zoom=maxz, bounds=(w, s, e, n),
             center=(clon, clat, cz))
    return h


def _pack_header(h: dict) -> bytes:
    w, s, e, n = h["bounds"]
    clon, clat, cz = h["center"]
    e7 = lambda v: int(round(float(v) * 1e7))
    out = (b"PMTiles" + bytes([3]) + struct.pack("<11Q", *(h[k] for k in _SECTIONS))
           + struct.pack("<6B", int(h["clustered"]), h["internal_compression"], h["tile_compression"],
                         h["tile_type"], h["min_zoom"], h["max_zoom"])
           + struct.pack("<4i", e7(w), e7(s), e7(e), e7(n)) + struct.pack("<B", int(cz))
           + struct.pack("<2i", e7(clon), e7(clat)))
    assert len(out) == HEADER_LEN
    return out


# ── reader ────────────────────────────────────────────────────────────────────────────────


class _Dir:
    __slots__ = ("ids", "entries")

    def __init__(self, entries: list[Entry]):
        self.entries = entries
        self.ids = [e.tile_id for e in entries]

    def find(self, tile_id: int) -> Entry | None:
        i = bisect.bisect_right(self.ids, tile_id) - 1
        return self.entries[i] if i >= 0 else None


class Archive:
    """A PMTiles v3 file opened for reading. `tile()` is safe to call from several threads."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self._f = open(self.path, "rb")
        except OSError as e:
            raise PMTilesError(f"can't open {self.path.name}: {e.strerror or e}") from None
        self._lock = threading.Lock()
        self._leaves: OrderedDict[tuple[int, int], _Dir] = OrderedDict()
        self._meta: dict | None = None
        try:
            size = os.fstat(self._f.fileno()).st_size
            self.header = parse_header(self._f.read(HEADER_LEN))
            h = self.header
            for name in ("root", "metadata", "leaf", "tile_data"):
                if h[f"{name}_offset"] + h[f"{name}_length"] > size:
                    raise PMTilesError(f"truncated file: the {name.replace('_', ' ')} section "
                                       f"runs past the end ({size} bytes)")
            self._root = _Dir(parse_directory(self._read(h["root_offset"], h["root_length"]),
                                              h["internal_compression"]))
        except BaseException:
            self._f.close()
            raise

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._f.close()

    def _read(self, offset: int, length: int) -> bytes:
        with self._lock:
            self._f.seek(offset)
            b = self._f.read(length)
        if len(b) != length:
            raise PMTilesError(f"truncated file: wanted {length} bytes at {offset}, got {len(b)}")
        return b

    def metadata(self) -> dict:
        if self._meta is None:
            h = self.header
            raw = self._read(h["metadata_offset"], h["metadata_length"])
            try:
                meta = json.loads(decompress(raw, h["internal_compression"]) or b"{}")
            except (OSError, EOFError, zlib.error, ValueError) as e:
                raise PMTilesError(f"broken metadata: {e}") from None
            if not isinstance(meta, dict):
                raise PMTilesError("broken metadata: not a JSON object")
            self._meta = meta
        return dict(self._meta)

    def _leaf(self, offset: int, length: int) -> _Dir:
        key = (offset, length)
        with self._lock:
            d = self._leaves.get(key)
            if d is not None:
                self._leaves.move_to_end(key)
                return d
        h = self.header
        d = _Dir(parse_directory(self._read(h["leaf_offset"] + offset, length),
                                 h["internal_compression"]))
        with self._lock:
            self._leaves[key] = d
            while len(self._leaves) > _LEAF_CACHE:
                self._leaves.popitem(last=False)
        return d

    def _entry_for(self, tile_id: int) -> Entry | None:
        d = self._root
        for _ in range(_MAX_DEPTH):
            e = d.find(tile_id)
            if e is None:
                return None
            if e.run_length == 0:
                d = self._leaf(e.offset, e.length)
                continue
            return e if tile_id < e.tile_id + e.run_length else None
        raise PMTilesError("broken file: leaf directories nested too deep")

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """The tile's stored bytes (still compressed as the header says), or None if absent.
        ValueError for coordinates that don't name a tile."""
        tile_id = zxy_to_tileid(z, x, y)
        e = self._entry_for(tile_id)
        if e is None:
            return None
        if e.offset + e.length > self.header["tile_data_length"]:
            raise PMTilesError(f"broken file: tile {z}/{x}/{y} lies outside the tile data")
        return self._read(self.header["tile_data_offset"] + e.offset, e.length)


# ── writer ────────────────────────────────────────────────────────────────────────────────


def _build_directories(entries: list) -> tuple[bytes, bytes]:
    root = serialize_directory(entries)
    if HEADER_LEN + len(root) <= ROOT_LIMIT:
        return root, b""
    leaf_size = 4096
    while True:
        leaves, root_entries = bytearray(), []
        for i in range(0, len(entries), leaf_size):
            chunk = entries[i:i + leaf_size]
            blob = serialize_directory(chunk)
            root_entries.append((chunk[0][0], 0, len(blob), len(leaves)))
            leaves += blob
        root = serialize_directory(root_entries)
        if HEADER_LEN + len(root) <= ROOT_LIMIT:
            return root, bytes(leaves)
        leaf_size *= 2


def _copy_range(src, dst, offset: int, length: int) -> None:
    src.seek(offset)
    while length > 0:
        b = src.read(min(length, 1 << 20))
        if not b:
            raise OSError("temporary tile data ended early")
        dst.write(b)
        length -= len(b)


def write_archive(path, tiles: Iterable[tuple[int, bytes]], metadata: dict, *, tile_type: int = 1,
                  tile_compression: int = GZIP, min_zoom: int, max_zoom: int,
                  bounds: tuple[float, float, float, float],
                  center: tuple[float, float, int]) -> None:
    """Write a clustered PMTiles v3 file: tile data in tile-id order, identical tiles stored once,
    runs of identical neighbours as one entry, leaf directories when the root won't fit.
    `tiles` may come in any order; the data streams through a temporary file next to `path`, so
    a large archive needn't fit in memory. The file appears at `path` only when complete."""
    path = Path(path)
    tmp_data = path.with_name(path.name + ".data.tmp")
    tmp_sorted = path.with_name(path.name + ".sorted.tmp")
    tmp_out = path.with_name(path.name + ".tmp")
    try:
        entries: list[tuple[int, int, int]] = []  # (tile id, data offset, length)
        seen: dict[bytes, tuple[int, int]] = {}
        ordered, last, pos = True, -1, 0
        with open(tmp_data, "w+b") as df:
            for tile_id, data in tiles:
                if not isinstance(tile_id, int) or tile_id < 0:
                    raise ValueError(f"bad tile id {tile_id!r}")
                if tile_id <= last:
                    if tile_id == last:
                        raise ValueError(f"tile id {tile_id} given twice")
                    ordered = False
                last = max(last, tile_id)
                key = hashlib.sha256(data).digest()
                hit = seen.get(key)
                if hit is None:
                    df.write(data)
                    hit = seen[key] = (pos, len(data))
                    pos += len(data)
                entries.append((tile_id, hit[0], hit[1]))
            data_file = tmp_data
            if not ordered:
                # re-lay the data in tile-id order, so the archive is clustered
                entries.sort()
                for a, b in zip(entries, entries[1:]):
                    if a[0] == b[0]:
                        raise ValueError(f"tile id {a[0]} given twice")
                remap: dict[int, int] = {}
                pos = 0
                with open(tmp_sorted, "wb") as sf:
                    for _, off, ln in entries:
                        if off not in remap:
                            remap[off] = pos
                            _copy_range(df, sf, off, ln)
                            pos += ln
                entries = [(t, remap[off], ln) for t, off, ln in entries]
                data_file = tmp_sorted
        runs: list[list[int]] = []  # [tile id, run length, length, offset]
        for tile_id, off, ln in entries:
            r = runs[-1] if runs else None
            if r and r[0] + r[1] == tile_id and r[3] == off and r[2] == ln:
                r[1] += 1
            else:
                runs.append([tile_id, 1, ln, off])
        root, leaves = _build_directories(runs)
        meta = gzip.compress(json.dumps(metadata).encode("utf-8"), mtime=0)
        root_off = HEADER_LEN
        meta_off = root_off + len(root)
        leaf_off = meta_off + len(meta)
        data_off = leaf_off + len(leaves)
        header = _pack_header({
            "root_offset": root_off, "root_length": len(root),
            "metadata_offset": meta_off, "metadata_length": len(meta),
            "leaf_offset": leaf_off, "leaf_length": len(leaves),
            "tile_data_offset": data_off, "tile_data_length": pos,
            "addressed_tiles": len(entries), "tile_entries": len(runs), "tile_contents": len(seen),
            "clustered": True, "internal_compression": GZIP, "tile_compression": tile_compression,
            "tile_type": tile_type, "min_zoom": min_zoom, "max_zoom": max_zoom,
            "bounds": bounds, "center": center,
        })
        with open(tmp_out, "wb") as out:
            out.write(header)
            out.write(root)
            out.write(meta)
            out.write(leaves)
            with open(data_file, "rb") as df:
                shutil.copyfileobj(df, out, 1 << 20)
        try:
            os.replace(tmp_out, path)
        except PermissionError as e:  # Windows: the file is open in the running Primer
            raise PermissionError(
                f"Can't replace {path}: it is open in another program (stop Primer first). "
                "The downloaded data is kept; run the same command again to finish without "
                "downloading.") from e
    finally:
        for p in (tmp_data, tmp_sorted, tmp_out):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
