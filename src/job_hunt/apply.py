"""Applying: a resume for every picked job, then the applications.

`apply` is the coordinator. It starts one resume worker per picked job, up to `RESUME_WORKERS` at
once (`claude -p` with the `write-resume` skill), and moves each job to `resume_ready` once its
`resume.pdf` is built. Then up to three company-site workers at once, or the day's plan's
`site_workers` (`claude --chrome -p` with the `apply-site` skill), and beside them one LinkedIn worker for the Easy Apply jobs (`apply-linkedin`), one job
after another under the LinkedIn lock (`linkedin_lock`), three to six minutes apart. Each
applies to one job in a Chrome tab of its own and writes `jobs/<id>/report.json`:

    {"result": "applied"}
    {"result": "paused", "needs": "captcha" | "sign_in" | "question" | "check_sent", "question": "...",
     "tab": "...", "tab_id": 123}
    {"result": "paused", "needs": "chrome", "connected": "..."}
    {"result": "failed", "reason": "..."}
    {"result": "company_site", "link": "..."}    (the LinkedIn worker only)

Each worker first selects this computer's Chrome among the browsers the account has connected,
never another computer's, and every report but a Chrome out of reach names it:
`"browser": {"name": ..., "system": ..., "on_this_computer": true}`. A report that does not is
refused as one that cannot be read: the application may have gone from another computer's
Chrome, so the user looks. With only other computers' browsers connected, the worker pauses
for Chrome (`connected` names them), which costs the job no try.

The LinkedIn worker takes every job applied to on LinkedIn: the Easy Apply jobs, and the
company-site jobs whose link is still their LinkedIn page, which only LinkedIn can lead to the
company's site. Its last report is for those, and for an Easy Apply job whose page sends to
the company's site: the job becomes a company-site job applied to at that link, in the same
run, with no try spent. The coordinator saves each report as the job's state. A `failed` report counts against its job
(`JobsList.fail`): tried once more, then `failed`. A worker that stops or ends with no report it
can be read by may have sent the application, so its job is paused for the user to look in
Chrome (`UNSURE`) and never tried again on its own. Run again, it picks up from each job's state
and never starts a second worker on a job that was being applied to. A resume worker sends
nothing: one that fails counts against its job.

A pause for a CAPTCHA or a sign-in is first tried again on its own, once, `RETRY_AFTER` later and
in a fresh tab (`JobsList.retry`): the site may not ask again, and a password manager may have
signed the browser in meanwhile. A job still on a CAPTCHA after that waits for the person, who
is asked when they are free (`available`); after `CAPTCHA_ASKS` questions, or at once with the
settings' `captcha` set to `skip`, it is `failed`, to apply by hand.

Fewer CAPTCHAs: two company-site workers start on one site's host at least `HOST_SPACING`
apart, and at most `HOST_DAILY` a day; the rest wait for the next day. A job going on in the tab
it paused in is not held: it is the same application.

With the day's plan (`plan`), the jobs start in its order, leftovers of an earlier day first,
spread over the day, up to its daily numbers, from the morning hour to the summary hour; the rest
wait for the next morning hour. A job whose title was applied to at the same company lately is left out
(skipped), whatever the plan. A job skipped while the run goes on is not started; one picked
or put back meanwhile is taken by the same run, its resume written first.
"""

from __future__ import annotations

import fcntl
import json
import shlex
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from job_hunt.career import career_lock, read_career, write_career
from job_hunt.finder import ONLY_NAMED_TOOLS
from job_hunt.jobs import CAPTCHA_ASKS, Job, JobsList, MoveError, State
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.plan import LINKEDIN_HOST, RESUME_WORKERS, Plan, applied_lately, gap, midnight, order, started_today
from resume_kit.contracts.form_answers import asks_self_identification

