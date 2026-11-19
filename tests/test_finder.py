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
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt import finder
from job_hunt.finder import (
    GMAIL_READ_TOOLS,
    GMAIL_GET_THREAD,
    GMAIL_SEARCH,
    FinderError,
    batch_command,
    PageError,
    fetch_page,
    find,
    page_text_and_kind,
    place_kept,
    read_found,
    skill_command,
    title_matches,
)
from job_hunt.jobs import FoundJob, JobsList, clean_link
from job_hunt.settings import Settings, read_settings
from alert_skill import AlertSkill, arguments
from movable_clock import MovableClock
from only_named_tools import assert_only_named_tools

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


class FakeSkill(AlertSkill):
    """Stands in for the `claude -p` runs: writes what the skill wrote for the made-up emails."""

    def __init__(self, found: Path, source: Path = HERE / "found-jobs.json", fail: set[int] | None = None) -> None:
        super().__init__(source, fail)
        self.found = found


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
        good, {**good, "remote": "maybe"}, {**good, "title": ""}, {k: v for k, v in good.items() if k != "link"},
        {**good, "link": "https://[not-an-address"}]}))
    emails, jobs, problems = read_found(found, settings)
    assert (emails, len(jobs)) == (2, 1)
    assert len(problems) == 4 and "remote" in problems[0] and "title" in problems[1] and "link" in problems[2]
    assert "link" in problems[3]


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


def test_a_company_posting_beside_an_optional_sign_in_form_is_read() -> None:
    # Only a board with a known description block can tell its sign-in wall from a posting.
    html = (HERE / "pages" / "company.html").read_text().replace(
        "<body>", '<body><form action="/login"><input name="email"></form>')
    assert '<form action="/login">' in html
    text, kind = page_text_and_kind("https://jobs.example.com/careers/1234", html)
    assert (kind, text.splitlines()[0]) == ("company_site", "QA Automation Engineer, Robot Fleet")


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


def test_the_search_runs_on_haiku_with_only_gmails_search_and_its_one_file(tmp_path: Path) -> None:
    since = datetime(2026, 11, 4, 7, 0, tzinfo=UTC)
    out = tmp_path / "data" / "found-jobs.json"
    command = skill_command(since, out)
    assert command[:3] == ["claude", "-p", command[2]]
    assert command[2].startswith("/read-job-alerts ")
    assert json.loads(command[2].removeprefix("/read-job-alerts ")) == {
        "after": int(since.timestamp()),
        "since": "2026-11-04T07:00:00+00:00",
        "out": str(out),
    }
    assert command[command.index("--model") + 1] == "claude-haiku-5-5"
    allowed = command[command.index("--allowedTools") + 1].split(",")
    # Without messages to read, the search opens no email: `get_thread` lists a long thread of alerts.
    assert allowed == [GMAIL_SEARCH, GMAIL_GET_THREAD, f"Edit(/{out})"]
    assert_only_named_tools(command)
    with_messages = skill_command(since, out, since)
    assert with_messages[with_messages.index("--allowedTools") + 1].split(",") == [*GMAIL_READ_TOOLS, f"Edit(/{out})"]
    assert all(tool.startswith("mcp__claude_ai_Gmail__") for tool in GMAIL_READ_TOOLS)
    assert not any(word in tool for tool in GMAIL_READ_TOOLS for word in ("send", "draft", "label", "trash", "spam", "forward", "reply"))


def test_a_batch_runs_on_haiku_naming_its_emails_with_only_get_message_and_its_one_file(tmp_path: Path) -> None:
    out = tmp_path / "data" / "found-jobs-2.json"
    command = batch_command(["18c0ffee00000011", "18c0ffee00000012"], ["Raleigh-Durham-Chapel Hill"], out)
    assert command[:3] == ["claude", "-p", command[2]]
    assert json.loads(command[2].removeprefix("/read-job-alerts ")) == {
        "email_ids": ["18c0ffee00000011", "18c0ffee00000012"],
        "areas": ["Raleigh-Durham-Chapel Hill"],
        "out": str(out),
    }
    assert command[command.index("--model") + 1] == "claude-haiku-5-5"
    allowed = command[command.index("--allowedTools") + 1].split(",")
    # Never get_thread: an alert's thread is its HTML, larger than a tool's answer may be.
    assert allowed == ["mcp__claude_ai_Gmail__get_message", f"Edit(/{out})"]
    assert_only_named_tools(command)
    assert all(tool.startswith("mcp__claude_ai_Gmail__") for tool in GMAIL_READ_TOOLS)
    assert not any(word in tool for tool in GMAIL_READ_TOOLS for word in ("send", "draft", "label", "trash", "spam", "forward", "reply"))


