# Layer 14: test discipline (tooling and docs)

Status: implemented in this PR (branch `shav/mem-14-test-discipline`), on top of the
2.19.3 stack. Section 11 lists where the implementation differs from the text below; where
they differ, section 11 wins.

Every claim about current behaviour cites `path:line` on `main` at 2.9.0 (f0ea1e1).

**Dependencies.** Layer 03 (codex-trusted-hash): gives `cairn hook install
--harness codex` a writer for the Codex trusted-hash table, a per-entry sha256 install
receipt, `--dry-run`, and an uninstall that removes only unchanged owned entries
(`tests/test_codex_trust.py`). The filled evidence example in section 5.3 describes that
output. Layers 01-13: each ships
`docs/<slug>-spec.md`, and layer 10 ships the generated `docs/error-codes.md`. The index
in `docs/README.md` must list all of them. Layer 12 (docs-cli-truth): its test parses every
fenced `cairn ...` command in docs, so the new docs keep fenced `cairn` commands valid.
No other layer is touched.

## 1. Purpose and non-goals

### Purpose

A test that passes with and without its fix pins nothing. A comparable project audited
its new tests and found that most of them passed on the baseline, without the fix they
claimed to cover. It now requires a discrimination check before it accepts a test. This layer
gives cairn the same check, in five parts:

1. `scripts/check-test-discriminates.py` and `make discriminates TEST=...`: run one test
   against a copy of the tree with the fix reverted (must fail), then against the real tree
   (must pass), and print a one-line evidence string for the PR body.
2. A PR template line and a CONTRIBUTING section, "A new test must fail without its fix",
   plus the rule "save test output to a file and read it with the real exit code".
3. A dated two-column harness evidence table: what tests prove, and what only a person
   with a real host can prove.
4. `docs/README.md`: a dated index of every document under `docs/`, newest first, one
   decision line each, negative results and superseded plans included.
5. Two short rules in the repository `CLAUDE.md`.

### Non-goals

- No runtime change under `src/cairn/`, `skills/`, `hooks/`, `references/` or any plugin
  manifest. The version-bump job keys on those globs (`.github/workflows/ci.yml:57-59`),
  so this layer needs no bump and must not touch them.
- CI does not run the discrimination script. It cannot know which hunk a test pins. The
  PR template line is the gate, and a reviewer reads it.
- No mutation-testing framework, no coverage tool, no new dependency.
- No change to how `make check` runs the suite.

## 2. Current behaviour

| Area | Fact | Cite |
|---|---|---|
| Gate | `make check` = `lint typecheck test`; lint runs ruff check and ruff format --check on `src tests` only | `Makefile:22`, `Makefile:8-10` |
| Test run | `uv run python -m unittest discover -s tests` | `Makefile:19-20`, `.github/workflows/ci.yml:31` |
| Test ids | `tests/` is a package (empty `tests/__init__.py`, 0 lines); tests import each other as `tests.test_*` | `tests/test_catalog.py:19` |
| Repo-relative tests | some tests resolve files from `Path(__file__).parents[1]` and list tracked files with `git ls-files` | `tests/test_hooks.py:217`, `tests/test_public_hygiene.py:18`, `tests/test_public_hygiene.py:42-43` |
| mypy scope | `files = ["src", "tests"]` | `pyproject.toml:51` |
| scripts/ | one file, `scripts/sync_plugin_manifest.py`; not linted; `ruff format --check scripts` reports it would reformat it (run 2026-10-30) | `Makefile:9-10` |
| CI | ruff, format, mypy, unittest on 3.11-3.13, then a check that the runner home has no `~/.local/state/cairn` or `~/.cache/cairn` | `.github/workflows/ci.yml:21-38` |
| Bump globs | `src/cairn/**`, `skills/**`, `hooks/**`, `.claude-plugin/**`, `.codex-plugin/**`, `plugin.json`, `.agents/plugins/**`, `references/**`, `commands/**`, `agents/**` | `.github/workflows/ci.yml:57-59` |
| PR template | tracked as `.github/PULL_REQUEST_TEMPLATE.md` (upper case); checklist has five lines, none about test evidence | `.github/PULL_REQUEST_TEMPLATE.md:11-17` |
| CONTRIBUTING | "Before you open a PR" then "Public-repo hygiene"; nothing about test quality or test output | `CONTRIBUTING.md:23-33`, `CONTRIBUTING.md:35-42` |
| CLAUDE.md | two sections: "Public-repo hygiene" and "Gate" | `CLAUDE.md:6-25`, `CLAUDE.md:27-30` |
| docs/ | 11 files, no index; README calls the folder "design plans, research and acceptance evidence" | `README.md:544` |
| Codex evidence | one-column "Evidence / result" table; trust row says the hook was trusted by hand in the TUI | `docs/codex-acceptance.md:40-47` |
| Codex gaps | desktop, real cron, cold model download, Linux unverified | `docs/codex-acceptance.md:175-191` |
| Trust today | `cairn hook status` says trust is "unverified"; for Codex it adds `host_hooks` from the client and points at `trustStatus` | `src/cairn/hooks.py:316`, `src/cairn/hooks.py:483-489` |
| Registration receipt | `codex-hook-registration.json` next to the user config | `src/cairn/harnesses/codex_registration.py:17-19` |
| Harness refs | agents read `references/harnesses/<host>.md` at run time | `references/harnesses/contract.md:3-5` |

