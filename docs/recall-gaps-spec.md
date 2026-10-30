# `cairn recall gaps`: computed coverage gaps for recall and search

Status: implemented in this PR (`cairn recall gaps`, `recall list --coverage`, the
`coverage` key of `recall digest --json`, the index stamp). Section 11 lists where the
implementation differs from the sections above; where they disagree, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0 (`f0ea1e1`). Timings marked "measured" were taken once,
on a developer machine with a warm file cache, on 2026-10-30.

**Dependencies:** none. The change is based on `main` and its edits to shared files
(`recall.py`, `sync.py`, `hooks.py`, `invoke.py`) are additive. A list of held malformed
exports, or a per-source export receipt, can become one more gap kind later; this spec
does not need either. Every `cairn recall gaps` line in the docs must parse against the
real argument parser.

## 1. Purpose and non-goals

### Purpose

An empty or thin recall must never read as "that never happened" when the store or the
index is behind. Today an agent that runs `cairn recall list yesterday --json` and gets
`[]` cannot tell "no sessions" from "the codex export stopped three days ago" or "codex
is disabled". A topic search that misses cannot tell "not in the corpus" from "the 14
newest files are not indexed yet and rg is not installed".

This change adds three things:

1. `cairn recall gaps [--json] [--source S]...`: a deterministic report, with no model
   call, computed only from facts cairn already has. One line per gap, each with a
   severity, then `status: clean` or `status: degraded`.
2. A `coverage` object in the machine output of `cairn recall list --json --coverage`
   and `cairn recall digest --json`. It carries only the gaps that can hide a row from
   that command, for the queried window and sources, plus a verdict that separates a
   clean miss from a degraded miss.
3. Skill changes: a computed **Gaps** line before **One Thing** in `/recall`, degraded
   versus clean-miss wording in `/search`, and a "resolve before asking" rule.

### Non-goals

- No model-written gap text. Every gap comes from a stat, a count, a config flag or a
  `qmd` CLI line. (A comparable tool lets its synthesis model write a free-text `gaps`
  list. That list cannot be tested, so this design does not copy it.)
- No embedding state. The skills use `qmd search` (BM25) only
  (`skills/recall/SKILL.md:173`, `skills/search/SKILL.md:69-70`), so pending vectors
  cannot hide a BM25 hit. `cairn doctor` already reports embeddings
  (`src/cairn/admin.py:896-927`).
- No per-hit evidence or confidence labels on search results. Ranking quality is measured separately.
- No repair. `gaps` never exports, indexes, registers or takes a lock. It names the
  command that fixes each gap.
- No change to the default `recall list --json` shape (section 3.2).
- No per-project scoping of export lag (open question 3).

## 2. Current behaviour

