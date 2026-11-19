"""`hunt find`'s own searches, besides the alert emails: LinkedIn's public guest search, signed
out, and the public job boards of every company in the company index (`companies.py`).

Plain HTTP with `urllib`, no cookies and no sign-in. LinkedIn is searched for each title in
each area, and once per title for remote jobs unless the settings exclude them, over the time
since its last run, at most `LINKEDIN_PAGES` pages a search and 2 to 5 seconds between its
requests (`Pace`); a 429 or 999 ends LinkedIn for the run. The boards are read through their
public JSON, in parallel across hosts and at most `HOST_LIMIT` at a time on one host
(`by_host`); they give each job's description, so no page is read for them.

A job's area is its place matched against the settings' areas (`area_of`): by `AREAS`, the
city names in each area this code knows, or the area's own name. A LinkedIn job found in an
area's search that the table cannot place keeps that area: LinkedIn put it there.
"""

from __future__ import annotations

import html
import http.client
import json
import random
import re
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from job_hunt.companies import SYSTEMS, Board, Companies, board_of, fill_index, wanted_boards
from job_hunt.finder import FIRST_RUN, html_text, title_matches
from job_hunt.jobs import FoundJob, JobsList, Remote, clean_link
from job_hunt.settings import Settings

LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_PAGE_SIZE = 10
LINKEDIN_PAGES = 5
LINKEDIN_STOPS = (429, 999)
PAUSE_SECONDS = (2.0, 5.0)
HOST_LIMIT = 4
WORKERS = 32
REQUEST_SECONDS = 30
ANSWER_BYTES = 50_000_000
# Pages of a board are read up to its total; these only stop a board that pages for ever.
WORKDAY_PAGE = 20
WORKDAY_PAGES = 50
SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_PAGES = 100
# A busy board (too many requests, or down for a moment) is asked again after these pauses.
BUSY = (429, 503)
BUSY_PAUSES = (5.0, 20.0)
USER_AGENT = "Mozilla/5.0 (compatible; job-hunt)"
# LinkedIn counts its window back from each request, which comes minutes after the run started
# (the alerts, the index); the window reaches this much further back, and duplicates are dropped.
LINKEDIN_OVERLAP = timedelta(minutes=15)


@dataclass(frozen=True)
class Answer:
    status: int
    text: str


class Get(Protocol):
    def __call__(self, url: str, body: Any = None) -> Answer: ...


class SearchError(RuntimeError):
    """A search or a board that did not answer as it should, and why."""


class Gone(SearchError):
    """A board that answered 404: the company has no board there (any more)."""


def http_get(url: str, body: Any = None, headers: dict[str, str] | None = None) -> Answer:
    """GET the address, or POST `body` as JSON, with `headers` besides the usual ones; any HTTP
    status is an answer, no answer an error."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html", **(headers or {})}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=REQUEST_SECONDS) as response:
            return Answer(response.status, response.read(ANSWER_BYTES).decode("utf-8", errors="replace"))
    except HTTPError as error:
        return Answer(error.code, error.read(ANSWER_BYTES).decode("utf-8", errors="replace"))
    except TimeoutError as error:
        raise SearchError(f"no answer in {REQUEST_SECONDS} seconds") from error
    except (URLError, OSError) as error:
        raise SearchError(str(getattr(error, "reason", error))) from error
    except http.client.HTTPException as error:
        # An answer cut short or not HTTP at all: `IncompleteRead`, `BadStatusLine`.
        raise SearchError(f"{type(error).__name__}: {error}") from error


class Pace:
    """A pause of 2 to 5 seconds before each LinkedIn request but the first, pages included; and
    why LinkedIn said stop this run, after which no LinkedIn request is made. The searches run
    in a new order each run (`order`), so a stop does not keep the same ones from ever running."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep, between: Callable[[], float] | None = None,
                 order: Callable[[list[Any]], None] = random.shuffle) -> None:
        self.sleep = sleep
        self.between = between or (lambda: random.uniform(*PAUSE_SECONDS))
        self.order = order
        self._first = True
        self.stopped: str | None = None

    def stop(self, status: int | str) -> None:
        self.stopped = f"LinkedIn answered {status}, so it is not searched again this run"

    def wait(self) -> None:
        if not self._first:
            self.sleep(self.between())
        self._first = False


# Areas

