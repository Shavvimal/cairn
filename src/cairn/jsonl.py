"""Low-level helpers for reading Claude Code's native JSONL session store.

These primitives are config-free on purpose: they take the projects base directory
(or a file path) as an argument rather than reading :func:`cairn.config.get_config`
at import time. That lets both the export adapter (:mod:`cairn.sources.claude`, which
binds them to its configured store) and :mod:`cairn.recall` reuse one implementation
without either inheriting the other's import-time configuration load.

Claude Code writes one ``<session-id>.jsonl`` transcript per session under
``<projects_base>/<encoded-project-dir>/``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path


def iter_jsonl(file_path: Path) -> Iterator[tuple[int, dict]]:
    """Yield ``(line_index, record)`` for each valid JSON line in a JSONL file.

    ``line_index`` is the 0-based physical line number - it advances even past the
    blank or torn lines that are skipped - so a caller scanning only the head of a
    transcript can bail out after the first few records. Best-effort, like
    :func:`parse_jsonl`: a missing file yields nothing and malformed lines are
    skipped rather than aborting the read. The caller owns the file handle's
    ``OSError`` / ``UnicodeDecodeError`` (raised lazily during iteration).
    """
    if not file_path.exists():
        return
    with open(file_path, encoding="utf-8") as f:
        for index, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
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
    "find_session_jsonl",
    "iter_jsonl",
    "iter_session_files",
    "parse_jsonl",
    "tool_result_text",
]