def test_find_saves_the_new_jobs_with_their_pages_and_says_how_many(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "data" / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "data" / "found-jobs.json")
    summary = find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    assert (summary.emails, summary.jobs, summary.new) == (4, 8, 4)
    assert str(summary) == "alert: 4 emails, 8 jobs, 4 new\nsame job from two sources: 0\ntook: alert 0s"
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
    searches = [arguments(command) for command in skill.commands if "email_ids" not in arguments(command)]
    assert searches[1]["since"] == first.isoformat()


def test_a_run_that_writes_no_file_fails_and_the_next_run_reads_from_the_same_time(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    found = tmp_path / "found-jobs.json"
    # A file left by an earlier run is not read as this run's.
    found.write_text((HERE / "found-jobs.json").read_text())
    summary = find(settings, jobs, tmp_path / "jobs", found, run=lambda command: None, fetch=_fetch, clock=clock)
    assert len(summary.failed) == 1 and summary.failed[0].startswith("alert: the read-job-alerts search wrote no")
    assert (summary.lines, jobs.last_run(), jobs.all()) == (
        ["same job from two sources: 0", "took: alert 0s"], None, [])


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
    assert capsys.readouterr().out == "alert: 4 emails, 8 jobs, 4 new\nsame job from two sources: 0\ntook: alert 0s\n"
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


def test_list_items_html_leaves_open_neither_hide_the_page_nor_add_what_follows_the_description() -> None:
    # HTML may leave `<li>` and `<p>` unclosed; the end of their list or block closes them.
    company = page_text_and_kind(
        "https://jobs.example.com/careers/7",
        "<body><nav><ul><li>Home<li>Jobs</ul></nav><h1>QA Engineer</h1><p>Test the app.<p>Ship it.</body>")
    assert company == ("QA Engineer\nTest the app.\nShip it.", "company_site")
    linkedin = page_text_and_kind(
        "https://www.linkedin.com/jobs/view/4012345678",
        '<body><div class="show-more-less-html__markup"><ul><li>Write tests<li>Fix bugs</ul></div>'
        "<section>Similar jobs: Cook</section></body>")
    assert linkedin == ("- Write tests\n- Fix bugs", "easy_apply")


def test_a_page_with_no_text_cannot_be_read() -> None:
    with pytest.raises(PageError, match="no text"):
        page_text_and_kind("https://jobs.example.com/1", "<html><head><title>x</title></head><body> </body></html>")


def test_the_skill_may_write_only_its_own_file_when_the_path_is_absolute(tmp_path: Path) -> None:
    command = skill_command(datetime(2026, 11, 4, tzinfo=UTC), tmp_path / "found.json")
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
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    print(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}), file=log)
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print("starting", file=sys.stderr)
    print("Not logged in. Run claude to sign in.", file=sys.stderr)
    sys.exit(1)
arguments = json.loads(sys.argv[2].removeprefix("/read-job-alerts "))
with open(os.environ["FAKE_CLAUDE_FOUND"]) as source:
    written = json.load(source)
if "email_ids" in arguments:
    answer = {{"emails": len(arguments["email_ids"]),
              "jobs": [job for job in written["jobs"] if job["email_id"] in arguments["email_ids"]]}}
else:
    answer = {{"alerts": [{{"email_id": job["email_id"], "email_date": job["email_date"]}} for job in written["jobs"]]}}
    if "messages_after" in arguments:
        answer.update(recruiter_emails=written["recruiter_emails"],
                      linkedin_notifications=written["linkedin_notifications"])
with open(arguments["out"], "w") as out:
    json.dump(answer, out)
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


