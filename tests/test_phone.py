"""Israeli phone normalization (hermescloak.phone): a dozen spellings → one NSN; non-phones → None."""
import pytest

from hermescloak.phone import CANDIDATE, normalize


@pytest.mark.parametrize("raw", [
    "+972547765611", "+972-54-776-5611", "972-54-7765611", "972547765611", "00972547765611",
    "+972 (0)54 776 5611", "054-7765611", "054 776 5611", "0547765611", "054.776.5611",
])
def test_every_spelling_of_one_mobile_gives_one_nsn(raw):
    assert normalize(raw) == "547765611"


@pytest.mark.parametrize("raw,nsn", [
    ("03-1234567", "31234567"), ("+972 3 1234567", "31234567"), ("00972-3-1234567", "31234567"),
    ("08 123 4567", "81234567"), ("077-1234567", "771234567"), ("076-5401234", "765401234"),
])
def test_landlines_and_voip(raw, nsn):
    assert normalize(raw) == nsn


@pytest.mark.parametrize("raw", [
    "05.10.2026", "2026-10-09", "50,000", "12345-06-23", "1-800-123-456", "000000018",  # ID: NSN starts with 0
    "123456789", "2021", "012345678901", "4580 1234 5678 9012",
])
def test_dates_amounts_case_numbers_ids_are_not_phones(raw):
    assert normalize(raw) is None


def test_candidate_finds_each_spelling_inside_text():
    text = ("נייד +972 (0)54-776-5611, משרד 03-1234567, פקס 00972-3-7654321 ו-054.776.5611; "
            "תאריך 05.10.2026, סך 50,000 ש\"ח")
    found = [m.group(0) for m in CANDIDATE.finditer(text) if normalize(m.group(0))]
    assert found == ["+972 (0)54-776-5611", "03-1234567", "00972-3-7654321", "054.776.5611"]
