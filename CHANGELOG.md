# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.6.0]

### Added
- `cairn invoke antigravity`: run Google Antigravity's `agy` CLI headlessly as a peer, the
  way `codex` and `claude` already run. Verified against agy 1.4.6: `-p=<prompt>` with
  `--output-format stream-json`, the conversation id captured from the `init` event,
  `--resume` continuing the same conversation with `--conversation`, large prompts through
  `--input-format stream-json`, and `--json-schema` for structured answers. Profiles map
  to `--mode plan` (review: writes and shell commands deferred), `--mode accept-edits`
  (edit) and `--dangerously-skip-permissions` (full); `--mode` passes a native mode
  directly. An unknown model or a missing login is reported as not retryable. Defaults
  live under `invoke.antigravity` in the config.
- Skill `invoke-antigravity` for both hosts.
- Source adapter `antigravity` (`cairn antigravity export|list|context|note|close|log|resume`,
  collection `antigravity-sessions`): every agy conversation exports from the JSONL
  transcript agy keeps under `<app data>/brain/<id>/.system_generated/logs/`, with the
  title, workspace, run status and lineage from `conversation_summaries.db`. User prompts
  lose agy's wrapper blocks, thinking is folded, tool results are paired with their calls,
  system messages render as `### System`, subagent conversations become
  `antigravity-subagent` documents in the parent's folder, and the agent's artifacts are
  listed. Peer runs (`cairn invoke antigravity`) now export and link the conversation like
  Codex and Claude peers.
- `cairn hook install|uninstall|status --harness antigravity`: a cairn-owned `Stop` hook in
  the global `~/.gemini/config/hooks.json` (agy has no SessionEnd; `Stop` fires per turn).
  `cairn hook handle --harness antigravity` queues the export and always answers `{}` so it
  never blocks agy. Link references accept every registered session source.
- `invoke.antigravity.billing` (`route`, `project`, `location`): which credits a headless
  `agy` run bills. `inherit` (default) passes the host environment through; `plan` strips
  `AGY_ADC_AUTH` so the consumer plan quota pays; `gcp` sets `AGY_ADC_AUTH=true` and the
  `GOOGLE_CLOUD_QUOTA_PROJECT` / `GOOGLE_CLOUD_LOCATION` exports, refuses to spawn (exit 2)
  unless the Application Default Credentials file and a quota project exist, and after the
  run reads agy's own log (`--log-file`, kept as `peer-NN.log`) to record the auth method,
  quota project and request projects as `billing.observed`; a mismatch is a `billing:`
  warning. `cairn invoke antigravity --billing ROUTE` overrides one round,
  `cairn config set invoke.antigravity.billing.route gcp` sets the machine default, and
  `cairn doctor` reports whether the `gcp` route is ready.

### Changed
- Publication steps are decided per peer: a peer whose collection is not configured has no
  session document to export or link, so those steps are not applicable to its rounds
  (one warning in the result) instead of pending forever. With a known caller the exchange
  document still lands in the caller's folder and is indexed; without one, only the answer
  is kept under the run directory.
- Harness adapters own the bytes a large prompt takes on the peer's stdin
  (`stdin_payload`), and the recorded argv masks a prompt glued to a flag.

## [2.5.1]

### Fixed

- Claude Code exports `CLAUDE_CODE_SESSION_ID` to every tool process, not
  `CLAUDE_SESSION_ID` (that name is only a template variable substituted into skill
  text). cairn read the wrong one, so under Claude Code `cairn invoke` never detected
  its caller without `--parent` (the exchange document then landed in the Codex session
  folder with no `links:`), and `cairn claude note|close|log` could not infer the
  current session. The Claude adapter now reads `CLAUDE_CODE_SESSION_ID` first and
  still accepts a hand-set `CLAUDE_SESSION_ID`; conflicting values are refused like
  the Codex pair.
- The test suite scrubs host identity variables, so running it from inside a Claude
  Code session no longer leaks that session into subprocess tests.

## [2.5.0]

### Upgrading from 2.4.0

Upgrade the engine (`uv tool upgrade cairn`) and the plugin, then run
`cairn claude export --all` and `cairn codex export --all` once. The export schema is now 2:
the full run re-renders every session document with the new head fields and, for the first
time, exports subagent transcripts as child documents. On a machine with a year of history
this is a one-time cost of a few minutes plus the embedding of the new child documents on
the next `cairn sync --cron`. Windowed cron runs never re-export old documents.

