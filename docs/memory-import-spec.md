# Claude Code auto memory into decision rows (`cairn claude memory import`): implementation spec

Status: implemented in the PR that adds this file. Section 11 lists where the
implementation differs from the text above it and records the answers to the open
questions; where section 11 differs from an earlier section, section 11 wins.

Claims about behaviour before this change cite `path:line` at 2.19.3.

**Dependencies.** The decisions table (docs/decisions-table-spec.md): `DecisionRow`,
`parse_row`, `apply`, `rewrite`, `collect`, `row_json`, `run_recall`. Forget
(docs/note-forget-spec.md): `withdrawals.load`, `is_withdrawn`, `fingerprint`, the lock
order "document first, ledger second". Provenance (docs/provenance-spec.md): the
`provenance` key and its four values. The error registry (docs/error-registry-spec.md):
new codes. The skill routing eval, the docs CLI truth test and the docs index
(docs/skill-routing-eval-spec.md, docs/docs-cli-truth-spec.md,
docs/test-discipline-spec.md), which this change keeps green.

## 1. Purpose and non-goals

Claude Code keeps a per-project "auto memory": short notes that Claude writes for itself
about corrections, preferences and project facts. These notes are the closest thing the
harness has to a decision log. But they live outside cairn: `cairn recall decisions`
cannot list them, `forget` cannot withdraw them, and QMD does not index them.

This change adds one command, `cairn claude memory import`. It reads the memory files of
the current project (or of every project) and appends each memory as one row of a
`## Decisions` table. If the memory names a session that cairn exported, the row goes
into that session's table. If not, it goes into a project-level decisions document
under the data root. A re-run adds only memories that are new.

Command choice: `cairn claude memory import`, not a top-level decisions command with a
`--from claude-memory` flag. The input is Claude-specific in every part: the directory,
the encoding of the project path and the session ids. Per-source extras already live on
the source (`resume` and `sync`, `src/cairn/sources/claude.py:957-973`). A top-level
`decisions` command would exist for one input type only. The name also leaves room for a
later `memory import` verb on the Codex source if Codex grows an equivalent.

Non-goals:

- Writing to the memory directory. The import never edits, moves or deletes a memory
  file or `MEMORY.md`.
- Syncing in both directions. A changed or deleted memory does not change or remove a
  row (4.6).
- Importing `CLAUDE.md`, rules or subagent memory (`agent-memory/<name>/`). These are
  instructions, not observations.
- Reading `autoMemoryDirectory` from Claude Code settings files (open question 1).
- A hook that imports on every session end. Run it by hand or from a routine.

## 2. Current behaviour and external facts

### 2.1 Claude Code auto memory (docs and this machine, 2026-10-30)

| Fact | Evidence |
|---|---|
| One memory directory per project: `<config dir>/projects/<project>/memory/` | memory docs, "Storage location" (Sources S1) |
| `<project>` comes from the git repository, so worktrees and subdirectories share one directory. Outside a git repo the project root is used | S1 |
| `<project>` is the path with each non-alphanumeric character replaced by `-`. A name over 200 characters is cut to 200 and gets a hash of the full path | S2 |
| `CLAUDE_CONFIG_DIR` moves every `~/.claude` path | S3 |
| `CLAUDE_CODE_PROJECT_DIR_NAME`, together with `CLAUDE_CONFIG_DIR`, replaces `<project>` | S1, S2 |
| `autoMemoryDirectory` in any settings scope moves the directory elsewhere | S1 |
| `MEMORY.md` is an index, one line per memory; only its first 200 lines or 25 KB load at session start | S1 |
| One topic file per memory. Its frontmatter `type` is `user`, `feedback`, `project` or `reference` | S1 |
| Claude Code writes `modified` (ISO 8601) into the frontmatter on each write | S1; changelog 2.1.214 (S5) |
| Memory files are plain markdown that a person can edit or delete; the retention sweep does not delete them | S1, S4 |
| `originSessionId` is not documented | S1, S5 (no match in the changelog), S6 |

A survey of this machine (240 memory files in 19 directories, read for shape only)
adds facts the docs do not state:

