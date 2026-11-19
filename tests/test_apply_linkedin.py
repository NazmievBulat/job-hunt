"""The LinkedIn worker of `hunt apply`: Easy Apply jobs one at a time under the LinkedIn lock,
three to six minutes apart, and a job whose Easy Apply sends to the company's site.

No model and no browser: the workers are stand-ins that write the report a worker would.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt.apply import LINKEDIN_SKILL, SITE_SKILL, ApplyError, apply, linkedin_command, read_report, site_command
from job_hunt.jobs import JobsList, MoveError
from job_hunt.linkedin_lock import PAUSE, STALE, LinkedInLock, random_pause
from movable_clock import MovableClock
# The fixtures `project` and `jobs` too: a made-up person's project folder and its jobs list.
from test_apply import Resumes, Sites, _found, _job_of, _picked, jobs, no_linkedin, project  # noqa: F401

START = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)


def _lock(project: Path, pause: float = 0.0, **change: object) -> LinkedInLock:
    return LinkedInLock(project / "data", pause=lambda: timedelta(seconds=pause), poll_seconds=0.01, **change)


class LinkedIn:
    """Stands in for the LinkedIn worker: notes when each application starts and ends, whether
    the lock was held meanwhile and how many ran at once, then writes the job's report."""

    def __init__(self, project: Path, reports: dict[int, dict] | None = None, seconds: float = 0.02) -> None:
        self.project, self.reports, self.seconds = project, reports or {}, seconds
        self.ran: list[tuple[int, float, float]] = []
        self.unlocked: list[int] = []
        self.running = self.most = 0
        self._lock = threading.Lock()

    def __call__(self, command: list[str]) -> None:
        job = _job_of(command, LINKEDIN_SKILL)
        lock = self.project / "data" / "linkedin.lock"
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
        began = time.monotonic()
        if not lock.exists() or json.loads(lock.read_text())["job"] != job:
            self.unlocked.append(job)
        time.sleep(self.seconds)
        (self.project / "jobs" / str(job) / "report.json").write_text(
            json.dumps(self.reports.get(job, {"result": "applied"})))
        with self._lock:
            self.running -= 1
            self.ran.append((job, began, time.monotonic()))

    def gaps(self) -> list[float]:
        return [later[1] - earlier[2] for earlier, later in zip(self.ran, self.ran[1:])]


def test_a_linkedin_worker_opens_the_jobs_linkedin_page_with_the_site_workers_narrow_tools(
    project: Path, jobs: JobsList
) -> None:
    job = jobs.get(_picked(jobs, 1, kind="easy_apply"))
    command = linkedin_command(job, project)
    assert command[:3] == ["claude", "--chrome", "-p"]
    arguments = json.loads(command[3].removeprefix(f"/{LINKEDIN_SKILL} "))
    assert arguments["link"] == job.link
    assert arguments["report"] == str(project / "jobs" / str(job.id) / "report.json")
    # The same tools as a company-site worker: its folder, the career, its report and Chrome.
    tools = command[command.index("--allowedTools") + 1]
    assert tools == site_command(job, project)[command.index("--allowedTools") + 1]


def test_one_linkedin_application_at_a_time_each_under_the_lock_beside_the_site_workers(
    project: Path, jobs: JobsList
) -> None:
    easy = [_picked(jobs, n, kind="easy_apply") for n in range(1, 5)]
    sites = [_picked(jobs, n) for n in range(5, 8)]
    linkedin, site = LinkedIn(project), Sites(project, seconds=0.05)
    summary = apply(jobs, project, run_resume=Resumes(project), run_site=site, run_linkedin=linkedin,
                    linkedin=_lock(project))
    assert [job for job, _, _ in linkedin.ran] == easy
    assert (linkedin.most, linkedin.unlocked) == (1, [])
    # The three site workers ran at once with the LinkedIn worker, not after it.
    assert site.most == 3
    assert {jobs.get(job).state for job in easy + sites} == {"applied"}
    assert summary.applied == 7
    assert not (project / "data" / "linkedin.lock").exists()


def test_two_linkedin_applications_are_three_to_six_minutes_apart(project: Path, jobs: JobsList) -> None:
    easy = [_picked(jobs, n, kind="easy_apply") for n in range(1, 4)]
    pauses: list[float] = []

    def pause() -> timedelta:
        pauses.append(0.15)
        return timedelta(seconds=0.15)

    linkedin = LinkedIn(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=LinkedInLock(project / "data", pause=pause, poll_seconds=0.01))
    assert [job for job, _, _ in linkedin.ran] == easy
    # A pause after each application, the next one only once it is over.
    assert len(pauses) == 3
    assert all(gap >= 0.15 for gap in linkedin.gaps())
    assert PAUSE == (timedelta(minutes=3), timedelta(minutes=6))
    chosen = {random_pause() for _ in range(200)}
    assert all(PAUSE[0] <= each <= PAUSE[1] for each in chosen)
    # At random: not one fixed gap a site could spot.
    assert len(chosen) > 100


