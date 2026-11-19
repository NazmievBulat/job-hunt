"""Stands in for the `read-job-alerts` runs of `hunt find`: the search run lists the alert emails
and notes the messages; each batch run writes the jobs of the emails it is named.

`written` is what one run read over the whole mailbox, as `tests/finder/found-jobs.json` holds
it: the alert emails are its jobs' emails, and a batch writes the jobs of its emails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from job_hunt.finder import SKILL


def arguments(command: list[str]) -> dict[str, Any]:
    return json.loads(command[2].removeprefix(f"/{SKILL} "))


def search_answer(written: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    alerts: dict[str, str] = {}
    if args.get("after") is not None:
        for job in written.get("jobs", []):
            alerts.setdefault(job["email_id"], job["email_date"])
    answer: dict[str, Any] = {"alerts": [{"email_id": id, "email_date": date} for id, date in alerts.items()]}
    if "messages_after" in args:
        for name in ("recruiter_emails", "linkedin_notifications"):
            if name in written:
                answer[name] = written[name]
    return answer


def batch_answer(written: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    ids = args["email_ids"]
    return {"emails": len(ids), "jobs": [job for job in written.get("jobs", []) if job["email_id"] in ids]}


class AlertSkill:
    """Writes what the skill wrote for the made-up emails; `fail` names the runs (1 the search,
    2 the first batch, ...) that write nothing."""

    def __init__(self, written: dict[str, Any] | Path, fail: set[int] | None = None) -> None:
        self.written = written if isinstance(written, dict) else json.loads(written.read_text())
        self.fail = fail or set()
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> None:
        self.commands.append(command)
        if len(self.commands) in self.fail:
            return
        args = arguments(command)
        answer = batch_answer(self.written, args) if "email_ids" in args else search_answer(self.written, args)
        # Not `Path.write_text`, which a test may make fail for the finder alone.
        with open(args["out"], "w") as out:
            json.dump(answer, out)

    @property
    def batches(self) -> list[list[str]]:
        return [arguments(command)["email_ids"] for command in self.commands if "email_ids" in arguments(command)]
