# ruff: noqa: RUF001  (these tests deliberately contain look-alike characters)
"""Which characters the PDF can print is data the contracts hold, checked against the fonts."""

import json
import unicodedata
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from reportlab.pdfbase.ttfonts import TTFont

from resume_kit.contracts.printable import (
    PRINTABLE_RANGES,
    VARIATION_SELECTOR_RANGES,
    WHITE_SPACE_RANGES,
    as_shown,
    in_capitals,
    unprintable_by_location,
    unprintable_characters,
    without_variation_selectors,
)

FONTS = Path(__file__).resolve().parents[2] / "src/resume_kit/documents/fonts"


def _characters_in(font_file: str) -> set[int]:
    return set(TTFont(font_file, str(FONTS / font_file)).face.charWidths)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]


def test_the_ranges_are_exactly_what_both_bundled_fonts_can_print() -> None:
    """A heading is bold and a bullet is not, so a character must be in both fonts."""
    in_both = _characters_in("NotoSans-Regular.ttf") & _characters_in("NotoSans-Bold.ttf")
    listed = {code for first, last in PRINTABLE_RANGES for code in range(first, last + 1)}
    assert listed == in_both


def test_the_ranges_are_in_order_and_do_not_touch() -> None:
    """One way to write the list, so the copy the browser holds can be compared with it."""
    for (_, last), (first, _) in pairwise(PRINTABLE_RANGES):
        assert first > last + 1


def test_names_each_unprintable_character_once_in_the_order_it_appears() -> None:
    assert unprintable_characters("Cut latency → 40 ms ✓, then → 20 ms.") == ("→", "✓")


def test_ordinary_resume_text_is_printable() -> None:
    text = "Backend · APIs — “Zoë” led 3 teams (2019–2023), cut costs 40% & saved €1.2M…"
    assert unprintable_characters(text) == ()


def test_letters_the_font_has_are_printable_and_letters_it_lacks_are_not() -> None:
    assert unprintable_characters("Müller Søren Łukasz Жанна Αθήνα") == ()
    assert unprintable_characters("李明 built it") == ("李", "明")


def test_white_space_is_never_named() -> None:
    """A line break or a tab draws nothing, whether or not the font lists it."""
    assert unprintable_characters("one\ttwo\nthree　four") == ()


# --- what draws nothing ---------------------------------------------------------------------


def test_the_white_space_ranges_are_what_python_calls_white_space() -> None:
    """Written out so the browser can hold the same list; this keeps it equal to `isspace`."""
    listed = {code for first, last in WHITE_SPACE_RANGES for code in range(first, last + 1)}
    assert listed == {code for code in range(0x110000) if chr(code).isspace()}


def test_the_mark_a_phone_adds_after_a_sign_is_never_named() -> None:
    """ "©" typed on a phone is "©" plus an invisible mark asking for the emoji look."""
    assert unprintable_characters("Acme©️ and Beta™️") == ()


def test_a_sign_the_font_lacks_is_named_without_its_invisible_mark() -> None:
    assert unprintable_characters("Done ✔️") == ("✔",)


@pytest.mark.parametrize(
    "text",
    ["\ufe0f", "  \ufe0f  ", "\ufe0fAcme", "Acme \ufe0f Inc.", "\ufe0f\ufe0f", "line\n\ufe0f"],
    ids=["alone", "between spaces", "at the start", "after a space", "two alone", "after a break"],
)
def test_an_invisible_mark_with_no_character_before_it_is_named(text: str) -> None:
    """It belongs to the character before it. Left alone (the sign was deleted and the mark
    stayed behind) it would make a text that looks empty and prints as nothing."""
    assert unprintable_characters(text) == ("\ufe0f",)


def test_several_marks_after_one_character_all_belong_to_it() -> None:
    assert unprintable_characters("Acme©\ufe0f\ufe0f") == ()
    assert unprintable_characters("葛\U000e0100 and ©\ufe0e") == ("葛",)


