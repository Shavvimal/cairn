# The `provenance` front-matter key: implementation spec

Status: implemented in the pull request that adds this file, on top of `main` at 2.9.0
(f0ea1e1). Section 11 lists where the implementation differs from the plan below.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0. A claim that
could not be checked is marked UNVERIFIED.

---

## 1. Purpose and non-goals

### 1.1 Purpose

A reader of the store (a person, `/recall`, a routine) cannot tell today whether a
document holds a session that a person drove, a headless peer run, or a mirrored
meeting. The digest reconstructs part of this from the body (the invoke preamble,
`src/cairn/digest.py:106-127`), and only for the digest. This layer gives every session
document and every exchange document one exporter-owned front-matter key:

```yaml
provenance: tool
```

`cairn recall list --json` and `cairn recall digest --json` show it, with the existing
`owner` label beside it. A repeatable `--provenance VALUE` flag on both commands filters
on it. The `recall list` filter reads front matter only, so it is deterministic and needs
no QMD. The digest filter also reads the first user message of a document without a stored
key (4.5), because the digest parses the body anyway.

### 1.2 The value set (decision)

Four values, one per document. The value names who produced the body text, the part
below `## My Notes` and the source head above the preserved tail.

| Value | Meaning | Documents |
|---|---|---|
| `tool` | cairn exported it from a harness log of a session that a person drove | `claude-session`, `codex-session`, `antigravity-session`, `cursor-session` without the invoke preamble |
| `agent` | an agent wrote the prompts, not a person | peer session documents (first user message starts with the invoke preamble), `*-subagent` documents, `agent-exchange` documents, routine journal entries (by convention, 3.4) |
| `imported` | mirrored from another application, not produced on this machine's harness | `granola-meeting`; service docs (derived by collection, 3.4) |
| `user` | a person wrote it by hand | the `notes` collection (derived by collection, 3.4); any hand-written file that declares it |

**One value, not a list.** A document cannot carry two values. User annotations already
have fixed places: the preserved tail (`status`, `tags`, `rating`, `comments`,
`projects`, `links`, `owner`; `src/cairn/frontmatter.py:290-345`) and the `## My Notes`
section (`src/cairn/frontmatter.py:189-236`). Their author is known by position. A list
value would need a merge rule inside `rewrite_preserved_tail`
(`src/cairn/frontmatter.py:348-373`) and an "any or all" rule for the filter. Neither
buys a fact that the position does not already give.

A comparable tool stamps a six-level trust rank on every row and enforces it with a
database trigger. cairn needs no rank and no ordering: the four values are labels, and
the filter is set membership.

### 1.3 Non-goals

- No version stamps, no `exporter:` block; those belong to a separate change.
- No trust order, no "minimum provenance" floor, no taint of derived documents.
- No user override. The key sits in the exporter-owned head, so a hand edit is
  replaced on the next re-render (3.3). A person who disagrees uses `tags`.
- No claim about who typed an annotation. `cairn claude note` writes a comment whether a
  person or an agent ran it (`src/cairn/mutations.py:107-115`). Position says
  "annotation", not "a person typed this".
- No change to service docs bytes. They stay verbatim copies (3.4).
- No `--provenance` on `cairn <source> list`. Its `--json` already dumps the whole front
  matter (`src/cairn/sources/base.py:466-468`), so the stored key appears there as is.
- No `--backfill` command and no `SCHEMA_VERSION` bump (4.3).

---

## 2. Current behaviour (main at 2.9.0)

1. **Head versus tail.** Each adapter writes its head through `head_open`
   (`src/cairn/frontmatter.py:260-266`) and `head_close` (`:269-280`). `assemble_document`
   appends the preserved tail (`src/cairn/sources/base.py:396-423`). `export_one` then
   inserts the shared metadata block from `metadata_lines` directly before the first
   `\nstatus:` (`src/cairn/sources/base.py:303-304`). That block holds `cairn_schema`,
   `source`, `native_id`, `started_at` and the other recall fields
   (`src/cairn/store.py:35-59`).
