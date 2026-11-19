"""Offering times for a recruiter's call, from the person's Google Calendar.

A thread that asks to talk is drafted by the `offer-times` skill instead of `draft-reply`: one
`claude -p` with the Calendar connector's read tool reads the events of the next five working
days and writes, beside the reply offering two or three free times,

    {"busy": [{"start": "<ISO time or date>", "end": "..."}], "times": ["<ISO time>", ...],
     "reply": "...", "draft_id": "..." | null}

The free times are worked out here again from `busy`, inside the working hours of
`data/settings.json` and in the person's time zone, as half-hour calls starting on the hour or
the half hour; a run that offers a time that is not free is a failed draft.

Each offer is a talk, kept in `data/jobs.db` beside the threads and written only by this module.
A talk keeps the reply it was made for: the thread's reply is saved apart, and a talk whose reply
is not the thread's (a run stopped between the two) is not that reply's, so nothing is held for it.
On the person's yes to the reply, the first time offered is added to the calendar as held (one
`claude -p` with the connector's create tool); when the recruiter's answer names one of the times
offered, the held event is moved to it and marked confirmed (its update tool). No event is
created or moved otherwise.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from job_hunt.settings import Settings

SKILL = "offer-times"
# Choosing times and writing in the person's voice is the drafting worker's work; adding an
# event to the calendar is not.
OFFER_MODEL = "claude-sonnet-5-5"
MODEL = "claude-haiku-5-5"
LENGTH = timedelta(minutes=30)
DAYS = 5
MOST = 3
TRIES = 2
CALENDAR = "mcp__claude_ai_Google_Calendar__"
READ_TOOLS = (f"{CALENDAR}list_events",)
HOLD_TOOLS = (f"{CALENDAR}create_event",)
CONFIRM_TOOLS = (f"{CALENDAR}update_event",)
UNSURE = "a look in Google Calendar whether it was added"

State = Literal["offered", "holding", "held", "confirmed", "failed", "unsure", "replaced"]
Run = Callable[[list[str]], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS talks (
    thread INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    conversation TEXT NOT NULL,
    title TEXT NOT NULL,
    offered TEXT NOT NULL,
    reply TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('offered', 'holding', 'held', 'confirmed', 'failed', 'unsure',
        'replaced')),
    state_reason TEXT,
    event_id TEXT,
    start TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


class TimesError(RuntimeError):
    """An offer or a calendar step that failed, in words for the person."""


def zone(settings: Settings) -> tzinfo:
    """The person's time zone: the one of the settings, else the computer's own (TZ first, as the
    rest of `hunt` reads the local time)."""
    if settings.time_zone:
        return ZoneInfo(settings.time_zone)
    if name := os.environ.get("TZ", "").removeprefix(":"):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # /etc/localtime links to the zone's file: its name keeps summer time right across the days.
    local = Path("/etc/localtime").resolve()
    if "zoneinfo" in local.parts:
        try:
            return ZoneInfo("/".join(local.parts[local.parts.index("zoneinfo") + 1:]))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or UTC


def working_days(now: datetime, where: tzinfo, hours: tuple[int, int], days: int = DAYS) -> list[tuple[datetime, datetime]]:
    """The working hours of the next `days` working days, Monday to Friday, from tomorrow on:
    a call later today is too short a notice."""
    found: list[tuple[datetime, datetime]] = []
    day = now.astimezone(where).date()
    while len(found) < days:
        day += timedelta(days=1)
        if day.weekday() < 5:
            found.append((_at(day, hours[0], where), _at(day, hours[1], where)))
    return found


def _at(day: date, hour: int, where: tzinfo) -> datetime:
    # The end hour may be 24, the next midnight.
    return datetime.combine(day, time(), where) + timedelta(hours=hour)


def free_times(busy: list[tuple[datetime, datetime]], days: list[tuple[datetime, datetime]]) -> list[datetime]:
    """The starts of the half-hour calls, on the hour or the half hour, that no busy time touches."""
    free: list[datetime] = []
    for start, end in days:
        when = start
        while when + LENGTH <= end:
            if not any(taken_from < when + LENGTH and taken_to > when for taken_from, taken_to in busy):
                free.append(when)
            when += LENGTH
    return free


def _instant(text: Any, where: tzinfo) -> datetime | None:
    """An ISO time, read in the person's zone when it gives none."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return when.replace(tzinfo=where) if when.tzinfo is None else when