## 3. Surface

### 3.1 Script

```
uv run python scripts/check-test-discriminates.py TARGET
    [--since REF] [--files PATH [PATH ...]] [--timeout SECONDS]
    [--log-dir DIR] [--keep] [--json]
```

| Argument | Meaning | Default |
|---|---|---|
| `TARGET` | a unittest name: module `tests.test_x`, class `tests.test_x.TestY` or method `tests.test_x.TestY.test_z` | required |
| `--since REF` | the pre-fix commit. Every reverted file takes its content at REF | `git merge-base HEAD main`, else `git merge-base HEAD origin/main` |
| `--files PATH...` | the files to revert, repo-relative (`..` is resolved first; an absolute path must lie in the repository). Any path outside `tests/` whose parent directories are not symlinks | every path under `src/` that differs between REF and the working tree, plus untracked files under `src/` |
| `--timeout` | seconds per test run | 600 |
| `--log-dir` | where the two run logs go, relative to the invocation directory; created if absent; never deleted | a new `cairn-discriminate-logs-*` directory under the system temp dir |
| `--keep` | keep the reverted copy for inspection; its path goes to stderr | off |
| `--json` | print the JSON object of section 5.1 instead of the evidence line | off |

`--since` and `--files` combine: `--files` picks the paths, `--since` picks the content.
This departs from the brief's "either/or" because a file list still needs a ref to revert
to. On a stacked branch the merge-base with `main` also reverts the lower layers, so
the author passes the parent branch: `--since shav/mem-13-recall-qrels`.

### 3.2 Make target

```make
discriminates:
	@test -n "$(TEST)" || { echo "usage: make discriminates TEST=tests.test_x.TestY.test_z [SINCE=REF] [FILES='src/cairn/x.py ...']" >&2; exit 2; }
	uv run python scripts/check-test-discriminates.py $(TEST) $(if $(SINCE),--since $(SINCE)) $(if $(FILES),--files $(FILES))
```

`discriminates` joins `.PHONY` (`Makefile:1`).

### 3.3 Exit codes and messages

All diagnostics go to stderr with the prefix `check-test-discriminates: `. Stdout holds
only the evidence line (or the JSON object), and it is printed for every verdict, so a
failed check is also recordable.

| Exit | Verdict | stderr line |
|---|---|---|
| 0 | discriminates | `DISCRIMINATES: TARGET fails with the fix reverted and passes with it` |
| 1 | does_not_discriminate | `DOES NOT DISCRIMINATE: TARGET passes with the fix reverted; tighten its assertions` |
| 2 | usage or setup error | one of the `error:` lines below |
| 3 | vacuous | `VACUOUS: TARGET did not run on the reverted tree (REASON); a load error or zero executed tests is not a failing assertion` |
| 4 | fix_fails | `FIX FAILS: TARGET fails on the current tree; fix the test or the code first` |
| 130 | interrupted | `interrupted; the working tree was not modified` |

Exit-2 messages, exact:

- `error: not inside a git work tree`
- `error: no ref 'main' or 'origin/main' to take the merge-base with; pass --since REF`
- `error: unknown ref 'REF'`
- `error: nothing to revert: no file under src/ differs from REF (SHORT)`
- `error: PATH is under tests/; --files takes source files only`
- `error: PATH is outside the repository`
- `error: PATH has a symlinked parent directory (PREFIX); pass the real path`
- `error: PATH resolves outside the copy; nothing was written there`
- `error: PATH is identical at REF (SHORT); nothing to revert`
- `error: package 'PKG' imports from PATH, not from the reverted copy; the run would test the fix`
- `error: import of 'PKG' timed out after N s on the reverted tree`
- `error: test run timed out after N s on the PHASE tree; log: LOG`