| Observation | Count |
|---|---|
| Nested form: `metadata:` holds `type`, `node_type`, `originSessionId`, `modified` | 201 files |
| Flat form (older): `type`, `originSessionId`, `modified` at top level | 39 files |
| `metadata:` line with a trailing space (`metadata: `) | 78 files |
| `description` double-quoted, some with `\"` escapes | 133 quoted, 15 with escapes |
| `name` differs from the file stem (a title, sometimes with spaces) | 106 files |
| Every file stem matches `[A-Za-z0-9][A-Za-z0-9._-]*` | 240 of 240 |
| Directories without `MEMORY.md`; files not linked from `MEMORY.md` | 6 directories; 6 files |
| `type` values | project 136, feedback 91, reference 9, user 4 |
| Files with `originSessionId`; distinct ids with an exported cairn document | 193; 142 of 142 |

An invented example of the nested form:

```markdown
---
name: release-train
description: "Ship each change as its own PR off a fresh main"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 3f2a9c1e-0000-4000-8000-000000000001
  modified: 2026-10-30T13:46:12.010Z
---

Body text that the import does not read.
```

Consequences: the key is the file stem, not `name`. The reader goes by the files, not
by `MEMORY.md`. The reader accepts both forms.

### 2.2 cairn today

| Area | Behaviour | Where |
|---|---|---|
| Claude config dir | `claude_home()`: `$CLAUDE_CONFIG_DIR` if set and non-empty, else `~/.claude` | `src/cairn/harnesses/claude.py:71-78` |
| Transcript root | `collections.claude-code-sessions.store` from the config, not `claude_home()` | `src/cairn/sources/claude.py:68-71`, `src/cairn/config.example.json:40-43` |
| Path encoding | `claude_slug` replaces only `/` and `.`; the docs rule replaces every non-alphanumeric character | `src/cairn/config.py:91-97` |
| Project labels | `project_name_from_dir` (grouped) and `repo_name_from_dir` (ungrouped, home prefix stripped) | `src/cairn/projects.py:66-90`, used at `src/cairn/sources/claude.py:100-103` |
| Frontmatter parser | Refuses any indented line that is not a list item, so it refuses `metadata:` children | `src/cairn/frontmatter.py:124-128` |
| Scalar unescape | Only `\"` to `"` | `src/cairn/frontmatter.py:69-71` |
| Row grammar | Seven cells. The `source` cell is free text: only a leading `supersedes #N` is parsed | `src/cairn/decisions.py:66-77`, `:277-320`, `:189-192` |
| Citations | `CITATION_RE` matches `[claude:<id> fN]`. `citations_error` checks only the `--source` flag of `decide` | `src/cairn/decisions.py:95-98`, `:605-611` |
| Kinds, confidence, claim cap | `decision fact commitment hunch`; `high medium low`, default `medium`; 500 characters | `src/cairn/decisions.py:106-110`, `:587-594` |
| Table write | `apply(find_file, id, edit, ...)` locates the file, takes the document lock, parses, applies `edit`, writes with `atomic_write`. A missing `## My Notes` is an error | `src/cairn/decisions.py:484-532` |
| Append only | An edit cannot remove a row | `src/cairn/decisions.py:431` |
| Session lookup | `find_file` goes through the catalog by native id | `src/cairn/sources/base.py:341-343`, `src/cairn/mutations.py:75-88` |
| Records | Only documents whose `type` is the source's session type become records | `src/cairn/store.py:182-184`, `:252-264` |
| `recall decisions` | Reads `store.records`, filters by project (default: the cwd), hides struck and withdrawn rows. Its text ref cuts the id to 8 characters | `src/cairn/decisions.py:1034-1103`, `:1095` |
| Ledger | `load`, `is_withdrawn`, `fingerprint`. Lock order: document, then ledger | `src/cairn/withdrawals.py:14-17`, `:97-102`, `:288-299` |
| Collections | A collection without `store` is allowed. Sync registers every enabled collection that holds markdown | `src/cairn/config.py:382-415`, `src/cairn/config.example.json:74`, `src/cairn/sync.py:250-275` |
| Provenance | `agent`: an agent wrote it; `imported`: not produced on this machine's harness | `src/cairn/provenance.py:3-8`, `:30` |

## 3. Surface

### 3.1 CLI

```text
cairn claude memory import [--all-projects | --memory-dir DIR]
                           [--include-reference] [--dry-run] [--json] [-q]
```