def test_hunt_find_starts_claude_on_haiku_in_the_run_folder_with_only_gmail_reads_and_its_file(
    fake_claude: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["find"]) == 0
    assert capsys.readouterr().out == "alert: 4 emails, 8 jobs, 4 new\nsame job from two sources: 0\ntook: alert 0s\n"
    search, batch = (json.loads(line) for line in (tmp_path / "claude.json").read_text().splitlines())
    for started in (search, batch):
        argv = started["argv"]
        assert Path(started["cwd"]) == fake_claude / ".background"
        assert argv[0] == "-p" and argv[1].startswith("/read-job-alerts {")
        assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    found = fake_claude / "data" / "found-jobs.json"
    # The search reads the recruiters' emails too; the batch only the alerts it is named.
    assert search["argv"][search["argv"].index("--allowedTools") + 1].split(",") == [
        *GMAIL_READ_TOOLS, f"Edit(/{found})"]
    assert batch["argv"][batch["argv"].index("--allowedTools") + 1].split(",") == [
        "mcp__claude_ai_Gmail__get_message", f"Edit(/{found.with_name('found-jobs-1.json')})"]
    # The batch's own file is merged into the jobs file and gone.
    assert not found.with_name("found-jobs-1.json").exists()
    assert len(list((fake_claude / "jobs").iterdir())) == 4


