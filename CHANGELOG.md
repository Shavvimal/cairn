# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
