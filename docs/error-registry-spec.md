# Error registry: stable codes for cairn failures

Status: implemented in this PR (branch `shav/mem-10-error-registry`). Section 12 lists
where the implementation departs from the text below; where they differ, section 12 wins.
The registry as shipped is `src/cairn/errors.py`, rendered as `docs/error-codes.md`.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0.

**Dependencies.** None functionally. The layer sits on 01-09 and reserves codes for the
new exits they add (04 routine status 0/1/2, 06 verify, 08 forget refusals, plus 02, 03,
05 and 09). It assumes those layers kept their exit codes and message text as their specs
state, and converts none of their sites. Layer 12 (docs-cli-truth) later parses every
`cairn ...` line in `docs/error-codes.md`, so every `verify` argv here must be a real
parser path.

## 1. Purpose and non-goals

cairn fails with exact messages and documented exit codes, but the meaning of a code and
the next step live in prose spread over `docs/routine-spec.md`, `docs/recall-digest-spec.md`
and the skills. An agent that reads `exit 2` from `cairn routine run` cannot tell an unknown
template variable from a nested run without parsing English. This layer gives every
common failure a stable upper-snake `code`, one registry row with the next step and a
verify command, an offline lookup (`cairn errors CODE`), a generated reference
page, and `code` + `fix` in machine output.

Non-goals:

- No exit code changes. A code names a failure; the exit stays what it is today. cairn
  does not adopt a global "exit 3 = stop and ask" rule: exit 3 already means "empty day"
  for `recall digest` (`src/cairn/digest.py:418`) and the routine `prepare` contract
  (`src/cairn/routine_plan.py:27`), and "stalled" for `agents wait`
  (`src/cairn/agents.py:42`).
- No message rewording. The first stderr line of every converted site stays byte-identical.
- No full sweep. About 60 `raise ConfigError` sites and the per-source `return 1/2` sites
  in `src/cairn/sources/*.py` keep their current output; they follow in later PRs.
- No actor/consent vocabulary, no reason sub-codes, no docs anchors per code. The row is
  seven fields.

## 2. Current behaviour (main at 2.9.0)

Exception types and catch-all handlers:

| Where | What happens today |
|---|---|
| `src/cairn/config.py:70-71` | `ConfigError(RuntimeError)`, no code; about 57 raise sites in the module. |
| `src/cairn/config.py:363-366` | `No cairn config found (looked at $CAIRN_CONFIG, ..., and the repo root). Run 'cairn config init' ...` |
| `src/cairn/config.py:901-902` | `Failed to read config at PATH: ...` for `OSError`/`JSONDecodeError`. |
| `src/cairn/config.py:378-380, 417-419, 448-450, 502-504, 585-586, 727-729` | `... has unknown keys: a, b. Allowed keys: ...` (collections, billing, peer, profile, invoke peers, routine). |
| `src/cairn/__main__.py:128-137` | Unknown command: `cairn: unknown command 'x'.` plus the known list, exit 2. |
| `src/cairn/recall.py:193-195` | One handler for `ValueError`, `ConfigError`, `OSError`, `TimeoutExpired`: `Error: {exc}`, exit 1. Covers `digest needs a one-day window` (`src/cairn/digest.py:84-89`) and unsafe `--out` (`src/cairn/digest.py:100-103`). |
| `src/cairn/digest.py:407-418` | `--json` prints `{"sessions": [...], "skipped": [...]}`; exit 3 when no session was written. |
| `src/cairn/routine_plan.py:33-34` | `RoutineError`: "the run exits 2 and writes no stamp". |
| `src/cairn/routine_plan.py:113-118` | `routine NAME: unknown template variable {{X}} in WHERE; known: ...` |
| `src/cairn/routine_plan.py:164-168, 177-181, 191-192` | prompt_file not found, not readable, renders empty; `cd` not a directory. |
| `src/cairn/routine.py:71-80` | Unknown routine (`is not in routines; known: ...`), invalid name. |
| `src/cairn/routine.py:142-150, 184-186, 201-203` | Non-macOS install/uninstall/kickstart: exit 2. |
| `src/cairn/routine.py:173-177, 206-217` | `launchctl` failures and "not loaded": exit 1. |
| `src/cairn/routine.py:584-586` | `cairn routine: {exc}` for `RoutineError`/`ConfigError`, exit 2. |
| `src/cairn/routine_run.py:545-557` | Day not ended in UTC; nested run refused (`CAIRN_INVOKE_DEPTH`). |
| `src/cairn/routine_run.py:510-524` | Exit 1 when a day failed or was deferred, 2 on `RoutineError`; `finish` writes `last-run.json` with `exit`, `error`, `failures` (`src/cairn/routine_run.py:458-477`). Failure reasons: `render-error`, `budget-exhausted`, `prepare-exit-N`, `invoke-exit-N`, `validate-exit-N`, `incomplete-input` (`src/cairn/routine_run.py:351-439, 510`). |
| `src/cairn/invoke.py:104-123` | `EXIT_OK 0`, `EXIT_FATAL 1`, `EXIT_USAGE 2`, `EXIT_DENIED 2`, `EXIT_BUSY 75`, `EXIT_TIMEOUT 124`, `EXIT_IDLE 125`, `EXIT_CANCELLED 130`, `EXIT_BY_OUTCOME`. |
| `src/cairn/invoke.py:163-164` | `UsageError`: refused before spawning, exit 2. Raised for nested runs (`:1500-1505`), self-check (`:1548-1550`), billing (`:1553-1562`), permissions (`:1574-1578`), bad flags (`:1283-1522`). |
| `src/cairn/invoke.py:1203-1246` | `round_result`: the one-line JSON result with `status`, `outcome`, `exit_code`, `error`, `retryable`, `warnings`. |
| `src/cairn/invoke.py:2100-2135` | Outcome to `error` text and `retryable` (timeouts and denied retryable, cancelled not, failed by `_retryable(text)`); `KEYCHAIN_GUIDANCE` (`src/cairn/harnesses/claude.py:65-68`). |
| `src/cairn/invoke.py:1596-1600, 2582-2586` | Busy: `result("busy")` exit 75; `invoke publish --json` prints `{..., "error": "thread is busy ..."}`. |
| `src/cairn/invoke.py:2784-2792` | `UsageError` -> `Error: ...` exit 2; `LookupError` and the rest -> exit 1. Text only, even with `--json`. |
| `src/cairn/admin.py:138-145, 202-209` | `config path` / `config show`: `No config loaded: ...` plus `It would live at: ...`, exit 1. `config show --json` prints nothing on stdout on failure. |
| `src/cairn/admin.py:584-596` | `config set/unset`: invalid JSON, or `Rejected: that change made the config invalid (...). Restored.`, exit 1. |
| `src/cairn/admin.py:845-855` | Doctor prints `  ok`, `  warn`, `  FAIL` lines; no `--json`. |
| `src/cairn/admin.py:889-891, 1127, 1139, 905-923, 1238, 1264, 1272-1274` | `FAIL qmd runtime`, `FAIL config`, `FAIL qmd - 'X' not found`, embeddings warn/fail (fail with `--strict-index`), `warn hook trust` (text from `src/cairn/hooks.py:316`), `warn cron`; exit 1 on any critical failure. |
| `src/cairn/agents.py:550-568, 611-619` | `agents wait`: exit 3 stalled, 124 timeout, 1 not succeeded, 2 usage. |
| `src/cairn/sync.py:425-428` | Exit 1 when any step failed; detail only in the log. |
| `src/cairn/sources/base.py:385-394, 483, 509, 514` | `No session_id`, exit 1; nothing to log / bad status, exit 2. |
| `src/cairn/mutations.py:85` | `Session file not found for XXXXXXXX`. |
| `src/cairn/sources/granola.py:233-236` | `GranolaAuthError`: token expired or invalid. |

Exit-code documentation lives in prose: `docs/recall-digest-spec.md:74-81`,
`docs/routine-spec.md:388-405, 806-919`, `skills/recall/SKILL.md:76`,
`skills/invoke-claude/SKILL.md:109-117`, `skills/invoke-antigravity/SKILL.md:99-110, 195`.
Tests pin messages by substring (`tests/test_doctor_index.py:61-64` asserts
`FAIL qmd - '/missing/qmd' not found`), and no test pins a converted site's whole stderr.

