"""Map plates: a page's places drawn as a self-contained SVG from the offline world map.

The world map is a PMTiles v3 archive of Mapbox Vector Tiles (gzip or uncompressed). A plate
draws three layers, `earth` (land), `water` and `boundaries` (thin lines), then the places as
numbered pins. Nothing from the places but their numbers and coordinates reaches the SVG, and
every number in it is formatted here, so the plate needs no escaping.

The frame is the places' Web Mercator bounds plus a 20% margin on each side, at least one
zoom-7 tile across, widened to the plate's 8:5 shape and kept inside ±85.05° latitude.
Longitudes wrap, and a set of places straddling ±180° is framed across the antimeridian (the
smaller wrap). Tiles come from the largest zoom ≤ 7 (and within the archive's zooms) at which
the frame needs at most 16 tiles.

Geometry is clipped to the plate, coordinates are rounded to 0.1 px, and points closer than
1 px to the previous kept point are dropped; while the SVG is over `max_bytes`, the tolerance
doubles and the plate is drawn again. With no archive, an unreadable archive or tile, or a
plate that stays too big, the plate is a plain one: a lat/lon grid, the pins and the note
"Drawn without the world map".

Pins: at most 300 are drawn (MAX_PINS), on either plate; the numbered list of places under the
plate is written by the book writer. A pin is labelled with the place's `n` when that is an int
from 1 to 9999, else with its 1-based position, and its centre is kept far enough inside the
plate that the whole pin shows. All the tiles of one plate share one 200 000-point budget, and
a tile stored larger than 5 MB is refused before it is decompressed. A plate over that budget
is tried again one zoom lower, with a fresh budget, down to the archive's lowest zoom.
"""
from __future__ import annotations

import math
import zlib
from pathlib import Path
from typing import NamedTuple

from primer.book.mvt import MAX_BYTES as _MAX_TILE, Budget, BudgetError, MVTError, decode
from primer.maps.pmtiles import Archive, PMTilesError, decompress

W, H = 800, 500
ASPECT = W / H
MAX_ZOOM = 7
MAX_TILES = 16
MARGIN = 0.2
MAX_LAT = 85.05
PLAIN_NOTE = "Drawn without the world map"
MAX_PINS = 300  # pins drawn on one plate; the numbered list under it is the book writer's
_PIN_R = 9
_PIN_EDGE = _PIN_R + 2  # radius, stroke and a little room
_LAYERS = frozenset({"earth", "water", "boundaries"})
_TOLERANCES = (1, 2, 4, 8, 16, 32, 64)
_PAD = 2.0  # clip a little outside the plate so edges don't show

_SEA = "#c9dde6"
_LAND = "#efe8d8"
_BORDER = "#9a8c98"
_CREAM = "#f5efdc"
_GRID = "#d8ccb0"
_PIN = "#8b1e1e"

_OPEN = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'width="{W}" height="{H}">')


class Frame(NamedTuple):
    """The plate's window in Web Mercator world units (0..1 across the world; x may run past
    0 or 1 when the frame crosses the antimeridian), and the tile zoom it is drawn from."""
    zoom: int
    x0: float
    y0: float
    x1: float
    y1: float


def _merc_y(lat: float) -> float:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    return 0.5 - math.asinh(math.tan(math.radians(lat))) / (2 * math.pi)


def _lat(y: float) -> float:
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))


_YTOP = _merc_y(MAX_LAT)
_YBOT = 1 - _YTOP


def _points(places: list[dict]) -> list[tuple[float, float]]:
    pts = []
    for p in places:
        if not isinstance(p, dict):
            continue
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(lat) and math.isfinite(lon):
            lon = (lon + 180.0) % 360.0 - 180.0
            pts.append(((lon + 180.0) / 360.0, _merc_y(lat)))
    return pts


def _tile_count(z: int, x0: float, y0: float, x1: float, y1: float) -> int:
    n = 1 << z
    ty0 = max(0, min(n - 1, math.floor(y0 * n)))
    ty1 = max(0, min(n - 1, math.floor(y1 * n)))
    return (math.floor(x1 * n) - math.floor(x0 * n) + 1) * (ty1 - ty0 + 1)


