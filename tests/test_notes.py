"""Notes to hiring managers: the poster a LinkedIn application saved, one note drafted for each
one a free `Message` reaches, asked about once for all, and sent only on the person's reply.

No model and no browser: the skill runs are stand-ins that write the files a run would.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.notes import (
    DRAFT_SKILL, LIMIT, SEND_SKILL, NoteError, Notes, collect, decide, draft_command, draft_notes, note_pause,
    send_command, send_notes, sendable, stopped_today,
)
from job_hunt.questions import anything_waiting, questions
from job_hunt.replies import Threads, read_messages
from job_hunt.settings import read_settings
from movable_clock import MovableClock

LOCAL = timezone(timedelta(hours=-5))
NOON = datetime(2026, 11, 18, 12, 0, tzinfo=LOCAL)


def _poster(number: int = 1, **change: object) -> dict:
    poster = {"name": f"Jane Doe {number}", "title": "Engineering Manager",
              "profile": f"https://www.linkedin.com/in/jane-doe-{number}/", "reach": "message"}
    return {**poster, **change}


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOON)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "jobs").mkdir()
    (tmp_path / "data" / "career.json").write_text(json.dumps({"person": {"name": "Ada Example"}}))
    return tmp_path


@pytest.fixture
def jobs(project: Path, clock: MovableClock) -> JobsList:
    return JobsList(project / "data" / "jobs.db", clock=clock)


@pytest.fixture
def notes(project: Path, clock: MovableClock) -> Notes:
    return Notes(project / "data" / "jobs.db", clock=clock)


@pytest.fixture
def threads(project: Path, clock: MovableClock) -> Threads:
    return Threads(project / "data" / "jobs.db", clock=clock)


def _applied(jobs: JobsList, project: Path, number: int, poster: dict | None = None, result: str = "applied") -> int:
    job = jobs.add(FoundJob(link=f"https://www.linkedin.com/jobs/view/{number}", title=f"QA Engineer {number}",
                            company=f"Example Co {number}", place="Durham, NC", remote="on_site", board="linkedin",
                            email_id="1", email_date="2026-11-17T07:00:00+00:00", area="Raleigh"), kind="easy_apply")
    assert job is not None
    jobs.score(job, 8, "fits")
    for state in ("picked", "resume_ready", "applying"):
        jobs.move(job, state)
    jobs.move(job, "applied") if result == "applied" else jobs.move(job, "paused", "a CAPTCHA solved")
    report = {"result": result}
    if poster is not None:
        report["poster"] = poster
    (project / "jobs" / str(job)).mkdir(exist_ok=True)
    (project / "jobs" / str(job) / "report.json").write_text(json.dumps(report))
    return job


def _prompt(command: list[str], skill: str) -> dict:
    prompt = command[command.index("-p") + 1]
    assert prompt.startswith(f"/{skill} ")
    return json.loads(prompt.removeprefix(f"/{skill} "))


class Drafter:
    """Stands in for the draft-note runs."""

    def __init__(self, text: str = "Hi Jane, I applied for QA Engineer and would love to help your team test.") -> None:
        self.text = text
        self.ran: list[int] = []

    def __call__(self, command: list[str]) -> None:
        arguments = _prompt(command, DRAFT_SKILL)
        self.ran.append(arguments["note"])
        Path(arguments["out"]).write_text(json.dumps({"note": self.text}))


class Sender:
    """Stands in for the send-note runs: writes the report it is given for each note, in turn."""

    def __init__(self, *reports: dict | None, lock: Path | None = None) -> None:
        self.reports = list(reports)
        self.sent: list[tuple[str, str]] = []
        self.locked: list[bool] = []
        self.lock = lock

    def __call__(self, command: list[str]) -> None:
        arguments = _prompt(command, SEND_SKILL)
        if self.lock is not None:
            self.locked.append(self.lock.exists())
        report = self.reports.pop(0) if self.reports else {
            "result": "sent", "conversation": f"https://www.linkedin.com/messaging/thread/{arguments['id']}/"}
        if report is None:
            return
        if report["result"] == "sent":
            self.sent.append((arguments["profile"], arguments["text"]))
        Path(arguments["report"]).write_text(json.dumps(report))


def _drafted(notes: Notes, jobs: JobsList, project: Path, *posters: dict) -> list[int]:
    for number, poster in enumerate(posters, start=1):
        _applied(jobs, project, number, poster)
    collect(notes, jobs, project)
    draft_notes(notes, jobs, project, run=Drafter())
    return [note.id for note in notes.in_states("drafted")]


def _send(notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock, run: Sender,
          daily: int = 15):
    lock = LinkedInLock(project / "data", clock=clock, pause=note_pause)
    return send_notes(notes, threads, jobs, project, run=run, lock=lock,
                      sleep=lambda seconds: clock.advance(seconds=seconds), daily=daily, clock=clock)


# The poster, saved with each way to reach them


@pytest.mark.parametrize("reach", ["message", "inmail", "connect"])
def test_a_poster_is_saved_with_each_way_to_reach_them(
        notes: Notes, jobs: JobsList, project: Path, reach: str) -> None:
    job = _applied(jobs, project, 1, _poster(reach=reach))

    assert collect(notes, jobs, project) == 1

    [note] = notes.all()
    assert (note.job, note.name, note.title, note.profile, note.reach) == (
        job, "Jane Doe 1", "Engineering Manager", "https://www.linkedin.com/in/jane-doe-1", reach)
    assert note.state == ("new" if reach == "message" else "unreachable")


def test_a_poster_is_saved_once_and_only_from_an_applied_job_with_a_poster(
        notes: Notes, jobs: JobsList, project: Path) -> None:
    _applied(jobs, project, 1, _poster(1))
    _applied(jobs, project, 2, None)
    _applied(jobs, project, 3, _poster(3), result="paused")
    _applied(jobs, project, 4, _poster(4, profile="https://example.com/jane"))

    assert collect(notes, jobs, project) == 1
    assert collect(notes, jobs, project) == 0
    assert [note.name for note in notes.all()] == ["Jane Doe 1"]


@pytest.mark.parametrize("reach", ["inmail", "connect"])
def test_a_poster_reached_only_by_inmail_or_connect_is_never_drafted_asked_or_sent(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock, reach: str) -> None:
    _applied(jobs, project, 1, _poster(reach=reach))
    collect(notes, jobs, project)
    drafter, sender = Drafter(), Sender()

    assert draft_notes(notes, jobs, project, run=drafter) == 0
    assert "hiring managers" not in questions(jobs, project / "data", threads, notes)
    with pytest.raises(NoteError):
        decide(notes, "send", None)
    _send(notes, threads, jobs, project, clock, sender)

    assert drafter.ran == [] and sender.sent == []
    assert not anything_waiting(jobs, project / "data", threads, notes)


# No connection request, ever


def test_no_step_can_make_a_connection_request() -> None:
    skill = (Path(cli.ROOT) / ".claude" / "skills" / SEND_SKILL / "SKILL.md").read_text()
    applying = (Path(cli.ROOT) / ".claude" / "skills" / "apply-linkedin" / "SKILL.md").read_text()

    assert "Never press Connect" in skill and "Never press Connect" in applying
    assert "InMail credit" in skill


def test_a_message_that_turned_out_not_free_sends_nothing_and_is_not_tried_again(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [note] = _drafted(notes, jobs, project, _poster())
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    sender = Sender({"result": "not_free", "reason": "Message would spend an InMail credit"})

    _send(notes, threads, jobs, project, clock, sender)
    _send(notes, threads, jobs, project, clock, sender)

    assert notes.get(note).state == "unreachable"
    assert sender.sent == []


# The note


def test_the_drafter_is_given_the_job_the_poster_and_the_limit_and_may_read_only_its_job_and_the_career(
        notes: Notes, jobs: JobsList, project: Path) -> None:
    job = _applied(jobs, project, 1, _poster())
    collect(notes, jobs, project)
    [note] = notes.all()

    command = draft_command(note, jobs.get(job), project)

    arguments = _prompt(command, DRAFT_SKILL)
    assert (arguments["title"], arguments["company"], arguments["poster"], arguments["limit"]) == (
        "QA Engineer 1", "Example Co 1", {"name": "Jane Doe 1", "title": "Engineering Manager"}, LIMIT)
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert sorted(tools) == sorted([f"Read(/{project}/jobs/{job}/**)", f"Read(/{project}/data/career.json)",
                                    f"Edit(/{project}/jobs/{job}/note.json)"])
    assert "--chrome" not in command


def test_a_note_longer_than_300_characters_is_drafted_again_then_failed(
        notes: Notes, jobs: JobsList, project: Path) -> None:
    _applied(jobs, project, 1, _poster())
    collect(notes, jobs, project)
    [note] = notes.all()
    drafter = Drafter("x" * (LIMIT + 1))

    assert draft_notes(notes, jobs, project, run=drafter) == 0
    assert notes.get(note.id).state == "new"
    assert draft_notes(notes, jobs, project, run=drafter) == 0
    assert notes.get(note.id).state == "failed"
    assert "300 characters" in (notes.get(note.id).state_reason or "")


def test_a_note_of_exactly_300_characters_is_kept(notes: Notes, jobs: JobsList, project: Path) -> None:
    _applied(jobs, project, 1, _poster())
    collect(notes, jobs, project)

    assert draft_notes(notes, jobs, project, run=Drafter("x" * LIMIT)) == 1


# One question for all of them


def test_the_notes_are_one_question_and_send_1_3_sends_only_those(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    first, second, third = _drafted(notes, jobs, project, _poster(1), _poster(2), _poster(3))

    asked = questions(jobs, project / "data", threads, notes)

    assert asked.count("Notes to hiring managers") == 1
    assert f"{first}. Jane Doe 1, Engineering Manager, Example Co 1" in asked
    assert "(then: hunt send-notes all, hunt send-notes <numbers>, hunt skip-notes)" in asked
    decide(notes, "send", [first, third])
    sender = Sender()
    _send(notes, threads, jobs, project, clock, sender)

    assert [profile for profile, _ in sender.sent] == [
        "https://www.linkedin.com/in/jane-doe-1", "https://www.linkedin.com/in/jane-doe-3"]
    assert notes.get(second).state == "skipped"
    assert "hiring managers" not in questions(jobs, project / "data", threads, notes)


def test_a_note_drafted_after_the_question_is_not_skipped_by_its_answer(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path) -> None:
    [first] = _drafted(notes, jobs, project, _poster(1))
    questions(jobs, project / "data", threads, notes)
    _applied(jobs, project, 2, _poster(2))
    collect(notes, jobs, project)
    draft_notes(notes, jobs, project, run=Drafter())

    decide(notes, "skip", None)

    assert notes.get(first).state == "skipped"
    assert [note.name for note in notes.in_states("drafted")] == ["Jane Doe 2"]


def test_nothing_is_sent_without_the_reply(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _drafted(notes, jobs, project, _poster(1))
    sender = Sender()

    _send(notes, threads, jobs, project, clock, sender)

    assert sender.sent == []


def test_a_number_not_asked_about_changes_nothing(notes: Notes, threads: Threads, jobs: JobsList, project: Path) -> None:
    [first] = _drafted(notes, jobs, project, _poster(1))
    questions(jobs, project / "data", threads, notes)

    with pytest.raises(NoteError, match="99"):
        decide(notes, "send", [first, 99])

    assert notes.get(first).state == "drafted"


# The daily cap and one note per person


def test_at_most_the_daily_cap_goes_out_and_the_rest_wait_for_tomorrow(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    drafted = _drafted(notes, jobs, project, *(_poster(number) for number in range(1, 5)))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    sender = Sender()

    said = _send(notes, threads, jobs, project, clock, sender, daily=2)

    assert len(sender.sent) == 2
    assert "2 a day" in str(said)
    assert sendable(notes, project / "data", 2, clock()) == []
    clock.now = NOON + timedelta(days=1)
    assert len(sendable(notes, project / "data", 2, clock())) == 2
    _send(notes, threads, jobs, project, clock, sender, daily=2)
    assert [notes.get(note).state for note in drafted] == ["sent"] * 4


def test_the_notes_are_a_few_minutes_apart_and_each_under_the_linkedin_lock(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _drafted(notes, jobs, project, _poster(1), _poster(2), _poster(3))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    sender = Sender(lock=project / "data" / "linkedin.lock")
    started = clock()

    _send(notes, threads, jobs, project, clock, sender)

    assert sender.locked == [True, True, True]
    assert timedelta(minutes=2) <= clock() - started <= timedelta(minutes=6)
    assert not (project / "data" / "linkedin.lock").exists()


def test_a_person_already_written_to_is_never_written_to_again(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [first] = _drafted(notes, jobs, project, _poster(1))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    _send(notes, threads, jobs, project, clock, Sender())
    _applied(jobs, project, 2, _poster(1, profile="https://www.linkedin.com/in/Jane-Doe-1?trk=x"))

    collect(notes, jobs, project)

    [again] = [note for note in notes.all() if note.id != first]
    assert again.state == "skipped"
    assert draft_notes(notes, jobs, project, run=Drafter()) == 0


def test_a_send_that_did_not_report_is_unsure_and_never_sent_again(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [note] = _drafted(notes, jobs, project, _poster(1))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    sender = Sender(None)

    _send(notes, threads, jobs, project, clock, sender)
    _send(notes, threads, jobs, project, clock, sender)

    assert notes.get(note).state == "unsure"
    assert sendable(notes, project / "data", 15, clock()) == []


# A LinkedIn warning ends the day's sending


def test_a_linkedin_warning_ends_sending_for_the_day_and_is_told_once(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _drafted(notes, jobs, project, _poster(1), _poster(2), _poster(3))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    sender = Sender({"result": "sent", "conversation": "https://www.linkedin.com/messaging/thread/a/"},
                    {"result": "refused", "reason": "LinkedIn says you have reached a weekly limit"})

    said = _send(notes, threads, jobs, project, clock, sender)
    again = _send(notes, threads, jobs, project, clock, sender)

    assert len(sender.sent) == 1
    assert "weekly limit" in str(said)
    assert stopped_today(project / "data", clock()) is not None
    assert sender.reports == []  # the second run started no send
    assert "weekly limit" in str(again)
    told = questions(jobs, project / "data", threads, notes)
    assert told.count("weekly limit") == 1
    assert "weekly limit" not in questions(jobs, project / "data", threads, notes)
    clock.now = NOON + timedelta(days=1)
    assert stopped_today(project / "data", clock()) is None
    assert len(sendable(notes, project / "data", 15, clock())) == 2


# The note's thread joins the recruiter replies


def test_a_sent_note_is_a_thread_and_the_answer_in_its_conversation_is_about_its_job(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _drafted(notes, jobs, project, _poster(1))
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    conversation = "https://www.linkedin.com/messaging/thread/abc/"
    _send(notes, threads, jobs, project, clock, Sender({"result": "sent", "conversation": conversation}))
    job = notes.all()[0].job

    [noted] = threads.in_states("sent")
    assert (noted.source, noted.conversation, noted.job, noted.sender) == ("linkedin", conversation, job, "Jane Doe 1")
    found, _ = read_messages({"linkedin_messages": [{
        "conversation": conversation + "?filter=unread", "from": "Jane Doe 1", "text": "Thanks Ada, let's talk.",
        "date": "2026-11-19T09:00:00-05:00", "job_link": None, "company": None, "asks_to_talk": True}]})
    answer = threads.save(found[0], jobs)

    assert answer is not None and threads.get(answer).job == job and threads.get(answer).state == "new"


def test_the_sender_may_only_use_chrome_and_write_its_report(notes: Notes, jobs: JobsList, project: Path) -> None:
    [note] = _drafted(notes, jobs, project, _poster(1))

    command = send_command(notes.get(note), project)

    arguments = _prompt(command, SEND_SKILL)
    assert arguments["profile"] == "https://www.linkedin.com/in/jane-doe-1"
    assert arguments["text"] == notes.get(note).note
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert all(tool.startswith("mcp__claude-in-chrome__") for tool in tools[:-1])
    assert tools[-1] == f"Edit(/{project}/jobs/{notes.get(note).job}/note-sent.json)"


# Settings


def test_the_daily_cap_is_a_setting_of_at_most_15(tmp_path: Path) -> None:
    base = {"titles": ["QA"], "areas": ["Raleigh"], "remote": "include", "lowest_salary": None, "morning_hour": 7}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(base))
    assert read_settings(path).linkedin_notes_daily == 15
    path.write_text(json.dumps({**base, "linkedin_notes_daily": 0}))
    assert read_settings(path).linkedin_notes_daily == 0
    path.write_text(json.dumps({**base, "linkedin_notes_daily": 16}))
    with pytest.raises(ValueError, match="linkedin_notes_daily"):
        read_settings(path)


# The commands


class FakeClaude:
    """Stands in for every `claude -p` the notes commands start, by its skill."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, command: list[str], folder: Path, *, skill: str, error: type[RuntimeError]) -> None:
        if skill == DRAFT_SKILL:
            arguments = _prompt(command, DRAFT_SKILL)
            Path(arguments["out"]).write_text(json.dumps({"note": f"Hi, I applied. ({arguments['note']})"}))
        elif skill == SEND_SKILL:
            arguments = _prompt(command, SEND_SKILL)
            self.sent.append(arguments["name"])
            Path(arguments["report"]).write_text(json.dumps(
                {"result": "sent", "conversation": f"https://www.linkedin.com/messaging/thread/{arguments['id']}/"}))
        else:
            raise AssertionError(f"no {skill} run expected")


