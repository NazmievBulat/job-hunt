"""A person's career, cleaned from a saved career export into what a resume worker reads.

The export holds the same job several times, once for each time a resume was imported, under
different spellings of the employer ("BD", "BECTON, DICKINSON (BD)"). Jobs are the same job when
their dates are the same. The first entry, in the order they were saved, is the one printed: its
employer, title and location. The bullets of all of them are kept as material to write from.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from resume_kit.contracts.base import CareerDate, dates_in_order
from resume_kit.contracts.form_answers import ReadyAnswers
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
    return _unique([text for text in value if isinstance(text, str)]) if isinstance(value, list) else []


def _dates(entry: dict[str, Any], named: str, problems: list[str], *, start_needed: bool) -> tuple[str | None, str | None]:
    dates: list[str | None] = []
    for field_name, words in (("start_date", "start date"), ("end_date", "end date")):
        value = _text(entry.get(field_name)) or None
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
        education.append(School(school=_text(entry.get("school")), degree=_text(entry.get("degree")),
                                field_of_study=_text(entry.get("field_of_study")) or None,
                                start_date=start, end_date=end))
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
        form_answers=kept.form_answers if kept is not None else {},
    )


def with_ready_answers(career: Career, answers: dict[str, Any]) -> Career:
    """The career with the standing form answers changed, beside the answers saved from forms."""
    unknown = sorted(set(answers) - set(STANDING))
    if unknown:
        raise CareerError(f"not a standing answer: {', '.join(unknown)} (they are {', '.join(STANDING)})")
    ready = {**(career.form_answers.get("ready") or {}), **answers}
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
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        lines = [paragraph.text for paragraph in document.paragraphs]
        lines += [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        return "\n".join(line for line in lines if line.strip())
    raise CareerError(f"{path.name} is not a PDF or a Word (.docx) file: save it as one, or paste its text")


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
