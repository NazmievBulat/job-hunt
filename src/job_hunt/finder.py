"""The finder: job alert emails, and the job sites' own searches, into the jobs list.

`claude -p` on Claude Haiku runs the `read-job-alerts` skill, allowed only to read the mailbox
through the Gmail connector and to write one file. The script walks the mailbox, not the model:
a first run only searches, and lists the alert emails since the last run,

    {"alerts": [{"email_id": "...", "email_date": "2026-11-17T07:04:00+00:00"}]}

then one run for each batch of at most `BATCH_EMAILS` of them, oldest first, is named the ids
to read, may only open those emails one at a time (`get_message`, never the whole thread an
alert's HTML fills), and writes a file of its own:

    {"emails": 4,
     "jobs": [{"link": "...", "title": "...", "company": "...", "place": "Durham, NC",
               "remote": "remote" | "hybrid" | "on_site", "board": "linkedin",
               "email_id": "...", "email_date": "2026-11-17T07:04:00+00:00",
               "area": "<one of the settings' areas>" | null}]}

Each batch's jobs are saved as soon as it ends, and the batches' files are merged into the
first run's. A batch that fails is named and the others still count; the time the next run reads
from moves only to the newest email of the batches that all succeeded in order, so a failed batch
is read again.

Given `messages` (`replies.EmailMessages`), the search run also notes the recruiters' emails since
the last time they were read (`"recruiter_emails"`, `"linkedin_notifications"`), which `replies`
saves as threads to answer.

Everything after that is this script: it drops jobs the list already has, titles that hold
none of the settings' titles, and jobs the area and remote settings leave out, saves the rest
as `new`, and saves each one's page as `jobs/<id>/posting.md`.

The searches of `search.py` (LinkedIn signed out, and every company's public job board) hand
their jobs to the same filters and saving, and so do the public job feeds of `feeds.py`; a
board's job comes with its description, so its page is not read, as does a feed's but Adzuna's
and Jooble's. The sources run at once, each in a thread of its own: the alert emails, LinkedIn,
the boards (with the company index first when it is due) and the feeds. One `save` takes every
job, holding one lock around the check and the insert, and the jobs list is used under that lock
only. Once all have ended, the boards their links led to are swept in a second, short pass, and
then the recruiters' emails are linked to the jobs. Each source has its own line, its own time
of the last run and how long it took, and a source that fails is named without stopping the
others.

A job is the same job as one the list has by its link, or by its company and title
(`JobsList.same_job`); such a one is not saved again, its link is kept as another link of the
first, and the run says how many there were.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, get_args
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

from job_hunt.jobs import FoundJob, Job, JobsList, Remote, clean_link
from job_hunt.settings import Settings

if TYPE_CHECKING:
    from job_hunt.feeds import Feeds
    from job_hunt.search import Search, Sweep

MODEL = "claude-haiku-5-5"
SKILL = "read-job-alerts"
GMAIL_SEARCH = "mcp__claude_ai_Gmail__search_threads"
GMAIL_GET_MESSAGE = "mcp__claude_ai_Gmail__get_message"
# The recruiters' emails are read whole, thread and all; the alerts never are.
GMAIL_READ_TOOLS = (GMAIL_SEARCH, "mcp__claude_ai_Gmail__get_thread", GMAIL_GET_MESSAGE)
# An alert's plain text is a few thousand characters: ten fit a run with room to spare.
BATCH_EMAILS = 10
# Every background run: `--allowedTools` only adds to what the person's own settings allow (their
# `Bash`, a plugin's browser, auto mode), so the run loads no user or local settings, and
# `dontAsk` refuses any tool its command does not name. `dontAsk` still lets WebFetch reach Claude
# Code's preapproved documentation sites, itself or through a subagent, so those three are taken
# away. Claude Code lets any run read its working folder, by Read or a read-only command, without
# a rule, so a run starts in `RUN_FOLDER`, which holds nothing, and every path it is given is
# absolute.
ONLY_NAMED_TOOLS = (
    "--setting-sources", "project", "--permission-mode", "dontAsk",
    "--disallowedTools", "WebFetch,WebSearch,Agent",
)
# Inside the project, so Claude Code still finds `.claude/skills` above it, and `uv run` the project.
RUN_FOLDER = ".background"
FIRST_RUN = timedelta(days=14)
SKILL_MINUTES = 30
PAGE_SECONDS = 20
PAGE_BYTES = 5_000_000
REMOTE_WORDS = {"remote": "remote", "hybrid": "hybrid", "on_site": "on site"}
TEXT_FIELDS = ("link", "title", "company", "board", "email_id", "email_date")
# Before every email: what an email without a date is put after.
EARLIEST = datetime.min.replace(tzinfo=UTC)


class Messages(Protocol):
    """Where the recruiters' emails the run noted go (`replies.EmailMessages`)."""

    def since(self, now: datetime) -> datetime: ...

    def saved(self, data: dict, started: datetime) -> list[str]: ...


