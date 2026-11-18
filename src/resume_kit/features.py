"""Feature flags: every new feature can be switched off, for everyone or for single accounts.

`FLAGS` lists every flag there is. Code asks `feature_on("<name>")` and never reads a setting
of its own for a feature. The values come from the environment (and the `.env` file the server
reads it with), read when the server starts:

- `RESUME_KIT_FEATURE_<NAME>=on` or `off` switches a flag for everyone; without it, or empty,
  the flag has its default.
- `RESUME_KIT_FEATURE_<NAME>_ON_FOR=one@example.com, two@example.com` switches it on for
  those accounts only, and `..._OFF_FOR` off for those accounts only, whatever the value for
  everyone is. An account is named by its email address.

A setting that names no flag, a value that is not on or off, and an address in both lists stop
the server from starting: a flag that was meant to be off must not be on by a typing mistake.
Only a go-back start (`RESUME_KIT_FLAGS_IGNORE_UNKNOWN` enabled) ignores unknown flag
settings so the shared settings of a newer release do not prevent a go-back. That switch is
named outside the `RESUME_KIT_FEATURE_` prefix: an image built before it refuses every name
with the prefix that it does not know.
See docs/decisions/0035-every-new-feature-is-behind-a-flag.md.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from resume_kit.contracts.accounts import Account


@dataclass(frozen=True)
class Flag:
    # Lower case words joined by `_`, as `RESUME_KIT_FEATURE_<NAME>` spells it in capitals.
    name: str
    default: bool
    # One line: what the flag switches.
    what: str
    # Whether the web app is told the flag's value (`GET /api/v1/features`). A flag only the
    # server reads is not.
    web: bool = True


# Every flag there is. A new feature adds its line here, off by default until it is ready.
FLAGS: tuple[Flag, ...] = (
    Flag(name="mail_inbox", default=False, what="Receive forwarded job-search email."),
    Flag(
        name="inbox_page", default=False, what="Read forwarded email and link it to applications."
    ),
    Flag("model_gateway", False, "Central paid-model budgets, retries and caching.", web=False),
    Flag(name="local_models", default=False, what="Local CPU embeddings and ranking.", web=False),
    Flag(name="jobs_page", default=False, what="The Jobs page of open jobs that fit the person."),
    Flag(
        name="applied_tracker",
        default=True,
        what="The Applied page: every application, its stages with their dates, and its note.",
    ),
    Flag(
        name="jobs_digest",
        default=False,
        what="The email of new jobs that fit the person, daily or weekly (needs jobs_page).",
    ),
    Flag(
        name="job_ranker_local",
        default=True,
        what="Jobs ranked by the local model service; off, they are ranked by words.",
        web=False,
    ),
    Flag(
        name="job_ranker_jev",
        default=False,
        what="Reserved: Jev ranks jobs. Real profiles are not sent out until the privacy "
        "notice allows it, whatever this says.",
        web=False,
    ),
    Flag(
        name="local_name_detector",
        default=False,
        what="Local detection of names and header places before resume reading.",
        web=False,
    ),
    Flag(
        name="shorten_summary",
        default=False,
        what="Shorten, beside a summary too long to save: a model's shorter version to use or not.",
    ),
    Flag(
        name="write_answers",
        default=False,
        what='Write it with the app, beside a form\'s "why us" question or cover letter: a draft '
        "from the profile and the posting, for a credit (needs assisted_apply).",
    ),
    Flag(
        name="several_resumes",
        default=False,
        what="Several resumes read at once on Import, joined into one reading.",
    ),
    Flag(
        name="assisted_apply",
        default=False,
        what="Apply on the Jobs page: the extension fills the company's form (needs jobs_page).",
    ),
    Flag(
        name="not_relevant",
        default=False,
        what="Not relevant on the Jobs page, with a reason the list learns from (needs jobs_page).",
    ),
    Flag(
        name="json_resume",
        default=False,
        what="A resume in the JSON Resume format (jsonresume.org) is read on Import and the "
        "first run.",
        web=False,
    ),
    Flag(
        name="appearance",
        default=False,
        what="Appearance in the menu: light or dark, colour, greys, corners and motion.",
    ),
    Flag(
        name="extension_by_itself",
        default=False,
        what="The extension's panel takes the posting by itself, and the builder tells of it.",
    ),
    Flag(
        name="page_speed",
        default=False,
        what="How fast each page was in the browser (Core Web Vitals), kept in the visit record.",
    ),
    Flag(
        name="first_steps",
        default=False,
        what="The list of first steps on the Resume builder, Import always on the Career "
        "profile, and empty pages that say how to start.",
    ),
    Flag(
        name="resume_queue",
        default=False,
        what="Several resumes written in the background, a few at once; the builder is free "
        "for the next posting.",
    ),
    Flag(
        name="resume_search",
        default=False,
        what="Search and filter the kept resumes by job, level and state.",
    ),
    # Not told to the web app: the page and its routes are not advertised to anyone. The web app
    # learns that the menu shows Admin from the session, which says so to the admin only.
    Flag(
        name="admin_area",
        default=False,
        what="The admin page: the accounts and the log of what was done there.",
        web=False,
    ),
    Flag(
        name="resume_length",
        default=False,
        what="One page or two pages chosen on the builder, asked of the writer, and said on the "
        "resume when its PDF is longer.",
    ),
    Flag(
        name="pdf_pages",
        default=False,
        what="PDF pages on a resume's page: the resume as its PDF prints it, page by page.",
    ),
    Flag(
        name="server_apply",
        default=False,
        what="Apply on the Jobs page through the app's server, which fills the form and sends it on "
        "the person's approval (needs jobs_page).",
    ),
    Flag(
        name="form_answers",
        default=False,
        what="Answers for application forms on the Profile page, saved from sent forms and "
        "filled in on the next (needs assisted_apply).",
    ),
    Flag(
        name="compact_skills",
        default=True,
        what="Skill groups wrap together in one paragraph: on the resume page, in the PDF and "
        "in the Word file.",
    ),
    Flag(
        name="resume_notes",
        default=False,
        what="Notes on the resume builder for the next resume: experience the profile is "
        "missing and what the employer said matters.",
    ),
)

_PREFIX = "RESUME_KIT_FEATURE_"
_IGNORE_UNKNOWN = "RESUME_KIT_FLAGS_IGNORE_UNKNOWN"
_ON_FOR = "_ON_FOR"
_OFF_FOR = "_OFF_FOR"
_NAME = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
_ON = {"on", "true", "1", "yes"}
_OFF = {"off", "false", "0", "no"}


class FeatureSettingsError(ValueError):
    """The flags' settings cannot be read. It names the setting, never an address."""


