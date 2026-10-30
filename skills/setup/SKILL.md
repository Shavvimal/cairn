---
name: setup
description: Install or update Cairn on this machine, including its engine, plugin, hooks, scheduled sync, and search verification. Preserve existing configuration and stores during updates. Use only when the user explicitly invokes Cairn setup, update, or onboarding. Side-effectful - never auto-invoke.
disable-model-invocation: true
allowed-tools: Bash, Read, Edit, AskUserQuestion, Monitor
---

Read [the harness contract](../../references/harnesses/contract.md) and its relevant
Claude or Codex reference from this installed package before host-specific operations.


# Install or update Cairn

Guided, user-initiated installation or update. Verify that **search actually returns
results** and report host lifecycle checks separately. Because this skill installs
software and can write configuration and the crontab, only the user can trigger it
(Claude `disable-model-invocation: true`; Codex `allow_implicit_invocation: false`).

## Choose the workflow first

- **Existing installation or update request:** read [Updating an existing installation](references/updating.md)
  and follow that workflow. Detect the current installation with `command -v cairn`,
  `cairn --version`, and `cairn config show --json` when available. An existing config or
  store still counts if the CLI is missing or broken; repair the engine without resetting
  the user's setup. Check `CAIRN_CONFIG` or the per-user config under
  `${XDG_CONFIG_HOME:-$HOME/.config}/cairn/config.json` before assuming a fresh install.
- **First installation:** follow steps 1–7 below. Collect only missing choices.