class FinderError(RuntimeError):
    """The finder run failed; the next run reads the mail from the same time again."""


class PageError(RuntimeError):
    """Why a job page could not be read."""


@dataclass
class Summary:
    emails: int
    jobs: int
    new: int
    # The alert entries left out, and why.
    problems: list[str] = field(default_factory=list)
    # One line per source that ran: how many it found and how many were new.
    lines: list[str] = field(default_factory=list)
    # Each source that failed, with its error.
    failed: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "\n".join(self.lines)


def _words(text: str) -> set[str]:
    # "Sr." is "sr", "QA-Automation" is "qa" and "automation"; C++ and C# keep their marks.
    return set(re.findall(r"[a-z0-9]+[+#]*", text.casefold()))


def title_matches(title: str, wanted: list[str]) -> bool:
    words = _words(title)
    return any(_words(each) <= words for each in wanted)


def place_kept(job: FoundJob, settings: Settings) -> bool:
    if job.remote == "remote":
        return settings.remote != "exclude"
    return settings.remote != "only" and job.area is not None


def _found_job(entry: Any, areas: dict[str, str]) -> FoundJob:
    if not isinstance(entry, dict):
        raise ValueError("is not an object")
    for name in TEXT_FIELDS:
        if not isinstance(entry.get(name), str) or not entry[name].strip():
            raise ValueError(f"has no {name}")
    if not entry["link"].startswith(("https://", "http://")):
        raise ValueError(f"has a link that is not a web address: {entry['link']!r}")
    try:
        clean_link(entry["link"])
    except ValueError as error:
        raise ValueError(f"has a link that is not a web address: {entry['link']!r} ({error})") from error
    if entry.get("remote") not in get_args(Remote):
        raise ValueError(f"has remote {entry.get('remote')!r}, not one of {', '.join(get_args(Remote))}")
    place, area = entry.get("place"), entry.get("area")
    return FoundJob(
        link=entry["link"].strip(),
        title=entry["title"].strip(),
        company=entry["company"].strip(),
        place=place.strip() if isinstance(place, str) else "",
        remote=entry["remote"],
        board=entry["board"].strip().casefold(),
        email_id=entry["email_id"].strip(),
        email_date=entry["email_date"].strip(),
        # Only an area of the settings, as the settings spell it; anything else is none.
        area=areas.get(area.strip().casefold()) if isinstance(area, str) else None,
    )


