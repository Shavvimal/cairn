# Claude Code Sessions

Export Claude Code conversations to markdown, and resume them. Shared verbs
(`export`, `list`, `context`, `note`, `close`, `log`) work as documented in
`../SKILL.md`. Claude additionally has `resume`.

## Data source

Claude Code stores each conversation as a transcript under
`~/.claude/projects/<project>/<session-id>.jsonl`. The exporter reads these and renders
one markdown file per session.

Subagent transcripts live beside the parent:

```text
<project>/<session-id>.jsonl                                   parent transcript
<project>/<session-id>/subagents/agent-<agentId>.jsonl         one Agent subagent
<project>/<session-id>/subagents/agent-<agentId>.meta.json     its agentType, description, ...
<project>/<session-id>/subagents/workflows/wf_<id>/agent-<agentId>.jsonl   a workflow agent
<project>/<session-id>/subagents/workflows/wf_<id>/journal.jsonl           (not exported)
```

Exported to `claude-code-sessions/` with the file pattern
`claude-code-sessions/YYYY-MM-DD-{session_id[:8]}.md`. Each subagent exports into the
session folder beside it: `claude-code-sessions/<project>/YYYY-MM-DD-{short}/agent-<agentId>.md`.

## Frontmatter schema

```yaml
type: claude-session
date: YYYY-MM-DD
session_id: uuid
title: "..."
summary: "..."
skills: [skill1, skill2] # skills used during the session
messages: 42
last_activity: ISO timestamp
status: active | done | blocked | handoff
tags: [] # see ../schema/tags.yaml
rating: null # 1-10
comments: |
  [2026-02-05 14:30] Comment here
related: []
```

Content sections: Summary, Skills Used, Artifacts, Subagents, My Notes, Decisions (once
`decide` adds a row), Conversation.
Preserved on export: `## My Notes`, the `## Decisions` table after it, plus frontmatter `comments`, `related`, `status`,
`tags`, `rating`.

A message that another Claude session or a subagent sent to this session
(`SendMessage`) renders as an `### Agent` turn with the text
`from agent <from> (<name>):` and the plaintext body. It does not count as a user
message. A subagent's final report (a task notification) stays a `### User` turn.

## Subagent documents

Every `agent-<agentId>.jsonl` exports as its own document with `type: claude-subagent`.

| Key | Value |
|---|---|
| `session_id` | `agent-<agentId>` (use this id with `note`, `close`, `log`, `recall expand`) |
| `parent_session_id` | the parent session id |
| `agent_id` | `<agentId>` |
| `agent_type` | meta `agentType` (for example `general-purpose`, `workflow-subagent`) |
| `description` | meta `description`, quoted; also the title (else the first prompt line) |
| `tool_use_id` | the parent's Agent `tool_use` id (absent for workflow agents) |
| `spawn_depth`, `request_shape`, `model` | from the meta (`request_shape`: `background` or `foreground`) |
| `workflow_run`, `workflow_phase` | `wf_<id>` and the phase, for workflow agents only |
| `parent_agent_id`, `is_fork` | for a forked agent only |
| `stopped_by_user` | `true` when the user stopped the agent |
| `agent_cwd` | the child's own working directory, when it differs from the parent's |
| `repo`, `git_branch`, `project`, `cwd` | the parent's values, never the child's |

The body has a `Parent: [<id>](../<parent file>)` line, Artifacts, My Notes, and the
Conversation: the Agent prompt first, the tool calls, the final report last. A child
starts with `status: done` when its meta says `finished` or `stoppedByUser`, or (without
a `finished` key) when its last assistant turn ended the turn; otherwise `active`. A
child exported as `active` moves to `done` when it finishes.

The parent gains a `## Subagents` index after Artifacts, read from the metas only:

```markdown
- [Scout the repo](./2026-10-15-c4c36add/agent-a193a0cb5d55bc5b6.md) - `agent-a193a0cb5d55bc5b6`, general-purpose, background, done
```

The fields are the agent type, `background`, `foreground` or `workflow wf_<id>`, and
`done`, `stopped` or `running`. The parent re-renders when any field of its index changes: a new child, a title, type,
request shape, workflow run, `finished` or `stoppedByUser`. A child re-renders when its transcript or
its meta changes.

Visibility: `cairn claude list` and `cairn recall list` show parents only;
`cairn recall list --include-subagents` adds the children. `export --since` selects a
parent when the parent or any child changed in the window, because background agents
finish after the parent goes quiet. A malformed `meta.json` does not block the family:
its index row reads `agent <id> (meta unreadable)`, the parent and the other subagents
export, and the run then fails (exit 1, or a failed hook job that retries).

## Resume (Claude-only)

```bash
cairn claude resume --pick      # -p  interactive picker
cairn claude resume --active    # -a  most recent active session
cairn claude resume --fork      # -f  fork instead of continue (combine with the above)
cairn claude resume --all       #     show all sessions in the picker, not just active
cairn claude resume <file>      #     resume from a specific exported markdown file
```

Resume reconstructs the session id from the chosen file and launches
`claude --resume <session_id>` (adding `--fork-session` when `--fork` is set). Specify
one of `--pick`, `--active`, or a file.

A subagent transcript cannot be resumed. `cairn claude resume <child file>` and
`cairn resume claude:agent-<agentId>` exit 1 and name the parent to resume instead.

## Internal sync hook

`cairn sync --hook` (the `SessionEnd` hook) exports the just-closed session and refreshes
the index. There is also `cairn claude sync` (with `--session-id` / explicit transcript)
for hook/explicit single-session sync - you rarely call this directly; use `export`.

A parent session id exports the parent and then every stale subagent. If a subagent
fails (for example its `sessionId` is not the parent), the parent is still written and
the hook job fails, so it retries. An `agent-<agentId>` id (with or without its
transcript path) refreshes the parent's index and exports that one subagent.

## Parser checks

- **Spec target.** `SPEC_TARGETS["claude"]` in `src/cairn/sources/spec_targets.py`: the date the
  record shapes were checked against a live store and the host version that day. Every
  document carries it as `exporter_spec_target`, beside `exporter_version`.
- **Fixtures.** `tests/fixtures/claude/` holds invented transcripts with expected
  messages; `tests/test_parser_fixtures.py` scores message recall exactly.
- **Drift.** A transcript whose `user` and `assistant` records (or unknown record types) render no message is refused as `zero-messages`. A file of only control records (`attachment`, `last-prompt` and the like) is `empty`: it exports as before and the gap check does not count it. Subagent transcripts never warn `no-user-turns`. A refused subagent fails the parent's hook job, so it retries.

## Annotate

```bash
cairn claude note "got it working"                                  # timestamped comment
cairn claude close "finished the feature"                           # mark done + comment
cairn claude log --status done --tags "implementation,automation" --rating 8 "Built the sync"
```

Find the current session id with `echo $CLAUDE_CODE_SESSION_ID`. Full annotation workflow:
`../workflows/log-session.md`.
