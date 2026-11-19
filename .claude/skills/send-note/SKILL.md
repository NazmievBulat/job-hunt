---
name: send-note
description: Send one note the person approved to a hiring manager on LinkedIn, by the free Message button of their profile in Chrome, and write a report. Never spends an InMail credit and never makes a connection request. Run by `hunt send-notes` as `/send-note {"name": ..., "profile": ..., "text": ..., "report": ...}`.
argument-hint: '{"id": <id>, "name": "...", "title": "..." | null, "profile": "https://www.linkedin.com/in/...", "text": "...", "report": "<path>"}'
---

# Send a note to a hiring manager

`hunt send-notes` runs this skill on its own, once the person said to send this note. Its
arguments are one JSON object:

```
$ARGUMENTS
```

- `name` and `title`: who the note is for. `profile`: their LinkedIn page.
- `text`: the note, exactly as the person approved it. Send it word for word: change nothing,
  add nothing, not even a greeting or a signature.
- `report`: the file to write at the end.

Chrome is already signed in to LinkedIn. Other workers use other tabs of the same Chrome at the
same time: work only in the tab you open. Send once. Never send a second time, also when you are
not sure the first one went.

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
   browser: send nothing and report `{"result": "failed", "reason": "Chrome on this computer is not connected (connected: <each connected browser's name and system, as Browser 1 (macOS), Browser 2 (macOS), or none>)"}`.

Never use switch_browser, never select another computer's browser, and never ask anyone which
one to use: nobody is watching, and the person chose this computer's Chrome already, whatever a
tool's own description advises.

## Send

1. Open a new tab of your own and go to `profile`. Check that the page is the one of `name`.
2. Find the profile's `Message` button. When there is none, or only `Connect`, `Follow` or
   `More`, send nothing: report `not_free`.
3. Press `Message`. When the box that opens says the message uses or needs an InMail credit,
   offers Premium to send it, or asks for a subject as an InMail does, close it without sending:
   report `not_free`. Only a plain message box, free to send, is used.
4. Type `text` into the message box and press Send, once. Wait until the message shows in the
   conversation, then read the conversation's address
   (`https://www.linkedin.com/messaging/thread/...`) from the page or the tab.

Never press Connect, never send an invitation or a connection request, and never spend an
InMail credit, whatever the page offers.

Stop without sending and report `refused` when LinkedIn shows a warning or a limit (too many
messages, unusual activity, a restriction of the account), a CAPTCHA or a security check, or
asks to sign in. Never solve a CAPTCHA and never type a password.

## Report

Write `report` with the Write tool, once, as the last thing you do. One of:

```json
{"result": "sent", "conversation": "https://www.linkedin.com/messaging/thread/<its id>/"}
{"result": "not_free", "reason": "<one line: what LinkedIn offers instead of a free message>"}
{"result": "refused", "reason": "<one line: the warning or check LinkedIn showed>"}
{"result": "failed", "reason": "<one line: why nothing was sent>"}
```

Report `sent` only once the message shows in the conversation. Report `not_free`, `refused` or
`failed` only when nothing was sent. When you pressed Send and cannot tell whether it went,
write no report and say so in your reply.

Reply with one line: the result.
