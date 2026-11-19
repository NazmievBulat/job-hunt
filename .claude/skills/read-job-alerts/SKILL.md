---
name: read-job-alerts
description: Read the job alert emails in Gmail since a given time and write the jobs they list to one JSON file. Run by `hunt find` as `/read-job-alerts {"after": ..., "since": ..., "areas": [...], "out": ...}`.
argument-hint: '{"after": <unix seconds>, "since": "<ISO time>", "areas": ["<area>"], "out": "<path>"}'
---

# Read the job alert emails

`hunt find` runs this skill on its own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `after`: read only emails that arrived after this time, in Unix seconds (`since` is the same
  time, written out).
- `areas`: the areas the person looks for jobs in, as they named them.
- `out`: the file to write.

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

Do nothing else: do not open job pages, do not change, label or answer any email. Reply with
one line: how many emails and jobs you wrote.
