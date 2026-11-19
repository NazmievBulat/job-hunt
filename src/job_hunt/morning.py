"""`hunt morning`: the day's start, run by the timer.

It runs `hunt find`, then `hunt score` when any job is new (one a failed run left new too),
and drafts a reply to each recruiter's message the find noted (`hunt draft-replies`).
When anything waits for the person (`questions.anything_waiting`) it ends yesterday's `job-hunt`
session and starts a fresh interactive Claude Code session of that name with Remote Control,
in a `tmux` session of the same name; its first message asks it to ask today's questions. With
nothing waiting it starts nothing. A failed step is written to `data/morning.log` and goes on;
the next `hunt questions` tells it once, so a failure alone also starts the session.

Yesterday's session is ended only while no `hunt apply` runs (`jobs/apply.lock`): ending it
then would stop a worker in the middle of an application.
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from job_hunt.finder import FinderError
from job_hunt.jobs import JobsList
from job_hunt.questions import anything_waiting, log_failure
from job_hunt.scoring import ScoreError
from job_hunt.settings import SettingsError

if TYPE_CHECKING:
    from job_hunt.replies import Threads

__all__ = ["FIRST_MESSAGE", "SESSION", "log_failure", "morning", "start_session"]

SESSION = "job-hunt"
# tmux takes a target that is not exact as the start of a name: `job-hunt` alone would end a
# `job-hunt-notes` session when no `job-hunt` runs.
TARGET = f"={SESSION}"
FIRST_MESSAGE = (
    "Good morning. Run `uv run hunt questions` and ask what it prints, one question at a time, "
    "as CLAUDE.md says."
)
# The session runs `hunt` commands without a permission prompt; anything else asks.
TOOLS = "Bash(uv run hunt:*)"
LINK = re.compile(r"https://claude\.ai/code/[\w./?=&%-]+")
LINK_SECONDS = 30
# The failures of a step that say what to do in their own words; any other is named by its kind.
KNOWN = (FinderError, ScoreError, SettingsError)

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _now() -> datetime:
    return datetime.now(UTC)


def _said(error: Exception) -> str:
    return str(error) if isinstance(error, KNOWN) else f"{type(error).__name__}: {error}"


def morning(
    jobs: JobsList,
    data: Path,
    *,
    find: Callable[[], object],
    score: Callable[[], object],
    start: Callable[[], object],
    clock: Callable[[], datetime] = _now,
    threads: Threads | None = None,
    draft: Callable[[], object] | None = None,
) -> str:
    try:
        find()
    except Exception as error:
        log_failure(data, "hunt find", _said(error), clock)
    if jobs.in_states("new"):
        try:
            score()
        except Exception as error:
            log_failure(data, "hunt score", _said(error), clock)
    if draft is not None and threads is not None and threads.in_states("new"):
        try:
            draft()
        except Exception as error:
            log_failure(data, "hunt draft-replies", _said(error), clock)
    if not anything_waiting(jobs, data, threads):
        return "Nothing new and nothing waiting: no session started."
    start()
    return f"Started the {SESSION} session: tmux attach -t {SESSION}, or the Claude app's Remote Control."


def tmux(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as missing:
        raise RuntimeError("tmux is not installed or not on the PATH") from missing
    except subprocess.TimeoutExpired as hung:
        raise RuntimeError(f"tmux did not answer in 60 seconds: {' '.join(command[:2])}") from hung


def session_command(root: Path) -> list[str]:
    # The prompt comes last, after an option that takes one value: --allowedTools takes many.
    return ["tmux", "new-session", "-d", "-s", SESSION, "-c", str(root),
            "claude", "--allowedTools", TOOLS, "--remote-control", SESSION, "--name", SESSION, FIRST_MESSAGE]


def start_session(root: Path, *, run: Run = tmux, sleep: Callable[[float], None] = time.sleep) -> None:
    """End yesterday's session and start today's, then keep its Remote Control link in
    `data/session.json` for the pushes, when the session shows one within `LINK_SECONDS`."""
    data, jobs = root / "data", root / "jobs"
    data.mkdir(exist_ok=True)
    jobs.mkdir(exist_ok=True)
    with (jobs / "apply.lock").open("w") as lock:
        # Waits for a running hunt apply; its workers have their own time limits.
        fcntl.flock(lock, fcntl.LOCK_EX)
        run(["tmux", "kill-session", "-t", TARGET])
        (data / "session.json").unlink(missing_ok=True)
        started = run(session_command(root))
    if started.returncode != 0:
        said = (started.stderr.strip() or started.stdout.strip() or f"exit {started.returncode}").splitlines()[-1]
        raise RuntimeError(f"tmux could not start the {SESSION} session: {said}")
    for _ in range(LINK_SECONDS):
        sleep(1)
        pane = run(["tmux", "capture-pane", "-p", "-J", "-t", f"{TARGET}:"])
        if pane.returncode != 0:
            # Its only window closed: claude stopped, or was not found on the PATH.
            raise RuntimeError(f"the {SESSION} session ended as it started: is claude on the timer's PATH?")
        found = LINK.search(pane.stdout or "")
        if found:
            (data / "session.json").write_text(json.dumps({"link": found.group()}) + "\n")
            return
