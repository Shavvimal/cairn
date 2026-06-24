# Log Session

Annotate the current session with title, tags, status, rating, comments, and an
auto-generated summary. Works for any **conversation** source - substitute the source name
in `cairn <source> note/close/log` (Claude shown below; the same verbs exist for codex,
cursor, and granola). service-docs is a file mirror and has no annotation verbs.

## Behavior

- **Always generates a summary** via a haiku subagent (analyzes the conversation)
- Reads valid tags from `../schema/tags.yaml`
- Updates frontmatter via the cairn CLI
- Fields: title, tags, status, rating, comments, summary

## Via CLI (primary)

```bash
# Add comment only
cairn claude note "got the sync working"

# Close session (mark done + comment)
cairn claude close "finished the feature"

# Full annotation
cairn claude log --status done --tags "implementation,automation" --rating 8 "Built the new sync"
```

`log` flags: `--status/-s`, `--tags/-t` (comma-separated), `--rating/-r N`, optional
trailing comment text, and `--session-id` to target a specific item (defaults to the
current one).

## Via voice/text (agent workflow)

User says: "log session - Enhanced sync skill, done, implementation automation, 8"

Claude:

1. Parses intent from natural language:
   - title: "Enhanced sync skill"
   - status: done
   - tags: [implementation, automation]
   - rating: 8
   - comment: (auto-generated from context)
2. Reads `../schema/tags.yaml` to validate tags
3. Runs a haiku subagent to generate a 2-3 line summary
4. Updates via CLI (`cairn claude log`, `cairn claude note`, `cairn claude close`)

### Example inputs

```
"log session - title: Built auth system, done, implementation, 9"
"log this - blocked on API, debugging"
"close session - shipped it, implementation, 8"
```

### Parsing rules

- **Title:** after "title:" or first quoted phrase
- **Status:** `done`, `active`, `blocked`, `handoff`
- **Rating:** number 1-10
- **Tags:** match against `../schema/tags.yaml`
- **Comment:** everything else, or auto-generate

## Finding the current session

```bash
echo $CLAUDE_SESSION_ID
# Session file pattern: claude-code-sessions/YYYY-MM-DD-{session_id[:8]}.md
```

## Status values

| Status    | When to use                  |
| --------- | ---------------------------- |
| `active`  | Still working on this        |
| `done`    | Finished, goal achieved      |
| `blocked` | Stuck, waiting for something |
| `handoff` | Branched to a new session    |

## Tags

**Tag vocabulary (resolved per machine):** read the user's own tags file if present -
`~/.config/cairn/tags.yaml` (or `$XDG_CONFIG_HOME/cairn/tags.yaml`) - otherwise fall back
to the bundled `../schema/tags.yaml`. The bundled file ships universal **type tags** plus
**example project tags** the user customizes; their real project taxonomy lives in the
local file (so it isn't committed to the plugin).

**Type tags (universal):** research, implementation, debugging, planning, brainstorm,
admin, quick, video, automation, writing

**Project tags:** defined by the user in their local tags file (the bundled file ships
`your-product` / `notes` as examples).

## Frontmatter updated

```yaml
title: "Enhanced cairn sync with tags, rating, comments"
status: done
tags:
  - implementation
  - automation
rating: 8
comments: |
  [2026-02-05 14:30] Productive session - built new schema
```
