"""`hunt`: every mechanical step of the job hunt."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from job_hunt.apply import (
    LINKEDIN_SKILL, RESUME_SKILL, SITE_SKILL, AnswerError, ApplyError, answer, apply, applied, resume_job,
)
from job_hunt.career import (
    STANDING, Career, CareerError, career_from_export, career_from_resume, read_career, resume_text,
    with_ready_answers, write_career,
)
from job_hunt.doctor import Machine, doctor, remote_desktop_ready
from job_hunt.finder import FinderError, fetch_page, find, run_claude
from job_hunt.finder import Summary as FindSummary
from job_hunt.jobs import Job, JobsList, PickError
from job_hunt.morning import morning, start_session
from job_hunt.notify import NotifyError, notify, post_pushover
from job_hunt.questions import job_line, log_failure, questions, then
from job_hunt.resume import ContentError, write_pdf
from job_hunt.scoring import SKILL as SCORE_SKILL, ScoreError, score
from job_hunt.scoring import Summary as ScoreSummary
from job_hunt.settings import SettingsError, read_settings
from job_hunt.timer import TimerError, timer

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CAREER = DATA / "career.json"


def _career(args: argparse.Namespace) -> int:
    export = json.loads(Path(args.export).read_text())
    career = career_from_export(export)
    write_career(career, CAREER)
    print(f"{len(career.jobs)} jobs, {len(career.education)} schools, {len(career.skills)} skills -> {CAREER}")
    for job in career.jobs:
        print(f"  {job.id}: {job.title} at {job.company}, {job.start_date} to {job.end_date or 'now'}")
    return 0


def _shown(career: Career) -> list[str]:
    """What was read from a resume, for the person to correct before it is saved."""
    person = career.person
    contacts = ", ".join(str(person[key]) for key in ("email", "phone", "location") if person.get(key))
    lines = [f"{person.get('name')}: {contacts}"]
    lines += [f"  link: {link.get('label') or 'link'} {link.get('url')}" for link in person.get("links") or []]
    lines.append(f"Jobs ({len(career.jobs)}):")
    for job in career.jobs:
        where = f", {job.location}" if job.location else ""
        lines.append(f"  {job.id}: {job.title} at {job.company}{where}, {job.start_date} to {job.end_date or 'now'}"
                     f" ({len(job.material)} {'line' if len(job.material) == 1 else 'lines'} of what was done)")
    lines.append(f"Schools ({len(career.education)}):")
    for school in career.education:
        degree = f"{school.degree}, {school.field_of_study}" if school.field_of_study else school.degree
        years = f", {school.start_date or '?'} to {school.end_date or '?'}" if school.start_date or school.end_date else ""
        lines.append(f"  {degree} at {school.school}{years}")
    lines.append(f"Certifications: {', '.join(career.certifications) or 'none'}")
    lines.append(f"Skills ({len(career.skills)}): {', '.join(career.skills) or 'none'}")
    lines.append(f"Titles: {', '.join(career.titles) or 'none'}")
    lines.append(f"Summary: {career.summary or 'none'}")
    return lines


def _career_from_resume(args: argparse.Namespace) -> int:
    try:
        draft = json.loads(Path(args.draft).read_text())
        kept = read_career(CAREER) if CAREER.exists() else None
        career = career_from_resume(draft, kept=kept)
    except (OSError, json.JSONDecodeError, CareerError) as error:
        print(f"hunt career-from-resume: {error}", file=sys.stderr)
        return 1
    print("\n".join(_shown(career)))
    if args.save:
        CAREER.parent.mkdir(parents=True, exist_ok=True)
        write_career(career, CAREER)
        print(f"saved to {CAREER}")
    else:
        print("not saved yet: run it again with --save once it is right")
    return 0


def _form_answers(args: argparse.Namespace) -> int:
    try:
        answers = json.loads(args.answers)
        if not isinstance(answers, dict):
            raise CareerError("give the answers as one JSON object")
        career = with_ready_answers(read_career(CAREER), answers)
    except (OSError, json.JSONDecodeError, CareerError) as error:
        print(f"hunt form-answers: {error}", file=sys.stderr)
        return 1
    write_career(career, CAREER)
    ready = career.form_answers["ready"]
    print("standing answers: " + ", ".join(f"{key} {ready[key]}" for key in STANDING if key in ready))
    return 0


def _resume_text(args: argparse.Namespace) -> int:
    try:
        print(resume_text(Path(args.file)))
    except (OSError, CareerError) as error:
        print(f"hunt resume-text: {error}", file=sys.stderr)
        return 1
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


def find_step() -> FindSummary:
    summary = find(
        read_settings(DATA / "settings.json"),
        JobsList(DATA / "jobs.db"),
        ROOT / "jobs",
        DATA / "found-jobs.json",
        run=run_skill,
        fetch=fetch_page,
    )
    for problem in summary.problems:
        print(f"hunt find: left out {problem}", file=sys.stderr)
    print(summary)
    return summary


def _find_and_tell() -> None:
    """For the morning: a job the finder left out is a failure it tells, not only a line."""
    summary = find_step()
    if summary and summary.problems:
        raise FinderError(f"left out {'; '.join(summary.problems)}")


def _find(args: argparse.Namespace) -> int:
    try:
        find_step()
    except (SettingsError, FinderError) as error:
        print(f"hunt find: {error}", file=sys.stderr)
        return 1
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
    return 0


def _morning(args: argparse.Namespace) -> int:
    try:
        print(morning(JobsList(DATA / "jobs.db"), DATA, find=_find_and_tell, score=_score_every_job,
                      start=lambda: start_session(ROOT)))
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Today's session did not start, so the next morning's questions tell it.
        log_failure(DATA, "hunt morning", error, lambda: datetime.now(UTC))
        print(f"hunt morning: {error}", file=sys.stderr)
        return 1
    return 0


def _questions(args: argparse.Namespace) -> int:
    print(questions(JobsList(DATA / "jobs.db"), DATA, remote_desktop=remote_desktop_ready(this_machine())), end="")
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
        hour = 0 if action == "off" else read_settings(DATA / "settings.json").morning_hour
        said = timer(action, platform.system(), Path.home(), ROOT, hour, run=run_command,
                     uv=shutil.which("uv") or "uv", path=os.environ.get("PATH", ""), uid=os.getuid())
    except (SettingsError, TimerError) as error:
        print(f"hunt timer: {error}", file=sys.stderr)
        return 1
    print(said)
    return 0


def _run_quietly(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        # `claude mcp list` checks every connector, which takes a while.
        return subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _timer_status() -> str:
    try:
        hour = read_settings(DATA / "settings.json").morning_hour
    except SettingsError:
        raise RuntimeError("it is set once data/settings.json is") from None
    return timer("status", platform.system(), Path.home(), ROOT, hour, run=run_command,
                 uv=shutil.which("uv") or "uv", path=os.environ.get("PATH", ""), uid=os.getuid())


def this_machine() -> Machine:
    return Machine(system=platform.system(), home=Path.home(), root=Path("/"), data=DATA,
                   python=sys.version_info[:2], run=_run_quietly, which=shutil.which, timer_status=_timer_status)


def _settings(args: argparse.Namespace) -> int:
    path = DATA / "settings.json"
    temporary: Path | None = None
    try:
        given = json.loads(args.settings)
        if not isinstance(given, dict):
            raise SettingsError("give the settings as one JSON object")
        if "pushover" not in given and path.exists():
            # Keys given once stay: the search is changed without typing them again.
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                if (keys := json.loads(path.read_text()).get("pushover")) is not None:
                    given["pushover"] = keys
        DATA.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=DATA, prefix=".settings.json.")
        temporary = Path(name)
        with os.fdopen(descriptor, "w") as out:
            out.write(json.dumps(given, indent=2, ensure_ascii=False) + "\n")
        # Saved only as the hunt reads it: what it cannot read never replaces what it can.
        settings = read_settings(temporary)
        os.replace(temporary, path)
    except (json.JSONDecodeError, SettingsError) as error:
        said = str(error).replace(str(temporary), str(path)) if temporary else str(error)
        print(f"hunt settings: {said}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    remote = {"include": "remote jobs too", "exclude": "no remote jobs", "only": "remote jobs only"}[settings.remote]
    salary = f"at least {settings.lowest_salary}" if settings.lowest_salary is not None else "any salary"
    print(f"saved: {', '.join(settings.titles)}; in {', '.join(settings.areas) or 'no area'}, {remote}; {salary}; "
          f"the morning run at {settings.morning_hour:02d}:00{'; Pushover keys kept' if settings.pushover else ''}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    lines = doctor(this_machine())
    for line in lines:
        print(line)
    return 1 if any(line.state == "missing" for line in lines) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hunt", description="Every mechanical step of the job hunt.")
    commands = parser.add_subparsers(dest="command", required=True)
    career = commands.add_parser("career", help="clean a career export into data/career.json")
    career.add_argument("export", nargs="?", default=str(DATA / "profile.json"))
    career.set_defaults(run=_career)
    from_resume = commands.add_parser(
        "career-from-resume", help="check a career the read-resume skill wrote from a resume and print what it "
        "holds; with --save, write it to data/career.json, keeping the career's form answers")
    from_resume.add_argument("draft")
    from_resume.add_argument("--save", action="store_true")
    from_resume.set_defaults(run=_career_from_resume)
    reading = commands.add_parser("resume-text", help="print the text of a resume, a PDF or a Word (.docx) file")
    reading.add_argument("file")
    reading.set_defaults(run=_resume_text)
    standing = commands.add_parser(
        "form-answers", help="save the standing form answers in data/career.json, as one JSON object of "
        f"{', '.join(STANDING)}")
    standing.add_argument("answers")
    standing.set_defaults(run=_form_answers)
    resume = commands.add_parser("resume", help="build resume.pdf from a job folder's resume-content.json")
    resume.add_argument("folder")
    resume.set_defaults(run=_resume)
    finder = commands.add_parser(
        "find",
        help="read the job alert emails since the last run into data/jobs.db (one claude -p on Haiku with the "
        "read-job-alerts skill, Gmail read-only), save each new job's page in jobs/<id>/posting.md and "
        "print how many emails, jobs and new jobs",
    )
    finder.set_defaults(run=_find)
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
        "questions; a failed step goes to data/morning.log and is told once")
    starting.set_defaults(run=_morning)
    asking = commands.add_parser(
        "questions", help="print what waits for the user, in the order to ask: a morning failure, new jobs "
        "(numbered for hunt pick), paused applications")
    asking.set_defaults(run=_questions)
    pushing = commands.add_parser(
        "notify", help="push one question with Pushover, linking to the day's session, when data/settings.json "
        "has pushover keys")
    pushing.add_argument("text")
    pushing.set_defaults(run=_notify)
    timing = commands.add_parser(
        "timer", help="turn the morning run on or off at the settings' morning hour (systemd user timer on Linux, "
        "launchd agent on macOS); without on or off, say its state")
    timing.add_argument("action", nargs="?", choices=["on", "off"])
    timing.set_defaults(run=_timer)
    searching = commands.add_parser(
        "settings", help="check and save what to look for in data/settings.json, as one JSON object of titles, "
        "areas, remote (include, exclude or only), lowest_salary and morning_hour; Pushover keys saved before stay")
    searching.add_argument("settings")
    searching.set_defaults(run=_settings)
    checking = commands.add_parser(
        "doctor", help="check what the hunt needs (Claude Code signed in, uv, Python 3.14, Claude in Chrome connected, "
        "the Gmail connector, the career, the settings, the timer, and optionally Chrome Remote Desktop) and print "
        "one line each: ok, or what is missing and how to add it")
    checking.set_defaults(run=_doctor)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
