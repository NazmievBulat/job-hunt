"""Normalize pasted text only at the hand-edited resume boundary."""

import re
from typing import cast

from resume_kit.contracts.base import stored_text, without_unstorable_characters
from resume_kit.contracts.resume import summary_paragraphs

# White space a one-line field holds as one space. A no-break space prints the same in the page,
# the PDF and the Word file, and keeps "20 %" or "1 000" on one line, so it stays.
_FOLDED_SPACE = re.compile(r"[^\S\u00a0\u2007\u202f]+")


def _one_line(value: str) -> str:
    return _FOLDED_SPACE.sub(" ", stored_text(without_unstorable_characters(value)))


def _lines(value: object) -> object:
    if isinstance(value, str):
        return _one_line(value)
    if isinstance(value, list):
        return [_lines(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {key: _lines(item) for key, item in cast(dict[str, object], value).items()}
    return value


def clean_resume_edit(value: object) -> object:
    """Clean writable text before `Resume` validates it; locked facts are left for its checks.

    Keep the normal contract strict everywhere else, including model output and stored runs.
    Copy containers so validation does not mutate the caller's input.
    """
    if not isinstance(value, dict):
        return value
    resume = dict(cast(dict[str, object], value))
    summary = resume.get("summary")
    if isinstance(summary, dict):
        cleaned = dict(cast(dict[str, object], summary))
        headline = cleaned.get("text")
        if isinstance(headline, str):
            cleaned["text"] = _one_line(headline) or None
        description = cleaned.get("summary_description")
        if isinstance(description, str):
            cleaned["summary_description"] = (
                "\n\n".join(summary_paragraphs(without_unstorable_characters(description))) or None
            )
        resume["summary"] = cleaned
    if "technical_skills" in resume:
        resume["technical_skills"] = _lines(resume["technical_skills"])
    jobs = resume.get("professional_experience")
    if isinstance(jobs, list):
        cleaned_jobs: list[object] = []
        for job in cast(list[object], jobs):
            if isinstance(job, dict):
                cleaned_job = dict(cast(dict[str, object], job))
                if "bullets" in cleaned_job:
                    cleaned_job["bullets"] = _lines(cleaned_job["bullets"])
                cleaned_jobs.append(cleaned_job)
            else:
                cleaned_jobs.append(job)
        resume["professional_experience"] = cleaned_jobs
    return resume
