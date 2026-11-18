"""Applying: a resume for every picked job, then the company-site applications.

`apply` is the coordinator. It starts one resume worker per picked job, all at once (`claude -p`
with the `write-resume` skill), and moves each job to `resume_ready` once its `resume.pdf` is
built. Then up to three company-site workers at once (`claude --chrome -p` with the `apply-site`
skill) each apply to one job in a Chrome tab of their own and write `jobs/<id>/report.json`:

    {"result": "applied"}
    {"result": "paused", "needs": "captcha" | "sign_in" | "question", "question": "...", "tab": "...",
     "tab_id": 123}
    {"result": "failed", "reason": "..."}

The coordinator saves each report as the job's state. A worker that fails, writes no report or
one that cannot be read counts against its job (`JobsList.fail`): tried once more, then `failed`.
Run again, it picks up from each job's state and never starts a second worker on a job that was
being applied to. Easy Apply jobs keep their resume for the LinkedIn worker.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from job_hunt.career import read_career, write_career
from job_hunt.jobs import Job, JobsList, State
from resume_kit.contracts.form_answers import asks_self_identification

MODEL = "claude-sonnet-5-5"
RESUME_SKILL = "write-resume"
SITE_SKILL = "apply-site"
SITE_WORKERS = 3
# What a site worker may do in Chrome: open its own tab, read and fill the page, upload the
# resume. No tab is closed (a paused job's tab waits for the user) and no script is run.
CHROME_TOOLS = tuple(f"mcp__claude-in-chrome__{name}" for name in (
    "tabs_context_mcp", "tabs_create_mcp", "navigate", "read_page", "get_page_text", "find", "form_input",
    "computer", "file_upload",
))
NEEDS = {"captcha": "a CAPTCHA solved", "sign_in": "a sign-in or a new account", "question": "an answer"}

Run = Callable[[list[str]], None]


class ApplyError(RuntimeError):
    """A worker run failed, or `hunt apply` cannot run now."""


class AnswerError(ValueError):
    """An answer or a resume the job cannot take, in words for the user."""


@dataclass(frozen=True)
class Report:
    result: Literal["applied", "paused", "failed"]
    needs: str | None = None
    question: str | None = None
    tab: str | None = None
    # The id Chrome's tools give the tab: two paused tabs may share a title.
    tab_id: int | None = None
    reason: str | None = None

    def pause_reason(self) -> str:
        what = f'an answer to "{self.question}"' if self.needs == "question" else NEEDS[self.needs or ""]
        return f'{what}, in the Chrome tab "{self.tab}"'


@dataclass
class Summary:
    resumes: int = 0
    applied: int = 0
    paused: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return f"{self.resumes} resumes written; {self.applied} applied, {self.paused} paused, {self.failed} failed"


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
        "--model", MODEL,
        "--allowedTools", ",".join([
            f"Read(/{folder}/**)", f"Read(/{_career(root)})", f"Edit(/{folder}/resume-content.json)",
            "Bash(uv run hunt resume:*)",
        ]),
    ]


def site_command(job: Job, root: Path, paused: Report | None = None) -> list[str]:
    """With the report of the job's pause, the worker carries on in the tab it paused in, where
    the user just did what it needed, instead of starting again in a new one."""
    folder = _folder(root, job.id)
    arguments = {"job": job.id, "link": job.link, "title": job.title, "company": job.company,
                 "resume": str(folder / "resume.pdf"), "career": str(_career(root)),
                 "report": str(folder / "report.json")}
    if paused is not None:
        arguments |= {"tab": paused.tab, "tab_id": paused.tab_id}
    return [
        "claude", "--chrome", "-p", f"/{SITE_SKILL} {json.dumps(arguments, ensure_ascii=False)}",
        "--model", MODEL,
        "--allowedTools", ",".join([
            f"Read(/{folder}/**)", f"Read(/{_career(root)})", f"Edit(/{folder}/report.json)", *CHROME_TOOLS,
        ]),
    ]


def _text(data: dict, name: str) -> str | None:
    value = data.get(name)
    return (" ".join(value.split()) or None) if isinstance(value, str) else None


def read_report(path: Path) -> Report:
    """The worker's report, or ApplyError naming what is wrong with it."""
    if not path.exists():
        raise ApplyError(f"the {SITE_SKILL} run wrote no report")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ApplyError(f"the {SITE_SKILL} run wrote a report that is not JSON: {error}") from error
    if not isinstance(data, dict) or data.get("result") not in ("applied", "paused", "failed"):
        raise ApplyError(f'the {SITE_SKILL} run wrote a report whose "result" is not applied, paused or failed')
    if data["result"] == "applied":
        return Report("applied")
    if data["result"] == "failed":
        if not (reason := _text(data, "reason")):
            raise ApplyError(f"the {SITE_SKILL} run reported a failure without a reason")
        return Report("failed", reason=reason)
    needs = data.get("needs")
    if not isinstance(needs, str) or needs not in NEEDS:
        raise ApplyError(f'the {SITE_SKILL} run paused, but "needs" is not one of {", ".join(NEEDS)}')
    if not (tab := _text(data, "tab")):
        raise ApplyError(f"the {SITE_SKILL} run paused without naming the tab")
    tab_id = data.get("tab_id")
    if tab_id is not None and type(tab_id) is not int:
        raise ApplyError(f'the {SITE_SKILL} run paused with a "tab_id" that is not a whole number')
    question = _text(data, "question")
    if needs == "question" and not question:
        raise ApplyError(f"the {SITE_SKILL} run paused on a question without the question")
    return Report("paused", needs=needs, question=question if needs == "question" else None, tab=tab, tab_id=tab_id)


