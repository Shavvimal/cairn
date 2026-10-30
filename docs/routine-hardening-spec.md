# `cairn routine` hardening: status exit codes, pause, breaker, terminal hook, stale sources, run report

Status: implemented in the PR that adds this file, on top of `main` at 2.9.0. Section 11
lists where the implementation differs from the first draft of this spec; where they
differ, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0. This spec
extends [routine-spec.md](routine-spec.md); notes in that file point here.

**Dependencies:** none beyond `main`. The JSON shapes here leave room for later `code`
and `fix` keys and do not add them. The change to the export path in `src/cairn/cli.py`
is one call into a separate helper module (`src/cairn/export_receipt.py`).

## 1. Purpose and non-goals

### Purpose

A daily routine must be easy to gate from outside: cron, `cairn doctor` or a script.
Today `status` always exits 0 and a broken routine fails silently, once a day, for as long as
nobody looks at it. This spec adds:

1. `cairn routine status NAME` exit codes: 0 fresh, 1 needs attention, 2 paused or disabled.
2. A status classifier that reads only files (stamps, markers, the log, the plist) and
   names one state: `disabled`, `paused`, `running`, `never_run`, `stale` or `fresh`.
3. `cairn routine pause NAME --reason TEXT` and `cairn routine resume NAME`.
4. A routine-level breaker. After 3 consecutive runs that end with a counted failure, the
   routine disables itself and `status` exits 2.
5. An optional `terminal_hook` argv per routine. It runs once each time a day's stamp is
   written. This lets the user send a notification without cairn knowing any channel.
6. A stale-source check after the refresh. It warns by default. It can refuse, by config
   or by flag.
7. `cairn routine run --json`: one report object with `schema_version` and a top-level
   `status`. The same object goes into `last-run.json`.

### Non-goals

- Quiet hours, notification channels, retries of the hook, or a hook per run.
- A configurable cadence. Routines stay daily (`at` only).
- Any change to the per-day attempt cap, the success-only done stamp or the
  stop-at-first-failure rule. All three exist already (section 2).
- Cron-side install. `install` stays launchd-only.

## 2. Current behaviour (main, 2.9.0)

| Topic | Today | Where |
|---|---|---|
| `status` exit | Always 0, also with a failed day. Exit 2 only for an unknown routine | `src/cairn/routine.py:371-373`, `:411`, `:584-586` |
| Install state | `launchctl print` against the plist, no timeout. `yes`, `unloaded`, `no` or `n/a` | `routine.py:227-234` |
| `run` flags | `--day`, `--force`, `--dry-run`. No `--json` | `routine.py:543-547` |
| Log echo | A log line is echoed to stdout only on a TTY | `src/cairn/routine_run.py:130-135` |
| `last-run.json` | `run_id`, `started_at`, `finished_at`, `exit`, `days`, the four counts, `cost_usd`, `error`, `failures`. No schema version, no per-day rows | `routine_run.py:459-477` |
| Nothing due | Writes `last-run.json`, exit 0 | `routine_run.py:483-485` |
| Lock held | Logs `skipped-lock`, exit 0, no `last-run.json` | `routine_run.py:559-566` |
| Success-only stamp | A done stamp is written only after `validate` exits 0 or 3, or `prepare` exits 3 | `routine_run.py:375-378`, `:432-436`, `:289-318` |
| Per-day cap | 3 counted attempts across runs, then the day is skipped `attempts-exhausted` until `--force` | `src/cairn/routine_plan.py:26`, `:245-247`. Counting: `routine_run.py:236-241` |
| Uncounted reasons | `incomplete-input`, `render-error`, `budget-exhausted` | `routine_plan.py:29` |
| Stop at first failure | The first failed day (not `budget-exhausted`) defers the rest | `routine_run.py:512-519` |
| Refresh | `cairn <source> export --since D` for each `sources` entry. One failure fails every day `incomplete-input` | `routine_run.py:443-457`, `:508-511` |
| Child env | `CAIRN_ROUTINE`, `CAIRN_ROUTINE_DAY`, `CAIRN_ROUTINE_ATTEMPT_DIR` | `routine_plan.py:292-306` |
| Digest input | The digest reads `claude`, `codex`, `antigravity`, `cursor` | `src/cairn/digest.py:54` |
| Export record | None. The catalog is a cache and is saved only when dirty, so its mtime is "last change", not "last export" | `src/cairn/catalog.py:38-41`, `:235-237` |
| Export lock | `cairn <source> export|sync` runs under the operation lock, then flushes the catalog | `src/cairn/cli.py:131-141` |
| Routine keys | `_ROUTINE_FIELDS`, unknown keys fail with the allowed list | `src/cairn/config.py:597-612`, `:725-730` |
| Argv validation | `_routine_argv` message | `config.py:657-663` |
| `config set` fields | `_ROUTINE_SCALARS`, `_ROUTINE_ARGV_FIELDS`, `_coerce_routine` | `src/cairn/admin.py:300-315`, `:355-381` |
| Doctor | Launchd, last run, stamps and journal lines. No paused or disabled line | `routine.py:419-495` |
| Process identity | `(pid, start token)` that survives pid reuse | `src/cairn/health.py:105-130` |
| `run_capped` | `stdin=DEVNULL`, no input argument | `src/cairn/procs.py:70-95` |
| Test that pins exit 0 | `test_status_json_sections` asserts exit 0 with a failed day | `tests/test_routine.py:704-707` |

