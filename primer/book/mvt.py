"""Mapbox Vector Tile (v2) geometry reader. Stdlib only.

Spec: https://github.com/mapbox/vector-tile-spec/tree/master/2.1. A tile is a protobuf message
of layers (field 3); a layer has a name (1), features (2), keys (3), values (4), an extent (5)
and a version (15); a feature has an id (1), tags (2), a type (3) and a packed geometry (4).
Geometry is a run of command integers (id in the low 3 bits, count above: MoveTo 1, LineTo 2,
ClosePath 7), each MoveTo/LineTo followed by zigzag-encoded deltas from the cursor.

Only what a map plate needs is read: layer names, extents, feature types and geometry. Tags,
keys and values are skipped. The reader is bounded so a hostile tile can't exhaust memory or
time: at most 5 MB of (decompressed) input, 200 000 points and geometry commands in all, and
varints of at most 10 bytes. As the spec says, a MoveTo moves to one point (count 1),
except in a POINT feature, where it may list several.
"""
from __future__ import annotations

MAX_BYTES = 5 * 1024 * 1024
MAX_POINTS = 200_000
_MAX_VARINT = 10


class MVTError(Exception):
    pass


class BudgetError(MVTError):
    """Too many points (or geometry commands): the tile may be fine, just too detailed."""


def _varint(b: bytes, i: int, end: int) -> tuple[int, int]:
    v = s = 0
    for k in range(_MAX_VARINT):
        if i >= end:
            raise MVTError("truncated varint")
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        if c < 0x80:
            return v, i
        s += 7
    raise MVTError("varint longer than 10 bytes")


def _fields(b: bytes, i: int, end: int):
    """Yield (field, wire type, value) over b[i:end]. A length-delimited value is a (start, stop)
    pair; the others are ints (fixed-width ones are skipped and given as 0)."""
    while i < end:
        key, i = _varint(b, i, end)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise MVTError("field number 0")
        if wt == 0:
            v, i = _varint(b, i, end)
            yield field, wt, v
        elif wt == 2:
            n, i = _varint(b, i, end)
            if n > end - i:
                raise MVTError("length runs past the end")
            yield field, wt, (i, i + n)
            i += n
        elif wt == 1 or wt == 5:
            w = 8 if wt == 1 else 4
            if w > end - i:
                raise MVTError("truncated fixed-width field")
            i += w
            yield field, wt, 0
        else:
            raise MVTError(f"unsupported wire type {wt}")


class Budget:
    """Points (and geometry commands) decoded so far. Share one across several `decode` calls
    to cap them together, as a map plate does for all its tiles."""
    __slots__ = ("points",)

    def __init__(self) -> None:
        self.points = 0

    def spend(self, k: int) -> None:
        self.points += k
        if self.points > MAX_POINTS:
            raise BudgetError(f"more than {MAX_POINTS} points")


def _geometry(b: bytes, i: int, end: int, budget: Budget, gtype: int) -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    ring: list[tuple[float, float]] | None = None
    x = y = 0
    while i < end:
        cmd, i = _varint(b, i, end)
        cid, count = cmd & 7, cmd >> 3
        budget.spend(1)  # every command counts, so runs of empty ones can't spin for long
        if cid == 7:
            if ring is None:
                raise MVTError("ClosePath before MoveTo")
            continue
        if cid not in (1, 2):
            raise MVTError(f"unknown geometry command {cid}")
        if cid == 1 and (count == 0 if gtype == 1 else count != 1):
            raise MVTError("a MoveTo needs a count of 1 (at least 1 for points)")
        if cid == 2 and ring is None:
            raise MVTError("LineTo before MoveTo")
        budget.spend(count)
        for _ in range(count):
            dx, i = _varint(b, i, end)
            dy, i = _varint(b, i, end)
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            if cid == 1:
                ring = []
                rings.append(ring)
            ring.append((float(x), float(y)))
    return rings


def _feature(b: bytes, i: int, end: int, budget: Budget) -> dict:
    gtype, geom = 0, None
    for field, wt, v in _fields(b, i, end):
        if field == 3 and wt == 0:
            if v > 3:
                raise MVTError(f"unknown feature type {v}")
            gtype = v
        elif field == 4 and wt == 2:
            geom = v
    rings = _geometry(b, geom[0], geom[1], budget, gtype) if geom else []
    return {"type": gtype, "rings": rings}


def decode(data: bytes, layers: set[str] | frozenset[str] | None = None, *,
           budget: Budget | None = None) -> dict[str, list[dict]]:
    """Layer name -> features `{type: 1|2|3, rings: [[(x, y), ...], ...], extent}` in tile units
    (0..extent; buffers may go a little outside). `layers` limits which layers are decoded;
    `budget` is shared with other calls to cap their points together (default: this tile
    alone). MVTError for a malformed or oversized tile."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MVTError("not bytes")
    b = bytes(data)
    if len(b) > MAX_BYTES:
        raise MVTError(f"tile larger than {MAX_BYTES} bytes")
    if budget is None:
        budget = Budget()
    out: dict[str, list[dict]] = {}
    for field, wt, v in _fields(b, 0, len(b)):
        if field != 3:
            continue
        if wt != 2:
            raise MVTError("layer is not a message")
        name, extent, feats = None, 4096, []
        for lf, lwt, lv in _fields(b, *v):
            if lf == 1 and lwt == 2:
                try:
                    name = b[lv[0]:lv[1]].decode("utf-8")
                except UnicodeDecodeError:
                    raise MVTError("layer name is not UTF-8") from None
            elif lf == 2 and lwt == 2:
                feats.append(lv)
            elif lf == 5 and lwt == 0:
                extent = lv
        if name is None:
            raise MVTError("layer without a name")
        if extent <= 0:
            raise MVTError("layer extent must be positive")
        if layers is not None and name not in layers:
            continue
        decoded = out.setdefault(name, [])
        for fs, fe in feats:
            f = _feature(b, fs, fe, budget)
            f["extent"] = extent
            decoded.append(f)
    return out
