# PR specification: Verify QMD compatibility and embedding completion

## Validation (2026-10-17, branch feat/invoke-codex-skill)

Checked against the code on this branch, the real sync log
(`~/Code/context/.context/logs/context-sync.log`), the installed QMD 2.5.3 source, `ps`
and `crontab -l`. Implemented in the same branch; see CHANGELOG `[2.5.0]`.

| Proposal | Verdict | Evidence |
| --- | --- | --- |
| Exit 0 with failed chunks is a false success | VALID | 16:19 run logged `[qmd-embed] ok (1890.7s)` above `⚠ 48219 chunks failed`; QMD 2.5.3 `embed` prints failures and still exits 0 (`dist/cli/qmd.js` `vectorIndex`). |
| Shared diagnostics in `qmd.py` | VALID WITH CHANGES | `parse_version`, `parse_status`, `index_status`, `qmd_version`, `embed_failures`. 2.5.3 prints `N chunks still failed after retries`, `Session expired` and `Error rate too high` (`dist/store.js`); the plan's patterns covered only the 1.1.5 wording. |
| Version floor 2.5.3 gates embedding; doctor fails below it | REJECTED (kept as a doctor warning) | One machine. Sync now judges every version by embed output and `qmd status`, which catches the 1.1.5 failure without a number check. |
| Runtime probe before the index stages in sync | REJECTED | `qmd collection list` and `qmd update` already fail loudly when QMD cannot run (02:07 to 16:07 runs: `[qmd-update] FAILED (0.0s)`). An extra hourly probe adds nothing. |
| No embed after a failed `qmd update` | VALID | Those same runs then ran `[qmd-embed] FAILED (0.0s)` for nothing. Collection-registration failure does not block embed (other collections still index). |
| One embed per run, same 3600 s timeout, status read inside the same lock | VALID | Tested: the stub records `embed held` and `status held`. |
| Pending documents after embed make the run fail | VALID WITH CHANGES | After a clean embed, pending is logged `[qmd-verify] INCOMPLETE` but does not fail: QMD skips whitespace-only documents (`if (!doc.body.trim()) continue`) yet counts them pending, a permanent hourly false failure. Failed chunks still fail via the output; unreadable status and documents-without-vectors fail. |
| Doctor: shared probe, `--strict-index` table | VALID WITH CHANGES | Runtime failure is critical; version rows warn in both modes; strict fails on unreadable status, no vectors or pending. The old `qmd status` call had no timeout. |
| Setup, update, README, CHANGELOG guidance | VALID WITH CHANGES | Written shorter; the setup text that said zero vectors means no search works is fixed; separate `search`/`vsearch`/`query` added. |
| Tests | VALID WITH CHANGES | `tests/test_qmd_health.py`, `tests/test_doctor_index.py`, and `tests/test_sync.py` (shared with the robustness plan, instead of `test_sync_embeddings.py`). |
| Manual acceptance and `docs/qmd-reliability-acceptance.md` | NOT DONE | Running `cairn sync` against the real store was out of bounds for this pass; the stub-binary subprocess tests cover the failure and recovery path. |
| Bump to 2.4.1 | REJECTED | This branch is the unreleased 2.5.0; entries go under `[2.5.0]`. |
| "`make check` lacks the format check" | INACCURATE | `make lint` already runs `ruff format --check`. |
| QMD 2.8.3 native SQLite crash | NOT VERIFIABLE HERE | Only `[qmd-embed] FAILED (123.0s)` at 17:00 and a qmd symlink re-created at 17:00 remain; recorded as a one-machine note in setup. |

Status: validated and implemented on feat/invoke-codex-skill with the changes recorded above.
Based on the Cairn 2.4.0 installation investigation on 2026-10-17.

## Proposed PR title

**Detect unsupported QMD installations and incomplete embedding runs**

## Problem and intended result

