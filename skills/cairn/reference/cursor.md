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

## Parser checks

- **Spec target.** `SPEC_TARGETS["cursor"]` in `src/cairn/sources/spec_targets.py`: the date the
  record shapes were checked against a live store and the host version that day. Every
  document carries it as `exporter_spec_target`, beside `exporter_version`.
- **Fixtures.** `tests/fixtures/cursor/` holds invented transcripts with expected
  messages; `tests/test_parser_fixtures.py` scores message recall exactly.
- **Drift.** A composer with two or more bubble headers and no readable user or assistant bubble is `zero-messages`. A single bubble with no text is an aborted request (`empty`). Cursor never warns `no-user-turns` (agent runs often carry no user text) and is not in the sync gap check. The host version is recorded as `unrecorded`.

Content sections: Artifacts, My Notes, Decisions (once `decide` adds a row), Conversation.
Preserved on export: `## My Notes`, the `## Decisions` table after it, plus frontmatter `comments`, `projects`, `status`,
`tags`, `rating`, `title`.
