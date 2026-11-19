---
name: read-job-alerts
description: Search Gmail for the job alert emails since a given time and list them, or read the jobs of the alert emails it is named, and write one JSON file; with messages_after, the search also notes the emails from recruiters and LinkedIn's new-message emails. Run by `hunt find` and `hunt watch` as `/read-job-alerts {"after": ..., "since": ..., "out": ..., "messages_after": ...}` or `/read-job-alerts {"email_ids": [...], "areas": [...], "out": ...}`.
argument-hint: '{"after": <unix seconds> | null, "since": "<ISO time>" | null, "out": "<path>", "messages_after": <unix seconds>} or {"email_ids": ["<Gmail message id>"], "areas": ["<area>"], "out": "<path>"}'
---

# Read the job alert emails

`hunt find` and `hunt watch` run this skill on their own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

A run does one of two tasks, which the arguments say:

- **The search**, when the arguments have `after`: list the alert emails that arrived after
  `after` (see "List the alerts"), and, when they have `messages_after`, read the messages too
  (see "Messages"). It reads no alert's jobs: `hunt` hands the listed emails to later runs.
- **A batch**, when the arguments have `email_ids`: read the jobs of those alert emails, and
  only those (see "Read the jobs").

The arguments:

- `after`: list only alert emails that arrived after this time, in Unix seconds (`since` is the
  same time, written out). When it is `null`, list none: write `"alerts": []`.
- `messages_after`: when the arguments have it, a time in Unix seconds: also read the messages
  that arrived after it (see "Messages") and add them to `out`. When the arguments do not have
  it, read no messages.
- `email_ids`: the Gmail message ids of the alert emails a batch reads.
- `areas`: the areas the person looks for jobs in, as they named them.
- `out`: the file to write, with the Write tool, once, at the end, also when nothing was found.
  `hunt` takes a file that is there as a task done whole, so write nothing to it before then.

## List the alerts

Search Gmail with the Gmail connector's `search_threads` for job alert emails that arrived after
`after`, using `after:<after>` in each search. Look for the alerts of LinkedIn, Indeed,
Glassdoor, Built In, ZipRecruiter, Dice, Wellfound and Jobot, and job alerts from companies'
own career sites (for example a search for
`after:<after> ("job alert" OR "new jobs" OR "jobs for you" OR "matching jobs")` next to one
search for each board's sender). Page through every result of a search with its `pageToken`.
Do not open the emails: each search result has its messages' ids, senders, subjects and dates,
and that is all this task needs. Leave out the emails that are not alerts listing jobs: messages
from recruiters (those are for "Messages"), application receipts, interview invitations,
newsletters.

Gmail puts a board's daily alerts, which share a sender and a subject, in one thread, and a
search result shows at most 5 messages of a thread, which need not be the ones that matched the
search. When a thread of alerts shows 5 messages, or fewer than its `messageCount`, list all of
its messages with `get_thread` and `"messageFormat": "METADATA_ONLY"`, which gives each
message's id and date and none of its text, and keep those that arrived after `after`. Never
call `get_thread` with another format in this task: an alert's text is larger than a tool's
answer may be.

Write `out`:

```json
{
  "alerts": [
    {"email_id": "the Gmail id of the message", "email_date": "2026-11-17T07:04:00+00:00"}
  ]
}
```

- One entry for each alert email, also when two searches found it: `hunt` keeps each once.
- `email_id` is the message's own id from the search result, copied exactly, not its thread's.
- `email_date` is when the email arrived, as an ISO time.

## Read the jobs

Read each email of `email_ids`, one at a time, with the Gmail connector's `get_message` and
`"messageFormat": "PLAIN_TEXT"`. Never use another format, and never `get_thread`: an alert's
HTML is larger than a tool's answer may be. Read no other email and make no search. An id that
Gmail does not find is left out and not counted in `emails`.

```json
{
  "emails": 4,
  "jobs": [
    {
      "link": "the job's own link from the email, exactly as written",
      "title": "Senior QA Automation Engineer",
      "company": "Example Labs",
      "place": "Durham, NC",
      "remote": "hybrid",
      "board": "linkedin",
      "email_id": "the Gmail id of the email, as it is in email_ids",
      "email_date": "2026-11-17T07:04:00+00:00",
      "area": "Raleigh-Durham-Chapel Hill"
    }
  ]
}
```

- `emails` is the number of the emails of `email_ids` that you read.
- One entry for every job in every alert, also when two alerts list the same job: `hunt` keeps
  each job once.
- Keep the link as the email gives it, however long, also a click-tracking address (Jobot's,
  refer.io's, a `click.` or `track` address): copy it whole, character for character, and never
  leave a job out because of its link. `hunt` follows it to the job's page and removes the
  tracking parts.
- `place` is the place as the email writes it, without the words remote, hybrid or on-site.
  It is `""` when the email gives none.
- `remote` is `remote`, `hybrid` or `on_site`, as the email says it. When it says nothing,
  `on_site`.
- `board` is `linkedin`, `indeed`, `glassdoor`, `builtin`, `ziprecruiter`, `dice`, `wellfound`,
  `jobot`, or `company` for a company's own career site.
- `email_date` is when the email arrived, as an ISO time.
- `area` is the one name from `areas`, written exactly as it is there, that the job's place is
  in: a town inside an area or next to it is in it ("Morrisville, NC" is in
  "Raleigh-Durham-Chapel Hill"; "San Jose, CA" is in "San Francisco Bay Area"). It is `null`
  when the place is in none of them, or when the email gives no place or only a country.

## Messages

Only when `messages_after` is given. Search Gmail for emails that arrived after
`messages_after`, using `after:<messages_after>` in each search, and look at each one that is
not from the person themselves:

- **A recruiter's or a company's email to the person about a role or an application**: a
  recruiter offering a job, a company answering an application, an invitation to talk or to
  interview, a question about the person's application. Not job alerts, not newsletters, not
  automatic receipts ("we received your application") and not rejections with nothing to
  answer. Add one entry to `recruiter_emails` for each such email.
- **LinkedIn's own email about a new message** ("… sent you a message", "… sent you a new
  message", "You have a new message"), from LinkedIn. Count them in `linkedin_notifications`;
  do not open LinkedIn.

Add to the file the search writes, next to `alerts`:

```json
{
  "recruiter_emails": [
    {
      "email_id": "the Gmail id of the email",
      "thread_id": "the Gmail id of its thread",
      "from": "Jane Recruiter <jane@example.com>",
      "subject": "the email's subject",
      "text": "the email's own words, without the quoted earlier emails and the signature's legal text",
      "date": "2026-11-18T09:12:00-05:00",
      "job_link": "the link to the job's posting in the email, or null",
      "company": "the company the role is at, or null",
      "asks_to_talk": true
    }
  ],
  "linkedin_notifications": 1
}
```

- `asks_to_talk` is `true` when the email asks for a call, a meeting or an interview, or asks
  when the person is free; otherwise `false`.
- `job_link` is a link to one posting, not to a careers page or a list of jobs.
- With no such email, write `"recruiter_emails": []` and `"linkedin_notifications": 0`.

Do nothing else: do not open job pages, do not change, label or answer any email. Reply with
one line: how many alert emails you listed and how many messages, or, for a batch, how many
emails and jobs you wrote.