def test_the_text_a_pdf_draws_has_no_variation_selectors() -> None:
    assert without_variation_selectors("Acme©️, 1️, 葛\U000e0100") == "Acme©, 1, 葛"
    assert without_variation_selectors("Plain text.") == "Plain text."


# --- characters people really paste -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Zero​width space, soft­hyphen, word⁠joiner, no break space",
        "Non-breaking hy‑phen and thin space",
        "The ligatures of a copied PDF: efﬁcient workﬂow",
        "• ‣ ⁃ bullets the font has",
        "½ ⅓ 20°C ±5% × ÷ ‰ № ™ © ® € £ ₹ ₽ ₿",
        "‘single’ “double” ‚low‘ «guillemets» ‹single› 5′ 10″",
        "é written as a letter and a separate accent",
    ],
)
def test_text_copied_from_documents_is_printable(text: str) -> None:
    assert unprintable_characters(text) == ()


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Cut costs −30%", ("−",)),  # the minus sign of Word and LaTeX, not a hyphen
        ("▪ square bullet ➢ arrowhead ◦ white bullet", ("▪", "➢", "◦")),
        ("≥ ≤ ≈ ≠ ∞ √", ("≥", "≤", "≈", "≠", "∞", "√")),
        ("← ↑ ↓ ⇒ ↔", ("←", "↑", "↓", "⇒", "↔")),
        ("★ ● ■ ◆ ☎ ✉", ("★", "●", "■", "◆", "☎", "✉")),
        ("עברית", ("ע", "ב", "ר", "י", "ת")),
        ("한국어", ("한", "국", "어")),
        ("हिन्दी", ("ह", "ि", "न", "्", "द", "ी")),
    ],
)
def test_signs_and_scripts_the_font_lacks_are_named(text: str, named: tuple[str, ...]) -> None:
    assert unprintable_characters(text) == named


def test_an_emoji_is_one_character_and_is_named_once() -> None:
    assert unprintable_characters("Shipped 🚀 fast 🚀🚀") == ("🚀",)


def test_an_emoji_made_of_several_is_named_by_its_parts_without_the_joiner() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"  # man, joiner, woman, joiner, girl
    thumbs_up = "\U0001f44d\U0001f3fd"  # thumbs up, skin tone

    assert unprintable_characters(family) == ("\U0001f468", "\U0001f469", "\U0001f467")
    assert unprintable_characters(thumbs_up) == ("\U0001f44d", "\U0001f3fd")


def test_storing_text_never_changes_whether_it_can_be_printed() -> None:
    """Saved text is put in one normal form (NFC). The browser checks text as it is typed.

    If the two could differ, the editor would mark a field the server accepts, or send one the
    server refuses. So for every character: the same answer before and after.
    """
    differing = [
        f"U+{code:04X}"
        for code in range(0x110000)
        if not 0xD800 <= code <= 0xDFFF
        and bool(unprintable_characters(chr(code)))
        != bool(unprintable_characters(unicodedata.normalize("NFC", chr(code))))
    ]
    assert differing == []


def test_a_letter_and_a_separate_accent_print_exactly_when_the_joined_letter_does() -> None:
    letters = [chr(code) for code in (*range(0x41, 0x250), *range(0x370, 0x530))]
    accents = [chr(code) for code in range(0x300, 0x370)]
    differing = [
        letter + accent
        for letter in letters
        if not unprintable_characters(letter)
        for accent in accents
        if bool(unprintable_characters(letter + accent))
        != bool(unprintable_characters(unicodedata.normalize("NFC", letter + accent)))
    ]
    assert differing == []


def test_text_the_pdf_prints_in_capitals_stays_printable() -> None:
    """The name, an employer and the headline are printed in capitals.

    Which letters have a capital depends on the Python that runs: each version brings its own list
    (Unicode 16, in Python 3.14, gave "ƛ" and "ɤ" capitals that no font here holds). So whatever
    the list says, a letter the fonts hold never turns into one they lack.
    """
    printable = (chr(code) for first, last in PRINTABLE_RANGES for code in range(first, last + 1))
    lost_in_capitals = [
        character
        for character in printable
        if not unprintable_characters(character) and unprintable_characters(in_capitals(character))
    ]
    assert lost_in_capitals == []


