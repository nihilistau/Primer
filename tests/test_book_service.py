"""The book job and its routes."""
import http.client
import json
import threading
import time
import zipfile

import pytest

T0 = 1_762_700_000.0


def page(g, title, parent=None, at=T0, phrase="it"):
    n = g.node_for(title, "concept")
    g.add_version(n.id, f"# {title}\n{title} is a page.", "storyteller", "normal")
    with g._db:
        g._db.execute("INSERT INTO edges(from_id, to_id, phrase, via, at) VALUES(?,?,?,?,?)",
                      (parent.id if parent else None, n.id, phrase, "context", at))
    return n


def raw_get(client, path):
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def wait_book(client, book_id, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        b = client.get(f"/v1/books/{book_id}")
        if b["status"] in ("done", "failed"):
            return b
        time.sleep(0.05)
    raise AssertionError("book job did not finish")


def test_book_job_and_routes(client, app_parts, g):
    app, _ = app_parts
    a = page(g, "Trebuchet")
    page(g, "Counterweight", a, T0 + 1)
    assert client.get("/v1/status")["book_available"] == {"ok": True, "reason": ""}
    assert client.get("/v1/books") == {"books": []}
    pre = client.post("/v1/books/preview", {"scope": {"kind": "all"}, "include": {}})
    assert pre["chapters"] == 2 and pre["pictures"] == 0 and pre["bytes"] > 0
    assert pre["title"] == "My wanderings"
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}, "by": "A reader"}, expect=202)
    book_id = r["id"]
    assert len(book_id) == 16
    b = wait_book(client, book_id)
    assert b["status"] == "done", b
    assert b["title"] == "My wanderings" and b["epub_size"] > 0 and b["html_size"] > 0
    assert b["progress"]["stage"] == "html" and b["progress"]["done"] == b["progress"]["total"] == 2
    status, headers, body = raw_get(client, f"/v1/books/{book_id}/epub")
    assert status == 200 and headers["Content-Type"] == "application/epub+zip"
    assert headers["Content-Disposition"] == 'attachment; filename="my-wanderings.epub"'
    assert len(body) == b["epub_size"] and body[:2] == b"PK"
    status, headers, body = raw_get(client, f"/v1/books/{book_id}/html")
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Disposition"] == 'attachment; filename="my-wanderings.html"'
    assert b"Trebuchet is a page." in body and len(body) == b["html_size"]
    listed = client.get("/v1/books")["books"]
    assert [x["id"] for x in listed] == [book_id]
    # a second book is listed first
    branch = client.post("/v1/books", {"scope": {"kind": "branch", "node_id": a.id}, "include": {},
                                       "title": "Siege engines!"}, expect=202)
    wait_book(client, branch["id"])
    assert [x["id"] for x in client.get("/v1/books")["books"]] == [branch["id"], book_id]
    status, headers, _ = raw_get(client, f"/v1/books/{branch['id']}/epub")
    assert headers["Content-Disposition"] == 'attachment; filename="siege-engines.epub"'
    files = app.var_dir / "books"
    assert (files / f"{book_id}.epub").is_file() and (files / f"{book_id}.html").is_file()
    assert client.post(f"/v1/books/{book_id}/delete", {}) == {"deleted": True}
    assert not (files / f"{book_id}.epub").exists() and not (files / f"{book_id}.html").exists()
    client.get(f"/v1/books/{book_id}", expect=404)
    assert raw_get(client, f"/v1/books/{book_id}/epub")[0] == 404
    client.post(f"/v1/books/{book_id}/delete", {}, expect=404)


def test_book_busy_and_too_big(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    app, _ = app_parts
    page(g, "Trebuchet")
    go = threading.Event()
    real = service.write_epub

    def slow(*a, **kw):
        go.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(service, "write_epub", slow)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    try:
        busy = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=409)
        assert busy["error"]
        assert client.get(f"/v1/books/{r['id']}")["status"] in ("queued", "making")
    finally:
        go.set()
    assert wait_book(client, r["id"])["status"] == "done"
    monkeypatch.setattr(service, "MAX_BYTES", 10)
    big = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=413)
    assert big["error"] == "Too big: leave out pictures or choose less"
    assert len(client.get("/v1/books")["books"]) == 1  # nothing was recorded


