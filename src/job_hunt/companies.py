"""The company index, `data/companies.db`: every company found on a public job-board system, and
its board.

A company is a board: the system (Greenhouse, Lever, Ashby, Workday, SmartRecruiters or
Workable) and the company's name on it (its slug; a Workday one is `tenant/wdN/site`).
`hunt companies` fills the index from Common Crawl's public URL index of the latest crawl, and
`hunt find` does it by itself once a week; any link a source meets that leads to a board adds
its company too, and so do the settings' `companies`. `hunt find` sweeps the whole index once a
day, and on its later runs only the boards that posted in the last week. A board that answers
404 twice in a row is dropped, and stays dropped: the next crawl does not bring it back.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote, urlsplit

if TYPE_CHECKING:
    from job_hunt.search import Get
    from job_hunt.settings import Settings

SYSTEMS = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable")
CRAWLS = "https://index.commoncrawl.org/collinfo.json"
# The addresses of each system's boards in the crawl's URL index: a prefix, or a whole domain.
CRAWL_PATTERNS = (
    "boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
    "*.myworkdayjobs.com", "jobs.smartrecruiters.com/*", "apply.workable.com/*",
)
# The index answers 504 when it is busy; each query is tried this many times.
CRAWL_TRIES = 3
CRAWL_PAUSE = 10.0
# A query's pages are read up to this many: the boards' addresses fill far fewer.
CRAWL_PAGES = 100
MISSES = 2
FULL_SWEEP = timedelta(hours=20)
REINDEX = timedelta(days=7)
POSTED_LATELY = timedelta(days=7)

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WORKDAY = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$")
LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# The first parts of a path that are the system's own pages, not a company.
NOT_COMPANIES = {
    "greenhouse": {"embed", "v1"}, "lever": set(), "ashby": {"api"}, "smartrecruiters": {"oneclick-ui", "api"},
    "workable": {"j", "api"},
}
HOSTS = {
    "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse", "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby", "jobs.smartrecruiters.com": "smartrecruiters", "apply.workable.com": "workable",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    system TEXT NOT NULL,
    slug TEXT NOT NULL COLLATE NOCASE,
    added_at TEXT NOT NULL,
    last_posted TEXT,
    misses INTEGER NOT NULL DEFAULT 0,
    dropped_at TEXT,
    PRIMARY KEY (system, slug)
);
CREATE TABLE IF NOT EXISTS times (
    name TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Board:
    system: str
    slug: str

    @property
    def name(self) -> str:
        """The company's name as its slug says it, for boards whose answers do not name it."""
        words = self.slug.split("/")[0].replace("-", " ").replace("_", " ").split()
        return " ".join(word[:1].upper() + word[1:] for word in words)


def board_of(link: str) -> Board | None:
    """The board a link leads to, or None for a link to anything else."""
    parts = urlsplit(link.strip())
    host = parts.netloc.lower()
    path = [part for part in parts.path.split("/") if part]
    if found := WORKDAY.match(host):
        path = [part for part in path if not LOCALE.match(part)]
        if path and path[0] != "wday" and SLUG.match(path[0]):
            return Board("workday", f"{found.group(1)}/{found.group(2)}/{path[0]}")
        return None
    system = HOSTS.get(host)
    if system is None:
        return None
    if system == "greenhouse" and path[:1] == ["embed"]:
        slug = dict(parse_qsl(parts.query)).get("for", "")
        return Board(system, slug) if SLUG.match(slug) else None
    if not path or path[0] in NOT_COMPANIES[system] or not SLUG.match(path[0]):
        return None
    return Board(system, path[0])


def wanted_boards(settings: Settings) -> list[Board]:
    """The boards the settings add by hand; the settings checked each is one."""
    return [board for link in settings.companies if (board := board_of(link)) is not None]


class CrawlError(RuntimeError):
    """A query of Common Crawl's index that did not answer."""


def _crawl_get(get: Get, url: str, sleep: Callable[[float], None]) -> str:
    """The answer of the index, tried again while it is busy; raises CrawlError saying why not."""
    said = ""
    for attempt in range(CRAWL_TRIES):
        if attempt:
            sleep(CRAWL_PAUSE)
        try:
            answer = get(url)
        except Exception as error:
            said = str(error)
            continue
        if answer.status == 200:
            return answer.text
        said = f"HTTP {answer.status}"
        if answer.status not in (429, 500, 502, 503, 504):
            break
    raise CrawlError(said)


