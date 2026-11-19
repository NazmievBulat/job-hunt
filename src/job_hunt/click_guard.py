"""The guard on the LinkedIn reader's computer tool: a PreToolUse hook that lets through only the
actions that open and read a conversation, never one that types, presses a key or drags.

LinkedIn's conversation list has no links, so the reader opens a conversation by clicking it, and
the computer tool that clicks also types. The reader reads strangers' messages with nobody
watching; this hook, not only the skill's words, keeps a message from making it type. Claude Code
runs it before each call of the tool, with the call as JSON on stdin; exit code 2 refuses the
call and tells the model why on stderr. Anything it cannot read is refused too.

    python -I -m job_hunt.click_guard
"""

import json
import sys
from typing import Any, TextIO

COMPUTER = "mcp__claude-in-chrome__computer"
# What opening and reading a conversation needs: a plain click on its list item, scrolling the
# list or the messages, and looking. Not type, key, right_click, double_click, triple_click,
# left_click_drag or hover, nor any action the tool gains later.
READ_ACTIONS = ("left_click", "scroll", "scroll_to", "screenshot", "zoom", "wait")


def refusal(event: Any) -> str | None:
    """Why the call must not run, or None when it may."""
    if not isinstance(event, dict) or event.get("tool_name") != COMPUTER:
        return f"the LinkedIn reader's guard allows only {COMPUTER} calls it can read"
    call = event.get("tool_input")
    action = call.get("action") if isinstance(call, dict) else None
    if action not in READ_ACTIONS:
        return (f"the LinkedIn reader may not use the computer tool's {action!r}: only "
                f"{', '.join(READ_ACTIONS)}. It never types, presses a key or sends anything.")
    if call.get("modifiers"):
        return "the LinkedIn reader clicks without modifier keys"
    if call.get("text"):
        return "the LinkedIn reader types nothing"
    return None


def main(stdin: TextIO = sys.stdin, stderr: TextIO = sys.stderr) -> int:
    # Any failure refuses: exit code 1 would let the call run.
    try:
        why = refusal(json.loads(stdin.read()))
    except Exception as error:
        why = f"the LinkedIn reader's guard could not read the call ({type(error).__name__})"
    if why is None:
        return 0
    print(why, file=stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
