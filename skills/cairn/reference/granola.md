# Granola Meetings

Export Granola AI meeting notes (summaries + transcripts) to markdown. Shared verbs
(`export`, `list`, `context`, `note`, `close`, `log`) work as documented in `../SKILL.md`.
Windowed - `export` defaults to the last 24h; use `--since` / `--all`.

## Data source (dual)

Combines two sources per meeting:

**Local cache** - `~/Library/Application Support/Granola/cache-v4.json`

- Meeting metadata, attendees, calendar events, user-written notes
- Updates live as you record - can query mid-meeting
- v4 cache stores `cache` as a dict (not a JSON string like v3)

**Granola API** (reverse-engineered from the desktop app) - AI summaries + transcripts

- `POST /v2/get-documents` - documents with AI summary panels (`include_last_viewed_panel: true`)
- `POST /v1/get-document-transcript` - transcript segments with speaker source + timestamps
- Auth: WorkOS OAuth Bearer token

### Auto-auth

The token is read automatically from
`~/Library/Application Support/Granola/supabase.json` (WorkOS OAuth, single-use refresh
tokens) - no manual setup. If the token is unavailable, falls back to **cache-only**
export. Read from Keychain / `supabase.json`; never entered by hand.

Exported to `granola-sessions/`.

## Output sections

Each meeting file includes (when available):

- **Frontmatter**: date, title, time, duration, attendees, creator
- **## Attendees**
- **## Summary** - AI-generated summary (ProseMirror JSON panels → clean markdown)
- **## Notes** - user-written notes from Granola
- **## My Notes** - preserved across re-exports (your annotations)
- **## Transcript** - full transcript with speaker labels + timestamps. Speaker source:
  `microphone` = you (rendered **You**), `system` = others on the call

```yaml
type: granola-meeting
date: 2026-03-10
meeting_id: b68af9b5-...
title: "Design sync - your-product"
time: "11:00"
duration_min: 30
attendees:
  - "Alex Rivera <alex@example.com>"
creator: "Jordan Lee <jordan@example.com>"
last_activity: 2026-03-10T11:30:00Z
status: raw
tags: []
```

Preserved on re-export: `## My Notes` plus frontmatter `status`, `tags`, `rating`,
`comments`, `title`, `projects`. New meetings export with `status: raw`.
