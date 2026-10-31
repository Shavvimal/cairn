# Log Session

Annotate the current session with title, tags, status, rating, comments, and an
auto-generated summary. Works for any **conversation** source - substitute the source name
in `cairn <source> note|close|log|decide|supersede|forget` (Claude shown below; the same verbs exist
for codex, antigravity, cursor, and granola). service-docs is a file mirror and has no annotation verbs.

## Behavior

- **Always generates a summary** from the available conversation context
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

Agent:

1. Parses intent from natural language:
   - title: "Enhanced sync skill"
   - status: done
   - tags: [implementation, automation]
   - rating: 8
   - comment: (auto-generated from context)
2. Reads `../schema/tags.yaml` to validate tags
3. Writes a 2-3 line summary from the conversation; do not require a host-specific subagent
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

## Record a decision

A decision, fact, commitment or hunch that the session established goes in the
`## Decisions` table, one row per claim, not in a comment. The table sits directly after
`## My Notes`, survives every re-export, and `cairn recall decisions` lists it.

```bash
cairn claude decide "One PR per layer, each based on the layer below" \
  --kind decision --confidence high --source "[claude:3f2a9c1e f12]"
cairn claude decide "Send the review notes" --kind commitment --revisit-by 2026-11-15
cairn claude supersede 3 "Keep confidence as three words until grading lands"
```

- **Kind** (`--kind`, default `decision`): `decision` is a choice between options,
  `fact` is something observed and checkable, `commitment` is something promised
  (`--revisit-by` is its due date), `hunch` is a belief not yet checked.
- **Confidence** (`--confidence`, default `medium`): `high`, `medium` or `low`.
- **Citation** (`--source`): one or more frame citations `[<source>:<shortid> f<N>]`,
  separated by single spaces. `N` is the 0-based frame ordinal from
  `cairn recall digest` or `cairn recall frame`.
- **Correction:** never edit a row's claim. Run `supersede N`: row N is struck as
  `~~claim~~ superseded by #M` and the new row M gets `supersedes #N` in its source cell.
  The new row keeps row N's kind unless `--kind` is given. It does not keep `revisit_by`.
- **Export first:** the verbs write to the exported document. If the live session is not
  exported yet, run `cairn <source> export` before you annotate it, or `decide` exits 1
  with `Session file not found`.
- A claim is one line of at most 500 characters and must not contain `~~`.

## Withdraw a claim

A row that is wrong and has no replacement is withdrawn, not deleted. If a replacement
exists, use `supersede` instead.

```bash
cairn claude forget 3 --reason hallucination
```

`forget N` strikes row N as `~~claim~~ withdrawn: <reason>` (a superseded row keeps its
marker: `superseded by #4, withdrawn: <reason>`). It also appends a fingerprint of the
claim to `withdrawn.jsonl` in the data root. After that, `decide` or `supersede` with the
same claim, in any session and any source, exits 3. Case, punctuation and spacing do not
matter to the match.

| `--reason`          | Use when                                                    |
| ------------------- | ----------------------------------------------------------- |
| `stale`             | It was true and is no longer                                |
| `wrong_source`      | The source cell points at the wrong session, file or person |
| `hallucination`     | No source supports it                                       |
| `cross_attribution` | It is true of a different project, repo or person           |
| `private`           | It must not be recalled                                     |
| `other`             | None of the above                                           |

- **Do not pass `--force` on your own.** `decide --force` adds a withdrawn claim again
  and records it as unwithdrawn. Use it only when the user confirms the claim is true now.
- **Withdrawn is not erased.** The claim text stays in the session document and in the
  QMD index until someone edits the file. `--reason private` prints a note that says so.
- Run `forget` again on the same row: it reports `already withdrawn` and changes nothing.

## Import Claude memories

Claude Code keeps auto-memory notes per project (`~/.claude/projects/<project>/memory/`).
`cairn claude memory import` adds each note's one-line description as a `## Decisions`
row. The row goes into the session that wrote the note when cairn exported that session,
else into the project decisions document `<data_root>/decisions/<project>.md`. The
source cell is `memory:<project>/<file>`. Look at the plan first, then import:

```bash
cairn claude memory import --dry-run
cairn claude memory import
```

- Do not run it with `--all-projects` unless the user asks. The default is the git
  project of the working directory.
- `feedback` and `project` notes become `decision` rows, `user` notes become `fact` rows.
  `reference` notes are skipped unless `--include-reference`.
- A re-run adds only new notes. A changed note prints a warning and leaves its row; use
  `supersede` if the new text is right. A withdrawn claim is never added again.
- Rows in a project document take `--session-id memory:<project>` for `decide`,
  `supersede` and `forget`.

## Finding the current session

Use the harness reference for current-session metadata, or select a source-qualified
record from `cairn recall list DATE --all-projects --json`. Pass the full native ID with
`--session-id`. If the target is ambiguous, ask; do not guess from recent filenames.
The CLI annotates comments/status/tags/rating; put a proposed title or summary in the
comment rather than inventing unsupported flags.

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
