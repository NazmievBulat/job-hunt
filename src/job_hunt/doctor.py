"""`hunt doctor`: what the hunt needs, one line each, ok or what is missing and how to add it.

The checks call no model but one. Claude Code's sign-in is `claude auth status`; the Gmail
connector is its line in `claude mcp list`; Claude in Chrome is its native messaging host, which
the extension installs, and the `claude --chrome-native-host` process Chrome starts while the
extension is connected, then the browsers the account has connected, listed by one short
`claude --chrome -p` on Haiku allowed only the listing tool: the workers use only this
computer's Chrome, so exactly one of them must be on this computer. When the settings name one
browser by its deviceId (`chrome_browser`), the workers use only that one, on whatever computer
it runs: the native-host checks, which are about this computer's Chrome, are left out, and that
deviceId must be among the connected browsers. Chrome Remote Desktop, for solving a paused application from the phone,
is optional: its host installed and turned on for this computer. The account's email is never
printed.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from job_hunt.career import CareerError, career_from_resume, read_career
from job_hunt.finder import ONLY_NAMED_TOOLS
from job_hunt.settings import SettingsError, read_settings

NATIVE_HOST = "com.anthropic.claude_code_browser_extension.json"
EXTENSION = "https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn"
CONNECTORS = "claude.ai/settings/connectors"
REMOTE_DESKTOP = "https://remotedesktop.google.com/access"
BROWSER_MODEL = "claude-haiku-5-5"
LIST_BROWSERS = "mcp__claude-in-chrome__list_connected_browsers"
# The model only copies the tool's answer: which browser is this computer's is decided below.
LIST_PROMPT = (
    "Call list_connected_browsers once and do nothing else. Reply with only one JSON list, no other "
    'text: one object for each browser it lists, {"deviceId": its deviceId, "name": its display name, '
    '"system": its OS platform, "isLocal": its isLocal, "onThisComputer": its onThisComputer, or null when it '
    'does not give one}, '
    "the values exactly as the tool gave them. With no browser connected, reply []."
)
# How each system names itself in the browser listing, in lower case.
SYSTEM_NAMES = {"Linux": ("linux",), "Darwin": ("mac", "darwin", "os x"), "Windows": ("windows",)}

Run = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]
State = Literal["ok", "missing", "optional"]


@dataclass(frozen=True)
class Machine:
    """What the checks look at. `run` gives None for a command not installed or that hung;
    `run_background` the same for a Claude run, which starts in the project's empty run folder,
    as every background run does, and gives None when that folder holds anything."""

    system: str
    home: Path
    root: Path
    data: Path
    python: tuple[int, int]
    run: Run
    run_background: Run
    which: Callable[[str], str | None]
    timer_status: Callable[[], str]

    def but(self, **changes: object) -> Machine:
        return replace(self, **changes)


@dataclass(frozen=True)
class Line:
    need: str
    state: State
    text: str

    def __str__(self) -> str:
        return f"{self.state:<8} {self.text}"


def _claude(machine: Machine) -> Line:
    if machine.which("claude") is None:
        return Line("claude", "missing", "Claude Code is not installed: get it at https://claude.com/claude-code")
    done = machine.run(["claude", "auth", "status", "--json"])
    try:
        status = json.loads(done.stdout) if done is not None else {}
    except json.JSONDecodeError:
        status = {}
    if isinstance(status, dict) and status.get("loggedIn") is True and status.get("authMethod") == "claude.ai":
        return Line("claude", "ok", "Claude Code, signed in with a Claude subscription")
    return Line("claude", "missing", "Claude Code is not signed in with a Claude subscription: run claude auth login")


def _uv(machine: Machine) -> Line:
    if machine.which("uv") is None:
        return Line("uv", "missing", "uv is not installed: see https://docs.astral.sh/uv/ to install it")
    return Line("uv", "ok", "uv")


def _python(machine: Machine) -> Line:
    if machine.python < (3, 14):
        major, minor = machine.python
        return Line("python", "missing",
                    f"Python 3.14 is needed, this is {major}.{minor}: run uv python install 3.14, then uv sync")
    return Line("python", "ok", "Python 3.14")


def _native_hosts(machine: Machine) -> list[Path]:
    if machine.system == "Darwin":
        support = machine.home / "Library" / "Application Support"
        browsers = [support / "Google" / "Chrome", support / "Chromium"]
    else:
        browsers = [machine.home / ".config" / "google-chrome", machine.home / ".config" / "chromium"]
    return [browser / "NativeMessagingHosts" / NATIVE_HOST for browser in browsers]


def _named_browser(machine: Machine) -> str | None:
    """The settings' chrome_browser; None without one, or with settings that cannot be read (their
    own line says so), and then the workers use only this computer's Chrome."""
    try:
        return read_settings(machine.data / "settings.json").chrome_browser
    except SettingsError:
        return None


