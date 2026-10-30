# Codex hook trust from `cairn hook install`: implementation spec

Status: implemented in this PR (branch `shav/mem-03-codex-trusted-hash`). Section 12
records where the implementation differs from the earlier sections; where they disagree,
section 12 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0. Fixture paths
(`/tmp/codex-home`, `~/.codex`) are invented or generic. A claim not checked is marked
UNVERIFIED.

Dependencies: none. This layer relies on no lower layer. It touches `admin.py` doctor
code in one new function plus one call line, so it merges beside layer 02 (which also
adds a doctor check) without a shared hunk. Layer 10 can later map the refusal messages
below to error codes.

---

## 1. Purpose and non-goals

### 1.1 Purpose

After `cairn hook install --harness codex`, a user must open `/hooks` in Codex and trust
cairn's SessionEnd command by hand. Every reinstall changes the command (it embeds the
engine path and the background environment), so the trust goes stale and the manual step
repeats. This layer makes the install command finish the job:

1. Write the Codex trust table for each hooks.json group cairn writes.
2. Record a receipt beside the Codex config with a sha256 per entry cairn wrote.
3. `--dry-run` prints every planned write and writes nothing.
4. `cairn hook uninstall --harness codex` removes only entries whose hash still matches
   the receipt.
5. `cairn doctor --harness codex` and `cairn hook status --harness codex` report the
   trust state from the files, without the Codex app-server.
6. The trust-hash format is pinned to the Codex versions it was verified against. Outside
   that range, cairn writes no trust and prints the manual `/hooks` step.

The user runs `cairn hook install` explicitly. That run is the consent to trust cairn's
own command, and only that command. `--no-trust` keeps the manual review.

### 1.2 Non-goals

- Trust for the bundled plugin hook (`hooks/codex.json`). Its state key format is not
  verified, and tested Codex 0.160.0 and 0.163.0 do not expose it
  (`references/harnesses/codex.md:74-75`).
- Re-keying trust tables of other tools when a group index shifts. cairn warns instead.
- A general TOML writer. cairn edits whole `[hooks.state."<key>"]` tables only.
- Claude hooks. Claude takes its hook from the plugin and has no trust table
  (`src/cairn/hooks.py:434-435`).

---

## 2. Current behaviour (main at 2.9.0)

| Fact | Where |
|---|---|
| `hook install` writes the launcher env file first, then calls `codex_registration.configure(config)` | `src/cairn/hooks.py:446-453` |
| `hook uninstall --harness codex` calls `configure(None, remove=True)` without loading config | `src/cairn/hooks.py:425-429` |
| hooks.json is `$CODEX_HOME/hooks.json` (resolved); ownership file is `<XDG config>/cairn/codex-hook-registration.json` | `src/cairn/harnesses/codex_registration.py:17-19` |
| Install inspects loaded hooks through `codex app-server` `hooks/list` first; a failed inspection raises before any write | `codex_registration.py:36`, `src/cairn/harnesses/codex.py:348-391` |
| Bundled discovery (`pluginId == "cairn@cairn"`) removes the fallback and writes nothing new | `codex_registration.py:37-39,91-94` |
| Command is `shlex.join(["/usr/bin/env", <env>..., sys.executable, "-m", "cairn", "hook", "handle", "--harness", "codex", "--require-plugin"])` | `codex_registration.py:43-56` |
| Ownership file `{path, commands[]}` is written before hooks.json, so an interrupted install can be removed | `codex_registration.py:64-70` |
| Ownership is by exact command string; a group whose only handler is cairn's is dropped, a mixed group keeps the others | `codex_registration.py:22-31` |
| Install removes the owned group, then appends a new group at the end of `SessionEnd`, so the index can move | `codex_registration.py:71-84` |
| Group shape: one handler `{type: "command", command, timeout: 3, statusMessage: "Cairn automatic session sync"}` | `codex_registration.py:73-83` |
| Wrong `CODEX_HOME` for the ownership file is a `ValueError` | `codex_registration.py:60-63` |
| Install success text ends "Review/trust it in /hooks." | `codex_registration.py:95` |
| `hook status` without `--harness codex` says trust is unverified; with it, adds `host_hooks` from `hooks/list` | `src/cairn/hooks.py:316,483-489` |
| Doctor prints `enabled=` and `trust=` per cairn hook from `hooks/list`, or a warning when inspection fails | `src/cairn/admin.py:1212-1236` |
| `atomic_write` uses `mkstemp`, so a new file is mode 0600, and `os.replace` replaces a symlink with a file | `src/cairn/storage.py:43-56` |
| `Codex.plugin_enabled` already reads `config.toml` with `tomllib` | `src/cairn/harnesses/codex.py:331-346` |
| Docs tell the user to trust by hand | `README.md:80,424`, `references/harnesses/codex.md:71-72`, `skills/setup/SKILL.md:245-246`, `skills/setup/references/updating.md:70-71`, `docs/codex-acceptance.md:47` |

