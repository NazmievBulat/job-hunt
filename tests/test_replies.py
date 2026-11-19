"""Recruiter replies: messages by email and on LinkedIn saved once as threads, a reply drafted
once for each, sent only on `hunt send-reply`, and `hunt watch`, which looks for new ones every
20 minutes during the day.

No model, no Gmail and no browser: the skill runs are stand-ins that write the files a run would.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.finder import SKILL as ALERTS_SKILL, find, skill_command
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.questions import anything_waiting, questions
from job_hunt.replies import (
    DRAFT_SKILL, READ_SKILL, SEND_SKILL, EmailMessages, ReplyError, Threads, draft_command, draft_replies,
    edit_reply, read_command, read_linkedin, read_messages, send_command, send_reply, skip_reply,
)
from job_hunt.settings import Settings
from job_hunt.watch import linkedin_due, watch
from movable_clock import MovableClock
from only_named_tools import assert_only_named_tools

# A local time zone of its own, so "the morning hour" does not depend on the machine's.
LOCAL = timezone(timedelta(hours=-5))
MORNING = datetime(2026, 11, 18, 7, 0, tzinfo=LOCAL)
SETTINGS = Settings(titles=["QA Engineer"], areas=["Raleigh-Durham-Chapel Hill"], remote="include",
                    lowest_salary=None, morning_hour=7, summary_hour=18)


def _email(number: int = 1, **change: object) -> dict:
    entry = {"email_id": f"m{number}", "thread_id": f"t{number}", "from": "Jane Recruiter <jane@example.com>",
             "subject": "QA role at Example Labs", "text": f"Hi Ada, are you open to a QA role? ({number})",
             "date": "2026-11-18T06:30:00-05:00", "job_link": None, "company": "Example Labs",
             "asks_to_talk": False}
    return {**entry, **change}


def _linkedin(number: int = 1, **change: object) -> dict:
    entry = {"conversation": f"https://www.linkedin.com/messaging/thread/{number}/", "from": "Sam Recruiter",
             "text": f"Hello Ada, a role for you ({number})", "date": "2026-11-18T06:40:00-05:00",
             "job_link": None, "company": "Example Labs", "asks_to_talk": False}
    return {**entry, **change}


def _found(number: int, **change: object) -> FoundJob:
    job = dict(link=f"https://jobs.example.com/{number}", title=f"QA Engineer {number}", company=f"Example Co {number}",
               place="Durham, NC", remote="on_site", board="indeed", email_id="1",
               email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill")
    return FoundJob(**{**job, **change})


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(MORNING)


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
def threads(project: Path, clock: MovableClock) -> Threads:
    return Threads(project / "data" / "jobs.db", clock=clock)


def _saved(threads: Threads, jobs: JobsList, *entries: dict) -> list[int]:
    found, problems = read_messages({"recruiter_emails": [e for e in entries if "email_id" in e],
                                     "linkedin_messages": [e for e in entries if "conversation" in e]})
    assert problems == []
    return [thread for each in found if (thread := threads.save(each, jobs)) is not None]


def _prompt(command: list[str], skill: str) -> dict:
    prompt = command[command.index("-p") + 1]
    assert prompt.startswith(f"/{skill} ")
    return json.loads(prompt.removeprefix(f"/{skill} "))


class Drafter:
    """Stands in for the draft-reply runs: writes each thread's reply, or fails when told to."""

    def __init__(self, failing: int = 0) -> None:
        self.ran: list[int] = []
        self.failing = failing

    def __call__(self, command: list[str]) -> None:
        arguments = _prompt(command, DRAFT_SKILL)
        self.ran.append(arguments["thread"])
        if self.failing:
            self.failing -= 1
            raise ReplyError("the draft-reply run failed (1): no answer")
        Path(arguments["out"]).write_text(json.dumps({"reply": f"Thanks, I'd like to hear more. ({arguments['thread']})",
                                                      "draft_id": "d1" if arguments["source"] == "email" else None}))


class Sender:
    """Stands in for the send-reply run: notes what it sent, writes its report."""

    def __init__(self, project: Path, report: dict | None = None, lock: Path | None = None) -> None:
        self.project, self.report, self.lock = project, report, lock
        self.sent: list[tuple[int, str]] = []
        self.locked: list[bool] = []

    def __call__(self, command: list[str]) -> None:
        arguments = _prompt(command, SEND_SKILL)
        self.sent.append((arguments["thread"], arguments["reply"]))
        if self.lock is not None:
            self.locked.append(self.lock.exists())
        if self.report is not None:
            Path(arguments["report"]).write_text(json.dumps(self.report))


def no_fetch(link: str) -> tuple[str, str]:
    raise AssertionError(f"no page should be read: {link}")


def no_sleep(seconds: float) -> None:
    raise AssertionError("nothing should wait")


# Threads saved once


