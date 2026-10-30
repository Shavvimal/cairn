# Contributing to cairn

Thanks for your interest in improving cairn. This project is small and pragmatic — contributions of all sizes are welcome, from typo fixes to new source adapters.

## Ground rules

- Be respectful. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- Keep changes focused. One logical change per pull request.
- Discuss large changes first by opening an issue, so we agree on direction before you invest time.

## Development setup

cairn's runtime is standard-library only; the dev tooling and the optional Granola path live behind extras.

```bash
git clone https://github.com/shavvimal/cairn
cd cairn
make install        # uv sync --extra granola
```

The editable install resolves `cairn.config.json` at the repo root via the dev fallback, so local edits are picked up without a global install.

## Before you open a PR

Run the full local gate — this mirrors CI exactly:

```bash
make check          # ruff lint + ruff format --check + mypy + unittest
```

Individual targets are available too: `make lint`, `make format`, `make typecheck`, `make test`.

CI runs the same checks across Python 3.11, 3.12, and 3.13. A PR must be green before it can merge.

## A new test must fail without its fix

A test that passes with and without its fix pins nothing, and it keeps passing after a
later change breaks the behaviour. A comparable project audited its new tests and found
that most of them passed on the baseline, without the fix they claimed to cover. cairn
therefore asks for evidence with every new or changed test.

Run the check for each new test:

```bash
make discriminates TEST=tests.test_x.TestY.test_z
```

On a stacked branch, name the parent so that only your change is reverted:

```bash
make discriminates TEST=tests.test_x.TestY.test_z SINCE=my-parent-branch
```

The script (`scripts/check-test-discriminates.py`) copies the working tree to a temporary
directory and reverts the source files there to their content at the base ref. By default
these are the files under `src/` that changed since the merge-base with `main`, and
`FILES='hooks/launch.sh'` picks others. The script runs the test in the copy and requires
it to fail. Then it runs the test in your real tree and requires it to pass. Your working
tree is never modified, so an interrupted run cannot lose work. Paste the line it prints
into the PR:

```text
reverted src/cairn/x.py (+12 -3) to f0ea1e1, ran tests.test_x.TestY.test_z -> FAIL (1), restored -> PASS
```

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | the test fails without the fix and passes with it | paste the line |
| 1 | the test passes without the fix | tighten the assertions |
| 2 | usage or setup error | read the message |
| 3 | the test did not run on one tree (import error, nothing ran, all skipped) | see below |
| 4 | the test fails with the fix | fix the test or the code |

Exit 3 usually means the test imports a name the fix adds. That proves a name changed,
not that the behaviour did. Paste the line anyway, and add one mutation probe: break the
new code on purpose (return the wrong value, drop the check), run the test, record how
many tests failed, then undo the edit. A test of unchanged behaviour (a characterisation
test) states that in place of the line.

Assertions that pass on broken code, and that review will reject:

- only `assertNotEqual(code, 0)`: a missing binary or a crash also exits non-zero;
- a substring that the broken output also contains;
- membership in a set that holds every value the code can return;
- a mock of the function under test.

### Save test output to a file

Save test output to a file and read it with the real exit code. Never pipe a test run
through `head` or `tail`:

```bash
make check > /tmp/cairn-check.txt 2>&1; echo "exit=$?"
```

A pipe reports the exit status of its last command, so `make check | tail` exits 0 when
the tests fail. `tail` also drops the start of the log, where the first error is. Read
the whole file, and report the number that `echo` printed.

### Recording harness evidence

An acceptance document for a harness (`docs/codex-acceptance.md`) opens with a dated
table of two columns. "Automated evidence (test name, date)" names the test and the date
and commit it last passed. "Remaining manual evidence (what, who, how to record)" names
what only a real host can show, who checks it, and where the result goes. A manual item
moves to the automated column when a test covers it, and is never deleted. Use the
template in `docs/test-discipline-spec.md`, section 5.2. When you add a file under
`docs/`, add its row to `docs/README.md`.

## Documented commands must parse

`tests/test_docs_cli.py` reads every `cairn ...` command in a fenced block, and every
inline code span that starts with `cairn `, in README.md, CONTRIBUTING.md, `skills/`,
`references/`, `docs/` and the `cairn help` text, and parses it with the real argparse
parser. If you rename a flag or a subcommand, fix the docs that name it. Write one command
per line, because `install | uninstall NAME` runs as a shell pipe when it is pasted. Put
`<!-- cairn-cli: historical -->` on the line above a fence that shows an older release's
CLI. Put `<!-- cairn-cli: skip -->` on a line whose inline code is not a command (an exact
error string or a label), or on the line above a fence that quotes a file body. Rerun the
check alone with `uv run python -m unittest tests.test_docs_cli`. Inline code is held to a
looser rule than a fence: it can name a command group or a flag without its required
arguments (`cairn routine`, `cairn recall digest --out`), but an unknown flag still fails.
The spec, `docs/docs-cli-truth-spec.md` section 3.5, lists the exact relaxations.

## Public-repo hygiene

This repository is public. Do not commit a private project or client name, a private
repository link, an absolute home directory path, a personal email or a cloud account id,
in files, commit messages or PR text. Anonymise a post-mortem or plan that comes from work
on a private codebase. `tests/test_public_hygiene.py` fails the gate on the generic shapes
and, locally, on the names you list in `$XDG_CONFIG_HOME/cairn/private-terms` (untracked,
one per line). See `CLAUDE.md` for the full rule.

## Adding a new source

Each source is a thin adapter over the shared renderer. Adding one is typically:

1. A new adapter module under `src/cairn/`.
2. One `@register(...)` line to wire it into the dispatcher.
3. A config block in `cairn.config.example.json` and a reference doc under `skills/cairn/reference/`.
4. Tests under `tests/`.

## Changing a skill description

A harness picks a skill from the `description` in its `SKILL.md` frontmatter. The
quoted phrases after "Use when the user says" are the trigger list.

1. If you add, remove or reword a quoted phrase, update `skills/<name>/routing-eval.jsonl`.
   Each line is `{"intent": ..., "expected": ..., "ambiguous_with": [...]}`. Write the
   intent as a user would type it. Never copy a phrase of two or more words into it; a
   single-word trigger such as "recall" may appear inside a longer request. Each phrase
   needs one row that matches it and no other phrase of the same skill, unless it holds
   every word of a shorter phrase of that skill.
2. Run `uv run python -m unittest tests.test_skill_routing -v`. If you add or remove a
   `recall` phrase on purpose, also update the count in
   `test_quoted_phrases_after_marker_only`.
3. If the test reports a new overlap pair, decide whether the overlap is real. If it is,
   add the skill to the row's `ambiguous_with` and the pair to `DECLARED_OVERLAPS`. If it
   is not, reword the trigger.

The test checks the trigger text, not the harness router. A pass means the description
still names each request; it does not prove the harness picks that skill.

## Pull request process

1. Fork and create a topic branch (`git checkout -b my-change`).
2. Make your change with tests and a green `make check`.
3. Open a PR against `main` and fill in the PR template.
4. A maintainer reviews; address review threads (they must be resolved before merge).
5. PRs are merged via **squash** to keep history linear.

## Commit messages

Keep them short and imperative ("add cursor workspace filter", not "added/adds"). Reference an issue number when relevant.
