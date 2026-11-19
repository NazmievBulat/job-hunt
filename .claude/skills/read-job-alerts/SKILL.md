---
name: read-job-alerts
description: Read the job alert emails in Gmail since a given time and write the jobs they list to one JSON file; with messages_after, also note the emails from recruiters and LinkedIn's new-message emails. Run by `hunt find` and `hunt watch` as `/read-job-alerts {"after": ..., "since": ..., "areas": [...], "out": ..., "messages_after": ...}`.
argument-hint: '{"after": <unix seconds> | null, "since": "<ISO time>" | null, "areas": ["<area>"], "out": "<path>", "messages_after": <unix seconds>}'
---

# Read the job alert emails

`hunt find` and `hunt watch` run this skill on their own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `after`: read only alert emails that arrived after this time, in Unix seconds (`since` is the
  same time, written out). When it is `null`, read no job alerts at all: write `"emails": 0`
  and `"jobs": []`.
- `areas`: the areas the person looks for jobs in, as they named them.
- `out`: the file to write.
- `messages_after`, only sometimes there: also read the messages that arrived after this time,
  in Unix seconds (see "Messages" below). Without it, read no messages.

## Find the alerts

Search Gmail with the Gmail connector for job alert emails that arrived after `after`, using
`after:<after>` in each search. Look for the alerts of LinkedIn, Indeed, Glassdoor, Built In,
ZipRecruiter, Dice and Wellfound, and job alerts from companies' own career sites (for example
a search for `after:<after> ("job alert" OR "new jobs" OR "jobs for you" OR "matching jobs")`
next to one search for each board's sender). Open every alert and read the jobs it lists. Page
through every result of a search. Leave out emails that are not alerts listing jobs: messages
from recruiters, application receipts, interview invitations, newsletters.

## Write each job

Write `out` with the Write tool, once, at the end, even when no alert was found:

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
      "email_id": "the Gmail id of the email",
      "email_date": "2026-11-17T07:04:00+00:00",
      "area": "Raleigh-Durham-Chapel Hill"
    }
  ]
}
```

- `emails` is the number of alert emails read.
- One entry for every job in every alert, also when two alerts list the same job: `hunt` keeps
  each job once. Keep the link as the email gives it; `hunt` removes the tracking parts.
- `place` is the place as the email writes it, without the words remote, hybrid or on-site.
  It is `""` when the email gives none.
- `remote` is `remote`, `hybrid` or `on_site`, as the email says it. When it says nothing,
  `on_site`.
- `board` is `linkedin`, `indeed`, `glassdoor`, `builtin`, `ziprecruiter`, `dice`, `wellfound`,
  or `company` for a company's own career site.
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

Add to the file you write, next to `emails` and `jobs`:

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
one line: how many emails and jobs you wrote, and how many messages.
