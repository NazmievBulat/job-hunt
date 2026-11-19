"""`hunt questions`: what waits for the person, in the order the chat asks it.

The state is all in `data/`, so a fresh session knows everything by running it:

1. a problem of the morning run, told once (`data/morning.log`, how much of it was told in
   `data/morning.told`);
2. new jobs, numbered as `hunt list` numbers them, for `hunt pick` and `hunt skip`;
3. paused applications, each with what it needs and the command after it;
4. recruiter messages and 5. requests to talk, once the jobs list keeps them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from job_hunt.apply import UNSURE
from job_hunt.jobs import Job, JobsList

LOG = "morning.log"
TOLD = "morning.told"
KINDS = {"easy_apply": "Easy Apply", "company_site": "company site"}


def log_failure(data: Path, step: str, error: object, clock: Callable[[], datetime]) -> None:
    """Note a failed step of the morning run, for the next questions to tell."""
    data.mkdir(parents=True, exist_ok=True)
    said = " ".join(str(error).split())
    with (data / LOG).open("a") as log:
        log.write(f"{clock().isoformat()} {step}: {said}\n")


def _told(data: Path) -> tuple[bytes, int]:
    """The log and how much of it was told already."""
    log = (data / LOG).read_bytes() if (data / LOG).exists() else b""
    try:
        told = int((data / TOLD).read_text())
    except (FileNotFoundError, ValueError):
        told = 0
    # A log cut down or removed by hand starts again from its beginning.
    return log, told if told <= len(log) else 0


def untold_failures(data: Path) -> list[str]:
    log, told = _told(data)
    return log[told:].decode().splitlines()


def anything_waiting(jobs: JobsList, data: Path) -> bool:
    return bool(untold_failures(data) or jobs.waiting() or jobs.in_states("paused"))


def where(job: Job) -> str:
    if job.remote == "remote":
        return "remote"
    place = job.place or "place not given"
    return f"{place}, hybrid" if job.remote == "hybrid" else place


def job_line(number: int, job: Job) -> str:
    kind = KINDS.get(job.kind or "", "way to apply not known")
    return (f"{number:>2}. {job.score:>2}  {job.title} · {job.company} · {where(job)} · {kind}\n"
            f"        {job.score_reason}")


def then(job: Job) -> str:
    """The command that carries on with a paused job once the person did what it needs."""
    if job.state_reason and job.state_reason.startswith("an answer to "):
        return f'hunt answer {job.id} "<the answer>"'
    if job.state_reason and job.state_reason.startswith(UNSURE):
        return f"hunt applied {job.id} if it was sent, hunt resume-job {job.id} if not"
    return f"hunt resume-job {job.id}"


def _failure(line: str) -> str:
    stamp, _, said = line.partition(" ")
    try:
        when = datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M %Z").strip()
    except ValueError:
        return f"  {line}"
    return f"  {said} (at {when})"


def questions(jobs: JobsList, data: Path) -> str:
    """The questions as text. Showing them numbers the new jobs and marks the failures told."""
    parts: list[str] = []
    log, told = _told(data)
    failures = log[told:].decode().splitlines()
    if failures:
        parts.append("\n".join(["The morning run had a problem:", *map(_failure, failures)]))
    new = jobs.show()
    if new:
        parts.append("\n".join([f"New jobs ({len(new)}):",
                                *(job_line(number, job) for number, job in enumerate(new, start=1))]))
    paused = jobs.in_states("paused")
    if paused:
        parts.append("\n".join([f"Paused applications ({len(paused)}):", *(
            f"  {job.id}. {job.title} at {job.company}: needs {job.state_reason} (then: {then(job)})"
            for job in paused)]))
    if failures:
        # Up to what was read: a failure logged meanwhile is told next time.
        (data / TOLD).write_text(f"{len(log)}\n")
    return "\n\n".join(parts) + "\n" if parts else "Nothing waits for you.\n"
