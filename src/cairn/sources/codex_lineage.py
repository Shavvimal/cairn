"""Parent/child relations across Codex rollouts from their first line only.

Every rollout starts with a ``session_meta`` record naming the thread and, for a
subagent, ``source.subagent.thread_spawn`` with the parent thread. Reading that one
line per file tells a windowed or targeted export every parent-child relation without
parsing bodies. Results are cached per file under
``$XDG_CACHE_HOME/cairn/lineage/<sha256(store)[:16]>.json`` and trusted only while the
file's ``(size, mtime_ns)`` is unchanged. A missing or corrupt cache is a cold cache.
A torn or foreign first line is skipped, never raised.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ..storage import atomic_write

LINEAGE_CACHE_VERSION = 1


def spawn_fields(payload: dict) -> dict:
    """Lineage keys from a ``session_meta`` payload (empty values for a root thread).

    ``parent_session_id`` comes only from ``thread_spawn``: a user-level fork also
    names a parent thread but is a session of its own, not a subagent.
    """
    source = payload.get("source")
    spawn: dict = {}
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict) and isinstance(subagent.get("thread_spawn"), dict):
            spawn = subagent["thread_spawn"]
    depth = spawn.get("depth")

    def text(key: str) -> str | None:
        value = spawn.get(key) or payload.get(key)
        return value if isinstance(value, str) and value else None

    parent = spawn.get("parent_thread_id")
    return {
        "parent_session_id": parent if isinstance(parent, str) and parent else None,
        "spawn_depth": depth if isinstance(depth, int) and not isinstance(depth, bool) else None,
        "agent_type": text("agent_role"),
        "agent_nickname": text("agent_nickname"),
        "agent_path": text("agent_path"),
    }


@dataclass(frozen=True)
class RolloutMeta:
    id: str
    parent: str | None
    role: str | None
    nickname: str | None
    path: str | None  # the agent path inside the team (``/root/worker``), not a file path
    originator: str | None
    thread_source: str | None

    @classmethod
    def from_payload(cls, payload: dict) -> RolloutMeta | None:
        thread_id = payload.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            return None
        spawn = spawn_fields(payload)

        def text(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None

        return cls(
            thread_id,
            spawn["parent_session_id"],
            spawn["agent_type"],
            spawn["agent_nickname"],
            spawn["agent_path"],
            text("originator"),
            text("thread_source"),
        )


def read_first_meta(path: Path) -> RolloutMeta | None:
    """The ``session_meta`` on a rollout's first line, or None if torn or absent."""
    # Binary read: a text-mode readline decodes a whole buffer, so a bad byte later in
    # the file would hide a valid first line (and that file's family membership).
    try:
        with open(path, "rb") as handle:
            raw = handle.readline()
    except OSError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return RolloutMeta.from_payload(payload) if isinstance(payload, dict) else None


class Lineage:
    """Rollout metas keyed by file, with parent and child lookups."""

    def __init__(self, metas: dict[Path, RolloutMeta]) -> None:
        self._metas = metas
        self._children: dict[str, list[Path]] = {}
        self._parents: dict[str, str] = {}
        self._paths: dict[str, list[Path]] = {}
        for path in sorted(metas):
            meta = metas[path]
            self._paths.setdefault(meta.id, []).append(path)
            if meta.parent and meta.parent != meta.id:
                self._children.setdefault(meta.parent, []).append(path)
                self._parents[meta.id] = meta.parent

    def children_of(self, parent_id: str) -> list[Path]:
        """Rollout files of every direct child of ``parent_id``."""
        return list(self._children.get(parent_id, []))

    def children_of_all(self) -> dict[str, list[Path]]:
        return {parent: list(paths) for parent, paths in self._children.items()}

    def parent_of(self, child_id: str) -> str | None:
        return self._parents.get(child_id)

    def paths_of(self, thread_id: str) -> list[Path]:
        """Every rollout fragment whose first meta names ``thread_id``."""
        return list(self._paths.get(thread_id, []))

    def meta_of(self, path: Path) -> RolloutMeta | None:
        return self._metas.get(path)


def lineage_cache_path(store: Path) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(str(store.resolve()).encode()).hexdigest()[:16]
    return cache / "cairn" / "lineage" / f"{key}.json"


def _load_cache(path: Path) -> dict:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(cached, dict) or cached.get("version") != LINEAGE_CACHE_VERSION:
        return {}
    files = cached.get("files")
    return files if isinstance(files, dict) else {}


def _cached_meta(entry: object, size: int, mtime_ns: int) -> tuple[bool, RolloutMeta | None]:
    """``(hit, meta)`` for one cache entry validated against the live stat."""
    if not isinstance(entry, dict):
        return False, None
    if entry.get("size") != size or entry.get("mtime_ns") != mtime_ns:
        return False, None
    meta = entry.get("meta")
    if meta is None:
        return True, None
    try:
        return True, RolloutMeta(**meta)
    except TypeError:
        return False, None


def rollout_lineage(store: Path) -> Lineage:
    """Lineage over every non-empty ``rollout-*.jsonl`` under ``store``."""
    cache_file = lineage_cache_path(store)
    cached = _load_cache(cache_file)
    files: dict[str, dict] = {}
    metas: dict[Path, RolloutMeta] = {}
    changed = False
    if store.is_dir():
        for path in sorted(store.rglob("rollout-*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size == 0:
                continue
            rel = path.relative_to(store).as_posix()
            hit, meta = _cached_meta(cached.get(rel), stat.st_size, stat.st_mtime_ns)
            if not hit:
                meta = read_first_meta(path)
                changed = True
            files[rel] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "meta": asdict(meta) if meta else None,
            }
            if meta is not None:
                metas[path] = meta
    if changed or set(files) != set(cached):
        try:
            atomic_write(
                cache_file,
                json.dumps({"version": LINEAGE_CACHE_VERSION, "files": files}, sort_keys=True),
            )
        except OSError as exc:
            print(f"Warning: cannot write lineage cache {cache_file}: {exc}", file=sys.stderr)
    return Lineage(metas)


__all__ = [
    "Lineage",
    "RolloutMeta",
    "lineage_cache_path",
    "read_first_meta",
    "rollout_lineage",
    "spawn_fields",
]
