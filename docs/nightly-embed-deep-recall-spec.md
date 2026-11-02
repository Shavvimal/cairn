# Nightly embedding at background priority, and a deep mode for recall

Status: implemented in 2.21.0. Section 17 lists where the implementation differs from the sections above; where they disagree, section 17 wins. Written 2026-11-01 against `main` at 2.20.1 (`e521878`). Every claim about current behaviour cites `path:line` on that commit. A claim marked **Inferred** was not read in code or measured; the implementer must check it before relying on it. A claim marked **Measured** was taken on one developer machine (a laptop with Apple silicon, qmd 2.5.3) and is not a benchmark.

This file holds no machine name, no home path, no personal data. Spelling follows the repository's docs convention, which is British (`behaviour`, `anonymised`, `tokenises`, `synthesised` in README.md, CONTRIBUTING.md and the skills). CONTRIBUTING.md says "Write skill text plainly: imperative steps, British spelling, no marketing adjectives."

## 1. Summary

Two changes in one PR, version 2.21.0:

- **A. Nightly embedding.** The scheduled job stops embedding every hour. An hourly light job exports every enabled source and runs `qmd update` (BM25) only, at background priority. One nightly job (default 03:00 local) runs the embed, at background priority, only on mains power by default, for up to three QMD embed sessions while the pending count falls. On macOS both jobs are LaunchAgents with `ProcessType Background` and `LowPriorityIO`; elsewhere they are crontab lines under `nice` and `ionice`. `cairn cron install` migrates the old hourly `cairn sync --cron` entry. A new `cairn embed` command drains a backlog by hand.
- **B. Deep recall.** A new `cairn recall search` command runs BM25 for the query and its variants, then runs ONE hybrid `qmd query` when the caller asks for it (`--deep`) or when BM25 finds fewer distinct documents than a configured minimum. It merges and de-duplicates the hits, labels each one `bm25`, `hybrid` or `bm25+hybrid`, enforces a time budget, and falls back to BM25 with a one-line note when vectors are missing or stale. The `/recall` skill uses it for topic recall and adds deep-recall triggers. The `/search` skill gains an explicit `--deep` flag and stays BM25-only otherwise.

## 2. Motivation

**Measured** from one machine's `context-sync.log`, 17 Oct to 1 Nov 2026 (1,658 cron runs and 785 hook runs):

| Step | Per hourly cron run |
| --- | --- |
| Exports (five sources) | about 35 s in total |
| `qmd update` (BM25 re-index) | about 11 s |
| `qmd embed` | median 153 s, p90 368 s, max 1,634 s (243 runs) |
| Embed time per day | 47 to 106 min (median about 60 min) |
| BM25 query, `qmd search ... -n 5` | 0.12 s (two runs) |
| Hybrid query, `qmd query`, qmd 2.5.3 | 8.4 s (one run) |

Further single runs on 2026-11-01 for this spec (same machine, on battery power, no other cairn job running, uncontrolled load), **Measured**:

| Command | Wall time |
| --- | --- |
| `qmd status` | 0.42 s |
| `qmd search "QUERY" -n 3 --format json` | 0.27 s |
| `qmd query "QUERY" -n 3 --format json -c notes` | 24 s |
| `qmd query "QUERY" -n 3 --format json -c notes -c claude-code-sessions --no-rerank` | 12 s |
| the same rerank query as row 3, run again | 79 s |

Three facts follow.

1. The scheduled job embeds every hour at normal priority. `cairn cron install` writes one crontab line with no `nice` and no background policy (`src/cairn/admin.py:806-829`). The routine LaunchAgent sets `ProcessType Background` (`src/cairn/routine.py:123`), but sync does not. So an embed of a median 153 s competes with foreground work every hour.
2. The main reader of the index does not read vectors. The recall and search skills use BM25 only, on purpose: "Use `qmd search` (BM25), **not** `qmd query` (hybrid) - speed matters here" (`skills/recall/SKILL.md:169-171`), and the same rule in `skills/search/SKILL.md:71-72`. Only the setup skill calls `qmd vsearch` and `qmd query` (`skills/setup/SKILL.md:306-307`). About an hour of GPU a day buys vectors that the default recall path does not use.
3. A hybrid query is 70 to 650 times slower than BM25, so it cannot be the default. It is worth one call when BM25 fails: a vague question, or a session that used different words.

Nightly embedding keeps vectors at most about a day old and cuts scheduled GPU time to one batch a night. Deep recall gives those vectors a reader, at a cost the user opts into or that only a thin BM25 result triggers.

## 3. Current behaviour

### 3.1 Scheduling

- `cairn cron install` (`src/cairn/admin.py:806-829`) builds `/usr/bin/env K=V ... <cairn> sync --cron >> <data_root>/logs/context-sync.log 2>&1 # cairn sync` from `background_environment(config)` (`src/cairn/runtime.py:30-67`) and `_cairn_invocation()` (`src/cairn/admin.py:56-64`), escapes `%`, removes every earlier line that contains `cairn sync` or `context_sync.py` (`_without_cairn_lines`, `src/cairn/admin.py:801-803`), and appends the new line. The schedule is `config.cron_schedule` (`src/cairn/config.py:914`), default `"7 * * * *"` (`src/cairn/config.py:50-51`).
- `cairn cron uninstall` removes those lines (`src/cairn/admin.py:832-840`). The parser has `install` and `uninstall` only (`src/cairn/admin.py:843-855`).
- `_current_crontab` treats only "no crontab" as empty and raises `ConfigError` on any other `crontab -l` failure (`src/cairn/admin.py:774-794`).
- Doctor's schedule check is one line: an `ok` when any crontab line contains `cairn sync`, else a warning "no entry; run 'cairn cron install'" (`src/cairn/admin.py:1470-1474`).
- `cairn config set cron.schedule VALUE` is the only settable `cron` key (`src/cairn/admin.py:462-464`). `config show` prints `cron_schedule` (`src/cairn/admin.py:226`, `src/cairn/admin.py:241`).
- Routines already install LaunchAgents: `render_plist` writes `ProcessType Background`, `RunAtLoad false`, `StartCalendarInterval {Hour, Minute}` and the environment block (`src/cairn/routine.py:114-130`). `install` runs `launchctl bootout`, writes the plist atomically, then `launchctl bootstrap gui/<uid>` (`src/cairn/routine.py:149-188`). Every `launchctl` call goes through `routine.launchctl` (`src/cairn/routine.py:57-63`), which the tests replace. Off macOS, `routine install` prints a crontab line and exits 2 (`src/cairn/routine.py:151-158`). The label prefix is `dev.cairn.routine.` (`src/cairn/config.py:639`), and the plist lives in `~/Library/LaunchAgents/` (`src/cairn/config.py:884-892`).

### 3.2 Sync and embed

- Modes are `hook`, `cron` and `all`, ranked `{"hook": 0, "cron": 1, "all": 2}` (`src/cairn/sync.py:52`). `--cron` and `--all` run the same stages (`src/cairn/sync.py:293-301`, `src/cairn/sync.py:308`).
- `_index_stages` (`src/cairn/sync.py:361-384`) holds `locked_index()` across collection registration, `qmd update`, `qmd embed` and the status check. The embed runs only when `embed=True`, which is every mode but `hook` (`src/cairn/sync.py:401`).
- `run_embed` (`src/cairn/sync.py:164-197`) runs one `qmd embed` with a 3,600 s timeout (`src/cairn/sync.py:51`) and reads the whole output for chunk failures (`embed_failures`, `src/cairn/qmd.py:451-473`). `verify_index` (`src/cairn/sync.py:200-226`) reads `qmd status` and returns `qmd-verify (incomplete)` when documents are still pending.
- QMD 1.1.5 and 2.5.3 cap one embed session at 30 minutes; a partly embedded document loses its vectors in 2.5.3 and restarts next run (`README.md:517-523`, `src/cairn/qmd.py:454-456`).
- One sync runs per data root. A second one records its mode under `<data_root>/sync-requests/` and exits 0; the holder services the widest request once in `_follow_up` (`src/cairn/sync.py:304-328`, `src/cairn/sync.py:461-477`). `_follow_up` runs extra stages only when the running mode is `hook` (`src/cairn/sync.py:469-471`).
- The hook worker and `cairn invoke` never take the sync lock; they meet a running sync only at the QMD index lock (`src/cairn/sync.py:19-25`).
- The index stamp `$XDG_STATE_HOME/cairn/qmd-index.json` records the last successful `qmd update` (`src/cairn/qmd.py:223-276`). No file records the last embed.

### 3.3 Doctor's index check

`_check_embeddings` (`src/cairn/admin.py:970-1002`) warns on `pending` and `no-vectors` and fails them with `--strict-index`. Its next step is always "run 'cairn sync --cron'". The `QMD_INDEX_INCOMPLETE` registry row says the same (`src/cairn/errors.py:155-167`).

### 3.4 Topic recall and search

- `cairn recall` has no topic command. Its subcommands are `list`, `expand`, `digest`, `frame`, `gaps`, `verify` and `decisions` (`src/cairn/recall.py:83-171`), and `cairn help` says "topic search is qmd's job" (`src/cairn/__main__.py:62`).
- Topic recall lives in the skill. It enumerates collections, writes 3 to 4 variants, runs `qmd search "VARIANT" -c COLLECTION -n 5` for each in parallel, runs one rg pass for literal terms, de-duplicates by path and fetches the top 2 to 3 (`skills/recall/SKILL.md:144-211`).
- `/search` runs one `qmd search "QUERY" -n NUM [-c COLLECTION]` or an rg pass (`skills/search/SKILL.md:42-72`) and forbids variants and synthesis (`skills/search/SKILL.md:149-150`).

### 3.5 What `qmd` prints (qmd 2.5.3)

**Measured** with read-only commands on 2026-11-01:

- `qmd search QUERY --format json` prints a JSON array. Each element has `docid` (for example `"#3a56d5"`), `score` (a float from 0 to 1), `file` (a `qmd://COLLECTION/PATH` URI), `line`, `title`, `context` and `snippet`. There is no `collection` key; the collection is the first segment of `file`. A query with no match prints `[]` and exits 0.
- `qmd query QUERY --format json` prints the same element shape.
- `-c NAME` is repeatable on both commands and limits the search to those collections (`qmd --help`: "Filter by one or more collections"). A two-collection `qmd query` returned hits from the second collection.
- `qmd query` accepts `--no-rerank` ("Skip LLM reranking (use RRF scores only, much faster on CPU)") and `-C, --candidate-limit`.
- BM25 scores do not separate a good result from a bad one. Top scores: `launchd` 0.90, `taskpolicy background priority` 0.95, a nonsense pair of words 0.90, a vague ten-word sentence 0.93. Hit counts with `-n 20`: 20, 20, 7 and 20. So a score floor and a hit-count floor both rarely fire for a multi-word query. Section 5.3 uses the count floor anyway, because it does fire for a rare term with no match, and it leaves the score floor off by default.
- `qmd status` prints `Total`, `Vectors` and an optional `Pending` line, which `parse_status` already reads (`src/cairn/qmd.py:387-407`).

## 4. Design A: the scheduled sync

### 4.1 Decision: launchd on macOS, cron elsewhere

The new default backend is launchd on macOS and cron on every other platform. `cron.backend: "cron"` keeps cron on macOS for a user who wants it.

Reasons for launchd on macOS:

1. launchd runs a missed calendar job after wake; cron drops it. `man launchd.plist`: "Unlike cron which skips job invocations when the computer is asleep, launchd will start the job the next time the computer wakes up. If multiple intervals transpire before the computer is woken, those events will be coalesced into one event upon wake from sleep." A laptop asleep at 03:00 gets its nightly embed on wake. Under cron it would never run on a laptop that sleeps every night.
2. Priority is declarative. `ProcessType Background` applies the resource limits for "work that was not directly requested by the user", and `LowPriorityIO` marks the job's file I/O as low priority (`man launchd.plist`).
3. cairn already writes, loads and removes LaunchAgents for routines, through one patchable `launchctl` wrapper (`src/cairn/routine.py:57-63`, `src/cairn/routine.py:149-221`). The new code reuses that wrapper and `is_macos()`.

