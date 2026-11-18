# Job hunt: design

A job-search assistant that runs on Claude Code with the user's own Claude subscription. It
has no app screens: the user works with it in a Claude Code session, on the computer or from
a phone through Remote Control.

Each morning it finds new jobs that fit the user and sends one numbered list. The user replies
with the ones they want ("apply 1 3 4"). It writes a resume for each and applies, and pauses
only for what needs the user: a CAPTCHA, a site sign-in, or a form question their career file
cannot answer. It also drafts replies to recruiters' emails for the user to send.

Success: on a normal day the user spends a minute reading the list and replying, and the
applications go out without any further step.

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
- Nothing is sent without the user's word: applications go out for the jobs they picked, and an
  email reply only after they approve its draft.

## Setup

`/setup` is a Claude Code command any user runs once, in about ten minutes:

1. It checks what the hunt needs: Claude Code signed in, the Claude in Chrome extension
   connected, the Gmail connector on, Python and `uv` installed. It says exactly what is
   missing and how to add it.
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
9. **Recruiter emails.** The finder also notes replies from recruiters and companies. For each,
   the session shows a drafted answer, saved as a Gmail draft. The user says "send", edits it,
   or says "skip".

## Pieces

Each piece has one job and talks to the others only through the jobs list and files.

| Piece | Kind | What it does |
|---|---|---|
| `hunt` | Python command | Every mechanical step: `career`, `resume`, `find`, `score`, `list`, `pick`, `apply`, `status`. It is the coordinator. |
| Jobs list | SQLite file `data/jobs.db` | One row per job: link, description, kind (Easy Apply or company site), score and reason, the user's decision, state, dates, reason for a failure. Only `hunt` writes it. |
| Career | `data/career.json` | The user's jobs, schools, certifications, skills, summary and standing form answers. |
| Settings | `data/settings.json` | Titles, areas by name, remote (include, exclude or only), salary floor, the timer's hour. |
| Resume worker | `claude -p` with the `write-resume` skill | Gets the career and one job description and writes `resume-content.json`. |
| Resume builder | Python (`resume_kit`) | Merges that content with the career, checks it against the resume contract, builds `resume.pdf`. |
| LinkedIn worker | `claude -p --chrome` with the `apply-linkedin` skill | Opens the job's LinkedIn page, presses Easy Apply, uploads the PDF, answers from the career, submits. |
| Company-site worker | `claude -p --chrome` with the `apply-site` skill | Opens the site's apply link, uploads the PDF, checks the fields the site read from it, answers, submits. |
| Reply drafter | `claude -p` with the `draft-reply` skill | Drafts an answer to one recruiter email as a Gmail draft. |
| Session rules | `.claude/commands` and `CLAUDE.md` | How the `job-hunt` session shows the list and turns replies into `hunt` commands. |

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
- If Chrome is closed or the computer is asleep, applying waits and says so once.
- Everything is files on the computer. If a session or worker stops halfway, `hunt apply`
  picks up from each job's state.
- The finder or the scoring failing is reported once, with the error.

## Usage

- The coordinator is a script and costs nothing.
- Measured on 2026-11-18: reading two weeks of alert emails on Haiku cost $0.32; one resume on
  Claude Sonnet took 28 seconds and $0.32; one small Chrome task on Haiku cost $0.02.
- Background work (`claude -p`) draws from the subscription's separate monthly credit for
  background use, not from its chat limits.
- A day with no new jobs costs only the finder's read.

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
5. The morning timer.
6. `/setup`, with the resume reader.
7. Recruiter email replies.