MODEL = "claude-sonnet-5-5"
RESUME_SKILL = "write-resume"
SITE_SKILL = "apply-site"
LINKEDIN_SKILL = "apply-linkedin"
SITE_WORKERS = 3
# What a site or LinkedIn worker may do in Chrome: select this computer's Chrome, open its own tab,
# read and fill the page, upload the resume. No tab is closed (a paused job's tab waits for the
# user), no script is run, and `switch_browser` is left out: it asks a person in every browser.
CHROME_TOOLS = tuple(f"mcp__claude-in-chrome__{name}" for name in (
    "list_connected_browsers", "select_browser", "tabs_context_mcp", "tabs_create_mcp", "navigate", "read_page", "get_page_text", "find", "form_input",
    "computer", "file_upload",
))
# What a job paused with no report it can be read by needs: the application may have been sent.
UNSURE = "a look in Chrome whether it was sent"
NEEDS = {"captcha": "a CAPTCHA solved", "sign_in": "a sign-in or a new account", "question": "an answer",
         # Submitted, but the site did not confirm it.
         "check_sent": UNSURE,
         # Chrome closed or its extension not connected: no tab, and no try of the job spent.
         "chrome": "Chrome open, with the Claude extension connected"}

# A pause tried again once on its own, this much later.
RETRIED = ("captcha", "sign_in")
RETRY_AFTER = timedelta(hours=3)
CAPTCHA_FAILED = "the site asks for a CAPTCHA"
HOST_SPACING = timedelta(minutes=10)
HOST_DAILY = 6
# The longest the run waits without looking for jobs put back meanwhile: an answered job or a
# solved CAPTCHA goes on in its tab within it, not after the spread's 15 minutes.
NAP = 60.0
# Half a second of tries at the run's lock.
LOCK_TRIES = 10

Run = Callable[[list[str]], None]


class ApplyError(RuntimeError):
    """A worker run failed, or `hunt apply` cannot run now."""


class AnswerError(ValueError):
    """An answer or a resume the job cannot take, in words for the user."""


@dataclass(frozen=True)
class Report:
    result: Literal["applied", "paused", "failed", "company_site"]
    needs: str | None = None
    question: str | None = None
    tab: str | None = None
    # The id Chrome's tools give the tab: two paused tabs may share a title.
    tab_id: int | None = None
    reason: str | None = None
    # The company's site an Easy Apply job sends to.
    link: str | None = None
    # Chrome out of reach while other computers' browsers are connected: those, named.
    connected: str | None = None

    def pause_reason(self) -> str:
        if self.needs is None:
            return f"{UNSURE}: {self.reason}"
        if self.needs == "chrome" and self.connected:
            return f"{NEEDS['chrome']}, on this computer (connected now: {self.connected}; workers never use them)"
        if self.needs == "chrome":
            return NEEDS["chrome"]
        what = f'an answer to "{self.question}"' if self.needs == "question" else NEEDS[self.needs or ""]
        return f'{what}, in the Chrome tab "{self.tab}"'


@dataclass
class Summary:
    resumes: int = 0
    applied: int = 0
    paused: int = 0
    failed: int = 0
    # Left for a later hunt apply: a pause tried again later, or a site's or the day's limit.
    later: int = 0
    # Skipped: the same title at the same company was applied to lately.
    left_out: int = 0

    def __str__(self) -> str:
        said = f"{self.resumes} resumes written; {self.applied} applied, {self.paused} paused, {self.failed} failed"
        said += f", {self.later} left for later" if self.later else ""
        return said + (f", {self.left_out} left out as applied to lately" if self.left_out else "")


def _folder(root: Path, job: int) -> Path:
    return root / "jobs" / str(job)


def _career(root: Path) -> Path:
    return root / "data" / "career.json"


def resume_command(job: int, root: Path) -> list[str]:
    folder = _folder(root, job)
    # The worker runs this very text, so it matches the rule word for word, a folder with a
    # space in it included.
    build = f"uv run hunt resume {shlex.quote(str(folder))}"
    arguments = {"job": job, "folder": str(folder), "career": str(_career(root)), "build": build}
    # A permission rule's path: `//` starts an absolute one. Edit rules cover the Write tool too.
    return [
        "claude", "-p", f"/{RESUME_SKILL} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL, *ONLY_NAMED_TOOLS,
        "--allowedTools", ",".join([
            f"Read(/{folder}/**)", f"Read(/{_career(root)})", f"Edit(/{folder}/resume-content.json)",
            # This job's build only: another folder's would let it overwrite that job's resume.
            f"Bash({build})",
        ]),
    ]