def test_hunt_find_says_why_the_skill_run_failed_and_reads_from_the_same_time_next(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    assert cli.main(["find"]) == 1
    printed = capsys.readouterr()
    assert printed.out == "same job from two sources: 0\ntook: alert 0s\n"
    assert printed.err == "hunt find: alert: the read-job-alerts run failed (1): Not logged in. Run claude to sign in.\n"
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
    assert printed.out == "alert: 4 emails, 7 jobs, 3 new\nsame job from two sources: 0\ntook: alert 0s\n"
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
    summary = find(settings, jobs, tmp_path / "jobs", found, run=AlertSkill(json.loads(written)),
                   fetch=redirect, clock=clock)
    assert (summary.jobs, summary.new) == (2, 1)
    assert [(each.link, each.page_link) for each in jobs.all()] == [
        ("https://click.example.com/t/one", "https://jobs.example.com/careers/1234")]


def test_a_second_run_while_one_runs_fails_and_neither_reads_the_others_file(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    refused: list[str] = []

    def first_run(command: list[str]) -> None:
        with pytest.raises(FinderError, match="already running") as error:
            find(settings, JobsList(tmp_path / "jobs.db", clock=clock), tmp_path / "jobs", skill.found,
                 run=skill, fetch=_fetch, clock=clock)
        refused.append(str(error.value))
        skill(command)

    summary = find(settings, jobs, tmp_path / "jobs", skill.found, run=first_run, fetch=_fetch, clock=clock)
    # The search and its one batch: each refused the second run.
    assert len(refused) == len(skill.commands) == 2 and (summary.jobs, summary.new) == (8, 4)
    assert jobs.last_run() == clock.now
    # The lock goes with the run: the next one runs.
    clock.advance(days=1)
    assert find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock).jobs == 8


def test_a_posting_cut_short_while_written_is_never_left_as_if_whole(
    tmp_path: Path, settings: Settings, clock: MovableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = FakeSkill(tmp_path / "found-jobs.json")
    write_text = Path.write_text

    def full_disk(path: Path, text: str, *args: object, **kwargs: object) -> int:
        write_text(path, text[: len(text) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", full_disk)
    with pytest.raises(OSError):
        find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    assert not list((tmp_path / "jobs").glob("*/posting.md"))
    monkeypatch.setattr(Path, "write_text", write_text)
    clock.advance(days=1)
    find(settings, jobs, tmp_path / "jobs", skill.found, run=skill, fetch=_fetch, clock=clock)
    lab = next(job for job in jobs.all() if "4012345678" in job.link)
    assert (tmp_path / "jobs" / str(lab.id) / "posting.md").read_text().endswith(lab.description + "\n")


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
    assert capsys.readouterr().out == "alert: 4 emails, 9 jobs, 4 new\nsame job from two sources: 1\ntook: alert 0s\n"
    saved = {job.title: job for job in JobsList(fake_claude / "data" / "jobs.db").all()}
    assert len(saved) == 4
    sdet = saved["SDET II"]
    assert (sdet.page_problem, sdet.kind) == ("ValueError: a bug in reading pages", None)
    posting = (fake_claude / "jobs" / str(sdet.id) / "posting.md").read_text()
    assert "could not be read (ValueError: a bug in reading pages)" in posting


def test_hunt_find_starts_no_run_while_the_run_folder_holds_a_file(
    fake_claude: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (fake_claude / ".background").mkdir()
    (fake_claude / ".background" / "career.json").write_text("{}")
    assert cli.main(["find"]) == 1
    assert "holds career.json, and it must be empty" in capsys.readouterr().err
    assert not (tmp_path / "claude.json").exists()


@pytest.mark.parametrize(("seconds", "said"), [(0, "0s"), (-1, "0s"), (45, "45s"), (250, "4m 10s"), (3720, "1h 2m")])
def test_how_long_a_source_took_is_said_in_hours_minutes_or_seconds(seconds: float, said: str) -> None:
    from job_hunt.finder import took

    assert took(seconds) == said


# The alert emails in batches


def _alerts(count: int) -> dict:
    """One remote job in each of `count` alert emails, an hour apart, listed newest first."""
    first = datetime(2026, 11, 5, 7, 0, tzinfo=UTC)
    jobs = [{**_job().__dict__, "link": f"https://jobs.example.com/careers/{number}", "remote": "remote",
             "title": f"QA Automation Engineer {number}", "board": "company", "area": None,
             "email_id": f"18c0ffee{number:08d}", "email_date": (first + timedelta(hours=number)).isoformat()}
            for number in range(1, count + 1)]
    for job in jobs:
        del job["source"]
    return {"emails": count, "jobs": list(reversed(jobs))}


def _any_page(link: str) -> tuple[str, str]:
    return link, (HERE / "pages" / "company.html").read_text()


def test_25_alert_emails_are_read_in_3_batches_of_at_most_10_oldest_first(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = AlertSkill(_alerts(25))
    found = tmp_path / "found-jobs.json"
    summary = find(settings, jobs, tmp_path / "jobs", found, run=skill, fetch=_any_page, clock=clock)
    assert [len(batch) for batch in skill.batches] == [10, 10, 5]
    assert [id for batch in skill.batches for id in batch] == [f"18c0ffee{number:08d}" for number in range(1, 26)]
    # Each batch writes a file of its own, which the script merges into one.
    outs = [arguments(command)["out"] for command in skill.commands]
    assert outs == [str(found), *(str(tmp_path / f"found-jobs-{number}.json") for number in (1, 2, 3))]
    assert sorted(path.name for path in tmp_path.glob("found-jobs*.json")) == ["found-jobs.json"]
    merged = json.loads(found.read_text())
    assert (merged["emails"], len(merged["jobs"])) == (25, 25)
    assert (summary.emails, summary.jobs, summary.new, summary.failed) == (25, 25, 25, [])
    assert jobs.last_run() == clock.now


def test_a_failed_middle_batch_is_named_the_others_save_and_the_next_run_reads_it_again(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    # Run 3 is the second batch: it writes nothing.
    skill = AlertSkill(_alerts(25), fail={3})
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=skill, fetch=_any_page,
                   clock=clock)
    assert summary.failed == [f"alert: batch 2 of 3 failed: the read-job-alerts batch wrote no file "
                              f"({tmp_path / 'found-jobs-2.json'})"]
    assert (summary.emails, summary.jobs, summary.new) == (15, 15, 15)
    assert str(summary).splitlines()[0] == "alert: 15 emails, 15 jobs, 15 new"
    # The last run moves only to the newest email of the first batch, so the next run reads from it.
    tenth = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)
    assert jobs.last_run() == tenth - timedelta(seconds=1)
    clock.advance(days=1)
    again = AlertSkill(_alerts(25))
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=again, fetch=_any_page,
                   clock=clock)
    assert arguments(again.commands[0])["after"] == int((tenth - timedelta(seconds=1)).timestamp())
    assert (summary.new, summary.failed, jobs.last_run()) == (10, [], clock.now)
    assert len(jobs.all()) == 25


def test_a_batch_that_read_fewer_emails_than_it_was_named_keeps_its_jobs_and_the_last_run_before_it(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    skill = AlertSkill(_alerts(25))

    def run(command: list[str]) -> None:
        skill(command)
        args = arguments(command)
        if args.get("email_ids", [""])[0] == "18c0ffee00000011":
            # The second batch leaves out its last email.
            out = Path(args["out"])
            written = json.loads(out.read_text())
            written["emails"] = 9
            written["jobs"] = [job for job in written["jobs"] if job["email_id"] != "18c0ffee00000020"]
            out.write_text(json.dumps(written))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=run, fetch=_any_page,
                   clock=clock)
    assert summary.failed == ["alert: batch 2 of 3 read 9 of 10 emails"]
    # Its jobs and the third batch's are saved.
    assert (summary.emails, summary.jobs, summary.new) == (24, 24, 24)
    # The last run stays before the second batch, so its emails are read again next run.
    tenth = datetime(2026, 11, 5, 17, 0, tzinfo=UTC)
    assert jobs.last_run() == tenth - timedelta(seconds=1)


def test_a_short_batch_holding_an_email_without_a_date_leaves_the_last_run_where_it_was(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    skill = AlertSkill(_alerts(12))

    def run(command: list[str]) -> None:
        skill(command)
        args = arguments(command)
        out = Path(args["out"])
        written = json.loads(out.read_text())
        if "email_ids" not in args:
            # The search lists the third email without a date, so it is read in the last batch.
            for entry in written["alerts"]:
                if entry["email_id"] == "18c0ffee00000003":
                    entry["email_date"] = None
        elif "18c0ffee00000003" in args["email_ids"]:
            written["emails"] -= 1
        out.write_text(json.dumps(written))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=run, fetch=_any_page,
                   clock=clock)
    assert summary.failed == ["alert: batch 2 of 2 read 1 of 2 emails"]
    assert jobs.last_run() is None


@pytest.mark.parametrize(("link", "cleaned"), [
    ("https://www.linkedin.com/comm/jobs/view/123?trackingId=x&refId=y", True),
    ("https://WWW.LinkedIn.com/jobs/view/123?trk=a", True),
    ("https://uk.indeed.com/rc/clk?jk=abc&from=email", True),
    ("https://indeed.com/viewjob?jk=abc&utm_source=x", True),
    ("https://notlinkedin.com/r?trk=a", False),
    ("https://linkedin.com.example.com/r?utm_source=x", False),
    ("https://myindeed.co/r?ref=1", False),
    ("  https://click.example.com/redirect?ref=job123  ", False),
])
def test_only_a_linkedin_or_indeed_link_is_cleaned_before_it_is_followed(link: str, cleaned: bool) -> None:
    assert finder.followed_link(link) == (clean_link(link) if cleaned else link.strip())


def test_a_failed_first_batch_leaves_the_last_run_where_it_was(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = AlertSkill(_alerts(12), fail={2})
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=skill, fetch=_any_page,
                   clock=clock)
    assert [line.split(":", 2)[1] for line in summary.failed] == [" batch 1 of 2 failed"]
    assert (summary.new, jobs.last_run()) == (2, None)


def test_a_batch_whose_run_fails_is_named_with_why_and_the_others_still_count(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    skill = AlertSkill(_alerts(12))

    def run(command: list[str]) -> None:
        if arguments(command).get("email_ids", [""])[0] == "18c0ffee00000011":
            raise FinderError("the read-job-alerts run failed (1): Gmail is not connected")
        skill(command)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=run, fetch=_any_page,
                   clock=clock)
    assert summary.failed == ["alert: batch 2 of 2 failed: the read-job-alerts run failed (1): Gmail is not connected"]
    assert summary.new == 10


def test_no_alert_email_runs_no_batch(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    skill = AlertSkill({"emails": 0, "jobs": []})
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=skill, fetch=_any_page,
                   clock=clock)
    assert (len(skill.commands), skill.batches, summary.emails, summary.failed) == (1, [], 0, [])
    assert jobs.last_run() == clock.now


def test_a_search_that_lists_an_email_twice_or_without_its_date_reads_it_once_last(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    found = tmp_path / "found-jobs.json"
    batches: list[list[str]] = []

    def run(command: list[str]) -> None:
        args = arguments(command)
        if "email_ids" in args:
            batches.append(args["email_ids"])
            Path(args["out"]).write_text(json.dumps({"emails": len(args["email_ids"]), "jobs": []}))
        else:
            found.write_text(json.dumps({"alerts": [
                {"email_id": "18c0ffee00000002", "email_date": "2026-11-05T09:00:00+00:00"},
                {"email_id": "18c0ffee00000009"},
                {"email_id": "18c0ffee00000001", "email_date": "2026-11-05T08:00:00+00:00"},
                {"email_id": "18c0ffee00000002", "email_date": "2026-11-05T09:00:00+00:00"},
                {"email_date": "2026-11-05T09:00:00+00:00"}]}))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=run, fetch=_any_page, clock=clock)
    assert batches == [["18c0ffee00000001", "18c0ffee00000002", "18c0ffee00000009"]]
    assert summary.problems == ["alert email 5 has no email_id"]


def test_a_click_tracking_link_is_saved_as_it_is_and_followed_to_its_page(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    tracking = ("https://click.appcast.io/track/7a1b2c3d?cs=jobot&jg=4xyz&bid=Q2xpY2tIZXJl&u=aHR0cHM6Ly9qb2JzLmV4YW1w"
                "bGUuY29tL2NhcmVlcnMvMTIzNA%3D%3D&utm_source=jobot&utm_medium=email")
    job = {**_job().__dict__, "link": tracking, "board": "company", "remote": "remote", "area": None}
    del job["source"]
    followed: list[str] = []

    def redirect(link: str) -> tuple[str, str]:
        followed.append(link)
        return "https://jobs.example.com/careers/1234?utm_source=jobot", (HERE / "pages" / "company.html").read_text()

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [job]}), fetch=redirect, clock=clock)
    assert (summary.new, summary.problems) == (1, [])
    (saved,) = jobs.all()
    assert followed == [tracking] and saved.link == clean_link(tracking)
    assert saved.page_link == "https://jobs.example.com/careers/1234"
    assert saved.description and saved.page_problem is None


@pytest.mark.parametrize("tracking", [
    "https://click.example.com/redirect?ref=job123",
    "https://www.jobot.com/redirect?src=email&trk=abc&refId=9",
])
def test_a_tracking_link_is_followed_with_the_parameters_the_saved_link_leaves_out(
    tmp_path: Path, settings: Settings, clock: MovableClock, tracking: str
) -> None:
    job = {**_job().__dict__, "link": tracking, "board": "company", "remote": "remote", "area": None}
    del job["source"]

    def redirect(link: str) -> tuple[str, str]:
        # Only the address as the email gave it leads to the job.
        if link != tracking:
            raise PageError("HTTP 404")
        return "https://jobs.example.com/careers/1234?utm_source=email", (HERE / "pages" / "company.html").read_text()

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [job]}), fetch=redirect, clock=clock)
    assert (summary.new, summary.problems) == (1, [])
    (saved,) = jobs.all()
    assert saved.page_problem is None and saved.description
    # The job is known by the address as the email gave it, which `clean_link` would leave with no
    # parameter, and the duplicate check after reading works on the cleaned page address.
    assert (saved.link, saved.page_link) == (tracking, "https://jobs.example.com/careers/1234")


