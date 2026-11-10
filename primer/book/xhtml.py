"""The parts both book writers share: a chapter, the cover, the stories, the closing note and
the nested contents, each as an XHTML fragment, plus the book's CSS.

Every piece of text from the database goes through `escape`; the chapter's Markdown arrives
already rendered to safe XHTML (`markdown.render`), the diagram SVG already re-cleaned
(`clean_svg`), and map plates are drawn by our own code from numbers. A writer says how it
links (`Links`): the EPUB points across files, the HTML within one page.
"""
from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from primer.timelines.dates import _APPROX

from .markdown import escape, safe_url
from .model import (MAX_CHAPTERS, MAX_DEPTH, MAX_PICTURES, PICTURE_NAME, PICTURES_PER_CHAPTER, BookPlan,
                    Chapter, MapData, Picture)

VIDEO_NOTE = "A video clip was made of this page in Primer."
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")

CSS = """\
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.5; color: #222;
       background: #fff; margin: 0 auto; max-width: 42em; padding: 0 1em; }
h1, h2, h3, h4 { font-weight: normal; line-height: 1.2; }
h1 { font-size: 1.8em; margin: 1.2em 0 0.3em; }
h2 { font-size: 1.3em; margin: 1.2em 0 0.4em; }
.via, .subtitle { color: #6b5d45; font-style: italic; margin-top: 0; }
.via em { font-style: normal; }
.via a, .contents a, sup a { color: #6b3b1e; text-decoration: none; }
figure { margin: 1.2em 0; text-align: center; page-break-inside: avoid; }
figure img, figure svg { max-width: 100%; height: auto; }
figcaption { font-size: 0.85em; color: #555; font-style: italic; margin-top: 0.3em; }
figure.map svg { border: 1px solid #c8b99a; }
ol.places { font-size: 0.9em; columns: 2; }
ol.timeline { list-style: none; padding-left: 0; border-left: 2px solid #c8b99a; }
ol.timeline li { padding-left: 0.8em; margin-bottom: 0.4em; }
ol.timeline .date { font-weight: bold; margin-right: 0.5em; }
.video { font-size: 0.9em; color: #555; }
ol.sources { font-size: 0.85em; word-wrap: break-word; }
.cover { text-align: center; padding-top: 2em; }
.cover .title { font-size: 2.4em; }
.cover .by, .cover .dates { font-size: 1.1em; color: #555; }
.contents ol { list-style: none; padding-left: 1.2em; }
.contents > ol { padding-left: 0; }
.story h3 { font-size: 1.1em; margin-top: 1em; }
.end p { font-size: 0.95em; }
"""


@dataclass
class Links:
    """How a writer links: `chapter(i)` is chapter i's anchor (from 1) as an href, and
    `picture(p)` is the picture's src, or None when its file can't be used."""
    chapter: Callable[[int], str]
    picture: Callable[[Picture], str | None]


def picture_path(media_dir: Path, name: str) -> Path | None:
    """The picture's file through the media-name check (64 hex digits and .png, directly in the
    media folder), or None when the name is not one or the file is not there."""
    if not isinstance(name, str) or not PICTURE_NAME.fullmatch(name):
        return None
    try:
        root = Path(media_dir).resolve()
        target = (root / name).resolve()
    except OSError:
        return None
    if target.parent != root or not target.is_file():
        return None
    return target


def day(t: float | None) -> str:
    """A reading time as "9 November 2026" (local date); "" when there is none."""
    if t is None:
        return ""
    try:
        lt = time.localtime(t)
    except (OSError, OverflowError, ValueError):
        return ""
    return f"{lt.tm_mday} {MONTHS[lt.tm_mon - 1]} {lt.tm_year}"


def _year(y) -> str:
    return f"{-y} BC" if isinstance(y, int) and y < 0 else str(y)


def event_date(e: dict) -> str:
    """The date as written; "about" for an approximate one, and "to <end>" for a span unless the
    written date already gives its end."""
    text = " ".join(str(e.get("date_text") or "").split()) or _year(e.get("year"))
    if e.get("approx"):
        text = "about " + (_APPROX.sub("", text, count=1).strip() or _year(e.get("year")))
    end = e.get("end_year")
    if (isinstance(end, int) and not isinstance(end, bool)
            and not re.search(rf"(?<!\d){abs(end)}(?!\d)", text)):  # the end year as a whole number
        text += " to " + _year(end)
    return text


def _figure(src: str, caption: str, cls: str = "picture") -> str:
    c = escape(caption)
    return f'<figure class="{cls}"><img src="{escape(src)}" alt="{c}"/><figcaption>{c}</figcaption></figure>'


def _paragraphs(text: str) -> str:
    parts = [" ".join(p.split()) for p in str(text or "").replace("\r", "").split("\n")]
    return "".join(f"<p>{escape(p)}</p>" for p in parts if p)


