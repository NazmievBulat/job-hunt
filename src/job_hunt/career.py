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
