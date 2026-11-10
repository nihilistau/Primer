"""The book as an EPUB 3: a zip of XHTML files, a nav, a stylesheet and the pictures.

The zip starts with `mimetype` (stored, uncompressed), then `META-INF/container.xml`,
`style.css`, `cover.xhtml`, one `chapter-0001.xhtml` per chapter (each picture once as
`images/<sha>.png`, just before the first page that shows it), `stories.xhtml` (when there
are stories), `end.xhtml`, `nav.xhtml` and, last, `OEBPS/content.opf`: a picture that can't be
read at write time is left out, so the manifest is written once the pictures are known. The spine reads cover, nav, chapters, stories, end. Chapters with a map
plate or a diagram carry inline SVG, and their manifest items say `properties="svg"`.
"""
from __future__ import annotations

import time
import uuid
import zipfile
from pathlib import Path

from . import xhtml
from .markdown import escape
from .model import BookPlan, Picture
from .plate import map_plate

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""
XHTML_TYPE = "application/xhtml+xml"


def chapter_file(i: int) -> str:
    return f"chapter-{i:04d}.xhtml"


def page(title: str, body: str) -> str:
    """An XHTML content document around a body fragment."""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="en" lang="en">\n<head>\n<meta charset="utf-8"/>\n'
            f"<title>{escape(title)}</title>\n"
            '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
            f"<body>\n{body}\n</body>\n</html>\n")


def _opf(plan: BookPlan, items: list[tuple[str, str, str, str]], spine: list[str]) -> str:
    """items: (id, href, media type, properties)."""
    meta = [f'<dc:identifier id="bookid">urn:uuid:{uuid.uuid4()}</dc:identifier>',
            f"<dc:title>{escape(plan.title)}</dc:title>", "<dc:language>en</dc:language>"]
    if plan.by:
        meta.append(f"<dc:creator>{escape(plan.by)}</dc:creator>")
    meta.append('<meta property="dcterms:modified">'
                f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</meta>')
    manifest = []
    for id_, href, mtype, props in items:
        p = f' properties="{props}"' if props else ""
        manifest.append(f'<item id="{id_}" href="{escape(href)}" media-type="{mtype}"{p}/>')
    refs = "".join(f'<itemref idref="{r}"/>' for r in spine)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
            'xml:lang="en">\n<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            + "\n".join(meta) + "\n</metadata>\n<manifest>\n" + "\n".join(manifest)
            + f"\n</manifest>\n<spine>{refs}</spine>\n</package>\n")


def write_epub(plan: BookPlan, media_dir: Path, out: Path, plate=map_plate, archive=None,
               progress=lambda i, n: None, plates: dict | None = None) -> int:
    """Write the book to `out` as an EPUB 3 and return its size in bytes. Each picture is read
    and written into the zip at its first use, before the page that shows it; one whose file is
    gone, unreadable or not a media name is left out of the page and the manifest.
    `progress(i, n)` is called after each chapter and again before each picture (with the
    chapters done so far), so a caller can stop the job there by raising. `plates` is the
    job's plate cache, shared with `write_html` (`xhtml.map_svg`)."""
    n = len(plan.chapters)
    out = Path(out)
    written: list[str] = []  # picture file names in the zip, in order
    missing: set[str] = set()
    done = 0

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def picture(p: Picture) -> str | None:
            if p.file in missing:
                return None
            if p.file not in written:
                progress(done, n)
                path = xhtml.picture_path(media_dir, p.file)
                try:
                    data = path.read_bytes() if path is not None else None
                except OSError:
                    data = None
                if data is None:
                    missing.add(p.file)
                    return None
                z.writestr(f"OEBPS/images/{p.file}", data, compress_type=zipfile.ZIP_STORED)
                written.append(p.file)
            return f"images/{p.file}"

        links = xhtml.Links(chapter=lambda i: f"{chapter_file(i)}#c{i}", picture=picture)
        z.writestr(zipfile.ZipInfo("mimetype", time.localtime()[:6]), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/style.css", xhtml.CSS)
        progress(0, n)
        cover_pic = xhtml.first_picture(plan, links)
        z.writestr("OEBPS/cover.xhtml", page(plan.title, xhtml.cover(plan, links)))
        index = {ch.node_id: i for i, ch in enumerate(plan.chapters, start=1)}
        svg_chapters = set()
        for i, ch in enumerate(plan.chapters, start=1):
            body = xhtml.chapter(i, ch, index, links, plate, archive, plates)
            if "<svg" in body:
                svg_chapters.add(i)
            z.writestr(f"OEBPS/{chapter_file(i)}", page(ch.title, body))
            done = i
            progress(i, n)
        if plan.stories:
            z.writestr("OEBPS/stories.xhtml", page("Stories", xhtml.stories(plan, links)))
        z.writestr("OEBPS/end.xhtml", page("Closing note", xhtml.closing(plan, len(written), len(missing))))
        entries = xhtml.contents_entries(plan, links, "stories.xhtml#stories" if plan.stories else None,
                                         "end.xhtml#end")
        nav = ('<nav epub:type="toc" id="toc" class="contents"><h1>Contents</h1>'
               + xhtml.contents(entries) + "</nav>")
        z.writestr("OEBPS/nav.xhtml", page(plan.title, nav))

        # the package last: only now is it known which pictures went in
        items = [("nav", "nav.xhtml", XHTML_TYPE, "nav"), ("css", "style.css", "text/css", ""),
                 ("cover", "cover.xhtml", XHTML_TYPE, "")]
        for i in range(1, n + 1):
            items.append((f"ch{i:04d}", chapter_file(i), XHTML_TYPE, "svg" if i in svg_chapters else ""))
        if plan.stories:
            items.append(("stories", "stories.xhtml", XHTML_TYPE, ""))
        items.append(("end", "end.xhtml", XHTML_TYPE, ""))
        for k, name in enumerate(written, start=1):
            cover = cover_pic is not None and cover_pic[0].file == name
            items.append((f"img{k:04d}", f"images/{name}", "image/png", "cover-image" if cover else ""))
        spine = ["cover", "nav"] + [f"ch{i:04d}" for i in range(1, n + 1)]
        spine += ["stories"] if plan.stories else []
        spine.append("end")
        z.writestr("OEBPS/content.opf", _opf(plan, items, spine))
    return out.stat().st_size
