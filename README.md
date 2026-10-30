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

For Codex CLI (tested on 0.160.0; hook discovery also checked on 0.163.0; hook trust
written by `cairn hook install` verified on 0.165.0):

```sh
codex plugin marketplace add shavvimal/cairn --ref main
codex plugin add cairn@cairn
```

Restart Codex, select **cairn:setup** in `/skills` or the `$` picker, and explicitly run it.
The plugin's onboarding action points to this same setup skill. Setup is never implicitly
invoked. Setup runs `cairn hook install --harness codex`, which also writes the Codex
trust entry for Cairn's SessionEnd command on the Codex versions it was verified against
(0.165.x). On another Codex version, or with `--no-trust`, review/trust the command in
`/hooks` when prompted.

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

Then restart Codex. On Codex 0.165.x the install refreshes the trust entry for the changed
command; on another version, review the changed hook in `/hooks`.

For a manual Claude Code update, in a session:

```text
/plugin marketplace update cairn   # refresh the marketplace and update the plugins installed from it
```

Closing the panel runs `/reload-plugins` for you; if it warns about the prompt cache, run
`/reload-plugins --force`. Then upgrade the engine with `uv tool upgrade cairn` and run
`cairn hook install --harness claude`. `/plugin update cairn@cairn` on its own reinstalls
the version the marketplace last saw. The guided workflow also refreshes existing cron
entries and verifies incremental sync and search.

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
cairn hook uninstall --harness codex  # only Cairn's unchanged Codex handler and trust entry
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
/recall digest yesterday       # one day's sessions condensed, every line cited to its frame
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

Every windowed source (claude, codex, cursor, granola) shares the same verbs — `export`, `list`, `context`, `note`, `close`, `log`, `decide`, `supersede`, `forget` — with `export` defaulting to the last 24h:

```bash
cairn claude export --since 7d   # export the last week of Claude sessions
cairn cursor export --all        # export all Cursor sessions
cairn granola export             # export the last 24h of meetings
cairn claude list --json         # list exported sessions as JSON
cairn resume codex:FULL_ID        # print a native resume command (Claude or Codex)
cairn claude resume --pick       # legacy interactive Claude picker
cairn codex note "progress"      # add a timestamped note
cairn codex decide "Use one PR per layer" --kind decision --revisit-by 2026-12-01
                                 # add a row to the session's ## Decisions table
cairn codex forget 3 --reason hallucination
                                 # withdraw row 3; a later decide of the claim exits 3
cairn recall decisions --overdue # active decision rows due for review
```

`forget` strikes the row as `~~claim~~ withdrawn: <reason>` and appends a SHA-256
fingerprint of the normalised claim to `withdrawn.jsonl` in the data root. The ledger
holds no claim text. Withdrawal does not erase: the claim stays in the session document
until you edit the file, in the QMD index until the next index update after that edit,
and in any backup or copy of the file. Recall and the digest hide withdrawn rows.

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

### `cairn routine`: run a peer on a schedule

A routine is a named job in config. Once a day, at a local time, launchd starts
`cairn routine run NAME`. The run finds every UTC day in its window that has no done stamp,
refreshes the listed session exports once, and for each day, oldest first, renders a prompt
file, runs an optional `prepare` command, runs `cairn invoke <peer>` and then your
`validate` command. cairn knows nothing about what the peer writes: every path comes from
config. The config template ships one example, which runs nothing until you install it:

```json
"routines": {
  "example-daily": {
    "peer": "claude",
    "prompt_file": "~/projects/site/prompts/daily.md",
    "cd": "~/projects/site",
    "profile": "scheduled",
    "at": "05:20",
    "sources": ["claude", "codex"],
    "outputs": { "journal_dir": "{{DATA_ROOT}}/journal" },
    "validate": ["true"]
  }
}
```

`peer` is `claude` or `codex`. `at` is `HH:MM` local time; the day a run works on is always
`yesterday-utc`. Optional fields: `model`, `catch_up_days` (1 to 30, default 3),
`timeout_s`, `budget_usd_per_day` (Claude only; the remainder for the day goes to
`--max-budget-usd`, a client-side estimate that spend can pass, so leave headroom), `outputs` (named directories, created before each attempt) and
`prepare`. `prepare` and `validate` are argv lists, never shell strings; both run in `cd`.
Path fields expand a leading `~` and `{{DATA_ROOT}}`, nothing else, and must end up absolute.

