---
name: recall
description: Loads context from previous sessions and notes. Temporal queries (yesterday, last week, a date) list sessions chronologically from the shared exported store via the cairn CLI; topic queries search across QMD collections (BM25) plus an exact-match ripgrep pass. Every recall ends with "One Thing" - the single highest-leverage next action synthesized from the results. Use when the user says "recall", "what did we work on", "load context about", "remember when we", "prime context from past sessions", "what was I doing", "session history", "digest yesterday", "daily digest", or "cite the frame". Requires the cairn CLI and qmd (install via /cairn:setup); rg is optional.
argument-hint: [yesterday|today|last week|this week|YYYY-MM-DD|TOPIC]
allowed-tools: Bash(cairn:*), Bash(qmd:*), Bash(rg:*), Bash(head:*)
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.


# Recall Skill

Two modes - **temporal** (date-based session timeline) and **topic** (search across QMD
collections) - both ending in the **One Thing**: a concrete, highest-leverage next action.

The logic lives in the `cairn` CLI and `qmd`; this skill just drives them. For exact
flags, run `cairn recall --help`.

## Step 1: Classify the query

Parse the user's input after `/recall`:

- **Temporal** - a time reference ("yesterday", "today", "last week", "this week", a date,
  "what was I doing") → Step 2. A digest of a day, or recall that must cite its
  sources → the `digest` block in Step 2.
- **Topic** - a subject ("auth flow", "the QMD migration", "onboarding") → Step 3.
- **Both** - temporal + topic ("what did I do with auth yesterday") → Step 2, then filter
  the listed sessions for the topic (or run Step 3 scoped to that window).

## Step 2: Temporal recall (`cairn recall`)

```bash
cairn recall list "DATE_EXPR"          # e.g. yesterday, today, "last week", 2026-06-20
cairn recall list "DATE_EXPR" --json   # machine-readable (session_id, time, msgs, title, project, type, parent, provenance, owner)
cairn recall list "DATE_EXPR" --json --coverage   # {"sessions": [...], "coverage": {...}}
cairn recall list "DATE_EXPR" --include-subagents   # also list subagent transcripts
```

Date expressions: `today`, `yesterday`, `YYYY-MM-DD`, `N days ago`, `last N days`,
`this week`, `last week`, `last <weekday>`. Useful flags (see `cairn recall list --help`):
`--all-projects` (scan every project, not just the current dir), `--min-msgs N` (default 3;
use `--min-msgs 1` to include short sessions), `--project PATH`, repeatable `--source claude|codex|cursor|granola`,
repeatable `--provenance user|agent|tool|imported`, and `--refresh`.
Use `--provenance tool` to leave out peer and routine runs.
Use `--refresh` for recent/live history: it exports without requiring QMD. Stop on refresh
failure and report the store as potentially stale. Date windows are UTC; reduced-precision
legacy records are labeled. Keep source-qualified `reference` values for expansion.

Subagent transcripts (`type: claude-subagent`, id `agent-<agentId>`) are hidden from
`list` by default. `--include-subagents` shows them; `--min-msgs` does not apply to them,
because a subagent has one user message, its prompt. Each JSON row has `type` (the
document type) and `parent` (the parent session id, or `null` for a session). Text rows
for a child end with `(subagent of <parent[:8]>)`. `expand` takes a child id directly,
for example `cairn recall expand claude:agent-a193a0cb5d55bc5b6`.

With `--coverage`, the JSON is `{"sessions": rows, "coverage": {...}}`. Each row has
`session_id`, `time`, `msgs`, `title`, `project`, `type`, `parent`, `provenance` and `owner`. `coverage.verdict`
is `found`, `clean-miss` or `degraded-miss`, and `coverage.gaps` lists the store gaps
that can hide a session in the window. Without `--coverage`, `--json` prints the bare
array.

Present the session table. To expand one the user picks:

```bash
cairn recall expand SOURCE:SESSION_ID         # condensed transcript (user msgs, assistant lines, tools)
```

