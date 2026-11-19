"""What a person looks for, as /setup saved it in `data/settings.json`:

    {"titles": ["QA Automation Engineer"],
     "areas": ["Raleigh-Durham-Chapel Hill", "New York City"],
     "remote": "include",
     "lowest_salary": 120000,
     "morning_hour": 7,
     "summary_hour": 18,
     "working_hours": {"start": 9, "end": 17},
     "time_zone": "America/New_York",
     "linkedin_notes_daily": 15,
     "pushover": {"token": "...", "user": "..."}}

Areas are names as a person says them, never distances; the finder's skill says which of them
a job is in. `remote` is `include` (remote jobs too), `exclude` (no remote jobs) or `only`.
`lowest_salary` may be null. `summary_hour`, the end of the day for `hunt watch`, may be left
out (18). `working_hours`, the hours a recruiter's call may be offered in, may be left out (9 to
17), and so may `time_zone`, their IANA name (the computer's own zone). `linkedin_notes_daily`,
how many notes to hiring managers go out a day (`notes`), may be left out (15); 0 sends none,
and more than 15 is refused, to keep the LinkedIn account safe. `pushover`, the keys `hunt
notify` pushes the chat's questions
with, may be left out; they are never printed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Remote = Literal["include", "exclude", "only"]
REMOTE: tuple[Remote, ...] = ("include", "exclude", "only")
FIELDS = ("titles", "areas", "remote", "lowest_salary", "morning_hour")
OPTIONAL = ("summary_hour", "working_hours", "time_zone", "pushover", "linkedin_notes_daily")
SUMMARY_HOUR = 18
WORKING_HOURS = (9, 17)
NOTES_DAILY = 15


class SettingsError(ValueError):
    """Why the settings cannot be used, in words that say what to change."""


@dataclass(frozen=True)
class Pushover:
    token: str = field(repr=False)
    user: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    titles: list[str]
    areas: list[str]
    remote: Remote
    lowest_salary: int | None
    morning_hour: int
    summary_hour: int = SUMMARY_HOUR
    pushover: Pushover | None = None
    working_hours: tuple[int, int] = WORKING_HOURS
    time_zone: str | None = None
    linkedin_notes_daily: int = NOTES_DAILY


def _names(data: dict[str, Any], field: str) -> list[str]:
    value = data[field]
    if not isinstance(value, list) or not all(isinstance(name, str) and name.strip() for name in value):
        raise SettingsError(f"{field} must be a list of names, none of them empty")
    return [name.strip() for name in value]


def _whole_number(data: dict[str, Any], field: str, low: int, high: int | None = None) -> int:
    value = data[field]
    # bool is an int in Python; `true` is not an hour.
    if type(value) is not int or value < low or (high is not None and value > high):
        bounds = f"from {low} to {high}" if high is not None else f"of {low} or more"
        raise SettingsError(f"{field} must be a whole number {bounds}")
    return value


def _pushover(data: dict[str, Any]) -> Pushover | None:
    keys = data.get("pushover")
    if keys is None:
        return None
    # The message names the fields only: the keys are never repeated.
    if (not isinstance(keys, dict) or set(keys) != {"token", "user"}
            or not all(isinstance(key, str) and key.strip() for key in keys.values())):
        raise SettingsError('pushover must be {"token": ..., "user": ...}, both filled in, or left out')
    return Pushover(token=keys["token"].strip(), user=keys["user"].strip())


def _working_hours(data: dict[str, Any]) -> tuple[int, int]:
    hours = data.get("working_hours")
    if hours is None:
        return WORKING_HOURS
    if not isinstance(hours, dict) or set(hours) != {"start", "end"}:
        raise SettingsError('working_hours must be {"start": 9, "end": 17}, or left out')
    start = _whole_number(hours, "start", 0, 23)
    end = _whole_number(hours, "end", 1, 24)
    if end <= start:
        raise SettingsError(f"working_hours end ({end}) must be later than its start ({start})")
    return start, end


def _time_zone(data: dict[str, Any]) -> str | None:
    name = data.get("time_zone")
    if name is None:
        return None
    try:
        if isinstance(name, str) and name.strip():
            ZoneInfo(name.strip())
            return name.strip()
    except (ZoneInfoNotFoundError, ValueError):
        pass
    raise SettingsError(f"time_zone must be a zone's name such as America/New_York, not {name!r}")


def read_settings(path: Path) -> Settings:
    if not path.exists():
        raise SettingsError(f"{path} is missing: run /setup to make it")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise SettingsError(f"{path} is not JSON: {error}") from error
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must hold one object")
    missing = [field for field in FIELDS if field not in data]
    unknown = sorted(set(data) - set(FIELDS) - set(OPTIONAL))
    if missing:
        raise SettingsError(f"{path} has no {', '.join(missing)}: run /setup to add it")
    if unknown:
        raise SettingsError(f"{path} has fields the hunt does not use: {', '.join(unknown)}")
    titles = _names(data, "titles")
    if not titles:
        raise SettingsError("titles must name at least one job title")
    areas = _names(data, "areas")
    remote = data["remote"]
    if remote not in REMOTE:
        raise SettingsError(f"remote must be one of {', '.join(REMOTE)}, not {remote!r}")
    if not areas and remote == "exclude":
        raise SettingsError("areas is empty and remote is exclude, so no job could be kept")
    lowest_salary = None if data["lowest_salary"] is None else _whole_number(data, "lowest_salary", 0)
    morning_hour = _whole_number(data, "morning_hour", 0, 23)
    summary_hour = _whole_number(data, "summary_hour", 1, 23) if "summary_hour" in data else SUMMARY_HOUR
    if summary_hour <= morning_hour:
        raise SettingsError(f"summary_hour ({summary_hour}) must be later than morning_hour ({morning_hour})")
    notes_daily = (_whole_number(data, "linkedin_notes_daily", 0, NOTES_DAILY) if "linkedin_notes_daily" in data
                   else NOTES_DAILY)
    return Settings(
        titles=titles,
        areas=areas,
        remote=remote,
        lowest_salary=lowest_salary,
        morning_hour=morning_hour,
        summary_hour=summary_hour,
        pushover=_pushover(data),
        working_hours=_working_hours(data),
        time_zone=_time_zone(data),
        linkedin_notes_daily=notes_daily,
    )
