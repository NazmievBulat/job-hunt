"""`hunt score`: the new jobs scored by one skill run, its score file read and checked.

No model: the skill's run is replaced by writing a score file, as the skill would.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.scoring import MODEL, SKILL, ScoreError, read_scores, score, skill_command
from movable_clock import MovableClock
from only_named_tools import assert_only_named_tools


def _found(number: int, **change: object) -> FoundJob:
    job = dict(link=f"https://www.linkedin.com/jobs/view/{number}", title="QA Automation Engineer",
               company="Example Labs", place="Durham, NC", remote="hybrid", board="linkedin", email_id="1",
               email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")
    return FoundJob(**{**job, **change})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "career.json").write_text('{"person": {"name": "Ada Example"}}\n')
    return tmp_path


@pytest.fixture
def jobs(project: Path) -> JobsList:
    clock = MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))
    return JobsList(project / "data" / "jobs.db", clock=clock)


class FakeSkill:
    """Stands in for the `claude -p` run: writes the given scores to the file it was told to."""

    def __init__(self, scores: object) -> None:
        self.scores, self.commands = scores, []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(command)
        arguments = json.loads(command[2].removeprefix(f"/{SKILL} "))
        out = Path(arguments["out"])
        out.write_text(self.scores if isinstance(self.scores, str) else json.dumps(self.scores))


def test_the_skill_reads_only_the_postings_sent_and_the_career_and_writes_only_its_file(project: Path) -> None:
    postings = [str(project / "jobs" / "3" / "posting.md"), str(project / "jobs" / "5" / "posting.md")]
    command = skill_command([{"id": 3, "posting": postings[0]}, {"id": 5, "posting": postings[1]}], project)
    assert command[:2] == ["claude", "-p"]
    arguments = json.loads(command[2].removeprefix(f"/{SKILL} "))
    assert arguments == {
        "jobs": [{"id": 3, "posting": postings[0]}, {"id": 5, "posting": postings[1]}],
        "career": str(project / "data" / "career.json"),
        "out": str(project / "jobs" / "scores.json"),
    }
    assert command[command.index("--model") + 1] == MODEL == "claude-haiku-5-5"
    assert_only_named_tools(command)
    # Not another job's folder, nor the recruiters' messages under jobs/replies.
    assert command[command.index("--allowedTools") + 1].split(",") == [
        f"Read(/{postings[0]})", f"Read(/{postings[1]})",
        f"Read(/{project}/data/career.json)",
        f"Edit(/{project}/jobs/scores.json)",
    ]


def test_score_saves_each_new_jobs_score_and_reason_and_moves_it_to_scored(project: Path, jobs: JobsList) -> None:
    first, second = jobs.add(_found(1)), jobs.add(_found(2, title="SDET"))
    run = FakeSkill({"scores": [
        {"job": first, "score": 8, "reason": "Selenium and Python, as in the last two jobs."},
        {"job": second, "score": 3, "reason": "Asks for\n  five years of Java."},
    ]})
    summary = score(jobs, project, run=run)
    assert (summary.scored, summary.missing) == (2, [])
    assert str(summary) == "2 jobs scored"
    one, two = jobs.get(first), jobs.get(second)
    assert (one.state, one.score, one.score_reason) == ("scored", 8, "Selenium and Python, as in the last two jobs.")
    # The reason is one line, whatever the skill wrote.
    assert (two.state, two.score, two.score_reason) == ("scored", 3, "Asks for five years of Java.")
    sent = json.loads(run.commands[0][2].removeprefix(f"/{SKILL} "))["jobs"]
    assert sent == [
        {"id": first, "title": "QA Automation Engineer", "company": "Example Labs",
         "posting": str(project / "jobs" / str(first) / "posting.md")},
        {"id": second, "title": "SDET", "company": "Example Labs",
         "posting": str(project / "jobs" / str(second) / "posting.md")},
    ]


def test_score_with_no_new_job_starts_no_run(project: Path, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    jobs.score(job, 5, "Fair.")
    run = FakeSkill({"scores": []})
    summary = score(jobs, project, run=run)
    assert run.commands == []
    assert str(summary) == "no new jobs to score"


def test_a_new_job_the_skill_left_out_stays_new_and_is_named(project: Path, jobs: JobsList) -> None:
    first, second = jobs.add(_found(1)), jobs.add(_found(2, title="SDET", company="Globex Example"))
    summary = score(jobs, project, run=FakeSkill({"scores": [{"job": first, "score": 6, "reason": "Close."}]}))
    assert (summary.scored, summary.missing) == (1, [f"job {second} (SDET at Globex Example)"])
    assert jobs.get(second).state == "new"


def test_a_score_file_left_from_an_earlier_run_is_never_read_as_this_runs(project: Path, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    (project / "jobs").mkdir()
    (project / "jobs" / "scores.json").write_text(json.dumps({"scores": [{"job": job, "score": 9, "reason": "Old."}]}))
    with pytest.raises(ScoreError, match="wrote no score file"):
        score(jobs, project, run=lambda command: None)
    assert jobs.get(job).state == "new"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("not json", "not JSON"),
        ('{"jobs": []}', 'a list of "scores"'),
        ('[{"job": 1, "score": 5, "reason": "x"}]', 'a list of "scores"'),
    ],
)
def test_a_score_file_that_is_not_the_skills_shape_is_refused(
    project: Path, jobs: JobsList, content: str, error: str
) -> None:
    jobs.add(_found(1))
    with pytest.raises(ScoreError, match=error):
        score(jobs, project, run=FakeSkill(content))
    assert jobs.get(1).state == "new"


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ("job 2 is a 5", "entry 2 is not an object"),
        ({"job": "2", "score": 5, "reason": "Fine."}, "entry 2 names no job"),
        ({"job": True, "score": 5, "reason": "Fine."}, "entry 2 names no job"),
        ({"job": 99, "score": 5, "reason": "Fine."}, "job 99, which was not sent to be scored"),
        ({"job": 1, "score": 6, "reason": "Again."}, "scores job 1 twice"),
        ({"job": 2, "score": 11, "reason": "Fine."}, "a whole number from 1 to 10"),
        ({"job": 2, "score": 0, "reason": "Fine."}, "a whole number from 1 to 10"),
        ({"job": 2, "score": 7.5, "reason": "Fine."}, "a whole number from 1 to 10"),
        ({"job": 2, "score": True, "reason": "Fine."}, "a whole number from 1 to 10"),
        ({"job": 2, "score": 5, "reason": "  "}, "no reason"),
        ({"job": 2, "score": 5}, "no reason"),
    ],
)
def test_a_score_file_with_one_wrong_entry_is_refused_and_no_job_is_scored(
    project: Path, jobs: JobsList, entry: object, error: str
) -> None:
    assert (jobs.add(_found(1)), jobs.add(_found(2))) == (1, 2)
    good = {"job": 1, "score": 4, "reason": "Fine."}
    with pytest.raises(ScoreError, match=error):
        score(jobs, project, run=FakeSkill({"scores": [good, entry]}))
    assert [job.state for job in jobs.all()] == ["new", "new"]


def test_a_second_run_while_one_runs_fails_and_neither_reads_the_others_file(project: Path, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    second = FakeSkill({"scores": []})

    def first_run(command: list[str]) -> None:
        FakeSkill({"scores": [{"job": job, "score": 7, "reason": "Fine."}]})(command)
        # Started after the first run's skill wrote its file: refused before it touches anything.
        with pytest.raises(ScoreError, match="another hunt score is already running"):
            score(jobs, project, run=second)

    assert str(score(jobs, project, run=first_run)) == "1 jobs scored"
    assert second.commands == [] and jobs.get(job).score == 7


def test_a_run_that_failed_does_not_keep_the_next_one_waiting(project: Path, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    with pytest.raises(ScoreError, match="wrote no score file"):
        score(jobs, project, run=lambda command: None)
    assert str(score(jobs, project, run=FakeSkill({"scores": [{"job": job, "score": 6, "reason": "Fine."}]}))) == (
        "1 jobs scored")


def test_read_scores_names_the_file_it_refused(tmp_path: Path) -> None:
    path = tmp_path / "scores.json"
    path.write_text("{")
    with pytest.raises(ScoreError, match=f"the {SKILL} run wrote a file that is not JSON"):
        read_scores(path, {1})


FAKE_CLAUDE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, log)
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print("Credit balance is too low", file=sys.stderr)
    sys.exit(1)
arguments = json.loads(sys.argv[2].removeprefix("/score-jobs "))
with open(arguments["out"], "w") as out:
    json.dump({{"scores": [{{"job": job["id"], "score": 7, "reason": "Made up."}} for job in arguments["jobs"]]}}, out)
"""


