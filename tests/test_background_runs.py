"""Every background `claude -p` that `hunt` builds may use only the tools its command names."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from job_hunt import cli
from job_hunt.finder import FinderError, run_claude

ROOT = Path(__file__).parent.parent


def _background_commands() -> list[tuple[str, int, ast.List]]:
    found = []
    for path in sorted((ROOT / "src" / "job_hunt").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant)
                    and node.elts[0].value == "claude"
                    and any(isinstance(each, ast.Constant) and each.value == "-p" for each in node.elts)):
                found.append((path.name, node.lineno, node))
    return found


def test_a_new_background_command_cannot_leave_out_the_only_named_tools_flags() -> None:
    commands = _background_commands()
    # The twelve of today: a builder written another way would hide from this test.
    assert len(commands) >= 12
    missing = [f"{name}:{line}" for name, line, node in commands
               if not any(isinstance(each, ast.Starred) and isinstance(each.value, ast.Name)
                          and each.value.id == "ONLY_NAMED_TOOLS" for each in node.elts)]
    assert missing == []


def test_the_project_settings_a_background_run_still_loads_open_no_tool() -> None:
    # `--setting-sources project` keeps this one file: an allow rule or a mode in it would reach
    # every background run.
    settings = ROOT / ".claude" / "settings.json"
    if settings.exists():
        permissions = json.loads(settings.read_text()).get("permissions", {})
        assert not permissions.get("allow") and "defaultMode" not in permissions


def test_a_background_run_starts_in_an_empty_folder_of_the_project(tmp_path: Path) -> None:
    # Claude Code lets a run read its working folder without a rule: the project's own, with the
    # career and every job, would be open to each run.
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    seen = tmp_path / "seen.json"
    look = f"import json, os; json.dump([os.getcwd(), os.listdir()], open({str(seen)!r}, 'w'))"
    run_claude([sys.executable, "-c", look], project, skill="look")
    folder, listed = json.loads(seen.read_text())
    assert Path(folder) == project / ".background"
    assert listed == []
    # Ignored, so nothing a run leaves there is committed.
    assert ".background/" in (ROOT / ".gitignore").read_text().splitlines()


def test_a_run_folder_that_holds_a_file_starts_no_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".background").mkdir(parents=True)
    (project / ".background" / "career.json").write_text("{}")
    started = tmp_path / "started"
    with pytest.raises(FinderError, match="holds career.json, and it must be empty"):
        run_claude([sys.executable, "-c", f"open({str(started)!r}, 'w')"], project, skill="look")
    assert not started.exists()


def test_only_run_claude_starts_a_program_in_a_working_folder_of_its_choosing() -> None:
    # Any other process started with `cwd=` could be a background run started in the project.
    found = []
    for path in sorted((ROOT / "src" / "job_hunt").glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found += [(path.name, function.name) for node in ast.walk(function)
                          if isinstance(node, ast.Call) and any(word.arg == "cwd" for word in node.keywords)]
    assert found == [("finder.py", "run_claude")]


WRAPPERS = {
    "run_skill": "read-job-alerts", "run_scoring": "score-jobs", "run_resume": "write-resume",
    "run_site": "apply-site", "run_linkedin": "apply-linkedin", "run_note_draft": "draft-note",
    "run_note_send": "send-note", "run_check": "read-job-alerts", "run_draft": "draft-reply",
    "run_read": "read-linkedin-messages", "run_send": "send-reply", "run_browsers": "browser listing",
}


def test_every_run_of_hunt_names_a_wrapper_here() -> None:
    # A new `run_*` wrapper must be added to WRAPPERS, so the next test reaches it.
    tree = ast.parse((ROOT / "src" / "job_hunt" / "cli.py").read_text())
    named = {node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("run_")}
    assert named - {"run_command"} == set(WRAPPERS)


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_each_run_of_hunt_starts_through_run_claude_in_the_project(
    wrapper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    monkeypatch.setattr(cli, "run_claude", lambda command, root, **named: called.append((command, root, named)))
    command = ["claude", "-p", f"/{WRAPPERS[wrapper]} {{}}"]
    getattr(cli, wrapper)(command)
    [(given, root, named)] = called
    assert given == command and root == cli.ROOT
    assert named.get("skill", "read-job-alerts") == WRAPPERS[wrapper]
