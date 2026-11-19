"""Scoring: the new jobs, each given a fit score from 1 to 10 and one line of reason.

One `claude -p` on Claude Haiku runs the `score-jobs` skill, allowed only to read the job
folders and the career and to write one file:

    {"scores": [{"job": 12, "score": 8, "reason": "Selenium and Python, as in the last two jobs."}]}

The script checks the whole file before it saves anything: a file that is not that shape, or
one wrong entry, is refused and every job stays `new` for the next run. A job the file leaves
out stays `new` too, and is named.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from job_hunt.finder import ONLY_NAMED_TOOLS
from job_hunt.jobs import Job, JobsList

MODEL = "claude-haiku-5-5"
SKILL = "score-jobs"


class ScoreError(RuntimeError):
    """The scoring run failed or wrote a file that cannot be saved; no job was scored."""


@dataclass
class Summary:
    scored: int
    missing: list[str] = field(default_factory=list)
    asked: bool = True

    def __str__(self) -> str:
        return f"{self.scored} jobs scored" if self.asked else "no new jobs to score"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    """The job folders, the career and the score file."""
    return root / "jobs", root / "data" / "career.json", root / "jobs" / "scores.json"


def skill_command(jobs: list[dict[str, Any]], root: Path) -> list[str]:
    _, career, out = _paths(root)
    arguments = {"jobs": jobs, "career": str(career), "out": str(out)}
    # A permission rule's path: `//` starts an absolute one. Edit rules cover every tool that
    # writes a file, the Write tool among them. The postings sent, not every job folder: those
    # hold the other jobs' reports and the recruiters' messages too.
    postings = [f"Read(/{job['posting']})" for job in jobs]
    return [
        "claude", "-p", f"/{SKILL} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL, *ONLY_NAMED_TOOLS,
        "--allowedTools", ",".join([*postings, f"Read(/{career})", f"Edit(/{out})"]),
    ]


def _entry(number: int, entry: Any, sent: set[int]) -> tuple[int, int, str]:
    if not isinstance(entry, dict):
        raise ScoreError(f"entry {number} is not an object")
    job = entry.get("job")
    if type(job) is not int:
        raise ScoreError(f"entry {number} names no job")
    if job not in sent:
        raise ScoreError(f"entry {number} scores job {job}, which was not sent to be scored")
    score = entry.get("score")
    if type(score) is not int or not 1 <= score <= 10:
        raise ScoreError(f"entry {number} gives job {job} {score!r}, not a whole number from 1 to 10")
    reason = entry.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) else ""
    if not reason:
        raise ScoreError(f"entry {number} gives job {job} no reason")
    return job, score, reason


def read_scores(path: Path, sent: set[int]) -> dict[int, tuple[int, str]]:
    """Each job's score and one-line reason, or ScoreError naming the first thing wrong."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ScoreError(f"the {SKILL} run wrote a file that is not JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("scores"), list):
        raise ScoreError(f'the {SKILL} run wrote a file without a list of "scores"')
    scores: dict[int, tuple[int, str]] = {}
    for number, entry in enumerate(data["scores"], start=1):
        job, score, reason = _entry(number, entry, sent)
        if job in scores:
            raise ScoreError(f"the {SKILL} run scores job {job} twice")
        scores[job] = (score, reason)
    return scores


def _sent(job: Job, folders: Path) -> dict[str, Any]:
    return {"id": job.id, "title": job.title, "company": job.company,
            "posting": str(folders / str(job.id) / "posting.md")}


def score(jobs: JobsList, root: Path, *, run: Callable[[list[str]], None]) -> Summary:
    folders, _, out = _paths(root)
    folders.mkdir(exist_ok=True)
    # One run at a time: two would score the same new jobs and write one score file.
    with out.with_name(out.name + ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ScoreError("another hunt score is already running") from error
        return _score(jobs, folders, out, root, run=run)


def _score(jobs: JobsList, folders: Path, out: Path, root: Path, *, run: Callable[[list[str]], None]) -> Summary:
    new = [job for job in jobs.all() if job.state == "new"]
    if not new:
        return Summary(0, asked=False)
    # A file an earlier run left must not pass for this run's.
    out.unlink(missing_ok=True)
    run(skill_command([_sent(job, folders) for job in new], root))
    if not out.exists():
        raise ScoreError(f"the {SKILL} run wrote no score file ({out})")
    scores = read_scores(out, {job.id for job in new})
    for job, (value, reason) in scores.items():
        jobs.score(job, value, reason)
    missing = [f"job {job.id} ({job.title} at {job.company})" for job in new if job.id not in scores]
    return Summary(len(scores), missing)
