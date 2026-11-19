"""Recruiter replies: messages from recruiters and companies, by email and on LinkedIn, each a
thread to answer, kept in `data/jobs.db` beside the jobs and written only by this module.

The messages come from two runs. The `read-job-alerts` run (of `hunt find` and of `hunt watch`'s
check) notes the recruiters' emails in its file:

    {"recruiter_emails": [{"email_id": "...", "thread_id": "...", "from": "Jane <jane@example.com>",
                           "subject": "...", "text": "...", "date": "<ISO time>",
                           "job_link": "..." | null, "company": "..." | null, "asks_to_talk": false}],
     "linkedin_notifications": 1}

and `hunt linkedin-messages` runs `read-linkedin-messages` in Chrome, under the LinkedIn lock:

    {"linkedin_messages": [{"conversation": "https://www.linkedin.com/messaging/thread/...",
                            "from": "...", "text": "...", "date": "...", "job_link": ..., "company": ...,
                            "asks_to_talk": false}]}

or, when it could not read them, `"unreachable"` with why, and the next read starts again from
the same time.

Each message is saved once (an email by its id, a LinkedIn message by its conversation and its
text); a newer message in a conversation replaces the one still open there, so only the latest
is answered. A thread is linked to its job by the posting link in the message, or else by its
company when the jobs list has one job of that company.

`hunt draft-replies` drafts a reply for each new thread (`draft-reply`, one `claude -p` each),
saves it in the thread's folder `jobs/replies/<id>/` and, for an email, as a Gmail draft. Nothing
is sent until `hunt send-reply <id>`: one `claude -p` with the `send-reply` skill sends exactly
the reply as the person approved it (an email through Gmail, a LinkedIn message in Chrome under
the lock) and reports. A send with no report may have gone out: the thread is `unsure` and is
never sent again on its own.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from job_hunt.apply import CHROME_TOOLS
from job_hunt.finder import PageError, page_text_and_kind
from job_hunt.jobs import JobsList, clean_link
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.questions import log_failure

DRAFT_SKILL = "draft-reply"
READ_SKILL = "read-linkedin-messages"
SEND_SKILL = "send-reply"
# Writing in the person's voice is the resume worker's kind of work; sending and reading are not.
DRAFT_MODEL = "claude-sonnet-5-5"
MODEL = "claude-haiku-5-5"
# A first read looks this far back: older messages were answered by hand, if at all.
FIRST_READ = timedelta(days=3)
TRIES = 2
UNSURE = "a look whether it was sent"
GMAIL_THREAD = "mcp__claude_ai_Gmail__get_thread"
DRAFT_GMAIL_TOOLS = (GMAIL_THREAD, "mcp__claude_ai_Gmail__create_draft")
SEND_GMAIL_TOOLS = (GMAIL_THREAD, "mcp__claude_ai_Gmail__reply", "mcp__claude_ai_Gmail__delete_draft")
# Reading LinkedIn types and clicks nothing: no computer (it types and clicks too), no form_input, no
# file_upload. A stranger's message, read with nobody watching, cannot make it send.
READ_CHROME_TOOLS = tuple(tool for tool in CHROME_TOOLS if not tool.endswith(("computer", "form_input", "file_upload")))

Source = Literal["email", "linkedin"]
State = Literal["new", "drafted", "sending", "sent", "skipped", "unsure", "failed", "replaced"]
OPEN: tuple[State, ...] = ("new", "drafted", "unsure")
Run = Callable[[list[str]], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('email', 'linkedin')),
    key TEXT NOT NULL,
    conversation TEXT NOT NULL,
    email_id TEXT,
    sender TEXT NOT NULL,
    subject TEXT,
    text TEXT NOT NULL,
    sent_at TEXT,
    job_link TEXT,
    company TEXT,
    job INTEGER REFERENCES jobs (id),
    asks_to_talk INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('new', 'drafted', 'sending', 'sent', 'skipped', 'unsure', 'failed',
        'replaced')),
    state_reason TEXT,
    reply TEXT,
    draft_id TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    asked_at TEXT,
    found_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, key)
);
CREATE TABLE IF NOT EXISTS message_reads (
    source TEXT NOT NULL CHECK (source IN ('email', 'linkedin')),
    started_at TEXT NOT NULL
);
"""


class ReplyError(RuntimeError):
    """A reply step that failed or cannot be done, in words for the person."""


