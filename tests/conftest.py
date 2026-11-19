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