## 3. Surface

**Command.** `cairn errors [CODE] [--json]`, added to `_ADMIN` in `src/cairn/__main__.py:24-35`
as `"errors": "cairn.errors:main"` and to `_HELP`.

| Call | Output | Exit |
|---|---|---|
| `cairn errors` | One line per code: `CODE  exit N  meaning`, sorted; then `Reserved:` lines | 0 |
| `cairn errors --json` | `{"codes": [entry, ...], "reserved": {CODE: note}}` | 0 |
| `cairn errors CODE` | The row as labelled lines (Meaning, Why, Next step, Verify, Exit, Retryable) | 0 |
| `cairn errors CODE --json` | One entry object | 0 |
| `cairn errors RESERVED_CODE` | `CODE is reserved for <note>; nothing emits it yet` | 0 |
| `cairn errors NOPE` | stderr `cairn errors: unknown code 'NOPE'; run 'cairn errors' for the list` | 2 |

`CODE` is matched after `.upper()`. The command is offline: it imports only `errors.py`
and never loads config.

**Generator.** `python -m cairn.errors --render` prints `docs/error-codes.md` to stdout.
No config key, no frontmatter key, no new state file.

**Human output of a converted site.** The first stderr line is unchanged. One line follows:

```
code: CONFIG_UNKNOWN_KEY (run 'cairn errors CONFIG_UNKNOWN_KEY')
```

Doctor lines gain a suffix instead: `  FAIL qmd - '/missing/qmd' not found - install it (...) [QMD_MISSING]`.

## 4. Behaviour

1. `src/cairn/errors.py` defines a frozen dataclass `ErrorCode(code, meaning, why,
   next_step, verify, exit, retryable, exit_by_command)`. `verify` is a tuple of argv
   strings or `None`. `exit_by_command` is a small mapping for a code whose exit differs by
   command (config codes exit 2 from `cairn routine`, 1 elsewhere). `REGISTRY` is a
   `MappingProxyType` keyed by code; `RESERVED` maps a reserved code to a one-line note.
2. `verify` may hold placeholders from a closed set: `{name}` (routine), `{thread_id}`,
   `{peer}`. A site passes the values it knows. If a placeholder stays unfilled, the
   machine `fix` omits `verify`; it never emits a template.
3. Raise sites carry a code. `ConfigError.__init__(self, message, code="CONFIG_INVALID")`;
   `RoutineError` and invoke's `UsageError` gain the same keyword (defaults
   `ROUTINE_USAGE`, `INVOKE_USAGE`). Only the raises that need a specific code change:
   `config.py:363` (`CONFIG_NOT_FOUND`), the six unknown-key raises (`CONFIG_UNKNOWN_KEY`),
   `routine_plan.py:115` (`ROUTINE_TEMPLATE_UNKNOWN_VAR`), `routine_plan.py:166, 168, 181,
   192` (`ROUTINE_INPUT_INVALID`), `routine.py:74` (`ROUTINE_UNKNOWN`),
   `routine_run.py:553` (`ROUTINE_NESTED`), `invoke.py:1502` (`INVOKE_NESTED`),
   `invoke.py:1556, 1562` (`INVOKE_BILLING_CHECK_FAILED`).
4. Helpers, each so a site changes by one line:
   - `code_of(exc, default) -> str`: `getattr(exc, "code", None) or default`.
   - `fix(code, **fill) -> dict`: `{"next_step": ..., "verify": [...]}`.
   - `tag(payload, code, **fill) -> dict`: returns `{**payload, "code": code, "fix": fix(...)}`;
     keys already in `payload` (`error`, `retryable`) win.
   - `fail(code, message, *, prefix="", as_json=False, command=None, **fill) -> int`:
     prints `prefix + message` on stderr, then the `code:` line; with `as_json` prints the
     failure object (section 5) on stdout; returns the exit from the row
     (`exit_by_command[command]` first).
   - `outcome_code(status) -> str | None`: `busy`, `denied`, `timeout`, `idle_timeout`,
     `cancelled`, `failed` to their codes; `succeeded` and unknown values to `None`.
   - `finding(code) -> str`: the ` [CODE]` suffix doctor appends.
   `fail` and `tag` raise `KeyError` for an unknown or reserved code, so a typo fails a test.
