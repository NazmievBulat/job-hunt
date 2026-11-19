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

1. With `tab_id`, find the tab of that id among Chrome's tabs. When it shows this job's
   application (the company's site, this job's title), carry on there from where the form is
   now; the question it paused on now has an answer in the career. When that tab still shows
   the CAPTCHA or the sign-in it paused on, report the same pause again at once (see "Pause
   instead"). Never use a tab by its
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
- you clicked Submit, but the site did not confirm it was sent: an error page, a page that
  never loads, or nothing changed (`check_sent`). It may have been sent, so never submit again.
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
{"result": "paused", "needs": "captcha", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "sign_in", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "question", "question": "<the form's question>", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "check_sent", "tab": "<the tab's title>", "tab_id": <the tab's id>, "browser": <the browser>}
{"result": "paused", "needs": "chrome"}
{"result": "paused", "needs": "chrome", "connected": "Browser 1 (macOS), Browser 2 (macOS)"}
{"result": "failed", "reason": "<one line: what went wrong, for the person to apply by hand>", "browser": <the browser>}
```

Report `failed` only when nothing was sent: the page is gone or says the job is closed, the
site shows an error before you clicked Submit, or the form will not submit. Report `applied`
only after the site confirmed the application was sent.

Do nothing else: read only the career and the files of this job, write only `report`, and do
not close the tab. Reply with one line: the result.
