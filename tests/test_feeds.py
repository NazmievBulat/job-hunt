"""`hunt find`'s public job feeds: Remotive, RemoteOK, Arbeitnow, Hacker News' "Who is hiring?",
and with a key USAJobs, Adzuna and Jooble.

No network and no model: every answer is a made-up one under `tests/feeds/`, shaped after the
real ones (the no-key feeds checked 2026-11-19; the keyed ones after their documentation),
served by `Feeds` in place of `http_get`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from job_hunt import cli
from job_hunt.feeds import (
    ARBEITNOW, FEEDS, HN_ITEM, HN_STORIES, HN_UNKNOWN_COMPANY, REMOTEOK, REMOTIVE, Feeds, hn_job, skipped,
)
from job_hunt.finder import find
from job_hunt.jobs import JobsList
from job_hunt.search import Answer, SearchError
from job_hunt.settings import Settings, SettingsError, read_settings
from movable_clock import MovableClock

HERE = Path(__file__).parent / "feeds"
FINDER = Path(__file__).parent / "finder"
KEYS = {"usajobs": {"email": "ada@example.com", "key": "usa-key"}, "adzuna": {"app_id": "ad-id", "app_key": "ad-key"},
        "jooble": {"key": "jo-key"}}
USAJOBS = "https://data.usajobs.gov/api/search"
ADZUNA = "https://api.adzuna.com/v1/api/jobs/us/search/"
JOOBLE = "https://jooble.org/api/jo-key"
STARTED = datetime(2026, 11, 18, 7, 0, tzinfo=UTC)


def _ok(name: str) -> Answer:
    return Answer(200, (HERE / name).read_text())


class Sites:
    """Stands in for the network: an answer for each address, by its start; anything else 404.
    Remembers each request with its body and headers."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, object, dict[str, str] | None]] = []

    def __call__(self, url: str, body: object = None, headers: dict[str, str] | None = None) -> Answer:
        self.asked.append((url, body, headers))
        for start, answer in self.answers.items():
            if url.startswith(start):
                return answer
        return Answer(404, "Not Found")

    def urls(self, start: str) -> list[str]:
        return [url for url, _, _ in self.asked if url.startswith(start)]


def _sites(**change: Answer) -> Sites:
    answers = {
        REMOTIVE: _ok("remotive.json"),
        REMOTEOK: _ok("remoteok.json"),
        f"{ARBEITNOW}?page=2": _ok("arbeitnow-2.json"),
        ARBEITNOW: _ok("arbeitnow-1.json"),
        HN_STORIES: _ok("hn-stories.json"),
        f"{HN_ITEM}9001": _ok("hn-thread.json"),
        USAJOBS: _ok("usajobs.json"),
        ADZUNA: _ok("adzuna.json"),
        JOOBLE: _ok("jooble.json"),
    }
    names = {"remotive": REMOTIVE, "remoteok": REMOTEOK, "arbeitnow": ARBEITNOW, "usajobs": USAJOBS,
             "adzuna": ADZUNA, "jooble": JOOBLE, "hn": HN_STORIES}
    return Sites(answers | {names[name]: answer for name, answer in change.items()})


@pytest.fixture
def settings() -> Settings:
    return replace(read_settings(FINDER / "settings.json"), keys=KEYS)


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(STARTED)


def _read(name: str, settings: Settings, sites: Sites | None = None, since: datetime = STARTED - timedelta(days=14)):
    return Feeds(get=sites or _sites(), sleep=lambda seconds: None).read(name, settings, since, STARTED)


def _jobs(read) -> list[tuple[str, str, str, str, str | None]]:
    return [(job.title, job.company, job.place, job.remote, job.area) for job, _ in read.jobs]


# The feeds without a key


def test_remotive_gives_its_wanted_remote_jobs_with_their_descriptions(settings: Settings) -> None:
    read = _read("remotive", settings)
    assert _jobs(read) == [
        ("Senior QA Automation Engineer", "Example Labs", "USA", "remote", None),
        ("SDET & Test Lead", "Sample Co", "Worldwide", "remote", None),
    ]
    job, description = read.jobs[0]
    assert (job.link, job.source, job.board, job.email_id) == (
        "https://remotive.com/remote-jobs/qa/senior-qa-automation-engineer-101", "remotive", "remotive", None)
    assert description == "Write Playwright tests for our checkout.\n- Python\n- CI"


