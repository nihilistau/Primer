"""Text for the voice: Markdown stripped, numbers spelled out, and cut into sentence-sized chunks.

A voice model reads digits badly ("1068" came back as "768"), so every number is written out
in words, in British style ("two thousand and five", "one hundred and twelve")."""
from __future__ import annotations

import re

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
         "fifteen sixteen seventeen eighteen nineteen").split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_PLURAL_TENS = {10: "tens", 20: "twenties", 30: "thirties", 40: "forties", 50: "fifties",
                60: "sixties", 70: "seventies", 80: "eighties", 90: "nineties"}
_SCALES = ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))
_ORDINAL_WORDS = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                  "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_MAX_CARDINAL = 10 ** 12


def _cardinal(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        rest = n % 100
        return f"{_ONES[n // 100]} hundred" + (f" and {_cardinal(rest)}" if rest else "")
    for scale, name in _SCALES:
        if n >= scale:
            head, rest = f"{_cardinal(n // scale)} {name}", n % scale
            if not rest:
                return head
            return head + (" and " if rest < 100 else " ") + _cardinal(rest)
    raise AssertionError(n)


def _digits(s: str) -> str:
    return " ".join(_ONES[int(c)] for c in s)


def _year(n: int) -> str:
    """1068 -> "ten sixty-eight", 1604 -> "sixteen oh four", 1100 -> "eleven hundred"."""
    if n % 1000 == 0:
        return _cardinal(n)
    head, tail = n // 100, n % 100
    if tail == 0:
        return f"{_cardinal(head)} hundred"
    return f"{_cardinal(head)} {'oh ' + _ONES[tail] if tail < 10 else _cardinal(tail)}"


def _ordinal(n: int) -> str:
    words = _cardinal(n)
    head, sep, last = words.rpartition("-" if "-" in words.rsplit(" ", 1)[-1] else " ")
    if last in _ORDINAL_WORDS:
        last = _ORDINAL_WORDS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + last


def _whole(digits: str, quantity: bool = False) -> str:
    """A whole number in words; a plain 4-digit one reads as a year unless `quantity`."""
    if (len(digits) > 1 and digits[0] == "0") or int(digits) >= _MAX_CARDINAL:
        return _digits(digits)  # 007, or too big to say
    n = int(digits)
    if not quantity and len(digits) == 4 and (1000 <= n < 2000 or 2010 <= n <= 2099):
        return _year(n)
    return _cardinal(n)


def _decade(digits: str) -> str:
    n = int(digits)
    if len(digits) == 2:
        return _PLURAL_TENS[n]
    if n % 1000 == 0:
        return _cardinal(n) + "s"
    if n % 100 == 0:
        return _cardinal(n // 100) + " hundreds"
    return f"{_cardinal(n // 100)} {_PLURAL_TENS[n % 100]}"


# a 4-digit number before one of these counts things rather than naming a year
_COUNTED = re.compile(r"\s*(?:km|kg|cm|mm)\b|\s+(?:soldiers|men|women|people|troops|miles|years|ships|"
                      r"houses|metres|meters|feet|kilometres|kilometers|acres|tons|tonnes|pounds|dollars|"
                      r"deaths|victims|civilians|villagers|inhabitants|words|pages)\b", re.I)

_CURRENCY = re.compile(r"""
    (?P<sym>[£$€])
    (?P<amt>\d{1,3}(?:,\d{3})+|\d+) (?:\.(?P<frac>\d+))?
    (?: \s?(?P<mag>bn|m|k|million|billion|thousand)\b )?
    (?!\d)
""", re.X | re.I)
_MONEY = {"£": ("pound", "pounds", "pence"), "$": ("dollar", "dollars", "cents"),
          "€": ("euro", "euros", "cents")}
_MAGNITUDE = {"bn": "billion", "billion": "billion", "m": "million", "million": "million",
              "k": "thousand", "thousand": "thousand"}


def _money(m: re.Match) -> str:
    one, many, minor = _MONEY[m["sym"]]
    whole = m["amt"].replace(",", "")
    words = _whole(whole, True)
    frac, mag = m["frac"], m["mag"]
    if mag:
        point = f" point {_digits(frac)}" if frac else ""
        return f"{words}{point} {_MAGNITUDE[mag.lower()]} {many}"
    if frac is None:
        return f"{words} {one if whole == '1' else many}"
    if len(frac) == 2:
        cents = int(frac)
        return f"{words} {one if whole == '1' else many}" + (f" {_cardinal(cents)}" if cents else "")
    return f"{words} point {_digits(frac)} {many}"


_NUMBER = re.compile(r"""
    (?<![\w.])
    (?: (?P<dotted>\d+(?:\.\d+){2,})
      | (?P<decade>\d{3}0|\d0)['’]?s\b
      | (?P<ord>\d+)(?:st|nd|rd|th)\b
      | (?P<num>\d{1,3}(?:,\d{3})+ | \d{1,3}(?:[\x20  ]\d{3})+(?!\d) | \d+)
        (?:\.(?P<frac>\d+))?
    ) (?P<pct>%)?
    (?!\d)
""", re.X)


def _number(m: re.Match) -> str:
    if m["dotted"]:  # a version or a date: never half spelled
        return " dot ".join(_digits(g) for g in m["dotted"].split("."))
    if m["decade"]:
        return _decade(m["decade"])
    if m["ord"]:
        digits = m["ord"]
        return _digits(digits) if int(digits) >= _MAX_CARDINAL else _ordinal(int(digits))
    raw = m["num"]
    grouped = not raw.isdigit()
    num = re.sub(r"\D", "", raw)
    rest = m.string[m.end():]
    if m["frac"] is not None:
        out = f"{_whole(num, True)} point {_digits(m['frac'])}"
    else:
        out = _whole(num, grouped or bool(_COUNTED.match(rest)))
    if m["pct"]:
        out += " percent"
    elif rest[:1].isalpha():  # 5km -> "five km"
        out += " "
    return out


_RANGE = re.compile(r"(?<![\d-])(\d{1,4})\s*([–—-])\s*(\d{1,4})(?![\d-])")
_ID = re.compile(r"(?<![\w.-])\d+(?:-\d+)+(?![\w-])")


def _range(m: re.Match) -> str:
    a, dash, b = m.groups()
    if dash == "-" and len(a) == 3 and len(b) == 4:  # 555-1234 is a phone number
        return m[0]
    return f"{a} to {b}"


# unit abbreviations, read only straight after a number ("5 km"); the short ones ("in", "m", "g")
# only when attached ("30m"), so the words "in" and "m" are never touched
_UNITS = {"mph": ("miles per hour", "miles per hour"), "km": ("kilometre", "kilometres"),
          "cm": ("centimetre", "centimetres"), "mm": ("millimetre", "millimetres"),
          "kg": ("kilogram", "kilograms"), "ft": ("foot", "feet"), "mi": ("mile", "miles"),
          "m": ("metre", "metres"), "g": ("gram", "grams"), "in": ("inch", "inches")}
_UNIT = re.compile(r"(?<![\w.])(?P<num>\d+(?:[.,]\d+)*)(?:(?P<sp>\s?)(?P<u>mph|km|cm|mm|kg|ft|mi)"
                   r"|(?P<u2>in|m|g))\b(?![.,]?\d)")
_DEGREES = re.compile(r"(?<![\w.])(?P<num>\d+(?:[.,]\d+)*)\s?°\s?(?P<s>[CF])\b")


def _unit(m: re.Match) -> str:
    u = m["u"] or m["u2"]
    one, many = _UNITS[u]
    return f"{m['num']} {one if m['num'] == '1' else many}"


def _degrees(m: re.Match) -> str:
    return f"{m['num']} degrees {'Celsius' if m['s'] == 'C' else 'Fahrenheit'}"


def spell_numbers(text: str) -> str:
    text = _RANGE.sub(_range, text)  # 1066-1087
    text = _ID.sub(lambda m: ", ".join(_digits(g) for g in m[0].split("-")), text)  # 555-1234
    text = re.sub(r"(?m)(?:^|(?<=[\s(\[]))[-−](?=\d)", "minus ", text)
    text = re.sub(r"\bca?\.\s*(?=\d)", "circa ", text)  # c. 1100
    text = _CURRENCY.sub(_money, text)
    text = _DEGREES.sub(_degrees, text)
    text = _UNIT.sub(_unit, text)
    return _NUMBER.sub(_number, text)


# --- Markdown ---

_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.S | re.M)
_FOOTNOTE_DEF = re.compile(r"^[ \t]*\[\^[^\]]+\]:.*$", re.M)
_LINK_DEF = re.compile(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", re.M)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]\[[^\]]*\]")
_LINK = re.compile(r"\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_CITATION = re.compile(r"[ \t]*(?:\[\^[^\]]*\]|\[\d+(?:\s*[,–-]\s*\d+)*\])")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>]*[^\s<>.,;:!?)\]'\"]")
_SUPERSCRIPT = re.compile("[¹²³⁰⁴-⁹]+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*(?:([-*_])\s*){3,}$|^\s*=+\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_END = re.compile(r"[.!?…][\"')\]”’]*$")


def _link(m: re.Match) -> str:
    text = m[1]
    return "" if re.fullmatch(r"\s*\d+\s*", text) else text


def _inline(line: str) -> str:
    line = re.sub(r"`([^`]*)`", r"\1", line)
    line = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", line)
    line = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", line)
    line = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"\1", line)
    line = re.sub(r"~~(.+?)~~", r"\1", line)
    line = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", line)
    line = line.replace("`", "")
    line = re.sub(r"(?<!\w)\*+|\*+(?!\w)", "", line)  # stray marks, but not 2*3*4
    return re.sub(r"(?<!\w)_+|_+(?!\w)", "", line)


