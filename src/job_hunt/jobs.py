"""The jobs list, `data/jobs.db`: one row per job, written only by this module.

A job is the same job when its link is, once the tracking parts are removed (`clean_link`), so
an alert that sends a job again never lists it twice. The address its page ended at, after the
redirects of a click-tracking link, is its second link (`page_link`) and counts the same when
the title and company are the same too. A job moves through the day's states only
along `MOVES`; a pause or a failure carries its reason. A worker that fails counts against its
job (`fail`): the first time the job is tried again, the second time it is `failed`.

Each job keeps its source (`SOURCES`): an alert email, LinkedIn's search, one of the job-board
systems or one of the public job feeds; each finder run of a source is kept, so the next run of that source reads from when it
started.

A job paused for a CAPTCHA or a sign-in is tried again once on its own (`retry`): back to
`resume_ready` with a time before which no `hunt apply` takes it (`not_before`, which any later
move clears). A job still on a CAPTCHA after that is asked about in one question with the others
(`asked`), at most `CAPTCHA_ASKS` times. Each start of a worker is kept with the site's host
(`started_on`), `linkedin.com` for the LinkedIn worker's, for the spacing and the daily limits of
`apply`. A pick keeps its time (`picked_at`): a job picked on an earlier day goes first.

A picked job not started yet may still be skipped: a resume written for it is never used.

The list the user picks from is numbered when it is shown (`show`), and the numbers are kept
in the file, so "pick 2" names the second job of the last list shown, also after more jobs
were scored.
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
    # To skipped: the user leaves out a job picked for them before it starts.
    "picked": ("resume_ready", "failed", "skipped"),
    "resume_ready": ("applying", "failed", "skipped"),
    # Back to resume_ready: a worker failed for the first time, and is tried again, or an Easy
    # Apply job turned out to apply on the company's site (`to_company_site`).
    "applying": ("applied", "paused", "failed", "resume_ready"),
    # Back to resume_ready: the user answered the question or did what the site needed. To
    # applied: the user saw in Chrome that the application was sent.
    "paused": ("applying", "failed", "resume_ready", "applied"),
    "applied": (),
    "failed": (),
}
WITH_REASON: tuple[State, ...] = ("paused", "failed")
# A job whose workers failed this many times is `failed`.
TRIES = 2
# A job still on a CAPTCHA after this many questions is `failed`.
CAPTCHA_ASKS = 2
KINDS = ("easy_apply", "company_site")
SOURCES = ("alert", "linkedin_search", "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable",
           "remotive", "remoteok", "arbeitnow", "hn_hiring", "usajobs", "adzuna", "jooble")
Remote = Literal["remote", "hybrid", "on_site"]

# Query parameters that only say where a click came from. Named one by one: a parameter such
# as `reference` or `gh_jid` names the job itself.
TRACKING = re.compile(
    r"^(utm_.*|trk.*|trackingid|ref|refid|referrer|src|source|gh_src|gclid|fbclid|mc_cid|mc_eid)$", re.IGNORECASE
)


class MoveError(ValueError):
    """A state change the jobs list does not allow."""


class PickError(ValueError):
    """A pick or skip the last list shown cannot take, in words for the user."""


@dataclass(frozen=True)
class FoundJob:
    """One job as the finder's skill read it from an alert email, or as a search found it."""

    link: str
    title: str
    company: str
    place: str
    remote: Remote
    board: str
    # The alert email the job came in; None for a job a search found.
    email_id: str | None
    email_date: str | None
    # Which of the settings' areas the place is in, or None.
    area: str | None
    source: str = "alert"


@dataclass(frozen=True)
class Job:
    id: int
    link: str
    page_link: str | None
    title: str
    company: str
    place: str | None
    remote: Remote | None
    board: str
    kind: str | None
    # Where a company-site worker applies, when it is not `link`: the company's site that an
    # Easy Apply job's LinkedIn page sent to.
    apply_link: str | None
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
    failures: int
    # The tries again of a CAPTCHA or sign-in pause spent (`retry`), and the time before which
    # no `hunt apply` takes the job.
    retries: int
    not_before: str | None
    # The questions a CAPTCHA pause of the job was in, and whether its pause now was in one.
    asks: int
    asked: int
    source: str
    found_at: str
    updated_at: str
    # When the job was last picked; None for one picked before picks kept their time.
    picked_at: str | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,
    page_link TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    place TEXT,
    remote TEXT CHECK (remote IN ('remote', 'hybrid', 'on_site')),
    board TEXT NOT NULL,
    kind TEXT CHECK (kind IN ('easy_apply', 'company_site')),
    apply_link TEXT,
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
    failures INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    not_before TEXT,
    asks INTEGER NOT NULL DEFAULT 0,
    asked INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'alert',
    found_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    picked_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_page_link ON jobs (page_link);
CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'alert'
);
CREATE TABLE IF NOT EXISTS shown (
    number INTEGER PRIMARY KEY,
    job INTEGER NOT NULL REFERENCES jobs (id)
);
CREATE TABLE IF NOT EXISTS lists (
    shown_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_starts (
    host TEXT NOT NULL,
    job INTEGER NOT NULL,
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
    # "#/jobs/123" is a route of the page and names the job; "#apply" only scrolls.
    route, _, route_query = parts.fragment.partition("?") if parts.fragment.startswith(("/", "!")) else ("", "", "")
    route_query = _untracked(route_query)
    fragment = f"{route}?{route_query}" if route_query else route
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/") or "/", _untracked(parts.query), fragment))


def _untracked(query: str) -> str:
    return urlencode([(name, value) for name, value in parse_qsl(query) if not TRACKING.match(name)])


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
        # A file made before failures were counted, before apply links or before sources, gains
        # the column; its jobs and runs were the alerts'.
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
        runs = {row["name"] for row in self._db.execute("PRAGMA table_info(runs)")}
        with self._db:
            if "failures" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")
            if "apply_link" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN apply_link TEXT")
            if "source" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'alert'")
            for column, kind in (("retries", "INTEGER NOT NULL DEFAULT 0"), ("not_before", "TEXT"),
                                 ("asks", "INTEGER NOT NULL DEFAULT 0"), ("asked", "INTEGER NOT NULL DEFAULT 0"),
                                 ("picked_at", "TEXT")):
                if column not in columns:
                    self._db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
            if "source" not in runs:
                self._db.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'alert'")

    def now(self) -> datetime:
        return self._clock()

    def _stamp(self) -> str:
        return self._clock().isoformat()

    def has(self, link: str) -> bool:
        link = clean_link(link)
        return self._db.execute("SELECT 1 FROM jobs WHERE ? IN (link, page_link)", (link,)).fetchone() is not None

    def known_links(self) -> set[str]:
        """Every link and page link of the list, for the searches that run beside this connection."""
        rows = self._db.execute("SELECT link FROM jobs UNION SELECT page_link FROM jobs WHERE page_link IS NOT NULL")
        return {row[0] for row in rows}

    def add(
        self,
        found: FoundJob,
        *,
        page_link: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        problem: str | None = None,
    ) -> int | None:
        """Save a new job with what its page gave, and return its id, or None when the list
        already has it by either link."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
        if found.source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}, not {found.source!r}")
        link = clean_link(found.link)
        page = clean_link(page_link) if page_link else None
        page = None if page == link else page
        if self.has(link):
            return None
        # Pages of two jobs may end at one page (an expired posting sends its link to the
        # careers page): the same page is the same job only under the same title and company.
        # Each job keeps its page, so a later link to any of them is known.
        if page and any(
            (known["title"].casefold(), known["company"].casefold()) == (found.title.casefold(), found.company.casefold())
            for known in self._db.execute("SELECT title, company FROM jobs WHERE ? IN (link, page_link)", (page,))
        ):
            return None
        now = self._stamp()
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO jobs (link, page_link, title, company, place, remote, board, email_id, email_date,"
                " area, description, kind, page_problem, source, state, found_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                (link, page, found.title, found.company, found.place, found.remote, found.board, found.email_id,
                 found.email_date, found.area, description, kind, problem, found.source, now, now),
            )
        return cursor.lastrowid

    def get(self, job: int) -> Job:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job,)).fetchone()
        if row is None:
            raise KeyError(f"no job {job} in the list")
        return Job(**{field.name: row[field.name] for field in fields(Job)})

    def all(self) -> list[Job]:
        return [self.get(row["id"]) for row in self._db.execute("SELECT id FROM jobs ORDER BY id")]

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
        # Bound, not `picked_at = updated_at`, which reads the row as it was before this update.
        decision += ", picked_at = ?" if state == "picked" else ""
        now = self._stamp()
        # Only from the state read: another connection (`hunt skip` from the chat, a run starting
        # the job) may have moved it between that read and this write.
        moved = self._db.execute(
            f"UPDATE jobs SET state = ?, state_reason = ?, updated_at = ?, not_before = NULL, asked = 0{decision}"
            " WHERE id = ? AND state = ?",
            (state, reason, now, *((state,) if state in ("picked", "skipped") else ()),
             *((now,) if state == "picked" else ()), job, start),
        )
        if moved.rowcount != 1:
            raise MoveError(f"job {job} moved meanwhile from {start}, to {self.get(job).state}")

    def fail(self, job: int, reason: str) -> State:
        """Count a worker's failure against the job and return its state now: `failed` with the
        reason once it has failed `TRIES` times, otherwise where it is tried again from."""
        with self._db:
            failures = self.get(job).failures + 1
            self._db.execute("UPDATE jobs SET failures = ? WHERE id = ?", (failures, job))
            start = self.get(job).state
            if failures >= TRIES:
                self._move(job, "failed", reason)
            elif start == "applying":
                self._move(job, "resume_ready", None)
            elif start != "picked":
                raise MoveError(f"job {job} is {start}: no worker of it can fail")
        return self.get(job).state

    def retry(self, job: int, not_before: datetime) -> None:
        """Try a job paused by a CAPTCHA or a sign-in again on its own, once, from `not_before` on."""
        with self._db:
            if self.get(job).retries:
                raise MoveError(f"job {job} was tried again on its own already")
            self._move(job, "resume_ready", None)
            self._db.execute("UPDATE jobs SET retries = retries + 1, not_before = ? WHERE id = ?",
                             (not_before.isoformat(), job))

    def later(self, job: int, not_before: datetime) -> None:
        """Leave a job ready to apply to for a `hunt apply` from `not_before` on."""
        with self._db:
            if self.get(job).state != "resume_ready":
                raise MoveError(f"job {job} is not ready to apply to")
            self._db.execute("UPDATE jobs SET not_before = ? WHERE id = ?", (not_before.isoformat(), job))

    def due(self, job: Job) -> bool:
        """Whether a `hunt apply` may take the job now: its `not_before`, if any, has come."""
        return job.not_before is None or datetime.fromisoformat(job.not_before) <= self.now()

    def asked(self, job: int) -> None:
        """Count the question a CAPTCHA pause of the job is shown in, once a pause."""
        with self._db:
            self._db.execute("UPDATE jobs SET asks = asks + 1, asked = 1 WHERE id = ? AND asked = 0", (job,))

    def started_on(self, host: str, job: int) -> None:
        """Keep that a company-site worker started on this job, on this host, now."""
        with self._db:
            self._db.execute("INSERT INTO site_starts (host, job, started_at) VALUES (?, ?, ?)",
                             (host, job, self._stamp()))

    def starts_on(self, host: str, since: datetime, but: int) -> dict[int, datetime]:
        """The other jobs started on this host since a time, each with its latest start: a job's
        own try again is the same application."""
        rows = self._db.execute("SELECT job, max(started_at) FROM site_starts WHERE host = ? AND job != ? GROUP BY job",
                                (host, but))
        return {row[0]: when for row in rows if (when := datetime.fromisoformat(row[1])) >= since}

    def started_since(self, since: datetime) -> dict[int, datetime]:
        """Every job a worker started on since a time, on any host, each with its latest start."""
        rows = self._db.execute("SELECT job, max(started_at) FROM site_starts GROUP BY job")
        return {row[0]: when for row in rows if (when := datetime.fromisoformat(row[1])) >= since}

    def to_company_site(self, job: int, link: str) -> None:
        """A job whose LinkedIn page sends to the company's site: from now on a company-site job
        applied to at `link`, back to `resume_ready` with no try spent."""
        with self._db:
            if (earlier := self.get(job).apply_link) is not None:
                raise MoveError(f"job {job} already applies on the company's site, at {earlier}")
            self._move(job, "resume_ready", None)
            self._db.execute("UPDATE jobs SET kind = 'company_site', apply_link = ? WHERE id = ?", (link, job))

    def in_states(self, *states: State) -> list[Job]:
        """The jobs in these states, in the order found."""
        marks = ", ".join("?" * len(states))
        rows = self._db.execute(f"SELECT id FROM jobs WHERE state IN ({marks}) ORDER BY id", states)
        return [self.get(row["id"]) for row in rows]

    def waiting(self) -> list[Job]:
        """The scored jobs not yet decided, best first; equal scores in the order found."""
        rows = self._db.execute("SELECT id FROM jobs WHERE state = 'scored' ORDER BY score DESC, id")
        return [self.get(row["id"]) for row in rows]

    def show(self, listed: list[Job] | None = None) -> list[Job]:
        """The jobs waiting for a pick, or the jobs given, numbered from 1 in this order from now on."""
        waiting = self.waiting() if listed is None else listed
        with self._db:
            self._db.execute("DELETE FROM shown")
            self._db.executemany("INSERT INTO shown (number, job) VALUES (?, ?)",
                                 [(number, job.id) for number, job in enumerate(waiting, start=1)])
            self._db.execute("DELETE FROM lists")
            self._db.execute("INSERT INTO lists (shown_at) VALUES (?)", (self._stamp(),))
        return waiting

    def _shown(self) -> dict[int, int]:
        if self._db.execute("SELECT 1 FROM lists").fetchone() is None:
            raise PickError("no list has been shown yet: run hunt list")
        return {row["number"]: row["job"] for row in self._db.execute("SELECT number, job FROM shown")}

    def last_list(self) -> dict[int, int]:
        """The numbers of the last list shown and their jobs; empty when none was shown."""
        return {row["number"]: row["job"] for row in self._db.execute("SELECT number, job FROM shown")}

    def numbers_above(self, score: int) -> list[int]:
        """The numbers of the last list whose jobs still wait for a pick and score more than `score`."""
        return self._above(self._shown(), score)

    def _above(self, shown: dict[int, int], score: int) -> list[int]:
        return [number for number, job in sorted(shown.items())
                if (each := self.get(job)).state == "scored" and each.score is not None and each.score > score]

    def decide(
        self, numbers: list[int], decision: Literal["picked", "skipped"], *, above: int | None = None
    ) -> list[tuple[int, Job]]:
        """Pick or skip the jobs of these numbers of the last list, all of them or, with one
        refused, none; each number decided once, with its job as it is now. With `above`, the
        numbers are those of `numbers_above`, read from the same list in the same transaction."""
        with self._db:
            # The list is read and the decisions recorded under one write lock, so a list shown
            # by another session in between cannot change what the numbers name.
            self._db.execute("BEGIN IMMEDIATE")
            return self._decide(numbers, decision, above)

    def _decide(
        self, numbers: list[int], decision: Literal["picked", "skipped"], above: int | None
    ) -> list[tuple[int, Job]]:
        shown = self._shown()
        if above is not None:
            numbers = self._above(shown, above)
        elif not shown:
            raise PickError("the last list was empty")
        chosen: list[int] = []
        for number in numbers:
            if number not in shown:
                raise PickError(f"there is no {number} on the last list; it has 1 to {len(shown)}")
            job = self.get(shown[number])
            # A skipped job may still be picked, and a pick not started yet skipped; a pick that
            # started, however far it went, stays a pick.
            if job.state != "scored" and not (job.state == "skipped" and decision == "picked") and not (
                    job.state in ("picked", "resume_ready") and decision == "skipped"):
                raise PickError(f"{number} ({job.title} at {job.company}) is already {job.decision or job.state}")
            if number not in chosen:
                chosen.append(number)
        for number in chosen:
            self._move(shown[number], decision, None)
        return [(number, self.get(shown[number])) for number in chosen]

    def last_run(self, source: str = "alert") -> datetime | None:
        row = self._db.execute("SELECT max(started_at) FROM runs WHERE source = ?", (source,)).fetchone()
        return datetime.fromisoformat(row[0]) if row[0] else None

    def finished_run(self, started: datetime, source: str = "alert") -> None:
        """Remember when a finder run of a source that finished started: the next run of that
        source reads from then."""
        with self._db:
            self._db.execute("INSERT INTO runs (started_at, source) VALUES (?, ?)", (started.isoformat(), source))