def test_remoteok_skips_its_legal_notice_and_keeps_the_wanted_job(settings: Settings) -> None:
    read = _read("remoteok", settings)
    assert _jobs(read) == [("QA Automation Engineer", "Example Remote", "United States", "remote", None)]
    job, description = read.jobs[0]
    assert job.link == "https://remoteOK.com/remote-jobs/remote-qa-automation-engineer-example-remote-201"
    assert description == "About the role\nAutomate our API tests."


def test_arbeitnow_reads_its_escaped_descriptions_and_pages_only_while_they_are_new(settings: Settings) -> None:
    sites = _sites()
    read = _read("arbeitnow", settings, sites)
    # Munich is in no area and not remote; the second page's job is older than the last run,
    # so the page after it is not asked for, but its jobs are still read.
    assert _jobs(read) == [
        ("QA Automation Engineer", "Example GmbH", "Berlin", "remote", None),
        ("SDET", "Example Inc", "Durham, NC", "on_site", "Raleigh-Durham-Chapel Hill"),
        ("SDET", "Beispiel AG", "Munich", "on_site", None),
        ("QA Automation Engineer", "Muster GmbH", "Hamburg", "remote", None),
    ]
    assert read.jobs[0][1] == "Test our platform."
    assert sites.urls(ARBEITNOW) == [ARBEITNOW, f"{ARBEITNOW}?page=2"]


def test_arbeitnow_stops_at_its_last_page_and_at_most_three_pages(settings: Settings) -> None:
    sites = _sites()
    sites.answers[f"{ARBEITNOW}?page=2"] = _ok("arbeitnow-last.json")
    read = _read("arbeitnow", settings, sites, since=datetime.fromtimestamp(1794900000, UTC))
    # The last page has no next one; the jobs read so far are kept.
    assert sites.urls(ARBEITNOW) == [ARBEITNOW, f"{ARBEITNOW}?page=2"] and len(read.jobs) == 4
    sites = _sites()
    # Every page new and another after it: the third is the last read.
    page = json.loads((HERE / "arbeitnow-1.json").read_text())
    sites.answers = {ARBEITNOW: Answer(200, json.dumps(page))}
    _read("arbeitnow", settings, sites, since=datetime.fromtimestamp(1794900000, UTC))
    assert len(sites.urls(ARBEITNOW)) == 3


def test_arbeitnow_reads_one_page_when_it_holds_a_job_from_before_the_last_run(settings: Settings) -> None:
    sites = _sites()
    _read("arbeitnow", settings, sites, since=datetime.fromtimestamp(1794955000, UTC))
    assert sites.urls(ARBEITNOW) == [ARBEITNOW]


def test_hacker_news_reads_the_latest_who_is_hiring_thread_one_comment_a_job(settings: Settings) -> None:
    sites = _sites()
    read = _read("hn_hiring", settings, sites)
    # The "wants to be hired" thread is passed over; the reply, the deleted comment and the data
    # scientist are not jobs of the settings.
    assert sites.urls("https://hn.algolia.com") == [HN_STORIES, f"{HN_ITEM}9001"]
    assert _jobs(read) == [
        ("QA Automation Engineer", "Example Robotics", "Durham, NC", "on_site", "Raleigh-Durham-Chapel Hill"),
        ("Senior SDET", "Sample Cloud", "REMOTE (US)", "remote", None),
        ("We are hiring an SDET to own our test suite, remote within the US.", HN_UNKNOWN_COMPANY, "", "remote",
         None),
    ]
    assert [job.link for job, _ in read.jobs] == [
        "https://news.ycombinator.com/item?id=9101", "https://news.ycombinator.com/item?id=9102",
        "https://news.ycombinator.com/item?id=9103"]
    assert read.jobs[0][1] == (
        "Example Robotics | QA Automation Engineer | Durham, NC | ONSITE | Full-time\n"
        "We build robot fleets and need someone to own their tests.\n"
        "Apply at https://example.com/jobs/qa\n\n"
        "Links in the comment:\n- https://example.com/jobs/qa")


