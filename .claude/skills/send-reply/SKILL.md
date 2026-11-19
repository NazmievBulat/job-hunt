---
name: send-reply
description: Send one reply the person approved, as an email reply through Gmail or as a LinkedIn message in Chrome, and write a report. Run by `hunt send-reply` as `/send-reply {"thread": ..., "source": ..., "reply": ..., "report": ...}`.
argument-hint: '{"thread": <id>, "source": "email" | "linkedin", "reply": "...", "email_id": "..." | null, "draft_id": "..." | null, "conversation": "...", "resume": "<path>" (when attached), "resume_base64": "<path>" (email only), "report": "<path>"}'
---

# Send a reply

`hunt send-reply` runs this skill on its own, once the person said yes to this reply. Its
arguments are one JSON object:

```
$ARGUMENTS
```

- `reply`: the text to send, exactly as the person approved it. Send it word for word: change
  nothing, add nothing, not even a greeting or a signature.
- `source`: `email` or `linkedin`.
- For an email: `email_id`, the message to answer, and `draft_id`, the earlier draft of this
  reply or `null`.
- For LinkedIn: `conversation`, the conversation's address.
- `resume`: given only when the reply goes out with the person's resume, a PDF. For an email,
  `resume_base64` is a file holding that PDF's bytes as base64.
- `report`: the file to write at the end.

Send once. Never send a second time, also when you are not sure the first one went.

## An email

1. Send the reply with the Gmail connector's reply tool: `messageId` is `email_id`, `body` is
   `reply`. Do not reply to all. With `resume`, read `resume_base64` and add one entry to
   `attachments`: `content` is that file's text exactly as it is, `filename` is the last part of
   `resume`'s path, `mimeType` is `application/pdf`.
2. When `draft_id` is not `null`, delete that draft with delete_draft: it may hold older words.

## A LinkedIn message

1. Open a new tab of your own and go to `conversation`. Chrome is already signed in to LinkedIn.
2. Check that the conversation is the one with the sender the reply answers.
3. With `resume`, attach that file first with the attach button and file_upload, and wait
   until it shows under the message box.
4. Type `reply` into the message box and press Send, once. Wait until the message shows in the
   conversation.

Never solve a CAPTCHA and never type a password: when LinkedIn asks for one, or asks to sign in,
report `failed` without sending.

## Report

Write `report` with the Write tool, once, as the last thing you do. One of:

```json
{"result": "sent"}
{"result": "failed", "reason": "<one line: why nothing was sent>"}
```

Report `sent` only once Gmail returned the sent message, or LinkedIn shows the message in the
conversation. Report `failed` only when nothing was sent. When you pressed Send and cannot tell
whether it went, write no report and say so in your reply.

Reply with one line: the result.
