"""`hunt morning`: find, score when anything is new, and the day's session only when something waits.

No Claude and no tmux: the steps and the commands are stand-ins that record what they were asked.
"""

from __future__ import annotations

import fcntl
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt.finder import FinderError
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.morning import FIRST_MESSAGE, SESSION, morning, start_session
from job_hunt.questions import questions
from job_hunt.scoring import ScoreError
from movable_clock import MovableClock


def _found(number: int) -> FoundJob:
    return FoundJob(link=f"https://www.linkedin.com/jobs/view/{number}", title="QA Automation Engineer",
                    company="Example Labs", place="Durham, NC", remote="hybrid", board="linkedin", email_id="1",
                    email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


@pytest.fixture
def jobs(tmp_path: Path, clock: MovableClock) -> JobsList:
    return JobsList(tmp_path / "data" / "jobs.db", clock=clock)


class Steps:
    """Stands in for hunt find, hunt score and the session start, in the order they ran."""

    def __init__(self, jobs: JobsList, *, finds: int = 0, find_error: Exception | None = None,
                 score_error: Exception | None = None) -> None:
        self.jobs, self.finds, self.find_error, self.score_error = jobs, finds, find_error, score_error
        self.ran: list[str] = []
        # Whether the day's session runs, as tmux would say.
        self.running = False

    def find(self) -> None:
        self.ran.append("find")
        for number in range(1, self.finds + 1):
            self.jobs.add(_found(number))
        if self.find_error:
            raise self.find_error

    def score(self) -> None:
        self.ran.append("score")
        if self.score_error:
            raise self.score_error
        for job in self.jobs.in_states("new"):
            self.jobs.score(job.id, 7, "A reason.")

    def start(self) -> None:
        self.ran.append("start")


def _morning(steps: Steps, jobs: JobsList, data: Path, clock: MovableClock) -> str:
    return morning(jobs, data, find=steps.find, score=steps.score, start=steps.start,
                   running=lambda: steps.running, clock=clock)


def test_a_later_run_of_the_day_leaves_the_running_session_alone(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    _morning(Steps(jobs, finds=1), jobs, tmp_path / "data", clock)
    clock.advance(hours=5)
    later = Steps(jobs, finds=3)
    later.running = True

    said = _morning(later, jobs, tmp_path / "data", clock)

    # The new jobs are found and scored; the chat asks for them at its next hunt questions.
    assert later.ran == ["find", "score"]
    assert "already runs" in said


def test_a_later_run_starts_the_session_when_none_runs(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    _morning(Steps(jobs), jobs, tmp_path / "data", clock)
    clock.advance(hours=5)
    later = Steps(jobs, finds=1)

    _morning(later, jobs, tmp_path / "data", clock)

    assert later.ran == ["find", "score", "start"]


def test_the_first_run_of_the_next_day_starts_a_fresh_session(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    _morning(Steps(jobs, finds=1), jobs, tmp_path / "data", clock)
    clock.advance(days=1)
    tomorrow = Steps(jobs)
    tomorrow.running = True

    _morning(tomorrow, jobs, tmp_path / "data", clock)

    assert tomorrow.ran == ["find", "start"]


def test_nothing_new_starts_nothing(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    steps = Steps(jobs)

    said = _morning(steps, jobs, tmp_path / "data", clock)

    assert steps.ran == ["find"]
    assert "no session" in said


def test_new_jobs_are_scored_and_start_the_session(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    steps = Steps(jobs, finds=2)

    _morning(steps, jobs, tmp_path / "data", clock)

    assert steps.ran == ["find", "score", "start"]


def test_a_paused_application_alone_starts_the_session(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    job = jobs.add(_found(1), kind="company_site")
    assert job is not None
    jobs.score(job, 6, "A reason.")
    for state in ("picked", "resume_ready", "applying"):
        jobs.move(job, state)  # type: ignore[arg-type]
    jobs.move(job, "paused", "a sign-in or a new account")
    steps = Steps(jobs)

    _morning(steps, jobs, tmp_path / "data", clock)

    assert steps.ran == ["find", "start"]


def test_a_failed_find_is_logged_still_scores_and_is_told_once(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    data = tmp_path / "data"
    # The jobs it saved before it failed are still scored.
    steps = Steps(jobs, finds=1, find_error=FinderError("the read-job-alerts run failed (1): no Gmail"))

    _morning(steps, jobs, data, clock)

    assert steps.ran == ["find", "score", "start"]
    assert (data / "morning.log").read_text() == (
        "2026-11-18T07:00:00+00:00 hunt find: the read-job-alerts run failed (1): no Gmail\n")
    assert "no Gmail" in questions(jobs, data)
    assert "no Gmail" not in questions(jobs, data)


def test_a_failed_score_alone_starts_the_session_to_tell_it(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    data = tmp_path / "data"
    steps = Steps(jobs, finds=1, score_error=ScoreError("the score-jobs run wrote no score file"))

    _morning(steps, jobs, data, clock)

    assert steps.ran == ["find", "score", "start"]
    assert "hunt score: the score-jobs run wrote no score file" in (data / "morning.log").read_text()


def test_an_unexpected_error_is_logged_with_its_kind(jobs: JobsList, tmp_path: Path, clock: MovableClock) -> None:
    data = tmp_path / "data"
    steps = Steps(jobs, find_error=OSError("disk full"))

    _morning(steps, jobs, data, clock)

    assert "hunt find: OSError: disk full" in (data / "morning.log").read_text()


class Tmux:
    """Stands in for tmux: records each command; the pane shows the given text."""

    def __init__(self, pane: str = "", *, start_fails: bool = False) -> None:
        self.pane, self.start_fails = pane, start_fails
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        failed = command[1] == "new-session" and self.start_fails
        out = self.pane if command[1] == "capture-pane" else ""
        return subprocess.CompletedProcess(command, 1 if failed else 0, out, "no server" if failed else "")


def test_the_session_ends_yesterdays_and_starts_one_with_remote_control(tmp_path: Path) -> None:
    tmux = Tmux("Remote Control: https://claude.ai/code/session_01Example\n")

    start_session(tmp_path, run=tmux, sleep=lambda seconds: None)

    assert tmux.commands[0] == ["tmux", "kill-session", "-t", f"={SESSION}"]
    started = [command for command in tmux.commands if command[1] == "new-session"]
    assert len(started) == 1
    command = started[0]
    assert command[:7] == ["tmux", "new-session", "-d", "-s", SESSION, "-c", str(tmp_path)]
    claude = command[command.index("claude"):]
    assert claude[claude.index("--remote-control") + 1] == SESSION
    assert claude[claude.index("--name") + 1] == SESSION
    assert claude[claude.index("--allowedTools") + 1] == "Bash(uv run hunt:*)"
    assert claude[-1] == FIRST_MESSAGE
    assert "hunt questions" in FIRST_MESSAGE
    assert (tmp_path / "data" / "session.json").read_text() == (
        '{"link": "https://claude.ai/code/session_01Example"}\n')


def test_only_the_session_of_that_exact_name_is_ended_or_read(tmp_path: Path) -> None:
    tmux = Tmux("https://claude.ai/code/session_01Example")

    start_session(tmp_path, run=tmux, sleep=lambda seconds: None)

    # A bare name is a prefix to tmux: it would end or read a `job-hunt-notes` session.
    targets = [command[command.index("-t") + 1] for command in tmux.commands if "-t" in command]
    assert targets and all(target.startswith(f"={SESSION}") for target in targets)


def test_a_session_whose_link_never_shows_keeps_no_old_link(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "session.json").write_text('{"link": "https://claude.ai/code/session_yesterday"}\n')
    waited: list[float] = []

    start_session(tmp_path, run=Tmux("Welcome to Claude Code\n"), sleep=waited.append)

    assert not (tmp_path / "data" / "session.json").exists()
    assert 0 < sum(waited) <= 30


def test_a_session_that_cannot_start_says_why(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="tmux could not start the job-hunt session: no server"):
        start_session(tmp_path, run=Tmux(start_fails=True), sleep=lambda seconds: None)


def test_yesterdays_session_is_not_ended_while_hunt_apply_runs(tmp_path: Path) -> None:
    (tmp_path / "jobs").mkdir()
    tmux = Tmux("https://claude.ai/code/session_01Example")
    with (tmp_path / "jobs" / "apply.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        starting = threading.Thread(target=start_session, args=(tmp_path,),
                                    kwargs={"run": tmux, "sleep": lambda seconds: None})
        starting.start()
        starting.join(0.5)
        assert starting.is_alive() and tmux.commands == []
    starting.join(5)
    assert tmux.commands[0][1] == "kill-session"


def test_a_session_that_did_not_start_is_logged_for_the_next_morning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from job_hunt import cli

    data = tmp_path / "data"
    jobs = JobsList(data / "jobs.db")
    job = jobs.add(_found(1))
    assert job is not None
    jobs.score(job, 7, "A reason.")
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "find_step", lambda: None)
    monkeypatch.setattr(cli, "score_step", lambda: None)

    def no_tmux(root: Path) -> None:
        raise RuntimeError("tmux is not installed or not on the PATH")

    monkeypatch.setattr(cli, "start_session", no_tmux)

    assert cli.main(["morning"]) == 1
    assert "hunt morning: tmux is not installed" in (data / "morning.log").read_text()
    assert "tmux is not installed" in capsys.readouterr().err


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["tmux"], 60), PermissionError("jobs/apply.lock")])
def test_any_failure_to_start_the_session_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from job_hunt import cli

    data = tmp_path / "data"
    job = JobsList(data / "jobs.db").add(_found(1))
    assert job is not None
    JobsList(data / "jobs.db").score(job, 7, "A reason.")
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "find_step", lambda: None)
    monkeypatch.setattr(cli, "score_step", lambda: None)

    def failing(root: Path) -> None:
        raise error

    monkeypatch.setattr(cli, "start_session", failing)

    assert cli.main(["morning"]) == 1
    assert "hunt morning: " in (data / "morning.log").read_text()


def test_a_hung_tmux_is_a_failure_in_words() -> None:
    from job_hunt import morning as module

    def hung(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["tmux"], 60)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module.subprocess, "run", hung)
        with pytest.raises(RuntimeError, match="tmux did not answer in 60 seconds"):
            module.tmux(["tmux", "kill-session", "-t", SESSION])


def test_a_session_that_ends_as_it_starts_is_a_failure(tmp_path: Path) -> None:
    class Gone(Tmux):
        def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
            done = super().__call__(command)
            if command[1] == "capture-pane":
                return subprocess.CompletedProcess(command, 1, "", f"can't find session: {SESSION}")
            return done

    with pytest.raises(RuntimeError, match="ended as it started"):
        start_session(tmp_path, run=Gone(), sleep=lambda seconds: None)


def test_jobs_the_scoring_left_out_are_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt import cli
    from job_hunt.scoring import Summary

    data = tmp_path / "data"
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "find_step", lambda: JobsList(data / "jobs.db").add(_found(1)))
    monkeypatch.setattr(cli, "score_step", lambda: Summary(0, ["job 1 (QA Automation Engineer at Example Labs)"]))
    started: list[Path] = []
    monkeypatch.setattr(cli, "start_session", started.append)

    assert cli.main(["morning"]) == 0

    assert "hunt score: not scored, still new: job 1 (QA Automation Engineer at Example Labs)" in (
        data / "morning.log").read_text()
    assert started == [tmp_path]


def test_jobs_the_finder_left_out_are_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt import cli
    from job_hunt.finder import Summary as Found

    data = tmp_path / "data"
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "find_step", lambda: Found(1, 1, 0, ["entry 1: no title"]))
    monkeypatch.setattr(cli, "score_step", lambda: None)
    started: list[Path] = []
    monkeypatch.setattr(cli, "start_session", started.append)

    assert cli.main(["morning"]) == 0

    assert "hunt find: left out entry 1: no title" in (data / "morning.log").read_text()
    assert started == [tmp_path]


def test_the_session_runs_when_tmux_has_it_by_its_exact_name() -> None:
    from job_hunt.morning import session_running

    asked: list[list[str]] = []

    def answer(code: int):
        def run(command: list[str]) -> subprocess.CompletedProcess[str]:
            asked.append(command)
            return subprocess.CompletedProcess(command, code, "", "")
        return run

    assert session_running(run=answer(0)) is True
    assert session_running(run=answer(1)) is False
    assert asked[0] == ["tmux", "has-session", "-t", f"={SESSION}"]


def test_a_recruiters_email_found_in_the_morning_is_drafted_and_starts_the_session(
    jobs: JobsList, tmp_path: Path, clock: MovableClock
) -> None:
    from job_hunt.replies import Threads, read_messages

    data = tmp_path / "data"
    threads = Threads(data / "jobs.db", clock=clock)
    (message,), _ = read_messages({"recruiter_emails": [{
        "email_id": "m1", "thread_id": "t1", "from": "Jane Recruiter <jane@example.com>", "text": "Hi Ada"}]})
    steps = Steps(jobs)
    drafted: list[int] = []

    def find() -> None:
        steps.find()
        threads.save(message, jobs)

    def draft() -> None:
        for thread in threads.in_states("new"):
            threads.drafted(thread.id, "Thanks, Jane.", None)
            drafted.append(thread.id)

    morning(jobs, data, find=find, score=steps.score, start=steps.start, running=lambda: False,
            clock=clock, threads=threads, draft=draft)

    assert drafted and steps.ran == ["find", "start"]
