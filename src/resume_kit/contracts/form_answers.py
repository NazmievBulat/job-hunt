"""The answers for application forms, kept on the Profile page.

the app fills them in on a company's form when it asks the same. Two kinds: the ready answers, a
fixed set of questions most forms ask (work authorization, salary, notice period...), and the
answers saved from forms, each a question in the form's own words with the answer the person
gave it there.

Desired locations and the work setting are the same values the Jobs page searches by
(`JobAnswers.place` and `JobAnswers.remote`): one value, shown and changed in both places.

Answers to voluntary self-identification questions (gender, race, veteran status, disability)
are never stored: no field holds them, and the extension sends none.
"""

import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from resume_kit.contracts.base import StorableStr, StrictModel, TrimmedNonblankStr
from resume_kit.contracts.jobs import MAX_PLACE_CHARACTERS

SELF_IDENTIFICATION_REFUSAL = (
    "the app never keeps answers about gender, race, ethnicity, veteran status or disability."
)

MAX_SHORT_ANSWER_CHARACTERS = 100
MAX_QUESTION_CHARACTERS = 500
MAX_ANSWER_CHARACTERS = 5_000
MAX_COMPANY_CHARACTERS = 200
# Saved answers an account keeps. A form that brings more adds none of its new questions; the
# ones it changes are still changed.
MAX_SAVED_ANSWERS = 300

# The words of a voluntary self-identification question, as the extension finds them
# (`apps/extension/src/fill/answers.ts`, `SELF_IDENTIFICATION`). A question sent from a form with
# them is not kept, whichever version of the extension sent it.
_SELF_IDENTIFICATION = re.compile(
    r"\b(gender|sex|pronouns?|race|racial|ethnicity|ethnic|hispanic|latin[oax]|veteran"
    r"|disabilit(y|ies)|disabled|sexual orientation|transgender|lgbt|self[- ]?identif\w*"
    r"|equal (employment )?opportunity|eeo)\b",
    re.IGNORECASE,
)


def asks_self_identification(question: str) -> bool:
    return _SELF_IDENTIFICATION.search(unicodedata.normalize("NFKC", question)) is not None


YesNo = Literal["yes", "no"]
WorkSetting = Literal["remote", "hybrid", "office"]

ShortAnswer = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_SHORT_ANSWER_CHARACTERS)
]
Locations = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_PLACE_CHARACTERS)
]
Question = Annotated[TrimmedNonblankStr, Field(max_length=MAX_QUESTION_CHARACTERS)]
Answer = Annotated[TrimmedNonblankStr, Field(max_length=MAX_ANSWER_CHARACTERS)]
Company = Annotated[StorableStr, Field(max_length=MAX_COMPANY_CHARACTERS)]


class ReadyAnswers(StrictModel):
    """The fixed questions, in the order the Profile page shows them. None or empty: not set."""

    authorized: YesNo | None = None
    sponsorship: YesNo | None = None
    salary: ShortAnswer = ""
    notice: ShortAnswer = ""
    relocate: YesNo | None = None
    setting: WorkSetting | None = None
    locations: Locations = ""


class ReadyChange(StrictModel):
    """The ready answers a sent form gave: only those not None change."""

    authorized: YesNo | None = None
    sponsorship: YesNo | None = None
    salary: ShortAnswer | None = None
    notice: ShortAnswer | None = None
    relocate: YesNo | None = None
    setting: WorkSetting | None = None
    locations: Locations | None = None


class SavedAnswer(StrictModel):
    id: int
    question: str
    answer: str
    # The company of the form it was saved from; None for one the person added on the page.
    company: str | None
    saved_at: datetime


class FormAnswers(StrictModel):
    ready: ReadyAnswers
    # The newest first.
    saved: list[SavedAnswer]


class SavedAnswerEdit(StrictModel):
    """A saved answer as the page sends it: with its id when it was saved before, without one
    when the person added it."""

    id: int | None = None
    question: Question
    answer: Answer


class ReadyAnswersEdit(ReadyAnswers):
    """A Profile edit: omitted values stay unchanged. Factories omit OpenAPI defaults so
    generated clients can omit these strings too."""

    salary: ShortAnswer = Field(default_factory=str)
    notice: ShortAnswer = Field(default_factory=str)
    locations: Locations = Field(default_factory=str)


class FormAnswersSave(StrictModel):
    """Only the ready fields and saved answers the person changed, plus explicit deletions.

    Omitted fields and rows stay as they are, including answers a form saved in another tab.
    """

    ready: ReadyAnswersEdit
    saved: list[SavedAnswerEdit] = Field(max_length=MAX_SAVED_ANSWERS)
    deleted: list[int] = Field(default=[], max_length=MAX_SAVED_ANSWERS)


class GivenAnswer(StrictModel):
    question: Question
    answer: Answer


class FormAnswersFromForm(StrictModel):
    """What a person gave on a form that was sent: ready answers the app recognised, and the other
    questions in the form's words. The same question again replaces its answer."""

    company: Company
    ready: ReadyChange = ReadyChange()
    saved: list[GivenAnswer] = Field(default=[], max_length=MAX_SAVED_ANSWERS)