def frame(places: list[dict], *, min_zoom: int = 0, max_zoom: int = MAX_ZOOM) -> Frame:
    pts = _points(places)
    if pts:
        xs = sorted(x for x, _ in pts)
        # start just after the widest gap between neighbours, counting the gap across ±180°
        start, widest = xs[0], xs[0] + 1 - xs[-1]
        for a, b in zip(xs, xs[1:]):
            if b - a > widest:
                start, widest = b, b - a
        ux = [x if x >= start else x + 1 for x in xs]
        minx, maxx = start, max(ux)
        ys = [y for _, y in pts]
        miny, maxy = min(ys), max(ys)
    else:
        minx, maxx, miny, maxy = 0.0, 1.0, _YTOP, _YBOT
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    w = max((maxx - minx) * (1 + 2 * MARGIN), 1 / 128)
    h = max((maxy - miny) * (1 + 2 * MARGIN), 1 / 128)
    if w / h > ASPECT:
        h = w / ASPECT
    else:
        w = h * ASPECT
    if h > _YBOT - _YTOP:
        h = _YBOT - _YTOP
        w = h * ASPECT
    y0 = min(max(cy - h / 2, _YTOP), _YBOT - h)
    x0 = cx - w / 2
    x1, y1 = x0 + w, y0 + h
    lo = max(0, min(min_zoom, MAX_ZOOM))
    zoom = lo
    for z in range(max(lo, min(max_zoom, MAX_ZOOM)), lo - 1, -1):
        if _tile_count(z, x0, y0, x1, y1) <= MAX_TILES:
            zoom = z
            break
    return Frame(zoom, x0, y0, x1, y1)


# ── formatting ────────────────────────────────────────────────────────────────────────────


def _num(v: float) -> str:
    """A number rounded to one decimal, without a trailing .0."""
    t = round(v * 10)
    q, r = divmod(abs(t), 10)
    s = f"{q}.{r}" if r else str(q)
    return "-" + s if t < 0 else s


def _tenths(t: int) -> str:
    q, r = divmod(abs(t), 10)
    s = f"{q}.{r}" if r else str(q)
    return "-" + s if t < 0 else s


def _pair(dx: int, dy: int) -> str:
    a, b = _tenths(dx), _tenths(dy)
    return a + b if b[0] == "-" else a + " " + b


def _path_d(rings: list[list[tuple[float, float]]], closed: bool, tol: float) -> str:
    """Relative path data in tenths of a pixel, dropping points within `tol` px of the last
    kept one. Rings left with too few points are dropped."""
    out: list[str] = []
    cur = (0, 0)  # the current point, in tenths
    need = 3 if closed else 2
    t2 = tol * tol
    for ring in rings:
        lx, ly = ring[0]
        kept = [(lx, ly)]
        for x, y in ring[1:]:
            if (x - lx) ** 2 + (y - ly) ** 2 >= t2:
                kept.append((x, y))
                lx, ly = x, y
        if not closed and kept[-1] != ring[-1]:
            kept.append(ring[-1])
        pts: list[tuple[int, int]] = []
        for x, y in kept:
            p = (round(x * 10), round(y * 10))
            if not pts or p != pts[-1]:
                pts.append(p)
        if closed and len(pts) > 1 and pts[-1] == pts[0]:
            pts.pop()
        if len(pts) < need:
            continue
        parts = ["m" + _pair(pts[0][0] - cur[0], pts[0][1] - cur[1])]
        px, py = pts[0]
        for x, y in pts[1:]:
            d = _pair(x - px, y - py)
            parts.append(d if d[0] == "-" else " " + d)
            px, py = x, y
        if closed:
            parts.append("z")
            cur = pts[0]
        else:
            cur = (px, py)
        out.append("".join(parts))
    return "".join(out)


# ── clipping ──────────────────────────────────────────────────────────────────────────────

_X0, _Y0, _X1, _Y1 = -_PAD, -_PAD, W + _PAD, H + _PAD


