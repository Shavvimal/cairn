# Recall qrels: a fixed corpus and relevance file for cairn's deterministic recall paths

Status: implemented in this PR (branch `shav/mem-13-recall-qrels`): the invented corpus,
the relevance file, `tests/test_recall_qrels.py`, the `_utcnow()` clock seam, `-e` in the
skills' rg lines and ripgrep in CI. Section 11 lists where the implementation differs
from the sections above; where they disagree, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0 (`f0ea1e1`).

**Dependencies:** none for behaviour. The test reads the bare JSON array that
`cairn recall list --json` prints without `--coverage`; the coverage object added by the
recall-gaps change only appears with `--coverage`, so the test does not see it. The test
builds the corpus with the exporters as they are today, including the `provenance`,
`exporter_version` and `exporter_spec_target` keys.

## 1. Purpose and non-goals

### Purpose

cairn has unit tests but had no test of retrieval quality. Three retrieval paths in cairn
are deterministic and can run without QMD:

1. The temporal listing `cairn recall list DATE` (recall.py:110-192). It does the window
   match, the project filter, the subagent and `--min-msgs` filters, and the
   `started_at` sort.
2. The project filter `SessionRecord.matches_project` (store.py:131-137).
3. The exact-match ripgrep pass that the `/search` and `/recall` skills run
   (skills/search/SKILL.md:89, skills/recall/SKILL.md:122).

This change adds an invented exported store of 33 files, a relevance file ("qrels") of 36
queries in three families, and a hermetic unittest. The test runs the real code paths
over the store, computes recall@5 and expected-top-1 per family, and asserts fixed floors.
When a retrieval bug is fixed, the fix adds one row whose `note` names the symptom. After
that, the bug cannot come back silently.

A comparable tool keeps a committed qrels file and a keyless CI test with floors that a
person sets by hand from the observed rate, and only tightens. This change copies that
discipline.

### Non-goals

- **Topic ranking.** cairn has no deterministic ranker for titles or free text. Topic
  search is `qmd search` (BM25) (skills/recall/SKILL.md:96-105), and qmd is not in CI.
  So the families `title-in-question` and `long-request` are out. A full question or a
  long request never occurs as a literal string, so an `rg -F` pass returns nothing, and
  rows of those families would measure nothing. The loader rejects any family outside
  the three (section 3), so nobody adds rows that never run.
- **Capture and replay** of real recall queries. Out for this change; section 4.5 gives
  the reasons.
- No new CLI command, no config key, no frontmatter key, no runtime dependency.
- No change to how `recall list` selects or sorts sessions. This change measures the
  current behaviour. It does not tune it.

## 2. Current behaviour (main, 2.9.0)

| Fact | Where |
|---|---|
| `recall list` parses the date expression with `parse_date_range`, then filters by project, subagent, window and `--min-msgs`, then sorts by `started_at` | recall.py:117, 125-129, 143-153 |
| Without `--project` or `--all-projects`, `list` filters to `os.getcwd()`, so a test that omits both depends on the directory it runs in | recall.py:125-127 |
| `parse_date_range` read the clock with `datetime.now(UTC)`, and no parameter injected a fixed time | timeutil.py:68 |
| Relative expressions (`yesterday`, `last week`, `last friday`, `N days ago`, `last N days`, `this week`) all derive from that clock | timeutil.py:71-101 |
| A record's activity is its `activity_dates` days. A file with no `activity_dates` falls back to the endpoints, plus `date` for pre-schema files | store.py:145-163, 190-203 |
| A file with `type` outside the source's session types (for example `agent-exchange`) is not a record | store.py:182-184, exchange.py:49 |
| A subagent has `parent_session_id` and is hidden from `list` unless `--include-subagents` is given | store.py:123-126, recall.py:143-144 |
| A record with `user_messages is None` passes `--min-msgs` | recall.py:151 |
| `matches_project` accepts the `project` label, the `repo` label, or a path that resolves to `cwd` | store.py:131-137 |
| Session file names are `YYYY-MM-DD-<short>.md` under `<project>/<repo>/`. The short id is 8 characters for Claude and Cursor, and head8+tail4 for Codex | paths.py:14-26, sources/base.py:65-67, sources/codex.py:578-583 |
| The rg list pass was `rg -l -i -F "QUERY" --type md --sortr modified PATHS \| head -n 20` in `/search` and `head -n 10` in `/recall` | skills/search/SKILL.md:89, skills/recall/SKILL.md:122 |
| rg ranks by file mtime, newest first. The skill states that an export file changes only when its session changes | skills/search/SKILL.md:94-95 |
| **Bug:** a query that starts with `-` was parsed as an rg flag. `rg -l -i -F "--sortr modified" ...` exits 2 with `rg: unrecognized flag --sortr modified` (checked with ripgrep 15.1.0) | skills/search/SKILL.md:89, skills/recall/SKILL.md:122 |
| CI ran `unittest discover` on ubuntu-latest and installed no rg | .github/workflows/ci.yml:17-31 |