def _tracked(link: str, title: str) -> dict:
    """An alert's job behind a click-tracking address, as the batch file holds it."""
    job = {**_job().__dict__, "link": link, "title": title, "board": "jobot", "remote": "remote", "area": None}
    del job["source"]
    return job


def _postings(pages: dict[str, str], followed: list[str]) -> Callable[[str], tuple[str, str]]:
    """A fetch that answers only each tracking address exactly as given, with the posting it leads to."""
    def redirect(link: str) -> tuple[str, str]:
        followed.append(link)
        if link not in pages:
            raise PageError("HTTP 404")
        return pages[link], (HERE / "pages" / "company.html").read_text()
    return redirect


@pytest.mark.parametrize("links", [
    ("https://click.example.com/redirect?ref=job123", "https://click.example.com/redirect?ref=job456"),
    ("https://www.jobot.com/redirect?src=email&trk=abc&refId=9",
     "https://www.jobot.com/redirect?src=email&trk=def&refId=10"),
])
def test_two_tracking_links_that_differ_only_in_their_tracking_parameters_are_two_jobs(
    tmp_path: Path, settings: Settings, clock: MovableClock, links: tuple[str, str]
) -> None:
    first, second = links
    followed: list[str] = []
    fetch = _postings({first: "https://jobs.example.com/careers/1?utm_source=email",
                       second: "https://jobs.example.com/careers/2?utm_source=email"}, followed)
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [_tracked(first, "QA Automation Engineer"),
                                                         _tracked(second, "SDET II QA Automation Engineer")]}),
                   fetch=fetch, clock=clock)
    assert sorted(followed) == sorted(links)
    assert (summary.jobs, summary.new, summary.problems) == (2, 2, [])
    assert "same job from two sources: 0" in summary.lines
    assert sorted((each.link, each.page_link) for each in jobs.all()) == [
        (first, "https://jobs.example.com/careers/1"), (second, "https://jobs.example.com/careers/2")]


