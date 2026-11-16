"""The book as an ordered list of blocks, for the bound book in the UI.

Each block is `{kind, html, chapter}` (a chapter head also has its `node_id`, for "Open this page
in Primer"). `chapter` is the chapter's number from 1, or 0 for the front and back matter.
Every block's HTML is one element.

The HTML comes only from the Phase 13 builders (`xhtml`), so it is escaped, allowlisted and
cleaned already. Each section is parsed with `xml.etree` (the builders write well-formed XML)
and split at its top-level children; stray text between them becomes a `para`. The contents is
one block per entry and a timeline at most TIMELINE_CHUNK events a block, so a deep trail or a
long timeline never makes one block taller than many pages.

Every block is written out again by `_html`, a second guard that emits only what it parsed and
only what it allows: the elements the builders write (and `clean_svg`'s, in SVG), with names
compared case-insensitively; no `style`, `on…`, namespaced or `xmlns` attribute; `href` only on
`a`, to `#…` or http(s); `src` only on `img`, to our own `/v1/media/<64hex>.png`; and no
attribute whose value says "javascript:". In text, "javascript:" keeps its words with the colon
as a character reference. `_html` is idempotent, so a saved block can be checked with `guard`.

Pictures are `/v1/media/<name>` URLs through the media-name check, never data: URIs.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from primer.diagrams.svg import ALLOWED as SVG_ATTRS, ALLOWED_ELEMENTS

from . import xhtml
from .markdown import escape
from .model import BookPlan, Picture
from .plate import map_plate

KINDS = ("cover", "contents", "chapter-head", "para", "heading", "list", "picture", "plate",
         "timeline", "diagram", "sources", "story", "closing", "back")
SVG_NS = "http://www.w3.org/2000/svg"
TIMELINE_CHUNK = 8  # events in one timeline block
# the elements the builders and the Markdown renderer write, by lower-case name
HTML_TAGS = {t: t for t in ("section", "nav", "div", "header", "h1", "h2", "h3", "h4", "h5", "h6",
                            "p", "em", "strong", "ul", "ol", "li", "sup", "a", "br", "figure",
                            "figcaption", "img", "span", "cite")}
SVG_TAGS = {t.lower(): t for t in ALLOWED_ELEMENTS}
VOID = frozenset(("br", "img"))
# attribute names the builders emit; anything else (srcset, ping, formaction...) is dropped
HTML_ATTRS = frozenset(("id", "class", "href", "src", "alt", "value", "start", "title"))
SVG_ROOT_ATTRS = frozenset(("viewBox", "width", "height", "role", "aria-label"))
MAX_NEST = 64  # deeper markup is refused, never recursed into
_HREF = re.compile(r"#[\w.:-]*|https?://\S+", re.ASCII)
_MEDIA_SRC = re.compile(r"/v1/media/[0-9a-f]{64}\.png", re.ASCII)
_JS = re.compile(r"javascript\s*:", re.I)


def media_links(media_dir: Path) -> xhtml.Links:
    """Links within the bound book: chapter i is `#c{i}`, and a picture is its `/v1/media/` URL
    when its name passes the media-name check and its file is there, else None."""
    def picture(p: Picture) -> str | None:
        path = xhtml.picture_path(media_dir, p.file)
        return None if path is None else f"/v1/media/{path.name}"

    return xhtml.Links(chapter=lambda i: f"#c{i}", picture=picture)


def back(plan: BookPlan) -> str:
    """The back cover: the title, small, and "Made with Primer"."""
    return (f'<section class="back" id="back"><p class="title">{escape(plan.title)}</p>'
            "<p>Made with Primer</p></section>")


# the guard: writing a parsed element out again

def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _text(s: str | None) -> str:
    return _JS.sub(lambda m: m.group(0)[:-1] + "&#58;", escape(s)) if s else ""


def _attr_ok(tag: str, key: str, value: str, svg: bool = False) -> bool:
    k = key.lower()
    if svg:
        names = set(SVG_ATTRS.get(tag, ())) | {"id", "class"} | (SVG_ROOT_ATTRS if tag == "svg" else set())
        if key not in names:
            return False
    elif k not in HTML_ATTRS:
        return False
    if key.startswith("{") or k.startswith("on") or k in ("style", "xmlns") or _JS.search(value):
        return False
    if k == "href":
        return tag == "a" and _HREF.fullmatch(value.strip()) is not None
    if k == "src":
        return tag == "img" and _MEDIA_SRC.fullmatch(value.strip()) is not None
    return True


def _html(el: ET.Element, parent_ns: str = "", depth: int = 0) -> str:
    """The element and what it holds, through the guard; "" for an element it doesn't allow
    (or one nested deeper than MAX_NEST)."""
    if depth > MAX_NEST:
        return ""
    ns, name = _local(el.tag)
    allowed = HTML_TAGS if ns == "" else SVG_TAGS if ns == SVG_NS else {}
    tag = allowed.get(name.lower())
    if tag is None or (ns == SVG_NS and parent_ns != SVG_NS and tag != "svg"):
        return ""
    attrs = [f' xmlns="{ns}"'] if ns and ns != parent_ns else []
    attrs += [f' {k}="{_text(v)}"' for k, v in el.attrib.items() if _attr_ok(tag, k, v, ns == SVG_NS)]
    head = f"<{tag}{''.join(attrs)}"
    inner = _text(el.text) + "".join(_html(c, ns, depth + 1) + _text(c.tail) for c in el)
    if not inner and not ns and tag in VOID:
        return head + "/>"
    return f"{head}>{inner}</{tag}>"


def _parse(fragment: str) -> ET.Element:
    """The fragment under a wrapper element (a fragment may have several roots)."""
    return ET.fromstring(f"<root>{fragment}</root>")


def guard(html: str) -> bool:
    """True when `html` is one element that the guard writes back unchanged."""
    try:
        root = _parse(html)
    except (ET.ParseError, RecursionError):
        return False
    return (len(root) == 1 and not (root.text or "").strip() and not (root[0].tail or "").strip()
            and _html(root[0]) == html)


# splitting

def _wrap(tag: str, cls: str, children: list[ET.Element], el_id: str = "") -> str:
    ident = f' id="{escape(el_id)}"' if el_id else ""
    return f'<{tag} class="{cls}"{ident}>' + "".join(_html(c) for c in children) + f"</{tag}>"


def _classes(el: ET.Element) -> set[str]:
    return set(el.get("class", "").split())


def _kind(el: ET.Element) -> str:
    """A section's child (or a child of a chapter's text) by tag and class."""
    _ns, name = _local(el.tag)
    cls = _classes(el)
    if name == "figure":
        if "map" in cls:
            return "plate"
        if "diagram" in cls:
            return "diagram"
        return "picture"
    if name == "ol" and "timeline" in cls:
        return "timeline"
    if name == "ol" and "sources" in cls:
        return "sources"
    if name in ("ul", "ol"):
        return "list"
    if re.fullmatch(r"h[1-6]", name):
        return "heading"
    return "para"


def _block(kind: str, html: str, chapter: int = 0, **extra) -> dict:
    return {"kind": kind, "html": html, "chapter": chapter, **extra}


def _parts(el: ET.Element) -> list:
    """The element's children in order, with any non-blank text between them as a str."""
    out: list = []
    if (el.text or "").strip():
        out.append(el.text)
    for c in el:
        out.append(c)
        if (c.tail or "").strip():
            out.append(c.tail)
    return out


