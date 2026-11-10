"""Book jobs: the trail collected into a BookPlan, then written as an EPUB and an HTML file under
`var/books/`. One worker thread, one book at a time; jobs are recorded in the graph.

A request is checked before anything is recorded: the scope and switches, a page to branch
from, something to put in the book, and the size estimate against MAX_BYTES. The plan the
request collected is the one the job writes. Files are written as `<id>.epub.part` and
`<id>.html.part` and renamed once whole; a job that fails, is deleted or is stopped by a
shutdown leaves no files behind.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
from pathlib import Path
from typing import Callable

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

    def create(self, scope, include, title=None, by=None) -> str:
        """Queue a book and return its id. BookBusy while one is queued or being made,
        BookTooBig(reason) over the size limit, ValueError for a bad request (a branch page that
        is unknown or never read says "unknown page" or "that page was never read")."""
        if title is not None and not isinstance(title, str):
            raise ValueError("title must be a string")
        if by is not None and not isinstance(by, str):
            raise ValueError("by must be a string")
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

    def _remove_files(self, book_id: str) -> None:
        for fmt in FORMATS:
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
            if fmt in FORMATS and BOOK_ID.fullmatch(stem):
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
        try:
            archive = self.archive_getter()
        except Exception:
            log.warning("no map file for the book's plates", exc_info=True)
            archive = None
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
        self.graph.update_book(book_id, status="done", epub_size=sizes["epub"], html_size=sizes["html"])
