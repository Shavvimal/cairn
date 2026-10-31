# `cairn recall verify` and routine-session exclusion: implementation spec

Status: implemented in 2.15.0, on top of the provenance, exporter-guard, trusted-hash,
routine-hardening and recall-gaps changes. Section 11 lists where the code differs from
the text below; where they differ, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0, the base this
spec was written against.

Dependencies: none. The `provenance` front matter key is not used; section 4.6 says why. An error-code registry can later map the reason vocabulary in 5.2 to codes.

## 1. Purpose and non-goals

### Purpose

2.9.0 lets a peer agent write a daily journal from `cairn recall digest`, but nothing
checks what the journal cites. A comparable tool measured that, before it added a
mechanical quote check, fewer than half of the quoted spans in its model-written
summaries were present in the source. This change adds:

1. One citation grammar for any agent-written document built from a digest.
2. `cairn recall verify FILE --digest DIR`: every citation resolves to a kept frame
   whose hash still matches, and every quoted span is a substring of a cited frame.
   `--fix` moves failing blocks into `## Unverified`.
3. The example routine gates its journal with `verify`.
4. `recall digest` excludes sessions owned by a routine (`owner: routine:*`), so a
   journal run never becomes input to the next journal.

### Non-goals

- No model call and no grounding judge. Uncited prose is not checked.
- No sentence splitting. The unit of failure is a Markdown block (4.2).
- No paraphrase or near-match tolerance beyond the normalisation in 4.4.
- No change to the digest format, the sidecar schema or `recall frame` output.
- No new config key. No new runtime dependency.

## 2. Current behaviour (main, 2.9.0)

| Fact | Where |
|---|---|
| A digest marker is `<!-- fN TS ROLE -->`; `fN` is the 0-based ordinal in the whole session | `src/cairn/digest.py:146-150`, `docs/recall-digest-spec.md:310-316` |
| Digest front matter `ref` is the full `source:session_id` | `src/cairn/digest.py:284`, `src/cairn/store.py:119-121` |
| Sidecar row: `ordinal, ts, role, offset, length, sha1_8, in_window, kept` | `src/cairn/digest.py:308-323` |
| Digest files match `^[a-z-]+-[A-Za-z0-9]+(\.frames\.jsonl|\.md)$`; the directory holds `.cairn-digest` | `src/cairn/digest.py:56-57`, `src/cairn/digest.py:349-361` |
| `recall frame` checks marker at `anchor + offset`, declared length, same offset in a fresh walk, same `sha1_8`; a mismatch warns and exits 0 | `src/cairn/digest.py:430-469` |
| `_stored_row` scans the sidecar for one ordinal per call | `src/cairn/digest.py:421-427` |
| `recall` subcommands and the shared error handler (exit 1) | `src/cairn/recall.py:56-106`, `src/cairn/recall.py:193-195` |
| Session resolution by full id or unique prefix, source-qualified | `src/cairn/store.py:270-285` |
| Nothing checks a journal's citations; no `verify` subcommand exists | `src/cairn/recall.py:56-95` |
| Template variables are `DAY`, `DATA_ROOT`, `ATTEMPT_DIR`, `DIGEST_DIR` and one per `outputs` key; there is no `{{JOURNAL_PATH}}` | `src/cairn/routine_plan.py:98-110`, `docs/routine-spec.md:343-352` |
| Rendering substitutes inside an argv element, so `{{JOURNAL_DIR}}/{{DAY}}.md` renders | `src/cairn/routine_plan.py:121-149` |
| `validate` exit 0 stamps `done`, exit 3 stamps `empty`, anything else fails the day | `src/cairn/routine_run.py:417-439`, `src/cairn/routine_plan.py:27` |
| Example routine: no `prepare`, `validate: ["true"]`, `outputs.journal_dir` | `src/cairn/config.example.json:21-32` (byte-identical copy `cairn.config.example.json`, enforced by `tests/test_config_examples.py:1-30`) |
| `routine install` warns when the first `validate` element is not on the recorded `PATH` | `src/cairn/routine.py:161` |
| `routine run` invokes the peer with `--owner routine:<name>` | `src/cairn/routine_plan.py:322` |
| `owner` is a preserved-tail key, emitted only when non-empty | `src/cairn/frontmatter.py:36`, `src/cairn/frontmatter.py:41`, `src/cairn/frontmatter.py:341-343` |
| The publisher writes `owner` onto the peer session after export | `src/cairn/invoke.py:531-532`, `src/cairn/mutations.py:209-222` |
| The digest reads `owner` from front matter, else from the owner-form preamble | `src/cairn/digest.py:293-295`, `src/cairn/digest.py:120-126`, `src/cairn/digest_condense.py:46-55` |
| The digest copies `owner` but does not exclude anything by it; the routine spec asked for exclusion and it was not built | `src/cairn/digest.py:376-405`, `docs/routine-spec.md:733-744` |
| The test for the owner preamble expects the routine session to be written | `tests/test_recall_digest_condense.py:285-293` |