5. Sites converted in this layer (top 10):

| # | Site | Change |
|---|---|---|
| 1 | `src/cairn/__main__.py:130-137` | `sys.exit(errors.fail("CLI_UNKNOWN_COMMAND", text))` |
| 2 | `src/cairn/recall.py:193-195` | `return errors.fail(errors.code_of(exc, "RECALL_FAILED"), str(exc), prefix="Error: ", as_json=args.json)` |
| 3 | `src/cairn/digest.py:407-418` | Empty day: `tag` the `--json` payload with `DIGEST_EMPTY_DAY`; human mode adds the `code:` line; exit stays 3 |
| 4 | `src/cairn/routine.py:584-586` | `return errors.fail(code_of(exc, "ROUTINE_USAGE"), str(exc), prefix="cairn routine: ", command="routine", name=args.name)` |
| 5 | `src/cairn/routine_run.py:520-524` and `finish` (`:458-477`) | Stderr via `fail`; `last-run.json` gains `code` (`ROUTINE_DAY_FAILED` on exit 1, the error's code on exit 2, `null` on 0) |
| 6 | `src/cairn/invoke.py:1203-1246` `round_result` | `if (c := outcome_code(result["status"])): result = tag(result, c, thread_id=..., peer=peer)`; covers the runner, `result-NN.json`, `invoke wait` and busy |
| 7 | `src/cairn/invoke.py:2784-2786` | `return errors.fail(code_of(exc, "INVOKE_USAGE"), str(exc), prefix="Error: ", as_json=getattr(args, "json", False), peer=args.command)` |
| 8 | `src/cairn/invoke.py:2582-2586` `cmd_publish` busy | `tag({**report, "error": message}, "INVOKE_BUSY", thread_id=thread_id)` |
| 9 | `src/cairn/admin.py:142-145, 206-209` | `fail(code_of(e, "CONFIG_INVALID"), ..., as_json=as_json)` for `config path` / `config show`; the `It would live at:` line stays |
| 10 | `src/cairn/admin.py` doctor | `_fail`/`_warn` take `code=None` and append `finding(code)`: `:890` `QMD_RUNTIME_FAILED`, `:1127` the config error's code, `:1139` `QMD_MISSING`, `:905-923` `QMD_INDEX_INCOMPLETE`, `:1238` `HOOK_NOT_TRUSTED` |

Later PRs, inventoried but not converted: `config set/unset` (`admin.py:575-596`), `agents
wait` (`agents.py:550-619`), `sync` (`sync.py:425-428`), source `note/close/log`
(`sources/base.py:385-537`, `mutations.py:75-216`), Granola auth, `invoke tail`'s
`{"kind": "result"}` line (`invoke.py:2456`), invoke `LookupError`, routine launchd sites.

6. `skills/cairn/SKILL.md` gains a short "On failure" section with the contract line:
   "On a failure, read `code`, run `cairn errors CODE`, run its verify command, then act."
   It adds that `fix.verify` edits no config or instruction file and never overwrites a
   finished round's outcome (it can create `data_root`, refresh the catalog cache, or
   finalise and publish an unfinished invoke round whose runner and peer have both exited,
   as every invoke reader does) and that an exit code alone is ambiguous
   (2 is usage and denied, 3 is empty day and stalled).

## 5. Data formats

Registry entry (`cairn errors CODE --json`):

```json
{"code": "ROUTINE_TEMPLATE_UNKNOWN_VAR",
 "meaning": "A {{NAME}} token in the prompt file, prepare, validate or profile extra_args is not a known template variable.",
 "why": "Rendering happens before any spend; an unknown token is a prompt or config defect, so no stamp is written.",
 "next_step": "Fix the token the message names, or add an outputs key that defines it, then dry-run.",
 "verify": ["cairn", "routine", "run", "{name}", "--dry-run"],
 "exit": 2, "exit_by_command": {}, "retryable": false}
```

