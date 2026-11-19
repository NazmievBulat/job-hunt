"""`hunt questions`: what waits for the person, in the order the chat asks it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt.available import Verdict
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.morning import log_failure
from job_hunt.questions import anything_waiting, questions
from movable_clock import MovableClock


def _found(number: int, title: str = "QA Automation Engineer") -> FoundJob:
    return FoundJob(link=f"https://www.linkedin.com/jobs/view/{number}", title=title, company="Example Labs",
                    place="Durham, NC", remote="hybrid", board="linkedin", email_id="1",
                    email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


@pytest.fixture
def jobs(tmp_path: Path, clock: MovableClock) -> JobsList:
    return JobsList(tmp_path / "data" / "jobs.db", clock=clock)


SIGN_IN = 'a sign-in or a new account, in the Chrome tab "Apply · Example Labs"'
CAPTCHA = 'a CAPTCHA solved, in the Chrome tab "Apply · Example Labs"'


def _paused(jobs: JobsList, number: int, title: str, reason: str = SIGN_IN) -> int:
    job = jobs.add(_found(number, title), kind="company_site")
    assert job is not None
    jobs.score(job, 6, "Close enough.")
    jobs.move(job, "picked")
    jobs.move(job, "resume_ready")
    jobs.move(job, "applying")
    jobs.move(job, "paused", reason)
    return job


def test_nothing_waiting_says_so(jobs: JobsList, tmp_path: Path) -> None:
    assert not anything_waiting(jobs, tmp_path / "data")
    assert questions(jobs, tmp_path / "data") == "Nothing waits for you.\n"


def test_new_jobs_come_before_paused_applications(jobs: JobsList, tmp_path: Path) -> None:
    paused = _paused(jobs, 1, "SDET")
    new = jobs.add(_found(2, "QA Lead"), kind="easy_apply")
    assert new is not None
    jobs.score(new, 8, "Selenium and Python, as in the last two jobs.")

    text = questions(jobs, tmp_path / "data")

    assert text.index("New jobs") < text.index("Paused applications")
    assert " 1.  8  QA Lead · Example Labs · Durham, NC, hybrid · Easy Apply" in text
    assert "Selenium and Python, as in the last two jobs." in text
    assert f"{paused}. SDET at Example Labs: needs a sign-in or a new account" in text
    assert f"(then: hunt resume-job {paused})" in text
    assert anything_waiting(jobs, tmp_path / "data")


def test_the_new_jobs_are_numbered_for_hunt_pick(jobs: JobsList, tmp_path: Path) -> None:
    for number, score in ((1, 5), (2, 9)):
        job = jobs.add(_found(number, f"Role {number}"))
        assert job is not None
        jobs.score(job, score, "A reason.")

    questions(jobs, tmp_path / "data")

    # The list was shown, so "pick 1" names the best job, as after hunt list.
    assert [job.title for _, job in jobs.decide([1], "picked")] == ["Role 2"]


def test_a_morning_failure_is_told_first_and_once(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    data = tmp_path / "data"
    log_failure(data, "hunt find", "the read-job-alerts run failed (1): Gmail is not connected", clock)
    job = jobs.add(_found(1))
    assert job is not None
    jobs.score(job, 7, "A reason.")

    assert anything_waiting(jobs, data)
    first = questions(jobs, data)
    second = questions(jobs, data)

    assert first.index("The morning run had a problem") < first.index("New jobs")
    assert "hunt find: the read-job-alerts run failed (1): Gmail is not connected" in first
    assert "morning run" not in second


def test_a_failure_alone_waits_for_the_person(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    data = tmp_path / "data"
    log_failure(data, "hunt score", "another hunt score is already running", clock)

    assert anything_waiting(jobs, data)
    questions(jobs, data)
    assert not anything_waiting(jobs, data)


def test_a_failure_logged_after_the_questions_is_told_next_time(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    data = tmp_path / "data"
    log_failure(data, "hunt find", "first", clock)
    questions(jobs, data)
    clock.advance(days=1)
    log_failure(data, "hunt find", "second", clock)

    text = questions(jobs, data)

    assert "second" in text and "first" not in text


def test_a_log_cut_by_hand_in_a_letter_still_reads(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    data = tmp_path / "data"
    log_failure(data, "hunt find", "the page of Société Example could not be read", clock)
    # Told up to the middle of the two bytes of "é".
    (data / "morning.told").write_text(f"{(data / 'morning.log').read_bytes().index('é'.encode()) + 1}\n")

    assert "could not be read" in questions(jobs, data)


def test_asking_again_keeps_the_numbers_of_the_jobs_still_waiting(jobs: JobsList, tmp_path: Path) -> None:
    for number, score in ((1, 9), (2, 8), (3, 7)):
        job = jobs.add(_found(number, f"Role {number}"))
        assert job is not None
        jobs.score(job, score, "A reason.")
    questions(jobs, tmp_path / "data")
    jobs.decide([1], "picked")

    again = questions(jobs, tmp_path / "data")

    # "2" still names Role 2, as when the list was shown.
    assert " 2.  8  Role 2" in again and " 3.  7  Role 3" in again and "Role 1 ·" not in again
    assert [job.title for _, job in jobs.decide([2], "picked")] == ["Role 2"]


def test_a_new_job_renumbers_the_whole_list(jobs: JobsList, tmp_path: Path) -> None:
    for number, score in ((1, 9), (2, 8)):
        job = jobs.add(_found(number, f"Role {number}"))
        assert job is not None
        jobs.score(job, score, "A reason.")
    questions(jobs, tmp_path / "data")
    jobs.decide([1], "picked")
    later = jobs.add(_found(3, "Role 3"))
    assert later is not None
    jobs.score(later, 9, "A reason.")

    again = questions(jobs, tmp_path / "data")

    assert "New jobs (2), numbered anew:" in again
    assert " 1.  9  Role 3" in again and " 2.  8  Role 2" in again


@pytest.mark.parametrize("state", ["picked", "applying"])
def test_an_approved_application_left_unfinished_waits(jobs: JobsList, tmp_path: Path, state: str) -> None:
    job = jobs.add(_found(1), kind="company_site")
    assert job is not None
    jobs.score(job, 7, "A reason.")
    jobs.move(job, "picked")
    if state == "applying":
        jobs.move(job, "resume_ready")
        jobs.move(job, "applying")

    assert anything_waiting(jobs, tmp_path / "data")
    assert "Unfinished applications (1): run hunt apply" in questions(jobs, tmp_path / "data")


@pytest.mark.parametrize("kind", ["company_site", "easy_apply"])
def test_an_approved_application_with_its_resume_ready_waits(jobs: JobsList, tmp_path: Path, kind: str) -> None:
    # hunt apply applies to both kinds, the Easy Apply ones through the LinkedIn worker.
    job = jobs.add(_found(1), kind=kind)
    assert job is not None
    jobs.score(job, 7, "A reason.")
    jobs.move(job, "picked")
    jobs.move(job, "resume_ready")

    assert anything_waiting(jobs, tmp_path / "data")
    assert "Unfinished applications (1): run hunt apply" in questions(jobs, tmp_path / "data")


def test_a_pause_that_needs_chrome_carries_the_remote_desktop_link_when_it_is_set_up(
        jobs: JobsList, tmp_path: Path) -> None:
    _paused(jobs, 1, "SDET")

    text = questions(jobs, tmp_path / "data", remote_desktop=True)

    assert "From your phone: open https://remotedesktop.google.com/access and do it in this computer's Chrome" in text


def test_without_remote_desktop_a_pause_that_needs_chrome_waits_for_the_computer(
        jobs: JobsList, tmp_path: Path) -> None:
    _paused(jobs, 1, "SDET")

    text = questions(jobs, tmp_path / "data")

    assert "remotedesktop.google.com" not in text
    assert "This waits until you are at the computer; the other jobs go on." in text


def test_a_pause_for_an_answer_alone_needs_no_computer(jobs: JobsList, tmp_path: Path) -> None:
    job = _paused(jobs, 1, "SDET")
    jobs.move(job, "applying")
    jobs.move(job, "paused", 'an answer to "Notice period?", in the Chrome tab "Apply · Example Labs"')

    for ready in (True, False):
        text = questions(jobs, tmp_path / "data", remote_desktop=ready)
        assert "remotedesktop" not in text and "at the computer" not in text


def _free(reason: str = "you are at the computer", from_afar: bool = False) -> Verdict:
    return Verdict(True, reason, from_afar)


def _away() -> Verdict:
    return Verdict(False, "you are away from the computer")


def test_captchas_wait_in_one_question_when_the_person_is_free(jobs: JobsList, tmp_path: Path) -> None:
    first = _paused(jobs, 1, "SDET", CAPTCHA)
    second = _paused(jobs, 2, "QA Lead", 'a CAPTCHA solved, in the Chrome tab "Careers"')
    signed_out = _paused(jobs, 3, "QA Analyst")

    text = questions(jobs, tmp_path / "data", available=_free)

    assert ("CAPTCHAs (2): 2 applications wait for a CAPTCHA in Chrome: Example Labs (\"Apply · Example Labs\" tab), "
            'Example Labs ("Careers" tab). Reply done when solved.') in text
    assert f"(then: hunt resume-job {first} {second})" in text
    # The sign-in is a question of its own, as before.
    assert f"{signed_out}. QA Analyst at Example Labs: needs a sign-in" in text
    assert f"{first}. SDET at Example Labs: needs" not in text
    assert [jobs.get(job).asks for job in (first, second, signed_out)] == [1, 1, 0]


def test_captchas_are_held_while_the_person_is_not_free(jobs: JobsList, tmp_path: Path) -> None:
    job = _paused(jobs, 1, "SDET", CAPTCHA)

    for available in (_away, None):
        assert questions(jobs, tmp_path / "data", available=available) == "Nothing waits for you.\n"
    assert jobs.get(job).asks == 0
    # Nor does it start the morning's session: the watch tells the chat once the person is free.
    assert not anything_waiting(jobs, tmp_path / "data")


def test_a_question_asked_again_before_a_reply_counts_once(jobs: JobsList, tmp_path: Path) -> None:
    job = _paused(jobs, 1, "SDET", CAPTCHA)

    questions(jobs, tmp_path / "data", available=_free)
    questions(jobs, tmp_path / "data", available=_free)

    assert jobs.get(job).asks == 1


def test_asked_from_the_chat_a_captcha_carries_the_remote_desktop_link(jobs: JobsList, tmp_path: Path) -> None:
    _paused(jobs, 1, "SDET", CAPTCHA)
    chatting = lambda: _free("you replied in the chat just now", from_afar=True)

    assert "remotedesktop.google.com/access" in questions(jobs, tmp_path / "data", available=chatting,
                                                          remote_desktop=True)
    assert "They are in this computer's Chrome." in questions(jobs, tmp_path / "data", available=chatting)
    assert "remotedesktop" not in questions(jobs, tmp_path / "data", available=_free, remote_desktop=True)


def test_a_try_again_not_due_yet_is_no_unfinished_application(
        jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    job = _paused(jobs, 1, "SDET", CAPTCHA)
    jobs.move(job, "applying")
    jobs.move(job, "resume_ready")
    jobs.later(job, clock.now + timedelta(hours=3))

    assert questions(jobs, tmp_path / "data") == "Nothing waits for you.\n"
    assert not anything_waiting(jobs, tmp_path / "data")
    clock.advance(hours=3)
    assert "Unfinished applications (1): run hunt apply" in questions(jobs, tmp_path / "data")