REASON in exit 3 is `load error: <first line of the loader exception>`, `0 tests ran`, or
`all N tests skipped`.

## 4. Behaviour, step by step

### 4.1 Design choice: copy, never touch the real tree

| | A: revert in place (`git diff REF -- src/ \| git apply -R`, run, re-apply) | B: temporary worktree at REF, test files copied in | Chosen: snapshot copy of the working tree, revert inside the copy |
|---|---|---|---|
| Writes to the real tree | yes, every reverted file, twice | no files; writes `.git/worktrees/` metadata | none |
| SIGKILL, power loss or crash mid-run | the tree is left reverted; uncommitted fix work survives only in a backup the user must find | stale worktree, recoverable with `git worktree prune`; no work lost | a stale temp dir; no work lost |
| An editor, agent or second session in the same tree during the run | sees and may save over reverted code | unaffected | unaffected |
| Uncommitted fix | must be stashed or diffed against the index; `git apply -R` fails on a partly staged hunk | the worktree has none of it; would need a second overlay step | included: the copy takes the working tree as it is |
| Non-src changes (skills, hooks, test helpers, fixtures) | kept | reverted to REF as well, which over-reverts | kept |
| Repo-relative tests (`git ls-files`) | work | work | work after `git init` + `git add -A` in the copy |

A cannot lose work only if nothing ever kills it between revert and restore. B cannot
lose work but reverts too much and drops uncommitted fixes. The chosen design is B's
safety property with A's precision: the script only reads the repository (`git ls-files`,
`git diff`, `git show`, `git cat-file`, `git ls-tree`, `git merge-base`, `git rev-parse`)
and writes only under the temp dir and the log dir. "Restore" is therefore not a step: the
real tree never changed, and the second run happens there.

### 4.2 Steps

1. Find the repo root with `git rev-parse --show-toplevel`. Exit 2 on failure.
2. Resolve REF: `--since` through `git rev-parse --verify REF^{commit}`, else the
   merge-base of HEAD with `main`, then `origin/main`. SHORT is `git rev-parse --short=7`.
3. Build the file list. Default: `git diff --no-renames --name-status REF -- src/`
   (REF against the working tree, so committed and uncommitted changes both count) plus
   `git ls-files --others --exclude-standard -- src/`. With `--files`, reject any path under
   `tests/`, and reject a path with no difference from REF. Exit 2 if the list is empty.
4. Count lines per file: `git diff --no-renames --numstat REF -- PATH`; an untracked file
   counts its lines as added; a binary file prints `(binary)`.
5. Snapshot: copy every path from `git ls-files -z --cached --others --exclude-standard`
   that exists into `<tmp>/tree` (`shutil.copy2`, symlinks kept as symlinks). Then run
   `git init -q` and `git add -A` inside the copy, so tests that list tracked files see the
   same set.
6. Revert inside the copy only. A path present at REF gets `git show REF:PATH` bytes and
   the REF mode from `git ls-tree REF -- PATH` (`100755` sets the exec bit, which matters
   for `hooks/launch.sh`). A path absent at REF is deleted.
7. Guard the import path. For each top-level package `PKG` under a reverted `src/PKG/...`,
   run `python -c "import PKG; print(PKG.__file__)"` with the copy's environment. Exit 2
   if the file is not under `<tmp>/tree/src`. This catches an editable install that wins
   over `PYTHONPATH`.
8. Reverted run: `sys.executable -c RUNNER TARGET OUT.json` with `cwd=<tmp>/tree`,
   `PYTHONPATH=<tmp>/tree/src` prepended, and stdout and stderr written to
   `<log-dir>/reverted.log`. RUNNER is a module constant. It loads TARGET with
   `unittest.defaultTestLoader.loadTestsFromName`, runs it with `TextTestRunner(verbosity=2)`,
   and writes `{"run", "failures", "errors", "skipped", "load_errors"}` to OUT.json.
   `run` counts test methods. `skipped` counts test methods in which nothing ran (every
   part skipped), so a method with one skipped subtest and one run subtest is not skipped.
   `load_errors` holds `loader.errors` plus any `unittest.loader._FailedTest` ids.
