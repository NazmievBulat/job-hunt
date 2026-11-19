"""`hunt find`'s public job feeds: the APIs that are meant to be read by programs.

No key: Remotive and RemoteOK (remote jobs only, so skipped when the settings exclude remote
jobs), Arbeitnow, and the latest monthly Hacker News "Who is hiring?" thread through the public
Algolia HN API, where one top-level comment is one job. With a free key of the person's own in
`keys` of the settings: USAJobs, Adzuna (its US jobs) and Jooble; a feed without its key is
skipped with one line.

The feeds are read side by side, one request at a time to each, and their jobs go through the
finder's filters and saving (`save`) as every other source's do. Remotive, RemoteOK, Arbeitnow,
Hacker News and USAJobs give each job's description; Adzuna and Jooble give only a snippet, so
their job's page is read as an alert's is. Adzuna and Jooble are searched for each title in each
area, and once for remote jobs unless the settings exclude them; USAJobs once for each title.
A job found by the remote search counts as remote; one found by an area's search that the area
table cannot place (`search.area_of`) keeps that area, the feed put it there, unless its place
names a state outside the area ("Chicago, IL" for Raleigh).

A Hacker News comment's first line is read by code: `Company | Title | Place | REMOTE | ...`,
the title being the part that holds one of the settings' titles. A comment without that shape
is kept with its first line as the title and its whole text as the description. Either way the
job's link is the comment, and the addresses the comment names close its description.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from job_hunt.finder import FIRST_RUN, html_text, title_matches
from job_hunt.jobs import FoundJob, JobsList, Remote
from job_hunt.search import (
    AREAS, BUSY, BUSY_PAUSES, Answer, SearchError, _placed, _remote, _states_named, _words, area_of, by_host,
)
from job_hunt.settings import FEED_KEYS, Settings

FEEDS = ("remotive", "remoteok", "arbeitnow", "hn_hiring", "usajobs", "adzuna", "jooble")
REMOTE_ONLY = ("remotive", "remoteok")
REMOTIVE = "https://remotive.com/api/remote-jobs"
REMOTEOK = "https://remoteok.com/api"
ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"
# Arbeitnow sends 300 jobs a page, newest first; later pages are read only while they are new.
ARBEITNOW_PAGES = 3
HN_STORIES = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
HN_ITEM = "https://hn.algolia.com/api/v1/items/"
HN_THREAD = "Ask HN: Who is hiring?"
HN_COMMENT = "https://news.ycombinator.com/item?id="
HN_UNKNOWN_COMPANY = "unknown (Hacker News)"
HN_TITLE_LETTERS = 150
USAJOBS = "https://data.usajobs.gov/api/search"
USAJOBS_PAGE = 500
USAJOBS_DAYS = 60
# The page's number follows.
ADZUNA = "https://api.adzuna.com/v1/api/jobs/us/search/"
ADZUNA_PAGE = 50
JOOBLE = "https://jooble.org/api/"
# A keyed feed's search is read page by page up to its total; this only stops one that pages for ever.
KEYED_PAGES = 10
# "NO REMOTE", "not remote", "Remote: no": a part of a first line that says the job is not remote.
NOT_REMOTE = re.compile(r"\b(?:no|not|non)[\s-]+remote\b|\bremote\s*[:=]\s*(?:no|none)\b", re.IGNORECASE)


class Get(Protocol):
    def __call__(self, url: str, body: Any = None, headers: dict[str, str] | None = None) -> Answer: ...


# A job a feed found, with its description, or None when its page is to be read.
Found = tuple[FoundJob, "str | None"]
Save = Callable[[FoundJob, "str | None"], bool]
Ask = Callable[..., Any]


@dataclass
class FeedRead:
    jobs: list[Found] = field(default_factory=list)
    # The searches of the feed that failed, each with why; the others' jobs are kept.
    problems: list[str] = field(default_factory=list)

    def add(self, found: Found) -> None:
        """A job two searches of one feed found is kept once: remote when the remote search found
        it, in an area when an area's search placed it."""
        job, description = found
        for number, (each, known) in enumerate(self.jobs):
            if each.link == job.link:
                self.jobs[number] = (replace(each, remote="remote" if job.remote == "remote" else each.remote,
                                             area=each.area or job.area), known)
                return
        self.jobs.append(found)


