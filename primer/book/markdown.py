"""A page's Markdown as a safe XHTML fragment for the book.

Only h1-h4, p, em, strong, ul, ol, li, sup, a and br come out. Raw HTML in the Markdown is
shown as text. Links go only to `#` anchors in the book (citations) or to public http(s)
addresses. Every line is capped, and no pattern can backtrack badly.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Container, Iterable
from urllib.parse import urlsplit

MAX_LINE = 20_000
MAX_SOURCES_N = 9999

# characters XML 1.0 can't hold (tab, newline and carriage return are kept)
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "=": "&#61;"}
_ESCAPE = re.compile("[&<>\"'=]")

_HEADING = re.compile(r"(#{1,6})[ \t]+(.*)")
_BULLET = re.compile(r"[ \t]*[-*+][ \t]+(.*)")
_NUMBERED = re.compile(r"[ \t]*\d{1,9}[.)][ \t]+(.*)")
_RULE = re.compile(r"[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*")
_FENCE = re.compile(r"[ \t]*(```|~~~)")
_QUOTE = re.compile(r"[ \t]*>[ \t]?(.*)")
# a link [text](url) (one level of parentheses in the url) or a citation [n] / [n, m];
# bounded, no nested brackets, and the url's alternatives start differently (no backtracking)
_INLINE = re.compile(r"\[([^\[\]\n]{1,500})\]\(((?:[^()\s]|\([^()\s]{0,200}\)){1,2000})\)|\[(\d{1,4}(?:[ \t]*,[ \t]*\d{1,4}){0,9})\]")
# emphasis opens before a non-space and closes after one, never touching another star
_STRONG = re.compile(r"(?<!\*)\*\*(?![\s*])([^*]+)(?<!\s)\*\*(?!\*)")
_EM = re.compile(r"(?<![*\w])\*(?![\s*])([^*]+)(?<!\s)\*(?![*\w])")


def escape(text: str) -> str:
    """Text made safe for XML content or a quoted attribute; characters XML can't hold are dropped."""
    return _ESCAPE.sub(lambda m: _ESCAPES[m.group()], _BAD_XML.sub("", str(text)))


_NUMERIC_LABEL = re.compile(r"[0-9]*|0x[0-9a-f]*")
_HOST_CHARS = re.compile(r"[^\W_]([\w.-]*[^\W_])?")


def safe_url(url: str) -> str | None:
    """The URL when it is http(s) to a public host written in ASCII, else None (no local or
    private addresses, and no host in other scripts, which may fold to one)."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if "@" in parts.netloc:  # user:password@host hides where the link really goes
        return None
    host = host.lower().rstrip(".")
    # browsers fold other scripts' digits and dots to ASCII (fullwidth "１２７．０．０．１" is
    # 127.0.0.1), so a host that isn't ASCII can't be checked here: it is shown as text
    if not host.isascii():
        return None
    # a host name is letters, digits, dots and hyphens; '%' escapes or spaces hide a real address
    if not _HOST_CHARS.fullmatch(host):
        return None
    # a host of only digits, dots and 0x-hex is an address in disguise (127.1, 0x7f.1, 2130706433)
    if all(_NUMERIC_LABEL.fullmatch(label) for label in host.split(".")):
        return None
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if "." not in host:  # a bare name only means something on the local network
            return None
    return url.strip()


def _emphasis(escaped: str) -> str:
    escaped = _STRONG.sub(r"<strong>\1</strong>", escaped)
    return _EM.sub(r"<em>\1</em>", escaped)


def _inline(text: str, anchor: str, numbers: Container[int]) -> str:
    out, at = [], 0
    for m in _INLINE.finditer(text):
        out.append(_emphasis(escape(text[at:m.start()])))
        at = m.end()
        if m.group(3) is not None:
            nums = [int(x) for x in m.group(3).split(",")]
            if all(n in numbers for n in nums):
                out.append("".join(f'<sup><a href="#{escape(anchor)}-src-{n}">{n}</a></sup>' for n in nums))
            else:
                out.append(escape(m.group()))
            continue
        label, url = m.group(1), safe_url(m.group(2))
        if url is None:
            out.append(_emphasis(escape(label)))
        else:
            out.append(f'<a href="{escape(url)}">{_emphasis(escape(label))}</a>')
    out.append(_emphasis(escape(text[at:])))
    return "".join(out)


def _indent(line: str) -> int:
    """The line's leading whitespace in columns, a tab reaching the next multiple of 4."""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _dedent4(line: str) -> str:
    """The line without its first 4 columns of indent (a code line as written)."""
    width, i = 0, 0
    while i < len(line) and width < 4 and line[i] in " \t":
        width += 1 if line[i] == " " else 4 - width % 4
        i += 1
    return line[i:]


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + "…"


