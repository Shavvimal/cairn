.PHONY: install lint format typecheck test check discriminates

# Sync the dev env including the optional `granola` extra (cryptography), so the
# Granola decrypt/export path is exercisable locally and in CI.
install:
	uv sync --extra granola

# scripts/check-test-discriminates.py is named alone: scripts/sync_plugin_manifest.py
# predates the linter config and is left as it is.
LINTED = src tests scripts/check-test-discriminates.py .agents/skills/skill-review/scripts/measure.py

lint:
	uv run ruff check $(LINTED)
	uv run ruff format --check $(LINTED)

format:
	uv run ruff check --fix $(LINTED)
	uv run ruff format $(LINTED)

typecheck:
	uv run mypy

test:
	uv run python -m unittest discover -s tests

check: lint typecheck test

# Run one test with the fix reverted (it must fail) and with it (it must pass); print the
# evidence line for the PR body. See CONTRIBUTING.md, "A new test must fail without its fix".
discriminates:
	@test -n "$(TEST)" || { echo "usage: make discriminates TEST=tests.test_x.TestY.test_z [SINCE=REF] [FILES='src/cairn/x.py ...']" >&2; exit 2; }
	uv run python scripts/check-test-discriminates.py $(TEST) $(if $(SINCE),--since $(SINCE)) $(if $(FILES),--files $(FILES))
