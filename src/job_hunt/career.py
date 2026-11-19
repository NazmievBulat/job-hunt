"""A person's career, cleaned from a saved career export into what a resume worker reads.

The export holds the same job several times, once for each time a resume was imported, under
different spellings of the employer ("BD", "BECTON, DICKINSON (BD)"). Jobs are the same job when
their dates are the same. The first entry, in the order they were saved, is the one printed: its
employer, title and location. The bullets of all of them are kept as material to write from.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from resume_kit.contracts.base import CareerDate, dates_in_order
from resume_kit.contracts.form_answers import ReadyAnswers
from resume_kit.contracts.printable import as_shown, unprintable_by_location
from resume_kit.contracts.resume import Person

# The standing answers /setup asks once; the other ready answers are the settings' to give.
STANDING = ("authorized", "sponsorship", "salary", "notice", "relocate")
DRAFT_FIELDS = {"person", "summary", "titles", "skills", "jobs", "education", "certifications"}
DRAFT_JOB = {"title", "company", "location", "start_date", "end_date", "material"}
DRAFT_SCHOOL = {"school", "degree", "field_of_study", "start_date", "end_date"}
_DATE = TypeAdapter(CareerDate)


class CareerError(ValueError):
    """Why a career cannot be saved, naming every thing to fix."""


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str | None
    start_date: str | None
    end_date: str | None
    material: list[str] = field(default_factory=list)


@dataclass
class School:
    school: str
    degree: str
    field_of_study: str | None
    start_date: str | None
    end_date: str | None


@dataclass
class Career:
    person: dict[str, Any]
    summary: str
    titles: list[str]
    skills: list[str]
    jobs: list[Job]
    education: list[School]
    certifications: list[str]
    form_answers: dict[str, Any]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _unique(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for text in texts:
        key = _slug(text)
        if key and key not in seen:
            seen.add(key)
            kept.append(text.strip())
    return kept


def _school(role: dict[str, Any]) -> School:
    # Saved as "Bachelor's degree, Economics" or "Bachelor of Economics · Economics".
    parts = re.split(r"\s*[,·]\s*", role["title"], maxsplit=1)
    degree = parts[0]
    field_of_study = parts[1] if len(parts) > 1 else None
    return School(
        school=role["organization"],
        degree=degree,
        field_of_study=field_of_study,
        start_date=role.get("start_date"),
        end_date=role.get("end_date"),
    )


def career_from_export(export: dict[str, Any]) -> Career:
    jobs: dict[tuple[str | None, str | None], Job] = {}
    schools: dict[str, School] = {}
    for role in export.get("roles") or []:
        bullets = [bullet["text"] for bullet in role.get("bullets") or []]
        if role["kind"] == "education":
            school = _school(role)
            # The same school saved under another name ("State" and "Federal") shares its first word.
            key = _slug(school.school).split("-")[0]
            kept = schools.get(key)
            if kept is None or (kept.start_date is None and school.start_date is not None):
                schools[key] = school
            continue
        key = (role.get("start_date"), role.get("end_date"))
        job = jobs.get(key)
        if job is None:
            job = Job(
                id=f"{_slug(role['organization'])}-{role.get('start_date') or 'undated'}",
                title=role["title"],
                company=role["organization"],
                location=role.get("location"),
                start_date=role.get("start_date"),
                end_date=role.get("end_date"),
            )
            jobs[key] = job
        job.material = _unique(job.material + bullets)
    return Career(
        person=(export.get("profile") or {}).get("person") or {},
        summary=export.get("summary") or "",
        titles=_unique([title["text"] for title in export.get("titles") or []]),
        skills=_unique([skill["name"] for skill in export.get("skills") or []]),
        jobs=sorted(jobs.values(), key=lambda job: job.start_date or "", reverse=True),
        education=list(schools.values()),
        certifications=_unique(
            [c["name"].rstrip(". ") for c in export.get("certifications") or []]
        ),
        form_answers=export.get("form_answers") or {},
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value: Any) -> list[str]:
    """Each text once. Unlike `_unique`, punctuation counts: C, C++ and C# are three skills."""
    seen: set[str] = set()
    kept: list[str] = []
    for text in value if isinstance(value, list) else []:
        key = " ".join(text.casefold().split()) if isinstance(text, str) else ""
        if key and key not in seen:
            seen.add(key)
            kept.append(" ".join(text.split()))
    return kept


