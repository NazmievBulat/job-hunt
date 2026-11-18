# ruff: noqa: RUF001  (these tests deliberately contain invisible and look-alike characters)
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from resume_kit.contracts.pool import (
    MAX_BULLET_TAGS,
    BulletInput,
    HeadlineTitleInput,
    LooseRole,
    Profile,
    RoleInput,
    SkillInput,
)

INVISIBLE_ONLY = [
    "​",  # zero-width space
    "‌‍",  # zero-width non-joiner and joiner
    "⁠",  # word joiner
    "﻿",  # byte-order mark
    "­",  # soft hyphen
    " ​ ­\t",
    " 　",  # no-break and ideographic space
]


def _role(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "kind": "job",
        "organization": "Acme",
        "title": "Engineer",
        "start_date": "2020-01",
    }
    data.update(overrides)
    return data


# Text that only looks empty


@pytest.mark.parametrize("text", INVISIBLE_ONLY)
def test_text_made_only_of_invisible_characters_is_blank_everywhere(text: str) -> None:
    attempts = [
        lambda: SkillInput.model_validate({"name": text}),
        lambda: SkillInput.model_validate({"name": "Python", "category": text}),
        lambda: HeadlineTitleInput.model_validate({"text": text}),
        lambda: BulletInput.model_validate({"text": text}),
        lambda: BulletInput.model_validate({"text": "ok", "skills": [text]}),
        lambda: RoleInput.model_validate(_role(organization=text)),
        lambda: RoleInput.model_validate(_role(title=text)),
        lambda: RoleInput.model_validate(_role(location=text)),
        lambda: Profile.model_validate({"person": {"name": text}}),
    ]

    for attempt in attempts:
        with pytest.raises(ValidationError):
            attempt()


def test_names_drop_format_characters_but_keep_internal_joiners() -> None:
    assert SkillInput.model_validate({"name": "\u200bPython\ufeff"}).name == "Python"
    assert SkillInput.model_validate({"name": "a\u200bb"}).name == "ab"
    # A zero-width joiner or non-joiner inside text is meaningful (emoji sequences, some scripts).
    assert SkillInput.model_validate({"name": "a\u200db"}).name == "a\u200db"
    assert SkillInput.model_validate({"name": "a\u200cb\u200d"}).name == "a\u200cb"
    assert BulletInput.model_validate({"text": "a\u200db"}).text == "a\u200db"


def test_the_same_visible_text_is_stored_the_same_way() -> None:
    composed = SkillInput.model_validate({"name": "café"}).name
    decomposed = SkillInput.model_validate({"name": "café"}).name

    assert composed == decomposed == "café"


def test_control_characters_are_still_refused() -> None:
    for bad in ("a\x00b", "a\x07b", "a\x1bb"):
        with pytest.raises(ValidationError):
            SkillInput.model_validate({"name": bad})


# Years


# The same range holds for a role read from a file (`LooseRole`) and one being saved (`RoleInput`).


@pytest.mark.parametrize("date", ["1900", "1900-01", "2024", "2100", "2100-12"])
def test_plausible_years_are_accepted(date: str) -> None:
    assert LooseRole.model_validate(_role(start_date=date)).start_date == date


@pytest.mark.parametrize("date", ["1900-01", "2024-06", "2100-12"])
def test_plausible_years_are_accepted_on_a_saved_role(date: str) -> None:
    assert RoleInput.model_validate(_role(start_date=date)).start_date == date


@pytest.mark.parametrize("date", ["0000", "0001", "1899", "1899-12", "2101", "9999", "9999-01"])
def test_implausible_years_are_refused_with_the_range(date: str) -> None:
    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        LooseRole.model_validate(_role(start_date=date))


@pytest.mark.parametrize("date", ["0000-01", "1899-12", "2101-01", "9999-01"])
def test_implausible_years_are_refused_on_a_saved_role(date: str) -> None:
    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        RoleInput.model_validate(_role(start_date=date))


def test_the_end_year_is_range_checked_too() -> None:
    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        LooseRole.model_validate(_role(start_date="2020", end_date="2999"))
    with pytest.raises(ValidationError, match="between 1900 and 2100"):
        RoleInput.model_validate(_role(end_date="2999-01"))


# Size limits: exactly at the limit is fine, one more is refused