def render(md: str, *, anchor: str, n_sources: int, numbers: Iterable[int] | None = None) -> str:
    """The page's Markdown as an XHTML fragment (no wrapper). The leading `# Title` line is
    dropped, since the chapter writes its own title. `[n]` becomes a superscript link to
    `#{anchor}-src-{n}` when n is one of `numbers` (the page's stored source numbers, which
    need not run 1..k), or, when `numbers` is not given, when 1 <= n <= n_sources."""
    if numbers is not None:
        cited: Container[int] = frozenset(int(n) for n in numbers)
    else:
        cited = range(1, max(0, min(int(n_sources), MAX_SOURCES_N)) + 1)
    lines = _BAD_XML.sub("", str(md or "")).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and _indent(lines[first]) < 4 and re.match(r"#[ \t]", lines[first].lstrip()):
        lines = lines[first + 1:]

    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    list_tag = ""
    fenced: list[str] | None = None
    code: list[str] | None = None  # lines indented 4 or more spaces, after a blank or a block
    code_blanks = 0  # blank lines met inside a code block, kept if more code follows

    def end_code():
        nonlocal code, code_blanks
        if code:
            blocks.append("<p>" + "<br/>".join(escape(x) for x in code) + "</p>")
        code, code_blanks = None, 0

    def end_para():
        if para:
            blocks.append("<p>" + " ".join(_inline(p, anchor, cited) for p in para) + "</p>")
            para.clear()

    def end_list():
        nonlocal list_tag
        if items:
            blocks.append(f"<{list_tag}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{list_tag}>")
            items.clear()
        list_tag = ""

    for raw in lines:
        line = _clip(raw.rstrip())
        if fenced is not None:  # code: shown line by line as plain text
            if _FENCE.match(line):
                if fenced:
                    blocks.append("<p>" + "<br/>".join(escape(x) for x in fenced) + "</p>")
                fenced = None
            else:
                fenced.append(line)
            continue
        # indented 4 or more spaces: code, never a heading, list or fence (inside a list it is
        # the list's own: a nested item or a continued one, as before)
        if code is not None and not line.strip():  # a blank inside code: held, not an end
            code_blanks += 1
            continue
        if line.strip() and _indent(line) >= 4 and not items:
            if code is not None:
                code.extend([""] * code_blanks)
                code_blanks = 0
                code.append(_dedent4(line))
            elif para:  # a continued paragraph
                para.append(line.strip())
            else:
                code = [_dedent4(line)]
            continue
        end_code()
        if _FENCE.match(line):
            end_para()
            end_list()
            fenced = []
            continue
        if not line.strip() or _RULE.fullmatch(line):
            end_para()
            end_list()
            continue
        m = _HEADING.fullmatch(line.lstrip())
        if m:
            end_para()
            end_list()
            level = min(len(m.group(1)), 4)
            text = m.group(2).rstrip(" \t#").strip()
            if text:
                blocks.append(f"<h{level}>{_inline(text, anchor, cited)}</h{level}>")
            continue
        bullet, numbered = _BULLET.fullmatch(line), _NUMBERED.fullmatch(line)
        if bullet or numbered:
            end_para()
            tag = "ul" if bullet else "ol"
            if tag != list_tag:
                end_list()
                list_tag = tag
            items.append(_inline((bullet or numbered).group(1).strip(), anchor, cited))
            continue
        if items and raw[:1] in (" ", "\t"):  # a continued list item
            items[-1] += " " + _inline(line.strip(), anchor, cited)
            continue
        end_list()
        q = _QUOTE.fullmatch(line)
        para.append((q.group(1) if q else line).strip())
    if fenced:
        blocks.append("<p>" + "<br/>".join(escape(x) for x in fenced) + "</p>")
    end_code()
    end_para()
    end_list()
    return "\n".join(blocks)
