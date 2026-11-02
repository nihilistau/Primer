"""map_extract.py — cut a small offline map out of a Protomaps world build.

Walks the remote PMTiles v3 archive's directories with HTTP Range requests, keeps every tile up
to --maxzoom (plus, with --bbox, the tiles up to --region-maxzoom that touch the box), downloads
their data in large coalesced ranges into a resumable `.part` file, and writes a local PMTiles v3
file with primer.maps.pmtiles. Downloads only from --source; writes only --out (and its
`.part` / `.tmp` working files beside it).

    python tools/map_extract.py --out maps/world-z9.pmtiles --maxzoom 9
    python tools/map_extract.py --out maps/uk.pmtiles --maxzoom 9 \\
        --bbox -8.7,49.8,1.8,60.9 --region-maxzoom 14
    python tools/map_extract.py ... --dry-run      # the plan and size, nothing downloaded

Default --source: today's https://build.protomaps.com/YYYYMMDD.pmtiles, or the newest of the
previous 10 days that answers (days in UTC).

Disk space: while it runs it needs about 2-3x the finished file's size free beside --out (the
downloaded `.part` data, the writer's temporary copy, then the file itself).
"""
from __future__ import annotations

import argparse
import http.client
import bisect
import datetime
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from primer.maps import USER_AGENT  # noqa: E402
from primer.maps.pmtiles import (HEADER_LEN, PMTilesError, decompress,  # noqa: E402
                                 parse_directory, parse_header, write_archive, zoom_start, zxy_to_tileid)

BUILD_URL = "https://build.protomaps.com/{:%Y%m%d}.pmtiles"
LOOKBACK_DAYS = 10
CHUNK = 16 << 20  # bytes per data request
GAP = 1 << 20  # tiles this close together are fetched in one range (the gap is downloaded too)
MAX_LAT = 85.0511287798066  # Web Mercator's edge
_MAX_DEPTH = 4


# ── network ───────────────────────────────────────────────────────────────────────────────


def http_fetcher(url: str, *, retries: int = 8, timeout: float = 120.0, sleep=time.sleep):
    """fetch(offset, length) -> exactly those bytes of `url`, with retries. A server that
    answers a range request with the whole file (200, not 206) is refused before reading."""

    def fetch(offset: int, length: int) -> bytes:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={
                    "Range": f"bytes={offset}-{offset + length - 1}", "User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    if r.status != 206:
                        raise OSError(f"the server ignored the byte range (HTTP {r.status})")
                    b = r.read(length + 1)
                if len(b) == length:
                    return b
                last = OSError(f"got {len(b)} of {length} bytes")
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 404, 410, 416):  # retrying won't help
                    break
            except (OSError, http.client.HTTPException) as e:  # IncompleteRead, BadStatusLine...
                last = e
            if attempt + 1 < retries:
                sleep(5 * (attempt + 1))
        raise OSError(f"download failed at byte {offset}: {last}")

    return fetch