def read_found(path: Path, settings: Settings) -> tuple[int, list[FoundJob], list[str]]:
    """The emails read, the jobs, and why each entry left out was left out."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise FinderError(f"the {SKILL} run wrote a file that is not JSON: {error}") from error
    if not isinstance(data, dict) or type(data.get("emails")) is not int or not isinstance(data.get("jobs"), list):
        raise FinderError(f'the {SKILL} run wrote a file without "emails" and a list of "jobs"')
    areas = {area.casefold(): area for area in settings.areas}
    jobs: list[FoundJob] = []
    problems: list[str] = []
    for number, entry in enumerate(data["jobs"], start=1):
        try:
            jobs.append(_found_job(entry, areas))
        except ValueError as error:
            title = entry.get("title") if isinstance(entry, dict) else None
            problems.append(f"job {number} ({title or 'no title'}) {error}")
    return data["emails"], jobs, problems


class _Text(HTMLParser):
    """The text of the first element `wanted` accepts, one line per block, without scripts."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "header", "footer", "button", "form"}
    BLOCKS = {"p", "div", "li", "ul", "ol", "br", "tr", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed", "base"}

    def __init__(self, wanted: Callable[[str, dict[str, str]], bool]) -> None:
        super().__init__(convert_charrefs=True)
        self.wanted, self.done = wanted, False
        # The elements open inside the wanted one, it first, each with whether its text is skipped.
        self.open: list[tuple[str, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done or tag in self.VOID:
            if self.open and tag == "br":
                self.parts.append("\n")
            return
        if self.open:
            skipping = tag in self.SKIP or self.open[-1][1]
            self.open.append((tag, skipping))
            if not skipping and tag in self.BLOCKS:
                self.parts.append("\n- " if tag == "li" else "\n")
        elif self.wanted(tag, {name: value or "" for name, value in attrs}):
            self.open.append((tag, False))

    def handle_endtag(self, tag: str) -> None:
        # An end tag also closes what HTML leaves open inside it (`<li>` without `</li>`); one
        # with no open start tag is ignored.
        if all(name != tag for name, _ in self.open):
            return
        while True:
            name, skipping = self.open.pop()
            if not skipping and name in self.BLOCKS:
                self.parts.append("\n")
            if name == tag:
                break
        if not self.open:
            self.done = True

    def handle_data(self, data: str) -> None:
        if self.open and not self.open[-1][1]:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).split("\n"))
        return "\n".join(line for line in lines if line and line != "-")


def _classes(attrs: dict[str, str]) -> set[str]:
    return set(attrs.get("class", "").split())


# Where a board keeps the description; failing that, the page's main part, then its body.
BOARD_DESCRIPTION = (
    lambda tag, attrs: "show-more-less-html__markup" in _classes(attrs),
    lambda tag, attrs: attrs.get("id") == "jobDescriptionText",
)
PAGE_PARTS = (
    lambda tag, attrs: tag == "main",
    lambda tag, attrs: tag == "body",
)
SIGN_IN = re.compile(r"/(authwall|login|signup|uas/login|checkpoint)\b")
SIGN_IN_FORM = re.compile(r"""<form[^>]+action\s*=\s*["']?[^"'\s>]*/(authwall|login)""", re.IGNORECASE)


def _first_text(html: str, places: tuple[Callable[[str, dict[str, str]], bool], ...]) -> str:
    for wanted in places:
        parser = _Text(wanted)
        parser.feed(html)
        if text := parser.text():
            return text
    return ""


def html_text(fragment: str) -> str:
    """The text of a piece of HTML, as a page's is read: one line per block, lists with dashes."""
    return _first_text(f"<body>{fragment}</body>", (PAGE_PARTS[1],))


def page_text_and_kind(link: str, html: str) -> tuple[str, str]:
    """The description on a fetched page, and whether the job is `easy_apply` or `company_site`."""
    if SIGN_IN.search(urlsplit(link).path):
        raise PageError("the page asks to sign in")
    host = urlsplit(link).netloc.lower()
    linkedin = host == "linkedin.com" or host.endswith(".linkedin.com")
    board = linkedin or host == "indeed.com" or host.endswith(".indeed.com")
    text = _first_text(html, BOARD_DESCRIPTION)
    # A board's posting may carry an optional sign-in form; without the board's description it
    # is a wall. Another site has no known description block to tell the two apart, so its
    # sign-in form alone does not hide its posting.
    if not text and board and SIGN_IN_FORM.search(html):
        raise PageError("the page asks to sign in")
    text = text or _first_text(html, PAGE_PARTS)
    if not text:
        raise PageError("the page has no text")
    offsite = 'id="applyUrl"' in html or "apply-link-offsite" in html
    return text, "easy_apply" if linkedin and not offsite else "company_site"


def offsite_link(html: str) -> str | None:
    """The company's own apply address a LinkedIn job page holds, or None. LinkedIn may wrap it
    in its own redirect (`/jobs/view/externalApply/...?url=...`)."""
    found = re.search(r'id="applyUrl"[^>]*>\s*<!--\s*"([^"]+)"', html)
    if found is None:
        return None
    link = html_unescape(found.group(1))
    parts = urlsplit(link)
    if parts.netloc.lower().endswith("linkedin.com"):
        link = dict(parse_qsl(parts.query)).get("url", "")
    return link if link.startswith(("https://", "http://")) else None


def fetch_page(link: str) -> tuple[str, str]:
    """The address the page ended at, and its HTML."""
    request = Request(link, headers={"User-Agent": "Mozilla/5.0 (compatible; job-hunt)", "Accept": "text/html"})
    try:
        with urlopen(request, timeout=PAGE_SECONDS) as response:
            body = response.read(PAGE_BYTES)
            url = response.geturl()
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as error:
        raise PageError(f"HTTP {error.code}") from error
    except TimeoutError as error:
        raise PageError(f"no answer in {PAGE_SECONDS} seconds") from error
    except (URLError, OSError) as error:
        raise PageError(str(getattr(error, "reason", error))) from error
    try:
        return url, body.decode(charset, errors="replace")
    except LookupError:
        # A charset Python does not know; most pages are UTF-8 whatever they say.
        return url, body.decode("utf-8", errors="replace")


def skill_command(since: datetime | None, out: Path, messages_after: datetime | None = None) -> list[str]:
    """The search run: it lists the alert emails since `since`, or none without it; with
    `messages_after` it also notes the recruiters' emails and LinkedIn's message notifications
    since then (`replies`)."""
    arguments: dict[str, Any] = {"after": int(since.timestamp()) if since else None,
                                 "since": since.isoformat() if since else None, "out": str(out)}
    if messages_after is not None:
        arguments["messages_after"] = int(messages_after.timestamp())
    return _command(arguments, GMAIL_READ_TOOLS if messages_after is not None else (GMAIL_SEARCH,), out)


def batch_command(email_ids: list[str], areas: list[str], out: Path) -> list[str]:
    """A batch run: it reads the jobs of the alert emails it is named, and only those."""
    return _command({"email_ids": email_ids, "areas": areas, "out": str(out)}, (GMAIL_GET_MESSAGE,), out)


def _command(arguments: dict[str, Any], tools: tuple[str, ...], out: Path) -> list[str]:
    # A permission rule's path: `//` starts an absolute one. Edit rules cover every tool that
    # writes a file, the Write tool among them.
    return [
        "claude", "-p", f"/{SKILL} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL, *ONLY_NAMED_TOOLS,
        "--allowedTools", ",".join([*tools, f"Edit(/{out})"]),
    ]


def run_folder(root: Path) -> Path:
    """Where a background run starts: an empty folder of the project, so the run can read only
    the files its rules name, the project's own included."""
    folder = root / RUN_FOLDER
    folder.mkdir(exist_ok=True)
    return folder


def run_claude(
    command: list[str], folder: Path, *, skill: str = SKILL, error: type[RuntimeError] = FinderError,
    minutes: int = SKILL_MINUTES,
) -> subprocess.CompletedProcess[str]:
    """Run a skill in the project `folder`'s run folder; a run that fails raises `error` saying why."""
    here = run_folder(folder)
    # A file left there could be read by every run without a rule: nothing starts until it is gone.
    if left := sorted(path.name for path in here.iterdir()):
        raise error(f"the {skill} run did not start: {here} holds {', '.join(left)}, and it must be empty")
    try:
        done = subprocess.run(command, cwd=here, capture_output=True, text=True, timeout=minutes * 60)
    except subprocess.TimeoutExpired as timeout:
        raise error(f"the {skill} run did not finish in {minutes} minutes") from timeout
    except FileNotFoundError as missing:
        raise error("claude is not installed or not on the PATH") from missing
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip()).splitlines()
        raise error(f"the {skill} run failed ({done.returncode}): {said[-1] if said else 'no message'}")
    return done