def _stray(text: str, chapter: int = 0) -> dict:
    return _block("para", f"<p>{_text(' '.join(text.split()))}</p>", chapter)


def _section(fragment: str) -> ET.Element:
    """The one section a builder writes."""
    return _parse(fragment)[0]


def _contents(plan: BookPlan, links: xhtml.Links) -> list[dict]:
    """The heading, then one block per entry, its depth as a class (`d1`..`d6`)."""
    entries = xhtml.contents_entries(plan, links, "#stories" if plan.stories else None, "#end")
    out = [_block("contents", '<h1 class="contents">Contents</h1>')]
    for depth, href, label in entries:
        html = (f'<ol class="contents d{int(depth)}"><li><a href="{escape(href)}">{escape(label)}'
                "</a></li></ol>")
        out.append(_block("contents", _html(_parse(html)[0])))
    return out


def _timeline(heading: ET.Element | None, ol: ET.Element, i: int) -> list[dict]:
    """The timeline in blocks of at most TIMELINE_CHUNK events; the first has the heading."""
    items = list(ol)
    out = []
    for k in range(0, max(len(items), 1), TIMELINE_CHUNK):
        chunk = items[k:k + TIMELINE_CHUNK]
        lis = "".join(_html(li) for li in chunk)
        if k == 0:
            head = _html(heading) if heading is not None else ""
            html = f'<div class="timeline">{head}<ol class="timeline">{lis}</ol></div>'
        else:
            html = f'<ol class="timeline more">{lis}</ol>'
        out.append(_block("timeline", html, i))
    return out


