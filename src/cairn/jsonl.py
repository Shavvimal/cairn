"""Low-level helpers for reading Claude Code's native JSONL session store.

These primitives are config-free on purpose: they take the projects base directory
(or a file path) as an argument rather than reading :func:`cairn.config.get_config`
at import time. That lets both the export adapter (:mod:`cairn.sources.claude`, which
binds them to its configured store) and :mod:`cairn.recall` reuse one implementation
without either inheriting the other's import-time configuration load.

Claude Code writes one ``<session-id>.jsonl`` transcript per session under
``<projects_base>/<encoded-project-dir>/``. Subagent transcripts live beside it in
``<session-id>/subagents/agent-<agentId>.jsonl`` (plus ``agent-<agentId>.meta.json``),
and workflow agents one level deeper in ``subagents/workflows/wf_<id>/``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class JsonlStats:
    """What one read of a JSONL file saw, filled in place by :func:`iter_jsonl`.

    ``lines`` counts non-blank physical lines, including lines dropped by
    ``skip_prefixes`` (those count as lines only). ``records`` counts decoded JSON
    objects, ``malformed`` the lines that did not decode or decoded to another JSON
    value (still yielded, as before), and ``types`` the ``str(record.get("type"))``
    of each record. ``bytes`` is the file size
    when the read started. A source whose top-level type is too coarse (Codex wraps
    control events and messages in the same ``event_msg``) gives ``kind_of``, and
    ``kinds`` then counts its finer ``type/subtype`` names as well.
    """

    lines: int = 0
    records: int = 0
    malformed: int = 0
    bytes: int = 0
    types: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)
    kind_of: Callable[[dict], str | None] | None = None


def iter_jsonl(
    file_path: Path, skip_prefixes: tuple[str, ...] = (), stats: JsonlStats | None = None
) -> Iterator[tuple[int, dict]]:
    """Yield ``(line_index, record)`` for each valid JSON line in a JSONL file.

    ``line_index`` is the 0-based physical line number - it advances even past the
    blank or torn lines that are skipped - so a caller scanning only the head of a
    transcript can bail out after the first few records. Best-effort, like
    :func:`parse_jsonl`: a missing file yields nothing and malformed lines are
    skipped rather than aborting the read. The caller owns the file handle's
    ``OSError`` / ``UnicodeDecodeError`` (raised lazily during iteration).

    ``skip_prefixes`` drops a line before JSON decoding when its first 40 characters
    contain any of the given markers (for example ``'"type":"api-request'``), so a
    caller can skip bulky record kinds it never reads without paying to decode them.

    ``stats`` (optional) is filled in place as the lines are read, so a caller can
    tell an empty file from one whose lines no longer parse (the drift guard).
    """
    if not file_path.exists():
        return
    with open(file_path, encoding="utf-8") as f:
        if stats is not None:
            stats.bytes = file_path.stat().st_size
        for index, line in enumerate(f):
            if skip_prefixes:
                head = line[:40]
                if any(marker in head for marker in skip_prefixes):
                    if stats is not None:
                        stats.lines += 1
                    continue
            line = line.strip()
            if not line:
                continue
            if stats is not None:
                stats.lines += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if stats is not None:
                    stats.malformed += 1
                continue
            if stats is not None and not isinstance(record, dict):
                stats.malformed += 1  # valid JSON, but no source writes a non-object record
            elif stats is not None:
                stats.records += 1
                stats.types[str(record.get("type"))] += 1
                if stats.kind_of is not None and (kind := stats.kind_of(record)):
                    stats.kinds[kind] += 1
            yield index, record


def parse_jsonl(file_path: Path) -> list[dict]:
    """Parse a JSONL file into a list of records.

    Best-effort by design: a missing file yields ``[]`` and individual malformed
    lines are skipped rather than aborting the whole read - these transcripts are
    appended to live by another process, so a torn final line is normal. Callers
    that require a specific session must check for an empty result themselves.
    """
    return [record for _, record in iter_jsonl(file_path)]


def iter_session_files(base: Path) -> list[Path]:
    """All non-empty ``*.jsonl`` session files one level under ``base``."""
    files: list[Path] = []
    if not base.is_dir():
        return files
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.stat().st_size > 0:
                files.append(jsonl)
    return files


def find_session_jsonl(base: Path, session_id: str) -> Path | None:
    """Find a session's JSONL transcript under ``base`` by exact id, else ``None``."""
    if not base.is_dir():
        return None
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def iter_subagent_files(session_jsonl: Path) -> list[Path]:
    """Every non-empty subagent transcript of one session, sorted by path.

    Covers both layouts: ``<session>/subagents/agent-*.jsonl`` and the workflow
    agents under ``<session>/subagents/workflows/*/agent-*.jsonl``.
    """
    folder = session_jsonl.with_suffix("") / "subagents"
    if not folder.is_dir():
        return []
    candidates = [*folder.glob("agent-*.jsonl"), *folder.glob("workflows/*/agent-*.jsonl")]
    return sorted(p for p in candidates if p.is_file() and p.stat().st_size > 0)