def test_book_pages_lists_read_pages_only(client, app_parts, g):
    assert client.get("/v1/books/pages") == {"pages": []}
    tre = page(g, "trebuchet")
    arrow = page(g, "Arrow slit")
    unread = g.node_for("Unread", "concept")
    with g._db:  # opened (an edge in) but never written
        g._db.execute("INSERT INTO edges(from_id, to_id, phrase, via, at) VALUES(?,?,?,?,?)",
                      (tre.id, unread.id, "it", "context", T0))
    pages = client.get("/v1/books/pages")["pages"]
    assert [p["id"] for p in pages] == [arrow.id, tre.id]  # by title, case-insensitively
    assert pages[0] == {"id": arrow.id, "title": arrow.title, "concept": arrow.concept}
    assert unread.id not in {p["id"] for p in pages}


def test_book_bad_requests(client, app_parts, g):
    page(g, "Trebuchet")
    unread = g.node_for("Unread", "concept")
    for scope in ({"kind": "nope"}, {"kind": "since", "since": "yesterday"}, "all", {"kind": "branch"}):
        client.post("/v1/books", {"scope": scope, "include": {}}, expect=400)
        client.post("/v1/books/preview", {"scope": scope, "include": {}}, expect=400)
    client.post("/v1/books", {"include": {}}, expect=400)
    client.post("/v1/books", {"scope": {"kind": "all"}, "include": "all"}, expect=400)
    client.post("/v1/books", {"scope": {"kind": "all"}, "title": 5}, expect=400)
    client.post("/v1/books", {"scope": {"kind": "all"}, "by": ["x"]}, expect=400)
    client.post("/v1/books", {"scope": {"kind": "branch", "node_id": "f" * 16}}, expect=404)
    client.post("/v1/books", {"scope": {"kind": "branch", "node_id": unread.id}}, expect=404)
    client.post("/v1/books/preview", {"scope": {"kind": "branch", "node_id": "f" * 16}}, expect=400)
    # nothing in that part of the trail
    client.post("/v1/books", {"scope": {"kind": "since", "since": 4_000_000_000}}, expect=400)
    for bad in ("nope", "F" * 16, "0" * 15, "0" * 17, "..%2f..%2fx", "0" * 16):
        client.get(f"/v1/books/{bad}", expect=404)
        assert raw_get(client, f"/v1/books/{bad}/epub")[0] == 404
        client.post(f"/v1/books/{bad}/delete", {}, expect=404)
    assert client.get("/v1/books") == {"books": []}


