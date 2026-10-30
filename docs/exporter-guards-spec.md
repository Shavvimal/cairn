# Exporter guards: implementation spec

Status: implemented in this PR, on top of `main` at 2.9.0. Section 11 lists where the
implementation departs from the sections above and why; where they differ, section 11
wins. Every `path:line` cites `main` at 2.9.0.

**Dependencies:** none. Layer 01 (`provenance`) adds frontmatter in the same place
this layer adds its stamp (`src/cairn/sources/base.py:303-304`). Both layers must add
a helper that returns lines and change only the one join line, so the merge is
mechanical. This layer does not define or read `provenance`.

The four guards are independent. Ship them as four commits, in the order 4, 1, 3, 2.
Each commit passes `make check` alone. Guard 3 reads guard 4's constant and guard 2
reads guard 1's findings file. Section 9 states what each guard does when the other
one is absent.

## 1. Purpose and non-goals

Purpose. An exporter must never fail silently when a host changes its transcript
format. Today a format change shows up as missing or empty documents, and nobody sees
it until a recall misses. This layer adds four guards:

1. **Parser-drift guard.** A source file holds records, but the adapter renders zero
   user or assistant messages. The export refuses to write and records a finding.
   Export, the hook worker, `cairn sync` and `cairn doctor` report the finding.
2. **Trailing-window gap check.** `cairn sync` (cron and all modes) compares the
   session ids on disk for the last 30 days with the documents in the store. It
   re-exports the missing ones once, checks again and reports the counts.
3. **Per-source parser fixtures.** Each adapter gets a small invented transcript and
   an expected-messages file. A test scores message recall exactly, so a parser change
   shows up as a number.
4. **Spec target.** Each adapter gets a dated constant that names the harness version
   its record shapes were checked against. The fixture test and doctor show it. Every
   exported document gets a two-key stamp.

Non-goals:
- Stale documents (the document exists but its `source_revision` is old). The gap
  check finds missing documents only. The windowed export already re-renders changed
  sources (`src/cairn/store.py:73-77`).
- Granola (remote API, no file on disk) and service docs (byte copies, never parsed:
  `src/cairn/sources/service_docs.py:73-90`).
- Cursor in the gap check (see open question 2).
- A general hold store for malformed files. Section 4.5 explains this and keeps one
  narrow part.
- Re-export when the adapter version or spec target changes. The stamp makes that
  possible later. This layer does not do it.

## 2. Current behaviour

| Fact | Evidence |
|---|---|
| Malformed and blank JSONL lines are skipped with no count | `src/cairn/jsonl.py:44-51` |
| Claude drops a session with no messages and no `sessionId` and returns `None`. `run_export` then marks the run failed but prints no reason | `src/cairn/sources/claude.py:711-713`, `:928-934` |
| Claude writes a document for a file with a `sessionId` but zero messages | `src/cairn/sources/claude.py:711-713` (only the "both missing" case returns) |
| Codex drops a rollout without `session_meta.id` with no message | `src/cairn/sources/codex.py:392-393`, `:641` |
| Codex drops a live replaying child on purpose (not drift) | `src/cairn/sources/codex.py:436-438`, `LIVE_CHILD_WINDOW_S = 600` at `src/cairn/sources/codex_replay.py:37` |
| Antigravity exports a transcript that parses to zero messages | `src/cairn/sources/antigravity.py:240-257`, `:663-678` |
| Cursor skips a composer with no headers, with no message | `src/cairn/sources/cursor.py:123-125`, `:452-455` |
| Per-file read errors already continue with the other files | `codex.py:635-639`, `antigravity.py:585-588`, `claude.py:930-933` |
| `export --json` is accepted but ignored by every `run_export` | `src/cairn/cli.py:38`. The only reader is `base.py:466` (`list`) |
| The hook only enqueues. The worker runs `cairn hook export` in a child and discards its stderr on success | `src/cairn/hooks.py:114-131`, `:173-178` |
| A failed worker export becomes `failed`, then `exhausted` after 3 attempts. Doctor shows up to 5 errors | `src/cairn/hooks.py:252-256`, `src/cairn/admin.py:1239-1245` |
| `cairn sync` exports each source with `--since <sync.since>` (default `1d`) in a subprocess | `src/cairn/sync.py:57-77`, `src/cairn/config.py:133` |
| A session older than the window whose export failed is never retried | `codex.py:734-735`, `antigravity.py:665-666`, `claude.py:909-913` |
| Every adapter has `version = "2.0.0"` | `base.py:42`, `claude.py:549`, `codex.py:568`, `antigravity.py:498`, `cursor.py:361` |
| The frontmatter parser rejects nested and flow mappings | `src/cairn/frontmatter.py:119-123`, `:137-141` |
| The catalog rejects the whole cache file if any value is not a string or a list of strings | `src/cairn/catalog.py:122-127` |
| Metadata lines are put in front of `status:` in `export_one` | `src/cairn/sources/base.py:303-304`, `src/cairn/store.py:27-59` |
| A prior document with bad frontmatter blocks re-export with one stderr warning | `src/cairn/sources/base.py:256-263`, `:299-301` |
| Fixtures: only `tests/fixtures/invoke/*` exist. Adapter shapes are inline in tests | `tests/test_sources_export.py:86-171` (Codex), `:177-221` (Cursor), `:454-530` (agy) |

