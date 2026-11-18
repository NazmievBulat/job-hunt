"""Profile and pool contracts: a person's reusable career facts."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal, LiteralString, Self, cast

from pydantic import (
    AfterValidator,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from resume_kit.contracts.base import (
    END_BEFORE_START,
    CareerDate,
    NameStr,
    StrictModel,
    TrimmedNonblankStr,
    dated_after_the_month_of,
    dates_in_order,
    single_spaced,
    stored_text,
)
from resume_kit.contracts.resume import Person, WebAddress, with_single_spaces

MAX_BULLET_CHARACTERS = 500
MAX_LINE_CHARACTERS = 200  # names, organizations, titles, locations
MAX_SHORT_CHARACTERS = 100  # skills, categories, tags, link labels
MAX_PHONE_CHARACTERS = 50
MAX_SUMMARY_CHARACTERS = 1000
# A summary as a file gave it, or as a row saved before the limit above was lowered from this
# number, holds it. It is read and shown; saving it means shortening it first.
MAX_LOOSE_SUMMARY_CHARACTERS = 5000
MAX_LINKS = 20
MAX_BULLET_TAGS = 30
# How many pages of an uploaded resume that are only pictures are read: each takes seconds.
MAX_PICTURE_PAGES_READ = 10
# Named so that the API can tell this refusal apart and answer a person in plain words.
START_MISSING = "A start date is required for a job or project."
EXPIRY_BEFORE_DATE = "A certification cannot expire before its date."
DATED_LATER = "A certification cannot be dated after the current month."
# How far the clock of a place may be ahead of UTC (Kiribati): a month has begun for someone
# once it has begun there.
FURTHEST_AHEAD_OF_UTC = timedelta(hours=14)
# What a project of one's own is saved under: a resume prints an organization for every project,
# and the fact checks match the one it prints against the saved one.
PERSONAL_PROJECT = "Personal project"
LineText = Annotated[TrimmedNonblankStr, Field(max_length=MAX_LINE_CHARACTERS)]
# A line kept with one space for each run of whitespace, and counted as it is kept.
SingleSpacedLine = Annotated[
    TrimmedNonblankStr, AfterValidator(single_spaced), Field(max_length=MAX_LINE_CHARACTERS)
]
ShortText = Annotated[TrimmedNonblankStr, Field(max_length=MAX_SHORT_CHARACTERS)]
SummaryText = Annotated[TrimmedNonblankStr, Field(max_length=MAX_SUMMARY_CHARACTERS)]
LooseSummaryText = Annotated[TrimmedNonblankStr, Field(max_length=MAX_LOOSE_SUMMARY_CHARACTERS)]
PicturePage = Annotated[int, Field(ge=1, le=2_147_483_647)]


class FromPictures(StrictModel):
    """Where an item read from an uploaded file may have come from, when not from its text.

    A page that is only pictures is read by a recognition program, which misreads. An item of a
    reading whose words are not in the pages read exactly names the picture pages it may come
    from, and the page marks it to be checked until the owner edits it or says it looks right,
    before saving and after.
    Empty for everything else.
    """

    # Left out when empty, so an item read exactly is written as it was before the marks: the
    # older copy reads a reading that waits with a contract that refuses a field it does not know.
    # At most what the PostgreSQL integer column holds: Undo sends the pages back (ticket 365).
    picture_pages: list[PicturePage] = Field(
        default_factory=list[int],
        max_length=MAX_PICTURE_PAGES_READ,
        exclude_if=lambda pages: not pages,
    )

    def checked(self) -> Self:
        """The item marked no longer: the owner edited it or said it looks right."""
        return self.model_copy(update={"picture_pages": []})


class _NamedPictures(FromPictures):
    """Undo restores every flag kept when old duplicate names were merged.

    A saved name can now carry the union of flags from several imports. The ten-page
    reading limit still applies to each import, through `FromPictures`.
    """

    picture_pages: list[PicturePage] = Field(
        default_factory=list[int], exclude_if=lambda pages: not pages
    )


class Profile(StrictModel):
    """The account profile: who the person is. A person has one, whatever their career profiles.

    There is no summary here: a summary describes the career, so it belongs to the career profile
    (see `Pool.summary`).
    """

    person: Person

    @model_validator(mode="wrap")
    @classmethod
    def require_reasonable_sizes(
        cls, data: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        # Person comes from the resume contract, which has no limits, so they are enforced here,
        # each at the field it is about, as a limit on the field itself would be. They are told
        # together with the person's other refusals, so nothing is left for a second round.
        try:
            profile = handler(data)
        except ValidationError as refused:
            sizes = _size_failures(_raw_person(data))
            if not sizes:
                raise
            raise ValidationError.from_exception_data(
                cls.__name__, told_with(refused, sizes)
            ) from None
        # A run of spaces in a one-line field is kept as one: Word would print every space.
        profile.person = with_single_spaces(profile.person)
        failures = _size_failures(profile.person.model_dump())
        if failures:
            raise ValidationError.from_exception_data(cls.__name__, failures)
        return profile


def told_with(refused: ValidationError, more: list[InitErrorDetails]) -> list[InitErrorDetails]:
    """The refusals of `refused`, and those of `more` at a place none of them is at."""
    told = refused.errors()
    places = {issue["loc"] for issue in told}
    # Each refusal as it was told: its own kind, words and context, which a refusal remade
    # from its kind alone could not always carry.
    return [
        InitErrorDetails(
            # A kind and words pydantic gave, which it reads as they are.
            type=PydanticCustomError(
                cast(LiteralString, issue["type"]),
                cast(LiteralString, issue["msg"]),
                issue.get("ctx"),
            ),
            loc=issue["loc"],
            input=issue["input"],
        )
        for issue in told
    ] + [failure for failure in more if failure.get("loc") not in places]


def _raw_person(data: object) -> dict[str, object]:
    """The person of a profile as it was sent, before its own checks."""
    if isinstance(data, Profile):
        return data.person.model_dump()
    sent = cast(dict[str, object], data) if isinstance(data, dict) else {}
    person = sent.get("person")
    return cast(dict[str, object], person) if isinstance(person, dict) else {}


def _size_failures(person: dict[str, object]) -> list[InitErrorDetails]:
    """The person's values that are longer than the profile keeps, each at its own place."""

    def length(value: object) -> int:
        return len(stored_text(value)) if isinstance(value, str) else 0

    def one_line(value: object) -> object:
        # Counted as it is kept, with one space for each run (`with_single_spaces`).
        return single_spaced(value) if isinstance(value, str) else value

    limits: list[tuple[tuple[str | int, ...], object, int]] = [
        (("person", "name"), one_line(person.get("name")), MAX_LINE_CHARACTERS),
        (("person", "phone"), one_line(person.get("phone")), MAX_PHONE_CHARACTERS),
        (("person", "location"), one_line(person.get("location")), MAX_LINE_CHARACTERS),
    ]
    links = person.get("links")
    links = cast(list[object], links) if isinstance(links, list) else []
    limits += [
        (("person", "links", index, "label"), cast(dict[str, object], link).get("label"),
         MAX_SHORT_CHARACTERS)
        for index, link in enumerate(links)
        if isinstance(link, dict)
    ]  # fmt: skip
    failures = [
        InitErrorDetails(type="string_too_long", loc=loc, input=value, ctx={"max_length": limit})
        for loc, value, limit in limits
        if length(value) > limit
    ]
    if len(links) > MAX_LINKS:
        failures.append(
            InitErrorDetails(
                type="too_long",
                loc=("person", "links"),
                input=None,
                ctx={"field_type": "List", "max_length": MAX_LINKS, "actual_length": len(links)},
            )
        )
    return failures


