"""Cairn-owned fallback registration for clients that omit portable plugin hooks.

``cairn hook install --harness codex`` writes one SessionEnd group to
``$CODEX_HOME/hooks.json``, the Codex trust table for it to ``config.toml``
(:mod:`.codex_trust`), and a receipt beside them with a sha256 per entry it wrote.
Uninstall removes an entry only while its sha256 still equals the receipt's.

Write order: receipt (``prepared``), hooks.json, config.toml, receipt (``installed``).
A crash before the trust table leaves an untrusted group that Codex skips; a crash
after it leaves pending receipt entries that the next run treats as owned.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import CairnConfig, user_config_path
from ..runtime import background_environment
from ..storage import atomic_write, lock_path, locked
from . import codex_trust as trust
from .codex import Codex

RECEIPT_NAME = "cairn-hook-receipt.json"
RECEIPT_VERSION = 2
ENTRY_KINDS = ("hooks_json_group", "config_toml_table")
HOOK_TIMEOUT_S = 3
STATUS_MESSAGE = "Cairn automatic session sync"
EVENT = "SessionEnd"


def _home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()


def _paths() -> tuple[Path, Path]:
    """hooks.json and the legacy (v1) ownership file in cairn's XDG config directory."""
    return _home() / "hooks.json", user_config_path().parent / "codex-hook-registration.json"


def receipt_path() -> Path:
    return _home() / RECEIPT_NAME


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _command(config: CairnConfig) -> str:
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
            "codex",
            "--require-plugin",
        ]
    )


def _group(command: str) -> dict:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": HOOK_TIMEOUT_S,
                "statusMessage": STATUS_MESSAGE,
            }
        ]
    }


def _handlers(group: object) -> list[dict]:
    handlers = group.get("hooks") if isinstance(group, dict) else None
    return [h for h in handlers if isinstance(h, dict)] if isinstance(handlers, list) else []


def _owned_handlers(group: object, commands: set[str]) -> int:
    return sum(h.get("command") in commands for h in _handlers(group))


def _render_hooks(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


# --- loading ------------------------------------------------------------------------


@dataclass
class _State:
    hooks_path: Path
    receipt_path: Path
    legacy_path: Path
    hooks_exists: bool
    hooks_text: str
    data: dict
    config: trust.ConfigSnapshot
    receipt: dict
    legacy: dict
    commands: set[str] = field(default_factory=set)

    def entries(self, kind: str) -> list[dict]:
        return [e for e in self.receipt.get("entries", []) if e.get("kind") == kind]


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not valid JSON ({exc}); move it aside to register again"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object; move it aside to register again")
    if not _strings(data.get("commands", [])):
        raise ValueError(f"{path} has a commands value that is not a list of strings")
    return data


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _read_receipt(path: Path) -> dict:
    """The receipt, checked for the shape the rest of this module reads."""
    receipt = _read_json(path)
    if not receipt:
        return receipt

    def bad(detail: str) -> ValueError:
        return ValueError(
            f"{path} is not a cairn hook receipt ({detail}); move it aside to register again"
        )

    if receipt.get("receipt_version") != RECEIPT_VERSION:
        raise bad(f"receipt_version {receipt.get('receipt_version')!r}, expected {RECEIPT_VERSION}")
    entries = receipt.get("entries", [])
    if not isinstance(entries, list):
        raise bad("entries is not a list")
    for number, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("kind") not in ENTRY_KINDS:
            raise bad(f"entry {number} has no known kind")
        if not isinstance(entry.get("sha256"), str):
            raise bad(f"entry {number} has no sha256")
        if entry["kind"] == "config_toml_table" and not isinstance(entry.get("key"), str):
            raise bad(f"entry {number} has no key")
    return receipt