Cairn currently accepts an existing QMD executable without checking its version or
whether its native dependencies can run. It also treats an embedding subprocess's
zero exit code as success, even when QMD reports failed chunks. `cairn doctor`
reports embeddings as healthy whenever any vectors exist.

After this PR, setup must detect an unsupported or broken QMD installation before
starting expensive embedding. Full sync must verify embedding completion and
return nonzero for failed or incomplete work. Doctor must distinguish a usable
keyword index, pending semantic indexing, and a fully embedded index. A normal
fresh installation must remain distinguishable from a broken dependency.

This is a Cairn integration and diagnostics fix. It does not fix QMD's internal
timeouts, native dependency bugs, or database migrations.

## Observed evidence and its limits

| Observation | Implication for this PR |
| --- | --- |
| QMD 1.1.5 embedded 83,072 of 131,291 chunks, then reported 48,219 failed chunks after its approximately 30-minute session limit. Cairn logged `[qmd-embed] ok` because the process returned 0. | Exit code alone cannot establish embedding success. |
| One document retained only its first seven of ten chunks. | Existing vectors do not establish complete coverage, including within a document. |
| QMD 2.8.3 crashed in its native SQLite dependency on this macOS Apple Silicon installation using Node 22.12.0. | A version check must be accompanied by an actual QMD runtime probe. This observation does not establish that 2.8.3 fails on every platform or Node release. |
| QMD 2.5.3 ran successfully on the same installation. The repair run embedded 53,742 chunks from 622 documents with no reported chunk failures. | Use 2.5.3 as the initial tested compatibility baseline, not as a claim that it is the earliest version containing every relevant fix. |
| Existing vectors were preserved using a backup and a one-off metadata repair after sample vectors were compared. | Do not turn that local repair into Cairn's database migration implementation. |
| A real Codex conversation ended with `/exit`, its own hook request reached `completed`, and its export passed recall, keyword, vector, and hybrid search. | No Codex hook rewrite is required for this PR. |
| A subsequent scheduled sync discovered additional history requiring embeddings. | Completion is a snapshot. Later pending work does not invalidate an earlier completed run. |
| Granola 7's credential storage prevented new Granola exports while other sources and indexing worked. | Keep source failures separate from index failures; Granola support is a separate issue. |

These are local observations, not a cross-platform compatibility matrix. The
successful QMD 2.5.3 run does not prove that arbitrary-size backfills avoid its
own session timeout.

## Required implementation

### 1. Shared QMD diagnostics in `src/cairn/qmd.py`

Add shared, typed helpers used by both sync and doctor. Keep the runtime
standard-library-only. Suggested interfaces are `probe_qmd(...)`,
`parse_qmd_status(...)`, and `embedding_failures(...)`; the behavior below is
required even if the final names differ.

The diagnostics result must retain:

- Configured QMD executable path, parsed version, and compatibility verdict.
- QMD-reported document total, vector total, and pending document count.
- Probe errors and bounded diagnostic output, including exit code or signal.
- Unknown values explicitly; do not substitute zero for missing information.

Implement these rules:

1. Always invoke `config.qmd_binary` with an argv list. Use the same effective
   environment/index as sync and the existing hook/cron launchers. Do not silently
   substitute another `qmd` found on PATH.
2. Run `qmd --version` with a 10-second timeout and `qmd status` with a 30-second
   timeout. `status` is the runtime probe: a binary that prints its version but
   crashes when opening its index is not usable.
3. Set the initial verified-embedding minimum to QMD **2.5.3**, the tested
   baseline. Versions below it are unsupported for verified embedding. A newer
   version is eligible only if its runtime probe and supported status parsing
   succeed; a higher version number alone is insufficient.
4. Parse actual version output such as `qmd 2.5.3 (179d450)`. Treat unparseable or
   prerelease versions as unverified rather than guessing compatibility. Do not
   change Cairn's existing engine/plugin version parser to accept arbitrary text.
