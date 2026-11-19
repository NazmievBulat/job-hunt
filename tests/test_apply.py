"""`hunt apply`, `hunt answer`, `hunt resume-job` and `hunt status`: resumes for the picked jobs,
then applying on company sites, up to three at once.

No model and no browser: the workers are stand-ins that write the files a worker would, the
resume PDF or the report.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.apply import (
    MODEL, RESUME_SKILL, SITE_SKILL, SITE_WORKERS, ApplyError, AnswerError, answer, apply, read_report,
    resume_command, resume_job, site_command,
)
from job_hunt.career import Career, read_career, write_career
from job_hunt.jobs import FoundJob, JobsList
from movable_clock import MovableClock


def _found(number: int, **change: object) -> FoundJob:
    job = dict(link=f"https://jobs.example.com/{number}", title=f"QA Engineer {number}",
               company=f"Example Co {number}", place="Durham, NC", remote="on_site", board="indeed",
               email_id="1", email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")
    return FoundJob(**{**job, **change})


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


def _picked(jobs: JobsList, number: int, kind: str = "company_site") -> int:
    job = jobs.add(_found(number), kind=kind)
    jobs.score(job, 8, "Good match.")
    jobs.move(job, "picked")
    return job


def _job_of(command: list[str], skill: str) -> int:
    prompt = command[command.index("-p") + 1]
    assert prompt.startswith(f"/{skill} ")
    return json.loads(prompt.removeprefix(f"/{skill} "))["job"]


class Resumes:
    """Stands in for the resume workers: builds `resume.pdf`, except for the jobs told to fail."""

    def __init__(self, project: Path, failing: dict[int, int] | None = None) -> None:
        self.project, self.failing, self.ran = project, dict(failing or {}), []
        self._lock = threading.Lock()

    def __call__(self, command: list[str]) -> None:
        job = _job_of(command, RESUME_SKILL)
        with self._lock:
            self.ran.append(job)
            if self.failing.get(job, 0) > 0:
                self.failing[job] -= 1
                raise ApplyError("the write-resume run failed (1): the contract refused the skills")
        (self.project / "jobs" / str(job) / "resume.pdf").write_bytes(b"%PDF-1.4 stand-in")


class Sites:
    """Stands in for the company-site workers: writes each job's report, one after another
    from `reports` (the last one repeats), and counts how many run at once."""

    def __init__(self, project: Path, reports: dict[int, list[dict | str | bytes | None]] | None = None,
                 seconds: float = 0.0) -> None:
        self.project, self.reports, self.seconds = project, reports or {}, seconds
        self.ran: list[int] = []
        self.running = self.most = 0
        self._lock = threading.Lock()

    def __call__(self, command: list[str]) -> None:
        job = _job_of(command, SITE_SKILL)
        with self._lock:
            self.ran.append(job)
            self.running += 1
            self.most = max(self.most, self.running)
            queue = self.reports.get(job, [{"result": "applied"}])
            report = queue.pop(0) if len(queue) > 1 else queue[0]
        try:
            time.sleep(self.seconds)
            out = self.project / "jobs" / str(job) / "report.json"
            if isinstance(report, str):
                out.write_text(report)
            elif isinstance(report, bytes):
                out.write_bytes(report)
            elif report is not None:
                out.write_text(json.dumps(report))
        finally:
            with self._lock:
                self.running -= 1


def test_a_resume_worker_reads_only_its_folder_and_the_career_and_runs_only_the_builder(project: Path) -> None:
    command = resume_command(12, project)
    assert command[:2] == ["claude", "-p"]
    assert json.loads(command[2].removeprefix(f"/{RESUME_SKILL} ")) == {"job": 12, "folder": "jobs/12"}
    assert command[command.index("--model") + 1] == MODEL
    folder, career = project / "jobs" / "12", project / "data" / "career.json"
    assert command[command.index("--allowedTools") + 1].split(",") == [
        f"Read(/{folder}/**)", f"Read(/{career})",
        f"Edit(/{folder}/resume-content.json)", "Bash(uv run hunt resume jobs/12)",
    ]
    assert "--chrome" not in command


def test_a_site_worker_drives_chrome_reads_its_folder_and_the_career_and_writes_only_its_report(
    project: Path, jobs: JobsList
) -> None:
    job = jobs.get(_picked(jobs, 1))
    command = site_command(job, project)
    assert command[:3] == ["claude", "--chrome", "-p"]
    folder = project / "jobs" / str(job.id)
    assert json.loads(command[3].removeprefix(f"/{SITE_SKILL} ")) == {
        "job": job.id, "link": "https://jobs.example.com/1", "title": "QA Engineer 1", "company": "Example Co 1",
        "resume": str(folder / "resume.pdf"), "career": str(project / "data" / "career.json"),
        "report": str(folder / "report.json"),
    }
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert tools[:3] == [f"Read(/{folder}/**)", f"Read(/{project}/data/career.json)", f"Edit(/{folder}/report.json)"]
    # The rest drive the browser; nothing runs a command or reads another folder.
    assert all(tool.startswith("mcp__claude-in-chrome__") for tool in tools[3:])
    assert "mcp__claude-in-chrome__file_upload" in tools
    assert "mcp__claude-in-chrome__javascript_tool" not in tools


def test_apply_writes_every_resume_at_once_then_applies_on_company_sites(project: Path, jobs: JobsList) -> None:
    site, easy = _picked(jobs, 1), _picked(jobs, 2, kind="easy_apply")
    resumes, sites = Resumes(project), Sites(project)
    summary = apply(jobs, project, run_resume=resumes, run_site=sites)
    assert sorted(resumes.ran) == [site, easy]
    # Easy Apply is the LinkedIn worker's (the next ticket): its resume waits for it.
    assert sites.ran == [site]
    assert (jobs.get(site).state, jobs.get(easy).state) == ("applied", "resume_ready")
    assert (summary.resumes, summary.applied, summary.paused, summary.failed) == (2, 1, 0, 0)


def test_all_the_resume_workers_run_at_the_same_time(project: Path, jobs: JobsList) -> None:
    picked = [_picked(jobs, n, kind="easy_apply") for n in range(1, 6)]
    together = threading.Barrier(len(picked), timeout=5)

    def run(command: list[str]) -> None:
        # Each waits for all the others: this passes only when they all run at once.
        together.wait()
        (project / "jobs" / str(_job_of(command, RESUME_SKILL)) / "resume.pdf").write_bytes(b"%PDF")

    apply(jobs, project, run_resume=run, run_site=Sites(project))
    assert {jobs.get(job).state for job in picked} == {"resume_ready"}


def test_at_most_three_company_site_workers_run_at_once(project: Path, jobs: JobsList) -> None:
    picked = [_picked(jobs, n) for n in range(1, 8)]
    sites = Sites(project, seconds=0.05)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert SITE_WORKERS == 3
    assert sites.most == 3
    assert sorted(sites.ran) == picked
    assert {jobs.get(job).state for job in picked} == {"applied"}


def test_a_job_is_applied_to_once_only_across_two_runs(project: Path, jobs: JobsList) -> None:
    first = _picked(jobs, 1)
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    second = _picked(jobs, 2)
    resumes = Resumes(project)
    summary = apply(jobs, project, run_resume=resumes, run_site=sites)
    assert sites.ran == [first, second]
    assert resumes.ran == [second]
    assert (jobs.get(first).state, jobs.get(second).state) == ("applied", "applied")
    assert summary.applied == 1


def test_a_resume_already_built_is_not_written_again(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1, kind="easy_apply")
    (project / "jobs" / str(job)).mkdir()
    (project / "jobs" / str(job) / "resume.pdf").write_bytes(b"%PDF from a run that stopped")
    resumes = Resumes(project)
    apply(jobs, project, run_resume=resumes, run_site=Sites(project))
    assert resumes.ran == []
    assert jobs.get(job).state == "resume_ready"


def test_a_site_failure_is_tried_once_more_and_a_second_failure_ends_as_failed(project: Path, jobs: JobsList) -> None:
    once, twice = _picked(jobs, 1), _picked(jobs, 2)
    sites = Sites(project, {
        once: [{"result": "failed", "reason": "The site showed an error page."}, {"result": "applied"}],
        twice: [{"result": "failed", "reason": "The form would not submit."}],
    })
    summary = apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sorted(sites.ran) == [once, once, twice, twice]
    assert jobs.get(once).state == "applied"
    failed = jobs.get(twice)
    assert (failed.state, failed.state_reason) == ("failed", "The form would not submit.")
    assert (summary.applied, summary.failed) == (1, 1)
    # Failed stays failed: the next run does not try it again.
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sorted(sites.ran) == [once, once, twice, twice]


def test_a_failure_counts_across_runs(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    # An earlier run failed it once and then stopped (the computer went to sleep).
    assert jobs.fail(job, "The site timed out.") == "picked"
    assert jobs.get(job).failures == 1
    sites = Sites(project, {job: [{"result": "failed", "reason": "The form would not submit."}]})
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    # This run gives it the one try it has left.
    assert sites.ran == [job]
    assert (jobs.get(job).state, jobs.get(job).state_reason) == ("failed", "The form would not submit.")


def test_a_resume_that_fails_twice_ends_as_failed_with_the_builders_words(project: Path, jobs: JobsList) -> None:
    once, twice = _picked(jobs, 1), _picked(jobs, 2)
    resumes = Resumes(project, failing={once: 1, twice: 2})
    sites = Sites(project)
    apply(jobs, project, run_resume=resumes, run_site=sites)
    assert sorted(resumes.ran) == [once, once, twice, twice]
    assert jobs.get(once).state == "applied"
    failed = jobs.get(twice)
    assert failed.state == "failed"
    assert failed.state_reason == "the write-resume run failed (1): the contract refused the skills"
    assert sites.ran == [once]


def test_a_resume_worker_that_ends_without_a_pdf_is_a_failure(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    apply(jobs, project, run_resume=lambda command: None, run_site=Sites(project))
    assert (jobs.get(job).state, jobs.get(job).state_reason) == (
        "failed", "the write-resume run built no resume.pdf")


UNSURE = "a look in Chrome whether it was sent: "


def _unsure_after_two_runs(project: Path, jobs: JobsList, job: int, run_site) -> None:
    """The worker left no report it can be read by: the job waits for the user, in this run and
    the next, and nothing counts against it."""
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=run_site)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sites.ran == []
    assert (jobs.get(job).state, jobs.get(job).failures) == ("paused", 0)


@pytest.mark.parametrize(("written", "words"), [
    (None, "wrote no report"),
    ("{not json", "not JSON"),
    # Cut off inside a character by a worker that stopped while writing.
    (b'{"result": "paused", "tab": "\xc3', "not JSON"),
    ('{"result": "sent"}', "result"),
    ('{"result": "failed"}', "reason"),
    ('{"result": "paused", "needs": "captcha"}', "tab"),
    ('{"result": "paused", "needs": "question", "tab": "Apply - Example Co"}', "question"),
    ('{"result": "paused", "needs": "a phone call", "tab": "Apply - Example Co"}', "needs"),
    ('{"result": "paused", "needs": "captcha", "tab": "Apply", "tab_id": "41"}', "tab_id"),
])
def test_a_report_that_cannot_be_read_pauses_the_job_saying_why(
    project: Path, jobs: JobsList, written: str | bytes | None, words: str
) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: [written]})
    # Without a report it can read, nothing says the form was not sent: no second worker.
    _unsure_after_two_runs(project, jobs, job, sites)
    assert sites.ran == [job]
    assert jobs.get(job).state_reason.startswith(UNSURE)
    assert words in jobs.get(job).state_reason


def test_a_report_left_by_an_earlier_try_does_not_pass_for_this_one(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: [{"result": "failed", "reason": "The site showed an error page."}, None]})
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sites.ran == [job, job]
    assert (jobs.get(job).state, jobs.get(job).state_reason) == (
        "paused", UNSURE + "the apply-site run wrote no report")
    assert jobs.get(job).failures == 1


def test_a_worker_that_crashes_without_a_report_pauses_the_job_with_its_error(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    started: list[int] = []

    def submitted_then_timed_out(command: list[str]) -> None:
        started.append(_job_of(command, SITE_SKILL))
        raise ApplyError("the apply-site run did not finish in 30 minutes")

    _unsure_after_two_runs(project, jobs, job, submitted_then_timed_out)
    assert started == [job]
    assert jobs.get(job).state_reason == UNSURE + "the apply-site run did not finish in 30 minutes"


def test_a_worker_that_ends_cleanly_without_a_report_pauses_the_job(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    started: list[int] = []
    _unsure_after_two_runs(project, jobs, job, lambda command: started.append(_job_of(command, SITE_SKILL)))
    assert started == [job]
    assert jobs.get(job).state_reason == UNSURE + "the apply-site run wrote no report"


def test_a_run_stopped_before_the_retry_started_does_not_count_the_last_failure_twice(
    project: Path, jobs: JobsList, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: [{"result": "failed", "reason": "The site showed an error page."}]})
    move, tries = jobs.move, []

    class Stopped(BaseException):
        """The computer went to sleep."""

    def stop_at_the_second_try(number: int, state: str, reason: str | None = None) -> None:
        move(number, state, reason)
        if state == "applying":
            tries.append(number)
            if len(tries) == 2:
                raise Stopped

    monkeypatch.setattr(jobs, "move", stop_at_the_second_try)
    with pytest.raises(Stopped):
        apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    monkeypatch.undo()
    assert (jobs.get(job).state, jobs.get(job).failures) == ("applying", 1)
    # The first try's failure was already counted; its report is gone before the second try.
    assert not (project / "jobs" / str(job) / "report.json").exists()
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert (jobs.get(job).state, jobs.get(job).failures) == ("paused", 1)
    assert sites.ran == [job]


def _pause(project: Path, jobs: JobsList, report: dict) -> int:
    job = _picked(jobs, 1)
    apply(jobs, project, run_resume=Resumes(project), run_site=Sites(project, {job: [report]}))
    assert jobs.get(job).state == "paused"
    return job


QUESTION = {"result": "paused", "needs": "question", "question": "Do you hold a current ISTQB certificate?",
            "tab": "Apply - Example Co 1"}


def test_a_paused_job_says_what_it_needs_and_in_which_tab(project: Path, jobs: JobsList) -> None:
    captcha = _pause(project, jobs, {"result": "paused", "needs": "captcha", "tab": "Apply - Example Co 1"})
    assert jobs.get(captcha).state_reason == 'a CAPTCHA solved, in the Chrome tab "Apply - Example Co 1"'
    assert read_report(project / "jobs" / str(captcha) / "report.json").needs == "captcha"


def test_hunt_answer_saves_the_answer_in_the_career_and_puts_the_job_back(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, QUESTION)
    assert jobs.get(job).state_reason == (
        'an answer to "Do you hold a current ISTQB certificate?", in the Chrome tab "Apply - Example Co 1"')
    career_file = project / "data" / "career.json"
    answer(jobs, project, job, "  Yes, ISTQB Foundation since 2021. ")
    assert jobs.get(job).state == "resume_ready"
    assert read_career(career_file).form_answers == {"saved": [
        {"question": "Do you hold a current ISTQB certificate?", "answer": "Yes, ISTQB Foundation since 2021.",
         "company": "Example Co 1"},
    ]}
    assert career_file.stat().st_mode & 0o777 == 0o600
    # The next run applies with it, and applies once.
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sites.ran == [job]
    assert jobs.get(job).state == "applied"


def test_an_answer_to_the_same_question_replaces_the_saved_one(project: Path, jobs: JobsList) -> None:
    career_file = project / "data" / "career.json"
    career = read_career(career_file)
    career.form_answers = {"authorized": "yes", "saved": [
        {"question": "do you hold a current istqb certificate?", "answer": "No.", "company": "Another Co"},
        {"question": "Notice period?", "answer": "Two weeks.", "company": "Another Co"},
    ]}
    write_career(career, career_file)
    job = _pause(project, jobs, QUESTION)
    answer(jobs, project, job, "Yes.")
    assert read_career(career_file).form_answers == {"authorized": "yes", "saved": [
        {"question": "Notice period?", "answer": "Two weeks.", "company": "Another Co"},
        {"question": "Do you hold a current ISTQB certificate?", "answer": "Yes.", "company": "Example Co 1"},
    ]}


def test_hunt_answer_takes_only_a_job_paused_on_a_question(project: Path, jobs: JobsList) -> None:
    captcha = _pause(project, jobs, {"result": "paused", "needs": "captcha", "tab": "Apply"})
    with pytest.raises(AnswerError, match="hunt resume-job"):
        answer(jobs, project, captcha, "done")
    with pytest.raises(AnswerError, match="not paused"):
        answer(jobs, project, _picked(jobs, 2), "Yes.")
    with pytest.raises(AnswerError, match="no job 99"):
        answer(jobs, project, 99, "Yes.")
    assert jobs.get(captcha).state == "paused"
    assert read_career(project / "data" / "career.json").form_answers == {}


def test_hunt_answer_needs_some_words(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, QUESTION)
    with pytest.raises(AnswerError, match="empty"):
        answer(jobs, project, job, "   ")
    assert jobs.get(job).state == "paused"


def test_an_answer_about_gender_race_veterans_or_disability_is_never_kept(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, {**QUESTION, "question": "What is your gender?"})
    with pytest.raises(AnswerError, match="never kept"):
        answer(jobs, project, job, "Prefer not to say")
    assert read_career(project / "data" / "career.json").form_answers == {}


def test_hunt_resume_job_puts_a_paused_job_back_after_the_user_solved_it(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, {"result": "paused", "needs": "sign_in", "tab": "Sign in - Example Co 1"})
    resume_job(jobs, job)
    assert (jobs.get(job).state, jobs.get(job).state_reason) == ("resume_ready", None)
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert (sites.ran, jobs.get(job).state) == ([job], "applied")
    with pytest.raises(AnswerError, match="not paused"):
        resume_job(jobs, job)


def test_a_job_left_applying_by_a_run_that_stopped_is_not_applied_to_again(project: Path, jobs: JobsList) -> None:
    left, reported = _picked(jobs, 1), _picked(jobs, 2)
    for job in (left, reported):
        jobs.move(job, "resume_ready")
        jobs.move(job, "applying")
    # The run stopped after this one's worker had written its report.
    (project / "jobs" / str(reported)).mkdir()
    (project / "jobs" / str(reported) / "report.json").write_text('{"result": "applied"}')
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert sites.ran == []
    assert jobs.get(reported).state == "applied"
    stopped = jobs.get(left)
    assert stopped.state == "paused"
    assert "whether it was sent" in stopped.state_reason


def test_two_apply_runs_never_run_at_once(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    started, release = threading.Event(), threading.Event()

    def slow(command: list[str]) -> None:
        started.set()
        release.wait(5)
        (project / "jobs" / str(job) / "resume.pdf").write_bytes(b"%PDF")

    def other_run() -> None:
        # Another hunt apply, with its own connection to the jobs list.
        apply(JobsList(project / "data" / "jobs.db"), project, run_resume=slow, run_site=Sites(project))

    first = threading.Thread(target=other_run)
    first.start()
    assert started.wait(5)
    try:
        with pytest.raises(ApplyError, match="already running"):
            apply(jobs, project, run_resume=Resumes(project), run_site=Sites(project))
    finally:
        release.set()
        first.join(5)


@pytest.fixture
def hunt(project: Path, monkeypatch: pytest.MonkeyPatch, jobs: JobsList) -> JobsList:
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    monkeypatch.setattr(cli, "CAREER", project / "data" / "career.json")
    return jobs


def test_hunt_status_lines(hunt: JobsList, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    applied, paused, failed = _picked(hunt, 1), _picked(hunt, 2), _picked(hunt, 3)
    silent, easy = _picked(hunt, 4), _picked(hunt, 5, kind="easy_apply")
    sites = Sites(project, {
        paused: [{"result": "paused", "needs": "captcha", "tab": "Apply - Example Co 2"}],
        failed: [{"result": "failed", "reason": "The site showed an error page."}],
        silent: [None],
    })
    apply(hunt, project, run_resume=Resumes(project), run_site=sites)
    unwritten = _picked(hunt, 6)  # picked after the run: its resume is not written yet
    hunt.add(_found(7))  # new, not picked: not part of applying
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Applied (1):",
        f"  {applied}. QA Engineer 1 at Example Co 1",
        "Paused (2):",
        f'  {paused}. QA Engineer 2 at Example Co 2: needs a CAPTCHA solved, in the Chrome tab "Apply - Example Co 2"'
        f" (then: hunt resume-job {paused})",
        f"  {silent}. QA Engineer 4 at Example Co 4: needs a look in Chrome whether it was sent: the apply-site run"
        f" wrote no report (then: hunt applied {silent} if it was sent, hunt resume-job {silent} if not)",
        "Failed (1):",
        f"  {failed}. QA Engineer 3 at Example Co 3: The site showed an error page.",
        "Waiting (2):",
        f"  {easy}. QA Engineer 5 at Example Co 5: resume ready, for the LinkedIn worker",
        f"  {unwritten}. QA Engineer 6 at Example Co 6: resume not written yet",
    ]


def test_hunt_status_with_nothing_picked(hunt: JobsList, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == "No picked jobs yet.\n"


def test_hunt_status_tells_how_to_answer_a_question(hunt: JobsList, project: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    job = _pause(project, hunt, QUESTION)
    capsys.readouterr()
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1] == (
        f'  {job}. QA Engineer 1 at Example Co 1: needs an answer to "Do you hold a current ISTQB certificate?",'
        f' in the Chrome tab "Apply - Example Co 1" (then: hunt answer {job} "<the answer>")')


def test_hunt_answer_and_resume_job_on_the_command_line(hunt: JobsList, project: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    job = _pause(project, hunt, QUESTION)
    assert cli.main(["answer", str(job), "Yes."]) == 0
    assert capsys.readouterr().out == (
        f"saved the answer; job {job} (QA Engineer 1 at Example Co 1) goes on at the next hunt apply\n")
    assert cli.main(["resume-job", str(job)]) == 1
    assert capsys.readouterr().err == f"hunt resume-job: job {job} is not paused, it is resume_ready\n"


def test_hunt_apply_on_the_command_line_prints_what_it_did(
    hunt: JobsList, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _picked(hunt, 1)
    monkeypatch.setattr(cli, "run_resume", Resumes(project))
    monkeypatch.setattr(cli, "run_site", Sites(project))
    assert cli.main(["apply"]) == 0
    assert capsys.readouterr().out == "1 resumes written; 1 applied, 0 paused, 0 failed\n"
    assert hunt.get(job).state == "applied"


def test_a_jobs_file_from_before_failures_were_counted_gains_the_count(tmp_path: Path) -> None:
    import sqlite3

    from job_hunt.jobs import SCHEMA

    path = tmp_path / "jobs.db"
    older = sqlite3.connect(path)
    older.executescript(SCHEMA.replace("    failures INTEGER NOT NULL DEFAULT 0,\n", ""))
    older.execute("INSERT INTO jobs (link, title, company, board, state, found_at, updated_at)"
                  " VALUES ('https://jobs.example.com/1', 'QA', 'Example Co', 'indeed', 'picked', 'now', 'now')")
    older.commit()
    older.close()
    jobs = JobsList(path)
    assert jobs.get(1).failures == 0
    assert jobs.fail(1, "The site timed out.") == "picked"
    assert jobs.fail(1, "The site timed out again.") == "failed"


def test_a_resumed_job_carries_on_in_the_tab_it_paused_in(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, {"result": "paused", "needs": "captcha", "tab": "Application", "tab_id": 41})
    resume_job(jobs, job)
    commands: list[list[str]] = []
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=lambda command: (commands.append(command), sites(command)))
    sent = json.loads(commands[0][3].removeprefix(f"/{SITE_SKILL} "))
    # Two jobs may pause on pages titled alike: the id names the tab, the title is for the user.
    assert (sent["tab"], sent["tab_id"]) == ("Application", 41)
    # A job that did not pause before starts in a new tab.
    assert "tab" not in json.loads(site_command(jobs.get(_picked(jobs, 2)), project)[3].removeprefix(f"/{SITE_SKILL} "))


def test_a_report_written_before_the_worker_stopped_badly_still_counts(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project)

    def sent_then_timed_out(command: list[str]) -> None:
        sites(command)
        raise ApplyError("the apply-site run did not finish in 30 minutes")

    apply(jobs, project, run_resume=Resumes(project), run_site=sent_then_timed_out)
    assert (sites.ran, jobs.get(job).state) == ([job], "applied")


def test_a_failure_reported_before_the_worker_stopped_badly_is_tried_again(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: [{"result": "failed", "reason": "The site showed an error page."},
                                  {"result": "applied"}]})

    def reported_then_timed_out(command: list[str]) -> None:
        sites(command)
        raise ApplyError("the apply-site run did not finish in 30 minutes")

    # The worker said it sent nothing: the one case a stop is tried again.
    apply(jobs, project, run_resume=Resumes(project), run_site=reported_then_timed_out)
    assert sites.ran == [job, job]
    assert (jobs.get(job).state, jobs.get(job).failures) == ("applied", 1)


def test_a_needs_that_is_not_a_word_pauses_the_job_and_does_not_crash_the_run(project: Path, jobs: JobsList) -> None:
    job, other = _picked(jobs, 1), _picked(jobs, 2)
    report = '{"result": "paused", "needs": ["captcha"], "tab": "Apply"}'
    apply(jobs, project, run_resume=Resumes(project), run_site=Sites(project, {job: [report]}))
    assert (jobs.get(job).state, jobs.get(job).failures) == ("paused", 0)
    assert jobs.get(other).state == "applied"


@pytest.mark.parametrize(("left", "state"), [
    ('{"result": "failed", "reason": "The site showed an error page."}', "applied"),
    ('{"result": "paused", "needs": "captcha", "tab": "Apply"}', "paused"),
    ("{not json", "paused"),
    (b'{"result": "paused", "tab": "\xc3', "paused"),
])
def test_every_report_a_stopped_run_left_is_saved_and_a_broken_one_pauses(
    project: Path, jobs: JobsList, left: str | bytes, state: str
) -> None:
    job = _picked(jobs, 1)
    jobs.move(job, "resume_ready")
    jobs.move(job, "applying")
    (project / "jobs" / str(job)).mkdir()
    report = project / "jobs" / str(job) / "report.json"
    report.write_bytes(left) if isinstance(left, bytes) else report.write_text(left)
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    # A failure counts once and the job is tried again; a pause or a broken report waits for the user.
    assert jobs.get(job).state == state
    assert sites.ran == ([job] if state == "applied" else [])
    assert jobs.get(job).failures == (1 if state == "applied" else 0)


def test_only_a_worker_of_a_picked_or_applying_job_can_fail(jobs: JobsList) -> None:
    from job_hunt.jobs import MoveError

    job = _picked(jobs, 1)
    jobs.move(job, "resume_ready")
    with pytest.raises(MoveError, match="resume_ready"):
        jobs.fail(job, "why")
    assert jobs.get(job).failures == 0


def test_a_job_whose_way_to_apply_is_not_known_keeps_its_resume_and_waits(project: Path, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    jobs.score(job, 8, "Good match.")
    jobs.move(job, "picked")
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert (jobs.get(job).state, sites.ran) == ("resume_ready", [])


def test_hunt_resume_job_and_the_status_of_a_stopped_run_on_the_command_line(
    hunt: JobsList, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _picked(hunt, 1)
    hunt.move(job, "resume_ready")
    hunt.move(job, "applying")
    apply(hunt, project, run_resume=Resumes(project), run_site=Sites(project))
    capsys.readouterr()
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1] == (
        f"  {job}. QA Engineer 1 at Example Co 1: needs a look in Chrome whether it was sent: the last hunt apply"
        f" stopped while applying (then: hunt applied {job} if it was sent, hunt resume-job {job} if not)")
    assert cli.main(["resume-job", str(job)]) == 0
    assert capsys.readouterr().out == f"job {job} (QA Engineer 1 at Example Co 1) goes on at the next hunt apply\n"
    assert hunt.get(job).state == "resume_ready"


def test_hunt_apply_while_another_runs_says_so(hunt: JobsList, project: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    import fcntl

    with (project / "jobs" / "apply.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert cli.main(["apply"]) == 1
    assert capsys.readouterr().err == "hunt apply: another hunt apply is already running\n"


def test_hunt_applied_ends_a_job_the_user_saw_was_sent(hunt: JobsList, project: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    job = _picked(hunt, 1)
    _unsure_after_two_runs(project, hunt, job, lambda command: None)
    capsys.readouterr()
    assert cli.main(["applied", str(job)]) == 0
    assert capsys.readouterr().out == f"job {job} (QA Engineer 1 at Example Co 1) is applied to\n"
    assert (hunt.get(job).state, hunt.get(job).state_reason) == ("applied", None)
    sites = Sites(project)
    apply(hunt, project, run_resume=Resumes(project), run_site=sites)
    assert sites.ran == []
    assert cli.main(["applied", str(job)]) == 1
    assert capsys.readouterr().err == f"hunt applied: job {job} is not paused, it is applied\n"
    assert cli.main(["applied", "99"]) == 1
    assert capsys.readouterr().err == "hunt applied: there is no job 99\n"


def _two_questions(project: Path, jobs: JobsList) -> tuple[int, int]:
    first, second = _picked(jobs, 1), _picked(jobs, 2)
    sites = Sites(project, {first: [QUESTION], second: [{**QUESTION, "question": "Notice period?"}]})
    apply(jobs, project, run_resume=Resumes(project), run_site=sites)
    assert {jobs.get(first).state, jobs.get(second).state} == {"paused"}
    return first, second


def test_two_answers_given_at_once_are_both_kept(project: Path, jobs: JobsList, monkeypatch: pytest.MonkeyPatch) -> None:
    import job_hunt.apply as applying

    first, second = _two_questions(project, jobs)
    read, write = applying.read_career, applying.write_career
    first_writing, second_read, release = threading.Event(), threading.Event(), threading.Event()

    def reading(path: Path) -> Career:
        if threading.current_thread().name == "second":
            second_read.set()
        return read(path)

    def held_before_writing(career: Career, path: Path) -> None:
        if threading.current_thread().name == "first":
            first_writing.set()
            release.wait(5)
        write(career, path)

    monkeypatch.setattr(applying, "read_career", reading)
    monkeypatch.setattr(applying, "write_career", held_before_writing)
    # Two hunt answer commands, each with its own connection to the jobs list.
    one = threading.Thread(name="first", target=lambda: answer(
        JobsList(project / "data" / "jobs.db"), project, first, "Yes."))
    two = threading.Thread(name="second", target=lambda: answer(
        JobsList(project / "data" / "jobs.db"), project, second, "Two weeks."))
    one.start()
    assert first_writing.wait(5)
    two.start()
    # The second waits until the first has written: it does not read the career meanwhile.
    assert not second_read.wait(0.3)
    release.set()
    one.join(5)
    two.join(5)
    saved = read_career(project / "data" / "career.json").form_answers["saved"]
    assert sorted(entry["answer"] for entry in saved) == ["Two weeks.", "Yes."]


def test_the_career_is_replaced_whole_never_written_in_place(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, QUESTION)
    career_file = project / "data" / "career.json"
    before = career_file.stat().st_ino
    answer(jobs, project, job, "Yes.")
    # A site worker reading it meanwhile sees the old file or the new one, never half of one.
    assert career_file.stat().st_ino != before
    assert career_file.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in (project / "data").iterdir()) == ["career.json", "career.lock", "jobs.db"]


def test_a_career_that_cannot_be_written_stays_as_it_was(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    career_file = project / "data" / "career.json"
    before = career_file.read_text()
    career = read_career(career_file)
    career.summary = "Changed."

    def full_disk(source: str, target: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError, match="No space"):
        write_career(career, career_file)
    assert career_file.read_text() == before
    # No copy of the career is left behind in data/.
    assert sorted(path.name for path in (project / "data").iterdir()) == ["career.json"]


def test_hunt_applied_takes_any_paused_job(hunt: JobsList, project: Path) -> None:
    # The user solved the CAPTCHA and sent the form in the tab themselves.
    job = _pause(project, hunt, {"result": "paused", "needs": "captcha", "tab": "Apply - Example Co 1"})
    assert cli.main(["applied", str(job)]) == 0
    assert hunt.get(job).state == "applied"


def test_a_resume_worker_may_build_only_its_own_jobs_resume(project: Path) -> None:
    tools = resume_command(1, project)[resume_command(1, project).index("--allowedTools") + 1].split(",")
    # A rule ending in `:*` would let job 1's worker build, and overwrite, job 12's resume too.
    assert [tool for tool in tools if tool.startswith("Bash(")] == ["Bash(uv run hunt resume jobs/1)"]


SENT_CHECK = {"result": "paused", "needs": "check_sent", "tab": "Thank you - Example Co 1", "tab_id": 7}


def test_a_worker_that_submitted_without_a_confirmation_pauses_for_a_look(
    hunt: JobsList, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _picked(hunt, 1)
    # Submit was clicked, then the page showed an error: it may have been sent.
    _unsure_after_two_runs(project, hunt, job, Sites(project, {job: [SENT_CHECK]}))
    assert hunt.get(job).state_reason == 'a look in Chrome whether it was sent, in the Chrome tab "Thank you - Example Co 1"'
    capsys.readouterr()
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1].endswith(
        f"(then: hunt applied {job} if it was sent, hunt resume-job {job} if not)")


def test_chrome_out_of_reach_pauses_every_job_and_costs_none_a_try(
    hunt: JobsList, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    picked = [_picked(hunt, n) for n in range(1, 4)]
    closed = {job: [{"result": "paused", "needs": "chrome"}] for job in picked}
    sites = Sites(project, closed)
    apply(hunt, project, run_resume=Resumes(project), run_site=sites)
    assert sorted(sites.ran) == picked
    assert {(hunt.get(job).state, hunt.get(job).failures) for job in picked} == {("paused", 0)}
    assert hunt.get(picked[0]).state_reason == "Chrome open, with the Claude extension connected"
    capsys.readouterr()
    cli.main(["status"])
    assert capsys.readouterr().out.splitlines()[1] == (
        f"  {picked[0]}. QA Engineer 1 at Example Co 1: needs Chrome open, with the Claude extension connected"
        f" (then: hunt resume-job {picked[0]})")
    # Once Chrome is back, each job goes on.
    resume_job(hunt, picked[0])
    apply(hunt, project, run_resume=Resumes(project), run_site=Sites(project))
    assert hunt.get(picked[0]).state == "applied"


def test_a_job_resumed_after_chrome_was_out_of_reach_starts_in_a_new_tab(project: Path, jobs: JobsList) -> None:
    job = _pause(project, jobs, {"result": "paused", "needs": "chrome"})
    resume_job(jobs, job)
    commands: list[list[str]] = []
    sites = Sites(project)
    apply(jobs, project, run_resume=Resumes(project), run_site=lambda command: (commands.append(command), sites(command)))
    # It paused in no tab: the worker is given none, not a tab named null.
    sent = json.loads(commands[0][3].removeprefix(f"/{SITE_SKILL} "))
    assert "tab" not in sent and "tab_id" not in sent


def test_a_worker_that_crashes_leaving_a_broken_report_is_named_by_its_error(project: Path, jobs: JobsList) -> None:
    job = _picked(jobs, 1)
    sites = Sites(project, {job: ["{not json"]})

    def wrote_half_then_crashed(command: list[str]) -> None:
        sites(command)
        raise ApplyError("the apply-site run failed (1): the connection to Chrome was lost")

    _unsure_after_two_runs(project, jobs, job, wrote_half_then_crashed)
    assert jobs.get(job).state_reason == UNSURE + "the apply-site run failed (1): the connection to Chrome was lost"
