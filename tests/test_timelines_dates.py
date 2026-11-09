import pytest

from primer.timelines.dates import normalise


def d(year, month=None, day=None, end_year=None, precision="year", approx=False):
    return {"year": year, "month": month, "day": day, "end_year": end_year,
            "precision": precision, "approx": approx}


@pytest.mark.parametrize("text, want", [
    ("1277", d(1277)),
    ("in 1277", d(1277)),
    ("c. 1277", d(1277, approx=True)),
    ("circa 1277", d(1277, approx=True)),
    ("around 1277", d(1277, approx=True)),
    ("ca. 1277", d(1277, approx=True)),
    ("AD 79", d(79)),
    ("79 AD", d(79)),
    ("A.D. 79", d(79)),
    ("79 CE", d(79)),
    ("300 BC", d(-300)),
    ("300 BCE", d(-300)),
    ("300 B.C.", d(-300)),
    ("c. 300 BC", d(-300, approx=True)),
    ("1277–1283", d(1277, end_year=1283)),
    ("1277-1283", d(1277, end_year=1283)),
    ("1277-83", d(1277, end_year=1283)),
    ("1277 to 1283", d(1277, end_year=1283)),
    ("between 1277 and 1283", d(1277, end_year=1283)),
    ("300–200 BC", d(-300, end_year=-200)),
    ("May 1282", d(1282, 5, precision="month")),
    ("Sept. 1282", d(1282, 9, precision="month")),
    ("4 June 1282", d(1282, 6, 4, precision="day")),
    ("4th June 1282", d(1282, 6, 4, precision="day")),
    ("June 4, 1282", d(1282, 6, 4, precision="day")),
    ("1282-06-04", d(1282, 6, 4, precision="day")),
    ("the 1640s", d(1640, precision="decade")),
    ("1640s", d(1640, precision="decade")),
    ("the 1640's", d(1640, precision="decade")),
    ("12th century", d(1150, precision="century")),
    ("the 12th century", d(1150, precision="century")),
    ("twelfth century", d(1150, precision="century")),
    ("12th century BC", d(-1150, precision="century")),
    ("early 12th century", d(1110, precision="century")),
    ("mid 12th century", d(1150, precision="century")),
    ("mid-12th century", d(1150, precision="century")),
    ("late 12th century", d(1190, precision="century")),
    ("c. 12th century", d(1150, precision="century", approx=True)),
    ("1st century AD", d(50, precision="century")),
    ("2nd millennium BC", d(-1500, precision="millennium")),
    ("1st millennium", d(500, precision="millennium")),
    ("the summer of 1282", d(1282)),
])
def test_date_forms(text, want):
    assert normalise(text, None) == want


@pytest.mark.parametrize("text", [
    "unknown", "", "   ", "undated", "long ago", "the Middle Ages", "0", "AD 0",
    "12000 BC", "2500", "3000 AD", "31 February 1282", "13/45/1282",
    "120th century", "15th millennium BC",
])
def test_odd_dates_dropped_without_a_guess(text):
    assert normalise(text, None) is None


def test_our_parser_wins_over_the_model():
    assert normalise("1277", 1300) == d(1277)
    assert normalise("12th century", 1101) == d(1150, precision="century")


def test_model_guess_only_when_in_range_and_well_formed():
    assert normalise("the reign of Edward I", 1272) == d(1272, approx=True)
    assert normalise("the reign of Edward I", "1272") == d(1272, approx=True)
    assert normalise("the reign of Edward I", -50) == d(-50, approx=True)
    for bad in (None, 99999, -20000, "soon", 12.5, True, [1272], {"y": 1}, "", "1272.5", 0):
        assert normalise("the reign of Edward I", bad) is None
    assert normalise("unknown", 1272) is None  # the text says it has no date: no guessing


@pytest.mark.parametrize("text", ["not stated", "...", "", "   ", "the text doesn't say", "I can't tell",
                                  "a long time ago", "the Middle Ages"])
def test_no_guess_for_a_date_text_that_names_no_date(text):
    """Final review, item 7: the model's year is used only when the date text has a digit, a
    century, decade or millennium word, an era mark or a regnal number."""
    assert normalise(text, 1277) is None


@pytest.mark.parametrize("text, year", [
    ("the reign of Henry VIII", 1509), ("the late thirteenth century", 1290),
    ("the early decades of the reign", 1300), ("the first millennium BC", -500),
    ("the winter of 1282-3", 1282), ("around the time of the Conquest, AD", 1066),
])
def test_guess_kept_for_a_date_text_that_names_a_date(text, year):
    got = normalise(text, year)
    assert got is not None and got["year"] == year


@pytest.mark.parametrize("text", [
    "c.", "BC", "AD", "-", "–", "s", "century", "the th century", "1277-", "-1277",
    "9" * 400, "1277–1283–1290", "\x00\x01", "<b>1277</b>", "12th century BCE AD",
    "early late 12th century", "1640ss", "May", "June 4", "1282-13-04", "1282-02-30",
])
def test_odd_inputs_never_crash_or_leave_the_range(text):
    got = normalise(text, None)
    if got is not None:
        assert -10000 <= got["year"] <= 2100
        assert got["precision"] in ("day", "month", "year", "decade", "century", "millennium")
        if got["end_year"] is not None:
            assert -10000 <= got["end_year"] <= 2100


def test_range_years_out_of_range_drop_the_end():
    got = normalise("1990–2150", None)
    assert got == d(1990)


@pytest.mark.parametrize("text, want", [
    ("1282/83", d(1282, end_year=1283)),
    ("1282/1283", d(1282, end_year=1283)),
    ("c. 1282/83", d(1282, end_year=1283, approx=True)),
    ("early 1990s", d(1992, precision="decade")),
    ("the mid 1990s", d(1995, precision="decade")),
    ("late 1640s", d(1648, precision="decade")),
    ("mid-1800s", d(1850, precision="century")),
    ("early 1800s", d(1810, precision="century")),
    ("the late 1700s", d(1790, precision="century")),
])
def test_slash_ranges_and_decade_parts(text, want):
    assert normalise(text, None) == want


def test_slash_forms_that_are_not_ranges():
    assert normalise("13/45/1282", None) is None
    assert normalise("1283/1282", None) == d(1283)  # an end before the start is dropped