def site_command(job: Job, root: Path, paused: Report | None = None) -> list[str]:
    """With the report of the job's pause, the worker carries on in the tab it paused in, where
    the user just did what it needed, instead of starting again in a new one."""
    return _chrome_command(SITE_SKILL, job, job.apply_link or job.link, root, paused)


def linkedin_command(job: Job, root: Path, paused: Report | None = None) -> list[str]:
    """As `site_command`, on the job's LinkedIn page."""
    return _chrome_command(LINKEDIN_SKILL, job, _linkedin_page(job) or job.link, root, paused)


def _chrome_command(skill: str, job: Job, link: str, root: Path, paused: Report | None) -> list[str]:
    folder = _folder(root, job.id)
    arguments = {"job": job.id, "link": link, "title": job.title, "company": job.company,
                 "resume": str(folder / "resume.pdf"), "career": str(_career(root)),
                 "report": str(folder / "report.json")}
    # A pause with no tab (Chrome out of reach) starts again in a new one.
    if paused is not None and paused.tab is not None:
        arguments |= {"tab": paused.tab, "tab_id": paused.tab_id}
    return [
        "claude", "--chrome", "-p", f"/{skill} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL, *ONLY_NAMED_TOOLS,
        "--allowedTools", ",".join([
            f"Read(/{folder}/**)", f"Read(/{_career(root)})", f"Edit(/{folder}/report.json)", *CHROME_TOOLS,
        ]),
    ]


def _text(data: dict, name: str) -> str | None:
    value = data.get(name)
    return (" ".join(value.split()) or None) if isinstance(value, str) else None