def test_two_tracking_links_whose_tracker_answers_itself_are_two_jobs(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    links = ("https://click.example.com/redirect?ref=job123", "https://click.example.com/redirect?ref=job456")
    followed: list[str] = []
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [_tracked(links[0], "QA Automation Engineer")]}),
                   fetch=_postings({link: link for link in links}, followed), clock=clock)
    clock.advance(days=1)
    later = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                 run=AlertSkill({"emails": 1, "jobs": [_tracked(links[1], "Lead QA Automation Engineer")]}),
                 fetch=_postings({link: link for link in links}, followed), clock=clock)
    assert (summary.new, later.new, followed) == (1, 1, list(links))
    assert [(each.link, each.page_link) for each in jobs.all()] == [(links[0], None), (links[1], None)]


def test_a_later_run_saves_a_new_posting_behind_another_tracking_link_of_the_same_board(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    first = "https://www.jobot.com/redirect?src=email&trk=abc&refId=9"
    third = "https://www.jobot.com/redirect?src=email&trk=ghi&refId=11"
    followed: list[str] = []
    fetch = _postings({first: "https://jobs.example.com/careers/1", third: "https://jobs.example.com/careers/3"},
                      followed)
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
         run=AlertSkill({"emails": 1, "jobs": [_tracked(first, "QA Automation Engineer")]}), fetch=fetch, clock=clock)
    clock.advance(days=1)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [_tracked(third, "Lead QA Automation Engineer")]}),
                   fetch=fetch, clock=clock)
    assert (summary.new, followed) == (1, [first, third])
    assert [each.page_link for each in jobs.all()] == ["https://jobs.example.com/careers/1",
                                                      "https://jobs.example.com/careers/3"]
    # The same address sent again is known before its page is read.
    clock.advance(days=1)
    again = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                 run=AlertSkill({"emails": 1, "jobs": [_tracked(third, "Lead QA Automation Engineer")]}),
                 fetch=fetch, clock=clock)
    assert (again.new, followed) == (0, [first, third])


