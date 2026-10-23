"""Cairn-owned registration of the Antigravity ``Stop`` hook.

agy reads lifecycle hooks from ``~/.gemini/config/hooks.json`` (shared by the CLI,
the IDE and the Antigravity 2.0 app). The file maps hook *names* to event handler
lists, so cairn owns exactly one top-level key, ``cairn``, with one ``Stop`` handler
that runs ``cairn hook handle --harness antigravity``. Every other key is preserved
byte for byte in content; removal deletes only the ``cairn`` key, and only when its
command is the one cairn recorded in its ownership file.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from ..config import CairnConfig, ConfigError, user_config_path
from ..runtime import background_environment
from ..storage import atomic_write, lock_path, locked
from .antigravity import (
    READ_ONLY_COMMANDS,
    TEST_COMMANDS,
    grant_commands,
    permission_rules,
    settings_path,
    shell_rules_present,
)

HOOK_NAME = "cairn"
HOOKS_FILE_ENV = "CAIRN_ANTIGRAVITY_HOOKS_FILE"
# The handler enqueues a durable job and returns; agy blocks its own loop until then.
HOOK_TIMEOUT_S = 10


def hooks_file() -> Path:
    override = os.environ.get(HOOKS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config" / "hooks.json"


def ownership_path() -> Path:
    return user_config_path().parent / "antigravity-hook-registration.json"


def hook_command(config: CairnConfig) -> str:
    """The ``sh -c`` command agy runs: an absolute interpreter with the recorded
    background environment, so neither PATH nor a login shell is needed."""
    return shlex.join(
        [
            "/usr/bin/env",
            *[f"{key}={value}" for key, value in background_environment(config).items()],
            sys.executable,
            "-m",
            "cairn",
            "hook",
            "handle",
            "--harness",
            "antigravity",
        ]
    )


def agy_settings(config: CairnConfig | None) -> Path:
    """The settings file of the configured agy store (the CLI's by default)."""
    store = None
    if config is not None:
        try:
            store = config.collection("antigravity-sessions").require_store()
        except ConfigError:
            store = None
    return settings_path(store)


def grant_read_only_commands(config: CairnConfig | None, *, tests: bool = True) -> str:
    """Allow cairn's review (and test-gate) shell commands in agy's settings."""
    path = agy_settings(config)
    commands = READ_ONLY_COMMANDS + (TEST_COMMANDS if tests else ())
    with locked(lock_path(path, "permissions")):
        added = grant_commands(path, commands)
    if not added:
        return f"agy already allows cairn's read-only and test commands ({path})."
    return (
        f"Allowed {len(added)} shell commands for headless agy runs in {path} "
        "(deny rules untouched). List them with: agy -p='/permissions'"
    )


def permission_warning(config: CairnConfig | None) -> str | None:
    """Why a headless review would stall, or ``None`` when shell commands may run."""
    path = agy_settings(config)
    try:
        allow, _deny = permission_rules(path)
    except (OSError, ValueError) as exc:
        return f"cannot read agy permissions in {path}: {exc}"
    if shell_rules_present(allow):
        return None
    return (
        f"agy has no command allow rules in {path}; a headless run soft-denies every shell "
        "command (grep, git diff, tests) and a review ends early. Run: cairn hook install "
        "--harness antigravity --grant-commands"
    )


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object of named hooks")
    return data


def registration(path: Path | None = None) -> dict:
    """What the hooks file says about cairn's hook (never writes anything)."""
    path = path or hooks_file()
    try:
        data = _load(path)
    except (OSError, ValueError) as exc:
        return {"path": str(path), "registered": False, "error": str(exc)}
    entry = data.get(HOOK_NAME)
    handlers = entry.get("Stop", []) if isinstance(entry, dict) else []
    commands = [h.get("command") for h in handlers if isinstance(h, dict)]
    return {
        "path": str(path),
        "registered": bool(commands),
        "enabled": bool(entry.get("enabled", True)) if isinstance(entry, dict) else False,
        "commands": commands,
    }


def configure(config: CairnConfig | None, *, remove: bool = False) -> str:
    path, owner = hooks_file(), ownership_path()
    with locked(lock_path(path, "registration")):
        data = _load(path)
        previous = json.loads(owner.read_text(encoding="utf-8")) if owner.exists() else {}
        if previous and previous.get("path") != str(path):
            raise ValueError(
                f"Cairn's Antigravity hook is registered in {previous.get('path')}, not {path}; "
                "use the same HOME (or CAIRN_ANTIGRAVITY_HOOKS_FILE) to update or remove it"
            )
        if remove:
            entry = data.get(HOOK_NAME)
            recorded = previous.get("command")
            handlers = entry.get("Stop", []) if isinstance(entry, dict) else []
            ours = [h for h in handlers if isinstance(h, dict) and h.get("command") == recorded]
            if entry is not None and (ours or not previous):
                # Without an ownership record the named key is still ours by name,
                # unless someone put a foreign command under it.
                foreign = [
                    h for h in handlers if isinstance(h, dict) and h.get("command") != recorded
                ]
                if previous or not foreign:
                    del data[HOOK_NAME]
                    if path.exists():
                        atomic_write(path, json.dumps(data, indent=2) + "\n")
            owner.unlink(missing_ok=True)
            return f"Removed the cairn Stop hook from {path}; other hooks preserved."
        assert config is not None
        command = hook_command(config)
        # Save ownership first: an interrupted install can always be removed.
        atomic_write(owner, json.dumps({"path": str(path), "command": command}, indent=2) + "\n")
        data[HOOK_NAME] = {
            "enabled": True,
            "Stop": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    return (
        f"Registered the cairn Stop hook in {path}; agy loads it when it next starts "
        "(every turn end exports the conversation)."
    )
