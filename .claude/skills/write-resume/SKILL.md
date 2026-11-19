---
name: write-resume
description: Write the resume content for one job posting in a job folder, then build its PDF. Use when asked to write a resume for jobs/<id>.
---

# Write a resume for one job

The job folder is the arguments' `folder`, and the career the arguments' `career`; without
them, they are `jobs/<id>/` and `data/career.json`. Use these paths as they are given. The
folder holds `posting.md`, the job description. Read the files with the Read tool: the one
shell command this work may run is the build below, and any other is refused.

## Write

Create a winning resume for this job. ATS coverage and selling the candidate are the
priorities.

- Write the candidate as an ideal match for this posting. Use the career's jobs, their
  `material`, the career summary and the skills as the starting point, and add or strengthen
  the skills, responsibilities and leadership the posting asks for. Use direct-experience
  language. Never mention gaps, uncertainty or missing evidence.
- Keep the summary under 45 words, with a value proposition for this job.
- One clear idea per bullet, preferably 12 to 18 words, never more than 20.
- Lead each recent job with its strongest bullet for this posting.
- Sell through ownership, scale and technical depth, not superlatives.
- Each tool in a bullet has a distinct, explained role. One coherent stack per claim: do not
  pile alternative frameworks into one bullet for keyword coverage.
- Map each important term of the posting to one credible place. Broad exact terms go in the
  skills rows; bullets hold coherent implementation claims.

## Format

Write `resume-content.json` in the job folder:

```json
{
  "headline": "job title and a few key skills, 15 words or fewer",
  "summary": "the summary paragraph",
  "skills": [{"title": "Row title", "items": ["item", "item"]}],
  "experience": [{"job": "<id of a job in the career>", "bullets": ["..."]}]
}
```

- `experience` has one entry for every job in the career, using its `id`, newest first,
  3 to 6 bullets each (more for the recent jobs). Employers, job titles, places, dates,
  schools and certifications are printed from the career, so they are not written here.
- Each skills row holds at most 10 items; its title and items together hold at most 17 words,
  and `Title: item · item` fits in 100 characters. Start another row instead of overfilling one.

## Build

Run the arguments' `build` command exactly as given, or `uv run hunt resume jobs/<id>` without
it, even after another command was refused. It prints the PDF's path when the resume is built. When it prints `resume-content.json needs
a fix: …`, fix what it names and run it again, up to three times. Then reply with one line: the
PDF's path, or the last error.
