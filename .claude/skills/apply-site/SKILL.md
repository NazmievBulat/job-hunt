---
name: apply-site
description: Apply to one job on the company's own site in Chrome, with the resume PDF and the career's answers, and write a report of how it went. Run by `hunt apply` as `/apply-site {"job": ..., "link": ..., "resume": ..., "career": ..., "report": ...}`.
argument-hint: '{"job": <id>, "link": "...", "title": "...", "company": "...", "resume": "<path>", "career": "<path>", "report": "<path>"}'
---

# Apply on a company site

`hunt apply` runs this skill on its own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `link`: the job's page. `title` and `company` say which job it is.
- `resume`: the resume PDF made for this job.
- `career`: the person's career file: their name and contacts (`person`), jobs, schools,
  certifications, skills and `form_answers`, the standing answers and those saved from forms
  (`saved`, each a question in a form's words and its answer).
- `report`: the file to write at the end.
- `tab` and `tab_id`, only when this job paused before: the title and the id of the tab it
  paused in. The person has since done what it needed there (solved the CAPTCHA, signed in) or
  answered its question.

The person picked this job, so applying is what they asked for. Other workers apply to other
jobs in the same Chrome at the same time: work only in your own tab, the one you open or `tab_id`.

## Apply

1. With `tab_id`, find the tab of that id among Chrome's tabs. When it shows this job's
   application (the company's site, this job's title), carry on there from where the form is
   now; the question it paused on now has an answer in the career. Never use a tab by its
   title alone: another job's tab may have the same one. Without `tab_id`, or when that tab is
   gone or shows something else, open a new tab of your own and go to `link`. Find the way to apply (an
   "Apply" button, a link to the company's careers site) and follow it in that tab.
2. If the site says this person has already applied to this job, stop: report `applied`.
3. Upload `resume` wherever the form asks for a resume or offers to fill the form from one.
4. Check every field the site filled from the resume against the career: name, email, phone,
   address, employers, titles, dates, schools. Correct what it read wrong.
5. Answer the rest from the career: the person's details, their jobs and schools, the standing
   form answers, and a saved answer whose question means the same as the form's. Optional
   fields with no answer in the career are left empty.
6. Questions about gender, race, ethnicity, veteran status or disability are voluntary: choose
   the option that declines to answer ("I don't wish to answer", "Decline to self-identify").
7. Submit, once. Wait for the page that confirms it was sent.

## Pause instead

Some things only the person can do. Leave the tab open exactly where it is and report
`paused` when:

- the site shows a CAPTCHA or asks to prove you are human (`captcha`). Never try to solve it.
- the site needs the person to sign in or to create an account (`sign_in`). Never type a
  password, never create an account, never pick one in a password prompt.
- a required question has no answer in the career (`question`). Put the question in the
  form's own words. Never guess an answer to a required question.

## Report

Write `report` with the Write tool, once, as the last thing you do. `tab_id` is the id
Chrome's tools give the tab, a whole number. One of:

```json
{"result": "applied"}
{"result": "paused", "needs": "captcha", "tab": "<the tab's title>", "tab_id": <the tab's id>}
{"result": "paused", "needs": "sign_in", "tab": "<the tab's title>", "tab_id": <the tab's id>}
{"result": "paused", "needs": "question", "question": "<the form's question>", "tab": "<the tab's title>", "tab_id": <the tab's id>}
{"result": "failed", "reason": "<one line: what went wrong, for the person to apply by hand>"}
```

Report `failed` when the job cannot be applied to here: the page is gone or says the job is
closed, the site shows an error, the form will not submit, or Chrome cannot be reached. Report
`applied` only after the site confirmed the application was sent.

Do nothing else: read only the career and the files of this job, write only `report`, and do
not close the tab. Reply with one line: the result.