## 3. Surface

### Files added

| Path | Content |
|---|---|
| `tests/fixtures/recall-corpus/` | 33 committed exported files in three collections (section 5.1) |
| `tests/fixtures/recall-corpus.spec.json` | The compact corpus description that the builder reads |
| `tests/recall_corpus.py` | Builder: `uv run python -m tests.recall_corpus --write` rewrites the corpus from the spec |
| `tests/fixtures/recall-qrels.json` | 36 queries (section 5.2) |
| `tests/test_recall_qrels.py` | The test (section 7) |
| `docs/recall-qrels-spec.md` | This document |

### Code and docs changed

- `src/cairn/timeutil.py`: a new `_utcnow() -> datetime` returns `datetime.now(UTC)`.
  `parse_date_range` calls it in place of `datetime.now(UTC)`. The test patches
  `cairn.timeutil._utcnow`. Nothing else changes.
- `skills/search/SKILL.md` (both rg passes) and `skills/recall/SKILL.md` (the list pass):
  `-e` goes directly before the query (`rg -l -i -F -e "QUERY" ...`). `-e` keeps a query
  that starts with `-` from being read as a flag.
- `.github/workflows/ci.yml`: one step installs ripgrep before `unittest`, so the `exact`
  and `title-bare` families run on every PR.

### Families and floors (constant `FLOORS` in the test module)

| family | engine | metrics gated | floor |
|---|---|---|---|
| `temporal` | `recall.main(["list", ...])` in process | `exact` (ordered list equal), `recall@5`, `top1` | 1.0, 1.0, 1.0 |
| `exact` | the skill's rg list pass | `recall@5`, `top1` | 1.0, 1.0 |
| `title-bare` | the same rg pass, with the bare session title as the literal | `recall@5`, `top1` | 1.0, 0.77 |

The `title-bare` top-1 floor is the observed rate, 7 of 9 rows. In two rows (b01, b07) a
later session quotes an older session's title. rg ranks by mtime, so the later session
comes first. These two misses are deliberate. They record a known weakness of the mtime
order. If a later change adds a title-aware order, it raises this floor to 1.0. A floor
is only raised, never lowered, except by a commit whose message says why.

### Exact loader messages (raised as `AssertionError` from `load_qrels`)

- `recall-qrels.json: schema_version must be 1, got {v!r}`
- `recall-qrels.json: row {id}: unknown family {family!r}; use temporal|exact|title-bare`
- `recall-qrels.json: row {id}: {path} is not in tests/fixtures/recall-corpus`
- `recall-qrels.json: row {id}: expected_top1 must be relevant[0] or in relevant`
- `recall-qrels.json: row {id}: a temporal row needs --all-projects or --project in args`
- `recall-qrels.json: duplicate id {id!r}`
- `recall-qrels.json: row {id}: {path} is listed twice in relevant`
- `recall-qrels.json: row {id}: a temporal row's expected_top1 must be relevant[0]`
- `recall-qrels.json: row {id}: args is for temporal rows only`

### Skip message

`rg not on PATH; the exact and title-bare families need ripgrep` (through `skipTest`, so
it shows in the unittest output). The `temporal` family still runs.

### Exit codes

None are new. The test passes or fails under `unittest`.

## 4. Behaviour

### 4.1 Building the temp store

