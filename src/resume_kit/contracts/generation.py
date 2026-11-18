"""Validated input and options for one resume-generation request."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from pydantic.main import IncEx

from resume_kit.contracts.base import NonblankStr, StrictModel
from resume_kit.contracts.masking import without_own_details
from resume_kit.contracts.pool import (
    Certification,
    HeadlineTitle,
    LooseSummaryText,
    Profile,
    RoleWithBullets,
    Skill,
)
from resume_kit.contracts.resume import Person


def _require_integer(value: object) -> int:
    """Reject booleans and floats before numeric literal equality can coerce them."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Alignment level must be an integer.")
    return value


ResumeAlignmentLevel = Annotated[Literal[0, 25, 50, 75, 100], BeforeValidator(_require_integer)]
# The level a resume is written at when nobody chose one: the only level whose prompt adds nothing
# the profile does not state, so a person who did not choose is never given another trade
#. The web page's default is the same (`DEFAULT_ALIGNMENT_LEVEL` in
# the web page's copy).
DEFAULT_ALIGNMENT_LEVEL: Literal[0] = 0


def _require_whole_pages(value: object) -> int:
    """Reject booleans and floats before numeric literal equality can coerce them."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("A resume length must be a whole number of pages.")
    return value


# The length a person asks for: one page or up to two. A request without one
# is written as before any length could be asked for, and its writer is told none.
ResumeLengthInPages = Annotated[Literal[1, 2], BeforeValidator(_require_whole_pages)]
ResumeGenerationFormat = Literal["pdf", "docx", "both"]


class CareerSnapshot(StrictModel):
    """What the candidate has done, copied from their career profile.

    The summary is the person's own words about their career. A resume's summary is still written
    per job: the writer reads the saved one, and the judges and the fact checks do not.
    """

    roles: list[RoleWithBullets]
    skills: list[Skill]
    titles: list[HeadlineTitle]
    # Optional because a run saved before certifications existed has no such key.
    certifications: list[Certification] = Field(default_factory=list[Certification])
    # Optional for the same reason, and because a person may have saved none. As loose as the
    # career profile reads it: an older, longer summary does not stop a run.
    summary: LooseSummaryText | None = None

    def saved_dates(self) -> frozenset[str]:
        """Every date the person saved: when each role began and ended, and each certification."""
        roles = (date for role in self.roles for date in (role.start_date, role.end_date))
        certifications = (certification.date for certification in self.certifications)
        return frozenset(date for date in (*roles, *certifications) if date is not None)


# What a model is not sent:
# - the person. A model writes no contact details (`ResumeBody` has no field for them), so it
#   has no use for them, and they are not handed to a model provider.
# - IDs, which a model can never cite, and where a bullet came from, which means nothing to it.
# - a certification's expiry, credential ID and verification link, which code adds to the
#   resume after the checks (`certification_details`).
# - a role's employment type: whether a resume may use it is not decided (ticket 471).
_BULLET_EXCLUDE: IncEx = {"id": True, "role_id": True, "source": True}
_CAREER_EXCLUDE: dict[str, IncEx | bool] = {
    "roles": {
        "__all__": {"id": True, "employment_type": True, "bullets": {"__all__": _BULLET_EXCLUDE}}
    },
    "skills": {"__all__": {"id": True}},
    "titles": {"__all__": {"id": True}},
    "certifications": {
        "__all__": {
            "id": True,
            "expiry_date": True,
            "credential_id": True,
            "verification_url": True,
        }
    },
}


def _left_out(career: CareerSnapshot, *, summary: bool) -> IncEx:
    """What is dropped from the JSON a model reads, on top of the person, IDs and bullet sources.

    - an empty list of certifications. The key is left out, so a candidate who has none is sent
      exactly as candidates were sent before certifications existed. A summary that was never
      saved is `None`, and unset fields are dropped anyway, so the same holds for it.
    - the summary itself, when `summary` is false.
    """
    career_exclude = dict(_CAREER_EXCLUDE)
    if not career.certifications:
        career_exclude["certifications"] = True
    if not summary:
        career_exclude["summary"] = True
    return {"person": True, "career": career_exclude}


class CandidateSnapshot(StrictModel):
    """A copy of the candidate taken when the run is created.

    Identity comes from the account profile and experience from the career profile. Embedding a copy
    instead of IDs keeps a saved run replayable after the pool is edited.
    """

    person: Person
    career: CareerSnapshot

    def for_the_writer(self) -> CandidateSnapshot:
        """This candidate with the person's own name, email, phone and links taken out of the
        summary and the bullets. The writer is not sent them: the code puts the person on the
        resume itself (`assemble_resume`). A bullet left with nothing is left out, and so is a
        summary."""
        roles = [
            role.model_copy(
                update={
                    "bullets": [
                        bullet.model_copy(update={"text": text})
                        for bullet in role.bullets
                        if (text := without_own_details(bullet.text, self.person))
                    ]
                }
            )
            for role in self.career.roles
        ]
        summary = self.career.summary
        if summary is not None:
            summary = without_own_details(summary, self.person) or None
        career = self.career.model_copy(update={"roles": roles, "summary": summary})
        return self.model_copy(update={"career": career})

    def prompt_json(self, *, indent: int | None = None) -> str:
        """The JSON the writer reads: the career only, without IDs or unset optional fields, and
        without the person's own details (`for_the_writer`)."""
        writer = self.for_the_writer()
        return writer.model_dump_json(
            exclude=_left_out(writer.career, summary=True), exclude_none=True, indent=indent
        )

    def evidence_json(self, *, indent: int | None = None) -> str:
        """The JSON a judge reads: the facts a resume is checked against.

        The same as `prompt_json` without the summary. The summary is the person's pitch, so a
        claim found only there is not supported by the profile.
        """
        return self.model_dump_json(
            exclude=_left_out(self.career, summary=False), exclude_none=True, indent=indent
        )

    @model_validator(mode="after")
    def keep_the_account_profile_size_limits(self) -> Self:
        # The limits live on the Profile wrapper, so the copy is checked against the same wrapper.
        Profile(person=self.person)
        return self


