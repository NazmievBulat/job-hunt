"""Notes to hiring managers: after a LinkedIn application, a short note to the person who posted
the job, kept in `data/jobs.db` beside the jobs and written only by this module.

The LinkedIn worker (`apply-linkedin`) saves who posted the job in its `applied` report, with
what LinkedIn offers to reach them:

    {"result": "applied", "poster": {"name": "...", "title": "...",
                                     "profile": "https://www.linkedin.com/in/...",
                                     "reach": "message" | "inmail" | "connect"}}

`collect` saves each poster once per job. Only a free `Message` button is used: a poster
reachable only by an InMail credit or by `Connect` is `unreachable`, never drafted, asked about
or sent to, and no step ever makes a connection request. A person written to once is never
written to again (`skipped`).

`hunt draft-notes` drafts a note for each new poster (`draft-note`, one `claude -p` each) of at
most `LIMIT` characters. The chat asks once for all the drafted notes (`ask`); its reply is
`hunt send-notes all`, `hunt send-notes 1 3` (the others asked about are skipped) or
`hunt skip-notes`. `hunt send-notes` sends the approved ones (`send-note`, one
`claude --chrome -p` each), each under the LinkedIn lock, released between notes for a pause of
one to three minutes, and at most `linkedin_notes_daily` a day. A note whose send wrote no report
may have gone: it is `unsure` and never sent again. A LinkedIn warning or refusal ends sending
for the day (`data/notes.stopped`), told once. A sent note is a thread of `replies`, so the
answer in its conversation is drafted and asked as any recruiter's message.
"""

from __future__ import annotations

import fcntl
import json
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from job_hunt.apply import CHROME_TOOLS
from job_hunt.finder import ONLY_NAMED_TOOLS
from job_hunt.jobs import Job, JobsList
from job_hunt.linkedin_lock import LinkedInLock
from job_hunt.questions import log_failure
from job_hunt.replies import Threads

DRAFT_SKILL = "draft-note"
SEND_SKILL = "send-note"
# Writing in the person's voice is the resume worker's kind of work; sending is not.
DRAFT_MODEL = "claude-sonnet-5-5"
MODEL = "claude-haiku-5-5"
LIMIT = 300
DAILY = 15
TRIES = 2
PAUSE = (timedelta(minutes=1), timedelta(minutes=3))
# A note is text: the sender, reading LinkedIn's pages with nobody watching, uploads no file.
SEND_TOOLS = tuple(tool for tool in CHROME_TOOLS if not tool.endswith("file_upload"))
STOPPED = "notes.stopped"
UNSURE = "a look on LinkedIn whether it was sent"

Reach = Literal["message", "inmail", "connect"]
REACH: tuple[Reach, ...] = ("message", "inmail", "connect")
State = Literal["new", "drafted", "approved", "sending", "sent", "skipped", "unreachable", "unsure", "failed"]
# A person with a note in one of these is written to, or will be once the person says so.
WRITTEN: tuple[State, ...] = ("new", "drafted", "approved", "sending", "sent", "unsure")
Run = Callable[[list[str]], None]

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    job INTEGER NOT NULL UNIQUE REFERENCES jobs (id),
    name TEXT NOT NULL,
    title TEXT,
    profile TEXT NOT NULL,
    reach TEXT NOT NULL CHECK (reach IN ('message', 'inmail', 'connect')),
    state TEXT NOT NULL CHECK (state IN ('new', 'drafted', 'approved', 'sending', 'sent', 'skipped',
        'unreachable', 'unsure', 'failed')),
    state_reason TEXT,
    note TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    asked_at TEXT,
    sent_at TEXT,
    conversation TEXT,
    found_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class NoteError(RuntimeError):
    """A note step that failed or cannot be done, in words for the person."""


@dataclass(frozen=True)
class Note:
    id: int
    job: int
    name: str
    title: str | None
    profile: str
    reach: Reach
    state: State
    state_reason: str | None
    note: str | None
    failures: int
    asked_at: str | None
    sent_at: str | None
    conversation: str | None
    found_at: str
    updated_at: str


def _local_now() -> datetime:
    return datetime.now().astimezone()


def note_pause() -> timedelta:
    """The pause between two notes, for the LinkedIn lock that sends them."""
    low, high = PAUSE
    return timedelta(seconds=random.uniform(low.total_seconds(), high.total_seconds()))


