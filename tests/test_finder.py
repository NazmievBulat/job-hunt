"""`hunt find`: the alert jobs the skill read, filtered and saved with their pages.

No model and no network: the skill's run is replaced by copying `tests/finder/found-jobs.json`,
the file the skill writes for the made-up emails in `tests/finder/emails/`, and pages come from
`tests/finder/pages/`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from email import message_from_string
from urllib.error import HTTPError, URLError
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt import finder
from job_hunt.finder import (
    GMAIL_READ_TOOLS,
    FinderError,
    PageError,
    fetch_page,
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


def test_a_posting_with_an_optional_sign_in_form_is_still_read() -> None:
    html = (HERE / "pages" / "linkedin-easy-apply.html").read_text().replace(
        "<nav>", '<form action="https://www.linkedin.com/uas/login-submit"><input name="session_key"></form><nav>')
    text, kind = page_text_and_kind("https://www.linkedin.com/jobs/view/4012345678", html)
    assert (kind, text.splitlines()[0]) == ("easy_apply", "About the job")


def test_a_linkedin_page_that_applies_on_another_site_is_a_company_site() -> None:
    text, kind = page_text_and_kind(*_fetch("https://www.linkedin.com/jobs/view/4012345690"))
    assert (kind, text) == ("company_site", "Initech Example is hiring an SDET to test its payments API.\nRemote in the United States.")


def test_a_company_page_is_a_company_site_without_scripts_or_page_furniture() -> None:
    text, kind = page_text_and_kind(*_fetch("https://jobs.example.com/careers/1234"))
    assert kind == "company_site"
    assert text.startswith("QA Automation Engineer, Robot Fleet\nSan Jose, CA · On-site")
    assert "JavaScript" not in text and "©" not in text


@pytest.mark.parametrize("form", ['<form action="/authwall">', "<form action='/authwall'>", "<FORM ACTION=/authwall>"])
def test_a_sign_in_page_is_a_page_that_cannot_be_read(form: str) -> None:
    html = (HERE / "pages" / "linkedin-sign-in.html").read_text().replace('<form action="/authwall">', form)
    assert form in html
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


def test_the_made_up_found_file_is_what_the_made_up_alert_emails_hold() -> None:
    # The whole email: a company alert names the company only in its sender and subject.
    emails = {path.name: path.read_text() for path in sorted((HERE / "emails").glob("*.eml"))}
    alerts = {message_from_string(text)["X-Gmail-Id"]: text for name, text in emails.items() if "recruiter" not in name}
    found = json.loads((HERE / "found-jobs.json").read_text())
    assert found["emails"] == len(alerts)
    for job in found["jobs"]:
        body = alerts[job["email_id"]]
        assert job["link"] in body and job["title"] in body and job["company"] in body


def test_a_page_with_no_text_cannot_be_read() -> None:
    with pytest.raises(PageError, match="no text"):
        page_text_and_kind("https://jobs.example.com/1", "<html><head><title>x</title></head><body> </body></html>")


def test_the_skill_may_write_only_its_own_file_when_the_path_is_absolute(tmp_path: Path) -> None:
    command = skill_command(datetime(2026, 11, 4, tzinfo=UTC), [], tmp_path / "found.json")
    # `//` starts an absolute path in a permission rule; one slash is the project folder.
    assert command[command.index("--allowedTools") + 1].split(",")[-1] == f"Edit(/{tmp_path}/found.json)"


class _Response:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body, self.content_type = body, content_type

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]

    def geturl(self) -> str:
        return "https://jobs.example.com/careers/1234"

    @property
    def headers(self):  # type: ignore[no-untyped-def]
        from email.message import Message
        headers = Message()
        headers["Content-Type"] = self.content_type
        return headers


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (HTTPError("https://jobs.example.com", 403, "Forbidden", {}, None), "HTTP 403"),  # type: ignore[arg-type]
        (URLError("Name or service not known"), "Name or service not known"),
        (TimeoutError("timed out"), "no answer in 20 seconds"),
        (ConnectionResetError("reset by peer"), "reset by peer"),
    ],
)
def test_a_fetch_that_fails_is_a_page_that_cannot_be_read(monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str) -> None:
    def fail(request: object, timeout: float) -> None:
        raise error

    monkeypatch.setattr(finder, "urlopen", fail)
    with pytest.raises(PageError, match=reason):
        fetch_page("https://jobs.example.com/careers/1234")


def test_a_page_in_a_charset_python_does_not_know_is_read_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finder, "urlopen", lambda request, timeout: _Response("<p>Café</p>".encode(), "text/html; charset=bogus"))
    assert fetch_page("https://jobs.example.com/careers/1234") == ("https://jobs.example.com/careers/1234", "<p>Café</p>")


def test_an_unexpected_error_while_reading_a_page_still_saves_the_job_and_finishes_the_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def broken(link: str) -> tuple[str, str]:
        if "4012345678" in link:
            raise ValueError("a bug in reading pages")
        return _fetch(link)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    summary = find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=broken, clock=clock)
    assert summary.new == 4 and jobs.last_run() == clock.now
    lab = next(job for job in jobs.all() if "4012345678" in job.link)
    assert lab.page_problem == "ValueError: a bug in reading pages"
    assert "could not be read (ValueError: a bug in reading pages)" in (tmp_path / "jobs" / str(lab.id) / "posting.md").read_text()


FAKE_CLAUDE = """#!{python}
import json, os, shutil, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, log)
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print("starting", file=sys.stderr)
    print("Not logged in. Run claude to sign in.", file=sys.stderr)
    sys.exit(1)
