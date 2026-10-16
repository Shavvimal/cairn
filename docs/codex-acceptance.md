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
| Automated engine checks | `make check`, including ruff, mypy and unittest; explicit formatting check. Full counts recorded in the final verification update. |
| Python compatibility | Tests passed with Python 3.11, 3.12 and 3.13 in isolated uv environments. |
| Portable schema | Root manifest validated with the published Agent Plugins 1.0.0 JSON Schema. All three manifests/version and cache-relative resources checked in tests. |
| Codex trust | `/hooks` showed one user SessionEnd fallback with a three-second timeout. Reviewed and trusted in the TUI, without hook-trust bypass for the lifecycle test. `hooks/list` then reported trusted. |
| Actual Codex shutdown | TUI session `01a145b7-2f15-7452-9196-436717c9d691` replied with “Cairn quince orchard lifecycle marker.” Normal `/exit` returned successfully. Durable request reached `completed`; `qmd search quince --json` returned the Codex export, without cron. |
| Actual Claude shutdown | Authenticated session `aafea0b1-4948-45be-af59-e4a5b13df071` emitted SessionEnd, exported and indexed successfully. Queue-to-completion was about 0.44 seconds. `qmd search orchard --json` returned its export. |
| Cross-host skills | Codex used the installed recall skill to list/expand the Claude session. Claude used the installed recall skill to expand the Codex quince session and returned the correct Codex resume argv. |
| Initial embeddings | Real `qmd embed` completed with exit 0 and vectors for the isolated corpus. A nonfatal Metal build warning preceded successful embedding. |
| Native Codex resume | `codex resume 01a145b7-2f15-7452-9196-436717c9d691` opened the prior conversation in the TUI and answered a new turn under that identity. |
| Shared store / concurrency | Synthetic cross-source, UTC, gaps, migration, collisions, bounded expansion, annotation races, failed QMD/retry, durable generations and detached worker tests pass. |

## Acceptance still requiring evidence

- GitHub-ref clean installation, update/removal, final installed engine checks and additional
  live mutations/resume checks are being completed on the candidate branch; update this
  record with their results before review.
- **Codex desktop:** no Codex desktop application is installed in this environment. Desktop
  package loading, onboarding, hook trust, lifecycle and resume UI remain unverified.
- **Full guided setup:** CLI setup components and real embedding/search were exercised;
  the complete conversational onboarding and real hourly crontab installation were not.
- **Full skill evaluation matrix:** cross-host recall ran in both hosts, but all four skills
  across missing inputs/prerequisites, unrelated activation, silent/failed/empty embedding,
  no-result and ordinary-conversation scenarios have not all been exercised live.
- **Other environments:** Linux host lifecycle, other client versions, Codex archive/delete
  and idle-timeout events, and desktop terminal launch actions remain unverified.

The PR must retain these limitations until corresponding host evidence exists. Packaging,
a green doctor, generated resume argv, and a direct handler invocation do not substitute
for host lifecycle or interactive onboarding evidence.
