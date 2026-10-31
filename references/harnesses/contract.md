# Harness execution contract

Resolve these resources relative to this installed package, never the user's repository.
Read only the relevant sibling: [Claude](claude.md) or [Codex](codex.md). Determine the
host from the current tool environment; ask if uncertain. A source describes the data,
not the active host: either harness can recall every configured source.

Use available tools to collect missing settings, execute CLI commands, observe long work,
and read/edit files. A process launch, quiet log, or elapsed timeout is not completion.
Verify exit status and functional results. Ask only for choices the user has not provided;
explicit task authorization carries through the workflow. Never invent an answer.

Prefer explicit source-qualified session identities from `cairn recall list --json`.
For note/close/log use the selected source command and pass its full native ID using
`--session-id`. If a current-session hint cannot be verified, ask for the target.
Never use a filename suffix as a full ID. Resume uses the selected record's source:
`cairn resume SOURCE:ID` prints the launch command; show it to the user for their terminal.
Use `--launch` only when explicitly running in a suitable interactive terminal.

Installed version checks use `cairn doctor --harness HOST --plugin-manifest PATH`.
The manifest path is relative to this package, not the working repository.

A source adapter that parses a host's native files declares a `SpecTarget` in
`src/cairn/sources/spec_targets.py`: the check date, the host and its version that day,
the record types that should render a message (`conversational`) and the ones it skips
on purpose (`ignored`). It calls `guard_parse` after each parse and ships invented
fixtures with expected messages under `tests/fixtures/<source>/`. Re-check the shapes on
a live store before changing the date.
