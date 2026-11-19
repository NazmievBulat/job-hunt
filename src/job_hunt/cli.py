"""`hunt`: every mechanical step of the job hunt."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from job_hunt.apply import (
    LINKEDIN_SKILL, RESUME_SKILL, SITE_SKILL, AnswerError, ApplyError, answer, apply, applied, resume_job,
)
from job_hunt.career import career_from_export, read_career, write_career
from job_hunt.companies import Companies, fill_index
from job_hunt.finder import SKILL as ALERTS_SKILL, FinderError, fetch_page, find, run_claude
from job_hunt.finder import Summary as FindSummary
from job_hunt.jobs import Job, JobsList, PickError
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.morning import morning, session_running, start_session
from job_hunt.notes import DRAFT_SKILL as NOTE_DRAFT_SKILL, SEND_SKILL as NOTE_SEND_SKILL, NoteError, Notes, collect
from job_hunt.notes import decide as decide_notes, draft_notes, note_pause, send_notes, stopped_today, went
from job_hunt.notify import NotifyError, notify, post_pushover
from job_hunt.questions import job_line, log_failure, questions, then
from job_hunt.replies import (
    DRAFT_SKILL, READ_SKILL, SEND_SKILL, EmailMessages, ReplyError, Threads, draft_replies, edit_reply, read_linkedin,
    send_reply, skip_reply,
)
from job_hunt.resume import ContentError, write_pdf
from job_hunt.scoring import SKILL as SCORE_SKILL, ScoreError, score
from job_hunt.scoring import Summary as ScoreSummary
from job_hunt.search import Pace, Search, http_get
from job_hunt.settings import SettingsError, read_settings
from job_hunt.timer import TimerError, timer
from job_hunt.watch import tell_session, watch

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CAREER = DATA / "career.json"
SLEEP = time.sleep


def _career(args: argparse.Namespace) -> int:
    export = json.loads(Path(args.export).read_text())
    career = career_from_export(export)
    write_career(career, CAREER)
    print(f"{len(career.jobs)} jobs, {len(career.education)} schools, {len(career.skills)} skills -> {CAREER}")
    for job in career.jobs:
        print(f"  {job.id}: {job.title} at {job.company}, {job.start_date} to {job.end_date or 'now'}")
    return 0


def _resume(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    try:
        write_pdf(read_career(CAREER), folder / "resume-content.json", folder / "resume.pdf")
    except ContentError as error:
        print(f"resume-content.json needs a fix: {error}", file=sys.stderr)
        return 1
    print(folder / "resume.pdf")
    return 0


def run_skill(command: list[str]) -> None:
    run_claude(command, ROOT)


def site_search() -> Search | None:
    """LinkedIn's search and the company boards, over the network."""
    return Search(get=http_get, pace=Pace(sleep=SLEEP), companies=Companies(DATA / "companies.db"), sleep=SLEEP,
                  clock=lambda: datetime.now(UTC))


def find_step() -> FindSummary:
    # The settings first: without them nothing is made in data/.
    settings = read_settings(DATA / "settings.json")
    jobs = JobsList(DATA / "jobs.db")
    summary = find(
        settings,
        jobs,
        ROOT / "jobs",
        DATA / "found-jobs.json",
        run=run_skill,
        fetch=fetch_page,
        search=site_search(),
        messages=EmailMessages(Threads(DATA / "jobs.db"), jobs),
    )
    for problem in summary.problems:
        print(f"hunt find: left out {problem}", file=sys.stderr)
    for failed in summary.failed:
        print(f"hunt find: {failed}", file=sys.stderr)
    if summary.lines:
        print(summary)
    return summary


def _find_and_tell() -> None:
    """For the morning: a job the finder left out, or a source that failed, is a failure it
    tells, not only a line."""
    summary = find_step()
    told = [f"left out {'; '.join(summary.problems)}"] if summary and summary.problems else []
    told += summary.failed if summary else []
    if told:
        raise FinderError("; ".join(told))


