"""`hunt doctor`: one line for each thing the hunt needs, ok or what is missing and how to add it.

Every check looks at a stand-in machine: the commands it runs (`claude auth status`,
`claude mcp list`, `pgrep`, the short `claude --chrome -p` that lists the connected browsers) are
answered by a fake runner, the home and the root are temporary
folders. Nothing real is run or read."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from job_hunt.career import career_from_resume, write_career
from job_hunt.doctor import BROWSER_MODEL, LIST_BROWSERS, NATIVE_HOST, Machine, doctor, remote_desktop_ready

SIGNED_IN = {"loggedIn": True, "authMethod": "claude.ai", "email": "ada@example.com", "subscriptionType": "pro"}
MCP_LIST = """Checking MCP server health...

claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
"""
SETTINGS = {"titles": ["QA Engineer"], "areas": ["New York City"], "remote": "include", "lowest_salary": None,
            "morning_hour": 7}
DRAFT = {"person": {"name": "Ada Example", "email": "ada@example.com"},
         "jobs": [{"title": "QA Engineer", "company": "Acme", "start_date": "2020-01", "end_date": None,
                   "material": ["Wrote the tests."]}]}


HERE = {"Linux": "Linux", "Darwin": "macOS"}
WORK_MAC = {"name": "Browser 1", "system": "macOS", "isLocal": False, "onThisComputer": False}
HOME_MAC = {"name": "Browser 2", "system": "macOS", "isLocal": False, "onThisComputer": False}


def this_computers(system: str = "Linux", name: str = "Browser 3") -> dict:
    return {"name": name, "system": HERE[system], "isLocal": True, "onThisComputer": True}


def listed(*browsers: dict) -> tuple[int, str]:
    """What the listing run prints: the browsers as one JSON list, as its prompt asks."""
    return 0, json.dumps(list(browsers))


class Runner:
    """Answers each command by its first two words; a command it has no answer for is not installed."""

    def __init__(self, **answers: tuple[int, str]) -> None:
        self.answers, self.commands = answers, []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(command)
        answer = self.answers.get("_".join(command[:2]).replace("-", "_"))
        return None if answer is None else subprocess.CompletedProcess(command, answer[0], answer[1], "")


def everything_ready(tmp_path: Path, system: str = "Linux") -> tuple[Machine, Runner]:
    home, root, data = tmp_path / "home", tmp_path / "root", tmp_path / "data"
    host = (home / ".config/google-chrome/NativeMessagingHosts" if system == "Linux"
            else home / "Library/Application Support/Google/Chrome/NativeMessagingHosts")
    host.mkdir(parents=True)
    (host / NATIVE_HOST).write_text("{}")
    if system == "Linux":
        (root / "opt/google/chrome-remote-desktop").mkdir(parents=True)
        (home / ".config/chrome-remote-desktop").mkdir(parents=True)
        (home / ".config/chrome-remote-desktop/host#0f0f.json").write_text("{}")
    else:
        (root / "Library/PrivilegedHelperTools").mkdir(parents=True)
        (root / "Library/PrivilegedHelperTools/org.chromium.chromoting.json").write_text("{}")
    data.mkdir()
    write_career(career_from_resume(DRAFT), data / "career.json")
    (data / "settings.json").write_text(json.dumps(SETTINGS))
    runner = Runner(claude_auth=(0, json.dumps(SIGNED_IN)), claude_mcp=(0, MCP_LIST), pgrep__f=(0, "4242\n"),
                    claude___chrome=listed(this_computers(system)))
    machine = Machine(
        system=system, home=home, root=root, data=data, python=(3, 14), run=runner,
        which=lambda name: f"/usr/bin/{name}" if name in ("claude", "uv") else None,
        timer_status=lambda: "the morning run is on, every day at 07:00",
    )
    return machine, runner


def lines(machine: Machine) -> dict[str, str]:
    """Each line by its first word after the state, as `ok`/`missing`/`optional` and its text."""
    found = {}
    for line in doctor(machine):
        found[line.need] = f"{line.state}: {line.text}"
    return found


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_a_ready_machine_has_every_line_ok(tmp_path: Path, system: str) -> None:
    machine, _ = everything_ready(tmp_path, system)

    said = doctor(machine)

    assert [line.need for line in said] == [
        "claude", "uv", "python", "chrome", "gmail", "career", "settings", "timer", "remote desktop"]
    assert all(line.state == "ok" for line in said), [str(line) for line in said]


def test_the_lines_print_their_state_first_and_never_the_account(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)

    printed = "\n".join(map(str, doctor(machine)))

    assert printed.splitlines()[0].startswith("ok       Claude Code")
    assert "ada@example.com" not in printed


def test_claude_code_not_installed_says_where_to_get_it(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    machine = machine.but(which=lambda name: "/usr/bin/uv" if name == "uv" else None)

    said = lines(machine)

    assert said["claude"].startswith("missing: Claude Code is not installed")
    assert "claude.com/claude-code" in said["claude"]
    # Without claude nothing it knows can be asked.
    assert said["gmail"].startswith("missing:") and said["chrome"].startswith("missing:")


@pytest.mark.parametrize("status", [
    {"loggedIn": False},
    {"loggedIn": True, "authMethod": "api_key"},
])
def test_claude_code_not_signed_in_with_a_subscription_says_how_to_sign_in(tmp_path: Path, status: dict) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude_auth"] = (0, json.dumps(status))

    assert lines(machine)["claude"] == (
        "missing: Claude Code is not signed in with a Claude subscription: run claude auth login")


def test_claude_auth_that_says_nothing_readable_is_not_ok(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude_auth"] = (1, "something went wrong")

    assert lines(machine)["claude"].startswith("missing:")


def test_uv_missing_says_how_to_install_it(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    machine = machine.but(which=lambda name: "/usr/bin/claude" if name == "claude" else None)

    assert "docs.astral.sh/uv" in lines(machine)["uv"]
    assert lines(machine)["uv"].startswith("missing:")


def test_an_older_python_is_named(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)

    assert lines(machine.but(python=(3, 12)))["python"] == (
        "missing: Python 3.14 is needed, this is 3.12: run uv python install 3.14, then uv sync")


def test_chrome_without_the_extension_says_where_to_get_it(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    (machine.home / ".config/google-chrome/NativeMessagingHosts" / NATIVE_HOST).unlink()

    said = lines(machine)["chrome"]

    assert said.startswith("missing: the Claude in Chrome extension is not installed")
    assert "chromewebstore.google.com" in said


def test_chrome_with_the_extension_but_not_connected_says_to_open_it(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["pgrep__f"] = (1, "")

    said = lines(machine)["chrome"]

    assert said.startswith("missing: Claude in Chrome is not connected")
    assert ["pgrep", "-f", "chrome-native-host"] in runner.commands


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_one_browser_on_this_computer_is_ok_and_named(tmp_path: Path, system: str) -> None:
    machine, _ = everything_ready(tmp_path, system)

    said = lines(machine)["chrome"]

    assert said == f"ok: Claude in Chrome, connected in this computer's Chrome (Browser 3, {HERE[system]})"


def test_other_computers_browsers_are_named_and_never_used(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude___chrome"] = listed(WORK_MAC, this_computers(), HOME_MAC)

    said = lines(machine)["chrome"]

    assert said == ("ok: Claude in Chrome, connected in this computer's Chrome (Browser 3, Linux); also connected: "
                    "Browser 1 (macOS), Browser 2 (macOS); workers never use them")


def test_only_other_computers_browsers_is_missing_with_what_to_do(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude___chrome"] = listed(WORK_MAC, HOME_MAC)

    said = lines(machine)["chrome"]

    assert said.startswith("missing: Claude in Chrome is not connected in this computer's Chrome")
    assert "Browser 1 (macOS), Browser 2 (macOS)" in said
    assert "open Chrome on this computer, click the Claude extension" in said
    assert "same Claude account" in said


def test_two_browsers_that_could_be_this_computers_are_missing(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude___chrome"] = listed(this_computers(name="Browser 3"), this_computers(name="Browser 4"))

    said = lines(machine)["chrome"]

    assert said.startswith("missing: more than one connected Chrome could be this computer's")
    assert "Browser 3 (Linux), Browser 4 (Linux)" in said


def test_without_on_this_computer_the_one_local_browser_of_this_system_counts(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    unsure = {"name": "Browser 3", "system": "Linux", "isLocal": True, "onThisComputer": None}
    runner.answers["claude___chrome"] = listed(unsure, {**WORK_MAC, "onThisComputer": None})
    assert lines(machine)["chrome"].startswith("ok: Claude in Chrome, connected in this computer's Chrome (Browser 3")

    # Local by its system alone, two of them: either could be another computer of the same system.
    runner.answers["claude___chrome"] = listed(unsure, {**unsure, "name": "Browser 4"})
    assert lines(machine)["chrome"].startswith("missing: more than one connected Chrome could be")

    # Said to be local, but of another system than this computer's: not this computer.
    runner.answers["claude___chrome"] = listed({**unsure, "system": "macOS"})
    assert lines(machine)["chrome"].startswith("missing: Claude in Chrome is not connected in this computer's")


def test_a_listing_in_a_code_fence_is_read(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    # As Claude Haiku answered in a dry run on made-up browsers.
    runner.answers["claude___chrome"] = (0, "```json\n" + json.dumps([WORK_MAC, this_computers()], indent=2) + "\n```\n")

    assert lines(machine)["chrome"].startswith("ok: Claude in Chrome, connected in this computer's Chrome (Browser 3")


@pytest.mark.parametrize("answer", [None, (1, "error"), (0, "I could not list them."), (0, '{"browsers": 3}')])
def test_a_listing_that_cannot_be_read_is_missing(tmp_path: Path, answer: tuple[int, str] | None) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude___chrome"] = answer

    said = lines(machine)["chrome"]

    assert said.startswith("missing: Claude in Chrome did not say which browsers are connected")


def test_the_listing_runs_on_haiku_allowed_only_the_listing_tool(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)

    lines(machine)

    [command] = [command for command in runner.commands if command[:2] == ["claude", "--chrome"]]
    assert command[command.index("--model") + 1] == BROWSER_MODEL == "claude-haiku-5-5"
    assert command[command.index("--allowedTools") + 1] == LIST_BROWSERS
    assert LIST_BROWSERS == "mcp__claude-in-chrome__list_connected_browsers"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"


def test_the_listing_is_not_run_while_the_native_host_is_not_running(tmp_path: Path) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["pgrep__f"] = (1, "")

    lines(machine)

    assert not [command for command in runner.commands if command[:2] == ["claude", "--chrome"]]


def test_chromium_counts_as_chrome(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    chrome = machine.home / ".config/google-chrome/NativeMessagingHosts"
    chromium = machine.home / ".config/chromium/NativeMessagingHosts"
    chromium.mkdir(parents=True)
    (chrome / NATIVE_HOST).rename(chromium / NATIVE_HOST)

    assert lines(machine)["chrome"].startswith("ok:")


@pytest.mark.parametrize("listing, said", [
    ("claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ! Needs authentication\n",
     "missing: the Gmail connector needs you to sign in to it again"),
    ("claude.ai Google Calendar: https://x - ✔ Connected\n", "missing: the Gmail connector is not on"),
    ("claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✘ Failed to connect\n",
     "missing: the Gmail connector needs you to sign in to it again"),
])
def test_gmail_not_connected_says_where_to_turn_it_on(tmp_path: Path, listing: str, said: str) -> None:
    machine, runner = everything_ready(tmp_path)
    runner.answers["claude_mcp"] = (0, listing)

    line = lines(machine)["gmail"]

    assert line.startswith(said)
    assert "claude.ai/settings/connectors" in line


def test_a_missing_career_and_settings_say_setup_makes_them(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    (machine.data / "career.json").unlink()
    (machine.data / "settings.json").unlink()

    said = lines(machine)

    assert said["career"] == "missing: no data/career.json yet: /setup reads your resume into it"
    assert said["settings"] == "missing: no data/settings.json yet: /setup asks what to look for"


def test_a_career_a_resume_cannot_print_is_named_with_what_to_fix(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    career = json.loads((machine.data / "career.json").read_text())
    career["jobs"][0]["company"] = ""
    (machine.data / "career.json").write_text(json.dumps(career))

    said = lines(machine)["career"]

    assert said.startswith("missing: data/career.json needs a fix:")
    assert "has no employer" in said


def test_a_career_with_an_employer_the_resume_pdf_cannot_print_needs_a_fix(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    career = json.loads((machine.data / "career.json").read_text())
    career["jobs"][0]["company"] = "Acme 🚀"
    (machine.data / "career.json").write_text(json.dumps(career))

    said = lines(machine)["career"]

    assert said.startswith("missing: data/career.json needs a fix:")
    assert "the employer" in said and "cannot print" in said


def test_a_career_with_an_employer_the_resume_contract_refuses_needs_a_fix(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    career = json.loads((machine.data / "career.json").read_text())
    career["jobs"][0]["company"] = "Acme\u0000"
    (machine.data / "career.json").write_text(json.dumps(career))

    said = lines(machine)["career"]

    assert said.startswith("missing: data/career.json needs a fix:")
    assert "the employer" in said and "cannot contain" in said


def test_a_career_that_is_not_json_is_named(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    (machine.data / "career.json").write_text("{")

    assert lines(machine)["career"].startswith("missing: data/career.json needs a fix:")


def test_settings_that_cannot_be_used_say_why(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    (machine.data / "settings.json").write_text(json.dumps({**SETTINGS, "remote": "sometimes"}))

    assert "remote must be one of include, exclude, only" in lines(machine)["settings"]


@pytest.mark.parametrize("status", [
    "the morning run is off (hunt timer on turns it on)",
    "the morning run is written but not running, every day at 07:00; hunt timer on sets it to the settings' "
    "07:00 and turns it on",
])
def test_a_timer_not_running_at_the_settings_hour_is_missing(tmp_path: Path, status: str) -> None:
    machine, _ = everything_ready(tmp_path)

    assert lines(machine.but(timer_status=lambda: status))["timer"] == f"missing: {status}"


def test_a_timer_that_cannot_be_read_is_missing_with_why(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)

    def broken() -> str:
        raise RuntimeError("settings needed first")

    assert lines(machine.but(timer_status=broken))["timer"] == "missing: the morning timer: settings needed first"


def test_remote_desktop_not_set_up_is_optional_and_says_how(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path)
    (machine.home / ".config/chrome-remote-desktop/host#0f0f.json").unlink()

    said = lines(machine)["remote desktop"]

    assert said.startswith("optional: Chrome Remote Desktop is not set up")
    assert "remotedesktop.google.com" in said
    assert not remote_desktop_ready(machine)


def test_remote_desktop_installed_but_never_turned_on_is_not_ready(tmp_path: Path) -> None:
    machine, _ = everything_ready(tmp_path, "Darwin")
    (machine.root / "Library/PrivilegedHelperTools/org.chromium.chromoting.json").unlink()

    assert not remote_desktop_ready(machine)
    assert remote_desktop_ready(everything_ready(tmp_path / "other")[0])


def test_the_command_exits_1_only_when_something_needed_is_missing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli

    machine, runner = everything_ready(tmp_path)
    (machine.home / ".config/chrome-remote-desktop/host#0f0f.json").unlink()
    monkeypatch.setattr(cli, "this_machine", lambda: machine)

    assert cli.main(["doctor"]) == 0
    assert "optional Chrome Remote Desktop" in capsys.readouterr().out
    runner.answers["claude_mcp"] = (0, "")
    assert cli.main(["doctor"]) == 1
    assert "missing  the Gmail connector is not on" in capsys.readouterr().out


def test_hunt_questions_carries_the_link_only_on_a_machine_with_remote_desktop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from job_hunt import cli
    from job_hunt.jobs import FoundJob, JobsList

    machine, _ = everything_ready(tmp_path)
    monkeypatch.setattr(cli, "DATA", machine.data)
    monkeypatch.setattr(cli, "this_machine", lambda: machine)
    jobs = JobsList(machine.data / "jobs.db")
    job = jobs.add(FoundJob(link="https://example.com/jobs/1", title="SDET", company="Example Labs",
                            place="Durham, NC", remote="hybrid", board="linkedin", email_id="1",
                            email_date="2026-11-17T07:00:00+00:00", area="Raleigh-Durham-Chapel Hill"),
                   kind="company_site")
    assert job is not None
    jobs.score(job, 6, "Close enough.")
    for state in ("picked", "resume_ready", "applying"):
        jobs.move(job, state)
    jobs.move(job, "paused", 'a sign-in or a new account, in the Chrome tab "Apply"')

    cli.main(["questions"])
    assert "remotedesktop.google.com/access" in capsys.readouterr().out
    (machine.home / ".config/chrome-remote-desktop/host#0f0f.json").unlink()
    cli.main(["questions"])
    assert "This waits until you are at the computer" in capsys.readouterr().out
