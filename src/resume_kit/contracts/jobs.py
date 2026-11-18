"""The Jobs page: open jobs from companies' own hiring feeds, ranked for one person.

The postings themselves are read by the feed reader into tables shared by
every account (`feed_postings`, `feed_companies`, `company_lookups`). What a person does with
them is theirs: their three answers, their marks on a job, the companies they follow, and the
scores the background ranking stored for them. See docs/jobs.md.
"""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from resume_kit.contracts.base import StorableStr, StrictModel, TrimmedNonblankStr

# The limits.
JOBS_PER_PAGE = 50
MAX_WANTED_TITLES = 5
MAX_FOLLOWED_COMPANIES = 20
MAX_PLACE_CHARACTERS = 200
MAX_WANTED_TITLE_CHARACTERS = 120
MAX_COMPANY_QUERY_CHARACTERS = 300

# "must": a job that does not match the answer is not shown. "nice": a job that matches it is
# shown higher, and the others are still shown.
Need = Literal["must", "nice"]
# The level choices: several may be picked.
JobLevel = Literal["junior", "mid", "senior", "lead", "principal"]
JOB_LEVELS: tuple[JobLevel, ...] = ("junior", "mid", "senior", "lead", "principal")

# "hidden" is Not relevant on the page; the marks of Hide count as Not
# relevant without a reason.
Mark = Literal["saved", "hidden", "applied"]
# Why a job is not relevant, as the person tapped it: what the list learns from it.
NotRelevantReason = Literal["role", "place", "level", "company", "other"]
JobsView = Literal["for_you", "saved"]
# What the feed reader found for a company a person typed.
LookupStatus = Literal["waiting", "found", "not_found", "unreadable", "refused"]

WantedTitle = Annotated[
    TrimmedNonblankStr, StringConstraints(max_length=MAX_WANTED_TITLE_CHARACTERS)
]
Place = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_PLACE_CHARACTERS)]


class JobAnswers(StrictModel):
    """The three questions a person answers once and may change at any time."""

    place: Place = Field(description="A city or country; empty when only remote is wanted.")
    remote: bool
    place_need: Need
    levels: list[JobLevel] = Field(min_length=1, max_length=len(JOB_LEVELS))
    level_need: Need
    titles: list[WantedTitle] = Field(min_length=1, max_length=MAX_WANTED_TITLES)
    titles_need: Need

    @model_validator(mode="after")
    def _somewhere(self) -> Self:
        if not self.place and not self.remote:
            raise ValueError("Say a city or country, or remote.")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("A level is given twice.")
        if len({title.casefold() for title in self.titles}) != len(self.titles):
            raise ValueError("A title is given twice.")
        return self


class JobAnswersState(StrictModel):
    """The answers, or none yet, with the titles of the Career profile to start the third
    question from."""

    answers: JobAnswers | None
    profile_titles: list[str]


class ListedJob(StrictModel):
    id: int
    title: str
    company: str
    location: str
    remote: bool
    # The posting's own page on the company's site.
    url: str
    # The titles and skills of the person's profile that the posting names.
    fits: list[str]
    # First seen after the person's visit before this one.
    new: bool
    saved: bool


class JobPageRequest(StrictModel):
    """Continue a view without repeating any received posting, even after a re-ranking."""

    view: JobsView
    seen: list[int]


class JobList(StrictModel):
    jobs: list[ListedJob]
    # How many jobs the view holds in all; `jobs` is one page of them.
    total: int
    # More jobs follow: the page asks for the next page.
    more: bool
    # How many jobs are saved and applied, for the names of the other views.
    saved_count: int
    applied_count: int
    # The person answered the three questions.
    answered: bool
    # The background ranking has not yet scored the open jobs for this person.
    ranking: bool


class JobPosting(StrictModel):
    """What "Write my resume" hands to the resume builder, and Apply to the extension."""

    id: int
    title: str
    company: str
    url: str
    text: str
    # The newest resume written for this job that did not fail, while it is kept: Apply attaches
    # it instead of writing another.
    run_id: UUID | None = None


class AppliedJob(StrictModel):
    id: int
    title: str
    company: str
    url: str
    applied_at: datetime
    # The resume written for it, when one was linked.
    run_id: UUID | None


