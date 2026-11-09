"""The diagram's SVG is untrusted model output: parsed, then rebuilt as a new tree holding only
allowlisted elements and attributes with checked values (design §3). Nothing from the model is
copied through unparsed: every kept value is re-validated and re-written, the text content is
plain text that ElementTree escapes, and the root is always our own
`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 700">`.

Refused outright: more than MAX_BYTES, any DOCTYPE or ENTITY declaration (entity bombs, external
entities), anything that isn't well-formed XML with an <svg> root, and drawings with fewer than
MIN_LABELS distinct labels. Dropped (with their contents): every element not on the allowlist,
such as script, style, foreignObject, image, a, use, animate, set, filter, mask and clipPath.

References are only `marker-start/mid/end="url(#id)"` to a marker in the same drawing, never from
inside a marker (no chains, no cycles); every id is written with a per-drawing prefix so a
drawing can't shadow the page's own ids or another drawing's. Every pattern match is linear:
scalar values are capped at MAX_SCALAR characters before matching, lists at MAX_POINTS."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from primer.diagrams import DiagramError

MAX_BYTES = 60 * 1024
MAX_ELEMENTS = 80
MAX_DEPTH = 32  # elements nested deeper are dropped (the rebuild recurses)
MAX_TEXT = 60
MIN_LABELS = 3
MAX_POINTS = 4000  # characters of one `d`, `points` or dash list
MAX_SCALAR = 32  # characters of any other attribute value (a transform: MAX_TRANSFORM)
MAX_TRANSFORM = 200
MIN_VIEW = 10.0  # a model view box narrower or lower than this is ignored
NUM_MIN, NUM_MAX = -2000.0, 3000.0
VIEW_W, VIEW_H = 1000.0, 700.0
SVG_NS = "http://www.w3.org/2000/svg"
ID_PREFIX = "dg-"  # the default prefix of every kept id (DOM clobbering, clashes between drawings)

TOO_LARGE = "the drawing is too large"
DECLARES = "the drawing declares entities"
UNREADABLE = "the drawing could not be read"
TOO_FEW = "the drawing has too few labels"

_PRESENTATION = ("fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap",
                 "stroke-linejoin", "opacity", "fill-opacity", "stroke-opacity", "transform")
_FONT = ("font-size", "font-weight", "font-family", "text-anchor", "dominant-baseline")
_MARKERS = ("marker-start", "marker-mid", "marker-end")
_SHAPE = _PRESENTATION + _MARKERS

# element -> the attributes it may keep (besides `id`, which any element may have)
ALLOWED = {
    "g": _SHAPE + _FONT,
    "line": ("x1", "y1", "x2", "y2") + _SHAPE,
    "polyline": ("points",) + _SHAPE,
    "polygon": ("points",) + _SHAPE,
    "rect": ("x", "y", "width", "height", "rx", "ry") + _PRESENTATION,
    "circle": ("cx", "cy", "r") + _PRESENTATION,
    "ellipse": ("cx", "cy", "rx", "ry") + _PRESENTATION,
    "path": ("d",) + _SHAPE,
    "text": ("x", "y") + _PRESENTATION + _FONT,
    "tspan": ("x", "y", "dx", "dy") + _PRESENTATION + _FONT,
    "title": (),
    "defs": (),
    "marker": ("markerWidth", "markerHeight", "refX", "refY", "orient", "markerUnits",
               "viewBox") + _PRESENTATION,
}
ALLOWED_ELEMENTS = frozenset(ALLOWED) | {"svg"}
_TEXT_ELEMENTS = ("text", "tspan", "title")

# unambiguous: digits, then an optional fraction (no way to split one run of digits two ways)
_NUM = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_NUM_ATTR = re.compile(rf"\s*({_NUM.pattern})\s*(?:px)?\s*")
_ID = re.compile(r"[A-Za-z][\w-]{0,30}", re.ASCII)
_PREFIX = re.compile(r"[A-Za-z][\w-]{0,15}", re.ASCII)
_D = re.compile(r"[MmLlHhVvCcSsQqTtAaZz0-9 .,\-eE+\t\r\n]*")
_LIST = re.compile(r"[0-9 .,\-eE+\t\r\n]*")
_TRANSFORM_PART = re.compile(r"\s*(translate|rotate|scale)\s*\(([^()]*)\)\s*,?")
_URL_REF = re.compile(r"\s*url\(\s*#([A-Za-z][\w-]{0,30})\s*\)\s*", re.ASCII)
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
_COLOURS = frozenset((
    "none", "black", "white", "gray", "grey", "silver", "lightgray", "lightgrey", "darkgray",
    "darkgrey", "dimgray", "dimgrey", "whitesmoke", "red", "darkred", "green", "darkgreen",
    "lightgreen", "blue", "darkblue", "lightblue", "navy", "skyblue", "steelblue", "yellow",
    "lightyellow", "gold", "orange", "brown", "tan", "beige", "ivory", "wheat", "maroon", "olive",
    "teal", "purple", "pink", "currentcolor"))
_KEYWORDS = {
    "stroke-linecap": ("butt", "round", "square"),
    "stroke-linejoin": ("miter", "round", "bevel"),
    "text-anchor": ("start", "middle", "end"),
    "dominant-baseline": ("auto", "middle", "central", "hanging", "alphabetic", "mathematical"),
    "font-weight": ("normal", "bold", "bolder", "lighter", "100", "200", "300", "400", "500",
                    "600", "700", "800", "900"),
    "markerUnits": ("strokeWidth", "userSpaceOnUse"),
}
_FAMILIES = ("serif", "sans-serif", "monospace")
_NON_NEGATIVE = ("width", "height", "r", "rx", "ry", "stroke-width", "font-size", "markerWidth",
                 "markerHeight")
_COORDS = ("x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "dx", "dy", "refX", "refY") + _NON_NEGATIVE
_LONG = ("d", "points", "stroke-dasharray", "viewBox")
_SPACE = re.compile(r"\s+")


def _fmt(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _in_range(v: float) -> bool:
    return math.isfinite(v) and NUM_MIN <= v <= NUM_MAX


def _number(text: str) -> float | None:
    """A plain number (an optional `px`) inside NUM_MIN..NUM_MAX, else None."""
    if len(text) > MAX_SCALAR:
        return None
    m = _NUM_ATTR.fullmatch(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v if _in_range(v) else None


def _numbers(text: str) -> list[float] | None:
    """Every number in a list (path data, points), or None when any is out of range."""
    if len(text) > MAX_POINTS:
        return None
    out = []
    for m in _NUM.finditer(text):
        try:
            v = float(m.group(0))
        except ValueError:
            return None
        if not _in_range(v):
            return None
        out.append(v)
    return out


def _transform(text: str) -> str | None:
    """translate/rotate/scale with 1 to 3 numbers each, re-written; None for anything else."""
    if len(text) > MAX_TRANSFORM:
        return None
    parts, pos = [], 0
    while pos < len(text):
        m = _TRANSFORM_PART.match(text, pos)
        if not m:
            return None
        args = [a for a in re.split(r"[\s,]+", m.group(2).strip()) if a]
        values = [_number(a) for a in args]
        if not 1 <= len(values) <= 3 or any(v is None for v in values):
            return None
        if m.group(1) != "rotate" and len(values) > 2:
            return None
        parts.append(f"{m.group(1)}({' '.join(_fmt(v) for v in values)})")
        pos = m.end()
    return " ".join(parts) or None


def _value(name: str, raw: str) -> str | None:
    """The checked, re-written value of an allowlisted attribute, or None to drop it."""
    raw = raw.strip()
    limit = MAX_POINTS if name in _LONG else MAX_TRANSFORM if name == "transform" else MAX_SCALAR
    if not raw or len(raw) > limit:
        return None
    if name in ("fill", "stroke"):
        low = raw.lower()
        return low if _HEX.fullmatch(raw) or low in _COLOURS else None
    if name in ("opacity", "fill-opacity", "stroke-opacity"):
        v = _number(raw)
        return _fmt(v) if v is not None and 0 <= v <= 1 else None
    if name in _COORDS:
        v = _number(raw)
        if v is None or (name in _NON_NEGATIVE and v < 0):
            return None
        return _fmt(v)
    if name in _KEYWORDS:
        return raw if raw in _KEYWORDS[name] else None
    if name == "font-family":
        for family in raw.split(","):
            family = family.strip().strip("'\"").lower()
            if family in _FAMILIES:
                return family
        return None
    if name == "d":
        if not _D.fullmatch(raw) or _numbers(raw) is None:
            return None
        return _SPACE.sub(" ", raw)
    if name == "points":
        values = _numbers(raw) if _LIST.fullmatch(raw) else None
        if not values or len(values) % 2:
            return None
        return " ".join(f"{_fmt(values[i])},{_fmt(values[i + 1])}" for i in range(0, len(values), 2))
    if name == "stroke-dasharray":
        if raw == "none":
            return raw
        values = _numbers(raw) if _LIST.fullmatch(raw) else None
        if not values or any(v < 0 for v in values):
            return None
        return " ".join(_fmt(v) for v in values)
    if name == "transform":
        return _transform(raw)
    if name == "orient":
        if raw in ("auto", "auto-start-reverse"):
            return raw
        v = _number(raw)
        return _fmt(v) if v is not None else None
    if name == "viewBox":
        values = _numbers(raw) if _LIST.fullmatch(raw) else None
        if not values or len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            return None
        return " ".join(_fmt(v) for v in values)
    if name in _MARKERS:
        m = _URL_REF.fullmatch(raw)
        return f"url(#{m.group(1)})" if m else None
    return None


def _split(tag) -> tuple[str | None, str] | None:
    """(namespace, local name) of an element or attribute name; None for a comment/PI."""
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return None, tag


def _plain(text: str | None) -> str:
    """Text content as one line: control characters out, spaces collapsed, at most MAX_TEXT."""
    text = "".join(ch if ch.isprintable() else " " for ch in (text or "")[:MAX_POINTS])
    return _SPACE.sub(" ", text).strip()[:MAX_TEXT].strip()


def _join(a: str | None, b: str) -> str:
    return _plain(f"{a or ''} {b}")


class _Builder:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.count = 0
        self.ids: set[str] = set()
        self.markers: set[str] = set()

    def children(self, src: ET.Element, dst: ET.Element, depth: int, in_text: bool = False,
                 in_marker: bool = False) -> None:
        for child in src:
            kept = self.element(child, dst, depth, in_text, in_marker)
            if kept is None and in_text:  # "a <b>x</b> c": the words after a dropped child stay
                tail = _plain(child.tail)
                if tail:
                    if len(dst):
                        dst[-1].tail = _join(dst[-1].tail, tail)
                    else:
                        dst.text = _join(dst.text, tail)

    def element(self, src: ET.Element, parent: ET.Element, depth: int, in_text: bool,
                in_marker: bool) -> ET.Element | None:
        split = _split(src.tag)
        if split is None or split[0] not in (None, SVG_NS):
            return None  # foreign namespaces (xhtml, ...) are dropped with their contents
        name = split[1]
        if name == "svg":  # a nested drawing becomes a plain group
            name = "g"
        if name not in ALLOWED or self.count >= MAX_ELEMENTS or depth >= MAX_DEPTH:
            return None
        if in_text != (name == "tspan"):  # tspans only inside text, nothing else inside it
            return None
        if in_marker and name == "marker":  # no nested markers
            return None
        out = ET.SubElement(parent, name)
        if split[1] != "svg":
            self.attributes(src, out, name, in_marker)
        self.count += 1
        if name in _TEXT_ELEMENTS:
            text = _plain(src.text)
            if text:
                out.text = text
        if name == "title":
            return out
        self.children(src, out, depth + 1, in_text=name in ("text", "tspan"),
                      in_marker=in_marker or name == "marker")
        if name == "tspan":
            tail = _plain(src.tail)
            if tail:
                out.tail = tail
        if name == "marker" and out.get("id"):
            self.markers.add(out.get("id")[len(self.prefix):])
        return out

    def attributes(self, src: ET.Element, out: ET.Element, name: str, in_marker: bool) -> None:
        allowed = ALLOWED[name]
        for key, raw in src.attrib.items():
            split = _split(key)
            if split is None or split[0] is not None or not isinstance(raw, str):
                continue  # namespaced attributes (xlink:href, ...) are never kept
            key = split[1]
            if key == "id":
                if _ID.fullmatch(raw) and raw not in self.ids:
                    out.set("id", self.prefix + raw)
                    self.ids.add(raw)
                continue
            if key not in allowed or (in_marker and key in _MARKERS):
                continue  # inside a marker: no marker references (no chains, no cycles)
            value = _value(key, raw)
            if value is not None:
                out.set(key, value)


def _scale_for(view_box: str | None) -> str | None:
    """The transform that fits the model's own view box into 1000x700. None when it already
    does, when the box is unusable or tiny (under MIN_VIEW), or when the fit would need a
    number outside NUM_MIN..NUM_MAX."""
    if not view_box or len(view_box) > MAX_SCALAR * 4:
        return None
    values = _numbers(view_box) if _LIST.fullmatch(view_box.strip()) else None
    if not values or len(values) != 4 or values[2] < MIN_VIEW or values[3] < MIN_VIEW:
        return None
    x, y, w, h = values
    if (x, y, w, h) == (0, 0, VIEW_W, VIEW_H):
        return None
    s = min(VIEW_W / w, VIEW_H / h)
    tx, ty = -x * s, -y * s
    if not all(_in_range(v) for v in (s, tx, ty)):
        return None
    parts = []
    if tx or ty:
        parts.append(f"translate({_fmt(tx)} {_fmt(ty)})")
    if s != 1:
        parts.append(f"scale({_fmt(s)})")
    return " ".join(parts) or None


def _resolve_markers(root: ET.Element, b: _Builder) -> None:
    """A marker reference must name a marker in this drawing (rewritten to its prefixed id);
    others are dropped."""
    for el in root.iter():
        for key in _MARKERS:
            ref = el.get(key)
            if ref is None:
                continue
            if ref[5:-1] in b.markers:
                el.set(key, f"url(#{b.prefix}{ref[5:-1]})")
            else:
                del el.attrib[key]


def _labels(root: ET.Element) -> list[str]:
    out, seen = [], set()
    for el in root.iter("text"):
        words = " ".join(t for t in (_plain(s) for s in el.itertext()) if t)
        label = _SPACE.sub(" ", words).strip()[:MAX_TEXT].strip()
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            out.append(label)
    return out


def clean_svg(raw: str, prefix: str = ID_PREFIX) -> tuple[str, list[str]]:
    """The drawing rebuilt from the allowlist, and its labels (the text of each <text>, in order,
    without repeats). Every kept id starts with `prefix` (letters, digits, _ and -; ValueError
    otherwise). Raises DiagramError with the reason when the drawing can't be used."""
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ValueError("bad id prefix")
    if not isinstance(raw, str) or not raw.strip():
        raise DiagramError(UNREADABLE)
    if len(raw.encode("utf-8", "replace")) > MAX_BYTES:
        raise DiagramError(TOO_LARGE)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        raise DiagramError(DECLARES)
    try:
        src = ET.fromstring(raw)  # expat: no DTD (refused above), so no entities of any kind
    except (ET.ParseError, ValueError):
        raise DiagramError(UNREADABLE) from None
    split = _split(src.tag)
    if split is None or split[0] not in (None, SVG_NS) or split[1] != "svg":
        raise DiagramError(UNREADABLE)
    root = ET.Element("svg", {"xmlns": SVG_NS, "viewBox": f"0 0 {int(VIEW_W)} {int(VIEW_H)}"})
    fit = _scale_for(src.get("viewBox"))
    holder = ET.SubElement(root, "g", {"transform": fit}) if fit else root
    b = _Builder(prefix)
    b.children(src, holder, depth=1 if fit else 0)
    _resolve_markers(root, b)
    labels = _labels(root)
    if len(labels) < MIN_LABELS:
        raise DiagramError(TOO_FEW)
    return ET.tostring(root, encoding="unicode"), labels