So the self-consumption guard does not exist in 2.9.0. A routine run on day D+1 creates
a peer session active on D+1, and the run on D+2 digests it.

## 3. Surface

### 3.1 Citation grammar

A citation names one frame of one session: `[SOURCE:ID fN]`. `SOURCE:ID` is the digest
file's `ref` (full id, or a unique prefix of at least 8 characters). `N` is the ordinal
from the `<!-- fN ... -->` marker. One frame per bracket; cite two frames as
`[claude:3f2a9c1e f11][claude:3f2a9c1e f12]`.

```python
CITATION = re.compile(
    r"\[(?P<source>[a-z][a-z0-9-]*):(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{7,})"
    r" f(?P<ordinal>[0-9]+)\](?!\()"
)
# Anything shaped like an attempt at a citation that CITATION does not match.
NEAR_CITATION = re.compile(r"\[(?:[a-z][a-z0-9-]*:[^\]\s]*\s+)?f[0-9]+[^\]]*\]")
```

The `(?!\()` keeps a Markdown link text `[claude:… f1](url)` out. A `NEAR_CITATION`
match at a position where `CITATION` does not match is a `malformed-citation` failure
(for example `[f12]`, `[claude:3f2a f12]` with a short id, `[claude:3f2a9c1e f12 f13]`).
Grep form for humans: `rg '\[[a-z-]+:[A-Za-z0-9._-]{8,} f[0-9]+\]'`.

Why this form and not `SOURCE:ID ORDINAL` as `recall frame` takes it: a bare
`claude:… 12` is not delimited, so prose numbers become false citations. The bracket
form maps one-to-one onto `recall frame SOURCE:ID N` and onto the digest marker `fN`.

### 3.2 Quote grammar

- Inline quote: a span between `"` and `"`, or `“` and `”`, inside one block, outside
  inline code. Spans shorter than `QUOTE_MIN_CHARS = 15` characters after normalisation
  are not checked (scare quotes, titles) and are counted as `skipped_short`.
- Block quote: a run of lines starting `>` (up to 3 leading spaces). Its text, with the
  `>` and one following space removed per line, is one quote, checked at any length.
  Inline quotes inside a block quote are not checked separately.
- Citation tokens inside a quote are removed before comparison.

### 3.3 CLI

```
cairn recall verify FILE --digest DIR [--json] [--fix]
cairn recall digest DATE ... [--include-routine-sessions]
```

| Flag | Meaning |
|---|---|
| `FILE` | The journal (any Markdown file). Read with `newline=""`. |
| `--digest DIR` | Required. A directory written by `recall digest` (holds `.cairn-digest`). |
| `--json` | One JSON object on stdout (5.1) instead of report lines. |
| `--fix` | Rewrite `FILE` in place, moving failing blocks to `## Unverified` (4.5). |
| `--include-routine-sessions` | `digest` only: keep sessions whose owner starts with `routine:`. |

`verify` takes no `--project` or `--source`: digest refs are full ids and resolve
globally through `store.resolve` (`src/cairn/store.py:270-285`).

### 3.4 Exit codes for `verify`

| Code | When |
|---|---|
| 0 | Every citation and every checked quote verifies. Also: with `--fix`, every failure was moved and the file written. Also: zero citations and zero quotes, with the stderr warning `Warning: FILE has no citations`. |
| 1 | Error: `FILE` missing or unreadable, `DIR` not a digest directory, config error, write failure under `--fix`. Existing handler, `src/cairn/recall.py:193-195`. |
| 2 | argparse usage error. |
| 4 | At least one failure, without `--fix`. |

