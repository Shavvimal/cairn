# The decisions table (`## Decisions`): implementation spec

Status: implemented in the PR that adds this file. Section 11 lists where the
implementation differs from the text above it and records the answers to the open
questions; where section 11 differs from an earlier section, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0.

**Dependencies.** The frame citation grammar `[<source>:<shortid> f<N>]` (for example
`[claude:3f2a9c1e f12]`, N a 0-based frame ordinal) is the one the journal citations
use. This change holds its own `CITATION_RE` constant in `src/cairn/decisions.py`
(section 5.2), in one place, so that a shared definition can replace it later. A later
withdrawal (forget) change builds on the row ids and the strike marker defined here.

## 1. Purpose and non-goals

A session establishes decisions and facts, but cairn has no structured place for them.
The annotations today are a free-text `## My Notes` section and the frontmatter fields
`comments`, `status`, `tags`, `rating`. "What did we decide about X" is therefore a
full-text search over prose, and nothing tells you when a decision is due for review.

This change adds:

1. A `## Decisions` markdown table directly after `## My Notes` in a session document,
   with the columns `# | claim | kind | confidence | since | revisit_by | source`.
2. Corrections in place: `~~old claim~~ superseded by #4` keeps the old row and strikes it.
3. Two verbs on every conversation source: `cairn <source> decide` and
   `cairn <source> supersede`.
4. Round trip: re-export preserves the section as it preserves `## My Notes`. A table
   that cairn wrote comes back byte for byte. A hand-edited table keeps every line's text,
   but its line endings become LF and trailing blank space at the end of the section goes.
5. Reading: `cairn recall decisions [DATE_EXPR] [--overdue] [--json]`, and an `overdue`
   list in the `cairn recall digest --json` object.

Non-goals:

- **Notes collections.** A store-less collection (for example a routine's journal) has no
  source class, no `find_file` and no annotation verbs (`docs/routine-spec.md:1350-1365`,
  `src/cairn/admin.py:161`). Supporting it means a new path-addressed verb. Not cheap; out.
- **Peer-exchange documents** get preservation only (one changed line in
  `src/cairn/exchange.py:404`), no verbs.
- **Cross-document supersede.** The replacement row always lands in the document that holds
  the struck row. A write to two files cannot be atomic.
- **Grading or resolution** of a claim, a calibration score, or a model judge.
- **Forget and withdrawal.** A later change owns these.
- **Hiding a row from QMD search.** The table is plain markdown and is indexed like the rest
  of the document.
- **Recording who wrote a row.** Provenance, if added, is document-level only.

## 2. Current behaviour (main at 2.9.0)

| Area | Behaviour | Where |
|---|---|---|
| Annotation verbs | `note`, `close`, `log` are registered for every conversation source with `--session-id` | `src/cairn/cli.py:98-115` |
| Mutation scope | Every mutation rewrites only the frontmatter tail. It never touches the body | `src/cairn/mutations.py:1-12`, `src/cairn/frontmatter.py:348-373` |
| Lock and write | Mutations take `document_lock`, read with `newline=""`, then `atomic_write` | `src/cairn/mutations.py:88-102` |
| Missing file | `Error: Session file not found for <id8>`, return False | `src/cairn/mutations.py:84-86` |
| Ambiguous id | `FileExistsError` from `find_record`, printed as `Error: ...` | `src/cairn/sources/base.py:217-225`, `src/cairn/mutations.py:81-83` |
| Current session | Explicit `--session-id`, else the harness id (claude, codex) or the source env var, else the single active session. Refuses mixed host hints and more than one active session | `src/cairn/sources/base.py:336-383` |
| No session | `Error: No session_id. Use --session-id[ or set VAR]`, exit 1 | `src/cairn/sources/base.py:385-394`, `:476-479` |
| Empty note | `Error: No note text provided`, exit 2 | `src/cairn/sources/base.py:480-483` |
| My Notes extraction | Starts at the line `## My Notes`, ends at the next `## ` line outside a code fence. Returns None if the conversation heading comes first. An unclosed fence falls back to a scan without fences | `src/cairn/frontmatter.py:189-236` |
| My Notes emission | Verbatim if preserved, else a placeholder | `src/cairn/frontmatter.py:376-385` |
| Document order | head, tail, title, `body_extra`, My Notes, `body_main` | `src/cairn/sources/base.py:396-423` |
| Re-export load | `load_existing` reads frontmatter and My Notes | `src/cairn/sources/base.py:248-267` |
| Granola | `conversation_heading = None`. The transcript comes after My Notes | `src/cairn/sources/granola.py:628`, `:677-682` |
| Exchange docs | Own `notes_block` and `extract_my_notes_section` calls | `src/cairn/exchange.py:344`, `:404` |
| Frame offsets | Measured from the `## Conversation` anchor, so they survive edits above it | `src/cairn/frames.py:54-56`, `src/cairn/digest.py:451-454` |
| Anchor rule | A `## Conversation` line counts only when a turn marker follows it | `src/cairn/frames.py:143-155` |
| Recall CLI | Subcommands `list`, `expand`, `digest`, `frame`. A cwd project filter is the default for `list` and `digest` | `src/cairn/recall.py:56-131` |
| Digest JSON | `{"sessions": rows, "skipped": skipped}`. Exit 3 when zero sessions are written | `src/cairn/digest.py:406-418` |
| Record lookup | `resolve` takes `source:prefix` and refuses an ambiguous prefix | `src/cairn/store.py:270-285` |
| Effect of a `## ` heading in My Notes | Ends My Notes, so the heading and everything after it is lost on re-export | `src/cairn/frontmatter.py:232-233` |

A user who writes `## Decisions` by hand today loses that section on the next export.

## 3. Surface

### 3.1 CLI

```
cairn <source> decide CLAIM... [--kind K] [--confidence C] [--revisit-by DATE]
                               [--source CITE] [--session-id ID] [--json] [-q]
cairn <source> supersede N CLAIM... [--kind K] [--confidence C] [--revisit-by DATE]
                                    [--source CITE] [--session-id ID] [--json] [-q]
cairn recall decisions [DATE_EXPR...] [--overdue] [--include-struck] [--as-of YYYY-MM-DD]
                       [--project P | --all-projects] [--source S]... [--json]
```

`<source>` is `claude`, `codex`, `antigravity`, `cursor` or `granola`. `CLAIM...` is joined
with single spaces, as `note` joins its text (`src/cairn/sources/base.py:480`).

| Flag | Default | Values |
|---|---|---|
| `--kind` | `decision`. On `supersede`, the kind of row N | `decision`, `fact`, `commitment`, `hunch` (argparse `choices`) |
| `--confidence` | `medium` | `high`, `medium`, `low` (argparse `choices`) |
| `--revisit-by` | empty | `YYYY-MM-DD`, on or after `since` |
| `--source` | empty | one or more frame citations separated by single spaces |
| `--session-id` | current session (section 4.1) | full id, unique prefix or `source:id` |
| `--as-of` | today, UTC | the date that `--overdue` and the `overdue` field use |

**Choice of kinds.** The set is closed at four values. Each value answers a different review
question:

- `decision`: a choice made between options. Question: is it still the right choice?
- `fact`: something observed and checkable, such as a tool version's behaviour. Question:
  is it still true?
- `commitment`: something someone promised to do. `revisit_by` is its due date.
- `hunch`: a belief that is not yet checked. It is kept apart so that nobody reads it as a
  fact.

A comparable tool records a user decision as a fact with a `commitment` kind and has no log
of decisions. cairn keeps `decision` and `commitment` separate because "what we chose" and
"what we owe" get reviewed differently. A predictive "bet" kind is left out because cairn
does not resolve or grade claims.

**Choice of confidence scale.** cairn uses three words, not numbers. A comparable tool uses
weights in 0.05 steps and caps self-reported weights at 0.75, and that precision pays off
only through grading. cairn does not grade claims (section 1), so a number would claim
precision that nothing measures. Three words are quicker to read in a table and simpler to
filter.

### 3.2 Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `decide`, `supersede` | row written | no session, file missing or ambiguous, no My Notes, table does not parse, row N missing or already struck | empty claim, claim holds `~~`, claim over 500 characters, bad date, bad citation, argparse error |
| `recall decisions` | listing printed, zero rows included | config error, bad DATE_EXPR or `--as-of` | argparse error |
| `recall digest` | unchanged: an overdue list never changes the exit code (`src/cairn/digest.py:418`) | | |

### 3.3 Exact messages (stderr, prefixed `Error: `)

- `claim is empty`
- `claim must not contain '~~' (it marks a struck row)`
- `claim is longer than 500 characters (got <n>)`
- `--revisit-by must be YYYY-MM-DD on or after <since>, got '<value>'`
- `--source must be frame citations like [claude:3f2a9c1e f12], separated by spaces, got '<value>'`
- `<file name> has no ## My Notes section; re-export it with cairn <source> export --all`
- `cannot parse ## Decisions in <file name>: line <n>: <reason>`
- `no row #<n> in ## Decisions of <file name>`
- `row #<n> is already struck (<marker or 'no marker'>)`
- `no row number is left after #999999999`
- The existing messages from section 2 for a missing session, a missing file and an
  ambiguous id.

Success messages go to stdout and are silenced by `-q`:
`Added #4 to claude:3f2a9c1e: <claim>` and `Struck #3; added #4 to claude:3f2a9c1e: <claim>`.

### 3.4 Files and config

No config key and no frontmatter key. The single new file location is the section inside
existing documents. A new module, `src/cairn/decisions.py`, holds the grammar, the parser,
the body mutation and the reader.

## 4. Behaviour

### 4.1 `decide`

1. Resolve the session with `_require_session_id(args.session_id)`, the path `note` uses
   (`src/cairn/sources/base.py:476-479`). This gives the same environment variable, the
   same harness detection and the same refusal when more than one session is active.
2. Validate the claim: join the words, turn each line break that `str.splitlines` knows
   (CR, LF, VT, FF, U+001C to U+001E, U+0085, U+2028, U+2029) into a space, collapse runs of
   spaces, strip. Then check the length and `~~` rules (exit 2). Validate `--revisit-by`
   against `since`, the UTC date now. Validate `--source` against `^CIT( CIT)*$`.
3. Find the file with `find_file`, as `_mutate_file` does (`src/cairn/mutations.py:79-86`).
   Factor that lookup into `mutations.locate(find_file, session_id) -> Path | None` and use
   it in both places.
4. Under `locked(document_lock(path))`, read with `newline=""` and split the text with
   `splitlines(keepends=True)`.
5. Run `frontmatter.scan_annotations(lines, conversation_heading=source.conversation_heading)`
   (section 5.4). It returns the line ranges of My Notes and Decisions.
6. If there is no My Notes section, exit 1.
7. If there is a Decisions section, parse it (section 5.2). Any problem: exit 1 with the
   first problem's line and reason. The new number is the highest `#` plus 1. Insert the
   canonical row directly after the last table row.
8. If there is no Decisions section, insert these lines at the end of My Notes, before
   the next `## ` line or at EOF: `## Decisions`, blank, header, separator, row, blank.
   Before that, make sure the preceding line ends with a newline. Use the newline that ends
   the `## My Notes` line (`\r\n` or `\n`).
9. `atomic_write` the joined lines. All bytes from the conversation anchor to EOF stay the
   same, so frame offsets and digest sidecars stay valid
   (`src/cairn/frames.py:54-56`).
10. Print the success line, or with `--json` print the row object (section 5.5) wrapped
    as `{"ref", "path", "row"}`.

### 4.2 `supersede N`

Steps 1 to 7 are the same, except that the section must exist, row N must exist (else exit 1)
and row N must be active (else exit 1). Then, in one write:

- Replace row N's line with its canonical rendering, with the claim cell set to
  `~~<claim>~~ superseded by #<M>`. The other cells keep their parsed values.
- Append row M = highest `#` + 1. Its kind is `--kind`, or row N's kind. Its source cell is
  `supersedes #N`, followed by a space and the `--source` value if one was given. Its
  `revisit_by` is never inherited, because the old date is the reason for the review.

### 4.3 Re-export (round trip)

`ConversationSource.load_existing` (`src/cairn/sources/base.py:264-266`) and
`exchange._read_existing` (`src/cairn/exchange.py:404`) call
`frontmatter.extract_annotations` in place of `extract_my_notes_section`. That function
returns the My Notes text, plus `"\n\n"` and the Decisions text when a Decisions section
follows My Notes, with each part right-stripped. The value goes through the existing
`my_notes` parameter, and `notes_block` emits it unchanged
(`src/cairn/frontmatter.py:378-379`). For that reason no adapter's `render_document`
changes. Extracting from the output gives the same string again, so the result is a fixed
point. A document without a Decisions section re-exports byte-identical to 2.9.0.
The extraction splits on line breaks and right-strips each part, as it always did for My
Notes. So a hand-edited section with CRLF line endings, or with blank lines or trailing
spaces after its last row, comes back with LF endings and without that trailing space.
The line text is kept. A table in the canonical layout that `decide` writes is unchanged.

### 4.4 `recall decisions`

1. Build the records as `list` does, including the cwd project default. Add `"decisions"` to
   the tuple at `src/cairn/recall.py:126` and to the loop at `:97`, which adds `--project`,
   `--all-projects` and `--source`.
2. For each record, call `decisions.read_table(path, heading)`, where `heading` is `None`
   for granola and `## Conversation` for every other source. The reader reads lines only
   until the frame anchor: the first `## Conversation` line followed by
   `<!-- cairn:turn `, the rule at `src/cairn/frames.py:150-155`. For granola, it reads to
   EOF. Then it runs `scan_annotations` and the parser on that prefix.
3. A row problem gives the stderr line `Warning: <path>: line <n>: <reason>`. The row is
   skipped and the other rows are kept.
4. Filters: DATE_EXPR (through `parse_date_range`) keeps the rows whose `since` falls in
   `[start, end)`. Struck rows are hidden unless `--include-struck` is given. `--overdue`
   keeps the active rows where `revisit_by < as_of`.
5. Sort by `(since, session ref, number)`. Human output is one line per row:
   `claude:3f2a9c1e#4  decision  medium  2026-10-30  revisit 2027-01-15  <claim>`, with
   `OVERDUE` at the end of an overdue row, then `<n> decisions`.

### 4.5 Digest `overdue`

In `run_digest`, after the subagent filter (`src/cairn/digest.py:376-377`) and after the
routine exclusion, collect the overdue rows from those records, in or out of the window,
with `as_of = end.date()`. Unless `--include-routine-sessions` is given, a session owned by
a routine (front matter `owner: routine:<name>`, or an owner-form preamble on a session of
the day) and the subagents of such a session give no overdue rows. An out-of-window
session whose only owner mark is a preamble is not read for it, because the overdue scan
stops at the conversation anchor. For the digest of day D, that is D + 1, so a row counts as overdue
when `revisit_by <= D`. The result depends only on the documents, so the output stays
deterministic. Add `"overdue": [...]` to the JSON object. Human output adds one
`Overdue: <ref>  revisit_by <date>  <claim>` line per row. The exit codes and the files
written do not change.

## 5. Data formats

### 5.1 Section and table (canonical, written by cairn)

```
## Decisions

| # | claim | kind | confidence | since | revisit_by | source |
|---|-------|------|------------|-------|------------|--------|
| 1 | <claim cell> | <kind> | <confidence> | YYYY-MM-DD | [YYYY-MM-DD] | <source cell> |
```

A row is written as `"| " + " | ".join(cells) + " |"`, so an empty cell is `|  |`. In a
cell, `\` is escaped as `\\` and `|` as `\|`.

### 5.2 Grammar

```python
HEADING = "## Decisions"
HEADER = "| # | claim | kind | confidence | since | revisit_by | source |"
SEPARATOR = "|---|-------|------|------------|-------|------------|--------|"
SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|){7}$")          # reader
CANONICAL_ROW_RE = re.compile(                                     # writer output
    r"^\| (?P<n>[1-9][0-9]{0,8})"
    r" \| (?P<claim>(?:[^|\\]|\\.)+)"
    r" \| (?P<kind>decision|fact|commitment|hunch)"
    r" \| (?P<confidence>high|medium|low)"
    r" \| (?P<since>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r" \| (?P<revisit_by>[0-9]{4}-[0-9]{2}-[0-9]{2})?"
    r" \| (?P<source>(?:[^|\\]|\\.)*) \|$"
)
CELL_RE = re.compile(r"\s*((?:[^|\\]|\\.)*?)\s*\|")                # reader, after the leading "|"
STRUCK_RE = re.compile(r"^~~(?P<claim>~?(?:[^~]~?)*)~~(?: (?P<marker>\S.*))?$")
SUPERSEDED_RE = re.compile(r"^superseded by #(?P<by>[1-9][0-9]{0,8})$")
SOURCE_RE = re.compile(
    r"^(?:supersedes #(?P<supersedes>[1-9][0-9]{0,8})(?![0-9]))?\s*(?P<rest>.*)$"
)
CITATION_RE = re.compile(
    r"\[(?:claude|codex|antigravity|cursor|granola):[A-Za-z0-9][A-Za-z0-9._-]* f(?:0|[1-9][0-9]*)\]"
)
```

Reader rules:

- The section is the lines from `## Decisions` up to the next `## ` line outside a fence,
  or EOF.