@pytest.mark.parametrize("days_later", [1, 40])
def test_a_tracking_link_that_ends_at_a_job_the_list_has_is_not_saved_twice(
    tmp_path: Path, settings: Settings, clock: MovableClock, days_later: int
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    jobs.add(FoundJob(**{**_tracked("https://jobs.example.com/careers/1", "QA Automation Engineer"),
                         "source": "greenhouse"}), description="Test the product.", kind="company_site")
    # A day later it is the same job by its title and company before its page is read; 40 days
    # later, past `SAME_JOB_DAYS`, by the page the link ended at.
    clock.advance(days=days_later)
    tracking = "https://www.jobot.com/redirect?src=email&trk=abc&refId=9"
    followed: list[str] = []
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [_tracked(tracking, "QA Automation Engineer")]}),
                   fetch=_postings({tracking: "https://jobs.example.com/careers/1?utm_source=jobot"}, followed),
                   clock=clock)
    assert (summary.new, len(jobs.all())) == (0, 1)
    assert followed == ([] if days_later == 1 else [tracking])
    # Another posting of that board behind another tracking address is not taken for this one.
    assert not jobs.has("https://www.jobot.com/redirect?src=email&trk=zzz&refId=99")


def test_the_skill_reads_each_alert_in_plain_text_by_its_id_and_keeps_a_tracking_link() -> None:
    skill = " ".join((Path(cli.ROOT) / ".claude" / "skills" / "read-job-alerts" / "SKILL.md").read_text().split())
    assert 'with the Gmail connector\'s `get_message` and `"messageFormat": "PLAIN_TEXT"`. Never use another format, ' \
           "and never `get_thread`" in skill
    assert "Read each email of `email_ids`" in skill and "Read no other email and make no search." in skill
    assert "also a click-tracking address" in skill and "never leave a job out because of its link" in skill


def test_the_skill_searches_a_thread_of_alerts_again_for_the_messages_its_result_leaves_out() -> None:
    skill = " ".join((Path(cli.ROOT) / ".claude" / "skills" / "read-job-alerts" / "SKILL.md").read_text().split())
    assert "a search result shows at most 5 messages of a thread" in skill
    assert "When a thread of alerts shows 5 messages, or fewer than its `messageCount`" in skill
    assert ("list all of its messages with `get_thread` and `\"messageFormat\": \"METADATA_ONLY\"`, which gives "
            "each message's id and date and none of its text, and keep those that arrived after `after`") in skill
    assert "Never call `get_thread` with another format in this task" in skill