1. `setUpClass` creates a `TemporaryDirectory`. It sets `HOME`, `XDG_CACHE_HOME`,
   `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `CAIRN_CONFIG` inside it, drops the host
   identity hints as `StoreFixture` does (tests/test_shared_store.py), drops
   `RIPGREP_CONFIG_PATH`, and clears `config.get_config`. It registers the directory
   removal, the environment restore and the cache clear as class cleanups at once, so
   they run even when a later setup step fails (unittest skips `tearDownClass` then).
2. It copies the three collection folders with `shutil.copytree` to `<tmp>/data/`.
3. It writes a config with `data_root=<tmp>/data` and the collections
   `claude-code-sessions`, `codex-sessions` and `cursor-sessions`. Each collection gets
   an empty `store` folder under `<tmp>`, because no export runs.
4. It sets every `.md` file's mtime to the file's `activity_at` (for the pre-schema file
   and the exchange document, `last_activity`), plus one second per file in
   (activity, path) order. This keeps all mtimes distinct, so the rg order is total, and
   never puts an older session after a newer one, however close their activity times. It models the
   contract in skills/search/SKILL.md: the file changes when its session does.
5. It writes `claude-code-sessions/decoy-not-markdown.txt`, which holds every `exact`
   and `title-bare` query, with an mtime 60 seconds after the newest `.md` file. With
   `--type md` the rg pass never lists it. Without `--type md` it takes top-1 on every
   rg row.

### 4.2 Temporal family

For each row, the test patches `cairn.timeutil._utcnow` to return `anchor_now` from the
qrels file (`2026-03-16T10:00:00Z`, a Monday). It runs
`recall.main(["list", query, *args, "--json"])` with stdout redirected and reads each
row's `path`, relative to `<tmp>/data`. The list keeps its order. The test compares it
with `relevant`, which is ordered by `started_at` as recall.py sorts. The command-line
equivalent of row t10 is:

```bash
cairn recall list last week --project harbour --json
```

### 4.3 Exact and title-bare families

1. `skill_rg_argv()` reads the one `rg -l` line from skills/search/SKILL.md and from
   skills/recall/SKILL.md. `test_skill_rg_lines_agree` asserts that the two lines agree
   when the placeholder name (`QUERY`/`TERM`) is ignored, and that the `head -n` caps
   are 20 and 10. The part before `|` is split with `shlex`. So the test runs the flags
   that the skills print. A skill edit that drops `-F`, `-i`, `-e` or `--type md`
   changes what the test runs.
2. The test replaces the placeholder with the row's query and the `"PATH1" "PATH2" ...`
   tail with the three collection directories, sorted. It runs `rg` with a 10-second
   timeout. Exit 1 (no match) gives an empty list. Any other non-zero exit scores the
   row 0 and records the first line of stderr in the table.
3. It keeps the first 20 lines (the `/search` cap) and makes them relative.
4. The ranking is mtime order. So `expected_top1` for `exact` is the newest file that
   holds the literal. That is the order the skill promises.

### 4.4 Scoring

For each row, with `R` = `relevant` and `G` = the returned list:

- `recall@5` = |G[:5] ∩ R| / min(|R|, 5). If R is empty, the value is 1.0 when G is
  empty and 0.0 otherwise.
- `top1` = 1 if G[0] == `expected_top1`. If `expected_top1` is null, the value is 1 when
  G is empty.
- `exact` (temporal only) = 1 if G == R as ordered lists.

The family value is the mean over the family's rows. Each family is compared with its
floors. There are two floor tests: `TemporalQrelsTest.test_floors` for `temporal` and
`ExactQrelsTest.test_floors` for `exact` and `title-bare` (the second skips without rg).
If a floor fails, that test's one `self.fail` prints a table of its families
(section 5.3), with every miss. A run shows every family, in at most two tables.
A row whose rg run failed (any exit other than 0 and 1) scores 0 on every metric, also
when its gold list is empty, and its misses line prints the rg error. A per-row `subTest` is not used, because the
floor is a property of the family, not of a single row.

### 4.5 Capture and replay: out, with reasons

1. The topic path is not cairn code. The skills call `qmd search` and `rg` directly.
   cairn would see a query only if the agent ran a new wrapper command or wrote a log
   row by hand. Neither is reliable today.
2. The deterministic paths are already covered by the qrels, which have true gold.
   Replay would measure stability, not correctness, and only for the QMD path, which CI
   cannot run.
3. Capture adds a store of real queries on disk. A query can hold a private name or a
   secret pasted into a prompt. That is a new privacy surface, and this change does not
   need it.

A later change that adds a search wrapper (a thin command over qmd and rg) can add
capture with these rules: off by default; a `capture on|off` subcommand writes
`{"schema_version":1,"ts","query","family"?,"engine","returned_paths"}` rows to
`$XDG_STATE_HOME/cairn/recall-log.ndjson`; the file stays local and is never exported or
synced; turning capture off with a purge flag deletes it; a replay command reports mean
Jaccard@5 and top-1 stability between the logged paths and the current paths.

## 5. Data formats

### 5.1 Corpus

The corpus holds 33 files: 13 Claude sessions (one is a subagent and one is pre-schema),
one `agent-exchange` document, 10 Codex sessions (one is a subagent), and 9 Cursor
sessions. There are 4 projects: `harbour` (repos `api`, `web`), `lantern`, `quarry` and
`fieldnotes`. The activity runs over 14 UTC days, from 2026-03-02 (Mon) to 2026-03-15
(Sun). Every `cwd` is under `/tmp/corpus/`. All names, ids and errors are invented.

| key | source, type | project/repo | start (UTC) | activity days | user msgs | title | literals in body |
|---|---|---|---|---|---|---|---|
| C01 | claude | harbour/api | 03-02 09:10 | 02 | 4 | Fix ECONNRESET in webhook retry loop | `ECONNRESET`, `retry_with_backoff` |
| C02 | claude | harbour/api | 03-03 14:00 | 03 | 5 | Add idempotency keys to payment intents | `IdempotencyKeyConflict` |
| C03 | claude | harbour/web | 03-04 10:00 | 04 | 3 | Migrate checkout form to server actions | the hydration error string |
| C04 | claude | harbour/api | 03-05 09:00 | 05, 09 | 6 | Investigate slow invoice PDF export | `src/harbour/billing/invoice_pdf.py`, `render_invoice_pdf` |
| C05 | claude | lantern | 03-06 16:20 | 06 | 4 | Rewrite frontmatter parser for block lists | `FrontmatterParseError: unterminated block list`, `parse_block_list` |
| C06 | claude-subagent of C05 | lantern | 03-06 17:05 | 06 | 1 | Find every caller of parse_block_list | `parse_block_list` |
| C07 | claude | quarry | 03-08 11:00 | 08 | 3 | Tune BM25 weights for the docs index | `bm25_k1` |
| C08 | claude | harbour/api | 03-10 23:50 | 10, 11 | 3 | Rotate the staging database credentials | `rotate_credentials.sh` |
| C09 | claude | fieldnotes | 03-11 08:00 | 11 | 2 | Short question about rg flags | `--sortr modified` |
| C10 | claude | harbour/web | 03-12 15:00 | 12 | 3 | Follow-up: webhook retries after the ECONNRESET fix | `ECONNRESET`; quotes C01's title |
| C11 | claude | lantern | 03-13 09:30 | 13 | 4 | Release checklist for 2.4.0 | `make check` |
| C12 | claude, pre-schema (no `cairn_schema`, no `activity_dates`; `date` 03-04, `last_activity` 03-08) | quarry | none (falls back to 03-04) | 04, 08 (endpoints) | none | Legacy: seed the quarry fixtures | `seed_fixtures.py` |
| C13 | claude | harbour/api | 03-15 18:00 | 15 | 5 | Plan the ledger reconciliation job | `LedgerMismatch`, `reconcile_ledger` |
| X01 | agent-exchange in C13's session folder | harbour/api | 03-15 18:40 | none | none | (derived from the prompt) | `LedgerMismatch` |
| K01 | codex | harbour/api | 03-02 11:30 | 02 | 3 | Review the webhook retry patch | `econnreset` (lower case), `retry_with_backoff` |
| K02 | codex | lantern | 03-03 09:00 | 03 | 3 | Port the frontmatter tests to unittest | `test_frontmatter.py` |
| K03 | codex | quarry | 03-05 13:00 | 05 | 4 | Profile the tokenizer on large transcripts | `tokenize_markdown`, `MemoryError` |
| K04 | codex | harbour/api | 03-07 10:00 | 07 | 3 | Write the idempotency key migration | `IdempotencyKeyConflict`, `0042_idempotency_keys.sql` |
| K05 | codex-subagent of K04 | harbour/api | 03-07 10:20 | 07 | 1 | Check the migration on a copy of staging | `IdempotencyKeyConflict` |
| K06 | codex | fieldnotes | 03-09 12:00 | 09 | 3 | Draft the weekly notes template | none |
| K07 | codex | lantern | 03-11 14:00 | 11 | 4 | Fix the Windows path bug in catalog keys | `OSError: [WinError 123]`, `PureWindowsPath` |
| K08 | codex | quarry | 03-12 10:00 | 12 | 3 | Benchmark BM25 against exact match | `bm25_k1`, `--sortr modified` |
| K09 | codex | harbour/web | 03-14 11:00 | 14 | 3 | Audit retry(attempt=3) call sites | `retry(attempt=3)` |
| K10 | codex | lantern | 03-15 20:00 | 15 | 3 | Bump version to 2.4.1 | `__version__` |
| U01 | cursor | harbour/web | 03-02 15:00 | 02 | 3 | Style the checkout summary card | `CheckoutSummary.tsx` |
| U02 | cursor | harbour/web | 03-04 16:30 | 04 | 4 | Debug the hydration mismatch on checkout | the hydration error string |
| U03 | cursor | fieldnotes | 03-05 18:00 | 05 | 3 | Outline the onboarding guide | none |
| U04 | cursor | quarry | 03-06 09:00 | 06 | 3 | Add a docs index status page | none |
| U05 | cursor | harbour/web | 03-09 10:00 | 09 | 3 | Add ECONNRESET toast to the retry UI | `ECONNRESET` |
| U06 | cursor | lantern | 03-10 13:00 | 10 | 3 | Rename parse_block_list to parse_yaml_list | `parse_block_list` |
| U07 | cursor | quarry | 03-12 17:00 | 12 | 3 | Fix flaky tokenizer test | `tokenize_markdown`, `MemoryError` |
| U08 | cursor | harbour/web | 03-15 21:00 | 15 | 3 | Ledger reconciliation UI | quotes C13's title |
| U09 | cursor | harbour/api | 03-14 09:00 | 14 | 3 | Explain the "invoice PDF" timeout | `render_invoice_pdf` |

The hydration error string is `Hydration failed because the server rendered HTML didn't
match the client`; it holds an apostrophe. U09's title holds double quotes. The
frontmatter escapes them to `\"`, but the `# title` heading keeps them plain. That
heading is the line rg matches.