- Blank lines are skipped. The first non-blank line must equal `HEADER` once trailing
  spaces are stripped. The next line must match `SEPARATOR_RE`.
- Each following non-blank line must start and end with `|` and hold exactly seven cells
  under `CELL_RE`. The reader is lenient about whitespace and strict about values.
- `#`: a positive integer of at most nine digits (`row number must be at most 999999999`
  otherwise), unique in the table. Numbers need not be contiguous. A `superseded by #M`
  marker or a `supersedes #N` prefix with more digits is not read as a number: the marker
  makes the row `struck`, and the prefix stays plain source text.
- The claim cell matches `STRUCK_RE`, or else contains no `~~`. A struck claim is any text
  without `~~`, so it may start or end with one `~` (`~~ends with~~~ superseded by #2`).
- `kind` and `confidence` must be in their closed sets. `since` and `revisit_by` must be
  real dates.
- A `superseded by #M` marker must name a row that exists.
- Any other line is a problem: `expected a table row`.

The row state follows from the claim cell:

- `active`: the claim is not struck.
- `superseded`: the claim is struck and the marker matches `SUPERSEDED_RE`.
- `struck`: the claim is struck with no marker or with any other marker. The reader keeps
  the marker text exactly as written.

### 5.3 Row identity and the contract for a later withdrawal change

