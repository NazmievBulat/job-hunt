import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _no_site_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`hunt find` searches no site and reads no feed in a test that does not ask for it: no test calls
    the network."""
    from job_hunt import cli

    monkeypatch.setattr(cli, "site_search", lambda: None)
    monkeypatch.setattr(cli, "public_feeds", lambda: None)


class Told:
    """Stands in for typing into the job-hunt session: keeps what would have been typed."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, root: object, *, text: str | None = None, **ignored: object) -> None:
        from job_hunt.watch import TELL

        self.texts.append(text or TELL)


@pytest.fixture(autouse=True)
def told(monkeypatch: pytest.MonkeyPatch) -> Told:
    """No test reads this computer's idle time or types into a real tmux session: the person is
    not free, and what would be told is kept."""
    from job_hunt import cli
    from job_hunt.available import Verdict

    monkeypatch.setattr(cli, "verdict", lambda: Verdict(False, "not free in a test"))
    stand_in = Told()
    monkeypatch.setattr(cli, "tell_session", stand_in)
    return stand_in