# Each area's states and the cities in it. `/setup` adds the areas a person names.
AREAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "raleigh-durham-chapel hill": (("NC",), (
        "Raleigh", "Durham", "Chapel Hill", "Cary", "Morrisville", "Apex", "Research Triangle Park", "RTP",
        "Wake Forest", "Garner", "Holly Springs", "Fuquay-Varina", "Hillsborough", "Carrboro", "Knightdale",
        "Clayton", "Pittsboro")),
    "san francisco bay area": (("CA",), (
        "San Francisco", "Bay Area", "Oakland", "San Jose", "Palo Alto", "Mountain View", "Sunnyvale", "Santa Clara",
        "Menlo Park", "Redwood City", "Berkeley", "Fremont", "San Mateo", "Cupertino", "South San Francisco",
        "Emeryville", "Foster City", "Burlingame", "Milpitas", "Hayward", "Pleasanton", "San Ramon", "Walnut Creek",
        "San Bruno", "Los Gatos", "San Carlos", "Livermore")),
    "new york city": (("NY", "NJ"), (
        "New York", "NYC", "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "Long Island City",
        "Jersey City", "Hoboken")),
}
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}
# Countries that put a US city name somewhere else ("Durham, England").
ABROAD = ("united kingdom", "uk", "england", "scotland", "wales", "ireland", "canada", "australia", "india",
          "germany", "france", "spain", "netherlands", "poland", "mexico", "brazil", "singapore", "japan", "china",
          "israel")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _holds(words: list[str], name: str) -> bool:
    wanted = _words(name)
    return bool(wanted) and any(words[at:at + len(wanted)] == wanted for at in range(len(words) - len(wanted) + 1))