@pytest.fixture
def hunt(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeClaude]:
    (project / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7,
        "linkedin_notes_daily": 5}))
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "note_pause", lambda: timedelta(0))
    fake = FakeClaude()
    monkeypatch.setattr(cli, "run_claude", fake)
    return project, fake


@pytest.mark.parametrize(("reply", "sent", "skipped"), [
    (["send-notes", "all"], ["Jane Doe 1", "Jane Doe 2", "Jane Doe 3"], []),
    (["send-notes", "1", "3"], ["Jane Doe 1", "Jane Doe 3"], ["Jane Doe 2"]),
    (["skip-notes"], [], ["Jane Doe 1", "Jane Doe 2", "Jane Doe 3"]),
])
def test_the_reply_to_the_notes_question_becomes_hunt_send_notes_or_skip_notes(
        hunt: tuple[Path, FakeClaude], capsys: pytest.CaptureFixture[str], reply: list[str], sent: list[str],
        skipped: list[str]) -> None:
    project, fake = hunt
    jobs = JobsList(project / "data" / "jobs.db")
    for number in (1, 2, 3):
        _applied(jobs, project, number, _poster(number))

    assert cli.main(["draft-notes"]) == 0
    assert "3 notes to hiring managers drafted" in capsys.readouterr().out
    assert cli.main(["send-notes"]) == 0
    assert fake.sent == []
    assert cli.main(["questions"]) == 0
    assert "Notes to hiring managers (3)" in capsys.readouterr().out
    assert cli.main(reply) == 0

    notes = Notes(project / "data" / "jobs.db")
    assert fake.sent == sent
    assert [note.name for note in notes.in_states("skipped")] == skipped
    assert cli.main(["status"]) == 0
    assert f"Notes to hiring managers sent ({len(sent)})" in capsys.readouterr().out or not sent


