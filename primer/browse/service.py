"""The browse service: open a page as Markdown, search, and proxy a picture.

Web addresses go only through the guarded fetchers in primer.grounding.web; kiwix: addresses
only through KiwixClient. Primer never serves a third party's HTML: pages leave as Markdown
built by primer.browse.reader, pictures leave only after their bytes are sniffed.
"""
from __future__ import annotations

import copy
import html
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Callable

from primer.browse.address import Address, kiwix_address, parse
from primer.browse.reader import read
from primer.grounding.grounder import plain_title
from primer.grounding.kiwix import KiwixClient, KiwixError, KiwixTooLarge
from primer.grounding.web import (WebError, WebRefused, WebTooLarge, WebUnsupported,
                                  safe_fetch_bytes, safe_fetch_html)

CACHE_PAGES = 50
CACHE_SECONDS = 600.0
IMAGE_MAX = 5_000_000
SEARCH_RESULTS = 8
IMAGE_ENDPOINT = "/v1/browse/image?u="
WEB_OFF = "Web is off in this tier"
NO_REFERENCE = "There is no offline encyclopedia in this tier"


class BrowseError(Exception):
    """A request the browser can't serve; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def image_proxy_url(src: str) -> str:
    """The Primer URL that serves the picture at src (an http(s) URL or kiwix: address)."""
    return IMAGE_ENDPOINT + urllib.parse.quote(src, safe="")


def sniff(data: bytes) -> str | None:
    """The picture type from its first bytes: PNG, JPEG, GIF or WebP; anything else (SVG
    included) is None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _web_failure(exc: WebError) -> BrowseError:
    if isinstance(exc, WebRefused):
        return BrowseError(403, str(exc))
    if isinstance(exc, WebTooLarge):
        return BrowseError(413, str(exc))
    if isinstance(exc, WebUnsupported):
        return BrowseError(415, str(exc))
    if exc.http_status in (404, 410):
        return BrowseError(404, str(exc))
    return BrowseError(502, str(exc))


def _kiwix_failure(exc: KiwixError) -> BrowseError:
    if isinstance(exc, KiwixTooLarge):
        return BrowseError(413, "that picture is too large")
    if exc.http_status == 404:
        return BrowseError(404, "that offline article doesn't exist")
    return BrowseError(502, str(exc))


class BrowseService:
    def __init__(self, kiwix: KiwixClient | None, web: bool, searcher,
                 fetch_html: Callable = safe_fetch_html, fetch_bytes: Callable = safe_fetch_bytes,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.kiwix = kiwix
        self.web = web
        self.searcher = searcher
        self.fetch_html = fetch_html
        self.fetch_bytes = fetch_bytes
        self.clock = clock
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    # -- addresses
    def _address(self, u: str) -> Address:
        try:
            addr = parse(u)
        except ValueError as exc:
            raise BrowseError(400, str(exc)) from None
        if addr.kind == "web" and not self.web:
            raise BrowseError(503, f"{WEB_OFF}; only the offline encyclopedia can be opened")
        if addr.kind == "reference" and self.kiwix is None:
            raise BrowseError(503, NO_REFERENCE)
        return addr

    # -- pages
    def open(self, u: str) -> dict:
        addr = self._address(u)
        now = self.clock()
        with self._lock:
            hit = self._cache.get(addr.url)
            if hit is not None and now < hit[0]:
                self._cache.move_to_end(addr.url)
                return copy.deepcopy(hit[1])
        out = self._load(addr)
        with self._lock:
            self._cache[addr.url] = (self.clock() + CACHE_SECONDS, out)
            self._cache.move_to_end(addr.url)
            while len(self._cache) > CACHE_PAGES:
                self._cache.popitem(last=False)
        return copy.deepcopy(out)

    def _load(self, addr: Address) -> dict:
        if addr.kind == "reference":
            try:
                raw = self.kiwix.article_html(addr.book, addr.path)
            except KiwixError as exc:
                raise _kiwix_failure(exc) from None
            final, origin = addr.url, self.kiwix.href(addr.book, addr.path)
            base = addr
        else:
            try:
                final, ctype, raw = self.fetch_html(addr.url)
            except WebError as exc:
                raise _web_failure(exc) from None
            if ctype == "text/plain":
                raw = "<pre>" + html.escape(raw) + "</pre>"
            try:
                base = parse(final)  # links resolve against where the page really was
            except ValueError:
                base = addr
            final = base.url
            origin = final
        page = read(raw, base, image_proxy_url)
        return {"url": addr.url, "final_url": final, "title": page.title, "markdown": page.markdown,
                "links": page.links, "where": addr.kind, "origin_href": origin}

    # -- search
    def search(self, q: str, where: str = "") -> list[dict]:
        q = (q or "").strip()
        if not q:
            raise BrowseError(400, "nothing to search for")
        if where not in ("", "web", "reference"):
            raise BrowseError(400, "where must be web or reference")
        searcher = self.searcher if self.web else None
        if not where:
            where = "web" if searcher is not None else "reference"
        if where == "web":
            if searcher is None:
                raise BrowseError(503, f"{WEB_OFF}; search the offline encyclopedia instead")
            try:
                rows = searcher.search(q, SEARCH_RESULTS)
            except Exception as exc:  # WebError, or a bug in an engine: a bad gateway either way
                raise BrowseError(502, (str(exc).strip() or type(exc).__name__)[:200]) from None
            out = []
            for r in rows:
                try:
                    url = parse(r.get("url") or "").url
                except ValueError:
                    continue
                out.append({"title": plain_title(r.get("title") or "") or url, "url": url,
                            "snippet": plain_title(r.get("snippet") or "")})
            return out
        if self.kiwix is None:
            raise BrowseError(503, NO_REFERENCE)
        try:
            rows = self.kiwix.search(q, SEARCH_RESULTS)
        except KiwixError as exc:
            raise _kiwix_failure(exc) from None
        return [{"title": plain_title(r["title"]) or r["path"],
                 "url": kiwix_address(r["book"], r["path"]),
                 "snippet": plain_title(r.get("snippet") or "")} for r in rows]

    # -- pictures
    def image(self, u: str) -> tuple[bytes, str]:
        addr = self._address(u)
        if addr.kind == "reference":
            try:
                data = self.kiwix.content(addr.book, addr.path, IMAGE_MAX)
            except KiwixError as exc:
                raise _kiwix_failure(exc) from None
        else:
            try:
                _, _, data = self.fetch_bytes(addr.url)
            except WebError as exc:
                raise _web_failure(exc) from None
        ctype = sniff(data)
        if ctype is None:
            raise BrowseError(415, "that isn't a picture Primer will show")
        return data, ctype
