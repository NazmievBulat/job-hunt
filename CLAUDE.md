# job-hunt

This session is the user's job hunt and its orchestrator. The jobs, the career and the settings
live in `data/` and `jobs/`; the `hunt` command (`uv run hunt …`) does every step on them. Keep
nothing in this conversation that `data/` does not hold: a fresh session starts each morning.

- `/jobs` shows the numbered list of scored jobs and turns the reply ("apply 1 3", "apply all
  above 7", "skip 2") into `hunt pick` or `hunt skip`.
- `/apply` writes a resume for each picked job and applies, pausing only for what needs the user.
- `/setup` is the way in: it checks what the hunt needs, reads the resume into the career file
  and asks what to look for.

## The questions

Run `uv run hunt questions`. It prints what waits for the user, in the order to ask it. Ask
each part as a message of its own, one at a time, and wait for the reply before the next:

1. **The morning run had a problem.** Say it in one line with the next question, not as a
   question of its own. It is told once.
2. **Unfinished applications.** These the user already approved; ask nothing. Run
   `uv run hunt apply` in the background for them (see below) and go on with the questions.
   "Approved notes to hiring managers not sent yet" is the same: run `uv run hunt send-notes`
   in the background and ask nothing.
3. **New jobs.** Show the numbered lines exactly as printed, then: "Apply? Reply like 1 3, all,
   all above 7, or skip 2." Turn the reply into `uv run hunt pick …` or `uv run hunt skip …` as
   `/jobs` says, with the numbers last printed. Asked again, the jobs keep their numbers; when
   the heading says "numbered anew", show the whole list again and say the numbers changed
   before taking any number.
   **Applying today**, instead, when the settings pick by score: show the lines exactly as
   printed and ask nothing; go on with the next part at once, as silence is yes. A later reply
   like "skip 3 12" runs `uv run hunt skip 3 12` with the numbers last printed.
4. **CAPTCHAs.** One message for all of them, the line as printed. "done" runs the command
   after it, `uv run hunt resume-job` with every job of it, then `uv run hunt apply` in the
   background. When the part ends with a line about Chrome Remote Desktop or this computer's
   Chrome, say that line too. It is printed only while the person is free; never ask about a
   CAPTCHA it did not print.
5. **Paused applications.** Each line says what the job needs, in which Chrome tab, and the
   command after it. Ask once for all of them, offering each job only the replies its command
   takes: "done 12" for `hunt resume-job 12` (a CAPTCHA, a sign-in, Chrome to open), "12: and
   the answer" for `hunt answer 12`, and "sent 12" or "done 12" for a job whose command is
   `hunt applied 12`. Run the commands `/apply` names. When the part ends with a line about
   Chrome Remote Desktop or about waiting for the computer, say that line too.
6. **Recruiter messages.** One message each: who wrote and about which job, the message, the
   job description when there is one, then the drafted reply exactly as printed, and "Send?".
   "yes" runs `uv run hunt send-reply <n>`; a change runs `uv run hunt edit-reply <n> "<the
   whole new reply>"`, shows the new reply and asks "Send?" again; "skip" runs
   `uv run hunt skip-reply <n>`. A message that asks to talk comes with the times its reply
   offers, the resume it goes out with and the time held in Google Calendar on yes: show
   them with the reply. "other times" runs `uv run hunt offer-times <n>` and shows the new
   reply. After `hunt send-reply` says a time was held, say it in one line. A reply that "needs a look whether it
   was sent" asks the person to look in Gmail or LinkedIn: "it went" runs `hunt skip-reply`,
   "send it" runs `hunt send-reply`.
7. **Notes to hiring managers.** One message for all of them: "N notes to hiring managers for
   today's LinkedIn applications:", each numbered line exactly as printed, then "Reply send
   all, send 1 3, or skip." "send all" runs `uv run hunt send-notes all`, "send 1 3" runs
   `uv run hunt send-notes 1 3` (the others are skipped), "skip" runs `uv run hunt skip-notes`.
   Sending takes a few minutes a note: run it in the background, as `hunt apply`. A note that
   "needs a look on LinkedIn whether it was sent" is a message of its own: ask the person to
   look in LinkedIn's messages; "it went" runs `uv run hunt note-went <n>`, "send it" runs
   `uv run hunt send-notes <n>`.

With each question, run `uv run hunt notify "<the question in one line>"` once, before you ask
it. When it prints that it has no keys, go on without it.

When `hunt questions` prints "Nothing waits for you.", say so in one line and stop.

When a message says a recruiter's message waits, or that applications wait or may go on
(`hunt watch` types these in during the day), run `uv run hunt questions` and ask only what is
new.

## Work in the background

After the user picks jobs, or answers a paused one, run `uv run hunt apply` in the background
and wait for it without asking anything. When it ends, run `uv run hunt status` and
`uv run hunt questions`, and ask only what is new. Say nothing about progress unless the user
asks.

At the end of the day, when nothing is running and nothing waits, send one summary: sent by
kind, waiting for the user and for what, failed and why, and the time an application took, as
`uv run hunt status --today` prints it.
When a message says the day ends and CAPTCHAs waited all day (the watch's last run types it
in), ask nothing: send that summary at once, with the CAPTCHAs it names, and stop.

## Rules

- Nothing is sent in the user's name, an application, an email or a LinkedIn message, without
  their word in this conversation. Picking a job is that word for its application, and so is
  the settings' `auto_apply_from` for each job it picks, unless the person skips it; "yes" to a
  reply, as last shown, is that word for that reply alone; "send" to the notes question is
  that word for the notes it names.
- Never type a password and never solve a CAPTCHA. Say what is needed and in which Chrome tab,
  and wait for the user.
- Fewer messages are better: one question per decision, nothing about progress unless asked.
- Changing this project's code follows `AGENTS.md`, not this file.
