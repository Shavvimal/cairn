# cairn

[![CI](https://github.com/Shavvimal/cairn/actions/workflows/ci.yml/badge.svg)](https://github.com/Shavvimal/cairn/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/Shavvimal/cairn)](https://github.com/Shavvimal/cairn/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

> **cairn** *(n.)* — from Scottish Gaelic *càrn*, "a heap of stones." A pile of stones stacked one at a time to mark a trail, so the next traveler — or you on the way back — never loses the path.

**Local memory for AI coding agents.** Give Claude Code, Codex, and Cursor persistent, searchable context across sessions — all on your machine. Nothing leaves your computer.

```txt
                  .-"""-.
                 /  . .  \
                 \  '-'  /
                  '.___.'
              .-""""""""-.
             / .--""""-.  \
            |  /        \  |
             \ \  .  .  / /
              '.\______/.'
          .-""""""""""""""-.
         /  .-"""""""""-.   \
        |  /   .    .    \   |
        |  |  '-.    .-'  |  |
         \  \    '--'    /  /
          \  '.________.'  /
           '-.__________.-'
     ___________________________
    /  .  .   .   .  .   .  .  . \
   `-----------------------------'
```

cairn exports your AI coding sessions, meetings, and docs to markdown, indexes them with [QMD](https://github.com/tobi/qmd) (local BM25 + vector + LLM-rerank search), and exposes them to Claude Code through a `/recall` skill. Set it up once; every session afterward becomes searchable automatically.

## How it works

```
Sources ──► Exporters ──► Markdown (.context/) ──► QMD index ──► /recall
```

- **Exporters** pull each source into clean markdown: Claude Code & Codex JSONL logs, Cursor's SQLite databases, Granola meeting transcripts (via its API), and service docs mirrored from your repos.
- **QMD** indexes everything locally — BM25 full-text, vector embeddings, and an LLM reranker, all running on-device via `node-llama-cpp`.
- **`/recall`** loads context before you start working, so you don't have to re-explain what you were doing.

Six collections and one search index:

| Collection             | Source                                           |
| ---------------------- | ------------------------------------------------ |
| `claude-code-sessions` | `~/.claude/projects` JSONL transcripts           |
| `codex-sessions`       | `~/.codex/sessions` rollout JSONL                |
| `cursor-sessions`      | Cursor `state.vscdb` SQLite databases            |
| `granola-sessions`     | Granola meeting API (transcripts + AI summaries) |
| `service-docs`         | Markdown docs mirrored from your repos           |
| `notes`                | A local notes directory                          |

## Requirements

- [QMD](https://github.com/tobi/qmd): `npm install -g @tobilu/qmd`
- [uv](https://docs.astral.sh/uv/) (for the `cairn` CLI)
- Claude Code (for the plugin + skills)

## Installation

cairn ships as a Claude Code plugin (skills + a SessionEnd hook) plus a globally installed `cairn` CLI. This repo is **both** the marketplace and the plugin.

```text
/plugin marketplace add shavvimal/cairn   # add the marketplace
/plugin install cairn@cairn               # install skills + hook
/cairn:setup                              # install the CLI, write config, register cron
```

`/cairn:setup` installs the engine globally (`uv tool install git+https://github.com/shavvimal/cairn`), writes a per-user config, exports your sessions, **registers the QMD collections, runs the first embed**, and registers an hourly sync — then verifies a real query returns results. After it, the skills work out of the box; there are no manual `qmd` steps.

Collection registration is automatic and self-healing: every `cairn sync` idempotently runs `qmd collection add` for each enabled collection that has markdown on disk, so sources that produce data later (Cursor/Codex/Granola) register themselves on a subsequent sync — no manual step ever.

> [!NOTE]
> The first embed downloads ~2GB of models (embeddings + reranker + query
> expansion) and takes a few minutes — `/cairn:setup` waits for it. Every run
> after that is incremental — only new or changed chunks are embedded.

### Developing cairn

Clone the repo and `uv pip install -e .`. The editable install finds `cairn.config.json` at the repo root via the dev fallback, so live edits are picked up — no global install.

## Usage

### `/recall` — load context from previous sessions

```text
/recall yesterday              # temporal: scan session history by date
/recall last week
/recall authentication work    # topic: BM25 search across QMD collections
```

### `/search` — fast inline lookup

```text
/search webhook processing            # search all collections
/search analysis pipeline -n 3        # limit results
/search onboarding -c granola-sessions   # specific collection
```

### `/cairn` — drive the CLI (export, list, annotate, resume)

Every windowed source (claude, codex, cursor, granola) shares the same verbs — `export`, `list`, `context`, `note`, `close`, `log` — with `export` defaulting to the last 24h:

```bash
cairn claude export --since 7d   # export the last week of Claude sessions
cairn cursor export --all        # export all Cursor sessions
cairn granola export             # export the last 24h of meetings
cairn claude list --json         # list exported sessions as JSON
cairn claude resume --pick       # resume a session (Claude only)
cairn codex note "progress"      # add a timestamped note
```

Service docs is a verbatim mirror with no `--since`/`--all`:
`cairn service-docs export [--clean]`.

## Configuration

Config is data, not code. `cairn config init` writes `~/.config/cairn/config.json` (XDG) —
the single source of truth for every path and the project catalog. cairn resolves config in
order: `$CAIRN_CONFIG` → `$XDG_CONFIG_HOME/cairn/config.json` → repo root (dev fallback).

**Service docs** are the one collection configured by folder rather than an app store. Point
it at any docs directories you want mirrored and searched:

```bash
cairn config set service-docs.enabled true
cairn config add-service-doc api "~/Code/myapp/api/docs" -d "Backend API docs"
cairn config remove-service-doc api    # drop one later
```

Each collection carries a `sync` block — its on/off switch and policy:

<details><summary>Per-collection <code>sync</code> config</summary>

```json
"collections": {
  "claude-code-sessions": {
    "store": "~/.claude/projects",
    "sync": { "enabled": true, "since": "1d", "on_hook": true }
  },
  "codex-sessions": {
    "store": "~/.codex/sessions",
    "sync": { "enabled": false, "since": "1d" }
  }
}
```

</details>

- `enabled` — `cairn sync` runs only enabled sources. Disabled or absent sources are skipped without crashing the sync.
- `since` — export window (`today` | `yesterday` | `Nd` | `Nw` | `YYYY-MM-DD`, default `1d`).
- `on_hook` — opts a source into the fast `SessionEnd` hook (default: Claude only).

To change later, edit `sync.enabled` per collection and re-run `cairn doctor` — no reinstall.

## Keeping it fresh

`cairn sync` is a single orchestration command; each collection runs as its own subprocess, so one failing source never blocks the others. Two triggers keep context current:

- **SessionEnd hook** (ships with the plugin) — fires when you close a Claude Code session, exporting claude-sessions immediately.
- **Hourly cron** (`cairn cron install`) — catches everything the hook misses: Cursor, Codex, Granola, and service docs.

```bash
cairn sync --hook    # fast: claude-sessions only + qmd update
cairn sync --cron    # full: all exports + qmd update + qmd embed
```

Every export checks whether its source actually changed (mtime/timestamp/hash) before doing work, so a "nothing changed" run is nearly instant. Steps are timed and logged to `.context/logs/context-sync.log`.

## Repository layout

```
src/cairn/                 # the engine (config, schema, renderer, sources, CLI, sync)
src/cairn/__main__.py      # the `cairn` dispatcher
skills/                    # plugin skills (setup, cairn, recall, search)
.claude-plugin/            # plugin.json + marketplace.json (this repo is both)
hooks/hooks.json           # SessionEnd → cairn sync --hook
tests/                     # stdlib unittest suite
cairn.config.example.json  # committed config template
.context/                  # synced data (data_root), git-ignored
```

The engine is a single package (`cairn/`) with a shared markdown renderer, frontmatter round-trip, project/repo catalog, and CLI. Each source is a thin adapter over it — adding a new source is one adapter file plus one `@register(...)` line.
