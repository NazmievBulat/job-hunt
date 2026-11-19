"""Offering times for a call: free half hours of the next five working days from a stand-in for
Google Calendar, the reply offering them, the event held only after `hunt send-reply`, and moved
and confirmed when the recruiter's answer picks one of the times.

No model, no calendar and no Gmail: the skill runs are stand-ins that write the files a run would.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from job_hunt import cli
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.questions import questions
from job_hunt.replies import (
    DRAFT_SKILL, SEND_SKILL, ReplyError, Threads, draft_replies, offer_times, read_messages, send_reply,
)
from job_hunt.settings import SettingsError, read_settings
from job_hunt.times import SKILL, Talks, TimesError, free_times, read_busy, working_days
from movable_clock import MovableClock

NEW_YORK = ZoneInfo("America/New_York")
# A Wednesday morning: the working days are Thursday, Friday, Monday, Tuesday and Wednesday.
MORNING = datetime(2026, 11, 18, 7, 0, tzinfo=NEW_YORK)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=NEW_YORK)


def _email(number: int = 1, **change: object) -> dict:
    entry = {"email_id": f"m{number}", "thread_id": "t1", "from": "Jane Recruiter <jane@example.com>",
             "subject": "QA role at Example Labs", "text": f"Hi Ada, could we talk this week? ({number})",
             "date": f"2026-11-18T06:{30 + number}:00-05:00", "job_link": "https://jobs.example.com/1",
             "company": "Example Labs", "asks_to_talk": True}
    return {**entry, **change}


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(MORNING)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "jobs" / "1").mkdir(parents=True)
    (tmp_path / "jobs" / "1" / "posting.md").write_text("# QA Engineer\n")
    (tmp_path / "data" / "career.json").write_text(json.dumps({"person": {"name": "Ada Example"}}))
    (tmp_path / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7,
        "time_zone": "America/New_York"}))
    return tmp_path


@pytest.fixture
def jobs(project: Path, clock: MovableClock) -> JobsList:
    jobs = JobsList(project / "data" / "jobs.db", clock=clock)
    jobs.add(FoundJob(link="https://jobs.example.com/1", title="QA Engineer", company="Example Labs",
                      place="Durham, NC", remote="on_site", board="indeed", email_id="1",
                      email_date="2026-11-17T07:00:00+00:00", area=None))
    return jobs


@pytest.fixture
def threads(project: Path, clock: MovableClock) -> Threads:
    return Threads(project / "data" / "jobs.db", clock=clock)


def _saved(threads: Threads, jobs: JobsList, *entries: dict) -> list[int]:
    found, problems = read_messages({"recruiter_emails": list(entries)})
    assert problems == []
    return [thread for each in found if (thread := threads.save(each, jobs)) is not None]


def _arguments(command: list[str]) -> tuple[str, dict]:
    prompt = command[command.index("-p") + 1]
    skill, _, rest = prompt.partition(" ")
    return skill.removeprefix("/"), json.loads(rest)


class Calendar:
    """Stands in for the runs and for Google Calendar: keeps the events, offers what is asked."""

    def __init__(self, busy: list[dict] | None = None, offer: list[str] | None = None,
                 chosen: str | None = None, hold: dict | None = None) -> None:
        self.busy = busy or []
        self.offer = offer
        self.chosen = chosen
        self.hold = hold
        self.reply = "Glad to talk. Thursday 10:00 or Monday 14:30? Ada"
        self.events: dict[str, dict] = {}
        self.ran: list[tuple[str, dict]] = []
        self.sent: list[dict] = []

    def __call__(self, command: list[str]) -> None:
        skill, arguments = _arguments(command)
        self.ran.append((skill, arguments))
        out = Path(arguments.get("out") or arguments.get("report"))
        if skill == SKILL and arguments["task"] == "offer":
            out.write_text(json.dumps({"busy": self.busy, "times": self.offer, "draft_id": "d2", "reply": self.reply}))
        elif skill == SKILL and arguments["task"] == "hold":
            if self.hold is not None:
                out.write_text(json.dumps(self.hold))
                return
            # With an event_id it moves that held event; without, it adds one.
            event = arguments.get("event_id") or f"e{len(self.events) + 1}"
            self.events[event] = {"title": arguments["title"], "start": arguments["start"]}
            out.write_text(json.dumps({"result": "done", "event_id": event}))
        elif skill == SKILL and arguments["task"] == "confirm":
            self.events[arguments["event_id"]] = {"title": arguments["title"], "start": arguments["start"]}
            out.write_text(json.dumps({"result": "done", "event_id": arguments["event_id"]}))
        elif skill == DRAFT_SKILL:
            out.write_text(json.dumps({"reply": "Thursday at 10 it is. Ada", "draft_id": None, "time": self.chosen}))
        elif skill == SEND_SKILL:
            self.sent.append(arguments)
            out.write_text(json.dumps({"result": "sent"}))

    def tasks(self) -> list[str]:
        return [arguments.get("task", skill) for skill, arguments in self.ran]


def no_fetch(link: str) -> tuple[str, str]:
    raise AssertionError(f"no page should be read: {link}")


def _send(threads: Threads, project: Path, thread: int, calendar: Calendar) -> None:
    sent = send_reply(threads, project, thread, run=calendar, lock=LinkedInLock(project / "data"), sleep=lambda _: None)
    assert sent.state == "sent"


# The free times


def test_the_working_days_are_the_next_five_weekdays_inside_the_working_hours() -> None:
    days = working_days(MORNING, NEW_YORK, (9, 17))

    assert [start.date().day for start, _ in days] == [19, 20, 23, 24, 25]
    assert days[0] == (_at(19, 9), _at(19, 17))


def test_free_times_are_half_hours_inside_working_hours_and_the_five_days() -> None:
    free = free_times([], working_days(MORNING, NEW_YORK, (9, 17)))

    assert len(free) == 5 * 16
    assert free[0] == _at(19, 9) and free[1] == _at(19, 9, 30) and free[-1] == _at(25, 16, 30)
    assert all(9 <= when.hour < 17 and when.weekday() < 5 for when in free)
    # Nothing today, nothing on the weekend, nothing after the fifth working day.
    assert {when.day for when in free} == {19, 20, 23, 24, 25}


def test_a_busy_day_is_skipped_and_a_busy_hour_is_left_out() -> None:
    busy = read_busy([{"start": "2026-11-20", "end": "2026-11-21"},
                      {"start": "2026-11-19T10:15:00-05:00", "end": "2026-11-19T11:00:00-05:00"}], NEW_YORK)

    free = free_times(busy, working_days(MORNING, NEW_YORK, (9, 17)))

    assert not [when for when in free if when.day == 20]
    assert _at(19, 10) not in free and _at(19, 10, 30) not in free
    assert _at(19, 9, 30) in free and _at(19, 11) in free


def test_a_busy_time_with_no_offset_is_read_in_the_persons_zone() -> None:
    busy = read_busy([{"start": "2026-11-19T09:00:00", "end": "2026-11-19T17:00:00"}], NEW_YORK)

    assert not [when for when in free_times(busy, working_days(MORNING, NEW_YORK, (9, 17))) if when.day == 19]


def test_working_hours_and_time_zone_come_from_the_settings(project: Path) -> None:
    path = project / "data" / "settings.json"
    settings = read_settings(path)
    assert settings.working_hours == (9, 17) and settings.time_zone == "America/New_York"

    path.write_text(json.dumps({**json.loads(path.read_text()), "working_hours": {"start": 10, "end": 16}}))
    assert read_settings(path).working_hours == (10, 16)

    for wrong in ({"working_hours": {"start": 17, "end": 9}}, {"working_hours": [9, 17]},
                  {"time_zone": "Mars/Olympus"}, {"time_zone": ""}):
        path.write_text(json.dumps({**json.loads(path.read_text()), "working_hours": {"start": 9, "end": 17},
                                    **wrong}))
        with pytest.raises(SettingsError):
            read_settings(path)


def test_with_no_time_zone_set_the_zone_of_tz_is_the_persons(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt.times import zone

    path = project / "data" / "settings.json"
    path.write_text(json.dumps({key: value for key, value in json.loads(path.read_text()).items() if key != "time_zone"}))
    monkeypatch.setenv("TZ", "America/Los_Angeles")

    assert zone(read_settings(path)) == ZoneInfo("America/Los_Angeles")


# The offer


def test_a_request_to_talk_gets_a_reply_offering_free_times(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, = _saved(threads, jobs, _email())
    calendar = Calendar(busy=[{"start": "2026-11-20", "end": "2026-11-21"}],
                        offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"])

    assert draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock) == 1

    skill, arguments = calendar.ran[0]
    assert skill == SKILL and arguments["task"] == "offer" and arguments["time_zone"] == "America/New_York"
    assert arguments["days"][0] == {"day": "2026-11-19", "from": "09:00", "to": "17:00"}
    assert len(arguments["days"]) == 5
    assert threads.get(thread).state == "drafted" and "Thursday 10:00" in (threads.get(thread).reply or "")
    talk = Talks(project / "data" / "jobs.db").get(thread)
    assert talk is not None and talk.times() == [_at(19, 10), _at(23, 14, 30)]
    assert talk.title == "Call with Example Labs about QA Engineer"
    # Nothing is added to the calendar before the person's yes.
    assert calendar.events == {} and calendar.tasks() == ["offer"]


@pytest.mark.parametrize("offer", [
    ["2026-11-20T10:00:00-05:00", "2026-11-23T14:30:00-05:00"],  # the busy day
    ["2026-11-19T08:00:00-05:00", "2026-11-23T14:30:00-05:00"],  # before the working hours
    ["2026-11-19T10:00:00-05:00", "2026-11-26T10:00:00-05:00"],  # after the five days
    ["2026-11-19T10:10:00-05:00", "2026-11-23T14:30:00-05:00"],  # not on the half hour
    ["2026-11-19T10:00:00-05:00"],  # one time while many are free
    ["2026-11-19T10:00:00-05:00", "2026-11-19T10:00:00-05:00"],  # the same time twice
])
def test_an_offer_of_a_time_that_is_not_free_is_a_failed_draft(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, offer: list[str]
) -> None:
    thread, = _saved(threads, jobs, _email())
    calendar = Calendar(busy=[{"start": "2026-11-20", "end": "2026-11-21"}], offer=offer)

    assert draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock) == 0

    assert threads.get(thread).state == "new" and threads.get(thread).failures == 1
    assert Talks(project / "data" / "jobs.db").get(thread) is None


def test_a_full_calendar_offers_no_time_and_holds_nothing(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, = _saved(threads, jobs, _email())
    busy = [{"start": "2026-11-19", "end": "2026-11-26"}]

    assert draft_replies(threads, project, run=Calendar(busy=busy, offer=[]), fetch=no_fetch, clock=clock) == 1

    assert Talks(project / "data" / "jobs.db").get(thread) is None
    calendar = Calendar()
    _send(threads, project, thread, calendar)
    assert calendar.tasks() == [SEND_SKILL] and calendar.events == {}


def test_the_question_shows_the_times_and_what_yes_holds(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)

    asked = questions(jobs, project / "data", threads)

    assert "asks to talk" in asked
    assert "Times offered: Thu 19 Nov 10:00, Mon 23 Nov 14:30 (EST)" in asked
    # An email goes without the resume: the Gmail connector takes its bytes only as typed text.
    assert "Resume attached" not in asked
    assert '"Held: Call with Example Labs about QA Engineer" is added to Google Calendar at Thu 19 Nov 10:00' in asked
    assert f"hunt offer-times {thread}" in asked


def test_offer_times_drafts_a_reply_again_with_new_times(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    calendar = Calendar(offer=["2026-11-24T11:00:00-05:00", "2026-11-25T15:00:00-05:00"])

    offer_times(threads, project, thread, run=calendar, clock=clock)

    skill, arguments = calendar.ran[0]
    assert arguments["old_draft_id"] == "d2"
    assert Talks(project / "data" / "jobs.db").get(thread).times() == [_at(24, 11), _at(25, 15)]
    assert threads.get(thread).state == "drafted"
    with pytest.raises(ReplyError, match="offered"):
        offer_times(threads, project, thread, run=Calendar(offer=["2026-11-19T07:00:00-05:00"]), clock=clock)


# The held event


def test_the_held_event_is_created_only_after_send_reply(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    calendar = Calendar()
    # Runs before the yes hold nothing.
    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
    questions(jobs, project / "data", threads)
    assert calendar.events == {} and calendar.ran == []

    _send(threads, project, thread, calendar)
    assert calendar.events == {}

    held = cli.hold_times(threads, project, run=calendar, clock=clock)

    assert held == [f"Call with Example Labs about QA Engineer at {_at(19, 10).isoformat()}"]
    assert calendar.events == {"e1": {"title": "Held: Call with Example Labs about QA Engineer",
                                      "start": "2026-11-19T10:00"}}
    skill, arguments = calendar.ran[-1]
    assert arguments["end"] == "2026-11-19T10:30" and arguments["time_zone"] == "America/New_York"
    talk = Talks(project / "data" / "jobs.db").get(thread)
    assert talk.state == "held" and talk.event_id == "e1"
    # Held once: a later run adds nothing.
    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
    assert calendar.tasks().count("hold") == 1


def test_hunt_send_reply_sends_an_email_offer_without_the_resume_and_holds_the_time(
    project: Path, threads: Threads, jobs: JobsList, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=MovableClock(MORNING))
    calendar = Calendar()
    monkeypatch.setattr(cli, "run_send", calendar)
    monkeypatch.setattr(cli, "run_draft", calendar)

    assert cli.main(["send-reply", str(thread)]) == 0

    said = capsys.readouterr().out
    assert "held in Google Calendar: Call with Example Labs about QA Engineer" in said
    sent, = calendar.sent
    assert "resume" not in sent and "resume_base64" not in sent
    assert not (project / "jobs" / "replies" / str(thread) / "resume.pdf.base64").exists()
    assert list(calendar.events) == ["e1"]


def test_a_plain_reply_goes_out_without_the_resume(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    thread, = _saved(threads, jobs, _email(asks_to_talk=False))
    draft_replies(threads, project, run=Calendar(), fetch=no_fetch, clock=clock)
    calendar = Calendar()

    _send(threads, project, thread, calendar)

    assert "resume" not in calendar.sent[0] and calendar.tasks() == [SEND_SKILL]


def test_a_hold_reported_failed_is_tried_again_then_told(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    _send(threads, project, thread, Calendar())
    refused = Calendar(hold={"result": "failed", "reason": "the calendar refused it"})
    talks = Talks(project / "data" / "jobs.db")

    assert cli.hold_times(threads, project, run=refused, clock=clock) == []
    assert talks.get(thread).state == "offered"
    assert "Calendar" not in questions(jobs, project / "data", threads)
    cli.hold_times(threads, project, run=refused, clock=clock)

    assert talks.get(thread).state == "failed"
    assert "was not held: the calendar refused it" in questions(jobs, project / "data", threads)
    cli.hold_times(threads, project, run=refused, clock=clock)
    assert refused.tasks().count("hold") == 2


@pytest.mark.parametrize("hold", [{"result": "what"}, None])
def test_a_hold_with_no_report_is_never_tried_again(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, hold: dict | None
) -> None:
    thread, = _saved(threads, jobs, _email())
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    _send(threads, project, thread, Calendar())

    def broken(command: list[str]) -> None:
        if hold is None:
            raise ReplyError("the offer-times run failed (1): no message")
        Path(_arguments(command)[1]["out"]).write_text(json.dumps(hold))

    cli.hold_times(threads, project, run=broken, clock=clock)
    again = Calendar()
    cli.hold_times(threads, project, run=again, clock=clock)

    assert Talks(project / "data" / "jobs.db").get(thread).state == "unsure"
    assert again.ran == []
    assert "needs a look in Google Calendar whether it was added" in questions(jobs, project / "data", threads)


# The confirmed time


def _held(project: Path, threads: Threads, jobs: JobsList, clock: MovableClock) -> tuple[int, Calendar]:
    thread, = _saved(threads, jobs, _email(1))
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    calendar = Calendar()
    _send(threads, project, thread, calendar)
    cli.hold_times(threads, project, run=calendar, clock=clock)
    return thread, calendar


def test_a_time_the_recruiter_picks_moves_the_held_event_and_confirms_it(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    # The answer asks to talk too: it is not offered times again, it is drafted with the offer.
    answer, = _saved(threads, jobs, _email(2, text="Monday 2:30 works, talk then!"))
    calendar.chosen = "2026-11-23T14:30:00-05:00"

    assert draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock) == 1

    skill, arguments = [run for run in calendar.ran if run[0] == DRAFT_SKILL][0]
    assert arguments["thread"] == answer
    assert arguments["offered_times"] == ["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]
    assert calendar.events == {"e1": {"title": "Call with Example Labs about QA Engineer", "start": "2026-11-23T14:30"}}
    talk = Talks(project / "data" / "jobs.db").get(thread)
    assert talk.state == "confirmed" and talk.start == _at(23, 14, 30).isoformat()
    assert threads.get(answer).state == "drafted"


def test_a_picked_time_is_confirmed_though_the_run_stopped_once_its_reply_was_saved(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    answer, = _saved(threads, jobs, _email(2, text="Monday 2:30 works, talk then!"))
    calendar.chosen = "2026-11-23T14:30:00-05:00"
    saved = Threads.drafted

    def drafted_then_stopped(self: Threads, *arguments: object) -> bool:
        saved(self, *arguments)
        raise KeyboardInterrupt

    monkeypatch.setattr(Threads, "drafted", drafted_then_stopped)
    with pytest.raises(KeyboardInterrupt):
        draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
    monkeypatch.undo()
    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert calendar.events == {"e1": {"title": "Call with Example Labs about QA Engineer", "start": "2026-11-23T14:30"}}
    assert Talks(project / "data" / "jobs.db").get(thread).state == "confirmed"
    assert threads.get(answer).state == "drafted" and calendar.tasks().count("confirm") == 1


def test_a_pick_in_an_answer_a_newer_message_replaced_moves_nothing(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    _saved(threads, jobs, _email(2, text="Monday 2:30 works, talk then!"))
    calendar.chosen = "2026-11-23T14:30:00-05:00"

    def newer_while_drafting(command: list[str]) -> None:
        # The watch reads a correction while this answer is being drafted.
        if _arguments(command)[0] == DRAFT_SKILL and len(threads.in_states("new")) == 1:
            _saved(threads, jobs, _email(3, text="Sorry, Thursday 10:00 after all"))
        calendar(command)

    draft_replies(threads, project, run=newer_while_drafting, fetch=no_fetch, clock=clock)

    assert Talks(project / "data" / "jobs.db").get(thread).state == "held"
    assert calendar.tasks().count("confirm") == 0


@pytest.mark.parametrize("chosen", [None, "2026-11-24T09:00:00-05:00"])
def test_an_answer_with_no_time_offered_moves_nothing(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, chosen: str | None
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    _saved(threads, jobs, _email(2, text="Could you do Tuesday at 9?"))
    calendar.chosen = chosen

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert "confirm" not in calendar.tasks()
    assert calendar.events["e1"]["start"] == "2026-11-19T10:00"
    assert Talks(project / "data" / "jobs.db").get(thread).state == "held"


def test_a_confirm_that_fails_is_told_and_the_event_stays_held(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    _saved(threads, jobs, _email(2, text="Monday 2:30 works"))

    def refusing(command: list[str]) -> None:
        skill, arguments = _arguments(command)
        if skill == SKILL:
            Path(arguments["out"]).write_text(json.dumps({"result": "failed", "reason": "no such event"}))
        else:
            calendar.chosen = "2026-11-23T14:30:00-05:00"
            calendar(command)

    draft_replies(threads, project, run=refusing, fetch=no_fetch, clock=clock)

    assert Talks(project / "data" / "jobs.db").get(thread).state == "held"
    assert "was not confirmed at 2026-11-23T14:30:00-05:00: no such event" in questions(jobs, project / "data", threads)


def test_a_time_that_was_not_offered_cannot_be_confirmed(project: Path, threads: Threads, jobs: JobsList,
                                                         clock: MovableClock) -> None:
    from job_hunt.times import confirm

    thread, calendar = _held(project, threads, jobs, clock)
    talks = Talks(project / "data" / "jobs.db")

    with pytest.raises(TimesError, match="not one of the times offered"):
        confirm(talks, talks.get(thread), _at(24, 9), NEW_YORK, project / "event.json", calendar)
    assert "confirm" not in calendar.tasks()


def test_the_draft_runs_are_named_by_their_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    named: list[str] = []
    monkeypatch.setattr(cli, "run_claude", lambda command, root, *, skill, error: named.append(skill))

    cli.run_draft(["claude", "-p", '/offer-times {"task": "hold"}', "--model", "m"])
    cli.run_draft(["claude", "-p", '/draft-reply {"thread": 1}'])

    assert named == ["offer-times", "draft-reply"]


# What the reviews found


def _offered(project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, **change: object) -> int:
    thread, = _saved(threads, jobs, _email(**change))
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    return thread


def test_an_offer_changed_by_hand_holds_nothing_on_its_yes(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    from job_hunt.replies import edit_reply

    thread = _offered(project, threads, jobs, clock)
    edit_reply(threads, project, thread, "Thanks, but I am no longer looking. Ada")
    calendar = Calendar()

    assert "Times offered" not in questions(jobs, project / "data", threads)
    _send(threads, project, thread, calendar)
    cli.hold_times(threads, project, run=calendar, clock=clock)

    assert calendar.events == {} and "resume" not in calendar.sent[0]


def test_a_skipped_offer_is_never_held(project: Path, threads: Threads, jobs: JobsList, clock: MovableClock) -> None:
    from job_hunt.replies import skip_reply

    thread = _offered(project, threads, jobs, clock)
    skip_reply(threads, thread)
    calendar = Calendar()

    cli.hold_times(threads, project, run=calendar, clock=clock)

    assert calendar.ran == []


def test_an_offer_another_run_took_is_not_held_twice(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    from job_hunt.times import hold

    thread = _offered(project, threads, jobs, clock)
    _send(threads, project, thread, Calendar())
    # Two runs read it `offered`; the second claims it after the first did.
    first, second = Talks(project / "data" / "jobs.db"), Talks(project / "data" / "jobs.db")
    assert first.claim(thread)
    calendar = Calendar()

    with pytest.raises(TimesError, match="no offer waiting"):
        hold(second, thread, NEW_YORK, project / "event.json", calendar)
    assert calendar.ran == []


def test_a_hold_left_by_a_run_that_died_is_told_and_never_added_again(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread = _offered(project, threads, jobs, clock)
    _send(threads, project, thread, Calendar())
    assert Talks(project / "data" / "jobs.db").claim(thread)
    calendar = Calendar()

    cli.hold_times(threads, project, run=calendar, clock=clock)

    assert calendar.ran == [] and Talks(project / "data" / "jobs.db").get(thread).state == "unsure"
    assert "needs a look in Google Calendar whether it was added" in questions(jobs, project / "data", threads)


def test_a_hold_stopped_in_the_middle_is_unsure(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread = _offered(project, threads, jobs, clock)
    _send(threads, project, thread, Calendar())

    def stopped(command: list[str]) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli.hold_times(threads, project, run=stopped, clock=clock)
    assert Talks(project / "data" / "jobs.db").get(thread).state == "unsure"


def test_the_watch_tries_a_failed_hold_again_when_nothing_is_new(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = _offered(project, threads, jobs, clock)
    _send(threads, project, thread, Calendar())
    cli.hold_times(threads, project, run=Calendar(hold={"result": "failed", "reason": "busy server"}), clock=clock)
    calendar = Calendar()
    monkeypatch.setattr(cli, "ROOT", project)
    monkeypatch.setattr(cli, "DATA", project / "data")
    monkeypatch.setattr(cli, "run_draft", calendar)
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: None)

    assert cli.main(["watch"]) == 0

    assert calendar.tasks() == ["hold"] and Talks(project / "data" / "jobs.db").get(thread).state == "held"


@pytest.mark.parametrize("then", ["confirmed", "unsure"])
def test_a_conversation_with_a_call_booked_is_not_offered_times_again(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, then: str
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    Talks(project / "data" / "jobs.db").move(thread, then)
    answer, = _saved(threads, jobs, _email(3, text="Looking forward to our call!"))

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert "offer" not in calendar.tasks()
    skill, arguments = [run for run in calendar.ran if run[0] == DRAFT_SKILL][-1]
    assert arguments["thread"] == answer and "offered_times" not in arguments


def test_a_picked_time_with_no_offset_is_read_in_the_persons_zone(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    _saved(threads, jobs, _email(2, text="Monday 2:30 works"))
    calendar.chosen = "2026-11-23T14:30:00"

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert Talks(project / "data" / "jobs.db").get(thread).state == "confirmed"


def test_an_offer_on_linkedin_goes_out_with_the_resume_to_upload(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    from job_hunt.replies import send_command

    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    found, _ = read_messages({"linkedin_messages": [{
        "conversation": "https://www.linkedin.com/messaging/thread/9/", "from": "Sam Recruiter",
        "text": "Can we talk?", "date": "2026-11-18T06:40:00-05:00", "job_link": "https://jobs.example.com/1",
        "company": "Example Labs", "asks_to_talk": True}]})
    thread = threads.save(found[0], jobs)
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)

    assert f"Resume attached: {project / 'jobs' / '1' / 'resume.pdf'}" in questions(jobs, project / "data", threads)
    command = send_command(threads.get(thread), "Glad to talk", project)

    skill, arguments = _arguments(command)
    assert arguments["resume"] == str(project / "jobs" / "1" / "resume.pdf") and "resume_base64" not in arguments
    assert "mcp__claude-in-chrome__file_upload" in command[command.index("--allowedTools") + 1]


def test_a_reply_saved_without_its_times_holds_nothing_and_goes_without_the_resume(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    found, _ = read_messages({"linkedin_messages": [{
        "conversation": "https://www.linkedin.com/messaging/thread/9/", "from": "Sam Recruiter",
        "text": "Can we talk?", "date": "2026-11-18T06:40:00-05:00", "job_link": "https://jobs.example.com/1",
        "company": "Example Labs", "asks_to_talk": True}]})
    thread = threads.save(found[0], jobs)
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)

    def stopped(*_: object) -> None:
        raise KeyboardInterrupt

    # Offered again, the run stops once the new reply is saved and before its times are.
    monkeypatch.setattr(Talks, "offered", stopped)
    calendar = Calendar(offer=["2026-11-24T11:00:00-05:00", "2026-11-25T15:00:00-05:00"])
    calendar.reply = "Glad to talk. Tuesday 11:00 or Wednesday 15:00? Ada"
    with pytest.raises(KeyboardInterrupt):
        offer_times(threads, project, thread, run=calendar, clock=clock)
    monkeypatch.undo()
    assert threads.get(thread).reply == calendar.reply

    assert "Times offered" not in questions(jobs, project / "data", threads)
    _send(threads, project, thread, calendar)
    cli.hold_times(threads, project, run=calendar, clock=clock)

    assert calendar.events == {} and "resume" not in calendar.sent[-1]
    assert Talks(project / "data" / "jobs.db").get(thread) is None
    # Told once: a later run has nothing left to hold.
    cli.hold_times(threads, project, run=calendar, clock=clock)
    assert (project / "data" / "morning.log").read_text().count("was not held") == 1
    assert calendar.events == {}


def test_an_offer_made_again_in_new_words_is_held_at_its_new_times(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    found, _ = read_messages({"linkedin_messages": [{
        "conversation": "https://www.linkedin.com/messaging/thread/9/", "from": "Sam Recruiter",
        "text": "Can we talk?", "date": "2026-11-18T06:40:00-05:00", "job_link": "https://jobs.example.com/1",
        "company": "Example Labs", "asks_to_talk": True}]})
    thread = threads.save(found[0], jobs)
    draft_replies(threads, project, run=Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"]),
                  fetch=no_fetch, clock=clock)
    calendar = Calendar(offer=["2026-11-24T11:00:00-05:00", "2026-11-25T15:00:00-05:00"])
    calendar.reply = "Glad to talk. Tuesday 11:00 or Wednesday 15:00? Ada"

    offer_times(threads, project, thread, run=calendar, clock=clock)

    asked = questions(jobs, project / "data", threads)
    assert "Times offered: Tue 24 Nov 11:00, Wed 25 Nov 15:00 (EST)" in asked and "Resume attached" in asked
    _send(threads, project, thread, calendar)
    assert calendar.sent[-1]["resume"] == str(project / "jobs" / "1" / "resume.pdf")
    cli.hold_times(threads, project, run=calendar, clock=clock)
    assert calendar.events == {"e1": {"title": "Held: Call with Example Labs about QA Engineer",
                                      "start": "2026-11-24T11:00"}}


def test_a_second_offer_in_a_conversation_moves_its_held_event(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    first, calendar = _held(project, threads, jobs, clock)
    # None of the times suit: the person asks for new ones on the recruiter's answer.
    answer, = _saved(threads, jobs, _email(2, text="None of those work, sorry", asks_to_talk=False))
    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
    calendar.offer = ["2026-11-24T11:00:00-05:00", "2026-11-25T15:00:00-05:00"]
    offer_times(threads, project, answer, run=calendar, clock=clock)
    _send(threads, project, answer, calendar)

    cli.hold_times(threads, project, run=calendar, clock=clock)

    moved = [arguments for skill, arguments in calendar.ran if arguments.get("task") == "hold"][-1]
    assert moved["event_id"] == "e1"
    assert calendar.events == {"e1": {"title": "Held: Call with Example Labs about QA Engineer",
                                      "start": "2026-11-24T11:00"}}
    talks = Talks(project / "data" / "jobs.db")
    assert talks.get(first).state == "replaced"
    assert talks.get(answer).state == "held" and talks.get(answer).event_id == "e1"


def test_a_send_the_person_saw_go_is_held(project: Path, threads: Threads, jobs: JobsList, clock: MovableClock) -> None:
    from job_hunt.replies import skip_reply

    thread = _offered(project, threads, jobs, clock)

    def no_report(command: list[str]) -> None:
        pass

    send_reply(threads, project, thread, run=no_report, lock=LinkedInLock(project / "data"), sleep=lambda _: None)
    assert threads.get(thread).state == "unsure"
    assert skip_reply(threads, thread).state == "sent"
    calendar = Calendar()

    cli.hold_times(threads, project, run=calendar, clock=clock)

    assert list(calendar.events) == ["e1"]


def test_a_call_that_is_over_leaves_the_next_round_to_be_offered(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    # The last time offered, Monday 14:30, is over.
    clock.now = _at(23, 15, 30)
    _saved(threads, jobs, _email(4, text="Great call! Could we set up a second round?", date="2026-11-23T16:00:00-05:00"))

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert "offer" in calendar.tasks()


def test_a_confirmed_call_that_is_over_leaves_the_next_round_to_be_offered(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    Talks(project / "data" / "jobs.db").move(thread, "confirmed", start=_at(19, 10).isoformat())
    clock.now = _at(19, 10, 30)
    _saved(threads, jobs, _email(4, text="Great call! Could we set up a second round?", date="2026-11-19T11:00:00-05:00"))

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert "offer" in calendar.tasks()


def test_a_later_time_picked_after_the_first_is_over_confirms_the_held_event(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    # Friday: Thursday 10:00, the time held, is over; Monday 14:30 is still to come.
    clock.now = _at(20, 9)
    _saved(threads, jobs, _email(5, text="Monday 2:30 works, let's talk then!", date="2026-11-20T08:30:00-05:00"))
    calendar.chosen = "2026-11-23T14:30:00-05:00"

    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)

    assert "offer" not in calendar.tasks()
    assert calendar.events == {"e1": {"title": "Call with Example Labs about QA Engineer", "start": "2026-11-23T14:30"}}
    assert Talks(project / "data" / "jobs.db").get(thread).state == "confirmed"


def test_a_next_round_held_after_the_first_is_over_is_a_new_event(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    first, calendar = _held(project, threads, jobs, clock)
    clock.now = _at(23, 15, 30)
    second, = _saved(threads, jobs, _email(4, text="Great call! Could we set up a second round?",
                                           date="2026-11-23T16:00:00-05:00"))
    calendar.offer = ["2026-11-24T11:00:00-05:00", "2026-11-25T15:00:00-05:00"]
    draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
    _send(threads, project, second, calendar)

    cli.hold_times(threads, project, run=calendar, clock=clock)

    # The first round's event stays where it was: the call it held is in the past.
    assert calendar.events == {
        "e1": {"title": "Held: Call with Example Labs about QA Engineer", "start": "2026-11-19T10:00"},
        "e2": {"title": "Held: Call with Example Labs about QA Engineer", "start": "2026-11-24T11:00"}}
    talks = Talks(project / "data" / "jobs.db")
    assert talks.get(first).state == "held" and talks.get(second).event_id == "e2"


def test_a_first_offer_stopped_before_its_reply_was_saved_is_offered_again_whole(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "jobs" / "1" / "resume.pdf").write_bytes(b"%PDF-1.4 Ada Example")
    found, _ = read_messages({"linkedin_messages": [{
        "conversation": "https://www.linkedin.com/messaging/thread/9/", "from": "Sam Recruiter",
        "text": "Can we talk?", "date": "2026-11-18T06:40:00-05:00", "job_link": "https://jobs.example.com/1",
        "company": "Example Labs", "asks_to_talk": True}]})
    thread = threads.save(found[0], jobs)
    calendar = Calendar(offer=["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"])
    saved_reply, saved_times = Threads.redrafted, Talks.offered

    def stopped(*_: object) -> None:
        raise KeyboardInterrupt

    # The run stops between the reply and its times, whichever is saved first.
    for first, second in ((Threads, "redrafted"), (Talks, "offered")):
        monkeypatch.setattr(first, second, stopped)
        with pytest.raises(KeyboardInterrupt):
            draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
        monkeypatch.undo()
        draft_replies(threads, project, run=calendar, fetch=no_fetch, clock=clock)
        assert Threads.redrafted is saved_reply and Talks.offered is saved_times

        asked = questions(jobs, project / "data", threads)
        assert "Times offered: Thu 19 Nov 10:00, Mon 23 Nov 14:30 (EST)" in asked and "Resume attached" in asked
        if second == "offered":
            break
        threads.move(thread, "new")
        Talks(project / "data" / "jobs.db").forget(thread)
    _send(threads, project, thread, calendar)
    assert calendar.sent[-1]["resume"] == str(project / "jobs" / "1" / "resume.pdf")
    cli.hold_times(threads, project, run=calendar, clock=clock)
    assert list(calendar.events) == ["e1"]


def test_a_newer_message_read_while_a_pick_is_confirmed_is_told(
    project: Path, threads: Threads, jobs: JobsList, clock: MovableClock
) -> None:
    thread, calendar = _held(project, threads, jobs, clock)
    _saved(threads, jobs, _email(2, text="Monday 2:30 works, talk then!"))
    calendar.chosen = "2026-11-23T14:30:00-05:00"

    def newer_while_confirming(command: list[str]) -> None:
        calendar(command)
        if _arguments(command)[1].get("task") == "confirm":
            _saved(threads, jobs, _email(3, text="Sorry, Thursday 10:00 after all"))

    draft_replies(threads, project, run=newer_while_confirming, fetch=no_fetch, clock=clock)

    assert Talks(project / "data" / "jobs.db").get(thread).state == "confirmed"
    told = (project / "data" / "morning.log").read_text()
    assert "newer message" in told and "Call with Example Labs about QA Engineer" in told