def _find(args: argparse.Namespace) -> int:
    try:
        summary = find_step()
    except (SettingsError, FinderError) as error:
        print(f"hunt find: {error}", file=sys.stderr)
        return 1
    return 1 if summary.failed else 0


def _companies(args: argparse.Namespace) -> int:
    line, problems = fill_index(Companies(DATA / "companies.db"), http_get, sleep=SLEEP)
    for problem in problems:
        print(f"hunt companies: {problem}", file=sys.stderr)
    print(line)
    return 0


def run_scoring(command: list[str]) -> None:
    run_claude(command, ROOT, skill=SCORE_SKILL, error=ScoreError)


def score_step() -> ScoreSummary:
    summary = score(JobsList(DATA / "jobs.db"), ROOT, run=run_scoring)
    for job in summary.missing:
        print(f"hunt score: not scored, still new: {job}", file=sys.stderr)
    print(summary)
    return summary


def _score_every_job() -> None:
    """For the morning: a job the scoring left out is a failure it tells, not only a line."""
    summary = score_step()
    if summary and summary.missing:
        raise ScoreError(f"not scored, still new: {', '.join(summary.missing)}")


def _score(args: argparse.Namespace) -> int:
    try:
        score_step()
    except ScoreError as error:
        print(f"hunt score: {error}", file=sys.stderr)
        return 1
    return 0


def _list(args: argparse.Namespace) -> int:
    shown = JobsList(DATA / "jobs.db").show()
    if not shown:
        print("No scored jobs wait for your pick.")
    for number, job in enumerate(shown, start=1):
        print(job_line(number, job))
    return 0


def _decide(args: argparse.Namespace) -> int:
    jobs = JobsList(DATA / "jobs.db")
    above = getattr(args, "above", None)
    try:
        if above is None:
            if not args.numbers:
                raise PickError("give the numbers to pick, or --above and a score"
                                if args.command == "pick" else "give the numbers to skip")
            numbers = args.numbers
        else:
            if args.numbers:
                raise PickError("give the numbers to pick, or --above and a score")
            if not 0 <= above <= 9:
                raise PickError("--above takes a score from 0 to 9")
            numbers = []
        decision = "picked" if args.command == "pick" else "skipped"
        decided = jobs.decide(numbers, decision, above=above)
    except PickError as error:
        print(f"hunt {args.command}: {error}", file=sys.stderr)
        return 1
    if not decided:
        print(f"no job of the last list waiting for a pick scores above {above}")
    for number, job in decided:
        print(f"{decision} {number}: {job.title} at {job.company}")
    return 0


def run_resume(command: list[str]) -> None:
    run_claude(command, ROOT, skill=RESUME_SKILL, error=ApplyError)


def run_site(command: list[str]) -> None:
    run_claude(command, ROOT, skill=SITE_SKILL, error=ApplyError)


def run_linkedin(command: list[str]) -> None:
    run_claude(command, ROOT, skill=LINKEDIN_SKILL, error=ApplyError)


def _apply(args: argparse.Namespace) -> int:
    try:
        summary = apply(JobsList(DATA / "jobs.db"), ROOT, run_resume=run_resume, run_site=run_site,
                        run_linkedin=run_linkedin)
    except ApplyError as error:
        print(f"hunt apply: {error}", file=sys.stderr)
        return 1
    print(summary)
    # The notes to the posters of today's LinkedIn applications, for the chat to ask about.
    try:
        if drafted := draft_notes_step():
            print(f"{drafted} notes to hiring managers drafted")
    except (NoteError, OSError) as error:
        log_failure(DATA, "hunt draft-notes", error, lambda: datetime.now(UTC))
        print(f"hunt draft-notes: {error}", file=sys.stderr)
    return 0


def run_note_draft(command: list[str]) -> None:
    run_claude(command, ROOT, skill=NOTE_DRAFT_SKILL, error=NoteError)


