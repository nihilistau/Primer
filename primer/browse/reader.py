"""HTML to inert Markdown: Primer's own converter for the browser lens.

The page is untrusted. Scripts, styles, frames, forms, SVG, hidden and furniture
elements are dropped while parsing (so even a page nested past the depth cap can't
leak them); every bit of source text is escaped so it can't make a link, an image,
HTML or structure; the only links and images are the ones built here, with clean
http(s)/kiwix destinations (images through the caller's proxy URL).
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable

from primer.browse.address import Address, clean_url, resolve
from primer.grounding.text import SKIP_CLASSES

MAX_MARKDOWN = 200_000
MAX_COLUMNS = 8
MAX_DEPTH = 100        # tree depth kept; deeper elements' content joins the deepest node
MAX_TITLE = 300
MAX_LINK_TEXT = 200
_SCAN = 64             # how far implicit closes (li, td, p...) look down the stack

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "iframe", "frame",
              "frameset", "noscript", "template", "svg", "math", "object", "embed", "applet",
              "canvas", "button", "select", "textarea", "video", "audio", "picture", "map",
              "dialog", "head", "noembed", "noframes", "link", "meta", "base", "input", "title"}
_VOID = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "embed", "source",
         "track", "wbr", "param", "keygen"}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 4, "h6": 4}
_BLOCKS = {"p", "div", "section", "article", "main", "body", "html", "figure", "figcaption",
           "dl", "dt", "dd", "center", "details", "summary", "address", "hgroup", "caption",
           "fieldset", "legend", "ul", "ol", "li", "blockquote", "pre", "table", "hr",
           "tr", "td", "th", "thead", "tbody", "tfoot", *_HEADINGS}
_COMPOSITE = {"ul", "ol", "blockquote", "pre", "table", "hr", *_HEADINGS}
_CLOSES_P =_BLOCKS - {"body", "html", "td", "th", "tr", "thead", "tbody", "tfoot", "caption"}
# ":" and "@" always, and "." at a text node's start or after w/W, so no GFM autolink
# (http://, www., a@b) can form even when the page splits it across elements
_ESCAPE = re.compile(r"[\\`*_\[\]<>|&~#:@]|(?:^|(?<=[wW]))\.")
# marked reads CR as a line break (and U+2028/2029/0085 are line breaks to others)
_NEWLINES = re.compile("\r\n?|[  \x85]")
_WS = re.compile(r"\s+")
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


@dataclass
class Page:
    title: str
    markdown: str
    links: list[dict] = field(default_factory=list)


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list = []


def _skips(tag: str, attrs: dict) -> bool:
    if tag in _SKIP_TAGS:
        return True
    cls = (attrs.get("class") or "").lower()
    if any(c in cls for c in SKIP_CLASSES) or (tag == "sup" and "reference" in cls):
        return True
    if "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(attrs.get("style") or ""))


class _Tree(HTMLParser):
    """A small, forgiving DOM: dropped elements never get nodes or text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        # (tag, node or None past the depth cap, skips)
        self.stack: list[tuple[str, _Node | None, bool]] = []
        self.open: dict[str, int] = {}
        self.skipping = 0
        self.nodes: list[_Node] = [self.root]  # the real nodes on the stack; [-1] is the parent
        self.first: dict[str, _Node] = {}
        self.articles = 0
        self.title: list[str] | None = None
        self.in_title = False

    def _pop_to(self, i: int) -> None:
        for tag, node, skips in self.stack[i:]:
            self.open[tag] -= 1
            self.skipping -= skips
            if node is not None:
                self.nodes.pop()
            if tag == "title":
                self.in_title = False
        del self.stack[i:]

    def _close(self, tags: set[str], boundary: set[str]) -> None:
        for i in range(len(self.stack) - 1, max(-1, len(self.stack) - 1 - _SCAN), -1):
            tag = self.stack[i][0]
            if tag in tags:
                self._pop_to(i)
                return
            if tag in boundary:
                return

    def handle_starttag(self, tag, attrs) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "li":
            self._close({"li"}, {"ul", "ol", "table"})
        elif tag in ("td", "th"):
            self._close({"td", "th"}, {"tr", "table"})
        elif tag == "tr":
            self._close({"tr"}, {"table"})
        elif tag in ("dt", "dd"):
            self._close({"dt", "dd"}, {"dl"})
        if tag in _CLOSES_P:
            self._close({"p"}, {"div", "li", "td", "th", "blockquote", "table", "section",
                                "article", "main", "body"})
        skips = _skips(tag, attrs)
        if tag == "title" and self.title is None and not (self.open.get("svg") or self.open.get("math")):
            self.title = []  # the first document title (not an SVG's), kept apart from the page
            self.in_title = True
        if tag in _VOID:
            if not skips and not self.skipping and tag in ("br", "img", "hr"):
                self.nodes[-1].children.append(_Node(tag, attrs))
            return
        node = None
        if not skips and not self.skipping and len(self.nodes) <= MAX_DEPTH:
            node = _Node(tag, attrs)
            self.nodes[-1].children.append(node)
            self.nodes.append(node)
            if tag in ("main", "article", "body", "h1") and tag not in self.first:
                self.first[tag] = node
            self.articles += tag == "article"
        self.stack.append((tag, node, skips))
        self.open[tag] = self.open.get(tag, 0) + 1
        self.skipping += skips

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag) -> None:
        if not self.open.get(tag):
            return  # nothing to close: O(1), so stray end tags can't make this quadratic
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self._pop_to(i)
                return

    def handle_data(self, data) -> None:
        if self.in_title and self.stack and self.stack[-1][0] == "title":
            self.title.append(data)
            return
        if self.skipping:
            return
        kids = self.nodes[-1].children
        if kids and isinstance(kids[-1], str):
            kids[-1] += data
        else:
            kids.append(data)


