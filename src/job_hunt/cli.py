"""`hunt`: every mechanical step of the job hunt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_hunt.career import career_from_export, read_career, write_career
from job_hunt.finder import FinderError, fetch_page, find, run_claude
from job_hunt.jobs import JobsList
from job_hunt.resume import ContentError, write_pdf
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
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
