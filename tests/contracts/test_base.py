from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from resume_kit.contracts.base import (
    CareerDate,
    NonblankStr,
    StrictModel,
    TrimmedNonblankStr,
    TrimmedStr,
    dated_after_the_month_of,
    dates_in_order,
    holds_text_json_cannot_store,
    same_name_key,
)


class _TrimmedContract(StrictModel):
    label: TrimmedStr


class _TrimmedNonblankContract(StrictModel):
    label: TrimmedNonblankStr


class _NonblankContract(StrictModel):
    label: NonblankStr


def test_trimmed_str_strips_surrounding_whitespace() -> None:
    contract = _TrimmedContract.model_validate({"label": "  resume  "})
    assert contract.label == "resume"


def test_trimmed_nonblank_str_strips_and_rejects_whitespace() -> None:
    contract = _TrimmedNonblankContract.model_validate({"label": "  resume  "})
    assert contract.label == "resume"

    with pytest.raises(ValidationError):
        _TrimmedNonblankContract.model_validate({"label": "   "})


def test_nonblank_str_rejects_whitespace_without_trimming() -> None:
    with pytest.raises(ValidationError):
        _NonblankContract.model_validate({"label": "   "})


def test_nonblank_str_preserves_surrounding_whitespace() -> None:
    contract = _NonblankContract.model_validate({"label": "  resume  "})
    assert contract.label == "  resume  "


@pytest.mark.parametrize("character", ["\x00", "\x01", "\x0b", "\x0c", "\x1f", "\ud800", "￿"])
def test_trimmed_nonblank_str_rejects_characters_xml_cannot_store(character: str) -> None:
    with pytest.raises(ValidationError, match="control"):
        _TrimmedNonblankContract.model_validate({"label": f"Built{character}systems"})


@pytest.mark.parametrize("character", ["\t", "\n", "\r", "\x7f", "é", "—"])
def test_trimmed_nonblank_str_keeps_characters_xml_can_store(character: str) -> None:
    contract = _TrimmedNonblankContract.model_validate({"label": f"Built{character}systems"})
    assert contract.label == f"Built{character}systems"


class _DateContract(StrictModel):
    date: CareerDate


@pytest.mark.parametrize("date", ["1900", "2019", "2100", "1900-01", "2019-03", "2100-12"])
def test_a_career_date_is_a_year_with_or_without_its_month(date: str) -> None:
    assert _DateContract.model_validate({"date": date}).date == date


@pytest.mark.parametrize(
    "date", ["", "19", "19-03", "2019-3", "2019-00", "2019-13", "2019/03", "March", "2019-03-15"]
)
def test_other_spellings_are_not_a_career_date(date: str) -> None:
    with pytest.raises(ValidationError, match="Expected YYYY or YYYY-MM"):
        _DateContract.model_validate({"date": date})


@pytest.mark.parametrize("date", ["1899", "2101", "1899-12", "2101-01", "0000-01", "9999-12"])
def test_a_career_date_keeps_the_plausible_year_range(date: str) -> None:
    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        _DateContract.model_validate({"date": date})


# Full-width, Arabic-Indic and Devanagari digits: they look like a date but are not ASCII.
@pytest.mark.parametrize(
    "date",
    [
        "\uff12\uff10\uff11\uff19",
        "\uff12\uff10\uff11\uff19-03",
        "\u0662\u0660\u0661\u0669-\u0660\u0663",
        "2019-\u0966\u0969",
    ],
)
def test_only_ascii_digits_make_a_career_date(date: str) -> None:
    with pytest.raises(ValidationError):
        _DateContract.model_validate({"date": date})


@pytest.mark.parametrize(
    "start,end",
    [
        ("2019-03", "2019-03"),
        ("2019-03", "2021-01"),
        ("2019", "2019"),
        # A year alone may be any month of that year, so it is never before a month inside it.
        ("2019", "2019-01"),
        ("2019-12", "2019"),
        ("2019-06", "2020"),
    ],
)
def test_dates_in_order_give_a_year_alone_the_benefit_of_the_doubt(start: str, end: str) -> None:
    assert dates_in_order(start, end)


@pytest.mark.parametrize(
    "start,end",
    [("2019-03", "2019-02"), ("2020", "2019"), ("2020", "2019-12"), ("2020-01", "2019")],
)
def test_an_end_before_its_start_is_out_of_order(start: str, end: str) -> None:
    assert not dates_in_order(start, end)


OCTOBER_2026 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("date", ["2026-10", "2026", "2026-09", "2025", "1990-01"])
def test_this_month_and_earlier_are_not_after_it(date: str) -> None:
    # A year alone may be any month of it, so this year alone is never after this month.
    assert not dated_after_the_month_of(date, OCTOBER_2026)


@pytest.mark.parametrize("date", ["2026-11", "2027", "2027-01", "2031-06"])
def test_a_later_month_or_year_is_after_this_month(date: str) -> None:
    assert dated_after_the_month_of(date, OCTOBER_2026)


@pytest.mark.parametrize(
    "value",
    ["a\x00b", "\ud800", "\udc00", {"roles": [{"id": "r\x00"}]}, ["fine", ["\x00"]]],
    ids=["nul", "high-half", "low-half", "nested-in-a-dict", "nested-in-a-list"],
)
def test_text_the_database_json_type_refuses_is_found_wherever_it_is(value: object) -> None:
    assert holds_text_json_cannot_store(value)


@pytest.mark.parametrize(
    "value",
    ["a\x0cb", "\ufeffposting", "\ufffe", "tab\tand\nline", "😀", 0, None, {"ok": [1, "x"]}],
    ids=[
        "form-feed",
        "byte-order-mark",
        "noncharacter",
        "whitespace",
        "emoji",
        "int",
        "none",
        "dict",
    ],
)
def test_what_the_json_type_holds_is_left_for_the_other_checks(value: object) -> None:
    # A form feed comes with text read from a file, and a byte-order mark with text pasted from a
    # web page; the worker drops the mark itself (`resume/start_workflow.py`).
    assert not holds_text_json_cannot_store(value)


# The same pairs as `sameNameKey` in the web page's copy
@pytest.mark.parametrize(
    "first,second",
    [
        ("Maßprüfung", "MASSPRÜFUNG"),
        ("Straße", "STRAẞE"),
        ("product  management", "Product Management"),
        (" Wound\tcare ", "wound care"),
        ("Ｐｙｔｈｏｎ", "python"),  # noqa: RUF001 - full-width letters on purpose
        ("Food\u200b safety", "Food safety"),
        ("Ά\u0345", "ά\u03b9"),
        ("Python\u200d", "\u200cpython"),
    ],
)
def test_same_name_key(first: str, second: str) -> None:
    assert same_name_key(first) == same_name_key(second)


@pytest.mark.parametrize(
    "first,second",
    [
        ("برنامه\u200cنویسی", "برنامهنویسی"),
        ("Kırım", "Kirim"),  # noqa: RUF001 - the dotless i on purpose
        ("Product management", "Product managers"),
    ],
)
def test_same_name_key_keeps_apart(first: str, second: str) -> None:
    assert same_name_key(first) != same_name_key(second)
