"""Book jobs: the trail collected into a BookPlan, then written as an EPUB and an HTML file under
`var/books/`. One worker thread, one book at a time; jobs are recorded in the graph.

A request is checked before anything is recorded: the scope and switches, a page to branch
from, something to put in the book, and the size estimate against MAX_BYTES. The plan the
request collected is the one the job writes. Files are written as `<id>.epub.part` and
`<id>.html.part` (and the bound book's blocks as `<id>.blocks.json.part`) and renamed once
whole; a job that fails, is deleted or is stopped by a shutdown leaves no files behind.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
from pathlib import Path
from typing import Callable

from primer.book.blocks import KINDS as BLOCK_KINDS
from primer.book.blocks import blocks as book_blocks
from primer.book.blocks import guard as block_guard
from primer.book.epub import write_epub
from primer.book.html import write_html
from primer.book.model import _include, _scope, collect, estimate

log = logging.getLogger(__name__)

MAX_BYTES = 400 * 1024 * 1024  # each of the EPUB and the HTML, by the estimate
TOO_BIG = "Too big: leave out pictures or choose less"
BUSY = "A book is being made; wait for it to finish"
SHUT_DOWN = "Primer shut down"
FAILED = "The book couldn't be made"
FAILED_WRITE = "The book couldn't be made: a file couldn't be written"
EMPTY = "Nothing to put in the book: no pages read in that part of the trail"
BOOK_ID = re.compile(r"[0-9a-f]{16}")
FORMATS = ("epub", "html")
BLOCKS = "blocks.json"  # the bound book's blocks, saved with the book (`<id>.blocks.json`)
UNKNOWN_PAGE = ("unknown page", "that page was never read")
MAX_BLOCKS_FILE = 64 * 1024 * 1024  # a saved blocks file bigger than this is rebuilt, not read
LAYING_OUT = "Primer is already laying out a book"


class BookBusy(Exception):
    pass


class BookTooBig(Exception):
    pass


class _Stopped(Exception):
    """The job was deleted or Primer is shutting down."""


def slug(title: str) -> str:
    """The download name: ASCII letters, digits and hyphens, at most 60 characters."""
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")[:60].strip("-")
    return s or "book"


class BookNotReady(Exception):
    pass


class BookLayingOut(Exception):
    """Another bound book is being built in memory (one at a time)."""


def _valid_block(b) -> bool:
    """A saved block as `blocks` writes it: a known kind, a chapter number, and HTML that the
    guard writes back unchanged."""
    if not isinstance(b, dict):
        return False
    chapter, html = b.get("chapter"), b.get("html")
    return (b.get("kind") in BLOCK_KINDS and isinstance(chapter, int) and not isinstance(chapter, bool)
            and chapter >= 0 and isinstance(html, str) and block_guard(html)
            and isinstance(b.get("node_id", ""), str))


class BookService:
    """`archive_getter()` is the tier's open map file, or None for plain map plates; it is read
    when a job starts (a tier switch may change it)."""

    def __init__(self, graph, var_dir: Path, archive_getter: Callable[[], object | None]) -> None:
        self.graph = graph
        self.dir = Path(var_dir) / "books"
        self.media_dir = Path(var_dir) / "media"
        self.archive_getter = archive_getter
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._layout = threading.Lock()  # one in-memory bound book at a time (live or rebuilt)
        self._closed = False
        self._busy: str | None = None  # "" while a request is being checked, else the job's id
        self._plans: dict[str, object] = {}
        self._dropped: set[str] = set()  # deleted while queued or making
        self._sweep()
        self._worker = threading.Thread(target=self._run, name="primer-books", daemon=True)
        self._worker.start()

    # requests
    def preview(self, scope, include) -> dict:
        """{chapters, pictures, bytes, title}; ValueError on a bad scope or include. It counts
        with a light collect (nothing rendered), which `estimate`s the same as the full one."""
        plan = collect(self.graph, scope, include, media_dir=self.media_dir, light=True)
        est = estimate(plan, self.media_dir)
        return {"chapters": est["chapters"], "pictures": est["pictures"], "bytes": est["bytes"],
                "title": plan.title}

    def live_blocks(self, scope, include, title=None, by=None) -> dict:
        """{blocks, title}: the trail as the bound book's blocks, built in memory with no job and
        no files. ValueError for a bad request (as `create`), BookTooBig over the size limit."""
        _check_names(title, by)
        plan, made = self._lay_out(scope, include, title or "", by or "")
        return {"blocks": made, "title": plan.title}

    def _lay_out(self, scope, include, title: str, by: str):
        """(plan, blocks) built in memory, one at a time: BookLayingOut while another is being
        built, BookTooBig over the size limit, ValueError for a bad scope or include."""
        if not self._layout.acquire(blocking=False):
            raise BookLayingOut(LAYING_OUT)
        try:
            plan = collect(self.graph, scope, include, title=title, by=by, media_dir=self.media_dir)
            if estimate(plan, self.media_dir)["bytes"] > MAX_BYTES:
                raise BookTooBig(TOO_BIG)
            return plan, book_blocks(plan, self.media_dir, archive=self._archive())
        finally:
            self._layout.release()

    def saved_blocks(self, book_id: str) -> dict | None:
        """{blocks, title, rebuilt} for a made book: its saved blocks, or, for a book saved
        without them (made before v0.15.0) or whose file can't be read, the book rebuilt from
        its scope and switches with `rebuilt` true. The saved file is checked, not trusted: one
        over MAX_BLOCKS_FILE, or with any block `_valid_block` refuses, is rebuilt. None for no
        such book, BookNotReady while it isn't done; rebuilding raises as `live_blocks` does
        (ValueError when its page is gone)."""
        book = self.get(book_id)
        if book is None:
            return None
        if book["status"] != "done":
            raise BookNotReady("The book isn't finished")
        path = self.dir / f"{book_id}.{BLOCKS}"
        try:
            if path.stat().st_size > MAX_BLOCKS_FILE:
                log.warning("book %s: saved blocks are too big; rebuilding", book_id)
            else:
                with path.open(encoding="utf-8") as f:
                    saved = json.load(f)
                found = saved.get("blocks") if isinstance(saved, dict) else None
                if isinstance(found, list) and all(_valid_block(b) for b in found):
                    return {"blocks": found, "title": book["title"], "rebuilt": False}
                log.warning("book %s: saved blocks don't check out; rebuilding", book_id)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError):
            log.warning("book %s: saved blocks can't be read; rebuilding", book_id, exc_info=True)
        params = book.get("params") or {}
        _plan, made = self._lay_out(params.get("scope"), params.get("include"),
                                    str(params.get("title") or book["title"] or ""),
                                    str(params.get("by") or ""))
        return {"blocks": made, "title": book["title"], "rebuilt": True}

    def create(self, scope, include, title=None, by=None) -> str:
        """Queue a book and return its id. BookBusy while one is queued or being made,
        BookTooBig(reason) over the size limit, ValueError for a bad request (a branch page that
        is unknown or never read says "unknown page" or "that page was never read")."""
        _check_names(title, by)
        with self._lock:
            if self._closed:
                raise ValueError(SHUT_DOWN)
            if self._busy is not None:
                raise BookBusy(BUSY)
            self._busy = ""  # held while the request is checked
        book_id = None
        try:
            plan = collect(self.graph, scope, include, title=title or "", by=by or "",
                           media_dir=self.media_dir)
            if not plan.chapters:
                raise ValueError(EMPTY)
            if estimate(plan, self.media_dir)["bytes"] > MAX_BYTES:
                raise BookTooBig(TOO_BIG)
            params = {"scope": _scope(scope), "include": _include(include), "title": plan.title,
                      "by": plan.by}
            with self._lock:
                if self._closed:
                    raise ValueError(SHUT_DOWN)
                book_id = self.graph.add_book(plan.title, params)
                self._plans[book_id] = plan
                self._busy = book_id
        finally:
            if book_id is None:
                with self._lock:
                    self._busy = None
        self._queue.put(book_id)
        return book_id

    def get(self, book_id: str) -> dict | None:
        if not isinstance(book_id, str) or not BOOK_ID.fullmatch(book_id):
            return None
        return self.graph.get_book(book_id)

    def list(self) -> list[dict]:
        return self.graph.list_books()

    def delete(self, book_id: str) -> bool:
        """Remove the book and its files; False when there is no such book. A book still being
        made stops."""
        if self.get(book_id) is None:
            return False
        with self._lock:
            if self._busy == book_id:
                self._dropped.add(book_id)
        gone = self.graph.delete_book(book_id)
        self._remove_files(book_id)
        return gone

    def path(self, book_id: str, fmt: str) -> Path | None:
        """The finished book's file in this format, or None."""
        if fmt not in FORMATS:
            return None
        book = self.get(book_id)
        if book is None or book["status"] != "done":
            return None
        p = self.dir / f"{book_id}.{fmt}"
        return p if p.is_file() else None

    def download_name(self, book_id: str, fmt: str) -> str:
        book = self.get(book_id)
        return f"{slug(book['title'] if book else '')}.{fmt}"

    def shutdown(self, reason: str = SHUT_DOWN) -> None:
        """Stop without finishing: a book queued or being made fails with `reason`."""
        with self._lock:
            self._closed = True
            pending = self._busy or None
        self._queue.put(None)
        if pending:
            self._fail(pending, reason)
        self._worker.join(timeout=2)

    # internals
    def _unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not remove %s", path.name, exc_info=True)

    def _save_blocks(self, book_id: str, plan, archive, plates: dict) -> None:
        """The bound book's blocks, so "Open as a book" shows exactly what was downloaded. A
        failure here doesn't fail the book: it is logged, no file is kept, and the book is
        rebuilt when it is opened. A stop still stops the job."""
        part = self.dir / f"{book_id}.{BLOCKS}.part"
        try:
            made = book_blocks(plan, self.media_dir, archive=archive, plates=plates)
            if self._stopped(book_id):
                raise _Stopped()
            with part.open("w", encoding="utf-8", newline="\n") as f:
                json.dump({"blocks": made}, f, ensure_ascii=False, separators=(",", ":"))
            if self._stopped(book_id):
                raise _Stopped()
            part.replace(self.dir / f"{book_id}.{BLOCKS}")
        except _Stopped:
            raise
        except Exception:
            log.exception("book %s: its blocks couldn't be saved; it will be rebuilt when opened",
                          book_id)
            self._unlink(part)

    def _archive(self):
        try:
            return self.archive_getter()
        except Exception:
            log.warning("no map file for the book's plates", exc_info=True)
            return None

    def _remove_files(self, book_id: str) -> None:
        for fmt in FORMATS + (BLOCKS,):
            for name in (f"{book_id}.{fmt}", f"{book_id}.{fmt}.part"):
                self._unlink(self.dir / name)

    def _sweep(self) -> None:
        """At start-up: no half-written files, and no book file without a finished book."""
        try:
            found = list(self.dir.iterdir()) if self.dir.is_dir() else []
        except OSError:
            log.warning("could not list %s", self.dir, exc_info=True)
            return
        for path in found:
            if not path.is_file():
                continue
            if path.name.endswith(".part"):
                self._unlink(path)
                continue
            stem, _, fmt = path.name.partition(".")
            if (fmt in FORMATS or fmt == BLOCKS) and BOOK_ID.fullmatch(stem):
                book = self.graph.get_book(stem)
                if book is None or book["status"] != "done":
                    self._unlink(path)

    def _fail(self, book_id: str, error: str) -> None:
        try:
            self.graph.update_book(book_id, status="failed", error=error)
        except ValueError:  # deleted meanwhile
            pass
        except Exception:
            log.exception("could not mark book %s failed", book_id)

    def _stopped(self, book_id: str) -> bool:
        with self._lock:
            return self._closed or book_id in self._dropped

    def _run(self) -> None:
        while True:
            book_id = self._queue.get()
            if book_id is None:
                return
            try:
                if not self._stopped(book_id):
                    self._job(book_id)
            except _Stopped:
                pass
            except Exception as e:  # a job never kills the worker; the detail goes to the log only
                log.exception("book job %s failed", book_id)
                self._fail(book_id, FAILED_WRITE if isinstance(e, OSError) else FAILED)
            finally:
                # failed, deleted or shut down at any point (even after the last rename): no files
                try:
                    book = self.graph.get_book(book_id)
                except Exception:
                    log.exception("could not read book %s", book_id)
                    book = None
                if book is None or book["status"] != "done":
                    self._remove_files(book_id)
                with self._lock:
                    self._plans.pop(book_id, None)
                    self._dropped.discard(book_id)
                    if self._busy == book_id:
                        self._busy = None

    def _job(self, book_id: str) -> None:
        with self._lock:
            plan = self._plans.get(book_id)
        if plan is None:
            raise RuntimeError("the book's plan is gone")
        self.graph.update_book(book_id, status="making",
                               progress={"done": 0, "total": len(plan.chapters), "stage": "epub"})
        archive = self._archive()
        self.dir.mkdir(parents=True, exist_ok=True)
        sizes = {}
        last: dict = {}
        plates: dict = {}  # each map plate drawn once, for both files
        for fmt, writer in (("epub", write_epub), ("html", write_html)):
            def progress(i, n, stage=fmt):
                if self._stopped(book_id):  # checked on every call: each chapter and picture
                    raise _Stopped()
                now = {"done": i, "total": n, "stage": stage}
                if now != last:
                    last.clear()
                    last.update(now)
                    self.graph.update_book(book_id, progress=now)

            part = self.dir / f"{book_id}.{fmt}.part"
            sizes[fmt] = writer(plan, self.media_dir, part, archive=archive, plates=plates,
                                 progress=progress)
            if self._stopped(book_id):
                raise _Stopped()
            part.replace(self.dir / f"{book_id}.{fmt}")
        self._save_blocks(book_id, plan, archive, plates)
        self.graph.update_book(book_id, status="done", epub_size=sizes["epub"], html_size=sizes["html"])


def _check_names(title, by) -> None:
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")
    if by is not None and not isinstance(by, str):
        raise ValueError("by must be a string")