def test_books_fail_on_restart(g, tmp_path):
    import serve
    from primer.book.service import BookService
    from primer.config import TIERS_DIR
    from primer.hardware import Hardware

    queued = g.add_book("A", {"scope": {"kind": "all"}})
    making = g.add_book("B", {"scope": {"kind": "all"}})
    g.update_book(making, status="making", progress={"done": 1, "total": 3, "stage": "epub"})
    done = g.add_book("C", {"scope": {"kind": "all"}})
    g.update_book(done, status="done", epub_size=10, html_size=20)
    assert g.get_book(making)["progress"] == {"done": 1, "total": 3, "stage": "epub"}
    assert g.fail_unfinished_books("Primer restarted") == 2
    for bid in (queued, making):
        assert g.get_book(bid)["status"] == "failed" and g.get_book(bid)["error"] == "Primer restarted"
    g.update_book(queued, status="done")  # a late writer can't revive a failed book
    assert g.get_book(queued)["status"] == "failed"
    g.update_book(done, status="failed", error="late")  # nor fail a done one
    assert g.get_book(done)["status"] == "done" and g.get_book(done)["epub_size"] == 10
    assert [b["id"] for b in g.list_books()] == [done, making, queued]
    assert g.delete_book(done) is True and g.delete_book(done) is False and g.get_book(done) is None
    with pytest.raises(ValueError):
        g.update_book(queued, status="lost")

    # serve.py fails them on start
    var = tmp_path / "var"
    from primer.graph import Graph
    g2 = Graph(var / "primer.sqlite")
    left = g2.add_book("Left", {})
    app = serve.build_app(serve.load_tier("light"), var, tmp_path / "ui", TIERS_DIR,
                          hardware=Hardware(None, 0.0, 16.0))
    try:
        assert app.graph.get_book(left)["status"] == "failed"
        assert isinstance(app.books, BookService)
    finally:
        app.shutdown_books()
        app.images.shutdown()
        app.pages.shutdown(drain=False)

    # shutting the service down fails a job that is still running
    svc = BookService(g, tmp_path / "v2", lambda: None)
    gate, started = threading.Event(), threading.Event()
    import primer.book.service as service
    real = service.write_epub

    def slow(plan, media_dir, out, **kw):
        out.write_bytes(b"half a book")
        started.set()
        gate.wait(5)
        return real(plan, media_dir, out, **kw)

    service.write_epub = slow
    try:
        page(g, "Trebuchet")
        bid = svc.create({"kind": "all"}, {}, "", "")
        assert started.wait(5)  # being made, with a half-written file
        svc.shutdown("Primer shut down")
        assert g.get_book(bid)["status"] == "failed" and g.get_book(bid)["error"] == "Primer shut down"
        with pytest.raises(ValueError):
            svc.create({"kind": "all"}, {}, "", "")
    finally:
        gate.set()
        service.write_epub = real
    svc._worker.join(5)
    assert not svc._worker.is_alive()
    assert g.get_book(bid)["status"] == "failed"
    books = tmp_path / "v2" / "books"
    assert books.is_dir()  # the job got as far as writing...
    assert list(books.iterdir()) == []  # ...and left nothing behind


def test_a_failed_status_read_keeps_the_files_and_fails_the_book(g, tmp_path, monkeypatch):
    """The job's last read of the book's status fails: the files are left alone (not taken as
    "not done" and removed), and a book that isn't done is marked failed with a reason."""
    import primer.book.service as service
    from primer.book.service import FAILED, STATUS_UNREAD, BookService

    class Flaky:
        broken = False

        def __getattr__(self, name):
            return getattr(g, name)

        def update_book(self, book_id, **kw):
            g.update_book(book_id, **kw)
            if kw.get("status") == "done":
                Flaky.broken = True  # the job's final read fails from here on

        def get_book(self, book_id):
            if Flaky.broken:
                raise RuntimeError("database is locked")
            return g.get_book(book_id)

    def run(svc):
        bid = svc.create({"kind": "all"}, {}, "", "")
        end = time.time() + 10
        while svc._busy is not None and time.time() < end:
            time.sleep(0.05)
        assert svc._busy is None
        return bid, g.get_book(bid)

    page(g, "Trebuchet")
    # a finished book whose status can't be read afterwards keeps its files and stays done
    svc = BookService(Flaky(), tmp_path / "v3", lambda: None)
    books = tmp_path / "v3" / "books"
    try:
        bid, b = run(svc)
        assert (books / f"{bid}.epub").is_file() and (books / f"{bid}.html").is_file()
        assert b["status"] == "done"
        # a job that fails, and whose status then can't be read: its file stays, the book fails
        Flaky.broken = False

        def broken_html(*a, **kw):
            Flaky.broken = True
            raise RuntimeError("no")

        monkeypatch.setattr(service, "write_html", broken_html)
        bid, b = run(svc)
        assert (books / f"{bid}.epub").is_file()
        assert b["status"] == "failed" and b["error"] == FAILED
        # a job that ends without finishing or failing, then can't be read: failed, saying why
        Flaky.broken = False
        monkeypatch.setattr(svc, "_job", lambda book_id: setattr(Flaky, "broken", True))
        bid, b = run(svc)
        assert b["status"] == "failed" and b["error"] == STATUS_UNREAD
    finally:
        svc.shutdown()