@pytest.fixture
def fake_claude(project: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `claude` first on the PATH that records how it was started, then scores every job 7."""
    folder = tmp_path_factory.mktemp("bin")
    claude = folder / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(folder / "claude.json"))
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    return folder / "claude.json"


def test_hunt_score_starts_claude_in_the_run_folder_and_prints_one_line(
    fake_claude: Path, project: Path, jobs: JobsList, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.add(_found(1))
    assert cli.main(["score"]) == 0
    assert capsys.readouterr().out == "1 jobs scored\n"
    assert Path(json.loads(fake_claude.read_text())["cwd"]) == project / ".background"
    assert jobs.get(1).state == "scored"


def test_hunt_score_says_why_the_run_failed_and_the_jobs_stay_new(
    fake_claude: Path, jobs: JobsList, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.add(_found(1))
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    assert cli.main(["score"]) == 1
    assert capsys.readouterr().err == f"hunt score: the {SKILL} run failed (1): Credit balance is too low\n"
    assert jobs.get(1).state == "new"


def test_hunt_score_names_a_job_left_unscored(
    fake_claude: Path, project: Path, jobs: JobsList, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.add(_found(1))
    monkeypatch.setattr(cli, "run_scoring", FakeSkill({"scores": []}))
    assert cli.main(["score"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "0 jobs scored\n"
    assert captured.err == "hunt score: not scored, still new: job 1 (QA Automation Engineer at Example Labs)\n"