class UnknownFlag(KeyError):
    """Code asked for a flag `FLAGS` does not list."""


@dataclass(frozen=True)
class Features:
    """The value of every flag, as the configuration set it."""

    flags: Mapping[str, Flag]
    for_everyone: Mapping[str, bool]
    on_for: Mapping[str, frozenset[str]]
    off_for: Mapping[str, frozenset[str]]

    def on(self, name: str, account: Account | None = None) -> bool:
        """Whether the flag is on: for this account when one is named, otherwise for everyone."""
        if name not in self.flags:
            raise UnknownFlag(name)
        if account is not None:
            email = _address(account.email)
            if email in self.off_for[name]:
                return False
            if email in self.on_for[name]:
                return True
        return self.for_everyone[name]

    def for_web(self, account: Account | None = None) -> dict[str, bool]:
        """The flags the web app is told about, as this account has them. Without one, a flag is
        on only when it is on for every account: on for everyone and off for nobody named."""
        if account is None:
            return {
                flag.name: self.for_everyone[flag.name] and not self.off_for[flag.name]
                for flag in self.flags.values()
                if flag.web
            }
        return {flag.name: self.on(flag.name, account) for flag in self.flags.values() if flag.web}


def _address(email: str) -> str:
    return email.strip().casefold()


def _switch(setting: str, value: str) -> bool:
    word = value.strip().lower()
    if word in _ON:
        return True
    if word in _OFF:
        return False
    raise FeatureSettingsError(f"{setting} must be on or off")


def _accounts(value: str) -> frozenset[str]:
    return frozenset(_address(part) for part in value.split(",") if part.strip())


def read_features(environ: Mapping[str, str], flags: tuple[Flag, ...] | None = None) -> Features:
    """The values of `flags` (by default `FLAGS`) from `environ`. Raises `FeatureSettingsError`
    when they cannot be read."""
    by_name: dict[str, Flag] = {}
    for flag in FLAGS if flags is None else flags:
        if not _NAME.fullmatch(flag.name) or flag.name.endswith(("_on_for", "_off_for")):
            raise FeatureSettingsError(f"{flag.name!r} is not a name a flag may have")
        if flag.name in by_name:
            raise FeatureSettingsError(f"{flag.name!r} is listed twice")
        by_name[flag.name] = flag
    settings = {name: value for name, value in environ.items() if name.startswith(_PREFIX)}
    known = {
        _PREFIX + name.upper() + suffix for name in by_name for suffix in ("", _ON_FOR, _OFF_FOR)
    }
    older_copy = environ.get(_IGNORE_UNKNOWN, "").strip().lower() in _ON
    for setting in settings:
        if setting not in known and not older_copy:
            raise FeatureSettingsError(f"{setting} names no flag")
    for_everyone: dict[str, bool] = {}
    on_for: dict[str, frozenset[str]] = {}
    off_for: dict[str, frozenset[str]] = {}
    for name, flag in by_name.items():
        setting = _PREFIX + name.upper()
        given = settings.get(setting, "").strip()
        # Empty is unset, as `${RESUME_KIT_FEATURE_X:-}` in a compose file writes it.
        for_everyone[name] = _switch(setting, given) if given else flag.default
        on_for[name] = _accounts(settings.get(setting + _ON_FOR, ""))
        off_for[name] = _accounts(settings.get(setting + _OFF_FOR, ""))
        if on_for[name] & off_for[name]:
            raise FeatureSettingsError(
                f"{setting}{_ON_FOR} and {setting}{_OFF_FOR} name the same account"
            )
    return Features(flags=by_name, for_everyone=for_everyone, on_for=on_for, off_for=off_for)


_current: Features | None = None


def use_features(features: Features) -> None:
    """Makes these the values `feature_on` answers from. The server calls it when it starts."""
    global _current
    _current = features


def current_features() -> Features:
    """The values in use. A program that did not set them reads them from its environment."""
    global _current
    if _current is None:
        _current = read_features(os.environ)
    return _current


def feature_on(name: str, account: Account | None = None) -> bool:
    """Whether the feature `name` is on: for `account` when one is named, else for everyone."""
    return current_features().on(name, account)
