"""A day's plan: picking by score up to the day's number, the limits `hunt apply` keeps to, the
leftovers carried to the next day, the 30-day rule for the same title at the same company, and
the day's summary (`hunt status --today`).

No model, no network and no browser: the workers are stand-ins, and the clock is a movable one.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.apply import LINKEDIN_SKILL, RESUME_SKILL, SITE_SKILL, apply
from job_hunt.jobs import JobsList
from job_hunt.plan import MOST_GAP, RESUME_WORKERS, Plan, auto_pick, gap, order, room, today
from movable_clock import MovableClock
# The fixtures `project` and `jobs` too: a made-up person's project folder and its jobs list.
from test_apply import (  # noqa: F401
    Resumes, Sites, Waiting, _found, _job_of, _no_pause, _picked, hunt, jobs, no_linkedin, project,
)

MORNING = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def utc(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """The computer's own time zone, so the morning and summary hours are those of the clock."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _clock(jobs: JobsList) -> MovableClock:
    return jobs._clock  # type: ignore[attr-defined,no-any-return]


def _scored(jobs: JobsList, number: int, score: int, kind: str = "company_site", **change: object) -> int:
    job = jobs.add(_found(number, **change), kind=kind)
    assert job is not None
    jobs.score(job, score, "Fits.")
    return job


def _apply(jobs: JobsList, project: Path, plan: Plan, *, site: object = None, linkedin: object = no_linkedin,
           resumes: object = None):  # type: ignore[no-untyped-def]
    return apply(jobs, project, run_resume=resumes or Resumes(project), run_site=site or Sites(project),
                 run_linkedin=linkedin, linkedin=_no_pause(project), sleep=Waiting(_clock(jobs)), plan=plan)


# Picking by score.

def test_the_jobs_at_or_above_the_score_are_picked_best_first_up_to_the_days_number(jobs: JobsList) -> None:
    nine, seven, eight, six, ten = (_scored(jobs, number, score) for number, score in enumerate((9, 7, 8, 6, 10), 1))
    picked = auto_pick(jobs, Plan(daily=3, auto_from=7))
    assert [job.id for job in picked.picked] == [ten, nine, eight]
    assert [jobs.get(job).state for job in (seven, six)] == ["scored", "skipped"]
    assert (picked.below, picked.later) == (1, 1)
    assert str(picked) == "3 jobs picked by their score; 1 below the score skipped; 1 left for the next day"
    # The next day, the one left over is picked first.
    _clock(jobs).advance(days=1)
    for job in (ten, nine, eight):
        jobs.move(job, "resume_ready")
        jobs.move(job, "applying")
        jobs.move(job, "applied")
    assert [job.id for job in auto_pick(jobs, Plan(daily=3, auto_from=7)).picked] == [seven]


def test_nothing_is_picked_by_itself_when_the_person_picks(jobs: JobsList) -> None:
    job = _scored(jobs, 1, 10)
    assert auto_pick(jobs, Plan(auto_from=None)).picked == []
    assert jobs.get(job).state == "scored"


def test_the_days_number_counts_the_jobs_started_today_and_the_picks_waiting(jobs: JobsList) -> None:
    started, waiting = _picked(jobs, 1), _picked(jobs, 2)
    jobs.move(started, "resume_ready")
    jobs.move(started, "applying")
    jobs.started_on("example1.com", started)
    held = _picked(jobs, 3)
    jobs.move(held, "resume_ready")
    jobs.later(held, MORNING + timedelta(days=1))
    # The pick held for tomorrow is tomorrow's.
    assert room(jobs, Plan(daily=5), jobs.now()) == 3
    _scored(jobs, 4, 9), _scored(jobs, 5, 8), _scored(jobs, 6, 7), _scored(jobs, 7, 7)
    assert len(auto_pick(jobs, Plan(daily=5, auto_from=7)).picked) == 3
    assert waiting in {job.id for job in jobs.in_states("picked")}