def _on_linkedin(link: str) -> bool:
    host = (urlsplit(link).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _linkedin_page(job: Job) -> str | None:
    """The job's LinkedIn page, the one its link ended at first: a click-tracking link may lead
    there. None once LinkedIn sent the job on to the company's site."""
    if job.apply_link is not None:
        return None
    return next((link for link in (job.page_link, job.link) if link and _on_linkedin(link)), None)


def _skill(job: Job) -> str:
    """Which worker applies to the job: a company-site worker never uses LinkedIn, as it would
    apply beside the LinkedIn worker."""
    if job.kind == "easy_apply" or (job.kind == "company_site" and _linkedin_page(job)):
        return LINKEDIN_SKILL
    return SITE_SKILL


def _this_computers_chrome(data: dict) -> bool:
    browser = data.get("browser")
    return (isinstance(browser, dict) and browser.get("on_this_computer") is True
            and bool(_text(browser, "name")) and bool(_text(browser, "system")))


def read_report(path: Path, skill: str = SITE_SKILL, *, worker: bool = False) -> Report:
    """The worker's report, or ApplyError naming what is wrong with it. With `worker`, a report
    just written by a worker, which must name this computer's Chrome unless it reached none; an
    earlier one read again (to answer its question, to go on in its tab) is taken as it is."""
    if not path.exists():
        raise ApplyError(f"the {skill} run wrote no report")
    try:
        data = json.loads(path.read_text())
    # Not UTF-8: cut off inside a character by a worker that stopped while writing it.
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ApplyError(f"the {skill} run wrote a report that is not JSON: {error}") from error
    results = ("applied", "paused", "failed", "company_site") if skill == LINKEDIN_SKILL else ("applied", "paused", "failed")
    if not isinstance(data, dict) or data.get("result") not in results:
        raise ApplyError(f'the {skill} run wrote a report whose "result" is not {", ".join(results[:-1])} or {results[-1]}')
    report = _report(data, skill)
    # Checked last: a report that cannot be read says first what is wrong with it.
    if worker and report.needs != "chrome" and not _this_computers_chrome(data):
        raise ApplyError(f"the {skill} run did not say it used this computer's Chrome")
    return report


def _report(data: dict, skill: str) -> Report:
    if data["result"] == "applied":
        return Report("applied")
    if data["result"] == "company_site":
        link = data.get("link")
        try:
            parts = urlsplit(link := link.strip()) if isinstance(link, str) else None
            address = parts is not None and parts.scheme in ("http", "https") and bool(parts.hostname)
            # Read only to raise on a port that is not a number.
            _ = parts is not None and parts.port
        # A broken address, such as "https://[broken" or "https://example.com:abc".
        except ValueError:
            address = False
        if not address:
            raise ApplyError(f"the {skill} run sent the job to the company's site without its web address")
        if _on_linkedin(link):
            raise ApplyError(f"the {skill} run sent the job to the company's site with a LinkedIn address")
        return Report("company_site", link=link)
    if data["result"] == "failed":
        if not (reason := _text(data, "reason")):
            raise ApplyError(f"the {skill} run reported a failure without a reason")
        return Report("failed", reason=reason)
    needs = data.get("needs")
    if not isinstance(needs, str) or needs not in NEEDS:
        raise ApplyError(f'the {skill} run paused, but "needs" is not one of {", ".join(NEEDS)}')
    if needs == "chrome":
        # "none": no browser connected anywhere, so there are none to name.
        connected = _text(data, "connected")
        return Report("paused", needs=needs, connected=None if (connected or "").lower() == "none" else connected)
    if not (tab := _text(data, "tab")):
        raise ApplyError(f"the {skill} run paused without naming the tab")
    tab_id = data.get("tab_id")
    if tab_id is not None and type(tab_id) is not int:
        raise ApplyError(f'the {skill} run paused with a "tab_id" that is not a whole number')
    question = _text(data, "question")
    if needs == "question" and not question:
        raise ApplyError(f"the {skill} run paused on a question without the question")
    return Report("paused", needs=needs, question=question if needs == "question" else None, tab=tab, tab_id=tab_id)


@dataclass(frozen=True)
class _Run:
    """What the parts of one `hunt apply` share."""
    jobs: JobsList
    root: Path
    summary: Summary
    captcha: str
    clock: Callable[[], datetime]
    plan: Plan | None = None


def apply(
    jobs: JobsList, root: Path, *, run_resume: Run, run_site: Run, run_linkedin: Run, linkedin: LinkedInLock | None = None,
    captcha: str = "ask", sleep: Callable[[float], None] = time.sleep, plan: Plan | None = None,
) -> Summary:
    """Without a plan, only the limits of this module hold: no daily number, no spread, no end of
    the day."""
    (root / "jobs").mkdir(exist_ok=True)
    # One run at a time: two would start two workers on one job.
    with (root / "jobs" / "apply.lock").open("w") as lock:
        # A few tries: `hunt questions` holds it for a moment to see whether a run holds it.
        for tries in range(LOCK_TRIES, 0, -1):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if tries == 1:
                    # Said to the person after "done" or an answer: the run going on takes it.
                    raise ApplyError("another hunt apply is already running;"
                                     " it takes the jobs put back within a minute") from error
                time.sleep(0.05)
        summary = Summary()
        this = _Run(jobs, root, summary, captcha, jobs.now, plan)
        _left_applying(this)
        _resumes(jobs, root, run_resume, summary)
        # A run spread over the day takes the jobs picked meanwhile too, by a later morning run.
        while _applications(this, run_site, run_linkedin, linkedin or LinkedInLock(root / "data"), sleep):
            _resumes(jobs, root, run_resume, summary)
        return summary


def _save(this: _Run, job: int, report: Report) -> State:
    jobs, summary = this.jobs, this.summary
    if report.result == "paused" and report.needs in RETRIED and not jobs.get(job).retries:
        jobs.retry(job, this.clock() + RETRY_AFTER)
        # The try again opens a fresh tab: the report of this pause is not carried on from.
        (_folder(this.root, job) / "report.json").unlink(missing_ok=True)
        summary.later += 1
    elif report.result == "paused" and report.needs == "captcha" and (
            this.captcha == "skip" or jobs.get(job).asks >= CAPTCHA_ASKS):
        jobs.move(job, "failed", CAPTCHA_FAILED)
        summary.failed += 1
    elif report.result == "applied":
        jobs.move(job, "applied")
        summary.applied += 1
    elif report.result == "company_site":
        jobs.to_company_site(job, report.link or "")
    elif report.result == "paused":
        jobs.move(job, "paused", report.pause_reason())
        summary.paused += 1
    elif jobs.fail(job, report.reason or "") == "failed":
        summary.failed += 1
    return jobs.get(job).state


def _left_applying(this: _Run) -> None:
    """Jobs an earlier run stopped on while their worker ran: its report, when it wrote one, is
    saved; without one the application may have been sent, so the user looks."""
    for job in this.jobs.in_states("applying"):
        try:
            report = read_report(_folder(this.root, job.id) / "report.json", _skill(job), worker=True)
        except ApplyError:
            this.jobs.move(job.id, "paused", f"{UNSURE}: the last hunt apply stopped while applying")
            this.summary.paused += 1
            continue
        _save(this, job.id, report)


def _write_resume(job: int, root: Path, run: Run) -> str | None:
    """Why the resume worker failed, or None when it built the PDF."""
    try:
        run(resume_command(job, root))
    except ApplyError as error:
        return str(error)
    return None if (_folder(root, job) / "resume.pdf").exists() else f"the {RESUME_SKILL} run built no resume.pdf"


def _moved(jobs: JobsList, job: int, state: State) -> bool:
    """Move the job, or leave it as another command moved it meanwhile (`hunt skip` from the
    chat): the run goes on with the others."""
    try:
        jobs.move(job, state)
    except MoveError:
        return False
    return True


def _failed(jobs: JobsList, job: int, failure: str) -> bool:
    """Count a failed resume against the job: whether it failed for good. Skipped meanwhile, it
    is left as it is."""
    try:
        return jobs.fail(job, failure) == "failed"
    except MoveError:
        return False


def _later(jobs: JobsList, job: int, not_before: datetime) -> bool:
    """Leave the job for `not_before`, unless another command moved it meanwhile."""
    try:
        jobs.later(job, not_before)
    except MoveError:
        return False
    return True


def _resumes(jobs: JobsList, root: Path, run: Run, summary: Summary) -> None:
    # A job failed once is written again at once; each pass has fewer, as failures count.
    while picked := jobs.in_states("picked"):
        todo = []
        for job in picked:
            _folder(root, job.id).mkdir(parents=True, exist_ok=True)
            if (_folder(root, job.id) / "resume.pdf").exists():
                _moved(jobs, job.id, "resume_ready")
            else:
                todo.append(job.id)
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=min(len(todo), RESUME_WORKERS)) as pool:
            failures = list(zip(todo, pool.map(lambda job: _write_resume(job, root, run), todo)))
        for job, failure in failures:
            # Skipped meanwhile: its resume is never used.
            if jobs.get(job).state != "picked":
                continue
            if failure is None:
                summary.resumes += _moved(jobs, job, "resume_ready")
            else:
                summary.failed += _failed(jobs, job, failure)