def _legacy_commands(state: _State) -> set[str]:
    """v1 commands that still own their groups: with no receipt, or beside a receipt
    still ``prepared`` (an interrupted migration). After an installed receipt, a leftover
    v1 file only needs deleting, and the receipt's sha256 values decide."""
    if state.receipt and state.receipt.get("status") != "prepared":
        return set()
    return set(state.legacy.get("commands", []))


def _identity(entry: dict) -> tuple:
    return entry.get("kind"), entry.get("key"), entry.get("sha256")


def _tables_by_key(state: _State) -> dict[str, list[dict]]:
    """Owned trust-table entries per key. An interrupted reinstall leaves two for one
    key, the earlier table and the planned one; either hash marks the table as cairn's."""
    tables: dict[str, list[dict]] = {}
    for entry in state.entries("config_toml_table"):
        tables.setdefault(entry["key"], []).append(entry)
    return tables


def _load(*, strict: bool = True, need_config: bool = True) -> _State:
    """Read hooks.json, the receipt, the v1 file and (when needed) config.toml.

    Uninstall reads config.toml only when the receipt names a trust table, so a
    config.toml that does not parse cannot block removing a v1 registration.
    """
    hooks_path, legacy_path = _paths()
    receipt = _read_receipt(receipt_path())
    legacy = _read_json(legacy_path)
    if strict and legacy and legacy.get("path") != str(hooks_path):
        raise ValueError(
            "Cairn registration belongs to another CODEX_HOME; use its matching XDG_CONFIG_HOME to update/remove"
        )
    if receipt and receipt.get("hooks_path") != str(hooks_path):
        raise ValueError(
            f"{receipt_path()} was written for {receipt.get('hooks_path')}, not {hooks_path}; "
            "move it aside to register again"
        )
    if legacy.get("path") != str(hooks_path):
        legacy = {}
    hooks_text = hooks_path.read_text() if hooks_path.exists() else ""
    return _State(
        hooks_path=hooks_path,
        receipt_path=receipt_path(),
        legacy_path=legacy_path,
        hooks_exists=hooks_path.exists(),
        hooks_text=hooks_text,
        data=_hooks_data(hooks_path, hooks_text),
        config=_config(need_config or _has_tables(receipt)),
        receipt=receipt,
        legacy=legacy,
        commands=set(receipt.get("commands", [])) | set(legacy.get("commands", [])),
    )


def _hooks_data(hooks_path: Path, text: str) -> dict:
    """hooks.json, checked for the shape cairn reads and writes."""
    data = json.loads(text) if text else {}
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    detail = None
    if not isinstance(data, dict):
        detail = "not a JSON object"
    elif not isinstance(hooks, dict):
        detail = '"hooks" is not an object'
    elif not isinstance(hooks.get(EVENT, []), list):
        detail = f'"hooks.{EVENT}" is not a list'
    elif any(
        not isinstance(group, dict) or not isinstance(group.get("hooks", []), list)
        for group in hooks.get(EVENT, [])
    ):
        detail = f'a "hooks.{EVENT}" group is not an object with a "hooks" list'
    if detail is not None:
        raise ValueError(
            f"{hooks_path} is not a Codex hooks file ({detail}); cairn changed nothing."
        )
    return data


def _has_tables(receipt: dict) -> bool:
    return any(e.get("kind") == "config_toml_table" for e in receipt.get("entries", []))


def _config(read: bool) -> trust.ConfigSnapshot:
    path = _home() / "config.toml"
    if read:
        return trust.read_config(path)
    return trust.ConfigSnapshot(path, path.resolve(), False, "", {}, None, None)