def _chrome(machine: Machine) -> Line:
    if machine.which("claude") is None:
        return Line("chrome", "missing", "Claude in Chrome needs Claude Code first")
    if (named := _named_browser(machine)) is not None:
        return _chrome_named(machine, named)
    if not any(host.exists() for host in _native_hosts(machine)):
        return Line("chrome", "missing", "the Claude in Chrome extension is not installed: add it to Chrome from "
                    f"{EXTENSION}, sign it in to your Claude account, then run claude --chrome once")
    running = machine.run(["pgrep", "-f", "chrome-native-host"])
    if running is None or running.returncode != 0:
        return Line("chrome", "missing", "Claude in Chrome is not connected: open Chrome and click the Claude "
                    "extension once, signed in to the same Claude account")
    browsers = _connected_browsers(machine)
    if browsers is None:
        return Line("chrome", "missing", "Claude in Chrome did not say which browsers are connected: open Chrome on "
                    "this computer, click the Claude extension once, then run hunt doctor again")
    here = [browser for browser in browsers if _could_be_here(browser, machine.system)]
    others = ", ".join(_named(browser) for browser in browsers if browser not in here)
    if not here:
        return Line("chrome", "missing", "Claude in Chrome is not connected in this computer's Chrome"
                    + (f" (connected: {others}; workers never use them)" if others else "")
                    + ": open Chrome on this computer, click the Claude extension and sign it in to the same "
                    "Claude account"
                    + ("; or, to use one of those instead, hunt chrome-browser <its deviceId> ("
                       + ", ".join(_named(browser, ids=True) for browser in browsers) + ")" if browsers else ""))
    if len(here) > 1:
        return Line("chrome", "missing", "more than one connected Chrome could be this computer's ("
                    + ", ".join(map(_named, here)) + "), so the workers use none: keep the Claude extension "
                    "connected in one Chrome on this computer and sign it out in the others")
    name, system = here[0]["name"], here[0]["system"]
    return Line("chrome", "ok", f"Claude in Chrome, connected in this computer's Chrome ({name}, {system})"
                + (f"; also connected: {others}; workers never use them" if others else ""))


def _chrome_named(machine: Machine, named: str) -> Line:
    """The one browser the settings name, by its deviceId: connected, or what to do."""
    setting = f"data/settings.json's chrome_browser ({named})"
    browsers = _connected_browsers(machine)
    if browsers is None:
        return Line("chrome", "missing", "Claude in Chrome did not say which browsers are connected: open Chrome "
                    f"with the browser {setting} names, click the Claude extension once, then run hunt doctor again")
    chosen = [browser for browser in browsers if browser.get("deviceId") == named]
    others = ", ".join(_named(browser, ids=not chosen) for browser in browsers if browser not in chosen)
    if not chosen:
        return Line("chrome", "missing", f"the browser {setting} names is not connected"
                    + (f" (connected: {others}; workers never use them)" if others else "")
                    + ": open Chrome where it runs, click the Claude extension and sign it in to the same Claude "
                    "account; or name another with hunt chrome-browser <its deviceId>, or hunt chrome-browser off "
                    "for this computer's Chrome")
    name, system = chosen[0]["name"], chosen[0]["system"]
    return Line("chrome", "ok", f"Claude in Chrome, connected in the browser {setting} names ({name}, {system}); "
                "workers use only it" + (f"; also connected: {others}; workers never use them" if others else ""))


def _connected_browsers(machine: Machine) -> list[dict] | None:
    """The account's connected browsers, or None when the listing cannot be read."""
    done = machine.run_background(["claude", "--chrome", "-p", LIST_PROMPT, "--model", BROWSER_MODEL, *ONLY_NAMED_TOOLS,
                        "--allowedTools", LIST_BROWSERS])
    if done is None or done.returncode != 0:
        return None
    text = done.stdout.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        browsers = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(browsers, list) or not all(
            isinstance(browser, dict) and isinstance(browser.get("name"), str)
            and isinstance(browser.get("system"), str)
            and isinstance(browser.get("deviceId"), str | None) for browser in browsers):
        return None
    return browsers