def test_skip_leaves_out_a_job_picked_by_its_score(jobs: JobsList, project: Path) -> None:
    one, two = _scored(jobs, 1, 9), _scored(jobs, 2, 8)
    picked = auto_pick(jobs, Plan(auto_from=7)).picked
    jobs.show(picked)
    jobs.decide([2], "skipped")
    _apply(jobs, project, Plan(auto_from=7))
    assert (jobs.get(one).state, jobs.get(two).state) == ("applied", "skipped")


def test_a_job_skipped_while_its_resume_is_written_is_not_applied_to(jobs: JobsList, project: Path) -> None:
    one, two = _picked(jobs, 1), _picked(jobs, 2)
    resumes = Resumes(project)

    def write(command: list[str]) -> None:
        resumes(command)
        if _job_of(command, RESUME_SKILL) == two:
            # `hunt skip`, from the chat, with its own connection.
            JobsList(project / "data" / "jobs.db").move(two, "skipped")

    sites = Sites(project)
    _apply(jobs, project, Plan(), resumes=write, site=sites)
    assert sites.ran == [one]
    assert jobs.get(two).state == "skipped"


def test_a_job_skipped_while_others_are_applied_to_is_not_started(jobs: JobsList, project: Path) -> None:
    one, two = _picked(jobs, 1), _picked(jobs, 2)
    sites = Sites(project)

    def run(command: list[str]) -> None:
        sites(command)
        other = JobsList(project / "data" / "jobs.db")
        if other.get(two).state == "resume_ready":
            other.move(two, "skipped")

    _apply(jobs, project, Plan(site_workers=1), site=run)
    assert sites.ran == [one]


# The plan's limits.

class Counted:
    """Stands in for a resume worker that takes a moment, counting how many run at once."""

    def __init__(self, project: Path) -> None:
        self.resumes, self.running, self.most = Resumes(project), 0, 0
        self._lock = threading.Lock()

    def __call__(self, command: list[str]) -> None:
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(0.02)
        self.resumes(command)
        with self._lock:
            self.running -= 1


def test_at_most_five_resumes_are_written_at_once(jobs: JobsList, project: Path) -> None:
    for number in range(1, 13):
        _picked(jobs, number)
    resumes = Counted(project)
    _apply(jobs, project, Plan(daily=12), resumes=resumes)
    assert RESUME_WORKERS == 5
    assert resumes.most == 5
    assert len(jobs.in_states("applied")) == 12


