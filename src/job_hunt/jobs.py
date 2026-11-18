"""The jobs list, `data/jobs.db`: one row per job, written only by this module.

A job is the same job when its link is, once the tracking parts are removed (`clean_link`), so
an alert that sends a job again never lists it twice. A job moves through the day's states only
along `MOVES`; a pause or a failure carries its reason.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

State = Literal["new", "scored", "picked", "skipped", "resume_ready", "applying", "paused", "applied", "failed"]
STATES: tuple[State, ...] = (
    "new", "scored", "picked", "skipped", "resume_ready", "applying", "paused", "applied", "failed")
MOVES: dict[State, tuple[State, ...]] = {
    "new": ("scored",),
    "scored": ("picked", "skipped"),
    # The user changes their mind about a job they skipped.
    "skipped": ("picked",),
    "picked": ("resume_ready", "failed"),
    "resume_ready": ("applying", "failed"),
    "applying": ("applied", "paused", "failed"),
    "paused": ("applying", "failed"),
    "applied": (),
    "failed": (),
}
WITH_REASON: tuple[State, ...] = ("paused", "failed")
KINDS = ("easy_apply", "company_site")
Remote = Literal["remote", "hybrid", "on_site"]

# Query parameters that only say where a click came from.
TRACKING = re.compile(r"^(utm_.*|trk.*|ref.*|src|source|gh_src|gclid|fbclid|mc_.*)$", re.IGNORECASE)


class MoveError(ValueError):
    """A state change the jobs list does not allow."""


@dataclass(frozen=True)
class FoundJob:
    """One job as the finder's skill read it from an alert email."""

    link: str
    title: str
    company: str
    place: str
    remote: Remote
    board: str
    email_id: str
    email_date: str
    # Which of the settings' areas the place is in, or None.
    area: str | None


@dataclass(frozen=True)
class Job:
    id: int
    link: str
    title: str
    company: str
    place: str | None
    remote: Remote | None
    board: str
    kind: str | None
    email_id: str | None
    email_date: str | None
    area: str | None
    description: str | None
    page_problem: str | None
    score: int | None
    score_reason: str | None
    decision: str | None
    state: State
    state_reason: str | None
    found_at: str
    updated_at: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    place TEXT,
    remote TEXT CHECK (remote IN ('remote', 'hybrid', 'on_site')),
    board TEXT NOT NULL,
    kind TEXT CHECK (kind IN ('easy_apply', 'company_site')),
    email_id TEXT,
    email_date TEXT,
    area TEXT,
    description TEXT,
    page_problem TEXT,
    score INTEGER CHECK (score BETWEEN 1 AND 10),
    score_reason TEXT,
    decision TEXT CHECK (decision IN ('picked', 'skipped')),
    state TEXT NOT NULL CHECK (state IN ('new', 'scored', 'picked', 'skipped', 'resume_ready',
        'applying', 'paused', 'applied', 'failed')),
    state_reason TEXT,
    found_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def clean_link(link: str) -> str:
    parts = urlsplit(link.strip())
    host = parts.netloc.lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        number = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", parts.path)
        current = dict(parse_qsl(parts.query)).get("currentJobId", "")
        job = number.group(1) if number else current if current.isdigit() else None
        if job:
            return f"https://www.linkedin.com/jobs/view/{job}"
    if host == "indeed.com" or host.endswith(".indeed.com"):
        key = dict(parse_qsl(parts.query)).get("jk")
        if key:
            return f"https://{'www.indeed.com' if host == 'indeed.com' else host}/viewjob?jk={key}"
    query = urlencode([(name, value) for name, value in parse_qsl(parts.query) if not TRACKING.match(name)])
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/", query, ""))


class JobsList:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if new:
            path.chmod(0o600)
        self._db.executescript(SCHEMA)

    def _stamp(self) -> str:
        return self._clock().isoformat()

    def has(self, link: str) -> bool:
        return self._db.execute("SELECT 1 FROM jobs WHERE link = ?", (clean_link(link),)).fetchone() is not None

    def add(self, found: FoundJob) -> int | None:
        """Save a new job and return its id, or None when the list already has it."""
        now = self._stamp()
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO jobs (link, title, company, place, remote, board, email_id, email_date, area,"
                " state, found_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)"
                " ON CONFLICT (link) DO NOTHING",
                (clean_link(found.link), found.title, found.company, found.place, found.remote, found.board,
                 found.email_id, found.email_date, found.area, now, now),
            )
        return cursor.lastrowid if cursor.rowcount else None

    def get(self, job: int) -> Job:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job,)).fetchone()
        if row is None:
            raise KeyError(f"no job {job} in the list")
        return Job(**{field.name: row[field.name] for field in fields(Job)})

    def all(self) -> list[Job]:
        return [self.get(row["id"]) for row in self._db.execute("SELECT id FROM jobs ORDER BY id")]

    def save_page(self, job: int, description: str | None, kind: str | None, problem: str | None) -> None:
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
        with self._db:
            self._db.execute(
                "UPDATE jobs SET description = ?, kind = ?, page_problem = ?, updated_at = ? WHERE id = ?",
                (description, kind, problem, self._stamp(), job),
            )

    def score(self, job: int, score: int, reason: str) -> None:
        if type(score) is not int or not 1 <= score <= 10:
            raise ValueError(f"a score is a whole number from 1 to 10, not {score!r}")
        with self._db:
            self._move(job, "scored", None)
            self._db.execute("UPDATE jobs SET score = ?, score_reason = ? WHERE id = ?", (score, reason, job))

    def move(self, job: int, state: State, reason: str | None = None) -> None:
        with self._db:
            self._move(job, state, reason)

    def _move(self, job: int, state: State, reason: str | None) -> None:
        start = self.get(job).state
        if state not in MOVES[start]:
            raise MoveError(f"job {job} cannot move from {start} to {state}")
        if (state in WITH_REASON) != bool(reason):
            raise MoveError(
                f"moving to {state} needs a reason" if state in WITH_REASON else f"moving to {state} takes no reason"
            )
        decision = ", decision = ?" if state in ("picked", "skipped") else ""
        self._db.execute(
            f"UPDATE jobs SET state = ?, state_reason = ?, updated_at = ?{decision} WHERE id = ?",
            (state, reason, self._stamp(), *((state,) if decision else ()), job),
        )

    def last_run(self) -> datetime | None:
        row = self._db.execute("SELECT max(started_at) FROM runs").fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def finished_run(self, started: datetime) -> None:
        """Remember when a finder run that finished started: the next run reads mail from then."""
        with self._db:
            self._db.execute("INSERT INTO runs (started_at) VALUES (?)", (started.isoformat(),))