For a whole day, or when each claim must cite its source, use `digest` instead of
expanding sessions one by one. `expand` reads the head of one session; `digest` keeps
only the day's frames of every session, tail included:

```bash
cairn recall digest DATE --all-projects --out DIR --json   # one day only: today, yesterday, YYYY-MM-DD
cairn recall frame SOURCE:SESSION_ID ORDINAL --frames DIR/SOURCE-SHORTID.frames.jsonl
```

Other flags: `--project P`, repeatable `--source claude|codex|antigravity|cursor`,
`--include-subagents`, `--min-msgs N` (default 1, not 3 as in `list`), `--max-chars N`
(per session, default 40000), repeatable `--provenance VALUE` and `--refresh`.
Use `--provenance tool` to leave out peer and routine runs. Each session gives `SOURCE-SHORTID.md`,
the condensed body, and `SOURCE-SHORTID.frames.jsonl`, one row per frame. A
`<!-- f12 TS ROLE -->` marker cites frame ordinal 12; the `cairn:turn` numbers are
lengths, never ids. To dig into a cited line, run `recall frame` with that ordinal: it
prints the frame as exported and warns on stderr if the export changed since the digest.
Exit 3 means an empty day: no session was written. A legacy or corrupt export is skipped with a warning and listed under `skipped` in the JSON; the rest still exit 0.
A failure or an empty day carries a `code` (`DIGEST_EMPTY_DAY`, `RECALL_FAILED`); `cairn errors CODE` gives the next step.
The digest JSON also has a `coverage` key, in the same shape as `list --coverage`.
Sessions owned by a routine (`owner: routine:<name>`) and their subagents are left out and
listed under `excluded`; `--include-routine-sessions` keeps them.
The JSON also has `overdue`: the active `## Decisions` rows whose `revisit_by` is on or
before the digest day, from any session outside a routine's (a routine's sessions and
their subagents give none unless `--include-routine-sessions`; `--provenance` filters them
too). Withdrawn rows are never listed. Mention them in the One Thing when they bear on the day's work.
An overdue row never changes the exit code.

### Decisions ("what did we decide about X")

```bash
cairn recall decisions --all-projects                 # every active row, oldest first
cairn recall decisions "last week" --json             # rows whose since falls in the window
cairn recall decisions --overdue [--as-of YYYY-MM-DD]  # active rows with revisit_by before the date
```

Each session's `## Decisions` table holds claims with `kind` (decision, fact, commitment,
hunch), `confidence` (high, medium, low), `since`, `revisit_by` and a `source` cell of
frame citations. Rows are `<source>:<native_id>#<n>`. Struck rows (superseded, withdrawn
with `forget`, or struck with any other marker) are hidden unless `--include-struck`. So
is an unstruck row whose claim was withdrawn in another session: cairn keeps a ledger of
withdrawn claims. With `--include-struck` such a row ends in `[withdrawn: <reason>]`, and
each JSON row has `withdrawn`, `withdrawn_reason` and `withdrawn_via` (`row` or `ledger`).
Do not repeat a withdrawn claim as fact. `--project`, `--all-projects`
and `--source` work as in `list`; the default is the current directory's project. A row
that does not parse is skipped with a `Warning:` on stderr and listed under `problems` in
the JSON.

When you write from a digest, cite each claim as `[SOURCE:ID fN]` (for example
`[claude:3f2a9c1e f11]`, the `ref` from the digest file's front matter, full or a unique
prefix of at least 8 characters, and the marker ordinal). Quote only text copied exactly
from the cited frame. Then check the file:

```bash
cairn recall verify FILE --digest DIR          # FILE:LINE: REASON: DETAIL per failure
cairn recall verify FILE --digest DIR --fix    # move failing blocks under ## Unverified
```

Exit 0: every citation and quote verifies (or `--fix` moved the failures). Exit 4: at
least one failure. Exit 1: an error, such as DIR without `.cairn-digest`. `--json` gives
the counts and every failure with its reason.

→ Go to Step 4 (Gaps).

## Step 3: Topic recall (QMD search + rg)