- A row's local id is its `#`. It is never renumbered and never reused by cairn. A row that
  is deleted by hand can free its number, and the spec documents this.
- The global id is `<source>:<native_id>#<n>`, for example
  `claude:3f2a9c1e-5b7d-4c11-9e0a-1b2c3d4e5f60#4`. `store.resolve` accepts a unique prefix
  before the `#` (`src/cairn/store.py:270-285`).
- The strike form is always `~~<claim>~~[ <marker>]`. 07 writes the marker
  `superseded by #M` only. A later change adds its own marker (for example a `withdrawn` marker
  with a reason) without a change to the format. The 07 reader already keeps any marker
  exactly as written, classifies it as `struck`, and hides it by default in
  `recall decisions` and in the overdue lists.

### 5.4 `scan_annotations`

`frontmatter.scan_annotations(lines, *, conversation_heading) -> (notes, decisions)`. Each
value is a `(start, end)` line range or `None`. It generalises `_scan_my_notes`
(`src/cairn/frontmatter.py:210-236`) and keeps that function's fence and unclosed-fence
behaviour. A Decisions section counts only when its heading is the line where My Notes
ends. As a result, a `## Decisions` line in a summary, in native meeting notes, or in the
conversation is never captured. Without that rule, each re-export would duplicate the
line. `extract_my_notes_section` keeps its signature and output.

