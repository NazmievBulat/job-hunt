"""A day's plan: picking by score up to the day's number, the limits `hunt apply` keeps to, the
leftovers carried to the next day, the 30-day rule for the same title at the same company, and
the day's summary (`hunt status --today`).

No model, no network and no browser: the workers are stand-ins, and the clock is a movable one.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.apply import LINKEDIN_SKILL, NAP, RESUME_SKILL, SITE_SKILL, apply
from job_hunt.jobs import JobsList, MoveError
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
    for number, score in ((4, 9), (5, 8), (6, 7), (7, 7)):
        _scored(jobs, number, score)
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


def _between_read_and_move(jobs: JobsList, to: str, other: Callable[[int], None], where: str = "_move") -> None:
    """Make `other` run once, from another connection, after `jobs` read a job's state to move it
    to `to` and before it writes the move; with `where`, after that method of `jobs` read it."""
    read = jobs.get
    done: list[int] = []

    def get(job: int):  # type: ignore[no-untyped-def]
        found = read(job)
        caller = sys._getframe(1)
        if caller.f_code.co_name == where and (where != "_move" or caller.f_locals.get("state") == to) and not done:
            done.append(job)
            other(job)
        return found

    jobs.get = get  # type: ignore[method-assign]


@pytest.mark.parametrize("kind", ["company_site", "easy_apply"])
def test_a_skip_between_the_starts_read_and_its_move_keeps_the_job_skipped(
        jobs: JobsList, project: Path, kind: str) -> None:
    one, two = _picked(jobs, 1, kind=kind), _picked(jobs, 2, kind=kind)
    # `hunt skip`, from the chat, with its own connection, as the run starts the first job.
    _between_read_and_move(jobs, "applying",
                           lambda job: JobsList(project / "data" / "jobs.db").move(job, "skipped"))
    worker = Sites(project, skill=LINKEDIN_SKILL if kind == "easy_apply" else SITE_SKILL)
    _apply(jobs, project, Plan(), site=worker, linkedin=worker)
    skipped = one if jobs.get(one).state == "skipped" else two
    assert jobs.get(skipped).state == "skipped"
    # The other one is still applied to, by the same run.
    assert worker.ran == [two if skipped == one else one]


def test_a_skip_as_a_resume_is_kept_leaves_the_run_going(jobs: JobsList, project: Path) -> None:
    one, two = _picked(jobs, 1), _picked(jobs, 2)
    _between_read_and_move(jobs, "resume_ready",
                           lambda job: JobsList(project / "data" / "jobs.db").move(job, "skipped"))
    sites = Sites(project)
    _apply(jobs, project, Plan(), site=sites)
    assert (jobs.get(one).state, jobs.get(two).state) == ("skipped", "applied")
    assert sites.ran == [two]


def test_a_skip_as_the_run_leaves_out_a_title_applied_to_lately_leaves_the_run_going(
        jobs: JobsList, project: Path) -> None:
    one = _picked(jobs, 1)
    two = _scored(jobs, 2, 8, title="QA Engineer 1", company="Example Co 1")
    jobs.move(two, "picked")
    _between_read_and_move(jobs, "skipped",
                           lambda job: JobsList(project / "data" / "jobs.db").move(job, "skipped"))
    sites = Sites(project)
    _apply(jobs, project, Plan(), site=sites)
    assert (jobs.get(one).state, jobs.get(two).state) == ("applied", "skipped")


def test_a_skip_as_the_run_leaves_a_job_for_tomorrow_leaves_the_run_going(jobs: JobsList, project: Path) -> None:
    one, two, three = _picked(jobs, 1), _picked(jobs, 2), _picked(jobs, 3)
    _between_read_and_move(jobs, "", lambda job: JobsList(project / "data" / "jobs.db").move(job, "skipped"),
                           where="later")
    summary = _apply(jobs, project, Plan(daily=1))
    assert [jobs.get(job).state for job in (one, two, three)] == ["applied", "skipped", "resume_ready"]
    assert summary.later == 1


def test_a_start_between_the_skips_read_and_its_move_refuses_the_skip(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    jobs.move(job, "resume_ready")
    chat = JobsList(project / "data" / "jobs.db")
    # The run starts the application after the skip read the job as ready.
    _between_read_and_move(chat, "skipped", lambda job: jobs.move(job, "applying"))
    with pytest.raises(MoveError, match="moved meanwhile"):
        chat.move(job, "skipped")
    assert jobs.get(job).state == "applying"


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
            JobsList(project / "data" / "jobs.db", clock=_clock(jobs)).move(later, "picked")

    resumes = Resumes(project)
    _apply(jobs, project, Plan(), site=run, resumes=resumes)
    # Picked today with a better score, it goes before the one waiting.
    assert sites.ran == [first, later, second]
    assert resumes.ran == [first, second, later]
    assert jobs.get(later).state == "applied"


def test_a_job_picked_while_the_last_application_runs_is_applied_in_the_same_run(
        jobs: JobsList, project: Path) -> None:
    first = _picked(jobs, 1)
    later = _scored(jobs, 2, 9)
    sites = Sites(project)

    def run(command: list[str]) -> None:
        sites(command)
        if _job_of(command, SITE_SKILL) == first:
            JobsList(project / "data" / "jobs.db", clock=_clock(jobs)).move(later, "picked")

    _apply(jobs, project, Plan(), site=run)
    assert sites.ran == [first, later]
    assert jobs.get(later).state == "applied"


def test_a_paused_job_put_back_while_the_run_goes_on_is_taken_by_it(jobs: JobsList, project: Path) -> None:
    paused = _picked(jobs, 1)
    _apply(jobs, project, Plan(), site=Sites(project, {
        paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}]}))
    other = _picked(jobs, 2)
    sites = Sites(project)

    def run(command: list[str]) -> None:
        sites(command)
        if _job_of(command, SITE_SKILL) == other:
            # `hunt answer`, from the chat, with its own connection.
            JobsList(project / "data" / "jobs.db", clock=_clock(jobs)).move(paused, "resume_ready")

    _apply(jobs, project, Plan(), site=run)
    assert sites.ran == [other, paused]
    assert jobs.get(paused).state == "applied"


def test_a_job_going_on_in_its_tab_put_back_during_the_spreads_wait_starts_within_a_minute(
        jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    paused = _picked(jobs, 1)
    _apply(jobs, project, Plan(), site=Sites(project, {
        paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}]}))
    one, two = _picked(jobs, 2), _picked(jobs, 3)
    started: dict[int, datetime] = {}
    sites = Sites(project, {paused: [{"result": "applied"}]})
    waiting = Waiting(clock)

    def nap(seconds: float) -> None:
        waiting(seconds)
        if clock.now >= MORNING + timedelta(minutes=1) and jobs.get(paused).state == "paused":
            # The answer, from the chat: `hunt answer`, with its own connection.
            JobsList(project / "data" / "jobs.db", clock=clock).move(paused, "resume_ready")

    apply(jobs, project, run_resume=Resumes(project), run_site=_started(clock, sites, started, SITE_SKILL),
          run_linkedin=no_linkedin, linkedin=_no_pause(project), sleep=nap, plan=Plan())
    # The first pick waits 15 minutes for the spread; the job answered a minute in does not.
    assert started[paused] <= MORNING + timedelta(minutes=2)
    assert started[paused] < started[one] < started[two]
    assert max(waiting.slept) <= NAP


def test_a_job_put_back_while_another_applies_starts_before_that_one_ends(
        jobs: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The run's nap made short, as the wait for a running worker is in real seconds, and no spread.
    monkeypatch.setattr("job_hunt.apply.NAP", 0.05)
    monkeypatch.setattr("job_hunt.plan.MOST_GAP", timedelta(0))
    paused = _picked(jobs, 1)
    _apply(jobs, project, Plan(), site=Sites(project, {
        paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}]}))
    other = _picked(jobs, 2)
    sites = Sites(project, {paused: [{"result": "applied"}]})
    started = threading.Event()
    seen: list[bool] = []

    def run(command: list[str]) -> None:
        if _job_of(command, SITE_SKILL) == paused:
            started.set()
        else:
            # `hunt answer` from the chat while this one applies, for longer than a nap.
            JobsList(project / "data" / "jobs.db", clock=_clock(jobs)).move(paused, "resume_ready")
            seen.append(started.wait(timeout=5))
        sites(command)

    _apply(jobs, project, Plan(site_workers=2), site=run)
    assert seen == [True]
    assert jobs.get(paused).state == "applied"


def test_a_linkedin_job_going_on_in_its_tab_goes_before_a_better_pick_waiting_for_the_spread(
        jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    paused = _picked(jobs, 1, kind="easy_apply")
    _apply(jobs, project, Plan(), linkedin=Sites(project, {
        paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "LinkedIn"}]},
        skill=LINKEDIN_SKILL))
    better = _scored(jobs, 2, 10, kind="easy_apply")
    jobs.move(better, "picked")
    started: dict[int, datetime] = {}
    linkedin = Sites(project, {paused: [{"result": "applied"}]}, skill=LINKEDIN_SKILL)
    waiting = Waiting(clock)

    def nap(seconds: float) -> None:
        waiting(seconds)
        if clock.now >= MORNING + timedelta(minutes=1) and jobs.get(paused).state == "paused":
            JobsList(project / "data" / "jobs.db", clock=clock).move(paused, "resume_ready")

    apply(jobs, project, run_resume=Resumes(project), run_site=no_linkedin,
          run_linkedin=_started(clock, linkedin, started, LINKEDIN_SKILL), linkedin=_no_pause(project),
          sleep=nap, plan=Plan())
    assert started[paused] <= MORNING + timedelta(minutes=2)
    assert started[paused] < started[better]


def test_a_ready_job_with_no_way_to_apply_known_ends_the_run(jobs: JobsList, project: Path) -> None:
    unknown = jobs.add(_found(1))
    assert unknown is not None
    jobs.score(unknown, 9, "Fits.")
    jobs.move(unknown, "picked")
    _apply(jobs, project, Plan(), site=no_linkedin)
    assert jobs.get(unknown).state == "resume_ready"


def test_a_leftover_due_during_the_run_goes_before_todays_picks(jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    clock.now = MORNING - timedelta(days=1)
    left = _picked(jobs, 1)
    clock.now = MORNING
    first, second = _picked(jobs, 2), _picked(jobs, 3)
    for job in (left, first, second):
        jobs.move(job, "resume_ready")
        (project / "jobs" / str(job)).mkdir(exist_ok=True)
        (project / "jobs" / str(job) / "resume.pdf").write_bytes(b"%PDF-1.4 stand-in")
    jobs.later(left, MORNING + timedelta(minutes=10))
    sites = Sites(project)
    _apply(jobs, project, Plan(daily=2), site=sites)
    assert sites.ran == [first, left]
    assert jobs.get(second).not_before == (MORNING + timedelta(days=1)).isoformat()


def test_nothing_starts_before_the_morning_hour_and_it_waits_for_this_morning(
        jobs: JobsList, project: Path) -> None:
    job = _picked(jobs, 1)
    _clock(jobs).now = MORNING.replace(hour=1)
    _apply(jobs, project, Plan(morning_hour=7))
    assert (jobs.get(job).state, jobs.get(job).not_before) == ("resume_ready", MORNING.isoformat())


def _started(clock: MovableClock, worker: Sites, times: dict[int, datetime], skill: str):  # type: ignore[no-untyped-def]
    def run(command: list[str]) -> None:
        times[_job_of(command, skill)] = clock.now
        worker(command)
    return run


def test_linkedin_and_the_company_sites_share_one_spread(jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    easy = [_picked(jobs, number, kind="easy_apply") for number in (1, 2)]
    site = _picked(jobs, 3)
    times: dict[int, datetime] = {}
    _apply(jobs, project, Plan(), site=_started(clock, Sites(project), times, SITE_SKILL),
           linkedin=_started(clock, Sites(project, skill=LINKEDIN_SKILL), times, LINKEDIN_SKILL))
    assert sorted(times.values()) == [MORNING, MORNING + MOST_GAP, MORNING + 2 * MOST_GAP]
    assert set(times) == {*easy, site}


def test_a_linkedin_leftover_goes_before_a_better_company_site_pick_of_today(
        jobs: JobsList, project: Path) -> None:
    clock = _clock(jobs)
    left = _picked(jobs, 1, kind="easy_apply")
    clock.advance(days=1)
    better = _scored(jobs, 2, 10)
    jobs.move(better, "picked")
    linkedin = Sites(project, skill=LINKEDIN_SKILL)
    sites = Sites(project)
    _apply(jobs, project, Plan(daily=1), site=sites, linkedin=linkedin)
    assert (linkedin.ran, sites.ran) == ([left], [])


def test_a_pick_keeps_the_time_it_was_picked_not_when_it_was_scored(jobs: JobsList) -> None:
    job = _scored(jobs, 1, 9)
    _clock(jobs).advance(days=3)
    jobs.move(job, "picked")
    assert jobs.get(job).picked_at == (MORNING + timedelta(days=3)).isoformat()


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


def test_a_paused_application_of_the_title_holds_and_a_failed_one_does_not(jobs: JobsList, project: Path) -> None:
    from job_hunt.plan import applied_lately

    paused, failed = _picked(jobs, 1), _picked(jobs, 2)
    _apply(jobs, project, Plan(), site=Sites(project, {
        paused: [{"result": "paused", "needs": "question", "question": "Visa?", "tab": "Acme"}],
        failed: [{"result": "failed", "reason": "closed"}]}))
    assert (jobs.get(paused).state, jobs.get(failed).state) == ("paused", "failed")
    same_as_paused = jobs.add(_found(1, link="https://jobs.example.com/p"), kind="company_site")
    same_as_failed = jobs.add(_found(2, link="https://jobs.example.com/f"), kind="company_site")
    assert same_as_paused is not None and same_as_failed is not None
    now = jobs.now()
    assert applied_lately(jobs, jobs.get(same_as_paused), now).id == paused  # type: ignore[union-attr]
    assert applied_lately(jobs, jobs.get(same_as_failed), now) is None


def test_two_scored_jobs_of_one_title_at_one_company_are_picked_once(jobs: JobsList) -> None:
    one = _scored(jobs, 1, 9)
    two = jobs.add(_found(1, link="https://jobs.example.com/same"), kind="company_site")
    assert two is not None
    jobs.score(two, 8, "Fits.")
    picked = auto_pick(jobs, Plan(auto_from=7))
    assert ([job.id for job in picked.picked], picked.same) == ([one], 1)
    assert jobs.get(two).state == "skipped"


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


def test_the_jobs_above_the_score_past_the_days_number_count_as_left_for_tomorrow(jobs: JobsList) -> None:
    for number, score in ((1, 9), (2, 8), (3, 5)):
        _scored(jobs, number, score)
    assert today(jobs, jobs.now(), auto_from=7).tomorrow == 2
    assert today(jobs, jobs.now()).tomorrow == 0


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


def test_unfinished_applications_are_not_asked_while_a_hunt_apply_runs(jobs: JobsList, project: Path) -> None:
    import fcntl

    from job_hunt.questions import questions

    data = project / "data"
    _picked(jobs, 1)
    (project / "jobs").mkdir(exist_ok=True)
    # A run spread over the day holds the lock until the summary hour, and takes the job itself.
    with (project / "jobs" / "apply.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "Unfinished applications" not in questions(jobs, data)
    assert "Unfinished applications (1): run hunt apply" in questions(jobs, data)


def test_a_hunt_apply_starting_as_hunt_questions_looks_at_its_lock_still_runs(
        jobs: JobsList, project: Path) -> None:
    import fcntl

    job = _picked(jobs, 1)
    (project / "jobs").mkdir(exist_ok=True)
    probed = threading.Event()

    def probe() -> None:
        # As `apply_running` holds it: a moment, then let go.
        with (project / "jobs" / "apply.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            probed.set()
            time.sleep(0.1)

    looking = threading.Thread(target=probe)
    looking.start()
    probed.wait(timeout=5)
    _apply(jobs, project, Plan())
    looking.join()
    assert jobs.get(job).state == "applied"


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


def test_jobs_picked_by_a_later_morning_run_are_told_once_to_the_running_session(
        jobs: JobsList, project: Path) -> None:
    from job_hunt.morning import morning

    data = project / "data"
    told: list[str] = []

    def noon() -> str:
        return morning(jobs, data, find=lambda: None, score=lambda: None, start=lambda: told.append("started"),
                       running=lambda: True, clock=_clock(jobs), pick=lambda: auto_pick(jobs, Plan(auto_from=7)),
                       tell=lambda: told.append("told"), auto_from=7)

    _scored(jobs, 1, 9)
    noon()
    # The day's first run started today's session; the morning's applications ended since.
    assert told == ["started"]
    for job in jobs.in_states("picked"):
        jobs.move(job.id, "skipped")
    _scored(jobs, 2, 9), _scored(jobs, 3, 8)
    assert noon() == "The job-hunt session already runs: told it of 2 jobs picked by their score."
    assert told == ["started", "told"]
    # Not told again for the same picks.
    assert noon().startswith("The job-hunt session already runs: it asks")
    assert told == ["started", "told"]


def test_a_tell_that_fails_is_logged_for_the_next_questions(jobs: JobsList, project: Path) -> None:
    from job_hunt.morning import morning
    from job_hunt.questions import questions

    data = project / "data"

    def tell() -> None:
        raise OSError("tmux is not installed or not on the PATH")

    def run() -> str:
        return morning(jobs, data, find=lambda: None, score=lambda: None, start=lambda: None, running=lambda: True,
                       clock=_clock(jobs), pick=lambda: auto_pick(jobs, Plan(auto_from=7)), tell=tell, auto_from=7)

    _scored(jobs, 1, 9)
    run()
    _scored(jobs, 2, 9)
    assert run() == "The job-hunt session already runs: it asks for anything new at its next hunt questions."
    said = questions(jobs, data, auto_from=7)
    assert "The morning run had a problem:" in said and "tmux is not installed" in said


def _settings(project: Path, **plan: object) -> None:
    import json

    (project / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7,
        **plan}))


def test_hunt_apply_keeps_to_the_settings_plan(
        hunt: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "JobsList", lambda path: JobsList(path, clock=_clock(hunt)))
    monkeypatch.setattr(cli, "run_resume", Resumes(project))
    monkeypatch.setattr(cli, "run_site", Sites(project))
    _settings(project, daily_applications=1, site_workers=2, linkedin_daily=0, auto_apply_from=8)
    assert cli.day_plan() == Plan(daily=1, linkedin_daily=0, site_workers=2, morning_hour=7, summary_hour=18,
                                  auto_from=8)
    one, two = _picked(hunt, 1), _picked(hunt, 2)
    assert cli.main(["apply"]) == 0
    assert capsys.readouterr().out == "2 resumes written; 1 applied, 0 paused, 0 failed, 1 left for later\n"
    assert (hunt.get(one).state, hunt.get(two).not_before) == ("applied", (MORNING + timedelta(days=1)).isoformat())
    (project / "data" / "settings.json").write_text("not json")
    assert cli.day_plan() is None


def test_hunt_morning_picks_by_the_settings_score_and_hunt_skip_leaves_one_out(
        hunt: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "JobsList", lambda path: JobsList(path, clock=_clock(hunt)))
    monkeypatch.setattr(cli, "find_step", lambda: None)
    monkeypatch.setattr(cli, "score_step", lambda: None)
    monkeypatch.setattr(cli, "verdict", lambda: None)
    monkeypatch.setattr(cli, "remote_desktop_ready", lambda machine: False)
    started: list[Path] = []
    monkeypatch.setattr(cli, "start_session", started.append)
    _settings(project, auto_apply_from=8)
    nine, ten, six = _scored(hunt, 1, 9), _scored(hunt, 2, 10), _scored(hunt, 3, 6)
    assert cli.main(["morning"]) == 0
    assert [hunt.get(job).state for job in (nine, ten, six)] == ["picked", "picked", "skipped"]
    assert started == [project]
    capsys.readouterr()
    assert cli.main(["questions"]) == 0
    said = capsys.readouterr().out
    assert "Applying to 2 jobs today (score 8 and up). Reply skip 3 12 to leave some out." in said
    assert " 1. 10  QA Engineer 2" in said and " 2.  9  QA Engineer 1" in said
    assert "New jobs" not in said
    assert cli.main(["skip", "2"]) == 0
    assert capsys.readouterr().out == "skipped 2: QA Engineer 1 at Example Co 1\n"
    assert hunt.get(nine).state == "skipped"
    assert cli.main(["questions"]) == 0
    assert "Applying to" not in capsys.readouterr().out
    # /jobs shows them again on its own, numbered anew for hunt skip.
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.startswith(
        "Applying to 1 job today (score 8 and up). Reply skip 3 12 to leave some out.\n 1. 10  QA Engineer 2")
    # The noon run picks one more while today's session runs: it is told, to run hunt questions.
    told: list[str] = []
    monkeypatch.setattr(cli, "session_running", lambda: True)
    monkeypatch.setattr(cli, "tell_session", lambda root, text: told.append(text))
    _scored(hunt, 4, 9)
    assert cli.main(["morning"]) == 0
    assert told == [cli.TELL_JOBS]
    assert capsys.readouterr().out == "The job-hunt session already runs: told it of 1 job picked by their score.\n"


def test_hunt_settings_keeps_the_days_plan_it_was_not_given(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from job_hunt.settings import read_settings

    monkeypatch.setattr(cli, "DATA", project / "data")
    _settings(project, daily_applications=100, auto_apply_from=7, site_workers=5, linkedin_daily=30)
    given = {"titles": ["SDET"], "areas": [], "remote": "include", "lowest_salary": None, "morning_hour": 7}
    assert cli.main(["settings", json.dumps(given)]) == 0
    saved = read_settings(project / "data" / "settings.json")
    assert (saved.titles, saved.daily_applications, saved.auto_apply_from, saved.site_workers,
            saved.linkedin_daily) == (["SDET"], 100, 7, 5, 30)