So three safeguards are already on `main`: the success-only stamp, the cap of 3 per day
across runs, and the stop at the first failed day. A per-day breaker that "refuses a day
after 3 failed attempts across runs until `--force`" is exactly `attempts-exhausted`. The gap is at routine level. A broken `validate` spends one paid
peer round per new day, indefinitely, because each new day starts with 0 attempts.
Section 4.4 adds the routine-level breaker.

## 3. Surface

### CLI

| Command | Change |
|---|---|
| `cairn routine status NAME [--json] [--lines N]` | New exit codes. New `state` line and JSON keys |
| `cairn routine pause NAME --reason TEXT` | New. `--reason` is required |
| `cairn routine resume NAME` | New. Removes the pause marker, the disabled marker and the breaker counter |
| `cairn routine run NAME [--json] [--stale-sources warn\|refuse\|ignore]` | New flags |
| `cairn routine list [--json]` | Adds `state=<state>` at the end of each line and `state` in JSON |
| `cairn routine install` and `kickstart` | Print a note when the routine is paused or disabled. They never touch the markers |

### Status exit codes

| Exit | When |
|---|---|
| 0 | `state` is `fresh` or `running`, and `attention` is empty |
| 1 | `state` is `never_run` or `stale`, or `attention` is not empty |
| 2 | `state` is `paused` or `disabled`. Also an unknown routine or a config error, as today |

Exit 2 is shared with usage errors. A gate tells them apart with `--json`: a usage error prints
no JSON on stdout.

### Config keys (in `routines.<name>`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `terminal_hook` | argv list or `null` | `null` | Runs once each time a day's stamp is written |
| `stale_sources` | `"warn"`, `"refuse"` or `"ignore"` | `"warn"` | What a stale digest source does to the run |

A config key is necessary for the second item: launchd runs a fixed `ProgramArguments`
(`routine.py:116`), so a flag alone could never reach a scheduled run. The flag overrides the
key for one manual run.

Exact validation messages, in the style of `config.py:657-663` and `:736`:

```text
routines.daily.terminal_hook must be a non-empty array of strings (an argv list, never a shell string), got str
routines.daily.terminal_hook takes no template variables (the hook reads CAIRN_ROUTINE_* from its environment), found {{DAY}}
routines.daily.terminal_hook[0] must be an absolute path after expanding ~, got 'notify'
routines.daily.stale_sources must be one of warn, refuse, ignore, got 'loud'
```

