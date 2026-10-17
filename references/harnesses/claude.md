# Claude execution

Use `Bash` for shell commands and `Read`/`Edit` for files. Collect missing settings with
`AskUserQuestion` when available, otherwise ordinary conversation.

## Long work: the first sync and embed

A foreground `Bash` call dies long before a ten-minute model download finishes, so run
`cairn sync --cron` detached and watch its log. The engine writes to
`<data_root>/logs/context-sync.log`; get `DATA_ROOT` from
`cairn config show --json` (key `data_root`).

1. Start it with `run_in_background: true`:

   ```bash
   cairn sync --cron
   ```

   Keep the task ID so you can read its real exit status later.

2. Arm `Monitor` on the log when the tool is available. The filter must catch completion,
   progress AND failures; silence is not success. The embed ends with
   `✓ Done! Embedded N chunks from M documents` and a `[qmd-verify]` line with the
   index-wide document/vector/pending counts; download progress prints percentages,
   "model" or "download"; failures show `Error`/`Traceback`/`failed`/`Killed`/`OOM`.

   > Monitor invocation:
   > - **description:** `cairn first sync (model download + embed) progress`
   > - **timeout_ms:** `3600000` (60 min)
   > - **command:**
   >   ```bash
   >   tail -n +1 -f "$DATA_ROOT/logs/context-sync.log" \
   >     | grep -E --line-buffered "Done!|Embedded |[0-9]+%|download|model|Error|Traceback|failed|FAILED|INCOMPLETE|qmd-verify|coalesced|skipped|Killed|OOM|=== (done|FAILED) ==="
   >   ```

   `tail -n +1 -f` replays lines written before Monitor armed, then streams new ones.

3. Decide from evidence, then stop the monitor (the `tail -f` never exits on its own):
   - **Success:** the log ends with `=== done ===` and the background task exits 0.
     Note N/M. Zero newly embedded chunks can mean the existing index is already
     current; check total documents/vectors and a real query before declaring success.
   - **Failure:** the log shows `=== FAILED ===` or an error line, or the task exits
     non-zero. `Read` the full log and stop and report; do not verify a broken index.
     The `failed steps:` line before `=== FAILED ===` names what failed.
     `[qmd-embed] FAILED - qmd exited 0 but reported: N chunks failed` or
     `[qmd-verify] INCOMPLETE` means QMD stopped early (QMD 1.1.5 and 2.5.3 cap one embed
     session at 30 minutes). Completed documents keep their vectors; a partly embedded
     document restarts. Rerun only while the pending count in `cairn doctor` falls.
     The embed line says `exited 0`, not `ok`; the `[qmd-verify]` line is the verdict.
   - **Skipped:** `=== context-sync (...) skipped: another cairn sync is running ... ===`
     means your run exited 0 without doing work. The running sync runs its missing
     stages (a `[coalesced]` line) before its own `=== done ===` or `=== FAILED ===`;
     judge that instead.
   - **Silence or timeout:** `Read` the tail of the log. Download or percentage lines
     mean it is still working, so re-arm Monitor. Otherwise report it as wedged.

Without `Monitor`, re-read the log tail with `Read` every few minutes and check the
background task's exit status; never treat elapsed time as completion.

Invoke `/cairn:setup` explicitly; other workflows are `/cairn:cairn`, `/cairn:recall`, and
`/cairn:search`. `disable-model-invocation` keeps setup explicit. Read the selected source's
reference for native data, independently of Claude as the executing harness.

Use the current session ID supplied by Claude or verified `CLAUDE_CODE_SESSION_ID` (the
variable Claude Code exports to every tool process; `CLAUDE_SESSION_ID` is only a template
variable inside skill text and is never set in a shell); pass it
explicitly to mutations. The plugin manifest is `.claude-plugin/plugin.json`.
Update the engine with `uv tool upgrade cairn`, then `/plugin update cairn@cairn` and
`/reload-plugins`. Re-run `cairn hook install` after an engine reinstall.

Bundled `hooks/hooks.json` supplies SessionEnd through `hooks/launch.sh`. `cairn hook
install` records absolute engine/config paths in `hook-launcher.env`; it does not add
another global hook. Claude gives every SessionEnd hook a shared 1.5 s budget that a
plugin-declared timeout cannot raise, so the handler only queues the event and starts a
detached worker. End a real session, then inspect `cairn hook status --harness claude`
and search a distinctive phrase from that session. An isolated shell invocation of the
handler is not host lifecycle verification.

## Peer runs and fan-out health

Start `cairn invoke <peer> … --json` with `Bash` `run_in_background: true` and keep the task
id; the JSON result line arrives on the task's stdout when it exits, and the host sends a
completion notice. Read the first stderr line for the thread id. Mid-run, read
`cairn invoke status <id>`; with `Monitor` available, watch the thread's `events-NN.jsonl`
under `<data_root>/logs/invoke/<peer>/<id>/`. Stop the monitor afterwards. Silence proves
nothing: a `silent` activity label past the stall threshold means a probable hang.

For a fan-out of `Agent` subagents plus peer runs, `cairn agents list` shows every item the
current session spawned with its execution state, activity and outcome. Run
`cairn agents wait --session claude:<id>` in the background to get one completion notice when
the last one exits. After a context reset, run `cairn agents list` before resuming work.

Skills: `/cairn:invoke-codex` and `/cairn:invoke-claude`. Both may be invoked by the model
when the user asks for a peer run or a quality gate; the engine's read-only default profile,
timeouts and depth guard bound what a run can do.
