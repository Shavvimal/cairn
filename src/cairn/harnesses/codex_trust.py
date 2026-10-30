"""Codex hook trust: the ``trusted_hash`` format, the version pin and a narrow TOML edit.

Codex keeps the trust for each hooks.json handler in ``$CODEX_HOME/config.toml`` as::

    [hooks.state."<hooks.json path>:<event_name>:<group index>:<handler index>"]
    trusted_hash = "sha256:<64 hex>"

The hash is over a canonical JSON identity of one handler (:func:`trusted_hash`). The
format is not documented by Codex, so cairn writes it only for the Codex versions it
was recomputed on (:data:`TRUST_FORMAT_VERIFIED`).

The standard library reads TOML but does not write it, so cairn edits whole
``[hooks.state."<key>"]`` tables line by line and refuses the result unless it parses
back to the original document with exactly the planned keys changed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..procs import run_capped
from ..storage import atomic_write

# Codex versions on which trusted_hash was recomputed from a live hooks.state entry.
TRUST_FORMAT_VERIFIED: tuple[tuple[str, str], ...] = (("0.165.0", "2026-10-30"),)

DEFAULT_TIMEOUT_S = 600  # Codex's handler timeout when hooks.json gives none
VERSION_TIMEOUT_S = 5.0
BACKUP_SUFFIX = ".cairn-bak"
MANUAL_STEP = "Review/trust the Cairn SessionEnd hook in /hooks."

Version = tuple[int, int, int]


# --- hash ---------------------------------------------------------------------------


def snake_case(event: str) -> str:
    """``SessionEnd`` -> ``session_end``, the event name Codex hashes and keys by."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha(value: object) -> str:
    """Hex sha256 of ``value`` as canonical JSON: the receipt's per-entry hash."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def trusted_hash(event: str, handler: dict) -> str:
    """The ``trusted_hash`` Codex stores for one hooks.json handler of ``event``."""
    identity: dict = {
        "type": handler["type"],
        "command": handler["command"],
        "timeout": handler.get("timeout", DEFAULT_TIMEOUT_S),
        "async": False,
    }
    if "statusMessage" in handler:
        identity["statusMessage"] = handler["statusMessage"]
    return "sha256:" + canonical_sha({"event_name": snake_case(event), "hooks": [identity]})


def state_key(hooks_path: Path, event: str, group: int, handler: int) -> str:
    return f"{hooks_path}:{snake_case(event)}:{group}:{handler}"


# --- version pin --------------------------------------------------------------------


def parse_version(text: str) -> Version | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def verified_range() -> tuple[Version, Version]:
    """Lowest verified version (inclusive) to the next minor after the highest (exclusive)."""
    versions = [parse_version(version) for version, _ in TRUST_FORMAT_VERIFIED]
    known = [v for v in versions if v is not None]
    highest = max(known)
    return min(known), (highest[0], highest[1] + 1, 0)


def range_text() -> str:
    low, high = verified_range()
    return f">= {'.'.join(map(str, low))}, < {'.'.join(map(str, high))}"


def in_range(version: str | None) -> bool:
    parsed = parse_version(version or "")
    if parsed is None or not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        return False
    low, high = verified_range()
    return low <= parsed < high


def codex_version() -> tuple[str | None, str]:
    """``(version, "")`` from ``codex --version``, or ``(None, reason)``."""
    try:
        result = run_capped(["codex", "--version"], timeout=VERSION_TIMEOUT_S)
    except FileNotFoundError:
        return None, "codex is not on PATH"
    except subprocess.TimeoutExpired:
        return None, f"codex --version did not answer in {VERSION_TIMEOUT_S:g}s"
    except OSError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, f"codex --version exited {result.returncode}"
    parsed = parse_version(result.stdout)
    if parsed is None:
        return None, f"unrecognised codex --version output {result.stdout.strip()!r}"
    return ".".join(map(str, parsed)), ""


# --- config.toml snapshot and line edit ---------------------------------------------


@dataclass(frozen=True)
class ConfigSnapshot:
    """``config.toml`` as read at the start of a run, for the edit and the race check."""

    path: Path  # the path cairn names in messages: $CODEX_HOME/config.toml
    target: Path  # the file written: path with symlinks resolved
    exists: bool
    text: str
    data: dict
    fingerprint: tuple[int, int, str] | None
    mode: int | None

    @property
    def state(self) -> dict:
        hooks = self.data.get("hooks")
        state = hooks.get("state") if isinstance(hooks, dict) else None
        return state if isinstance(state, dict) else {}


def _fingerprint(path: Path) -> tuple[tuple[int, int, str] | None, bytes]:
    try:
        info = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, b""
    return (info.st_mtime_ns, info.st_size, hashlib.sha256(raw).hexdigest()), raw


def read_config(path: Path) -> ConfigSnapshot:
    """Read and parse ``path``; a file that does not parse is an error before any write."""
    target = path.resolve()
    fingerprint, raw = _fingerprint(target)
    if fingerprint is None:
        return ConfigSnapshot(path, target, False, "", {}, None, None)
    try:
        text = raw.decode("utf-8")
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path} does not parse as TOML ({exc}); cairn changed nothing.") from None
    mode = stat.S_IMODE(target.stat().st_mode)
    return ConfigSnapshot(path, target, True, text, data, fingerprint, mode)


def _header_path(line: str) -> tuple[tuple[str, ...] | None, bool]:
    """``(dotted path, is a header)`` for one line; ``[[...]]`` is a header with no path.

    A line that does not parse alone is not a header (for example a line inside a
    multi-line string). The post-edit parse check catches any other misreading.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None, False
    try:
        parsed = tomllib.loads(stripped + "\n")
    except tomllib.TOMLDecodeError:
        return None, False
    if stripped.startswith("[["):
        return None, True
    path: list[str] = []
    node: object = parsed
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return (tuple(path), True) if node == {} else (None, True)


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _spans(lines: list[str]) -> dict[tuple[str, ...], tuple[int, int]]:
    """Header line to the line before the next header, minus trailing blank/comment lines."""
    headers = []
    for index, line in enumerate(lines):
        path, is_header = _header_path(line)
        if is_header:
            headers.append((index, path))
    spans: dict[tuple[str, ...], tuple[int, int]] = {}
    for position, (start, path) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        while end > start + 1 and _is_filler(lines[end - 1]):
            end -= 1
        if path is not None:
            spans[path] = (start, end)
    return spans


