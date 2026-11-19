"""`hunt doctor`: what the hunt needs, one line each, ok or what is missing and how to add it.

The checks call no model. Claude Code's sign-in is `claude auth status`; the Gmail connector is
its line in `claude mcp list`; Claude in Chrome is its native messaging host, which the
extension installs, and the `claude --chrome-native-host` process Chrome starts while the
extension is connected. Chrome Remote Desktop, for solving a paused application from the phone,
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
from job_hunt.settings import SettingsError, read_settings

NATIVE_HOST = "com.anthropic.claude_code_browser_extension.json"
EXTENSION = "https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn"
CONNECTORS = "claude.ai/settings/connectors"
REMOTE_DESKTOP = "https://remotedesktop.google.com/access"

Run = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]
State = Literal["ok", "missing", "optional"]


@dataclass(frozen=True)
class Machine:
    """What the checks look at. `run` gives None for a command not installed or that hung."""

    system: str
    home: Path
    root: Path
    data: Path
    python: tuple[int, int]
    run: Run
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


def _chrome(machine: Machine) -> Line:
    if machine.which("claude") is None:
        return Line("chrome", "missing", "Claude in Chrome needs Claude Code first")
    if not any(host.exists() for host in _native_hosts(machine)):
        return Line("chrome", "missing", "the Claude in Chrome extension is not installed: add it to Chrome from "
                    f"{EXTENSION}, sign it in to your Claude account, then run claude --chrome once")
    running = machine.run(["pgrep", "-f", "chrome-native-host"])
    if running is None or running.returncode != 0:
        return Line("chrome", "missing", "Claude in Chrome is not connected: open Chrome and click the Claude "
                    "extension once, signed in to the same Claude account")
    return Line("chrome", "ok", "Claude in Chrome, connected")


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