Cost: two install paths. The cron path stays, because it is the only path off macOS and the existing tests cover it.

**Inferred:** `ProcessType Background` and `taskpolicy -b` throttle CPU and I/O, and the darwin background policy is inherited by child processes (the `qmd` Node process and its llama.cpp work). Apple does not document a GPU effect. Section 11.2 has a manual check for the inherited priority and the embed duration.

### 4.2 New sync modes

`cairn sync` gains two flags. Existing flags keep their meaning.

| Flag | Mode | Stages |
| --- | --- | --- |
| `--hook` | `hook` | unchanged: on-hook sources, `qmd update` |
| `--light` (new) | `light` | hook-worker retry, every enabled export, gap checks, drift line, collections, `qmd update`. No embed. Then the catch-up rule of 4.5. |
| `--nightly` (new) | `nightly` | the `light` stages, then the embed loop of 4.3 under the power policy of 4.4 |
| `--cron` | `cron` | unchanged: `light` stages plus one embed and verify, no power policy |
| `--all` / no flag | `all` | unchanged, same as `--cron` |

Implementation in `src/cairn/sync.py`:

- `build_parser` (`src/cairn/sync.py:283-301`): add `--light` and `--nightly` to a mutually exclusive group with `--hook`, `--cron` and `--all`. Today the flags are independent `store_true` options and `main` picks the first set one (`src/cairn/sync.py:308`); a mutually exclusive group makes `--light --cron` an argparse error (exit 2). **Inferred:** no caller passes two mode flags; check `hooks/` and the docs before the change.
- `MODE_RANK` becomes `{"hook": 0, "light": 1, "nightly": 2, "cron": 3, "all": 4}`.
- `_plan` (`src/cairn/sync.py:65-88`) treats `light` and `nightly` like `cron`: every enabled source.
- `_stages` (`src/cairn/sync.py:387-401`) calls `_index_stages(config, embed=mode in ("cron", "all"), record=record)`. For `nightly` it then calls `embed_loop(config, record, nightly=True)` (4.3). For `light` it then calls `maybe_catch_up(config, record)` (4.5).
- `_follow_up` (`src/cairn/sync.py:461-477`) gains one rule before the existing ones. When the requested mode is `nightly`, `cron` or `all`, and this run did not embed (`mode` is `hook` or `light`), run the stages this run skipped: for a `hook` run the export stages as today, then for any of them the embed. A `nightly` request runs `embed_loop(config, record, nightly=True)`, so the power policy applies. A `cron` or `all` request runs `embed_loop(config, record, nightly=False, max_sessions=1)`, which is today's single embed. The existing rule that a request is serviced once and new requests wait for the next invocation stays.
- The `=== context-sync (MODE) ... ===` header (`src/cairn/sync.py:483`) prints the new mode names, so the log shows which job ran.

### 4.3 The embed loop and the 30-minute session cap

QMD stops one embed session after 30 minutes and exits 0 (`src/cairn/qmd.py:454-456`). A large backlog therefore needs several sessions. The new function `embed_loop` in `src/cairn/sync.py` replaces the single embed for `nightly`, for the follow-up embed and for `cairn embed`:

```python
def embed_loop(
    config: CairnConfig,
    record,
    *,
    nightly: bool,
    max_sessions: int | None = None,
    respect_power: bool | None = None,
) -> str:
    """Run up to ``max_sessions`` QMD embed sessions while the pending count falls.

    Returns the outcome written to the embed stamp: ``complete``, ``incomplete``,
    ``stalled``, ``failed``, ``deferred`` or ``skipped``.
    """
```

Defaults: `max_sessions` is `config.schedule.embed_max_sessions` when `nightly`, else 1. `respect_power` is `True` when `nightly`, else `False`.

Algorithm:

1. Before session `n` (1-based):
   - If `n > 1` and `nightly` and the local time is at or after `cron.embed_deadline` today, log `  [qmd-embed] stopped - embed_deadline HH:MM reached after N-1 sessions` and stop. The first session never checks the deadline: a job that launchd fires late after wake still does its owed work.
   - If `respect_power`, read `power_source()` (4.4). On `battery` with policy `defer` or `skip` and `n == 1`, apply 4.4 and return `deferred` or `skipped`. On `battery` and `n > 1`, log `  [qmd-embed] stopped - on battery power after N-1 sessions`, write the owed marker (4.6) and stop.
2. Take `locked_index()` (`src/cairn/qmd.py:30-48`), run `run_embed(qmd)` (`src/cairn/sync.py:164`), and if QMD exited, read the status with a new `verify_index_status(qmd) -> tuple[str | None, IndexStatus | None]`. Keep `verify_index` as a wrapper that returns the label only, so existing tests and callers do not change. Release the index lock after each session, so a hook worker that waits on it can run its `qmd update` between sessions. The sync lock stays held for the whole loop.
3. Log `  [qmd-embed] session N/MAX` before each session.
4. After session `n`:
   - label `None` (no pending): outcome `complete`, stop.
   - label `qmd-verify (incomplete)`: if this is the first session, or the pending count fell since the previous session, and `n < max_sessions`, continue. If the count did not fall, outcome `stalled`, log `  [qmd-verify] STALLED - pending stayed at P after session N; a document may never fit in one 30-minute session. Investigate instead of retrying.`, stop. If `n == max_sessions`, outcome `incomplete`, stop.
   - any other label, or QMD did not exit: outcome `failed`, stop.
5. Record only the last session's verdict, so a backlog that a later session finishes does not fail the run: `complete` records nothing; `incomplete` records `qmd-verify (incomplete)`; `stalled` records `qmd-verify (stalled)`; `failed` records `qmd-embed` and the verify label. A session that reported chunk failures but whose pending count fell continues the loop; its failure lines stay in the log.
6. Write the embed stamp (4.6) after every session and once more at the end with the outcome.

A backlog larger than `embed_max_sessions` sessions takes several nights. To drain it at once, the user runs `cairn embed --until-complete` (4.7), which loops up to 12 sessions under the same stop rules.

### 4.4 Power policy

New module `src/cairn/power.py`:

```python
def power_source() -> str:
    """``ac``, ``battery`` or ``unknown``. Never raises."""
```

- macOS: run `pmset -g batt` with a 5 s timeout. The first line is `Now drawing from 'AC Power'`, `Now drawing from 'Battery Power'` or `Now drawing from 'UPS Power'` (**Measured** for the first two; the UPS form is **Inferred**). `AC Power` gives `ac`. `Battery Power` and `UPS Power` give `battery`. A missing binary, a timeout, a non-zero exit or any other text gives `unknown`.
- Linux: read `/sys/class/power_supply/*/type` and `online`. Any supply of type `Mains` with `online` equal to `1` gives `ac`. Otherwise, a supply of type `Battery` gives `battery`. No supply gives `unknown`. **Inferred** from the kernel sysfs ABI; cairn has never read it.
- Any other platform gives `unknown`.
- The sysfs root and the `pmset` runner are module-level seams for the tests.

`unknown` counts as mains power: a desktop has no battery.

`cron.embed_on_battery`:

| Value | First session on battery |
| --- | --- |
| `defer` (default) | no embed; log `  [qmd-embed] deferred - on battery power; the next light sync on AC power runs it`; write the owed marker; the run exits 0 |
| `skip` | no embed; log `  [qmd-embed] skipped - on battery power; the next nightly run tries again`; no marker; the run exits 0 |
| `run` | embed as on mains power |

A deferred or skipped embed is not a failed step. The run ends `=== done ===` when nothing else failed.

### 4.5 Catch-up after a deferred night

After its own stages, a `light` run checks the owed marker (4.6). It runs ONE embed session (`embed_loop(config, record, nightly=False, max_sessions=1, respect_power=True)` with the configured battery policy) when all of these hold:

- the marker exists and its `database` equals the current QMD database (`qmd_resources()[1]`, `src/cairn/runtime.py:20-27`);
- `cron.embed` is `nightly` and `cron.embed_catchup` is `true`;
- `power_source()` is `ac` or `unknown`;
- the marker's `catchup_on` is not today's local date.

Before the session it writes today's local date to `catchup_on`, so a failing catch-up runs at most once a day. A `complete` outcome deletes the marker. The log line is `  [embed-catchup] running the embed deferred at TIME`. An `incomplete` catch-up keeps the marker; the next nightly run continues the backlog. Only a battery deferral writes the marker, so a large backlog never turns the hourly job back into an hourly embed.

### 4.6 State files

Both live next to the index stamp, under `$XDG_STATE_HOME/cairn/` (`state_home()`, `src/cairn/config.py:878-881`), and are written with `atomic_write` (`src/cairn/storage.py`). A write failure prints a warning on stderr and never fails the run, as `record_update` does (`src/cairn/qmd.py:237-254`).

`qmd-embed.json`, the embed stamp, written by a new `record_embed()` in `src/cairn/qmd.py`:

```json
{
  "version": 1,
  "database": "<resolved INDEX_PATH>",
  "finished_at": "2026-11-02T03:41:10Z",
  "outcome": "complete",
  "mode": "nightly",
  "sessions": 1,
  "documents": 18214,
  "vectors": 3173623,
  "pending": 0,
  "last_embed_at": "2026-11-02T03:41:10Z",
  "last_complete_at": "2026-11-02T03:41:10Z"
}
```

`outcome` is one of `complete`, `incomplete`, `stalled`, `failed`, `deferred`, `skipped`. `last_embed_at` is the end of the latest session in which QMD exited (any outcome but `deferred` and `skipped`). `last_complete_at` is the end of the latest `complete` run. Both are copied forward from the previous stamp when this run does not set them. `read_embed_stamp()` validates the shape as `read_stamp` does (`src/cairn/qmd.py:257-276`) and returns `None` for a missing, corrupt or other-version file, or for one whose `database` differs from the current one. `cairn sync --cron`, `--all` and `cairn embed` write it too, so deep recall and doctor see every embed.

`embed-owed.json`, the owed marker:

```json
{"version": 1, "database": "<resolved INDEX_PATH>", "since": "2026-11-02T03:00:04Z", "reason": "battery", "catchup_on": null}
```

Any `complete` outcome deletes it.

### 4.7 Commands

#### `cairn cron install`

Reads `cron.*` (section 6) and installs the jobs for the configured backend. The command name stays `cron` for backwards compatibility; the help text says "scheduled sync" for both backends.

Jobs by `cron.embed`:

| `cron.embed` | Jobs |
| --- | --- |
| `nightly` (default) | light job on `cron.schedule` running `sync --light`; nightly job at `cron.embed_at` running `sync --nightly` |
| `hourly` | one job on `cron.schedule` running `sync --cron` (the pre-2.21 behaviour, now at the configured priority) |
| `off` | light job only; vectors change only when the user runs `cairn embed` or `cairn sync --cron` |

**launchd backend.** New module `src/cairn/schedule.py`. Labels `dev.cairn.sync.light` and `dev.cairn.sync.nightly` (new constant `SYNC_LABEL_PREFIX = "dev.cairn.sync."` in `src/cairn/config.py` next to `ROUTINE_LABEL_PREFIX`); the `hourly` mode uses `dev.cairn.sync.light` with `sync --cron`. Plists in `~/Library/LaunchAgents/<label>.plist`. Render with `plistlib` as `render_plist` does (`src/cairn/routine.py:114-130`):

```python
{
    "EnvironmentVariables": background_environment(config),  # same block cron uses today
    "Label": "dev.cairn.sync.light",
    "ProcessType": "Background",          # "Standard" when cron.priority is "normal"
    "LowPriorityIO": True,                # omitted when cron.priority is "normal"
    "ProgramArguments": [*_cairn_invocation(), "sync", "--light"],
    "RunAtLoad": False,
    "StandardErrorPath": "<data_root>/logs/context-sync.log",
    "StandardOutPath": "<data_root>/logs/context-sync.log",
    "StartCalendarInterval": [{"Minute": 7}],
}
```