9. Current-tree run: the same command with `cwd=<repo root>`, `PYTHONPATH=<repo>/src`,
   output to `<log-dir>/restored.log`.
10. Delete `<tmp>` unless `--keep` is set (in a `finally`, which also runs on Ctrl-C).
11. Decide, in this order:
    - The current-tree run has `load_errors`, failures or errors: exit 4.
    - The reverted run has `load_errors`, `run == 0`, or `run == skipped`: exit 3.
    - The reverted run has `failures + errors >= 1`: exit 0.
    - Otherwise: exit 1.
12. Print the evidence line (or JSON) on stdout and the verdict line on stderr, followed by
    `logs: <log-dir>`.

The run uses `sys.executable`, so `make discriminates` (through `uv run`) tests with the
same interpreter as `make test`.

## 5. Data formats

### 5.1 Evidence line and JSON

```
reverted src/cairn/x.py (+12 -3) to f0ea1e1, ran tests.test_x.TestY.test_z -> FAIL (1), restored -> PASS
```

Several files are space-separated: `reverted src/cairn/a.py (+12 -3) src/cairn/b.py
(+4 -0) to f0ea1e1, ...`. The reverted result is `FAIL (N)` with N = failures + errors,
`PASS`, or `NOT RUN (REASON)`. The restored result is `PASS` or `FAIL (N)`. The line holds
only repo-relative paths, so it is safe for a public PR body. `to SHORT` is an addition to
the brief's shape: a reviewer needs the ref to repeat the check.

```json
{"schema_version": 1, "verdict": "discriminates", "exit": 0,
 "target": "tests.test_x.TestY.test_z", "ref": "f0ea1e1<full sha>", "ref_short": "f0ea1e1",
 "files": [{"path": "src/cairn/x.py", "added": 12, "deleted": 3, "state": "modified"}],
 "reverted": {"run": 1, "failures": 1, "errors": 0, "skipped": 0, "load_errors": [],
              "seconds": 0.41, "log": "<log-dir>/reverted.log"},
 "restored": {"run": 1, "failures": 0, "errors": 0, "skipped": 0, "load_errors": [],
              "seconds": 0.38, "log": "<log-dir>/restored.log"},
 "evidence": "reverted src/cairn/x.py (+12 -3) to f0ea1e1, ran ... -> FAIL (1), restored -> PASS"}
```

`state` is `modified`, `added` (absent at REF, deleted in the copy), `deleted` (present at
REF, restored in the copy) or `untracked`.

### 5.2 Harness evidence table: convention and template

Each harness acceptance document under `docs/` (today `docs/codex-acceptance.md`, later
`docs/<harness>-acceptance.md`) opens with a dated evidence table. A row is one behaviour.
The automated column names a test id and the date and commit it last passed. The manual
column names what a real host must show, who checks it, and where the result is written.
A manual item moves to the automated column only when a test covers it. It is never
deleted. The table does not go into `references/harnesses/*.md`: those files ship in the
plugin (`.github/workflows/ci.yml:58-59`, a version bump) and agents read them at run time
(`references/harnesses/contract.md:3-5`).

```markdown
## Evidence (YYYY-MM-DD, cairn X.Y.Z, commit SHORT)

| Behaviour | Automated evidence (test name, date) | Remaining manual evidence (what, who, how to record) |
| --- | --- | --- |
| <one behaviour> | `tests.test_x.TestY.test_z`, YYYY-MM-DD, SHORT | What: <what a real host must show>. Who: <owner / any maintainer>. How: <the row to add under "Manual evidence log": date, host version, session id prefix, the output excerpt>. |

### Manual evidence log

| Date | Host and version | Item | Result | Excerpt |
| --- | --- | --- | --- | --- |
```

### 5.3 Filled example: Codex hook trust (layer 03)

Layer 03 makes `cairn hook install --harness codex` write three things: the Cairn-owned
SessionEnd group in `$CODEX_HOME/hooks.json`, a `trusted_hash` table for that group in
Codex's hook trust state, and an install receipt with one sha256 per owned entry. Tests
can prove that cairn writes the hash it computes and removes only what it owns. Only a real
Codex can prove that Codex computes the same hash and skips the review prompt.

