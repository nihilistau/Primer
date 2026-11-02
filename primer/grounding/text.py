"""HTML to plain paragraphs, and cutting text to a budget."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "table", "figure"}
_SKIP_CLASSES = ("infobox", "navbox", "reflist", "references", "mw-editsection", "hatnote")
_VOID = {"br", "img", "hr", "meta", "link", "input", "area", "base", "col", "embed", "source",
         "track", "wbr"}
_BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "ul", "ol",
           "blockquote", "br", "dd", "dt", "pre"}
_MAIN = {"article", "main"}


class _Extract(HTMLParser):
    def __init__(self, main_only: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.main_only = main_only
        self.stack: list[tuple[str, bool, bool]] = []  # (tag, skips, is_main)
        self.paras: list[str] = []
        self.buf: list[str] = []

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if text:
            self.paras.append(text)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _VOID:
            if tag in _BLOCKS and not self._skipping():
                self._flush()
            return
        cls = (dict(attrs).get("class") or "").lower()
        skips = (tag in _SKIP_TAGS or any(c in cls for c in _SKIP_CLASSES)
                 or (tag == "sup" and "reference" in cls))
        if tag in _BLOCKS and not self._skipping():
            self._flush()
        self.stack.append((tag, skips, tag in _MAIN))

    def handle_endtag(self, tag) -> None:
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                skipping_before = self._skipping()
                del self.stack[i:]
                if tag in _BLOCKS and not skipping_before:
                    self._flush()
                return

    def _skipping(self) -> bool:
        return any(s for _, s, _m in self.stack)

    def handle_data(self, data) -> None:
        if self._skipping():
            return
        if self.main_only and not any(m for _, _s, m in self.stack):
            return
        self.buf.append(data)

    def result(self) -> str:
        self._flush()
        return "\n\n".join(self.paras)


def html_to_text(html: str, main_only: bool = True) -> str:
    has_main = main_only and re.search(r"<(article|main)\b", html, re.I) is not None
    p = _Extract(has_main)
    p.feed(html)
    p.close()
    return p.result()


def clip(text: str, budget: int) -> str:
    """Cut to at most budget chars, at a sentence end when one falls in the last 30%."""
    if len(text) <= budget:
        return text
    cut = text[:budget]
    end = -1
    for m in re.finditer(r"[.!?][\"')\]]*(?=\s|$)", cut):
        end = m.end()
    if end >= budget * 0.7:
        return cut[:end]
    return cut.rstrip()