def test_a_comment_the_code_cannot_read_is_kept_with_its_whole_text(settings: Settings) -> None:
    comment = {"id": 7, "text": "Hiring a QA Automation Engineer for our Durham lab, " + "and more words " * 20
               + "<p>Mail jobs@example.com."}
    job, description = hn_job(comment, settings)
    assert job.company == HN_UNKNOWN_COMPANY and job.remote == "on_site"
    assert job.title.startswith("Hiring a QA Automation Engineer") and job.title.endswith("…")
    assert len(job.title) <= 151
    # Its place is its first line, which names an area's city.
    assert job.area == "Raleigh-Durham-Chapel Hill"
    assert description.endswith("Mail jobs@example.com.") and "and more words and more words" in description


def test_a_hybrid_comment_is_hybrid_and_a_title_part_is_found_wherever_it_is(settings: Settings) -> None:
    job, _ = hn_job({"id": 8, "text": "Example Labs | Raleigh, NC | HYBRID | Lead SDET | Full-time"}, settings)
    assert (job.title, job.place, job.remote, job.area) == (
        "Lead SDET", "Raleigh, NC", "hybrid", "Raleigh-Durham-Chapel Hill")


@pytest.mark.parametrize("line", ["Example Labs | SDET | Raleigh, NC | NO REMOTE",
                                  "Example Labs | SDET | Raleigh, NC | ONSITE | not remote",
                                  "Example Labs | SDET | Raleigh, NC | non-remote",
                                  "Example Labs | SDET | Raleigh, NC | Remote: no",
                                  "Example Labs | SDET | Raleigh, NC | Remote = none",
                                  "Hiring an SDET in Raleigh, NC, no remote"])
def test_a_comment_that_says_not_remote_is_on_site(settings: Settings, line: str) -> None:
    job, _ = hn_job({"id": 9, "text": line}, settings)
    assert (job.remote, job.area) == ("on_site", "Raleigh-Durham-Chapel Hill")


@pytest.mark.parametrize(("line", "place"), [
    ("Example Labs | SDET | Remote (US) | no remote outside the US", "Remote (US)"),
    ("Example Labs | SDET | Remote no visa sponsorship", "Remote no visa sponsorship"),
])
def test_a_comment_remote_in_one_part_is_remote_whatever_another_part_says(
    settings: Settings, line: str, place: str
) -> None:
    job, _ = hn_job({"id": 12, "text": line}, settings)
    assert (job.remote, job.place) == ("remote", place)


def test_a_part_that_says_not_remote_is_no_place(settings: Settings) -> None:
    job, _ = hn_job({"id": 13, "text": "Example Labs | SDET | NO REMOTE"}, settings)
    assert (job.remote, job.place) == ("on_site", "")


def test_a_comment_of_two_parts_is_kept_as_text_and_a_part_that_is_no_place_is_not_its_place(
    settings: Settings
) -> None:
    job, _ = hn_job({"id": 10, "text": "Example Labs | SDET"}, settings)
    assert (job.company, job.title) == (HN_UNKNOWN_COMPANY, "Example Labs | SDET")
    job, _ = hn_job({"id": 11, "text": "Example Labs | SDET | Full-time | $150k"}, settings)
    assert (job.company, job.title, job.place, job.area) == ("Example Labs", "SDET", "", None)


def test_hacker_news_without_a_who_is_hiring_thread_fails(settings: Settings) -> None:
    sites = _sites(hn=Answer(200, json.dumps({"hits": [{"title": "Ask HN: Who wants to be hired?", "objectID": "1"}]})))
    with pytest.raises(Exception, match='no "Ask HN: Who is hiring\\?" thread'):
        _read("hn_hiring", settings, sites)


# The feeds with a key


def test_usajobs_asks_with_the_key_in_its_headers_for_each_title_since_the_last_run(settings: Settings) -> None:
    sites = _sites()
    read = _read("usajobs", settings, sites, since=STARTED - timedelta(days=2, hours=1))
    asked = [(url, headers) for url, _, headers in sites.asked]
    assert [parse_qs(urlsplit(url).query) for url, _ in asked] == [
        {"Keyword": [title], "DatePosted": ["3"], "ResultsPerPage": ["500"], "Page": ["1"]}
        for title in settings.titles]
    assert all(headers == {"User-Agent": "ada@example.com", "Authorization-Key": "usa-key"} for _, headers in asked)
    # Both titles found every job; each wanted one is kept once, the budget analyst not at all.
    assert _jobs(read) == [
        ("QA Automation Engineer", "Example Agency", "Durham, North Carolina", "on_site", "Raleigh-Durham-Chapel Hill"),
        ("SDET (Software Test Engineer)", "Example Bureau", "Anywhere in the U.S. (remote job)", "remote", None),
        ("SDET", "Example Office", "Raleigh, North Carolina", "on_site", "Raleigh-Durham-Chapel Hill"),
    ]
    assert read.jobs[0][0].link == "https://www.usajobs.gov/job/801"
    # What the job needs is kept with what it does; a job that says neither has its page read.
    assert [description for _, description in read.jobs] == [
        "Test the agency's public systems.\n\n- Write automated tests\n- Review releases\n\n"
        "One year of test automation experience.\n\nUS citizenship.",
        # The documented answer gives the duties as one text, the live one as a list.
        "Build the bureau's test tools.\n\nKeep the test tools running.",
        None,
    ]