### Added
- `cairn invoke codex|claude`: run the other agent headlessly from either host, stream its
  events to a per-thread log, capture the real thread id, continue the same thread later
  with `--resume`, read progress mid-run with `cairn invoke status`, cancel with
  `cairn invoke cancel`, and finish pending publishing with `cairn invoke publish`. Defaults
  per peer (model, effort, profile, timeouts) live in the config `invoke` block.
- Every peer run exports the peer session at once, writes `links:` on both session
  documents, and records an exchange document (prompt, progress, answer and result per
  round) in the calling session's folder, so `/search` and `rg` find it (session recall
  lists the peer session; exchange documents stay out of the session timeline by design).
- Health for long-running work: execution state, activity with age, and outcome are
  reported separately; an idle watchdog kills a hung run; one invocation per thread is
  enforced; a runner killed mid-run leaves an orphaned round that `status` adopts.
- `cairn agents list|wait`: one view over the peer runs and the Claude and Codex subagents
  a session spawned, and a blocking wait for a whole fan-out.
- Claude Code subagent transcripts (`<session>/subagents/agent-*.jsonl`, including
  workflow agents) export as `claude-subagent` documents in the parent's folder with the
  agent's type, description, tool use id, spawn depth, request shape, model and lineage;
  parents gain a `## Subagents` index; child-to-parent messages render as Agent turns.
- Codex subagent rollouts export as `codex-subagent` documents with parent thread, role,
  nickname, path and depth; rollouts carry `originator`, `thread_source`, `effort`,
  `sandbox` and `approval_policy`; `custom_tool_call` records render like function calls;
  inter-agent messages render as Agent turns marked encrypted.
- `cairn recall list --include-subagents`; JSON rows carry `type` and `parent`.
- Skills `invoke-codex` and `invoke-claude` for both hosts, `references/peer-review.md`
  (gate prompts, critical evaluation, the bounded convergence loop), and peer-run recipes
  in the harness references.
- `links` is a preserved frontmatter field.
- `cairn doctor` probes the configured QMD: `qmd --version` (warns below the tested 2.5.3
  or on a prerelease) and `qmd status` as a runtime check (a binary that crashes opening
  its index is now a critical failure). The embeddings line reports documents, vectors and
  pending documents. `cairn doctor --strict-index` fails unless the index is readable and
  fully embedded; an empty index passes but says nothing can be verified.
- Full sync logs a `[qmd-verify]` line with the index-wide counts after `qmd embed`, and
  a `failed steps:` line naming every failed step before `=== FAILED ===`.

