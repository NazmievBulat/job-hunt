import json

PATHS = ("career", "out", "report", "resume", "posting", "message", "folder")


def assert_only_named_tools(command: list[str]) -> None:
    """A background run loads no user or local settings, so a tool the person's own Claude Code
    allows everywhere (Bash, a plugin's browser) is not allowed to it, and runs in `dontAsk`, so
    a tool its `--allowedTools` does not name is refused rather than asked about. WebFetch, which
    `dontAsk` lets reach preapproved sites, is taken away with the subagent that could call it.
    The run starts in an empty folder, so every path its file rules and its arguments name is
    absolute: a relative one would name a file of that empty folder."""
    assert command[command.index("--setting-sources") + 1] == "project"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--disallowedTools") + 1].split(",") == ["WebFetch", "WebSearch", "Agent"]
    rules = command[command.index("--allowedTools") + 1].split(",")
    paths = [rule[rule.index("(") + 1:-1] for rule in rules if rule.startswith(("Read(", "Edit("))]
    assert paths and all(path.startswith("//") for path in paths)
    prompt = command[command.index("-p") + 1]
    arguments = json.loads(prompt[prompt.index(" ") + 1:])
    relative = [value for key, value in _strings(arguments)
                if (key in PATHS or value.endswith(FILES)) and not value.startswith("/")]
    assert relative == []


FILES = (".json", ".md", ".pdf", ".docx")


def _strings(value: object, key: str | None = None) -> list[tuple[str | None, str]]:
    """Every text in the arguments, nested ones too, with the name it is given under."""
    if isinstance(value, dict):
        return [found for name, each in value.items() for found in _strings(each, name)]
    if isinstance(value, list):
        return [found for each in value for found in _strings(each, key)]
    return [(key, value)] if isinstance(value, str) else []
