"""The book as one HTML file: the CSS inline, pictures as `data:` URIs, map plates and diagrams
as inline SVG, a contents list at the top and each chapter anchored `#c{index}`.

It is written in an XHTML-compatible way (every element closed, every attribute quoted), so it
parses as XML as well as HTML. It needs nothing from anywhere else: no scripts, no fonts, no
remote images; http(s) source links are its only outbound links.

Pictures are streamed: a fragment is built with a marker where each picture's src goes, and
the writer copies the file into the page through base64 a chunk at a time, so memory stays
flat however big a picture is.
"""
from __future__ import annotations

import base64
import re
import secrets
from pathlib import Path

from . import xhtml
from .markdown import escape
from .model import BookPlan, Picture
from .plate import map_plate

CHUNK = 3 * 64 * 1024  # a multiple of 3, so the chunks' base64 joins up with no padding between
DATA_PREFIX = "data:image/png;base64,"


def _stream_picture(f, path: Path) -> None:
    f.write(DATA_PREFIX)
    with path.open("rb") as src:
        while chunk := src.read(CHUNK):
            f.write(base64.b64encode(chunk).decode("ascii"))


def write_html(plan: BookPlan, media_dir: Path, out: Path, plate=map_plate, archive=None,
               progress=lambda i, n: None, plates: dict | None = None) -> int:
    """Write the book to `out` as a single HTML file and return its size in bytes. A picture
    whose file is gone (or whose name is not a media name, or can't be opened) is left out.
    `progress(i, n)` is called after each chapter and before each picture (with the chapters
    done so far), so a caller can stop the job there by raising. `plates` is the job's plate
    cache, shared with `write_epub` (`xhtml.map_svg`).

    The cover's picture is embedded again in its chapter (a data: URI can't be shared without
    a script), as is a scene picture also shown in a chapter; `estimate` counts those copies."""
    used: set[str] = set()
    missing: set[str] = set()
    n = len(plan.chapters)
    done = 0
    # the marker is letters, digits and hyphens (escape leaves it alone), and its random token
    # can't be guessed by text from the book, so only our own src attributes match it
    token = secrets.token_hex(16)
    marker = re.compile(rf"primer-picture-{token}-(\d+)")
    paths: list[Path] = []

    def picture(p: Picture) -> str | None:
        if p.file in missing:
            return None
        progress(done, n)
        path = xhtml.picture_path(media_dir, p.file)
        if path is not None:
            try:
                with path.open("rb"):
                    pass
            except OSError:
                path = None
        if path is None:
            missing.add(p.file)
            return None
        used.add(p.file)
        paths.append(path)
        return f"primer-picture-{token}-{len(paths) - 1}"

    def put(f, fragment: str) -> None:
        """Write the fragment, streaming each picture in place of its marker."""
        parts = marker.split(fragment)
        for k, part in enumerate(parts):
            if k % 2:
                _stream_picture(f, paths[int(part)])
            else:
                f.write(part)
        paths.clear()  # each marker is used once; no path outlives its fragment

    links = xhtml.Links(chapter=lambda i: f"#c{i}", picture=picture)
    out = Path(out)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write('<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n'
                '<head>\n<meta charset="utf-8"/>\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
                f"<title>{escape(plan.title)}</title>\n<style>\n{xhtml.CSS}</style>\n</head>\n<body>\n")
        progress(0, n)
        put(f, xhtml.cover(plan, links) + "\n")
        entries = xhtml.contents_entries(plan, links, "#stories" if plan.stories else None, "#end")
        f.write('<nav id="contents" class="contents"><h1>Contents</h1>' + xhtml.contents(entries)
                + "</nav>\n")
        index = {ch.node_id: i for i, ch in enumerate(plan.chapters, start=1)}
        for i, ch in enumerate(plan.chapters, start=1):
            put(f, xhtml.chapter(i, ch, index, links, plate, archive, plates) + "\n")
            done = i
            progress(i, n)
        if plan.stories:
            put(f, xhtml.stories(plan, links) + "\n")
        f.write(xhtml.closing(plan, len(used), len(missing)) + "\n")
        f.write("</body>\n</html>\n")
    return out.stat().st_size