**How the files are built.** `tests/recall_corpus.py` reads the spec file and, inside a
throwaway home and config, passes each session through the source's own
`render_document` and the shared `ConversationSource.export_one`. So the frontmatter, the
metadata block, the `provenance`, `exporter_version` and `exporter_spec_target` keys, the
file name and every `<!-- cairn:turn N -->` length are what an export writes today. A
subagent is written to `<session folder>/agent-<short>.md` and its parent carries the
`## Subagents` index its exporter renders. Two documents cannot come from an exporter:
the pre-schema file is composed from `head_open`, `title_line`, `head_close`,
`preserved_tail`, `notes_block` and `render_conversation`, without the metadata block;
the exchange document is `render_exchange` of an invented one-round thread, written to
`exchange-codex-<short>.md`. `source_revision` is `"fixture"`.

### 5.2 `tests/fixtures/recall-qrels.json`

```json
{
  "schema_version": 1,
  "anchor_now": "2026-03-16T10:00:00Z",
  "k": 5,
  "queries": [ { "id": "t01", "family": "temporal", "query": "2026-03-02",
                 "args": ["--all-projects"], "relevant": ["..."],
                 "expected_top1": "...", "note": "..." } ]
}
```

A row has `id`, `family`, `query`, `relevant`, `expected_top1` and `note`. `args` exists
for `temporal` rows only. A path is relative to `tests/fixtures/recall-corpus/`. In the
rows below, `{C01}` stands for that document's path.

