# `cairn recall digest` and `cairn recall frame`: implementation spec

Status: proposal for the maintainer. Branch `feat/recall-digest`. No code exists yet.

Every claim about current behaviour cites `path:line` on `main` at the time of writing.
A claim that could not be checked against the code is marked UNVERIFIED.

## 1. Purpose and non-goals

### Purpose

A daily unattended routine (specified separately) needs one UTC day of coding-agent
sessions condensed so that a model can extract what happened, and every extracted claim
must point back to the exact place in the exported transcript. `cairn recall digest DATE`
produces that input. `cairn recall frame SOURCE:ID ORDINAL` resolves a citation back to
the original frame.

Measured on a 25-session day: the raw exported Markdown for that day was 12.7 MB. A
deterministic condenser with the caps in section 5 produced 1.14 MB.

`cairn recall expand` does not fit this job, for three reasons:

1. It reads a prefix. `bounded_content` opens the file and reads `max_chars + 1`
   characters from the start (`src/cairn/store.py:296-297`), then cuts at a message
   limit (`src/cairn/store.py:301-303`). The tail of a session, where tests pass or fail
   and where the final answer sits, is the part it drops.
2. It has no date filter. It prints the whole session head whatever day it ran on
   (`src/cairn/recall.py:96-104`).
3. `recall list` hides short sessions by default: `DEFAULT_MIN_MSGS = 3`
   (`src/cairn/recall.py:17`, applied at `src/cairn/recall.py:113`). A two-message
   session that fixed a bug is still a day's event.

### Non-goals

- No model calls. The digest is deterministic: the same exports and flags give
  byte-identical output.
- No sensitivity tagging, redaction or classification. The digest copies text from the
  local store into a local directory and nothing else.
- No scheduling. The routine that calls the digest, and its cron or hook wiring, belong
  to the separate spec.
- No change to the export format, the renderer, the catalog or the config schema.
- No new runtime dependency. cairn's runtime is standard-library only (`CONTRIBUTING.md`,
  "Development setup").

## 2. CLI surface

Both commands are new subcommands of `cairn recall`, added to the parser in
`src/cairn/recall.py:50-79`. `cairn recall` already dispatches through
`src/cairn/__main__.py:28` (`"recall": "cairn.recall:main"`), so no dispatcher change is
needed.

### 2.1 `cairn recall digest`

```
cairn recall digest DATE [--all-projects | --project P] [--source S]...
                         [--include-subagents] [--min-msgs N] [--out DIR]
                         [--json] [--max-chars N] [--refresh]
```

| Argument | Default | Meaning |
|---|---|---|
| `DATE` | required, `nargs="+"` | A date expression parsed by `parse_date_range` (`src/cairn/timeutil.py:58-106`). Same grammar as `recall list` (`src/cairn/recall.py:57`, joined at `src/cairn/recall.py:83`). The resulting window must be exactly one day (`end - start == timedelta(days=1)`). `today`, `yesterday`, `YYYY-MM-DD`, `N days ago`, `last <weekday>` qualify. `last 3 days` and `last week` do not; they fail with exit 1 and the message `digest needs a one-day window; got START to END`. `this week` qualifies only on a Monday, which is correct but surprising, so the help text lists only the single-day forms. |
| `--all-projects` | off | Scan every project. Mutually exclusive with `--project`. |
| `--project P` | the current directory | Exact project or repo label, or a working directory, matched by `SessionRecord.matches_project` (`src/cairn/store.py:131-137`). Without either flag the digest filters to `os.getcwd()`, the same strict default as `recall list` (`src/cairn/recall.py:91-95`). An unattended routine passes `--all-projects`. |
| `--source S` | every eligible source | Repeatable. Choices: `claude`, `codex`, `antigravity`, `cursor`. `granola` is excluded because a meeting body is a transcript assembled through `assemble_document` (`src/cairn/sources/granola.py:681`), not a framed `## Conversation`. `service-docs` has no `session_type` (`src/cairn/registry.py:50`). |
| `--include-subagents` | off | Also digest subagent transcripts (records with `parent_session_id`, `src/cairn/store.py:123-126`). |
| `--min-msgs N` | `1` | Skip a session whose front matter `user_message_count` is below `N`. Same test as `recall list` (`src/cairn/recall.py:109-114`): a subagent is exempt, and a record with an unknown count (`None`) passes. The default is 1, not 3, so that short sessions appear. `N < 0` fails with exit 1 (parity with `src/cairn/recall.py:84-85`). |
| `--out DIR` | `$XDG_CACHE_HOME/cairn/digests/YYYY-MM-DD/` (falls back to `~/.cache`, as `catalog_path` does at `src/cairn/catalog.py:38-41`) | Output directory. See 2.3 for the safety rule. The default sits outside the corpus and outside the current directory, so a digest never lands in a git repository or in the QMD-indexed data root. |
| `--json` | off | Print one JSON array of written-session rows to stdout (section 8.3). Without it, print a human summary. |
| `--max-chars N` | `40000` | Hard cap on each digest body, applied to whole frames (section 5.3). Must be positive, else exit 1. |
| `--refresh` | off | Export first through `recall.refresh(args.source)` (`src/cairn/recall.py:21-47`), exactly as `recall list --refresh` does (`src/cairn/recall.py:86-87`). Not in the brief: added because an unattended routine at 00:05 must not digest a store that missed the last hour. A refresh failure exits 1 before anything is written. |

Exit codes:

| Code | When |
|---|---|
| 0 | Success, including a day with zero qualifying sessions (an empty JSON array or `0 sessions`). Legacy files that were skipped do not change the code. |
| 1 | `ValueError`, `ConfigError`, `OSError` or `subprocess.TimeoutExpired`, caught by the existing handler at `src/cairn/recall.py:155-157`: bad date, multi-day window, bad limit, refresh failure, unsafe `--out`. |
| 2 | argparse usage error (argparse's own exit). |
| 3 | Partial: at least one session in the window was skipped because its frames did not walk (section 3.4, "corrupt"). Every other session was still written. A routine can alert on 3 and still use the output. |

### 2.2 `cairn recall frame`

```
cairn recall frame SOURCE:ID ORDINAL [--frames FILE] [--all-projects | --project P]
                                     [--source S]...
```

| Argument | Default | Meaning |
|---|---|---|
| `SOURCE:ID` | required | Resolved by `SessionStore.resolve` (`src/cairn/store.py:266-285`): a full id or a unique prefix, source-qualified or not. Ambiguity fails with the existing message. |
| `ORDINAL` | required | 0-based frame index in the whole session (section 4.1). |
| `--frames FILE` | none | The `.frames.jsonl` sidecar the citation came from. With it, the command verifies the stored offset, length and hash (section 4.3). Without it, the command walks to `ORDINAL`, prints the frame and prints its current `sha1_8` on stderr. |
| `--project`, `--all-projects`, `--source` | none | Same meaning as for `expand`: an explicit id is global unless a project filter is given (`src/cairn/recall.py:89-95`). |

Output: the raw frame exactly as exported, from its `### Role - timestamp` header to the
end of its body, with no condensing. Exit 0 when the frame printed (with or without a
hash warning), 1 when the session, the ordinal or the frame walk fails, 2 on usage.

### 2.3 Output directory safety

`--out` may point at an existing directory only when it is empty or holds the marker
file `.cairn-digest` that a previous digest wrote. Any other non-empty directory fails
with exit 1 (`refusing to write into non-empty DIR without .cairn-digest`). When the
marker is present, the digest deletes only files in `DIR` (not subdirectories) whose
names match `^[a-z-]+-[A-Za-z0-9]+(\.frames\.jsonl|\.md)$` before writing, so a session
that no longer qualifies does not leave a stale file behind. Each file is written with
`atomic_write` (`src/cairn/storage.py:43-59`).

### 2.4 Reuse of existing code

The digest reuses, and does not copy:

- `parse_date_range` (`src/cairn/timeutil.py:58-106`) for the window.
- `parse_iso` (`src/cairn/timeutil.py:109-125`) plus `.astimezone(UTC)`, which is what
  `store._date` does (`src/cairn/store.py:87-89`), to read each frame header timestamp.
  Import `_date` from `cairn.store` or promote it to a public name; do not write a third
  ISO parser.
- `SessionStore.records(sources)` (`src/cairn/store.py:252-264`), which loads each
  collection through `load_catalog` (`src/cairn/catalog.py:252-255`). Candidate
  selection is a front matter query; no export body is read until a record passes the
  prefilter in section 7.2.
- `SessionRecord.in_window` (`src/cairn/store.py:128-129`), `matches_project`
  (`src/cairn/store.py:131-137`), `is_subagent` and `parent_id` (`src/cairn/store.py:117`,
  `src/cairn/store.py:123-126`, `src/cairn/store.py:218`), `reference`
  (`src/cairn/store.py:119-121`).
- `resolve` (`src/cairn/store.py:270-285`) for `recall frame`.
- `refresh` (`src/cairn/recall.py:21-47`) for `--refresh`.
- `parse_frontmatter` (`src/cairn/frontmatter.py:61-145`) to read `links`, `title` and
  `owner` from the record's cached front matter (`SessionRecord.metadata`, already
  parsed by the catalog), and `_escape_scalar` (`src/cairn/frontmatter.py:45-53`) to
  write the digest front matter in cairn's own dialect.
