"""The company index's Common Crawl queries when the index does not answer. No network: the
answers come from `Sites` of `test_search.py`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from alert_skill import AlertSkill
from job_hunt.companies import CRAWL_PAUSE, CRAWL_TRIES, Board, Companies, crawl_index
from job_hunt.finder import find
from job_hunt.jobs import JobsList
from job_hunt.search import Answer, Search, SearchError
from job_hunt.settings import Settings, read_settings
from movable_clock import MovableClock
from test_search import FINDER, LINKEDIN, Sites, _board_sites, _crawl_sites, _no_pause

CRAWLS = "https://index.commoncrawl.org/collinfo.json"
# What `http_get` raises when the index closes the connection without an answer.
CLOSED = SearchError("Remote end closed connection without response")


class Closing:
    """The index closing the connection `times` times (every time with None), then answering."""

    def __init__(self, sites: Sites, times: int | None) -> None:
        self.sites, self.times, self.closed = sites, times, 0

    def __call__(self, url: str, body: object = None) -> Answer:
        if url.startswith(CRAWLS) and (self.times is None or self.closed < self.times):
            self.closed += 1
            raise CLOSED
        return self.sites(url, body)


@pytest.fixture
def settings() -> Settings:
    return read_settings(FINDER / "settings.json")


@pytest.fixture
def clock() -> MovableClock:
    return MovableClock(datetime(2026, 11, 18, 7, 0, tzinfo=UTC))


def test_common_crawl_is_retried_3_times_30_seconds_apart() -> None:
    assert (CRAWL_TRIES, CRAWL_PAUSE) == (4, 30.0)


def test_common_crawl_closing_the_connection_twice_then_answering_fills_the_index() -> None:
    slept: list[float] = []
    get = Closing(_crawl_sites(), times=2)
    boards, problems = crawl_index(get, sleep=slept.append)
    assert (len(boards), problems) == (2, [])
    assert (get.closed, slept) == (2, [30.0, 30.0])


def test_common_crawl_closing_the_connection_every_time_is_named_once_after_its_tries() -> None:
    slept: list[float] = []
    get = Closing(_crawl_sites(), times=None)
    boards, problems = crawl_index(get, sleep=slept.append)
    assert boards == []
    assert problems == ["Common Crawl's list of crawls: Remote end closed connection without response"]
    assert (get.closed, slept) == (4, [30.0] * 3)


def test_with_common_crawl_never_answering_the_boards_the_alerts_led_to_are_still_swept(
    tmp_path: Path, settings: Settings, clock: MovableClock
) -> None:
    alert = {"link": "https://boards.greenhouse.io/examplelabs/jobs/7001", "title": "QA Automation Engineer",
             "company": "Example Labs", "place": "Durham, NC", "remote": "on_site", "board": "company",
             "email_id": "18c0ffee00000009", "email_date": "2026-11-18T06:00:00+00:00",
             "area": "Raleigh-Durham-Chapel Hill"}
    sites = _board_sites()
    sites.answers[LINKEDIN] = Answer(200, "")
    get = Closing(sites, times=None)
    companies = Companies(tmp_path / "companies.db", clock=clock)
    jobs = JobsList(tmp_path / "jobs.db", clock=clock)
    search = Search(get=get, pace=_no_pause(), companies=companies, sleep=lambda seconds: None)

    def page(link: str) -> tuple[str, str]:
        return link, "<html><body><main><p>Test the robots.</p></main></body></html>"

    summary = find(settings, jobs, tmp_path / "jobs", tmp_path / "found-jobs.json",
                   run=AlertSkill({"emails": 1, "jobs": [alert]}), fetch=page, clock=clock, search=search)
    assert "companies: Common Crawl's list of crawls: Remote end closed connection without response" in summary.failed
    assert "companies: 0 added from Common Crawl, 0 in the index" in str(summary).splitlines()
    # Its board came from the alert's link, and was swept in the same run.
    assert Board("greenhouse", "examplelabs") in companies.to_sweep(full=True)
    assert sites.urls("https://boards-api.greenhouse.io/v1/boards/examplelabs/")
    assert any(line.startswith("greenhouse: ") and "(0 boards)" not in line for line in str(summary).splitlines())
    # The index is due again at the next run: a crawl that gave no board is tried again.
    assert companies.index_due()