class LooseRole(StrictModel):
    """A role as a file gave it, or as a row saved before a start date was required holds it.

    Either date may be missing. A date is a year, or a year and its month. An empty end date
    means the role is current.
    """

    kind: Literal["job", "project", "education"]
    organization: SingleSpacedLine
    title: SingleSpacedLine
    start_date: CareerDate | None = None
    end_date: CareerDate | None = None
    location: LineText | None = None
    # How the job was worked, in the resume's or the person's words: "Part-time, 20 hours per
    # week", "Fixed-term contract" (ticket 471). Left out of the JSON while there is none, so a
    # role without one is stored and sent as roles were before; one that has it is kept beside
    # what an older copy reads (`persistence/newer_fields.py`), and a writer is not sent it.
    employment_type: LineText | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def name_a_personal_project(cls, data: object) -> object:
        """A project given no organization is the person's own: it is saved as one."""
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        if fields.get("kind") != "project":
            return fields
        organization = fields.get("organization")
        # Empty as the field keeps it: nothing is left once what shows nothing leaves the ends.
        if organization is None or (
            isinstance(organization, str) and not stored_text(organization)
        ):
            return {**fields, "organization": PERSONAL_PROJECT}
        return fields

    @model_validator(mode="after")
    def require_date_order(self) -> Self:
        if self.start_date is None or self.end_date is None:
            return self
        if not dates_in_order(self.start_date, self.end_date):
            raise ValueError(END_BEFORE_START)
        return self


class RoleInput(LooseRole):
    """A role that may be saved: precise enough to write a resume from.

    A job or project says when it started. Education may give only the date it ended, or no
    dates. A date may be a year alone: a resume prints it as it was saved.
    """

    @model_validator(mode="after")
    def require_start_of_work(self) -> Self:
        if self.kind != "education" and self.start_date is None:
            raise ValueError(START_MISSING)
        return self


class Role(LooseRole):
    id: str


