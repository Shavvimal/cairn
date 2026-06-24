"""Project / repo resolution against the shared catalog.

All sources organize exported sessions into ``<project>/<repo>/`` folders using
the same catalog. They resolve the project from different inputs:

* Claude Code: the slug-encoded ``~/.claude/projects/<dir>`` name
  (:func:`project_name_from_dir` / :func:`repo_name_from_dir`).
* Codex / Cursor: a real filesystem path (the session ``cwd`` / workspace folder)
  via :func:`project_from_folder_path`.

The catalog (project groups, repo descriptions) and the machine-specific path
prefixes are configuration, not code - they come from :mod:`cairn.config`.
:func:`repo_subdir_from_name` maps a grouped repo name to its catalog subfolder
and is identical for every source.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from .config import get_config


def _strip_prefix(name: str, prefixes: tuple[str, ...]) -> str:
    """Strip the first matching prefix from ``name`` (prefixes are longest-first)."""
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def repo_subdir_from_name(project_group: str, repo_name: str) -> str | None:
    """Derive the repo subdirectory from a repo name within a project group.

    Strips known group prefixes (and their ``-worktrees-`` variants), then matches
    the remainder against the catalog (longest entry first for a greedy match).

    e.g. 'your-product-api' → 'api'
         'your-product-worktrees-api-feature-sop' → 'api'
         'superset-worktrees-daemons-you-eng-1088-foo' → 'daemons'
         'context' → None (no catalog entry)
    """
    cfg = get_config()
    catalog = cfg.repo_catalog.get(project_group)
    if not catalog:
        return None

    group_names = [p for p in cfg.project_groups if cfg.project_groups[p] == project_group]
    prefixes = tuple(
        sorted(
            [f"{p}-" for p in group_names] + [f"{p}-worktrees-" for p in group_names],
            key=len,
            reverse=True,
        )
    )
    remainder = _strip_prefix(repo_name, prefixes)

    for candidate in sorted(catalog, key=len, reverse=True):
        if remainder == candidate or remainder.startswith(candidate + "-"):
            return candidate
    return None


def repo_name_from_dir(encoded_dir: str) -> str:
    """Derive the ungrouped repo/dir name from an encoded project dir name.

    e.g. '-Users-you-Code-your-product-api' -> 'your-product-api'
         '-Users-you--superset-worktrees-api-ci' -> 'superset-worktrees-api-ci'
    """
    return _strip_prefix(encoded_dir, get_config().home_slug_prefixes) or encoded_dir


def project_name_from_dir(encoded_dir: str) -> str:
    """Derive a human-readable project folder name from an encoded project dir name.

    Groups worktrees under their root project using the configured project groups.

    e.g. '-Users-you-Code-your-product-api' -> 'your-product'
         '-Users-you-Code-your-product-worktrees-api-feature-sop' -> 'your-product'
         '-Users-you--superset-worktrees-api-ci' -> 'your-product'
         '-Users-you-Code-context' -> 'context'
    """
    cfg = get_config()
    name = _strip_prefix(encoded_dir, cfg.home_slug_prefixes)
    for prefix in sorted(cfg.project_groups, key=len, reverse=True):
        if name.startswith(prefix):
            return cfg.project_groups[prefix]
    return name or encoded_dir


def project_from_folder_path(folder_path: str) -> tuple[str | None, str | None]:
    """Derive (project_group, repo_subdir) from a real folder path.

    e.g. '~/Code/your-product/api' → ('your-product', 'api')
         '~/Code/context' → ('context', None)
         '~/Code/your-product/worktrees/api/branch' → ('your-product', 'api')
         '~/.superset/worktrees/api/branch' → ('your-product', 'api')
    """
    cfg = get_config()

    # Superset terminal worktrees live outside Code/ under a flat layout:
    # <superset_prefix>/<repo>/<branch...>. Which project they belong to is
    # config-driven (not hardcoded): the conventional project_groups key
    # "superset-worktrees" names the group. Absent that mapping we don't guess.
    superset_prefix = f"{cfg.superset_prefix}/"
    if folder_path.startswith(superset_prefix):
        group = cfg.project_groups.get("superset-worktrees")
        if group is None:
            return None, None
        relative = folder_path[len(superset_prefix) :].rstrip("/").split("/")
        catalog = cfg.repo_catalog.get(group)
        if catalog and relative and relative[0] in catalog:
            return group, relative[0]
        return group, None

    code_prefix = f"{cfg.code_prefix}/"
    if not folder_path.startswith(code_prefix):
        return None, None

    parts = (
        folder_path[len(code_prefix) :].rstrip("/").split("/")
    )  # e.g. ["your-product", "api"] or ["context"]
    if not parts:
        return None, None

    project_group = cfg.project_groups.get(parts[0], parts[0])
    catalog = cfg.repo_catalog.get(project_group)
    if not catalog:
        return project_group, None

    # Worktree path: your-product/worktrees/api/branch-name → api
    if len(parts) >= 3 and parts[1] == "worktrees" and parts[2] in catalog:
        return project_group, parts[2]

    # Direct subdirectory: your-product/api → api (also covers your-product/api/.worktrees/...)
    if len(parts) >= 2 and parts[1] in catalog:
        return project_group, parts[1]

    return project_group, None


def project_from_file_uris(file_uris: list[str]) -> tuple[str | None, str | None]:
    """Fallback: extract project from file URIs by finding their common path."""
    paths = []
    for uri in file_uris:
        if uri.startswith("file://"):
            paths.append(unquote(uri[7:]))
        elif uri.startswith("/"):
            paths.append(uri)

    if not paths:
        return None, None

    common = str(Path(paths[0]).parent) if len(paths) == 1 else os.path.commonpath(paths)
    return project_from_folder_path(common)


def repo_label(project: str | None, repo_subdir: str | None) -> str | None:
    """Build the ``repo`` frontmatter value from a resolved project/subdir."""
    if project and repo_subdir:
        return f"{project}-{repo_subdir}"
    if project:
        return project
    return None


__all__ = [
    "project_from_file_uris",
    "project_from_folder_path",
    "project_name_from_dir",
    "repo_label",
    "repo_name_from_dir",
    "repo_subdir_from_name",
]