def run_note_send(command: list[str]) -> None:
    run_claude(command, ROOT, skill=NOTE_SEND_SKILL, error=NoteError)


def notes_daily() -> int:
    """The daily cap of the settings; with settings that cannot be read, 0: they may turn
    sending off, so none goes until they are mended."""
    try:
        return read_settings(DATA / "settings.json").linkedin_notes_daily
    except SettingsError:
        return 0


def draft_notes_step() -> int:
    # With sending turned off (0 a day) no note could go, so none is drafted or asked about.
    if notes_daily() == 0:
        return 0
    notes, jobs = Notes(DATA / "jobs.db"), JobsList(DATA / "jobs.db")
    collect(notes, jobs, ROOT)
    return draft_notes(notes, jobs, ROOT, run=run_note_draft)


def _draft_notes(args: argparse.Namespace) -> int:
    print(f"{draft_notes_step()} notes to hiring managers drafted")
    return 0


def _note_went(args: argparse.Namespace) -> int:
    try:
        note = went(Notes(DATA / "jobs.db"), args.note)
    except NoteError as error:
        print(f"hunt note-went: {error}", file=sys.stderr)
        return 1
    print(f"the note to {note.name} is sent")
    return 0


def _notes(args: argparse.Namespace) -> int:
    notes = Notes(DATA / "jobs.db")
    decision = "send" if args.command == "send-notes" else "skip"
    chosen = args.numbers
    try:
        if chosen == ["all"]:
            chosen = []
        elif any(not number.isdigit() for number in chosen):
            raise NoteError("give the notes' numbers, or all")
        if chosen or args.command == "skip-notes" or args.numbers == ["all"]:
            decided = decide_notes(notes, decision, [int(number) for number in chosen] or None)
            for note in decided:
                print(f"{'approved' if decision == 'send' else 'skipped'} {note.id}: the note to {note.name}")
    except NoteError as error:
        print(f"hunt {args.command}: {error}", file=sys.stderr)
        return 1
    if decision == "skip":
        return 0
    try:
        daily = read_settings(DATA / "settings.json").linkedin_notes_daily
    except SettingsError as error:
        print(f"hunt {args.command}: data/settings.json: {error}; no note is sent until it is mended",
              file=sys.stderr)
        return 1
    lock = LinkedInLock(DATA, pause=note_pause)
    print(send_notes(notes, Threads(DATA / "jobs.db"), JobsList(DATA / "jobs.db"), ROOT, run=run_note_send,
                     lock=lock, sleep=time.sleep, daily=daily))
    return 0


def _named(job: Job) -> str:
    return f"{job.title} at {job.company}"


def _answer(args: argparse.Namespace) -> int:
    jobs = JobsList(DATA / "jobs.db")
    try:
        if args.command == "answer":
            job = answer(jobs, ROOT, args.job, args.text)
        elif args.command == "applied":
            job = applied(jobs, args.job)
        else:
            job = resume_job(jobs, args.job)
    except AnswerError as error:
        print(f"hunt {args.command}: {error}", file=sys.stderr)
        return 1
    if args.command == "applied":
        print(f"job {job.id} ({_named(job)}) is applied to")
        return 0
    done = "saved the answer; " if args.command == "answer" else ""
    print(f"{done}job {job.id} ({_named(job)}) goes on at the next hunt apply")
    return 0


def _waits_for(job: Job) -> str:
    if job.state == "picked":
        return "resume not written yet"
    if job.state == "applying":
        return "being applied to"
    if job.kind in ("company_site", "easy_apply"):
        return "resume ready, for the next hunt apply"
    return "resume ready; the way to apply is not known"