def _chapter(i: int, node_id: str, section: ET.Element) -> list[dict]:
    """Chapter i: its head (the title and the "From ... in ..." line), then each part."""
    head: list[ET.Element] = []
    out: list[dict] = []
    parts = _parts(section)
    k = 0
    while k < len(parts):
        child = parts[k]
        k += 1
        if isinstance(child, str):
            out.append(_stray(child, i))
            continue
        _ns, name = _local(child.tag)
        cls = _classes(child)
        if not out and (name == "h1" or (name == "p" and cls & {"subtitle", "via"})):
            head.append(child)
        elif name == "div" and "text" in cls:
            for c in _parts(child):
                out.append(_stray(c, i) if isinstance(c, str) else _block(_kind(c), _html(c), i))
        elif name == "ol" and "sources" in cls:
            out.extend(_block("sources", _wrap("ol", "sources", [li]), i) for li in child)
        elif (name == "h2" and k < len(parts) and not isinstance(parts[k], str)
              and _kind(parts[k]) == "timeline"):
            out.extend(_timeline(child, parts[k], i))
            k += 1
        elif _kind(child) == "timeline":
            out.extend(_timeline(None, child, i))
        else:
            out.append(_block(_kind(child), _html(child), i))
    first = _block("chapter-head", _wrap("div", "chapter-head", head, f"c{i}"), i, node_id=node_id)
    return [first] + out


def _stories(section: ET.Element) -> list[dict]:
    """The part's heading (anchored `stories`, where the contents links), then each story's
    headings as `story` blocks, with its scenes' pictures and paragraphs."""
    out = []

    def add(el) -> None:
        if isinstance(el, str):
            out.append(_stray(el))
            return
        kind = _kind(el)
        out.append(_block("story" if kind == "heading" else kind, _html(el)))

    for child in _parts(section):
        if not isinstance(child, str) and _local(child.tag)[1] == "section":
            for c in _parts(child):
                add(c)
            continue
        if not isinstance(child, str) and _local(child.tag)[1] == "h1":
            child.set("id", section.get("id", "stories"))
        add(child)
    return out


def blocks(plan: BookPlan, media_dir: Path, plate=map_plate, archive=None,
           plates: dict | None = None) -> list[dict]:
    """The book as blocks, in reading order: the cover, the contents, each chapter, the stories,
    the closing note and the back cover. `plates` is a job's plate cache (`xhtml.map_svg`)."""
    links = media_links(media_dir)
    used: set[str] = set()
    missing: set[str] = set()

    def picture(p: Picture) -> str | None:
        src = links.picture(p)
        (used if src is not None else missing).add(p.file)
        return src

    counting = xhtml.Links(chapter=links.chapter, picture=picture)
    out = [_block("cover", _html(_section(xhtml.cover(plan, counting))))]
    out.extend(_contents(plan, counting))
    index = {ch.node_id: i for i, ch in enumerate(plan.chapters, start=1)}
    for i, ch in enumerate(plan.chapters, start=1):
        section = _section(xhtml.chapter(i, ch, index, counting, plate, archive, plates))
        out.extend(_chapter(i, ch.node_id, section))
    if plan.stories:
        out.extend(_stories(_section(xhtml.stories(plan, counting))))
    out.append(_block("closing", _html(_section(xhtml.closing(plan, len(used), len(missing - used))))))
    out.append(_block("back", _html(_section(back(plan)))))
    return out
