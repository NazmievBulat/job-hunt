"""Applying: a resume for every picked job, then the applications.

`apply` is the coordinator. It starts one resume worker per picked job, all at once (`claude -p`
with the `write-resume` skill), and moves each job to `resume_ready` once its `resume.pdf` is
built. Then up to three company-site workers at once (`claude --chrome -p` with the `apply-site`
skill), and beside them one LinkedIn worker for the Easy Apply jobs (`apply-linkedin`), one job
after another under the LinkedIn lock (`linkedin_lock`), three to six minutes apart. Each
applies to one job in a Chrome tab of its own and writes `jobs/<id>/report.json`:

    {"result": "applied"}
    {"result": "paused", "needs": "captcha" | "sign_in" | "question" | "check_sent", "question": "...",
     "tab": "...", "tab_id": 123}
    {"result": "paused", "needs": "chrome"}
    {"result": "failed", "reason": "..."}
    {"result": "company_site", "link": "..."}    (the LinkedIn worker only)

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
apart, and at most `HOST_DAILY` a day; the rest wait for the next day.
"""

from __future__ import annotations

import fcntl
import json
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
from job_hunt.jobs import CAPTCHA_ASKS, Job, JobsList, State
from job_hunt.linkedin_lock import LinkedInLock
from resume_kit.contracts.form_answers import asks_self_identification