## 3. Surface

### 3.1 Files

| Path | Kind |
|---|---|
| `src/cairn/sources/spec_targets.py` | new, constants only, no config read |
| `src/cairn/sources/guards.py` | new: `Finding`, `check_parse`, `FindingsStore`, `ParserDrift` |
| `src/cairn/gaps.py` | new: enumerators, `scan`, `refs`, `gap_sources` |
| `<data_root>/exporter/findings.json` | new state file (user data, not tracked) |
| `tests/fixtures/<source>/` for `claude`, `codex`, `antigravity`, `cursor` | new fixtures |

### 3.2 CLI

| Command | Change |
|---|---|
| `cairn <source> export --json` | Prints exactly one JSON object on stdout (section 5.2). Human lines go to stderr. |
| `cairn <source> export` | Exit 1 when any error-level finding is raised in this run. Warn-level findings do not change the exit code. |
| `cairn hook export` (hidden) | A refused target exits 1 with `Error: parser drift: ...` |
| `cairn sync --cron`, `cairn sync` | New stages `gap-check:<collection>` and `gap-heal:<collection>`, and one `[exporter-drift]` log line. `--hook` mode does not run them. |
| `cairn doctor` | New warn-only lines: `exporter [<source>]`, `parser drift`, `gap check [<collection>]`. Doctor never returns FAIL for these. |

No new config key. The window is a constant (open question 3).

### 3.3 Frontmatter

There are two flat keys, and `export_one` writes them after the metadata lines:

```yaml
exporter_version: "2.0.0"
exporter_spec_target: "2026-10-30"
```

An earlier design used one nested key, `exporter: {adapter, version, spec_target}`.
A nested mapping cannot be used. The parser raises on it (`frontmatter.py:119-123`), and the catalog
would reject every cache load (`catalog.py:122-127`). The adapter is already the
`source:` key (`store.py:37`). The stamp is worth one line each. A future selective
re-export can find old-adapter documents with `rg exporter_version`. A debugger can
see which parser produced a document. The keys are regenerated on each export, like
the other metadata. They are not in `_TAIL_KEYS` (`frontmatter.py:41`), and they do
not bump `SCHEMA_VERSION` (`store.py:19`), so old documents are not forced to
re-export.

### 3.4 Exact messages