```bash
cairn routine run example-daily --dry-run   # print each due day's argv and rendered prompt; run nothing
cairn routine install example-daily         # add the launchd job dev.cairn.routine.example-daily
cairn routine kickstart example-daily       # ask launchd to start it now (--force-restart kills a running instance first)
cairn routine status example-daily          # state, config, paths, per-day stamps, last run, cost, log tail (--json, --lines N)
cairn routine status example-daily --json > /tmp/status.json || echo "needs a look"   # exit 0 fresh, 1 needs attention, 2 paused or disabled
cairn routine list [--json]                 # every routine with install state, last exit and state
cairn routine pause example-daily --reason "away this week"   # every run skips until 'cairn routine resume example-daily'
cairn routine run example-daily --day 2026-10-27 --force   # re-run one day as a new attempt
cairn routine uninstall example-daily       # remove the job; stamps, attempts and the log stay
```

The prompt file, each `prepare` and `validate` element and the profile's `extra_args` are
rendered per day and per attempt:

| Variable | Value |
|---|---|
| `{{DAY}}` | the UTC day, `YYYY-MM-DD` |
| `{{DATA_ROOT}}` | the resolved `data_root` |
| `{{ATTEMPT_DIR}}` | `<data_root>/routines/<name>/<DAY>/attempt-<n>` |
| `{{DIGEST_DIR}}` | `<ATTEMPT_DIR>/digest`, created empty before each attempt; pass it to `prepare` as `--out` |
| `{{<KEY>}}` | one per `outputs` key, upper-cased: `journal_dir` gives `{{JOURNAL_DIR}}` |

An unknown variable stops the run with exit 2 and no stamp, because a typo would fail every
day the same way. `cairn invoke` itself never renders a template.

Exit codes are the contract. `prepare` exit 0 continues, exit 3 means the day had no input
(the day is stamped `empty`, and invoke and `validate` are skipped), anything else fails the
day. After an invoke exit 0, `validate` exit 0 stamps the day `done`, exit 3 stamps it
`empty`, and anything else (a timeout or a missing executable too) fails it. A day is done
only after `validate` passes: an invoke that exits non-zero fails the day without running
`validate`. A failed day is retried on the next run, up to 3 counted attempts. The run
stops at its first failed day and defers the rest, so a systemic fault (a logged-out peer, a
broken validator) costs one attempt per run, not one per day. `prepare` has 600 s,
`validate` 900 s.

Catch-up: launchd runs a job whose time passed during sleep once on wake, and coalesces
several missed times into one run. That run picks up every window day still without a done
stamp, so two days asleep cost nothing. A machine that was powered off misses the run
entirely; the next one covers what is still inside `catch_up_days`. An older day shows as
`expired` in `status`, and `cairn routine run NAME --day DATE` recovers it. Sources are
refreshed with `--since` set to the ISO date one day before the oldest due day, so the
window covers the whole UTC day in any time zone. Every listed source must refresh, or each
day fails with `incomplete-input` and no invoke runs.

Each round runs with `--owner routine:<name>`, so the peer session carries that owner label.
A digest does not skip these sessions: it copies `owner` into each one's front matter, and
the consumer decides what to do with them. One run at a time per routine: a second
start logs `skipped-lock` and exits 0. `routine run` refuses to start inside a peer run.
After 3 consecutive failed runs with a counted failure the routine disables itself, so a
broken validator costs at most 3 peer rounds in a row (a run with a done or empty day resets
the count); `cairn routine resume NAME` clears it.
`cairn routine run NAME --json` prints the run report, which is also `last-run.json`. A
run warns about a digest source that is enabled, not in `sources` and not exported in
the last 24 h (`stale_sources`: `warn`, `refuse` or `ignore`). To be told about each day,
set `terminal_hook`, an argv list whose first element is an absolute path:

```bash
cairn config set routines.example-daily.terminal_hook '["/usr/bin/osascript", "-e", "display notification \"cairn routine finished a day\""]'
```

