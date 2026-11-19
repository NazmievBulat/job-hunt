"""The guard on the LinkedIn reader's computer tool: a PreToolUse hook that lets through only the
actions that open and read a conversation, never one that types, presses a key or drags, and a
click only on an item of a list.

LinkedIn's conversation list has no links, so the reader opens a conversation by clicking it, and
the computer tool that clicks also types. The reader reads strangers' messages with nobody
watching; this hook, not only the skill's words, keeps a message from making it type, or click
Send, Archive, Delete or a menu: a click must name an element by its ref, and the latest page
read (read_page or find) of the same tab that showed that ref, since the tab's last navigate or
click, must have given it the role `listitem`. A button, a link, a message box or a menu item is
never one. The hook reads the run's transcript for that, a file Claude Code writes before it
calls the hook (`transcript_path`), and only the results of those two tools in it: not the
model's own words.

Claude Code runs it before each call of the tool, with the call as JSON on stdin; exit code 2
refuses the call and tells the model why on stderr. Anything it cannot read is refused too.

    python -I -m job_hunt.click_guard
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

COMPUTER = "mcp__claude-in-chrome__computer"
# What opening and reading a conversation needs: a plain click on its list item, scrolling the
# list or the messages, and looking. Not type, key, right_click, double_click, triple_click,
# left_click_drag or hover, nor any action the tool gains later.
READ_ACTIONS = ("left_click", "scroll", "scroll_to", "screenshot", "zoom", "wait")
# The tools whose results say what a ref is.
PAGE_READERS = ("mcp__claude-in-chrome__read_page", "mcp__claude-in-chrome__find")
NAVIGATE = "mcp__claude-in-chrome__navigate"
BROWSER_CHOICE = ("mcp__claude-in-chrome__select_browser", "mcp__claude-in-chrome__switch_browser")
REF = re.compile(r"\bref_\d+\b")
QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def _strings(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item["text"] for item in content
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def page_reads(transcript: Path, tab: int, pending: object = None) -> list[str]:
    """The text of each read_page and find result of `tab` in the transcript, in order, since the
    last navigate or click in that tab and the last choice of a browser: a ref read before any of
    them may name another element now.
    `pending` is the id of the click being judged, should the transcript hold it already."""
    calls: dict[str, tuple[str, Any]] = {}
    reads: list[str] = []
    for line in transcript.read_text().splitlines():
        entry = json.loads(line) if line.strip() else {}
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if entry.get("type") == "assistant" and item.get("type") == "tool_use":
                name, given = str(item.get("name")), item.get("input")
                given = given if isinstance(given, dict) else {}
                calls[str(item.get("id"))] = (name, given.get("tabId"))
                changes = name == NAVIGATE or (name == COMPUTER and given.get("action") == "left_click")
                if (changes and given.get("tabId") == tab and item.get("id") != pending) or name in BROWSER_CHOICE:
                    # Another browser has tabs and refs of its own.
                    reads.clear()
            elif entry.get("type") == "user" and item.get("type") == "tool_result":
                name, read_tab = calls.get(str(item.get("tool_use_id")), ("", None))
                if name in PAGE_READERS and read_tab == tab:
                    reads.append(_strings(item.get("content")))
    return reads


def role(line: str) -> str | None:
    """The role a read gives an element on its line: its first word once its quoted name and text
    and its refs are taken out. Text a stranger wrote is quoted, so it is never the role."""
    words = re.findall(r"[A-Za-z]+", REF.sub(" ", QUOTED.sub(" ", line)))
    return words[0] if words else None


def is_list_item(ref: str, reads: list[str]) -> bool:
    """Whether the latest read that showed `ref` showed it as a list item: each line whose one ref,
    outside its quoted text, is `ref` gives it the role `listitem`. A ref inside quoted text, or a
    second ref on a line, is text that names another element, as a message could: it says
    nothing of what that ref is."""
    for read in reversed(reads):
        # Quoted text is taken out of the whole read first: a message of several lines is one
        # quoted string, and a line inside it is not a line of the page.
        structure = QUOTED.sub('""', read)
        if '"' in structure.replace('""', ""):
            # Quotes that do not pair up (a read cut short): where its text ends cannot be told,
            # so a ref it names may be anything now, whatever an older read said.
            if ref in REF.findall(read):
                return False
            continue
        lines = [line for line in structure.splitlines() if REF.findall(line) == [ref]]
        if lines:
            return all(role(line) == "listitem" for line in lines)
    return False


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
    if action != "left_click":
        return None
    ref = call.get("ref")
    if not isinstance(ref, str) or not REF.fullmatch(ref) or call.get("coordinate") is not None:
        return "the LinkedIn reader clicks only by ref, never at a place on the screen"
    transcript, tab = event.get("transcript_path"), call.get("tabId")
    if not isinstance(transcript, str) or not isinstance(tab, int) or \
            not is_list_item(ref, page_reads(Path(transcript), tab, event.get("tool_use_id"))):
        return (f"the LinkedIn reader clicks only a conversation's list item: no read_page or find of tab {tab} "
                f"since its last navigate or click showed {ref} as a listitem. Read the list of conversations "
                f"with read_page again and click the ref of the line that says listitem.")
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