The first message comes from `_routine_argv`, unchanged. `_ROUTINE_FIELDS` gains both keys,
so the unknown-key message lists them. `config set routines.<name>.terminal_hook` accepts a
JSON array or `null`. `config set routines.<name>.stale_sources` takes the plain string.

Why argv[0] must be absolute: launchd runs with the recorded `PATH`. That `PATH` is not the
shell's, and a hook that fails is only logged. An absolute path removes a silent failure
class. `validate` keeps its looser rule because its failure is visible as a failed day.

### Files (all under the state dir `<state_home>/cairn/routines/<name>/`)

| File | Written by | Removed by |
|---|---|---|
| `paused.json` | `pause` | `resume` |
| `disabled.json` | `run` (the breaker) | `resume` |
| `breaker.json` | `run` | `resume`, or any run with a done or empty day |
| `running.json` | `run`, after it takes the lock | `run`, when `body` returns. A raising run rewrites it as interrupted |
| `last-run.json` | `run` | never (extended, section 5) |

Export receipts: `<state_home>/cairn/exports/<source>.json`, written by `cli.run` after every
`export` or `sync` subcommand, inside the operation lock.

### Exact messages

```text
routine daily: paused since 2026-10-30T09:12:00+00:00 (moving house); run 'cairn routine resume daily' to resume
routine daily: disabled since 2026-10-30T05:31:02+00:00 (3 consecutive failed runs: validate-exit-1, validate-exit-1, validate-exit-1); fix the cause, then run 'cairn routine resume daily'
Paused daily: moving house (runs skip until 'cairn routine resume daily')
daily was already paused; reason updated: moving house
Resumed daily (removed: paused.json, disabled.json)
daily is not paused or disabled; nothing to resume
cairn routine: --reason must be non-empty, one line, at most 200 characters
warning: routine daily: source cursor was last exported 2026-10-27T10:00:00+00:00 (71.2 h ago); add it to routines.daily.sources or run 'cairn sync'
warning: routine daily: source antigravity has no export record; add it to routines.daily.sources or run 'cairn sync'
note: routine daily is paused (moving house); the job is installed but each run skips until 'cairn routine resume daily'
```

The paused and disabled skip lines go to stderr on every run, TTY or not. Under launchd,
stderr is the routine log (`routine.py:117-119`).

## 4. Behaviour

### 4.1 Status classification

`classify(config, spec, now) -> Classification` lives in a new module
`src/cairn/routine_state.py`. It reads files only: no database, no network, no `launchctl`.
The `installed` field keeps calling `launchctl print` (`routine.py:227-234`), bounded by
`LAUNCHCTL_PROBE_S` = 5 s. A probe that does not answer in time reads `installed: unknown`
(doctor warns `'launchctl print' did not answer in 5 s; launchd may be wedged`). The exit
code never depends on it, so status works while launchd is wedged.

States, first match wins:

1. `disabled`: `disabled.json` exists.
2. `paused`: `paused.json` exists.
3. `running`: `running.json` exists and `health.identity_alive(pid, start_token)` is true.
4. `never_run`: `last-run.json` is missing.
5. `stale`: the age of the last run is over `STALE_AFTER_S` = 2 × 86 400 s. The age comes
   from `finished_at` in `last-run.json`, or from the file mtime when that key cannot be
   parsed.
6. `fresh`: none of the above.

Why 2 cadences. launchd fires a missed calendar job once at wake, so a healthy laptop sees
gaps up to about 24 h plus its sleep. A threshold of 1 cadence flaps every morning before the
fire. A threshold of 2 still reports one fully missed day. A comparable tool measured a
6-interval tolerance for a 5-minute daemon whose healthy gap is 2 intervals. The same
reasoning ("about 2 × the healthy gap") gives 2 here.