def test_an_email_from_a_recruiter_is_saved_once(threads: Threads, jobs: JobsList) -> None:
    assert len(_saved(threads, jobs, _email(1))) == 1
    # Read again by the next run: the same message is not a second thread.
    assert _saved(threads, jobs, _email(1)) == []
    (thread,) = threads.in_states("new")
    assert (thread.source, thread.sender, thread.subject) == ("email", "Jane Recruiter <jane@example.com>",
                                                             "QA role at Example Labs")
    assert thread.email_id == "m1" and thread.conversation == "t1"


def test_a_linkedin_message_is_saved_once(threads: Threads, jobs: JobsList) -> None:
    assert len(_saved(threads, jobs, _linkedin(1))) == 1
    assert _saved(threads, jobs, _linkedin(1)) == []
    (thread,) = threads.in_states("new")
    assert thread.source == "linkedin" and thread.conversation == "https://www.linkedin.com/messaging/thread/1/"


def test_a_newer_message_in_the_same_conversation_replaces_the_open_one(threads: Threads, jobs: JobsList) -> None:
    first, = _saved(threads, jobs, _linkedin(1, text="Hello Ada"))
    later, = _saved(threads, jobs, _linkedin(1, text="Hello again, Ada"))

    assert threads.get(first).state == "replaced"
    assert threads.get(later).state == "new"


def test_a_message_without_what_it_needs_is_left_out_with_the_reason() -> None:
    found, problems = read_messages({"recruiter_emails": [_email(1, text=" "), _email(2)],
                                     "linkedin_messages": [_linkedin(1, conversation="not a link")]})
    assert [each.email_id for each in found] == ["m2"]
    assert len(problems) == 2 and "text" in problems[0] and "conversation" in problems[1]


def test_a_thread_is_linked_to_its_job_by_the_link_in_the_message(threads: Threads, jobs: JobsList) -> None:
    job = jobs.add(_found(1))
    jobs.add(_found(2, company="Example Labs"))
    thread, = _saved(threads, jobs, _email(1, job_link="https://jobs.example.com/1?utm_source=mail"))
    assert threads.get(thread).job == job


def test_a_thread_is_linked_to_the_one_job_of_its_company(threads: Threads, jobs: JobsList) -> None:
    job = jobs.add(_found(1, company="Example Labs"))
    thread, = _saved(threads, jobs, _email(1, company="example labs"))
    assert threads.get(thread).job == job


def test_a_company_with_two_jobs_links_the_thread_to_neither(threads: Threads, jobs: JobsList) -> None:
    jobs.add(_found(1, company="Example Labs"))
    jobs.add(_found(2, company="Example Labs"))
    thread, = _saved(threads, jobs, _email(1))
    assert threads.get(thread).job is None


def test_the_finder_saves_the_recruiter_emails_it_read_and_reads_them_from_the_last_time(
    project: Path, jobs: JobsList, threads: Threads, clock: MovableClock
) -> None:
    out = project / "data" / "found-jobs.json"
    arguments: list[dict] = []

    def run(command: list[str]) -> None:
        arguments.append(_prompt(command, ALERTS_SKILL))
        out.write_text(json.dumps({"emails": 0, "jobs": [], "recruiter_emails": [_email(1)],
                                   "linkedin_notifications": 0}))

    messages = EmailMessages(threads, jobs)
    find(SETTINGS, jobs, project / "jobs", out, run=run, fetch=no_fetch, clock=clock, messages=messages)
    clock.advance(hours=1)
    find(SETTINGS, jobs, project / "jobs", out, run=run, fetch=no_fetch, clock=clock, messages=messages)

    assert len(threads.in_states("new")) == 1
    assert arguments[1]["messages_after"] == int(MORNING.timestamp())


def test_the_finder_without_messages_asks_for_none() -> None:
    command = skill_command(MORNING, [], Path("data/found-jobs.json"))
    assert "messages_after" not in _prompt(command, ALERTS_SKILL)


# Drafting