def _states_named(place: str, words: list[str]) -> set[str]:
    # An abbreviation counts only in capitals: "in" and "or" are words.
    named = {code for code in re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", place) if code in STATES}
    return named | {code for code, name in STATES.items() if _holds(words, name)}


def area_of(place: str, areas: list[str]) -> str | None:
    """The first of the settings' areas the place is in, as the settings spell it, or None. A
    location naming several places ("San Francisco; New York") is matched place by place."""
    for each in re.split(r"\s*(?:[;|/]|\bor\b)\s*", place):
        if (area := _area_of_one(each, areas)) is not None:
            return area
    return None


def _area_of_one(place: str, areas: list[str]) -> str | None:
    words = _words(place)
    if not words:
        return None
    abroad = any(_holds(words, country) for country in ABROAD)
    named = _states_named(place, words)
    for area in areas:
        states, cities = AREAS.get(area.casefold(), ((), ()))
        if not any(_holds(words, name) for name in (area, *cities)):
            continue
        # A city of the same name abroad or in another state is not the area the table knows;
        # an area it does not know ("London") is matched by its name alone, abroad too.
        if states and (abroad or (named and not named & set(states))):
            continue
        return area
    return None


def _placed(places: list[str], areas: list[str]) -> tuple[str, str | None]:
    """The first place in an area and that area; failing that, the first place and no area."""
    places = [" ".join(place.split()) for place in places if isinstance(place, str) and place.strip()]
    for place in places:
        if (area := area_of(place, areas)) is not None:
            return place, area
    return (places[0] if places else ""), None


def _remote(kind: str | None, places: list[str]) -> Remote:
    """From the board's own word for it when it has one, else from the places."""
    word = re.sub(r"[^a-z]", "", (kind or "").casefold())
    if word in ("remote", "hybrid"):
        return word  # type: ignore[return-value]
    if word == "onsite":
        return "on_site"
    return "remote" if any(re.search(r"\bremote\b", place, re.IGNORECASE) for place in places if place) else "on_site"


# LinkedIn


@dataclass(frozen=True)
class Card:
    link: str
    title: str
    company: str
    place: str


class _Cards(HTMLParser):
    FIELDS = {"base-search-card__title": "title", "base-search-card__subtitle": "company",
              "job-search-card__location": "place"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        # The field whose text is being read, its tag, and how many of that tag opened inside it.
        self.field: str | None = None
        self.tag, self.depth = "", 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        named = {name: value or "" for name, value in attrs}
        classes = set(named.get("class", "").split())
        if classes & {"base-search-card", "job-search-card"}:
            self.cards.append({"link": "", "title": "", "company": "", "place": ""})
            self.field = None
        if not self.cards:
            return
        if tag == "a" and "base-card__full-link" in classes:
            self.cards[-1]["link"] = named.get("href", "")
        if self.field is not None:
            self.depth += tag == self.tag
            return
        for name, wanted in self.FIELDS.items():
            if name in classes:
                self.field, self.tag, self.depth = wanted, tag, 0

    def handle_endtag(self, tag: str) -> None:
        if self.field is not None and tag == self.tag:
            if self.depth:
                self.depth -= 1
            else:
                self.field = None

    def handle_data(self, data: str) -> None:
        if self.field is not None:
            self.cards[-1][self.field] += data


def linkedin_cards(page: str) -> list[Card]:
    """The jobs on a page of LinkedIn's guest search, each with its link, title, company, place."""
    parser = _Cards()
    parser.feed(page)
    cards = []
    for card in parser.cards:
        text = {name: " ".join(value.split()) for name, value in card.items()}
        if not (text["link"].startswith("https://") and text["title"]):
            continue
        try:
            link = clean_link(text["link"])
        except ValueError:
            # A link that is not one ("https://[bad"): the card is skipped, not the search.
            continue
        cards.append(Card(link, text["title"], text["company"], text["place"]))
    return cards


@dataclass
class LinkedInResult:
    jobs: list[FoundJob]
    problems: list[str] = field(default_factory=list)
    # Why LinkedIn stopped answering this run, or None.
    stopped: str | None = None


def linkedin_search(settings: Settings, since: datetime, now: datetime, get: Get, pace: Pace) -> LinkedInResult:
    seconds = max(1, int((now - since).total_seconds()))
    searches: list[tuple[str, str | None]] = []
    if settings.remote != "only":
        searches += [(title, area) for title in settings.titles for area in settings.areas]
    if settings.remote != "exclude":
        searches += [(title, None) for title in settings.titles]
    pace.order(searches)
    found: dict[str, FoundJob] = {}
    result = LinkedInResult([], stopped=pace.stopped)
    if pace.stopped:
        return result
    for title, area in searches:
        named = f"LinkedIn search for {title} in {area}" if area else f"LinkedIn search for {title}, remote"
        for page in range(LINKEDIN_PAGES):
            # Without remote jobs, an area's search asks for on-site and hybrid ones only.
            where = {"location": area, **({"f_WT": "1,3"} if settings.remote == "exclude" else {})} if area else {
                "f_WT": "2"}
            query = urlencode({"keywords": title, **where, "f_TPR": f"r{seconds}", "start": page * LINKEDIN_PAGE_SIZE})
            pace.wait()
            try:
                answer = get(f"{LINKEDIN_SEARCH}?{query}")
            except SearchError as error:
                result.problems.append(f"{named}: {error}")
                break
            if answer.status in LINKEDIN_STOPS:
                pace.stop(answer.status)
                result.stopped = pace.stopped
                result.jobs = list(found.values())
                return result
            if answer.status != 200:
                result.problems.append(f"{named}: HTTP {answer.status}")
                break
            cards = linkedin_cards(answer.text)
            for card in cards:
                job = found.setdefault(card.link, FoundJob(
                    link=card.link, title=card.title, company=card.company, place=card.place,
                    remote="on_site" if area else "remote", board="linkedin", email_id=None, email_date=None,
                    area=area_of(card.place, settings.areas) or area, source="linkedin_search"))
                # An area's search does not say a job is remote; the remote search does.
                if area is None and job.remote != "remote":
                    found[card.link] = replace(job, remote="remote")
            # A short page is the last one.
            if len(cards) < LINKEDIN_PAGE_SIZE:
                break
    result.jobs = list(found.values())
    return result


# Boards


@dataclass(frozen=True)
class Searched:
    job: FoundJob
    description: str


@dataclass
class BoardRead:
    jobs: list[Searched]
    # When the board's newest job was posted, or None when it says nothing.
    newest: datetime | None
    name: str


Ask = Callable[..., Any]


def _answer(get: Get, board: Board, sleep: Callable[[float], None], url: str, body: Any = None) -> Any:
    answer = get(url, body)
    for pause in BUSY_PAUSES:
        if answer.status not in BUSY:
            break
        sleep(pause)
        answer = get(url, body)
    if answer.status == 404:
        raise Gone("HTTP 404")
    if answer.status != 200:
        raise SearchError(f"HTTP {answer.status}")
    try:
        return json.loads(answer.text)
    except json.JSONDecodeError as error:
        raise SearchError(f"{board.system} {board.slug}: the answer is not JSON") from error


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _newest(dates: list[datetime | None]) -> datetime | None:
    known = [date for date in dates if date is not None]
    return max(known) if known else None


def _job(board: Board, settings: Settings, *, link: str, title: str, company: str, places: list[str],
         kind: str | None, description: str) -> Searched:
    place, area = _placed(places, settings.areas)
    return Searched(FoundJob(
        link=link, title=" ".join(title.split()), company=company, place=place, remote=_remote(kind, places),
        board=board.system, email_id=None, email_date=None, area=area, source=board.system), description)


def _greenhouse(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    data = ask(f"https://boards-api.greenhouse.io/v1/boards/{quote(board.slug)}/jobs?content=true")
    entries = data["jobs"]
    jobs = [
        _job(board, settings, link=entry["absolute_url"], title=entry["title"],
             company=entry.get("company_name") or board.name, places=[(entry.get("location") or {}).get("name", "")],
             kind=None, description=html_text(html.unescape(entry.get("content") or "")))
        for entry in entries if title_matches(entry["title"], settings.titles)]
    names = [entry.get("company_name") for entry in entries if entry.get("company_name")]
    return BoardRead(jobs, _newest([_date(entry.get("first_published")) for entry in entries]),
                     names[0] if names else board.name)


def _lever_description(entry: dict[str, Any]) -> str:
    lists = "".join(f"<h3>{each.get('text', '')}</h3><ul>{each.get('content', '')}</ul>" for each in entry.get("lists") or [])
    return html_text(f"{entry.get('description') or ''}{lists}{entry.get('additional') or ''}")


def _lever(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    entries = ask(f"https://api.lever.co/v0/postings/{quote(board.slug)}?mode=json")
    if not isinstance(entries, list):
        raise SearchError(f"lever {board.slug}: the answer is not a list of postings")
    jobs = []
    for entry in entries:
        if not title_matches(entry["text"], settings.titles):
            continue
        categories = entry.get("categories") or {}
        jobs.append(_job(board, settings, link=entry["hostedUrl"], title=entry["text"], company=board.name,
                         places=[categories.get("location", ""), *(categories.get("allLocations") or [])],
                         kind=entry.get("workplaceType"), description=_lever_description(entry)))
    created = [datetime.fromtimestamp(entry["createdAt"] / 1000, UTC) for entry in entries
               if isinstance(entry.get("createdAt"), int | float)]
    return BoardRead(jobs, _newest(created), board.name)


def _ashby(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    entries = ask(f"https://api.ashbyhq.com/posting-api/job-board/{quote(board.slug)}")["jobs"]
    jobs = []
    for entry in entries:
        if not title_matches(entry["title"], settings.titles):
            continue
        places = [entry.get("location", ""), *(each.get("location", "") for each in entry.get("secondaryLocations") or [])]
        kind = entry.get("workplaceType") or ("remote" if entry.get("isRemote") else None)
        jobs.append(_job(board, settings, link=entry["jobUrl"], title=entry["title"], company=board.name, places=places,
                         kind=kind, description=html_text(entry.get("descriptionHtml") or "")))
    return BoardRead(jobs, _newest([_date(entry.get("publishedAt")) for entry in entries]), board.name)


def _posted_on(text: Any, now: datetime) -> datetime | None:
    """Workday's "Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+ Days Ago"."""
    words = str(text or "").casefold()
    if "today" in words:
        return now
    if "yesterday" in words:
        return now - timedelta(days=1)
    days = re.search(r"(\d+)\+? days? ago", words)
    return now - timedelta(days=int(days.group(1))) if days else None


def _workday(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    tenant, wd, site = board.slug.split("/")
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}"
    postings: dict[str, dict[str, Any]] = {}
    posted: list[datetime | None] = []
    for title in settings.titles:
        total = 0
        for page in range(WORKDAY_PAGES):
            body = {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": page * WORKDAY_PAGE, "searchText": title}
            data = ask(f"{api}/jobs", body)
            # Workday says the total on the first page only.
            total = total or int(data.get("total") or 0)
            found = data.get("jobPostings") or []
            for posting in found:
                posted.append(_posted_on(posting.get("postedOn"), now))
                if title_matches(posting.get("title", ""), settings.titles):
                    postings.setdefault(posting["externalPath"], posting)
            if len(found) < WORKDAY_PAGE or (page + 1) * WORKDAY_PAGE >= total:
                break
    jobs, name = [], board.name
    for path, posting in postings.items():
        # Only a job the list does not have yet is worth its details.
        if clean_link(f"{host}/{site}{path}") in known:
            continue
        try:
            details = ask(f"{api}{path}")
        except Gone:
            # The job closed since the list was read; the board is still there.
            continue
        info = details["jobPostingInfo"]
        name = (details.get("hiringOrganization") or {}).get("name") or name
        places = [info.get("location", ""), *(info.get("additionalLocations") or [])]
        jobs.append(_job(board, settings, link=info.get("externalUrl") or f"{host}/{site}{path}", title=info["title"],
                         company=name, places=places, kind=info.get("remoteType"),
                         description=html_text(info.get("jobDescription") or "")))
    return BoardRead(jobs, _newest(posted), name)


def _smartrecruiters(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    api = f"https://api.smartrecruiters.com/v1/companies/{quote(board.slug)}/postings"
    entries: list[dict[str, Any]] = []
    for page in range(SMARTRECRUITERS_PAGES):
        data = ask(f"{api}?limit={SMARTRECRUITERS_PAGE}&offset={page * SMARTRECRUITERS_PAGE}")
        entries += data["content"]
        if len(data["content"]) < SMARTRECRUITERS_PAGE or len(entries) >= int(data.get("totalFound") or 0):
            break
    jobs = []
    names = [(entry.get("company") or {}).get("name") for entry in entries]
    name = next((each for each in names if each), board.name)
    for entry in entries:
        if not title_matches(entry["name"], settings.titles):
            continue
        public = f"https://jobs.smartrecruiters.com/{board.slug}/{entry['id']}"
        # Its saved link may go on with the title after the number.
        if any(link == public or link.startswith(f"{public}-") for link in known):
            continue
        try:
            details = ask(f"{api}/{quote(str(entry['id']))}")
        except Gone:
            continue
        sections = (details.get("jobAd") or {}).get("sections") or {}
        text = "".join((sections.get(part) or {}).get("text") or ""
                       for part in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
        where = entry.get("location") or {}
        kind = "remote" if where.get("remote") else "hybrid" if where.get("hybrid") else "onsite"
        place = where.get("fullLocation") or ", ".join(part for part in (where.get("city"), where.get("region")) if part)
        jobs.append(_job(board, settings, link=details.get("postingUrl") or public, title=entry["name"], company=name,
                         places=[place], kind=kind, description=html_text(text)))
    return BoardRead(jobs, _newest([_date(entry.get("releasedDate")) for entry in entries]), name)


def _workable(board: Board, settings: Settings, ask: Ask, known: set[str], now: datetime) -> BoardRead:
    data = ask(f"https://apply.workable.com/api/v1/widget/accounts/{quote(board.slug)}?details=true")
    entries = data["jobs"]
    name = data.get("name") or board.name
    jobs = []
    for entry in entries:
        if not title_matches(entry["title"], settings.titles):
            continue
        places = [", ".join(part for part in (each.get("city"), each.get("region"), each.get("country")) if part)
                  for each in entry.get("locations") or []]
        places = places or [", ".join(part for part in (entry.get("city"), entry.get("state"), entry.get("country")) if part)]
        jobs.append(_job(board, settings, link=entry["url"], title=entry["title"], company=name, places=places,
                         kind="remote" if entry.get("telecommuting") else None,
                         description=html_text(entry.get("description") or "")))
    return BoardRead(jobs, _newest([_date(entry.get("published_on")) for entry in entries]), name)


READERS: dict[str, Callable[[Board, Settings, Ask, set[str], datetime], BoardRead]] = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workday": _workday,
    "smartrecruiters": _smartrecruiters, "workable": _workable,
}
API_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com",
    "smartrecruiters": "api.smartrecruiters.com", "workable": "apply.workable.com",
}


def api_host(board: Board) -> str:
    if board.system == "workday":
        tenant, wd, _ = board.slug.split("/")
        return f"{tenant}.{wd}.myworkdayjobs.com"
    return API_HOSTS[board.system]


def read_board(board: Board, settings: Settings, get: Get, known: set[str], *, now: datetime | None = None,
               sleep: Callable[[float], None] = time.sleep) -> BoardRead:
    """The board's jobs whose titles are wanted, each with its description. `known` holds the
    links the jobs list has, whose details are not read again. Raises Gone for a 404."""

    def ask(url: str, body: Any = None) -> Any:
        return _answer(get, board, sleep, url, body)

    try:
        return READERS[board.system](board, settings, ask, known, now or datetime.now(UTC))
    except SearchError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise SearchError(f"{board.system} {board.slug}: the answer is not the board's shape ({error!r})") from error


T = TypeVar("T")


def by_host(tasks: list[tuple[str, Callable[[], T]]], *, limit: int = HOST_LIMIT, workers: int = WORKERS) -> list[T | Exception]:
    """Run each task, at most `limit` at a time on one host and the hosts side by side; give
    each task's result, or its error, in the order given."""
    results: list[T | Exception | None] = [None] * len(tasks)
    queues: dict[str, deque[int]] = {}
    for number, (host, _) in enumerate(tasks):
        queues.setdefault(host, deque()).append(number)

    def lane(queue: deque[int]) -> None:
        # A host's lanes take its tasks in turn, so no more of them run at once than it has lanes.
        while True:
            try:
                number = queue.popleft()
            except IndexError:
                return
            try:
                results[number] = tasks[number][1]()
            except Exception as error:
                results[number] = error

    lanes = [queue for queue in queues.values() for _ in range(min(limit, len(queue)))]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(lanes)))) as pool:
        list(pool.map(lane, lanes))
    return results  # type: ignore[return-value]


# The whole search


Save = Callable[[FoundJob, "str | None"], bool]


@dataclass
class _Tally:
    boards: int = 0
    found: int = 0
    new: int = 0
    gone: int = 0
    failed: list[str] = field(default_factory=list)

    def line(self, system: str) -> str:
        said = [f"{self.boards} board{'' if self.boards == 1 else 's'}"]
        if self.gone:
            said.append(f"{self.gone} answered 404")
        if self.failed:
            said.append(f"{len(self.failed)} did not answer")
        return f"{system}: {self.found} found, {self.new} new ({', '.join(said)})"


@dataclass
class Search:
    get: Get
    pace: Pace
    companies: Companies
    sleep: Callable[[float], None] = time.sleep
    # The time LinkedIn's searches start at; None for the run's start (tests).
    clock: Callable[[], datetime] | None = None

    def run(self, settings: Settings, jobs: JobsList, started: datetime, save: Save, met: list[str]
            ) -> tuple[list[str], list[str]]:
        """Search LinkedIn and sweep the boards, saving each job through `save` (the finder's
        filters and saving); return a line per source and what failed. `met` are the links the
        alerts led to, whose boards join the index."""
        lines: list[str] = []
        failed: list[str] = []
        self.companies.add(wanted_boards(settings))
        if self.companies.index_due():
            line, problems = fill_index(self.companies, self.get, sleep=self.sleep)
            lines.append(line)
            failed += [f"companies: {problem}" for problem in problems]

        since = jobs.last_run("linkedin_search") or started - FIRST_RUN
        now = self.clock() if self.clock else started
        linkedin = linkedin_search(settings, since - LINKEDIN_OVERLAP, now, self.get, self.pace)
        new = sum(save(job, None) for job in linkedin.jobs)
        lines.append(f"linkedin_search: {len(linkedin.jobs)} found, {new} new")
        failed += [f"linkedin_search: {problem}" for problem in linkedin.problems]
        # LinkedIn may say stop to the searches, or later to a job page the saving read.
        stopped = linkedin.stopped or self.pace.stopped
        if stopped:
            failed.append(f"linkedin_search: {stopped}")
        if not stopped and not linkedin.problems:
            jobs.finished_run(started, "linkedin_search")

        # The boards the alerts' and LinkedIn's jobs led to, LinkedIn's saved just above.
        self.companies.add([board for link in met if (board := board_of(link)) is not None])
        full = self.companies.full_due()
        boards = self.companies.to_sweep(full=full)
        known = jobs.known_links()
        read = by_host([(api_host(board), lambda board=board: read_board(board, settings, self.get, known, now=started,
                                                                           sleep=self.sleep))
                        for board in boards])
        tally = {system: _Tally() for system in SYSTEMS}
        for board, result in zip(boards, read):
            counted = tally[board.system]
            counted.boards += 1
            if isinstance(result, Gone):
                counted.gone += 1
                self.companies.missed(board)
            elif isinstance(result, Exception):
                counted.failed.append(f"{board.slug}: {result}")
            else:
                self.companies.answered(board, result.newest)
                counted.found += len(result.jobs)
                counted.new += sum(save(each.job, each.description) for each in result.jobs)
        for system, counted in tally.items():
            lines.append(counted.line(system))
            if counted.failed and len(counted.failed) == counted.boards:
                failed.append(f"{system}: no board answered ({counted.failed[0]})")
        if full:
            self.companies.swept_full()
        return lines, failed
