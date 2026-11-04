"""Kiwix's OPDS catalogue: the Wikipedia ZIMs on offer, with size, date and download address."""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from primer.setup.net import CatalogueError, Net

CATALOGUE = "https://library.kiwix.org/catalog/v2/entries"
_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(entry, tag: str) -> str:
    el = entry.find(_ATOM + tag)
    return (el.text or "").strip() if el is not None else ""


def parse(xml_bytes: bytes, lang: str, name_prefix: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise CatalogueError("the Kiwix catalogue is not valid XML") from None
    out = []
    for entry in root.findall(_ATOM + "entry"):
        name = _text(entry, "name")
        if not name.startswith(name_prefix) or lang not in _text(entry, "language").split(","):
            continue
        found = None
        for link in entry.findall(_ATOM + "link"):
            href = link.get("href", "")
            if (link.get("rel") or "").startswith("http://opds-spec.org/acquisition") \
                    and href.endswith(".zim.meta4"):
                found = link
                break
        if found is None:
            continue
        url = href[: -len(".meta4")]
        if urllib.parse.urlsplit(url).scheme != "https":
            continue
        try:
            size = int(found.get("length") or 0)  # rounded up to the next KiB, not exact
        except ValueError:
            size = 0
        out.append({
            "name": name,
            "flavour": _text(entry, "flavour"),
            "date": _text(entry, "updated")[:10],
            "size": size,
            "url": url,
            "sha256_url": url + ".sha256",
        })
    out.sort(key=lambda z: (z["name"], z["flavour"]))
    return out


def zims(lang: str = "eng", name_prefix: str = "wikipedia_en_", net: Net | None = None) -> list[dict]:
    """[{name, flavour, date, size, url, sha256_url}] from the live catalogue."""
    q = urllib.parse.urlencode({"lang": lang, "count": "-1"})
    resp = (net or Net()).get(f"{CATALOGUE}?{q}", accept="application/atom+xml")
    if resp.status >= 400:
        raise CatalogueError(f"the Kiwix catalogue answered {resp.status}")
    return parse(resp.body, lang, name_prefix)