### 5.5 JSON row (`recall decisions --json`, digest `overdue`, `decide --json`)

```json
{
  "ref": "claude:3f2a9c1e-5b7d-4c11-9e0a-1b2c3d4e5f60#4",
  "session": "claude:3f2a9c1e-5b7d-4c11-9e0a-1b2c3d4e5f60",
  "number": 4,
  "claim": "Keep confidence as three words until grading lands",
  "kind": "decision",
  "confidence": "medium",
  "since": "2026-10-30",
  "revisit_by": "2027-01-15",
  "source": "supersedes #3 [claude:3f2a9c1e f40]",
  "citations": ["[claude:3f2a9c1e f40]"],
  "supersedes": 3,
  "state": "active",
  "marker": null,
  "superseded_by": null,
  "overdue": false,
  "title": "Stack the review layers",
  "project": "cairn",
  "path": "/tmp/data/claude-code-sessions/cairn/2026-10-28-3f2a9c1e.md"
}
```

`recall decisions --json` prints
`{"as_of": "2026-10-30", "decisions": [rows], "problems": [{"path", "line", "reason"}]}`.
The digest prints `{"sessions": [...], "skipped": [...], "overdue": [rows]}`.

### 5.6 Worked example

Before. The document is `claude:3f2a9c1e`, and the conversation is shortened:

```
# Stack the review layers

## My Notes

<!-- Add your notes here. This section is preserved across syncs. -->

## Decisions

| # | claim | kind | confidence | since | revisit_by | source |
|---|-------|------|------------|-------|------------|--------|
| 1 | One PR per layer, each based on the layer below | decision | high | 2026-10-28 |  | [claude:3f2a9c1e f12] |
| 2 | The digest is byte-identical for a fixed export | fact | high | 2026-10-28 |  |  |
| 3 | Keep confidence as three words, not numbers | decision | medium | 2026-10-29 | 2026-11-15 | [codex:9b1d07aa f4] |

## Conversation

<!-- cairn:turn 57 -->
```

The command, run on 2026-10-30 UTC:

```
cairn claude supersede 3 "Keep confidence as three words until grading lands" \
  --revisit-by 2027-01-15 --source "[claude:3f2a9c1e f40]"
```

After. Only the line of row 3 changes and one line is added. The text from
`## Conversation` to EOF is byte-identical:

```
| 3 | ~~Keep confidence as three words, not numbers~~ superseded by #4 | decision | medium | 2026-10-29 | 2026-11-15 | [codex:9b1d07aa f4] |
| 4 | Keep confidence as three words until grading lands | decision | medium | 2026-10-30 | 2027-01-15 | supersedes #3 [claude:3f2a9c1e f40] |
```

stdout: `Struck #3; added #4 to claude:3f2a9c1e: Keep confidence as three words until grading lands`.
Row 3 still has `revisit_by` 2026-11-15, but it is struck, so it is never overdue.

## 6. Failure modes

| Case | Result |
|---|---|
| The current session is not exported yet | The same as `note`: `Session file not found`, exit 1. The skill tells the agent to export first |
| More than one active session, or mixed host hints | The existing refusal (`src/cairn/sources/base.py:355-378`), exit 1 |
| A row was edited by hand into a bad shape | `decide` and `supersede` refuse with the line number and write nothing. `recall` warns and skips that row. Re-export keeps every line's text; line endings become LF and trailing blank space at the end of the section goes, as for My Notes |
| `## Decisions` is not directly after My Notes | Not recognised, not parsed. Re-export behaves as on 2.9.0 (section 2, last row) |
| A fenced `## Conversation` in My Notes | The reader stops at the real anchor only, because of the turn-marker rule |
| A claim holds `\|` or `\\` | Escaped on write and unescaped on read. The round trip is tested |
| A concurrent export | Serialised by `document_lock`, as at `src/cairn/sources/base.py:298` |
| CRLF before the anchor | The inserted lines use the newline of the `## My Notes` line |
| An older cairn re-exports the document | It drops the section. The CHANGELOG notes that this cannot be downgraded |
| A large store | `recall decisions` reads each file only up to its anchor. Measured in section 11 |

## 7. Tests

New module `tests/test_decisions.py`, with temp directories only:

- `TestGrammar.test_canonical_row_matches_regex_and_round_trips`: render, parse and render again give the same line.
- `TestGrammar.test_pipe_and_backslash_escape_round_trip`
- `TestGrammar.test_superseded_marker_sets_state_and_target`
- `TestGrammar.test_unknown_marker_is_struck_and_kept_verbatim`: the contract for a later withdrawal change.
- `TestGrammar.test_unknown_kind_is_a_problem_with_line_number`
- `TestGrammar.test_duplicate_number_is_a_problem`
- `TestGrammar.test_dangling_superseded_by_is_a_problem`
- `TestGrammar.test_lenient_whitespace_strict_values`
- `TestSections.test_decisions_directly_after_my_notes_is_captured`
- `TestSections.test_decisions_heading_in_summary_or_conversation_is_ignored`
- `TestSections.test_extract_annotations_is_a_fixed_point`
- `TestSections.test_extract_my_notes_section_output_unchanged`
- `TestDecide.test_first_decide_creates_section_before_conversation`
- `TestDecide.test_next_number_is_max_plus_one`
- `TestDecide.test_bytes_from_anchor_to_eof_unchanged_and_frames_locate_identically`
- `TestDecide.test_refuses_malformed_table_without_writing`
- `TestDecide.test_rejects_strike_marker_long_claim_bad_date_bad_citation_exit_2`
- `TestDecide.test_granola_section_lands_before_transcript`
- `TestSupersede.test_worked_example_exact_bytes`: the example from section 5.6.
- `TestSupersede.test_missing_row_exit_1`
- `TestSupersede.test_already_struck_row_exit_1`
- `TestSupersede.test_inherits_kind_not_revisit_by`
- `TestRecallDecisions.test_hides_struck_rows_unless_include_struck`
- `TestRecallDecisions.test_date_expr_filters_on_since`
- `TestRecallDecisions.test_overdue_is_strictly_before_as_of`
- `TestRecallDecisions.test_json_shape_and_problems_list`
- `TestRecallDecisions.test_reader_stops_at_frame_anchor`

