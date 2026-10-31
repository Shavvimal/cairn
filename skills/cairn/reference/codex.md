# Codex CLI Sessions

Export Codex CLI conversations to markdown. Shared verbs (`export`, `list`, `context`,
`note`, `close`, `log`) work as documented in `../SKILL.md`. Windowed - `export`
defaults to the last 24h; use `--since` / `--all`.

## Data source

Codex stores each session as a rollout JSONL file:

```
~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl
```

Each line is `{"type": ..., "payload": {...}}`. The exporter pulls:

- `session_meta` (the first one) → session id, cwd, timestamp, git branch/commit, cli
  version, `originator` (`codex-tui`, `codex_exec`, ...), `thread_source`,
  `forked_from_id`, and for a subagent the `source.subagent.thread_spawn` lineage
- `turn_context` → model (e.g. `gpt-5.5`), `effort`, `sandbox` (`sandbox_policy.type`),
  `approval_policy`
- `event_msg` (`user_message`) → user prompts
- `response_item` (`message`, role assistant) → assistant text; `phase: final_answer`
  marks a subagent finished
- `response_item` (`function_call` / `function_call_output`, `custom_tool_call` /
  `custom_tool_call_output`) → compact tool trace (code-mode `exec` calls included)
- `response_item` (`agent_message`) → an `### Agent` turn: `from <author> to
  <recipient>`, the plaintext header, and `[encrypted]` for the encrypted body

Skipped: telemetry (`token_count`, `rate_limits`, `task_started` / `task_complete`) and
encrypted `reasoning` items.

## Project / repo resolution

Project and repo are resolved from the session `cwd`, including worktree paths
(`~/Code/your-product/worktrees/api/<branch>` and
`~/.superset/worktrees/api/<branch>`). Sessions that don't resolve land in the
collection root.

Exported to `codex-sessions/<project>/<repo>/`.

## Frontmatter schema

```yaml
type: codex-session
date: YYYY-MM-DD
session_id: 019ee073-1633-7f92-abec-6f8626edb240
repo: your-product-api
branch: feature/tuning-resolution
commit: 5611a97848ba96f6cf454cb150e680acfa5383a8
model: gpt-5.5
originator: codex-tui             # when present
thread_source: user               # when present
effort: high                      # latest turn_context
sandbox: read-only                # sandbox_policy.type
approval_policy: never
forked_from_id: 019e...           # fork children only
title: "..."
messages: 4
last_activity: ISO timestamp
status: active
tags: []
rating: null
comments: ""
projects: []
```

A subagent document has `type: codex-subagent` and adds, after the context keys:

```yaml
parent_session_id: 019e...        # thread_spawn.parent_thread_id
spawn_depth: 1
agent_type: worker                # thread_spawn.agent_role, omitted when null
agent_nickname: "Bohr"
agent_path: "/root/worker"
replay_unresolved: true           # only when the replay cut failed (see below)
```

Its status defaults to `done` once it gave a final answer.

Content sections: Subagents (parents only), Tool Calls, My Notes, Conversation.
Preserved on export: `## My Notes` plus frontmatter `comments`, `projects`, `status`,
`tags`, `rating`, `title`.

## Subagent rollouts

Codex multi-agent runs write one rollout per child thread. The exporter keeps each child
as its own document:

- **First meta wins.** A child spawned with `fork_context` writes two `session_meta`
  lines, its own then the parent's, followed by the replayed parent history. The child
  keeps its own id, so it never merges with the parent.
- **Replay cut** (`src/cairn/sources/codex_replay.py`, tagged `codex-cli 0.160`). The
  child's own history starts at the first `task_started` whose `turn_id` sorts at or
  after the child id (both UUIDv7), or at the `thread_settings_applied` event for the
  child, or at the developer message with `<multi_agent_role>`, whichever comes first
  (the exact child marker always counts, even after an ancestor's UUIDv7 turn ids). If
  none appears, a
  rollout changed in the last 600 s is a live child and is skipped this run. An older
  one exports whole with `replay_unresolved: true` and a warning on stderr.
- **Title.** The first user prompt. Children usually have none (inter-agent text is
  encrypted), so the title falls back to `<agent_type> <agent_nickname>: <agent_path>`.
- **Location.** A child lives in its parent's session folder:
  `<parent stem>/agent-<short id>.md`. A child exported before its parent document
  existed stays at the canonical path. Two children with the same short id get the
  same sha256 suffix `export_one` applies, and the parent links each to its real file.
- **Parent index.** The parent document lists every child under `## Subagents` with a
  relative link, the child id, its agent type, and `finished` or `running`. A new,
  retitled, or finished child re-renders the parent even when the parent rollout did not
  change, and so does a moved link. A windowed export selects a parent when it or any
  child changed in the window.
- **Listing.** `codex-subagent` documents resolve by id (`note`, `log`, recall) but are
  hidden from `list`. `export_target` for any family member loads the whole family (the
  root and all its descendants), so every rewritten index is complete. If a family
  rollout cannot be read, it writes the readable documents and then fails, so the hook
  job retries. The first-line lineage index is cached under
  `$XDG_CACHE_HOME/cairn/lineage/`.

## Parser checks

- **Spec target.** `SPEC_TARGETS["codex"]` in `src/cairn/sources/spec_targets.py`: the date the
  record shapes were checked against a live store and the host version that day. Every
  document carries it as `exporter_spec_target`, beside `exporter_version`.
- **Fixtures.** `tests/fixtures/codex/` holds invented transcripts with expected
  messages; `tests/test_parser_fixtures.py` scores message recall exactly.
- **Drift.** `event_msg` and `response_item` also carry control payloads, so the guard counts the finer kinds it renders (`event_msg/user_message`, `response_item/message/assistant`, `function_call`, `custom_tool_call`). A rollout with none of them and only known types is `empty`. A rollout older than 10 minutes with no `session_meta` id is `no-session-id`, filed under the thread id in its filename. A live replaying child is skipped, not flagged. Subagent rollouts have no user turn by design and never warn `no-user-turns`.

## Resume and automatic sync

`cairn resume codex:ID` (or `cairn codex resume ID`) prints `codex resume ID`. Run it in
the user's terminal, not a nested agent shell. Notes/status accept `--session-id ID`.
The Codex SessionEnd hook exports the event's native ID through the shared durable worker;
`cairn hook status --harness codex` distinguishes queued from completed indexing.
