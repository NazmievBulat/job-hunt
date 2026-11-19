"""The settings' `chrome_browser`: one browser, named by its deviceId, that every browser worker
uses instead of this computer's Chrome, on whatever computer it runs, and never another.

No model, no browser and no network: the workers are stand-ins that write their reports, and the
commands are read as they would be started. The deviceIds are made up."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.apply import SITE_SKILL, UNSURE, apply, linkedin_command, read_report, site_command
from job_hunt.career import Career, write_career
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.plan import WAITS
from job_hunt.replies import read_command
from job_hunt.settings import SettingsError, read_settings
from movable_clock import MovableClock

HOME_MAC = "0b6c9a52-1111-4a2b-9c3d-000000000002"
WORK_MAC = "0b6c9a52-1111-4a2b-9c3d-000000000001"
SEARCH = {"titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7}
# The browser a worker says it selected: the one the settings name, on another computer.
THE_NAMED = {"device_id": HOME_MAC, "name": "Browser 2", "system": "macOS", "on_this_computer": False}
CONNECTED = "Browser 1 (macOS), Browser 2 (macOS)"


def _settings(folder: Path, **more: object) -> Path:
    path = folder / "settings.json"
    path.write_text(json.dumps({**SEARCH, **more}))
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "jobs").mkdir()
    write_career(Career(person={"name": "Ada Example", "email": "ada@example.com"}, summary="", titles=[],
                        skills=[], jobs=[], education=[], certifications=[], form_answers={}),
                 tmp_path / "data" / "career.json")
    return tmp_path


@pytest.fixture
def jobs(project: Path) -> JobsList:
    return JobsList(project / "data" / "jobs.db", clock=MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC)))


def _picked(jobs: JobsList, number: int = 1, kind: str = "company_site") -> int:
    job = jobs.add(FoundJob(link=f"https://jobs.example.com/{number}", title=f"QA Engineer {number}",
                            company="Example Co", place="Durham, NC", remote="on_site", board="indeed", email_id="1",
                            email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill"), kind=kind)
    jobs.score(job, 8, "Good match.")
    jobs.move(job, "picked")
    return job


def _arguments(command: list[str]) -> dict:
    prompt = command[command.index("-p") + 1]
    return json.loads(prompt.split(" ", 1)[1])


class Worker:
    """Stands in for the resume and site workers: builds the resume, writes the given report, and
    keeps the arguments each site worker was started with."""

    def __init__(self, project: Path, report: dict | None) -> None:
        self.project, self.report, self.started = project, report, []

    def __call__(self, command: list[str]) -> None:
        arguments = _arguments(command)
        folder = self.project / "jobs" / str(arguments["job"])
        if "build" in arguments:
            (folder / "resume.pdf").write_bytes(b"%PDF-1.4 stand-in")
            return
        self.started.append(arguments)
        if self.report is not None:
            (folder / "report.json").write_text(json.dumps(self.report))


def no_linkedin(command: list[str]) -> None:
    raise AssertionError(f"no LinkedIn worker expected: {command}")


def _apply(project: Path, jobs: JobsList, report: dict | None, browser: str | None = HOME_MAC) -> Worker:
    worker = Worker(project, report)
    apply(jobs, project, run_resume=worker, run_site=worker, run_linkedin=no_linkedin, browser=browser)
    return worker


# The setting


def test_chrome_browser_is_left_out_by_default_and_keeps_a_device_id(tmp_path: Path) -> None:
    assert read_settings(_settings(tmp_path)).chrome_browser is None
    assert read_settings(_settings(tmp_path, chrome_browser=None)).chrome_browser is None
    assert read_settings(_settings(tmp_path, chrome_browser=f" {HOME_MAC} ")).chrome_browser == HOME_MAC


@pytest.mark.parametrize("value", ["", " ", 12, True, ["x"], {"deviceId": HOME_MAC}, "Browser 2", "a b",
                                   "-starts-with-a-dash", "x" * 129, 'quote"in'])
def test_chrome_browser_that_is_not_a_device_id_is_refused(tmp_path: Path, value: object) -> None:
    with pytest.raises(SettingsError, match="chrome_browser must be a browser's deviceId"):
        read_settings(_settings(tmp_path, chrome_browser=value))


@pytest.fixture
def hunt(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    return project


def test_hunt_chrome_browser_sets_says_and_clears_the_one_browser(
        hunt: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _settings(hunt / "data", pushover={"token": "t0ken", "user": "us3r"}, daily_applications=40)

    assert cli.main(["chrome-browser"]) == 0
    assert capsys.readouterr().out == "the browser workers use only the Chrome on this computer\n"

    assert cli.main(["chrome-browser", HOME_MAC]) == 0
    assert capsys.readouterr().out == (
        f"saved: the browser workers use only the browser {HOME_MAC}, on whatever computer it runs, and never "
        "another; hunt doctor says whether it is connected\n")
    saved = read_settings(path)
    # Only chrome_browser changed.
    assert (saved.chrome_browser, saved.daily_applications, saved.titles) == (HOME_MAC, 40, ["QA Engineer"])
    assert saved.pushover is not None and saved.pushover.token == "t0ken"
    assert cli.main(["chrome-browser"]) == 0
    assert HOME_MAC in capsys.readouterr().out

    assert cli.main(["chrome-browser", "off"]) == 0
    assert capsys.readouterr().out == "saved: the browser workers use only the Chrome on this computer\n"
    assert "chrome_browser" not in json.loads(path.read_text())
    assert read_settings(path).daily_applications == 40


def test_hunt_chrome_browser_refuses_what_is_not_a_device_id_and_keeps_the_settings(
        hunt: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _settings(hunt / "data", chrome_browser=HOME_MAC)
    before = path.read_text()

    assert cli.main(["chrome-browser", "Browser 1 (macOS)"]) == 1

    assert "hunt chrome-browser: chrome_browser must be a browser's deviceId" in capsys.readouterr().err
    assert path.read_text() == before


def test_hunt_chrome_browser_needs_the_settings_first(hunt: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["chrome-browser", HOME_MAC]) == 1
    assert "run /setup to make it" in capsys.readouterr().err
    assert not (hunt / "data" / "settings.json").exists()


def test_hunt_settings_keeps_the_browser_named_and_null_clears_it(hunt: Path) -> None:
    path = _settings(hunt / "data", chrome_browser=HOME_MAC)

    assert cli.main(["settings", json.dumps({**SEARCH, "titles": ["SDET"]})]) == 0
    assert (read_settings(path).titles, read_settings(path).chrome_browser) == (["SDET"], HOME_MAC)

    assert cli.main(["settings", json.dumps({**SEARCH, "chrome_browser": None})]) == 0
    assert read_settings(path).chrome_browser is None


def test_settings_that_cannot_be_read_name_no_browser(hunt: Path) -> None:
    # A broken file never sends a worker to another computer's browser: this computer's Chrome only.
    _settings(hunt / "data", chrome_browser=HOME_MAC, remote="sometimes")
    assert cli.chrome_browser() is None
    _settings(hunt / "data", chrome_browser=HOME_MAC)
    assert cli.chrome_browser() == HOME_MAC


# Every browser worker is given it


def test_each_browser_workers_command_carries_the_browser_only_when_named(project: Path, jobs: JobsList) -> None:
    site = jobs.get(_picked(jobs, 1))
    easy = jobs.get(_picked(jobs, 2, kind="easy_apply"))
    after, out = datetime(2026, 11, 18, tzinfo=UTC), project / "data" / "linkedin-messages.json"

    for command in (site_command(site, project, browser=HOME_MAC), linkedin_command(easy, project, browser=HOME_MAC),
                    read_command(after, out, browser=HOME_MAC)):
        assert _arguments(command)["browser"] == HOME_MAC
    for command in (site_command(site, project), linkedin_command(easy, project), read_command(after, out),
                    site_command(site, project, browser=None)):
        assert "browser" not in _arguments(command)


class Started(Exception):
    """Raised by a stand-in for the step that starts the browser workers, once it saw its browser."""


@pytest.mark.parametrize(("argv", "step"), [
    (["apply"], "apply"), (["send-notes"], "send_notes"), (["linkedin-messages"], "read_linkedin"),
    (["send-reply", "1"], "send_reply")])
def test_each_hunt_command_that_starts_a_browser_worker_passes_the_settings_browser(
        hunt: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], step: str) -> None:
    given: list[str | None] = []

    def started(*args: object, browser: str | None = None, **named: object) -> object:
        given.append(browser)
        raise Started

    monkeypatch.setattr(cli, step, started)
    for setting in (HOME_MAC, None):
        _settings(hunt / "data", chrome_browser=setting)
        with pytest.raises(Started):
            cli.main(argv)
    assert given == [HOME_MAC, None]


# hunt apply takes only a report from the browser named


def test_a_worker_is_started_with_the_browser_and_its_report_from_it_is_taken(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs)
    worker = _apply(project, jobs, {"result": "applied", "browser": THE_NAMED})

    assert [arguments["browser"] for arguments in worker.started] == [HOME_MAC]
    # Not on this computer, and that is right: the settings named it.
    assert jobs.get(job).state == "applied"


@pytest.mark.parametrize("browser", [
    None,
    # This computer's Chrome: not the one named.
    {"name": "Browser 3", "system": "Linux", "on_this_computer": True},
    {**THE_NAMED, "device_id": WORK_MAC},
    {**THE_NAMED, "device_id": HOME_MAC.upper()},
    {**THE_NAMED, "name": " "},
    {key: value for key, value in THE_NAMED.items() if key != "system"},
    HOME_MAC,
])
def test_a_report_from_another_browser_than_the_one_named_pauses_the_job_for_a_look(
        project: Path, jobs: JobsList, browser: object) -> None:
    job = _picked(jobs)
    report = {"result": "applied"} | ({} if browser is None else {"browser": browser})

    _apply(project, jobs, report)

    # It may have applied from a browser the person never chose: never tried again, no try spent.
    assert (jobs.get(job).state, jobs.get(job).failures) == ("paused", 0)
    assert jobs.get(job).state_reason == (
        f"{UNSURE}: the {SITE_SKILL} run did not say it used the browser data/settings.json names "
        f"(chrome_browser {HOME_MAC})")


def test_without_the_setting_a_report_from_the_named_browser_on_another_computer_is_refused(
        project: Path, jobs: JobsList) -> None:
    job = _picked(jobs)

    worker = _apply(project, jobs, {"result": "applied", "browser": THE_NAMED}, browser=None)

    assert "browser" not in worker.started[0]
    assert jobs.get(job).state == "paused"
    assert jobs.get(job).state_reason == f"{UNSURE}: the {SITE_SKILL} run did not say it used this computer's Chrome"


@pytest.mark.parametrize(("connected", "said"), [
    (CONNECTED, f"; connected now: {CONNECTED}; workers use no other"), ("none", ""), (None, "")])
def test_the_named_browser_not_connected_pauses_for_chrome_at_no_cost_and_names_it(
        project: Path, jobs: JobsList, connected: str | None, said: str) -> None:
    job = _picked(jobs)
    report = {"result": "paused", "needs": "chrome"} | ({"connected": connected} if connected else {})

    _apply(project, jobs, report)

    assert (jobs.get(job).state, jobs.get(job).failures) == ("paused", 0)
    reason = jobs.get(job).state_reason
    assert reason == ("Chrome open, with the Claude extension connected, in the browser data/settings.json names "
                      f"(chrome_browser {HOME_MAC}{said})")
    # The day's plan still counts it as waiting for Chrome.
    assert next(what for what, start in WAITS if reason.startswith(start)) == "Chrome open"


def test_a_report_read_again_later_is_taken_as_it_is(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs)
    path = project / "jobs" / str(job) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": "paused", "needs": "question", "question": "Visa?", "tab": "Apply"}))

    assert read_report(path, browser=HOME_MAC).question == "Visa?"
