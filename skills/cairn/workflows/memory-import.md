# Import Claude memories

`cairn claude memory import` turns Claude Code auto memories (one markdown file each)
into `## Decisions` rows: into the session that wrote them when cairn exported it, else into
`<data_root>/decisions/<project>.md`. The source cell is `memory:<project>/<file>`. A
re-run adds only new memories. Memory files are never edited. Directory rules, kind
mapping and exit codes: `reference/claude.md`, section
"Memory import".

## Privacy

A memory holds whatever Claude chose to save, including private names. The import
writes only under `data_root`, which QMD indexes. Never copy a decisions document or a
memory row into a tracked file. To hide a row from recall, withdraw it (step 6); the text
stays in the file.

## Steps

1. Plan the import for the current project:

   ```bash
   cairn claude memory import --dry-run
   ```

   Read each `Would add` line and the summary `Dry run: would import N memories;
   nothing written`. Exit 1 with `MEMORY_DIR_MISSING`: run
   `cairn errors MEMORY_DIR_MISSING`. If the user moved auto memory with
   `autoMemoryDirectory`, ask for that path and repeat with `--memory-dir DIR`.

2. Show the user the count and the kinds (`feedback` and `project` memories become
   `decision` rows, `user` memories `fact` rows, `reference` memories are skipped), and ask
   whether to import. The default is the git project of the working directory. Use
   `--all-projects` or `--include-reference` only when the user asks for them.

3. Import:

   ```bash
   cairn claude memory import --json
   ```

   Read `added`, `skipped_existing`, `skipped_withdrawn`, `changed`, `invalid` and
   `failed`. Exit 1 means at least one memory was invalid or failed; each `Warning:`
   line on stderr names the file and the reason. A withdrawn claim is never added again.

4. For each `changed` memory, the warning names the row it was imported as (for example
   `claude:3f2a9c1e#4`). Ask whether the new text replaces the old claim. If yes, run
   `cairn claude supersede N "NEW CLAIM" --session-id ID` with N and ID from that ref.

5. Check the result:

   ```bash
   cairn recall decisions --source memory --json
   ```

   Rows in a project document have refs such as `memory:example-app#3`; rows in a
   session end in `[memory]` in text output. A `[memory]` row is model-written: cite it
   as a hint, not as a decision the user made.

6. Withdraw a wrong or private row:

   ```bash
   cairn claude forget 3 --reason private --session-id memory:example-app
   ```

   Read the `Withdrew row #3` line.