def test_files_left_by_a_failed_status_read_are_tidied_without_a_restart(g, tmp_path, monkeypatch):
    """A book whose delete went through has its files go at once, whatever the read says.
    Otherwise (a delete that failed included) the clean-up waits for the next read that works:
    a book that isn't done then loses its files and, if it still reads as unfinished, is marked
    failed with a reason; a done book's files stay."""
    import primer.book.service as service
    from primer.book.service import STATUS_UNREAD, BookService

    monkeypatch.setattr(service, "RETRY_UNREAD", 0.05)

    class Flaky:
        broken = False  # status reads fail
        locked = False  # writes (update, delete) fail too: a sustained lock

        def __getattr__(self, name):
            return getattr(g, name)

        def get_book(self, book_id):
            if Flaky.broken:
                raise RuntimeError("database is locked")
            return g.get_book(book_id)

        def update_book(self, book_id, **kw):
            if Flaky.locked:
                raise RuntimeError("database is locked")
            return g.update_book(book_id, **kw)

        def delete_book(self, book_id):
            if Flaky.locked:
                raise RuntimeError("database is locked")
            return g.delete_book(book_id)

    svc = BookService(Flaky(), tmp_path / "v4", lambda: None)
    books = tmp_path / "v4" / "books"
    files = lambda bid: [books / f"{bid}.{fmt}" for fmt in ("epub", "html")]  # noqa: E731

    def wait_idle():
        end = time.time() + 10
        while svc._busy is not None and time.time() < end:
            time.sleep(0.02)

    def until(cond, timeout=5.0):
        end = time.time() + timeout
        while not cond() and time.time() < end:
            time.sleep(0.02)
        return cond()

    try:
        page(g, "Trebuchet")

        def write(book_id):
            books.mkdir(parents=True, exist_ok=True)
            for f in files(book_id):
                f.write_bytes(b"x")

        # deleted while being made, and then the read fails: its files go now
        def deleted(book_id):
            assert svc.delete(book_id) is True
            write(book_id)
            Flaky.broken = True

        monkeypatch.setattr(svc, "_job", deleted)
        bid = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        assert not any(f.exists() for f in files(bid))
        # a delete that fails under a lock, which also stops the job's read and its marking failed:
        # the book isn't taken as deleted, so its files wait for a read that works
        Flaky.broken = False

        def delete_fails(book_id):
            Flaky.locked = True
            with pytest.raises(RuntimeError):
                svc.delete(book_id)
            write(book_id)
            Flaky.broken = True

        monkeypatch.setattr(svc, "_job", delete_fails)
        stuck = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        time.sleep(0.2)
        assert all(f.exists() for f in files(stuck))
        assert g.get_book(stuck)["status"] == "queued"  # couldn't be marked failed
        # the lock lifts: the files go, and the book (still unfinished) is failed, saying why
        Flaky.broken = Flaky.locked = False
        assert until(lambda: not any(f.exists() for f in files(stuck)))
        assert until(lambda: g.get_book(stuck)["status"] == "failed")
        assert g.get_book(stuck)["error"] == STATUS_UNREAD
        # a job that ends unfinished and can't be read: its files stay while reads fail...
        Flaky.broken = False

        def unread(book_id):
            write(book_id)
            Flaky.broken = True

        monkeypatch.setattr(svc, "_job", unread)
        failed = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        time.sleep(0.2)
        assert all(f.exists() for f in files(failed))
        # ...and go once a read works again (it reads as failed), with no restart
        Flaky.broken = False
        assert until(lambda: not any(f.exists() for f in files(failed)))
        assert g.get_book(failed)["status"] == "failed"
        # a book that finished, then couldn't be read: once read again it is done, so it stays
        def done(book_id):
            write(book_id)
            g.update_book(book_id, status="done", epub_size=1, html_size=1)
            Flaky.broken = True

        monkeypatch.setattr(svc, "_job", done)
        kept = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        Flaky.broken = False
        assert until(lambda: not svc._unread)
        assert all(f.exists() for f in files(kept))
    finally:
        Flaky.broken = Flaky.locked = False
        svc.shutdown()


