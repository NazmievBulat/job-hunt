"""The LinkedIn reader opens conversations by clicking them, as LinkedIn's list of conversations
has no links, and reads each down to its last message. Its computer tool goes through a hook that
lets it only click, scroll and look: a stranger's message cannot make it type, press a key or send.

No model and no browser: the hook is run as Claude Code runs it, a process with the call on stdin.
"""

from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunt.click_guard import COMPUTER, READ_ACTIONS, main, refusal
from job_hunt.replies import READ_GUARD, read_command

SKILL = Path(__file__).parents[1] / ".claude" / "skills" / "read-linkedin-messages" / "SKILL.md"
REFUSED = ("type", "key", "right_click", "double_click", "triple_click", "left_click_drag", "hover", "drag", None)


def _call(action: str | None, **more: object) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": COMPUTER,
            "tool_input": {"action": action, "tabId": 7, **more}}


def _hook(event: object, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Runs the hook command the reader's settings name, from an empty folder, as Claude Code does."""
    command = READ_GUARD["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    stdin = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(shlex.split(command), input=stdin, cwd=tmp_path, capture_output=True, text=True, timeout=30)


# The guard


@pytest.mark.parametrize("action", READ_ACTIONS)
def test_the_guard_lets_through_clicking_scrolling_and_looking(action: str) -> None:
    assert refusal(_call(action, ref="ref_12")) is None


@pytest.mark.parametrize("action", REFUSED)
def test_the_guard_refuses_typing_keys_and_every_other_action(action: str | None) -> None:
    why = refusal(_call(action, text="Yes, I am interested"))
    assert why is not None and "never types" in why


def test_the_guard_refuses_a_click_with_modifiers_or_text() -> None:
    assert refusal(_call("left_click", ref="ref_3", modifiers="ctrl"))
    assert refusal(_call("left_click", ref="ref_3", text="hello"))
    assert refusal(_call("left_click", coordinate=[120, 340])) is None


@pytest.mark.parametrize("event", [
    None, [], "left_click", {"tool_name": COMPUTER}, {"tool_name": COMPUTER, "tool_input": "left_click"},
    {"tool_name": "mcp__claude-in-chrome__javascript_tool", "tool_input": {"action": "left_click"}},
])
def test_the_guard_refuses_what_it_cannot_read(event: object) -> None:
    assert refusal(event)


def test_the_guard_refuses_by_exit_code_2_and_says_why() -> None:
    # Claude Code refuses a call on exit code 2 only; any other failure would let it run.
    stderr = io.StringIO()
    assert main(io.StringIO(json.dumps(_call("type", text="hi"))), stderr) == 2
    assert "'type'" in stderr.getvalue()
    assert main(io.StringIO("not json"), io.StringIO()) == 2
    assert main(io.StringIO(json.dumps(_call("scroll"))), io.StringIO()) == 0


def test_the_hook_the_reader_is_given_runs_and_refuses_typing(tmp_path: Path) -> None:
    allowed = _hook(_call("left_click", ref="ref_4"), tmp_path)
    assert (allowed.returncode, allowed.stderr) == (0, "")
    for event in (_call("type"), _call("key"), _call("type", text="Sure, call me"), _call("key", text="Enter"), "{"):
        refused = _hook(event, tmp_path)
        assert refused.returncode == 2 and refused.stderr


def test_the_reader_runs_with_the_guard_on_its_computer_tool(tmp_path: Path) -> None:
    command = read_command(datetime(2026, 11, 18, tzinfo=UTC), tmp_path / "out.json")
    assert json.loads(command[command.index("--settings") + 1]) == READ_GUARD
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert COMPUTER in tools
    assert READ_GUARD["hooks"]["PreToolUse"][0]["matcher"] == COMPUTER


# The skill


def _read_section() -> str:
    return SKILL.read_text().split("## Read", 1)[1].split("## Write", 1)[0]


def test_the_skill_names_exactly_the_actions_the_guard_lets_through() -> None:
    text = _read_section()
    allowed = text.split("only these actions:", 1)[1].split("Any other action", 1)[0]
    assert tuple(re.findall(r"`(\w+)`", allowed)) == READ_ACTIONS
    assert "`type` and `key` among them, is refused" in text


def test_the_skill_opens_conversations_by_a_click_and_forbids_typing_sending_and_menus() -> None:
    text = " ".join(_read_section().split())
    assert "no tool that clicks" not in SKILL.read_text()
    assert "Click the item with left_click and its ref" in text
    assert "Never click a button inside the item" in text
    assert "Click nothing but a conversation in the list. Never type, never press a key" in text
    for forbidden in ("Send", "Accept", "Archive", "Delete", "Mute"):
        assert forbidden in text.split("Click nothing but a conversation in the list.", 1)[1]
    assert "javascript" not in SKILL.read_text().lower()


def test_the_skill_reads_each_conversation_down_to_its_last_message() -> None:
    text = " ".join(_read_section().split())
    assert "Read the whole conversation, down to its last message" in text
    assert "read that message's own element with read_page and its ref_id" in text
    assert "take a screenshot, and compare" in text
    assert "When the person wrote the last message, they have answered already" in text
    # A conversation it could not read is not left out quietly: the whole read is tried again.
    write = " ".join(SKILL.read_text().split("## Write", 1)[1].split())
    assert "could not open a conversation step 2 found or read it down to its last message" in write
