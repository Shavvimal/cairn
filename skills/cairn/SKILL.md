---
name: cairn
description: Exports, lists, annotates, and resumes local AI coding sessions (Claude Code, Codex, Cursor) plus Granola meetings and service docs to searchable markdown via the cairn CLI. Use when the user says "sync sessions", "export sessions", "export codex", "export cursor", "export granola", "resume session", "add session note", "close session", "log session", "sync docs", "sync service docs", or "meeting transcripts". Covers per-source export/list/context/note/close/log, the Claude-only resume, the top-level sync/doctor/config/cron commands, and the config-driven enablement model. Requires the cairn CLI - install via /cairn:setup.
allowed-tools: Bash, Read
---

# Cairn

Cairn syncs local AI activity to markdown for observability and QMD search. One CLI,
`cairn <source> <verb>`, drives five sources:

| Source         | Collection              | What it captures                     |
| -------------- | ----------------------- | ------------------------------------ |
| `claude`       | `claude-code-sessions/` | Claude Code conversations (+ resume) |
| `codex`        | `codex-sessions/`       | Codex CLI rollouts                   |
| `cursor`       | `cursor-sessions/`      | Cursor IDE composer sessions         |
| `granola`      | `granola-sessions/`     | Granola meeting notes + transcripts  |
| `service-docs` | `service-docs/`         | Verbatim mirror of repo docs         |

Install first with `/cairn:setup`. Run `cairn help` for the canonical overview and
`cairn doctor` to check install, config, qmd, cron, and PATH.

**Authoritative args.** Treat the CLI as the source of truth, not this page - verbs and
flags evolve. Discover exact arguments with `cairn help`, `cairn <source> <verb> --help`
(e.g. `cairn claude export --help`), `cairn config --help`, and `cairn recall --help`.
For the live set of configured integrations and their enabled state, run `cairn config show`
(`--json` for machine output).

## Shared command vocabulary

The four conversation sources (claude, codex, cursor, granola) accept the same verbs.
**service-docs is a file mirror - it supports only `export` / `list` / `context`** (no
`note` / `close` / `log`, no time window; see `reference/service-docs.md`).

| Verb      | Does                                    | Example                                                  |
| --------- | --------------------------------------- | -------------------------------------------------------- |
| `export`  | Render items to markdown                | `cairn codex export --since 7d`                          |
| `list`    | List exported items (`--all`, `--json`) | `cairn claude list --json`                               |
| `context` | Register QMD context descriptions       | `cairn cursor context`                                   |
| `note`    | Add a timestamped comment               | `cairn claude note "got it working"`                     |
| `close`   | Mark item done (+ optional note)        | `cairn codex close "shipped"`                            |
| `log`     | Annotate status/tags/rating (+ comment) | `cairn cursor log --status done --tags "x,y" --rating 8` |

Claude additionally has `resume` (and an internal `sync` hook); see `reference/claude.md`.

### Time window (windowed sources: claude, codex, cursor, granola)

`export` defaults to the **last 24h** (`--since 1d`). Override with:

```bash
cairn codex export --since today|yesterday|Nd|Nw|YYYY-MM-DD   # e.g. --since 7d, --since 2w
cairn codex export --all                                       # everything (XOR --since)
```

`--since` and `--all` are mutually exclusive. **service-docs is a verbatim mirror - it
has no `--since`/`--all`**; instead `export` takes an optional `--clean` (full re-export).

### Output and flags

- `--json` on read commands emits machine-readable JSON on **stdout**; human messages go to **stderr**.
- `-q/--quiet` suppresses per-item stderr output.

## Enablement is config-driven

Which sources `cairn sync` runs lives in the config file, not in flags. Each collection
has a `sync.enabled` flag chosen at setup. Inspect and change it via the CLI (no
hand-editing JSON):

- `cairn config show` - see every integration and whether it's enabled.
- `/cairn:setup` or `cairn config init --enable claude,codex,cursor,granola,service-docs` - initial choice.
- `cairn config set <source>.enabled true|false` - toggle one later (e.g. `cairn config set cursor.enabled false`).

`cairn sync` runs only **enabled** sources, then refreshes QMD. The `SessionEnd` hook
(`cairn sync --hook`) runs only sources with `on_hook` true (engine default: **Claude only**;
toggle with `cairn config set <source>.on_hook true|false`).

Config resolution: `$CAIRN_CONFIG`, else `$XDG_CONFIG_HOME/cairn/config.json` (else
`~/.config/cairn/config.json`), else the repo root for dev checkouts. Locate it with
`cairn config path`.

## Top-level commands

```bash
cairn sync [--hook | --cron | --all]   # sync enabled sources, then refresh QMD index
cairn recall list <date> | expand <id> # temporal session timeline (drives /cairn:recall)
cairn doctor                           # check install, config, qmd, cron, PATH, stores
cairn config show [--json]             # print resolved config + integrations
cairn config init [--enable ...] | set KEY VALUE | path
cairn cron install | uninstall         # manage the hourly sync crontab entry
cairn help                             # overview an agent can read to drive the tool
cairn --version
```

`--hook` is the fast SessionEnd path (on_hook sources only); `--cron` is the hourly
full sync; `--all` syncs every enabled source regardless of window. Temporal recall has
its own skill (`/cairn:recall`); `cairn recall --help` shows its args.

## Preserved on export (shared)

Re-exporting never clobbers your annotations. Across all sources, exports preserve the
`## My Notes` section and these frontmatter fields: `comments`, `status`, `tags`,
`rating`, `title`, `projects` (claude uses `related` instead of `projects`).

## Per-source reference

Each source's data location, frontmatter schema, and source-specific commands:

- `reference/claude.md` - Claude Code sessions + the `resume` command
- `reference/codex.md` - Codex CLI rollouts
- `reference/cursor.md` - Cursor IDE sessions
- `reference/granola.md` - Granola meetings (dual source: cache + API)
- `reference/service-docs.md` - verbatim service-docs mirror

## Annotation workflow

To log/annotate the current session (CLI forms, natural-language parsing, auto-summary,
status and tag values): see `workflows/log-session.md`. Valid tags live in
`schema/tags.yaml`.
