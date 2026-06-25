---
name: setup
description: One-time setup for cairn on this machine - install prerequisites and the engine, write the per-user config, bootstrap the QMD collections, run the first embed, register the hourly sync, and verify search actually returns results. USE ONLY when the user explicitly runs /cairn:setup. Side-effectful - never auto-invoke.
disable-model-invocation: true
allowed-tools: Bash, Read, Edit, AskUserQuestion, Monitor
---

# Install Cairn

Guided, user-initiated setup. It installs the prerequisites, installs the `cairn` CLI
globally, writes the per-user config, **bootstraps the QMD collections and runs the first
embed**, registers the hourly sync, and verifies that **search actually returns results** -
not just that the plumbing is wired. Because it installs software, writes
`~/.config/cairn/config.json`, and edits the crontab, only the user can trigger it
(`disable-model-invocation: true`) - it is never auto-invoked.

Drive the steps in order, pausing where noted. **Stop and report if any step fails; do not
silently continue.** A green `cairn doctor` is **not** the finish line - step 7 is: a real
query returning hits.

> **Already installed and just need the latest?** This is a fresh setup. To *update* an
> existing install, upgrade the engine and the plugin together (they're version-locked):
> `uv tool upgrade cairn`, then `/plugin update` and `/reload-plugins`. `cairn doctor` warns
> when the two have drifted. See "Updating cairn" in the README.

## 1. Preflight - check ALL prerequisites at once

cairn needs `qmd` (the search index) and an **engine installer** (`uv` *or* `pipx`); if
`qmd` is missing you also need `npm` to install it. Check everything up front and report the
whole gap list in one pass - never discover a missing tool three steps later.

```bash
echo "qmd:  $(command -v qmd  || echo MISSING)"
echo "uv:   $(command -v uv   || echo MISSING)"
echo "pipx: $(command -v pipx || echo MISSING)"
echo "npm:  $(command -v npm  || echo MISSING)"
echo "brew: $(command -v brew || echo MISSING)"
```

Decide what's missing and how to close it, then use **AskUserQuestion** to let the user pick,
per gap, "install it for me now" vs "I'll run it myself and re-run preflight". Auto-install
only what's actually installable on this machine:

- **`uv` missing, `brew` present** → `brew install uv`
- **`uv` missing, no `brew`** → `curl -LsSf https://astral.sh/uv/install.sh | sh`
  (then a new shell or `source`-ing the env file it prints is needed for `uv` to land on PATH)
- **`qmd` missing, `npm` present** → `npm install -g @tobilu/qmd`
  (the old advice called `qmd` "not auto-installable" - that only meant *cairn* can't install
  it; `npm install -g @tobilu/qmd` works fine whenever `npm` exists, so treat it as
  installable then)
- **`qmd` missing AND `npm` missing** → not auto-installable. Stop and ask the user to install
  Node/npm (`brew install node`, or nvm) and re-run `/cairn:setup`.
- **prefer `pipx` and it's missing** → `brew install pipx` (or
  `python3 -m pip install --user pipx && python3 -m pipx ensurepath`). Only needed if they
  decline `uv`.

After any install, **re-run the preflight block** and confirm the tool resolves. Do not
proceed until `qmd` resolves **and** at least one of `uv`/`pipx` resolves.

## 2. Ask the user what to set up

Use **AskUserQuestion**. Keep the **session sources** and **service docs** as *separate*
questions - they are configured differently (sessions point at one app store each; service
docs point at one-or-more arbitrary docs folders you must collect), and bundling them on one
checkbox is confusing.

- **data_root** - directory for exported markdown (default `~/.cairn/.context`; they may
  point it anywhere, e.g. a git-tracked notes repo).
- **session integrations** (multi-select) - which of **claude / codex / cursor / granola** to
  enable. Config-as-data: only enabled sources are synced, written into the config in step 4
  (`--enable`), not hardcoded. **Do NOT put service-docs in this list.**