Changes to existing tests:

- `tests/test_sources.py`: add `decide` and `supersede` to `CANONICAL_VERBS` (line 25).
- `tests/test_shared_store.py`:
  - `test_reexport_preserves_decisions_byte_identical`: decide, export, then compare the bytes.
  - `test_document_without_decisions_is_unchanged`
- `tests/test_recall_digest.py`:
  - `test_digest_json_lists_overdue_relative_to_day_after`
  - `test_overdue_does_not_change_exit_3`
- `tests/test_invoke.py`: `test_exchange_rerender_preserves_decisions`.
- `tests/test_frontmatter.py`: `test_scan_annotations_unclosed_fence_fallback`.

## 8. Docs to update

- `docs/decisions-table-spec.md`: this spec, in the style of `docs/routine-spec.md`.
- `skills/cairn/SKILL.md`: add `decide` and `supersede` to the verb table (lines 44-46),
  and add `## Decisions` to "Preserved on export" (line 151).
- `skills/cairn/workflows/log-session.md`: a "Decisions" section that says when to use each
  kind, how to cite a frame, how to supersede, and to export before you annotate a live
  session.
- `skills/cairn/reference/{claude,codex,antigravity,cursor,granola}.md`: the
  "Content sections" and "Preserved on export" lines.
- `skills/recall/SKILL.md`: `cairn recall decisions`, and `overdue` in the digest block.
- `src/cairn/__main__.py` `_HELP`: the list of shared verbs (line 52) and one
  `recall decisions` line.
- `README.md`: one example next to `cairn codex note` (line 197).
- `skills/cairn/schema/tags.yaml`: no change. Kinds are not tags.

## 9. Implementation plan

1. `src/cairn/frontmatter.py`: add `DECISIONS_SECTION`, `scan_annotations` and
   `extract_annotations`. Make `_scan_my_notes` delegate to them. About 60 lines.
2. `src/cairn/decisions.py` (new): the constants, `DecisionRow`, `parse_section`,
   `render_row`, `add`, `supersede`, `read_table`, `overdue`, `row_json`, `run_recall`.
   About 330 lines.
3. `src/cairn/mutations.py`: factor out `locate`. About 15 lines.
4. `src/cairn/sources/base.py`: `load_existing` uses `extract_annotations`. Add `cmd_decide`
   and `cmd_supersede`. About 60 lines.
5. `src/cairn/cli.py`: the two subparsers. About 30 lines.
6. `src/cairn/exchange.py:404`: one changed line.
7. `src/cairn/recall.py`: the `decisions` subparser and dispatch. About 30 lines.
8. `src/cairn/digest.py`: the `overdue` key and the human lines. About 20 lines.
9. Tests (section 7): about 480 lines. Then docs (section 8): about 150 lines.

Keep every edit to `recall.py`, `digest.py` and `base.py` additive, because other
changes touch the same files.

## 10. Open questions

1. **Confidence scale.** Use three words or 0.05-step numbers? Recommended default: three
   words (section 3.1). If grading lands later, a number column can be added next to the
   words.
2. **Overdue rows for the routine peer.** The digest JSON goes to `prepare.out`, not to the
   digest directory that the peer reads (`docs/routine-spec.md:934-935`). Should the digest
   also write `overdue-decisions.md`? Recommended default: JSON only in 07. Add the file
   when a journal prompt uses it.
3. **Read cost of `recall decisions`.** The reader reads each document up to its anchor, but
   nobody has measured this on a large store. Should 07 add a frontmatter count to skip
   files that hold no table? Recommended default: no. Measure on a real store first, and
   add the count only if one run takes longer than about 2 s.

