import pytest

from resume_kit.contracts.dates_in_words import with_dates_in_words
from resume_kit.contracts.resume_language import ResumeLanguage


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Head Baker at Acme Bakery since 2019-06.", "Head Baker at Acme Bakery since June 2019."),
        ("Started 2019-06-15 as a baker", "Started June 15, 2019 as a baker"),
        ("From 2019-06 to 2021-03", "From June 2019 to March 2021"),
        ("Led the line (2019-06\u20132021-03)", "Led the line (June 2019 \u2013 March 2021)"),
        ("Led the line 2019-06 - 2021-03", "Led the line June 2019 \u2013 March 2021"),
        ("Led the line 2019-06-2021-03", "Led the line June 2019 \u2013 March 2021"),
        ("Hired 2019-06. Promoted 2020-01.", "Hired June 2019. Promoted January 2020."),
        (
            "Since 2019-06, wrote for https://example.com/2019-06 and 2020-01",
            "Since June 2019, wrote for https://example.com/2019-06 and January 2020",
        ),
        ("Opened 2020-01, 2020-12 and 2021-07", "Opened January 2020, December 2020 and July 2021"),
        ("Head Baker since 2019-06-present", "Head Baker since June 2019-present"),
    ],
)
def test_a_year_and_month_in_prose_is_written_in_words(text: str, expected: str) -> None:
    assert with_dates_in_words(text, "en") == expected


@pytest.mark.parametrize(
    "text",
    [
        "Certified to ISO 27001-2013",
        "Ran 2019 and 2020 audits",
        "Shipped release A2019-06 to 4,000 stores",
        "Part 2019-06x was replaced",
        "Grew sales 2019-13 percent",  # no month 13: not a date
        "Ticket ABC-2019-06 closed",
        "Served 1200-1500 guests a night",
        "Published at https://example.com/2019-06-15/report",
        "Published at www.example.com/news/2019-06",
        "Published at example.com/2019-06",
        "Published at example.com:8080/2019-06/report",
        "Published at example.com?since=2019-06",
        "Published at example.com#2019-06",
        "Attached menu-2019-06.pdf and 2019-06.pdf",
        "Coached the 2011-12 team and led the club 2010-11",  # two years, not a month
        "Cut cost 2000-10% in a year",
        "Opened 1850-06 as a mill",
        "Inspected 2019-02-30 and 2019-06-31",  # no such day
        "Ran 2019-06 - 2019-02-30",  # one part of the span is no date
        "Ran 2019-06-32 shifts",  # a hyphen and a number after the month: no date
    ],
)
def test_text_that_is_not_a_date_is_left_as_it_is(text: str) -> None:
    assert with_dates_in_words(text, "en") == text


def test_a_month_that_could_be_two_years_is_a_month_when_the_person_saved_it() -> None:
    saved = frozenset({"2010-11"})
    assert with_dates_in_words("Joined 2010-11", "en", saved) == "Joined November 2010"
    assert with_dates_in_words("Joined 2011-12", "en", saved) == "Joined 2011-12"
    # A day makes it a date whatever was saved.
    assert with_dates_in_words("Joined 2010-11-05", "en") == "Joined November 5, 2010"


@pytest.mark.parametrize(
    ("language", "month", "day"),
    [
        ("en", "June 2019", "June 1, 2019"),
        ("de", "Juni 2019", "1. Juni 2019"),
        ("fr", "juin 2019", "1er juin 2019"),
        ("es", "junio de 2019", "1 de junio de 2019"),
        ("it", "giugno 2019", "1 giugno 2019"),
        ("pt", "junho de 2019", "1 de junho de 2019"),
        ("nl", "juni 2019", "1 juni 2019"),
    ],
)
def test_the_words_are_those_of_the_resume_language(
    language: ResumeLanguage, month: str, day: str
) -> None:
    assert with_dates_in_words("2019-06", language) == month
    assert with_dates_in_words("2019-06-01", language) == day


def test_a_day_other_than_the_first_has_no_ordinal_in_french() -> None:
    assert with_dates_in_words("2019-06-15", "fr") == "15 juin 2019"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Head Baker since 2019-06", "Head Baker since 2019"),
        ("Hired 2019-06-15.", "Hired 2019."),
        ("Led the line 2019-06 - 2021-03", "Led the line 2019\u20132021"),
        ("Led the line 2019-06-2021-03", "Led the line 2019\u20132021"),
        ("Led the line 2019-06 to 2019-11", "Led the line 2019 to 2019"),
        ("Led the line 2019-06 \u2013 2019-11", "Led the line 2019"),
        ("Coached the 2010-11 team", "Coached the 2010-11 team"),
        ("Published at example.com/2019-06", "Published at example.com/2019-06"),
    ],
)
def test_a_date_as_a_year_alone_is_never_more_words(text: str, expected: str) -> None:
    written = with_dates_in_words(text, "en", year_only=True)
    assert written == expected
    assert len(written.split()) <= len(text.split())