def test_usajobs_reads_every_page_of_a_search_and_asks_at_most_60_days_back(settings: Settings) -> None:
    page = json.loads((HERE / "usajobs.json").read_text())
    page["SearchResult"]["SearchResultCountAll"] = 1200
    asked: list[str] = []

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        asked.append(url)
        return Answer(200, json.dumps(page))

    Feeds(get=get).read("usajobs", replace(settings, titles=["SDET"]), STARTED - timedelta(days=90), STARTED)
    assert [parse_qs(urlsplit(url).query)["Page"] for url in asked] == [["1"], ["2"], ["3"]]
    assert {parse_qs(urlsplit(url).query)["DatePosted"][0] for url in asked} == {"60"}


def test_adzuna_searches_each_title_in_each_area_and_once_remote_and_reads_the_pages(settings: Settings) -> None:
    sites = _sites()
    read = _read("adzuna", settings, sites)
    queries = [parse_qs(urlsplit(url).query) for url in sites.urls(ADZUNA)]
    assert [(query["what_phrase"][0], query.get("where", query.get("what_and"))[0]) for query in queries] == [
        (title, where) for title in settings.titles for where in ("Raleigh, NC", "San Francisco, CA", "remote")]
    assert all(query["app_id"] == ["ad-id"] and query["app_key"] == ["ad-key"] and query["max_days_old"] == ["14"]
               for query in queries)
    # The job's link is its id, whatever redirect the answer gave; the nurse is not wanted. The
    # Raleigh search found it, so it keeps that area (Wake County is in no city table, but North
    # Carolina is the area's state); the remote search found it too, so it is remote.
    assert [(job.link, job.title, job.remote, job.area, description) for job, description in read.jobs] == [
        ("https://www.adzuna.com/details/901", "QA Automation Engineer", "remote", "Raleigh-Durham-Chapel Hill", None)]


def test_jooble_posts_its_searches_with_the_key_in_the_address(settings: Settings) -> None:
    sites = _sites()
    read = _read("jooble", settings, sites)
    assert [body for _, body, _ in sites.asked] == [
        body for title in settings.titles for body in (
            {"keywords": title, "location": "Raleigh, NC", "page": "1"},
            {"keywords": title, "location": "San Francisco, CA", "page": "1"},
            {"keywords": f"{title} remote", "location": "USA", "page": "1"})]
    assert {url for url, _, _ in sites.asked} == {JOOBLE}
    # Each search answered the same two jobs, once each, the remote search too, so both count as
    # remote. Durham is in an area; Chicago names another state than the Raleigh search's, so it
    # takes no area from it.
    assert [(job.link, job.title, job.place, job.remote, job.area, description)
            for job, description in read.jobs] == [
        ("https://jooble.org/desc/5551", "Senior QA Automation Engineer", "Durham, NC", "remote",
         "Raleigh-Durham-Chapel Hill", None),
        ("https://jooble.org/desc/5552", "SDET", "Chicago, IL", "remote", None, None),
    ]


def test_a_feed_answer_not_in_its_shape_is_named(settings: Settings) -> None:
    with pytest.raises(Exception, match="the answer is not the feed's shape"):
        _read("remotive", settings, _sites(remotive=Answer(200, json.dumps({"jobs": [{"title": "SDET"}]}))))
    with pytest.raises(Exception, match="the answer is not JSON"):
        _read("remoteok", settings, _sites(remoteok=Answer(200, "<html>")))


