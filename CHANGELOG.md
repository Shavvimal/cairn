# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Community health files: `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue and pull
  request templates, and Dependabot configuration.
- CI: a `version-bump` gate that requires `__version__` to change when
  `src/cairn/` is modified.

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
