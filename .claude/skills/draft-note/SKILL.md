---
name: draft-note
description: Draft a short note in the person's voice to the hiring manager who posted a job they just applied to on LinkedIn, from the job and the career, and write it to one JSON file. Sends nothing. Run by `hunt draft-notes` as `/draft-note {"note": ..., "title": ..., "company": ..., "poster": ..., "career": ..., "out": ...}`.
argument-hint: '{"note": <id>, "job": <id>, "title": "...", "company": "...", "poster": {"name": "...", "title": "..." | null}, "posting": "<path>" | null, "career": "<path>", "limit": 300, "out": "<path>"}'
---

# Draft a note to a hiring manager

`hunt draft-notes` runs this skill on its own, with nobody watching. Its arguments are one JSON
object:

```
$ARGUMENTS
```

- `title` and `company`: the job the person just applied to through LinkedIn Easy Apply.
- `poster`: the person who posted the job, with their name and title.
- `posting`: the job's description, or `null` when there is none.
- `career`: the person's career file: their name (`person`), jobs, schools, certifications,
  skills and summary.
- `limit`: the most characters the note may have.
- `out`: the file to write.

The person reads the note before anything is sent, and sends it or skips it.

## Write the note

Read the posting and the career, then write the note the person would send to the poster as a
LinkedIn message:

- In the first person, as the person. Greet the poster by first name.
- Say that the person applied for the role, named by its title.
- Give one reason from the career that fits this job: an employer, a skill or a kind of work
  the posting asks for and the career shows. Only what the career says.
- At most `limit` characters, spaces included. Two or three short sentences.
- Plain text: no Markdown, no links, no attachment, no placeholders such as "[your name]",
  and no signature block. No request for a call and no question about salary.

Write `out` with the Write tool, once, at the end:

```json
{"note": "the note's text"}
```

Do nothing else: read only the files given, write only `out`. Reply with one line: done.
