"""A clock a test can move, so "eight days later" does not need eight days."""

from datetime import UTC, datetime, timedelta


class MovableClock:
    """Stands in for the real clock wherever the app asks what time it is."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, days: float = 0, hours: float = 0, seconds: float = 0) -> datetime:
        self.now += timedelta(days=days, hours=hours, seconds=seconds)
        return self.now