| Flag | Default | Meaning |
|---|---|---|
| (none) | current project | the memory directory of the git repository that holds the cwd |
| `--all-projects` | off | every `<projects>/*/memory/` directory |
| `--memory-dir DIR` | off | exactly this directory (for `autoMemoryDirectory` or a copied tree) |
| `--include-reference` | off | import `reference` memories as kind `fact` |
| `--dry-run` | off | print the plan, write nothing, take no lock |
| `--json` | off | the summary object of 5.3 on stdout |
| `-q` | off | no per-row lines; warnings still go to stderr |

`--all-projects` and `--memory-dir` are mutually exclusive (argparse, exit 2).

`--session-id memory:<label>` becomes valid for `cairn claude decide`, `supersede` and
`forget`, so a person can strike or supersede a row in a project document.

### 3.2 Files

| Path | Writer | Content |
|---|---|---|
| `<data_root>/decisions/<label>.md` | this command | the project decisions document (5.2) |
| session documents | this command, through `decisions.apply` | new rows only |

`<label>` is `repo_name_from_dir(<encoded dir>)` with leading `-` removed, for example
`example-app`. This value is ungrouped, so two worktree directories never share a document.

`config.example.json` gains `"decisions": { "sync": { "enabled": true } }`, the same shape
as `journal`. QMD then indexes the directory on the next sync.

### 3.3 Exit codes and messages

| Exit | When |
|---|---|
| 0 | every memory was added or skipped by rule |
| 1 | no memory directory; a memory file could not be read or parsed; a write failed; the ledger is invalid. Other memories are still imported |
| 2 | usage error |

Exact messages (stderr unless marked):

- `Error: no Claude memory directory at <dir> for <project root>; pass --memory-dir DIR`
  (code `MEMORY_DIR_MISSING`, exit 1)
- `Error: more than one project directory starts with <first 200 characters>; pass --memory-dir DIR`
  (exit 1)
- `Warning: <file>: <reason>; skipped`. Reasons: `no frontmatter`,
  `frontmatter line <n>: <problem>`, `no description`, `unknown type '<t>'`,
  `claim is longer than 500 characters (got <n>)`, `claim must not contain '~~' (it marks a struck row)`
- `Warning: memory <label>/<stem> changed since it was imported as <ref>; supersede that row to update it`
- `Warning: session <id8> of memory <label>/<stem> is not exported; added to decisions/<label>.md`
  (only when `originSessionId` is present)
- `Warning: collections.decisions is not configured; QMD will not index <path>`
- `Error: could not write <path>: <reason>` (code `MEMORY_IMPORT_WRITE_FAILED`, exit 1)
- stdout, per added row: `Added <ref>  <kind>  <since>  memory:<label>/<stem>  <claim>`
- stdout, summary:
  `Imported <n> memories (<s> into sessions, <p> into project documents); skipped <e> existing, <w> withdrawn, <r> reference, <i> invalid`
- stdout, dry run: each line starts `Would add` and the summary is
  `Dry run: would import <n> memories; nothing written`

No doctor line. The import is a manual action, and doctor has no state to check.

## 4. Behaviour

### 4.1 Discovery

1. Projects root. If `CLAUDE_CONFIG_DIR` is set and non-empty, use `claude_home()/projects`.
   This matches Claude Code (S3). Else use the configured
   `claude-code-sessions.store`, which is the directory the exporter reads.