Failure object on stdout (`fail(..., as_json=True)`):

```json
{"ok": false, "code": "CONFIG_UNKNOWN_KEY", "error": "invoke.codex has unknown keys: efort. Allowed keys: ...",
 "exit": 1, "retryable": false,
 "fix": {"next_step": "Remove or rename the key; 'cairn config show --json' lists the valid shape.",
         "verify": ["cairn", "config", "show", "--json"]}}
```

`error` holds the message without the human prefix. Invoke result line: the existing keys
plus `"code": "INVOKE_DENIED", "fix": {...}`; `retryable` keeps the per-round value from
`invoke.py:2122-2132`. `last-run.json`: existing keys plus `"code"`.

## 6. Proposed registry (25 active entries)

| Code | Exit | Retry | Meaning | Next step | Verify | Raised today |
|---|---|---|---|---|---|---|
| `CLI_UNKNOWN_COMMAND` | 2 | no | First argument is not a cairn command | Use a command from the list printed | `cairn help` | `__main__.py:130-137` |
| `CONFIG_NOT_FOUND` | 1 (routine 2) | no | No config at `$CAIRN_CONFIG`, the user path or the repo root | Run `cairn config init` | `cairn config path` | `config.py:363-366` |
| `CONFIG_INVALID` | 1 (routine 2) | no | Config is not valid JSON or a value fails its check | Fix the field the message names | `cairn config show --json` | `config.py:901-902` and other raises |
| `CONFIG_UNKNOWN_KEY` | 1 (routine 2) | no | A config object has a key cairn does not know | Remove or rename the key | `cairn config show --json` | `config.py:378, 417, 448, 502, 585, 727` |
| `QMD_MISSING` | 1 | no | The configured `qmd_binary` is not on PATH or a file | Install QMD or fix `qmd_binary` | `cairn doctor` | `admin.py:1139` |
| `QMD_RUNTIME_FAILED` | 1 | no | `qmd status` failed, so QMD cannot open its index | Read the QMD error; check the QMD install | `cairn doctor` | `admin.py:889-891` |
| `QMD_INDEX_INCOMPLETE` | 0 (`--strict-index` 1) | yes | Index empty, without vectors, pending, or unreadable | Run `cairn sync --cron`; investigate if pending does not fall | `cairn doctor --strict-index` | `admin.py:905-923` |
| `HOOK_NOT_TRUSTED` | 0 | no | Hook files exist but the host has not confirmed trust | Ask the user to review and trust cairn's hook in the host | `cairn hook status` | `admin.py:1238`, `hooks.py:316` |
| `RECALL_FAILED` | 1 | no | recall could not answer: bad argument, unreadable file, refresh timeout | Act on the argument or file the message names | `cairn recall list today` | `recall.py:193-195`, `digest.py:84-103` |
| `DIGEST_EMPTY_DAY` | 3 | no | No session was written for the day | Nothing to digest; stop the day as empty | `cairn recall list yesterday --all-projects` | `digest.py:418` |
| `ROUTINE_UNKNOWN` | 2 | no | Name is not in `routines` | Use a listed name or add the routine | `cairn routine list` | `routine.py:71-74` |
| `ROUTINE_USAGE` | 2 | no | Bad name, future `--day`, or launchd-only action off macOS | Correct the argument the message names | `cairn routine list` | `routine.py:78-80, 142-203`, `routine_run.py:545-546` |
| `ROUTINE_TEMPLATE_UNKNOWN_VAR` | 2 | no | Unknown `{{NAME}}` token | Fix the token, then dry-run | `cairn routine run {name} --dry-run` | `routine_plan.py:113-118` |
| `ROUTINE_INPUT_INVALID` | 2 | no | prompt_file missing, unreadable or empty; `cd` not a directory | Fix the path in config | `cairn routine run {name} --dry-run` | `routine_plan.py:164-192` |
| `ROUTINE_NESTED` | 2 | no | `routine run` started inside a peer | Start it from a shell or with `cairn routine kickstart {name}` | `cairn routine status {name}` | `routine_run.py:551-557` |
| `ROUTINE_DAY_FAILED` | 1 | yes | A day failed or was deferred; reasons in `failures` | Read the reason and the attempt dir; re-run with `--day` | `cairn routine status {name} --json` | `routine_run.py:510-524` |
| `INVOKE_USAGE` | 2 | no | Request refused before spawn: bad flag, profile, prompt or cwd | Fix the flag the message names | `cairn invoke {peer} --help` | `invoke.py:1283-1522, 2784-2786` |
| `INVOKE_NESTED` | 2 | no | Invoke inside a peer without `--allow-nested` | Do not nest; ask the user before passing `--allow-nested` | none | `invoke.py:1500-1505` |
| `INVOKE_BILLING_CHECK_FAILED` | 2 | no | Billing route not ready or the ADC token was refused | Run the command the message names, or ask the user | `cairn doctor --harness antigravity` | `invoke.py:1553-1562` |
| `INVOKE_BUSY` | 75 | yes | Another round of the thread holds the lock | Wait for it, then retry | `cairn invoke status {thread_id}` | `invoke.py:1596-1600, 2582-2586` |
| `INVOKE_DENIED` | 2 | yes | The peer's tool calls were refused and nothing stands | Add the allow rule the error names, then resume | `cairn invoke tail {thread_id} --no-follow` | `invoke.py:2104-2107` |
| `INVOKE_TIMEOUT` | 124 | yes | Wall-clock timeout | Raise `--timeout-s` or narrow the task | `cairn invoke status {thread_id}` | `invoke.py:2112-2113` |
| `INVOKE_IDLE_TIMEOUT` | 125 | yes | No activity for `idle_timeout_s` | Raise `--idle-timeout-s` or narrow the task | `cairn invoke status {thread_id}` | `invoke.py:2114-2118` |
| `INVOKE_CANCELLED` | 130 | no | Round stopped by a signal | Resume only if the user still wants it | `cairn invoke status {thread_id}` | `invoke.py:2119-2120` |
| `INVOKE_FAILED` | peer rc or 1 | per round | Peer exited non-zero or without a final event | Read `error` and `stderr_path`; retry when `retryable` | `cairn invoke status {thread_id}` | `invoke.py:2108-2111, 2133-2135` |

