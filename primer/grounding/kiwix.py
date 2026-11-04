"""A small client for kiwix-serve (local, so never through a proxy)."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from primer.grounding.text import html_to_text

_ATOM = "{http://www.w3.org/2005/Atom}"
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class KiwixError(Exception):
    def __init__(self, message: str = "", http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class KiwixTooLarge(KiwixError):
    pass


class KiwixClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, limit: int | None = None) -> bytes:
        try:
            with _DIRECT.open(self.base_url + path, timeout=self.timeout) as resp:
                return resp.read() if limit is None else resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            raise KiwixError(f"kiwix-serve returned HTTP {exc.code} for {path}", exc.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(cause, ConnectionRefusedError):
                raise KiwixError(f"nothing is answering at {self.base_url}") from None
            if isinstance(cause, TimeoutError):
                raise KiwixError(f"kiwix-serve at {self.base_url} didn't answer in time") from None
            raise KiwixError(f"cannot reach kiwix-serve at {self.base_url}: {cause}") from None

    def _xml(self, path: str) -> ET.Element:
        try:
            return ET.fromstring(self._get(path))
        except ET.ParseError as exc:
            raise KiwixError(f"kiwix-serve sent unreadable XML: {exc}") from None

    def books(self) -> list[dict]:
        root = self._xml("/catalog/v2/entries")
        out = []
        for entry in root.iter(_ATOM + "entry"):
            # the name articles live under is in the html link (/content/<name>); the
            # catalog's <name> drops the flavour and date, so it is only a fallback
            name = next((link.get("href", "")[len("/content/"):].strip("/")
                         for link in entry.iter(_ATOM + "link")
                         if link.get("type") == "text/html"
                         and link.get("href", "").startswith("/content/")), "")
            name = urllib.parse.unquote(name) or (entry.findtext(_ATOM + "name") or "").strip()
            if name:
                out.append({"name": name, "title": (entry.findtext(_ATOM + "title") or name).strip()})
        return out

    def health(self) -> bool:
        try:
            self._get("/catalog/v2/entries")
            return True
        except KiwixError:
            return False

    def search(self, query: str, n: int = 5) -> list[dict]:
        qs = urllib.parse.urlencode({"pattern": query, "format": "xml", "pageLength": n})
        root = self._xml("/search?" + qs)
        out = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            parts = urllib.parse.unquote(urllib.parse.urlsplit(link).path).split("/", 3)
            if len(parts) != 4 or parts[1] != "content" or not parts[2] or not parts[3]:
                continue
            out.append({"book": parts[2], "path": parts[3],
                        "title": (item.findtext("title") or "").strip(),
                        "snippet": (item.findtext("description") or "").strip()})
        return out[:n]

    def suggest(self, book: str, query: str) -> list[dict]:
        qs = urllib.parse.urlencode({"content": book, "term": query})
        try:
            data = json.loads(self._get("/suggest?" + qs))
        except ValueError:
            raise KiwixError("kiwix-serve sent unreadable suggestions") from None
        if not isinstance(data, list):
            raise KiwixError("kiwix-serve sent unreadable suggestions")
        return [e for e in data if isinstance(e, dict) and e.get("kind") == "path" and e.get("path")]

    def article(self, book: str, path: str) -> str:
        raw = self._get(f"/content/{urllib.parse.quote(book, safe='')}/{urllib.parse.quote(path, safe='/')}")
        return html_to_text(raw.decode("utf-8", "replace"))

    def article_html(self, book: str, path: str) -> str:
        raw = self._get(f"/content/{urllib.parse.quote(book, safe='')}/{urllib.parse.quote(path, safe='/')}")
        return raw.decode("utf-8", "replace")

    def content(self, book: str, path: str, limit: int) -> bytes:
        """An entry's raw bytes (a picture); KiwixError when it is longer than `limit`."""
        raw = self._get(f"/content/{urllib.parse.quote(book, safe='')}/{urllib.parse.quote(path, safe='/')}", limit)
        if len(raw) > limit:
            raise KiwixTooLarge("the entry is too large")
        return raw

    def href(self, book: str, path: str) -> str:
        return f"{self.base_url}/content/{urllib.parse.quote(book, safe='')}/{urllib.parse.quote(path, safe='/')}"
