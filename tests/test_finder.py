"""`hunt find`: the alert jobs the skill read, filtered and saved with their pages.

No model and no network: the skill's run is replaced by copying `tests/finder/found-jobs.json`,
the file the skill writes for the made-up emails in `tests/finder/emails/`, and pages come from
`tests/finder/pages/`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.finder import (
    GMAIL_READ_TOOLS,
    FinderError,
    PageError,
    find,
    page_text_and_kind,
    place_kept,
    read_found,
    skill_command,
    title_matches,
)
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.settings import Settings, read_settings
from movable_clock import MovableClock

HERE = Path(__file__).parent / "finder"
PAGES = {
    "https://www.linkedin.com/jobs/view/4012345678": "linkedin-easy-apply.html",
    "https://www.linkedin.com/jobs/view/4012345690": "linkedin-offsite.html",
    "https://jobs.example.com/careers/1234": "company.html",
}


@pytest.fixture
def settings() -> Settings:
    return read_settings(HERE / "settings.json")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


def _fetch(link: str) -> tuple[str, str]:
    if link not in PAGES:
        raise PageError("HTTP 403")
    return link, (HERE / "pages" / PAGES[link]).read_text()


class FakeSkill:
    """Stands in for the `claude -p` run: writes what the skill wrote for the made-up emails."""

    def __init__(self, found: Path, source: Path = HERE / "found-jobs.json") -> None:
        self.found, self.source, self.commands = found, source, []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(command)
        shutil.copy(self.source, self.found)


def _job(**change: object) -> FoundJob:
    job = dict(link="https://www.linkedin.com/jobs/view/1", title="QA Automation Engineer", company="Example Labs",
               place="Durham, NC", remote="on_site", board="linkedin", email_id="1",
               email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")
    return FoundJob(**{**job, **change})


@pytest.mark.parametrize(
    ("title", "matches"),
    [
        ("QA Automation Engineer", True),
        ("Senior QA Automation Engineer", True),
        ("Automation Engineer, QA (Contract)", True),
        ("qa-automation engineer", True),
        ("SDET II", True),
        ("Sr. SDET", True),
        ("QA Engineer", False),
        ("Product Manager, QA Tools", False),
        ("Software Engineer in Test", False),
        # A settings word must be a whole word of the title, not a part of one.
        ("SDETs Lead", False),
    ],
)
def test_a_title_matches_when_it_holds_every_word_of_a_settings_title(title: str, matches: bool) -> None:
    assert title_matches(title, ["QA Automation Engineer", "SDET"]) is matches


@pytest.mark.parametrize(
    ("remote_setting", "job_remote", "area", "kept"),
    [
        ("include", "remote", None, True),
        ("include", "on_site", "Raleigh-Durham-Chapel Hill", True),
        ("include", "hybrid", "Raleigh-Durham-Chapel Hill", True),
        ("include", "on_site", None, False),
        ("exclude", "remote", "Raleigh-Durham-Chapel Hill", False),
        ("exclude", "on_site", "Raleigh-Durham-Chapel Hill", True),
        ("exclude", "hybrid", None, False),
        ("only", "remote", None, True),
        ("only", "on_site", "Raleigh-Durham-Chapel Hill", False),
        ("only", "hybrid", "Raleigh-Durham-Chapel Hill", False),
    ],
)
def test_the_area_and_remote_settings_keep_or_drop_a_job(
    settings: Settings, remote_setting: str, job_remote: str, area: str | None, kept: bool
) -> None:
    assert place_kept(_job(remote=job_remote, area=area), replace(settings, remote=remote_setting)) is kept


def test_an_area_the_settings_do_not_name_counts_as_none(tmp_path: Path, settings: Settings) -> None:
    found = tmp_path / "found.json"
    found.write_text(json.dumps({"emails": 1, "jobs": [
        {**_job().__dict__, "area": "raleigh-durham-chapel hill"},
        {**_job().__dict__, "link": "https://www.linkedin.com/jobs/view/2", "area": "Research Triangle"},
    ]}))
    emails, jobs, problems = read_found(found, settings)
    assert [job.area for job in jobs] == ["Raleigh-Durham-Chapel Hill", None]
    assert (emails, problems) == (1, [])


def test_a_job_the_skill_wrote_wrongly_is_left_out_and_named(tmp_path: Path, settings: Settings) -> None:
    found = tmp_path / "found.json"
    good = _job().__dict__
    found.write_text(json.dumps({"emails": 2, "jobs": [
        good, {**good, "remote": "maybe"}, {**good, "title": ""}, {k: v for k, v in good.items() if k != "link"}]}))
    emails, jobs, problems = read_found(found, settings)
    assert (emails, len(jobs)) == (2, 1)
    assert len(problems) == 3 and "remote" in problems[0] and "title" in problems[1] and "link" in problems[2]


@pytest.mark.parametrize("content", ["", "[]", '{"jobs": []}', '{"emails": 1, "jobs": {}}'])
def test_a_found_file_that_is_not_the_skills_shape_fails_the_run(tmp_path: Path, settings: Settings, content: str) -> None:
    found = tmp_path / "found.json"
    found.write_text(content)
    with pytest.raises(FinderError):
        read_found(found, settings)


def test_a_linkedin_page_with_easy_apply_is_easy_apply_and_its_text_is_the_description() -> None:
    text, kind = page_text_and_kind(*_fetch("https://www.linkedin.com/jobs/view/4012345678"))
    assert kind == "easy_apply"
    assert text.startswith("About the job\nExample Labs builds lab software.")
    assert "Write end-to-end tests with Playwright and Python." in text
    assert "window.tracking" not in text and "Sign in" not in text and "LinkedIn Corporation" not in text


def test_a_linkedin_page_that_applies_on_another_site_is_a_company_site() -> None:
    text, kind = page_text_and_kind(*_fetch("https://www.linkedin.com/jobs/view/4012345690"))
    assert (kind, text) == ("company_site", "Initech Example is hiring an SDET to test its payments API.\nRemote in the United States.")


def test_a_company_page_is_a_company_site_without_scripts_or_page_furniture() -> None:
    text, kind = page_text_and_kind(*_fetch("https://jobs.example.com/careers/1234"))
    assert kind == "company_site"
    assert text.startswith("QA Automation Engineer, Robot Fleet\nSan Jose, CA · On-site")
    assert "JavaScript" not in text and "©" not in text


def test_a_sign_in_page_is_a_page_that_cannot_be_read() -> None:
    html = (HERE / "pages" / "linkedin-sign-in.html").read_text()
    with pytest.raises(PageError, match="sign in"):
        page_text_and_kind("https://www.linkedin.com/authwall?trk=x", html)
    with pytest.raises(PageError, match="sign in"):
        page_text_and_kind("https://www.linkedin.com/jobs/view/4012345678", html)


def test_the_skill_runs_on_haiku_with_only_gmails_read_tools_and_its_one_file(tmp_path: Path) -> None:
    since = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
    command = skill_command(since, ["Raleigh-Durham-Chapel Hill"], Path("data/found-jobs.json"))
    assert command[:3] == ["claude", "-p", command[2]]
    assert command[2].startswith("/read-job-alerts ")
    assert json.loads(command[2].removeprefix("/read-job-alerts ")) == {
        "after": int(since.timestamp()),
        "since": "2026-11-04T07:00:00+00:00",
        "areas": ["Raleigh-Durham-Chapel Hill"],
        "out": "data/found-jobs.json",
    }
    assert command[command.index("--model") + 1] == "claude-haiku-5-5"
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert allowed == [*GMAIL_READ_TOOLS, "Edit(./data/found-jobs.json)"]
    assert all(tool.startswith("mcp__claude_ai_Gmail__") for tool in GMAIL_READ_TOOLS)
    assert not any(word in tool for tool in GMAIL_READ_TOOLS for word in ("send", "draft", "label", "trash", "spam", "forward", "reply"))


def test_find_saves_the_new_jobs_with_their_pages_and_says_how_many(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "data" / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "data" / "found-jobs.json")
    summary = find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    assert (summary.emails, summary.jobs, summary.new) == (4, 8, 4)
    assert str(summary) == "4 emails, 8 jobs, 4 new"
    saved = {job.title: job for job in jobs.all()}
    # Left out: the product manager and the engineer in test (titles), Austin (no area, on site),
    # and the second alert's copy of the Example Labs job.
    assert sorted(saved) == ["QA Automation Engineer", "QA Automation Engineer, Robot Fleet",
                             "SDET II", "Senior QA Automation Engineer"]
    assert {job.state for job in saved.values()} == {"new"}
    lab = saved["Senior QA Automation Engineer"]
    assert (lab.link, lab.kind, lab.email_id, lab.page_problem) == (
        "https://www.linkedin.com/jobs/view/4012345678", "easy_apply", "18c0ffee00000001", None)
    posting = (tmp_path / "jobs" / str(lab.id) / "posting.md").read_text()
    assert posting.startswith("# Senior QA Automation Engineer\n\nExample Labs · Durham, NC · hybrid\n"
                              "https://www.linkedin.com/jobs/view/4012345678\n\nAbout the job\n")
    assert lab.description in posting
    assert (saved["SDET II"].kind, saved["QA Automation Engineer, Robot Fleet"].kind) == ("company_site", "company_site")
    assert jobs.last_run() == clock.now
    since = json.loads(skill.commands[0][2].removeprefix("/read-job-alerts "))["since"]
    assert since == (clock.now - timedelta(days=14)).isoformat()


def test_a_page_that_cannot_be_read_is_saved_with_what_the_alert_gave_and_the_reason(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    indeed = next(job for job in jobs.all() if job.board == "indeed")
    assert (indeed.link, indeed.page_problem, indeed.kind, indeed.state) == (
        "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b", "HTTP 403", "company_site", "new")
    posting = (tmp_path / "jobs" / str(indeed.id) / "posting.md").read_text()
    assert "Umbrella Example · Morrisville, NC · on site" in posting
    assert "The job page could not be read (HTTP 403)" in posting


def test_a_linkedin_page_that_cannot_be_read_leaves_the_kind_empty(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def refuse(link: str) -> tuple[str, str]:
        raise PageError("HTTP 999")

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=refuse, clock=clock)
    linkedin = [job for job in jobs.all() if job.board == "linkedin"]
    assert [(job.kind, job.page_problem) for job in linkedin] == [(None, "HTTP 999")] * 2


def test_the_next_run_reads_from_when_the_last_finished_run_started_and_finds_nothing_twice(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    first = clock.now
    clock.advance(days=1)
    summary = find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    assert (summary.jobs, summary.new) == (8, 0)
    assert json.loads(skill.commands[1][2].removeprefix("/read-job-alerts "))["since"] == first.isoformat()


def test_a_run_that_writes_no_file_fails_and_the_next_run_reads_from_the_same_time(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    found = tmp_path / "found-jobs.json"
    # A file left by an earlier run is not read as this run's.
    found.write_text((HERE / "found-jobs.json").read_text())
    with pytest.raises(FinderError, match="wrote no"):
        find(settings, jobs, tmp_path / "jobs", found, run=lambda command: None, fetch=_fetch, clock=clock)
    assert (jobs.last_run(), jobs.all()) == (None, [])


def test_hunt_find_prints_one_line_and_a_missing_settings_file_names_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DATA", tmp_path / "data")
    assert cli.main(["find"]) == 1
    assert "/setup" in capsys.readouterr().err

    (tmp_path / "data").mkdir()
    shutil.copy(HERE / "settings.json", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(cli, "run_skill", FakeSkill(tmp_path / "data" / "found-jobs.json"))
    monkeypatch.setattr(cli, "fetch_page", _fetch)
    assert cli.main(["find"]) == 0
    assert capsys.readouterr().out == "4 emails, 8 jobs, 4 new\n"
    assert (tmp_path / "jobs").is_dir()


def test_hunt_help_names_the_find_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    # Wide enough that the help is not wrapped inside a hyphenated word.
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert "find" in help_text and "read-job-alerts" in help_text
