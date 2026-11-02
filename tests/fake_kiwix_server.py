"""A fake kiwix-serve for tests: /catalog/v2/entries (Atom), /search?format=xml (RSS),
/suggest (JSON), /content/<book>/<path>. Also a script: python tests/fake_kiwix_server.py --port P"""
from __future__ import annotations

import argparse
import json
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

DEFAULT_BOOKS = {
    "wikipedia_en": {
        "Whale": ("Whale", "<html><body><article><p>Whales are marine mammals. They breathe air.</p></article></body></html>"),
        "Whale_shark": ("Whale shark", "<html><body><p>The whale shark is a fish.</p></body></html>"),
    }
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def _send(self, body: str, ctype: str, code: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        books = self.server.books
        parts = urlsplit(self.path)
        q = parse_qs(parts.query)
        if parts.path == "/catalog/v2/entries":
            # like kiwix-serve: <name> is the catalog name; the content name is in the html link
            entries = "".join(f"<entry><name>{escape(n.rsplit('_', 1)[0])}</name>"
                              f"<title>{escape(n.replace('_', ' '))}</title>"
                              f'<link type="text/html" href="/content/{escape(n)}" /></entry>'
                              for n in books)
            return self._send('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                              + entries + "</feed>", "application/atom+xml")
        if parts.path == "/search":
            pat = q.get("pattern", [""])[0].lower()
            n = int(q.get("pageLength", ["25"])[0])
            items = []
            for book, pages in books.items():
                for path, (title, html) in pages.items():
                    if pat and pat in title.lower():
                        items.append(f"<item><title>{escape(title)}</title>"
                                     f"<link>/content/{book}/{quote(path)}</link>"
                                     f"<description>{escape(title)} snippet</description></item>")
            return self._send('<?xml version="1.0"?><rss version="2.0"><channel>'
                              + "".join(items[:n]) + "</channel></rss>", "application/rss+xml")
        if parts.path == "/suggest":
            book = q.get("content", [""])[0]
            term = q.get("term", [""])[0].lower()
            wrap = getattr(self.server, "highlight", False)
            out = [{"label": f"<b>{t}</b>" if wrap else t, "value": t, "kind": "path", "path": p}
                   for p, (t, _h) in books.get(book, {}).items() if term in t.lower()]
            out.append({"label": f"containing '{term}'...", "value": term, "kind": "pattern"})
            return self._send(json.dumps(out), "application/json")
        if parts.path.startswith("/content/"):
            _, _, book, path = unquote(parts.path).split("/", 3)
            page = books.get(book, {}).get(path)
            if page:
                return self._send(page[1], "text/html; charset=utf-8")
        self._send("not found", "text/plain", 404)


class FakeKiwix(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, books: dict) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.books = books

    def kill(self) -> None:
        self.shutdown()
        self.server_close()


def serve(books: dict | None = None, port: int = 0) -> tuple[FakeKiwix, str]:
    server = FakeKiwix(port, DEFAULT_BOOKS if books is None else books)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--port", type=int, default=8830)
    args, _ = ap.parse_known_args()
    try:
        FakeKiwix(args.port, DEFAULT_BOOKS).serve_forever()
    except KeyboardInterrupt:
        pass