| Where | Text |
|---|---|
| export stderr, error | `Error: parser drift: {source}:{id} {kind}: rendered 0 messages from {records} records ({bytes} bytes) in {path}; adapter {version}, spec target {checked} ({harness} {harness_version}); see 'cairn doctor'` |
| export stderr, warn | `Warning: parser drift: {source}:{id} no-user-turns: {assistant} assistant messages, 0 user messages in {path}; adapter {version}, spec target {checked} ({harness} {harness_version})` |
| export stderr, unreadable lines | `Error: parser drift: {source}:{id} unreadable-lines: 0 of {lines} lines parse as JSON ({bytes} bytes) in {path}; ...` (the tail is the same as the error row) |
| export stderr, document | `Error: refusing to re-export {source}:{id}: existing document {path} has unparseable frontmatter ({reason}); fix or move it` |
| sync log | `  [gap-check] {collection}: {on_disk} on disk in 30d, {missing} missing, {held} held by parser drift` |
| sync log | `  [gap-check] {collection}: after heal {n} missing` and, when n > 0, `: {ref}, {ref}, ... (+{k} more)` with at most 5 refs |
| sync log | `  [exporter-drift] {n} open findings: {ref} {kind}, ...` with at most 5 |
| doctor | `ok   exporter [{source}] - adapter {version}, spec target {checked} ({harness} {harness_version})` |
| doctor | `warn parser drift - {ref} {kind}: {detail}; first seen {iso}` (at most 5), then `warn parser drift - +{k} more; see {findings path}` |
| doctor | `ok   gap check [{collection}] - {n} sessions on disk in the last 30 days, all exported` |
| doctor | `ok   gap check [{collection}] - {n} sessions on disk in the last 30 days, no unexplained gaps; {h} held by parser drift` when drift findings hold ids with no document |
| doctor | `warn gap check [{collection}] - {m} of {n} sessions in the last 30 days have no document: {refs}; run 'cairn sync'` |

`{ref}` is `source:<first 8 chars of the id>`. `{path}` is printed as found. It goes
to the user's terminal and log, never to a tracked file.

## 4. Behaviour

### 4.1 Guard 4: spec target

`spec_targets.py` defines this:

```python
@dataclass(frozen=True)
class SpecTarget:
    checked: str          # ISO date the record shapes were checked against a live store
    harness: str          # "Claude Code"
    harness_version: str  # the version on the machine that day
    store: str            # "JSONL, one file per session"
SPEC_TARGETS: dict[str, SpecTarget]  # keys: claude, codex, antigravity, cursor
```

The implementer fills `harness_version` from the machine on the day of the check and
sets `checked` to that day. Known earlier observations: Claude Code 2.1.302 and Codex
0.160.0 on 2026-10-17 (`docs/sync-robustness-plan.md`), agy 1.4.6
(`antigravity.py:12`). Cursor has no recorded version. Write `"unrecorded"` rather
than guess. `ConversationSource` gets `spec_target: SpecTarget | None = None`, and each
adapter sets it from the table. Doctor imports only `spec_targets.py`, so it does not
trigger the config read at source-module import time (`claude.py:46-49`).

### 4.2 Guard 1: where the count is observed

The count is observed at parse time in each adapter, through one base method. It is
not observed in rendering. `render_conversation` (`rendering.py:104`) renders any
list. At that point it cannot tell a legitimately empty session from a dropped one,
and it does not know the source byte count.

1. `iter_jsonl(path, skip_prefixes=(), stats: JsonlStats | None = None)` fills a
   `JsonlStats(lines, records, malformed, types: Counter[str])` in place. `types`
   counts `str(record.get("type"))` for dict records. Lines dropped by
   `skip_prefixes` count as `lines` only. The default `None` keeps every current
   caller unchanged.