class JobDescription(StrictModel):
    """The job posting wrapped as JSON. ``jd`` is the posting text exactly as it was found."""

    jd: NonblankStr


class JobDescriptionFileText(StrictModel):
    """The text read from a posting that was uploaded as a file or given as pictures of it.

    `notes` holds a sentence, ready to show, for each thing that was not read and may have held
    words of the posting: a page that is a picture, pictures, form fields, a screenshot with no
    text. Empty when all was read.
    """

    text: str
    notes: list[str]


# The longest page title kept with a run; the builder cuts a longer one before sending it.
PAGE_TITLE_CHARACTERS = 300

# The title found on the job page the posting was taken from, by the browser extension or the
# Jobs page. It names the resume (`resume_name`) and is never sent to a model: the writer's
# request holds the candidate, the posting and the level only.
PageTitle = Annotated[str, Field(max_length=PAGE_TITLE_CHARACTERS)]


# The longest note a person may give with a resume, in each of its two boxes.
# The builder says so before Generate; the server refuses a longer one (`notes_too_long`).
NOTE_CHARACTERS = 2000
NOTES_TOO_LONG_SENTENCE = (
    f"A note is longer than {NOTE_CHARACTERS:,} characters. Shorten it and generate again."
)


def _blank_as_none(value: str | None) -> str | None:
    return None if value is None or value.strip() == "" else value


# A note as the person typed it, kept exactly; a box left empty or blank is no note.
NoteText = Annotated[str | None, AfterValidator(_blank_as_none)]


class ResumeNotes(StrictModel):
    """What the person told before asking for one resume, for that resume only.

    `facts` is what they did that the career profile does not hold; `guidance` is what the
    employer said matters, which is never taken as experience they have. They are kept with the
    run, never in the career profile, and no model is sent them yet: what the writer is told of
    them is a separate step behind the writer evaluation.
    """

    facts: NoteText = None
    guidance: NoteText = None

    def too_long(self) -> bool:
        return any(
            note is not None and len(note) > NOTE_CHARACTERS for note in (self.facts, self.guidance)
        )


def given_notes(notes: ResumeNotes | None) -> ResumeNotes | None:
    """The notes, or `None` when both boxes were empty: a run asked for with empty notes is the
    same request as one asked for with none."""
    if notes is None or (notes.facts is None and notes.guidance is None):
        return None
    return notes


class ResumeFromProfile(StrictModel):
    """What a person chooses for one resume. The server adds the candidate from what they saved."""

    job_description: JobDescription
    alignment_level: ResumeAlignmentLevel = DEFAULT_ALIGNMENT_LEVEL
    length_in_pages: ResumeLengthInPages | None = None
    format: ResumeGenerationFormat = "pdf"
    page_title: PageTitle | None = None
    # Taken only while the flag `resume_notes` is on for the account.
    notes: ResumeNotes | None = None


class ResumeGeneration(StrictModel):
    """Candidate and job inputs plus the production options required to generate a resume."""

    candidate: CandidateSnapshot
    job_description: JobDescription
    alignment_level: ResumeAlignmentLevel = DEFAULT_ALIGNMENT_LEVEL
    length_in_pages: ResumeLengthInPages | None = None
    format: ResumeGenerationFormat = "pdf"
    page_title: PageTitle | None = None
    # What the person told for this resume; `None` when nothing (`given_notes`).
    notes: ResumeNotes | None = None