The nightly plist differs in `Label`, `ProgramArguments` (`sync --nightly`) and `StartCalendarInterval` (`{"Hour": 3, "Minute": 0}` from `cron.embed_at`). The log path is the same file the cron line appends to today (`src/cairn/admin.py:67-68`); `sync.log` writes it directly and prints only on a TTY (`src/cairn/sync.py:101-113`), so the redirect only catches stray output.

`cron.schedule` is a cron expression, and launchd has no cron syntax. `schedule.calendar_intervals(expr)` converts this subset and refuses every other form with a `ConfigError` (code `CONFIG_INVALID`) whose message ends `set cron.backend to "cron", or use a schedule of the form 'M * * * *'`:

| Expression | `StartCalendarInterval` |
| --- | --- |
| `M * * * *` | `[{"Minute": M}]` |
| `M1,M2 * * * *` | `[{"Minute": M1}, {"Minute": M2}]` |
| `*/N * * * *` (N from 1 to 59) | one dict per minute `0, N, 2N, ...` below 60 |
| anything with a non-`*` hour, day, month or weekday field | refused |

Install order, per job: `routine.launchctl("bootout", "gui/<uid>/<label>")` (not loaded is fine), `atomic_write(plist, text)`, `routine.launchctl("bootstrap", "gui/<uid>", plist)`. A bootstrap failure prints the `launchctl` stderr and exits 1, as routines do (`src/cairn/routine.py:177-185`). Calls go through `cairn.routine.launchctl`, so the existing test patch point covers them. A job that the configuration no longer wants (for example the nightly job after `cron.embed` changes to `off`) is booted out and its plist deleted.

**cron backend.** Two lines, each tagged so `_without_cairn_lines` removes them (the tag holds `cairn sync`):

```text
7 * * * * <priority prefix> /usr/bin/env K=V ... <cairn> sync --light >> <log> 2>&1 # cairn sync light
0 3 * * * <priority prefix> /usr/bin/env K=V ... <cairn> sync --nightly >> <log> 2>&1 # cairn sync nightly
```

The nightly line's schedule is `MINUTE HOUR * * *` from `cron.embed_at`. The priority prefix for `cron.priority: "background"` is `/usr/sbin/taskpolicy -b` on macOS, and on Linux `<nice> -n 10` followed by `<ionice> -c 3` when `shutil.which("ionice")` finds it at install time (absolute paths from `shutil.which`; `ionice` omitted when absent, with a note on stderr). `normal` gives no prefix. `%` is escaped as today.

**Migration, both backends.** Install always removes every crontab line that `_without_cairn_lines` matches, and on macOS boots out and deletes every `dev.cairn.sync.*` plist that the chosen backend does not use. So switching backends, or upgrading from the single hourly line, leaves exactly the configured jobs. When the removed crontab lines include one that runs `sync --cron` and the new configuration is not `hourly`, install prints `Removed the old hourly 'cairn sync --cron' crontab entry (it embedded every hour).` A crontab read failure stops install before any write, as today.

Output on macOS with the defaults:

```text
Removed the old hourly 'cairn sync --cron' crontab entry (it embedded every hour).
Installed dev.cairn.sync.light: hourly at minute 7, background priority, log <data_root>/logs/context-sync.log
Installed dev.cairn.sync.nightly: daily at 03:00 local, background priority, on battery: defer
```

Install is idempotent: a second run writes the same plists or lines and prints the same `Installed` lines without the `Removed` line.

#### `cairn cron uninstall`

Removes every cairn crontab line and, on macOS, boots out and deletes every `dev.cairn.sync.*` plist. It prints one `Removed ...` line per item, or `No cairn sync job found; nothing to remove.` It leaves the embed stamp and the owed marker in place.

#### `cairn cron status [--json]` (new)

Read-only. It reads the plists, the crontab and the two state files, and probes each label with `routine.launchctl("print", target, timeout=routine.LAUNCHCTL_PROBE_S)`.

```text
backend    : launchd
light      : dev.cairn.sync.light loaded, hourly at minute 7, background
nightly    : dev.cairn.sync.nightly loaded, daily at 03:00 local, on battery: defer
last embed : 2026-11-02T03:41:10Z complete (1 session, 0 pending)
owed       : none
legacy     : none
```

`--json` gives `{"backend", "jobs": [{"name", "label_or_tag", "installed", "loaded", "schedule", "argv_mode", "priority", "matches_config"}], "embed_stamp", "owed", "legacy_entries"}`. Exit 0 when every configured job is installed and matches the configuration, 1 otherwise.

#### `cairn embed [--sessions N | --until-complete] [--respect-power]` (new)

Registered in `src/cairn/__main__.py` `_ADMIN` as `"embed": "cairn.sync:embed_main"`.

- Takes the sync lock without blocking. When another sync holds it, print `another cairn sync is running for <data_root>; run 'cairn embed' again after it finishes` on stderr and exit 3. It records no request.
- Runs collection registration and `qmd update` (`_index_stages(embed=False)`) so new exports are indexed, then `embed_loop(config, record, nightly=False, max_sessions=N, respect_power=FLAG)`.
- `--sessions N`: 1 to 12, default 1. `--until-complete`: `max_sessions` 12; the stall rule still stops it. The two flags are mutually exclusive.
- `--respect-power`: apply `cron.embed_on_battery`. Off by default, because the user ran the command.
- Logs to `context-sync.log` under a `=== context-sync (embed) ... ===` header and echoes on a TTY like `sync`.
- Exit 0 on `complete`, `deferred` or `skipped`; 1 on `incomplete`, `stalled` or `failed`; 3 when busy; 2 on a usage error.

### 4.8 Doctor

Replace the cron check (`src/cairn/admin.py:1470-1474`) with a `sync schedule` group built on the same reader as `cron status`:

| Check | Result |
| --- | --- |
| every configured job installed and matching the config | `ok sync schedule - launchd: light hourly at :07, nightly 03:00 (background)` |
| a configured job missing | warn `no <job> job; run 'cairn cron install'` |
| an installed job differs from the config (schedule, argv mode, priority) | warn `installed <job> job differs from the config; run 'cairn cron install'` |
| a crontab line runs `sync --cron` while `cron.embed` is not `hourly` | warn `[SYNC_SCHEDULE_LEGACY]` `the old hourly 'cairn sync --cron' entry still embeds every hour; run 'cairn cron install'` |
| both a cairn crontab line and a `dev.cairn.sync.*` LaunchAgent are present | warn `two schedulers run cairn sync; run 'cairn cron install'` |
| `cron.backend` cron, `cron.priority` background, and the line has no priority prefix | warn `the cron line runs at normal priority; run 'cairn cron install'` |
| `cron.embed` is `nightly` and `last_embed_at` is missing or older than 48 h | warn `[EMBED_OVERDUE]` `no embed finished since TIME; check 'cairn cron status' and run 'cairn embed'` |
| owed marker present | ok line `embed owed since TIME (battery); the next light sync on AC power runs it` |

`_check_embeddings` (`src/cairn/admin.py:970-1002`) changes in two ways:

- In default mode, when `cron.embed` is `nightly`, the state is `pending`, and `last_embed_at` is within 30 hours, print `ok embeddings - D documents, V vectors, P pending; the nightly embed at HH:MM covers them` instead of the warning. With the nightly schedule, new documents are pending for most of each day by design. `--strict-index` still fails on pending, because setup needs a fully embedded index.
- Every "run 'cairn sync --cron'" next step becomes "run 'cairn embed'". The `QMD_INDEX_INCOMPLETE` row's `next_step` (`src/cairn/errors.py:160`) becomes `Run 'cairn embed'; investigate if the pending count does not fall across runs.` `verify_index`'s incomplete message (`src/cairn/sync.py:218-221`) becomes `the next nightly embed or 'cairn embed' continues (see 'cairn doctor' for the pending count)`.

New registry rows in `src/cairn/errors.py`, in the existing `ErrorCode` shape (`src/cairn/errors.py:41-57`), then regenerate `docs/error-codes.md` with `python -m cairn.errors --render`:

- `SYNC_SCHEDULE_LEGACY`: meaning "The scheduled sync is the pre-2.21 hourly entry that embeds every hour."; why "It uses the GPU every hour at normal priority."; next step "Run 'cairn cron install'."; verify `("cairn", "cron", "status")`; exit 0; retryable False; raised at `src/cairn/admin.py:doctor_main`; shown in "doctor [CODE] suffix".
- `EMBED_OVERDUE`: meaning "No QMD embed has finished in the last 48 hours while the nightly embed is configured."; why "Semantic and deep recall miss every document exported since then."; next step "Run 'cairn cron status', then 'cairn embed'."; verify `("cairn", "cron", "status")`; exit 0; retryable True; raised at `src/cairn/admin.py:doctor_main`; shown in "doctor [CODE] suffix".

## 5. Design B: deep recall

### 5.1 Decision: a CLI command plus skill text

The logic goes in a new CLI subcommand, `cairn recall search`, and the skills call it. Reasons from the code:

1. A time budget needs a process-group kill. `run_capped` (`src/cairn/procs.py:70`) already gives that, and the sync uses it for exactly this reason (`README.md:509-510`). A skill can only wrap `qmd query` in `timeout`, which macOS does not ship.
2. The trigger needs config values (`recall.deep.*`) and the vector state from `qmd status` and the embed stamp. `parse_status` and `read_embed_stamp` live in Python.
3. Merging, de-duplication and labels must be the same in `/recall` and `/search`, and testable with the `#!python` qmd stubs the suite already uses (`tests/test_sync.py:1-5`, `tests/test_recall_gaps.py:1-5`).

The rg pass stays in the skills. It depends on the guardrails in `skills/search/SKILL.md:74-114` and on reading each collection's `Path:` from `qmd collection show`; moving it is a separate change. The skills label rg hits `rg` themselves.

### 5.2 `cairn recall search`

New module `src/cairn/recall_search.py`; the parser and dispatch go in `src/cairn/recall.py` next to `gaps` (`src/cairn/recall.py:145-160`, `src/cairn/recall.py:178-179`).

```text
cairn recall search QUERY [-q VARIANT]... [-c COLLECTION]... [-n N] [--deep | --no-deep] [--json]
```

| Argument | Meaning |
| --- | --- |
| `QUERY` | one positional string, the user's own phrasing |
| `-q VARIANT` | repeatable, 0 to 6 extra phrasings for BM25 |
| `-c COLLECTION` | repeatable; passed unchanged to every `qmd search` and to the `qmd query` |
| `-n N` | BM25 results per phrasing, 1 to 20, default 5 |
| `--deep` | run the hybrid query (trigger `flag`) |
| `--no-deep` | never run it, whatever the counts |
| `--json` | the machine output of 5.7 |

Flow:

1. Load the config (`get_config()`). Read `recall.deep` (section 6).
2. If `-c` is given, check each name against `list_collections(qmd)` (`src/cairn/qmd.py:203-213`). An unknown name prints `unknown collection NAME; known: A, B, C` on stderr and exits 2. Without `-c`, pass no `-c`: QMD searches every collection, which is what the skills do today.
3. BM25: for each phrasing in `[QUERY, *VARIANTS]`, run `qmd search PHRASE -n N --format json [-c C]...` through `run_capped` with a 15 s timeout, at most 4 at a time (`concurrent.futures.ThreadPoolExecutor(max_workers=4)`). Parse each stdout as a JSON array. A call that times out, exits non-zero or prints no JSON array counts as failed.
4. If every BM25 call failed, exit 1 with code `RECALL_FAILED` (or `QMD_MISSING` when the binary cannot start), through `errors.emit` and, with `--json`, `errors.failure` (`src/cairn/recall.py:195-202` shows the pattern).
5. Decide the trigger (5.3). If there is none, set `hybrid.status` to `not-needed` (or `disabled` for `--no-deep`, or for `recall.deep.auto: false` without `--deep`) and go to step 8.
6. Check the vectors (5.5). If they are not usable, set the skip status and note, and go to step 8.
7. Run the hybrid query (5.4).
8. Merge (5.6) and print (5.7).

### 5.3 "Too little": the auto trigger

