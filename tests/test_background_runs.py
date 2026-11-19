"""Every background `claude -p` that `hunt` builds may use only the tools its command names."""

from __future__ import annotations

import ast
import json
from pathlib import Path

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