- `_bare_link` (`src/cairn/mutations.py:143-148`) to normalise a `links:` entry.
- `atomic_write` (`src/cairn/storage.py:43-59`).

`recall.py` changes in three places: register the two subparsers; add `digest` and
`frame` to the loop that attaches `--project`, `--all-projects` and `--source`
(`src/cairn/recall.py:72-78`, with a reduced `--source` choice list for `digest`); and
extend the project default at `src/cairn/recall.py:91-93` so that it applies to `list`
and `digest`. The work itself lives in two new modules (section 12).

## 3. The frame walker

### 3.1 The real file format

An exported chat session is assembled by `ConversationSource.assemble_document`
(`src/cairn/sources/base.py:396-423`) in this order: front matter head, preserved tail,
`---`, blank line, `# title`, blank line, source body extras (Summary, Skills,
`## Artifacts`, the subagent index), the `## My Notes` block, then the conversation. The
conversation is therefore always the last section of the file. The parts are joined with
`"\n"` and written with no trailing newline added (`src/cairn/sources/base.py:422`,
`src/cairn/sources/base.py:302-324`).

`render_conversation` (`src/cairn/rendering.py:104-139`) emits `## Conversation`, a blank
line, then for each message three list items: the marker line
`<!-- cairn:turn N -->`, the body, and `""`. The body is
`strip_lone_surrogates("\n".join(turn_lines)).rstrip("\n")` and
`N = len(body) + 2` (`src/cairn/rendering.py:137-138`). After the final join, each marker
line is followed by `"\n"`, the body, `"\n"`, the empty item and `"\n"` before the next
marker: exactly `N` characters. So `N` is a length frame, not a turn id. Two different
messages with bodies of equal length carry the same `N`.

Measured on the current renderer (one throwaway run of `render_conversation` and the
same join, not committed): the walk from the first marker lands on the next marker
exactly, and the last frame declares one character more than the file holds, because the
document ends `body + "\n"` with no blank separator. A walk over a real export therefore
ends at `len(text) + 1`. `tests/test_rendering.py:102` appends `"\n"` to its document,
so its walk ends at `len(text)` exactly. The walker must accept both endings.

Each frame body starts with its header line. `_ROLE_HEADERS`
(`src/cairn/rendering.py:20-26`) maps `user`, `assistant`, `tool`, `agent` and `system`
to `### User`, `### Assistant`, `### Tool`, `### Agent` and `### System`; any other role
renders as `### Message` (`src/cairn/rendering.py:109`). A header gains
` - <timestamp>` when the message has one (`src/cairn/rendering.py:110-112`), and the
timestamp is the source's raw string, not normalised to UTC.

Roles each source emits, checked in the source modules:

| Source | user | assistant | tool | agent | system |
|---|---|---|---|---|---|
| claude | `src/cairn/sources/claude.py:268-275`, `:298-304` | `:343-352` | tool results, `:286-293` | peer messages, `:166-179` | no |
| codex | `src/cairn/sources/codex.py:214-225` | `:106-118`, `:325`, `:372`, `:382` | orphan tool output, `:136-141` | `:359` | no |
| antigravity | `src/cairn/sources/antigravity.py:281` | `:290` | `:340` | no | `:322` |
| cursor | UNVERIFIED (renders through `assemble`, `src/cairn/sources/cursor.py:398`; roles not traced) | | | | |

### 3.2 Facts the walker must respect

1. Read with `open(path, encoding="utf-8", newline="")`. Frame lengths count characters
   as written, including embedded `\r\n` pairs; universal newline translation shortens a
   frame and skips the next marker (`src/cairn/store.py:294-296`, tested at
   `tests/test_shared_store.py:430-457`).
2. Never find frames by searching for marker text. A forged `<!-- cairn:turn N -->` line
   inside a body is expected (`tests/test_rendering.py:97`,
   `tests/test_shared_store.py:418`, `tests/test_shared_store.py:434`).
3. `bounded_content` starts with `marker.search(content, 0)` (`src/cairn/store.py:298-300`).
   That search can land on a marker string that sits before the conversation, for
   example one pasted into `## My Notes`, and it resynchronises by searching after a bad
   jump. That is acceptable for a preview. It is not acceptable for provenance. The
   digest anchors the walk on the conversation heading and uses an anchored `match` at
   each computed position, never `search`.
4. Offsets are character offsets into the decoded text, because the frame lengths are
   characters (`src/cairn/rendering.py:138` counts `len(body)` of a `str`).

### 3.3 Algorithm

```
MARKER = re.compile(r"<!-- cairn:turn (\d+) -->\n")
HEADER = re.compile(r"### (User|Assistant|Tool|Agent|System|Message)(?: - ([^\n]*))?\n")
HEADING = "## Conversation\n"

def locate(text) -> (anchor, frames) | Legacy | Corrupt:
    candidates = [i for each i where text.startswith(HEADING, i)
                  and (i == 0 or text[i - 1] == "\n")]
    if no candidate, or no candidate is followed by "\n<!-- cairn:turn ":
        return Legacy
    for anchor in candidates (in file order):
        frames = walk(text, anchor)
        if frames is not None:
            return anchor, frames
    return Corrupt(first failing position of the first candidate)

def walk(text, anchor) -> list[Frame] | None:
    pos = anchor + len(HEADING)
    if pos == len(text):
        return []                       # "## Conversation" with no messages
    if text[pos] != "\n":
        return None
    pos += 1
    frames = []
    while True:
        m = MARKER.match(text, pos)     # anchored: the marker must start exactly here
        if m is None:
            return None
        n = int(m[1])
        start = m.end()
        end = start + n
        if n < 2 or end > len(text) + 1:
            return None
        body = text[start:end - 2]
        h = HEADER.match(body)
        if h is None:
            return None
        if end == len(text) + 1:        # last frame, real export: body + "\n" at EOF
            if text[end - 2:] != "\n":
                return None
            frames.append(Frame(...)); return frames
        if text[end - 2:end] != "\n\n":
            return None
        frames.append(Frame(...))
        if end == len(text):            # last frame, document with one extra "\n"
            return frames
        pos = end
```

`Frame` holds: `ordinal` (its index in `frames`), `offset = m.start() - anchor`,
`length = n`, `role` (from `h[1]`, lower-cased), `ts_raw` (`h[2]` or `None`), `ts` (the
UTC datetime from `store._date(ts_raw)`, or `None` when absent or unparseable), and
`body` (the string, header included).