Never 3: a routine `validate` exit 3 stamps the day `empty` and done
(`src/cairn/routine_run.py:420`, `src/cairn/routine_run.py:432`), so a failing journal
must not be able to return it.

### 3.5 Exact messages

| Case | Message |
|---|---|
| no file | `Error: no such file: FILE` (exit 1) |
| not a digest | `Error: not a digest directory: DIR has no .cairn-digest` (exit 1) |
| report line | `FILE:LINE: REASON: DETAIL` (stdout) |
| summary | `verified C/Ct citations, Q/Qt quotes (S short quotes not checked); F failures` |
| fix summary | `moved M blocks to ## Unverified in FILE` |
| digest exclusion (human output) | `N routine sessions excluded; pass --include-routine-sessions to keep them` |

## 4. Behaviour

### 4.1 Index the digest

1. Refuse `DIR` without `.cairn-digest` (`OUT_MARKER`, `src/cairn/digest.py:56`).
2. For each file matching `_DIGEST_FILE` and ending `.md`, parse the front matter with
   `parse_frontmatter` and read `ref`. Pair it with `<stem>.frames.jsonl`.
3. Load each sidecar once into `{ordinal: row}` (new `digest.load_sidecar(path)`; keep
   `_stored_row` for `recall frame`).

### 4.2 Split the file into blocks

1. Skip a leading `---` front matter block.
2. Ignore everything from the last line that is exactly `## Unverified` (outside a
   fence) to the end of the file. This makes `--fix` idempotent.