5. Strip ANSI escapes before parsing the QMD `Documents` status block. Accept
   comma-separated numbers. Require recognizable `Total` and `Vectors` fields.
   In the tested 2.5.3 format, `Pending` is omitted when zero: recognize that
   specific complete output shape as zero pending. Missing/malformed output or
   an unrecognized format must be unknown, not healthy.
6. Preserve QMD's units: vector counts are chunks; pending counts describe
   document hashes needing embeddings, not missing chunks. Do not compare vector
   and document totals as if they were the same unit or invent a completion
   percentage from them.
7. Inspect the complete captured embedding stdout/stderr for positive failed-
   chunk summaries and explicit per-chunk embedding errors before truncating
   them for logging. Recognize the observed `48,219 chunks failed`/`48219 chunks
   failed`, `Error embedding ... chunk ...`, and associated session-release
   failures. A reported zero failed chunks is not an error.
8. Do not treat all stderr as failure. The observed Metal compiler warning also
   appeared before successful embedding and search. Nonzero process exit,
   explicit embedding failures, and verified index state determine the result.

Use QMD's CLI as the integration boundary. Do not read or modify its private
SQLite tables, model aliases, fingerprints, or chunk metadata in production
Cairn code. A CLI probe may perform QMD's own normal initialization; Cairn must
not implement a separate migration or automatically install/upgrade dependencies
as part of doctor or cron.

Node information is diagnostic context, not an invented global version rule.
Setup should record Node's version on PATH and QMD's executable path, then test
the configured QMD itself. If a wrapper uses a different runtime, do not label
the PATH version as proof of that wrapper's actual Node version. Do not hardcode
“all Node 22 works” or “all QMD 2.8.3 fails.”

### 2. Verified full-sync embedding in `src/cairn/sync.py`

Replace the bare embedding call at the end of `main()` with an embedding-specific
runner that uses the shared diagnostics. Do not make generic source-export
success depend on QMD-specific output parsing.

Required execution order and behavior:

1. Preserve durable hook-worker processing and independent source exports. A
   QMD problem must not prevent other source Markdown exports from being saved.
2. Keep the existing canonical `locked_index()` boundary around collection
   registration, update, embedding, and the final status check. Do not add a
   second simultaneous embedding process or acquire the same lock recursively.
3. Check runtime health before the full-sync index stages. If QMD cannot run,
   record an index failure and skip dependent index stages. If only its version
   is unsupported/unverified, keyword update may proceed, but skip embedding
   and return nonzero with the compatibility diagnosis.
4. Require successful collection registration and `qmd update` before running
   embedding. Do not claim a complete sync after an index-update failure.
5. Run `qmd embed` once per sync invocation, keeping the existing outer timeout
   unless there is separate evidence for changing it. Increasing Cairn's timeout
   does not remove QMD's internal session timeout. No unbounded retry loop.
6. Capture the subprocess result and inspect the full output for embedding
   errors. Retain bounded head/tail output in the log, as today.
7. If QMD exited normally, obtain post-run status while still holding the index
   lock. Emit `[qmd-embed] ok` only after verification, not immediately on exit 0.
8. Return nonzero for any of: nonzero embedding exit, timeout, positive chunk
   failures, explicit embedding errors, unreadable post-run status, or remaining
   pending documents. A nonempty index with zero vectors is also not complete.
9. A valid empty index is a no-op success, explicitly labeled `empty`. A valid,
   already-embedded index is also success even if the run creates zero new
   vectors. Never require the vector count to increase on every run.
10. A failed source must still allow successful index stages to run. Preserve
    the overall nonzero exit, but name the failed source separately from the
    embedding outcome.

Use distinct messages for failure and unfinished work. For example:

```text
[qmd-embed] FAILED: QMD exited 0 but reported 48219 failed chunks
[qmd-embed] INCOMPLETE: 611 documents still need embeddings; retry incremental sync
[qmd-embed] FAILED: could not verify post-run QMD status
[qmd-embed] ok: pending=0; existing vectors retained
[qmd-embed] empty: no indexed documents to embed
```