def test_a_stopped_book_never_stays_making_with_no_files(g, tmp_path, monkeypatch):
    """A delete that fails while the job runs still stops the job: with reads working, the book
    ends failed with a reason and no files. When marking a book failed fails, the clean-up keeps
    it and tries again later."""
    import primer.book.service as service
    from primer.book.service import STATUS_UNREAD, STOPPED, BookService

    monkeypatch.setattr(service, "RETRY_UNREAD", 0.05)

    class Flaky:
        reads = True
        updates = True
        deletes = True

        def __getattr__(self, name):
            return getattr(g, name)

        def get_book(self, book_id):
            if not Flaky.reads:
                raise RuntimeError("database is locked")
            return g.get_book(book_id)

        def update_book(self, book_id, **kw):
            if not Flaky.updates:
                raise RuntimeError("database is locked")
            return g.update_book(book_id, **kw)

        def delete_book(self, book_id):
            if not Flaky.deletes:
                raise RuntimeError("database is locked")
            return g.delete_book(book_id)

    svc = BookService(Flaky(), tmp_path / "v5", lambda: None)
    books = tmp_path / "v5" / "books"
    files = lambda bid: [books / f"{bid}.{fmt}" for fmt in ("epub", "html")]  # noqa: E731

    def write(book_id):
        books.mkdir(parents=True, exist_ok=True)
        for f in files(book_id):
            f.write_bytes(b"x")

    def wait_idle():
        end = time.time() + 10
        while svc._busy is not None and time.time() < end:
            time.sleep(0.02)

    def until(cond, timeout=5.0):
        end = time.time() + timeout
        while not cond() and time.time() < end:
            time.sleep(0.02)
        return cond()

    try:
        page(g, "Trebuchet")

        # M1: the user's delete fails mid-job (reads work); the job stops as asked
        def delete_fails(book_id):
            Flaky.deletes = False
            with pytest.raises(RuntimeError):
                svc.delete(book_id)
            Flaky.deletes = True
            write(book_id)
            if svc._stopped(book_id):
                raise service._Stopped()

        monkeypatch.setattr(svc, "_job", delete_fails)
        bid = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        b = g.get_book(bid)
        assert b["status"] == "failed" and b["error"] == STOPPED
        assert not any(f.exists() for f in files(bid))

        # M2: reads fail at the end of the job; when they work again, writes still fail, so the
        # book can't be marked failed yet: it stays waiting, and is failed once writes work
        def unread(book_id):
            write(book_id)
            Flaky.reads = Flaky.updates = False

        monkeypatch.setattr(svc, "_job", unread)
        stuck = svc.create({"kind": "all"}, {}, "", "")
        wait_idle()
        Flaky.reads = True
        assert until(lambda: not any(f.exists() for f in files(stuck)))
        time.sleep(0.2)  # several more tries while writes still fail
        assert stuck in svc._unread
        assert g.get_book(stuck)["status"] == "queued"
        Flaky.updates = True
        assert until(lambda: g.get_book(stuck)["status"] == "failed")
        assert g.get_book(stuck)["error"] == STATUS_UNREAD
        assert until(lambda: stuck not in svc._unread)
    finally:
        Flaky.reads = Flaky.updates = Flaky.deletes = True
        svc.shutdown()


