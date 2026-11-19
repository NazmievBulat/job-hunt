---
name: read-resume
description: Read a person's resume (a PDF, a Word file or pasted text) into data/career.json, show them what was read, and save it once they say it is right. Run by /setup as `/read-resume {"resume": ..., "draft": ...}`.
argument-hint: '{"resume": "<path to a .pdf or .docx, or null when the text is pasted in the chat>", "draft": "data/career-draft.json"}'
allowed-tools: Read, Edit(data/career-draft.json), Bash(uv run hunt resume-text:*), Bash(uv run hunt career-from-resume:*)
---

# Read a resume into the career

`/setup` runs this skill with the person in the chat. Its arguments are one JSON object:

```
$ARGUMENTS
```

- `resume`: the resume file, a PDF or a Word (`.docx`) file. When it is null, the person pasted
  the resume's text in the chat; read that.
- `draft`: the file to write what you read into.

## Read

For a PDF, read it with the Read tool. For a Word file, run `uv run hunt resume-text <resume>`,
which prints its text. A file of another kind is refused by that command: ask the person to save
it as a PDF or paste its text.

## Write the draft

Write `draft` with the Write tool, in this shape:

```json
{
  "person": {"name": "...", "email": "...", "phone": "...", "location": "City, ST",
             "links": [{"url": "https://...", "label": "LinkedIn"}]},
  "summary": "the resume's summary, or empty",
  "titles": ["the job titles the resume's heading gives the person"],
  "skills": ["each skill once"],
  "jobs": [
    {"title": "...", "company": "...", "location": "... or null", "start_date": "2019-03",
     "end_date": "2022-11", "material": ["each bullet under this job"]}
  ],
  "education": [
    {"school": "...", "degree": "Bachelor's degree", "field_of_study": "Physics or null",
     "start_date": "2010 or null", "end_date": "2014 or null"}
  ],
  "certifications": ["each certification's name"]
}
```

- Employers, titles, places, dates, schools, degrees and certifications are printed on every
  resume as they are here, so copy each as the resume writes it.
- A date is the year and month, `2019-03`, or the year alone, `2019`, when the resume gives only
  the year. The job the person has now has `end_date` null.
- `material` holds the bullets under that job, word for word. They are what the resume writer
  works from later.
- Leave out a field the resume does not have (a phone, links), or make it null. Write only the
  fields shown: the check refuses others.

## Check and show

Run `uv run hunt career-from-resume <draft>`.

- When it refuses, it names each problem. A problem you misread, fix in the draft. What the
  resume itself lacks (an employer, a start date, an email) only the person knows: ask for all
  of it in one message, put the answers in the draft, and run the check again.
- When it passes, it prints what was read. Show that exactly as printed and ask: "Is this right?
  Reply yes, or tell me what to change."

When the person names changes, make them in the draft with the Edit tool, run the check again
and show it again. Once they say yes, run `uv run hunt career-from-resume <draft> --save`, and
reply with the line it printed last.

Do nothing else: read only the resume, write only the draft.