def read_busy(entries: Any, where: tzinfo) -> list[tuple[datetime, datetime]]:
    """The busy times a run read; a whole day's event is given as dates, its end the day after."""
    if not isinstance(entries, list):
        raise TimesError(f'the {SKILL} run wrote no list of "busy" times')
    busy = []
    for number, entry in enumerate(entries, start=1):
        start = entry.get("start") if isinstance(entry, dict) else None
        end = entry.get("end") if isinstance(entry, dict) else None
        if isinstance(start, str) and isinstance(end, str) and len(start.strip()) == len(end.strip()) == 10:
            try:
                busy.append((datetime.combine(date.fromisoformat(start.strip()), time(), where),
                             datetime.combine(date.fromisoformat(end.strip()), time(), where)))
                continue
            except ValueError:
                pass
        begins, ends = _instant(start, where), _instant(end, where)
        if begins is None or ends is None or ends <= begins:
            raise TimesError(f"the {SKILL} run wrote busy time {number} with no start and end it can be read by")
        busy.append((begins, ends))
    return busy


@dataclass(frozen=True)
class Offer:
    times: list[datetime]
    reply: str
    draft_id: str | None


def read_offer(path: Path, days: list[tuple[datetime, datetime]], where: tzinfo) -> Offer:
    """The offer a run wrote, its times checked against the calendar it read."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise TimesError(f"the {SKILL} run wrote no offer") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TimesError(f"the {SKILL} run wrote an offer that is not JSON: {error}") from error
    if not isinstance(data, dict):
        raise TimesError(f"the {SKILL} run wrote an offer that is not one object")
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise TimesError(f'the {SKILL} run wrote no "reply"')
    free = free_times(read_busy(data.get("busy"), where), days)
    given = data.get("times")
    if not isinstance(given, list):
        raise TimesError(f'the {SKILL} run wrote no list of "times"')
    times = [_instant(each, where) for each in given]
    if any(when is None for when in times):
        raise TimesError(f"the {SKILL} run wrote a time it cannot be read by: {given}")
    taken = [when for when in times if when not in free]
    if taken:
        raise TimesError(f"the {SKILL} run offered {', '.join(when.isoformat() for when in taken)}, "
                         "which is not a free half hour of the working days")
    least = min(2, len(free))
    if not least <= len(times) <= MOST or len(set(times)) != len(times):
        raise TimesError(f"the {SKILL} run offered {len(times)} different times, not {least} to {MOST}")
    draft = data.get("draft_id")
    return Offer(times=[when.astimezone(where) for when in times], reply=reply.strip(),
                 draft_id=draft.strip() or None if isinstance(draft, str) else None)


def _rule(path: Path) -> str:
    # `//` starts an absolute path in a permission rule.
    return f"/{path}"


def _local(when: datetime, where: tzinfo) -> str:
    # The connector reads a time with no offset in the zone it is given.
    return when.astimezone(where).replace(tzinfo=None).isoformat(timespec="minutes")


def zone_name(where: tzinfo) -> str:
    return getattr(where, "key", None) or str(where)


def offer_command(arguments: dict[str, Any], days: list[tuple[datetime, datetime]], where: tzinfo,
                  reads: list[str], gmail: tuple[str, ...]) -> list[str]:
    """The `offer` run: `arguments` are the drafting run's own (message, posting, career, out…)."""
    window = [{"day": start.date().isoformat(), "from": start.strftime("%H:%M"),
               "to": "24:00" if end.date() != start.date() else end.strftime("%H:%M")} for start, end in days]
    task = {"task": "offer", **arguments, "time_zone": zone_name(where), "days": window,
            "minutes": int(LENGTH.total_seconds() // 60)}
    out = Path(arguments["out"])
    tools = [*reads, f"Edit({_rule(out)})", *READ_TOOLS, *gmail]
    return ["claude", "-p", f"/{SKILL} {json.dumps(task, ensure_ascii=False)}", "--model", OFFER_MODEL,
            "--allowedTools", ",".join(tools)]


def event_command(task: Literal["hold", "confirm"], talk: Talk, start: datetime, where: tzinfo, out: Path,
                  move: str | None = None) -> list[str]:
    """Hold a time, or confirm it; `move`, the id of a held event to move instead of adding one."""
    arguments: dict[str, Any] = {"task": task, "title": event_title(talk.title, task), "start": _local(start, where),
                                 "end": _local(start + LENGTH, where), "time_zone": zone_name(where), "out": str(out)}
    if task == "confirm":
        arguments["event_id"] = talk.event_id
    elif move is not None:
        arguments["event_id"] = move
    tools = HOLD_TOOLS if task == "hold" and move is None else CONFIRM_TOOLS
    return ["claude", "-p", f"/{SKILL} {json.dumps(arguments, ensure_ascii=False)}", "--model", MODEL,
            "--allowedTools", ",".join([*tools, f"Edit({_rule(out)})"])]


def event_title(title: str, task: Literal["hold", "confirm"]) -> str:
    return f"Held: {title}" if task == "hold" else title


def read_event(path: Path) -> str | None:
    """The event's id when the run reports it done; TimesError with the reason when it failed;
    None when it wrote no report it can be read by, and the event may have been made."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    result = data.get("result") if isinstance(data, dict) else None
    if result == "done" and isinstance(data.get("event_id"), str) and data["event_id"].strip():
        return data["event_id"].strip()
    if result == "failed" and isinstance(data.get("reason"), str) and data["reason"].strip():
        raise TimesError(" ".join(data["reason"].split()))
    return None


# The talks


@dataclass(frozen=True)
class Talk:
    thread: int
    source: str
    conversation: str
    title: str
    offered: str
    reply: str
    state: State
    state_reason: str | None
    event_id: str | None
    start: str | None
    failures: int
    updated_at: str

    def times(self) -> list[datetime]:
        return [datetime.fromisoformat(each) for each in json.loads(self.offered)]


def _now() -> datetime:
    return datetime.now(UTC)


class Talks:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if new:
            path.chmod(0o600)
        self._db.executescript(SCHEMA)

    def offered(self, thread: int, source: str, conversation: str, title: str, times: list[datetime],
                reply: str) -> None:
        """Keep the times a reply offers; an offer made again before the yes replaces them, and an
        offer of no time (the calendar was full) forgets them."""
        if not times:
            self.forget(thread)
            return
        with self._db:
            self._db.execute(
                "INSERT INTO talks (thread, source, conversation, title, offered, reply, state, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'offered', ?) ON CONFLICT (thread) DO UPDATE SET"
                " title = excluded.title, offered = excluded.offered, reply = excluded.reply,"
                " updated_at = excluded.updated_at WHERE state = 'offered'",
                (thread, source, conversation, title, json.dumps([when.isoformat() for when in times]), reply,
                 self._clock().isoformat()))

    def forget(self, thread: int) -> None:
        """Drop an offer not yet held: the reply that offered it was changed by hand."""
        with self._db:
            self._db.execute("DELETE FROM talks WHERE thread = ? AND state = 'offered'", (thread,))

    def claim(self, thread: int) -> bool:
        """Mark an offer `holding`, unless another run took it first."""
        with self._db:
            return self._db.execute("UPDATE talks SET state = 'holding', updated_at = ? WHERE thread = ?"
                                    " AND state = 'offered'", (self._clock().isoformat(), thread)).rowcount == 1

    def get(self, thread: int) -> Talk | None:
        row = self._db.execute("SELECT * FROM talks WHERE thread = ?", (thread,)).fetchone()
        return None if row is None else Talk(**{field.name: row[field.name] for field in fields(Talk)})

    def of_reply(self, thread: int, reply: str | None) -> Talk | None:
        """The offer this reply makes: none when the talk was made for another reply."""
        talk = self.get(thread)
        return talk if talk is not None and talk.reply == reply else None

    def in_states(self, *states: State) -> list[Talk]:
        marks = ", ".join("?" * len(states))
        rows = self._db.execute(f"SELECT thread FROM talks WHERE state IN ({marks}) ORDER BY thread", states)
        return [talk for row in rows if (talk := self.get(row["thread"])) is not None]

    def booked_in(self, source: str, conversation: str, now: datetime) -> Talk | None:
        """The latest call of a conversation whose time went to the calendar, or may have, and is
        still to come: its next message is not offered times again, and may confirm it while it
        is `held`, until the last time offered: the recruiter may pick any of them. A call that is
        over leaves the next round to be offered anew."""
        row = self._db.execute(
            "SELECT thread FROM talks WHERE source = ? AND conversation = ?"
            " AND state IN ('holding', 'held', 'confirmed', 'unsure') ORDER BY thread DESC LIMIT 1",
            (source, conversation)).fetchone()
        talk = None if row is None else self.get(row["thread"])
        if talk is None:
            return None
        # A held event sits at the first time offered; only a confirmed one is at the call's time.
        last = datetime.fromisoformat(talk.start) if talk.state == "confirmed" and talk.start else max(talk.times())
        return talk if last + LENGTH > now else None

    def held_before(self, talk: Talk) -> Talk | None:
        """An earlier offer of the same conversation still held: a new offer moves its event."""
        row = self._db.execute(
            "SELECT thread FROM talks WHERE source = ? AND conversation = ? AND state = 'held' AND thread < ?"
            " ORDER BY thread DESC LIMIT 1", (talk.source, talk.conversation, talk.thread)).fetchone()
        return None if row is None else self.get(row["thread"])

    def _set(self, thread: int, **values: object) -> None:
        names = ", ".join(f"{name} = ?" for name in values)
        with self._db:
            self._db.execute(f"UPDATE talks SET {names}, updated_at = ? WHERE thread = ?",
                             (*values.values(), self._clock().isoformat(), thread))

    def move(self, thread: int, state: State, reason: str | None = None, **values: object) -> None:
        self._set(thread, state=state, state_reason=reason, **values)

    def fail(self, thread: int, reason: str) -> State:
        """Count a failed hold: tried again at the next run, then `failed`."""
        talk = self.get(thread)
        failures = (talk.failures if talk else 0) + 1
        self.move(thread, "failed" if failures >= TRIES else "offered", reason, failures=failures)
        return "failed" if failures >= TRIES else "offered"


# The calendar steps


def hold(talks: Talks, thread: int, where: tzinfo, out: Path, run: Run) -> Talk:
    """Add the first time offered to the calendar as held, once: the person said yes to the reply."""
    talk = talks.get(thread)
    if talk is None or not talks.claim(thread):
        raise TimesError(f"thread {thread} has no offer waiting to be held")
    start = talk.times()[0]
    earlier = talks.held_before(talk)
    out.unlink(missing_ok=True)
    try:
        run(event_command("hold", talk, start, where, out, move=earlier.event_id if earlier else None))
    except Exception as error:
        # A run that failed may still have added it: never added a second time on its own.
        talks.move(thread, "unsure", f"{UNSURE}: {error}")
        return talks.get(thread) or talk
    except BaseException:
        talks.move(thread, "unsure", f"{UNSURE}: the run stopped while adding it")
        raise
    try:
        event = read_event(out)
    except TimesError as error:
        talks.fail(thread, str(error))
        raise
    if event is None:
        talks.move(thread, "unsure", f"{UNSURE}: the {SKILL} run wrote no report it can be read by")
    else:
        talks.move(thread, "held", event_id=event, start=start.isoformat())
        if earlier is not None:
            talks.move(earlier.thread, "replaced", f"moved to the offer of thread {thread}")
    return talks.get(thread) or talk


def confirm(talks: Talks, talk: Talk, start: datetime, where: tzinfo, out: Path, run: Run) -> Talk:
    """Move the held event to the time the recruiter chose among those offered, and confirm it."""
    if start not in talk.times():
        raise TimesError(f"{start.isoformat()} is not one of the times offered")
    out.unlink(missing_ok=True)
    run(event_command("confirm", talk, start, where, out))
    if read_event(out) is None:
        raise TimesError(f"the {SKILL} run wrote no report it can be read by")
    talks.move(talk.thread, "confirmed", start=start.astimezone(where).isoformat())
    return talks.get(talk.thread) or talk