LIMIT_CASES: list[tuple[Callable[[int], object], int]] = [
    (lambda n: RoleInput.model_validate(_role(organization="o" * n)), 200),
    (lambda n: RoleInput.model_validate(_role(title="t" * n)), 200),
    (lambda n: RoleInput.model_validate(_role(location="l" * n)), 200),
    (lambda n: SkillInput.model_validate({"name": "s" * n}), 100),
    (lambda n: SkillInput.model_validate({"name": "Go", "category": "c" * n}), 100),
    (lambda n: HeadlineTitleInput.model_validate({"text": "h" * n}), 200),
    (lambda n: BulletInput.model_validate({"text": "b" * n}), 500),
    (lambda n: BulletInput.model_validate({"text": "ok", "skills": ["t" * n]}), 100),
    (lambda n: Profile.model_validate({"person": {"name": "n" * n}}), 200),
    (lambda n: Profile.model_validate({"person": {"name": "A", "phone": "5" * n}}), 50),
    (lambda n: Profile.model_validate({"person": {"name": "A", "location": "c" * n}}), 200),
]


@pytest.mark.parametrize("build,limit", LIMIT_CASES)
def test_every_text_field_has_a_limit(build: Callable[[int], object], limit: int) -> None:
    build(limit)
    with pytest.raises(ValidationError):
        build(limit + 1)


def test_a_megabyte_of_text_is_refused_not_stored() -> None:
    with pytest.raises(ValidationError):
        SkillInput.model_validate({"name": "x" * 1_000_000})
    with pytest.raises(ValidationError):
        Profile.model_validate({"person": {"name": "x" * 1_000_000}})


def test_the_number_of_links_and_tags_is_limited() -> None:
    link = {"url": "https://example.com", "label": "Site"}
    Profile.model_validate({"person": {"name": "A", "links": [link] * 20}})
    with pytest.raises(ValidationError, match="List should have at most 20 items"):
        Profile.model_validate({"person": {"name": "A", "links": [link] * 21}})
    BulletInput.model_validate({"text": "ok", "skills": ["a"] * MAX_BULLET_TAGS})
    with pytest.raises(ValidationError):
        BulletInput.model_validate({"text": "ok", "skills": ["a"] * (MAX_BULLET_TAGS + 1)})


def test_a_long_link_label_is_refused() -> None:
    link = {"url": "https://example.com", "label": "x" * 101}

    with pytest.raises(ValidationError) as refused:
        Profile.model_validate({"person": {"name": "A", "links": [link]}})

    assert [error["loc"] for error in refused.value.errors()] == [("person", "links", 0, "label")]


def test_the_error_names_the_field_that_is_too_long() -> None:
    with pytest.raises(ValidationError) as refused:
        Profile.model_validate({"person": {"name": "n" * 201}})

    [error] = refused.value.errors()
    assert (error["loc"], error["type"]) == (("person", "name"), "string_too_long")


# Link safety


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "ftp://example.com",
        "//example.com",
    ],
)
def test_only_web_links_are_accepted(url: str) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({"person": {"name": "A", "links": [{"url": url, "label": "x"}]}})


MORE_INVISIBLE = [
    "\u200e\u200f",  # left-to-right and right-to-left marks
    "\u2061",  # invisible function application
    "\u180e",  # Mongolian vowel separator
    "\u034f",  # combining grapheme joiner
    "\u2800",  # Braille blank
    "\u3164",  # Hangul filler
    "\u115f\u1160",  # Hangul choseong and jungseong fillers
]


@pytest.mark.parametrize("text", MORE_INVISIBLE)
def test_more_invisible_characters_count_as_blank(text: str) -> None:
    with pytest.raises(ValidationError):
        SkillInput.model_validate({"name": text})
    with pytest.raises(ValidationError):
        Profile.model_validate({"person": {"name": text}})


@pytest.mark.parametrize("override", ["\u202e", "\u202d", "\u202a", "\u2066", "\u2069"])
def test_text_direction_overrides_are_refused_anywhere_in_the_text(override: str) -> None:
    # An override can make "gnp.exe" display as "exe.png": a spoofing trick in generated resumes.
    for text in (override, f"Python{override}", f"Py{override}thon"):
        with pytest.raises(ValidationError, match="direction override"):
            SkillInput.model_validate({"name": text})


def test_direction_marks_are_fine_inside_right_to_left_text() -> None:
    assert SkillInput.model_validate({"name": "\u05e9\u05dc\u05d5\u05dd\u200f 2024"}).name


@pytest.mark.parametrize(
    "date", ["\u0662\u0660\u0662\u0660", "\uff12\uff10\uff12\uff10", "2020-\u0660\u0661"]
)
def test_only_ascii_digits_make_a_date(date: str) -> None:
    for role in (LooseRole, RoleInput):
        with pytest.raises(ValidationError, match="Invalid date format"):
            role.model_validate(_role(start_date=date))