| id | family | query | args | relevant (ordered for temporal) | expected_top1 | note |
|---|---|---|---|---|---|---|
| t01 | temporal | `2026-03-02` | `--all-projects` | C01, K01, U01 | C01 | three sources on one day, sorted by started_at |
| t02 | temporal | `yesterday` | `--all-projects` | C13, K10, U08 | C13 | the exchange document is not a session |
| t03 | temporal | `2026-03-09` | `--all-projects` | C04, U05, K06 | C04 | a resumed session lists on its later day and sorts by its first start |
| t04 | temporal | `2026-03-07` | `--all-projects` | K04 | K04 | subagent K05 hidden by default |
| t05 | temporal | `2026-03-07` | `--all-projects --include-subagents` | K04, K05 | K04 | the child lists after its parent |
| t06 | temporal | `2026-03-11` | `--all-projects` | C08, K07 | C08 | session crossing UTC midnight lists on both days; C09 hidden by --min-msgs 3 |
| t07 | temporal | `2026-03-11` | `--all-projects --min-msgs 1` | C08, C09, K07 | C08 | the two-message session shows |
| t08 | temporal | `2026-03-08` | `--all-projects` | C12, C07 | C12 | pre-schema file matches on its endpoints and sorts by its date |
| t09 | temporal | `2026-03-06` | `--all-projects` | U04, C05 | U04 | subagent C06 hidden |
| t10 | temporal | `last week` | `--project harbour` | C04, U05, C08, C10, U09, K09, C13, U08 | C04 | project label filter over a relative window |
| t11 | temporal | `last friday` | `--project lantern` | C11 | C11 | last weekday from a Monday anchor |
| t12 | temporal | `this week` | `--all-projects` | (empty) | null | empty window returns no rows, not an error |
| t13 | temporal | `4 days ago` | `--project /tmp/corpus/quarry` | K08, U07 | K08 | project given as a working directory |
| t14 | temporal | `last 3 days` | `--project lantern` | C11, K10 | C11 | last N days includes today |
| t15 | temporal | `2026-03-05` | `--all-projects --source codex` | K03 | K03 | one source |
| e01 | exact | `ECONNRESET` | | C10, U05, K01, C01 | C10 | `-i` also finds the lower-case log line |
| e02 | exact | `retry_with_backoff` | | K01, C01 | K01 | newest first |
| e03 | exact | `IdempotencyKeyConflict` | | K05, K04, C02 | K05 | a subagent transcript newer than its parent |
| e04 | exact | the FrontmatterParseError line | | C05 | C05 | colon and spaces |
| e05 | exact | `parse_block_list` | | U06, C06, C05 | U06 | identifier in a subagent transcript; newest first |
| e06 | exact | the hydration error string | | U02, C03 | U02 | apostrophe |
| e07 | exact | `retry(attempt=3)` | | K09 | K09 | regex metacharacters need -F |
| e08 | exact | the invoice path | | C04 | C04 | slashes and dots |
| e09 | exact | `LedgerMismatch` | | X01, C13 | X01 | rg finds the exchange document, which `list` hides |
| e10 | exact | `OSError: [WinError 123]` | | K07 | K07 | brackets |
| e11 | exact | `tokenize_markdown` | | U07, K03 | U07 | two sources; newest first |
| e12 | exact | `--sortr modified` | | K08, C09 | K08 | a query that starts with a dash was read as an rg flag (exit 2) before -e |
| b01 | title-bare | C01's title | | C10, C01 | C01 | known miss |
| b02-b06 | title-bare | the titles of C02, C04, C05, K03, U02 | | that document | that document | one row each |
| b07 | title-bare | C13's title | | U08, C13 | C13 | known miss |
| b08 | title-bare | K09's title | | K09 | K09 | regex metacharacters |
| b09 | title-bare | U09's quoted title | | U09 | U09 | quotes escaped in the frontmatter only |

