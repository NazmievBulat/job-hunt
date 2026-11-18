"""Shared Pydantic primitives for external contract boundaries."""

import re
import unicodedata
from datetime import datetime
from typing import Annotated, cast

from pydantic import AfterValidator, BaseModel, ConfigDict

# Characters that are invisible on screen even though they are not whitespace: format characters
# (zero-width space, joiners, soft hyphen, byte-order mark, direction marks) and a few "blank"
# letters. They are trimmed from the ends of text, and text made only of them counts as blank.
_ALSO_BLANK = frozenset("\u034f\u115f\u1160\u17b4\u17b5\u180e\u2800\u3164\uffa0")
_NOISE_CATEGORIES = frozenset({"Cf", "Zs", "Zl", "Zp"})

# Direction overrides, embeddings and isolates can make text display as something it is not.
_BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")

# Code points XML 1.0 cannot store; DOCX rendering fails on them.
_XML_INCOMPATIBLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")

# What PostgreSQL's JSON type cannot hold (a NUL character), and half of a surrogate pair, which
# cannot be written as UTF-8 at all. A run holding either could not be read back by the database.
_NOT_STORABLE_AS_JSON = re.compile("[\x00\ud800-\udfff]")

# The refusals below are named so that the API can tell which one it is and answer a person in
# plain words (`contracts/plain_words.py`). The text itself is what a model reads when its answer is
# repaired, so it stays as it is.
BLANK = "Value cannot be blank."
DIRECTION_CONTROL = "Value cannot contain text-direction override characters."
UNSTORABLE_CHARACTER = "Value cannot contain control or noncharacter code points."
END_BEFORE_START = "End date cannot be before start date."