class Running:
    """A clock that runs as the real one does, from the morning: the coordinator waits in real
    seconds while workers apply."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def __call__(self) -> datetime:
        return MORNING + timedelta(seconds=time.monotonic() - self.started)


@pytest.mark.parametrize("workers, picks", [(2, 4), (5, 7)])
def test_at_most_the_settings_site_workers_apply_at_once(
        project: Path, monkeypatch: pytest.MonkeyPatch, workers: int, picks: int) -> None:
    # The spread made short: each application takes longer than five of its starts.
    monkeypatch.setattr("job_hunt.plan.MOST_GAP", timedelta(seconds=0.05))
    jobs = JobsList(project / "data" / "jobs.db", clock=Running())
    for number in range(1, picks + 1):
        _picked(jobs, number)
    sites = Sites(project, seconds=0.6)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites, run_linkedin=no_linkedin,
          sleep=time.sleep, plan=Plan(daily=picks, site_workers=workers))
    assert sites.most == workers
    assert len(jobs.in_states("applied")) == picks


def test_linkedin_easy_apply_stops_at_its_daily_number_and_the_rest_wait_for_the_morning(
        jobs: JobsList, project: Path) -> None:
    easy = [_picked(jobs, number, kind="easy_apply") for number in range(1, 4)]
    summary = _apply(jobs, project, Plan(linkedin_daily=2), linkedin=Sites(project, skill=LINKEDIN_SKILL))
    assert [jobs.get(job).state for job in easy] == ["applied", "applied", "resume_ready"]
    assert jobs.get(easy[2]).not_before == (MORNING + timedelta(days=1)).isoformat()
    assert summary.later == 1


def test_no_application_starts_past_the_days_number(jobs: JobsList, project: Path) -> None:
    picked = [_picked(jobs, number) for number in range(1, 4)]
    picked.append(_picked(jobs, 4, kind="easy_apply"))
    _apply(jobs, project, Plan(daily=2), linkedin=Sites(project, skill=LINKEDIN_SKILL))
    assert len(jobs.in_states("applied")) == 2
    assert {job.not_before for job in jobs.in_states("resume_ready")} == {
        (MORNING + timedelta(days=1)).isoformat()}


def test_nothing_starts_after_the_summary_hour(jobs: JobsList, project: Path) -> None:
    job = _picked(jobs, 1)
    _clock(jobs).now = MORNING.replace(hour=18)
    summary = _apply(jobs, project, Plan(summary_hour=18))
    assert (jobs.get(job).state, jobs.get(job).not_before) == (
        "resume_ready", (MORNING + timedelta(days=1)).isoformat())
    assert summary.later == 1


def test_a_job_going_on_in_its_tab_after_done_is_not_held_by_the_end_of_the_day(
        jobs: JobsList, project: Path) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"},
                                  {"result": "applied"}]})
    _apply(jobs, project, Plan(), site=sites)
    jobs.move(job, "resume_ready")
    _clock(jobs).now = MORNING.replace(hour=20)
    _apply(jobs, project, Plan(summary_hour=18, daily=1), site=sites)
    assert jobs.get(job).state == "applied"


def test_the_starts_are_spread_over_what_is_left_of_the_day(jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    clock.now = MORNING.replace(hour=17, minute=48)
    picked = [_picked(jobs, number) for number in range(1, 4)]
    started: list[datetime] = []
    sites = Sites(project)

    def run(command: list[str]) -> None:
        started.append(clock.now)
        sites(command)

    _apply(jobs, project, Plan(summary_hour=18), site=run)
    # Twelve minutes for three: six minutes apart, and the third would start at 18:00.
    assert started == [clock.now.replace(hour=17, minute=48), clock.now.replace(hour=17, minute=54)]
    assert jobs.get(picked[2]).not_before == (MORNING + timedelta(days=1)).isoformat()


def test_a_job_picked_while_the_run_waits_gets_its_resume_and_is_applied_in_the_same_run(
        jobs: JobsList, project: Path) -> None:
    first, second = _picked(jobs, 1), _picked(jobs, 2)
    sites = Sites(project)
    later = _scored(jobs, 3, 9)

    def run(command: list[str]) -> None:
        sites(command)
        if _job_of(command, SITE_SKILL) == first:
            # The noon morning run picks it by its score, with its own connection.
            JobsList(project / "data" / "jobs.db").move(later, "picked")

    resumes = Resumes(project)
    _apply(jobs, project, Plan(), site=run, resumes=resumes)
    # Picked today with a better score, it goes before the one waiting.
    assert sites.ran == [first, later, second]
    assert resumes.ran == [first, second, later]
    assert jobs.get(later).state == "applied"


def test_the_spread_is_at_most_fifteen_minutes() -> None:
    plan = Plan(summary_hour=18)
    assert MOST_GAP == timedelta(minutes=15)
    assert gap(plan, MORNING, 3) == MOST_GAP
    assert gap(plan, MORNING, 110) == timedelta(minutes=6)
    assert gap(plan, MORNING, 0) == timedelta(0)


def test_a_job_left_over_goes_first_the_next_day(jobs: JobsList, project: Path) -> None:
    old = _picked(jobs, 1)
    _apply(jobs, project, Plan(daily=1))
    assert jobs.get(old).state == "applied"
    left = _picked(jobs, 2)
    _apply(jobs, project, Plan(daily=1))
    assert jobs.get(left).state == "resume_ready"
    _clock(jobs).advance(days=1)
    better = _scored(jobs, 3, 10)
    jobs.move(better, "picked")
    sites = Sites(project)
    _apply(jobs, project, Plan(daily=1), site=sites)
    assert sites.ran == [left]
    assert [job.id for job in order([jobs.get(better), jobs.get(left)])] == [left, better]


# Never the same title at the same company twice in 30 days.

def test_the_same_title_at_the_same_company_from_another_source_is_left_out(jobs: JobsList, project: Path) -> None:
    first = _picked(jobs, 1)
    _apply(jobs, project, Plan())
    assert jobs.get(first).state == "applied"
    _clock(jobs).advance(days=29)
    again = jobs.add(_found(1, link="https://boards.greenhouse.io/example/jobs/9", title=" qa engineer  1",
                            company="EXAMPLE Co 1", source="greenhouse"), kind="company_site")
    assert again is not None
    jobs.score(again, 9, "Fits.")
    # Not picked by itself either.
    assert auto_pick(jobs, Plan(auto_from=7)).same == 1
    assert jobs.get(again).state == "skipped"
    jobs.move(again, "picked")
    sites = Sites(project)
    summary = _apply(jobs, project, Plan(), site=sites)
    assert sites.ran == []
    assert jobs.get(again).state == "skipped"
    assert summary.left_out == 1
    assert str(summary).endswith(", 1 left out as applied to lately")


def test_the_same_title_may_be_applied_to_again_after_thirty_days(jobs: JobsList, project: Path) -> None:
    first = _picked(jobs, 1)
    _apply(jobs, project, Plan())
    _clock(jobs).advance(days=31)
    again = jobs.add(_found(1, link="https://jobs.example.com/again"), kind="company_site")
    assert again is not None
    jobs.score(again, 9, "Fits.")
    jobs.move(again, "picked")
    _apply(jobs, project, Plan())
    assert (jobs.get(first).state, jobs.get(again).state) == ("applied", "applied")


def test_two_picks_of_one_title_at_one_company_apply_once(jobs: JobsList, project: Path) -> None:
    one = _picked(jobs, 1)
    two = jobs.add(_found(1, link="https://jobs.example.com/same"), kind="company_site")
    assert two is not None
    jobs.score(two, 8, "Fits.")
    jobs.move(two, "picked")
    _apply(jobs, project, Plan())
    assert (jobs.get(one).state, jobs.get(two).state) == ("applied", "skipped")


# The day's summary.

def test_the_day_is_counted_by_kind_by_what_waits_and_by_failure(jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    yesterday = _picked(jobs, 1)
    _apply(jobs, project, Plan())
    clock.advance(days=1)
    sites = {number: _picked(jobs, number) for number in range(2, 5)}
    easy = _picked(jobs, 5, kind="easy_apply")
    paused = {number: _picked(jobs, number) for number in range(6, 9)}
    failed = _picked(jobs, 9)
    reports = {
        paused[6]: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}],
        paused[7]: [{"result": "paused", "needs": "check_sent", "tab": "Acme"}],
        paused[8]: [{"result": "paused", "needs": "question", "question": "Salary?", "tab": "Other"}],
        failed: [{"result": "failed", "reason": "the posting is closed"}],
    }

    class Slow(Sites):
        def __call__(self, command: list[str]) -> None:
            clock.advance(seconds=240)
            super().__call__(command)

    _apply(jobs, project, Plan(), site=Slow(project, reports), linkedin=Slow(project, skill=LINKEDIN_SKILL))
    left = _picked(jobs, 10)
    jobs.move(left, "resume_ready")
    jobs.later(left, clock.now + timedelta(days=1))
    counted = today(jobs, clock.now)
    assert counted.lines() == [
        "Today: 4 applications sent, 3 on company sites and 1 by LinkedIn Easy Apply; 3 wait for you "
        "(2 for an answer, 1 for a look in Chrome); 1 failed; 1 left for tomorrow.",
        "An application took 4 minutes on average.",
        "Failed today (1):",
        f"  {failed}. QA Engineer 9 at Example Co 9: the posting is closed",
    ]
    assert jobs.get(yesterday).state == "applied"
    assert jobs.get(sites[2]).state == jobs.get(easy).state == "applied"


def test_a_day_with_nothing_says_so(jobs: JobsList) -> None:
    assert today(jobs, jobs.now()).lines() == ["Today: 0 applications sent; 0 failed."]


def test_hunt_status_today_prints_the_days_summary(
        hunt: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "JobsList", lambda path: JobsList(path, clock=_clock(hunt)))
    sent, paused = _picked(hunt, 1), _picked(hunt, 2)
    sites = Sites(project, {paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}]})
    _apply(hunt, project, Plan(), site=sites)
    assert cli.main(["status", "--today"]) == 0
    assert capsys.readouterr().out == (
        "Today: 1 applications sent, 1 on company sites; 1 wait for you (1 for an answer); 0 failed.\n"
        "An application took under a minute on average.\n"
        "Paused (1):\n"
        f'  {paused}. QA Engineer 2 at Example Co 2: needs an answer to "Visa?", in the Chrome tab "Acme" '
        f'(then: hunt answer {paused} "<the answer>")\n')
    assert hunt.get(sent).state == "applied"


# Told in the morning, asking nothing.

def test_the_picks_by_score_are_told_once_numbered_for_skip_and_ask_nothing(jobs: JobsList, project: Path) -> None:
    from job_hunt.questions import anything_waiting, questions

    data = project / "data"
    one, two, low = _scored(jobs, 1, 9), _scored(jobs, 2, 7), _scored(jobs, 3, 5)
    auto_pick(jobs, Plan(auto_from=7))
    said = questions(jobs, data, auto_from=7)
    assert "Applying to 2 jobs today (score 7 and up). Reply skip 3 12 to leave some out.\n 1.  9  QA Engineer 1" in said
    assert "New jobs" not in said
    # Told once; the job below the score was skipped and is not asked about.
    assert "Applying to" not in questions(jobs, data, auto_from=7)
    assert jobs.get(low).state == "skipped"
    jobs.decide([2], "skipped")
    assert jobs.get(two).state == "skipped"
    # One more picked later in the day tells them again, numbered anew.
    _scored(jobs, 4, 10)
    auto_pick(jobs, Plan(auto_from=7))
    assert "Applying to 2 jobs today" in questions(jobs, data, auto_from=7)
    assert jobs.get(one).state == "picked"
    # A scored job above the score left for the next day waits for no one.
    jobs.move(one, "skipped")
    jobs.move(4, "skipped")
    _scored(jobs, 5, 8)
    assert anything_waiting(jobs, data, auto_from=7) is False
    assert anything_waiting(jobs, data) is True


def test_the_morning_run_picks_by_score_also_with_nothing_new(jobs: JobsList, project: Path) -> None:
    from job_hunt.morning import morning

    data = project / "data"
    left = _scored(jobs, 1, 9)
    picks: list[str] = []
    said = morning(jobs, data, find=lambda: None, score=lambda: None, start=lambda: None, running=lambda: False,
                   clock=_clock(jobs), pick=lambda: picks.append(str(auto_pick(jobs, Plan(auto_from=7)))),
                   auto_from=7)
    assert picks == ["1 jobs picked by their score"]
    assert jobs.get(left).state == "picked"
    assert said.startswith("Started the job-hunt session")


def test_hunt_apply_keeps_to_the_settings_plan(
        hunt: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    (project / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7,
        "daily_applications": 1, "site_workers": 2, "linkedin_daily": 0, "auto_apply_from": 8}))
    assert cli.day_plan() == Plan(daily=1, linkedin_daily=0, site_workers=2, morning_hour=7, summary_hour=18,
                                  auto_from=8)
    (project / "data" / "settings.json").write_text("not json")
    assert cli.day_plan() is None
