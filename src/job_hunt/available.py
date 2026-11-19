"""`hunt available`: whether the person is free right now for a CAPTCHA in Chrome.

It decides from signals on the person's own computer, read in this order, and never asks the
person anything:

1. **At the computer:** the keyboard or mouse used in the last `IDLE` and the screen not locked.
   On GNOME the idle time is `org.gnome.Mutter.IdleMonitor.GetIdletime` and the lock the
   graphical session's `LockedHint` (`loginctl`); on other X11 desktops `xprintidle`; on macOS
   the `HIDIdleTime` of `ioreg`, with no lock to read. Ask at once: the tab is right there.
2. **Just talking to the chat:** the person replied in the `job-hunt` session in the last
   `REPLY` (`data/chat-reply`, written by the session's own prompt hook, `replied`). Ask at once.
3. **Busy:** a call the hunt held or confirmed in their Google Calendar is on now. Hold.
4. **Night:** the hours from the summary hour to the morning hour. Hold.

Otherwise the person is away: hold. A signal that cannot be read is "not known", and the next
one is read. Busy, night and away all hold, so no model is started to read the calendar: the
calls the hunt itself put there are the busy times it knows.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_hunt.jobs import Job, JobsList
from job_hunt.settings import Settings

IDLE = timedelta(minutes=3)
REPLY = timedelta(minutes=10)
CALL = timedelta(minutes=30)
REPLIED = "chat-reply"

Run = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]


@dataclass(frozen=True)
class Verdict:
    ask: bool
    reason: str
    # Asked from the chat, not at the computer: the CAPTCHA is done in its Chrome from afar.
    from_afar: bool = False


def run_quietly(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    """A command's answer, or None when it is not installed or does not answer in 5 seconds."""
    environment = dict(os.environ)
    # A timer's run has no session bus address; the user's bus is at its usual place.
    environment.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5, env=environment)
    except (OSError, subprocess.SubprocessError):
        return None


def _out(run: Run, command: list[str]) -> str | None:
    done = run(command)
    return done.stdout.strip() if done is not None and done.returncode == 0 and done.stdout.strip() else None


