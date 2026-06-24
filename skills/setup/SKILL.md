---
name: setup
description: One-time setup for cairn on this machine - install the engine globally, write the per-user config, register the hourly sync, and verify. USE ONLY when the user explicitly runs /cairn:setup. Side-effectful - never auto-invoke.
disable-model-invocation: true
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

# Install Cairn

Guided, user-initiated setup. This installs the `cairn` CLI globally, creates the
per-user config, registers the hourly sync cron, and verifies everything. Because it
installs software, writes `~/.config/cairn/config.json`, and edits the crontab, only the
user can trigger it (`disable-model-invocation: true`) - it is never auto-invoked.

Drive these steps in order, pausing for the user where noted. Stop and report if any
step fails; do not silently continue.

## 1. Preflight - check `qmd`

```bash
command -v qmd || echo "MISSING"
```

`qmd` is the search index and is **not** auto-installable. If missing, ask the user to
install it and re-run this step:

```bash
npm install -g @tobilu/qmd      # see https://github.com/tobi/qmd
```

## 2. Ask the user what to set up

Use **AskUserQuestion** to collect:

- **data_root** - directory for exported markdown (default `~/.cairn/.context`; they may
  point it anywhere, e.g. a git-tracked notes repo).
- **integrations** - which of claude / codex / cursor / granola / service-docs to enable
  (multi-select). This is the config-as-data choice: only enabled sources are synced, and
  it is written into the config file in step 4 (`--enable`), not hardcoded anywhere.

Hold the answers for steps 3–4. **Whether granola was selected decides the install
target in step 3** - granola needs the optional `cryptography` dependency, shipped as
the `[granola]` extra.

## 3. Install the cairn engine (global, isolated)

Choose the command by the step-2 answer. **If granola was enabled, install the
`[granola]` extra** (it pulls in `cryptography`, required to decrypt Granola's token
store); otherwise install plain.

```bash
# granola enabled  ->  include the [granola] extra:
uv tool install "cairn[granola] @ git+https://github.com/shavvimal/cairn"

# granola NOT enabled  ->  plain install:
uv tool install "git+https://github.com/shavvimal/cairn"

# No uv? Use pipx (same rule - add [granola] only when granola is enabled):
#   pipx install "cairn[granola] @ git+https://github.com/shavvimal/cairn"
#   pipx install "git+https://github.com/shavvimal/cairn"
```

This puts `cairn` on PATH. Do **not** use `pip install` into the active project venv and
do **not** clone-and-`pip install -e .` unless the user is a cairn developer - an
isolated global install is what makes the cron and SessionEnd hook work everywhere.

Adding granola later needs no full re-setup - just reinstall with the extra:
`uv tool install --reinstall "cairn[granola] @ git+https://github.com/shavvimal/cairn"`.
(If the extra is missing, `cairn granola export` fails loudly at runtime telling you to
install it, rather than silently skipping.)

Confirm it resolved:

```bash
cairn --version
```

## 4. Write the config with the chosen integrations

Everything is set through the CLI - no hand-editing JSON. Run `cairn config --help` to see
the available actions. Pass the step-2 answers straight into `config init`:

```bash
cairn config init --data-root "<data_root>" --enable claude,codex,cursor,granola,service-docs
```

`--enable` writes `sync.enabled: true` for the chosen sources and `false` for the rest, so
`cairn sync` runs exactly what the user asked for. Apply any non-default machine values with
`cairn config set KEY VALUE` (see `cairn config set --help`):

```bash
cairn config set qmd_binary "$(command -v qmd)"   # absolute path - safe under cron's minimal PATH
cairn config set claude.store "~/.claude/projects" # only if a store isn't at its default
# <source>.enabled | <source>.since | <source>.on_hook | cron.schedule are also settable
```

The bundled template already carries the standard store paths, so most users only need
`init` + the `qmd_binary` line. The richer `project_groups` / `repo_catalog` /
`project_descriptions` catalog (used to label projects in QMD context) can be filled in by
editing the file at `cairn config path` - offer to do this with the user, but it is optional.

To change integrations later: `cairn config set <source>.enabled true|false` (no reinstall).

## 5. Register the hourly sync

```bash
cairn cron install     # absolute-path 'cairn sync --cron' entry, safe under cron's PATH
```

The plugin's `SessionEnd` hook (`cairn sync --hook`) is registered automatically when the
plugin is enabled - nothing to do here.

## 6. Verify

```bash
cairn config show      # confirm data_root + the right integrations are enabled
cairn doctor           # install, config, qmd, data_root, stores, cron - all green?
cairn claude export    # a real export of the last 24h
cairn claude list      # exported sessions show up
crontab -l             # the hourly 'cairn sync --cron' line is present
```

If `cairn` is "command not found", the install bin dir isn't on PATH. Find it with
`python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))"` (or `uv tool dir`)
and add it to the shell profile, then re-open the shell.