def test_a_failed_batch_holding_an_email_without_a_date_leaves_the_last_run_where_it_was(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    found = tmp_path / "found-jobs.json"
    listed = [{"email_id": f"18c0ffee{number:08d}", "email_date": f"2026-11-10T{number:02d}:00:00+00:00"}
              for number in range(10)] + [{"email_id": "18c0ffee00000099"}]

    def run(command: list[str]) -> None:
        args = arguments(command)
        if "email_ids" not in args:
            found.write_text(json.dumps({"alerts": listed}))
        elif "18c0ffee00000099" not in args["email_ids"]:
            Path(args["out"]).write_text(json.dumps({"emails": len(args["email_ids"]), "jobs": []}))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=run, fetch=_any_page, clock=clock)
    assert [line.split(":", 2)[1] for line in summary.failed] == [" batch 2 of 2 failed"]
    # Its email may have come before the ten read: the next run reads from the same time again.
    assert jobs.last_run() is None


@pytest.mark.parametrize(("written", "said"), [
    ('{"emails": 0, "jobs": []}', 'a file without a list of "alerts"'),
    ('{"alerts": null}', 'a file without a list of "alerts"'),
    ("{not json", "a file that is not JSON"),
])
def test_a_search_without_a_list_of_alerts_fails_and_the_last_run_stays(
    tmp_path: Path, settings: Settings, clock: MovableClock, written: str, said: str
) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=AlertSkill({"emails": 0, "jobs": []}),
         fetch=_any_page, clock=clock)
    before = jobs.last_run()
    clock.advance(days=1)
    found = tmp_path / "found-jobs.json"
    commands: list[list[str]] = []

    def run(command: list[str]) -> None:
        commands.append(command)
        found.write_text(written)

    summary = find(settings, jobs, tmp_path / "jobs", found, run=run, fetch=_any_page, clock=clock)
    assert len(summary.failed) == 1 and summary.failed[0].startswith(f"alert: the read-job-alerts search wrote {said}")
    assert (len(commands), jobs.last_run()) == (1, before)


def test_alert_emails_are_ordered_by_their_time_whatever_its_offset(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    found = tmp_path / "found-jobs.json"
    # Ten emails from 16:30 UTC, one a minute; 12:00-05:00 is 17:00 UTC, after them as a time but first as text;
    # 08:00 with no offset is read as UTC and is the oldest.
    listed = [{"email_id": f"utc-{number}", "email_date": f"2026-11-05T16:{30 + number:02d}:00+00:00"}
              for number in range(10)]
    listed += [{"email_id": "eastern", "email_date": "2026-11-05T12:00:00-05:00"},
               {"email_id": "no-offset", "email_date": "2026-11-05T08:00:00"}]
    batches: list[list[str]] = []

    def run(command: list[str]) -> None:
        args = arguments(command)
        if "email_ids" not in args:
            found.write_text(json.dumps({"alerts": listed}))
            return
        batches.append(args["email_ids"])
        if len(batches) == 1:
            Path(args["out"]).write_text(json.dumps({"emails": len(args["email_ids"]), "jobs": []}))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    find(settings, jobs, tmp_path / "jobs", found, run=run, fetch=_any_page, clock=clock)
    assert batches == [["no-offset", *(f"utc-{number}" for number in range(9))], ["utc-9", "eastern"]]
    # The newest of the first batch, 16:38 UTC, less a second: before both emails of the failed one.
    assert jobs.last_run() == datetime(2026, 11, 5, 16, 37, 59, tzinfo=UTC)


def test_a_failed_batch_never_moves_the_last_run_past_the_runs_start(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    found = tmp_path / "found-jobs.json"
    # Ten emails that came while the search ran, after the run started, and one more.
    listed = [{"email_id": f"late-{number}", "email_date": (clock.now + timedelta(minutes=number + 1)).isoformat()}
              for number in range(11)]

    def run(command: list[str]) -> None:
        args = arguments(command)
        if "email_ids" not in args:
            found.write_text(json.dumps({"alerts": listed}))
        elif "late-10" not in args["email_ids"]:
            Path(args["out"]).write_text(json.dumps({"emails": len(args["email_ids"]), "jobs": []}))

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    started = clock.now
    summary = find(settings, jobs, tmp_path / "jobs", found, run=run, fetch=_any_page, clock=clock)
    assert len(summary.failed) == 1 and jobs.last_run() == started
