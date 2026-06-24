"""Register QMD context descriptions for a session collection.

Identical across sources except for the collection name; the project and repo
descriptions come from the shared catalog in :mod:`cairn.config`. Every QMD
mutation goes through the ``qmd`` CLI (never by editing its index file directly),
so QMD owns its own state.
"""

from __future__ import annotations

import subprocess
import sys

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


__all__ = ["add_context", "register_context", "register_descriptions"]
