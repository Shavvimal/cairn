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