def test_the_pause_holds_across_two_hunt_apply_runs(project: Path, jobs: JobsList) -> None:
    first = _picked(jobs, 1, kind="easy_apply")
    linkedin = LinkedIn(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=_lock(project, pause=0.3))
    second = _picked(jobs, 2, kind="easy_apply")
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=_lock(project, pause=0.3))
    assert [job for job, _, _ in linkedin.ran] == [first, second]
    assert linkedin.gaps()[0] >= 0.3


def test_a_lock_another_run_holds_is_respected_until_it_is_released(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    other = _lock(project)
    assert other.take(99) is None
    released = []

    def release_later() -> None:
        time.sleep(0.3)
        released.append(time.monotonic())
        other.release()

    releaser = threading.Thread(target=release_later)
    releaser.start()
    linkedin = LinkedIn(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=_lock(project))
    releaser.join(5)
    assert [ran for ran, _, _ in linkedin.ran] == [job]
    assert linkedin.ran[0][1] >= released[0]
    assert linkedin.unlocked == []


def test_a_fresh_lock_is_respected_and_a_stale_one_taken_back(project: Path) -> None:
    clock = MovableClock(START)
    dead = _lock(project, clock=clock)
    assert dead.take(7) is None
    # The run that held it died: nothing releases it.
    living = _lock(project, clock=clock)
    clock.advance(seconds=STALE.total_seconds() - 60)
    assert living.take(8) is not None
    assert json.loads((project / "data" / "linkedin.lock").read_text())["job"] == 7
    clock.advance(seconds=60)
    assert STALE == timedelta(minutes=30)
    assert living.take(8) is None
    assert json.loads((project / "data" / "linkedin.lock").read_text())["job"] == 8
    # The dead run's release, if it came back, leaves the new holder's lock alone.
    dead.release()
    assert json.loads((project / "data" / "linkedin.lock").read_text())["job"] == 8
    living.release()
    assert not (project / "data" / "linkedin.lock").exists()


def test_a_lock_cut_off_while_written_is_as_old_as_its_file(project: Path) -> None:
    (project / "data" / "linkedin.lock").write_text('{"job": 7, "tak')
    assert _lock(project).take(8) is not None
    later = MovableClock(datetime.now(UTC) + STALE + timedelta(seconds=1))
    assert _lock(project, clock=later).take(8) is None


def test_a_held_lock_waits_no_longer_than_it_has_left_before_it_is_stale(project: Path) -> None:
    clock = MovableClock(START)
    assert _lock(project, clock=clock).take(7) is None
    clock.advance(seconds=STALE.total_seconds() - 5)
    slow_poll = LinkedInLock(project / "data", clock=clock, poll_seconds=60)
    assert slow_poll.take(8) == pytest.approx(5)


def test_a_stale_lock_a_dead_run_left_does_not_stop_hunt_apply(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    (project / "data" / "linkedin.lock").write_text(
        json.dumps({"job": 99, "taken_at": (datetime.now(UTC) - STALE - timedelta(minutes=1)).isoformat()}))
    linkedin = LinkedIn(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=_lock(project))
    assert [ran for ran, _, _ in linkedin.ran] == [job]
    assert jobs.get(job).state == "applied"


def test_a_job_whose_easy_apply_sends_to_the_company_site_becomes_a_company_site_job(
    project: Path, jobs: JobsList
) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    linkedin = LinkedIn(project, {job: {"result": "company_site", "link": " https://careers.example.com/apply/1 "}})
    commands: list[list[str]] = []
    sites = Sites(project)
    summary = apply(jobs, project, run_resume=Resumes(project), run_linkedin=linkedin,
                    run_site=lambda command: (commands.append(command), sites(command)), linkedin=_lock(project))
    moved = jobs.get(job)
    assert (moved.kind, moved.apply_link) == ("company_site", "https://careers.example.com/apply/1")
    # Applied to on the company's site in the same run, at that link, with no try spent.
    assert sites.ran == [job]
    assert json.loads(commands[0][3].removeprefix(f"/{SITE_SKILL} "))["link"] == "https://careers.example.com/apply/1"
    assert (moved.state, moved.failures, summary.applied) == ("applied", 0, 1)
    assert [ran for ran, _, _ in linkedin.ran] == [job]


@pytest.mark.parametrize(("link", "error"), [
    (None, "without its web address"),
    ("careers.example.com/apply", "without its web address"),
    ("https://www.linkedin.com/jobs/view/123", "with a LinkedIn address"),
])
def test_a_company_site_report_needs_the_sites_own_address(tmp_path: Path, link: str | None, error: str) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"result": "company_site", "link": link}))
    with pytest.raises(ApplyError, match=error):
        read_report(report, LINKEDIN_SKILL)