def test_deleting_a_book_being_made_stops_it(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    app, _ = app_parts
    page(g, "Trebuchet")
    started, go = threading.Event(), threading.Event()
    real = service.write_epub

    def slow(*a, **kw):
        started.set()
        go.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(service, "write_epub", slow)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert started.wait(5)
    assert client.post(f"/v1/books/{r['id']}/delete", {}) == {"deleted": True}
    go.set()
    end = time.time() + 5
    while app.books._busy is not None and time.time() < end:
        time.sleep(0.05)
    assert app.books._busy is None
    client.get(f"/v1/books/{r['id']}", expect=404)
    books = app.var_dir / "books"
    assert not list(books.glob(f"{r['id']}*"))
    monkeypatch.setattr(service, "write_epub", real)
    again = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert wait_book(client, again["id"])["status"] == "done"


def test_failed_book_stores_fixed_text_not_the_exception(client, app_parts, g, monkeypatch):
    import primer.book.service as service

    page(g, "Trebuchet")

    def broken(plan, media_dir, out, **kw):
        raise OSError(f"[Errno 28] No space left on device: '{out}'")

    monkeypatch.setattr(service, "write_epub", broken)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    b = wait_book(client, r["id"])
    assert b["status"] == "failed"
    assert b["error"] == "The book couldn't be made: a file couldn't be written"
    assert "books" not in b["error"] and r["id"] not in b["error"]

    def odd(*a, **kw):
        raise RuntimeError("secret detail at C:/somewhere")

    monkeypatch.setattr(service, "write_epub", odd)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert wait_book(client, r["id"])["error"] == "The book couldn't be made"


def test_start_up_sweeps_the_books_folder(g, tmp_path):
    from primer.book.service import BookService

    books = tmp_path / "var" / "books"
    books.mkdir(parents=True)
    done = g.add_book("Done", {})
    g.update_book(done, status="done", epub_size=1, html_size=1)
    failed = g.add_book("Failed", {})
    g.update_book(failed, status="failed", error="x")
    queued = g.add_book("Queued", {})
    orphan = "0123456789abcdef"
    keep = [f"{done}.epub", f"{done}.html", "notes.txt"]
    drop = [f"{done}.epub.part", f"{failed}.epub", f"{failed}.html", f"{queued}.html.part",
            f"{queued}.epub", f"{orphan}.epub", f"{orphan}.html", "stray.part"]
    for name in keep + drop:
        (books / name).write_bytes(b"x")
    svc = BookService(g, tmp_path / "var", lambda: None)
    try:
        assert sorted(p.name for p in books.iterdir()) == sorted(keep)
    finally:
        svc.shutdown()
    BookService(g, tmp_path / "nothing-yet", lambda: None).shutdown()  # no folder: nothing to sweep


def test_preview_skips_rendering_and_jobs_share_one_plate_cache(client, app_parts, g, monkeypatch):
    import primer.book.model as model
    import primer.book.service as service

    a = page(g, "Trebuchet")
    page(g, "Counterweight", a, T0 + 1)
    real_render = model.render

    def no(*a, **kw):
        raise AssertionError("the preview rendered a page")

    monkeypatch.setattr(model, "render", no)
    monkeypatch.setattr(model, "clean_svg", no)
    pre = client.post("/v1/books/preview", {"scope": {"kind": "all"}, "include": {}})
    assert pre["chapters"] == 2 and pre["title"] == "My wanderings"
    monkeypatch.setattr(model, "render", real_render)
    seen = []
    for name in ("write_epub", "write_html"):
        real = getattr(service, name)

        def spy(*a, real=real, **kw):
            seen.append(kw.get("plates"))
            return real(*a, **kw)

        monkeypatch.setattr(service, name, spy)
    r = client.post("/v1/books", {"scope": {"kind": "all"}, "include": {}}, expect=202)
    assert wait_book(client, r["id"])["status"] == "done"
    assert len(seen) == 2 and isinstance(seen[0], dict) and seen[0] is seen[1]