`INVOKE_FAILED` stores `exit: 1` and the docs say "the peer's code when it is positive";
its `retryable` default is `false` and the round value overrides it.

**Reserved codes** (in `RESERVED`, rendered in their own section, never emitted here):

| Code | Layer | Note |
|---|---|---|
| `EXPORT_PARSER_DRIFT` | 02 | Bytes read but zero frames parsed |
| `SYNC_SOURCE_GAP` | 02 | 30-day trailing gap in a source |
| `HOOK_INSTALL_ENTRY_CHANGED` | 03 | Uninstall left a user-changed entry in place |
| `ROUTINE_STATUS_ATTENTION` | 04 | `routine status` exit 1 |
| `ROUTINE_STATUS_BROKEN` | 04 | `routine status` exit 2 |
| `RECALL_DEGRADED` | 05 | Result is degraded, not a clean miss |
| `VERIFY_CITATION_UNRESOLVED` | 06 | A journal citation does not resolve |
| `VERIFY_QUOTE_MISMATCH` | 06 | A quote is not a substring of its frame |
| `FORGET_CLAIM_WITHDRAWN` | 08 | Re-annotation refused: the claim was withdrawn |
| `FORGET_REASON_UNKNOWN` | 08 | Reason outside the fixed vocabulary |
| `RECALL_FIRST_BLOCK_EDITED` | 09 | Marked block was edited by hand |

Names follow the one-line descriptions of those layers. If a lower layer's merged spec
uses other words, the implementer renames the reserved code to match before merge;
the codes are not published until this layer ships.

## 7. Failure modes

- A typo in a code at a site: `KeyError` at the call, caught by the site test.
- A reserved code emitted early: same `KeyError`; a lower layer's sites convert in a later PR.
- Stale `docs/error-codes.md`: the freshness test fails and prints the regenerate command.
- A verify argv that writes: blocked by an allowlist test (section 8). `cairn doctor`
  creates `data_root` when missing (`admin.py:1147-1152`); the allowlist accepts it and
  the doc says so.
