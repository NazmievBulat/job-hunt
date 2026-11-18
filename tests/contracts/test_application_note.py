"""An application's note: what a person types beside it on the Applied page."""

import pytest
from pydantic import ValidationError

from resume_kit.contracts.base import UNSTORABLE_CHARACTER
from resume_kit.contracts.jobs import NoteChange


@pytest.mark.parametrize("character", ["\x00", "\x07", "\ud800"])
def test_a_note_holding_a_character_the_database_cannot_hold_is_refused(character: str) -> None:
    with pytest.raises(ValidationError, match=UNSTORABLE_CHARACTER):
        NoteChange(note=f"call {character} back", based_on="")


@pytest.mark.parametrize("note", ["", "Call back\nFriday", "Ask for\tAnn\r\n"])
def test_an_empty_note_and_one_with_line_breaks_and_tabs_are_kept(note: str) -> None:
    assert NoteChange(note=note, based_on="").note == note.strip()