It runs once per day stamp, with a 10 s timeout, the day's report row on stdin and
`CAIRN_ROUTINE_DAY`, `CAIRN_ROUTINE_OUTCOME`, `CAIRN_ROUTINE_REASON` and the other
`CAIRN_ROUTINE_*` names in its environment. A hook failure is logged and changes nothing
else. Details: [docs/routine-hardening-spec.md](docs/routine-hardening-spec.md).
State lives in `$XDG_STATE_HOME/cairn/routines/<name>/` (default `~/.local/state`), each
attempt keeps its prompt, args, invoke result, `prepare.out` (the stdout of `prepare`),
`validate.out` and `validate.err` under `{{ATTEMPT_DIR}}`. The stderr of `prepare` goes to
the log, `<data_root>/logs/routine-<name>.log`. Attempt
directories are not pruned in this version.

Scheduling is launchd only (macOS) in this version. On another OS `cairn routine install`
prints the crontab line it would need and exits 2. The step-by-step operator workflow is
[skills/cairn/workflows/routine.md](skills/cairn/workflows/routine.md); the design is in
[docs/routine-spec.md](docs/routine-spec.md).

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

**Config profiles.** Besides the built-in `review`, `edit` and `full`, Claude and Codex take
named profiles under `invoke.<peer>.profiles`. Each one is a built-in `base` plus
`extra_args`, a native argv list emitted after the base's flags:

```json
"profiles": {
  "scheduled": { "base": "edit", "extra_args": ["--permission-mode", "dontAsk", "--allowedTools", "Read,Glob,Grep,Edit,Write", "--add-dir", "{{JOURNAL_DIR}}"] }
}
```

`--profile scheduled`, `invoke.claude.profile` and a routine's `profile` all accept the name.
A profile cannot drop its base's deny rules or switch to a looser permission mode, and flags
cairn sets itself (`--model`, `--resume`, `--output-format` and the like) are refused. It can
widen what its base approves without a prompt, which is the point: in Claude, deny beats
allow, so `--allowedTools "Bash(pnpm:*)"` plus `--disallowedTools "Bash(pnpm publish:*)"`
runs every pnpm script except publishing. Write a Bash rule as `Bash(cmd *)`, the form the
permission dialog writes; `Bash(cmd:*)` is an equivalent spelling of the trailing wildcard.
A path-scoped rule needs the `//` absolute form: `Edit(/{{JOURNAL_DIR}}/**)` renders to
`Edit(//...)`. `--settings`, `--mcp-config`, `--strict-mcp-config` and
`--permission-prompt-tool` need base `full`, because they can add allow rules, hooks or an
approval tool behind the base's back. Codex `extra_args` take only `--add-dir DIR` and
`-c KEY=VALUE` pairs.
`{{NAME}}` tokens are rendered only by `cairn routine run`; plain `cairn invoke` refuses a
profile that holds one. Set and remove profiles from the CLI:

```bash
cairn config set invoke.claude.profiles.scheduled '{"base": "edit", "extra_args": ["--permission-mode", "dontAsk"]}'
cairn config set invoke.claude.profiles.scheduled.extra_args '["--permission-mode", "dontAsk", "--add-dir", "{{JOURNAL_DIR}}"]'
cairn config unset invoke.claude.profiles.scheduled
```

`config set` also takes `routines.<name>` (a JSON object), `routines.<name>.<field>` and
`routines.<name>.outputs.<key>`; argv fields take only JSON arrays. `config unset` removes a
routine, an optional routine field, an output or a profile, and re-validates the file.

**Owner labels.** `cairn invoke <peer> --owner LABEL` records who started a round, separate
from the calling session: the thread, each round, the result line, the exchange document and
the peer session's front matter carry `owner: LABEL`. A routine passes `routine:<name>`. On
resume the recorded owner stays, and a different `--owner` is refused.

