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

cairn exports your AI coding sessions, meetings, and docs to markdown, indexes them with [QMD](https://github.com/tobi/qmd) (local BM25 + vector + LLM-rerank search), and exposes them to Claude Code and Codex through shared recall and search skills. Set it up once; every session afterward becomes searchable automatically.

> 📖 **[Read the blog post](https://shav.dev/blog/i-open-sourced-my-coding-agents-memory)** — *I Open-Sourced My Coding Agents' Memory*: why cairn exists, and the three months of tools breaking that shaped it.

## How it works

```
Sources ──► Exporters ──► Markdown (.context/) ──► QMD index ──► /recall
```

- **Exporters** pull each source into clean markdown: Claude Code, Codex and Antigravity (agy) JSONL transcripts, Cursor's SQLite databases, Granola meeting transcripts (via its API), and service docs mirrored from your repos.
- **QMD** indexes everything locally — BM25 full-text, vector embeddings, and an LLM reranker, all running on-device via `node-llama-cpp`.
- **`/recall`** loads context before you start working, so you don't have to re-explain what you were doing.

Seven collections and one search index:

| Collection             | Source                                           |
| ---------------------- | ------------------------------------------------ |
| `claude-code-sessions` | `~/.claude/projects` JSONL transcripts, subagent transcripts, peer exchanges |
| `codex-sessions`       | `~/.codex/sessions` rollout JSONL, subagent rollouts, peer exchanges |
| `antigravity-sessions` | `~/.gemini/antigravity-cli` agy conversation transcripts, subagent conversations |
| `cursor-sessions`      | Cursor `state.vscdb` SQLite databases            |
| `granola-sessions`     | Granola meeting API (transcripts + AI summaries) |
| `service-docs`         | Markdown docs mirrored from your repos           |
| `notes`                | A local notes directory                          |

## Requirements

- [QMD](https://github.com/tobi/qmd): `npm install -g @tobilu/qmd`
- [uv](https://docs.astral.sh/uv/) (for the `cairn` CLI)
- Optional: [ripgrep](https://github.com/BurntSushi/ripgrep) (`brew install ripgrep`) for exact-match and regex search in `/search` and `/recall`
- Claude Code or Codex CLI (for the plugin + skills). See [acceptance evidence](docs/codex-acceptance.md) for tested clients and remaining desktop checks.

## Installation

Cairn ships one set of skills, host-specific lifecycle adapters, and a globally installed `cairn` CLI. This repo is **both** the marketplace and the plugin.

For Codex CLI (tested on 0.160.0; hook discovery also checked on 0.163.0):

```sh
codex plugin marketplace add shavvimal/cairn --ref main
codex plugin add cairn@cairn
```

Restart Codex, select **cairn:setup** in `/skills` or the `$` picker, and explicitly run it.
The plugin's onboarding action points to this same setup skill. Setup is never implicitly
invoked. Review/trust the Cairn SessionEnd command in `/hooks` when prompted.

For Claude Code:

```text
/plugin marketplace add shavvimal/cairn   # add the marketplace
/plugin install cairn@cairn               # install skills + hook
/reload-plugins                           # load the freshly installed skills
/cairn:setup                              # install the CLI, write config, register cron
```

`/cairn:setup` installs the engine globally (`uv tool install git+https://github.com/shavvimal/cairn`), writes a per-user config, exports your sessions, **registers the QMD collections, runs the first embed**, and registers an hourly sync — then verifies a real query returns results. After it, the skills work out of the box; there are no manual `qmd` steps.

Collection registration is automatic and self-healing: every `cairn sync` idempotently runs `qmd collection add` for each enabled collection that has markdown on disk, so sources that produce data later (Cursor/Codex/Granola) register themselves on a subsequent sync — no manual step ever.

> [!NOTE]
> The first embed downloads ~2GB of models (embeddings + reranker + query
> expansion) and takes a few minutes — `/cairn:setup` waits for it. Every run
> after that is incremental — only new or changed documents need embedding.

### Updating cairn

Run the [setup skill](skills/setup/SKILL.md) again and ask it to update Cairn:

- **Claude Code:** `/cairn:setup`
- **Codex:** select **cairn:setup** in `/skills` or the `$` picker and explicitly run it.

Setup detects existing installations and follows its [update workflow](skills/setup/references/updating.md):
upgrade the engine and plugin, preserve your configuration, refresh hooks and any existing
cron entry, run incremental sync, and verify search. It also walks you through required
host restarts and hook trust checks.

For a manual Codex update from your terminal:

```sh
uv tool upgrade cairn
codex plugin marketplace upgrade cairn
codex plugin add cairn@cairn
cairn hook install --harness codex
```

Then restart Codex and review changed hooks.

For a manual Claude Code update:

```text
/plugin marketplace update cairn   # refresh the marketplace so the new version is visible
/plugin update cairn@cairn         # install the new plugin version
/reload-plugins                    # load the updated skills
```

Then upgrade the engine with `uv tool upgrade cairn` and run
`cairn hook install --harness claude`. Without the marketplace refresh, `/plugin update`
reinstalls the version the marketplace last saw. The guided workflow also refreshes
existing cron entries and verifies incremental sync and search.

If your installed setup skill still says “fresh setup only”, refresh the plugin using
the [Claude](references/harnesses/claude.md) or [Codex](references/harnesses/codex.md)
instructions, then reload/restart the host to load the updated skill.

**Existing stores need no conversion or full re-index for 2.4.0.** Keep your Markdown
and QMD database. The setup skill explains optional historical metadata backfill and
its re-embedding cost; there is no separate “v2 store” to create.

### Disable or remove

Disable a source's automatic synchronization with `cairn config set codex.on_hook false`
(or `claude.on_hook false`); `enabled false` also prevents source export. Codex fallback
hooks additionally check that the plugin remains enabled and installed.

```sh
cairn hook uninstall --harness codex  # only Cairn's registered Codex handlers
codex plugin remove cairn@cairn
codex plugin marketplace remove cairn
```

Claude removal uses `/plugin uninstall cairn@cairn`. Shared exported data, annotations,
QMD collections, and the other host's launcher/configuration are preserved. To remove all
automatic maintenance intentionally, also run `cairn cron uninstall` and `cairn hook uninstall`.
Public-directory submission is deferred; this release is distributed through GitHub.

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
/search ECONNRESET --exact            # exact string or regex, via ripgrep
```

`/search` uses QMD BM25 for topics and ripgrep for literal strings (identifiers, error text, paths, session IDs) and regex. ripgrep reads the markdown directly, so it also finds sessions exported after the last index update. The skill resolves each collection's directory from `qmd collection show`, so collections outside cairn's data root (such as your own notes folder) are searched too.

### `/cairn` — drive the CLI (export, list, annotate, resume)

Every windowed source (claude, codex, cursor, granola) shares the same verbs — `export`, `list`, `context`, `note`, `close`, `log` — with `export` defaulting to the last 24h:

```bash
cairn claude export --since 7d   # export the last week of Claude sessions
cairn cursor export --all        # export all Cursor sessions
cairn granola export             # export the last 24h of meetings
cairn claude list --json         # list exported sessions as JSON
cairn resume codex:FULL_ID        # print a native resume command (Claude or Codex)
cairn claude resume --pick       # legacy interactive Claude picker
cairn codex note "progress"      # add a timestamped note
```

Service docs is a verbatim mirror with no `--since`/`--all`:
`cairn service-docs export [--clean]`.

### `/invoke-codex`, `/invoke-claude` and `/invoke-antigravity` — ask another agent

Run another agent headlessly from either host, for an adversarial review, a plan review or
an independent pass, and keep the whole exchange searchable:

```bash
cairn invoke codex --json --prompt-file brief.md --effort high   # background it; --json gives one result line
cairn invoke status <thread-id>                             # health line, progress, answer so far
cairn invoke tail <thread-id>                               # one line per tool call, until the final event
cairn invoke wait <thread-id> --json                        # block until the round exits; its result line
cairn invoke codex --resume <thread-id> --prompt "…"         # continue the same thread, days later too
cairn invoke claude --prompt "…" --profile review            # the mirror direction
cairn invoke antigravity --prompt "…" --model gemini-3.1-pro-high   # Google Antigravity (agy), a Gemini opinion
cairn agents list                                           # peer runs and subagents of this session, with health
cairn agents wait --session claude:<id>                     # one completion notice for a whole fan-out
```

Each run captures the peer's real thread id, streams its events to a per-thread log under
`<data_root>/logs/invoke/`, exports the peer session at once, writes `links:` on both
session documents, and records an exchange document (prompt, progress, answer and result
per round) in the calling session's folder. Profiles `review` (read-only), `edit` and
`full` map to each peer's native sandbox, permission or mode flags, and the record stores the
exact flags that ran. `agy` must be logged in once interactively; its conversation is
exported like any other session, linked to the caller, and resumable with
`cairn resume antigravity:<id>`. Which
credits a headless `agy` run bills is a per-machine choice (`invoke.antigravity.billing`):
`inherit` the host environment, force the consumer `plan` quota, or `gcp` (Vertex AI through
Application Default Credentials on a named quota project, checked before the spawn and read
back from agy's log afterwards). `cairn invoke antigravity --billing ROUTE` overrides one round. The doctrine for adversarial review and the bounded debate loop is
in [references/peer-review.md](references/peer-review.md); the design is in
[docs/agent-exchange-plan.md](docs/agent-exchange-plan.md).

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
- `on_hook` — explicit false opts a source out of its host's SessionEnd hook. Each host defaults to exporting its own session. Legacy `sync --hook` retains the Claude-default policy.

To change later, edit `sync.enabled` per collection and re-run `cairn doctor` — no reinstall.

## Keeping it fresh

`cairn sync` is a single orchestration command; each collection runs as its own subprocess, so one failing source never blocks the others. Two triggers keep context current:

- **SessionEnd hook** — queues the originating Claude or Codex session and starts a detached worker. Export, collection registration and `qmd update` run after the callback returns. Embeddings stay out of this path. Antigravity has no SessionEnd; its `Stop` hook (end of every turn) does the same through `cairn hook handle --harness antigravity`, which always answers `{}` so it never blocks agy.
- **Hourly cron** (`cairn cron install`) — retries failed work, exports other enabled sources, and maintains embeddings.

Configure with `cairn hook install --harness claude|codex|antigravity`. The Antigravity
installer writes one named hook (`cairn`) into the global `~/.gemini/config/hooks.json`,
shared by the agy CLI, the IDE and the Antigravity 2.0 app; `--grant-commands` also writes
the allow rules (read-only and test shell commands) and deny rules (cloud, database, network,
interpreters, history-moving git, dotenv files) a headless run needs into agy's
`settings.json`, printing every rule it adds. The Codex installer inspects
actual hook discovery. Codex 0.160.0/0.163.0 currently expose portable skills/onboarding
but omit portable hooks; Cairn installs a narrowly owned user hook as a fallback. If a
client discovers the bundled hook, setup removes the fallback instead. Unrelated host
hooks are preserved. Trust is still the host's decision; inspect `/hooks`.

Codex SessionEnd occurs on normal shutdown, archive/delete of an open conversation, or
its idle-session timeout; switching tabs does not immediately end the session. Check
`cairn hook status --harness codex` for actual registration/trust and separate queue
completion. A completed request plus a query returning the session verifies indexing.
Failed requests get at most three attempts across hook workers and cron. After that,
`cairn hook status` reports `exhausted` with the error, retained for seven days.
After fixing the cause, run `cairn hook retry SOURCE:FULL_SESSION_ID` to start a fresh
attempt budget. A new SessionEnd event also creates a fresh request. A failed spawn returns an
error even though the request was saved. This POSIX worker uses flock and detached sessions;
no new Windows support is claimed.

```bash
cairn sync --hook    # fast: claude-sessions only + qmd update
cairn sync --cron    # full: all exports + qmd update + qmd embed
```

One `cairn sync` runs per data root at a time. A second one (an hourly cron run that
overlaps a manual run, say) records its mode under `<data_root>/sync-requests/`, logs one
`skipped: another cairn sync is running` line and exits 0. Before the running sync exits,
it runs once the stages the skipped request needs that it did not run itself (for example
the embed a `--cron` request needs after a legacy `--hook` run). A step that times out takes its whole process group with it, so a `qmd` wrapper's
Node child cannot keep running outside the index lock. When a run fails, the log names the
failed steps on a `failed steps:` line just before `=== FAILED ===`.

QMD exits 0 even when embedding chunks fail. Full sync reads the whole `qmd embed` output
and fails on reported chunk failures, then logs a `[qmd-verify]` line with the index-wide
documents, vectors and pending documents from `qmd status`. Pending documents left after
the embed fail the run as `INCOMPLETE` (`failed steps: qmd-verify (incomplete)`), which is
unfinished work, not corruption. The embed step logs `exited 0`; `[qmd-verify]` gives the
verdict. QMD 1.1.5 and 2.5.3 cap one embed session at 30 minutes, so a large backlog takes
several runs. Completed documents keep their vectors, but QMD 2.5.3 removes the vectors of
a partly embedded document, so that document restarts on the next run. Check progress with
the pending count in `cairn doctor`: if it does not fall across runs, a document may never
fit in one session, so investigate instead of retrying. BM25 keyword search works as soon as a session is exported and `qmd update`
runs (the SessionEnd hook does both); semantic search waits for the next full sync.

`cairn doctor` probes the configured QMD (`--version`, then `status` as a runtime check)
and reports pending embeddings. `cairn doctor --strict-index` fails unless the index is
readable and fully embedded; setup uses it. cairn is tested with QMD 2.5.3; a newer
version is judged by the same runtime and output checks, not by its number.

Recall uses the shared exported Markdown store, independent of the active harness:

```sh
cairn recall list yesterday --all-projects --refresh --json
cairn recall list "last week" --source codex --project /absolute/project
cairn recall expand codex:FULL_ID --max-msgs 30 --max-chars 20000
```

Dates are UTC and match recorded activity, not every day between start and finish.
Listing defaults strictly to the current directory; use `--all-projects` to broaden it.
Legacy date-only records are labeled; unknown counts do not exclude meetings. Unique
prefixes are accepted, collisions fail with source-qualified choices. `--refresh` exports
without QMD and returns failure if refresh failed. Schema upgrades preserve annotations
even when native mtime is unchanged; unavailable native data leaves old exports intact.

Every export checks whether its source actually changed (mtime/timestamp/hash) before doing work, so a "nothing changed" run is nearly instant. Steps are timed and logged to `.context/logs/context-sync.log`.

Everything a session spawned lives in one folder named after its document,
`<collection>/<project>/<date>-<shortid>/`: subagent transcripts as `agent-<id>.md`
(`type: claude-subagent` or `codex-subagent`, hidden from `list`, shown by
`cairn recall list --include-subagents`) and peer exchanges as `exchange-<peer>-<short>.md`.
The parent document carries a `## Subagents` index. Export windows use the newest mtime
across a parent and its children, so a background agent that finishes after the parent's
SessionEnd is still picked up. After upgrading to 2.5.0, run `cairn claude export --all` and
`cairn codex export --all` once to render the new schema and the child documents.

Session lookup, `list` and `recall` read a per-collection catalog under
`$XDG_CACHE_HOME/cairn/catalogs/` rather than parsing every exported file. The markdown
stays the only source of truth: an entry is trusted only while the file's mtime, ctime,
size and inode still match, changed files are re-parsed, and a missing or corrupt catalog
is rebuilt from the files. On a store of 8,000 Claude sessions a lookup drops from about
a second to under 100 ms. The catalog never lives inside the corpus, so a git-tracked
data root does not see it.

Locks live under `~/.local/state/cairn/locks/`, outside the corpus and QMD directories.
Their identities use canonical resource paths, so different config/cache environments
still coordinate on shared files. Lock files remain in place to keep waiting writers
on the same inode.

## Repository layout

```
src/cairn/                 # the engine (config, schema, renderer, sources, CLI, sync)
src/cairn/__main__.py      # the `cairn` dispatcher
skills/                    # plugin skills (setup, cairn, recall, search, invoke-codex, invoke-claude, invoke-antigravity)
.claude-plugin/            # plugin.json + marketplace.json (this repo is both)
plugin.json                # portable manifest (authoritative OpenAI settings)
.agents/plugins/           # Codex GitHub marketplace
.codex-plugin/             # generated compatibility manifest
references/harnesses/      # shared skill execution contract + host tool references
references/peer-review.md  # adversarial review doctrine shared by the invoke skills
docs/                      # design plans, research and acceptance evidence
hooks/hooks.json           # Claude SessionEnd
hooks/codex.json           # Codex SessionEnd (explicit selection)
hooks/antigravity.json     # Antigravity Stop (reference shape; `cairn hook install` writes the real one)
tests/                     # stdlib unittest suite
cairn.config.example.json  # committed config template
.context/                  # synced data (data_root), git-ignored
```

The engine is a single package (`cairn/`) with a shared markdown renderer, frontmatter round-trip, project/repo catalog, and CLI. Each source is a thin adapter over it — adding a new source is one adapter plus a lazy descriptor in `registry.py`. Source parsing stays independent of harness execution.

See [implementation and live acceptance evidence](docs/codex-acceptance.md) for verified
behavior and incomplete criteria. Packaging alone does not establish lifecycle support.