With `--deep`, the trigger is `flag`. With `--no-deep`, there is no trigger. Otherwise, when `recall.deep.auto` is `true`, the first rule that holds sets the trigger:

1. `few-hits`: the number of distinct `file` values across all successful BM25 calls is below `recall.deep.min_hits` (integer, default 3; 0 turns the rule off).
2. `low-score`: `recall.deep.min_top_score` is a number, and the highest BM25 `score` across all calls is below it (default `null`, which turns the rule off).

Counting distinct documents across every phrasing, not hits per call, means that four variants that each return the same two files still count as two. Section 3.5 shows why the score rule is off by default: BM25 scores from qmd 2.5.3 cluster between 0.8 and 0.95 for good and nonsense queries alike. The count rule fires mostly for rare terms. The skill adds a third, judgement trigger that the CLI cannot compute (section 8.1, Step 3.3b): the agent reads the BM25 titles and snippets, and if none is about the topic it runs the command once more with `--deep`.

### 5.4 The hybrid call

- Exactly one call per `cairn recall search` run: `qmd query QUERY -n DEEP_N --format json [-c C]... [--no-rerank]`, through `run_capped` with `timeout=recall.deep.timeout_s`. A timeout kills the process group (`src/cairn/procs.py:70`), so no Node child keeps the GPU busy.
- It uses `QUERY` only, not the variants: `qmd query` expands the query itself ("Hybrid search with auto expansion + reranking", `qmd --help`).
- `--no-rerank` is passed when `recall.deep.rerank` is `false` (the default). Section 2 measured 12 s without rerank against 24 s and 79 s with it. The agent reads the snippets and ranks them itself, so the LLM rerank buys little here. **Inferred** from one run each; the open questions ask the maintainer to confirm the default.
- The call takes no cairn lock. It only reads the index. **Inferred:** QMD's SQLite index allows a reader during a nightly embed; if the implementer finds a lock error, treat it as `failed` with the note of 5.7.

### 5.5 Vectors missing or stale

Before the hybrid call, read `index_status(qmd)` (`src/cairn/qmd.py:410-434`, 0.42 s measured) and `read_embed_stamp()`:

| State | Action | `hybrid.status` |
| --- | --- | --- |
| `qmd status` fails or prints an unknown format | skip | `skipped-status-unreadable` |
| `empty` or `no-vectors` | skip | `skipped-no-vectors` |
| stamp present and `last_embed_at` older than `recall.deep.max_vector_age_h` | skip | `skipped-stale` |
| stamp missing (an install that never embedded through 2.21) | run; the age is unknown, so `qmd status` decides | `ran` |
| `pending` > 0 | run, with the pending note | `ran` |
| `complete` | run | `ran` |

`recall.deep.max_vector_age_h` defaults to 72, so the daily cadence and one missed night never trip it, but three missed nights do. `null` turns the age rule off. Pending documents do not stop the hybrid call: with a nightly embed, today's sessions are pending all day, BM25 finds them, and the merge keeps them.

### 5.6 Merge, de-duplication and labels

- The key is the `file` URI (`qmd://COLLECTION/PATH`). `docid` is not the key, because two paths can share content. **Inferred** from `docid` being a short content hash.
- Each result keeps the best BM25 score across phrasings, the hybrid score, the phrasings that matched, and one snippet (the BM25 snippet when there is one, else the hybrid snippet).
- Order by reciprocal rank fusion with k = 60: `rank_score = max over phrasings of 1/(60 + bm25_rank) + 1/(60 + hybrid_rank)`, where a missing side adds 0 and ranks start at 1. Taking the BM25 maximum, not the sum, stops four variants from drowning the single hybrid list. Break ties by the higher raw score, then by `file`.
- `sources` is `["bm25"]`, `["hybrid"]` or `["bm25", "hybrid"]`; `label` is `bm25`, `hybrid` or `bm25+hybrid`. The skills add `rg` for their own pass.
- Return at most 20 merged results.

### 5.7 Output and exit codes

`--json`:

```json
{
  "query": "the vendor contract renewal",
  "variants": ["supplier agreement", "contract renewal date"],
  "collections": [],
  "bm25": {"calls": 3, "failed": 0, "documents": 2, "elapsed_s": 0.61},
  "hybrid": {
    "status": "ran",
    "trigger": "few-hits",
    "rerank": false,
    "elapsed_s": 11.8,
    "pending": 3,
    "note": "Hybrid ran; 3 documents are not embedded yet, so only BM25 can find them."
  },
  "results": [
    {
      "file": "qmd://claude-code-sessions/example-app/2026-10-12-3f2a9c1e.md",
      "collection": "claude-code-sessions",
      "docid": "#3a56d5",
      "title": "Renewal terms for the supplier",
      "line": 42,
      "snippet": "...",
      "sources": ["bm25", "hybrid"],
      "label": "bm25+hybrid",
      "scores": {"bm25": 0.82, "hybrid": 0.5},
      "variants": ["supplier agreement"],
      "rank_score": 0.0325
    }
  ]
}
```

`hybrid.status` is one of `not-needed`, `disabled`, `ran`, `skipped-no-vectors`, `skipped-stale`, `skipped-status-unreadable`, `timeout`, `failed`, `unsupported`. `trigger` is `flag`, `few-hits`, `low-score` or `null`. `note` is `null` or one line, exactly one of:

| Status | Note |
| --- | --- |
| `skipped-no-vectors` | `Hybrid skipped: the index has no vectors yet; BM25 results only. Run 'cairn embed'.` |
| `skipped-stale` | `Hybrid skipped: the last embed finished AGE ago (limit H h); BM25 results only. Run 'cairn embed'.` |
| `skipped-status-unreadable` | `Hybrid skipped: could not read 'qmd status' (REASON); BM25 results only.` |
| `timeout` | `Hybrid stopped after S s (recall.deep.timeout_s); BM25 results only.` |
| `failed` | `Hybrid failed (qmd exit N); BM25 results only.` |
| `unsupported` | `Hybrid skipped: qmd VERSION printed no JSON for 'qmd query --format json'; BM25 results only.` |
| `ran` with pending > 0 | `Hybrid ran; P documents are not embedded yet, so only BM25 can find them.` |
| a BM25 call failed but not all | `N of M BM25 searches failed; results are partial.` (joined to any other note with a space) |

AGE is whole hours (`50 h`). S is the configured timeout. P is the pending count.

Text output, one line per result, then the note when there is one:

```text
1. [bm25+hybrid] qmd://claude-code-sessions/example-app/2026-10-12-3f2a9c1e.md  Renewal terms for the supplier
2. [hybrid] qmd://notes/vendors/contract.md  Contract notes
hybrid: ran (few-hits, 11.8 s, no rerank)
```

Exit codes: 0 when the command ran, with or without results and whatever the hybrid status; 1 when every BM25 call failed or the config cannot load; 2 on a usage error or an unknown collection. A hybrid failure never changes the exit code: BM25 results stand on their own.

### 5.8 qmd version differences

- cairn is tested with qmd 2.5.3 (`TESTED_QMD_VERSION`, `src/cairn/qmd.py:339`). The command does not gate on the version number, in line with the doctor rule that "a newer version is judged by the same runtime and output checks, not by its number" (`README.md:527-528`).
- When `qmd search --format json` prints no JSON array, that BM25 call counts as failed (5.2 step 3). When all fail, the exit is 1, and the skill falls back to the plain `qmd search` it uses today (8.1).
- When `qmd query --format json` prints no JSON array, the status is `unsupported` and the note names the version from `qmd_version()` (`src/cairn/qmd.py:437-448`), called only in this case. **Inferred:** qmd 1.x may lack `--format json` or `--no-rerank`; the implementer must not assume either way, and the `unsupported` path covers both.
- An unknown option error from `--no-rerank` exits non-zero. Retry once without `--no-rerank` inside the same time budget when stderr names the option, and add `rerank: true` to the JSON. **Inferred** error shape; match on the option name only.

### 5.9 Collection scoping

