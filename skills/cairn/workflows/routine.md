# Routine

Run a prompt file through a peer agent (`claude` or `codex`) once a day, gate the result
with your own `validate` command, and keep a stamp per UTC day. A routine is config as data
under `routines`; `cairn routine` installs, runs and reports it. Scheduling is launchd only
(macOS) in this version.

## Behavior

- launchd starts `cairn routine run NAME` daily at `at` (local time)
- The run handles every UTC day in the last `catch_up_days` without a done stamp, oldest first
- It refreshes the listed `sources` once, then per day: render the prompt, run `prepare`,
  run `cairn invoke <peer> --owner routine:NAME`, run `validate`
- A day is done only after `validate` passes
- One run at a time per routine; a second start logs `skipped-lock` and exits 0
- A paused or disabled routine skips every run until `cairn routine resume NAME`

## 1. Define the routine

Start from the `example-daily` entry that `cairn config init` writes, or create one:

```bash
cairn config set routines.daily '{"peer": "claude", "prompt_file": "~/projects/site/prompts/daily.md", "cd": "~/projects/site", "profile": "scheduled", "at": "05:20", "validate": ["true"]}'
cairn config set routines.daily.sources claude,codex
cairn config set routines.daily.prepare '["cairn", "recall", "digest", "{{DAY}}", "--all-projects", "--min-msgs", "1", "--json", "--out", "{{DIGEST_DIR}}"]'
cairn config set routines.daily.validate '["pnpm", "--dir", "~/projects/site", "validate", "--day", "{{DAY}}"]'
cairn config set routines.daily.outputs.inbox_dir "~/projects/site/inbox"
```

| Field | Required | Rule |
| ----- | -------- | ---- |
| `peer` | yes | `claude` or `codex` |
| `prompt_file`, `cd` | yes | absolute after `~` and `{{DATA_ROOT}}` expansion; no `$VARS` |
| `at` | yes | `HH:MM`, 24-hour local time |
| `validate` | yes | argv list (JSON array), never a shell string |
| `profile` | no | a built-in or a name in `invoke.<peer>.profiles`; default `invoke.<peer>.profile` |
| `model`, `timeout_s` | no | default from `invoke.<peer>` |
| `sources` | no | windowed session sources whose collection is enabled |
| `catch_up_days` | no | 1 to 30, default 3 |
| `budget_usd_per_day` | no | Claude only; the remainder goes to `--max-budget-usd` |
| `outputs` | no | named directories; `journal_dir` defaults to `{{DATA_ROOT}}/journal` |
| `prepare` | no | argv list, or null |
| `terminal_hook` | no | argv list with an absolute first element, or null; no template variables |
| `stale_sources` | no | `warn` (default), `refuse` or `ignore` |

A scheduled peer usually needs a config profile with its own permissions. Claude:

```bash
cairn config set invoke.claude.profiles.scheduled '{"base": "edit", "extra_args": ["--permission-mode", "dontAsk", "--allowedTools", "Read,Glob,Grep,Edit,Write,Bash(pnpm:*)", "--disallowedTools", "WebFetch,WebSearch,Bash(pnpm publish:*),Bash(git push:*)", "--add-dir", "{{JOURNAL_DIR}}"]}'
```

`--allowedTools` and `--disallowedTools` each take one comma-joined string. Deny beats
allow, so the profile above runs pnpm scripts but never publishes or pushes. `Bash(cmd *)`
and `Bash(cmd:*)` are the same trailing wildcard; the permission dialog writes the first.
A path-scoped rule needs the `//` absolute form: `Edit(/{{JOURNAL_DIR}}/**)` renders to
`Edit(//...)`. A profile cannot drop its base's deny rules or switch to a looser permission
mode, and `--settings`, `--mcp-config` and `--permission-prompt-tool` need base `full`.
Codex profiles take only `--add-dir DIR` and `-c KEY=VALUE` pairs.

Run `cairn config show` to see the routine with its resolved paths.
`cairn config unset routines.daily.prepare` removes an optional field. Disabling a
collection (`cairn config set codex.enabled false`) removes that source from every
routine's `sources`; re-enabling it does not add the source back.

## 2. Write the prompt and the commands

These variables are rendered in the prompt file, in each `prepare` and `validate` element
and in the profile's `extra_args`, per day and per attempt:

| Variable | Value |
| -------- | ----- |
| `{{DAY}}` | the UTC day, `YYYY-MM-DD` |
| `{{DATA_ROOT}}` | the resolved `data_root` |
| `{{ATTEMPT_DIR}}` | `<data_root>/routines/<name>/<DAY>/attempt-<n>` |
| `{{DIGEST_DIR}}` | `<ATTEMPT_DIR>/digest`; `prepare` stdout goes to `<ATTEMPT_DIR>/prepare.out` |
| `{{<KEY>}}` | one per `outputs` key, upper-cased (`inbox_dir` gives `{{INBOX_DIR}}`) |

Only `{{NAME}}` without spaces is a token. There is no escape. An unknown variable stops the
run with exit 2 and no stamp, and the message names the file and line or the argv element.
In `prepare`, `validate` and `extra_args`, an element that is a bare `~` or starts with `~/`
is expanded.

The exit codes are the contract:

| Command | 0 | 3 | anything else |
| ------- | - | - | ------------- |
| `prepare` (600 s) | continue to invoke | day has no input: stamp `empty`, skip invoke and `validate` | failed, `prepare-exit-<n>` |
| `cairn invoke` | run `validate` | | failed, `invoke-exit-<n>`; `validate` does not run |
| `validate` (900 s) | stamp `done` | stamp `empty` | failed, `validate-exit-<n>` |

A timeout is `prepare-timeout` or `validate-timeout`. A missing executable is exit 127.

A digest `prepare` that adds `"--provenance", "tool"` leaves out peer and routine runs,
so the routine does not digest its own earlier output. The journal entries the peer writes
are not exported by cairn, so cairn writes no `provenance` key into them. The prompt should
tell the peer to put `provenance: agent` in each entry's front matter.

## 3. Dry-run

```bash
cairn routine run daily --dry-run
```

For each due day it prints the attempt directory it would create, the refresh, `prepare`,
invoke and `validate` argv, and the rendered prompt. It takes no lock, creates nothing and
runs nothing. A render error exits 2; fix it before installing.

## 4. Install

```bash
cairn routine install daily
```

It checks that `prompt_file` is readable, that `cd` is a directory and that yesterday
renders, warns when the peer or the first `validate` element is not on the recorded `PATH`,
then writes `~/Library/LaunchAgents/dev.cairn.routine.daily.plist` and loads it. Installing
again replaces the job. On another OS it prints the crontab line it would need and exits 2.

## 5. Kickstart

```bash
cairn routine kickstart daily
```

Asks launchd to start the job now, so the first run happens under launchd's environment,
not your shell's. It leaves a running instance alone; `--force-restart` kills it first,
which loses that round's spend. The killed run writes no failed stamp, and the restarted
run opens a new attempt for the same day. Not loaded: exit 1, run `install`.

## 6. Read status

```bash
cairn routine status daily          # --json, --lines N
cairn routine list                  # one line per routine, ending in state=<state>
```

`status` names one state and exits with a code a script can gate on:

| State | When | Exit |
| ----- | ---- | ---- |
| `fresh` | the last run finished under 48 h ago | 0, or 1 with an attention code |
| `running` | a run is in progress (its process is alive) | 0, or 1 with an attention code |
| `never_run` | no `last-run.json` yet | 1 |
| `stale` | the last run finished over 48 h ago | 1 |
| `paused` | `cairn routine pause` wrote a marker | 2 |
| `disabled` | the breaker stopped the routine | 2 |

The attention codes are `failed-day`, `attempts-exhausted`, `corrupt-stamp`,
`last-run-exit-<n>`, `interrupted-run`, `launchd-missing` (macOS only) and `stale-source`.
The state comes from files only, so `status` answers while launchd is wedged. An unknown
routine is also exit 2, but prints no JSON on stdout:

```bash
cairn routine status daily --json > /tmp/daily-status.json || echo "routine daily needs a look"
```

`status` also prints the config, the resolved paths (state dir, log, plist), the install state,
the last run, one line per day (`done attempt 2 cost 0.84`, `failed (validate-exit-1)
attempts 3/3`, `pending`, `expired`), the last cost and the log tail. Codex reports no cost,
so its cost is `null`.

Where to look after a failure:

- Log: `<data_root>/logs/routine-<name>.log`, one `event=` line per step, with the failing
  child's stderr tail
