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

The recall-first block (`cairn instructions install --harness codex`) goes into the Codex
home (`$CODEX_HOME`, else `~/.codex`). Codex loads `AGENTS.override.md` there in place of
`AGENTS.md` when the override is not empty (checked on CLI 0.165.0), so cairn writes to
the override when it holds text and to `AGENTS.md` otherwise. If an override appears after
an install, the next install writes the override and names the old copy in `AGENTS.md`;
`cairn instructions remove --harness codex` clears both and records the opt-out.

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

Restart Codex. On a verified Codex version (table below), `cairn hook install --harness
codex` also trusts the fallback it writes: it adds `[hooks.state."<hooks.json
path>:session_end:<group>:0"]` with `trusted_hash` to `$CODEX_HOME/config.toml`. The
command embeds the engine path and the background environment, so a reinstall after either
changes refreshes the trust entry in place; a reinstall with neither changed writes nothing. On any other version, or with
`--no-trust`, review/trust the exact Cairn hook in `/hooks`: installation alone does not
grant trust there. `cairn hook status --harness codex` reports the client’s
registration/trust state (`host_hooks`) and the trust state read from the files
(`codex_trust`) separately from observed events and results. `cairn doctor --harness codex`
prints one `Codex hook trust` line: `trusted`, `stale`, `missing`, `disabled` or `manual`.
The explicit hook file is `hooks/codex.json`. `cairn hook install --harness codex`
inspects the actual client: if bundled discovery works it removes any previous fallback;
otherwise it adds only a Cairn-owned user hook and preserves unrelated entries. Tested
0.160.0 and 0.163.0 omit hooks from the portable manifest, so need this fallback.
Run `cairn hook uninstall --harness codex` before removing the Codex plugin; it preserves
the shared launcher/data used by Claude. Disable/removal also gates the fallback at runtime.

### Hook trust written by cairn

Codex stores trust as `trusted_hash = "sha256:<hex>"` over the canonical JSON of one
handler: `{"event_name": "<snake_case event>", "hooks": [{"type", "command", "timeout"
(600 when absent), "async": false, "statusMessage" (when present)}]}`, keys sorted, no
spaces. The format is not documented by Codex, so cairn writes it only on the versions
where it recomputed a live entry:

| Codex | Verified | How |
| --- | --- | --- |
| 0.165.0 | 2026-10-30 | Recomputed all four `hooks.state` hashes of a live `config.toml` from its `hooks.json` |

The accepted range runs from the lowest verified version to the end of the highest
verified minor (today `>= 0.165.0, < 0.166.0`). Outside it the install prints
`Codex <version> is outside the versions cairn verified the trust hash on (...)` and the
manual `/hooks` step. To add a version, recompute a live entry and add a row here and to
`TRUST_FORMAT_VERIFIED` in `src/cairn/harnesses/codex_trust.py`.

Files and safety rules:

- `$CODEX_HOME/cairn-hook-receipt.json`: the receipt. One entry per hooks.json group and
  trust table cairn wrote, each with a sha256. It replaces the earlier
  `<XDG config>/cairn/codex-hook-registration.json`, which the next install or uninstall
  reads once and deletes.
- `$CODEX_HOME/config.toml.cairn-bak`: the bytes of `config.toml` before cairn's last edit,
  with the same mode (beside the target file when `config.toml` is a symlink).
- cairn edits only whole `[hooks.state."<key>"]` tables. Before it replaces the file, the
  new text must parse back to the original with only the planned keys changed, and the file
  must be unchanged since cairn read it (Codex does not take cairn's lock). cairn stages
  and fsyncs the backup and the new file first and checks just before the rename, so only
  a Codex save in that last instant can be lost. A symlinked `config.toml` or `hooks.json`
  stays a symlink, and the file mode is kept.
- A table at cairn's key with any key besides `trusted_hash` (for example
  `enabled = false`) is left alone, with a warning.
- After the write, cairn asks Codex (`hooks/list`) once more. If Codex lists the hook with
  a `trustStatus` other than `trusted`, cairn removes the table it wrote in that run (no new
  backup; a `config.toml` it created is deleted) and prints the `/hooks` step. If `hooks/list` fails, the table stays and the success line ends with
  `(Codex did not answer hooks/list: <reason>)`. If it does not list the hook or gives no
  status, the table stays and the success line has no note.
- Reinstall replaces cairn's group only while its sha256 equals the receipt's. If you
  edited the group (timeout, matcher, status message, an added handler), install stops
  and changes nothing:
  restore or remove the group, then run it again. A group whose command you changed is no
  longer cairn's; install appends a new one.
- During a reinstall the receipt keeps the earlier and the planned entries until the
  writes finish, so an interrupted run can still be uninstalled or completed.
- Uninstall removes a table or group only while its sha256 equals the receipt's; a changed
  entry stays and gets a warning. Removing the group shifts later SessionEnd groups down
  one index, so Codex asks to trust them again; cairn warns about that and does not
  re-key tables it did not write.

```sh
cairn hook install --harness codex --dry-run    # print every planned write; cairn writes nothing
cairn hook install --harness codex --no-trust   # register only; trust in /hooks
cairn hook uninstall --harness codex --dry-run  # print the planned removals
```

`--dry-run` writes no file and takes no lock. Install still asks Codex for its loaded hooks
(`codex app-server`, `hooks/list`), as a real install does, and Codex itself can create its
state files in `$CODEX_HOME` while it answers.

SessionEnd runs when the main session ends (normal client shutdown, archive/delete of an
open conversation, or the host's idle timeout). Switching conversations does not immediately
end one. End a real session and check completed sync plus a QMD search for its distinctive
text, without waiting for cron. The three-second callback hands work to a detached process.

## Peer runs and fan-out health

Run `cairn invoke <peer> … --json` with `exec_command` and a long `yield_time_ms`, keep the
session handle, and poll with `write_stdin` (an empty write returns new output) until it
exits; the last stdout line is the JSON result. Read the first stderr line for the thread id
and poll `cairn invoke status <id>` between writes, or run `cairn invoke wait <id> --json`
for one blocking call that prints the result line; `cairn invoke tail <id> --no-follow`
dumps the events so far, one line each. Run invoke with escalated permissions:
the sandbox hides the login keychain (`claude -p` reports "Not logged in"), blocks the peer's
network, and denies writes under `~/.codex` and `~/.claude`. Never start a nested
`codex exec` inside the sandbox; the engine refuses depth 1 without `--allow-nested`.

`cairn agents list` shows every peer run and subagent of the current thread with execution
state, activity and outcome; `cairn agents wait --session codex:<id>` blocks until the last
one exits. The `invoke-codex` and `invoke-claude` skills ship with
`allow_implicit_invocation: true`, so a request in conversation can start them; setup stays
explicit.
