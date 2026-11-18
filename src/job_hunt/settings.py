"""What a person looks for, as /setup saved it in `data/settings.json`:

    {"titles": ["QA Automation Engineer"],
     "areas": ["Raleigh-Durham-Chapel Hill", "New York City"],
     "remote": "include",
     "lowest_salary": 120000,
     "morning_hour": 7}

Areas are names as a person says them, never distances; the finder's skill says which of them
a job is in. `remote` is `include` (remote jobs too), `exclude` (no remote jobs) or `only`.
`lowest_salary` may be null.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Remote = Literal["include", "exclude", "only"]
REMOTE: tuple[Remote, ...] = ("include", "exclude", "only")
FIELDS = ("titles", "areas", "remote", "lowest_salary", "morning_hour")


class SettingsError(ValueError):
    """Why the settings cannot be used, in words that say what to change."""


@dataclass(frozen=True)
class Settings:
    titles: list[str]
    areas: list[str]
    remote: Remote
    lowest_salary: int | None
    morning_hour: int


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
    unknown = sorted(set(data) - set(FIELDS))
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
    return Settings(
        titles=titles,
        areas=areas,
        remote=remote,
        lowest_salary=lowest_salary,
        morning_hour=_whole_number(data, "morning_hour", 0, 23),
    )
