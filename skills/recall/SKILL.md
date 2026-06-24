---
name: recall
description: Loads context from previous sessions and notes. Temporal queries (yesterday, last week, a date) list sessions chronologically from the native timeline via the cairn CLI; topic queries search across QMD collections. Every recall ends with "One Thing" - the single highest-leverage next action synthesized from the results. Use when the user says "recall", "what did we work on", "load context about", "remember when we", "prime context", "yesterday", "what was I doing", "last week", or "session history". Requires the cairn CLI and qmd (install via /cairn:setup).
argument-hint: [yesterday|today|last week|this week|YYYY-MM-DD|TOPIC]
allowed-tools: Bash(cairn:*), Bash(qmd:*)
---

# Recall Skill

Two modes - **temporal** (date-based session timeline) and **topic** (search across QMD
collections) - both ending in the **One Thing**: a concrete, highest-leverage next action.

The logic lives in the `cairn` CLI and `qmd`; this skill just drives them. For exact
flags, run `cairn recall --help`.

## Step 1: Classify the query

Parse the user's input after `/recall`:

- **Temporal** - a time reference ("yesterday", "today", "last week", "this week", a date,
  "what was I doing") → Step 2.
- **Topic** - a subject ("auth flow", "the QMD migration", "onboarding") → Step 3.
- **Both** - temporal + topic ("what did I do with auth yesterday") → Step 2, then filter
  the listed sessions for the topic (or run Step 3 scoped to that window).

## Step 2: Temporal recall (`cairn recall`)

```bash
cairn recall list "DATE_EXPR"          # e.g. yesterday, today, "last week", 2026-06-20
cairn recall list "DATE_EXPR" --json   # machine-readable (session_id, time, msgs, title, project)
```

Date expressions: `today`, `yesterday`, `YYYY-MM-DD`, `N days ago`, `last N days`,
`this week`, `last week`, `last <weekday>`. Useful flags (see `cairn recall list --help`):
`--all-projects` (scan every project, not just the current dir), `--min-msgs N` (default 3;
use `--min-msgs 1` to include short sessions), `--project PATH`.

Present the session table. To expand one the user picks:

```bash
cairn recall expand SESSION_ID         # condensed transcript (user msgs, assistant lines, tools)
```

→ Go to Step 4 (One Thing).

## Step 3: Topic recall (QMD search)

1. **Enumerate the collections to search** - do not hardcode or guess names:

   ```bash
   qmd collection list
   ```

   Use the returned collection names (e.g. `notes`, `claude-code-sessions`,
   `codex-sessions`, `cursor-sessions`, `granola-sessions`, `service-docs`) as the valid
   `-c` values. (cairn-managed integrations + their enabled state: `cairn config show`.)

2. **Expand the query.** BM25 is keyword-based, so the user's phrasing often misses the
   words the session actually used. Generate 3–4 alternative phrasings (synonyms / related
   terms) for the topic.

3. **Search** all variants across the relevant collections (BM25 - fast, ~0.3s each), and
   run them in parallel:

   ```bash
   qmd search "VARIANT" -c COLLECTION -n 5
   ```

   Prioritize session collections for "what did we do" questions; include `notes` /
   `granola-sessions` / `service-docs` as relevant. Use `qmd search` (BM25), **not**
   `qmd query` (hybrid) - speed matters here.

4. **Deduplicate** by document path (keep the highest score); present the top ~5 unique hits.

5. **Fetch full context** for the top 2–3:

   ```bash
   qmd get "qmd://collection/path/to/file.md" -l 50
   ```

   Organize by type: sessions (what was worked on, decisions, status), notes (research,
   plans), meetings (discussions, attendees), docs (architecture, APIs).

→ Go to Step 4 (One Thing).

## Step 4: One Thing

After presenting results, synthesize the single highest-leverage next action.

**How to pick it:** what has momentum (recent, mid-flow) · what's blocked (removing the
blocker unlocks downstream) · what's closest to done (finishing > starting) · urgency
signals (deadlines, "blocked" status).

**Format** - a bold line at the end:

> **One Thing: [specific, concrete action]**

Good: _"One Thing: Finish the export CLI - `--since` parsing is the only piece left."_
Bad: _"Continue working on the CLI."_ (too generic)

If the results don't support a clear pick, skip it and ask: "What would you like to work
on from here?"

## Fallback: no results

```
No results for "QUERY". Try: different terms, a broader date range, or --min-msgs 1.
```

## Notes

- Temporal recall reads the native session timeline via `cairn recall` (no QMD needed) -
  it sees today's activity even before it's exported/indexed.
- Topic recall uses BM25 (`qmd search`), not hybrid (`qmd query`) - much faster inline.
- Always enumerate collections with `qmd collection list` rather than assuming a fixed set.