### Fixed
- Sync reported `[qmd-embed] ok` when QMD exited 0 with failed chunks (seen: 48,219 of
  131,291 chunks after QMD's 30-minute embed session expired). The whole embed output is
  now checked for failed-chunk reports and chunk errors, and the run fails. A clean exit
  logs `[qmd-embed] exited 0`; `[qmd-verify]` gives the verdict. Pending documents after
  the embed fail the run as `INCOMPLETE` (`failed steps: qmd-verify (incomplete)`), and a
  non-empty index with no vectors, an unreadable `qmd status`, or a `Pending` line in an
  unknown shape also fails it.
- Two `cairn sync` runs could overlap (an hourly cron run during a manual one) and
  interleave their log blocks. A per-data-root sync lock now lets one run at a time. The
  other records its mode under `<data_root>/sync-requests/`, logs one `skipped` line and
  exits 0; the running sync then runs, once, the stages the request needs that it did not
  run (a `--cron` request after a legacy `--hook` run gets the worker retry, the other
  exports, update, embed and verify; a full request after a full run gets a hook-worker
  retry). Requests that arrive during that pass wait for the next invocation.
- A timed-out sync step, hook export or `qmd update` killed only its direct child, so the
  Node grandchild behind the `qmd` launcher could keep running outside the index lock. The
  whole process group is now killed (also on Ctrl-C). A timeout keeps the partial stdout
  and stderr even when a descendant escaped the group, and process cleanup is bounded
  (`cleanup incomplete` is reported instead of an unbounded wait).
- Full sync no longer runs `qmd embed` after `qmd update` failed.
- The test suite wrote catalogs and lock files into the real home (`~/.cache/cairn`,
  `~/.local/state/cairn`); CI now fails if it does.
- Codex: a forked subagent rollout carries two `session_meta` records; the parser let the
  last one win, so the child was parsed as its parent and dropped by the fragment dedupe.
  The first record now wins and the replayed parent history is cut. On the author's
  machine this recovers 100 of 108 subagent rollouts.
- Codex: `custom_tool_call` and `custom_tool_call_output` records were silently skipped.
- Claude: messages a subagent sent to its parent were dropped as meta records.

### Changed
- Export schema 2; see Upgrading.
- `cairn doctor` no longer says "All critical checks passed." without a qualifier when the
  semantic index is empty, unreadable or has pending documents.
- Setup verifies keyword, vector and hybrid search separately and documents QMD
  compatibility and recovery without touching QMD's database.
- `cairn resume` refuses a subagent transcript and names the parent to resume instead.

## [2.4.0]

### Upgrading from 2.3.0

Run the [setup skill](skills/setup/SKILL.md) again to follow its
[update workflow](skills/setup/references/updating.md). It upgrades the engine first,
then the plugin, using the relevant host adapter. Afterwards it runs
`cairn hook install --harness claude` (or `codex`) so
the SessionEnd hook records the engine's interpreter and config. Until then the
bundled launcher falls back to `cairn` on PATH. If the plugin updates before the
engine, the launcher detects the 2.3.0 engine, runs the legacy `cairn sync --hook`
detached, and prints the upgrade command to the hook log.

Keep the existing Markdown store and QMD database; no full rebuild is required.
Legacy metadata remains readable and upgrades on export when native data is available.
For older history, optionally export each enabled source with `--all`, then run
`cairn sync --cron`. Rewritten documents need their embeddings refreshed; unchanged
documents do not. See the setup skill's update workflow for preservation and
unavailable-history behavior.
Reinstall hooks and the existing cron entry to record the complete QMD environment.

### Added
- Hooks and cron retain the selected QMD configuration, cache and database paths,
  including effective defaults, instead of inheriting a different host index.
- Index writers coordinate on canonical configuration and database resources, even
  when environment aliases or different cache directories refer to shared state.
- Collection discovery and registration time out after 30 seconds; failed hook jobs
  release their locks and can be retried.
- Legacy Claude `agent-*` sidechains are intentional skips, not export failures.
- Legacy date-only records retain their recorded day label across machine timezones.
- Structurally invalid catalog caches rebuild from Markdown instead of crashing.

- Shared Claude/Codex harness adapters, portable packaging, explicit Codex onboarding,
  and discovery-based lifecycle registration with trust diagnostics.
- Durable session exports queued to a detached worker; failures get at most three attempts,
  index updates are serialised, and embedding stays out of the callback. Completed and
  disabled requests and exhausted failures are pruned seven days after becoming terminal.
  `cairn hook retry SOURCE:FULL_SESSION_ID` explicitly resets a failed request's attempt
  budget; `cairn hook status`
  shows every actionable request plus the ten most recent completed ones (`--all`).
- Recall of exported conversations across sources with source-qualified IDs, UTC activity
  filtering, schema migration, bounded expansion, and safe concurrent annotations.
- Native resume command selection, host-aware diagnostics, and shared skill references.
- A stat-validated session catalog (`$XDG_CACHE_HOME/cairn/catalogs/`) so session lookup,
  `list` and `recall` cost one directory walk instead of parsing every exported file.
  The catalog is a derived cache: entries are trusted only while the file's
  mtime/ctime/size/inode match, and a missing or corrupt catalog is rebuilt.
- A POSIX `sh` hook launcher (`hooks/launch.sh`) that never depends on the macOS
  `/usr/bin/python3` stub.
- Resource locks under `~/.local/state/cairn/locks/` keep lock files out of Markdown
  and QMD directories while coordinating shared canonical paths across profiles.
- Community health files: `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue and pull
  request templates, and Dependabot configuration.
- CI: a `version-bump` gate that requires `__version__` to change when
  `src/cairn/` is modified.
- Live verification and outstanding desktop/skill-evaluation acceptance recorded in
  `docs/codex-acceptance.md`.

### Fixed
- Hook and cron index updates create the QMD database and config directories they
  pin, so the first job on a machine where QMD has never run no longer fails.
- A code fence left open in `## My Notes` no longer swallows the conversation that
  follows it, which made every export append another copy of the conversation.
- Granola notes and summaries containing `## Conversation` no longer cause
  handwritten `## My Notes` content to be discarded on re-export.
- Recall expansion preserves embedded CRLF line endings so message limits remain
  accurate for existing exports. Annotation edits preserve transcript bytes and
  their turn lengths as well.

### Changed
- The setup skill handles existing installations through a dedicated update workflow;
  the README points users there for upgrades and optional historical metadata backfill.
- Exported frontmatter records `activity_dates` (unique UTC days) instead of one line
  per message timestamp; the full timestamps remain in each turn header. Files written
  by the pre-release branch with `activity_timestamps` are still read.

## [2.3.0]

### Added
- First-class ripgrep search in the skills. `/search` gets an rg engine (`--exact`,
  or chosen automatically for identifiers, error strings, paths, session IDs and
  regex), and `/recall` topic mode runs an exact-match rg pass next to the BM25
  searches. rg reads the markdown directly, so it also finds files that QMD has not
  indexed yet.
- The skills resolve each collection's directory from `qmd collection show`, so rg
  also covers collections outside `data_root`. No path is hardcoded.
- Output guardrails for rg in both skills: list files first (`-l`, newest first via
  `--sortr modified`, capped with `head`), then print lines for chosen files only with
  `-M 300 -m 5`. A session transcript can hold a single line of several megabytes.
- `cairn doctor` checks for `rg` on PATH. A missing rg is a warning, never a failure.
- `/cairn:setup` preflight reports `rg` and offers to install it (optional).

## [2.2.0]

### Added
- The plugin manifest version is now locked to the Python package version:
  `.claude-plugin/plugin.json` declares `version` equal to `cairn.__version__`,
  enforced by a test (and `release.yml`) so the plugin and engine can never drift
  apart in the repo. One release is one version across both artifacts.
- `cairn doctor` detects plugin/engine drift: given the plugin manifest
  (`--plugin-manifest`, or `$CLAUDE_PLUGIN_ROOT` when run from the plugin) it compares
  the installed engine version to the plugin version and tells you to
  `uv tool upgrade cairn` (or `/plugin update`) when they differ.

### Changed
- The CI `version-bump` gate now fires for any user-facing change - the engine
  (`src/cairn/`) **or** the plugin (`skills/`, `hooks/`, `.claude-plugin/`,
  `commands/`, `agents/`) - so plugin-only changes also bump the shared version (and
  the plugin cache actually refreshes for users).
- `service-docs --version` now reports the cairn package version instead of a stale
  hardcoded `1.0.0`.

## [2.1.0]

### Added
- Self-healing QMD collection registration: `cairn sync` now idempotently runs
  `qmd collection add` for every enabled collection that has markdown on disk
  (new `qmd-collections` step, before `qmd update`, in all modes). A fresh install
  no longer needs the manual `qmd collection add` step, and sources that produce
  data later (Cursor/Codex/Granola) register automatically on a subsequent sync.
- `cairn doctor` now checks that enabled collections are registered in qmd and that
  the index has embedded vectors - so a green doctor reflects a searchable index,
  not just wired-up plumbing. Both are warnings (a fresh install is legitimately
  empty until the first embed).
- `cairn.qmd.list_collections()` and `cairn.qmd.ensure_collection()` helpers.
- `cairn config add-service-doc NAME PATH [-d DESC]` and `cairn config
  remove-service-doc NAME` to configure the `service-docs` collection's folders from
  the CLI (previously `service_sources` could only be set by hand-editing JSON). The
  add command validates the path exists and clears the bundled placeholder on first
  use. `cairn config show` now lists each service-docs folder.
- `/cairn:setup` now gives service docs their own question (separate from the session
  sources) and prompts for the folders to mirror, instead of bundling service-docs on
  the same checkbox as granola.

### Changed
- The first `qmd embed` during `cairn sync --cron`/`--all` may download ~2GB of
  models; its timeout is raised from 600s to 3600s so an unattended first run can
  finish.
- `/cairn:setup` now bootstraps collections and runs the first embed (watched via
  the Monitor tool) and verifies a real query returns results before declaring
  success - it no longer stops at a green `cairn doctor`.

## [2.0.0]

- Repackaged as an installable CLI (`src/` layout, hatchling) and a Claude Code
  marketplace plugin (skills + SessionEnd hook).