def _text(entry: dict, name: str) -> str | None:
    value = entry.get(name)
    return (" ".join(value.split()) or None) if isinstance(value, str) else None


def profile_link(link: str) -> str | None:
    """A LinkedIn member's page as one address, or None when it is not one."""
    try:
        parts = urlsplit(link.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    if not path.startswith("/in/") or path == "/in":
        return None
    return f"https://www.linkedin.com{path}"


def read_poster(report: Path) -> dict[str, Any] | None:
    """The poster an `applied` report names, or None when it names none it can be read by."""
    try:
        data = json.loads(report.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    poster = data.get("poster") if isinstance(data, dict) and data.get("result") == "applied" else None
    if not isinstance(poster, dict):
        return None
    name, profile, reach = _text(poster, "name"), profile_link(str(poster.get("profile") or "")), poster.get("reach")
    if not name or not profile or reach not in REACH:
        return None
    return {"name": name, "title": _text(poster, "title"), "profile": profile, "reach": reach}


class Notes:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _local_now) -> None:
        self.data = path.parent
        self._clock = clock
        new = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if new:
            path.chmod(0o600)
        self._db.executescript(SCHEMA)

    def now(self) -> datetime:
        return self._clock()

    def _stamp(self) -> str:
        return self._clock().isoformat()

    def save(self, job: int, poster: dict[str, Any]) -> int | None:
        """Save the job's poster and return the note's id, or None when the job has one."""
        now = self._stamp()
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM notes WHERE job = ?", (job,)).fetchone():
                return None
            marks = ", ".join("?" * len(WRITTEN))
            written = self._db.execute(f"SELECT 1 FROM notes WHERE profile = ? AND state IN ({marks})",
                                       (poster["profile"], *WRITTEN)).fetchone()
            if poster["reach"] != "message":
                state, reason = "unreachable", f"LinkedIn offers only {poster['reach']}, never used"
            elif written:
                state, reason = "skipped", "written to already"
            else:
                state, reason = "new", None
            cursor = self._db.execute(
                "INSERT INTO notes (job, name, title, profile, reach, state, state_reason, found_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job, poster["name"], poster["title"], poster["profile"], poster["reach"], state, reason, now, now))
        return cursor.lastrowid

    def get(self, note: int) -> Note:
        row = self._db.execute("SELECT * FROM notes WHERE id = ?", (note,)).fetchone()
        if row is None:
            raise KeyError(f"no note {note}")
        return Note(**{field.name: row[field.name] for field in fields(Note)})

    def all(self) -> list[Note]:
        return [self.get(row["id"]) for row in self._db.execute("SELECT id FROM notes ORDER BY id")]

    def in_states(self, *states: State) -> list[Note]:
        marks = ", ".join("?" * len(states))
        rows = self._db.execute(f"SELECT id FROM notes WHERE state IN ({marks}) ORDER BY id", states)
        return [self.get(row["id"]) for row in rows]

    def jobs_with_notes(self) -> set[int]:
        return {row["job"] for row in self._db.execute("SELECT job FROM notes")}

    def _set(self, number: int, /, **values: object) -> None:
        names = ", ".join(f"{name} = ?" for name in values)
        with self._db:
            self._db.execute(f"UPDATE notes SET {names}, updated_at = ? WHERE id = ?",
                             (*values.values(), self._stamp(), number))

    def move(self, note: int, state: State, reason: str | None = None) -> None:
        self._set(note, state=state, state_reason=reason)

    def drafted(self, note: int, text: str) -> bool:
        """Save the note, unless it left `new` meanwhile."""
        with self._db:
            done = self._db.execute("UPDATE notes SET state = 'drafted', state_reason = NULL, note = ?, updated_at = ?"
                                    " WHERE id = ? AND state = 'new'", (text, self._stamp(), note)).rowcount
        return done == 1

    def fail(self, note: int, reason: str, back_to: State) -> State:
        """Count a failure: back to `back_to` for the next run, then `failed`."""
        failures = self.get(note).failures + 1
        self._set(note, failures=failures)
        self.move(note, "failed" if failures >= TRIES else back_to, reason)
        return self.get(note).state

    def asked(self, note: int) -> None:
        self._set(note, asked_at=self._stamp())

    def sent(self, note: int, conversation: str | None) -> None:
        self._set(note, state="sent", state_reason=None, sent_at=self._stamp(), conversation=conversation)

    def written_to(self, profile: str, but: int) -> bool:
        return self._db.execute("SELECT 1 FROM notes WHERE profile = ? AND id != ? AND state IN ('sent', 'sending',"
                                " 'unsure')", (profile, but)).fetchone() is not None

    def gone_today(self, now: datetime) -> int:
        """How many went today, or may have: sent, being sent, or unsure since."""
        count = 0
        for note in self.in_states("sent", "sending", "unsure"):
            when = datetime.fromisoformat(note.sent_at or note.updated_at)
            if when.astimezone(now.tzinfo).date() == now.date():
                count += 1
        return count


# Collecting the posters


def collect(notes: Notes, jobs: JobsList, root: Path) -> int:
    """Save the poster of each applied job whose report names one; return how many are new."""
    known = notes.jobs_with_notes()
    new = 0
    for job in jobs.in_states("applied"):
        if job.id in known:
            continue
        poster = read_poster(root / "jobs" / str(job.id) / "report.json")
        if poster is not None and notes.save(job.id, poster) is not None:
            new += 1
    return new


# Drafting


def _rule(path: Path) -> str:
    # `//` starts an absolute path in a permission rule.
    return f"/{path}"


def _folder(root: Path, job: int) -> Path:
    return root / "jobs" / str(job)


def draft_command(note: Note, job: Job, root: Path) -> list[str]:
    folder = _folder(root, job.id)
    posting = folder / "posting.md"
    career = root / "data" / "career.json"
    arguments = {"note": note.id, "job": job.id, "title": job.title, "company": job.company,
                 "poster": {"name": note.name, "title": note.title},
                 "posting": str(posting) if posting.exists() else None, "career": str(career),
                 "limit": LIMIT, "out": str(folder / "note.json")}
    tools = [f"Read({_rule(folder)}/**)", f"Read({_rule(career)})", f"Edit({_rule(folder)}/note.json)"]
    return ["claude", "-p", f"/{DRAFT_SKILL} {json.dumps(arguments, ensure_ascii=False)}",
            "--model", DRAFT_MODEL, *ONLY_NAMED_TOOLS, "--allowedTools", ",".join(tools)]


def _read_note(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise NoteError(f"the {DRAFT_SKILL} run wrote no note") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise NoteError(f"the {DRAFT_SKILL} run wrote a note that is not JSON: {error}") from error
    text = data.get("note") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise NoteError(f'the {DRAFT_SKILL} run wrote no "note"')
    text = text.strip()
    if len(text) > LIMIT:
        raise NoteError(f"the {DRAFT_SKILL} run wrote a note of {len(text)} characters, more than {LIMIT} characters")
    return text


def draft_notes(notes: Notes, jobs: JobsList, root: Path, *, run: Run) -> int:
    """Draft a note for each new poster; return how many were drafted."""
    notes.data.mkdir(parents=True, exist_ok=True)
    # One drafting run at a time, the end of a hunt apply's and the morning's: a note is drafted once.
    with (notes.data / "notes-draft.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _draft_notes(notes, jobs, root, run)


def _draft_notes(notes: Notes, jobs: JobsList, root: Path, run: Run) -> int:
    drafted = 0
    for note in notes.in_states("new"):
        out = _folder(root, note.job) / "note.json"
        out.unlink(missing_ok=True)
        try:
            run(draft_command(note, jobs.get(note.job), root))
            text = _read_note(out)
        except NoteError as error:
            if notes.fail(note.id, str(error), "new") == "failed":
                log_failure(notes.data, "hunt draft-notes", f"no note to {note.name}: {error}", notes.now)
            continue
        if notes.drafted(note.id, text):
            drafted += 1
    return drafted


# Asking


def _line(note: Note, jobs: JobsList) -> str:
    job = jobs.get(note.job)
    who = ", ".join(part for part in (note.name, note.title, job.company) if part)
    return f'  {note.id}. {who} (job {job.id}: {job.title}): "{note.note}"'


def ask(notes: Notes, jobs: JobsList) -> str | None:
    """The one question for every drafted note; showing it marks them asked. A note that may
    have gone is asked about on its own."""
    parts = []
    waiting = notes.in_states("drafted")
    if waiting:
        lines = [f"Notes to hiring managers ({len(waiting)}), for the LinkedIn applications:"]
        for note in waiting:
            lines.append(_line(note, jobs))
            notes.asked(note.id)
        lines.append("  (then: hunt send-notes all, hunt send-notes <numbers>, hunt skip-notes)")
        parts.append("\n".join(lines))
    for note in notes.in_states("unsure"):
        parts.append("\n".join([f"A note to a hiring manager needs {note.state_reason}:", _line(note, jobs),
                                f"  (then: hunt note-went {note.id} if it went, hunt send-notes {note.id} to send it "
                                f"again)"]))
    return "\n\n".join(parts) or None


def went(notes: Notes, note: int) -> Note:
    """The person saw on LinkedIn that a note that may have gone did go."""
    found = _unsure(notes, note)
    notes.sent(found.id, found.conversation)
    return notes.get(note)


def _unsure(notes: Notes, note: int) -> Note:
    try:
        found = notes.get(note)
    except KeyError as error:
        raise NoteError(f"there is no note {note}") from error
    if found.state != "unsure":
        raise NoteError(f"note {note} is {found.state}, not waiting for a look whether it was sent")
    return found


def decide(notes: Notes, decision: Literal["send", "skip"], numbers: list[int] | None) -> list[Note]:
    """Approve or skip the notes asked about: all of them, or only those numbered. After "send 1 3"
    the others asked about are skipped; after "skip 2" they wait to be asked again, since only
    "send" approves a note. Return the notes approved or skipped as asked."""
    asked = {note.id: note for note in notes.in_states("drafted") if note.asked_at is not None}
    unsure = {note.id for note in notes.in_states("unsure")}
    if decision == "send" and numbers and set(numbers) <= unsure:
        # "send it again", after the person looked on LinkedIn and it had not gone.
        for number in numbers:
            notes.move(number, "approved")
        return [notes.get(number) for number in numbers]
    if not asked:
        raise NoteError("no note waits for an answer: run hunt questions to ask")
    chosen = list(asked) if numbers is None else numbers
    unknown = [str(number) for number in chosen if number not in asked]
    if unknown:
        raise NoteError(f"no note asked about has the number {', '.join(unknown)}; asked: "
                        f"{', '.join(map(str, asked))}")
    for number in asked:
        if number in chosen:
            notes.move(number, "approved" if decision == "send" else "skipped")
        elif decision == "send":
            # A note left out of "send 1 3" is not sent; one left out of "skip 2" is asked again.
            notes.move(number, "skipped")
    return [notes.get(number) for number in chosen]


# Sending


def stopped_today(data: Path, now: datetime) -> str | None:
    """Why sending stopped today, or None."""
    try:
        day, _, reason = (data / STOPPED).read_text().partition(" ")
    except FileNotFoundError:
        return None
    return reason.strip() or "LinkedIn refused a note" if day == now.date().isoformat() else None


def sendable(notes: Notes, data: Path, daily: int, now: datetime) -> list[Note]:
    """The approved notes that may go now: none once sending stopped today or the cap is reached."""
    if stopped_today(data, now) is not None or notes.gone_today(now) >= daily:
        return []
    return notes.in_states("approved")


def send_command(note: Note, root: Path) -> list[str]:
    report = _folder(root, note.job) / "note-sent.json"
    arguments = {"id": note.id, "name": note.name, "title": note.title, "profile": note.profile,
                 "text": note.note, "report": str(report)}
    return ["claude", "--chrome", "-p", f"/{SEND_SKILL} {json.dumps(arguments, ensure_ascii=False)}",
            "--model", MODEL, *ONLY_NAMED_TOOLS, "--allowedTools", ",".join([*SEND_TOOLS, f"Edit({_rule(report)})"])]


@dataclass
class Sent:
    sent: int = 0
    unreachable: int = 0
    unsure: int = 0
    failed: int = 0
    left: int = 0
    why_stopped: str | None = None

    def __str__(self) -> str:
        said = f"{self.sent} notes sent"
        for count, what in ((self.unreachable, "no longer free to message"), (self.unsure, f"need {UNSURE}"),
                            (self.failed, "failed")):
            if count:
                said += f", {count} {what}"
        if self.left:
            said += f", {self.left} wait"
        return said + (f"; {self.why_stopped}" if self.why_stopped else "")


def _read_sent(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise NoteError(f"the {SEND_SKILL} run wrote no report it can be read by") from error
    result = data.get("result") if isinstance(data, dict) else None
    if result == "sent":
        return {"result": "sent", "conversation": _text(data, "conversation")}
    if result in ("not_free", "refused", "failed"):
        return {"result": result, "reason": _text(data, "reason") or result.replace("_", " ")}
    raise NoteError(f'the {SEND_SKILL} run wrote a report whose "result" is not sent, not_free, refused or failed')


def recover_sends(notes: Notes, data: Path) -> None:
    """A note left `sending` while no send runs was stopped by a run that died: it may have gone."""
    data.mkdir(parents=True, exist_ok=True)
    with (data / "notes-send.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for note in notes.in_states("sending"):
            notes.move(note.id, "unsure", f"{UNSURE}: hunt send-notes stopped while sending")


def send_notes(notes: Notes, threads: Threads, jobs: JobsList, root: Path, *, run: Run, lock: LinkedInLock,
               sleep: Callable[[float], None], daily: int = DAILY,
               clock: Callable[[], datetime] | None = None) -> Sent:
    """Send the approved notes, one at a time, each under the LinkedIn lock."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    # Held while sending, so a note `sending` with the lock free is a dead run's.
    with (data / "notes-send.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        return _send_notes(notes, threads, jobs, root, run, lock, sleep, daily, clock or notes.now)


def _send_notes(notes: Notes, threads: Threads, jobs: JobsList, root: Path, run: Run, lock: LinkedInLock,
                sleep: Callable[[float], None], daily: int, clock: Callable[[], datetime]) -> Sent:
    data = root / "data"
    summary = Sent()
    while True:
        now = clock()
        if (why := stopped_today(data, now)) is not None:
            summary.why_stopped = f"sending stopped for today: {why}"
            break
        waiting = notes.in_states("approved")
        if not waiting:
            break
        if notes.gone_today(now) >= daily:
            summary.why_stopped = f"at most {daily} a day: the rest go tomorrow"
            break
        note = waiting[0]
        if notes.written_to(note.profile, note.id):
            notes.move(note.id, "skipped", "written to already")
            continue
        while (later := lock.take(f"note {note.id}")) is not None:
            sleep(later)
        try:
            _send_one(notes, threads, root, run, note, summary)
        finally:
            lock.release()
    summary.left = len(notes.in_states("approved"))
    return summary


def _send_one(notes: Notes, threads: Threads, root: Path, run: Run, note: Note, summary: Sent) -> None:
    report = _folder(root, note.job) / "note-sent.json"
    report.unlink(missing_ok=True)
    notes.move(note.id, "sending")
    failure: str | None = None
    try:
        try:
            run(send_command(note, root))
        except RuntimeError as error:
            failure = str(error)
        result = _read_sent(report)
    except NoteError as error:
        notes.move(note.id, "unsure", f"{UNSURE}: {failure or error}")
        summary.unsure += 1
        return
    except BaseException:
        # Stopped in the middle (Ctrl-C, a killed run): it may have gone, and must not be lost.
        notes.move(note.id, "unsure", f"{UNSURE}: hunt send-notes stopped while sending")
        raise
    if result["result"] == "sent":
        notes.sent(note.id, result["conversation"])
        if result["conversation"]:
            threads.noted(result["conversation"], note.name, note.note or "", note.job)
        summary.sent += 1
    elif result["result"] == "not_free":
        notes.move(note.id, "unreachable", result["reason"])
        summary.unreachable += 1
    elif result["result"] == "refused":
        # Nothing went: the note waits for another day, and no other note goes today.
        notes.move(note.id, "approved", result["reason"])
        now = notes.now()
        (root / "data" / STOPPED).write_text(f"{now.date().isoformat()} {result['reason']}\n")
        log_failure(root / "data", "hunt send-notes", f"LinkedIn stopped the notes for today: {result['reason']}",
                    notes.now)
    elif notes.fail(note.id, result["reason"], "approved") == "failed":
        summary.failed += 1
