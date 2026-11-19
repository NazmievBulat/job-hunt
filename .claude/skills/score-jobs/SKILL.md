---
name: score-jobs
description: Score how well each new job fits the person's career, from 1 to 10 with one line of reason, and write the scores to one JSON file. Run by `hunt score` as `/score-jobs {"jobs": [...], "career": ..., "out": ...}`.
argument-hint: '{"jobs": [{"id": <id>, "title": "...", "company": "...", "posting": "<path>"}], "career": "<path>", "out": "<path>"}'
---

# Score the new jobs

`hunt score` runs this skill on its own, with nobody watching. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `jobs`: the jobs to score, each with its id, title, company and `posting`, the file that holds
  its description.
- `career`: the person's career file: their jobs (with `material`, what they did there), schools,
  certifications, skills and summary.
- `out`: the file to write.

## Score

Read the career once, then each posting. Give each job a fit score from 1 to 10: how likely
this person is to be a strong candidate for it, judged by what they have done.

- 9 or 10: the work is what they have done, at their level, with the tools the posting names.
- 7 or 8: most of what the posting asks for is in the career; a gap or two a resume can bridge.
- 4 to 6: part of the work fits, but the main tools, the field or the level differ.
- 1 to 3: a different kind of work, or a requirement they plainly lack (a licence, a clearance,
  many more years).

A posting that says only what the alert gave, because its page could not be read, is scored
from its title and company, with that said in the reason.

The reason is one line of at most 20 words, for the person deciding whether to apply: the
strongest match or the main gap ("Playwright and Python as in the last two jobs; asks for Java
too").

## Write

Write `out` with the Write tool, once, at the end:

```json
{
  "scores": [
    {"job": 12, "score": 8, "reason": "Playwright and Python as in the last two jobs; asks for Java too."}
  ]
}
```

- One entry for each job in `jobs`, with `job` set to its `id` exactly, a whole number.
- `score` is a whole number from 1 to 10.

Do nothing else: read only the career and the postings, write only `out`. Reply with one line:
how many jobs you scored.