def test_a_reply_is_drafted_once_for_each_unanswered_thread(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    first, second = _saved(threads, jobs, _email(1), _linkedin(2))
    drafter = Drafter()

    assert draft_replies(threads, project, run=drafter, fetch=no_fetch) == 2
    assert draft_replies(threads, project, run=drafter, fetch=no_fetch) == 0

    assert sorted(drafter.ran) == [first, second]
    assert threads.get(first).state == "drafted" and threads.get(first).draft_id == "d1"
    assert threads.get(first).reply == f"Thanks, I'd like to hear more. ({first})"
    assert (project / "jobs" / "replies" / str(first) / "message.md").exists()


def test_a_draft_that_fails_twice_is_failed_and_told(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    drafter = Drafter(failing=2)

    draft_replies(threads, project, run=drafter, fetch=no_fetch)
    assert threads.get(thread).state == "new"
    draft_replies(threads, project, run=drafter, fetch=no_fetch)

    assert threads.get(thread).state == "failed"
    assert "draft-reply" in questions(jobs, project / "data", threads)


def test_the_drafter_may_only_write_its_reply_and_reads_its_folder_the_career_and_the_job(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    job = jobs.add(_found(1, company="Example Labs"))
    email, = _saved(threads, jobs, _email(1))
    linkedin, = _saved(threads, jobs, _linkedin(2, company="Nobody Inc"))
    folder = project / "jobs" / "replies" / str(email)

    tools = draft_command(threads.get(email), project)
    allowed = tools[tools.index("--allowedTools") + 1].split(",")
    assert_only_named_tools(tools)
    assert f"Edit(/{folder}/reply.json)" in allowed
    assert f"Read(/{project}/jobs/{job}/**)" in allowed
    assert "mcp__claude_ai_Gmail__create_draft" in allowed
    assert not any("send" in tool or "reply" == tool.rsplit("__", 1)[-1] for tool in allowed)

    tools = draft_command(threads.get(linkedin), project)
    allowed = tools[tools.index("--allowedTools") + 1].split(",")
    assert_only_named_tools(tools)
    assert not any(tool.startswith("mcp__") for tool in allowed)


def test_a_posting_the_message_links_to_is_saved_as_the_jobs_description(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    thread, = _saved(threads, jobs, _email(1, job_link="https://jobs.example.com/77", company="Nobody Inc"))
    page = "<html><body><main><h1>QA Lead</h1><p>Test the things.</p></main></body></html>"

    draft_replies(threads, project, run=Drafter(), fetch=lambda link: (link, page))

    posting = project / "jobs" / "replies" / str(thread) / "posting.md"
    assert "Test the things." in posting.read_text()
    assert _prompt(draft_command(threads.get(thread), project), DRAFT_SKILL)["posting"] == str(posting)


# Asking and sending


def test_a_drafted_reply_is_a_question_with_the_message_and_the_reply(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    # One that asks to talk is drafted with the calendar's times: see test_times.
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)

    asked = questions(jobs, project / "data", threads)

    assert "Recruiter messages (1):" in asked
    assert "are you open to a QA role?" in asked and "Thanks, I'd like to hear more." in asked
    assert f"hunt send-reply {thread}" in asked
    assert anything_waiting(jobs, project / "data", threads)


def test_nothing_is_sent_before_send_reply(project: Path, threads: Threads, jobs: JobsList) -> None:
    sender = Sender(project, {"result": "sent"})
    _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    questions(jobs, project / "data", threads)

    assert sender.sent == []


def test_send_reply_sends_the_drafted_text_once(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    sender = Sender(project, {"result": "sent"})

    send_reply(threads, project, thread, run=sender, lock=LinkedInLock(project / "data"), sleep=no_sleep)

    assert sender.sent == [(thread, f"Thanks, I'd like to hear more. ({thread})")]
    assert threads.get(thread).state == "sent"
    with pytest.raises(ReplyError, match="sent"):
        send_reply(threads, project, thread, run=sender, lock=LinkedInLock(project / "data"), sleep=no_sleep)
    assert len(sender.sent) == 1


def test_an_edit_changes_the_reply_that_is_sent(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    sender = Sender(project, {"result": "sent"})

    edit_reply(threads, project, thread, "  Thanks! Tuesday works.  ")
    send_reply(threads, project, thread, run=sender, lock=LinkedInLock(project / "data"), sleep=no_sleep)

    assert sender.sent == [(thread, "Thanks! Tuesday works.")]


def test_skip_closes_the_thread_without_sending(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)

    skip_reply(threads, thread)

    assert threads.get(thread).state == "skipped"
    assert "Recruiter messages" not in questions(jobs, project / "data", threads)


def test_a_send_with_no_report_is_unsure_and_never_sent_again_on_its_own(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)

    send_reply(threads, project, thread, run=Sender(project, None), lock=LinkedInLock(project / "data"),
               sleep=no_sleep)

    assert threads.get(thread).state == "unsure"
    assert "whether it was sent" in questions(jobs, project / "data", threads)


def test_a_send_that_failed_waits_again_with_the_reason(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)

    send_reply(threads, project, thread, run=Sender(project, {"result": "failed", "reason": "Gmail refused it"}),
               lock=LinkedInLock(project / "data"), sleep=no_sleep)

    assert threads.get(thread).state == "drafted"
    assert "Gmail refused it" in questions(jobs, project / "data", threads)


def test_a_linkedin_reply_is_sent_under_the_lock_and_an_email_is_not(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    email, linkedin = _saved(threads, jobs, _email(1), _linkedin(2))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    lock = LinkedInLock(project / "data", pause=lambda: timedelta(0))
    sender = Sender(project, {"result": "sent"}, lock=lock.path)

    send_reply(threads, project, linkedin, run=sender, lock=lock, sleep=no_sleep)
    send_reply(threads, project, email, run=sender, lock=lock, sleep=no_sleep)

    assert sender.locked == [True, False]
    assert not lock.path.exists()


def test_a_linkedin_reply_waits_for_the_application_holding_the_lock(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    _, linkedin = _saved(threads, jobs, _email(1), _linkedin(2))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    lock = LinkedInLock(project / "data", pause=lambda: timedelta(0))
    applying = LinkedInLock(project / "data")
    assert applying.take(12) is None
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        applying.release()
        (project / "data" / "linkedin.next").unlink()

    send_reply(threads, project, linkedin, run=Sender(project, {"result": "sent"}), lock=lock, sleep=sleep)

    assert len(waits) == 1 and threads.get(linkedin).state == "sent"


def test_the_sender_gets_only_the_tools_of_its_channel(project: Path, threads: Threads, jobs: JobsList) -> None:
    email, linkedin = _saved(threads, jobs, _email(1), _linkedin(2))
    email_tools = send_command(threads.get(email), "Hi", project)
    linkedin_tools = send_command(threads.get(linkedin), "Hi", project)

    assert "--chrome" not in email_tools and "--chrome" in linkedin_tools
    assert_only_named_tools(email_tools)
    assert_only_named_tools(linkedin_tools)
    assert "mcp__claude_ai_Gmail__reply" in email_tools[email_tools.index("--allowedTools") + 1]
    assert "Gmail" not in linkedin_tools[linkedin_tools.index("--allowedTools") + 1]


def test_a_linkedin_reply_without_a_resume_cannot_upload_a_file(project: Path, threads: Threads, jobs: JobsList) -> None:
    # It types into a stranger's conversation with nobody watching: only a reply that goes out
    # with the resume may upload, and test_times pins that one.
    (linkedin,) = _saved(threads, jobs, _linkedin(2))
    command = send_command(threads.get(linkedin), "Hi", project)
    tools = command[command.index("--allowedTools") + 1].split(",")

    assert "mcp__claude-in-chrome__computer" in tools and "mcp__claude-in-chrome__form_input" in tools
    assert "mcp__claude-in-chrome__file_upload" not in tools


# Reading LinkedIn


def test_the_linkedin_reader_has_no_tool_that_types_or_clicks(tmp_path: Path) -> None:
    # It reads strangers' messages with nobody watching: a message must not be able to make it send.
    command = read_command(datetime(2026, 11, 18, tzinfo=UTC), tmp_path / "out.json")
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert "mcp__claude-in-chrome__navigate" in tools
    assert_only_named_tools(command)
    assert "mcp__claude-in-chrome__get_page_text" in tools
    assert not [tool for tool in tools if tool.endswith(("computer", "form_input", "file_upload"))]


def test_linkedin_is_read_under_the_lock_and_its_messages_saved_once(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    lock = LinkedInLock(project / "data", pause=lambda: timedelta(0))
    held: list[bool] = []

    def run(command: list[str]) -> None:
        arguments = _prompt(command, READ_SKILL)
        held.append(lock.path.exists())
        Path(arguments["out"]).write_text(json.dumps({"linkedin_messages": [_linkedin(1)]}))

    assert read_linkedin(threads, jobs, project, run=run, lock=lock, sleep=no_sleep, clock=clock) == 1
    assert read_linkedin(threads, jobs, project, run=run, lock=lock, sleep=no_sleep, clock=clock) == 0

    assert held == [True, True] and not lock.path.exists()
    assert threads.last_read("linkedin") == MORNING


# hunt watch


class Watch:
    """The stand-ins of one watch run: the Haiku check, LinkedIn's reading, the drafter and the chat."""

    def __init__(self, project: Path, threads: Threads, jobs: JobsList, *, emails: list[dict] | None = None,
                 notifications: int = 0, linkedin: list[dict] | None = None) -> None:
        self.project, self.threads, self.jobs = project, threads, jobs
        self.emails, self.notifications, self.linkedin = emails or [], notifications, linkedin or []
        self.checks: list[dict] = []
        self.linkedin_reads = 0
        self.drafter = Drafter()
        self.told: list[list[int]] = []

    def check(self, command: list[str]) -> None:
        arguments = _prompt(command, ALERTS_SKILL)
        self.checks.append(arguments)
        Path(arguments["out"]).write_text(json.dumps({"emails": 0, "jobs": [], "recruiter_emails": self.emails,
                                                      "linkedin_notifications": self.notifications}))
        self.emails = []

    def read_linkedin(self) -> int | None:
        self.linkedin_reads += 1
        found, _ = read_messages({"linkedin_messages": self.linkedin})
        self.linkedin = []
        return sum(self.threads.save(each, self.jobs) is not None for each in found)

    def draft(self) -> int:
        return draft_replies(self.threads, self.project, run=self.drafter, fetch=no_fetch)

    def tell(self, told: list) -> None:
        self.told.append([thread.id for thread in told])

    def __call__(self, clock: MovableClock) -> str:
        return watch(self.threads, self.jobs, self.project / "data", SETTINGS, check=self.check,
                     read_linkedin=self.read_linkedin, draft=self.draft, tell=self.tell, clock=clock)


def _read_linkedin_today(threads: Threads, clock: MovableClock) -> None:
    threads.finished_read("linkedin", clock())


def test_a_watch_with_nothing_new_ends_after_the_haiku_check_and_says_nothing(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    clock.advance(hours=1)
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs)

    assert run(clock) == ""
    assert len(run.checks) == 1 and run.linkedin_reads == 0 and run.drafter.ran == [] and run.told == []
    # The check reads messages only, not the job alerts.
    assert run.checks[0]["after"] is None and "messages_after" in run.checks[0]


def test_a_linkedin_notification_email_opens_linkedin(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    clock.advance(hours=1)
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs, notifications=1, linkedin=[_linkedin(1)])

    run(clock)

    assert run.linkedin_reads == 1


def test_linkedin_is_read_in_the_morning_and_at_mid_day_without_a_notification(
    threads: Threads, clock: MovableClock
) -> None:
    yesterday = MORNING - timedelta(hours=10)
    assert linkedin_due(None, MORNING, SETTINGS)
    assert linkedin_due(yesterday, MORNING, SETTINGS)
    assert not linkedin_due(MORNING, MORNING + timedelta(hours=2), SETTINGS)
    # Mid-day is half way from the morning hour (7) to the summary hour (18): 12:00.
    assert not linkedin_due(MORNING, MORNING.replace(hour=11, minute=40), SETTINGS)
    assert linkedin_due(MORNING, MORNING.replace(hour=12), SETTINGS)
    assert not linkedin_due(MORNING.replace(hour=12), MORNING.replace(hour=15), SETTINGS)


def test_outside_the_hours_nothing_runs(project: Path, threads: Threads, jobs: JobsList) -> None:
    for when in (MORNING.replace(hour=6, minute=40), MORNING.replace(hour=18), MORNING.replace(hour=23)):
        run = Watch(project, threads, jobs, emails=[_email(1)], notifications=1)
        assert run(MovableClock(when)) == ""
        assert run.checks == [] and run.linkedin_reads == 0


def test_a_new_thread_is_drafted_and_asked_at_once_and_only_once(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs, emails=[_email(1)])

    said = run(clock)
    clock.advance(seconds=20 * 60)
    run(clock)

    thread, = threads.in_states("drafted")
    assert run.drafter.ran == [thread.id]
    assert run.told == [[thread.id]]
    assert "1 new" in said


def test_the_watch_reads_emails_from_where_the_last_read_ended(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs)
    run(clock)
    clock.advance(seconds=20 * 60)
    run(clock)

    assert run.checks[1]["messages_after"] == int(MORNING.timestamp())


def test_a_check_that_fails_keeps_the_time_it_reads_from(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    def failing(command: list[str]) -> None:
        raise ReplyError("the read-job-alerts run failed (1): no Gmail")

    with pytest.raises(ReplyError):
        watch(threads, jobs, project / "data", SETTINGS, check=failing, read_linkedin=lambda: 0, draft=lambda: 0,
              tell=lambda told: None, clock=clock)
    assert threads.last_read("email") is None


# The commands


def test_hunt_names_the_reply_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    said = capsys.readouterr().out
    for command in ("watch", "linkedin-messages", "draft-replies", "send-reply", "edit-reply", "skip-reply"):
        assert command in said


def test_send_reply_of_a_thread_that_is_not_drafted_says_so(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    thread, = _saved(threads, jobs, _email(1))
    with pytest.raises(ReplyError, match="no reply drafted"):
        send_reply(threads, project, thread, run=Sender(project), lock=LinkedInLock(project / "data"),
                   sleep=no_sleep)
    with pytest.raises(ReplyError, match="no thread 99"):
        skip_reply(threads, 99)


def test_a_watch_that_keeps_failing_is_told_once_until_it_works_again(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = project / "data"
    (data / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 0,
        "summary_hour": 23}))
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: (_ for _ in ()).throw(ReplyError("no Gmail")))

    assert cli.main(["watch"]) == 1
    assert cli.main(["watch"]) == 1
    jobs = JobsList(data / "jobs.db")
    assert questions(jobs, data).count("no Gmail") == 1

    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: "")
    assert cli.main(["watch"]) == 0
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: (_ for _ in ()).throw(ReplyError("no Gmail")))
    assert cli.main(["watch"]) == 1
    assert "no Gmail" in questions(jobs, data)


def test_a_linkedin_read_that_could_not_reach_linkedin_fails_and_keeps_the_time_it_reads_from(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    lock = LinkedInLock(project / "data", pause=lambda: timedelta(0))

    def run(command: list[str]) -> None:
        out = Path(_prompt(command, READ_SKILL)["out"])
        out.write_text(json.dumps({"linkedin_messages": [], "unreachable": "Chrome is closed"}))

    with pytest.raises(ReplyError, match="Chrome is closed"):
        read_linkedin(threads, jobs, project, run=run, lock=lock, sleep=no_sleep, clock=clock)
    assert threads.last_read("linkedin") is None and not lock.path.exists()


# Found by the coverage review


def test_a_send_stopped_by_anything_leaves_the_thread_to_look_at_not_lost(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)

    def stopped(command: list[str]) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        send_reply(threads, project, thread, run=stopped, lock=LinkedInLock(project / "data"), sleep=no_sleep)

    assert threads.get(thread).state == "unsure"
    assert f"hunt send-reply {thread}" in questions(jobs, project / "data", threads)


def test_a_notification_whose_linkedin_read_failed_opens_linkedin_again_at_the_next_run(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    clock.advance(hours=1)
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs, notifications=1)
    run.read_linkedin = lambda: None  # type: ignore[method-assign]  # LinkedIn could not be read
    run(clock)

    clock.advance(seconds=20 * 60)
    again = Watch(project, threads, jobs)
    again(clock)

    assert again.linkedin_reads == 1


def test_a_draft_that_failed_once_is_drafted_and_asked_at_the_next_run(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs, emails=[_email(1)])
    run.drafter.failing = 1
    run(clock)
    assert run.told == []

    clock.advance(seconds=20 * 60)
    run(clock)

    thread, = threads.in_states("drafted")
    assert run.told == [[thread.id]]


class FakeClaude:
    """Stands in for every `claude -p` hunt starts, by its skill, writing what the run would."""

    def __init__(self, found: dict | None = None, sent: dict | None = None) -> None:
        self.found = found or {"emails": 0, "jobs": [], "recruiter_emails": [], "linkedin_notifications": 0}
        self.sent = sent
        self.skills: list[str] = []

    def __call__(self, command: list[str], folder: Path, *, skill: str = ALERTS_SKILL,
                 error: type[RuntimeError] = RuntimeError) -> None:
        self.skills.append(skill)
        arguments = _prompt(command, skill)
        if skill == ALERTS_SKILL:
            Path(arguments["out"]).write_text(json.dumps(self.found))
        elif skill == DRAFT_SKILL:
            Path(arguments["out"]).write_text(json.dumps({"reply": "Thanks, Jane. Ada", "draft_id": "d1"}))
        elif skill == SEND_SKILL and self.sent is not None:
            Path(arguments["report"]).write_text(json.dumps(self.sent))


@pytest.fixture
def hunt(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (project / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 0,
        "summary_hour": 23}))
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    monkeypatch.setattr(cli, "fetch_page", no_fetch)
    monkeypatch.setattr(cli, "tell_session", lambda root: None)
    return project


def test_hunt_find_saves_a_recruiters_email_as_a_thread(
    hunt: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_claude", FakeClaude({"emails": 0, "jobs": [], "recruiter_emails": [_email(1)]}))

    assert cli.main(["find"]) == 0

    assert len(Threads(hunt / "data" / "jobs.db").in_states("new")) == 1


@pytest.mark.parametrize(("sent", "code", "state"), [
    ({"result": "sent"}, 0, "sent"), ({"result": "failed", "reason": "Gmail refused it"}, 1, "drafted"),
    (None, 1, "unsure")])
def test_hunt_watch_drafts_and_asks_and_send_reply_says_how_it_went(
    hunt: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    sent: dict | None, code: int, state: str
) -> None:
    threads = Threads(hunt / "data" / "jobs.db")
    # The watch's own clock, not the machine's: from the summary hour (23 here) it does nothing.
    monkeypatch.setattr(cli, "watch", partial(watch, clock=lambda: MORNING))
    threads.finished_read("linkedin", MORNING)
    monkeypatch.setattr(cli, "run_claude", FakeClaude({"emails": 0, "jobs": [], "recruiter_emails": [_email(1)],
                                                       "linkedin_notifications": 0}, sent))

    assert cli.main(["watch"]) == 0
    assert "1 new" in capsys.readouterr().out
    assert cli.main(["questions"]) == 0
    thread, = threads.in_states("drafted")
    assert f"hunt send-reply {thread.id}" in capsys.readouterr().out

    assert cli.main(["send-reply", str(thread.id)]) == code
    assert threads.get(thread.id).state == state


def test_hunt_edit_and_skip_reply_change_and_close_the_thread(
    hunt: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    threads = Threads(hunt / "data" / "jobs.db")
    thread, = _saved(threads, JobsList(hunt / "data" / "jobs.db"), _email(1))
    monkeypatch.setattr(cli, "run_claude", FakeClaude())
    assert cli.main(["draft-replies"]) == 0

    assert cli.main(["edit-reply", str(thread), "Tuesday works."]) == 0
    assert threads.get(thread).reply == "Tuesday works."
    assert cli.main(["skip-reply", str(thread)]) == 0
    assert threads.get(thread).state == "skipped"
    assert cli.main(["send-reply", str(thread)]) == 1
    assert "skipped" in capsys.readouterr().err


# Found by Codex's review


def test_a_send_whose_process_died_shows_as_unsure_once_no_send_runs(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    # A run killed while sending leaves only its state behind.
    threads.move(thread, "sending")

    assert f"hunt send-reply {thread}" in questions(jobs, project / "data", threads)
    assert threads.get(thread).state == "unsure"


def test_a_send_still_running_is_not_taken_for_a_dead_one(project: Path, threads: Threads, jobs: JobsList) -> None:
    import fcntl

    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    threads.move(thread, "sending")
    with (project / "data" / "send.lock").open("w") as running:
        fcntl.flock(running, fcntl.LOCK_EX)
        questions(jobs, project / "data", threads)
        assert threads.get(thread).state == "sending"


def test_the_newest_message_of_a_conversation_stays_open_whatever_order_they_were_read_in(
    threads: Threads, jobs: JobsList
) -> None:
    found, _ = read_messages({"linkedin_messages": [
        _linkedin(1, text="Second", date="2026-11-18T09:00:00-05:00"),
        _linkedin(1, text="First", date="2026-11-18T08:00:00-05:00")]})
    EmailMessages(threads, jobs).save_all(found)

    (open_one,) = threads.in_states("new")
    assert open_one.text == "Second"


def test_read_times_written_in_different_time_zones_give_the_latest_instant(threads: Threads) -> None:
    threads.finished_read("email", datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
    # 07:20 at UTC-5 is 12:20 UTC: later, though "07" sorts before "12".
    threads.finished_read("email", datetime(2026, 11, 18, 7, 20, tzinfo=LOCAL))

    assert threads.last_read("email") == datetime(2026, 11, 18, 12, 20, tzinfo=UTC)


def test_two_drafting_runs_never_draft_one_thread_twice(project: Path, threads: Threads, jobs: JobsList) -> None:
    import threading

    _saved(threads, jobs, _email(1))
    ran: list[int] = []
    inside = threading.Event()

    def slow(command: list[str]) -> None:
        ran.append(_prompt(command, DRAFT_SKILL)["thread"])
        inside.set()
        import time
        time.sleep(0.2)
        Drafter()(command)

    other = threading.Thread(target=lambda: draft_replies(
        Threads(project / "data" / "jobs.db"), project, run=slow, fetch=no_fetch))
    other.start()
    inside.wait()
    draft_replies(threads, project, run=slow, fetch=no_fetch)
    other.join()

    assert len(ran) == 1


# Found by Codex's second review


def test_a_thread_replaced_while_its_reply_was_drafted_stays_replaced(
    project: Path, threads: Threads, jobs: JobsList
) -> None:
    first, = _saved(threads, jobs, _linkedin(1, text="Hello Ada"))

    def drafter(command: list[str]) -> None:
        # A newer message in the conversation arrives while the model writes.
        _saved(threads, jobs, _linkedin(1, text="Hello again, Ada"))
        Drafter()(command)

    draft_replies(threads, project, run=drafter, fetch=no_fetch)

    assert threads.get(first).state == "replaced"


def test_a_dead_send_alone_is_something_waiting(project: Path, threads: Threads, jobs: JobsList) -> None:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Drafter(), fetch=no_fetch)
    threads.move(thread, "sending")

    assert anything_waiting(jobs, project / "data", threads)


def test_a_linkedin_notification_the_finder_read_is_owed_to_the_next_watch(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    out = project / "data" / "found-jobs.json"

    def run(command: list[str]) -> None:
        out.write_text(json.dumps({"emails": 0, "jobs": [], "recruiter_emails": [], "linkedin_notifications": 1}))

    find(SETTINGS, jobs, project / "jobs", out, run=run, fetch=no_fetch, clock=clock,
         messages=EmailMessages(threads, jobs))
    clock.advance(hours=1)
    _read_linkedin_today(threads, clock)
    clock.advance(seconds=20 * 60)
    watched = Watch(project, threads, jobs)
    watched(clock)

    assert watched.linkedin_reads == 1


def test_a_message_about_a_job_found_in_the_same_run_is_linked_to_it(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    out = project / "data" / "found-jobs.json"
    job = {"link": "https://jobs.example.com/9", "title": "QA Engineer", "company": "Example Labs", "place": "",
           "remote": "remote", "board": "indeed", "email_id": "a1", "email_date": "2026-11-18T06:00:00+00:00",
           "area": None}

    def run(command: list[str]) -> None:
        out.write_text(json.dumps({"emails": 1, "jobs": [job], "recruiter_emails": [
            _email(1, job_link="https://jobs.example.com/9")]}))

    page = "<html><body><main><p>Test the things.</p></main></body></html>"
    find(SETTINGS, jobs, project / "jobs", out, run=run, fetch=lambda link: (link, page), clock=clock,
         messages=EmailMessages(threads, jobs))

    (thread,) = threads.in_states("new")
    assert thread.job is not None and jobs.get(thread.job).link == "https://jobs.example.com/9"


def test_two_linkedin_reads_at_once_each_read_their_own_file(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    import threading
    import time

    errors: list[Exception] = []
    second: list[threading.Thread] = []

    def read() -> None:
        try:
            read_linkedin(Threads(project / "data" / "jobs.db", clock=clock), JobsList(project / "data" / "jobs.db"),
                          project, run=run, lock=LinkedInLock(project / "data", pause=lambda: timedelta(0),
                                                              poll_seconds=0.01), sleep=time.sleep, clock=clock)
        except Exception as error:  # noqa: BLE001 - the test reports it
            errors.append(error)

    def run(command: list[str]) -> None:
        Path(_prompt(command, READ_SKILL)["out"]).write_text(json.dumps({"linkedin_messages": [_linkedin(1)]}))
        if not second:
            # The second read starts while the first has written its file and not yet read it.
            second.append(threading.Thread(target=read))
            second[0].start()
            time.sleep(0.1)

    read()
    second[0].join()

    assert errors == []


# Found by the independent review


def test_an_older_message_read_late_does_not_replace_the_newer_one_left_open(
    threads: Threads, jobs: JobsList
) -> None:
    newer, = _saved(threads, jobs, _email(2, thread_id="t1", date="2026-11-18T09:00:00-05:00"))
    # Gmail showed the 08:00 email only to the next read.
    older, = _saved(threads, jobs, _email(1, date="2026-11-18T08:00:00-05:00"))

    assert threads.get(newer).state == "new"
    assert threads.get(older).state == "replaced"


@pytest.mark.parametrize("written", [{}, {"emails": 0, "jobs": []}, {"recruiter_emails": "none"}])
def test_a_check_whose_file_has_no_list_of_messages_keeps_the_time_it_reads_from(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, written: dict
) -> None:
    problems = EmailMessages(threads, jobs).saved(written, clock())

    assert threads.last_read("email") is None
    assert any("recruiter_emails" in problem for problem in problems)


@pytest.mark.parametrize("written", [{}, {"linkedin_messages": "none"}, []])
def test_a_linkedin_read_whose_file_has_no_list_of_messages_fails_and_keeps_its_time(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, written: object
) -> None:
    lock = LinkedInLock(project / "data", pause=lambda: timedelta(0))

    def run(command: list[str]) -> None:
        Path(_prompt(command, READ_SKILL)["out"]).write_text(json.dumps(written))

    with pytest.raises(ReplyError, match="linkedin_messages"):
        read_linkedin(threads, jobs, project, run=run, lock=lock, sleep=no_sleep, clock=clock)
    assert threads.last_read("linkedin") is None


def test_a_notification_noted_while_linkedin_is_read_is_still_owed_after_it(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    clock.advance(hours=1)
    _read_linkedin_today(threads, clock)
    run = Watch(project, threads, jobs, notifications=1)
    read = run.read_linkedin

    def read_while_the_finder_notes_another() -> int | None:
        EmailMessages(threads, jobs).saved({"recruiter_emails": [], "linkedin_notifications": 1},
                                           clock() + timedelta(minutes=1))
        return read()

    run.read_linkedin = read_while_the_finder_notes_another  # type: ignore[method-assign]
    run(clock)

    clock.advance(seconds=20 * 60)
    again = Watch(project, threads, jobs)
    again(clock)
    assert again.linkedin_reads == 1


class _ReadsThenWaits:
    """A connection whose first read in `save` lets another run save before this one goes on."""

    def __init__(self, db: object, meanwhile: object) -> None:
        self.db, self.meanwhile = db, meanwhile

    def __enter__(self) -> _ReadsThenWaits:
        self.db.__enter__()  # type: ignore[attr-defined]
        return self

    def __exit__(self, *args: object) -> object:
        return self.db.__exit__(*args)  # type: ignore[attr-defined]

    def execute(self, sql: str, *args: object) -> object:
        result = self.db.execute(sql, *args)  # type: ignore[attr-defined]
        if sql.startswith("SELECT sent_at FROM threads") and self.meanwhile is not None:
            other, self.meanwhile = threading.Thread(target=self.meanwhile), None  # type: ignore[arg-type]
            other.start()
            other.join(timeout=0.3)
        return result


def test_two_runs_saving_at_once_keep_the_newest_message_open(project: Path, jobs: JobsList) -> None:
    # The morning's find and the 07:00 watch read the mail at the same time.
    path = project / "data" / "jobs.db"
    older, = read_messages({"recruiter_emails": [_email(1, date="2026-11-18T08:00:00-05:00")]})[0]
    newer, = read_messages({"recruiter_emails": [_email(2, thread_id="t1", date="2026-11-18T09:00:00-05:00")]})[0]
    errors: list[BaseException] = []

    def the_watch_saves_the_newer() -> None:
        try:
            Threads(path).save(newer, JobsList(path))
        except BaseException as error:  # noqa: BLE001 - told by the assertion below
            errors.append(error)

    finder = Threads(path)
    finder._db = _ReadsThenWaits(finder._db, the_watch_saves_the_newer)  # type: ignore[assignment]
    finder.save(older, jobs)
    for each in threading.enumerate():
        if each is not threading.current_thread():
            each.join(timeout=10)

    assert errors == []
    states = {thread.email_id: thread.state for thread in Threads(path).in_states("new", "replaced")}
    assert states == {"m1": "replaced", "m2": "new"}
