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

- `session_meta` → session id, cwd, timestamp, git branch/commit, cli version
- `turn_context` → model (e.g. `gpt-5.5`)
- `event_msg` (`user_message`) → user prompts
- `response_item` (`message`, role assistant) → assistant text
- `response_item` (`function_call` / `function_call_output`) → compact tool trace

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
title: "..."
messages: 4
last_activity: ISO timestamp
status: active
tags: []
rating: null
comments: ""
projects: []
```

Content sections: Tool Calls, My Notes, Conversation.
Preserved on export: `## My Notes` plus frontmatter `comments`, `projects`, `status`,
`tags`, `rating`, `title`.