---

## 3. Verified trust-hash format

### 3.1 Evidence (2026-10-30, read-only)

- Installed Codex: `codex-cli 0.165.0` (`codex --version`).
- A live `~/.codex/config.toml` holds six `[hooks.state."<path>:<event>:<group>:<handler>"]`
  tables, each with one key, `trusted_hash = "sha256:<64 hex>"`. Codex writes an empty
  `[hooks.state]` header, then one sub-table per entry, separated by blank lines. The
  tables are not at end of file: other tables follow them.
- The live `hooks.json` holds four groups (SessionStart, UserPromptSubmit, Stop: one
  foreign handler each, no `timeout`, no `statusMessage`; SessionEnd: cairn's fallback).
- The formula in 3.2 reproduced all four stored hashes. No other candidate did.
- The Stop table at group index 1 has no group in hooks.json. Codex leaves stale tables
  in place.
- cairn's Codex queue shows a completed SessionEnd request at 12:32 on 2026-10-30 with
  0.165.0 installed, so the trusted fallback ran. `hooks/list` was not called in this
  check (it starts the app-server, which writes under `~/.codex`).

Candidates tested and rejected: key order as inserted; default `json.dumps` separators;
`async` absent; `timeout` absent when the handler has none; CamelCase `event_name`;
`statusMessage` dropped (this one fails for cairn's group only). A comparable tool hashes
`{event_name, hooks:[{type, command, timeout, async:false}]}` and checked it against
Codex 0.162.1. That form omits `statusMessage`, so it does not reproduce cairn's hash.

### 3.2 Formula

```
event_name = snake_case(event)              # SessionEnd -> session_end
handler    = {"type": h["type"], "command": h["command"],
              "timeout": h.get("timeout", 600), "async": False}
             + {"statusMessage": h["statusMessage"]} when present
identity   = {"event_name": event_name, "hooks": [handler]}
text       = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
trusted_hash = "sha256:" + sha256(text.encode("utf-8")).hexdigest()
key        = f"{hooks_path}:{event_name}:{group_index}:{handler_index}"
```

`ensure_ascii=False` is inferred from Codex being a Rust program that emits UTF-8. All
live commands were ASCII, so the choice is UNVERIFIED for non-ASCII. cairn refuses to
write trust for a command with a non-ASCII character (section 7). `matcher` is not part
of the verified identity. cairn writes no matcher.

### 3.3 Known-answer vectors (invented fixtures, for tests)

| event | handler | trusted_hash |
|---|---|---|
| `session_end` | command `/usr/bin/env CAIRN_CONFIG=/tmp/cairn/config.json /tmp/venv/bin/python -m cairn hook handle --harness codex --require-plugin`, timeout 3, statusMessage `Cairn automatic session sync` | `sha256:0ec9f818c3b060ec3263662e9ba07558e1008dc000e285af0dcba792bac89743` |
| `stop` | command `echo hi`, no timeout | `sha256:f2aa169c7a1cf9b7cc9b61ce5ecec0315e0f7d13ac35935b1aa74440f9a839f8` |

### 3.4 Version pin

New constant in `src/cairn/harnesses/codex_trust.py`:

```python
# Codex versions on which trusted_hash was recomputed from a live hooks.state entry.
TRUST_FORMAT_VERIFIED: tuple[tuple[str, str], ...] = (("0.165.0", "2026-10-30"),)
```

The accepted range is from the lowest verified version up to the last patch of the
highest verified minor: today `0.165.0 <= v < 0.166.0`. A newer Codex gets the manual
step until someone verifies it and adds a row. The docs carry the same table (section 9).

---

## 4. Surface

| Command | Change |
|---|---|
| `cairn hook install --harness codex [--dry-run] [--no-trust]` | writes the group, the trust table and the receipt |
| `cairn hook uninstall --harness codex [--dry-run]` | removes owned, unchanged entries only |
| `cairn hook status --harness codex` | adds `codex_trust` to the JSON report |
| `cairn doctor --harness codex` | adds one `Codex hook trust` line per owned group |

`--dry-run` also applies to `--harness antigravity` as "not supported" (exit 2, message
`--dry-run applies to --harness codex`). `--no-trust` with another harness exits 1 with
`--no-trust applies to --harness codex`, as `--grant-commands` does (`hooks.py:447-448`).

Files:

| File | Owner | Content |
|---|---|---|
| `$CODEX_HOME/hooks.json` | shared | cairn's SessionEnd group |
| `$CODEX_HOME/config.toml` | shared | `[hooks.state."<key>"]` with `trusted_hash` |
| `$CODEX_HOME/cairn-hook-receipt.json` | cairn | receipt, section 6 |
| `$CODEX_HOME/config.toml.cairn-bak` | cairn | the bytes before cairn's last edit, same mode |
| `<XDG config>/cairn/codex-hook-registration.json` | cairn | legacy v1 ownership file; read once, deleted after migration |

Exit codes: 0 success (also when trust is skipped and the manual step is printed);
1 refusal or I/O error (`Error: ...` on stderr, `hooks.py:508-510`); 2 argparse.

Exact messages (stdout unless marked):

- `Registered Cairn fallback in {hooks_path} (SessionEnd group {i}) and trusted it in {config_path} for Codex {version}. Restart Codex.`
- `Cairn fallback in {hooks_path} is already registered and trusted; nothing changed.`
- `Codex {version} is outside the versions cairn verified the trust hash on ({range}); trust not written. Review/trust the Cairn SessionEnd hook in /hooks.`
- `Could not read the Codex version ({reason}); trust not written. Review/trust the Cairn SessionEnd hook in /hooks.`
- `--no-trust: trust not written. Review/trust the Cairn SessionEnd hook in /hooks.`
- stderr `warning: [hooks.state."{key}"] in {config_path} has keys cairn did not write ({keys}); left unchanged. Review/trust the Cairn SessionEnd hook in /hooks.`
- stderr `warning: Codex reports the Cairn hook as {trustStatus} after the write; trust table removed. Review/trust it in /hooks.`
- stderr `warning: removing the Cairn group shifted {n} later SessionEnd group(s) down one index; Codex will ask to trust them again.`
- stderr `warning: {entry} changed since cairn wrote it; left in place.` (uninstall)
- Error: `{config_path} does not parse as TOML ({detail}); cairn changed nothing.`
- Error: `{config_path} changed while cairn was writing; cairn changed nothing. Run the command again.`
- Error: `the edited {config_path} did not parse back to the expected tables; cairn changed nothing.`

---

## 5. Behaviour, step by step

### 5.1 Install

1. Parse arguments. With `--dry-run`, skip `install_launcher` (`hooks.py:449`).
2. Run `inspect_hooks` as today. Bundled discovery keeps today's path, and it also removes
   cairn's owned trust table for the removed fallback group.
3. Probe `codex --version` with `run_capped`, 5 s timeout. Parse `(\d+)\.(\d+)\.(\d+)`.
4. Take the lock `lock_path(hooks_path, "registration")` (`codex_registration.py:57`).
5. Read hooks.json (JSON) and config.toml (bytes, then `tomllib`). Record `(st_mtime_ns,
   st_size, sha256)` of config.toml. Load the receipt, else migrate the v1 file.
6. Plan the group. If an owned group exists (canonical sha matches the receipt, or its
   handler command is in the legacy `commands`), replace it in place at its index. Else
   append. Never move an owned group, so later groups keep their indexes.
7. Plan the trust table: key and hash from section 3.2, computed from the group exactly
   as it will be serialised. Skip it with the matching message when `--no-trust` is set,
   the version is outside the range, or the command has a non-ASCII character.
8. Plan stale-table removal: an owned table (receipt hash matches) whose key differs from
   the new key.
9. `--dry-run`: print each step as `would write`, `would replace` or `would remove` with
   the file, the key or index, and the sha256. Exit 0.
10. Write the receipt with `status: "prepared"` and every planned entry as `pending: true`.
11. Write hooks.json. Read it back, recompute the group's canonical sha256 and the
    trusted_hash from the parsed bytes.
12. Edit config.toml (section 5.3): remove stale owned tables, then write the new table.
13. Write the receipt with `status: "installed"`, `pending: false`.
14. Verify: call `inspect_hooks` again. If the cairn hook reports a `trustStatus` other
    than `trusted`, remove the table cairn just wrote, drop it from the receipt and print
    the warning. If inspection fails, keep the table and print the success line with
    `(Codex did not answer hooks/list: {reason})`.

The trust table goes last. A crash before step 12 leaves an untrusted group that Codex
skips. A crash after it leaves pending receipt entries that the next run treats as owned.

### 5.2 Uninstall

Reverse order: trust tables, then the hooks.json group, then the receipt. An entry is
removed only when its current sha256 equals the receipt's. A changed entry stays and gets
the warning. If foreign SessionEnd groups follow the removed group, print the index-shift
warning. Delete the receipt only when no owned entry is left. `--dry-run` prints the plan.

### 5.3 config.toml edit (stdlib only)

`tomllib` reads. There is no stdlib writer, so cairn edits lines:

1. Decode UTF-8. Keep the line ending of the first line (`\r\n` or `\n`). Split with
   `keepends=True`.
2. Find table headers: a line whose stripped form starts with `[` but not `[[`. Parse that
   one line with `tomllib.loads(line + "\n")` to get its dotted path. A header that does
   not parse alone is not a table header (for example a line inside a multi-line string).
3. A table's span is its header through the line before the next header, minus trailing
   blank lines.
4. Replace: allowed only when the span's parsed body is exactly `{"trusted_hash": <str>}`.
   Keys other than `trusted_hash`, such as `enabled = false`, mean the user changed it:
   leave it and warn.
5. Insert: if no table for the key exists and the parsed document has no such key under
   `hooks.state` (inline tables and dotted keys included), insert after the span of the
   last `[hooks.state."..."]` table. With none, append at end of file. Make sure the file
   ends with a newline, then add a blank line, the header and the value.
6. Remove: delete the span and one blank line before it.
7. Render the header as `[hooks.state.<basic string>]`, escaping `\`, `"` and control
   characters. Render the value as `trusted_hash = "sha256:<hex>"`.
8. Safety checks before the replace, any failure changes nothing:
   - The new text parses with `tomllib`.
   - The parsed result equals the original parse with exactly the planned keys added,
     changed or removed under `hooks.state`. Every other value compares equal.
   - The file's `(st_mtime_ns, st_size, sha256)` is unchanged since step 5. Codex does
     not take cairn's lock.
9. Copy the original bytes to `config.toml.cairn-bak`, then write through the resolved
   path (a symlinked config stays a symlink) with `atomic_write`, then `chmod` to the
   original mode.

The same symlink and mode rule applies to hooks.json.

### 5.4 Trust state (doctor and status)

A pure function `trust_state(hooks_path, config_path, receipt)` returns, per cairn group:

| state | meaning |
|---|---|
| `trusted` | the table's `trusted_hash` equals the hash of the group now in hooks.json |
| `stale` | a table exists with a different hash (the group changed after trust) |
| `missing` | no table for the key |
| `disabled` | the table has `enabled = false` |
| `manual` | the Codex version is outside the range; trust is the user's step |

Doctor prints `ok Codex hook trust - trusted (written by cairn)` or `(written by Codex)`,
and `warn` with the `/hooks` step for the other states. Doctor keeps the existing
`hooks/list` line (`admin.py:1212-1236`).

---

## 6. Data formats

Receipt `$CODEX_HOME/cairn-hook-receipt.json`:

```json
{
  "receipt_version": 2,
  "harness": "codex",
  "status": "installed",
  "hooks_path": "/tmp/codex-home/hooks.json",
  "config_path": "/tmp/codex-home/config.toml",
  "codex_version": "0.165.0",
  "commands": ["/usr/bin/env CAIRN_CONFIG=/tmp/cairn/config.json ..."],
  "entries": [
    {"kind": "hooks_json_group", "event": "SessionEnd", "index": 0,
     "sha256": "<hex of canonical group JSON>", "pending": false},
    {"kind": "config_toml_table", "key": "/tmp/codex-home/hooks.json:session_end:0:0",
     "trusted_hash": "sha256:<hex>", "sha256": "<hex of canonical table JSON>",
     "origin": "written", "replaced": null, "pending": false}
  ],
  "updated_at": "2026-10-30T12:00:00+00:00"
}
```

Canonical JSON for an entry sha256 is `json.dumps(obj, sort_keys=True, separators=(",",
":"), ensure_ascii=False)`. For a table, `obj` is the parsed table. `origin` is `written`
or `adopted` (a table already present with the exact hash cairn would write). `replaced`
holds an earlier `trusted_hash` that cairn replaced. `commands` keeps v1 compatibility.

`hook status --harness codex` adds:

```json
"codex_trust": {"codex_version": "0.165.0", "in_range": true,
  "groups": [{"index": 0, "key": "...", "state": "trusted", "origin": "written"}]}
```

---

## 7. Failure modes

| Case | Result |
|---|---|
| hooks.json does not parse | `ValueError` as today, nothing written |
| config.toml does not parse | error, hooks.json not written either (checked at step 5) |
| config.toml absent | create it with the one table, mode 0600 |
| `codex` not on PATH | `inspect_hooks` fails first, as in 2.9.0; nothing written |
| Version unreadable (timeout, non-zero exit, unknown output) | group written, trust skipped, manual message |
| Version outside range | as above, with the range |
| Table at the key has extra keys | left unchanged, warning, manual step |
| config.toml changed mid-run | error, nothing changed by this step; receipt keeps pending entries |
| Post-write parse mismatch | error, original kept |
| `hooks/list` reports not trusted | table removed, warning |
| Non-ASCII in command | trust skipped, manual message |
| `CODEX_HOME` is a symlink | key uses the resolved path (`codex_registration.py:18`); UNVERIFIED that Codex keys the same path; step 14 catches a mismatch |
| Legacy v1 file for another `CODEX_HOME` | today's `ValueError` (`codex_registration.py:60-63`) |
| Removing the group shifts foreign groups | warning; their tables are not touched |

---

## 8. Tests

New `tests/test_codex_trust.py` (temp `CODEX_HOME`, `inspect_hooks` and the version probe
patched):

- `test_trusted_hash_known_answers` - both vectors from 3.3.
- `test_hash_includes_status_message_and_default_timeout` - dropping either changes the hash.
- `test_version_range_accepts_verified_minor_only` - 0.165.0 and 0.165.9 in, 0.164.9 and 0.166.0 out, garbage out.
- `test_install_writes_group_then_table_then_receipt` - order via patched writers; receipt prepared before hooks.json.
- `test_install_is_idempotent_and_keeps_index` - second run: "nothing changed", same index.
- `test_reinstall_with_new_command_replaces_in_place` - foreign group after cairn's keeps its index and table.
- `test_toml_edit_preserves_foreign_bytes` - every byte outside the cairn table is identical; CRLF file stays CRLF.
- `test_toml_insert_after_last_hooks_state_table` - insertion point when `[hooks.state."..."]` tables exist.
- `test_toml_refuses_inline_hooks_state` - `hooks = { state = {...} }` refused, nothing written.
- `test_table_with_enabled_false_is_left_alone` - warning, manual step.
- `test_adopts_identical_existing_table` - `origin: adopted`, no duplicate header.
- `test_concurrent_change_refuses` - mtime change between read and write.
- `test_out_of_range_version_writes_no_trust` - group written, table absent, message exact.
- `test_no_trust_flag` - same, `--no-trust` message.
- `test_dry_run_writes_nothing` - no launcher, hooks.json, config.toml or receipt change; plan lines printed.
- `test_uninstall_removes_only_unchanged_entries` - edited table and edited group stay, warnings printed.
- `test_uninstall_warns_on_index_shift` - foreign group after cairn's.
- `test_migrates_v1_ownership_file` - v1 commands become owned; v1 file deleted after install.
- `test_bundled_discovery_removes_owned_trust_table` - extends `test_bundled_discovery_removes_previous_fallback` (`tests/test_hooks.py:474`).
- `test_post_write_untrusted_rolls_back_table` - `hooks/list` says `untrusted`.
- `test_symlinked_config_stays_symlink_and_mode_kept`.

`tests/test_harness_diagnostics.py`: `test_doctor_reports_trust_states` - trusted, stale,
missing, disabled, manual lines. Existing `TestCodexRegistration` tests keep passing.

---

## 9. Docs to update

- `README.md:71,80,421-424` - install now trusts the hook; manual `/hooks` only outside the verified range or with `--no-trust`.
- `references/harnesses/codex.md:62,71-77` - same, plus the verified-version table and the receipt path.
- `skills/setup/SKILL.md:245-246` and `skills/setup/references/updating.md:70-71` - reinstall refreshes trust; check `cairn doctor --harness codex`.
- `docs/codex-acceptance.md` - a dated row: trust written by cairn, Codex version, `hooks/list` result.
- `docs/codex-trusted-hash-spec.md` - this spec, with the version table from 3.4.

---

## 10. Implementation plan

1. `src/cairn/harnesses/codex_trust.py` (new, about 260 lines): hash, version probe and range, TOML table find/insert/replace/remove, safety checks, `trust_state`.
2. `src/cairn/harnesses/codex_registration.py` (about +150 lines): receipt v2, v1 migration, in-place replace, ordered writes, dry-run plan, uninstall by hash, post-write verify.
3. `src/cairn/storage.py` (about +15 lines): optional `mode` argument for `atomic_write`, write through a resolved symlink.
4. `src/cairn/hooks.py` (about +25 lines): `--dry-run`, `--no-trust`, `codex_trust` in status.
5. `src/cairn/admin.py` (about +40 lines): `_check_codex_trust(config)`, one call in `doctor_main`.
6. Tests (about 450 lines), then docs.

Estimate: one day with the tests, plus 30 minutes for a live check on a scratch `CODEX_HOME`.

---

## 11. Open questions

1. Widen the range below 0.165.0? Default: no. Add 0.160.0 and 0.163.0 rows only after a
   live recompute on a temporary npm prefix, as in `docs/codex-acceptance.md:9-10`.
2. Receipt location: beside the Codex config (this spec) or the existing XDG file?
   Default: beside the config, because one receipt per `CODEX_HOME` removes the
   wrong-home error class. Migrate the v1 file once.
3. Replace a stale table at cairn's key that cairn did not write? Default: yes, when its
   only key is `trusted_hash`, and record the old value in `replaced`. Any other key means
   the user edited it, so cairn leaves it.

---

## 12. Implementation notes

The open questions in section 11 took their recommended defaults. The code differs from
the sections above in these points:

1. Array-of-tables headers (`[[...]]`) also end a table's span. A live `config.toml` holds
   `[[hooks.SessionStart]]` tables, so 5.3 step 2 alone would extend a span over them.
2. A span drops trailing comment lines as well as blank lines, so a comment written above
   the next table stays with that table when cairn removes its own.
3. The config.toml edit is planned and checked (5.3 step 8, parse and equality) before
   hooks.json is written. A refused edit leaves every file unchanged. The race check
   (unchanged mtime, size and sha256) runs again just before the replace.
4. With `--no-trust`, an unreadable version, a version outside the range or a non-ASCII
   command, install also removes the unchanged trust tables cairn wrote earlier, so no
   cairn-written trust remains for a command the user did not trust through cairn.
5. When trust is skipped, stdout is `Registered Cairn fallback in {hooks_path} (SessionEnd
   group {i}).` followed by the message from section 4. The non-ASCII message is `The Cairn
   hook command has a non-ASCII character, and the trust hash is not verified for one;
   trust not written. Review/trust the Cairn SessionEnd hook in /hooks.`
6. If `hooks/list` after the write does not list cairn's command, cairn keeps the table and
   prints the success line. Only a listed hook with a `trustStatus` other than `trusted`
   rolls the table back.
7. A receipt whose `hooks_path` is not this `CODEX_HOME`'s hooks.json is an error that asks
   the user to move the receipt aside.
8. Uninstall reads config.toml only when the receipt names a trust table, so a config.toml
   that does not parse cannot block the removal of a v1 registration.
9. A group with cairn's handler and a foreign handler is not replaced in place: install
   removes cairn's handler from it and appends a new group, as 2.9.0 did.
10. `--dry-run` without `--harness codex` is an argparse error (exit 2) for install and
    uninstall, not only for `--harness antigravity`.
11. The range renders as `>= 0.165.0, < 0.166.0`. The `codex_trust` report also carries
    `verified_range`, and `version_error` when the version probe failed.
12. Doctor's check is `_check_codex_trust()` with no argument. It prints nothing when
    hooks.json holds no cairn group (the bundled hook case); the `hooks/list` line covers
    that. An adopted table reports `(written by Codex)`.
13. `atomic_write` gained the `mode` argument only. The callers resolve symlinks, so other
    writers keep their behaviour. Trust-state precedence is `disabled`, `trusted`,
    `manual`, `stale`, `missing`.
14. Review fixes. Step 6 holds for receipt v2: a group with only cairn's command whose
    sha256 is neither in the receipt nor the group cairn would write is user-edited.
    Install raises `SessionEnd group {i} in {hooks_path} changed since cairn wrote it;
    cairn changed nothing. Restore or remove that group, then run the command again.`
    (exit 1). Command ownership alone applies to the v1 file and to a receipt with no
    group entry.
15. The prepared receipt (step 10) also keeps every entry of the earlier receipt, marked
    pending, so after a crash either version of a replaced group or table is cairn's.
    Several entries can share one table key; any of their sha256 values marks it owned.
16. `write_config` stages and fsyncs the backup and the new file first, then runs the race
    check, then renames both. A Codex save between that check and the rename is still lost:
    a writer that does not share cairn's lock cannot be excluded.
17. `--dry-run` takes no lock and writes no file. It lists every launcher copy
    (`launcher_paths()`) with its sha256. The receipt line gives the entry count, not a
    sha256: the receipt carries a timestamp. Install still runs `hooks/list`, and Codex can
    create its own state files in `$CODEX_HOME` while it answers.
18. Uninstall warns `no SessionEnd group in {hooks_path} runs cairn's command any more
    (cairn wrote group {i}); left as it is, and its receipt entry dropped.` when no group
    matches the receipt, for example after the user changed the command.
19. A receipt that is not a JSON object, has another `receipt_version`, or has an entry
    without a known `kind`, a `sha256` or (for a table) a `key` is a `ValueError` that
    names the receipt and asks to move it aside (exit 1).
