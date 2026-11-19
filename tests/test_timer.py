"""`hunt timer`: the morning run at the settings' hour, a systemd user timer on Linux and a
launchd agent on macOS. The files are checked as text; nothing is installed and neither
systemctl nor launchctl runs: the command runner is a stand-in."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from job_hunt.timer import (
    LABEL, UNIT, WATCH_LABEL, WATCH_UNIT, TimerError, launchd_plist, systemd_units, timer, watch_plist, watch_units,
)


class Runner:
    def __init__(self, out: str = "") -> None:
        self.out, self.commands = out, []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, self.out, "")


def test_the_systemd_timer_runs_the_morning_at_the_hour(tmp_path: Path) -> None:
    service, timer_unit = systemd_units(tmp_path, 7, "/usr/local/bin/uv", "/usr/local/bin:/usr/bin")

    assert f"WorkingDirectory={tmp_path}" in service
    assert f'ExecStart="/usr/local/bin/uv" run --project "{tmp_path}" hunt morning' in service
    assert 'Environment="PATH=/usr/local/bin:/usr/bin"' in service
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


class Failing(Runner):
    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        failed = command[:3] == ["systemctl", "--user", "enable"]
        return subprocess.CompletedProcess(command, 1 if failed else 0, "", "Failed to connect to bus" if failed else "")


def test_a_timer_that_systemd_refuses_is_not_called_on(tmp_path: Path) -> None:
    with pytest.raises(TimerError, match="Failed to connect to bus"):
        timer("on", "Linux", tmp_path, tmp_path, 7, run=Failing(), uv="/usr/bin/uv", path="/usr/bin")


def test_the_macos_state_reads_the_agents_hour(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Darwin", home, root, 6, run=Runner(), uv="uv", path="/usr/bin", uid=501)

    loaded = timer("status", "Darwin", home, root, 6, run=Runner(), uv="uv", path="/usr/bin", uid=501)

    class Unloaded(Runner):
        def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 113, "", "Could not find service")

    unloaded = timer("status", "Darwin", home, root, 6, run=Unloaded(), uv="uv", path="/usr/bin", uid=501)
    assert loaded == "the morning run is on, every day at 06:00"
    assert "written but not running" in unloaded and "hunt timer on" in unloaded


def test_hunt_timer_uses_the_settings_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from job_hunt import cli

    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({
        "titles": ["QA Automation Engineer"], "areas": ["New York City"], "remote": "include",
        "lowest_salary": None, "morning_hour": 8}))
    runner = Runner()
    monkeypatch.setattr(cli, "DATA", data)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli, "run_command", runner)

    assert cli.main(["timer", "on"]) == 0

    assert "OnCalendar=*-*-* 08:00:00" in (tmp_path / "home" / ".config" / "systemd" / "user" / f"{UNIT}.timer").read_text()
    assert "08:00" in capsys.readouterr().out

    def missing(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise TimerError("systemctl is not installed or not on the PATH")

    monkeypatch.setattr(cli, "run_command", missing)
    assert cli.main(["timer", "off"]) == 1
    assert "hunt timer: systemctl is not installed" in capsys.readouterr().err


def test_paths_with_spaces_and_percent_signs_are_quoted(tmp_path: Path) -> None:
    root = tmp_path / "Job hunt 100%"
    service, _ = systemd_units(root, 7, "/opt/my tools/uv", "/opt/my tools:/usr/bin")

    assert f'WorkingDirectory={str(root).replace("%", "%%")}' in service
    assert f'ExecStart="/opt/my tools/uv" run --project "{str(root).replace("%", "%%")}" hunt morning' in service
    assert 'Environment="PATH=/opt/my tools:/usr/bin"' in service


def test_a_dollar_sign_in_a_path_is_not_read_as_a_variable(tmp_path: Path) -> None:
    root = tmp_path / "jobs-${ARCH}"
    service, _ = systemd_units(root, 7, "/opt/$tools/uv", "/usr/bin")

    assert f'ExecStart="/opt/$$tools/uv" run --project "{str(root).replace("$", "$$")}" hunt morning' in service


class Refusing(Runner):
    """A scheduler that refuses to stop the timer."""

    def __init__(self, said: str) -> None:
        super().__init__()
        self.said = said

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        refused = command[:3] == ["systemctl", "--user", "disable"] or command[1] == "bootout"
        return subprocess.CompletedProcess(command, 5 if refused else 0, "", self.said if refused else "")


def test_a_timer_systemd_will_not_stop_is_not_called_off(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Linux", home, root, 7, run=Runner(), uv="uv", path="/usr/bin")

    with pytest.raises(TimerError, match="Access denied"):
        timer("off", "Linux", home, root, 7, run=Refusing("Access denied"), uv="uv", path="/usr/bin")

    assert (home / ".config" / "systemd" / "user" / f"{UNIT}.timer").exists()


def test_off_when_already_off_runs_nothing(tmp_path: Path) -> None:
    runner = Runner()

    said = timer("off", "Linux", tmp_path, tmp_path, 7, run=runner, uv="uv", path="/usr/bin")

    assert runner.commands == [] and "off" in said


def test_a_macos_agent_launchd_will_not_unload_is_not_called_off(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Darwin", home, root, 7, run=Runner(), uv="uv", path="/usr/bin", uid=501)

    with pytest.raises(TimerError, match="Operation not permitted"):
        timer("off", "Darwin", home, root, 7, run=Refusing("Operation not permitted"), uv="uv", path="/usr/bin",
              uid=501)
    assert (home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists()


def test_a_macos_agent_not_loaded_is_still_turned_off(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Darwin", home, root, 7, run=Runner(), uv="uv", path="/usr/bin", uid=501)

    timer("off", "Darwin", home, root, 7, run=Refusing("Boot-out failed: 3: No such process"), uv="uv",
          path="/usr/bin", uid=501)

    assert not (home / "Library" / "LaunchAgents" / f"{LABEL}.plist").exists()


def test_hunt_timer_off_needs_no_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_hunt import cli

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text("{broken")
    monkeypatch.setattr(cli, "DATA", tmp_path / "data")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli, "run_command", Runner())

    assert cli.main(["timer", "off"]) == 0


# The watch for recruiters' messages, every 20 minutes from the morning hour to the summary hour


def test_the_watch_runs_every_20_minutes_from_the_morning_to_the_summary_hour(tmp_path: Path) -> None:
    service, timer_unit = watch_units(tmp_path, 7, 18, "/usr/bin/uv", "/usr/bin")

    assert f'ExecStart="/usr/bin/uv" run --project "{tmp_path}" hunt watch' in service
    assert "KillMode=process" in service
    # 07:00, 07:20, ... 17:40; at 18:00 the day's summary is due and the watch stops.
    assert "OnCalendar=*-*-* 07..17:00/20:00" in timer_unit
    assert f"Unit={WATCH_UNIT}.service" in timer_unit


def test_the_watch_agent_runs_every_20_minutes_in_the_hours(tmp_path: Path) -> None:
    agent = plistlib.loads(watch_plist(tmp_path, 7, 9, "uv", "/usr/bin").encode())

    assert agent["Label"] == WATCH_LABEL
    assert agent["ProgramArguments"][-2:] == ["hunt", "watch"]
    assert agent["StartCalendarInterval"] == [{"Hour": hour, "Minute": minute}
                                              for hour in (7, 8) for minute in (0, 20, 40)]


def test_on_with_the_summary_hour_turns_the_watch_on_next_to_the_morning(tmp_path: Path) -> None:
    home, root, runner = tmp_path / "home", tmp_path / "project", Runner()

    said = timer("on", "Linux", home, root, 7, run=runner, uv="uv", path="/usr/bin", watch_until=18)

    assert (home / ".config" / "systemd" / "user" / f"{WATCH_UNIT}.timer").exists()
    assert ["systemctl", "--user", "enable", "--now", f"{WATCH_UNIT}.timer"] in runner.commands
    assert "every 20 minutes" in said
    runner = Runner()
    timer("off", "Linux", home, root, 7, run=runner, uv="uv", path="/usr/bin")
    assert ["systemctl", "--user", "disable", "--now", f"{WATCH_UNIT}.timer"] in runner.commands
    assert not list((home / ".config" / "systemd" / "user").iterdir())


def test_on_and_off_load_and_unload_the_macos_watch_agent(tmp_path: Path) -> None:
    home, root, runner = tmp_path / "home", tmp_path / "project", Runner()
    agent = home / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"

    timer("on", "Darwin", home, root, 7, run=runner, uv="uv", path="/usr/bin", uid=501, watch_until=18)
    assert agent.exists() and ["launchctl", "bootstrap", "gui/501", str(agent)] in runner.commands
    timer("off", "Darwin", home, root, 7, run=runner, uv="uv", path="/usr/bin", uid=501)

    assert not agent.exists()


def test_a_macos_watch_agent_launchd_will_not_unload_is_not_called_off(tmp_path: Path) -> None:
    home, root = tmp_path / "home", tmp_path / "project"
    timer("on", "Darwin", home, root, 7, run=Runner(), uv="uv", path="/usr/bin", uid=501, watch_until=18)

    class RefusingWatch(Runner):
        def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
            self.commands.append(command)
            refused = command[1] == "bootout" and command[2].endswith(WATCH_LABEL)
            return subprocess.CompletedProcess(command, 5 if refused else 0, "", "Operation not permitted" if refused else "")

    with pytest.raises(TimerError, match="Operation not permitted"):
        timer("off", "Darwin", home, root, 7, run=RefusingWatch(), uv="uv", path="/usr/bin", uid=501)
    assert (home / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist").exists()