## 11. Implementation notes and answers

Answers to the open questions:

1. **Confidence scale:** three words, `high`, `medium`, `low`.
2. **Overdue rows for the routine peer:** JSON only. The digest adds the `overdue` key to
   its JSON object and `Overdue:` lines to its human output. It writes no new file.
3. **Read cost:** no frontmatter count. `TestRecallDecisions.test_read_cost_on_two_thousand_documents`
   builds 2,000 invented documents (about 83 KB each, 166 MB in all, a table in every
   second one) in a temp store. One `cairn recall decisions --overdue --json` run over
   them took 0.17 s to 0.19 s (three runs, macOS, a warm page cache). That is far below
   the 2 s threshold. The test asserts less than 10 s.

Where the implementation differs from the sections above:

- **The anchor rule.** An exported `## Conversation` heading is followed by a blank line
  and then the first `<!-- cairn:turn ` marker (`src/cairn/frames.py:34`). The reader
  stops at the first such heading outside a code fence, or at a heading that ends the
  file, which is the rule of `frames.locate`. Fence tracking means a fenced copy of the
  anchor in My Notes does not stop the reader early. The worked example in section 5.6
  shows the blank line.
- **The inserted section.** The section goes after the last non-blank line of My Notes,
  after one blank line, and a blank line separates it from the next heading. The result
  is the layout that a re-export writes, so `decide` followed by an export is
  byte-identical.
- **Unclosed fences.** `scan_annotations` finds My Notes exactly as 2.9.0 did, including
  the fallback for an unclosed fence. The Decisions section has its own fallback, so an
  unclosed fence inside it ends the section at the next heading instead of changing how
  My Notes is found. `extract_my_notes_section` output does not change.
- **Writer API.** `decisions.apply(find_file, session_id, edit, *, source,
  conversation_heading, id_field)` returns the written rows and the native id;
  `decisions.mutate_table` is the same call reduced to a boolean. `edit` gets the parsed
  rows and returns the full new list. A changed row replaces its own line only. A row
  with a new number is inserted after the last row. An edit cannot remove a row or
  reuse a number. Unchanged rows keep their exact bytes, so a hand-formatted row is not
  rewritten into canonical form.
- **Parser API.** `decisions.parse(text)` returns the rows of a document and raises
  `TableProblem` on the first problem. `decisions.render(rows)` returns the canonical
  header, separator and row lines. `DecisionRow` has `number`, `claim` (unescaped, without
  the strike or the marker), `kind`, `confidence`, `since`, `revisit_by` (empty when
  absent), `source`, `struck` and `suffix` (the marker text), plus the derived `state`,
  `marker`, `superseded_by`, `supersedes` and `citations`.
- **Reader problem wording.** The reasons are: `expected the table header`,
  `expected the table separator`, `the section has no table header and separator`,
  `expected a table row`, `expected 7 cells, got <n>`,
  `row number must be a positive integer, got '<v>'`, `duplicate row #<n>`,
  `claim is empty`, `claim holds '~~' outside a strike`,
  `unknown kind '<v>'; use one of decision, fact, commitment, hunch`,
  `unknown confidence '<v>'; use one of high, medium, low`,
  `since must be a date YYYY-MM-DD, got '<v>'`,
  `revisit_by must be a date YYYY-MM-DD, got '<v>'`, and
  `row #<n> is superseded by #<m>, which does not exist`.
- **`recall decisions` registration.** The subcommand registers its own `--project`,
  `--all-projects` and `--source` flags in `decisions.add_recall_parser` and is
  dispatched before the shared record selection in `recall.main`, so the shared tuples
  in `recall.py` do not change. The project default is the current directory, as for
  `list`. Human output prints `revisit -` for a row without `revisit_by`, and a struck row
  (with `--include-struck`) as `~~claim~~ [marker]`.
- **`--as-of` error.** A bad value prints `Error: --as-of must be YYYY-MM-DD, got '<v>'`
  and exits 1.
- **Exchange documents.** `exchange._read_existing` uses `extract_annotations`, so a
  Decisions section written by hand into a peer-exchange document survives a re-render.
- **Tests.** The tests listed for `tests/test_decisions.py` are there. The granola case is
  tested on `decisions.rewrite` with a meeting-shaped document, because the test store has
  no granola collection.

