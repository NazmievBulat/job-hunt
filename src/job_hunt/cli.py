"""`hunt`: every mechanical step of the job hunt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_hunt.apply import (
    RESUME_SKILL, SITE_SKILL, UNSURE, AnswerError, ApplyError, answer, apply, applied, resume_job,
)
from job_hunt.career import career_from_export, read_career, write_career
from job_hunt.finder import FinderError, fetch_page, find, run_claude
from job_hunt.jobs import Job, JobsList, PickError
from job_hunt.resume import ContentError, write_pdf
from job_hunt.scoring import SKILL as SCORE_SKILL, ScoreError, score
from job_hunt.settings import SettingsError, read_settings

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


def _find(args: argparse.Namespace) -> int:
    try:
        summary = find(
            read_settings(DATA / "settings.json"),
            JobsList(DATA / "jobs.db"),
            ROOT / "jobs",
            DATA / "found-jobs.json",
            run=run_skill,
            fetch=fetch_page,
        )
    except (SettingsError, FinderError) as error:
        print(f"hunt find: {error}", file=sys.stderr)
        return 1
    for problem in summary.problems:
        print(f"hunt find: left out {problem}", file=sys.stderr)
    print(summary)
    return 0


def run_scoring(command: list[str]) -> None:
    run_claude(command, ROOT, skill=SCORE_SKILL, error=ScoreError)


def _score(args: argparse.Namespace) -> int:
    try:
        summary = score(JobsList(DATA / "jobs.db"), ROOT, run=run_scoring)
    except ScoreError as error:
        print(f"hunt score: {error}", file=sys.stderr)
        return 1
    for job in summary.missing:
        print(f"hunt score: not scored, still new: {job}", file=sys.stderr)
    print(summary)
    return 0


KINDS = {"easy_apply": "Easy Apply", "company_site": "company site"}


def _where(job: Job) -> str:
    if job.remote == "remote":
        return "remote"
    place = job.place or "place not given"
    return f"{place}, hybrid" if job.remote == "hybrid" else place


def _list(args: argparse.Namespace) -> int:
    shown = JobsList(DATA / "jobs.db").show()
    if not shown:
        print("No scored jobs wait for your pick.")
    for number, job in enumerate(shown, start=1):
        kind = KINDS.get(job.kind or "", "way to apply not known")
        print(f"{number:>2}. {job.score:>2}  {job.title} · {job.company} · {_where(job)} · {kind}")
        print(f"        {job.score_reason}")
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


def _apply(args: argparse.Namespace) -> int:
    try:
        summary = apply(JobsList(DATA / "jobs.db"), ROOT, run_resume=run_resume, run_site=run_site)
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


def _then(job: Job) -> str:
    if job.state_reason and job.state_reason.startswith("an answer to "):
        return f'hunt answer {job.id} "<the answer>"'
    if job.state_reason and job.state_reason.startswith(UNSURE):
        return f"hunt applied {job.id} if it was sent, hunt resume-job {job.id} if not"
    return f"hunt resume-job {job.id}"


def _waits_for(job: Job) -> str:
    if job.state == "picked":
        return "resume not written yet"
    if job.state == "applying":
        return "being applied to"
    if job.kind == "company_site":
        return "resume ready, for the next hunt apply"
    if job.kind == "easy_apply":
        return "resume ready, for the LinkedIn worker"
    return "resume ready; the way to apply is not known"


def _status(args: argparse.Namespace) -> int:
    jobs = JobsList(DATA / "jobs.db")
    groups = [
        ("Applied", jobs.in_states("applied"), lambda job: ""),
        ("Paused", jobs.in_states("paused"), lambda job: f": needs {job.state_reason} (then: {_then(job)})"),
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
        "to the company-site jobs, up to three at once (claude --chrome -p with the apply-site skill)",
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
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