@dataclass(frozen=True)
class Message:
    """One message as a skill read it."""

    source: Source
    key: str
    conversation: str
    email_id: str | None
    sender: str
    subject: str | None
    text: str
    sent_at: str | None
    job_link: str | None
    company: str | None
    asks_to_talk: bool


@dataclass(frozen=True)
class Thread:
    id: int
    source: Source
    key: str
    conversation: str
    email_id: str | None
    sender: str
    subject: str | None
    text: str
    sent_at: str | None
    job_link: str | None
    company: str | None
    job: int | None
    asks_to_talk: bool
    state: State
    state_reason: str | None
    reply: str | None
    draft_id: str | None
    failures: int
    asked_at: str | None
    found_at: str
    updated_at: str


def _now() -> datetime:
    return datetime.now(UTC)


def _text(entry: dict, name: str) -> str | None:
    value = entry.get(name)
    return value.strip() or None if isinstance(value, str) else None


def _web(link: str | None) -> bool:
    return link is not None and urlsplit(link).scheme in ("http", "https") and bool(urlsplit(link).hostname)


def _message(entry: Any, source: Source) -> Message:
    if not isinstance(entry, dict):
        raise ValueError("is not an object")
    needed = ("email_id", "thread_id", "from", "text") if source == "email" else ("conversation", "from", "text")
    for name in needed:
        if not _text(entry, name):
            raise ValueError(f"has no {name}")
    text = _text(entry, "text") or ""
    if source == "email":
        conversation, email_id = _text(entry, "thread_id") or "", _text(entry, "email_id")
        key = email_id or ""
    else:
        conversation, email_id = _text(entry, "conversation") or "", None
        if not _web(conversation):
            raise ValueError(f"has a conversation that is not a web address: {conversation!r}")
        # LinkedIn gives a message no id: the same text in the same conversation is the same message.
        key = f"{conversation}#{hashlib.sha256(' '.join(text.split()).encode()).hexdigest()[:16]}"
    job_link = _text(entry, "job_link")
    return Message(source=source, key=key, conversation=conversation, email_id=email_id,
                   sender=_text(entry, "from") or "", subject=_text(entry, "subject"), text=text,
                   sent_at=_text(entry, "date"), job_link=job_link if _web(job_link) else None,
                   company=_text(entry, "company"), asks_to_talk=entry.get("asks_to_talk") is True)


def read_messages(data: Any) -> tuple[list[Message], list[str]]:
    """The messages of a skill's file, and why each one left out was left out."""
    if not isinstance(data, dict):
        return [], ["the file does not hold one object"]
    found: list[Message] = []
    problems: list[str] = []
    for name, source in (("recruiter_emails", "email"), ("linkedin_messages", "linkedin")):
        entries = data.get(name) or []
        if not isinstance(entries, list):
            problems.append(f"{name} is not a list")
            continue
        for number, entry in enumerate(entries, start=1):
            try:
                found.append(_message(entry, source))
            except ValueError as error:
                sender = entry.get("from") if isinstance(entry, dict) else None
                problems.append(f"{source} message {number} ({sender or 'no sender'}) {error}")
    return found, problems


