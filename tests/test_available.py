"""`hunt available`: whether the person is free right now for a CAPTCHA, from stand-ins for the
signals of their computer; and the watch's part for the jobs. No command is run for real."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.available import TELL_JOBS, Verdict, available, day_ends, day_summary, replied, watch_jobs
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.morning import FIRST_MESSAGE, hook_settings, session_command
from job_hunt.questions import on_captcha
from job_hunt.settings import Settings
from job_hunt.watch import in_hours
from movable_clock import MovableClock

# Taken before the tests' stand-in replaces it (conftest.py).
REAL_VERDICT = cli.verdict
SETTINGS = Settings(titles=["QA Engineer"], areas=[], remote="only", lowest_salary=None, morning_hour=7,
                    summary_hour=18)
# 10:00 on a working day, the person's own time.
NOON = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


class Computer:
    """Answers the commands that read the signals, as a GNOME desktop would; a missing answer
    stands for a command that is not installed."""

    def __init__(self, idle_ms: int | None = 1_000, locked: str | None = "no", xprintidle: int | None = None,
                 ioreg_ns: int | None = None) -> None:
        self.idle_ms, self.locked, self.xprintidle, self.ioreg_ns = idle_ms, locked, xprintidle, ioreg_ns
        self.ran: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        self.ran.append(command)
        said = None
        if command[0] == "gdbus" and self.idle_ms is not None:
            said = f"(uint64 {self.idle_ms},)"
        elif command[0] == "xprintidle" and self.xprintidle is not None:
            said = str(self.xprintidle)
        elif command[:2] == ["loginctl", "show-user"] and self.locked is not None:
            said = "2"
        elif command[:2] == ["loginctl", "show-session"] and self.locked is not None:
            assert command[2] == "2"
            said = self.locked
        elif command[0] == "ioreg" and self.ioreg_ns is not None:
            said = f'    | |   "HIDIdleTime" = {self.ioreg_ns}\n'
        if said is None:
            return None
        return subprocess.CompletedProcess(command, 0, said + "\n", "")


def _verdict(tmp_path: Path, computer: Computer, *, at: datetime = NOON, system: str = "Linux",
             calls: list[datetime] | None = None, settings: Settings | None = SETTINGS) -> Verdict:
    return available(tmp_path, settings, system=system, run=computer, uid=1000, calls=lambda: calls or [],
                     clock=lambda: at)


def test_used_a_minute_ago_and_not_locked_is_at_the_computer(tmp_path: Path) -> None:
    said = _verdict(tmp_path, Computer(idle_ms=60_000))
    assert (said.ask, said.reason, said.from_afar) == (True, "you are at the computer", False)


def test_a_locked_screen_is_not_at_the_computer(tmp_path: Path) -> None:
    assert not _verdict(tmp_path, Computer(idle_ms=1_000, locked="yes")).ask


def test_three_minutes_without_a_key_is_not_at_the_computer(tmp_path: Path) -> None:
    said = _verdict(tmp_path, Computer(idle_ms=180_000))
    assert (said.ask, said.reason) == (False, "you are away from the computer")


def test_other_desktops_read_xprintidle_and_macos_ioreg(tmp_path: Path) -> None:
    assert _verdict(tmp_path, Computer(idle_ms=None, xprintidle=2_000)).ask
    assert not _verdict(tmp_path, Computer(idle_ms=None, xprintidle=600_000)).ask
    # macOS has no lock to read: input a moment ago is the person.
    assert _verdict(tmp_path, Computer(ioreg_ns=5_000_000_000), system="Darwin").ask
    assert not _verdict(tmp_path, Computer(ioreg_ns=900_000_000_000), system="Darwin").ask


def test_a_signal_that_cannot_be_read_is_not_known_and_the_next_one_is_read(tmp_path: Path) -> None:
    # No idle time at all: not known, so the chat's reply decides.
    nothing = Computer(idle_ms=None, locked=None)
    assert _verdict(tmp_path, nothing) == Verdict(False, "you are away from the computer")
    (tmp_path / "chat-reply").write_text((NOON - timedelta(minutes=2)).isoformat())
    assert _verdict(tmp_path, nothing) == Verdict(True, "you replied in the chat 2 minutes ago", from_afar=True)
    # The lock not known: a key pressed a moment ago still is the person.
    assert _verdict(tmp_path, Computer(idle_ms=1_000, locked=None)).ask
    # A reply file that cannot be read is not known either.
    (tmp_path / "chat-reply").write_text("not a time")
    assert not _verdict(tmp_path, nothing).ask
    # Nor is one that cannot be opened at all.
    (tmp_path / "chat-reply").unlink()
    (tmp_path / "chat-reply").mkdir()
    assert _verdict(tmp_path, nothing) == Verdict(False, "you are away from the computer")


def test_a_reply_in_the_chat_in_the_last_ten_minutes_asks_from_afar(tmp_path: Path) -> None:
    away = Computer(idle_ms=3_600_000)
    (tmp_path / "chat-reply").write_text((NOON - timedelta(minutes=9)).isoformat())
    said = _verdict(tmp_path, away)
    assert (said.ask, said.from_afar) == (True, True)
    (tmp_path / "chat-reply").write_text((NOON - timedelta(minutes=10)).isoformat())
    assert not _verdict(tmp_path, away).ask


def test_a_call_held_in_the_calendar_now_holds(tmp_path: Path) -> None:
    away = Computer(idle_ms=3_600_000)
    said = _verdict(tmp_path, away, calls=[NOON - timedelta(minutes=10)])
    assert said == Verdict(False, "you are in a call held in your Google Calendar")
    # Over: away, as any other hour.
    assert _verdict(tmp_path, away, calls=[NOON - timedelta(minutes=30)]).reason == "you are away from the computer"


def test_night_hours_hold(tmp_path: Path) -> None:
    away = Computer(idle_ms=3_600_000)
    for hour in (18, 23, 3, 6):
        assert _verdict(tmp_path, away, at=NOON.replace(hour=hour)) == Verdict(
            False, "it is night, from 18:00 to 07:00")
    assert _verdict(tmp_path, away, at=NOON.replace(hour=7)).reason == "you are away from the computer"
    # Settings that cannot be read leave night not known.
    assert _verdict(tmp_path, away, at=NOON.replace(hour=3), settings=None).reason == "you are away from the computer"


def test_at_the_computer_comes_first_even_at_night_or_in_a_call(tmp_path: Path) -> None:
    here = Computer(idle_ms=1_000)
    assert _verdict(tmp_path, here, at=NOON.replace(hour=23)).ask
    assert _verdict(tmp_path, here, calls=[NOON]).ask


def test_the_hook_keeps_a_reply_but_not_a_message_the_hunt_typed(tmp_path: Path) -> None:
    automatic = (FIRST_MESSAGE, TELL_JOBS)
    replied(json.dumps({"prompt": FIRST_MESSAGE}), tmp_path, NOON, automatic)
    replied(json.dumps({"prompt": TELL_JOBS + "\n"}), tmp_path, NOON, automatic)
    replied("not json", tmp_path, NOON, automatic)
    assert not (tmp_path / "chat-reply").exists()
    replied(json.dumps({"prompt": "done", "session_id": "x"}), tmp_path, NOON, automatic)
    assert (tmp_path / "chat-reply").read_text().strip() == NOON.isoformat()


def test_only_the_job_hunt_session_has_the_hook(tmp_path: Path) -> None:
    command = session_command(tmp_path)
    settings = json.loads(command[command.index("--settings") + 1])
    hook, = settings["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert hook["command"] == f"uv run --project {tmp_path} hunt replied"
    assert command[command.index("--settings") + 1] == hook_settings(tmp_path)


# The watch's part


def _job(jobs: JobsList, number: int, reason: str | None = 'a CAPTCHA solved, in the Chrome tab "Apply"') -> int:
    job = jobs.add(FoundJob(link=f"https://jobs.example{number}.com/{number}", title="QA Engineer",
                            company=f"Example Co {number}", place="", remote="remote", board="indeed", email_id=None,
                            email_date=None, area=None), kind="company_site")
    assert job is not None
    jobs.score(job, 8, "Good.")
    for state in ("picked", "resume_ready", "applying"):
        jobs.move(job, state)  # type: ignore[arg-type]
    if reason is not None:
        jobs.move(job, "paused", reason)
    return job


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(NOON)


@pytest.fixture
def jobs(tmp_path: Path, clock: MovableClock) -> JobsList:
    return JobsList(tmp_path / "jobs.db", clock=clock)


class Tell:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, text: str) -> None:
        self.texts.append(text)

    @property
    def times(self) -> int:
        return len(self.texts)


def test_waiting_captchas_are_told_at_once_when_free_and_held_otherwise(tmp_path: Path, jobs: JobsList) -> None:
    _job(jobs, 1)
    _job(jobs, 2)
    tell = Tell()
    assert watch_jobs(jobs, tmp_path, verdict=lambda: Verdict(False, "it is night"), tell=tell) == ""
    assert tell.times == 0
    said = watch_jobs(jobs, tmp_path, verdict=lambda: Verdict(True, "you are at the computer"), tell=tell)
    assert said == "told the chat: 2 CAPTCHAs wait and you are at the computer"
    assert tell.times == 1


def test_a_captcha_told_is_not_told_again_for_an_hour_and_never_once_asked(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    job = _job(jobs, 1)
    tell, free = Tell(), lambda: Verdict(True, "you are at the computer")
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    clock.advance(seconds=20 * 60)
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    assert tell.times == 1
    clock.advance(hours=1)
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    assert tell.times == 2
    # The chat asked it: nothing more to tell for this pause.
    jobs.asked(job)
    clock.advance(hours=2)
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    assert tell.times == 2


def test_a_captcha_held_all_day_is_told_once_by_the_last_watch_of_the_day(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    _job(jobs, 1)
    tell, away = Tell(), lambda: Verdict(False, "you are away from the computer")
    clock.now = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)
    # The watch's runs, every 20 minutes from the morning hour to the summary hour, and on.
    while clock.now.hour < 20:
        if in_hours(clock.now, SETTINGS):
            watch_jobs(jobs, tmp_path, verdict=away, tell=tell, ends=day_ends(clock.now, SETTINGS))
        clock.advance(seconds=20 * 60)
    assert tell.texts == [day_summary(on_captcha(jobs))]
    assert "Example Co 1" in tell.texts[0]
    # The chat sends the summary from `hunt status`; the hook keeps no reply for it.
    replied(json.dumps({"prompt": tell.texts[0]}), tmp_path, clock.now, automatic=(TELL_JOBS,))
    assert not (tmp_path / "chat-reply").exists()


def test_the_last_watch_tells_no_summary_for_a_captcha_already_asked_or_on_a_day_without_one(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    tell, away = Tell(), lambda: Verdict(False, "you are away from the computer")
    clock.now = datetime(2026, 11, 18, 17, 40, tzinfo=UTC)
    assert day_ends(clock.now, SETTINGS) == "2026-11-18"
    assert day_ends(clock.now - timedelta(seconds=60), SETTINGS) is None
    watch_jobs(jobs, tmp_path, verdict=away, tell=tell, ends=day_ends(clock.now, SETTINGS))
    jobs.asked(_job(jobs, 1))
    watch_jobs(jobs, tmp_path, verdict=away, tell=tell, ends=day_ends(clock.now, SETTINGS))
    assert tell.texts == []


def test_a_captcha_held_two_days_is_in_each_day_s_summary(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    _job(jobs, 1)
    tell, away = Tell(), lambda: Verdict(False, "you are away from the computer")
    for day in (18, 19):
        clock.now = datetime(2026, 11, day, 17, 40, tzinfo=UTC)
        watch_jobs(jobs, tmp_path, verdict=away, tell=tell, ends=day_ends(clock.now, SETTINGS))
    assert tell.times == 2


def test_a_try_again_that_came_is_told_once(tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    job = _job(jobs, 1, reason=None)
    jobs.retry(job, clock.now + timedelta(hours=3))
    tell = Tell()

    def never() -> Verdict:
        raise AssertionError("no CAPTCHA waits, so the computer is not read")

    assert watch_jobs(jobs, tmp_path, verdict=never, tell=tell) == ""
    clock.advance(hours=3)
    assert watch_jobs(jobs, tmp_path, verdict=never, tell=tell) == "told the chat: 1 application may go on"
    clock.advance(seconds=20 * 60)
    assert watch_jobs(jobs, tmp_path, verdict=never, tell=tell) == ""
    assert tell.times == 1


def test_hunt_replied_reads_the_hook_from_its_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from job_hunt import cli
    from job_hunt.watch import TELL

    monkeypatch.setattr(cli, "DATA", tmp_path)
    for automatic in (FIRST_MESSAGE, TELL, TELL_JOBS):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": automatic})))
        assert cli.main(["replied"]) == 0
    assert not (tmp_path / "chat-reply").exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "done"})))
    assert cli.main(["replied"]) == 0
    assert datetime.fromisoformat((tmp_path / "chat-reply").read_text().strip()) <= datetime.now(UTC)


def test_a_try_again_and_a_captcha_are_told_together_and_a_held_captcha_later(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    again = _job(jobs, 1, reason=None)
    jobs.retry(again, clock.now)
    _job(jobs, 2)
    tell = Tell()
    held = watch_jobs(jobs, tmp_path, verdict=lambda: Verdict(False, "it is night"), tell=tell)
    assert (held, tell.times) == ("told the chat: 1 application may go on", 1)
    free = watch_jobs(jobs, tmp_path, verdict=lambda: Verdict(True, "you are at the computer"), tell=tell)
    assert (free, tell.times) == ("told the chat: 1 CAPTCHA waits and you are at the computer", 2)


def test_a_captcha_paused_again_after_done_is_told_again_at_once(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    job = _job(jobs, 1)
    tell, free = Tell(), lambda: Verdict(True, "you are at the computer")
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    jobs.asked(job)
    jobs.move(job, "resume_ready")
    jobs.move(job, "applying")
    clock.advance(seconds=60)
    jobs.move(job, "paused", 'a CAPTCHA solved, in the Chrome tab "Apply"')
    watch_jobs(jobs, tmp_path, verdict=free, tell=tell)
    assert tell.times == 2
    # Only what still waits is kept.
    assert list(json.loads((tmp_path / "jobs.told").read_text())) == [f"captcha {job} {jobs.get(job).updated_at}"]


# The command line


@pytest.fixture
def hunt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from job_hunt import cli

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(json.dumps({
        "titles": ["QA Engineer"], "areas": [], "remote": "only", "lowest_salary": None, "morning_hour": 7,
        "time_zone": "America/New_York"}))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DATA", tmp_path / "data")
    return tmp_path


def test_hunt_available_reads_the_settings_zone_and_the_held_calls(
        hunt: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli
    from job_hunt.times import Talks

    talks = Talks(hunt / "data" / "jobs.db")
    talks.offered(1, "email", "c1", "Call with Example Co", [NOON], "Tuesday?")
    talks.move(1, "held", start=NOON.isoformat())
    seen: dict = {}

    def spy(data: Path, settings: Settings | None, **given: object) -> Verdict:
        seen.update(given, settings=settings)
        return Verdict(False, "it is night, from 18:00 to 07:00")

    monkeypatch.setattr(cli, "available", spy)
    monkeypatch.setattr(cli, "verdict", REAL_VERDICT)
    assert cli.main(["available"]) == 0
    assert capsys.readouterr().out == "hold: it is night, from 18:00 to 07:00\n"
    assert str(seen["clock"]().tzinfo) == "America/New_York"
    assert seen["calls"]() == [NOON]



def _captcha_waits(hunt: Path) -> None:
    _job(JobsList(hunt / "data" / "jobs.db"), 1)


def test_hunt_watch_tells_the_chat_of_waiting_captchas_in_its_hours_only(
        hunt: Path, monkeypatch: pytest.MonkeyPatch, told) -> None:  # type: ignore[no-untyped-def]
    _captcha_waits(hunt)
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: "")
    monkeypatch.setattr(cli, "hold_times", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "verdict", lambda: Verdict(True, "you are at the computer"))
    monkeypatch.setattr(cli, "in_hours", lambda now, settings: False)
    assert cli.main(["watch"]) == 0
    assert told.texts == []
    monkeypatch.setattr(cli, "in_hours", lambda now, settings: True)
    assert cli.main(["watch"]) == 0
    assert told.texts == [TELL_JOBS]


def test_a_failing_jobs_part_of_the_watch_leaves_the_messages_part_running(
        hunt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages = []
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: messages.append(1) or "")
    monkeypatch.setattr(cli, "hold_times", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "in_hours", lambda now, settings: True)

    def broken(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(cli, "watch_jobs", broken)
    assert cli.main(["watch"]) == 0
    assert cli.main(["watch"]) == 0
    assert messages == [1, 1]
    # Told once, not every 20 minutes.
    assert (hunt / "data" / "morning.log").read_text().count("hunt watch jobs: disk full") == 1


def test_hunt_watch_tells_the_day_s_summary_at_its_last_run(
        hunt: Path, monkeypatch: pytest.MonkeyPatch, told) -> None:  # type: ignore[no-untyped-def]
    _captcha_waits(hunt)
    monkeypatch.setattr(cli, "watch", lambda *args, **kwargs: "")
    monkeypatch.setattr(cli, "hold_times", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "in_hours", lambda now, settings: True)
    monkeypatch.setattr(cli, "day_ends", lambda now, settings: None)
    assert cli.main(["watch"]) == 0
    assert told.texts == []
    monkeypatch.setattr(cli, "day_ends", lambda now, settings: "2026-11-18")
    assert cli.main(["watch"]) == 0
    assert cli.main(["watch"]) == 0
    assert told.texts == [day_summary(on_captcha(JobsList(hunt / "data" / "jobs.db")))]


def test_a_session_the_watch_starts_opens_with_what_it_was_told(tmp_path: Path) -> None:
    from job_hunt.watch import tell_session

    started: list[tuple[Path, str]] = []
    gone = lambda command: subprocess.CompletedProcess(command, 1, "", "")  # noqa: E731
    tell_session(tmp_path, run=gone, start=lambda root, text: started.append((root, text)), text="The day ends")
    assert started == [(tmp_path, "The day ends")]
    assert session_command(tmp_path, message="The day ends")[-1] == "The day ends"
    assert session_command(tmp_path)[-1] == FIRST_MESSAGE


def test_the_last_watch_tells_no_summary_when_the_person_is_free_for_the_captcha(
        tmp_path: Path, jobs: JobsList, clock: MovableClock) -> None:
    _job(jobs, 1)
    tell = Tell()
    clock.now = datetime(2026, 11, 18, 17, 40, tzinfo=UTC)
    said = watch_jobs(jobs, tmp_path, verdict=lambda: Verdict(True, "you are at the computer"), tell=tell,
                      ends=day_ends(clock.now, SETTINGS))
    assert tell.texts == [TELL_JOBS]
    assert said == "told the chat: 1 CAPTCHA waits and you are at the computer"