def _notes_status(notes: Notes) -> list[str]:
    lines = []
    sent = notes.in_states("sent")
    if sent:
        lines.append(f"Notes to hiring managers sent ({len(sent)}):")
        lines += [f"  {note.id}. {note.name} (job {note.job})" for note in sent]
    waiting = notes.in_states("drafted", "approved", "unsure")
    if waiting:
        lines.append(f"Notes to hiring managers waiting ({len(waiting)}):")
        lines += [f"  {note.id}. {note.name} (job {note.job}): "
                  + {"drafted": "for your answer", "approved": "approved, for the next sending"}.get(
                      note.state, f"needs {note.state_reason}") for note in waiting]
    failed = notes.in_states("failed")
    if failed:
        lines.append(f"Notes to hiring managers failed ({len(failed)}):")
        lines += [f"  {note.id}. {note.name} (job {note.job}): {note.state_reason}" for note in failed]
    if (why := stopped_today(DATA, notes.now())) is not None:
        lines.append(f"Sending notes stopped for today: {why}")
    return lines


def _status(args: argparse.Namespace) -> int:
    jobs = JobsList(DATA / "jobs.db")
    groups = [
        ("Applied", jobs.in_states("applied"), lambda job: ""),
        ("Paused", jobs.in_states("paused"), lambda job: f": needs {job.state_reason} (then: {then(job)})"),
        ("Failed", jobs.in_states("failed"), lambda job: f": {job.state_reason}"),
        ("Waiting", jobs.in_states("picked", "resume_ready", "applying"), lambda job: f": {_waits_for(job)}"),
    ]
    if not any(listed for _, listed, _ in groups):
        print("No picked jobs yet.")
    for name, listed, why in groups:
        if listed:
            print(f"{name} ({len(listed)}):")
        for job in listed:
            print(f"  {job.id}. {_named(job)}{why(job)}")
    for line in _notes_status(Notes(DATA / "jobs.db")):
        print(line)
    return 0


def _morning(args: argparse.Namespace) -> int:
    try:
        print(morning(JobsList(DATA / "jobs.db"), DATA, find=_find_and_tell, score=_score_every_job,
                      start=lambda: start_session(ROOT), running=session_running, threads=Threads(DATA / "jobs.db"),
                      draft=draft_step, notes=Notes(DATA / "jobs.db"), notes_daily=notes_daily(),
                      draft_notes=draft_notes_step))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Today's session did not start, so the next morning's questions tell it.
        log_failure(DATA, "hunt morning", error, lambda: datetime.now(UTC))
        print(f"hunt morning: {error}", file=sys.stderr)
        return 1
    return 0


def _questions(args: argparse.Namespace) -> int:
    print(questions(JobsList(DATA / "jobs.db"), DATA, Threads(DATA / "jobs.db"), Notes(DATA / "jobs.db"),
                    notes_daily()), end="")
    return 0


def run_check(command: list[str]) -> None:
    run_claude(command, ROOT, skill=ALERTS_SKILL, error=ReplyError)


def run_draft(command: list[str]) -> None:
    run_claude(command, ROOT, skill=DRAFT_SKILL, error=ReplyError)


def run_read(command: list[str]) -> None:
    run_claude(command, ROOT, skill=READ_SKILL, error=ReplyError)


def run_send(command: list[str]) -> None:
    run_claude(command, ROOT, skill=SEND_SKILL, error=ReplyError)


def draft_step() -> int:
    return draft_replies(Threads(DATA / "jobs.db"), ROOT, run=run_draft, fetch=fetch_page)


def linkedin_step() -> int:
    return read_linkedin(Threads(DATA / "jobs.db"), JobsList(DATA / "jobs.db"), ROOT, run=run_read,
                         lock=LinkedInLock(DATA), sleep=time.sleep)


def _told_once(step: str, error: object) -> None:
    """A step of the watch that fails is told once, not every 20 minutes, until it works again."""
    marker = DATA / f"{step.removeprefix('hunt ').replace(' ', '-')}.failed"
    if not marker.exists():
        log_failure(DATA, step, error, lambda: datetime.now(UTC))
        marker.write_text(f"{error}\n")
    print(f"{step}: {error}", file=sys.stderr)


def _worked(step: str) -> None:
    (DATA / f"{step.removeprefix('hunt ').replace(' ', '-')}.failed").unlink(missing_ok=True)


