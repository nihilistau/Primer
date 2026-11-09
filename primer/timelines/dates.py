"""Normalising a date as a text writes it ("c. 1277", "the 12th century", "300 BC") to a year
(negative before the common era), with month and day when given, a precision and whether it
is approximate. Our parser reads the text first; the model's reading is used only when ours
can't and it is a whole year in range."""
from __future__ import annotations

import calendar
import re

MIN_YEAR = -10000
MAX_YEAR = 2100
PRECISIONS = ("day", "month", "year", "decade", "century", "millennium")
_MAX_TEXT = 120

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12,
    "december": 12,
}
_MONTH = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20, "twenty-first": 21,
}
_NO_DATE = {"unknown", "undated", "none", "n/a", "na", "?", "-", "unclear", "not given", "no date"}

_APPROX = re.compile(r"^(?:c\.|ca\.|c(?=\s*\d)|circa|around|about|approximately|approx\.?|roughly|~)\s*",
                     re.IGNORECASE)
_BC = r"(?:b\.?\s?c\.?(?:\s?e\.?)?)"
_AD = r"(?:a\.?\s?d\.?|c\.?\s?e\.?)"
_ERA = rf"(?P<era>{_BC}|{_AD})"
_LEAD = re.compile(r"^(?:in|on|from|by|during|since|the\s+year|the|year)\s+", re.IGNORECASE)
_DASH = re.compile(r"\s*[-‐‑‒–—―]\s*|\s+to\s+|\s+until\s+")

_ISO = re.compile(r"^(?P<y>\d{3,4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})$")
_DMY = re.compile(rf"^(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+(?P<y>\d{{1,4}})(?:\s+{_ERA})?$",
                  re.IGNORECASE)
_MDY = re.compile(rf"^{_MONTH}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{1,4}})(?:\s+{_ERA})?$",
                  re.IGNORECASE)
_MY = re.compile(rf"^(?:(?:early|mid|late)[\s-]+)?{_MONTH},?\s+(?:of\s+)?(?P<y>\d{{1,4}})(?:\s+{_ERA})?$",
                 re.IGNORECASE)
_YEAR = re.compile(rf"^(?:(?P<pre>{_AD})\s*)?(?P<y>\d{{1,5}})(?:\s*{_ERA})?$", re.IGNORECASE)
_RANGE = re.compile(rf"^(?:between\s+)?(?:(?P<pre>{_AD})\s*)?(?P<a>\d{{1,5}})(?:\s*(?P<era1>{_BC}|{_AD}))?"
                    rf"(?:\s*[-‐‑‒–—―/]\s*|\s+(?:to|until|and)\s+)"
                    rf"(?P<b>\d{{1,5}})(?:\s*(?P<era2>{_BC}|{_AD}))?$", re.IGNORECASE)
_DECADE = re.compile(r"^(?:(?P<part>early|mid|middle|late)[\s-]+)?(?P<y>\d{2,3}0)['’]?s$", re.IGNORECASE)
# a part of a decade ("early 1990s" -> 1992) or, for a round hundred, of a century ("mid-1800s" -> 1850)
_DECADE_PART = {"early": 2, "mid": 5, "middle": 5, "late": 8}
_HUNDREDS_PART = {"early": 10, "mid": 50, "middle": 50, "late": 90}
_PART = r"(?:(?P<part>early|mid|middle|late)[\s-]+)?"
_CENTURY = re.compile(rf"^{_PART}(?P<n>\d{{1,3}}(?:st|nd|rd|th)|[a-z]+(?:-[a-z]+)?)\s+"
                      rf"(?P<unit>century|c\.|cent\.?|millennium)(?:\s+{_ERA})?$", re.IGNORECASE)
_LOOSE_YEAR = re.compile(rf"(?<![\d.:/-])(?P<y>\d{{3,4}})(?![\d:/])(?:\s*{_ERA})?", re.IGNORECASE)


def _result(year, month=None, day=None, end_year=None, precision="year", approx=False) -> dict | None:
    if not isinstance(year, int) or year == 0 or not MIN_YEAR <= year <= MAX_YEAR:
        return None
    if month is not None:
        if not 1 <= month <= 12:
            return None
        if day is not None:
            limit = calendar.monthrange(year, month)[1] if 1 <= year <= 9999 else 31
            if not 1 <= day <= limit:
                return None
    if end_year is not None and (end_year == 0 or not MIN_YEAR <= end_year <= MAX_YEAR or end_year <= year):
        end_year = None
    return {"year": year, "month": month, "day": day, "end_year": end_year,
            "precision": precision, "approx": approx}


def _signed(y: int, era: str | None) -> int:
    return -y if era and re.match(r"b", era, re.IGNORECASE) else y


def _ordinal(token: str) -> int | None:
    token = token.lower()
    m = re.fullmatch(r"(\d{1,3})(st|nd|rd|th)", token)
    if m:
        return int(m.group(1))
    return _ORDINAL_WORDS.get(token)