That gives 15 + 12 + 9 = 36 rows.

### 5.3 Failure table (in the assertion message)

```
family      rows recall@5 top1  exact floors                             status
exact         12   0.92   0.92   -    recall>=1.00 top1>=1.00            FAIL
title-bare     9   1.00   0.78   -    recall>=1.00 top1>=0.77            ok
misses:
  e12 exact  want codex-sessions/quarry/2026-03-12-0195a008b000.md
             got  []
             note: a query that starts with a dash was read as an rg flag (exit 2) before -e
             rg: exit 2: rg: unrecognized flag --sortr modified
```

This is the table `ExactQrelsTest.test_floors` printed against the skill lines before
the `-e` fix (shortened: the misses list also names b01 and b07, which do not fail
their floor). `TemporalQrelsTest.test_floors` prints the same shape with the one
`temporal` row and an `exact` column.

## 6. Failure modes

| Case | Result |
|---|---|
| rg absent | the `exact` and `title-bare` tests skip with the exact message. `temporal` still runs |
| rg exits 2 (bad flag, bad regex) | the row scores 0, and the table prints the first line of stderr |
| A skill drops `-F`, `-i`, `-e` or `--type md` | e07 and e10 fail without `-F`; e12 fails without `-e`; `test_query_is_passed_with_e` names a missing `-e` on the list pass and `test_search_second_pass_passes_the_query_with_e` on the `/search` pass 2; without `--type md` the decoy takes top-1 on every rg row |
| The two skills' rg lines differ | `test_skill_rg_lines_agree` fails and prints both lines |
| A temporal row has no project scope | the loader fails, because the row would depend on the test's cwd |
| The renderer changes the document shape | `test_corpus_matches_builder` fails. The author runs `uv run python -m tests.recall_corpus --write` and reviews the diff |
| `parse_date_range` grows a new expression | no effect. New rows can use it |
| A user's ripgrep config file | ignored: the test drops `RIPGREP_CONFIG_PATH` |
| The catalog or a lock leaks into the real home | prevented, because HOME and the XDG homes are under the temp root. The CI home check still guards it |