- A placeholder the site cannot fill: `verify` is omitted, `next_step` stays.
- `--json` consumers that treated empty stdout as failure: the new failure object is
  additive and carries `"ok": false`.

## 8. Tests

`tests/test_errors.py`:

- `test_codes_are_upper_snake_and_unique`: every key matches `^[A-Z][A-Z0-9_]+$`, active and reserved disjoint.
- `test_every_entry_has_all_fields`: non-empty `meaning`, `why`, `next_step`; `exit` an int.
- `test_verify_is_read_only`: each `verify` starts with an allowlisted prefix (`cairn help`, `cairn config path|show`, `cairn doctor`, `cairn routine list|status`, `cairn routine run ... --dry-run`, `cairn invoke status|tail ... --no-follow|--help`, `cairn hook status`, `cairn recall list`).
- `test_verify_parses`: each `verify`, placeholders filled, parses with the real parser (`parse_args` via the `_ADMIN` module's `build_parser`/`main` under `SystemExit` capture).
- `test_placeholder_unfilled_drops_verify`: `fix("INVOKE_BUSY")` has no `verify`.
- `test_render_is_fresh`: `render_markdown()` equals `docs/error-codes.md`; message names `python -m cairn.errors --render > docs/error-codes.md`.
- `test_cli_lists_codes` / `test_cli_lookup_json` / `test_cli_unknown_exits_2` / `test_cli_reserved_code`.
- `test_fail_keeps_first_line_and_adds_code_line`: stderr line 1 byte-identical.
- `test_fail_json_object_shape`: keys `ok, code, error, exit, retryable, fix`.
- `test_exit_by_command`: `CONFIG_UNKNOWN_KEY` returns 2 for `command="routine"`, 1 otherwise.
- `test_outcome_codes_cover_exit_table`: every key of `invoke.EXIT_BY_OUTCOME` except `succeeded`, plus `failed` and `busy`, maps to a code whose `exit` equals the table value.

Site tests, added next to existing ones:

- `tests/test_config.py::test_unknown_key_carries_code`, `test_missing_config_carries_code`.
- `tests/test_routine.py::test_unknown_variable_reports_code`, `test_last_run_json_has_code`.
- `tests/test_recall_digest.py::test_empty_day_json_has_code`.
- `tests/test_recall.py::test_bad_date_json_failure_object`.
- `tests/test_invoke.py::test_result_line_has_code_and_fix_on_timeout`, `test_usage_error_json`, `test_succeeded_has_no_code`.
- `tests/test_admin_config.py::test_config_show_json_failure_object`.
- `tests/test_doctor_index.py::test_missing_executable_is_critical` gains `assertIn("[QMD_MISSING]", out)`.
- `tests/test_hooks.py` or `test_main`: unknown command prints the `code:` line, exit 2.

## 9. Docs to update

- `docs/error-codes.md` (new, generated): header comment `<!-- GENERATED by python -m cairn.errors --render; do not edit -->`, the four-step contract, a summary table, one section per code, the reserved table.
- `docs/error-registry-spec.md` (this spec).
- `skills/cairn/SKILL.md`: "On failure" section with the contract line; `cairn errors` in the command block.
- `skills/recall/SKILL.md:76`, `skills/invoke-claude/SKILL.md:109-117`, `skills/invoke-codex/SKILL.md`, `skills/invoke-antigravity/SKILL.md:195`: one line each, "the result carries `code`; see `cairn errors CODE`". Exit tables stay.
- `docs/routine-spec.md` and `docs/recall-digest-spec.md`: one cross-reference line under each exit table.
- `README.md`: one line next to the `cairn doctor` paragraph (`README.md:470`).

## 10. Implementation plan

1. `src/cairn/errors.py`: dataclass, 25 entries, `RESERVED`, helpers, `render_markdown`, `main`, `--render`. About 330 lines (entries are most of it).
2. `config.py`, `routine_plan.py`, `routine.py`, `routine_run.py`, `invoke.py`: `code=` on the three exception classes and the listed raises. About 25 lines.
3. Convert the ten sites in section 4 step 5. About 40 lines.
4. `__main__.py`: `_ADMIN` entry and `_HELP` line. 3 lines.
5. Generate `docs/error-codes.md`; add skills and docs lines. About 300 generated lines, 25 hand lines.
6. Tests in section 8. About 260 lines.
7. `make check`. Estimate: one working day if the site tests reuse the existing fixtures.

## 11. Open questions

1. Code on the human stderr path: a second `code:` line, or a ` [CODE]` suffix on the first?
   Recommended: the second line, so every first line and every pinned message stays exact.
2. Should `exit` in the registry describe the exit or drive it? Recommended: describe it
   only. Sites keep their own return values; `test_outcome_codes_cover_exit_table` and the
   site tests catch drift.
3. Should `cairn doctor` gain `--json` with a code per finding in this layer? Recommended:
   no. The suffix gives agents the code now; a `--json` doctor is its own PR.

## 12. Implementation notes (as shipped)

The lower layers landed before this one, so the codes this text reserved for them are
real entries now, with the words those layers use. Where a site was cheap to convert it
prints the code; otherwise the entry describes the failure and `raised_at` names the site.

- **No reserved codes.** Every reserved code became an entry or was dropped, so
  `RESERVED`, the `Reserved:` lines and the reserved branch of `cairn errors CODE` are
  gone. `cairn errors --json` prints `{"codes": [...]}`.
- **Entries added for layers 02-09** (38 entries in all): `EXPORT_PARSER_DRIFT`,
  `EXPORT_DOCUMENT_UNPARSEABLE`, `SYNC_SOURCE_GAP` (doctor suffix; the export's own
  `Error:` line and the sync gap-check step print no code yet); `CODEX_HOOK_TRUST_BROKEN`
  (doctor, Codex trust stale, missing or disabled; `manual` maps to `HOOK_NOT_TRUSTED`);
  `ROUTINE_NEEDS_ATTENTION` and `ROUTINE_HELD` (`routine status` exit 1 and 2, in `--json`
  and as a stderr code line); `RECALL_DEGRADED` (described only: the status is data and
  the exit is 0); `RECALL_VERIFY_FAILED` (`recall verify` exit 4, `code` in `--json`);
  `DECISION_CLAIM_WITHDRAWN` (`decide`/`supersede` exit 3); and four `INSTRUCTIONS_*`
  codes for `cairn instructions` exit 1 (`HOME_MISSING`, `BLOCK_EDITED`,
  `MARKERS_DAMAGED`, and `FAILED` for read, write, race, encoding and ledger errors).
  `FORGET_REASON_UNKNOWN` was dropped: argparse rejects a reason outside the vocabulary
  with its own exit 2. `HOOK_INSTALL_ENTRY_CHANGED` was dropped: uninstall only warns.
- **Three more fields.** `raised_at` names each site as `path:function`, not `path:line`, so
  the generated page does not go stale when lines move; a test checks every function
  exists. `shown_in` says where the code is printed today (empty: described only).
  `exit_note` replaces the number when one number cannot describe the exit
  (`INVOKE_FAILED`, `QMD_INDEX_INCOMPLETE`, `SYNC_SOURCE_GAP`, `RECALL_DEGRADED`).
- **`exit` describes, never drives.** `fail(code, message, *, exit, ...)` takes the
  site's exit and returns it; `exit_for(code, command)` is for tests.
- **Placeholders** are `{name}`, `{thread_id}`, `{peer}` and `{harness}`.
- **`ROUTINE_USAGE`** covers the exit-2 routine errors that name no more specific code
  (bad name, `--day` not ended, `--json` with `--dry-run`, a bad `--reason`).
- **`CONFIG_NOT_FOUND`** is also the code when `$CAIRN_CONFIG` names a missing file.
- **`config path`** prints the code line after its two existing lines; `config show
  --json` prints the failure object on stdout.
- **Existing tests.** Tests from layers 04 and 06-09 that pinned a whole stderr string
  or a whole doctor line now expect the code line or the suffix as well; their first
  lines are unchanged.
- **Doctor `--json`** is not part of this layer, as section 11 recommends.
