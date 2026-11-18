"""A date a model wrote into a sentence, written in words: "since 2019-06" reads "since June 2019".

The writer is given every date as "2019-06", and sometimes copies it into the headline, the
summary or a bullet. A person does not write a date so in a sentence. Code turns it into words
after writing, so nothing sent to a model changes. A role's own dates are not sentences: they stay
"2019-06" and are printed as "Jun 2019" (`printed_date`).

When the words would make a text longer than its limit, the year alone is written ("since 2019"):
it is never more words than the date the model wrote.

The months are written out in full, in the language the resume is printed in. They are not the
short `PrintedWords.months`, which the browser keeps a copy of: these words are written into the
resume's text by the server, and the browser only shows them.
"""

import re
from collections.abc import Collection
from datetime import date

from resume_kit.contracts.resume_language import ResumeLanguage

_MONTHS: dict[ResumeLanguage, tuple[str, ...]] = {
    "en": (
        *("January", "February", "March", "April", "May", "June"),
        *("July", "August", "September", "October", "November", "December"),
    ),
    "de": (
        *("Januar", "Februar", "März", "April", "Mai", "Juni"),
        *("Juli", "August", "September", "Oktober", "November", "Dezember"),
    ),
    "fr": (
        *("janvier", "février", "mars", "avril", "mai", "juin"),
        *("juillet", "août", "septembre", "octobre", "novembre", "décembre"),
    ),
    "es": (
        *("enero", "febrero", "marzo", "abril", "mayo", "junio"),
        *("julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
    ),
    "it": (
        *("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno"),
        *("luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"),
    ),
    "pt": (
        *("janeiro", "fevereiro", "março", "abril", "maio", "junho"),
        *("julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
    ),
    "nl": (
        *("januari", "februari", "maart", "april", "mei", "juni"),
        *("juli", "augustus", "september", "oktober", "november", "december"),
    ),
}

# A year and a month, with a day or without: "2019-06", "2019-06-15".
_DATE = r"((?:19|20)\d\d)-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?"
# One date, or two joined as a span ("2019-06 - 2021-03", "2019-06-2021-03"). Not part of a longer
# number or code ("27001-2013", "A2019-06", "ABC-2019-06", "2019-06x"), nor of a file name
# ("2019-06.pdf") or a share ("2000-10%"); a full stop that ends the sentence is not a file name.
# A hyphen after it may lead to a word ("2019-06-present"), never to a number ("2019-06-32").
_DATES = re.compile(
    rf"(?<![\w-]){_DATE}(?:\s*[-\u2013\u2014]\s*{_DATE})?(?![\w%]|-(?![^\W\d_])|\.\w)"
)
# A web address, with a scheme, "www." or a domain followed by a path, a port or a query: a date
# in it is part of the address and is kept ("https://example.com/2019-06-15/report",
# "example.com?since=2019-06").
_ADDRESS = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.)\S+|[\w-]+(?:\.[\w-]+)+[/:?#]\S*", re.IGNORECASE
)


def _in_words(year: str, month: str, day: str | None, language: ResumeLanguage) -> str:
    name = _MONTHS[language][int(month) - 1]
    if day is None:
        return f"{name} de {year}" if language in ("es", "pt") else f"{name} {year}"
    number = int(day)
    match language:
        case "en":
            return f"{name} {number}, {year}"
        case "de":
            return f"{number}. {name} {year}"
        case "fr":
            return f"{'1er' if number == 1 else number} {name} {year}"
        case "es" | "pt":
            return f"{number} de {name} de {year}"
        case "it" | "nl":
            return f"{number} {name} {year}"


def _is_a_date(year: str, month: str, day: str | None, saved_dates: Collection[str]) -> bool:
    if day is not None:
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            return False  # "2019-02-30" is no day: it is left as it was written
        return True
    # "2010-11" may be the years 2010 to 2011 ("the 2010-11 season"). Read as a month only when
    # the person saved it as one.
    if int(month) == (int(year) + 1) % 100:
        return f"{year}-{month}" in saved_dates
    return True


def with_dates_in_words(
    text: str,
    language: ResumeLanguage,
    saved_dates: Collection[str] = (),
    *,
    year_only: bool = False,
) -> str:
    """`text` with each "2019-06" or "2019-06-15" in it written in words; a span gets a dash.

    `saved_dates` are the dates the person saved ("2010-11"). A web address is left as it is, and
    so is a span with a part that is not a date. With `year_only`, a date is its year and a span
    is "2019\u20132021", one word, so the text has no more words than before.
    """

    def replace(found: re.Match[str]) -> str:
        dates: list[tuple[str, str, str | None]] = [(found[1], found[2], found[3])]
        if found[4] is not None:
            dates.append((found[4], found[5], found[6]))
        if not all(_is_a_date(year, month, day, saved_dates) for year, month, day in dates):
            return found.group()
        if year_only:
            return "\u2013".join(dict.fromkeys(year for year, _, _ in dates))
        return " \u2013 ".join(_in_words(year, month, day, language) for year, month, day in dates)

    parts: list[str] = []
    written = 0
    for address in _ADDRESS.finditer(text):
        parts.append(_DATES.sub(replace, text[written : address.start()]))
        parts.append(address.group())
        written = address.end()
    parts.append(_DATES.sub(replace, text[written:]))
    return "".join(parts)
