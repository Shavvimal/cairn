# Codex support acceptance — 2.4.0

Implementation follows [the support plan](codex-support-plan.md). This record distinguishes
observed host behavior from unit tests and packaging checks. Full desktop parity is not
verified; do not infer it from successful CLI installation.

## Environment and isolation

Verified on 2026-10-16, macOS 26.6.2 (Apple Silicon), Codex CLI 0.160.0 and Claude Code
2.1.301. Codex 0.163.0 was also installed in a temporary npm prefix to inspect plugin
loading. No Windows support is claimed. Engine version: 2.4.0.

All live sessions, plugin registrations, exports, QMD index, and uv tools used temporary
profiles outside this checkout. Authentication used the existing local signed-in accounts;
credentials and raw transcripts are not committed. The developer's actual history,
crontab, global engine and plugin settings were not modified.

## Feasibility findings that changed the implementation

- `SessionEnd` supplies the full session ID, absolute rollout path and cwd. Inherited
  `CODEX_THREAD_ID` can belong to a parent session, so the event is authoritative.
- A detached POSIX process with closed host streams survives normal client exit. The
  initial probe handed off in 0.0015 seconds; that measures the probe, not all of Cairn.
- Both inspected Codex versions load portable skills and onboarding but return no bundled
  hooks from `hooks/list` for the schema-valid portable manifest. Removing `$schema`
  made compatibility hooks appear, but violates the portable schema. Cairn keeps a valid
  manifest and uses the plan's owned user-hook fallback after inspecting actual discovery.
- The CLI also requires the generated `.codex-plugin/plugin.json` compatibility projection.
  The root manifest remains authoritative; `scripts/sync_plugin_manifest.py` generates it.
- Codex 0.160.0 can emit user input only as `response_item` messages. Environment context
  has the same role but different `content_item_kinds`. The parser supports these and
  legacy `event_msg` prompts without double counting mirrored prompts.