| Fact | Where |
|---|---|
| `recall list --json` prints a bare JSON array of rows. | `src/cairn/recall.py:154-173` |
| Text mode prints rows, a count, and one stderr note for reduced-precision records. | `src/cairn/recall.py:174-191` |
| `recall digest --json` prints one object `{"sessions": rows, "skipped": skipped}`; exit 3 means zero sessions written. | `src/cairn/digest.py:406-418` |
| Digest sources default to `DIGEST_SOURCES = ("claude", "codex", "antigravity", "cursor")`. | `src/cairn/digest.py:54`, `src/cairn/recall.py:115` |
| `--refresh` skips a source whose collection is absent or `sync.enabled` is false, so a disabled source silently keeps its old exports. | `src/cairn/recall.py:30-35` |
| `SessionStore.records` loads the catalog of every configured session collection, enabled or not, and saves it. | `src/cairn/store.py:252-264` |
| The catalog walks every `*.md` with one `stat` each, does not follow directory symlinks, and keeps parse errors in memory only. | `src/cairn/catalog.py:81-97`, `src/cairn/catalog.py:65` |
| A cold catalog rebuild of 8,344 files took 577 ms; a warm lookup 80 to 90 ms. | `docs/codex-acceptance.md:89` |
| Exported frontmatter carries `activity_at`, the last message time, which annotation writes do not change. | `src/cairn/store.py:42-43` |
| `qmd update` runs at three sites: `cairn sync`, the hook export and `cairn invoke`. None records when it ran; sync only logs a line. | `src/cairn/sync.py:348-349`, `src/cairn/hooks.py:196-200`, `src/cairn/invoke.py:501-505` |
| QMD skips whitespace-only files, so its file count can be lower than the store's. | `src/cairn/sync.py:195-197` |
| `list_collections` parses `qmd://NAME/` tokens out of `qmd collection list`, with a 30 s timeout. | `src/cairn/qmd.py:24`, `src/cairn/qmd.py:123-143` |
| `index_status` runs `qmd status` (30 s timeout). | `src/cairn/qmd.py:202`, `src/cairn/qmd.py:277-301` |
| Doctor warns on a disabled source, a missing store, and an enabled collection with markdown that QMD has not registered. | `src/cairn/admin.py:1155-1190` |
| Doctor's rg check is `shutil.which("rg")`, warn only. | `src/cairn/admin.py:989-1002` |
| Native discovery: Claude `<store>/*/*.jsonl`; Codex `rollout-*.jsonl` under its store; Antigravity `brain/<id>/.system_generated/logs/<transcript>`; Cursor `globalStorage/state.vscdb`; Granola `cache-v<N>.json`. | `src/cairn/jsonl.py:65-76`, `src/cairn/sources/codex.py:453-457`, `src/cairn/harnesses/antigravity.py:226-238`, `src/cairn/sources/cursor.py:47`, `src/cairn/sources/granola.py:431-435` |
| Source modules read config at import and call `require_store()`, which raises for a missing store. | `src/cairn/sources/cursor.py:44-46`, `src/cairn/sources/antigravity.py:63-66` |
| The current session's id is in `CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID`. | `src/cairn/sources/claude.py:555`, `src/cairn/sources/codex.py:575` |
| `/recall` ends with One Thing; its no-result fallback suggests other terms only. | `skills/recall/SKILL.md:145-167` |
| `/search` may run only `qmd`, `rg` and `head`. | `skills/search/SKILL.md:5` |

Measured on 2026-10-30 (warm cache): a stat-only walk of 11,205 exported `.md` files took
28 ms; a walk of 2,676 Claude `.jsonl` files at all depths took 60 ms; `qmd collection
list` took 0.11 s; `qmd status` took 0.55 s. `qmd collection list` prints, per
collection, `Files:    N` and `Updated:  9m ago` (unit `s`, `m`, `h` or `d`).

## 3. Surface

### 3.1 New subcommand

```
cairn recall gaps [--json] [--source S]...
```

| Flag | Meaning |
|---|---|
| `--json` | One JSON object on stdout (section 5.1). |
| `--source S` | Repeatable. Choices: every registry source, `claude codex antigravity cursor granola service-docs` (`src/cairn/registry.py:29-51`). Without it, every configured collection is checked, including collections with no registry source (for example `journal`). |

The `gaps` parser is added outside the loop that gives other subcommands `--project` and
`--all-projects` (`src/cairn/recall.py:97-105`): gaps are per source, not per project.