The filled table is the "Evidence (2026-10-30, cairn 2.19.3, commit e29f161)" section at
the top of `docs/codex-acceptance.md`. Its rows name the real test ids from
`tests/test_codex_trust.py`, for example
`tests.test_codex_trust.TestInstall.test_install_writes_group_then_table_then_receipt`
and `tests.test_codex_trust.TestUninstall.test_dry_run_writes_nothing`, and its manual
log holds the read-only hash check on Codex CLI 0.165.0.

### 5.4 `docs/README.md`

The index lists every file under `docs/`, newest first, as `| Date | Document | Kind |
Decision |`. The date is the date the document states for its research or observation,
else its first commit. The decision line comes from the document's Status line or its
implementation notes, and it names rejected proposals, negative results and superseded
plans. The shipped file is the reference; `tests/test_test_discipline.py` keeps it
complete.

## 6. Failure modes

| Case | Result |
|---|---|
| Test imports a name that does not exist at REF | exit 3, `NOT RUN (load error: ImportError: ...)`. See open question 1. |
| Test passes on both trees | exit 1, line printed, PR must not claim discrimination |
| Test fails on the current tree | exit 4; the reverted result is still reported |
| Editable install shadows `PYTHONPATH` | exit 2 from step 7, before any run |
| A test leaks into `~/.local/state/cairn` | not this script's job; `make check` and CI still catch it (`.github/workflows/ci.yml:34-38`) |
| Ctrl-C | temp copy removed, logs kept, exit 130, real tree untouched |
| SIGKILL | a stale `cairn-discriminate-*` temp dir; real tree untouched |
| Disk full during copy | `OSError` names the path, exit 2 |
| Stacked branch with the default ref | lower layers are reverted too; the line shows every reverted file, so a reviewer sees it. CONTRIBUTING tells the author to pass `SINCE=` |
| Hooks or skills are the fix | `--files hooks/launch.sh`; the exec bit comes from REF |

## 7. Tests

`tests/test_check_test_discriminates.py` runs the script as a subprocess, with the same
interpreter, against a temp git repo. The repo holds `src/demo/__init__.py`, `tests/__init__.py` and `tests/test_demo.py`,
uses invented identity `x@y.z`, and sets `HOME` to a temp dir. Skip if `git` is absent.

| Test | Asserts |
|---|---|
| `test_discriminating_test_exits_0_with_evidence_line` | exit 0, stdout equals the exact line with `(+N -M)` and the short ref |
| `test_test_passing_both_ways_exits_1` | exit 1, `-> PASS, restored -> PASS` |
| `test_import_error_on_reverted_tree_exits_3` | exit 3, `NOT RUN (load error: ImportError` |
| `test_all_skipped_exits_3` | exit 3, `all 1 tests skipped` |
| `test_failing_on_current_tree_exits_4` | exit 4 |
| `test_real_tree_bytes_unchanged` | sha256 of every file, including an uncommitted edit and an untracked file, is equal before and after |
| `test_uncommitted_fix_is_reverted` | fix left unstaged still gives exit 0 |
| `test_untracked_source_file_is_removed_in_copy` | `--keep` copy lacks the file; state `untracked` |
| `test_exec_bit_restored_from_ref` | a `100755` file at REF is executable in the kept copy |
| `test_files_under_tests_rejected` | exit 2, exact message |
| `test_nothing_to_revert_exits_2` | exit 2, exact message |
| `test_unknown_ref_exits_2` | exit 2, exact message |
| `test_no_main_and_no_since_exits_2` | exit 2, exact message |
| `test_shadowed_import_exits_2` | an installed `.pth` pointing at the real `src` gives exit 2 |
| `test_json_shape` | keys and types of section 5.1 |
| `test_temp_copy_removed_and_logs_kept` | copy path gone, both logs exist |
| `test_multiple_files_in_line` | two files, space-separated, in path order |
| `test_symlinked_parent_rejected_and_target_kept` | `--files` through a symlinked directory: exit 2, exact message, the link target survives |
| `test_git_env_does_not_touch_real_index` | with `GIT_INDEX_FILE` or `GIT_DIR` pointing at the real repo, its index is byte-identical after a run |
| `test_dotdot_paths_normalised` | `src/demo/../demo/x.py` reverts `src/demo/x.py`; `src/../tests/...` and `../x` are refused |
| `test_relative_log_dir_is_taken_from_the_invocation_dir` | `--log-dir ../rel-logs` writes both logs there |
| `test_timeout_kills_the_test_process_group` | exit 2, exact message, a grandchild process is gone, the real tree and temp dir are clean |
| `test_hanging_import_probe_times_out` | an import that hangs at REF gives exit 2 within `--timeout` |
| `test_skipped_subtests_on_current_tree_exit_3` | every subtest skipped on the current tree: `restored -> NOT RUN (all 1 tests skipped)` |
| `test_one_skipped_subtest_beside_a_run_one_counts_as_run` | a skipped subtest next to a failing one: exit 0, `skipped` 0 |
| `test_zero_tests_exit_3` | an empty test class: `NOT RUN (0 tests ran)` |
| `test_unexpected_success_counts_as_failure` | an `expectedFailure` test that passes on the current tree: exit 4 |

