# Sync robustness and test hygiene: implementation plan

Status: proposed; nothing in this document is implemented yet.

Observation date: 2026-10-17, on the first day of running cairn 2.4.0 (merge commit
`1443f89`) on a real machine with Claude Code 2.1.302, Codex 0.160.0 and QMD 2.5.3.

Target: one PR into `main`, released as **2.4.1**. Four code changes, one CI guard, a
changelog entry and the version bump. No behaviour change for recall, export, hooks or
the store format.

## 1. What went wrong today

| # | Observation | Evidence |
| --- | --- | --- |
| A | Two `cairn sync --cron` runs executed at once. The second waited on `locked_index()`, then repeated `qmd update` and `qmd embed`. Their log blocks interleaved: the 17:07 header sits inside the 17:04 run, and the 17:04 run has no closing marker. | `~/Code/context/.context/logs/context-sync.log` lines 96672 to 96752 |
| B | A step timeout would not have stopped the embed. The `qmd` entry point is a Node wrapper that spawns `qmd.js` as a grandchild (pids 66694 and 66698). `subprocess.run(timeout=...)` kills only the direct child. | `src/cairn/sync.py:101`, process tree captured during the run |
| C | The test suite writes into the real home on every run: +5 files in `~/.cache/cairn/catalogs`, +25 in `~/.local/state/cairn/locks`. The real directories held 287 catalogs and 1938 lock files after a handful of runs. | Measured per module, see section 5 |
| D | One permanently failing source (Granola, no Keychain token) marks every run `=== FAILED ===` with exit 1, so a new failure in another step is invisible in the summary line. | 1365 Granola failures in the log; every run block ends in FAILED |

Not cairn defects, listed so nobody re-investigates them: the Metal "error compiling
source" line is the llama.cpp M5 tensor-API probe (fixed upstream in b10734, not yet in any
QMD release) and Metal still accelerates the embed; QMD 2.5.3 re-fingerprints vectors and
re-embeds once after upgrade; the Granola token failure is a Keychain access problem.

## 2. Change 1: one sync at a time

**File:** `src/cairn/sync.py`, `main()`.

Take a non-blocking per-store lock before anything is logged:

```python
config = get_config()
sync_lock = lock_path(config.data_root, "sync")
try:
    with locked(sync_lock, blocking=False):
        return _run(config, mode)
except BlockingIOError:
    log(f"=== context-sync ({mode}) {datetime.now().isoformat()} skipped: another sync holds {sync_lock.name} ===")
    return 0
```

Rules:

- Move `rotate_log()` and the header line inside the lock. Rotation while another
  process appends can drop lines.
- The skip is a single `log()` call so it cannot interleave with a running block, and
  it exits 0. A skipped cron run is not a failure; the running sync covers the work.
- All three modes (`--hook`, `--cron`, `--all`) take the lock. Legacy `sync --hook`
  callers lose nothing: the running sync exports the same sources.
- Keep `locked_index()` around the QMD steps. Detached `cairn hook worker` processes do
  not take the sync lock, so the index lock still protects against them.
- Lock order becomes sync > worker > operation > qmd. Only sync processes take the new
  lock, and they take it first, so no cycle is introduced.
- `lock_path` already keys on `resource.resolve()`, so `data_root` aliases contend.

**Tests** (new `tests/test_sync.py`, fixture `StoreFixture`):

1. Hold `locked(lock_path(cfg.data_root, "sync"))` in the test process, run
   `python -m cairn sync --cron` with `self.env`. Expect exit 0, one new log line
   containing `skipped`, and no `[claude-code-sessions]` step line.
2. Without contention, with a fake `qmd` on `qmd_binary` (move `fake_qmd()` from
   `tests/test_hooks.py:TestHooks` into `StoreFixture`), expect the header, the step
   lines and `=== done ===` in that order and exit 0.

## 3. Change 2: kill the whole process group on timeout

**Files:** new helper in `src/cairn/procs.py` (stdlib only), used by `sync.run_step`
(`src/cairn/sync.py:97`), the two `COLLECTION_TIMEOUT` calls in `src/cairn/qmd.py:137`
and `src/cairn/qmd.py:187`, and both timed calls in the hook worker: the per-source
export at `src/cairn/hooks.py:140` and `qmd update` at `src/cairn/hooks.py:156`, each
with `timeout=120` today. The export call also spawns `python -m cairn`, whose own
`qmd` children would otherwise survive a timeout.

```python
def run_capped(command, *, timeout) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, stdout=PIPE, stderr=PIPE, text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(command, process.returncode, out, err)
```

`run_step` keeps its logging and its `TimeoutExpired` branch; it only swaps the call.
The partial output logged on timeout comes from the exception as before.

**Test** (in `tests/test_sync.py`): run `run_step("probe", [sys.executable, "-c", SCRIPT],
timeout=1)` where SCRIPT prints the pid of a grandchild it spawns with
`subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])`, flushes, then
sleeps. Parse the pid from the partial output logged to the sync log, and assert
`os.kill(pid, 0)` raises `ProcessLookupError` within 10 seconds. Assert `run_step`
returned `False` and the log holds `[probe] TIMEOUT after 1s`.

