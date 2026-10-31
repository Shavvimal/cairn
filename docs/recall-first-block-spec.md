# `cairn instructions`: the recall-first block

Status: implemented in this PR (branch `shav/mem-09-recall-first-block`). Section 11 lists
where the implementation departs from the text below; where they differ, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0. The two harness
facts this design relies on are checked against the installed CLIs in section 3.4.

Dependencies: none. This layer is independent of 01-08. It does not touch
`src/cairn/hooks.py`, which layer 03 rewrites. Layer 05 adds an ask-with-a-hypothesis rule
inside the recall and search skills; this block points the agent at those skills and
works with or without layer 05. Layer 10 can later register this layer's error messages
as codes; layer 12 will parse the new fenced `cairn instructions ...` lines.

## 1. Purpose and non-goals

### Purpose

cairn installs the `recall` and `search` skills and a SessionEnd hook, but no text in
the agent's own instruction file tells it to look in cairn before it asks the user to
re-explain past work. A comparable tool measured this gap: with its memory tools
registered and no instruction block, the agent almost never used its memory. With a
short marked block in the user instruction file, it used its memory in most trials. That
is the other tool's measurement. cairn has not reproduced it.

This layer adds `cairn instructions install|remove|show|status --harness claude|codex`.
The command writes, upgrades, removes and reports one short marked block in the user-level
instruction file of Claude Code or Codex. The block tells the agent to run recall before
it asks about past work, to use exact search for literals, and to ask with a hypothesis.

### Why a new command, not `cairn hook install`

1. `hook install` needs a valid config and writes the hook launcher
   (`src/cairn/hooks.py:445-449`). The block needs neither.
2. `skills/setup/references/updating.md:61` re-runs `hook install` after every engine
   reinstall, and layer 03 rewrites that install branch (receipts, `--dry-run`). A
   separate module keeps the two layers free of merge conflicts.
3. An edit to the user's personal instruction file is a consent decision. It is not the
   same decision as a hook registration, so setup asks it as its own question.
4. `hook status` prints the sync queue as JSON (`src/cairn/hooks.py:481-498`), and
   `hook uninstall` removes hook registrations (`src/cairn/hooks.py:425-444`). Block
   state and block removal do not fit those verbs.

### Non-goals

- No per-turn repetition. cairn has no UserPromptSubmit hook. Claude gets only the bundled
  SessionEnd hook (`references/harnesses/claude.md:73-76`). See open question 1.
- No project-scope file (`./CLAUDE.md`, `./AGENTS.md`). User scope only.
- No Antigravity target in this layer (section 3.4).
- No MCP server and no change to the recall or search skills.
- `hook install` does not call this command.

## 2. Current behaviour

