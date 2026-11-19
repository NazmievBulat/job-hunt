---
description: Show the numbered list of scored jobs and record which to apply to or skip.
allowed-tools: Bash(uv run hunt list), Bash(uv run hunt pick:*), Bash(uv run hunt skip:*)
---

# The list of jobs

Run `uv run hunt list` and show what it prints exactly as printed, then ask: "Which ones?
Reply like apply 1 3, apply all above 7, or skip 2."

When the person replies, turn the reply into the command it means and run it. Each example
below is one reply and its command; use the numbers the person wrote, no others:

- "apply 1 3 4" → `uv run hunt pick 1 3 4`
- "3" → `uv run hunt pick 3`
- "apply all above 7" → `uv run hunt pick --above 7`
- "apply all" → `uv run hunt pick --above 0`
- "skip 2" → `uv run hunt skip 2`
- "skip 2 and 5" → `uv run hunt skip 2 5`
- "apply 1, skip 2" → `uv run hunt pick 1`, then `uv run hunt skip 2`

When the settings pick by score, the list shown is "Applying to N jobs today": "skip 3 12"
leaves those out (`uv run hunt skip 3 12`) and nothing else is asked.

The numbers are always those of the list shown. Show what the command printed. When it refuses
(a number not on the list), show its line and ask again. A reply that is not one of these is
answered by asking which numbers they mean.

Do nothing else: no other command, no file read or written, no job opened.