`tests/test_test_discipline.py`:

| Test | Asserts |
|---|---|
| `test_docs_index_lists_every_doc` | every `docs/**/*.md` except `README.md` is linked from `docs/README.md` |
| `test_docs_index_links_resolve` | every relative link target exists |
| `test_docs_index_newest_first` | the Date column is ISO and non-increasing |
| `test_pr_template_asks_for_discrimination_evidence` | `.github/PULL_REQUEST_TEMPLATE.md` holds `Discrimination evidence:` |
| `test_claude_md_states_both_rules` | `CLAUDE.md` holds "never pipe" and "discrimination evidence" |
| `test_makefile_has_discriminates_target` | `discriminates:` exists and is in `.PHONY` |
| `test_acceptance_docs_open_with_evidence_table` | in each `docs/*-acceptance.md` the first `##` section is the dated evidence heading with the three-column header and the manual log; every automated cell is `none` or a test id, a date and a commit, and the test id loads as one test |

The script's own tests cannot be checked by the script: reverting a new file gives exit 3.
Their evidence in the PR body is one mutation probe: make the verdict function always
return 0, run `tests.test_check_test_discriminates`, record the failing count, then revert.

## 8. Docs to update

| File | Change |
|---|---|
| `.github/PULL_REQUEST_TEMPLATE.md` | add after line 14: `- [ ] Discrimination evidence for each new test: \`<paste the line from make discriminates>\`` (edit the upper-case file; a lower-case copy would be a second template on Linux) |
| `CONTRIBUTING.md` | new section 8.1 after "Before you open a PR" (`CONTRIBUTING.md:33`) |
| `CLAUDE.md` | new "Tests" section (8.2) |
| `docs/README.md` | new, section 5.4 |
| `docs/codex-acceptance.md` | new "Evidence (2026-10-30 ...)" section after the intro (line 6), section 5.3, plus a pointer that the older tables below are the 2.4.0 record |
| `docs/test-discipline-spec.md` | this spec |
| `README.md:544` | `docs/  # design plans, research and acceptance evidence (index: docs/README.md)` |

### 8.1 CONTRIBUTING section

`CONTRIBUTING.md`, "A new test must fail without its fix", with the subsections "Save test
output to a file" and "Recording harness evidence". It sits after "Before you open a PR"
and before "Documented commands must parse".

### 8.2 CLAUDE.md placement

A "Tests" section after "Gate", in the same imperative bullet style: save test output to a
file and read the real exit code, never pipe a test run through `head` or `tail`; a new
test comes with its discrimination evidence.

## 9. Implementation plan

| # | Step | Files | Lines |
|---|---|---|---|
| 1 | Write the script: argument parsing, ref and file resolution, snapshot copy, revert, import guard, two runs, verdict, line and JSON output | `scripts/check-test-discriminates.py` | ~280 |
| 2 | Add the make target; lint the new script without reformatting `sync_plugin_manifest.py`: `ruff check src tests scripts/check-test-discriminates.py` and the same for `ruff format --check` | `Makefile`, `.github/workflows/ci.yml:28-29` | +10 |
| 3 | Add the script to mypy (`mypy` accepts the hyphenated file name; checked on a probe file on 2026-10-30) | `pyproject.toml:51` | +1 |
| 4 | Script tests, then the mutation probe for the PR body | `tests/test_check_test_discriminates.py` | ~240 |
| 5 | PR template line, CONTRIBUTING section, CLAUDE.md section | three files | +95 |
| 6 | `docs/README.md` from section 5.4, with the rows of layers 01-13 filled from their Status lines | new | ~45 |
| 7 | Evidence table in `docs/codex-acceptance.md`, with layer 03's real test ids | one file | +25 |
| 8 | Docs tests | `tests/test_test_discipline.py` | ~90 |
| 9 | `make check > /tmp/cairn-check.txt 2>&1; echo "exit=$?"`, then run `make discriminates` once on a test from a lower layer, as a smoke run, and paste its line into the PR | none | 0 |