def _terminate(unit: str) -> str:
    unit = unit.rstrip(" ,")
    return unit if _END.search(unit) or unit.endswith((":", ";")) else unit + "."


def _units(md: str) -> list[str]:
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    md = _FENCE.sub("", md)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"</?[A-Za-z][^>]*>", "", md)
    md = _FOOTNOTE_DEF.sub("", md)
    md = _LINK_DEF.sub("", md)
    md = _IMAGE.sub("", md)
    md = _LINK.sub(_link, md)
    md = _CITATION.sub("", md)
    md = _URL.sub("", md)
    md = _SUPERSCRIPT.sub("", md)
    units: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            units.append(" ".join(para))
            para.clear()

    for raw in md.split("\n"):
        line = _QUOTE.sub("", raw)
        if not line.strip():
            flush()
            continue
        if _RULE.match(line) or _TABLE_SEP.match(line):
            flush()
            continue
        block = False
        if (m := _HEADING.match(line)):
            line, block = m[1], True
        elif _LIST.match(line):
            line, block = _LIST.sub("", line, count=1), True
        elif "|" in line:
            line, block = ", ".join(c.strip() for c in line.strip().strip("|").split("|") if c.strip()), True
        line = _inline(line).strip()
        if not line:
            continue
        if block:
            flush()
            units.append(line)
        else:
            para.append(line)
    flush()
    return units


