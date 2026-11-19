"""`hunt timer`: the morning run at the settings' hour, a systemd user timer on Linux and a
launchd agent on macOS. The files are checked as text; nothing is installed and neither
systemctl nor launchctl runs: the command runner is a stand-in."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from job_hunt.timer import LABEL, UNIT, TimerError, launchd_plist, systemd_units, timer


class Runner:
    def __init__(self, out: str = "") -> None:
        self.out, self.commands = out, []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, self.out, "")


def test_the_systemd_timer_runs_the_morning_at_the_hour(tmp_path: Path) -> None:
    service, timer_unit = systemd_units(tmp_path, 7, "/usr/local/bin/uv", "/usr/local/bin:/usr/bin")

    assert f"WorkingDirectory={tmp_path}" in service
    assert f"ExecStart=/usr/local/bin/uv run --project {tmp_path} hunt morning" in service
    assert "Environment=PATH=/usr/local/bin:/usr/bin" in service
    # The tmux server it starts must outlive the run.
    assert "KillMode=process" in service
    assert "OnCalendar=*-*-* 07:00:00" in timer_unit
    assert "Persistent=true" in timer_unit
    assert f"Unit={UNIT}.service" in timer_unit


def test_the_launchd_agent_runs_the_morning_at_the_hour(tmp_path: Path) -> None:
    agent = plistlib.loads(launchd_plist(tmp_path, 18, "/opt/homebrew/bin/uv", "/opt/homebrew/bin:/usr/bin").encode())

    assert agent["Label"] == LABEL
    assert agent["ProgramArguments"] == ["/opt/homebrew/bin/uv", "run", "--project", str(tmp_path), "hunt", "morning"]
    assert agent["WorkingDirectory"] == str(tmp_path)
    assert agent["StartCalendarInterval"] == {"Hour": 18, "Minute": 0}
    assert agent["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert agent["AbandonProcessGroup"] is True


def test_on_writes_the_linux_units_and_turns_the_timer_on(tmp_path: Path) -> None:
    home, root, runner = tmp_path / "home", tmp_path / "project", Runner()

    said = timer("on", "Linux", home, root, 7, run=runner, uv="/usr/bin/uv", path="/usr/bin")

    units = home / ".config" / "systemd" / "user"
    assert (units / f"{UNIT}.service").read_text() == systemd_units(root, 7, "/usr/bin/uv", "/usr/bin")[0]
    assert (units / f"{UNIT}.timer").exists()
    assert runner.commands == [["systemctl", "--user", "daemon-reload"],
                               ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"]]
    assert "07:00" in said


def test_off_turns_the_linux_timer_off_and_removes_its_units(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Linux", home, root, 7, run=Runner(), uv="/usr/bin/uv", path="/usr/bin")
    runner = Runner()

    timer("off", "Linux", home, root, 7, run=runner, uv="/usr/bin/uv", path="/usr/bin")

    assert runner.commands[0] == ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"]
    assert not list((home / ".config" / "systemd" / "user").iterdir())


def test_on_and_off_load_and_unload_the_macos_agent(tmp_path: Path) -> None:
    home, root, runner = tmp_path / "home", tmp_path / "project", Runner()
    agent = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    timer("on", "Darwin", home, root, 7, run=runner, uv="/opt/homebrew/bin/uv", path="/usr/bin", uid=501)
    assert agent.exists()
    timer("off", "Darwin", home, root, 7, run=runner, uv="/opt/homebrew/bin/uv", path="/usr/bin", uid=501)

    assert not agent.exists()
    assert runner.commands == [["launchctl", "bootout", f"gui/501/{LABEL}"],
                               ["launchctl", "bootstrap", "gui/501", str(agent)],
                               ["launchctl", "bootout", f"gui/501/{LABEL}"]]


def test_the_state_says_on_with_the_hour_or_off(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    assert "off" in timer("status", "Linux", home, root, 7, run=Runner(), uv="/usr/bin/uv", path="/usr/bin")

    timer("on", "Linux", home, root, 9, run=Runner(), uv="/usr/bin/uv", path="/usr/bin")
    said = timer("status", "Linux", home, root, 7, run=Runner("active\n"), uv="/usr/bin/uv", path="/usr/bin")

    assert "on" in said and "09:00" in said
    # The settings' hour moved since: run hunt timer on again.
    assert "hunt timer on" in said


def test_another_system_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TimerError, match="Linux or macOS"):
        timer("on", "Windows", tmp_path, tmp_path, 7, run=Runner(), uv="uv", path="")
