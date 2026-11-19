"""`hunt find`'s own searches: LinkedIn signed out, and the public job boards of every company in
`data/companies.db`.

No network and no model: every answer is a made-up one under `tests/search/`, shaped after the
real ones (checked 2026-11-18), served by `Sites` in place of `http_get`.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from job_hunt import cli
from job_hunt.companies import (
    CRAWL_TRIES, Board, Companies, board_of, crawl_index, wanted_boards,
)
from job_hunt.finder import find
from job_hunt.jobs import FoundJob, JobsList
from job_hunt.search import (
    BUSY_PAUSES, LINKEDIN_OVERLAP, LINKEDIN_PAGES, Answer, Gone, Pace, Search, SearchError, area_of, by_host, linkedin_cards,
    linkedin_search, read_board,
)
from job_hunt.settings import Settings, SettingsError, read_settings
from alert_skill import AlertSkill
from movable_clock import MovableClock

HERE = Path(__file__).parent / "search"
FINDER = Path(__file__).parent / "finder"
LINKEDIN = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def _saved(name: str) -> str:
    return (HERE / name).read_text()


@pytest.fixture
def settings() -> Settings:
    return read_settings(FINDER / "settings.json")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


class Sites:
    """Stands in for the network: an answer for each address, by its start; anything else 404."""

    def __init__(self, answers: dict[str, Answer | list[Answer]] | None = None) -> None:
        self.answers = dict(answers or {})
        self.asked: list[tuple[str, object]] = []
        self.lock = threading.Lock()

    def __call__(self, url: str, body: object = None) -> Answer:
        with self.lock:
            self.asked.append((url, body))
            for start, answer in self.answers.items():
                if url.startswith(start):
                    if isinstance(answer, list):
                        # Answers in turn, the last one again once they run out.
                        return answer.pop(0) if len(answer) > 1 else answer[0]
                    return answer
        return Answer(404, "Not Found")

    def urls(self, start: str = "") -> list[str]:
        return [url for url, _ in self.asked if url.startswith(start)]


def _ok(name: str) -> Answer:
    return Answer(200, _saved(name))


def _no_pause() -> Pace:
    # The searches keep the order they are listed in, so a test can say which is asked first.
    return Pace(sleep=lambda seconds: None, between=lambda: 3.0, order=lambda searches: None)


# LinkedIn


def test_a_linkedin_search_page_gives_each_cards_job() -> None:
    cards = linkedin_cards(_saved("linkedin-full-page.html"))
    assert len(cards) == 10
    first = cards[0]
    assert (first.link, first.title, first.company, first.place) == (
        "https://www.linkedin.com/jobs/view/4100000000", "QA Automation Engineer", "Example Labs", "Durham, NC")
    assert cards[4].place == "Chapel Hill, NC" and cards[1].company == "Globex Example"


def test_a_linkedin_search_page_without_cards_gives_none() -> None:
    assert linkedin_cards("") == [] and linkedin_cards("<html><body>No jobs</body></html>") == []


def _linkedin_sites(pages: list[str]) -> Sites:
    return Sites({LINKEDIN: [Answer(200, _saved(page) if page else "") for page in pages]})


def test_linkedin_searches_each_title_in_each_area_and_once_remote(settings: Settings, clock: MovableClock) -> None:
    sites = _linkedin_sites(["linkedin-short-page.html"])
    since = clock.now - timedelta(hours=9)
    linkedin_search(settings, since, clock.now, sites, _no_pause())
    asked = [parse_qs(urlsplit(url).query) for url in sites.urls(LINKEDIN)]
    searched = [(each["keywords"][0], each.get("location", [None])[0], each.get("f_WT", [None])[0]) for each in asked]
    assert searched == [
        ("QA Automation Engineer", "Raleigh-Durham-Chapel Hill", None),
        ("QA Automation Engineer", "San Francisco Bay Area", None),
        ("SDET", "Raleigh-Durham-Chapel Hill", None),
        ("SDET", "San Francisco Bay Area", None),
        ("QA Automation Engineer", None, "2"),
        ("SDET", None, "2"),
    ]
    # The time window is the time since the last run, in seconds.
    assert {each["f_TPR"][0] for each in asked} == {f"r{9 * 3600}"}
    assert {each["start"][0] for each in asked} == {"0"}


@pytest.mark.parametrize(("remote", "searches"), [("exclude", 4), ("only", 2)])
def test_linkedin_searches_remote_only_when_the_settings_want_it(
    settings: Settings, clock: MovableClock, remote: str, searches: int
) -> None:
    sites = _linkedin_sites([""])
    linkedin_search(replace(settings, remote=remote), clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    asked = [parse_qs(urlsplit(url).query) for url in sites.urls(LINKEDIN)]
    assert len(asked) == searches
    assert all(each["f_WT"] == (["2"] if remote == "only" else ["1,3"]) for each in asked)


def test_linkedin_reads_the_next_page_until_the_limit(settings: Settings, clock: MovableClock) -> None:
    one = replace(settings, titles=["QA Automation Engineer"], areas=["Raleigh-Durham-Chapel Hill"], remote="exclude")
    sites = _linkedin_sites(["linkedin-full-page.html"])
    result = linkedin_search(one, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    starts = [parse_qs(urlsplit(url).query)["start"][0] for url in sites.urls(LINKEDIN)]
    assert starts == ["0", "10", "20", "30", "40"] and LINKEDIN_PAGES == 5
    # The same ten cards on every page are ten jobs.
    assert len(result.jobs) == 10 and result.stopped is None


def test_linkedin_stops_a_search_at_an_empty_or_short_page(settings: Settings, clock: MovableClock) -> None:
    one = replace(settings, titles=["QA Automation Engineer"], areas=["Raleigh-Durham-Chapel Hill"], remote="exclude")
    sites = _linkedin_sites(["linkedin-full-page.html", ""])
    linkedin_search(one, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    assert len(sites.urls(LINKEDIN)) == 2
    sites = _linkedin_sites(["linkedin-full-page.html", "linkedin-short-page.html"])
    result = linkedin_search(one, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    assert len(sites.urls(LINKEDIN)) == 2 and len(result.jobs) == 13


@pytest.mark.parametrize("status", [429, 999])
def test_a_429_or_999_ends_linkedin_for_the_run(settings: Settings, clock: MovableClock, status: int) -> None:
    sites = Sites({LINKEDIN: [Answer(200, _saved("linkedin-full-page.html")), Answer(status, "")]})
    result = linkedin_search(settings, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    assert len(sites.urls(LINKEDIN)) == 2
    assert result.stopped == f"LinkedIn answered {status} after 1 search page, so it is not searched again this run"
    assert len(result.jobs) == 10


def test_a_linkedin_search_page_is_counted_before_its_turn_is_given_up(
    settings: Settings, clock: MovableClock
) -> None:
    # A job page read in another thread may take the turn as soon as it is given up, and its
    # stop names the pages counted by then.
    class Watched(Pace):
        counted: list[int] = []

        @contextmanager
        def turn(self) -> Iterator[None]:
            with super().turn():
                yield
            self.counted.append(self.pages)

    one = replace(settings, titles=["QA Automation Engineer"], areas=["Raleigh-Durham-Chapel Hill"], remote="exclude")
    pace = Watched(sleep=lambda seconds: None, between=lambda: 3.0, order=lambda searches: None)
    linkedin_search(one, clock.now - timedelta(days=1), clock.now, _linkedin_sites(["linkedin-full-page.html", ""]),
                    pace)
    assert pace.counted == [1, 2]


def test_linkedin_pauses_two_to_five_seconds_between_requests(settings: Settings, clock: MovableClock) -> None:
    slept: list[float] = []
    pace = Pace(sleep=slept.append, between=lambda: 4.0)
    linkedin_search(settings, clock.now - timedelta(days=1), clock.now, _linkedin_sites([""]), pace)
    # Six searches of one page each: a pause before each request after the first.
    assert slept == [4.0] * 5
    default = Pace(sleep=lambda seconds: None)
    assert all(2 <= default.between() <= 5 for _ in range(50))


def test_linkedin_jobs_get_the_searched_area_and_remote_ones_are_remote(settings: Settings, clock: MovableClock) -> None:
    sites = Sites({f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": _ok("linkedin-short-page.html"),
                   f"{LINKEDIN}?keywords=QA+Automation+Engineer&f_WT=2": _ok("linkedin-remote-page.html"),
                   LINKEDIN: Answer(200, "")})
    result = linkedin_search(settings, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    by_link = {job.link: job for job in result.jobs}
    near = by_link["https://www.linkedin.com/jobs/view/4100000100"]
    assert (near.area, near.remote, near.board, near.source, near.email_id) == (
        "Raleigh-Durham-Chapel Hill", "on_site", "linkedin", "linkedin_search", None)
    far = by_link["https://www.linkedin.com/jobs/view/4100000200"]
    assert (far.area, far.remote, far.place) == (None, "remote", "United States")


def test_a_linkedin_search_that_fails_is_named_and_the_others_go_on(settings: Settings, clock: MovableClock) -> None:
    sites = Sites({f"{LINKEDIN}?keywords=SDET": Answer(500, "oops"), LINKEDIN: _ok("linkedin-short-page.html")})
    result = linkedin_search(settings, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    assert result.problems == ["LinkedIn search for SDET in Raleigh-Durham-Chapel Hill: HTTP 500",
                               "LinkedIn search for SDET in San Francisco Bay Area: HTTP 500",
                               "LinkedIn search for SDET, remote: HTTP 500"]
    assert len(result.jobs) == 3 and result.stopped is None


# Areas


@pytest.mark.parametrize(
    ("place", "area"),
    [
        ("Durham, NC", "Raleigh-Durham-Chapel Hill"),
        ("Research Triangle Park, North Carolina, United States", "Raleigh-Durham-Chapel Hill"),
        ("US-NC-Morrisville", "Raleigh-Durham-Chapel Hill"),
        ("Raleigh-Durham-Chapel Hill Area", "Raleigh-Durham-Chapel Hill"),
        ("San Francisco, CA", "San Francisco Bay Area"),
        ("Oakland, California", "San Francisco Bay Area"),
        ("san jose", "San Francisco Bay Area"),
        # A city of the same name in another state is not the area.
        ("Durham, NH", None),
        ("Cary, IL", None),
        ("Austin, TX", None),
        ("Remote - US", None),
        ("", None),
    ],
)
def test_a_place_is_matched_to_an_area_by_the_cities_in_it(place: str, area: str | None) -> None:
    assert area_of(place, ["Raleigh-Durham-Chapel Hill", "San Francisco Bay Area"]) == area


def test_an_area_the_table_does_not_know_matches_its_own_name() -> None:
    assert area_of("Boise, ID", ["Boise"]) == "Boise"
    assert area_of("Boise, ID", ["Raleigh-Durham-Chapel Hill"]) is None
    # The settings' spelling of the area is the one given back.
    assert area_of("Brooklyn, NY", ["new york city"]) == "new york city"


# Boards


def _known() -> set[str]:
    return set()


def test_greenhouse_jobs_are_read_with_their_description(settings: Settings) -> None:
    board = Board("greenhouse", "examplelabs")
    sites = Sites({"https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs?content=true": _ok("greenhouse.json")})
    read = read_board(board, settings, sites, _known())
    # The account executive's title is not wanted.
    assert [job.job.title for job in read.jobs] == ["QA Automation Engineer", "SDET, Remote", "QA Automation Engineer"]
    first = read.jobs[0]
    assert (first.job.link, first.job.company, first.job.place, first.job.area, first.job.remote, first.job.board,
            first.job.source) == ("https://boards.greenhouse.io/examplelabs/jobs/7001?gh_src=abc", "Example Labs",
                                  "Durham, NC", "Raleigh-Durham-Chapel Hill", "on_site", "greenhouse", "greenhouse")
    assert first.description == ("Example Labs builds lab software.\nWhat you will do\n- Write end-to-end tests with "
                                 "Playwright.\n- Keep the test suite fast.")
    assert (read.jobs[1].job.remote, read.jobs[1].job.area) == ("remote", None)
    assert read.newest == datetime(2026, 11, 17, 14, 0, tzinfo=UTC)
    assert read.name == "Example Labs"


def test_lever_jobs_are_read_with_their_description(settings: Settings) -> None:
    sites = Sites({"https://api.lever.co/v0/postings/globex?mode=json": _ok("lever.json")})
    read = read_board(Board("lever", "globex"), settings, sites, _known())
    assert len(read.jobs) == 1
    job = read.jobs[0]
    assert (job.job.link, job.job.title, job.job.company, job.job.place, job.job.area, job.job.remote) == (
        "https://jobs.lever.co/globex/aaaa1111-0000-4000-8000-000000000001", "Senior QA Automation Engineer",
        "Globex", "San Francisco, CA", "San Francisco Bay Area", "hybrid")
    assert job.description == ("Globex Example tests rockets.\nWhat you will do\n- Automate the launch checks.\n"
                               "- Review test plans.\nWe hire everywhere in the Bay Area.")
    assert read.newest == datetime.fromtimestamp(1795000000, UTC)


def test_ashby_jobs_are_read_and_a_second_place_in_an_area_counts(settings: Settings) -> None:
    sites = Sites({"https://api.ashbyhq.com/posting-api/job-board/initech": _ok("ashby.json")})
    read = read_board(Board("ashby", "initech"), settings, sites, _known())
    remote, two = read.jobs
    assert (remote.job.title, remote.job.remote, remote.job.area, remote.description) == (
        "SDET", "remote", None, "Initech Example is hiring an SDET.")
    assert (two.job.place, two.job.area, two.job.remote) == ("Chapel Hill, NC", "Raleigh-Durham-Chapel Hill", "on_site")
    assert read.newest == datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def test_workday_is_searched_by_title_and_reads_each_wanted_jobs_details(settings: Settings, clock: MovableClock) -> None:
    api = "https://umbrella.wd5.myworkdayjobs.com/wday/cxs/umbrella/Careers"
    sites = Sites({f"{api}/jobs": _ok("workday-list.json"),
                   f"{api}/job/Durham-NC/QA-Automation-Engineer_R100": _ok("workday-job.json")})
    read = read_board(Board("workday", "umbrella/wd5/Careers"), settings, sites, _known(), now=clock.now)
    bodies = [body for url, body in sites.asked if url == f"{api}/jobs"]
    assert [body["searchText"] for body in bodies] == ["QA Automation Engineer", "SDET"]  # type: ignore[index]
    assert bodies[0] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "QA Automation Engineer"}
    # The payroll job's title is not wanted, so its details are never read; the QA job is found by
    # both searches and read once.
    assert sites.urls(f"{api}/job/") == [f"{api}/job/Durham-NC/QA-Automation-Engineer_R100"]
    job = read.jobs[0]
    assert len(read.jobs) == 1
    assert (job.job.link, job.job.company, job.job.place, job.job.area) == (
        "https://umbrella.wd5.myworkdayjobs.com/Careers/job/Durham-NC/QA-Automation-Engineer_R100",
        "Umbrella Example", "Durham, NC", "Raleigh-Durham-Chapel Hill")
    assert job.description == "Umbrella Example tests its clinic software.\n- Selenium and Python."
    # The payroll job was posted today.
    assert read.newest == clock.now


def test_workday_does_not_read_the_details_of_a_job_the_list_has(settings: Settings, clock: MovableClock) -> None:
    api = "https://umbrella.wd5.myworkdayjobs.com/wday/cxs/umbrella/Careers"
    sites = Sites({f"{api}/jobs": _ok("workday-list.json")})
    known = {"https://umbrella.wd5.myworkdayjobs.com/Careers/job/Durham-NC/QA-Automation-Engineer_R100"}
    assert read_board(Board("workday", "umbrella/wd5/Careers"), settings, sites, known, now=clock.now).jobs == []
    assert sites.urls(f"{api}/job/") == []


def test_smartrecruiters_jobs_are_read_with_their_details(settings: Settings) -> None:
    api = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
    sites = Sites({f"{api}/744000000000001": _ok("smartrecruiters-job.json"), api: _ok("smartrecruiters-list.json")})
    read = read_board(Board("smartrecruiters", "ExampleCorp"), settings, sites, _known())
    job = read.jobs[0]
    assert len(read.jobs) == 1 and sites.urls(f"{api}/744000000000002") == []
    assert (job.job.link, job.job.company, job.job.place, job.job.area, job.job.remote) == (
        "https://jobs.smartrecruiters.com/ExampleCorp/744000000000001-qa-automation-engineer", "Example Corp",
        "Morrisville, NC, United States", "Raleigh-Durham-Chapel Hill", "hybrid")
    assert job.description == "Example Corp makes test rigs.\nAutomate the rig checks.\n- Python"


def test_workable_jobs_are_read_with_their_description(settings: Settings) -> None:
    sites = Sites({"https://apply.workable.com/api/v1/widget/accounts/hooli?details=true": _ok("workable.json")})
    read = read_board(Board("workable", "hooli"), settings, sites, _known())
    oakland, berlin = read.jobs
    assert (oakland.job.link, oakland.job.company, oakland.job.place, oakland.job.area, oakland.job.remote) == (
        "https://apply.workable.com/j/AB12CD34EF", "Hooli Example", "Oakland, California, United States",
        "San Francisco Bay Area", "on_site")
    assert oakland.description == "Hooli Example tests search."
    assert (berlin.job.remote, berlin.job.area) == ("remote", None)


@pytest.mark.parametrize(("status", "error"), [(404, Gone), (500, SearchError)])
def test_a_board_that_does_not_answer_says_so(settings: Settings, status: int, error: type[Exception]) -> None:
    sites = Sites({"https://api.lever.co/": Answer(status, "")})
    with pytest.raises(error, match=f"HTTP {status}"):
        read_board(Board("lever", "gone-example"), settings, sites, _known())


@pytest.mark.parametrize("status", [429, 503])
def test_a_busy_board_is_asked_again_after_a_pause(settings: Settings, status: int) -> None:
    slept: list[float] = []
    sites = Sites({"https://api.lever.co/": [Answer(status, ""), Answer(status, ""), _ok("lever.json")]})
    read = read_board(Board("lever", "globex"), settings, sites, _known(), sleep=slept.append)
    assert len(read.jobs) == 1 and slept == list(BUSY_PAUSES)
    sites = Sites({"https://api.lever.co/": Answer(status, "")})
    with pytest.raises(SearchError, match=f"HTTP {status}"):
        read_board(Board("lever", "globex"), settings, sites, _known(), sleep=lambda seconds: None)
    assert len(sites.asked) == len(BUSY_PAUSES) + 1


def test_a_board_answer_that_is_not_its_shape_is_an_error(settings: Settings) -> None:
    sites = Sites({"https://api.lever.co/": Answer(200, '{"not": "a list"}')})
    with pytest.raises(SearchError, match="lever"):
        read_board(Board("lever", "odd-example"), settings, sites, _known())


# At most four at a time on one host


def test_at_most_four_requests_run_at_once_on_one_host_and_hosts_run_side_by_side() -> None:
    running: dict[str, int] = {}
    most: dict[str, int] = {}
    lock = threading.Lock()
    both = threading.Barrier(2, timeout=5)

    def task(host: str, number: int) -> int:
        with lock:
            running[host] = running.get(host, 0) + 1
            most[host] = max(most.get(host, 0), running[host])
        if number == 0:
            # The first task of each host waits for the other host's: they run at the same time.
            both.wait()
        time.sleep(0.01)
        with lock:
            running[host] -= 1
        return number

    tasks = [(host, lambda host=host, number=number: task(host, number))
             for host in ("a.example.com", "b.example.com") for number in range(12)]
    results = by_host(tasks)
    assert results == list(range(12)) * 2
    assert most == {"a.example.com": 4, "b.example.com": 4}


def test_a_task_that_fails_gives_its_error_and_the_others_their_results() -> None:
    def broken() -> int:
        raise SearchError("HTTP 500")

    results = by_host([("a.example.com", lambda: 1), ("a.example.com", broken), ("b.example.com", lambda: 3)])
    assert results[0] == 1 and isinstance(results[1], SearchError) and results[2] == 3


# The company index


def test_a_board_is_known_from_any_of_its_links() -> None:
    assert board_of("https://boards.greenhouse.io/examplelabs/jobs/7001?gh_src=abc") == Board("greenhouse", "examplelabs")
    assert board_of("https://job-boards.greenhouse.io/examplelabs") == Board("greenhouse", "examplelabs")
    assert board_of("https://boards.greenhouse.io/embed/job_app?for=examplelabs&token=1") == Board("greenhouse", "examplelabs")
    assert board_of("https://jobs.lever.co/globex/aaaa/apply") == Board("lever", "globex")
    assert board_of("https://jobs.ashbyhq.com/initech/bbbb") == Board("ashby", "initech")
    assert board_of("https://umbrella.wd5.myworkdayjobs.com/en-US/Careers/job/Durham-NC/QA_R100") == Board(
        "workday", "umbrella/wd5/Careers")
    assert board_of("https://jobs.smartrecruiters.com/ExampleCorp/744000000000001-qa") == Board(
        "smartrecruiters", "ExampleCorp")
    assert board_of("https://apply.workable.com/hooli/j/AB12CD34EF/") == Board("workable", "hooli")
    for other in ("https://www.linkedin.com/jobs/view/1", "https://jobs.ashbyhq.com/api/non-user-graphql",
                  "https://apply.workable.com/j/AB12CD34EF", "https://jobs.lever.co/", "https://example.com/jobs"):
        assert board_of(other) is None, other


def _companies(tmp_path: Path, clock: MovableClock) -> Companies:
    return Companies(tmp_path / "companies.db", clock=clock)


def _crawl_sites(ashby: Answer | list[Answer] | None = None) -> Sites:
    api = "https://index.commoncrawl.org/CC-MAIN-2026-46-index"
    return Sites({
        "https://index.commoncrawl.org/collinfo.json": _ok("commoncrawl-collinfo.json"),
        f"{api}?url=jobs.ashbyhq.com%2F%2A&output=json&fl=url&showNumPages=true": Answer(200, '{"pages": 1}'),
        f"{api}?url=jobs.ashbyhq.com%2F%2A&output=json&fl=url&page=0": ashby or _ok("commoncrawl-ashby.jsonl"),
        f"{api}?": Answer(200, '{"pages": 0}'),
    })


def test_the_index_is_filled_from_the_latest_common_crawl(tmp_path: Path, clock: MovableClock) -> None:
    sites = _crawl_sites()
    boards, problems = crawl_index(sites, sleep=lambda seconds: None)
    assert (boards, problems) == ([Board("ashby", "initech"), Board("ashby", "Hooli-Example")], [])
    assert all("CC-MAIN-2026-46-index" in url for url in sites.urls("https://index.commoncrawl.org/CC-MAIN"))
    queried = {parse_qs(urlsplit(url).query)["url"][0] for url in sites.urls("https://index.commoncrawl.org/CC-MAIN")}
    assert queried == {"boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
                       "*.myworkdayjobs.com", "jobs.smartrecruiters.com/*", "apply.workable.com/*"}
    companies = _companies(tmp_path, clock)
    assert companies.add(boards) == 2 and companies.add(boards) == 0


def test_a_504_from_common_crawl_is_tried_again(clock: MovableClock) -> None:
    slept: list[float] = []
    sites = _crawl_sites([Answer(504, "Gateway Time-out"), _ok("commoncrawl-ashby.jsonl")])
    boards, problems = crawl_index(sites, sleep=slept.append)
    assert len(boards) == 2 and problems == [] and len(slept) == 1


def test_a_common_crawl_query_that_keeps_failing_is_named_and_the_others_go_on() -> None:
    sites = _crawl_sites(Answer(504, "Gateway Time-out"))
    boards, problems = crawl_index(sites, sleep=lambda seconds: None)
    assert boards == [] and CRAWL_TRIES == 4
    assert problems == ["Common Crawl for jobs.ashbyhq.com/*: HTTP 504"]


def test_a_company_whose_board_answers_404_twice_is_dropped_and_not_added_again(
    tmp_path: Path, clock: MovableClock
) -> None:
    companies = _companies(tmp_path, clock)
    gone, kept = Board("lever", "gone-example"), Board("lever", "globex")
    companies.add([gone, kept])
    assert companies.missed(gone) is False
    # An answer between two misses starts the count again.
    companies.answered(gone, None)
    assert companies.missed(gone) is False
    assert companies.missed(gone) is True
    assert companies.to_sweep(full=True) == [kept]
    assert companies.add([gone]) == 0 and companies.to_sweep(full=True) == [kept]


def test_the_later_runs_sweep_only_the_boards_that_posted_in_the_last_week(tmp_path: Path, clock: MovableClock) -> None:
    companies = _companies(tmp_path, clock)
    fresh, stale, unread = Board("ashby", "fresh"), Board("ashby", "stale"), Board("ashby", "unread")
    companies.add([fresh, stale, unread])
    companies.answered(fresh, clock.now - timedelta(days=6))
    companies.answered(stale, clock.now - timedelta(days=8))
    assert companies.to_sweep(full=True) == [fresh, stale, unread]
    assert companies.to_sweep(full=False) == [fresh]


def test_the_full_sweep_and_the_index_fall_due_by_time(tmp_path: Path, clock: MovableClock) -> None:
    companies = _companies(tmp_path, clock)
    assert companies.full_due() and companies.index_due()
    companies.swept_full()
    companies.indexed()
    clock.advance(hours=5)
    assert not companies.full_due() and not companies.index_due()
    clock.advance(hours=15)
    assert companies.full_due() and not companies.index_due()
    clock.advance(days=7)
    assert companies.index_due()


def test_the_settings_companies_are_board_links_added_by_hand(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    data = json.loads((FINDER / "settings.json").read_text())
    path.write_text(json.dumps({**data, "companies": ["https://jobs.lever.co/globex", "https://jobs.ashbyhq.com/initech"]}))
    assert wanted_boards(read_settings(path)) == [Board("lever", "globex"), Board("ashby", "initech")]
    path.write_text(json.dumps({**data, "companies": ["https://example.com/careers"]}))
    with pytest.raises(SettingsError, match="companies"):
        read_settings(path)


# The whole run


def _board_sites() -> Sites:
    return Sites({
        "https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs?content=true": _ok("greenhouse.json"),
        "https://api.lever.co/v0/postings/globex?mode=json": _ok("lever.json"),
        "https://api.lever.co/v0/postings/gone-example?mode=json": Answer(404, ""),
        "https://api.ashbyhq.com/posting-api/job-board/initech": Answer(500, "Internal Server Error"),
        f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": _ok("linkedin-short-page.html"),
        LINKEDIN: Answer(200, ""),
    })


class Skill(AlertSkill):
    def __init__(self, found: Path, fail: bool = False) -> None:
        # Failing, the search writes nothing.
        super().__init__(FINDER / "found-jobs.json", {1} if fail else None)


def _fetch(link: str) -> tuple[str, str]:
    page = "linkedin-offsite.html" if "linkedin.com" in link else "company.html"
    return link, (FINDER / "pages" / page).read_text()


def _run(tmp_path: Path, settings: Settings, clock: MovableClock, sites: Sites, *, skill_fails: bool = False,
         jobs: JobsList | None = None, companies: Companies | None = None):
    jobs = jobs or JobsList(tmp_path / "jobs.db", clock=clock)
    companies = companies or _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("greenhouse", "examplelabs"), Board("lever", "globex"), Board("lever", "gone-example"),
                   Board("ashby", "initech")])
    found = tmp_path / "found-jobs.json"
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found, skill_fails), fetch=_fetch, clock=clock,
                   search=search)
    return summary, jobs, companies


def test_find_saves_the_jobs_of_every_source_and_says_how_many_each_found(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    summary, jobs, companies = _run(tmp_path, settings, clock, _board_sites())
    assert str(summary).splitlines() == [
        "alert: 4 emails, 8 jobs, 4 new",
        "linkedin_search: 3 found, 3 new",
        "greenhouse: 3 found, 2 new (1 board)",
        "lever: 1 found, 1 new (2 boards, 1 answered 404)",
        "ashby: 0 found, 0 new (1 board, 1 did not answer)",
        "workday: 0 found, 0 new (0 boards)",
        "smartrecruiters: 0 found, 0 new (0 boards)",
        "workable: 0 found, 0 new (0 boards)",
        "same job from two sources: 0",
        "took: alert 0s, linkedin_search 0s, boards 0s",
    ]
    # One board that did not answer is counted; a source none of whose boards answered fails.
    assert summary.failed == ["ashby: no board answered (initech: HTTP 500)"]
    by_source: dict[str, list[str]] = {}
    for job in jobs.all():
        by_source.setdefault(job.source, []).append(job.title)
    assert sorted(by_source) == ["alert", "greenhouse", "lever", "linkedin_search"]
    # The Austin job is in no area and not remote; the Durham one and the remote SDET are kept.
    greenhouse = next(job for job in jobs.all() if job.source == "greenhouse")
    assert (greenhouse.link, greenhouse.kind, greenhouse.page_problem, greenhouse.email_id) == (
        "https://boards.greenhouse.io/examplelabs/jobs/7001", "company_site", None, None)
    assert greenhouse.description.startswith("Example Labs builds lab software.")
    posting = (tmp_path / "jobs" / str(greenhouse.id) / "posting.md").read_text()
    assert posting.startswith("# QA Automation Engineer\n\nExample Labs · Durham, NC · on site\n")
    # A LinkedIn search job has its page read, as an alert job does.
    searched = next(job for job in jobs.all() if job.source == "linkedin_search")
    assert (searched.kind, searched.board) == ("company_site", "linkedin")


def test_the_lever_board_that_answered_404_is_counted_and_dropped_on_the_second_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    _, jobs, companies = _run(tmp_path, settings, clock, _board_sites())
    assert Board("lever", "gone-example") in companies.to_sweep(full=True)
    clock.advance(days=1)
    _run(tmp_path, settings, clock, _board_sites(), jobs=jobs, companies=companies)
    assert Board("lever", "gone-example") not in companies.to_sweep(full=True)


def test_a_job_from_an_alert_and_from_a_search_is_saved_once(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    # The alert's Example Labs job is 4012345678; LinkedIn's search finds it too.
    card = _saved("linkedin-short-page.html").replace("4100000100", "4012345678")
    sites = _board_sites()
    sites.answers = {**sites.answers, f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": Answer(200, card)}
    summary, jobs, _ = _run(tmp_path, settings, clock, sites)
    # The sources run at once: whichever saved it first has it.
    assert len([job for job in jobs.all() if job.link.endswith("4012345678")]) == 1
    new = {line.split(":")[0]: int(line.split(", ")[-1].split()[0]) for line in str(summary).splitlines()[:2]}
    assert new["alert"] + new["linkedin_search"] == 4 + 2


def test_a_failing_source_is_named_and_the_others_are_saved(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    summary, jobs, _ = _run(tmp_path, settings, clock, _board_sites(), skill_fails=True)
    assert summary.failed[0].startswith("alert: the read-job-alerts search wrote no file")
    assert {job.source for job in jobs.all()} == {"greenhouse", "lever", "linkedin_search"}
    # The alerts are read from the same time again; the searches from this run.
    assert jobs.last_run("alert") is None and jobs.last_run("linkedin_search") == clock.now


def test_linkedins_time_window_comes_from_its_own_last_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    sites = _board_sites()
    _, jobs, companies = _run(tmp_path, settings, clock, sites)
    first = clock.now
    clock.advance(hours=5)
    sites = _board_sites()
    _run(tmp_path, settings, clock, sites, jobs=jobs, companies=companies)
    windows = {parse_qs(urlsplit(url).query)["f_TPR"][0] for url in sites.urls(LINKEDIN)}
    assert windows == {f"r{int((clock.now - first + LINKEDIN_OVERLAP).total_seconds())}"}


def test_linkedins_window_is_counted_back_from_when_its_search_starts(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    # LinkedIn counts f_TPR back from the request; the alerts may have taken an hour before it.
    _, jobs, companies = _run(tmp_path, settings, clock, _board_sites())
    first = clock.now
    clock.advance(hours=5)
    sites = _board_sites()
    later = clock.now + timedelta(hours=1)
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None, clock=lambda: later)
    found = tmp_path / "found-jobs.json"
    find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=_fetch, clock=clock, search=search)
    windows = {parse_qs(urlsplit(url).query)["f_TPR"][0] for url in sites.urls(LINKEDIN)}
    assert windows == {f"r{int((later - first + LINKEDIN_OVERLAP).total_seconds())}"}
    # The next run reads from this run's start, so nothing between the two is left out.
    assert jobs.last_run("linkedin_search") == clock.now


def test_an_area_the_table_does_not_know_is_matched_by_name_abroad_too() -> None:
    assert area_of("London, United Kingdom", ["London"]) == "London"
    assert area_of("Durham, England", ["Raleigh-Durham-Chapel Hill"]) is None
    assert area_of("London, United Kingdom", ["Raleigh-Durham-Chapel Hill", "London"]) == "London"


def test_a_linkedin_stop_is_named_and_linkedin_is_read_from_the_same_time_next(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    sites = _board_sites()
    sites.answers = {start: answer for start, answer in sites.answers.items() if "linkedin" not in start}
    sites.answers[LINKEDIN] = Answer(429, "")
    summary, jobs, _ = _run(tmp_path, settings, clock, sites)
    assert "linkedin_search: LinkedIn answered 429 after 0 search pages, so it is not searched again this run" in (
        summary.failed)
    assert len(sites.urls(LINKEDIN)) == 1
    assert jobs.last_run("linkedin_search") is None
    assert any(job.source == "greenhouse" for job in jobs.all())


def test_a_later_run_of_the_day_sweeps_only_the_boards_that_posted_this_week(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    _, jobs, companies = _run(tmp_path, settings, clock, _board_sites())
    clock.advance(hours=5)
    sites = _board_sites()
    _run(tmp_path, settings, clock, sites, jobs=jobs, companies=companies)
    # Greenhouse posted on the 17th; Lever's newest is from 2026-11-17 too; Ashby never answered.
    asked = {urlsplit(url).netloc for url in sites.urls() if "linkedin" not in url}
    assert asked == {"boards-api.greenhouse.io", "api.lever.co"}
    assert sites.urls("https://api.lever.co/v0/postings/gone-example") == []
    clock.advance(hours=20)
    sites = _board_sites()
    _run(tmp_path, settings, clock, sites, jobs=jobs, companies=companies)
    assert sites.urls("https://api.ashbyhq.com/")


def test_an_apply_link_on_a_board_adds_its_company(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    def to_board(link: str) -> tuple[str, str]:
        ended = "https://jobs.lever.co/hooli-example/1111" if "1234" in link else link
        return ended, (FINDER / "pages" / "company.html").read_text()

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    search = Search(get=Sites({LINKEDIN: Answer(200, "")}), pace=_no_pause(), companies=companies,
                    sleep=lambda seconds: None)
    find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=to_board, clock=clock, search=search)
    assert Board("lever", "hooli-example") in companies.to_sweep(full=True)


def test_the_index_is_filled_inside_find_once_a_week(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    found = tmp_path / "found-jobs.json"
    sites = _crawl_sites()
    sites.answers[LINKEDIN] = Answer(200, "")
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=_fetch, clock=clock, search=search)
    assert "companies: 2 added from Common Crawl, 2 in the index" in str(summary).splitlines()
    assert sorted(sites.urls("https://api.ashbyhq.com/posting-api/job-board/")) == [
        "https://api.ashbyhq.com/posting-api/job-board/Hooli-Example",
        "https://api.ashbyhq.com/posting-api/job-board/initech"]
    clock.advance(days=1)
    sites = _crawl_sites()
    sites.answers[LINKEDIN] = Answer(200, "")
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=_fetch, clock=clock, search=search)
    assert sites.urls("https://index.commoncrawl.org/") == []


def test_hunt_companies_fills_the_index_and_says_how_many(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "DATA", tmp_path)
    monkeypatch.setattr(cli, "http_get", _crawl_sites())
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: None)
    assert cli.main(["companies"]) == 0
    assert capsys.readouterr().out == "companies: 2 added from Common Crawl, 2 in the index\n"
    assert not Companies(tmp_path / "companies.db").index_due()


def test_hunt_help_describes_the_new_sources(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert "companies" in help_text and "Common Crawl" in help_text
    with pytest.raises(SystemExit):
        cli.main(["find", "--help"])
    find_help = capsys.readouterr().out
    assert "LinkedIn" in find_help and "Greenhouse" in find_help and "Workable" in find_help


# What the first review found


def test_a_job_that_closed_before_its_details_were_read_is_skipped_not_the_board(
    settings: Settings, clock: MovableClock
) -> None:
    api = "https://umbrella.wd5.myworkdayjobs.com/wday/cxs/umbrella/Careers"
    sites = Sites({f"{api}/jobs": _ok("workday-list.json"), f"{api}/job/": Answer(404, "")})
    read = read_board(Board("workday", "umbrella/wd5/Careers"), settings, sites, _known(), now=clock.now)
    assert read.jobs == []
    api = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
    sites = Sites({f"{api}/744000000000001": Answer(404, ""), api: _ok("smartrecruiters-list.json")})
    assert read_board(Board("smartrecruiters", "ExampleCorp"), settings, sites, _known()).jobs == []


def test_workday_pages_on_with_the_total_its_first_page_gave(settings: Settings, clock: MovableClock) -> None:
    # Workday says the total on the first page only, and 0 on the later ones.
    page = json.loads(_saved("workday-list.json"))
    first = {**page, "total": 85, "jobPostings": [page["jobPostings"][1]] * 20}
    later = {**page, "total": 0, "jobPostings": [page["jobPostings"][1]] * 20}
    api = "https://umbrella.wd5.myworkdayjobs.com/wday/cxs/umbrella/Careers"
    sites = Sites({f"{api}/jobs": [Answer(200, json.dumps(first))] + [Answer(200, json.dumps(later))] * 4})
    one = replace(settings, titles=["QA Automation Engineer"])
    read_board(Board("workday", "umbrella/wd5/Careers"), one, sites, _known(), now=clock.now)
    assert [body["offset"] for _, body in sites.asked] == [0, 20, 40, 60, 80]  # type: ignore[index]


def test_an_http_error_of_the_standard_library_is_a_search_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    from job_hunt import search

    def cut_short(request: object, timeout: float) -> object:
        raise http.client.IncompleteRead(b"half")

    monkeypatch.setattr(search, "urlopen", cut_short)
    with pytest.raises(SearchError, match="IncompleteRead"):
        search.http_get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search")


def test_a_search_that_breaks_is_named_and_the_alerts_still_get_their_postings(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def broken(url: str, body: object = None) -> Answer:
        raise RuntimeError("a bug in a reader")

    summary, jobs, _ = _run(tmp_path, settings, clock, broken)  # type: ignore[arg-type]
    # Each source broke on its own; the others went on.
    assert summary.failed == [
        "linkedin_search: RuntimeError: a bug in a reader",
        "greenhouse: no board answered (examplelabs: a bug in a reader)",
        "lever: no board answered (globex: a bug in a reader)",
        "ashby: no board answered (initech: a bug in a reader)"]
    alerts = [job for job in jobs.all() if job.source == "alert"]
    assert len(alerts) == 4 and all((tmp_path / "jobs" / str(job.id) / "posting.md").exists() for job in alerts)


def test_hunt_find_with_a_failing_source_says_it_and_exits_1_and_the_morning_logs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(FINDER / "settings.json", data / "settings.json")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "run_skill", Skill(data / "found-jobs.json"))
    monkeypatch.setattr(cli, "fetch_page", _fetch)

    runs: list[int] = []

    def search() -> Search:
        # A fresh index each run, so the morning's run is the day's first sweep too.
        runs.append(len(runs))
        companies = Companies(data / f"companies-{len(runs)}.db")
        companies.indexed()
        companies.add([Board("ashby", "initech")])
        return Search(get=_board_sites(), pace=_no_pause(), companies=companies, sleep=lambda seconds: None)

    monkeypatch.setattr(cli, "site_search", search)
    assert cli.main(["find"]) == 1
    printed = capsys.readouterr()
    assert printed.out.splitlines()[0] == "alert: 4 emails, 8 jobs, 4 new" and len(printed.out.splitlines()) == 10
    assert printed.err == "hunt find: ashby: no board answered (initech: HTTP 500)\n"

    monkeypatch.setattr(cli, "score_step", lambda: None)
    monkeypatch.setattr(cli, "start_session", lambda root: None)
    assert cli.main(["morning"]) == 0
    assert "hunt find: ashby: no board answered (initech: HTTP 500)" in (data / "morning.log").read_text()


def test_the_site_search_keeps_its_index_in_the_data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real one, which the tests otherwise replace.
    monkeypatch.undo()
    monkeypatch.setattr(cli, "DATA", tmp_path)
    search = cli.site_search()
    assert search is not None and search.get is cli.http_get and (tmp_path / "companies.db").exists()


def test_after_linkedin_said_stop_no_linkedin_page_is_read_and_its_jobs_are_still_saved(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    read: list[str] = []

    def fetch(link: str) -> tuple[str, str]:
        read.append(link)
        return _fetch(link)

    sites = _board_sites()
    sites.answers = {start: answer for start, answer in sites.answers.items() if "linkedin" not in start}
    sites.answers[LINKEDIN] = [Answer(200, _saved("linkedin-short-page.html")), Answer(429, "")]
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=fetch, clock=clock, search=search)
    searched = [job for job in jobs.all() if job.source == "linkedin_search"]
    assert len(searched) == 3 and not any("41000001" in link for link in read)
    assert {(job.kind, job.page_problem) for job in searched} == {
        (None, "not read: LinkedIn answered 429 after 1 search page, so it is not searched again this run")}


@pytest.mark.parametrize("status", [429, 999])
def test_a_linkedin_page_that_answers_429_ends_the_reading_of_linkedin_pages(
    tmp_path: Path, settings: Settings, clock: MovableClock, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    from urllib.error import HTTPError

    from job_hunt import finder

    read: list[str] = []

    def answer(request: object, timeout: float) -> object:
        raise HTTPError(read[-1], status, "Too Many", {}, None)  # type: ignore[arg-type]

    # The page is fetched by the real fetch_page, so its own words for the status are the ones read.
    monkeypatch.setattr(finder, "urlopen", answer)

    # Every LinkedIn request in the order made, the searches' and the pages' alike.
    linkedin: list[str] = []

    def fetch(link: str) -> tuple[str, str]:
        read.append(link)
        if "linkedin.com" in link:
            linkedin.append(link)
        return finder.fetch_page(link) if "linkedin.com" in link else _fetch(link)

    sites = Sites({f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": _ok("linkedin-short-page.html"),
                   LINKEDIN: Answer(200, "")})

    def get(url: str, body: object = None) -> Answer:
        if "linkedin.com" in url:
            linkedin.append(url)
        return sites(url, body)
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    search = Search(get=get, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=fetch, clock=clock, search=search)
    # How many search pages answered first depends on when the page was read.
    stops = [line for line in summary.failed if re.fullmatch(
        f"linkedin_search: LinkedIn answered {status} after \\d+ search pages?, so it is not searched again this run", line)]
    assert len(stops) == 1
    # LinkedIn is searched from the same time next run.
    assert jobs.last_run("linkedin_search") is None
    # The first LinkedIn page answered 429: no other LinkedIn page or search is asked for after it.
    pages = [link for link in read if "linkedin.com" in link]
    assert len(pages) == 1 and linkedin[-1] == pages[0]
    # The alerts' two LinkedIn jobs are saved all the same.
    assert {"https://www.linkedin.com/jobs/view/4012345678", "https://www.linkedin.com/jobs/view/4012345690"} <= {
        job.link for job in jobs.all()}


def test_with_remote_excluded_the_area_searches_ask_for_on_site_and_hybrid_jobs(
    settings: Settings, clock: MovableClock
) -> None:
    sites = _linkedin_sites([""])
    linkedin_search(replace(settings, remote="exclude"), clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    assert {parse_qs(urlsplit(url).query)["f_WT"][0] for url in sites.urls(LINKEDIN)} == {"1,3"}


# What the second review found


@pytest.mark.parametrize("place", ["San Francisco; New York", "London, UK; San Francisco, CA", "New York, NY | Oakland, CA",
                                   "Austin, TX or San Jose, CA"])
def test_each_place_of_a_combined_location_is_matched_on_its_own(place: str) -> None:
    assert area_of(place, ["San Francisco Bay Area"]) == "San Francisco Bay Area"


def test_a_job_the_remote_search_finds_again_is_remote(settings: Settings, clock: MovableClock) -> None:
    sites = Sites({f"{LINKEDIN}?keywords=QA+Automation+Engineer": _ok("linkedin-short-page.html"), LINKEDIN: Answer(200, "")})
    result = linkedin_search(settings, clock.now - timedelta(days=1), clock.now, sites, _no_pause())
    # Found first by the area's search, then by the remote one: remote, and still in the area.
    assert {(job.remote, job.area) for job in result.jobs} == {("remote", "Raleigh-Durham-Chapel Hill")}


def test_a_linkedin_page_stop_after_the_searches_is_named_and_keeps_linkedins_window(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    from job_hunt.finder import PageError

    def fetch(link: str) -> tuple[str, str]:
        if "41000001" in link:
            raise PageError("HTTP 429")
        return _fetch(link)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    sites = Sites({f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": _ok("linkedin-short-page.html"),
                   LINKEDIN: Answer(200, "")})
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=fetch, clock=clock, search=search)
    assert any(re.fullmatch(
        r"linkedin_search: LinkedIn answered 429 after \d+ search pages?, so it is not searched again this run", line)
        for line in summary.failed)
    assert jobs.last_run("linkedin_search") is None


def test_linkedins_searches_run_in_a_new_order_each_run(settings: Settings, clock: MovableClock) -> None:
    # LinkedIn answers 429 after a few pages; in one fixed order the last searches would never run.
    orders: list[list[tuple[str, str | None]]] = []
    pace = Pace(sleep=lambda seconds: None, between=lambda: 3.0, order=lambda searches: (searches.reverse(),
                                                                                        orders.append(list(searches))))
    sites = _linkedin_sites([""])
    linkedin_search(settings, clock.now - timedelta(days=1), clock.now, sites, pace)
    asked = [parse_qs(urlsplit(url).query) for url in sites.urls(LINKEDIN)]
    assert [(each["keywords"][0], each.get("location", [None])[0]) for each in asked] == orders[0]
    assert orders[0][0] == ("SDET", None) and len(orders[0]) == 6
    from job_hunt.search import random as search_random
    assert Pace().order is search_random.shuffle


def test_a_linkedin_card_whose_link_is_not_one_is_skipped() -> None:
    page = _saved("linkedin-short-page.html")
    first = re.search(r'href="(https://[^"]+)"', page)
    assert first is not None
    cards = linkedin_cards(page.replace(first.group(1), "https://[bad", 1))
    assert len(cards) == len(linkedin_cards(page)) - 1 and all(card.link.startswith("https://") for card in cards)


def test_workday_pages_past_a_hundred_jobs_to_its_total(settings: Settings, clock: MovableClock) -> None:
    page = json.loads(_saved("workday-list.json"))
    full = {**page, "total": 125, "jobPostings": [page["jobPostings"][1]] * 20}
    api = "https://umbrella.wd5.myworkdayjobs.com/wday/cxs/umbrella/Careers"
    sites = Sites({f"{api}/jobs": Answer(200, json.dumps(full))})
    one = replace(settings, titles=["QA Automation Engineer"])
    read_board(Board("workday", "umbrella/wd5/Careers"), one, sites, _known(), now=clock.now)
    assert [body["offset"] for url, body in sites.asked if url == f"{api}/jobs"] == [  # type: ignore[index]
        0, 20, 40, 60, 80, 100, 120]


def test_smartrecruiters_pages_past_a_thousand_jobs_to_its_total(settings: Settings) -> None:
    api = "https://api.smartrecruiters.com/v1/companies/ExampleCorp/postings"
    entries = [{"id": str(each), "name": "Pastry Chef", "company": {"name": "Example Corp"}} for each in range(100)]
    sites = Sites({api: Answer(200, json.dumps({"totalFound": 1050, "content": entries}))})
    read_board(Board("smartrecruiters", "ExampleCorp"), settings, sites, _known())
    offsets = [int(parse_qs(urlsplit(url).query)["offset"][0]) for url in sites.urls(api)]
    assert offsets == list(range(0, 1100, 100))


def test_the_apply_link_on_a_linkedin_page_names_the_companys_own_address() -> None:
    from job_hunt.finder import offsite_link

    plain = '<code id="applyUrl" style="display: none"><!--"https://jobs.lever.co/hooli-example/1111"--></code>'
    wrapped = ('<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/externalApply/41?'
               'url=https%3A%2F%2Fjobs%2Eashbyhq%2Ecom%2Finitech%2F22&amp;urlHash=x"--></code>')
    assert offsite_link(plain) == "https://jobs.lever.co/hooli-example/1111"
    assert offsite_link(wrapped) == "https://jobs.ashbyhq.com/initech/22"
    assert offsite_link("<html>Easy Apply</html>") is None


def test_a_linkedin_searched_jobs_apply_link_adds_its_board_that_same_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    page = (FINDER / "pages" / "linkedin-offsite.html").read_text()
    page = re.sub(r'(id="applyUrl"[^>]*>\s*<!--\s*")[^"]+', r"\1https://jobs.lever.co/hooli-example/1111", page)

    def fetch(link: str) -> tuple[str, str]:
        return link, page

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    sites = Sites({f"{LINKEDIN}?keywords=QA+Automation+Engineer&location=Raleigh": _ok("linkedin-short-page.html"),
                   LINKEDIN: Answer(200, "")})
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    find(settings, jobs, tmp_path / "jobs", found, run=Skill(found, fail=True), fetch=fetch, clock=clock, search=search)
    assert any(job.source == "linkedin_search" for job in jobs.all())
    assert Board("lever", "hooli-example") in companies.to_sweep(full=True)
    # Added before the sweep, so this run's sweep already read it.
    assert sites.urls("https://api.lever.co/v0/postings/hooli-example")


# The sources at once


def _wait_for(seen: Callable[[], bool]) -> bool:
    give_up = time.monotonic() + 5
    while time.monotonic() < give_up:
        if seen():
            return True
        time.sleep(0.01)
    return False


def _in_list(db: Path, link: str) -> Callable[[], bool]:
    def seen() -> bool:
        with closing(sqlite3.connect(db)) as reading:
            return reading.execute("SELECT 1 FROM jobs WHERE link = ?", (link,)).fetchone() is not None
    return seen


def test_the_sources_run_at_once(tmp_path: Path, settings: Settings, clock: MovableClock) -> None:
    # The alerts' run ends only once a board's job is saved: one after the other, it never would be.
    found = tmp_path / "found-jobs.json"
    greenhouse = _in_list(tmp_path / "jobs.db", "https://boards.greenhouse.io/examplelabs/jobs/7001")

    def alerts(command: list[str]) -> None:
        if _wait_for(greenhouse):
            AlertSkill(FINDER / "found-jobs.json")(command)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("greenhouse", "examplelabs")])
    search = Search(get=_board_sites(), pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=alerts, fetch=_fetch, clock=clock, search=search)
    assert summary.failed == [] and "alert: 4 emails, 8 jobs, 4 new" in str(summary).splitlines()


def test_a_source_that_breaks_or_is_told_to_stop_leaves_the_others_to_finish(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    class Broken:
        def run(self, *args: object) -> tuple[list[str], list[str]]:
            raise RuntimeError("the feeds broke")

    sites = _board_sites()
    sites.answers = {start: answer for start, answer in sites.answers.items() if "linkedin" not in start}
    sites.answers[LINKEDIN] = Answer(429, "")
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("greenhouse", "examplelabs")])
    found = tmp_path / "found-jobs.json"
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=_fetch, clock=clock,
                   search=search, feeds=Broken())  # type: ignore[arg-type]
    assert summary.failed == ["linkedin_search: LinkedIn answered 429 after 0 search pages, so it is not searched again this run",
                              "feeds: RuntimeError: the feeds broke"]
    lines = str(summary).splitlines()
    assert "alert: 4 emails, 8 jobs, 4 new" in lines and "greenhouse: 3 found, 2 new (1 board)" in lines
    assert {job.source for job in jobs.all()} == {"alert", "greenhouse"}


def test_the_same_job_saved_by_two_sources_at_the_same_moment_is_listed_once(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    # The alert's Morrisville job, and a feed's under another link, reach the list at one moment.
    together = threading.Barrier(2, timeout=5)
    indeed = "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b"
    feed_job = FoundJob(link="https://remotive.com/remote-jobs/qa/umbrella-1", title="QA Automation Engineer",
                        company="Umbrella Example, Inc.", place="Morrisville, NC", remote="on_site", board="remotive",
                        email_id=None, email_date=None, area="Raleigh-Durham-Chapel Hill", source="remotive")

    class Slow(JobsList):
        def same_job(self, found: FoundJob) -> int | None:
            # Long enough between the check and the insert for the other source to check too.
            first = super().same_job(found)
            time.sleep(0.05)
            return first

    class Feed:
        def run(self, settings: Settings, jobs: JobsList, started: datetime, save: Callable[..., bool]
                ) -> tuple[list[str], list[str]]:
            together.wait()
            return [f"remotive: 1 found, {int(save(feed_job, 'Test the clinic software.'))} new"], []

    def fetch(link: str) -> tuple[str, str]:
        if link == indeed:
            together.wait()
        return _fetch(link)

    jobs = Slow(tmp_path / "jobs.db", clock=clock)
    found = tmp_path / "found-jobs.json"
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=fetch, clock=clock,
                   feeds=Feed())  # type: ignore[arg-type]
    assert len([job for job in jobs.all() if job.company.startswith("Umbrella Example")]) == 1
    assert "same job from two sources: 1" in str(summary).splitlines()
    assert jobs.has(indeed) and jobs.has(feed_job.link)


def test_linkedins_pause_is_kept_between_the_requests_of_two_threads() -> None:
    # A made-up clock: a pause moves it on, and so does each request, which takes a second.
    now = [0.0]
    requests: list[tuple[float, float]] = []
    pace = Pace(sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), between=lambda: 3.0)

    def search() -> None:
        for _ in range(3):
            with pace.turn():
                began = now[0]
                # Really waits, so the other thread asks for its turn meanwhile.
                time.sleep(0.01)
                now[0] += 1.0
                requests.append((began, now[0]))

    threads = [threading.Thread(target=search) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    requests.sort()
    assert len(requests) == 6
    # One request at a time, and a whole pause after each one ended before the next began.
    assert [later[0] - earlier[1] for earlier, later in zip(requests, requests[1:])] == [3.0] * 5


def test_a_board_an_alerts_link_leads_to_is_swept_in_the_same_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def to_board(link: str) -> tuple[str, str]:
        ended = "https://jobs.lever.co/hooli-example/1111" if "1234" in link else link
        return ended, (FINDER / "pages" / "company.html").read_text()

    sites = _board_sites()
    sites.answers["https://api.lever.co/v0/postings/hooli-example?mode=json"] = _ok("lever.json")
    found = tmp_path / "found-jobs.json"

    def alerts(command: list[str]) -> None:
        # The sweep has its boards by the time the alerts' links are read, as on a slow morning.
        if _wait_for(lambda: bool(sites.urls("https://boards-api.greenhouse.io/"))):
            AlertSkill(FINDER / "found-jobs.json")(command)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("greenhouse", "examplelabs")])
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=alerts, fetch=to_board, clock=clock, search=search)
    assert sites.urls("https://api.lever.co/v0/postings/hooli-example")
    assert len(sites.urls("https://boards-api.greenhouse.io/")) == 1
    assert "lever: 1 found, 1 new (1 board)" in str(summary).splitlines()
    assert any(job.source == "lever" and job.company == "Hooli Example" for job in jobs.all())


def test_an_alerts_run_that_breaks_unexpectedly_is_named_and_the_others_finish(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    def alerts(command: list[str]) -> None:
        raise PermissionError("claude: Permission denied")

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("greenhouse", "examplelabs")])
    search = Search(get=_board_sites(), pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json", run=alerts, fetch=_fetch,
                   clock=clock, search=search)
    assert summary.failed == ["alert: PermissionError: claude: Permission denied"]
    assert "greenhouse: 3 found, 2 new (1 board)" in str(summary).splitlines()
    new = [job for job in jobs.all() if job.state == "new"]
    assert new and all((tmp_path / "jobs" / str(job.id) / "posting.md").exists() for job in new)


def test_a_board_met_through_the_same_job_under_another_link_is_swept_in_the_same_run(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    # LinkedIn listed the job yesterday; today an alert brings it again, on the company's Lever board.
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    jobs.add(FoundJob(link="https://www.linkedin.com/jobs/view/4199999999", title="QA Automation Engineer",
                      company="Hooli Example", place="Durham, NC", remote="on_site", board="linkedin", email_id=None,
                      email_date=None, area="Raleigh-Durham-Chapel Hill", source="linkedin_search"),
             description="Test the clinic software.", kind="company_site")
    found = tmp_path / "found-jobs.json"

    def alerts(command: list[str]) -> None:
        AlertSkill({"emails": 1, "jobs": [{
            "link": "https://jobs.lever.co/hooli-example/2222", "title": "QA Automation Engineer",
            "company": "Hooli Example, Inc.", "place": "Durham, NC", "remote": "on_site", "board": "lever",
            "email_id": "18c0ffee00000009", "email_date": "2026-11-18T06:00:00+00:00",
            "area": "Raleigh-Durham-Chapel Hill"}]})(command)

    sites = _board_sites()
    sites.answers["https://api.lever.co/v0/postings/hooli-example?mode=json"] = _ok("lever.json")
    companies = _companies(tmp_path, clock)
    companies.indexed()
    search = Search(get=sites, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=alerts, fetch=_fetch, clock=clock, search=search)
    assert "same job from two sources: 1" in str(summary).splitlines()
    assert sites.urls("https://api.lever.co/v0/postings/hooli-example")


def test_a_page_an_alert_leads_to_shares_its_hosts_limit_with_the_boards(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    lock = threading.Lock()
    running = [0]
    most = [0]

    def on_workable(ask: Callable[[], Any]) -> Any:
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        try:
            time.sleep(0.2)
            return ask()
        finally:
            with lock:
                running[0] -= 1

    sites = _board_sites()

    def get(url: str, body: object = None) -> Answer:
        if url.startswith("https://apply.workable.com/"):
            return on_workable(lambda: Answer(200, '{"jobs": []}'))
        return sites(url, body)

    found = tmp_path / "found-jobs.json"

    def alerts(command: list[str]) -> None:
        # The alert's page is read while the boards hold the host's four requests.
        _wait_for(lambda: running[0] == 4)
        AlertSkill({"emails": 1, "jobs": [{
            "link": "https://apply.workable.com/hooli/j/AB12CD34EF/", "title": "QA Automation Engineer",
            "company": "Hooli Example", "place": "Durham, NC", "remote": "on_site", "board": "workable",
            "email_id": "18c0ffee00000009", "email_date": "2026-11-18T06:00:00+00:00",
            "area": "Raleigh-Durham-Chapel Hill"}]})(command)

    def fetch(link: str) -> tuple[str, str]:
        if urlsplit(link).hostname == "apply.workable.com":
            return on_workable(lambda: _fetch(link))
        return _fetch(link)

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    companies.add([Board("workable", f"example-{number}") for number in range(8)])
    search = Search(get=get, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    find(settings, jobs, tmp_path / "jobs", found, run=alerts, fetch=fetch, clock=clock, search=search)
    assert jobs.has("https://apply.workable.com/hooli/j/AB12CD34EF/")
    assert most[0] == 4


def test_hunt_find_help_says_the_sources_run_at_once_and_a_job_from_two_is_listed_once(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "300")
    with pytest.raises(SystemExit):
        cli.main(["find", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "The sources run at once" in help_text
    assert "A job is listed once" in help_text and "found in the last 30 days" in help_text
    design = " ".join((Path(__file__).parents[1] / "docs" / "design.md").read_text().split())
    assert "The sources run at once" in design and "same job from two sources: N" in design


@pytest.mark.parametrize("breaks", ["sweep", "sweep_met"])
def test_a_sweep_that_breaks_is_named_and_the_other_sources_still_save_and_say_how_long(
    tmp_path: Path, settings: Settings, clock: MovableClock, breaks: str
) -> None:
    class Broken(Search):
        def sweep(self, *args: Any, **kwargs: Any) -> Any:
            if breaks == "sweep":
                raise RuntimeError("a bug in the sweep")
            return super().sweep(*args, **kwargs)

        def sweep_met(self, *args: Any, **kwargs: Any) -> Any:
            # The second pass takes a minute of the made-up clock, then breaks.
            clock.advance(seconds=60)
            raise RuntimeError("a bug in the second pass")

    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    companies = _companies(tmp_path, clock)
    companies.indexed()
    found = tmp_path / "found-jobs.json"
    search = Broken(get=_board_sites(), pace=_no_pause(), companies=companies, sleep=lambda seconds: None)
    summary = find(settings, jobs, tmp_path / "jobs", found, run=Skill(found), fetch=_fetch, clock=clock, search=search)
    said = "a bug in the sweep" if breaks == "sweep" else "a bug in the second pass"
    assert summary.failed == [f"boards: RuntimeError: {said}"]
    lines = str(summary).splitlines()
    assert lines[0] == "alert: 4 emails, 8 jobs, 4 new" and lines[1].startswith("linkedin_search: ")
    assert lines[-1] == f"took: alert 0s, linkedin_search 0s, boards {'0s' if breaks == 'sweep' else '1m 0s'}"
    alerts = [job for job in jobs.all() if job.source == "alert"]
    assert len(alerts) == 4 and all((tmp_path / "jobs" / str(job.id) / "posting.md").exists() for job in alerts)


def test_a_linkedin_search_that_gets_no_answer_is_named_and_the_others_go_on(
    settings: Settings, clock: MovableClock
) -> None:
    sites = Sites({LINKEDIN: _ok("linkedin-short-page.html")})

    def get(url: str, body: object = None) -> Answer:
        if "keywords=SDET" in url:
            raise SearchError("no answer in 30 seconds")
        return sites(url, body)

    result = linkedin_search(settings, clock.now - timedelta(days=1), clock.now, get, _no_pause())
    assert result.problems == ["LinkedIn search for SDET in Raleigh-Durham-Chapel Hill: no answer in 30 seconds",
                               "LinkedIn search for SDET in San Francisco Bay Area: no answer in 30 seconds",
                               "LinkedIn search for SDET, remote: no answer in 30 seconds"]
    assert len(result.jobs) == 3 and result.stopped is None