def test_a_reply_that_is_not_numbers_is_refused(hunt: tuple[Path, FakeClaude],
                                                capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["send-notes", "one"]) == 1
    assert "numbers" in capsys.readouterr().err


# What the reviews found untested


def _approved(notes: Notes, threads: Threads, jobs: JobsList, project: Path, *posters: dict) -> list[int]:
    drafted = _drafted(notes, jobs, project, *posters)
    questions(jobs, project / "data", threads, notes)
    decide(notes, "send", None)
    return drafted


def test_the_command_keeps_the_daily_cap_of_the_settings(
        hunt: tuple[Path, FakeClaude], monkeypatch: pytest.MonkeyPatch) -> None:
    project, fake = hunt
    settings = json.loads((project / "data" / "settings.json").read_text())
    (project / "data" / "settings.json").write_text(json.dumps({**settings, "linkedin_notes_daily": 1}))
    jobs = JobsList(project / "data" / "jobs.db")
    for number in (1, 2, 3):
        _applied(jobs, project, number, _poster(number))
    cli.main(["draft-notes"])
    cli.main(["questions"])

    assert cli.main(["send-notes", "all"]) == 0

    assert fake.sent == ["Jane Doe 1"]


def test_a_note_that_may_have_gone_counts_against_the_daily_cap(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _approved(notes, threads, jobs, project, _poster(1), _poster(2), _poster(3))
    sender = Sender(None)

    _send(notes, threads, jobs, project, clock, sender, daily=2)

    assert [note.state for note in notes.all()] == ["unsure", "sent", "approved"]


def test_skip_2_skips_only_that_note_and_approves_the_others(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path) -> None:
    first, second, third = _drafted(notes, jobs, project, _poster(1), _poster(2), _poster(3))
    questions(jobs, project / "data", threads, notes)

    decide(notes, "skip", [second])

    assert [notes.get(note).state for note in (first, second, third)] == ["approved", "skipped", "approved"]


def test_a_note_left_sending_by_a_run_that_died_is_unsure(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path) -> None:
    [note] = _approved(notes, threads, jobs, project, _poster(1))
    notes.move(note, "sending")

    anything_waiting(jobs, project / "data", threads, notes)

    assert notes.get(note).state == "unsure"


def test_a_send_stopped_in_the_middle_is_unsure_and_the_stop_goes_on(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [note] = _approved(notes, threads, jobs, project, _poster(1))

    def stopped(command: list[str]) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _send(notes, threads, jobs, project, clock, stopped)  # type: ignore[arg-type]

    assert notes.get(note).state == "unsure"
    assert not (project / "data" / "linkedin.lock").exists()


def test_a_failed_send_is_tried_once_more_then_failed(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [note] = _approved(notes, threads, jobs, project, _poster(1))
    sender = Sender({"result": "failed", "reason": "the box did not open"},
                    {"result": "failed", "reason": "the box did not open"})

    said = _send(notes, threads, jobs, project, clock, sender)

    assert notes.get(note).state == "failed" and said.failed == 1
    assert sender.reports == [] and sender.sent == []


def test_two_approved_notes_to_one_person_send_one(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    first, second = _approved(notes, threads, jobs, project, _poster(1), _poster(2))
    # Saved before the rule of one note per person: the same person on both.
    notes._db.execute("UPDATE notes SET profile = ?", (notes.get(first).profile,))
    notes._db.commit()
    sender = Sender()

    _send(notes, threads, jobs, project, clock, sender)

    assert len(sender.sent) == 1
    assert notes.get(second).state == "skipped"


def test_approved_notes_over_the_cap_are_named_for_the_chat_to_send_the_next_day(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    _approved(notes, threads, jobs, project, _poster(1), _poster(2))
    _send(notes, threads, jobs, project, clock, Sender(), daily=1)

    assert "not sent yet" not in questions(jobs, project / "data", threads, notes, 1)
    clock.now = NOON + timedelta(days=1)
    assert ("Approved notes to hiring managers not sent yet (1): run hunt send-notes"
            in questions(jobs, project / "data", threads, notes, 1))


def test_a_note_sent_without_its_conversation_makes_no_thread(
        notes: Notes, threads: Threads, jobs: JobsList, project: Path, clock: MovableClock) -> None:
    [note] = _approved(notes, threads, jobs, project, _poster(1))

    _send(notes, threads, jobs, project, clock, Sender({"result": "sent"}))

    assert notes.get(note).state == "sent"
    assert threads.in_states("sent") == []


def test_hunt_apply_drafts_the_notes_at_its_end(
        hunt: tuple[Path, FakeClaude], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = hunt
    _applied(JobsList(project / "data" / "jobs.db"), project, 1, _poster(1))
    monkeypatch.setattr(cli, "apply", lambda *args, **kwargs: "0 resumes written")

    assert cli.main(["apply"]) == 0

    assert "1 notes to hiring managers drafted" in capsys.readouterr().out
