---
name: send-reply
description: Send one reply the person approved, as an email reply through Gmail or as a LinkedIn message in Chrome, and write a report. Run by `hunt send-reply` as `/send-reply {"thread": ..., "source": ..., "reply": ..., "report": ...}`.
argument-hint: '{"thread": <id>, "source": "email" | "linkedin", "reply": "...", "email_id": "..." | null, "draft_id": "..." | null, "conversation": "...", "resume": "<path>" (LinkedIn, when attached), "report": "<path>", "browser": "<deviceId>" (only when the settings name one)}'
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
- `resume`: given only for a LinkedIn message that goes out with the person's resume, a PDF.
- `report`: the file to write at the end.
- `browser`, only when the person's settings name one: the `deviceId` of the one browser to
  use (see "The browser first").

Send once. Never send a second time, also when you are not sure the first one went.

## The browser first

For a LinkedIn message, before anything else in Chrome, choose the browser. The person's
Claude account may have Claude in Chrome connected in several browsers on several computers,
and only one of them may be used.

1. Call list_connected_browsers.
2. When the arguments give `browser`, the person named that one browser by its `deviceId` in
   their settings, and no other may be used, not even this computer's Chrome. When a connected
   browser has exactly that `deviceId`, select it with select_browser and that `deviceId`, and
   note its name, its system and whether it is on this computer. Then go on. When none has it,
   or when select_browser fails, do nothing in any
   browser: send nothing and report `{"result": "failed", "reason": "The browser the settings name is not connected (connected: <each connected browser's name and system, as Browser 1 (macOS), Browser 2 (macOS), or none>)"}`.
3. Without `browser`, only the Chrome on the computer this runs on may be used, never another
   computer's. This computer's Chrome is the browser it gives `onThisComputer: true`. A browser
   it gives no `onThisComputer` for counts only when it says `isLocal: true`, its system is the
   one this runs on, and no other browser could be this computer's.
4. When exactly one browser is this computer's, select it with select_browser and its
   `deviceId`, and note its name and system. Then go on.
5. When none is, when more than one could be, or when select_browser fails, do nothing in any
   browser: send nothing and report `{"result": "failed", "reason": "Chrome on this computer is not connected (connected: <each connected browser's name and system, as Browser 1 (macOS), Browser 2 (macOS), or none>)"}`.

Never use switch_browser, never select a browser these steps did not choose, and never ask
anyone which one to use: nobody is watching, and the person chose already, whatever a tool's own
description advises.

An email goes through Gmail and needs no browser.

## An email

1. Send the reply with the Gmail connector's reply tool: `messageId` is `email_id`, `body` is
   `reply`. Do not reply to all. Attach nothing.
2. When `draft_id` is not `null`, delete that draft with delete_draft: it may hold older words.

## A LinkedIn message

1. In this computer's Chrome, selected as above, open a new tab of your own and go to
   `conversation`. Chrome is already signed in to LinkedIn.
2. Check that the conversation is the one with the sender the reply answers.
3. With `resume`, attach that file first with the attach button and file_upload, and wait
   until it shows under the message box.
4. Type `reply` into the message box and press Send, once. Wait until the message shows in the
   conversation.

Never solve a CAPTCHA and never type a password: when LinkedIn asks for one, or asks to sign in,
report `failed` without sending.

## Report

Write `report` with the Write tool, once, as the last thing you do. For a LinkedIn message,
every report but one that says Chrome is not connected also names the browser you
selected, `"browser": {"device_id": "<its deviceId>", "name": "<its name>", "system": "<its system>", "on_this_computer": <true for this computer's Chrome, else false>}`. One of:

```json
{"result": "sent"}
{"result": "failed", "reason": "<one line: why nothing was sent>"}
```

Report `sent` only once Gmail returned the sent message, or LinkedIn shows the message in the
conversation. Report `failed` only when nothing was sent. When you pressed Send and cannot tell
whether it went, write no report and say so in your reply.

Reply with one line: the result.