## 7. Tests (`tests/test_recall_qrels.py`)

- `ScoringTest.test_metrics`: `recall_at`, `top1` and `exact` on empty and non-empty lists, and recall@5 for a list longer than five.
- `ScoringTest.test_an_rg_error_scores_zero_and_is_shown`: an rg error on an empty-gold row scores 0 and prints the error; the same row without an error scores 1.
- `ScoringTest.test_a_recall_miss_shows_the_relevant_set`: an rg row that keeps top-1 but loses a relevant document prints the relevant set and the first k.
- `ScoringTest.test_table`: the exact text of a two-family table with one miss.
- `ScoringTest.test_rg_outcome`: exit 0 gives the first 20 lines, exit 1 an empty list, exit 2 the first stderr line.
- `CorpusStoreSetupTest.test_failed_setup_restores_the_environment`: a copy failure in `setUpClass` leaves no patched environment, cached config or temp directory after the class cleanups.
- `CorpusStoreSetupTest.test_the_store_holds_a_non_markdown_decoy`: the decoy is the newest file and holds the rg queries.
- `QrelsFileTest.test_schema_and_ids`: the file loads; ids are unique; all three families occur.
- `QrelsFileTest.test_every_path_is_in_the_corpus`: every `relevant` and `expected_top1` path exists.
- `QrelsFileTest.test_family_sizes`: temporal at least 12, exact at least 10, title-bare at least 8 rows.
- `QrelsFileTest.test_loader_rejects_bad_rows`: each loader message in section 3, from inline bad documents.
- `CorpusTest.test_corpus_matches_builder`: the builder output is byte-identical to the committed corpus.
- `CorpusTest.test_every_document_parses`: the 32 session files give a `SessionRecord`; the exchange gives `None`.
- `CorpusTest.test_span_and_spread`: 3 sources, 4 projects, 14 distinct activity days.
- `TemporalQrelsTest.test_floors`: every temporal row through `recall.main` with `_utcnow` patched.
- `TemporalQrelsTest.test_clock_seam_is_used`: with `_utcnow` patched, `yesterday` is 03-15 to 03-16.
- `SkillRgTest.test_skill_rg_lines_agree`: the two skills hold the same list-pass flags. Needs no rg.
- `SkillRgTest.test_query_is_passed_with_e`: `-e` sits directly before the query placeholder in both skills.
- `SkillRgTest.test_search_second_pass_passes_the_query_with_e`: the same for the `/search` pass-2 `rg -n` line.
- `ExactQrelsTest.test_floors`: the `exact` and `title-bare` rows through rg (skipped without rg).