class BulletInput(StrictModel):
    text: Annotated[TrimmedNonblankStr, Field(max_length=MAX_BULLET_CHARACTERS)]
    skills: list[ShortText] = Field(default_factory=list[str], max_length=MAX_BULLET_TAGS)
    source: Literal["imported", "manual"] = "manual"


class Bullet(BulletInput):
    id: str
    role_id: str


class SkillInput(StrictModel):
    name: Annotated[NameStr, Field(max_length=MAX_SHORT_CHARACTERS)]
    category: ShortText | None = None


class Skill(SkillInput):
    id: str


class NewSkill(SkillInput, _NamedPictures):
    """A skill to save. Undo saves one just removed as it was: one still to be checked against the
    picture pages it may come from keeps them (ticket 365). A skill typed names none."""


class HeadlineTitleInput(StrictModel):
    text: Annotated[NameStr, AfterValidator(single_spaced), Field(max_length=MAX_LINE_CHARACTERS)]


class HeadlineTitle(HeadlineTitleInput):
    id: str


class NewTitle(HeadlineTitleInput, _NamedPictures):
    """A headline title to save, with the picture pages it is still to be checked against, as
    `NewSkill`."""


class CertificationInput(StrictModel):
    """A certification the person holds. Only the name is required: who issued it and when are
    theirs to give or leave out."""

    name: SingleSpacedLine
    issuer: LineText | None = None
    # When it was issued: the year, with the month when the person gives it ("2023", "2023-03").
    date: CareerDate | None = None
    expiry_date: CareerDate | None = None
    # The number the issuer gave it and the page where an employer can check it. The writer is
    # not sent them; code prints them after the certification's line (`certification_details`).
    credential_id: LineText | None = None
    verification_url: WebAddress | None = None


class CertificationEntry(CertificationInput):
    """A certification as a person adds or changes one: it does not expire before its date.

    Compared as a role's dates are (`dates_in_order`). One stored before this rule, or read from
    a resume, is a `CertificationInput` and still loads, so it can be shown and put right.
    """

    @model_validator(mode="after")
    def require_expiry_after_date(self) -> Self:
        if self.date is None or self.expiry_date is None:
            return self
        if not dates_in_order(self.date, self.expiry_date):
            raise ValueError(EXPIRY_BEFORE_DATE)
        return self


def require_held(certification: CertificationInput, now: datetime) -> None:
    """Refuse, at its date, a certification dated after the current month: it is not held yet.

    Asked where a person adds or changes one by hand. One stored before this rule, or read from
    a resume, is not checked here.
    """
    date = certification.date
    if date is None or not dated_after_the_month_of(date, now + FURTHEST_AHEAD_OF_UTC):
        return
    raise ValidationError.from_exception_data(
        type(certification).__name__,
        [
            InitErrorDetails(
                type=PydanticCustomError(
                    "value_error", "Value error, {error}", {"error": DATED_LATER}
                ),
                loc=("date",),
                input=date,
            )
        ],
    )


class Certification(CertificationInput):
    id: str


class RoleWithBullets(Role):
    bullets: list[Bullet]


def _month_of(date: str) -> tuple[int, int]:
    """A date as (year, month); a year alone counts as before every month of its year."""
    year, _, month = date.partition("-")
    return int(year), int(month or 0)


def listed_newest_first[R: LooseRole](roles: Sequence[R]) -> list[R]:
    """The roles as a resume lists them, for the Career profile (ticket 555).

    A current one (a start and no end) comes first, then by end date and then start date,
    newest first; one without any date comes last. Equal dates keep the order they were added.
    Only what the page shows: the writer is still given the roles in the order they were added.
    """

    def newness(role: R) -> tuple[int, tuple[int, int], tuple[int, int]]:
        start = _month_of(role.start_date) if role.start_date is not None else (0, 0)
        if role.end_date is None:
            return (2 if role.start_date is not None else 0), (0, 0), start
        return 1, _month_of(role.end_date), start

    # `sorted` keeps equal ones in their order, reversed too.
    return sorted(roles, key=newness, reverse=True)


class SummaryInput(StrictModel):
    text: SummaryText


class Pool(StrictModel):
    roles: list[RoleWithBullets]
    skills: list[Skill]
    titles: list[HeadlineTitle]
    certifications: list[Certification]
    # The person's own description of their career: shown on the Career profile page and sent
    # to the writer. Read loosely, so one saved before the limit was lowered still loads; it is
    # saved through `SummaryInput`, which holds the limit.
    summary: LooseSummaryText | None = None
    # The saved items read from a picture page that wait to be checked, by id, with the pages
    # they may come from (`FromPictures`). Kept apart from the items, which a resume run copies
    # and the older copy reads.
    # Left out when empty, so a career profile with nothing to check is answered as before.
    picture_pages: dict[str, list[int]] = Field(
        default_factory=dict[str, list[int]], exclude_if=lambda marked: not marked
    )