def find_source(probe, today: datetime.date | None = None, days: int = LOOKBACK_DAYS) -> str:
    """The newest daily build that answers, today first, then one day back at a time."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    for back in range(days + 1):
        url = BUILD_URL.format(today - datetime.timedelta(days=back))
        if probe(url):
            return url
    raise SystemExit(f"no Protomaps build found for {today:%Y-%m-%d} or the {days} days before; "
                     "pass --source")


def _probe(url: str) -> bool:
    try:
        parse_header(http_fetcher(url, retries=1, timeout=30)(0, HEADER_LEN))
        return True
    except (OSError, http.client.HTTPException, PMTilesError):
        return False


# ── what to keep ──────────────────────────────────────────────────────────────────────────


def _mercator_y(lat: float, n: int) -> float:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    r = math.radians(lat)
    return (1 - math.asinh(math.tan(r)) / math.pi) / 2 * n


def region_ids(bbox: tuple[float, float, float, float], zooms: range) -> list[int]:
    """Sorted ids of the tiles of these zooms that intersect bbox (W, S, E, N in degrees)."""
    w, s, e, n_ = bbox
    ids: list[int] = []
    for z in zooms:
        n = 1 << z
        clamp = lambda v: max(0, min(n - 1, v))
        x0 = clamp(math.floor((w + 180) / 360 * n))
        x1 = clamp(max(x0, math.ceil((e + 180) / 360 * n) - 1))
        y0 = clamp(math.floor(_mercator_y(n_, n)))
        y1 = clamp(max(y0, math.ceil(_mercator_y(s, n)) - 1))
        ids.extend(zxy_to_tileid(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
    ids.sort()
    return ids


def plan(fetch, maxzoom: int, bbox=None, region_maxzoom: int | None = None) -> dict:
    """Read the source's header, metadata and directories; list the tiles to keep."""
    head = fetch(0, HEADER_LEN)
    h = parse_header(head)
    top = max(maxzoom, region_maxzoom or 0) if bbox else maxzoom
    all_below = zoom_start(maxzoom + 1)  # every tile id under this is kept
    region = region_ids(bbox, range(maxzoom + 1, top + 1)) if bbox else []
    limit = zoom_start(top + 1)

    def wanted(lo: int, hi: int):
        yield from range(lo, min(hi, all_below))
        yield from region[bisect.bisect_left(region, lo):bisect.bisect_left(region, hi)]

    def any_wanted(lo: int, hi: int) -> bool:
        i = bisect.bisect_left(region, lo)
        return lo < all_below or (i < len(region) and region[i] < hi)

    tiles: list[tuple[int, int, int]] = []  # (tile id, source data offset, length)
    dirs = [0]

    def walk(raw: bytes, end: int, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise PMTilesError("source leaf directories nested too deep")
        es = parse_directory(raw, h["internal_compression"])
        dirs[0] += 1
        for k, e in enumerate(es):
            if e.tile_id >= limit:
                return
            nxt = es[k + 1].tile_id if k + 1 < len(es) else end
            if e.run_length == 0:
                if any_wanted(e.tile_id, nxt):
                    walk(fetch(h["leaf_offset"] + e.offset, e.length), nxt, depth + 1)
            else:
                tiles.extend((t, e.offset, e.length) for t in wanted(e.tile_id, e.tile_id + e.run_length))

    walk(fetch(h["root_offset"], h["root_length"]), limit, 0)
    tiles.sort()
    try:
        meta = json.loads(decompress(fetch(h["metadata_offset"], h["metadata_length"]),
                                      h["internal_compression"]) or b"{}")
    except (OSError, EOFError, ValueError, zlib.error) as e:
        raise PMTilesError(f"source metadata unreadable: {e}") from None
    if not isinstance(meta, dict):
        raise PMTilesError("source metadata is not a JSON object")
    spans: list[list[int]] = []  # coalesced [start, end) ranges of the source's tile data
    for off, ln in sorted({(off, ln) for _, off, ln in tiles}):
        if spans and off - spans[-1][1] <= GAP:
            spans[-1][1] = max(spans[-1][1], off + ln)
        else:
            spans.append([off, off + ln])
    return {"header": h, "head": head, "metadata": meta, "tiles": tiles, "spans": spans,
            "top": top, "directories": dirs[0]}


# ── the download and the output ───────────────────────────────────────────────────────────


def _download(fetch, p: dict, part: Path, chunk: int, log) -> None:
    """Append the spans' bytes to `part`, resuming where an earlier run of the same plan
    stopped (a `.part.json` beside it names the plan; a different plan starts over)."""
    sidecar = Path(str(part) + ".json")
    key = hashlib.sha256(p["head"] + json.dumps(p["spans"]).encode()).hexdigest()
    total = sum(e - s for s, e in p["spans"])
    try:
        same = json.loads(sidecar.read_text(encoding="utf-8")).get("plan") == key
    except (OSError, ValueError, AttributeError):
        same = False
    have = part.stat().st_size if same and part.exists() else 0
    if not same or have > total:
        have = 0
        part.write_bytes(b"")
        sidecar.write_text(json.dumps({"plan": key}), encoding="utf-8")
    if have:
        log(f"resuming: {have / 1e6:.1f} of {total / 1e6:.1f} MB already here")
    data_off = p["header"]["tile_data_offset"]
    done, pos, step = have, 0, max(total // 20, 1)
    next_note = done + step
    with open(part, "ab") as f:
        for s, e in p["spans"]:
            if pos + (e - s) <= have:
                pos += e - s
                continue
            cur = s + max(0, have - pos)
            while cur < e:
                n = min(chunk, e - cur)
                b = fetch(data_off + cur, n)
                if len(b) != n:
                    raise OSError(f"got {len(b)} of {n} bytes at {data_off + cur}")
                f.write(b)
                f.flush()
                cur += n
                done += n
                if done >= next_note or done == total:
                    log(f"data {done / 1e6:.1f} / {total / 1e6:.1f} MB")
                    next_note = done + step
            pos += e - s


def extract(fetch, out, maxzoom: int, bbox=None, region_maxzoom: int | None = None, *,
            dry_run: bool = False, chunk: int = CHUNK, log=print) -> dict:
    out = Path(out)
    p = plan(fetch, maxzoom, bbox, region_maxzoom)
    total = sum(e - s for s, e in p["spans"])
    where = f" + z{maxzoom + 1}-{p['top']} inside {bbox}" if bbox and p["top"] > maxzoom else ""
    log(f"{len(p['tiles'])} tiles (z0-{maxzoom}{where}) from {p['directories']} directories: "
        f"about {total / 1e6:.1f} MB to download in {len(p['spans'])} range(s)")
    summary = {"tiles": len(p["tiles"]), "bytes": total, "ranges": len(p["spans"])}
    if dry_run:
        return summary
    part = Path(str(out) + ".part")
    _download(fetch, p, part, chunk, log)

    starts = [s for s, _ in p["spans"]]
    bases, acc = [], 0
    for s, e in p["spans"]:
        bases.append(acc)
        acc += e - s
    h = p["header"]
    meta = dict(p["metadata"], maxzoom=p["top"])
    clon, clat, cz = h["center"]
    with open(part, "rb") as f:
        def tiles():
            for tile_id, off, ln in p["tiles"]:
                i = bisect.bisect_right(starts, off) - 1
                f.seek(bases[i] + off - starts[i])
                b = f.read(ln)
                if len(b) != ln:
                    raise OSError(f"{part.name} is shorter than the plan")
                yield tile_id, b
        write_archive(out, tiles(), meta, tile_type=h["tile_type"],
                      tile_compression=h["tile_compression"], min_zoom=min(h["min_zoom"], p["top"]),
                      max_zoom=p["top"], bounds=h["bounds"], center=(clon, clat, min(cz, p["top"])))
    for leftover in (part, Path(str(part) + ".json")):
        leftover.unlink(missing_ok=True)
    log(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return summary


# ── command line ──────────────────────────────────────────────────────────────────────────


def _bbox(text: str) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("--bbox is W,S,E,N in degrees, e.g. -8.7,49.8,1.8,60.9") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise argparse.ArgumentTypeError("--bbox needs -180 <= W < E <= 180 and -90 <= S < N <= 90")
    return w, s, e, n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Needs about 2-3x the finished file's size free beside --out while running.")
    ap.add_argument("--source", help="PMTiles v3 URL (default: the newest Protomaps daily build)")
    ap.add_argument("--out", required=True, help="the local .pmtiles file to write")
    ap.add_argument("--maxzoom", type=int, required=True, help="keep every tile up to this zoom")
    ap.add_argument("--bbox", type=_bbox, help="W,S,E,N: a region to keep in more detail")
    ap.add_argument("--region-maxzoom", type=int, help="the region's max zoom (with --bbox)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and size, download nothing")
    a = ap.parse_args(argv)
    if not 0 <= a.maxzoom <= 15:
        ap.error("--maxzoom must be 0 to 15")
    if (a.bbox is None) != (a.region_maxzoom is None):
        ap.error("--bbox and --region-maxzoom go together")
    if a.bbox and not a.maxzoom < a.region_maxzoom <= 15:
        ap.error("--region-maxzoom must be above --maxzoom and at most 15")
    url = a.source or find_source(_probe)
    print(f"source {url}", flush=True)
    try:
        extract(http_fetcher(url), a.out, a.maxzoom, a.bbox, a.region_maxzoom, dry_run=a.dry_run,
                log=lambda m: print(m, flush=True))
    except (OSError, PMTilesError) as e:
        print(f"map_extract: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
