---
name: cairn
description: Exports, lists, annotates, and resumes local AI coding sessions (Claude Code, Codex, Cursor) plus Granola meetings and service docs to searchable markdown via the cairn CLI. Use when the user says "sync sessions", "export sessions", "export codex", "export cursor", "export granola", "resume session", "add session note", "close session", "log session", "sync docs", "sync service docs", "meeting transcripts", "scheduled routine", or "import claude memories". Covers per-source export/list/context/note/close/log/decide/supersede/forget, Claude and Codex resume, Claude memory import, the top-level sync/doctor/config/cron/routine commands, and the config-driven enablement model. Requires the cairn CLI - install via /cairn:setup.
allowed-tools: Bash, Read
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.


# Cairn

Cairn syncs local AI activity to markdown for observability and QMD search. One CLI,
`cairn <source> <verb>`, drives five sources:

| Source         | Collection              | What it captures                     |
| -------------- | ----------------------- | ------------------------------------ |
| `claude`       | `claude-code-sessions/` | Claude Code conversations (+ resume, memory import) |
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
`note` / `close` / `log` / `decide` / `supersede` / `forget`, no time window; see `reference/service-docs.md`).

| Verb        | Does                                    | Example                                                       |
| ----------- | --------------------------------------- | ------------------------------------------------------------- |
| `export`    | Render items to markdown                | `cairn codex export --since 7d`                               |
| `list`      | List exported items (`--all`, `--json`) | `cairn claude list --json`                                    |
| `context`   | Register QMD context descriptions       | `cairn cursor context`                                        |
| `note`      | Add a timestamped comment               | `cairn claude note "got it working"`                          |
| `close`     | Mark item done (+ optional note)        | `cairn codex close "shipped"`                                 |
| `log`       | Annotate status/tags/rating (+ comment) | `cairn cursor log --status done --tags "x,y" --rating 8`      |
| `decide`    | Add a row to the `## Decisions` table   | `cairn claude decide "Ship one PR per layer" --kind decision` |
| `supersede` | Strike row N, add its replacement       | `cairn claude supersede 3 "Keep three words until grading"`   |
| `forget`    | Withdraw row N so it stays withdrawn    | `cairn claude forget 3 --reason hallucination`                |

Use `cairn resume SOURCE:ID` for either Claude or Codex: it prints the appropriate native
command. Show that command for the user's terminal. Claude also retains its legacy picker.

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
- `export --json` prints one JSON object on stdout: `source`, `exit`, `written` (document
  paths), `failed`, `findings` (`ref`, `kind`, `severity`, `message`), `exporter_version`
  and `spec_target`. Every human line goes to stderr.
- `-q/--quiet` suppresses per-item stderr output.

### Parser drift

`Error: parser drift: SOURCE:ID KIND: ...` means the source file holds records but the
adapter rendered no user or assistant message (`zero-messages`), no line parsed as JSON
(`unreadable-lines`) or a Codex rollout has no session id (`no-session-id`). The export
wrote nothing for that session, kept the last good document and exits 1. The cause is a
host format change, a corrupt file or a cairn parser regression. Treat the line as a
diagnostic signal, not something to work around: report it, the `spec target` it names
and the installed host version (`claude --version`, `codex --version`, `agy --version`)
to the user. Do not retry in a loop. `Warning: parser drift:
... no-user-turns` writes the document anyway. `cairn doctor` lists open findings and
`cairn sync` logs them on an `[exporter-drift]` line until a later export of that session
parses cleanly.

### On failure

On a failure, read `code`, run `cairn errors CODE`, run its verify command, then act. The
code is the `code:` stderr line (`code: CODE (run 'cairn errors CODE')`), the `code` key of a
`--json` failure object or result line (next to `fix.next_step` and `fix.verify`), or the
` [CODE]` suffix of a `cairn doctor` line. A verify command edits no config or instruction
file and never overwrites a finished round's outcome; `invoke status` finalises and
publishes an unfinished round whose runner and peer have both exited, as every invoke reader
does. An exit code alone is ambiguous: 2 is both a usage
error and a denied invoke round, and 3 is an empty digest day, a stalled `agents wait` and
a withdrawn decision claim. `cairn errors` lists every
code; `docs/error-codes.md` is the same table.

## Enablement is config-driven

Which sources `cairn sync` runs lives in the config file, not in flags. Each collection
has a `sync.enabled` flag chosen at setup. Inspect and change it via the CLI (no
hand-editing JSON):

- `cairn config show` - see every integration and whether it's enabled.
- `/cairn:setup` or `cairn config init --enable claude,codex,cursor,granola,service-docs` - initial choice.
- `cairn config set <source>.enabled true|false` - toggle one later (e.g. `cairn config set cursor.enabled false`).

`cairn sync` runs only **enabled** sources, then refreshes QMD. The `SessionEnd` hook
(`cairn hook handle --harness HOST`) exports only the originating session. Enabled sources
default to hook sync unless `on_hook` is explicitly false. `cairn sync --hook` retains the
legacy Claude-default policy for existing scripts.

Config resolution: `$CAIRN_CONFIG`, else `$XDG_CONFIG_HOME/cairn/config.json` (else
`~/.config/cairn/config.json`), else the repo root for dev checkouts. Locate it with
`cairn config path`.

## Top-level commands

