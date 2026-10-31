"""Resolve and record the environment shared by background sync entry points."""

from __future__ import annotations

import os
from pathlib import Path

from .config import CairnConfig


def _qmd_locations() -> tuple[Path, Path]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    config_dir = Path(os.environ.get("QMD_CONFIG_DIR") or config_home / "qmd")
    database = Path(os.environ.get("INDEX_PATH") or cache_home / "qmd/index.sqlite")
    return config_dir, database


def qmd_resources() -> tuple[Path, Path]:
    """Canonical config and database paths for QMD's default index.

    QMD selects its configuration and database independently. In particular,
    QMD_CONFIG_DIR does not relocate the database; XDG_CACHE_HOME/INDEX_PATH do.
    Cairn invokes the default index (it does not pass QMD's --index option).
    """
    config_dir, database = _qmd_locations()
    return (config_dir / "index.yml").resolve(), database.resolve()


def background_environment(config: CairnConfig, *, pin_agent_homes: bool = False) -> dict[str, str]:
    """Pin effective defaults as well as overrides, independent of the host's env.

    Absolute paths also keep relative environment overrides stable when a worker
    changes its cwd. Only the runtime settings we need are recorded, never secrets.

    ``CODEX_HOME`` and ``CLAUDE_CONFIG_DIR`` are recorded only when the host sets
    them, unless ``pin_agent_homes`` pins their defaults too. Pinning them changes
    where a peer CLI finds its login: with ``CLAUDE_CONFIG_DIR`` exported, ``claude
    -p`` reads ``<dir>/.claude.json`` instead of ``~/.claude.json`` and reports
    ``Not logged in``. So no caller pins them by default.
    """
    config_dir, database = _qmd_locations()
    paths: dict[str, Path | None] = {
        "XDG_CONFIG_HOME": Path.home() / ".config",
        "XDG_CACHE_HOME": Path.home() / ".cache",
        "CODEX_HOME": Path.home() / ".codex" if pin_agent_homes else None,
        "CLAUDE_CONFIG_DIR": Path.home() / ".claude" if pin_agent_homes else None,
    }
    resolved: dict[str, str] = {}
    for key, default in paths.items():
        chosen = os.environ.get(key) or default
        if chosen:
            resolved[key] = str(Path(chosen).resolve())
    return {
        "CAIRN_CONFIG": str(config.config_path.resolve()),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        **resolved,
        # Resolve the directory, not index.yml itself: the file may be a symlink
        # to a differently named YAML file. QMD must still open that same link.
        "QMD_CONFIG_DIR": str(config_dir.resolve()),
        "INDEX_PATH": str(database.resolve()),
    }
