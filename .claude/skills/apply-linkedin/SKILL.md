---
name: apply-linkedin
description: Apply to one job through LinkedIn Easy Apply in Chrome, with the resume PDF and the career's answers, and write a report of how it went. Run by `hunt apply` as `/apply-linkedin {"job": ..., "link": ..., "resume": ..., "career": ..., "report": ...}`.
argument-hint: '{"job": <id>, "link": "...", "title": "...", "company": "...", "resume": "<path>", "career": "<path>", "report": "<path>"}'
---

# Apply through LinkedIn Easy Apply

`hunt apply` runs this skill on its own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `link`: the job's LinkedIn page. `title` and `company` say which job it is.
- `resume`: the resume PDF made for this job.
- `career`: the person's career file: their name and contacts (`person`), jobs, schools,
  certifications, skills and `form_answers`, the standing answers and those saved from forms
  (`saved`, each a question in a form's words and its answer).
- `report`: the file to write at the end.
- `tab` and `tab_id`, only when this job paused before: the title and the id of the tab it
  paused in. The person has since done what it needed there (signed in, passed a security
  check) or answered its question.

The person picked this job, so applying is what they asked for. Chrome is already signed in to
LinkedIn. Other workers apply to other jobs on company sites in the same Chrome at the same
time: work only in your own tab, the one you open or `tab_id`.

## This computer's Chrome first

Before anything else in Chrome, choose the browser. The person's Claude account may have
Claude in Chrome connected in several browsers on several computers, and they use only the
Chrome on the computer this runs on, never another computer's.

1. Call list_connected_browsers.
2. This computer's Chrome is the browser it gives `onThisComputer: true`. A browser it gives no
   `onThisComputer` for counts only when it says `isLocal: true`, its system is the one this
   runs on, and no other browser could be this computer's.
3. When exactly one browser is this computer's, select it with select_browser and its
   `deviceId`, and note its name and system. Then go on.
4. When none is, when more than one could be, or when select_browser fails, do nothing in any
   browser: report `{"result": "paused", "needs": "chrome", "connected": "<each connected browser's name and system, as Browser 1 (macOS), Browser 2 (macOS), or none>"}`.
   That is the pause for "Chrome on this computer is not connected".

Never use switch_browser, never select another computer's browser, and never ask anyone which
one to use: nobody is watching, and the person chose this computer's Chrome already, whatever a
tool's own description advises.

## Apply

1. With `tab_id`, find the tab of that id among Chrome's tabs. When it shows this job's Easy
   Apply form (LinkedIn, this job's title), carry on there from where the form is now; the
   question it paused on now has an answer in the career. Never use a tab by its title alone:
   another job's tab may have the same one. Without `tab_id`, or when that tab is gone or shows
   something else, open a new tab of your own and go to `link`.
2. If the page says this person already applied ("Applied", "Application submitted"), stop:
   report `applied`.
3. If the page has no "Easy Apply" button but an "Apply" button that leads to the company's
   own site, do not apply there: find the address it leads to and report `company_site` with
   it. Read the address from the button's link when the page shows it; otherwise press the
   button and read the address of the page or tab it opens. Fill nothing on that site.
4. Press "Easy Apply". The form has several steps; go through them with "Next" or "Review".
5. Where it asks for a resume, upload `resume`, even when LinkedIn offers one uploaded before,
   and choose the one you uploaded.
6. Check every field LinkedIn filled in against the career: name, email, phone, city,
   employers, titles, dates, schools. Correct what is wrong.
7. Answer the rest from the career: the person's details, their jobs and schools, the standing
   form answers, and a saved answer whose question means the same as the form's. Optional
   fields with no answer in the career are left empty.
8. When the form has a cover letter field or a "message to the hiring team" field, fill it with
   a note of two or three sentences in the person's voice: the role by its title, and one
   reason from the career that fits the job, only what the career says. No placeholders, no
   Markdown. It is part of the application the person approved, so it is sent with it.
9. Questions about gender, race, ethnicity, veteran status or disability are voluntary: choose
   the option that declines to answer ("I don't wish to answer", "Decline to self-identify").
10. On the last step, clear the box that follows the company, if it is ticked.
11. Submit, once. Wait for LinkedIn to say the application was sent.

## The poster

Once LinkedIn said the application was sent, look at the job's page for the person who posted
the job ("Meet the hiring team", "Job poster"). When it shows one, note their name, their title
and the link to their profile (`https://www.linkedin.com/in/...`), and what LinkedIn offers to
reach them, without pressing anything that sends:

- `message`: a `Message` button that sends a free message.
- `inmail`: a `Message` that would spend an InMail credit, or that offers Premium to send it.
- `connect`: only `Connect`, or nothing else to write with.

When you cannot tell whether a `Message` is free, it is `inmail`. Never press Connect, never
send an invitation, never write to the poster here: only note who they are.

## Pause instead

Some things only the person can do. Leave the tab open exactly where it is and report
`paused` when:

- LinkedIn shows a CAPTCHA, a security check or asks to prove you are human (`captcha`). Never
  try to solve it.
- LinkedIn is signed out or asks the person to sign in again (`sign_in`). Never type a
  password, never pick one in a password prompt.
- a required question has no answer in the career (`question`). Put the question in the
  form's own words. Never guess an answer to a required question.
- you pressed Submit, but LinkedIn did not say it was sent: an error, a page that never loads,
  or nothing changed (`check_sent`). It may have been sent, so never submit again.
- Chrome cannot be reached: it is closed or its Claude extension does not answer (`chrome`).
  There is no tab to name. This computer's Chrome not among the connected browsers is this
  pause too, with `connected` (see "This computer's Chrome first").

## Report

Write `report` with the Write tool, once, as the last thing you do. `tab_id` is the id
Chrome's tools give the tab, a whole number. Every report but a `chrome` pause also names the
browser you selected; `<the browser>` below is
`{"name": "<its name>", "system": "<its system>", "on_this_computer": true}`. A report without it
is not taken, and the person has to look whether the job was sent. One of:

```json
{"result": "applied", "browser": <the browser>}
{"result": "applied", "poster": {"name": "...", "title": "...", "profile": "https://www.linkedin.com/in/...", "reach": "message" | "inmail" | "connect"}, "browser": <the browser>}
{"result": "company_site", "link": "<the address on the company's site>", "browser": <the browser>}
{"result": "paused", "needs": "captcha", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "sign_in", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "question", "question": "<the form's question>", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "check_sent", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "chrome"}
{"result": "paused", "needs": "chrome", "connected": "Browser 1 (macOS), Browser 2 (macOS)"}
{"result": "failed", "reason": "<one line: what went wrong, for the person to apply by hand>", "browser": <the browser>}
```

The `company_site` address is the company's own, never a LinkedIn one. Report `failed` only
when nothing was sent: the page is gone or says the job no longer takes applications,
LinkedIn shows an error before you pressed Submit, the form will not submit, or the job sends
to the company's site and you cannot find the address. Report `applied` only after LinkedIn
said the application was sent.

Do nothing else: read only the career and the files of this job, write only `report`, and do
not close the tab. Reply with one line: the result.