def test_a_busy_feed_is_asked_again_after_a_pause(settings: Settings) -> None:
    answers = [Answer(429, ""), Answer(503, ""), _ok("remotive.json")]
    pauses: list[float] = []

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        return answers.pop(0)

    read = Feeds(get=get, sleep=pauses.append).read("remotive", settings, STARTED, STARTED)
    assert len(read.jobs) == 2 and pauses == [5.0, 20.0]


# Which feeds run


def test_a_keyed_feed_without_its_key_and_a_remote_feed_the_settings_exclude_are_skipped(settings: Settings) -> None:
    assert [skipped(name, settings) for name in FEEDS] == [None] * len(FEEDS)
    keyless = replace(settings, keys={"jooble": KEYS["jooble"]}, remote="exclude")
    assert {name: skipped(name, keyless) for name in FEEDS} == {
        "remotive": "it lists only remote jobs and the settings exclude them",
        "remoteok": "it lists only remote jobs and the settings exclude them",
        "arbeitnow": None,
        "hn_hiring": None,
        "usajobs": "no key in data/settings.json (keys.usajobs)",
        "adzuna": "no key in data/settings.json (keys.adzuna)",
        "jooble": None,
    }


def test_with_remote_excluded_or_only_the_place_searches_follow(settings: Settings) -> None:
    sites = _sites()
    _read("jooble", replace(settings, remote="exclude", titles=["SDET"]), sites)
    assert [body for _, body, _ in sites.asked] == [
        {"keywords": "SDET", "location": "Raleigh, NC", "page": "1"},
        {"keywords": "SDET", "location": "San Francisco, CA", "page": "1"}]
    sites = _sites()
    _read("jooble", replace(settings, remote="only", titles=["SDET"]), sites)
    assert [body for _, body, _ in sites.asked] == [{"keywords": "SDET remote", "location": "USA", "page": "1"}]
    # An area the table does not know is asked for by its own name.
    sites = _sites()
    _read("jooble", replace(settings, remote="exclude", titles=["SDET"], areas=["Lisbon"]), sites)
    assert [body for _, body, _ in sites.asked] == [{"keywords": "SDET", "location": "Lisbon", "page": "1"}]


# The feeds inside hunt find


class Skill:
    def __call__(self, command: list[str]) -> None:
        out = Path(json.loads(command[2].split(" ", 1)[1])["out"])
        shutil.copy(FINDER / "found-jobs.json", out)


def _fetch(link: str) -> tuple[str, str]:
    # Jooble's Durham job is the alert's LinkedIn job: its page ends there.
    if link == "https://jooble.org/desc/5551":
        return "https://www.linkedin.com/jobs/view/4012345678", (FINDER / "pages" / "linkedin-easy-apply.html").read_text()
    page = "linkedin-easy-apply.html" if "linkedin.com" in link else "company.html"
    return link, (FINDER / "pages" / page).read_text()


def _find(tmp_path: Path, settings: Settings, clock: MovableClock, sites: Sites, jobs: JobsList | None = None):
    jobs = jobs or JobsList(tmp_path / "jobs.db", clock=clock)
    feeds = Feeds(get=sites, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=Skill(), fetch=_fetch,
                   clock=clock, feeds=feeds)
    return summary, jobs