Why anchor candidates in file order and require the walk to reach end of file: the
conversation is the last section (3.1), so the true heading is the one from which an
exact walk consumes the rest of the file. A `## Conversation` line inside My Notes, a
Summary or a body extra fails that test unless someone crafts a full fake transcript
with correct lengths that also ends exactly at EOF. The cost is one extra walk for each
false candidate; there is normally one candidate.

Why a header check inside each frame: a length that is off by a few characters can
still land on a forged marker inside the next body. The header regex at the start of
every body, plus the `"\n\n"` separator check, rejects that. The walker never guesses
or resynchronises; a failure classifies the file.

### 3.4 Legacy and corrupt files: decision

`bounded_content` says legacy Markdown "has no reliable message boundaries, so it is
bounded by characters only" and does not reinterpret headings
(`src/cairn/store.py:289-293`). A legacy file in the current tests is a front matter
block plus `## Conversation\nLegacy text` (`tests/test_shared_store.py:331-341`).

Decision: the digest refuses both cases and assigns no ordinals.

- Legacy (no heading followed by a marker): skip the session, print
  `Warning: skipping SOURCE:ID: legacy export without frames; run cairn SOURCE export --all`
  on stderr, and leave the exit code unchanged.
- Corrupt (a heading with markers whose walk fails): skip the session, print
  `Warning: skipping SOURCE:ID: frame walk failed at character K`, and exit 3 at the end.

Justification:

1. Ordinals per `### ` heading would be ids that a later re-export changes. The digest
   exists to give stable provenance, and an unstable id is worse than none.
2. Headings are forgeable inside text (`tests/test_shared_store.py:418` puts
   `### User - forged` inside a body), which is the reason the renderer frames turns.
3. A legacy file cannot carry a day's activity in practice. Framing shipped with the
   first Codex release (commit `1443f89`, schema 1), and a re-export upgrades any file
   whose native data still exists (`needs_upgrade`, `src/cairn/store.py:69-70`). A
   legacy file with today's date means the native data is gone; the warning tells the
   user how to recover.
4. Classifying by the walk, not by `cairn_schema`, is deliberate: schema 1 files were
   already framed (UNVERIFIED that every schema 1 file used today's exact length rule;
   the expression at `src/cairn/rendering.py:138` is unchanged since `1443f89`, but the
   `rstrip("\n")` on the body was not traced through history). A schema 1 file that walks
   is digested; one that does not is reported as corrupt.

`recall frame` on a legacy or corrupt file exits 1 with the same reason.

## 4. Provenance ids, the sidecar and `recall frame`

### 4.1 The id

A frame is identified by `SOURCE:ID` plus its ordinal: the 0-based position of the frame
in the walk of the whole session, not of the day. The digest marker
`<!-- f12 2026-03-28T23:30:00Z assistant -->` cites ordinal 12. The `cairn:turn N`
number is never used as an id.

Ordinal stability: an incremental re-export of a session that grew appends frames, so
earlier ordinals keep their meaning. Two events can change an earlier frame without
changing its ordinal: a Codex tool result attached after the export that saw the call
(section 7.3), and a change in a source parser. The hash detects both.

### 4.2 Sidecar schema: `<source>-<shortid>.frames.jsonl`

One JSON object per line, one line per frame of the whole session (not only the window),
in ordinal order, keys in this order:

| Key | Type | Meaning |
|---|---|---|
| `ordinal` | int | 0-based frame index. |
| `ts` | string or null | Header timestamp normalised to UTC, ISO 8601 with `Z` (`2026-03-28T23:30:00Z`; fractional seconds kept when present). `null` when the header has none or it does not parse. |
| `role` | string | `user`, `assistant`, `tool`, `agent`, `system` or `message`. |
| `offset` | int | Character offset of the marker line's `<` relative to the first character of the anchoring `## Conversation` line. |
| `length` | int | The declared `N`. |
| `sha1_8` | string | First 8 hex digits of SHA-1 over `body.encode("utf-8")`, where `body` is the `N - 2` characters after the marker line (header included, separator excluded). Use `hashlib.sha1(..., usedforsecurity=False)`: this is a change detector, not a security control. |
| `in_window` | bool | The frame's effective timestamp (section 7.1) falls in the window. |
| `kept` | bool | Added to the brief: the frame appears in the digest body. `in_window and not kept` means the peer rule or the `--max-chars` cap dropped it. A consumer can tell "not today" from "cut". |

Why the offset is relative to the conversation heading and not to the start of the file:
`note`, `close`, `log` and `invoke`'s link step rewrite the front matter in place
(`rewrite_preserved_tail`, `src/cairn/frontmatter.py:323-348`; `add_link`,
`src/cairn/mutations.py:151-190`), and a user edits My Notes by hand. Both shift every
absolute offset while the conversation bytes stay identical
(`tests/test_shared_store.py:452-457` asserts that the bytes after `\n## Conversation\n`
survive an annotation). A relative offset survives annotation; an absolute one does not.

Why characters and not bytes: the frame lengths are characters, the walker works on the
decoded string, and `recall frame` re-reads the file anyway (4.3). A byte offset would
add a second unit for no reader.

### 4.3 `recall frame` verification

1. Resolve the record (`resolve`, `src/cairn/store.py:270-285`) and read the file with
   `newline=""`.
2. Run `locate` (3.3). Legacy or corrupt: exit 1.
3. Without `--frames`: take `frames[ORDINAL]` (out of range: exit 1, `ordinal K out of
   range; session has M frames`), print its raw text (marker line excluded, body
   included), print `sha1_8 XXXXXXXX` on stderr, exit 0.
4. With `--frames FILE`: read the row whose `ordinal` equals `ORDINAL` (missing: exit 1).
   Check, in order:
   - the text at `anchor + row.offset` matches `MARKER` with `N == row.length`;
   - that frame is `frames[ORDINAL]` in the fresh walk (same offset);
   - its `sha1_8` equals `row.sha1_8`.
   If all three hold, print the frame and exit 0. If any fails, print
   `Warning: SOURCE:ID frame K changed since the digest (stored sha1_8 X, offset O;
   now sha1_8 Y, offset P); the export was re-rendered` on stderr, then print
   `frames[ORDINAL]` from the fresh walk and exit 0.

Exit 0 on a mismatch is a choice: the command still answers "what is frame K now", and
the warning is on stderr where a script can see it. Open question 3 asks whether the
maintainer prefers a distinct exit code.

## 5. Condensing rules

### 5.1 Structure of a frame body

Inside a body the condenser recognises these parts, in the order the renderer writes
them (`src/cairn/rendering.py:113-134`):

1. The header line and the blank line after it.
2. A reasoning block, only when it starts at the third line: the lines
   `<details><summary>Reasoning</summary>`, `""`, the thinking text, `""`,
   `</details>`, `""` (`src/cairn/rendering.py:115-126`). The block ends at the first
   `"\n\n</details>\n"` after its opening line. Or the line
   `> [reasoning: unrecoverable]` (`src/cairn/rendering.py:127-128`).
3. The message text.
4. Zero or more tool blocks (`_render_tool_call`, `src/cairn/rendering.py:62-72`): a line
   `<tool name="NAME" status="STATUS">`, then optionally `arguments:` and a fence, then
   optionally `result:` and a fence, then `</tool>`. Each fence is one backtick longer
   than the longest backtick run in its content, with a minimum of three
   (`src/cairn/rendering.py:46-59`), so the closing fence of a block opened at a known
   position cannot occur inside that content.
5. Zero or more code blocks (`src/cairn/rendering.py:75-79`), Cursor only.

