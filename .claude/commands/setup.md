---
description: Set up the job hunt, one step at a time - check what it needs, read the resume, say what to look for, turn on job alerts, give the standing form answers, switch on the morning run. Run again to change one thing.
argument-hint: '[resume | search | alerts | answers | pushover | remote desktop | timer]'
allowed-tools: Bash(uv run hunt doctor), Bash(uv run hunt career:*), Bash(uv run hunt career-from-resume:*), Bash(uv run hunt settings:*), Bash(uv run hunt form-answers:*), Bash(uv run hunt timer:*), Bash(uv --version), Bash(uv sync), Bash(uv python install 3.14), Read(data/settings.json), Read(data/career.json), Skill(read-resume)
---

# Set up the job hunt

The person has just cloned this project, or is changing one thing. Speak to them in plain
words, one step at a time: one message a step, then wait for the reply. Say nothing about how
the hunt is built.

The step to change, if they named one: `$ARGUMENTS`. When it names a step, do only that step,
then run `uv run hunt doctor` and show what it prints. Otherwise go through the steps in order.
A step already done is said in one line ("Your resume is read: 4 jobs.") and skipped.

## 1. What the hunt needs

Run `uv run hunt doctor`. When `uv` is not found, say: "The hunt needs uv. Install it from
https://docs.astral.sh/uv/, then run /setup again." and stop. When Python 3.14 is missing, run
`uv python install 3.14` and `uv sync`, then the doctor again.

The doctor prints one line for each thing: `ok`, `missing` with how to add it, or `optional`.
The career, the settings and the timer are made by the steps below; leave them for now. For each
other line marked `missing`, one at a time, say what to do in the doctor's words and wait for
"done", then run the doctor again. A fix that is a command the person must run themselves
(`claude auth login`), they type in this chat with `!` in front: `! claude auth login`.

## 2. The resume

Done when the doctor says `ok` for `data/career.json`. Otherwise ask: "Send me your resume: the
path to a PDF or Word file on this computer, or paste its text here." Then run the
`read-resume` skill with `{"resume": "<the path>", "draft": "data/career-draft.json"}`, or with
`"resume": null` for pasted text. It shows what it read and saves it once the person says it is
right.

A person who has a career export in the shape `hunt career` reads gives its path instead: run
`uv run hunt career <path>` and show what it prints.

## 3. What to look for

Done when the doctor says `ok` for `data/settings.json`. Otherwise ask, in one message:

- the job titles to look for;
- the areas, by name as people say them ("Raleigh-Durham-Chapel Hill", "New York City");
- remote jobs: included, excluded, or only remote;
- the lowest salary, or none;
- the hour of the morning run, 7 if they do not mind.

Save the answers with `uv run hunt settings '<json>'`, one JSON object:
`{"titles": [...], "areas": [...], "remote": "include" | "exclude" | "only", "lowest_salary":
<a whole number or null>, "morning_hour": <0 to 23>}`. When it refuses, ask again for what it
names. Show the line it prints.

## 4. Job alerts

The hunt finds jobs in the job alert emails in the person's Gmail, so ask: "Turn on job alerts
on the job boards you use (LinkedIn, Indeed and others), for these titles and areas, sent to the
Gmail account connected to Claude. Reply done when they are on." Name the titles and areas from
step 3. When this is the step they named, or they say the alerts are on already, it is done.

## 5. The standing form answers

Read `data/career.json`; its `form_answers.ready` holds the answers given before. When all five
are there, show them in one line and the step is done. Otherwise ask for the missing ones in one
message:

- authorized to work in the country of the jobs: yes or no;
- needs visa sponsorship: yes or no;
- the salary wish, as forms should get it ("120000", "120000 to 140000");
- the notice period ("two weeks");
- willing to relocate: yes or no.

Save them with `uv run hunt form-answers '<json>'`, an object of `authorized`, `sponsorship`,
`salary`, `notice` and `relocate` (yes and no written as "yes" and "no"). When it refuses, ask
again for what it names.

## 6. Pushover, if they want it (optional)

Done when `data/settings.json` has `pushover`. Otherwise ask once: "The hunt can push each
question to your phone with Pushover (pushover.net). Send its application token and your user
key, or reply skip." With the keys, save them with `uv run hunt settings '<json>'`: the settings
of step 3 with `"pushover": {"token": "...", "user": "..."}` added. Never repeat the keys in a
message.

## 7. Chrome Remote Desktop (optional)

Done when the doctor says `ok` for Chrome Remote Desktop. A paused application (a CAPTCHA, a
site sign-in) is solved by the person in this computer's Chrome, never by Claude. So ask: "To
solve those from your phone, open https://remotedesktop.google.com/access in Chrome on this
computer, turn on remote access and choose a PIN, then install the Chrome Remote Desktop app on
your phone, signed in to the same Google account. Reply done, or skip to solve them at the
computer." On done, run the doctor and say whether it now shows `ok`.

## 8. The morning run

Run `uv run hunt timer on` and show the line it prints. When it refuses, show its line.

## The end

Run `uv run hunt doctor` and show what it prints. When every line is `ok` or `optional`, say:
"All set. Each morning at <hour>:00 the hunt reads your job alerts and sends you the new jobs to
pick from. To change one thing later, run /setup resume, search, alerts, answers, pushover,
remote desktop or timer." Otherwise say which step to run again.

Do nothing else: no other command, no other file read or written, no Chrome tab opened. Never
type a password and never ask for one.