def _paused(report: Path, skill: str) -> Report | None:
    try:
        earlier = read_report(report, skill)
    except ApplyError:
        return None
    return earlier if earlier.result == "paused" else None


def _unsure(error: ApplyError) -> Report:
    return Report("paused", reason=str(error))


def _apply_one(job: Job, root: Path, run: Run, paused: Report | None) -> Report:
    skill = _skill(job)
    report = _folder(root, job.id) / "report.json"
    command = linkedin_command if skill == LINKEDIN_SKILL else site_command
    try:
        run(command(job, root, paused))
    except ApplyError as error:
        # A worker that wrote its report and then stopped badly still did what it reports: an
        # application it sent must not be sent again.
        try:
            return read_report(report, skill, worker=True)
        except ApplyError:
            return _unsure(error)
    try:
        return read_report(report, skill, worker=True)
    except ApplyError as error:
        return _unsure(error)


def host(job: Job) -> str:
    """The host a company-site worker applies on, without its `www.`: where its link ended, not
    a click-tracking link's own host."""
    return (urlsplit(job.apply_link or job.page_link or job.link).hostname or "").lower().removeprefix("www.")


def _midnight(now: datetime, days: int = 0) -> datetime:
    """Midnight of the computer's day of `now`, `days` on, at midnight's own offset: on a day the
    clocks change, it is not the offset of `now`."""
    return datetime.combine(now.astimezone().date() + timedelta(days=days), datetime.min.time()).astimezone()


