"""`hunt questions`: what waits for the person, in the order the chat asks it.

The state is all in `data/`, so a fresh session knows everything by running it:

1. a problem of the morning run, told once (`data/morning.log`, how much of it was told in
   `data/morning.told`);
2. new jobs, numbered as `hunt list` numbers them, for `hunt pick` and `hunt skip`. Asked again,
   the jobs of the last list keep their numbers, so a reply to it still names the same jobs;
   only a job scored since numbers the whole list anew;
3. paused applications, each with what it needs and the command after it. One that needs
   hands in Chrome (a CAPTCHA, a sign-in) carries the Chrome Remote Desktop link when that is
   set up, so it is done from the phone; without it, it waits until the person is at the
   computer;
4. recruiter messages and 5. requests to talk, once the jobs list keeps them.

Before them comes work the person already approved and that stopped before its end (a picked
job, one whose resume is ready, one left `applying`): it asks nothing, the chat runs
`hunt apply` for it.
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
REMOTE_DESKTOP = "https://remotedesktop.google.com/access"


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


def _lines(log: bytes) -> list[str]:
    # A log cut by hand may start in the middle of a letter.
    return log.decode(errors="replace").splitlines()


def untold_failures(data: Path) -> list[str]:
    log, told = _told(data)
    return _lines(log[told:])


def unfinished(jobs: JobsList) -> list[Job]:
    return jobs.in_states("picked", "resume_ready", "applying")


def anything_waiting(jobs: JobsList, data: Path) -> bool:
    return bool(untold_failures(data) or jobs.waiting() or jobs.in_states("paused") or unfinished(jobs))


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


def questions(jobs: JobsList, data: Path, *, remote_desktop: bool = False) -> str:
    """The questions as text. Showing them numbers the new jobs and marks the failures told."""
    parts: list[str] = []
    log, told = _told(data)
    failures = _lines(log[told:])
    if failures:
        parts.append("\n".join(["The morning run had a problem:", *map(_failure, failures)]))
    left = unfinished(jobs)
    if left:
        parts.append("\n".join([f"Unfinished applications ({len(left)}): run hunt apply", *(
            f"  {job.id}. {job.title} at {job.company}" for job in left)]))
    waiting = {job.id: job for job in jobs.waiting()}
    kept = {number: waiting[job] for number, job in sorted(jobs.last_list().items()) if job in waiting}
    if waiting and len(kept) == len(waiting):
        parts.append("\n".join([f"New jobs ({len(kept)}):", *(job_line(number, job) for number, job in kept.items())]))
    elif waiting:
        new = jobs.show()
        heading = f"New jobs ({len(new)}), numbered anew:" if kept else f"New jobs ({len(new)}):"
        parts.append("\n".join([heading, *(job_line(number, job) for number, job in enumerate(new, start=1))]))
    paused = jobs.in_states("paused")
    if paused:
        parts.append("\n".join([f"Paused applications ({len(paused)}):", *(
            f"  {job.id}. {job.title} at {job.company}: needs {job.state_reason} (then: {then(job)})"
            for job in paused)]))
        # An answer is typed from anywhere; the rest is done in this computer's Chrome.
        if any(not (job.state_reason or "").startswith("an answer to ") for job in paused):
            parts[-1] += ("\n  From your phone: open " + REMOTE_DESKTOP + " and do it in this computer's Chrome."
                          if remote_desktop else "\n  This waits until you are at the computer; the other jobs go on.")
    if failures:
        # Up to what was read: a failure logged meanwhile is told next time.
        (data / TOLD).write_text(f"{len(log)}\n")
    return "\n\n".join(parts) + "\n" if parts else "Nothing waits for you.\n"