2. **Mutations keep the head.** `rewrite_preserved_tail` finds the first line that starts
   with a tail key (`_TAIL_KEYS`, `src/cairn/frontmatter.py:41`), keeps every line before
   it byte for byte and re-emits only the tail (`src/cairn/frontmatter.py:368-373`). Every
   mutation goes through it (`src/cairn/mutations.py:72-104`). So a head key survives
   `note`, `close`, `log`, `set_owner` and `add_link` with no change.
3. **Re-export.** A re-export re-renders the head from native data and re-emits the tail
   from the old file (`src/cairn/sources/base.py:298-304`). A document is re-rendered
   only when its native revision changes or its `cairn_schema` differs from
   `SCHEMA_VERSION = 2` (`src/cairn/store.py:19`, `:69-77`).
4. **Owner.** `owner` is a preserved-tail key, emitted only when non-empty
   (`src/cairn/frontmatter.py:341-343`). `set_owner` writes it on the peer session
   document after export (`src/cairn/mutations.py:209-225`; `src/cairn/invoke.py:531-547`).
   `render_exchange` puts it in the exchange tail (`src/cairn/exchange.py:342`).
5. **Invoke preamble.** Every peer prompt starts with
   `[cairn invoke] You are <peer> being invoked by <who> via cairn (round N).`
   (`src/cairn/invoke.py:1289-1301`). `INVOKE_PREAMBLE` matches it, with an optional
   `/plan `-style prefix and the owner form (`src/cairn/digest_condense.py:43-56`).
   `digest_condense` imports only `hashlib`, `re` and `dataclasses`
   (`src/cairn/digest_condense.py:17-21`), so any module can import it without a cycle.
6. **Records.** `SessionRecord.metadata` is the parsed front matter
   (`src/cairn/store.py:115`, `:216`), cached by the catalog
   (`src/cairn/store.py:230-245`). Exchange documents are type `agent-exchange` and never
   become records (`src/cairn/store.py:182-184`; `src/cairn/exchange.py:11-14`).
7. **recall list** rows carry `source`, `session_id`, `reference`, `time`, `msgs`,
   `total_messages`, `title`, `project`, `cwd`, `precision`, `path`, `type`, `parent`
   (`src/cairn/recall.py:154-171`). No `owner`.
8. **digest.** Front matter fields are built at `src/cairn/digest.py:283-309`; `owner`
   is the stored label, else the owner form of the preamble (`:293-295`). The JSON row
   has no `owner` (`src/cairn/digest.py:324-335`). Digest sources are `claude`, `codex`,
   `antigravity`, `cursor` (`src/cairn/digest.py:54`), so a digest never holds an
   `imported` document.
9. **Exchange head guard.** `validate_head` refuses a head key that starts with a tail
   key name or is `source`/`native_id` (`src/cairn/exchange.py:235-248`). `provenance`
   starts with neither.
10. **Service docs** are byte copies (`shutil.copy2`,
    `src/cairn/sources/service_docs.py:90`) and the skip check compares bytes
    (`:81-86`). **Journal** entries are written by the routine's peer agent, not by cairn;
    the `journal` collection is store-less (`docs/routine-spec.md` section 8.1).
11. **Naming.** "Provenance" already names the digest's frame citations
    (`src/cairn/digest.py:1`, `src/cairn/frames.py:10`, `README.md:485`). Docs for this
    layer say "the `provenance` key" to keep the two apart.

---

## 3. Surface

### 3.1 Front-matter key

| Key | Where | Values | Written by |
|---|---|---|---|
| `provenance` | session documents: head, last line of the metadata block, directly before `status:`; exchange documents: head, directly before `title:` (4.1) | `user`, `agent`, `tool`, `imported` | `export_one` for every session document; `render_exchange` for every exchange document |

Unquoted scalar, for example `provenance: agent`. Not a preserved-tail key: it is not
added to `PRESERVED_FIELDS` or `_TAIL_KEYS`.

