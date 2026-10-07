---
name: search
description: Quick inline search across all collections. Lightweight mid-conversation lookup - QMD BM25 for topics and concepts, ripgrep (rg) for exact strings, identifiers, error text, paths, session IDs, or regex. Use when the user says "search for", "find", "look up", "grep", "where was", "what was that", or just /search QUERY. Requires qmd (install via /cairn:setup); rg is optional.
argument-hint: QUERY [-n NUM] [-c COLLECTION] [--full] [--exact]
allowed-tools: Bash(qmd:*), Bash(rg:*), Bash(head:*)
---

# Search Skill

Lightweight mid-conversation search. One query, top results with snippets, done.

Unlike `/recall` (which expands queries, fetches full documents, and synthesizes a "One
Thing"), `/search` is a fast inline lookup - results with snippets, no synthesis.

Two engines:

- **`qmd search`** (BM25, ranked) - topics and concepts ("webhook processing").
- **`rg`** (ripgrep, exact) - literal strings and regex ("ECONNRESET", `parse_frontmatter`,
  a file path, a session ID). rg reads the markdown files directly, so it also finds
  sessions that QMD has not indexed yet. It has no ranking, so it needs the output limits
  in step 4.

## Usage

```
/search webhook processing          # search all collections (BM25)
/search analysis pipeline -n 3      # limit results
/search onboarding flow -c notes    # restrict to one collection
/search policy management --full    # show full document content
/search ECONNRESET --exact          # exact match with rg
/search "def \w+_frontmatter" --exact   # regex with rg
```

## Workflow

1. **Parse the query and flags** from the user's input:
   - `-n NUM` - number of results (default: 5)
   - `-c COLLECTION` - restrict to one collection (see step 2 for valid names)
   - `--full` - show full document content instead of snippets
   - `--exact` (alias `--rg`) - use rg instead of BM25
   - Everything else is the search query.

   **Choose the engine.** Use rg if `--exact` is given, or if the query is clearly a
   literal: an identifier, error string, file path, URL, session ID, or regex. Use rg
   also when the user asks about something from the last hour, which QMD might not have
   indexed yet. Otherwise use `qmd search`. If rg is not installed (`command not found`),
   fall back to `qmd search` and say so in one line.

2. **Resolve the collection (only if `-c` is given).** Do **not** hardcode or guess
   collection names - they vary per machine and change as integrations are added. Get the
   live list:

   ```bash
   qmd collection list
   ```

   Use one of the returned names for `-c`. Omit `-c` to search every collection (default).

3. **BM25 path - run the search:**

   ```bash
   qmd search "QUERY" -n NUM [-c COLLECTION]
   ```

   Use `qmd search` (BM25), not `qmd query` (hybrid) - BM25 is much faster, and speed is
   the point for inline lookups. Go to step 5.

4. **rg path - resolve paths, then search in two passes.**

   a. **Resolve each collection's directory.** Do **not** hardcode or guess paths.
      Collections can live outside cairn's `data_root` (for example a user's own notes
      folder). For each collection to search (all names from `qmd collection list`, or the
      `-c` one), read the `Path:` line:

      ```bash
      qmd collection show COLLECTION
      ```

      Skip a collection whose directory does not exist. Quote each path, because paths
      can contain spaces.

   b. **Pass 1 - list matching files, newest first, capped:**

      ```bash
      rg -l -i -F "QUERY" --type md --sortr modified "PATH1" "PATH2" ... | head -n 20
      ```

      - `-F` treats the query as literal text. Drop `-F` for a regex query.
      - Drop `-i` if case matters (identifiers, error codes).
      - `--sortr modified` puts the newest files first. An exported session file changes
        only when its session changes, so this is a good proxy for recency.
      - For counts per file instead of names, use `-c` in place of `-l`.

   c. **Pass 2 - matched lines for the top NUM files only, capped:**

      ```bash
      rg -n -i -F "QUERY" -M 300 -m 5 -C 1 "FILE1" "FILE2" ...
      ```

      - `-M 300` cuts lines longer than 300 characters. **Never omit it**: a session
        transcript can hold a single line of several megabytes.
      - `-m 5` limits matches per file. `-C 1` adds one line of context.

   **Guardrails.** Never run rg over a whole collection without `-l` or `-c`, and never
   print matched lines without both `-M` and `-m`. An unbounded rg over a real corpus can
   return tens of thousands of lines and fill the context window.

5. **Present results** as a compact, scannable list/table. For BM25 hits: title,
   collection, score, and snippet. For rg hits: file path, collection, date (session files
   are named `YYYY-MM-DD-<id>.md`), and the matched line. The user wants a quick answer,
   not a wall of text.

6. **If `--full`** (or the user asks to expand a hit):

   ```bash
   qmd get "document-path" -l 100
   ```

   For an rg hit, use its `qmd://COLLECTION/<path relative to the collection Path>` form,
   or read the file directly.

Do NOT: expand the query into variants, fetch full documents unless asked, synthesize a
"One Thing", or run parallel searches across variants - that's `/recall`'s job.
