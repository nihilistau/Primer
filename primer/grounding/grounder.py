"""Turns a mode and a query into numbered sources, or an honest note saying why not."""
from __future__ import annotations

import html
import logging
import re
import time
import queue
import threading
from typing import Callable
from urllib.parse import urlsplit

from . import Source
from .kiwix import KiwixClient, KiwixError
from .text import clip, html_to_text
from .web import Searcher, WebError, safe_fetch, safe_fetch_html

log = logging.getLogger(__name__)

_MAX_SOURCES = 3
_FIRST_BUDGET = 2400
_OTHER_BUDGET = 1200
_TOTAL_BUDGET = 6000
_REFERENCE_DEADLINE = 20.0
_WEB_DEADLINE = 30.0
_TAGS = re.compile(r"<[^>]*>")
_HEAD = re.compile(r"<head\b.*?</head\s*>|<title\b.*?</title\s*>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OWN = "written from the model's own knowledge."
_CHANGE = " Change the default source in Engines."


def plain_title(title: str) -> str:
    """A title as plain one-line text: no tags (kiwix highlights matches), entities unescaped."""
    return " ".join(html.unescape(_TAGS.sub("", title or "")).split())


_plain = plain_title  # the older private name


def _reason(exc: Exception) -> str:
    return (str(exc).strip() or type(exc).__name__)[:120]


def _budgeted(items: list[tuple[str, dict]]) -> list[Source]:
    """items: (text, Source kwargs without n/text). Numbers from 1, clips, caps the total."""
    out: list[Source] = []
    left = _TOTAL_BUDGET
    for text, kw in items[:_MAX_SOURCES]:
        piece = clip(text, min(_FIRST_BUDGET if not out else _OTHER_BUDGET, left))
        if not piece:
            continue
        left -= len(piece)
        out.append(Source(n=len(out) + 1, text=piece, **kw))
    return out


class Grounder:
    def __init__(self, kiwix: KiwixClient | None, searcher: Searcher | None,
                 fetch=safe_fetch, book_order: list[str] | None = None,
                 clock=time.monotonic, fetch_html=safe_fetch_html) -> None:
        self.kiwix = kiwix
        self.searcher = searcher
        self.fetch = fetch
        self.fetch_html = fetch_html
        self.book_order = list(book_order or [])
        self.clock = clock

    def available(self, mode: str) -> tuple[bool, str]:
        if mode == "storyteller":
            return True, ""
        if mode == "reference":
            if self.kiwix is None:
                return False, "No [reference] section in this tier"
            if not self.kiwix.health():
                return False, "The reference server isn't running"
            return True, ""
        if mode == "web":
            if self.searcher is None:
                return False, "Web search is off in this tier"
            return True, ""
        return False, f"unknown mode {mode!r}"

    def gather(self, mode: str, query: str, status: Callable[[str], None]) -> tuple[list[Source], str]:
        """Never raises for a backend failure: returns ([], note) instead."""
        try:
            if mode == "reference":
                if self.kiwix is None:
                    return [], f"Offline reference isn't set up in this tier; {_OWN}{_CHANGE}"
                return self._reference(query, status)
            if mode == "web":
                if self.searcher is None:
                    return [], f"Web search is off in this tier; {_OWN}{_CHANGE}"
                return self._web(query, status)
        except (KiwixError, WebError) as exc:
            log.warning("grounding (%s) failed: %s", mode, exc)
            return [], self._down(mode, exc)
        except Exception as exc:  # a backend bug must not break the page
            log.exception("grounding (%s) crashed", mode)
            return [], self._down(mode, exc)
        return [], ""

    def gather_url(self, u: str) -> tuple[list[Source], str]:
        """One given page (an http(s) URL or kiwix: address) as the single source. Never
        raises for a backend failure: returns ([], note) instead."""
        from primer.browse.address import parse  # browse imports this module: avoid a cycle
        try:
            addr = parse(u)
        except ValueError as exc:
            return [], f"That address can't be read ({_reason(exc)}); {_OWN}"
        if addr.kind == "web" and self.searcher is None:  # web off in this tier: fetch nothing
            return [], f"Web search is off in this tier; {_OWN}{_CHANGE}"
        try:
            if addr.kind == "reference":
                if self.kiwix is None:
                    return [], f"Offline reference isn't set up in this tier; {_OWN}{_CHANGE}"
                raw = self.kiwix.article_html(addr.book, addr.path)
                titles = {b["name"]: b["title"] for b in self.kiwix.books()}
                kw = {"origin": "reference", "book": addr.book, "path": addr.path,
                      "where": titles.get(addr.book, addr.book.replace("_", " "))}
                fallback_title = addr.path.rsplit("/", 1)[-1].replace("_", " ")
            else:
                final, ctype, raw = self.fetch_html(addr.url)
                if ctype != "text/html":
                    raw = "<pre>" + html.escape(raw) + "</pre>"
                host = (urlsplit(final).hostname or "").removeprefix("www.")
                kw = {"origin": "web", "url": final, "where": host}
                fallback_title = host or final
            m = _TITLE.search(raw)
            title = _plain(m.group(1)) if m else ""
            kw["title"] = title or fallback_title
            body = _HEAD.sub("", raw)  # the title is the source's title, not part of its text
            text = html_to_text(body).strip()
        except (KiwixError, WebError) as exc:
            log.warning("grounding (%s) failed: %s", u, exc)
            return [], f"That page couldn't be read ({_reason(exc)}); {_OWN}"
        except Exception as exc:  # a backend bug must not break the page
            log.exception("grounding (%s) crashed", u)
            return [], f"That page couldn't be read ({_reason(exc)}); {_OWN}"
        sources = _budgeted([(text, kw)])
        if not sources:
            return [], f"That page had no text to read; {_OWN}"
        return sources, ""

    @staticmethod
    def _down(mode: str, exc: Exception) -> str:
        if mode == "reference":
            return f"The offline encyclopedia isn't reachable ({_reason(exc)}); {_OWN}"
        return f"Web search isn't working ({_reason(exc)}); {_OWN}"

    def _reference(self, query: str, status) -> tuple[list[Source], str]:
        status("Searching the offline encyclopedia…")
        kiwix = self.kiwix
        titles = {b["name"]: b["title"] for b in kiwix.books()}
        # the configured order first, then any loaded book it didn't name (a renamed file)
        order = [b for b in self.book_order if b in titles]
        order += [b for b in titles if b not in order]
        end = self.clock() + _REFERENCE_DEADLINE
        want = query.strip().casefold()
        exact: list[dict] = []
        loose: list[dict] = []
        failed: Exception | None = None
        answered = False  # some call worked: an empty result then means "nothing found"
        for book in order:
            if self.clock() > end:
                break
            try:
                suggested = kiwix.suggest(book, query)
            except KiwixError as exc:
                log.warning("reference suggest in %s failed: %s", book, exc)
                failed = exc
                continue
            answered = True
            for e in suggested:
                title = _plain(e.get("value") or e.get("label") or e["path"])
                hit = {"book": book, "path": e["path"], "title": title}
                (exact if title.casefold() == want else loose).append(hit)
        if self.clock() <= end:
            try:
                loose += [{"book": h["book"], "path": h["path"], "title": _plain(h["title"])}
                          for h in kiwix.search(query, 5)]
                answered = True
            except KiwixError as exc:  # no full-text index, mixed-language ZIMs, a timeout
                log.warning("reference full-text search failed: %s", exc)
                failed = exc
        seen: set[str] = set()
        picked: list[dict] = []
        for hit in exact + loose:
            key = hit["title"].strip().casefold()
            if key and key not in seen:
                seen.add(key)
                picked.append(hit)
            if len(picked) == _MAX_SOURCES:
                break
        if not picked:
            if failed is not None and not answered:
                raise failed  # nothing found and every call failed: the server isn't reachable
            return [], f'No offline article found for "{query}"; {_OWN}'
        status(f"Reading {len(picked)} article{'s' if len(picked) != 1 else ''}…")
        items = []
        for hit in picked:
            if items and self.clock() > end:
                break
            try:
                text = kiwix.article(hit["book"], hit["path"]).strip()
            except KiwixError as exc:
                log.warning("reference article %s/%s failed: %s", hit["book"], hit["path"], exc)
                continue
            if text:
                items.append((text, {"title": hit["title"], "origin": "reference", "book": hit["book"],
                                     "path": hit["path"], "where": titles.get(hit["book"], hit["book"])}))
        sources = _budgeted(items)
        if not sources:
            return [], f'No offline article found for "{query}"; {_OWN}'
        return sources, ""

    def _web(self, query: str, status) -> tuple[list[Source], str]:
        status("Searching the web…")
        end = self.clock() + _WEB_DEADLINE
        results = self.searcher.search(query, 5)
        if results:
            status(f"Reading {min(len(results), _MAX_SOURCES)} pages…")
        # fetch every result at once on daemon threads (they never hold up exit); keep the first
        # _MAX_SOURCES usable ones in rank order, from whatever has arrived by the deadline
        done: queue.Queue = queue.Queue()

        def run(i: int, url: str) -> None:
            try:
                done.put((i, self.fetch(url)))
            except Exception as exc:  # WebError, or a bug in a fetcher: skip this page
                done.put((i, exc))

        for i, r in enumerate(results):
            threading.Thread(target=run, args=(i, r["url"]), daemon=True,
                             name="primer-fetch").start()
        arrived: dict[int, object] = {}
        items = []
        for i, r in enumerate(results):
            while i not in arrived:
                try:
                    j, got = done.get(timeout=max(0.0, end - self.clock()))
                except queue.Empty:
                    break
                arrived[j] = got
            if i not in arrived:
                log.info("web fetch %s: past the deadline", r.get("url"))
                continue
            got = arrived[i]
            if isinstance(got, Exception):
                log.info("web fetch %s skipped: %s", r.get("url"), got)
                continue
            final_url, text = got
            text = (text or "").strip()
            if not text:
                continue
            host = (urlsplit(final_url).hostname or "").removeprefix("www.")
            items.append((text, {"title": _plain(r.get("title") or "") or host or final_url, "origin": "web",
                                 "url": final_url, "where": host}))
            if len(items) == _MAX_SOURCES:
                break
        sources = _budgeted(items)
        if not sources:
            return [], f'No usable web page found for "{query}"; {_OWN}'
        return sources, ""