def _parse(text: str, approx: bool) -> dict | None:
    m = _ISO.match(text)
    if m:
        return _result(int(m["y"]), int(m["m"]), int(m["d"]), precision="day", approx=approx)
    for pattern in (_DMY, _MDY):
        m = pattern.match(text)
        if m:
            return _result(_signed(int(m["y"]), m["era"]), _MONTHS[m["mon"].lower()], int(m["d"]),
                           precision="day", approx=approx)
    m = _MY.match(text)
    if m:
        return _result(_signed(int(m["y"]), m["era"]), _MONTHS[m["mon"].lower()],
                       precision="month", approx=approx)
    m = _YEAR.match(text)
    if m:
        if m["pre"] and m["era"]:
            return None  # "AD 79 BC"
        return _result(_signed(int(m["y"]), m["era"]), approx=approx)
    m = _RANGE.match(text)
    if m:
        if m["pre"] and (m["era1"] or m["era2"]) and re.match("b", m["era1"] or m["era2"], re.I):
            return None
        era = m["era2"] or m["era1"]
        a_txt, b_txt = m["a"], m["b"]
        a = _signed(int(a_txt), m["era1"] or era)
        if len(b_txt) < len(a_txt) and not m["era2"] and a > 0:  # "1277-83"
            b = int(a_txt[:len(a_txt) - len(b_txt)] + b_txt)
        else:
            b = _signed(int(b_txt), era)
        return _result(a, end_year=b, approx=approx) if b > a else _result(a, approx=approx)
    m = _DECADE.match(text)
    if m:
        year, part = int(m["y"]), (m["part"] or "").lower()
        if not part:
            return _result(year, precision="decade", approx=approx)
        if year % 100 == 0:  # "the mid-1800s" is a part of a century
            return _result(year + _HUNDREDS_PART[part], precision="century", approx=approx)
        return _result(year + _DECADE_PART[part], precision="decade", approx=approx)
    m = _CENTURY.match(text)
    if m:
        n = _ordinal(m["n"])
        if n is None or n < 1:
            return None
        unit = "millennium" if m["unit"].lower() == "millennium" else "century"
        size = 1000 if unit == "millennium" else 100
        offset = {"early": 10, "mid": 50, "middle": 50, "late": 90}.get((m["part"] or "mid").lower(), 50)
        offset = offset * size // 100
        bc = bool(m["era"]) and re.match("b", m["era"], re.IGNORECASE) is not None
        start = (n - 1) * size
        year = -(start + size - offset) if bc else start + offset
        return _result(year, precision=unit, approx=approx)
    return None


def _loose(text: str, approx: bool) -> dict | None:
    """One year inside other words ("the summer of 1282"); None when there are none or several."""
    found = {(int(m["y"]), (m["era"] or "").lower()) for m in _LOOSE_YEAR.finditer(text)}
    if len(found) != 1:
        return None
    y, era = next(iter(found))
    return _result(_signed(y, era), approx=approx)


def _guess(model_guess) -> int | None:
    if isinstance(model_guess, bool):
        return None
    if isinstance(model_guess, int):
        return model_guess
    if isinstance(model_guess, float) and model_guess.is_integer():
        return int(model_guess)
    if isinstance(model_guess, str) and re.fullmatch(r"\s*-?\d{1,5}\s*", model_guess):
        return int(model_guess)
    return None


def normalise(date_text, model_guess=None) -> dict | None:
    """{year, month, day, end_year, precision, approx} for a date as written, else the model's
    whole-year reading (approximate), else None. Years outside -10000..2100 are dropped."""
    text = date_text if isinstance(date_text, str) else ""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text[:_MAX_TEXT * 4]).split())
    if len(text) > _MAX_TEXT:
        text = ""
    text = text.strip(" .,;:")
    if text.casefold() in _NO_DATE:
        return None
    original = text
    approx = False
    while True:
        m = _APPROX.match(text)
        if not m:
            break
        approx, text = True, text[m.end():]
    while True:
        m = _LEAD.match(text)
        if not m:
            break
        text = text[m.end():]
    if text:
        got = _parse(text, approx)
        if got is not None:
            return got
        if _looks_dated(text):
            return None  # a date we read and refused (out of range, no such day): no guessing
        got = _loose(text, approx)
        if got is not None:
            return got
    if not _NAMES_A_DATE.search(original):
        return None  # "not stated", "...": the model's year would be a guess at nothing
    year = _guess(model_guess)
    return _result(year, approx=True) if year is not None else None


# a date text the model's year may stand for: it has a digit, a century, decade or millennium
# word, an era mark, or a regnal number ("the reign of Edward I")
_NAMES_A_DATE = re.compile(r"\d|\b(?i:centur(?:y|ies)|decades?|millenni(?:um|a))\b|\b(?:BCE?|AD|CE)\b"
                           r"|\b[A-Z][a-z]+\s+[IVXL]+\b")


def _looks_dated(text: str) -> bool:
    """A form we parse but whose value was refused: the model's guess must not override it."""
    return any(p.match(text) for p in (_ISO, _DMY, _MDY, _MY, _YEAR, _RANGE, _DECADE, _CENTURY))