def _continues(this: _Run, job: int) -> bool:
    """Going on in the tab it paused in, after "done": the same application, and a solved CAPTCHA
    does not last a wait."""
    paused = _paused(_folder(this.root, job) / "report.json", _skill(this.jobs.get(job)))
    return paused is not None and paused.tab is not None


def _held(this: _Run, job: int, skill: str) -> bool:
    """Whether the job leaves the waiting: skipped meanwhile, left out as its title was applied to
    lately, or past the day's plan, for the next morning."""
    jobs, plan, now = this.jobs, this.plan, this.clock()
    found = jobs.get(job)
    if found.state != "resume_ready":
        return True
    if _continues(this, job):
        return False
    # One answered or put back after its CAPTCHA, before this starts, is still that application.
    going_on = [other.id for other in jobs.in_states("resume_ready") if other.id != job and _continues(this, other.id)]
    if applied_lately(jobs, found, now, going_on=going_on) is not None:
        this.summary.left_out += _moved(jobs, job, "skipped")
        return True
    if plan is None:
        return False
    linkedin = jobs.starts_on(LINKEDIN_HOST, midnight(now), but=job)
    if (not plan.day_start(now) <= now < plan.day_end(now) or len(started_today(jobs, now) - {job}) >= plan.daily
            or (skill == LINKEDIN_SKILL and len(linkedin) >= plan.linkedin_daily)):
        this.summary.later += _later(jobs, job, plan.next_morning(now))
        return True
    return False


def _spread(this: _Run, waiting: int) -> float | None:
    """How many seconds until the plan's spread lets a new application start, or None when one
    may start now."""
    if this.plan is None:
        return None
    now = this.clock()
    starts = this.jobs.started_since(midnight(now))
    if not starts:
        return None
    # From the last start, the day shared as it was then: rechecked each nap, it stays put.
    last = max(starts.values())
    left = last + gap(this.plan, last, min(waiting, this.plan.daily - len(starts))) - now
    return left.total_seconds() if left > timedelta(0) else None


def _next_site(this: _Run, waiting: list[int], spread: float | None = None) -> tuple[int | None, float | None]:
    """The first waiting job whose host may be started on now, or None and how many seconds until
    one may. A job whose host had its starts of the day waits for the next day; with a `spread`
    to wait, only a job going on in its tab starts."""
    now = this.clock()
    day = _midnight(now)
    soonest = spread
    for job in list(waiting):
        if _held(this, job, SITE_SKILL):
            waiting.remove(job)
            continue
        if _continues(this, job):
            return job, None
        if spread is not None:
            continue
        on = host(this.jobs.get(job))
        if len(this.jobs.starts_on(on, day, but=job)) >= HOST_DAILY:
            this.summary.later += _later(this.jobs, job, _midnight(now, days=1))
            waiting.remove(job)
            continue
        # Also across midnight: the last start of yesterday counts.
        recent = this.jobs.starts_on(on, now - HOST_SPACING, but=job)
        if recent and (left := (max(recent.values()) + HOST_SPACING - now).total_seconds()) > 0:
            soonest = left if soonest is None else min(soonest, left)
            continue
        return job, None
    return None, soonest