def _source(anchor: str, s: dict) -> str:
    n = s.get("n")
    title = escape(s.get("title") or "Untitled")
    kind = escape(s.get("kind") or "")
    out = [f'<li id="{escape(anchor)}-src-{int(n)}" value="{int(n)}"><cite>{title}</cite>']
    if kind:
        out.append(f" ({kind})")
    url = str(s.get("url") or "")
    if url:
        safe = safe_url(url)
        if safe is None:
            out.append(f". {escape(url)}")  # shown, never linked
        else:
            out.append(f'. <a href="{escape(safe)}">{escape(safe)}</a>')
    out.append("</li>")
    return "".join(out)


def map_svg(mp: MapData, plate, archive, plates: dict | None) -> str:
    """The map's plate. `plates` (map id to the plate, zlib-compressed) is a job's cache, so
    the EPUB and the HTML draw each plate once; None draws every time."""
    if plates is None:
        return plate(mp.places, archive)
    kept = plates.get(mp.map_id)
    if kept is not None:
        return zlib.decompress(kept).decode("utf-8")
    svg = plate(mp.places, archive)
    plates[mp.map_id] = zlib.compress(svg.encode("utf-8"))  # plates are big, and compress well
    return svg


def chapter(i: int, ch: Chapter, index: dict[str, int], links: Links, plate, archive,
            plates: dict | None = None) -> str:
    """Chapter i (from 1) as a section anchored `c{i}`. `index` maps a node id to its chapter
    number, for the "From ... in ..." link; `plates` is the job's plate cache (`map_svg`)."""
    anchor = f"c{i}"
    out = [f'<section class="chapter" id="{anchor}">', f"<h1>{escape(ch.title)}</h1>"]
    phrase = " ".join(str(ch.via_phrase or "").split())
    if ch.via_title:
        if ch.via_title == ch.title and phrase:
            out.append(f'<p class="subtitle">{escape(phrase)}</p>')
        where = escape(ch.via_title)
        parent = index.get(ch.parent_id) if ch.parent_id else None
        if parent is not None:
            where = f'<a href="{escape(links.chapter(parent))}">{where}</a>'
        lead = f"From <em>{escape(phrase)}</em> in " if phrase else "From "
        out.append(f'<p class="via">{lead}{where}</p>')
    if ch.html:
        out.append(f'<div class="text">{ch.html}</div>')
    for p in ch.pictures:
        src = links.picture(p)
        if src is not None:
            out.append(_figure(src, p.caption))
    for mp in ch.maps:
        svg = map_svg(mp, plate, archive, plates)
        caption = " ".join(str(mp.phrase or "").split()) or "Places on this page"
        out.append(f'<figure class="map">{svg}<figcaption>{escape(caption)}</figcaption></figure>')
        items = []
        for place in mp.places:
            name, what = str(place.get("name") or ""), " ".join(str(place.get("what") or "").split())
            items.append(f"<li>{escape(name)}{', ' + escape(what) if what else ''}</li>")
        if items:
            out.append(f'<ol class="places">{"".join(items)}</ol>')
    if ch.timeline:
        out.append("<h2>Timeline</h2>")
        out.append('<ol class="timeline">' + "".join(
            f'<li><span class="date">{escape(event_date(e))}</span> '
            f'<span class="label">{escape(e.get("label") or "")}</span></li>' for e in ch.timeline) + "</ol>")
    if ch.diagram:
        caption = str(ch.diagram.get("caption") or "")
        cap = f"<figcaption>{escape(caption)}</figcaption>" if caption.strip() else ""
        out.append(f'<figure class="diagram">{ch.diagram["svg"]}{cap}</figure>')
    if ch.has_video:
        out.append(f'<p class="video">{VIDEO_NOTE}</p>')
    if ch.sources:
        out.append("<h2>Sources</h2>")
        out.append('<ol class="sources">' + "".join(_source(anchor, s) for s in ch.sources) + "</ol>")
    out.append("</section>")
    return "\n".join(out)


def first_picture(plan: BookPlan, links: Links) -> tuple[Picture, str] | None:
    """The first picture in the book whose file can be used, with its src."""
    pics = [p for ch in plan.chapters for p in ch.pictures]
    pics += [sc["picture"] for s in plan.stories for sc in s.scenes if sc.get("picture") is not None]
    for p in pics:
        src = links.picture(p)
        if src is not None:
            return p, src
    return None


def date_range(plan: BookPlan) -> str:
    first, last = day(plan.first_at), day(plan.last_at)
    if not first:
        return ""
    return first if first == last or not last else f"{first} to {last}"


def cover(plan: BookPlan, links: Links) -> str:
    out = ['<section class="cover" id="cover">', f'<h1 class="title">{escape(plan.title)}</h1>']
    if plan.by:
        out.append(f'<p class="by">By {escape(plan.by)}</p>')
    when = date_range(plan)
    if when:
        out.append(f'<p class="dates">{escape(when)}</p>')
    found = first_picture(plan, links)
    if found is not None:
        p, src = found
        out.append(f'<figure class="cover-picture"><img src="{escape(src)}" alt="{escape(p.caption)}"/></figure>')
    out.append("</section>")
    return "\n".join(out)