## 4. Change 3: clearer run summary

**File:** `src/cairn/sync.py`.

Collect the names of failed steps instead of a bare boolean. Final line:

- all steps passed: `=== done ===`
- some failed: `=== FAILED: granola-sessions ===` (names comma separated, in run order)

Exit code stays 1 whenever any step failed, so cron mail and `cairn doctor` behave as
today. The only change is the summary line, which now says what failed without
scrolling. Update the two tests from change 1 to assert the new form.

## 5. Change 4: tests must not touch the real home

Measured with `uv run python -m unittest tests.<module>` per module, counting files in
`~/.cache/cairn/catalogs` and `~/.local/state/cairn/locks` before and after:

| Module and test | Leak per run | Cause | Fix |
| --- | --- | --- | --- |
| `tests/test_recall.py`, `_RecallFixture.setUp` | +5 catalogs, +15 locks | Sets only `CAIRN_CONFIG`. `Path.home()` and `XDG_CACHE_HOME` resolve to the real home for both the in-process calls and the `claude export` subprocess. | Build the environment the way `StoreFixture` does (`tests/test_shared_store.py:49` to `:62`): `HOME`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` under the temp root, applied with `patch.dict(os.environ, ...)` and passed as `env=` to the subprocess. Simplest: subclass `StoreFixture` and drop the hand-rolled teardown. |
| `tests/test_hooks.py`, `TestHooks.test_cron_command_retains_config_and_index_environment` | +10 locks | Runs the generated cron command with `env={"PATH": ..., "PYTHONPATH": ...}`. Without `HOME`, `Path.home()` falls back to the password database, which is the real home. | Add `"HOME": str(self.root / "home")` to that `env`. cron always sets `HOME`, so the test stays faithful to production. |

Every other module measured zero.

**CI guard** (`.github/workflows/ci.yml`, after the `unittest discover` step):

```yaml
- name: tests left nothing in the runner home
  run: |
    for d in ~/.local/state/cairn/locks ~/.cache/cairn/catalogs; do
      if [ -e "$d" ] && [ -n "$(ls -A "$d")" ]; then echo "::error::$d has files after tests"; ls -A "$d"; exit 1; fi
    done
```

The runner home is empty before the suite, so any file there afterwards is a leak.
This catches regressions that a per-test assertion cannot, because leaks come from
subprocesses the tests spawn.

## 6. Out of scope for this PR

- A `qmd_env` config key to pass extra environment such as
  `GGML_METAL_TENSOR_DISABLE=1` into hook and cron runs. Not needed for GPU use.
- The Codex double `session_meta` subagent export bug. Separate PR already planned.
- Pinning `HOME` in `runtime.background_environment`. Hosts and cron set it; the lock
  directory is deliberately `HOME`-based (`src/cairn/storage.py:18`), so leave it.
- Granola Keychain access, QMD version drift and the Metal probe line.

## 7. Release chores

1. `src/cairn/__init__.py`: `__version__ = "2.4.1"`. The `version-bump` CI job
   requires a strict increase because `src/cairn/**` changes.
2. Three manifests carry the same string and must read `2.4.1`: `plugin.json`
   (authoritative portable manifest), `.claude-plugin/plugin.json`, and
   `.codex-plugin/plugin.json`. Edit the first two by hand, then regenerate the third
   with `uv run python scripts/sync_plugin_manifest.py`, which projects `plugin.json`
   into the legacy Codex layout. `tests/test_plugin_manifest.py` fails the build if a
   manifest version differs from `cairn.__version__`.
3. `CHANGELOG.md`, new `## [2.4.1]` above `## [2.4.0]`:
   - Fixed: concurrent `cairn sync` runs are serialised per store; a second run logs
     one `skipped` line and exits 0 instead of repeating the QMD steps.
   - Fixed: a timed-out step kills its whole process group, so a `qmd` wrapper's child
     cannot keep running outside the index lock.
   - Changed: the sync summary line names the failed steps.
   - Internal: the test suite no longer writes catalogs or lock files into the real home;
     CI fails if it does.

## 8. Verification before merge

Automated: `uv run ruff check src tests`, `uv run ruff format --check src tests`,
`uv run mypy`, `uv run python -m unittest discover -s tests`, then the leak counts above
measured locally must be +0 for every module.

Manual, on the real machine after `uv tool upgrade cairn`:

1. Start `cairn sync --cron` in one terminal and again in a second within a few seconds.
   The second exits 0 at once and the log gains one `skipped` line. The first run's
   block is contiguous from header to summary.
2. `crontab -l` is unchanged; the hourly entry needs no reinstall for this release.
3. End one Claude Code session and one Codex session during a running sync.
   `cairn hook status` shows both requests completed; the detached workers are not
   blocked by the sync lock.