def test_only_the_linkedin_worker_sends_a_job_to_the_company_site(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"result": "company_site", "link": "https://careers.example.com/1"}))
    with pytest.raises(ApplyError, match='"result" is not applied, paused or failed'):
        read_report(report, SITE_SKILL)


def test_only_an_easy_apply_job_moves_to_the_company_site(jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    jobs.move(job, "resume_ready")
    jobs.move(job, "applying")
    with pytest.raises(MoveError, match="not an Easy Apply job"):
        jobs.to_company_site(job, "https://careers.example.com/1")


def test_a_linkedin_worker_that_stops_with_no_report_releases_the_lock_and_its_job_pauses(
    project: Path, jobs: JobsList
) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    tries = []

    def run(command: list[str]) -> None:
        tries.append(_job_of(command, LINKEDIN_SKILL))
        if len(tries) == 1:
            raise ApplyError("the apply-linkedin run failed (1): no message")
        (project / "jobs" / str(job) / "report.json").write_text('{"result": "applied"}')

    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=run, linkedin=_lock(project))
    # No report: it may have been sent, so it pauses for a look and is not tried again.
    assert tries == [job]
    assert jobs.get(job).state == "paused"
    assert "the apply-linkedin run failed" in (jobs.get(job).state_reason or "")
    assert not (project / "data" / "linkedin.lock").exists()


def test_a_run_that_stops_while_the_linkedin_worker_applies_still_releases_the_lock(
    project: Path, jobs: JobsList
) -> None:
    _picked(jobs, 1, kind="easy_apply")

    def broken(command: list[str]) -> None:
        raise RuntimeError("the worker's thread broke")

    with pytest.raises(RuntimeError):
        apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=broken,
              linkedin=_lock(project))
    assert not (project / "data" / "linkedin.lock").exists()
    assert (project / "data" / "linkedin.next").exists()


def test_a_paused_easy_apply_job_goes_on_in_its_tab_through_the_linkedin_worker(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    linkedin = LinkedIn(project, {job: {"result": "paused", "needs": "sign_in", "tab": "LinkedIn", "tab_id": 4}})
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin, run_linkedin=linkedin,
          linkedin=_lock(project))
    assert jobs.get(job).state_reason == 'a sign-in or a new account, in the Chrome tab "LinkedIn"'
    jobs.move(job, "resume_ready")
    commands: list[list[str]] = []
    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin,
          run_linkedin=lambda command: (commands.append(command), LinkedIn(project)(command)),
          linkedin=_lock(project))
    arguments = json.loads(commands[0][3].removeprefix(f"/{LINKEDIN_SKILL} "))
    assert (arguments["tab"], arguments["tab_id"]) == ("LinkedIn", 4)
    assert jobs.get(job).state == "applied"


def test_a_company_site_report_a_stopped_run_left_is_saved(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    (project / "jobs" / str(job)).mkdir()
    (project / "jobs" / str(job) / "resume.pdf").write_bytes(b"%PDF")
    jobs.move(job, "resume_ready")
    jobs.move(job, "applying")
    (project / "jobs" / str(job) / "report.json").write_text(
        json.dumps({"result": "company_site", "link": "https://careers.example.com/1"}))
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites, run_linkedin=no_linkedin, linkedin=_lock(project))
    assert (jobs.get(job).kind, sites.ran, jobs.get(job).state) == ("company_site", [job], "applied")


def test_a_jobs_list_made_before_apply_links_gains_the_column(project: Path) -> None:
    path = project / "data" / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, link TEXT NOT NULL UNIQUE, page_link TEXT,"
                " title TEXT NOT NULL, company TEXT NOT NULL, place TEXT, remote TEXT, board TEXT NOT NULL,"
                " kind TEXT, email_id TEXT, email_date TEXT, area TEXT, description TEXT, page_problem TEXT,"
                " score INTEGER, score_reason TEXT, decision TEXT, state TEXT NOT NULL, state_reason TEXT,"
                " failures INTEGER NOT NULL DEFAULT 0, found_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.commit()
    old.close()
    jobs = JobsList(path)
    job = jobs.add(_found(1), kind="easy_apply")
    assert job is not None
    assert jobs.get(job).apply_link is None