def _clip_ring(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sutherland-Hodgman against the (padded) plate rectangle."""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    if min(xs) >= _X0 and max(xs) <= _X1 and min(ys) >= _Y0 and max(ys) <= _Y1:
        return ring
    if max(xs) < _X0 or min(xs) > _X1 or max(ys) < _Y0 or min(ys) > _Y1:
        return []
    pts = ring
    for axis, bound, keep_above in ((0, _X0, True), (0, _X1, False),
                                    (1, _Y0, True), (1, _Y1, False)):
        if not pts:
            break
        out = []
        prev = pts[-1]
        pin = (prev[axis] >= bound) if keep_above else (prev[axis] <= bound)
        for cur in pts:
            cin = (cur[axis] >= bound) if keep_above else (cur[axis] <= bound)
            if cin != pin:
                t = (bound - prev[axis]) / (cur[axis] - prev[axis])
                ix = prev[0] + t * (cur[0] - prev[0])
                iy = prev[1] + t * (cur[1] - prev[1])
                out.append((bound, iy) if axis == 0 else (ix, bound))
            if cin:
                out.append(cur)
            prev, pin = cur, cin
        pts = out
    return pts


def _clip_line(line: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Liang-Barsky per segment; the visible pieces as polylines."""
    pieces: list[list[tuple[float, float]]] = []
    run: list[tuple[float, float]] = []
    for (ax, ay), (bx, by) in zip(line, line[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - _X0), (dx, _X1 - ax), (-dy, ay - _Y0), (dy, _Y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    if r > t1:
                        ok = False
                        break
                    t0 = max(t0, r)
                else:
                    if r < t0:
                        ok = False
                        break
                    t1 = min(t1, r)
        if not ok:
            if len(run) > 1:
                pieces.append(run)
            run = []
            continue
        s = (ax + t0 * dx, ay + t0 * dy)
        e = (ax + t1 * dx, ay + t1 * dy)
        if not run or run[-1] != s:
            if len(run) > 1:
                pieces.append(run)
            run = [s]
        run.append(e)
        if t1 < 1.0:
            pieces.append(run)
            run = []
    if len(run) > 1:
        pieces.append(run)
    return pieces


# ── drawing ───────────────────────────────────────────────────────────────────────────────


def _pin_xy(fr: Frame, places: list[dict]) -> list[tuple[str, float, float]]:
    u = (fr.x1 - fr.x0) / W
    cx = (fr.x0 + fr.x1) / 2
    pins = []
    for i, p in enumerate(places):
        if len(pins) >= MAX_PINS:
            break
        pts = _points([p])
        if not pts:
            continue
        x, y = pts[0]
        x += round(cx - x)  # the copy of the world nearest the frame's centre
        n = p.get("n")
        if type(n) is not int or not 1 <= n <= 9999:  # not bool, float, huge or negative
            n = i + 1
        # keep the whole pin on the plate (a place near a pole sits on the frame's edge)
        px = min(max((x - fr.x0) / u, _PIN_EDGE), W - _PIN_EDGE)
        py = min(max((y - fr.y0) / u, _PIN_EDGE), H - _PIN_EDGE)
        pins.append((str(n), px, py))
    return pins


def _title(places: list[dict]) -> str:
    k = len(places)
    return f"<title>Map of {k} place{'' if k == 1 else 's'}</title>"


def _pins_svg(fr: Frame, places: list[dict]) -> str:
    out = []
    for label, x, y in _pin_xy(fr, places):
        out.append(f'<circle cx="{_num(x)}" cy="{_num(y)}" r="{_PIN_R}" fill="{_PIN}" stroke="#fff" '
                   f'stroke-width="1.5"/>')
        out.append(f'<text x="{_num(x)}" y="{_num(y + 4)}" font-family="sans-serif" '
                   f'font-size="11" font-weight="bold" text-anchor="middle" fill="#fff">'
                   f'{label}</text>')
    return "".join(out)


def _plain(fr: Frame, places: list[dict]) -> str:
    u = (fr.x1 - fr.x0) / W
    lon0, lon1 = fr.x0 * 360 - 180, fr.x1 * 360 - 180
    step = 1 if lon1 - lon0 <= 20 else 10
    lines = []
    lon = math.ceil(lon0 / step) * step
    while lon <= lon1:
        x = ((lon + 180) / 360 - fr.x0) / u
        lines.append(f"M{_num(x)} 0V{H}")
        lon += step
    lat_top, lat_bot = _lat(fr.y0), _lat(fr.y1)
    lat = math.ceil(lat_bot / step) * step
    while lat <= lat_top:
        if abs(lat) <= MAX_LAT:
            y = (_merc_y(lat) - fr.y0) / u
            lines.append(f"M0 {_num(y)}H{W}")
        lat += step
    grid = (f'<path fill="none" stroke="{_GRID}" stroke-width="0.6" d="{"".join(lines)}"/>'
            if lines else "")
    return (_OPEN + _title(places) + f'<rect width="{W}" height="{H}" fill="{_CREAM}"/>' + grid
            + _pins_svg(fr, places)
            + f'<text x="12" y="{H - 12}" font-family="serif" font-size="13" fill="#6b5d45">'
            + PLAIN_NOTE + "</text></svg>")


def _gunzip(raw: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, _MAX_TILE + 1)
    if len(out) > _MAX_TILE or d.unconsumed_tail:
        raise MVTError("tile decompresses to more than 5 MB")
    if not d.eof:
        raise MVTError("truncated gzip tile")
    return out


def _geometry(arch, fr: Frame):
    """Clipped plate-pixel geometry: (earth rings, water rings, boundary lines), or None when
    the archive has no tile at all for the frame."""
    h = arch.header
    if h.get("tile_type") != 1:
        raise MVTError("the map file's tiles are not vector tiles")
    comp = h.get("tile_compression")
    z = fr.zoom
    n = 1 << z
    if _tile_count(z, fr.x0, fr.y0, fr.x1, fr.y1) > MAX_TILES:
        raise MVTError("frame needs too many tiles")
    u = (fr.x1 - fr.x0) / W
    earth: list = []
    water: list = []
    borders: list = []
    cache: dict[tuple[int, int], dict | None] = {}
    budget = Budget()  # one point cap for the whole plate, not per tile
    found = False
    ty0 = max(0, math.floor(fr.y0 * n))
    ty1 = min(n - 1, math.floor(fr.y1 * n))
    for tx in range(math.floor(fr.x0 * n), math.floor(fr.x1 * n) + 1):
        for ty in range(ty0, ty1 + 1):
            key = (tx % n, ty)
            if key not in cache:
                raw = arch.tile(z, key[0], ty)
                if raw is None:
                    cache[key] = None
                else:
                    if len(raw) > _MAX_TILE:
                        raise MVTError("tile larger than 5 MB")
                    data = _gunzip(raw) if comp == 2 else decompress(raw, comp)
                    cache[key] = decode(data, _LAYERS, budget=budget)
            layers = cache[key]
            if layers is None:
                continue
            found = True
            for name, dest, closed in (("earth", earth, True), ("water", water, True),
                                       ("boundaries", borders, False)):
                for f in layers.get(name, ()):
                    if f["type"] != (3 if closed else 2):
                        continue
                    k = f["extent"] * n * u
                    ox = (tx / n - fr.x0) / u
                    oy = (ty / n - fr.y0) / u
                    for ring in f["rings"]:
                        if len(ring) < 2:
                            continue
                        px = [(ox + x / k, oy + y / k) for x, y in ring]
                        if closed:
                            c = _clip_ring(px)
                            if len(c) >= 3:
                                dest.append(c)
                        else:
                            dest.extend(_clip_line(px))
    return (earth, water, borders) if found else None


def _map_svg(fr: Frame, places: list[dict], geom, tol: float) -> str:
    earth, water, borders = geom
    parts = [_OPEN, _title(places), f'<rect width="{W}" height="{H}" fill="{_SEA}"/>']
    d = _path_d(earth, True, tol)
    if d:
        parts.append(f'<path fill="{_LAND}" d="{d}"/>')
    d = _path_d(water, True, tol)
    if d:
        parts.append(f'<path fill="{_SEA}" d="{d}"/>')
    d = _path_d(borders, False, tol)
    if d:
        parts.append(f'<path fill="none" stroke="{_BORDER}" stroke-width="0.8" '
                     f'stroke-linejoin="round" d="{d}"/>')
    parts.append(_pins_svg(fr, places))
    parts.append("</svg>")
    return "".join(parts)


def map_plate(places: list[dict], archive=None, *, max_bytes: int = 300_000) -> str:
    """The places as an 800x500 SVG plate over the world map. `archive` is an open
    `pmtiles.Archive`, a path to one, or None for the plain plate."""
    if archive is None:
        return _plain(frame(places), places)
    opened = None
    try:
        if isinstance(archive, (str, Path)):
            archive = opened = Archive(archive)
        h = archive.header
        min_zoom = int(h.get("min_zoom", 0))
        fr = frame(places, min_zoom=min_zoom, max_zoom=int(h.get("max_zoom", MAX_ZOOM)))
        while True:
            try:
                geom = _geometry(archive, fr)  # a fresh point budget for each zoom
                break
            except BudgetError:
                # too detailed at this zoom: a lower zoom has fewer points and fewer tiles
                if fr.zoom <= max(0, min_zoom):
                    raise
                fr = fr._replace(zoom=fr.zoom - 1)
    except (PMTilesError, MVTError, OSError, ValueError, EOFError, zlib.error):
        return _plain(frame(places), places)
    finally:
        if opened is not None:
            opened.close()
    if geom is None:
        return _plain(fr, places)
    for tol in _TOLERANCES:
        svg = _map_svg(fr, places, geom, tol)
        if len(svg.encode("utf-8")) <= max_bytes:
            return svg
    return _plain(frame(places), places)