# --- rendering ----------------------------------------------------------------

def _esc(text: str) -> str:
    return _ESCAPE.sub(lambda m: "\\" + m.group(0), text)


def _line_start(line: str) -> str:
    """Escape what would make a line a list, rule or setext heading."""
    if line[:1] in ("-", "+", "="):
        return "\\" + line
    m = re.match(r"\d+(?=[.)])", line)
    if m:
        return line[:m.end()] + "\\" + line[m.end():]
    return line


def _para(inline: str) -> str:
    lines = [ln.strip() for ln in inline.split("\n")]
    return "  \n".join(_line_start(ln) for ln in lines if ln)


def _plain(node) -> str:
    """Unescaped text of a node (for link texts, titles, pre)."""
    if isinstance(node, str):
        return node
    if node.tag == "img":
        return node.attrs.get("alt", "")
    if node.tag == "br":
        return "\n"
    return "".join(_plain(c) for c in node.children)


class _Render:
    def __init__(self, base: Address, image_url: Callable[[str], str]) -> None:
        self.base = base
        self.image_url = image_url
        self.emitted: list[tuple[str, str]] = []  # (url, text) of every link written
        self.marks: list[int] = []  # per top-level block: len(emitted) once it was written
        self.size = 0

    # inline: escaped Markdown, "\n" for a line break
    def inline(self, node, in_link: bool = False) -> str:
        if isinstance(node, str):
            return _esc(_WS.sub(" ", _TEXT_CONTROLS.sub("", node)))
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag == "img":
            return self.image(node)
        inner = "".join(self.inline(c, in_link or tag == "a") for c in node.children)
        if tag in _BLOCKS:
            inner = f" {inner} "
        if tag == "a" and not in_link:
            url = resolve(self.base, node.attrs.get("href", ""))
            if url and inner.strip():
                text = _WS.sub(" ", _TEXT_CONTROLS.sub("", _plain(node))).strip()[:MAX_LINK_TEXT]
                self.emitted.append((url, text or url))
                return f"[{inner.strip()}]({url})"
        return inner

    def image(self, node) -> str:
        attrs = node.attrs
        for dim in ("width", "height"):
            if attrs.get(dim, "").isdigit() and int(attrs[dim]) < 20:
                return ""  # icons and spacers
        src = resolve(self.base, attrs.get("src", ""))
        if not src:
            return ""
        proxied = self.image_url(src)
        url = clean_url(proxied) if re.match(r"(?i)https?:", proxied) else _rel(proxied)
        if not url:
            return ""
        alt = _esc(_WS.sub(" ", _TEXT_CONTROLS.sub("", attrs.get("alt", ""))).strip())
        return f"![{alt}]({url})"

    def blocks(self, node, top: bool = False) -> list[str]:
        """Render a container's children as blocks; inline runs become paragraphs.
        top: these are the page's own blocks, so record each one's link mark."""
        out: list[str] = []
        buf: list[str] = []

        def flush() -> None:
            if buf:
                p = _para("".join(buf))
                buf.clear()
                if p:
                    out.append(p)
                    self.size += len(p) + 2
                    if top:
                        self.marks.append(len(self.emitted))

        for child in node.children:
            if self.size > MAX_MARKDOWN:
                break
            if isinstance(child, str) or child.tag not in _BLOCKS:
                buf.append(self.inline(child))
                continue
            flush()
            if top and child.tag not in _COMPOSITE:
                out.extend(self.blocks(child, True))  # a plain container: its blocks are ours
                continue
            res = self.block(child)
            if res and top:
                res = ["\n\n".join(res)]
                self.marks.append(len(self.emitted))
            out.extend(res)
        flush()
        return out

    def block(self, node) -> list[str]:
        out = self._block(node)
        if node.tag in _HEADINGS or node.tag in ("pre", "table", "hr"):
            self.size += sum(len(b) + 2 for b in out)  # leaves; paragraphs count in blocks()
        return out

    def _block(self, node) -> list[str]:
        tag = node.tag
        if tag in _HEADINGS:
            text = " ".join(self.inline(node).split())
            return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
        if tag in ("ul", "ol"):
            return self.list(node)
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(node))
            return ["\n".join(("> " + ln) if ln else ">" for ln in inner.split("\n"))] if inner else []
        if tag == "pre":
            text = _TEXT_CONTROLS.sub("", _NEWLINES.sub("\n", _plain(node))).strip("\n").rstrip()
            if not text:
                return []
            runs = [len(r) for r in re.findall(r"`+", text)]
            fence = "`" * max(3, max(runs, default=0) + 1)
            return [f"{fence}\n{text}\n{fence}"]
        if tag == "table":
            return self.table(node)
        if tag == "hr":
            return ["---"]
        return self.blocks(node)

    def list(self, node) -> list[str]:
        ordered = node.tag == "ol"
        start = node.attrs.get("start", "")
        n = int(start) if start.isdigit() and len(start) < 9 else 1
        items = []
        for child in node.children:
            if isinstance(child, str):
                if child.strip():
                    items.append(_para(self.inline(child)))
                continue
            if child.tag in ("ul", "ol") and items:
                items[-1] += "\n" + "\n".join(self.list(child))
                continue
            parts = self.blocks(child) if child.tag == "li" else self.block(child) \
                if child.tag in _BLOCKS else [_para(self.inline(child))]
            body = ""
            for p in parts:
                if not p:
                    continue
                sep = "\n" if p.startswith(("- ", "1. ")) or re.match(r"\d+\. ", p) else "\n\n"
                body = p if not body else body + sep + p
            if body:
                items.append(body)
        out = []
        for body in items:
            prefix = f"{n}. " if ordered else "- "
            pad = " " * len(prefix)
            lines = body.split("\n")
            out.append(prefix + lines[0] + "".join("\n" + (pad + ln if ln else "") for ln in lines[1:]))
            n += 1
        return ["\n".join(out)] if out else []

    def table(self, node) -> list[str]:
        rows: list[list[str]] = []
        caption = []

        def walk(n) -> None:
            for c in n.children:
                if isinstance(c, str):
                    continue
                if c.tag == "tr":
                    cells = [" ".join(self.inline(td).split()) for td in c.children
                             if not isinstance(td, str) and td.tag in ("td", "th")]
                    if any(cells):
                        rows.append(cells)
                elif c.tag == "caption":
                    caption.append(" ".join(self.inline(c).split()))
                elif c.tag in ("thead", "tbody", "tfoot"):
                    walk(c)

        walk(node)
        out = [_line_start(c) for c in caption if c]
        cols = min(MAX_COLUMNS, max((len(r) for r in rows), default=0))
        if cols == 1:
            return out + [_line_start(r[0]) for r in rows if r[0]]
        if cols:
            rows = [(r + [""] * cols)[:cols] for r in rows]
            lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * cols]
            lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            out.append("\n".join(lines))
        return out


