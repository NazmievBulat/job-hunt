"""A draft of one open question of an application form, written by a model.

Beside a form Apply opened, the panel lists the questions left for the person. One that asks why
this company or this role ("why"), or for a cover letter ("cover_letter"), can be drafted from the
person's career profile and the posting of the resume written for the job. The draft is put in
the form for the person to read and change; nothing here sends or saves it.
"""

from typing import Annotated, Literal

from pydantic import Field

from resume_kit.contracts.base import StrictModel, TrimmedNonblankStr

AnswerKind = Literal["why", "cover_letter"]

# The most characters a question's words are taken with: a form's label, with its hint.
MAX_QUESTION_CHARACTERS = 500
# The longest draft of each kind when the form sets no limit of its own.
LONGEST_DRAFT: dict[AnswerKind, int] = {"why": 1200, "cover_letter": 3000}
# A field that holds fewer characters than this takes no draft: no answer fits in it.
SHORTEST_LIMIT = 200


class AnswerRequest(StrictModel):
    """The question as the form words it, what kind it is, and the field's own limit, if any."""

    question: Annotated[TrimmedNonblankStr, Field(max_length=MAX_QUESTION_CHARACTERS)]
    kind: AnswerKind
    max_characters: Annotated[int, Field(ge=SHORTEST_LIMIT)] | None = None


class DraftAnswer(StrictModel):
    """The draft, as it goes into the form, and each name, number and year of it that neither
    the career profile nor the posting has, for the person to check."""

    text: str
    not_in_profile: list[str] = Field(default_factory=list[str])


# The longest letter printed as a file: a draft the person did not lengthen much.
MAX_LETTER_CHARACTERS = 6000


class LetterToPrint(StrictModel):
    """A cover letter to print as a PDF in the resume's look, for a form that asks for a file."""

    text: Annotated[TrimmedNonblankStr, Field(max_length=MAX_LETTER_CHARACTERS)]