def _applications(this: _Run, run_site: Run, run_linkedin: Run, linkedin: LinkedInLock,
                  sleep: Callable[[float], None]) -> bool:
    """Apply to the jobs ready now and to those ready meanwhile. True when it stopped, with
    nothing running, for jobs picked meanwhile, whose resumes are written first, or for jobs put
    back as it ended."""
    jobs, root = this.jobs, this.root
    waiting: dict[str, list[int]] = {SITE_SKILL: [], LINKEDIN_SKILL: []}
    running: dict[Future[Report], tuple[int, str]] = {}

    def gather() -> None:
        """The jobs ready and due that wait for no worker yet: at the start, and each turn the ones
        a time came for or another command put back."""
        known = {job for line in waiting.values() for job in line} | {job for job, _ in running.values()}
        new = [job for job in jobs.in_states("resume_ready")
               if jobs.due(job) and job.id not in known and job.kind in ("easy_apply", "company_site")]
        if not new:
            return
        for job in new:
            waiting[_skill(job)].append(job.id)
        # The whole line in the plan's order: a leftover due now goes before today's picks.
        for skill, line in waiting.items():
            waiting[skill] = [job.id for job in order([jobs.get(job) for job in line])]

    def start(job: int, run: Run) -> bool:
        """Start the job's worker; False when the job was moved meanwhile (skipped from the chat)."""
        skill = _skill(jobs.get(job))
        report = _folder(root, job) / "report.json"
        paused = _paused(report, skill)
        # Gone before the job is `applying`: a run stopped in between must not take an
        # earlier try's report for this one's.
        report.unlink(missing_ok=True)
        try:
            jobs.move(job, "applying")
        except MoveError:
            return False
        jobs.started_on(host(jobs.get(job)) if skill == SITE_SKILL else LINKEDIN_HOST, job)
        running[pool.submit(_apply_one, jobs.get(job), root, run, paused)] = (job, skill)
        return True

    # The jobs list is written here only, on this thread; the workers run the skills.
    try:
        workers = this.plan.site_workers if this.plan is not None else SITE_WORKERS
        with ThreadPoolExecutor(max_workers=workers + 1) as pool:
            def sites() -> float | None:
                """Start the company-site jobs that may start now; how long until one may."""
                busy = sum(skill == SITE_SKILL for _, skill in running.values())
                site_later = None
                while waiting[SITE_SKILL] and busy < workers:
                    spread = _spread(this, sum(map(len, waiting.values())))
                    chosen, site_later = _next_site(this, waiting[SITE_SKILL], spread)
                    if chosen is None:
                        break
                    waiting[SITE_SKILL].remove(chosen)
                    busy += start(chosen, run_site)
                return site_later

            def linkedin_one() -> float | None:
                """Start the next LinkedIn job when it may; how long until it may, when it waits for
                the lock or the spread. The lock alone keeps it to one; while this run's own
                applies, it is not asked."""
                in_line = waiting[LINKEDIN_SKILL]
                # A job going on in its tab goes first: it waits for no spread, and a solved
                # CAPTCHA does not last one.
                going_on = [job for job in in_line if _continues(this, job)]
                in_line[:] = going_on + [job for job in in_line if job not in going_on]
                while in_line and all(skill != LINKEDIN_SKILL for _, skill in running.values()):
                    if _held(this, in_line[0], LINKEDIN_SKILL):
                        in_line.pop(0)
                        continue
                    if not _continues(this, in_line[0]) and (
                            later := _spread(this, sum(map(len, waiting.values())))) is not None:
                        return later
                    later = linkedin.take(in_line[0])
                    if later is None and not start(in_line.pop(0), run_linkedin):
                        # Skipped as it started: the lock goes back, and the next one waits the pause.
                        linkedin.release()
                        continue
                    return later
                return None

            gather()
            while any(waiting.values()) or running:
                gather()
                # The next start, of the spread or the day's number, goes to the job first in the
                # plan's order, of either kind: the LinkedIn worker first when its job comes first.
                first = [jobs.get(line[0]) for line in waiting.values() if line]
                if len(first) == 2 and order(first)[0].id == waiting[LINKEDIN_SKILL][0]:
                    linkedin_later = linkedin_one()
                    site_later = sites()
                else:
                    site_later = sites()
                    linkedin_later = linkedin_one()
                later = min((each for each in (linkedin_later, site_later) if each is not None), default=None)
                if not running:
                    if jobs.in_states("picked"):
                        return True
                    if later is not None:
                        sleep(min(later, NAP))
                    continue
                done, _ = wait(running, timeout=min(later, NAP) if later is not None else NAP,
                               return_when=FIRST_COMPLETED)
                for future in done:
                    job, skill = running.pop(future)
                    if skill == LINKEDIN_SKILL:
                        linkedin.release()
                    if _save(this, job, future.result()) == "resume_ready" and jobs.due(jobs.get(job)):
                        waiting[_skill(jobs.get(job))].append(job)
            # Picked or put back while the last application ran: the same run goes on, a picked
            # job's resume first.
            return bool(jobs.in_states("picked")) or any(
                job.kind in ("easy_apply", "company_site") and jobs.due(job) for job in jobs.in_states("resume_ready"))
    finally:
        # A run stopped while the LinkedIn worker applied still starts the pause, once the
        # worker ended.
        linkedin.release()
    return False


