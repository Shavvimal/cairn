.PHONY: install lint format typecheck test check

# Sync the dev env including the optional `granola` extra (cryptography), so the
# Granola decrypt/export path is exercisable locally and in CI.
install:
	uv sync --extra granola

lint:
	uv run ruff check src tests

format:
	uv run ruff check --fix src tests
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	uv run python -m unittest discover -s tests

check: lint typecheck test