### 3.2 CLI flags

| Command | Flag | Behaviour |
|---|---|---|
| `cairn recall list` | `--provenance VALUE` (repeatable, `choices` = the four values) | keep only rows whose effective value is in the set |
| `cairn recall digest` | `--provenance VALUE` (repeatable, same choices) | keep only sessions whose digest value is in the set; `--provenance tool` excludes all agent-produced sessions |

No new config key. No new exit code: a bad value is argparse exit 2; a digest filtered
to nothing is the existing exit 3 (`src/cairn/digest.py:446`).

### 3.3 Messages

| Case | Stream | Exact text |
|---|---|---|
| stored value outside the set | stderr, once per record per command | `Warning: <path>: provenance '<value>' is not one of user, agent, tool, imported; reading it as <derived>` |
| bad flag value | stderr, exit 2 | argparse `invalid choice` text for `--provenance` (wording varies by Python version; tests assert the exit code and `--provenance`) |

### 3.4 Store-less collections (documented, not written)

cairn does not write these files, so this layer writes no key into them:

| Collection | Value a reader assumes |
|---|---|
| `service-docs` | `imported` |
| `journal` | `agent` |
| any other store-less collection (`notes`) | `user` |

The routine workflow doc tells prompt authors to put `provenance: agent` in each journal
entry's front matter. Search surfaces that need these values belong to a later change.

---

## 4. Behaviour

### 4.1 At export: `provenance.at_export(source, doc_type, messages)`

Checked in this order; the first match wins:

1. `doc_type` is the source's `subagent_type` (`src/cairn/registry.py:15-26`): `agent`.
2. `source == "granola"`: `imported`.
3. The first message with role `user` and non-empty text matches
   `INVOKE_PREAMBLE` at its start: `agent`.
4. Otherwise: `tool`.

`provenance.head_lines(source, markdown, data)` reads `doc_type` from the rendered head
(`fm_mod.parse_frontmatter(markdown).get("type")`, after `render_document` at
`src/cairn/sources/base.py:302`) and the messages through `messages_of(data)`
(`src/cairn/store.py:22-24`), and returns `["provenance: <value>"]`. `export_one` joins
those lines after the `metadata_lines` output, so the key follows the `activity_dates`
lines. The existing parse and identity checks then validate the whole
head (`src/cairn/sources/base.py:306-322`).

`render_exchange` appends `provenance: agent` to its head before `title_line`
(`src/cairn/exchange.py:339`), so `validate_head` (`:341`) checks it.

### 4.2 On read: `provenance.effective(fm, source, path) -> str`

1. `fm["provenance"]` is one of the four values: return it.
2. Present but invalid: print the 3.3 warning, continue with step 3.
3. Derive: `type` is the source's `subagent_type`: `agent`. `source == "granola"`:
   `imported`. Non-empty `owner`, or a `links` entry whose role is `parent`
   (the same test as `src/cairn/digest.py:110-113`): `agent`. Otherwise `tool`.

The derivation reads only front matter, so it uses the catalog and reads no body.

### 4.3 Migration (decision: compute on read, no backfill)

