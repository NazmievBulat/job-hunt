"""`hunt notify "<text>"`: one Pushover push of a question of the chat.

The keys are the settings' `pushover.token` and `pushover.user`; they are never printed, and
an error that repeats them has them blotted out. The push links to the day's session when
`hunt morning` kept its Remote Control link (`data/session.json`). Without keys nothing is
sent: the Claude app's own notification for a waiting session does the job. That is said once
(`data/notify.told`), not with every question.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from job_hunt.settings import read_settings

PUSHOVER = "https://api.pushover.net/1/messages.json"
NO_KEYS = ("no Pushover keys in data/settings.json, so no push was sent; the Claude app's own "
           "notification tells of each question. /setup adds the keys.")

Post = Callable[[str, dict[str, str]], Any]


class NotifyError(RuntimeError):
    """The push was not sent, in words without the keys."""


def post_pushover(url: str, fields: dict[str, str]) -> Any:
    body = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20) as answer:
            return json.load(answer)
    except urllib.error.HTTPError as refused:
        # Pushover says why in the body of a refusal (400 for a wrong key).
        return json.load(refused)


def _link(data: Path) -> str | None:
    try:
        link = json.loads((data / "session.json").read_text()).get("link")
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return None
    return link if isinstance(link, str) and link else None


def notify(text: str, data: Path, *, post: Post = post_pushover) -> str:
    """Send the push; return what to print ("" when there is nothing to say)."""
    keys = read_settings(data / "settings.json").pushover
    if keys is None:
        told = data / "notify.told"
        if told.exists():
            return ""
        told.write_text("told\n")
        return NO_KEYS
    secrets = (keys.token, keys.user)

    def blotted(said: object) -> str:
        said = str(said)
        for secret in secrets:
            said = said.replace(secret, "<key>")
        return said

    fields = {"token": keys.token, "user": keys.user, "message": text, "title": "Job hunt"}
    link = _link(data)
    if link:
        fields |= {"url": link, "url_title": "Open the job-hunt chat"}
    try:
        answer = post(PUSHOVER, fields)
    except Exception as error:
        raise NotifyError(f"the push was not sent: {blotted(f'{type(error).__name__}: {error}')}") from None
    if not isinstance(answer, dict) or answer.get("status") != 1:
        errors = answer.get("errors") if isinstance(answer, dict) else None
        why = "; ".join(map(str, errors)) if isinstance(errors, list) and errors else "Pushover did not say why"
        raise NotifyError(f"Pushover refused the push: {blotted(why)}")
    # A new user without keys is told again if the keys are removed later.
    (data / "notify.told").unlink(missing_ok=True)
    return "sent"