def _dates(entry: dict[str, Any], named: str, problems: list[str], *, start_needed: bool) -> tuple[str | None, str | None]:
    dates: list[str | None] = []
    for field_name, words in (("start_date", "start date"), ("end_date", "end date")):
        given = entry.get(field_name)
        if given is not None and not isinstance(given, str):
            # A year given as a number is a date, not a missing one: dropped, an end would read as now.
            problems.append(f"{named}: the {words} {given!r} is not written as text (\"2019\", \"2019-03\")")
            dates.append(None)
            continue
        value = _text(given) or None
        if value is None:
            if field_name == "start_date" and start_needed:
                problems.append(f"{named} has no start date")
        else:
            try:
                _DATE.validate_python(value)
            except ValidationError:
                problems.append(f"{named}: the {words} {value!r} is not a year or a year and month (2019, 2019-03)")
                value = None
        dates.append(value)
    start, end = dates
    if start and end and not dates_in_order(start, end):
        problems.append(f"{named} ends before it starts")
    return start, end


def _unknown(entry: dict[str, Any], known: set[str], named: str, problems: list[str]) -> None:
    unknown = sorted(set(entry) - known)
    if unknown:
        problems.append(f"{named} has fields a career does not have: {', '.join(unknown)} "
                        f"(it has {', '.join(sorted(known))})")


def _printable(value: Any, named: str, problems: list[str]) -> None:
    """A text the career gives every resume as it is, so one the PDF cannot print is refused: the
    resume writer cannot change it."""
    for location, characters in unprintable_by_location(value):
        where = ".".join(map(str, location))
        problems.append(f"{named}{f'.{where}' if where else ''}: the resume PDF cannot print "
                        f"{', '.join(map(as_shown, characters))}")


def career_from_resume(draft: dict[str, Any], kept: Career | None = None) -> Career:
    """The career the read-resume skill wrote from a resume. The jobs get the ids an export's
    jobs get, so the same resume read again keeps them; the form answers of the career it
    replaces (`kept`) stay. Every problem is named at once in a CareerError."""
    problems: list[str] = []
    if not isinstance(draft, dict):
        raise CareerError("the draft must be one JSON object")
    _unknown(draft, DRAFT_FIELDS, "the draft", problems)
    person = draft.get("person") if isinstance(draft.get("person"), dict) else {}
    try:
        Person.model_validate(person)
    except ValidationError as error:
        problems += [f"person.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()]
    if not _text(person.get("email")):
        problems.append("person has no email: forms and the resume need one")
    _printable(person, "person", problems)
    entries = draft.get("jobs") if isinstance(draft.get("jobs"), list) else []
    if not entries:
        problems.append("the draft has no jobs: a resume prints at least one")
    jobs: list[Job] = []
    for number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"job {number} is not an object")
            continue
        title, company = _text(entry.get("title")), _text(entry.get("company"))
        named = f"job {number} ({json.dumps(title, ensure_ascii=False)}{f' at {company}' if company else ''})"
        _unknown(entry, DRAFT_JOB, named, problems)
        if not company:
            problems.append(f"{named} has no employer (company)")
        if not title:
            problems.append(f"{named} has no title")
        start, end = _dates(entry, named, problems, start_needed=True)
        for field_name, words in (("company", "the employer"), ("title", "the title"), ("location", "the place")):
            _printable(_text(entry.get(field_name)), f"{named}: {words}", problems)
        jobs.append(Job(id="", title=title, company=company, location=_text(entry.get("location")) or None,
                        start_date=start, end_date=end, material=_texts(entry.get("material"))))
    education: list[School] = []
    for number, entry in enumerate(draft.get("education") or [], start=1):
        if not isinstance(entry, dict):
            problems.append(f"school {number} is not an object")
            continue
        named = f"school {number} ({json.dumps(_text(entry.get('school')), ensure_ascii=False)})"
        _unknown(entry, DRAFT_SCHOOL, named, problems)
        if not _text(entry.get("school")) or not _text(entry.get("degree")):
            problems.append(f"{named} needs both its school and its degree")
        start, end = _dates(entry, named, problems, start_needed=False)
        for field_name, words in (("school", "the school"), ("degree", "the degree"),
                                  ("field_of_study", "the field of study")):
            _printable(_text(entry.get(field_name)), f"{named}: {words}", problems)
        education.append(School(school=_text(entry.get("school")), degree=_text(entry.get("degree")),
                                field_of_study=_text(entry.get("field_of_study")) or None,
                                start_date=start, end_date=end))
    for number, certification in enumerate(_texts(draft.get("certifications")), start=1):
        _printable(certification, f"certification {number} ({json.dumps(certification, ensure_ascii=False)})",
                   problems)
    if problems:
        raise CareerError("; ".join(problems))
    # Ids from the job alone, never its place in the resume: read again, a job keeps its id.
    jobs.sort(key=lambda job: (job.start_date or "", job.company, job.title), reverse=True)
    taken: dict[str, int] = {}
    for job in sorted(jobs, key=lambda job: (job.start_date or "", job.company, job.title)):
        base = f"{_slug(job.company)}-{job.start_date}"
        taken[base] = taken.get(base, 0) + 1
        job.id = base if taken[base] == 1 else f"{base}-{taken[base]}"
    return Career(
        person={key: value for key, value in person.items() if value not in (None, "", [])},
        summary=_text(draft.get("summary")),
        titles=_texts(draft.get("titles")),
        skills=_texts(draft.get("skills")),
        jobs=jobs,
        education=education,
        certifications=_texts(draft.get("certifications")),
        form_answers=_form_answers(kept.form_answers) if kept is not None else {},
    )