| Fact | Where |
|---|---|
| Top-level commands dispatch through the `_ADMIN` table of `"module:function"` strings | `src/cairn/__main__.py:24-35` |
| The help overview lists `hook install` and doctor, and has no instructions command | `src/cairn/__main__.py:74,81-96` |
| `hook install` choices are claude, codex and antigravity, and it needs a config | `src/cairn/hooks.py:39,402-404,445-459` |
| Codex home is `$CODEX_HOME`, else `~/.codex` (`codex_home()`) | `src/cairn/harnesses/codex_models.py:28-31` |
| The same Codex resolution is repeated inline twice | `src/cairn/harnesses/codex.py:333`, `src/cairn/harnesses/codex_registration.py:18` |
| No Claude home resolver exists in the Claude adapter | `src/cairn/harnesses/claude.py:195-335` (no home function) |
| cairn treats `CLAUDE_CONFIG_DIR` as Claude's config home, default `~/.claude` | `src/cairn/runtime.py:36-47` |
| Antigravity homes known to cairn: app data `~/.gemini/antigravity-cli`, hooks `~/.gemini/config/hooks.json` | `src/cairn/harnesses/antigravity.py:117`, `src/cairn/harnesses/antigravity_registration.py:41-45` |
| Antigravity is a peer, not a host that loads cairn skills | `src/cairn/harnesses/__init__.py:7-9` |
| State home is `$XDG_STATE_HOME`, else `~/.local/state`; routines keep state under `<state>/cairn/routines` | `src/cairn/config.py:825-828,834` |
| `atomic_write` writes through `mkstemp` and `os.replace`. A symlink at the path becomes a regular file, and the mode becomes 0600 | `src/cairn/storage.py:43-59` |
| `lock_path` and `locked` give a per-resource flock under `~/.local/state/cairn/locks` | `src/cairn/storage.py:18-36` |
| Doctor prints `ok`/`warn`/`FAIL` lines through `_ok`/`_warn`/`_fail` | `src/cairn/admin.py:847-857` |
| Doctor takes `--harness claude\|codex\|antigravity`. Its hook section runs only with a config | `src/cairn/admin.py:1096,1202-1258` (cron check at `:1260`) |
| Claude skill names are `/cairn:recall` and `/cairn:search` | `references/harnesses/claude.md:60-61` |
| Codex selects skills through `/skills` or the `$` picker | `references/harnesses/codex.md:51-52` |
| `/search --exact` uses rg for literals: identifiers, error strings, paths | `skills/search/SKILL.md:21-22,46-48` |
| Recall takes a time or a topic | `skills/recall/SKILL.md:4,22-30` |
| Setup step 6 runs `hook install` and has no instruction-file step | `skills/setup/SKILL.md:237-251` |
| The test fixture points HOME, XDG homes, `CODEX_HOME` and `CLAUDE_CONFIG_DIR` into a temp root | `tests/test_shared_store.py:67-78` |
| CI fails when `~/.local/state/cairn` exists after the tests | `.github/workflows/ci.yml:36` |

## 3. Surface

### 3.1 CLI

```
cairn instructions install --harness claude|codex [--dry-run] [--force] [--json]
cairn instructions remove  --harness claude|codex [--dry-run] [--json]
cairn instructions show    --harness claude|codex
cairn instructions status  [--harness claude|codex] [--json]
```

- `install`: add or upgrade the block. `--force` overrides an opt-out or a hand edit.
- `remove`: delete only the block and record an opt-out.
- `show`: print the exact block bytes (markers included) for a paste by hand.
- `status`: print the state per harness. Without `--harness`, it reports each harness
  whose home exists.
- `--dry-run`: print a unified diff (`difflib.unified_diff`, labels `a/PATH` and `b/PATH`)
  to stdout. The command writes nothing and does not change the ledger.

Dispatch: `"instructions": "cairn.instructions:main"` in `_ADMIN`. One help line:
`cairn instructions install | remove | show | status --harness claude|codex   recall-first block in the user instruction file`.

### 3.2 Exit codes

| Code | When |
|---|---|
| 0 | Written, unchanged, removed, skipped because of an opt-out, dry run, show, status |
| 1 | Refused or failed: the harness home is missing, markers are damaged, a hand edit without `--force`, the file changed during the write, the file is not UTF-8, the ledger is unreadable, an OS error |
| 2 | argparse usage error (`--harness antigravity` included) |

### 3.3 Exact messages

Success lines go to stdout. Errors go to stderr with the prefix `Error: `.

| Case | Message |
|---|---|
| appended | `added: recall-first block v1 appended to PATH` |
| created | `created: PATH did not exist; cairn created it with only the recall-first block` |
| upgraded | `updated: recall-first block in PATH upgraded from vOLD to v1` |
| current | `unchanged: recall-first block v1 in PATH is current` |
| opt-out kept | `skipped: the recall-first block for HARNESS was removed on DATE (REASON); pass --force to add it back` |
| removed | `removed: recall-first block from PATH; opt-out recorded, install will not add it back without --force` |
| removed, file deleted | the removed line plus ` (file deleted: cairn created it and nothing else was in it)` |
| nothing to remove | `absent: no recall-first block in PATH; opt-out recorded` |
| home missing | `Error: HOME_DIR does not exist; is LABEL installed? cairn edits PATH only inside an existing LABEL home` |
| damaged | `Error: recall-first markers in PATH are damaged (B begin, E end; expected one of each, begin first); fix or delete them by hand, then re-run` |
| hand edit | `Error: the recall-first block in PATH was edited by hand since cairn wrote it; move your text outside the markers, or pass --force to replace it` |
| race | `Error: PATH changed while cairn was updating it; nothing was written. Re-run` |
| not UTF-8 | `Error: PATH is not UTF-8 text; cairn will not edit it` |
| bad ledger | `Error: LEDGER is not valid JSON (DETAIL); move it aside to reset the recall-first opt-outs` |