Exit codes: `0` the report was produced, clean or degraded (the status is in the output,
so a skill's Bash call does not show an error). `1` `ConfigError` or `OSError`, message
`Error: <exc>` (the existing handler, `src/cairn/recall.py:193-195`). `2` argparse usage.

Text output, one line per gap, warn lines first, then info, each group sorted by kind
then collection:

```
warn  export-lag          codex-sessions: native activity at 2026-10-30T12:32Z, newest export 2026-10-30T09:10Z (3h22m behind); run cairn sync
warn  index-lag           claude-code-sessions: 14 files changed after the last qmd update (2026-10-30T11:02Z); run cairn sync
info  source-disabled     cursor-sessions: sync.enabled=false; exports stop at 2026-09-02T17:40Z
info  rg-missing          rg not on PATH: exact-match search is off
status: degraded (2 warn, 2 info)
```

With no gap the output is the single line `status: clean (checked 5 collections, qmd, rg)`.

### 3.2 Coverage on existing commands

| Command | Change |
|---|---|
| `cairn recall list ... --json --coverage` | New flag `--coverage`. With `--json` the output becomes `{"sessions": [rows], "coverage": {...}}`. Without `--coverage`, `--json` still prints the bare array, because tests and scripts parse it (`tests/test_recall.py:127`, `tests/test_shared_store.py:309-330`). `--coverage` without `--json` is accepted and changes nothing beyond the text-mode note below. |
| `cairn recall list` (text) | When coverage is degraded, one stderr line after the existing notes: `Note: coverage degraded: <first warn message> (+N more; run cairn recall gaps)`. |
| `cairn recall digest --json` | Adds a `coverage` key to the existing object. Additive: `sessions` and `skipped` keep their shape and the exit codes do not change. |
| `cairn recall digest` (text) | The same stderr note as `list`. |

### 3.3 New state file

`$XDG_STATE_HOME/cairn/qmd-index.json` (default `~/.local/state/cairn/qmd-index.json`,
through `state_home()`, `src/cairn/config.py:825-829`): the index stamp, written after
each successful `qmd update` (section 5.3). No config key is added; the
two thresholds are module constants (open question 1).

| Constant | Value | Use |
|---|---|---|
| `EXPORT_GRACE_S` | `3600` | Export lag below this is `info`, above it `warn`. One hour matches the default hourly cron sync. |
| `QMD_PROBE_TIMEOUT_S` | `5` | Timeout for the single `qmd collection list` call. |

## 4. Behaviour

### 4.1 Gathering facts (`gather`)

`src/cairn/recall_gaps.py` (new) builds a `Facts` value in this order. Nothing in it writes,
except the catalog cache save that `recall list` already does.

1. **Config.** For each configured collection, in name order: `enabled`
   (`SyncPolicy.enabled`, `src/cairn/config.py:121-134`), `store`, and the registry
   source, if any. `--source` filters to the named registry sources.
2. **Exports.** For a collection whose registry source has a `session_type`, load the
   catalog (`load_catalog`, then `save`, as `src/cairn/store.py:259-263` does). Record
   `exported_docs` (session-type documents), `newest_activity_at` (the largest
   `activity_at`, else `started_at`, over session-type entries), `last_export_at` (the
   largest entry `mtime_ns`), and `unreadable` (`len(catalog.errors)`). For any other
   collection (service docs, journal), do a stat-only walk with the new
   `catalog.scan(output_dir)`, extracted unchanged from `Catalog._walk`
   (`src/cairn/catalog.py:81-97`), so no frontmatter is parsed.
3. **Native newest.** For an enabled collection with a store, apply its probe from the
   table below and record `native_newest_at` and `native_basis`. Probes use `os.scandir`
   and `stat` only. They never import a source module, because those read config and
   raise at import (section 2). A native path that contains a current session id
   (`CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID`; both are set when one harness runs
   the other as a peer, and both sessions are live) is skipped, so the live session
   that runs the skill never reports itself as lag.

   | Source | Probe | Basis |
   |---|---|---|
   | claude | `<store>/*/*.jsonl`, size > 0 | `file` |
   | codex | `<store>/**/rollout-*.jsonl`, size > 0 | `file` |
   | antigravity | `conversation_transcript(store, id)` for each `brain/<id>` (import-safe helper) | `file` |
   | cursor | `max(mtime)` of `globalStorage/state.vscdb` and `state.vscdb-wal` | `store` |
   | granola | newest `cache-v*.json` | `store` |
   | service-docs | newest `**/*.md` under each `service_sources` path | `file` |
   | other | none | `none` |

   `store` basis means the mtime changes without a new session (a database or cache
   rewrite), so lag measured on it is never above `info`.
4. **Index.** If `shutil.which(qmd_binary)` (or the path is a file) finds QMD, run
   `qmd collection list` once with `QMD_PROBE_TIMEOUT_S` and parse each block with the
   new `qmd.parse_collection_list` into `{name: (files, updated_age_s)}`. `qmd status` is
   not called (0.55 s measured). Read the stamp file (section 5.3). A stamp is used only
   if its `database` equals the current `qmd_resources()[1]`
   (`src/cairn/runtime.py:19-27`) and the collection is in its `collections`. Then
   `unindexed_files` = entries whose `mtime_ns` > `started_ns`, with basis `cairn`.
   Without a usable stamp, use the collection's `updated_age_s`: count entries with
   `mtime` > `now - updated_age_s`, basis `qmd-relative`. That count is a lower bound,
   because the unit is coarse. It is a lower bound whether QMD's `Updated` means the last
   scan or the newest indexed file.
5. **rg.** `shutil.which("rg")`, as `src/cairn/admin.py:994`.

### 4.2 Classifying gaps (`classify`, pure)

`classify(facts, now, *, window=None, requested=None) -> list[Gap]`. Same input, same
output: no clock read, no I/O. Gap kinds are a fixed vocabulary:

| Kind | Scope | Severity | Condition |
|---|---|---|---|
| `export-lag` | store | warn if lag > `EXPORT_GRACE_S` and basis `file`, else info | `native_newest_at > newest_activity_at`; lag = the difference (section 11: Claude, Codex and Antigravity compare each native file with its own export) |
| `never-exported` | store | warn | enabled, native files exist, `exported_docs == 0` |
| `unreadable-exports` | store | warn | `unreadable > 0`, or a digest skipped a session in the window (section 11) |
| `source-disabled` | store | warn if the source was named with `--source`, else info | `enabled` is false |
| `store-missing` | store | info | enabled, store configured, store path absent |
| `collection-missing` | index | warn | enabled, markdown on disk, name absent from `qmd collection list` (doctor's rule, `src/cairn/admin.py:1178-1190`) |
| `index-lag` | index | warn | `unindexed_files > 0` |
| `index-count` | index | warn if store count > QMD `Files`, else info | the counts differ |
| `index-stamp-missing` | index | info | no usable stamp, so `index-lag` used basis `qmd-relative` |
| `index-unknown` | index | warn | QMD not found, timed out, exited non-zero, or printed no parseable block |
| `rg-missing` | index | info | rg not on PATH |

`status` is `degraded` if any gap has severity `warn`, else `clean`.

### 4.3 Window filter (coverage only)

`coverage(config, sources, window, rows)` calls `gather` without the index step (no QMD
call) and `classify` with the window and the requested sources, then keeps only gaps of
scope `store`. `recall list` and `digest` read the store, not the index
(`skills/recall/SKILL.md:171`), so index gaps cannot hide one of their rows. A store gap
applies to window `[start, end)` as follows:

- `export-lag`: the unexported interval `(newest_activity_at, native_newest_at]`
  intersects the window.
- `never-exported`: `native_newest_at >= start`.
- `unreadable-exports`, `source-disabled`, `store-missing`: always, because their dates
  are unknown.

Requested sources are the `--source` values, else every session source for `list`, else
`DIGEST_SOURCES` for `digest`. `verdict` is `found` when `rows` is non-empty,
`clean-miss` when it is empty and the status is clean, and `degraded-miss` when it is
empty and degraded.

Coverage is computed after `--refresh`, so it describes the store the rows came from.

## 5. Data formats

### 5.1 `cairn recall gaps --json`

```json
{
  "schema_version": 1,
  "status": "degraded",
  "checked_at": "2026-10-30T13:50:02Z",
  "gaps": [
    {
      "kind": "export-lag",
      "severity": "warn",
      "scope": "store",
      "collection": "codex-sessions",
      "source": "codex",
      "message": "native activity at 2026-10-30T12:32Z, newest export 2026-10-30T09:10Z (3h22m behind)",
      "fix": "cairn sync",
      "facts": {"lag_s": 12120, "basis": "file"}
    }
  ],
  "collections": [
    {
      "collection": "codex-sessions",
      "source": "codex",
      "enabled": true,
      "store": "~/.codex/sessions",
      "store_exists": true,
      "exported_docs": 798,
      "unreadable": 0,
      "last_export_at": "2026-10-30T09:11:40Z",
      "newest_activity_at": "2026-10-30T09:10:05Z",
      "native_newest_at": "2026-10-30T12:32:18Z",
      "native_basis": "file",
      "index": {"registered": true, "files": 798, "unindexed_files": 0, "basis": "cairn"}
    }
  ],
  "index": {"qmd": "available", "stamp_at": "2026-10-30T11:02:00Z", "stamp_basis": "cairn"},
  "rg": {"available": true}
}
```

`store` is printed as configured, after expansion. `index.qmd` is `available`,
`missing` or `failed`. A collection with no index data has `"index": null`. Timestamps
are UTC ISO-8601, seconds precision. `fix` is a command string or `null`.

### 5.2 `coverage`

```json
"coverage": {
  "status": "degraded",
  "verdict": "degraded-miss",
  "scope": "store",
  "window": {"start": "2026-10-29T00:00:00Z", "end": "2026-10-30T00:00:00Z"},
  "sources": ["claude", "codex"],
  "gaps": [ /* the gap objects of 5.1, store scope only */ ]
}
```

### 5.3 Index stamp `qmd-index.json`

```json
{"version": 1, "database": "~/.cache/qmd/index.sqlite", "started_ns": 1793361720000000000,
 "started_at": "2026-10-30T11:02:00Z", "collections": ["claude-code-sessions", "codex-sessions"]}
```

The new `qmd.record_update(started_ns, collections)` writes it with `atomic_write`
(`src/cairn/storage.py`) while the caller still holds `locked_index()`, and only after
`qmd update` exits 0. `started_ns` is read before `qmd update` starts, so a file written
during the update counts as unindexed: the stamp errs towards reporting a gap. `database`
is the resolved path from `qmd_resources()`; the example shows `~` for brevity, real
files hold the absolute path. `collections` is the `known` set the caller already
fetched, plus any collection it added. The three call sites are `_index_stages`
(`src/cairn/sync.py:348`), `hooks._update_index` (`src/cairn/hooks.py:196`) and
`invoke.update_index` (`src/cairn/invoke.py:501`). A failed stamp write prints
`Warning: could not record the qmd update time: <exc>` on stderr and never fails the
caller.

## 6. Failure modes

| Case | Result |
|---|---|
| QMD not installed | `index-unknown` warn, `qmd not found: topic search cannot run; temporal recall still works`. Store gaps are still computed. `coverage` never needs QMD. |
| `qmd collection list` times out (5 s), exits non-zero, or prints no `qmd://` block | `index-unknown` warn with the reason. No `index-lag` or `collection-missing` is claimed from missing data. |
| Stamp missing, corrupt, wrong `version`, or for another database | Treated as absent. `index-stamp-missing` info; `index-lag` falls back to `qmd-relative`. A `started_ns` that is not positive or not before year 10000 (`qmd.MAX_STAMP_NS`) is corrupt. |
| Native store unreadable (`PermissionError`) | `store-missing` info with the error text. A permission or other error on a folder or file inside the store gives `store-missing` info `native store read incompletely: <first error> (+N more)`; the facts read from the rest are kept. A file that vanishes during the walk is ignored. |
| Native mtime in the future (clock skew) | Lag is clamped to `now`; a skew over 60 s adds `(clock skew)` to the message. |
| Sync running while `gaps` runs | `gaps` takes no lock, so a transient `index-lag` can appear; the next run clears it. Stated in the help text. |
| A note written after the last update | Counted in `unindexed_files`. That is true: the note text is not searchable until the next `qmd update`. |
| A concurrent live session in another terminal | Shows as `export-lag` until it is exported. Only the current sessions (section 4.1 step 3) are excluded. |
| Cold catalog cache | The first run rebuilds it (577 ms for 8,344 files, `docs/codex-acceptance.md:89`). The same rebuild would happen on the next `recall list`. |

**Speed.** Warm, on 10,000 exported files: catalog load about 90 ms
(`docs/codex-acceptance.md:89`), stat-only walks under 30 ms per 10,000 files, native
probes about 60 ms for 2,676 Claude files, one `qmd collection list` 0.11 s: under 0.4 s
in total. The design stays under 1 s because it uses one QMD call (not `qmd status`), no
frontmatter parse for non-session collections, no file reads for native probes, and a
5 s timeout as the upper bound when QMD hangs.

## 7. Tests

New `tests/test_recall_gaps.py` (uses `StoreFixture`, which already sets
`XDG_STATE_HOME` to a temp dir, `tests/test_shared_store.py:73`):

- `TestClassify.test_clean_facts_give_clean_status`: no gaps, `status == "clean"`.
- `TestClassify.test_export_lag_over_grace_is_warn_and_under_is_info`.
- `TestClassify.test_store_basis_lag_is_never_warn`: cursor and granola.
- `TestClassify.test_disabled_source_is_warn_only_when_requested`.
- `TestClassify.test_never_exported_and_unreadable_are_warn`.
- `TestClassify.test_window_filter_keeps_only_intersecting_lag`.
- `TestClassify.test_verdict_found_clean_miss_degraded_miss`.
- `TestClassify.test_is_deterministic`: same facts and `now`, byte-equal JSON twice.
- `TestGather.test_current_session_file_is_excluded`: `CLAUDE_CODE_SESSION_ID` set.
- `TestGather.test_non_session_collection_is_not_parsed`: `Catalog.stats["parsed"]` stays 0 for service docs.
- `TestGather.test_stamp_for_other_database_is_ignored`.
- `TestGather.test_unindexed_count_uses_stamp_started_ns`.
- `TestGather.test_qmd_relative_fallback_is_a_lower_bound`.
- `TestCli.test_gaps_text_lists_warn_before_info_and_ends_with_status`.
- `TestCli.test_gaps_json_shape_and_schema_version`.
- `TestCli.test_qmd_missing_gives_index_unknown_and_exit_0`.
- `TestCli.test_qmd_timeout_gives_index_unknown`: stub sleeps past a patched timeout.
- `TestCli.test_source_filter_rejects_unknown_source`: exit 2.
- `TestCoverage.test_list_json_without_flag_is_still_an_array`.
- `TestCoverage.test_list_json_coverage_wraps_sessions`.
- `TestCoverage.test_coverage_never_calls_qmd`: stub logs calls; log stays empty.
- `TestCoverage.test_digest_json_has_coverage_and_exit_codes_unchanged`: exit 3 with `degraded-miss`.
- `TestCoverage.test_text_mode_prints_one_stderr_note_when_degraded`.

Existing modules:

- `tests/test_qmd_health.py::TestParseCollectionList.test_files_and_relative_updated`, `test_unknown_unit_is_none`, `test_block_without_files_line`.
- `tests/test_qmd_collections.py::test_list_collections_unchanged_after_refactor`.
- `tests/test_sync.py::TestIndexStamp.test_stamp_written_after_successful_update`, `test_no_stamp_after_failed_update`.
- `tests/test_hooks.py::test_update_index_writes_stamp`.
- `tests/test_invoke.py::test_update_index_writes_stamp`.
- `tests/test_catalog.py::test_scan_matches_walk`.

## 8. Docs to update

- `docs/recall-gaps-spec.md`: this spec, in the style of `docs/recall-digest-spec.md`.
- `docs/recall-digest-spec.md` section 8.4: one sentence on the added `coverage` key.
- `skills/recall/SKILL.md`:
  - Step 2: use `cairn recall list "DATE" --json --coverage`.
  - Step 3: run `cairn recall gaps --json` once, in parallel with the searches.
  - New Step 4 "Gaps", before One Thing (which becomes Step 5). Print
    `**Gaps:** <warn messages joined by "; ">`, then the info messages if there is
    room. If the status is clean, print `**Gaps:** none found (exports, index, rg checked)`.
    Build the line from the JSON only; never add a gap the JSON does not contain.
  - "Fallback: no results" splits in two. Clean miss: `No sessions or notes match "QUERY" in WINDOW; exports and the index are current.`
    Degraded miss: `No results for "QUERY", but coverage is degraded: <gaps>. This is not proof it did not happen. Run <fix> and recall again.`
  - New section "Resolve before asking": before asking the user about past work or a
    past decision, run the temporal recall for the likely window and the topic recall
    for its key terms. Then ask with a hypothesis: "I found X on DATE (REF); is that the
    decision you mean?" Ask an open question only if both recalls return a clean miss,
    and say so. A degraded miss is reported with its gaps, not as "I have no record".
- `skills/search/SKILL.md`: add `Bash(cairn recall gaps:*)` to `allowed-tools` (line 5).
  In step 5, if a search returns zero hits or fewer than half of `-n`, run
  `cairn recall gaps --json` once. Report a degraded result as
  `No match for "QUERY", but the index is behind: <gaps>. rg can see unindexed files: /search QUERY --exact.`
  Report a clean miss as `No match for "QUERY" in the indexed collections.`
- `skills/cairn/SKILL.md:91`: add `cairn recall gaps [--json]` to the command list.
- `src/cairn/__main__.py:57-62`: one help line, `cairn recall gaps --json   what recall cannot see right now`.
- `README.md` recall block (`README.md:476-482`): one `cairn recall gaps` line and two
  sentences on `coverage`.

## 9. Implementation plan

1. `catalog.py`: extract `scan(output_dir) -> dict[str, os.stat_result]` from `_walk`;
   `_walk` calls it. About 15 lines.
2. `qmd.py`: `parse_collection_list(text)`, `collection_details(qmd_binary, timeout)`,
   `list_collections` reimplemented on top with the same errors; `STAMP_VERSION`,
   `stamp_path()`, `record_update()`, `read_stamp()`. About 90 lines.
3. `sync.py`, `hooks.py`, `invoke.py`: take `time.time_ns()` before `qmd update`, call
   `record_update` after exit 0, inside the lock. About 6 lines each.
4. `recall_gaps.py` (new): `Facts`, `Gap`, native probe table, `gather`, `classify`,
   `coverage`, `render_text`, `to_json`. About 300 lines.
5. `recall.py`: `gaps` subparser and handler, `--coverage` on `list`, coverage passed
   to `digest.run_digest` as a keyword argument, stderr note. About 50 lines;
   `digest.py` about 10 lines.
6. Tests from section 7. About 450 lines.
7. Skills, README, help text, spec doc (section 8). Run `make check`.

Estimate: about 1,000 lines including tests. One day of work if the fixtures hold; add
half a day if the stamp write breaks existing sync tests that assert the state dir is
empty.

## 10. Open questions (each resolved by its recommended default)

1. **Thresholds as config?** `EXPORT_GRACE_S` could become `recall.gaps.export_grace`
   in `cairn.config.json`, which needs `config.py` parsing and an example entry.
   Recommended default: keep the constant for now, and add the key only when a user
   with a non-hourly cron needs it.
2. **`recall list --json` shape.** Wrapping the array in an object always is cleaner, but
   it breaks about 15 test assertions and any external script. Recommended default:
   the opt-in `--coverage` flag. Make the object the default at the next major version.
3. **Per-project export lag.** A live session in another repository shows as `export-lag`
   on a project-scoped `recall list`. Claude native files sit in per-project folders, so
   a project filter is possible for Claude but not for the other sources. Recommended
   default: per source only. Rely on the one-hour grace and on excluding the current
   session, and revisit if users report noise.

## 11. Implementation notes

Where the code differs from the sections above:

- **Claude, Codex and Antigravity compare each native file with its own export.** The
  probe maps a native file to its native id (the Claude file stem, the Codex rollout's
  thread uuid, the Antigravity `brain/<id>` folder). A file is covered when the export
  document with that id has an mtime at least as new as the file: an export rewrites its
  document whenever the native `source_revision` (path, mtime, size) changes, so a
  touched file is cleared by `cairn sync`. A file with no export counts only when it is
  newer than `newest_activity_at`, because an older one is outside the export window or
  one the exporter skips (no messages). `export-lag` lists the uncovered files
  (`facts.sessions`); the lag is the widest unexported interval: from the export's
  `activity_at` to the file's mtime, or from the mtime to now when the file has no
  export. Its severity is `warn` past `EXPORT_GRACE_S`. Another session's export, a
  peer exchange or a note on another session no longer hides the lag; a note on the
  same session still does until the next export.
- **Store-basis sources (Cursor, Granola) compare with the collection.** `export-lag` is
  raised only when `native_newest_at` is also later than `last_export_at`, the newest
  export file's mtime, because the store mtime moves without a new session. Its
  severity is never above `info`.
- **Unreadable exports use recall's record check.** `unreadable` counts the catalog's
  parse errors plus every session-type document that `store.record_from_frontmatter`
  rejects (a `source` that disagrees with the collection, no native id). Those are left
  out of `exported_docs`. The gap is checked before the `source-disabled` return,
  because recall reads a disabled collection's exports too.
- **Skipped digest sessions.** `run_digest` passes its `skipped` rows to the coverage
  callable. Each source with a skipped session adds an `unreadable-exports` warn with
  `facts.count` and `facts.skipped` (the refs), and `fix` `cairn <source> export --all`
  when one was a legacy export. A day whose only sessions were skipped is a
  `degraded-miss`, not a `clean-miss`.
- **Repair commands.** `never-exported` names `cairn <source> export --all` for a
  windowed source and `cairn <source> export` for one without `--all` (service docs).
- **Non-session collections** (service docs, journal) compare the native newest file with
  `last_export_at`, because they have no `activity_at`.
- **`index-count`** is reported only for a collection with no `index-lag`, so one set of
  unindexed files is not reported twice. The store count is every `*.md` file
  (`markdown_files` in the JSON), because QMD indexes peer exchanges and subagent
  transcripts too.
- **`index-stamp-missing`** is one global `info` gap that names the collections whose
  `index-lag` used the `qmd-relative` basis. Collections without markdown are left out.
- **`qmd-relative` rounding.** The cutoff is the printed age less half of its unit
  (`1h ago` counts files changed in the last 30 minutes). That is a lower bound whether
  QMD floors or rounds the age. The `w` unit is read as well as `s`, `m`, `h` and `d`.
- **Index stamp collections.** `cairn sync` records the enabled collections that hold
  markdown (the set its registration step ensures). The hook and `cairn invoke` record the
  `qmd collection list` set they fetched plus the collections they registered. Each call
  site reads `time.time_ns()` before `qmd update` and calls `qmd.record_update` after it
  exits 0: two lines per site.
- **JSON additions.** Each `collections[]` row also carries `markdown_files`, and
  `index` carries `stamp_basis`. `rg.available` is `null` in facts gathered for coverage,
  which does not check rg.
- **Text note.** `+N more` counts every other gap in the coverage, warn or info.
- **Digest coverage** is passed to `run_digest` as a callable that takes "was any session
  written" and the skipped sessions, so the verdict uses the digest's own result.