def speakable(markdown: str) -> str:
    """`markdown` as plain sentences for the voice: no markup, links as their text, citations and
    footnote marks dropped, numbers in words, whitespace collapsed. Headings and list items
    become sentences of their own."""
    units = _units(markdown)
    units = [_terminate(u) for u in units[:-1]] + units[-1:]
    text = spell_numbers(" ".join(units))
    return re.sub(r"\s+", " ", text).strip()


# --- chunks ---

_ABBREVIATIONS = frozenset("dr mr mrs ms st mt vs jr sr prof gen col capt lt sgt fr rev hon gov".split())
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]”’]*(?=\s)")


def _sentences(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        if text[m.start()] == ".":
            word = re.search(r"(\w+)$", text[:m.start()])
            if word:
                w = word[1]
                if w.lower() in _ABBREVIATIONS:
                    continue
                if len(w) == 1 and (w.isupper() or text[word.start() - 1:word.start()] == "."):
                    continue  # an initial ("J. R. Tolkien") or "e.g."
        spans.append((start, m.end()))
        start = m.end()
        while start < len(text) and text[start].isspace():
            start += 1
    spans.append((start, len(text)))
    return [(b, e) for b, e in ((b, len(text[:e].rstrip())) for b, e in spans) if e > b]


def _split(text: str, b: int, e: int, limit: int) -> list[tuple[int, int]]:
    out = []
    while e - b > limit:
        cut = 0
        for i in range(limit - 1, limit // 4, -1):  # a comma, semicolon or colon
            if text[b + i] in ",;:" and text[b + i + 1].isspace():
                cut = b + i + 1
                break
        else:
            for i in range(limit, limit // 4, -1):  # else a space
                if text[b + i].isspace():
                    cut = b + len(text[b:b + i].rstrip())
                    break
            else:
                cut = b + limit  # no break to be found
        out.append((b, cut))
        b = cut
        while b < e and text[b].isspace():
            b += 1
    if b < e:
        out.append((b, e))
    return out


def chunks(text: str, limit: int = 300) -> list[tuple[int, int]]:
    """Character ranges into `text`, each at most `limit` long: whole sentences packed together
    while they fit (a longer sentence is cut at a comma, else at a space). They cover the text
    but for whitespace."""
    pieces: list[tuple[int, int]] = []
    for b, e in _sentences(text):
        pieces += _split(text, b, e, limit)
    out: list[tuple[int, int]] = []
    for b, e in pieces:  # pack neighbours while they fit: fewer requests, steadier prosody
        if out and e - out[-1][0] <= limit:
            out[-1] = (out[-1][0], e)
        else:
            out.append((b, e))
    return out