REASON is `cairn instructions remove` or `block deleted by hand`. LABEL is `Claude Code`
or `Codex`.

### 3.4 Targets

| Harness | Home | Target file |
|---|---|---|
| claude | `$CLAUDE_CONFIG_DIR`, else `~/.claude`. New `claude_home()` in `src/cairn/harnesses/claude.py` | `HOME/CLAUDE.md` |
| codex | `codex_home()` (`codex_models.py:28-31`) | `HOME/AGENTS.override.md` if that file exists and holds non-whitespace text, else `HOME/AGENTS.md` |

Verified (2026-10-30): Claude Code reads `CLAUDE.md` from `$CLAUDE_CONFIG_DIR` when that
variable is set. Checked on Claude Code 2.1.314: the bundled code resolves the config
directory as `process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")` and the "User"
memory file as `<config dir>/CLAUDE.md`. The public memory page
(https://code.claude.com/docs/en/memory) names `~/.claude/CLAUDE.md` and does not mention
the variable for this file, so the evidence is the shipped code.

Verified (2026-10-30): Codex loads a non-empty `AGENTS.override.md` in place of `AGENTS.md`
in its home. Checked on codex-cli 0.165.0: the home instructions loader lists
`AGENTS.override.md` before `AGENTS.md`. The public AGENTS.md guide
(https://learn.chatgpt.com/docs/agent-configuration/agents-md, redirected from
developers.openai.com/codex/guides/agents-md) says that in the Codex home "Codex reads
`AGENTS.override.md` if it exists", otherwise `AGENTS.md`, and that it "uses only the first
non-empty file" at that level. cairn treats an override that holds only whitespace as
empty.

Antigravity: deferred. cairn uses agy as a peer that it starts headless
(`src/cairn/harnesses/__init__.py:7-9`). The cairn plugin skills that the block names are
installed for Claude and Codex only (`references/harnesses/claude.md:60`,
`references/harnesses/codex.md:51`). No module in `src/cairn/harnesses/antigravity*.py`
names a global rules file. A block in agy would name skills that agy cannot call. Revisit
this when a global rules path is verified on a pinned agy version and agy has a recall route.

### 3.5 The block

The begin line carries the block version. `BLOCK_VERSION = 1` is a constant in
`src/cairn/instructions.py`. It does not track `cairn.__version__`, because a release must
not mark every user's block stale. The constant changes only when the text changes.

Marker lines (exact; the parser accepts an optional ` vN`, so a future version still
matches):

```
<!-- cairn:recall-first:begin v1 -->
<!-- cairn:recall-first:end -->
```

Begin regex: `^<!-- cairn:recall-first:begin(?: v(\d+))? -->$`, after a trailing `\r` is
removed and, on line 1, a BOM. End: the exact line above.

Block for `claude` (10 lines, markers included):

```
<!-- cairn:recall-first:begin v1 -->
## Past work: check cairn before you ask

cairn keeps this user's past coding sessions, notes and meetings on this machine.
- Before you ask the user about past work, a past decision or "where were we", run `/cairn:recall` with a time ("yesterday", "last week") or a topic.
- If the question involves a literal (an error string, a path, an identifier), run `/cairn:search` with `--exact`.
- If you still need to ask, ask with a hypothesis built from the results: "On Tuesday you chose X because Y. Is that still the plan?"
- Recall adds to what the user tells you. It never replaces it: when the two differ, the user is right.
- cairn manages the text between these markers. Opt out: `cairn instructions remove --harness claude`.
<!-- cairn:recall-first:end -->
```

Block for `codex`: the same bytes, with `/cairn:recall` replaced by the `$recall` skill,
`/cairn:search` replaced by the `$search` skill, and `--harness codex` in the last
bullet. The template lives in one table, `SKILL_NAMES = {"claude": ("/cairn:recall",
"/cairn:search"), "codex": ("$recall", "$search")}`, so the text exists once.

## 4. Behaviour

### install

1. Resolve the target (section 3.4). If the harness home is not a directory, exit 1 with
   the home-missing error. cairn never creates `~/.claude` or `~/.codex`.
2. Load the ledger (section 5). An unreadable ledger exits 1.
3. Take `locked(lock_path(target, "recall-first"))`.
4. Read the target as bytes. A missing file gives state `no-file`. Decode UTF-8 and keep
   a leading BOM. Find the newline style: CRLF if the first line ends in `\r\n`, else LF.
5. Scan the marker lines and classify the state:
   - `damaged`: begin count != 1, end count != 1, or end before begin. Exit 1.
   - `absent`: no markers. If the ledger says `installed` for this harness, the user
     deleted the block by hand. Record `declined` with reason `block deleted by hand`,
     then take the opt-out branch.
   - `current`: the block bytes equal the rendered block in the file's newline style.
   - `edited`: present, differs from the render, and the ledger holds a `block_sha256`
     that differs from the sha of the block on disk. With no ledger record: present,
     differs, and the begin version equals `BLOCK_VERSION`.
   - `stale`: present, differs, and the state is not `edited`.
6. Opt-out branch: if the ledger says `declined` and `--force` is absent, print the
   skipped line and exit 0. Nothing is written.
7. `current`: print the unchanged line and do not write. The mtime stays the same.
8. `edited` without `--force`: exit 1.
9. Compute the new text:
   - `no-file`: the block alone.
   - `absent`: the existing text. If it is non-empty, end it with one newline and add one
     blank line. Then append the block.
   - `stale` or `edited`: replace the lines from begin to end. Text outside the markers is
     kept byte for byte.
10. With `--dry-run`, print the diff and exit 0.
11. Write through the resolved path (`Path.resolve()`), so a symlinked `CLAUDE.md` stays a
    symlink. Read the bytes again just before `atomic_write`. If they differ from step 4,
    exit 1 with the race error. Pass `mode=` with the old mode, or 0o644 for a new file.
12. Update the ledger: `installed`, `block_version`, `block_sha256` of the block written
    (newline style ignored: CRLF is hashed as LF, so one sha covers both styles),
    `created_file` (true if this run created the file, or if it was already true), `path`
    and `updated_at`. Print the result line.

### remove

Steps 1-5 as above. `damaged` exits 1. `no-file` or `absent`: record `declined` and print
the absent line. `present` (any version, edited or not): delete the begin line through the
end line. If the line before begin is blank and the block ran to the end of the file, also
delete that one blank line. If `created_file` is true and only whitespace remains, delete
the file. Otherwise write it as in install step 11. Record `declined` with reason
`cairn instructions remove`. `--dry-run` prints the diff.

Round-trip property: for a file that ends in a newline, `install` then `remove` gives the
original bytes back. A file without a final newline gets one, and `remove` keeps it.

### show and status

`show` prints the render for the harness and touches nothing. `status` runs steps 1-5
without the lock, then prints one line per harness:
`HARNESS STATE vN PATH` (for example `claude current v1 ~/.claude/CLAUDE.md`). It also
prints the JSON shape in section 5 with `--json`.

### doctor

New `_check_recall_first(harness: str | None)` in `src/cairn/admin.py`. It reuses
`instructions.inspect()` and prints warn-only lines. `doctor_main` calls it before the
cron check, outside the `config is not None` guards, so it runs without a config. Without
`--harness`, it checks claude and codex, and it skips a harness whose home is missing.
With `--harness antigravity`, it prints nothing.

| State | Line |
|---|---|
| current | `ok   recall-first [claude] - v1 current in PATH` |
| declined | `ok   recall-first [claude] - opted out on DATE; 'cairn instructions install --harness claude --force' adds it` |
| no-file, absent | `warn recall-first [claude] - not in PATH; run 'cairn instructions install --harness claude' (or 'remove' to silence this)` |
| stale | `warn recall-first [claude] - stale (vOLD on disk, v1 shipped); run 'cairn instructions install --harness claude'` |
| edited | `warn recall-first [claude] - edited by hand inside the markers; edits there are replaced by 'install --force'` |
| damaged | `warn recall-first [claude] - damaged markers in PATH; fix them by hand` |
| home missing, `--harness` given | `warn recall-first [claude] - HOME_DIR not found` |

A codex line adds `(AGENTS.override.md is the active file)` when the override is the target.

## 5. Data formats

Ledger: `state_home() / "cairn" / "recall-first.json"`. The ledger is state, not config.
`cairn.config.json` rejects unknown keys, and a user opt-out is not a setting the user
types by hand. It sits beside the routine state (`src/cairn/config.py:834`). The writer
uses `json.dumps(..., indent=2, sort_keys=True) + "\n"` through `atomic_write`.

```json
{
  "schema_version": 1,
  "harnesses": {
    "claude": {
      "block_sha256": "<64 hex>",
      "block_version": 1,
      "created_file": false,
      "path": "/tmp/h/.claude/CLAUDE.md",
      "reason": null,
      "status": "installed",
      "updated_at": "2026-10-30T12:00:00Z"
    }
  }
}
```

`status` is `installed` or `declined`. `reason` is null, `cairn instructions remove` or
`block deleted by hand`. An opt-out is per harness, not per path, so it survives a Codex
target move to `AGENTS.override.md`. An unknown `schema_version` fails loudly with the
bad-ledger message.

`--json` result for install and remove, and one element of the `status --json` array:

```json
{"harness": "claude", "path": "/tmp/h/.claude/CLAUDE.md", "state_before": "absent",
 "state_after": "current", "action": "added", "block_version": 1, "dry_run": false}
```

`action` is one of `added`, `created`, `updated`, `unchanged`, `skipped`, `removed`,
`absent`, `none` (status). On exit 1, `--json` prints
`{"harness": ..., "path": ..., "error": "<message>"}` to stdout, and the plain message
goes to stderr.

## 6. Failure modes

| Failure | Handling |
|---|---|
| Symlinked `CLAUDE.md` (dotfiles) | The write goes through the resolved path, so the link survives. A dangling link resolves to a path whose parent may be missing, which gives an OS error and exit 1 |
| Mode 0600 after `atomic_write` | New `mode: int \| None = None` keyword on `atomic_write`, applied with `os.fchmod` before `os.replace` |
| An editor saves during the write | The flock covers other cairn runs only. The bytes are read again before the replace, and the race error exits 1 |
| CRLF or BOM file | Markers match without `\r` and BOM. The block is written in the file's newline style. The BOM is kept |
| A marker string pasted elsewhere in the file | It counts as a marker line, so the state is `damaged`. The user fixes it by hand |
| The user deletes the block, and a later install runs | Detected as a hand removal, recorded as `declined`, not re-added. `status` and doctor show it as an opt-out |
| The user edits the text inside the markers | `edited`. install refuses without `--force`, and doctor warns |
| Codex override appears after an install | install writes the new target and prints `note: a recall-first block is still in OLD_PATH, which Codex no longer loads; delete it there by hand, or 'cairn instructions remove --harness codex' removes both copies and records the opt-out`. remove clears the target and the recorded old path |
| Harness not installed | Exit 1 on install. Doctor skips the harness |
| Real home touched by tests | Every test runs under the `StoreFixture` environment (`tests/test_shared_store.py:67-78`) |

## 7. Tests

New `tests/test_instructions.py`, built on `StoreFixture`. Targets are under
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` in the temp root.

- `test_render_is_byte_stable_and_at_most_twelve_lines`: two renders are equal, the block is 12 lines or fewer, and each harness names its own skills.
- `test_block_text_is_pinned_to_block_version`: a sha256 per harness is pinned with `BLOCK_VERSION`, so a text change without a version bump fails.
- `test_install_appends_after_one_blank_line`
- `test_install_twice_writes_once_and_keeps_mtime`
- `test_install_then_remove_restores_original_bytes`
- `test_install_creates_missing_file_and_says_so`
- `test_remove_deletes_file_cairn_created_when_only_whitespace_remains`
- `test_remove_keeps_file_cairn_did_not_create`
- `test_missing_harness_home_refuses_with_exit_1`
- `test_stale_block_is_upgraded_and_outside_text_kept`
- `test_hand_edited_block_refused_without_force_and_replaced_with_force`
- `test_damaged_markers_refused`: duplicate begin, orphan end, end before begin.
- `test_remove_records_opt_out_and_install_skips`
- `test_hand_deleted_block_becomes_opt_out`
- `test_force_overrides_opt_out`
- `test_dry_run_prints_unified_diff_and_changes_neither_file_nor_ledger`
- `test_crlf_and_bom_preserved`
- `test_symlink_target_written_through_link`
- `test_file_mode_preserved`
- `test_codex_nonempty_override_is_target_and_empty_override_is_not`
- `test_claude_config_dir_and_codex_home_respected`
- `test_concurrent_change_aborts_without_writing`
- `test_non_utf8_file_refused`
- `test_corrupt_or_unknown_schema_ledger_fails_loud`
- `test_show_prints_exact_block`
- `test_status_json_shape`
- `test_antigravity_harness_is_a_usage_error`

Elsewhere:

- `tests/test_storage.py::test_atomic_write_applies_mode`
- `tests/test_admin_config.py::test_doctor_reports_recall_first_states`: current, absent, stale, edited, declined and damaged lines.
- `tests/test_admin_config.py::test_doctor_recall_first_runs_without_config`

## 8. Docs to update

- `docs/recall-first-block-spec.md`: this spec, in the style of `docs/routine-spec.md`, with the verified harness versions.
- `src/cairn/__main__.py` `_HELP`: one line (section 3.1).
- `README.md`: a short "Recall-first instructions" paragraph after "Keeping it fresh" (`README.md:408`). Add `cairn instructions remove --harness H` to "Disable or remove" (`README.md:143-158`).
- `skills/setup/SKILL.md`: step 2 gets a yes/no question, "Add a 10-line recall-first note to your user instruction file?" Step 6 runs `cairn instructions install --harness HOST --dry-run`, shows the diff, then installs on yes or runs `remove` on no, which records the opt-out.
- `skills/setup/references/updating.md` step 3: `cairn instructions status --harness HOST`, then `install` for a stale block only. An absent block gets setup's separate question first; an opt-out is kept.
- `skills/cairn/SKILL.md` "Top-level commands" (`:87-101`): one line.
- `references/harnesses/claude.md` and `references/harnesses/codex.md`: one paragraph each on the target file, the Codex override rule and how to opt out.
- CHANGELOG and version: the orchestrator writes them.

## 9. Implementation plan

1. `src/cairn/storage.py`: add the `mode` keyword to `atomic_write`, plus a test (+8 lines).
2. `src/cairn/harnesses/claude.py`: add `claude_home()` (+6). Optionally point `codex.py:333` and `codex_registration.py:18` at `codex_home()`. Recommendation: leave them, to stay clear of layer 03.
3. `src/cairn/instructions.py` (new, about 260 lines): `target_path()`, `SKILL_NAMES`, `BLOCK_VERSION`, `render()`, `scan()`, `inspect()`, `install()`, `remove()`, the ledger functions, `main()` with argparse.
4. `src/cairn/__main__.py`: the `_ADMIN` entry and the help line (+2).
5. `src/cairn/admin.py`: `_check_recall_first` and the call in `doctor_main` (+40).
6. `tests/test_instructions.py` (about 330 lines), plus the doctor tests (+50).
7. The docs in section 8 (about 120 lines across the files).
8. `make check`. Save the test output to a file and read it, then commit.

Estimate: about 4 hours with the tests, if the two harness facts hold (both do, section 3.4). Add
1 hour if Codex override handling needs a live check.

## 10. Open questions

1. Repeat the rule every turn through a UserPromptSubmit hook? The comparable tool found
   that a file block alone was not followed on some hosts and added a per-turn line.
   Recommended default: no, not in this layer. A per-turn hook adds latency to every
   prompt. First measure how often recall runs with the block in place.
2. Should `cairn hook install` print a hint when the block is absent and not declined?
   Recommended default: no. Doctor already reports it, and layer 03 owns that code path.
3. Should setup install the block by default, or ask? Recommended default: ask, with the
   diff shown, because the file is the user's own.

## 11. Implementation notes

Where the implementation departs from sections 1-10:

- The lock is one per-user lock on the ledger (`lock_path(ledger, "recall-first")`), not one
  per target file. Every run reads and writes the shared ledger, so one lock covers both.
- A ledger record carries one more key, `old_target` (`null` or `{"path", "created_file"}`).
  It keeps the previous Codex target after an install moves to `AGENTS.override.md`, so
  `remove` can still clear the old copy. `created_file` is per path: it does not carry over
  to a new target.
- A block deleted by hand is detected only when the ledger `path` equals the current target.
  A moved Codex target is not an opt-out.
- Doctor shows a hand-deleted block that install has not yet recorded as
  `ok   recall-first [claude] - opted out (block deleted by hand); '... --force' adds it`.
- `status` prints `-` in the version column when the file holds no block, and exits 1 with the
  home-missing error when `--harness` names a harness whose home does not exist. `install
  --dry-run --json` adds a `diff` key to the JSON result.
- `atomic_write` gains opt-in keywords only: `mode`, `follow_symlinks` (write through the
  resolved target; its parent must exist) and `check_unchanged` with `expected` (read the
  file again just before `os.replace` and raise `ConcurrentChangeError` if it changed).
  Existing callers keep the old behaviour.
- The `note:` line for an old Codex copy goes to stderr, like other cairn warnings.
- `old_target` stays in the record until `remove` clears that copy. A decline (by `remove` or
  by a hand deletion) keeps it. An old copy that cairn cannot read, or whose markers are
  damaged, is named on stderr (`note: cannot check OLD for an old recall-first block: ...`),
  and its receipt is kept for the next `remove`.
- `remove --dry-run` previews every file the run would change: the target and an old Codex
  copy, one diff after the other.
- `install` on a `current` block writes no file but makes the receipt agree with it: a run
  whose ledger write failed, `--force` over an opt-out when the block is back, a moved Codex
  target. The instruction file's mtime stays the same.
- `created_file` counts only for the record's own path while the record is `installed`, and a
  decline clears it once that file is gone. A file the user makes later is never deleted.
- The ledger loader checks every record field it reads (types, `status` value, the nested
  `old_target`) and fails with the bad-ledger message, for example
  `(harnesses.codex.status is not one of installed, declined)`.
- I/O failures exit 1 with the JSON error shape: `Error: cannot take the recall-first lock: ...`,
  and `Error: cannot write LEDGER: ...; PATH was already changed, and a re-run records it`
  when the instruction file was written but the ledger was not. The re-run reconciles.
- A Codex override is empty when its text, after a BOM, is only whitespace (Unicode aware).
  An override that exists but cannot be read exits 1 with `Error: cannot read PATH: ...`.
- An old copy exists only inside one Codex home: the other file of the `AGENTS.md` and
  `AGENTS.override.md` pair. A changed `CLAUDE_CONFIG_DIR` or `CODEX_HOME` is another
  profile, and cairn neither names nor edits the file there.
- The STATE column of `status` shows `declined` for an opt-out, including a block deleted by
  hand that install has not recorded yet, as doctor does. `status --json` keeps the file
  state. With no `--harness` and no home, `status` prints nothing on stdout and
  `no Claude Code or Codex home found; nothing to report` on stderr, exit 0.
- `--force` over a hand edit prints `updated: hand edits in the recall-first block in PATH
  replaced with v1`. A begin line without a version upgrades as `from an unversioned block`.
- `remove --dry-run` on a file without a block prints `absent: no recall-first block in PATH;
  --dry-run recorded nothing`. `remove --dry-run --json` adds a `diff` key when a file
  changes, as install does.
- A diff line without a final newline is followed by `\ No newline at end of file`.
