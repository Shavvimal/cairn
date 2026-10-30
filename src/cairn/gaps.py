"""The trailing-window gap check: native sessions on disk with no exported document.

``cairn sync`` (every mode but ``--hook``) and ``cairn doctor`` compare the session
ids a source store holds for the last :data:`GAP_WINDOW_DAYS` days with the
documents in that source's collection. Enumeration reads no transcript contents: one
``stat`` per candidate file, one catalog walk per collection and, for Antigravity,
one read-only query of agy's conversation index. A file changed in
the last :data:`GAP_SETTLE_S` seconds is still being written and is left to the
next run.

``missing = on_disk - exported - held``, where ``held`` are the ids with an open
error or info finding from the parser-drift guard (:mod:`cairn.sources.guards`):
those already have a reason on record. Sync re-exports the missing ids once with a
``--since 30d`` export, scans again and fails the ``gap-check:<collection>`` step
when any id is still missing.

Covered: Claude Code (sessions and subagents), Codex and Antigravity. Cursor is out:
enumerating it means decoding every composer blob, and composers without headers
are skipped on purpose, which would read as permanent gaps. Granola and service
docs have no session files to compare.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .catalog import load_catalog
from .config import CairnConfig
from .harnesses.antigravity import conversation_transcript
from .jsonl import iter_session_files, iter_subagent_files
from .registry import BY_NAME
from .sources.codex_lineage import rollout_id
from .sources.guards import REF_CHARS, FindingsStore
from .store import native_id_of

GAP_WINDOW_DAYS = 30
GAP_SETTLE_S = 600  # equal to codex_replay.LIVE_CHILD_WINDOW_S
GAP_HEAL_TIMEOUT_S = 600
GAP_SOURCES = ("claude", "codex", "antigravity")
MAX_REFS = 5


def _settled(path: Path, start: float, end: float) -> bool:
    """True when ``path`` changed inside ``[start, end]``."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return start <= mtime <= end


def claude_sessions(store: Path, start: float, end: float) -> dict[str, Path]:
    """Parent sessions (``<id>.jsonl``) and subagents (``agent-<id>``) in the window.

    A top-level ``agent-*.jsonl`` is a legacy side-thread that is never exported.
    """
    found: dict[str, Path] = {}
    for transcript in iter_session_files(store):
        if transcript.stem.startswith("agent-"):
            continue
        if _settled(transcript, start, end):
            found[transcript.stem] = transcript
        for child in iter_subagent_files(transcript):
            if _settled(child, start, end):
                found[child.stem] = child
    return found


def codex_sessions(store: Path, start: float, end: float) -> dict[str, Path]:
    """Rollouts in the window, keyed by the thread id in the filename."""
    found: dict[str, Path] = {}
    if not store.is_dir():
        return found
    for rollout in store.rglob("rollout-*.jsonl"):
        try:
            if rollout.stat().st_size == 0:
                continue
        except OSError:
            continue
        if _settled(rollout, start, end):
            found.setdefault(rollout_id(rollout), rollout)
    return found


def antigravity_sessions(store: Path, start: float, end: float) -> dict[str, Path]:
    """``brain/<id>/`` conversations whose activity time is in the window.

    The activity time is the one the agy export's ``--since`` filters on: the
    index's ``last_modified`` when agy recorded one, else the transcript mtime. With
    the mtime alone, a transcript touched long after its last turn read as missing
    while the heal's ``--since 30d`` export skipped it, so the gap never closed.
    The index is one read-only SQLite query, not a transcript read.
    """
    found: dict[str, Path] = {}
    brain = store / "brain"
    if not brain.is_dir():
        return found
    summaries = _agy_activity(store)
    for folder in brain.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        transcript = conversation_transcript(store, folder.name)
        if transcript is None:
            continue
        activity = summaries.get(folder.name)
        if activity is not None:
            if start <= activity <= end:
                found[folder.name] = transcript
        elif _settled(transcript, start, end):
            found[folder.name] = transcript
    return found


def _agy_activity(store: Path) -> dict[str, float]:
    """``last_modified`` per conversation from agy's index (empty when absent)."""
    from .sources.antigravity import read_summaries

    summaries = read_summaries(store / "conversation_summaries.db")
    return {sid: s.last_modified for sid, s in summaries.items() if s.last_modified}


ENUMERATORS = {
    "claude": claude_sessions,
    "codex": codex_sessions,
    "antigravity": antigravity_sessions,
}


@dataclass
class GapScan:
    source: str
    collection: str
    on_disk: dict[str, Path]
    exported: set[str]
    held: set[str]

    @property
    def missing(self) -> list[str]:
        return sorted(set(self.on_disk) - self.exported - self.held)

    @property
    def held_on_disk(self) -> int:
        """On-disk ids with no document that a drift finding explains."""
        return len((set(self.on_disk) - self.exported) & self.held)


def refs(source: str, ids: list[str], limit: int = MAX_REFS) -> str:
    """``codex:019e1000, claude:5e551000 (+3 more)``."""
    shown = ", ".join(f"{source}:{native_id[:REF_CHARS]}" for native_id in ids[:limit])
    return shown + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "")


def gap_sources(config: CairnConfig) -> list[str]:
    """The covered sources whose collection is configured, enabled and has a store."""
    result = []
    for source in GAP_SOURCES:
        coll = config.collections.get(BY_NAME[source].collection)
        if coll is not None and coll.sync.enabled and coll.store is not None:
            result.append(source)
    return result


def exported_ids(config: CairnConfig, source: str) -> set[str]:
    """Native ids with a session or subagent document in the collection (catalog walk)."""
    binding = BY_NAME[source]
    catalog = load_catalog(config.output_dir(binding.collection))
    ids = set()
    for _path, fm in catalog.items():
        if fm.get("type") in binding.session_types:
            native_id = native_id_of(fm, binding.id_field)
            if native_id is not None:
                ids.add(native_id)
    return ids


def scan(config: CairnConfig, source: str, now: float | None = None) -> GapScan:
    """One read-only pass: on disk, exported and held ids for ``source``."""
    binding = BY_NAME[source]
    store = config.collections[binding.collection].store
    now = time.time() if now is None else now
    start = now - GAP_WINDOW_DAYS * 86400
    on_disk = ENUMERATORS[source](store, start, now - GAP_SETTLE_S) if store else {}
    exported = exported_ids(config, source) if on_disk else set()
    held = FindingsStore(config.data_root).held(source) if on_disk else set()
    return GapScan(source, binding.collection, on_disk, exported, held)


__all__ = [
    "ENUMERATORS",
    "GAP_HEAL_TIMEOUT_S",
    "GAP_SETTLE_S",
    "GAP_SOURCES",
    "GAP_WINDOW_DAYS",
    "GapScan",
    "antigravity_sessions",
    "claude_sessions",
    "codex_sessions",
    "exported_ids",
    "gap_sources",
    "refs",
    "scan",
]
