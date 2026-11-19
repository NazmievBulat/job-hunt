# job-hunt

A job-search assistant that runs on [Claude Code](https://claude.com/claude-code) with your own
Claude subscription. Each morning it reads your job alert emails, scores the new jobs against
your career, and sends you one numbered list. You reply with the ones you want ("apply 1 3 4"),
and it writes a resume for each and applies through your own Chrome. It pauses for what needs
you: a CAPTCHA, a site sign-in, a question it cannot answer. It drafts replies to recruiters for
you to send.

There are no app screens. You use it from a Claude Code session, on your computer or from your
phone through Remote Control.

**Status:** being built. Setup (`/setup`, `hunt doctor`), the resume builder, the finder, scoring,
the list with picking, applying and the morning run work; recruiter replies and offering times
are next. See [`docs/design.md`](docs/design.md).

## What it needs

- Claude Code, signed in with a Claude subscription
- The Claude in Chrome extension, and the Gmail connector on your Claude account
- Python 3.14 and [`uv`](https://docs.astral.sh/uv/)
- Linux or macOS, with your computer on in the morning
- Optionally Chrome Remote Desktop on the computer and your phone, to solve a CAPTCHA or a site
  sign-in from the phone; without it, that one application waits until you are at the computer

## Start

```sh
git clone https://github.com/NazmievBulat/job-hunt.git
cd job-hunt
uv sync
claude
```

Then type `/setup` in Claude Code. It checks what is missing and says how to add it, reads your
resume, asks what to look for and your standing form answers, and switches on the morning run,
in about ten minutes. Run it again to change one thing (`/setup search`). `uv run hunt doctor`
prints what the hunt still needs at any time.

## How it keeps you in charge

- It applies only to the jobs you pick, and sends an email only after you approve its draft.
- Your employers, job titles, dates, schools and certifications are printed from your own
  career file; the model writes only the summary, the skills and the bullets.
- It never types a password and never solves a CAPTCHA. Sign in once in your browser, and it
  uses that browser.
- Your career, jobs and resumes stay on your computer, in `data/` and `jobs/`.

## Run the tests

```sh
uv run pytest -q
```

## License

Apache-2.0 (see [`LICENSE`](LICENSE)). Built through [Shipline](https://github.com/NazmievBulat/Shipline).
