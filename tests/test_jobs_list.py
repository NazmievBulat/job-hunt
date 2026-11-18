"""The jobs list, data/jobs.db: one row per job, never the same job twice, and its states."""

from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt.jobs import MOVES, STATES, FoundJob, JobsList, MoveError, clean_link
from movable_clock import MovableClock


def _found(link: str, **change: object) -> FoundJob:
    job = {
        "link": link,
        "title": "Senior QA Automation Engineer",
        "company": "Example Labs",
        "place": "Durham, NC",
        "remote": "hybrid",
        "board": "linkedin",
        "email_id": "18c0ffee00000001",
        "email_date": "2026-11-17T07:12:00+00:00",
        "area": "Raleigh-Durham-Chapel Hill",
    }
    return FoundJob(**{**job, **change})


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


@pytest.fixture
def jobs(tmp_path: Path, clock: MovableClock) -> JobsList:
    return JobsList(tmp_path / "jobs.db", clock=clock)


@pytest.mark.parametrize(
    ("link", "clean"),
    [
        (
            "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D%3D&refId=xyz&lipi=urn%3Ali"
            "&midToken=AQ&midSig=1&trk=eml-email_job_alert_digest_01-job_card-0-view_job&trkEmail=eml",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        (
            "https://www.linkedin.com/jobs/view/senior-qa-automation-engineer-at-example-labs-4012345678?trk=public",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        ("https://LinkedIn.com/jobs/view/4012345678#top", "https://www.linkedin.com/jobs/view/4012345678"),
        (
            "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=sdet",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        (
            "https://www.indeed.com/rc/clk/dl?jk=1a2b3c4d5e6f7a8b&from=ja&qd=xyz&rd=abc&tk=1hx&alid=ff&bb=zz",
            "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
        ),
        (
            "https://indeed.com/viewjob?from=jasx&jk=1a2b3c4d5e6f7a8b&tk=1hx#apply",
            "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
        ),
        (
            "https://www.indeed.com/pagead/clk?mo=r&ad=-6NYlbfkN0&jk=1a2b3c4d5e6f7a8b&from=ja",
            "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
        ),
        (
            "https://Jobs.Example.com/careers/1234/?utm_source=alert&utm_medium=email&gh_src=x&ref=mail&id=7#apply",
            "https://jobs.example.com/careers/1234?id=7",
        ),
        # A fragment that is a route of the page names the job; it stays.
        ("https://careers.example.com/#/jobs/123?utm_source=x&id=9", "https://careers.example.com/#/jobs/123?id=9"),
        ("https://careers.example.com/#/jobs/123?utm_source=y", "https://careers.example.com/#/jobs/123"),
        ("https://careers.example.com/jobs#!/456", "https://careers.example.com/jobs#!/456"),
    ],
)
def test_the_tracking_parts_of_a_link_are_removed(link: str, clean: str) -> None:
    assert clean_link(link) == clean


@pytest.mark.parametrize("name", ["reference", "jobId", "gh_jid", "requisition"])
def test_jobs_that_differ_only_in_a_parameter_that_names_the_job_stay_two_jobs(jobs: JobsList, name: str) -> None:
    first = jobs.add(_found(f"https://careers.example.com/job?{name}=JR-1&utm_source=alert"))
    second = jobs.add(_found(f"https://careers.example.com/job?{name}=JR-2&utm_source=alert"))
    assert first is not None and second is not None and first != second
    assert jobs.get(first).link == f"https://careers.example.com/job?{name}=JR-1"


def test_a_job_is_also_known_by_the_address_its_page_ended_at(jobs: JobsList) -> None:
    job = jobs.add(_found("https://click.example.com/t/aaa"), page_link="https://jobs.example.com/careers/1234?utm_source=x",
                   description="We test robots.", kind="company_site")
    assert jobs.has("https://jobs.example.com/careers/1234")
    saved = jobs.get(job)
    assert (saved.link, saved.page_link, saved.description, saved.kind, saved.page_problem) == (
        "https://click.example.com/t/aaa", "https://jobs.example.com/careers/1234", "We test robots.", "company_site", None)
    # A page address that is the link itself is not kept twice.
    same = jobs.add(_found("https://jobs.example.com/careers/9"), page_link="https://jobs.example.com/careers/9/")
    assert jobs.get(same).page_link is None


def test_two_jobs_whose_pages_end_at_one_page_are_two_jobs_unless_title_and_company_are_the_same(jobs: JobsList) -> None:
    # An expired posting often sends its link to the careers page.
    careers = "https://jobs.example.com/careers"
    first = jobs.add(_found("https://click.example.com/t/1", title="QA Automation Engineer"), page_link=careers)
    other = jobs.add(_found("https://click.example.com/t/2", title="SDET"), page_link=careers)
    elsewhere = jobs.add(_found("https://click.example.com/t/4", title="QA Automation Engineer", company="Globex Example"),
                         page_link=careers)
    again = jobs.add(_found("https://click.example.com/t/3", title="qa automation engineer"), page_link=careers)
    # Each job keeps the page, so a later link to the second job is known too, not only one to the first.
    other_again = jobs.add(_found("https://click.example.com/t/5", title="SDET"), page_link=careers)
    assert first is not None and other is not None and elsewhere is not None and again is None and other_again is None
    assert (jobs.get(first).page_link, jobs.get(other).page_link) == (careers, careers)
    assert len(jobs.all()) == 3


def test_the_same_job_from_two_alerts_is_saved_once(jobs: JobsList) -> None:
    first = jobs.add(_found("https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=one"))
    again = jobs.add(
        _found(
            "https://www.linkedin.com/jobs/view/senior-qa-automation-engineer-at-example-labs-4012345678?trk=two",
            email_id="18c0ffee00000002",
        )
    )
    assert first is not None and again is None
    assert [job.id for job in jobs.all()] == [first]
    assert jobs.get(first).email_id == "18c0ffee00000001"
    assert jobs.has("https://www.linkedin.com/jobs/view/4012345678?trk=three")


def test_a_new_job_keeps_what_the_alert_gave(jobs: JobsList, clock: MovableClock) -> None:
    job = jobs.get(jobs.add(_found("https://www.linkedin.com/jobs/view/4012345678")))
    assert (job.link, job.title, job.company, job.place, job.remote, job.board) == (
        "https://www.linkedin.com/jobs/view/4012345678",
        "Senior QA Automation Engineer",
        "Example Labs",
        "Durham, NC",
        "hybrid",
        "linkedin",
    )
    assert (job.email_id, job.email_date, job.area) == (
        "18c0ffee00000001", "2026-11-17T07:12:00+00:00", "Raleigh-Durham-Chapel Hill")
    assert (job.state, job.kind, job.description, job.score, job.score_reason, job.decision) == (
        "new", None, None, None, None, None)
    assert job.found_at == job.updated_at == clock.now.isoformat()


def test_the_page_is_saved_with_its_kind_or_the_reason_it_could_not_be_read(jobs: JobsList) -> None:
    read = jobs.add(_found("https://www.linkedin.com/jobs/view/1"), description="We test things.", kind="easy_apply")
    unread = jobs.add(_found("https://www.linkedin.com/jobs/view/2"), problem="HTTP 999")
    assert (jobs.get(read).description, jobs.get(read).kind, jobs.get(read).page_problem) == (
        "We test things.", "easy_apply", None)
    assert (jobs.get(unread).description, jobs.get(unread).kind, jobs.get(unread).page_problem) == (
        None, None, "HTTP 999")


def test_a_kind_other_than_easy_apply_or_company_site_is_refused(jobs: JobsList) -> None:
    with pytest.raises(ValueError, match="kind"):
        jobs.add(_found("https://www.linkedin.com/jobs/view/1"), description="text", kind="quick_apply")
    assert jobs.all() == []


def _walk(jobs: JobsList, job: int, *states: str) -> None:
    for state in states:
        jobs.move(job, state, "why" if state in ("paused", "failed") else None)


def test_a_job_moves_through_the_day_and_its_dates_follow(jobs: JobsList, clock: MovableClock) -> None:
    job = jobs.add(_found("https://www.linkedin.com/jobs/view/1"))
    jobs.score(job, 8, "Strong match for the test framework work.")
    clock.advance(hours=1)
    _walk(jobs, job, "picked", "resume_ready", "applying")
    jobs.move(job, "paused", "The site asks for a CAPTCHA.")
    assert (jobs.get(job).state, jobs.get(job).state_reason) == ("paused", "The site asks for a CAPTCHA.")
    _walk(jobs, job, "applying", "applied")
    saved = jobs.get(job)
    assert (saved.state, saved.state_reason, saved.decision) == ("applied", None, "picked")
    assert (saved.score, saved.score_reason) == (8, "Strong match for the test framework work.")
    assert saved.updated_at == clock.now.isoformat() != saved.found_at


def test_skipping_is_the_users_decision_and_can_be_changed(jobs: JobsList) -> None:
    job = jobs.add(_found("https://www.linkedin.com/jobs/view/1"))
    jobs.score(job, 3, "Needs Java.")
    jobs.move(job, "skipped")
    assert jobs.get(job).decision == "skipped"
    jobs.move(job, "picked")
    assert jobs.get(job).decision == "picked"


def test_every_move_not_listed_is_refused(jobs: JobsList) -> None:
    allowed = {(start, end) for start, ends in MOVES.items() for end in ends}
    assert set(MOVES) | {end for ends in MOVES.values() for end in ends} == set(STATES)
    for start in STATES:
        for end in STATES:
            job = jobs.add(_found(f"https://www.linkedin.com/jobs/view/{len(jobs.all()) + 1}"))
            path = _path_to(start)
            _walk(jobs, job, *path)
            if (start, end) in allowed:
                jobs.move(job, end, "why" if end in ("paused", "failed") else None)
                assert jobs.get(job).state == end
            else:
                with pytest.raises(MoveError, match=f"{start} to {end}"):
                    jobs.move(job, end, "why")
                assert jobs.get(job).state == start


def _path_to(state: str) -> list[str]:
    paths = {
        "new": [],
        "scored": ["scored"],
        "picked": ["scored", "picked"],
        "skipped": ["scored", "skipped"],
        "resume_ready": ["scored", "picked", "resume_ready"],
        "applying": ["scored", "picked", "resume_ready", "applying"],
        "paused": ["scored", "picked", "resume_ready", "applying", "paused"],
        "applied": ["scored", "picked", "resume_ready", "applying", "applied"],
        "failed": ["scored", "picked", "failed"],
    }
    return paths[state]


def test_a_pause_or_failure_needs_its_reason_and_other_moves_take_none(jobs: JobsList) -> None:
    job = jobs.add(_found("https://www.linkedin.com/jobs/view/1"))
    _walk(jobs, job, "scored", "picked")
    with pytest.raises(MoveError, match="reason"):
        jobs.move(job, "failed")
    with pytest.raises(MoveError, match="reason"):
        jobs.move(job, "resume_ready", "because")


def test_a_score_is_from_1_to_10_and_only_for_a_new_job(jobs: JobsList) -> None:
    job = jobs.add(_found("https://www.linkedin.com/jobs/view/1"))
    with pytest.raises(ValueError, match="1 to 10"):
        jobs.score(job, 11, "Too good.")
    jobs.score(job, 7, "Good.")
    with pytest.raises(MoveError, match="scored to scored"):
        jobs.score(job, 6, "Again.")


def test_the_last_finished_run_is_kept(jobs: JobsList, clock: MovableClock) -> None:
    assert jobs.last_run() is None
    jobs.finished_run(clock.now)
    later = clock.advance(days=1)
    jobs.finished_run(later)
    assert jobs.last_run() == later


def test_the_file_is_the_users_alone_and_survives_reopening(tmp_path: Path, clock: MovableClock) -> None:
    path = tmp_path / "jobs.db"
    job = JobsList(path, clock=clock).add(_found("https://www.linkedin.com/jobs/view/1"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert JobsList(path, clock=clock).get(job).title == "Senior QA Automation Engineer"
    # The link is unique in the file itself, not only in the code that writes it.
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(path) as raw:
            raw.execute("INSERT INTO jobs (link, title, company, board, state, found_at, updated_at)"
                        " VALUES ('https://www.linkedin.com/jobs/view/1', 't', 'c', 'linkedin', 'new', 'x', 'x')")
