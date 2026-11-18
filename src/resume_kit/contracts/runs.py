"""Validated states and results for asynchronous resume-generation runs."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from resume_kit.contracts.base import StrictModel, TrimmedNonblankStr
from resume_kit.contracts.generation import ResumeAlignmentLevel, ResumeLengthInPages
from resume_kit.contracts.resume import Education, Resume
from resume_kit.contracts.resume_edit_text import clean_resume_edit

RunStage = Literal["normalizing", "generating", "rendering"]
RunStatus = Literal["queued", "running", "succeeded", "failed"]

# How many resumes one account may have on their way (queued or being written) at once, with the
# flag `resume_queue` on. Without it, one.
RESUMES_AT_ONCE = 3
PublicErrorCode = Literal[
    "GENERATION_FAILED", "RENDER_FAILED", "VALIDATION_FAILED", "INTERNAL_ERROR"
]


class PublicError(StrictModel):
    """Public failure details; callers must supply an already-sanitized message."""

    code: PublicErrorCode
    message: TrimmedNonblankStr


class ResumeFiles(StrictModel):
    """At least one generated-file download address; relative API paths are allowed."""

    pdf_url: TrimmedNonblankStr | None = None
    docx_url: TrimmedNonblankStr | None = None

    @model_validator(mode="after")
    def require_download_link(self) -> Self:
        """Reject results that provide neither PDF nor DOCX downloads."""
        if self.pdf_url is None and self.docx_url is None:
            raise ValueError("At least one PDF or DOCX download link is required.")
        return self


class AddedLines(StrictModel):
    """What the resume as generated says that the profile its run was written from does not.

    Lines are named by their text, not their place: a line changed by hand no longer reads the
    same, so it is no longer marked, and a line removed by hand marks nothing. `job_titles` has
    one entry for each job of the generated resume, in its order: the title saved in the profile
    when the resume gives another one, otherwise none. `bullets` has one list for each job, in
    the same order. A job title is known by its place: by hand it can only be put back to the
    saved one, which is then no longer marked.
    """

    headline: TrimmedNonblankStr | None = None
    summary: TrimmedNonblankStr | None = None
    job_titles: list[TrimmedNonblankStr | None] = Field(default_factory=list[str | None])
    bullets: list[list[str]] = Field(default_factory=list[list[str]])
    # Locked degree facts identify its marked bullets even after other degrees are left off.
    education_bullets: list[Education] = Field(default_factory=list[Education])
    skills: list[str] = Field(default_factory=list[str])


class ResumeRunResult(StrictModel):
    """The resume as it stands now and its downloadable files.

    `generated_resume` is absent until the person changes the resume by hand. From then on it
    holds what was generated, and `resume` holds their version; the files are made from `resume`.
    Its person is the one last copied from the profile: as it was when the resume was written,
    or as it was when the person took newer details from it (`ResumeEdit.person_from_profile`).
    `alignment_level` is the level the resume was written at; runs saved before it was recorded
    have none. `length_in_pages` is the length the person asked for, if they asked for one.
    `pdf_page_count` is how many pages the PDF has: a result with no PDF has none, and neither
    has one saved before pages were counted. The Word file is not counted, because
    its pages depend on the program that opens it. `added` is what the generated resume says
    that the profile does not; a run saved before it was found has none. It is about what was
    generated, so a hand edit keeps it, and the files never show it.
    """

    resume: Resume
    files: ResumeFiles
    generated_resume: Resume | None = None
    alignment_level: ResumeAlignmentLevel | None = None
    length_in_pages: ResumeLengthInPages | None = None
    pdf_page_count: PositiveInt | None = None
    added: AddedLines | None = None

    @model_validator(mode="after")
    def count_pages_only_of_a_pdf(self) -> Self:
        if self.pdf_page_count is not None and self.files.pdf_url is None:
            raise ValueError("A result with no PDF cannot have a page count.")
        return self


def _to_utc(value: datetime) -> datetime:
    """Preserve the instant while converting an already-aware timestamp to UTC."""
    return value.astimezone(UTC)


RunTimestamp = Annotated[AwareDatetime, AfterValidator(_to_utc)]

# Whatever stamps or compares run times asks a Clock, so a test can move the date.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The real clock."""
    return datetime.now(UTC)


def back_from(now: datetime, kept_for: timedelta) -> datetime:
    """The time before which something is old; the earliest time there is when that is before it.

    A keeping time written as a very large number of days means "never delete". Counting that
    far back is not possible, and must not fail the sweep: nothing is that old.
    """
    try:
        return now - kept_for
    except OverflowError:
        return datetime.min.replace(tzinfo=now.tzinfo)


class ResumeRunBase(StrictModel):
    """Run identity and caller-supplied aware timestamps normalized to UTC."""

    run_id: UUID
    created_at: RunTimestamp
    updated_at: RunTimestamp

    @model_validator(mode="after")
    def require_timestamp_order(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at.")
        return self


class QueuedResumeRun(ResumeRunBase):
    """A run waiting to start."""

    status: Literal["queued"]


class RunningResumeRun(ResumeRunBase):
    """A run in progress, optionally reporting its current step."""

    status: Literal["running"]
    stage: RunStage | None = None


class SucceededResumeRun(ResumeRunBase):
    """A completed run with its required result."""

    status: Literal["succeeded"]
    result: ResumeRunResult


class FailedResumeRun(ResumeRunBase):
    """A failed run with its required public error."""

    status: Literal["failed"]
    error: PublicError


def _time_sent_as_text(value: object) -> object:
    """JSON has no type for a time, so a request carries one as text; nothing else is read."""
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError as not_a_time:
        raise ValueError("Not a time.") from not_a_time


SentRunTimestamp = Annotated[RunTimestamp, BeforeValidator(_time_sent_as_text)]
# A name a page gives itself each time it is opened, so the server can tell its saves from
# another tab's. Random letters and digits; it says nothing about the person.
TabId = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
# The number a page gives each of its saves: 1 for the first, one more for each after it. No
# higher than the largest whole number a browser counts exactly.
SaveSequence = Annotated[int, Field(ge=1, le=2**53 - 1)]


class ResumeEdit(StrictModel):
    """A resume changed by hand, with what the server needs to tell two open tabs apart.

    `started_from` is the run's `updated_at` as the sending tab last read it. A save that started
    from an older version than the stored one is refused, so one tab cannot put back the text
    another tab has replaced.

    `sequence` orders the saves of one tab. Two of them can be on their way at once and arrive
    in either order, and the one with the lower number is never stored over the other.

    `person_from_profile` is "Use them": the name and contact details are put on the resume as
    the account profile holds them now, and the ones sent are not read. The server copies them,
    as it did when the resume was written, so no other details can reach the resume this way.
    """

    resume: Annotated[Resume, BeforeValidator(clean_resume_edit)]
    started_from: SentRunTimestamp
    tab: TabId
    sequence: SaveSequence
    person_from_profile: bool = False


ResumeRun = Annotated[
    QueuedResumeRun | RunningResumeRun | SucceededResumeRun | FailedResumeRun,
    Field(discriminator="status"),
]
ResumeRunAdapter: TypeAdapter[ResumeRun] = TypeAdapter(ResumeRun)