class StrictModel(BaseModel):
    """Reject unknown fields and scalar coercion at external boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _trim_string(value: str) -> str:
    return value.strip()


TrimmedStr = Annotated[str, AfterValidator(_trim_string)]


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError(BLANK)
    return value


NonblankStr = Annotated[str, AfterValidator(_require_nonblank)]


def _is_noise(character: str) -> bool:
    return (
        character.isspace()
        or character in _ALSO_BLANK
        or unicodedata.category(character) in _NOISE_CATEGORIES
    )


def _strip_noise(value: str) -> str:
    start, end = 0, len(value)
    while start < end and _is_noise(value[start]):
        start += 1
    while end > start and _is_noise(value[end - 1]):
        end -= 1
    return value[start:end]


def holds_unseen_character(text: str) -> bool:
    """Whether the text holds a character that shows nothing and is not a space: a format
    character (zero-width space, byte-order mark, ...) or a blank letter."""
    return any(
        character in _ALSO_BLANK or unicodedata.category(character) == "Cf" for character in text
    )


def stored_text(value: str) -> str:
    """Text as a `TrimmedNonblankStr` keeps it: one form for each accented letter, nothing unseen
    at its ends. For counting what was sent the way it will be kept."""
    return _strip_noise(unicodedata.normalize("NFC", value))


def _require_trimmed_nonblank(value: str) -> str:
    # NFC so the same visible text is always stored the same way ("e" + accent becomes "é").
    normalized = unicodedata.normalize("NFC", value)
    if _BIDI_CONTROLS.search(normalized):
        raise ValueError(DIRECTION_CONTROL)
    stripped = _strip_noise(normalized)
    if not stripped:
        raise ValueError(BLANK)
    if _XML_INCOMPATIBLE.search(stripped):
        raise ValueError(UNSTORABLE_CHARACTER)
    return stripped


TrimmedNonblankStr = Annotated[str, AfterValidator(_require_trimmed_nonblank)]


def _require_storable(value: str) -> str:
    if _XML_INCOMPATIBLE.search(value):
        raise ValueError(UNSTORABLE_CHARACTER)
    return value


# Text a person types that may be empty and run over several lines (a note): refused only for the
# characters `TrimmedNonblankStr` refuses as unstorable. Tabs and line breaks are kept.
StorableStr = Annotated[str, AfterValidator(_require_storable)]

_SPACE_RUNS = re.compile(r"\s+")


def single_spaced(value: str) -> str:
    """One-line text with each run of whitespace in it made one space, a single tab, line break or
    no-break space included, as a paste often doubles or mixes them.

    The screen and the PDF draw a run as one space, but Word prints every one of them."""
    return _SPACE_RUNS.sub(" ", value)


# The zero-width non-joiner and joiner change how a word is spelled in Persian, Hindi and other
# scripts ("برنامه‌نویسی"), so a name keeps them. Direction controls are left for the checks
# that refuse them.
_KEPT_FORMAT_CHARACTERS = re.compile("[\u200c\u200d\u202a-\u202e\u2066-\u2069]")


def without_format_characters(value: str) -> str:
    """The text without the invisible format characters that do not change how it is spelled."""
    return "".join(
        character
        for character in value
        if unicodedata.category(character) != "Cf" or _KEPT_FORMAT_CHARACTERS.match(character)
    )


def _folded(text: str) -> str:
    # NFKC again after folding, as Unicode's caseless match of compatible text does: a fold can
    # leave a letter and its accent apart where NFKC joins them.
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def same_name_key(name: str) -> str:
    """What two names have in common when they are the same skill, title or certification.

    Width, case (full case folding, so "ß" and "SS" match), spacing at the ends and inside, and
    format characters are ignored. The browser decides the same way (`sameNameKey` in
    the web page's copy), so what it shows as one is saved as one.
    """
    # A joiner or non-joiner at an end joins nothing, and a saved name is trimmed of it.
    return " ".join(_folded(without_format_characters(name)).split()).strip(" \u200c\u200d")


def same_certification_name_key(name: str) -> str:
    """Compare certification names without one sentence-ending ASCII full stop.

    Keep repeated dots and dotted final tokens (acronyms such as P.M.P., or .NET.).
    This is comparison only: the approved spelling and other kinds of names stay intact.
    Check the original punctuation before NFKC so look-alike stops do not extend the rule.
    """
    trimmed = re.sub(r"^[\s\u200c\u200d]+|[\s\u200c\u200d]+$", "", without_format_characters(name))
    key = same_name_key(trimmed)
    if trimmed.endswith(".") and key[:-1].strip() and "." not in key.split()[-1][:-1]:
        return same_name_key(trimmed[:-1])
    return key


def _require_name(value: str) -> str:
    # Keep the existing refusals (especially direction overrides), then remove invisible
    # format characters inside names so a pasted name has the same stored identity.
    checked = _require_trimmed_nonblank(value)
    return _require_trimmed_nonblank(without_format_characters(checked))


NameStr = Annotated[str, AfterValidator(_require_name)]


def holds_text_json_cannot_store(value: object) -> bool:
    """Whether any text in `value` (a model's dump: dicts, lists and strings) holds a character
    the database's JSON type refuses. Checked where a request is taken in and not by the
    contract, so what was stored before the check existed is still read."""
    if isinstance(value, str):
        return _NOT_STORABLE_AS_JSON.search(value) is not None
    if isinstance(value, dict):
        items = cast(dict[object, object], value).values()
        return any(holds_text_json_cannot_store(item) for item in items)
    if isinstance(value, list):
        return any(holds_text_json_cannot_store(item) for item in cast(list[object], value))
    return False


def without_unstorable_characters(text: str) -> str:
    """Text without the characters `TrimmedNonblankStr` refuses.

    For text nobody typed here (a file's text, a model's copy of it): a direction control is
    taken out, and a control code point, which is usually a line break of some kind, becomes a
    space.
    """
    return _XML_INCOMPATIBLE.sub(" ", _BIDI_CONTROLS.sub("", text))


MIN_YEAR = 1900
MAX_YEAR = 2100
_CAREER_DATE_PATTERN = re.compile(r"^([0-9]{4})(?:-(0[1-9]|1[0-2]))?$")  # ASCII digits only


YEAR_OUT_OF_RANGE = f"Year must be between {MIN_YEAR} and {MAX_YEAR}."
DATE_FORMAT = "Invalid date format. Expected YYYY or YYYY-MM."


def require_plausible_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(YEAR_OUT_OF_RANGE)


def _require_career_date(date: str) -> str:
    match = _CAREER_DATE_PATTERN.fullmatch(date)
    if match is None:
        raise ValueError(DATE_FORMAT)
    require_plausible_year(int(match.group(1)))
    return date


# A date as a person writes it on a resume: the year, or the year and its month ("2019",
# "2019-03"). A month is never added to a year alone, so a resume prints what was saved.
CareerDate = Annotated[str, AfterValidator(_require_career_date)]


def _month_index(date: str, default_month: int) -> int:
    match = _CAREER_DATE_PATTERN.fullmatch(date)
    assert match is not None  # dates are validated before they are compared
    month = int(match.group(2)) if match.group(2) is not None else default_month
    return int(match.group(1)) * 12 + month


def dated_after_the_month_of(date: str, now: datetime) -> bool:
    """Whether a date is after the month `now` is in.

    A year alone may be any month of it, as in `dates_in_order`, so this year alone is never after.
    """
    return _month_index(date, 1) > now.year * 12 + now.month


def dates_in_order(start: str, end: str) -> bool:
    """Whether an end date can be on or after its start date.

    A year alone may be any month of that year: a start may be January and an end December. So
    the comparison gives each the benefit of the doubt instead of inventing a month.
    """
    return _month_index(end, 12) >= _month_index(start, 1)
