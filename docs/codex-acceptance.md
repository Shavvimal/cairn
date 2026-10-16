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

## Acceptance still requiring evidence

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

These gaps remain open acceptance criteria. Packaging, a green doctor, generated resume
argv, and a direct handler invocation do not substitute for the missing host evidence.
OpenAI public-directory submission remains deferred.