def _rel(u: str) -> str | None:
    """Primer's own relative proxy URL (from image_url), percent-encoded like the rest."""
    if not u.startswith("/") or u.startswith("//"):
        return None
    return urllib.parse.quote(_TEXT_CONTROLS.sub("", u), safe="-._~:/?@!$&*+,;=%")


def _cap(blocks: list[str]) -> tuple[str, int]:
    """The Markdown within MAX_MARKDOWN, and how many blocks it holds whole."""
    out, size = [], 0
    for b in blocks:
        if size + len(b) + 2 > MAX_MARKDOWN:
            if not out:
                return b[:MAX_MARKDOWN], 0
            break
        out.append(b)
        size += len(b) + 2
    return "\n\n".join(out)[:MAX_MARKDOWN], len(out)


def read(html: str, base: Address, image_url: Callable[[str], str]) -> Page:
    html = _NEWLINES.sub("\n", html)
    tree = _Tree()
    tree.feed(html)
    tree.close()
    root = (tree.first.get("main")
            or (tree.first.get("article") if tree.articles == 1 else None)
            or tree.first.get("body") or tree.root)
    r = _Render(base, image_url)
    blocks = r.blocks(root, top=True)
    markdown, whole = _cap(blocks)
    title = _WS.sub(" ", _TEXT_CONTROLS.sub("", "".join(tree.title or []))).strip()
    if not title and "h1" in tree.first:
        title = _WS.sub(" ", _TEXT_CONTROLS.sub("", _plain(tree.first["h1"]))).strip()
    first: dict[str, str] = {}
    for url, text in r.emitted[:r.marks[whole - 1] if whole else 0]:  # links in kept blocks
        first.setdefault(url, text)
    links = [{"n": i, "text": t, "url": u} for i, (u, t) in enumerate(first.items(), 1)]
    return Page(title[:MAX_TITLE], markdown, links)