def _watch_linkedin() -> int | None:
    try:
        new = linkedin_step()
    except ReplyError as error:
        # The emails are still drafted and asked; LinkedIn is tried again at the next run.
        _told_once("hunt linkedin-messages", error)
        return None
    _worked("hunt linkedin-messages")
    return new


def _watch(args: argparse.Namespace) -> int:
    jobs, threads = JobsList(DATA / "jobs.db"), Threads(DATA / "jobs.db")
    try:
        said = watch(threads, jobs, DATA, read_settings(DATA / "settings.json"), check=run_check,
                     read_linkedin=_watch_linkedin, draft=draft_step, tell=lambda told: tell_session(ROOT))
    except (SettingsError, ReplyError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        _told_once("hunt watch", error)
        return 1
    _worked("hunt watch")
    if said:
        print(said)
    return 0


def _linkedin_messages(args: argparse.Namespace) -> int:
    try:
        new = linkedin_step()
    except ReplyError as error:
        print(f"hunt linkedin-messages: {error}", file=sys.stderr)
        return 1
    print(f"{new} new LinkedIn messages")
    return 0


def _draft_replies(args: argparse.Namespace) -> int:
    print(f"{draft_step()} replies drafted")
    return 0


def _reply(args: argparse.Namespace) -> int:
    threads = Threads(DATA / "jobs.db")
    try:
        if args.command == "send-reply":
            thread = send_reply(threads, ROOT, args.thread, run=run_send, lock=LinkedInLock(DATA), sleep=time.sleep)
        elif args.command == "edit-reply":
            thread = edit_reply(threads, ROOT, args.thread, args.text)
        else:
            thread = skip_reply(threads, args.thread)
    except ReplyError as error:
        print(f"hunt {args.command}: {error}", file=sys.stderr)
        return 1
    if thread.state == "sent":
        print(f"sent the reply to {thread.sender}")
    elif thread.state == "unsure":
        print(f"the reply to {thread.sender} may not have gone: needs {thread.state_reason}")
        return 1
    elif thread.state == "drafted" and args.command == "send-reply":
        print(f"the reply to {thread.sender} was not sent: {thread.state_reason}")
        return 1
    elif thread.state == "skipped":
        print(f"closed the thread of {thread.sender} without a reply")
    else:
        print(f"the reply to {thread.sender} is now:\n{thread.reply}")
    return 0


def _notify(args: argparse.Namespace) -> int:
    try:
        said = notify(args.text, DATA, post=post_pushover)
    except (SettingsError, NotifyError) as error:
        print(f"hunt notify: {error}", file=sys.stderr)
        return 1
    if said:
        print(said)
    return 0


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as missing:
        raise TimerError(f"{command[0]} is not installed or not on the PATH") from missing


def _timer(args: argparse.Namespace) -> int:
    try:
        action = args.action or "status"
        # Turning it off needs no hour, so broken settings never keep the timer running.
        settings = None if action == "off" else read_settings(DATA / "settings.json")
        said = timer(action, platform.system(), Path.home(), ROOT, settings.morning_hour if settings else 0,
                     run=run_command, uv=shutil.which("uv") or "uv", path=os.environ.get("PATH", ""), uid=os.getuid(),
                     watch_until=settings.summary_hour if settings and action == "on" else None)
    except (SettingsError, TimerError) as error:
        print(f"hunt timer: {error}", file=sys.stderr)
        return 1
    print(said)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hunt", description="Every mechanical step of the job hunt.")
    commands = parser.add_subparsers(dest="command", required=True)
    career = commands.add_parser("career", help="clean a career export into data/career.json")
    career.add_argument("export", nargs="?", default=str(DATA / "profile.json"))
    career.set_defaults(run=_career)
    resume = commands.add_parser("resume", help="build resume.pdf from a job folder's resume-content.json")
    resume.add_argument("folder")
    resume.set_defaults(run=_resume)
    finder = commands.add_parser(
        "find",
        help="read the job alert emails since the last run into data/jobs.db (one claude -p on Haiku with the "
        "read-job-alerts skill, Gmail read-only), search LinkedIn signed out and every company's public job board "
        "in data/companies.db (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable), save each new job's "
        "page in jobs/<id>/posting.md and print, for each source, how many jobs it found and how many were new",
        description="Every source in turn; one that fails is named and the others go on. The alert emails and "
        "LinkedIn's search (each title in each area, and once remote unless the settings exclude it; at most 5 pages "
        "a search, 2 to 5 seconds apart, a 429 or 999 ends LinkedIn for the run) read what came since their own "
        "last run. Every company's public board (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable) is "
        "read in the first run of a day, and in the later runs only the boards that posted in the last week; at "
        "most 4 requests at a time to one host. Once a week the company index is filled from Common Crawl first "
        "(hunt companies), and a board an alert's link leads to joins it.",
    )
    finder.set_defaults(run=_find)
    indexing = commands.add_parser(
        "companies", help="fill data/companies.db with every company whose job board on Greenhouse, Lever, Ashby, "
        "Workday, SmartRecruiters or Workable is in the latest Common Crawl (hunt find does it once a week)")
    indexing.set_defaults(run=_companies)
    scoring = commands.add_parser(
        "score",
        help="score the new jobs from 1 to 10 with one line of reason (one claude -p on Haiku with the score-jobs "
        "skill, reading only jobs/ and data/career.json) and move them to scored",
    )
    scoring.set_defaults(run=_score)
    listing = commands.add_parser(
        "list", help="print the scored jobs not yet picked or skipped, best first, numbered for hunt pick and skip")
    listing.set_defaults(run=_list)
    pick = commands.add_parser("pick", help="pick jobs by their number on the last list, or all scored above a score")
    pick.add_argument("numbers", nargs="*", type=int)
    pick.add_argument("--above", type=int, metavar="SCORE", help="pick every job of the last list scored more than this")
    pick.set_defaults(run=_decide)
    skip = commands.add_parser("skip", help="skip jobs by their number on the last list")
    skip.add_argument("numbers", nargs="*", type=int)
    skip.set_defaults(run=_decide)
    applying = commands.add_parser(
        "apply",
        help="write a resume for every picked job (claude -p with the write-resume skill, all at once), then apply "
        "to the company-site jobs, up to three at once (claude --chrome -p with the apply-site skill), and to the "
        "Easy Apply jobs one at a time, three to six minutes apart (claude --chrome -p with the apply-linkedin skill)",
    )
    applying.set_defaults(run=_apply)
    answering = commands.add_parser(
        "answer", help="save the answer to a paused job's question in the career's form answers and put it back")
    answering.add_argument("job", type=int, help="the job's number, as hunt status prints it")
    answering.add_argument("text")
    answering.set_defaults(run=_answer)
    resuming = commands.add_parser(
        "resume-job", help="put a paused job back once its CAPTCHA, sign-in or question is done in Chrome")
    resuming.add_argument("job", type=int, help="the job's number, as hunt status prints it")
    resuming.set_defaults(run=_answer)
    sent = commands.add_parser(
        "applied", help="end a paused job as applied once Chrome shows its application was sent")
    sent.add_argument("job", type=int, help="the job's number, as hunt status prints it")
    sent.set_defaults(run=_answer)
    status = commands.add_parser("status", help="print the picked jobs: applied, paused (what each needs), failed "
                                 "(why) and waiting")
    status.set_defaults(run=_status)
    starting = commands.add_parser(
        "morning",
        help="the timer's run: hunt find, hunt score when anything is new, then, when anything waits for the user, "
        "a fresh job-hunt Claude Code session with Remote Control in tmux (ending yesterday's) that asks the "
        "questions; a later run of the day starts one only when none runs; a failed step goes to "
        "data/morning.log and is told once")
    starting.set_defaults(run=_morning)
    asking = commands.add_parser(
        "questions", help="print what waits for the user, in the order to ask: a morning failure, new jobs "
        "(numbered for hunt pick), paused applications")
    asking.set_defaults(run=_questions)
    noting = commands.add_parser(
        "draft-notes", help="draft a note of at most 300 characters to the poster of each LinkedIn job applied to "
        "whom a free Message reaches (claude -p with the draft-note skill); sends nothing. hunt apply runs it at "
        "its end")
    noting.set_defaults(run=_draft_notes)
    sending_notes = commands.add_parser(
        "send-notes", help="on the person's answer to the notes question: approve all the notes asked about, or "
        "those numbered (the others are skipped), then send the approved ones by the free Message button, one at "
        "a time under the LinkedIn lock, one to three minutes apart, at most linkedin_notes_daily a day "
        "(claude --chrome -p with the send-note skill); without numbers, send those approved already")
    sending_notes.add_argument("numbers", nargs="*", help="the notes' numbers as hunt questions prints them, or all")
    sending_notes.set_defaults(run=_notes)
    skipping_notes = commands.add_parser(
        "skip-notes", help="skip the notes asked about, all of them or those numbered (the others wait to be asked "
        "again)")
    skipping_notes.add_argument("numbers", nargs="*", help="the notes' numbers as hunt questions prints them")
    skipping_notes.set_defaults(run=_notes)
    note_went = commands.add_parser(
        "note-went", help="end a note that may have gone as sent, once LinkedIn shows it in the conversation")
    note_went.add_argument("note", type=int, help="the note's number, as hunt questions prints it")
    note_went.set_defaults(run=_note_went)
    pushing = commands.add_parser(
        "notify", help="push one question with Pushover, linking to the day's session, when data/settings.json "
        "has pushover keys")
    pushing.add_argument("text")
    pushing.set_defaults(run=_notify)
    timing = commands.add_parser(
        "timer", help="turn the morning run on or off at the settings' morning hour, with two later runs at 12:00 "
        "and 16:00 when they come after it (systemd user timer on Linux, launchd agent on macOS); without on or off, "
        "say its state")
    timing.add_argument("action", nargs="?", choices=["on", "off"])
    timing.set_defaults(run=_timer)
    watching = commands.add_parser(
        "watch", help="the watch timer's run, every 20 minutes from the morning hour to the summary hour: one "
        "claude -p on Haiku looks for recruiters' emails and LinkedIn's message notifications, LinkedIn is read "
        "only after one (or in the morning and at mid-day), new messages get a drafted reply and the chat asks")
    watching.set_defaults(run=_watch)
    inbox = commands.add_parser(
        "linkedin-messages", help="read the new recruiter messages on LinkedIn (claude --chrome -p with the "
        "read-linkedin-messages skill, under the LinkedIn lock); sends nothing")
    inbox.set_defaults(run=_linkedin_messages)
    drafting = commands.add_parser(
        "draft-replies", help="draft a reply to each recruiter's message not yet answered (claude -p with the "
        "draft-reply skill), saved in jobs/replies/<n>/ and, for an email, as a Gmail draft; sends nothing")
    drafting.set_defaults(run=_draft_replies)
    sending = commands.add_parser(
        "send-reply", help="send a drafted reply as it stands, on the person's yes: the email through Gmail, a "
        "LinkedIn message in Chrome under the LinkedIn lock (claude -p with the send-reply skill)")
    sending.add_argument("thread", type=int, help="the message's number, as hunt questions prints it")
    sending.set_defaults(run=_reply)
    editing = commands.add_parser("edit-reply", help="change a drafted reply before it is sent")
    editing.add_argument("thread", type=int, help="the message's number, as hunt questions prints it")
    editing.add_argument("text")
    editing.set_defaults(run=_reply)
    skipping = commands.add_parser("skip-reply", help="close a recruiter's message without a reply")
    skipping.add_argument("thread", type=int, help="the message's number, as hunt questions prints it")
    skipping.set_defaults(run=_reply)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
