---
name: offer-times
description: For a recruiter's request to talk, read the person's free times in Google Calendar and draft the reply offering two or three of them; on the person's yes, add the held event, and when the recruiter picks a time, move it there. Run by `hunt draft-replies`, `hunt offer-times` and `hunt send-reply` as `/offer-times {"task": "offer" | "hold" | "confirm", ...}`.
argument-hint: '{"task": "offer", "thread": <id>, "source": "email" | "linkedin", "message": "<path>", "posting": "<path>" | null, "career": "<path>", "email_id": "..." | null, "old_draft_id": "..." | null, "time_zone": "America/New_York", "days": [{"day": "2026-11-19", "from": "09:00", "to": "17:00"}], "minutes": 30, "out": "<path>"} or {"task": "hold" | "confirm", "title": "...", "start": "2026-11-19T10:00", "end": "2026-11-19T10:30", "time_zone": "...", "event_id": "..." (confirm only), "out": "<path>"}'
---

# Offer times for a call

The `hunt` command runs this skill on its own, with nobody watching. Its arguments are one JSON
object:

```
$ARGUMENTS
```

`task` says which of the three jobs below to do. Do only that one.

## task: offer

A recruiter asked the person for a call. Find when the person is free and write the reply that
offers those times. Nothing is sent: the person reads the reply first.

- `message`: the recruiter's message. `posting`: the job's description, or `null`.
- `career`: the person's career file: their name, jobs, skills and `form_answers`.
- `days`: the working hours of each day to look in, in `time_zone`. `minutes`: a call's length.
- `source`: `email` or `linkedin`. `email_id`: the Gmail id of the email, for an email.
  `old_draft_id`: the Gmail draft of an earlier reply to this message, or `null`.
- `out`: the file to write.

1. Read the person's events from the first day's `from` to the last day's `to` with the Google
   Calendar connector's list_events: `startTime` and `endTime` as local times with no offset,
   and `timeZone` set to `time_zone`. Read every page. Every event the person has not declined
   is busy, a whole day's event too; an event marked free (availability free) is not.
2. A free time is a call of `minutes` that starts on the hour or the half hour, starts and ends
   inside one day's `from` and `to`, and overlaps no busy event; a call may start the minute an
   event ends. Choose two or three of them, on different days when there are free times on more
   than one day, and not at the very start or the very end of a day when others are free. When
   only one time is free, offer that one. When none is, offer none.
3. Write the reply the person would send, in the first person, signed with their first name (on
   LinkedIn too): glad to talk about the role, named as the message names it and not by the
   career's titles, the times you chose as a short list, each with its weekday, date, time and
   the time zone's short name (for example "Thursday, November 19, 10:00 EST"), and a line
   asking which suits, or for another time if none does. When no time is free, say the person
   would be glad to talk and ask for a few times that suit the recruiter. Answer briefly
   anything else the message asks, from the career; leave out what it does not say. Short: what
   a busy recruiter reads in half a minute. A LinkedIn reply is shorter still, with no signature
   block. Plain text: no Markdown, no placeholders.
4. For an email (`source` is `email`), save the reply as a Gmail draft answering that email:
   create_draft with `replyToMessageId` set to `email_id` and the reply as `body`. When
   `old_draft_id` is not `null`, delete that draft with delete_draft. Never send anything.
5. Write `out` with the Write tool, once, at the end:

```json
{"busy": [{"start": "2026-11-19T13:00:00-05:00", "end": "2026-11-19T14:00:00-05:00"},
          {"start": "2026-11-20", "end": "2026-11-21"}],
 "times": ["2026-11-19T10:00:00-05:00", "2026-11-23T14:30:00-05:00"],
 "reply": "the reply's text",
 "draft_id": "the Gmail draft's id, or null"}
```

`busy` lists every busy event of those days as you read it: times with their offset, or, for a
whole day's event, its first day and the day after its last. `times` are the starts of the calls
the reply offers, with their offset, in the order the reply gives them; `[]` when it offers none.
`hunt` checks each of them against `busy` and the days, so offer no time an event touches.

## task: hold

The person said yes to a reply offering times, and it was sent. Add the first time offered to
the calendar, so nothing else is booked over it: create_event with `summary` set to `title`,
`startTime` to `start`, `endTime` to `end` and `timeZone` to `time_zone`. Add no attendees, so
no invitation goes to anyone. Create it once; never a second time, also when you are not sure
the first one was made.

## task: confirm

The recruiter chose one of the times offered. Move the held event there: update_event with
`eventId` set to `event_id`, `startTime` to `start`, `endTime` to `end`, `timeZone` to
`time_zone` and `summary` to `title`. Change nothing else.

## The report of hold and confirm

Write `out` with the Write tool, once, as the last thing you do. One of:

```json
{"result": "done", "event_id": "the event's id"}
{"result": "failed", "reason": "<one line: why nothing was changed>"}
```

Report `done` only once the calendar returned the event. Report `failed` only when nothing was
created or changed. When you cannot tell, write no report and say so in your reply.

Do nothing else: read only the files given, write only `out`. Reply with one line: done, or the
result.
