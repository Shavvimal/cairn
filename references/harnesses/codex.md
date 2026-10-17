# Codex execution

Use the shell execution tool available in this client (for example `exec_command`). Do
not use Claude-only `Bash` background flags or require `Monitor`. Use available file
reads/patches.

## Long work: the first sync and embed

A single shell call cannot wait out a ten-minute model download, so run
`cairn sync --cron` as a long-lived process and poll it. The engine writes to
`<data_root>/logs/context-sync.log`; get `DATA_ROOT` from `cairn config show --json`.

1. Start it with `exec_command` and retain the returned session/process handle:

   ```bash
   cairn sync --cron
   ```

2. Poll with `write_stdin` on that handle (an empty write returns new output) until the
   process exits, and read bounded log output between polls:

   ```bash
   tail -n 20 "$DATA_ROOT/logs/context-sync.log"
   ```

   Completion prints `✓ Done! Embedded N chunks from M documents`, a
   `[qmd-verify]` line with the index-wide document/vector/pending counts, and then
   `=== done ===`; progress prints percentages, "model" or "download"; failures show
   `Error`/`Traceback`/`failed`/`Killed`/`OOM`, a `failed steps:` line and `=== FAILED ===`.

3. Decide from evidence: exit 0 plus `=== done ===` is sync success. Note N/M; zero new
   chunks can mean the index is already current, so verify total documents/vectors and
   a real query. A non-zero exit or `=== FAILED ===` means stop and report with
   the log; `failed steps:` names what failed. `[qmd-embed] FAILED - qmd exited 0 but
   reported: N chunks failed` or `[qmd-verify] INCOMPLETE` means QMD stopped early
   (QMD 1.1.5 and 2.5.3 cap one embed session at 30 minutes). Completed documents keep
   their vectors; a partly embedded document restarts. Rerun only while the pending
   count in `cairn doctor` falls. The embed line says `exited 0`, not `ok`; the
   `[qmd-verify]` line is the verdict. Silence with download lines means keep polling; silence without them means
   report it as wedged. Elapsed time is never completion.
   If the only new line is `=== context-sync (...) skipped: another cairn sync is
   running ... ===`, your run exited 0 without doing work: the running sync runs its
   missing stages (a `[coalesced]` line) before its own `=== done ===` or
   `=== FAILED ===`; judge that instead.

Collect missing settings with an available user-input tool. Some synchronous input tools
are restricted to Plan mode; use asynchronous input or ordinary conversation otherwise.
Keep required answers pending and continue only independent work; elapsed time is not an
answer or permission.

Use `/skills` or the `$` picker to select the installed `setup`, `cairn`, `recall`, or
`search` skill. Run setup only on explicit request or explicit onboarding. The setup
policy `allow_implicit_invocation: false` is packaged in `agents/openai.yaml`.

Prefer the current thread ID provided by the host. `CODEX_THREAD_ID`/`CODEX_SESSION_ID`
can be inherited from a parent client: do not use them if they conflict with the selected
thread. `CLAUDE_PLUGIN_ROOT` is also supplied by Codex for compatibility and does not
identify Claude as the active harness. For operations, explicitly pass the correct native
ID to `cairn codex note|close|log --session-id ID`.

The authoritative manifest is root `plugin.json`; `.codex-plugin/plugin.json` is a generated
compatibility projection required by the tested CLI 0.160.0. Update with:

```sh
uv tool upgrade cairn
codex plugin marketplace upgrade cairn
codex plugin add cairn@cairn
cairn hook install --harness codex
```

Restart Codex. Review/trust the exact Cairn hook in `/hooks`; installation alone does
not grant trust. `cairn hook status --harness codex` reports the client’s registration/trust state separately from observed events and results. A changed hook may require review again. The explicit hook file is `hooks/codex.json`. `cairn hook install --harness codex`
inspects the actual client: if bundled discovery works it removes any previous fallback;
otherwise it adds only a Cairn-owned user hook and preserves unrelated entries. Tested
0.160.0 and 0.163.0 omit hooks from the portable manifest, so need this fallback.
Run `cairn hook uninstall --harness codex` before removing the Codex plugin; it preserves
the shared launcher/data used by Claude. Disable/removal also gates the fallback at runtime.

SessionEnd runs when the main session ends (normal client shutdown, archive/delete of an
open conversation, or the host's idle timeout). Switching conversations does not immediately
end one. End a real session and check completed sync plus a QMD search for its distinctive
text, without waiting for cron. The three-second callback hands work to a detached process.

## Peer runs and fan-out health

Run `cairn invoke <peer> … --json` with `exec_command` and a long `yield_time_ms`, keep the
session handle, and poll with `write_stdin` (an empty write returns new output) until it
exits; the last stdout line is the JSON result. Read the first stderr line for the thread id
and poll `cairn invoke status <id>` between writes. Run invoke with escalated permissions:
the sandbox hides the login keychain (`claude -p` reports "Not logged in"), blocks the peer's
network, and denies writes under `~/.codex` and `~/.claude`. Never start a nested
`codex exec` inside the sandbox; the engine refuses depth 1 without `--allow-nested`.

`cairn agents list` shows every peer run and subagent of the current thread with execution
state, activity and outcome; `cairn agents wait --session codex:<id>` blocks until the last
one exits. The `invoke-codex` and `invoke-claude` skills ship with
`allow_implicit_invocation: true`, so a request in conversation can start them; setup stays
explicit.