def test_find_saves_every_feeds_jobs_once_with_a_line_for_each(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    summary, jobs = _find(tmp_path, settings, clock, _sites())
    assert str(summary).splitlines() == [
        "alert: 4 emails, 8 jobs, 4 new",
        "remotive: 2 found, 2 new",
        "remoteok: 1 found, 1 new",
        # Munich is in no area and not remote.
        "arbeitnow: 4 found, 3 new",
        "hn_hiring: 3 found, 3 new",
        "usajobs: 3 found, 3 new",
        "adzuna: 1 found, 1 new",
        # The Durham job is the alert's LinkedIn job, saved once.
        "jooble: 2 found, 1 new",
    ]
    assert summary.failed == []
    linkedin = [job for job in jobs.all() if job.link == "https://www.linkedin.com/jobs/view/4012345678"]
    assert len(linkedin) == 1 and linkedin[0].source == "alert"
    assert not any(job.link.startswith("https://jooble.org/desc/5551") for job in jobs.all())
    remotive = next(job for job in jobs.all() if job.source == "remotive")
    assert (remotive.kind, remotive.description, remotive.page_problem) == (
        "company_site", "Write Playwright tests for our checkout.\n- Python\n- CI", None)
    # A feed that gives only a snippet has its page read.
    adzuna = next(job for job in jobs.all() if job.source == "adzuna")
    assert adzuna.description is not None and adzuna.description != "Automate the tests of our patient portal…"
    # The source is kept, and every new job has its posting.
    assert {job.source for job in jobs.all()} == {"alert", *FEEDS}
    assert all((tmp_path / "jobs" / str(job.id) / "posting.md").exists() for job in jobs.all())
    # Each feed's run is kept: the next one reads from this run's start.
    assert all(jobs.last_run(name) == clock.now for name in FEEDS)


def test_find_again_lists_no_feed_job_twice(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    _, jobs = _find(tmp_path, settings, clock, _sites())
    count = len(jobs.all())
    clock.now += timedelta(days=1)
    summary, _ = _find(tmp_path, settings, clock, _sites(), jobs)
    assert len(jobs.all()) == count
    assert all(line.endswith(" 0 new") for line in str(summary).splitlines())


def test_a_failing_feed_is_named_and_the_others_are_saved(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    summary, jobs = _find(tmp_path, settings, clock, _sites(remoteok=Answer(500, "down"), usajobs=Answer(401, "")))
    assert summary.failed == ["remoteok: HTTP 500", "usajobs: QA Automation Engineer: HTTP 401",
                              "usajobs: SDET: HTTP 401"]
    lines = str(summary).splitlines()
    assert "remotive: 2 found, 2 new" in lines and "usajobs: 0 found, 0 new" in lines
    assert not any(line.startswith("remoteok") for line in lines)
    assert {job.source for job in jobs.all()} == {"alert", *FEEDS} - {"remoteok", "usajobs"}
    # A feed that failed reads from the same time next run; the others from this run.
    assert jobs.last_run("remoteok") is None and jobs.last_run("usajobs") is None
    assert jobs.last_run("remotive") == clock.now
    # The keys never show in what is told.
    assert not any(key in " ".join(summary.failed + lines) for key in ("usa-key", "ad-key", "jo-key"))


def test_a_job_that_cannot_be_saved_is_named_and_the_feeds_other_jobs_and_the_later_feeds_are_saved(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    answer = json.loads((HERE / "remotive.json").read_text())
    answer["jobs"][0]["url"] = "https://["
    summary, jobs = _find(tmp_path, settings, clock, _sites(remotive=Answer(200, json.dumps(answer))))
    assert summary.failed == ["remotive: left out https://[: ValueError: Invalid IPv6 URL"]
    assert [job.title for job in jobs.all() if job.source == "remotive"] == ["SDET & Test Lead"]
    assert "remotive: 2 found, 1 new" in str(summary).splitlines()
    assert {job.source for job in jobs.all()} == {"alert", *FEEDS}
    # The feed reads from the same time next run; the later feeds from this run.
    assert jobs.last_run("remotive") is None and jobs.last_run("jooble") == clock.now


def test_a_key_in_an_error_is_never_told(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    sites = _sites()

    def leaky(url: str, body: object = None, headers: dict[str, str] | None = None) -> Answer:
        if url.startswith(USAJOBS):
            raise ValueError(f"Invalid header value {(headers or {})['Authorization-Key']!r}")
        if url.startswith(ADZUNA):
            raise SearchError(f"could not reach {url}")
        return sites(url, body, headers)

    summary, _ = _find(tmp_path, settings, clock, leaky)  # type: ignore[arg-type]
    told = " ".join(summary.failed)
    assert "usajobs: " in told and "adzuna: " in told
    assert not any(key in told for key in ("usa-key", "ad-id", "ad-key"))


def test_feeds_that_break_are_named_and_the_alerts_still_get_their_postings(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def broken(url: str, body: object = None, headers: object = None) -> Answer:
        raise RuntimeError("a bug in a reader")

    summary, jobs = _find(tmp_path, settings, clock, broken)  # type: ignore[arg-type]
    assert summary.failed == [f"{name}: a bug in a reader" for name in FEEDS]
    assert {job.source for job in jobs.all()} == {"alert"}
    assert all((tmp_path / "jobs" / str(job.id) / "posting.md").exists() for job in jobs.all())

    class Broken(Feeds):
        def run(self, *args: object) -> tuple[list[str], list[str]]:
            raise RuntimeError("the feeds broke")

    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=Skill(), fetch=_fetch,
                   clock=clock, feeds=Broken(get=broken))  # type: ignore[arg-type]
    assert summary.failed == ["feeds: RuntimeError: the feeds broke"]


def test_without_keys_hunt_find_says_each_keyed_feed_is_skipped_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FINDER / "settings.json", data / "settings.json")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "run_skill", Skill())
    monkeypatch.setattr(cli, "fetch_page", _fetch)
    monkeypatch.setattr(cli, "public_feeds", lambda: Feeds(get=_sites(), sleep=lambda seconds: None))
    assert cli.main(["find"]) == 0
    printed = capsys.readouterr()
    assert printed.out.splitlines()[-3:] == [
        "usajobs: skipped, no key in data/settings.json (keys.usajobs)",
        "adzuna: skipped, no key in data/settings.json (keys.adzuna)",
        "jooble: skipped, no key in data/settings.json (keys.jooble)",
    ]
    assert printed.err == ""


def test_the_public_feeds_go_over_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real one, which the tests otherwise replace.
    monkeypatch.undo()
    feeds = cli.public_feeds()
    assert feeds is not None and feeds.get is cli.http_get


def test_http_get_sends_the_headers_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt import search

    sent: list[dict[str, str]] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, size: int) -> bytes:
            return b"{}"

    def answer(request, timeout: float) -> Response:  # type: ignore[no-untyped-def]
        sent.append(dict(request.header_items()))
        return Response()

    monkeypatch.setattr(search, "urlopen", answer)
    search.http_get(USAJOBS, headers={"Authorization-Key": "k", "User-Agent": "ada@example.com"})
    search.http_get(USAJOBS)
    # urllib spells a header's name with only its first letter in capitals.
    assert sent[0]["Authorization-key"] == "k" and sent[0]["User-agent"] == "ada@example.com"
    assert "Authorization-key" not in sent[1] and sent[1]["User-agent"] == search.USER_AGENT


def test_adzuna_reads_every_page_of_a_search(settings: Settings) -> None:
    full = {"count": 120, "results": [
        {"id": str(number), "title": "SDET", "company": {"display_name": "Example Labs"},
         "location": {"display_name": "Raleigh, NC"}} for number in range(50)]}
    asked: list[str] = []

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        asked.append(url)
        return Answer(200, json.dumps(full))

    read = Feeds(get=get).read("adzuna", replace(settings, titles=["SDET"], remote="exclude",
                                                  areas=["Raleigh-Durham-Chapel Hill"]), STARTED, STARTED)
    assert [urlsplit(url).path.rsplit("/", 1)[1] for url in asked] == ["1", "2", "3"]
    assert len(read.jobs) == 50 and read.problems == []


def test_jooble_reads_pages_up_to_its_total(settings: Settings) -> None:
    page = json.loads((HERE / "jooble.json").read_text())
    page["totalCount"] = 5
    bodies: list[object] = []

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        bodies.append(body)
        # Two jobs a page, new ones each page, an empty page after the total.
        number = int(body["page"])  # type: ignore[index]
        for job, at in zip(page["jobs"], (0, 1)):
            job["id"] = number * 10 + at
        return Answer(200, json.dumps(page))

    read = Feeds(get=get).read("jooble", replace(settings, titles=["SDET"], remote="only"), STARTED, STARTED)
    assert [body["page"] for body in bodies] == ["1", "2", "3"]  # type: ignore[index]
    assert len(read.jobs) == 3  # the SDETs of three pages; the senior QA engineers are not wanted


def test_one_failed_search_of_a_feed_keeps_the_others_jobs(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    sites = _sites()

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        if url.startswith(ADZUNA) and "where=San+Francisco" in url:
            return Answer(500, "down")
        return sites(url, body, headers)

    summary, jobs = _find(tmp_path, settings, clock, get)  # type: ignore[arg-type]
    assert summary.failed == ["adzuna: QA Automation Engineer in San Francisco, CA: HTTP 500",
                              "adzuna: SDET in San Francisco, CA: HTTP 500"]
    assert "adzuna: 1 found, 1 new" in str(summary).splitlines()
    assert any(job.source == "adzuna" for job in jobs.all())
    # Its jobs are saved; it reads from the same time next run.
    assert jobs.last_run("adzuna") is None and jobs.last_run("jooble") == clock.now


def test_the_next_run_of_a_feed_reads_from_its_last_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    _, jobs = _find(tmp_path, settings, clock, _sites())
    clock.now += timedelta(hours=5)
    sites = _sites()
    _find(tmp_path, settings, clock, sites, jobs)
    assert {parse_qs(urlsplit(url).query)["max_days_old"][0] for url in sites.urls(ADZUNA)} == {"1"}
    assert {parse_qs(urlsplit(url).query)["DatePosted"][0] for url in sites.urls(USAJOBS)} == {"1"}
    # Arbeitnow's first page holds a job from before the last run.
    assert sites.urls(ARBEITNOW) == [ARBEITNOW]


def test_a_job_two_searches_found_keeps_the_area_one_gave_and_an_unknown_area_its_search(
    settings: Settings
) -> None:
    from job_hunt.feeds import FeedRead, _job

    read = FeedRead()
    read.add((_job("jooble", settings, link="https://jooble.org/desc/1", title="SDET", company="Example Labs",
                   places=["Somewhere"], remote="remote"), None))
    read.add((_job("jooble", settings, link="https://jooble.org/desc/1", title="SDET", company="Example Labs",
                   places=["Somewhere"], remote="on_site", area="San Francisco Bay Area"), None))
    assert [(job.remote, job.area) for job, _ in read.jobs] == [("remote", "San Francisco Bay Area")]
    # An area the table does not know keeps its search's area whatever state the place names.
    lisbon = replace(settings, areas=["Lisbon"])
    job = _job("jooble", lisbon, link="https://jooble.org/desc/2", title="SDET", company="Example Labs",
               places=["Example Street, OR"], remote="on_site", area="Lisbon")
    assert job.area == "Lisbon"


def _pages(make):  # type: ignore[no-untyped-def]
    """A feed answering each request with `make(number)`, counting from 1."""
    asked: list[tuple[str, object]] = []

    def get(url: str, body: object = None, headers: object = None) -> Answer:
        asked.append((url, body))
        answer = make(len(asked))
        return answer if isinstance(answer, Answer) else Answer(200, json.dumps(answer))

    return get, asked


def _adzuna_page(number: int, size: int = 50, count: int = 10_000) -> dict:
    return {"count": count, "results": [
        {"id": f"{number}-{at}", "title": "SDET", "company": {"display_name": "Example Labs"},
         "location": {"display_name": "Raleigh, NC"}} for at in range(size)]}


ONE_SEARCH = {"titles": ["SDET"], "remote": "exclude", "areas": ["Raleigh-Durham-Chapel Hill"]}


def test_a_keyed_search_stops_at_ten_pages(settings: Settings) -> None:
    get, asked = _pages(_adzuna_page)
    read = Feeds(get=get).read("adzuna", replace(settings, **ONE_SEARCH), STARTED, STARTED)
    assert len(asked) == 10 and len(read.jobs) == 500


def test_adzuna_stops_at_a_short_page_whatever_count_it_says(settings: Settings) -> None:
    get, asked = _pages(lambda number: _adzuna_page(number, size=7, count=500))
    Feeds(get=get).read("adzuna", replace(settings, **ONE_SEARCH), STARTED, STARTED)
    assert len(asked) == 1


def test_jooble_stops_at_an_empty_page_whatever_total_it_says(settings: Settings) -> None:
    page = json.loads((HERE / "jooble.json").read_text()) | {"totalCount": 100}
    get, asked = _pages(lambda number: page if number == 1 else {"totalCount": 100, "jobs": []})
    Feeds(get=get).read("jooble", replace(settings, **ONE_SEARCH), STARTED, STARTED)
    assert len(asked) == 2


@pytest.mark.parametrize("name", ["adzuna", "jooble", "usajobs"])
def test_a_later_page_that_fails_keeps_the_pages_read_before_it(settings: Settings, name: str) -> None:
    usajobs = json.loads((HERE / "usajobs.json").read_text())
    usajobs["SearchResult"]["SearchResultCountAll"] = 5000
    jooble = json.loads((HERE / "jooble.json").read_text()) | {"totalCount": 100}
    first = {"adzuna": _adzuna_page(1), "jooble": jooble, "usajobs": usajobs}[name]
    get, asked = _pages(lambda number: first if number == 1 else Answer(500, "down"))
    read = Feeds(get=get).read(name, replace(settings, **ONE_SEARCH), STARTED, STARTED)
    assert len(asked) == 2 and len(read.jobs) >= 1
    assert len(read.problems) == 1 and read.problems[0].endswith("HTTP 500")
