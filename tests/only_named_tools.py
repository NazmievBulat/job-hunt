def assert_only_named_tools(command: list[str]) -> None:
    """A background run loads no user or local settings, so a tool the person's own Claude Code
    allows everywhere (Bash, a plugin's browser) is not allowed to it, and runs in `dontAsk`, so
    a tool its `--allowedTools` does not name is refused rather than asked about."""
    assert command[command.index("--setting-sources") + 1] == "project"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