The text is not escaped, so a message can contain a line that looks like a tool block or
a `</details>` line. The condenser parses tool blocks by scanning lines for
`^<tool name="([^"]*)" status="([^"]*)">$` followed by `arguments:` or `result:` and an
opening fence, then reading to the exact matching closing fence and `</tool>`. A forged
block in prose that has this full shape is condensed as a tool call. This affects only
the condensed view. Provenance (ordinals, offsets, hashes) comes from the walker and
never from body parsing. Thinking text that itself contains `"\n\n</details>\n"` ends
the stripped block early and leaks its remainder into the digest. Both limits are
recorded in the module docstring.

Tool calls and tool output sit in different places per source, and the rules cover both:

- Claude: a call is a tool block inside an `### Assistant` frame with `arguments:` and
  no result (`src/cairn/sources/claude.py:327-335`). Its output is a separate `### Tool`
  frame whose text is the result (`src/cairn/sources/claude.py:286-293`).
- Codex: a call and its result share one tool block inside an `### Assistant` frame,
  because the result is attached to the call (`src/cairn/sources/codex.py:123-134`). An
  output with no matching call is a `### Tool` frame holding a tool block named `tool`
  with only a result (`src/cairn/sources/codex.py:135-141`).
- Antigravity: `### Tool` frames (`src/cairn/sources/antigravity.py:340`). UNVERIFIED
  whether their body is plain text or a tool block; the rules below handle both.

### 5.2 Rules

`head(s, k)` is the first `k` characters, `tail(s, k)` the last `k`. "Omission marker"
is the literal line `[... N chars omitted]` with `N` the exact count removed. All caps
are in characters of the decoded text. `\r\n` in condensed text is normalised to `\n`;
the hash in the sidecar is computed on the original body, never on condensed text.

| Part | Rule | Reason for the cap |
|---|---|---|
| Header line | Replaced by the digest marker `<!-- f<ordinal> <ts> <role> -->`. `ts` is the UTC form from 4.2, or `-` when null. | The marker carries the provenance id; the original header carries nothing more. |
| User text | `head(text, 2000)` plus the omission marker when longer. Applied after the task-notification rule. | A user message states intent in its first paragraphs. Pasted logs and files are the long tail, and 2,000 kept the measured day at 1.14 MB. |
| `<task-notification>...</task-notification>` block inside user text | Each block becomes `head(block, 300)` plus the omission marker. | These are host-generated status notices for background agents (`src/cairn/agents.py:127-140` reads them). The status and task id sit at the start; the embedded result repeats what the subagent frame or the following assistant prose already says. |
| Reasoning block, unrecoverable-reasoning line | Removed. | Reasoning is the largest single component of an assistant frame and is not an outcome. The final prose states the conclusion. |
| Assistant text | Kept in full. | Prose is where decisions, summaries and explanations live; it is the main signal and is already short compared with tool traffic. |
| Tool call (a tool block with `arguments:`) | One line: `[tool NAME] ` + `head(arguments_one_line, 120)` + ` …` when cut. `arguments_one_line` replaces each run of whitespace with one space. | The name plus the start of the arguments (a command, a path, a query) shows what was done. Full arguments (file contents for a write, long prompts) are bulk. |
| Tool output (a `### Tool` frame body, or the `result:` fence of a tool block) | If at most 600 characters, kept. Otherwise `head(out, 200)` + omission marker + `tail(out, 400)`. A result inside a tool block is printed as `[result]` on the line after the call line. | The head shows what ran; the tail holds test summaries, exit codes, final measurements and the last error line. The middle is progress noise. |
| Identical consecutive tool output | When an output's full original text equals the previous tool output kept in this digest (in ordinal order, across intervening assistant frames), the output is replaced by `[same output as f<k>]`. Compared on SHA-1 of the original text. | Polling loops and repeated `git status` calls repeat the same output many times. |
| `### Agent` frame text | If at most 6,000 characters, kept. Otherwise `head(text, 3000)` + omission marker + `tail(text, 3000)`. | An agent frame is another agent's report to this session (`src/cairn/sources/claude.py:166-179`, `src/cairn/sources/codex.py:359`). It is an outcome, often long; the brief gives no cap, so this spec reuses the 6,000 peer budget and splits it because both the summary at the top and the conclusion at the end matter. |
| `### System` frame text | `head(text, 300)` plus the omission marker. | Host-injected messages (`src/cairn/sources/antigravity.py:322`) are boilerplate. Same reasoning and cap as task notifications. |
| `### Message` frame (unknown role) | Treated as user text. | The renderer uses it only for a role it does not know (`src/cairn/rendering.py:109`); the conservative choice is the user cap. |
| Code blocks (Cursor) | Treated as tool output. | They follow the tool blocks and can hold whole files. |
| A body line that matches `^<!-- f\d+ ` | Prefixed with a backslash. | A transcript can quote a digest. The prefix stops a quoted line from posing as a frame marker; the sidecar stays the authority either way. |

Peer sessions use a different body (section 6.3).

### 5.3 The `--max-chars` cap

The cap applies to the digest body: the string written after the front matter, which is
the kept frame blocks joined with `"\n"`, each block being the marker line, `"\n"` and
the condensed body. The front matter does not count.

1. Condense every in-window frame. If the total length is at most the cap, keep all.
2. Otherwise select frames in three passes, keeping a running total:
   1. User frames (role `user` or `message`), newest first. Add each one that fits.
      Skip one that does not fit and continue, so a single oversized user frame does not
      block the others.
   2. All other frames, from the last backwards. Add each that fits. Stop at the first
      that does not fit, so the kept tail is contiguous.
   3. All other frames not yet kept, from the first forwards. Add each that fits. Stop at
      the first that does not fit.
3. Emit kept frames in ordinal order. No gap marker is written: a gap shows as a jump in
   the `f<ordinal>` numbers, and a marker line would itself need budget.
4. `truncated_frames` is the count of in-window frames not kept, `truncated_chars` the
   sum of their condensed block lengths (the size of what was cut, not of the raw text).

Why this order: user frames say what was asked, the end of the window says how it ended,
and the start says how it began. The middle is the most compressible.

## 6. Role and parent resolution

### 6.1 Order of checks

For each candidate record, in this order:

1. Subagent. `record.is_subagent` (`parent_session_id` present,
   `src/cairn/store.py:123-126`, `src/cairn/store.py:218`). `role: subagent`,
   `parent: <source>:<parent_session_id>`. Claude writes the field for every child
   (`src/cairn/sources/claude.py:610-611`) and Codex for a `thread_spawn` child
   (`src/cairn/sources/codex_lineage.py:29-46`). Skipped unless `--include-subagents`.
2. Peer by link. The front matter `links:` list (preserved by `preserved_tail`,
   `src/cairn/frontmatter.py:314-318`) holds an entry `parent <source>:<id>` after
   normalisation with `_bare_link`. `cairn invoke` writes exactly this entry on the peer
   document and `child <peer>:<thread>` on the caller (`src/cairn/invoke.py:535-555`,
   roles in `src/cairn/mutations.py:35`). `role: peer`, `parent` = that reference. With
   more than one `parent` entry, take the first and print a warning.
3. Peer by preamble. A user frame whose text, after the header and blank line, matches
   the regex in 6.2. Scan all user frames of the whole session in ordinal order and use
   the first match. Not only the first user frame: a Codex session can open with
   host-context user messages (UNVERIFIED: whether Codex records the environment context
   as a `user_message` before the prompt; `user_message` at
   `src/cairn/sources/codex.py:214-225` takes both event and response items). If the
   match names a caller, `role: peer`, `parent` = the caller reference.
4. Orphan peer. A preamble match that names no caller (`an operator`), or a caller whose
   reference fails `_LINK_REF`-style validation (`src/cairn/mutations.py:36-37`):
   `role: primary`, `parent: null`, `orphan_peer: true`. It is condensed as a primary
   session, so nothing is lost when lineage is unknown.
5. Otherwise `role: primary`, `parent: null`.

