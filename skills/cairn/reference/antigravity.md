# Google Antigravity (agy) Conversations

Export Google Antigravity CLI conversations to markdown. Shared verbs (`export`, `list`,
`context`, `note`, `close`, `log`) work as documented in `../SKILL.md`, plus `resume`
(`cairn antigravity resume <id>` prints `agy --conversation <id>`). Windowed - `export`
defaults to the last 24h; use `--since` / `--all`.

## Data source

The collection's `store` is agy's app-data directory, `~/.gemini/antigravity-cli` for the
CLI (the IDE and the Antigravity 2.0 app keep the same layout under
`~/.gemini/antigravity-ide` and `~/.gemini/antigravity`). Two things are read:

```
<store>/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl   # one step per line
<store>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl        # compact fallback
<store>/conversation_summaries.db                                              # SQLite index
```

Transcript records carry `step_index`, `source`, `type`, `status`, `created_at`, `content`,
`thinking`, `tool_calls`, token counts, `error` and `media`. The exporter maps:

- `USER_INPUT` → `### User`, the text inside `<USER_REQUEST>` (agy's metadata blocks dropped)
- `PLANNER_RESPONSE` → `### Assistant`: text, folded `Reasoning` (`thinking`), one
  `<tool name=… status=…>` block per call, whose result is the following `GENERIC` step
  (`status` done | error | running; the `Created At` / `Completed At` preamble removed)
- `SYSTEM_MESSAGE` → `### System`, the body of agy's `<SYSTEM_MESSAGE>` block
- an unpaired `GENERIC` → `### Tool`; `media` → `[media <mime>](<uri>)` lines

The index gives the title agy generated, the workspace (→ `cwd`, project and repo), the
run status, the agent name and the parent conversation of a subagent. A conversation without
an index row still exports; its title is the first prompt. Subagent conversations become
`antigravity-subagent` documents in the parent's folder, and the parent gets a `## Subagents`
index. User-facing artifacts agy wrote (`brain/<id>/*.md`) are listed under `## Artifacts`.

Exported to `antigravity-sessions/<project>/<repo>/`.

## Frontmatter schema

```yaml
type: antigravity-session           # antigravity-subagent for a child conversation
date: YYYY-MM-DD
session_id: 3cb7b301-f006-4d8a-8f84-3dae465accfe   # the agy conversation id
repo: cairn
run_status: IDLE                    # agy's CASCADE_RUN_STATUS_* with the prefix stripped
agent: default                      # when agy names one
tokens: "in=6970 out=27 cached=0"
steps: 5
step_errors: 1                      # steps agy marked ERROR, when any
parent_session_id: …                # children only
spawn_depth: 1                      # children only
title: "..."
messages: 2                         # user turns
last_activity: ISO timestamp
```

Then the shared metadata (`cairn_schema`, `source`, `native_id`, `source_revision`, `cwd`,
`project`, `started_at`, `activity_at`, counts, `activity_dates`), the `provenance` key
(`tool`, or `agent` for a peer run or a subagent) and the preserved tail
(`status`, `tags`, `rating`, `comments`, `projects`, `links`).

## Parser checks

- **Spec target.** `SPEC_TARGETS["antigravity"]` in `src/cairn/sources/spec_targets.py`: the date the
  record shapes were checked against a live store and the host version that day. Every
  document carries it as `exporter_spec_target`, beside `exporter_version`.
- **Fixtures.** `tests/fixtures/antigravity/` holds invented transcripts with expected
  messages; `tests/test_parser_fixtures.py` scores message recall exactly.
- **Drift.** agy files any step type it does not know as a tool result, so the guard counts only rendered user and assistant turns: a transcript of unknown step types is `zero-messages`, not a list of tool turns. Child conversations never warn `no-user-turns`.

Content sections: My Notes, Decisions (once `decide` adds a row), Conversation.
Preserved on export: `## My Notes`, the `## Decisions` table after it, and the
frontmatter tail above.

## Hook

agy has no SessionEnd; its `Stop` event fires at the end of every turn. `cairn hook install
--harness antigravity` writes one named hook into the global `~/.gemini/config/hooks.json`:

```json
{
  "cairn": {
    "enabled": true,
    "Stop": [
      { "type": "command", "command": "/usr/bin/env CAIRN_CONFIG=… <python> -m cairn hook handle --harness antigravity", "timeout": 10 }
    ]
  }
}
```

The handler receives agy's camelCase payload (`conversationId`, `workspacePaths`,
`transcriptPath`, …) on stdin, queues the export, prints `{}` (any other decision would block
agy's stop) and exits 0. Re-exporting an unchanged conversation is a no-op, so per-turn firing
is cheap. `cairn hook status --harness antigravity` shows the registration;
`cairn hook uninstall --harness antigravity` removes only the `cairn` key. agy loads the file
when it next starts.

Peer runs (`cairn invoke antigravity`) export the conversation at the end of the round
without the hook, and link it to the calling session.

## Permissions for headless runs

Headless agy cannot prompt, so a tool call that needs approval is auto-denied and the turn
goes on without it; the `result` still says `SUCCESS` and lists `denied_actions`. Rules live
in `<store>/settings.json` under `permissions.allow` / `permissions.deny`: `command(<prefix>)`
matches a shell command word by word (`command(git log)` covers `git log -3`, not `git`
alone), `command(regex:...)` a pattern, `read_file(<path>)` a file; every segment of a
compound command needs its own rule and a deny rule beats an allow rule. Allowed commands run
in plan mode too (verified live); `--dangerously-skip-permissions` needs no rules.

`cairn hook install --harness antigravity --grant-commands` writes cairn's allow set (the
read-only commands a review needs and the usual test runners) and deny set (`supabase`,
`psql`, `terraform`, `aws`, `gcloud`, `kubectl`, `curl`, `wget`, `python`, `python3`, the
history-moving `git` verbs, `read_file(.env*)` by name), keeps every existing rule, skips a
deny whose target the user allows explicitly, and prints each rule it added. `cairn doctor`
lists both sets.

`cairn invoke antigravity` refuses an `accept-edits` run with no command rule before the
spawn (exit 2) and warns on the others. A round whose peer was refused calls and answered
nothing ends as `denied` (exit 2, retryable) with the rule each command needs in `error`; a
refusal beside an answer is a `permissions:` warning. The result records `denied`,
`files_written` and the `permissions` in force, and a resume after a rule change tells the
peer what changed and which refused commands it may now run.