`INCOMPLETE` must still contribute to exit code 1. If a final source list is
logged, preserve the existing `=== done ===` / `=== FAILED ===` terminators for
existing log readers. All checks are index-wide, matching the existing global
`qmd embed`; they must not claim coverage is limited to Cairn-managed collections.

Do not classify new pending documents as corruption. Cairn's lock coordinates
Cairn writers; it cannot prevent an unrelated process from editing QMD directly.
Report the state at verification time and let a later incremental run process
new work.

### 3. Accurate doctor output in `src/cairn/admin.py`

Replace `_qmd_vector_count()` with the shared diagnostics. Reuse a single status
result for the runtime and embedding checks rather than repeatedly launching
the same probe.

Add **`cairn doctor --strict-index`** for setup and acceptance checks. Existing
`--harness` and `--plugin-manifest` options must compose with it.

| QMD/index state | Default doctor | `--strict-index` |
| --- | --- | --- |
| Missing executable or failed runtime probe | Critical failure, exit 1 | Exit 1 |
| Version below the 2.5.3 baseline | Compatibility failure, exit 1; explain that keyword export/indexing may still work | Exit 1 |
| Unknown version or unrecognized status output | Warning; do not label index verified | Exit 1 |
| Valid empty index | `empty` / not yet populated; no critical failure | Exit 0, explicitly no search verification possible |
| Nonempty index, zero vectors | Warn that keyword search may work but semantic indexing is not ready | Exit 1 |
| Nonempty index with vectors and pending documents | Warn with vector and pending counts | Exit 1 |
| Nonempty index with vectors and zero pending | Index check passes | Index check passes |

Existing unrelated critical failures still determine the overall exit code. When
default doctor only warns about incomplete or unverified indexing, its summary
must explicitly mention that limitation instead of presenting an unqualified
index-health success. Do not call an empty index a successful search test.

Doctor must not embed, download models, upgrade QMD, edit configuration, alter
hook trust, or change enabled sources. It should explain the next action.

### 4. Setup, update, and recovery guidance

Update these existing files:

| File | Required edit |
| --- | --- |
| `skills/setup/SKILL.md` | Extend preflight beyond executable existence; record version/runtime diagnostics. Use strict index verification after sync and require keyword **and** vector search before declaring semantic search ready. |
| `skills/setup/references/updating.md` | Document QMD compatibility checks, preservation of the existing index, incomplete-embedding recovery, and the difference between new pending work and a failed previous run. |
| `README.md` | State the verified QMD baseline, explain the runtime probe and strict check, and distinguish keyword availability after exit from embeddings maintained by full/hourly sync. |
| `CHANGELOG.md` | Describe the false-success fix, compatibility diagnostics, and new strict option. |

The guidance must explicitly say:

- An existing QMD binary can still be too old or broken. A successful package
  installation or `--version` call is not the finish line.
- QMD 2.5.3 with Node 22.12.0 on macOS Apple Silicon is the observed working
  combination, not a universal support guarantee. Provide an explicit
  `npm install -g @tobilu/qmd@2.5.3` recovery example for that environment;
  do not recommend an untested upgrade to latest as a guaranteed fix.
- Recheck the **configured absolute QMD executable** after installation. Changing
  a package under a different npm prefix may leave Cairn using the old binary.
- Preserve the existing configuration, Markdown, QMD index, models, cron,
  hooks/trust, and enabled sources. Back up the index before dependency changes
  that may migrate it, using a consistent backup method while writers are
  coordinated; copying only a live SQLite main file is not a safe backup recipe.
- Resolve the compatibility/runtime issue, then retry incremental
  `cairn sync --cron` after the previous job has ended. Do not prescribe deleting
  the index, `qmd embed -f`, or importing the temporary metadata repair script.
- If supported QMD migration/recovery cannot preserve a legacy index, report
  the upstream limitation and available options. This PR does not promise that
  every legacy migration can avoid re-embedding.