def _basic_string(value: str) -> str:
    escapes = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n"}
    escapes |= {"\f": "\\f", "\r": "\\r"}
    out = []
    for char in value:
        if char in escapes:
            out.append(escapes[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def table_header(key: str) -> str:
    return f"[hooks.state.{_basic_string(key)}]"


def _value_line(value: str) -> str:
    return f"trusted_hash = {_basic_string(value)}"


def _prune(data: dict) -> dict:
    """Drop an empty ``hooks.state`` and an empty ``hooks``: removing the last table
    can make either disappear from the parse, which is not a change to anything else."""
    data = copy.deepcopy(data)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get("state") == {}:
            del hooks["state"]
        if not hooks:
            del data["hooks"]
    return data


def edit(snapshot: ConfigSnapshot, set_tables: dict[str, str], remove: Iterable[str]) -> str:
    """The new config text with ``remove`` tables deleted and ``set_tables`` written.

    Raises ``ValueError`` (cairn changed nothing) when a planned key is defined some
    other way than its own ``[hooks.state."<key>"]`` table, or when the result does not
    parse back to the original with exactly the planned keys changed.
    """
    path = snapshot.path
    refused = f"the edited {path} did not parse back to the expected tables; cairn changed nothing."
    lines = snapshot.text.splitlines(keepends=True)
    eol = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    state = snapshot.state
    expected = copy.deepcopy(snapshot.data)
    for key in remove:
        span = _spans(lines).get(("hooks", "state", key))
        if span is None:
            if key in state:
                raise ValueError(refused)
            continue
        start, end = span
        if start > 0 and not lines[start - 1].strip():
            start -= 1
        del lines[start:end]
        del expected["hooks"]["state"][key]
    for key, value in set_tables.items():
        spans = _spans(lines)
        span = spans.get(("hooks", "state", key))
        if span is not None:
            start, end = span
            lines[start + 1 : end] = [_value_line(value) + eol]
        elif key in state:
            raise ValueError(refused)
        else:
            # Only tables the parse knows: a header-shaped line in a string is no anchor.
            anchors = [
                s
                for p, s in spans.items()
                if p[:2] == ("hooks", "state") and len(p) == 3 and p[2] in state
            ]
            at = max(end for _, end in anchors) if anchors else len(lines)
            if at > 0 and not lines[at - 1].endswith("\n"):
                lines[at - 1] += eol
            block = [table_header(key) + eol, _value_line(value) + eol]
            lines[at:at] = ([eol] if at > 0 else []) + block
        expected.setdefault("hooks", {}).setdefault("state", {})[key] = {"trusted_hash": value}
    text = "".join(lines)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError(refused) from None
    if _prune(parsed) != _prune(expected):
        raise ValueError(refused)
    return text


def _stage(path: Path, text: str, mode: int | None) -> str:
    """Write ``text`` to a fsynced temporary file beside ``path``; return its name."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
    except BaseException:
        os.unlink(temporary)
        raise
    return temporary


def has_table(snapshot: ConfigSnapshot, key: str) -> bool:
    """Whether ``key`` has its own ``[hooks.state."<key>"]`` header line."""
    lines = snapshot.text.splitlines(keepends=True)
    return ("hooks", "state", key) in _spans(lines) and key in snapshot.state


def remove_config(snapshot: ConfigSnapshot) -> None:
    """Delete a config.toml cairn created in this run, unless it changed meanwhile."""
    if _fingerprint(snapshot.target)[0] != snapshot.fingerprint:
        raise ValueError(
            f"{snapshot.path} changed while cairn was writing; cairn changed nothing. "
            "Run the command again."
        )
    snapshot.target.unlink()


def write_config(snapshot: ConfigSnapshot, text: str, *, backup: bool = True) -> None:
    """Back up the original bytes, then replace the file through any symlink, keeping
    its mode. Refuses when the file changed since :func:`read_config`; Codex does not
    take cairn's lock.

    Both files are staged and fsynced first, and the race check runs after that, just
    before the two renames. A Codex save that lands between that check and the rename
    of the config is still lost; no lock-free check can close that last window.
    """
    target = snapshot.target
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_path = target.with_name(target.name + BACKUP_SUFFIX)
    staged: list[tuple[str, Path]] = []
    try:
        if snapshot.exists and backup:
            staged.append((_stage(backup_path, snapshot.text, snapshot.mode), backup_path))
        staged.append((_stage(target, text, snapshot.mode), target))
        current, _ = _fingerprint(target)
        if current != snapshot.fingerprint:
            raise ValueError(
                f"{snapshot.path} changed while cairn was writing; cairn changed nothing. "
                "Run the command again."
            )
        for temporary, destination in staged:
            os.replace(temporary, destination)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        for temporary, _ in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)


def write_json(path: Path, text: str) -> None:
    """Write a shared JSON file through any symlink, keeping an existing file's mode."""
    target = path.resolve()
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    atomic_write(target, text, mode=mode)


# --- trust state (doctor and status) ------------------------------------------------


def table_state(table: object, expected_hash: str, version_ok: bool) -> str:
    """``trusted``, ``stale``, ``missing``, ``disabled`` or ``manual`` for one handler."""
    if isinstance(table, dict) and table.get("enabled") is False:
        return "disabled"
    if isinstance(table, dict) and table.get("trusted_hash") == expected_hash:
        return "trusted"
    if not version_ok:
        return "manual"
    return "stale" if isinstance(table, dict) else "missing"


def trust_state(
    hooks_path: Path,
    hooks_data: dict,
    config_state: dict,
    commands: set[str],
    receipt: dict,
    version_ok: bool,
) -> list[dict]:
    """Per cairn-owned SessionEnd group: key, state and who wrote the trusted table."""
    tables = [e for e in receipt.get("entries", []) if e.get("kind") == "config_toml_table"]
    groups = []
    for index, group in enumerate(session_end_groups(hooks_data)):
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        for position, handler in enumerate(handlers if isinstance(handlers, list) else []):
            if not isinstance(handler, dict) or handler.get("command") not in commands:
                continue
            if not isinstance(handler.get("type"), str):
                continue
            key = state_key(hooks_path, "SessionEnd", index, position)
            expected = trusted_hash("SessionEnd", handler)
            table = config_state.get(key)
            state = table_state(table, expected, version_ok)
            origin = None
            if state == "trusted":
                # An interrupted reinstall leaves two receipt entries for one key.
                cairn_wrote = any(
                    e.get("key") == key
                    and e.get("trusted_hash") == expected
                    and e.get("origin") == "written"
                    for e in tables
                )
                origin = "written" if cairn_wrote else "codex"
            groups.append({"index": index, "key": key, "state": state, "origin": origin})
    return groups


def session_end_groups(data: dict) -> list:
    """The raw SessionEnd group list; indexes are Codex's, so nothing is filtered out."""
    hooks = data.get("hooks") if isinstance(data, dict) else None
    groups = hooks.get("SessionEnd") if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


__all__ = [
    "MANUAL_STEP",
    "TRUST_FORMAT_VERIFIED",
    "ConfigSnapshot",
    "canonical_sha",
    "codex_version",
    "edit",
    "has_table",
    "in_range",
    "parse_version",
    "range_text",
    "read_config",
    "remove_config",
    "session_end_groups",
    "snake_case",
    "state_key",
    "table_header",
    "table_state",
    "trust_state",
    "trusted_hash",
    "verified_range",
    "write_config",
    "write_json",
]