MODEL = "claude-sonnet-5-5"
RESUME_SKILL = "write-resume"
SITE_SKILL = "apply-site"
LINKEDIN_SKILL = "apply-linkedin"
SITE_WORKERS = 3
# What a site or LinkedIn worker may do in Chrome: open its own tab, read and fill the page, upload the
# resume. No tab is closed (a paused job's tab waits for the user) and no script is run.
CHROME_TOOLS = tuple(f"mcp__claude-in-chrome__{name}" for name in (
    "tabs_context_mcp", "tabs_create_mcp", "navigate", "read_page", "get_page_text", "find", "form_input",
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

    def pause_reason(self) -> str:
        if self.needs is None:
            return f"{UNSURE}: {self.reason}"
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
    # Left for a later hunt apply: a pause tried again later, or a site's limit for the day.
    later: int = 0

    def __str__(self) -> str:
        said = f"{self.resumes} resumes written; {self.applied} applied, {self.paused} paused, {self.failed} failed"
        return said + (f", {self.later} left for later" if self.later else "")


def _folder(root: Path, job: int) -> Path:
    return root / "jobs" / str(job)


def _career(root: Path) -> Path:
    return root / "data" / "career.json"


def resume_command(job: int, root: Path) -> list[str]:
    folder = _folder(root, job)
    arguments = {"job": job, "folder": f"jobs/{job}"}
    # A permission rule's path: `//` starts an absolute one. Edit rules cover the Write tool too.
    return [
        "claude", "-p", f"/{RESUME_SKILL} {json.dumps(arguments)}",
        "--model", MODEL, *ONLY_NAMED_TOOLS,
        "--allowedTools", ",".join([
            f"Read(/{folder}/**)", f"Read(/{_career(root)})", f"Edit(/{folder}/resume-content.json)",
            # This job's build only: another folder's would let it overwrite that job's resume.
            f"Bash(uv run hunt resume jobs/{job})",
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


def read_report(path: Path, skill: str = SITE_SKILL) -> Report:
    """The worker's report, or ApplyError naming what is wrong with it."""
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
        return Report("paused", needs=needs)
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


def apply(
    jobs: JobsList, root: Path, *, run_resume: Run, run_site: Run, run_linkedin: Run, linkedin: LinkedInLock | None = None,
    captcha: str = "ask", sleep: Callable[[float], None] = time.sleep,
) -> Summary:
    (root / "jobs").mkdir(exist_ok=True)
    # One run at a time: two would start two workers on one job.
    with (root / "jobs" / "apply.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ApplyError("another hunt apply is already running") from error
        summary = Summary()
        this = _Run(jobs, root, summary, captcha, jobs.now)
        _left_applying(this)
        _resumes(jobs, root, run_resume, summary)
        _applications(this, run_site, run_linkedin, linkedin or LinkedInLock(root / "data"), sleep)
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
            report = read_report(_folder(this.root, job.id) / "report.json", _skill(job))
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


def _resumes(jobs: JobsList, root: Path, run: Run, summary: Summary) -> None:
    # A job failed once is written again at once; each pass has fewer, as failures count.
    while picked := jobs.in_states("picked"):
        todo = []
        for job in picked:
            _folder(root, job.id).mkdir(parents=True, exist_ok=True)
            if (_folder(root, job.id) / "resume.pdf").exists():
                jobs.move(job.id, "resume_ready")
            else:
                todo.append(job.id)
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            failures = list(zip(todo, pool.map(lambda job: _write_resume(job, root, run), todo)))
        for job, failure in failures:
            if failure is None:
                jobs.move(job, "resume_ready")
                summary.resumes += 1
            elif jobs.fail(job, failure) == "failed":
                summary.failed += 1


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
            return read_report(report, skill)
        except ApplyError:
            return _unsure(error)
    try:
        return read_report(report, skill)
    except ApplyError as error:
        return _unsure(error)


def host(job: Job) -> str:
    """The host a company-site worker applies on, without its `www.`."""
    return (urlsplit(job.apply_link or job.link).hostname or "").lower().removeprefix("www.")


def _next_site(this: _Run, waiting: list[int]) -> tuple[int | None, float | None]:
    """The first waiting job whose host may be started on now, or None and how many seconds until
    one may. A job whose host had its starts of the day waits for the next day."""
    now = this.clock()
    day = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    soonest = None
    for job in list(waiting):
        on = host(this.jobs.get(job))
        starts = this.jobs.starts_on(on, day, but=job)
        if len(starts) >= HOST_DAILY:
            this.jobs.later(job, day + timedelta(days=1))
            waiting.remove(job)
            this.summary.later += 1
            continue
        if starts and (left := (max(starts.values()) + HOST_SPACING - now).total_seconds()) > 0:
            soonest = left if soonest is None else min(soonest, left)
            continue
        return job, None
    return None, soonest


def _applications(this: _Run, run_site: Run, run_linkedin: Run, linkedin: LinkedInLock,
                  sleep: Callable[[float], None]) -> None:
    jobs, root = this.jobs, this.root
    ready = [job for job in jobs.in_states("resume_ready") if jobs.due(job)]
    waiting: dict[str, list[int]] = {SITE_SKILL: [], LINKEDIN_SKILL: []}
    for job in ready:
        if job.kind in ("easy_apply", "company_site"):
            waiting[_skill(job)].append(job.id)
    running: dict[Future[Report], tuple[int, str]] = {}

    def start(job: int, run: Run) -> None:
        skill = _skill(jobs.get(job))
        report = _folder(root, job) / "report.json"
        paused = _paused(report, skill)
        # Gone before the job is `applying`: a run stopped in between must not take an
        # earlier try's report for this one's.
        report.unlink(missing_ok=True)
        jobs.move(job, "applying")
        if skill == SITE_SKILL:
            jobs.started_on(host(jobs.get(job)), job)
        running[pool.submit(_apply_one, jobs.get(job), root, run, paused)] = (job, skill)

    # The jobs list is written here only, on this thread; the workers run the skills.
    try:
        with ThreadPoolExecutor(max_workers=SITE_WORKERS + 1) as pool:
            while any(waiting.values()) or running:
                sites = sum(skill == SITE_SKILL for _, skill in running.values())
                site_later = None
                while waiting[SITE_SKILL] and sites < SITE_WORKERS:
                    chosen, site_later = _next_site(this, waiting[SITE_SKILL])
                    if chosen is None:
                        break
                    waiting[SITE_SKILL].remove(chosen)
                    start(chosen, run_site)
                    sites += 1
                # How long until the LinkedIn worker may start, when it waits for the lock. The
                # lock alone keeps it to one; while this run's own applies, it is not asked.
                later = None
                if waiting[LINKEDIN_SKILL] and all(skill != LINKEDIN_SKILL for _, skill in running.values()):
                    later = linkedin.take(waiting[LINKEDIN_SKILL][0])
                    if later is None:
                        start(waiting[LINKEDIN_SKILL].pop(0), run_linkedin)
                later = min((each for each in (later, site_later) if each is not None), default=None)
                if not running:
                    if later is not None:
                        sleep(later)
                    continue
                done, _ = wait(running, timeout=later, return_when=FIRST_COMPLETED)
                for future in done:
                    job, skill = running.pop(future)
                    if skill == LINKEDIN_SKILL:
                        linkedin.release()
                    if _save(this, job, future.result()) == "resume_ready" and jobs.due(jobs.get(job)):
                        waiting[_skill(jobs.get(job))].append(job)
    finally:
        # A run stopped while the LinkedIn worker applied still starts the pause, once the
        # worker ended.
        linkedin.release()


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
    tab it paused in."""
    _paused_job(jobs, job)
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
