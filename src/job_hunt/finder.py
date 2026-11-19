"""The finder: job alert emails, and the job sites' own searches, into the jobs list.

One `claude -p` on Claude Haiku runs the `read-job-alerts` skill, allowed only to read the
mailbox through the Gmail connector and to write one file:

    {"emails": 4,
     "jobs": [{"link": "...", "title": "...", "company": "...", "place": "Durham, NC",
               "remote": "remote" | "hybrid" | "on_site", "board": "linkedin",
               "email_id": "...", "email_date": "2026-11-17T07:04:00+00:00",
               "area": "<one of the settings' areas>" | null}]}

Everything after that is this script: it drops jobs the list already has, titles that hold
none of the settings' titles, and jobs the area and remote settings leave out, saves the rest
as `new`, and saves each one's page as `jobs/<id>/posting.md`.

Then the searches of `search.py` (LinkedIn signed out, and every company's public job board)
hand their jobs to the same filters and saving; a board's job comes with its description, so
its page is not read. Each source has its own line and its own time of the last run, and a
source that fails is named without stopping the others.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from job_hunt.jobs import FoundJob, Job, JobsList, Remote, clean_link
from job_hunt.settings import Settings

if TYPE_CHECKING:
    from job_hunt.search import Search

MODEL = "claude-haiku-5-5"
SKILL = "read-job-alerts"
GMAIL_READ_TOOLS = (
    "mcp__claude_ai_Gmail__search_threads",
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__get_message",
)
FIRST_RUN = timedelta(days=14)
SKILL_MINUTES = 30
PAGE_SECONDS = 20
PAGE_BYTES = 5_000_000
REMOTE_WORDS = {"remote": "remote", "hybrid": "hybrid", "on_site": "on site"}
TEXT_FIELDS = ("link", "title", "company", "board", "email_id", "email_date")


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


def skill_command(since: datetime, areas: list[str], out: Path) -> list[str]:
    arguments = {"after": int(since.timestamp()), "since": since.isoformat(), "areas": areas, "out": str(out)}
    # A permission rule's path: `//` starts an absolute one, `./` one in the working folder.
    # Edit rules cover every tool that writes a file, the Write tool among them.
    write = f"Edit(/{out})" if out.is_absolute() else f"Edit(./{out})"
    return [
        "claude", "-p", f"/{SKILL} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL,
        "--allowedTools", ",".join([*GMAIL_READ_TOOLS, write]),
    ]


def run_claude(
    command: list[str], folder: Path, *, skill: str = SKILL, error: type[RuntimeError] = FinderError
) -> None:
    """Run a skill in the project folder, where Claude Code finds `.claude/skills`; a run that
    fails raises `error` saying why."""
    try:
        done = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=SKILL_MINUTES * 60)
    except subprocess.TimeoutExpired as timeout:
        raise error(f"the {skill} run did not finish in {SKILL_MINUTES} minutes") from timeout
    except FileNotFoundError as missing:
        raise error("claude is not installed or not on the PATH") from missing
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip()).splitlines()
        raise error(f"the {skill} run failed ({done.returncode}): {said[-1] if said else 'no message'}")


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


def _read_page(link: str, fetch: Callable[[str], tuple[str, str]]) -> tuple[str | None, str | None, str | None, str | None]:
    """The address the page ended at, its description, the kind and why it could not be read."""
    try:
        ended_at, html = fetch(link)
        text, kind = page_text_and_kind(ended_at, html)
        return ended_at, text, kind, None
    except Exception as error:
        # Whatever went wrong with its page, the job is saved with the reason, never dropped.
        problem = str(error) if isinstance(error, PageError) else f"{type(error).__name__}: {error}"
        return None, None, None if urlsplit(link).netloc.endswith("linkedin.com") else "company_site", problem


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
) -> Summary:
    # One run at a time: two would write one jobs file, and one could save the other's jobs and
    # move the time the next run reads from past alerts it never saved.
    with found.with_name(found.name + ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FinderError("another hunt find is already running") from error
        return _find(settings, jobs, jobs_folder, found, run=run, fetch=fetch, clock=clock, search=search)


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
) -> Summary:
    started = clock()
    # The links the jobs met, whose job boards join the company index.
    met: list[str] = []

    def save(each: FoundJob, description: str | None) -> bool:
        """Save a job the filters keep, with its page read unless its description came with it;
        return whether it is new."""
        if jobs.has(each.link) or not title_matches(each.title, settings.titles) or not place_kept(each, settings):
            return False
        if description is not None:
            return jobs.add(each, description=description, kind="company_site") is not None
        link = clean_link(each.link)
        linkedin = search is not None and urlsplit(link).netloc.endswith("linkedin.com")
        if linkedin and search.pace.stopped:
            # LinkedIn said stop: the job is saved without its page, as one whose page failed.
            return jobs.add(each, problem=f"not read: {search.pace.stopped}") is not None
        if linkedin:
            # LinkedIn's pages count as its requests, paced as its searches are.
            search.pace.wait()
        ended_at, text, kind, problem = _read_page(link, fetch)
        if linkedin and problem in ("HTTP 429", "HTTP 999"):
            search.pace.stop(problem.removeprefix("HTTP "))
        met.extend(page for page in (link, ended_at) if page)
        return jobs.add(each, page_link=ended_at, description=text, kind=kind, problem=problem) is not None

    summary = Summary(0, 0, 0)
    try:
        summary.emails, found_jobs, summary.problems = _read_alerts(settings, jobs, found, run, started)
        summary.jobs = len(found_jobs)
        summary.new = sum(save(each, None) for each in found_jobs)
        jobs.finished_run(started)
        summary.lines.append(f"alert: {summary.emails} emails, {summary.jobs} jobs, {summary.new} new")
    except FinderError as error:
        summary.failed.append(f"alert: {error}")
    if search is not None:
        try:
            lines, failed = search.run(settings, jobs, started, save, met)
        except Exception as error:
            # Whatever broke in the searches, the alerts' jobs still get their postings.
            lines, failed = [], [f"search: {type(error).__name__}: {error}"]
        summary.lines += lines
        summary.failed += failed
    # Every new job has its posting, also one whose run stopped after saving it.
    for job in jobs.all():
        if job.state == "new" and not (jobs_folder / str(job.id) / "posting.md").exists():
            _write_posting(job, jobs_folder)
    return summary


def _read_alerts(
    settings: Settings, jobs: JobsList, found: Path, run: Callable[[list[str]], None], started: datetime
) -> tuple[int, list[FoundJob], list[str]]:
    """The alert emails since the alerts' last run, as the skill read them."""
    since = jobs.last_run() or started - FIRST_RUN
    # A file an earlier run left must not pass for this run's.
    found.unlink(missing_ok=True)
    run(skill_command(since, settings.areas, found))
    if not found.exists():
        raise FinderError(f"the {SKILL} run wrote no jobs file ({found})")
    return read_found(found, settings)