def _instant(text: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(text or "")
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _when(message: Message) -> datetime | None:
    return _instant(message.sent_at)


def chronological(found: list[Message]) -> list[Message]:
    """Oldest first, so the newest of a conversation is the one left open; as read when a date
    cannot be read."""
    times = [_when(each) for each in found]
    if any(when is None for when in times):
        return found
    return [each for _, each in sorted(zip(times, found), key=lambda pair: pair[0])]


@contextmanager
def _locked(path: Path, *, wait: bool = True) -> Iterator[bool]:
    """Hold an exclusive lock on the file; without `wait`, say whether it was free."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def job_of(jobs: JobsList, message: Message) -> int | None:
    """The job the message is about: the one of its posting link, else the one job of its company."""
    every = jobs.all()
    if message.job_link:
        try:
            link = clean_link(message.job_link)
        except ValueError:
            link = None
        for job in every:
            if link in (job.link, job.page_link):
                return job.id
    if message.company:
        same = [job.id for job in every if job.company.casefold() == message.company.casefold()]
        if len(same) == 1:
            return same[0]
    return None


OWED = "linkedin.owed"


def owe_linkedin(data: Path, now: datetime) -> None:
    """A notification email came: LinkedIn is read at the next watch, and until a read works."""
    (data / OWED).write_text(f"{now.isoformat()}\n")


def linkedin_owed(data: Path) -> str | None:
    """The notification owed, or None; `linkedin_paid` is given it back after the read."""
    try:
        return (data / OWED).read_text()
    except FileNotFoundError:
        return None


def linkedin_paid(data: Path, owed: str | None) -> None:
    """A read worked: what was owed before it is paid, a notification noted meanwhile is not."""
    if linkedin_owed(data) == owed:
        (data / OWED).unlink(missing_ok=True)


class Threads:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _now) -> None:
        self.data = path.parent
        self._clock = clock
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if new:
            path.chmod(0o600)
        self._db.executescript(SCHEMA)

    def _stamp(self) -> str:
        return self._clock().isoformat()

    def save(self, message: Message, jobs: JobsList) -> int | None:
        """Save a new message as a thread and return its id, or None when it is saved already."""
        job = job_of(jobs, message)
        now = self._stamp()
        with self._db:
            if self._db.execute("SELECT 1 FROM threads WHERE source = ? AND key = ?",
                                (message.source, message.key)).fetchone():
                return None
            marks = ", ".join("?" * len(OPEN))
            open_now = self._db.execute(
                f"SELECT sent_at FROM threads WHERE source = ? AND conversation = ? AND state IN ({marks})",
                (message.source, message.conversation, *OPEN)).fetchall()
            sent = _when(message)
            # An older message read late is kept, but the newer one left open stays the one answered.
            older = sent is not None and any(
                (then := _instant(row["sent_at"])) is not None and then > sent for row in open_now)
            if not older:
                self._db.execute(
                    f"UPDATE threads SET state = 'replaced', updated_at = ? WHERE source = ? AND conversation = ?"
                    f" AND state IN ({marks})", (now, message.source, message.conversation, *OPEN))
            cursor = self._db.execute(
                "INSERT INTO threads (source, key, conversation, email_id, sender, subject, text, sent_at, job_link,"
                " company, job, asks_to_talk, state, found_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message.source, message.key, message.conversation, message.email_id, message.sender,
                 message.subject, message.text, message.sent_at, message.job_link, message.company, job,
                 int(message.asks_to_talk), "replaced" if older else "new", now, now))
        return cursor.lastrowid

    def get(self, thread: int) -> Thread:
        row = self._db.execute("SELECT * FROM threads WHERE id = ?", (thread,)).fetchone()
        if row is None:
            raise KeyError(f"no thread {thread}")
        values = {field.name: row[field.name] for field in fields(Thread)}
        return Thread(**{**values, "asks_to_talk": bool(values["asks_to_talk"])})

    def in_states(self, *states: State) -> list[Thread]:
        marks = ", ".join("?" * len(states))
        rows = self._db.execute(f"SELECT id FROM threads WHERE state IN ({marks}) ORDER BY id", states)
        return [self.get(row["id"]) for row in rows]

    def _set(self, thread: int, **values: object) -> None:
        names = ", ".join(f"{name} = ?" for name in values)
        with self._db:
            self._db.execute(f"UPDATE threads SET {names}, updated_at = ? WHERE id = ?",
                             (*values.values(), self._stamp(), thread))

    def move(self, thread: int, state: State, reason: str | None = None) -> None:
        self._set(thread, state=state, state_reason=reason)

    def drafted(self, thread: int, reply: str, draft_id: str | None) -> bool:
        """Save the reply, unless the thread left `new` meanwhile (replaced, skipped)."""
        with self._db:
            done = self._db.execute(
                "UPDATE threads SET state = 'drafted', state_reason = NULL, reply = ?, draft_id = ?, updated_at = ?"
                " WHERE id = ? AND state = 'new'", (reply, draft_id, self._stamp(), thread)).rowcount
        return done == 1

    def edited(self, thread: int, reply: str) -> None:
        self._set(thread, reply=reply)

    def fail(self, thread: int, reason: str) -> State:
        """Count a failed draft: tried again at the next run, then `failed`."""
        failures = self.get(thread).failures + 1
        self._set(thread, failures=failures)
        if failures >= TRIES:
            self.move(thread, "failed", reason)
        return self.get(thread).state

    def unasked(self) -> list[Thread]:
        return [thread for thread in self.in_states("drafted") if thread.asked_at is None]

    def asked(self, thread: int) -> None:
        self._set(thread, asked_at=self._stamp())

    def last_read(self, source: Source) -> datetime | None:
        # Compared as instants: the finder's clock is UTC, the watch's the computer's own.
        times = [datetime.fromisoformat(row["started_at"]) for row in
                 self._db.execute("SELECT started_at FROM message_reads WHERE source = ?", (source,))]
        return max(times, default=None)

    def finished_read(self, source: Source, started: datetime) -> None:
        """Remember when a read that finished started: the next one reads from then."""
        with self._db:
            self._db.execute("INSERT INTO message_reads (source, started_at) VALUES (?, ?)",
                             (source, started.astimezone(UTC).isoformat()))


class EmailMessages:
    """The recruiters' emails of a `read-job-alerts` run, for `finder.find` and `hunt watch`."""

    def __init__(self, threads: Threads, jobs: JobsList) -> None:
        self.threads, self.jobs = threads, jobs
        self.new = 0
        self.notifications = 0

    def since(self, now: datetime) -> datetime:
        return self.threads.last_read("email") or now - FIRST_READ

    def save_all(self, found: list[Message]) -> int:
        """Save the messages, oldest first; return how many threads are new."""
        new = sum(self.threads.save(each, self.jobs) is not None for each in chronological(found))
        self.new += new
        return new

    def saved(self, data: Any, started: datetime) -> list[str]:
        """Save the emails and LinkedIn's notifications the run noted; return what was left out.
        A file with no list of emails read none: the next run reads from the same time."""
        if not isinstance(data, dict) or not isinstance(data.get("recruiter_emails"), list):
            return ["recruiter_emails: the run wrote no list of them; they are read again from the same time"]
        found, problems = read_messages(data)
        self.save_all(found)
        count = data.get("linkedin_notifications") if isinstance(data, dict) else None
        if type(count) is int and count > 0:
            self.notifications += count
            owe_linkedin(self.threads.data, started)
        self.threads.finished_read("email", started)
        return [f"recruiter {problem}" for problem in problems]


# The folders


def folder(root: Path, thread: int) -> Path:
    return root / "jobs" / "replies" / str(thread)


def _career(root: Path) -> Path:
    return root / "data" / "career.json"


def _rule(path: Path) -> str:
    # `//` starts an absolute path in a permission rule.
    return f"/{path}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    part.replace(path)


def _message_file(thread: Thread) -> str:
    head = [f"From: {thread.sender}"]
    if thread.subject:
        head.append(f"Subject: {thread.subject}")
    if thread.sent_at:
        head.append(f"Date: {thread.sent_at}")
    head.append("Channel: " + ("email" if thread.source == "email" else f"LinkedIn, {thread.conversation}"))
    return "\n".join(head) + f"\n\n{thread.text}\n"


def posting_path(thread: Thread, root: Path) -> Path | None:
    """The description of the thread's job: its own posting, else the one the message linked to."""
    if thread.job is not None and (root / "jobs" / str(thread.job) / "posting.md").exists():
        return root / "jobs" / str(thread.job) / "posting.md"
    linked = folder(root, thread.id) / "posting.md"
    return linked if linked.exists() else None


# Drafting


def draft_command(thread: Thread, root: Path) -> list[str]:
    here = folder(root, thread.id)
    posting = posting_path(thread, root)
    arguments = {"thread": thread.id, "source": thread.source, "message": str(here / "message.md"),
                 "posting": str(posting) if posting else None, "career": str(_career(root)),
                 "email_id": thread.email_id, "asks_to_talk": thread.asks_to_talk, "out": str(here / "reply.json")}
    tools = [f"Read({_rule(here)}/**)", f"Read({_rule(_career(root))})", f"Edit({_rule(here)}/reply.json)"]
    if thread.job is not None:
        tools.append(f"Read({_rule(root / 'jobs' / str(thread.job))}/**)")
    if thread.source == "email":
        tools += DRAFT_GMAIL_TOOLS
    return ["claude", "-p", f"/{DRAFT_SKILL} {json.dumps(arguments, ensure_ascii=False)}",
            "--model", DRAFT_MODEL, "--allowedTools", ",".join(tools)]


def _save_posting(thread: Thread, root: Path, fetch: Callable[[str], tuple[str, str]]) -> None:
    """The posting the message links to, when the job has none of its own."""
    if thread.job_link is None or posting_path(thread, root) is not None:
        return
    try:
        ended_at, html = fetch(thread.job_link)
        text, _ = page_text_and_kind(ended_at, html)
    except (PageError, OSError, ValueError):
        # The reply is drafted from the message alone.
        return
    _write(folder(root, thread.id) / "posting.md", f"{thread.job_link}\n\n{text}\n")


def _read_reply(path: Path) -> tuple[str, str | None]:
    if not path.exists():
        raise ReplyError(f"the {DRAFT_SKILL} run wrote no reply")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReplyError(f"the {DRAFT_SKILL} run wrote a reply that is not JSON: {error}") from error
    reply = data.get("reply") if isinstance(data, dict) else None
    if not isinstance(reply, str) or not reply.strip():
        raise ReplyError(f'the {DRAFT_SKILL} run wrote no "reply"')
    draft = data.get("draft_id")
    return reply.strip(), draft.strip() or None if isinstance(draft, str) else None


def draft_replies(threads: Threads, root: Path, *, run: Run,
                  fetch: Callable[[str], tuple[str, str]], clock: Callable[[], datetime] = _now) -> int:
    """Draft a reply for each new thread; return how many were drafted."""
    # One drafting run at a time, the morning's and the watch's: a thread is drafted once.
    with _locked(root / "data" / "draft.lock"):
        return _draft_replies(threads, root, run, fetch, clock)


def _draft_replies(threads: Threads, root: Path, run: Run, fetch: Callable[[str], tuple[str, str]],
                   clock: Callable[[], datetime]) -> int:
    drafted = 0
    for thread in threads.in_states("new"):
        here = folder(root, thread.id)
        _write(here / "message.md", _message_file(thread))
        _save_posting(thread, root, fetch)
        (here / "reply.json").unlink(missing_ok=True)
        try:
            run(draft_command(threads.get(thread.id), root))
            reply, draft = _read_reply(here / "reply.json")
        except ReplyError as error:
            if threads.fail(thread.id, str(error)) == "failed":
                log_failure(root / "data", "hunt draft-replies", f"no reply to {thread.sender}: {error}", clock)
            continue
        if threads.drafted(thread.id, reply, draft):
            _write(here / "reply.md", reply + "\n")
            drafted += 1
    return drafted


# Asking: what the person decides


def _waiting(threads: Threads, thread: int, *states: State) -> Thread:
    try:
        found = threads.get(thread)
    except KeyError as error:
        raise ReplyError(f"there is no thread {thread}") from error
    if found.state not in states:
        if found.state == "new":
            raise ReplyError(f"thread {thread} has no reply drafted yet: run hunt draft-replies")
        raise ReplyError(f"thread {thread} is {found.state}, not waiting for a reply")
    return found


def edit_reply(threads: Threads, root: Path, thread: int, text: str) -> Thread:
    _waiting(threads, thread, "drafted", "unsure")
    if not (text := text.strip()):
        raise ReplyError("the reply is empty")
    threads.edited(thread, text)
    _write(folder(root, thread) / "reply.md", text + "\n")
    return threads.get(thread)


def skip_reply(threads: Threads, thread: int) -> Thread:
    _waiting(threads, thread, "new", "drafted", "unsure")
    threads.move(thread, "skipped")
    return threads.get(thread)


# Sending


def send_command(thread: Thread, reply: str, root: Path) -> list[str]:
    here = folder(root, thread.id)
    arguments = {"thread": thread.id, "source": thread.source, "reply": reply, "email_id": thread.email_id,
                 "draft_id": thread.draft_id, "conversation": thread.conversation, "report": str(here / "sent.json")}
    prompt = f"/{SEND_SKILL} {json.dumps(arguments, ensure_ascii=False)}"
    write = f"Edit({_rule(here)}/sent.json)"
    if thread.source == "email":
        return ["claude", "-p", prompt, "--model", MODEL, "--allowedTools", ",".join([*SEND_GMAIL_TOOLS, write])]
    return ["claude", "--chrome", "-p", prompt, "--model", MODEL, "--allowedTools", ",".join([*CHROME_TOOLS, write])]


def _read_sent(path: Path) -> tuple[bool, str | None]:
    """Whether the report says sent, and the reason of a failure; ReplyError without a report."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReplyError(f"the {SEND_SKILL} run wrote no report it can be read by") from error
    result = data.get("result") if isinstance(data, dict) else None
    if result == "sent":
        return True, None
    if result == "failed" and (reason := _text(data, "reason")):
        return False, reason
    raise ReplyError(f'the {SEND_SKILL} run wrote a report whose "result" is not sent or failed')


def _with_lock(lock: LinkedInLock, sleep: Callable[[float], None], work: Callable[[], None]) -> None:
    """Do the work holding the LinkedIn lock: after the application holding it, never during."""
    while (later := lock.take("messages")) is not None:
        sleep(later)
    try:
        work()
    finally:
        lock.release()


def recover_sends(threads: Threads, data: Path) -> None:
    """A thread left `sending` while no send runs was stopped by a run that died: it may have gone."""
    with _locked(data / "send.lock", wait=False) as free:
        if free:
            for thread in threads.in_states("sending"):
                threads.move(thread.id, "unsure", f"{UNSURE}: hunt send-reply stopped while sending")


def send_reply(threads: Threads, root: Path, thread: int, *, run: Run, lock: LinkedInLock,
               sleep: Callable[[float], None]) -> Thread:
    """Send the reply as the person approved it, once."""
    # Held while sending, so a thread `sending` with the lock free is a dead run's.
    with _locked(root / "data" / "send.lock"):
        return _send_reply(threads, root, thread, run, lock, sleep)


def _send_reply(threads: Threads, root: Path, thread: int, run: Run, lock: LinkedInLock,
                sleep: Callable[[float], None]) -> Thread:
    found = _waiting(threads, thread, "drafted", "unsure")
    report = folder(root, thread) / "sent.json"
    report.unlink(missing_ok=True)
    threads.move(thread, "sending")
    failure: str | None = None

    def send() -> None:
        nonlocal failure
        try:
            run(send_command(found, found.reply or "", root))
        except ReplyError as error:
            failure = str(error)

    try:
        if found.source == "linkedin":
            _with_lock(lock, sleep, send)
        else:
            send()
        sent, reason = _read_sent(report)
    except ReplyError as error:
        threads.move(thread, "unsure", f"{UNSURE}: {failure or error}")
        return threads.get(thread)
    except BaseException:
        # Stopped in the middle (Ctrl-C, a killed run): it may have gone, and must not be lost.
        threads.move(thread, "unsure", f"{UNSURE}: hunt send-reply stopped while sending")
        raise
    if sent:
        threads.move(thread, "sent")
    else:
        threads.move(thread, "drafted", reason)
    return threads.get(thread)


# Reading LinkedIn


def read_command(after: datetime, out: Path) -> list[str]:
    arguments = {"after": after.isoformat(), "out": str(out)}
    return ["claude", "--chrome", "-p", f"/{READ_SKILL} {json.dumps(arguments)}", "--model", MODEL,
            "--allowedTools", ",".join([*READ_CHROME_TOOLS, f"Edit({_rule(out)})"])]


def read_linkedin(threads: Threads, jobs: JobsList, root: Path, *, run: Run, lock: LinkedInLock,
                  sleep: Callable[[float], None], clock: Callable[[], datetime] = _now) -> int:
    """Read the new LinkedIn messages under the lock; return how many threads are new."""
    out = root / "data" / "linkedin-messages.json"
    found: dict[str, Any] = {}

    def read() -> None:
        # The file is the lock holder's alone: another read waits until this one has read it.
        found["started"] = clock()
        found["after"] = threads.last_read("linkedin") or found["started"] - FIRST_READ
        out.unlink(missing_ok=True)
        run(read_command(found["after"], out))
        try:
            found["data"] = json.loads(out.read_text())
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ReplyError(f"the {READ_SKILL} run wrote no file it can be read by ({out})") from error

    _with_lock(lock, sleep, read)
    data, started = found["data"], found["started"]
    # Nothing read is not "no messages": the next read starts from the same time again.
    if isinstance(data, dict) and (why := _text(data, "unreachable")):
        raise ReplyError(f"the {READ_SKILL} run could not read LinkedIn: {why}")
    if not isinstance(data, dict) or not isinstance(data.get("linkedin_messages"), list):
        raise ReplyError(f"the {READ_SKILL} run wrote no list of linkedin_messages ({out})")
    messages, problems = read_messages(data)
    if problems:
        log_failure(root / "data", "hunt linkedin-messages", "left out " + "; ".join(problems), clock)
    new = EmailMessages(threads, jobs).save_all(messages)
    threads.finished_read("linkedin", started)
    return new