def subagent_meta_path(agent_jsonl: Path) -> Path:
    """``agent-<id>.meta.json`` beside a subagent transcript (it might not exist)."""
    return agent_jsonl.with_name(agent_jsonl.stem + ".meta.json")


def workflow_run_of(agent_jsonl: Path) -> str | None:
    """The ``wf_<id>`` run that owns a workflow agent's transcript, else ``None``."""
    run = agent_jsonl.parent
    if run.parent.name == "workflows" and run.name.startswith("wf_"):
        return run.name
    return None


def subagent_parent_jsonl(agent_jsonl: Path) -> Path:
    """The parent session transcript implied by a subagent transcript's location.

    ``<enc>/<sid>/subagents/agent-x.jsonl`` and
    ``<enc>/<sid>/subagents/workflows/wf_y/agent-x.jsonl`` both give ``<enc>/<sid>.jsonl``.
    Raises ``ValueError`` for a path in neither layout.
    """
    run_dir = agent_jsonl.parent
    subagents = run_dir.parent.parent if workflow_run_of(agent_jsonl) else run_dir
    if subagents.name != "subagents" or not agent_jsonl.name.startswith("agent-"):
        raise ValueError(f"not a subagent transcript path: {agent_jsonl}")
    session_folder = subagents.parent
    return session_folder.parent / f"{session_folder.name}.jsonl"


def find_subagent_jsonl(base: Path, agent_id: str) -> tuple[Path, Path] | None:
    """Locate ``agent-<agent_id>.jsonl`` under ``base``: ``(child, parent)`` or ``None``."""
    if not base.is_dir():
        return None
    name = f"agent-{agent_id}.jsonl"
    for pattern in (f"*/*/subagents/{name}", f"*/*/subagents/workflows/*/{name}"):
        for child in sorted(base.glob(pattern)):
            if child.is_file() and child.stat().st_size > 0:
                return child, subagent_parent_jsonl(child)
    return None


def newest_mtime(session_jsonl: Path) -> float:
    """The latest mtime over a session transcript and all its subagent transcripts.

    Background subagents finish after the parent goes quiet, so a windowed export
    selects a parent by this value rather than by the parent file alone.
    """
    newest = session_jsonl.stat().st_mtime
    for child in iter_subagent_files(session_jsonl):
        newest = max(newest, child.stat().st_mtime)
        meta = subagent_meta_path(child)
        if meta.exists():
            newest = max(newest, meta.stat().st_mtime)
    return newest


def tool_result_text(content: object) -> str:
    """Stringify tool_result content (a string, or a list of text/image parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                itype = item.get("type")
                if itype == "text":
                    parts.append(item.get("text", ""))
                elif itype == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(item))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content is not None else ""


__all__ = [
    "JsonlStats",
    "find_session_jsonl",
    "find_subagent_jsonl",
    "iter_jsonl",
    "iter_session_files",
    "iter_subagent_files",
    "newest_mtime",
    "parse_jsonl",
    "subagent_meta_path",
    "subagent_parent_jsonl",
    "tool_result_text",
    "workflow_run_of",
]