2. Current project root: `git rev-parse --path-format=absolute --git-common-dir` from the
   cwd. If the result ends in `/.git`, its parent is the root, so a linked worktree
   resolves to the main checkout (S1: "all worktrees ... share one auto memory
   directory"). If git is absent or the cwd is not in a repository, the root is the cwd.
3. Directory name. If `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_PROJECT_DIR_NAME` are both set
   and the name matches `^[A-Za-z0-9_-]{1,64}$`, use that name (S2). Else replace each
   character outside `[A-Za-z0-9]` with `-`. If the result is longer than 200
   characters, the hash is undocumented. In that case, glob `<first 200>*` and require
   exactly one match. The new helper `claude_project_dir_name(path)` goes in
   `harnesses/claude.py`. `claude_slug` stays as it is because prefix derivation uses it.
4. `--all-projects` lists `<projects>/*/memory/` and sorts by name.
5. In each directory, read every `*.md` except `MEMORY.md`, sorted by name. Do not
   recurse. Do not read the index (2.1: 6 directories have no index).

### 4.2 The memory reader

The new module `cairn/claude_memory.py` holds `read_memory(path) -> Memory | MemoryProblem`.
It is a small reader because `parse_frontmatter` refuses nested mappings by design
(`src/cairn/frontmatter.py:124-128`). Widening that parser would weaken its round-trip
contract for every document.

- The file starts with `---\n`. The block ends at the next line that is exactly `---`.
  At most 64 KiB is read.
- Top-level lines are `key: value`. A key whose value is empty (after stripping
  whitespace, so `metadata: ` counts) opens a mapping. Lines indented by two spaces
  under it are `key: value` children. A deeper indent, a list item, a flow value
  (`[`, `{`) or a block scalar (`|`, `>`) is a problem with its line number.
- Scalars: a double-quoted value decodes the escapes `\"`, `\\`, `\n` and `\t`. A
  single-quoted value decodes `''`. A plain value is taken as is.
- `type`, `originSessionId` and `modified` are read from `metadata` first, then from
  the top level (the flat form).
- Unknown keys are kept and ignored. Claude Code owns this schema, so a new key must
  not break the import.

`Memory` has these fields: `stem`, `name`, `description`, `type`, `origin` (str or
None), `modified` (str or None), `mtime`.

### 4.3 Mapping

| Memory field | Row cell |
|---|---|
| `type` `feedback` or `project` | kind `decision` |
| `type` `user` | kind `fact` |
| `type` `reference` | skipped (`skipped_reference`); with `--include-reference`, kind `fact` |
| other or missing `type` | skipped as invalid |
| `description` | claim, after `normalise_claim` (line breaks become spaces), then `claim_error` |
| (constant) | confidence `medium` (`DEFAULT_CONFIDENCE`) |
| `modified`, else the file mtime | `since`, the UTC date |
| (none) | `revisit_by` empty |
| file stem | source cell `memory:<label>/<stem>` |

The claim is never cut: a description over 500 characters is skipped with a warning,
because a cut claim states less than the memory does. `since` is the date of the last
write, not of creation. The docs record only the write time (S1).

### 4.4 The source marker

The source cell `memory:<label>/<stem>` already parses today, because the cell is free
text (`src/cairn/decisions.py:316`). Add a grammar only for readers:

```python
MEMORY_SOURCE_RE = re.compile(
    r"^memory:(?P<label>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<stem>[A-Za-z0-9][A-Za-z0-9._-]*)$"
)
```

`DecisionRow.memory` returns `"<label>/<stem>"` or `None`, and `row_json` gains
`"memory"`. `CITATION_RE` and `citations_error` do not change. The marker is not a
citation, and `decide --source` keeps its frame-only rule, so a person cannot type a
`memory:` marker and pass it off as an import. `recall decisions` text output appends
`[memory]` to such a row. This is how a reader tells that the claim is model-written
(S1: "notes Claude writes itself"). A per-row provenance key is not needed: the marker
names the origin exactly, and `provenance` is a per-document key (`src/cairn/provenance.py:3`).

### 4.5 Placement and writing

1. For each memory, find the target. If `origin` is set and
   `ClaudeSource.find_file(origin)` returns a path, the target is that session document.
   Otherwise the target is the project document, and `unknown_session` counts the
   memory.
2. Existing markers: read the tables of the project document, of every target session
   document, and of each Claude record whose `project` equals the directory's
   `project_name_from_dir` label. Collect each row's `memory` value. This scan finds a
   row even after a later run moves its memory to a different session.
3. Group the memories by target. Call `decisions.apply` once per target, with
   `source="claude"`. The project document gets a `find_file` closure that returns its
   path. The `edit` callback runs under the document lock. It does these steps:
   1. Load the ledger once (document lock first, ledger lock second, as
      `src/cairn/withdrawals.py:14-17` requires).
   2. Skip a memory whose marker is in the table (`skipped_existing`).
   3. Skip a memory whose `fingerprint(claim)` is withdrawn (`skipped_withdrawn`). An
      `EmptyClaim` is skipped as invalid. No `--force` exists: an import must never
      lift a withdrawal.
   4. Append the other memories as new rows, numbered from the highest number plus 1.
4. Before the first write, create the project document if it does not exist. Under its
   document lock, check again, then write the template of 5.2 with `atomic_write`.
5. A session document without `## My Notes` makes `apply` print its error and return
   `None`. Count that memory as `failed` and set exit 1. Do not fall back to the project
   document, because that would place one memory in two places over time.
6. `--dry-run` does steps 1 and 2 and the ledger check without locks, then prints.

### 4.6 Idempotency, change and deletion

The key is the marker `memory:<label>/<stem>`. A re-run adds only memories without a row,
whatever the row's state. A struck, superseded or withdrawn row still blocks a re-add.

If a row is active and `withdrawals.normalise_claim(row.claim)` differs from the
normalised current description, the run prints the "changed since" warning and counts
`changed`. It does not edit the row: rows are append-only (`src/cairn/decisions.py:431`),
and only a person decides whether the new text replaces the old claim.

A memory deleted from the directory leaves its row in place. Claude prunes memories to
keep the index under 200 lines (S1), so a deletion there is housekeeping, not a
withdrawal. A row that is wrong is withdrawn with `cairn claude forget N --reason R
--session-id <id or memory:label>`.

### 4.7 recall decisions

- `decisions.memory_records(config) -> list[SessionRecord]` builds one record per
  `<data_root>/decisions/*.md` whose `type` is `claude-memory-decisions`, with these
  values: `source="memory"`, `session_id=<label>`, `title`, `project` and `cwd` from
  the frontmatter.
- `run_recall` adds these records to `store.records(...)` unless `--source` is given
  without `memory`. `add_recall_parser` adds `memory` to the `--source` choices of
  `recall decisions` only.
- `_HEADINGS["memory"] = None`, so the reader reads to the end of the file.
- The text ref uses the full label for `memory` records: `memory:example-app#3`. The
  JSON `ref` is the same.
- The digest overdue list needs no change: imported rows have no `revisit_by`.

`ClaudeSource.find_file` returns the project document path for an id that starts with
`memory:`. Through that, `decide`, `supersede` and `forget` work on project documents.

## 5. Data formats

### 5.1 Row

```text
| 4 | Ship each change as its own PR off a fresh main | decision | medium | 2026-10-30 |  | memory:example-app/release-train |
```

### 5.2 Project document

```markdown
---
type: claude-memory-decisions
native_id: memory:example-app
title: "Claude memory: example-app"
project: example-app
cwd: /tmp/example-app
memory_dir: ~/.claude/projects/-tmp-example-app/memory
provenance: agent
---

# Claude memory: example-app

## My Notes

Rows imported from Claude Code auto memory by cairn claude memory import.

## Decisions

| # | claim | kind | confidence | since | revisit_by | source |
|---|-------|------|------------|-------|------------|--------|
```

`cwd` is written only when the project root is known (current-project mode).
`memory_dir` is written with `~` for the home directory. `provenance: agent` follows
`src/cairn/provenance.py:6`: Claude wrote the text on this machine, so the value is
`agent`, not `imported`.

### 5.3 JSON summary

```json
{
  "schema_version": 1,
  "dry_run": false,
  "directories": [{"memory_dir": "~/.claude/projects/-tmp-example-app/memory", "label": "example-app", "project": "example-app"}],
  "added": 2, "skipped_existing": 5, "skipped_withdrawn": 1, "skipped_reference": 1,
  "unknown_session": 1, "changed": 1, "invalid": 0, "failed": 0,
  "rows": [
    {"memory": "example-app/release-train", "action": "added", "placement": "session",
     "ref": "claude:3f2a9c1e-0000-4000-8000-000000000001#4", "kind": "decision",
     "since": "2026-10-30", "path": "/tmp/data/claude-code-sessions/example-app/2026-10-30-3f2a9c1e.md"}
  ]
}
```

`action` is one of `added`, `existing`, `withdrawn`, `reference`, `invalid` or `failed`.
`placement` is `session` or `project`. `ref` is `null` when nothing was written.

## 6. Failure modes

| Case | Result |
|---|---|
| No memory directory | exit 1, `MEMORY_DIR_MISSING` |
| Empty directory | exit 0, `Imported 0 memories ...` |
| Malformed frontmatter | warning, `invalid` +1, exit 1 at the end |
| Session document table does not parse | `apply` error, `failed` +1, exit 1 |
| Ledger invalid (`LedgerError`) | exit 1 before any write |
| Concurrent import | the document lock serialises the work. The marker check inside `edit` stops a duplicate row |
| Concurrent re-export of a target session | the export keeps the section (`src/cairn/decisions.py:20-25`) |
| Memory written while the import runs | the import reads the file once. The next run sees the new state |
| `autoMemoryDirectory` set | the default directory is empty or missing. The error tells the user to pass `--memory-dir` |

## 7. Tests

`tests/test_claude_memory.py` (temp `HOME`, temp `CLAUDE_CONFIG_DIR`, invented memories
under `tests/fixtures/claude_memory/`):

- `test_reader_nested_metadata`: the nested form gives the type, origin and modified values.
- `test_reader_flat_form`: the top-level `type` and `originSessionId` are read.
- `test_reader_trailing_space_metadata`: `metadata: ` opens the mapping.
- `test_reader_quoted_escapes`: `\"` and `\\` decode.
- `test_reader_rejects_block_scalar`: a problem with its line number.
- `test_dir_name_docs_rule`: `/tmp/a_b.c` becomes `-tmp-a-b-c`.
- `test_dir_name_long_path_glob`: one match is used; two matches exit 1.
- `test_project_dir_name_env`: `CLAUDE_CODE_PROJECT_DIR_NAME` applies only with `CLAUDE_CONFIG_DIR`.
- `test_worktree_resolves_main_root`: a linked worktree maps to the main checkout's directory.
- `test_mapping_kinds`: feedback and project map to decision, user maps to fact, reference is skipped.
- `test_include_reference`: a reference memory becomes kind fact.
- `test_since_from_modified_then_mtime`: both paths.
- `test_long_description_skipped`: 501 characters give a warning, `invalid`, exit 1.
- `test_place_in_exported_session`: the row is in that session's table; other bytes do not change.
- `test_unknown_session_goes_to_project_doc`: the document is created with the template and the row is added.
- `test_rerun_adds_nothing`: the second run reports `skipped_existing` for every memory.
- `test_changed_description_warns`: the warning appears and the row does not change.
- `test_deleted_memory_keeps_row`: the row is still listed.
- `test_withdrawn_fingerprint_skipped`: `skipped_withdrawn` is 1 and the ledger does not change.
- `test_dry_run_writes_nothing`: the tree hash before equals the tree hash after.
- `test_json_summary_shape`: keys, actions and `schema_version`.

`tests/test_decisions.py`:

- `test_memory_marker_property`: the marker parses; `citations` is `[]`.
- `test_decide_source_still_rejects_memory_marker`: exit 2.
- `test_recall_decisions_lists_project_doc`: the ref is `memory:example-app#1` and `[memory]` appears.
- `test_recall_source_filter_memory`: the filter works.
- `test_forget_memory_session_id`: it strikes the row in the project document.

Existing tests that must stay green: `test_public_hygiene` (fixtures invented),
`test_config_examples` (new `decisions` collection), `test_docs_cli` (new fenced
commands), `test_skill_routing` (one new intent row, below), and the docs index test.

## 8. Docs to update

- `docs/memory-import-spec.md`: this spec, without the stack header. Add a row in
  `docs/README.md`.
- `skills/cairn/workflows/log-session.md`: a section "Import Claude memories", with
  `cairn claude memory import --dry-run`, then `cairn claude memory import`, and the
  rule: do not run it with `--all-projects` unless the user asks.
- `skills/cairn/SKILL.md`: add `memory import` to the Claude extras line, and add
  "import claude memories" to the description triggers. Add a routing-eval row for that
  phrase.
- `skills/cairn/reference/claude.md`: the command, its flags and placement.
- `README.md`: one line under the Claude verbs.
- `src/cairn/__main__.py` `_HELP`: one line.
- `src/cairn/config.example.json`: the `decisions` collection.
- `src/cairn/errors.py`: `MEMORY_DIR_MISSING` and `MEMORY_IMPORT_WRITE_FAILED`. Then
  regenerate `docs/error-codes.md`.
- Privacy paragraph (README and reference): memories hold whatever Claude saved,
  including private names. The import writes only under `data_root`, never into a
  repository. A person must not copy a project decisions document into a tracked file.

CHANGELOG entry:

```markdown
### Added
- `cairn claude memory import` adds Claude Code auto-memory notes as `## Decisions`
  rows: into the session that wrote them when cairn exported it, else into
  `<data_root>/decisions/<project>.md`. The source cell is `memory:<project>/<file>`.
  Re-runs add only new memories, warn on changed ones, and skip withdrawn claims.
  `--dry-run`, `--json`, `--all-projects`, `--memory-dir`, `--include-reference`.
- `recall decisions` lists project decisions documents (`--source memory`).
```

## 9. Implementation plan

1. `harnesses/claude.py`: `claude_project_dir_name`, `projects_root`. About 50 lines.
2. `claude_memory.py`: reader, discovery, mapping, `cmd_memory_import`. About 330 lines.
3. `decisions.py`: `MEMORY_SOURCE_RE`, `DecisionRow.memory`, `row_json["memory"]`,
   `memory_records`, the `run_recall` and `add_recall_parser` changes, the text ref and
   `[memory]`. About 70 lines.
4. `sources/claude.py`: the `memory import` subparser in `add_extra_parsers`, and the
   `find_file` override for `memory:`. About 30 lines.
5. `config.example.json`, `errors.py`, `docs/error-codes.md` regeneration. About 25 lines.
6. Tests and fixtures. About 450 lines.
7. Docs and skills (section 8). About 120 lines.
8. `make check`, then `make discriminates` for the new tests.

The estimate is a day if the fixtures come together on the first pass.

## 10. Open questions

1. Read `autoMemoryDirectory` from the settings files? Recommended default: no. The
   setting has five scopes and a trust rule (S1). `--memory-dir` covers it, and the
   missing-directory error names the flag.
2. Import the memory body as well as the description? Recommended default: no. The
   description is the one-line summary that Claude writes to fit the index. A body can
   run to many paragraphs and does not fit a 500-character claim.
3. A `decisions` collection key in the user config when it is missing? Recommended
   default: warn only. `recall decisions` reads the document from disk, so only QMD
   search misses it until the user adds the key.

## 11. As implemented

The surface, placement, idempotency rules, messages and JSON keys of sections 3 to 5
are as written, with these differences.

**Open questions.** All three recommended defaults hold: `autoMemoryDirectory` is not
read (`--memory-dir` covers it and the missing-directory error names the flag), the
note body is not imported, and a missing `decisions` collection only warns.

**Reader.** `cairn/claude_memory.py:parse_head` reads one level of mapping, as 4.2 says.
It also reports these as `frontmatter line <n>: <problem>`, with `n` the file line: an
escape other than `\"`, `\\`, `\n` and `\t`, an unescaped quote inside a quoted
value, tab indentation, and a block that is not closed by `---`. A line that starts
with `#` is a YAML comment and is skipped. A file that is not UTF-8 is `not UTF-8`.

**Mapping.** A memory without a `type` is reported as `no type`, not as
`unknown type ''`. A file stem outside `[A-Za-z0-9][A-Za-z0-9._-]*` is invalid
(`file name '<stem>' cannot form a marker`), because its marker would not parse back.
A `modified` value that is not ISO 8601 falls back to the file mtime, the documented
fallback, without a warning. A naive timestamp is read as UTC.

**Discovery.** When `--git-common-dir` does not end in `.git` (a submodule, for
example), the root is `git rev-parse --show-toplevel`, else the working directory. The
glob for a name over 200 characters matches only directories that hold `memory/`. With
`--memory-dir DIR` the label comes from the parent directory's name when `DIR` is named
`memory`, else from `DIR`'s own name, and `cwd` is not written. `MEMORY_DIR_MISSING`
is also the code for a missing `--memory-dir` and, with `--all-projects`, a missing
projects root. The error for two matching long names has no code.

**Placement and writing.** The project document is created only when at least one
memory is to be added to it. `unknown_session` counts every memory added to (or failed
for) a project document, with or without an `originSessionId`. The "is not exported"
warning names only memories that have one. An `originSessionId` that matches two
exported files is an error for that memory (`failed`), not a fallback.

**Output.** A text `Added` line shows the short reference, for example
`claude:3f2a9c1e#4`. A dry run prints `Would add <document>` without a row number,
because the number is assigned only under the document lock. The summary gains
`; <f> failed` when a write failed. Each JSON row also has `changed` (the "changed
since" warning was printed for it) and `reason` (the problem of an invalid or failed
memory, else `null`).

**Verbs on project documents.** `decide`, `supersede` and `forget` print the whole
`memory:<label>` instead of an 8-character id, and their JSON `ref` is
`memory:<label>#N`. A `memory:<label>` that has no document reaches the existing
"Session file not found" error.

**Error codes.** `MEMORY_DIR_MISSING` has no verify command (no read-only command
checks a memory directory). `MEMORY_IMPORT_WRITE_FAILED` has `cairn recall decisions
--all-projects --source memory --json` as its verify command and is retryable.

**Tests.** `tests/test_claude_memory.py` holds the tests of section 7 plus the
invalid-type, missing-directory, no-fallback and `--all-projects` cases. The
`decide`-on-a-project-document check is in `test_forget_memory_session_id` in
`tests/test_decisions.py`.

## Sources

- S1 https://code.claude.com/docs/en/memory (redirect from
  https://docs.claude.com/en/docs/claude-code/memory), fetched 2026-10-30:
  - "Each project gets its own memory directory at `~/.claude/projects/<project>/memory/`. The `<project>` path is derived from the git repository, so all worktrees and subdirectories within the same repo share one auto memory directory. Outside a git repo, the project root is used instead."
  - "As it works, Claude saves four kinds of notes for itself. Claude records the kind as a `type` field in the memory file's frontmatter": `user`, `feedback`, `project`, `reference`.
  - "To store auto memory in a different location, set `autoMemoryDirectory` in your `settings.json`. It is read from any settings scope: user, project, local, policy, or `--settings`."
  - "If you set `CLAUDE_CODE_PROJECT_DIR_NAME` beside `CLAUDE_CONFIG_DIR`, Claude Code uses that name as the `<project>` directory under `<config dir>/projects/` instead"
  - "The directory contains a `MEMORY.md` index and one topic file per memory" and "`MEMORY.md` acts as an index of the memory directory."
  - "The first 200 lines of `MEMORY.md`, or the first 25KB, whichever comes first, are loaded at the start of every conversation."
  - "When Claude writes a memory file that begins with YAML frontmatter, Claude Code records the write time in a `modified` frontmatter field as an ISO 8601 timestamp."
  - "Auto memory files are plain markdown you can edit or delete at any time."
  - "Auto memory is machine-local."
- S2 https://code.claude.com/docs/en/sessions, "Where transcripts are stored" and "Name the project directory yourself":
  - "`<project>` is your working directory path with non-alphanumeric characters replaced by `-`. For a working directory whose converted name exceeds 200 characters, Claude Code truncates the name to 200 characters and appends a hash of the full path"
  - "Claude Code ignores `CLAUDE_CODE_PROJECT_DIR_NAME` when `CLAUDE_CONFIG_DIR` is unset." and "Use 1-64 letters, digits, hyphens, or underscores".
- S3 https://code.claude.com/docs/en/claude-directory: "If you set `CLAUDE_CONFIG_DIR`, every `~/.claude` path on this page lives under that directory instead."
- S4 same page, "Cleaned up automatically": "the sweep doesn't delete the memory files in a project's auto memory directory, `projects/<project>/memory/`."
- S5 Claude Code changelog (the local cache of the release notes, read 2026-10-30): 2.1.59 "Claude automatically saves useful context to auto-memory. Manage with /memory"; 2.1.63 "Project configs & auto memory now shared across git worktrees of the same repository"; 2.1.74 "Added `autoMemoryDirectory` setting"; 2.1.75 "Added last-modified timestamps to memory files"; 2.1.214 "Added an ISO `modified` timestamp to memory file frontmatter". The changelog has no entry for `originSessionId`.
- S6 A web search for `"originSessionId"` with Claude Code memory returned no documentation of the field. Its meaning (the session that wrote the memory) is inferred from this machine: every one of the 142 distinct ids has an exported cairn session document.