Links win over the preamble because `invoke` writes links after the round from its own
state, while the preamble is text that a user could paste. `parent` comes from these
two places and not from `record.parent_id`, because `parent_id` is set only from
`parent_session_id` (`src/cairn/store.py:218`), which no Codex or Antigravity peer
session carries.

### 6.2 The invoke preamble

The preamble is built by `preamble()` (`src/cairn/invoke.py:1257-1266`):

```
[cairn invoke] You are {peer} being invoked by {who} via cairn (round {number}). Reply budget: ...
```

where `who` is `{source} session {caller}` and `caller` is the full `source:id`
reference, or the literal `an operator` when no caller was found
(`src/cairn/invoke.py:1259`). So a real preamble reads
`... being invoked by claude session claude:3f2a9c1e-... via cairn (round 1).` and the
source appears twice. The brief's shape `invoked by <source> session <id>` is close but
misses that the id is source-qualified. The runner prepends the preamble to every
round's prompt as `head + "\n\n---\n\n" + prompt`, with an optional permissions note
between (`src/cairn/invoke.py:1553-1556`). The note starts with
`[cairn invoke] Shell permissions` (`src/cairn/invoke.py:852`) and does not match the
regex below because it sits after the first line.

Regex (applied with `re.match` to the user text, after the header line and blank line):

```python
INVOKE_PREAMBLE = re.compile(
    r"\[cairn invoke\] You are (?P<peer>[a-z][a-z0-9-]*) being invoked by "
    r"(?:(?P<caller_source>[a-z][a-z0-9-]*) session "
    r"(?P<caller>(?P<ref_source>[a-z][a-z0-9-]*):\S+)|an operator) "
    r"via cairn \(round (?P<round>\d+)\)\."
)
```

After a match, require `caller_source == ref_source` when a caller is present; if not,
treat the match as an orphan. Callers come from `--parent` (`claude:` or `codex:` only,
`_CALLER_REF` at `src/cairn/invoke.py:152`) or from the detected host
(`resolve_caller`, `src/cairn/invoke.py:1269-1282`), so the source group is not limited
to two names.

Antigravity strips its own wrapper tags and keeps the `<USER_REQUEST>` body
(`user_text`, `src/cairn/sources/antigravity.py:198-206`), and the harness notes that the
prompt always starts with cairn's preamble (`src/cairn/harnesses/antigravity.py:355`). The
tests assert the preamble at the start of the prompt for Codex and Antigravity
(`tests/test_invoke.py:287`, `tests/test_invoke.py:823`, `tests/test_invoke.py:1229`).
UNVERIFIED: that a Claude peer export (from `claude -p`) records the prompt as a plain
user frame beginning with the preamble; the Claude parser keeps string content as user
text (`src/cairn/sources/claude.py:265-275`), which suggests it does.

### 6.3 Peer body

A peer thread can run several rounds, and each `--resume` round adds a new user frame
with its own preamble (`(round N)`). The brief asks for "the last assistant frame inside
the window". This spec refines it to one answer per round, because a resumed thread
holds several answers and the brief's rule would keep only the last one.

1. Split the session's frames into rounds: a round starts at each user frame that
   matches `INVOKE_PREAMBLE` and runs until the next one.
2. For each round with at least one in-window frame, keep the last assistant frame of
   that round that is in the window and has non-empty text (tool-only frames do not
   count). Body: its condensed text without tool blocks, `head(text, 6000)` plus the
   omission marker when longer.
3. Also keep every in-window assistant frame, not already kept, that has a line matching
   `^[\s>*_]*VERDICT:` (multiline). Its body is only those lines.
4. Emit in ordinal order. `--max-chars` then applies as in 5.3.

The 6,000 cap is the brief's. The caller session already holds the prompt (as a
`cairn invoke` tool call), so the peer digest carries only the answer. Open question 5
asks whether the first 300 characters of the prompt after the preamble should be kept.

## 7. Date window semantics

### 7.1 Which frames are in the window

- The window is `[start, end)` in UTC from `parse_date_range`, the same half-open window
  `in_window` uses (`src/cairn/store.py:128-129`).
- A frame's timestamp is its header timestamp, parsed with `parse_iso` and converted to
  UTC. A header `2026-03-29T01:30:00+02:00` belongs to 2026-03-28, as
  `tests/test_shared_store.py:321-329` already requires of `recall list`.
- Effective timestamp: a frame with no usable timestamp takes the effective timestamp of
  the nearest earlier frame; a leading run with none takes the first later one; a
  session with none has no in-window frames. The sidecar `ts` stays `null` for such a
  frame; only `in_window` uses the inherited value. Reason: a Claude tool result shares
  its record's timestamp, but some source rows have none, and dropping them would cut a
  call from its output.

### 7.2 Which sessions are candidates

1. `records = SessionStore(config).records(args.source)`, then the project filter, then
   the subagent filter and `--min-msgs`, as in `recall list`
   (`src/cairn/recall.py:88-114`).
2. Prefilter on `record.in_window(start, end)`. Front matter `activity_dates` are the UTC
   days of the message timestamps (`metadata_lines`, `src/cairn/store.py:28-32`), and
   every timestamped message becomes exactly one framed header with the same string
   (`src/cairn/rendering.py:110-112`). So a session with an in-window frame always
   passes the prefilter, and the prefilter costs no body read.
3. Walk the file. A session with zero in-window frames after the walk (possible for an
   endpoints-only record, `src/cairn/store.py:193-203`) is not written and gets no row.

### 7.3 Exported-frame dates, not event dates

