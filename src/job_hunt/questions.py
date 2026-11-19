"""`hunt questions`: what waits for the person, in the order the chat asks it.

The state is all in `data/`, so a fresh session knows everything by running it:

1. a problem of the morning run, told once (`data/morning.log`, how much of it was told in
   `data/morning.told`);
2. new jobs, numbered as `hunt list` numbers them, for `hunt pick` and `hunt skip`. Asked again,
   the jobs of the last list keep their numbers, so a reply to it still names the same jobs;
   only a job scored since numbers the whole list anew;
3. paused applications, each with what it needs and the command after it;
4. recruiter messages, each with its drafted reply (`replies`), for `hunt send-reply`,
   `hunt edit-reply` and `hunt skip-reply`; one that asks to talk shows the times its reply
   offers, the resume it goes out with, and the time held in the calendar on yes.

Before them comes work the person already approved and that stopped before its end (a picked
job, one whose resume is ready, one left `applying`): it asks nothing, the chat runs
`hunt apply` for it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from job_hunt.apply import UNSURE
from job_hunt.jobs import Job, JobsList

if TYPE_CHECKING:
    from job_hunt.replies import Threads

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


def _lines(log: bytes) -> list[str]:
    # A log cut by hand may start in the middle of a letter.
    return log.decode(errors="replace").splitlines()


def untold_failures(data: Path) -> list[str]:
    log, told = _told(data)
    return _lines(log[told:])


def unfinished(jobs: JobsList) -> list[Job]:
    return jobs.in_states("picked", "resume_ready", "applying")


def anything_waiting(jobs: JobsList, data: Path, threads: Threads | None = None) -> bool:
    if threads is not None:
        # Imported here: `replies` uses this module's `log_failure`.
        from job_hunt.replies import recover_sends

        # A send a dead run left is asked about, so it starts the session too.
        recover_sends(threads, data)
    return bool(untold_failures(data) or jobs.waiting() or jobs.in_states("paused") or unfinished(jobs)
                or (threads is not None and threads.in_states("drafted", "unsure")))


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


def _indented(text: str, first: str) -> list[str]:
    lines = text.strip().splitlines() or [""]
    return [f"     {first}{lines[0]}", *(f"       {line}" for line in lines[1:])]


DESCRIPTION_CHARACTERS = 600


def _recruiters(jobs: JobsList, data: Path, threads: Threads) -> str | None:
    # Imported here: `replies` uses this module's `log_failure`.
    from job_hunt.replies import posting_path, recover_sends, resume_of
    from job_hunt.settings import SettingsError, read_settings
    from job_hunt.times import Talks, zone

    recover_sends(threads, data)
    waiting = threads.in_states("drafted", "unsure")
    if not waiting:
        return None
    lines = [f"Recruiter messages ({len(waiting)}):"]
    for thread in waiting:
        channel = "Email" if thread.source == "email" else "LinkedIn message"
        about = []
        if thread.job is not None:
            job = jobs.get(thread.job)
            about.append(f"about job {job.id}: {job.title} at {job.company}")
        if thread.asks_to_talk:
            about.append("asks to talk")
        subject = f', "{thread.subject}"' if thread.subject else ""
        lines.append(f"  {thread.id}. {channel} from {thread.sender}{subject}"
                     + (f" ({'; '.join(about)})" if about else ""))
        lines += _indented(thread.text, "Message: ")
        if (posting := posting_path(thread, data.parent)) is not None:
            text = posting.read_text()
            cut = text if len(text) <= DESCRIPTION_CHARACTERS else text[:DESCRIPTION_CHARACTERS].rstrip() + " …"
            lines += _indented(cut, f"Job description ({posting}): ")
        lines += _indented(thread.reply or "", "Reply: ")
        if (talk := Talks(data / "jobs.db").get(thread.id)) is not None and talk.state == "offered":
            try:
                where = zone(read_settings(data / "settings.json"))
            except SettingsError:
                where = talk.times()[0].tzinfo
            times = [when.astimezone(where) for when in talk.times()]
            offered = [when.strftime("%a %d %b %H:%M") for when in times]
            lines.append(f"     Times offered: {', '.join(offered)} ({times[0].tzname()})")
            if (resume := resume_of(thread, data.parent)) is not None:
                lines.append(f"     Resume attached: {resume}")
            lines.append(f'     On yes, "Held: {talk.title}" is added to Google Calendar at {offered[0]}')
        if thread.state == "unsure":
            lines.append(f"     Needs {thread.state_reason} (then: hunt skip-reply {thread.id} if it went, "
                         f"hunt send-reply {thread.id} to send it again)")
        else:
            if thread.state_reason:
                lines.append(f"     The last send did not go out: {thread.state_reason}")
            lines.append(f'     (then: hunt send-reply {thread.id}, hunt edit-reply {thread.id} "<the reply>", '
                         f"hunt offer-times {thread.id}, hunt skip-reply {thread.id})")
    return "\n".join(lines)


def questions(jobs: JobsList, data: Path, threads: Threads | None = None) -> str:
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
    if threads is not None and (recruiters := _recruiters(jobs, data, threads)):
        parts.append(recruiters)
    if failures:
        # Up to what was read: a failure logged meanwhile is told next time.
        (data / TOLD).write_text(f"{len(log)}\n")
    return "\n\n".join(parts) + "\n" if parts else "Nothing waits for you.\n"