Existing documents have no key. Step 3 of 4.2 gives them a value with no rewrite. A
document gains the stored key on its next natural re-render (native change). A
`SCHEMA_VERSION` bump would force a re-render of every document on the next
`export --all`, and some native logs are gone (`src/cairn/store.py:149-151`), so this
layer does not bump it. A `--backfill` would write the same value that step 3 already
returns. The one gap: a peer run with no caller and no `--owner` ("invoked by an
operator"), exported before this layer, reads `tool` in `recall list` until it is
re-rendered. The digest closes the gap (4.5).

### 4.4 `cairn recall list`

After the subagent and `--min-msgs` filters (`src/cairn/recall.py:158-167`), compute
`effective` once per record. With `--provenance`, drop records outside the set.
`--provenance agent` does not show subagents unless `--include-subagents` is also
given. Each JSON row gains `"provenance"` and `"owner"` (the stored label, or `null`)
after `"parent"`. Each text row becomes
`<ref>  <time>  <title>  [<precision>] <provenance>[ owner=<owner>][ (subagent of X)]`.
`recall expand` prints `Provenance: <value>[ (owner <owner>)]` after its `File:` line
(`src/cairn/recall.py:149-152`).

### 4.5 `cairn recall digest`

In `build` (`src/cairn/digest.py:273-363`): the value is
`effective(..., peer_body=opens_with_preamble(frames))`. When the document has no valid
stored key and its first user frame with text starts with the invoke preamble, the value
is `agent`. This is the export test of 4.1 on the frames the digest already parses
(`src/cairn/digest.py:131-144`): a preamble pasted later in a session a person drove does
not count, although `lineage` still reads it for `role` and `parent`. A `parent` link is
front-matter evidence and `derive` already reads it (4.2). Front matter gains
`("provenance", value)` after `orphan_peer`, before `owner`. The JSON row gains
`"provenance"` and `"owner"` after `"parent"`, with the same values as the front matter.
`owner` is the stored `owner` label, else the owner label of the invoke preamble that
`lineage` found (`src/cairn/digest.py:127`), else `null`.

In `run_digest` (`src/cairn/digest.py:389-446`), drop built digests outside the
`--provenance` set before `_stems` and `write`. A filtered session is not listed in
`skipped`; that list stays for legacy and corrupt exports. If nothing remains, exit 3.
The text line becomes `<ref>  <role>  <provenance>  frames_today=N  chars=N[  owner=X][  [truncated N]]`.

---

## 5. Data formats

Session document head (claude, invented values):

```yaml
cairn_schema: "2"
source: "claude"
native_id: "abc12345-feed-0000-0000-000000000000"
activity_dates:
  - 2026-03-28
provenance: tool
status: active
```

Exchange document head: `... run_status: finished`, `rounds: 1`, `started_at: ...`,
`provenance: agent`, `title: "..."`, `last_activity: ...`, then the tail.

`recall list --json` row:

```json
{"source": "codex", "session_id": "019a...", "reference": "codex:019a...",
 "time": "2026-03-28T05:20:11+00:00", "msgs": 1, "total_messages": 4,
 "title": "...", "project": "demo", "cwd": "/tmp/demo", "precision": "messages",
 "path": "/tmp/x/codex-sessions/....md", "type": "codex-session", "parent": null,
 "provenance": "agent", "owner": "routine:daily"}
```

Digest front matter adds `provenance: "agent"` (the digest quotes every string,
`src/cairn/digest.py:232-241`). Digest `--json` row adds `"provenance"` and `"owner"`.

---

## 6. Failure modes

| Case | Behaviour |
|---|---|
| hand-edited invalid value | warning (3.3), derived value used; the next re-render writes the computed value |
| person types a line that starts with `[cairn invoke] You are` as the first message | `agent`; documented |
| person resumes a peer session by hand | stays `agent` (first message decides) |
| operator peer exported before this layer | `tool` in `recall list` until re-render; `agent` in the digest (4.5) |
| key in the tail position of a hand-edited file | `rewrite_preserved_tail` drops it on the next mutation, as for any non-tail key after `status:`; the next export restores it |
| rendered head does not parse | existing refusal, nothing written (`src/cairn/sources/base.py:309-316`) |
| filter removes every digest session | exit 3, marker-only directory, as for an empty day |

---

## 7. Tests

`tests/test_provenance.py` (new):

| Test | Asserts |
|---|---|
| `test_plain_transcript_is_tool` | user-driven messages give `tool` |
| `test_invoke_preamble_is_agent` | first user message from `invoke.preamble(...)` gives `agent` |
| `test_mode_prefixed_preamble_is_agent` | `/plan [cairn invoke] ...` gives `agent` |
| `test_owner_form_preamble_is_agent` | `being invoked by routine:example-daily` gives `agent` |
| `test_preamble_in_later_message_is_tool` | only the first user message counts |
| `test_subagent_type_is_agent` | `claude-subagent`, `codex-subagent`, `antigravity-subagent` |
| `test_granola_is_imported` | source `granola` |
| `test_stored_value_wins` | stored `imported` on a claude doc returns `imported` |
| `test_absent_key_derives_from_owner_and_parent_link` | `owner` or `parent` link gives `agent`; `peer`/`child` link alone gives `tool` |
| `test_invalid_stored_value_warns_and_derives` | exact 3.3 warning on stderr, derived value |
| `test_null_or_blank_owner_is_no_owner` | `owner_of` reads `null`, blank and non-string owners as `None`, so they derive `tool` (added in review) |
| `test_empty_stored_value_warns` | `provenance:` and `provenance: ""` warn with `provenance ''` (added in review) |
| `test_absent_key_is_silent` | no key: derived value, empty stderr (added in review) |

Other modules:

| Module :: test | Asserts |
|---|---|
| `tests/test_frontmatter.py::MutationScoping::test_provenance_survives_tail_mutations` | status, tags, rating, comment, owner and link mutations keep `provenance:` byte for byte |
| `tests/test_sources_export.py::test_export_writes_provenance_before_status` | a Claude export has `provenance: tool` as the line before `status:` |
| `tests/test_sources_export.py::test_reexport_keeps_provenance_and_annotations` | re-export after a note and a tag keeps the key, the tag and My Notes, and recomputes a hand-edited key |
| `tests/test_sources_export.py::test_peer_export_is_agent` | a Codex rollout whose first user message carries the preamble exports `provenance: agent` |
| `tests/test_invoke.py::test_exchange_head_carries_provenance_agent` | `render_exchange` head holds `provenance: agent` and passes `validate_head` |
| `tests/test_recall.py::TestRecallList::test_list_json_has_provenance_and_owner` | row has `provenance: tool`, `owner: null` |
| `tests/test_recall.py::TestRecallList::test_provenance_filter` | `--provenance agent` gives `[]`, `--provenance tool` gives the row |
| `tests/test_recall.py::TestRecallList::test_bad_provenance_exits_2` | exit 2, `--provenance` in stderr |
| `tests/test_recall_digest_condense.py::test_digest_carries_provenance` | front matter and JSON row hold `provenance` and `owner` |
| `tests/test_recall_digest_condense.py::test_provenance_tool_drops_peers` | peer and owner-preamble sessions absent, primary present |
| `tests/test_recall_digest_condense.py::test_operator_peer_without_key_is_agent` | no stored key, operator preamble: `agent` |
| `tests/test_recall_digest_condense.py::test_filter_to_nothing_exits_3` | exit 3, only the marker file |
| `tests/test_recall_digest_condense.py::test_later_preamble_without_key_is_tool` | a preamble in a later user message (operator, owner and caller forms) leaves `provenance: tool`, `role: primary`, no parent, `orphan_peer: false`, no owner (added in review) |
| `tests/test_recall_digest_condense.py::test_indented_preamble_reads_as_at_export` | leading whitespace before the preamble reads `agent` at export and in the digest (added in review) |
| `tests/test_recall_digest_condense.py::test_stored_key_beats_body_evidence` | a valid stored key wins over a peer body; an invalid or empty one warns and reads `agent` (added in review) |
| `tests/test_recall_digest_condense.py::test_filter_to_nothing_clears_a_populated_directory` | a rerun filtered to nothing removes the earlier markdown and sidecars (added in review) |
| `tests/test_recall.py::TestRecallList::test_owner_in_text_and_json` | `owner=` in list text rows, `(owner ...)` in expand, owner in JSON (added in review) |
| `tests/test_recall.py::TestRecallList::test_null_or_blank_owner_is_no_owner` | `owner: null` and a blank owner give `"owner": null` (added in review) |
| `tests/test_recall.py::TestRecallSubagents::test_subagent_export_is_agent` | a Claude subagent export writes `provenance: agent` (added in review) |
| `tests/test_recall.py::TestRecallSubagents::test_provenance_agent_needs_include_subagents` | `--provenance agent` lists the subagent only with `--include-subagents` (added in review) |
| `tests/test_sources_export.py::TestGranolaExport::test_export_is_imported` | a Granola export writes `provenance: imported` (added in review) |

Fixture values stay invented (`/tmp/demo`, `routine:example-daily`).

---

## 8. Docs to update

- `docs/provenance-spec.md`: this spec, without the "Dependencies" line.
- `README.md`: the recall block (`README.md:475-500`): the key, the value table, the
  flag, the new JSON fields.
- `skills/recall/SKILL.md`: Step 2 JSON field list (line 34) and digest flags (line 70);
  one line: "use `--provenance tool` to leave out peer and routine runs".
- `skills/cairn/SKILL.md`: the store-format paragraph near line 144: one sentence on the
  key.
- `skills/cairn/workflows/routine.md`: journal entries should declare
  `provenance: agent`; a routine that digests the previous day can pass
  `--provenance tool`.
- `docs/recall-digest-spec.md`: add `provenance` to the front-matter table (section 8.2)
  and the JSON row (section 8.4), marked as added by the provenance layer.

The orchestrator owns `CHANGELOG.md` and `__version__`.

---

## 9. Implementation plan

| Step | Files | Lines (est.) |
|---|---|---|
| 1. New module: `VALUES`, `at_export`, `derive`, `effective` | `src/cairn/provenance.py` | 70 |
| 2. `head_lines(source, markdown, data)` in the new module | `src/cairn/provenance.py` | 8 |
| 3. Join the head lines in `export_one` | `src/cairn/sources/base.py` | 4 |
| 4. `provenance: agent` in the exchange head; docstring line | `src/cairn/exchange.py` | 3 |
| 5. Flag, filter, JSON fields, text rows, expand line | `src/cairn/recall.py` | 25 |
| 6. Digest field, row, lineage fallback, filter, text | `src/cairn/digest.py` | 20 |
| 7. Tests (section 7) | 6 test modules | 220 |
| 8. Docs (section 8) | 6 files | 150 |

Order: 1, 2-4 with their tests, 5-6 with theirs, then docs, then `make check`. Before
the push, grep the diff for home paths and private names (CLAUDE.md hygiene rules).

---

## 10. Open questions (resolved: each recommended default was adopted)

1. **Subagent documents: `agent` or `tool`?** A subagent's prompt comes from the parent
   agent; the role header for such messages is already `### Agent`
   (`src/cairn/rendering.py:24`). Recommended default: `agent`.
2. **Should `recall list` also use body evidence for operator peers without a stored
   key?** It would read the first frame of each candidate. Recommended default: no;
   keep `list` on front matter only and let re-render fill the key.
3. **Should a derived `annotated` flag (tags, rating or comments non-empty) join the
   JSON rows?** `## My Notes` lives in the body, so a front-matter-only flag would be
   partial. Recommended default: not in this layer.

---

## 11. As implemented

- `metadata_lines` is unchanged. The key comes from `provenance.head_lines`, and
  `export_one` changes only the line that joins the head block, so other changes to that
  block merge with at most a one-line conflict.
- `provenance.effective` takes a keyword `peer_body`. The digest passes
  `opens_with_preamble(frames)` (the first user frame with text starts with the invoke
  preamble), so a document without a valid stored key reads `agent`, and the warning for
  an invalid stored value names that value. A present but empty value (`provenance:` or
  `provenance: ""`) is invalid and warns too; only an absent key is silent.
- `recall list` text rows put the value after the precision label:
  `<ref>  <time>  <title>  [<precision>] <provenance>[ owner=<owner>][ (subagent of X)]`.
  Digest text rows put `owner=X` after `chars=N`.