def apply_running(root: Path) -> bool:
    """Whether a `hunt apply` holds its lock now: a run spread over the day takes the jobs ready
    meanwhile itself."""
    path = root / "jobs" / "apply.lock"
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        # Let go at once: a run starting now must not find it held.
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def skip_captchas(jobs: JobsList) -> list[Job]:
    """With the settings' `captcha` set to `skip`: the jobs already waiting on a CAPTCHA are
    failed too, to apply by hand, and never asked."""
    skipped = [job for job in jobs.in_states("paused") if (job.state_reason or "").startswith(NEEDS["captcha"])]
    for job in skipped:
        jobs.move(job.id, "failed", CAPTCHA_FAILED)
    return skipped


def _paused_job(jobs: JobsList, job: int) -> Job:
    try:
        found = jobs.get(job)
    except KeyError as error:
        raise AnswerError(f"there is no job {job}") from error
    if found.state != "paused":
        raise AnswerError(f"job {job} is not paused, it is {found.state}")
    return found


def applied(jobs: JobsList, job: int) -> Job:
    """End a paused job the user saw was sent in Chrome; no worker applies to it again."""
    _paused_job(jobs, job)
    jobs.move(job, "applied")
    return jobs.get(job)


def resume_job(jobs: JobsList, job: int) -> Job:
    """Put a paused job back once the user did what it needed; the next run applies to it, in the
    tab it paused in. A "done" to a CAPTCHA counts as its question, once a pause, also when it
    was asked from `hunt status` and not `hunt questions`."""
    if (_paused_job(jobs, job).state_reason or "").startswith(NEEDS["captcha"]):
        jobs.asked(job)
    jobs.move(job, "resume_ready")
    return jobs.get(job)


def answer(jobs: JobsList, root: Path, job: int, text: str) -> Job:
    """Save the answer to a paused job's question in the career's form answers, so no form asks
    it again, and put the job back."""
    found = _paused_job(jobs, job)
    try:
        report = read_report(_folder(root, job) / "report.json", _skill(found))
    except ApplyError:
        report = None
    if report is None or report.needs != "question" or not report.question:
        raise AnswerError(f"job {job} waits for no answer: once it is done in Chrome, run hunt resume-job {job}")
    if asks_self_identification(report.question):
        raise AnswerError("answers about gender, race, ethnicity, veteran status or disability are never kept: "
                          "answer it in the Chrome tab, then run hunt resume-job " + str(job))
    if not (text := " ".join(text.split())):
        raise AnswerError("the answer is empty")
    # One answer at a time: two read at once would each write the career without the other's.
    with career_lock(_career(root)):
        career = read_career(_career(root))
        saved = [entry for entry in career.form_answers.get("saved") or []
                 if entry.get("question", "").casefold() != report.question.casefold()]
        saved.append({"question": report.question, "answer": text, "company": found.company})
        career.form_answers = {**career.form_answers, "saved": saved}
        write_career(career, _career(root))
    jobs.move(job, "resume_ready")
    return jobs.get(job)
