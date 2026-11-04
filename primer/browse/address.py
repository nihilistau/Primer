"""What the browser can open: public web URLs and kiwix:<book>/<path> articles.

Every address that leaves this module is "clean": absolute, no fragment, and with every
character that could end or bend a Markdown link destination (space, parens, angle
brackets, quotes, backslash, pipe, control characters, non-ASCII) percent-encoded.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

MAX_ADDRESS = 4096
_BOOK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
# characters left as they are in a path/query; everything else is percent-encoded
_SAFE = "-._~:/?@!$&*+,;=%"
_NETLOC = re.compile(r"[A-Za-z0-9.\-_:\[\]]+\Z")
_KIWIX_BASE = "http://kiwix.invalid/content/"  # only used to reuse urljoin's rules


@dataclass(frozen=True)
class Address:
    kind: str  # "web" | "reference"
    url: str   # the clean address string (http(s) URL or kiwix:<book>/<path>)
    book: str = ""
    path: str = ""  # decoded article path inside the book


def kiwix_address(book: str, path: str) -> str:
    return "kiwix:" + urllib.parse.quote(book, safe="") + "/" + urllib.parse.quote(path, safe="/")


def clean_url(url: str) -> str | None:
    """An absolute http(s) URL made safe to sit in a Markdown link; None if it can't be."""
    url = _CONTROLS.sub("", url.strip())
    try:
        sp = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    scheme = sp.scheme.lower()
    if scheme not in ("http", "https") or not sp.netloc:
        return None
    netloc = sp.netloc
    if not netloc.isascii():
        host, sep, port = netloc.partition(":")  # a non-ASCII host is never an IPv6 literal
        try:
            netloc = host.encode("idna").decode("ascii") + sep + port
        except UnicodeError:
            return None
    if not _NETLOC.match(netloc):
        return None  # userinfo (@), stray characters: not a plain host
    try:
        if not urllib.parse.urlsplit("//" + netloc).hostname:
            return None
    except ValueError:
        return None
    rest = urllib.parse.quote(sp.path, safe=_SAFE)
    if sp.query:
        rest += "?" + urllib.parse.quote(sp.query, safe=_SAFE)
    return f"{scheme}://{netloc.lower()}{rest}"


def _kiwix(rest: str) -> Address:
    rest = rest.split("#", 1)[0].split("?", 1)[0]
    book, _, raw = rest.partition("/")
    if not _BOOK.match(book):
        raise ValueError("that isn't an offline book name")
    path = urllib.parse.unquote(raw)
    segs = path.split("/")
    if (not raw or any(s in ("", ".", "..") for s in segs) or _CONTROLS.search(path)
            or "\\" in path):
        raise ValueError("that isn't an article address")
    return Address("reference", kiwix_address(book, path), book, path)


def parse(u: str) -> Address:
    """A typed or clicked address; ValueError with a short reason for anything else."""
    u = (u or "").strip()
    if not u:
        raise ValueError("no address")
    if len(u) > MAX_ADDRESS:
        raise ValueError("that address is too long")
    m = _SCHEME.match(u)
    scheme = m.group(1).lower() if m else ""
    if scheme == "kiwix":
        return _kiwix(u[m.end():])
    if scheme in ("http", "https"):
        url = clean_url(u)
        if url is None:
            raise ValueError("that address has no plain host")
        return Address("web", url)
    raise ValueError("can't open that kind of address")


def _resolve_kiwix(base: Address, href: str) -> str | None:
    href = href.split("#", 1)[0].split("?", 1)[0]
    if href.startswith("/"):
        if not href.startswith("/content/"):
            return None  # kiwix-serve's own pages (skins, catalog, search)
        try:
            return _kiwix(href[len("/content/"):]).url
        except ValueError:
            return None
    segs = [urllib.parse.quote(s, safe="") for s in base.path.split("/")[:-1]]
    for s in href.split("/"):
        if s in ("", "."):
            continue
        if s == "..":
            if segs:
                segs.pop()
            continue  # above the book's root: clamp
        segs.append(s)
    if len(segs) > 1 and urllib.parse.unquote(segs[0]) == base.book:
        segs = segs[1:]  # older ZIMs: ../<book>/A/Title
    if not segs:
        return None
    try:
        return _kiwix(base.book + "/" + "/".join(segs)).url
    except ValueError:
        return None


def resolve(base: Address, href: str) -> str | None:
    """A link on a page at base, as a clean address; None for in-page anchors and
    schemes the browser doesn't open (javascript:, data:, mailto:, ...). kiwix: links
    are only followed from kiwix articles."""
    # browsers drop tabs and newlines anywhere in a URL, and leading/trailing spaces
    href = _CONTROLS.sub("", href or "").strip()
    if not href or href.startswith("#") or len(href) > MAX_ADDRESS:
        return None
    m = _SCHEME.match(href)
    scheme = m.group(1).lower() if m else ""
    if scheme == "kiwix":
        if base.kind != "reference":
            return None  # a web page can't point the browser into the offline library
        try:
            return _kiwix(href[m.end():]).url
        except ValueError:
            return None
    if scheme and scheme not in ("http", "https"):
        return None
    if base.kind == "reference":
        if scheme or href.startswith("//"):
            return clean_url(href if scheme else "https:" + href)
        return _resolve_kiwix(base, href.replace("\\", "/"))
    try:
        joined = urllib.parse.urljoin(base.url, href)
    except ValueError:
        return None
    return clean_url(joined.split("#", 1)[0])