def test_a_letter_whose_capital_the_fonts_lack_stays_as_it_is() -> None:
    # "ↄ" has a capital in every Python, and the fonts lack it.
    assert in_capitals("ↄ") == "ↄ"
    assert in_capitals("aↄb ƛ ɤ")[:3] == "AↄB"
    assert not unprintable_characters(in_capitals("ƛɤ"))


@pytest.mark.parametrize(
    "text", ["Zoë Müller", "straße", "ǆ ǉ ǌ", "ὀδυσσεύς", "İstanbul ı", "Example Inc. 42", ""]
)
def test_every_other_text_is_put_in_capitals_as_python_does_it(text: str) -> None:
    assert in_capitals(text) == text.upper()


# --- finding them inside a contract -----------------------------------------------------------


def test_each_text_is_found_with_where_it_sits() -> None:
    dumped = {
        "summary": {"text": "Engineer → Lead", "summary_description": None},
        "rows": [{"title": "Fine", "bullets": ["ok", "Go ✓ → Rust"]}],
        "count": 3,
        "current": True,
        "ratio": 0.5,
    }

    assert list(unprintable_by_location(dumped)) == [
        (("summary", "text"), ("→",)),
        (("rows", 0, "bullets", 1), ("✓", "→")),
    ]


def test_nothing_is_found_where_there_is_no_text() -> None:
    assert list(unprintable_by_location({"a": None, "b": [1, 2.5, True], "c": {}, "d": []})) == []
    assert list(unprintable_by_location("→", ("top",))) == [(("top",), ("→",))]


def test_the_capitals_the_browser_is_held_to_are_the_ones_printed() -> None:
    """`examples/capitals.json` holds each letter the PDF can print whose capital differs from it.

    The browser draws the preview and makes the copied text with a rule of its own
   , and its test reads this
    file. So the file must say what `in_capitals` says, for every character and on every Python.
    """
    written = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/capitals.json").read_text(encoding="utf-8")
    )
    printed = {
        chr(code): in_capitals(chr(code))
        for first, last in PRINTABLE_RANGES
        for code in range(first, last + 1)
    }

    assert written == {letter: capital for letter, capital in printed.items() if capital != letter}
    assert len(written) > 800


# --- one set of examples for the server and the browser -------------------------------------

_EXAMPLES = json.loads(
    (Path(__file__).resolve().parents[2] / "examples/printable-characters.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("example", _EXAMPLES, ids=[example["case"] for example in _EXAMPLES])
def test_the_shared_examples_are_answered_as_the_browser_answers_them(
    example: dict[str, Any],
) -> None:
    """the web page's copy reads the same file."""
    named = unprintable_characters(example["text"])

    assert list(named) == example["named"]
    assert [as_shown(character) for character in named] == example["shown"]


def test_a_character_nobody_can_see_is_shown_by_its_number() -> None:
    """Named as it is, an invisible mark would leave a hole in the sentence."""
    assert as_shown("\ufe0f") == "U+FE0F (invisible)"
    assert as_shown("\U000e0100") == "U+E0100 (invisible)"
    assert as_shown("\u180b") == "U+180B (invisible)"  # a Mongolian variation selector
    assert as_shown("\u0092") == "U+0092 (invisible)"  # a control character, from bad decoding
    assert as_shown("\u061c") == "U+061C (invisible)"  # a format character
    assert as_shown("→") == "→"
    assert as_shown("\U0001f680") == "\U0001f680"


def test_every_variation_selector_unicode_defines_is_in_the_list() -> None:
    """The four blocks of them: Mongolian, the sixteen common ones, and the supplement."""
    listed = {code for first, last in VARIATION_SELECTOR_RANGES for code in range(first, last + 1)}
    named_so = {
        code for code in range(0x110000) if "VARIATION SELECTOR" in unicodedata.name(chr(code), "")
    }
    assert listed == named_so