3. Skip fenced code (```` ``` ```` or `~~~`, up to 3 leading spaces) entirely.
4. A block is one of: a heading line; a list item with its continuation lines (a nested
   item starts its own block); a table row; a run of `>` lines; a paragraph. A blank line
   ends a block.
5. Mask inline code spans (same-length runs of backticks) with spaces for the scan, so a
   `"` or a citation inside code is not seen. Quote text is still taken from the
   original characters.

### 4.3 Check citations

For each `CITATION` match, in file order:

| Step | Failure reason |
|---|---|
| Match `SOURCE:ID` against indexed refs: same source, session id starts with `ID` | `session-not-in-digest` (0 matches), `ambiguous-session` (more than 1) |
| Sidecar file exists | `sidecar-missing` |
| `ordinal` is a sidecar row | `bad-ordinal` |
| Row has `kept: true` (the frame was in the digest body the peer read) | `frame-not-in-digest` |
| Resolve the record, `read_text`, `locate` (cached per ref) | `export-unreadable` (no session, legacy, corrupt) |
| Same check as `recall frame` (marker at `anchor + offset`, length, offset, `sha1_8`) | `hash-mismatch` |

The fourth row is the cairn form of a citation outside the gathered evidence: a frame
that exists but that the digest did not show cannot have been read.

Extract the comparison in `run_frame` (`src/cairn/digest.py:451-460`) into
`digest.frame_unchanged(text, anchor, frame, row) -> bool` and the warning detail into
`digest.changed_detail(ref, ordinal, row, frame) -> str`, and call both from `run_frame`
and from `verify`. `run_frame` output stays byte-identical.

### 4.4 Check quotes

1. The quote's citations are those in its own block. A block quote with none takes the
   citations of the block directly before it (at most one blank line between). A quote
   with none fails `quote-uncited`.
2. If none of those citations resolved, the quote is `unchecked`; the citation failure
   already fails the block.
3. Normalise both the quote and each resolved frame body (`Frame.body`,
   `src/cairn/frames.py:49-64`): Unicode NFC; `“ ” „` to `"`, `‘ ’` to `'`; every run of
   whitespace to one space; strip. Comparison is case-sensitive.
4. Pass when the normalised quote is a substring of any resolved cited frame. Otherwise
   `quote-not-found`. The raw export body is the reference, not the condensed digest,
   so a quote that spans a `[... N chars omitted]` marker fails, as it should.

### 4.5 `--fix`

1. Every block with any failure moves. A block quote attached to a moved block moves
   with it.
2. Remove the block's lines and the one blank line after it, if any. Every other byte of
   the file is unchanged.
3. If the file has no `## Unverified` heading, append `\n## Unverified\n\n` (first
   adding a final newline if missing). Then append, per moved block in file order:
   `<!-- cairn:unverified line=N reason=R1,R2 -->`, the block's lines verbatim, a blank
   line. `N` is the block's first line in the file before the rewrite.
4. Write with `atomic_write` (`src/cairn/storage.py:43-59`). When nothing failed, do not
   write (the mtime stays).
5. A second `--fix` finds nothing to move (4.2 step 2) and writes nothing.

### 4.6 Routine-owned sessions in the digest

1. Constant `ROUTINE_OWNER_PREFIX = "routine:"` in `src/cairn/mutations.py`, used by
   `invoke_argv` (`src/cairn/routine_plan.py:322`) and by the digest.
2. In `run_digest` (`src/cairn/digest.py:376-405`), before reading a body: when
   `record.metadata.get("owner")` starts with the prefix and the flag is off, skip the
   record and add `{"ref", "owner"}` to `excluded`.
3. After `build`: when the resolved owner (`digest.py:293`, front matter or preamble)
   starts with the prefix, drop the digest the same way. The preamble path covers a
   session exported before the publisher wrote `owner`.
4. Drop a subagent digest whose `parent` is an excluded ref.
5. A nested peer under a routine cannot exist: `routine run` refuses to start inside a
   peer, and invoke refuses `CAIRN_INVOKE_DEPTH >= 1` (`src/cairn/routine_run.py:527`, `docs/routine-spec.md:1783`).
6. Exit 3 when every candidate was excluded, as for any empty day
   (`src/cairn/digest.py:418`).

Why `owner` and not a `provenance` key: every `cairn invoke` peer session is agent
work, and a manual review is a day event the journal must keep. Only `owner` names the
routine, it survives re-export as a preserved-tail key, and it has a fallback in the
preamble. The journal file itself is not a session record (it lives in the store-less
`journal` collection, `src/cairn/config.example.json:35`, `src/cairn/store.py:252-264`),
so it never enters a digest.

### 4.7 Routine integration

The example routine gains a digest `prepare` and a `verify` `validate`:

```json
"prepare": ["cairn", "recall", "digest", "{{DAY}}", "--all-projects", "--json", "--out", "{{DIGEST_DIR}}"],
"validate": ["cairn", "recall", "verify", "{{JOURNAL_DIR}}/{{DAY}}.md", "--digest", "{{DIGEST_DIR}}"]
```

`{{JOURNAL_DIR}}/{{DAY}}.md` is the journal path; there is no `{{JOURNAL_PATH}}`
variable. The prompt must tell the peer to write that path. The example uses plain
`verify` so a bad journal fails the day and is retried (up to 3 counted attempts). The
workflow doc shows `--fix` as the alternative policy: keep the journal and flag the
failures. The prompt snippet in the doc:

> Cite every claim as `[REF fN]`: REF is the `ref` value from the digest file's front
> matter, N the number in a `<!-- fN ... -->` marker. Quote only text copied exactly from
> one frame, in double quotes or a `>` block quote, in the same list item as its
> citation. Before you finish, run `cairn recall verify {{JOURNAL_DIR}}/{{DAY}}.md
> --digest {{DIGEST_DIR}}` and fix or delete every reported line.

The profile must allow `Bash(cairn recall verify *)` for the peer to self-check.

### 4.8 Worked examples

Digest: the fixture of `docs/recall-digest-spec.md` 8.2, ref
`claude:3f2a9c1e-7b4d-4e2a-9c1e-000000000001`, frames 8 to 13 kept, 14 sidecar rows.
f8 is `The retry test still fails on CI. Find out why and fix it.`, f11 contains
`I moved the cap after the jitter.`

| Journal line 5 | Result |
|---|---|
| `- Fixed the backoff cap: "I moved the cap after the jitter" [claude:3f2a9c1e f11]` | citation resolves (kept, hash equal); quote is a substring of f11; verified |
| `- The full suite went green [claude:3f2a9c1e f42]` | `journal.md:5: bad-ordinal: ordinal 42 not in claude-3f2a9c1e7b4d.frames.jsonl (14 frames)` |
| `- The user asked to "fix the retry test before the release" [claude:3f2a9c1e f8]` | `journal.md:5: quote-not-found: "fix the retry test before the release" is not in f8 of claude:3f2a9c1e-7b4d-4e2a-9c1e-000000000001` |

With `--fix` on the third case, line 5 and its following blank line leave the body and
the file ends:

```markdown
## Unverified

<!-- cairn:unverified line=5 reason=quote-not-found -->
- The user asked to "fix the retry test before the release" [claude:3f2a9c1e f8]

```

The blank line after each moved block is part of the format (4.5 step 3).

## 5. Data formats

### 5.1 `verify --json`

```json
{
  "schema_version": 1,
  "file": "/tmp/journal/2026-03-28.md",
  "digest": "/tmp/digest",
  "ok": false,
  "citations": {"total": 3, "verified": 2, "failed": 1},
  "quotes": {"total": 2, "verified": 1, "failed": 1, "unchecked": 0, "skipped_short": 0},
  "failures": [
    {"line": 6, "block": [6, 6], "kind": "citation", "reason": "bad-ordinal",
     "text": "[claude:3f2a9c1e f42]", "ref": "claude:3f2a9c1e-7b4d-4e2a-9c1e-000000000001",
     "ordinal": 42, "detail": "ordinal 42 not in claude-3f2a9c1e7b4d.frames.jsonl (14 frames)"}
  ],
  "moved": 0
}
```

`ref` is `null` when the session did not resolve to one digest file. `ordinal` is the
cited `N` for every citation failure except `malformed-citation`, where it is `null`. A
quote failure carries `ref` and `ordinal` only when it names exactly one frame. `kind` is
`citation` or `quote`.

### 5.2 Reason vocabulary

`malformed-citation`, `session-not-in-digest`, `ambiguous-session`, `sidecar-missing`,
`bad-ordinal`, `frame-not-in-digest`, `export-unreadable`, `hash-mismatch`,
`quote-not-found`, `quote-uncited`. Fixed; a test pins the set.

### 5.3 `digest --json`

`{"sessions": [...], "skipped": [...], "excluded": [{"ref": "codex:…", "owner": "routine:daily"}]}`.
The new key is additive; `sessions` and `skipped` keep their shape
(`src/cairn/digest.py:408`).

## 6. Failure modes

| Case | Result |
|---|---|
| Digest directory from another day | Citations fail `session-not-in-digest` or `bad-ordinal`; nothing guessed |
| Codex ids share their first 8 characters (UUIDv7, `docs/recall-digest-spec.md:633-640`) | `ambiguous-session`; the prompt asks for the full ref |
| Export re-rendered after the digest (Codex result attached late) | `hash-mismatch`; re-run `prepare` and the peer |
| Journal written with CRLF | Lines are split with `keepends=True`; `--fix` keeps each line's ending |
| Quote contains Markdown escapes (`\*`) | Fails; the prompt says copy text exactly. Decision 2 in section 10 |
| `--fix` on a read-only file | exit 1, file unchanged |

## 7. Tests

`tests/test_recall_verify.py`, reusing `DigestFixture` and `fixture_document` from
`tests/test_recall_digest.py:100-131`:

- `test_grammar_matches_examples`: `CITATION` matches the three 4.8 tokens and not `[x](u)` link text.
- `test_malformed_citation_reported`: `[f12]` and a 4-character id give `malformed-citation`.
- `test_verified_journal_exits_0`: example 1, exit 0, summary line exact.
- `test_bad_ordinal_line_and_exit_4`: example 2, exact report line, exit 4.
- `test_quote_not_found`: example 3, exact report line.
- `test_frame_not_kept_fails`: citing f0 (out of window) gives `frame-not-in-digest`.
- `test_session_not_in_digest` and `test_ambiguous_prefix`.
- `test_hash_mismatch_after_rerender`: edit frame 4 as `test_frame_warns_on_rerender` does (`tests/test_recall_digest.py:229`).
- `test_quote_normalisation`: curly quotes, line breaks and double spaces still verify.
- `test_short_quote_not_checked`: a 10-character quote counts in `skipped_short`.
- `test_blockquote_takes_previous_citation` and `test_uncited_quote_fails`.
- `test_code_is_ignored`: a citation in a fence and a quote in inline code are not checked.
- `test_unverified_section_ignored`.
- `test_fix_moves_only_failing_blocks`: verified lines are byte-identical before and after.
- `test_fix_is_idempotent`: a second run writes nothing and exits 0.
- `test_fix_appends_to_existing_section`.
- `test_json_shape_and_reason_vocabulary`.
- `test_not_a_digest_dir_exits_1` and `test_exit_code_never_3`.
- `test_recall_frame_output_unchanged`: `run_frame` after the refactor, same stdout and stderr.

In `tests/test_recall_digest.py` (and the condense module for the preamble case):

- `test_routine_owner_excluded_by_default`: front matter `owner: routine:daily`.
- `test_routine_owner_from_preamble_excluded`: change `tests/test_recall_digest_condense.py:285-293` to pass `--include-routine-sessions`, and add the default case.
- `test_include_routine_sessions_keeps_them`.
- `test_other_owner_kept`: `owner: review` is written.
- `test_subagent_of_routine_session_excluded`.
- `test_only_routine_sessions_is_empty_day`: exit 3, `excluded` lists it.

Elsewhere: `tests/test_config_examples.py` asserts the example `validate` starts
`["cairn", "recall", "verify"]` and `prepare` starts `["cairn", "recall", "digest"]`;
`tests/test_routine.py` adds `test_example_validate_renders_journal_path`.

## 8. Docs to update

- `docs/journal-verify-spec.md`: this spec.
- `skills/recall/SKILL.md:60-76`: the grammar, `verify` and its exit codes, the routine exclusion.
- `skills/cairn/workflows/routine.md:17-27` and `:62-88`: example `prepare`/`validate`, the prompt snippet, plain vs `--fix`, `Bash(cairn recall verify *)` in the profile.
- `src/cairn/config.example.json` and `cairn.config.example.json`: the example routine and the `//routines` comment.
- `src/cairn/__main__.py:57-62`: one `verify` help line.
- `README.md:481-490`: one `verify` line and one sentence on routine exclusion.
- `docs/routine-spec.md:733-744`: one line that 5.4 is now built, pointing to the new spec.

## 9. Implementation plan

1. `src/cairn/digest.py`: `load_sidecar`, `frame_unchanged`, `changed_detail`; `run_frame` uses them (+35, -10). Run `make check`.
2. `src/cairn/mutations.py`: `ROUTINE_OWNER_PREFIX`; `routine_plan.py:322` uses it (+3).
3. `src/cairn/digest.py`: exclusion in `run_digest`, `excluded` key, human line (+40). `recall.py`: the flag (+4). Digest tests (+90).
4. New `src/cairn/verify.py`: grammar, block splitter, checks, report, `--fix` (about 320). `recall.py`: `verify` subparser and dispatch (+15). `tests/test_recall_verify.py` (about 350). Run `make check`.
5. Config examples, config-example and routine tests (+20).
6. Docs from section 8. Run `make check`; read the diff for home paths and private names.

About 900 lines in all, half of them tests: one day of work.

## 10. Decisions

1. A journal with zero citations and zero quotes passes: `verify` warns on stderr and
   exits 0. A `--min-citations N` flag can follow if journals come back uncited.
2. Normalisation does not drop Markdown backslash escapes; exact copy is the rule, and
   the prompt says so.
3. The example routine fails and retries the day rather than using `--fix`, because a
   retry costs one peer round and a silent `## Unverified` section is easy to miss.

## 11. As built

- `CITATION_RE` and `NEAR_CITATION_RE` live in `src/cairn/verify.py`, with `REASONS`,
  `QUOTE_MIN_CHARS` and `SCHEMA_VERSION`. Other modules import them from there.
- A `NEAR_CITATION_RE` match followed by `(` is Markdown link text, not a malformed
  citation, the same rule `(?!\()` applies to a valid one.
- A citation that fails `hash-mismatch` does not count as resolved for its quotes: they
  are `unchecked`, because the cited frame is no longer the frame the digest showed.
- A quote whose block has only malformed citations is `unchecked`, not `quote-uncited`:
  the citation failure already fails the block.
- `quotes.total` counts the checked quotes (`verified + failed + unchecked`); short quotes
  are counted only in `skipped_short`. `citations.total` includes malformed citations.
- For `quote-not-found` with more than one resolved citation, the detail lists each
  frame (`f8 of REF or f9 of REF`) and `ref` and `ordinal` are `null`.
- Details for the other reasons: `session-not-in-digest`: `no session in DIR matches
  SOURCE:ID`; `ambiguous-session`: `SOURCE:ID matches N sessions (REFS); cite the full
  ref`; `sidecar-missing`: `no sidecar NAME in DIR`; `frame-not-in-digest`: `fN of REF is
  not in the digest body (kept: false)`; `export-unreadable`: `REF: ERROR`;
  `hash-mismatch`: the `recall frame` warning text without `Warning: `;
  `malformed-citation`: `TEXT is not a citation; write [SOURCE:ID fN] with an id of at
  least 8 characters`; `quote-uncited`: `"QUOTE" has no citation in its block`.
- `--fix` checks that `FILE` is writable before it writes, because the atomic rename
  would otherwise replace a read-only file. New lines use the file's first line ending.
- A line that starts with three backticks opens a fence only when no backtick follows
  on that line (CommonMark: a backtick fence's info string holds no backtick), so
  `` ```code``` `` at the start of a line is inline code and the rest of the file is checked.
- A straight `"` opens a quote only at the start of a block or after a space or `([{-/`,
  and before a non-space; it closes only after a non-space. So `12"` is an inch mark.
  An opener with no closer is skipped and the scan goes on.
- A table is found by its delimiter row (`--- | ---`, with or without outer pipes): the
  header line, the delimiter and each following line up to a blank, fenced, heading or
  `>` line are one block each, like a `|` row.
- `--fix` takes the document lock (`storage.document_lock`) on the resolved path, re-reads
  the file and writes nothing when it changed since the check read it (`Error: FILE
  changed while verify ran; nothing written, run it again`, exit 1). An editor that saves
  between the re-read and the rename can still lose that save; the window is the
  `atomic_write` call. A symlink is written through, and the file keeps its mode.
- `ok` in `--json` is `false` when any check failed, also under `--fix`, which then exits
  0 and reports the moves in `moved`.
- A subagent's parent is looked up among every record of the digest sources, before the
  project, day and `--min-msgs` filters, and its owner read from front matter or the
  owner-form preamble. A routine's subagent is excluded even when the routine session
  itself is not digested (it ran the day before, sits in another project, or has too few
  user messages).
- An indented `>` line under a list item belongs to the item: each run of such lines is
  one block quote, checked at any length against the item's own citations (before or
  after it), so "in the same list item as its citation" verifies. Inline quotes inside
  that run are not checked separately.
- A code fence that never closes runs to the end of the file, as in CommonMark, and
  verify warns `Warning: FILE:N: code fence never closes; the lines after it are not
  checked` on stderr. The exit code does not change.
- `NEAR_CITATION_RE` stays on one line and inside one bracket pair, so an unclosed `[f1`
  does not swallow a later valid citation, and a report line never holds a newline.
- Failures are reported in line order. A quote checked against the same frame cited twice
  names that frame once and keeps its `ref` and `ordinal`.
- Lines end at `\n` only (`\r\n` stays on its line), so a form feed or U+2028 in the
  journal does not shift line numbers.
- `sidecar-missing` also covers a sidecar that exists but cannot be read or parsed; the
  detail is then `cannot read NAME: ERROR`.
- `hash-mismatch` also covers a cited ordinal past the end of a re-rendered export; the
  detail is then `REF frame N changed since the digest (the export now has M frames); the
  export was re-rendered`.
- `--fix` adds no second blank line before `## Unverified` when the text already ends
  with one, and none at all when every block moved.
- The prompt block in `skills/cairn/workflows/routine.md` also tells the peer the journal
  path. The example `scheduled` profile allows `Bash(cairn recall verify *)`.
