"""Canonical, validated resume domain contract."""

import re
from collections.abc import Callable, Collection
from typing import Annotated, Any, Self, cast
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import (
    AfterValidator,
    BeforeValidator,
    EmailStr,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import InitErrorDetails

from resume_kit.contracts.base import (
    END_BEFORE_START,
    CareerDate,
    StrictModel,
    TrimmedNonblankStr,
    dates_in_order,
    single_spaced,
    stored_text,
)
from resume_kit.contracts.dates_in_words import with_dates_in_words
from resume_kit.contracts.printable import as_shown, unprintable_by_location
from resume_kit.contracts.resume_language import ResumeLanguage

MAX_SKILL_ROW_WORDS = 17
MAX_SKILL_ROW_CHARACTERS = 100
MAX_SKILL_ROW_ITEMS = 10
MAX_BULLET_WORDS = 20
MAX_HEADLINE_WORDS = 15  # one line under the name: a job title and a few skills


def _count_words(text: str) -> int:
    return len(text.split())


def _require_experience_bullet(value: str) -> str:
    if _count_words(value) > MAX_BULLET_WORDS:
        raise ValueError(
            f"Experience bullets must contain no more than {MAX_BULLET_WORDS} words "
            "(approximately two lines)."
        )
    return value


def _require_date_order(start_date: str | None, end_date: str | None) -> None:
    if start_date is not None and end_date is not None and not dates_in_order(start_date, end_date):
        raise ValueError(END_BEFORE_START)


# The same date a saved career profile holds: a resume prints exactly what was stored.
ResumeDate = CareerDate
SkillItem = TrimmedNonblankStr
ExperienceBullet = Annotated[TrimmedNonblankStr, AfterValidator(_require_experience_bullet)]


# The longest address a browser keeps; anything longer is left to be refused as it is.
_LONGEST_ADDRESS = 2083
_NOT_IN_A_HOST = frozenset(" \t\r\n\f\v@")


def _is_without_scheme(url: str) -> bool:
    """Whether `url` is an address written without its scheme, as people and resumes write
    "linkedin.com/in/name": a host with a dot inside it, then nothing, a port or a path.
    "mailto:…", "maria@example.com" and a lone word are not. Read in one pass, so a long text
    costs no more than its length."""
    if len(url) > _LONGEST_ADDRESS:
        return False
    end = next((i for i, character in enumerate(url) if character in ":/"), len(url))
    host = url[:end]
    if any(character in _NOT_IN_A_HOST for character in host):
        return False
    return "." in host[1:-1]


def _with_scheme(url: object) -> object:
    """An address written without its scheme, with "https://" in front. Anything else is left as
    it is, to be accepted or refused as written."""
    if isinstance(url, str) and _is_without_scheme(url.strip()):
        return "https://" + url.strip()
    return url


# A web address as a person types it: one written without its scheme gets "https://".
WebAddress = Annotated[HttpUrl, BeforeValidator(_with_scheme)]


class Link(StrictModel):
    url: WebAddress
    label: TrimmedNonblankStr


_LINK_URL: TypeAdapter[HttpUrl] = TypeAdapter(WebAddress)
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Query parameters that only record where a visit came from: the page is the same without them.
# Any parameter whose name begins with "utm_" is one too. Parameters a site may read to choose
# what it shows ("ref" of GitHub, "v" of YouTube) are not in the list.
_TRACKING_PARAMETERS = frozenset(
    {
        "trk", "trkinfo", "trackingid", "refid", "lipi", "licu", "midtoken", "midsig",
        "originalsubdomain", "li_fat_id", "fbclid", "gclid", "dclid", "gbraid", "wbraid",
        "msclkid", "yclid", "twclid", "ttclid", "igshid", "igsh", "mc_cid", "mc_eid", "mkt_tok",
        "_ga", "_gl", "ref_src", "ref_url",
    }
)  # fmt: skip
# A LinkedIn profile's page is "/in/<name>": whatever follows "?" or "#" shows the same profile.
_LINKEDIN_PROFILE = re.compile(r"/in/[^/]+")


def _linkedin_profile(parsed: HttpUrl) -> str | None:
    """The path of a LinkedIn profile ("/in/rosa"), or None for any other page."""
    host = parsed.host or ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    path = (parsed.path or "").removesuffix("/")
    return path if _LINKEDIN_PROFILE.fullmatch(path) else None


def _untracked_query(query: str | None) -> str | None:
    """The query without its tracking parameters, as written, or None when nothing is left."""
    if query is None:
        return None
    kept = [
        part
        for part in query.split("&")
        if part
        and (name := unquote(part.partition("=")[0]).casefold()) not in _TRACKING_PARAMETERS
        and not name.startswith("utm_")
    ]
    return "&".join(kept) or None


def same_page(url: object) -> str | None:
    """What two addresses of one page have in common, or None for what is not a web address.

    "linkedin.com/in/rosa" and "https://www.linkedin.com/in/rosa/" lead to the same page: the
    scheme, a "www." in front of the host, the letter case of the host, one "/" at the end of
    the path and the query parameters that only track a visit ("utm_source", LinkedIn's "trk")
    do not count. A LinkedIn profile is its "/in/<name>" alone, on any of LinkedIn's hosts. The
    rest of the path, the query and the fragment are kept as written, since a site may tell
    "/Rosa" from "/rosa"."""
    try:
        parsed = _LINK_URL.validate_python(url)
    except ValidationError:
        return None
    profile = _linkedin_profile(parsed)
    if profile is not None:
        return f"linkedin.com{profile}"
    host = (parsed.host or "").removeprefix("www.")
    port = "" if parsed.port == _DEFAULT_PORTS.get(parsed.scheme) else f":{parsed.port}"
    path = (parsed.path or "").removesuffix("/")
    query = _untracked_query(parsed.query)
    query = f"?{query}" if query is not None else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment is not None else ""
    return f"{host}{port}{path}{query}{fragment}"


def without_tracking(url: str) -> str:
    """The address as a reading keeps it: without the query parameters that only track a visit,
    and a LinkedIn profile as "https://www.linkedin.com/in/<name>". What is not a web address is
    returned as it is, to be refused where it is checked."""
    try:
        parsed = _LINK_URL.validate_python(url)
    except ValidationError:
        return url
    profile = _linkedin_profile(parsed)
    if profile is not None:
        return f"https://www.linkedin.com{profile}"
    parts = urlsplit(str(parsed))
    return urlunsplit(parts._replace(query=_untracked_query(parsed.query) or ""))


def _empty_link_list() -> list[Link]:
    return []


def _without_mailto(value: object) -> object:
    if isinstance(value, str):
        address = value.strip()
        if address[:7].lower() == "mailto:":
            return unquote(address[7:].partition("?")[0])
    return value


class Person(StrictModel):
    name: TrimmedNonblankStr
    email: Annotated[EmailStr, BeforeValidator(_without_mailto)] | None = None
    phone: TrimmedNonblankStr | None = None
    links: list[Link] = Field(default_factory=_empty_link_list)
    location: TrimmedNonblankStr | None = None


def with_single_spaces(person: Person) -> Person:
    """The person with one space between the words of its name, phone and location."""
    return person.model_copy(
        update={
            field: single_spaced(value)
            for field, value in (
                ("name", person.name),
                ("phone", person.phone),
                ("location", person.location),
            )
            if value is not None
        }
    )


def _count_headline_words(text: str) -> int:
    """Words in a headline. A separator standing alone ("|", "·", "-", "&") is not a word."""
    return sum(1 for token in text.split() if any(char.isalnum() for char in token))


def headline_is_too_long(text: str) -> bool:
    return _count_headline_words(text) > MAX_HEADLINE_WORDS


def _require_headline(value: str) -> str:
    if headline_is_too_long(value):
        raise ValueError(
            f"The headline (summary.text) must contain no more than {MAX_HEADLINE_WORDS} words. "
            "The summary paragraph belongs in summary.summary_description."
        )
    return value


# The headline (`text`) and the summary paragraph as a model must write them: both are required.
class Summary(StrictModel):
    # The headline printed under the name. The name `text` does not say so, and a model once put
    # the paragraph here, so its size is checked: a swap is a schema failure, which is repaired.
    text: Annotated[TrimmedNonblankStr, AfterValidator(_require_headline)]
    # The summary paragraph.
    summary_description: TrimmedNonblankStr


# The same two parts on a resume a person has edited. Either may have been cleared, and a cleared
# part is left off the page together with its heading. The size of a headline is checked where a
# hand edit is saved, not here, so a resume stored before that rule can still be read.
class PrintedSummary(StrictModel):
    text: TrimmedNonblankStr | None = None
    summary_description: TrimmedNonblankStr | None = None


_LINE_BREAKS = re.compile(r"[\r\n]+")


def summary_paragraphs(summary: str) -> list[str]:
    """The paragraphs of a summary, as the editor shows them and the browser saves them: each
    line break typed starts one, and empty lines between them count as one break. A line holding
    only what is cut off a stored text (`stored_text`) is empty too, as the browser judges it."""
    lines = (stored_text(line) for line in _LINE_BREAKS.split(summary))
    return [line for line in lines if line]


class TechnicalSkill(StrictModel):
    title: TrimmedNonblankStr
    bullets: list[SkillItem] = Field(min_length=1)

    @model_validator(mode="after")
    def require_single_line_size(self) -> Self:
        if len(self.bullets) > MAX_SKILL_ROW_ITEMS:
            raise ValueError(f"Skill rows must contain no more than {MAX_SKILL_ROW_ITEMS} items.")

        word_count = _count_words(self.title) + sum(_count_words(item) for item in self.bullets)
        if word_count > MAX_SKILL_ROW_WORDS:
            raise ValueError(f"Skill rows must contain no more than {MAX_SKILL_ROW_WORDS} words.")

        rendered_row = f"{self.title}: {' · '.join(self.bullets)}"
        if len(rendered_row) > MAX_SKILL_ROW_CHARACTERS:
            raise ValueError(
                f"Skill rows must contain no more than {MAX_SKILL_ROW_CHARACTERS} characters."
            )

        return self


def _empty_skill_row_list() -> list[TechnicalSkill]:
    return []


# Skill rows. Empty when the candidate profile lists no skills: none are invented.
class TechnicalSkills(StrictModel):
    skills: list[TechnicalSkill] = Field(default_factory=_empty_skill_row_list)


class ProfessionalExperience(StrictModel):
    work_title: TrimmedNonblankStr
    company_name: TrimmedNonblankStr
    location: TrimmedNonblankStr | None = None
    start_date: ResumeDate
    end_date: ResumeDate | None
    bullets: list[ExperienceBullet] = Field(min_length=1)

    @model_validator(mode="after")
    def require_date_order(self) -> Self:
        _require_date_order(self.start_date, self.end_date)
        return self


def _empty_bullet_list() -> list[str]:
    return []


class Education(StrictModel):
    school_name: TrimmedNonblankStr
    degree: TrimmedNonblankStr
    field_of_study: TrimmedNonblankStr | None = None
    start_date: ResumeDate | None = None
    end_date: ResumeDate | None = None
    bullets: list[ExperienceBullet] = Field(default_factory=_empty_bullet_list)

    @model_validator(mode="after")
    def require_date_order(self) -> Self:
        _require_date_order(self.start_date, self.end_date)
        return self


def _empty_certification_list() -> list[str]:
    return []


class Certifications(StrictModel):
    certifications: list[TrimmedNonblankStr] = Field(default_factory=_empty_certification_list)


# What a model is told when its answer is refused for this. A model reads these words, so they
# change only with an evaluation.
_CANNOT_PRINT = "The resume PDF cannot print {}"


def _named(characters: tuple[str, ...]) -> str:
    if len(characters) == 1:
        return f"this character: {as_shown(characters[0])}"
    return f"these characters: {' '.join(map(as_shown, characters))}"


def _empty_professional_experience_list() -> list[ProfessionalExperience]:
    return []


def _empty_education_list() -> list[Education]:
    return []


# The classes below describe what a model returns. Pydantic copies a class docstring into the
# JSON schema as its `description`, and that schema is sent to the model, so the notes on them
# are comments: a docstring here would be part of the prompt.
#
# The sections a model writes and a printed resume both hold.
# - A section the candidate profile has nothing for is empty and is left off the page:
#   experience, education, skills and certifications may each be empty.
class _Sections(StrictModel):
    technical_skills: TechnicalSkills
    professional_experience: list[ProfessionalExperience] = Field(
        default_factory=_empty_professional_experience_list
    )
    education: list[Education] = Field(default_factory=_empty_education_list)
    certifications: Certifications


class _WrittenSummary(StrictModel):
    summary: Summary


class _PrintedSummary(StrictModel):
    summary: PrintedSummary


class _Identity(StrictModel):
    person: Person


# The language a resume prints its headings, month names and "Present" in: the one its profile
# is written in (`resume_language.py`). Code sets it, as it copies the person.
class _Language(StrictModel):
    language: ResumeLanguage

    # A resume saved before languages existed has none, and was printed in English. Filled in
    # here rather than given as a default, so the schema stays one strict mode accepts.
    @model_validator(mode="before")
    @classmethod
    def english_when_saved_before_languages(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        return fields if "language" in fields else {**fields, "language": "en"}


# Pydantic lists the fields of the base named last first. That is why the bases below are in
# reverse reading order: the summary stays the first thing a model is asked to write, and the
# person stays the first field of a saved resume.
#
# ResumeBody is everything on a resume that is written for one job.
# - There is deliberately no person. Contact details are facts to copy, not text to write, so a
#   model is given no field to put them in.
# - `job_title` is not printed: it names the resume on the list and its downloads (`resume_name`),
#   since a posting often begins with a heading ("About the job"). It is the last field, so the resume is written
#   first, and it has no limit: a name is cut by code, and a title refused would cost a repair.
class ResumeBody(_Sections, _WrittenSummary):
    job_title: str = Field(
        default_factory=str,
        description=(
            "The job title this posting hires for, as the posting writes it. "
            "Empty when the posting names no title."
        ),
    )

    # - Every text holds only characters the PDF can print. The rule is here, on what a model
    #   returns, and not on Resume: a resume stored before the rule must still be read. A hand edit
    #   is held to it where it is saved, and what is copied from the profile where a run is created.
    @model_validator(mode="after")
    def require_printable_characters(self) -> Self:
        # One failure for each text, at that text's own location, so a repair request says where.
        failures = [
            InitErrorDetails(
                type="value_error",
                loc=location,
                input=None,  # the text itself is never repeated in an error
                ctx={"error": ValueError(_CANNOT_PRINT.format(_named(characters)))},
            )
            # The job title is never printed, so the PDF's characters are no rule for it.
            for location, characters in unprintable_by_location(
                self.model_dump(mode="json", exclude={"job_title"})
            )
        ]
        if failures:
            raise ValidationError.from_exception_data(type(self).__name__, failures)
        return self


# A resume ready to render: the person it is about, what was written for one job, and the
# language it is printed in.
# It is not a ResumeBody: a person may clear the summary by hand, a model may not leave it out.
class Resume(_Language, _Sections, _PrintedSummary, _Identity):
    pass


def with_sentence_dates_in_words(
    body: ResumeBody,
    language: ResumeLanguage,
    saved_dates: Collection[str] = (),
    *,
    fitted: bool = False,
) -> dict[str, Any]:
    """What a model wrote, as JSON, with each date in a sentence written in words.

    The sentences are the headline, the summary paragraph and the bullets of jobs and education
    (`dates_in_words.py`). A role's own dates and the certification lines are left: the renderers
    print those. The result is validated as a ResumeBody again, because words are longer than
    "2019-06": a bullet or a headline that is now over its limit fails like any other answer.
    With `fitted`, such a text gets its dates as years instead ("since 2019"), and the result is
    as valid as `body`.
    """

    def rewrite(text: str, too_long: Callable[[str], bool]) -> str:
        words = with_dates_in_words(text, language, saved_dates)
        if fitted and too_long(words):
            return with_dates_in_words(text, language, saved_dates, year_only=True)
        return words

    def bullet_too_long(text: str) -> bool:
        return _count_words(text) > MAX_BULLET_WORDS

    written: dict[str, Any] = body.model_dump(mode="json")
    summary = written["summary"]
    summary["text"] = rewrite(summary["text"], headline_is_too_long)
    summary["summary_description"] = rewrite(summary["summary_description"], lambda _: False)
    for entry in (*written["professional_experience"], *written["education"]):
        entry["bullets"] = [rewrite(bullet, bullet_too_long) for bullet in entry["bullets"]]
    return written


def assemble_resume(person: Person, body: ResumeBody, *, language: ResumeLanguage) -> Resume:
    """Put the candidate's own contact details, unchanged, on what was written for the job, and
    the language it is printed in."""
    sections = {name: getattr(body, name) for name in _Sections.model_fields}
    summary = PrintedSummary(
        text=body.summary.text, summary_description=body.summary.summary_description
    )
    return Resume(person=person, summary=summary, language=language, **sections)