def crawl_index(get: Get, *, sleep: Callable[[float], None]) -> tuple[list[Board], list[str]]:
    """The boards the latest crawl saw, in the order first seen, and each query that failed."""
    try:
        api = json.loads(_crawl_get(get, CRAWLS, sleep))[0]["cdx-api"]
    except (CrawlError, ValueError, LookupError, TypeError) as error:
        return [], [f"Common Crawl's list of crawls: {error}"]
    boards: dict[Board, None] = {}
    problems: list[str] = []
    for pattern in CRAWL_PATTERNS:
        query = f"{api}?url={quote(pattern, safe='')}&output=json&fl=url"
        try:
            pages = int(json.loads(_crawl_get(get, f"{query}&showNumPages=true", sleep))["pages"])
            for page in range(min(pages, CRAWL_PAGES)):
                for line in _crawl_get(get, f"{query}&page={page}", sleep).splitlines():
                    if line.strip() and (board := board_of(json.loads(line).get("url", ""))) is not None:
                        boards.setdefault(board)
        except (CrawlError, ValueError, LookupError, TypeError, AttributeError) as error:
            problems.append(f"Common Crawl for {pattern}: {error}")
    # Two spellings of one slug are one company, as the index keeps them.
    unique: dict[tuple[str, str], Board] = {}
    for board in boards:
        unique.setdefault((board.system, board.slug.casefold()), board)
    return list(unique.values()), problems


def fill_index(companies: Companies, get: Get, *, sleep: Callable[[float], None]) -> tuple[str, list[str]]:
    """Add the latest crawl's boards to the index; return the line saying so and what failed.
    A crawl that gave no board at all is tried again at the next run."""
    boards, problems = crawl_index(get, sleep=sleep)
    added = companies.add(boards)
    if boards:
        companies.indexed()
    return f"companies: {added} added from Common Crawl, {companies.count()} in the index", problems


def _now() -> datetime:
    return datetime.now(UTC)


class Companies:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self._clock = clock
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The finder's sweep uses it from a thread of its own, then the finder from its own.
        self._db = sqlite3.connect(path, check_same_thread=False)
        if new:
            path.chmod(0o600)
        self._db.executescript(SCHEMA)

    def add(self, boards: list[Board]) -> int:
        """Add the boards the index does not have, a dropped one neither; return how many."""
        now = self._clock().isoformat()
        with self._db:
            before = self.count()
            self._db.executemany(
                "INSERT OR IGNORE INTO companies (system, slug, added_at) VALUES (?, ?, ?)",
                [(board.system, board.slug, now) for board in boards])
            return self.count() - before

    def count(self) -> int:
        return self._db.execute("SELECT count(*) FROM companies WHERE dropped_at IS NULL").fetchone()[0]

    def to_sweep(self, *, full: bool) -> list[Board]:
        """Every board in the index, or only those that posted in the last week."""
        since = (self._clock() - POSTED_LATELY).isoformat()
        rows = self._db.execute(
            "SELECT system, slug FROM companies WHERE dropped_at IS NULL"
            + ("" if full else " AND last_posted >= ?") + " ORDER BY rowid",
            () if full else (since,))
        return [Board(system, slug) for system, slug in rows]

    def answered(self, board: Board, newest: datetime | None) -> None:
        """The board answered: its misses start again, and its newest posting is kept."""
        with self._db:
            self._db.execute(
                "UPDATE companies SET misses = 0, last_posted = coalesce(?, last_posted) WHERE system = ? AND slug = ?",
                (newest.astimezone(UTC).isoformat() if newest else None, board.system, board.slug))

    def missed(self, board: Board) -> bool:
        """The board answered 404; return whether that dropped it."""
        with self._db:
            self._db.execute("UPDATE companies SET misses = misses + 1 WHERE system = ? AND slug = ?",
                             (board.system, board.slug))
            self._db.execute(
                "UPDATE companies SET dropped_at = ? WHERE system = ? AND slug = ? AND misses >= ? AND dropped_at IS NULL",
                (self._clock().isoformat(), board.system, board.slug, MISSES))
            row = self._db.execute("SELECT dropped_at FROM companies WHERE system = ? AND slug = ?",
                                   (board.system, board.slug)).fetchone()
        return bool(row and row[0])

    def _since(self, name: str) -> timedelta | None:
        row = self._db.execute("SELECT at FROM times WHERE name = ?", (name,)).fetchone()
        return self._clock() - datetime.fromisoformat(row[0]) if row else None

    def _mark(self, name: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO times (name, at) VALUES (?, ?)", (name, self._clock().isoformat()))

    def full_due(self) -> bool:
        """The morning's whole sweep: none in the last 20 hours."""
        since = self._since("full_sweep")
        return since is None or since >= FULL_SWEEP

    def swept_full(self) -> None:
        self._mark("full_sweep")

    def index_due(self) -> bool:
        since = self._since("indexed")
        return since is None or since >= REINDEX

    def indexed(self) -> None:
        self._mark("indexed")
