"""Register QMD context descriptions for a session collection.

Identical across sources except for the collection name; the project and repo
descriptions come from the shared catalog in :mod:`cairn.config`. Every QMD
mutation goes through the ``qmd`` CLI (never by editing its index file directly),
so QMD owns its own state.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .config import get_config


def add_context(path: str, description: str, qmd_binary: str | None = None) -> bool:
    """Run ``qmd context add`` for one ``qmd://`` path; report status; return success.

    On failure we surface stderr rather than swallowing it: a registration that
    silently failed would leave QMD search results without their context.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    result = subprocess.run(
        [qmd_binary, "context", "add", path, description],
        capture_output=True,
        text=True,
    )
    ok = result.returncode == 0
    print(f"  [{'ok' if ok else 'FAILED'}] {path}: {description}")
    if not ok and result.stderr.strip():
        print(f"      {result.stderr.strip()}", file=sys.stderr)
    return ok


def register_descriptions(descriptions: dict[str, str], qmd_binary: str | None = None) -> int:
    """Register a map of ``qmd://`` path → description; return 0 iff all succeeded.

    The shared body of every source's ``context`` command: register each entry,
    count failures, print the summary, and return an exit code that distinguishes a
    partial failure from a clean run. *Which* paths to register is each source's
    policy (project/repo catalog, a single collection, or per-service docs); this
    only owns the registration loop and reporting.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    failures = sum(
        0 if add_context(path, description, qmd_binary) else 1
        for path, description in descriptions.items()
    )
    if failures:
        print(
            f"\n{failures} context registration(s) FAILED. Run 'qmd context list' to inspect.",
            file=sys.stderr,
        )
        return 1
    print("\nDone. Run 'qmd context list' to verify.")
    return 0


def register_context(collection: str) -> int:
    """Register project- and repo-level QMD context descriptions for a collection.

    Convenience wrapper over :func:`register_descriptions` building the path map from
    the shared project/repo catalog in :mod:`cairn.config`.
    """
    cfg = get_config()
    descriptions = {
        f"qmd://{collection}/{project}": description
        for project, description in cfg.project_descriptions.items()
    }
    for project, repos in cfg.repo_catalog.items():
        for repo, description in repos.items():
            descriptions[f"qmd://{collection}/{project}/{repo}"] = description
    return register_descriptions(descriptions, cfg.qmd_binary)


# ---------------------------------------------------------------------------
# Collection registration (the bootstrap that makes the index non-empty)
# ---------------------------------------------------------------------------
#
# QMD only indexes directories that have been *registered* as collections; until
# then ``qmd update``/``qmd embed`` have nothing to work on and every search is
# empty. cairn never edits QMD's index file - it drives the ``qmd`` CLI, exactly
# as :func:`add_context` does - so these wrap ``qmd collection list/add`` and stay
# idempotent (safe to run on every sync).


# A registered collection shows up as a ``qmd://<name>/`` URI in ``collection
# list`` (and in ``show``/``status``); that token is the one stable anchor across
# qmd's human-readable, no-``--json`` output, so we parse names out of it.
_COLLECTION_URI = re.compile(r"qmd://([^/\s)]+)/")


def list_collections(qmd_binary: str | None = None) -> set[str]:
    """Return the set of collection names qmd currently has registered.

    Raises on a *real* ``qmd`` failure (non-zero exit) rather than returning an
    empty set: mistaking "qmd is broken" for "nothing registered" would make the
    caller re-add collections forever.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    result = subprocess.run(
        [qmd_binary, "collection", "list"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`qmd collection list` failed (exit {result.returncode}): "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    return set(_COLLECTION_URI.findall(result.stdout))


def _has_markdown(path: Path) -> bool:
    """True iff ``path`` is a directory containing at least one ``.md`` file."""
    return path.is_dir() and next(path.rglob("*.md"), None) is not None


def ensure_collection(
    name: str,
    path: Path,
    qmd_binary: str | None = None,
    known: set[str] | None = None,
) -> str:
    """Idempotently register ``path`` as the qmd collection ``name``.

    Returns a status word so callers can report what happened:

    - ``"skipped"`` - the dir is missing or has no markdown yet (e.g. a source that
      has not produced data). Not an error: it registers on a later sync once it
      does. This is what lets cursor/codex/granola self-heal when they first appear.
    - ``"exists"``  - already a qmd collection (idempotent no-op).
    - ``"added"``   - newly registered via ``qmd collection add``.
    - ``"FAILED"``  - ``qmd collection add`` returned non-zero; stderr is surfaced.

    Pass ``known`` (a pre-fetched :func:`list_collections` set) to avoid one
    ``collection list`` call per collection when registering several at once.
    """
    qmd_binary = qmd_binary or get_config().qmd_binary
    path = Path(path)
    if not _has_markdown(path):
        return "skipped"
    if known is None:
        known = list_collections(qmd_binary)
    if name in known:
        return "exists"
    result = subprocess.run(
        [qmd_binary, "collection", "add", str(path), "--name", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if result.stderr.strip():
            print(f"      {result.stderr.strip()}", file=sys.stderr)
        return "FAILED"
    return "added"


__all__ = [
    "add_context",
    "ensure_collection",
    "list_collections",
    "register_context",
    "register_descriptions",
]