**Stop and report if a required step fails; do not silently continue.** A green
`cairn doctor` alone does not establish working search. Do not run the fresh-install
configuration steps during an update.

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
echo "rg:   $(command -v rg   || echo 'MISSING (optional)')"
```

Decide what's missing and how to close it, then use the harness input mechanism to let the user pick,
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
- **`rg` (ripgrep) missing** → optional: it powers exact-match and regex search in `/search`
  and `/recall`. Offer `brew install ripgrep` (or the system package manager). A missing
  `rg` never blocks setup - the skills fall back to BM25.
- **prefer `pipx` and it's missing** → `brew install pipx` (or
  `python3 -m pip install --user pipx && python3 -m pipx ensurepath`). Only needed if they
  decline `uv`.

After any install, **re-run the preflight block** and confirm the tool resolves. Do not
proceed until `qmd` resolves **and** at least one of `uv`/`pipx` resolves.

A resolving `qmd` can still be too old or unable to run. Record its version and prove it
opens its index (a binary that prints a version but crashes on `status` is not usable):

```bash
qmd --version   # cairn is tested with 2.5.3; record what you see
qmd status      # must exit 0 and print a Documents block
node --version  # context only: the qmd wrapper can use a different runtime
```

If `qmd status` crashes, stop and report it with the version and the output. On macOS Apple
Silicon with Node 22.12.0, QMD 2.8.3 crashed in its native SQLite module and 2.5.3 worked
(one machine, not a support matrix); `npm install -g @tobilu/qmd@2.5.3` is the known-good
recovery there. Do not recommend "latest" as a guaranteed fix.

## 2. Ask the user what to set up

Collect missing choices through the harness input mechanism. Keep the **session sources** and **service docs** as *separate*
questions - they are configured differently (sessions point at one app store each; service
docs point at one-or-more arbitrary docs folders you must collect), and bundling them on one
checkbox is confusing.

- **data_root** - directory for exported markdown (default `~/.cairn/.context`; they may
  point it anywhere, e.g. a git-tracked notes repo).
- **session integrations** (multi-select) - which of **claude / codex / antigravity / cursor / granola** to
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
cairn config init --data-root "<data_root>" --enable claude,codex,antigravity,cursor,granola
# Pre-select antigravity when `command -v agy` succeeds (the agy CLI is installed).
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

Run `cairn sync --cron` using the harness's long-process tools and retain the process
handle. This exports enabled sources, registers nonempty collections, updates QMD, and
embeds. **The first embed downloads ~2GB of GGUF models and can take 10+ minutes**, far
longer than a foreground shell call survives. Inspect bounded output from
`<data_root>/logs/context-sync.log` and wait for the actual process exit code.

For example, in Claude Code run it with Bash `run_in_background: true` and watch the log
with `Monitor` when available; in Codex start it with `exec_command` and poll with
`write_stdin`. Your harness reference (`references/harnesses/claude.md` or `codex.md`)
has the exact recipe, including the log filter and what success, failure and silence
look like.

- Exit 0: inspect `qmd collection list` and `qmd status`. Collections with exported
  Markdown must be registered, and a nonempty corpus must have embeddings.
- Nonzero exit: read the failed source/index step in the log, fix the cause and retry.
  Do not treat a partly populated index as completed setup.
- Silence: inspect the process status and log. Download or percentage lines mean it is
  still working, so keep observing with the host tools. A timeout is not success; if
  wedged, report the evidence.
- No exported data: report an empty installation honestly. Widen a source's window with
  `cairn SOURCE export --since 30d` if the user wants older history, then sync again.
  Do not invent a search result or claim a populated index.

Do not start a second embedding process while sync is still running. A second
`cairn sync` for the same data root logs one `skipped: another cairn sync is running` line
and exits 0; the running sync runs that request's missing stages before it exits. If embedding failed, retry `cairn sync --cron` after the
running job ends and the error is resolved.

QMD exits 0 even when chunks fail, so cairn checks the embed output and then `qmd status`:
`[qmd-embed] FAILED - qmd exited 0 but reported: N chunks failed` fails the sync. The
embed step logs `exited 0` on a clean exit; the `[qmd-verify]` line gives the verdict with
the index-wide documents, vectors and pending documents, and `INCOMPLETE` (pending > 0)
also fails the sync. A first embed of a large history can pass QMD's 30-minute session
limit (1.1.5 and 2.5.3). Completed documents keep their vectors, but QMD 2.5.3 removes the
vectors of a partly embedded document, so it restarts on the next run. Rerun
`cairn sync --cron` while the pending count in `cairn doctor` falls; if it stops falling,
stop and report it (a document may never fit in one session).

## 6. Configure automatic sync and periodic maintenance

```bash
cairn hook install --harness HOST  # HOST is claude or codex from the adapter
cairn hook install --harness antigravity  # when antigravity is enabled: agy's Stop hook in ~/.gemini/config/hooks.json
cairn cron install     # absolute-path 'cairn sync --cron' entry, safe under cron's PATH
```

Verify the bundled hook through the harness reference. On the Codex versions in the
harness reference's verified table, `cairn hook install --harness codex` also writes the
trust entry; confirm it with `cairn doctor --harness codex` (`Codex hook trust - trusted`).
On another Codex version, Codex requires review/trust in `/hooks`; file installation does
not establish readiness there. Use the deterministic install command to inspect bundled discovery and configure only
the needed fallback; do not hand-edit host settings. End a real session, inspect `cairn hook status --harness HOST`, wait for `completed`,
and search a distinctive phrase from that session. If this cannot be exercised in this
client, report lifecycle verification as incomplete. Cron is maintenance/recovery, not a
replacement for the session hook.

## 7. Verify that search actually works (not just the plumbing)

A green `cairn doctor` once told a user "all good" while search was completely broken. Verify
in layers; declare success only when a **real query returns hits**.

**7a. Plumbing + the strict index check.**

```bash
cairn doctor --strict-index   # install, config, qmd version + runtime, collections, index, cron
crontab -l                    # the hourly 'cairn sync --cron' line is present
```

`--strict-index` fails unless QMD runs, its status is readable, and a nonempty index has
vectors with zero pending documents. An empty index passes but says no search can be
verified. Plain `cairn doctor` only warns about these and says so in its summary line.

**7b. The index has content.**

```bash
qmd status             # documents > 0, vectors > 0, no Pending line
qmd collection list    # expect the enabled collections, each with a non-zero file count
```

Zero documents means nothing can be found: revisit step 5. Documents without vectors (or a
Pending line) leave BM25 keyword search working but semantic search incomplete: rerun
`cairn sync --cron` and report it; do not call semantic search ready. **Stop and report**
if it stays that way.

**7c. Real queries return results (the finish line).** Pick a distinctive phrase from a
known exported conversation (a title/filename you saw under `$DATA_ROOT` in step 5) and run
all three:

```bash
qmd search "<exact term>" -n 5          # keyword (BM25)
qmd vsearch "<paraphrase of it>" -n 5   # vector only
qmd query "<paraphrase of it>" -n 5     # hybrid
```

Each must return the expected document. A hybrid hit alone can come from the keyword half,
so it does not prove vector retrieval. If keyword works and vector does not, report
semantic search as not ready with the `qmd status` output and the queries you tried.

Only when 7a-7c and the live lifecycle check all pass: tell the user setup is complete. Summarize what's enabled
(`cairn config show`), how many chunks/documents were embedded (from step 5), and that `/recall`
and `/search` now work. Mention the hourly cron and the SessionEnd hook keep it fresh: a
finished session is exported and keyword-indexed at once, and its embeddings follow on the
next full sync. Incremental embeds are usually short but can take a long time after a large
backfill.

## Troubleshooting

- **`cairn` command not found** - install bin dir not on PATH; see step 3.
- **`qmd embed` re-downloads models every run** - should be first-run only; if it repeats, the
  model cache dir (`~/.cache/qmd/models`) may be unwritable.
- **Embed seems hung** - it's the ~2GB download. Inspect `<data_root>/logs/context-sync.log`; download/percentage lines
  mean progress. Continue observing with the harness tools.
- **`cairn doctor` green but `/search` empty** - exactly the regression this skill guards
  against. Run 7b/7c; a green doctor is not proof of a working index on its own.