- **service docs?** (separate yes/no) - "Do you also want to index local documentation
  folders (e.g. a repo's `docs/`) so they're searchable alongside your sessions?" If **yes**,
  collect one or more **(name, folder path)** pairs - a short service name (becomes the
  subfolder, e.g. `api`) and the absolute path to that docs folder (e.g.
  `~/Code/myapp/api/docs`), plus an optional one-line description each. Ask the user directly
  for the paths (they're free-form, so let them type as many as they want). Each path must be
  a real directory - you'll validate when you add it in step 4.

Hold the answers for steps 3-4. **Whether granola was selected decides the install target in
step 3** - granola needs the optional `cryptography` dependency, shipped as the `[granola]`
extra.

## 3. Install the cairn engine (global, isolated)

Pick the command by the installer that resolved in step 1 (`uv` preferred) and the step-2
granola answer. **If granola was enabled, install the `[granola]` extra** (it pulls in
`cryptography`, required to decrypt Granola's token store); otherwise install plain.

```bash
# uv, granola enabled  ->  include the [granola] extra:
uv tool install "cairn[granola] @ git+https://github.com/shavvimal/cairn"
# uv, granola NOT enabled  ->  plain install:
uv tool install "git+https://github.com/shavvimal/cairn"

# pipx (only if uv is unavailable; same rule - add [granola] only when granola is enabled):
#   pipx install "cairn[granola] @ git+https://github.com/shavvimal/cairn"
#   pipx install "git+https://github.com/shavvimal/cairn"
```

This puts `cairn` on PATH. Do **not** `pip install` into the active project venv and do
**not** clone-and-`pip install -e .` unless the user is a cairn developer - an isolated global
install is what makes the cron and SessionEnd hook work everywhere.

Adding granola later needs no full re-setup - reinstall with the extra:
`uv tool install --reinstall "cairn[granola] @ git+https://github.com/shavvimal/cairn"`.
(If the extra is missing, `cairn granola export` fails loudly at runtime telling you to
install it, rather than silently skipping.)

Confirm it resolved:

```bash
cairn --version
```

If "command not found", the install bin dir isn't on PATH - find it (`uv tool dir`, or
`python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))"`), add it to the shell
profile, reopen the shell, re-check. Stop and report if it still fails.

## 4. Write the config with the chosen integrations

Everything is set through the CLI - no hand-editing JSON. Pass the step-2 **session** answers
into `config init` (list ONLY the session sources the user picked - **not** service-docs;
service docs are configured by folder in step 4b):

```bash
cairn config init --data-root "<data_root>" --enable claude,codex,cursor,granola
```

`--enable` writes `sync.enabled: true` for the listed sources and `false` for the rest. Then
pin the qmd binary to an **absolute path** (so cron's minimal PATH finds it) and apply any
non-default stores:

```bash
cairn config set qmd_binary "$(command -v qmd)"     # absolute path
cairn config set claude.store "~/.claude/projects"   # only if a store isn't at its default
# <source>.enabled | <source>.since | <source>.on_hook | cron.schedule are also settable
```

The bundled template carries the standard store paths, so most users only need `init` + the
`qmd_binary` line. The richer `project_groups` / `repo_catalog` / `project_descriptions`
catalog (labels projects in QMD context) can be filled in by editing `cairn config path` -
offer it, but it's optional.

### 4b. Service docs (only if the user said yes in step 2)

Service docs aren't an app store - they're arbitrary docs folders you mirror. Enable the
collection, then add each **(name, path)** pair with `add-service-doc` (it validates the path
exists, fails loud if not, and clears the bundled placeholder on the first real add - so the
config never keeps mirroring a non-existent `~/Code/your-product/...` path):

```bash
cairn config set service-docs.enabled true
cairn config add-service-doc api  "~/Code/myapp/api/docs"  -d "Backend API docs"
cairn config add-service-doc web  "~/Code/myapp/web/docs"  -d "Frontend docs"
# ...one line per folder the user gave you
```

If `add-service-doc` reports "Path not found", re-ask the user for the correct folder - do not
enable a source pointing at a missing path. To drop one later: `cairn config remove-service-doc
<name>`. Skip this whole sub-step if the user didn't want service docs.

Confirm:

```bash
cairn config show     # data_root correct? the right integrations enabled?
```

To change integrations later: `cairn config set <source>.enabled true|false` (no reinstall).

## 5. First export, collection bootstrap, and the first embed (the slow core)

This is the step the old skill skipped, leaving QMD with zero collections and an empty index.
Three parts: produce markdown, register collections, embed. **The first embed downloads ~2GB
of GGUF models and can take 10+ minutes** - far longer than a foreground Bash call survives -
so it runs in the **background**, watched by the **Monitor** tool.

### 5a. Produce markdown for the enabled sources

`cairn sync --cron` exports every enabled source into `<data_root>/<collection>/`, **registers
each collection that now has markdown** (the engine's self-healing `qmd-collections` step),
then runs `qmd update` + `qmd embed`:

```bash
cairn sync --cron
```

Confirm markdown actually landed before continuing:

```bash
DATA_ROOT="$(cairn config show --json | python3 -c 'import sys,json,os; print(os.path.expanduser(json.load(sys.stdin)["data_root"]))')"
ls -R "$DATA_ROOT" | head -40     # expect per-collection subfolders with .md files
```

If no markdown was produced (the user has no recent sessions in the enabled sources), say so -
the embed will have nothing to index and search will legitimately be empty. Suggest widening a
window (`cairn <source> export --since 30d`) or enabling another source, then re-run 5a.

### 5b. Verify the collections registered

The self-healing engine registers collections during 5a. Verify:

```bash
qmd collection list     # expect the enabled collections that had markdown
```

If a collection with markdown is **missing** (an older engine without self-healing, or a dir
that appeared after the sync), register them yourself - one collection per subfolder that has
markdown (`qmd collection add` is idempotent, so re-adding is safe):

```bash
for dir in "$DATA_ROOT"/*/; do
  name="$(basename "$dir")"
  [ "$name" = "logs" ] && continue
  if find "$dir" -name '*.md' -print -quit | grep -q .; then
    qmd collection add "$dir" --name "$name"
  fi
done
qmd collection list     # re-verify
```

The collection name MUST equal the subfolder name. Do not proceed until `qmd collection list`
shows the collections for the enabled sources that have data.

### 5c. Run the first embed in the background and watch it with Monitor

`cairn sync --cron` already kicked off an embed, but on a large first run its internal step
can hit its timeout, and either way you must **wait for the model download + embed to finish**
before verifying. Run a dedicated embed **detached** (Bash `run_in_background: true`) to a log,
then arm **Monitor** on that log so the conversation isn't blocked.

**Start the embed in the background** (`run_in_background: true`):

```bash
QMD="$(command -v qmd)"
EMBED_LOG="$DATA_ROOT/logs/qmd-embed-setup.log"
mkdir -p "$DATA_ROOT/logs"; : > "$EMBED_LOG"
"$QMD" embed > "$EMBED_LOG" 2>&1
```

**Then arm Monitor on that log.** The filter must catch completion, progress, AND failures -
silence is not success. The embed ends with `✓ Done! Embedded N chunks from M documents`;
download progress prints percentages / "model" / "download"; failures show
`Error`/`Traceback`/`failed`/`Killed`/`OOM`. Give it a generous timeout (the max) for a large
first embed:

> Monitor invocation:
> - **description:** `qmd first embed (model download + embedding) progress`
> - **timeout_ms:** `3600000`  (60 min)
> - **command:**
>   ```bash
>   tail -n +1 -f "$DATA_ROOT/logs/qmd-embed-setup.log" \
>     | grep -E --line-buffered "Done!|Embedded |[0-9]+%|download|model|Error|Traceback|failed|FAILED|Killed|OOM"
>   ```

`tail -n +1 -f` replays lines written before Monitor armed, then streams new ones.

**Detect completion / failure, then stop the monitor:**

- **Success:** Monitor surfaces `✓ Done! Embedded N chunks from M documents` and the
  background command exits 0. Stop the monitor (the `tail -f` won't exit on its own) and go to
  step 6. Note N/M; if `Embedded 0 chunks`, search will be empty - investigate (usually 5a
  produced no markdown or 5b registered nothing) before continuing.
- **Failure:** Monitor surfaces `Error`/`Traceback`/`Killed`/`OOM`, or the background command
  exits non-zero. Stop the monitor, `Read` the full `$EMBED_LOG`, and **stop and report** - do
  not verify a broken index.
- **Silence/timeout:** `Read` the tail of `$EMBED_LOG`. Active download lines mean it's still
  working - re-arm Monitor. Otherwise report it as wedged.

## 6. Register the hourly sync

```bash
cairn cron install     # absolute-path 'cairn sync --cron' entry, safe under cron's PATH
```

The plugin's `SessionEnd` hook (`cairn sync --hook`) is registered automatically when the
plugin is enabled - nothing to do here.

## 7. Verify that search actually works (not just the plumbing)

A green `cairn doctor` once told a user "all good" while search was completely broken. Verify
in layers; declare success only when a **real query returns hits**.

**7a. Plumbing + the new index checks.** `cairn doctor` now also checks collections-registered
and embeddings-present:

```bash
cairn doctor           # install, config, qmd, data_root, stores, collections, embeddings, cron
crontab -l             # the hourly 'cairn sync --cron' line is present
```

The collections/embeddings lines are warnings, not failures - but after step 5 they should be
green. If they aren't, fix that before continuing.

**7b. The index has content.** An empty index is the exact failure mode we're guarding against:

```bash
qmd status             # expect documents > 0 AND vectors > 0
qmd collection list    # expect the enabled collections, each with a non-zero file count
```

If `qmd status` shows 0 documents or 0 vectors, search cannot work: 0 documents → revisit
5a/5b; documents but 0 vectors → revisit 5c. **Stop and report** - do not call setup done.

**7c. A real query returns results (the finish line).** Pick a term you expect to exist (a
title/filename you saw under `$DATA_ROOT` in 5a, or the user's project name) and search:

```bash
qmd search "<term-you-expect>" -n 5
```

Expect ranked hits with titles, scores, and snippets. If nothing, try another term and one
`qmd query "<term>"` (hybrid) to rule out a BM25-only miss. If real queries consistently
return nothing despite `qmd status` showing documents and vectors, **stop and report** with
the `qmd status` output and the queries you tried.

Only when 7a-7c all pass: tell the user setup is complete. Summarize what's enabled
(`cairn config show`), how many chunks/documents were embedded (from 5c), and that `/recall`
and `/search` now work. Mention the hourly cron and the SessionEnd hook keep it fresh, and
that subsequent embeds are incremental (seconds, not minutes).

## Troubleshooting

- **`cairn` command not found** - install bin dir not on PATH; see step 3.
- **`qmd embed` re-downloads models every run** - should be first-run only; if it repeats, the
  model cache dir (`~/.cache/qmd/models`) may be unwritable.
- **Embed seems hung** - it's the ~2GB download. `Read` `$EMBED_LOG`; download/percentage lines
  mean progress. Re-arm Monitor rather than killing it.
- **`cairn doctor` green but `/search` empty** - exactly the regression this skill guards
  against. Run 7b/7c; a green doctor is not proof of a working index on its own.
