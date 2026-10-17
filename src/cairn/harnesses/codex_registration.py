"""Cairn-owned fallback registration for clients that omit portable plugin hooks."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from ..config import CairnConfig, user_config_path
from ..runtime import background_environment
from ..storage import atomic_write, locked
from .codex import Codex


def _paths() -> tuple[Path, Path]:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    return home / "hooks.json", user_config_path().parent / "codex-hook-registration.json"


def _remove_owned(data: dict, commands: set[str]) -> None:
    groups = data.get("hooks", {}).get("SessionEnd", [])
    retained = []
    for group in groups:
        # Only remove handlers whose exact command Cairn previously recorded.
        handlers = [h for h in group.get("hooks", []) if h.get("command") not in commands]
        if handlers or not group.get("hooks"):
            retained.append({**group, "hooks": handlers})
    if "SessionEnd" in data.get("hooks", {}):
        data["hooks"]["SessionEnd"] = retained


def configure(config: CairnConfig | None, *, remove: bool = False) -> str:
    hooks_path, ownership_path = _paths()
    observed = [] if remove else Codex().inspect_hooks(os.getcwd())
    bundled = any(
        h.get("pluginId") == "cairn@cairn" and h.get("eventName") == "sessionEnd" for h in observed
    )
    command = None
    if not remove and not bundled:
        assert config is not None
        command = shlex.join(
            [
                "/usr/bin/env",
                *[f"{key}={value}" for key, value in background_environment(config).items()],
                sys.executable,
                "-m",
                "cairn",
                "hook",
                "handle",
                "--harness",
                "codex",
                "--require-plugin",
            ]
        )
    with locked(hooks_path.with_suffix(".cairn.lock")):
        data = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
        previous = json.loads(ownership_path.read_text()) if ownership_path.exists() else {}
        if previous and previous.get("path") != str(hooks_path):
            raise ValueError(
                "Cairn registration belongs to another CODEX_HOME; use its matching XDG_CONFIG_HOME to update/remove"
            )
        commands = set(previous.get("commands", []))
        if command:
            commands.add(command)
            # Save ownership first: an interrupted install can always be removed.
            atomic_write(
                ownership_path, json.dumps({"path": str(hooks_path), "commands": sorted(commands)})
            )
        _remove_owned(data, commands)
        if command:
            data.setdefault("hooks", {}).setdefault("SessionEnd", []).append(
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 3,
                            "statusMessage": "Cairn automatic session sync",
                        }
                    ]
                }
            )
        if hooks_path.exists() or command:
            atomic_write(hooks_path, json.dumps(data, indent=2) + "\n")
        if not command:
            ownership_path.unlink(missing_ok=True)
    if remove:
        return "Removed only Cairn-owned Codex hook handlers; shared data/launcher preserved."
    if bundled:
        return (
            "Bundled Codex SessionEnd discovered; no fallback registered. Review trust in /hooks."
        )
    return f"Bundled hook not exposed by this client; registered Cairn fallback in {hooks_path}. Review/trust it in /hooks."