def apply(jobs: JobsList, root: Path, *, run_resume: Run, run_site: Run) -> Summary:
    (root / "jobs").mkdir(exist_ok=True)
    # One run at a time: two would start two workers on one job.
    with (root / "jobs" / "apply.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ApplyError("another hunt apply is already running") from error
        summary = Summary()
        _left_applying(jobs, root, summary)
        _resumes(jobs, root, run_resume, summary)
        _sites(jobs, root, run_site, summary)
        return summary


def _save(jobs: JobsList, job: int, report: Report, summary: Summary) -> State:
    if report.result == "applied":
        jobs.move(job, "applied")
        summary.applied += 1
    elif report.result == "paused":
        jobs.move(job, "paused", report.pause_reason())
        summary.paused += 1
    elif jobs.fail(job, report.reason or "") == "failed":
        summary.failed += 1
    return jobs.get(job).state


def _left_applying(jobs: JobsList, root: Path, summary: Summary) -> None:
    """Jobs an earlier run stopped on while their worker ran: its report, when it wrote one, is
    saved; without one the application may have been sent, so the user looks."""
    for job in jobs.in_states("applying"):
        try:
            report = read_report(_folder(root, job.id) / "report.json")
        except ApplyError:
            jobs.move(job.id, "paused", "a look in Chrome whether it was sent: the last hunt apply stopped "
                                        "while applying")
            summary.paused += 1
            continue
        _save(jobs, job.id, report, summary)


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


def _paused(report: Path) -> Report | None:
    try:
        earlier = read_report(report)
    except ApplyError:
        return None
    return earlier if earlier.result == "paused" else None


def _apply_on_site(job: Job, root: Path, run: Run) -> Report:
    report = _folder(root, job.id) / "report.json"
    paused = _paused(report)
    # A report an earlier try left must not pass for this one's.
    report.unlink(missing_ok=True)
    try:
        run(site_command(job, root, paused))
    except ApplyError as error:
        # A worker that wrote its report and then stopped badly still did what it reports: an
        # application it sent must not be sent again.
        if report.exists():
            try:
                return read_report(report)
            except ApplyError:
                pass
        return Report("failed", reason=str(error))
    try:
        return read_report(report)
    except ApplyError as error:
        return Report("failed", reason=str(error))


def _sites(jobs: JobsList, root: Path, run: Run, summary: Summary) -> None:
    waiting = [job.id for job in jobs.in_states("resume_ready") if job.kind == "company_site"]
    running: dict[Future[Report], int] = {}
    # The jobs list is written here only, on this thread; the workers run the skills.
    with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
        while waiting or running:
            while waiting and len(running) < SITE_WORKERS:
                job = waiting.pop(0)
                jobs.move(job, "applying")
                running[pool.submit(_apply_on_site, jobs.get(job), root, run)] = job
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                job = running.pop(future)
                if _save(jobs, job, future.result(), summary) == "resume_ready":
                    waiting.append(job)


def _paused_job(jobs: JobsList, job: int) -> Job:
    try:
        found = jobs.get(job)
    except KeyError as error:
        raise AnswerError(f"there is no job {job}") from error
    if found.state != "paused":
        raise AnswerError(f"job {job} is not paused, it is {found.state}")
    return found


def resume_job(jobs: JobsList, job: int) -> Job:
    """Put a paused job back once the user did what it needed; the next run applies to it."""
    _paused_job(jobs, job)
    jobs.move(job, "resume_ready")
    return jobs.get(job)


def answer(jobs: JobsList, root: Path, job: int, text: str) -> Job:
    """Save the answer to a paused job's question in the career's form answers, so no form asks
    it again, and put the job back."""
    found = _paused_job(jobs, job)
    try:
        report = read_report(_folder(root, job) / "report.json")
    except ApplyError:
        report = None
    if report is None or report.needs != "question" or not report.question:
        raise AnswerError(f"job {job} waits for no answer: once it is done in Chrome, run hunt resume-job {job}")
    if asks_self_identification(report.question):
        raise AnswerError("answers about gender, race, ethnicity, veteran status or disability are never kept: "
                          "answer it in the Chrome tab, then run hunt resume-job " + str(job))
    if not (text := " ".join(text.split())):
        raise AnswerError("the answer is empty")
    career = read_career(_career(root))
    saved = [entry for entry in career.form_answers.get("saved") or []
             if entry.get("question", "").casefold() != report.question.casefold()]
    saved.append({"question": report.question, "answer": text, "company": found.company})
    career.form_answers = {**career.form_answers, "saved": saved}
    write_career(career, _career(root))
    jobs.move(job, "resume_ready")
    return jobs.get(job)
