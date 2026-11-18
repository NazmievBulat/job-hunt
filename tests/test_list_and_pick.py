"""`hunt list`, `hunt pick` and `hunt skip`: the numbered list of scored jobs and the user's decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.jobs import FoundJob, JobsList, PickError
from movable_clock import MovableClock


def _found(number: int, **change: object) -> FoundJob:
    job = dict(link=f"https://www.linkedin.com/jobs/view/{number}", title=f"QA Engineer {number}",
               company="Example Labs", place="Durham, NC", remote="on_site", board="linkedin", email_id="1",
               email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")
    return FoundJob(**{**job, **change})


@pytest.fixture
def jobs(tmp_path: Path) -> JobsList:
    return JobsList(tmp_path / "jobs.db", clock=MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC)))


def _scored(jobs: JobsList, number: int, score: int, **change: object) -> int:
    job = jobs.add(_found(number, **change), kind="easy_apply")
    jobs.score(job, score, f"Reason {number}.")
    return job


def test_the_list_is_the_scored_jobs_not_yet_decided_best_first(jobs: JobsList) -> None:
    low = _scored(jobs, 1, 4)
    best = _scored(jobs, 2, 9)
    tie_first = _scored(jobs, 3, 7)
    tie_second = _scored(jobs, 4, 7)
    jobs.add(_found(5))  # new, not scored yet
    skipped = _scored(jobs, 6, 10)
    jobs.move(skipped, "skipped")
    # Equal scores keep the order they were found in.
    assert [job.id for job in jobs.show()] == [best, tie_first, tie_second, low]


def test_a_number_names_the_job_of_the_last_list_shown_even_after_more_jobs_are_scored(jobs: JobsList) -> None:
    first = _scored(jobs, 1, 6)
    second = _scored(jobs, 2, 5)
    assert [job.id for job in jobs.show()] == [first, second]
    # A better job scored after the list was shown does not move the numbers the user read.
    better = _scored(jobs, 3, 9)
    assert [job.id for _, job in jobs.decide([2], "picked")] == [second]
    assert jobs.get(second).state == "picked"
    # The next list numbers what is left, and the numbers follow it.
    assert [job.id for job in jobs.show()] == [better, first]
    assert [job.id for _, job in jobs.decide([2], "skipped")] == [first]
    assert (jobs.get(first).state, jobs.get(better).state) == ("skipped", "scored")


def test_the_numbers_are_kept_with_the_list_file(tmp_path: Path) -> None:
    jobs = JobsList(tmp_path / "jobs.db")
    first, second = _scored(jobs, 1, 9), _scored(jobs, 2, 3)
    jobs.show()
    again = JobsList(tmp_path / "jobs.db")
    assert [job.id for _, job in again.decide([2, 1], "picked")] == [second, first]


def test_pick_and_skip_by_number(jobs: JobsList) -> None:
    one, two, three, four = (_scored(jobs, n, 10 - n) for n in range(1, 5))
    jobs.show()
    assert [job.id for _, job in jobs.decide([1, 3, 3, 4], "picked")] == [one, three, four]
    assert [job.id for _, job in jobs.decide([2], "skipped")] == [two]
    assert [jobs.get(job).decision for job in (one, two, three, four)] == ["picked", "skipped", "picked", "picked"]


def test_a_skipped_job_can_still_be_picked_from_the_same_list(jobs: JobsList) -> None:
    job = _scored(jobs, 1, 5)
    jobs.show()
    jobs.decide([1], "skipped")
    jobs.decide([1], "picked")
    assert jobs.get(job).state == "picked"


def test_pick_above_takes_the_jobs_of_the_last_list_scored_more_than_the_number(jobs: JobsList) -> None:
    nine, seven, eight, five = _scored(jobs, 1, 9), _scored(jobs, 2, 7), _scored(jobs, 3, 8), _scored(jobs, 4, 5)
    jobs.show()
    jobs.decide([2], "skipped")  # number 2 is the 8
    unseen = _scored(jobs, 5, 10)
    # Not the 7 itself, not the 8 already skipped, not the 10 the user has not seen.
    assert jobs.numbers_above(7) == [1]
    assert [job.id for _, job in jobs.decide(jobs.numbers_above(4), "picked")] == [nine, seven, five]
    assert jobs.get(eight).state == "skipped" and jobs.get(unseen).state == "scored"


def test_a_number_not_on_the_list_is_refused_and_nothing_is_decided(jobs: JobsList) -> None:
    one, two = _scored(jobs, 1, 9), _scored(jobs, 2, 8)
    jobs.show()
    with pytest.raises(PickError, match="^there is no 5 on the last list; it has 1 to 2$"):
        jobs.decide([1, 5], "picked")
    with pytest.raises(PickError, match="^there is no 0 on the last list; it has 1 to 2$"):
        jobs.decide([0], "skipped")
    assert [jobs.get(job).state for job in (one, two)] == ["scored", "scored"]


def test_before_any_list_is_shown_a_pick_says_to_show_it(jobs: JobsList) -> None:
    _scored(jobs, 1, 9)
    with pytest.raises(PickError, match="^no list has been shown yet: run hunt list$"):
        jobs.decide([1], "picked")


def test_an_empty_list_is_shown_and_a_pick_then_is_refused(jobs: JobsList) -> None:
    _scored(jobs, 1, 9)
    jobs.show()
    jobs.decide([1], "picked")
    assert jobs.show() == []
    with pytest.raises(PickError, match="^the last list was empty$"):
        jobs.decide([1], "picked")


def test_a_job_already_decided_the_other_way_is_named_and_nothing_is_decided(jobs: JobsList) -> None:
    one, two = _scored(jobs, 1, 9), _scored(jobs, 2, 8)
    jobs.show()
    jobs.decide([1], "picked")
    with pytest.raises(PickError, match=r"^1 \(QA Engineer 1 at Example Labs\) is already picked$"):
        jobs.decide([2, 1], "skipped")
    with pytest.raises(PickError, match=r"^1 \(QA Engineer 1 at Example Labs\) is already picked$"):
        jobs.decide([1], "picked")
    assert (jobs.get(one).state, jobs.get(two).state) == ("picked", "scored")
    # A pick that has gone further than picked is still a pick.
    jobs.move(one, "resume_ready")
    with pytest.raises(PickError, match="is already picked"):
        jobs.decide([1], "skipped")


@pytest.fixture
def hunt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: JobsList) -> JobsList:
    monkeypatch.setattr(cli, "DATA", tmp_path)
    return jobs


def test_hunt_list_prints_number_score_title_company_place_way_to_apply_and_reason(
    hunt: JobsList, capsys: pytest.CaptureFixture[str]
) -> None:
    _scored(hunt, 1, 9, title="Senior SDET", place="Durham, NC", remote="hybrid")
    _scored(hunt, 2, 10, title="QA Lead", company="Globex Example", place="United States", remote="remote")
    hunt.add(_found(3, title="Test Engineer", place=""), kind="company_site")
    hunt.score(3, 6, "Close enough.")
    hunt.add(_found(4, title="Automation Engineer"))
    hunt.score(4, 5, "Some of it.")
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == (
        " 1. 10  QA Lead · Globex Example · remote · Easy Apply\n"
        "        Reason 2.\n"
        " 2.  9  Senior SDET · Example Labs · Durham, NC, hybrid · Easy Apply\n"
        "        Reason 1.\n"
        " 3.  6  Test Engineer · Example Labs · place not given · company site\n"
        "        Close enough.\n"
        " 4.  5  Automation Engineer · Example Labs · Durham, NC · way to apply not known\n"
        "        Some of it.\n"
    )


def test_hunt_list_with_nothing_waiting_says_so(hunt: JobsList, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "No scored jobs wait for your pick.\n"


def test_hunt_pick_and_skip_print_what_they_recorded(hunt: JobsList, capsys: pytest.CaptureFixture[str]) -> None:
    for number, score in ((1, 9), (2, 8), (3, 4)):
        _scored(hunt, number, score)
    assert cli.main(["list"]) == 0
    capsys.readouterr()
    assert cli.main(["pick", "1", "2"]) == 0
    assert cli.main(["skip", "3"]) == 0
    assert capsys.readouterr().out == (
        "picked 1: QA Engineer 1 at Example Labs\n"
        "picked 2: QA Engineer 2 at Example Labs\n"
        "skipped 3: QA Engineer 3 at Example Labs\n"
    )


def test_hunt_pick_above(hunt: JobsList, capsys: pytest.CaptureFixture[str]) -> None:
    _scored(hunt, 1, 9)
    _scored(hunt, 2, 7)
    cli.main(["list"])
    capsys.readouterr()
    assert cli.main(["pick", "--above", "8"]) == 0
    assert cli.main(["pick", "--above", "8"]) == 0
    assert capsys.readouterr().out == (
        "picked 1: QA Engineer 1 at Example Labs\n"
        "no job of the last list waiting for a pick scores above 8\n"
    )


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["pick", "3"], "hunt pick: there is no 3 on the last list; it has 1 to 1\n"),
        (["pick"], "hunt pick: give the numbers to pick, or --above and a score\n"),
        (["pick", "1", "--above", "7"], "hunt pick: give the numbers to pick, or --above and a score\n"),
        (["pick", "--above", "11"], "hunt pick: --above takes a score from 0 to 9\n"),
        (["pick", "--above", "-1"], "hunt pick: --above takes a score from 0 to 9\n"),
        (["skip"], "hunt skip: give the numbers to skip\n"),
    ],
)
def test_hunt_pick_refuses_with_one_clear_line(
    hunt: JobsList, capsys: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    _scored(hunt, 1, 9)
    cli.main(["list"])
    capsys.readouterr()
    assert cli.main(argv) == 1
    assert capsys.readouterr() == ("", error)
    assert hunt.get(1).state == "scored"


def test_hunt_pick_above_0_picks_every_job_waiting_on_the_last_list(
    hunt: JobsList, capsys: pytest.CaptureFixture[str]
) -> None:
    for number, score in ((1, 9), (2, 1), (3, 5)):
        _scored(hunt, number, score)
    cli.main(["list"])
    assert cli.main(["pick", "--above", "0"]) == 0
    assert [job.state for job in hunt.all()] == ["picked", "picked", "picked"]


def test_skipping_a_job_skipped_already_is_refused_with_one_line(
    hunt: JobsList, capsys: pytest.CaptureFixture[str]
) -> None:
    _scored(hunt, 1, 4)
    cli.main(["list"])
    assert cli.main(["skip", "1"]) == 0
    capsys.readouterr()
    assert cli.main(["skip", "1"]) == 1
    assert capsys.readouterr() == ("", "hunt skip: 1 (QA Engineer 1 at Example Labs) is already skipped\n")
