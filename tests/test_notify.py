"""`hunt notify`: one Pushover push per question, when the settings have its keys.

Pushover is never called: the post is a stand-in that records what it was given.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.notify import PUSHOVER, NotifyError, notify

SETTINGS = {"titles": ["QA Automation Engineer"], "areas": ["New York City"], "remote": "include",
            "lowest_salary": None, "morning_hour": 7}
TOKEN, USER = "aExampleAppToken0000000000000", "uExampleUserKey00000000000000"


def _data(tmp_path: Path, **settings: object) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({**SETTINGS, **settings}))
    return data


class Post:
    def __init__(self, answer: dict[str, object] | Exception | None = None) -> None:
        self.answer = {"status": 1} if answer is None else answer
        self.sent: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        self.sent.append((url, fields))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_without_keys_nothing_is_sent_and_it_says_so_once(tmp_path: Path) -> None:
    data = _data(tmp_path)
    post = Post()

    first = notify("3 new jobs: apply?", data, post=post)
    second = notify("1 paused application", data, post=post)

    assert post.sent == []
    assert "no Pushover keys" in first
    assert second == ""


def test_with_keys_one_push_carries_the_text_and_the_session_link(tmp_path: Path) -> None:
    data = _data(tmp_path, pushover={"token": TOKEN, "user": USER})
    (data / "session.json").write_text('{"link": "https://claude.ai/code/session_01Example"}\n')
    post = Post()

    said = notify("3 new jobs: apply?", data, post=post)

    assert post.sent == [(PUSHOVER, {"token": TOKEN, "user": USER, "message": "3 new jobs: apply?",
                                     "title": "Job hunt", "url": "https://claude.ai/code/session_01Example",
                                     "url_title": "Open the job-hunt chat"})]
    assert said == "sent"
    assert TOKEN not in said and USER not in said


def test_without_a_session_link_the_push_has_none(tmp_path: Path) -> None:
    data = _data(tmp_path, pushover={"token": TOKEN, "user": USER})
    post = Post()

    notify("A question", data, post=post)

    assert "url" not in post.sent[0][1]


def test_a_refused_push_names_why_and_never_the_keys(tmp_path: Path) -> None:
    data = _data(tmp_path, pushover={"token": TOKEN, "user": USER})
    post = Post({"status": 0, "errors": [f"application token is invalid: {TOKEN}"]})

    with pytest.raises(NotifyError) as refused:
        notify("A question", data, post=post)

    assert "application token is invalid" in str(refused.value)
    assert TOKEN not in str(refused.value) and USER not in str(refused.value)


def test_a_failed_connection_never_shows_the_keys(tmp_path: Path) -> None:
    data = _data(tmp_path, pushover={"token": TOKEN, "user": USER})
    post = Post(OSError(f"connection refused for user={USER}"))

    with pytest.raises(NotifyError) as failed:
        notify("A question", data, post=post)

    assert USER not in str(failed.value)


def test_hunt_notify_prints_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    data = _data(tmp_path, pushover={"token": TOKEN, "user": USER})
    post = Post({"status": 0, "errors": [f"user key {USER} is invalid"]})
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "post_pushover", post)

    assert cli.main(["notify", "A question"]) == 1
    assert cli.main(["notify", "A question"]) == 1

    printed = capsys.readouterr()
    assert "is invalid" in printed.err
    assert TOKEN not in printed.out + printed.err and USER not in printed.out + printed.err