```bash
cairn sync [--hook | --cron | --all]   # sync enabled sources, then refresh QMD index
cairn recall list <date>               # temporal session timeline (drives /cairn:recall)
cairn recall expand <id>               # one session's condensed transcript
cairn recall digest <date>             # one day condensed with provenance
cairn recall frame <ref> <n>           # open a cited frame
cairn recall verify <journal> --digest <dir> [--fix]  # check a journal's citations and quotes; exit 4 on a failure
cairn recall gaps [--json]             # what recall cannot see right now: export/index lag, disabled sources, rg
cairn doctor [--strict-index]          # check install, config, qmd + index, cron, PATH, stores
cairn config show [--json]             # print resolved config + integrations
cairn config init [--enable ...]       # create the per-user config
cairn config set KEY VALUE             # set one config value
cairn config unset KEY                 # remove a routine, routine field or invoke profile
cairn config path                      # print the resolved config path
cairn cron install                     # add / refresh the hourly sync crontab entry
cairn cron uninstall                   # remove the cairn cron entry
cairn instructions install --harness claude|codex   # add the recall-first block to the user instruction file
cairn instructions remove --harness claude|codex    # delete the block and record an opt-out
cairn instructions show --harness claude|codex      # print the block for a paste by hand
cairn instructions status [--json]                  # the block state per harness
cairn routine install NAME             # add a routine's launchd job (macOS)
cairn routine uninstall NAME           # remove the launchd job; stamps are kept
cairn routine run NAME [--day D] [--force] [--dry-run] [--json]   # run the due days now
cairn routine list [--json]            # every configured routine
cairn routine status NAME [--json]     # state, stamps, last run; exits 0/1/2
cairn routine pause NAME --reason TEXT # skip runs until resumed
cairn routine resume NAME              # remove the pause
cairn routine kickstart NAME [--force-restart]   # ask launchd to start the job now
cairn help                             # overview an agent can read to drive the tool
cairn --version
```

`--hook` is the legacy fast sync (on_hook sources only); `--cron` is the hourly
full sync; `--all` also updates and embeds every enabled source using its configured window. Temporal recall has
its own skill (`/cairn:recall`); `cairn recall --help` shows its args.

A routine runs a prompt file through `claude` or `codex` once a day for each UTC day without
a done stamp, and stamps a day done only after its `validate` command exits 0 (3 means done
and empty). Set one up, dry-run, install, read status and re-run a day: see
`workflows/routine.md`.

## Peer agents and fan-out health

```bash
cairn invoke codex --prompt-file brief.md --effort high   # run Codex headlessly, record the exchange
cairn invoke claude --prompt "…" --profile review          # the mirror direction
cairn invoke antigravity --prompt "…" --model <slug>       # Google Antigravity (agy); slugs from `agy models`
cairn antigravity export [--all]                           # export agy conversations (also via the Stop hook)
cairn invoke status <thread-id> [--follow]                 # health line, progress, answer
cairn invoke tail <thread-id>                              # one line per tool call (CMD, WRITE, DENIED, RESULT)
cairn invoke wait <thread-id> --json                       # block until the round exits; print its result line
cairn invoke codex --resume <thread-id> --prompt "…"        # continue the same thread, any time later
cairn invoke cancel <thread-id>                            # stop a run
cairn invoke publish <thread-id>                           # finish pending export/link/index
cairn invoke list-models                                   # Codex model and effort catalogue
cairn agents list [--session SOURCE:ID] [--all]            # peer runs plus Claude/Codex subagents, with health
cairn agents wait --session SOURCE:ID [--timeout-s N]      # block until the fan-out has exited
```

The `invoke-codex`, `invoke-claude` and `invoke-antigravity` skills drive these; the doctrine for adversarial
review lives in `references/peer-review.md`. Defaults (model, effort, profile, timeouts) are
config as data under the `invoke` block: `cairn config set invoke.codex.effort medium`.
Claude and Codex also take named profiles under `invoke.<peer>.profiles`: a built-in `base`
plus native `extra_args`, usable as `--profile NAME`. A profile cannot drop its base's deny
rules or switch to a looser permission mode. `--owner LABEL` records who started a round
(`routine:<name>` for a routine) on the thread, the result line and the peer session.

## One folder per session

Everything a session spawned sits in a folder named after its document:
`<collection>/<project>/<date>-<shortid>/`. Subagent transcripts export there as
`agent-<id>.md` (`type: claude-subagent` / `codex-subagent`, hidden from `list`, shown by
`cairn recall list --include-subagents`) and peer exchanges as `exchange-<peer>-<short>.md`
(`type: agent-exchange`). The parent document carries a `## Subagents` index and `links:`
to the peer sessions it spawned or was spawned by.

Every session and exchange document also carries `provenance: tool|agent|imported|user`
in its head (before `status:` in a session, before `title:` in an exchange): who produced
the body text. The exporter writes it
on every re-render, so it is not an annotation; `cairn recall list --provenance VALUE`
filters on it.

## Preserved on export (shared)

Re-exporting never clobbers your annotations. Across all sources, exports preserve the
`## My Notes` section, a `## Decisions` table directly after it (a table cairn wrote stays
byte for byte; a hand edit keeps its text, with LF endings), and these
frontmatter fields: `comments`, `status`, `tags`, `rating`, `title`, `projects`, `links`.
`decide`, `supersede` and `forget` are described in `workflows/log-session.md`, and so is
`cairn claude memory import`, which adds Claude Code auto-memory notes as decision rows.

## Per-source reference

Each source's data location, frontmatter schema, and source-specific commands:

- `reference/claude.md` - Claude Code sessions, the `resume` command and `memory import`
- `reference/codex.md` - Codex CLI rollouts
- `reference/cursor.md` - Cursor IDE sessions
- `reference/granola.md` - Granola meetings (dual source: cache + API)
- `reference/service-docs.md` - verbatim service-docs mirror

## Annotation workflow

To log/annotate the current session (CLI forms, natural-language parsing, auto-summary,
status and tag values): see `workflows/log-session.md`. To schedule a daily peer run, see
`workflows/routine.md`. Valid tags live in
`schema/tags.yaml`.
