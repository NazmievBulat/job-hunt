"""Which characters a resume's PDF can print.

The PDF is drawn with two bundled fonts (Noto Sans, regular and bold) and prints nothing for a
character they lack. The list lives here, as data, so that a contract can refuse such a character
without importing the renderer. A test compares it with the font files, and the browser holds a
copy that another test compares with this one.
"""

import unicodedata
from bisect import bisect_right
from collections.abc import Iterator
from typing import cast

# Where a text sits inside a contract, as Pydantic writes a location: field names and positions.
Location = tuple[int | str, ...]

# First and last character of each unbroken run that both fonts hold, in order.
PRINTABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0000),
    (0x000D, 0x000D),
    (0x0020, 0x007E),
    (0x00A0, 0x0377),
    (0x037A, 0x037F),
    (0x0384, 0x038A),
    (0x038C, 0x038C),
    (0x038E, 0x03A1),
    (0x03A3, 0x03E1),
    (0x03F0, 0x052F),
    (0x1AB0, 0x1AC0),
    (0x1C80, 0x1C88),
    (0x1D00, 0x1DF9),
    (0x1DFB, 0x1F15),
    (0x1F18, 0x1F1D),
    (0x1F20, 0x1F45),
    (0x1F48, 0x1F4D),
    (0x1F50, 0x1F57),
    (0x1F59, 0x1F59),
    (0x1F5B, 0x1F5B),
    (0x1F5D, 0x1F5D),
    (0x1F5F, 0x1F7D),
    (0x1F80, 0x1FB4),
    (0x1FB6, 0x1FC4),
    (0x1FC6, 0x1FD3),
    (0x1FD6, 0x1FDB),
    (0x1FDD, 0x1FEF),
    (0x1FF2, 0x1FF4),
    (0x1FF6, 0x1FFE),
    (0x2000, 0x2064),
    (0x2066, 0x2071),
    (0x2074, 0x208E),
    (0x2090, 0x209C),
    (0x20A0, 0x20BF),
    (0x20F0, 0x20F0),
    (0x2100, 0x215F),
    (0x2184, 0x2184),
    (0x2189, 0x2189),
    (0x25CC, 0x25CC),
    (0x2C60, 0x2C7F),
    (0x2DE0, 0x2E52),
    (0xA640, 0xA69F),
    (0xA700, 0xA7BF),
    (0xA7C2, 0xA7CA),
    (0xA7F5, 0xA7FF),
    (0xA92E, 0xA92E),
    (0xAB30, 0xAB6B),
    (0xFB00, 0xFB06),
    (0xFE00, 0xFE00),
    (0xFE20, 0xFE2F),
    (0xFEFF, 0xFEFF),
    (0xFFFC, 0xFFFD),
    (0xFFFF, 0xFFFF),
)
# White space: what Python's `str.isspace` takes as such. Written out, and held to `isspace` by a
# test, so the browser can hold the same list.
WHITE_SPACE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0009, 0x000D),
    (0x001C, 0x0020),
    (0x0085, 0x0085),
    (0x00A0, 0x00A0),
    (0x1680, 0x1680),
    (0x2000, 0x200A),
    (0x2028, 0x2029),
    (0x202F, 0x202F),
    (0x205F, 0x205F),
    (0x3000, 0x3000),
)
# Variation selectors: marks that only choose between two looks of the character before them. A
# phone writes "©" as "©" plus one of these, asking for the emoji look. They have no shape of
# their own, so the PDF leaves them out, and after a character they are never named: nobody could
# see what to remove.
VARIATION_SELECTOR_RANGES: tuple[tuple[int, int], ...] = (
    (0x180B, 0x180D),
    (0x180F, 0x180F),
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)


def _within(ranges: tuple[tuple[int, int], ...], code: int) -> bool:
    index = bisect_right(ranges, (code, 0x110000)) - 1
    return index >= 0 and code <= ranges[index][1]


def as_shown(character: str) -> str:
    """A named character as a sentence shows it.

    Itself, unless nobody could see it. A variation selector, a control character (what badly
    decoded text is full of) and a format character have no shape, so they are shown by their
    number and the sentence that names them does not end in a hole.
    """
    code = ord(character)
    invisible = _within(VARIATION_SELECTOR_RANGES, code) or unicodedata.category(character) in (
        "Cc",
        "Cf",
    )
    return f"U+{code:04X} (invisible)" if invisible else character


def without_variation_selectors(text: str) -> str:
    """The text as the PDF draws it: every variation selector left out."""
    return "".join(
        character for character in text if not _within(VARIATION_SELECTOR_RANGES, ord(character))
    )


def unprintable_characters(text: str) -> tuple[str, ...]:
    """The characters of `text` the PDF cannot print: each one once, as they first appear.

    White space is never named: it draws nothing, whatever the font holds. Nor is a variation
    selector that follows a character, which it belongs to. One with no character before it is
    named: the sign was deleted and the mark stayed behind, and a text made only of such marks
    would look empty and print as nothing.
    """
    found: dict[str, None] = {}
    follows_a_character = False
    for character in text:
        code = ord(character)
        if _within(VARIATION_SELECTOR_RANGES, code):
            if not follows_a_character:
                found[character] = None
        elif _within(WHITE_SPACE_RANGES, code):
            follows_a_character = False
        else:
            follows_a_character = True
            if not _within(PRINTABLE_RANGES, code):
                found[character] = None
    return tuple(found)


def in_capitals(text: str) -> str:
    """The text as a resume prints a name, an employer and the headline: in capitals.

    A letter whose capital the PDF's fonts lack stays as it is, so that it is printed and not
    left out. The Word file uses the same text, so the two files of one resume read the same.
    Which letters have a capital is Python's own list and grows from one version to the next.
    """
    return "".join(
        character if unprintable_characters(capital) else capital
        for character in text
        for capital in (character.upper(),)
    )


def unprintable_by_location(
    value: object, location: Location = ()
) -> Iterator[tuple[Location, tuple[str, ...]]]:
    """Each text inside a dumped contract that the PDF cannot print in full, with where it is.

    `value` is what `model_dump(mode="json")` gives: dictionaries, lists and plain values. Walking
    it, and not a list of field names, means a field added to a contract later is checked too.
    """
    if isinstance(value, str):
        characters = unprintable_characters(value)
        if characters:
            yield location, characters
    elif isinstance(value, dict):
        for name, item in cast("dict[str, object]", value).items():
            yield from unprintable_by_location(item, (*location, name))
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            yield from unprintable_by_location(item, (*location, index))