def skipped(name: str, settings: Settings) -> str | None:
    """Why the feed is not read this run, or None."""
    if name in REMOTE_ONLY and settings.remote == "exclude":
        return "it lists only remote jobs and the settings exclude them"
    if name in FEED_KEYS and name not in settings.keys:
        return f"no key in data/settings.json (keys.{name})"
    return None


def _plain(text: Any) -> str:
    """A title or name without the tags and entities a feed may leave in it."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text if isinstance(text, str) else "")).split())


def _job(name: str, settings: Settings, *, link: str, title: str, company: str, places: list[str], remote: Remote,
         area: str | None = None) -> FoundJob:
    place, placed = _placed(places, settings.areas)
    return FoundJob(link=link, title=_plain(title), company=_plain(company), place=place, remote=remote, board=name,
                    email_id=None, email_date=None, area=placed or _searched_area(place, area), source=name)


def _searched_area(place: str, area: str | None) -> str | None:
    """The area of the search that found a job the table cannot place, unless the place names
    another state than the area's."""
    if area is None:
        return None
    states = AREAS.get(area.casefold(), ((), ()))[0]
    named = _states_named(place, _words(place))
    return None if states and named and not named & set(states) else area


def _days(since: datetime, now: datetime) -> int:
    """The whole days back to `since`, at least one."""
    return max(1, -(-(now - since) // timedelta(days=1)))


def _searches(settings: Settings) -> list[tuple[str, str | None]]:
    """Where a feed that searches by place is searched: each area's place, as the feed is asked
    (`Raleigh, NC` for an area the table knows), with its area; then remote, with None."""
    places: list[tuple[str, str | None]] = []
    if settings.remote != "only":
        for area in settings.areas:
            states, cities = AREAS.get(area.casefold(), ((), ()))
            places.append((f"{cities[0]}, {states[0]}" if cities else area, area))
    if settings.remote != "exclude":
        places.append(("remote", None))
    return places


def _remotive(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    read = FeedRead()
    for entry in ask(REMOTIVE)["jobs"]:
        if title_matches(_plain(entry["title"]), settings.titles):
            read.add((_job("remotive", settings, link=entry["url"], title=entry["title"], company=entry["company_name"],
                           places=[entry.get("candidate_required_location") or ""], remote="remote"),
                      html_text(entry.get("description") or "")))
    return read


def _remoteok(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    read = FeedRead()
    # The first entry is the feed's legal notice, not a job.
    for entry in ask(REMOTEOK):
        if "position" in entry and title_matches(_plain(entry["position"]), settings.titles):
            read.add((_job("remoteok", settings, link=entry["url"], title=entry["position"], company=entry["company"],
                           places=[entry.get("location") or ""], remote="remote"),
                      html_text(entry.get("description") or "")))
    return read


def _arbeitnow(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    read = FeedRead()
    url: str | None = ARBEITNOW
    for _ in range(ARBEITNOW_PAGES):
        if not url:
            break
        data = ask(url)
        entries = data["data"]
        for entry in entries:
            if not title_matches(_plain(entry["title"]), settings.titles):
                continue
            places = [entry.get("location") or ""]
            remote = "remote" if entry.get("remote") is True else _remote(None, places)
            # The description comes as escaped HTML.
            read.add((_job("arbeitnow", settings, link=entry["url"], title=entry["title"],
                           company=entry["company_name"], places=places, remote=remote),
                      html_text(html.unescape(entry.get("description") or ""))))
        if not entries or min(int(entry["created_at"]) for entry in entries) < since.timestamp():
            break
        url = data["links"]["next"]
    return read


def _says_remote(text: str) -> bool:
    return bool(re.search(r"\bremote\b", text, re.IGNORECASE)) and not NOT_REMOTE.search(text)


def hn_job(comment: dict[str, Any], settings: Settings) -> Found:
    """One top-level comment of a "Who is hiring?" thread as a job."""
    text = comment["text"]
    first = " ".join(html_text(re.split(r"<p>", text, maxsplit=1)[0]).split())
    parts = [part.strip() for part in first.split("|") if part.strip()]
    # Part by part: "Remote (US) | no remote outside the US" is remote.
    remote: Remote = ("hybrid" if re.search(r"\bhybrid\b", first, re.IGNORECASE)
                      else "remote" if any(_says_remote(part) for part in (parts if len(parts) >= 3 else [first]))
                      else "on_site")
    if len(parts) >= 3:
        company, rest = parts[0], parts[1:]
        title = next((part for part in rest if title_matches(part, settings.titles)), rest[0])
        places = [part for part in rest if part is not title]
        in_area = [part for part in places if area_of(part, settings.areas) is not None]
        remote_named = [part for part in places if _says_remote(part)]
        places = in_area[:1] or remote_named[:1]
    else:
        # A comment the code cannot read: its first line, cut at a word, stands for the title.
        company, title = HN_UNKNOWN_COMPANY, first
        if len(title) > HN_TITLE_LETTERS:
            title = title[:HN_TITLE_LETTERS].rsplit(" ", 1)[0] + "…"
        places = [first] if area_of(first, settings.areas) is not None else []
    links = list(dict.fromkeys(html.unescape(link) for link in re.findall(r'href="([^"]+)"', text)))
    description = html_text(text) + ("\n\nLinks in the comment:\n" + "\n".join(f"- {link}" for link in links)
                                     if links else "")
    job = _job("hn_hiring", settings, link=f"{HN_COMMENT}{comment['id']}", title=title, company=company,
               places=places, remote=remote)
    return job, description


def _hn_hiring(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    stories = ask(HN_STORIES)["hits"]
    story = next((hit for hit in stories if (hit.get("title") or "").startswith(HN_THREAD)), None)
    if story is None:
        raise SearchError(f'no "{HN_THREAD}" thread among the latest stories')
    read = FeedRead()
    for comment in ask(f"{HN_ITEM}{quote(str(story['objectID']))}")["children"]:
        # A deleted or flagged comment has no text.
        if comment.get("text"):
            job, description = hn_job(comment, settings)
            if title_matches(job.title, settings.titles):
                read.add((job, description))
    return read


def _usajobs_job(entry: dict[str, Any], settings: Settings) -> Found:
    job = entry["MatchedObjectDescriptor"]
    details = (job.get("UserArea") or {}).get("Details") or {}
    display = job.get("PositionLocationDisplay") or ""
    places = [each.get("LocationName") or "" for each in job.get("PositionLocation") or []] or [display]
    remote = ("remote" if details.get("RemoteIndicator") is True or re.search(r"\bremote\b", display, re.IGNORECASE)
              else _remote(None, places))
    duties = "\n".join(f"- {duty}" for duty in details.get("MajorDuties") or [] if isinstance(duty, str))
    # What the job needs, besides what it does: the scoring and the resume read it.
    parts = [details.get("JobSummary"), duties, job.get("QualificationSummary"), details.get("Requirements"),
             details.get("Education")]
    description = "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
    # Without a description the job's page is read.
    return (_job("usajobs", settings, link=job["PositionURI"], title=job["PositionTitle"],
                 company=job.get("OrganizationName") or "", places=places, remote=remote), description or None)


def _usajobs(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    key = settings.keys["usajobs"]
    headers = {"User-Agent": key["email"], "Authorization-Key": key["key"]}
    read = FeedRead()
    for title in settings.titles:
        for page in range(1, KEYED_PAGES + 1):
            query = urlencode({"Keyword": title, "DatePosted": min(_days(since, now), USAJOBS_DAYS),
                               "ResultsPerPage": USAJOBS_PAGE, "Page": page})
            try:
                result = ask(f"{USAJOBS}?{query}", None, headers)["SearchResult"]
            except SearchError as error:
                read.problems.append(f"{title}: {error}")
                break
            entries = result["SearchResultItems"]
            for entry in entries:
                if title_matches(_plain(entry["MatchedObjectDescriptor"]["PositionTitle"]), settings.titles):
                    read.add(_usajobs_job(entry, settings))
            if not entries or page * USAJOBS_PAGE >= int(result.get("SearchResultCountAll") or 0):
                break
    return read


def _adzuna(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    key = settings.keys["adzuna"]
    read = FeedRead()
    for title in settings.titles:
        for where, area in _searches(settings):
            query = {"app_id": key["app_id"], "app_key": key["app_key"], "results_per_page": ADZUNA_PAGE,
                     "what_phrase": title, "max_days_old": _days(since, now), "content-type": "application/json"}
            query |= {"what_and": "remote"} if area is None else {"where": where}
            entries: list[Any] = []
            for page in range(1, KEYED_PAGES + 1):
                try:
                    data = ask(f"{ADZUNA}{page}?{urlencode(query)}")
                except SearchError as error:
                    read.problems.append(f"{title} in {where}: {error}")
                    break
                entries += data["results"]
                if len(data["results"]) < ADZUNA_PAGE or page * ADZUNA_PAGE >= int(data.get("count") or 0):
                    break
            for entry in entries:
                place = (entry.get("location") or {}).get("display_name") or ""
                remote = "remote" if area is None else _remote(None, [place, _plain(entry["title"])])
                # The redirect address changes from one answer to the next; the id names the job.
                read.add((_job("adzuna", settings, link=f"https://www.adzuna.com/details/{entry['id']}",
                               title=entry["title"], company=(entry.get("company") or {}).get("display_name") or "",
                               places=[place], remote=remote, area=area), None))
    read.jobs = [found for found in read.jobs if title_matches(found[0].title, settings.titles)]
    return read


def _jooble(settings: Settings, ask: Ask, since: datetime, now: datetime) -> FeedRead:
    key = settings.keys["jooble"]["key"]
    read = FeedRead()
    for title in settings.titles:
        for where, area in _searches(settings):
            search = {"keywords": f"{title} remote"} if area is None else {"keywords": title, "location": where}
            entries: list[Any] = []
            for page in range(1, KEYED_PAGES + 1):
                try:
                    data = ask(f"{JOOBLE}{quote(key)}", {**search, "page": str(page)})
                except SearchError as error:
                    read.problems.append(f"{title} in {where}: {error}")
                    break
                entries += data["jobs"]
                if not data["jobs"] or len(entries) >= int(data.get("totalCount") or 0):
                    break
            for entry in entries:
                place = _plain(entry.get("location"))
                remote = "remote" if area is None else _remote(None, [place, _plain(entry["title"])])
                # The link carries the search it came from; the id names the job.
                read.add((_job("jooble", settings, link=f"https://jooble.org/desc/{entry['id']}", title=entry["title"],
                               company=entry.get("company") or "", places=[place], remote=remote, area=area), None))
    read.jobs = [found for found in read.jobs if title_matches(found[0].title, settings.titles)]
    return read


READERS: dict[str, Callable[[Settings, Ask, datetime, datetime], FeedRead]] = {
    "remotive": _remotive, "remoteok": _remoteok, "arbeitnow": _arbeitnow, "hn_hiring": _hn_hiring,
    "usajobs": _usajobs, "adzuna": _adzuna, "jooble": _jooble,
}


@dataclass
class Feeds:
    get: Get
    sleep: Callable[[float], None] = time.sleep

    def _ask(self, url: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
        answer = self.get(url, body, headers)
        for pause in BUSY_PAUSES:
            if answer.status not in BUSY:
                break
            self.sleep(pause)
            answer = self.get(url, body, headers)
        if answer.status != 200:
            raise SearchError(f"HTTP {answer.status}")
        try:
            return json.loads(answer.text)
        except json.JSONDecodeError as error:
            raise SearchError("the answer is not JSON") from error

    def read(self, name: str, settings: Settings, since: datetime, now: datetime) -> FeedRead:
        """One feed's jobs whose titles are wanted; raises SearchError for a feed that failed."""
        try:
            return READERS[name](settings, self._ask, since, now)
        except SearchError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError, IndexError) as error:
            raise SearchError(f"the answer is not the feed's shape ({error!r})") from error

    def run(self, settings: Settings, jobs: JobsList, started: datetime, save: Save) -> tuple[list[str], list[str]]:
        """Read every feed the settings allow, saving each job through `save` (the finder's
        filters and saving); return a line per feed and what failed."""
        # In the order of `FEEDS`: a line for each feed read or skipped, none for one that failed.
        lines: dict[str, str] = {}
        failed: list[str] = []
        ready: list[str] = []
        for name in FEEDS:
            if (why := skipped(name, settings)) is not None:
                lines[name] = f"{name}: skipped, {why}"
            else:
                ready.append(name)
        since = {name: jobs.last_run(name) or started - FIRST_RUN for name in ready}
        # One lane a feed: each is its own host, asked one request at a time.
        read = by_host([(name, lambda name=name: self.read(name, settings, since[name], started)) for name in ready])
        for name, result in zip(ready, read):
            if isinstance(result, Exception):
                failed.append(f"{name}: {result}")
                continue
            new = sum(save(job, description) for job, description in result.jobs)
            lines[name] = f"{name}: {len(result.jobs)} found, {new} new"
            failed += [f"{name}: {problem}" for problem in result.problems]
            if not result.problems:
                jobs.finished_run(started, name)
        return [lines[name] for name in FEEDS if name in lines], failed