The same `-c` list goes to every BM25 call and to the hybrid call, so the merge compares like with like. No `-c` means every QMD collection, including ones cairn does not manage (a user's notes folder), as today. `collection` in each result comes from the `file` URI, so a result is attributed even when QMD prints no collection key.

## 6. Config keys

All keys are optional. A missing key takes the default. Parse them in `src/cairn/config.py` into two frozen dataclasses with defaults, `SyncSchedule` (field `schedule` on `CairnConfig`) and `RecallPolicy` (field `recall`). Keep `CairnConfig.cron_schedule` as it is (`src/cairn/config.py:289`) so existing callers and `config show` JSON keep working. A wrong type or value raises `ConfigError` with code `CONFIG_INVALID` and names the key, the value and the allowed values. Validate `HH:MM` with the existing `_AT_RE` (`src/cairn/config.py:637`). Keys starting with `//` are comments, as in the rest of the config.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cron.schedule` | string, 5-field cron | `"7 * * * *"` | unchanged key; now the light job's schedule (the hourly full job's when `cron.embed` is `hourly`) |
| `cron.backend` | `"auto"`, `"launchd"`, `"cron"` | `"auto"` | `auto` is launchd on macOS, cron elsewhere; `launchd` off macOS is a `CONFIG_INVALID` error at install |
| `cron.priority` | `"background"`, `"normal"` | `"background"` | scheduler priority for every scheduled sync job |
| `cron.embed` | `"nightly"`, `"hourly"`, `"off"` | `"nightly"` | which job embeds |
| `cron.embed_at` | `"HH:MM"` local | `"03:00"` | nightly job start |
| `cron.embed_deadline` | `"HH:MM"` local | `"07:00"` | no new embed session starts after this, except the first |
| `cron.embed_max_sessions` | integer 1 to 12 | `3` | nightly sessions while the pending count falls (each at most 30 minutes in qmd 2.5.3) |
| `cron.embed_on_battery` | `"defer"`, `"skip"`, `"run"` | `"defer"` | section 4.4 |
| `cron.embed_catchup` | boolean | `true` | section 4.5 |
| `recall.deep.auto` | boolean | `true` | allow the `few-hits` and `low-score` triggers |
| `recall.deep.min_hits` | integer 0 to 50 | `3` | distinct BM25 documents below which hybrid runs; 0 turns it off |
| `recall.deep.min_top_score` | number 0 to 1, or `null` | `null` | top BM25 score below which hybrid runs |
| `recall.deep.timeout_s` | integer 1 to 600 | `30` | hybrid time budget; a low floor of 1 keeps the timeout test fast |
| `recall.deep.rerank` | boolean | `false` | pass no `--no-rerank` when `true` |
| `recall.deep.n` | integer 1 to 50 | `10` | hybrid results requested |
| `recall.deep.max_vector_age_h` | integer 1 to 8760, or `null` | `72` | section 5.5 |

`cairn config set` (`_set_dotted`, `src/cairn/admin.py:456-527`) accepts every key above and coerces it: booleans from `true`/`false`, integers, `null`/`none` for the nullable keys. Extend the "unsupported key" message to list `cron.<field>` and `recall.deep.<field>`. `cairn config unset` accepts them and removes the key, so the default applies again. `config show` prints a `cron` block and a `recall` block; `config show --json` adds `"cron": {...}` and `"recall": {"deep": {...}}` objects with the resolved values and keeps `"cron_schedule"`.

Both config templates (`cairn.config.example.json:7-8` and `src/cairn/config.example.json:7-8`, which `tests/test_config_examples.py` keeps in step) change to:

```json
"//cron": "Optional. The scheduled sync. 'schedule' is the light job (exports + qmd update, no embed) as a 5-field crontab expression, default '7 * * * *'; on the launchd backend it must have the form 'M * * * *', 'M1,M2 * * * *' or '*/N * * * *'. 'embed' is nightly | hourly | off. 'embed_at' and 'embed_deadline' are local HH:MM. 'embed_on_battery' is defer | skip | run. 'backend' is auto | launchd | cron. 'priority' is background | normal. Run 'cairn cron install' after a change.",
"cron": { "schedule": "7 * * * *", "embed": "nightly", "embed_at": "03:00", "embed_deadline": "07:00", "embed_max_sessions": 3, "embed_on_battery": "defer", "embed_catchup": true, "backend": "auto", "priority": "background" },
"//recall": "Optional. 'deep' controls the one hybrid qmd query that 'cairn recall search' runs on --deep or when BM25 finds fewer than min_hits distinct documents. rerank false passes --no-rerank (faster). max_vector_age_h skips hybrid when the last embed is older; null turns that off.",
"recall": { "deep": { "auto": true, "min_hits": 3, "min_top_score": null, "timeout_s": 30, "rerank": false, "n": 10, "max_vector_age_h": 72 } },
```

## 7. CLI changes

| Command | Change |
| --- | --- |
| `cairn sync --light` | new mode (4.2) |
| `cairn sync --nightly` | new mode (4.2) |
| `cairn sync` mode flags | now mutually exclusive |
| `cairn embed` | new command (4.7) |
| `cairn cron install` | installs the jobs of 4.7 for the chosen backend; migrates the old entry |
| `cairn cron uninstall` | removes crontab lines and `dev.cairn.sync.*` LaunchAgents |
| `cairn cron status` | new, read-only (4.7) |
| `cairn recall search` | new (5.2) |
| `cairn config set` / `unset` | accept the keys of section 6 |
| `cairn doctor` | the checks of 4.8 |

`cairn help` (`src/cairn/__main__.py:39-121`) changes:

```text
recall (temporal view over shared exported sessions; topic search runs BM25, hybrid on demand):
  cairn recall search "QUERY" -q "VARIANT" --json   one BM25 search per phrasing; hybrid on --deep or too few hits
```

added after the `recall decisions` line, and in "orchestration / admin":

```text
  cairn sync [--hook | --light | --nightly | --cron | --all]   sync every source, then refresh QMD
  cairn embed [--sessions N | --until-complete]   run QMD embed sessions now (drains a backlog)
  cairn cron install                         install the hourly light sync and the nightly embed jobs
  cairn cron uninstall                       remove the cairn sync jobs (crontab and LaunchAgents)
  cairn cron status [--json]                 the installed sync jobs, the last embed and any owed embed
```

replacing the current `cairn sync`, `cairn cron install` and `cairn cron uninstall` lines (`src/cairn/__main__.py:90`, `src/cairn/__main__.py:106-107`). Change the line `cairn help` prints for doctor to `check install, config, qmd + index, rg, sync schedule, PATH`.

## 8. Skill text changes

Each block below is the exact new text. Unchanged sections are not repeated.

### 8.1 `skills/recall/SKILL.md`

Frontmatter (replaces lines 3 to 5):

```yaml
description: Loads context from previous sessions and notes. Temporal queries (yesterday, last week, a date) list sessions chronologically from the shared exported store via the cairn CLI; topic queries search across QMD collections (BM25, plus one hybrid query when asked or when BM25 finds too little) plus an exact-match ripgrep pass; decision queries list recorded decisions. Every recall ends with "One Thing" - the single highest-leverage next action synthesised from the results. Use when the user says "recall", "what did we work on", "load context about", "remember when we", "prime context from past sessions", "what was I doing", "session history", "digest yesterday", "daily digest", "cite the frame", "what did we decide", "list our decisions", "verify journal citations", "missing sessions", "why can't recall see", "deep recall", or "search harder". Requires the cairn CLI and qmd (install with the setup skill); rg is optional.
argument-hint: "[yesterday|today|last week|this week|YYYY-MM-DD|TOPIC] [--deep]"
allowed-tools: Bash(cairn:*), Bash(qmd:*), Bash(rg:*), Bash(head:*)
```

`argument-hint` is quoted because the 2.20.1 changelog records that an unquoted `[a] [b]` form is not valid YAML. **Inferred:** the existing unquoted single-bracket hint parses only because it is one flow sequence; with two, quote it.

Step 1, add one bullet after the **Topic** bullet (after line 28):

```markdown
- **Deep** - a topic query with "deep recall", "search harder", "dig deeper" or `--deep`, or
  the user says they only vaguely remember it or do not know the words used → Step 3 with
  `--deep` on the first search.
```

Step 3, replace lines 144 to 211 with:

````markdown
## Step 3: Topic recall (BM25, hybrid when needed, and rg)

1. **Enumerate the collections to search** - do not hardcode or guess names. Run the
   gaps report once, in parallel with it (Step 4 reads it):

   ```bash
   qmd collection list
   cairn recall gaps --json
   ```

   Use the returned collection names (e.g. `notes`, `claude-code-sessions`,
   `codex-sessions`, `cursor-sessions`, `granola-sessions`, `service-docs`) as the valid
   `-c` values. (cairn-managed integrations + their enabled state: `cairn config show`.)

2. **Expand the query.** BM25 is keyword-based, so the user's phrasing often misses the
   words the session actually used. Generate 3–4 alternative phrasings (synonyms / related
   terms) for the topic.

3. **Search** with one command. It runs BM25 for the query and every variant, and runs
   one hybrid (`qmd query`) search only when it is asked to or when BM25 finds too little:

   ```bash
   cairn recall search "QUERY" -q "VARIANT 1" -q "VARIANT 2" -q "VARIANT 3" -c COLLECTION -n 5 --json
<!-- cairn-cli: skip -->
   ```

   Repeat `-c` for each relevant collection, or omit it to search all of them. Prioritise
   session collections for "what did we do" questions; include `notes` /
   `granola-sessions` / `service-docs` as relevant.

   a. Add `--deep` to this first command when Step 1 classified the query as **Deep**.
      Before a `--deep` command, tell the user in one line that a deep search takes up
      to about 30 seconds.
   b. If the command ran without a hybrid search (`hybrid.status` is `not-needed`) and,
      after you read the titles and snippets, none of the results is about the topic, run
      the same command once more with `--deep`. Never run more than one `--deep` command
      in one recall.
   c. Read `results` (each has `file`, `title`, `snippet` and `label`) and `hybrid`. If
      `hybrid.note` is not null, print it as one line under the results. Do not retry a
      skipped, timed-out or failed hybrid search.
   d. If the `recall search` command fails with `invalid choice` (an older cairn), or exits 1,
      run `qmd search "VARIANT" -c COLLECTION -n 5` for each variant in parallel instead,
      as before, and say in one line that deep recall needs a newer cairn or that the
      search command failed. Use `qmd search` (BM25) for this fallback, not `qmd query`.

4. **Exact-match pass (rg).** If the topic contains literal terms - an identifier, error
   string, file path, product or person name - run one rg pass for them in parallel with
   step 3. BM25 tokenises such terms badly, and rg also sees files that QMD has
   not indexed yet. Skip this step if rg is not installed.

   Resolve each collection's directory from its `Path:` line (never hardcode paths -
   collections can live outside cairn's `data_root`):

   ```bash
   qmd collection show COLLECTION
   ```

   Then list matching files, newest first, capped:

   ```bash
   rg -l -i -F -e "TERM" --type md --sortr modified "PATH1" "PATH2" ... | head -n 10
<!-- cairn-cli: skip -->
   ```

   `-e` keeps a query that starts with `-` from being read as a flag.

   Never print matched lines from a whole collection. If you need lines, run rg again on
   the chosen files only, with `-M 300 -m 5` (a session transcript can hold a single line
   of several megabytes). See the search skill for the full rg rules.

5. **Merge and label.** The `recall search` command already merged its own hits by document
   and labelled each one `bm25`, `hybrid` or `bm25+hybrid`. Add the rg hits: an rg hit at
   `<collection Path>/<rel>` is the same document as `qmd://COLLECTION/<rel>`, so append
   `+rg` to that result's label; an rg-only hit gets the label `rg`. Keep the command's
   order, and rank rg-only hits after the others unless the literal term is the point of
   the query. Present the top ~5 unique hits, each with its label in brackets:

   ```text
   1. [bm25+hybrid] qmd://claude-code-sessions/example-app/2026-10-12-3f2a9c1e.md - Renewal terms
   2. [rg] qmd://notes/vendors/contract.md - Contract notes
   ```

6. **Fetch full context** for the top 2–3:

   ```bash
   qmd get "qmd://collection/path/to/file.md" -l 50
   ```

   Organise by type: sessions (what was worked on, decisions, status), notes (research,
   plans), meetings (discussions, attendees), docs (architecture, APIs).

→ Go to Step 4 (Gaps).
````

Notes, replace the bullet on line 308 with:

<!-- cairn-cli: skip -->
```markdown
- Topic recall runs BM25 first. `cairn recall search` adds one hybrid (`qmd query`)
  search only on `--deep` or when BM25 finds fewer distinct documents than
  `recall.deep.min_hits` (default 3). A hybrid search takes seconds, BM25 a fraction of a
  second. Vectors are refreshed by a nightly embed, so documents from today are found by
  BM25 and rg; the hybrid note says how many are not embedded yet.
```

Step 4 (Gaps), Step 5 (One Thing), the fallbacks and the coverage section do not change. The One Thing still ends every recall, deep or not.

### 8.2 `skills/search/SKILL.md`

Frontmatter (replaces lines 3 to 5):

<!-- cairn-cli: skip -->
```yaml
description: Search previously exported Cairn sessions, notes, meetings and indexed docs using QMD BM25 or exact ripgrep, or one hybrid search with --deep. Use when the user says "cairn search", "search my sessions", "search past sessions", "search my meetings", "grep my sessions", or "exact search", or otherwise asks to locate material in personal session history. Do not activate for ordinary web searches, repository code searches, or unrelated uses of find/look up. Requires qmd; rg is optional.
argument-hint: "QUERY [-n NUM] [-c COLLECTION] [--full] [--exact] [--deep]"
allowed-tools: Bash(qmd:*), Bash(rg:*), Bash(head:*), Bash(cairn recall gaps:*), Bash(cairn recall search:*), Bash(cairn errors:*)
```

No new quoted trigger: `/search --deep` is a flag, not a phrase, so the routing eval and its counts for `search` do not change.

Usage block, add one line after the `--exact` example (after line 34):

```text
/search the thing about vendor terms --deep   # one hybrid (BM25 + vector) search, slower
```

Step 1, add one flag bullet after the `--exact` bullet (after line 46), and one sentence to "Choose the engine":

<!-- cairn-cli: skip -->
```markdown
   - `--deep` - one hybrid search (BM25 + vectors) through `cairn recall search`; only
     when the user passes it
```

```markdown
   Use the deep path only when `--deep` is given; never choose it yourself. `--exact`
   wins over `--deep`.
```

Add a new step after step 3 (after line 72), and renumber the later steps:

````markdown
4. **Deep path (`--deep` only):**

   ```bash
   cairn recall search "QUERY" --deep -n NUM [-c COLLECTION] --json
   ```

   Tell the user in one line that this takes up to about 30 seconds. Present `results`
   as in step 6, with each hit's `label` (`bm25`, `hybrid` or `bm25+hybrid`) in
   brackets. If `hybrid.note` is not null, print it as one line under the results. If the
   command exits non-zero or says `invalid choice`, run the BM25 path of step 3 and say
   in one line that deep search is not available.
````

The closing rule (line 149) becomes:

```markdown
Do NOT: expand the query into variants, fetch full documents unless asked, synthesise a
"One Thing", run parallel searches across variants, or add `--deep` unless the user
passed it - that's `/recall`'s job.
```

The sentence on line 71 ("Use `qmd search` (BM25), not `qmd query` (hybrid) - BM25 is much faster, and speed is the point for inline lookups.") stays, because the default path does not change.

### 8.3 `skills/setup/SKILL.md`

- Step 6 (line 248): `cairn cron install     # hourly light sync + nightly embed; LaunchAgents on macOS, crontab elsewhere`
- 7a (lines 280 to 281):

  ```bash
  cairn doctor --strict-index   # install, config, qmd version + runtime, collections, index, sync schedule
  cairn cron status             # the light and nightly jobs are installed and loaded
  ```

- Step 5, the backlog sentence (lines 237 to 241) becomes: "A first embed of a large history can pass QMD's 30-minute session limit. QMD 2.5.3 removes the vectors of a partly embedded document, so it restarts on the next run. Run `cairn embed --until-complete`: it runs embed sessions while the pending count falls and stops when it stops falling (a document may never fit in one session); report that case."
- Closing summary (lines 315 to 319): "Mention that the SessionEnd hook exports and keyword-indexes a finished session at once, the hourly light sync keeps every source keyword-indexed, and a nightly embed (03:00 by default, on mains power) refreshes vectors. `/recall` uses vectors only for a deep search (`/recall --deep TOPIC`, or when keyword search finds too little)."

### 8.4 `skills/cairn/SKILL.md` and references

- Line 27: "`cairn doctor` to check install, config, qmd, the sync schedule, and PATH."
- Line 127: `cairn sync [--hook | --light | --nightly | --cron | --all]   # sync enabled sources, then refresh QMD index`
- Line 138: `... # check install, config, qmd + index, sync schedule, PATH, stores`
- Lines 144 to 145, replaced by:

  ```text
  cairn cron install                     # install the hourly light sync and the nightly embed
  cairn cron uninstall                   # remove the cairn sync jobs
  cairn cron status [--json]             # installed jobs, last embed, owed embed
  cairn embed [--sessions N | --until-complete]   # run embed sessions now
  ```

- Line 162 to 163: "`--hook` is the legacy fast sync (on_hook sources only); `--light` is the hourly scheduled sync (all sources, keyword index only); `--nightly` adds the embed under the power policy; `--cron` and `--all` export, index and embed once."
- `skills/cairn/reference/service-docs.md:27`: "Indexing happens automatically through the hourly light sync, and vectors through the nightly embed. Manual reindex:"
- `references/harnesses/claude.md:9-16` and `references/harnesses/codex.md:10-16` keep `cairn sync --cron`: a first sync must embed.

### 8.5 Routing eval

Add one row per new recall phrase to `skills/recall/routing-eval.jsonl` and change the count in `tests/test_skill_routing.py:384` from 15 to 17. Follow CONTRIBUTING.md "Changing a skill description": the intent must not copy a phrase of two or more words. Candidate rows, to check with `uv run python -m unittest tests.test_skill_routing -v`:

```json
{"intent": "go deep on recall for the vendor contract, I barely remember what we called it", "expected": "recall"}
{"intent": "that came back thin, search much harder through what we did on the billing rewrite", "expected": "recall"}
```

If the test reports an overlap with `search` for the second row, reword it; do not declare the pair.

## 9. Migration and backwards compatibility

- **Upgrade without re-running install.** The old crontab line keeps running `cairn sync --cron`, which keeps its meaning: export, index and embed every hour. Nothing breaks and nothing changes until the user runs `cairn cron install`. `cairn doctor` warns with `SYNC_SCHEDULE_LEGACY`, and the CHANGELOG says to re-run the install. The setup skill's update path already refreshes cron entries (`README.md:137-138`).
- **Re-running install** removes the old line and installs the new jobs (4.7). A user who wants the old behaviour sets `cron.embed` to `hourly`, which keeps one job running `sync --cron`, now at background priority unless `cron.priority` is `normal`.
- **Config files** without the new keys load with the defaults. `cron.schedule` keeps its meaning for the cron backend. On the launchd backend, a schedule outside the subset of 4.7 makes install exit 2 with the hint; loading the config does not fail, so every other command still works.
- **`config show --json`** keeps `cron_schedule` and adds `cron` and `recall` objects. A reader of the old key keeps working.
- **Skills and engine at different versions.** A new skill on an old engine gets `invalid choice` from `cairn recall search` and falls back to plain `qmd search` (8.1 step 3d, 8.2 step 4). An old skill on a new engine keeps calling `qmd search` and never runs hybrid.
- **Sync request files.** A request file named `light` or `nightly` written by 2.21 and read by an older running sync is ignored, because `consume_requests` iterates `MODE_RANK` (`src/cairn/sync.py:340-349`). That case lasts one run at most and loses nothing that the next hourly run does not redo.
- **Downgrade.** An older cairn's `cairn cron install` removes the new crontab lines (they hold `cairn sync`) but not the LaunchAgents. The CHANGELOG tells a user who downgrades to run `cairn cron uninstall` first.
- **Recall with no embed stamp** (an index embedded before 2.21): deep recall relies on `qmd status` only (5.5). The first nightly embed writes the stamp.

## 10. Failure modes

| Failure | Behaviour |
| --- | --- |
| Laptop asleep at 03:00 | launchd fires the nightly job once on wake (4.1). The first session runs whatever the clock says; later sessions respect `embed_deadline`. Under the cron backend the night is missed; doctor reports `EMBED_OVERDUE` after 48 h. |
| On battery at 03:00 | `defer`: no embed, owed marker, catch-up at the next light run on AC power, at most once a day. `skip`: tries again the next night. |
| Power changes to battery mid-loop | the loop stops before the next session and writes the owed marker. A running session is not stopped. |
| `pmset` missing, slow or unreadable | `unknown`, treated as mains power. |
| Backlog bigger than one night | `incomplete` with `qmd-verify (incomplete)` in the log, the run fails as today, the next night continues; `cairn embed --until-complete` drains it at once. |
| A document that never fits in 30 minutes | `stalled` after the first session in which the pending count does not fall; the run fails with the stalled message; doctor still shows the pending count. |
| Background priority makes the embed slower | more sessions per night. Compare `[qmd-embed] exited 0 (Ns)` durations before and after (11.2). **Inferred** risk. |
| Light job overlaps the nightly job | the second one records a request and exits 0 (`src/cairn/sync.py:312-327`); the holder services it. BM25 updates pause while a nightly loop holds the sync lock (at most `embed_max_sessions` × 30 minutes); the hook worker still updates between sessions, because the loop releases the index lock. |
| `launchctl bootstrap` fails | install prints the stderr and exits 1; the other job's state is reported; re-run install. |
| `cron.backend: launchd` off macOS | `CONFIG_INVALID` at install, exit 2. |
| Cron schedule outside the launchd subset | install exits 2 with the hint (4.7). |
| Both schedulers present | doctor warns; install removes the extra one. |
| Hybrid query slow | killed at `recall.deep.timeout_s` with its process group; BM25 results returned; note printed. |
| No vectors or vectors older than 72 h | hybrid skipped with a note; BM25 results returned. |
| qmd without `--format json` for `query` | `unsupported` note; BM25 results returned. |
| qmd missing | `cairn recall search` exits 1 with `QMD_MISSING`; the skill reports `cairn errors QMD_MISSING`. |
| Unknown `-c` name | exit 2 with the known names. |
| Hybrid query during a nightly embed | it reads while the embed writes; slower because both use the GPU. **Inferred:** no lock error; if one occurs, `failed` with a note. |

## 11. Test plan

Every new test comes with its `make discriminates TEST=...` line in the PR body (`CLAUDE.md`, CONTRIBUTING.md "A new test must fail without its fix"). QMD, `launchctl`, `crontab` and `pmset` are always stubs or patches; no test needs a real install. Save test output to a file: `make check > /tmp/cairn-check.txt 2>&1; echo "exit=$?"`.

### 11.1 Unit tests

`tests/test_sync_schedule.py` (new; patches `cairn.routine.launchctl`, `cairn.routine.is_macos`, `cairn.admin._current_crontab`, `cairn.admin._write_crontab`):

- `test_cron_backend_writes_light_and_nightly_lines`: two lines; the first ends `sync --light >> LOG 2>&1 # cairn sync light`, the second starts `0 3 * * *` and runs `sync --nightly`; an unrelated line stays first.
- `test_cron_backend_background_prefix_on_macos`: with `is_macos` true, each line holds `/usr/sbin/taskpolicy -b /usr/bin/env`.
- `test_cron_backend_nice_ionice_on_linux`: with `is_macos` false and `shutil.which` patched, each line holds `<nice> -n 10 <ionice> -c 3`; with `ionice` missing, only `nice`, and stderr has the note.
- `test_cron_backend_normal_priority_has_no_prefix`.
- `test_install_removes_legacy_line_and_says_so`: a crontab with the 2.20 line (`... sync --cron ... # cairn sync`) loses it; stdout has the `Removed the old hourly` line.
- `test_install_is_idempotent`: two installs give equal crontab writes and no `Removed` line the second time.
- `test_embed_hourly_installs_one_cron_job`: `cron.embed` `hourly` gives one line running `sync --cron` with the prefix.
- `test_embed_off_installs_light_only_and_removes_nightly`.
- `test_calendar_intervals_subset`: `"7 * * * *"` gives `[{"Minute": 7}]`; `"5,35 * * * *"` gives two; `"*/20 * * * *"` gives minutes 0, 20, 40; `"7 9 * * *"` and `"7 * * * 1"` raise `ConfigError` with code `CONFIG_INVALID` and the hint text.
- `test_launchd_plist_keys`: `ProcessType` `Background`, `LowPriorityIO` true, `RunAtLoad` false, `ProgramArguments` ends `["sync", "--light"]`, `EnvironmentVariables` equals `background_environment(config)`; the nightly plist has `{"Hour": 3, "Minute": 0}`; `cron.priority` `normal` gives `Standard` and no `LowPriorityIO`.
- `test_launchd_install_order_and_crontab_cleanup`: the recorded calls are bootout then bootstrap per label; the cairn crontab line is removed; plists are written under a temporary `HOME`.
- `test_launchd_uninstall_removes_both_and_crontab`: bootout both, delete both plists, strip crontab; a second uninstall prints `No cairn sync job found; nothing to remove.`
- `test_launchd_bootstrap_failure_exits_1`.
- `test_launchd_backend_off_macos_is_config_invalid`.
- `test_cron_status_json_reports_mismatch`: an installed plist with minute 5 against a config of minute 7 gives `matches_config` false and exit 1.

`tests/test_sync_embed_policy.py` (new; uses the scenario QMD stub pattern of `tests/test_sync.py:22-60` and a patched `power.power_source` and clock):

- `test_light_mode_never_runs_embed`: the stub records `update` and no `embed`.
- `test_nightly_on_ac_embeds_and_writes_stamp`: one `embed` call; the stamp has `outcome` `complete`, `mode` `nightly`, `sessions` 1.
- `test_nightly_on_battery_defer_writes_marker_and_exits_0`: no `embed` call; the log has `[qmd-embed] deferred - on battery power`; the marker has `reason` `battery`; exit 0 and `=== done ===`.
- `test_nightly_on_battery_skip_writes_no_marker`.
- `test_nightly_on_battery_run_embeds`.
- `test_unknown_power_counts_as_ac`.
- `test_loop_continues_while_pending_falls`: status scenarios pending 10, 4, 0 give three sessions and `complete`; no failed step.
- `test_loop_stops_when_pending_stalls`: pending 10, 10 gives two sessions, `stalled`, failed step `qmd-verify (stalled)`.
- `test_loop_stops_at_max_sessions`: pending 10, 8, 6 with max 3 gives `incomplete` and `qmd-verify (incomplete)`.
- `test_deadline_allows_first_session_only`: the clock after `embed_deadline` gives one session.
- `test_index_lock_released_between_sessions`: the stub records that the index lock was free at least once between two `embed` calls (extend the existing lock probe in `SCENARIO_QMD`).
- `test_battery_mid_loop_stops_and_marks_owed`.
- `test_light_catch_up_runs_once_per_day`: marker present, AC power: one `embed` call and the marker is deleted on `complete`; with an `incomplete` result, a second light run the same day makes no `embed` call.
- `test_catch_up_ignores_marker_for_other_database`.
- `test_follow_up_light_run_services_nightly_request`: a `nightly` request file present when a `light` run finishes gives one embed.
- `test_mode_flags_are_mutually_exclusive`: `sync --light --cron` exits 2.
- `test_embed_command_busy_exits_3`: the sync lock held gives exit 3 and the stderr line.
- `test_embed_until_complete_caps_at_12`.

`tests/test_power.py` (new): `pmset` fixtures for AC, battery, UPS, empty output, non-zero exit and timeout; sysfs fixture trees under a temporary directory for mains online, mains offline with a battery, and no supplies.

`tests/test_qmd_health.py` (extend): `record_embed` and `read_embed_stamp` round trip; copy-forward of `last_complete_at` after an `incomplete`; a corrupt file, another version and another database read as `None`.

`tests/test_doctor_index.py` (extend):

- `test_pending_is_ok_after_recent_nightly_embed`: `cron.embed` `nightly`, stamp 2 h old, pending 3: an `ok embeddings` line naming the nightly time, no `QMD_INDEX_INCOMPLETE`; with `--strict-index` it still fails.
- `test_pending_warns_without_recent_embed`: stamp 40 h old gives the warning with `run 'cairn embed'`.
- `test_legacy_cron_line_warns_sync_schedule_legacy`.
- `test_embed_overdue_warns`: stamp 50 h old gives `[EMBED_OVERDUE]`.
- `test_two_schedulers_warn`.

`tests/test_recall_search.py` (new; QMD is a `#!python` stub that prints JSON fixtures per subcommand and records every argv to a file):

- `test_enough_bm25_hits_runs_no_hybrid`: 5 distinct files, no flag: no `query` call; `hybrid.status` `not-needed`.
- `test_deep_flag_runs_exactly_one_hybrid_with_same_collections`: one `query` argv holding `--format json`, `-n 10`, both `-c` values and `--no-rerank`.
- `test_rerank_true_omits_no_rerank`.
- `test_few_hits_trigger`: 2 distinct files across three phrasings (the same files repeated) give trigger `few-hits`.
- `test_min_hits_zero_disables_count_trigger`.
- `test_low_score_trigger_when_configured`.
- `test_no_deep_and_auto_false`: `--no-deep` gives `disabled` with 0 hits; `auto: false` without `--deep` gives `disabled`.
- `test_merge_labels_and_dedup`: the same `file` from BM25 and hybrid gives one result with `label` `bm25+hybrid` and both scores; a hybrid-only file gives `hybrid`.
- `test_rrf_order`: a fixture where the order differs from raw-score order; assert the RRF order.
- `test_no_vectors_skips_hybrid_with_note`: status `no-vectors` gives no `query` call and the exact note text of 5.7.
- `test_stale_stamp_skips_hybrid`: `last_embed_at` 100 h old with the default limit.
- `test_missing_stamp_runs_hybrid`.
- `test_pending_runs_hybrid_with_note`.
- `test_hybrid_timeout_returns_bm25_and_kills_group`: the stub sleeps 5 s, `timeout_s` 1: `timeout` status, exit 0, BM25 results present, and the stub's child process (it writes its pid) no longer exists.
- `test_hybrid_non_json_is_unsupported`.
- `test_no_rerank_unknown_option_retries_once`.
- `test_all_bm25_failed_exits_1_with_code`.
- `test_partial_bm25_failure_note`.
- `test_unknown_collection_exits_2`.
- `test_qmd_missing_exits_1_qmd_missing`.
- `test_text_output_shape`.

`tests/test_config.py` and `tests/test_admin_config.py` (extend): defaults when the keys are absent; each invalid value of section 6 raises `CONFIG_INVALID` naming the key; `cairn config set cron.embed_at 02:30`, `cron.embed_catchup false`, `recall.deep.min_top_score null` and `recall.deep.timeout_s 45` round-trip; `config unset recall.deep.timeout_s` restores the default; `config show --json` keeps `cron_schedule` and has the new objects.

Existing tests that change: `tests/test_hooks.py:230-259` (`test_cron_command_retains_config_and_index_environment`) now finds the light line, whose command runs `sync --light`; keep its environment assertions. `tests/test_skill_routing.py:384` count 15 to 17. `tests/test_docs_cli.py` parses every new documented command without change to the test. `tests/test_errors.py` (or the render check) needs the regenerated `docs/error-codes.md`. `tests/test_config_examples.py` needs both templates equal.

### 11.2 Manual checks (macOS, after `uv tool install` of the branch)

Run them in order. Each line gives the command and the expected result.

1. `cairn cron install` prints the three lines of 4.7 (the `Removed` line only on a machine with the old entry). Exit 0.
2. `crontab -l | grep -c "cairn sync"` prints `0`.
3. `launchctl print gui/$(id -u)/dev.cairn.sync.light | grep -E "spawn type|Minute"` shows `spawn type = background (5)` and `"Minute" => 7`. (The routine LaunchAgent shows `spawn type = background (5)` today, **Measured**.)
4. `launchctl print gui/$(id -u)/dev.cairn.sync.nightly | grep -E "Hour|Minute"` shows `"Hour" => 3` and `"Minute" => 0`.
5. `cairn cron status` shows both jobs `loaded` and `legacy : none`; `echo $?` prints `0`.
6. `cairn cron install` again prints the same `Installed` lines and no `Removed` line; `ls ~/Library/LaunchAgents/dev.cairn.sync.*` lists two files.
7. `launchctl kickstart gui/$(id -u)/dev.cairn.sync.light`, wait one minute, then `grep -A30 "context-sync (light)" "$(cairn config show --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["data_root"])')/logs/context-sync.log" | tail -40` shows `[qmd-update] ok` and no `[qmd-embed]` line.
8. While a light or nightly run is active: `ps -Ao pid,pri,nice,command | grep -E "qmd (update|embed)" | grep -v grep` shows the qmd process with a lower `PRI` than an interactive shell process. **Inferred** expected value; record what it shows.
9. On battery: `launchctl kickstart gui/$(id -u)/dev.cairn.sync.nightly`; the log shows `[qmd-embed] deferred - on battery power`, and `cairn cron status` shows `owed : since ... (battery)`.
10. Plug in AC, `launchctl kickstart gui/$(id -u)/dev.cairn.sync.light`; the log shows `[embed-catchup]`, then `[qmd-verify] ok`; `cairn cron status` shows `owed : none`.
11. `cairn embed --sessions 1` exits 0 on a complete index and prints `[qmd-verify] ok`.
12. `cairn doctor` shows the `sync schedule` ok line and no `SYNC_SCHEDULE_LEGACY`.
13. Duration check: compare the `[qmd-embed] exited 0 (Ns)` values in the log for the week before and the week after. Record the median of each in the PR. **Inferred** risk that background priority slows the embed.
14. Deep recall, not needed: `time cairn recall search "launchd" --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["hybrid"]["status"], len(d["results"]))'` prints `not-needed` and a count of at least 3, in under 2 s.
15. Deep recall, flag: `time cairn recall search "how often should embeddings refresh" --deep -c notes --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["hybrid"]["status"], d["hybrid"]["elapsed_s"], sorted({r["label"] for r in d["results"]}))'` prints `ran`, a time under 30, and a label set that holds `hybrid` or `bm25+hybrid`.
16. Deep recall, rare term: `cairn recall search "zzqqxxnonexistentterm" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["hybrid"]["trigger"])'` prints `few-hits`.
17. Budget: `cairn config set recall.deep.timeout_s 1`, repeat check 15; it prints `timeout`, and `ps -Ao command | grep "qmd query" | grep -v grep` prints nothing. Then `cairn config unset recall.deep.timeout_s`.
18. Skills: in Claude Code, `/cairn:recall --deep <a topic you know>` shows labelled results, the hybrid note when there is one, a Gaps line and a One Thing. `/cairn:search <topic> --deep` shows labelled results and no One Thing.
19. Uninstall: `cairn cron uninstall` prints two `Removed` lines; `ls ~/Library/LaunchAgents/dev.cairn.sync.* 2>/dev/null | wc -l` prints `0`. Re-run `cairn cron install` afterwards.

On Linux (if a contributor has one): steps 1, 2 (expect `2`), 5, 6 with the cron backend, and `crontab -l | grep "cairn sync"` shows `nice -n 10` on both lines.

## 12. Docs and CHANGELOG

### 12.1 README.md

- Line 95: "... **registers the QMD collections, runs the first embed**, and installs an hourly keyword sync and a nightly embed — then verifies a real query returns results."
- Line 179: `/recall authentication work    # topic: BM25 across QMD collections, one hybrid search when BM25 finds too little`, and a new line `/recall --deep that pricing idea  # topic: also run one hybrid (vector) search`.
- Line 192, add a sentence: "`/search QUERY --deep` runs one hybrid (BM25 + vector) search through `cairn recall search`; it takes seconds rather than a fraction of a second."
- "Keeping it fresh", replace the cron bullet (line 460) with two bullets:

  ```markdown
  - **Hourly light sync** (`cairn cron install`) — retries failed work, exports every enabled source and runs `qmd update`, so keyword search is at most an hour behind. It does not embed.
  - **Nightly embed** (`cairn cron install`, 03:00 local by default) — embeds what the day added, for up to three QMD embed sessions while the pending count falls. On a laptop it waits for mains power by default (`cron.embed_on_battery`): a deferred night runs at the next hourly sync on AC power. Both jobs run at background priority: LaunchAgents with `ProcessType Background` and `LowPriorityIO` on macOS (launchd also runs a job missed during sleep on wake), crontab lines under `nice` and `ionice` elsewhere. Set `cron.embed` to `hourly` for the old behaviour, or `off` to embed only by hand.
  ```

- Commands block (lines 500 to 503):

  ```bash
  cairn sync --hook      # fast: claude-sessions only + qmd update
  cairn sync --light     # hourly: all exports + qmd update, no embed
  cairn sync --nightly   # nightly: --light, then embed under the power policy
  cairn sync --cron      # full: all exports + qmd update + one qmd embed
  cairn embed --until-complete   # embed sessions now, while the pending count falls
  cairn cron status      # the installed jobs, the last embed and any owed embed
  ```

- The 30-minute paragraph (lines 517 to 523): replace "so a large backlog takes several runs" with "so a large backlog takes several sessions: the nightly embed runs up to `cron.embed_max_sessions` (default 3) while the pending count falls, and `cairn embed --until-complete` drains the rest at once". Replace the last sentence with "BM25 keyword search works as soon as a session is exported and `qmd update` runs (the SessionEnd hook and the hourly light sync do both); semantic and deep search see it after the next nightly embed."
- New subsection after the recall block, "Deep recall": the trigger rules of 5.3, the vector rules of 5.5, the `recall.deep.*` keys, and the measured cost (BM25 0.12 s; hybrid 8 to 24 s, more under load).
- Line 486: "An existing install keeps the old values until `cairn hook install` and `cairn cron install` run again." stays; add "Upgrading from 2.20 or earlier: run `cairn cron install` once to replace the hourly embedding entry; until then `cairn doctor` warns `SYNC_SCHEDULE_LEGACY`."

### 12.2 docs/

- Add this spec as `docs/nightly-embed-deep-recall-spec.md`, with its status line changed to "implemented in 2.21.0", and a section at the end listing where the implementation differs, as `docs/recall-gaps-spec.md` does.
- Add its row at the top of the table in `docs/README.md` (the test checks the order and the links): `| 2026-11-01 | [Nightly embed and deep recall](nightly-embed-deep-recall-spec.md) | spec | Implemented in 2.21.0: an hourly light sync and a nightly embed at background priority, launchd on macOS, deferred on battery; cairn recall search with one hybrid query on --deep or too few BM25 hits. Rejected: a BM25 score floor by default (scores do not separate good from bad results), rg inside the CLI. |`
- Regenerate `docs/error-codes.md`.

### 12.3 CHANGELOG.md

<!-- cairn-cli: skip -->
```markdown
## [2.21.0]

### Added
- `cairn sync --light` (all exports and `qmd update`, no embed) and `cairn sync --nightly`
  (the same, then up to `cron.embed_max_sessions` QMD embed sessions while the pending
  count falls, under `cron.embed_on_battery`).
- `cairn cron install` installs an hourly light job and a nightly embed job (03:00 local by
  default). On macOS they are LaunchAgents (`dev.cairn.sync.light`,
  `dev.cairn.sync.nightly`) with `ProcessType Background` and `LowPriorityIO`; elsewhere
  they are crontab lines under `nice` and `ionice`. A night deferred on battery runs at the
  next light sync on AC power, at most once a day.
- `cairn cron status [--json]` and `cairn embed [--sessions N | --until-complete]
  [--respect-power]`.
- `cairn recall search QUERY [-q VARIANT]... [-c COLLECTION]... [--deep | --no-deep]
  [--json]`: BM25 for each phrasing, then one hybrid `qmd query` on `--deep` or when BM25
  finds fewer than `recall.deep.min_hits` distinct documents. Results are merged by
  document and labelled `bm25`, `hybrid` or `bm25+hybrid`. The hybrid query has a time
  budget and is skipped, with a one-line note, when the index has no vectors or the last
  embed is older than `recall.deep.max_vector_age_h`.
- Config keys `cron.backend`, `cron.priority`, `cron.embed`, `cron.embed_at`,
  `cron.embed_deadline`, `cron.embed_max_sessions`, `cron.embed_on_battery`,
  `cron.embed_catchup` and `recall.deep.*`.
- The embed stamp `$XDG_STATE_HOME/cairn/qmd-embed.json` and the owed marker
  `embed-owed.json`.
- Error codes `SYNC_SCHEDULE_LEGACY` and `EMBED_OVERDUE`.

### Changed
- The scheduled sync no longer embeds every hour. Run `cairn cron install` once after the
  upgrade: it removes the old hourly `cairn sync --cron` entry. Until then that entry keeps
  embedding every hour and `cairn doctor` warns. Set `cron.embed` to `hourly` to keep the
  old behaviour. If you downgrade later, run `cairn cron uninstall` first.
- `cairn sync` mode flags are mutually exclusive.
- `cairn doctor`: pending documents are an ok line, not a warning, within 30 hours of a
  nightly embed (`--strict-index` still fails). The next step for an incomplete index is
  `cairn embed`.
- Skills: `/recall` runs topic search through `cairn recall search`, runs one deep
  (hybrid) search on "deep recall", "search harder" or `--deep`, or when no BM25 hit is
  about the topic, and labels each hit `bm25`, `hybrid`, `bm25+hybrid` or `rg`. `/search`
  accepts `--deep`. The setup skill drains a first-embed backlog with
  `cairn embed --until-complete`.
```

## 13. Version bump

Minor bump, 2.20.1 to 2.21.0: new commands and config keys, no removal (Semantic Versioning, `CHANGELOG.md:6`). Change the version in all four places: `src/cairn/__init__.py:11`, `plugin.json:4`, `.claude-plugin/plugin.json:3`, `.codex-plugin/plugin.json:3`. The PR title follows the repository's pattern: `Nightly embedding at background priority and deep recall (2.21.0)`. If another PR takes 2.21.0 first, use the next minor number and change the docs and CHANGELOG to match.

## 14. Out of scope

- Remote or agent-box collections, a second data root for sessions from another machine, and quarantine pulls of those transcripts. A separate future PR.
- Moving the rg pass into `cairn recall search`.
- A typed `qmd query` document (`lex:` lines from the variants, a `vec:` line from the query) to skip QMD's own expansion. It could cut hybrid latency; it needs its own measurement.
- Keeping QMD's models warm with `qmd mcp --http --daemon` for faster hybrid queries.
- A vector-state gap kind in `cairn recall gaps`.
- A recall qrels family for hybrid results (`tests/test_recall_qrels.py`).
- Stopping embeddings when nobody reads vectors, or measuring how often deep recall runs.
- Priority or power policy for the SessionEnd hook worker and `cairn invoke` publishing.
- Proof that background QoS throttles the GPU. The manual checks record durations only.
- Windows.
- `cairn routine` changes. A routine whose `at` time falls inside the embed window keeps running; it exports without QMD (`src/cairn/recall.py:23-49`).

## 15. PR checklist

- [ ] `src/cairn/power.py`, `src/cairn/schedule.py` and `src/cairn/recall_search.py` added; `sync.py`, `admin.py`, `config.py`, `qmd.py`, `recall.py`, `errors.py` and `__main__.py` changed as specified.
- [ ] `cairn sync --light`, `--nightly`, `cairn embed`, `cairn cron status` and `cairn recall search` parse, and `cairn help` lists them.
- [ ] Both config templates updated and equal.
- [ ] Skill texts of section 8 applied; routing rows added and the count changed to 17; `uv run python -m unittest tests.test_skill_routing -v` passes.
- [ ] `/skill-review skills/recall`, `/skill-review skills/search`, `/skill-review skills/setup` and `/skill-review skills/cairn` run; VERDICT lines pasted in the PR body (CONTRIBUTING.md).
- [ ] README, docs spec, `docs/README.md` row and regenerated `docs/error-codes.md` in the PR.
- [ ] CHANGELOG `[2.21.0]` entry; version bumped in the four files.
- [ ] `make check > /tmp/cairn-check.txt 2>&1; echo "exit=$?"` prints `exit=0`; the whole file read.
- [ ] A `make discriminates TEST=...` line for each new test in the PR body, or the mutation probe for an exit 3.
- [ ] Manual checks 1 to 19 of 11.2 run on macOS; results and the two embed medians in the PR body.
- [ ] The diff read once more for home paths, emails, machine names and private project names; `tests/test_public_hygiene.py` passes locally with the private-terms file.
- [ ] No attribution trailer in commits or the PR body.

## 16. Open questions

1. `recall.deep.rerank` defaults to `false` because one run without rerank took 12 s against 24 s and 79 s with it. Keep that default, or accept the slower, reranked query?
2. A nightly job that launchd fires late after wake runs its first session whatever the time (it may run at 09:00 on AC power). Keep that, or skip the night when the wake is after `embed_deadline`?
3. The auto trigger counts distinct BM25 documents. Measured BM25 output almost always holds 3 or more hits for a multi-word query, so in practice deep mode runs on request or on the agent's "none of these is relevant" judgement. Is that enough, or is a different signal wanted?
4. The catch-up embed after a battery-deferred night runs during the day, at most once a day, on AC power. Keep it on by default?
5. Stale vectors (last embed older than 72 h) skip the hybrid query, as briefed. Running it anyway with a note would still find older documents. Keep the skip?

## 17. Implementation notes

Where the code (2.21.0) differs from the sections above, and the qmd facts that decided
it. The qmd facts come from the qmd 2.5.3 source and docs (github.com/tobi/qmd) and from
`qmd --help` on the test machine.

- **BM25 runs one call per phrasing per collection, not one call with every `-c`.** In
  qmd 2.5.3 a repeated `-c` searches the whole index and filters afterwards, so a call can
  return fewer than `-n` hits or none at all (fixed upstream in 2.8.3). `bm25.calls` in the
  JSON is therefore `phrasings × max(1, collections)`. The hybrid call still passes every
  `-c` once: one `qmd query` per collection would multiply the slow call.
- **No `--no-rerank` retry (5.8).** qmd's argument parser runs with `strict: false`, so an
  unknown flag is ignored rather than refused. The retry path would never run. An older
  qmd that ignores `--format json` prints text, which the `unsupported` status covers.
- **`cron` and `all` modes run through the embed loop too** (`max_sessions` 1, no power
  policy), so every embed writes the stamp of 4.6, and `_index_stages` no longer embeds.
  The index lock is released between `qmd update` and the embed session. The `cron`
  behaviour is otherwise unchanged: the log gains one `[qmd-embed] session 1/1` line.
- **The last session's embed result is recorded as well as its verify label.** `run_embed`
  returns false when QMD reports chunk failures or exits non-zero, and that step failure
  is recorded for the last session whatever the outcome (today's `cron` behaviour). A
  `complete` outcome can therefore still end `=== FAILED ===` with `failed steps:
  qmd-embed` when the last session reported chunk failures; the stamp records what
  `qmd status` said.
- **The embed stamp and owed marker carry a `mode` and come from one module.**
  `record_embed(outcome, mode, sessions, status)`, `read_embed_stamp()`, `write_owed()`,
  `mark_catchup()`, `read_owed()` and `clear_owed()` live in `src/cairn/qmd.py`. The
  embed loop takes a `mode` keyword (`nightly`, `cron`, `all`, `light` for a catch-up,
  `embed` for the command) for the stamp. The catch-up marks `catchup_on` before its
  session, as specified, and the marker keeps its first `since` across rewrites.
- **`embed_loop` records `qmd-embed` for the last session and `qmd-verify (stalled)` or
  the verify label**, so a run that stalls fails with `failed steps: qmd-verify (stalled)`.
- **The scheduled jobs use `routine.environment_block`, not `background_environment`.**
  It adds `XDG_STATE_HOME` when the installing shell sets it, so the job writes the embed
  stamp where `cairn doctor`, `cairn cron status` and `cairn recall search` read it.
- **Unknown keys under `cron` and `recall` are `CONFIG_UNKNOWN_KEY`**, as every other
  parser site does; `cron.schedule` must have five fields. Before 2.21 unknown `cron`
  keys were ignored. The dataclasses are `SyncSchedule` (field `schedule`) and
  `RecallPolicy` with a nested `DeepRecall` (field `recall`).
- **`cairn cron` catches `ConfigError` and exits 2 with the code**, including a `crontab
  -l` failure, which was an uncaught traceback before. Doctor's `sync schedule` group runs
  only when the config loaded.
- **`EMBED_OVERDUE` fires with no stamp at all**, so a fresh install warns until its first
  nightly embed (or `cairn embed`) writes one. Doctor's pending-is-ok line needs a stamp
  whose `last_embed_at` is within 30 hours.
- **Text output of `cron status`** names a job `installed, not loaded` or `installed,
  launchd did not answer` as well as `loaded` and `not installed`, and marks a job that
  differs from the config. The `legacy` line lists each extra crontab line or LaunchAgent.
- **`cairn recall search` sets `NO_COLOR=1`** for its qmd calls and runs with the user's
  working directory. qmd walks up from the working directory for a `.qmd/index.yaml`
  when `INDEX_PATH` is unset, as the skills' own `qmd search` calls do today.
- **The stamp tests live in `tests/test_sync_embed_policy.py`**, not
  `tests/test_qmd_health.py`, which has no state-home fixture. `tests/test_shared_store.py`
  now replaces `cairn.routine.launchctl` with a failing fake for every `StoreFixture`
  test, after an unpatched install test loaded two LaunchAgents into the developer's
  launchd during development.
- **Deep-search decision rules in the recall skill** (8.1) are the rule set in Step 3,
  "When to add `--deep`": on the first search only on a Deep classification or after an
  earlier empty recall in the conversation; once after the first pass when the automatic
  hybrid did not run and no hit is about the topic; never for literals, temporal queries
  or scope problems; one `--deep` per recall. "A concept or question" is not a trigger on
  its own, because it would send most recalls to the slow path.
- **After the Codex review (gpt-6-astra, high), these points changed from the sections
  above.** The embed stamp's `last_embed_at` moves only when QMD ran a session to a
  normal exit (`record_embed(..., exited=False)` copies it forward), so a job that times
  out every night cannot make vectors look fresh. `clear_owed()` deletes the marker only
  when it belongs to the current database. A light run skips the catch-up embed, and a
  follow-up skips a coalesced embed, when this run's `qmd update` failed. A nightly run
  that deferred or skipped its embed still runs a coalesced `cron` or `all` request's
  embed (those modes have no power policy). The power and deadline checks run after the
  index lock is taken, so a wait behind another writer cannot cross them unseen.
  `cron status` exits 0 only when every configured job is installed, matches the config
  and is loaded; doctor warns `the <job> job is installed but not loaded`. A `launchctl
  bootout` that leaves a job loaded keeps its plist and fails install or uninstall with
  exit 1 instead of reporting a removal. `cairn cron uninstall` needs no valid config.
  The crontab mode detector reads the command's tokens, not substrings. `recall search`
  turns a failed `qmd collection list` into `RECALL_FAILED` and tolerates a hybrid hit
  without a score. `config set` into a `null` `cron` or `recall` block works. Dismissed:
  the stamp's database identity follows `qmd_resources()`, the same identity the index
  stamp and `cairn recall gaps` already use; qmd's project-local `.qmd/index.yaml`
  discovery is outside every cairn path that pins `INDEX_PATH`.
- **Measured on the implementer's machine (2026-11-02, qmd 2.5.3, Node 24):** see the PR
  body for the manual checks of 11.2.
