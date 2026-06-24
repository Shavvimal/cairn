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

## [2.0.0]

- Repackaged as an installable CLI (`src/` layout, hatchling) and a Claude Code
  marketplace plugin (skills + SessionEnd hook).
