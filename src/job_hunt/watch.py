"""`hunt watch`: a recruiter gets an answer within the hour.

A timer runs it every 20 minutes from the morning hour to the summary hour (`timer`); outside
those hours it does nothing. Each run is one small `claude -p` on Claude Haiku with only the
Gmail connector's read tools (the `read-job-alerts` skill, asked for messages and no alerts):
it notes new recruiter emails and counts LinkedIn's own emails about a new message. LinkedIn
is opened (`hunt linkedin-messages`) only when one of those came, and besides twice a day, in
the morning and at mid-day, in case a notification email was missed. A run with nothing new
ends there and says nothing. A new thread is drafted at once, and the chat is told once, so it
asks the person (`tell_session`).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from job_hunt.finder import skill_command
from job_hunt.jobs import JobsList
from job_hunt.morning import SESSION, TARGET, start_session, tmux
from job_hunt.replies import EmailMessages, ReplyError, Thread, Threads
from job_hunt.settings import Settings

TELL = ("A recruiter's message waits for your answer. Run `uv run hunt questions` and ask what is new, "
        "as CLAUDE.md says.")

Run = Callable[[list[str]], None]


def _local_now() -> datetime:
    return datetime.now().astimezone()


def in_hours(now: datetime, settings: Settings) -> bool:
    return settings.morning_hour <= now.hour < settings.summary_hour


def _at(now: datetime, hour: int) -> datetime:
    return now.replace(hour=hour, minute=0, second=0, microsecond=0)


def linkedin_due(last: datetime | None, now: datetime, settings: Settings) -> bool:
    """Whether LinkedIn is read without a notification: once from the morning hour, once from mid-day."""
    midday = (settings.morning_hour + settings.summary_hour) // 2
    since = _at(now, midday) if now.hour >= midday else _at(now, settings.morning_hour)
    return last is None or last < since


def watch(
    threads: Threads,
    jobs: JobsList,
    data: Path,
    settings: Settings,
    *,
    check: Run,
    read_linkedin: Callable[[], int],
    draft: Callable[[], int],
    tell: Callable[[list[Thread]], None],
    clock: Callable[[], datetime] = _local_now,
) -> str:
    """One run; return what to print ("" when nothing was new)."""
    now = clock()
    if not in_hours(now, settings):
        return ""
    data.mkdir(parents=True, exist_ok=True)
    # One run at a time: a run still waiting for the LinkedIn lock is not started twice.
    with (data / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ""
        return _watch(threads, jobs, data, settings, check, read_linkedin, draft, tell, now)


def _watch(threads: Threads, jobs: JobsList, data: Path, settings: Settings, check: Run, read_linkedin: Callable[[], int],
           draft: Callable[[], int], tell: Callable[[list[Thread]], None], now: datetime) -> str:
    out = data / "messages-check.json"
    emails = EmailMessages(threads, jobs)
    out.unlink(missing_ok=True)
    check(skill_command(None, [], out, emails.since(now)))
    try:
        found = json.loads(out.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReplyError(f"the messages check wrote no file it can be read by ({out})") from error
    problems = emails.saved(found, now)
    new = emails.new
    if emails.notifications or linkedin_due(threads.last_read("linkedin"), now, settings):
        new += read_linkedin()
    if not new and not threads.in_states("new") and not threads.unasked():
        return "; ".join(problems)
    drafted = draft()
    told = threads.unasked()
    if told:
        tell(told)
        for thread in told:
            threads.asked(thread.id)
    said = f"{new} new messages, {drafted} replies drafted, {len(told)} asked"
    return "; ".join([said, *problems])


def tell_session(root: Path, *, run: Callable[[list[str]], subprocess.CompletedProcess[str]] = tmux,
                 start: Callable[[Path], None] = start_session) -> None:
    """Have the chat ask: typed into today's session, or a session started when none runs."""
    if run(["tmux", "has-session", "-t", TARGET]).returncode != 0:
        start(root)
        return
    pane = f"{TARGET}:"
    typed = run(["tmux", "send-keys", "-t", pane, "-l", TELL])
    if typed.returncode != 0:
        raise ReplyError(f"could not type into the {SESSION} session: {typed.stderr.strip() or 'no message'}")
    run(["tmux", "send-keys", "-t", pane, "Enter"])