**Journal.** The `journal` collection (`"journal": { "sync": { "enabled": true } }`) is
store-less: nothing is exported into it, and `cairn sync` registers `<data_root>/journal` in
QMD once a routine has written markdown there.

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
hooks are preserved. On the Codex versions where cairn verified Codex's trust-hash format
(0.165.x), the installer also writes the `[hooks.state."<key>"]` trust table for its own
SessionEnd group in `$CODEX_HOME/config.toml`, keeps a byte backup in
`config.toml.cairn-bak`, and records a sha256 per entry in
`$CODEX_HOME/cairn-hook-receipt.json`. `--dry-run` prints every planned write and cairn
writes nothing (Codex's own `hooks/list` answer can still create Codex state files);
`--no-trust` leaves trust to you. On another Codex version cairn writes no trust:
review/trust the hook in `/hooks`. `cairn hook uninstall --harness codex` removes only the
entries whose sha256 still matches the receipt.

The environment that cron, the hook launcher, the Codex and Antigravity hook registrations
and a routine's launchd job record pins `CAIRN_CONFIG`, `PATH`, the XDG and QMD paths. It
no longer pins `CLAUDE_CONFIG_DIR` or `CODEX_HOME` unless the host shell set them. With
`CLAUDE_CONFIG_DIR` exported, headless `claude -p` looks for its account file at
`<CLAUDE_CONFIG_DIR>/.claude.json` instead of `~/.claude.json` and reports `Not logged in`.
An existing install keeps the old values until `cairn hook install` and `cairn cron install`
run again.

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
cairn recall digest yesterday --all-projects --json
cairn recall digest yesterday --all-projects --provenance tool --json
cairn recall frame claude:FULL_ID 12 --frames DIR/claude-SHORTID.frames.jsonl
cairn recall gaps --json
cairn recall verify journal.md --digest DIR   # every [SOURCE:ID fN] citation and quote checks
```

`cairn recall gaps` reports what recall cannot see right now: export lag per source,
files changed after the last `qmd update`, collections QMD has not registered, disabled
sources and a missing rg. Each gap has a severity and, where one exists, the command
that fixes it (`fix` is `null` for a disabled source or a missing rg), and the
last line is `status: clean` or `status: degraded`. `cairn recall list --json --coverage`
prints `{"sessions": [...], "coverage": {...}}`, and `cairn recall digest --json` gains a
`coverage` key: the store gaps that can hide a session in the window, with a verdict of
`found`, `clean-miss` or `degraded-miss`. An empty result with a degraded verdict is not
proof that nothing happened.

`digest` condenses one UTC day of sessions for a model to read, with provenance. Each
session gives two files in `--out` (default `$XDG_CACHE_HOME/cairn/digests/DATE/`):
`<source>-<shortid>.md`, the condensed body with one `<!-- fN TS ROLE -->` marker per frame,
and `<source>-<shortid>.frames.jsonl`, one row per frame with its offset, length and hash.
The digest ordinal `N` is the stable id; the `cairn:turn` markers in an export are length
frames, not ids. `cairn recall frame REF ORDINAL` prints one frame as exported, and with
`--frames` it warns when the frame changed since the digest. Dates are those of the
exported frames: a Codex tool result that arrives after midnight stays on its call's day.

`--json` prints `{"sessions": [...], "skipped": [...], "excluded": [...]}`: one row per
written session (`ref`, `path`, `frames_path`, `role`, `parent`, `provenance`, `owner`,
`project`, `cwd`, `frames_today`, `chars`, `truncated`), one `{"ref", "reason"}` per legacy or corrupt export
left out, and one `{"ref", "owner"}` per session a routine owns. A routine's own sessions
(`owner: routine:<name>`) and their subagents are left out of the digest so a journal run
never feeds the next journal; `--include-routine-sessions` keeps them. Exit 0
means at least one session was written. Exit 3 means an empty day: no session qualified,
so the directory holds only its marker. This is the routine's `prepare` contract, so a
digest as `prepare` stamps such a day `empty`.


Every session document and every peer exchange document carries a `provenance` key in its
front matter: directly before `status:` in a session document, directly before `title:` in
an exchange document. The key says who produced the body text. It is
not the digest's frame citations, which this README also calls provenance.

| Value | Meaning |
|---|---|
| `tool` | cairn exported it from the harness log of a session that a person drove |
| `agent` | an agent wrote the prompts: a peer run (first user message is the `cairn invoke` preamble), a subagent transcript, an exchange document |
| `imported` | mirrored from another application: a Granola meeting |
| `user` | a person wrote it by hand |

The exporter owns the key and writes it again on every re-render, so a hand edit does not
last; use `tags` to label a document yourself. A document exported before the key existed
gets a value from its front matter when it is read: a subagent type reads `agent`, Granola
reads `imported`, an `owner` label or a `parent` link reads `agent`, and the rest read
`tool`. The digest also reads `agent` when the first user message with text starts with
the invoke preamble. A stored value outside the four prints a warning on stderr and reads as the derived value.

`cairn recall list --json` and `cairn recall digest --json` rows carry `provenance` and
`owner` (the stored owner label, or `null`; the digest also takes the label from the
invoke preamble). On both commands, the repeatable `--provenance VALUE` keeps only
documents with that value: `--provenance tool` leaves out peer runs and routine runs.
Neither filter needs QMD. The `recall list` filter reads front matter only. A digest
filtered down to no session exits 3, as an empty day does.

`verify` checks a journal written from a digest. A citation is `[SOURCE:ID fN]`, one frame
per bracket, with the digest's `ref` (full, or a unique prefix of at least 8 characters).
Each citation must name a frame the digest kept, unchanged since, and each quoted span (in
double quotes, at least 15 characters, or a `>` block quote) must be in a frame its block
cites. It prints `FILE:LINE: REASON: DETAIL` per failure and exits 0 when all pass, 4 when
one fails and 1 on an error; `--json` gives the counts and failures, and `--fix` moves
failing blocks under `## Unverified`. See `docs/journal-verify-spec.md`.

Dates are UTC and match recorded activity, not every day between start and finish.
Listing defaults strictly to the current directory; use `--all-projects` to broaden it.
Legacy date-only records are labeled; unknown counts do not exclude meetings. Unique
prefixes are accepted, collisions fail with source-qualified choices. `--refresh` exports
without QMD and returns failure if refresh failed. Schema upgrades preserve annotations
even when native mtime is unchanged; unavailable native data leaves old exports intact.

Every export checks whether its source actually changed (mtime/timestamp/hash) before doing work, so a "nothing changed" run is nearly instant. Steps are timed and logged to `.context/logs/context-sync.log`.

**Exporter guards.** A host can change its transcript format without notice. An export
refuses to write a session whose source file holds records but renders no user or
assistant message, prints `Error: parser drift: SOURCE:ID KIND: ...` and exits 1. The
last good document stays in place. The finding is kept in
`<data_root>/exporter/findings.json` until a later parse of that session succeeds; the
hook worker stores the same message as the request error. A top-level session with
assistant turns but no user turn is written with a `Warning: parser drift: ... no-user-turns`
line. An existing document whose frontmatter does not parse blocks its re-export with
`Error: refusing to re-export ...; fix or move it`. `cairn <source> export --json` prints
one JSON object on stdout (documents written, findings, the spec target) and sends the
human lines to stderr. Every exported document carries `exporter_version` and
`exporter_spec_target`: the adapter version and the date its record shapes were last
checked against a live store.

`cairn sync --cron` and `cairn sync --all` then run a gap check for Claude Code, Codex
and Antigravity. It compares the session files changed in the last 30 days (leaving out
files changed in the last 10 minutes) with the documents in the collection, re-exports
the missing ones once with `--since 30d` and checks again:

```text
  [gap-check] codex-sessions: 41 on disk in 30d, 1 missing, 0 held by parser drift
  [gap-heal:codex-sessions] ok (2.1s)
  [gap-check] codex-sessions: after heal 0 missing
  [exporter-drift] 1 open findings: claude:5e551000 zero-messages
```

A session with an open drift finding is "held", not missing. A gap the heal cannot close
fails the `gap-check:<collection>` step, so the run ends `=== FAILED ===` with that step
named. The `[exporter-drift]` line repeats on every sync while a finding stays open.
Cursor is not in the gap check. A document you delete by hand comes back on the next full
sync while its source file is under 30 days old; move the source file or wait for the
window to pass. `cairn sync --hook` runs neither step.

`cairn doctor` prints one `exporter [SOURCE]` line per enabled adapter with its spec
target, up to five `parser drift` warnings (oldest first), and a read-only
`gap check [COLLECTION]` line per covered collection. These lines warn and never fail
the doctor run.

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