def _could_be_here(browser: dict, system: str) -> bool:
    """On this computer by the tool's word; without that word, local by the tool's weaker hint and of
    this computer's system. Two that could be leave the workers none."""
    if browser.get("onThisComputer") is not None:
        return browser["onThisComputer"] is True
    names = SYSTEM_NAMES.get(system, (system.lower(),))
    return browser.get("isLocal") is True and any(name in browser["system"].lower() for name in names)


def _named(browser: dict, *, ids: bool = False) -> str:
    """Its name and system; with `ids`, its deviceId too, for hunt chrome-browser."""
    if ids and browser.get("deviceId"):
        return f"{browser['name']} ({browser['system']}, deviceId {browser['deviceId']})"
    return f"{browser['name']} ({browser['system']})"


def _gmail(machine: Machine) -> Line:
    if machine.which("claude") is None:
        return Line("gmail", "missing", "the Gmail connector needs Claude Code first")
    listed = machine.run(["claude", "mcp", "list"])
    found = re.search(r"^claude\.ai Gmail:.*$", listed.stdout if listed is not None else "", re.M)
    if found is None:
        return Line("gmail", "missing", f"the Gmail connector is not on: turn it on at {CONNECTORS}")
    if "Connected" not in found.group(0):
        return Line("gmail", "missing", f"the Gmail connector needs you to sign in to it again, at {CONNECTORS}")
    return Line("gmail", "ok", "the Gmail connector, connected")


def check_career(path: Path) -> None:
    """Raise CareerError when the saved career cannot make a resume: the same check a career read
    from a resume passes before it is saved."""
    try:
        career = read_career(path)
        draft = asdict(career)
        draft.pop("form_answers")
        for job in draft["jobs"]:
            job.pop("id")
        career_from_resume(draft)
    except CareerError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise CareerError(f"{type(error).__name__}: {error}") from None


def _career(machine: Machine) -> Line:
    path = machine.data / "career.json"
    if not path.exists():
        return Line("career", "missing", "no data/career.json yet: /setup reads your resume into it")
    try:
        check_career(path)
    except CareerError as error:
        return Line("career", "missing", f"data/career.json needs a fix: {error}")
    return Line("career", "ok", "data/career.json")


def _settings(machine: Machine) -> Line:
    if not (machine.data / "settings.json").exists():
        return Line("settings", "missing", "no data/settings.json yet: /setup asks what to look for")
    try:
        read_settings(machine.data / "settings.json")
    except SettingsError as error:
        return Line("settings", "missing", f"data/settings.json: {error}")
    return Line("settings", "ok", "data/settings.json")


def _timer(machine: Machine) -> Line:
    try:
        said = machine.timer_status()
    except Exception as error:
        return Line("timer", "missing", f"the morning timer: {error}")
    # Off, written but not running, or at another hour than the settings' all add "hunt timer on".
    if said.startswith("the morning run is on") and "hunt timer on" not in said:
        return Line("timer", "ok", said)
    return Line("timer", "missing", said)


def remote_desktop_ready(machine: Machine) -> bool:
    """Chrome Remote Desktop's host is installed and turned on for this computer."""
    if machine.system == "Darwin":
        return (machine.root / "Library" / "PrivilegedHelperTools" / "org.chromium.chromoting.json").exists()
    installed = (machine.root / "opt" / "google" / "chrome-remote-desktop").exists()
    hosts = machine.home / ".config" / "chrome-remote-desktop"
    return installed and hosts.is_dir() and any(hosts.glob("host#*.json"))


def _remote_desktop(machine: Machine) -> Line:
    if remote_desktop_ready(machine):
        return Line("remote desktop", "ok", "Chrome Remote Desktop, to solve a paused application from your phone")
    return Line("remote desktop", "optional",
                "Chrome Remote Desktop is not set up, so a paused application (a CAPTCHA, a sign-in) waits until "
                f"you are at the computer: to solve it from your phone, open {REMOTE_DESKTOP} on this computer, "
                "turn on remote access, and install the Chrome Remote Desktop app on your phone")


CHECKS = (_claude, _uv, _python, _chrome, _gmail, _career, _settings, _timer, _remote_desktop)


def doctor(machine: Machine) -> list[Line]:
    return [check(machine) for check in CHECKS]
