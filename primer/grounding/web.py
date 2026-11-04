"""Web search and a guarded fetch (adapted from Kairos harness/skills/search.py).

Unlike the local servers, these are outbound internet requests, so they may use the
system proxy. Search results and fetched URLs come from the internet: safe_fetch
refuses anything that is not a public http(s) address.
"""
from __future__ import annotations

import html as _html
import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from primer.config import REPO_ROOT, WebConfig
from primer.grounding.text import html_to_text

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 10.0
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3
_TYPES = ("text/html", "text/plain")


class WebError(Exception):
    """A web fetch or search failed. `http_status` is the page's HTTP status when it said no."""

    def __init__(self, message: str = "", http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class WebRefused(WebError):
    """The guard refused the address (not public, not plain, not http(s))."""


class WebTooLarge(WebError):
    """The body is over the size cap."""


class WebUnsupported(WebError):
    """The server answered with a content type this fetch doesn't take."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # the 3xx surfaces as an HTTPError; safe_fetch follows it by hand


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows http(s) redirects only, and drops credentials when the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            return None
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).netloc.lower()
                                != urllib.parse.urlsplit(req.full_url).netloc.lower()):
            for h in ("X-subscription-token", "Authorization", "Cookie"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


def build_opener(proxies=None, follow=False):
    """An opener; follow=False never follows redirects (safe_fetch does it by hand).
    proxies=None uses the system proxy."""
    handlers = [_SafeRedirect()] if follow else [_NoRedirect()]
    if proxies is not None:
        handlers.append(urllib.request.ProxyHandler(proxies))
    return urllib.request.build_opener(*handlers)


def _clean(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _fail(exc: Exception) -> WebError:
    if isinstance(exc, urllib.error.HTTPError):
        return WebError(f"the web request returned HTTP {exc.code}")
    return WebError(f"web request failed: {getattr(exc, 'reason', None) or exc}")


def _http_url(url: str) -> bool:
    """True only for an http(s) URL with a host (drops javascript:, relative ad links...)."""
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in ("http", "https") and bool(p.hostname)
    except ValueError:
        return False


_NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException, ValueError)


def _is_loopback(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Searcher:
    name = "searcher"

    def __init__(self, opener=None) -> None:
        # searchers talk to the configured endpoint, so they may follow its redirects
        self.opener = opener or build_opener(follow=True)

    def _read(self, req: urllib.request.Request) -> str:
        try:
            with self.opener.open(req, timeout=TIMEOUT) as resp:
                return resp.read(MAX_BYTES).decode("utf-8", "replace")
        except _NET_ERRORS as exc:
            raise _fail(exc) from None

    def _json(self, req: urllib.request.Request) -> dict:
        try:
            obj = json.loads(self._read(req))
        except ValueError:
            raise WebError("search engine sent unreadable JSON") from None
        if not isinstance(obj, dict):
            raise WebError("search engine sent unreadable JSON")
        return obj

    def search(self, query: str, n: int = 5) -> list[dict]:
        raise NotImplementedError


_DDG_RESULT = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>)?', re.S | re.I)


class DuckDuckGo(Searcher):
    name = "duckduckgo"

    def search(self, query: str, n: int = 5) -> list[dict]:
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=urllib.parse.urlencode({"q": query}).encode(),
            headers={"User-Agent": UA})
        out = []
        for href, title, snip in _DDG_RESULT.findall(self._read(req)):
            url = _html.unescape(href)
            m = re.search(r"[?&]uddg=([^&]+)", url)
            if m:
                url = urllib.parse.unquote(m.group(1))
            elif url.startswith("//"):
                url = "https:" + url
            if not _http_url(url):
                continue
            out.append({"title": _clean(title), "url": url, "snippet": _clean(snip)})
            if len(out) >= n:
                break
        return out


class SearXNG(Searcher):
    name = "searxng"

    def __init__(self, url: str, opener=None) -> None:
        self.url = url.rstrip("/")
        if opener is None and _is_loopback(self.url):
            opener = build_opener(proxies={}, follow=True)  # a local engine: never via a proxy
        super().__init__(opener)

    def search(self, query: str, n: int = 5) -> list[dict]:
        url = self.url + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "en"})
        obj = self._json(urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}))
        rows = [h for h in (obj.get("results") or [])
                if isinstance(h, dict) and _http_url((h.get("url") or "").strip())][:n]
        return [{"title": (h.get("title") or "").strip(), "url": (h.get("url") or "").strip(),
                 "snippet": (h.get("content") or "").strip()} for h in rows]


class Brave(Searcher):
    name = "brave"

    def __init__(self, key_file: str, opener=None) -> None:
        super().__init__(opener)
        self.key_file = key_file

    def _key(self) -> str:
        try:
            # relative to the repo root, like engine api_key_file; absolute paths stand
            key = (REPO_ROOT / self.key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WebError(f"cannot read api key file: {exc.strerror or exc}") from None
        if not key:
            raise WebError("the api key file is empty")
        return key

    def search(self, query: str, n: int = 5) -> list[dict]:
        key = self._key()
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": max(1, min(int(n), 20))})
        obj = self._json(urllib.request.Request(url, headers={
            "Accept": "application/json", "X-Subscription-Token": key, "User-Agent": UA}))
        rows = [h for h in ((obj.get("web") or {}).get("results") or [])
                if isinstance(h, dict) and _http_url((h.get("url") or "").strip())][:n]
        return [{"title": (h.get("title") or "").strip(), "url": (h.get("url") or "").strip(),
                 "snippet": (h.get("description") or "").strip()} for h in rows]


def make_searcher(cfg: WebConfig) -> Searcher | None:
    if cfg.search == "duckduckgo":
        return DuckDuckGo()
    if cfg.search == "searxng":
        return SearXNG(cfg.searxng_url)
    if cfg.search == "brave":
        return Brave(cfg.api_key_file)
    return None


# --- guarded fetch -----------------------------------------------------------

def _embedded_v4(a: ipaddress.IPv6Address):
    """IPv4 addresses an IPv6 address can smuggle: mapped, 6to4, ::/96, 64:ff9b::/96."""
    out = []
    if a.ipv4_mapped:
        out.append(a.ipv4_mapped)
    if a.sixtofour:
        out.append(a.sixtofour)
    top = a.packed[:12]
    if top == bytes(12) or top == bytes.fromhex("0064ff9b" + "00" * 8):
        out.append(ipaddress.IPv4Address(a.packed[12:]))
    return out


def _check_host(host: str, resolve) -> None:
    """Refuse unless every address the host maps to is public (ipaddress is_global)."""
    if not host:
        raise WebRefused("the URL has no host")
    try:
        addrs = [ipaddress.ip_address(host.split("%", 1)[0])]
    except ValueError:
        try:
            infos = resolve(host, None)
        except (OSError, ValueError) as exc:
            raise WebError(f"cannot resolve {host}: {exc}") from None
        try:
            addrs = [ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]
        except (ValueError, IndexError, TypeError):
            raise WebError(f"cannot resolve {host}") from None
        if not addrs:
            raise WebError(f"cannot resolve {host}")
    for a in list(addrs):
        if isinstance(a, ipaddress.IPv6Address):
            addrs.extend(_embedded_v4(a))
    if not all(a.is_global for a in addrs):
        raise WebRefused(f"{host} is not a public address")


_HTML_ACCEPT = "text/html, text/plain;q=0.9"


def _request(url: str, accept: str = _HTML_ACCEPT) -> tuple[urllib.request.Request, str]:
    """Build the Request and check the host urllib will actually connect to."""
    try:
        netloc = urllib.parse.urlsplit(url).netloc
        scheme = urllib.parse.urlsplit(url).scheme
    except ValueError:
        raise WebError("bad URL") from None
    if scheme not in ("http", "https"):
        raise WebRefused("only http and https URLs can be fetched")
    if "@" in netloc or "%" in re.sub(r"\[[^\]]*\]", "", netloc):
        raise WebRefused("the URL host is not plain")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": accept})
        host = urllib.parse.urlsplit("//" + req.host).hostname or ""
    except ValueError:
        raise WebError("bad URL") from None
    return req, host


def _limit_socket(resp, remaining: float) -> None:
    """Best effort: cap the next socket wait at the time left."""
    try:
        resp.fp.raw._sock.settimeout(max(0.05, remaining))
    except AttributeError:
        pass


def safe_fetch(url: str, *, resolve=socket.getaddrinfo, opener=None) -> tuple[str, str]:
    """Fetch a public page; returns (final_url, readable text). Raises WebError."""
    final, ctype, text = safe_fetch_html(url, resolve=resolve, opener=opener)
    return final, (html_to_text(text) if ctype == "text/html" else text.strip())


def safe_fetch_html(url: str, *, resolve=socket.getaddrinfo,
                    opener=None) -> tuple[str, str, str]:
    """Fetch a public page; returns (final_url, content_type, decoded text). Raises WebError."""
    final, ctype, raw, charset = _guarded_fetch(
        url, accept=_HTML_ACCEPT, allowed=lambda t: t in _TYPES, max_bytes=MAX_BYTES,
        what="page", resolve=resolve, opener=opener, truncate=True)
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    return final, ctype, text


IMAGE_MAX_BYTES = 5_000_000


def safe_fetch_bytes(url: str, *, max_bytes: int = IMAGE_MAX_BYTES, resolve=socket.getaddrinfo,
                     opener=None) -> tuple[str, str, bytes]:
    """The same guarded fetch for a picture: returns (final_url, claimed content_type, bytes).
    Any image/* type is fetched; the caller sniffs the bytes, which decide. Raises WebError."""
    final, ctype, raw, _ = _guarded_fetch(
        url, accept="image/png, image/jpeg, image/gif, image/webp, image/*;q=0.5",
        allowed=lambda t: t.startswith("image/"), max_bytes=max_bytes, what="picture",
        resolve=resolve, opener=opener)
    return final, ctype, raw


def _guarded_fetch(url: str, *, accept: str, allowed, max_bytes: int, what: str, resolve,
                   opener, truncate: bool = False) -> tuple[str, str, bytes, str]:
    """The one guarded fetch: scheme and host checks on every hop, redirects followed by
    hand (at most MAX_REDIRECTS, each re-checked), type, size and total-time caps.
    Returns (final_url, content_type, raw bytes, charset).
    Known limit: the host is resolved here and again when connecting, so a DNS
    rebinding answer between the two is not caught (accepted for this use)."""
    opener = opener or build_opener()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        req, host = _request(current, accept)
        _check_host(host, resolve)
        try:
            resp = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location") if exc.headers else None
                exc.close()
                if not loc:
                    raise WebError("redirect without a Location") from None
                current = urllib.parse.urljoin(current, loc)
                continue
            raise WebError(f"the {what} returned HTTP {exc.code}", exc.code) from None
        except _NET_ERRORS as exc:
            raise WebError(f"cannot fetch the {what}: {getattr(exc, 'reason', None) or exc}") from None
        with resp:
            ctype = resp.headers.get_content_type()
            if not allowed(ctype):
                raise WebUnsupported(f"unsupported content type {ctype}")
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise WebTooLarge(f"the {what} is too large")
            deadline = time.monotonic() + TIMEOUT  # total, not per socket operation
            chunks, got = [], 0
            try:
                # a page is cut at the cap; anything else reads one byte more to refuse an overrun
                limit = max_bytes if truncate else max_bytes + 1
                while got < limit:
                    if time.monotonic() > deadline:
                        raise WebError(f"the {what} took too long to download")
                    _limit_socket(resp, deadline - time.monotonic())
                    # read1: returns after one receive, so the deadline is re-checked often
                    chunk = (getattr(resp, "read1", resp.read))(min(65536, limit - got))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    got += len(chunk)
            except TimeoutError:
                raise WebError(f"the {what} took too long to download") from None
            except _NET_ERRORS as exc:
                raise WebError(f"cannot read the {what}: {exc}") from None
            if got > max_bytes:
                raise WebTooLarge(f"the {what} is too large")
            raw = b"".join(chunks)
            charset = resp.headers.get_content_charset() or "utf-8"
        return current, ctype, raw, charset
    raise WebError("too many redirects")
