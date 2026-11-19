"""A day's plan: how many applications start in a day, which jobs are picked by themselves, and
what the day came to.

The settings give the day's number (`daily_applications`), how many company-site applications
run at once (`site_workers`) and how many LinkedIn Easy Apply ones start a day
(`linkedin_daily`). `hunt apply` keeps to them (`apply`): no application starts past the day's
number, before the morning hour or past the summary hour, and the jobs left over wait for the
next morning hour, where they
go first (`order`). The starts are spread over what is left of the day, never more than
`MOST_GAP` apart, so a hundred do not all go in the first hour and three picks are not held for
hours (`gap`). A job resumed in the tab it paused in is the same application and is never held.

With `auto_apply_from`, `auto_pick` picks the scored jobs at or above that score, best first, up
to what is left of the day's number; the others above it wait for the next day, and those below
it are skipped. Every pick, by the person or by score, is left out when the same title at the same
company was applied to in the last `SAME_TITLE_DAYS`, whichever source found it (`applied_lately`).

`today` counts the day: applications sent by kind, the ones waiting for the person and for what,
the failures and why, and how long an application took on average.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from job_hunt.jobs import Job, JobsList
from job_hunt.settings import (
    DAILY_APPLICATIONS, LINKEDIN_DAILY, SITE_WORKERS, SUMMARY_HOUR, Settings,
)

# What a LinkedIn worker's start is kept under (`JobsList.started_on`).
LINKEDIN_HOST = "linkedin.com"
RESUME_WORKERS = 5
SAME_TITLE_DAYS = timedelta(days=30)
MOST_GAP = timedelta(minutes=15)
# What a paused job waits for, by the start of its reason (`apply.NEEDS`).
WAITS = (("a CAPTCHA", "a CAPTCHA solved"), ("a sign-in", "a sign-in or a new account"),
         ("an answer", "an answer to "), ("Chrome open", "Chrome open"))
KINDS = {"company_site": "on company sites", "easy_apply": "by LinkedIn Easy Apply"}


@dataclass(frozen=True)
class Plan:
    daily: int = DAILY_APPLICATIONS
    linkedin_daily: int = LINKEDIN_DAILY
    site_workers: int = SITE_WORKERS
    morning_hour: int = 7
    summary_hour: int = SUMMARY_HOUR
    # The lowest score picked by itself; None when the person picks.
    auto_from: int | None = None

    @classmethod
    def of(cls, settings: Settings) -> Plan:
        return cls(daily=settings.daily_applications, linkedin_daily=settings.linkedin_daily,
                   site_workers=settings.site_workers, morning_hour=settings.morning_hour,
                   summary_hour=settings.summary_hour, auto_from=settings.auto_apply_from)

    def day_start(self, now: datetime) -> datetime:
        return at_hour(now, self.morning_hour)

    def day_end(self, now: datetime) -> datetime:
        return at_hour(now, self.summary_hour)

    def next_morning(self, now: datetime) -> datetime:
        """This morning's hour while it has not come, else the next day's."""
        today = self.day_start(now)
        return today if now < today else at_hour(now, self.morning_hour, days=1)


def at_hour(now: datetime, hour: int, days: int = 0) -> datetime:
    """That hour of the computer's day of `now`, `days` on, at that hour's own offset."""
    return datetime.combine(now.astimezone().date() + timedelta(days=days), time(hour)).astimezone()


def midnight(now: datetime) -> datetime:
    return at_hour(now, 0)


def _same(text: str) -> str:
    return " ".join(text.split()).casefold()


def applied_lately(jobs: JobsList, job: Job, now: datetime, *, picks: bool = False,
                   going_on: Collection[int] = ()) -> Job | None:
    """Another job of the same title at the same company being applied to, waiting for the
    person, or applied to in the last `SAME_TITLE_DAYS`; with `picks`, also one picked and not
    started yet; and those of `going_on`, ready to go on in the tab they paused in. A failed one
    was maybe never sent, and does not count."""
    states = ("applying", "paused", "applied", *(("picked", "resume_ready") if picks or going_on else ()))
    for other in jobs.in_states(*states):
        if other.id == job.id or (_same(other.company), _same(other.title)) != (_same(job.company), _same(job.title)):
            continue
        if not picks and other.state in ("picked", "resume_ready") and other.id not in going_on:
            continue
        if other.state != "applied" or datetime.fromisoformat(other.updated_at) >= now - SAME_TITLE_DAYS:
            return other
    return None


def started_today(jobs: JobsList, now: datetime) -> set[int]:
    return set(jobs.started_since(midnight(now)))


def _waiting_today(jobs: JobsList, now: datetime) -> set[int]:
    """The picks not started yet that may start today: one held for tomorrow is tomorrow's."""
    tomorrow = midnight(now) + timedelta(days=1)
    return {job.id for job in jobs.in_states("picked", "resume_ready")
            if job.not_before is None or datetime.fromisoformat(job.not_before) < tomorrow}