def stories(plan: BookPlan, links: Links) -> str:
    out = ['<section class="stories" id="stories">', "<h1>Stories</h1>"]
    for k, s in enumerate(plan.stories, start=1):
        out.append(f'<section class="story" id="story-{k}"><h2>{escape(s.title)}</h2>')
        for sc in s.scenes:
            if str(sc.get("title") or "").strip():
                out.append(f"<h3>{escape(sc['title'])}</h3>")
            pic = sc.get("picture")
            src = links.picture(pic) if pic is not None else None
            if src is not None:
                out.append(_figure(src, pic.caption))
            out.append(_paragraphs(sc.get("text") or ""))
        out.append("</section>")
    out.append("</section>")
    return "\n".join(out)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def closing(plan: BookPlan, pictures: int, missing: int) -> str:
    """The closing note: what is in the book, what was left out, and when it was read.
    `pictures` is how many pictures went in; `missing` how many more the writer couldn't find."""
    out = ['<section class="end" id="end">', "<h1>Closing note</h1>",
           f"<p>This book has {_count(len(plan.chapters), 'page', 'pages')} and "
           f"{_count(pictures, 'picture', 'pictures')}.</p>"]
    left = plan.left_out or {}
    if left.get("chapters_cap"):
        out.append(f"<p>{_count(left['chapters_cap'], 'more page was', 'more pages were')} left out: "
                   f"a book holds at most {MAX_CHAPTERS} pages.</p>")
    if left.get("pictures_cap"):
        out.append(f"<p>{_count(left['pictures_cap'], 'picture was', 'pictures were')} left out: "
                   f"a book holds at most {PICTURES_PER_CHAPTER} pictures a page and "
                   f"{MAX_PICTURES} in all.</p>")
    gone = int(left.get("missing_media") or 0) + missing
    if gone:
        out.append(f"<p>{_count(gone, 'picture', 'pictures')} could not be found, so "
                   f"{'it is' if gone == 1 else 'they are'} not here.</p>")
    first, last = day(plan.first_at), day(plan.last_at)
    if first and last:
        out.append(f"<p>Made with Primer, from what I read between {first} and {last}.</p>")
    else:
        out.append("<p>Made with Primer.</p>")
    out.append("</section>")
    return "\n".join(out)


def contents(entries: list[tuple[int, str, str]]) -> str:
    """Nested `ol`s for (depth, href, label) entries in order (`chapter_tree` gives the
    chapters' depths). A depth is never allowed to rise more than one level past the entry
    before it, so every nested list hangs from a link."""
    out: list[str] = []
    level = 0
    for depth, href, label in entries:
        depth = 1 if level == 0 else max(1, min(int(depth), level + 1))
        if depth > level:
            out.append("<ol>")
        else:
            out.append("</li>")
            while level > depth:
                out.append("</ol></li>")
                level -= 1
        out.append(f'<li><a href="{escape(href)}">{escape(label)}</a>')
        level = depth
    if level:
        out.append("</li>")
        while level > 1:
            out.append("</ol></li>")
            level -= 1
        out.append("</ol>")
    return "".join(out)


def chapter_tree(plan: BookPlan) -> list[tuple[int, int]]:
    """(chapter number, visible depth) in contents order: each chapter under the chapter it
    branched from (`parent_id`, which is always earlier in the book), children in trail order,
    roots (no parent in the book) in trail order. Below depth MAX_DEPTH nothing nests further:
    the descendants of a depth-6 entry follow it as siblings at depth 6, in trail order."""
    index = {ch.node_id: i for i, ch in enumerate(plan.chapters, start=1)}
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for i, ch in enumerate(plan.chapters, start=1):
        parent = index.get(ch.parent_id) if ch.parent_id else None
        if parent is not None and parent < i:
            children.setdefault(parent, []).append(i)
        else:
            roots.append(i)

    def descendants(i: int) -> list[int]:
        out, todo = [], list(children.get(i, ()))
        while todo:
            c = todo.pop()
            out.append(c)
            todo.extend(children.get(c, ()))
        return sorted(out)

    order: list[tuple[int, int]] = []
    stack = [(i, 1) for i in reversed(roots)]
    while stack:
        i, depth = stack.pop()
        order.append((i, depth))
        if depth >= MAX_DEPTH:
            order.extend((c, MAX_DEPTH) for c in descendants(i))
        else:
            stack.extend((c, depth + 1) for c in reversed(children.get(i, ())))
    return order


def contents_entries(plan: BookPlan, links: Links, stories_href: str | None,
                     end_href: str) -> list[tuple[int, str, str]]:
    entries = [(depth, links.chapter(i), plan.chapters[i - 1].title) for i, depth in chapter_tree(plan)]
    if stories_href is not None:
        entries.append((1, stories_href, "Stories"))
    entries.append((1, end_href, "Closing note"))
    return entries
