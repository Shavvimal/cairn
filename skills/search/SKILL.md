---
name: search
description: Quick inline QMD search across all collections. Lightweight mid-conversation lookup - returns snippets with titles and scores. Use when the user says "search for", "find", "look up", "where was", "what was that", or just /search QUERY. Requires qmd (install via /cairn:setup).
argument-hint: QUERY [-n NUM] [-c COLLECTION] [--full]
allowed-tools: Bash(qmd:*)
---

# Search Skill

Lightweight mid-conversation search. One query, top results with snippets, done.

Unlike `/recall` (which expands queries, fetches full documents, and synthesizes a "One
Thing"), `/search` is a fast inline lookup - BM25 results with snippets, no synthesis.

## Usage

```
/search webhook processing          # search all collections
/search analysis pipeline -n 3      # limit results
/search onboarding flow -c notes    # restrict to one collection
/search policy management --full    # show full document content
```

## Workflow

1. **Parse the query and flags** from the user's input:
   - `-n NUM` - number of results (default: 5)
   - `-c COLLECTION` - restrict to one collection (see step 2 for valid names)
   - `--full` - show full document content instead of snippets
   - Everything else is the search query.

2. **Resolve the collection (only if `-c` is given).** Do **not** hardcode or guess
   collection names - they vary per machine and change as integrations are added. Get the
   live list:

   ```bash
   qmd collection list
   ```

   Use one of the returned names for `-c`. Omit `-c` to search every collection (default).

3. **Run the search:**

   ```bash
   qmd search "QUERY" -n NUM [-c COLLECTION]
   ```

   Use `qmd search` (BM25), not `qmd query` (hybrid) - BM25 is much faster, and speed is
   the point for inline lookups.

4. **Present results** as a compact, scannable list/table: title, collection, score, and
   snippet per hit. The user wants a quick answer, not a wall of text.

5. **If `--full`** (or the user asks to expand a hit):

   ```bash
   qmd get "document-path" -l 100
   ```

Do NOT: expand the query into variants, fetch full documents unless asked, synthesize a
"One Thing", or run parallel searches across variants - that's `/recall`'s job.
