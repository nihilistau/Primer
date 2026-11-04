"""Browser addresses: web URLs and kiwix:<book>/<path>, and resolving links on a page."""
import pytest

from primer.browse.address import Address, kiwix_address, parse, resolve

BOOK = "wikipedia_en_top_nopic_2026-09"


def test_address_parse_and_resolve():
    a = parse("https://example.org/a/b?q=1#frag")
    assert a.kind == "web" and a.url == "https://example.org/a/b?q=1"
    assert parse("  http://Example.org  ").kind == "web"

    k = parse(f"kiwix:{BOOK}/River_Avon_%28Warwickshire%29")
    assert (k.kind, k.book, k.path) == ("reference", BOOK, "River_Avon_(Warwickshire)")
    assert k.url == kiwix_address(BOOK, "River_Avon_(Warwickshire)")
    assert parse(f"kiwix:{BOOK}/AC/DC#History").path == "AC/DC"

    for bad in ["", "javascript:alert(1)", "data:text/html,x", "file:///c:/x", "ftp://x/",
                "mailto:a@b.c", "http:///nohost", "kiwix:", f"kiwix:{BOOK}", f"kiwix:{BOOK}/",
                f"kiwix:{BOOK}/../catalog", f"kiwix:{BOOK}/a/%2E%2E/b", "kiwix:../x/A", f"kiwix:{BOOK}/A/..%5C..%5Cx",
                f"kiwix:{BOOK}/A\\x",
                "kiwix:bad book/A", "Warwick Castle", "x" * 5000]:
        with pytest.raises(ValueError):
            parse(bad)
    with pytest.raises(ValueError, match="kind of address"):
        parse("javascript:alert(1)")

    # kiwix_address quotes so the result is safe inside a Markdown link
    addr = kiwix_address(BOOK, "A b (c)/<d>")
    assert " " not in addr and "(" not in addr and ")" not in addr and "<" not in addr
    assert parse(addr).path == "A b (c)/<d>"

    web = parse("https://en.example.org/wiki/Warwick")
    assert resolve(web, "/wiki/Kenilworth") == "https://en.example.org/wiki/Kenilworth"
    assert resolve(web, "Kenilworth#History") == "https://en.example.org/wiki/Kenilworth"
    assert resolve(web, "//other.example/x") == "https://other.example/x"
    assert resolve(web, "http://127.0.0.1:8810/v1/x") == "http://127.0.0.1:8810/v1/x"  # shown; Task 2 refuses
    assert resolve(web, "a b(c)") == "https://en.example.org/wiki/a%20b%28c%29"
    for dropped in ["#cite_note-1", "javascript:alert(1)", " JaVa\tScRiPt:alert(1)", "data:x",
                    "mailto:a@b.c", "vbscript:x", "geo:52.2,-1.5", "", "   "]:
        assert resolve(web, dropped) is None, dropped
    assert resolve(web, f"kiwix:{BOOK}/Warwick") is None  # web pages can't reach the library
    assert resolve(parse(f"kiwix:{BOOK}/X"), f"kiwix:{BOOK}/Warwick") == f"kiwix:{BOOK}/Warwick"

    kw = parse(f"kiwix:{BOOK}/Warwick_Castle")
    assert resolve(kw, "Kenilworth_Castle") == f"kiwix:{BOOK}/Kenilworth_Castle"
    assert resolve(kw, "./River_Avon_(Warwickshire)") == kiwix_address(BOOK, "River_Avon_(Warwickshire)")
    assert resolve(kw, "Warwick_Castle#History") == f"kiwix:{BOOK}/Warwick_Castle"
    assert resolve(kw, "../Warwick_Castle") == f"kiwix:{BOOK}/Warwick_Castle"
    assert resolve(kw, f"../{BOOK}/A/Title") == f"kiwix:{BOOK}/A/Title"
    assert resolve(kw, f"/content/{BOOK}/Kenilworth") == f"kiwix:{BOOK}/Kenilworth"
    assert resolve(kw, "#cite_note-3") is None
    assert resolve(kw, "/skins/x.css") is None
    assert resolve(kw, "https://www.example.org/x") == "https://www.example.org/x"
    old = parse(f"kiwix:{BOOK}/A/Warwick_Castle")
    assert resolve(old, "Kenilworth") == f"kiwix:{BOOK}/A/Kenilworth"
    assert resolve(old, "../I/m/pic.png") == f"kiwix:{BOOK}/I/m/pic.png"
    assert isinstance(kw, Address)
