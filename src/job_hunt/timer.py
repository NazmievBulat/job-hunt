"""`hunt timer on|off`, `hunt timer`: `hunt morning` every day at the settings' morning hour.

On Linux a systemd user timer (`~/.config/systemd/user/job-hunt-morning.{service,timer}`), on
macOS a launchd agent (`~/Library/LaunchAgents/com.job-hunt.morning.plist`). Both run the
project's `hunt morning` with `uv`, with the PATH of the shell that turned them on, so `tmux`
and `claude` are found. The session `hunt morning` starts must outlive the run: the service
kills only its own process (`KillMode=process`), and the agent leaves its process group alone.
A missed morning (the computer asleep) runs when it wakes. The hour is written when the timer
is turned on; after the settings' hour changes, `hunt timer on` again.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

UNIT = "job-hunt-morning"
LABEL = "com.job-hunt.morning"

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class TimerError(RuntimeError):
    """The timer could not be turned on or off, and why."""


def _literal(value: object) -> str:
    """A value systemd reads as it is: `%` starts a specifier."""
    return str(value).replace("%", "%%")


def _quoted(value: object) -> str:
    """One word of a systemd command line or setting, spaces and all."""
    return '"' + _literal(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _word(value: object) -> str:
    """One word of `ExecStart`, which also reads `$` as the start of a variable."""
    return _quoted(value).replace("$", "$$")


def systemd_units(root: Path, hour: int, uv: str, path: str) -> tuple[str, str]:
    service = f"""[Unit]
Description=The job hunt's morning run

[Service]
Type=oneshot
WorkingDirectory={_literal(root)}
Environment={_quoted(f"PATH={path}")}
ExecStart={_word(uv)} run --project {_word(root)} hunt morning
KillMode=process
"""
    timer = f"""[Unit]
Description=The job hunt's morning run at {hour:02d}:00

[Timer]
OnCalendar=*-*-* {hour:02d}:00:00
Persistent=true
Unit={UNIT}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def launchd_plist(root: Path, hour: int, uv: str, path: str) -> str:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--project", str(root), "hunt", "morning"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": path},
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        "AbandonProcessGroup": True,
        "StandardOutPath": str(root / "data" / "morning.out"),
        "StandardErrorPath": str(root / "data" / "morning.out"),
    }).decode()


def _units(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _agent(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _must(run: Run, command: list[str]) -> None:
    done = run(command)
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}").splitlines()[-1]
        raise TimerError(f"{' '.join(command)} failed: {said}")


def _written_hour(system: str, home: Path) -> int | None:
    if system == "Linux":
        timer = _units(home) / f"{UNIT}.timer"
        found = re.search(r"^OnCalendar=\*-\*-\* (\d\d):", timer.read_text(), re.M) if timer.exists() else None
        return int(found.group(1)) if found else None
    agent = _agent(home)
    return plistlib.loads(agent.read_bytes())["StartCalendarInterval"]["Hour"] if agent.exists() else None


def timer(
    action: str, system: str, home: Path, root: Path, hour: int, *, run: Run, uv: str, path: str, uid: int = 0
) -> str:
    """Turn the timer on or off, or say its state ("status"); return what to print."""
    if system not in ("Linux", "Darwin"):
        raise TimerError(f"the morning timer runs on Linux or macOS, not {system}")
    if action == "status":
        return _status(system, home, hour, run, uid)
    if system == "Linux":
        units = _units(home)
        if action == "on":
            units.mkdir(parents=True, exist_ok=True)
            service, timer_unit = systemd_units(root, hour, uv, path)
            (units / f"{UNIT}.service").write_text(service)
            (units / f"{UNIT}.timer").write_text(timer_unit)
            _must(run, ["systemctl", "--user", "daemon-reload"])
            _must(run, ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"])
        elif (units / f"{UNIT}.timer").exists():
            # The units stay while systemd refuses: removed, nothing would show it still runs.
            _must(run, ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
            for name in (f"{UNIT}.service", f"{UNIT}.timer"):
                (units / name).unlink(missing_ok=True)
    else:
        agent = _agent(home)
        # An agent loaded before is unloaded first; one that was not is no error.
        unloaded = run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
        said = f"{unloaded.stderr} {unloaded.stdout}"
        if unloaded.returncode != 0 and action == "off" and not any(
                gone in said for gone in ("No such process", "Could not find")):
            raise TimerError(f"launchctl bootout failed: {said.strip().splitlines()[-1] if said.strip() else 'no message'}")
        if action == "on":
            agent.parent.mkdir(parents=True, exist_ok=True)
            agent.write_text(launchd_plist(root, hour, uv, path))
            _must(run, ["launchctl", "bootstrap", f"gui/{uid}", str(agent)])
        else:
            agent.unlink(missing_ok=True)
    return f"the morning run is on, every day at {hour:02d}:00" if action == "on" else "the morning run is off"


def _status(system: str, home: Path, hour: int, run: Run, uid: int) -> str:
    written = _written_hour(system, home)
    if written is None:
        return "the morning run is off (hunt timer on turns it on)"
    if system == "Linux":
        active = run(["systemctl", "--user", "is-active", f"{UNIT}.timer"]).stdout.strip() == "active"
    else:
        active = run(["launchctl", "print", f"gui/{uid}/{LABEL}"]).returncode == 0
    said = f"the morning run is {'on' if active else 'written but not running'}, every day at {written:02d}:00"
    if written != hour or not active:
        said += f"; hunt timer on sets it to the settings' {hour:02d}:00 and turns it on"
    return said
