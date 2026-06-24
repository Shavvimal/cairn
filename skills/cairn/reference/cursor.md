# Cursor IDE Sessions

Export Cursor IDE composer conversations to markdown. Shared verbs (`export`, `list`,
`context`, `note`, `close`, `log`) work as documented in `../SKILL.md`. Windowed -
`export` defaults to the last 24h; use `--since` / `--all`.

## Data source

Cursor stores sessions in SQLite (`state.vscdb`):

- **Global DB**: `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- **Workspace DBs**: `~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb`

The exporter reads composer threads across both. Exported to `cursor-sessions/`.

## Frontmatter schema

```yaml
type: cursor-session
date: YYYY-MM-DD
composer_id: uuid
repo: your-product-api
mode: agent # agent / chat / edit / plan
model: claude-4.5-sonnet-thinking
title: "..."
branch: feature/move-webhook-proc
messages: 34
last_activity: ISO timestamp
status: active
tags: []
rating: null
comments: ""
projects: []
```

Content sections: Artifacts, My Notes, Conversation.
Preserved on export: `## My Notes` plus frontmatter `comments`, `projects`, `status`,
`tags`, `rating`, `title`.
