"""A resume from what a worker wrote for one job and the person's career.

The worker writes only `resume-content.json`:

    {"headline": "...", "summary": "...",
     "skills": [{"title": "...", "items": ["..."]}],
     "experience": [{"job": "<career job id>", "bullets": ["..."]}]}

Everything else on the page (name, contacts, employers, titles, places, dates, schools,
certifications) is copied from the career. Every job of the career is printed, so each needs
bullets, and a job id the career does not have is an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from resume_kit.contracts.resume import Resume, ResumeBody
from resume_kit.documents.pdf import render_pdf
from job_hunt.career import Career


class ContentError(ValueError):
    """What is wrong with a worker's content, in words the worker can fix."""


def _body(career: Career, content: dict[str, Any]) -> dict[str, Any]:
    written = {item.get("job"): item.get("bullets") for item in content.get("experience") or []}
    known = {job.id for job in career.jobs}
    problems = [f"experience names job {name!r}, which is not in the career" for name in written if name not in known]
    problems += [f"job {job.id!r} has no bullets" for job in career.jobs if not written.get(job.id)]
    if problems:
        raise ContentError("; ".join(problems))
    return {
        "summary": {"text": content.get("headline", ""), "summary_description": content.get("summary", "")},
        "technical_skills": {
            "skills": [
                {"title": row.get("title", ""), "bullets": row.get("items", [])}
                for row in content.get("skills") or []
            ]
        },
        "professional_experience": [
            {
                "work_title": job.title,
                "company_name": job.company,
                "location": job.location,
                "start_date": job.start_date,
                "end_date": job.end_date,
                "bullets": written[job.id],
            }
            for job in career.jobs
        ],
        "education": [
            {
                "school_name": school.school,
                "degree": school.degree,
                "field_of_study": school.field_of_study,
                "start_date": school.start_date,
                "end_date": school.end_date,
            }
            for school in career.education
        ],
        "certifications": {"certifications": career.certifications},
    }


def build_resume(career: Career, content: dict[str, Any]) -> Resume:
    try:
        body = ResumeBody.model_validate(_body(career, content))
    except ValidationError as error:
        raise ContentError(
            "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
        ) from error
    return Resume.model_validate(
        {**body.model_dump(mode="json", exclude={"job_title"}), "person": career.person}
    )


def write_pdf(career: Career, content_path: Path, pdf_path: Path) -> None:
    content = json.loads(content_path.read_text())
    pdf_path.write_bytes(render_pdf(build_resume(career, content)))