def _posting(job: Job) -> str:
    where = " · ".join(part for part in (job.company, job.place, REMOTE_WORDS.get(job.remote or "", "")) if part)
    body = job.description or f"The job page could not be read ({job.page_problem}), so this is only what the alert gave."
    return f"# {job.title}\n\n{where}\n{job.link}\n\n{body}\n"


def _write_posting(job: Job, folder: Path) -> None:
    folder = folder / str(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    # Written whole or not at all: a posting cut short would pass for written on every next run.
    part = folder / "posting.md.part"
    part.write_text(_posting(job))
    part.replace(folder / "posting.md")


def _read_page(
    link: str, fetch: Callable[[str], tuple[str, str]]
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """The address the page ended at, its description, the kind, why it could not be read, and
    the company's apply address a LinkedIn page names."""
    try:
        ended_at, html = fetch(link)
        text, kind = page_text_and_kind(ended_at, html)
        return ended_at, text, kind, None, offsite_link(html)
    except Exception as error:
        # Whatever went wrong with its page, the job is saved with the reason, never dropped.
        problem = str(error) if isinstance(error, PageError) else f"{type(error).__name__}: {error}"
        return None, None, None if urlsplit(link).netloc.endswith("linkedin.com") else "company_site", problem, None


def _now() -> datetime:
    return datetime.now(UTC)


def find(
    settings: Settings,
    jobs: JobsList,
    jobs_folder: Path,
    found: Path,
    *,
    run: Callable[[list[str]], None],
    fetch: Callable[[str], tuple[str, str]],
    clock: Callable[[], datetime] = _now,
    search: Search | None = None,
    messages: Messages | None = None,
    feeds: Feeds | None = None,
) -> Summary:
    # One run at a time: two would write one jobs file, and one could save the other's jobs and
    # move the time the next run reads from past alerts it never saved.
    with found.with_name(found.name + ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FinderError("another hunt find is already running") from error
        return _find(settings, jobs, jobs_folder, found, run=run, fetch=fetch, clock=clock, search=search,
                     messages=messages, feeds=feeds)


class _Locked:
    """The jobs list as the sources' threads see it: one call at a time on its one connection."""

    def __init__(self, jobs: JobsList, lock: threading.RLock) -> None:
        self._jobs, self._lock = jobs, lock

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._jobs, name)
        if not callable(attribute):
            return attribute

        def locked(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return locked


def took(seconds: float) -> str:
    """How long a source took, as "1h 2m", "4m 10s" or "45s"."""
    whole = max(0, round(seconds))
    hours, minutes, rest = whole // 3600, whole // 60 % 60, whole % 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {rest}s" if minutes else f"{rest}s"


def _find(
    settings: Settings,
    jobs: JobsList,
    jobs_folder: Path,
    found: Path,
    *,
    run: Callable[[list[str]], None],
    fetch: Callable[[str], tuple[str, str]],
    clock: Callable[[], datetime],
    search: Search | None,
    messages: Messages | None,
    feeds: Feeds | None,
) -> Summary:
    started = clock()
    lock = threading.RLock()
    shared = cast(JobsList, _Locked(jobs, lock))
    # The links the jobs met, whose job boards join the company index; the links whose pages are
    # being read; and the jobs another source had already found under another link.
    met: list[str] = []
    reading: set[str] = set()
    same: list[str] = []

    def known(each: FoundJob, page_link: str | None = None) -> bool:
        """Whether the list has the job, by its link or as the same job; under the lock."""
        if jobs.has(each.link):
            return True
        if (first := jobs.same_job(each)) is None:
            return False
        jobs.also_link(first, each, page_link)
        same.append(each.link)
        # Its board may be one the index does not have yet.
        met.extend(page for page in (each.link, page_link) if page)
        return True

    def save(each: FoundJob, description: str | None) -> bool:
        """Save a job the filters keep, with its page read unless its description came with it;
        return whether it is new. Two sources saving one job at once save it once."""
        if not title_matches(each.title, settings.titles) or not place_kept(each, settings):
            return False
        link = clean_link(each.link)
        with lock:
            if link in reading or known(each):
                return False
            if description is not None:
                return jobs.add(each, description=description, kind="company_site") is not None
            reading.add(link)
        try:
            ended_at, text, kind, problem, offsite = _read(link)
        finally:
            with lock:
                reading.discard(link)
        with lock:
            met.extend(page for page in (link, ended_at, offsite) if page)
            # Another source may have saved the same job, under another link, while the page was read.
            if known(each, ended_at):
                return False
            return jobs.add(each, page_link=ended_at, description=text, kind=kind, problem=problem) is not None

    def _read(link: str) -> tuple[str | None, str | None, str | None, str | None, str | None]:
        if search is None:
            return _read_page(link, fetch)
        if not urlsplit(link).netloc.endswith("linkedin.com"):
            # A board's host takes no more requests at once than the sweep gives it.
            with search.hosts.slot(urlsplit(link).hostname or ""):
                return _read_page(link, fetch)
        # LinkedIn's pages count as its requests, paced as its searches are, one at a time.
        with search.pace.turn():
            if search.pace.stopped:
                # LinkedIn said stop: the job is saved without its page, as one whose page failed.
                return None, None, None, f"not read: {search.pace.stopped}", None
            read = _read_page(link, fetch)
            if read[3] in ("HTTP 429", "HTTP 999"):
                search.pace.stop(read[3].removeprefix("HTTP "))
            return read

    def alerts() -> _Alerts:
        return _read_alerts(settings, shared, found, run, started, messages_after, save)

    # Read here: the recruiters' threads are not used from the sources' threads.
    messages_after = messages.since(started) if messages else None
    sources: dict[str, Callable[[], Any]] = {"alert": alerts}
    if search is not None:
        sources["linkedin_search"] = lambda: search.linkedin(settings, shared, started, save)
        sources["boards"] = lambda: search.sweep(settings, shared, started, save)
    if feeds is not None:
        sources["feeds"] = lambda: feeds.run(settings, shared, started, save)
    seconds: dict[str, float] = {}

    def timed(name: str) -> Any:
        begun = clock()
        try:
            return sources[name]()
        except Exception as error:
            return error
        finally:
            seconds[name] = (clock() - begun).total_seconds()

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        ended = dict(zip(sources, pool.map(timed, sources)))

    summary = Summary(0, 0, 0)
    alert = ended["alert"]
    if isinstance(alert, FinderError):
        summary.failed.append(f"alert: {alert}")
    elif isinstance(alert, Exception):
        # Whatever broke in the alerts, the other sources' jobs still get their postings.
        summary.failed.append(f"alert: {type(alert).__name__}: {alert}")
    else:
        summary.emails, summary.jobs, summary.new = alert.emails, alert.jobs, alert.new
        summary.problems, summary.failed = alert.problems, alert.failed
        # After every source's jobs: a message about a job of this run is linked to it.
        if messages is not None:
            summary.problems += messages.saved(alert.data, started)
        if alert.through is not None:
            jobs.finished_run(alert.through)
        summary.lines.append(f"alert: {summary.emails} emails, {summary.jobs} jobs, {summary.new} new")
    if search is not None:
        sweep: Sweep | Exception = ended["boards"]
        if not isinstance(sweep, Exception):
            summary.lines += sweep.lines
        _add(summary, "linkedin_search", ended["linkedin_search"])
        if search.pace.stopped:
            summary.failed.append(f"linkedin_search: {search.pace.stopped}")
        if isinstance(sweep, Exception):
            # Whatever broke in the sweep, the other sources' jobs still get their postings.
            summary.failed.append(f"boards: {type(sweep).__name__}: {sweep}")
        else:
            begun = clock()
            try:
                _add(summary, "boards", search.sweep_met(sweep, settings, jobs, started, save, met))
            except Exception as error:
                _add(summary, "boards", error)
            seconds["boards"] += (clock() - begun).total_seconds()
    if feeds is not None:
        _add(summary, "feeds", ended["feeds"])
    summary.lines.append(f"same job from two sources: {len(same)}")
    summary.lines.append("took: " + ", ".join(f"{name} {took(seconds[name])}" for name in sources))
    # Every new job has its posting, also one whose run stopped after saving it.
    for job in jobs.all():
        if job.state == "new" and not (jobs_folder / str(job.id) / "posting.md").exists():
            _write_posting(job, jobs_folder)
    return summary


def _add(summary: Summary, name: str, ended: tuple[list[str], list[str]] | Exception) -> None:
    """A source's lines and failures, or, when it broke, why."""
    if isinstance(ended, Exception):
        summary.failed.append(f"{name}: {type(ended).__name__}: {ended}")
    else:
        summary.lines += ended[0]
        summary.failed += ended[1]


@dataclass
class _Alerts:
    emails: int
    jobs: int
    new: int
    problems: list[str]
    # Each batch that failed, with why.
    failed: list[str]
    # The time the next run reads from, or None to read from the same time again.
    through: datetime | None
    # The search's file with every batch's jobs merged in, as one run would have written it.
    data: dict[str, Any]


def _email_date(text: Any) -> datetime | None:
    try:
        date = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return None if date is None else (date if date.tzinfo else date.replace(tzinfo=UTC)).astimezone(UTC)


def alert_emails(listed: list[Any]) -> tuple[list[tuple[str, datetime | None]], list[str]]:
    """The alert emails the search listed, each once, oldest first and those without a date last;
    and why each entry left out was left out."""
    emails: dict[str, datetime | None] = {}
    problems: list[str] = []
    for number, entry in enumerate(listed, start=1):
        email_id = entry.get("email_id") if isinstance(entry, dict) else None
        if not isinstance(email_id, str) or not email_id.strip():
            problems.append(f"alert email {number} has no email_id")
            continue
        date = _email_date(entry.get("email_date"))
        emails[email_id.strip()] = emails.get(email_id.strip()) or date
    order = sorted(emails.items(), key=lambda each: (each[1] is None, each[1] or EARLIEST))
    return order, problems


def _write_whole(path: Path, data: dict[str, Any]) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    part.replace(path)


def _read_alerts(
    settings: Settings, jobs: JobsList, found: Path, run: Callable[[list[str]], None], started: datetime,
    messages_after: datetime | None, save: Callable[[FoundJob, str | None], bool],
) -> _Alerts:
    """The alert emails since the alerts' last run, listed by one run and read in batches, each
    batch's jobs saved as it ends."""
    since = jobs.last_run() or started - FIRST_RUN
    # A file an earlier run left must not pass for this run's.
    found.unlink(missing_ok=True)
    run(skill_command(since, found, messages_after))
    if not found.exists():
        raise FinderError(f"the {SKILL} search wrote no file ({found})")
    try:
        data = json.loads(found.read_text())
    except json.JSONDecodeError as error:
        raise FinderError(f"the {SKILL} search wrote a file that is not JSON: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        raise FinderError(f'the {SKILL} search wrote a file without a list of "alerts"')
    emails, problems = alert_emails(data["alerts"])
    batches = [emails[start:start + BATCH_EMAILS] for start in range(0, len(emails), BATCH_EMAILS)]
    done = _Alerts(0, 0, 0, problems, [], started, data)
    read: list[FoundJob] = []
    # Whether every batch so far succeeded, and the newest email they read.
    in_order, newest = True, None
    for number, batch in enumerate(batches, start=1):
        out = found.with_name(f"{found.stem}-{number}{found.suffix}")
        out.unlink(missing_ok=True)
        try:
            run(batch_command([email_id for email_id, _ in batch], settings.areas, out))
            if not out.exists():
                raise FinderError(f"the {SKILL} batch wrote no file ({out})")
            count, found_jobs, left_out = read_found(out, settings)
        except Exception as error:
            said = str(error) if isinstance(error, FinderError) else f"{type(error).__name__}: {error}"
            done.failed.append(f"alert: batch {number} of {len(batches)} failed: {said}")
            if in_order:
                # A second before it: the search's `after:` may leave out an email of that very second.
                done.through = newest - timedelta(seconds=1) if newest else None
                in_order = False
            if any(date is None for _, date in batch):
                # An email without a date may be older than any the batches before read.
                done.through = None
            continue
        finally:
            out.unlink(missing_ok=True)
        if in_order:
            newest = max([newest, *(date for _, date in batch)], key=lambda date: date or EARLIEST)
        done.problems += [f"batch {number} {problem}" if len(batches) > 1 else problem for problem in left_out]
        done.emails += count
        read += found_jobs
        done.new += sum(save(each, None) for each in found_jobs)
    done.jobs = len(read)
    data = {name: value for name, value in data.items() if name != "alerts"}
    data.update(emails=done.emails, jobs=[asdict(each) for each in read])
    _write_whole(found, data)
    done.data = data
    return done