def _receipt(
    state: _State,
    *,
    status: str,
    commands: set[str],
    entries: list[dict],
    version: str | None,
) -> dict:
    return {
        "receipt_version": RECEIPT_VERSION,
        "harness": "codex",
        "status": status,
        "hooks_path": str(state.hooks_path),
        "config_path": str(state.config.path),
        "codex_version": version,
        "commands": sorted(commands),
        "entries": entries,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _write_receipt(state: _State, receipt: dict) -> None:
    atomic_write(state.receipt_path, json.dumps(receipt, indent=2) + "\n")


def _table_label(state: _State, key: str) -> str:
    return f"{trust.table_header(key)} in {state.config.path}"


def _index_shift(groups: list, removed: list[int]) -> None:
    if not removed:
        return
    shifted = sum(1 for index in range(min(removed) + 1, len(groups)) if index not in removed)
    if shifted:
        _warn(
            f"removing the Cairn group shifted {shifted} later SessionEnd group(s) down one "
            "index; Codex will ask to trust them again."
        )


# --- uninstall ----------------------------------------------------------------------


def _uninstall(*, dry_run: bool, before_write: Callable[[], None] | None = None) -> list[str]:
    """Remove owned, unchanged entries in reverse order; return dry-run plan lines."""
    hooks_path = _paths()[0]
    # A dry run reads only, so it takes no lock: the lock file is state too.
    with nullcontext() if dry_run else locked(lock_path(hooks_path, "registration")):
        state = _load(need_config=False)
        plan: list[str] = []
        kept: list[dict] = []
        remove_keys = []
        for key, entries in _tables_by_key(state).items():
            current = state.config.state.get(key)
            if current is None:
                continue
            sha = trust.canonical_sha(current)
            if any(e["sha256"] == sha for e in entries):
                remove_keys.append(key)
                plan.append(
                    f"would remove {state.config.path}: {trust.table_header(key)} (sha256 {sha})"
                )
            else:
                _warn(f"{_table_label(state, key)} changed since cairn wrote it; left in place.")
                kept.extend(entries)
        group_entries = state.entries("hooks_json_group")
        group_shas = {e.get("sha256") for e in group_entries}
        groups = trust.session_end_groups(state.data)
        retained, removed = [], []
        kept_group = False
        for index, group in enumerate(groups):
            if not _owned_handlers(group, state.commands):
                retained.append(group)
            elif not group_entries or _owned_handlers(group, _legacy_commands(state)):
                # v1 ownership, by exact command, while the v1 file exists (also beside the
                # prepared receipt of an interrupted migration)
                legacy = state.commands if not group_entries else _legacy_commands(state)
                handlers = [h for h in _handlers(group) if h.get("command") not in legacy]
                if handlers:
                    retained.append({**group, "hooks": handlers})
                else:
                    removed.append(index)
                plan.append(
                    f"would remove {hooks_path}: Cairn handlers of SessionEnd group {index}"
                )
            elif trust.canonical_sha(group) in group_shas:
                removed.append(index)
                plan.append(
                    f"would remove {hooks_path}: SessionEnd group {index} "
                    f"(sha256 {trust.canonical_sha(group)})"
                )
            else:
                _warn(
                    f"SessionEnd group {index} in {hooks_path} changed since cairn wrote it; left in place."
                )
                retained.append(group)
                kept_group = True
        if kept_group:
            kept.extend(group_entries)  # their sha256 still marks the group as cairn's
        elif group_entries and not removed:
            indexes = ", ".join(sorted({str(e.get("index")) for e in group_entries}))
            _warn(
                f"no SessionEnd group in {hooks_path} runs cairn's command any more (cairn wrote "
                f"group {indexes}); left as it is, and its receipt entry dropped."
            )
        config_text = trust.edit(state.config, {}, remove_keys) if remove_keys else None
        hooks_changed = len(retained) != len(groups) or any(
            a is not b for a, b in zip(retained, groups, strict=False)
        )
        if kept:
            plan.append(f"would write {state.receipt_path} (entries left in place)")
        elif state.receipt:
            plan.append(f"would remove {state.receipt_path}")
        if state.legacy:
            plan.append(f"would remove {state.legacy_path}")
        if dry_run:
            return plan
        if before_write is not None:
            before_write()  # the removal plan passed its checks
        if config_text is not None:
            trust.write_config(state.config, config_text)
        if hooks_changed:
            state.data["hooks"][EVENT] = retained
            trust.write_json(hooks_path, _render_hooks(state.data))
            _index_shift(groups, removed)
        if kept:
            commands = {
                h.get("command")
                for group in retained
                for h in _handlers(group)
                if h.get("command") in state.commands
            }
            receipt = _receipt(
                state,
                status="installed",
                commands={c for c in commands if isinstance(c, str)},
                entries=kept,
                version=state.receipt.get("codex_version"),
            )
            _write_receipt(state, receipt)
        else:
            state.receipt_path.unlink(missing_ok=True)
        state.legacy_path.unlink(missing_ok=True)
        return plan


# --- install ------------------------------------------------------------------------


@dataclass
class _Plan:
    data: dict
    groups_before: list
    removed: list[int]
    index: int
    group: dict
    hooks_changed: bool
    key: str
    trusted_hash: str | None = None
    table_entry: dict | None = None
    set_tables: dict[str, str] = field(default_factory=dict)
    remove_keys: list[str] = field(default_factory=list)
    skip: str | None = None
    lines: list[str] = field(default_factory=list)
    config_text: str | None = None


def _plan_install(
    state: _State, command: str, version: str | None, reason: str, want_trust: bool
) -> _Plan:
    data = json.loads(json.dumps(state.data))  # a deep copy that stays JSON
    hooks = data.setdefault("hooks", {})
    groups_before = list(hooks.get(EVENT, []))
    commands = state.commands | {command}
    new_group = _group(command)
    # A v2 receipt owns a group by its sha256; command ownership is for v1 only.
    group_shas = {e["sha256"] for e in state.entries("hooks_json_group")}
    group_shas.add(trust.canonical_sha(new_group))
    groups: list = []
    removed: list[int] = []
    index = None
    for position, group in enumerate(groups_before):
        owned = _owned_handlers(group, commands)
        if not owned:
            groups.append(group)
            continue
        if (
            state.entries("hooks_json_group")
            and trust.canonical_sha(group) not in group_shas
            and not _owned_handlers(group, _legacy_commands(state))
        ):
            raise ValueError(
                f"SessionEnd group {position} in {state.hooks_path} changed since cairn wrote "
                "it; cairn changed nothing. Restore or remove that group, then run the "
                "command again."
            )
        if owned == len(_handlers(group)) and index is None:
            index = len(groups)  # replace in place: later groups keep their indexes
            groups.append(new_group)
        elif owned == len(_handlers(group)):
            removed.append(position)
        else:
            others = [h for h in _handlers(group) if h.get("command") not in commands]
            groups.append({**group, "hooks": others})
    if index is None:
        index = len(groups)
        groups.append(new_group)
    hooks[EVENT] = groups
    text = _render_hooks(data)
    plan = _Plan(
        data=data,
        groups_before=groups_before,
        removed=removed,
        index=index,
        group=new_group,
        hooks_changed=text != state.hooks_text,
        key=trust.state_key(state.hooks_path, EVENT, index, 0),
    )
    group_sha = trust.canonical_sha(new_group)
    in_place = index < len(groups_before) and index not in removed
    verb = "would write" if not state.hooks_exists else "would replace" if in_place else "would add"
    plan.lines.append(
        f"{verb if plan.hooks_changed else 'unchanged'} {state.hooks_path}: "
        f"SessionEnd group {index} (sha256 {group_sha})"
    )
    if not want_trust:
        plan.skip = f"--no-trust: trust not written. {trust.MANUAL_STEP}"
    elif version is None:
        plan.skip = (
            f"Could not read the Codex version ({reason}); trust not written. {trust.MANUAL_STEP}"
        )
    elif not trust.in_range(version):
        plan.skip = (
            f"Codex {version} is outside the versions cairn verified the trust hash on "
            f"({trust.range_text()}); trust not written. {trust.MANUAL_STEP}"
        )
    elif not command.isascii():
        plan.skip = (
            "The Cairn hook command has a non-ASCII character, and the trust hash is not "
            f"verified for one; trust not written. {trust.MANUAL_STEP}"
        )
    owned_tables = _tables_by_key(state)
    checked = plan.key if plan.skip is None else None
    if checked is not None:
        _plan_table(state, plan, owned_tables.get(checked, []))
    for key, recorded in owned_tables.items():
        if key == checked:
            continue
        current = state.config.state.get(key)
        if current is None:
            continue
        sha = trust.canonical_sha(current)
        if any(e["sha256"] == sha for e in recorded):
            plan.remove_keys.append(key)
            plan.lines.append(
                f"would remove {state.config.path}: {trust.table_header(key)} (sha256 {sha})"
            )
        else:
            _warn(f"{_table_label(state, key)} changed since cairn wrote it; left in place.")
    if plan.set_tables or plan.remove_keys:
        # Checked before any write: a refused edit leaves hooks.json untouched too.
        plan.config_text = trust.edit(state.config, plan.set_tables, plan.remove_keys)
        if state.config.exists:
            plan.lines.append(
                f"would back up {state.config.path} to {state.config.target}{trust.BACKUP_SUFFIX}"
            )
    return plan


def _plan_table(state: _State, plan: _Plan, owned: list[dict]) -> None:
    handler = plan.group["hooks"][0]
    value = trust.trusted_hash(EVENT, handler)
    table = {"trusted_hash": value}
    sha = trust.canonical_sha(table)
    current = state.config.state.get(plan.key)
    entry = {
        "kind": "config_toml_table",
        "key": plan.key,
        "trusted_hash": value,
        "sha256": sha,
        "origin": "written",
        "replaced": None,
    }
    header = trust.table_header(plan.key)
    if current == table and not trust.has_table(state.config, plan.key):
        # Defined inline or by dotted keys: Codex trusts it, but cairn cannot edit it.
        plan.lines.append(f"unchanged {state.config.path}: {header} (not cairn's to edit)")
        plan.trusted_hash = value
        return
    if current == table:
        match = next((e for e in owned if e["sha256"] == sha), None)
        if match is not None:
            entry["origin"] = match.get("origin", "written")
            entry["replaced"] = match.get("replaced")
        else:
            entry["origin"] = "adopted"
        plan.lines.append(f"unchanged {state.config.path}: {header} (sha256 {sha})")
    elif current is None:
        plan.set_tables[plan.key] = value
        plan.lines.append(
            f"would write {state.config.path}: {header} trusted_hash = {value} (sha256 {sha})"
        )
    elif (
        isinstance(current, dict)
        and set(current) == {"trusted_hash"}
        and isinstance(current["trusted_hash"], str)
    ):
        ours = any(e["sha256"] == trust.canonical_sha(current) for e in owned)
        entry["replaced"] = None if ours else current["trusted_hash"]
        plan.set_tables[plan.key] = value
        plan.lines.append(
            f"would replace {state.config.path}: {header} trusted_hash = {value} (sha256 {sha})"
        )
    else:
        keys = (
            ", ".join(sorted(k for k in current if k != "trusted_hash"))
            if isinstance(current, dict)
            else "not a table"
        )
        _warn(
            f"{header} in {state.config.path} has keys cairn did not write ({keys}); "
            f"left unchanged. {trust.MANUAL_STEP}"
        )
        plan.skip = f"trust not written. {trust.MANUAL_STEP}"
        return
    plan.trusted_hash = value
    plan.table_entry = entry


def _install(
    config: CairnConfig,
    *,
    dry_run: bool,
    want_trust: bool,
    before_write: Callable[[], None] | None = None,
) -> str:
    command = _command(config)
    version, reason = trust.codex_version() if want_trust else (None, "")
    hooks_path = _paths()[0]
    with nullcontext() if dry_run else locked(lock_path(hooks_path, "registration")):
        state = _load()
        plan = _plan_install(state, command, version, reason, want_trust)
        group_entry = {
            "kind": "hooks_json_group",
            "event": EVENT,
            "index": plan.index,
            "sha256": trust.canonical_sha(plan.group),
        }
        entries = [group_entry] + ([plan.table_entry] if plan.table_entry else [])
        final = _receipt(
            state,
            status="installed",
            commands={command},
            entries=[{**e, "pending": False} for e in entries],
            version=version,
        )
        receipt_changed = _comparable(final) != _comparable(state.receipt)
        if dry_run:
            verb = "would write" if receipt_changed else "unchanged"
            plan.lines.append(f"{verb} {state.receipt_path} ({len(entries)} entries)")
            if state.legacy:
                plan.lines.append(f"would remove {state.legacy_path}")
            return "\n".join(plan.lines + ([plan.skip] if plan.skip else []))
        if before_write is not None:
            before_write()  # the plan passed every refusal check, so writing starts here
        if not (plan.hooks_changed or plan.config_text is not None):
            resumed = state.receipt.get("status") == "prepared"
            if receipt_changed:
                _write_receipt(state, final)
            state.legacy_path.unlink(missing_ok=True)
            if plan.trusted_hash is None:
                return _registered_untrusted(hooks_path, plan)
            if not resumed:
                return f"Cairn fallback in {hooks_path} is already registered and trusted; nothing changed."
            # An interrupted run wrote both files: finish it, verification included.
            note = _verify_trust(state, plan, final, command)
            if note is None:
                return _registered_untrusted(hooks_path, plan)
            return (
                f"Finished the interrupted Cairn registration in {hooks_path} (SessionEnd group "
                f"{plan.index}), trusted in {state.config.path} for Codex {version}. "
                f"Restart Codex.{note}"
            )
        # Save ownership first: an interrupted install can always be removed. The
        # earlier entries stay until the installed receipt, so either version of a
        # replaced group or table is still cairn's after a crash.
        planned = {_identity(e) for e in entries}
        previous = [e for e in state.receipt.get("entries", []) if _identity(e) not in planned]
        prepared = _receipt(
            state,
            status="prepared",
            commands=state.commands | {command},
            entries=[{**e, "pending": True} for e in entries + previous],
            version=version,
        )
        _write_receipt(state, prepared)
        if plan.hooks_changed:
            trust.write_json(hooks_path, _render_hooks(plan.data))
            _check_written_group(hooks_path, plan)
            _index_shift(plan.groups_before, plan.removed)
        if plan.config_text is not None:
            trust.write_config(state.config, plan.config_text)
        installed = _receipt(
            state,
            status="installed",
            commands={command},
            entries=[{**e, "pending": False} for e in entries],
            version=version,
        )
        _write_receipt(state, installed)
        state.legacy_path.unlink(missing_ok=True)
        if plan.trusted_hash is None:
            return _registered_untrusted(hooks_path, plan)
        note = _verify_trust(state, plan, installed, command)
    if note is None:
        return _registered_untrusted(hooks_path, plan)
    return (
        f"Registered Cairn fallback in {hooks_path} (SessionEnd group {plan.index}) and trusted it "
        f"in {state.config.path} for Codex {version}. Restart Codex.{note}"
    )


def _comparable(receipt: dict) -> dict:
    return {k: v for k, v in receipt.items() if k != "updated_at"}


def _registered_untrusted(hooks_path: Path, plan: _Plan) -> str:
    line = f"Registered Cairn fallback in {hooks_path} (SessionEnd group {plan.index})."
    return f"{line}\n{plan.skip}" if plan.skip else line


def _check_written_group(hooks_path: Path, plan: _Plan) -> None:
    """Recompute the group's sha256 and trusted_hash from the bytes on disk."""
    written = trust.session_end_groups(json.loads(hooks_path.read_text()))
    group = written[plan.index] if plan.index < len(written) else None
    same_group = group is not None and trust.canonical_sha(group) == trust.canonical_sha(plan.group)
    if not same_group or (
        plan.trusted_hash is not None
        and trust.trusted_hash(EVENT, _handlers(group)[0]) != plan.trusted_hash
    ):
        raise RuntimeError(
            f"{hooks_path} did not read back with the Cairn group at SessionEnd index "
            f"{plan.index}; trust not written"
        )


def _verify_trust(state: _State, plan: _Plan, receipt: dict, command: str) -> str | None:
    """Ask Codex whether it now trusts the hook. ``None`` means the table was rolled back."""
    try:
        observed = Codex().inspect_hooks(os.getcwd())
    except (OSError, ValueError, RuntimeError) as exc:
        return f" (Codex did not answer hooks/list: {exc})"
    mine = [h for h in observed if h.get("command") == command]
    status = next((h.get("trustStatus") for h in mine if h.get("trustStatus")), None)
    if not mine or status in (None, "trusted"):
        return ""
    snapshot = trust.read_config(state.config.path)
    entry = plan.table_entry or {}
    wrote = entry.get("origin") == "written"  # an adopted or inline table is not cairn's
    if wrote and trust.canonical_sha(snapshot.state.get(plan.key)) == entry["sha256"]:
        text = trust.edit(snapshot, {}, [plan.key])
        if not state.config.exists and not text.strip():
            trust.remove_config(snapshot)  # cairn created the file; take it away again
        else:
            # No backup: config.toml.cairn-bak keeps the bytes from before this run.
            trust.write_config(snapshot, text, backup=False)
    receipt["entries"] = [e for e in receipt["entries"] if e.get("kind") != "config_toml_table"]
    _write_receipt(state, receipt)
    removed = "trust table removed" if wrote else "the table is not cairn's, so it stays"
    _warn(
        f"Codex reports the Cairn hook as {status} after the write; {removed}. "
        "Review/trust it in /hooks."
    )
    plan.skip = None
    return None


# --- entry points -------------------------------------------------------------------


def configure(
    config: CairnConfig | None,
    *,
    remove: bool = False,
    dry_run: bool = False,
    trust_hook: bool = True,
    before_write: Callable[[], None] | None = None,
) -> str:
    """Install (or with ``remove``, uninstall) the fallback group and its trust table.

    ``before_write`` runs once the install is known to go ahead, before cairn's first
    write: ``hook install`` writes the launcher there, so a refused install writes nothing.
    """
    if remove:
        plan = _uninstall(dry_run=dry_run)
        if dry_run:
            return "\n".join(plan) if plan else "Nothing cairn owns in this CODEX_HOME."
        return "Removed only Cairn-owned Codex hook handlers; shared data/launcher preserved."
    observed = Codex().inspect_hooks(os.getcwd())
    bundled = any(
        h.get("pluginId") == "cairn@cairn" and h.get("eventName") == "sessionEnd" for h in observed
    )
    if bundled:
        plan = _uninstall(dry_run=dry_run, before_write=before_write)
        message = (
            "Bundled Codex SessionEnd discovered; no fallback registered. Review trust in /hooks."
        )
        return "\n".join([*plan, message]) if dry_run else message
    assert config is not None
    return _install(config, dry_run=dry_run, want_trust=trust_hook, before_write=before_write)


def trust_report() -> dict:
    """Trust state from the files alone, for ``hook status`` and ``doctor``."""
    version, reason = trust.codex_version()
    report: dict = {
        "codex_version": version,
        "in_range": trust.in_range(version),
        "verified_range": trust.range_text(),
        "groups": [],
    }
    if version is None:
        report["version_error"] = reason
    try:
        state = _load(strict=False)
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
        return report
    report["groups"] = trust.trust_state(
        state.hooks_path,
        state.data,
        state.config.state,
        state.commands,
        state.receipt,
        report["in_range"],
    )
    return report
