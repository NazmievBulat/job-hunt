# job-hunt

This session is the user's job hunt. The jobs, the career and the settings live in `data/` and
`jobs/`; the `hunt` command (`uv run hunt …`) does every step on them.

- `/jobs` shows the numbered list of scored jobs and turns the reply ("apply 1 3", "apply all
  above 7", "skip 2") into `hunt pick` or `hunt skip`.
- `/apply` writes a resume for each picked job and applies, pausing only for what needs the user.
- `/setup` is the way in: it checks what the hunt needs, reads the resume into the career file
  and asks what to look for.

Rules:

- Nothing is sent in the user's name, an application or an email, without their word in this
  conversation. Picking a job is that word for its application.
- Never type a password and never solve a CAPTCHA. Say what is needed and in which Chrome tab,
  and wait for the user.
- Changing this project's code follows `AGENTS.md`, not this file.
