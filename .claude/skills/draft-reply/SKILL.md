---
name: draft-reply
description: Draft a reply in the person's voice to one recruiter's message, by email or on LinkedIn, from the message, the career and the job, and write it to one JSON file; for an email also save it as a Gmail draft. Sends nothing. Run by `hunt draft-replies` as `/draft-reply {"thread": ..., "message": ..., "career": ..., "out": ...}`.
argument-hint: '{"thread": <id>, "source": "email" | "linkedin", "message": "<path>", "posting": "<path>" | null, "career": "<path>", "email_id": "..." | null, "asks_to_talk": false, "offered_times": ["<ISO time>"] (only after an offer), "out": "<path>"}'
---

# Draft a reply to a recruiter

`hunt draft-replies` runs this skill on its own, with nobody watching. Its arguments are one
JSON object:

```
$ARGUMENTS
```

- `message`: the message to answer: who wrote, the subject, the channel, and the words.
- `posting`: the description of the job it is about, or `null` when there is none.
- `career`: the person's career file: their name and contacts (`person`), jobs, schools,
  certifications, skills, summary and `form_answers` (work authorization, notice period, salary
  wish, willingness to relocate).
- `source`: `email` or `linkedin`. `email_id`: the Gmail id of the email, for an email.
- `asks_to_talk`: the message asks for a call or an interview.
- `offered_times`: given only when the person's last reply in this conversation offered times
  for a call: those times. Left out otherwise.
- `out`: the file to write.

The person reads the reply before anything is sent, and says yes, changes it or skips it.

## Write the reply

Read the message, the posting and the career, then write the reply the person would send:

- In the first person, as the person, signed with their first name.
- Short: what a busy recruiter reads in half a minute. A LinkedIn reply is shorter still, with
  no subject and no signature block.
- Answer what the message asks, from the career: their experience for the role, work
  authorization, notice period, whether they would relocate, from `form_answers`. Leave out what
  the career does not say rather than guess.
- When the message names a salary, or asks for the person's salary wish, answer with the
  career's salary wish when it has one; otherwise say the person would like to talk about it.
- When the message asks to talk, say the person would be glad to and will send some times;
  do not offer times or dates yourself.
- With `offered_times`, the message may answer that offer. When it picks one of those times,
  or names a time that is one of them, the reply confirms that time in a line or two, with its
  weekday, date and time. When it asks for a time that is not one of them, the reply says the
  person will look and come back; do not agree to it yourself.
- When the role is far from what the person does, thank the recruiter and say so kindly.
- Plain text: no Markdown, no placeholders such as "[your name]".

## Save it

For an email (`source` is `email`), save the reply as a Gmail draft answering that email: use
the Gmail connector's create_draft with `replyToMessageId` set to `email_id` and the reply as
`body`. Read the thread with get_thread first when you need the earlier emails. Never send it.

Write `out` with the Write tool, once, at the end:

```json
{"reply": "the reply's text", "draft_id": "the Gmail draft's id, or null", "time": null}
```

`draft_id` is `null` on LinkedIn, and for an email whose draft could not be saved. `time` is the
one of `offered_times` the message picked, exactly as given there, and `null` in every other
case: no `offered_times`, no time picked, or a time that is not one of them.

Do nothing else: read only the files given, write only `out`. Reply with one line: done.