`attention` is a list of fixed reason codes, computed in every state:

| Code | When |
|---|---|
| `failed-day` | A window day has a failed stamp under the cap |
| `attempts-exhausted` | A window day has a failed stamp at the cap |
| `corrupt-stamp` | A window day has an unreadable failed stamp (as `routine_plan.py:240-244`) |
| `last-run-exit-<n>` | `last-run.json` has `exit` other than 0 |
| `interrupted-run` | `running.json` exists, but its process is gone |
| `launchd-missing` | On macOS, the plist file is absent |
| `stale-source` | The last run report lists a stale source |

Off macOS, `launchd-missing` is never raised: cron is the user's own business there.

### 4.2 Pause and resume

1. `pause NAME --reason TEXT` needs the routine in config (`_routine`, `routine.py:70-75`).
   It strips the reason and refuses an empty reason, a newline or more than 200 characters.
   Then it writes `paused.json` atomically and prints `Paused ...`. When the file already
   exists, it keeps `paused_at`, replaces the reason and prints the "already paused" line.
   Exit 0.
2. `resume NAME` checks the name with `_check_name`, so a routine already removed from
   config can still be resumed. It removes whichever of `paused.json`, `disabled.json` and
   `breaker.json` exist, then prints the list. Exit 0 in both cases.
3. `run` checks the markers after the depth check and before the lock (`routine_run.py:551-561`),
   and again right after it takes the lock, before `running.json`: the run that held the lock
   may have tripped the breaker, or a pause may have landed, between the two. `disabled` is
   checked first. On a marker, `run` logs `event=skipped-paused reason=...` or
   `event=skipped-disabled reason=...`, prints the stderr line and exits 0. It writes no stamp,
   no `last-run.json`, no `running.json` and runs no hook. With `--json`, it prints a report
   with `status: skipped` and `reason: paused` or `disabled`. `--force` does not bypass a
   marker: `resume` is the one way out.
4. `--dry-run` prints the paused or disabled line first, then the normal dry run.
5. `install`, `uninstall` and `kickstart` never read or write the markers, apart from the
   `note:` line. `uninstall` keeps them with the stamps.

### 4.3 Running marker

After the lock is taken (`routine_run.py:561`), `run` writes `running.json`
`{pid, start_token, run_id, started_at}` from `health.process_identity(os.getpid())`.
It deletes the file when `body` returns, inside the lock. When `body` raises (any
exception, `KeyboardInterrupt` included), `run` rewrites the file with `pid` and
`start_token` null plus `interrupted` (the exception class) and `interrupted_at`, then
re-raises, so status reports `interrupted-run` at once. A kill leaves the file as written,
and the dead identity gives the same result. The next run overwrites the file.

### 4.4 Breaker

`BREAKER_RUNS = 3`. In `Run.finish` (`routine_run.py:459`), after the report is built:

1. If any day in the run ended done or empty (`ok` or `partial`), delete `breaker.json`.
2. If `status` is `failed` and at least one failure has a counted reason (not in
   `UNCOUNTED_REASONS`, not `stale-source`), add the run to `breaker.json`:
   `{consecutive_failed_runs, runs: [{run_id, finished_at, reason}]}`, the last 3 runs only.
3. If `consecutive_failed_runs` reaches 3, write `disabled.json`
   `{reason, disabled_at, runs}` and log `event=disabled reason=...`.
4. Any other run (`clean`, uncounted failures only, `skipped`) leaves `breaker.json` as it is.

Cost bound: with a broken validator, the routine spends at most 3 peer rounds before it
stops, instead of one round per day.

### 4.5 Stale-source check

1. The digest sources are the entries of `DIGEST_SOURCES` (`digest.py:54`) whose collection
   is in config and enabled. They are not the routine's `sources`: those were just refreshed,
   so they cannot be stale.