- A partially populated index is not a completed embedding run. Conversely,
  BM25 keyword search can work without any vectors; fix the current setup text
  that says zero vectors means all search cannot work.
- A normal exit hook completes export and keyword indexing. A completed hook
  request does not establish that semantic embeddings are ready.
- Setup must separately run `qmd search`, `qmd vsearch`, and `qmd query` against
  a known exported conversation. A successful hybrid query alone can rely on
  lexical results and does not prove vector retrieval.
- Preserve enabled but failing sources and report their specific blockers.
  A Granola-only failure does not mean the Codex path failed, nor does Codex
  success mean every enabled source is healthy.
- Remove promises that subsequent embedding always takes seconds. Incremental
  indexing can still take substantial time after a large history backfill.

## Tests that must accompany the implementation

Use the existing `unittest` and `unittest.mock` conventions. Automated tests must
not need model downloads, credentials, network access, or the developer's real
QMD database. Fixtures must contain synthetic text and sanitized CLI output.

| Location | Required cases |
| --- | --- |
| New `tests/test_qmd_health.py` | Parse version with commit suffix; old, current, newer, prerelease, and unknown versions; ANSI/comma-formatted status; zero pending via the known omitted-line format; nonzero pending; missing fields; malformed counts; explicit zero failed chunks versus positive failures. |
| New `tests/test_qmd_health.py` | Version succeeds but runtime status crashes; missing binary; timeout; nonzero exit; configured executable and effective environment are honored; benign stderr does not cause failure. |
| New `tests/test_sync_embeddings.py` | QMD returns 0 with failed chunks: sync returns 1 and never logs embedding `ok`. Include a failure in the middle of output that bounded logging would omit. |
| New `tests/test_sync_embeddings.py` | Exit 0 plus pending work; unknown post-status; nonzero exit; timeout; healthy no-op; valid empty corpus; successful complete embedding; nonempty corpus with zero vectors. |
| New `tests/test_sync_embeddings.py` | Update/registration failure skips embedding; unsupported version skips embedding but preserves exports; one failed source does not prevent other exports or index work, and still makes the overall result nonzero. |
| New `tests/test_sync_embeddings.py` | Embedding and post-status stay within the same index lock; one embed attempt per invocation; no automatic forced rebuild or database mutation. |
| New `tests/test_doctor_index.py` | Exercise every row of the doctor table, including exit codes and the final summary; verify `--strict-index` composes with existing options and performs no embedding/install commands. |
| Update `tests/test_qmd_collections.py` | Replace obsolete `_qmd_vector_count` tests with shared-helper coverage; keep existing collection-registration tests intact. |
| Existing `tests/test_hooks.py` and `tests/test_runtime.py` | Retain passing hook lifecycle and canonical-lock regressions; no embeddings added to the hook callback or worker. |

Add a small subprocess-fixture test using a fake QMD executable that exits 0
while printing a failed-chunk summary. It should exercise the sync result and
log together, beyond just a regex unit test. A later invocation can simulate
successful recovery, proving there is no permanent false-failure latch.

## Manual acceptance before merging

Use a temporary profile/index and synthetic conversation for new tests. Do not
commit private transcripts, local configuration, SQLite files, model files, or
credentials. Do not require a repeat of the full historical backfill.

1. Record Cairn version, configured QMD path/version, Node/runtime context, OS,
   architecture, and relevant index/config paths.
2. With a supported QMD installation and a small exported corpus, run full sync.
   Require a verified embedding result with zero pending work and no chunk
   failures. Run `cairn doctor --strict-index`.
3. Run full sync again without changing the corpus. It must succeed without
   demanding new vectors or rebuilding existing embeddings.
4. Run the fake failure case. Exit 0 from QMD plus a failed-chunk summary must
   produce a nonzero Cairn result and a clear embedding failure in the log.
