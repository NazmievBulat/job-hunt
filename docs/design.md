# Job hunt: design

A job-search assistant that runs on Claude Code with the user's own Claude subscription. It
has no app screens: the user works with it in a Claude Code session, on the computer or from
a phone through Remote Control.

The user talks to one chat, the `job-hunt` session. It is their orchestrator: it runs the
finder, the resume writers and the applying workers, several at once, and asks the user only
when a decision is theirs. Each question is one short message with what it needs shown (the
job, the resume, the reply, the time), answered with "yes" or a short change.

Success: the user answers a few questions a day from their phone, and everything else happens
without them.

## Decisions

- Each user runs it on their own computer, on the official Claude Code program, signed in with
  their own Claude subscription. Background work starts Claude Code with `claude -p`. It does
  not use the Agent SDK and never routes one person's work through another's subscription.
- No app and no web screens. The chat session is the interface; `/setup` is the way in.
- Choosing a job approves its resume. Nothing reviews or tests the model's writing. A bad
  resume is one lost application.
- The prompt carries no "do not invent" warning. Employers, titles, dates, degrees and
  certifications never pass through the model (see Resume). The model writes the summary,
  the bullets and the skills to match the job as closely as it can. It is not asked for numbers.
- Company-site accounts (Workday and others): the user creates them the first time, or their
  password manager fills them in. Claude never sees or types a password.
- One LinkedIn worker handles every Easy Apply job, one at a time. Other workers handle jobs
  on company sites, up to three at once.
- Nothing is sent without the user's word: applications go out for the jobs they picked, an
  email reply only after they approve its draft, and a note to a hiring manager only on their
  "send" to the day's notes question.
- Notes to hiring managers use only LinkedIn's free `Message`: never an InMail credit, never a
  connection request (a free account may add a note to only a few invitations a month, and
  invitations have a weekly cap; checked 2026-11-18). At most 15 a day, one person written to
  once, and a LinkedIn warning ends the day's sending.

## The chat

- The `job-hunt` session runs in `tmux` with Remote Control, so the user reaches it from the
  Claude app on their phone. A fresh one starts each morning, so its context stays small; the
  state lives in `data/`, never in the chat.
- It does the mechanical work through `hunt` commands and starts `claude -p` workers for
  judgment. It waits for them in the background and asks the user only for decisions.
- The questions, each a message of its own:
  - **A new job.** Title, company, area, score and its one-line reason, Easy Apply or company
    site: "Apply?" Several new jobs come as one numbered list, answered "1 3" or "all".
  - **A paused application.** A CAPTCHA, a sign-in, or a form question the career file cannot
    answer, with the Chrome tab: "done" or the answer.
  - **A recruiter's message**, by email or on LinkedIn. The message, the job description when
    there is one, and a drafted reply: "Send?"
  - **Notes to hiring managers.** One question for all the notes drafted after the day's
    LinkedIn applications, each with who it is for and its words: "send all", "send 1 3" or
    "skip".
  - **A request to talk.** Free times from the user's Google Calendar and the reply offering
    one, with the resume for that job: "Send?" On yes it sends and adds the time to the
    calendar as held; when the recruiter confirms, the event is confirmed.
- Every question also goes out as a push through Pushover when the user set its keys in
  `/setup`; without them the Claude app's own notification for a waiting session does it.
- Fewer messages are better: one question per decision, nothing about progress unless asked,
  one summary at the end of a day.
- Nothing is sent in the user's name and no time is booked without their yes to that message.

## Setup

`/setup` is a Claude Code command any user runs once, in about ten minutes:

1. It checks what the hunt needs with `hunt doctor`: Claude Code signed in, the Claude in Chrome
   extension connected, the Gmail connector on, Python and `uv` installed. It says exactly what
   is missing and how to add it. The doctor calls no model, so it can be run at any time.
2. It reads the user's resume (PDF, Word or pasted text) into `data/career.json` and shows what
   it read for the user to correct. An export in the career shape `hunt career` reads works too.
3. It asks what to look for: job titles, the areas by name ("Raleigh-Durham-Chapel Hill",
   "New York City"), remote jobs included, excluded or only, and the lowest salary. These go in
   `data/settings.json`.
4. It asks the user to turn on job alerts on the boards they use (LinkedIn, Indeed and others)
   for those searches, since the finder reads the alert emails.
5. It asks the standing form answers once: work authorization, sponsorship, notice period,
   salary wish, willingness to relocate. These go in the career file's form answers.
6. It switches on the morning timer.

A step already done is said and skipped; `/setup <step>` changes one thing. Two optional steps:
Pushover keys, and Chrome Remote Desktop on the computer and the phone. A paused application
(a CAPTCHA, a site sign-in) is solved by the user in the computer's own Chrome, never by Claude;
with Chrome Remote Desktop the pause question carries its link, so it is done from the phone.
Without it the job waits until the user is at the computer, and the other jobs go on.

## How a day goes