- Attempt: `{{ATTEMPT_DIR}}` holds `prompt.md`, `profile-args.json`, `invoke-result.json`,
  `invoke.stderr`, `prepare.out`, `validate.out`, `validate.err` and `attempt.json` (`prepare` stderr goes to the routine log)
- Stamps: `$XDG_STATE_HOME/cairn/routines/<name>/` (default `~/.local/state`), with
  `done-<DAY>.json`, `failed-<DAY>.json` (reason and history) and `last-run.json`
- Peer round: `cairn invoke status <thread>` with the `thread_id` from the stamp

A failed day is retried on the next run, up to 3 counted attempts. A run stops at its first
failed day and defers the rest. `incomplete-input` (a source refresh failed),
`render-error`, `budget-exhausted` and `stale-source` do not count as attempts.

After 3 consecutive runs that each end failed with a counted failure, the routine
disables itself: it writes `disabled.json`, and every later run logs `skipped-disabled`
and exits 0 without spending a peer round. Fix the cause, then run
`cairn routine resume daily`. A run with a done or empty day resets the count.

`cairn routine run daily --json` prints one report object on stdout (`schema_version`,
`status` of `ok`, `partial`, `failed`, `clean` or `skipped`, the counts, `failures`,
`stale_sources`, `breaker` and one row per day). The same object is `last-run.json`.

After the refresh, each run checks the digest sources that are enabled but not in
`sources`. A source with no successful export in the last 24 h gives one warning:

```text
warning: routine daily: source cursor was last exported 2026-10-27T10:00:00+00:00 (71.2 h ago); add it to routines.daily.sources or run 'cairn sync'
```

`stale_sources: "refuse"` (or `run --stale-sources refuse`) fails the days with
`stale-source` instead, before any peer round; `ignore` skips the check. Each
collection-wide `cairn <source> export` (not `export FILE`, not `sync`) writes
`<state_home>/cairn/exports/<source>-<key>.json`, keyed by the output directory; a store
with no receipt falls back to its catalog's modification time.

## Pause and resume

```bash
cairn routine pause daily --reason "moving house"
cairn routine resume daily
```

`pause` writes `paused.json` in the state dir; the reason is one line of at most 200
characters. Each run then prints `routine daily: paused since ... (moving house); run
'cairn routine resume daily' to resume` on stderr and exits 0, with no stamp, no
`last-run.json` and no hook. `--force` does not bypass a pause or a disabled routine;
`--dry-run` still works. `resume` removes `paused.json`, `disabled.json` and
`breaker.json`. `install` and `kickstart` keep the markers and print a `note:` line.

## Notify on each day

```bash
cairn config set routines.daily.terminal_hook '["/usr/bin/osascript", "-e", "display notification \"cairn routine finished a day\""]'
```

The hook runs once each time a day's done or failed stamp is written, with a 10 s
timeout, `cd` as its working directory and the day's report row as one JSON line on
stdin. Its environment adds `CAIRN_ROUTINE`, `CAIRN_ROUTINE_DAY`,
`CAIRN_ROUTINE_OUTCOME` (`done`, `empty` or `failed`), `CAIRN_ROUTINE_REASON`,
`CAIRN_ROUTINE_ATTEMPT`, `CAIRN_ROUTINE_ATTEMPT_DIR` and `CAIRN_ROUTINE_RUN_ID`. The
first element must be an absolute path, because launchd runs with the recorded `PATH`.
The run logs `event=hook` with the exit (`timeout`, `missing` or a number) and never
retries it; a hook failure changes no stamp, count or exit code.

## 7. Re-run a day

```bash
cairn routine run daily --day 2026-10-27            # one day, also outside the window
cairn routine run daily --day 2026-10-27 --force    # even if done or at 3 attempts
```

`--day` must be a UTC day that has ended. `--force` always starts a new attempt directory.
Use `--day` to recover an `expired` day (one that fell out of `catch_up_days` while the
machine was off). `--force` without `--day` re-runs every day in the window and warns first.

## 8. Uninstall

```bash
cairn routine uninstall daily
cairn config unset routines.daily
```

`uninstall` unloads the job and removes the plist. Stamps, attempt directories and the log
stay. It works for a routine already removed from config. `config unset` alone leaves the
launchd job in place and says so.