class AppliedList(StrictModel):
    jobs: list[AppliedJob]


class MarkRequest(StrictModel):
    # None takes back "saved", and "hidden" while Not relevant is on (Undo). "applied" is not
    # taken back.
    mark: Mark | None
    # Why the job is not relevant: only with "hidden", and only while Not relevant is on. Sent
    # again with another reason, it replaces the one before.
    reason: NotRelevantReason | None = None

    @model_validator(mode="after")
    def _reason_of_not_relevant(self) -> Self:
        if self.reason is not None and self.mark != "hidden":
            raise ValueError("A reason is given only for a job that is not relevant.")
        return self


class ResumeLink(StrictModel):
    run_id: Annotated[UUID, Field(strict=False)]


class FollowRequest(StrictModel):
    query: Annotated[TrimmedNonblankStr, StringConstraints(max_length=MAX_COMPANY_QUERY_CHARACTERS)]


class FollowedCompany(StrictModel):
    id: int
    # What the person typed, as typed.
    typed: str
    status: LookupStatus
    # The company and its hiring system, once found.
    company: str | None
    source: str | None


class FollowedCompanies(StrictModel):
    companies: list[FollowedCompany]
    limit: int


class JobsMenu(StrictModel):
    # The menu shows "New" beside Jobs until the person first opens it.
    new: bool


# --- The Applied page: every application a person made, through its stages.

MAX_APPLICATION_TEXT_CHARACTERS = 200
MAX_NOTE_CHARACTERS = 2000

# The stages an application goes through, in order, and the two it may end in.
ApplicationStage = Literal["applied", "replied", "interview", "offer", "rejected", "withdrawn"]
STAGES_IN_ORDER: tuple[ApplicationStage, ...] = ("applied", "replied", "interview", "offer")
END_STAGES: tuple[ApplicationStage, ...] = ("rejected", "withdrawn")
# Where an application was added: the Jobs page's Applied, a resume, or typed in.
ApplicationSource = Literal["jobs", "resume", "hand"]

ApplicationText = Annotated[
    TrimmedNonblankStr, StringConstraints(max_length=MAX_APPLICATION_TEXT_CHARACTERS)
]


class ApplicationStep(StrictModel):
    stage: ApplicationStage
    at: datetime


class Application(StrictModel):
    id: int
    title: str
    # Empty when it is not known: a resume written from a pasted posting names no company.
    company: str
    # The posting's own page, for one added from the Jobs page.
    url: str | None
    source: ApplicationSource
    # The resume written for it, while the resume is kept.
    run_id: UUID | None
    # Every stage it was put in, oldest first; the first is always "applied".
    steps: list[ApplicationStep] = Field(min_length=1)
    note: str
    # Identity of the latest removable step; null for the initial step or after Undo.
    undo_step_id: int | None = None


class ApplicationList(StrictModel):
    applications: list[Application]


class NewApplication(StrictModel):
    """An application made somewhere else, typed in by hand."""

    company: ApplicationText
    title: ApplicationText


class ApplicationDetailsChange(StrictModel):
    """Correct the displayed company and title; the linked resume and posting stay as they are."""

    company: Annotated[
        StorableStr,
        StringConstraints(strip_whitespace=True, max_length=MAX_APPLICATION_TEXT_CHARACTERS),
    ]
    title: ApplicationText


class ResumeApplication(StrictModel):
    """The resume a person applied with ("I applied with this resume")."""

    run_id: Annotated[UUID, Field(strict=False)]


class StageChange(StrictModel):
    stage: ApplicationStage


class StageUndo(StrictModel):
    based_on: Annotated[int, Field(gt=0)]


class NoteChange(StrictModel):
    # The saved note when editing began; required so an older tab cannot replace a newer note.
    based_on: StorableStr
    note: Annotated[
        StorableStr, StringConstraints(strip_whitespace=True, max_length=MAX_NOTE_CHARACTERS)
    ]


class NoteClash(StrictModel):
    detail: str
    code: Literal["ITEM_CHANGED_ELSEWHERE"] = "ITEM_CHANGED_ELSEWHERE"
    saved: Application


class StageClash(NoteClash):
    """The saved application when the stage to undo is no longer its latest change."""