1. **Enumerate the collections to search** - do not hardcode or guess names. Run the
   gaps report once, in parallel with it (Step 4 reads it):

   ```bash
   qmd collection list
   cairn recall gaps --json
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

4. **Exact-match pass (rg).** If the topic contains literal terms - an identifier, error
   string, file path, product or person name - run one rg pass for them in parallel with
   the BM25 searches. BM25 tokenises such terms badly, and rg also sees files that QMD has
   not indexed yet. Skip this step if rg is not installed.

   Resolve each collection's directory from its `Path:` line (never hardcode paths -
   collections can live outside cairn's `data_root`):

   ```bash
   qmd collection show COLLECTION
   ```

   Then list matching files, newest first, capped:

   ```bash
   rg -l -i -F "TERM" --type md --sortr modified "PATH1" "PATH2" ... | head -n 10
   ```

   Never print matched lines from a whole collection. If you need lines, run rg again on
   the chosen files only, with `-M 300 -m 5` (a session transcript can hold a single line
   of several megabytes). See the `/search` skill for the full rg rules.

5. **Deduplicate** by document path (keep the highest score); present the top ~5 unique
   hits. An rg hit at `<collection Path>/<rel>` is the same document as
   `qmd://COLLECTION/<rel>`. Rank rg-only hits after BM25 hits unless the literal term is
   the point of the query.

6. **Fetch full context** for the top 2–3:

   ```bash
   qmd get "qmd://collection/path/to/file.md" -l 50
   ```

   Organize by type: sessions (what was worked on, decisions, status), notes (research,
   plans), meetings (discussions, attendees), docs (architecture, APIs).

→ Go to Step 4 (Gaps).

## Step 4: Gaps

Print one computed **Gaps** line before One Thing. Temporal recall reads
`coverage.gaps` from the JSON of Step 2. Topic recall reads `gaps` from
`cairn recall gaps --json` (Step 3). Each gap has `kind`, `severity` (`warn` or
`info`), `collection`, `message` and `fix`.

- If the status is `degraded`, print `**Gaps:** <warn messages joined by "; ">`, then
  the info messages if there is room.
- If the status is `clean` and `gaps` still holds `info` gaps, print
  `**Gaps:** none blocking (<info messages joined by "; ">)`.
- If `gaps` is empty, name only what was checked. Temporal coverage (`scope: store`)
  checks the exported store, not QMD or rg: print `**Gaps:** none found in the
  exported store`. The topic report checks the store, QMD and rg: print
  `**Gaps:** none found (exports, index, rg checked)`.

Build the line from the JSON only. Never add a gap the JSON does not contain.

## Step 5: One Thing

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

A miss is clean or degraded. Read `coverage.verdict` (temporal) or `status` from
`cairn recall gaps --json` (topic).

- Clean miss:

  ```
  No sessions or notes match "QUERY" in WINDOW; no coverage gap was found.
  ```

  Then suggest different terms, a broader date range, or `--min-msgs 1`.
- Degraded miss:

  ```
  No results for "QUERY", but coverage is degraded: <gaps>. This is not proof it did not happen. Run <fix> and recall again.
  ```

## Resolve before asking

Before you ask the user about past work or a past decision, run the temporal recall for
the likely window and the topic recall for its key terms. Then ask with a hypothesis:
"I found X on DATE (REF); is that the decision you mean?" Ask an open question only if
both recalls return a clean miss, and say so. Report a degraded miss with its gaps, never
as "I have no record".

## Notes

- Temporal recall reads exported sessions from all configured sources (no QMD needed).
  Use `--refresh` when exports may be stale. The active harness never limits source visibility.
- Topic recall uses BM25 (`qmd search`), not hybrid (`qmd query`) - much faster inline.
- The rg pass wins on exact terms (identifiers, error strings, names) and on files that
  were exported after the last `qmd update`.
- Always enumerate collections with `qmd collection list` rather than assuming a fixed set.
- `cairn recall gaps` never exports or indexes; a gap with a non-null `fix` names the command that fixes it.