def _form_answers(value: Any) -> dict[str, Any]:
    """Form answers as the hunt reads them: an object, its `ready` an object too. A damaged
    `ready` is dropped, and /setup asks the standing answers again."""
    if not isinstance(value, dict):
        return {}
    return {key: answer for key, answer in value.items() if key != "ready" or isinstance(answer, dict)}


def with_ready_answers(career: Career, answers: dict[str, Any]) -> Career:
    """The career with the standing form answers changed, beside the answers saved from forms."""
    unknown = sorted(set(answers) - set(STANDING))
    if unknown:
        raise CareerError(f"not a standing answer: {', '.join(unknown)} (they are {', '.join(STANDING)})")
    career.form_answers = _form_answers(career.form_answers)
    ready = {**career.form_answers.get("ready", {}), **answers}
    try:
        checked = ReadyAnswers.model_validate(ready)
    except ValidationError as error:
        raise CareerError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())) from None
    kept = checked.model_dump(include=set(ready))
    career.form_answers = {**career.form_answers, "ready": kept}
    return career


def resume_text(path: Path) -> str:
    """The text of a PDF or Word resume, for the read-resume skill: Claude reads a PDF itself,
    but not a .docx. A Word resume laid out in a table keeps its cells, one a line."""
    suffix = path.suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise CareerError(f"{path.name} is not a PDF or a Word (.docx) file: save it as one, or paste its text")
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
        from docx import Document

        document = Document(str(path))
    except FileNotFoundError:
        raise
    except Exception as error:
        # pypdf and python-docx each raise their own errors for a damaged or misnamed file.
        raise CareerError(f"{path.name} could not be read ({type(error).__name__}): save it again, "
                          "or paste its text") from None
    # A name and contacts are often in the page header, so it comes first.
    lines = [line for part in _headers(document, "header") for line in _lines(part)]
    lines += _lines(document)
    lines += [line for part in _headers(document, "footer") for line in _lines(part)]
    return "\n".join(line for line in lines if line.strip())


def _headers(document: Any, kind: str) -> Iterator[Any]:
    """Each header (or footer) the document prints: the first page's and the even pages' too when
    a section has them, and one linked to the section before only once, where it is defined."""
    even = document.settings.odd_and_even_pages_header_footer
    for number, section in enumerate(document.sections):
        parts = [getattr(section, f"first_page_{kind}")] if section.different_first_page_header_footer else []
        parts.append(getattr(section, kind))
        if even:
            parts.append(getattr(section, f"even_page_{kind}"))
        yield from (part for part in parts if number == 0 or not part.is_linked_to_previous)


def _lines(part: Any) -> list[str]:
    """The paragraphs of a document, header or footer, then its table cells, one a line."""
    return ([paragraph.text for paragraph in part.paragraphs]
            + [cell.text for table in part.tables for row in table.rows for cell in row.cells])


@contextlib.contextmanager
def career_lock(path: Path) -> Iterator[None]:
    """Held from reading the career to writing it, by every writer: two at once would each write
    the career without the other's change. The folder is made, as the first career saved needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "career.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_career(career: Career, path: Path) -> None:
    """Replace the file whole, so a reader meanwhile sees the old career or the new one, never
    half of one. The temporary file is made mode 600, as the career is."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as out:
            out.write(json.dumps(asdict(career), indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_career(path: Path) -> Career:
    data = json.loads(path.read_text())
    return Career(
        **{
            **data,
            "jobs": [Job(**job) for job in data["jobs"]],
            "education": [School(**school) for school in data["education"]],
        }
    )