1. **Finder (on a timer).** At the chosen hour one `claude -p` on Claude Haiku reads the job
   alert emails through the Gmail connector and writes each job's link, title, company and
   place. The run also says which of the user's areas each job is in. A script drops jobs
   already in the list, wrong titles, and jobs outside the areas or the remote setting. For each job left it saves the full description from the job page and notes
   whether it is a LinkedIn Easy Apply or a company site.
2. **Scoring (only when the finder found something).** One `claude -p` reads the new jobs and
   the career file, and gives each a fit score from 1 to 10 with a one-line reason.
3. **The list reaches the user.** The script opens a Claude Code session named `job-hunt` with
   Remote Control, which shows the numbered list. With nothing new, nothing opens and nothing
   is spent.
4. **The user replies** "apply 1 3 4", "apply all above 7" or "skip 2". The session runs the
   `hunt` command with the picks. That is all it does, so it stays cheap.
5. **Resumes (in parallel).** One resume worker per pick (see Resume).
6. **Applying.** The LinkedIn worker takes the Easy Apply picks one after another, a few
   minutes apart. Up to three company-site workers take the rest at once.
7. **Paused jobs.** A CAPTCHA, a site that needs an account, or a form question the career
   file cannot answer pauses that one job. The session says what is needed and in which Chrome
   tab, and the other jobs go on. The user replies "done" or gives the answer; an answer to a
   form question is saved, so it is not asked again.
8. **Summary.** One message at the end: applied, waiting for the user, failed and why.
9. **Recruiter messages (every 20 minutes during the day).** A recruiter gets an answer within
   the hour. `hunt watch` runs every 20 minutes from the morning hour to the summary hour: one
   small `claude -p` on Claude Haiku with only Gmail's read tools notes new emails from
   recruiters and companies and counts LinkedIn's own emails about a new message. Only after
   one of those, and besides in the morning and at mid-day, does `hunt linkedin-messages` open
   LinkedIn's messages in Chrome, under the LinkedIn lock, reading and sending nothing else. A
   run with nothing new ends there. Each new message is a thread, saved once in the jobs list
   and linked to its job; `hunt draft-replies` drafts a reply in the user's voice from the
   message, the career and the job (for an email also as a Gmail draft), and the session asks
   at once: the message, the job description, the reply, "Send?". The user says yes
   (`hunt send-reply`), changes it (`hunt edit-reply`) or says "skip" (`hunt skip-reply`). A
   send that does not report back may have gone out: it is never sent again on its own.
10. **Notes to hiring managers.** When an Easy Apply form has a cover letter or "message to the
    hiring team" field, the LinkedIn worker fills it with a note of two or three sentences, sent
    with the application. After applying it saves who posted the job and whether a free
    `Message` reaches them. At the end of `hunt apply`, `hunt draft-notes` drafts a note of at
    most 300 characters for each one it does (`draft-note`); the chat asks once for all of them.
    `hunt send-notes` sends the approved ones by the free `Message` (`send-note`), each under the
    LinkedIn lock, one to three minutes apart, at most `linkedin_notes_daily` a day. A sent note
    is a thread of the recruiter replies, so the answer is drafted and asked like any other.

## Pieces

Each piece has one job and talks to the others only through the jobs list and files.

| Piece | Kind | What it does |
|---|---|---|
| `hunt` | Python command | Every mechanical step: `doctor`, `career`, `career-from-resume`, `settings`, `form-answers`, `resume`, `find`, `score`, `list`, `pick`, `apply`, `answer`, `resume-job`, `applied`, `status`, `morning`, `questions`, `notify`, `timer`, `watch`, `linkedin-messages`, `draft-replies`, `send-reply`, `edit-reply`, `skip-reply`, `draft-notes`, `send-notes`, `skip-notes`, `note-went`. It is the coordinator. |
| Jobs list | SQLite file `data/jobs.db` | One row per job: link, description, kind (Easy Apply or company site), score and reason, the user's decision, state, dates, reason for a failure. Only `hunt` writes it. |
| Career | `data/career.json` | The user's jobs, schools, certifications, skills, summary and standing form answers. |
| Settings | `data/settings.json` | Titles, areas by name, remote (include, exclude or only), salary floor, the morning hour, the summary hour (18 when left out), the notes to hiring managers a day (`linkedin_notes_daily`, 15 when left out, at most 15). |
| Resume worker | `claude -p` with the `write-resume` skill | Gets the career and one job description and writes `resume-content.json`. |
| Resume builder | Python (`resume_kit`) | Merges that content with the career, checks it against the resume contract, builds `resume.pdf`. |
| LinkedIn worker | `claude -p --chrome` with the `apply-linkedin` skill | Opens the job's LinkedIn page, presses Easy Apply, uploads the PDF, answers from the career, submits. |
| Company-site worker | `claude -p --chrome` with the `apply-site` skill | Opens the site's apply link, uploads the PDF, checks the fields the site read from it, answers, submits. |
| Reply drafter | `claude -p` with the `draft-reply` skill | Drafts an answer to one recruiter's message, an email (also as a Gmail draft) or a LinkedIn message. |
| Reply sender | `claude -p` with the `send-reply` skill | Sends one approved reply word for word: through Gmail, or in Chrome on LinkedIn under the LinkedIn lock. |
| Watch | `hunt watch` on a timer | Every 20 minutes from the morning hour to the summary hour: the Haiku check for new messages, LinkedIn only when needed, drafting and asking. |
| The chat | `CLAUDE.md` and `.claude/commands` | The `job-hunt` session: asks the questions, turns answers into `hunt` commands, starts and waits for workers. |
| Notifier | `hunt notify` | Sends one question as a Pushover push, when the user set its keys. |
| Scheduler | `claude -p` with the `offer-times` skill | Reads free times from Google Calendar, drafts the reply offering them, adds the held event on yes. |
| Note drafter | `claude -p` with the `draft-note` skill | Drafts a note of at most 300 characters to the poster of a job applied to on LinkedIn. |
| Note sender | `claude -p --chrome` with the `send-note` skill | Sends one approved note by the poster's free `Message` button, under the LinkedIn lock; never InMail, never Connect. |
| LinkedIn inbox | `claude -p --chrome` with the `read-linkedin-messages` skill | Reads new recruiter messages on LinkedIn, one worker at a time under the LinkedIn lock. |