These are observations of the tested clients, not guarantees about other clients. See the
vendor [hook reference](https://learn.chatgpt.com/docs/hooks) and
[plugin packaging guide](https://developers.openai.com/plugins/build/plugins).

## Evidence collected

| Criterion | Evidence / result |
| --- | --- |
| Automated engine checks | `make check`, including ruff, mypy and unittest; explicit formatting check. 157 tests pass; 125 passed at baseline. |
| Python compatibility | All 157 tests passed with Python 3.11, 3.12 and 3.13 in isolated uv environments. |
| Portable schema | Root manifest validated with the published Agent Plugins 1.0.0 JSON Schema. All three manifests/version and cache-relative resources checked in tests. |
| GitHub installation | From outside the checkout: `codex plugin marketplace add shavvimal/cairn --ref feat/codex-support`, then `codex plugin add cairn@cairn`. All four cached skills and onboarding were discovered. `uv tool install git+https://github.com/shavvimal/cairn@feat/codex-support` built commit `66788529d4cb1762d67a1885bfd8e273e9604743`; `uv tool upgrade cairn` subsequently fetched and installed `49cfce182829d42ac6dc2c2985b268a1c1240e97` and final code commit `6faf4e4`. |
| GitHub Claude installation | `claude plugin marketplace add https://github.com/shavvimal/cairn.git#feat/codex-support` and `claude plugin install cairn@cairn` succeeded. `claude plugin update cairn@cairn` recognized version 2.4.0. |
| Codex trust written by cairn (2026-10-30) | Codex CLI 0.165.0. The `trusted_hash` formula in [the trust-hash spec](codex-trusted-hash-spec.md) reproduced all four `[hooks.state."<key>"]` hashes of a live `config.toml` from its `hooks.json`, cairn's SessionEnd fallback included, read-only. A completed SessionEnd request at 12:32 that day shows the trusted fallback ran on 0.165.0. `hooks/list` after a cairn-written table, on a temporary `CODEX_HOME` with Codex 0.165.0 (2026-10-30): the fallback reported `trustStatus` `trusted`, and `cairn doctor --harness codex` printed `trusted (written by cairn)`; with the stored hash altered, `hooks/list` reported `modified`. Not yet run on a real `~/.codex`. Unit tests cover the table edit on temporary `CODEX_HOME` directories only. |
| Codex trust | `/hooks` showed one user SessionEnd fallback with a three-second timeout. Reviewed and trusted in the TUI, without hook-trust bypass for the lifecycle test. `hooks/list` then reported trusted. |
| Actual Codex shutdown | TUI session `01a145b7-2f15-7452-9196-436717c9d691` replied with “Cairn quince orchard lifecycle marker.” Normal `/exit` returned successfully. Durable request reached `completed`; `qmd search quince --json` returned the Codex export, without cron. |
| Actual Claude shutdown | Authenticated session `aafea0b1-4948-45be-af59-e4a5b13df071` emitted SessionEnd, exported and indexed successfully. Queue-to-completion was about 0.44 seconds. `qmd search orchard --json` returned its export. |
| GitHub lifecycle | After TUI trust, session `01a145c2-2ab9-74d1-a893-151c6f396482` returned the marigold marker and ended normally. The worker completed in 0.385 seconds from enqueue; `qmd search marigold --json` returned its export. Claude setup session `d9c2fd78-16e5-4ac6-9938-dd4247077f22` completed its hook job in 0.424 seconds and became searchable. No scheduled sync ran. |
| Cross-host skills | Codex used the installed recall skill to list/expand the Claude session. Claude used the installed recall skill to expand the Codex quince session and returned the correct Codex resume argv. |
| Initial embeddings | Real `qmd embed` completed with exit 0 and vectors for the isolated corpus. A nonfatal Metal build warning preceded successful embedding. |
| Guided setup | Installed setup instructions ran in both hosts. Codex initial sync returned 0 with 3 documents and 5 embedded chunks; real searches hit both sources. Claude verified the same index and its own launcher. Both reported unverified lifecycle before shutdown. Cron installation used an isolated `crontab` executable, not actual scheduling. |
| Mutations | The Codex quince record retained three selected-session notes and active status; the separately selected Claude record became done. Both hosts used source-qualified IDs; no note appeared on the other record. |
| Native Claude resume | The argv from `cairn resume claude:aafea0b1-4948-45be-af59-e4a5b13df071 --json`, executed with a noninteractive prompt, returned under the same session ID and emitted its shutdown hook. |
| Native Codex resume | `codex resume 01a145b7-2f15-7452-9196-436717c9d691` opened the prior conversation in the TUI and answered a new turn under that identity. |
| Upgrade / migration | `codex plugin marketplace upgrade cairn --json` refreshed the Git snapshot; reinstall and engine upgrade worked. Re-export added native revision metadata while preserving notes/status. Installed mixed-host mutation hints were refused while explicit source-qualified IDs worked. Tests verify both directions of version drift and host-specific instructions. |
| Disable / removal | A real Codex session with plugin `enabled = false` created no durable request. `cairn hook uninstall --harness codex` and `codex plugin remove cairn@cairn` removed only the owned handler/package. An unrelated handler, Claude configuration, shared launcher, all Markdown hashes, and the cron fixture were unchanged. Reinstallation succeeded. |
| Post-upgrade lifecycle | Codex juniper session `01a145cc-19a2-7fc1-9c00-a4c17a91e9ff` and Claude saffron session `bb188913-f599-4c80-810d-bafbec44d72a` both completed automatic sync and returned matching QMD hits after the native-revision fix. The isolated queue contained 8 completed and 0 failed/running/queued requests at that checkpoint. A further real Codex session on final code `6faf4e4` returned the hazel marker, completed its hook job, and appeared in `qmd search hazel` (9 completed, 0 failed). |
| Shared store / concurrency | Synthetic cross-source, UTC, gaps, migration, collisions, bounded expansion, annotation races, failed QMD/retry, durable generations and detached worker tests pass. Native checkpoints are independent of exported file mtimes, so annotations cannot hide new native messages. |

## Live skill evaluations

Each host used the installed package outside the checkout. These are observed behaviors,
not claims that all models will always follow the workflow.

| Scenario | Codex 0.160.0 | Claude 2.1.301 |
| --- | --- | --- |
| Unrelated “find the double function” | Read the code, returned 6, no Cairn invocation. | Read the code, returned 6, no Cairn invocation. |
| Search + no-result recall + annotation/resume | Found quince through search; 1999-01-01 returned `[]`; wrote only the selected Codex note; printed resume without launching. | Same observable results using the shared workflows. |
| Missing setup choices | Asked for storage, integrations and service docs; no configuration writes. | Asked the same required choices; no configuration writes. |
| Missing prerequisites | Restricted-PATH preflight reported qmd, installers, npm, brew and optional rg together; no installs/writes. | Same result through explicit `/cairn:setup`. |
| Silent embedding then failure | Actual sync used an injected qmd embed failure after 15 seconds of silence. Observed exit 1, 1 document/0 vectors, and stopped before hooks/cron. | Explicit `/cairn:setup` observed the failure and refused to call setup complete despite a BM25 hit. |
| Empty source | Sync returned 0, but 0 documents/vectors were reported as incomplete verification; no invented hits. | Same result through explicit `/cairn:setup`. |
| Explicit setup policy | Ordinary code requests did not activate setup; onboarding loaded the installed setup resource. | Natural-language attempts to invoke the Skill tool were rejected by `disable-model-invocation`; explicit `/cairn:setup` ran successfully. This policy is intentionally retained. |

The successful real embedding run and the controlled failure/empty runs are separate
experiments. The injected failure tests validate supervision, not model download reliability.
Optional Granola dependency failures are covered automatically, not with a live Granola account.

## Review follow-up (2026-10-17)

A review of the branch found two blockers and several defects; all are fixed on this
branch and re-verified as follows. Unit and CLI evidence only: the live host criteria
below were not repeated after these changes and remain open.

| Change | Evidence |
| --- | --- |
| Session lookup no longer parses every exported file | Stat-validated catalog (`src/cairn/catalog.py`). On the developer's real store (8,344 Claude files, 329 MB): `find_record` 577 ms cold (one-time rebuild), 80 to 90 ms warm, against 620 to 1,140 ms before. `cairn recall list yesterday --all-projects --json` returned the identical 35 references as the pre-change code on the same day. A real `cairn claude export --since 1d` into a scratch data root took 0.46 s for 14 sessions and 0.06 s when nothing changed, with no duplicate `native_id`. |
| Hook survives a plugin-before-engine upgrade | `hooks/launch.sh` (POSIX sh). Tests cover the recorded-engine path, `cairn` on PATH without a launcher file, a 2.3.x engine (exit 2, `unknown command 'hook'`) degrading to detached `cairn sync --hook` with the upgrade hint, pass-through of other failures, and no engine at all. `/usr/bin/python3` is only tried when the Command Line Tools exist. |
| Turn marker framing | Declared length is now exactly the written body plus separator; a test covers text, tool result and code block bodies ending in a newline plus a forged marker inside a body. The real 1,575-message session still frames with zero mismatches. |
| Frontmatter size | `activity_dates` (unique UTC days) replaces one line per timestamp; a 288-timestamp test session yields three lines. Files from this branch's earlier test profiles that carry `activity_timestamps` are still read. |
| Codex `export_target` | Each candidate rollout is parsed once. |
| Queue hygiene | Completed/disabled requests are pruned seven days after their terminal timestamp; failed and running requests are never pruned. `cairn hook status` shows actionable requests plus the ten most recent completed (`--all` for everything); doctor caps failure warnings at five. |
| Checks | `make check`: ruff, mypy and 184 unit tests pass; `ruff format --check` passes. |

At that checkpoint, not repeated after these changes: installation from GitHub, real Claude and Codex
shutdowns through the installed plugin, the Codex user-level fallback registration, and
the plugin-only upgrade path against a real 2.3.0 engine. These must be re-run with the
temporary-profile method above before the PR leaves draft. The final CLI check below
supersedes the outstanding shutdown/fallback checks; its package came from a local
build of the final sources, not a new GitHub fetch.

## Acceptance still requiring evidence

### Second review fixes (2026-10-17)

The six additional review findings are fixed. `make check` passes lint, mypy and
193 tests; explicit formatting and `git diff --check` also pass. New regression
coverage exercises both installed hook paths under a different host environment,
cross-process contention on shared QMD configuration/database paths, collection
timeouts followed by successful retry, legacy Claude sidechain skips, date-only
recall in four timezones, malformed cache structures, and annotation-preserving
upgrades with unavailable native history left intact.

A separate real-QMD check in temporary directories installed the launcher with a
selected configuration/cache, then invoked it from a different environment. The
detached worker completed; the new `marigold` text returned one hit from the selected
index and zero from the host's alternate index. A subsequent metadata upgrade and
`qmd update` kept the annotation and searchable result in the existing database.
This check did not run embedding or a real host shutdown; it does not close the
live-host acceptance gaps above or below. The user's existing store was inspected
read-only, with no exports or index updates against it.

### Remaining code findings and final CLI check (2026-10-17)

Both medium findings are resolved:

- Requests stop after three attempts, including recovery from a crash on the last
  attempt. `exhausted` requests retain the error for seven days and remain visible
  in status/doctor. Explicit `cairn hook retry SOURCE:FULL_SESSION_ID` starts a new
  budget. A final failure cannot overwrite a newer generation.
- Document, collection, queue, worker, hook-registration and QMD locks live under
  `~/.local/state/cairn/locks/`, keyed by canonical resource path and lock purpose.
  Different config/cache homes still contend on shared resources. New tests run
  export, annotation and indexing against a Git corpus and verify no lock sidecars.

All 200 tests pass on Python 3.11, 3.12 and 3.13; lint, mypy, formatting and whitespace
checks pass. The final sources were built into the isolated global tool installation
and copied into both installed plugin caches in the existing acceptance profiles.
No new GitHub fetch, user-store update, real cron registration or desktop test was run.

| Final check | Evidence |
| --- | --- |
| Claude Code 2.1.302 shutdown | Authenticated print-mode session `471b423d-08bf-42d2-ae83-05ace9cd6f0b` returned the tangerine marker. Its installed bundled hook completed in 0.67 seconds on attempt 1. The matching export appeared in QMD despite wrong host `XDG_CONFIG_HOME`, `QMD_CONFIG_DIR` and `INDEX_PATH`. |
| Codex CLI 0.160.0 shutdown | Session `01a14955-be22-7872-8a47-62170f0a9c63` returned the nectarine marker. The changed fallback hook was reviewed/trusted in the normal TUI, then `/exit` completed. The request finished in 0.38 seconds on attempt 1, and QMD returned the matching export despite wrong host index settings. |
| Queue/index isolation | Both live checks completed without cron. Final queue counts: 6 completed, 0 queued/running/failed/exhausted/invalid. Codex reports the SessionEnd hook enabled and trusted. The host's alternate index was never created. |
| Incremental maintenance | `cairn sync --cron` exited 0; real QMD embedded 9 chunks from 5 changed documents using cached models. Index: 17 documents, 65 vectors. No new lock sidecars appeared in the corpus or QMD directories. A nonfatal Metal compilation warning preceded successful embedding. |

- **Catalog rejection diagnostics:** `Catalog.stats["rejected"]` counts invalid or
  unreadable cache loads; a missing cache is an ordinary cold load and is not counted.
- **Bundled launcher environment follow-up:** installation now records identical
  environments under the selected XDG config home and `~/.config/cairn`; the shell
  launcher falls back to the latter when discovery misses. Regression coverage
  exercises both wrong and absent host `XDG_CONFIG_HOME`, plus removal of both copies.
  The follow-up `make check` passes all 194 tests.

### Annotation and expansion follow-up (2026-10-17)

Two additional regressions were reproduced with failing tests before fixing them:

- Granola's native notes and summaries may contain `## Conversation` before Cairn's
  handwritten `## My Notes`. The notes extractor now applies the conversation
  boundary only to chat sources. Tests verify that handwritten notes and fenced
  headings survive repeated meeting exports without duplicating transcript text.
- Recall now reads length-framed turns without translating embedded CRLF pairs.
  Annotation writes also preserve the transcript's original characters. A CLI
  regression test verifies the message limit before and after adding a note, with
  a forged marker inside the CRLF message, and checks unchanged transcript bytes.

Local `make check` passes lint, mypy and all 202 tests; explicit formatting and
whitespace checks pass. These changes were tested with synthetic exports and CLI
calls; authenticated host lifecycle checks were not repeated for this follow-up.

### Open host and platform checks

- **Codex desktop:** no Codex desktop application is installed in this environment. Desktop
  package loading, onboarding, hook trust, lifecycle and resume UI remain unverified.
- **Real hourly scheduling:** setup's crontab read/write flow was exercised through an
  isolated test double. No real hourly entry was installed or allowed to fire, so OS cron
  scheduling is unverified. The generated command itself was executed under a minimal
  environment against real QMD, with config, PATH and index environment recorded explicitly. This preserves the plan's isolation requirement.
- **Long first model download:** real embedding and a silent delayed failure were tested;
  a cold multi-gigabyte model download in each host was not. Models were already cached.
- **Additional host paths:** Linux lifecycle, Codex desktop, archive/delete and idle-timeout
  events, native fork UI, and Claude interactive terminal resume remain unverified. Codex
  CLI resume and Claude native resume in print mode were exercised. CLI 0.163.0 was checked
  for package/hook discovery, not its complete lifecycle matrix.

These gaps remain limitations on the support claim: the evidence supports the tested
macOS CLI hosts, not full desktop/Linux parity or public-directory submission.
For the CLI release, the remaining merge gate is green GitHub CI (including the
Python matrix and version check) on the final pushed commits. Do not treat checks
on an older PR head as verification of these changes. OpenAI public-directory
submission remains deferred.

### Isolated Claude profile check on the merge candidate (2026-10-17)

Run against `2fa71cf` with a scratch `HOME` (`/tmp/cairn-test-home`), a scratch
`CLAUDE_CONFIG_DIR`, the engine installed there with `uv tool install`, and the plugin
installed from a local clone of the branch through `claude plugin marketplace add` and
`claude plugin install`. The real `~/.claude`, `~/.config/cairn`, store and QMD index were
not touched (checked after every step).

| Check | Evidence |
| --- | --- |
| Hand-fired launcher with a real transcript | Request queued; first attempt failed with QMD `Cannot open database because the directory does not exist` for the pinned `INDEX_PATH`. Fixed in this commit (`locked_index` creates the directories). After `cairn hook retry`, completed on attempt 1. |
| Claude Code 2.1.302 interactive session `2a8b3ca6` | Hook ran on `/exit`; request completed in 0.38 s on attempt 1; export written; `qmd search` returns the marker. |
| Claude Code 2.1.302 print-mode session `594f74e9` | Debug log shows `Read hooks.json for plugin cairn` and `Registered 1 hooks`; request completed in 0.37 s; export indexed. |
| Launcher without `hook-launcher.env`, 2.4.0 engine on PATH | Session `262d175c`: launcher path 2 queued and completed the request. |
| 2.3.0 engine on PATH with the 2.4.0 plugin (violated upgrade order) | Session `6474a514`: launcher reported `completed with status 0` to the host, ran the legacy `cairn sync --hook` detached, which exported and indexed the session; no 2.4 queue request was created. |

Not covered by this run: Codex, cron scheduling, embedding, desktop, Linux.