The digest promises the dates of exported frames, which are not always the dates of the
original events. The case that matters: Codex attaches a tool result to the assistant
message that carries its call, and that message keeps the call's timestamp
(`_append_tool_call` stores `timestamp` at `src/cairn/sources/codex.py:106-118`;
`_resolve_tool_output` writes the result into it at `src/cairn/sources/codex.py:132-134`
and ignores the result's own timestamp). A call at 23:59:50 on day D whose result
arrives at 00:00:10 on D+1 is one frame stamped D. The digest for D shows the result;
the digest for D+1 does not.

A second consequence: if an export runs between the call and the result, the frame has
no result yet. The next export fills it in, the frame keeps its ordinal, and its hash
changes. `recall frame --frames` reports that as a re-render (4.3).

### 7.4 Sessions that started earlier

A session that started on D-1 and continued on D appears in the digest for D with only
its D frames. Its front matter carries `session_started_at` (from `record.started_at`)
and `frames_total`, so a consumer knows it is a continuation. `role`, `parent` and
`artifacts` describe the whole session, not the day: `## Artifacts` is a whole-session
list (`render_artifacts`, `src/cairn/rendering.py:82-101`).

## 8. Output formats

### 8.1 File names

`<source>-<shortid>.md` and `<source>-<shortid>.frames.jsonl`. `shortid` is the native
id with every character outside `[A-Za-z0-9]` removed, cut to 12 characters. If two
sessions in one run produce the same `<source>-<shortid>`, both use the full cleaned id.

Why not the adapters' `short_id`: the base rule is the first 8 characters
(`src/cairn/sources/base.py:65-67`), and Codex ids are UUIDv7, time-ordered, so sessions
started seconds apart share their first 8 hex digits; the Codex adapter adds 4 random
characters for that reason (`src/cairn/sources/codex.py:578-583`). Calling the adapters
means importing source modules that bind config at import (`_load_source`,
`src/cairn/invoke.py:235-238`). Twelve characters with the dash removed gives 48 bits of
UUIDv7 millisecond timestamp, and the in-run collision rule covers the rest.

### 8.2 Digest Markdown, with one full example

Front matter keys, in this order. Scalars are written as `key: "value"` with
`_escape_scalar`, integers and booleans bare, `null` bare, lists in block style, so
`parse_frontmatter` reads the file back.

| Key | Value |
|---|---|
| `ref` | `record.reference` |
| `source` | source name |
| `title` | the session title from front matter (added to the brief: one line that says what the session was about, at no cost) |
| `project` | `record.project` or `null` |
| `cwd` | `record.cwd` or `null` |
| `role` | `primary`, `peer` or `subagent` |
| `parent` | `source:id` or `null` |
| `orphan_peer` | `true` or `false` |
| `owner` | copied from the session front matter when present; the key is omitted otherwise |
| `date` | the window day, `YYYY-MM-DD` |
| `session_started_at` | `record.started_at` in UTC, or `null` |
| `first_ts`, `last_ts` | first and last effective timestamp among in-window frames |
| `frames_today` | number of in-window frames |
| `frames_total` | number of frames in the session |
| `raw_chars_today` | sum of `length` over in-window frames (added: lets a consumer see how much the condenser removed) |
| `artifacts` | block list, see below |
| `truncated_frames` | see 5.3 |
| `truncated_chars` | see 5.3 |

Two changes from the brief, with reasons:

- `truncated: {frames_dropped, chars_dropped}` becomes the two flat keys
  `truncated_frames` and `truncated_chars`. A nested mapping is outside the dialect
  `parse_frontmatter` accepts; it raises on any indented key
  (`src/cairn/frontmatter.py:108-112`). Flat keys keep the digest readable by cairn's own
  parser and by QMD-style tools. The `--json` row keeps the nested object, because JSON
  has no such limit.
- `owner`: no exporter writes an `owner` field today (a search of `src/cairn` for
  `owner` finds only exchange and harness ownership code, `src/cairn/exchange.py:185`,
  `src/cairn/harnesses/antigravity_registration.py:48`). The digest copies the key when
  a session carries one and omits it otherwise. How a routine-generated session gets the
  key is open question 1.

`artifacts`: parse the region between the first line and the `## My Notes` heading. In
the first `## Artifacts` section, take each list item: ``- `PATH` `` (Claude and Cursor,
`render_artifacts`, `src/cairn/rendering.py:92-100`) gives `PATH`; `- [LABEL](URI)`
(Antigravity, `src/cairn/sources/antigravity.py:440-446`) gives `URI`. The `**Created:**`
and `**Modified:**` labels are not kept. Codex documents have no `## Artifacts` section
(`src/cairn/sources/codex.py:596-620`), so the list is empty.

Example (fixture values only):

````markdown
---
ref: "claude:3f2a9c1e-7b4d-4e2a-9c1e-000000000001"
source: "claude"
title: "Fix the widget pipeline retry bug"
project: "demo"
cwd: "/tmp/demo"
role: "primary"
parent: null
orphan_peer: false
date: "2026-03-28"
session_started_at: "2026-03-27T22:10:00Z"
first_ts: "2026-03-28T09:02:11Z"
last_ts: "2026-03-28T09:41:57Z"
frames_today: 6
frames_total: 14
raw_chars_today: 18342
artifacts:
  - src/widget/retry.py
  - tests/test_retry.py
truncated_frames: 0
truncated_chars: 0
---
<!-- f8 2026-03-28T09:02:11Z user -->
The retry test still fails on CI. Find out why and fix it.
<!-- f9 2026-03-28T09:02:40Z assistant -->
I will run the failing test first.
[tool Bash] {"command": "python -m pytest tests/test_retry.py -x -q"}
<!-- f10 2026-03-28T09:02:52Z tool -->
F
=================================== FAILURES ===================================
____________________________ test_backoff_caps_delay ____________________________
[... 1873 chars omitted]
E       AssertionError: 64 != 30
tests/test_retry.py:41: AssertionError
=========================== short test summary info ============================
FAILED tests/test_retry.py::test_backoff_caps_delay - AssertionError: 64 != 30
1 failed in 0.31s
<!-- f11 2026-03-28T09:05:13Z assistant -->
The backoff never applies the cap: `min()` runs before the jitter is added. I moved the cap after the jitter.
[tool Edit] {"file_path": "src/widget/retry.py", "old_string": "delay = min(base * 2 ** attempt, cap) + jitter", "new_string": "dela …
<!-- f12 2026-03-28T09:05:30Z tool -->
The file src/widget/retry.py has been updated.
<!-- f13 2026-03-28T09:41:57Z assistant -->
Fixed. The retry test passes and the full suite is green (212 passed).
````

### 8.3 Sidecar example

For the same session (frames 0 to 7 are from 2026-03-27; values are illustrative):

```json
{"ordinal": 0, "ts": "2026-03-27T22:10:00Z", "role": "user", "offset": 17, "length": 212, "sha1_8": "9b1c04aa", "in_window": false, "kept": false}
{"ordinal": 1, "ts": "2026-03-27T22:10:31Z", "role": "assistant", "offset": 253, "length": 1408, "sha1_8": "51e0d7f2", "in_window": false, "kept": false}
{"ordinal": 2, "ts": "2026-03-27T22:10:40Z", "role": "tool", "offset": 1686, "length": 2210, "sha1_8": "c3a92e10", "in_window": false, "kept": false}
{"ordinal": 3, "ts": "2026-03-27T22:11:02Z", "role": "assistant", "offset": 3921, "length": 311, "sha1_8": "0d4471be", "in_window": false, "kept": false}
{"ordinal": 4, "ts": "2026-03-27T22:11:09Z", "role": "tool", "offset": 4256, "length": 96, "sha1_8": "7f00ac35", "in_window": false, "kept": false}
{"ordinal": 5, "ts": "2026-03-27T22:30:44Z", "role": "user", "offset": 4375, "length": 88, "sha1_8": "e2b8f061", "in_window": false, "kept": false}
{"ordinal": 6, "ts": "2026-03-27T22:30:59Z", "role": "assistant", "offset": 4486, "length": 640, "sha1_8": "aa31c9d8", "in_window": false, "kept": false}
{"ordinal": 7, "ts": "2026-03-27T22:31:20Z", "role": "agent", "offset": 5150, "length": 1210, "sha1_8": "3e6b7c52", "in_window": false, "kept": false}
{"ordinal": 8, "ts": "2026-03-28T09:02:11Z", "role": "user", "offset": 6385, "length": 104, "sha1_8": "18f4e9a0", "in_window": true, "kept": true}
{"ordinal": 9, "ts": "2026-03-28T09:02:40Z", "role": "assistant", "offset": 6513, "length": 402, "sha1_8": "bd02c71e", "in_window": true, "kept": true}
{"ordinal": 10, "ts": "2026-03-28T09:02:52Z", "role": "tool", "offset": 6939, "length": 2650, "sha1_8": "64a1f3d9", "in_window": true, "kept": true}
{"ordinal": 11, "ts": "2026-03-28T09:05:13Z", "role": "assistant", "offset": 9614, "length": 13490, "sha1_8": "f9c2e804", "in_window": true, "kept": true}
{"ordinal": 12, "ts": "2026-03-28T09:05:30Z", "role": "tool", "offset": 23130, "length": 98, "sha1_8": "2a7d5e61", "in_window": true, "kept": true}
{"ordinal": 13, "ts": "2026-03-28T09:41:57Z", "role": "assistant", "offset": 23251, "length": 1598, "sha1_8": "c08e9b37", "in_window": true, "kept": true}
```

The first frame's offset is 17: `len("## Conversation\n")` is 16, plus the blank line.

### 8.4 `--json` stdout

One JSON array (`json.dumps(rows, indent=2)`, as `recall list` does at
`src/cairn/recall.py:134-135`), one object per written session, sorted by `first_ts`
then `ref`:

```json
[
  {
    "ref": "claude:3f2a9c1e-7b4d-4e2a-9c1e-000000000001",
    "path": "~/.cache/cairn/digests/2026-03-28/claude-3f2a9c1e7b4d.md",
    "frames_path": "~/.cache/cairn/digests/2026-03-28/claude-3f2a9c1e7b4d.frames.jsonl",
    "role": "primary",
    "parent": null,
    "project": "demo",
    "cwd": "/tmp/demo",
    "frames_today": 6,
    "chars": 1388,
    "truncated": {"frames_dropped": 0, "chars_dropped": 0}
  }
]
```

`path` and `frames_path` are absolute in real output (the `~` above is for the
example). `chars` is the digest body length. Skipped sessions (legacy, corrupt) are not
rows; they are stderr warnings, and corrupt ones set exit 3.

Without `--json`, stdout is one line per written session,
`REF  ROLE  frames_today=N  chars=C  [truncated F]`, then
`N sessions written to DIR`.

## 9. Test plan

New file `tests/test_recall_digest.py`. Unit tests build export documents in memory with
`render_conversation` (`src/cairn/rendering.py:104`) joined the way
`assemble_document` joins them (`src/cairn/sources/base.py:410-423`), so the fixture is
the real format without a native store. Integration tests write such documents into a
temp data root, with `HOME`, `XDG_CACHE_HOME` and `XDG_CONFIG_HOME` pointed into the temp
directory, as `tests/test_recall.py:94-106` and `tests/test_shared_store.py:63-82` do.
The Codex midnight test exports a native rollout through `python -m cairn codex export
--all`, as `tests/test_shared_store.py:430-450` does, because the attachment behaviour
lives in the Codex parser.

### 9.1 The fixture transcript

One Claude session document, `session_id` `fixture-0001`, `cwd` `/tmp/demo`,
`user_message_count` 4, `activity_dates` 2026-03-27 and 2026-03-28, `## Artifacts` with
one created file, a My Notes block that contains the line `<!-- cairn:turn 40 -->` and a
fenced `## Conversation` line, then these messages:

| # | Role | Timestamp | Content |
|---|---|---|---|
| 0 | user | 2026-03-27T22:10:00Z | "Start the widget work." |
| 1 | assistant | 2026-03-27T23:59:00Z | text "Done for today." |
| 2 | user | 2026-03-28T09:00:00Z | 2,500 characters, with a `<task-notification>` block of 900 characters in the middle |
| 3 | assistant | 2026-03-28T09:01:00Z | `thinking` "secret plan", text containing a line `### C1` and a line `<!-- cairn:turn 40 -->` and a line `<!-- f99 forged -->`, one tool call `Bash` with 300-character arguments |
| 4 | tool | 2026-03-28T09:01:05Z | 3,000-character output ending `1 failed in 0.31s` |
| 5 | tool | 2026-03-28T09:01:06Z | same text as 4 |
| 6 | agent | 2026-03-28T09:02:00Z | "from agent x:\n\nreport" |
| 7 | user | 2026-03-28T09:03:00Z | "ok" |
| 8 | assistant | 2026-03-28T09:03:30Z | "ab" |
| 9 | assistant | 2026-03-28T09:03:31Z | "cd" (same body length as 8 apart from the timestamp; give both the same timestamp string length so the two `N` values are equal) |

### 9.2 Walker and provenance tests

| Test | Asserts |
|---|---|
| `test_walk_counts_every_frame_and_ignores_forged_markers` | `locate` returns 10 frames for the fixture. The `<!-- cairn:turn 40 -->` inside frame 3 and inside My Notes is not a frame. `re.findall` on the marker finds more matches than frames, proving the test would catch a regex walker. |
| `test_anchor_skips_conversation_heading_in_notes` | The fenced `## Conversation` line in My Notes is tried and rejected; the anchor is the real heading; frame 0's `offset` is 17. |
| `test_heading_inside_body_is_not_a_frame` | The `### C1` line in frame 3 does not start a frame and does not change ordinals. |
| `test_equal_lengths_get_distinct_ordinals_and_hashes` | Frames 8 and 9 have the same `length`, ordinals 8 and 9, different `sha1_8`; `recall frame fixture-0001 9` prints "cd". |
| `test_walk_accepts_both_document_endings` | The walk succeeds when the document ends with `body + "\n"` (real export) and with one extra `"\n"` (the `tests/test_rendering.py:102` form). |
| `test_crlf_bodies_walk_by_characters` | A user text with `\r\n` and a forged marker after it walks correctly when read with `newline=""`; reading with universal newlines makes `locate` return Corrupt. Mirrors `tests/test_shared_store.py:430-457`. |
| `test_offsets_survive_annotation` | Write the sidecar, add a note with `cairn claude note`, run `recall frame --frames`: no warning, same frame. |
| `test_frame_warns_on_rerender` | Edit one character inside frame 4's body and fix its `N` (simulating a re-render): `recall frame --frames ... 4` exits 0, prints the new body, and stderr contains `changed since the digest`. |
| `test_frame_ordinal_out_of_range` | Exit 1 with `out of range`. |
| `test_legacy_file_is_skipped_not_guessed` | The legacy document from `tests/test_shared_store.py:333-336` with today's date: no output file, a `legacy export without frames` warning, exit 0. `recall frame legacy 0` exits 1. |
| `test_corrupt_frame_sets_exit_3` | Change one declared `N` in a second session: that session is skipped with `frame walk failed at character`, the fixture session is still written, exit 3. |

### 9.3 Window and condensing tests

| Test | Asserts |
|---|---|
| `test_only_window_frames_in_body` | Digest for 2026-03-28: the body holds `f2` to `f9`, not `f0` or `f1`. Sidecar has 10 rows; `in_window` is false for 0 and 1. `frames_today` 8, `frames_total` 10, `session_started_at` 2026-03-27T22:10:00Z. |
| `test_offset_timestamp_is_utc` | A header timestamp `2026-03-29T01:30:00+02:00` lands in the 2026-03-28 digest and prints as `2026-03-28T23:30:00Z`. |
| `test_multi_day_window_rejected` | `digest "last 3 days"` exits 1 with `one-day window`. |
| `test_user_text_and_task_notification_caps` | Frame 2's notification shrinks to 300 characters plus a marker, and the user text is at most 2,000 characters plus a marker. |
| `test_reasoning_removed_prose_kept` | "secret plan" is absent; frame 3's full prose is present; the forged `<!-- f99` line is escaped with a backslash. |
| `test_tool_call_is_name_plus_120` | The `Bash` call becomes one `[tool Bash] ` line with 120 argument characters and ` …`. |
| `test_tool_output_head_and_tail` | Frame 4 keeps 200 head and 400 tail characters, the tail includes `1 failed in 0.31s`, and the marker states the exact omitted count. |
| `test_identical_consecutive_output_deduplicated` | Frame 5 renders as `[same output as f4]`. |
| `test_agent_frame_kept` | The `### Agent` frame appears with role `agent` and its text. |
| `test_single_user_message_session_included` | A second session with one user frame and one assistant frame in the window is written with the default `--min-msgs 1`, and is not written with `--min-msgs 3`. |
| `test_max_chars_keeps_users_then_tail_then_head` | With `--max-chars` set so that only part fits: every user frame is kept, the kept non-user frames form a contiguous tail plus a contiguous head, the body length is at most the cap, and `truncated_frames` and `truncated_chars` match the dropped frames. |
| `test_frontmatter_round_trips` | `parse_frontmatter` on the digest returns `role`, `artifacts` as a list, and the flat `truncated_*` keys, with no `FrontmatterParseError`. |

### 9.4 Role, peer and Codex tests

| Test | Asserts |
|---|---|
| `test_peer_by_preamble_codex` | A Codex document whose first user frame starts with `[cairn invoke] You are codex being invoked by claude session claude:fixture-0001 via cairn (round 1).` and has no `links:`: `role: peer`, `parent: claude:fixture-0001`. Body holds only the last assistant frame with text and the `VERDICT: accept - fixture` line. |
| `test_peer_by_link_wins_over_preamble` | Same document with `links:` holding `parent claude:other-0002`: `parent` is `claude:other-0002`. |
| `test_peer_rounds_each_keep_their_answer` | Two rounds in the window (`round 1`, `round 2`): two answer frames are kept. |
| `test_operator_peer_is_orphan_primary` | Preamble `being invoked by an operator`: `role: primary`, `orphan_peer: true`, full primary condensing. |
| `test_subagents_skipped_unless_requested` | A `claude-subagent` document with `parent_session_id`: absent by default, present with `--include-subagents`, `role: subagent`, `parent: claude:<parent id>`. |
| `test_codex_result_after_midnight_stays_with_call` | Native rollout: a `function_call` at 2026-03-28T23:59:50Z and its `function_call_output` at 2026-03-29T00:00:10Z, exported through the CLI. The digest for 2026-03-28 contains the result inside the call's frame; the digest for 2026-03-29 has no frame for it. |
| `test_owner_copied_when_present` | A session with `owner: "routine"` in front matter yields `owner: "routine"`; one without has no `owner` key. |
| `test_out_dir_safety` | A non-empty `--out` without `.cairn-digest` exits 1 and writes nothing; with the marker, a stale `claude-old.md` is removed. |

### 9.5 Existing tests

No existing test changes. `tests/test_recall.py` and `tests/test_shared_store.py` must
pass unmodified, which shows `list` and `expand` keep their behaviour.

## 10. Docs to update

1. `skills/recall/SKILL.md`: after the `expand` block (`skills/recall/SKILL.md:53-58`),
   add a short "Daily digest" paragraph: `cairn recall digest DATE --all-projects --out
   DIR --json`, what the two files hold, that `f<ordinal>` markers cite frames, and
   `cairn recall frame SOURCE:ID ORDINAL --frames FILE` to check a citation. State that
   digest defaults to `--min-msgs 1` while `list` defaults to 3
   (`skills/recall/SKILL.md:40-41`).
2. `README.md`: add two lines to the recall usage block (`README.md:333-339`) and one
   sentence after it on UTC exported-frame dates and the Codex caveat.
3. `src/cairn/__main__.py:56-59`: add `digest` and `frame` lines to the help text.
4. `CHANGELOG.md`: a new `## [2.9.0]` section with `### Added` entries for both
   commands, in the style of the existing entries (`CHANGELOG.md:8-14`).
5. Version: the PR template requires a bump when `src/cairn/` changes
   (`.github/PULL_REQUEST_TEMPLATE.md:16`). A new command is a minor change under the
   Semantic Versioning the changelog follows (`CHANGELOG.md:5-6`): 2.8.1 to 2.9.0 in
   `src/cairn/__init__.py:11`, `plugin.json:4` and `.claude-plugin/plugin.json:3`.
   `tests/test_plugin_manifest.py:24-30` fails if `plugin.json` and `__version__`
   differ.
6. Config examples: no change. The digest adds no config key; `--out` defaults to the
   XDG cache. `cairn.config.example.json` and `src/cairn/config.example.json` stay as
   they are, and `tests/test_config_examples.py` needs no edit.
7. `skills/cairn/reference/*.md`: no change; no source behaviour changes.

## 11. Open questions for the maintainer

1. `owner`: no exporter writes it. Should the routine mark its own sessions through a new
   `links`-style preserved field, through `tags` (already preserved,
   `src/cairn/frontmatter.py:284-290`), or through an environment variable that the
   exporters record? Until this is decided, `owner` is copied when present and the
   routine cannot exclude its own sessions by it. `tags` needs no code change and might
   be enough: the digest could copy `tags` too.
2. Legacy and corrupt files: this spec skips them. Is a stderr warning enough for the
   routine, or should `--json` print a second array of skipped references?
3. `recall frame` on a hash mismatch exits 0 with a warning. Would a distinct exit code
   (for example 3, as digest uses for partial output) suit automation better?
4. `bounded_content` begins its marker search at position 0 (`src/cairn/store.py:300`),
   so a marker string in My Notes or a Summary can become `expand`'s first frame. Should
   `expand` move to the anchored walker in the same PR, or in a follow-up? This spec
   leaves `expand` unchanged.
5. Peer digests drop the prompt. Should the first 300 characters after the preamble's
   `---` separator be kept, so a peer digest reads without its caller's digest?
6. Context for a continued session: should the digest carry the last user frame before
   the window (capped at 2,000) so that a day that starts mid-task is readable alone?
7. `--max-chars` default 40,000 applies per session. Does the routine also need a total
   cap across the day, or does it handle that itself?
8. Antigravity tool frame bodies and Cursor roles are UNVERIFIED (3.1, 5.1). Are there
   real exports in a test fixture that show them, or should the implementer add one?

## 12. How a subagent implements this

Do the steps in order. Run `make check` (ruff, ruff format, mypy, unittest;
`Makefile`) after steps 2, 4 and 7, and do not go on while it fails.

1. Read this spec, `src/cairn/rendering.py`, `src/cairn/store.py:288-308`,
   `src/cairn/recall.py` and `tests/test_rendering.py:74-127`.
2. Create `src/cairn/frames.py`: `Frame` (frozen dataclass), `locate(text)` returning
   `(anchor, frames)` or raising `LegacyExport` / `CorruptExport(position)`, `read_text`
   (the `newline=""` open), `sha1_8(body)`, `effective_times(frames)`. Standard library
   only. Add `tests/test_recall_digest.py` with the 9.2 walker tests that need no CLI.
3. Create `src/cairn/digest.py`: role resolution (6.1, with `INVOKE_PREAMBLE`), body
   condensing (5.2), the cap (5.3), artifact parsing (8.2), front matter and sidecar
   writers, the out-directory rule (2.3), and `run_digest(args, records, window) -> int`
   and `run_frame(args, record) -> int`. Ruff sets no file-length rule (`pyproject.toml`,
   `select`), but split condensing into `src/cairn/digest_condense.py` if `digest.py`
   passes about 400 lines, so each module stays readable.
4. Edit `src/cairn/recall.py`: the `digest` and `frame` subparsers, the shared flag loop
   (`src/cairn/recall.py:72-78`), the project default (`src/cairn/recall.py:91-93`), the
   `--refresh` branch, and dispatch to `digest.run_digest` / `digest.run_frame`. Add the
   9.3 and 9.4 tests.
5. Edit `src/cairn/__main__.py:56-59` help text.
6. Docs from section 10: `skills/recall/SKILL.md`, `README.md`, `CHANGELOG.md`.
7. Bump the version to 2.9.0 in `src/cairn/__init__.py`, `plugin.json` and
   `.claude-plugin/plugin.json`. Run `make check`.
8. Before any push, read the diff for home paths, personal emails and private names, as
   `CLAUDE.md` requires; `tests/test_public_hygiene.py` checks the generic shapes. Do not
   commit or push unless the maintainer asks.

## 13. Maintainer answers (proposed by the implementing agent, 2026-10-28, pending the owner's review)

1. `owner` is written by `cairn invoke --owner` into the preserved tail of the peer session's front matter in the `exported` step (see `docs/routine-spec.md` section 5). Exporters stay unchanged. Digest copies it when present.
2. `--json` also lists skipped sessions in a `skipped[]` array with `{ref, reason: legacy|corrupt}`.
3. A hash mismatch in `recall frame` prints the warning and exits 0; the frame it prints is still the best available evidence.
4. `expand` moves to the anchored walker in a later PR, tracked as a follow-up issue.
5. Yes, a peer digest keeps the first 300 characters of its prompt.
6. Yes, a session that continues from an earlier day carries its last user frame from before the window, marked `in_window: false` in the body header, so the extractor knows the goal.
7. No total cap per day in cairn; the consumer's tiers handle it.
8. Fixtures are synthetic. Build the Antigravity tool-frame and Cursor role fixtures from the renderer's own output on invented data, never from real exports.
