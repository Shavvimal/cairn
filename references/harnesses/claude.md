# Claude execution

Use `Bash` for shell commands and `Read`/`Edit` for files. Collect missing settings with
`AskUserQuestion` when available, otherwise ordinary conversation. For long setup work,
use Bash's supported background execution, retain the task ID, observe progress with
`Monitor` when available, and read the task's actual exit status. Stop the monitor after
the task finishes; silence alone proves nothing.

Invoke `/cairn:setup` explicitly; other workflows are `/cairn:cairn`, `/cairn:recall`, and
`/cairn:search`. `disable-model-invocation` keeps setup explicit. Read the selected source's
reference for native data, independently of Claude as the executing harness.

Use the current session ID supplied by Claude or verified `CLAUDE_SESSION_ID`; pass it
explicitly to mutations. The plugin manifest is `.claude-plugin/plugin.json`.
Update the engine with `uv tool upgrade cairn`, then `/plugin update cairn@cairn` and
`/reload-plugins`. Re-run `cairn hook install` after an engine reinstall.

Bundled `hooks/hooks.json` supplies SessionEnd. `cairn hook install` records absolute
engine/config paths; it does not add another global hook. End a real session, then inspect
`cairn hook status --harness claude` and search a distinctive phrase from that session.
An isolated shell invocation of the handler is not host lifecycle verification.