2. The check runs once per run, after the refresh succeeds and before the first day
   (between `routine_run.py:508` and `:512`). Mode: `--stale-sources` if given, else
   `spec.stale_sources`. `ignore` skips the check.
3. For each source, the last export is `last_ok_at` from `exports/<source>.json`. When that
   file is missing (a store exported before this version), the fallback is the mtime of
   `catalog_path(output_dir)`, with `basis: "catalog-mtime"`. With neither, the basis is
   `none` and the source is stale.
4. A source whose last export is older than `STALE_SOURCE_S` = 24 h is stale. Each stale
   source gives one `warning:` line on stderr, one `event=stale-source source= age_h= basis=`
   log line and one entry in the report's `stale_sources`.
5. `warn` continues. `refuse` fails every day in the set with reason `stale-source`. That
   reason joins the uncounted reasons, so it costs no attempt and no breaker strike. No
   `prepare` or invoke runs, and the run exits 1, as `incomplete-input` does.

Export receipt: `src/cairn/export_receipt.py`, `write(source, command, exit_code)`.
`cli.run` calls it (through `record`) inside the operation lock at `cli.py:134-140`
(section 11 gives the exact place).
The shape is `{source, last_command, last_finished_at, last_exit, last_ok_at, cairn_version}`.
`last_ok_at` changes only when the exit code is 0. An `OSError` is caught and printed as a
warning, because a receipt must never fail an export.

### 4.6 Terminal hook

1. `Run.fail_day` and `Run.done_day` call `self.hook(day, outcome, reason, attempt, where, cost)`
   after the stamp write. Every stamp write gives exactly one call. `incomplete-input` and
   `stale-source` give one call per day with `attempt: null`. Deferred and skipped days get
   no call.
2. Environment: `child_environment(name, day, where)` (`routine_plan.py:292-306`), plus
   `CAIRN_ROUTINE_OUTCOME` (`done`, `empty` or `failed`), `CAIRN_ROUTINE_REASON` (empty
   unless failed), `CAIRN_ROUTINE_ATTEMPT` (the number, or empty) and `CAIRN_ROUTINE_RUN_ID`.
   When `where` is `None`, `CAIRN_ROUTINE_ATTEMPT_DIR` is empty. A first draft proposed
   `CAIRN_DAY` and `CAIRN_OUTCOME`. This spec uses the `CAIRN_ROUTINE_` prefix instead,
   because `prepare` and `validate` already read `CAIRN_ROUTINE_DAY` and
   `CAIRN_ROUTINE_ATTEMPT_DIR`, and one thing gets one name.
3. Stdin: the day's report row (section 5) as one JSON line. `cwd` is `spec.cd`.
4. The runner is `routine_run.run_hook(argv, *, stdin_text, env, cwd)`, which tests replace.
   It calls `procs.run_capped(..., timeout=10, input=stdin_text)`. `run_capped` gains a
   keyword `input: str | None = None`: with a value, `stdin=PIPE` and `communicate(input=...)`.
   Without it, behaviour is unchanged.
5. The run logs `event=hook day= outcome= exit=<n|timeout|missing> seconds=`, plus the stderr
   tail on a non-zero exit. The hook is never retried. Its result goes into the row's `hook`
   field only. It never changes a stamp, a count, the run exit or the breaker.

### 4.7 Run report and `--json`

`Run.finish` builds the report (section 5) and writes it as `last-run.json`. With `--json`,
it also prints the report as one indented JSON object on stdout. With `--json`, `RunLog.write`
does not echo to stdout, the lock-skip line is not printed, and errors still go to stderr.
The run exit codes stay as today (`routine_run.py:542-543`): 0, 1 or 2.

Status, first match wins:

| status | When | Exit |
|---|---|---|
| `skipped` | Lock held, paused or disabled. `reason` is `lock-held`, `paused` or `disabled` | 0 |
| `failed` | Exit 2 (a render or config error), or no day ended done or empty, with at least one failure | 1 or 2 |
| `partial` | At least one day done or empty, and at least one failed or deferred | 1 |
| `ok` | Every day in the set ended done or empty | 0 |
| `clean` | Nothing due | 0 |

A lock-held run does not write `last-run.json`, as today. The report goes to stdout only, so
the last real run stays on disk.

## 5. Data formats

Run report (also `last-run.json`). The keys on today's `last-run.json` keep their names and
meanings, so `status` (`routine.py:393-400`) and doctor (`routine.py:446-466`) read it
unchanged. All new keys are additive, so `schema_version` starts at 1:

```json
{
  "schema_version": 1,
  "routine": "daily",
  "status": "partial",
  "reason": null,
  "run_id": "20261030T052003Z-1a2b3c4d",
  "started_at": "2026-10-30T05:20:03+00:00",
  "finished_at": "2026-10-30T05:41:10+00:00",
  "exit": 1,
  "days": ["2026-10-28", "2026-10-29"],
  "done": 1, "empty": 0, "failed": 1, "deferred": 0,
  "cost_usd": 1.31,
  "error": null,
  "failures": [{"day": "2026-10-29", "reason": "validate-exit-1"}],
  "stale_sources": [{"source": "cursor", "last_export_at": "2026-10-27T10:00:00+00:00", "age_h": 71.2, "basis": "receipt"}],
  "breaker": {"consecutive_failed_runs": 0, "disabled": false},
  "rows": [
    {"day": "2026-10-28", "outcome": "done", "reason": null, "attempt": 2, "attempts": 1,
     "cost_usd": 0.84, "attempt_dir": "~/.context/routines/daily/2026-10-28/attempt-2",
     "hook": {"exit": 0, "seconds": 0.3}},
    {"day": "2026-10-29", "outcome": "failed", "reason": "validate-exit-1", "attempt": 1,
     "attempts": 1, "cost_usd": 0.47,
     "attempt_dir": "~/.context/routines/daily/2026-10-29/attempt-1", "hook": null}
  ]
}
```

`days` stays a list of strings for compatibility, and the per-day rows go under `rows`.
`outcome` is `done`, `empty`, `failed`, `deferred` or `skipped`. A skipped row comes from
`compute_days` and carries its reason (`done`, `attempts-exhausted`, `corrupt-stamp`).
`hook` is `null` when no hook is configured or none ran. `exit` is `"timeout"` or
`"missing"` for those cases.

Status `--json` gains these keys. Every existing key stays (`tests/test_routine.py:709-718`):

```json
{
  "schema_version": 1,
  "state": "fresh",
  "exit": 1,
  "attention": ["failed-day"],
  "paused": null,
  "disabled": null,
  "last_run_age_s": 3600.0,
  "stale_after_s": 172800
}
```

`paused` and `disabled` hold the marker objects when present.

Markers:

```json
{"reason": "moving house", "paused_at": "2026-10-30T09:12:00+00:00", "cairn_version": "2.10.0"}
{"reason": "3 consecutive failed runs: validate-exit-1, validate-exit-1, validate-exit-1", "disabled_at": "...", "runs": [{"run_id": "...", "finished_at": "...", "reason": "validate-exit-1"}]}
```

Hook stdin: one row object from `rows`, plus `"routine"` and `"run_id"`.

New log events: `skipped-paused`, `skipped-disabled`, `stale-source`, `hook`, `disabled`.
Each follows the format of `routine_run.py:137-139` and passes `LOG_LINE` in
`tests/test_routine.py:28-31`.

## 6. Failure modes