5. Exercise a real trusted Codex SessionEnd with an actual user/assistant
   exchange containing a distinctive marker, then normal `/exit`. Match the
   **new session's own ID** to a completed request; do not use an old completed
   count or manual hook invocation as proof. A shell-only session is insufficient.
6. Find that export with `qmd search` before a scheduled full sync. Complete
   embedding via full sync, then retrieve the same export using paraphrased
   `qmd vsearch` and a `qmd query`; verify recall can expand the conversation.
7. Simulate an unrelated source failure. Verify the index result remains
   independently reported, the overall sync exits nonzero, and the source stays
   enabled. No real Granola credentials are needed for this test.
8. Record commands, exit codes, expected document identity, and relevant log
   summaries in a new `docs/qmd-reliability-acceptance.md`. Keep historical Codex
   acceptance evidence intact. Label untested platforms as untested.

If new activity creates pending work after a successful check, record both
snapshots. Do not claim permanent zero backlog, run an endless catch-up loop,
or reinterpret the already-completed test as a failure.

## Files and release work

Production changes should be confined to `src/cairn/qmd.py`,
`src/cairn/sync.py`, and `src/cairn/admin.py`, plus the docs/tests listed above.
No new runtime dependency or configuration setting is required.

Because engine and setup-skill behavior change, include the repository's required
release bookkeeping:

- Bump `src/cairn/__init__.py` to the next release version; use 2.4.1 if the target
  branch is still at 2.4.0, otherwise choose the next appropriate version.
- Keep root `plugin.json` and `.claude-plugin/plugin.json` aligned with it.
- Regenerate `.codex-plugin/plugin.json` with
  `python scripts/sync_plugin_manifest.py` after updating the root manifest.
- Refresh `uv.lock` if the local Cairn package version changes in the lockfile.
- Keep the plugin-manifest/version tests passing.

Run the actual CI gates:

```sh
make check
uv run ruff format --check src tests
```

The explicit format check is needed because the current Makefile's `check`
target runs lint, typecheck, and tests; CI also runs formatting verification.
CI must pass on its existing Python 3.11, 3.12, and 3.13 matrix.

## Excluded from this PR

- The temporary SQLite metadata migration, machine-specific repair paths, or
  production imports of QMD's internal JavaScript modules.
- A change to the Codex hook schema, trust flow, fallback registration, or
  embeddings inside SessionEnd.
- Granola 7 credential/Keychain support, disabling Granola, or hiding its errors.
- Installing Node automatically from doctor/cron, globally forcing QMD versions
  on existing users, or patching files inside a globally installed npm package.
- Automatic deletion/rebuilding of indexes, forced embedding, or optional
  historical metadata backfills.
- A general dependency manager, new search algorithm, or broad source-adapter
  refactor.

## Suggested PR description

> Cairn could report a successful sync when QMD exited 0 but left failed embedding
> chunks, and doctor marked any nonzero vector count as healthy. This change
> probes the configured QMD version/runtime, verifies embedding output and
> post-run pending work, and adds `doctor --strict-index` for setup verification.
> Source failures remain independent of index results. Setup now verifies
> keyword and semantic retrieval and documents compatible dependency recovery
> without modifying QMD's database directly.

Add the actual automated and manual validation results when implementation is
complete; the proposed behavior above is not evidence that those tests passed.

## Merge acceptance checklist

- [ ] Exit 0 plus embedding errors can no longer produce a successful sync.
- [ ] Missing, old, broken, and unverified QMD installations are distinguished.
- [ ] Doctor reports pending embeddings and strict mode enforces the table above.
- [ ] Empty and already-embedded indexes are handled without false failures.
- [ ] Source failures remain isolated and accurately reported.
- [ ] Recovery preserves user state and performs no private QMD database edits.
- [ ] Real session exit, export, recall, keyword, vector, and hybrid retrieval pass.
- [ ] Regression tests, CI, release metadata, and acceptance evidence are included.