Total: about 800 lines, most of them tests and docs. Estimate: half a day if layer 03's
test names are final, plus an hour to draft index rows for layers 01-13.

## 10. Open questions

1. **Does an import error on the reverted tree count as discrimination?** Recommended
   default: no. Exit 3, the line still printed, and a mutation probe in the PR body.
   Many layers in this stack add modules, so exit 3 will be common; the probe is a
   two-minute step.
2. **Evidence tables in `references/harnesses/*.md` as the brief says, or in docs only?**
   Recommended default: docs only. A change under `references/**` forces a version bump
   (`.github/workflows/ci.yml:58-59`). Agents also read those files at run time
   (`references/harnesses/contract.md:3-5`), so a dated table costs context on every run.
3. **Should CI enforce the PR template line?** Recommended default: no. CI cannot know
   which test pins which hunk, and a grep for the line proves only that it was pasted.
   The reviewer checks it.

## 11. Implementation notes

The three open questions took their recommended defaults: an import error on the
reverted tree is exit 3, the evidence tables live only in `docs/*-acceptance.md`, and CI
does not check the PR line. The code differs from the sections above in these points:

1. **`git init` after the revert.** Step 5 ran `git init -q` and `git add -A` before step
   6. The script runs them after the revert, so `git ls-files` in the copy lists the
   reverted set: a file absent at REF is not listed, and a file restored from REF is.
2. **The current tree must run too.** If no test executed on the current tree (0 ran, or
   all skipped), the verdict is exit 3 with `did not run on the current tree (REASON)`.
   Without this, an all-skipped current run read as `restored -> PASS`.
3. **Counting.** Unexpected successes count as failures. A child that exits without
   writing its result is a load error, `RunnerError: the test process exited N without a
   result`. A load error on the current tree prints `restored -> NOT RUN (load error:
   ...)` and exits 4.
4. **Paths.** Absolute paths under the copy or the repository are removed from load-error
   text, so the evidence line holds only repo-relative paths.
5. **REF in messages.** `identical at REF (SHORT)` and `differs from REF (SHORT)` print the
   ref as given (`--since` value) or `merge-base with main`, then the short sha. PHASE in
   the timeout message is `reverted` or `current`.
6. **No writes from the reads.** The script's git calls set `GIT_OPTIONAL_LOCKS=0`, so a
   `git diff` cannot refresh the real index, and both runs set
   `PYTHONDONTWRITEBYTECODE=1`. `test_real_tree_bytes_unchanged` hashes `.git` as well.
7. **`--files` with a path outside the repository** is exit 2,
   `error: PATH is outside the repository`.
8. **Lint scope.** `make lint`, `make format` and CI name `scripts/check-test-discriminates.py`
   alone; `scripts/sync_plugin_manifest.py` stays outside the linter.
9. **Tests.** `tests/test_check_test_discriminates.py` has 29 tests: the 27 of section 7
   plus `test_identical_file_exits_2` and `test_not_a_git_tree_exits_2`.
   `test_shadowed_import_exits_2` uses a `sitecustomize.py` on `PYTHONPATH` that puts the
   real `src` first, in place of an installed `.pth`. `tests/test_test_discipline.py`
   adds `test_contributing_has_the_section` to the seven of section 7, and its evidence
   table test also requires the dated heading and the manual log.
10. **Mutation probes for the script's own tests.** With `decide()` forced to return 0,
    4 of the first 19 tests failed. With `revert()` made a no-op, 9 of 19 failed. Both edits were
    undone.
11. **Review fixes (PR review, round 1).** `--files` resolves `..` before any check, so
    `src/../tests/x.py` is refused as a test path and `../x.py` as outside the repository.
    A path whose parent directory is a symlink is refused, and every write in the copy
    first checks that its parent resolves inside the copy, so a link into the real tree
    cannot be followed. The copy's `git init`/`git add`, the import probe and both runs drop
    the variables `git rev-parse --local-env-vars` lists (`GIT_DIR`, `GIT_INDEX_FILE` and
    the rest), so a run started from a git hook cannot write into the real index. The
    import probe runs under `--timeout` in its own process group. `--log-dir` is made
    absolute before the reverted run changes directory. `skipped` counts methods in which
    nothing ran, not skipped subtests.