The coordinator is a script, not a Claude session. It reads the list, starts the workers,
collects their reports and writes states. Claude is used only for judgment: reading emails,
scoring, writing, filling forms.

Each job has its own folder, `jobs/<id>/`, holding the description, the resume content, the
PDF and the worker's report. A worker sees only its own folder and the career.

`data/` and `jobs/` hold the user's own data. They are never committed.

## Resume

1. The resume worker writes only this:
   ```json
   {
     "headline": "…",
     "summary": "…",
     "skills": [{"title": "…", "items": ["…"]}],
     "experience": [{"job": "<career job id>", "bullets": ["…"]}]
   }
   ```
   `job` points at one of the jobs in the career. The model never writes an employer, title,
   date, degree or certification.
2. The builder fills the fixed parts from the career: name, contacts, employers, titles,
   places, dates, education, certifications.
3. The resume contract checks the merged resume. On an error the builder prints it, and the
   worker fixes its file and runs the builder again, at most three times.
4. The builder makes the PDF with the layout and fonts in `resume_kit/documents`.

## Browser

- The workers use the user's own Chrome through the Claude in Chrome extension. The user signs
  in to LinkedIn and to company sites there, and the browser stays signed in.
- Several `claude -p --chrome` workers can each drive a tab of their own in one Chrome at the
  same time (checked on 2026-11-18 with three).
- Only one worker at a time may use LinkedIn: a lock file the LinkedIn worker holds for each
  application. Company-site workers are limited to three at once.
- Workers do not solve CAPTCHAs. They pause and ask the user.

## When things go wrong

- A job whose worker fails twice (the site errors, the form breaks) is marked failed with the
  reason, and is named in the summary for the user to apply by hand.
- A worker that stops without saying what happened (a timeout, a crash, no report) may have
  sent the application. Its job is never tried again on its own: it pauses for the user to look
  in Chrome, and the user says it was sent or tries it again.
- If Chrome is closed or the computer is asleep, applying waits and says so once. (Built so
  far: a worker that cannot reach Chrome pauses its job, which costs it no try.)
- Everything is files on the computer. If a session or worker stops halfway, `hunt apply`
  picks up from each job's state.
- The finder or the scoring failing is reported once, with the error.

## Usage

- The coordinator is a script and costs nothing.
- Measured on 2026-11-18: reading two weeks of alert emails on Haiku cost $0.32; one resume on
  Claude Sonnet took 28 seconds and $0.32; one small Chrome task on Haiku cost $0.02.
- Background work (`claude -p`) draws from the subscription's separate monthly credit for
  background use, not from its chat limits.
- A day with no new jobs costs only the finder's read, and the watch's small Haiku checks (33 a
  day from 7:00 to 18:00); LinkedIn is opened only after a notification email, and twice a day.

## Testing

- The resume contract and layouts keep their tests. They test code, not the model, and run in
  seconds.
- New code gets tests for what it decides: the finder's filters and email parsing, never
  listing the same job twice, the jobs list's states, the LinkedIn lock and the limit of three.
- Nothing tests the model's writing or its scores.

## Not in the first version

- Weekly statistics.
- Job sites without alert emails.
- Applying without the user's daily pick.

## Build order

1. Done: the browser check, the career import, the resume builder and the `write-resume` skill.
2. The finder and the jobs list.
3. Scoring, the `job-hunt` session and picking.
4. The company-site worker, then the LinkedIn worker.
5. The chat: the morning session, its questions and Pushover.
6. Done: `/setup`, with the resume reader and `hunt doctor`.
7. Done: recruiter replies, by email and on LinkedIn, watched every 20 minutes.
8. Offering times from Google Calendar.