| Case | Result |
|---|---|
| A marker file is corrupt JSON | Treated as present. The reason shows `(unreadable marker)`, so the run fails closed |
| `last-run.json` is corrupt | `never_run` plus `last-run-exit-unknown` in attention. Exit 1 |
| `running.json` from a crashed or raising run | `interrupted-run`. The next run overwrites the file |
| `launchctl print` hangs | Killed after 5 s. `installed` is `unknown`; state and exit are unchanged |
| `ps` is missing (identity probe fails) | `process_identity` returns `None`, so `running` is not reported. The run's own lock still protects it |
| The hook hangs | Killed at 10 s with its process group (`procs.py:96-100`). Logged `exit=timeout` |
| The hook binary is gone | Exit 127, logged `exit=missing`. The run is unaffected |
| The hook writes into the state dir | Not prevented. It runs as the user, like `validate` |
| The receipt write fails | A warning on stderr. The export exit code is unchanged |
| Clock skew (`finished_at` in the future) | The age is clamped to 0, so the routine reads as `fresh` |
| A user script runs `status` under `set -e` | It now stops on a failed day. This is a deliberate change, recorded in CHANGELOG under Changed |
| The breaker trips on a transient outage | `resume` clears it. Three daily runs make the outage at least 2 days long |

## 7. Tests

`tests/test_routine_state.py` (new):

- `test_state_precedence_disabled_paused_running_never_stale_fresh`: one fixture per state, checked in order.
- `test_stale_after_two_cadences`: 47 h is `fresh`, 49 h is `stale`.
- `test_attention_codes_from_stamps`: `failed-day`, `attempts-exhausted`, `corrupt-stamp`.
- `test_launchd_missing_only_on_macos`: patch `is_macos`.
- `test_interrupted_run_when_identity_dead`: `running.json` with a dead pid.
- `test_status_exit_codes_0_1_2`: CLI exits for `fresh`, a failed day, and paused.
- `test_status_never_calls_launchctl_for_exit`: a `Launchctl` fake that raises still gives an exit code.
- `test_status_answers_when_launchctl_print_hangs`: `subprocess.run` raises `TimeoutExpired`; `status --json` exits 0 with `installed: unknown`, and doctor warns.
- `test_run_that_raises_leaves_interrupted_evidence`: `KeyboardInterrupt` and `OSError` out of `body` leave `running.json` with no identity; status exits 1 with `interrupted-run`.

`tests/test_routine.py` (extended):

- `test_status_json_sections`: now expects exit 1 and the new keys.
- `test_pause_writes_marker_and_run_skips_without_stamp`.
- `test_pause_requires_reason_and_rejects_newline`.
- `test_resume_removes_markers_and_breaker`.
- `test_install_keeps_pause_marker_and_notes_it`.
- `test_breaker_disables_after_three_counted_failed_runs`.
- `test_breaker_ignores_uncounted_failures_and_resets_on_done`.
- `test_marker_that_lands_before_the_lock_still_skips`: `disabled.json` written while the lock is taken still skips the run.
- `test_hook_runs_once_per_stamp_with_env_and_stdin`.
- `test_hook_failure_and_timeout_never_change_stamp_or_exit`.
- `test_hook_runs_per_day_on_incomplete_input`.
- `test_stale_source_warns_and_reports`.
- `test_stale_source_refuse_fails_days_uncounted_without_invoke`.
- `test_stale_source_ignores_routine_sources_and_disabled_collections`.
- `test_stale_source_falls_back_to_catalog_mtime`.
- `test_run_json_report_status_values`: `clean`, `ok`, `partial`, `failed`, `skipped`.
- `test_run_json_stdout_is_one_object_on_tty`: patch `isatty` to true.
- `test_last_run_json_keeps_legacy_keys`.
- `test_list_appends_state`.
- `test_doctor_reports_paused_and_disabled`.

`tests/test_config_routines.py`:

- `test_terminal_hook_argv_rules`: string, empty list, template token, relative argv[0], `null`.
- `test_stale_sources_enum`.

`tests/test_export_receipt.py` (new):

- `test_receipt_written_after_export_ok_and_failed`: `last_ok_at` moves only on exit 0.
- `test_receipt_write_error_is_warning`.