def idle_time(system: str, run: Run) -> timedelta | None:
    if system == "Darwin":
        said = _out(run, ["ioreg", "-c", "IOHIDSystem", "-d", "4"])
        found = re.search(r'"HIDIdleTime" = (\d+)', said or "")
        return timedelta(microseconds=int(found.group(1)) / 1000) if found else None
    said = _out(run, ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                      "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                      "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
    if found := re.search(r"uint64 (\d+)", said or ""):
        return timedelta(milliseconds=int(found.group(1)))
    said = _out(run, ["xprintidle"])
    return timedelta(milliseconds=int(said)) if said and said.isdigit() else None


def locked(system: str, run: Run, uid: int) -> bool | None:
    if system == "Darwin":
        return None
    session = _out(run, ["loginctl", "show-user", str(uid), "-p", "Display", "--value"])
    if not session:
        return None
    hint = _out(run, ["loginctl", "show-session", session, "-p", "LockedHint", "--value"])
    return {"yes": True, "no": False}.get(hint or "")


def at_computer(system: str, run: Run, uid: int) -> bool | None:
    idle = idle_time(system, run)
    if idle is None:
        return None
    if idle >= IDLE:
        return False
    # Used a moment ago: a lock that cannot be read does not hide that.
    return locked(system, run, uid) is not True


def replied(hook_input: str, data: Path, now: datetime, automatic: Sequence[str]) -> None:
    """The session's prompt hook: keep the time of the person's reply. A message the hunt typed
    into the session itself (`automatic`) is no reply."""
    try:
        prompt = json.loads(hook_input).get("prompt")
    except (json.JSONDecodeError, AttributeError):
        return
    if not isinstance(prompt, str) or prompt.strip() in {text.strip() for text in automatic}:
        return
    data.mkdir(parents=True, exist_ok=True)
    (data / REPLIED).write_text(now.isoformat() + "\n")


def last_reply(data: Path) -> datetime | None:
    try:
        when = datetime.fromisoformat((data / REPLIED).read_text().strip())
    except (OSError, ValueError):
        return None
    return when if when.tzinfo is not None else None


def busy_now(calls: Sequence[datetime], now: datetime) -> bool:
    return any(start <= now < start + CALL for start in calls)


def night(settings: Settings, now: datetime) -> bool:
    return not settings.morning_hour <= now.hour < settings.summary_hour


def available(
    data: Path,
    settings: Settings | None,
    *,
    system: str,
    run: Run = run_quietly,
    uid: int = 0,
    calls: Callable[[], Sequence[datetime]] = lambda: (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Verdict:
    """`clock` is the person's own time: night is read from its hour."""
    now = clock()
    if at_computer(system, run, uid):
        return Verdict(True, "you are at the computer")
    if (reply := last_reply(data)) is not None and timedelta(0) <= now - reply < REPLY:
        minutes = int((now - reply).total_seconds() // 60)
        return Verdict(True, f"you replied in the chat {minutes} minutes ago" if minutes else
                       "you replied in the chat just now", from_afar=True)
    try:
        busy = busy_now(calls(), now)
    except (sqlite3.Error, OSError, ValueError):
        # The jobs file could not be read: not known.
        busy = False
    if busy:
        return Verdict(False, "you are in a call held in your Google Calendar")
    if settings is not None and night(settings, now):
        return Verdict(False, f"it is night, from {settings.summary_hour:02d}:00 to {settings.morning_hour:02d}:00")
    return Verdict(False, "you are away from the computer")


# The watch's part for the jobs

TOLD = "jobs.told"
# A CAPTCHA the chat was told of, but did not ask about (the person was gone again when it
# looked), is told again after this long.
RETELL = timedelta(hours=1)
TELL_JOBS = ("Applications wait for you or may go on now. Run `uv run hunt questions` and ask what is new, "
             "as CLAUDE.md says.")


def _told(data: Path) -> dict[str, datetime]:
    try:
        return {key: datetime.fromisoformat(when) for key, when in json.loads((data / TOLD).read_text()).items()}
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return {}


def watch_jobs(jobs: JobsList, data: Path, *, verdict: Callable[[], Verdict], tell: Callable[[], None]) -> str:
    """Run by `hunt watch`: have the chat ask, once, when a job's try again or the next day of
    its site has come (`hunt questions` lists it as unfinished), or when CAPTCHAs wait and the
    person is free. Return what to print ("" when there was nothing to tell)."""
    # Imported here: `questions` reads this module's verdicts.
    from job_hunt.questions import on_captcha

    data.mkdir(parents=True, exist_ok=True)
    # One at a time: two watches at once would both tell, and both start a session.
    with (data / "jobs.told.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""
        return _watch_jobs(jobs, data, verdict, tell, on_captcha(jobs))


def _watch_jobs(jobs: JobsList, data: Path, verdict: Callable[[], Verdict], tell: Callable[[], None],
                waiting: list[Job]) -> str:
    now = jobs.now()
    told = _told(data)
    due = [f"due {job.id} {job.not_before}" for job in jobs.in_states("resume_ready")
           if job.not_before is not None and jobs.due(job)]
    captchas = [f"captcha {job.id} {job.updated_at}" for job in waiting if not job.asked]
    telling = [key for key in due if key not in told]
    fresh = [key for key in captchas if key not in told or now - told[key] >= RETELL]
    if fresh and (said := verdict()).ask:
        telling += fresh
    if telling:
        tell()
    # Only what still waits is kept.
    kept = {key: told[key] for key in due + captchas if key in told} | dict.fromkeys(telling, now)
    (data / TOLD).write_text(json.dumps({key: when.isoformat() for key, when in kept.items()}) + "\n")
    if not telling:
        return ""
    reasons = []
    if count := sum(key.startswith("due") for key in telling):
        reasons.append(f"{count} applications may go on" if count > 1 else "1 application may go on")
    if count := sum(key.startswith("captcha") for key in telling):
        reasons.append(f"{count} CAPTCHAs wait and {said.reason}" if count > 1 else
                       f"1 CAPTCHA waits and {said.reason}")
    return "told the chat: " + "; ".join(reasons)