arguments = json.loads(sys.argv[2].removeprefix("/read-job-alerts "))
shutil.copy(os.environ["FAKE_CLAUDE_FOUND"], arguments["out"])
"""


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `claude` first on the PATH that records how it was started, then writes the found file."""
    folder = tmp_path / "bin"
    folder.mkdir()
    claude = folder / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.json"))
    monkeypatch.setenv("FAKE_CLAUDE_FOUND", str(HERE / "found-jobs.json"))
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    shutil.copy(HERE / "settings.json", project / "data" / "settings.json")
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    monkeypatch.setattr(cli, "fetch_page", _fetch)
    return project


def test_hunt_find_starts_claude_on_haiku_in_the_project_with_only_gmail_reads_and_its_file(
    fake_claude: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["find"]) == 0
    assert capsys.readouterr().out == "4 emails, 8 jobs, 4 new\n"
    started = json.loads((tmp_path / "claude.json").read_text())
    argv = started["argv"]
    assert Path(started["cwd"]) == fake_claude
    assert argv[0] == "-p" and argv[1].startswith("/read-job-alerts {")
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    found = fake_claude / "data" / "found-jobs.json"
    assert argv[argv.index("--allowedTools") + 1].split(",") == [*GMAIL_READ_TOOLS, f"Edit(/{found})"]
    assert len(list((fake_claude / "jobs").iterdir())) == 4


def test_hunt_find_says_why_the_skill_run_failed_and_reads_from_the_same_time_next(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    assert cli.main(["find"]) == 1
    printed = capsys.readouterr()
    assert printed.out == ""
    assert printed.err == "hunt find: the read-job-alerts run failed (1): Not logged in. Run claude to sign in.\n"
    assert JobsList(fake_claude / "data" / "jobs.db").last_run() is None


def test_hunt_find_names_what_it_left_out_apart_from_its_one_line(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = json.loads((HERE / "found-jobs.json").read_text())
    found["jobs"][1]["remote"] = "maybe"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(found))
    monkeypatch.setenv("FAKE_CLAUDE_FOUND", str(bad))
    assert cli.main(["find"]) == 0
    printed = capsys.readouterr()
    assert printed.out == "4 emails, 7 jobs, 3 new\n"
    assert printed.err == "hunt find: left out job 2 (SDET II) has remote 'maybe', not one of remote, hybrid, on_site\n"


def test_two_tracking_links_that_end_at_the_same_page_are_one_job(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    found = tmp_path / "found.json"
    job = {**_job().__dict__, "board": "glassdoor"}
    written = json.dumps({"emails": 2, "jobs": [
        {**job, "link": "https://click.example.com/t/one"}, {**job, "link": "https://click.example.com/t/two"}]})
    pages = {"https://click.example.com/t/one": "https://jobs.example.com/careers/1234?utm_source=one",
             "https://click.example.com/t/two": "https://jobs.example.com/careers/1234?utm_source=two"}

    def redirect(link: str) -> tuple[str, str]:
        return pages[link], (HERE / "pages" / "company.html").read_text()

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=lambda command: found.write_text(written),
                   fetch=redirect, clock=clock)
    assert (summary.jobs, summary.new) == (2, 1)
    assert [(each.link, each.page_link) for each in jobs.all()] == [
        ("https://click.example.com/t/one", "https://jobs.example.com/careers/1234")]


def test_a_new_job_whose_posting_was_never_written_gets_it_on_the_next_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    # As if the run had stopped after saving the rows and before writing two postings.
    lab = next(job for job in jobs.all() if "4012345678" in job.link)
    indeed = next(job for job in jobs.all() if job.board == "indeed")
    written = (tmp_path / "jobs" / str(lab.id) / "posting.md").read_text()
    for job in (lab, indeed):
        (tmp_path / "jobs" / str(job.id) / "posting.md").unlink()
    jobs.move(indeed.id, "scored")
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    assert (tmp_path / "jobs" / str(lab.id) / "posting.md").read_text() == written
    # Only a job still new: a scored one has been read already.
    assert not (tmp_path / "jobs" / str(indeed.id) / "posting.md").exists()


def test_hunt_find_saves_one_job_for_two_links_to_one_page_and_a_job_whose_page_failed(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = json.loads((HERE / "found-jobs.json").read_text())
    copy = {**found["jobs"][7], "link": "https://click.example.com/t/robot-fleet"}
    found["jobs"].append(copy)
    path = tmp_path / "found.json"
    path.write_text(json.dumps(found))
    monkeypatch.setenv("FAKE_CLAUDE_FOUND", str(path))

    def fetch(link: str) -> tuple[str, str]:
        if link == "https://click.example.com/t/robot-fleet":
            return _fetch("https://jobs.example.com/careers/1234")
        if link == "https://www.linkedin.com/jobs/view/4012345690":
            raise ValueError("a bug in reading pages")
        return _fetch(link)

    monkeypatch.setattr(cli, "fetch_page", fetch)
    assert cli.main(["find"]) == 0
    assert capsys.readouterr().out == "4 emails, 9 jobs, 4 new\n"
    saved = {job.title: job for job in JobsList(fake_claude / "data" / "jobs.db").all()}
    assert len(saved) == 4
    sdet = saved["SDET II"]
    assert (sdet.page_problem, sdet.kind) == ("ValueError: a bug in reading pages", None)
    posting = (fake_claude / "jobs" / str(sdet.id) / "posting.md").read_text()
    assert "could not be read (ValueError: a bug in reading pages)" in posting