`tests/test_procs.py`:

- `test_run_capped_input_reaches_stdin`.

## 8. Docs to update

- `docs/routine-hardening-spec.md` (new, this spec in repo style).
- `docs/routine-spec.md`: notes in 6.2.4 (new events), 6.2.5 (`last-run.json` is the report),
  6.4 (exit codes) and 2.8 (new messages). Each note points to the new spec.
- `skills/cairn/workflows/routine.md`: section 6 (state, exit codes, gate example), new
  sections "Pause and resume" and "Notify on each day", and the stale-source warning.
- `skills/cairn/SKILL.md:97-100`: add `pause | resume` and `run --json`.
- `src/cairn/__main__.py:92-95`: the same help lines.
- `README.md:235-272`: a pause line, a status gate line, and a `terminal_hook` example.
- `src/cairn/config.example.json` and `cairn.config.example.json`: `"//routines.terminal_hook"`
  and `"//routines.stale_sources"` comment keys only. Leave `example-daily` without a hook, so
  `tests/test_config_examples.py:38-45` still holds.

## 9. Implementation plan

1. `procs.run_capped` `input` keyword and test. About 10 lines.
2. `export_receipt.py`, the `cli.run` call and tests. About 60 lines.
3. `config.py`: `RoutineSpec.terminal_hook`, `RoutineSpec.stale_sources`, `_ROUTINE_FIELDS`,
   parse rules. `admin.py`: `_ROUTINE_SCALARS` gains `stale_sources`, and `terminal_hook` gets
   argv-or-null coercion. Tests. About 70 lines.
4. `routine_state.py`: marker read and write, `classify`, constants. About 150 lines.
5. `routine.py`: `pause`, `resume`, status exit codes and keys, the `list` state, install and
   kickstart notes, doctor lines, parser. About 120 lines.
6. `routine_run.py`: marker check, `running.json`, stale check, hook, rows, report, breaker,
   `--json`, the `--stale-sources` flag. About 220 lines.
7. Tests (section 7). About 600 lines.
8. Docs (section 8). About 200 lines.

Commit order follows the steps. Each commit passes `make check`.

## 10. Decisions

1. `never_run` exits 1. A routine installed today reads as needing attention until its
   first fire: a gate means "the routine has produced output", and the message names the
   state.
2. `STALE_AFTER_S` and `STALE_SOURCE_S` are constants in `routine_state.py`, not config
   keys, until a user asks for a change, as was done for `ATTEMPT_CAP`
   (`docs/routine-spec.md:1786`).
3. A manual `run --day D --force` does not bypass `disabled` or `paused`. `--dry-run` works
   while disabled, and `resume` followed by one run puts the breaker back at 0 of 3.

## 11. Differences in the implementation

- The export receipt is written inside the `try` of `cli.run` (`src/cairn/cli.py`),
  right after the export returns and before `flush_catalog`, still inside the operation
  lock. One added line keeps the diff small. An export that raises writes no receipt,
  which reads the same as "did not finish". The receipt also records `last_command`
  (`export` or `sync`).
- A hook that fails in a way other than a timeout or a missing binary (for example a
  process that cannot be reaped) is logged with `exit=error`. The run is unaffected.
- `run --json --dry-run` is refused with exit 2 and
  `cairn routine: --json does not combine with --dry-run`, because the dry run prints text.
- A deferred row carries no attempt fields; a skipped row carries only its reason.
- `status` prints a `state: <state> (exit <n>)` line, with the attention codes, as its
  first line after the routine name, and the paused or disabled line under it. The
  status `config` section gains `terminal_hook` and `stale_sources`.
- `doctor` adds a `routine [<name>] paused` or `routine [<name>] disabled` warning line,
  whose detail is the skip line without the `routine <name>: ` prefix.
- The new test of `run_capped` is `tests/test_procs.py`, a new module.