2. Each adapter declares `CONVERSATIONAL_TYPES` (record types that should produce a
   message) and `IGNORED_TYPES` (types the parser skips on purpose, measured from a
   real store on the implementer's machine).

   | Adapter | Conversational | Unit |
   |---|---|---|
   | Claude | `user`, `assistant` (`claude.py:232-305`) | `<id>.jsonl`, `agent-<id>.jsonl` child |
   | Codex | `response_item`, `event_msg` | rollout file |
   | agy | `USER_INPUT`, `PLANNER_RESPONSE` | `transcript_full.jsonl` |
   | Cursor | n/a. The unit is `len(bubble_order)` plus legacy turns | composer |

3. After a parse, the adapter calls
   `self.guard_parse(native_id, source_file, stats, data) -> bool`. That method is on
   `ConversationSource` and is backed by `guards.check_parse`. Messages are
   `messages_of(data)` (`store.py:22-24`). `user` and `assistant` count by role.
   Classification, first match wins:

   | Kind | Condition | Severity | Effect |
   |---|---|---|---|
   | `unreadable-lines` | bytes > 0, records == 0, malformed > 0 | error | no write |
   | `zero-messages` | messages == 0 and (conversational records > 0 or any type not in either set) | error | no write |
   | `empty` | messages == 0 and every record type is in `IGNORED_TYPES` | info | today's behaviour stays. Recorded only so the gap check does not count the file |
   | `no-user-turns` | top-level session (no `parent_session_id`), assistant > 0, user == 0 | warn | the document is written |
   | none | otherwise | | clears any open finding for this id |

   Codex child rollouts and agy children have no user turn by design, so they are
   exempt from `no-user-turns`. For Cursor, the condition `bubble_order` non-empty and
   zero messages means `zero-messages`.

4. Call sites: `claude.py` after `:711` (parent) and after `:789` (child). Codex
   `_parse_rollouts` (`:633-643`) calls it for parsed data. It also calls it with
   `data=None` and the filename id when `parse_rollout` returns `None`, the file is
   older than `LIVE_CHILD_WINDOW_S` and `records > 0`. That case gives kind
   `no-session-id` (error). Other call sites: `antigravity.py:_parse` (`:579-595`) and
   `cursor.py` after `fetch_bubbles` (`:461-463`).
5. `guard_parse` returns `False` for an error. The adapter then skips the session the
   same way it handles an unreadable file today, and adds it to its failed set, so
   `run_export` exits 1. Each `export_target` raises
   `guards.ParserDrift(finding.message)`. That class is a `ValueError`, so it reaches
   `hooks.py:508-510` as `Error: parser drift: ...`, exit 1. The worker stores the
   message in the request `error`. `cairn hook status` and doctor show it.
6. "Loud" for a hook means this. The host hook still exits as today: it only enqueues
   (`hooks.py:114-131`), and agy always answers `{}` with exit 0
   (`hooks.py:363-375`). The finding appears in five places: `findings.json`, the
   worker request error, the worker log, every later `cairn sync` log (one
   `[exporter-drift]` line while it stays open) and `cairn doctor`.

### 4.3 Findings store

`FindingsStore(data_root)` reads `<data_root>/exporter/findings.json` once per
process. It writes only on a change (new finding, kind changed, cleared). Each write
is a read-modify-write under `locked(lock_path(<data_root>/exporter, "findings"))`
with `atomic_write`. A pass on an id that has no entry costs no I/O. Entries whose
`path` no longer exists are pruned on the next write. Doctor and the gap check read it
without the lock.

### 4.4 `--json` on export

`cli.run` (`cli.py:131-141`): when `args.command == "export"` and `args.json`, it
sets `args.quiet = True`. It runs the handler under
`contextlib.redirect_stdout(sys.stderr)`, then prints `source.report.as_dict()` to
the real stdout. `ConversationSource.report` is an `ExportReport` that `export_one`
fills (distinct paths written) and `guard_parse` fills (findings from this run).

### 4.5 Optional scope: holds (recommendation: out, except one part)

A hold store is out. A bad JSONL line is skipped per line (`jsonl.py:47-50`), and a
torn last line is normal for a live file. Holding the whole file would hide good
sessions. Service docs are copied as bytes and never parsed. An unreadable source
file already fails only itself. The drift finding is the reason record for the cases
that matter.

One part is in. `load_existing` (`base.py:256-263`) silently blocks re-export of a
session whose document has bad frontmatter. It records an `unparseable-document`
finding (error) through the same store, so doctor lists it. That is one call site and
no new file.

### 4.6 Guard 2: gap check in `cairn sync`

`gaps.py` constants: `GAP_WINDOW_DAYS = 30`, `GAP_SETTLE_S = 600` (equal to
`LIVE_CHILD_WINDOW_S`), `GAP_HEAL_TIMEOUT_S = 600`.

1. **Enumerate** without reading any file contents (config-free functions that take
   the store path, like `jsonl.py`):
   - Claude: `iter_session_files(store)` (`jsonl.py:65-76`). Skip top-level `agent-*`
     stems (`claude.py:917-919`). The id is the stem. Children come from
     `iter_subagent_files` (`jsonl.py:92-102`) as `agent-<id>`.
   - Codex: `rglob("rollout-*.jsonl")`, size > 0 (`codex.py:453-457`). The id is the
     trailing UUID in the filename. `export_target` already relies on the id being in
     the name (`codex.py:763`).
   - agy: `brain/<id>/` folders with a transcript (`antigravity.py:559-571`,
     `harnesses/antigravity.py:231-238`).
     Its time is the index's `last_modified` when present, else the transcript mtime
     (section 11).

   A candidate file has `now - 30d <= mtime <= now - GAP_SETTLE_S`.
2. **Exported ids**: `load_catalog(config.output_dir(collection))`, then
   `native_id_of` over entries whose `type` is in `Source.session_types`. This is the
   same index as `base.py:154-168`.
3. **Diff**: `missing = on_disk - exported - held`. `held` means ids with an open
   finding of severity error or info.
4. **Heal**: when `missing` is non-empty, run
   `run_step("gap-heal:<collection>", _export(source, "--since", "30d", "-q"), timeout=GAP_HEAL_TIMEOUT_S)`.
   This is the normal export path, so the drift guard, locking and subprocess
   isolation (`sync.py:4-8`) apply.
5. **Verify**: refresh the catalog and diff again. A second pass with zero missing is
   the done signal. If any id is still missing, call
   `record("gap-check:<collection>", False)`, so the run ends `=== FAILED ===` with the
   name.
6. Placement: in `_stages` (`sync.py:362-374`) after the export steps and before
   `_index_stages`, only when `mode != "hook"`. A source whose config is disabled or
   absent is skipped, as in `_plan`. Then log one `[exporter-drift]` line when the
   findings store has open warn or error findings.

Cost bound: one `stat` per candidate file plus one catalog walk per collection. There
are no content reads unless something is missing. Then the cost is one windowed
export per affected collection. Claude skips current sessions with a catalog hit and
stats (`claude.py:694-701`). Codex and agy already parse every file on every run
(`codex.py:812`, `antigravity.py:760`).

Doctor runs steps 1 to 3 read-only (no heal) and prints the gap lines.

### 4.7 Guard 3: fixtures

Layout: `tests/fixtures/<source>/<case>.<ext>` plus `<case>.expected.json`.

| Source | Cases | Input |
|---|---|---|
| claude | `basic` (control records first, 2 user and 2 assistant turns, one tool call), `drifted` (only an unknown record type) | `.jsonl` |
| codex | `basic` (current `response_item` user shape plus legacy `event_msg user_message`), `child` (no user turn), `drifted` | `.jsonl` |
| antigravity | `basic` (vendored from `AGY_STEPS`, `test_sources_export.py:454-523`), `drifted` | `.jsonl` copied into a temp `brain/<id>/.system_generated/logs/transcript_full.jsonl` |
| cursor | `basic` | `.json` holding the `cursorDiskKV` rows. The test loads it into `sqlite3.connect(":memory:")`, as `test_sources_export.py:194-208` does. A binary `.vscdb` fixture is rejected because it cannot be reviewed in a diff. |

All text, ids and paths are invented (`/tmp/demo`, `x@y.z`). Scoring: an expected
message matches the first unmatched parsed message with the same role whose `text`
contains `text_contains`. recall = matched / expected. The test asserts recall == 1.0,
parsed user and assistant count == expected count, roles == `expected_roles`, and
`spec_target == SPEC_TARGETS[source].checked`. A failure message names the number and
the target, for example:
`claude/basic: message recall 3/4 (0.75); missing: user "Investigate the"; spec target 2026-10-30 (Claude Code 2.1.302)`.
A `drifted` case asserts that `check_parse` gives `zero-messages`.

## 5. Data formats

### 5.1 `findings.json`

```json
{"schema_version": 1,
 "findings": {"claude:1a2b3c4d-...": {
   "source": "claude", "native_id": "1a2b3c4d-...", "path": "/tmp/demo/1a2b.jsonl",
   "kind": "zero-messages", "severity": "error",
   "bytes": 88213, "lines": 412, "records": 410, "malformed": 2,
   "record_types": {"user": 3, "assistant": 5, "turn-v2": 402},
   "messages": 0, "user": 0, "assistant": 0,
   "file_revision": "<store.file_revision sha256>",
   "adapter_version": "2.0.0", "spec_target": "2026-10-30",
   "first_seen": "2026-10-30T08:00:00Z", "last_seen": "2026-10-30T09:00:00Z"}}}
```

`record_types` keeps the 20 most frequent types.

### 5.2 `export --json`

```json
{"schema_version": 1, "source": "codex", "exit": 1,
 "written": ["<path>", "..."], "failed": 1,
 "findings": [{"ref": "codex:019edba7", "kind": "no-session-id", "severity": "error", "message": "..."}],
 "exporter_version": "2.0.0",
 "spec_target": {"checked": "2026-10-30", "harness": "Codex", "harness_version": "0.160.0"}}
```

### 5.3 `<case>.expected.json`

```json
{"fixture_id": "claude/basic", "source": "claude", "spec_target": "2026-10-30",
 "expected_roles": ["user", "assistant"],
 "expected_messages": [{"role": "user", "text_contains": "Investigate the widget"},
                       {"role": "assistant", "text_contains": "off-by-one"}],
 "expected_finding": null}
```

## 6. Failure modes

| Case | Behaviour |
|---|---|
| A host adds a new ignored record type to a non-empty session | Messages > 0, so no finding. The fixture still passes. |
| A host renames the user record type | `zero-messages` (unknown types, 0 messages). No write. Exit 1. |
| A legitimate control-only file | `empty` (info). Doctor is silent. The gap check skips it. |
| A drift on an already-exported session | The last good document is kept (no write). The finding stays open until a parse passes. |
| A Codex session with a drifted fragment and a healthy one | The whole session is held: no fragment is exported, so an older fragment never replaces the document a newer one wrote. The finding clears only when every fragment of the session parses without an error or warn finding. |
| `findings.json` parses but has the wrong shape (not an object, another `schema_version`, an entry that is not an object or a field of the wrong type) | Same as corrupt: treated as empty with one stderr warning. |
| The worker retries a deterministic drift | It fails 3 times, then `exhausted` (`hooks.py:252-256`). After an upgrade, `cairn hook retry SOURCE:ID`. No state-machine change. |
| A Codex filename UUID differs from `session_meta.id` | It shows as still missing after heal and is named in the log. It is fixed in the enumerator, not hidden. |
| The user deletes a document on purpose | The gap check re-exports it while the source file is under 30 days old. This is documented, and no tombstone is added. |
| `findings.json` is corrupt | It is treated as empty and rewritten on the next change. One stderr warning. |
| The gap heal times out | `gap-heal:<collection>` TIMEOUT, as `run_step` reports it. Verify still runs and reports. |
| The store dir is missing | 0 on disk, no lines beyond the count. |

## 7. Tests

`tests/test_exporter_guards.py`:
- `test_iter_jsonl_stats_counts_lines_records_malformed_types`
- `test_default_iter_jsonl_unchanged_without_stats`
- `test_zero_messages_with_unknown_types_is_error`
- `test_control_only_file_is_empty_info`
- `test_all_lines_malformed_is_unreadable_lines`
- `test_no_user_turns_warns_for_top_level_only`
- `test_claude_drift_refuses_write_and_keeps_last_good_document`
- `test_codex_rollout_without_session_meta_is_no_session_id`
- `test_codex_live_child_is_not_drift`
- `test_cursor_headers_without_bubbles_is_zero_messages`
- `test_finding_cleared_after_clean_parse`
- `test_findings_written_only_on_change`
- `test_export_json_prints_one_object_and_exits_1_on_error`
- `test_hook_export_reports_parser_drift_message`
- `test_unparseable_document_recorded`
- `test_rendered_frontmatter_has_exporter_keys_and_catalog_accepts_it`

`tests/test_parser_fixtures.py`:
- `test_every_fixture_scores_full_recall` (subTest per fixture)
- `test_drifted_fixtures_raise_zero_messages`
- `test_fixture_spec_target_matches_adapter`
- `test_every_file_source_has_a_spec_target`
- `test_recall_scorer_reports_partial_recall` (scorer self-test)

`tests/test_gaps.py` (on `StoreFixture`, `tests/test_shared_store.py:34`):
- `test_enumerates_claude_parents_and_children_skips_legacy_agent_files`
- `test_codex_id_from_filename`
- `test_settle_window_excludes_live_files`
- `test_missing_session_is_healed_and_second_pass_is_clean`
- `test_still_missing_fails_the_named_step`
- `test_drift_held_ids_are_not_missing`
- `test_hook_mode_skips_gap_check`
- `test_no_heal_subprocess_when_nothing_missing`

`tests/test_doctor_exporter.py`:
- `test_doctor_prints_spec_targets_drift_and_gaps_warn_only`

## 8. Docs to update

- `docs/exporter-guards-spec.md` (this spec, reworded as cairn's own design)
- `README.md`: the sync section (around `:410-470`): gap check, `[exporter-drift]`,
  re-appearing deleted documents. The doctor section: new lines.
- `skills/cairn/SKILL.md`: `export --json` and what a parser-drift error means.
- `skills/cairn/reference/claude.md`, `codex.md`, `antigravity.md`, `cursor.md`: spec
  target, fixture location, drift kinds.
- `references/harnesses/contract.md`: an adapter must declare conversational and
  ignored types, a spec target and fixtures.

## 9. Implementation plan

| Step | Guard | Files | Lines (estimate) |
|---|---|---|---|
| 1 | 4 | `sources/spec_targets.py`, `sources/base.py` (attribute, `exporter_lines`, join at `:303`), 4 adapters (one line each), `admin.py` doctor lines | 110 |
| 2 | 1 | `jsonl.py` stats, `sources/guards.py`, `base.py` (`guard_parse`, `report`, `load_existing` finding), call sites in claude, codex, antigravity, cursor, `export_target` raises, `cli.py` `--json`, `sync.py` drift line, doctor drift lines | 420 |
| 3 | 3 | `tests/fixtures/{claude,codex,antigravity,cursor}/`, `tests/parser_fixtures.py` scorer, `tests/test_parser_fixtures.py` | 330 including fixtures |
| 4 | 2 | `gaps.py`, `sync.py` `_stages`, doctor gap lines, `tests/test_gaps.py` | 380 |
| 5 | | Docs from section 8 | 150 |

Independence: without step 1, step 3 drops `test_fixture_spec_target_matches_adapter`
and step 2 omits the spec target from its messages. Without step 2, the gap check
sets `held` to the empty set, and step 3 drops the `drifted` cases.

## 10. Open questions

1. A gap that heal cannot close: should the sync step fail, or warn? **Default: fail**
   the `gap-check:<collection>` step. Silent holes are the defect this layer exists to
   catch, and the summary names the step (`sync.py:425-427`), so other failures stay
   visible.
2. Should Cursor be in the gap check? **Default: out.** Enumeration has to decode every
   `composerData` blob, and composers without headers are skipped on purpose
   (`cursor.py:123-125`). That gives permanent false gaps until the drift guard for
   Cursor proves itself. The Cursor drift guard and fixture are in.
3. Should the window be a constant or `sync.gap_days` per collection? **Default:
   constant** `GAP_WINDOW_DAYS = 30`. The `sync` block has no unknown-key check today
   (`config.py:387-392`), so a new key needs that validation first. Add the key only
   when a user asks for it.

## 11. Implementation notes

What shipped differs from the sections above in these places.

| Topic | Shipped | Why |
|---|---|---|
| Spec target values | `checked` is 2026-10-30 for all four adapters. Claude Code 2.1.314, Codex 0.165.0, agy 1.4.10, Cursor `unrecorded` | The versions installed on the day the record types were measured on a live store |
| Type sets | `conversational` and `ignored` live on `SpecTarget` (and so on `ConversationSource.spec_target`), not as separate adapter attributes | They were measured on the same day as the date they sit beside; the guard, doctor and the tests read one object |
| Adapter version in doctor | `SpecTarget.adapter_version`, checked equal to each adapter's `version` by `test_fixture_spec_target_matches_adapter` | Doctor imports only `spec_targets.py` (4.1), so it cannot read the adapter class |
| Codex conversational types | Finer kinds: `event_msg/user_message`, `response_item/message/assistant`, `response_item/function_call`, `response_item/custom_tool_call`, counted through `JsonlStats.kind_of` | With the top-level types `event_msg` and `response_item`, 4 of 412 real rollouts (abandoned sessions with only `task_started` or context messages) were false `zero-messages` errors. With the finer kinds they are `empty` |
| What counts as rendered | Only `user` and `assistant` messages count for `zero-messages` and `empty` | agy files every unknown step type as a tool result, so counting all roles let the `antigravity/drifted` fixture pass as a list of tool turns |
| Cursor | One-bubble composers count as `single-bubble` (ignored); Cursor passes `top_level=False` | On a real store, 7 of 1,223 composers were a single bubble with no text (aborted requests) and 83 had no user text by design; both were noise, not drift |
| Unparseable document | Recorded in `export_one` at the id-collision read (`base.py:290-291`) as well as in `load_existing`; Codex and agy `_safe_target` no longer raise on such a file | On `main` the first read at `base.py:291` raised `FrontmatterParseError` before `load_existing` ran, and the Codex and agy planners raised earlier still, aborting the whole export |
| Finding clears | A clean parse clears only drift kinds; a successful write clears `unparseable-document` | Otherwise every run cleared and re-recorded a blocked document (two writes per run) |
| Heal exit code | `gap-heal:<collection>` is logged, not recorded as a failed step; the second scan is the verdict | A held drift outside the 1-day sync window makes the heal export exit 1 on every run, while the gap itself is closed |
| Doctor gap line | `ok` says `no unexplained gaps; N held by parser drift` when held ids exist, else `all exported` | "all exported" would claim held sessions were exported |
| Codex unknown payload kinds | Under `event_msg` and `response_item` the finer kind decides: a kind in neither set (or a wrapper record with no payload type, `<wrapper>/?`) is unknown, and `ignored` lists the control and context kinds measured on the live store | Taking the whole wrapper as known let a renamed payload type (`response_item/message_v2`) read as `empty` and write an empty document. Re-measured on 807 rollouts: 800 clean, 7 `empty`, no error |
| Codex fragments | The guard verdict is per logical session: one refused fragment holds every fragment of that session | Dropping only the refused file let an older healthy fragment overwrite the newer document, and a later clean fragment cleared the finding |
| agy gap clock | The agy enumerator places a conversation in the window by the index's `last_modified` when agy recorded one, else the transcript mtime: the same time the export's `--since` filters on | With the mtime alone, a transcript touched long after its last turn was reported missing while the `--since 30d` heal skipped it, so `gap-check:antigravity-sessions` failed on every sync |
| agy fixture | Invented steps with the record shapes of `AGY_STEPS`, not a copy of its text | Fixture text is invented throughout |

Measured once on 2026-10-30 on one machine, read-only: Claude Code 222 sessions and
2,438 subagents and Codex 412 rollouts (files changed in the last 60 days), all 97 agy
conversations and all 1,223 Cursor composers with headers. With the shipped rules no file was an error or a warning; 7 Codex rollouts and
7 Cursor composers were `empty` (info).
