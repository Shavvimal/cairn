# Updating an existing installation

Use this workflow when setup is invoked on an existing installation. Preserve the
user's configuration, enabled sources, native stores, `data_root`, Markdown, QMD
database, and scheduling choices. An update does not require `cairn config init`.

## 1. Record the current installation

Read `cairn --version` and `cairn config show --json`. Identify the engine's installer
(`uv` or `pipx`), any pinned Git revision, and enabled extras such as Granola. Preserve
the installation method and extras; do not replace an editable developer checkout or
silently change a deliberately pinned release.

Inspect the installed plugin manifest for the active host, using the paths in the
[harness contract](../../../references/harnesses/contract.md). Read the current Cairn
cron entry with `crontab -l` and identify which hosts already have Cairn installed.
No crontab is a valid choice; a permission/read error is not evidence that none exists.

Use the shell environment that selects the existing store and index. Check
`CAIRN_CONFIG`, `QMD_CONFIG_DIR`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `INDEX_PATH`, and
any custom `CODEX_HOME` or `CLAUDE_CONFIG_DIR` against the existing hook launcher and
cron entry. Resolve any mismatch before registering replacements: a host shell may
lack the overrides used for the original installation. Do not default to a new index.
Use the configured `qmd_binary` for QMD checks below.

## 2. Update the engine, then the plugin

Cairn's engine and plugin share a release version but update separately. For a normal
uv installation:

```bash
uv tool upgrade cairn
cairn --version
```

For a pipx installation use `pipx upgrade cairn`. If the engine is missing, repair it
with the original installer and extras using setup's engine-install instructions,
then resume here without rewriting the config.

Update each already-installed host's plugin using its adapter:
[Claude](../../../references/harnesses/claude.md) or
[Codex](../../../references/harnesses/codex.md). Follow its reload/restart instructions
and continue with the updated installed skill if a restart interrupts this workflow.
Do not add a second host just because its transcripts are enabled as a data source.

If an old setup skill only supports fresh installs, the plugin may have been refreshed
first to obtain this workflow. Continue with the engine upgrade; the bundled launcher
tolerates the temporary mismatch, but verify matching versions before calling the
update complete.

## 3. Refresh existing automatic sync

From the verified environment in step 1, run this for each already-installed host:

```bash
cairn hook install --harness HOST  # replace HOST with claude or codex
```

If the user already uses Cairn cron, refresh its entry with `cairn cron install` and
check that its configured schedule is retained. Leave cron absent if it was absent;
do not enable sources or hook policies the user had disabled.

Hooks and cron record absolute engine/config paths and the effective QMD environment,
including default paths. Repeat this registration after future engine reinstalls or
intentional changes to those paths. Restart the affected hosts and review/trust changed
Codex hooks in `/hooks`; installing a hook does not establish trust.

## 4. Run incremental sync and verify

Run `cairn sync --cron` and follow the active harness's long-process instructions.
Wait for its actual successful exit and inspect `<data_root>/logs/context-sync.log`.
Use `--cron` even if scheduling is disabled: it selects the incremental sync mode and
does not install a cron job. `cairn sync --all` currently performs the same incremental
work as `--cron`; neither forces all existing vectors to be regenerated.

Check the installed package, not a manifest in the current working repository:

```bash
cairn doctor --harness HOST --plugin-manifest /absolute/installed/package/plugin.json
```

For Claude use the installed `.claude-plugin/plugin.json`; for Codex use root
`plugin.json`. Check every host updated. Investigate version drift or unexpected
configuration changes. An intentionally absent cron entry is not an update failure.

Inspect `qmd status` and `qmd collection list` in the same environment. Search for a
known term from an existing export with `qmd search "<known term>" -n 5`. Zero **new**
embedded chunks is normal when nothing changed; verify the existing document/vector
counts and search results. An empty corpus cannot establish working search.

Verify each enabled host hook by ending a real session, checking
`cairn hook status --harness HOST` for that session's completed request, and finding
its distinctive text through QMD without waiting for cron. If this cannot be exercised
in the current client, state that lifecycle verification remains pending. Do not claim
it passed based on registration, old completed requests, or manual sync.

Report the engine/plugin versions, preserved store/index paths, refreshed hooks and
existing schedule, search evidence, and any pending restart/trust/lifecycle checks.

## Store compatibility when upgrading to 2.4.0

No destructive migration or full index rebuild is required. Cairn's release is
**2.4.0** and its exported metadata uses `cairn_schema: "1"`; there is no separate
"v2 store" to create. Legacy Markdown remains readable. Metadata upgrades when a
session is next exported from its native source.

A legacy `date` is a local-day label without its original timezone. Recall keeps
that label as its fallback day. Once native timestamps are available on re-export,
recall uses their UTC activity days, which can differ near midnight. This is a
change in date interpretation, not lost history.

For more complete activity dates, message counts, and project filtering across older
history, explain the optional backfill and its embedding cost. Run it only if the
user wants that historical upgrade:

```bash
cairn SOURCE export --all  # one invocation per enabled claude/codex/cursor/granola source
cairn sync --cron
```

Replace `SOURCE` with each relevant source name. This preserves existing filenames,
titles, notes, comments, status, tags, and ratings. If a native transcript is no longer
available, its Markdown remains intact and recall uses the metadata it still has.
Use recall's `--all-projects` for old records without working-directory metadata.

Re-exporting legacy files changes their content hashes, so a large backfill can
require substantial re-embedding of those changed documents. Unchanged documents
retain their vectors. Keep the QMD database and model cache; deleting them or forcing
a full re-embed is unnecessary for this upgrade.
