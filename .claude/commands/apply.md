---
description: Write a resume for each picked job and apply, on the company sites and through LinkedIn Easy Apply, then show where every picked job stands.
allowed-tools: Bash(uv run hunt apply), Bash(uv run hunt status), Bash(uv run hunt answer:*), Bash(uv run hunt resume-job:*), Bash(uv run hunt applied:*)
---

# Apply to the picked jobs

Run `uv run hunt apply` in the background: it writes the resumes and applies, and can take
many minutes. Wait for it to finish, then run `uv run hunt status` and show what it prints
exactly as printed. When `hunt apply` refuses (another one is running), show its line.

A paused job waits for the person. Its line says what it needs, in which Chrome tab, and the
command to run after. Ask once for all of them: "Reply done for a CAPTCHA or sign-in you did, or
once Chrome is open, or give the answer to a question." When a job waits for a look in Chrome
whether it was sent, add: "Reply sent if Chrome shows it was sent, or done to try it again."

When the person replies, turn it into the commands it means and run them, using the job
numbers `hunt status` printed:

- "done 12" (or "done" when only one job is paused) → `uv run hunt resume-job 12`
- "done" to the CAPTCHAs question → `uv run hunt resume-job` with every job its line names, such
  as `uv run hunt resume-job 12 14`
- "sent 12" (or "sent" when only one job waits for that look) → `uv run hunt applied 12`
- "12: yes, since 2021" → `uv run hunt answer 12 "yes, since 2021"`

Then run `uv run hunt apply` again for those jobs, unless every reply was "sent", and show
`uv run hunt status` once more.

Do nothing else: no other command, no file read or written, no Chrome tab opened. Never type
a password and never solve a CAPTCHA.
