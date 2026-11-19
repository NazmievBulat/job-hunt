"""The LinkedIn lock, `data/linkedin.lock`: one LinkedIn worker at a time, a few minutes apart.

The file is there while a LinkedIn worker applies, and says for which job and since when; no
other LinkedIn worker starts while it is, from this `hunt apply` or another. One left by a run
that died is taken back once it is older than `STALE`. Each release writes `data/linkedin.next`,
the time before which no LinkedIn worker starts: three to six minutes later, chosen at random,
so two applications are never sent back to back, also by two runs one after the other.
"""

from __future__ import annotations

import fcntl
import json
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Longer than a worker may run (`SKILL_MINUTES`), so only a dead run's lock is ever this old.
STALE = timedelta(minutes=30)
PAUSE = (timedelta(minutes=3), timedelta(minutes=6))
# How often a lock held by another is looked at again.
POLL_SECONDS = 30.0


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(text: str) -> datetime:
    """A time this module wrote; one without its time zone was not, and is a ValueError."""
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError(f"no time zone in {text!r}")
    return when


def random_pause() -> timedelta:
    low, high = PAUSE
    return timedelta(seconds=random.uniform(low.total_seconds(), high.total_seconds()))


class LinkedInLock:
    def __init__(
        self,
        data: Path,
        *,
        clock: Callable[[], datetime] = _now,
        pause: Callable[[], timedelta] = random_pause,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.path = data / "linkedin.lock"
        self._next = data / "linkedin.next"
        self._guard = data / "linkedin.guard"
        self._clock, self._pause, self._poll = clock, pause, poll_seconds
        # What this holder wrote in the lock, so a release never removes another's.
        self._held: str | None = None

    @contextmanager
    def _guarded(self) -> Iterator[None]:
        # Looking at the lock and taking it is one step: two runs that both find it stale must
        # not both take it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._guard.open("w") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            yield

    def _taken_at(self, now: datetime) -> datetime | None:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        try:
            taken_at = _aware(json.loads(text)["taken_at"])
        # Cut off while written, or not written here: as old as the file.
        except (ValueError, KeyError, TypeError):
            taken_at = None
        # Later than now, taken while the clock was ahead: as old as the file too.
        if taken_at is None or taken_at > now:
            taken_at = min(datetime.fromtimestamp(self.path.stat().st_mtime, UTC), now)
        return taken_at

    def _not_before(self, now: datetime) -> datetime | None:
        try:
            not_before = _aware(self._next.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        # Never longer than the longest pause from now, whatever the clock said when written; the
        # shorter time is written back, so the next look does not start the six minutes again.
        if not_before > now + PAUSE[1]:
            not_before = now + PAUSE[1]
            self._next.write_text(not_before.isoformat())
        return not_before

    def take(self, job: int) -> float | None:
        """Hold the lock for this job's application and return None, or return how many
        seconds to wait before trying again: another holds it, or the pause after the last
        application is not over."""
        with self._guarded():
            now = self._clock()
            taken_at = self._taken_at(now)
            if taken_at is not None and now - taken_at < STALE:
                return min(self._poll, (taken_at + STALE - now).total_seconds())
            not_before = self._not_before(now)
            if not_before is not None and now < not_before:
                return (not_before - now).total_seconds()
            held = json.dumps({"job": job, "taken_at": now.isoformat()})
            part = self.path.with_name(self.path.name + ".part")
            part.write_text(held)
            part.replace(self.path)
            self._held = held
            return None

    def release(self) -> None:
        """Let the next LinkedIn worker start, after the pause; nothing when this holds no lock."""
        if self._held is None:
            return
        with self._guarded():
            self._next.write_text((self._clock() + self._pause()).isoformat())
            try:
                mine = self.path.read_text() == self._held
            except FileNotFoundError:
                mine = False
            # Held past STALE and taken back: the lock is the other holder's now.
            if mine:
                self.path.unlink()
            self._held = None