The gate discriminates. Each mutation below was run once against this change:

| Mutation | Result |
|---|---|
| The skill lines without `-e` (the state before this change) | e12 fails: `rg: unrecognized flag --sortr modified`, exact 0.92/0.92 |
| `-F` removed from both skill lines | e07 and e10 fail (exact 0.83/0.83); b08 fails (title-bare top-1 0.67) |
| `--sortr modified` removed from both skill lines | e01 and e11 lose top-1 (exact top-1 0.83) |
| The `_utcnow` patch removed from the temporal runner | t02, t10, t11, t13 and t14 fail (temporal 0.67) |
| `--type md` removed from both skill lines | the decoy takes top-1 on every rg row (exact top-1 0.00, title-bare top-1 0.00) |
| `-e` removed from the `/search` pass-2 line | `test_search_second_pass_passes_the_query_with_e` fails |
| `score` ignores the rg error | `test_an_rg_error_scores_zero_and_is_shown` fails |
| The environment restore not registered as a class cleanup | `test_failed_setup_restores_the_environment` fails |

## 8. How to add a query when a retrieval bug is fixed

Add one row per bug to `tests/fixtures/recall-qrels.json`. Give it the next free `id` in
its family. Write the `note` as the symptom the user saw, for example "session resumed on
Monday did not list on Monday". Do not write the fix in the `note`. If the corpus lacks a
document that shows the bug, add it to `tests/fixtures/recall-corpus.spec.json`, then
run:

```bash
uv run python -m tests.recall_corpus --write
```

and commit the new files. Run the test on the code before the fix and confirm that the
new row fails:

```bash
uv run python -m unittest tests.test_recall_qrels -v
```

Then run it on the fix and confirm that it passes. If a floor changes, change it in
`FLOORS` in the same commit, and give the observed rate and the reason in the commit
message. A floor is raised to the observed rate and never lowered without a written
reason. A new document can change the rg order of existing rows (its mtime is its last
activity), so read the whole table, not only the new row.

## 9. Docs updated

- `skills/search/SKILL.md` and `skills/recall/SKILL.md`: `-e` before the query, and the
  sentence "`-e` keeps a query that starts with `-` from being read as a flag."
- `README.md`: one line under "Developing cairn" that points to this document.

## 10. Open questions

1. **Capture and replay.** Out (section 4.5). Look at it again in the change that adds a
   search wrapper. That wrapper is the first point where cairn sees a topic query.
2. **A deterministic title path.** Nothing in this change. The b01 and b07 misses record
   that the mtime order lets a newer quote outrank the titled session. A later change can
   promote the files whose `title:` line matches; it raises the `title-bare` top-1 floor
   to 1.0, and it can re-admit a `title-in-question` family if it extracts quoted titles
   from a question.

## 11. Implementation notes

Where the implementation departs from the design above:

1. **recall@5 denominator.** The design divided by |R|. A temporal row with eight
   relevant sessions (t10) could then score at most 5/8 although the list was exactly
   right. The test divides by min(|R|, 5), the usual definition. The ordered `exact`
   metric still checks every row of a temporal list.
2. **The builder runs `export_one`.** The design listed the helper functions that
   `export_one` and `assemble_document` call. The builder instead calls the real
   `render_document` and `export_one` in a throwaway home, so the join, the metadata
   block and the exporter and provenance stamps cannot drift from the exporters. The two
   documents that no exporter writes (pre-schema, exchange) use the helpers.
3. **Mtime tie-break.** One second per file in (activity, path) order, not in spec order.
   The offset only makes the order total: it cannot invert two sessions, even when a new
   corpus document lands seconds from another.
4. **The exchange title** is the one `render_exchange` derives from the prompt
   ("codex round 1: ..."), not a hand-written title.
5. **ripgrep in CI.** The recommended default was taken: one `apt-get` step before
   `unittest`. The `rg` check in `cairn doctor` stays optional for users.
6. **The `--sortr modified` mutation** failed e01 and e11, not the four rows the design
   predicted: without a sort, rg's directory order happened to put e03 and e05 right.
