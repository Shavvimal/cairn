# Claude Code Sessions

Export Claude Code conversations to markdown, and resume them. Shared verbs
(`export`, `list`, `context`, `note`, `close`, `log`) work as documented in
`../SKILL.md`. Claude additionally has `resume`.

## Data source

Claude Code stores each conversation as a transcript under
`~/.claude/projects/<project>/<session-id>.jsonl`. The exporter reads these and renders
one markdown file per session.

Exported to `claude-code-sessions/` with the file pattern
`claude-code-sessions/YYYY-MM-DD-{session_id[:8]}.md`.

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

Content sections: Summary, Skills Used, Artifacts, My Notes, Conversation.
Preserved on export: `## My Notes` plus frontmatter `comments`, `related`, `status`,
`tags`, `rating`.

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

## Internal sync hook

`cairn sync --hook` (the `SessionEnd` hook) exports the just-closed session and refreshes
the index. There is also `cairn claude sync` (with `--session-id` / explicit transcript)
for hook/explicit single-session sync - you rarely call this directly; use `export`.

## Annotate

```bash
cairn claude note "got it working"                                  # timestamped comment
cairn claude close "finished the feature"                           # mark done + comment
cairn claude log --status done --tags "implementation,automation" --rating 8 "Built the sync"
```

Find the current session id with `echo $CLAUDE_SESSION_ID`. Full annotation workflow:
`../workflows/log-session.md`.