def room(jobs: JobsList, plan: Plan, now: datetime) -> int:
    """How many more jobs may be picked for today: the day's number less the jobs started today
    and those picked that wait to start."""
    return max(0, plan.daily - len(started_today(jobs, now) | _waiting_today(jobs, now)))


@dataclass
class Picked:
    picked: list[Job] = field(default_factory=list)
    below: int = 0
    # Left out: the same title at the same company was applied to lately.
    same: int = 0
    # Above the score, for the next day's number.
    later: int = 0

    def __str__(self) -> str:
        said = [f"{len(self.picked)} jobs picked by their score"]
        said += [f"{self.below} below the score skipped"] if self.below else []
        said += [f"{self.same} left out, the same title at the same company applied to lately"] if self.same else []
        said += [f"{self.later} left for the next day"] if self.later else []
        return "; ".join(said)


def auto_pick(jobs: JobsList, plan: Plan) -> Picked:
    """Pick the scored jobs at or above the plan's score, best first, up to what is left of the
    day's number; skip those below it. Nothing when the person picks."""
    done = Picked()
    if plan.auto_from is None:
        return done
    now = jobs.now()
    left = room(jobs, plan, now)
    for job in jobs.waiting():
        if job.score is None or job.score < plan.auto_from:
            jobs.move(job.id, "skipped")
            done.below += 1
        elif applied_lately(jobs, job, now, picks=True) is not None:
            jobs.move(job.id, "skipped")
            done.same += 1
        elif left > 0:
            jobs.move(job.id, "picked")
            done.picked.append(jobs.get(job.id))
            left -= 1
        else:
            done.later += 1
    return done


def order(jobs: list[Job]) -> list[Job]:
    """The order to start them in: a job picked on an earlier day first, then best first."""
    def key(job: Job) -> tuple[str, int, int]:
        day = datetime.fromisoformat(job.picked_at).astimezone().date().isoformat() if job.picked_at else ""
        return day, -(job.score or 0), job.id
    return sorted(jobs, key=key)


def gap(plan: Plan, now: datetime, left: int) -> timedelta:
    """How far apart the next starts go: what is left of the day shared among the applications
    still to start, at most `MOST_GAP`."""
    if left <= 0:
        return timedelta(0)
    return max(timedelta(0), min(MOST_GAP, (plan.day_end(now) - now) / left))


@dataclass
class Today:
    sent: Counter[str] = field(default_factory=Counter)
    waiting: Counter[str] = field(default_factory=Counter)
    failed: list[Job] = field(default_factory=list)
    tomorrow: int = 0
    # The minutes each application sent today took, from its last start.
    minutes: list[float] = field(default_factory=list)

    def lines(self) -> list[str]:
        sent = sum(self.sent.values())
        kinds = " and ".join(f"{count} {KINDS.get(kind, 'by a way not known')}"
                             for kind, count in sorted(self.sent.items(), key=lambda item: -item[1]))
        said = f"Today: {sent} applications sent" + (f", {kinds}" if kinds else "")
        waits = sum(self.waiting.values())
        if waits:
            said += f"; {waits} wait for you (" + ", ".join(
                f"{count} for {what}" for what, count in self.waiting.items()) + ")"
        said += f"; {len(self.failed)} failed"
        said += f"; {self.tomorrow} left for tomorrow." if self.tomorrow else "."
        lines = [said]
        if self.minutes:
            average = sum(self.minutes) / len(self.minutes)
            lines.append("An application took " + (f"{average:.0f} minutes" if average >= 1 else "under a minute")
                         + " on average.")
        if self.failed:
            lines.append(f"Failed today ({len(self.failed)}):")
            lines += [f"  {job.id}. {job.title} at {job.company}: {job.state_reason}" for job in self.failed]
        return lines


def _waits_for(job: Job) -> str:
    reason = job.state_reason or ""
    return next((what for what, start in WAITS if reason.startswith(start)), "a look in Chrome")


def today(jobs: JobsList, now: datetime, *, auto_from: int | None = None) -> Today:
    """With `auto_from`, the scored jobs at or above it count as left for tomorrow too: they wait
    for the next day's number."""
    day = midnight(now)
    tomorrow = day + timedelta(days=1)
    counted = Today()
    starts = jobs.started_since(day - SAME_TITLE_DAYS)
    for job in jobs.in_states("applied"):
        if (when := datetime.fromisoformat(job.updated_at)) >= day:
            counted.sent[job.kind or ""] += 1
            if job.id in starts and starts[job.id] <= when:
                counted.minutes.append((when - starts[job.id]).total_seconds() / 60)
    for job in jobs.in_states("paused"):
        counted.waiting[_waits_for(job)] += 1
    counted.failed = [job for job in jobs.in_states("failed") if datetime.fromisoformat(job.updated_at) >= day]
    counted.tomorrow = sum(1 for job in jobs.in_states("resume_ready")
                           if job.not_before is not None and datetime.fromisoformat(job.not_before) >= tomorrow)
    if auto_from is not None:
        counted.tomorrow += sum(1 for job in jobs.waiting() if job.score is not None and job.score >= auto_from)
    return counted
